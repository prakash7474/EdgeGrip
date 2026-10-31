"""Shared pytest fixtures for the Phase 1 prototype."""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import PROJECT_ROOT, Settings
from image_loader import LoadedImage


class FakeProvider:
    """Deterministic stand-in for a cloud provider adapter.

    Tests never touch the network unless they opt in to a live test, so the
    pipeline, logging and reporting logic can be verified offline.
    """

    name = "fake"

    def __init__(self, payload=None, error: Exception | None = None, delay: float = 0.0):
        self.payload = (
            payload if payload is not None else [{"label": "tabby, tabby cat", "score": 0.91}]
        )
        self.error = error
        self.delay = delay
        self.calls: list[dict[str, object]] = []

    def classify(self, image: LoadedImage, *, timeout: tuple[float, float]):
        self.calls.append({"filename": image.filename, "timeout": timeout})
        if self.delay:
            time.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return self.payload


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        api_token="test-token-not-real",
        model_id="google/vit-base-patch16-224",
        endpoint_url=(
            "https://router.huggingface.co/hf-inference/models/google/vit-base-patch16-224"
        ),
        connect_timeout=5.0,
        read_timeout=30.0,
        images_dir=PROJECT_ROOT / "images",
        log_path=tmp_path / "results.csv",
        latency_target_ms=5000.0,
    )


@pytest.fixture
def make_provider():
    """Return the FakeProvider class so tests can construct configured fakes."""

    return FakeProvider
