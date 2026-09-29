"""
fees.py

Fee and PnL primitives for TRX bot. Pure functions, no exchange I/O.

Conventions:
  - Amounts are in the perpetual's settlement coin (USDC for TRXPERP).
  - Fees are positive costs.
  - Funding cashflow is REALIZED funding from the exchange ledger
    (`ExchangeClient.fetch_realized_funding`), positive = received.
"""

from __future__ import annotations

from dataclasses import dataclass

LONG = "long"
SHORT = "short"


@dataclass(frozen=True)
class FeeSchedule:
    taker_rate: float
    maker_rate: float = 0.0

    def taker_fee_for_notional(self, notional: float) -> float:
        return abs(notional) * self.taker_rate


def gross_pnl(side: str, entry_price: float, exit_price: float, qty: float) -> float:
    """Gross PnL of a linear perpetual position: a SHORT profits when price
    falls, a LONG when it rises."""
    if side == SHORT:
        return (entry_price - exit_price) * qty
    if side == LONG:
        return (exit_price - entry_price) * qty
    raise ValueError(f"Unknown side: {side!r}")
