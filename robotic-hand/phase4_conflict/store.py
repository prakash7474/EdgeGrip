"""SQLite-backed conflict handling: results claims, discard log, decision gate."""
from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from phase4_conflict.context import TaskContext


RESULTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS results (
  task_id TEXT PRIMARY KEY,
  provider TEXT NOT NULL,
  decision TEXT NOT NULL,
  claimed_at TEXT NOT NULL
);
"""

DISCARDED_SCHEMA = """
CREATE TABLE IF NOT EXISTS discarded (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id TEXT NOT NULL,
  provider TEXT,
  reason TEXT NOT NULL CHECK(reason IN ('duplicate','late','mismatch')),
  received_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_discarded_task ON discarded(task_id);
"""

APPLIED_SCHEMA = """
CREATE TABLE IF NOT EXISTS applied_ids (
  task_id TEXT PRIMARY KEY,
  frame_timestamp TEXT,
  frame_ts_num REAL,
  decision TEXT,
  applied_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_applied_frame ON applied_ids(frame_ts_num);
"""


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _parse_frame_ts(frame_timestamp: str | float | int | None) -> float:
    if frame_timestamp is None:
        return 0.0
    if isinstance(frame_timestamp, (int, float)):
        return float(frame_timestamp)
    s = str(frame_timestamp).strip()
    if not s:
        return 0.0
    try:
        return float(s)
    except Exception:
        pass
    # try ISO-ish simple parse fallback
    try:
        # minimal: not fully robust, but good enough
        from datetime import datetime
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt.timestamp()
    except Exception:
        return 0.0


class ConflictStore:
    """Stores results claims and discarded responses. Safe under concurrent writers."""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        # WAL helps concurrent writers; safe to ignore if unsupported
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
        except Exception:
            pass
        return conn

    def _init(self) -> None:
        with self._connect() as conn:
            conn.executescript(RESULTS_SCHEMA)
            conn.executescript(DISCARDED_SCHEMA)
            conn.executescript(APPLIED_SCHEMA)

    def claim_result(self, task_id: str, provider: str, decision: str) -> bool:
        """First writer wins. Returns True if this call claimed the task."""
        now = _now_iso()
        with self._connect() as conn:
            # Use explicit transaction for clarity; autocommit off via isolation_level tweak isn't needed
            cur = conn.execute(
                "INSERT INTO results (task_id, provider, decision, claimed_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(task_id) DO NOTHING",
                (task_id, provider, decision, now),
            )
            return cur.rowcount > 0

    def get_result(self, task_id: str) -> Optional[dict]:
        with self._connect() as conn:
            cur = conn.execute("SELECT * FROM results WHERE task_id=?", (task_id,))
            row = cur.fetchone()
            return dict(row) if row else None

    def record_discard(self, task_id: str, provider: str | None, reason: str) -> None:
        if reason not in ("duplicate", "late", "mismatch"):
            raise ValueError(f"invalid reason: {reason}")
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO discarded (task_id, provider, reason, received_at) VALUES (?, ?, ?, ?)",
                (task_id, provider, reason, _now_iso()),
            )

    def discard_counts(self) -> dict[str, int]:
        with self._connect() as conn:
            cur = conn.execute("SELECT reason, COUNT(*) as c FROM discarded GROUP BY reason")
            return {r["reason"]: r["c"] for r in cur.fetchall()}

    def total_results(self) -> int:
        with self._connect() as conn:
            cur = conn.execute("SELECT COUNT(*) FROM results")
            return int(cur.fetchone()[0])

    def close(self) -> None:
        pass


@dataclass
class DecisionResult:
    applied: bool
    reason: str  # applied | already_applied | deadline_passed | newer_frame_decided | late
    decision: str | None = None


class DecisionGate:
    """Robot-side decision gate with applied_ids table. Survives restarts."""

    def __init__(self, db_path: str | Path):
        self.store = ConflictStore(db_path)
        self.db_path = self.store.db_path

    def _connect(self):
        return self.store._connect()

    def try_apply(self, task_id: str, decision: str, frame_timestamp: str | float = "",
                  deadline: float = 0.0, now: float | None = None) -> DecisionResult:
        """Apply only if not already applied, deadline not passed, and no newer frame already decided."""
        if now is None:
            now = time.time()
        # deadline check
        if deadline and now > deadline:
            self.store.record_discard(task_id, None, "late")
            return DecisionResult(applied=False, reason="deadline_passed", decision="safe_default")
        ts_num = _parse_frame_ts(frame_timestamp)
        with self._connect() as conn:
            # Use BEGIN IMMEDIATE for write safety
            try:
                conn.execute("BEGIN IMMEDIATE")
            except Exception:
                pass
            try:
                # already applied?
                cur = conn.execute("SELECT 1 FROM applied_ids WHERE task_id=?", (task_id,))
                if cur.fetchone():
                    conn.execute("COMMIT")
                    return DecisionResult(applied=False, reason="already_applied")
                # newer frame already decided?
                cur = conn.execute("SELECT 1 FROM applied_ids WHERE frame_ts_num > ? LIMIT 1", (ts_num,))
                if cur.fetchone():
                    conn.execute("COMMIT")
                    # not applied due to newer; record as discard? spec says "apply only if..." - late/stale in effect
                    self.store.record_discard(task_id, None, "late")
                    return DecisionResult(applied=False, reason="newer_frame_decided")
                # insert
                conn.execute(
                    "INSERT INTO applied_ids (task_id, frame_timestamp, frame_ts_num, decision, applied_at) VALUES (?,?,?,?,?)",
                    (task_id, str(frame_timestamp or ""), ts_num, decision, _now_iso()),
                )
                conn.execute("COMMIT")
                return DecisionResult(applied=True, reason="applied", decision=decision)
            except sqlite3.IntegrityError:
                try:
                    conn.execute("ROLLBACK")
                except Exception:
                    pass
                return DecisionResult(applied=False, reason="already_applied")
            except Exception:
                try:
                    conn.execute("ROLLBACK")
                except Exception:
                    pass
                raise

    def is_applied(self, task_id: str) -> bool:
        with self._connect() as conn:
            cur = conn.execute("SELECT 1 FROM applied_ids WHERE task_id=?", (task_id,))
            return cur.fetchone() is not None

    def applied_count(self) -> int:
        with self._connect() as conn:
            cur = conn.execute("SELECT COUNT(*) FROM applied_ids")
            return int(cur.fetchone()[0])
