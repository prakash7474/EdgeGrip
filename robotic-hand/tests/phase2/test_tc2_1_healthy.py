"""TC2.1 — 100 messages, healthy network: all acked, 0 lost."""
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
def test_tc2_1_100_messages_all_acked_no_loss():
    # Start broker
    compose_file = ROBOTIC_HAND / "docker-compose.yml"
    # try to start
    try:
        subprocess.run(["docker", "compose", "-f", str(compose_file), "up", "-d"], cwd=str(ROBOTIC_HAND), check=False, timeout=60)
    except Exception:
        pytest.skip("Docker not available")
    time.sleep(8)
    
    settings = load_settings()
    outbox = Outbox(settings.outbox_db)
    # clean dbs
    try:
        if settings.outbox_db.exists():
            settings.outbox_db.unlink()
        if settings.processed_db.exists():
            settings.processed_db.unlink()
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
    for i in range(100):
        frame = grabber.next_frame()
        publisher.send_frame(frame.jpeg_bytes)
    
    # wait for acks
    deadline = time.time() + 60
    while time.time() < deadline:
        counts = outbox.count_by_state()
        if counts.get('acked', 0) >= 100:
            break
        time.sleep(1)
    
    counts = outbox.count_by_state()
    assert counts.get('acked', 0) == 100, f"Not all acked: {counts}"
    
    publisher.stop()
    worker.stop()
    # stop broker
    try:
        subprocess.run(["docker", "compose", "-f", str(compose_file), "down"], cwd=str(ROBOTIC_HAND), check=False, timeout=60)
    except Exception:
        pass
