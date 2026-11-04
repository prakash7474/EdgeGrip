"""Parametrized controllable mock provider."""
from __future__ import annotations

import asyncio
import os

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from mock_server.app import create_app as create_mock_app


def create_mock_app_controlled(name: str = "mock"):
    app = create_mock_app()

    state = {"fail": False, "slow_ms": 0, "hang": False}

    @app.middleware("http")
    async def control_middleware(request: Request, call_next):
        if request.url.path == "/predict":
            if state["fail"]:
                return JSONResponse(status_code=503, content={"detail": f"{name} forced failure"})
            if state["hang"]:
                await asyncio.sleep(3600)
                return await call_next(request)
            if state["slow_ms"] > 0:
                await asyncio.sleep(state["slow_ms"] / 1000.0)
        return await call_next(request)

    @app.get("/control/fail")
    async def set_fail(fail: int = 1):
        state["fail"] = fail == 1
        return {"fail": state["fail"]}

    @app.get("/control/slow")
    async def set_slow(ms: int = 0):
        state["slow_ms"] = ms
        return {"slow_ms": state["slow_ms"]}

    @app.get("/control/hang")
    async def set_hang(hang: int = 1):
        state["hang"] = hang == 1
        return {"hang": state["hang"]}

    @app.get("/control/reset")
    async def reset_all():
        state["fail"] = False
        state["slow_ms"] = 0
        state["hang"] = False
        return {"ok": True}

    @app.get("/health")
    async def health_ext():
        return {"status": "ok", "provider": name, "fail": state["fail"], "slow_ms": state["slow_ms"], "hang": state["hang"]}

    return app


# for backward compat
app = create_mock_app_controlled("mock")
