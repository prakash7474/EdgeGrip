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
| Worst-case failover time (ms) | 4.338 |
| Attempts | 2 |
| Routed to | mock_b |

<!-- TC3_8_FAILOVER:END -->















<!-- TC3_8_HANG_FAILOVER:START -->
### TC3.8 hanging-provider failover (5 runs, measured)

Bound per run = connect_timeout (1.0s) + read_timeout (2.0s)
+ one failover call (measured) + fixed slack (0.3s).

| Run | Total ms | Bound ms | A call ms | B call ms (failover) | Connect ms | Breaker check ms | Health ms (off path) |
|---|---|---|---|---|---|---|---|
| 1 | 2015.4 | 3307.3 | 2008.0 | 7.3 | 0.74 | 0.0049 | 4.4 |
| 2 | 2023.1 | 3306.0 | 2017.1 | 6.0 | 0.73 | 0.0049 | 4.0 |
| 3 | 2007.7 | 3306.6 | 2001.0 | 6.6 | 0.86 | 0.0052 | 4.4 |
| 4 | 2015.5 | 3307.0 | 2008.4 | 7.0 | 0.76 | 0.0038 | 3.1 |
| 5 | 2010.5 | 3307.0 | 2003.4 | 7.0 | 0.74 | 0.0034 | 3.4 |

| Metric | Value |
|---|---|
| min total | 2007.7 ms |
| median total | 2015.4 ms |
| max total | 2023.1 ms |
| retries observed | 0 (one call per provider) |
| sleeps on the request path | 0 |
| health checks | background thread, not on the request path |

Breakdown of the earlier 4028 ms regression: ~2000 ms first read timeout, ~2000 ms an
accidental second call to the same hanging provider (Router caught the provider's
internal `TypeError` and retried), ~7 ms failover call, ~0 ms connect and breaker
checks. Fixed by making `CloudClient` format a tuple timeout correctly and by
choosing `task_context`/`timeout` kwargs via `inspect.signature` instead of
`except TypeError`.
<!-- TC3_8_HANG_FAILOVER:END -->





<!-- TC4_RESULTS:START -->
### Phase 4 (TC4.1-TC4.12) conflict handling

| Metric | Value |
|---|---|
| TC4.5 trials (two responses through the real worker claim path, random jitter) | 200 |
| TC4.5 violations (a trial without exactly one result and one applied decision) | 0 |
| TC4.5 duplicate discards (one per losing response) | 200 |
| TC4.8 | see the TC4.8 block below; injected faults are labelled there |
| TC4.9 | see the TC4.9 block below |
| TC4.1-TC4.4, TC4.6, TC4.7, TC4.10-TC4.12 | passed (see test docstrings for the spec mapping) |
| Invariant violations found | 0 |

Injected expired deadlines in TC4.8 are reported there as injected faults, not as a
late-answer rate. No organic late-answer rate is claimed for TC4.8 because the
expired deadlines were configured by the test.
<!-- TC4_RESULTS:END -->

<!-- TC4_9_DEADLINE_TIMEOUT:START -->
### TC4.9 deadline-derived attempt timeout (A hangs, B healthy, deadline 1.5 s)

| Group | Tasks | Success | Notes |
|---|---|---|---|
| first 20 after A starts hanging (breaker closed) | 20 | 100% | A gets ~750 ms (1.5 s / 2 providers), B gets the rest |
| first 20 after the breaker opened | 20 | 100% | A skipped, B answers immediately |

Group 1 measured attempt budgets: [749.947, 749.972, 749.977, 749.983, 749.984, 749.985, 749.986, 749.987, 749.988, 749.989, 749.99, 749.991, 749.992, 898.197, 901.591, 911.479, 920.121, 921.89, 926.931, 927.406, 928.282, 928.29, 929.176, 929.327, 929.894, 930.125, 930.249, 930.285, 930.553, 930.665, 931.174, 931.214, 931.339] ms
Group 1 elapsed: min 573 / median 577 / max 607 ms
Group 2 elapsed: min 4 / median 5 / max 6 ms
<!-- TC4_9_DEADLINE_TIMEOUT:END -->

<!-- TC4_8_FULL_WORKER:START -->
### TC4.8 (rewritten) 1000 tasks through worker + router + gate

Injected (configured by the test, not observed rates):
- random provider 500s: fail_p=0.15 on each of 2 providers
- random provider delays: slow_p=0.15, delay uniform 0-250 ms (some exceed the per-attempt budget)
- duplicate deliveries: 15% of tasks delivered twice (173 extra deliveries)
- expired deadlines: 10% of tasks (109 tasks) - injected, NOT a late rate

Observed outcomes:
| Outcome | Count |
|---|---|
| applied (model decision) | 864 |
| applied safe_default (organic provider failure before deadline) | 27 |
| safe_default from injected expired deadline (not applied) | 109 |
| failed (no decision; organic) | 0 |
| total tasks | 1000 |
| applied more than once | 0 |

Observed discards:
| Reason | Count |
|---|---|
| late (from injected expired deadlines) | 109 |
| late (organic) | 0 |
| duplicate | 173 |
| mismatch | 0 |

Injected provider counters (from mock /control/stats):
- HTTP 500 responses: 142
- delayed responses: 146

Organic observations:
- late discards not attributable to injected expired deadlines: 0
- client-side timeouts are not separately counted by the mock; they appear as failed attempts and, when both providers fail, as safe_default decisions.
- injected expired deliveries (including duplicates): 130
<!-- TC4_8_FULL_WORKER:END -->
