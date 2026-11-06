"""Kill switch and entry risk gates."""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from typing import Any, Optional

from src.config import RuntimeConfig
from src.infra.logging import get_logger
from src.persistence.db import Database

log = get_logger(__name__)


class KillSwitch:
    KEY = "killswitch"

    def __init__(self, db: Database) -> None:
        self._db = db
        self.active = False
        self.reason = ""
        self.since = 0.0
        self._load()

    def _load(self) -> None:
        raw = self._db.get_setting(self.KEY) if self._db is not None else None
        if not raw:
            return
        try:
            data = json.loads(raw)
            self.active = bool(data.get("active", False))
            self.reason = str(data.get("reason", ""))
            self.since = float(data.get("since", 0.0))
        except (TypeError, ValueError):
            pass

    def _save(self) -> None:
        if self._db is None:
            return
        self._db.set_setting(
            self.KEY,
            json.dumps(
                {"active": self.active, "reason": self.reason, "since": self.since}
            ),
        )

    def stop(self, reason: str) -> None:
        self.active = True
        self.reason = reason
        self.since = time.time()
        self._save()
        log.warning("killswitch.engaged", reason=reason)

    def start(self) -> None:
        self.active = False
        self.reason = ""
        self.since = 0.0
        self._save()
        log.info("killswitch.released")

    def to_dict(self) -> dict:
        return {"active": self.active, "reason": self.reason, "since": self.since}


class RiskManager:
    def __init__(self, db: Database, runtime: RuntimeConfig, killswitch: KillSwitch) -> None:
        self._db = db
        self._runtime = runtime
        self._kill = killswitch
        self._pnl_cache: dict[str, tuple[float, float]] = {}

    def daily_pnl(self, asset: str | None = None) -> float:
        key = str(asset).upper() if asset else ""
        now = time.time()
        cached = self._pnl_cache.get(key)
        if cached is not None and now - cached[0] < 2.0:
            return cached[1]
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        pnl = float(self._db.daily_realized(today, key or None).get("pnl") or 0.0)
        self._pnl_cache[key] = (now, pnl)
        return pnl

    def can_enter(self, asset: str | None = None) -> tuple[bool, str]:
        if self._kill.active:
            return False, "killswitch"
        params = self._runtime.params_for(asset)
        if self.daily_pnl(asset) <= -abs(params.daily_loss_limit_usd):
            return False, "daily_loss_limit"
        if asset:
            # Portfolio-level guard keeps a bad day in one asset from being
            # hidden by a good day in another.
            global_params = self._runtime.params
            if self.daily_pnl(None) <= -abs(global_params.daily_loss_limit_usd):
                return False, "daily_loss_limit_global"
        return True, ""
