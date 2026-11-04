"""TC1.3 — a known labelled image returns the expected label.

The expected labels come from ``images/labels.json``, the same manifest the
mock server indexes. An unknown image must still get a stable answer.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from phase1_pipeline.capture import FrameGrabber, encode_jpeg
from phase1_pipeline.client import CloudClient

IMAGES_DIR = Path(__file__).resolve().parent.parent / "images"


def _expected_labels() -> dict[str, str]:
    manifest = json.loads((IMAGES_DIR / "labels.json").read_text(encoding="utf-8"))
    return {name: entry["expected_label"] for name, entry in manifest["images"].items()}


def test_tc1_3_known_image_returns_expected_label(client: CloudClient) -> None:
    labels = _expected_labels()
    assert labels, "labels.json declared no images"

    grabber = FrameGrabber(IMAGES_DIR, camera_enabled=False)
    for name, expected in labels.items():
        frame = grabber.frame_for(name)
        result = client.predict(frame.jpeg_bytes, image_name=name)

        assert result.ok, f"{name}: {result.error_kind}: {result.error_message}"
        assert result.label == expected, f"{name}: expected {expected!r}, got {result.label!r}"
        assert result.prediction is not None
        assert result.prediction.confidence is not None
        assert result.prediction.extra.get("known_image") is True


def test_tc1_3_repeated_request_returns_the_same_label(client: CloudClient) -> None:
    grabber = FrameGrabber(IMAGES_DIR, camera_enabled=False)
    frame = grabber.frame_for("cat.jpg")

    first = client.predict(frame.jpeg_bytes, image_name=frame.name)
    second = client.predict(frame.jpeg_bytes, image_name=frame.name)

    assert first.ok and second.ok
    assert first.label == second.label
    assert first.prediction.confidence == second.prediction.confidence


def test_tc1_3_unknown_image_still_gets_a_stable_label(client: CloudClient) -> None:
    rng = np.random.default_rng(seed=1234)
    noise = rng.integers(0, 256, size=(64, 64, 3), dtype=np.uint8)
    payload = encode_jpeg(noise)

    first = client.predict(payload, image_name="synthetic-noise")
    second = client.predict(payload, image_name="synthetic-noise")

    assert first.ok and second.ok
    assert first.label == second.label
    assert first.prediction.extra.get("known_image") is False
