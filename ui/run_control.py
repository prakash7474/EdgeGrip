"""Validated start/stop control for ``live_run.py``.

The dashboard never accepts a shell command. It accepts a small typed parameter
set, builds an argument vector, and starts ``sys.executable live_run.py`` as a
separate process with ``shell=False``.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ALLOWED_PARAMS = {
    "source", "fps", "deadline_ms", "duration", "endpoints", "camera",
}


class RunValidationError(ValueError):
    """A run parameter is missing, malformed or out of range."""


class RunBusyError(RuntimeError):
    """A run is already active, or there is no active run to stop."""


def _number(payload: dict[str, Any], name: str, default: float,
            minimum: float, maximum: float) -> float:
    value = payload.get(name, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RunValidationError(f"{name} must be a number")
    number = float(value)
    if not minimum <= number <= maximum:
        raise RunValidationError(f"{name} must be between {minimum} and {maximum}")
    return number


def validate_run_params(payload: dict[str, Any], *, demo: bool) -> dict[str, Any]:
    """Validate and normalise dashboard run parameters."""
    if not isinstance(payload, dict):
        raise RunValidationError("run parameters must be a JSON object")
    unknown = set(payload) - ALLOWED_PARAMS
    if unknown:
        raise RunValidationError(f"unknown parameter(s): {', '.join(sorted(unknown))}")

    source = payload.get("source", "camera")
    if source not in {"camera", "images"}:
        raise RunValidationError("source must be 'camera' or 'images'")
    fps = _number(payload, "fps", 2.0, 0.1, 30.0)
    deadline_ms = _number(payload, "deadline_ms", 8000.0, 100.0, 60000.0)
    duration = _number(payload, "duration", 60.0, 1.0, 3600.0)
    camera = _number(payload, "camera", 0, 0, 16)
    if int(camera) != camera:
        raise RunValidationError("camera must be a whole number")

    raw_endpoints = payload.get("endpoints", [])
    if isinstance(raw_endpoints, str):
        raw_endpoints = [part.strip() for part in raw_endpoints.split(",") if part.strip()]
    if not isinstance(raw_endpoints, list) or len(raw_endpoints) > 8:
        raise RunValidationError("endpoints must be a list of at most 8 URLs")
    endpoints: list[str] = []
    for raw in raw_endpoints:
        if not isinstance(raw, str):
            raise RunValidationError("each endpoint must be a string")
        endpoint = raw.strip()
        if not endpoint or len(endpoint) > 2000 or any(ch.isspace() for ch in endpoint):
            raise RunValidationError(f"invalid endpoint: {raw!r}")
        parsed = urllib.parse.urlparse(endpoint)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise RunValidationError(f"endpoint must be an http(s) URL: {endpoint!r}")
        if not parsed.path.rstrip("/").endswith("/predict"):
            raise RunValidationError(f"endpoint path must end in /predict: {endpoint!r}")
        endpoints.append(endpoint)
    if not demo and not endpoints:
        raise RunValidationError("at least one provider endpoint is required outside demo mode")

    return {
        "source": source,
        "fps": round(fps, 3),
        "deadline_ms": round(deadline_ms, 3),
        "duration": round(duration, 3),
        "endpoints": endpoints,
        "camera": int(camera),
    }


class RunManager:
    """Own the one optional child run process and its status."""

    def __init__(self, *, demo: bool, project_root: Path = PROJECT_ROOT,
                 source: Any = None) -> None:
        self.demo = demo
        self.root = Path(project_root)
        self.source = source
        self._lock = threading.Lock()
        self._process: subprocess.Popen | None = None
        self._state: dict[str, Any] = {
            "status": "idle",
            "run_id": None,
            "started_at": None,
            "pid": None,
            "params": None,
            "returncode": None,
            "log_path": None,
        }
        self.telemetry_path = self.root / "logs" / "ui" / "router_telemetry.jsonl"

    def status(self) -> dict[str, Any]:
        with self._lock:
            process = self._process
            if process is not None:
                code = process.poll()
                if code is None:
                    self._state["status"] = "running"
                else:
                    self._state["returncode"] = int(code)
                    self._state["status"] = "completed" if code == 0 else "failed"
                    self._process = None
                    self._state["pid"] = None
            return dict(self._state)

    def start(self, payload: dict[str, Any]) -> dict[str, Any]:
        params = validate_run_params(payload, demo=self.demo)
        with self._lock:
            if self._process is not None and self._process.poll() is None:
                raise RunBusyError("a run is already active")
            if self._state.get("status") == "running":
                raise RunBusyError("a run is already active")
            run_id = datetime.now(timezone.utc).strftime("ui-%Y%m%dT%H%M%SZ")
            started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
            if self.demo:
                if self.source is not None:
                    self.source.start_run(params)
                self._state = {
                    "status": "running", "run_id": run_id, "started_at": started_at,
                    "pid": None, "params": params, "returncode": None,
                    "log_path": None,
                }
                return dict(self._state)

            self.telemetry_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                self.telemetry_path.write_text("", encoding="utf-8")
            except OSError:
                pass
            log_path = self.root / "logs" / "ui" / f"run_{run_id}.log"
            log_path.parent.mkdir(parents=True, exist_ok=True)
            command = [
                sys.executable, str(self.root / "live_run.py"),
                "--duration", f"{params['duration']:g}",
                "--fps", f"{params['fps']:g}",
                "--source", params["source"],
                "--deadline-ms", f"{params['deadline_ms']:g}",
                "--camera", str(params["camera"]),
                "--run-dir", str(self.root / "logs" / "live"),
                "--out", str(self.root / "logs" / "ui" / "last_run_report.md"),
                "--label", "dashboard",
            ]
            if params["endpoints"]:
                command += ["--endpoints", ",".join(params["endpoints"])]
            env = os.environ.copy()
            env["ROUTER_TELEMETRY_PATH"] = str(self.telemetry_path)
            try:
                handle = log_path.open("a", encoding="utf-8")
            except OSError as exc:
                raise RunValidationError(f"could not open run log: {exc.__class__.__name__}") from exc
            try:
                process = subprocess.Popen(  # noqa: S603 - fixed argv, shell=False
                    command,
                    cwd=str(self.root),
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=handle,
                    stderr=subprocess.STDOUT,
                    shell=False,
                )
            except OSError as exc:
                handle.close()
                raise RunValidationError(
                    f"could not start live_run.py: {exc.__class__.__name__}"
                ) from exc
            # The child owns the descriptor after Popen; closing ours is safe.
            handle.close()
            self._process = process
            self._state = {
                "status": "running", "run_id": run_id, "started_at": started_at,
                "pid": process.pid, "params": params, "returncode": None,
                "log_path": str(log_path),
            }
            return dict(self._state)

    def stop(self) -> dict[str, Any]:
        with self._lock:
            process = self._process
            if self.demo:
                if self.source is not None:
                    try:
                        self.source.stop_run()
                    except RuntimeError as exc:
                        raise RunBusyError(str(exc)) from exc
                if self._state.get("status") != "running":
                    raise RunBusyError("no active run")
                self._state["status"] = "idle"
                self._state["pid"] = None
                return dict(self._state)
            if process is None or process.poll() is not None:
                raise RunBusyError("no active run")
            self._state["status"] = "stopping"
            try:
                process.terminate()
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    pass
            except OSError:
                pass
            self._state["returncode"] = int(process.returncode or 0)
            self._state["status"] = "idle"
            self._state["pid"] = None
            self._process = None
            return dict(self._state)
