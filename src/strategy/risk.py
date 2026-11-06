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
        self._pnl_cache_ts = 0.0
        self._pnl_cache = 0.0

    def daily_pnl(self) -> float:
        now = time.time()
        if now - self._pnl_cache_ts < 2.0:
            return self._pnl_cache
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        self._pnl_cache = float(self._db.daily_realized(today).get("pnl") or 0.0)
        self._pnl_cache_ts = now
        return self._pnl_cache

    def can_enter(self) -> tuple[bool, str]:
        if self._kill.active:
            return False, "killswitch"
        params = self._runtime.params
        if self.daily_pnl() <= -abs(params.daily_loss_limit_usd):
            return False, "daily_loss_limit"
        return True, ""
