"""SQLite outbox for the edge publisher.

Messages are written as 'pending' BEFORE any send attempt. State transitions:
- pending -> sent (after publish attempt) / stays pending until ack
- pending/sent/unacked -> acked when ACK received for message_id
- resend logic uses retry_count and next_retry_at with exponential backoff
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from phase2_reliability.common import Message, new_message_id, now_iso


SCHEMA = """
CREATE TABLE IF NOT EXISTS outbox (
  message_id TEXT PRIMARY KEY,
  created_at TEXT NOT NULL,
  payload_b64 TEXT NOT NULL,
  state TEXT NOT NULL CHECK(state IN ('pending','sent','acked','failed')),
  retry_count INTEGER NOT NULL DEFAULT 0,
  next_retry_at REAL NOT NULL DEFAULT 0,
  last_attempt_at REAL,
  acked_at TEXT,
  published_at TEXT,
  attempt INTEGER NOT NULL DEFAULT 1,
  frame_timestamp TEXT NOT NULL DEFAULT '',
  deadline REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_outbox_state_next ON outbox(state, next_retry_at);
"""


@dataclass
class OutboxEntry:
    message_id: str
    created_at: str
    payload_b64: str
    state: str
    retry_count: int
    next_retry_at: float
    attempt: int = 1
    frame_timestamp: str = ""
    deadline: float = 0.0


class Outbox:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(SCHEMA)
            # migrate existing dbs
            try:
                cols = {r[1] for r in conn.execute("PRAGMA table_info(outbox)").fetchall()}
                for col, defn in [("attempt", "INTEGER NOT NULL DEFAULT 1"), ("frame_timestamp", "TEXT NOT NULL DEFAULT ''"), ("deadline", "REAL NOT NULL DEFAULT 0")]:
                    if col not in cols:
                        conn.execute(f"ALTER TABLE outbox ADD COLUMN {col} {defn}")
            except Exception:
                pass
            conn.commit()

    def enqueue(self, jpeg_bytes: bytes, message_id: str | None = None, created_at: str | None = None,
                 attempt: int = 1, frame_timestamp: str = "", deadline: float = 0.0) -> Message:
        from phase2_reliability.common import jpeg_to_base64

        msg_id = message_id or new_message_id()
        created = created_at or now_iso()
        payload_b64 = jpeg_to_base64(jpeg_bytes)
        now = time.time()
        with self._connect() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO outbox
                   (message_id, created_at, payload_b64, state, retry_count, next_retry_at, attempt, frame_timestamp, deadline)
                   VALUES (?, ?, ?, 'pending', 0, ?, ?, ?, ?)""",
                (msg_id, created, payload_b64, now, int(attempt), str(frame_timestamp or ""), float(deadline or 0.0)),
            )
            conn.commit()
        return Message(message_id=msg_id, created_at=created, payload=payload_b64,
                       attempt=int(attempt), frame_timestamp=str(frame_timestamp or ""), deadline=float(deadline or 0.0))

    def mark_sent(self, message_id: str) -> None:
        now = time.time()
        with self._connect() as conn:
            conn.execute(
                "UPDATE outbox SET state='sent', published_at=?, last_attempt_at=? WHERE message_id=? AND state!='acked'",
                (now_iso(), now, message_id),
            )
            conn.commit()

    def mark_acked(self, message_id: str) -> bool:
        now = now_iso()
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE outbox SET state='acked', acked_at=? WHERE message_id=? AND state!='acked'",
                (now, message_id),
            )
            conn.commit()
            return cur.rowcount > 0

    def get_unacked_for_resend(self, now: float | None = None) -> list[OutboxEntry]:
        if now is None:
            now = time.time()
        with self._connect() as conn:
            cur = conn.execute(
                """SELECT message_id, created_at, payload_b64, state, retry_count, next_retry_at,
                          COALESCE(attempt,1) as attempt, COALESCE(frame_timestamp,'') as frame_timestamp, COALESCE(deadline,0) as deadline
                   FROM outbox
                   WHERE state IN ('pending','sent') AND next_retry_at <= ?
                   ORDER BY created_at ASC, next_retry_at ASC""",
                (now,),
            )
            rows = cur.fetchall()
        return [
            OutboxEntry(
                message_id=r["message_id"],
                created_at=r["created_at"],
                payload_b64=r["payload_b64"],
                state=r["state"],
                retry_count=r["retry_count"],
                next_retry_at=r["next_retry_at"],
                attempt=int(r["attempt"] or 1),
                frame_timestamp=str(r["frame_timestamp"] or ""),
                deadline=float(r["deadline"] or 0.0),
            )
            for r in rows
        ]

    def schedule_retry(self, message_id: str, retry_count: int, backoff_sec: float) -> None:
        next_time = time.time() + backoff_sec
        with self._connect() as conn:
            conn.execute(
                "UPDATE outbox SET retry_count=?, next_retry_at=?, last_attempt_at=? WHERE message_id=?",
                (retry_count + 1, next_time, time.time(), message_id),
            )
            conn.commit()

    def count_by_state(self) -> dict[str, int]:
        with self._connect() as conn:
            cur = conn.execute("SELECT state, COUNT(*) as c FROM outbox GROUP BY state")
            return {r["state"]: r["c"] for r in cur.fetchall()}
