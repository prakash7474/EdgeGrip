"""TC1.1 — a single valid image is sent, predicted, timed and logged."""

from __future__ import annotations

import os

import pytest

from cloud.inference import HuggingFaceProvider, send_image_to_cloud
from config import load_settings
from image_loader import load_image
from request_log import read_results


def test_single_request_returns_prediction(settings, make_provider):
    result = send_image_to_cloud(
        settings.images_dir / "test_1.jpg", settings=settings, provider=make_provider()
    )

    assert result.status == "success"
    assert result.predicted_label == "tabby, tabby cat"
    assert result.confidence == pytest.approx(0.91)
    assert result.latency_ms >= 0.0
    assert result.task_id.startswith("task-")
    assert result.request_time_utc.endswith("Z")
    assert result.response_time_utc.endswith("Z")


def test_single_request_is_logged_without_secrets(settings, make_provider):
    result = send_image_to_cloud(
        settings.images_dir / "test_1.jpg", settings=settings, provider=make_provider()
    )
    rows = read_results(settings.log_path)

    assert len(rows) == 1
    assert rows[0]["task_id"] == result.task_id
    assert rows[0]["status"] == "success"
    assert rows[0]["image_filename"] == "test_1.jpg"
    assert rows[0]["predicted_label"] == "tabby, tabby cat"
    assert "test-token-not-real" not in settings.log_path.read_text(encoding="utf-8")


def test_each_request_gets_a_unique_task_id(settings, make_provider):
    task_ids = {
        send_image_to_cloud(
            image, settings=settings, provider=make_provider()
        ).task_id
        for image in sorted(settings.images_dir.glob("test_*.jpg"))
    }
    assert len(task_ids) == 3


def test_huggingface_provider_sends_documented_request(settings):
    image = load_image(settings.images_dir / "test_3.jpg")

    class StubResponse:
        status_code = 200
        text = ""

        def json(self):
            return [{"label": "banana", "score": 0.99}]

    class StubSession:
        def __init__(self):
            self.request = None

        def post(self, url, *, headers, data, timeout):
            self.request = {"url": url, "headers": headers, "data": data, "timeout": timeout}
            return StubResponse()

    session = StubSession()
    provider = HuggingFaceProvider.from_settings(settings, session=session)
    payload = provider.classify(image, timeout=(5.0, 30.0))

    assert payload == [{"label": "banana", "score": 0.99}]
    assert session.request is not None
    assert session.request["url"].endswith("/hf-inference/models/google/vit-base-patch16-224")
    assert session.request["headers"]["Authorization"] == "Bearer test-token-not-real"
    assert session.request["headers"]["Content-Type"] == "image/jpeg"
    assert session.request["data"].startswith(b"\xff\xd8")  # JPEG start-of-image marker
    assert session.request["timeout"] == (5.0, 30.0)


def test_missing_image_is_recorded_not_raised(settings, make_provider):
    result = send_image_to_cloud(
        settings.images_dir / "does_not_exist.jpg",
        settings=settings,
        provider=make_provider(),
    )

    assert result.status == "error"
    assert result.error_kind == "image"
    assert read_results(settings.log_path)[0]["status"] == "error"


@pytest.mark.live
def test_live_single_request():
    """Real API check. Opt in with RUN_LIVE_TESTS=1 and a valid token."""

    if os.getenv("RUN_LIVE_TESTS") != "1":
        pytest.skip("set RUN_LIVE_TESTS=1 and HF_API_TOKEN to run live API tests")

    settings = load_settings()
    result = send_image_to_cloud(
        settings.images_dir / "test_1.jpg", settings=settings, test_case="TC1.1"
    )
    assert result.succeeded, f"{result.error_kind}: {result.error_message}"
    assert result.predicted_label
