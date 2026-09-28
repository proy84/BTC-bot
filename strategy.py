"""
strategy.py

Config and pure decision logic for BTC bot -- a two-leg hedge:
  - SHORT leg: BTC/USDT perpetual (settles in USDT)
  - LONG  leg: BTC/USDC perpetual (settles in USDC)
both opened together at market, same leverage.

No network/exchange dependency by design: exchange I/O lives in
`exchange.py`, orchestration in `main.py`, fee/PnL math in `fees.py`.

Vocabulary:
  - CYCLE: both legs open -> one leg hits its take profit -> CLOSE ALL.
  - SEQUENCE: the run of cycles from a reset to the base notional up to the
    cycle whose close leaves the sequence in net profit. Per-coin realized
    net (USDT from the short leg, USDC from the long leg) is accumulated over
    the whole sequence.

Rules:
  - Take profit: a leg hits TP when ITS gross PnL minus the fees (open + close)
    of BOTH legs plus the realized funding of BOTH legs is >= take_profit_net_pct
    of that leg's notional (`check_take_profit`).
  - After CLOSE ALL (`decide_after_close`):
      a) sequence net USDT + USDC > 0 -> the winning coin repays the losing
         coin's loss (spot conversion), the rest buys BTC spot, and a new
         sequence starts at the base notional on both legs;
      b) otherwise -> reopen with CUMULATIVE per-leg multipliers: the leg that
         hit TP = its previous notional x winner_multiplier (2.0), the other
         = its previous notional x loser_multiplier (1.5).
"""

from __future__ import annotations

import json
import math
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, Mapping, Optional

from dotenv import load_dotenv

from fees import LONG, SHORT, FeeSchedule, LegPosition, TwoLegNet, two_leg_net

# Loads .env (BYBIT_API_KEY / BYBIT_API_SECRET / USE_TESTNET / TELEGRAM_*)
# into the process environment. No-op if the file doesn't exist.
load_dotenv()

BOT_NAME = "BTC bot"
SHORT_LEG = "short"
LONG_LEG = "long"


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class LegConfig:
    name: str         # "short" | "long"
    symbol: str       # CCXT perpetual symbol, e.g. "BTC/USDT:USDT"
    side: str         # SHORT | LONG
    settle_coin: str  # "USDT" | "USDC"
    spot_btc_symbol: str  # spot pair used to buy BTC with this coin, e.g. "BTC/USDT"


@dataclass(frozen=True)
class StrategyConfig:
    legs: Dict[str, LegConfig]
    leverage: int
    margin_mode: str
    take_profit_net_pct: float
    taker_rate: float
    maker_rate: float
    base_notional_usd: float
    equity_based_sizing_enabled: bool
    equity_based_sizing_percentage: float
    winner_multiplier: float
    loser_multiplier: float
    max_multiplier_steps: Optional[int]
    settlement_enabled: bool
    stable_conversion_symbol: str
    spot_min_order_value: float
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
    def fee_schedule(self) -> FeeSchedule:
        return FeeSchedule(taker_rate=self.taker_rate, maker_rate=self.maker_rate)

    @staticmethod
    def load(path: str | Path) -> "StrategyConfig":
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        legs = {}
        for name, expected_side in ((SHORT_LEG, SHORT), (LONG_LEG, LONG)):
            leg = raw["legs"][name]
            side = leg.get("side", expected_side)
            if side != expected_side:
                raise ValueError(f"legs.{name}.side must be {expected_side!r}, got {side!r}")
            legs[name] = LegConfig(
                name=name,
                symbol=leg["symbol"],
                side=side,
                settle_coin=leg["settle_coin"],
                spot_btc_symbol=leg["spot_btc_symbol"],
            )
        max_steps = raw.get("max_multiplier_steps")
        settlement = raw.get("settlement", {})
        return StrategyConfig(
            legs=legs,
            leverage=int(raw["leverage"]),
            margin_mode=raw.get("margin_mode", "cross"),
            take_profit_net_pct=float(raw["take_profit_net_pct"]),
            taker_rate=float(raw["fees"]["taker_rate"]),
            maker_rate=float(raw["fees"].get("maker_rate", 0.0)),
            base_notional_usd=float(raw.get("base_notional_usd", 1.0)),
            equity_based_sizing_enabled=bool(raw.get("equity_based_sizing", {}).get("enabled", True)),
            equity_based_sizing_percentage=float(raw.get("equity_based_sizing", {}).get("percentage", 1.0)),
            winner_multiplier=float(raw["multipliers"]["winner"]),
            loser_multiplier=float(raw["multipliers"]["loser"]),
            max_multiplier_steps=None if max_steps is None else int(max_steps),
            settlement_enabled=bool(settlement.get("enabled", True)),
            stable_conversion_symbol=settlement.get("stable_conversion_symbol", "USDC/USDT"),
            spot_min_order_value=float(settlement.get("spot_min_order_value", 5.0)),
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
# Take profit
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class TakeProfitSignal:
    leg: str
    breakdown: TwoLegNet


def evaluate_legs(legs: Mapping[str, LegPosition], marks: Mapping[str, float],
                  schedule: FeeSchedule) -> Dict[str, TwoLegNet]:
    return {name: two_leg_net(legs, marks, name, schedule) for name in legs}


def check_take_profit(legs: Mapping[str, LegPosition], marks: Mapping[str, float],
                      schedule: FeeSchedule, tp_net_pct: float) -> Optional[TakeProfitSignal]:
    """Returns the leg that hit its take profit (net of BOTH legs' fees and
    funding >= tp_net_pct% of its own notional), or None. Requires both legs
    open. If both qualify at once (only possible with a large funding credit),
    the one further past the target wins."""
    if set(legs) != {SHORT_LEG, LONG_LEG}:
        return None
    hits = []
    for name, bd in evaluate_legs(legs, marks, schedule).items():
        if bd.notional > 0 and bd.net_pnl >= bd.notional * tp_net_pct / 100.0:
            hits.append((bd.net_pct, name, bd))
    if not hits:
        return None
    _, name, bd = max(hits)
    return TakeProfitSignal(leg=name, breakdown=bd)


# --------------------------------------------------------------------------- #
# Sequence state, multipliers and the post-close decision
# --------------------------------------------------------------------------- #

@dataclass
class SequenceState:
    """Persisted (btc_bot_state.json) so a restart never loses where the
    multiplier sequence is."""
    sequence_id: int = 1
    step: int = 0  # 0 = base notional; +1 on every multiplier reopen
    base_notional: float = 0.0
    notionals: Dict[str, float] = field(default_factory=dict)  # target notional per leg
    net_by_coin: Dict[str, float] = field(default_factory=dict)  # realized net over the sequence
    cycle_id: int = 1

    @staticmethod
    def new(base_notional: float, sequence_id: int = 1, cycle_id: int = 1) -> "SequenceState":
        return SequenceState(
            sequence_id=sequence_id, step=0, base_notional=base_notional,
            notionals={SHORT_LEG: base_notional, LONG_LEG: base_notional},
            net_by_coin={}, cycle_id=cycle_id,
        )

    def add_cycle_result(self, net_by_coin: Mapping[str, float]) -> None:
        for coin, amount in net_by_coin.items():
            self.net_by_coin[coin] = self.net_by_coin.get(coin, 0.0) + amount

    @property
    def total_net(self) -> float:
        return sum(self.net_by_coin.values())

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: Mapping) -> "SequenceState":
        return SequenceState(
            sequence_id=int(d["sequence_id"]),
            step=int(d["step"]),
            base_notional=float(d["base_notional"]),
            notionals={k: float(v) for k, v in d["notionals"].items()},
            net_by_coin={k: float(v) for k, v in d.get("net_by_coin", {}).items()},
            cycle_id=int(d.get("cycle_id", 1)),
        )


def next_round_notionals(notionals: Mapping[str, float], tp_leg: str,
                         winner_multiplier: float, loser_multiplier: float) -> Dict[str, float]:
    """CUMULATIVE multipliers: the leg that hit TP = its previous notional x
    winner_multiplier, every other leg = its previous notional x
    loser_multiplier. Always, no exceptions."""
    if tp_leg not in notionals:
        raise ValueError(f"Unknown take-profit leg: {tp_leg!r}")
    return {
        name: value * (winner_multiplier if name == tp_leg else loser_multiplier)
        for name, value in notionals.items()
    }


NEW_SEQUENCE = "new_sequence"
MULTIPLY = "multiply"
STOP = "stop"


@dataclass(frozen=True)
class Decision:
    action: str                       # NEW_SEQUENCE | MULTIPLY | STOP
    next_notionals: Dict[str, float]  # empty for NEW_SEQUENCE (base is re-read from equity) and STOP
    winner_coin: Optional[str] = None
    loser_coin: Optional[str] = None
    repay_amount: float = 0.0         # losing coin's sequence loss, to be covered by the winner
    btc_buy_amount: float = 0.0       # rest of the winner's profit, to buy BTC spot


def decide_after_close(seq: SequenceState, tp_leg: str, winner_multiplier: float,
                       loser_multiplier: float, max_multiplier_steps: Optional[int]) -> Decision:
    """Call AFTER `seq.add_cycle_result(...)` for the cycle just closed.

    a) The sequence is in net profit (USDT + USDC > 0): the coin with the
       larger net is the winner; it repays the loser's loss (if any) and the
       rest buys BTC spot. New sequence at base notional.
    b) Otherwise: cumulative multipliers. If `max_multiplier_steps` is set and
       this reopen would exceed it -> STOP (None = no limit)."""
    if seq.total_net > 0 and seq.net_by_coin:
        winner = max(seq.net_by_coin, key=lambda c: seq.net_by_coin[c])
        losers = [c for c in seq.net_by_coin if c != winner]
        loser = losers[0] if losers else None
        repay = max(0.0, -seq.net_by_coin[loser]) if loser else 0.0
        return Decision(
            action=NEW_SEQUENCE, next_notionals={}, winner_coin=winner, loser_coin=loser,
            repay_amount=repay, btc_buy_amount=seq.net_by_coin[winner] - repay,
        )
    if max_multiplier_steps is not None and seq.step + 1 > max_multiplier_steps:
        return Decision(action=STOP, next_notionals={})
    return Decision(
        action=MULTIPLY,
        next_notionals=next_round_notionals(seq.notionals, tp_leg, winner_multiplier, loser_multiplier),
    )


# --------------------------------------------------------------------------- #
# Sizing helpers
# --------------------------------------------------------------------------- #

def effective_base_notional(equity: float, percentage: float, fallback: float,
                            min_notionals: Mapping[str, float]) -> float:
    """Base notional per leg = percentage% of total equity (or `fallback` if
    equity is unknown), raised to the largest exchange minimum among the legs
    so both legs can open with the same base."""
    base = equity * percentage / 100.0 if equity > 0 else fallback
    return max([base, *min_notionals.values()])


def qty_for_notional(notional: float, price: float, qty_step: float, min_qty: float) -> float:
    """Order quantity for a target notional, rounded to the NEAREST exchange
    step (half up) rather than truncated -- e.g. 1.5 x 0.001 BTC -> 0.002, not
    0.001 -- and never below the exchange minimum. Multipliers are applied to
    the target notional, never to the rounded qty, so the sequence stays
    exact however the rounding falls."""
    if price <= 0 or notional <= 0:
        return 0.0
    raw = notional / price
    if qty_step > 0:
        steps = math.floor(raw / qty_step + 0.5 + 1e-9)
        qty = steps * qty_step
        decimals = max(0, -int(math.floor(math.log10(qty_step)))) if qty_step < 1 else 0
        qty = round(qty, decimals + 2)
    else:
        qty = raw
    return max(qty, min_qty)


# --------------------------------------------------------------------------- #
# Spot settlement plan (repay via spot + BTC purchase)
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class SpotSettlementPlan:
    convert_amount: float  # winner coin spent to credit the loser coin (0 = none)
    btc_buy_amount: float  # winner coin spent on BTC spot (0 = none)
    notes: tuple = ()


def plan_spot_settlement(repay_amount: float, winner_profit: float,
                         min_order_value: float) -> SpotSettlementPlan:
    """Bybit Demo rejects /v5/account/repay (retCode 10032), so the losing
    coin's loss is covered by CREDITING that coin through a spot conversion
    from the winner -- on a Unified account, a credit on a coin with a
    negative balance offsets the borrow. Bybit spot orders have a minimum
    value (5 USDT/USDC): a smaller repay is rounded UP to the minimum (+2%
    margin; the excess simply stays on the losing coin's balance), and a BTC
    purchase below the minimum is skipped (the rest stays in the winner coin).
    Never spends more than the winner's profit."""
    notes = []
    convert = 0.0
    if repay_amount > 0:
        convert = max(repay_amount, min_order_value * 1.02)
        if convert > winner_profit:
            notes.append("conversione saltata: profitto vincente sotto il minimo ordine spot")
            convert = 0.0
    # If the conversion was skipped, the part of the profit that should have
    # covered the loss is still NOT spent on BTC.
    rest = max(0.0, winner_profit - (convert if convert > 0 else repay_amount))
    btc = rest if rest >= min_order_value else 0.0
    if 0 < rest < min_order_value:
        notes.append("acquisto BTC saltato: resto sotto il minimo ordine spot")
    return SpotSettlementPlan(convert_amount=convert, btc_buy_amount=btc, notes=tuple(notes))
