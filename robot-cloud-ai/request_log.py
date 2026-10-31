"""CSV request logging.

One row per inference attempt, successes and failures alike. The API token is
never passed to this module, so it cannot end up in the log.
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # avoids a runtime import cycle with cloud.inference
    from cloud.inference import InferenceResult

FIELDS = [
    "task_id",
    "image_filename",
    "image_path",
    "request_time_utc",
    "response_time_utc",
    "latency_ms",
    "predicted_label",
    "confidence",
    "status",
    "http_status",
    "error_kind",
    "error_message",
    "model_id",
    "test_case",
    "expected_label",
    "prediction_correct",
]


def append_result(result: InferenceResult, log_path: str | Path) -> Path:
    """Append one result row, writing the header for a new or empty file."""

    path = Path(log_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    is_empty = not path.exists() or path.stat().st_size == 0

    with path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        if is_empty:
            writer.writeheader()
        writer.writerow(result.as_row())

    return path


def read_results(log_path: str | Path) -> list[dict[str, str]]:
    """Read every recorded row. A missing log file yields an empty list."""

    path = Path(log_path)
    if not path.is_file():
        return []
    with path.open("r", newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        return [row for row in reader if row.get("task_id")]


def reset_log(log_path: str | Path) -> Path:
    """Truncate the log and write a fresh header row."""

    path = Path(log_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        csv.DictWriter(handle, fieldnames=FIELDS).writeheader()
    return path
