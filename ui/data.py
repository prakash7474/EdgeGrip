"""Read-only data sources for the EdgeGrip dashboard.

``DemoSource`` produces deterministic synthetic values so the UI runs without
a camera, Docker, broker or cloud endpoint. ``LiveSource`` reads the existing
run databases, latency CSV, actuation log and opt-in router telemetry. Missing
files are reported as zero/unknown, never invented.
"""

from __future__ import annotations

import csv
import io
import json
import math
import os
import random
import socket
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from PIL import Image, ImageDraw, ImageFont

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEMO_BANNER = "SIMULATED DATA - NOT A MEASUREMENT"
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def iso_epoch(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat(timespec="seconds")


def percentile(values: Iterable[float], pct: float) -> float | None:
    """Linear-interpolated percentile; None for an empty sample."""
    data = sorted(float(value) for value in values)
    if not data:
        return None
    if len(data) == 1:
        return round(data[0], 3)
    position = (len(data) - 1) * (pct / 100.0)
    low = math.floor(position)
    high = math.ceil(position)
    if low == high:
        value = data[low]
    else:
        value = data[low] + (data[high] - data[low]) * (position - low)
    return round(value, 3)


def latency_summary(points: list[dict[str, Any]]) -> dict[str, Any]:
    values = [point["ms"] for point in points if point.get("ms") is not None]
    return {
        "points": points,
        "count": len(values),
        "p50": percentile(values, 50),
        "p95": percentile(values, 95),
        "p99": percentile(values, 99),
        "min": round(min(values), 3) if values else None,
        "max": round(max(values), 3) if values else None,
    }


def is_loopback(host: str) -> bool:
    host = host.strip().lower()
    if host in LOOPBACK_HOSTS:
        return True
    try:
        import ipaddress

        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def parse_mock_providers(values: Iterable[str]) -> dict[str, str]:
    """Parse ``name=base_url`` entries, allowing loopback http(s) only."""
    providers: dict[str, str] = {}
    for raw in values:
        name, separator, url = str(raw).partition("=")
        name, url = name.strip(), url.strip().rstrip("/")
        if not separator or not name or not url:
            raise ValueError(f"mock provider must be name=url, got {raw!r}")
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError(f"mock provider {name}: URL must be http(s)")
        if not is_loopback(parsed.hostname):
            raise ValueError(f"mock provider {name}: only loopback URLs are allowed")
        if not name.replace("-", "").replace("_", "").isalnum():
            raise ValueError(f"mock provider name is not safe: {name!r}")
        providers[name] = url
    return providers


class FrameService:
    """Render the latest JPEG at a low rate, with a prediction overlay."""

    def __init__(self, *, demo: bool, prediction: Callable[[], dict[str, Any] | None]):
        self.demo = demo
        self._prediction = prediction
        self._lock = threading.Lock()
        self._cached = b""
        self._cached_at = 0.0
        self._camera = None
        self._camera_failed_at = 0.0
        self._rng = random.Random(6)

    def get_jpeg(self) -> bytes:
        with self._lock:
            now = time.monotonic()
            if self._cached and now - self._cached_at < 0.7:
                return self._cached
            image = self._demo_image() if self.demo else self._camera_image()
            self._draw_overlay(image)
            buffer = io.BytesIO()
            image.convert("RGB").save(buffer, format="JPEG", quality=78)
            self._cached = buffer.getvalue()
            self._cached_at = now
            return self._cached

    def close(self) -> None:
        if self._camera is not None:
            try:
                self._camera.release()
            except Exception:
                pass

    def _demo_image(self) -> Image.Image:
        width, height = 640, 360
        image = Image.new("RGB", (width, height), (18, 24, 32))
        draw = ImageDraw.Draw(image)
        for y in range(height):
            shade = int(20 + 18 * (y / height))
            draw.line([(0, y), (width, y)], fill=(shade, shade + 6, shade + 14))
        # Abstract part on a conveyor-like surface.
        draw.rectangle([70, 250, 570, 330], fill=(52, 60, 70))
        draw.ellipse([240, 120, 400, 280], fill=(154, 166, 176), outline=(210, 220, 228), width=4)
        draw.ellipse([292, 172, 348, 228], fill=(36, 44, 54), outline=(210, 220, 228), width=3)
        draw.line([(180, 210), (250, 210)], fill=(198, 206, 214), width=8)
        draw.text((14, 12), "DEMO CAMERA - SIMULATED", fill=(240, 200, 80),
                  font=ImageFont.load_default())
        return image

    def _camera_image(self) -> Image.Image:
        now = time.monotonic()
        if self._camera is None and now - self._camera_failed_at > 3.0:
            try:
                import cv2

                camera = cv2.VideoCapture(0)
                if camera.isOpened():
                    self._camera = camera
                else:
                    camera.release()
                    self._camera_failed_at = now
            except Exception:
                self._camera_failed_at = now
        if self._camera is not None:
            try:
                ok, frame = self._camera.read()
                if ok and frame is not None:
                    import cv2

                    rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                    return Image.fromarray(rgb)
            except Exception:
                pass
            try:
                self._camera.release()
            except Exception:
                pass
            self._camera = None
            self._camera_failed_at = now

        image = Image.new("RGB", (640, 360), (16, 20, 26))
        draw = ImageDraw.Draw(image)
        draw.text((20, 150), "Camera unavailable or busy", fill=(235, 238, 242),
                  font=ImageFont.load_default())
        draw.text((20, 172), "The dashboard does not substitute a fake frame in live mode.",
                  fill=(170, 180, 190), font=ImageFont.load_default())
        return image

    def _draw_overlay(self, image: Image.Image) -> None:
        prediction = self._prediction() or {}
        draw = ImageDraw.Draw(image)
        label = prediction.get("label") or "no prediction yet"
        score = prediction.get("score")
        score_text = f"{float(score):.3f}" if score is not None else "n/a"
        stamp = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
        text = f"{label}  |  score {score_text}  |  {stamp}"
        width = min(image.width - 20, max(320, len(text) * 7 + 20))
        draw.rectangle([10, image.height - 42, 10 + width, image.height - 10],
                       fill=(0, 0, 0))
        draw.text((20, image.height - 32), text, fill=(240, 244, 248),
                  font=ImageFont.load_default())
        if self.demo:
            draw.text((12, 34), DEMO_BANNER, fill=(255, 210, 70), font=ImageFont.load_default())


class DemoSource:
    """Deterministic synthetic state for an offline demo."""

    demo = True
    environment = "demo"
    banner = DEMO_BANNER

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._run: dict[str, Any] = {
            "status": "idle", "run_id": None, "started_at": None,
            "pid": None, "params": None, "returncode": None,
        }
        self._stopped = False
        self._last_prediction = {"label": "bolt", "score": 0.93, "at": utc_now()}
        self._latency = self._make_latency()
        self._events = self._make_events()
        self._actuation = self._make_actuation()
        self._counts = {
            "sent": 240, "acked": 236, "applied": 231, "safe_default": 3,
            "outbox_depth": 4, "unconfirmed": 1,
            "discarded": {"duplicate": 2, "late": 1, "mismatch": 1},
        }
        self._providers = {
            "mock-a": {
                "name": "mock-a", "health": "healthy", "healthy": True,
                "ewma_ms": 42.3, "circuit": "closed", "last_failover": None,
                "last_latency_ms": 41.8, "last_seen": utc_now(),
            },
            "mock-b": {
                "name": "mock-b", "health": "degraded", "healthy": True,
                "ewma_ms": 218.6, "circuit": "half_open",
                "last_failover": iso_epoch(time.time() - 38),
                "last_latency_ms": 236.4, "last_seen": utc_now(),
            },
            "mock-c": {
                "name": "mock-c", "health": "failed", "healthy": False,
                "ewma_ms": None, "circuit": "open",
                "last_failover": iso_epoch(time.time() - 91),
                "last_latency_ms": None, "last_seen": utc_now(),
            },
        }
        self.frames = FrameService(demo=True, prediction=lambda: self._last_prediction)

    def _make_latency(self) -> list[dict[str, Any]]:
        rng = random.Random(11)
        points = []
        now = time.time()
        for index in range(90):
            provider = "mock-a" if index % 7 else "mock-b"
            base = 38 + 8 * math.sin(index / 6.0) if provider == "mock-a" else 190 + 30 * math.sin(index / 4.0)
            points.append({
                "t": iso_epoch(now - (90 - index) * 2),
                "ms": round(max(1.0, base + rng.uniform(-8, 8)), 3),
                "provider": provider,
                "label": "bolt" if index % 3 else "nut",
            })
        return points

    def _make_events(self) -> list[dict[str, Any]]:
        now = time.time()
        return [
            {"timestamp": iso_epoch(now - 8), "kind": "retry", "severity": "warning",
             "provider": "mock-b", "message": "Outbox retry after ACK timeout", "task_id": "demo-0238"},
            {"timestamp": iso_epoch(now - 23), "kind": "failover", "severity": "warning",
             "provider": "mock-b", "message": "Failover from mock-a after a 2.0 s timeout", "task_id": "demo-0231"},
            {"timestamp": iso_epoch(now - 38), "kind": "discard", "severity": "error",
             "provider": "mock-c", "message": "Discarded duplicate response", "task_id": "demo-0229"},
            {"timestamp": iso_epoch(now - 54), "kind": "nack", "severity": "error",
             "provider": "mock-b", "message": "Actuator NACK: safe_state", "task_id": "demo-0227"},
            {"timestamp": iso_epoch(now - 77), "kind": "provider_error", "severity": "error",
             "provider": "mock-c", "message": "HTTP 503 from provider", "task_id": "demo-0219"},
        ]

    def _make_actuation(self) -> list[dict[str, Any]]:
        now = time.time()
        rows = []
        actions = [("grip", {"speed": 25}, True, "ack"), ("hold", {}, False, "hold"),
                   ("release", {"speed": 20}, True, "ack"), ("grip", {"speed": 25}, False, "timeout")]
        for index, (action, parameters, acked, reason) in enumerate(actions * 4):
            rows.append({
                "task_id": f"demo-{230 - index:04d}",
                "command_id": None if action == "hold" else f"cmd-demo-{230 - index:04d}",
                "action": action,
                "parameters": parameters,
                "acked": acked,
                "reason": reason,
                "detail": "",
                "sent_at": iso_epoch(now - index * 7),
                "acked_at": iso_epoch(now - index * 7 + 0.2) if acked else None,
                "latency_ms": 210.4 if acked else None,
            })
        return rows

    def state(self, run: dict[str, Any] | None = None) -> dict[str, Any]:
        with self._lock:
            providers = [dict(item) for item in self._providers.values()]
            counts = json.loads(json.dumps(self._counts))
            last = dict(self._last_prediction)
            status = dict(self._run)
        return {
            "demo": True,
            "banner": DEMO_BANNER,
            "environment": "demo",
            "generated_at": utc_now(),
            "run": run or status,
            "broker": {"status": "connected", "host": "demo", "port": 0, "demo": True},
            "providers": providers,
            "counts": counts,
            "last_prediction": last,
            "actuator_stopped": self._stopped,
            "stop": {
                "not_an_emergency_stop": True,
                "label": "Software stop only - not an emergency stop",
            },
        }

    def latency(self) -> dict[str, Any]:
        with self._lock:
            return latency_summary([dict(point) for point in self._latency])

    def events(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(event) for event in self._events[:limit]]

    def actuation(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(row) for row in self._actuation[:limit]]

    def frame_jpeg(self) -> bytes:
        return self.frames.get_jpeg()

    def start_run(self, params: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            if self._run["status"] == "running":
                raise RuntimeError("a run is already active")
            self._run = {
                "status": "running",
                "run_id": f"demo-{int(time.time())}",
                "started_at": utc_now(),
                "pid": None,
                "params": params,
                "returncode": None,
            }
            self._events.insert(0, {
                "timestamp": utc_now(), "kind": "run", "severity": "info",
                "provider": None, "message": "Demo run started", "task_id": None,
            })
            return dict(self._run)

    def stop_run(self) -> dict[str, Any]:
        with self._lock:
            if self._run["status"] != "running":
                raise RuntimeError("no active run")
            self._run["status"] = "idle"
            self._events.insert(0, {
                "timestamp": utc_now(), "kind": "run", "severity": "info",
                "provider": None, "message": "Demo run stopped", "task_id": None,
            })
            return dict(self._run)

    def mock_fault(self, provider: str, action: str, *, confirmed: bool,
                   ms: int = 1000) -> dict[str, Any]:
        if not confirmed:
            raise ValueError("confirmation is required")
        if action not in {"fail", "slow", "reset"}:
            raise ValueError("unknown mock action")
        with self._lock:
            item = self._providers.get(provider)
            if item is None:
                raise PermissionError("provider is not a configured mock provider")
            if action == "fail":
                item.update(health="failed", healthy=False, circuit="open",
                            last_seen=utc_now())
                message = "Forced failure enabled"
            elif action == "slow":
                item.update(health="degraded", healthy=True, circuit="closed",
                            ewma_ms=float(max(1, ms)), last_seen=utc_now())
                message = f"Forced slow response enabled ({ms} ms)"
            else:
                item.update(health="healthy", healthy=True, circuit="closed",
                            ewma_ms=42.3, last_seen=utc_now())
                message = "Mock provider reset"
            self._events.insert(0, {
                "timestamp": utc_now(), "kind": "mock_control",
                "severity": "warning" if action != "reset" else "info",
                "provider": provider, "message": message, "task_id": None,
            })
        return {"ok": True, "provider": provider, "action": action, "demo": True}

    def software_stop(self) -> dict[str, Any]:
        self._stopped = True
        with self._lock:
            self._events.insert(0, {
                "timestamp": utc_now(), "kind": "software_stop", "severity": "warning",
                "provider": None,
                "message": "Demo software stop requested (not an emergency stop)",
                "task_id": None,
            })
        return {
            "ok": True,
            "performed": True,
            "demo": True,
            "not_an_emergency_stop": True,
            "message": "Software stop simulated. This is not an emergency stop.",
        }


class LiveSource:
    """Read the latest run artifacts and opt-in router telemetry read-only."""

    demo = False
    banner = None

    def __init__(
        self,
        *,
        project_root: Path = PROJECT_ROOT,
        mock_providers: dict[str, str] | None = None,
        telemetry_path: str | Path | None = None,
    ) -> None:
        self.root = Path(project_root)
        self.run_root = self.root / "logs" / "live"
        self.mock_providers = mock_providers or parse_mock_providers(
            filter(None, os.getenv("UI_MOCK_PROVIDERS", "").split(","))
        )
        self.telemetry_path = Path(
            telemetry_path
            or os.getenv("ROUTER_TELEMETRY_PATH", "").strip()
            or self.root / "logs" / "ui" / "router_telemetry.jsonl"
        )
        self.environment = os.getenv("ENVIRONMENT_LABEL", "").strip() or "live"
        self._broker_cache = (0.0, {"status": "unknown"})
        self._last_prediction: dict[str, Any] | None = None
        self.frames = FrameService(demo=False, prediction=lambda: self._last_prediction)

    # ------------------------------------------------------------- artifacts
    def _latest_run_dir(self) -> Path | None:
        if not self.run_root.is_dir():
            return None
        candidates = [
            path for path in self.run_root.iterdir()
            if path.is_dir() and any(
                (path / name).is_file()
                for name in ("outbox.db", "conflict.db", "latency.csv", "actuation.csv")
            )
        ]
        if not candidates:
            return None
        candidates.sort(key=lambda path: path.stat().st_mtime, reverse=True)
        return candidates[0]

    def _connect_ro(self, path: Path):
        if not path.is_file():
            return None
        try:
            return sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=1.0)
        except sqlite3.Error:
            return None

    def _query(self, path: Path, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        connection = self._connect_ro(path)
        if connection is None:
            return []
        try:
            connection.row_factory = sqlite3.Row
            return list(connection.execute(sql, params).fetchall())
        except sqlite3.Error:
            return []
        finally:
            connection.close()

    def _counts(self) -> dict[str, Any]:
        run_dir = self._latest_run_dir()
        outbox_db = (run_dir / "outbox.db") if run_dir else self.root / "phase2_reliability" / "outbox.db"
        conflict_db = (run_dir / "conflict.db") if run_dir else None

        by_state = {row["state"]: int(row["count"]) for row in self._query(
            outbox_db, "SELECT state, COUNT(*) AS count FROM outbox GROUP BY state"
        )}
        discarded = {}
        applied = 0
        safe_default = 0
        unconfirmed = 0
        if conflict_db is not None:
            discarded = {row["reason"]: int(row["count"]) for row in self._query(
                conflict_db,
                "SELECT reason, COUNT(*) AS count FROM discarded GROUP BY reason",
            )}
            rows = self._query(conflict_db, "SELECT COUNT(*) AS count FROM applied_ids")
            applied = int(rows[0]["count"]) if rows else 0
            rows = self._query(
                conflict_db,
                "SELECT COUNT(*) AS count FROM results WHERE decision='safe_default'",
            )
            safe_default = int(rows[0]["count"]) if rows else 0
            rows = self._query(
                conflict_db,
                "SELECT COUNT(*) AS count FROM actuation_log "
                "WHERE command_id IS NOT NULL AND acked_at IS NULL",
            )
            unconfirmed = int(rows[0]["count"]) if rows else 0
        return {
            "sent": sum(by_state.values()),
            "acked": int(by_state.get("acked", 0)),
            "applied": applied,
            "safe_default": safe_default,
            "outbox_depth": int(by_state.get("pending", 0)) + int(by_state.get("sent", 0)),
            "unconfirmed": unconfirmed,
            "discarded": {
                "duplicate": int(discarded.get("duplicate", 0)),
                "late": int(discarded.get("late", 0)),
                "mismatch": int(discarded.get("mismatch", 0)),
            },
        }

    def _latency_points(self) -> list[dict[str, Any]]:
        run_dir = self._latest_run_dir()
        if run_dir is not None and not (run_dir / "latency.csv").is_file():
            run_dir = None
        path = (run_dir / "latency.csv") if run_dir else self.root / "logs" / "latency.csv"
        if not path.is_file():
            return []
        try:
            with path.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
        except OSError:
            return []
        points = []
        for row in rows[-300:]:
            raw_ms = row.get("edge_latency_ms") or row.get("latency_ms")
            try:
                ms = float(raw_ms) if raw_ms not in (None, "") else None
            except ValueError:
                ms = None
            if ms is None:
                continue
            try:
                attempts = int(row.get("attempts") or 0)
            except ValueError:
                attempts = 0
            points.append({
                "t": row.get("result_at_utc") or row.get("sent_at") or "",
                "ms": round(ms, 3),
                "provider": row.get("provider") or "unknown",
                "label": row.get("decision") or None,
                "attempts": attempts,
            })
        return points

    def _telemetry(self) -> list[dict[str, Any]]:
        if not self.telemetry_path.is_file():
            return []
        try:
            with self.telemetry_path.open("r", encoding="utf-8", errors="replace") as handle:
                lines = deque(handle, maxlen=2000)
        except OSError:
            return []
        events = []
        for line in lines:
            try:
                payload = json.loads(line)
            except ValueError:
                continue
            if isinstance(payload, dict):
                events.append(payload)
        return events

    def _providers(self, telemetry: list[dict[str, Any]],
                   points: list[dict[str, Any]]) -> list[dict[str, Any]]:
        providers: dict[str, dict[str, Any]] = {}
        for point in points:
            name = str(point.get("provider") or "unknown")
            item = providers.setdefault(name, {
                "name": name, "health": "unknown", "healthy": None,
                "ewma_ms": None, "circuit": "unknown", "last_failover": None,
                "last_latency_ms": None, "last_seen": None,
            })
            item["last_latency_ms"] = point["ms"]
            item["last_seen"] = point.get("t") or item["last_seen"]
            if int(point.get("attempts") or 0) > 1:
                item["last_failover"] = point.get("t") or item["last_failover"]
            values = [p["ms"] for p in points if p.get("provider") == name][-20:]
            if values:
                ewma = values[0]
                for value in values[1:]:
                    ewma = 0.3 * value + 0.7 * ewma
                item["ewma_ms"] = round(ewma, 3)
        for event in telemetry:
            name = str(event.get("provider") or "")
            if not name:
                continue
            item = providers.setdefault(name, {
                "name": name, "health": "unknown", "healthy": None,
                "ewma_ms": None, "circuit": "unknown", "last_failover": None,
                "last_latency_ms": None, "last_seen": None,
            })
            kind = event.get("kind")
            if kind == "provider_health":
                item["healthy"] = bool(event.get("healthy"))
                item["health"] = "healthy" if item["healthy"] else "failed"
                if event.get("circuit"):
                    item["circuit"] = str(event["circuit"])
            elif kind == "provider_result":
                item["last_seen"] = event.get("ts") or item["last_seen"]
                if event.get("circuit"):
                    item["circuit"] = str(event["circuit"])
                if event.get("ewma_ms") is not None:
                    item["ewma_ms"] = round(float(event["ewma_ms"]), 3)
                if event.get("latency_ms") is not None:
                    item["last_latency_ms"] = round(float(event["latency_ms"]), 3)
                if event.get("ok"):
                    item["healthy"] = True
                    item["health"] = "degraded" if item["circuit"] == "half_open" else "healthy"
                else:
                    item["healthy"] = False if item["circuit"] == "open" else item.get("healthy")
                    item["health"] = "failed" if item["circuit"] == "open" else "degraded"
            elif kind == "failover":
                item["last_failover"] = event.get("ts") or item["last_failover"]
        for item in providers.values():
            if item["circuit"] == "open":
                item["health"] = "failed"
                item["healthy"] = False
            elif item["circuit"] == "half_open":
                item["health"] = "degraded"
        return sorted(providers.values(), key=lambda item: item["name"])

    def _broker(self) -> dict[str, Any]:
        now = time.monotonic()
        if now - self._broker_cache[0] < 3.0:
            return self._broker_cache[1]
        host = os.getenv("MQTT_BROKER_HOST", "127.0.0.1").strip() or "127.0.0.1"
        try:
            port = int(os.getenv("MQTT_BROKER_PORT", "1883"))
        except ValueError:
            port = 1883
        status = "unavailable"
        try:
            with socket.create_connection((host, port), timeout=0.35):
                status = "connected"
        except OSError:
            status = "unavailable"
        result = {"status": status, "host": host, "port": port}
        self._broker_cache = (now, result)
        return result

    def state(self, run: dict[str, Any] | None = None) -> dict[str, Any]:
        points = self._latency_points()
        telemetry = self._telemetry()
        providers = self._providers(telemetry, points)
        last_result = next((
            event for event in reversed(telemetry)
            if event.get("kind") == "provider_result"
            and event.get("ok") and event.get("label")
        ), None)
        if last_result is not None:
            score = last_result.get("score")
            self._last_prediction = {
                "label": last_result.get("label"),
                "score": float(score) if score is not None else None,
                "at": last_result.get("ts"),
            }
        elif points:
            last = points[-1]
            self._last_prediction = {
                "label": last.get("label"),
                "score": None,
                "at": last.get("t"),
            }
        counts = self._counts()
        return {
            "demo": False,
            "banner": None,
            "environment": self.environment,
            "generated_at": utc_now(),
            "run": run or {
                "status": "idle", "run_id": None, "started_at": None,
                "pid": None, "params": None, "returncode": None,
            },
            "broker": self._broker(),
            "providers": providers,
            "counts": counts,
            "last_prediction": self._last_prediction,
            "actuator_stopped": False,
            "stop": {
                "not_an_emergency_stop": True,
                "label": "Software stop only - not an emergency stop",
            },
        }

    def latency(self) -> dict[str, Any]:
        return latency_summary(self._latency_points())

    def events(self, limit: int = 100) -> list[dict[str, Any]]:
        run_dir = self._latest_run_dir()
        events: list[dict[str, Any]] = []
        for event in self._telemetry():
            kind = str(event.get("kind") or "")
            if kind == "provider_result":
                if event.get("ok"):
                    continue
                kind = "provider_error"
            elif kind == "provider_health":
                continue
            events.append({
                "timestamp": event.get("ts") or "",
                "kind": kind or "event",
                "severity": "warning" if kind in {"failover", "retry"} else "info",
                "provider": event.get("provider"),
                "message": self._telemetry_message(event),
                "task_id": event.get("task_id"),
            })
        if run_dir is not None:
            for row in self._query(
                run_dir / "conflict.db",
                "SELECT task_id, provider, reason, received_at FROM discarded "
                "ORDER BY received_at DESC LIMIT 50",
            ):
                events.append({
                    "timestamp": row["received_at"], "kind": "discard",
                    "severity": "error", "provider": row["provider"],
                    "message": f"Discarded: {row['reason']}", "task_id": row["task_id"],
                })
            for row in self._query(
                run_dir / "conflict.db",
                "SELECT task_id, command_id, action, reason, acked_at FROM actuation_log "
                "WHERE acked=0 AND command_id IS NOT NULL AND acked_at IS NOT NULL "
                "ORDER BY acked_at DESC LIMIT 50",
            ):
                events.append({
                    "timestamp": row["acked_at"], "kind": "nack", "severity": "error",
                    "provider": None,
                    "message": f"Actuator NACK on {row['action']}: {row['reason'] or 'unknown'}",
                    "task_id": row["task_id"],
                })
            for row in self._query(
                run_dir / "outbox.db",
                "SELECT message_id, retry_count, last_attempt_at FROM outbox "
                "WHERE retry_count > 0 ORDER BY last_attempt_at DESC LIMIT 50",
            ):
                timestamp = row["last_attempt_at"]
                events.append({
                    "timestamp": iso_epoch(float(timestamp)) if timestamp else "",
                    "kind": "retry", "severity": "warning", "provider": None,
                    "message": f"Outbox retry #{int(row['retry_count'])}",
                    "task_id": row["message_id"],
                })
        events.sort(key=lambda item: str(item.get("timestamp") or ""), reverse=True)
        return events[:limit]

    @staticmethod
    def _telemetry_message(event: dict[str, Any]) -> str:
        kind = event.get("kind")
        if kind == "failover":
            return f"Failover after {event.get('attempts', '?')} attempt(s)"
        if kind == "provider_result" and not event.get("ok"):
            return f"Provider error: {event.get('error') or 'unknown'}"
        if kind == "run":
            return str(event.get("message") or "Run event")
        return str(event.get("message") or event.get("error") or kind or "Event")

    def actuation(self, limit: int = 50) -> list[dict[str, Any]]:
        run_dir = self._latest_run_dir()
        if run_dir is not None:
            rows = self._query(
                run_dir / "conflict.db",
                "SELECT * FROM actuation_log ORDER BY sent_at DESC LIMIT ?", (limit,),
            )
            result = []
            for row in rows:
                item = dict(row)
                try:
                    item["parameters"] = json.loads(item.get("parameters") or "{}")
                except ValueError:
                    item["parameters"] = {}
                item["acked"] = bool(item.get("acked"))
                result.append(item)
            if result:
                return result
            csv_path = run_dir / "actuation.csv"
            if csv_path.is_file():
                try:
                    with csv_path.open(newline="", encoding="utf-8") as handle:
                        return list(csv.DictReader(handle))[:limit]
                except OSError:
                    pass
        return []

    def frame_jpeg(self) -> bytes:
        return self.frames.get_jpeg()

    def mock_fault(self, provider: str, action: str, *, confirmed: bool,
                   ms: int = 1000) -> dict[str, Any]:
        if not confirmed:
            raise ValueError("confirmation is required")
        if action not in {"fail", "slow", "reset"}:
            raise ValueError("unknown mock action")
        base = self.mock_providers.get(provider)
        if not base:
            raise PermissionError("provider is not a configured local mock")
        suffix = {"fail": "fail", "slow": f"slow?ms={max(0, min(ms, 30000))}", "reset": "reset"}[action]
        url = f"{base}/control/{suffix}"
        try:
            with urllib.request.urlopen(url, timeout=2.0) as response:
                if response.status >= 400:
                    raise RuntimeError(f"mock returned HTTP {response.status}")
        except (urllib.error.URLError, OSError) as exc:
            raise RuntimeError(f"mock control failed: {exc.__class__.__name__}") from exc
        return {"ok": True, "provider": provider, "action": action, "demo": False}

    def software_stop(self) -> dict[str, Any]:
        port = os.getenv("ACTUATION_SERIAL_PORT", "").strip()
        if not port:
            return {
                "ok": False,
                "performed": False,
                "not_an_emergency_stop": True,
                "message": "No ACTUATION_SERIAL_PORT configured; no actuator command was sent. "
                           "This is not an emergency stop.",
            }
        try:
            from actuator.serial_actuator import SerialActuator

            actuator = SerialActuator(port)
            try:
                result = actuator.stop()
            finally:
                actuator.close(send_stop=False)
            return {
                "ok": bool(result.acked),
                "performed": True,
                "acked": bool(result.acked),
                "reason": result.reason,
                "not_an_emergency_stop": True,
                "message": "Software stop sent to the configured serial actuator. "
                           "This is not an emergency stop.",
            }
        except Exception as exc:
            return {
                "ok": False,
                "performed": False,
                "not_an_emergency_stop": True,
                "message": f"Software stop failed: {exc.__class__.__name__}. "
                           "This is not an emergency stop.",
            }



