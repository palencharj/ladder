"""Engine interface shared by every backend on the ladder."""

from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class Result:
    """Uniform return shape from every engine.

    The orchestrator only ever sees this. That is what keeps the caller's
    context clean: the full transcript stays in the store, and only `text`
    comes back up.
    """

    text: str
    ok: bool = True
    engine: str = ""
    model: str = ""
    rung: int = -1
    tokens_in: int = 0
    tokens_out: int = 0
    cache_read: int = 0
    cache_write: int = 0
    cost_usd: float = 0.0
    latency_ms: int = 0
    stop_reason: str = ""
    error: str = ""
    raw: dict = field(default_factory=dict)

    def summary(self) -> str:
        state = "ok" if self.ok else "FAIL"
        return (
            f"[{state}] rung={self.rung} {self.model} "
            f"in={self.tokens_in} out={self.tokens_out} "
            f"${self.cost_usd:.5f} {self.latency_ms}ms"
        )


class Engine:
    """Base class. Subclasses implement `run`."""

    name = "base"

    def available(self) -> tuple[bool, str]:
        """Return (usable, human-readable reason)."""
        return True, "ok"

    def run(self, tier, system: str, prompt: str, max_tokens: int = 8000) -> Result:
        raise NotImplementedError


class Timer:
    def __enter__(self):
        self.t0 = time.perf_counter()
        return self

    def __exit__(self, *exc):
        self.ms = int((time.perf_counter() - self.t0) * 1000)
