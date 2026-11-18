"""Subprocess harness: run the worker's inference/claim/gate work, then crash.

Used by TC4.10. The process deliberately exits without marking the message
processed and without publishing an ACK, modelling a worker that dies after
inference but before delivery is acknowledged.

Arguments are passed on the command line; the first decision is written to
``--result-out`` before the crash so the parent test can inspect it.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import replace
from pathlib import Path

from phase2_reliability.common import Message
from phase2_reliability.config import load_settings
from phase2_reliability.worker import Worker
from phase3_router import HTTPProvider, Router
from phase4_conflict import ConflictStore, DecisionGate


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--message", required=True)
    parser.add_argument("--conflict-db", required=True)
    parser.add_argument("--processed-db", required=True)
    parser.add_argument("--result-out", required=True)
    args = parser.parse_args()

    settings = replace(
        load_settings(),
        processed_db=Path(args.processed_db),
        outbox_db=Path(args.processed_db).with_name("unused_outbox.db"),
    )
    store = ConflictStore(args.conflict_db)
    gate = DecisionGate(args.conflict_db)
    router = Router([HTTPProvider("mock_a", args.endpoint)], health_check=False)
    worker = Worker(settings=settings, router=router, conflict_store=store, decision_gate=gate)

    message = Message.from_json(Path(args.message).read_text(encoding="utf-8"))
    result = worker.process_message(message)
    Path(args.result_out).write_text(json.dumps(result), encoding="utf-8")
    sys.stdout.write(json.dumps({"first_decision": result.get("decision")}) + "\n")
    sys.stdout.flush()
    # Crash after inference: no processed mark, no ACK, no result publish.
    os._exit(3)


if __name__ == "__main__":
    main()
