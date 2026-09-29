"""Breakeven netto (fee + funding + chiusura stimata) e regola di inversione a 0,5%."""

import pytest

from fees import gross_pnl
from strategy import PositionBook, breakeven_net, qty_for_notional, reversal_signal

F = 0.00055
PCT = 0.5


def total_net_if_closed_at(b: PositionBook, p: float) -> float:
    return b.realized_net + gross_pnl(b.side, b.avg_entry, p, b.qty) - F * p * b.qty


@pytest.mark.parametrize("side", ["sell", "buy"])
def test_breakeven_closes_episode_at_exactly_zero(side):
    b = PositionBook()
    b.apply_fill(side, 300, 0.33, F * 300 * 0.33)
    b.apply_fill(side, 300, 0.34, F * 300 * 0.34)
    b.funding = -0.004
    be = breakeven_net(b, F)
    assert total_net_if_closed_at(b, be) == pytest.approx(0.0, abs=1e-12)


def test_breakeven_includes_realized_part_of_a_reduced_position():
    b = PositionBook()
    b.apply_fill("sell", 300, 0.34, F * 300 * 0.34)
    b.apply_fill("buy", 100, 0.33, F * 100 * 0.33)  # realizes +1.0 on 100 TRX
    be = breakeven_net(b, F)
    assert be > b.avg_entry * (1 - 2 * F)            # realized profit pushes the short BE UP
    assert total_net_if_closed_at(b, be) == pytest.approx(0.0, abs=1e-12)


def test_short_breakeven_is_below_entry_long_above():
    s, l = PositionBook(), PositionBook()
    s.apply_fill("sell", 100, 0.30, F * 30)
    l.apply_fill("buy", 100, 0.30, F * 30)
    assert breakeven_net(s, F) < 0.30 < breakeven_net(l, F)


def test_short_reverses_to_long_only_past_half_percent():
    b = PositionBook()
    b.apply_fill("sell", 100, 0.30, F * 30)
    trigger = breakeven_net(b, F) * (1 - PCT / 100)
    assert reversal_signal("short", b, trigger + 1e-6, PCT, F) is None
    assert reversal_signal("short", b, trigger, PCT, F) == "long"
    assert reversal_signal("long", b, trigger, PCT, F) is None  # already firing long


def test_long_reverses_to_short_only_past_half_percent():
    b = PositionBook()
    b.apply_fill("buy", 100, 0.30, F * 30)
    trigger = breakeven_net(b, F) * (1 + PCT / 100)
    assert reversal_signal("long", b, trigger - 1e-6, PCT, F) is None
    assert reversal_signal("long", b, trigger, PCT, F) == "short"
    assert reversal_signal("short", b, trigger, PCT, F) is None


def test_losing_position_never_reverses():
    b = PositionBook()
    b.apply_fill("sell", 100, 0.30, F * 30)
    assert reversal_signal("short", b, 0.40, PCT, F) is None     # short deep in loss: keeps selling
    assert reversal_signal("short", PositionBook(), 0.30, PCT, F) is None  # flat


def test_qty_rounding_trx():
    assert qty_for_notional(33.3, 0.3347, qty_step=1, min_qty=1) == 99   # 99.49 -> 99
    assert qty_for_notional(33.5, 0.3347, qty_step=1, min_qty=1) == 100  # 100.09 -> 100
    assert qty_for_notional(0.1, 0.3347, qty_step=1, min_qty=1) == 1


def test_qty_never_below_min_order_value():
    # 5 USDC at 0.3343: nearest step would be 15 TRX = 5.01, below the 5.05 target -> round UP to 16
    assert qty_for_notional(5.0, 0.3343, 1, 1, min_notional=5.05) == 16
    # 5 USDC at 0.30: 17 TRX = 5.10 already above -> unchanged
    assert qty_for_notional(5.0, 0.30, 1, 1, min_notional=5.05) == 17
    # rounding DOWN below the minimum (14.4 -> 14 TRX = 4.82) is corrected up
    assert qty_for_notional(5.0, 0.3472, 1, 1, min_notional=5.0) * 0.3472 >= 5.0
