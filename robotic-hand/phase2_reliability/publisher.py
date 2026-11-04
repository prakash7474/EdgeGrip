"""Edge publisher: writes to outbox before send, publishes over MQTT QoS 1,
subscribes to ACKs, resends unacked with exponential backoff."""
from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Callable

import paho.mqtt.client as mqtt

from phase2_reliability.common import Message, new_message_id, prepare_jpeg_under_size
from phase2_reliability.config import Settings, load_settings
from phase2_reliability.outbox import Outbox

LOG = logging.getLogger("phase2.publisher")

BACKOFF_SEQUENCE = [1.0, 2.0, 4.0]
MAX_BACKOFF = 30.0


def compute_backoff(retry_count: int) -> float:
    if retry_count < len(BACKOFF_SEQUENCE):
        return BACKOFF_SEQUENCE[retry_count]
    val = BACKOFF_SEQUENCE[-1] * (2 ** (retry_count - len(BACKOFF_SEQUENCE) + 1))
    return min(val, MAX_BACKOFF)


class Publisher:
    def __init__(self, settings: Settings | None = None, outbox: Outbox | None = None):
        self.settings = settings or load_settings()
        self.outbox = outbox or Outbox(self.settings.outbox_db)
        try:
            self.client = mqtt.Client(client_id=f"edge-{new_message_id()[:8]}", protocol=mqtt.MQTTv311)
        except Exception:
            self.client = mqtt.Client(client_id=f"edge-{new_message_id()[:8]}")
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message
        self._connected = threading.Event()
        self._lock = threading.Lock()
        self._stop = threading.Event()

    def start(self):
        self.client.connect(self.settings.broker_host, self.settings.broker_port, 60)
        self.client.loop_start()
        self._connected.wait(timeout=10)
        self.client.subscribe(self.settings.topic_ack, qos=1)
        # resume sending on startup
        self._drain_loop()

    def stop(self):
        self._stop.set()
        try:
            self.client.loop_stop()
            self.client.disconnect()
        except Exception:
            pass

    def _on_connect(self, client, userdata, flags, rc):
        if rc == 0:
            self._connected.set()
            LOG.info("Edge connected to broker")
        else:
            LOG.warning("Edge connect rc=%s", rc)

    def _on_message(self, client, userdata, msg):
        try:
            payload = msg.payload.decode("utf-8")
            data = json.loads(payload)
            mid = data.get("message_id")
            if mid:
                if self.outbox.mark_acked(mid):
                    LOG.info("ACK received for %s", mid)
        except Exception as e:
            LOG.warning("Bad ACK: %s", e)

    def send_frame(self, jpeg_bytes: bytes) -> str:
        # prepare/compress to under ~100KB
        prepared = prepare_jpeg_under_size(jpeg_bytes, max_kb=100.0)
        msg = self.outbox.enqueue(prepared)
        self._publish_message(msg)
        return msg.message_id

    def _publish_message(self, msg: Message):
        try:
            if not self._connected.is_set():
                # offline - keep in outbox, will drain on reconnect
                return
            self.client.publish(self.settings.topic_publish, msg.to_json(), qos=1)
            self.outbox.mark_sent(msg.message_id)
        except Exception as e:
            LOG.warning("Publish failed for %s: %s", msg.message_id, e)

    def _drain_loop(self):
        # run in background thread logic via simple loop
        def loop():
            while not self._stop.is_set():
                try:
                    now = time.time()
                    for entry in self.outbox.get_unacked_for_resend(now):
                        # compute backoff based on retry_count
                        backoff = compute_backoff(entry.retry_count)
                        # if never sent or past timeout, resend
                        msg = Message(message_id=entry.message_id, created_at=entry.created_at, payload=entry.payload_b64)
                        if not self._connected.is_set():
                            break  # offline, keep writing to outbox
                        self.client.publish(self.settings.topic_publish, msg.to_json(), qos=1)
                        self.outbox.schedule_retry(entry.message_id, entry.retry_count, backoff)
                        LOG.info("Resent %s (retry %d)", entry.message_id, entry.retry_count + 1)
                    time.sleep(0.5)
                except Exception as e:
                    LOG.warning("Drain error: %s", e)
                    time.sleep(1)
        t = threading.Thread(target=loop, daemon=True)
        t.start()
        self._drain_thread = t
