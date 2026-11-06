"""FastAPI dashboard, served in the bot process on the same asyncio loop."""
from __future__ import annotations

import math
import secrets
import time
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from src.config import StrategyParams
from src.infra.logging import get_logger

log = get_logger(__name__)

WEB_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = WEB_DIR / "templates"
STATIC_DIR = WEB_DIR / "static"

app = FastAPI(title="Polymarket 5m TWAP Paper", docs_url=None, redoc_url=None, openapi_url=None)
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

_security = HTTPBasic(auto_error=False)
_state: Any = None


def set_state(state: Any) -> None:
    global _state
    _state = state


def _get_state() -> Any:
    if _state is None:
        raise HTTPException(status_code=503, detail="bot state not attached")
    return _state


async def require_auth(
    credentials: HTTPBasicCredentials | None = Depends(_security),
) -> None:
    settings = _get_state().settings
    if not settings.web_password:
        return
    if credentials is None:
        raise HTTPException(
            status_code=401,
            detail="authentication required",
            headers={"WWW-Authenticate": "Basic"},
        )
    user_ok = secrets.compare_digest(credentials.username, settings.web_username)
    pass_ok = secrets.compare_digest(credentials.password, settings.web_password)
    if not (user_ok and pass_ok):
        raise HTTPException(
            status_code=401,
            detail="invalid credentials",
            headers={"WWW-Authenticate": "Basic"},
        )


def _clean(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value]
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    return value


def _render_index() -> HTMLResponse:
    path = TEMPLATES_DIR / "index.html"
    if not path.exists():
        return HTMLResponse("<h1>index.html not found</h1>", status_code=500)
    return HTMLResponse(
        path.read_text(encoding="utf-8"),
        headers={"Cache-Control": "no-store"},
    )


@app.get("/", response_class=HTMLResponse)
async def index(_: None = Depends(require_auth)) -> HTMLResponse:
    return _render_index()


@app.get("/api/health")
async def health() -> dict:
    return {"ok": True, "ts": time.time()}


@app.get("/api/status")
async def status(_: None = Depends(require_auth)) -> dict:
    state = _get_state()
    return _clean(
        {
            "mode": state.router.mode,
            "uptime_s": time.time() - state.started_at,
            "killswitch": state.killswitch.to_dict(),
            "summary": state.db.summary(),
            "assets": state.engine.latest,
            "positions": state.db.open_positions(),
            "feeds": {
                "rtds": state.rtds.health(),
                "clob": state.books.health(),
            },
            "config": state.runtime.params.model_dump(),
        }
    )


@app.get("/api/windows")
async def windows(limit: int = Query(50, ge=1, le=500), _: None = Depends(require_auth)) -> dict:
    return {"data": _clean(_get_state().db.recent_windows(limit))}


@app.get("/api/signals")
async def signals(limit: int = Query(100, ge=1, le=1000), _: None = Depends(require_auth)) -> dict:
    return {"data": _clean(_get_state().db.recent_signals(limit))}


@app.get("/api/orders")
async def orders(limit: int = Query(100, ge=1, le=1000), _: None = Depends(require_auth)) -> dict:
    return {"data": _clean(_get_state().db.recent_orders(limit))}


@app.get("/api/fills")
async def fills(limit: int = Query(100, ge=1, le=1000), _: None = Depends(require_auth)) -> dict:
    return {"data": _clean(_get_state().db.recent_fills(limit))}


@app.get("/api/positions")
async def positions(limit: int = Query(100, ge=1, le=1000), _: None = Depends(require_auth)) -> dict:
    return {"data": _clean(_get_state().db.recent_positions(limit))}


@app.get("/api/pnl")
async def pnl(days: int = Query(7, ge=1, le=90), _: None = Depends(require_auth)) -> dict:
    state = _get_state()
    return {
        "summary": _clean(state.db.summary()),
        "by_day": _clean(state.db.pnl_by_day(days)),
    }


@app.get("/api/config")
async def get_config(_: None = Depends(require_auth)) -> dict:
    return {"params": _get_state().runtime.params.model_dump()}


@app.put("/api/config")
async def put_config(payload: dict[str, Any], _: None = Depends(require_auth)) -> dict:
    state = _get_state()
    try:
        params = state.runtime.update(payload)
    except ValidationError as exc:
        raise HTTPException(status_code=400, detail=exc.errors()) from exc
    log.info("web.config_updated", patch=list(payload.keys()))
    return {"ok": True, "params": params.model_dump()}


@app.post("/api/config/reset")
async def reset_config(_: None = Depends(require_auth)) -> dict:
    params = _get_state().runtime.reset()
    log.info("web.config_reset")
    return {"ok": True, "params": params.model_dump()}


@app.get("/api/control")
async def control(_: None = Depends(require_auth)) -> dict:
    return _get_state().killswitch.to_dict()


@app.post("/api/control/stop")
async def control_stop(_: None = Depends(require_auth)) -> dict:
    state = _get_state()
    state.killswitch.stop("dashboard")
    return {"ok": True, **state.killswitch.to_dict()}


@app.post("/api/control/start")
async def control_start(_: None = Depends(require_auth)) -> dict:
    state = _get_state()
    state.killswitch.start()
    return {"ok": True, **state.killswitch.to_dict()}


@app.get("/api/config/schema")
async def config_schema(_: None = Depends(require_auth)) -> dict:
    return {
        "fields": [
            {"name": name, "default": field.default}
            for name, field in StrategyParams.model_fields.items()
        ]
    }
