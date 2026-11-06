"""Cookie-session authentication for the dashboard.

* Credentials come from WEB_USERNAME / WEB_PASSWORD (Coolify env / .env).
* A successful login mints a random 256-bit token kept in an in-memory
  session store; the browser gets it as an HttpOnly, SameSite=Lax cookie
  (Secure when the request arrived over HTTPS).
* Sessions are process-local: a redeploy logs everyone out. That is
  intentional for a single-operator dashboard.
* WEB_PASSWORD empty -> read-only pages stay public, but /config and the
  config/control APIs remain locked; the login page explains how to fix it.
"""
from __future__ import annotations

import secrets
import time
from dataclasses import dataclass

from fastapi import Request, Response

from src.config import get_settings
from src.infra.logging import get_logger

log = get_logger(__name__)

SESSION_COOKIE = "poly5m_session"
DEFAULT_TTL_S = 7 * 24 * 3600

_MAX_FAILURES = 10
_FAILURE_WINDOW_S = 300.0


@dataclass
class Session:
    user: str
    expires_at: float


_sessions: dict[str, Session] = {}
_failures: dict[str, list[float]] = {}


def session_ttl_s() -> int:
    try:
        return max(60, int(get_settings().web_session_ttl_s))
    except Exception:  # noqa: BLE001
        return DEFAULT_TTL_S


def credentials_configured() -> bool:
    return bool(get_settings().web_password)


def auth_required() -> bool:
    return credentials_configured()


def verify_credentials(username: str, password: str) -> bool:
    cfg = get_settings()
    if not cfg.web_password:
        return False
    expected_user = cfg.web_username or "admin"
    # compare_digest on str only accepts ASCII; encode so a non-ASCII password
    # (e.g. Chinese) does not raise at the login endpoint.
    return secrets.compare_digest(
        (username or "").encode("utf-8"), expected_user.encode("utf-8")
    ) and secrets.compare_digest(
        (password or "").encode("utf-8"), cfg.web_password.encode("utf-8")
    )


def create_session(user: str) -> str:
    now = time.time()
    _prune(now)
    token = secrets.token_urlsafe(32)
    _sessions[token] = Session(user=user, expires_at=now + session_ttl_s())
    return token


def get_session(token: str | None) -> Session | None:
    if not token:
        return None
    session = _sessions.get(token)
    if session is None:
        return None
    if session.expires_at <= time.time():
        _sessions.pop(token, None)
        return None
    return session


def delete_session(token: str | None) -> None:
    if token:
        _sessions.pop(token, None)


def _prune(now: float) -> None:
    for token in [t for t, s in _sessions.items() if s.expires_at <= now]:
        _sessions.pop(token, None)


def current_session(request: Request) -> Session | None:
    return get_session(request.cookies.get(SESSION_COOKIE))


def current_user(request: Request) -> str | None:
    session = current_session(request)
    return session.user if session else None


def is_https(request: Request) -> bool:
    forwarded = request.headers.get("x-forwarded-proto", "")
    first = forwarded.split(",")[0].strip().lower()
    return request.url.scheme == "https" or first == "https"


def set_session_cookie(response: Response, token: str, request: Request) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=session_ttl_s(),
        httponly=True,
        samesite="lax",
        secure=is_https(request),
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/")


def client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    first = forwarded.split(",")[0].strip()
    if first:
        return first
    return request.client.host if request.client else "unknown"


def login_blocked(ip: str) -> bool:
    now = time.time()
    stamps = [t for t in _failures.get(ip, []) if now - t < _FAILURE_WINDOW_S]
    _failures[ip] = stamps
    return len(stamps) >= _MAX_FAILURES


def record_login_failure(ip: str) -> None:
    _failures.setdefault(ip, []).append(time.time())


def reset_login_failures(ip: str) -> None:
    _failures.pop(ip, None)
