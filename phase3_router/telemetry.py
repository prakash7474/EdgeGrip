"""Opt-in, non-blocking router telemetry for the local dashboard.

Enable by setting ``ROUTER_TELEMETRY_PATH`` to a JSONL file. The writer uses a
bounded queue and a daemon thread: a slow or full disk drops telemetry instead
of blocking inference. No token or request body is ever written.

The dashboard reads this file; it is not required by Phase 3 tests.
"""

from __future__ import annotations

import json
import queue
import threading
import time
from pathlib import Path
from typing import Any

_MAX_QUEUE = 1000


class TelemetryWriter:
    """Append structured events to a JSONL file without blocking callers."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._queue: queue.Queue[dict[str, Any] | None] = queue.Queue(maxsize=_MAX_QUEUE)
        self._thread = threading.Thread(
            target=self._run, name="router-telemetry", daemon=True
        )
        self._thread.start()

    def emit(self, kind: str, **fields: Any) -> None:
        """Queue one event. Never raises and never waits for the disk."""
        event = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "epoch": round(time.time(), 6),
            "kind": str(kind),
            **fields,
        }
        try:
            self._queue.put_nowait(event)
        except queue.Full:
            pass

    def close(self, timeout: float = 1.0) -> None:
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            return
        self._thread.join(timeout=timeout)

    def _run(self) -> None:
        try:
            with self.path.open("a", encoding="utf-8") as handle:
                while True:
                    try:
                        event = self._queue.get(timeout=0.5)
                    except queue.Empty:
                        continue
                    if event is None:
                        return
                    try:
                        handle.write(json.dumps(event, separators=(",", ":")) + "\n")
                        handle.flush()
                    except (OSError, TypeError, ValueError):
                        # Telemetry must never take the router down.
                        continue
        except OSError:
            return
