"""Unit tests. No network, no models, no API key -- these must pass in CI.

Engine behaviour is faked so the escalation logic itself is what gets tested.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ladder import prompts, tiers  # noqa: E402
from ladder.engines.base import Result  # noqa: E402
from ladder.pool import Swarm, Task, TierGate  # noqa: E402
from ladder.router import (  # noqa: E402
    Router,
    json_parses,
    python_parses,
    strip_fence,
)
from ladder.store import Store  # noqa: E402

# --------------------------------------------------------------------------
# Fakes
# --------------------------------------------------------------------------

class FakeEngine:
    """Succeeds only at or above `succeed_at_rung`."""

    def __init__(self, succeed_at_rung: int = 0, text: str = "ok"):
        self.succeed_at_rung = succeed_at_rung
        self.text = text
        self.calls: list[int] = []

    def available(self):
        return True, "fake"

    def run(self, tier, system, prompt, max_tokens=8000):
        self.calls.append(tier.rung)
        ok = tier.rung >= self.succeed_at_rung
        return Result(
            text=self.text if ok else "",
            ok=ok, engine="fake", model=tier.model, rung=tier.rung,
            tokens_in=10, tokens_out=20,
            cost_usd=tiers.estimate_cost(tier, 10, 20),
            error="" if ok else f"fake failure at rung {tier.rung}",
        )


@pytest.fixture
def store():
    with tempfile.TemporaryDirectory() as d, Store(Path(d) / "t.db") as s:
        yield s


def router_with(engine, **kw):
    r = Router(**kw)
    r._ollama = engine
    r._api = engine
    r._cli = engine
    r.engine_for = lambda tier: engine
    return r


# --------------------------------------------------------------------------
# Ladder shape
# --------------------------------------------------------------------------

def test_ladder_rungs_are_contiguous_and_ordered():
    assert [t.rung for t in tiers.LADDER] == list(range(len(tiers.LADDER)))


def test_price_is_monotonic_up_the_ladder():
    paid = [t for t in tiers.LADDER if t.engine != "ollama"]
    for a, b in zip(paid, paid[1:], strict=False):
        assert b.price_out >= a.price_out, f"{b.name} cheaper than {a.name}"


def test_rung_zero_is_free_and_local():
    t = tiers.by_rung(0)
    assert t.engine == "ollama" and t.price_in == 0 and t.price_out == 0


def test_haiku_never_sends_effort():
    """Haiku 4.5 returns HTTP 400 if output_config.effort is present."""
    assert tiers.by_name("haiku").effort is None


def test_local_concurrency_is_small():
    """CPU inference is near zero-sum; a wide local fan-out just thrashes."""
    assert tiers.by_rung(0).concurrency <= 4


def test_api_tiers_fan_out_wider_than_local():
    assert tiers.by_name("haiku").concurrency > tiers.by_rung(0).concurrency


@pytest.mark.parametrize("rung,expected", [(-5, 0), (0, 0), (3, 3), (99, tiers.MAX_RUNG)])
def test_by_rung_clamps(rung, expected):
    assert tiers.by_rung(rung).rung == expected


def test_by_name_rejects_unknown():
    with pytest.raises(KeyError):
        tiers.by_name("gpt-9")


# --------------------------------------------------------------------------
# Resolution
# --------------------------------------------------------------------------

def test_mechanical_kinds_start_free():
    for kind in ("classify", "docstring", "boilerplate", "rename", "triage"):
        assert tiers.resolve(kind=kind).rung == 0, kind


def test_harder_kinds_start_higher():
    assert tiers.resolve(kind="implement").rung >= 2
    assert tiers.resolve(kind="architect").rung >= tiers.resolve(kind="implement").rung


def test_resolution_precedence_is_tier_then_rung_then_kind():
    assert tiers.resolve(kind="classify", rung=4, tier_name="haiku").name == "haiku"
    assert tiers.resolve(kind="classify", rung=4).rung == 4
    assert tiers.resolve(kind="classify").rung == 0


def test_unknown_kind_falls_back_to_default():
    assert tiers.resolve(kind="nonsense").rung == tiers.TASK_RUNGS[tiers.DEFAULT_KIND]


def test_every_task_kind_has_a_prompt():
    for kind in tiers.TASK_RUNGS:
        assert prompts.system_for(kind), kind


def test_estimate_cost_matches_rate_card():
    t = tiers.by_name("haiku")  # $1 in / $5 out per Mtok
    assert tiers.estimate_cost(t, 1_000_000, 0) == pytest.approx(1.0)
    assert tiers.estimate_cost(t, 0, 1_000_000) == pytest.approx(5.0)


# --------------------------------------------------------------------------
# Verifiers
# --------------------------------------------------------------------------

def test_strip_fence_removes_markdown_wrapper():
    assert strip_fence("```python\nx = 1\n```") == "x = 1"
    assert strip_fence("x = 1") == "x = 1"


def test_python_verifier():
    assert python_parses("def f():\n    return 1")
    assert python_parses("```python\ndef f():\n    return 1\n```")
    assert not python_parses("def f( :")


def test_json_verifier():
    assert json_parses('{"a": 1}')
    assert json_parses('```json\n{"a": 1}\n```')
    assert not json_parses("{a: 1,}")


# --------------------------------------------------------------------------
# Escalation
# --------------------------------------------------------------------------

def test_success_at_first_rung_never_escalates(store):
    eng = FakeEngine(succeed_at_rung=0)
    out = router_with(eng, store=store).run_job(prompt="p", kind="classify")
    assert out["ok"] and out["rung"] == 0 and out["escalations"] == 0
    assert eng.calls == [0]


def test_escalates_one_rung_at_a_time(store):
    eng = FakeEngine(succeed_at_rung=3)
    out = router_with(eng, store=store).run_job(prompt="p", kind="classify")
    assert out["ok"] and out["rung"] == 3
    assert eng.calls == [0, 1, 2, 3], "must climb linearly, never skip"


def test_max_rung_caps_escalation(store):
    eng = FakeEngine(succeed_at_rung=5)
    out = router_with(eng, store=store).run_job(
        prompt="p", kind="classify", max_rung=2)
    assert not out["ok"]
    assert eng.calls == [0, 1, 2], "must not climb past max_rung"


def test_max_rung_equal_to_start_forbids_escalation(store):
    eng = FakeEngine(succeed_at_rung=5)
    out = router_with(eng, store=store).run_job(
        prompt="p", kind="classify", rung=0, max_rung=0)
    assert not out["ok"] and eng.calls == [0]


def test_max_rung_below_start_is_raised_to_start(store):
    """A ceiling under the floor must not produce a zero-attempt job."""
    eng = FakeEngine(succeed_at_rung=9)
    out = router_with(eng, store=store).run_job(
        prompt="p", rung=3, max_rung=1)
    assert eng.calls == [3], "should still attempt the requested start rung"
    assert not out["ok"]


def test_failed_verifier_forces_escalation(store):
    """Engine reports success, but the output is not valid Python."""

    class BadPython(FakeEngine):
        def run(self, tier, system, prompt, max_tokens=8000):
            self.calls.append(tier.rung)
            text = "def f( :" if tier.rung < 2 else "def f():\n    return 1"
            return Result(text=text, ok=True, engine="fake",
                          model=tier.model, rung=tier.rung)

    eng = BadPython()
    out = router_with(eng, store=store).run_job(
        prompt="p", kind="classify", verify="python")
    assert out["ok"] and out["rung"] == 2
    assert eng.calls == [0, 1, 2]


def test_cost_accumulates_across_escalations(store):
    eng = FakeEngine(succeed_at_rung=2)
    out = router_with(eng, store=store).run_job(prompt="p", kind="classify")
    assert out["cost_usd"] > 0
    assert out["cost_usd"] == pytest.approx(sum(a["cost_usd"] for a in out["trail"]))


def test_trail_records_every_attempt(store):
    eng = FakeEngine(succeed_at_rung=2)
    out = router_with(eng, store=store).run_job(prompt="p", kind="classify")
    assert [a["rung"] for a in out["trail"]] == [0, 1, 2]
    assert [a["ok"] for a in out["trail"]] == [False, False, True]


# --------------------------------------------------------------------------
# Store
# --------------------------------------------------------------------------

def test_store_persists_job_and_attempts(store):
    eng = FakeEngine(succeed_at_rung=1)
    out = router_with(eng, store=store).run_job(
        prompt="hello", kind="classify", title="t")
    job = store.get_job(out["job_id"])
    assert job["status"] == "done"
    assert job["start_rung"] == 0 and job["final_rung"] == 1
    assert len(job["attempt_log"]) == 2


def test_store_records_failure(store):
    eng = FakeEngine(succeed_at_rung=9)
    out = router_with(eng, store=store).run_job(
        prompt="p", kind="classify", max_rung=1)
    job = store.get_job(out["job_id"])
    assert job["status"] == "failed" and job["error"]


def test_stats_reports_savings_against_top_rung(store):
    eng = FakeEngine(succeed_at_rung=0)
    r = router_with(eng, store=store)
    for _ in range(3):
        r.run_job(prompt="p", kind="classify")
    s = store.stats()
    assert s["totals"]["jobs"] == 3
    assert s["fable_equivalent_cost"] > 0
    assert s["savings_vs_fable"] >= 0


def test_get_missing_job_returns_none(store):
    assert store.get_job("nope") is None


# --------------------------------------------------------------------------
# Swarm
# --------------------------------------------------------------------------

def test_swarm_runs_every_task(store):
    eng = FakeEngine(succeed_at_rung=0)
    sw = Swarm(router_with(eng, store=store))
    res = sw.run([Task(prompt=f"t{i}", kind="classify") for i in range(12)], "s1")
    assert res["total"] == 12 and res["succeeded"] == 12
    assert len(eng.calls) == 12


def test_swarm_isolates_a_failing_task(store):
    class Exploder(FakeEngine):
        def run(self, tier, system, prompt, max_tokens=8000):
            if prompt == "boom":
                raise RuntimeError("engine exploded")
            return super().run(tier, system, prompt, max_tokens)

    sw = Swarm(router_with(Exploder(), store=store))
    res = sw.run([Task(prompt=p, kind="classify") for p in ("a", "boom", "c")], "s2")
    assert res["total"] == 3 and res["succeeded"] == 2 and res["failed"] == 1


def test_swarm_groups_jobs_under_its_id(store):
    sw = Swarm(router_with(FakeEngine(), store=store))
    sw.run([Task(prompt=f"t{i}", kind="classify") for i in range(4)], "swarm-xyz")
    assert len(store.list_jobs(swarm_id="swarm-xyz")) == 4


def test_tier_gate_limits_are_per_rung():
    gate = TierGate(overrides={0: 1})
    gate.acquire(0)
    assert not gate._sems[0].acquire(blocking=False), "rung 0 should be saturated"
    assert gate._sems[1].acquire(blocking=False), "rung 1 must be unaffected"
    gate._sems[1].release()
    gate.release(0)


def test_task_start_rung_follows_the_same_rules_as_resolve():
    assert Task(prompt="p", kind="classify").start_rung() == 0
    assert Task(prompt="p", kind="classify", rung=3).start_rung() == 3
    assert Task(prompt="p", tier_name="fable").start_rung() == tiers.MAX_RUNG


# --------------------------------------------------------------------------
# Per-job model override
# --------------------------------------------------------------------------

class RecordingEngine(FakeEngine):
    """Remembers the model string it was handed at each rung."""

    def __init__(self, succeed_at_rung=0):
        super().__init__(succeed_at_rung=succeed_at_rung)
        self.models: list[str] = []

    def run(self, tier, system, prompt, max_tokens=8000):
        self.models.append(tier.model)
        return super().run(tier, system, prompt, max_tokens)


def test_model_override_replaces_model_at_start_rung(store):
    eng = RecordingEngine()
    out = router_with(eng, store=store).run_job(
        prompt="p", kind="classify", model="qwen2.5-coder:3b")
    assert out["ok"] and eng.models == ["qwen2.5-coder:3b"]


def test_model_override_keeps_the_rung_and_its_pricing(store):
    """Overriding the model must not smuggle in a different tier's economics."""
    eng = RecordingEngine()
    out = router_with(eng, store=store).run_job(
        prompt="p", kind="classify", model="some-other-model")
    assert out["rung"] == 0 and out["tier"] == "local"
    assert out["cost_usd"] == 0.0, "rung 0 must stay free whatever model runs"


def test_escalation_above_start_uses_standard_models(store):
    """The override describes this task, not the ladder."""
    eng = RecordingEngine(succeed_at_rung=2)
    router_with(eng, store=store).run_job(
        prompt="p", kind="classify", model="qwen2.5-coder:3b")
    assert eng.models[0] == "qwen2.5-coder:3b"
    assert eng.models[1] == tiers.by_rung(1).model
    assert eng.models[2] == tiers.by_rung(2).model


def test_no_override_uses_the_tier_default(store):
    eng = RecordingEngine()
    router_with(eng, store=store).run_job(prompt="p", kind="classify")
    assert eng.models == [tiers.by_rung(0).model]


def test_swarm_task_carries_model_override(store):
    eng = RecordingEngine()
    sw = Swarm(router_with(eng, store=store))
    sw.run([Task(prompt="p", kind="classify", model="qwen2.5-coder:3b")], "s-model")
    assert eng.models == ["qwen2.5-coder:3b"]


# --------------------------------------------------------------------------
# Cross-tier fairness
#
# Regression test for head-of-line blocking. With one shared thread pool,
# threads blocked on a saturated tier's semaphore still occupy pool slots, so
# tasks for other tiers queue behind them. Found by running ladder_review at
# rung 0 over pool.py, then reproduced: 60 local tasks ahead of 3 Haiku tasks
# delayed the first Haiku completion to position 29 of 63.
# --------------------------------------------------------------------------

def test_a_saturated_narrow_tier_does_not_starve_a_wide_one():
    import threading
    import time

    order: list[str] = []
    lock = threading.Lock()

    class SlowRouter:
        def run_job(self, **kw):
            time.sleep(0.05)
            with lock:
                order.append(kw["title"])
            return {"ok": True, "result": "x", "cost_usd": 0.0,
                    "trail": [], "escalations": 0}

    # Far more rung-0 tasks than any single pool would have threads for,
    # submitted ahead of a few rung-1 tasks.
    tasks = [Task(prompt="p", kind="classify", title=f"local{i}") for i in range(60)]
    tasks += [Task(prompt="p", kind="doc", title=f"haiku{i}") for i in range(3)]

    Swarm(SlowRouter(), gate=TierGate()).run(tasks, "fairness")

    first_haiku = min(i for i, t in enumerate(order) if t.startswith("haiku"))
    assert first_haiku <= 8, (
        f"rung-1 work starved behind rung-0: first haiku completed at "
        f"position {first_haiku} of {len(order)}"
    )


def test_every_task_still_runs_when_partitioned_by_rung():
    """Partitioning must not drop or duplicate work."""
    eng = FakeEngine(succeed_at_rung=0)
    router = router_with(eng)
    tasks = (
        [Task(prompt=f"c{i}", kind="classify") for i in range(10)]
        + [Task(prompt=f"d{i}", kind="doc") for i in range(5)]
        + [Task(prompt=f"i{i}", kind="implement") for i in range(3)]
    )
    res = Swarm(router).run(tasks, "mixed")
    assert res["total"] == 18 and res["succeeded"] == 18
    assert len(eng.calls) == 18


# --------------------------------------------------------------------------
# Local timeouts
#
# A fixed timeout made the free tier bill you: default max_tokens=8000 at the
# 3-5 tok/s of CPU inference needs 1400-2500s, but the deadline was 900s. Every
# rung-0 job that filled its budget timed out, the router read that as failure,
# and the job escalated to a paid tier.
# --------------------------------------------------------------------------

def test_local_deadline_scales_with_requested_tokens():
    from ladder.engines.ollama_engine import MIN_TOKENS_PER_SEC, OllamaEngine

    eng = OllamaEngine()
    small, large = eng._timeout_for(300), eng._timeout_for(8000)
    assert large > small, "a bigger generation must get a longer deadline"
    # The deadline must actually cover the work at the pessimistic floor rate.
    assert large >= 8000 / MIN_TOKENS_PER_SEC


def test_default_max_tokens_is_survivable_at_rung_zero():
    """Regression: the shipped defaults must not guarantee a timeout."""
    from ladder.engines.ollama_engine import OllamaEngine

    default_max_tokens = 8000
    slowest_measured_tps = 3.2
    deadline = OllamaEngine()._timeout_for(default_max_tokens)
    needed = default_max_tokens / slowest_measured_tps
    assert deadline >= needed, (
        f"default max_tokens={default_max_tokens} needs ~{needed:.0f}s at "
        f"{slowest_measured_tps} tok/s but the deadline is {deadline}s -- "
        "the free tier would time out and escalate to a paid rung"
    )


def test_local_deadline_is_bounded():
    from ladder.engines.ollama_engine import MAX_TIMEOUT_SEC, OllamaEngine

    assert OllamaEngine()._timeout_for(10_000_000) == MAX_TIMEOUT_SEC


def test_explicit_timeout_still_wins():
    from ladder.engines.ollama_engine import OllamaEngine

    assert OllamaEngine(timeout=42)._timeout_for(8000) == 42


# --------------------------------------------------------------------------
# Stored system prompt
# --------------------------------------------------------------------------

def test_web_and_mcp_paths_store_the_same_system_prompt(store):
    """The dashboard showed only system_extra for web-created jobs."""
    from ladder import prompts
    from ladder.server import create_app

    app = create_app(db_path=str(Path(store.path).parent / "web.db"))
    app.config.update(TESTING=True)
    client = app.test_client()

    resp = client.post("/api/job", json={"prompt": "x", "kind": "review",
                                         "max_rung": 0, "system_extra": "EXTRA"})
    assert resp.status_code == 202
    job_id = resp.get_json()["job_id"]

    job = app.ladder_store.get_job(job_id)
    app.ladder_store.close()
    expected = prompts.system_for("review", "EXTRA")
    assert job["system"] == expected, "web path must store the full system prompt"
    assert "EXTRA" in job["system"]
    assert job["system"].startswith(prompts.BASE[:40])


# --------------------------------------------------------------------------
# Adjudication
#
# Structural verifiers check shape, not correctness. Observed live: a 3B
# returned well-formed JSON asserting "charlie" has 6 characters (it has 7).
# The json verifier passed it and the job was recorded as a rung-0 success.
# Adjudication asks the next rung up whether the answer is actually right.
# --------------------------------------------------------------------------

def test_adjudication_verdict_parsing():
    from ladder.router import adjudication_verdict

    assert adjudication_verdict("PASS - looks right")[0]
    assert adjudication_verdict("  pass, fine")[0]
    assert adjudication_verdict("**PASS** ok")[0]
    assert not adjudication_verdict("FAIL - charlie has 7 letters")[0]
    assert not adjudication_verdict("I think maybe?")[0], "ambiguity must not pass"
    assert not adjudication_verdict("")[0], "empty must not pass"


class Adjudicating(FakeEngine):
    """Answers tasks, and plays adjudicator when handed the checker prompt."""

    def __init__(self, verdict: str):
        super().__init__(succeed_at_rung=0)
        self.verdict = verdict
        self.adjudications = 0

    def run(self, tier, system, prompt, max_tokens=8000):
        if "PROPOSED ANSWER:" in prompt:
            self.adjudications += 1
            return Result(text=self.verdict, ok=True, engine="fake",
                          model=tier.model, rung=tier.rung, cost_usd=0.001)
        return super().run(tier, system, prompt, max_tokens)


def test_adjudicator_rejection_forces_escalation(store):
    eng = Adjudicating("FAIL - the count is wrong")
    out = router_with(eng, store=store).run_job(
        prompt="p", kind="classify", adjudicate=True, max_rung=2)
    # Rejected at rung 0 and 1, so it climbs; rung 2 is the ceiling and is
    # accepted without adjudication since there is no rung above it to ask.
    assert out["ok"] and out["rung"] == 2
    assert out["escalations"] == 2


def test_adjudicator_approval_keeps_the_cheap_answer(store):
    eng = Adjudicating("PASS - correct")
    out = router_with(eng, store=store).run_job(
        prompt="p", kind="classify", adjudicate=True)
    assert out["ok"] and out["rung"] == 0, "an approved cheap answer must stand"
    assert eng.adjudications == 1


def test_adjudication_is_off_by_default(store):
    eng = Adjudicating("FAIL - would reject everything")
    out = router_with(eng, store=store).run_job(prompt="p", kind="classify")
    assert out["ok"] and out["rung"] == 0
    assert eng.adjudications == 0, "must not spend money unless asked"


def test_no_adjudication_at_the_ceiling(store):
    """There is no rung above the ceiling to ask, so do not pay for a check."""
    eng = Adjudicating("FAIL")
    out = router_with(eng, store=store).run_job(
        prompt="p", kind="classify", rung=0, max_rung=0, adjudicate=True)
    assert out["ok"] and eng.adjudications == 0


def test_broken_adjudicator_does_not_escalate_everything(store):
    """If the checker itself fails, that is an infra problem, not a spending one."""

    class BrokenChecker(FakeEngine):
        def run(self, tier, system, prompt, max_tokens=8000):
            if "PROPOSED ANSWER:" in prompt:
                return Result(text="", ok=False, engine="fake", model=tier.model,
                              rung=tier.rung, error="adjudicator exploded")
            return super().run(tier, system, prompt, max_tokens)

    out = router_with(BrokenChecker(), store=store).run_job(
        prompt="p", kind="classify", adjudicate=True)
    assert out["ok"] and out["rung"] == 0


def test_adjudication_cost_is_counted(store):
    eng = Adjudicating("PASS")
    out = router_with(eng, store=store).run_job(
        prompt="p", kind="classify", adjudicate=True)
    assert out["cost_usd"] >= 0.001, "the check call must show up in the bill"


# --------------------------------------------------------------------------
# Worth-it reporting
#
# The point of this report is that it can say no. A savings dashboard that
# only ever reports savings is marketing.
# --------------------------------------------------------------------------

BASE_REPORT = {
    "window_days": None, "jobs": 100, "jobs_done": 100,
    "requests_deflected": 80, "requests_spent": 20, "deflection_rate": 0.80,
    "tokens_deflected": 2_900_000, "tokens_spent": 700_000, "cli_calls": 20,
    "quota_multiplier": 5.0, "local_hours": 0.5, "seconds_per_deflection": 22.5,
    "wasted_local_attempts": 0, "wasted_local_hours": 0.0,
    "notional_spend_usd": 1.0, "by_kind": [], "by_user": [],
    "paid_attempts": 20, "batched_attempts": 0, "batch_savings_tokens": 0,
}


def _report(**over):
    return {**BASE_REPORT, **over}


def test_verdict_can_say_not_worth_it():
    """Deflecting nothing means you waited AND spent the allowance."""
    from ladder import verdict

    a = verdict.assess(_report(deflection_rate=0.05, requests_deflected=5))
    assert a["verdict"] == "not-worth-it"
    assert a["actions"], "a negative verdict must come with something to do"


def test_verdict_flags_slow_deflection_as_marginal():
    from ladder import verdict

    a = verdict.assess(_report(seconds_per_deflection=1800))
    assert a["verdict"] == "marginal"
    assert any("3b" in x for x in a["actions"])


def test_verdict_says_worth_it_when_deflection_is_cheap_and_common():
    from ladder import verdict

    assert verdict.assess(_report())["verdict"] == "worth-it"


def test_verdict_refuses_to_judge_on_thin_data():
    from ladder import verdict

    assert verdict.assess(_report(jobs_done=3))["verdict"] == "insufficient-data"


def test_verdict_reports_allowance_not_dollars():
    """On a subscription, dollars are notional; tokens are the real cost."""
    from ladder import verdict

    rep = _report()
    text = verdict.render(rep, verdict.assess(rep))
    assert "tokens deflected" in text
    assert "quota multiplier" in text
    assert "not a bill" in text


def test_verdict_calls_out_wasted_local_effort():
    from ladder import verdict

    a = verdict.assess(_report(wasted_local_attempts=9, wasted_local_hours=2.0))
    assert any("bought nothing" in f for f in a["findings"])


def test_verdict_recommends_batching_when_cli_calls_pile_up():
    """The ~35k overhead is per invocation, so fewer larger calls win."""
    from ladder import verdict

    a = verdict.assess(_report(paid_attempts=40, batched_attempts=0))
    assert any("batch=true" in x for x in a["actions"])


def test_verdict_credits_batching_that_already_happened():
    from ladder import verdict

    a = verdict.assess(_report(paid_attempts=20, batched_attempts=20,
                               cli_calls=2, batch_savings_tokens=630_000))
    assert any("Batching already saved" in f for f in a["findings"])
    assert not any("batch=true" in x for x in a["actions"]),         "must not nag about batching that is already happening"


def test_render_never_crashes_on_any_verdict():
    from ladder import verdict

    for over in ({}, {"deflection_rate": 0.0, "requests_deflected": 0},
                 {"jobs_done": 1}, {"seconds_per_deflection": None},
                 {"quota_multiplier": None}, {"wasted_local_attempts": 3,
                                              "wasted_local_hours": 1.0}):
        rep = _report(**over)
        assert verdict.render(rep, verdict.assess(rep))


# --------------------------------------------------------------------------
# Store: orphan reaping and per-user attribution
# --------------------------------------------------------------------------

def test_orphaned_running_jobs_are_reaped(store):
    """Every Claude Code restart kills the MCP server mid-flight."""
    import time as _t

    job_id = store.create_job(kind="classify", title="t", prompt="p", system="s",
                              start_rung=0, max_rung=0)
    store.mark_running(job_id)
    store._write("UPDATE jobs SET started_at=? WHERE id=?",
                 (_t.time() - 7 * 3600, job_id))

    assert store.reap_stale() == 1
    job = store.get_job(job_id)
    assert job["status"] == "failed" and "orphaned" in job["error"]


def test_live_jobs_are_not_reaped(store):
    job_id = store.create_job(kind="classify", title="t", prompt="p", system="s",
                              start_rung=0, max_rung=0)
    store.mark_running(job_id)
    assert store.reap_stale() == 0
    assert store.get_job(job_id)["status"] == "running"


def test_jobs_record_a_user(store):
    from ladder.store import current_user

    job_id = store.create_job(kind="classify", title="t", prompt="p", system="s",
                              start_rung=0, max_rung=0)
    assert store.get_job(job_id)["user"] == current_user()


def test_report_counts_deflected_requests_and_tokens(store):
    """Every locally-completed request avoids a ~35k-token harness hit."""
    eng = FakeEngine(succeed_at_rung=0)

    class LocalEngine(FakeEngine):
        def run(self, tier, system, prompt, max_tokens=8000):
            res = super().run(tier, system, prompt, max_tokens)
            res.engine = "ollama"
            return res

    r = router_with(LocalEngine(), store=store)
    for _ in range(3):
        r.run_job(prompt="p", kind="classify")

    rep = store.report()
    assert rep["requests_deflected"] == 3
    assert rep["deflection_rate"] == 1.0
    assert rep["tokens_deflected"] >= 3 * tiers.CLI_OVERHEAD_TOKENS
    assert rep["cli_calls"] == 0
    assert eng is not None


# --------------------------------------------------------------------------
# Batching
#
# The ~35k harness overhead is charged per `claude -p` invocation, not per
# task. On a subscription that overhead is allowance, so packing many tasks
# into one call is the single biggest lever available. Measured live: 6
# classifications in 1 invocation, 175k tokens of allowance saved.
# --------------------------------------------------------------------------

def test_batch_prompt_numbers_every_task():
    from ladder.engines.cli_engine import build_batch_prompt

    text = build_batch_prompt(["alpha", "bravo", "charlie"])
    assert "3 tasks" in text
    for n in (1, 2, 3):
        assert f"=== TASK {n} ===" in text


def test_batch_reply_parsing_is_strict_about_alignment():
    from ladder.engines.cli_engine import parse_batch_reply

    assert parse_batch_reply('["a","b","c"]', 3) == ["a", "b", "c"]
    assert parse_batch_reply('```json\n["a","b"]\n```', 2) == ["a", "b"]
    # A short or long array would silently shift answers onto the wrong tasks.
    assert parse_batch_reply('["a","b"]', 3) is None
    assert parse_batch_reply('["a","b","c","d"]', 3) is None
    assert parse_batch_reply("not json at all", 2) is None
    assert parse_batch_reply('{"a": 1}', 2) is None


def test_batchable_rejects_singletons_and_oversized_sets():
    from ladder.engines.cli_engine import MAX_BATCH, ClaudeCliEngine

    eng = ClaudeCliEngine()
    assert not eng.batchable(["only one"])
    assert eng.batchable(["a", "b"])
    assert not eng.batchable(["x"] * (MAX_BATCH + 1))
    assert not eng.batchable(["x" * 40_000, "y" * 40_000])


class BatchEngine(FakeEngine):
    """Records how many invocations happened, batched or not."""

    def __init__(self):
        super().__init__(succeed_at_rung=0)
        self.invocations = 0

    def batchable(self, prompts):
        return len(prompts) > 1

    def run(self, tier, system, prompt, max_tokens=8000):
        self.invocations += 1
        return super().run(tier, system, prompt, max_tokens)

    def run_batch(self, tier, system, prompts, max_tokens=8000):
        self.invocations += 1
        return [
            Result(text=f"answer {i}", ok=True, engine="cli", model=tier.model,
                   rung=tier.rung, tokens_in=10, tokens_out=10,
                   raw={"batched": len(prompts)})
            for i, _ in enumerate(prompts)
        ]


def test_batching_collapses_many_tasks_into_one_invocation(store):
    eng = BatchEngine()
    sw = Swarm(router_with(eng, store=store))
    tasks = [Task(prompt=f"t{i}", kind="doc", rung=1, max_rung=1) for i in range(8)]

    res = sw.run(tasks, "s-batch", batch=True)
    assert res["succeeded"] == 8
    assert eng.invocations == 1, "8 tasks must cost one invocation, not eight"
    assert res["batched"] == 8


def test_without_batching_each_task_is_its_own_invocation(store):
    eng = BatchEngine()
    sw = Swarm(router_with(eng, store=store))
    tasks = [Task(prompt=f"t{i}", kind="doc", rung=1, max_rung=1) for i in range(8)]

    sw.run(tasks, "s-nobatch", batch=False)
    assert eng.invocations == 8


def test_local_tier_is_never_batched(store):
    """Rung 0 has no per-call overhead to amortise, and one shared budget
    would only make a slow tier slower."""
    eng = BatchEngine()
    sw = Swarm(router_with(eng, store=store))
    tasks = [Task(prompt=f"t{i}", kind="classify") for i in range(6)]

    sw.run(tasks, "s-local", batch=True)
    assert eng.invocations == 6


def test_tasks_with_escalation_headroom_are_not_batched(store):
    """A batch answers at exactly one rung, so it cannot climb."""
    eng = BatchEngine()
    sw = Swarm(router_with(eng, store=store))
    tasks = [Task(prompt=f"t{i}", kind="doc", rung=1, max_rung=4) for i in range(5)]

    sw.run(tasks, "s-esc", batch=True)
    assert eng.invocations == 5


def test_adjudicated_tasks_are_not_batched(store):
    eng = BatchEngine()
    sw = Swarm(router_with(eng, store=store))
    tasks = [Task(prompt=f"t{i}", kind="doc", rung=1, max_rung=1, adjudicate=True)
             for i in range(4)]

    sw.run(tasks, "s-adj", batch=True)
    assert eng.invocations == 4


def test_incompatible_tasks_split_into_separate_batches(store):
    eng = BatchEngine()
    sw = Swarm(router_with(eng, store=store))
    tasks = ([Task(prompt=f"a{i}", kind="doc", rung=1, max_rung=1) for i in range(3)]
             + [Task(prompt=f"b{i}", kind="doc", rung=1, max_rung=1,
                     max_tokens=99) for i in range(3)])

    sw.run(tasks, "s-split", batch=True)
    assert eng.invocations == 2, "differing max_tokens cannot share one budget"


def test_unparseable_batch_falls_back_to_individual_calls(store):
    """Never guess at alignment: a bad batch reply must not shift answers."""

    class BadBatch(BatchEngine):
        def run_batch(self, tier, system, prompts, max_tokens=8000):
            self.invocations += 1
            return None

    eng = BadBatch()
    sw = Swarm(router_with(eng, store=store))
    tasks = [Task(prompt=f"t{i}", kind="doc", rung=1, max_rung=1) for i in range(4)]

    res = sw.run(tasks, "s-fallback", batch=True)
    assert res["succeeded"] == 4, "fallback must still answer every task"
    assert eng.invocations == 5, "1 failed batch + 4 individual retries"


def test_report_counts_invocations_not_batched_attempts(store):
    """Counting attempt rows would overstate the very thing batching reduces."""
    eng = BatchEngine()
    Swarm(router_with(eng, store=store)).run(
        [Task(prompt=f"t{i}", kind="doc", rung=1, max_rung=1) for i in range(10)],
        "s-count", batch=True)

    rep = store.report()
    assert rep["paid_attempts"] == 10
    assert rep["cli_calls"] == 1
    assert rep["batch_savings_tokens"] == 9 * tiers.CLI_OVERHEAD_TOKENS


# --------------------------------------------------------------------------
# MCP argument hygiene
#
# Found by using the tool: a swarm-level `rung` was silently dropped because
# the schema never declared it and the task builder never read it. The call
# reported success while every task ran on a different tier than asked for.
# Silent parameter drops are worse than errors -- the report looks healthy.
# --------------------------------------------------------------------------

def _mcp():
    import sys as _s
    _s.path.insert(0, str(Path(__file__).resolve().parent.parent / "mcp"))
    import ladder_mcp

    return ladder_mcp


def _tool(name):
    return next(t for t in _mcp().TOOLS if t["name"] == name)


def test_every_arg_a_handler_reads_is_declared_in_its_schema():
    """Schema and implementation must not drift: undeclared args get dropped
    before the hardening, and falsely rejected after it."""
    import inspect
    import re

    m = _mcp()
    for name, fn in m.HANDLERS.items():
        src = inspect.getsource(fn)
        declared = set(_tool(name)["inputSchema"].get("properties", {}))
        read = set(re.findall(r'args\.get\(\s*"([a-z_]+)"', src))
        missing = read - declared
        assert not missing, f"{name} reads undeclared args: {sorted(missing)}"


def test_every_per_task_key_the_swarm_reads_is_declared():
    import inspect
    import re

    src = inspect.getsource(_mcp().t_swarm)
    items = _tool("ladder_swarm")["inputSchema"]["properties"]["tasks"]["items"]
    declared = set(items["properties"])
    read = set(re.findall(r't\.get\(\s*"([a-z_]+)"', src)) | {"prompt"}
    missing = read - declared
    assert not missing, f"tasks[] reads undeclared keys: {sorted(missing)}"


def test_swarm_level_defaults_reach_every_task():
    """A swarm-level rung must apply to tasks that do not set their own."""
    import inspect
    import re

    src = inspect.getsource(_mcp().t_swarm)
    for field in ("rung", "max_rung", "kind", "verify", "max_tokens", "model"):
        assert re.search(rf't\.get\("{field}",\s*args\.get\("{field}"', src), (
            f"swarm-level {field!r} does not fall through to tasks; it would be "
            "silently ignored"
        )


def test_unknown_top_level_argument_is_rejected():
    m = _mcp()
    err = m.validate_args(_tool("ladder_run"), {"prompt": "x", "effort": "high"})
    assert err and "effort" in err
    assert "Accepted:" in err, "the error must say what IS accepted"


def test_unknown_per_task_key_is_rejected():
    m = _mcp()
    err = m.validate_args(_tool("ladder_swarm"),
                          {"tasks": [{"prompt": "x", "rungg": 0}]})
    assert err and "rungg" in err and "tasks[0]" in err


def test_valid_arguments_pass_validation():
    m = _mcp()
    assert m.validate_args(_tool("ladder_run"),
                           {"prompt": "x", "rung": 0, "adjudicate": True}) is None
    assert m.validate_args(_tool("ladder_swarm"), {
        "tasks": [{"prompt": "x", "max_tokens": 50, "system_extra": "ctx"}],
        "rung": 1, "batch": True,
    }) is None


def test_validation_tolerates_malformed_task_entries():
    """Bad input should produce a clear message, never a crash."""
    m = _mcp()
    assert m.validate_args(_tool("ladder_swarm"), {"tasks": ["not a dict"]}) is None
    assert m.validate_args(_tool("ladder_swarm"), {"tasks": "not a list"}) is None


# --------------------------------------------------------------------------
# Truncation
#
# max_tokens is one value for the whole job, so escalating on truncation could
# never fix it: the next rung up gets the same cap and hits the same wall,
# having cost a rung. Truncation retries at the same rung with more budget.
# --------------------------------------------------------------------------

class Truncating(FakeEngine):
    """Truncates until the budget reaches `needs`, then answers."""

    def __init__(self, needs: int):
        super().__init__(succeed_at_rung=0)
        self.needs = needs
        self.budgets: list[int] = []
        self.rungs: list[int] = []

    def run(self, tier, system, prompt, max_tokens=8000):
        self.budgets.append(max_tokens)
        self.rungs.append(tier.rung)
        if max_tokens < self.needs:
            return Result(text="half an ans", ok=True, engine="fake",
                          model=tier.model, rung=tier.rung, stop_reason="length")
        return Result(text="complete answer", ok=True, engine="fake",
                      model=tier.model, rung=tier.rung, stop_reason="end_turn")


def test_truncation_retries_the_same_rung_with_more_budget(store):
    eng = Truncating(needs=400)
    out = router_with(eng, store=store).run_job(
        prompt="p", kind="classify", max_tokens=100)
    assert out["ok"] and out["result"] == "complete answer"
    assert eng.budgets == [100, 200, 400], "budget must double until sufficient"
    assert eng.rungs == [0, 0, 0], "truncation must NOT climb the ladder"
    assert out["rung"] == 0


def test_truncation_does_not_count_as_an_escalation(store):
    """Same-rung retries would otherwise inflate the number people tune on."""
    eng = Truncating(needs=400)
    out = router_with(eng, store=store).run_job(
        prompt="p", kind="classify", max_tokens=100)
    assert out["escalations"] == 0
    assert len(out["trail"]) == 3, "the retries are still recorded"


def test_truncation_at_the_ceiling_is_a_failure_not_a_short_answer(store):
    """A half-finished answer reporting success is the silent-wrongness trap."""
    from ladder.router import MAX_TOKENS_CEILING

    eng = Truncating(needs=MAX_TOKENS_CEILING * 10)
    out = router_with(eng, store=store).run_job(
        prompt="p", kind="classify", rung=0, max_rung=0, max_tokens=1000)
    assert not out["ok"]
    assert "truncated" in out["error"] and "incomplete" in out["error"]


def test_budget_growth_is_bounded(store):
    from ladder.router import MAX_TOKENS_CEILING

    eng = Truncating(needs=MAX_TOKENS_CEILING * 10)
    router_with(eng, store=store).run_job(
        prompt="p", kind="classify", rung=0, max_rung=0, max_tokens=1000)
    assert max(eng.budgets) <= MAX_TOKENS_CEILING


def test_a_genuine_failure_still_escalates(store):
    """Only truncation stays put; real failures must still climb."""
    eng = FakeEngine(succeed_at_rung=2)
    out = router_with(eng, store=store).run_job(prompt="p", kind="classify")
    assert out["ok"] and out["rung"] == 2 and out["escalations"] == 2


def test_was_truncated_recognises_every_engine_wording():
    from ladder.engines.base import Result as R
    from ladder.router import was_truncated

    assert was_truncated(R(text="", stop_reason="length"))      # ollama
    assert was_truncated(R(text="", stop_reason="max_tokens"))  # anthropic / cli
    assert not was_truncated(R(text="", stop_reason="end_turn"))
    assert not was_truncated(R(text="", stop_reason=""))


# --------------------------------------------------------------------------
# Port collision
#
# Found live: two dashboards were listening on 5151, one 405 minutes old. On
# Windows Werkzeug's SO_REUSEADDR means a second bind succeeds and steals the
# port rather than failing, so the older process kept answering -- 404ing routes
# added since, from a database that had been deleted.
# --------------------------------------------------------------------------

def test_port_probe_detects_a_listener():
    import socket

    from ladder.server import port_is_taken

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as srv:
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        host, port = srv.getsockname()
        assert port_is_taken(host, port)

    # Once closed, the same port must read as free.
    assert not port_is_taken(host, port)


def test_port_probe_is_false_for_a_free_port():
    import socket

    from ladder.server import port_is_taken

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    assert not port_is_taken("127.0.0.1", port)
