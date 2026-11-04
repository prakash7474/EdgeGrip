"""Run the real inference server: ``python -m real_server [--port 8001]``."""

from __future__ import annotations

import argparse

import uvicorn

from real_server.app import create_app


def main() -> None:
    parser = argparse.ArgumentParser(description="Real inference server (RT-DETR)")
    parser.add_argument("--host", default="127.0.0.1", help="Bind address (default 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8001, help="Bind port (default 8001)")
    parser.add_argument("--reload", action="store_true", help="Enable auto-reload for development")
    args = parser.parse_args()

    uvicorn.run(create_app(), host=args.host, port=args.port, reload=args.reload)


if __name__ == "__main__":
    main()
