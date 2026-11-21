"""Run the dashboard: ``python -m ui --port 8080``."""

from __future__ import annotations

import argparse
import os
import sys

import uvicorn

from ui.data import DEMO_BANNER, parse_mock_providers
from ui.server import BindError, create_app, validate_bind


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="EdgeGrip local dashboard (FastAPI + plain HTML/CSS/JS)"
    )
    parser.add_argument("--host", default="127.0.0.1", help="Bind address (default 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8080, help="Bind port (default 8080)")
    parser.add_argument(
        "--demo", action="store_true",
        help="Synthetic data; shows the SIMULATED DATA banner",
    )
    parser.add_argument(
        "--allow-remote", action="store_true",
        help="Allow a non-loopback bind. Requires API_TOKEN.",
    )
    parser.add_argument(
        "--mock-provider", action="append", default=[],
        metavar="NAME=URL",
        help="Local mock control endpoint, e.g. mock-a=http://127.0.0.1:8002 "
             "(repeatable; loopback only)",
    )
    args = parser.parse_args(argv)

    if not 1 <= args.port <= 65535:
        print("error: --port must be between 1 and 65535", file=sys.stderr)
        return 2

    token = os.getenv("API_TOKEN", "").strip()
    try:
        validate_bind(args.host, allow_remote=args.allow_remote, token=token)
        mocks = parse_mock_providers(args.mock_provider)
    except (BindError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    app = create_app(
        demo=args.demo, token=token, mock_providers=args.mock_provider
    )
    print(f"EdgeGrip dashboard: http://{args.host}:{args.port}")
    if args.demo:
        print(f"DEMO MODE: {DEMO_BANNER}")
    if token:
        print("X-API-Token required for /api and /stream requests (value never logged).")
    if args.host not in {"127.0.0.1", "localhost", "::1"}:
        print(f"WARNING: dashboard is bound to {args.host}; traffic may leave this host.")
    # uvicorn never logs headers, and access logging stays off to avoid noise.
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning", access_log=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
