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
