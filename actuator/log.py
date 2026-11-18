"""Durable ``actuation_log``: one row per task, before anything is sent.

``task_id`` is the primary key, so a task can cause at most one action even if
a delivery is repeated, two workers race, or the edge restarts. ``sent_at`` is
written before the command leaves the edge; ``acked_at`` is filled in only
after the device replies. A row with a command id and no ``acked_at`` is an
unconfirmed command and is reported, never retried automatically.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS actuation_log (
  task_id TEXT PRIMARY KEY,
  command_id TEXT,
  action TEXT NOT NULL,
  parameters TEXT,
  acked INTEGER NOT NULL DEFAULT 0,
  reason TEXT,
  detail TEXT,
  sent_at TEXT NOT NULL,
  acked_at TEXT,
  latency_ms REAL
);
CREATE INDEX IF NOT EXISTS idx_actuation_log_sent ON actuation_log(sent_at);
CREATE INDEX IF NOT EXISTS idx_actuation_log_command ON actuation_log(command_id);
"""


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class ActuationLog:
    """SQLite-backed actuation log, safe for concurrent writers."""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
        except Exception:
            pass
        return conn

    def begin(self, task_id: str, command_id: str | None, action: str,
              parameters: dict[str, int] | None = None,
              sent_at: str | None = None) -> bool:
        """Claim the task and record the send attempt.

        Returns False when a row for ``task_id`` already exists; in that case
        nothing must be sent.
        """
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO actuation_log "
                "(task_id, command_id, action, parameters, acked, reason, sent_at) "
                "VALUES (?, ?, ?, ?, 0, NULL, ?) "
                "ON CONFLICT(task_id) DO NOTHING",
                (
                    task_id,
                    command_id,
                    action,
                    json.dumps(parameters or {}, sort_keys=True),
                    sent_at or _now_iso(),
                ),
            )
            return cur.rowcount > 0

    def complete(self, task_id: str, *, acked: bool, reason: str = "",
                 detail: str = "", acked_at: str | None = None,
                 latency_ms: float | None = None) -> bool:
        """Record the device reply. The first completion wins."""
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE actuation_log SET acked=?, reason=?, detail=?, acked_at=?, latency_ms=? "
                "WHERE task_id=? AND acked_at IS NULL",
                (
                    1 if acked else 0,
                    str(reason or ""),
                    str(detail or ""),
                    acked_at,
                    latency_ms,
                    task_id,
                ),
            )
            return cur.rowcount > 0

    def get(self, task_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM actuation_log WHERE task_id=?", (task_id,)
            ).fetchone()
            return self._row_to_dict(row) if row else None

    def rows(self) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM actuation_log ORDER BY sent_at ASC"
            ).fetchall()
            return [self._row_to_dict(row) for row in rows]

    def counts(self) -> dict[str, int]:
        """Counts by outcome. ``holds`` sent nothing and are not ACKed."""
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT
                  COUNT(*) AS total,
                  SUM(CASE WHEN command_id IS NULL THEN 1 ELSE 0 END) AS holds,
                  SUM(CASE WHEN acked = 1 THEN 1 ELSE 0 END) AS acked,
                  SUM(CASE WHEN acked = 0 AND command_id IS NOT NULL
                            AND acked_at IS NOT NULL THEN 1 ELSE 0 END) AS nacked,
                  SUM(CASE WHEN acked = 0 AND command_id IS NOT NULL
                            AND acked_at IS NULL THEN 1 ELSE 0 END) AS unacked
                FROM actuation_log
                """
            ).fetchone()
        return {key: int(row[key] or 0) for key in
                ("total", "holds", "acked", "nacked", "unacked")}

    def total(self) -> int:
        return self.counts()["total"]

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> dict:
        data = dict(row)
        try:
            data["parameters"] = json.loads(data.get("parameters") or "{}")
        except json.JSONDecodeError:
            data["parameters"] = {}
        data["acked"] = bool(data.get("acked"))
        return data
