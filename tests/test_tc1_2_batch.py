"""TC1.2 — 20 back-to-back requests all complete; avg/min/max are logged.

The benchmark writes one CSV row per request before the run ends, so the
assertions read the same numbers the user sees, recomputed from the file.
"""

from __future__ import annotations

import csv
import logging
from pathlib import Path

import pytest

from phase1_pipeline.benchmark import (
    CSV_FIELDS,
    render_measurement,
    run_benchmark,
    update_measurements,
)
from phase1_pipeline.capture import FrameGrabber
from phase1_pipeline.client import CloudClient

REQUEST_COUNT = 20


def _read_rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def test_tc1_2_twenty_back_to_back_requests_complete(
    client: CloudClient,
    grabber: FrameGrabber,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    csv_path = tmp_path / "latency.csv"
    with caplog.at_level(logging.INFO, logger="benchmark"):
        summary = run_benchmark(client, grabber, count=REQUEST_COUNT, csv_path=csv_path, progress=False)

    rows = _read_rows(csv_path)

    # Every attempt is recorded, in order, with the documented columns.
    assert len(rows) == REQUEST_COUNT
    assert list(rows[0].keys()) == list(CSV_FIELDS)
    assert [int(row["index"]) for row in rows] == list(range(1, REQUEST_COUNT + 1))

    # All 20 completed successfully and returned a prediction.
    assert summary.total == REQUEST_COUNT
    assert summary.succeeded == REQUEST_COUNT
    assert summary.failed == 0
    assert all(row["ok"] == "True" for row in rows)
    assert all(row["label"] for row in rows)

    # Average/min/max exist, are ordered sensibly, and match the CSV.
    assert summary.average_ms is not None
    assert summary.minimum_ms is not None
    assert summary.maximum_ms is not None
    assert summary.minimum_ms <= summary.average_ms <= summary.maximum_ms

    latencies = [float(row["latency_ms"]) for row in rows]
    assert summary.average_ms == pytest.approx(sum(latencies) / len(latencies), abs=0.01)
    assert summary.minimum_ms == pytest.approx(min(latencies), abs=0.001)
    assert summary.maximum_ms == pytest.approx(max(latencies), abs=0.001)

    # The summary was printed, not just stored.
    assert f"{REQUEST_COUNT}/{REQUEST_COUNT} succeeded" in caplog.text
    assert "avg=" in caplog.text and "min=" in caplog.text and "max=" in caplog.text


def test_tc1_2_rejects_a_non_positive_count(client: CloudClient, grabber: FrameGrabber) -> None:
    with pytest.raises(ValueError, match="count must be at least 1"):
        run_benchmark(client, grabber, count=0, csv_path=None, progress=False)


def test_tc1_2_latency_is_written_to_the_measurements_doc(
    client: CloudClient, grabber: FrameGrabber, tmp_path: Path
) -> None:
    csv_path = tmp_path / "latency.csv"
    summary = run_benchmark(client, grabber, count=3, csv_path=csv_path, progress=False)

    block = render_measurement(
        summary, mode="mock", endpoint=client.endpoint, csv_path=csv_path
    )
    doc = tmp_path / "measurements.md"
    doc.write_text(
        "# Measurements\n\n"
        "<!-- PHASE1_BASELINE:START -->\n_old_\n<!-- PHASE1_BASELINE:END -->\n",
        encoding="utf-8",
    )

    update_measurements(doc, block)
    update_measurements(doc, block)  # a re-run replaces the section, never duplicates it

    text = doc.read_text(encoding="utf-8")
    assert text.count("PHASE1_BASELINE:START") == 1
    assert text.count("PHASE1_BASELINE:END") == 1
    assert "_old_" not in text
    assert "Average round trip" in text
    assert f"{summary.average_ms:.3f} ms" in text
    assert "mock" in text
