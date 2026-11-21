# Testing

Tests are split into six tiers. Only the offline tier runs without Docker,
credentials, hardware, a webcam or a model download.

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
93 passed, 16 skipped
```

Skipped: 4 real-model tests, 6 Phase 2 broker tests, 1 Phase 3 broker test,
2 Phase 5b hardware-gated tests, 1 optional PyBullet test, 1 Phase 6 webcam
test and 1 optional Playwright screenshot test.

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

## 4. Phase 6 dataset and training tier

This tier is manual because it needs a webcam, physical parts and (for a real
run) a GPU. The offline suite covers the split logic and the classification
serving contract with a fake classifier; it does not collect photos or train a
model.

Capture one class at a time. The tool writes `data/<class>/<session>_<n>.jpg`
and prints live per-class counts. `data/` is gitignored.

```bash
python tools/capture_dataset.py bolt session01
python tools/capture_dataset.py nut session02
```

Split by session, not by random file. This fails on duplicate paths, a session
in two splits, a class below the minimum, or fewer than three sessions/class.

```bash
python tools/split_dataset.py --data-dir data --manifest data/manifest.csv
```

Train on a free Colab/Kaggle GPU, or use `--smoke` for a CPU-only plumbing
check. Training writes `metrics.csv` and `best/`; it never reads the test split.

```bash
pip install -r requirements-training.txt
python -m training.train --config config/train.classification.json --smoke
python -m training.train --config config/train.classification.json
```

Evaluate the locked test set once and write the model card:

```bash
python -m training.evaluate \
  --checkpoint training/runs/classifier/best \
  --manifest data/manifest.csv --data-dir data
```

`docs/model_card.md` is a template until this command runs. The repository
contains no captured dataset, no checkpoint and no accuracy number.

To exercise classification serving manually:

```bash
TASK=classification MODEL_ID=microsoft/resnet-18 \
  API_TOKEN=replace-with-a-real-token python -m real_server --port 8001

curl -s -X POST --data-binary @images/cat.jpg \
  -H 'Content-Type: image/jpeg' \
  -H 'X-API-Token: replace-with-a-real-token' \
  -H 'X-Task-Id: manual-1' \
  http://127.0.0.1:8001/predict
```

A top score below `CLASSIFICATION_THRESHOLD` (default `0.5`) returns
`"prediction": "unknown"`. Load `config/policy.classification.example.json`
through `ACTUATION_POLICY_PATH` to map the example labels; its default is
`hold`.

## 5. Dashboard tier (offline)

The dashboard tests start the FastAPI app with the deterministic demo source.
They need no browser, camera, broker or network. They cover the API shapes for
state, latency, events, actuation and frames; the exact demo banner; token
gating and token absence from bodies/logs; loopback default and remote-bind
refusal; validated run parameters and unknown-field rejection; mock-fault
confirmation and non-mock refusal; and the software-stop label.

```bash
python -m pytest tests/test_ui_dashboard.py tests/test_ui_live_source.py -q
# 13 passed, 1 skipped
python -m ui --demo --port 8080
```

`tests/test_ui_live_source.py` writes a temporary SQLite/CSV/JSONL run snapshot
and checks the read-only parser. It does not start a real live run.

The optional Playwright screenshot test skips when the package is absent:

```bash
pip install playwright
python -m playwright install chromium
python -m pytest tests/test_ui_dashboard.py -q
```

A real demo screenshot was captured in this workspace with headless Chrome at
`docs/screenshots/dashboard-demo.png`; it is demo data, not a measurement.

## 6. Hardware-gated tier

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
| TC6.1 | session-based split, manifest, duplicate and minimum-count failures |
| TC6.2 | webcam capture (manual; skipped without a camera and physical parts) |
| TC6.3 | classification HTTP contract with a tiny fake classifier |
| TC6.4 | low top score → `unknown`; policy `hold` |
| UI | dashboard APIs, token gate, bind rules, run/mock/stop controls, live artifact parsing |
| REAL-MODEL | optional RT-DETR endpoint |

## What the suite does not verify

- Real cloud latency or availability.
- Real hardware motion, calibration or firmware compilation.
- Multi-machine SQLite consistency.
- TLS or token rotation for `real_server`.
- Losslessness outside the failure modes listed in
  [ARCHITECTURE.md](ARCHITECTURE.md).
- Real classifier accuracy: no dataset was captured, no training ran, and the
  model card contains no accuracy number. The offline tests use a fake model.
- Webcam capture: TC6.2 is skipped because it needs a camera and a person
  placing parts.
- A real live run started from the dashboard, or a real camera feed in the live
  view. The UI tests use demo data and a temporary artifact snapshot.
- Browser automation: the Playwright screenshot test is skipped because the
  package is not installed. The committed screenshot is a manual headless-Chrome
  run of the demo page.
- That `POST /api/stop` cuts servo power. It is a software stop, not an
  emergency stop.
