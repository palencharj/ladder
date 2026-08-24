"""Fallback for rungs 1-5: shell out to `claude -p` (headless Claude Code).

Why this exists: it authenticates with an existing Claude Code subscription, so
it needs no API key. What it costs: the Claude Code harness ships ~25-35k
tokens of system prompt and tool definitions on *every* invocation, measured on
this machine at ~$0.023 per call even for a one-word answer. That overhead is
irreducible -- stripping settings and MCP config only moved it from ~35k to
~25k and broke the cache prefix, making a single call cost more.

So: correct for a handful of tool-using jobs, wrong for a swarm. Prefer
AnthropicEngine whenever a credential is available.

The trade it buys you is real: this engine gets file editing, bash, and search
for free, which the raw API engine does not.
"""

from __future__ import annotations

import json
import shutil
import subprocess

from .base import Engine, Result, Timer

# Measured on this machine, 2026-08-24: a trivial `claude -p --model haiku`
# call reported 24,909 cache-read + 9,976 cache-creation tokens of pure
# harness overhead. Used only to warn; real cost comes back in the JSON.
HARNESS_OVERHEAD_TOKENS = 35_000


class ClaudeCliEngine(Engine):
    name = "cli"

    def __init__(self, cwd: str | None = None, allowed_tools: str | None = None,
                 timeout: int = 1800):
        self.cwd = cwd
        self.allowed_tools = allowed_tools
        self.timeout = timeout

    def available(self) -> tuple[bool, str]:
        exe = shutil.which("claude")
        if not exe:
            return False, "`claude` CLI not on PATH"
        return True, f"claude CLI at {exe}"

    def run(self, tier, system: str, prompt: str, max_tokens: int = 8000) -> Result:
        cmd = [
            "claude", "-p", prompt,
            "--model", tier.model,
            "--output-format", "json",
            "--system-prompt", system,
        ]
        if self.allowed_tools:
            cmd += ["--allowedTools", self.allowed_tools]
        else:
            # No tools requested -> single shot, no agentic loop.
            cmd += ["--max-turns", "1"]

        try:
            with Timer() as t:
                proc = subprocess.run(
                    cmd, capture_output=True, text=True,
                    timeout=self.timeout, cwd=self.cwd,
                )
        except subprocess.TimeoutExpired:
            return Result(
                text="", ok=False, engine=self.name, model=tier.model,
                rung=tier.rung, error=f"claude CLI timed out after {self.timeout}s",
            )
        except Exception as exc:  # noqa: BLE001
            return Result(
                text="", ok=False, engine=self.name, model=tier.model,
                rung=tier.rung, error=f"claude CLI failed: {exc}",
            )

        if proc.returncode != 0:
            return Result(
                text="", ok=False, engine=self.name, model=tier.model, rung=tier.rung,
                error=f"claude CLI exit {proc.returncode}: {proc.stderr[:500]}",
            )

        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError:
            # The CLI sometimes appends hook noise after the JSON object.
            line = next(
                (ln for ln in proc.stdout.splitlines() if ln.startswith("{")), ""
            )
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                return Result(
                    text="", ok=False, engine=self.name, model=tier.model,
                    rung=tier.rung,
                    error=f"unparseable CLI output: {proc.stdout[:300]}",
                )

        usage = data.get("usage", {}) or {}
        text = data.get("result", "") or ""
        return Result(
            text=text,
            ok=not data.get("is_error", False) and bool(text.strip()),
            engine=self.name,
            model=tier.model,
            rung=tier.rung,
            tokens_in=usage.get("input_tokens", 0),
            tokens_out=usage.get("output_tokens", 0),
            cache_read=usage.get("cache_read_input_tokens", 0),
            cache_write=usage.get("cache_creation_input_tokens", 0),
            cost_usd=float(data.get("total_cost_usd", 0.0) or 0.0),
            latency_ms=t.ms,
            stop_reason=data.get("stop_reason", "") or "",
            error="" if text.strip() else "empty CLI response",
            raw={"session_id": data.get("session_id", "")},
        )
