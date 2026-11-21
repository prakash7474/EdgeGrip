"""Phase 7 dashboard tests: demo APIs, token gate, bind rules and controls.

All tests run offline. The optional Playwright screenshot test is skipped when
Playwright (or its browser) is not installed; the rest need no browser.
"""

from __future__ import annotations

import contextlib
import importlib.util
import json
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest
import uvicorn
from PIL import Image

from ui.data import DEMO_BANNER
from ui.run_control import RunValidationError, validate_run_params
from ui.server import BindError, create_app, validate_bind

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TOKEN = "phase7-ui-token-not-a-real-secret"


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
    raise RuntimeError("UI test server did not start")


@contextlib.contextmanager
def serve(app):
    host, port = "127.0.0.1", _free_port()
    server = uvicorn.Server(uvicorn.Config(
        app, host=host, port=port, log_level="warning", access_log=False,
    ))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        _wait_ready(host, port)
        yield f"http://{host}:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def _request(url: str, method: str = "GET", body: dict | None = None,
             headers: dict[str, str] | None = None) -> tuple[int, bytes, dict]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(
        url, data=data, method=method,
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, response.read(), dict(response.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), dict(exc.headers)


def _json(url: str, method: str = "GET", body: dict | None = None,
          headers: dict[str, str] | None = None):
    code, payload, response_headers = _request(url, method, body, headers)
    return code, json.loads(payload.decode("utf-8")), response_headers


@pytest.fixture()
def demo_server():
    with serve(create_app(demo=True)) as base_url:
        yield base_url


@pytest.fixture()
def token_server():
    with serve(create_app(demo=True, token=TOKEN)) as base_url:
        yield base_url


# ------------------------------------------------------------------- API state
def test_ui_state_contract_and_demo_banner(demo_server):
    code, state, _ = _json(f"{demo_server}/api/state")
    assert code == 200
    assert state["demo"] is True
    assert state["banner"] == DEMO_BANNER
    assert state["environment"] == "demo"
    assert {"run", "broker", "providers", "counts", "last_prediction", "stop"} <= set(state)
    assert state["stop"]["not_an_emergency_stop"] is True
    assert "not an emergency stop" in state["stop"]["label"].lower()

    required = {"name", "health", "ewma_ms", "circuit", "last_failover"}
    assert state["providers"]
    for provider in state["providers"]:
        assert required <= set(provider)
    counts = state["counts"]
    assert {"sent", "acked", "applied", "safe_default", "outbox_depth",
            "unconfirmed", "discarded"} <= set(counts)
    assert set(counts["discarded"]) == {"duplicate", "late", "mismatch"}

    page_code, page, _ = _request(f"{demo_server}/")
    assert page_code == 200
    assert DEMO_BANNER.encode() in page
    assert b"/static/app.js" in page
    assert not re.search(rb'<script[^>]+https?://', page)


def test_ui_latency_percentiles_and_points(demo_server):
    code, latency, _ = _json(f"{demo_server}/api/latency")
    assert code == 200
    assert latency["points"]
    assert latency["count"] == len(latency["points"])
    assert latency["p50"] <= latency["p95"] <= latency["p99"]
    assert latency["min"] <= latency["p50"]
    assert latency["max"] >= latency["p99"]


def test_ui_events_and_actuation_contract(demo_server):
    code, events, _ = _json(f"{demo_server}/api/events")
    assert code == 200
    assert events
    kinds = {event["kind"] for event in events}
    assert {"failover", "retry", "discard", "nack"} <= kinds
    for event in events:
        assert {"timestamp", "kind", "severity", "message"} <= set(event)

    code, rows, _ = _json(f"{demo_server}/api/actuation")
    assert code == 200
    assert rows
    assert {"task_id", "action", "acked", "sent_at"} <= set(rows[0])


def test_ui_frame_endpoint_returns_a_jpeg(demo_server):
    code, payload, headers = _request(f"{demo_server}/stream/frame")
    assert code == 200
    assert headers.get("content-type", "").startswith("image/jpeg")
    assert payload.startswith(b"\xff\xd8")
    from io import BytesIO

    image = Image.open(BytesIO(payload))
    image.load()
    assert image.size == (640, 360)


# --------------------------------------------------------------- run controls
def test_ui_run_start_stop_and_rejects_unknown_fields(demo_server):
    code, started, _ = _json(
        f"{demo_server}/api/run/start", "POST",
        {"source": "images", "fps": 2, "deadline_ms": 8000, "duration": 10,
         "endpoints": [], "camera": 0},
    )
    assert code == 200
    assert started["status"] == "running"

    code, _, _ = _json(
        f"{demo_server}/api/run/start", "POST",
        {"source": "images", "fps": 2, "deadline_ms": 8000, "duration": 10,
         "endpoints": [], "camera": 0},
    )
    assert code == 409

    code, stopped, _ = _json(f"{demo_server}/api/run/stop", "POST", {})
    assert code == 200
    assert stopped["status"] == "idle"

    code, _, _ = _json(
        f"{demo_server}/api/run/start", "POST",
        {"source": "images", "fps": 2, "deadline_ms": 8000, "unknown": 1},
    )
    assert code == 422
    code, _, _ = _json(
        f"{demo_server}/api/run/start", "POST",
        {"source": "shell", "fps": 2, "deadline_ms": 8000},
    )
    assert code in {400, 422}


def test_ui_run_parameter_validation_is_strict():
    with pytest.raises(RunValidationError, match="unknown parameter"):
        validate_run_params({"fps": 2, "rm": "-rf"}, demo=True)
    with pytest.raises(RunValidationError, match="fps"):
        validate_run_params({"fps": 0}, demo=True)
    with pytest.raises(RunValidationError, match="deadline_ms"):
        validate_run_params({"deadline_ms": 10}, demo=True)
    with pytest.raises(RunValidationError, match="/predict"):
        validate_run_params({"endpoints": ["http://127.0.0.1:8001/health"]}, demo=True)
    with pytest.raises(RunValidationError, match="at least one"):
        validate_run_params({}, demo=False)
    valid = validate_run_params({
        "source": "images", "fps": 2, "deadline_ms": 8000,
        "endpoints": ["http://127.0.0.1:8001/predict"],
    }, demo=False)
    assert valid["endpoints"] == ["http://127.0.0.1:8001/predict"]


# ---------------------------------------------------------------- mock and stop
def test_ui_mock_faults_require_confirmation_and_mock_provider(demo_server):
    code, _, _ = _json(
        f"{demo_server}/api/mock/mock-a/fail", "POST", {"confirm": False}
    )
    assert code == 400

    code, result, _ = _json(
        f"{demo_server}/api/mock/mock-a/fail", "POST", {"confirm": True}
    )
    assert code == 200
    assert result["ok"] is True

    code, result, _ = _json(
        f"{demo_server}/api/mock/mock-a/slow", "POST", {"confirm": True, "ms": 1200}
    )
    assert code == 200 and result["action"] == "slow"

    code, result, _ = _json(
        f"{demo_server}/api/mock/mock-a/reset", "POST", {"confirm": True}
    )
    assert code == 200 and result["action"] == "reset"

    code, _, _ = _json(
        f"{demo_server}/api/mock/cloud-a/fail", "POST", {"confirm": True}
    )
    assert code == 403

    code, _, _ = _json(
        f"{demo_server}/api/mock/mock-a/explode", "POST", {"confirm": True}
    )
    assert code == 400


def test_ui_software_stop_is_labelled_not_emergency(demo_server):
    code, result, _ = _json(f"{demo_server}/api/stop", "POST", {})
    assert code == 200
    assert result["not_an_emergency_stop"] is True
    assert "not an emergency stop" in result["message"].lower()


# --------------------------------------------------------------- bind and token
def test_ui_default_bind_is_loopback_and_remote_bind_needs_flag_and_token():
    validate_bind("127.0.0.1", allow_remote=False, token="")
    validate_bind("localhost", allow_remote=False, token="")
    with pytest.raises(BindError, match="allow-remote"):
        validate_bind("0.0.0.0", allow_remote=False, token="secret")
    with pytest.raises(BindError, match="API_TOKEN"):
        validate_bind("0.0.0.0", allow_remote=True, token="")
    validate_bind("0.0.0.0", allow_remote=True, token="secret")

    result = subprocess.run(
        [sys.executable, "-m", "ui", "--host", "0.0.0.0", "--port", "65530"],
        cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 2
    assert "refusing a non-loopback bind" in result.stderr

    result = subprocess.run(
        [sys.executable, "-m", "ui", "--host", "0.0.0.0", "--port", "65530",
         "--allow-remote"],
        cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=20,
        env={**__import__("os").environ, "API_TOKEN": ""},
    )
    assert result.returncode == 2
    assert "API_TOKEN" in result.stderr


def test_ui_token_is_required_and_never_echoed(token_server, caplog):
    code, body, _ = _json(f"{token_server}/api/state")
    assert code == 401
    assert TOKEN not in json.dumps(body)

    headers = {"X-API-Token": TOKEN}
    code, state, _ = _json(f"{token_server}/api/state", headers=headers)
    assert code == 200
    payloads = [json.dumps(state)]
    for path in ("/api/latency", "/api/events", "/api/actuation"):
        code, value, _ = _json(f"{token_server}{path}", headers=headers)
        assert code == 200
        payloads.append(json.dumps(value))
    code, frame, _ = _request(f"{token_server}/stream/frame", headers=headers)
    assert code == 200
    assert TOKEN.encode() not in frame
    assert all(TOKEN not in payload for payload in payloads)
    assert TOKEN not in caplog.text
    assert TOKEN not in json.dumps(dict(_request(f"{token_server}/healthz")[2]))


def test_ui_remote_bind_requires_the_same_header(token_server):
    code, _, headers = _json(f"{token_server}/api/state")
    assert code == 401
    assert headers.get("www-authenticate") == "X-API-Token"
    code, _, _ = _json(f"{token_server}/api/state", headers={"X-API-Token": "wrong"})
    assert code == 401


# ---------------------------------------------------------------- static offline
def test_ui_static_assets_have_no_cdn_and_the_chart_is_local():
    html = (PROJECT_ROOT / "ui" / "static" / "index.html").read_text(encoding="utf-8")
    js = (PROJECT_ROOT / "ui" / "static" / "app.js").read_text(encoding="utf-8")
    css = (PROJECT_ROOT / "ui" / "static" / "app.css").read_text(encoding="utf-8")
    assert not re.search(r'(?:src|href)\s*=\s*["\']https?://', html)
    for text in (html, js, css):
        assert "cdn." not in text.lower()
        assert "fonts.googleapis" not in text.lower()
    assert "getContext(\"2d\")" in js
    assert "demo-banner" in css


# ------------------------------------------------------------------ optional
@pytest.mark.skipif(
    importlib.util.find_spec("playwright") is None,
    reason="playwright not installed; no browser screenshot was taken",
)
def test_ui_playwright_screenshot_optional(demo_server, tmp_path):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch()
        except Exception as exc:  # pragma: no cover - browser binary missing
            pytest.skip(f"playwright browser unavailable: {exc.__class__.__name__}")
        page = browser.new_page(viewport={"width": 1440, "height": 1100})
        page.goto(demo_server, wait_until="networkidle")
        page.wait_for_selector("#providers .provider")
        page.screenshot(path=str(tmp_path / "dashboard.png"), full_page=True)
        assert DEMO_BANNER in page.locator("#demo-banner").inner_text()
        browser.close()
    assert (tmp_path / "dashboard.png").stat().st_size > 10_000
