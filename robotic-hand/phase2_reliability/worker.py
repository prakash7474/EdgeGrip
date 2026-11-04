"""Cloud worker: subscribes to frames, deduplicates by message_id in processed_ids,
skips duplicates but re-sends ACK, uses Router for multi-provider inference, publishes result and ACK."""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from pathlib import Path

import paho.mqtt.client as mqtt

from phase1_pipeline.client import CloudClient
from phase2_reliability.common import Message, base64_to_jpeg
from phase2_reliability.config import Settings, load_settings
from phase3_router import Router, HTTPProvider

LOG = logging.getLogger("phase2.worker")

PROCESSED_SCHEMA = """
CREATE TABLE IF NOT EXISTS processed_ids (
  message_id TEXT PRIMARY KEY,
  processed_at TEXT NOT NULL,
  result_json TEXT
);
"""


class ProcessedDB:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def _connect(self):
        conn = sqlite3.connect(str(self.db_path), timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    def _init(self):
        with self._connect() as conn:
            conn.executescript(PROCESSED_SCHEMA)
            conn.commit()

    def is_processed(self, message_id: str) -> bool:
        with self._connect() as conn:
            cur = conn.execute("SELECT 1 FROM processed_ids WHERE message_id=?", (message_id,))
            return cur.fetchone() is not None

    def mark_processed(self, message_id: str, result_json: str):
        from phase2_reliability.common import now_iso
        with self._connect() as conn:
            try:
                conn.execute(
                    "INSERT INTO processed_ids (message_id, processed_at, result_json) VALUES (?,?,?)",
                    (message_id, now_iso(), result_json),
                )
                conn.commit()
            except sqlite3.IntegrityError:
                pass


class Worker:
    def __init__(self, settings: Settings | None = None, inference_endpoint: str | None = None,
                 inference_api_key: str = "", router: Router | None = None,
                 provider_endpoints: list[str] | None = None):
        self.settings = settings or load_settings()
        self.processed = ProcessedDB(self.settings.processed_db)
        try:
            self.client = mqtt.Client(client_id=f"worker-{int(time.time())}", protocol=mqtt.MQTTv311)
        except Exception:
            self.client = mqtt.Client(client_id=f"worker-{int(time.time())}")
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message
        self._connected = threading.Event()
        self.inference_endpoint = inference_endpoint
        self.inference_api_key = inference_api_key
        self.cloud_client = None
        self.router = router
        self.provider_endpoints = provider_endpoints

    def _build_router(self):
        if self.router is not None:
            return self.router
        endpoints = self.provider_endpoints
        if not endpoints:
            # try env
            eps = os.getenv("ROUTER_ENDPOINTS")
            if eps:
                endpoints = [e.strip() for e in eps.split(",") if e.strip()]
        if not endpoints and self.inference_endpoint:
            endpoints = [self.inference_endpoint]
        if not endpoints:
            endpoints = ["http://127.0.0.1:8000/predict"]
        providers = [HTTPProvider(f"p{i}", ep, api_key=self.inference_api_key) for i, ep in enumerate(endpoints)]
        from phase3_router.config import get_recheck_interval_sec
        self.router = Router(providers, recheck_interval_sec=get_recheck_interval_sec())
        return self.router

    def start(self):
        self._build_router()
        self.cloud_client = CloudClient(self.inference_endpoint or "http://127.0.0.1:8000/predict", api_key=self.inference_api_key)
        self.client.connect(self.settings.broker_host, self.settings.broker_port, 60)
        self.client.loop_start()
        self._connected.wait(timeout=10)
        self.client.subscribe(self.settings.topic_publish, qos=1)

    def stop(self):
        try:
            self.client.loop_stop()
            self.client.disconnect()
        except Exception:
            pass
        try:
            if self.cloud_client:
                self.cloud_client.close()
        except Exception:
            pass

    def _on_connect(self, client, userdata, flags, rc):
        if rc == 0:
            self._connected.set()
            LOG.info("Worker connected to broker")
        else:
            LOG.warning("Worker connect rc=%s", rc)

    def _on_message(self, client, userdata, msg):
        try:
            payload = msg.payload.decode("utf-8")
            message = Message.from_json(payload)
            # Always re-send ACK if duplicate (as specified)
            self._send_ack(message.message_id)
            if self.processed.is_processed(message.message_id):
                LOG.info("Duplicate %s - skipped processing, ACK resent", message.message_id)
                return
            # Process: use Router
            jpeg = base64_to_jpeg(message.payload)
            router = self._build_router()
            rres = router.predict(jpeg)
            result_dict = {
                "message_id": message.message_id,
                "ok": rres.ok,
                "label": rres.label,
                "provider": rres.provider,
                "latency_ms": rres.latency_ms,
                "attempts": rres.attempts,
            }
            self.processed.mark_processed(message.message_id, json.dumps(result_dict))
            try:
                self.client.publish(self.settings.topic_result, json.dumps(result_dict), qos=1)
            except Exception:
                pass
            LOG.info("Processed %s -> %s via %s", message.message_id, rres.label if rres.ok else "fail", rres.provider)
        except Exception as e:
            LOG.warning("Worker error: %s", e)

    def _send_ack(self, message_id: str):
        try:
            ack = json.dumps({"message_id": message_id})
            self.client.publish(self.settings.topic_ack, ack, qos=1)
        except Exception as e:
            LOG.warning("Failed to send ACK: %s", e)
