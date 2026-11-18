"""Serial actuator speaking the Phase 5b line protocol.

``pyserial`` is an optional dependency, imported only when no transport is
injected. ``SerialActuator(transport=FakeDevice().transport())`` runs the real
framing, ACK matching and timeout logic without any hardware.
"""

from __future__ import annotations

import threading
import time

from actuator.base import ActuationResult, Actuator, new_command_id, utc_now_iso
from actuator.protocol import ProtocolError, format_frame, parse_reply


class SerialActuator(Actuator):
    """Sends newline-terminated command frames over a serial port."""

    name = "serial"

    def __init__(
        self,
        port: str | None = None,
        *,
        baudrate: int = 115200,
        timeout: float = 1.0,
        transport=None,
        heartbeat_interval_s: float = 0.5,
    ):
        self.port = port or "injected-transport"
        self.baudrate = int(baudrate)
        self.timeout = float(timeout)
        self.heartbeat_interval_s = float(heartbeat_interval_s)
        self._lock = threading.Lock()
        self._closed = False
        self._heartbeat_stop = threading.Event()
        self._heartbeat_thread: threading.Thread | None = None
        self.heartbeats_sent = 0
        self.heartbeat_failures = 0

        if transport is not None:
            self._transport = transport
        else:
            try:
                import serial  # optional dependency
            except ImportError as exc:  # pragma: no cover - depends on environment
                raise RuntimeError(
                    "SerialActuator requires pyserial (pip install -r requirements-hardware.txt)"
                ) from exc
            if not port:
                raise ValueError("SerialActuator needs a serial port, e.g. COM5 or /dev/ttyUSB0")
            self._transport = serial.Serial(
                port=port,
                baudrate=self.baudrate,
                timeout=self.timeout,
                write_timeout=self.timeout,
            )

    # ------------------------------------------------------------------ commands
    def grip(self, *, speed: int | None = None,
             command_id: str | None = None) -> ActuationResult:
        parameters = {} if speed is None else {"speed": speed}
        return self._send("grip", parameters, command_id)

    def release(self, *, speed: int | None = None,
                command_id: str | None = None) -> ActuationResult:
        parameters = {} if speed is None else {"speed": speed}
        return self._send("release", parameters, command_id)

    def move_to(self, angle: int, *, speed: int | None = None,
                command_id: str | None = None) -> ActuationResult:
        parameters: dict[str, int] = {"angle": angle}
        if speed is not None:
            parameters["speed"] = speed
        return self._send("move_to", parameters, command_id)

    def stop(self, *, command_id: str | None = None) -> ActuationResult:
        return self._send("stop", {}, command_id)

    def heartbeat(self, *, command_id: str | None = None) -> ActuationResult:
        return self._send("heartbeat", {}, command_id)

    def _send(self, command: str, parameters: dict[str, int],
              command_id: str | None) -> ActuationResult:
        cid = command_id or new_command_id()
        sent_perf = time.perf_counter()
        sent_at = utc_now_iso()
        try:
            frame = format_frame(cid, command, parameters).encode("ascii")
        except ProtocolError as exc:
            # Local validation failure: nothing was written to the device.
            return ActuationResult(
                command_id=cid, action=command, parameters=dict(parameters),
                acked=False, reason=exc.reason, detail=exc.detail,
                sent_at=sent_at, acked_at=None, latency_ms=None, sent_perf=sent_perf,
            )

        with self._lock:
            if self._closed:
                return self._transport_failure(cid, command, parameters, "closed", sent_at, sent_perf)
            try:
                self._transport.write(frame)
            except Exception as exc:  # noqa: BLE001 - class name only
                return self._transport_failure(
                    cid, command, parameters, exc.__class__.__name__, sent_at, sent_perf
                )

            deadline = time.monotonic() + self.timeout
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    raw = self._transport.readline()
                except Exception as exc:  # noqa: BLE001 - class name only
                    return self._transport_failure(
                        cid, command, parameters, exc.__class__.__name__, sent_at, sent_perf
                    )
                if not raw:
                    # pyserial blocks for its timeout; an injected transport returns
                    # immediately, so avoid a busy loop.
                    time.sleep(min(0.002, remaining))
                    continue
                try:
                    reply = parse_reply(raw)
                except ProtocolError:
                    continue  # ignore unsolicited or malformed lines
                if reply.command_id != cid:
                    continue
                replied_perf = time.perf_counter()
                return ActuationResult(
                    command_id=cid,
                    action=command,
                    parameters=dict(parameters),
                    acked=reply.ok,
                    reason=reply.reason,
                    detail=reply.detail,
                    sent_at=sent_at,
                    acked_at=utc_now_iso(),
                    latency_ms=round((replied_perf - sent_perf) * 1000.0, 3),
                    sent_perf=sent_perf,
                    acked_perf=replied_perf,
                )

        # No reply in time. The command may still have reached the device, so this
        # is reported as unacknowledged, never retried automatically.
        return ActuationResult(
            command_id=cid, action=command, parameters=dict(parameters),
            acked=False, reason="timeout", detail=f"no reply in {self.timeout:.2f}s",
            sent_at=sent_at, acked_at=None,
            latency_ms=round((time.perf_counter() - sent_perf) * 1000.0, 3),
            sent_perf=sent_perf,
        )

    def _transport_failure(self, cid: str, command: str, parameters: dict[str, int],
                           detail: str, sent_at: str, sent_perf: float) -> ActuationResult:
        return ActuationResult(
            command_id=cid, action=command, parameters=dict(parameters),
            acked=False, reason="transport_error", detail=detail,
            sent_at=sent_at, acked_at=None,
            latency_ms=round((time.perf_counter() - sent_perf) * 1000.0, 3),
            sent_perf=sent_perf,
        )

    # ------------------------------------------------------------------ heartbeat
    def start_heartbeat(self, interval_s: float | None = None) -> None:
        """Start the background heartbeat loop. Idempotent."""
        if self._closed:
            raise RuntimeError("actuator is closed")
        if self._heartbeat_thread is not None and self._heartbeat_thread.is_alive():
            return
        interval = self.heartbeat_interval_s if interval_s is None else float(interval_s)
        if interval <= 0:
            raise ValueError("heartbeat interval must be positive")
        self._heartbeat_stop.clear()

        def loop() -> None:
            while not self._heartbeat_stop.is_set():
                result = self.heartbeat()
                if result.acked:
                    self.heartbeats_sent += 1
                else:
                    self.heartbeat_failures += 1
                if self._heartbeat_stop.wait(interval):
                    return

        self._heartbeat_thread = threading.Thread(
            target=loop, name="actuator-heartbeat", daemon=True
        )
        self._heartbeat_thread.start()

    def close(self, send_stop: bool = True) -> None:
        if self._closed:
            return
        self._heartbeat_stop.set()
        thread = self._heartbeat_thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        if send_stop:
            try:
                self.stop()
            except Exception:
                pass
        self._closed = True
        try:
            self._transport.close()
        except Exception:
            pass
