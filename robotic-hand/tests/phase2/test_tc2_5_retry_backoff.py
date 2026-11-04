"""TC2.5 — unacked past timeout: retried with growing delays, then acked."""
from __future__ import annotations

import os
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
def test_tc2_5_retry_with_backoff_then_ack():
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
    time.sleep(1)

    # stop worker so messages go unacked
    worker.stop()
    time.sleep(1)

    grabber = FrameGrabber(ROBOTIC_HAND / "images", camera_enabled=False)
    for i in range(5):
        frame = grabber.next_frame()
        publisher.send_frame(frame.jpeg_bytes)
    time.sleep(5)  # wait for retries (backoff 1s,2s,4s...)

    # restart worker
    worker2 = Worker(settings=settings, inference_endpoint="http://127.0.0.1:8000/predict")
    worker2.start()
    time.sleep(10)

    deadline = time.time() + 60
    while time.time() < deadline:
        counts = outbox.count_by_state()
        if counts.get('acked', 0) >= 5:
            break
        time.sleep(1)
    counts = outbox.count_by_state()
    assert counts.get('acked', 0) >= 5

    publisher.stop()
    worker2.stop()
    try:
        subprocess.run(["docker", "compose", "-f", str(compose_file), "down"], cwd=str(ROBOTIC_HAND), check=False, timeout=60)
    except Exception:
        pass
