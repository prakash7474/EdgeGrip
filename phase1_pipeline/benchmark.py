"""Latency benchmark for the Phase 1 pipeline.

Sends N frames one after another, writes one CSV row per request, prints
average/min/max latency, and (optionally) records the run in
``docs/measurements.md``. Every number produced here comes from a real run;
nothing is estimated.

Usage::

    python -m phase1_pipeline.benchmark --count 20
    python -m phase1_pipeline.benchmark --count 20 --endpoint http://127.0.0.1:8000/predict
"""

from __future__ import annotations

import argparse
import csv
import logging
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from phase1_pipeline.capture import FrameGrabber
from phase1_pipeline.client import CloudClient, InferenceResult
from phase1_pipeline.config import PROJECT_ROOT, ConfigurationError, Settings, load_settings

LOG = logging.getLogger("benchmark")

CSV_FIELDS = (
    "index",
    "timestamp",
    "image_name",
    "ok",
    "status_code",
    "latency_ms",
    "label",
    "error_kind",
    "error_message",
)
SECTION_START = "<!-- PHASE1_BASELINE:START -->"
SECTION_END = "<!-- PHASE1_BASELINE:END -->"


@dataclass(frozen=True)
class BenchmarkRow:
    """One recorded request."""

    index: int
    timestamp: str
    image_name: str
    ok: bool
    status_code: int | None
    latency_ms: float
    label: str | None
    error_kind: str | None
    error_message: str | None

    @classmethod
    def from_result(cls, index: int, result: InferenceResult) -> "BenchmarkRow":
        """Build a row from a client result."""
        return cls(
            index=index,
            timestamp=result.sent_at,
            image_name=result.image_name,
            ok=result.ok,
            status_code=result.status_code,
            latency_ms=result.latency_ms,
            label=result.label,
            error_kind=result.error_kind,
            error_message=result.error_message,
        )


@dataclass(frozen=True)
class Summary:
    """Aggregate latency for one benchmark run."""

    total: int
    succeeded: int
    failed: int
    average_ms: float | None
    minimum_ms: float | None
    maximum_ms: float | None

    @property
    def success_rate(self) -> float:
        """Return the percentage of requests that produced a prediction."""
        return (self.succeeded / self.total * 100.0) if self.total else 0.0


def summarise(rows: list[BenchmarkRow]) -> Summary:
    """Compute totals and latency statistics over successful requests."""
    latencies = [row.latency_ms for row in rows if row.ok]
    succeeded = len(latencies)
    return Summary(
        total=len(rows),
        succeeded=succeeded,
        failed=len(rows) - succeeded,
        average_ms=round(sum(latencies) / succeeded, 3) if succeeded else None,
        minimum_ms=round(min(latencies), 3) if succeeded else None,
        maximum_ms=round(max(latencies), 3) if succeeded else None,
    )


def _write_header(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        csv.DictWriter(handle, fieldnames=CSV_FIELDS).writeheader()


def _append_row(path: Path, row: BenchmarkRow) -> None:
    with path.open("a", newline="", encoding="utf-8") as handle:
        csv.DictWriter(handle, fieldnames=CSV_FIELDS).writerow(asdict(row))


def run_benchmark(
    client: CloudClient,
    grabber: FrameGrabber,
    *,
    count: int = 20,
    csv_path: str | Path | None = None,
    progress: bool = True,
) -> Summary:
    """Send ``count`` requests back to back and record each one.

    Rows are flushed to the CSV as they happen, so an interrupted run keeps the
    measurements it already made. Failures are counted, never silently dropped.

    Args:
        client: Configured cloud client.
        grabber: Frame source (camera with sample-image fallback).
        count: Number of requests to send.
        csv_path: Destination CSV. None means "do not persist".
        progress: Log one line per request.
    """

    if count < 1:
        raise ValueError("count must be at least 1")

    destination = Path(csv_path) if csv_path else None
    if destination:
        _write_header(destination)

    rows: list[BenchmarkRow] = []
    for index in range(1, count + 1):
        frame = grabber.next_frame()
        result = client.predict(frame.jpeg_bytes, image_name=frame.name)
        row = BenchmarkRow.from_result(index, result)
        rows.append(row)
        if destination:
            _append_row(destination, row)
        if progress:
            if row.ok:
                LOG.info(
                    "request %3d  %-12s %8.1f ms  label=%s",
                    index, row.image_name, row.latency_ms, row.label,
                )
            else:
                LOG.warning(
                    "request %3d  %-12s %8.1f ms  %s: %s",
                    index, row.image_name, row.latency_ms,
                    row.error_kind, row.error_message,
                )

    summary = summarise(rows)
    LOG.info(
        "summary: %d/%d succeeded (%.1f%%), avg=%s min=%s max=%s",
        summary.succeeded,
        summary.total,
        summary.success_rate,
        format_ms(summary.average_ms),
        format_ms(summary.minimum_ms),
        format_ms(summary.maximum_ms),
    )
    return summary


def format_ms(value: float | None) -> str:
    """Format a latency value for logs and Markdown."""
    return "n/a" if value is None else f"{value:.3f} ms"


def _safe_target(endpoint: str) -> str:
    """Return ``host:port/path`` without query strings or credentials."""
    parts = urlsplit(endpoint)
    return f"{parts.hostname or '?'}:{parts.port or '?'}{parts.path or ''}"


def _display_path(path: str | Path) -> str:
    """Return the path relative to the project root when possible."""
    candidate = Path(path)
    try:
        return candidate.resolve().relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        return candidate.as_posix()


def render_measurement(
    summary: Summary,
    *,
    mode: str,
    endpoint: str,
    csv_path: str | Path | None,
    run_at: str | None = None,
) -> str:
    """Render the Phase 1 block for ``docs/measurements.md``."""
    timestamp = run_at or datetime.now(timezone.utc).isoformat(timespec="seconds")
    target = _safe_target(endpoint)

    if mode == "mock":
        context = (
            "These requests were served by the **local mock inference server over loopback**. "
            "They measure the pipeline's own overhead (capture, HTTP, JSON, mock handler), "
            "not a real cloud provider's round trip; no hosted API was called."
        )
    else:
        context = (
            "These requests were sent to the configured hosted endpoint. "
            "The endpoint URL is read from the environment, so reproduce it with the same "
            "`INFERENCE_ENDPOINT` to compare numbers."
        )

    source = f"`{_display_path(csv_path)}`" if csv_path else "not persisted"
    return "\n".join([
        SECTION_START,
        "### Phase 1 — baseline single-request cloud round trip",
        "",
        f"_Run: {timestamp} · mode: `{mode}` · target: `{target}` · source: {source}_",
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| Requests sent | {summary.total} |",
        f"| Completed | {summary.succeeded} |",
        f"| Failed | {summary.failed} |",
        f"| Success rate | {summary.success_rate:.1f}% |",
        f"| Average round trip | {format_ms(summary.average_ms)} |",
        f"| Minimum round trip | {format_ms(summary.minimum_ms)} |",
        f"| Maximum round trip | {format_ms(summary.maximum_ms)} |",
        "",
        "Latency is the client-side round trip measured with "
        "`time.perf_counter()` around the HTTP POST, including JSON parsing.",
        "",
        context,
        SECTION_END,
    ])


def replace_section(text: str, block: str) -> str:
    """Replace the Phase 1 block in ``text``, appending it when absent."""
    if SECTION_START in text and SECTION_END in text:
        start = text.index(SECTION_START)
        end = text.index(SECTION_END, start) + len(SECTION_END)
        return text[:start] + block + text[end:]
    separator = "" if text.endswith("\n") or not text else "\n"
    return f"{text}{separator}{block}\n"


def update_measurements(path: str | Path, block: str) -> Path:
    """Write ``block`` into the measurements document and return its path."""
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    existing = destination.read_text(encoding="utf-8") if destination.is_file() else ""
    destination.write_text(replace_section(existing, block), encoding="utf-8")
    return destination


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Phase 1 latency benchmark")
    parser.add_argument("--count", type=int, default=20, help="Requests to send (default 20)")
    parser.add_argument("--endpoint", help="Override INFERENCE_ENDPOINT")
    parser.add_argument("--timeout", type=float, help="Override INFERENCE_TIMEOUT_SECONDS")
    parser.add_argument("--image", help="Use one sample image for every request")
    parser.add_argument("--no-camera", action="store_true", help="Skip the camera probe")
    parser.add_argument("--csv", help="CSV output path (default from settings)")
    parser.add_argument("--no-doc", action="store_true", help="Do not update docs/measurements.md")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the benchmark from the command line."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")
    args = _build_parser().parse_args(argv)

    try:
        settings: Settings = load_settings()
    except ConfigurationError as exc:
        LOG.error("Configuration error: %s", exc)
        return 2

    endpoint = args.endpoint or settings.endpoint
    timeout = args.timeout if args.timeout is not None else settings.timeout_seconds
    csv_path = Path(args.csv) if args.csv else settings.log_path

    grabber = FrameGrabber(
        settings.images_dir,
        camera_enabled=settings.camera_enabled and not args.no_camera,
        camera_index=settings.camera_index,
    )
    if args.image:
        try:
            grabber.pin_sample(args.image)
        except Exception as exc:  # CaptureError is the expected case
            LOG.error("Could not pin sample image %s: %s", args.image, exc)
            return 2

    LOG.info(
        "benchmark start: %d requests -> %s (%s mode, timeout %.1f s)",
        args.count, endpoint, settings.mode, timeout,
    )
    try:
        with CloudClient(endpoint, timeout_seconds=timeout, api_key=settings.api_key) as client:
            summary = run_benchmark(client, grabber, count=args.count, csv_path=csv_path)
    except KeyboardInterrupt:
        LOG.error("Interrupted — CSV rows collected so far are already written")
        return 130

    if not args.no_doc:
        block = render_measurement(
            summary,
            mode=settings.mode,
            endpoint=endpoint,
            csv_path=csv_path,
        )
        path = update_measurements(settings.measurements_path, block)
        LOG.info("Measurements updated in %s", path)

    return 0 if summary.failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
