# robotic-hand — edge/cloud inference pipeline (phases 1–5b)

A low-cost robotic hand for small factories that cannot afford a local AI
server. An edge client captures a camera frame, sends it through a reliable
MQTT link, and gets a conflict-checked decision back from one of several
inference providers.

This folder is the phase-by-phase implementation. Phases 1–5b are implemented
and tested offline; the Phase 5b firmware sketch is **untested on real
hardware**, and no physical robot has been driven.

## Status

| Phase | Scope | Status |
|---|---|---|
| **1** | Baseline edge → cloud pipeline with measured latency | **Complete** |
| **2** | Reliable delivery: MQTT (Mosquitto), SQLite outbox, QoS 1, ACKs, retries with backoff, dedup | **Complete** (integration tests need Docker) |
| **3** | Multi-provider router: health checks, circuit breaker, sequential failover, deadline-derived timeouts | **Complete** |
| **4** | Task context end to end, result claims, discard log, robot decision gate, safe defaults | **Complete** |
| **5a** | Cloud deployment harness: token auth, container image, live latency/fault scripts, deployment docs | **Complete** |
| **5b** | Actuator interface, policy, gate→actuator wiring, device protocol, fake device, firmware sketch, hardware docs | **Software complete and tested offline**; firmware untested on hardware |
| 5c | Field integration with a real hand | Not started |

Latest offline run in this workspace (Windows 11, Python 3.14.5):

```
74 passed, 14 skipped
```

Skipped: 4 real-model tests (`RUN_REAL_MODEL!=1`), 6 Phase 2 broker tests plus
1 Phase 3 broker test (Docker unavailable), 2 Phase 5b hardware-gated tests
(TC5b.4/5b.5), and the optional PyBullet test (`pybullet` not installed).

## How it works

```
camera / images
      │  phase1_pipeline/capture.py
      ▼
Frame(name, jpeg_bytes, width, height)
      │  phase2_reliability/outbox.py  (pending row before send)
      ▼
Publisher ──MQTT QoS 1──► Mosquitto ──► Worker
      ▲                                   │  dedup by task_id
      │ ACK                               ▼
      └──────────────────────────── phase3_router/router.py
                                          │ health checks
                                          │ circuit breaker
                                          │ remaining-deadline / remaining-providers
                                          ▼
                              provider (mock A, mock B, or real RT-DETR)
                                          │ echoes task_id
                                          ▼
                          results claim (first writer wins)
                                          │
                                          ▼
                          robot decision gate (applied_ids)
                              deadline · newer frame · already applied
                                          │
                                          ▼
                         apply decision | safe default | discard(late)
                                          │
                                          ▼
                          actuation policy (label, score) → action
                                          │  actuation_log claim (task_id unique,
                                          │  command_id derived from task_id)
                                          ▼
                          Actuator: stub | serial | sim  ──► hand controller
```

Phase 1 can still be run on its own: capture → HTTP POST → measured round trip.

## Project structure

```
robotic-hand/
├── README.md
├── .env.example / .gitignore
├── requirements.txt / requirements-real.txt
├── pytest.ini
├── docker-compose.yml            # Mosquitto broker (Phase 2)
├── mosquitto/                    # broker config
├── images/                       # sample frames + labels.json
├── mock_server/                  # deterministic /predict and /predict/slow
├── real_server/                  # optional RT-DETR endpoint
├── phase1_pipeline/
│   ├── capture.py                # camera with sample-image fallback
│   ├── client.py                 # HTTP POST, timeouts, latency, structured errors
│   ├── benchmark.py              # N requests, CSV, avg/min/max
│   └── config.py
├── phase2_reliability/
│   ├── common.py                 # message format + task context fields
│   ├── outbox.py                 # durable pending/sent/acked state
│   ├── publisher.py              # QoS 1, ACK, backoff resend, offline drain
│   ├── worker.py                 # dedup, inference, claim, gate, result publish
│   └── config.py
├── phase3_router/
│   ├── router.py                 # health, breaker, EWMA, sequential failover
│   ├── config.py
│   └── providers/                # base protocol, HTTP adapter, controllable mocks
├── phase4_conflict/
│   ├── context.py                # TaskContext (task_id, attempt, ts, deadline)
│   ├── store.py                  # results, discarded, applied_ids, DecisionGate
│   └── __init__.py
├── actuator/
│   ├── base.py                   # Actuator interface + ActuationResult
│   ├── stub.py                   # logs commands, ACKs immediately (default)
│   ├── serial_actuator.py        # line protocol over pyserial (optional)
│   ├── sim.py                    # PyBullet kinematic hand (optional)
│   ├── protocol.py               # frames, checksum, limits, ACK/NACK parsing
│   ├── fake_device.py            # Python device used by tests, no hardware
│   ├── policy.py                 # (label, score) → action, default hold
│   ├── log.py                    # actuation_log table
│   ├── grip.py                   # Phase 4 gate → policy → actuator, once per task
│   └── policy.example.json       # example rule set
├── firmware/
│   ├── README.md                 # UNTESTED firmware notes and bench checks
│   └── robotic_hand/robotic_hand.ino
├── benchmarks/router_overhead.py # loopback-only overhead benchmark
├── tests/
│   ├── test_tc1_*.py             # Phase 1 (15 tests)
│   ├── phase2/                   # TC2.1–TC2.6 (Docker/broker)
│   ├── phase3/                   # TC3.1–TC3.9 + integration
│   ├── phase4/                   # TC4.1–TC4.12
│   ├── phase5b/                  # TC5b.1–TC5b.8 + live latency join
│   └── test_real_model.py        # marked real_model
├── docs/
│   ├── measurements.md           # real measurements, labelled mock vs real
│   ├── device_protocol.md        # normative serial protocol spec
│   └── hardware.md               # wiring, e-stop, first-run safety checklist
└── logs/                         # generated CSV/DB (gitignored)
```

## Setup

Python 3.10 or newer is required; verified on 3.14.5.

```bash
cd robotic-hand

python -m venv .venv
source .venv/Scripts/activate     # Windows (Git Bash)
# macOS / Linux: source .venv/bin/activate

python -m pip install --upgrade pip
pip install -r requirements.txt
cp .env.example .env
```

`requirements.txt` pins `fastapi`, `uvicorn`, `opencv-python`, `numpy`,
`requests`, `python-dotenv`, `paho-mqtt` and `pytest`. No API key is needed for
the offline path.

### Configuration

All settings come from environment variables (or `.env`, gitignored);
`.env.example` documents every one. Nothing is hard-coded, no secret is stored
in source, and API keys are never logged.

| Variable | Default | Purpose |
|---|---|---|
| `INFERENCE_MODE` | `mock` | `mock` = local server; `real` = hosted provider |
| `INFERENCE_ENDPOINT` | `http://127.0.0.1:8000/predict` | Full URL that receives the JPEG |
| `INFERENCE_API_KEY` | empty | Bearer token, required only in `real` mode |
| `INFERENCE_TIMEOUT_SECONDS` | `10` | Connect + read timeout per request |
| `LATENCY_TARGET_MS` | `5000` | Prototype budget for one round trip |
| `CAMERA_ENABLED` / `CAMERA_INDEX` | `1` / `0` | Camera probe settings |
| `SAMPLE_IMAGES_DIR` | `images` | Fallback frames and `labels.json` |
| `BENCHMARK_CSV_PATH` | `logs/latency.csv` | Per-request latency log |
| `MEASUREMENTS_PATH` | `docs/measurements.md` | Where results are recorded |
| `MQTT_BROKER_HOST` / `MQTT_BROKER_PORT` | `127.0.0.1` / `1883` | Broker address |
| `ROUTER_ENDPOINTS` | empty | Comma-separated provider `/predict` URLs |
| `MODEL_ID` | `PekingU/rtdetr_r18vd` | Real model (optional) |
| `DETECTION_THRESHOLD` | `0.5` | Real model confidence threshold |
| `ACTUATION_POLICY_PATH` | empty | JSON policy file; empty uses the built-in default (`hold`) |
| `ACTUATION_SERIAL_PORT` | empty | Serial port for `--actuator serial`, e.g. `COM5` or `/dev/ttyUSB0` |

## Run it

Two terminals for the baseline path:

```bash
# terminal 1
cd robotic-hand
python -m mock_server --port 8000

# terminal 2
python -c "from phase1_pipeline.capture import capture_once; \
from phase1_pipeline.client import CloudClient; \
f = capture_once('images', camera_enabled=False); \
r = CloudClient('http://127.0.0.1:8000/predict').predict(f.jpeg_bytes, f.name); \
print(r.label, r.latency_ms, 'ms')"

# 20 requests: CSV rows, avg/min/max, measurements doc
python -m phase1_pipeline.benchmark --count 20 --no-camera
```

`--no-camera` skips the webcam probe; capture falls back to `images/` when no
device is available.

### Real model endpoint (optional)

```bash
pip install -r requirements-real.txt
python -m real_server --port 8001
export INFERENCE_ENDPOINT=http://127.0.0.1:8001/predict
python -m phase1_pipeline.benchmark --count 20 --no-camera
RUN_REAL_MODEL=1 REAL_ENDPOINT=http://127.0.0.1:8001/predict python -m pytest tests/test_real_model.py -v
```

`POST /predict` accepts raw JPEG bytes and returns `label`, `prediction`,
`detections` and `server_inference_ms`; `"none"` means nothing passed the
threshold. `GET /health` reports the model, device and threshold. The model is
COCO-trained and does not know factory-specific parts.

## Phase 1 — baseline pipeline

Capture a frame, POST raw JPEG, measure the round trip with `perf_counter`, and
return structured failures instead of raising. The mock server returns the
label declared in `images/labels.json` keyed by the image SHA-256, so tests are
deterministic and offline; it is not an accuracy benchmark.

Measured baseline from a real 20-request run against the local mock server
(full record in [`docs/measurements.md`](docs/measurements.md)):

| Metric | Value |
|---|---|
| Requests | 20 |
| Completed / failed | 20 / 0 |
| Average round trip | 14.992 ms |
| Minimum | 3.342 ms |
| Maximum | 50.853 ms |

These are loopback mock numbers, not cloud numbers. No hosted provider was
called.

## Phase 2 — reliable delivery

`docker compose up -d` starts Mosquitto; `python -m mock_server` provides
inference.

- **Outbox (SQLite)**: row written as `pending` **before** any send attempt,
  then `sent`, then `acked` only on ACK. Survives restarts.
- **Publisher**: MQTT QoS 1, subscribes to the ACK topic, resends unacked
  messages with backoff 1 s → 2 s → 4 s capped at 30 s, oldest first. Offline
  frames stay in the outbox and drain on reconnect.
- **Worker**: checks `processed_ids`, skips duplicates but re-sends the ACK,
  runs inference, records the result, publishes the result and ACK.
- **Message format**: JSON with `message_id` (UUID4), `created_at`, base64 JPEG
  (compressed under ~100 KB) plus Phase 4 fields (`attempt`,
  `frame_timestamp`, `deadline`).

```bash
python -m pytest tests/phase2 -v   # requires Docker; otherwise skipped
```

Guarantee: *no message loss in the tested failure modes*.

## Phase 3 — multi-provider router

`phase3_router/router.py` routes each task to **exactly one provider**:

- Background health checks with a cached result — never on the request path.
- Circuit breaker: opens after consecutive failures, skips the provider during
  cooldown, half-open trial, closes on success.
- EWMA latency preference among healthy providers.
- Sequential failover only; providers are never called in parallel.
- Per-attempt timeout derived from `remaining_deadline / remaining_providers`
  with a floor, split into connect and read budgets.

Providers live in `phase3_router/providers/`: an HTTP adapter plus two
controllable mock servers (force 503, slow, hang, random faults, label
override) and the optional real endpoint.

```bash
python -m pytest tests/phase3 -v
python benchmarks/router_overhead.py   # loopback-only, records run count
```

TC3.8 is measured over 5 runs with a bound of
`connect_timeout + read_timeout + one failover call + fixed slack`. The earlier
4028 ms regression was an accidental second call to the same hanging provider
(`TypeError` from timeout formatting caught as "provider lacks task_context").
Recorded breakdown and min/median/max are in `docs/measurements.md`.

## Phase 4 — conflict handling

Every task carries `task_id` (= Phase 2 `message_id`), attempt, frame timestamp
and deadline from the outbox through the broker, worker, router and provider.
Mock providers echo `task_id`; the router rejects a mismatch and logs it.

| Table | Purpose |
|---|---|
| `results(task_id PRIMARY KEY, provider, decision, claimed_at)` | Claimed with `INSERT … ON CONFLICT DO NOTHING`; first writer wins |
| `discarded(task_id, provider, reason, received_at)` | `duplicate`, `late`, `mismatch` — never dropped silently |
| `applied_ids(task_id PRIMARY KEY, frame_timestamp, frame_ts_num, decision, applied_at)` | Robot gate state; survives restarts |

The decision gate applies a decision only when it is not already applied, the
deadline has not passed, and no newer frame has already been decided. An
expired deadline yields a safe default; a later answer is logged as `late` and
never applied.

```bash
python -m pytest tests/phase4 -v
```

TC4.8 runs 1000 tasks through the worker + router + gate with injected random
500s, delays, duplicate deliveries and expired deadlines. Injected faults are
reported separately from organic observations, and injected expirations are not
reported as a late-answer rate. See `docs/measurements.md`.

TC4.10 (crash after inference, before ACK) and TC4.11 (edge process killed
mid-task, restarted in a separate process) run real subprocesses and verify the
same task is applied exactly once.

## Phase 5a — live cloud test harness

Phase 5a adds no pipeline features. It prepares and measures a real deployment
of the existing phases against cloud-hosted `real_server` instances.

- **`real_server/Dockerfile`** — CPU image; `MODEL_ID`, `DETECTION_THRESHOLD`
  and `API_TOKEN` come from the environment, weights are preloaded at build
  time by default (`PRELOAD_MODEL=0` defers to first start), and the
  healthcheck probes `/health`.
- **Token auth** — `/predict` requires `X-API-Token` matching `API_TOKEN`;
  missing/wrong values get `401`, and an unconfigured server returns `503`.
  `/health` returns only model and device. The client reads `API_TOKEN` and
  never logs it.
- **`live_run.py`** — webcam at a requested fps for a requested duration,
  through outbox → MQTT → worker → router → providers. It measures latency on
  the edge clock and writes `docs/live_results.md` with sent/acked/applied/
  safe-default/lost counts, duplicates, p50/p95/p99, achieved fps, max outbox
  depth, failover events and the recorded environment. `--soak` adds a
  memory/outbox CSV every 10 s.
- **`fault_drill.py`** — timed operator instructions (stop provider A, cut
  Wi-Fi for 20 s, kill the edge, both providers down for 60 s) with epoch
  timestamps and a CSV that joins to the latency log.
- **`docs/deploy.md`** — Google Cloud and Azure CPU VM setup, Docker install,
  firewall limited to your IP, budget alerts, teardown, and the plain-HTTP
  warning.

```bash
# Token (same value on the client and the container)
python -c "import secrets; print(secrets.token_urlsafe(32))"

# Build and run the real server image locally
docker build -f real_server/Dockerfile -t robotic-hand-real-server:5a .
docker run --rm -p 8001:8001 \
  -e API_TOKEN="$API_TOKEN" \
  -e MODEL_ID=PekingU/rtdetr_r18vd \
  -e DETECTION_THRESHOLD=0.5 \
  robotic-hand-real-server:5a

# Live run against one or two endpoints
export ROUTER_ENDPOINTS="http://VM_A:8001/predict,http://VM_B:8001/predict"
python live_run.py --duration 120 --fps 2 --soak \
  --network "home fibre, Wi-Fi 5" --vm-type "e2-standard-2" --region "us-central1"

# Timed fault sequence in a second terminal
python fault_drill.py
```

`--source images` exists only as a plumbing smoke test when no camera is
available; it is labelled as such in the generated report.

## Phase 5b — actuator layer and device protocol

Phase 5b connects the Phase 4 decision gate to a hand controller. The gate,
policy and audit log stay on the edge; the device only executes validated
commands.

- **Actuator interface** — `grip`, `release`, `move_to`, `stop`, `heartbeat`.
  `StubActuator` logs and ACKs immediately (default, no hardware),
  `SerialActuator` speaks the line protocol over USB serial, and `SimActuator`
  drives a PyBullet kinematic model (optional dependency).
- **Policy** — the first matching rule over `(label, score)` selects an
  action. An unknown label or a missing score falls through to `hold`, which
  sends nothing. Rules and thresholds are JSON
  (`actuator/policy.example.json`, loaded from `ACTUATION_POLICY_PATH` or
  `--policy`); invalid configuration fails at load, not mid-actuation.
- **At most one action per task** — `ActuationGrip` wraps the Phase 4
  `DecisionGate` with the same `try_apply` signature. It claims `task_id` in
  the new `actuation_log` table **before** sending, and the command id is
  derived from `task_id` (`cmd-` + SHA-256 prefix). A duplicate delivery, a
  second worker or an edge restart cannot actuate the same task twice. The
  trade-off is fail-safe: a crash after the claim but before the send leaves
  an unconfirmed row and no motion.
- **Device protocol** — `command_id|command|parameters|checksum` frames
  (FNV-1a 32-bit), `ACK`/`NACK` replies with a reason, angle 0–180° and speed
  1–100 % limits, a 32-command-id dedup window, boot-safe state, and a servo
  safe state after a heartbeat timeout. Full spec:
  [`docs/device_protocol.md`](docs/device_protocol.md).
- **`FakeDevice`** implements the same protocol in Python, so TC5b.6–TC5b.8
  (checksum/format/NACK reasons, dedup window, limits, safe state) run in
  pytest without hardware. `SerialActuator` is tested against it through the
  real framing and ACK-matching path.
- **`live_run.py`** records **capture → actuator ACK** latency on the edge
  clock, ACK/NACK/hold counts and percentiles in `docs/live_results.md`, plus
  one row per task (including holds) in `logs/live/<run>/actuation.csv`. `--actuator
  stub|sim|serial`, `--serial-port` and `--policy` select the backend; the
  default stub never moves anything.

```bash
python -m pytest tests/phase5b -q          # no hardware, no broker

# Live run with the logging stub actuator (never moves anything)
python live_run.py --duration 30 --fps 2 --actuator stub \
  --endpoints http://127.0.0.1:8001/predict

# Live run against a real controller; read docs/hardware.md first
export ACTUATION_SERIAL_PORT=COM5
python live_run.py --duration 30 --fps 1 --actuator serial \
  --policy actuator/policy.example.json --endpoints ...
```

`actuation_log` columns: `task_id` (primary key), `command_id`, `action`,
`parameters`, `acked`, `reason`, `detail`, `sent_at`, `acked_at`,
`latency_ms`. Holds are logged with no `command_id` and no `acked_at` because
nothing was sent. A row with a command id and no `acked_at` is an unconfirmed
command; it is reported, never retried automatically.

### Firmware and hardware status

> **The firmware sketch in [`firmware/`](firmware/) is untested on hardware.**
> It has never been compiled, flashed or run. The servo rail needs a separate
> supply and a physical e-stop that cuts servo power. Wiring and the first-run
> checklist are in [`docs/hardware.md`](docs/hardware.md). No physical robot
> was driven in this workspace, and TC5b.4/TC5b.5 skip without a port and an
> explicit opt-in.

## Tests

The offline suite needs no Docker, credentials or internet:
`tests/conftest.py` starts the deterministic mock server in a background
Uvicorn thread on a free loopback port.

```bash
python -m pytest -q
```

| Case | Test file | What is asserted |
|---|---|---|
| **TC1.1** | `test_tc1_1_single_request.py` | One valid image returns 200 with a prediction under 5 s; capture falls back to a sample or raises clearly |
| **TC1.2** | `test_tc1_2_batch.py` | 20 requests complete, CSV rows in order, avg/min/max match recomputation |
| **TC1.3** | `test_tc1_3_known_label.py` | Labelled samples return expected labels; repeats and unknown images are stable |
| **TC1.4** | `test_tc1_4_failure.py` | Timeout, refused connection and invalid payload are caught, logged, and the client continues |
| **TC2.1–2.6** | `phase2/` | 100 messages no loss, broker restart, edge restart, duplicate once, backoff, overhead (Docker) |
| **TC3.1–3.9** | `phase3/` | Healthy routing, failure/timeout failover, both fail, three providers, recovery, latency preference, measured failover bound, 50 concurrent tasks |
| **TC4.1–4.12** | `phase4/` | Context echo, claim race, discard reasons, mismatch rejection, 200 real claim-path trials, gate rules, safe default, 1000-task run, deadline timeout, crash/restart, late-after-B, edge kill/restart |
| **TC5b.1–5b.8** | `phase5b/` | Stub actuator contract, policy mapping/validation, gate→actuator once per task with `actuation_log`, protocol checksum/ACK/NACK, 32-id dedup, limits and heartbeat safe state; TC5b.4/5b.5 are hardware-gated |

### Latest run

```bash
# Full offline suite (skips real model, Docker and hardware tests)
SKIP_PHASE2_DOCKER_TESTS=1 python -m pytest -q
# 74 passed, 14 skipped, 0 failed

python -m pytest tests/phase2 -q
# 6 skipped — Docker not available
```

Skipped and why: 4 real-model tests (`RUN_REAL_MODEL!=1`), 1 Phase 3 and 6
Phase 2 Docker/broker tests, 2 Phase 5b hardware-gated tests, and the optional
PyBullet test.

## Measured numbers

Real, measured results only — no fabricated values. All sections are in
[`docs/measurements.md`](docs/measurements.md), including:

- Phase 1 loopback latency (above).
- Phase 2 reliability runs, where Docker was available.
- Phase 3 failover bound over 5 runs (min/median/max) and router overhead
  (loopback-only).
- Phase 4 claim races, discard counts by reason, deadline-derived timeout
  success rates before/after the circuit breaker opens, and the 1000-task run.

## Known limitations

- **Mock accuracy means nothing.** Labels come from a hash manifest; TC1.3
  verifies plumbing, not model quality.
- **Broker path tests need Docker.** They skip cleanly without it; the
  guarantee is limited to the tested failure modes.
- **Real-provider/model path is optional and unverified here.** The RT-DETR
  server exists, but its benchmark was not run in this environment (model
  download and Python/torch compatibility).
- **Camera path is simple.** One frame per request, no calibration; hardware
  integration is Phase 5c.
- **Firmware is untested.** The sketch in `firmware/` has never been compiled,
  flashed or run; the protocol tests exercise `FakeDevice`, not the C++.
- **No actuator hardware was run.** TC5b.4/5b.5 skip without a serial port and
  explicit opt-in, and the `SimActuator` path was not exercised here because
  `pybullet` is not installed.
- **An ACK is not a finished motion.** The device ACKs command acceptance, so
  capture→ACK latency does not include the physical move.
- **The e-stop is hardware.** `stop` and the heartbeat safe state are software
  behaviours and must not replace a physical switch that cuts servo power.
- **Latency depends on host and frame size**, so compare runs on the same host.
- **Mock providers over loopback** are used for Phase 3/4 measurements; they
  demonstrate routing/conflict behaviour, not WAN performance.

## Next phase

Phase 5c is field integration: compile and flash the firmware, calibrate servo
angles, complete the first-run safety checklist in `docs/hardware.md`, then
connect the real camera and hand and measure capture → actuator ACK on the
mechanism.
