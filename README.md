![Robotic Hand banner](banner.svg)

# Robotic Hand — Edge/Cloud AI Pipeline

![Status](https://img.shields.io/badge/status-implemented%20phases%201%E2%80%934-2E7D32)
![License](https://img.shields.io/badge/license-MIT-blue)
![Platform](https://img.shields.io/badge/platform-edge%20%2B%20cloud-6B7280)

AI-assisted robotic hand for small-scale industrial automation. Perception and decision-making run on whichever compute is available — an on-site GPU if the factory has one, a rented cloud GPU if it doesn't — connected through a reliable messaging layer so no sensor reading or command is lost if the network drops.

The reference implementation lives in [`robotic-hand/`](robotic-hand/README.md). Phases 1–4 are built and tested: baseline pipeline, reliable MQTT delivery, multi-provider routing, and conflict-safe decision handling.

## Table of contents

- [Overview](#overview)
- [Features](#features)
- [Architecture](#architecture)
- [Tech stack](#tech-stack)
- [Getting started](#getting-started)
- [Project structure](#project-structure)
- [Reliability](#reliability-how-data-loss-is-actually-prevented)
- [Security](#security)
- [Monitoring](#monitoring)
- [Roadmap](#roadmap)
- [Contributing](#contributing)
- [License](#license)

## Overview

Most small manufacturers can't justify a dedicated on-site AI server just to add vision-guided automation. This project's premise: keep the robot hand's safety-critical motor control fully local, and let the perception/decision step run wherever makes sense — locally if the factory already has a GPU, in the cloud if it doesn't — without the robot ever losing data when the connection is unstable.

This repository contains a working reference implementation plus the original single-provider prototype.

### Implemented phases

| Phase | What it adds | Where |
|---|---|---|
| **1 — baseline** | Capture → HTTP inference → measured latency; deterministic offline mock server; optional real RT-DETR endpoint | [`robotic-hand/`](robotic-hand/README.md) |
| **2 — reliable delivery** | SQLite outbox written before send, Mosquitto/MQTT QoS 1, ACKs, exponential backoff, restart recovery, worker dedup | [`robotic-hand/phase2_reliability/`](robotic-hand/phase2_reliability/) |
| **3 — multi-provider routing** | Health checks, circuit breaker, EWMA preference, sequential failover, shared sessions, deadline-derived timeouts | [`robotic-hand/phase3_router/`](robotic-hand/phase3_router/) |
| **4 — conflict handling** | Task context end to end, first-writer-wins result claims, discard log, robot decision gate, safe defaults, exactly-once application | [`robotic-hand/phase4_conflict/`](robotic-hand/phase4_conflict/) |

The earlier single-provider prototype is kept in [`robot-cloud-ai/`](robot-cloud-ai/README.md) for comparison; it is not the current pipeline.

## Features

- **Hybrid inference routing** — prefers local GPU compute when available, falls back to cloud automatically
- **Zero data loss on disconnect** — local buffering and durable message delivery, not best-effort
- **Safety-first local control** — motor control and e-stop logic never depend on the network
- **Hardware-agnostic cloud path** — works with any production-grade rented GPU inference service
- **Model sync** — local and cloud inference paths kept on compatible model versions

## Architecture

![Architecture diagram](architecture-diagram.svg)

**Pipeline (as implemented):**

1. **Robot** — camera and sensors capture data locally; motor control and safety stops run on-device, independent of network state.
2. **Edge controller** — assigns a `task_id` (UUID4), attempt number, frame timestamp and deadline; writes the frame to a SQLite outbox as `pending` *before* sending, then publishes with MQTT QoS 1.
3. **Message broker (MQTT/Mosquitto)** — delivers frames and ACKs; unacked messages are resent with exponential backoff (1 s, 2 s, 4 s, capped at 30 s) and drained oldest-first after an outage or restart.
4. **Worker** — deduplicates by `task_id`, runs the router, and records the outcome. Results are claimed with `INSERT … ON CONFLICT DO NOTHING`, so the first valid writer wins.
5. **Router** — skips unhealthy providers (background health checks), respects a circuit breaker, prefers the lowest recent latency, and fails over **sequentially**. Each attempt's timeout is derived from the remaining deadline divided by the remaining providers, with a floor.
6. **Providers** — deterministic controllable mock providers for tests, plus an optional real RT-DETR endpoint. Each response echoes `task_id`; a mismatch is rejected and logged.
7. **Decision gate (robot side)** — applies a decision only if it is not already applied, the deadline has not passed, and no newer frame has already been decided. Expired deadlines fall back to a safe default; a later answer is logged as `late` and never applied.

## Tech stack

| Concern | Implemented / recommended | Notes |
|---|---|---|
| Edge runtime | Python 3.10+ (verified on 3.14.5) | OpenCV capture, `requests`, SQLite outbox |
| Messaging | MQTT — Mosquitto (Docker Compose) | QoS 1, ACK topic, durable outbox |
| Multi-provider routing | `phase3_router` with HTTP providers | Health checks, circuit breaker, EWMA preference |
| Conflict handling | SQLite `results`, `discarded`, `applied_ids` | First-writer-wins claims, exactly-once application |
| Mock inference | FastAPI deterministic mock servers | Offline, controllable failure/slow/hang for tests |
| Real inference (optional) | RT-DETR (`PekingU/rtdetr_r18vd`) via `real_server` | Requires `requirements-real.txt` and model download |
| Edge hardware | NVIDIA Jetson Orin Nano (recommended) | Local inference path; not integrated in this phase |
| Transport | Wired Ethernet where possible | More reliable and lower-latency than shared WiFi |

## Getting started

**Prerequisites**
- Python 3.10+ (verified on 3.14.5)
- Docker Desktop / Docker Engine for the Mosquitto broker (Phase 2 integration tests)
- No camera, GPU or API key is needed for the offline tests

```bash
# clone the repo
git clone https://github.com/your-org/robotic-hand.git
cd robotic-hand

python -m venv .venv
source .venv/Scripts/activate   # Windows (Git Bash); use .venv/bin/activate elsewhere
python -m pip install --upgrade pip
pip install -r requirements.txt
cp .env.example .env

# optional: local broker for the MQTT path
docker compose up -d

# terminal 1: deterministic mock inference server
python -m mock_server --port 8000

# terminal 2: run the offline test suite (no broker, camera or credentials)
python -m pytest -q

# optional: optional real model server (large download)
pip install -r requirements-real.txt
python -m real_server --port 8001
```

See [`robotic-hand/README.md`](robotic-hand/README.md) for each phase's commands, configuration and measurements.

## Project structure

```
robotic-hand/
├── README.md                  # phase-by-phase documentation and commands
├── .env.example / .gitignore
├── requirements.txt / requirements-real.txt
├── docker-compose.yml         # Mosquitto broker for Phase 2
├── mosquitto/                 # broker configuration
├── images/                    # sample frames + labels.json
├── mock_server/               # deterministic HTTP inference mock
├── real_server/               # optional RT-DETR endpoint
├── phase1_pipeline/           # capture, HTTP client, benchmark
├── phase2_reliability/        # outbox, publisher, worker, common/config
├── phase3_router/             # router, health checks, providers
├── phase4_conflict/           # task context, claims, discard log, decision gate
├── benchmarks/                # router overhead benchmark
├── tests/                     # TC1.x, phase2, phase3, phase4 suites
└── docs/measurements.md       # real measurements, never fabricated
```

## Reliability: how data loss is actually prevented

Individual packet loss within a connection is already handled by the transport layer (TCP, which MQTT and gRPC sit on top of) — packets lost mid-transmission are automatically detected and resent. The real risk is a **full connection drop**, and what happens to data generated during that gap.

| Layer | Mechanism | Failure it protects against |
|---|---|---|
| Edge controller | SQLite outbox written as `pending` before every send | Data loss during an outage or process kill |
| Publisher | QoS 1 + ACK-driven `acked` state, backoff resend | Lost or unacknowledged delivery |
| Broker | MQTT QoS 1; duplicate delivery is expected and handled | Duplicated delivery across an unstable link |
| Worker | `processed_ids` dedup, re-ACK on duplicate | Double processing of a redelivered frame |
| Router | Health checks, circuit breaker, sequential failover, deadline-derived timeouts | Hanging or failed provider blocking the task |
| Results store | `INSERT … ON CONFLICT DO NOTHING` claim | Two providers/workers applying different answers |
| Robot gate | `applied_ids` + deadline + newer-frame check | Re-applying, applying late, or overwriting a newer decision |
| Robot | Timeout → safe default; late answers logged and discarded | Indefinite hang if no response ever arrives |

## Security

- TLS/mTLS on every connection between robot, broker, and processing endpoints
- Per-device certificates for authentication — don't leave factory-floor devices open on the network
- Confirm data residency/retention policy with the factory before any camera footage leaves the site

## Monitoring

Track and alert on:
- End-to-end latency (robot → decision → robot)
- Local vs. cloud routing ratio, failover events, router overhead
- Outbox depth, retry rate and timeout rate
- Discard counts by reason (`duplicate`, `late`, `mismatch`)
- Safe-default rate and any task applied more than once (must be zero)

## Roadmap

- [x] Edge controller reference implementation (capture, outbox, publisher)
- [x] MQTT broker config + QoS policy (Mosquitto, QoS 1, ACKs)
- [x] Cloud inference path + reliable delivery (worker, retries, dedup)
- [x] Multi-provider routing with health checks and failover
- [x] Conflict handling: task IDs, claims, decision gate, safe defaults
- [ ] Local GPU inference path on Jetson-class hardware
- [ ] Hardware integration with the robot hand
- [ ] Field test in a single factory with no on-site GPU
- [ ] Field test in a factory with an on-site GPU

## Contributing

Phases 1–4 are implemented and tested. Issues and pull requests that correct behaviour, extend test coverage, or add hardware integration notes are welcome. Please open an issue describing the change before submitting a large pull request.

## License

MIT — replace with your preferred license before publishing.
