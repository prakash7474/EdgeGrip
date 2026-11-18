"""Capture-to-actuator-ACK join used by ``live_run.py`` (Phase 5b req 7).

The full live run needs an MQTT broker, so this checks the measurement join
itself with explicit perf_counter values: ACKed commands count, holds and
NACKs do not, and an ACK with no capture row is reported rather than guessed.
"""

from __future__ import annotations

import csv

from actuator import ActuationResult, GripOutcome
from live_run import capture_to_actuator_ack_ms, write_actuation_csv


def outcome(task_id: str, *, acked: bool, acked_perf: float | None) -> GripOutcome:
    result = ActuationResult(
        command_id=f"cmd-{task_id}",
        action="grip",
        acked=acked,
        acked_perf=acked_perf,
        acked_at="2026-11-15T00:00:00.001Z" if acked_perf is not None else None,
    )
    return GripOutcome(applied=True, reason="actuated", task_id=task_id, result=result)


def test_capture_to_ack_join_counts_only_acked_commands():
    outcomes = [
        outcome("t1", acked=True, acked_perf=10.120),
        outcome("t2", acked=True, acked_perf=11.050),
        outcome("t3", acked=False, acked_perf=12.000),   # NACK: no ACK
        GripOutcome(applied=True, reason="hold", task_id="t4", action="hold"),
        outcome("t-missing", acked=True, acked_perf=13.000),
    ]
    captures = {
        "t1": {"perf": 10.000},
        "t2": {"perf": 11.000},
        "t3": {"perf": 11.500},
        "t4": {"perf": 11.900},
    }

    latencies, unjoined = capture_to_actuator_ack_ms(outcomes, captures)
    assert latencies == [120.0, 50.0]
    assert unjoined == 1


def test_capture_to_ack_join_with_no_actuation_is_empty():
    latencies, unjoined = capture_to_actuator_ack_ms([], {})
    assert latencies == [] and unjoined == 0


def test_actuation_csv_records_ack_latency_and_keeps_holds(tmp_path):
    rows = [
        {
            "task_id": "t1", "command_id": "cmd-1", "action": "grip",
            "parameters": {"speed": 20}, "captured_at": "", "sent_at": "s1",
            "acked_at": "a1", "acked": True, "reason": "ack", "detail": "",
        },
        {
            "task_id": "t2", "command_id": None, "action": "hold",
            "parameters": {}, "captured_at": "", "sent_at": "s2",
            "acked_at": None, "acked": False, "reason": None, "detail": None,
        },
    ]
    captures = {
        "t1": {"perf": 100.0, "captured_at": "c1"},
        "t2": {"perf": 101.0, "captured_at": "c2"},
    }
    outcome = GripOutcome(
        applied=True, reason="actuated", task_id="t1",
        result=ActuationResult(command_id="cmd-1", action="grip", acked=True,
                               acked_perf=100.250),
    )
    path = tmp_path / "actuation.csv"
    write_actuation_csv(path, rows, [outcome], captures)

    with path.open(newline="", encoding="utf-8") as handle:
        written = list(csv.DictReader(handle))
    assert len(written) == 2
    assert written[0]["capture_to_ack_ms"] == "250.000"
    assert written[0]["captured_at_utc"] == "c1"
    assert written[1]["action"] == "hold"
    assert written[1]["capture_to_ack_ms"] == ""
