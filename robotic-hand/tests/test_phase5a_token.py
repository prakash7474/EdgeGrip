"""Phase 5a: API token enforcement on real_server and client header support."""

from __future__ import annotations

import json
import socket
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest
import uvicorn

from phase1_pipeline.capture import FrameGrabber
from phase1_pipeline.client import CloudClient
from phase3_router.providers.http_provider import HTTPProvider
from real_server.app import create_app

TOKEN = "phase5a-test-token-not-a-real-secret"
IMAGES_DIR = Path(__file__).resolve().parent.parent / "images"


class _FakeModel:
    model_id = "fake-rtdetr"
    device = "cpu"

    def predict(self, image_bytes: bytes):
        return {
            "label": "banana",
            "prediction": "banana",
            "model": self.model_id,
            "device": self.device,
            "server_inference_ms": 1.0,
        }, 1.0


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


@pytest.fixture()
def token_server(monkeypatch):
    monkeypatch.setenv("API_TOKEN", TOKEN)
    host, port = "127.0.0.1", _free_port()
    server = uvicorn.Server(uvicorn.Config(
        create_app(_FakeModel()), host=host, port=port,
        log_level="warning", access_log=False,
    ))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        _wait_ready(host, port)
        yield f"http://{host}:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def _post(url: str, data: bytes, headers: dict[str, str]) -> tuple[int, dict]:
    request = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def _sample_jpeg() -> bytes:
    return FrameGrabber(IMAGES_DIR, camera_enabled=False).next_frame().jpeg_bytes


def test_tc5a_health_reports_only_model_and_device(token_server: str) -> None:
    with urllib.request.urlopen(f"{token_server}/health", timeout=10) as response:
        body = json.loads(response.read().decode("utf-8"))
    assert set(body) == {"model", "device"}
    assert body["model"] == "fake-rtdetr"
    assert body["device"] == "cpu"


def test_tc5a_predict_rejects_missing_and_wrong_token(token_server: str) -> None:
    endpoint = f"{token_server}/predict"
    headers = {"Content-Type": "image/jpeg"}
    missing_code, _ = _post(endpoint, _sample_jpeg(), headers)
    wrong_code, _ = _post(endpoint, _sample_jpeg(), {**headers, "X-API-Token": "wrong"})
    assert missing_code == 401
    assert wrong_code == 401


def test_tc5a_predict_accepts_matching_token_and_echoes_task_id(token_server: str) -> None:
    code, body = _post(
        f"{token_server}/predict",
        _sample_jpeg(),
        {"Content-Type": "image/jpeg", "X-API-Token": TOKEN, "X-Task-Id": "task-123"},
    )
    assert code == 200
    assert body["label"] == "banana"
    assert body["task_id"] == "task-123"


def test_tc5a_server_fails_closed_without_configured_token(
    token_server: str, monkeypatch
) -> None:
    monkeypatch.delenv("API_TOKEN")
    code, _ = _post(
        f"{token_server}/predict",
        _sample_jpeg(),
        {"Content-Type": "image/jpeg", "X-API-Token": TOKEN},
    )
    assert code == 503


def test_tc5a_client_sends_api_token(token_server: str) -> None:
    endpoint = f"{token_server}/predict"
    with CloudClient(endpoint, api_token=TOKEN) as client:
        assert TOKEN not in repr(client)
        result = client.predict(_sample_jpeg(), image_name="banana.jpg")
    assert result.ok, result.error_message
    assert result.label == "banana"

    with CloudClient(endpoint) as client:
        result = client.predict(_sample_jpeg(), image_name="banana.jpg")
    assert not result.ok
    assert result.status_code == 401


def test_tc5a_http_provider_reads_token_from_env(token_server: str, monkeypatch) -> None:
    monkeypatch.setenv("API_TOKEN", TOKEN)
    provider = HTTPProvider("a", f"{token_server}/predict")
    result = provider.predict(_sample_jpeg(), task_context={"task_id": "provider-task"})
    assert result.ok, result.error
    assert result.task_id == "provider-task"
    assert provider.api_token == TOKEN
    assert TOKEN not in repr(provider)
