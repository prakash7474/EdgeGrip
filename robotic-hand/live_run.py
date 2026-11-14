"""Phase 5a live run: webcam -> outbox -> MQTT -> worker -> router -> providers.

This is a measurement harness, not a new pipeline stage. It wires the existing
Phase 1-4 components together, captures frames at a requested rate for a
requested duration, and writes ``docs/live_results.md`` from real observations
only. Values that cannot be measured are reported as "not available".

Examples
--------
    python live_run.py --duration 120 --fps 2 \
        --endpoints http://VM_A:8001/predict,http://VM_B:8001/predict

    # Same run plus a memory/outbox sample every 10 s
    python live_run.py --duration 1800 --fps 1 --soak ...

Only the endpoints and broker are configuration; the API token is read from
``API_TOKEN`` (never printed).
"""

from __future__ import annotations

import argparse
import csv
import ctypes
import math
import os
import platform
import socket
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import cv2
import paho.mqtt.client as mqtt

from phase1_pipeline.capture import CaptureError, encode_jpeg, list_sample_images, load_sample_frame
from phase2_reliability.config import Settings, load_settings
from phase2_reliability.outbox import Outbox
from phase2_reliability.publisher import Publisher
from phase2_reliability.worker import Worker
from phase3_router import HTTPProvider, Router
from phase3_router.providers.base import ProviderResult
from phase4_conflict.store import ConflictStore, DecisionGate

PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_RESULTS_PATH = PROJECT_ROOT / "docs" / "live_results.md"
DEFAULT_RUN_ROOT = PROJECT_ROOT / "logs" / "live"


# ---------------------------------------------------------------------------
# Small utilities
# ---------------------------------------------------------------------------

def utc_now_iso() -> str:
    """Return the current UTC time with millisecond precision."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def redact_endpoint(endpoint: str) -> str:
    """Drop userinfo, query and fragment so credentials never reach a report."""
    from urllib.parse import urlsplit, urlunsplit

    parts = urlsplit(endpoint)
    netloc = parts.netloc.rsplit("@", 1)[-1]
    return urlunsplit((parts.scheme, netloc, parts.path, "", ""))


def display_path(path: Path) -> str:
    """Return a project-relative path when possible, else the absolute path."""
    try:
        return str(path.resolve().relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path.resolve())


def percentile(values: list[float], p: float) -> float | None:
    """Linear-interpolation percentile (same convention as numpy's default)."""
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (len(ordered) - 1) * (p / 100.0)
    lower = math.floor(rank)
    upper = math.ceil(rank)
    if lower == upper:
        return ordered[lower]
    weight = rank - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def fmt(value: float | int | None, digits: int = 1, suffix: str = "") -> str:
    """Format a measured value, or say it was not available."""
    if value is None:
        return "not available"
    return f"{value:.{digits}f}{suffix}"


def rss_mb() -> float | None:
    """Return this process's resident set size in MiB, or None if unknown."""
    try:
        import psutil  # type: ignore

        return round(psutil.Process().memory_info().rss / (1024 * 1024), 1)
    except Exception:
        pass

    status = Path("/proc/self/status")
    if status.is_file():
        try:
            for line in status.read_text(encoding="utf-8").splitlines():
                if line.startswith("VmRSS:"):
                    return round(int(line.split()[1]) / 1024.0, 1)
        except OSError:
            pass

    if os.name == "nt":
        try:
            class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
                _fields_ = [
                    ("cb", ctypes.c_ulong),
                    ("PageFaultCount", ctypes.c_ulong),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t),
                ]

            counters = PROCESS_MEMORY_COUNTERS()
            counters.cb = ctypes.sizeof(PROCESS_MEMORY_COUNTERS)
            process = ctypes.windll.kernel32.GetCurrentProcess()
            ok = ctypes.windll.psapi.GetProcessMemoryInfo(
                process, ctypes.byref(counters), counters.cb
            )
            if ok:
                return round(counters.WorkingSetSize / (1024 * 1024), 1)
        except Exception:
            pass
    return None


def check_endpoint_health(endpoint: str, timeout: float = 3.0) -> str:
    """GET the endpoint's /health once and return a short, secret-free status."""
    if not endpoint.rstrip("/").endswith("/predict"):
        return "not checked (URL does not end in /predict)"
    base = endpoint.rsplit("/predict", 1)[0]
    try:
        with urllib.request.urlopen(f"{base}/health", timeout=timeout) as response:
            return f"HTTP {response.status}"
    except urllib.error.HTTPError as exc:
        return f"HTTP {exc.code}"
    except Exception as exc:  # noqa: BLE001 - report the class only
        return f"error: {exc.__class__.__name__}"


# ---------------------------------------------------------------------------
# Measurement collectors
# ---------------------------------------------------------------------------

@dataclass
class ProviderCall:
    """One provider.predict invocation, used to reconstruct failover events."""

    task_id: str
    provider: str
    started_perf: float
    started_wall: float
    finished_perf: float
    ok: bool
    error: str | None


class RecordingProvider:
    """Transparent provider proxy that records call start/end times."""

    def __init__(self, inner: Any, calls: list[ProviderCall], lock: threading.Lock):
        self._inner = inner
        self._calls = calls
        self._lock = lock

    @property
    def name(self) -> str:
        return str(self._inner.name)

    @property
    def endpoint(self) -> str:
        return str(self._inner.endpoint)

    def health(self) -> bool:
        return bool(self._inner.health())

    def predict(self, jpeg_bytes: bytes, task_context: Any = None,
                timeout: float | None = None) -> ProviderResult:
        task_id = ""
        if isinstance(task_context, dict):
            task_id = str(task_context.get("task_id") or "")
        elif task_context is not None:
            task_id = str(getattr(task_context, "task_id", "") or "")

        started_perf = time.perf_counter()
        started_wall = time.time()
        ok = False
        error: str | None = None
        try:
            result = self._inner.predict(jpeg_bytes, task_context=task_context, timeout=timeout)
            ok = bool(result.ok)
            error = result.error
            return result
        except Exception as exc:  # noqa: BLE001 - class name only, never the token
            error = exc.__class__.__name__
            raise
        finally:
            call = ProviderCall(
                task_id=task_id,
                provider=str(self._inner.name),
                started_perf=started_perf,
                started_wall=started_wall,
                finished_perf=time.perf_counter(),
                ok=ok,
                error=error,
            )
            with self._lock:
                self._calls.append(call)


class RecordingGate:
    """Proxy around the robot decision gate that records which tasks applied."""

    def __init__(self, gate: DecisionGate):
        self._gate = gate
        self._lock = threading.Lock()
        self.applied: dict[str, dict[str, Any]] = {}

    def try_apply(self, task_id: str, decision: str, frame_timestamp: str | float = "",
                  deadline: float = 0.0, now: float | None = None) -> Any:
        result = self._gate.try_apply(task_id, decision, frame_timestamp, deadline, now=now)
        if getattr(result, "applied", False):
            with self._lock:
                self.applied[task_id] = {
                    "decision": decision,
                    "at": time.perf_counter(),
                }
        return result

    def __getattr__(self, name: str) -> Any:
        return getattr(self._gate, name)


class ResultCollector:
    """Subscribe to ``robot/results`` on the edge and log each arrival."""

    def __init__(self, settings: Settings, captures: dict[str, dict[str, Any]],
                 captures_lock: threading.Lock, applied: dict[str, dict[str, Any]],
                 csv_path: Path):
        self.settings = settings
        self._captures = captures
        self._captures_lock = captures_lock
        self._applied = applied
        self.csv_path = csv_path
        self.results: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._connected = threading.Event()
        self._csv_file = None
        self._writer: csv.writer | None = None
        client_kwargs: dict[str, Any] = {
            "client_id": f"live-results-{os.getpid()}",
            "protocol": mqtt.MQTTv311,
        }
        if hasattr(mqtt, "CallbackAPIVersion"):
            # paho-mqtt 2.x otherwise emits a deprecation warning for the v1 API.
            client_kwargs["callback_api_version"] = mqtt.CallbackAPIVersion.VERSION1
        try:
            self.client = mqtt.Client(**client_kwargs)
        except Exception:
            self.client = mqtt.Client(client_id=f"live-results-{os.getpid()}")
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message

    @property
    def count(self) -> int:
        with self._lock:
            return len(self.results)

    def start(self) -> None:
        self.csv_path.parent.mkdir(parents=True, exist_ok=True)
        self._csv_file = self.csv_path.open("w", newline="", encoding="utf-8")
        self._writer = csv.writer(self._csv_file)
        self._writer.writerow([
            "task_id", "captured_at_utc", "captured_epoch_s", "result_at_utc",
            "edge_latency_ms", "decision", "provider", "router_latency_ms",
            "attempts", "applied",
        ])
        self._csv_file.flush()
        self.client.connect(self.settings.broker_host, self.settings.broker_port, 60)
        self.client.loop_start()
        if not self._connected.wait(timeout=5):
            raise RuntimeError("Could not subscribe to result topic within 5 s")

    def stop(self) -> None:
        try:
            self.client.loop_stop()
            self.client.disconnect()
        except Exception:
            pass
        if self._csv_file is not None:
            try:
                self._csv_file.close()
            except Exception:
                pass

    def _on_connect(self, client, userdata, flags, rc):
        if rc == 0:
            # Subscribe here as well as at startup so a reconnect keeps working.
            client.subscribe(self.settings.topic_result, qos=1)
            self._connected.set()

    def _on_message(self, client, userdata, msg):
        received_perf = time.perf_counter()
        received_at = utc_now_iso()
        try:
            import json

            payload = json.loads(msg.payload.decode("utf-8"))
        except Exception:
            return
        task_id = str(payload.get("task_id") or payload.get("message_id") or "")
        if not task_id:
            return
        decision = str(payload.get("decision") or payload.get("label") or "")
        provider = str(payload.get("provider") or "")
        router_latency = payload.get("latency_ms")
        attempts = payload.get("attempts")

        with self._captures_lock:
            capture = dict(self._captures.get(task_id) or {})
        edge_ms = None
        if capture.get("perf") is not None:
            edge_ms = round((received_perf - float(capture["perf"])) * 1000.0, 3)

        with self._lock:
            self.results[task_id] = {
                "task_id": task_id,
                "edge_ms": edge_ms,
                "decision": decision,
                "provider": provider,
                "router_latency_ms": router_latency,
                "attempts": attempts,
                "received_at": received_at,
            }
        if self._writer is not None and self._csv_file is not None:
            self._writer.writerow([
                task_id,
                capture.get("captured_at", ""),
                f"{capture['wall']:.3f}" if capture.get("wall") is not None else "",
                received_at,
                f"{edge_ms:.3f}" if edge_ms is not None else "",
                decision,
                provider,
                router_latency if router_latency is not None else "",
                attempts if attempts is not None else "",
                "yes" if task_id in self._applied else "no",
            ])
            self._csv_file.flush()

    def snapshot(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            return dict(self.results)


class RuntimeMonitor:
    """Sample outbox depth every second; optionally write a soak CSV every 10 s."""

    def __init__(self, outbox: Outbox, started_perf: float, stop_event: threading.Event,
                 soak_path: Path | None = None, soak_interval: float = 10.0):
        self.outbox = outbox
        self.started_perf = started_perf
        self.stop_event = stop_event
        self.soak_path = soak_path
        self.soak_interval = float(soak_interval)
        self.max_depth = 0
        self.depth_samples = 0
        self.soak_rows = 0
        self.peak_rss_mb: float | None = None
        self._thread = threading.Thread(target=self._loop, name="live-monitor", daemon=True)
        self._soak_file = None
        self._writer: csv.writer | None = None

    def start(self) -> None:
        if self.soak_path is not None:
            self.soak_path.parent.mkdir(parents=True, exist_ok=True)
            self._soak_file = self.soak_path.open("w", newline="", encoding="utf-8")
            self._writer = csv.writer(self._soak_file)
            self._writer.writerow([
                "timestamp_utc", "elapsed_s", "rss_mb", "outbox_depth",
                "pending", "sent", "acked",
            ])
            self._soak_file.flush()
        self._thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        self._thread.join(timeout=5)
        # Always leave a final sample so a run shorter than the soak interval
        # still records memory and outbox depth at the end.
        if self._writer is not None:
            self._write_soak(time.perf_counter() - self.started_perf)
        if self._soak_file is not None:
            try:
                self._soak_file.close()
            except Exception:
                pass

    def _counts(self) -> dict[str, int]:
        try:
            return self.outbox.count_by_state()
        except Exception:
            return {}

    def _write_soak(self, elapsed: float) -> None:
        counts = self._counts()
        depth = int(counts.get("pending", 0)) + int(counts.get("sent", 0))
        memory = rss_mb()
        if memory is not None:
            self.peak_rss_mb = memory if self.peak_rss_mb is None else max(self.peak_rss_mb, memory)
        assert self._writer is not None
        self._writer.writerow([
            utc_now_iso(), f"{elapsed:.1f}",
            f"{memory:.1f}" if memory is not None else "",
            depth,
            counts.get("pending", 0), counts.get("sent", 0), counts.get("acked", 0),
        ])
        if self._soak_file is not None:
            self._soak_file.flush()
        self.soak_rows += 1

    def _loop(self) -> None:
        next_soak = 0.0
        while not self.stop_event.is_set():
            elapsed = time.perf_counter() - self.started_perf
            counts = self._counts()
            depth = int(counts.get("pending", 0)) + int(counts.get("sent", 0))
            self.max_depth = max(self.max_depth, depth)
            self.depth_samples += 1

            if self._writer is not None and elapsed >= next_soak:
                self._write_soak(elapsed)
                next_soak += self.soak_interval

            self.stop_event.wait(1.0)


# ---------------------------------------------------------------------------
# Frame sources
# ---------------------------------------------------------------------------

class CameraSource:
    """A persistent webcam capture at the requested resolution."""

    def __init__(self, index: int, width: int | None, height: int | None, quality: int):
        self.index = index
        self.quality = quality
        capture = cv2.VideoCapture(index)
        if not capture.isOpened() and os.name == "nt":
            capture.release()
            capture = cv2.VideoCapture(index, cv2.CAP_DSHOW)
        if not capture.isOpened():
            capture.release()
            raise CaptureError(
                f"Camera {index} could not be opened. Check the index/permissions, "
                "or use --source images for a plumbing smoke test (not a live run)."
            )
        if width:
            capture.set(cv2.CAP_PROP_FRAME_WIDTH, float(width))
        if height:
            capture.set(cv2.CAP_PROP_FRAME_HEIGHT, float(height))
        self.capture = capture
        self.name = f"camera:{index}"
        for _ in range(5):  # discard warm-up frames
            self.capture.read()

    def read(self) -> tuple[bytes, int, int]:
        ok, image = self.capture.read()
        if not ok or image is None:
            raise CaptureError(f"Camera {self.index} stopped returning frames")
        height, width = image.shape[:2]
        return encode_jpeg(image, self.quality), int(width), int(height)

    def close(self) -> None:
        try:
            self.capture.release()
        except Exception:
            pass


class ImageSource:
    """Replay bundled images. Explicitly a smoke-test source, not a webcam."""

    def __init__(self, images_dir: Path):
        self.paths = list_sample_images(images_dir)
        if not self.paths:
            raise CaptureError(f"No sample images in {images_dir}")
        self.cursor = 0
        self.name = f"images:{images_dir}"

    def read(self) -> tuple[bytes, int, int]:
        frame = load_sample_frame(self.paths[self.cursor % len(self.paths)])
        self.cursor += 1
        return frame.jpeg_bytes, frame.width, frame.height

    def close(self) -> None:
        return None


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def build_failover_events(calls: list[ProviderCall]) -> tuple[list[dict[str, Any]], int]:
    """Turn per-task provider calls into failover events and exhausted-task count."""
    by_task: dict[str, list[ProviderCall]] = {}
    for call in calls:
        by_task.setdefault(call.task_id, []).append(call)

    events: list[dict[str, Any]] = []
    exhausted = 0
    for task_id, task_calls in by_task.items():
        ordered = sorted(task_calls, key=lambda c: c.started_perf)
        for attempt, (previous, current) in enumerate(zip(ordered, ordered[1:]), start=1):
            if not previous.ok:
                events.append({
                    "task_id": task_id,
                    "at": datetime.fromtimestamp(previous.started_wall, timezone.utc)
                    .isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                    "epoch": previous.started_wall,
                    "from": previous.provider,
                    "to": current.provider,
                    "gap_ms": round(max(0.0, (current.started_perf - previous.finished_perf) * 1000.0), 3),
                    "attempt": attempt + 1,
                    "error": previous.error or "failed",
                })
        if ordered and not ordered[-1].ok:
            exhausted += 1
    return events, exhausted


def write_results_doc(path: Path, data: dict[str, Any]) -> None:
    """Render ``docs/live_results.md`` from measured values only."""
    path.parent.mkdir(parents=True, exist_ok=True)
    env = data["environment"]
    summary = data["summary"]
    latency = data["latency"]
    events = data["failover_events"]
    lines: list[str] = []
    add = lines.append

    add("# Live run results — Phase 5a")
    add("")
    add(f"- **Run ID:** `{data['run_id']}`")
    if data.get("label"):
        add(f"- **Label:** {data['label']}")
    add(f"- **Started (UTC):** {data['started_at']}")
    add(f"- **Ended (UTC):** {data['ended_at']}")
    add(f"- **Interrupted:** {'yes' if data.get('interrupted') else 'no'}")
    add("")
    add("> Generated by `live_run.py` from runtime observations. Missing values are")
    add("> reported as \"not available\"; nothing here is estimated or fabricated.")
    add("")

    if data.get("source_mode") == "images":
        add("> **Smoke test:** frames were replayed from bundled images, not a webcam.")
        add("> These numbers verify plumbing only and are not a live Phase 5a result.")
        add("")

    add("## Traffic")
    add("")
    add("| Metric | Value |")
    add("|---|---|")
    add(f"| Requested duration | {fmt(data['requested_duration'], 1, ' s')} |")
    add(f"| Actual duration | {fmt(data['actual_duration'], 1, ' s')} |")
    add(f"| Requested frame rate | {fmt(data['requested_fps'], 2, ' fps')} |")
    add(f"| Frames sent (enqueued) | {summary['sent']} |")
    add(f"| Frames acked | {summary['acked']} |")
    add(f"| Frames applied by gate | {summary['applied']} |")
    add(f"| Safe defaults | {summary['safe_default']} |")
    add(f"| Lost / unacked after grace | {summary['lost']} |")
    add(f"| Still pending after grace | {summary['pending']} |")
    add(f"| Duplicate deliveries discarded | {summary['duplicates']} |")
    add(f"| Publisher resends | {summary['resends']} |")
    add(f"| Result messages received | {summary['results']} |")
    add(f"| Achieved capture rate | {fmt(data['achieved_fps'], 2, ' fps')} |")
    add(f"| Achieved applied rate | {fmt(data['applied_fps'], 2, ' fps')} |")
    add(f"| Max outbox depth (1 s samples) | {data['max_depth']} |")
    add("")
    add("`lost` means sent minus acked after the drain grace period; those frames are")
    add("still in the durable outbox, not silently deleted.")
    add("")

    add("## Edge-clock latency (capture → result received)")
    add("")
    add("| Percentile | Latency |")
    add("|---|---|")
    add(f"| p50 | {fmt(latency['p50'], 1, ' ms')} |")
    add(f"| p95 | {fmt(latency['p95'], 1, ' ms')} |")
    add(f"| p99 | {fmt(latency['p99'], 1, ' ms')} |")
    add(f"| min | {fmt(latency['min'], 1, ' ms')} |")
    add(f"| max | {fmt(latency['max'], 1, ' ms')} |")
    add(f"| samples | {latency['count']} |")
    add("")
    add("Latency is `perf_counter` at frame capture to `perf_counter` when the result")
    add("arrived on the edge's result subscription. Frames without a result are not")
    add("included in the percentiles.")
    add("")

    add("## Failover events")
    add("")
    if events:
        add("| At (UTC) | Task | From | To | Gap | Attempt | Error |")
        add("|---|---|---|---|---|---|---|")
        for event in events[:100]:
            add(
                f"| {event['at']} | `{event['task_id'][:8]}…` | {event['from']} | {event['to']} | "
                f"{fmt(event['gap_ms'], 1, ' ms')} | {event['attempt']} | {event['error']} |"
            )
        if len(events) > 100:
            add("")
            add(f"_{len(events) - 100} further events omitted from the table._")
    else:
        add("No provider failover occurred during this run.")
    add("")
    add(f"Tasks with all providers failing: {data['exhausted_tasks']}.")
    add("")

    add("## Environment")
    add("")
    add("| Item | Value |")
    add("|---|---|")
    for key, value in env.items():
        add(f"| {key} | {value} |")
    add("")

    add("## Soak")
    add("")
    if data.get("soak_csv"):
        add(f"- CSV: `{data['soak_csv']}`")
        add(f"- Samples: {data['soak_rows']} (one every 10 s)")
        add(f"- Peak RSS: {fmt(data['peak_rss_mb'], 1, ' MiB')}")
    else:
        add("Not enabled for this run (`--soak` turns on the 10 s memory/outbox CSV).")
    add("")
    add(f"Per-frame latency rows: `{data['latency_csv']}`")
    add("")

    add("## Notes and limitations")
    add("")
    add("- The worker and provider calls ran in this process on the edge machine;")
    add("  provider endpoints were reached over the network.")
    add("- CPU inference latency depends on the VM size and region; compare runs only")
    add("  on identical hardware.")
    add("- Traffic in this phase is plain HTTP with an `X-API-Token` header; use")
    add("  non-sensitive test images only.")
    add("")
    path.write_text("\n".join(lines), encoding="utf-8")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Phase 5a live run: webcam -> outbox -> MQTT -> worker -> providers",
    )
    parser.add_argument("--duration", type=float, default=60.0, help="Run length in seconds (default 60)")
    parser.add_argument("--fps", type=float, default=2.0, help="Capture rate (default 2)")
    parser.add_argument("--camera", type=int, default=0, help="Webcam index (default 0)")
    parser.add_argument("--source", choices=("camera", "images"), default="camera",
                        help="camera (default) or images for a plumbing smoke test")
    parser.add_argument("--images-dir", default=str(PROJECT_ROOT / "images"),
                        help="Images used by --source images")
    parser.add_argument("--width", type=int, default=None, help="Requested camera width")
    parser.add_argument("--height", type=int, default=None, help="Requested camera height")
    parser.add_argument("--quality", type=int, default=90, help="JPEG quality (default 90)")
    parser.add_argument("--deadline-ms", type=float, default=8000.0,
                        help="Per-frame inference deadline from capture (default 8000 ms)")
    parser.add_argument("--grace", type=float, default=15.0,
                        help="Seconds to wait for in-flight frames after capture (default 15)")
    parser.add_argument("--broker", default=None, help="host:port (default from MQTT_BROKER_*)")
    parser.add_argument("--endpoints", default=None,
                        help="Comma-separated provider /predict URLs (default ROUTER_ENDPOINTS)")
    parser.add_argument("--out", default=str(DEFAULT_RESULTS_PATH), help="Results Markdown path")
    parser.add_argument("--run-dir", default=str(DEFAULT_RUN_ROOT), help="Root for run databases/CSVs")
    parser.add_argument("--soak", action="store_true", help="Write memory/outbox samples every 10 s")
    parser.add_argument("--label", default="", help="Free-form label for this run")
    parser.add_argument("--network", default="", help="Network description for the report")
    parser.add_argument("--vm-type", default="", help="Provider VM type(s) for the report")
    parser.add_argument("--region", default="", help="Provider region(s) for the report")
    parser.add_argument("--device", default="", help="Edge device description for the report")
    parser.add_argument("--verbose", action="store_true", help="Show pipeline INFO logs")
    args = parser.parse_args(argv)

    if args.duration <= 0:
        parser.error("--duration must be positive")
    if args.fps <= 0:
        parser.error("--fps must be positive")
    if not 1 <= args.quality <= 100:
        parser.error("--quality must be between 1 and 100")
    if args.deadline_ms <= 0:
        parser.error("--deadline-ms must be positive")
    if args.grace < 0:
        parser.error("--grace cannot be negative")
    return args


def resolve_endpoints(value: str | None) -> list[str]:
    raw = value if value is not None else os.getenv("ROUTER_ENDPOINTS", "")
    return [item.strip() for item in raw.split(",") if item.strip()]


def resolve_broker(value: str | None) -> tuple[str, int]:
    if value:
        host, _, port = value.partition(":")
        return host or "127.0.0.1", int(port or "1883")
    return (
        os.getenv("MQTT_BROKER_HOST", "127.0.0.1").strip() or "127.0.0.1",
        int(os.getenv("MQTT_BROKER_PORT", "1883")),
    )


def main(argv: list[str] | None = None) -> int:
    import logging

    args = parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    endpoints = resolve_endpoints(args.endpoints)
    if not endpoints:
        print("error: no provider endpoints. Pass --endpoints or set ROUTER_ENDPOINTS.", file=sys.stderr)
        return 2

    broker_host, broker_port = resolve_broker(args.broker)
    try:
        with socket.create_connection((broker_host, broker_port), timeout=3):
            pass
    except OSError as exc:
        print(f"error: MQTT broker {broker_host}:{broker_port} is not reachable ({exc.__class__.__name__}).",
              file=sys.stderr)
        print("       Start it first, e.g. `docker compose up -d` in robotic-hand/.", file=sys.stderr)
        return 2

    if not os.getenv("API_TOKEN", "").strip():
        print("warning: API_TOKEN is not set; real_server will reject /predict with 503.", file=sys.stderr)

    try:
        source: CameraSource | ImageSource
        if args.source == "camera":
            source = CameraSource(args.camera, args.width, args.height, args.quality)
        else:
            source = ImageSource(Path(args.images_dir))
    except CaptureError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    if args.label:
        slug = "".join(c if c.isalnum() or c in "-_" else "-" for c in args.label)[:40]
        run_id = f"{run_id}_{slug}"
    run_dir = Path(args.run_dir) / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    settings = replace(
        load_settings(),
        broker_host=broker_host,
        broker_port=broker_port,
        outbox_db=run_dir / "outbox.db",
        processed_db=run_dir / "processed.db",
    )
    outbox = Outbox(settings.outbox_db)
    conflict_store = ConflictStore(run_dir / "conflict.db")
    gate = DecisionGate(run_dir / "conflict.db")
    recording_gate = RecordingGate(gate)

    calls: list[ProviderCall] = []
    calls_lock = threading.Lock()
    providers = [
        RecordingProvider(
            HTTPProvider(f"provider-{index}", endpoint,
                         api_token=os.getenv("API_TOKEN", "").strip()),
            calls,
            calls_lock,
        )
        for index, endpoint in enumerate(endpoints)
    ]
    router = Router(
        providers,
        recheck_interval_sec=float(os.getenv("ROUTER_RECHECK_INTERVAL_SEC", "5.0")),
        conflict_store=conflict_store,
    )
    worker = Worker(
        settings,
        router=router,
        conflict_store=conflict_store,
        decision_gate=recording_gate,
    )
    captures: dict[str, dict[str, Any]] = {}
    captures_lock = threading.Lock()
    collector = ResultCollector(
        settings, captures, captures_lock, recording_gate.applied,
        run_dir / "latency.csv",
    )
    publisher = Publisher(settings, outbox=outbox)
    stop_monitor = threading.Event()
    monitor = RuntimeMonitor(
        outbox,
        time.perf_counter(),
        stop_monitor,
        soak_path=(run_dir / "soak.csv") if args.soak else None,
    )

    started_at = utc_now_iso()
    run_start_perf = time.perf_counter()
    run_start_wall = time.time()
    capture_end_wall: float | None = None
    sent = 0
    first_frame_size: tuple[int, int] | None = None
    interrupted = False
    health_start: dict[str, str] = {}
    health_end: dict[str, str] = {}

    monitor.started_perf = run_start_perf
    monitor.start()
    try:
        worker.start()
        publisher.start()
        collector.start()
        health_start = {redact_endpoint(ep): check_endpoint_health(ep) for ep in endpoints}

        # Start the clock after setup/health probes so measured duration is the
        # actual capture window.
        run_start_perf = time.perf_counter()
        run_start_wall = time.time()
        monitor.started_perf = run_start_perf

        interval = 1.0 / args.fps
        next_at = run_start_perf
        last_progress = run_start_perf
        print(f"live run {run_id}: {args.duration:.0f}s at {args.fps:g} fps, "
              f"{len(endpoints)} provider(s), broker {broker_host}:{broker_port}")
        while True:
            now = time.perf_counter()
            if now - run_start_perf >= args.duration:
                break
            if now < next_at:
                time.sleep(min(next_at - now, 0.05))
                continue

            jpeg, width, height = source.read()
            captured_perf = time.perf_counter()
            captured_wall = time.time()
            if first_frame_size is None:
                first_frame_size = (width, height)
            deadline = captured_wall + (args.deadline_ms / 1000.0)
            message_id = publisher.send_frame(
                jpeg,
                frame_timestamp=f"{captured_wall:.6f}",
                deadline=deadline,
            )
            with captures_lock:
                captures[message_id] = {
                    "perf": captured_perf,
                    "wall": captured_wall,
                    "captured_at": utc_now_iso(),
                    "size": (width, height),
                }
            sent += 1

            next_at += interval
            if next_at < now - interval:
                next_at = now  # never burst to catch up

            if now - last_progress >= 5.0:
                counts = outbox.count_by_state()
                depth = int(counts.get("pending", 0)) + int(counts.get("sent", 0))
                print(f"  t+{now - run_start_perf:6.1f}s  sent={sent} "
                      f"acked={counts.get('acked', 0)} applied={len(recording_gate.applied)} "
                      f"depth={depth}")
                last_progress = now
    except KeyboardInterrupt:
        interrupted = True
        print("\ninterrupted; draining in-flight frames before reporting", file=sys.stderr)
    except CaptureError as exc:
        print(f"error: capture failed: {exc}", file=sys.stderr)
        interrupted = True
    finally:
        capture_end_wall = time.time()
        grace_deadline = time.perf_counter() + args.grace
        while time.perf_counter() < grace_deadline:
            counts = outbox.count_by_state()
            depth = int(counts.get("pending", 0)) + int(counts.get("sent", 0))
            if depth == 0 and collector.count >= sent:
                break
            time.sleep(0.2)

        counts = outbox.count_by_state()
        results = collector.snapshot()
        health_end = {redact_endpoint(ep): check_endpoint_health(ep) for ep in endpoints}
        monitor.stop()
        collector.stop()
        worker.stop()
        publisher.stop()
        source.close()
        with calls_lock:
            calls_snapshot = list(calls)
        try:
            with sqlite3.connect(str(settings.outbox_db), timeout=10) as conn:
                row = conn.execute("SELECT COALESCE(SUM(retry_count), 0) FROM outbox").fetchone()
                resends = int(row[0] or 0)
        except Exception:
            resends = 0
        discard_counts = conflict_store.discard_counts()

    ended_at = utc_now_iso()
    actual_duration = max(0.0, capture_end_wall - run_start_wall) if capture_end_wall else 0.0
    applied_ids = list(recording_gate.applied)
    latencies = [
        float(result["edge_ms"])
        for result in results.values()
        if result.get("edge_ms") is not None
    ]
    safe_default = sum(1 for r in results.values() if r["decision"] == "safe_default")
    failover_events, exhausted = build_failover_events(calls_snapshot)

    summary = {
        "sent": sent,
        "acked": int(counts.get("acked", 0)),
        "applied": len(applied_ids),
        "safe_default": safe_default,
        "lost": max(0, sent - int(counts.get("acked", 0))),
        "pending": int(counts.get("pending", 0)) + int(counts.get("sent", 0)),
        "duplicates": int(discard_counts.get("duplicate", 0)),
        "resends": resends,
        "results": len(results),
    }
    environment = {
        "Source": ("webcam index " + str(args.camera)) if args.source == "camera"
                  else f"bundled images ({args.images_dir}) — smoke test",
        "Frame size": f"{first_frame_size[0]}x{first_frame_size[1]}" if first_frame_size else "not available",
        "JPEG quality": str(args.quality),
        "Per-frame deadline": f"{args.deadline_ms:.0f} ms",
        "Broker": f"{broker_host}:{broker_port} (MQTT QoS 1)",
        "Provider endpoints": ", ".join(redact_endpoint(ep) for ep in endpoints),
        "Provider health at start": "; ".join(f"{k}: {v}" for k, v in health_start.items()) or "not checked",
        "Provider health at end": "; ".join(f"{k}: {v}" for k, v in health_end.items()) or "not checked",
        "API token configured": "yes" if os.getenv("API_TOKEN", "").strip() else "no",
        "Edge host": socket.gethostname(),
        "Edge platform": platform.platform(),
        "Python": platform.python_version(),
        "CPU count": str(os.cpu_count() or "not available"),
        "Edge device (reported)": args.device or "not recorded",
        "Network (reported)": args.network or "not recorded",
        "Provider VM type (reported)": args.vm_type or "not recorded",
        "Provider region (reported)": args.region or "not recorded",
        "Machine architecture": platform.machine() or "not available",
    }

    duration = actual_duration or 1.0
    data = {
        "run_id": run_id,
        "label": args.label,
        "started_at": started_at,
        "ended_at": ended_at,
        "interrupted": interrupted,
        "source_mode": args.source,
        "requested_duration": args.duration,
        "actual_duration": actual_duration,
        "requested_fps": args.fps,
        "achieved_fps": sent / duration,
        "applied_fps": len(applied_ids) / duration,
        "summary": summary,
        "latency": {
            "p50": percentile(latencies, 50),
            "p95": percentile(latencies, 95),
            "p99": percentile(latencies, 99),
            "min": min(latencies) if latencies else None,
            "max": max(latencies) if latencies else None,
            "count": len(latencies),
        },
        "max_depth": monitor.max_depth,
        "exhausted_tasks": exhausted,
        "failover_events": failover_events,
        "environment": environment,
        "soak_csv": display_path(run_dir / "soak.csv") if args.soak else None,
        "soak_rows": monitor.soak_rows,
        "peak_rss_mb": monitor.peak_rss_mb,
        "latency_csv": display_path(run_dir / "latency.csv"),
    }
    write_results_doc(Path(args.out), data)

    print(f"\nrun complete: sent={summary['sent']} acked={summary['acked']} "
          f"applied={summary['applied']} safe_default={summary['safe_default']} "
          f"lost={summary['lost']} p50={fmt(data['latency']['p50'], 1, ' ms')}")
    print(f"report: {Path(args.out)}")
    print(f"run artifacts: {run_dir}")
    return 130 if interrupted else 0


if __name__ == "__main__":
    sys.exit(main())
