"""FastAPI application for the local EdgeGrip dashboard.

The dashboard is read-only for run artifacts. Only two actions mutate state:
mock-provider fault controls and the software stop endpoint. Starting a run
spawns ``live_run.py`` as a separate process with a validated argument vector.
"""

from __future__ import annotations

import hmac
import os
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from ui.data import (
    DEMO_BANNER,
    DemoSource,
    LiveSource,
    parse_mock_providers,
)
from ui.run_control import (
    PROJECT_ROOT,
    RunBusyError,
    RunManager,
    RunValidationError,
    validate_run_params,
)

STATIC_DIR = Path(__file__).resolve().parent / "static"
INDEX_PATH = STATIC_DIR / "index.html"


class BindError(ValueError):
    """The requested bind would expose the UI without the token gate."""


def is_loopback_host(host: str) -> bool:
    from ui.data import is_loopback

    return is_loopback(host)


def validate_bind(host: str, *, allow_remote: bool, token: str) -> None:
    """Refuse a non-loopback bind unless explicitly allowed and token-gated."""
    if is_loopback_host(host):
        return
    if not allow_remote:
        raise BindError(
            "refusing a non-loopback bind without --allow-remote"
        )
    if not token.strip():
        raise BindError(
            "refusing a non-loopback bind without API_TOKEN; set the same "
            "X-API-Token value the real server uses"
        )


class RunStartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: Literal["camera", "images"] = "camera"
    fps: float = Field(2.0, ge=0.1, le=30.0)
    deadline_ms: float = Field(8000.0, ge=100.0, le=60000.0)
    duration: float = Field(60.0, ge=1.0, le=3600.0)
    endpoints: list[str] = Field(default_factory=list, max_length=8)
    camera: int = Field(0, ge=0, le=16)


class MockFaultRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confirm: bool = False
    ms: int = Field(1000, ge=0, le=30000)


def _index_html(demo: bool) -> str:
    try:
        text = INDEX_PATH.read_text(encoding="utf-8")
    except OSError:
        return "<!doctype html><title>EdgeGrip UI</title><h1>UI assets missing</h1>"
    banner = (
        f'<div id="demo-banner" class="demo-banner" role="status">'
        f"{DEMO_BANNER}</div>"
        if demo else ""
    )
    text = text.replace("<!--DEMO_BANNER-->", banner)
    text = text.replace("<!--DEMO_STATE-->", "true" if demo else "false")
    return text


def create_app(
    *,
    demo: bool = False,
    token: str | None = None,
    mock_providers: list[str] | None = None,
    source: DemoSource | LiveSource | None = None,
    run_manager: RunManager | None = None,
    project_root: Path = PROJECT_ROOT,
) -> FastAPI:
    """Build the dashboard application.

    Args:
        demo: Use synthetic data and show the fixed simulation banner.
        token: X-API-Token value. Empty disables the gate (loopback only).
        mock_providers: ``name=url`` local mock control endpoints.
        source: Optional injected data source for tests.
        run_manager: Optional injected run controller for tests.
    """
    resolved_token = (token if token is not None else os.getenv("API_TOKEN", "")).strip()
    parsed_mocks = parse_mock_providers(mock_providers or [])
    if source is None:
        source = DemoSource() if demo else LiveSource(mock_providers=parsed_mocks)
    if run_manager is None:
        run_manager = RunManager(demo=demo, project_root=project_root, source=source)

    app = FastAPI(title="EdgeGrip local dashboard", version="1.0.0")
    app.state.source = source
    app.state.runs = run_manager
    app.state.token_configured = bool(resolved_token)
    app.state.demo = demo

    @app.middleware("http")
    async def token_gate(request: Request, call_next):
        path = request.url.path
        if resolved_token and (path.startswith("/api/") or path.startswith("/stream/")):
            supplied = request.headers.get("X-API-Token", "")
            if not hmac.compare_digest(supplied, resolved_token):
                return JSONResponse(
                    status_code=401,
                    content={
                        "detail": "Unauthorized. Send X-API-Token.",
                        "auth_required": True,
                    },
                    headers={"WWW-Authenticate": "X-API-Token"},
                )
        return await call_next(request)

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return _index_html(demo)

    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        return {
            "status": "ok",
            "demo": demo,
            "auth_required": bool(resolved_token),
        }

    @app.get("/api/state")
    def state() -> dict[str, Any]:
        return source.state(run=run_manager.status())

    @app.get("/api/latency")
    def latency() -> dict[str, Any]:
        return source.latency()

    @app.get("/api/events")
    def events(limit: int = Query(100, ge=1, le=500)) -> list[dict[str, Any]]:
        return source.events(limit=limit)

    @app.get("/api/actuation")
    def actuation(limit: int = Query(50, ge=1, le=200)) -> list[dict[str, Any]]:
        return source.actuation(limit=limit)

    @app.get("/stream/frame")
    def frame() -> Response:
        return Response(
            content=source.frame_jpeg(),
            media_type="image/jpeg",
            headers={"Cache-Control": "no-store"},
        )

    @app.post("/api/run/start")
    def run_start(body: RunStartRequest) -> dict[str, Any]:
        # Re-validate the typed body so the child always receives a plain dict.
        try:
            params = validate_run_params(body.model_dump(), demo=demo)
            return run_manager.start(params)
        except RunBusyError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except RunValidationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/run/stop")
    def run_stop() -> dict[str, Any]:
        try:
            return run_manager.stop()
        except RunBusyError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/mock/{provider}/{action}")
    def mock_fault(provider: str, action: str, body: MockFaultRequest) -> dict[str, Any]:
        try:
            return source.mock_fault(
                provider, action, confirmed=body.confirm, ms=body.ms
            )
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.post("/api/stop")
    def software_stop() -> dict[str, Any]:
        return source.software_stop()

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app
