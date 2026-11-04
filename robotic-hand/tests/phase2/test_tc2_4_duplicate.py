"""TC2.4 — same message_id sent twice: processed exactly once."""
from __future__ import annotations

import os
import sqlite3
import subprocess
import time
from pathlib import Path

import pytest

from phase2_reliability.config import load_settings
from phase2_reliability.outbox import Outbox
from phase2_reliability.publisher import Publisher
from phase2_reliability.worker import Worker
from phase1_pipeline.capture import FrameGrabber


PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
ROBOTIC_HAND = PROJECT_ROOT / "robotic-hand"


@pytest.mark.skipif(os.environ.get("SKIP_PHASE2_DOCKER_TESTS") == "1", reason="Phase 2 docker tests skipped")
def test_tc2_4_duplicate_processed_once():
    compose_file = ROBOTIC_HAND / "docker-compose.yml"
    try:
        subprocess.run(["docker", "compose", "-f", str(compose_file), "up", "-d"], cwd=str(ROBOTIC_HAND), check=False, timeout=60)
    except Exception:
        pytest.skip("Docker not available")
    time.sleep(6)

    settings = load_settings()
    for db in [settings.outbox_db, settings.processed_db]:
        try:
            if db.exists():
                db.unlink()
        except Exception:
            pass
    outbox = Outbox(settings.outbox_db)
    worker = Worker(settings=settings, inference_endpoint="http://127.0.0.1:8000/predict")
    worker.start()
    time.sleep(2)
    publisher = Publisher(settings=settings, outbox=outbox)
    publisher.start()
    time.sleep(2)

    grabber = FrameGrabber(ROBOTIC_HAND / "images", camera_enabled=False)
    frame = grabber.next_frame()
    msg = outbox.enqueue(frame.jpeg_bytes)
    # publish twice
    publisher._publish_message(msg)
    time.sleep(1)
    publisher._publish_message(msg)
    time.sleep(5)

    # check processed once
    conn = sqlite3.connect(str(settings.processed_db))
    cur = conn.execute("SELECT COUNT(*) FROM processed_ids WHERE message_id=?", (msg.message_id,))
    count = cur.fetchone()[0]
    conn.close()
    assert count == 1

    publisher.stop()
    worker.stop()
    try:
        subprocess.run(["docker", "compose", "-f", str(compose_file), "down"], cwd=str(ROBOTIC_HAND), check=False, timeout=60)
    except Exception:
        pass
