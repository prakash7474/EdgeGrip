"""TC5b.1-TC5b.8: actuator, policy, gate wiring and device protocol.

Spec-table mapping
------------------
The literal Phase 5b table was not available in this repository, so each test
is mapped to the numbered requirement it implements:

TC5b.1 -> req 1  Actuator interface (grip, release, move_to, stop, heartbeat)
                  and StubActuator logging.
TC5b.2 -> req 2  Config-driven policy from (label, score) to action, with the
                  default "hold".
TC5b.3 -> req 3  Phase 4 gate wired to the actuator: one action per task_id,
                  actuation_log(task_id, command_id, action, sent_at, acked_at).
TC5b.4 -> req 1  SerialActuator against a real device. Hardware only: skipped
                  unless a port is supplied.
TC5b.5 -> req 5  Firmware rules on a real board. Hardware only and motion-free:
                  skipped unless explicitly enabled.
TC5b.6 -> req 4  Frame checksum/format validation, ACK/NACK reasons.
TC5b.7 -> req 5  Last 32 command ids are remembered; duplicates are ACKed but
                  not executed.
TC5b.8 -> req 5  Angle/speed limits, boot-safe state and heartbeat timeout.

TC5b.6-TC5b.8 run against ``FakeDevice``, which implements the same protocol in
Python. No test in this file moves real hardware by default.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import threading
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from actuator import (
    ActuationGrip,
    ActuationLog,
    Actuator,
    FakeDevice,
    ManualClock,
    Policy,
    SerialActuator,
    SimActuator,
    StubActuator,
    command_id_for_task,
    format_frame,
    load_policy,
    parse_frame,
)
from actuator.protocol import ProtocolError, checksum_for
from phase1_pipeline.capture import load_sample_frame
from phase2_reliability.common import Message
from phase2_reliability.config import load_settings
from phase2_reliability.worker import Worker
from phase3_router import HTTPProvider, Router
from phase4_conflict import ConflictStore, DecisionGate

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
IMAGES_DIR = PROJECT_ROOT / "images"

HARDWARE_PORT = os.getenv("ACTUATION_SERIAL_PORT", "").strip()
HARDWARE_ENABLED = os.getenv("ACTUATION_HARDWARE_TEST", "") == "1"
FIRMWARE_ENABLED = os.getenv("ACTUATION_FIRMWARE_TEST", "") == "1"
HARDWARE_REASON = (
    "needs an ESP32/Arduino running firmware/robotic_hand with the servo rail "
    "powered; set ACTUATION_SERIAL_PORT and ACTUATION_HARDWARE_TEST=1"
)


# --------------------------------------------------------------------- helpers
class FakeClient:
    """Stands in for the MQTT client so the worker path runs without a broker."""

    def __init__(self):
        self.published = []

    def publish(self, topic, payload, qos=1):
        self.published.append((topic, payload))
        return None


def make_worker(tmp_path: Path, name: str, endpoint: str, grip, store) -> Worker:
    settings = replace(
        load_settings(),
        processed_db=tmp_path / f"processed_{name}.db",
        outbox_db=tmp_path / f"outbox_{name}.db",
    )
    router = Router(
        [HTTPProvider("mock", endpoint)],
        health_check=False,
        conflict_store=store,
    )
    worker = Worker(settings=settings, router=router, conflict_store=store,
                    decision_gate=grip)
    worker.client = FakeClient()
    return worker


def deliver(worker: Worker, message: Message) -> None:
    worker._on_message(None, None, SimpleNamespace(payload=message.to_json().encode("utf-8")))


def message_for(task_id: str, jpeg: bytes, *, frame_timestamp: str = "1000",
                deadline: float | None = None) -> Message:
    return Message(
        message_id=task_id,
        created_at="2026-11-15T00:00:00.000Z",
        payload=base64.b64encode(jpeg).decode("ascii"),
        attempt=1,
        frame_timestamp=frame_timestamp,
        deadline=time.time() + 30 if deadline is None else deadline,
    )


def raw_frame(command_id: str, command: str, parameters_text: str,
              checksum: str | None = None) -> str:
    """Build a frame without local limit checks, for invalid-device-input tests."""
    csum = checksum or checksum_for(command_id, command, parameters_text)
    return f"{command_id}|{command}|{parameters_text}|{csum}\n"


def grip_policy_for(label: str = "cat", min_score: float = 0.55) -> Policy:
    return Policy({
        "version": 1,
        "default": {"action": "hold"},
        "rules": [
            {"label": label, "min_score": min_score, "action": "grip",
             "parameters": {"speed": 20}},
        ],
    })


def build_grip(tmp_path: Path, *, policy: Policy | None = None,
               actuator=None, log: ActuationLog | None = None,
               gate: DecisionGate | None = None):
    store = ConflictStore(tmp_path / "conflict.db")
    gate = gate or DecisionGate(tmp_path / "conflict.db")
    log = log or ActuationLog(tmp_path / "conflict.db")
    actuator = actuator or StubActuator()
    grip = ActuationGrip(gate, policy or grip_policy_for(), actuator, log)
    return grip, gate, log, actuator, store


# --------------------------------------------------------------------- TC5b.1
def test_tc5b_1_actuator_interface_and_stub_logs_all_commands(caplog):
    """Req 1: the interface has all five commands; the stub records each call."""
    with pytest.raises(TypeError):
        Actuator()  # abstract methods make the interface non-instantiable

    caplog.set_level(logging.INFO, logger="actuator.stub")
    stub = StubActuator()
    results = [
        stub.grip(speed=30),
        stub.release(),
        stub.move_to(90, speed=40),
        stub.stop(),
        stub.heartbeat(),
    ]

    assert [r.action for r in results] == [
        "grip", "release", "move_to", "stop", "heartbeat",
    ]
    assert all(r.acked and r.reason == "ack" for r in results)
    assert len({r.command_id for r in results}) == 5
    assert all(r.sent_at and r.acked_at for r in results)
    assert all(r.latency_ms is not None and r.latency_ms >= 0 for r in results)
    assert results[0].parameters == {"speed": 30}
    assert results[2].parameters == {"angle": 90, "speed": 40}
    assert len(stub.snapshot()) == 5
    assert any("grip" in record.getMessage() for record in caplog.records)
    stub.close()
    stub.close()  # idempotent


# --------------------------------------------------------------------- TC5b.2
def test_tc5b_2_policy_maps_label_and_score_with_hold_default():
    """Req 2: first matching rule wins; unknown/scoreless decisions hold."""
    policy = Policy()

    grip = policy.decide("part", 0.9)
    assert (grip.action, grip.parameters) == ("grip", {"speed": 25})
    assert grip.matched == "part"

    assert policy.decide("part", 0.2).action == "hold"      # below threshold
    assert policy.decide("part", None).action == "hold"     # score unavailable
    assert policy.decide("component", 0.6).action == "grip"  # boundary matches
    assert policy.decide("bin", 0.7).action == "release"
    assert policy.decide("cat", 0.99).action == "hold"      # unknown label
    assert policy.decide("", None).action == "hold"

    # Case/whitespace normalisation.
    assert policy.decide(" PART ", 0.9).action == "grip"

    # NaN would satisfy every comparison; it must be rejected, not acted on.
    with pytest.raises(ValueError):
        policy.decide("part", float("nan"))


def test_tc5b_2_policy_loads_from_json_file(tmp_path, monkeypatch):
    """Req 2: the mapping is configuration, not code."""
    config = {
        "version": 1,
        "default": {"action": "stop"},
        "rules": [
            {"label": "banana", "min_score": 0.75, "action": "move_to",
             "parameters": {"angle": 45, "speed": 15}},
            {"label": "banana", "action": "release", "parameters": {"speed": 10}},
        ],
    }
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(config), encoding="utf-8")

    policy = Policy.from_file(path)
    matched = policy.decide("banana", 0.8)
    assert (matched.action, matched.parameters) == ("move_to", {"angle": 45, "speed": 15})
    # Second rule for the same label catches a low score (first rule needs >= 0.75).
    assert policy.decide("banana", 0.2).action == "release"
    # No score: rules with a threshold cannot match, but an unconstrained rule
    # for the same label still does. No matching rule -> the default applies.
    assert policy.decide("banana", None).action == "release"
    assert policy.decide("kiwi", 0.9).action == "stop"

    monkeypatch.setenv("ACTUATION_POLICY_PATH", str(path))
    assert load_policy().decide("banana", 0.9).action == "move_to"

    # The committed example file must stay loadable and map "part" like the default.
    example = Policy.from_file(PROJECT_ROOT / "actuator" / "policy.example.json")
    assert example.decide("part", 0.99).action == "grip"


@pytest.mark.parametrize(
    "config",
    [
        {"rules": [{"label": "part", "action": "explode"}]},
        {"rules": [{"label": "part", "action": "hold", "parameters": {"speed": 10}}]},
        {"rules": [{"label": "part", "action": "move_to", "parameters": {}}]},
        {"rules": [{"label": "part", "action": "move_to",
                    "parameters": {"angle": 181}}]},
        {"rules": [{"label": "part", "action": "grip",
                    "parameters": {"speed": 0}}]},
        {"rules": [{"label": "", "action": "grip"}]},
        {"rules": [{"label": "part", "action": "grip", "min_score": 0.9,
                    "max_score": 0.1}]},
        {"rules": [{"label": "part", "action": "grip", "score": 0.5}]},
        {"default": {"action": "hold", "parameters": {"speed": 10}}},
        {"rule": []},
    ],
)
def test_tc5b_2_policy_rejects_invalid_config(config):
    """Req 2: bad configuration fails at load time, not mid-actuation."""
    with pytest.raises(ValueError):
        Policy(config)


# --------------------------------------------------------------------- TC5b.3
def test_tc5b_3_gate_applies_at_most_one_action_per_task(endpoint, tmp_path):
    """Req 3: duplicate deliveries and a second worker still cause one action."""
    frame = load_sample_frame(IMAGES_DIR / "cat.jpg")
    provider = HTTPProvider("mock", endpoint)
    probe = provider.predict(frame.jpeg_bytes, task_context={"task_id": "probe"})
    assert probe.ok and probe.score is not None and probe.score >= 0.55

    grip, gate, log, stub, store = build_grip(tmp_path)
    worker_a = make_worker(tmp_path, "a", endpoint, grip, store)
    worker_b = make_worker(tmp_path, "b", endpoint, grip, store)

    task_id = "task-5b3-0001"
    message = message_for(task_id, frame.jpeg_bytes, frame_timestamp="1001")
    deliver(worker_a, message)
    deliver(worker_a, message)   # same worker: processed_ids rejects it
    deliver(worker_b, message)   # second worker: gate rejects the duplicate
    assert len(stub.snapshot()) == 1

    row = log.get(task_id)
    assert row is not None
    assert {"task_id", "command_id", "action", "sent_at", "acked_at"} <= set(row)
    assert row["task_id"] == task_id
    assert row["command_id"] == command_id_for_task(task_id)
    assert row["action"] == "grip"
    assert row["parameters"] == {"speed": 20}
    assert row["acked"] is True and row["acked_at"]
    assert log.counts() == {"total": 1, "holds": 0, "acked": 1, "nacked": 0,
                            "unacked": 0}

    # A fresh edge process with the same DB cannot act again.
    replay_grip, _, _, replay_actuator, _ = build_grip(tmp_path)
    outcome = replay_grip.try_apply(task_id, "cat", "1001", time.time() + 30)
    assert outcome.applied is False and outcome.reason == "already_applied"
    assert len(replay_actuator.snapshot()) == 0

    # The log's primary key is the second line of defence.
    assert log.begin("task-log-1", "cmd-a", "grip") is True
    assert log.begin("task-log-1", "cmd-b", "grip") is False


def test_tc5b_3_hold_is_logged_but_sends_nothing(endpoint, tmp_path):
    """Req 2+3: the default hold records the task with no command or ACK."""
    frame = load_sample_frame(IMAGES_DIR / "cat.jpg")
    grip, gate, log, stub, store = build_grip(
        tmp_path, policy=Policy({"default": {"action": "hold"}, "rules": []})
    )
    worker = make_worker(tmp_path, "hold", endpoint, grip, store)
    task_id = "task-5b3-hold"
    deliver(worker, message_for(task_id, frame.jpeg_bytes, frame_timestamp="2001"))

    assert stub.snapshot() == []
    row = log.get(task_id)
    assert row is not None
    assert row["action"] == "hold"
    assert row["command_id"] is None and row["acked_at"] is None
    assert row["acked"] is False
    assert gate.is_applied(task_id)


def test_tc5b_3_late_decision_does_not_actuate(endpoint, tmp_path):
    """Req 3: a deadline-expired task never reaches the actuator."""
    frame = load_sample_frame(IMAGES_DIR / "cat.jpg")
    grip, gate, log, stub, store = build_grip(tmp_path)
    worker = make_worker(tmp_path, "late", endpoint, grip, store)
    task_id = "task-5b3-late"
    deliver(worker, message_for(task_id, frame.jpeg_bytes, frame_timestamp="3001",
                                deadline=time.time() - 1.0))
    assert stub.snapshot() == []
    assert log.get(task_id) is None
    assert gate.is_applied(task_id) is False


def test_tc5b_3_concurrent_workers_still_one_action(endpoint, tmp_path):
    """Req 3: five tasks raced by two workers each -> exactly five commands."""
    frame = load_sample_frame(IMAGES_DIR / "cat.jpg")
    grip, gate, log, stub, store = build_grip(tmp_path)
    worker_a = make_worker(tmp_path, "race_a", endpoint, grip, store)
    worker_b = make_worker(tmp_path, "race_b", endpoint, grip, store)
    task_ids = [f"task-5b3-race-{index}" for index in range(5)]
    barrier = threading.Barrier(2)

    for task_id in task_ids:
        message = message_for(task_id, frame.jpeg_bytes, frame_timestamp=task_id[-1])

        def run(worker, msg=message, bar=barrier):
            bar.wait()
            deliver(worker, msg)

        threads = [threading.Thread(target=run, args=(worker,))
                   for worker in (worker_a, worker_b)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

    assert len(stub.snapshot()) == len(task_ids)
    for task_id in task_ids:
        row = log.get(task_id)
        assert row is not None and row["acked"] is True
    assert log.counts()["acked"] == len(task_ids)


# --------------------------------------------------------------------- TC5b.4
@pytest.mark.skipif(
    not (HARDWARE_PORT and HARDWARE_ENABLED),
    reason="hardware only: " + HARDWARE_REASON,
)
def test_tc5b_4_serial_actuator_handshake_on_real_device():
    """Req 1: serial handshake. Motion-free on purpose."""
    actuator = SerialActuator(HARDWARE_PORT)
    try:
        heartbeat = actuator.heartbeat()
        assert heartbeat.acked, (
            f"no ACK for heartbeat: {heartbeat.reason} {heartbeat.detail}"
        )
        stopped = actuator.stop()
        assert stopped.acked, f"no ACK for stop: {stopped.reason} {stopped.detail}"
        assert stopped.latency_ms is not None
    finally:
        actuator.close(send_stop=False)


# --------------------------------------------------------------------- TC5b.5
@pytest.mark.skipif(
    not (HARDWARE_PORT and HARDWARE_ENABLED and FIRMWARE_ENABLED),
    reason="firmware on hardware only: set ACTUATION_SERIAL_PORT, "
           "ACTUATION_HARDWARE_TEST=1 and ACTUATION_FIRMWARE_TEST=1",
)
def test_tc5b_5_firmware_idempotence_on_real_device():
    """Req 5: firmware ACKs a duplicate stop without moving again.

    Angle/speed limits and the heartbeat safe state cannot be observed from the
    host without device telemetry, so they remain unverified on hardware.
    """
    actuator = SerialActuator(HARDWARE_PORT)
    try:
        assert actuator.heartbeat().acked
        first = actuator.stop(command_id="cmd-tc5b5-stop")
        second = actuator.stop(command_id="cmd-tc5b5-stop")
        assert first.acked and second.acked
        assert second.detail == "duplicate"
    finally:
        actuator.close(send_stop=False)


# --------------------------------------------------------------------- TC5b.6
def test_tc5b_6_frames_roundtrip_and_reject_bad_input():
    """Req 4: checksum/format/command/parameter errors get a NACK with a reason."""
    frame = format_frame("cmd-rt-1", "move_to", {"angle": 30, "speed": 20})
    parsed = parse_frame(frame)
    assert (parsed.command_id, parsed.command, parsed.parameters) == (
        "cmd-rt-1", "move_to", {"angle": 30, "speed": 20},
    )

    device = FakeDevice(clock=ManualClock())
    device.reply_to(format_frame("cmd-hb", "heartbeat"))

    # Corrupted checksum.
    corrupted = frame[:-2] + ("0" if frame[-2] != "0" else "1") + "\n"
    assert device.reply_to(corrupted).startswith("NACK|cmd-rt-1|checksum|")
    # Missing field / garbage / oversized.
    assert device.reply_to("cmd-x|grip||\n").startswith("NACK|")
    assert device.reply_to("garbage\n").startswith("NACK|garbage|format|")
    oversized = raw_frame("cmd-long", "move_to", "angle=10") + ("#" * 250) + "\n"
    assert device.reply_to(oversized).startswith("NACK|cmd-long|format|")
    # Unknown command, with a valid checksum.
    unknown = raw_frame("cmd-unknown", "explode", "")
    assert device.reply_to(unknown).startswith("NACK|cmd-unknown|unknown_command|")
    # Bad / unknown / duplicate parameters and a missing required one.
    for parameters in ("force=10", "speed=abc", "speed=10,speed=20"):
        reply = device.reply_to(raw_frame("cmd-p", "grip", parameters))
        assert "invalid_parameter" in reply, (parameters, reply)
    assert "invalid_parameter" in device.reply_to(
        raw_frame("cmd-m", "move_to", "speed=10")
    )
    # Out-of-range values are a limit, distinct from a malformed parameter.
    assert device.reply_to(raw_frame("cmd-lim", "move_to", "angle=181")).startswith(
        "NACK|cmd-lim|limit|"
    )
    assert device.reply_to(raw_frame("cmd-lim2", "grip", "speed=0")).startswith(
        "NACK|cmd-lim2|limit|"
    )
    # None of the rejected frames executed.
    assert device.executed == []
    assert len(device.rejected) >= 10


def test_tc5b_6_serial_actuator_reads_ack_and_nack_without_hardware():
    """Req 4+6: ACK/NACK parsing through the real SerialActuator code path."""
    device = FakeDevice(clock=ManualClock(), max_angle=90)
    actuator = SerialActuator(transport=device.transport(), timeout=0.2)

    # Boots safe: motion is NACKed until the first heartbeat.
    refused = actuator.grip()
    assert refused.acked is False and refused.reason == "safe_state"

    assert actuator.heartbeat().acked
    accepted = actuator.grip(speed=20)
    assert accepted.acked and accepted.detail == ""
    assert accepted.latency_ms is not None
    assert device.status()["gripping"] is True

    # Valid on the wire, outside this device's configured limit.
    limited = actuator.move_to(120)
    assert limited.acked is False and limited.reason == "limit"
    assert device.executed[-1]["command"] == "grip"

    # Rejected by the actuator locally; no frame is written at all.
    before = device.frames_received
    local = actuator.move_to(181)
    assert local.acked is False and local.reason == "limit"
    assert device.frames_received == before

    with pytest.raises(ProtocolError):
        format_frame("cmd-bad", "grip", {"speed": 101})


class SilentTransport:
    """Accepts writes but never answers, like an unplugged device."""

    def __init__(self):
        self.bytes_written = 0

    def write(self, data: bytes) -> int:
        self.bytes_written += len(data)
        return len(data)

    def readline(self) -> bytes:
        return b""

    def close(self) -> None:
        return None


def test_tc5b_6_serial_timeout_is_reported_not_raised():
    """Req 4: no reply becomes an unacknowledged result, never an exception."""
    transport = SilentTransport()
    actuator = SerialActuator(transport=transport, timeout=0.05)
    result = actuator.heartbeat()
    assert result.acked is False
    assert result.reason == "timeout"
    assert result.acked_at is None
    assert transport.bytes_written > 0


# --------------------------------------------------------------------- TC5b.7
def test_tc5b_7_duplicate_command_ids_are_acked_but_not_executed():
    """Req 5: the device remembers command ids and ignores duplicates."""
    device = FakeDevice(clock=ManualClock(), dedup_size=32)
    device.reply_to(format_frame("cmd-hb", "heartbeat"))
    command_id = "cmd-fixed-0001"
    frame = format_frame(command_id, "grip", {"speed": 20})

    first = device.reply_to(frame)
    second = device.reply_to(frame)
    assert first.startswith(f"ACK|{command_id}|grip|")
    assert second.startswith(f"ACK|{command_id}|grip|duplicate")
    assert len(device.executed) == 1
    assert device.duplicates == 1

    # A heartbeat with a duplicate id still refreshes the watchdog.
    heartbeat_id = "cmd-hb-dup"
    assert device.reply_to(format_frame(heartbeat_id, "heartbeat")).startswith("ACK|")
    clock = ManualClock()
    device2 = FakeDevice(clock=clock, heartbeat_timeout_s=2.0)
    device2.reply_to(format_frame(heartbeat_id, "heartbeat"))
    clock.advance(1.5)
    duplicate_heartbeat = device2.reply_to(format_frame(heartbeat_id, "heartbeat"))
    assert "duplicate" in duplicate_heartbeat
    clock.advance(1.5)
    assert device2.poll() is False
    clock.advance(0.6)
    assert device2.poll() is True


def test_tc5b_7_window_is_the_last_32_command_ids():
    """Req 5: the 32-id ring is real; the 33rd accepted id evicts the oldest."""
    device = FakeDevice(clock=ManualClock(), dedup_size=32)
    device.reply_to(format_frame("cmd-hb", "heartbeat"))
    for index in range(32):
        assert device.reply_to(
            format_frame(f"cmd-{index:03d}", "move_to", {"angle": 10})
        ).startswith("ACK|")
    assert len(device.executed) == 32

    # Still inside the window -> duplicate.
    device.reply_to(format_frame("cmd-000", "move_to", {"angle": 10}))
    assert len(device.executed) == 32
    assert device.duplicates == 1

    # One more accepted id evicts cmd-000; the same id now executes again.
    device.reply_to(format_frame("cmd-032", "move_to", {"angle": 10}))
    device.reply_to(format_frame("cmd-000", "move_to", {"angle": 10}))
    assert len(device.executed) == 34
    assert [entry["command_id"] for entry in device.executed].count("cmd-000") == 2


def test_tc5b_7_rejected_commands_are_not_remembered():
    """A NACKed command must not later be mistaken for an executed duplicate."""
    device = FakeDevice(clock=ManualClock())
    device.reply_to(format_frame("cmd-hb", "heartbeat"))
    rejected = device.reply_to(raw_frame("cmd-rej", "move_to", "angle=181"))
    assert "limit" in rejected
    retried = device.reply_to(format_frame("cmd-rej", "move_to", {"angle": 10}))
    assert retried.startswith("ACK|cmd-rej|move_to|")
    assert len(device.executed) == 1


# --------------------------------------------------------------------- TC5b.8
def test_tc5b_8_angle_and_speed_limits_are_enforced():
    """Req 5: limits are inclusive, rejected values never execute."""
    device = FakeDevice(clock=ManualClock())
    device.reply_to(format_frame("cmd-hb", "heartbeat"))

    assert device.reply_to(format_frame("cmd-a0", "move_to", {"angle": 0})).startswith("ACK|")
    assert device.reply_to(format_frame("cmd-a180", "move_to", {"angle": 180})).startswith("ACK|")
    assert device.reply_to(format_frame("cmd-s1", "move_to", {"angle": 90, "speed": 1})).startswith("ACK|")
    assert device.reply_to(format_frame("cmd-s100", "move_to", {"angle": 90, "speed": 100})).startswith("ACK|")

    executed_before = len(device.executed)
    assert "limit" in device.reply_to(raw_frame("cmd-neg", "move_to", "angle=-1"))
    assert "limit" in device.reply_to(raw_frame("cmd-high", "move_to", "angle=181"))
    assert "limit" in device.reply_to(raw_frame("cmd-slow", "grip", "speed=0"))
    assert "limit" in device.reply_to(raw_frame("cmd-fast", "release", "speed=101"))
    assert len(device.executed) == executed_before

    # Missing speed gets the documented device default.
    device.reply_to(format_frame("cmd-default", "move_to", {"angle": 45}))
    assert device.executed[-1]["parameters"]["speed"] == 30


def test_tc5b_8_safe_state_on_boot_and_after_heartbeat_timeout():
    """Req 5: boot safe, heartbeat arms, timeout drops back to safe, motion NACKed."""
    clock = ManualClock()
    device = FakeDevice(clock=clock, heartbeat_timeout_s=2.0)

    assert device.safe_state is True and device.servo_enabled is False
    boot = device.reply_to(format_frame("cmd-boot", "grip"))
    assert boot.startswith("NACK|cmd-boot|safe_state|")
    assert device.executed == []

    assert device.reply_to(format_frame("cmd-arm", "heartbeat")).startswith("ACK|")
    assert device.safe_state is False and device.servo_enabled is True
    assert device.reply_to(format_frame("cmd-grip", "grip", {"speed": 20})).startswith("ACK|")

    clock.advance(2.5)
    assert device.poll() is True
    status = device.status()
    assert status["speed"] == 0
    assert status["servo_enabled"] is False
    assert status["motion"] == "safe"

    refused = device.reply_to(format_frame("cmd-late", "move_to", {"angle": 30}))
    assert refused.startswith("NACK|cmd-late|safe_state|")

    assert device.reply_to(format_frame("cmd-rearm", "heartbeat")).startswith("ACK|")
    assert device.reply_to(format_frame("cmd-move", "move_to", {"angle": 30})).startswith("ACK|")


def test_tc5b_8_heartbeat_timeout_is_configurable_and_refreshed():
    """Req 5: the timeout comes from config; every heartbeat restarts it."""
    clock = ManualClock()
    short = FakeDevice(clock=clock, heartbeat_timeout_s=0.25)
    short.reply_to(format_frame("cmd-hb1", "heartbeat"))
    clock.advance(0.20)
    assert short.poll() is False
    clock.advance(0.06)
    assert short.poll() is True

    clock2 = ManualClock()
    device = FakeDevice(clock=clock2, heartbeat_timeout_s=2.0)
    device.reply_to(format_frame("cmd-hb2", "heartbeat"))
    clock2.advance(1.9)
    device.reply_to(format_frame("cmd-hb3", "heartbeat"))
    clock2.advance(1.9)
    assert device.poll() is False  # 1.9 s since the second heartbeat, not 3.8 s
    clock2.advance(0.2)
    assert device.poll() is True


# ------------------------------------------------------- optional simulation
def test_tc5b_sim_optional_pybullet_backend():
    """The simulation backend is optional; report a skip when it is absent."""
    pytest.importorskip("pybullet", reason="SimActuator needs the optional pybullet package")
    actuator = SimActuator()
    try:
        assert actuator.heartbeat().acked
        assert actuator.move_to(45, speed=10).acked
        assert actuator.status()["angle"] == 45
        assert actuator.grip().acked and actuator.status()["gripping"] is True
        assert actuator.release().acked and actuator.status()["gripping"] is False
        assert actuator.stop().acked
    finally:
        actuator.close()
