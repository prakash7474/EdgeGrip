"""Subprocess harness for the edge kill/restart test.

``enqueue``  : enqueue one frame in the real outbox, mark it sent, then exit
               abruptly (disk state is what survives the kill).
``resend``   : restart, read the unacknowledged outbox rows and print the
               message that the edge would republish (oldest first).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from phase2_reliability.common import Message
from phase2_reliability.outbox import Outbox


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["enqueue", "resend"])
    parser.add_argument("--outbox", required=True)
    parser.add_argument("--image", required=False)
    parser.add_argument("--task-id", required=False)
    args = parser.parse_args()

    outbox = Outbox(args.outbox)
    if args.mode == "enqueue":
        jpeg = Path(args.image).read_bytes()
        message = outbox.enqueue(jpeg, message_id=args.task_id, attempt=1,
                                 frame_timestamp="1000.0", deadline=time.time() + 60)
        outbox.mark_sent(message.message_id)
        sys.stdout.write(json.dumps({"enqueued": message.message_id}) + "\n")
        sys.stdout.flush()
        os._exit(7)
    else:
        entries = outbox.get_unacked_for_resend(now=time.time() + 3600)
        payload = [
            Message(message_id=e.message_id, created_at=e.created_at, payload=e.payload_b64,
                    attempt=e.attempt, frame_timestamp=e.frame_timestamp, deadline=e.deadline).to_json()
            for e in entries
        ]
        sys.stdout.write(json.dumps({"pending": payload}) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
