"""Speculative execution: draft free, verify once, pay only for the misses.

The idea is lifted from speculative decoding in LLM inference, and it
transplants unusually well.

How the original works
----------------------
A small *draft* model generates K tokens autoregressively. The large *target*
model then verifies all K in a **single forward pass**, because attention
parallelises over positions -- scoring K tokens costs one pass where generating
them costs K. The longest valid prefix is accepted, the rest discarded. You
have replaced K expensive passes with one.

The load-bearing property is not that the draft model is small. It is that
**verification batches and generation does not.**

Why that maps onto this ladder
------------------------------
The same asymmetry exists here for an entirely different reason. A `claude -p`
invocation spends ~35k tokens of harness overhead before it does any work, and
that cost is charged per *invocation*, not per task. So one call that checks
twenty answers costs what one call that checks a single answer costs.

    naive:        K tasks at the target rung  = K x 35k
    speculative:  1 verify + 1 batched rerun  = ~2 x 35k, independent of K

At K=20 that is 700k tokens of allowance against 70k.

Where this is *better* than the original
----------------------------------------
In real speculative decoding, rejecting token i invalidates every token after
it -- they were generated conditioned on a token that turned out wrong. Expected
accepted length is (1 - a^(K+1)) / (1 - a), which **saturates**: even at a=0.9,
raising K past ~20 buys almost nothing.

Independent tasks have no causal chain between them. Task 4 was not conditioned
on task 3, so rejecting task 3 leaves it untouched. Acceptance is per-item and
the expected yield is **K*a -- linear in K, no saturation.** Larger batches keep
paying, which is exactly the regime the 35k fixed cost rewards.

Where this is worse, stated plainly
-----------------------------------
**It is not lossless.** The original is provably distribution-identical to
running the target alone, because rejection sampling has access to the target's
real logits. Here the verifier is a *judge*. "The target model thinks this
answer looks correct" is not "the target model would have produced this answer".
This is speculative decoding's economics with a quality gate standing in for the
sampler; the guarantee is statistical, not exact.

**Verification is not free in K.** A transformer verifies K tokens in one pass
because positions are parallel. Our verify prompt has to physically contain the
K drafts, so its cost grows with total draft length. Hence CHUNK_CHARS.

What the acceptance rate is for
-------------------------------
`a` is the whole ballgame, and it is measurable rather than assumed. Every run
records drafted/accepted per task kind, so `ladder_report` can show which kinds
are worth speculating on. A kind sitting near a=0 is one where the local model
cannot do the work, and speculating on it just adds a verify call to a job that
was always going to be paid for. Stop speculating on those.
"""

from __future__ import annotations

import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from . import prompts as prompts_mod
from . import tiers
from .pool import Task

# How many drafts go into one verification call.
#
# Unlike the token case there is no acceptance-rate reason to cap this -- yield
# is linear in K. The cap exists because the verify prompt must contain every
# draft, and an over-long prompt risks a truncated verdict array, which fails
# the whole chunk. MAX_BATCH in the CLI engine bounds it too.
DEFAULT_CHUNK = 12

# Total characters of (task + draft) allowed in one verification call. Kept
# below the engine's 60k batch ceiling so the verdicts have room to come back.
CHUNK_CHARS = 40_000

# A draft this short is not a real answer. Sending it to the verifier wastes
# room in the prompt for a verdict that is certain to be FAIL.
MIN_DRAFT_CHARS = 2

# The repair pass corrects a rejected draft instead of re-authoring the answer.
# One system prompt covers every task in the batch, whatever its kind, so the
# batch stays homogeneous by construction; per-task instructions ride in the
# task text instead.
REPAIR_SYSTEM = (
    "A cheaper model attempted these tasks and a reviewer rejected its "
    "answers. For each task you are given the task, the rejected draft, and "
    "why it was rejected.\n\n"
    "Correct the draft. Keep whatever it already got right and fix what the "
    "reviewer objected to -- you are editing, not starting over. Each answer "
    "you return must still stand alone and be complete, because the draft is "
    "discarded once you reply. Follow the per-task instructions given under "
    "INSTRUCTIONS."
)

REPAIR_TEMPLATE = (
    "INSTRUCTIONS:\n{system}\n\n"
    "TASK:\n{task}\n\n"
    "REJECTED DRAFT:\n{draft}\n\n"
    "WHY IT WAS REJECTED:\n{reason}\n\n"
    "Return the corrected, complete answer."
)


@dataclass
class Speculation:
    """Accounting for one speculative run. Cost is measured in invocations.

    Dollars are the wrong unit on a prepaid plan: the binding constraint is the
    subscription allowance, and what spends it is the number of `claude -p`
    calls, each carrying its fixed ~35k overhead.
    """

    drafted: int = 0
    accepted: int = 0
    rejected: int = 0
    draft_failed: int = 0
    verify_calls: int = 0
    repair_calls: int = 0
    unverified: int = 0
    draft_seconds: float = 0.0
    verify_seconds: float = 0.0
    # Engine-reported output tokens. Used for the allowance figure only: the
    # two engines do not count the same thing, so these must NOT be compared
    # against each other (the CLI reported 1,486 output tokens for a 1,047
    # character answer -- roughly 5x the visible text -- because its number
    # carries harness overhead that Ollama's eval_count does not).
    local_tokens: int = 0
    paid_tokens: int = 0
    # Characters of *delivered* answer, by author. Same yardstick on both
    # sides, so the ratio means something. Verification output is deliberately
    # excluded: it is overhead, already counted in the allowance figure, and
    # counting it here as well is what made the first version of this metric
    # meaningless.
    local_chars: int = 0
    paid_chars: int = 0
    by_kind: dict[str, list[int]] = field(default_factory=dict)  # kind -> [acc, tot]

    @property
    def paid_calls(self) -> int:
        return self.verify_calls + self.repair_calls

    @property
    def acceptance(self) -> float:
        """a -- the fraction of drafts the target rung was willing to accept."""
        checked = self.accepted + self.rejected
        return (self.accepted / checked) if checked else 0.0

    @property
    def local_share(self) -> float:
        """Fraction of the DELIVERED answer text written by the free model.

        The analogue of the draft model's token share in speculative decoding,
        and a better headline than deflection, which counts whole tasks and so
        flatters a run where rung 0 answered ten trivial questions while the
        paid tier wrote the one long answer.

        Measured in characters because that is the only yardstick both engines
        report identically. Verification output is excluded on purpose: nobody
        receives it, it is overhead rather than authorship, and it already
        appears in the allowance figure.
        """
        total = self.local_chars + self.paid_chars
        return (self.local_chars / total) if total else 0.0

    @property
    def naive_calls(self) -> int:
        """What the same work would have cost with no speculation and no batching."""
        return self.drafted

    @property
    def tokens_saved(self) -> int:
        return max(0, self.naive_calls - self.paid_calls) * tiers.CLI_OVERHEAD_TOKENS

    def note(self, kind: str, accepted: bool) -> None:
        row = self.by_kind.setdefault(kind, [0, 0])
        row[0] += int(accepted)
        row[1] += 1

    def as_dict(self) -> dict:
        return {
            "drafted": self.drafted,
            "accepted": self.accepted,
            "rejected": self.rejected,
            "draft_failed": self.draft_failed,
            "unverified": self.unverified,
            "acceptance": round(self.acceptance, 3),
            "local_share": round(self.local_share, 3),
            "local_chars": self.local_chars,
            "paid_chars": self.paid_chars,
            "local_tokens": self.local_tokens,
            "paid_tokens": self.paid_tokens,
            "verify_calls": self.verify_calls,
            "repair_calls": self.repair_calls,
            "paid_calls": self.paid_calls,
            "naive_calls": self.naive_calls,
            "tokens_saved": self.tokens_saved,
            "draft_seconds": round(self.draft_seconds, 1),
            "verify_seconds": round(self.verify_seconds, 1),
            "by_kind": {k: {"accepted": v[0], "drafted": v[1]}
                        for k, v in self.by_kind.items()},
        }


@dataclass
class Drafted:
    """One task plus whatever the local model produced for it."""

    task: Task
    text: str = ""
    ok: bool = False
    error: str = ""
    latency_ms: int = 0
    tokens_out: int = 0
    # This draft's share of the verification call that judged it. The call is
    # paid for whether the draft is accepted or not, so the cost belongs to
    # the draft either way.
    verify_tokens: int = 0
    target_rung: int = 1
    # Filled in by the verifier so the repair pass can tell the target model
    # what was actually wrong, rather than making it guess.
    reject_reason: str = ""

    @property
    def usable(self) -> bool:
        return self.ok and len(self.text.strip()) >= MIN_DRAFT_CHARS


def target_rung_for(task: Task) -> int:
    """The rung this task would have run at with no speculation.

    That is the tier we are speculating *against*, so it is also the tier that
    verifies -- exactly the target model's role in the original algorithm. A
    task explicitly pinned to rung 0 has no target above it; caller decides.
    """
    return tiers.resolve(kind=task.kind, rung=task.rung,
                         tier_name=task.tier_name).rung


def chunk_drafts(drafts: list[Drafted], size: int = DEFAULT_CHUNK,
                 char_budget: int = CHUNK_CHARS) -> list[list[Drafted]]:
    """Split verifiable drafts into chunks that fit one verification call.

    Two bounds, both real: a count (so the verdict array stays short enough to
    come back intact) and a character budget (so the prompt fits). A single
    oversized draft still gets its own chunk rather than being dropped -- a
    chunk of one falls back to single adjudication upstream.
    """
    chunks: list[list[Drafted]] = []
    current: list[Drafted] = []
    used = 0
    for d in drafts:
        cost = len(d.task.prompt) + len(d.text)
        if current and (len(current) >= size or used + cost > char_budget):
            chunks.append(current)
            current, used = [], 0
        current.append(d)
        used += cost
    if current:
        chunks.append(current)
    return chunks


class Speculator:
    """Draft locally, verify in bulk at the target rung, pay only for misses.

    The three phases mirror the algorithm: draft, verify, repair. Phase 2 is
    the only one that has to happen in a single call for the economics to work,
    and phase 3 is batched for the same reason.
    """

    def __init__(self, router, store=None, gate=None):
        self.router = router
        self.store = store
        self.gate = gate
        self._swarm_id: str | None = None
        self._telemetry_errors: list[str] = []

    # -- phase 1: draft -----------------------------------------------------

    def draft(self, tasks: list[Task], model: str | None = None,
              swarm_id: str | None = None) -> list[Drafted]:
        """Generate every answer on the free tier.

        Concurrency is deliberately the local tier's own budget (2). Local
        generation is memory-bandwidth-bound and close to zero-sum: eight
        parallel local jobs do not finish eight times sooner, they each run
        roughly eight times slower. Fanning out here would buy nothing and
        would evict the model from RAM under contention.
        """
        local = tiers.by_rung(0)
        width = min(len(tasks) or 1, max(1, local.concurrency))
        out: list[Drafted] = [None] * len(tasks)  # type: ignore[list-item]

        def one(i: int, task: Task) -> None:
            res = self.router.run_job(
                prompt=task.prompt,
                kind=task.kind,
                rung=0,
                max_rung=0,           # never climb during drafting: that is the
                system_extra=task.system_extra,   # verifier's job, and climbing
                verify=task.verify,               # here would spend the very
                max_tokens=task.max_tokens,       # allowance we are protecting
                title=task.title or task.prompt[:60],
                swarm_id=swarm_id,
                model=model or task.model,
            )
            trail = res.get("trail", [])
            out[i] = Drafted(
                task=task,
                text=res.get("result", "") or "",
                ok=bool(res.get("ok")),
                error=res.get("error", "") or "",
                latency_ms=sum(a.get("latency_ms", 0) for a in trail),
                tokens_out=sum(a.get("tokens_out", 0) for a in trail),
                target_rung=target_rung_for(task),
            )

        with ThreadPoolExecutor(max_workers=width) as pool:
            futures = [pool.submit(one, i, task) for i, task in enumerate(tasks)]
            for i, fut in enumerate(futures):
                # A submitted future swallows its exception until someone asks
                # for the result. Without this the task would vanish outright:
                # no draft, no rejection, no repair, no entry in the results --
                # silently dropped work, which is worse than a failed job.
                try:
                    fut.result()
                except Exception as exc:  # noqa: BLE001
                    out[i] = Drafted(
                        task=tasks[i], text="", ok=False,
                        error=f"draft raised {type(exc).__name__}: {exc}",
                        target_rung=target_rung_for(tasks[i]),
                    )

        return [d if d is not None else Drafted(
            task=tasks[i], ok=False, error="draft produced no result",
            target_rung=target_rung_for(tasks[i]))
            for i, d in enumerate(out)]

    # -- phase 2: verify ----------------------------------------------------

    def verify(self, drafts: list[Drafted], rung: int,
               spec: Speculation) -> dict[int, tuple[bool, str]]:
        """Judge a chunk of drafts in ONE invocation at the target rung.

        Returns {index in `drafts` -> (passed, reason)}. Indices missing from
        the mapping could not be judged; the caller treats those as unverified
        rather than as passes, because silently accepting an unchecked answer
        is the failure this whole mechanism exists to prevent.
        """
        if not drafts:
            return {}
        pairs = [(d.task.prompt, d.text) for d in drafts]

        usage: dict = {}
        t0 = time.perf_counter()
        if len(pairs) == 1:
            # fail_open=False: an unreachable checker must not silently approve
            # the cheapest model's answer. Re-run it instead.
            passed, reason, _ = self.router._adjudicate(
                rung, *pairs[0], fail_open=False)
            got = [(passed, reason)]
        else:
            got = self.router.adjudicate_batch(rung, pairs, usage=usage)
        spec.verify_seconds += time.perf_counter() - t0
        judged = int(usage.get("tokens_out", 0))
        spec.paid_tokens += judged
        # Split evenly, the same way cli_engine divides a batched invocation's
        # tokens across its tasks, so per-draft figures stay comparable.
        # A chunk of one goes through _adjudicate, which does not report
        # tokens, so single-draft verifications under-count slightly.
        share = judged // max(1, len(drafts))
        for d in drafts:
            d.verify_tokens = share

        if got is None:
            # A batch reply that would not parse is never split across tasks --
            # that is how you hand task 3's verdict to task 4. Report nothing
            # verified and let the caller escalate the chunk wholesale.
            return {}

        spec.verify_calls += 1
        return dict(enumerate(got))

    # -- phase 3: repair ----------------------------------------------------

    def repair(self, drafts: list[Drafted], rung: int,
               spec: Speculation, swarm_id: str | None) -> list[dict]:
        """Fix the rejected tasks at the target rung, in ONE call.

        The target *corrects the draft* rather than re-authoring from scratch.
        That is what the original algorithm does -- after a rejection the target
        emits a correction, not a fresh sequence -- and it matters here for the
        same reason it matters there: the draft already contains most of the
        answer, and paying the dear tier to retype it is pure waste. Handing
        over the draft and the reviewer's objection also raises quality, since
        the model is told what was wrong instead of guessing.

        Everything goes under ONE system prompt with the per-task instructions
        folded into each task's text. Router.run_batch takes its system prompt
        from the *first* task, so a mixed-kind batch there would silently apply
        one kind's instructions to every task -- the misalignment class this
        codebase refuses everywhere else.
        """
        if not drafts:
            return []

        prompts = [
            REPAIR_TEMPLATE.format(
                system=prompts_mod.system_for(d.task.kind, d.task.system_extra),
                task=d.task.prompt,
                draft=(d.text.strip() if d.usable
                       else "(the local model produced nothing usable)"),
                reason=(d.reject_reason or d.error
                        or "the draft was rejected on review"),
            )
            for d in drafts
        ]
        budget = max((d.task.max_tokens for d in drafts), default=8000)
        tier = tiers.by_rung(rung)
        engine = self.router.engine_for(tier)

        results = None
        if len(prompts) > 1 and hasattr(engine, "run_batch") \
                and engine.batchable(prompts):
            results = engine.run_batch(tier, REPAIR_SYSTEM, prompts,
                                       max_tokens=budget)
        if results is not None:
            spec.repair_calls += 1
        else:
            # Batching refused or unparseable: one call each. Slower and
            # dearer, but never misaligned.
            results = [engine.run(tier, REPAIR_SYSTEM, p, max_tokens=budget)
                       for p in prompts]
            spec.repair_calls += len(prompts)

        out: list[dict] = []
        for d, res in zip(drafts, results, strict=True):
            checker = self.router._resolve_verifier(d.task.verify)
            if res.ok and checker and not checker(res.text):
                res.ok = False
                res.error = "failed verify check after repair"
            spec.paid_tokens += res.tokens_out
            spec.paid_chars += len(res.text or "")

            # Repairs go through the engine directly rather than run_job, so
            # nothing would record them without this -- and an invisible paid
            # call is the one kind this tool must never make. The dashboard's
            # whole job is showing where the allowance went.
            job_id = None
            if self.store:
                job_id = self.store.create_job(
                    kind=d.task.kind,
                    title=d.task.title or d.task.prompt[:80],
                    prompt=d.task.prompt, system=REPAIR_SYSTEM,
                    start_rung=rung, max_rung=rung,
                    swarm_id=swarm_id, cwd=getattr(self.router, "cwd", None),
                )
                self.store.mark_running(job_id)
                self.store.add_attempt(job_id, 1, tier, res)
                self.store.finish_job(
                    job_id, status="done" if res.ok else "failed",
                    result=res.text, error=res.error, final_rung=rung)

            self._record(d, accepted=False,
                         reason=d.reject_reason or d.error,
                         final=res.text, rung=rung, repair_job=job_id,
                         paid_tokens=res.tokens_out + d.verify_tokens)

            out.append({
                "job_id": job_id,
                "ok": res.ok,
                "result": res.text,
                "error": res.error,
                "title": d.task.title or d.task.prompt[:60],
                "rung": rung,
                "tier": tier.name,
                "model": res.model,
                "cost_usd": res.cost_usd,
                "escalations": 0,
                "repaired_from_draft": d.usable,
            })
        return out

    # -- the loop -----------------------------------------------------------

    def _record(self, d: Drafted, *, accepted: bool, reason: str, final: str,
                rung: int, repair_job: str | None = None,
                paid_tokens: int = 0) -> None:
        """Persist one draft-and-verdict. Never let telemetry break the run."""
        if not self.store:
            return
        try:
            self.store.record_speculation(
                kind=d.task.kind, prompt=d.task.prompt, draft=d.text,
                accepted=accepted, reason=reason, final=final,
                verify_rung=rung, swarm_id=self._swarm_id,
                repair_job=repair_job,
                draft_model=d.task.model or tiers.by_rung(0).model,
                draft_tokens=d.tokens_out, paid_tokens=paid_tokens,
            )
        except Exception as exc:  # noqa: BLE001
            # A failed insert must not lose the answer the caller is waiting
            # for. Losing a metric is survivable; losing the work is not.
            self._telemetry_errors.append(f"{type(exc).__name__}: {exc}")

    def _settle(self, group: list[Drafted], rung: int, spec: Speculation,
                results: list[dict], rejected: list[Drafted]) -> None:
        """Verify one chunk and file each draft as accepted or rejected."""
        verdicts = self.verify(group, rung, spec)
        for i, d in enumerate(group):
            if i not in verdicts:
                # Unjudged is not the same as approved. A chunk whose verdicts
                # would not parse gets re-run rather than waved through.
                spec.unverified += 1
                rejected.append(d)
                continue
            passed, reason = verdicts[i]
            spec.note(d.task.kind, passed)
            if not passed:
                spec.rejected += 1
                d.reject_reason = reason
                rejected.append(d)
                continue
            spec.accepted += 1
            # Only an accepted draft's tokens count as local generation. A
            # rejected one was regenerated at the paid tier, so counting it
            # would inflate the share with work that got thrown away.
            spec.local_tokens += d.tokens_out
            spec.local_chars += len(d.text)
            self._record(d, accepted=True, reason=reason, final=d.text,
                         rung=rung, paid_tokens=d.verify_tokens)
            results.append({
                "ok": True,
                "result": d.text,
                "title": d.task.title or d.task.prompt[:60],
                "rung": 0,
                "tier": "local",
                "speculative": True,
                "verified_by": tiers.by_rung(rung).name,
                "adjudication": reason,
                "cost_usd": 0.0,
                "escalations": 0,
            })

    def run(self, tasks: list[Task], *, verify_rung: int | None = None,
            draft_model: str | None = None, chunk: int = DEFAULT_CHUNK,
            swarm_id: str | None = None, pipeline: bool = True) -> dict:
        """Draft free, verify in bulk, re-run only the misses.

        `pipeline` overlaps drafting of chunk i+1 with verification of chunk i.
        The local box would otherwise sit idle for the entire 30-60s a
        verification call takes, and the two use completely different resources
        -- one is local memory bandwidth, the other is a network round trip. So
        the overlap is free throughput rather than contention. Asynchronous
        speculative decoding does the same thing for the same reason.

        Turn it off to measure the phases separately; the results are identical
        either way, only the wall clock differs.
        """
        if not tasks:
            return {"ok": True, "results": [], "spec": Speculation().as_dict()}

        swarm_id = swarm_id or uuid.uuid4().hex[:12]
        self._swarm_id = swarm_id
        self._telemetry_errors = []
        spec = Speculation()

        # Verify at the dearest rung any task in the set would have used.
        # Speculating against a cheaper tier than the work actually warranted
        # would be self-deception: it approves answers the real target rung
        # might well have rejected, and the saving is then imaginary.
        rung = verify_rung
        if rung is None:
            rung = max((target_rung_for(t) for t in tasks), default=1)
        rung = max(1, min(rung, tiers.MAX_RUNG))

        groups = [tasks[i:i + chunk] for i in range(0, len(tasks), chunk)]
        results: list[dict] = []
        rejected: list[Drafted] = []
        draft_clock = 0.0

        with ThreadPoolExecutor(max_workers=1) as ahead:
            def start(idx: int):
                return ahead.submit(self.draft, groups[idx], draft_model, swarm_id)

            pending, waited_from = start(0), time.perf_counter()

            for i, _ in enumerate(groups):
                drafts = pending.result()
                draft_clock += time.perf_counter() - waited_from
                has_next = i + 1 < len(groups)

                # Hand the next chunk to the local model BEFORE blocking on
                # this one's verification. The two contend for nothing -- one
                # is local memory bandwidth, the other a network round trip --
                # so the drafting is free.
                if pipeline and has_next:
                    pending, waited_from = start(i + 1), time.perf_counter()

                spec.drafted += len(drafts)
                usable = [d for d in drafts if d.usable]
                spec.draft_failed += len(drafts) - len(usable)
                rejected.extend(d for d in drafts if not d.usable)

                # Re-chunk after drafting: the character budget depends on the
                # answers, which did not exist when the tasks were first split.
                for sub in chunk_drafts(usable, size=chunk):
                    self._settle(sub, rung, spec, results, rejected)

                if not pipeline and has_next:
                    pending, waited_from = start(i + 1), time.perf_counter()

        # Time *blocked* waiting for drafts, not time spent drafting. When
        # pipelined the two differ, and the gap is exactly what the overlap
        # bought.
        spec.draft_seconds = draft_clock

        repaired = self.repair(rejected, rung, spec, swarm_id)
        for r in repaired:
            r["speculative"] = False
            results.append(r)

        return {
            "ok": all(r.get("ok") for r in results),
            "swarm_id": swarm_id,
            "verify_rung": rung,
            "verify_tier": tiers.by_rung(rung).name,
            "results": results,
            "spec": spec.as_dict(),
            # Surfaced rather than swallowed: silent telemetry loss is how a
            # dashboard quietly stops meaning anything.
            "telemetry_errors": list(self._telemetry_errors),
        }
