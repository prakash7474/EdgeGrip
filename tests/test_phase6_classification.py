"""Phase 6: dataset split integrity and classification serving.

TC6.1  session-based split, manifest, no file in two splits, minimum counts
TC6.2  webcam capture (manual, skipped: needs a camera and physical parts)
TC6.3  classification HTTP contract with a tiny fake classifier (offline)
TC6.4  low top score becomes "unknown" and the policy holds

TC6.3/TC6.4 inject a fake pipeline into ``ClassificationModel``, so no torch,
transformers download, GPU or real dataset is needed.
"""

from __future__ import annotations

import contextlib
import csv
import json
import socket
import threading
import time
import urllib.error
import urllib.request
from dataclasses import replace
from pathlib import Path

import pytest
import uvicorn

from actuator.policy import Policy
from real_server.app import (
    ClassificationModel,
    configured_task,
    create_app,
)
from tools.capture_dataset import capture
from tools.split_dataset import (
    SplitError,
    build_manifest,
    discover_items,
    validate_manifest,
    write_manifest,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
IMAGES_DIR = PROJECT_ROOT / "images"
POLICY_PATH = PROJECT_ROOT / "config" / "policy.classification.example.json"
TOKEN = "phase6-test-token-not-a-real-secret"
SAMPLE_JPEG = (IMAGES_DIR / "cat.jpg").read_bytes()


# --------------------------------------------------------------------- helpers
class FixedClassifier:
    """Tiny fake transformers image-classification pipeline."""

    def __init__(self, label: str, score: float) -> None:
        self.label = label
        self.score = score
        self.calls = 0

    def __call__(self, image):
        self.calls += 1
        return [{"label": self.label, "score": self.score}]


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_ready(host: str, port: int, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.25):
                return
        except OSError:
            time.sleep(0.05)
    raise RuntimeError("test server did not start")


@contextlib.contextmanager
def _serve(model):
    host, port = "127.0.0.1", _free_port()
    server = uvicorn.Server(
        uvicorn.Config(
            create_app(model), host=host, port=port,
            log_level="warning", access_log=False,
        )
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        _wait_ready(host, port)
        yield f"http://{host}:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def _post(url: str, headers: dict[str, str]) -> tuple[int, dict, dict]:
    request = urllib.request.Request(
        url, data=SAMPLE_JPEG, headers={"Content-Type": "image/jpeg", **headers},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read().decode("utf-8")), dict(response.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8")), dict(exc.headers)


def _make_dataset(root: Path, plan: dict[str, dict[str, int]]) -> None:
    """Create class/session images by copying one real JPEG."""
    for label, sessions in plan.items():
        class_dir = root / label
        class_dir.mkdir(parents=True)
        for session, count in sessions.items():
            for index in range(1, count + 1):
                (class_dir / f"{session}_{index}.jpg").write_bytes(SAMPLE_JPEG)


# --------------------------------------------------------------------- TC6.1
def test_tc6_1_split_is_by_session_and_manifest_is_complete(tmp_path):
    data = tmp_path / "data"
    plan = {
        "bolt": {f"s{index:02d}": 2 for index in range(1, 6)},
        "nut": {f"s{index:02d}": 2 for index in range(1, 6)},
    }
    _make_dataset(data, plan)

    items = discover_items(data)
    rows = build_manifest(items, seed=7, min_per_class=3)
    expected = sum(
        count for sessions in plan.values() for count in sessions.values()
    )
    assert len(rows) == expected

    # Every discovered file appears exactly once and nowhere else.
    assert len({row.path for row in rows}) == len(rows)
    assert {row.path for row in rows} == {item.path.as_posix() for item in items}

    # No session leaks across splits, and each class is represented in all three.
    session_splits: dict[tuple[str, str], set[str]] = {}
    for row in rows:
        session_splits.setdefault((row.label, row.session), set()).add(row.split)
    assert all(len(splits) == 1 for splits in session_splits.values())
    for label in plan:
        assert {row.split for row in rows if row.label == label} == {"train", "val", "test"}

    # Same seed == same manifest; the split is reproducible.
    assert rows == build_manifest(items, seed=7, min_per_class=3)

    manifest = write_manifest(rows, data / "manifest.csv")
    with manifest.open(newline="", encoding="utf-8") as handle:
        written = list(csv.DictReader(handle))
    assert len(written) == len(rows)
    assert set(written[0]) == {"path", "label", "session", "split"}


def test_tc6_1_rejects_duplicate_files_and_short_classes(tmp_path):
    data = tmp_path / "data"
    _make_dataset(data, {"bolt": {f"s{index:02d}": 2 for index in range(1, 5)}})
    rows = build_manifest(discover_items(data), seed=1, min_per_class=3)

    # The same file in two splits is a hard failure.
    with pytest.raises(SplitError, match="more than one split"):
        validate_manifest([rows[0], replace(rows[0], split="test")],
                          min_per_class=1, min_per_split=1)

    # The same file twice in one split is also refused.
    with pytest.raises(SplitError, match="more than once"):
        validate_manifest([rows[0], rows[0]], min_per_class=1, min_per_split=1)

    # A class with too few images fails before anything is written.
    with pytest.raises(SplitError, match="minimum is"):
        build_manifest(discover_items(data), min_per_class=len(rows) + 1)


# --------------------------------------------------------------------- TC6.2
@pytest.mark.skip(
    reason="needs a webcam and a person placing parts; run "
           "python tools/capture_dataset.py <class> <session>"
)
def test_tc6_2_webcam_capture_is_a_manual_step():
    """The capture tool is interactive and cannot run unattended."""
    assert callable(capture)


# --------------------------------------------------------------------- TC6.3
def test_tc6_3_classification_contract_with_tiny_fake_classifier(monkeypatch):
    monkeypatch.setenv("API_TOKEN", TOKEN)
    pipe = FixedClassifier("bolt", 0.93)
    model = ClassificationModel(
        pipe=pipe, model_id="tiny-fake", device="cpu", threshold=0.5
    )
    with _serve(model) as base_url:
        code, body, headers = _post(
            f"{base_url}/predict",
            {"X-API-Token": TOKEN, "X-Task-Id": "task-6-3"},
        )

    assert code == 200
    assert body["prediction"] == "bolt"
    assert body["label"] == "bolt"
    assert body["score"] == pytest.approx(0.93)
    assert body["confidence"] == pytest.approx(0.93)
    assert body["task"] == "classification"
    assert body["model"] == "tiny-fake"
    assert isinstance(body["server_inference_ms"], (int, float))
    assert body["server_inference_ms"] >= 0.0
    assert body["task_id"] == "task-6-3"
    assert headers.get("x-task-id") == "task-6-3"
    assert pipe.calls == 1


def test_tc6_3_task_defaults_to_detection_and_rejects_unknown_values(monkeypatch):
    monkeypatch.delenv("TASK", raising=False)
    assert configured_task() == "detection"

    monkeypatch.setenv("TASK", "classification")
    assert configured_task() == "classification"

    monkeypatch.setenv("TASK", "segmentation")
    with pytest.raises(ValueError, match="TASK must be one of"):
        configured_task()


# --------------------------------------------------------------------- TC6.4
def test_tc6_4_low_score_becomes_unknown_and_policy_holds():
    model = ClassificationModel(
        pipe=FixedClassifier("bolt", 0.21), model_id="tiny-fake", device="cpu",
        threshold=0.5,
    )
    response, _ = model.predict(SAMPLE_JPEG)
    assert response["prediction"] == "unknown"
    assert response["label"] == "unknown"
    assert response["score"] == pytest.approx(0.21)
    assert response["above_threshold"] is False
    assert response["threshold"] == 0.5

    policy = Policy.from_file(POLICY_PATH)
    assert policy.default_action == "hold"
    outcome = policy.decide(response["prediction"], response["score"])
    assert outcome.action == "hold"
    assert outcome.matched == "default"

    # A score above both the server threshold and the policy's 0.6 rule grips.
    accepted = ClassificationModel(
        pipe=FixedClassifier("bolt", 0.7), model_id="tiny-fake", device="cpu",
        threshold=0.5,
    ).predict(SAMPLE_JPEG)[0]
    assert accepted["prediction"] == "bolt"
    assert policy.decide(accepted["prediction"], accepted["score"]).action == "grip"

    # At exactly the server threshold the label is kept, but the policy's
    # stricter 0.6 minimum still holds. Both gates fail safe.
    boundary = ClassificationModel(
        pipe=FixedClassifier("bolt", 0.5), model_id="tiny-fake", device="cpu",
        threshold=0.5,
    ).predict(SAMPLE_JPEG)[0]
    assert boundary["prediction"] == "bolt"
    assert policy.decide(boundary["prediction"], boundary["score"]).action == "hold"


def test_tc6_4_http_response_is_unknown_below_threshold(monkeypatch):
    monkeypatch.setenv("API_TOKEN", TOKEN)
    model = ClassificationModel(
        pipe=FixedClassifier("washer", 0.34), model_id="tiny-fake", device="cpu",
        threshold=0.6,
    )
    with _serve(model) as base_url:
        code, body, headers = _post(
            f"{base_url}/predict",
            {"X-API-Token": TOKEN, "X-Task-Id": "task-6-4"},
        )
    assert code == 200
    assert body["prediction"] == "unknown"
    assert body["score"] == pytest.approx(0.34)
    assert body["task_id"] == "task-6-4"
    assert headers.get("x-task-id") == "task-6-4"

    policy = Policy.from_file(POLICY_PATH)
    assert policy.decide(body["prediction"], body["score"]).action == "hold"
    # Even a very confident "unknown" has no rule and still holds.
    assert policy.decide("unknown", 0.99).action == "hold"
    # The example's new labels are wired: washer releases, bolt grips.
    assert policy.decide("washer", 0.9).action == "release"
    assert policy.decide("bolt", 0.9).action == "grip"
