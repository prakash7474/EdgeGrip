"""Shared fixtures: an in-process mock server, a client and a frame source.

The mock server runs on a loopback socket in a background thread, so the tests
need no Docker, no credentials and no internet access.
"""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
import uvicorn

from mock_server.app import create_app
from phase1_pipeline.capture import FrameGrabber
from phase1_pipeline.client import CloudClient

PROJECT_DIR = Path(__file__).resolve().parent.parent
IMAGES_DIR = PROJECT_DIR / "images"
STARTUP_TIMEOUT_SECONDS = 15.0


def free_port() -> int:
    """Return a currently unused loopback port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_until_ready(host: str, port: int, timeout: float = STARTUP_TIMEOUT_SECONDS) -> None:
    """Block until the mock server accepts TCP connections."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.25):
                return
        except OSError:
            time.sleep(0.05)
    raise RuntimeError(f"Mock server did not start on {host}:{port} within {timeout:.0f}s")


@pytest.fixture(scope="session")
def mock_server_url() -> Iterator[str]:
    """Start the deterministic mock inference server for the test session."""
    host = "127.0.0.1"
    port = free_port()
    config = uvicorn.Config(
        create_app(IMAGES_DIR),
        host=host,
        port=port,
        log_level="warning",
        access_log=False,
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, name="mock-server", daemon=True)
    thread.start()
    try:
        wait_until_ready(host, port)
        yield f"http://{host}:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        if thread.is_alive():
            raise RuntimeError("Mock server thread did not stop")


@pytest.fixture()
def endpoint(mock_server_url: str) -> str:
    """Full URL of the mock ``/predict`` endpoint."""
    return f"{mock_server_url}/predict"


@pytest.fixture()
def slow_endpoint(mock_server_url: str) -> str:
    """URL of the mock endpoint that sleeps before answering."""
    return f"{mock_server_url}/predict/slow"


@pytest.fixture()
def client(endpoint: str) -> Iterator[CloudClient]:
    """Client with a generous timeout for normal-path tests."""
    with CloudClient(endpoint, timeout_seconds=10.0) as instance:
        yield instance


@pytest.fixture()
def grabber() -> FrameGrabber:
    """Frame source that always uses the bundled sample images."""
    return FrameGrabber(IMAGES_DIR, camera_enabled=False)
