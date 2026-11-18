# Hardware notes and first-run safety (Phase 5b)

> **Nothing in this repository has been tested on real hardware.** The
> actuator layer and protocol are tested against `FakeDevice` only; the
> firmware sketch has never been compiled or flashed. This page is a wiring and
> safety plan, not a report of a working machine.

Read this whole page before powering a servo. If any step is unclear, stop and
use a bench supply with a current limit until it is clear.

## 1. What you need

| Part | Notes |
|---|---|
| Controller | ESP32 dev board (recommended) or Arduino Uno/Nano |
| Servos | Two hobby servos: wrist rotation and gripper. Rated voltage/current from the datasheet. |
| Servo power supply | **Separate** 5–6 V supply, current capability above the sum of the stall currents. Do not use the controller board's 5 V pin. |
| Bulk capacitor | 470–1000 µF electrolytic across the servo supply, close to the servos |
| Fuse | Inline fuse on the servo supply positive, sized just above expected stall current |
| E-stop | Normally-closed, latching mushroom switch rated for the servo supply current |
| Wiring | Adequate gauge for stall current; common ground between controller and servo supply |
| USB cable | Data-capable, strain-relieved, from the edge PC to the controller |
| Mechanical restraint | Clamp or cage the hand for the first runs |

The numbers above are a starting point; use the servo datasheet, not a guess
from this page.

## 2. Wiring

### Signals

| Signal | ESP32 default | Uno/Nano default | Notes |
|---|---|---|---|
| Wrist servo PWM | GPIO 18 | D9 | Change `WRIST_PIN` in the sketch if you rewire |
| Gripper servo PWM | GPIO 19 | D10 | Change `GRIPPER_PIN` |
| Controller GND | GND | GND | Must be tied to the servo supply ground |
| USB | native USB | USB | 115200 baud serial console |

Hobby servos usually accept a 3.3 V PWM signal. If yours does not, use a level
shifter powered from the servo supply, not from the controller's 3.3 V rail.

### Power

```
  +5–6 V supply ──[ fuse ]──[ E-stop, NC ]──┬── servo 1 +V
                                            ├── servo 2 +V
                                            └── 470–1000 µF ── GND

  supply GND ──┬── servo 1 GND
               ├── servo 2 GND
               └── controller GND   (common ground is required)

  controller GPIO ── servo signal
```

- **Never power servos from the controller's 5 V/VIN pin or from the USB
  port.** Stall current can brown out the controller and reset it.
- Keep the servo power wires short and thick enough for stall current; keep
  them away from the signal wires.
- Put the bulk capacitor at the servo end of the power run, observing
  polarity. A reversed electrolytic capacitor can fail violently.
- Add the fuse on the supply positive. A bench supply with a current limit is
  the preferred first power source.

## 3. Physical e-stop (required)

The e-stop must **cut servo power**, not ask the firmware to stop:

```
supply + ──[ fuse ]──[ latching NC e-stop ]── servo +V
```

- Use a normally-closed latching switch (or a contactor/relay controlled by
  one) rated for the servo stall current.
- Do **not** implement the e-stop in software. A hung controller, a corrupted
  frame or a bug in this untested sketch must not be able to keep the servos
  powered.
- Mount the e-stop where the operator can reach it without reaching into the
  mechanism. One hand on the e-stop during the first runs.
- Cutting servo power removes torque. The hand can fall or drop its load.
  Clamp the mechanism or keep it low and clear before testing.
- Test the e-stop before every session: command a slow move, press the
  e-stop, confirm the servo stops and the controller stays alive (it will see
  no replies).
- The software `stop` command and the heartbeat safe state are additional
  behaviours, not replacements for the e-stop.

## 4. First-run safety checklist

Do these in order. Do not skip a step because a previous phase passed in
software. If any check fails, cut power with the e-stop or the supply and fix
it before continuing.

| # | Check | Pass criteria |
|---|---|---|
| 1 | Offline software suite passes | `python -m pytest tests/phase5b -q` shows passes/skips, no failures |
| 2 | Firmware compiles for **your** board | No errors in Arduino IDE / `arduino-cli`; treat the shipped sketch as unverified |
| 3 | Servo supply voltage and fuse correct | Measured at the servo connector, within the servo datasheet range |
| 4 | Common ground verified | Continuity between controller GND and supply GND |
| 5 | E-stop cuts servo power | With the supply on and controller idle, press e-stop: servo power goes to 0 V; controller stays powered |
| 6 | Mechanism clamped, **no object**, hands clear | Nothing can be pinched; cables cannot be pulled into the mechanism |
| 7 | Supply current limit set low | Start at a conservative limit for one servo idle + one small move |
| 8 | Heartbeat and stop only | `SerialActuator(...).heartbeat()` and `.stop()` return ACK; no motion |
| 9 | Grip/release at low speed, unloaded | `grip(speed=10)`, `release(speed=10)` move slowly and stop at the calibrated positions |
| 10 | Small wrist move at low speed | `move_to(5, speed=5)`, then small increments; direction and range match the mechanism |
| 11 | Limit rejection | `move_to(181)` returns `NACK ... limit` and no servo moves |
| 12 | Duplicate rejection | Sending the same `command_id` twice gets `duplicate` on the second ACK; no second move |
| 13 | Heartbeat safe state | Stop heartbeats for > 2 s: servos go limp; motion is NACKed with `safe_state`; a heartbeat re-arms |
| 14 | First loaded run | Only after 1–13, at low speed, with the e-stop held by a second person and the load restrained |

Two people for the first loaded run. Safety glasses. No loose sleeves, hair or
jewellery near the mechanism. Keep the workspace clear of the hand's full range
of motion.

## 5. Known gaps that need a real device

These cannot be verified in this environment and are **not** claimed as working:

- The firmware has never been compiled, flashed or run.
- Servo direction, travel and `GRIP_ANGLE` / `RELEASE_ANGLE` calibration.
- Real ACK/NACK round-trip timing and serial reliability over USB.
- Whether 115200 baud and the AVR serial buffer are sufficient in practice.
- The heartbeat safe state's physical effect (torque removal, coast/fall
  behaviour) and the re-arm transient.
- Angle/speed limits as observed on the mechanism (a servo can stall below the
  commanded speed; the firmware has no feedback).
- Power integrity under stall (supply sag, resets, EMI from servo motors).
- The e-stop's behaviour under load (contact rating, arcing, drop behaviour).
- The hardware-gated pytest cases TC5b.4/TC5b.5, which are skipped unless a
  port and explicit opt-in are provided.

## 6. Test status (software vs hardware)

| Layer | Status |
|---|---|
| Protocol encode/parse, checksum | Tested in Python (TC5b.6) |
| ACK/NACK reasons | Tested against `FakeDevice` (TC5b.6) |
| 32-id dedup ring | Tested against `FakeDevice` (TC5b.7) |
| Limits, boot-safe, heartbeat timeout | Tested against `FakeDevice` (TC5b.8) |
| Gate → actuator, one action per task, `actuation_log` | Tested through the real worker path with mock inference (TC5b.3) |
| Policy mapping | Tested in Python (TC5b.2) |
| Stub actuator | Tested (TC5b.1) |
| PyBullet simulation | Not exercised here (`pybullet` not installed); optional test skips |
| Serial port on real hardware | Hardware-gated test skips |
| Firmware on real hardware | **Untested** |

## 7. If something goes wrong

1. Press the e-stop first, then cut the supply.
2. Do not touch a hot servo or driver; stall current heats motors quickly.
3. Power off before changing wiring. Capacitors may hold charge.
4. Record what happened (command, reply, LED/serial output) before changing
   anything. The edge's `actuation_log` and `live_results.md` are useful here.
5. If a servo ran away, do not re-power until the signal wiring, ground and
   `GRIP_ANGLE`/`RELEASE_ANGLE` values have been checked.
