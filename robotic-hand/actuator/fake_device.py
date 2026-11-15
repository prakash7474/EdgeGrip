"""A Python implementation of the device protocol, for tests without hardware.

``FakeDevice`` enforces the same rules as the firmware sketch:

* checksum and frame format validation,
* angle and speed limits,
* the last 32 command ids are remembered and never executed twice,
* the device boots in a safe state and only motion after a heartbeat,
* a heartbeat timeout drops back to the safe state.

``FakeDevice.transport()`` returns a pair of in-memory ``write``/``readline``
buffers, so ``SerialActuator`` can be exercised without a serial port.
"""

from __future__ import annotations

import time
from collections import deque

from actuator.protocol import (
    DEFAULT_SPEED,
    MAX_ANGLE,
    MAX_FRAME_BYTES,
    MAX_SPEED,
    MIN_ANGLE,
    MOTION_COMMANDS,
    ProtocolError,
    encode_ack,
    encode_nack,
    is_valid_command_id,
    normalize_parameters,
    parse_frame,
)


class ManualClock:
    """Deterministic monotonic clock for heartbeat tests."""

    def __init__(self, start: float = 1000.0):
        self._value = float(start)

    def __call__(self) -> float:
        return self._value

    def advance(self, seconds: float) -> float:
        self._value += float(seconds)
        return self._value


class FakeDevice:
    """Protocol-complete device stand-in. Actuates nothing physical."""

    def __init__(
        self,
        *,
        clock=None,
        heartbeat_timeout_s: float = 2.0,
        dedup_size: int = 32,
        min_angle: int = MIN_ANGLE,
        max_angle: int = MAX_ANGLE,
        max_speed: int = MAX_SPEED,
        grip_gap_mm: float = 12.0,
        release_gap_mm: float = 40.0,
    ):
        if dedup_size <= 0:
            raise ValueError("dedup_size must be positive")
        self._clock = clock or time.monotonic
        self.heartbeat_timeout_s = float(heartbeat_timeout_s)
        self.dedup_size = int(dedup_size)
        self.min_angle = int(min_angle)
        self.max_angle = int(max_angle)
        self.max_speed = int(max_speed)
        self.grip_gap_mm = float(grip_gap_mm)
        self.release_gap_mm = float(release_gap_mm)

        # Physical/mode state. The device is unarmed until the first heartbeat.
        self.safe_state = True
        self.armed = False
        self.servo_enabled = False
        self.angle = 0
        self.gripper_gap_mm = float(release_gap_mm)
        self.gripping = False
        self.speed = 0
        self.motion = "safe"
        self.last_heartbeat = self._clock()

        # Observations for tests.
        self.frames_received = 0
        self.executed: list[dict] = []
        self.rejected: list[dict] = []
        self.duplicates = 0

        self._seen: deque[str] = deque(maxlen=self.dedup_size)
        self._buffer = b""
        self._replies: deque[bytes] = deque()

    # ------------------------------------------------------------------ frames
    def reply_to(self, line: str | bytes) -> str:
        """Validate and execute one frame, returning the reply line."""
        self.frames_received += 1
        raw = bytes(line).decode("ascii", "replace") if isinstance(line, (bytes, bytearray)) else str(line)
        hint = raw.split("|", 1)[0].strip()
        if not is_valid_command_id(hint):
            hint = "-"
        try:
            frame = parse_frame(
                raw,
                min_angle=self.min_angle,
                max_angle=self.max_angle,
                max_speed=self.max_speed,
            )
        except ProtocolError as exc:
            self._reject(hint, exc.reason, exc.detail)
            return encode_nack(hint, exc.reason, exc.detail)

        if frame.command == "heartbeat":
            duplicate = self._remember(frame.command_id)
            if duplicate:
                self.duplicates += 1
            self._on_heartbeat()
            return encode_ack(frame.command_id, "heartbeat", "duplicate" if duplicate else "-")

        if frame.command_id in self._seen:
            self.duplicates += 1
            return encode_ack(frame.command_id, frame.command, "duplicate")

        if self.safe_state and frame.command in MOTION_COMMANDS:
            self._reject(frame.command_id, "safe_state", "heartbeat required")
            return encode_nack(frame.command_id, "safe_state", "heartbeat required")

        parameters = normalize_parameters(
            frame.command,
            frame.parameters,
            min_angle=self.min_angle,
            max_angle=self.max_angle,
            max_speed=self.max_speed,
        )
        self._execute(frame.command, parameters, frame.command_id)
        self._remember(frame.command_id)
        return encode_ack(frame.command_id, frame.command, "-")

    def feed(self, data: bytes) -> None:
        """Accept bytes written by the host and queue replies."""
        self._buffer += bytes(data)
        while b"\n" in self._buffer:
            line, self._buffer = self._buffer.split(b"\n", 1)
            self._replies.append(self.reply_to(line).encode("ascii"))
        if len(self._buffer) > MAX_FRAME_BYTES - 1:
            self._buffer = b""
            self._reject("-", "format", "frame too long")
            self._replies.append(encode_nack("-", "format", "frame too long").encode("ascii"))

    def next_reply(self) -> bytes:
        if not self._replies:
            return b""
        return self._replies.popleft()

    def transport(self) -> "FakeTransport":
        """Return a serial-like transport backed by this device."""
        return FakeTransport(self)

    # ------------------------------------------------------------------ state
    def poll(self, now: float | None = None) -> bool:
        """Apply the heartbeat watchdog. Returns True while safe."""
        moment = self._clock() if now is None else float(now)
        if not self.safe_state and (moment - self.last_heartbeat) > self.heartbeat_timeout_s:
            self._enter_safe_state()
        return self.safe_state

    def status(self) -> dict:
        return {
            "safe_state": self.safe_state,
            "armed": self.armed,
            "servo_enabled": self.servo_enabled,
            "angle": self.angle,
            "gripper_gap_mm": self.gripper_gap_mm,
            "gripping": self.gripping,
            "speed": self.speed,
            "motion": self.motion,
            "last_heartbeat": self.last_heartbeat,
        }

    # ----------------------------------------------------------------- internal
    def _remember(self, command_id: str) -> bool:
        """Remember an accepted id. Returns True when it was already present."""
        if command_id in self._seen:
            return True
        self._seen.append(command_id)
        return False

    def _reject(self, command_id: str, reason: str, detail: str) -> None:
        self.rejected.append(
            {"command_id": command_id, "reason": reason, "detail": detail}
        )

    def _on_heartbeat(self) -> None:
        self.last_heartbeat = self._clock()
        self.armed = True
        if self.safe_state:
            self.safe_state = False
            self.servo_enabled = True
            self.motion = "idle"

    def _enter_safe_state(self) -> None:
        self.safe_state = True
        self.servo_enabled = False
        self.speed = 0
        self.motion = "safe"
        self.gripping = False

    def _execute(self, command: str, parameters: dict[str, int], command_id: str) -> None:
        self.executed.append(
            {"command_id": command_id, "command": command, "parameters": dict(parameters)}
        )
        speed = parameters.get("speed", DEFAULT_SPEED)
        if command == "grip":
            self.gripping = True
            self.gripper_gap_mm = self.grip_gap_mm
            self.speed = speed
            self.motion = "grip"
        elif command == "release":
            self.gripping = False
            self.gripper_gap_mm = self.release_gap_mm
            self.speed = speed
            self.motion = "release"
        elif command == "move_to":
            self.angle = int(parameters["angle"])
            self.speed = speed
            self.motion = "move_to"
        elif command == "stop":
            self.speed = 0
            self.motion = "stop"


class FakeTransport:
    """Minimal ``pyserial``-like transport backed by a :class:`FakeDevice`."""

    def __init__(self, device: FakeDevice):
        self.device = device
        self.closed = False
        self.bytes_written = 0

    def write(self, data: bytes) -> int:
        self.bytes_written += len(data)
        self.device.feed(data)
        return len(data)

    def readline(self) -> bytes:
        return self.device.next_reply()

    def reset_input_buffer(self) -> None:
        self.device._buffer = b""
        self.device._replies.clear()

    def close(self) -> None:
        self.closed = True
