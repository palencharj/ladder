"""Work out what kind of task a prompt is, so nobody has to say.

The ladder only pays off if work lands on the right rung, and until now that
meant the caller knew what `kind="docstring"` implied and which rung it mapped
to. That is fine for someone who has read the docs and hopeless as a default:
a developer asking for docstrings across a package should not have to learn a
tier taxonomy to get them cheaply.

So: infer the kind from the text, and let TASK_RUNGS do the rest.

Why keywords rather than a model
--------------------------------
This runs on every task, and it has to be free and instant or it eats the
saving it exists to produce. A local model call costs seconds; a regex costs
microseconds. More importantly a wrong guess here is cheap and self-correcting
-- the verifier or the escalation loop catches it -- so the accuracy needed is
"usually right", which keywords comfortably reach.

`infer_kind` deliberately returns the *default* kind rather than guessing wildly
when nothing matches, because the default starts at rung 2 and the failure mode
of an unrecognised prompt should be "runs somewhere sensible", not "runs free
and comes back wrong".

Precedence is the whole design
------------------------------
Prompts routinely match several patterns at once. "Write a docstring explaining
why this concurrency bug happens" contains both `docstring` and `bug`. Getting
that one wrong in the cheap direction produces a confident, useless answer, so
judgement signals are checked FIRST and win. The list is ordered most-demanding
to least, and the first match wins.
"""

from __future__ import annotations

import re

from . import tiers

# Ordered most-demanding first. The first pattern to match decides, so a prompt
# mentioning both a refactor and a rename is treated as a refactor.
_PATTERNS: tuple[tuple[str, str], ...] = (
    # --- judgement work: must win over any mechanical word in the same prompt
    ("architect", r"\b(architect|design (?:a|the|this) system|trade-?offs?|"
                  r"which approach|should we use|high-level design)\b"),
    # `bug` cannot be matched bare: "classify these issues as BUG" is a rung-0
    # classification, not a debugging job. It only counts as debugging when
    # something is being asked *about* the bug.
    ("debug", r"\b(debug|root cause|traceback|stack ?trace|failing test|"
              r"reproduce|regression|crash(?:es|ing)?|race condition|deadlock|"
              r"flaky)\b"
              r"|\bwhy\b[\s\S]{0,40}?\b(bug|happen\w*|fail\w*|break\w*|wrong|"
              r"error|broken)\b"
              r"|\b(bug|defect) (?:in|here|happens|occurs|is)\b"),
    ("migrate", r"\b(migrat\w+|upgrade (?:to|from)|port (?:this|it|the) "
                r"(?:to|from)|backport)\b"),
    ("refactor", r"\b(refactor|restructur\w+|extract (?:a )?(?:method|function|"
                 r"class)|decompos\w+|untangle)\b"),
    ("implement", r"\b(implement|build (?:a|the)|add support for|write (?:a|the) "
                  r"(?:feature|endpoint|parser|module)|make it (?:work|support))\b"),
    # --- fluency work: rung 1
    ("review", r"\b(review|code ?review|critique|audit|look over|"
               r"what(?:'s| is) wrong with)\b"),
    ("test", r"\b(unit ?tests?|write tests?|test cases?|pytest|add coverage)\b"),
    ("commit_message", r"\b(commit (?:message|subject)|git log entry)\b"),
    ("changelog", r"\b(changelog|release notes?|what changed)\b"),
    ("readme", r"\b(readme|getting started (?:guide|doc))\b"),
    ("doc", r"\b(document(?:ation)?|write (?:the )?docs?|explain (?:how|what) "
            r"(?:this|it) does|user guide)\b"),
    # --- mechanical work: rung 0
    ("docstring", r"\b(docstrings?|doc ?comments?|javadoc|xmldoc)\b"),
    ("classify", r"\b(classify|categori[sz]e|label (?:this|each)|which category|"
                 r"tag (?:this|each)|is this a bug or)\b"),
    ("triage", r"\b(triage|prioriti[sz]e|severity|assign (?:a )?priority)\b"),
    ("extract", r"\b(extract|pull out|list (?:all|the) (?:names?|functions?|"
                r"imports?|fields?)|parse out|enumerate the)\b"),
    ("summarize", r"\b(summari[sz]e|summary|tl;?dr|in one sentence|"
                  r"one-?line description|briefly (?:describe|say))\b"),
    ("rename", r"\b(rename|renaming|change the name of)\b"),
    ("boilerplate", r"\b(boilerplate|scaffold|stub out|getters? and setters?|"
                    r"dataclass|template for)\b"),
    ("simple_edit", r"\b(typo|spelling|reformat|reindent|add a trailing comma|"
                    r"sort the imports)\b"),
)

_COMPILED = tuple((kind, re.compile(rx, re.IGNORECASE)) for kind, rx in _PATTERNS)


def infer_kind(prompt: str, default: str | None = None) -> str:
    """Guess the task kind from the prompt text.

    Returns the configured default when nothing matches, which puts an
    unrecognised prompt on a rung that can actually do the work rather than
    gambling it on the free tier.
    """
    text = (prompt or "").strip()
    if not text:
        return default or tiers.DEFAULT_KIND
    for kind, rx in _COMPILED:
        if rx.search(text):
            return kind
    return default or tiers.DEFAULT_KIND


def infer_rung(prompt: str) -> int:
    """The rung `prompt` would start at if its kind were inferred."""
    return tiers.resolve(kind=infer_kind(prompt)).rung


def explain(prompt: str) -> dict:
    """What was inferred and why -- for the routing hint and for debugging."""
    kind = infer_kind(prompt)
    tier = tiers.resolve(kind=kind)
    return {
        "kind": kind,
        "rung": tier.rung,
        "tier": tier.name,
        "free": tier.rung == 0,
        "matched": kind != tiers.DEFAULT_KIND,
    }
