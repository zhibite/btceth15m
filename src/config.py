"""Configuration: config.toml defaults, .env / process env overrides.

Precedence (low -> high):
    code defaults < config.toml < .env < process environment < explicit kwargs

Secrets (CLOB keys) are only read from the environment / .env; they are never
written to config.toml or returned by the dashboard API.
"""
from __future__ import annotations

import os
import tomllib
from functools import lru_cache
from pathlib import Path
from typing import Any

from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent
_TOML_PATH = ROOT / "config.toml"


@lru_cache(maxsize=1)
def _toml() -> dict:
    if not _TOML_PATH.exists():
        return {}
    with _TOML_PATH.open("rb") as f:
        return tomllib.load(f)


def _section(name: str) -> dict[str, Any]:
    raw = _toml().get(name.lower(), {}) or {}
    return {str(k).upper(): v for k, v in raw.items()}


@lru_cache(maxsize=1)
def _env() -> dict[str, str]:
    values: dict[str, str] = {}
    env_file = ROOT / ".env"
    if env_file.exists():
        for k, v in dotenv_values(env_file).items():
            if v is not None:
                values[str(k).upper()] = str(v)
    # Process environment wins over .env.
    values.update({str(k).upper(): v for k, v in os.environ.items()})
    return values


class AppSettings(BaseSettings):
    """Deployment / feed / web settings. Strategy knobs live in StrategyParams."""

    model_config = SettingsConfigDict(env_file=(str(ROOT / ".env"),), extra="ignore")

    assets: str = "BTC,ETH"
    window_sec: int = 300
    twap_window_s: int = 60

    rtds_url: str = "wss://ws-live-data.polymarket.com"
    clob_ws_url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    gamma_api: str = "https://gamma-api.polymarket.com"
    clob_api: str = "https://clob.polymarket.com"
    price_buffer_s: int = 900
    reconnect_base_s: float = 1.0
    reconnect_max_s: float = 30.0

    web_host: str = "127.0.0.1"
    web_port: int = 8036
    web_username: str = "admin"
    web_password: str = ""
    web_enabled: bool = True

    paper_trading: bool = True
    live_enabled: bool = False

    data_dir: str = str(ROOT / "data")
    log_level: str = "INFO"

    # Live CLOB credentials (never exposed by the API).
    api_key: str = ""
    api_secret: str = ""
    api_passphrase: str = ""
    wallet_private_key: str = ""
    wallet_address: str = ""
    funder_address: str = ""
    signature_type: int = 0

    @property
    def assets_list(self) -> list[str]:
        return [a.strip().upper() for a in self.assets.split(",") if a.strip()]

    @property
    def db_path(self) -> Path:
        return Path(self.data_dir) / "poly5m.db"

    @property
    def log_dir(self) -> Path:
        return Path(self.data_dir) / "logs"


class StrategyParams(BaseModel):
    """Runtime-editable strategy and risk knobs."""

    model_config = ConfigDict(extra="ignore")

    # Entry: fair - executable_ask - fee >= min_edge + model_buffer
    min_edge: float = 0.05
    model_buffer: float = 0.02
    max_position_usd: float = 5.0
    daily_loss_limit_usd: float = 20.0
    entry_min_rel_s: int = 60
    entry_max_rel_s: int = 285
    max_spread: float = 0.03
    max_stale_book_s: float = 3.0
    min_ask_shares: float = 5.0
    min_order_shares: float = 5.0
    taker_fee_rate: float = 0.07
    sigma_lookback_s: int = 600
    min_sigma_samples: int = 120
    engine_tick_s: float = 0.5
    hold_to_settlement: bool = True
    signal_log_min_edge: float = 0.02


def _toml_settings() -> dict[str, Any]:
    g, f, w, e = (
        _section("GENERAL"),
        _section("FEED"),
        _section("WEB"),
        _section("EXECUTION"),
    )
    return {
        "assets": g.get("ASSETS", "BTC,ETH"),
        "window_sec": g.get("WINDOW_SEC", 300),
        "twap_window_s": g.get("TWAP_WINDOW_S", 60),
        "rtds_url": f.get("RTDS_URL", "wss://ws-live-data.polymarket.com"),
        "clob_ws_url": f.get(
            "CLOB_WS_URL",
            "wss://ws-subscriptions-clob.polymarket.com/ws/market",
        ),
        "gamma_api": f.get("GAMMA_API", "https://gamma-api.polymarket.com"),
        "clob_api": f.get("CLOB_API", "https://clob.polymarket.com"),
        "price_buffer_s": f.get("PRICE_BUFFER_S", 900),
        "reconnect_base_s": f.get("RECONNECT_BASE_S", 1.0),
        "reconnect_max_s": f.get("RECONNECT_MAX_S", 30.0),
        "web_host": w.get("HOST", "127.0.0.1"),
        "web_port": w.get("PORT", 8036),
        "web_username": w.get("USERNAME", "admin"),
        "web_password": w.get("PASSWORD", ""),
        "paper_trading": e.get("PAPER_TRADING", True),
        "live_enabled": e.get("LIVE_ENABLED", False),
        "data_dir": str(ROOT / "data"),
    }


@lru_cache(maxsize=1)
def get_settings() -> AppSettings:
    data = _toml_settings()
    env = _env()
    for field in AppSettings.model_fields:
        env_name = field.upper()
        if env_name in env:
            data[field] = env[env_name]
    return AppSettings(**data)


@lru_cache(maxsize=1)
def get_strategy_defaults() -> StrategyParams:
    raw = {str(k).lower(): v for k, v in _section("STRATEGY").items()}
    env = _env()
    for field in StrategyParams.model_fields:
        env_name = field.upper()
        if env_name in env:
            raw[field] = env[env_name]
    return StrategyParams(**raw)


class RuntimeConfig:
    """Strategy params persisted in SQLite so dashboard edits survive restarts."""

    KEY = "strategy_params"

    def __init__(self, db: Any, defaults: StrategyParams | None = None) -> None:
        self._db = db
        self._defaults = defaults or get_strategy_defaults()
        raw = db.get_setting(self.KEY) if db is not None else None
        if raw:
            try:
                self.params = StrategyParams.model_validate_json(raw)
            except Exception:  # noqa: BLE001
                self.params = self._defaults
        else:
            self.params = self._defaults

    def update(self, patch: dict[str, Any]) -> StrategyParams:
        merged = self.params.model_dump()
        for key, value in patch.items():
            if key in merged:
                merged[key] = value
        self.params = StrategyParams.model_validate(merged)
        if self._db is not None:
            self._db.set_setting(self.KEY, self.params.model_dump_json())
        return self.params

    def reset(self) -> StrategyParams:
        self.params = self._defaults
        if self._db is not None:
            self._db.set_setting(self.KEY, self.params.model_dump_json())
        return self.params
