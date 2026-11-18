"""Line protocol shared by the edge actuator and the device firmware.

One command is one ASCII line::

    command_id|command|parameters|checksum\n

and the device answers one line::

    ACK|command_id|command|detail\n
    NACK|command_id|reason|detail\n

``docs/device_protocol.md`` is the normative description. Keeping the parser
here (rather than inside the serial actuator) lets :class:`FakeDevice` and the
Edge use exactly the same validation rules, including limits.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Mapping

PROTOCOL_VERSION = 1
MAX_FRAME_BYTES = 192
DETAIL_MAX_CHARS = 40
MAX_COMMAND_ID_CHARS = 36

MIN_ANGLE = 0
MAX_ANGLE = 180
MIN_SPEED = 1
MAX_SPEED = 100
DEFAULT_SPEED = 30

MOTION_COMMANDS = ("grip", "release", "move_to")
COMMANDS = ("grip", "release", "move_to", "stop", "heartbeat")
ACK = "ACK"
NACK = "NACK"
NO_DETAIL = "-"

REASONS = (
    "checksum",
    "format",
    "unknown_command",
    "invalid_parameter",
    "limit",
    "safe_state",
    "busy",
)
REASON_SET = frozenset(REASONS)

#: Allowed parameters and their inclusive bounds, per command.
PARAMETER_LIMITS: dict[str, dict[str, tuple[int, int]]] = {
    "grip": {"speed": (MIN_SPEED, MAX_SPEED)},
    "release": {"speed": (MIN_SPEED, MAX_SPEED)},
    "move_to": {"angle": (MIN_ANGLE, MAX_ANGLE), "speed": (MIN_SPEED, MAX_SPEED)},
    "stop": {},
    "heartbeat": {},
}
REQUIRED_PARAMETERS: dict[str, tuple[str, ...]] = {"move_to": ("angle",)}

_COMMAND_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,%d}$" % MAX_COMMAND_ID_CHARS)
_CHECKSUM_RE = re.compile(r"^[0-9A-Fa-f]{8}$")
_KEY_RE = re.compile(r"^[a-z_]{1,16}$")
_INT_RE = re.compile(r"^-?\d{1,4}$")


def is_valid_command_id(value: str) -> bool:
    """True when ``value`` can be used as a command id on the wire."""
    return isinstance(value, str) and bool(_COMMAND_ID_RE.match(value))


class ProtocolError(ValueError):
    """A frame or reply was rejected. ``reason`` is one of :data:`REASONS`."""

    def __init__(self, reason: str, detail: str = ""):
        if reason not in REASON_SET:
            raise ValueError(f"invalid protocol reason: {reason}")
        self.reason = reason
        self.detail = clean_detail(detail)
        text = f"{reason}: {self.detail}" if self.detail else reason
        super().__init__(text)


def clean_detail(detail: str | None) -> str:
    """Return a reply-safe detail: one line, no field separators, short."""
    if not detail:
        return ""
    text = str(detail).replace("|", "/").replace("\r", " ").replace("\n", " ")
    return text[:DETAIL_MAX_CHARS]


def _detail_field(detail: str | None) -> str:
    cleaned = clean_detail(detail)
    return cleaned if cleaned else NO_DETAIL


def fnv1a32(data: bytes) -> int:
    """32-bit FNV-1a, identical to the firmware implementation."""
    value = 0x811C9DC5
    for byte in data:
        value = ((value ^ byte) * 0x01000193) & 0xFFFFFFFF
    return value


def checksum_for(command_id: str, command: str, parameters_text: str) -> str:
    """Checksum over ``command_id|command|parameters`` as ASCII."""
    payload = f"{command_id}|{command}|{parameters_text}".encode("ascii")
    return f"{fnv1a32(payload):08X}"


def encode_parameters(parameters: Mapping[str, int] | None) -> str:
    """Encode parameters as sorted ``key=value`` pairs separated by commas."""
    if not parameters:
        return ""
    items: list[str] = []
    for key in sorted(parameters):
        if not _KEY_RE.match(key):
            raise ProtocolError("invalid_parameter", f"bad parameter name {key!r}")
        value = parameters[key]
        if isinstance(value, bool) or not isinstance(value, int):
            raise ProtocolError("invalid_parameter", f"{key} must be an integer")
        items.append(f"{key}={value}")
    return ",".join(items)


def validate_parameters(
    command: str,
    parameters: Mapping[str, int] | None,
    *,
    min_angle: int = MIN_ANGLE,
    max_angle: int = MAX_ANGLE,
    max_speed: int = MAX_SPEED,
) -> dict[str, int]:
    """Validate a command's parameters; raise :class:`ProtocolError` if bad."""
    if command not in COMMANDS:
        raise ProtocolError("unknown_command", command)
    allowed = PARAMETER_LIMITS[command]
    cleaned: dict[str, int] = {}
    for key, value in (parameters or {}).items():
        if key not in allowed:
            raise ProtocolError("invalid_parameter", f"parameter {key} not allowed for {command}")
        if isinstance(value, bool) or not isinstance(value, int):
            raise ProtocolError("invalid_parameter", f"{key} must be an integer")
        low, high = allowed[key]
        if key == "angle":
            low, high = min_angle, max_angle
        elif key == "speed":
            high = min(high, max_speed)
        if not low <= value <= high:
            raise ProtocolError("limit", f"{key}={value} outside {low}..{high}")
        cleaned[key] = value
    for key in REQUIRED_PARAMETERS.get(command, ()):
        if key not in cleaned:
            raise ProtocolError("invalid_parameter", f"{key} is required")
    return cleaned


def normalize_parameters(
    command: str,
    parameters: Mapping[str, int] | None,
    *,
    min_angle: int = MIN_ANGLE,
    max_angle: int = MAX_ANGLE,
    max_speed: int = MAX_SPEED,
) -> dict[str, int]:
    """Validate parameters and fill the device-side default speed."""
    cleaned = validate_parameters(
        command, parameters, min_angle=min_angle, max_angle=max_angle, max_speed=max_speed
    )
    if command in MOTION_COMMANDS and "speed" not in cleaned:
        cleaned["speed"] = min(DEFAULT_SPEED, max_speed)
    return cleaned


def format_frame(
    command_id: str,
    command: str,
    parameters: Mapping[str, int] | None = None,
) -> str:
    """Build one complete command frame, including its trailing newline."""
    if not isinstance(command_id, str) or not _COMMAND_ID_RE.match(command_id):
        raise ProtocolError("format", "bad command_id")
    if command not in COMMANDS:
        raise ProtocolError("unknown_command", command)
    validate_parameters(command, parameters)
    parameters_text = encode_parameters(parameters)
    checksum = checksum_for(command_id, command, parameters_text)
    frame = f"{command_id}|{command}|{parameters_text}|{checksum}"
    if len(frame.encode("ascii")) + 1 > MAX_FRAME_BYTES:
        raise ProtocolError("format", "frame too long")
    return frame + "\n"


@dataclass(frozen=True)
class Frame:
    command_id: str
    command: str
    parameters: dict[str, int]


def parse_frame(
    line: str | bytes,
    *,
    min_angle: int = MIN_ANGLE,
    max_angle: int = MAX_ANGLE,
    max_speed: int = MAX_SPEED,
) -> Frame:
    """Parse and validate one command frame; raise :class:`ProtocolError`."""
    if isinstance(line, (bytes, bytearray)):
        try:
            text = bytes(line).decode("ascii")
        except UnicodeDecodeError as exc:
            raise ProtocolError("format", "non-ascii frame") from exc
    else:
        text = line
    text = text.rstrip("\r\n")
    if not text:
        raise ProtocolError("format", "empty frame")
    if not text.isascii():
        raise ProtocolError("format", "non-ascii frame")
    if len(text) + 1 > MAX_FRAME_BYTES:
        raise ProtocolError("format", "frame too long")
    fields = text.split("|")
    if len(fields) != 4:
        raise ProtocolError("format", "expected 4 fields")
    command_id, command, parameters_text, checksum = fields
    if not _COMMAND_ID_RE.match(command_id):
        raise ProtocolError("format", "bad command_id")
    if not _CHECKSUM_RE.match(checksum):
        raise ProtocolError("format", "bad checksum field")
    if checksum_for(command_id, command, parameters_text) != checksum:
        raise ProtocolError("checksum", "checksum mismatch")
    if command not in COMMANDS:
        raise ProtocolError("unknown_command", command)
    parameters: dict[str, int] = {}
    if parameters_text:
        for item in parameters_text.split(","):
            key, separator, raw = item.partition("=")
            if not separator or not _KEY_RE.match(key) or not _INT_RE.match(raw):
                raise ProtocolError("invalid_parameter", f"bad parameter {item!r}")
            if key in parameters:
                raise ProtocolError("invalid_parameter", f"duplicate parameter {key}")
            parameters[key] = int(raw)
    validate_parameters(
        command, parameters, min_angle=min_angle, max_angle=max_angle, max_speed=max_speed
    )
    return Frame(command_id=command_id, command=command, parameters=parameters)


def encode_ack(command_id: str, command: str, detail: str = "") -> str:
    return f"{ACK}|{command_id}|{command}|{_detail_field(detail)}\n"


def encode_nack(command_id: str, reason: str, detail: str = "") -> str:
    if reason not in REASON_SET:
        raise ValueError(f"invalid NACK reason: {reason}")
    return f"{NACK}|{command_id}|{reason}|{_detail_field(detail)}\n"


@dataclass(frozen=True)
class Reply:
    kind: str
    command_id: str
    command: str | None
    reason: str
    detail: str
    ok: bool


def parse_reply(line: str | bytes) -> Reply:
    """Parse one ACK/NACK line from the device."""
    if isinstance(line, (bytes, bytearray)):
        try:
            text = bytes(line).decode("ascii")
        except UnicodeDecodeError as exc:
            raise ProtocolError("format", "non-ascii reply") from exc
    else:
        text = line
    text = text.strip("\r\n")
    fields = text.split("|")
    if len(fields) != 4:
        raise ProtocolError("format", "reply must have 4 fields")
    kind, command_id, third, detail = fields
    detail = "" if detail in ("", NO_DETAIL) else clean_detail(detail)
    if kind == ACK:
        return Reply(kind=ACK, command_id=command_id, command=third, reason="ack",
                     detail=detail, ok=True)
    if kind == NACK:
        if third not in REASON_SET:
            raise ProtocolError("format", f"unknown NACK reason {third!r}")
        return Reply(kind=NACK, command_id=command_id, command=None, reason=third,
                     detail=detail, ok=False)
    raise ProtocolError("format", f"unknown reply kind {kind!r}")
