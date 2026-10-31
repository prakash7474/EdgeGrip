"""TC1.2 — 20 sequential requests, with every attempt recorded."""

from __future__ import annotations

import requests

from cloud.inference import send_image_to_cloud
from reporting import summarise
from request_log import read_results


def _send_many(settings, provider, count: int):
    images = sorted(settings.images_dir.glob("test_*.jpg"))
    return [
        send_image_to_cloud(
            images[index % len(images)], settings=settings, provider=provider
        )
        for index in range(count)
    ]


def test_twenty_sequential_requests_are_all_logged(settings, make_provider):
    results = _send_many(settings, make_provider(), 20)
    rows = read_results(settings.log_path)
    summary = summarise(rows)

    assert len(results) == 20
    assert len(rows) == 20
    assert summary.total == 20
    assert summary.successes == 20
    assert summary.failures == 0
    assert summary.success_rate == 100.0
    assert summary.average_latency_ms is not None
    assert summary.min_latency_ms <= summary.average_latency_ms <= summary.max_latency_ms
    assert len({row["task_id"] for row in rows}) == 20


def test_failures_are_counted_not_discarded(settings, make_provider):
    good = make_provider()
    failing = make_provider(error=requests.exceptions.ConnectionError("simulated outage"))
    images = sorted(settings.images_dir.glob("test_*.jpg"))
    failing_indices = {3, 7, 11}

    for index in range(20):
        provider = failing if index in failing_indices else good
        send_image_to_cloud(
            images[index % len(images)], settings=settings, provider=provider
        )

    rows = read_results(settings.log_path)
    summary = summarise(rows)

    assert summary.total == 20
    assert summary.successes == 17
    assert summary.failures == 3
    assert summary.success_rate == 85.0
    assert sum(1 for row in rows if row["error_kind"] == "connection") == 3


def test_latency_statistics_are_computed_from_successful_requests(settings, make_provider):
    slow = make_provider(delay=0.01)
    _send_many(settings, slow, 4)
    rows = read_results(settings.log_path)
    summary = summarise(rows)

    assert summary.min_latency_ms is not None
    assert summary.max_latency_ms is not None
    assert summary.min_latency_ms >= 0.0
    assert summary.max_latency_ms >= summary.min_latency_ms
