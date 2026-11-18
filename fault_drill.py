"""Phase 5a fault drill: timed operator instructions for a live run.

The drill does not execute destructive commands itself. It prints each step at
the scheduled moment, with an epoch timestamp and a CSV row, so the events can
be aligned with ``live_run.py``'s per-frame latency CSV.

Sequence (defaults):
    t+0    start live_run.py
    t+20   stop provider A
    t+50   cut Wi-Fi for 20 s (restore at t+70)
    t+90   kill the edge process
    t+105  restart the edge as a second segment
    t+130  stop both providers for 60 s (restore at t+190)
    t+220  end the drill

Run:
    python fault_drill.py
    python fault_drill.py --dry-run
    python fault_drill.py --csv logs/drill.csv
"""

from __future__ import annotations

import argparse
import csv
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent


@dataclass(frozen=True)
class DrillStep:
    """One scheduled operator action."""

    offset_s: float
    event: str
    instruction: str


DEFAULT_STEPS: tuple[DrillStep, ...] = (
    DrillStep(
        0,
        "START LIVE RUN",
        "Start the live run now in another terminal: "
        "`python live_run.py --duration 300 --fps 2 --endpoints ... --soak`",
    ),
    DrillStep(
        20,
        "STOP PROVIDER A",
        "On provider A's host: `docker stop real-infer-a` (or stop VM A in the cloud console).",
    ),
    DrillStep(
        50,
        "CUT WI-FI (20 s)",
        "Disable Wi-Fi / unplug the edge Ethernet now. Keep it down for 20 seconds.",
    ),
    DrillStep(
        70,
        "RESTORE NETWORK",
        "Re-enable Wi-Fi / reconnect Ethernet. The outbox should drain on reconnect.",
    ),
    DrillStep(
        90,
        "KILL EDGE",
        "Kill the live_run process abruptly: `kill -9 <pid>` (Git Bash/POSIX) "
        "or Task Manager -> End task. That segment ends here.",
    ),
    DrillStep(
        105,
        "RESTART EDGE (segment 2)",
        "Start live_run again with a separate report, e.g. "
        "`--out docs/live_results_segment2.md`. The outbox from the killed run is preserved.",
    ),
    DrillStep(
        130,
        "BOTH PROVIDERS DOWN (60 s)",
        "On the provider hosts: `docker stop real-infer-a real-infer-b` "
        "(or stop both VMs). Keep them down for 60 seconds.",
    ),
    DrillStep(
        190,
        "RESTORE BOTH PROVIDERS",
        "`docker start real-infer-a real-infer-b` (or start both VMs).",
    ),
    DrillStep(
        220,
        "END DRILL",
        "Stop live_run with Ctrl+C so it writes the report, and keep the drill CSV.",
    ),
)


def _iso_utc(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _write_row(writer: csv.writer, epoch: float, offset: float, event: str, instruction: str) -> None:
    writer.writerow([f"{epoch:.3f}", _iso_utc(epoch), f"{offset:.1f}", event, instruction])


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Timed fault-drill instructions for a live run")
    parser.add_argument("--csv", default=None,
                        help="Event CSV path (default logs/fault_drill_<UTC>.csv)")
    parser.add_argument("--dry-run", action="store_true", help="Print the plan without waiting")
    parser.add_argument("--start-delay", type=float, default=0.0,
                        help="Extra seconds before the first step (default 0)")
    parser.add_argument("--no-countdown", action="store_true", help="Do not print waiting notices")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    csv_path = Path(args.csv) if args.csv else (
        PROJECT_ROOT / "logs" / f"fault_drill_{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.csv"
    )

    print("Phase 5a fault drill")
    print("Every step is printed with epoch + UTC time so it can be joined to")
    print("live_run.py's logs/live/<run>/latency.csv (captured_epoch_s).")
    print("This script only prints instructions; it changes nothing itself.\n")

    if args.dry_run:
        for step in DEFAULT_STEPS:
            print(f"t+{step.offset_s:03.0f}s  {step.event}")
            print(f"         {step.instruction}")
        print(f"\nDry run only: no CSV written, nothing waited. Planned actions: {len(DEFAULT_STEPS)}.")
        return 0

    csv_path.parent.mkdir(parents=True, exist_ok=True)
    start = time.monotonic() + max(0.0, args.start_delay)
    written = 0
    try:
        with csv_path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(["epoch_s", "iso_utc", "offset_s", "event", "instruction"])
            for index, step in enumerate(DEFAULT_STEPS):
                target = start + step.offset_s
                now = time.monotonic()
                if target > now and not args.no_countdown:
                    print(f"next: {step.event} in {target - now:.0f}s")
                while (remaining := target - time.monotonic()) > 0:
                    time.sleep(min(remaining, 10.0))
                    remaining = target - time.monotonic()
                    if not args.no_countdown and remaining > 0.5:
                        print(f"  ... {remaining:4.0f}s to {step.event}")

                epoch = time.time()
                elapsed = time.monotonic() - start
                _write_row(writer, epoch, elapsed, step.event, step.instruction)
                handle.flush()
                written += 1
                print(f"[{epoch:.3f} | {_iso_utc(epoch)} | t+{elapsed:05.1f}s] {step.event}")
                print(f"    {step.instruction}")
    except KeyboardInterrupt:
        print("\ndrill aborted by operator (Ctrl+C).", file=sys.stderr)
        if csv_path.exists():
            print(f"partial event log: {csv_path}", file=sys.stderr)
        return 130

    print(f"\ndrill complete. {written} steps written to {csv_path}")
    print("Keep this CSV next to the live run report for the write-up.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
