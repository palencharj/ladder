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
from pathlib import Path

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


def infer(prompt: str):
    """Ask Ladder's own classifier where this prompt would go.

    Imported lazily and defensively: this hook runs on every prompt the user
    types, and a hook that raises is a hook that gets switched off. A missing
    or broken Ladder checkout must degrade to the keyword signals below, not
    to an error in the middle of someone's session.
    """
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from ladder.classify import explain

        return explain(prompt)
    except Exception:  # noqa: BLE001
        return None


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
            "This is repetitive mechanical work -- the strongest case for "
            "Ladder. Use ladder_spec (speculative execution): the free local "
            "model drafts every answer and ONE paid call verifies the whole "
            "batch, so cost stops scaling with the number of tasks. Measured "
            "on 8 mixed tasks: 1 invocation against 6 for "
            "ladder_swarm(batch=true), all answers correct. Send the whole "
            "list in a single call -- that is where the entire saving is."
        )
    elif mechanical:
        notes.append(
            "This looks mechanical and self-contained. One item -> ladder_run. "
            "Several -> ladder_spec, which drafts them free and verifies them "
            "in one paid call. The task kind is inferred from the prompt, so "
            "no rung needs picking."
        )
    elif bulk:
        notes.append(
            "This is repetitive across many items. If each item is independent "
            "and mechanical, ladder_spec drafts them all locally and checks "
            "them in one invocation instead of N. If they depend on each "
            "other, or on judgement about the whole picture, keep it here -- "
            "fan-out cannot see the whole picture, and a cheap tier's answer "
            "on judgement work costs more to verify than it saved."
        )

    routed = infer(prompt)
    if routed and routed["free"] and routed["matched"] and not notes:
        notes.append(
            f"Ladder would route this to the FREE local tier as "
            f"kind={routed['kind']}. If it is one item, ladder_run; if several, "
            f"ladder_spec. Neither spends subscription allowance on the draft."
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
