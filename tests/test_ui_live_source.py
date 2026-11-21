"""UI live data source: read-only parsing of real run artifacts."""

from __future__ import annotations

import csv
import json
import sqlite3
from pathlib import Path

from ui.data import LiveSource


def test_ui_live_source_reads_sqlite_csv_and_telemetry(tmp_path, monkeypatch):
    run_dir = tmp_path / "logs" / "live" / "20260101T000000Z"
    run_dir.mkdir(parents=True)

    outbox = sqlite3.connect(run_dir / "outbox.db")
    outbox.execute("CREATE TABLE outbox (message_id TEXT PRIMARY KEY, state TEXT, retry_count INTEGER, last_attempt_at REAL)")
    outbox.executemany(
        "INSERT INTO outbox VALUES (?,?,?,?)",
        [("t1", "acked", 0, 0), ("t2", "acked", 0, 0),
         ("t3", "pending", 2, 1700000000.0)],
    )
    outbox.commit()
    outbox.close()

    store = sqlite3.connect(run_dir / "conflict.db")
    store.execute("CREATE TABLE results (task_id TEXT PRIMARY KEY, provider TEXT, decision TEXT, claimed_at TEXT)")
    store.execute("CREATE TABLE discarded (id INTEGER PRIMARY KEY, task_id TEXT, provider TEXT, reason TEXT, received_at TEXT)")
    store.execute("CREATE TABLE applied_ids (task_id TEXT PRIMARY KEY, decision TEXT)")
    store.execute("CREATE TABLE actuation_log (task_id TEXT PRIMARY KEY, command_id TEXT, action TEXT, parameters TEXT, acked INTEGER, reason TEXT, detail TEXT, sent_at TEXT, acked_at TEXT, latency_ms REAL)")
    store.executemany("INSERT INTO results VALUES (?,?,?,?)", [
        ("t1", "mock-a", "bolt", "2026-01-01T00:00:01Z"),
        ("t2", "mock-a", "safe_default", "2026-01-01T00:00:02Z"),
    ])
    store.executemany("INSERT INTO discarded VALUES (?,?,?,?,?)", [
        (1, "t2", "mock-b", "late", "2026-01-01T00:00:03Z"),
        (2, "t3", "mock-b", "duplicate", "2026-01-01T00:00:04Z"),
    ])
    store.executemany("INSERT INTO applied_ids VALUES (?,?)", [("t1", "bolt"), ("t2", "safe_default")])
    store.executemany("INSERT INTO actuation_log VALUES (?,?,?,?,?,?,?,?,?,?)", [
        ("t1", "cmd-1", "grip", "{}", 1, "ack", "", "2026-01-01T00:00:01Z", "2026-01-01T00:00:02Z", 12.5),
        ("t2", "cmd-2", "release", "{}", 0, "timeout", "", "2026-01-01T00:00:02Z", None, None),
        ("t3", "cmd-3", "grip", "{}", 0, "safe_state", "", "2026-01-01T00:00:03Z", "2026-01-01T00:00:04Z", 8.0),
    ])
    store.commit()
    store.close()

    with (run_dir / "latency.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["task_id", "result_at_utc", "edge_latency_ms", "provider", "attempts"])
        writer.writerow(["t1", "2026-01-01T00:00:02Z", "44.5", "mock-a", "1"])
        writer.writerow(["t2", "2026-01-01T00:00:03Z", "91.0", "mock-b", "2"])

    telemetry = tmp_path / "telemetry.jsonl"
    telemetry.write_text("\n".join([
        json.dumps({"ts": "2026-01-01T00:00:01Z", "kind": "provider_health",
                    "provider": "mock-a", "healthy": True, "circuit": "closed"}),
        json.dumps({"ts": "2026-01-01T00:00:02Z", "kind": "provider_result",
                    "provider": "mock-a", "ok": True, "ewma_ms": 44.5,
                    "latency_ms": 44.5, "circuit": "closed"}),
        json.dumps({"ts": "2026-01-01T00:00:03Z", "kind": "provider_result",
                    "provider": "mock-b", "ok": False, "error": "timeout",
                    "circuit": "open", "ewma_ms": 260.0}),
        json.dumps({"ts": "2026-01-01T00:00:03Z", "kind": "failover",
                    "provider": "mock-b", "attempts": 2}),
    ]) + "\n", encoding="utf-8")
    monkeypatch.setenv("MQTT_BROKER_PORT", "1")
    source = LiveSource(project_root=tmp_path, telemetry_path=telemetry)
    state = source.state(run={"status": "completed"})

    assert state["demo"] is False and state["banner"] is None
    assert state["counts"]["sent"] == 3
    assert state["counts"]["acked"] == 2
    assert state["counts"]["applied"] == 2
    assert state["counts"]["safe_default"] == 1
    assert state["counts"]["outbox_depth"] == 1
    assert state["counts"]["unconfirmed"] == 1
    assert state["counts"]["discarded"] == {"duplicate": 1, "late": 1, "mismatch": 0}
    assert state["run"]["status"] == "completed"

    providers = {item["name"]: item for item in state["providers"]}
    assert providers["mock-a"]["health"] == "healthy"
    assert providers["mock-a"]["circuit"] == "closed"
    assert providers["mock-b"]["health"] == "failed"
    assert providers["mock-b"]["circuit"] == "open"
    assert providers["mock-b"]["last_failover"] == "2026-01-01T00:00:03Z"

    latency = source.latency()
    assert latency["count"] == 2 and latency["p50"] == 67.75
    kinds = {event["kind"] for event in source.events()}
    assert {"discard", "nack", "retry", "failover", "provider_error"} <= kinds
    rows = source.actuation()
    assert len(rows) == 3
    assert rows[0]["task_id"] in {"t1", "t2", "t3"}
    assert rows[0]["parameters"] == {}
