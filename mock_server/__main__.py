"""Run the mock inference server: ``python -m mock_server [--port 8000]``."""

from __future__ import annotations

import argparse

import uvicorn

from mock_server.app import create_app


def main() -> None:
    """Parse CLI arguments and serve the mock API."""
    parser = argparse.ArgumentParser(description="Deterministic mock inference server")
    parser.add_argument("--host", default="127.0.0.1", help="Bind address (default 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8000, help="Bind port (default 8000)")
    parser.add_argument("--reload", action="store_true", help="Enable auto-reload for development")
    args = parser.parse_args()

    uvicorn.run(create_app(), host=args.host, port=args.port, reload=args.reload)


if __name__ == "__main__":
    main()
