"""Where the Phase 4 decision gate meets the actuator.

``ActuationGrip.try_apply`` has the same signature as ``DecisionGate.try_apply``
(plus an optional ``score``), so it can be passed to the existing worker as its
``decision_gate`` without other changes.

Ordering is deliberate:

1. the Phase 4 gate decides whether this task may be applied at all;
2. the policy maps ``(label, score)`` to one action;
3. the actuation log claims ``task_id`` **before** the command is written;
4. the actuator is called once and the reply recorded.

If the edge dies between (3) and (4) the task is logged as an unconfirmed
command and is never retried by this process, so a task cannot be actuated
twice. The trade-off is fail-safe: a crash at that point causes no motion.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any

from actuator.base import (
    ActuationResult,
    Actuator,
    command_id_for_task,
    utc_now_iso,
)
from actuator.log import ActuationLog
from actuator.policy import Policy, PolicyAction


@dataclass
class GripOutcome:
    """What happened for one task, including the gate's verdict."""

    applied: bool
    reason: str
    task_id: str
    decision: str | None = None
    action: str | None = None
    command_id: str | None = None
    result: ActuationResult | None = None
    policy_match: str = "default"

    @property
    def acked(self) -> bool:
        return bool(self.result is not None and self.result.acked)


class ActuationGrip:
    """Decision gate wrapper that actuates at most once per task id."""

    def __init__(self, gate: Any, policy: Policy, actuator: Actuator,
                 log: ActuationLog):
        self.gate = gate
        self.policy = policy
        self.actuator = actuator
        self.log = log
        self._history: list[GripOutcome] = []
        self._lock = threading.Lock()

    # ------------------------------------------------------------- gate API
    def try_apply(self, task_id: str, decision: str, frame_timestamp: str | float = "",
                  deadline: float = 0.0, now: float | None = None,
                  score: float | None = None) -> GripOutcome:
        gate_result = self.gate.try_apply(
            task_id, decision, frame_timestamp, deadline, now=now
        )
        if not getattr(gate_result, "applied", False):
            return self._finish(
                GripOutcome(
                    applied=False,
                    reason=str(getattr(gate_result, "reason", "not_applied")),
                    task_id=task_id,
                    decision=getattr(gate_result, "decision", None),
                    action="hold",
                )
            )

        action = self.policy.decide(decision, score)
        if action.action == "hold":
            self.log.begin(task_id, None, action.action, {}, utc_now_iso())
            return self._finish(
                GripOutcome(
                    applied=True, reason="hold", task_id=task_id, decision=decision,
                    action="hold", policy_match=action.matched,
                )
            )

        command_id = command_id_for_task(task_id)
        if not self.log.begin(
            task_id, command_id, action.action, action.parameters, utc_now_iso()
        ):
            # Already claimed by another delivery or an earlier process. Never resend.
            return self._finish(
                GripOutcome(
                    applied=True, reason="action_already_logged", task_id=task_id,
                    decision=decision, action=action.action, command_id=command_id,
                    policy_match=action.matched,
                )
            )

        sent_at = utc_now_iso()
        try:
            result = self._invoke(action, command_id)
        except Exception as exc:  # noqa: BLE001 - report the class only
            result = ActuationResult(
                command_id=command_id, action=action.action,
                parameters=dict(action.parameters), acked=False,
                reason="transport_error", detail=exc.__class__.__name__,
                sent_at=sent_at, acked_at=None,
            )
            outcome_reason = "actuator_error"
        else:
            outcome_reason = "actuated" if result.acked else f"actuator_{result.reason}"

        self.log.complete(
            task_id, acked=result.acked, reason=result.reason,
            detail=result.detail, acked_at=result.acked_at,
            latency_ms=result.latency_ms,
        )
        return self._finish(
            GripOutcome(
                applied=True, reason=outcome_reason, task_id=task_id, decision=decision,
                action=action.action, command_id=command_id, result=result,
                policy_match=action.matched,
            )
        )

    def is_applied(self, task_id: str) -> bool:
        return bool(self.gate.is_applied(task_id))

    def applied_count(self) -> int:
        return int(self.gate.applied_count())

    def snapshot(self) -> list[GripOutcome]:
        with self._lock:
            return list(self._history)

    # --------------------------------------------------------------- internal
    def _invoke(self, action: PolicyAction, command_id: str) -> ActuationResult:
        parameters = dict(action.parameters)
        if action.action == "grip":
            return self.actuator.grip(speed=parameters.get("speed"), command_id=command_id)
        if action.action == "release":
            return self.actuator.release(speed=parameters.get("speed"), command_id=command_id)
        if action.action == "move_to":
            return self.actuator.move_to(
                parameters["angle"],
                speed=parameters.get("speed"),
                command_id=command_id,
            )
        if action.action == "stop":
            return self.actuator.stop(command_id=command_id)
        raise ValueError(f"policy action {action.action!r} cannot be actuated")

    def _finish(self, outcome: GripOutcome) -> GripOutcome:
        with self._lock:
            self._history.append(outcome)
        return outcome

    def __getattr__(self, name: str) -> Any:
        # Delegate everything else (applied(), applied_count(), ...) to the gate.
        return getattr(self.gate, name)
