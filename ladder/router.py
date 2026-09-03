"""The router: runs one job, climbing the ladder only when it has to.

Escalation policy
-----------------
Start at the rung the task kind implies (or the caller's explicit rung). Run.
If the attempt fails a check, move up exactly one rung and try again, up to
``max_rung``. Never skip a rung -- a linear climb means the cheapest sufficient
tier is always the one that wins, and the audit trail shows why each step up
happened.

A "failure" is any of: engine error, empty output, model refusal, or a
caller-supplied verifier returning False. That last one is what makes
escalation useful rather than decorative -- for example, checking that
generated Python actually parses before accepting it.

Truncation is the exception
---------------------------
Running out of output budget is a *budget* failure, not a capability failure,
and escalating for it would be strictly wrong: a dearer model handed the same
output cap hits the same wall, and you have paid a rung for the privilege.
Worse, ``max_tokens`` is one value for the whole job, so the climb could never
have fixed it.

So a truncated attempt retries at the **same** rung with double the budget, up
to a ceiling. Only real failures climb. An answer still truncated at the
ceiling is rejected rather than returned, because a half-finished answer that
reports success is the same silent-wrongness trap that structural verifiers
fall into.
"""

from __future__ import annotations

import ast
import json as _json
import re
from collections.abc import Callable
from dataclasses import replace

from . import prompts as prompts_mod
from . import tiers
from .engines import AnthropicEngine, ClaudeCliEngine, OllamaEngine, Result

Verifier = Callable[[str], bool]

_FENCE = re.compile(r"^\s*```[a-zA-Z0-9_+-]*\s*\n(.*?)\n?\s*```\s*$", re.DOTALL)


def strip_fence(text: str) -> str:
    """Remove a surrounding markdown code fence, if the model added one."""
    m = _FENCE.match(text)
    return m.group(1) if m else text


def python_parses(text: str) -> bool:
    """Verifier: output is syntactically valid Python."""
    try:
        ast.parse(strip_fence(text))
        return True
    except SyntaxError:
        return False


def json_parses(text: str) -> bool:
    """Verifier: output is valid JSON."""
    try:
        _json.loads(strip_fence(text))
        return True
    except ValueError:
        return False


def nonempty(text: str) -> bool:
    """Verifier: output has some content."""
    return bool(text.strip())


VERIFIERS: dict[str, Verifier] = {
    "python": python_parses,
    "json": json_parses,
    "nonempty": nonempty,
}

# Stop reasons meaning "I ran out of output budget", per engine. Ollama says
# "length"; the Anthropic API and the CLI say "max_tokens".
TRUNCATION_STOP_REASONS = frozenset({"length", "max_tokens"})

# How much more budget a rung gets after a truncated attempt, and the hard cap.
TRUNCATION_GROWTH = 2
MAX_TOKENS_CEILING = 32_000


def _rungs_climbed(trail: list[dict]) -> int:
    """How many rungs a job actually climbed.

    Not `len(trail) - 1`: a truncation retry adds a trail entry at the *same*
    rung, and counting those as escalations would overstate how often the
    ladder had to reach for a dearer tier -- exactly the number people tune
    TASK_RUNGS against.
    """
    return max(0, len({a["rung"] for a in trail}) - 1)


def was_truncated(res: Result) -> bool:
    """Did this attempt fail because it hit the output cap rather than the task?"""
    return (res.stop_reason or "").lower() in TRUNCATION_STOP_REASONS

# Adjudication prompt. Deliberately asks for a verdict token first so the
# answer can be parsed from a very short generation, and deliberately tells the
# adjudicator that rejecting is cheap -- a false PASS silently ships a wrong
# answer, while a false FAIL only costs one more rung.
ADJUDICATOR_SYSTEM = (
    "You check whether a proposed answer actually satisfies a task. "
    "Reply with exactly PASS or FAIL as the first word, then one short "
    "sentence of justification. Judge correctness and completeness, not style "
    "or formatting. Verify any arithmetic, counting, or factual claim yourself "
    "rather than assuming it is right. If you are unsure, answer FAIL -- a "
    "wrong answer that ships is far more expensive than one extra retry."
)

ADJUDICATOR_MAX_TOKENS = 200


def adjudication_verdict(text: str) -> tuple[bool, str]:
    """Parse an adjudicator reply into (passed, reason).

    Anything that is not a clear PASS counts as a failure, so a malformed or
    empty adjudication escalates rather than silently approving.
    """
    stripped = text.strip().lstrip("*# ").upper()
    if stripped.startswith("PASS"):
        return True, text.strip()[:200]
    return False, text.strip()[:200] or "adjudicator returned nothing"


class Router:
    """Owns the engines and executes the escalation loop for a single job."""

    def __init__(self, store=None, ollama_host: str | None = None,
                 cwd: str | None = None, prefer_cli: bool = False):
        self.store = store
        self.cwd = cwd
        self.prefer_cli = prefer_cli
        self._ollama = OllamaEngine(ollama_host) if ollama_host else OllamaEngine()
        self._api = AnthropicEngine()
        self._cli = ClaudeCliEngine(cwd=cwd)

    def engine_for(self, tier):
        """Pick the engine for a tier, falling back when no credential exists.

        Rungs 1-5 use the raw API when a credential exists, and otherwise the
        ``claude -p`` CLI against a Claude Code subscription. On a prepaid plan
        the CLI is the normal path, not a degraded one -- there is no bill, and
        the cost that matters is the ~35k tokens of harness overhead each
        invocation spends from the allowance.
        """
        if tier.engine == "ollama":
            return self._ollama
        if tier.engine == "anthropic":
            if self.prefer_cli:
                return self._cli
            ok, _ = self._api.available()
            return self._api if ok else self._cli
        return self._cli

    def health(self) -> dict:
        """Report which engines are usable right now, and which paid path wins."""
        api_ok, api_msg = self._api.available()
        cli_ok, cli_msg = self._cli.available()
        oll_ok, oll_msg = self._ollama.available()
        return {
            "ollama": {
                "ok": oll_ok,
                "detail": oll_msg,
                "models": self._ollama.installed_models(),
            },
            "anthropic_api": {"ok": api_ok, "detail": api_msg},
            "claude_cli": {"ok": cli_ok, "detail": cli_msg},
            "effective_paid_engine": (
                "anthropic" if api_ok else ("cli" if cli_ok else None)
            ),
        }

    def _resolve_verifier(self, verify) -> Verifier | None:
        if isinstance(verify, str):
            return VERIFIERS.get(verify)
        if callable(verify):
            return verify
        return None

    def _adjudicate(self, rung: int, prompt: str, answer: str,
                    fail_open: bool = True) -> tuple[bool, str, float]:
        """Ask the tier at `rung` whether `answer` actually satisfies `prompt`.

        Returns (passed, reason, cost).

        ``fail_open`` decides what an *unreachable checker* means, and the two
        callers genuinely want opposite answers. For ``run_job`` an answer that
        already passed its verifier is being double-checked, so a broken
        adjudicator should not escalate every job in flight -- that turns an
        infrastructure problem into a spending problem.

        Speculation is the opposite case: the answer came from the cheapest
        model and the check is the *only* thing standing behind it. Passing it
        by default would mark unverified local output as verified, which is the
        precise failure the mechanism exists to prevent. Those callers pass
        ``fail_open=False`` and get the work re-run instead.
        """
        tier = tiers.by_rung(rung)
        check_prompt = (
            f"TASK:\n{prompt}\n\n"
            f"PROPOSED ANSWER:\n{answer}\n\n"
            "Does the proposed answer correctly and completely satisfy the task?"
        )
        res = self.engine_for(tier).run(
            tier, ADJUDICATOR_SYSTEM, check_prompt,
            max_tokens=ADJUDICATOR_MAX_TOKENS,
        )
        if not res.ok:
            return (fail_open,
                    f"adjudicator unavailable ({res.error[:80]})", res.cost_usd)
        passed, reason = adjudication_verdict(res.text)
        return passed, reason, res.cost_usd

    def adjudicate_batch(self, rung: int, pairs: list[tuple[str, str]],
                         usage: dict | None = None) -> list[tuple[bool, str]] | None:
        """Check many answers in ONE invocation at the tier above.

        Adjudication used to force a job out of its batch and into its own call,
        which meant "cheap tier does the work, dearer tier checks it" cost 2N
        invocations for N tasks -- the exact overhead batching exists to avoid.
        Checking them together makes it 2 invocations total.

        Returns one (passed, reason) per pair, or None if the batch could not be
        trusted, in which case the caller falls back to checking individually.

        ``usage``, if given, is filled with this call's token counts. Callers
        measuring how much generation stayed local need the checker's own
        output counted as paid, or the figure flatters itself.
        """
        if not pairs:
            return []
        tier = tiers.by_rung(rung)
        engine = self.engine_for(tier)
        if not hasattr(engine, "run_batch"):
            return None

        prompts = [
            f"TASK:\n{task}\n\nPROPOSED ANSWER:\n{answer}\n\n"
            "Does the proposed answer correctly and completely satisfy the task?"
            for task, answer in pairs
        ]
        if not engine.batchable(prompts):
            return None

        results = engine.run_batch(tier, ADJUDICATOR_SYSTEM, prompts,
                                   max_tokens=ADJUDICATOR_MAX_TOKENS)
        if results is None:
            return None
        if usage is not None:
            usage["tokens_out"] = sum(r.tokens_out for r in results)
            usage["tokens_in"] = sum(r.tokens_in for r in results)
            usage["cost_usd"] = sum(r.cost_usd for r in results)
        # A verdict that will not parse counts as a failure, matching the
        # single-answer path: never approve something we could not read.
        return [adjudication_verdict(r.text) if r.ok
                else (False, f"adjudicator error: {r.error[:80]}")
                for r in results]

    def run_batch(self, tasks: list, swarm_id: str | None = None,
                  adjudicate: bool = False) -> list[dict] | None:
        """Answer several same-rung tasks in ONE paid invocation.

        The reason this exists: on a subscription the ~35k harness overhead is
        charged per `claude -p` call, not per task. Ten tasks answered in one
        call spend that fixed cost once instead of ten times.

        Returns None when batching is not safe or the reply could not be
        trusted -- unbatchable engine, too few or too many tasks, unparseable
        answer, wrong answer count. The caller falls back to individual jobs,
        which is slower and spends more allowance but is never misaligned.

        Tasks whose output fails their verifier come back with ``ok: False``
        and no escalation attempted; the caller escalates those individually,
        because a batch cannot climb as a unit.
        """
        if not tasks:
            return []
        first = tasks[0]
        tier = tiers.resolve(kind=first.kind, rung=first.rung,
                             tier_name=first.tier_name)
        if tier.engine == "ollama":
            return None  # local gains nothing: no per-call overhead to amortise

        engine = self.engine_for(tier)
        if not hasattr(engine, "run_batch"):
            return None
        prompts = [t.prompt for t in tasks]
        if not engine.batchable(prompts):
            return None

        system = prompts_mod.system_for(first.kind, first.system_extra)
        results = engine.run_batch(tier, system, prompts,
                                   max_tokens=first.max_tokens)
        if results is None:
            return None

        # One adjudication call covering every answer in the batch, rather than
        # one per task. Only answers that passed their structural verifier are
        # worth checking -- a malformed one has already failed.
        verdicts: dict[int, tuple[bool, str]] = {}
        if adjudicate and tier.rung < tiers.MAX_RUNG:
            candidates = [(i, tasks[i].prompt, results[i].text)
                          for i in range(len(tasks)) if results[i].ok]
            if candidates:
                got = self.adjudicate_batch(
                    tier.rung + 1, [(p, a) for _, p, a in candidates])
                if got is not None:
                    verdicts = {idx: v for (idx, _, _), v in zip(candidates, got,
                                                                 strict=True)}

        out: list[dict] = []
        for n_i, (task, res) in enumerate(zip(tasks, results, strict=True)):
            checker = self._resolve_verifier(task.verify)
            if res.ok and checker and not checker(res.text):
                res.ok = False
                res.error = f"failed {getattr(checker, '__name__', 'verify')} check"

            adjudication = ""
            if res.ok and n_i in verdicts:
                passed, reason = verdicts[n_i]
                adjudication = reason
                if not passed:
                    res.ok = False
                    res.error = f"rejected by rung {tier.rung + 1} adjudicator: {reason}"

            job_id = None
            if self.store:
                job_id = self.store.create_job(
                    kind=task.kind, title=task.title or task.prompt[:80],
                    prompt=task.prompt,
                    system=prompts_mod.system_for(task.kind, task.system_extra),
                    start_rung=tier.rung, max_rung=tier.rung,
                    swarm_id=swarm_id, cwd=self.cwd,
                )
                self.store.mark_running(job_id)
                self.store.add_attempt(job_id, 1, tier, res)
                self.store.finish_job(
                    job_id, status="done" if res.ok else "failed",
                    result=res.text, error=res.error, final_rung=tier.rung)

            out.append({
                "job_id": job_id, "ok": res.ok, "result": res.text,
                "error": res.error, "rung": tier.rung, "tier": tier.name,
                "model": res.model, "cost_usd": res.cost_usd,
                "escalations": 0, "batched": True,
                "title": task.title or task.prompt[:60],
                "trail": [{
                    "rung": tier.rung, "tier": tier.name, "model": res.model,
                    "engine": res.engine, "ok": res.ok,
                    "cost_usd": res.cost_usd, "latency_ms": res.latency_ms,
                    "error": res.error, "adjudication": adjudication,
                }],
            })
        return out

    def run_job(self, *, prompt: str, kind: str = "implement",
                rung: int | None = None, tier_name: str | None = None,
                max_rung: int | None = None, system_extra: str = "",
                verify: str | Verifier | None = None, max_tokens: int = 8000,
                title: str = "", swarm_id: str | None = None,
                job_id: str | None = None, model: str | None = None,
                adjudicate: bool = False) -> dict:
        """Run one task, escalating on failure. Returns a compact result dict.

        The dict deliberately carries only the final text plus accounting --
        never the full transcript. Detail lives in the store.

        ``adjudicate`` has the next rung up check each answer before it is
        accepted. Structural verifiers catch malformed output but not wrong
        output -- a 3B will happily return well-formed JSON claiming "charlie"
        has 6 characters, and the ``json`` verifier passes it. Adjudication
        costs one small call at the rung above, which is far cheaper than
        running the whole task there, and is the intended way to trust cheap
        tiers on work that has to be right.

        ``model`` swaps the model at the *starting* rung only, keeping that
        rung's engine, pricing, and concurrency budget. It exists so a caller
        can pick a smaller, faster local model for short-output work -- a 3B
        classifies in a few seconds where a 30B takes half a minute -- without
        inventing a whole new rung. Escalation above the start rung always uses
        the standard model for each rung, because the override is a statement
        about this task, not about the ladder.
        """
        start_tier = tiers.resolve(kind=kind, rung=rung, tier_name=tier_name)
        if model:
            start_tier = replace(start_tier, model=model)
        ceiling = tiers.MAX_RUNG if max_rung is None else max(start_tier.rung, max_rung)
        system = prompts_mod.system_for(kind, system_extra)
        checker = self._resolve_verifier(verify)

        if self.store and not job_id:
            job_id = self.store.create_job(
                kind=kind, title=title or prompt[:80], prompt=prompt,
                system=system, start_rung=start_tier.rung, max_rung=ceiling,
                swarm_id=swarm_id, cwd=self.cwd,
            )
        if self.store and job_id:
            self.store.mark_running(job_id)

        trail: list[dict] = []
        last: Result | None = None
        budget = max_tokens

        n = 0
        for rung_n in range(start_tier.rung, ceiling + 1):
            # The override applies to the starting rung only; every rung above
            # it uses that rung's standard model.
            tier = start_tier if rung_n == start_tier.rung else tiers.by_rung(rung_n)

            # Truncation is a *budget* failure, not a capability failure, so it
            # is answered with more budget at the same rung rather than by
            # escalating. Climbing would be strictly wrong: a dearer model with
            # the same output cap hits the same wall, and you have paid for the
            # privilege. Retry here until the budget is genuinely the problem.
            while True:
                n += 1
                res = self.engine_for(tier).run(tier, system, prompt,
                                                max_tokens=budget)
                last = res
                if not (was_truncated(res) and budget < MAX_TOKENS_CEILING):
                    break
                grown = min(budget * TRUNCATION_GROWTH, MAX_TOKENS_CEILING)
                res.ok = False
                res.error = (
                    f"output truncated at max_tokens={budget}; "
                    f"retrying same rung with {grown}"
                )
                if self.store and job_id:
                    self.store.add_attempt(job_id, n, tier, res)
                trail.append({
                    "rung": tier.rung, "tier": tier.name, "model": res.model,
                    "engine": res.engine, "ok": False, "cost_usd": res.cost_usd,
                    "latency_ms": res.latency_ms, "error": res.error,
                    "adjudication": "",
                })
                budget = grown

            # A truncated answer that survived the loop above hit the ceiling.
            # It is incomplete by definition, so do not let it pass silently.
            if res.ok and was_truncated(res):
                res.ok = False
                res.error = (
                    f"output truncated at the {MAX_TOKENS_CEILING} token ceiling; "
                    "the answer is incomplete"
                )

            if res.ok and checker and not checker(res.text):
                res.ok = False
                res.error = f"failed {getattr(checker, '__name__', 'verify')} check"

            # Structural verifiers only check shape. A cheap model can return
            # perfectly-formed JSON with a wrong number in it, and the job would
            # be recorded as a success. Adjudication asks the next rung up
            # whether the answer is actually right.
            adjudication = ""
            if res.ok and adjudicate and tier.rung < ceiling:
                passed, reason, cost = self._adjudicate(
                    tier.rung + 1, prompt, res.text)
                res.cost_usd += cost
                adjudication = reason
                if not passed:
                    res.ok = False
                    res.error = f"rejected by rung {tier.rung + 1} adjudicator: {reason}"

            if self.store and job_id:
                self.store.add_attempt(job_id, n, tier, res)
            trail.append({
                "rung": tier.rung, "tier": tier.name, "model": res.model,
                "engine": res.engine, "ok": res.ok, "cost_usd": res.cost_usd,
                "latency_ms": res.latency_ms, "error": res.error,
                "adjudication": adjudication,
                "tokens_in": res.tokens_in, "tokens_out": res.tokens_out,
            })

            if res.ok:
                if self.store and job_id:
                    self.store.finish_job(job_id, status="done",
                                          result=res.text, final_rung=tier.rung)
                return {
                    "job_id": job_id, "ok": True, "result": res.text,
                    "rung": tier.rung, "tier": tier.name, "model": res.model,
                    "cost_usd": sum(a["cost_usd"] for a in trail),
                    "escalations": _rungs_climbed(trail), "trail": trail,
                }

        err = last.error if last else "no attempt ran"
        if self.store and job_id:
            self.store.finish_job(job_id, status="failed", error=err,
                                  final_rung=ceiling)
        return {
            "job_id": job_id, "ok": False, "result": "", "error": err,
            "rung": ceiling, "cost_usd": sum(a["cost_usd"] for a in trail),
            "escalations": _rungs_climbed(trail), "trail": trail,
        }
