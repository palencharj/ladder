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
"""

from __future__ import annotations

import ast
import json as _json
import re
from collections.abc import Callable
from dataclasses import replace

from . import prompts, tiers
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

        Rungs 1-5 want the raw API. Without a credential that is impossible, so
        they fall back to the ``claude -p`` CLI, which authenticates against a
        Claude Code subscription instead. Correct, but far costlier per call
        because of the harness overhead the CLI ships on every invocation.
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

    def run_job(self, *, prompt: str, kind: str = "implement",
                rung: int | None = None, tier_name: str | None = None,
                max_rung: int | None = None, system_extra: str = "",
                verify: str | Verifier | None = None, max_tokens: int = 8000,
                title: str = "", swarm_id: str | None = None,
                job_id: str | None = None, model: str | None = None) -> dict:
        """Run one task, escalating on failure. Returns a compact result dict.

        The dict deliberately carries only the final text plus accounting --
        never the full transcript. Detail lives in the store.

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
        system = prompts.system_for(kind, system_extra)
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

        for n, rung_n in enumerate(range(start_tier.rung, ceiling + 1), start=1):
            # The override applies to the starting rung only; every rung above
            # it uses that rung's standard model.
            tier = start_tier if rung_n == start_tier.rung else tiers.by_rung(rung_n)
            res = self.engine_for(tier).run(tier, system, prompt,
                                            max_tokens=max_tokens)
            last = res

            if res.ok and checker and not checker(res.text):
                res.ok = False
                res.error = f"failed {getattr(checker, '__name__', 'verify')} check"

            if self.store and job_id:
                self.store.add_attempt(job_id, n, tier, res)
            trail.append({
                "rung": tier.rung, "tier": tier.name, "model": res.model,
                "engine": res.engine, "ok": res.ok, "cost_usd": res.cost_usd,
                "latency_ms": res.latency_ms, "error": res.error,
            })

            if res.ok:
                if self.store and job_id:
                    self.store.finish_job(job_id, status="done",
                                          result=res.text, final_rung=tier.rung)
                return {
                    "job_id": job_id, "ok": True, "result": res.text,
                    "rung": tier.rung, "tier": tier.name, "model": res.model,
                    "cost_usd": sum(a["cost_usd"] for a in trail),
                    "escalations": len(trail) - 1, "trail": trail,
                }

        err = last.error if last else "no attempt ran"
        if self.store and job_id:
            self.store.finish_job(job_id, status="failed", error=err,
                                  final_rung=ceiling)
        return {
            "job_id": job_id, "ok": False, "result": "", "error": err,
            "rung": ceiling, "cost_usd": sum(a["cost_usd"] for a in trail),
            "escalations": len(trail) - 1, "trail": trail,
        }
