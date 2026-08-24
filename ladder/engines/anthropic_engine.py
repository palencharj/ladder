"""Rungs 1-5: the Anthropic API via the official SDK.

Per-model API rules encoded here (getting these wrong is a 400):

* ``effort`` lives inside ``output_config``, never at the top level, and
  Haiku 4.5 rejects it outright -- so it is only sent when the tier declares one.
* Adaptive thinking (``{"type": "adaptive"}``) is for Sonnet 5 / Opus 5 /
  Fable 5. Haiku 4.5 predates it.
* Fable 5 always thinks; it cannot be disabled and ``budget_tokens`` is gone.
* ``temperature`` / ``top_p`` / ``top_k`` were removed on these models -- we
  never send them.
* ``stop_reason == "refusal"`` arrives as HTTP 200, so it must be checked
  before reading content.
"""

from __future__ import annotations

import os

from .base import Engine, Result, Timer

# Cache pricing multipliers relative to the base input rate.
CACHE_WRITE_MULT = 1.25  # 5-minute TTL
CACHE_READ_MULT = 0.10

# Above this, the SDK wants streaming so a long generation cannot trip the
# HTTP timeout.
STREAM_THRESHOLD = 16_000

# Caching only engages past roughly this many tokens of stable prefix; below it
# a breakpoint is silently ignored, so we do not bother setting one.
MIN_CACHEABLE_CHARS = 4_000


class AnthropicEngine(Engine):
    name = "anthropic"

    def __init__(self, api_key: str | None = None):
        self._key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        self._client = None

    def _client_or_raise(self):
        if self._client is None:
            import anthropic

            # Zero-arg construction also picks up ANTHROPIC_AUTH_TOKEN or an
            # `ant auth login` profile, so do not force the key in.
            self._client = (
                anthropic.Anthropic(api_key=self._key)
                if self._key
                else anthropic.Anthropic()
            )
        return self._client

    def available(self) -> tuple[bool, str]:
        try:
            import anthropic  # noqa: F401
        except ImportError:
            return False, "anthropic SDK not installed (pip install anthropic)"
        if self._key:
            return True, "ANTHROPIC_API_KEY set"
        if os.environ.get("ANTHROPIC_AUTH_TOKEN"):
            return True, "ANTHROPIC_AUTH_TOKEN set"
        return False, (
            "no API credential found; set ANTHROPIC_API_KEY or run `ant auth login`. "
            "Falling back to the claude-cli engine, which costs ~25-35k tokens of "
            "harness overhead per call."
        )

    @staticmethod
    def _cost(tier, usage) -> float:
        base_in = tier.price_in / 1e6
        return (
            getattr(usage, "input_tokens", 0) * base_in
            + getattr(usage, "cache_creation_input_tokens", 0) * base_in * CACHE_WRITE_MULT
            + getattr(usage, "cache_read_input_tokens", 0) * base_in * CACHE_READ_MULT
            + getattr(usage, "output_tokens", 0) * (tier.price_out / 1e6)
        )

    def _build_kwargs(self, tier, system: str, prompt: str, max_tokens: int) -> dict:
        # A long, stable system prompt is worth a cache breakpoint; a short one
        # is not (the API ignores prefixes under ~1024 tokens).
        if len(system) >= MIN_CACHEABLE_CHARS:
            system_field = [
                {
                    "type": "text",
                    "text": system,
                    "cache_control": {"type": "ephemeral"},
                }
            ]
        else:
            system_field = system

        kwargs: dict = {
            "model": tier.model,
            "max_tokens": max_tokens,
            "system": system_field,
            "messages": [{"role": "user", "content": prompt}],
        }
        if tier.effort:
            kwargs["output_config"] = {"effort": tier.effort}
        if tier.thinking:
            kwargs["thinking"] = {"type": "adaptive"}
        return kwargs

    def run(self, tier, system: str, prompt: str, max_tokens: int = 8000) -> Result:
        try:
            client = self._client_or_raise()
        except Exception as exc:  # noqa: BLE001
            return Result(
                text="", ok=False, engine=self.name, model=tier.model,
                rung=tier.rung, error=f"client init failed: {exc}",
            )

        kwargs = self._build_kwargs(tier, system, prompt, max_tokens)

        try:
            with Timer() as t:
                if max_tokens > STREAM_THRESHOLD:
                    with client.messages.stream(**kwargs) as stream:
                        msg = stream.get_final_message()
                else:
                    msg = client.messages.create(**kwargs)
        except Exception as exc:  # noqa: BLE001 - surface any API failure to the router
            return Result(
                text="", ok=False, engine=self.name, model=tier.model,
                rung=tier.rung, error=f"{type(exc).__name__}: {exc}",
            )

        # A refusal is a 200 with no usable content -- check before reading.
        if msg.stop_reason == "refusal":
            details = getattr(msg, "stop_details", None)
            category = getattr(details, "category", None) if details else None
            return Result(
                text="", ok=False, engine=self.name, model=tier.model,
                rung=tier.rung, stop_reason="refusal",
                cost_usd=self._cost(tier, msg.usage),
                latency_ms=t.ms,
                error=f"model refused (category={category})",
            )

        text = "".join(b.text for b in msg.content if b.type == "text")
        u = msg.usage
        return Result(
            text=text,
            ok=bool(text.strip()),
            engine=self.name,
            model=tier.model,
            rung=tier.rung,
            tokens_in=getattr(u, "input_tokens", 0),
            tokens_out=getattr(u, "output_tokens", 0),
            cache_read=getattr(u, "cache_read_input_tokens", 0),
            cache_write=getattr(u, "cache_creation_input_tokens", 0),
            cost_usd=self._cost(tier, u),
            latency_ms=t.ms,
            stop_reason=msg.stop_reason or "",
            error="" if text.strip() else "empty response",
        )
