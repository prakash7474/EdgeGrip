"""Real model tests - skipped unless RUN_REAL_MODEL=1 is set.

These tests require the real inference server running with a downloaded model
(model download is large). Run the real server on a separate port and point
INFERENCE_ENDPOINT to it, or use the fixture pattern below.
"""

from __future__ import annotations

import os
import time

import numpy as np
import pytest
import requests

from phase1_pipeline.capture import FrameGrabber
from phase1_pipeline.client import CloudClient

pytestmark = pytest.mark.real_model


def _real_enabled() -> bool:
    return os.environ.get("RUN_REAL_MODEL") == "1"


def _get_endpoint() -> str:
    return os.environ.get("REAL_ENDPOINT") or os.environ.get("INFERENCE_ENDPOINT") or "http://127.0.0.1:8001/predict"


@pytest.mark.skipif(not _real_enabled(), reason="Set RUN_REAL_MODEL=1 to run real model tests (large download)")
def test_real_health_reports_device_and_model():
    base = _get_endpoint().replace("/predict", "/health")
    resp = requests.get(base, timeout=30)
    assert resp.status_code == 200
    data = resp.json()
    # /health intentionally exposes only model and device.
    assert set(data) == {"model", "device"}


@pytest.mark.skipif(not _real_enabled(), reason="Set RUN_REAL_MODEL=1 to run real model tests (large download)")
def test_real_sample_image_returns_valid_response():
    endpoint = _get_endpoint()
    grabber = FrameGrabber("images", camera_enabled=False)
    frame = grabber.next_frame()
    client = CloudClient(endpoint, timeout_seconds=120)
    try:
        result = client.predict(frame.jpeg_bytes, image_name=frame.name)
        assert result.ok, f"{result.error_kind}: {result.error_message}"
        assert result.prediction is not None
        assert result.label is not None
    finally:
        client.close()


@pytest.mark.skipif(not _real_enabled(), reason="Set RUN_REAL_MODEL=1 to run real model tests (large download)")
def test_real_blank_image_returns_prediction_none():
    endpoint = _get_endpoint()
    # create blank image
    import cv2

    img = np.zeros((640, 480, 3), dtype=np.uint8)
    ok, buf = cv2.imencode(".jpg", img)
    assert ok
    client = CloudClient(endpoint, timeout_seconds=120)
    try:
        result = client.predict(buf.tobytes(), image_name="blank.jpg")
        assert result.ok
        assert result.label == "none" or result.prediction.label == "none"
    finally:
        client.close()


@pytest.mark.skipif(not _real_enabled(), reason="Set RUN_REAL_MODEL=1 to run real model tests (large download)")
def test_real_non_image_upload_returns_4xx():
    endpoint = _get_endpoint()
    client = CloudClient(endpoint, timeout_seconds=30)
    try:
        result = client.predict(b"this is not an image", image_name="bad.txt")
        assert not result.ok
        assert result.status_code is not None and result.status_code >= 400
    finally:
        client.close()
