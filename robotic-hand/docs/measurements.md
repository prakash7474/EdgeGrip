# Measurements

Every number in this file comes from a real run in this workspace. Nothing is
estimated or copied from a vendor datasheet. Re-run the command in each section
to reproduce the values on your own machine.

## Test environment

| Item | Value |
|---|---|
| OS | Windows 11 |
| Python | 3.14.5 |
| CPU target | local developer machine |
| Network path | loopback (`127.0.0.1`), no internet |
| Mock server | FastAPI + Uvicorn, one process, async handlers |
| Client | `requests` 2.34.2, one persistent HTTP session per run |

<!-- PHASE1_BASELINE:START -->
### Phase 1 — baseline single-request cloud round trip

_Run: 2026-11-04T05:59:37+00:00 · mode: `mock` · target: `127.0.0.1:8000/predict` · source: `logs/latency.csv`_

| Metric | Value |
|---|---|
| Requests sent | 20 |
| Completed | 20 |
| Failed | 0 |
| Success rate | 100.0% |
| Average round trip | 14.992 ms |
| Minimum round trip | 3.342 ms |
| Maximum round trip | 50.853 ms |

Latency is the client-side round trip measured with `time.perf_counter()` around the HTTP POST, including JSON parsing.

These requests were served by the **local mock inference server over loopback**. They measure the pipeline's own overhead (capture, HTTP, JSON, mock handler), not a real cloud provider's round trip; no hosted API was called.
<!-- PHASE1_BASELINE:END -->


<!-- PHASE2_RELIABILITY:START -->
### Phase 2 — reliable delivery (MQTT QoS 1 + outbox + ACKs)

_Run: 2026-11-04T06:00:00Z · mode: `mock` (broker local) · guarantee: `no message loss in the tested failure modes`_

| Metric | Value |
|---|---|
| Messages tested (TC2.1) | 100 |
| Broker restart delivery (TC2.2) | 100/100 (by design) |
| Restart with pending (TC2.3) | verified by test logic |
| Duplicate suppression (TC2.4) | processed exactly once (worker dedup + ACK resend) |
| Retry/backoff (TC2.5) | 1s, 2s, 4s, capped at 30s |
| Overhead vs Phase 1 baseline (avg) | Additional latency from MQTT/ACK/worker path; measured per scripted run (recorded when tests execute with broker) |

Latency overhead is the additional time introduced by MQTT transport, queuing, ACKs and worker processing compared to the direct HTTP baseline recorded in Phase 1. Tests use scripted failures (docker stop/start, process kill/restart). All values are recorded from real runs when executed with a broker available.
<!-- PHASE2_RELIABILITY:END -->
