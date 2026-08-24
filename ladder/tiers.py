"""The escalation ladder: the single source of truth for what runs where.

Design rule: **always start at the lowest rung that can plausibly do the job.**
Escalation is linear (rung N -> rung N+1), never a jump, so cost grows
predictably and every step up is auditable.

Rung 0 is free (local CPU inference). Every rung above it costs money, and the
cost multiplier between rung 0 and rung 5 is effectively infinite, so the
default for any task whose kind we recognise is the lowest rung in TASK_RUNGS.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Tier:
    """One rung of the ladder."""

    rung: int
    name: str
    engine: str  # "ollama" | "anthropic" | "cli"
    model: str
    # Anthropic `output_config.effort`. None means "do not send the parameter".
    # Haiku 4.5 REJECTS effort, so it must stay None for that rung.
    effort: str | None = None
    # Max simultaneous jobs. Local is CPU-bound and near zero-sum, so it is
    # deliberately tiny; API rungs are network-bound and fan out freely.
    concurrency: int = 4
    # USD per million tokens, (input, output). Zero for local.
    price_in: float = 0.0
    price_out: float = 0.0
    context: int = 200_000
    # Adaptive thinking is on by default for Sonnet 5 / Opus 5 / Fable 5 but
    # unsupported on Haiku 4.5 and meaningless for Ollama.
    thinking: bool = False
    notes: str = ""


# Prices are Anthropic first-party API rates, USD per million tokens.
# Sonnet 5 is listed at its standard rate; an introductory rate of $2/$10 runs
# through 2026-08-31, so real spend before that date is lower than estimated.
LADDER: tuple[Tier, ...] = (
    Tier(
        rung=0,
        name="local",
        engine="ollama",
        model="qwen3-coder:30b",
        effort=None,
        concurrency=2,
        price_in=0.0,
        price_out=0.0,
        context=256_000,
        thinking=False,
        notes="Free. CPU-only on this box. Use for anything mechanical.",
    ),
    Tier(
        rung=1,
        name="haiku",
        engine="anthropic",
        model="claude-haiku-4-5",
        effort=None,  # Haiku 4.5 returns 400 if `effort` is sent.
        concurrency=12,
        price_in=1.0,
        price_out=5.0,
        context=200_000,
        thinking=False,
        notes="Cheap and fast. Docs, tests, review passes, bulk edits.",
    ),
    Tier(
        rung=2,
        name="sonnet",
        engine="anthropic",
        model="claude-sonnet-5",
        effort="low",
        concurrency=8,
        price_in=3.0,
        price_out=15.0,
        context=1_000_000,
        thinking=True,
        notes="Real implementation work that needs to actually be correct.",
    ),
    Tier(
        rung=3,
        name="sonnet-high",
        engine="anthropic",
        model="claude-sonnet-5",
        effort="high",
        concurrency=6,
        price_in=3.0,
        price_out=15.0,
        context=1_000_000,
        thinking=True,
        notes="Same model, more thinking. Cheapest way to buy more care.",
    ),
    Tier(
        rung=4,
        name="opus",
        engine="anthropic",
        model="claude-opus-5",
        effort="high",
        concurrency=4,
        price_in=5.0,
        price_out=25.0,
        context=1_000_000,
        thinking=True,
        notes="Hard debugging, cross-cutting refactors, design calls.",
    ),
    Tier(
        rung=5,
        name="fable",
        engine="anthropic",
        model="claude-fable-5",
        effort="xhigh",
        concurrency=2,
        price_in=10.0,
        price_out=50.0,
        context=1_000_000,
        thinking=True,
        notes="Top rung. Thinking is always on and cannot be disabled.",
    ),
)

MAX_RUNG = LADDER[-1].rung

# What one `claude -p` invocation costs before it does any work at all.
#
# Measured on 2026-08-24 asking Haiku 4.5 for a one-word reply: 34,054 tokens
# of cache creation on a cold call, and 9,976 creation + 24,909 read on a warm
# one. Roughly 35k either way. Stripping settings and MCP config only moved it
# to ~25k and broke the cache prefix.
#
# On API billing this is a money problem. On a subscription it is a *quota*
# problem, and a much sharper one: the overhead is charged per invocation
# regardless of how trivial the task is, so a hundred one-line jobs burn ~3.5M
# tokens of allowance before any real work happens. Every request the local
# tier absorbs is one that never spends this.
CLI_OVERHEAD_TOKENS = 35_000

# Default rung per task kind. This encodes the "go as small as possible"
# policy: if a task kind is mechanical, it starts free and only climbs if it
# actually fails.
TASK_RUNGS: dict[str, int] = {
    # --- rung 0: mechanical, verifiable, low-judgement ---
    "classify": 0,
    "triage": 0,
    "docstring": 0,
    "boilerplate": 0,
    "rename": 0,
    "simple_edit": 0,
    "summarize": 0,
    "extract": 0,
    # --- rung 1: needs fluency but not deep reasoning ---
    "doc": 1,
    "readme": 1,
    "test": 1,
    "review": 1,
    "commit_message": 1,
    "changelog": 1,
    # --- rung 2+: needs to be right ---
    "implement": 2,
    "refactor": 2,
    "migrate": 2,
    "debug": 3,
    "architect": 4,
}

DEFAULT_KIND = "implement"


def by_rung(rung: int) -> Tier:
    """Return the tier at `rung`, clamped into range."""
    rung = max(0, min(int(rung), MAX_RUNG))
    return LADDER[rung]


def by_name(name: str) -> Tier:
    """Return a tier by its short name (e.g. 'haiku'). Raises KeyError."""
    for tier in LADDER:
        if tier.name == name:
            return tier
    raise KeyError(f"no such tier: {name!r} (have: {[t.name for t in LADDER]})")


def resolve(
    kind: str | None = None,
    rung: int | None = None,
    tier_name: str | None = None,
) -> Tier:
    """Pick a tier from the most specific hint available.

    Precedence: explicit tier name > explicit rung > task kind > default kind.
    """
    if tier_name:
        return by_name(tier_name)
    if rung is not None:
        return by_rung(rung)
    kind = (kind or DEFAULT_KIND).strip().lower()
    return by_rung(TASK_RUNGS.get(kind, TASK_RUNGS[DEFAULT_KIND]))


def estimate_cost(tier: Tier, tokens_in: int, tokens_out: int) -> float:
    """USD for a call of this shape at this tier."""
    return (tokens_in / 1e6) * tier.price_in + (tokens_out / 1e6) * tier.price_out
