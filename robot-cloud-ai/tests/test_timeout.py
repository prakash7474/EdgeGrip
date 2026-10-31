"""TC1.4 — network failures are captured, logged, and survivable.

No test disables the machine's networking. Failures are injected with a stub
provider or by targeting a closed local port.
"""

from __future__ import annotations

from dataclasses import replace

import requests

from cloud.inference import send_image_to_cloud
from request_log import read_results


def test_timeout_is_captured_and_logged(settings, make_provider):
    provider = make_provider(error=requests.exceptions.Timeout("simulated read timeout"))
    result = send_image_to_cloud(
        settings.images_dir / "test_1.jpg", settings=settings, provider=provider
    )

    assert result.status == "error"
    assert result.error_kind == "timeout"
    assert "timed out" in result.error_message.lower()
    assert result.predicted_label == ""
    rows = read_results(settings.log_path)
    assert len(rows) == 1
    assert rows[0]["error_kind"] == "timeout"
    assert rows[0]["status"] == "error"


def test_short_timeout_against_closed_local_port(settings):
    failure_settings = replace(
        settings,
        endpoint_url="http://127.0.0.1:9",
        connect_timeout=0.05,
        read_timeout=0.05,
    )
    result = send_image_to_cloud(
        settings.images_dir / "test_1.jpg",
        settings=failure_settings,
        timeout=(0.05, 0.05),
    )

    assert result.status == "error"
    assert result.error_kind in {"connection", "timeout"}
    assert result.http_status is None


def test_client_continues_after_a_failure(settings, make_provider):
    failing = make_provider(error=requests.exceptions.Timeout("simulated timeout"))
    first = send_image_to_cloud(
        settings.images_dir / "test_1.jpg", settings=settings, provider=failing
    )
    second = send_image_to_cloud(
        settings.images_dir / "test_2.jpg", settings=settings, provider=make_provider()
    )

    assert first.status == "error"
    assert second.status == "success"
    assert [row["status"] for row in read_results(settings.log_path)] == ["error", "success"]


def test_http_error_status_is_reported(settings):
    image = (settings.images_dir / "test_1.jpg").read_bytes()

    class ErrorResponse:
        status_code = 503
        text = "Model is currently loading"

        def json(self):
            return {}

    class ErrorSession:
        def post(self, url, *, headers, data, timeout):
            return ErrorResponse()

    from cloud.inference import HuggingFaceProvider
    from image_loader import load_image

    provider = HuggingFaceProvider.from_settings(settings, session=ErrorSession())
    result = send_image_to_cloud(
        settings.images_dir / "test_1.jpg", settings=settings, provider=provider
    )

    assert image  # fixture really exists
    assert result.status == "error"
    assert result.error_kind == "unavailable"
    assert result.http_status == 503
    assert load_image(settings.images_dir / "test_1.jpg").width > 0
