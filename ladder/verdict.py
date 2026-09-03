"""Is this tool worth running? A judgement, not a scoreboard.

Written to be capable of saying no. A dashboard that can only ever report
savings is marketing, and the premise of the ladder -- spend the least that
works -- is worthless if you cannot tell when it has stopped working.

Measured in quota, not dollars
------------------------------
On a prepaid Claude Code plan nobody receives a bill, so dollars are the wrong
unit. The binding constraint is subscription allowance, and the unit that
consumes it is the **invocation**: every `claude -p` call spends ~35k tokens of
harness overhead before touching the task, charged per call however trivial the
work. A hundred one-line jobs burn ~3.5M tokens of allowance on overhead alone.

So the question is not "how much money did this save" but "how many requests
never had to spend allowance, and what did that cost in wall clock".

The three ways Ladder fails to earn its keep, in order of how often they bite:

1. **Nothing gets deflected.** If jobs escalate off the local tier anyway, you
   waited *and* spent the allowance. Strictly worse than going straight to the
   CLI. A low deflection rate means `TASK_RUNGS` starts too high, or the local
   model is not up to the work being sent.

2. **Deflection costs more wall clock than it is worth.** Rung 0 is free in
   quota and expensive in time. That is a fine trade unattended and a bad one
   with someone waiting, so the report surfaces seconds-per-deflection rather
   than burying it.

3. **Wasted local attempts.** Time spent on a local try that failed and
   escalated anyway is pure loss: the wall clock is gone and the request still
   spent its allowance.
"""

from __future__ import annotations

# Deflect less than this and the local tier is barely pulling its weight.
POOR_DEFLECTION_RATE = 0.25
GOOD_DEFLECTION_RATE = 0.60

# Wall clock per deflected request. Ten minutes of local compute to save one
# invocation is only sane for unattended batch work.
SLOW_SECONDS_PER_DEFLECTION = 600
BRISK_SECONDS_PER_DEFLECTION = 60

# Under this many finished jobs, any conclusion is noise.
MIN_JOBS_FOR_A_VERDICT = 20


def _fmt_tokens(n: float) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}k"
    return str(int(n))


def assess(report: dict, using_cli_fallback: bool = True) -> dict:
    """Turn a report into a verdict plus concrete, ranked advice.

    `using_cli_fallback` is accepted for callers that still pass it, but the
    judgement no longer changes on it: the CLI is the normal paid path on a
    subscription, not a degraded one.
    """
    findings: list[str] = []
    actions: list[str] = []

    done = report.get("jobs_done", 0)
    deflected = report.get("requests_deflected", 0)
    rate = report.get("deflection_rate", 0.0)
    tokens_saved = report.get("tokens_deflected", 0)
    tokens_spent = report.get("tokens_spent", 0)
    multiplier = report.get("quota_multiplier")
    spd = report.get("seconds_per_deflection")
    wasted_n = report.get("wasted_local_attempts", 0)
    wasted_h = report.get("wasted_local_hours", 0.0)

    if done < MIN_JOBS_FOR_A_VERDICT:
        return {
            "verdict": "insufficient-data",
            "headline": (
                f"Only {done} finished job(s). Too little to judge -- come back "
                f"after ~{MIN_JOBS_FOR_A_VERDICT}."
            ),
            "findings": findings,
            "actions": ["Run a real batch of work through it, then check again."],
        }

    # ---- wasted local effort is worth calling out at any verdict ----
    if wasted_n and wasted_h > 0.25:
        findings.append(
            f"{wasted_n} local attempts failed and escalated anyway, burning "
            f"{wasted_h:.1f}h of wall clock for requests that spent their "
            "allowance regardless. That time bought nothing."
        )
        actions.append(
            "Raise the starting rung for the kinds with the worst deflection "
            "rate below. A local attempt that reliably fails is pure loss."
        )

    # ---- the core judgement ----
    if rate < POOR_DEFLECTION_RATE:
        findings.append(
            f"Only {rate:.0%} of requests were handled locally. The rest spent "
            "subscription allowance anyway, after waiting for a local attempt "
            "first -- strictly worse than going straight to the CLI."
        )
        actions.insert(0, (
            "Send more mechanical work: classify, triage, extract, summarize, "
            "docstring. Those deflect reliably; open-ended implementation does "
            "not."
        ))
        verdict = "not-worth-it"
        headline = f"Barely deflecting anything ({rate:.0%})."

    elif spd is not None and spd > SLOW_SECONDS_PER_DEFLECTION:
        findings.append(
            f"Deflecting {rate:.0%} of requests, but each one costs "
            f"{spd / 60:.1f} minutes of local compute. Worth it unattended; "
            "painful if anyone is waiting."
        )
        actions.append(
            "Check the model is staying resident. A cold 18GB model costs ~33s "
            "to load against 0.3s warm, so intermittent use pays that reload "
            "repeatedly. Raise LADDER_KEEP_ALIVE (default 30m), and confirm "
            "with `ollama ps` that it is still held."
        )
        actions.append(
            "Lower max_tokens on rung-0 jobs. Local wall clock scales directly "
            "with it, and sustained runs throttle toward ~3 tok/s."
        )
        actions.append(
            "Only swap to a smaller local model if RAM is contended or the "
            "model is often cold -- warm, a 3B and a 30B MoE are within 7% of "
            "each other, so this is not the throughput lever it appears to be."
        )
        verdict = "marginal"
        headline = (
            f"{_fmt_tokens(tokens_saved)} tokens of allowance preserved, but slowly."
        )

    else:
        verdict = "worth-it"
        mult = f", stretching the plan {multiplier:.1f}x" if multiplier else ""
        headline = (
            f"{deflected} of {done} requests never spent allowance "
            f"({rate:.0%}){mult}."
        )

    # ---- always show the arithmetic ----
    findings.append(
        f"{_fmt_tokens(tokens_saved)} tokens of allowance not spent "
        f"({deflected} requests x ~35k harness overhead, plus the work "
        f"itself). {_fmt_tokens(tokens_spent)} tokens actually spent across "
        f"{report.get('cli_calls', 0)} CLI calls."
    )
    if spd is not None:
        findings.append(
            f"Local compute cost {spd / 60:.1f} min per deflected request "
            f"({report.get('local_hours', 0):.1f}h total)."
        )

    # ---- the structural lever, always worth stating ----
    unbatched = report.get("paid_attempts", 0) - report.get("batched_attempts", 0)
    if unbatched >= 5:
        actions.append(
            f"Batch paid work: pass batch=true to ladder_swarm. {unbatched} paid "
            f"tasks ran as their own invocation, spending ~"
            f"{_fmt_tokens(unbatched * 35_000)} tokens on harness overhead alone. "
            "The overhead is per call, not per task."
        )
    if report.get("batch_savings_tokens"):
        findings.append(
            f"Batching already saved {_fmt_tokens(report['batch_savings_tokens'])} "
            f"tokens by answering {report.get('paid_attempts', 0)} tasks in "
            f"{report.get('cli_calls', 0)} invocations."
        )

    return {
        "verdict": verdict,
        "headline": headline,
        "findings": findings,
        "actions": actions,
    }


def render(report: dict, assessment: dict) -> str:
    """Human-readable report. Used by the MCP tool and the dashboard."""
    banner = {
        "worth-it": "WORTH IT",
        "marginal": "MARGINAL",
        "not-worth-it": "NOT WORTH IT",
        "insufficient-data": "NOT ENOUGH DATA",
    }[assessment["verdict"]]

    window = (f"last {report['window_days']}d" if report.get("window_days")
              else "all time")
    mult = report.get("quota_multiplier")
    lines = [
        f"=== Is Ladder worth it? ({window}) ===",
        f"{banner}: {assessment['headline']}",
        "",
        "  SUBSCRIPTION ALLOWANCE",
        f"    requests deflected   {report['requests_deflected']} of "
        f"{report['jobs_done']}  ({report['deflection_rate']:.0%})",
        f"    tokens deflected     {_fmt_tokens(report['tokens_deflected'])}   "
        "(est: ~35k harness per call + the work)",
        f"    tokens spent         {_fmt_tokens(report['tokens_spent'])}   "
        f"across {report['cli_calls']} CLI calls",
        f"    quota multiplier     {f'{mult:.1f}x' if mult else 'n/a'}",
    ]
    if report.get("batch_savings_tokens"):
        lines.append(
            f"    saved by batching    "
            f"{_fmt_tokens(report['batch_savings_tokens'])}   "
            f"({report['paid_attempts']} tasks in {report['cli_calls']} calls)"
        )
    spec = report.get("speculation") or {}
    if spec.get("total"):
        lines += [
            "",
            "  SPECULATION  (local drafts checked in bulk by a paid tier)",
            f"    drafts accepted      {spec['accepted']} of {spec['total']}  "
            f"({spec['acceptance']:.0%} acceptance)",
            f"    generated locally    {_fmt_tokens(spec['local_tokens'])} of "
            f"{_fmt_tokens(spec['local_tokens'] + spec['paid_tokens'])} tokens  "
            f"({spec['local_share']:.0%} local)",
        ]
        # Acceptance and local share answer different questions, and the gap
        # between them is where the interesting cases hide: high acceptance
        # with a low local share means rung 0 handled many small things while
        # the paid tier still wrote all the long ones.
        weak = [k for k in spec.get("by_kind", [])
                if k["n"] >= 3 and k["acceptance"] < 0.5]
        if weak:
            names = ", ".join(
                f"{k['kind']} ({k['acceptance']:.0%} of {k['n']})" for k in weak[:4])
            lines.append(f"    rarely accepted      {names}")
            lines.append("                         -- stop speculating on these; "
                         "the verify call is pure overhead when the draft "
                         "always loses")

    lines += [
        "",
        "  WHAT IT COST",
        f"    local compute        {report['local_hours']:.2f} h",
    ]
    if report.get("seconds_per_deflection") is not None:
        lines.append(
            f"    per deflection       {report['seconds_per_deflection'] / 60:.1f} min"
        )
    if report.get("wasted_local_attempts"):
        lines.append(
            f"    wasted locally       {report['wasted_local_attempts']} attempts, "
            f"{report['wasted_local_hours']:.2f} h (failed, escalated anyway)"
        )

    if report.get("by_kind"):
        lines += ["", "  BY TASK KIND"]
        for row in report["by_kind"]:
            lines.append(
                f"    {row['kind']:<16} {row['n']:>4} jobs   "
                f"deflected {row['deflection_rate']:>4.0%}   "
                f"first-try {row['first_try_rate']:>4.0%}"
            )

    if report.get("by_user"):
        lines += ["", "  BY USER"]
        for row in report["by_user"]:
            lines.append(
                f"    {row['user']:<16} {row['jobs']:>4} jobs   "
                f"deflected {row['deflection_rate']:>4.0%}"
            )

    if assessment["findings"]:
        lines += ["", "  WHAT THE NUMBERS MEAN"]
        lines += [f"    - {f}" for f in assessment["findings"]]
    if assessment["actions"]:
        lines += ["", "  DO THIS NEXT"]
        lines += [f"    {i}. {a}" for i, a in enumerate(assessment["actions"], 1)]

    lines += [
        "",
        f"  (Notional API-rate equivalent: ${report.get('notional_spend_usd', 0):.4f}. "
        "On a subscription this is not a bill -- allowance above is the real cost.)",
    ]
    return "\n".join(lines)
