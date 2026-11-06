"""Configuration: config.toml defaults, .env / process env overrides.

Precedence (low -> high):
    code defaults < config.toml < .env < process environment < explicit kwargs

Secrets (CLOB keys) are only read from the environment / .env; they are never
written to config.toml or returned by the dashboard API.
"""
from __future__ import annotations

import json
import os
import re
import tomllib
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent
_TOML_PATH = ROOT / "config.toml"

_INTERVAL_RE = re.compile(r"^(\d+)([smhd])$")
_INTERVAL_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def interval_seconds(value: str) -> int:
    """Parse a market interval such as ``"5m"`` or ``"15m"`` into seconds."""
    text = str(value).strip().lower()
    match = _INTERVAL_RE.fullmatch(text)
    if match is None:
        raise ValueError(
            f"invalid market interval {value!r}; expected a value like '5m' or '15m'"
        )
    return int(match.group(1)) * _INTERVAL_UNITS[match.group(2)]


def interval_label(seconds: int) -> str:
    """Inverse of :func:`interval_seconds` for intervals that divide a day."""
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60), ("s", 1)):
        if seconds % size == 0:
            return f"{seconds // size}{unit}"
    return f"{seconds}s"


@lru_cache(maxsize=1)
def _toml() -> dict:
    if not _TOML_PATH.exists():
        return {}
    with _TOML_PATH.open("rb") as f:
        return tomllib.load(f)


def _table(name: str) -> dict[str, Any]:
    """Case-insensitive top-level TOML table lookup.

    config.toml has always used upper-case table names (``[STRATEGY]``), so a
    plain ``_toml().get(name.lower())`` lookup silently ignored the whole file.
    Accept either casing.
    """
    for key, value in _toml().items():
        if str(key).lower() == name.lower() and isinstance(value, dict):
            return value
    return {}


def _section(name: str) -> dict[str, Any]:
    return {str(k).upper(): v for k, v in _table(name).items()}


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

    assets: str = "BTC,ETH,BNB,SOL,XRP,DOGE,HYPE"
    # Market cadence: the slug tag is ``updown-{market_interval}`` and the
    # window duration must agree with it.
    market_interval: str = "15m"
    window_sec: int = 900
    twap_window_s: int = 60

    rtds_url: str = "wss://ws-live-data.polymarket.com"
    clob_ws_url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    gamma_api: str = "https://gamma-api.polymarket.com"
    clob_api: str = "https://clob.polymarket.com"
    price_buffer_s: int = 900
    reconnect_base_s: float = 1.0
    reconnect_max_s: float = 30.0

    web_host: str = "127.0.0.1"
    web_port: int = 8037
    web_username: str = "admin"
    web_password: str = ""
    web_session_ttl_s: int = 7 * 24 * 3600
    web_enabled: bool = True

    paper_trading: bool = True
    live_enabled: bool = False

    data_dir: str = str(ROOT / "data")
    log_level: str = "INFO"

    # Live recording for offline book replay.  The recorder taps the existing
    # feeds; it does not open extra WebSocket connections.
    recording_enabled: bool = False
    recording_dir: str = ""
    recording_snapshot_hz: float = 1.0
    recording_top_levels: int = 10
    recording_raw_clob: bool = False
    recording_retention_days: int = 30
    recording_min_free_gb: float = 2.0

    # Live CLOB credentials (never exposed by the API).
    api_key: str = ""
    api_secret: str = ""
    api_passphrase: str = ""
    wallet_private_key: str = ""
    wallet_address: str = ""
    funder_address: str = ""
    signature_type: int = 0

    @model_validator(mode="after")
    def _check_market_interval(self) -> "AppSettings":
        seconds = interval_seconds(self.market_interval)
        if seconds != self.window_sec:
            raise ValueError(
                f"MARKET_INTERVAL={self.market_interval!r} implies {seconds}s "
                f"but WINDOW_SEC={self.window_sec}"
            )
        if self.window_sec <= 0 or 86400 % self.window_sec:
            raise ValueError("WINDOW_SEC must divide 86400 so windows align to UTC day")
        return self

    @property
    def slug_tag(self) -> str:
        return f"updown-{self.market_interval.strip().lower()}"

    @property
    def assets_list(self) -> list[str]:
        return [a.strip().upper() for a in self.assets.split(",") if a.strip()]

    @property
    def db_path(self) -> Path:
        return Path(self.data_dir) / "poly5m.db"

    @property
    def log_dir(self) -> Path:
        return Path(self.data_dir) / "logs"

    @property
    def recording_path(self) -> Path:
        return (
            Path(self.recording_dir)
            if self.recording_dir
            else Path(self.data_dir) / "recordings"
        )


class StrategyParams(BaseModel):
    """Runtime-editable strategy and risk knobs."""

    model_config = ConfigDict(extra="ignore")

    # Entry: fair - executable_ask - fee >= min_edge + model_buffer
    min_edge: float = Field(0.05, ge=0.0, le=1.0)
    model_buffer: float = Field(0.02, ge=0.0, le=1.0)
    # Side-level gates, applied before the edge threshold:
    #   * min_fair          — skip a side whose model probability is too low
    #                         (filters lottery tickets around fair≈0.5).
    #   * min_entry_price   — skip a side whose all-in entry price is too cheap;
    #                         a cheap entry means the market disagrees with the
    #                         model, which is dangerous when the model is
    #                         overconfident.
    min_fair: float = Field(0.0, ge=0.0, le=1.0)
    min_entry_price: float = Field(0.0, ge=0.0, le=1.0)
    max_position_usd: float = Field(5.0, gt=0.0, le=100000.0)
    daily_loss_limit_usd: float = Field(20.0, gt=0.0)
    entry_min_rel_s: int = Field(540, ge=0, le=899)
    entry_max_rel_s: int = Field(870, ge=1, le=900)
    max_spread: float = Field(0.03, ge=0.0, le=1.0)
    max_stale_book_s: float = Field(3.0, ge=0.1, le=60.0)
    min_ask_shares: float = Field(5.0, ge=0.0)
    min_order_shares: float = Field(5.0, ge=0.0)
    taker_fee_rate: float = Field(0.07, ge=0.0, le=0.2)
    sigma_lookback_s: int = Field(600, ge=60, le=3600)
    min_sigma_samples: int = Field(120, ge=30, le=3000)
    engine_tick_s: float = Field(0.5, ge=0.1, le=60.0)
    signal_log_min_edge: float = Field(0.02, ge=0.0, le=1.0)


# Key parameters editable per asset on the config page. Everything else is a
# common/global parameter: a saved per-asset override only contains these
# fields, so later global edits keep flowing into the other fields.
ASSET_KEY_FIELDS: tuple[str, ...] = (
    "min_edge",
    "model_buffer",
    "max_position_usd",
    "entry_min_rel_s",
    "entry_max_rel_s",
    "min_fair",
    "min_entry_price",
    "daily_loss_limit_usd",
)


class AssetConfig(BaseModel):
    """Runtime switch for one asset: trade on/off and paper/live mode."""

    model_config = ConfigDict(extra="ignore")

    enabled: bool = True
    # inherit -> follow PAPER_TRADING; paper/live -> explicit per-asset mode.
    mode: Literal["inherit", "paper", "live"] = "inherit"


def _toml_settings() -> dict[str, Any]:
    g, f, w, e, r = (
        _section("GENERAL"),
        _section("FEED"),
        _section("WEB"),
        _section("EXECUTION"),
        _section("RECORDING"),
    )
    raw_interval = g.get("MARKET_INTERVAL")
    raw_window = g.get("WINDOW_SEC")
    if raw_interval is None:
        # Backwards compatibility for a config that only sets WINDOW_SEC.
        raw_interval = interval_label(int(raw_window)) if raw_window else "15m"
    if raw_window is None:
        raw_window = interval_seconds(str(raw_interval))
    raw_recording_dir = str(r.get("DIR", "") or "")
    if raw_recording_dir and not Path(raw_recording_dir).is_absolute():
        raw_recording_dir = str(ROOT / raw_recording_dir)
    return {
        "assets": g.get("ASSETS", "BTC,ETH,BNB,SOL,XRP,DOGE,HYPE"),
        "market_interval": str(raw_interval),
        "window_sec": raw_window,
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
        "web_port": w.get("PORT", 8037),
        "web_username": w.get("USERNAME", "admin"),
        "web_password": w.get("PASSWORD", ""),
        "web_session_ttl_s": w.get("SESSION_TTL_S", 7 * 24 * 3600),
        "paper_trading": e.get("PAPER_TRADING", True),
        "live_enabled": e.get("LIVE_ENABLED", False),
        "data_dir": str(ROOT / "data"),
        "recording_enabled": r.get("ENABLED", False),
        "recording_dir": raw_recording_dir,
        "recording_snapshot_hz": r.get("SNAPSHOT_HZ", 1.0),
        "recording_top_levels": r.get("TOP_LEVELS", 10),
        "recording_raw_clob": r.get("RAW_CLOB", False),
        "recording_retention_days": r.get("RETENTION_DAYS", 30),
        "recording_min_free_gb": r.get("MIN_FREE_GB", 2.0),
    }


@lru_cache(maxsize=1)
def get_settings() -> AppSettings:
    data = _toml_settings()
    env = _env()
    for field in AppSettings.model_fields:
        env_name = field.upper()
        if env_name in env:
            data[field] = env[env_name]
    if "WINDOW_SEC" in env and "MARKET_INTERVAL" not in env:
        # A deployment that only overrides WINDOW_SEC still gets a matching
        # slug tag instead of a validation error.
        data["market_interval"] = interval_label(int(data["window_sec"]))
    return AppSettings(**data)


@lru_cache(maxsize=1)
def get_strategy_defaults() -> StrategyParams:
    # Skip nested [STRATEGY.<ASSET>] tables; they are parsed separately.
    raw = {
        str(k).lower(): v
        for k, v in _section("STRATEGY").items()
        if not isinstance(v, dict)
    }
    env = _env()
    for field in StrategyParams.model_fields:
        env_name = field.upper()
        if env_name in env:
            raw[field] = env[env_name]
    return StrategyParams(**raw)


@lru_cache(maxsize=1)
def get_asset_strategy_defaults() -> dict[str, dict[str, Any]]:
    """Per-asset defaults from ``[STRATEGY.<ASSET>]`` tables in config.toml.

    The values are partial: fields that are not listed keep inheriting the
    current global default (including edits made from the dashboard). A
    dashboard override is also partial and sits on top of these defaults.
    """
    raw = _table("STRATEGY")
    known = set(StrategyParams.model_fields)
    base = get_strategy_defaults().model_dump()
    out: dict[str, dict[str, Any]] = {}
    for asset, section in raw.items():
        if not isinstance(section, dict):
            continue
        values = {
            str(key).lower(): value
            for key, value in section.items()
            if str(key).lower() in known
        }
        if not values:
            continue
        # Fail fast at import/startup on an invalid per-asset default.
        StrategyParams.model_validate({**base, **values})
        out[str(asset).upper()] = values
    return out


class RuntimeConfig:
    """Strategy params persisted in SQLite so dashboard edits survive restarts.

    Layers, from lowest to highest precedence:
      * ``params`` — global defaults (config.toml / env / DB edit).
      * ``asset_defaults`` — partial ``[STRATEGY.<ASSET>]`` defaults from
        config.toml, merged over the *current* global params.
      * ``asset_overrides`` — partial per-asset overrides persisted in SQLite.
        Only the fields the operator changed are stored, so unset fields keep
        inheriting the current global value.

    ``asset_states`` stores the per-asset on/off switch and paper/live mode.
    """

    KEY = "strategy_params"
    ASSET_KEY = "asset_strategy_params"
    ASSET_STATE_KEY = "asset_state"

    def __init__(
        self,
        db: Any,
        defaults: StrategyParams | None = None,
        asset_defaults: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self._db = db
        self._defaults = defaults or get_strategy_defaults()
        self.asset_defaults = {
            str(asset).upper(): dict(values)
            for asset, values in (
                asset_defaults
                if asset_defaults is not None
                else get_asset_strategy_defaults()
            ).items()
        }
        self._known = set(StrategyParams.model_fields)
        self._effective: dict[str, tuple[int, StrategyParams]] = {}
        self._version = 0
        self.params = self._load(self.KEY, self._defaults)

        # Partial overrides. Older builds stored a full snapshot here; the
        # loader accepts both shapes, so existing databases keep working.
        self.asset_overrides: dict[str, dict[str, Any]] = {}
        raw = db.get_setting(self.ASSET_KEY) if db is not None else None
        if raw:
            try:
                data = json.loads(raw)
                for asset, value in (data or {}).items():
                    if not isinstance(value, dict):
                        continue
                    clean = {
                        str(name): item
                        for name, item in value.items()
                        if str(name) in self._known
                    }
                    if clean:
                        self.asset_overrides[str(asset).upper()] = clean
            except Exception:  # noqa: BLE001
                self.asset_overrides = {}

        self.asset_states: dict[str, AssetConfig] = {}
        raw_state = db.get_setting(self.ASSET_STATE_KEY) if db is not None else None
        if raw_state:
            try:
                data = json.loads(raw_state)
                for asset, value in (data or {}).items():
                    self.asset_states[str(asset).upper()] = AssetConfig.model_validate(
                        value
                    )
            except Exception:  # noqa: BLE001
                self.asset_states = {}

    def _load(self, key: str, default: StrategyParams) -> StrategyParams:
        raw = self._db.get_setting(key) if self._db is not None else None
        if raw:
            try:
                return StrategyParams.model_validate_json(raw)
            except Exception:  # noqa: BLE001
                pass
        return default

    def _bump(self) -> None:
        self._version += 1
        self._effective.clear()

    def params_for(self, asset: str | None) -> StrategyParams:
        if not asset:
            return self.params
        key = str(asset).upper()
        extra = self.asset_defaults.get(key) or {}
        override = self.asset_overrides.get(key) or {}
        if not extra and not override:
            return self.params
        cached = self._effective.get(key)
        if cached is not None and cached[0] == self._version:
            return cached[1]
        merged = self.params.model_dump()
        merged.update(extra)
        merged.update(override)
        params = StrategyParams.model_validate(merged)
        self._effective[key] = (self._version, params)
        return params

    def inherited_params_for(self, asset: str | None) -> StrategyParams:
        """Effective params without the SQLite per-asset override.

        Used by the config page to show what an asset would use after
        "reset", and to decide which key fields need an explicit override.
        """
        if not asset:
            return self.params
        key = str(asset).upper()
        extra = self.asset_defaults.get(key)
        if not extra:
            return self.params
        cache_key = f"inherited:{key}"
        cached = self._effective.get(cache_key)
        if cached is not None and cached[0] == self._version:
            return cached[1]
        merged = self.params.model_dump()
        merged.update(extra)
        params = StrategyParams.model_validate(merged)
        self._effective[cache_key] = (self._version, params)
        return params

    def has_override(self, asset: str | None) -> bool:
        return bool(asset) and bool(self.asset_overrides.get(str(asset).upper()))

    def override_fields_for(self, asset: str | None) -> list[str]:
        if not asset:
            return []
        return sorted(self.asset_overrides.get(str(asset).upper(), {}))

    def source_for(self, asset: str | None) -> str:
        """Where the effective params for ``asset`` come from.

        ``override``      — saved per-asset override in SQLite
        ``asset_default`` — partial [STRATEGY.<ASSET>] defaults in config.toml
        ``global``        — inherits the global defaults
        """
        key = str(asset).upper() if asset else ""
        if not key:
            return "global"
        if self.has_override(key):
            return "override"
        if key in self.asset_defaults:
            return "asset_default"
        return "global"

    # ── asset on/off + paper/live mode ────────────────────────────────────
    def asset_state_for(self, asset: str) -> dict[str, Any]:
        state = self.asset_states.get(str(asset).upper(), AssetConfig())
        return state.model_dump()

    def is_enabled(self, asset: str | None) -> bool:
        if not asset:
            return True
        state = self.asset_states.get(str(asset).upper())
        return True if state is None else state.enabled

    def mode_for(self, asset: str | None) -> str:
        if not asset:
            return "inherit"
        state = self.asset_states.get(str(asset).upper())
        return "inherit" if state is None else state.mode

    def enabled_assets(self, assets: list[str]) -> list[str]:
        return [a for a in assets if self.is_enabled(a)]

    def set_asset_state(
        self,
        asset: str,
        *,
        enabled: bool | None = None,
        mode: str | None = None,
    ) -> AssetConfig:
        key = str(asset).upper()
        data = self.asset_state_for(key)
        if enabled is not None:
            data["enabled"] = bool(enabled)
        if mode is not None:
            data["mode"] = mode
        state = AssetConfig.model_validate(data)
        self.asset_states[key] = state
        self._save_states()
        return state

    def _save_states(self) -> None:
        if self._db is None:
            return
        payload = {
            asset: state.model_dump() for asset, state in self.asset_states.items()
        }
        self._db.set_setting(self.ASSET_STATE_KEY, json.dumps(payload))

    # ── writes ────────────────────────────────────────────────────────────
    def _save_assets(self) -> None:
        if self._db is None:
            return
        self._db.set_setting(self.ASSET_KEY, json.dumps(self.asset_overrides))

    def update(
        self, patch: dict[str, Any], asset: str | None = None
    ) -> StrategyParams:
        key = str(asset).upper() if asset else None
        current = self.params_for(key).model_dump()
        merged = dict(current)
        for name, value in patch.items():
            if name in self._known:
                merged[name] = value
        params = StrategyParams.model_validate(merged)
        if key:
            override = dict(self.asset_overrides.get(key, {}))
            for name in patch:
                if name in self._known:
                    override[name] = getattr(params, name)
            if override:
                self.asset_overrides[key] = override
            else:
                self.asset_overrides.pop(key, None)
            self._bump()
            self._save_assets()
        else:
            self.params = params
            self._bump()
            if self._db is not None:
                self._db.set_setting(self.KEY, params.model_dump_json())
        return params

    def replace_key_overrides(
        self, asset: str, patch: dict[str, Any]
    ) -> StrategyParams:
        """Replace the config-page key fields for one asset.

        Fields absent from ``patch`` are removed from the override and fall
        back to the asset default / global value. Non-key fields from legacy
        full-snapshot overrides are preserved untouched.
        """
        key = str(asset).upper()
        values = {
            name: value for name, value in patch.items() if name in ASSET_KEY_FIELDS
        }
        current = self.params_for(key).model_dump()
        params = StrategyParams.model_validate({**current, **values})
        override = dict(self.asset_overrides.get(key, {}))
        for name in ASSET_KEY_FIELDS:
            override.pop(name, None)
        for name in values:
            override[name] = getattr(params, name)
        if override:
            self.asset_overrides[key] = override
        else:
            self.asset_overrides.pop(key, None)
        self._bump()
        self._save_assets()
        return self.params_for(key)

    def reset(self, asset: str | None = None) -> StrategyParams:
        key = str(asset).upper() if asset else None
        if key:
            self.asset_overrides.pop(key, None)
            self._bump()
            self._save_assets()
            return self.params_for(key)
        self.params = self._defaults
        self._bump()
        if self._db is not None:
            self._db.set_setting(self.KEY, self.params.model_dump_json())
        return self.params
