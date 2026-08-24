"""SQLite job store.

This is the other half of "keep the orchestrator's context clean": every
prompt, every response, and every escalation step is written here, and the
caller gets back only a job id and a final answer. When you want the detail,
you open the dashboard instead of pasting a transcript into the conversation.
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
import uuid
from pathlib import Path

from . import tiers

DEFAULT_DB = Path(__file__).resolve().parent.parent / "ladder.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id            TEXT PRIMARY KEY,
    swarm_id      TEXT,
    kind          TEXT NOT NULL,
    title         TEXT NOT NULL,
    prompt        TEXT NOT NULL,
    system        TEXT,
    status        TEXT NOT NULL,          -- queued|running|done|failed
    start_rung    INTEGER NOT NULL,
    final_rung    INTEGER,
    max_rung      INTEGER NOT NULL,
    result        TEXT,
    error         TEXT,
    cost_usd      REAL DEFAULT 0,
    tokens_in     INTEGER DEFAULT 0,
    tokens_out    INTEGER DEFAULT 0,
    attempts      INTEGER DEFAULT 0,
    created_at    REAL NOT NULL,
    started_at    REAL,
    finished_at   REAL,
    cwd           TEXT
);

CREATE TABLE IF NOT EXISTS attempts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id      TEXT NOT NULL,
    n           INTEGER NOT NULL,
    rung        INTEGER NOT NULL,
    tier        TEXT NOT NULL,
    engine      TEXT NOT NULL,
    model       TEXT NOT NULL,
    ok          INTEGER NOT NULL,
    text        TEXT,
    error       TEXT,
    tokens_in   INTEGER DEFAULT 0,
    tokens_out  INTEGER DEFAULT 0,
    cache_read  INTEGER DEFAULT 0,
    cache_write INTEGER DEFAULT 0,
    cost_usd    REAL DEFAULT 0,
    latency_ms  INTEGER DEFAULT 0,
    stop_reason TEXT,
    created_at  REAL NOT NULL,
    FOREIGN KEY (job_id) REFERENCES jobs(id)
);

CREATE INDEX IF NOT EXISTS idx_jobs_status  ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_swarm   ON jobs(swarm_id);
CREATE INDEX IF NOT EXISTS idx_jobs_created ON jobs(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_att_job      ON attempts(job_id);
"""


# A job cannot legitimately still be running after this long: the local
# deadline caps at 7200s and even a full climb through every paid rung is far
# short of six hours. Anything older was orphaned by a process that died --
# every Claude Code restart kills the MCP server mid-flight -- and would
# otherwise sit in `running` forever, skewing every statistic that follows.
STALE_JOB_SECONDS = 6 * 3600


def current_user() -> str:
    """Best-effort identity, so team usage can be aggregated per person."""
    import getpass

    for source in (
        lambda: os.environ.get("LADDER_USER"),
        getpass.getuser,
        lambda: os.environ.get("USERNAME"),
        lambda: os.environ.get("USER"),
    ):
        try:
            value = source()
        except Exception:  # noqa: BLE001 - getpass raises on odd environments
            continue
        if value:
            return str(value)
    return "unknown"


class Store:
    def __init__(self, path: str | Path = DEFAULT_DB, reap: bool = True):
        self.path = str(path)
        self._lock = threading.Lock()
        # check_same_thread=False because the Flask server and the worker pool
        # share one connection, guarded by self._lock.
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()
        self._migrate()
        if reap:
            self.reap_stale()

    def _migrate(self) -> None:
        """Add columns introduced after the first release.

        Databases predate features. `CREATE TABLE IF NOT EXISTS` will not add a
        column to a table that already exists, so an older ladder.db would
        otherwise fail every query mentioning a new column.
        """
        with self._lock:
            have = {r["name"] for r in self._conn.execute("PRAGMA table_info(jobs)")}
            if "user" not in have:
                self._conn.execute("ALTER TABLE jobs ADD COLUMN user TEXT")
                self._conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_jobs_user ON jobs(user)"
                )
                self._conn.commit()

    def reap_stale(self, max_age_seconds: int = STALE_JOB_SECONDS) -> int:
        """Mark long-abandoned `running` jobs as failed. Returns how many."""
        cutoff = time.time() - max_age_seconds
        with self._lock:
            cur = self._conn.execute(
                """UPDATE jobs SET status='failed',
                          error='orphaned: the process running this job exited',
                          finished_at=?
                   WHERE status IN ('running','queued')
                     AND COALESCE(started_at, created_at) < ?""",
                (time.time(), cutoff),
            )
            self._conn.commit()
            return cur.rowcount

    def close(self) -> None:
        """Release the SQLite handle.

        Required on Windows, where an open connection keeps a lock on the file
        and blocks the directory from being removed.
        """
        with self._lock:
            self._conn.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _write(self, sql: str, params: tuple = ()) -> None:
        with self._lock:
            self._conn.execute(sql, params)
            self._conn.commit()

    def _rows(self, sql: str, params: tuple = ()) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    # ---------------- jobs ----------------

    def create_job(self, *, kind: str, title: str, prompt: str, system: str,
                   start_rung: int, max_rung: int, swarm_id: str | None = None,
                   cwd: str | None = None, user: str | None = None) -> str:
        job_id = uuid.uuid4().hex[:12]
        self._write(
            """INSERT INTO jobs (id, swarm_id, kind, title, prompt, system, status,
                                 start_rung, max_rung, created_at, cwd, user)
               VALUES (?,?,?,?,?,?,'queued',?,?,?,?,?)""",
            (job_id, swarm_id, kind, title, prompt, system,
             start_rung, max_rung, time.time(), cwd, user or current_user()),
        )
        return job_id

    def mark_running(self, job_id: str) -> None:
        self._write(
            "UPDATE jobs SET status='running', started_at=? WHERE id=?",
            (time.time(), job_id),
        )

    def finish_job(self, job_id: str, *, status: str, result: str = "",
                   error: str = "", final_rung: int | None = None) -> None:
        agg = self._rows(
            """SELECT COALESCE(SUM(cost_usd),0) c, COALESCE(SUM(tokens_in),0) ti,
                      COALESCE(SUM(tokens_out),0) to_, COUNT(*) n
               FROM attempts WHERE job_id=?""",
            (job_id,),
        )[0]
        self._write(
            """UPDATE jobs SET status=?, result=?, error=?, final_rung=?,
                               cost_usd=?, tokens_in=?, tokens_out=?,
                               attempts=?, finished_at=?
               WHERE id=?""",
            (status, result, error, final_rung, agg["c"], agg["ti"],
             agg["to_"], agg["n"], time.time(), job_id),
        )

    def add_attempt(self, job_id: str, n: int, tier, res) -> None:
        self._write(
            """INSERT INTO attempts (job_id, n, rung, tier, engine, model, ok, text,
                                     error, tokens_in, tokens_out, cache_read,
                                     cache_write, cost_usd, latency_ms, stop_reason,
                                     created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (job_id, n, tier.rung, tier.name, res.engine, res.model,
             1 if res.ok else 0, res.text, res.error, res.tokens_in,
             res.tokens_out, res.cache_read, res.cache_write, res.cost_usd,
             res.latency_ms, res.stop_reason, time.time()),
        )

    def get_job(self, job_id: str) -> dict | None:
        rows = self._rows("SELECT * FROM jobs WHERE id=?", (job_id,))
        if not rows:
            return None
        job = rows[0]
        job["attempt_log"] = self._rows(
            "SELECT * FROM attempts WHERE job_id=? ORDER BY n", (job_id,)
        )
        return job

    def list_jobs(self, limit: int = 100, swarm_id: str | None = None) -> list[dict]:
        if swarm_id:
            return self._rows(
                "SELECT * FROM jobs WHERE swarm_id=? ORDER BY created_at DESC LIMIT ?",
                (swarm_id, limit),
            )
        return self._rows(
            "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)
        )

    def report(self, days: int | None = None) -> dict:
        """Is this tool actually earning its keep?

        Built to be able to answer "no". Every number here is either measured
        or a clearly-labelled counterfactual, and the counterfactual is
        deliberately conservative:

        * **avoided_spend** prices work that finished free at rung 0 using
          rung 1 rates -- the cheapest paid tier that could have done it. It is
          not priced at the top rung, because nobody would have run a docstring
          through Fable. Pricing against the most expensive model is how tools
          like this flatter themselves.
        * **wasted_spend** is money spent on attempts that failed and had to
          escalate. Trying cheap and missing is not free, and a report that
          hides it is lying.
        * **local_hours** is wall-clock spent on the free tier. Free is not
          free if a person sat waiting for it, so the report converts the
          trade into an implied hourly rate and judges it.

        net_saving = avoided_spend - wasted_spend. It can be negative.
        """
        where, params = "", []
        if days:
            where = "WHERE created_at > ?"
            params = [time.time() - days * 86400]

        totals = self._rows(
            f"""SELECT COUNT(*) jobs, COALESCE(SUM(cost_usd),0) spend,
                       COALESCE(SUM(tokens_in),0) tin, COALESCE(SUM(tokens_out),0) tout
                FROM jobs {where}""", tuple(params))[0]

        att_where = where.replace("created_at", "a.created_at")
        # Work that actually completed on the free tier.
        free = self._rows(
            f"""SELECT COUNT(*) n, COALESCE(SUM(a.tokens_in),0) tin,
                       COALESCE(SUM(a.tokens_out),0) tout,
                       COALESCE(SUM(a.latency_ms),0) ms
                FROM attempts a {att_where}
                {"AND" if where else "WHERE"} a.rung = 0 AND a.ok = 1""",
            tuple(params))[0]

        # Money spent on attempts that did not produce the final answer.
        wasted = self._rows(
            f"""SELECT COALESCE(SUM(a.cost_usd),0) c, COUNT(*) n
                FROM attempts a {att_where}
                {"AND" if where else "WHERE"} a.ok = 0""", tuple(params))[0]

        haiku = tiers.by_rung(1)
        avoided = tiers.estimate_cost(haiku, free["tin"], free["tout"])
        net = avoided - wasted["c"]
        local_hours = free["ms"] / 3_600_000

        jobs_by_start = self._rows(
            f"""SELECT kind, COUNT(*) n,
                       SUM(CASE WHEN final_rung = start_rung THEN 1 ELSE 0 END) first_try,
                       COALESCE(SUM(cost_usd),0) cost
                FROM jobs {where} {"AND" if where else "WHERE"} status='done'
                GROUP BY kind ORDER BY n DESC""", tuple(params))
        for row in jobs_by_start:
            row["first_try_rate"] = row["first_try"] / row["n"] if row["n"] else 0.0

        by_user = self._rows(
            f"""SELECT COALESCE(user,'unknown') user, COUNT(*) jobs,
                       COALESCE(SUM(cost_usd),0) spend,
                       SUM(CASE WHEN final_rung = 0 THEN 1 ELSE 0 END) free_jobs
                FROM jobs {where} GROUP BY user ORDER BY jobs DESC""",
            tuple(params))

        done = self._rows(
            f"""SELECT COUNT(*) n FROM jobs {where}
                {"AND" if where else "WHERE"} status='done'""", tuple(params))[0]["n"]
        first_try = self._rows(
            f"""SELECT COUNT(*) n FROM jobs {where}
                {"AND" if where else "WHERE"} status='done'
                  AND final_rung = start_rung""", tuple(params))[0]["n"]

        return {
            "window_days": days,
            "jobs": totals["jobs"],
            "jobs_done": done,
            "actual_spend": totals["spend"],
            "free_attempts": free["n"],
            "free_tokens_in": free["tin"],
            "free_tokens_out": free["tout"],
            "avoided_spend": avoided,
            "wasted_spend": wasted["c"],
            "wasted_attempts": wasted["n"],
            "net_saving": net,
            "local_hours": local_hours,
            "implied_hourly_rate": (net / local_hours) if local_hours > 0.01 else None,
            "first_try_rate": (first_try / done) if done else 0.0,
            "by_kind": jobs_by_start,
            "by_user": by_user,
        }

    def stats(self) -> dict:
        totals = self._rows(
            """SELECT COUNT(*) jobs, COALESCE(SUM(cost_usd),0) cost,
                      COALESCE(SUM(tokens_in),0) tin, COALESCE(SUM(tokens_out),0) tout
               FROM jobs"""
        )[0]
        by_status = {r["status"]: r["n"] for r in self._rows(
            "SELECT status, COUNT(*) n FROM jobs GROUP BY status")}
        by_tier = self._rows(
            """SELECT tier, rung, COUNT(*) n, COALESCE(SUM(cost_usd),0) cost,
                      COALESCE(AVG(latency_ms),0) avg_ms
               FROM attempts GROUP BY tier, rung ORDER BY rung"""
        )
        # What the same work would have cost if every job had gone straight to
        # the top rung. This is the number that justifies the ladder.
        saved = self._rows(
            """SELECT COALESCE(SUM((tokens_in/1e6)*10.0 + (tokens_out/1e6)*50.0),0) s
               FROM attempts"""
        )[0]["s"]
        return {
            "totals": totals,
            "by_status": by_status,
            "by_tier": by_tier,
            "fable_equivalent_cost": saved,
            "savings_vs_fable": max(0.0, saved - totals["cost"]),
        }
