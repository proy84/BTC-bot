"""
strategy.py

Config and pure decision logic for the accumulation bot -- one-way, one
linear perpetual (currently ETH/USDT, 150x). No network/exchange dependency by
design: exchange I/O lives in `exchange.py`, orchestration in `main.py`.

Rules:
  - At startup the bot opens a SHORT of `base notional` = `percentage`% (1%)
    of the account's total equity, RE-READ BEFORE EVERY ORDER (current
    config), never below the exchange minimum (0.01 ETH on ETHUSDT). With
    equity sizing off: fixed `base_notional_usd` (0 = exchange minimum).
  - Every timeframe (1m) -- counted from the PREVIOUS order, not aligned to
    the clock -- it fires one more market order of base notional in the
    ACTIVE DIRECTION (initially short).
  - One-way mode: there is a single NET position. Orders in the direction
    opposite to the position reduce it (and flip it if they exceed it).
  - Reversal, symmetric on price vs BREAK-EVEN = plain average entry of the
    net position (fees and funding NOT included):
        price <= BE * (1 - reversal_pct/100) -> fire LONG every minute
        price >= BE * (1 + reversal_pct/100) -> fire SHORT every minute
        in between                            -> keep the current direction
  - No take profit and no size limit for now: orders fire forever.

A POSITION EPISODE starts when the position opens from flat and ends when it
returns to flat (or crosses zero, which closes it and opens a new one on the
other side at the fill price -- so the new side's break-even starts fresh).
Realized PnL, fees and funding of each episode are tracked for the history.
"""

from __future__ import annotations

import json
import math
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

from fees import LONG, SHORT, gross_pnl

# Loads .env (BYBIT_API_KEY / BYBIT_API_SECRET / USE_TESTNET / TELEGRAM_*)
# into the process environment. No-op if the file doesn't exist.
load_dotenv()

BOT_NAME = "ETH bot"
QTY_EPS = 1e-9


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def parse_timeframe_seconds(timeframe: str) -> int:
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    if len(timeframe) < 2 or timeframe[-1] not in units:
        raise ValueError(f"Unsupported timeframe: {timeframe!r}")
    value = int(timeframe[:-1])
    if value <= 0:
        raise ValueError(f"Timeframe must be positive: {timeframe!r}")
    return value * units[timeframe[-1]]


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class StrategyConfig:
    symbol: str
    leverage: int
    margin_mode: str
    timeframe: str
    timeframe_sec: int
    initial_direction: str
    reversal_pct: float
    taker_rate: float
    maker_rate: float
    base_notional_usd: float
    equity_based_sizing_enabled: bool
    equity_based_sizing_percentage: float
    notifier_enabled: bool
    tick_poll_interval_sec: float
    funding_poll_interval_sec: float
    trade_history_path: str
    state_export_path: str
    runtime_state_path: str
    exchange_options: dict
    api_key: str
    api_secret: str
    use_testnet: bool

    @property
    def base_asset(self) -> str:
        return self.symbol.split("/")[0]            # "ETH" for "ETH/USDT:USDT"

    @property
    def settle_coin(self) -> str:
        return self.symbol.split(":")[-1]           # "USDT" for "ETH/USDT:USDT"

    @staticmethod
    def load(path: str | Path) -> "StrategyConfig":
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        direction = raw.get("initial_direction", SHORT)
        if direction not in (SHORT, LONG):
            raise ValueError(f"initial_direction must be 'short' or 'long', got {direction!r}")
        return StrategyConfig(
            symbol=raw["symbol"],
            leverage=int(raw["leverage"]),
            margin_mode=raw.get("margin_mode", "cross"),
            timeframe=raw["timeframe"],
            timeframe_sec=parse_timeframe_seconds(raw["timeframe"]),
            initial_direction=direction,
            reversal_pct=float(raw["reversal_pct"]),
            taker_rate=float(raw["fees"]["taker_rate"]),
            maker_rate=float(raw["fees"].get("maker_rate", 0.0)),
            base_notional_usd=float(raw.get("base_notional_usd", 5.0)),
            equity_based_sizing_enabled=bool(raw.get("equity_based_sizing", {}).get("enabled", True)),
            equity_based_sizing_percentage=float(raw.get("equity_based_sizing", {}).get("percentage", 1.0)),
            notifier_enabled=bool(raw.get("notifier", {}).get("enabled", False)),
            tick_poll_interval_sec=float(raw["polling"]["tick_poll_interval_sec"]),
            funding_poll_interval_sec=float(raw["polling"]["funding_poll_interval_sec"]),
            trade_history_path=raw["paths"]["trade_history_path"],
            state_export_path=raw["paths"]["state_export_path"],
            runtime_state_path=raw["paths"]["runtime_state_path"],
            exchange_options=raw.get("exchange", {}).get("options", {}),
            api_key=os.environ.get("BYBIT_API_KEY", ""),
            api_secret=os.environ.get("BYBIT_API_SECRET", ""),
            use_testnet=_env_bool("USE_TESTNET", True),
        )


# --------------------------------------------------------------------------- #
# One-way position book
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class ClosedEpisode:
    """A position episode that just returned to flat (or crossed zero)."""
    episode_id: int
    side: str
    realized_gross: float
    fees: float
    funding: float

    @property
    def net(self) -> float:
        return self.realized_gross - self.fees + self.funding


@dataclass
class PositionBook:
    """Local mirror of the one-way NET position, built from our own fills.

    Reductions keep the average entry unchanged and realize PnL on the
    reduced quantity (standard one-way netting). An order larger than the
    open position closes the episode and opens a new one on the other side
    with the remainder, splitting the order's fee pro rata."""
    side: Optional[str] = None   # LONG | SHORT | None (flat)
    qty: float = 0.0
    avg_entry: float = 0.0
    realized_gross: float = 0.0  # current episode
    fees: float = 0.0            # current episode, positive = paid
    funding: float = 0.0         # current episode, positive = received
    episode_id: int = 1

    @property
    def is_flat(self) -> bool:
        return self.side is None or self.qty <= QTY_EPS

    @property
    def realized_net(self) -> float:
        return self.realized_gross - self.fees + self.funding

    @property
    def notional(self) -> float:
        return self.avg_entry * self.qty

    def _open(self, side: str, qty: float, price: float, fee: float) -> None:
        self.side, self.qty, self.avg_entry = side, qty, price
        self.fees += fee

    def _close_episode(self) -> ClosedEpisode:
        closed = ClosedEpisode(self.episode_id, self.side, self.realized_gross, self.fees, self.funding)
        self.side, self.qty, self.avg_entry = None, 0.0, 0.0
        self.realized_gross = self.fees = self.funding = 0.0
        self.episode_id += 1
        return closed

    def apply_fill(self, order_side: str, qty: float, price: float, fee: float) -> Optional[ClosedEpisode]:
        """order_side: "buy" | "sell". Returns the episode closed by this fill, if any."""
        if qty <= QTY_EPS:
            return None
        fill_dir = LONG if order_side == "buy" else SHORT
        if self.is_flat:
            self._open(fill_dir, qty, price, fee)
            return None
        if fill_dir == self.side:
            self.avg_entry = (self.avg_entry * self.qty + price * qty) / (self.qty + qty)
            self.qty += qty
            self.fees += fee
            return None

        reduce = min(qty, self.qty)
        self.realized_gross += gross_pnl(self.side, self.avg_entry, price, reduce)
        self.fees += fee * reduce / qty
        self.qty -= reduce
        if self.qty > QTY_EPS:
            return None
        closed = self._close_episode()
        remainder = qty - reduce
        if remainder > QTY_EPS:
            self._open(fill_dir, remainder, price, fee * remainder / qty)
        return closed

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "PositionBook":
        return PositionBook(**d)


def breakeven(book: PositionBook) -> Optional[float]:
    """Break-even used for the reversal rule: the plain AVERAGE ENTRY price of
    the open net position -- fees and funding deliberately NOT included (they
    are still tracked in the episode's realized net)."""
    return None if book.is_flat else book.avg_entry


def reversal_thresholds(book: PositionBook, reversal_pct: float) -> Optional[tuple]:
    """(long_below, short_above): fire LONG at/below the first price, SHORT
    at/above the second. None when flat."""
    be = breakeven(book)
    if be is None:
        return None
    return be * (1.0 - reversal_pct / 100.0), be * (1.0 + reversal_pct / 100.0)


def reversal_signal(mode: str, book: PositionBook, price: float, reversal_pct: float) -> Optional[str]:
    """Symmetric rule on price vs break-even, whatever the position's side:
        price <= BE * (1 - pct)  -> fire LONG
        price >= BE * (1 + pct)  -> fire SHORT
        in between               -> keep the current direction
    Returns the new direction only if it differs from `mode`, else None."""
    th = reversal_thresholds(book, reversal_pct)
    if th is None:
        return None
    long_below, short_above = th
    if price <= long_below:
        target = LONG
    elif price >= short_above:
        target = SHORT
    else:
        return None
    return target if target != mode else None


def order_side_for(mode: str) -> str:
    return "sell" if mode == SHORT else "buy"


# --------------------------------------------------------------------------- #
# Sizing / scheduling helpers
# --------------------------------------------------------------------------- #

def base_notional_from_equity(equity: float, percentage: float, fallback: float, min_notional: float) -> float:
    """Base notional = percentage% of total equity (or `fallback` if equity is
    unknown), never below the exchange minimum."""
    base = equity * percentage / 100.0 if equity > 0 else fallback
    return max(base, min_notional)


def qty_for_notional(notional: float, price: float, qty_step: float, min_qty: float,
                     min_notional: float = 0.0) -> float:
    """Order quantity for a target notional, rounded to the NEAREST exchange
    step (half up), never below the exchange minimum quantity -- and, when
    `min_notional` is given, rounded UP as needed so that qty * price never
    falls below it (Bybit rejects orders under the minimum order value)."""
    if price <= 0 or notional <= 0:
        return 0.0

    def to_step(x: float, up: bool) -> float:
        if qty_step <= 0:
            return x
        n = math.ceil(x / qty_step - 1e-9) if up else math.floor(x / qty_step + 0.5 + 1e-9)
        decimals = max(0, -int(math.floor(math.log10(qty_step)))) if qty_step < 1 else 0
        return round(n * qty_step, decimals + 2)

    qty = to_step(notional / price, up=False)
    if min_notional > 0 and qty * price < min_notional:
        qty = to_step(min_notional / price, up=True)
    return max(qty, min_qty)


@dataclass
class BotState:
    """Persisted (runtime_state_path) so a restart resumes where it left off."""
    mode: str
    base_notional: float
    book: PositionBook
    last_order_ts: float = 0.0  # unix time of the last order attempt; the next fires timeframe_sec later
    orders_count: int = 0

    def to_dict(self) -> dict:
        return {"mode": self.mode, "base_notional": self.base_notional, "book": self.book.to_dict(),
                "last_order_ts": self.last_order_ts, "orders_count": self.orders_count}

    @staticmethod
    def from_dict(d: dict) -> "BotState":
        return BotState(mode=d["mode"], base_notional=float(d["base_notional"]),
                        book=PositionBook.from_dict(d["book"]), last_order_ts=float(d.get("last_order_ts", 0.0)),
                        orders_count=int(d.get("orders_count", 0)))
