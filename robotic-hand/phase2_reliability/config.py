"""Configuration for Phase 2 reliable delivery."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")

DEFAULT_BROKER_HOST = "127.0.0.1"
DEFAULT_BROKER_PORT = 1883
DEFAULT_TOPIC_PUBLISH = "robot/frames"
DEFAULT_TOPIC_ACK = "robot/acks"
DEFAULT_TOPIC_RESULT = "robot/results"

DEFAULT_OUTBOX_DB = PROJECT_ROOT / "phase2_reliability" / "outbox.db"
DEFAULT_PROCESSED_DB = PROJECT_ROOT / "phase2_reliability" / "processed.db"


class ConfigurationError(RuntimeError):
    pass


@dataclass(frozen=True)
class Settings:
    broker_host: str
    broker_port: int
    topic_publish: str
    topic_ack: str
    topic_result: str
    outbox_db: Path
    processed_db: Path
    resend_timeout_sec: float = 5.0  # initial timeout before first retry? spec says "Resend unacked messages after a timeout with exponential backoff (1s, 2s, 4s, capped at 30s)"
    max_backoff_sec: float = 30.0


def load_settings() -> Settings:
    host = os.getenv("MQTT_BROKER_HOST", DEFAULT_BROKER_HOST).strip()
    port = int(os.getenv("MQTT_BROKER_PORT", str(DEFAULT_BROKER_PORT)).strip())
    return Settings(
        broker_host=host,
        broker_port=port,
        topic_publish=os.getenv("MQTT_TOPIC_PUBLISH", DEFAULT_TOPIC_PUBLISH).strip(),
        topic_ack=os.getenv("MQTT_TOPIC_ACK", DEFAULT_TOPIC_ACK).strip(),
        topic_result=os.getenv("MQTT_TOPIC_RESULT", DEFAULT_TOPIC_RESULT).strip(),
        outbox_db=Path(os.getenv("OUTBOX_DB") or DEFAULT_OUTBOX_DB),
        processed_db=Path(os.getenv("PROCESSED_DB") or DEFAULT_PROCESSED_DB),
    )
