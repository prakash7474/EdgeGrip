"""TC2.2 — stop the broker after ~30 messages, restart later: all 100 delivered."""
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
def test_tc2_2_broker_restart():
    compose_file = ROBOTIC_HAND / "docker-compose.yml"
    try:
        subprocess.run(["docker", "compose", "-f", str(compose_file), "up", "-d"], cwd=str(ROBOTIC_HAND), check=False, timeout=60)
    except Exception:
        pytest.skip("Docker not available")
    time.sleep(8)

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
    # send 30
    for i in range(30):
        frame = grabber.next_frame()
        publisher.send_frame(frame.jpeg_bytes)
        time.sleep(0.1)
    # stop broker
    try:
        subprocess.run(["docker", "compose", "-f", str(compose_file), "stop"], cwd=str(ROBOTIC_HAND), check=False, timeout=30)
    except Exception:
        pass
    time.sleep(2)
    # send remaining while down
    for i in range(70):
        frame = grabber.next_frame()
        publisher.send_frame(frame.jpeg_bytes)
        time.sleep(0.05)
    time.sleep(2)
    # restart broker
    try:
        subprocess.run(["docker", "compose", "-f", str(compose_file), "start"], cwd=str(ROBOTIC_HAND), check=False, timeout=60)
    except Exception:
        pass
    time.sleep(10)

    deadline = time.time() + 90
    while time.time() < deadline:
        counts = outbox.count_by_state()
        if counts.get('acked', 0) >= 100:
            break
        time.sleep(1)

    counts = outbox.count_by_state()
    assert counts.get('acked', 0) == 100, f"Not all delivered after restart: {counts}"

    publisher.stop()
    worker.stop()
    try:
        subprocess.run(["docker", "compose", "-f", str(compose_file), "down"], cwd=str(ROBOTIC_HAND), check=False, timeout=60)
    except Exception:
        pass
