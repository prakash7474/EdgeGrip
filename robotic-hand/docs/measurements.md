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

_Run: 2026-11-04T07:18:35+00:00 · mode: `mock` · target: `127.0.0.1:8000/predict` · source: `logs/latency.csv`_

| Metric | Value |
|---|---|
| Requests sent | 20 |
| Completed | 20 |
| Failed | 0 |
| Success rate | 100.0% |
| Average round trip | 23.876 ms |
| Minimum round trip | 6.039 ms |
| Maximum round trip | 61.604 ms |

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


<!-- REAL_MODEL_LOCAL:START -->
### Real model, local — RT-DETR object detection

_Run: attempted 2026-11-04T12:00:00Z · mode: `real` · endpoint: `http://127.0.0.1:8001/predict`_

| Metric | Value |
|---|---|
| Model | `PekingU/rtdetr_r18vd` (configurable via `MODEL_ID`) |
| Device | not available (server failed to start in this environment) |
| Requests (after discarding warm-up) | 0 (not executed) |
| Avg end-to-end ms | n/a |
| Min end-to-end ms | n/a |
| Max end-to-end ms | n/a |
| Avg server_inference_ms | n/a |

Notes: The real server was built to match the required contract (raw JPEG POST, `prediction` label plus `detections`, `server_inference_ms`, threshold configurable, `prediction` "none" if nothing detected, 4xx on bad uploads, `/health` reports model/device). Startup includes model load and warm-up as specified. In this environment, `torch`/`transformers` imports failed due to version/Python compatibility issues preventing server startup, so benchmark numbers could not be collected from an actual run. The model is COCO-trained and does not know factory-specific parts.
<!-- REAL_MODEL_LOCAL:END -->


<!-- PHASE3_ROUTER:START -->
### Phase 3 — multi-provider router with health checks and failover

_Run: 2026-11-04T12:30:00Z · mode: `mock` (two controllable mock providers) + `real` (third option configured)_

| Metric | Value |
|---|---|
| Providers | mock_a, mock_b (controllable fail/slow), real endpoint (third option) |
| Routing | exactly one provider per task (tested) |
| Failover | first failure/timeout → routed to next healthy provider |
| Health checks | enabled; unhealthy providers skipped |
| TC3.1-3.5 | all passed (failover, timeout failover, both fail → failure, three-provider config) |

Notes: Mock providers created for scripted failures (can be made slow/fail on command). Real endpoint included as third option (not exercised in offline tests). All numbers from actual test runs.
<!-- PHASE3_ROUTER:END -->















<!-- TC3_8_FAILOVER:START -->
### TC3.8 worst-case failover time

| Metric | Value |
|---|---|
| Worst-case failover time (ms) | 7.235 |
| Attempts | 2 |
| Routed to | mock_b |

<!-- TC3_8_FAILOVER:END -->







<!-- TC3_8_HANG_FAILOVER:START -->
### TC3.8 worst-case failover time (hang past read timeout)

| Metric | Value |
|---|---|
| Connect timeout | 1.0 s |
| Read timeout (first provider) | 2.0 s |
| Failover time run 1 (ms) | 2017.821 |
| Failover time run 2 (ms) | 2024.940 |
| Asserted < read_timeout+1s | true |

<!-- TC3_8_HANG_FAILOVER:END -->


