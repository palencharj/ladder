"""Is this tool worth running? A judgement, not a scoreboard.

Written to be capable of saying no. A dashboard that can only ever report
savings is marketing, and the whole premise of the ladder -- spend the least
that works -- is worthless if you cannot tell when it has stopped working.

The three ways Ladder fails to earn its keep, in order of how often they bite:

1. **No API credential.** Rungs 1-5 fall back to shelling out to `claude -p`,
   which carries 25-35k tokens of harness overhead per call. Measured: $0.023
   for a one-word reply, against roughly $0.0005 over the raw API. That is
   ~40x, and it makes every paid rung worse value than simply asking Claude
   Code directly. This is nearly always the biggest available lever.

2. **Wall clock nobody was willing to spend.** Rung 0 is free in dollars and
   expensive in time. Free is only genuinely free if the work ran unattended.
   The report converts the trade into an implied hourly rate -- dollars avoided
   per hour of local compute -- and judges that number rather than hiding it.

3. **A mistuned policy.** If jobs routinely escalate, the cheap attempt is pure
   waste: you pay for the failure and then pay again a rung up. A low first-try
   rate means `TASK_RUNGS` starts too low for the work being sent.
"""

from __future__ import annotations

# Below this, the free tier is not buying enough to justify a person waiting.
# Deliberately low: it is the cost of a coffee per hour, not a salary.
POOR_HOURLY_RATE = 1.0
GOOD_HOURLY_RATE = 10.0

# Under this many finished jobs, any conclusion is noise.
MIN_JOBS_FOR_A_VERDICT = 20

# Escalating more than this fraction of the time means the policy starts too low.
POOR_FIRST_TRY_RATE = 0.6


def assess(report: dict, using_cli_fallback: bool) -> dict:
    """Turn a report dict into a verdict plus concrete, ranked advice."""
    findings: list[str] = []
    actions: list[str] = []

    jobs = report.get("jobs_done", 0)
    net = report.get("net_saving", 0.0)
    hours = report.get("local_hours", 0.0)
    rate = report.get("implied_hourly_rate")
    first_try = report.get("first_try_rate", 0.0)
    wasted = report.get("wasted_spend", 0.0)
    spend = report.get("actual_spend", 0.0)

    # ---- the dominant lever, if it applies ----
    if using_cli_fallback:
        findings.append(
            "Paid rungs are running through the `claude -p` fallback, which "
            "adds 25-35k tokens of harness overhead to every call "
            "(~$0.023 measured for a one-word reply, vs ~$0.0005 over the raw "
            "API). Every paid number below is inflated roughly 40x."
        )
        actions.append(
            "Set ANTHROPIC_API_KEY. This is the single highest-value change "
            "available and it costs nothing to try."
        )

    # ---- not enough evidence ----
    if jobs < MIN_JOBS_FOR_A_VERDICT:
        return {
            "verdict": "insufficient-data",
            "headline": (
                f"Only {jobs} finished job(s). Too little to judge -- come back "
                f"after ~{MIN_JOBS_FOR_A_VERDICT}."
            ),
            "findings": findings,
            "actions": actions or [
                "Run a real batch of work through it, then check again."
            ],
        }

    # ---- is it net positive at all? ----
    if net <= 0:
        findings.append(
            f"Net saving is ${net:.4f}: the cheap tiers avoided less than the "
            f"failed attempts cost (${wasted:.4f} wasted on attempts that had "
            "to escalate)."
        )
        actions.append(
            "Raise the starting rung for whichever kinds escalate most -- see "
            "the per-kind table. Paying for a doomed cheap attempt first is "
            "worse than starting one rung up."
        )
        verdict = "not-worth-it"
        headline = "Costing more than it saves."

    # ---- free, but at what wall-clock price? ----
    elif rate is not None and rate < POOR_HOURLY_RATE and hours > 0.25:
        findings.append(
            f"The free tier avoided ${net:.4f} in exchange for {hours:.1f} "
            f"hours of local compute -- an implied ${rate:.2f}/hour. That is "
            "only a good trade if nobody was waiting on it."
        )
        actions.append(
            "Use rung 0 only for unattended batch work. For anything "
            "interactive, start at rung 1 -- Haiku costs cents and returns in "
            "seconds."
        )
        actions.append(
            "For short-output work, pass model='qwen2.5-coder:3b': measured "
            "1.7s vs 33.8s for the 30B, same answer."
        )
        verdict = "marginal"
        headline = f"Saving real money (${net:.4f}) but slowly."

    elif first_try < POOR_FIRST_TRY_RATE:
        findings.append(
            f"Only {first_try:.0%} of jobs succeed at their starting rung, so "
            "most work pays for a failed cheap attempt before escalating."
        )
        actions.append(
            "Retune TASK_RUNGS upward for the kinds with the worst first-try "
            "rate in the per-kind table."
        )
        verdict = "marginal"
        headline = "Working, but the policy starts too low."

    else:
        verdict = "worth-it"
        rate_note = f" at ${rate:.2f}/hour of local compute" if rate else ""
        headline = (
            f"Net ${net:.4f} avoided across {jobs} jobs{rate_note}, "
            f"{first_try:.0%} landing on the first rung."
        )
        findings.append(
            f"Actual spend ${spend:.4f}. Work that finished free would have "
            f"cost ${report.get('avoided_spend', 0):.4f} at Haiku rates."
        )

    if rate is not None and rate >= GOOD_HOURLY_RATE:
        findings.append(
            f"The free tier is returning ${rate:.2f}/hour of avoided spend, "
            "which comfortably justifies the wall clock."
        )

    return {
        "verdict": verdict,
        "headline": headline,
        "findings": findings,
        "actions": actions,
    }


def render(report: dict, assessment: dict) -> str:
    """Human-readable report. Used by the MCP tool and the CLI."""
    v = assessment["verdict"]
    banner = {
        "worth-it": "WORTH IT",
        "marginal": "MARGINAL",
        "not-worth-it": "NOT WORTH IT",
        "insufficient-data": "NOT ENOUGH DATA",
    }[v]

    window = (f"last {report['window_days']}d" if report.get("window_days")
              else "all time")
    lines = [
        f"=== Is Ladder worth it? ({window}) ===",
        f"{banner}: {assessment['headline']}",
        "",
        f"  jobs                {report['jobs_done']} done of {report['jobs']}",
        f"  actual spend        ${report['actual_spend']:.4f}",
        f"  avoided spend       ${report['avoided_spend']:.4f}   "
        "(free rung-0 work, priced at Haiku rates)",
        f"  wasted on retries   ${report['wasted_spend']:.4f}   "
        f"({report['wasted_attempts']} failed attempts)",
        f"  net saving          ${report['net_saving']:.4f}",
        f"  local compute       {report['local_hours']:.2f} h",
    ]
    if report.get("implied_hourly_rate") is not None:
        lines.append(
            f"  implied rate        ${report['implied_hourly_rate']:.2f}/hour "
            "of local compute"
        )
    lines.append(f"  first-try rate      {report['first_try_rate']:.0%}")

    if report.get("by_kind"):
        lines += ["", "  by task kind:"]
        for row in report["by_kind"]:
            lines.append(
                f"    {row['kind']:<16} {row['n']:>4} jobs  "
                f"first-try {row['first_try_rate']:.0%}  ${row['cost']:.4f}"
            )

    if report.get("by_user") and len(report["by_user"]) > 0:
        lines += ["", "  by user:"]
        for row in report["by_user"]:
            lines.append(
                f"    {row['user']:<16} {row['jobs']:>4} jobs  "
                f"{row['free_jobs']} free  ${row['spend']:.4f}"
            )

    if assessment["findings"]:
        lines += ["", "  what the numbers mean:"]
        lines += [f"    - {f}" for f in assessment["findings"]]
    if assessment["actions"]:
        lines += ["", "  do this next:"]
        lines += [f"    {i}. {a}" for i, a in enumerate(assessment["actions"], 1)]

    return "\n".join(lines)
