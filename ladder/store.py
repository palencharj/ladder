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
    batch_size  INTEGER DEFAULT 1,
    created_at  REAL NOT NULL,
    FOREIGN KEY (job_id) REFERENCES jobs(id)
);

CREATE INDEX IF NOT EXISTS idx_jobs_status  ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_swarm   ON jobs(swarm_id);
CREATE INDEX IF NOT EXISTS idx_jobs_created ON jobs(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_att_job      ON attempts(job_id);

-- One row per speculative draft: what was asked, what the free model wrote,
-- whether the paid tier accepted it, and what was finally returned.
--
-- This is the telemetry the acceptance rate is computed from. It is also,
-- deliberately, a training corpus that costs nothing to collect: accepted rows
-- are positive examples of work the local model can already do, and rejected
-- rows pair a bad answer with the corrected one. A draft model fine-tuned on
-- these raises the acceptance rate, which is the single number that decides
-- how much of the work stays free.
CREATE TABLE IF NOT EXISTS speculations (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    swarm_id     TEXT,
    draft_job    TEXT,
    repair_job   TEXT,
    kind         TEXT NOT NULL,
    prompt       TEXT NOT NULL,
    draft        TEXT,
    accepted     INTEGER NOT NULL,
    reason       TEXT,
    final        TEXT,
    verify_rung  INTEGER,
    draft_model  TEXT,
    draft_tokens INTEGER DEFAULT 0,
    paid_tokens  INTEGER DEFAULT 0,
    user         TEXT,
    created_at   REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_spec_kind    ON speculations(kind);
CREATE INDEX IF NOT EXISTS idx_spec_swarm   ON speculations(swarm_id);
CREATE INDEX IF NOT EXISTS idx_spec_created ON speculations(created_at DESC);
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
            att = {r["name"] for r in self._conn.execute("PRAGMA table_info(attempts)")}
            if "batch_size" not in att:
                self._conn.execute(
                    "ALTER TABLE attempts ADD COLUMN batch_size INTEGER DEFAULT 1")
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
                                     batch_size, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (job_id, n, tier.rung, tier.name, res.engine, res.model,
             1 if res.ok else 0, res.text, res.error, res.tokens_in,
             res.tokens_out, res.cache_read, res.cache_write, res.cost_usd,
             res.latency_ms, res.stop_reason,
             int((res.raw or {}).get("batched", 1) or 1), time.time()),
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

    def record_speculation(self, *, kind: str, prompt: str, draft: str,
                           accepted: bool, reason: str = "", final: str = "",
                           verify_rung: int = 1, swarm_id: str | None = None,
                           draft_job: str | None = None,
                           repair_job: str | None = None,
                           draft_model: str = "", draft_tokens: int = 0,
                           paid_tokens: int = 0) -> None:
        """Record one draft-and-verdict, whichever way it went.

        Rejections are as valuable as acceptances here -- more so, arguably. An
        accepted draft says the local model can already do this kind of work; a
        rejected one, paired with the correction, says exactly how it falls
        short. Both are needed to tell whether a task kind belongs at rung 0,
        and both are training data if a fine-tuned draft model is ever built.
        """
        self._write(
            """INSERT INTO speculations (swarm_id, draft_job, repair_job, kind,
                                         prompt, draft, accepted, reason, final,
                                         verify_rung, draft_model, draft_tokens,
                                         paid_tokens, user, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (swarm_id, draft_job, repair_job, kind, prompt, draft,
             1 if accepted else 0, reason, final, verify_rung, draft_model,
             draft_tokens, paid_tokens, current_user(), time.time()),
        )

    def speculation_report(self, days: int | None = None) -> dict:
        """Acceptance rate and local generation share, overall and per kind.

        Two different questions, and conflating them hides the interesting
        case. Acceptance counts *tasks* the local model got right. Local share
        counts *tokens* it generated. A run where rung 0 answered nine one-line
        questions and the paid tier wrote one long file has 90% acceptance and
        might have 20% local share -- and the token figure is the one that
        maps onto the rate limit.
        """
        where, params = "", []
        if days:
            where = "WHERE created_at >= ?"
            params.append(time.time() - days * 86400)

        # local/paid chars measure the DELIVERED answer by author, using the
        # same yardstick on both sides. An accepted row was written by the
        # local model; a rejected one was rewritten by the paid tier, so its
        # draft was discarded and does not count as local output.
        rows = self._rows(
            f"""SELECT kind,
                       COUNT(*)                AS n,
                       SUM(accepted)           AS accepted,
                       SUM(draft_tokens)       AS draft_tokens,
                       SUM(paid_tokens)        AS paid_tokens,
                       SUM(CASE WHEN accepted = 1
                                THEN LENGTH(COALESCE(draft, '')) ELSE 0 END)
                                               AS local_chars,
                       SUM(CASE WHEN accepted = 0
                                THEN LENGTH(COALESCE(final, '')) ELSE 0 END)
                                               AS paid_chars
                FROM speculations {where}
                GROUP BY kind ORDER BY n DESC""",
            tuple(params),
        )
        total = {"n": 0, "accepted": 0, "draft_tokens": 0, "paid_tokens": 0,
                 "local_chars": 0, "paid_chars": 0}
        by_kind = []
        for r in rows:
            for k in total:
                total[k] += r[k] or 0
            n, acc = r["n"] or 0, r["accepted"] or 0
            local_ch, paid_ch = r["local_chars"] or 0, r["paid_chars"] or 0
            by_kind.append({
                "kind": r["kind"],
                "n": n,
                "accepted": acc,
                "acceptance": (acc / n) if n else 0.0,
                "draft_tokens": r["draft_tokens"] or 0,
                "paid_tokens": r["paid_tokens"] or 0,
                "local_chars": local_ch,
                "paid_chars": paid_ch,
                "local_share": (local_ch / (local_ch + paid_ch))
                               if (local_ch + paid_ch) else 0.0,
            })

        written = total["local_chars"] + total["paid_chars"]
        return {
            "total": total["n"],
            "accepted": total["accepted"],
            "acceptance": (total["accepted"] / total["n"]) if total["n"] else 0.0,
            "local_tokens": total["draft_tokens"],
            "paid_tokens": total["paid_tokens"],
            "local_chars": total["local_chars"],
            "paid_chars": total["paid_chars"],
            "local_share": (total["local_chars"] / written) if written else 0.0,
            "by_kind": by_kind,
        }

    def training_pairs(self, kind: str | None = None,
                       limit: int = 500) -> list[dict]:
        """Rejected drafts paired with the answer that replaced them.

        The corpus a fine-tuned draft model would learn from, in the form it
        would learn from: the task, what the small model said, and what the
        larger one said instead. Collected as a side effect of ordinary use --
        no labelling pass, no separate data-gathering project.
        """
        clause = "WHERE accepted = 0 AND final != '' AND final IS NOT NULL"
        params: list = []
        if kind:
            clause += " AND kind = ?"
            params.append(kind)
        params.append(limit)
        return self._rows(
            f"""SELECT kind, prompt, draft, final, reason, created_at
                FROM speculations {clause}
                ORDER BY created_at DESC LIMIT ?""",
            tuple(params),
        )

    def report(self, days: int | None = None) -> dict:
        """Is this tool earning its keep? Measured in quota, not dollars.

        On a prepaid Claude Code plan the scarce resource is subscription
        allowance, not money, so this reports in the units that actually bind:
        **requests deflected** and **tokens deflected**.

        The unit that matters is the invocation. Every `claude -p` call spends
        ~35k tokens of harness overhead before it does any work, charged per
        call no matter how small the task. So a request the local tier absorbs
        does not save a fraction of a cent -- it saves an entire ~35k-token hit
        against the allowance.

        * **requests_deflected** -- jobs that finished at rung 0 and never
          invoked the CLI at all. Countable and unambiguous; this is the
          headline.
        * **tokens_deflected** -- what those requests would have spent, being
          the per-call overhead plus the tokens the work itself consumed. An
          estimate, and stated as one.
        * **quota_multiplier** -- total requests served divided by requests
          that actually spent allowance. 2.0 means the plan went twice as far.
        * **seconds_per_deflection** -- the honest price. Local compute is free
          in quota and expensive in wall clock, and this is the exchange rate.

        Dollar figures are still recorded, but they are notional on a
        subscription: what the same work would have cost at API rates, not a
        bill anyone receives.
        """
        where, params = "", []
        if days:
            where = "WHERE created_at > ?"
            params = [time.time() - days * 86400]
        and_or_where = "AND" if where else "WHERE"

        totals = self._rows(
            f"""SELECT COUNT(*) jobs, COALESCE(SUM(cost_usd),0) spend
                FROM jobs {where}""", tuple(params))[0]
        done = self._rows(
            f"""SELECT COUNT(*) n FROM jobs {where} {and_or_where} status='done'""",
            tuple(params))[0]["n"]

        att_where = where.replace("created_at", "a.created_at")

        # Work that completed locally: never touched the subscription.
        local = self._rows(
            f"""SELECT COUNT(*) n, COALESCE(SUM(a.tokens_in),0) tin,
                       COALESCE(SUM(a.tokens_out),0) tout,
                       COALESCE(SUM(a.latency_ms),0) ms
                FROM attempts a {att_where}
                {and_or_where} a.engine = 'ollama' AND a.ok = 1""",
            tuple(params))[0]

        # Attempts that actually spent allowance, and what they cost in tokens.
        # One batched invocation answers N tasks and is recorded as N attempts,
        # so counting rows would overstate invocations -- the very thing
        # batching exists to reduce. Summing 1/batch_size recovers the true
        # number of `claude -p` calls.
        paid = self._rows(
            f"""SELECT COALESCE(SUM(1.0 / MAX(a.batch_size, 1)),0) calls,
                       COUNT(*) attempts,
                       COALESCE(SUM(a.tokens_in + a.tokens_out
                                    + a.cache_read + a.cache_write),0) tok,
                       COALESCE(SUM(CASE WHEN a.batch_size > 1 THEN 1 ELSE 0 END),0) batched
                FROM attempts a {att_where}
                {and_or_where} a.engine != 'ollama'""", tuple(params))[0]

        # Local attempts that failed and had to escalate: wall clock spent for
        # nothing, and the request still cost allowance in the end.
        wasted_local = self._rows(
            f"""SELECT COUNT(*) n, COALESCE(SUM(a.latency_ms),0) ms
                FROM attempts a {att_where}
                {and_or_where} a.engine = 'ollama' AND a.ok = 0""",
            tuple(params))[0]

        jobs_local = self._rows(
            f"""SELECT COUNT(*) n FROM jobs {where}
                {and_or_where} status='done' AND final_rung = 0""",
            tuple(params))[0]["n"]

        deflected_tokens = jobs_local * tiers.CLI_OVERHEAD_TOKENS +             local["tin"] + local["tout"]
        served = done
        spent_requests = max(0, served - jobs_local)

        by_kind = self._rows(
            f"""SELECT kind, COUNT(*) n,
                       SUM(CASE WHEN final_rung = 0 THEN 1 ELSE 0 END) local_n,
                       SUM(CASE WHEN final_rung = start_rung THEN 1 ELSE 0 END) first_try
                FROM jobs {where} {and_or_where} status='done'
                GROUP BY kind ORDER BY n DESC""", tuple(params))
        for row in by_kind:
            row["deflection_rate"] = row["local_n"] / row["n"] if row["n"] else 0.0
            row["first_try_rate"] = row["first_try"] / row["n"] if row["n"] else 0.0

        by_user = self._rows(
            f"""SELECT COALESCE(user,'unknown') user, COUNT(*) jobs,
                       SUM(CASE WHEN final_rung = 0 THEN 1 ELSE 0 END) local_jobs,
                       COALESCE(SUM(cost_usd),0) spend
                FROM jobs {where} GROUP BY user ORDER BY jobs DESC""",
            tuple(params))
        for row in by_user:
            row["deflection_rate"] = (
                row["local_jobs"] / row["jobs"] if row["jobs"] else 0.0)

        local_hours = local["ms"] / 3_600_000
        return {
            "window_days": days,
            "jobs": totals["jobs"],
            "jobs_done": done,
            # --- the metrics that matter on a subscription ---
            "requests_deflected": jobs_local,
            "requests_spent": spent_requests,
            "deflection_rate": (jobs_local / served) if served else 0.0,
            "tokens_deflected": deflected_tokens,
            "tokens_spent": paid["tok"],
            "cli_calls": round(paid["calls"]),
            "paid_attempts": paid["attempts"],
            "batched_attempts": paid["batched"],
            "batch_savings_tokens": max(
                0, (paid["attempts"] - round(paid["calls"]))
                * tiers.CLI_OVERHEAD_TOKENS),
            "quota_multiplier": (served / spent_requests) if spent_requests else None,
            # --- the price paid for it ---
            "local_hours": local_hours,
            "seconds_per_deflection": (
                (local["ms"] / 1000) / jobs_local if jobs_local else None),
            "wasted_local_attempts": wasted_local["n"],
            "wasted_local_hours": wasted_local["ms"] / 3_600_000,
            # --- notional, on a subscription ---
            "notional_spend_usd": totals["spend"],
            "by_kind": by_kind,
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
