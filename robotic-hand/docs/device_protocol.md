# Device protocol — edge ⇄ hand controller (Phase 5b)

Version 1. This document is normative for `actuator/protocol.py`,
`actuator/fake_device.py` and `firmware/robotic_hand/robotic_hand.ino`.

> **Implementation status:** the Python side is implemented and tested against
> `FakeDevice` (no hardware). The firmware sketch has never been compiled or
> run on hardware. See [`../firmware/README.md`](../firmware/README.md) and
> [`hardware.md`](hardware.md).

## 1. Transport

- USB serial (or UART), **115200 baud, 8N1**, ASCII only.
- Frames are terminated by `\n` (LF). A `\r` before LF is ignored.
- One command in flight: the host sends a frame, waits for the reply, then
  sends the next. There is no pipelining and no flow control.
- A frame is at most **192 bytes including the LF**.
- The device never sends unsolicited data. The host ignores lines that do not
  match a reply for the command in flight.

The link is a short, trusted cable. The checksum detects corruption, not
tampering; there is no authentication or encryption.

## 2. Command frame

```
command_id "|" command "|" parameters "|" checksum LF
```

| Field | Rule |
|---|---|
| `command_id` | 1–36 characters, `[A-Za-z0-9_-]`. Chosen by the host. |
| `command` | One of `grip`, `release`, `move_to`, `stop`, `heartbeat`. |
| `parameters` | Empty, or comma-separated `key=value` pairs. Keys are lowercase `[a-z_]`, values are decimal integers. The host emits keys sorted alphabetically; the device accepts any order. |
| `checksum` | 8 hexadecimal digits, see §3. The host emits uppercase; both ends accept either case. |

### Commands and parameters

| Command | Parameters | Meaning |
|---|---|---|
| `grip` | `speed` (optional) | Close the gripper to its configured grip angle. |
| `release` | `speed` (optional) | Open the gripper to its configured release angle. |
| `move_to` | `angle` (required), `speed` (optional) | Rotate the wrist joint to `angle` degrees. |
| `stop` | none | Stop motion and hold the current position. Allowed while safe. |
| `heartbeat` | none | Reset the watchdog and leave the safe state. |

Limits (inclusive):

| Parameter | Range | Default |
|---|---|---|
| `angle` | 0–180 degrees | — (required for `move_to`) |
| `speed` | 1–100 % | 30 |

`grip` and `release` do not take an angle: the grip/release positions are
calibrated on the device. A hobby servo has no force feedback, so there is no
force parameter; `speed` only limits the slew rate.

## 3. Checksum

32-bit **FNV-1a** over the ASCII bytes of

```
command_id "|" command "|" parameters
```

(no trailing pipe, no checksum, no LF), with offset basis `0x811C9DC5` and
prime `0x01000193`, rendered as 8 uppercase hex digits (parsers accept either
case).

Reference (Python, also in `actuator/protocol.py`):

```python
def fnv1a32(data: bytes) -> int:
    value = 0x811C9DC5
    for byte in data:
        value = ((value ^ byte) * 0x01000193) & 0xFFFFFFFF
    return value

checksum = f"{fnv1a32(b'command_id|command|parameters'):08X}"
```

Worked example:

```
command_id  = cmd-1a2b3c4d5e6f7788
command     = move_to
parameters  = angle=45,speed=20
input       = cmd-1a2b3c4d5e6f7788|move_to|angle=45,speed=20
checksum    = 8E8F1EC6
frame       = cmd-1a2b3c4d5e6f7788|move_to|angle=45,speed=20|8E8F1EC6\n
```

## 4. Replies

```
ACK  "|" command_id "|" command "|" detail LF
NACK "|" command_id "|" reason  "|" detail LF
```

- `detail` is free text without `|` or newlines, at most 40 characters. `-`
  means "no detail". A duplicate ACK uses `duplicate`.
- A reply echoes the `command_id` of the frame it answers.
- **ACK means the command was accepted and applied, not that the motion has
  finished.** Motion is executed after the ACK is sent; the ACK latency
  measured by `live_run.py` is therefore the command acceptance latency.

| NACK reason | Meaning |
|---|---|
| `checksum` | Checksum mismatch; nothing executed. |
| `format` | Empty frame, wrong field count, bad command id, bad checksum field, or frame too long. |
| `unknown_command` | Checksum valid but the command is not in the list. |
| `invalid_parameter` | Unknown/duplicate parameter, non-integer value, missing required parameter, or a parameter not allowed for that command. |
| `limit` | Numeric value outside the allowed range. |
| `safe_state` | Motion command refused because the device is in the safe state. |
| `busy` | Reserved; the v1 firmware does not use it. |

Examples:

```
ACK|cmd-1a2b3c4d5e6f7788|move_to|-
ACK|cmd-1a2b3c4d5e6f7788|grip|duplicate
NACK|cmd-1a2b3c4d5e6f7788|limit|angle out of range
NACK|-|format|bad command_id
```

## 5. Device state and safety rules

### Boot

The device boots in the **safe state**: both servos are detached (no PWM, no
torque). Motion commands are answered with `NACK ... safe_state`.

### Heartbeat

- The host sends `heartbeat` every 500 ms by default (`SerialActuator`).
- The first valid heartbeat arms the device: servos are attached at the
  current commanded positions, without moving to a stored target.
- If no valid heartbeat arrives for `HEARTBEAT_TIMEOUT_MS` (2000 ms in the
  reference firmware), the device enters the safe state, detaches both servos
  and stops stepping motion. The next heartbeat re-arms it; it does not resume
  the interrupted motion.
- A duplicate heartbeat is still a valid heartbeat: it resets the watchdog and
  is answered `ACK ... duplicate`.

### Dedup window

The device remembers the **last 32 accepted command ids** (FIFO; the oldest is
evicted by the 33rd). A frame whose id is in the window is answered with
`ACK ... duplicate` and is not executed again. Heartbeats consume a slot.

Rejected frames (bad checksum, bad format, invalid parameter, limit, safe
state) are **not** remembered, so a corrected retry with the same id is
accepted. Dedup is per boot; a power cycle clears it.

The host uses a deterministic command id derived from `task_id`
(`cmd-` + first 16 hex characters of SHA-256 of the task id). A resend after
an edge restart therefore carries the same id.

### Motion

- Motion is stepped one degree at a time, scaled by `speed`.
- A new motion command retargets the axis; there is no queue and no `busy`
  reply in v1.
- `stop` stops stepping immediately and holds position. In the safe state it
  is accepted but changes nothing (servos are already detached).

## 6. Host behaviour

- A reply timeout is 1 s by default. No reply means **unconfirmed**, not
  failed: the command may have been executed. It is recorded with no ACK and
  is **never retried automatically**.
- A NACK is final for that command; the host does not retry. `live_run.py`
  records the reason in `actuation_log`.
- `ActuationGrip` claims `task_id` in `actuation_log` before sending, so a
  task causes at most one command even across duplicate deliveries, two
  workers, or an edge restart. A crash after the claim but before the send
  leaves an unconfirmed row and no motion (fail-safe).
- The host must keep heartbeats flowing while the device is armed.
  `SerialActuator.start_heartbeat()` provides the loop.

## 7. Conformance

| Side | Status |
|---|---|
| `actuator/fake_device.py` | Implemented; exercised by TC5b.6–TC5b.8 without hardware. Enforces checksum, format, command, parameter, limit, boot-safe, dedup-32, heartbeat timeout. |
| `firmware/robotic_hand/robotic_hand.ino` | Sketch written to this spec; **never compiled or run on hardware.** |
| `actuator/serial_actuator.py` | Implemented; tested against `FakeDevice` through the same framing/ACK matching. A real port is only exercised by hardware-gated TC5b.4. |

`actuator/protocol.py` is the shared Python implementation:
`format_frame()`, `parse_frame()`, `parse_reply()`, `normalize_parameters()`.
Its limits (`MIN_ANGLE`, `MAX_ANGLE`, `MIN_SPEED`, `MAX_SPEED`,
`DEFAULT_SPEED`, `MAX_FRAME_BYTES`) must be kept in sync with the constants at
the top of the firmware sketch.
