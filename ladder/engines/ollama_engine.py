"""Rung 0: local inference via Ollama. Free, unmetered, CPU-bound here.

Uses only the standard library so the free tier has no pip dependencies.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from .base import Engine, Result, Timer

DEFAULT_HOST = "http://127.0.0.1:11434"


class OllamaEngine(Engine):
    name = "ollama"

    def __init__(self, host: str = DEFAULT_HOST, timeout: int = 900):
        self.host = host.rstrip("/")
        self.timeout = timeout

    def _post(self, path: str, payload: dict, timeout: int | None = None) -> dict:
        req = urllib.request.Request(
            f"{self.host}{path}",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:
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
        }
        try:
            with Timer() as t:
                data = self._post("/api/chat", payload)
        except urllib.error.URLError as exc:
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
