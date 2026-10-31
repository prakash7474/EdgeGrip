# Phase 1 — Cloud AI Communication Prototype

Python prototype for the robotic hand's perception path: read a local image,
send it to **one** hosted AI inference API, validate the prediction, measure
round-trip latency, and record every attempt — successes and failures — to CSV.

This is the executable Phase 1 slice of the larger [robotic hand project](../README.md).
Multi-cloud routing, retries and buffering are deliberately **out of scope**.

| Item | Value |
|---|---|
| Provider (Phase 1 only) | Hugging Face Inference Providers (`hf-inference` router) |
| Default model | `google/vit-base-patch16-224` (ImageNet-1k image classification) |
| Endpoint | `https://router.huggingface.co/hf-inference/models/{model_id}` |
| Auth | `Authorization: Bearer $HF_API_TOKEN` |
| Payload | Raw JPEG bytes, `Content-Type: image/jpeg` |
| Response | JSON array of `{"label": str, "score": float}` |

The endpoint and payload follow the current Hugging Face Inference Providers
documentation; no endpoints or response fields are invented here.

## Table of contents

- [Architecture](#architecture)
- [Project structure](#project-structure)
- [Technology stack](#technology-stack)
- [Installation (Windows 11 + VS Code)](#installation-windows-11--vs-code)
- [Environment configuration](#environment-configuration)
- [Add your own test images](#add-your-own-test-images)
- [Run the prototype](#run-the-prototype)
- [Run the four test cases](#run-the-four-test-cases)
- [Performance report](#performance-report)
- [Testing](#testing)
- [Verification status](#verification-status)
- [Current limitations](#current-limitations)
- [Roadmap](#roadmap)
- [Troubleshooting](#troubleshooting)

## Architecture

```
Local image (images/test_*.jpg)
        |
        v
Image loader + validation        image_loader.py   (OpenCV: exists, decodes, JPEG-encodes)
        |
        v
Task ID generator                cloud/inference.py (task-<uuid4>)
        |
        v
Cloud communication module       cloud/inference.py (HuggingFaceProvider)
        |
        v
Hosted AI inference API          router.huggingface.co/hf-inference
        |
        v
JSON response -> validation      validate_prediction()  (checks label/score shape)
        |
        v
Round-trip latency               time.perf_counter() around the HTTP call
        |
        v
CSV record                       request_log.py -> logs/results.csv
        |
        v
Metrics + report                 reporting.py -> logs/performance_report.md
```

Design boundaries:

- `config.py` — all environment access and validation, nothing else.
- `image_loader.py` — local file checks and canonical JPEG encoding.
- `cloud/inference.py` — the reusable `send_image_to_cloud()` pipeline plus a
  provider adapter. A new cloud (AWS, Azure, Google Cloud) is a new class
  implementing `classify()`; the pipeline does not change.
- `request_log.py` — append-only CSV, one row per attempt.
- `reporting.py` / `label_matching.py` — aggregation and labelled-accuracy scoring.
- `main.py` — CLI only; no business logic.

Errors are returned as structured `InferenceResult` objects rather than raised,
so one failed request never stops a batch. The API token is never written to
the CSV, the report, or an error message.

## Project structure

```
robot-cloud-ai/
├── images/
│   ├── test_1.jpg          # cat
│   ├── test_2.jpg          # husky
│   ├── test_3.jpg          # banana
│   └── labels.json         # expected labels used by TC1.3
├── cloud/
│   ├── __init__.py
│   └── inference.py        # send_image_to_cloud(), HuggingFaceProvider
├── tests/
│   ├── conftest.py
│   ├── test_single_request.py
│   ├── test_multiple_requests.py
│   ├── test_prediction.py
│   └── test_timeout.py
├── logs/
│   ├── results.csv          # created/updated at runtime
│   └── performance_report.md
├── main.py                  # CLI: single | batch | validate | timeout | all | report
├── config.py
├── image_loader.py
├── label_matching.py
├── request_log.py
├── reporting.py
├── pytest.ini
├── requirements.txt
├── .env.example
├── .env                     # local only, gitignored
├── .gitignore
└── README.md
```

## Technology stack

| Component | Technology |
|---|---|
| OS | Windows 11 |
| IDE | Visual Studio Code |
| Language | Python 3.10+ (verified on 3.14.5) |
| Image processing | OpenCV (`opencv-python`) |
| HTTP | `requests` |
| Environment variables | `python-dotenv` |
| Inference | Hugging Face hosted Inference Providers |
| Logging | CSV + Python `logging` |
| Tests | `pytest` |

No local model is deployed and no AI server is created.

## Installation (Windows 11 + VS Code)

Open **PowerShell** in the repository root and run:

```powershell
cd .\robot-cloud-ai

# Create and activate a virtual environment
py -3.12 -m venv .venv
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\.venv\Scripts\Activate.ps1

# Install dependencies
python -m pip install --upgrade pip
pip install -r requirements.txt
```

> `py -3.12` assumes Python 3.12 is installed. Any Python 3.10+ works; use
> `python -m venv .venv` if the launcher is unavailable.

In VS Code: **File → Open Folder →** `robot-cloud-ai`, then select the
`.venv` interpreter (`Ctrl+Shift+P` → *Python: Select Interpreter*).
The tests are discovered from `tests/` via `pytest.ini`.

## Environment configuration

```powershell
Copy-Item .env.example .env
notepad .env
```

Create a token at <https://huggingface.co/settings/tokens> with
**Inference Providers** permission, then set it in `.env`:

```dotenv
HF_API_TOKEN=hf_xxxxxxxxxxxxxxxxxxxxxxxx
HF_MODEL_ID=google/vit-base-patch16-224
HTTP_CONNECT_TIMEOUT_SECONDS=5
HTTP_READ_TIMEOUT_SECONDS=30
LATENCY_TARGET_MS=5000
```

Rules enforced by `config.py`:

- The token is read from `.env` **or** the process environment (environment wins).
- `.env` is listed in `.gitignore`; only `.env.example` is committed.
- A missing token stops the command with a clear message and exit code `2`.
- Timeouts and the latency target must be positive numbers.
- `HF_ENDPOINT_URL` can override the full endpoint if the provider changes it.

One-off PowerShell override without editing `.env`:

```powershell
$env:HF_API_TOKEN = "hf_xxxxxxxxxxxxxxxxxxxxxxxx"
python main.py single
Remove-Item Env:\HF_API_TOKEN
```

## Add your own test images

1. Drop `.jpg`, `.jpeg`, `.png`, `.bmp` or `.webp` files into `images/`.
2. To grade them in TC1.3, add an entry to `images/labels.json`:

```json
{
  "model_id": "google/vit-base-patch16-224",
  "images": {
    "bolt.jpg": { "expected_label": "screw" }
  }
}
```

`expected_label` must be a label the chosen model can actually output — the
default ViT model uses the ImageNet-1k taxonomy. Comparison is a documented
token heuristic (`label_matching.py`): it accepts `"tabby, tabby cat"` for
`"cat"` and `"tiger cat"` for `"cat"`, but it will not map unrelated
taxonomies. If you change `HF_MODEL_ID`, update both the labels and the
manifest's `model_id`.

The three bundled images are real photographs (Wikimedia Commons / PyTorch
example image), so the prototype is not classifying synthetic noise.

## Run the prototype

All commands run from `robot-cloud-ai/` with the virtual environment active.

```powershell
# One image (defaults to images\test_1.jpg)
python main.py single

# One specific image
python main.py single --image images\test_2.jpg

# 20 sequential requests (TC1.2), cycling through the three images
python main.py batch --count 20

# Labelled prediction check for every entry in images/labels.json (TC1.3)
python main.py validate

# Controlled failure: closed local port + 50 ms timeout (TC1.4)
python main.py timeout --seconds 0.05

# Everything: clears the log, runs TC1.1–TC1.4, writes the report
python main.py all --count 20

# Rebuild the report from the existing CSV without sending requests
python main.py report
```

Exit codes: `0` behaviour observed, `1` a test expectation failed, `2`
configuration problem.

`all` clears `logs/results.csv` first so the report describes one coherent run.
Individual commands append to the log.

## Run the four test cases

| Case | Command | Pass condition |
|---|---|---|
| **TC1.1** Single valid image | `python main.py single` | Valid prediction, latency measured, row logged. The 5 s target is a benchmark, not an SLA — exceeding it logs `OVER` but still returns success. |
| **TC1.2** Multiple requests | `python main.py batch --count 20` | All 20 attempts logged; average/min/max latency and success percentage computed; failures counted, never dropped. |
| **TC1.3** Prediction validation | `python main.py validate` | Each labelled image compared with its expected label; prints PASS/FAIL and accuracy. Exit `1` if any label mismatches. |
| **TC1.4** Network failure | `python main.py timeout --seconds 0.05` | Failure captured and logged without a crash, then a normal request proves the client can continue. |

TC1.4 never disables the machine's network. It points at the closed local port
`http://127.0.0.1:9` with a deliberately tiny timeout. If `HF_API_TOKEN` is set,
step 2 sends a real recovery request; if not, step 2 is skipped with a warning
and the failure-handling result still stands.

Run all four in order:

```powershell
python main.py all
```

## Performance report

`logs/performance_report.md` is regenerated by `all` or `report`. It contains:

| Metric | Meaning |
|---|---|
| Total requests | Attempts recorded in the log |
| Successful requests | Usable prediction returned |
| Failed requests | Timeout, connection, auth, HTTP or invalid-response failures |
| Success rate | Successful / total × 100 |
| Average latency | Mean round trip of **successful** requests |
| Minimum latency | Fastest successful round trip |
| Maximum latency | Slowest successful round trip |
| Prediction accuracy | Correct labelled predictions / labelled cases × 100 |

**Success rate and accuracy are different measurements.** Success rate asks
"did a usable answer arrive?" Accuracy asks "was that answer right?" A batch can
be 100% successful and still be wrong; failed requests are excluded from the
accuracy denominator but still count as attempts.

Latency is the full client-side round trip measured with
`time.perf_counter()` around the HTTP call: TLS setup, network transit, provider
queueing and model execution. It is not the provider's reported inference time.
A 5,000 ms target is a prototype benchmark.

The report currently on disk was generated by the verification run described
below, which contains only a forced network failure — so it shows 0% success and
"not measured" accuracy on purpose:

```markdown
| Total requests | 1 |
| Successful requests | 0 |
| Failed requests | 1 |
| Success rate | 0.0% |
| Prediction accuracy | not measured (no labelled records) |
```

After a token-backed run, the same table fills with live latency and accuracy.

## Testing

Offline suite (no token, no network, deterministic fake provider):

```powershell
python -m pytest -q
```

The suite covers request construction, unique task IDs, CSV format and secret
hygiene, 20-request aggregation, failure accounting, label matching, malformed
responses, timeouts, and continuation after failure.

Live check against the real API (requires a valid token):

```powershell
$env:RUN_LIVE_TESTS = "1"
python -m pytest -q -m live
Remove-Item Env:\RUN_LIVE_TESTS
```

## Verification status

Verified in this workspace (Windows, Python 3.14.5):

- `python -m compileall` — exit 0.
- `python -m pytest -q` — **34 passed, 1 skipped** (the live test, skipped
  because no token is configured).
- `python main.py timeout --seconds 0.05` — captured a real timeout
  (`ConnectTimeoutError` to `127.0.0.1:9`) in 66.43 ms, wrote the CSV row, and
  exited 0 without crashing.
- `python main.py report` — generated `logs/performance_report.md`.
- `python main.py single` with no token — clear configuration error, exit 2.

**Not yet executed:** live TC1.1, TC1.2, TC1.3 against Hugging Face, because no
API token is available in this environment. Those results are not claimed.
To complete the Definition of Done, set `HF_API_TOKEN` and run:

```powershell
python main.py all
```

## Current limitations

- **Single provider.** Phase 1 intentionally talks only to Hugging Face. The
  provider adapter exists so Phase 3 can add others, but no routing exists yet.
- **No delivery guarantees.** A request is one HTTP POST. There is no retry,
  no local buffer, no acknowledgement and no idempotency — Phase 2 and 4 scope.
- **Best-effort request only.** A timeout is logged and abandoned; the task is
  not requeued.
- **Small accuracy sample.** Accuracy comes from whatever labelled images are
  in `images/` (three by default). It is a smoke check, not a benchmark.
- **Label matching is heuristic.** Token containment handles ImageNet aliases
  and taxonomy width; it does not understand synonyms outside the model's list.
- **CPU-hosted model.** `hf-inference` serves small models on CPU. Expect
  variable latency and occasional `503` while a model loads.
- **Local storage.** Logs are flat CSV files with no rotation or concurrency
  control.
- **Nothing robot-specific.** No camera, sensors, actuators or safety logic.

## Roadmap

Phase 1 (this folder) is the baseline for:

- **Phase 2 — Reliable communication:** persistent local task buffer,
  acknowledgements, bounded retries with backoff, task status tracking,
  duplicate suppression.
- **Phase 3 — Multi-cloud routing:** additional provider adapters, health
  checks, latency- and cost-aware routing, automatic failover.
- **Phase 4 — Distributed response management:** idempotency keys, duplicate
  response detection, task ownership, conflict resolution.
- **Phase 5 — Physical robot integration:** camera and sensor capture, hand
  controller, actuator control, safety mechanisms, end-to-end testing.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `Configuration error: No API token found` | `.env` missing or `HF_API_TOKEN` empty. Copy `.env.example` and fill it in. |
| HTTP 401 / 403 | Token lacks **Inference Providers** permission, or the model is gated. Create a new token or accept the model terms. |
| HTTP 503 `unavailable` | Model is cold/loading at the provider. Retry manually; Phase 2 adds backoff. |
| HTTP 429 `rate_limited` | Free-tier rate limit. Reduce the batch count or wait. |
| `prediction accuracy` is `n/a` | No record has an `expected_label`. Run `validate` or populate `images/labels.json`. |
| `Activate.ps1 cannot be loaded` | Run `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass` in that PowerShell session. |
| Timeout test reports `connection` instead of `timeout` | Port 9 refused immediately instead of timing out. Either outcome is a valid controlled failure; TC1.4 accepts both. |
