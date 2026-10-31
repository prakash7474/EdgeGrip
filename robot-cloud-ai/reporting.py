"""Latency, success-rate and label-accuracy reporting.

The two quality dimensions are deliberately kept apart:

* request success rate — did the HTTP call return a usable prediction?
* prediction accuracy — did that prediction match a known expected label?
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from label_matching import labels_match  # re-exported for callers and tests

CASE_ORDER = {"TC1.1": 0, "TC1.2": 1, "TC1.3": 2, "TC1.4": 3}


@dataclass(frozen=True)
class Summary:
    total: int
    successes: int
    failures: int
    success_rate: float
    average_latency_ms: float | None
    min_latency_ms: float | None
    max_latency_ms: float | None
    labelled: int
    correct: int
    accuracy: float | None
    per_case: dict[str, dict[str, Any]]


def summarise(rows: Sequence[Mapping[str, str]]) -> Summary:
    """Aggregate CSV rows into the Phase 1 performance metrics."""

    total = len(rows)
    success_rows = [row for row in rows if row.get("status", "").strip().lower() == "success"]
    failures = total - len(success_rows)

    latencies = [
        value
        for value in (_as_float(row.get("latency_ms")) for row in success_rows)
        if value is not None
    ]

    labelled_rows = [row for row in rows if str(row.get("prediction_correct", "")).strip()]
    correct = sum(1 for row in labelled_rows if _as_bool(row.get("prediction_correct", "")))

    per_case: dict[str, dict[str, Any]] = {}
    for row in rows:
        case = (row.get("test_case") or "").strip() or "(unlabelled)"
        stats = per_case.setdefault(
            case,
            {"attempts": 0, "successes": 0, "failures": 0, "_latencies": []},
        )
        stats["attempts"] += 1
        if row.get("status", "").strip().lower() == "success":
            stats["successes"] += 1
            latency = _as_float(row.get("latency_ms"))
            if latency is not None:
                stats["_latencies"].append(latency)
        else:
            stats["failures"] += 1

    for stats in per_case.values():
        case_latencies: list[float] = stats.pop("_latencies")
        stats["average_latency_ms"] = (
            round(sum(case_latencies) / len(case_latencies), 2) if case_latencies else None
        )

    return Summary(
        total=total,
        successes=len(success_rows),
        failures=failures,
        success_rate=(len(success_rows) / total * 100.0) if total else 0.0,
        average_latency_ms=round(sum(latencies) / len(latencies), 2) if latencies else None,
        min_latency_ms=round(min(latencies), 2) if latencies else None,
        max_latency_ms=round(max(latencies), 2) if latencies else None,
        labelled=len(labelled_rows),
        correct=correct,
        accuracy=(correct / len(labelled_rows) * 100.0) if labelled_rows else None,
        per_case=per_case,
    )


def render_markdown(
    summary: Summary,
    *,
    rows: Sequence[Mapping[str, str]],
    source: str | Path,
    generated_at: str | None = None,
) -> str:
    """Render the Phase 1 performance report as Markdown."""

    stamp = generated_at or datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )

    lines = [
        "# Phase 1 — Cloud AI prototype performance report",
        "",
        f"- Generated: {stamp}",
        f"- Log source: `{source}`",
        f"- Records in log: {len(rows)}",
        "",
        "## Headline metrics",
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| Total requests | {summary.total} |",
        f"| Successful requests | {summary.successes} |",
        f"| Failed requests | {summary.failures} |",
        f"| Success rate | {_pct(summary.success_rate)} |",
        f"| Average latency | {_ms(summary.average_latency_ms)} |",
        f"| Minimum latency | {_ms(summary.min_latency_ms)} |",
        f"| Maximum latency | {_ms(summary.max_latency_ms)} |",
        f"| Prediction accuracy | {_accuracy(summary)} |",
        "",
    ]

    if summary.per_case:
        lines += [
            "## Results by test case",
            "",
            "| Test case | Attempts | Successful | Failed | Average latency |",
            "|---|---:|---:|---:|---:|",
        ]
        for case in sorted(summary.per_case, key=_case_sort_key):
            stats = summary.per_case[case]
            lines.append(
                f"| {case} | {stats['attempts']} | {stats['successes']} | "
                f"{stats['failures']} | {_ms(stats['average_latency_ms'])} |"
            )
        lines.append("")

    lines += [
        "## How to read this report",
        "",
        "- Latency is the complete client-side round trip measured with "
        "`time.perf_counter()` around the HTTP call. It includes TLS setup, "
        "network transit, provider queueing and model execution — not model "
        "inference time alone.",
        "- Latency statistics use successful requests only.",
        "- Success rate measures whether a usable prediction came back. It is not "
        "prediction quality.",
        "- Accuracy is computed only from records that carry an expected label. "
        "Failed requests are excluded from the accuracy denominator but still "
        "count as attempts.",
        "- The 5,000 ms response target is a prototype benchmark, not a promised "
        "service level from the inference provider.",
        "- Phase 1 neither retries nor buffers requests. Failures are recorded, "
        "not recovered; that is Phase 2 scope.",
        "",
    ]
    return "\n".join(lines)


def write_report(
    summary: Summary,
    *,
    rows: Sequence[Mapping[str, str]],
    source: str | Path,
    destination: str | Path,
) -> Path:
    """Write the Markdown report to ``destination`` and return its path."""

    path = Path(destination)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_markdown(summary, rows=rows, source=source), encoding="utf-8")
    return path


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _as_bool(value: Any) -> bool:
    return str(value).strip().lower() in {"true", "1", "yes"}


def _case_sort_key(name: str) -> tuple[int, str]:
    return (CASE_ORDER.get(name, 99), name)


def _ms(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.1f} ms"


def _pct(value: float) -> str:
    return f"{value:.1f}%"


def _accuracy(summary: Summary) -> str:
    if summary.accuracy is None:
        return "not measured (no labelled records)"
    return f"{_pct(summary.accuracy)} ({summary.correct}/{summary.labelled} correct)"
