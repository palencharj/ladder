"""Fallback for rungs 1-5: shell out to `claude -p` (headless Claude Code).

Why this exists: it authenticates with an existing Claude Code subscription, so
it needs no API key. What it costs: the Claude Code harness ships ~25-35k
tokens of system prompt and tool definitions on *every* invocation, measured on
this machine at ~$0.023 per call even for a one-word answer. That overhead is
irreducible -- stripping settings and MCP config only moved it from ~35k to
~25k and broke the cache prefix, making a single call cost more.

On API billing that is a money problem. On a prepaid Claude Code plan -- which
is how most teams run -- it is an *allowance* problem, and a sharper one: the
overhead is charged per invocation regardless of task size, so a hundred
one-line jobs spend ~3.5M tokens of quota before any real work happens.

This engine is not a degraded fallback in that setting; it is the normal paid
path, and the AnthropicEngine is the special case that needs a key. The way to
economise is not to avoid this engine but to *invoke it less*: deflect what you
can to rung 0, and batch what you cannot.

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


# Batching caps. The overhead is per invocation, so bigger batches are always
# cheaper in allowance -- but a batch that overruns its output budget loses
# every task in it, so these stay conservative.
MAX_BATCH = 20
MAX_BATCH_PROMPT_CHARS = 60_000

BATCH_SYSTEM = (
    "You will be given several independent tasks in one message. Answer every "
    "one of them.\n\n"
    "Return ONLY a JSON array of strings: one element per task, in the order "
    "given, each element being the complete answer to that task. Return "
    "nothing outside the array -- no prose, no markdown fence, no commentary. "
    "The array must have exactly as many elements as there are tasks. If a "
    "task cannot be answered, still return an element for it explaining why, "
    "so the positions stay aligned."
)


def build_batch_prompt(prompts: list[str]) -> str:
    """Pack independent tasks into one message with positional markers."""
    parts = [f"There are {len(prompts)} tasks. Return a JSON array of "
             f"{len(prompts)} strings.\n"]
    for i, p in enumerate(prompts, start=1):
        parts.append(f"=== TASK {i} ===\n{p}\n")
    return "\n".join(parts)


def parse_batch_reply(text: str, expected: int) -> list[str] | None:
    """Split a batch reply into per-task answers, or None if unusable.

    Returning None rather than a partial list is deliberate: a misaligned batch
    would silently hand task 3's answer to task 4, which is far worse than
    falling back to individual calls.
    """
    cleaned = text.strip()
    if cleaned.startswith("```"):
        lines = cleaned.split("\n")
        cleaned = "\n".join(lines[1:-1] if lines[-1].strip().startswith("```")
                            else lines[1:])
    try:
        parsed = json.loads(cleaned)
    except ValueError:
        return None
    if not isinstance(parsed, list) or len(parsed) != expected:
        return None
    return [x if isinstance(x, str) else json.dumps(x) for x in parsed]


class ClaudeCliEngine(Engine):
    name = "cli"

    def __init__(self, cwd: str | None = None, allowed_tools: str | None = None,
                 timeout: int = 1800):
        self.cwd = cwd
        self.allowed_tools = allowed_tools
        self.timeout = timeout

    def batchable(self, prompts: list[str]) -> bool:
        """Is this set of prompts safe to send as one invocation?"""
        return (
            1 < len(prompts) <= MAX_BATCH
            and sum(len(p) for p in prompts) <= MAX_BATCH_PROMPT_CHARS
        )

    def run_batch(self, tier, system: str, prompts: list[str],
                  max_tokens: int = 8000) -> list[Result] | None:
        """Answer many independent tasks in ONE invocation.

        This is the main lever on a subscription. The ~35k harness overhead is
        charged per call, not per task, so ten tasks in one call spend roughly
        35k instead of 350k of allowance -- a 10x saving on the fixed cost that
        has nothing to do with the work itself.

        Returns None when the batch cannot be trusted (unparseable reply, wrong
        number of answers, engine failure). The caller then falls back to
        individual calls: slower and dearer, but never misaligned.

        Accounting divides the invocation's real tokens evenly across the
        tasks, so per-job figures stay comparable with unbatched runs.
        """
        if not prompts:
            return []
        combined = build_batch_prompt(prompts)
        # One shared output budget has to cover every answer.
        budget = min(max_tokens * len(prompts), 32_000)

        res = self.run(tier, f"{system}\n\n{BATCH_SYSTEM}", combined,
                       max_tokens=budget)
        if not res.ok:
            return None
        answers = parse_batch_reply(res.text, len(prompts))
        if answers is None:
            return None

        n = len(prompts)
        return [
            Result(
                text=answer,
                ok=bool(answer.strip()),
                engine=self.name,
                model=res.model,
                rung=tier.rung,
                tokens_in=res.tokens_in // n,
                tokens_out=res.tokens_out // n,
                cache_read=res.cache_read // n,
                cache_write=res.cache_write // n,
                cost_usd=res.cost_usd / n,
                latency_ms=res.latency_ms // n,
                stop_reason=res.stop_reason,
                error="" if answer.strip() else "empty answer in batch",
                raw={"batched": n},
            )
            for answer in answers
        ]

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
                    # text=True alone decodes with the *locale* encoding, which
                    # is cp1252 on a default Windows box. The CLI emits UTF-8,
                    # so every em dash and curly quote came back corrupted --
                    # silently, in the delivered answer, not just in a log.
                    # errors="replace" keeps a stray byte from killing the job.
                    encoding="utf-8", errors="replace",
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
