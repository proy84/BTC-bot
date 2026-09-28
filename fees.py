"""
fees.py

Fee, funding and PnL math for BTC bot (two-leg hedge: BTC/USDT SHORT +
BTC/USDC LONG). Pure functions, no exchange I/O.

Conventions:
  - Every amount is in the leg's own settlement coin (USDT for the short
    leg, USDC for the long leg). Where the two legs are summed together
    (the two-leg net used for the take profit) USDT and USDC are treated
    1:1 -- both are USD stablecoins, and the TP threshold is small enough
    that a sub-0.1% depeg is immaterial.
  - Funding cashflow is REALIZED funding read from the exchange's own
    settlement ledger (`ExchangeClient.fetch_realized_funding`), positive =
    received, negative = paid -- never estimated from a polled rate.
  - Fees are positive costs.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

LONG = "long"
SHORT = "short"


@dataclass(frozen=True)
class FeeSchedule:
    taker_rate: float
    maker_rate: float = 0.0

    def taker_fee_for_notional(self, notional: float) -> float:
        return abs(notional) * self.taker_rate


@dataclass
class LegPosition:
    """One open leg of the hedge, as the bot tracks it locally."""
    name: str            # "short" | "long" -- the leg's key in the config
    symbol: str          # e.g. "BTC/USDT:USDT"
    side: str            # SHORT | LONG
    settle_coin: str     # "USDT" | "USDC"
    qty: float
    entry_price: float
    open_fee: float
    funding: float = 0.0  # realized funding cashflow accumulated while open

    @property
    def notional(self) -> float:
        return self.entry_price * self.qty


def gross_pnl(side: str, entry_price: float, exit_price: float, qty: float) -> float:
    """Gross PnL of a linear perpetual leg: a SHORT profits when price falls,
    a LONG when it rises."""
    if side == SHORT:
        return (entry_price - exit_price) * qty
    if side == LONG:
        return (exit_price - entry_price) * qty
    raise ValueError(f"Unknown side: {side!r}")


def estimate_close_fee(leg: LegPosition, mark_price: float, schedule: FeeSchedule) -> float:
    """Taker fee the leg would pay to close at market at `mark_price`."""
    return schedule.taker_fee_for_notional(mark_price * leg.qty)


@dataclass(frozen=True)
class TwoLegNet:
    """Net result of ONE leg once the costs of BOTH legs are charged to it --
    the quantity the take profit is measured on."""
    leg: str
    gross_pnl: float        # this leg only
    open_fees: float        # both legs
    close_fees_est: float   # both legs, estimated at the current marks
    funding: float          # both legs (positive = received)
    net_pnl: float          # gross - open_fees - close_fees_est + funding
    notional: float         # this leg's entry notional
    net_pct: float          # net_pnl / notional * 100


def two_leg_net(legs: Mapping[str, LegPosition], marks: Mapping[str, float], leg_name: str,
                schedule: FeeSchedule) -> TwoLegNet:
    """Net PnL of `leg_name` after subtracting the opening AND (estimated)
    closing fees of EVERY leg, plus the realized funding of every leg. This
    is deliberately stricter than the leg's own net: a take profit on one leg
    closes both, so the winning leg has to pay for the whole round trip."""
    leg = legs[leg_name]
    gross = gross_pnl(leg.side, leg.entry_price, marks[leg_name], leg.qty)
    open_fees = sum(l.open_fee for l in legs.values())
    close_fees = sum(estimate_close_fee(l, marks[name], schedule) for name, l in legs.items())
    funding = sum(l.funding for l in legs.values())
    net = gross - open_fees - close_fees + funding
    notional = leg.notional
    return TwoLegNet(
        leg=leg_name,
        gross_pnl=gross,
        open_fees=open_fees,
        close_fees_est=close_fees,
        funding=funding,
        net_pnl=net,
        notional=notional,
        net_pct=(net / notional * 100.0) if notional > 0 else 0.0,
    )


def realized_leg_net(leg: LegPosition, exit_price: float, close_fee: float) -> float:
    """Realized net of a single closed leg, in its own settlement coin: what
    that coin's balance actually gained or lost on this cycle."""
    return gross_pnl(leg.side, leg.entry_price, exit_price, leg.qty) - leg.open_fee - close_fee + leg.funding
