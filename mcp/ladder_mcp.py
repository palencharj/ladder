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

from ladder import tiers  # noqa: E402
from ladder.pool import Swarm, Task, TierGate  # noqa: E402
from ladder.router import Router  # noqa: E402
from ladder.store import Store  # noqa: E402

DEFAULT_PROTOCOL = "2025-06-18"

_store = Store()
_router = Router(store=_store)
_swarm = Swarm(_router, gate=TierGate())


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
                            "title": {"type": "string"},
                        },
                        "required": ["prompt"],
                    },
                },
                "kind": {"type": "string", "description": "Default kind for tasks that do not set one."},
                "max_rung": {"type": "integer", "description": "Default escalation ceiling for all tasks."},
                "verify": {"type": "string", "enum": ["python", "json", "nonempty"]},
                "adjudicate": {"type": "boolean", "description": "Have the next rung up check the answer before accepting it. Structural verifiers catch malformed output but not WRONG output -- a small local model will return well-formed JSON with a wrong number in it and the 'json' verify passes. Adjudication costs one small call at the rung above (far less than running the whole task there) and escalates if the answer is rejected. Use it whenever a cheap tier's answer has to be right rather than merely well-formed."},
                "system_extra": {"type": "string"},
                "wait": {"type": "boolean", "description": "Block until every task finishes and return all results. Default false (returns a swarm_id to poll)."},
            },
            "required": ["tasks"],
        },
    },
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
            "  NOTE: the CLI fallback adds ~25-35k tokens of harness overhead "
            "per call (~$0.02+ even for trivial work). Set ANTHROPIC_API_KEY "
            "to make rungs 1-5 dramatically cheaper."
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
            rung=t.get("rung"),
            tier_name=t.get("tier"),
            max_rung=t.get("max_rung", args.get("max_rung")),
            system_extra=t.get("system_extra", args.get("system_extra", "")),
            verify=t.get("verify", args.get("verify")),
            max_tokens=int(t.get("max_tokens", 8000)),
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
            target=_swarm.run, args=(tasks, swarm_id), daemon=True
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

    res = _swarm.run(tasks, swarm_id)
    lines = [
        f"swarm {swarm_id}: {res['succeeded']}/{res['total']} ok, "
        f"{res['failed']} failed, {res['escalations']} escalations, "
        f"${res['cost_usd']:.5f}",
        "",
    ]
    for r in res["results"]:
        mark = "ok  " if r.get("ok") else "FAIL"
        lines.append(f"[{mark}] {r.get('title', '')[:70]}")
        body = r.get("result") or r.get("error", "")
        lines.append("    " + body[:600].replace("\n", "\n    "))
        lines.append("")
    return {"text": "\n".join(lines)}


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
            max_tokens=4000,
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


HANDLERS = {
    "ladder_health": t_health,
    "ladder_tiers": t_tiers,
    "ladder_run": t_run,
    "ladder_swarm": t_swarm,
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
