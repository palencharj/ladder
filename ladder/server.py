"""Flask app: REST API + the dashboard the whole thing is driven from.

Deliberately small. Jobs run on a background thread pool so the HTTP layer
never blocks -- a local rung-0 job can take a minute, which is far longer than
any sane request timeout.
"""

from __future__ import annotations

import threading
import uuid
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory

from . import tiers
from .pool import Swarm, Task, TierGate
from .router import Router
from .store import Store

WEB_DIR = Path(__file__).resolve().parent / "web"


def create_app(db_path: str | None = None, cwd: str | None = None) -> Flask:
    app = Flask(__name__, static_folder=None)
    store = Store(db_path) if db_path else Store()
    router = Router(store=store, cwd=cwd)
    gate = TierGate()
    swarm = Swarm(router, gate=gate)

    # Background execution so HTTP returns immediately with a job id.
    runner = threading.Semaphore(sum(t.concurrency for t in tiers.LADDER))

    def _spawn(fn, *args, **kwargs) -> None:
        def wrapped():
            with runner:
                try:
                    fn(*args, **kwargs)
                except Exception:  # noqa: BLE001 - never kill the worker thread
                    app.logger.exception("background job failed")

        threading.Thread(target=wrapped, daemon=True).start()

    # ---------------- static ----------------

    @app.get("/")
    def index():
        return send_from_directory(WEB_DIR, "index.html")

    # ---------------- meta ----------------

    @app.get("/api/health")
    def health():
        return jsonify(router.health())

    @app.get("/api/tiers")
    def list_tiers():
        return jsonify({
            "ladder": [
                {
                    "rung": t.rung, "name": t.name, "engine": t.engine,
                    "model": t.model, "effort": t.effort,
                    "concurrency": t.concurrency, "price_in": t.price_in,
                    "price_out": t.price_out, "context": t.context,
                    "thinking": t.thinking, "notes": t.notes,
                }
                for t in tiers.LADDER
            ],
            "task_rungs": tiers.TASK_RUNGS,
        })

    @app.get("/api/stats")
    def stats():
        return jsonify(store.stats())

    # ---------------- jobs ----------------

    @app.post("/api/job")
    def create_job():
        body = request.get_json(force=True, silent=True) or {}
        prompt = (body.get("prompt") or "").strip()
        if not prompt:
            return jsonify({"error": "prompt is required"}), 400

        kind = body.get("kind", "implement")
        start = tiers.resolve(kind=kind, rung=body.get("rung"),
                              tier_name=body.get("tier"))
        ceiling = body.get("max_rung")
        ceiling = tiers.MAX_RUNG if ceiling is None else max(start.rung, int(ceiling))

        job_id = store.create_job(
            kind=kind, title=body.get("title") or prompt[:80], prompt=prompt,
            system=body.get("system_extra", ""), start_rung=start.rung,
            max_rung=ceiling, cwd=cwd,
        )

        _spawn(
            router.run_job,
            prompt=prompt, kind=kind, rung=body.get("rung"),
            tier_name=body.get("tier"), max_rung=body.get("max_rung"),
            system_extra=body.get("system_extra", ""),
            verify=body.get("verify"), max_tokens=int(body.get("max_tokens", 8000)),
            title=body.get("title", ""), job_id=job_id,
            model=body.get("model"),
        )
        return jsonify({"job_id": job_id, "start_rung": start.rung,
                        "start_tier": start.name, "max_rung": ceiling}), 202

    @app.get("/api/job/<job_id>")
    def get_job(job_id: str):
        job = store.get_job(job_id)
        return (jsonify(job), 200) if job else (jsonify({"error": "not found"}), 404)

    @app.get("/api/jobs")
    def list_jobs():
        return jsonify(store.list_jobs(
            limit=int(request.args.get("limit", 100)),
            swarm_id=request.args.get("swarm_id"),
        ))

    # ---------------- swarm ----------------

    @app.post("/api/swarm")
    def create_swarm():
        body = request.get_json(force=True, silent=True) or {}
        raw = body.get("tasks") or []
        if not raw:
            return jsonify({"error": "tasks[] is required"}), 400

        defaults = {
            "kind": body.get("kind", "implement"),
            "max_rung": body.get("max_rung"),
            "system_extra": body.get("system_extra", ""),
            "verify": body.get("verify"),
            "max_tokens": int(body.get("max_tokens", 8000)),
        }
        tasks = [
            Task(
                prompt=t["prompt"],
                kind=t.get("kind", defaults["kind"]),
                title=t.get("title", ""),
                rung=t.get("rung"),
                tier_name=t.get("tier"),
                max_rung=t.get("max_rung", defaults["max_rung"]),
                system_extra=t.get("system_extra", defaults["system_extra"]),
                verify=t.get("verify", defaults["verify"]),
                max_tokens=int(t.get("max_tokens", defaults["max_tokens"])),
                model=t.get("model", body.get("model")),
            )
            for t in raw if t.get("prompt")
        ]
        swarm_id = uuid.uuid4().hex[:12]
        _spawn(swarm.run, tasks, swarm_id)
        return jsonify({"swarm_id": swarm_id, "queued": len(tasks)}), 202

    @app.get("/api/swarm/<swarm_id>")
    def get_swarm(swarm_id: str):
        jobs = store.list_jobs(limit=1000, swarm_id=swarm_id)
        done = [j for j in jobs if j["status"] in ("done", "failed")]
        return jsonify({
            "swarm_id": swarm_id,
            "total": len(jobs),
            "finished": len(done),
            "succeeded": sum(1 for j in jobs if j["status"] == "done"),
            "failed": sum(1 for j in jobs if j["status"] == "failed"),
            "cost_usd": sum(j["cost_usd"] or 0 for j in jobs),
            "jobs": jobs,
        })

    return app


def main() -> None:
    import argparse

    ap = argparse.ArgumentParser(description="Ladder orchestration server")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=5151)
    ap.add_argument("--db", default=None)
    ap.add_argument("--cwd", default=None,
                    help="working directory for tool-using CLI jobs")
    args = ap.parse_args()

    app = create_app(db_path=args.db, cwd=args.cwd)
    print(f"Ladder dashboard -> http://{args.host}:{args.port}")
    app.run(host=args.host, port=args.port, threaded=True)


if __name__ == "__main__":
    main()
