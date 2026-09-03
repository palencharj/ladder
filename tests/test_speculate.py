"""Speculative execution tests.

The claim being tested is economic, not behavioural: **the number of paid
invocations must not grow with the number of tasks.** That is the entire reason
this mechanism exists, so most of these tests count calls rather than inspect
answers.

The second thing under test is the safety property. Speculation accepts work
from a cheap model on a verifier's say-so, so every path where the verifier
fails to speak clearly must fall back to re-running -- never to accepting.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ladder import tiers  # noqa: E402
from ladder.engines.base import Result  # noqa: E402
from ladder.pool import Task  # noqa: E402
from ladder.router import Router  # noqa: E402
from ladder.speculate import (  # noqa: E402
    CHUNK_CHARS,
    Drafted,
    Speculation,
    Speculator,
    chunk_drafts,
    target_rung_for,
)
from ladder.store import Store  # noqa: E402

CHECK_MARKER = "PROPOSED ANSWER:"


class SpecEngine:
    """Drafts free at rung 0, judges and re-runs at paid rungs.

    Counts local and paid invocations separately, because the whole point is
    that one grows with the task count and the other does not.

    A task whose prompt contains any string in `bad` is rejected by the
    verifier, standing in for the local model getting it wrong.
    """

    def __init__(self, bad: tuple[str, ...] = (), batch_ok: bool = True,
                 verdicts_parse: bool = True):
        self.bad = bad
        self.batch_ok = batch_ok
        self.verdicts_parse = verdicts_parse
        self.local_calls = 0
        self.paid_calls = 0
        self.batch_sizes: list[int] = []

    def available(self):
        return True, "fake"

    def batchable(self, prompts):
        return self.batch_ok and len(prompts) > 1

    def _rejects(self, text: str) -> bool:
        return any(b in text for b in self.bad)

    def _result(self, tier, text, ok=True):
        return Result(text=text, ok=ok, engine="fake", model=tier.model,
                      rung=tier.rung, tokens_in=10, tokens_out=10)

    def run(self, tier, system, prompt, max_tokens=8000):
        if tier.rung == 0:
            self.local_calls += 1
            return self._result(tier, f"draft::{prompt}")

        self.paid_calls += 1
        if CHECK_MARKER in prompt:
            verdict = "FAIL wrong" if self._rejects(prompt) else "PASS fine"
            return self._result(tier, verdict)
        return self._result(tier, f"paid::{prompt}")

    def run_batch(self, tier, system, prompts, max_tokens=8000):
        if not self.batch_ok:
            return None
        self.paid_calls += 1
        self.batch_sizes.append(len(prompts))
        if prompts and CHECK_MARKER in prompts[0]:
            if not self.verdicts_parse:
                return None
            return [
                self._result(tier, "FAIL wrong" if self._rejects(p) else "PASS fine")
                for p in prompts
            ]
        return [self._result(tier, f"paid::{p}") for p in prompts]


@pytest.fixture
def store():
    with tempfile.TemporaryDirectory() as d, Store(Path(d) / "t.db") as s:
        yield s


def spec_with(engine, **kw) -> Speculator:
    r = Router(**kw)
    r._ollama = engine
    r._api = engine
    r._cli = engine
    r.engine_for = lambda tier: engine
    return Speculator(r, store=kw.get("store"))


def tasks(n: int, kind: str = "doc", prefix: str = "t") -> list[Task]:
    return [Task(prompt=f"{prefix}{i}", kind=kind, rung=1, max_rung=1)
            for i in range(n)]


# --------------------------------------------------------------------------
# The economic claim
# --------------------------------------------------------------------------

def test_paid_invocations_do_not_grow_with_task_count():
    """The headline. 12 accepted drafts must cost ONE verification call."""
    eng = SpecEngine()
    out = spec_with(eng).run(tasks(12), chunk=12)

    assert out["spec"]["accepted"] == 12
    assert eng.paid_calls == 1, "one verify call should cover the whole chunk"
    assert eng.local_calls == 12, "every draft is generated locally"
    assert out["spec"]["paid_calls"] == 1


def test_scaling_the_batch_does_not_scale_the_bill():
    """Twice the tasks, same paid cost. This is the property that saturates in
    real speculative decoding and does not saturate here."""
    small = SpecEngine()
    large = SpecEngine()
    spec_with(small).run(tasks(6), chunk=24)
    spec_with(large).run(tasks(18), chunk=24)

    assert small.paid_calls == large.paid_calls == 1
    assert large.local_calls == 3 * small.local_calls


def test_rejects_are_rerun_together_in_one_more_call():
    """Misses cost one extra invocation for the whole rejected set, not one each."""
    eng = SpecEngine(bad=("bad",))
    mixed = tasks(4, prefix="good") + tasks(4, prefix="bad")
    out = spec_with(eng).run(mixed, chunk=12)

    assert out["spec"]["accepted"] == 4
    assert out["spec"]["rejected"] == 4
    assert eng.paid_calls == 2, "1 verify + 1 batched repair, regardless of miss count"
    assert out["spec"]["repair_calls"] == 1


def test_reported_saving_counts_invocations_not_dollars():
    """On a prepaid plan the binding cost is invocations x 35k of allowance."""
    out = spec_with(SpecEngine()).run(tasks(10), chunk=10)
    s = out["spec"]

    assert s["naive_calls"] == 10
    assert s["paid_calls"] == 1
    assert s["tokens_saved"] == 9 * tiers.CLI_OVERHEAD_TOKENS


# --------------------------------------------------------------------------
# Safety: never accept what was not actually checked
# --------------------------------------------------------------------------

def test_unparseable_verdicts_reject_everything_rather_than_accept():
    """A batch reply that will not parse must not be split across tasks.

    Handing task 3's verdict to task 4 is the misalignment failure the batch
    parser refuses elsewhere; approving an unchecked answer is worse still.
    """
    eng = SpecEngine(verdicts_parse=False)
    out = spec_with(eng).run(tasks(6), chunk=6)

    assert out["spec"]["accepted"] == 0
    assert out["spec"]["unverified"] == 6
    assert all(not r.get("speculative") for r in out["results"])


def test_a_failed_draft_never_reaches_the_verifier():
    """An empty local answer is a miss already; spending prompt room to have it
    judged would be pure waste."""
    class Empty(SpecEngine):
        def run(self, tier, system, prompt, max_tokens=8000):
            if tier.rung == 0:
                self.local_calls += 1
                return self._result(tier, "", ok=False)
            return super().run(tier, system, prompt, max_tokens)

    eng = Empty()
    out = spec_with(eng).run(tasks(4), chunk=4)

    assert out["spec"]["draft_failed"] == 4
    assert out["spec"]["accepted"] == 0
    assert out["spec"]["verify_calls"] == 0, "nothing was worth verifying"
    assert out["spec"]["repair_calls"] == 1, "but all four still get answered"


def test_accepted_results_record_who_verified_them():
    """An accepted answer came from rung 0 but carries the verifier's name, so
    a reader can tell it was checked rather than merely cheap."""
    out = spec_with(SpecEngine()).run(tasks(3), verify_rung=2, chunk=3)
    accepted = [r for r in out["results"] if r.get("speculative")]

    assert len(accepted) == 3
    for r in accepted:
        assert r["rung"] == 0
        assert r["tier"] == "local"
        assert r["verified_by"] == tiers.by_rung(2).name
        assert r["cost_usd"] == 0.0


def test_drafting_never_climbs_the_ladder():
    """Escalating during the draft phase would spend the very allowance the
    mechanism exists to protect -- the verifier decides, not the drafter."""
    class LocalFails(SpecEngine):
        def run(self, tier, system, prompt, max_tokens=8000):
            if tier.rung == 0:
                self.local_calls += 1
                return self._result(tier, "", ok=False)
            return super().run(tier, system, prompt, max_tokens)

    eng = LocalFails()
    spec_with(eng).run([Task(prompt="x", kind="doc", max_rung=5)], chunk=4)

    # One local attempt, then the repair path -- never a climb through 1..5.
    assert eng.local_calls == 1


# --------------------------------------------------------------------------
# Choosing the target rung
# --------------------------------------------------------------------------

def test_verify_rung_defaults_to_the_dearest_rung_the_work_warranted():
    """Verifying below the rung the task would have used approves answers that
    the real target might have rejected, making the saving imaginary."""
    mixed = [Task(prompt="a", kind="docstring"),   # rung 0
             Task(prompt="b", kind="implement")]   # rung 2
    out = spec_with(SpecEngine()).run(mixed, chunk=4)

    assert out["verify_rung"] == tiers.TASK_RUNGS["implement"]


def test_verify_rung_is_never_zero():
    """Rung 0 cannot verify itself: the drafter and the judge would be the same
    model, which checks nothing."""
    out = spec_with(SpecEngine()).run(
        [Task(prompt="a", kind="docstring")], chunk=4)
    assert out["verify_rung"] >= 1


def test_target_rung_follows_the_task_kind():
    assert target_rung_for(Task(prompt="x", kind="classify")) == 0
    assert target_rung_for(Task(prompt="x", kind="debug")) == tiers.TASK_RUNGS["debug"]
    assert target_rung_for(Task(prompt="x", kind="classify", rung=4)) == 4


# --------------------------------------------------------------------------
# Chunking
# --------------------------------------------------------------------------

def _d(prompt: str, text: str) -> Drafted:
    return Drafted(task=Task(prompt=prompt), text=text, ok=True)


def test_chunking_respects_the_count_bound():
    chunks = chunk_drafts([_d("p", "a") for _ in range(10)], size=4)
    assert [len(c) for c in chunks] == [4, 4, 2]


def test_chunking_respects_the_character_budget():
    """The verify prompt must physically contain every draft, so a few long
    answers split the chunk even when the count is small."""
    big = _d("p", "x" * (CHUNK_CHARS // 2))
    chunks = chunk_drafts([big, big, big], size=100)
    assert len(chunks) > 1


def test_an_oversized_draft_still_gets_its_own_chunk():
    """Dropping it would silently lose a task."""
    huge = _d("p", "x" * (CHUNK_CHARS * 3))
    chunks = chunk_drafts([huge, _d("p", "small")], size=100)
    assert sum(len(c) for c in chunks) == 2


def test_chunking_an_empty_list_is_empty():
    assert chunk_drafts([]) == []


# --------------------------------------------------------------------------
# Pipelining
# --------------------------------------------------------------------------

def test_pipelining_does_not_change_the_answers():
    """Overlapping draft and verify is a scheduling change only; identical
    results, different wall clock."""
    mixed = tasks(3, prefix="good") + tasks(3, prefix="bad")
    on = spec_with(SpecEngine(bad=("bad",))).run(list(mixed), chunk=2, pipeline=True)
    off = spec_with(SpecEngine(bad=("bad",))).run(list(mixed), chunk=2, pipeline=False)

    assert on["spec"]["accepted"] == off["spec"]["accepted"] == 3
    assert on["spec"]["rejected"] == off["spec"]["rejected"] == 3
    assert sorted(r["title"] for r in on["results"]) == \
           sorted(r["title"] for r in off["results"])


def test_every_task_is_answered_across_several_chunks():
    """Chunking must not lose work at the seams."""
    out = spec_with(SpecEngine(bad=("bad",))).run(
        tasks(5, prefix="good") + tasks(4, prefix="bad"), chunk=2)
    assert len(out["results"]) == 9


# --------------------------------------------------------------------------
# Accounting
# --------------------------------------------------------------------------

def test_acceptance_rate_is_measured_not_assumed():
    s = Speculation()
    for _ in range(8):
        s.note("doc", True)
        s.accepted += 1
    for _ in range(2):
        s.note("doc", False)
        s.rejected += 1
    assert s.acceptance == 0.8
    assert s.as_dict()["by_kind"]["doc"] == {"accepted": 8, "drafted": 10}


def test_acceptance_is_zero_when_nothing_was_checked():
    """No division by zero on an all-drafts-failed run."""
    assert Speculation().acceptance == 0.0


def test_per_kind_acceptance_is_tracked_separately():
    """So a kind the local model cannot do can be found and stopped."""
    out = spec_with(SpecEngine(bad=("bad",))).run(
        [Task(prompt="good1", kind="doc", rung=1, max_rung=1),
         Task(prompt="bad1", kind="test", rung=1, max_rung=1)], chunk=4)
    by_kind = out["spec"]["by_kind"]
    assert by_kind["doc"]["accepted"] == 1
    assert by_kind["test"]["accepted"] == 0


def test_empty_task_list_is_a_no_op():
    out = spec_with(SpecEngine()).run([])
    assert out["ok"] and out["results"] == []
    assert out["spec"]["paid_calls"] == 0


def test_repair_falls_back_to_single_calls_when_batching_is_refused():
    """Slower and dearer, but never misaligned -- the same trade the batch
    parser makes."""
    eng = SpecEngine(bad=("bad",), batch_ok=False)
    out = spec_with(eng).run(tasks(3, prefix="bad"), chunk=4)
    assert out["spec"]["rejected"] + out["spec"]["unverified"] == 3
    assert len(out["results"]) == 3


# --------------------------------------------------------------------------
# Tracking: a paid call that nothing recorded is the one kind never allowed
# --------------------------------------------------------------------------

def test_repairs_are_recorded_in_the_store(store):
    """Repairs bypass run_job, so without explicit recording they would be
    invisible paid calls -- spending allowance the dashboard never shows."""
    sp = spec_with(SpecEngine(bad=("bad",)), store=store)
    sp.store = store
    out = sp.run(tasks(2, prefix="bad"), chunk=4)

    repaired = [r for r in out["results"] if not r.get("speculative")]
    assert repaired and all(r["job_id"] for r in repaired)
    for r in repaired:
        job = store.get_job(r["job_id"])
        assert job is not None
        assert job["final_rung"] == out["verify_rung"]


def test_repair_hands_the_target_the_draft_and_the_objection():
    """Re-authoring from scratch throws away work the local box already did,
    and makes the model guess at what was wrong."""
    seen = []

    class Watch(SpecEngine):
        def run_batch(self, tier, system, prompts, max_tokens=8000):
            if prompts and "REJECTED DRAFT:" in prompts[0]:
                seen.extend(prompts)
            return super().run_batch(tier, system, prompts, max_tokens)

    spec_with(Watch(bad=("bad",))).run(tasks(2, prefix="bad"), chunk=4)
    assert seen, "repair never ran"
    assert "REJECTED DRAFT:" in seen[0]
    assert "draft::bad0" in seen[0], "the draft itself must be carried over"
    assert "WHY IT WAS REJECTED:" in seen[0]


def test_local_share_counts_only_accepted_draft_tokens():
    """A rejected draft was regenerated at the paid tier. Counting its tokens
    as local generation would flatter the number with discarded work."""
    s = Speculation()
    s.local_tokens, s.paid_tokens = 900, 100
    assert s.local_share == 0.9
    assert Speculation().local_share == 0.0


def test_mixed_kind_repair_uses_one_system_prompt():
    """Router.run_batch takes its system prompt from the FIRST task, so routing
    mixed kinds through it would apply one kind's instructions to all of them."""
    systems = []

    class Watch(SpecEngine):
        def run_batch(self, tier, system, prompts, max_tokens=8000):
            systems.append(system)
            return super().run_batch(tier, system, prompts, max_tokens)

    sp = spec_with(Watch(bad=("bad",)))
    sp.run([Task(prompt="bad-a", kind="doc", rung=1, max_rung=1),
            Task(prompt="bad-b", kind="test", rung=1, max_rung=1)], chunk=4)

    repair_systems = [s for s in systems if "rejected" in s.lower()]
    assert len(set(repair_systems)) == 1, "one homogeneous repair system prompt"


def test_a_draft_that_raises_is_not_silently_dropped():
    """A future's exception stays hidden until someone asks for the result.
    An unasked-for future means the task vanishes with no result at all."""
    class Boom(SpecEngine):
        def run(self, tier, system, prompt, max_tokens=8000):
            if tier.rung == 0 and "boom" in prompt:
                raise RuntimeError("local engine exploded")
            return super().run(tier, system, prompt, max_tokens)

    out = spec_with(Boom()).run(
        [Task(prompt="fine", kind="doc", rung=1, max_rung=1),
         Task(prompt="boom", kind="doc", rung=1, max_rung=1)], chunk=4)

    assert len(out["results"]) == 2, "the exploding task must still be answered"


def test_an_unreachable_checker_does_not_approve_local_output():
    """run_job may fail open on a broken adjudicator, but speculation must not:
    the check is the only thing standing behind a rung-0 answer."""
    class NoChecker(SpecEngine):
        def run(self, tier, system, prompt, max_tokens=8000):
            if tier.rung > 0 and CHECK_MARKER in prompt:
                return self._result(tier, "", ok=False)
            return super().run(tier, system, prompt, max_tokens)

        def run_batch(self, tier, system, prompts, max_tokens=8000):
            if prompts and CHECK_MARKER in prompts[0]:
                return None      # checker unreachable
            return super().run_batch(tier, system, prompts, max_tokens)

    out = spec_with(NoChecker()).run(tasks(1), chunk=4)
    assert out["spec"]["accepted"] == 0
    assert not any(r.get("speculative") for r in out["results"])


def test_store_and_in_run_local_share_agree(store):
    """Two sources of truth that disagree are worse than one that is wrong.
    The verify call's tokens must land on the drafts it judged, in both."""
    sp = spec_with(SpecEngine(), store=store)
    sp.store = store
    out = sp.run(tasks(4), chunk=4)

    assert not out["telemetry_errors"]
    from_store = store.speculation_report()
    assert from_store["total"] == 4
    assert from_store["local_tokens"] == out["spec"]["local_tokens"]
    assert from_store["paid_tokens"] == out["spec"]["paid_tokens"]
    assert abs(from_store["local_share"] - out["spec"]["local_share"]) < 1e-9


# --------------------------------------------------------------------------
# Kind inference: the caller should not need to know the tier taxonomy
# --------------------------------------------------------------------------

from ladder.classify import explain, infer_kind, infer_rung  # noqa: E402


@pytest.mark.parametrize("prompt,want", [
    ("Add docstrings to every function in pool.py", "docstring"),
    ("Classify these 40 issues as BUG, FEATURE or QUESTION", "classify"),
    ("Summarize what this function does in one sentence", "summarize"),
    ("Extract all the constant names from this module", "extract"),
    ("Write a commit message for the speculation work", "commit_message"),
    ("Review this file for correctness bugs", "review"),
    ("Refactor the router to extract the escalation loop", "refactor"),
    ("Debug why the batch parser returns None", "debug"),
    ("Write unit tests for chunk_drafts", "test"),
])
def test_kind_is_inferred_from_the_prompt(prompt, want):
    assert infer_kind(prompt) == want


def test_judgement_signals_beat_mechanical_ones():
    """Prompts routinely match several patterns. Getting this wrong in the
    cheap direction produces a confident, useless answer, so the demanding
    reading must win."""
    assert infer_kind("Write a docstring explaining why this bug happens") == "debug"
    assert infer_kind("Summarize the root cause of the deadlock") == "debug"


def test_bug_alone_is_not_debugging():
    """'classify these as BUG' is a rung-0 classification, not a debug job."""
    assert infer_kind("Classify each issue as BUG or FEATURE") == "classify"


def test_an_unmatched_prompt_lands_somewhere_it_can_actually_work():
    """The failure mode of an unrecognised prompt must be 'runs on a capable
    tier', never 'runs free and comes back wrong'."""
    assert infer_kind("Make the coffee machine work") == tiers.DEFAULT_KIND
    assert infer_rung("Make the coffee machine work") >= 2
    assert explain("Make the coffee machine work")["matched"] is False


def test_empty_prompt_does_not_crash():
    assert infer_kind("") == tiers.DEFAULT_KIND
    assert infer_kind(None) == tiers.DEFAULT_KIND


def test_mechanical_kinds_actually_land_on_the_free_tier():
    """The inference is only useful if the kinds it picks are the free ones."""
    for p in ("Add docstrings everywhere", "Classify these tickets",
              "Summarize each function", "Extract the imports"):
        assert explain(p)["free"], p
