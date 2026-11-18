"""TC1.1 — one valid image reaches the cloud endpoint and returns a prediction.

Pass condition: a usable prediction field, measured latency clearly under the
5 s budget, and a real HTTP round trip (no stubbed client).
"""

from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np
import pytest

from phase1_pipeline import capture as capture_module
from phase1_pipeline.capture import CaptureError, FrameGrabber
from phase1_pipeline.client import CloudClient

LATENCY_BUDGET_MS = 5000.0


def test_tc1_1_single_valid_image_returns_prediction(client: CloudClient, grabber: FrameGrabber) -> None:
    frame = grabber.next_frame()
    result = client.predict(frame.jpeg_bytes, image_name=frame.name)

    assert result.ok, f"request failed: {result.error_kind}: {result.error_message}"
    assert result.prediction is not None
    assert result.label
    assert result.status_code == 200
    assert result.image_name == frame.name
    assert result.latency_ms > 0.0
    assert result.latency_ms < LATENCY_BUDGET_MS, (
        f"round trip {result.latency_ms:.1f} ms exceeded the {LATENCY_BUDGET_MS:.0f} ms budget"
    )


def test_tc1_1_capture_falls_back_when_no_camera_is_available(monkeypatch: pytest.MonkeyPatch) -> None:
    class MissingCamera:
        def isOpened(self) -> bool:
            return False

        def read(self) -> tuple[bool, None]:
            return False, None

        def release(self) -> None:
            return None

    monkeypatch.setattr(capture_module.cv2, "VideoCapture", lambda *args, **kwargs: MissingCamera())

    frame = FrameGrabber(Path("images"), camera_enabled=True).next_frame()

    assert frame.source == "sample"
    assert frame.jpeg_bytes
    assert frame.width > 0 and frame.height > 0


def test_tc1_1_missing_samples_raise_a_clear_error(tmp_path) -> None:
    grabber = FrameGrabber(tmp_path, camera_enabled=False)
    with pytest.raises(CaptureError, match="No camera and no sample images"):
        grabber.next_frame()


def test_tc1_1_sample_frame_is_a_decodable_jpeg(grabber: FrameGrabber) -> None:
    frame = grabber.next_frame()
    decoded = cv2.imdecode(np.frombuffer(frame.jpeg_bytes, dtype=np.uint8), cv2.IMREAD_COLOR)
    assert decoded is not None
    assert decoded.shape[1] == frame.width
    assert decoded.shape[0] == frame.height
