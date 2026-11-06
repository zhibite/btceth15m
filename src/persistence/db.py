"""SQLite persistence: windows, signals, orders, fills, positions, settings."""
from __future__ import annotations

import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from src.market.models import PaperPosition, SignalSnapshot, WindowMarket

SCHEMA = """
CREATE TABLE IF NOT EXISTS windows (
  asset           TEXT NOT NULL,
  start_ts        INTEGER NOT NULL,
  end_ts          INTEGER NOT NULL,
  slug            TEXT,
  condition_id    TEXT,
  up_token        TEXT,
  down_token      TEXT,
  price_to_beat   REAL,
  final_price     REAL,
  outcome         TEXT,
  volume          REAL DEFAULT 0,
  tick_size       REAL DEFAULT 0.01,
  twap_lookback   INTEGER DEFAULT 60,
  p0_source       TEXT DEFAULT 'pending',
  status          TEXT DEFAULT 'open',
  discovered_at   TEXT,
  settled_at      TEXT,
  PRIMARY KEY (asset, start_ts)
);

CREATE TABLE IF NOT EXISTS signals (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  ts          REAL NOT NULL,
  asset       TEXT NOT NULL,
  start_ts    INTEGER NOT NULL,
  side        TEXT,
  fair        REAL,
  ask         REAL,
  edge        REAL,
  fee         REAL,
  shares      REAL,
  reason      TEXT,
  acted       INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS orders (
  id          TEXT PRIMARY KEY,
  ts          REAL NOT NULL,
  asset       TEXT NOT NULL,
  start_ts    INTEGER NOT NULL,
  token_id    TEXT,
  side        TEXT,
  price       REAL,
  size        REAL,
  notional    REAL,
  status      TEXT,
  mode        TEXT DEFAULT 'paper',
  reason      TEXT
);

CREATE TABLE IF NOT EXISTS fills (
  id          TEXT PRIMARY KEY,
  order_id    TEXT,
  ts          REAL NOT NULL,
  asset       TEXT NOT NULL,
  start_ts    INTEGER NOT NULL,
  token_id    TEXT,
  side        TEXT,
  price       REAL,
  size        REAL,
  fee         REAL DEFAULT 0,
  role        TEXT
);

CREATE TABLE IF NOT EXISTS positions (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  asset           TEXT NOT NULL,
  start_ts        INTEGER NOT NULL,
  end_ts          INTEGER NOT NULL,
  token_id        TEXT NOT NULL,
  side            TEXT NOT NULL,
  size            REAL NOT NULL,
  avg_price       REAL NOT NULL,
  cost            REAL NOT NULL,
  fees            REAL NOT NULL,
  entry_ts        REAL NOT NULL,
  price_to_beat   REAL NOT NULL,
  status          TEXT DEFAULT 'open',
  outcome         TEXT,
  payout          REAL DEFAULT 0,
  pnl             REAL DEFAULT 0,
  settled_ts      REAL,
  UNIQUE(asset, start_ts)
);

CREATE TABLE IF NOT EXISTS settings (
  key   TEXT PRIMARY KEY,
  value TEXT
);

CREATE INDEX IF NOT EXISTS idx_windows_start ON windows(start_ts);
CREATE INDEX IF NOT EXISTS idx_signals_ts ON signals(ts);
CREATE INDEX IF NOT EXISTS idx_orders_ts ON orders(ts);
CREATE INDEX IF NOT EXISTS idx_fills_ts ON fills(ts);
CREATE INDEX IF NOT EXISTS idx_positions_settled ON positions(settled_ts);
"""


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _day_bounds(date_str: str) -> tuple[float, float]:
    d = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    start = d.timestamp()
    return start, start + 86400


class Database:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        with self._lock:
            self.conn.executescript(SCHEMA)
            self.conn.commit()

    def close(self) -> None:
        with self._lock:
            self.conn.commit()
            self.conn.close()

    # ── low-level ────────────────────────────────────────────────────────
    def _exec(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self.conn.execute(sql, params)
            self.conn.commit()
            return cur

    def _query(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def _one(self, sql: str, params: tuple = ()) -> Optional[dict[str, Any]]:
        rows = self._query(sql, params)
        return rows[0] if rows else None

    # ── windows ──────────────────────────────────────────────────────────
    def upsert_window(self, market: WindowMarket) -> None:
        self._exec(
            """
            INSERT INTO windows (
              asset, start_ts, end_ts, slug, condition_id, up_token, down_token,
              price_to_beat, final_price, outcome, volume, tick_size,
              twap_lookback, p0_source, status, discovered_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(asset, start_ts) DO UPDATE SET
              end_ts=excluded.end_ts,
              slug=excluded.slug,
              condition_id=excluded.condition_id,
              up_token=excluded.up_token,
              down_token=excluded.down_token,
              price_to_beat=COALESCE(excluded.price_to_beat, windows.price_to_beat),
              final_price=COALESCE(excluded.final_price, windows.final_price),
              outcome=COALESCE(excluded.outcome, windows.outcome),
              volume=excluded.volume,
              tick_size=excluded.tick_size,
              twap_lookback=excluded.twap_lookback,
              p0_source=excluded.p0_source
            """,
            (
                market.asset,
                market.start_ts,
                market.end_ts,
                market.slug,
                market.condition_id,
                market.up_token,
                market.down_token,
                market.price_to_beat,
                market.final_price,
                market.outcome,
                market.volume,
                market.tick_size,
                market.twap_lookback,
                market.p0_source,
                "open",
                _iso_now(),
            ),
        )

    def set_window_p0(self, asset: str, start_ts: int, p0: float, source: str) -> None:
        self._exec(
            "UPDATE windows SET price_to_beat=?, p0_source=? WHERE asset=? AND start_ts=?",
            (p0, source, asset, int(start_ts)),
        )

    def set_window_final(
        self, asset: str, start_ts: int, final_price: Optional[float], outcome: str
    ) -> None:
        self._exec(
            "UPDATE windows SET final_price=?, outcome=?, status='settled', settled_at=? "
            "WHERE asset=? AND start_ts=?",
            (final_price, outcome, _iso_now(), asset, int(start_ts)),
        )

    # ── signals / orders / fills ─────────────────────────────────────────
    def insert_signal(self, sig: SignalSnapshot) -> int:
        cur = self._exec(
            "INSERT INTO signals (ts, asset, start_ts, side, fair, ask, edge, fee, shares, reason, acted) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                sig.ts,
                sig.asset,
                sig.start_ts,
                sig.side,
                sig.fair,
                sig.ask,
                sig.edge,
                sig.fee,
                sig.shares,
                sig.reason,
                1 if sig.acted else 0,
            ),
        )
        return int(cur.lastrowid or 0)

    def insert_order(
        self,
        order_id: str,
        ts: float,
        asset: str,
        start_ts: int,
        token_id: str,
        side: str,
        price: float,
        size: float,
        notional: float,
        status: str,
        mode: str = "paper",
        reason: str = "",
    ) -> None:
        self._exec(
            "INSERT OR REPLACE INTO orders "
            "(id, ts, asset, start_ts, token_id, side, price, size, notional, status, mode, reason) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                order_id,
                ts,
                asset,
                int(start_ts),
                token_id,
                side,
                price,
                size,
                notional,
                status,
                mode,
                reason,
            ),
        )

    def insert_fill(
        self,
        fill_id: str,
        order_id: str,
        ts: float,
        asset: str,
        start_ts: int,
        token_id: str,
        side: str,
        price: float,
        size: float,
        fee: float,
        role: str,
    ) -> None:
        self._exec(
            "INSERT OR REPLACE INTO fills "
            "(id, order_id, ts, asset, start_ts, token_id, side, price, size, fee, role) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                fill_id,
                order_id,
                ts,
                asset,
                int(start_ts),
                token_id,
                side,
                price,
                size,
                fee,
                role,
            ),
        )

    # ── positions ────────────────────────────────────────────────────────
    def insert_position(self, pos: PaperPosition) -> int:
        try:
            cur = self._exec(
                "INSERT INTO positions "
                "(asset, start_ts, end_ts, token_id, side, size, avg_price, cost, fees, "
                " entry_ts, price_to_beat, status) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,'open')",
                (
                    pos.asset,
                    pos.start_ts,
                    pos.end_ts,
                    pos.token_id,
                    pos.side,
                    pos.size,
                    pos.avg_price,
                    pos.cost,
                    pos.fees,
                    pos.entry_ts,
                    pos.price_to_beat,
                ),
            )
            return int(cur.lastrowid or 0)
        except sqlite3.IntegrityError:
            row = self._one(
                "SELECT id FROM positions WHERE asset=? AND start_ts=?",
                (pos.asset, pos.start_ts),
            )
            return int(row["id"]) if row else 0

    def settle_position(
        self, position_id: int, outcome: str, payout: float, pnl: float
    ) -> None:
        self._exec(
            "UPDATE positions SET status='settled', outcome=?, payout=?, pnl=?, settled_ts=? "
            "WHERE id=?",
            (outcome, payout, pnl, time.time(), int(position_id)),
        )

    def open_positions(self) -> list[dict[str, Any]]:
        return self._query("SELECT * FROM positions WHERE status='open' ORDER BY start_ts")

    def recent_positions(self, limit: int = 50) -> list[dict[str, Any]]:
        return self._query(
            "SELECT * FROM positions ORDER BY id DESC LIMIT ?", (int(limit),)
        )

    # ── queries for the dashboard ────────────────────────────────────────
    def recent_windows(self, limit: int = 50) -> list[dict[str, Any]]:
        return self._query(
            "SELECT * FROM windows ORDER BY start_ts DESC LIMIT ?", (int(limit),)
        )

    def recent_signals(self, limit: int = 100) -> list[dict[str, Any]]:
        return self._query(
            "SELECT * FROM signals ORDER BY id DESC LIMIT ?", (int(limit),)
        )

    def recent_orders(self, limit: int = 100) -> list[dict[str, Any]]:
        return self._query(
            "SELECT * FROM orders ORDER BY ts DESC LIMIT ?", (int(limit),)
        )

    def recent_fills(self, limit: int = 100) -> list[dict[str, Any]]:
        return self._query(
            "SELECT * FROM fills ORDER BY ts DESC LIMIT ?", (int(limit),)
        )

    def daily_realized(self, date_str: str) -> dict[str, Any]:
        start, end = _day_bounds(date_str)
        row = self._one(
            "SELECT COALESCE(SUM(pnl),0) AS pnl, COUNT(*) AS n, "
            "COALESCE(SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END),0) AS wins, "
            "COALESCE(SUM(CASE WHEN pnl <= 0 THEN 1 ELSE 0 END),0) AS losses "
            "FROM positions WHERE status='settled' AND settled_ts>=? AND settled_ts<?",
            (start, end),
        )
        return row or {"pnl": 0.0, "n": 0, "wins": 0, "losses": 0}

    def total_realized(self) -> dict[str, Any]:
        row = self._one(
            "SELECT COALESCE(SUM(pnl),0) AS pnl, COUNT(*) AS n, "
            "COALESCE(SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END),0) AS wins, "
            "COALESCE(SUM(CASE WHEN pnl <= 0 THEN 1 ELSE 0 END),0) AS losses "
            "FROM positions WHERE status='settled'"
        )
        return row or {"pnl": 0.0, "n": 0, "wins": 0, "losses": 0}

    def summary(self) -> dict[str, Any]:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        return {"today": self.daily_realized(today), "total": self.total_realized()}

    def pnl_by_day(self, days: int = 7) -> list[dict[str, Any]]:
        return self._query(
            "SELECT strftime('%Y-%m-%d', settled_ts, 'unixepoch') AS day, "
            "COALESCE(SUM(pnl),0) AS pnl, COUNT(*) AS n, "
            "COALESCE(SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END),0) AS wins "
            "FROM positions WHERE status='settled' AND settled_ts IS NOT NULL "
            "GROUP BY day ORDER BY day DESC LIMIT ?",
            (int(days),),
        )

    # ── settings ─────────────────────────────────────────────────────────
    def get_setting(self, key: str) -> Optional[str]:
        row = self._one("SELECT value FROM settings WHERE key=?", (key,))
        return row["value"] if row else None

    def set_setting(self, key: str, value: str) -> None:
        self._exec(
            "INSERT INTO settings (key, value) VALUES (?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
