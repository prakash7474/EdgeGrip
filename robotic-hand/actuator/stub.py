"""Stub actuator: logs every command and acknowledges immediately.

It is the default actuator for tests and for a ``live_run.py`` plumbing smoke
test. It drives no hardware at all.
"""

from __future__ import annotations

import logging
import threading
import time

from actuator.base import ActuationResult, Actuator, new_command_id, utc_now_iso

LOG = logging.getLogger("actuator.stub")


class StubActuator(Actuator):
    """In-memory actuator that records calls instead of moving anything."""

    name = "stub"

    def __init__(self, logger: logging.Logger | None = None):
        self._log = logger or LOG
        self._lock = threading.Lock()
        self.calls: list[ActuationResult] = []

    def _record(self, action: str, parameters: dict[str, int],
                command_id: str | None) -> ActuationResult:
        sent_perf = time.perf_counter()
        sent_at = utc_now_iso()
        acked_perf = time.perf_counter()
        result = ActuationResult(
            command_id=command_id or new_command_id(),
            action=action,
            parameters=dict(parameters),
            acked=True,
            reason="ack",
            detail="stub",
            sent_at=sent_at,
            acked_at=utc_now_iso(),
            latency_ms=round((acked_perf - sent_perf) * 1000.0, 3),
            sent_perf=sent_perf,
            acked_perf=acked_perf,
        )
        with self._lock:
            self.calls.append(result)
        self._log.info("stub actuator %s parameters=%s command_id=%s",
                       action, parameters, result.command_id)
        return result

    def grip(self, *, speed: int | None = None,
             command_id: str | None = None) -> ActuationResult:
        parameters = {} if speed is None else {"speed": speed}
        return self._record("grip", parameters, command_id)

    def release(self, *, speed: int | None = None,
                command_id: str | None = None) -> ActuationResult:
        parameters = {} if speed is None else {"speed": speed}
        return self._record("release", parameters, command_id)

    def move_to(self, angle: int, *, speed: int | None = None,
                command_id: str | None = None) -> ActuationResult:
        parameters = {"angle": angle}
        if speed is not None:
            parameters["speed"] = speed
        return self._record("move_to", parameters, command_id)

    def stop(self, *, command_id: str | None = None) -> ActuationResult:
        return self._record("stop", {}, command_id)

    def heartbeat(self, *, command_id: str | None = None) -> ActuationResult:
        return self._record("heartbeat", {}, command_id)

    def snapshot(self) -> list[ActuationResult]:
        with self._lock:
            return list(self.calls)
