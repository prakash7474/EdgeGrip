"""Actuator interface shared by stub, simulated and serial devices.

Every method returns an :class:`ActuationResult`. A device ACK means "command
accepted and applied"; for a move it does not mean the motion has finished.
That distinction matters for the capture-to-ACK latency recorded by
``live_run.py``.
"""

from __future__ import annotations

import abc
import hashlib
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone


def utc_now_iso() -> str:
    """UTC timestamp with millisecond precision."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def new_command_id() -> str:
    """Random command id: ``cmd-`` plus 16 hex characters (20 total)."""
    return "cmd-" + uuid.uuid4().hex[:16]


def command_id_for_task(task_id: str) -> str:
    """Derive a stable command id from a task id.

    Deterministic ids make retries idempotent: if the edge restarts after
    sending a command but before recording the ACK, resending the same task
    produces the same command id and the device ignores the duplicate.
    """
    digest = hashlib.sha256(str(task_id).encode("utf-8")).hexdigest()[:16]
    return f"cmd-{digest}"


@dataclass(frozen=True)
class ActuationResult:
    """Outcome of one command, whether it reached the device or not."""

    command_id: str
    action: str
    parameters: dict[str, int] = field(default_factory=dict)
    acked: bool = False
    reason: str = "ack"
    detail: str = ""
    sent_at: str = ""
    acked_at: str | None = None
    latency_ms: float | None = None
    sent_perf: float | None = None
    acked_perf: float | None = None

    @property
    def ok(self) -> bool:
        """True when the device acknowledged the command."""
        return self.acked


class Actuator(abc.ABC):
    """Interface implemented by :class:`StubActuator`, ``SimActuator`` and
    ``SerialActuator``."""

    name: str = "actuator"

    @abc.abstractmethod
    def grip(self, *, speed: int | None = None, command_id: str | None = None) -> ActuationResult:
        """Close the gripper."""

    @abc.abstractmethod
    def release(self, *, speed: int | None = None, command_id: str | None = None) -> ActuationResult:
        """Open the gripper."""

    @abc.abstractmethod
    def move_to(self, angle: int, *, speed: int | None = None,
                command_id: str | None = None) -> ActuationResult:
        """Rotate the wrist joint to ``angle`` degrees."""

    @abc.abstractmethod
    def stop(self, *, command_id: str | None = None) -> ActuationResult:
        """Stop motion, holding the current position."""

    @abc.abstractmethod
    def heartbeat(self, *, command_id: str | None = None) -> ActuationResult:
        """Refresh the device watchdog."""

    def close(self) -> None:
        """Release resources. Safe to call more than once."""

    def __enter__(self) -> "Actuator":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()
