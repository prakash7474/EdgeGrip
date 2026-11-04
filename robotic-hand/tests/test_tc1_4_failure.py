"""TC1.4 — a dropped connection or short timeout is caught and logged.

Each case checks three things: the failure is detected, it is recorded, and the
process keeps working afterwards. No external network is touched: one case uses
a closed loopback port, the other a deliberately slow mock endpoint.
"""

from __future__ import annotations

import csv
import logging
import socket
from pathlib import Path

import pytest

from phase1_pipeline.benchmark import run_benchmark
from phase1_pipeline.capture import FrameGrabber
from phase1_pipeline.client import (
    ERROR_CONNECTION,
    ERROR_HTTP,
    ERROR_TIMEOUT,
    CloudClient,
)

IMAGES_DIR = Path(__file__).resolve().parent.parent / "images"
SHORT_TIMEOUT_SECONDS = 0.2


def _closed_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def test_tc1_4_short_timeout_is_caught(slow_endpoint: str, grabber: FrameGrabber) -> None:
    frame = grabber.next_frame()
    with CloudClient(f"{slow_endpoint}?delay_ms=1500", timeout_seconds=SHORT_TIMEOUT_SECONDS) as client:
        result = client.predict(frame.jpeg_bytes, image_name=frame.name)

    assert not result.ok
    assert result.error_kind == ERROR_TIMEOUT
    assert result.error_message
    assert result.prediction is None
    # The client gave up near its timeout instead of hanging for the full delay.
    assert result.latency_ms < 1200, f"client waited {result.latency_ms:.1f} ms"


def test_tc1_4_dropped_connection_is_caught(grabber: FrameGrabber) -> None:
    frame = grabber.next_frame()
    endpoint = f"http://127.0.0.1:{_closed_port()}/predict"
    with CloudClient(endpoint, timeout_seconds=1.0) as client:
        result = client.predict(frame.jpeg_bytes, image_name=frame.name)

    assert not result.ok
    assert result.error_kind in {ERROR_CONNECTION, ERROR_TIMEOUT}
    assert result.error_message
    assert result.status_code is None


def test_tc1_4_invalid_payload_is_reported(client: CloudClient) -> None:
    result = client.predict(b"this is not a jpeg", image_name="garbage")

    assert not result.ok
    assert result.error_kind == ERROR_HTTP
    assert result.status_code == 400


def test_tc1_4_failure_is_logged_to_csv_without_crashing(
    slow_endpoint: str,
    grabber: FrameGrabber,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    csv_path = tmp_path / "failures.csv"
    endpoint = f"{slow_endpoint}?delay_ms=1500"
    with CloudClient(endpoint, timeout_seconds=SHORT_TIMEOUT_SECONDS) as client:
        with caplog.at_level(logging.WARNING, logger="benchmark"):
            summary = run_benchmark(client, grabber, count=2, csv_path=csv_path, progress=True)

    assert summary.total == 2
    assert summary.succeeded == 0
    assert summary.failed == 2
    assert summary.average_ms is None

    with csv_path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 2
    assert all(row["ok"] == "False" for row in rows)
    assert all(row["error_kind"] == ERROR_TIMEOUT for row in rows)
    assert "timeout" in caplog.text


def test_tc1_4_client_recovers_after_a_failure(endpoint: str, slow_endpoint: str, grabber: FrameGrabber) -> None:
    frame = grabber.next_frame()

    with CloudClient(f"{slow_endpoint}?delay_ms=1500", timeout_seconds=SHORT_TIMEOUT_SECONDS) as failing:
        failed = failing.predict(frame.jpeg_bytes, image_name=frame.name)
    assert not failed.ok

    with CloudClient(endpoint, timeout_seconds=10.0) as healthy:
        recovered = healthy.predict(frame.jpeg_bytes, image_name=frame.name)
    assert recovered.ok, f"{recovered.error_kind}: {recovered.error_message}"
    assert recovered.label
