![Robotic Hand banner](banner.svg)

# Robotic Hand — Edge/Cloud AI Pipeline

![License](https://img.shields.io/badge/license-MIT-blue)
![Platform](https://img.shields.io/badge/platform-edge%20%2B%20cloud-6B7280)

Small manufacturers often cannot justify a dedicated on-site AI server for
vision-guided automation. This project keeps the robotic hand's motor control
and safety stops local, while the perception/decision step runs on whichever
compute is available — an on-site GPU when the factory has one, a rented cloud
GPU when it does not — connected by a durable MQTT path so a network drop does
not lose work in the failure modes that were tested. The repository is a
reference implementation of that split: camera capture, a SQLite outbox,
Mosquitto delivery, multi-provider routing, conflict-safe claims, a robot-side
decision gate, and a serial actuator protocol. The firmware and the physical
hand remain untested.

## Architecture

![Architecture diagram](architecture-diagram.svg)

A frame is captured on the edge, written to a SQLite outbox as `pending`
*before* it is published, and sent over MQTT QoS 1. The worker deduplicates by
`task_id` and asks the router for a decision; the router checks provider
health, respects a circuit breaker, prefers the lowest recent latency, and
fails over sequentially within the task deadline. Results are claimed with
`INSERT … ON CONFLICT DO NOTHING`, so the first valid writer wins. The robot
gate applies a decision only if it is not already applied, the deadline has
not passed, and no newer frame has been decided; otherwise it falls back to a
safe default. A configurable policy maps the decision to at most one actuator
action, claimed in `actuation_log` before anything is sent.

More detail: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Phase status

| Phase | Built | Tested offline | Tested with broker/Docker or real cloud/hardware |
|---|---|---|---|
| **1 — baseline pipeline** | Yes | Yes — capture, mock HTTP inference, latency CSV | **No real cloud run.** No hosted provider was called. The local real-model server was attempted but did not start, so it produced no inference measurement. |
| **2 — reliable delivery** | Yes | Partial — outbox and message code are exercised by offline Phase 4 tests | **No local broker run recorded.** The Docker daemon was unavailable here, so all six Phase 2 broker tests skipped. A CI job is defined but has not run yet. |
| **3 — multi-provider routing** | Yes | Yes — in-process mock providers | **No local broker run.** The Phase 3 worker/router/broker integration test skipped with Docker unavailable. |
| **4 — conflict handling** | Yes | Yes — claim races, restarts and 1000-task worker path | Broker not required for these tests. No real-cloud run. |
| **5a — deployment harness** | Yes | Yes — token enforcement and client/provider token handling | **No real cloud deployment.** No VM was provisioned and no hosted endpoint was called. |
| **5b — actuation** | Yes | Yes — stub actuator, fake device, serial path over a fake transport | **No real hardware.** The firmware sketch has never been compiled, flashed or run; hardware-gated tests skipped. |

## Test status

Collected from the repository with `python -m pytest --collect-only -q`
(88 tests). Statuses below are from the offline run in this workspace on
Windows 11 with Python 3.14.5:

```text
74 passed, 14 skipped
```

<!-- TEST_TABLE_START -->
| Test ID | Test file | Status |
|---|---|---|
| TC1.1 | tests/test_tc1_1_single_request.py | Passed (4 tests) |
| TC1.2 | tests/test_tc1_2_batch.py | Passed (3 tests) |
| TC1.3 | tests/test_tc1_3_known_label.py | Passed (3 tests) |
| TC1.4 | tests/test_tc1_4_failure.py | Passed (5 tests) |
| TC2.1 | tests/phase2/test_tc2_1_healthy.py | Skipped — Docker daemon unavailable locally |
| TC2.2 | tests/phase2/test_tc2_2_broker_restart.py | Skipped — Docker daemon unavailable locally |
| TC2.3 | tests/phase2/test_tc2_3_restart_pending.py | Skipped — Docker daemon unavailable locally |
| TC2.4 | tests/phase2/test_tc2_4_duplicate.py | Skipped — Docker daemon unavailable locally |
| TC2.5 | tests/phase2/test_tc2_5_retry_backoff.py | Skipped — Docker daemon unavailable locally |
| TC2.6 | tests/phase2/test_tc2_6_latency_overhead.py | Skipped — Docker daemon unavailable locally |
| TC3.1 | tests/phase3/test_tc3_router.py | Passed |
| TC3.2 | tests/phase3/test_tc3_router.py | Passed |
| TC3.3 | tests/phase3/test_tc3_router.py | Passed |
| TC3.4 | tests/phase3/test_tc3_router.py | Passed |
| TC3.5 | tests/phase3/test_tc3_router.py | Passed |
| TC3.6 | tests/phase3/test_tc3_extra.py | Passed |
| TC3.7 | tests/phase3/test_tc3_extra.py | Passed |
| TC3.8 | tests/phase3/test_tc3_extra.py, tests/phase3/test_tc3_refactor.py | Passed (2 tests) |
| TC3.9 | tests/phase3/test_tc3_extra.py | Passed |
| TC3-integration | tests/phase3/test_tc3_extra.py | Skipped — Docker daemon unavailable locally |
| TC3-unit | tests/phase3/test_tc3_refactor.py | Passed (circuit breaker and health-thread tests) |
| TC4.1 | tests/phase4/test_tc4_conflict.py | Passed |
| TC4.2 | tests/phase4/test_tc4_conflict.py | Passed |
| TC4.3 | tests/phase4/test_tc4_conflict.py | Passed |
| TC4.4 | tests/phase4/test_tc4_conflict.py | Passed |
| TC4.5 | tests/phase4/test_tc4_conflict.py | Passed |
| TC4.6 | tests/phase4/test_tc4_conflict.py | Passed |
| TC4.7 | tests/phase4/test_tc4_conflict.py | Passed |
| TC4.8 | tests/phase4/test_tc4_conflict.py | Passed |
| TC4.9 | tests/phase4/test_tc4_conflict.py | Passed |
| TC4.10 | tests/phase4/test_tc4_conflict.py | Passed |
| TC4.11 | tests/phase4/test_tc4_conflict.py | Passed |
| TC4.12 | tests/phase4/test_tc4_conflict.py | Passed |
| TC5a | tests/test_phase5a_token.py | Passed (6 tests) |
| TC5b.1 | tests/phase5b/test_tc5b_actuator.py | Passed |
| TC5b.2 | tests/phase5b/test_tc5b_actuator.py | Passed (12 tests) |
| TC5b.3 | tests/phase5b/test_tc5b_actuator.py | Passed (4 tests) |
| TC5b.4 | tests/phase5b/test_tc5b_actuator.py | Hardware-gated — skipped, no serial device |
| TC5b.5 | tests/phase5b/test_tc5b_actuator.py | Hardware-gated — skipped, no serial device |
| TC5b.6 | tests/phase5b/test_tc5b_actuator.py | Passed (3 tests) |
| TC5b.7 | tests/phase5b/test_tc5b_actuator.py | Passed (3 tests) |
| TC5b.8 | tests/phase5b/test_tc5b_actuator.py | Passed (3 tests) |
| TC5b-sim | tests/phase5b/test_tc5b_actuator.py | Skipped — optional pybullet package not installed |
| TC5b-live | tests/phase5b/test_tc5b_live_latency.py | Passed (3 tests) |
| REAL-MODEL | tests/test_real_model.py | Skipped — RUN_REAL_MODEL not set; no model download or real inference run |
<!-- TEST_TABLE_END -->

`python -m scripts.check_docs` verifies that every test ID in this table maps
to a collected test and every collected test has a row.

## Measured results

Every number below is labelled with the environment that produced it. Nothing
here is a cloud-latency measurement. Full records are in
[docs/measurements.md](docs/measurements.md).

| Measurement | Value | Environment |
|---|---|---|
| Phase 1 round trip, 20 requests | avg 23.876 ms, min 6.039 ms, max 61.604 ms | **Loopback mock** (`127.0.0.1`, local FastAPI mock server) |
| Phase 3 worst-case failover | 6.808 ms | **Loopback mock** providers, local process |
| Phase 3 hanging-provider failover, 5 runs | min 2009.9 ms, median 2013.3 ms, max 2031.2 ms | **Loopback mock**, 2.0 s read timeout, one failover call |
| Phase 4 claim-path trials | 200 trials, 0 invariant violations | **Loopback mock** providers |
| Phase 4 1000-task worker run | 0 tasks applied more than once; 0 organic failures | **Loopback mock** providers and local SQLite |
| Real model (RT-DETR), local | 0 requests completed; server did not start | **Real model, local** — attempted, not executed |
| Real cloud inference | No run recorded | **Real cloud** — not tested |
| Actuator capture→ACK | No run recorded | **Real hardware** — not tested; stub/fake only |

## Claims audit

The repository was searched for absolute reliability wording. These were the
claims found, and what they now say:

| Original claim | Location | Replacement |
|---|---|---|
| "Zero data loss on disconnect" | README | "No message loss in the tested failure modes" |
| "exactly-once application" | README | "at most one application per task in the tested scenarios" |
| "100 messages … 0 lost" (test docstring) | tests/phase2/test_tc2_1_healthy.py | "100 messages all acked in this test" |
| "processed exactly once" | tests/phase2/test_tc2_4_duplicate.py, docs/measurements.md, tests/phase2/test_tc2_6_latency_overhead.py | "processed once in the duplicate-delivery test" |
| "100 messages no loss" | docs/PHASES.md | "100 messages all acked in the test" |
| "never dropped silently" | docs/PHASES.md, tests/phase4/test_tc4_conflict.py | "recorded in the discard log" |
| "same task is applied exactly once" | docs/PHASES.md | "the same task is not applied twice in the tested crash/restart scenarios" |

The durable outbox, QoS 1, ACK-driven retry, deduplication and claims are
tested against the failure modes listed in
[docs/TESTING.md](docs/TESTING.md). They are not a proof of losslessness for
untested failures such as storage corruption, a disk full during the claim
write, or a device that never returns an ACK.

## Known limitations

- **Single-machine SQLite claim store.** `results`, `discarded`, `applied_ids`
  and `actuation_log` live in one SQLite file. There is no replication or
  multi-machine consensus; two edge machines with separate databases are not
  covered.
- **Plain-HTTP tokens.** `real_server` authenticates `/predict` with an
  `X-API-Token` header over plain HTTP unless a TLS proxy is added. The token is
  not encrypted in transit by this code, and there is no key rotation.
- **COCO-only model.** The optional RT-DETR model
  (`PekingU/rtdetr_r18vd`) is COCO-trained and does not know factory-specific
  parts. The offline mock labels come from a hash manifest and measure plumbing,
  not accuracy.
- **Firmware untested on hardware.** The sketch in `firmware/` has never been
  compiled, flashed or run. Protocol behaviour is verified against
  `FakeDevice`, not the C++ implementation.
- **Broker and worker run on the edge machine.** Mosquitto and the worker are
  expected on the same machine as the capture process. Only inference endpoints
  are remote. This avoids WAN broker latency but is not a distributed
  deployment.
- **Mock-only latency unless real runs are recorded.** All latency and failover
  numbers above come from loopback mock servers. No real-cloud or real-provider
  latency was measured.
- **No hardware actuation was run.** No physical hand was moved. The `stop`,
  heartbeat safe state and limits are software behaviours and must not replace
  a physical e-stop that cuts servo power.
- **Optional simulation not exercised.** The PyBullet `SimActuator` test skips
  because `pybullet` is not installed in this environment.

## Quickstart (Windows PowerShell)

These commands were executed in this workspace. They install the offline
dependencies, create a local `.env`, and run the offline suite. The suite still
skips the real-model, broker/Docker and hardware cases as described above.

```powershell
python --version
python -m venv .venv
python -m pip install -r requirements.txt
Copy-Item .env.example .env
$env:SKIP_PHASE2_DOCKER_TESTS = "1"
python -m pytest --collect-only -q
python -m pytest -q
```

Start the deterministic mock inference server and check it:

```powershell
Start-Process python -ArgumentList '-m','mock_server','--port','8000' -PassThru
Start-Sleep -Seconds 4
(Invoke-WebRequest -UseBasicParsing http://127.0.0.1:8000/health).Content
```

The health response was:

```json
{"status":"ok","model":"mock-vit-b16","known_images":3}
```

Docker, broker, real-model and hardware commands are documented in
[docs/TESTING.md](docs/TESTING.md); they were not run in this workspace.

## Documentation

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — diagram, data flow, deadlines
  and claim-before-send logic.
- [docs/TESTING.md](docs/TESTING.md) — offline, broker, real-model and
  hardware-gated test tiers.
- [docs/PHASES.md](docs/PHASES.md) — phase-by-phase implementation notes and
  commands.
- [docs/measurements.md](docs/measurements.md) — measured results with
  environment labels.
- [docs/device_protocol.md](docs/device_protocol.md) and
  [docs/hardware.md](docs/hardware.md) — serial protocol and hardware safety.

## License

MIT — see [LICENSE](LICENSE).
