"""Check the README test table against the tests pytest actually collects.

Fails when:
  * a test ID in the README table has no matching collected test, or
  * a collected test has no row in the README table.

Run from the repository root:

    python -m scripts.check_docs
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
README = ROOT / "README.md"
START = "<!-- TEST_TABLE_START -->"
END = "<!-- TEST_TABLE_END -->"

TC_PATTERN = re.compile(r"^test_tc(\d+)([a-z]?)_(\d+)")
SPECIAL_PREFIXES = (
    ("test_tc5a_", "TC5a"),
    ("test_tc3_integration", "TC3-integration"),
    ("test_circuit_breaker", "TC3-unit"),
    ("test_health_thread", "TC3-unit"),
    ("test_tc5b_sim", "TC5b-sim"),
    ("test_capture_to_ack", "TC5b-live"),
    ("test_actuation_csv", "TC5b-live"),
    ("test_real_", "REAL-MODEL"),
)


def table_rows() -> dict[str, str]:
    """Return {test ID: file cell} parsed from the marked README table."""
    text = README.read_text(encoding="utf-8")
    try:
        block = text.split(START, 1)[1].split(END, 1)[0]
    except IndexError as exc:  # pragma: no cover - malformed README
        raise SystemExit(f"README is missing {START} / {END} markers") from exc

    rows: dict[str, str] = {}
    for line in block.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) < 3 or cells[0] in {"Test ID", "---"} or set(cells[0]) <= {"-"}:
            continue
        rows[cells[0].strip("` ")] = cells[1]
    if not rows:
        raise SystemExit("README test table has no data rows")
    return rows


def collected_tests() -> list[tuple[str, str, str]]:
    """Return [(relative file, function name, nodeid)] from pytest collection."""
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        sys.stderr.write(result.stdout)
        sys.stderr.write(result.stderr)
        raise SystemExit("pytest collection failed")

    tests = []
    for raw in result.stdout.splitlines():
        line = raw.strip()
        if "::" not in line or line.startswith("="):
            continue
        nodeid = line.split("[", 1)[0]
        file_part, _, function = nodeid.partition("::")
        tests.append((file_part.replace("\\", "/"), function, line))
    if not tests:
        raise SystemExit("pytest collected no tests")
    return tests


def test_id_for(function: str) -> str:
    """Map a test function name to its README row ID."""
    for prefix, row_id in SPECIAL_PREFIXES:
        if function.startswith(prefix):
            return row_id
    match = TC_PATTERN.match(function)
    if match:
        number, letter, case = match.groups()
        return f"TC{number}{letter}.{case}"
    raise ValueError(f"cannot derive a test ID from {function!r}")


def main() -> int:
    rows = table_rows()
    tests = collected_tests()
    errors: list[str] = []

    seen_ids: set[str] = set()
    for file_part, function, nodeid in tests:
        try:
            row_id = test_id_for(function)
        except ValueError as exc:
            errors.append(str(exc))
            continue
        seen_ids.add(row_id)
        if row_id not in rows:
            errors.append(f"{nodeid}: no row for {row_id} in the README test table")
            continue
        if file_part not in rows[row_id]:
            errors.append(
                f"{nodeid}: README row {row_id} lists {rows[row_id]!r}, "
                f"not {file_part!r}"
            )

    for row_id in sorted(set(rows) - seen_ids):
        errors.append(f"README lists {row_id}, but no collected test matches it")

    if errors:
        print("Documentation check FAILED:", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        return 1

    print(
        f"Documentation check passed: {len(tests)} collected tests map to "
        f"{len(rows)} README rows ({len(seen_ids)} IDs)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
