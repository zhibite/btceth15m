"""FastAPI dashboard and JSON API.

Pages:
  /login     -> HTML login page (cookie session, no Basic-auth popup)
  /          -> 控制台
  /trades    -> 交易记录
  /config    -> 策略配置 (always requires a login)

Read APIs follow the same rule as the pages: public only when WEB_PASSWORD
is empty. Config and control APIs always require a session.
"""
from __future__ import annotations

import hashlib
import math
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ValidationError

from src.config import StrategyParams
from src.infra.logging import get_logger
from src.web import auth

log = get_logger(__name__)

WEB_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = WEB_DIR / "templates"
STATIC_DIR = WEB_DIR / "static"

app = FastAPI(title="BTCETH5M", docs_url=None, redoc_url=None, openapi_url=None)
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

_state: Any = None


def set_state(state: Any) -> None:
    global _state
    _state = state


def _get_state() -> Any:
    if _state is None:
        raise HTTPException(status_code=503, detail="bot state not attached")
    return _state


def _asset_version() -> str:
    digest = hashlib.md5()
    try:
        for name in ("style.css", "auth-nav.js", "dashboard.js", "config.js", "trades.js"):
            path = STATIC_DIR / name
            if path.exists():
                digest.update(path.read_bytes())
    except Exception:  # noqa: BLE001
        return "1"
    return digest.hexdigest()[:10] or "1"


ASSET_VERSION = _asset_version()


def _render(name: str) -> HTMLResponse:
    path = TEMPLATES_DIR / name
    if not path.exists():
        return HTMLResponse(f"<h1>{name} not found</h1>", status_code=500)
    html = path.read_text(encoding="utf-8").replace("__ASSET_VERSION__", ASSET_VERSION)
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})


def _clean(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value]
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    return value


def _paginated(rows: Any, limit: int, offset: int, total: int) -> dict:
    return {
        "data": _clean(rows),
        "pagination": {
            "limit": int(limit),
            "offset": int(offset),
            "total": int(total),
            "has_more": int(offset) + int(limit) < int(total),
        },
    }


def _safe_next(value: str | None) -> str:
    if value and value.startswith("/") and not value.startswith("//"):
        return value
    return "/"


def _login_redirect(request: Request, *, always: bool = False) -> RedirectResponse | None:
    if (always or auth.auth_required()) and auth.current_session(request) is None:
        path = request.url.path
        if request.url.query:
            path += "?" + request.url.query
        return RedirectResponse(f"/login?next={quote(path)}", status_code=302)
    return None


async def require_read(request: Request) -> None:
    if auth.auth_required() and auth.current_session(request) is None:
        raise HTTPException(status_code=401, detail="需要登录")


async def require_login(request: Request) -> None:
    if auth.current_session(request) is None:
        raise HTTPException(status_code=401, detail="需要登录")


class LoginPayload(BaseModel):
    username: str = ""
    password: str = ""


def _validation_errors(exc: ValidationError) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for error in exc.errors():
        out.append(
            {
                "loc": ".".join(str(x) for x in error.get("loc", [])),
                "msg": str(error.get("msg", "")),
                "type": str(error.get("type", "")),
            }
        )
    return out


# ---------------------------------------------------------------------------
# Auth routes
# ---------------------------------------------------------------------------
@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request) -> Any:
    if auth.current_session(request) is not None:
        return RedirectResponse(_safe_next(request.query_params.get("next")), status_code=302)
    return _render("login.html")


@app.get("/api/auth/me")
async def api_auth_me(request: Request) -> dict:
    session = auth.current_session(request)
    return {
        "authenticated": session is not None,
        "user": session.user if session else None,
        "auth_required": auth.auth_required(),
        "credentials_configured": auth.credentials_configured(),
    }


@app.post("/api/auth/login")
async def api_auth_login(
    payload: LoginPayload, request: Request, response: Response
) -> dict:
    if not auth.credentials_configured():
        raise HTTPException(
            status_code=503,
            detail="管理员尚未设置 WEB_PASSWORD，请在 Coolify 环境变量中配置后重新部署",
        )
    ip = auth.client_ip(request)
    if auth.login_blocked(ip):
        raise HTTPException(status_code=429, detail="登录失败次数过多，请 5 分钟后再试")
    if not auth.verify_credentials(payload.username, payload.password):
        auth.record_login_failure(ip)
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    auth.reset_login_failures(ip)
    user = payload.username or "admin"
    token = auth.create_session(user)
    auth.set_session_cookie(response, token, request)
    log.info("web.login", user=user, ip=ip)
    return {"ok": True, "user": user}


@app.post("/api/auth/logout")
async def api_auth_logout(request: Request, response: Response) -> dict:
    token = request.cookies.get(auth.SESSION_COOKIE)
    auth.delete_session(token)
    auth.clear_session_cookie(response)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
async def page_dashboard(request: Request) -> Any:
    redirect = _login_redirect(request)
    if redirect is not None:
        return redirect
    return _render("dashboard.html")


@app.get("/trades", response_class=HTMLResponse)
async def page_trades(request: Request) -> Any:
    redirect = _login_redirect(request)
    if redirect is not None:
        return redirect
    return _render("trades.html")


@app.get("/config", response_class=HTMLResponse)
async def page_config(request: Request) -> Any:
    redirect = _login_redirect(request, always=True)
    if redirect is not None:
        return redirect
    return _render("config.html")


# ---------------------------------------------------------------------------
# Health + read APIs
# ---------------------------------------------------------------------------
@app.get("/api/health")
async def api_health() -> dict:
    return {"ok": True, "ts": time.time()}


@app.get("/api/status")
async def api_status(_: None = Depends(require_read)) -> dict:
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
async def api_windows(
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    _: None = Depends(require_read),
) -> dict:
    db = _get_state().db
    return _paginated(db.recent_windows(limit, offset), limit, offset, db.count_windows())


@app.get("/api/signals")
async def api_signals(
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    _: None = Depends(require_read),
) -> dict:
    db = _get_state().db
    return _paginated(db.recent_signals(limit, offset), limit, offset, db.count_signals())


@app.get("/api/orders")
async def api_orders(
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    _: None = Depends(require_read),
) -> dict:
    db = _get_state().db
    return _paginated(db.recent_orders(limit, offset), limit, offset, db.count_orders())


@app.get("/api/fills")
async def api_fills(
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    _: None = Depends(require_read),
) -> dict:
    db = _get_state().db
    return _paginated(db.recent_fills(limit, offset), limit, offset, db.count_fills())


@app.get("/api/positions")
async def api_positions(
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    _: None = Depends(require_read),
) -> dict:
    db = _get_state().db
    return _paginated(
        db.recent_positions(limit, offset), limit, offset, db.count_positions()
    )


@app.get("/api/pnl")
async def api_pnl(
    days: int = Query(7, ge=1, le=90), _: None = Depends(require_read)
) -> dict:
    state = _get_state()
    return {
        "summary": _clean(state.db.summary()),
        "by_day": _clean(state.db.pnl_by_day(days)),
    }


# ---------------------------------------------------------------------------
# Config + control APIs (always require a login)
# ---------------------------------------------------------------------------
@app.get("/api/config")
async def api_get_config(_: None = Depends(require_login)) -> dict:
    state = _get_state()
    return {
        "params": state.runtime.params.model_dump(),
        "mode": state.router.mode,
    }


@app.put("/api/config")
async def api_put_config(
    payload: dict[str, Any], _: None = Depends(require_login)
) -> dict:
    state = _get_state()
    try:
        params = state.runtime.update(payload)
    except ValidationError as exc:
        raise HTTPException(status_code=400, detail=_validation_errors(exc)) from exc
    log.info("web.config_updated", patch=list(payload.keys()))
    return {"ok": True, "params": params.model_dump()}


@app.post("/api/config/reset")
async def api_reset_config(_: None = Depends(require_login)) -> dict:
    params = _get_state().runtime.reset()
    log.info("web.config_reset")
    return {"ok": True, "params": params.model_dump()}


@app.get("/api/config/schema")
async def api_config_schema(_: None = Depends(require_login)) -> dict:
    return {
        "fields": [
            {"name": name, "default": field.default}
            for name, field in StrategyParams.model_fields.items()
        ]
    }


@app.get("/api/control")
async def api_control(_: None = Depends(require_login)) -> dict:
    return _get_state().killswitch.to_dict()


@app.post("/api/control/stop")
async def api_control_stop(_: None = Depends(require_login)) -> dict:
    state = _get_state()
    state.killswitch.stop("dashboard")
    return {"ok": True, **state.killswitch.to_dict()}


@app.post("/api/control/start")
async def api_control_start(_: None = Depends(require_login)) -> dict:
    state = _get_state()
    state.killswitch.start()
    return {"ok": True, **state.killswitch.to_dict()}
