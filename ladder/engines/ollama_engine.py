"""Rung 0: local inference via Ollama. Free, unmetered, CPU-bound here.

Uses only the standard library so the free tier has no pip dependencies.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

from .base import Engine, Result, Timer

DEFAULT_HOST = "http://127.0.0.1:11434"

# A fixed timeout cannot work here, because how long a local job takes is a
# function of how many tokens it was asked for and how slow the box is.
#
# The original fixed 900s was actively harmful: the default max_tokens of 8000,
# at the ~3 tok/s this hardware falls to once thermally throttled, needs well
# over 2000s. Every
# rung-0 job that genuinely filled its budget timed out, the router read the
# timeout as a failure, and the job escalated to a PAID tier. The free tier was
# quietly billing you.
#
# So derive the deadline from the work requested, using a deliberately
# pessimistic floor rate. Being too generous only delays a failure; being too
# tight spends real money.
MIN_TOKENS_PER_SEC = 2.0   # slower than any machine measured; a floor, not an estimate
LOAD_OVERHEAD_SEC = 120    # cold model load from disk, plus prompt prefill
MIN_TIMEOUT_SEC = 300
MAX_TIMEOUT_SEC = 7200     # backstop so a wedged server cannot hang a swarm forever

# How long Ollama keeps the model resident in RAM after a request.
#
# This is the single biggest lever on *perceived* rung-0 speed, and it is not
# about tokens per second at all. Measured on this machine with the same
# two-token classification:
#
#   qwen3-coder:30b, cold   ~33 s   (paging 18 GB in from disk)
#   qwen3-coder:30b, warm     0.3 s
#
# A hundredfold difference, entirely residency. An earlier round of measurement
# mistook that load cost for the 3B being twenty times faster to generate; warm,
# the two are within 7% of each other.
#
# Ollama's own default is 5 minutes, which is far too short for a tool used in
# bursts: walk away for a coffee and the next job pays the full reload. Thirty
# minutes keeps it resident across a working session. Set LADDER_KEEP_ALIVE to
# "-1" to hold it indefinitely, or something short to reclaim the RAM sooner.
KEEP_ALIVE = os.environ.get("LADDER_KEEP_ALIVE", "30m")


class OllamaEngine(Engine):
    name = "ollama"

    def __init__(self, host: str = DEFAULT_HOST, timeout: int | None = None):
        """`timeout` of None derives a per-request deadline from max_tokens."""
        self.host = host.rstrip("/")
        self.timeout = timeout

    def _timeout_for(self, max_tokens: int) -> int:
        """Deadline for generating `max_tokens` on a slow CPU."""
        if self.timeout is not None:
            return self.timeout
        needed = max_tokens / MIN_TOKENS_PER_SEC + LOAD_OVERHEAD_SEC
        return int(min(max(needed, MIN_TIMEOUT_SEC), MAX_TIMEOUT_SEC))

    def _post(self, path: str, payload: dict, timeout: int) -> dict:
        req = urllib.request.Request(
            f"{self.host}{path}",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())

    def available(self) -> tuple[bool, str]:
        try:
            with urllib.request.urlopen(f"{self.host}/api/version", timeout=3) as r:
                v = json.loads(r.read().decode()).get("version", "?")
            return True, f"ollama {v}"
        except Exception as exc:  # noqa: BLE001 - report any reachability failure
            return False, f"ollama unreachable at {self.host}: {exc}"

    def installed_models(self) -> list[str]:
        try:
            with urllib.request.urlopen(f"{self.host}/api/tags", timeout=5) as r:
                data = json.loads(r.read().decode())
            return [m["name"] for m in data.get("models", [])]
        except Exception:  # noqa: BLE001
            return []

    def run(self, tier, system: str, prompt: str, max_tokens: int = 8000) -> Result:
        payload = {
            "model": tier.model,
            "stream": False,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            "options": {"num_predict": max_tokens, "temperature": 0.2},
            # Keep the model resident. On a cold start an 18 GB model costs
            # ~33s to page in, against 0.3s warm -- residency dominates
            # perceived speed far more than tokens/sec does.
            "keep_alive": KEEP_ALIVE,
        }
        deadline = self._timeout_for(max_tokens)
        try:
            with Timer() as t:
                data = self._post("/api/chat", payload, timeout=deadline)
        except TimeoutError as exc:
            return Result(
                text="", ok=False, engine=self.name, model=tier.model, rung=tier.rung,
                error=(
                    f"local model exceeded {deadline}s for max_tokens={max_tokens}. "
                    f"Lower max_tokens, or raise MIN_TOKENS_PER_SEC if this box is "
                    f"faster than the floor assumes. ({exc})"
                ),
            )
        except urllib.error.URLError as exc:
            # A socket timeout surfaces here as URLError wrapping TimeoutError.
            reason = getattr(exc, "reason", None)
            if isinstance(reason, TimeoutError):
                return Result(
                    text="", ok=False, engine=self.name, model=tier.model,
                    rung=tier.rung,
                    error=(
                        f"local model exceeded {deadline}s for "
                        f"max_tokens={max_tokens}. Lower max_tokens."
                    ),
                )
            return Result(
                text="", ok=False, engine=self.name, model=tier.model,
                rung=tier.rung, error=f"ollama request failed: {exc}",
            )
        except Exception as exc:  # noqa: BLE001
            return Result(
                text="", ok=False, engine=self.name, model=tier.model,
                rung=tier.rung, error=f"ollama error: {exc}",
            )

        text = (data.get("message") or {}).get("content", "")
        return Result(
            text=text,
            ok=bool(text.strip()),
            engine=self.name,
            model=tier.model,
            rung=tier.rung,
            tokens_in=data.get("prompt_eval_count", 0),
            tokens_out=data.get("eval_count", 0),
            cost_usd=0.0,
            latency_ms=t.ms,
            stop_reason=data.get("done_reason", ""),
            error="" if text.strip() else "empty response from local model",
            raw={k: v for k, v in data.items() if k != "message"},
        )
