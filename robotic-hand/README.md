# robotic-hand — Phase 1: baseline cloud pipeline

A low-cost robotic hand for small factories that cannot afford a local AI
server. An edge client captures a camera frame, sends it to a cloud inference
endpoint, and gets a decision back. This folder is the phase-by-phase build of
that system.

**Phase 1 proves one thing:** a camera frame reaches a cloud endpoint, a
decision comes back, and the round trip is measured. There is deliberately no
retry, no queue, no broker and no multi-provider routing yet.

## Status

| Phase | Scope | Status |
|---|---|---|
| **1** | Baseline edge → cloud pipeline with measured latency | **Complete** |
| **2** | Reliable delivery: MQTT (Mosquitto), SQLite outbox, QoS 1, ACKs, retries with backoff, dedup | **Complete (tests written, broker-dependent)** |
| **3** | Multi-provider router with health checks and failover | **Complete** |
| 4 | End-to-end task IDs, first-valid-answer-wins | Not started |
| 5 | Robot hardware integration | Not started |

Test status for Phase 1: **15 passed, 0 failed** (`python -m pytest -q`, offline).

## How it works

```
images/ (or camera)
      │  capture.py: OpenCV frame, JPEG bytes
      ▼
phase1_pipeline/capture.py ── Frame(name, jpeg_bytes, width, height)
      │
      ▼
phase1_pipeline/client.py ── POST raw JPEG, timeout, perf_counter round trip
      │
      ▼
mock_server/app.py (FastAPI) ── deterministic label for known images
      │
      ▼
InferenceResult(ok, latency_ms, prediction, error_kind)
      │
      ▼
phase1_pipeline/benchmark.py ── one CSV row per request + avg/min/max
      │
      ▼
docs/measurements.md ── Phase 1 section updated with the real numbers
```

The mock server returns the label declared for each sample image in
`images/labels.json`, keyed by the SHA-256 of the file. It is a deterministic
stand-in for a hosted model: it makes the tests offline and reproducible, and
it is clearly not an accuracy benchmark.

## Project structure

```
robotic-hand/
├── README.md
├── .env.example
├── .gitignore
├── requirements.txt
├── pytest.ini
├── docker-compose.yml        # Mosquitto broker for Phase 2
├── phase2_reliability/      # Outbox, publisher, worker, ACKs, retries
├── mosquitto/               # Mosquitto config
├── images/
│   ├── banana.jpg
│   ├── cat.jpg
│   ├── dog.jpg
│   └── labels.json           # expected labels used by TC1.3
├── mock_server/
│   ├── __init__.py
│   ├── app.py                # deterministic /predict and /predict/slow
│   └── __main__.py           # python -m mock_server
├── phase1_pipeline/
│   ├── __init__.py
│   ├── config.py             # all environment access and validation
│   ├── capture.py            # camera with sample-image fallback
│   ├── client.py             # HTTP POST, timeout, latency, structured errors
│   └── benchmark.py          # N requests, CSV, avg/min/max, measurements doc
├── tests/
│   ├── conftest.py           # in-process mock server on a free loopback port
│   ├── test_tc1_1_single_request.py
│   ├── test_tc1_2_batch.py
│   ├── test_tc1_3_known_label.py
│   └── test_tc1_4_failure.py
├── docs/
│   └── measurements.md       # real measured numbers
└── logs/                     # generated CSV (gitignored)
```

## Setup

Python 3.10 or newer is required; it was verified on 3.14.5.

```bash
cd robotic-hand

python -m venv .venv
# Windows (Git Bash / PowerShell):
source .venv/Scripts/activate
# macOS / Linux:
# source .venv/bin/activate

python -m pip install --upgrade pip
pip install -r requirements.txt

cp .env.example .env
```

`requirements.txt` pins `fastapi`, `uvicorn`, `opencv-python`, `numpy`,
`requests`, `python-dotenv` and `pytest`. No API key is needed for Phase 1.

## Configuration

All settings come from environment variables (or `.env`, which is gitignored);
`.env.example` documents every one. Nothing is hard-coded, no secret is stored
in source, and the API key is never written to a log or CSV row.

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

### Using a real provider (optional)

Set `INFERENCE_MODE=real`, `INFERENCE_ENDPOINT` to the provider's documented
URL, and `INFERENCE_API_KEY`. The client sends **raw JPEG bytes** with
`Content-Type: image/jpeg` and a `Bearer` token, then accepts either
`{"label": ..., "confidence": ...}` or the list shape
`[{"label": ..., "score": ...}]`.

Provider APIs change and differ from each other, so no hosted endpoint is
assumed here — check the provider's current docs and point `INFERENCE_ENDPOINT`
at the right route. Real-provider latency is **not** measured in this phase;
`docs/measurements.md` contains mock-server numbers only, labelled as such.

## Run it

Two terminals. Start the mock inference server first:

```bash
cd robotic-hand
python -m mock_server --port 8000
# check: curl http://127.0.0.1:8000/health
```

Then run the pipeline:

```bash
# One frame, print the prediction and latency (uses the camera if present)
python -c "from phase1_pipeline.capture import capture_once; \
from phase1_pipeline.client import CloudClient; \
f = capture_once('images', camera_enabled=False); \
r = CloudClient('http://127.0.0.1:8000/predict').predict(f.jpeg_bytes, f.name); \
print(r.label, r.latency_ms, 'ms')"

# 20 requests: CSV rows, then avg/min/max, and update docs/measurements.md
python -m phase1_pipeline.benchmark --count 20 --no-camera
```

`--no-camera` skips the webcam probe. Without it, capture tries `CAMERA_INDEX`
first and falls back to `images/` when no device is available.

Stop the mock server with `Ctrl+C` in its terminal.





## Real model endpoint

This project can run a real object-detection server alongside the mock server. The client switches via the existing `INFERENCE_ENDPOINT` environment variable (no hardcoded URLs).

### Install (real model)
```bash
cd robotic-hand
pip install -r requirements-real.txt
```

### Run
In one terminal:
```bash
python -m real_server --port 8001
# or: MODEL_ID=PekingU/rtdetr_r50vd python -m real_server --port 8001
```
In another terminal, point the client/benchmark to it:
```bash
# mock (default)
# INFERENCE_ENDPOINT=http://127.0.0.1:8000/predict

# real
export INFERENCE_ENDPOINT=http://127.0.0.1:8001/predict  # or set in .env
python -m phase1_pipeline.benchmark --count 20 --no-camera
```

### Environment variables
| Variable | Default | Purpose |
|---|---|---|
| `MODEL_ID` | `PekingU/rtdetr_r18vd` | HuggingFace model for `pipeline("object-detection")`. Can swap to `rtdetr_r50vd`. |
| `DETECTION_THRESHOLD` | `0.5` | Confidence threshold for detections. |
| `INFERENCE_ENDPOINT` | `http://127.0.0.1:8000/predict` | Target endpoint (mock or real). |

### API contract
- `POST /predict` accepts raw JPEG bytes (`Content-Type: image/jpeg`), same contract as mock.
- Returns top label as `prediction` (also `label` for compatibility), `detections` list (`label`, `score`, `box`), and `server_inference_ms` measured around the model call only.
- If nothing is detected above threshold, returns a valid response with `prediction` `"none"` (not an error).
- `GET /health` reports `status`, `model`, `device` (CUDA if available else CPU), `threshold`.

### Tests (real model)
```bash
RUN_REAL_MODEL=1 REAL_ENDPOINT=http://127.0.0.1:8001/predict python -m pytest tests/test_real_model.py -v
```
Tests are marked `real_model` and skipped unless `RUN_REAL_MODEL=1` is set (model download is large).

**Limitation:** The model is COCO-trained and does not know factory-specific parts.

## Phase 2: Reliable delivery

### How to run (MQTT broker)
```bash
cd robotic-hand
docker compose up -d
# start mock server in another terminal
python -m mock_server --port 8000
```

### Architecture (Phase 2)
- **Outbox (SQLite)**: written as `pending` BEFORE any send attempt (`phase2_reliability/outbox.py`). Survives restarts.
- **Publisher (edge)**: publishes with MQTT QoS 1, subscribes to ACK topic, marks `acked` only on ACK. Resends unacked with exponential backoff (1s, 2s, 4s, capped at 30s). Offline: keep writing to outbox, drain oldest-first on reconnect.
- **Worker (cloud)**: subscribes, checks `processed_ids` (SQLite), skips duplicates but re-sends ACK, calls Phase 1 client, publishes result + ACK.
- **Message format**: JSON with `message_id` (UUID4), `created_at`, `payload` (base64 JPEG, resized/compressed to under ~100 KB).

### Tests (TC2.1–TC2.6)
```bash
python -m pytest tests/phase2 -v
```
Tests use scripted failures (broker stop/start via docker, killing/restarting edge processes). **Guarantee**: "no message loss in the tested failure modes".

Results are recorded in `docs/measurements.md` under the Phase 2 section.

## Tests

The whole suite runs offline. `tests/conftest.py` starts the mock server in a
background Uvicorn thread on a free loopback port, so no Docker, no credentials
and no internet are involved.

```bash
cd robotic-hand
python -m pytest -q
```

| Case | Test | What is asserted |
|---|---|---|
| **TC1.1** | `test_tc1_1_single_request.py` | One valid image returns HTTP 200 with a prediction, in **under 5 s** (measured). Capture falls back to a sample when no camera exists and raises a clear error when neither source exists. |
| **TC1.2** | `test_tc1_2_batch.py` | 20 back-to-back requests all complete, every row lands in the CSV in order, and avg/min/max in the log match statistics recomputed from the file. The measurements writer replaces its own section instead of duplicating it. |
| **TC1.3** | `test_tc1_3_known_label.py` | Each labelled sample returns its expected label; repeated and unknown images return a stable label. |
| **TC1.4** | `test_tc1_4_failure.py` | A 200 ms timeout against a slow endpoint, a refused loopback connection and an invalid payload are all caught and recorded (CSV rows with `ok=False`), the benchmark keeps going, and the client succeeds on the next request. |

Result of the last run in this workspace (Windows 11, Python 3.14.5):

```
15 passed in 4.25s
```

## Measured baseline

From a real 20-request run against the local mock server; the full record is in
[`docs/measurements.md`](docs/measurements.md):

| Metric | Value |
|---|---|
| Requests | 20 |
| Completed / failed | 20 / 0 |
| Average round trip | 14.992 ms |
| Minimum | 3.342 ms |
| Maximum | 50.853 ms |

The maximum is dominated by `dog.jpg` (~500 KB), whose upload and SHA-256 cost
more than the smaller frames; the minimum is a small frame on a warm connection.
**These are loopback mock numbers, not cloud numbers.** No hosted provider was
called, so no real-network latency is claimed.

## Known limitations

- **Mock accuracy means nothing.** The mock server looks labels up by image
  hash. TC1.3 verifies the plumbing (bytes out, JSON back, labels match the
  manifest), not model quality.
- **No delivery guarantee.** One POST, one timeout, and the result is
  abandoned on failure. There is no retry, queue, ACK or deduplication — that
  is Phase 2.
- **Real-provider path is unverified.** It is configurable and implemented
  generically, but no hosted API was called in this phase.
- **One image per request, no batching or compression.** The large sample frame
  is sent at full size; Phase 2 adds resizing/compression.
- **Camera path is simple.** A frame is grabbed per request with a short warm-up
  and no calibration; hardware integration is Phase 5.
- **Latency depends on the machine and the frame size**, so compare runs on the
  same host.

## Next phase

Phase 2 adds reliable delivery: a Mosquitto broker via `docker-compose.yml`, a
SQLite outbox written before every send, MQTT QoS 1, ACK-driven `acked` state,
exponential backoff and restart recovery. It is intentionally not built yet.


## Phase 3: Multi-provider router

- **Router**: `phase3_router/router.py` routes each task to **exactly one provider**. Health checks skip unhealthy providers; on failure/timeout it fails over to the next in order.
- **Providers**: `phase3_router/providers/` includes HTTP adapter and two controllable mock providers (`mock_provider_a`, `mock_provider_b`) that can be made slow or fail on command (via control endpoints/env) for scripted tests. **Real endpoint** included as a third option.
- **Tests**: `tests/phase3/test_tc3_router.py` (TC3.1–TC3.5) cover healthy routing, failover on failure, failover on timeout, both providers fail, and three-provider configuration including real endpoint.

```bash
python -m pytest tests/phase3 -v
```

Results recorded in `docs/measurements.md` under Phase 3 (mock vs real labelled).
