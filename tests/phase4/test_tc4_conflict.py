"""TC4.1-TC4.12: conflict handling.

Spec-table mapping
------------------
The literal spec table was not available in this repository, so each test is
mapped to the numbered Phase 4 requirement it implements. The two review
additions are mapped to the review item that asked for them.

TC4.1  -> spec 1  task_id/attempt/frame_timestamp/deadline carried end to end;
                    mock provider echoes task_id.
TC4.2  -> spec 2  results claim via INSERT ... ON CONFLICT DO NOTHING, first
                    writer wins, concurrent-safe.
TC4.3  -> spec 3  discarded table reasons duplicate|late|mismatch.
TC4.4  -> spec 4  router rejects mismatched task_id; failover stays sequential.
TC4.5  -> spec 2 + review 5: two responses race through the real worker claim
                    path (not just direct store writes).
TC4.6  -> spec 5  robot decision gate: not applied twice, deadline, newer
                    frame already decided, survives restart.
TC4.7  -> spec 6  deadline passed -> safe default; a later answer is late and
                    never applied.
TC4.8  -> spec 7  full worker+router+gate run over many tasks with injected
                    faults; observed outcomes and invariant.
TC4.9  -> review 2  per-attempt timeout derived from remaining deadline /
                    remaining providers; success rate before vs after the
                    circuit breaker opens.
TC4.10 -> review 6  crash after inference before ACK, message resent, second
                    inference differs, exactly one decision applied.
TC4.11 -> review 4  edge process killed mid-task, restarted in a separate real
                    process, same task_id applied once.
TC4.12 -> review 4  A-late-after-B: cannot happen in the sequential HTTP path,
                    so it is simulated through the claim/gate path (stated).

TC4.8's provider faults are injected by the controllable mock servers and are
reported separately from organic observations; injected expired deadlines are
not reported as a "late rate".
"""
from __future__ import annotations

import base64
import json
import os
import random
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests
import uvicorn

from phase1_pipeline.capture import FrameGrabber
from phase2_reliability.common import Message
from phase2_reliability.config import load_settings
from phase2_reliability.outbox import Outbox
from phase2_reliability.worker import Worker
from phase3_router import HTTPProvider, Router
from phase3_router.providers import mock_provider_a, mock_provider_b
from phase4_conflict import ConflictStore, DecisionGate, TaskContext

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
IMAGES_DIR = PROJECT_ROOT / "images"
HERE = Path(__file__).resolve().parent


# ---------------------------------------------------------------- fixtures

def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _start(app, port):
    cfg = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", access_log=False)
    srv = uvicorn.Server(cfg)
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    _wait(port)
    return srv, thread


def _wait(port: int, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.25):
                return
        except OSError:
            time.sleep(0.05)
    raise RuntimeError(f"mock provider did not start on {port}")


@pytest.fixture(scope="module")
def providers():
    port_a, port_b = _free_port(), _free_port()
    srv_a, t_a = _start(mock_provider_a.app, port_a)
    srv_b, t_b = _start(mock_provider_b.app, port_b)
    conf = {
        "a": f"http://127.0.0.1:{port_a}/predict",
        "b": f"http://127.0.0.1:{port_b}/predict",
        "ca": f"http://127.0.0.1:{port_a}",
        "cb": f"http://127.0.0.1:{port_b}",
    }
    yield conf
    srv_a.should_exit = True
    srv_b.should_exit = True
    t_a.join(timeout=5)
    t_b.join(timeout=5)


@pytest.fixture(autouse=True)
def _reset_controls(providers):
    for key in ("ca", "cb"):
        requests.get(f"{providers[key]}/control/reset", timeout=5)
    yield
    for key in ("ca", "cb"):
        try:
            requests.get(f"{providers[key]}/control/reset", timeout=5)
        except Exception:
            pass


@pytest.fixture()
def frame():
    return FrameGrabber(IMAGES_DIR, camera_enabled=False).next_frame()


# ---------------------------------------------------------------- helpers

class FakeClient:
    """Stands in for the MQTT client so the worker path runs without a broker."""

    def __init__(self):
        self.published = []

    def publish(self, topic, payload, qos=1):
        self.published.append((topic, payload))
        return None


def make_worker(tmp_path, name, router, store=None, gate=None):
    settings = replace(
        load_settings(),
        processed_db=tmp_path / f"processed_{name}.db",
        outbox_db=tmp_path / f"outbox_{name}.db",
    )
    worker = Worker(settings=settings, router=router, conflict_store=store, decision_gate=gate)
    worker.client = FakeClient()
    return worker


def deliver(worker, message):
    worker._on_message(None, None, SimpleNamespace(payload=message.to_json().encode("utf-8")))


def message_for(task_id, jpeg, *, attempt=1, frame_timestamp="", deadline=0.0):
    return Message(
        message_id=task_id,
        created_at="2026-11-07T00:00:00.000Z",
        payload=base64.b64encode(jpeg).decode("ascii"),
        attempt=attempt,
        frame_timestamp=frame_timestamp,
        deadline=deadline,
    )


def stats(base):
    return requests.get(f"{base}/control/stats", timeout=5).json()


# ---------------------------------------------------------------- TC4.1

def test_tc4_1_task_context_carried_end_to_end_and_mock_echoes(providers, frame, tmp_path):
    """Spec 1: context survives outbox/wire and the provider echoes task_id."""
    outbox = Outbox(tmp_path / "outbox.db")
    task_id = "task-tc4-1-0001"
    deadline = time.time() + 30
    msg = outbox.enqueue(frame.jpeg_bytes, message_id=task_id, attempt=2,
                         frame_timestamp="12345.678", deadline=deadline)
    assert (msg.task_id, msg.attempt, msg.frame_timestamp) == (task_id, 2, "12345.678")

    with sqlite3.connect(str(tmp_path / "outbox.db")) as conn:
        row = conn.execute(
            "SELECT attempt, frame_timestamp, deadline FROM outbox WHERE message_id=?", (task_id,)
        ).fetchone()
    assert row[0] == 2 and row[1] == "12345.678" and abs(row[2] - deadline) < 0.001

    decoded = Message.from_json(msg.to_json())
    assert (decoded.task_id, decoded.attempt, decoded.frame_timestamp) == (task_id, 2, "12345.678")

    store = ConflictStore(tmp_path / "conflict.db")
    router = Router([HTTPProvider("mock_a", providers["a"])], health_check=False,
                    conflict_store=store)
    ctx = TaskContext(task_id=task_id, attempt=2, frame_timestamp="12345.678", deadline=deadline)
    result = router.predict(frame.jpeg_bytes, task_context=ctx)
    assert result.ok
    assert result.task_id == task_id
    assert result.decision is not None


# ---------------------------------------------------------------- TC4.2 / TC4.3

def test_tc4_2_results_claim_first_writer_wins_concurrent(tmp_path):
    """Spec 2: INSERT ... ON CONFLICT DO NOTHING; exactly one writer claims."""
    store = ConflictStore(tmp_path / "conflict.db")
    task_id = "task-claim-1"
    outcomes = []
    barrier = threading.Barrier(10)

    def claim(who):
        barrier.wait()
        outcomes.append(store.claim_result(task_id, f"provider-{who}", f"decision-{who}"))

    threads = [threading.Thread(target=claim, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert outcomes.count(True) == 1
    assert store.get_result(task_id)["task_id"] == task_id


def test_tc4_3_discarded_table_records_reasons(tmp_path):
    """Spec 3: duplicate|late|mismatch are all recorded, never dropped silently."""
    store = ConflictStore(tmp_path / "conflict.db")
    store.record_discard("t1", "p1", "duplicate")
    store.record_discard("t2", "p1", "late")
    store.record_discard("t3", "p2", "mismatch")
    store.record_discard("t1", "p2", "duplicate")
    counts = store.discard_counts()
    assert counts == {"duplicate": 2, "late": 1, "mismatch": 1}


# ---------------------------------------------------------------- TC4.4

def test_tc4_4_router_rejects_task_id_mismatch_and_failover_sequential(providers, frame, tmp_path):
    """Spec 4: mismatched task_id is rejected; the next provider is tried in order."""
    from fastapi import Body, FastAPI
    from fastapi.responses import JSONResponse

    bad = FastAPI()

    @bad.get("/health")
    async def health():
        return {"status": "ok", "provider": "bad"}

    @bad.post("/predict")
    async def predict_bad(body: bytes = Body(...)):
        if not body:
            return JSONResponse(status_code=400, content={"detail": "empty"})
        return JSONResponse(
            {"label": "wrong", "confidence": 0.9, "mock": True, "task_id": "WRONG-ID", "model": "mock"},
            headers={"X-Task-Id": "WRONG-ID"},
        )

    port = _free_port()
    srv, thread = _start(bad, port)
    try:
        store = ConflictStore(tmp_path / "conflict.db")
        router = Router(
            [HTTPProvider("bad", f"http://127.0.0.1:{port}/predict"),
             HTTPProvider("good", providers["b"])],
            health_check=False, conflict_store=store,
        )
        ctx = TaskContext(task_id="correct-task", deadline=time.time() + 30)
        result = router.predict(frame.jpeg_bytes, task_context=ctx)
        assert result.ok
        assert result.provider == "good"
        assert result.task_id == "correct-task"
        assert result.attempts == 2
        assert store.discard_counts().get("mismatch", 0) >= 1
    finally:
        srv.should_exit = True
        thread.join(timeout=5)


# ---------------------------------------------------------------- TC4.5

def test_tc4_5_two_hundred_trials_race_through_real_claim_path(providers, frame, tmp_path):
    """Spec 2 + review 5: 200 trials of two real worker responses racing; one survives.

    Two workers (separate processed DBs, as separate cloud replicas would have)
    run the real worker path on the same task. Both do inference and both enter
    the worker's claim code, so this is the claim path, not a direct store write.
    """
    store = ConflictStore(tmp_path / "conflict.db")
    gate = DecisionGate(tmp_path / "conflict.db")
    workers = []
    for name, endpoint in (("w1", providers["a"]), ("w2", providers["b"])):
        router = Router([HTTPProvider(name, endpoint)], health_check=False, conflict_store=store)
        workers.append(make_worker(tmp_path, name, router, store, gate))

    rng = random.Random(424242)
    trials = 200
    for trial in range(trials):
        message = message_for(f"task-race-{trial}", frame.jpeg_bytes,
                              frame_timestamp=f"{trial:04d}", deadline=time.time() + 30)
        barrier = threading.Barrier(2)

        def run(worker, msg=message, bar=barrier):
            bar.wait()
            time.sleep(rng.uniform(0, 0.002))  # jitter so the two responses interleave
            deliver(worker, msg)

        threads = [threading.Thread(target=run, args=(w,)) for w in workers]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        with sqlite3.connect(str(tmp_path / "conflict.db")) as conn:
            results_rows = conn.execute(
                "SELECT provider FROM results WHERE task_id=?", (message.message_id,)
            ).fetchall()
            applied_rows = conn.execute(
                "SELECT decision FROM applied_ids WHERE task_id=?", (message.message_id,)
            ).fetchall()
        assert len(results_rows) == 1, f"trial {trial}: exactly one response may claim the result"
        assert len(applied_rows) == 1, f"trial {trial}: exactly one decision may be applied"

    # Every losing response went through the worker's duplicate discard path.
    assert store.discard_counts().get("duplicate", 0) == trials


# ---------------------------------------------------------------- TC4.6 / TC4.7

def test_tc4_6_decision_gate_rules_and_survives_restart(tmp_path):
    """Spec 5: gate rejects re-apply, expired deadline and older frames; survives restart."""
    gate = DecisionGate(tmp_path / "gate.db")
    now = time.time()
    first = gate.try_apply("t2", "go", frame_timestamp=200.0, deadline=now + 30, now=now)
    assert first.applied and first.reason == "applied"

    older = gate.try_apply("t1", "stop", frame_timestamp=100.0, deadline=now + 30, now=now)
    assert not older.applied and older.reason == "newer_frame_decided"

    again = gate.try_apply("t2", "go2", frame_timestamp=200.0, deadline=now + 30, now=now)
    assert not again.applied and again.reason == "already_applied"

    expired = gate.try_apply("t3", "go", frame_timestamp=300.0, deadline=now - 1, now=now)
    assert not expired.applied and expired.reason == "deadline_passed"

    restarted = DecisionGate(tmp_path / "gate.db")
    assert restarted.is_applied("t2")
    after_restart = restarted.try_apply("t2", "go3", frame_timestamp=200.0, deadline=now + 30, now=now)
    assert not after_restart.applied and after_restart.reason == "already_applied"


def test_tc4_7_timeout_safe_default_late_never_applied(tmp_path):
    """Spec 6: expired deadline -> safe default; the late answer never applies."""
    gate = DecisionGate(tmp_path / "gate.db")
    now = time.time()
    res = gate.try_apply("late-task", "model_answer", frame_timestamp=10.0, deadline=now - 5, now=now)
    assert not res.applied
    assert res.reason == "deadline_passed"
    assert res.decision == "safe_default"
    assert gate.store.discard_counts().get("late", 0) >= 1

    later = gate.try_apply("late-task", "model_answer", frame_timestamp=10.0, deadline=now - 5, now=now + 1)
    assert not later.applied


# ---------------------------------------------------------------- TC4.8

def test_tc4_8_one_thousand_tasks_full_worker_path_with_random_faults(providers, frame, tmp_path):
    """Spec 7: 1000 tasks through worker+router+gate with injected faults."""
    rng = random.Random(20261107)
    requests.get(f"{providers['ca']}/control/random",
                 params={"fail_p": 0.15, "slow_p": 0.15, "slow_max_ms": 250, "seed": 11}, timeout=5)
    requests.get(f"{providers['cb']}/control/random",
                 params={"fail_p": 0.15, "slow_p": 0.15, "slow_max_ms": 250, "seed": 22}, timeout=5)

    store = ConflictStore(tmp_path / "conflict.db")
    gate = DecisionGate(tmp_path / "conflict.db")
    workers = []
    for name, endpoint in (("w1", providers["a"]), ("w2", providers["b"])):
        router = Router([HTTPProvider(f"{name}-a", providers["a"]),
                         HTTPProvider(f"{name}-b", providers["b"])],
                        health_check=False, conflict_store=store, recheck_interval_sec=5.0)
        workers.append(make_worker(tmp_path, name, router, store, gate))

    total = 1000
    injected_expired = 0
    injected_expired_deliveries = 0
    injected_duplicates = 0
    expired_ids: set[str] = set()
    for i in range(total):
        task_id = f"t8-{i}"
        expired = rng.random() < 0.10
        if expired:
            deadline = time.time() - 1
            injected_expired += 1
            expired_ids.add(task_id)
        else:
            deadline = time.time() + 0.5
        msg = message_for(task_id, frame.jpeg_bytes, frame_timestamp=f"{i:08d}", deadline=deadline)
        deliver(workers[i % len(workers)], msg)
        if expired:
            injected_expired_deliveries += 1
        if rng.random() < 0.15:
            injected_duplicates += 1
            deliver(workers[i % len(workers)], msg)
            if expired:
                injected_expired_deliveries += 1

    with sqlite3.connect(str(tmp_path / "conflict.db")) as conn:
        applied_rows = conn.execute("SELECT task_id, decision FROM applied_ids").fetchall()
        result_rows = conn.execute("SELECT COUNT(*) FROM results").fetchone()[0]
    applied = dict(applied_rows)
    applied_model = sum(1 for d in applied.values() if d != "safe_default")
    applied_safe_default = sum(1 for d in applied.values() if d == "safe_default")
    # Injected expired deadlines produce a safe_default decision that the gate
    # correctly refuses to apply (its own deadline already passed).
    injected_safe_default = sum(1 for tid in expired_ids if tid not in applied)
    applied_total = len(applied_rows)
    failed = total - applied_total - injected_safe_default
    discards = store.discard_counts()
    a_stats, b_stats = stats(providers["ca"]), stats(providers["cb"])
    injected_500s = a_stats.get("fail", 0) + b_stats.get("fail", 0)
    injected_slow = a_stats.get("slow", 0) + b_stats.get("slow", 0)
    with sqlite3.connect(str(tmp_path / "conflict.db")) as conn:
        late_rows = conn.execute(
            "SELECT task_id, COUNT(*) FROM discarded WHERE reason='late' GROUP BY task_id"
        ).fetchall()
    late_injected = sum(c for tid, c in late_rows if tid in expired_ids)
    organic_late = sum(c for tid, c in late_rows if tid not in expired_ids)

    assert applied_total <= total
    assert failed >= 0, f"classification over-counted: {failed}"
    assert applied_model + applied_safe_default + injected_safe_default + failed == total
    assert result_rows <= applied_total  # duplicates never create a second result row
    with sqlite3.connect(str(tmp_path / "conflict.db")) as conn:
        dup_tasks = conn.execute(
            "SELECT COUNT(*) FROM (SELECT task_id FROM applied_ids GROUP BY task_id HAVING COUNT(*)>1)"
        ).fetchone()[0]
    assert dup_tasks == 0

    block = f"""
<!-- TC4_8_FULL_WORKER:START -->
### TC4.8 (rewritten) 1000 tasks through worker + router + gate

Injected (configured by the test, not observed rates):
- random provider 500s: fail_p=0.15 on each of 2 providers
- random provider delays: slow_p=0.15, delay uniform 0-250 ms (some exceed the per-attempt budget)
- duplicate deliveries: 15% of tasks delivered twice ({injected_duplicates} extra deliveries)
- expired deadlines: 10% of tasks ({injected_expired} tasks) - injected, NOT a late rate

Observed outcomes:
| Outcome | Count |
|---|---|
| applied (model decision) | {applied_model} |
| applied safe_default (organic provider failure before deadline) | {applied_safe_default} |
| safe_default from injected expired deadline (not applied) | {injected_safe_default} |
| failed (no decision; organic) | {failed} |
| total tasks | {total} |
| applied more than once | {dup_tasks} |

Observed discards:
| Reason | Count |
|---|---|
| late (from injected expired deadlines) | {late_injected} |
| late (organic) | {organic_late} |
| duplicate | {discards.get("duplicate", 0)} |
| mismatch | {discards.get("mismatch", 0)} |

Injected provider counters (from mock /control/stats):
- HTTP 500 responses: {injected_500s}
- delayed responses: {injected_slow}

Organic observations:
- late discards not attributable to injected expired deadlines: {organic_late}
- client-side timeouts are not separately counted by the mock; they appear as failed attempts and, when both providers fail, as safe_default decisions.
- injected expired deliveries (including duplicates): {injected_expired_deliveries}
<!-- TC4_8_FULL_WORKER:END -->
"""
    _upsert(block, "TC4_8_FULL_WORKER")


# ---------------------------------------------------------------- TC4.9

def test_tc4_9_deadline_derived_attempt_timeout(providers, frame):
    """Review 2: budget per attempt from remaining deadline / remaining providers."""
    requests.get(f"{providers['ca']}/control/hang?hang=1", timeout=5)
    deadline_sec = 1.5

    def one_task(router, task_id):
        deadline = time.time() + deadline_sec
        ctx = TaskContext(task_id=task_id, deadline=deadline)
        began = time.perf_counter()
        result = router.predict(frame.jpeg_bytes, task_context=ctx)
        elapsed = (time.perf_counter() - began) * 1000
        ok = bool(result.ok and result.provider == "mock_b" and elapsed < deadline_sec * 1000)
        return ok, result, elapsed

    # Group 1: first 20 tasks after A starts hanging; breaker cannot open.
    routers_1 = Router(
        [HTTPProvider("mock_a", providers["a"], connect_timeout=1.0, read_timeout=2.0),
         HTTPProvider("mock_b", providers["b"], connect_timeout=1.0, read_timeout=2.0)],
        health_check=False, failure_threshold=10_000, min_attempt_timeout_sec=0.2,
    )
    group1 = []
    for i in range(20):
        ok, result, elapsed = one_task(routers_1, f"t9-first-{i}")
        group1.append({"ok": ok, "elapsed_ms": round(elapsed, 1),
                       "attempt_timeouts_ms": result.attempt_timeouts_ms,
                       "errors": result.errors[-2:]})

    # Group 2: same setup, but prime the breaker open before measuring.
    routers_2 = Router(
        [HTTPProvider("mock_a", providers["a"], connect_timeout=1.0, read_timeout=2.0),
         HTTPProvider("mock_b", providers["b"], connect_timeout=1.0, read_timeout=2.0)],
        health_check=False, failure_threshold=3, cooldown_sec=60.0, min_attempt_timeout_sec=0.2,
    )
    for i in range(3):
        one_task(routers_2, f"t9-prime-{i}")
    assert routers_2._cb["mock_a"].state == "open"
    group2 = []
    for i in range(20):
        ok, result, elapsed = one_task(routers_2, f"t9-open-{i}")
        group2.append({"ok": ok, "elapsed_ms": round(elapsed, 1),
                       "attempt_timeouts_ms": result.attempt_timeouts_ms,
                       "errors": result.errors[-2:]})

    rate_1 = sum(1 for r in group1 if r["ok"]) / len(group1)
    rate_2 = sum(1 for r in group2 if r["ok"]) / len(group2)
    assert rate_1 == 1.0, group1
    assert rate_2 == 1.0, group2

    block = f"""
<!-- TC4_9_DEADLINE_TIMEOUT:START -->
### TC4.9 deadline-derived attempt timeout (A hangs, B healthy, deadline 1.5 s)

| Group | Tasks | Success | Notes |
|---|---|---|---|
| first 20 after A starts hanging (breaker closed) | 20 | {rate_1:.0%} | A gets ~750 ms (1.5 s / 2 providers), B gets the rest |
| first 20 after the breaker opened | 20 | {rate_2:.0%} | A skipped, B answers immediately |

Group 1 measured attempt budgets: {sorted({t for r in group1 for t in (r["attempt_timeouts_ms"] or [])})} ms
Group 1 elapsed: min {min(r["elapsed_ms"] for r in group1):.0f} / median {sorted(r["elapsed_ms"] for r in group1)[len(group1)//2]:.0f} / max {max(r["elapsed_ms"] for r in group1):.0f} ms
Group 2 elapsed: min {min(r["elapsed_ms"] for r in group2):.0f} / median {sorted(r["elapsed_ms"] for r in group2)[len(group2)//2]:.0f} / max {max(r["elapsed_ms"] for r in group2):.0f} ms
<!-- TC4_9_DEADLINE_TIMEOUT:END -->
"""
    _upsert(block, "TC4_9_DEADLINE_TIMEOUT")


# ---------------------------------------------------------------- TC4.10

def test_tc4_10_worker_crashes_after_inference_before_ack(providers, frame, tmp_path):
    """Review 6: crash after inference/before ACK; resend differs; one decision applied."""
    requests.get(f"{providers['ca']}/control/label", params={"value": "first"}, timeout=5)
    message = message_for("task-crash-1", frame.jpeg_bytes, frame_timestamp="10.0",
                          deadline=time.time() + 60)
    message_path = tmp_path / "message.json"
    message_path.write_text(message.to_json(), encoding="utf-8")
    result_path = tmp_path / "first_result.json"
    conflict_db = tmp_path / "conflict.db"
    processed_db = tmp_path / "processed.db"

    env = {**os.environ, "PYTHONPATH": str(PROJECT_ROOT)}
    proc = subprocess.run(
        [sys.executable, str(HERE / "_crash_worker.py"),
         "--endpoint", providers["a"],
         "--message", str(message_path),
         "--conflict-db", str(conflict_db),
         "--processed-db", str(processed_db),
         "--result-out", str(result_path)],
        capture_output=True, text=True, env=env, timeout=120,
    )
    assert proc.returncode == 3, proc.stderr
    first = json.loads(result_path.read_text(encoding="utf-8"))
    assert first["decision"] == "first"

    # The message is resent; this inference returns a different decision.
    requests.get(f"{providers['ca']}/control/label", params={"value": "second"}, timeout=5)
    gate = DecisionGate(conflict_db)
    store = ConflictStore(conflict_db)
    router = Router([HTTPProvider("mock_a", providers["a"])], health_check=False,
                    conflict_store=store)
    worker = make_worker(tmp_path, "live", router, store, gate)
    deliver(worker, message)

    with sqlite3.connect(str(conflict_db)) as conn:
        applied = conn.execute(
            "SELECT decision FROM applied_ids WHERE task_id=?", (message.message_id,)
        ).fetchall()
        claimed = conn.execute(
            "SELECT decision FROM results WHERE task_id=?", (message.message_id,)
        ).fetchall()
    assert len(applied) == 1 and applied[0][0] == "first"
    assert len(claimed) == 1 and claimed[0][0] == "first"
    assert store.discard_counts().get("duplicate", 0) == 1


# ---------------------------------------------------------------- TC4.11

def test_tc4_11_edge_killed_mid_task_restarted_in_separate_process(providers, frame, tmp_path):
    """Review 4: real separate process killed mid-task; restart keeps one task_id."""
    outbox_path = tmp_path / "edge.db"
    image_path = tmp_path / "frame.jpg"
    image_path.write_bytes(frame.jpeg_bytes)
    task_id = "task-edge-restart-1"
    env = {**os.environ, "PYTHONPATH": str(PROJECT_ROOT)}

    killed = subprocess.run(
        [sys.executable, str(HERE / "_edge_restart.py"), "enqueue",
         "--outbox", str(outbox_path), "--image", str(image_path), "--task-id", task_id],
        capture_output=True, text=True, env=env, timeout=120,
    )
    assert killed.returncode == 7, killed.stderr
    assert json.loads(killed.stdout.strip().splitlines()[-1])["enqueued"] == task_id

    restarted = subprocess.run(
        [sys.executable, str(HERE / "_edge_restart.py"), "resend", "--outbox", str(outbox_path)],
        capture_output=True, text=True, env=env, timeout=120,
    )
    assert restarted.returncode == 0, restarted.stderr
    pending = json.loads(restarted.stdout.strip().splitlines()[-1])["pending"]
    assert len(pending) == 1
    recovered = Message.from_json(pending[0])
    assert recovered.message_id == task_id

    store = ConflictStore(tmp_path / "conflict.db")
    gate = DecisionGate(tmp_path / "conflict.db")
    router = Router([HTTPProvider("mock_a", providers["a"])], health_check=False,
                    conflict_store=store)
    worker = make_worker(tmp_path, "restarted", router, store, gate)
    deliver(worker, recovered)
    deliver(worker, recovered)  # redelivery after the restarted edge republishes

    with sqlite3.connect(str(tmp_path / "conflict.db")) as conn:
        applied = conn.execute(
            "SELECT COUNT(*) FROM applied_ids WHERE task_id=?", (task_id,)
        ).fetchone()[0]
        claimed = conn.execute(
            "SELECT COUNT(*) FROM results WHERE task_id=?", (task_id,)
        ).fetchone()[0]
    assert applied == 1
    assert claimed == 1
    assert gate.is_applied(task_id)


# ---------------------------------------------------------------- TC4.12

def test_tc4_12_a_late_after_b_simulated_through_claim_path(frame, tmp_path):
    """Review 4: A-late-after-B cannot happen over sequential HTTP; simulate it.

    The router waits for A's timeout before calling B, so A cannot answer after
    B. This test models the post-hoc arrival at the claim/gate path instead.
    """
    store = ConflictStore(tmp_path / "conflict.db")
    gate = DecisionGate(tmp_path / "conflict.db")
    task_id = "task-a-late-1"

    won = store.claim_result(task_id, "B", "decision_b")
    assert won
    applied_b = gate.try_apply(task_id, "decision_b", frame_timestamp=200.0,
                               deadline=time.time() + 30)
    assert applied_b.applied

    # A's answer for an older frame arrives afterwards.
    assert store.claim_result(task_id, "A", "decision_a") is False
    store.record_discard(task_id, "A", "late")
    applied_a = gate.try_apply(task_id, "decision_a", frame_timestamp=100.0,
                               deadline=time.time() + 30)
    assert not applied_a.applied

    with sqlite3.connect(str(tmp_path / "conflict.db")) as conn:
        result = conn.execute("SELECT provider, decision FROM results WHERE task_id=?",
                              (task_id,)).fetchone()
        applied = conn.execute("SELECT decision FROM applied_ids WHERE task_id=?",
                               (task_id,)).fetchall()
    assert result == ("B", "decision_b")
    assert applied == [("decision_b",)]
    assert store.discard_counts().get("late", 0) == 1


# ---------------------------------------------------------------- docs helper

def _upsert(block: str, marker: str) -> None:
    path = PROJECT_ROOT / "docs" / "measurements.md"
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    start_tag = f"<!-- {marker}:START -->"
    end_tag = f"<!-- {marker}:END -->"
    if start_tag in text and end_tag in text:
        start = text.index(start_tag)
        end = text.index(end_tag) + len(end_tag)
        text = text[:start] + block.strip() + text[end:]
    else:
        text = text.rstrip() + "\n\n" + block.strip() + "\n"
    path.write_text(text, encoding="utf-8")
