"""Parametrized controllable mock provider.

Besides the deterministic behaviour of ``mock_server``, this app exposes test
controls on the same real HTTP server:

* ``/control/fail``   - force every ``/predict`` to return HTTP 503
* ``/control/slow``   - fixed artificial delay before answering
* ``/control/hang``   - never answer (client read timeout path)
* ``/control/label``  - force the returned label (used to model a different
  decision on a retried inference)
* ``/control/random`` - per-request random 503s and/or delays
* ``/control/stats``  - count of responses produced, including injected faults
* ``/control/reset``  - clear all controls and counters

The counters are what let a test separate injected faults from organic ones.
"""
from __future__ import annotations

import asyncio
import json
import random

from fastapi import Request
from fastapi.responses import JSONResponse

from mock_server.app import create_app as create_mock_app


def create_mock_app_controlled(name: str = "mock"):
    app = create_mock_app()

    state = {
        "fail": False,
        "slow_ms": 0,
        "hang": False,
        "label_override": None,
        "fail_p": 0.0,
        "slow_p": 0.0,
        "slow_max_ms": 0,
        "rng": random.Random(),
        "stats": {"ok": 0, "fail": 0, "slow": 0},
    }

    @app.middleware("http")
    async def control_middleware(request: Request, call_next):
        if request.url.path not in ("/predict", "/predict/slow"):
            return await call_next(request)

        if state["fail"]:
            state["stats"]["fail"] += 1
            return JSONResponse(status_code=503, content={"detail": f"{name} forced failure"})
        if state["hang"]:
            await asyncio.sleep(3600)
            return await call_next(request)
        if state["slow_ms"] > 0:
            await asyncio.sleep(state["slow_ms"] / 1000.0)
        elif state["fail_p"] > 0 or state["slow_p"] > 0:
            draw = state["rng"].random()
            if draw < state["fail_p"]:
                state["stats"]["fail"] += 1
                return JSONResponse(status_code=503, content={"detail": f"{name} random failure"})
            if draw < state["fail_p"] + state["slow_p"] and state["slow_max_ms"] > 0:
                delay = state["rng"].uniform(0, state["slow_max_ms"]) / 1000.0
                state["stats"]["slow"] += 1
                await asyncio.sleep(delay)

        state["stats"]["ok"] += 1
        response = await call_next(request)
        if state["label_override"] and response.status_code == 200:
            body = b""
            async for chunk in response.body_iterator:
                body += chunk if isinstance(chunk, bytes) else str(chunk).encode()
            try:
                data = json.loads(body)
            except ValueError:
                return JSONResponse(status_code=500, content={"detail": "bad mock body"})
            data["label"] = state["label_override"]
            data["forced_label"] = True
            return JSONResponse(status_code=200, content=data)
        return response

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

    @app.get("/control/label")
    async def set_label(value: str | None = None):
        state["label_override"] = value or None
        return {"label_override": state["label_override"]}

    @app.get("/control/random")
    async def set_random(fail_p: float = 0.0, slow_p: float = 0.0,
                         slow_max_ms: int = 0, seed: int = 0):
        state["fail_p"] = max(0.0, min(1.0, fail_p))
        state["slow_p"] = max(0.0, min(1.0, slow_p))
        state["slow_max_ms"] = max(0, slow_max_ms)
        state["rng"] = random.Random(seed)
        return {"fail_p": state["fail_p"], "slow_p": state["slow_p"],
                "slow_max_ms": state["slow_max_ms"], "seed": seed}

    @app.get("/control/stats")
    async def get_stats():
        return {"provider": name, **state["stats"]}

    @app.get("/control/reset")
    async def reset_all():
        state["fail"] = False
        state["slow_ms"] = 0
        state["hang"] = False
        state["label_override"] = None
        state["fail_p"] = 0.0
        state["slow_p"] = 0.0
        state["slow_max_ms"] = 0
        state["rng"] = random.Random()
        state["stats"] = {"ok": 0, "fail": 0, "slow": 0}
        return {"ok": True}

    @app.get("/health")
    async def health_ext():
        return {"status": "ok", "provider": name, "fail": state["fail"],
                "slow_ms": state["slow_ms"], "hang": state["hang"]}

    return app


# for backward compat
app = create_mock_app_controlled("mock")
