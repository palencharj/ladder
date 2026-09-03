"""Ladder MCP server -- stdio JSON-RPC, no third-party dependencies.

Talks to the Router in-process and writes to the same SQLite database the
dashboard reads, so work driven from Claude Code shows up in the web UI
without the Flask server needing to be running at all.

Wire protocol: newline-delimited JSON-RPC 2.0 on stdin/stdout. Anything the
server wants to say to a human goes to stderr -- stdout is the protocol channel
and a stray print there corrupts the session.
"""

from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ladder import models as _models  # noqa: E402
from ladder import tiers  # noqa: E402
from ladder import verdict as _verdict  # noqa: E402
from ladder.classify import explain as _explain_kind  # noqa: E402
from ladder.classify import infer_kind  # noqa: E402
from ladder.pool import Swarm, Task, TierGate  # noqa: E402
from ladder.recall import (  # noqa: E402
    DEFAULT_CANDIDATES,
    DEFAULT_MAX_CHARS,
    Recaller,  # noqa: E402
)
from ladder.recall import render as _render_recall  # noqa: E402
from ladder.router import Router  # noqa: E402
from ladder.speculate import DEFAULT_CHUNK, Speculator  # noqa: E402
from ladder.store import Store  # noqa: E402

DEFAULT_PROTOCOL = "2025-06-18"

_store = Store()
_router = Router(store=_store)
_swarm = Swarm(_router, gate=TierGate())
_spec = Speculator(_router, store=_store)
_recall = Recaller(_router)


def log(msg: str) -> None:
    """Human-facing output must never touch stdout."""
    print(f"[ladder-mcp] {msg}", file=sys.stderr, flush=True)


# --------------------------------------------------------------------------
# Tool schemas
# --------------------------------------------------------------------------

_EFFORT_DESC = (
    "Lowest rung to start at. 0=local (free, CPU, slow, mechanical work), "
    "1=haiku, 2=sonnet/low, 3=sonnet/high, 4=opus, 5=fable. "
    "Omit to let the task kind choose -- that is the cheap default and is "
    "usually right."
)

_KIND_DESC = (
    "Task kind. Sets the starting rung automatically. Rung 0: classify, triage, "
    "docstring, boilerplate, rename, simple_edit, summarize, extract. "
    "Rung 1: doc, readme, test, review, commit_message, changelog. "
    "Rung 2+: implement, refactor, migrate, debug, architect."
)

TOOLS = [
    {
        "name": "ladder_health",
        "description": (
            "Check which engines are usable: local Ollama, the Anthropic API, "
            "and the claude CLI fallback. Call this first if anything is "
            "behaving unexpectedly -- it reports which paid path is actually "
            "in effect and why."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "ladder_tiers",
        "description": (
            "Show the escalation ladder: every rung with its model, price, "
            "concurrency budget, and the default rung for each task kind."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "ladder_run",
        "description": (
            "Run ONE task on the cheapest tier that can do it, escalating one "
            "rung at a time only if the attempt fails. Returns just the final "
            "text plus accounting -- the full transcript stays in the database, "
            "so this keeps the calling context clean. Use for a single unit of "
            "work; use ladder_swarm for many."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "The task. Be specific and self-contained."},
                "kind": {"type": "string", "description": _KIND_DESC},
                "rung": {"type": "integer", "description": _EFFORT_DESC},
                "tier": {"type": "string", "description": "Force a tier by name (local, haiku, sonnet, sonnet-high, opus, fable). Overrides rung and kind."},
                "model": {"type": "string", "description": "Swap the model at the starting rung only, keeping that rung's engine and pricing. Use a smaller local model for short-output work: qwen2.5-coder:3b classifies in ~4s where the 30B default takes ~30s. Escalation above the start rung uses each rung's standard model."},
                "max_rung": {"type": "integer", "description": "Ceiling for escalation. Set to the same value as rung to forbid escalation entirely (e.g. max_rung=0 keeps a job free)."},
                "verify": {"type": "string", "enum": ["python", "json", "nonempty"], "description": "Structural check applied to the output. On failure the job escalates one rung. 'python' means the output must parse as Python. NOTE: this checks form only, never correctness -- use adjudicate for that."},
                "adjudicate": {"type": "boolean", "description": "Have the next rung up check the answer before accepting it. Structural verifiers catch malformed output but not WRONG output -- a small local model will return well-formed JSON with a wrong number in it and the 'json' verify passes. Adjudication costs one small call at the rung above (far less than running the whole task there) and escalates if the answer is rejected. Use it whenever a cheap tier's answer has to be right rather than merely well-formed."},
                "system_extra": {"type": "string", "description": "Extra context appended to the system prompt, e.g. project conventions."},
                "max_tokens": {"type": "integer", "description": "Output cap. Default 8000. Use ~256 for classification."},
                "title": {"type": "string", "description": "Short label for the dashboard."},
            },
            "required": ["prompt"],
        },
    },
    {
        "name": "ladder_swarm",
        "description": (
            "Fan out MANY tasks concurrently, each on its own cheapest tier. "
            "Per-tier concurrency caps are enforced: local is capped low "
            "because CPU inference is near zero-sum, while API rungs fan out "
            "wide. Returns a swarm_id immediately; poll ladder_status. This is "
            "the tool for bulk work -- docstrings across a package, a review "
            "pass over every changed file, triaging a backlog."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "tasks": {
                    "type": "array",
                    "description": "Task objects. Each needs 'prompt'; may override kind/rung/tier/max_rung/verify/title.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "prompt": {"type": "string"},
                            "kind": {"type": "string"},
                            "rung": {"type": "integer"},
                            "tier": {"type": "string"},
                            "model": {"type": "string"},
                            "max_rung": {"type": "integer"},
                            "verify": {"type": "string"},
                            "adjudicate": {"type": "boolean"},
                            "system_extra": {"type": "string"},
                            "max_tokens": {"type": "integer"},
                            "title": {"type": "string"},
                        },
                        "required": ["prompt"],
                    },
                },
                "kind": {"type": "string", "description": "Default kind for tasks that do not set one."},
                "rung": {"type": "integer", "description": "Default starting rung for all tasks. " + _EFFORT_DESC},
                "tier": {"type": "string", "description": "Default tier by name for all tasks. Overrides rung and kind."},
                "model": {"type": "string", "description": "Default model override for all tasks, applied at the starting rung only."},
                "max_tokens": {"type": "integer", "description": "Default output cap for all tasks. Default 8000."},
                "max_rung": {"type": "integer", "description": "Default escalation ceiling for all tasks."},
                "verify": {"type": "string", "enum": ["python", "json", "nonempty"]},
                "adjudicate": {"type": "boolean", "description": "Have the next rung up check the answer before accepting it. Structural verifiers catch malformed output but not WRONG output -- a small local model will return well-formed JSON with a wrong number in it and the 'json' verify passes. Adjudication costs one small call at the rung above (far less than running the whole task there) and escalates if the answer is rejected. Use it whenever a cheap tier's answer has to be right rather than merely well-formed."},
                "system_extra": {"type": "string"},
                "batch": {"type": "boolean", "description": "Pack compatible tasks into ONE `claude -p` invocation instead of one per task. The ~35k harness overhead is charged per invocation, not per task, so 10 batched tasks spend it once instead of ten times. Measured: 6 classifications in 1 call, 175k tokens of allowance saved. Only tasks sharing kind/verify/max_tokens/model batch together, and only those with no escalation headroom and no adjudication -- a batch answers at exactly one rung. Falls back to individual calls if the batched reply cannot be parsed, so answers are never misaligned. Strongly recommended for bulk paid work on a subscription."},
                "wait": {"type": "boolean", "description": "Block until every task finishes and return all results. Default false (returns a swarm_id to poll)."},
            },
            "required": ["tasks"],
        },
    },
    {'name': 'ladder_spec', 'description': 'SPECULATIVE EXECUTION -- the cheapest way to run a LIST of tasks, and the first thing to reach for on bulk mechanical work.\n\nThe free local model drafts EVERY answer, then ONE paid call checks all of them at once, and only rejected drafts are re-run (the paid tier corrects the draft rather than rewriting from scratch). Borrowed from speculative decoding: a small model proposes, a big one verifies in bulk, and verification batches where generation does not.\n\nPREFER THIS OVER ladder_swarm(batch=true). Batching can only merge tasks sharing kind, verify, max_tokens and model, so mixed work fragments into several invocations. Speculation does not care -- every verification prompt has the same shape. MEASURED on 8 real mixed tasks from this repo: 1 invocation vs 6, all 8 answers correct, ~39k tokens of allowance against ~210k.\n\nIt also removes the shared-output-budget failure: a batch of answers must fit one 32k cap or the whole batch is lost, while verdicts need ~200 tokens each.\n\nCOSTS: roughly 2x the wall clock, because drafting happens first and local generation is slow. Latency-tolerant work only.\n\nDo NOT use for a single task (nothing to amortise), for sequential work where each step depends on the last, or for judgement-heavy work like code review -- measured here, the local model answered a review request with a summary and found none of the real bugs.\n\nPASTE THE GROUND TRUTH INTO EACH PROMPT when the answer makes factual claims (an API shape, a signature, real output). The verifier sees only the task and the answer, so with nothing to check against it grades structure, not facts -- measured, it accepted a draft containing a command-line syntax that does not exist. Accepted means checked, not correct.', 'inputSchema': {'type': 'object', 'properties': {'tasks': {'type': 'array', 'description': "Task objects. Only 'prompt' is required -- 'kind' is inferred from the text when omitted, so the caller needs no knowledge of the tier taxonomy.", 'items': {'type': 'object', 'properties': {'prompt': {'type': 'string'}, 'kind': {'type': 'string'}, 'title': {'type': 'string'}, 'verify': {'type': 'string'}, 'max_tokens': {'type': 'integer'}, 'system_extra': {'type': 'string'}}, 'required': ['prompt']}}, 'verify_rung': {'type': 'integer', 'description': 'Which tier checks the drafts. Omit to use the dearest rung the tasks themselves imply -- verifying below that approves answers the real target might have rejected. 1 (haiku) is the cheap default and is usually enough.'}, 'kind': {'type': 'string', 'description': 'Default kind for tasks that do not set one. Omit to infer per task from the prompt text.'}, 'draft_model': {'type': 'string', 'description': 'Local model to draft with. Defaults to the rung-0 model.'}, 'chunk': {'type': 'integer', 'description': 'Drafts per verification call. Default 12. Larger is cheaper -- yield is linear in chunk size, unlike real speculative decoding where it saturates -- but the verify prompt must hold every draft.'}, 'max_tokens': {'type': 'integer', 'description': 'Default output cap per task. Default 8000.'}, 'pipeline': {'type': 'boolean', 'description': 'Draft the next chunk while the current one is verified. Default true; the local box is otherwise idle for the whole round trip.'}}, 'required': ['tasks']}},
    {'name': 'ladder_route', 'description': 'Ask where prompts WOULD go, without running them. Returns the inferred task kind, the rung, and whether it is free. Use it when unsure whether something belongs on the local tier, or to explain the routing to someone. Costs nothing and calls no model.', 'inputSchema': {'type': 'object', 'properties': {'prompts': {'type': 'array', 'items': {'type': 'string'}, 'description': 'One or more prompts to classify.'}}, 'required': ['prompts']}},
    {'name': 'ladder_recall', 'description': "Answer a question FROM THE MEMORY VAULT without loading the vault into this conversation. Runs entirely on the free local model.\n\nUse it INSTEAD of reading vault notes yourself whenever you need past context: a prior decision and its reasoning, a gotcha already hit, the state of some project, where a credential lives. Browsing Home.md plus a map costs thousands of tokens of your context; this returns a few hundred characters of the passages that actually bear on the question.\n\nWHAT IT RETURNS: verbatim excerpts plus the file each came from. Never a summary. Every quote is mechanically checked to appear in the file it cites, and any the local model altered or invented is dropped before you see it -- so the usual caveat about cheap models fabricating does not apply here. It cannot fabricate; it can only choose badly, and a bad choice is visible to you as an irrelevant quote.\n\nIf the local model is unavailable or selects nothing verifiable, you get the search engine's own ranked paths and summaries instead, clearly marked. It is never worse than a free keyword search.\n\nCosts no subscription allowance. Takes 60-250s, so it is for questions worth waiting on rather than a reflex on every turn.", 'inputSchema': {'type': 'object', 'properties': {'question': {'type': 'string', 'description': 'What you want to know. A natural question works better than keywords -- retrieval is hybrid semantic plus lexical.'}, 'k': {'type': 'integer', 'description': 'Candidate notes to consider before filtering. Default 8. More costs local time, not allowance.'}, 'max_chars': {'type': 'integer', 'description': 'Hard cap on returned excerpt text. Default 4000. This is the whole point: a bounded, predictable context cost.'}, 'model': {'type': 'string', 'description': 'Local model override for the selection step. The default rung-0 model is worth its time here -- measured, a 3B returned 99 characters of tangential material where the 30B returned 1,033 characters that answered the question.'}}, 'required': ['question']}},
    {
        "name": "ladder_review",
        "description": (
            "Run a code review over one or more files, one job per file, "
            "concurrently. Defaults to rung 1 (Haiku) because review needs "
            "fluency but rarely deep reasoning. Drop to rung 0 for a free pass "
            "or raise for security-sensitive code."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "paths": {"type": "array", "items": {"type": "string"}, "description": "File paths to review."},
                "rung": {"type": "integer", "description": "Starting rung. Default 1 (haiku). 0 is free."},
                "max_rung": {"type": "integer", "description": "Escalation ceiling. Default: no escalation past the starting rung."},
                "focus": {"type": "string", "description": "Optional steer, e.g. 'concurrency safety' or 'error handling'."},
                "max_tokens": {"type": "integer", "description": "Output cap per file. Default 8000. Raise it for large files -- a review cut off mid-finding is worse than no review."},
                "model": {"type": "string", "description": "Model override at the starting rung. At rung 0 a smaller local model is dramatically faster on hardware without a discrete GPU."},
                "wait": {"type": "boolean", "description": "Block for results. Default true."},
            },
            "required": ["paths"],
        },
    },
    {
        "name": "ladder_status",
        "description": "Get the state of a job (by job_id) or a whole swarm (by swarm_id), including results and per-attempt escalation history.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "job_id": {"type": "string"},
                "swarm_id": {"type": "string"},
                "limit": {"type": "integer", "description": "With neither id, list this many recent jobs. Default 20."},
                "full": {"type": "boolean", "description": "Include full result text rather than a truncated preview."},
            },
        },
    },
    {
        "name": "ladder_report",
        "description": (
            "Is this tool actually worth running? Measured in SUBSCRIPTION "
            "ALLOWANCE, not dollars: how many requests never had to invoke the "
            "CLI at all, and how many tokens that avoided. Every `claude -p` "
            "call spends ~35k tokens of harness overhead before doing any work, "
            "charged per invocation however small the task, so each request the "
            "local tier absorbs saves a whole ~35k hit. Reports deflection rate, "
            "tokens deflected, quota multiplier, and the wall-clock price paid "
            "for it. Returns a verdict -- worth-it, marginal, or not-worth-it -- "
            "and will genuinely conclude the tool is not earning its keep. "
            "Per-user and per-task-kind breakdowns included."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "days": {"type": "integer", "description": "Only consider jobs from the last N days. Omit for all time."},
            },
        },
    },
    {
        "name": "ladder_models",
        "description": (
            "Manage the local models rung 0 runs on. Ladder is usually the only "
            "thing using local models, so it manages them rather than leaving "
            "you to remember ollama commands. Residency is the point: a cold "
            "18GB model costs ~33s to load against 0.3s warm, so warm it BEFORE "
            "a batch rather than letting the first job pay. Only one model "
            "should normally be resident -- idle ones hold their full weight in "
            "RAM for nothing. Actions: status (default), warm, unload, "
            "unload_others, ensure."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["status", "warm", "unload", "unload_others", "ensure"], "description": "status: what is installed, resident, and how much RAM is free. warm: load a model now. unload: free one. unload_others: free everything except `model`. ensure: present on disk AND resident."},
                "model": {"type": "string", "description": "Which model. Defaults to the current rung-0 model."},
            },
        },
    },
    {
        "name": "ladder_stats",
        "description": (
            "Spend summary: totals, per-tier breakdown, and what the same work "
            "would have cost had every job gone straight to the top rung. That "
            "delta is the argument for the ladder."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
]


# --------------------------------------------------------------------------
# Tool implementations
# --------------------------------------------------------------------------

def _compact(job: dict, full: bool = False) -> dict:
    """Trim a job row down to what an orchestrator actually needs."""
    result = job.get("result") or ""
    if not full and len(result) > 1200:
        result = result[:1200] + f"\n... [truncated, {len(result)} chars total]"
    return {
        "job_id": job["id"],
        "title": job["title"],
        "status": job["status"],
        "kind": job["kind"],
        "start_rung": job["start_rung"],
        "final_rung": job["final_rung"],
        "attempts": job["attempts"],
        "cost_usd": round(job["cost_usd"] or 0, 6),
        "result": result,
        "error": job.get("error") or "",
    }


def t_health(_args: dict) -> dict:
    h = _router.health()
    lines = ["Engine health:"]
    for key in ("ollama", "anthropic_api", "claude_cli"):
        v = h[key]
        lines.append(f"  {'OK ' if v['ok'] else 'DOWN'}  {key}: {v['detail']}")
    if h["ollama"]["models"]:
        lines.append(f"  local models: {', '.join(h['ollama']['models'])}")
    lines.append(f"  paid rungs (1-5) will use: {h['effective_paid_engine']}")
    if h["effective_paid_engine"] == "cli":
        lines.append(
            "  NOTE: each `claude -p` call carries ~35k tokens of harness "
            "overhead, charged per invocation however small the task. On a "
            "subscription that is allowance, not money. Deflecting requests to "
            "rung 0 is what preserves it -- see ladder_report."
        )
    return {"text": "\n".join(lines), "data": h}


def t_tiers(_args: dict) -> dict:
    lines = ["rung  tier         engine     model                 effort  conc   $in/$out per Mtok"]
    for t in tiers.LADDER:
        price = "free" if t.engine == "ollama" else f"${t.price_in}/${t.price_out}"
        lines.append(
            f"  {t.rung}   {t.name:<12} {t.engine:<10} {t.model:<21} "
            f"{str(t.effort or '-'):<7} {t.concurrency:<5}  {price}"
        )
    lines.append("\nDefault rung by task kind:")
    for rung in range(tiers.MAX_RUNG + 1):
        kinds = sorted(k for k, v in tiers.TASK_RUNGS.items() if v == rung)
        if kinds:
            lines.append(f"  rung {rung} ({tiers.by_rung(rung).name}): {', '.join(kinds)}")
    return {"text": "\n".join(lines)}


def t_run(args: dict) -> dict:
    out = _router.run_job(
        prompt=args["prompt"],
        kind=args.get("kind", "implement"),
        rung=args.get("rung"),
        tier_name=args.get("tier"),
        max_rung=args.get("max_rung"),
        system_extra=args.get("system_extra", ""),
        verify=args.get("verify"),
        max_tokens=int(args.get("max_tokens", 8000)),
        title=args.get("title", ""),
        model=args.get("model"),
        adjudicate=bool(args.get("adjudicate", False)),
    )
    head = (
        f"job {out['job_id']} | {'ok' if out['ok'] else 'FAILED'} | "
        f"tier={out.get('tier', '?')} (rung {out['rung']}) | "
        f"escalations={out['escalations']} | ${out['cost_usd']:.5f}"
    )
    if not out["ok"]:
        trail = "; ".join(
            f"rung {a['rung']} {a['tier']}: {a['error'][:120]}" for a in out["trail"]
        )
        return {"text": f"{head}\n{trail}"}
    return {"text": f"{head}\n\n{out['result']}"}


def t_swarm(args: dict) -> dict:
    raw = args.get("tasks") or []
    tasks = [
        Task(
            prompt=t["prompt"],
            kind=t.get("kind", args.get("kind", "implement")),
            title=t.get("title", ""),
            # Every setting falls back to the swarm-level default. `rung` and
            # `max_tokens` used not to, so a swarm-level rung was silently
            # dropped and the tasks quietly ran somewhere else entirely.
            rung=t.get("rung", args.get("rung")),
            tier_name=t.get("tier", args.get("tier")),
            max_rung=t.get("max_rung", args.get("max_rung")),
            system_extra=t.get("system_extra", args.get("system_extra", "")),
            verify=t.get("verify", args.get("verify")),
            max_tokens=int(t.get("max_tokens", args.get("max_tokens", 8000))),
            model=t.get("model", args.get("model")),
            adjudicate=bool(t.get("adjudicate", args.get("adjudicate", False))),
        )
        for t in raw if t.get("prompt")
    ]
    if not tasks:
        return {"text": "No tasks with a prompt were supplied."}

    swarm_id = uuid.uuid4().hex[:12]

    if not args.get("wait", False):
        import threading

        threading.Thread(
            target=_swarm.run, args=(tasks, swarm_id),
            kwargs={"batch": bool(args.get("batch", False))}, daemon=True
        ).start()
        by_rung: dict[int, int] = {}
        for t in tasks:
            by_rung[t.start_rung()] = by_rung.get(t.start_rung(), 0) + 1
        plan = ", ".join(
            f"{n} at rung {r} ({tiers.by_rung(r).name})"
            for r, n in sorted(by_rung.items())
        )
        return {"text": (
            f"swarm {swarm_id} started: {len(tasks)} tasks ({plan}).\n"
            f"Poll with ladder_status(swarm_id='{swarm_id}')."
        )}

    res = _swarm.run(tasks, swarm_id, batch=bool(args.get("batch", False)))
    lines = [
        f"swarm {swarm_id}: {res['succeeded']}/{res['total']} ok, "
        f"{res['failed']} failed, {res['escalations']} escalations, "
        f"{res.get('batched', 0)} batched, ${res['cost_usd']:.5f}",
        "",
    ]
    for r in res["results"]:
        mark = "ok  " if r.get("ok") else "FAIL"
        lines.append(f"[{mark}] {r.get('title', '')[:70]}")
        body = r.get("result") or r.get("error", "")
        lines.append("    " + body[:600].replace("\n", "\n    "))
        lines.append("")
    return {"text": "\n".join(lines)}


def t_spec(args: dict) -> dict:
    raw = args.get("tasks") or []
    default_kind = args.get("kind")
    tasks = [
        Task(
            prompt=t["prompt"],
            # Inference is per task, not per call: a mixed list is exactly what
            # speculation is best at, so forcing one kind across it would throw
            # away the advantage.
            kind=t.get("kind") or default_kind or infer_kind(t["prompt"]),
            title=t.get("title", ""),
            verify=t.get("verify"),
            max_tokens=int(t.get("max_tokens", args.get("max_tokens", 8000))),
            system_extra=t.get("system_extra", ""),
        )
        for t in raw if t.get("prompt")
    ]
    if not tasks:
        return {"text": "No tasks with a prompt were supplied."}
    if len(tasks) < 2:
        return {"text": (
            "Speculation needs at least 2 tasks -- its whole saving is spreading "
            "one verification call across many drafts. Use ladder_run for one."
        )}

    out = _spec.run(
        tasks,
        verify_rung=args.get("verify_rung"),
        draft_model=args.get("draft_model"),
        chunk=int(args.get("chunk", DEFAULT_CHUNK)),
        pipeline=bool(args.get("pipeline", True)),
    )
    sp = out["spec"]
    lines = [
        "speculative run {}: {}/{} drafts accepted by {} (rung {})".format(
            out["swarm_id"], sp["accepted"], sp["drafted"],
            out["verify_tier"], out["verify_rung"]),
        "  paid invocations: {} ({} verify + {} repair) vs {} unbatched".format(
            sp["paid_calls"], sp["verify_calls"], sp["repair_calls"],
            sp["naive_calls"]),
        "  acceptance {:.0%} | allowance preserved ~{:,} tokens".format(
            sp["acceptance"], sp["tokens_saved"]),
        "  generated tokens: {:,} local / {:,} paid ({:.0%} local)".format(
            sp["local_tokens"], sp["paid_tokens"], sp["local_share"]),
        "  draft {:.0f}s, verify {:.0f}s".format(
            sp["draft_seconds"], sp["verify_seconds"]),
    ]
    if sp["draft_failed"] or sp["unverified"]:
        lines.append(
            "  draft failures {}, unverified {} (both re-run at the paid tier)"
            .format(sp["draft_failed"], sp["unverified"]))
    if out.get("telemetry_errors"):
        lines.append("  TELEMETRY NOT RECORDED: {}".format(
            out["telemetry_errors"][:2]))
    lines.append("")

    for r in out["results"]:
        mark = "local" if r.get("speculative") else "rung {}".format(
            r.get("rung", "?"))
        flag = "ok  " if r.get("ok") else "FAIL"
        lines.append("[{}|{:>6}] {}".format(flag, mark, (r.get("title") or "")[:66]))
        body = r.get("result") or r.get("error", "")
        lines.append("    " + body[:700].replace(chr(10), chr(10) + "    "))
        lines.append("")
    return {"text": chr(10).join(lines)}


def t_route(args: dict) -> dict:
    prompts = args.get("prompts") or []
    if not prompts:
        return {"text": "No prompts given."}
    lines = []
    for pr in prompts:
        e = _explain_kind(pr)
        where = ("FREE (local)" if e["free"]
                 else "rung {} ({})".format(e["rung"], e["tier"]))
        note = "" if e["matched"] else "  [no pattern matched; using the default]"
        lines.append("{:<20} kind={:<15} {}{}".format(
            where, e["kind"], pr[:60], note))
    return {"text": chr(10).join(lines)}


def t_recall(args: dict) -> dict:
    question = (args.get("question") or "").strip()
    if not question:
        return {"text": "No question given."}
    out = _recall.recall(
        question,
        k=int(args.get("k", DEFAULT_CANDIDATES)),
        max_chars=int(args.get("max_chars", DEFAULT_MAX_CHARS)),
        model=args.get("model"),
    )
    return {"text": _render_recall(out)}


def t_review(args: dict) -> dict:
    paths = args.get("paths") or []
    rung = args.get("rung", tiers.TASK_RUNGS["review"])
    focus = args.get("focus", "")

    tasks, missing = [], []
    for p in paths:
        f = Path(p)
        if not f.is_file():
            missing.append(p)
            continue
        try:
            src = f.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            missing.append(f"{p} ({exc})")
            continue
        steer = f"\nPay particular attention to: {focus}\n" if focus else ""
        tasks.append(Task(
            prompt=(
                f"Review this file for correctness bugs and clear "
                f"simplifications.{steer}\n\n--- {f.name} ---\n{src}"
            ),
            kind="review",
            title=f"review {f.name}",
            rung=rung,
            max_rung=args.get("max_rung", rung),
            max_tokens=int(args.get("max_tokens", 8000)),
            model=args.get("model"),
        ))

    if not tasks:
        return {"text": f"No readable files. Missing: {', '.join(missing) or 'none given'}"}

    swarm_id = uuid.uuid4().hex[:12]
    if not args.get("wait", True):
        import threading

        threading.Thread(target=_swarm.run, args=(tasks, swarm_id), daemon=True).start()
        return {"text": f"review swarm {swarm_id} started over {len(tasks)} files."}

    res = _swarm.run(tasks, swarm_id)
    lines = [
        f"Reviewed {res['succeeded']}/{res['total']} files at rung {rung} "
        f"({tiers.by_rung(rung).name}), ${res['cost_usd']:.5f}",
    ]
    if missing:
        lines.append(f"Skipped (unreadable): {', '.join(missing)}")
    lines.append("")
    for r in res["results"]:
        lines.append(f"=== {r.get('title', '')} ===")
        lines.append(r.get("result") or f"[failed] {r.get('error', '')}")
        lines.append("")
    return {"text": "\n".join(lines)}


def t_status(args: dict) -> dict:
    full = bool(args.get("full", False))
    if args.get("job_id"):
        job = _store.get_job(args["job_id"])
        if not job:
            return {"text": f"No job {args['job_id']}."}
        out = _compact(job, full)
        trail = "\n".join(
            f"  attempt {a['n']}: rung {a['rung']} {a['tier']} ({a['model']}) "
            f"{'ok' if a['ok'] else 'FAIL'} {a['latency_ms']}ms "
            f"${a['cost_usd']:.5f} {a['error'][:100]}"
            for a in job["attempt_log"]
        )
        return {"text": json.dumps(out, indent=2) + "\n\nEscalation trail:\n" + trail}

    if args.get("swarm_id"):
        jobs = _store.list_jobs(limit=1000, swarm_id=args["swarm_id"])
        if not jobs:
            return {"text": f"No jobs for swarm {args['swarm_id']}."}
        done = sum(1 for j in jobs if j["status"] in ("done", "failed"))
        head = (
            f"swarm {args['swarm_id']}: {done}/{len(jobs)} finished, "
            f"{sum(1 for j in jobs if j['status'] == 'done')} ok, "
            f"${sum(j['cost_usd'] or 0 for j in jobs):.5f}"
        )
        return {"text": head + "\n\n" + json.dumps(
            [_compact(j, full) for j in jobs], indent=2)}

    jobs = _store.list_jobs(limit=int(args.get("limit", 20)))
    return {"text": json.dumps([_compact(j, full) for j in jobs], indent=2)}


def t_stats(_args: dict) -> dict:
    s = _store.stats()
    t = s["totals"]
    lines = [
        f"jobs={t['jobs']}  spend=${t['cost']:.4f}  "
        f"tokens in={t['tin']} out={t['tout']}",
        f"status: {s['by_status']}",
        "",
        "By tier:",
    ]
    for row in s["by_tier"]:
        lines.append(
            f"  rung {row['rung']} {row['tier']:<12} attempts={row['n']:<5} "
            f"${row['cost']:.4f}  avg {int(row['avg_ms'])}ms"
        )
    lines += [
        "",
        f"Same work entirely at rung 5 (fable) would be ~${s['fable_equivalent_cost']:.4f}.",
        f"Ladder saved ~${s['savings_vs_fable']:.4f}.",
    ]
    return {"text": "\n".join(lines)}


def t_report(args: dict) -> dict:
    rep = _store.report(days=args.get("days"))
    rep["speculation"] = _store.speculation_report(days=args.get("days"))
    health = _router.health()
    assessment = _verdict.assess(
        rep, using_cli_fallback=health["effective_paid_engine"] == "cli")
    return {"text": _verdict.render(rep, assessment)}


def validate_args(tool: dict, args: dict) -> str | None:
    """Return an error string if `args` contains keys the tool does not accept.

    Silently ignoring an unknown argument is the worst failure mode this server
    has: the call succeeds, the report looks healthy, and the work quietly ran
    somewhere other than where it was asked to. Found the hard way -- a
    swarm-level `rung` was dropped and an entire batch ran on the local tier
    while reporting success.

    Names are checked one level deep into `tasks[]` too, since that is where
    most per-task settings live.
    """
    schema = tool.get("inputSchema", {})
    allowed = set(schema.get("properties", {}))
    unknown = sorted(set(args) - allowed)
    if unknown:
        return (
            f"unknown argument(s) for {tool['name']}: {', '.join(unknown)}. "
            f"Accepted: {', '.join(sorted(allowed)) or '(none)'}."
        )

    item_schema = (schema.get("properties", {}).get("tasks", {})
                   .get("items", {}).get("properties"))
    if item_schema and isinstance(args.get("tasks"), list):
        task_allowed = set(item_schema)
        for i, task in enumerate(args["tasks"]):
            if not isinstance(task, dict):
                continue
            bad = sorted(set(task) - task_allowed)
            if bad:
                return (
                    f"unknown key(s) in tasks[{i}]: {', '.join(bad)}. "
                    f"Accepted per task: {', '.join(sorted(task_allowed))}."
                )
    return None


def t_models(args: dict) -> dict:
    action = args.get("action", "status")
    model = args.get("model") or tiers.by_rung(0).model

    if action == "status":
        st = _models.status()
        lines = [f"rung-0 model: {model}"]
        avail = st["available_ram_gb"]
        lines.append(
            f"RAM available: {avail:.1f} GB" if avail else "RAM available: unknown")
        lines.append(f"resident: {st['resident_gb']:.1f} GB across "
                     f"{len(st['resident'])} model(s)")
        if st["resident"]:
            lines.append("")
            for m in st["resident"]:
                lines.append(f"  LOADED  {m['name']:<24} {m['size_gb']:>5.1f} GB")
        lines.append("")
        for m in st["installed"]:
            held = any(r["name"] == m["name"] for r in st["resident"])
            lines.append(f"  {'on disk' if not held else 'loaded ':<8}"
                         f"{m['name']:<24} {m['size_gb']:>5.1f} GB")
        _, why = _models.fits(model)
        lines += ["", f"fit check: {why}"]
        return {"text": "\n".join(lines)}

    if action == "warm":
        ok, msg = _models.warm(model)
    elif action == "unload":
        ok, msg = _models.unload(model)
    elif action == "ensure":
        ok, msg = _models.ensure(model)
    elif action == "unload_others":
        freed = _models.unload_all_except(model)
        ok, msg = True, (f"freed {', '.join(freed)}" if freed
                         else f"nothing else was resident besides {model}")
    else:
        return {"text": f"unknown action {action!r}"}

    avail = _models.available_ram_gb()
    tail = f"  |  {avail:.1f} GB RAM available" if avail else ""
    return {"text": f"{'ok' if ok else 'FAILED'}: {msg}{tail}"}


HANDLERS = {
    "ladder_models": t_models,
    "ladder_report": t_report,
    "ladder_health": t_health,
    "ladder_tiers": t_tiers,
    "ladder_run": t_run,
    "ladder_swarm": t_swarm,
    "ladder_spec": t_spec,
    "ladder_recall": t_recall,
    "ladder_route": t_route,
    "ladder_review": t_review,
    "ladder_status": t_status,
    "ladder_stats": t_stats,
}


# --------------------------------------------------------------------------
# JSON-RPC plumbing
# --------------------------------------------------------------------------

def handle(msg: dict) -> dict | None:
    method = msg.get("method")
    mid = msg.get("id")

    if method == "initialize":
        client_proto = (msg.get("params") or {}).get("protocolVersion")
        return {
            "jsonrpc": "2.0", "id": mid,
            "result": {
                "protocolVersion": client_proto or DEFAULT_PROTOCOL,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "ladder", "version": "0.1.0"},
            },
        }

    if method in ("notifications/initialized", "initialized"):
        return None  # notification: no reply

    if method == "ping":
        return {"jsonrpc": "2.0", "id": mid, "result": {}}

    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}}

    if method == "tools/call":
        params = msg.get("params") or {}
        name = params.get("name")
        args = params.get("arguments") or {}
        fn = HANDLERS.get(name)
        if not fn:
            return {"jsonrpc": "2.0", "id": mid,
                    "error": {"code": -32601, "message": f"unknown tool: {name}"}}

        tool = next((t for t in TOOLS if t["name"] == name), None)
        if tool and (problem := validate_args(tool, args)):
            return {"jsonrpc": "2.0", "id": mid, "result": {
                "content": [{"type": "text", "text": problem}], "isError": True}}
        try:
            out = fn(args)
            return {"jsonrpc": "2.0", "id": mid,
                    "result": {"content": [{"type": "text", "text": out["text"]}]}}
        except Exception as exc:  # noqa: BLE001 - report, never crash the server
            log(f"tool {name} raised: {type(exc).__name__}: {exc}")
            return {"jsonrpc": "2.0", "id": mid, "result": {
                "content": [{"type": "text",
                             "text": f"tool error: {type(exc).__name__}: {exc}"}],
                "isError": True,
            }}

    if mid is None:
        return None  # unknown notification
    return {"jsonrpc": "2.0", "id": mid,
            "error": {"code": -32601, "message": f"unknown method: {method}"}}


def main() -> None:
    log(f"ready; db={_store.path}")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            log(f"bad JSON: {line[:200]}")
            continue
        resp = handle(msg)
        if resp is not None:
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
