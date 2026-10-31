"""Command-line entry point for the Phase 1 cloud AI prototype.

Commands:
    single   TC1.1 — one valid image
    batch    TC1.2 — 20 sequential requests with latency statistics
    validate TC1.3 — labelled images compared against the model prediction
    timeout  TC1.4 — controlled network failure, then a recovery request
    all      run all four test cases and write the performance report
    report   rebuild the report from the existing CSV log

Run ``python main.py --help`` for the exact PowerShell-ready usage.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import replace
from pathlib import Path

import reporting
from cloud.inference import (
    InferenceProvider,
    InferenceResult,
    send_image_to_cloud,
)
from config import (
    DEFAULT_ROUTER_BASE_URL,
    ConfigurationError,
    Settings,
    load_settings,
)
from image_loader import list_images
from request_log import read_results, reset_log

LOG = logging.getLogger("robot-cloud-ai")

# A closed local port. Using it produces a real, controlled connection failure
# without touching the machine's network configuration.
DEFAULT_FAILURE_ENDPOINT = "http://127.0.0.1:9"


def _build_settings(args: argparse.Namespace, *, require_token: bool = True) -> Settings:
    settings = load_settings(require_token=require_token)
    updates: dict[str, object] = {}

    model = getattr(args, "model", None)
    if model and model != settings.model_id:
        updates["model_id"] = model
        updates["endpoint_url"] = f"{DEFAULT_ROUTER_BASE_URL}/{model}"

    endpoint = getattr(args, "endpoint", None)
    if endpoint:
        updates["endpoint_url"] = endpoint

    return replace(settings, **updates) if updates else settings


def _resolve_image(settings: Settings, value: str | None) -> Path:
    if value:
        return Path(value)
    images = list_images(settings.images_dir)
    if not images:
        raise ConfigurationError(
            f"No images found in {settings.images_dir}. Add JPG/PNG files and retry."
        )
    return images[0]


def _run_one(
    settings: Settings,
    image_path: str | Path,
    test_case: str,
    *,
    expected_label: str | None = None,
    timeout: tuple[float, float] | None = None,
    provider: InferenceProvider | None = None,
) -> InferenceResult:
    result = send_image_to_cloud(
        image_path,
        settings=settings,
        provider=provider,
        timeout=timeout,
        test_case=test_case,
        expected_label=expected_label,
    )
    _log_result(result)
    return result


def _log_result(result: InferenceResult) -> None:
    if result.succeeded:
        confidence = "n/a" if result.confidence is None else f"{result.confidence:.3f}"
        LOG.info(
            "%s %-13s success %8.1f ms  label=%r confidence=%s",
            result.task_id,
            result.image_filename,
            result.latency_ms,
            result.predicted_label,
            confidence,
        )
    else:
        LOG.error(
            "%s %-13s error   %8.1f ms  %s: %s",
            result.task_id,
            result.image_filename,
            result.latency_ms,
            result.error_kind,
            result.error_message,
        )


def run_single(args: argparse.Namespace) -> int:
    """TC1.1 — one valid image reaches the API and returns a prediction."""

    settings = _build_settings(args)
    image = _resolve_image(settings, getattr(args, "image", None))
    result = _run_one(settings, image, "TC1.1")

    if not result.succeeded:
        LOG.error("TC1.1 FAIL — %s: %s", result.error_kind, result.error_message)
        return 1

    within_target = result.latency_ms <= settings.latency_target_ms
    LOG.info(
        "TC1.1 PASS — label=%r confidence=%s in %.1f ms (%s the %.0f ms prototype target)",
        result.predicted_label,
        "n/a" if result.confidence is None else f"{result.confidence:.3f}",
        result.latency_ms,
        "within" if within_target else "OVER",
        settings.latency_target_ms,
    )
    return 0


def run_batch(args: argparse.Namespace) -> int:
    """TC1.2 — sequential requests; every attempt is logged and summarised."""

    settings = _build_settings(args)
    images = list_images(settings.images_dir)
    if not images:
        raise ConfigurationError(f"No images found in {settings.images_dir}.")
    count = int(getattr(args, "count", 20))
    if count < 1:
        raise ConfigurationError("--count must be at least 1")

    fixed_image = getattr(args, "image", None)
    results: list[InferenceResult] = []
    for index in range(count):
        image = Path(fixed_image) if fixed_image else images[index % len(images)]
        results.append(_run_one(settings, image, "TC1.2"))

    summary = reporting.summarise([result.as_row() for result in results])
    LOG.info(
        "TC1.2 batch: %d/%d succeeded (%.1f%%), avg=%s min=%s max=%s",
        summary.successes,
        summary.total,
        summary.success_rate,
        _fmt_ms(summary.average_latency_ms),
        _fmt_ms(summary.min_latency_ms),
        _fmt_ms(summary.max_latency_ms),
    )
    if summary.successes == 0:
        LOG.error("TC1.2 FAIL — no request produced a usable prediction")
        return 1
    LOG.info("TC1.2 PASS — all %d attempts were recorded", summary.total)
    return 0


def run_validate(args: argparse.Namespace) -> int:
    """TC1.3 — compare predictions with known expected labels."""

    settings = _build_settings(args)
    targets = _labelled_targets(settings, getattr(args, "image", None), getattr(args, "expected", None))

    passed = failed = 0
    for path, expected in targets:
        result = _run_one(settings, path, "TC1.3", expected_label=expected)
        if result.succeeded and result.prediction_correct:
            passed += 1
            LOG.info("PASS %s expected=%r predicted=%r", path.name, expected, result.predicted_label)
        else:
            failed += 1
            detail = (
                f"predicted={result.predicted_label!r} confidence="
                f"{'n/a' if result.confidence is None else round(result.confidence, 4)}"
                if result.succeeded
                else f"error={result.error_kind}: {result.error_message}"
            )
            LOG.error("FAIL %s expected=%r %s", path.name, expected, detail)

    total = passed + failed
    LOG.info(
        "TC1.3 accuracy: %d/%d correct (%.1f%%)",
        passed,
        total,
        (passed / total * 100.0) if total else 0.0,
    )
    return 0 if failed == 0 else 1


def run_timeout(args: argparse.Namespace) -> int:
    """TC1.4 — force a controlled failure, prove the client survives it."""

    base = load_settings(require_token=False)
    seconds = float(getattr(args, "seconds", 0.05))
    target = getattr(args, "failure_endpoint", None) or DEFAULT_FAILURE_ENDPOINT
    failure_settings = replace(
        base,
        endpoint_url=target,
        connect_timeout=seconds,
        read_timeout=seconds,
    )
    image = _resolve_image(base, getattr(args, "image", None))

    LOG.info(
        "TC1.4 step 1: forcing a failure against %s with a %.3f s timeout", target, seconds
    )
    failure = _run_one(failure_settings, image, "TC1.4", timeout=(seconds, seconds))
    if failure.status != "error" or failure.error_kind not in {"timeout", "connection", "http"}:
        LOG.error(
            "TC1.4 FAIL — expected a controlled timeout/connection failure, got status=%s kind=%s",
            failure.status,
            failure.error_kind,
        )
        return 1
    LOG.info(
        "TC1.4 PASS — failure captured as %s, logged, and the process is still running",
        failure.error_kind,
    )

    try:
        live_settings = _build_settings(args)
    except ConfigurationError:
        LOG.warning(
            "TC1.4 step 2 skipped — no API token configured, so no recovery request was sent"
        )
        return 0

    LOG.info("TC1.4 step 2: sending a normal request to prove the client recovered")
    recovery = _run_one(live_settings, image, "TC1.4")
    if recovery.succeeded:
        LOG.info("TC1.4 PASS — subsequent request succeeded after the failure")
        return 0
    LOG.error("TC1.4 recovery FAIL — %s: %s", recovery.error_kind, recovery.error_message)
    return 1


def run_all(args: argparse.Namespace) -> int:
    """Run TC1.1–TC1.4 in order and write the performance report."""

    settings = _build_settings(args)
    reset_log(settings.log_path)
    LOG.info("Cleared %s for a fresh test run", settings.log_path)

    exit_codes = [
        run_single(args),
        run_batch(args),
        run_validate(args),
        run_timeout(args),
    ]
    _write_report(args)
    return 0 if all(code == 0 for code in exit_codes) else 1


def run_report(args: argparse.Namespace) -> int:
    """Rebuild the Markdown report from the recorded CSV log."""

    summary = _write_report(args)
    return 0 if summary is not None else 1


def _labelled_targets(
    settings: Settings, image: str | None, expected: str | None
) -> list[tuple[Path, str]]:
    if image:
        path = Path(image)
        label = expected or _manifest_label(settings.images_dir, path.name)
        if not label:
            raise ConfigurationError(
                "No expected label for this image. Pass --expected or add the image "
                "to images/labels.json."
            )
        return [(path, label)]

    manifest_path = Path(settings.images_dir) / "labels.json"
    if not manifest_path.is_file():
        raise ConfigurationError(
            f"No labelled images found ({manifest_path} is missing). "
            "Add images with expected labels or pass --image and --expected."
        )

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ConfigurationError(f"Could not read {manifest_path}: {exc}") from exc

    targets: list[tuple[Path, str]] = []
    for name, entry in (manifest.get("images") or {}).items():
        path = Path(settings.images_dir) / name
        label = entry.get("expected_label") if isinstance(entry, dict) else None
        if label and path.is_file():
            targets.append((path, str(label)))
    if not targets:
        raise ConfigurationError(f"No labelled images with existing files in {manifest_path}")
    return targets


def _manifest_label(images_dir: Path, filename: str) -> str | None:
    manifest_path = Path(images_dir) / "labels.json"
    if not manifest_path.is_file():
        return None
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    entry = (manifest.get("images") or {}).get(filename)
    if isinstance(entry, dict) and entry.get("expected_label"):
        return str(entry["expected_label"])
    return None


def _write_report(args: argparse.Namespace) -> reporting.Summary | None:
    settings = load_settings(require_token=False)
    rows = read_results(settings.log_path)
    if not rows:
        LOG.warning("No records found in %s — run a test first", settings.log_path)
        return None

    summary = reporting.summarise(rows)
    destination = settings.log_path.parent / "performance_report.md"
    reporting.write_report(
        summary, rows=rows, source=settings.log_path, destination=destination
    )
    LOG.info("Performance report written to %s", destination)
    print(reporting.render_markdown(summary, rows=rows, source=settings.log_path))
    return summary


def _fmt_ms(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.1f} ms"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Phase 1 cloud AI communication prototype (single provider)."
    )
    parser.add_argument("--model", help="Override HF_MODEL_ID for this run")
    parser.add_argument("--endpoint", help="Override the full inference endpoint URL")

    sub = parser.add_subparsers(dest="command", required=True)

    single = sub.add_parser("single", help="TC1.1 — send one image")
    single.add_argument("--image", help="Image path (default: first file in images/)")

    batch = sub.add_parser("batch", help="TC1.2 — sequential request batch")
    batch.add_argument("--image", help="Reuse one image for every request")
    batch.add_argument("--count", type=int, default=20, help="Number of requests (default 20)")

    validate = sub.add_parser("validate", help="TC1.3 — labelled prediction check")
    validate.add_argument("--image", help="Labelled image to check")
    validate.add_argument("--expected", help="Expected label (defaults to images/labels.json)")

    timeout = sub.add_parser("timeout", help="TC1.4 — controlled network failure")
    timeout.add_argument("--image", help="Image path (default: first file in images/)")
    timeout.add_argument(
        "--seconds", type=float, default=0.05, help="Timeout in seconds (default 0.05)"
    )
    timeout.add_argument(
        "--failure-endpoint",
        help=f"Endpoint to fail against (default {DEFAULT_FAILURE_ENDPOINT})",
    )

    all_tests = sub.add_parser("all", help="Run TC1.1–TC1.4 and write the report")
    all_tests.add_argument("--count", type=int, default=20, help="TC1.2 request count")
    all_tests.add_argument("--seconds", type=float, default=0.05, help="TC1.4 timeout seconds")

    sub.add_parser("report", help="Rebuild the report from logs/results.csv")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")

    handlers = {
        "single": run_single,
        "batch": run_batch,
        "validate": run_validate,
        "timeout": run_timeout,
        "all": run_all,
        "report": run_report,
    }

    try:
        return handlers[args.command](args)
    except ConfigurationError as exc:
        LOG.error("Configuration error: %s", exc)
        return 2
    except KeyboardInterrupt:
        LOG.error("Interrupted")
        return 130


if __name__ == "__main__":
    sys.exit(main())
