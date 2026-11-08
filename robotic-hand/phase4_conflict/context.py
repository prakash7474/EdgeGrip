"""Task context carried through outbox -> broker -> worker -> router -> provider -> result."""
from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any


@dataclass
class TaskContext:
    """Context for a single inference task.

    task_id is the Phase 2 message_id.
    """
    task_id: str
    attempt: int = 1
    frame_timestamp: str = ""
    deadline: float = 0.0  # epoch seconds

    def to_headers(self) -> dict[str, str]:
        return {
            "X-Task-Id": str(self.task_id),
            "X-Attempt": str(self.attempt),
            "X-Frame-Timestamp": str(self.frame_timestamp or ""),
            "X-Deadline": str(self.deadline or 0.0),
        }

    @classmethod
    def from_headers(cls, headers: dict[str, Any]) -> "TaskContext | None":
        # case-insensitive lookup helper
        def get(k: str) -> str | None:
            for hk, hv in headers.items():
                if hk.lower() == k.lower():
                    return str(hv)
            return None
        task_id = get("X-Task-Id")
        if not task_id:
            return None
        try:
            attempt = int(get("X-Attempt") or "1")
        except Exception:
            attempt = 1
        frame_ts = get("X-Frame-Timestamp") or ""
        try:
            deadline = float(get("X-Deadline") or "0")
        except Exception:
            deadline = 0.0
        return cls(task_id=task_id, attempt=attempt, frame_timestamp=frame_ts, deadline=deadline)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> "TaskContext | None":
        if not d:
            return None
        task_id = d.get("task_id") or d.get("message_id")
        if not task_id:
            return None
        return cls(
            task_id=str(task_id),
            attempt=int(d.get("attempt", 1) or 1),
            frame_timestamp=str(d.get("frame_timestamp", "") or ""),
            deadline=float(d.get("deadline", 0.0) or 0.0),
        )
