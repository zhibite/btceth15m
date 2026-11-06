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
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError

from src.config import (
    ASSET_KEY_FIELDS,
    AssetConfig,
    StrategyParams,
    get_settings,
)
from src.execution.live import LiveBroker
from src.infra.logging import get_logger
from src.web import auth

log = get_logger(__name__)

WEB_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = WEB_DIR / "templates"
STATIC_DIR = WEB_DIR / "static"

app = FastAPI(title="Polymarket 15m Bot", docs_url=None, redoc_url=None, openapi_url=None)
# The dashboard polls /api/status every 2s. Its 7-asset JSON is ~18 KiB raw but
# ~1.3 KiB gzipped because the payload is highly repetitive.
app.add_middleware(GZipMiddleware, minimum_size=1024)
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
        for name in (
            "style.css",
            "auth-nav.js",
            "dashboard.js",
            "config.js",
            "trades.js",
            "latency.js",
        ):
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
    settings = get_settings()
    interval = settings.market_interval.lower()
    html = (
        path.read_text(encoding="utf-8")
        .replace("__ASSET_VERSION__", ASSET_VERSION)
        .replace("__MARKET_LABEL__", f"POLY {interval.upper()}")
        .replace("__INTERVAL_LABEL__", interval)
    )
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


@app.get("/latency", response_class=HTMLResponse)
async def page_latency(request: Request) -> Any:
    redirect = _login_redirect(request)
    if redirect is not None:
        return redirect
    return _render("latency.html")


# ---------------------------------------------------------------------------
# Health + read APIs
# ---------------------------------------------------------------------------
def _live_info(state: Any) -> dict:
    settings = getattr(state, "settings", None)
    live = getattr(state, "live", None)
    enabled = bool(getattr(settings, "live_enabled", False))
    credentials = bool(getattr(settings, "wallet_private_key", ""))
    client_installed = LiveBroker.client_installed()
    if live is not None:
        available = bool(getattr(live, "available", False))
        reason = str(getattr(live, "unavailable_reason", "") or "")
    else:
        available = enabled and credentials and client_installed
        reason = ""
        if not available:
            if not enabled:
                reason = "LIVE_ENABLED=false"
            elif not credentials:
                reason = "WALLET_PRIVATE_KEY 未配置"
            else:
                reason = "py-clob-client-v2 未安装"
    return {
        "enabled": enabled,
        "available": available,
        "client_installed": client_installed,
        "credentials_configured": credentials,
        "reason": reason,
    }


def _global_mode(state: Any) -> str:
    settings = getattr(state, "settings", None)
    if bool(getattr(settings, "paper_trading", True)):
        return "paper"
    if not bool(getattr(settings, "live_enabled", False)):
        return "paper"
    # The global default is live only when live is actually usable; otherwise
    # "inherit" assets run as paper.
    return "live" if _live_info(state)["available"] else "paper"


def _effective_mode(state: Any, asset: str) -> str:
    router = getattr(state, "router", None)
    mode_for = getattr(router, "mode_for", None)
    if callable(mode_for):
        return str(mode_for(asset))
    return _global_mode(state)


def _asset_rows(state: Any) -> list[dict[str, Any]]:
    runtime = state.runtime
    assets = list(getattr(state.settings, "assets_list", []) or [])
    rows: list[dict[str, Any]] = []
    for asset in assets:
        params = runtime.params_for(asset)
        inherited = runtime.inherited_params_for(asset)
        asset_state = runtime.asset_state_for(asset)
        rows.append(
            {
                "asset": asset,
                "enabled": bool(asset_state.get("enabled", True)),
                "mode": str(asset_state.get("mode", "inherit")),
                "effective_mode": _effective_mode(state, asset),
                "params": {
                    name: getattr(params, name) for name in ASSET_KEY_FIELDS
                },
                "inherited": {
                    name: getattr(inherited, name) for name in ASSET_KEY_FIELDS
                },
                "override_fields": [
                    name
                    for name in runtime.override_fields_for(asset)
                    if name in ASSET_KEY_FIELDS
                ],
                "source": runtime.source_for(asset),
                "overridden": runtime.has_override(asset),
            }
        )
    return rows


def _asset_config_payload(state: Any) -> dict[str, Any]:
    return {
        "asset_rows": _asset_rows(state),
        "key_fields": list(ASSET_KEY_FIELDS),
        "global_mode": _global_mode(state),
        "live": _live_info(state),
    }


@app.get("/api/health")
async def api_health() -> dict:
    return {"ok": True, "ts": time.time()}


@app.get("/api/status")
async def api_status(_: None = Depends(require_read)) -> dict:
    state = _get_state()
    configured = list(state.settings.assets_list)
    enabled = state.runtime.enabled_assets(configured)
    modes = {
        asset: {
            "enabled": state.runtime.is_enabled(asset),
            "mode": state.runtime.mode_for(asset),
            "effective_mode": _effective_mode(state, asset),
        }
        for asset in configured
    }
    live_info = _live_info(state)
    return _clean(
        {
            "mode": state.router.mode,
            "uptime_s": time.time() - state.started_at,
            "killswitch": state.killswitch.to_dict(),
            "summary": state.db.summary(),
            "assets": state.engine.latest,
            "configured_assets": configured,
            "enabled_assets": enabled,
            "asset_states": modes,
            "modes": {
                "global_default": _global_mode(state),
                "live_enabled": live_info["enabled"],
                "live_available": live_info["available"],
                "paper_assets": [
                    a for a in enabled if _effective_mode(state, a) == "paper"
                ],
                "live_assets": [
                    a for a in enabled if _effective_mode(state, a) == "live"
                ],
            },
            "live": live_info,
            "market_interval": getattr(state.settings, "market_interval", "15m"),
            "window_sec": getattr(state.settings, "window_sec", 900),
            "positions": state.db.open_positions(),
            "feeds": {
                "rtds": state.rtds.health(),
                "clob": state.books.health(),
            },
            "recorder": (
                state.recorder.health()
                if getattr(state, "recorder", None) is not None
                else None
            ),
            "config": state.runtime.params.model_dump(),
        }
    )


@app.get("/api/latency")
async def api_latency(
    hours: int = Query(24, ge=1, le=168),
    _: None = Depends(require_read),
) -> dict:
    state = _get_state()
    tracker = getattr(state, "latency", None)
    if tracker is None:
        raise HTTPException(status_code=503, detail="latency tracker not attached")
    return _clean(
        {
            "summary": tracker.summary(900),
            "decision": tracker.decision_summary(3600),
            "buckets": state.db.recent_latency_buckets(hours, limit=500),
            "events": state.db.recent_latency_events(100),
            "feeds": {"rtds": state.rtds.health(), "clob": state.books.health()},
            "note": (
                "publish→recv 包含本机与 RTDS 的时钟偏差；"
                "client 最小值可作为网络+解析+偏差的上界。"
            ),
        }
    )


@app.get("/api/windows")
async def api_windows(
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    asset: str | None = Query(None),
    _: None = Depends(require_read),
) -> dict:
    db = _get_state().db
    key = _asset_key(asset)
    return _paginated(
        db.recent_windows(limit, offset, key),
        limit,
        offset,
        db.count_windows(key),
    )


@app.get("/api/signals")
async def api_signals(
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    asset: str | None = Query(None),
    _: None = Depends(require_read),
) -> dict:
    db = _get_state().db
    key = _asset_key(asset)
    return _paginated(
        db.recent_signals(limit, offset, key),
        limit,
        offset,
        db.count_signals(key),
    )


@app.get("/api/orders")
async def api_orders(
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    asset: str | None = Query(None),
    _: None = Depends(require_read),
) -> dict:
    db = _get_state().db
    key = _asset_key(asset)
    return _paginated(
        db.recent_orders(limit, offset, key),
        limit,
        offset,
        db.count_orders(key),
    )


@app.get("/api/fills")
async def api_fills(
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    asset: str | None = Query(None),
    _: None = Depends(require_read),
) -> dict:
    db = _get_state().db
    key = _asset_key(asset)
    return _paginated(
        db.recent_fills(limit, offset, key),
        limit,
        offset,
        db.count_fills(key),
    )


@app.get("/api/positions")
async def api_positions(
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    asset: str | None = Query(None),
    _: None = Depends(require_read),
) -> dict:
    db = _get_state().db
    key = _asset_key(asset)
    return _paginated(
        db.recent_positions(limit, offset, key),
        limit,
        offset,
        db.count_positions(key),
    )


@app.get("/api/pnl")
async def api_pnl(
    days: int = Query(7, ge=1, le=90),
    asset: str | None = Query(None),
    _: None = Depends(require_read),
) -> dict:
    state = _get_state()
    key = _asset_key(asset)
    return {
        "summary": _clean(state.db.summary(key)),
        "by_day": _clean(state.db.pnl_by_day(days, key)),
    }


# ---------------------------------------------------------------------------
# Config + control APIs (always require a login)
# ---------------------------------------------------------------------------
def _asset_key(asset: str | None) -> str | None:
    value = str(asset or "").strip().upper()
    return value or None


def _config_response(state: Any, key: str | None = None) -> dict:
    runtime = state.runtime
    params = runtime.params_for(key) if key else runtime.params
    payload = {
        "params": params.model_dump(),
        "global_params": runtime.params.model_dump(),
        "asset_defaults": {
            asset: dict(values)
            for asset, values in runtime.asset_defaults.items()
        },
        "assets": list(state.settings.assets_list),
        "asset": key or "",
        "source": runtime.source_for(key),
        "overridden": bool(key and runtime.has_override(key)),
        "mode": getattr(state.router, "mode", _global_mode(state)),
    }
    payload.update(_asset_config_payload(state))
    return payload


@app.get("/api/config")
async def api_get_config(
    asset: str | None = Query(None), _: None = Depends(require_login)
) -> dict:
    return _config_response(_get_state(), _asset_key(asset))


@app.put("/api/config")
async def api_put_config(
    payload: dict[str, Any], _: None = Depends(require_login)
) -> dict:
    state = _get_state()
    key = _asset_key(payload.get("asset"))
    patch = payload.get("params")
    if not isinstance(patch, dict):
        # Backward compatibility: old clients sent a flat patch (global only).
        patch = {k: v for k, v in payload.items() if k != "asset"}
    try:
        params = state.runtime.update(patch, asset=key)
    except ValidationError as exc:
        raise HTTPException(status_code=400, detail=_validation_errors(exc)) from exc
    log.info("web.config_updated", asset=key or "GLOBAL", patch=list(patch.keys()))
    return {
        "ok": True,
        "params": params.model_dump(),
        "asset": key or "",
        "source": state.runtime.source_for(key),
        "overridden": bool(key and state.runtime.has_override(key)),
    }


class AssetConfigPatch(BaseModel):
    asset: str
    enabled: bool | None = None
    mode: str | None = None
    params: dict[str, Any] | None = None


class ConfigBulkPayload(BaseModel):
    global_params: dict[str, Any] | None = None
    assets: list[AssetConfigPatch] = Field(default_factory=list)


def _validate_bulk(state: Any, payload: ConfigBulkPayload) -> list[AssetConfigPatch]:
    runtime = state.runtime
    configured = {
        str(a).upper() for a in getattr(state.settings, "assets_list", [])
    }
    live_info = _live_info(state)

    if payload.global_params:
        unknown = [
            name
            for name in payload.global_params
            if name not in StrategyParams.model_fields
        ]
        if unknown:
            raise HTTPException(
                status_code=400, detail=f"未知的通用参数：{', '.join(unknown)}"
            )
        try:
            StrategyParams.model_validate(
                {**runtime.params.model_dump(), **payload.global_params}
            )
        except ValidationError as exc:
            raise HTTPException(
                status_code=400, detail=_validation_errors(exc)
            ) from exc

    seen: set[str] = set()
    checked: list[AssetConfigPatch] = []
    for item in payload.assets:
        asset = _asset_key(item.asset)
        if not asset or asset in seen:
            raise HTTPException(status_code=400, detail="币种重复或为空")
        if asset not in configured:
            raise HTTPException(
                status_code=400, detail=f"未配置的币种：{asset}"
            )
        seen.add(asset)
        if item.mode is not None:
            try:
                AssetConfig(mode=item.mode)
            except ValidationError as exc:
                raise HTTPException(
                    status_code=400, detail=_validation_errors(exc)
                ) from exc
            if item.mode == "live" and not live_info["available"]:
                raise HTTPException(
                    status_code=400,
                    detail=f"实盘不可用：{live_info['reason']}",
                )
        if item.params:
            unknown = [
                name for name in item.params if name not in ASSET_KEY_FIELDS
            ]
            if unknown:
                raise HTTPException(
                    status_code=400,
                    detail=f"币种列表只能设置关键参数：{', '.join(unknown)}",
                )
            try:
                StrategyParams.model_validate(
                    {**runtime.params_for(asset).model_dump(), **item.params}
                )
            except ValidationError as exc:
                raise HTTPException(
                    status_code=400, detail=_validation_errors(exc)
                ) from exc
        checked.append(item)
    return checked


@app.put("/api/config/bulk")
async def api_put_config_bulk(
    payload: ConfigBulkPayload, _: None = Depends(require_login)
) -> dict:
    state = _get_state()
    checked = _validate_bulk(state, payload)
    if payload.global_params:
        state.runtime.update(payload.global_params)
    for item in checked:
        asset = str(item.asset).strip().upper()
        if item.enabled is not None or item.mode is not None:
            state.runtime.set_asset_state(
                asset, enabled=item.enabled, mode=item.mode
            )
        if item.params is not None:
            state.runtime.replace_key_overrides(asset, item.params)
    log.info(
        "web.config_bulk_updated",
        global_fields=sorted((payload.global_params or {}).keys()),
        assets=[str(item.asset).upper() for item in checked],
    )
    return {"ok": True, **_config_response(state)}


@app.post("/api/config/reset")
async def api_reset_config(
    payload: dict[str, Any] | None = None, _: None = Depends(require_login)
) -> dict:
    state = _get_state()
    key = _asset_key((payload or {}).get("asset"))
    params = state.runtime.reset(key)
    log.info("web.config_reset", asset=key or "GLOBAL")
    return {
        "ok": True,
        "params": params.model_dump(),
        "asset": key or "",
        "source": state.runtime.source_for(key),
        "overridden": bool(key and state.runtime.has_override(key)),
    }


@app.get("/api/config/schema")
async def api_config_schema(_: None = Depends(require_login)) -> dict:
    return {
        "fields": [
            {"name": name, "default": field.default}
            for name, field in StrategyParams.model_fields.items()
        ],
        "asset_key_fields": list(ASSET_KEY_FIELDS),
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
