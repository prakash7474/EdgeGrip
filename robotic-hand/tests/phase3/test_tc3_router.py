"""TC3.x — multi-provider router with health checks and failover, one task to exactly one provider."""
from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest
import requests
import uvicorn
import threading

from phase1_pipeline.capture import FrameGrabber
from phase3_router import Router, HTTPProvider
from phase3_router.providers import mock_provider_a, mock_provider_b


PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
ROBOTIC_HAND = PROJECT_ROOT  # running from robotic-hand project dir context
IMAGES_DIR = ROBOTIC_HAND / "images"


def _free_port():
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture(scope="session")
def mock_providers():
    # start two controllable mock providers
    port_a = _free_port()
    port_b = _free_port()
    cfg_a = uvicorn.Config(mock_provider_a.app, host="127.0.0.1", port=port_a, log_level="warning")
    cfg_b = uvicorn.Config(mock_provider_b.app, host="127.0.0.1", port=port_b, log_level="warning")
    srv_a = uvicorn.Server(cfg_a)
    srv_b = uvicorn.Server(cfg_b)
    t_a = threading.Thread(target=srv_a.run, daemon=True)
    t_b = threading.Thread(target=srv_b.run, daemon=True)
    t_a.start()
    t_b.start()
    time.sleep(3)
    try:
        yield {"a": f"http://127.0.0.1:{port_a}/predict", "b": f"http://127.0.0.1:{port_b}/predict", "ha": f"http://127.0.0.1:{port_a}/health", "hb": f"http://127.0.0.1:{port_b}/health", "ca": f"http://127.0.0.1:{port_a}", "cb": f"http://127.0.0.1:{port_b}"}
    finally:
        srv_a.should_exit = True
        srv_b.should_exit = True
        t_a.join(timeout=5)
        t_b.join(timeout=5)


def test_tc3_1_one_task_to_exactly_one_provider_healthy_first(mock_providers):
    p1 = HTTPProvider("mock_a", mock_providers["a"])
    p2 = HTTPProvider("mock_b", mock_providers["b"])
    router = Router([p1, p2])
    grabber = FrameGrabber(IMAGES_DIR, camera_enabled=False)
    frame = grabber.next_frame()
    result = router.predict(frame.jpeg_bytes)
    assert result.ok
    assert result.provider == "mock_a"  # goes to first healthy
    assert result.attempts == 1


def test_tc3_2_failover_when_first_provider_fails(mock_providers):
    # make first fail
    requests.get(f"{mock_providers['ca']}/control/fail?fail=1")
    time.sleep(0.5)
    p1 = HTTPProvider("mock_a", mock_providers["a"])
    p2 = HTTPProvider("mock_b", mock_providers["b"])
    router = Router([p1, p2])
    grabber = FrameGrabber(IMAGES_DIR, camera_enabled=False)
    frame = grabber.next_frame()
    result = router.predict(frame.jpeg_bytes)
    assert result.ok
    assert result.provider == "mock_b"  # failed over to second
    assert result.attempts >= 2
    # reset
    requests.get(f"{mock_providers['ca']}/control/fail?fail=0")


def test_tc3_3_failover_on_slow_timeout(mock_providers):
    # make first very slow
    requests.get(f"{mock_providers['ca']}/control/slow?ms=5000")
    time.sleep(0.2)
    p1 = HTTPProvider("mock_a", mock_providers["a"], timeout=0.5)
    p2 = HTTPProvider("mock_b", mock_providers["b"], timeout=5)
    router = Router([p1, p2])
    grabber = FrameGrabber(IMAGES_DIR, camera_enabled=False)
    frame = grabber.next_frame()
    result = router.predict(frame.jpeg_bytes)
    assert result.ok
    assert result.provider == "mock_b"
    requests.get(f"{mock_providers['ca']}/control/slow?ms=0")


def test_tc3_4_both_fail_returns_failure(mock_providers):
    requests.get(f"{mock_providers['ca']}/control/fail?fail=1")
    requests.get(f"{mock_providers['cb']}/control/fail?fail=1")
    time.sleep(0.2)
    p1 = HTTPProvider("mock_a", mock_providers["a"])
    p2 = HTTPProvider("mock_b", mock_providers["b"])
    router = Router([p1, p2])
    grabber = FrameGrabber(IMAGES_DIR, camera_enabled=False)
    frame = grabber.next_frame()
    result = router.predict(frame.jpeg_bytes)
    assert not result.ok
    requests.get(f"{mock_providers['ca']}/control/fail?fail=0")
    requests.get(f"{mock_providers['cb']}/control/fail?fail=0")


def test_tc3_5_three_providers_including_real_option(mock_providers):
    requests.get(f"{mock_providers['ca']}/control/fail?fail=0")
    requests.get(f"{mock_providers['cb']}/control/fail?fail=0")
    p1 = HTTPProvider("mock_a", mock_providers["a"])
    p2 = HTTPProvider("mock_b", mock_providers["b"])
    p3 = HTTPProvider("real", os.environ.get("REAL_ENDPOINT", "http://127.0.0.1:8001/predict"))
    router = Router([p1, p2, p3], health_check=False)  # don't require health for third
    grabber = FrameGrabber(IMAGES_DIR, camera_enabled=False)
    frame = grabber.next_frame()
    result = router.predict(frame.jpeg_bytes)
    assert result.ok
    assert result.provider in ("mock_a", "mock_b")
