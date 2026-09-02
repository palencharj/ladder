"""UserPromptSubmit hook: put Ladder routing guidance where the decision is made.

An `@import` in CLAUDE.md is loaded once at session start and then competes with
everything else in context. This runs on *every* prompt and injects guidance
only when the prompt actually looks routable, so the reminder arrives at the
moment it is relevant instead of scrolling away hours earlier.

It cannot force a tool call -- nothing can. What it can do is make the cheapest
correct option impossible to overlook, and refuse to nag when the work is
clearly not a fit.

Two jobs:

1. **Nudge on routable work.** Bulk, mechanical, latency-tolerant prompts get a
   short pointer at the right tool with the right flags.
2. **Warn when rung 0 is down.** This is the failure that costs real allowance:
   if Ollama is not running, every job silently becomes a paid one and nothing
   in the UI says so. Cheap to check, expensive to miss.

Contract: reads the hook JSON on stdin, writes JSON on stdout. Silence (empty
output) is a valid answer and the common case -- most prompts are not routable
and deserve no interruption.
"""

from __future__ import annotations

import json
import re
import sys
import urllib.request

OLLAMA = "http://127.0.0.1:11434"

# Work Ladder is good at. Deliberately conservative: a false nudge is noise on
# every prompt, which is how a hook gets switched off.
BULK_SIGNALS = re.compile(
    r"\b("
    r"every|each|all (?:of )?(?:the )?(?:files?|functions?|methods?|classes|"
    r"endpoints?|tickets?|issues?|modules?)|"
    r"for (?:each|every)|across (?:the )?(?:repo|codebase|package)|"
    r"bulk|batch|one by one|in bulk"
    r")\b",
    re.I,
)

MECHANICAL_SIGNALS = re.compile(
    r"\b("
    r"docstrings?|summari[sz]e|summaries|classify|classification|triage|"
    r"categori[sz]e|extract|boilerplate|changelog|commit messages?|"
    r"rename|scaffold"
    r")\b",
    re.I,
)

# Work that gets *worse* through a cheap tier. Half the policy is knowing when
# to stay quiet.
PRECISION_SIGNALS = re.compile(
    r"\b("
    r"debug|root cause|why (?:is|does|did)|race condition|deadlock|"
    r"security|vulnerab|architect|design (?:a|the) |refactor the|migrate the|"
    r"invariant|thread.?saf"
    r")\b",
    re.I,
)


def ollama_down() -> bool:
    try:
        urllib.request.urlopen(f"{OLLAMA}/api/version", timeout=1.5)
        return False
    except Exception:  # noqa: BLE001 - any failure means rung 0 is unusable
        return True


def guidance(prompt: str) -> str | None:
    """What, if anything, is worth saying about this prompt."""
    notes: list[str] = []

    # The expensive silent failure comes first, and applies regardless of shape.
    if ollama_down():
        notes.append(
            "Ladder's local tier (rung 0) is DOWN -- Ollama is not responding. "
            "Every ladder job will fall through to a paid rung and spend "
            "subscription allowance without saying so. Start Ollama, or call "
            "ladder_health to confirm, before routing bulk work."
        )

    bulk = bool(BULK_SIGNALS.search(prompt))
    mechanical = bool(MECHANICAL_SIGNALS.search(prompt))
    precision = bool(PRECISION_SIGNALS.search(prompt))

    # Precision work beats a bulk signal: "debug every failing test" is still
    # debugging, and fanning it out produces confident shallow findings.
    if precision:
        return "\n".join(notes) if notes else None

    if bulk and mechanical:
        notes.append(
            "This looks like repetitive mechanical work -- the strongest case "
            "for Ladder. Prefer ladder_swarm with batch=true: the ~35k harness "
            "overhead is charged per invocation, not per task, so batching is "
            "the difference between one call and N. Add max_rung=0 to guarantee "
            "it cannot spend allowance, or adjudicate=true if the answers have "
            "to be right rather than merely well-formed."
        )
    elif mechanical:
        notes.append(
            "This looks mechanical and self-contained -- a ladder_run candidate. "
            "The task kind picks the rung; docstring/classify/summarize/extract "
            "start free on the local model."
        )
    elif bulk:
        notes.append(
            "This is repetitive across many items. If each item is independent "
            "and mechanical, ladder_swarm with batch=true does them in one "
            "invocation instead of N. If they depend on each other, or on "
            "judgement about the whole picture, keep it in this session."
        )

    return "\n".join(notes) if notes else None


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except Exception:  # noqa: BLE001 - never break the user's prompt
        return

    prompt = str(payload.get("prompt", ""))
    if not prompt.strip():
        return

    note = guidance(prompt)
    if not note:
        return  # silence is the common, correct answer

    json.dump(
        {
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": f"[Ladder routing]\n{note}",
            },
            "suppressOutput": True,
        },
        sys.stdout,
    )


if __name__ == "__main__":
    main()
