"""Swarm executor: run many jobs at once, with per-tier concurrency caps.

Why per-tier caps rather than one global limit
----------------------------------------------
The rungs have completely different scaling behaviour:

* Rung 0 (local) is **CPU-bound and close to zero-sum**. Token generation on
  this box is limited by memory bandwidth, not cores, so running eight local
  jobs does not make eight jobs go faster -- it makes each one roughly eight
  times slower. Its cap is deliberately tiny.
* Rungs 1-5 are **network-bound**. Fanning out is nearly free in wall-clock
  terms, and the only real limits are API rate limits and your wallet.

So a swarm that mixes rungs must not share one thread pool. A batch of 50 local
jobs and 50 Haiku jobs must not let the local jobs starve the Haiku ones.

Two mechanisms, doing different jobs -- the distinction matters, because
conflating them is how the first version shipped a starvation bug:

* **Per-rung thread pools** (`Swarm.run`) provide fairness *within* one swarm.
* **Per-rung semaphores** (`TierGate`) bound concurrency *across* simultaneous
  swarms, which a per-swarm pool cannot observe.

Semaphores alone are not sufficient for fairness: a thread blocked on one still
occupies a pool slot. See `Swarm.run` for the measurement.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any

from . import tiers


@dataclass
class Task:
    """One unit of swarm work. Mirrors Router.run_job's parameters."""

    prompt: str
    kind: str = "implement"
    title: str = ""
    rung: int | None = None
    tier_name: str | None = None
    max_rung: int | None = None
    system_extra: str = ""
    verify: Any = None
    max_tokens: int = 8000
    model: str | None = None
    adjudicate: bool = False

    def start_rung(self) -> int:
        return tiers.resolve(kind=self.kind, rung=self.rung,
                             tier_name=self.tier_name).rung


class TierGate:
    """One semaphore per rung, sized from each tier's concurrency budget."""

    def __init__(self, overrides: dict[int, int] | None = None):
        overrides = overrides or {}
        self._sems = {
            t.rung: threading.Semaphore(max(1, overrides.get(t.rung, t.concurrency)))
            for t in tiers.LADDER
        }

    def acquire(self, rung: int) -> None:
        self._sems[max(0, min(rung, tiers.MAX_RUNG))].acquire()

    def release(self, rung: int) -> None:
        self._sems[max(0, min(rung, tiers.MAX_RUNG))].release()


class Swarm:
    """Runs a batch of tasks through a Router, respecting per-tier limits."""

    def __init__(self, router, gate: TierGate | None = None):
        self.router = router
        self.gate = gate or TierGate()

    def _run_one(self, task: Task, swarm_id: str) -> dict:
        rung = task.start_rung()
        self.gate.acquire(rung)
        try:
            return self.router.run_job(
                prompt=task.prompt, kind=task.kind, rung=task.rung,
                tier_name=task.tier_name, max_rung=task.max_rung,
                system_extra=task.system_extra, verify=task.verify,
                max_tokens=task.max_tokens, title=task.title,
                swarm_id=swarm_id, model=task.model,
                adjudicate=task.adjudicate,
            )
        except Exception as exc:  # noqa: BLE001 - one task must not kill the swarm
            return {"ok": False, "result": "", "error": f"{type(exc).__name__}: {exc}",
                    "cost_usd": 0.0, "title": task.title, "trail": []}
        finally:
            self.gate.release(rung)

    def _drain_group(self, rung: int, group: list[Task], swarm_id: str,
                     results: list[dict], lock: threading.Lock) -> None:
        """Run one rung's tasks in a pool sized to that rung's budget."""
        width = min(len(group), max(1, tiers.by_rung(rung).concurrency))
        with ThreadPoolExecutor(max_workers=width) as pool:
            futures = {pool.submit(self._run_one, t, swarm_id): t for t in group}
            for fut in as_completed(futures):
                task = futures[fut]
                out = fut.result()
                out.setdefault("title", task.title or task.prompt[:60])
                with lock:
                    results.append(out)

    def run(self, tasks: list[Task], swarm_id: str) -> dict:
        """Execute every task; return per-task results plus a roll-up.

        Tasks are partitioned by starting rung and each rung gets its own
        thread pool, sized to that rung's concurrency budget. The rung pools
        then run concurrently with each other.

        This partitioning is load-bearing, not tidiness. A single shared pool
        deadlocks nothing but starves badly: threads that block acquiring a
        saturated tier's semaphore keep occupying pool slots, so tasks for
        *other* tiers queue behind them and never get a thread. Measured with
        one shared pool, 60 local tasks submitted ahead of 3 Haiku tasks
        pushed the first Haiku completion to position 29 of 63, despite Haiku
        having six times the concurrency budget. Per-rung pools mean a slow,
        narrow tier can never hold a wide, fast one hostage.
        """
        groups: dict[int, list[Task]] = {}
        for t in tasks:
            groups.setdefault(t.start_rung(), []).append(t)

        results: list[dict] = []
        lock = threading.Lock()
        threads = [
            threading.Thread(
                target=self._drain_group,
                args=(rung, group, swarm_id, results, lock),
                daemon=True,
            )
            for rung, group in groups.items()
        ]
        for th in threads:
            th.start()
        for th in threads:
            th.join()

        ok = [r for r in results if r.get("ok")]
        cost = sum(r.get("cost_usd", 0.0) for r in results)
        return {
            "swarm_id": swarm_id,
            "total": len(results),
            "succeeded": len(ok),
            "failed": len(results) - len(ok),
            "cost_usd": cost,
            "escalations": sum(r.get("escalations", 0) for r in results),
            "results": results,
        }
