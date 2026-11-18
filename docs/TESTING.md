# Testing

Tests are split into four tiers. Only the offline tier runs without Docker,
credentials, hardware or a model download.

## 1. Offline tier

This is the default suite. `tests/conftest.py` starts the deterministic mock
inference server in a background Uvicorn thread on a free loopback port, so no
external service is needed.

Windows PowerShell (executed in this workspace):

```powershell
python -m venv .venv
python -m pip install -r requirements.txt
Copy-Item .env.example .env
$env:SKIP_PHASE2_DOCKER_TESTS = "1"
python -m pytest -q
```

Bash/macOS/Linux:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
SKIP_PHASE2_DOCKER_TESTS=1 python -m pytest -q
```

`SKIP_PHASE2_DOCKER_TESTS=1` skips the six Phase 2 broker tests and the Phase 3
broker integration test cleanly. The real-model, hardware-gated and optional
PyBullet tests also skip.

To exclude the large-download and optional simulation markers explicitly:

```bash
python -m pytest -q -m "not real_model and not pybullet"
```

Check the README test table against collected tests:

```bash
python -m scripts.check_docs
```

Current offline result in this workspace (Windows 11, Python 3.14.5):

```text
74 passed, 14 skipped
```

Skipped: 4 real-model tests, 6 Phase 2 broker tests, 1 Phase 3 broker test,
2 Phase 5b hardware-gated tests and 1 optional PyBullet test.

## 2. Broker tier (Docker)

This tier needs Docker with the Compose plugin. It starts Mosquitto from
`docker-compose.yml` and runs the Phase 2 broker tests and the Phase 3
worker/router/outbox integration test. Phase 2 tests need the mock inference
server on port 8000.

```bash
docker compose up -d

python -m mock_server --port 8000 &
MOCK_PID=$!
for i in $(seq 1 30); do
  curl -sf http://127.0.0.1:8000/health && break
  sleep 1
done

python -m pytest tests/phase2 tests/phase3 -q -m "not real_model and not pybullet"

kill "$MOCK_PID"
docker compose down
```

Use the broker tier without `SKIP_PHASE2_DOCKER_TESTS`; leave the variable
unset. These commands were not run in this workspace because the Docker daemon
was unavailable. The GitHub Actions workflow runs them on every push and pull
request.

## 3. Real-model tier

This tier downloads and runs `PekingU/rtdetr_r18vd` through `real_server`. It is
large and slow, so it is opt-in and marked `real_model`.

```bash
pip install -r requirements-real.txt
python -m real_server --port 8001

# in another shell
export REAL_ENDPOINT=http://127.0.0.1:8001/predict
export RUN_REAL_MODEL=1
python -m pytest tests/test_real_model.py -q
```

PowerShell:

```powershell
$env:REAL_ENDPOINT = "http://127.0.0.1:8001/predict"
$env:RUN_REAL_MODEL = "1"
python -m pytest tests/test_real_model.py -q
```

Without `RUN_REAL_MODEL=1`, all four tests skip. The real-model benchmark in
[measurements.md](measurements.md) records 0 completed requests: the server did
not start in this environment, so no real inference latency is claimed.

## 4. Hardware-gated tier

No test moves real hardware by default. The hardware cases only handshake,
send `stop`, or check duplicate-stop idempotence.

```bash
export ACTUATION_SERIAL_PORT=COM5      # or /dev/ttyUSB0
export ACTUATION_HARDWARE_TEST=1       # TC5b.4
export ACTUATION_FIRMWARE_TEST=1       # TC5b.5 as well
python -m pytest tests/phase5b/test_tc5b_actuator.py -q
```

PowerShell:

```powershell
$env:ACTUATION_SERIAL_PORT = "COM5"
$env:ACTUATION_HARDWARE_TEST = "1"
python -m pytest tests/phase5b/test_tc5b_actuator.py -q
```

Read [hardware.md](hardware.md) before connecting a servo rail. The firmware in
`firmware/` has never been compiled, flashed or run; these tests are skipped in
this workspace.

## Optional simulation tier

`SimActuator` uses PyBullet. Install the optional dependency and run the marked
test:

```bash
pip install -r requirements-hardware.txt
python -m pytest tests/phase5b/test_tc5b_actuator.py -q -m pybullet
```

If `pybullet` is not installed, the test skips. The CI workflow deselects it.

## Test ID map

The README test table is the authoritative ID-to-file map, checked by
`python -m scripts.check_docs`. Test IDs in code map as follows:

| Range | Area |
|---|---|
| TC1.1–TC1.4 | baseline capture, HTTP, known labels, failures |
| TC2.1–TC2.6 | reliable delivery (broker tier) |
| TC3.1–TC3.9 | routing, failover, latency preference, concurrency |
| TC3-integration | worker + router + outbox + broker |
| TC3-unit | circuit breaker and health-thread unit tests |
| TC4.1–TC4.12 | task context, claims, discards, gate, deadline, crash/restart |
| TC5a | token enforcement and client/provider token handling |
| TC5b.1–TC5b.8 | actuator, policy, protocol, dedup, limits, safe state |
| TC5b-live | capture-to-ACK CSV join |
| REAL-MODEL | optional RT-DETR endpoint |

## What the suite does not verify

- Real cloud latency or availability.
- Real hardware motion, calibration or firmware compilation.
- Multi-machine SQLite consistency.
- TLS or token rotation for `real_server`.
- Losslessness outside the failure modes listed in
  [ARCHITECTURE.md](ARCHITECTURE.md).
