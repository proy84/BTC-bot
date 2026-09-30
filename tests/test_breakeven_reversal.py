"""Breakeven = prezzo medio d'ingresso (SENZA fee) e regola di inversione
simmetrica a 0,5%: sotto BE-0,5% si sparano LONG, sopra BE+0,5% SHORT."""

import pytest

from strategy import PositionBook, breakeven, qty_for_notional, reversal_signal, reversal_thresholds

PCT = 0.5


def short_book(price=2700.0, qty=0.02, fee=0.03):
    b = PositionBook()
    b.apply_fill("sell", qty, price, fee)
    return b


def test_breakeven_is_plain_average_entry_without_fees():
    b = PositionBook()
    b.apply_fill("sell", 0.01, 2700.0, 5.0)     # huge fee on purpose
    b.apply_fill("sell", 0.01, 2720.0, 5.0)
    b.funding = -3.0
    assert breakeven(b) == pytest.approx(2710.0)   # fees and funding ignored
    assert breakeven(PositionBook()) is None


def test_breakeven_unchanged_by_partial_reduction():
    b = short_book(2700.0, 0.03)
    b.apply_fill("buy", 0.01, 2650.0, 0.01)
    assert breakeven(b) == pytest.approx(2700.0)


def test_thresholds():
    long_below, short_above = reversal_thresholds(short_book(2700.0), PCT)
    assert long_below == pytest.approx(2686.5)
    assert short_above == pytest.approx(2713.5)


def test_short_below_breakeven_switches_to_long():
    b = short_book(2700.0)
    assert reversal_signal("short", b, 2686.6, PCT) is None
    assert reversal_signal("short", b, 2686.5, PCT) == "long"
    assert reversal_signal("long", b, 2686.5, PCT) is None       # already firing long


def test_price_above_breakeven_switches_to_short_whatever_the_side():
    # long position in profit
    b = PositionBook()
    b.apply_fill("buy", 0.02, 2700.0, 0.03)
    assert reversal_signal("long", b, 2713.4, PCT) is None
    assert reversal_signal("long", b, 2713.5, PCT) == "short"
    # short position still being reduced by longs, price back ABOVE its BE +0.5% -> back to short
    s = short_book(2700.0)
    assert reversal_signal("long", s, 2713.5, PCT) == "short"


def test_price_below_breakeven_with_long_position_keeps_long():
    b = PositionBook()
    b.apply_fill("buy", 0.02, 2700.0, 0.03)
    assert reversal_signal("long", b, 2600.0, PCT) is None       # keeps buying below BE
    assert reversal_signal("short", b, 2600.0, PCT) == "long"


def test_between_thresholds_keeps_direction():
    b = short_book(2700.0)
    for p in (2690.0, 2700.0, 2710.0):
        assert reversal_signal("short", b, p, PCT) is None
        assert reversal_signal("long", b, p, PCT) is None
    assert reversal_signal("short", PositionBook(), 2000.0, PCT) is None   # flat


def test_qty_eth_minimum():
    # ETHUSDT: step 0.01, min qty 0.01 (~27 USDT) -> the minimum order is 0.01 ETH
    assert qty_for_notional(27.4, 2737.0, 0.01, 0.01, min_notional=5.05) == pytest.approx(0.01)
    assert qty_for_notional(5.0, 2737.0, 0.01, 0.01, min_notional=5.05) == pytest.approx(0.01)


def test_qty_never_below_min_order_value():
    # 5 at 0.3343 with step 1: nearest step 15 = 5.01 < 5.05 -> round UP to 16
    assert qty_for_notional(5.0, 0.3343, 1, 1, min_notional=5.05) == 16
    assert qty_for_notional(5.0, 0.30, 1, 1, min_notional=5.05) == 17
    assert qty_for_notional(5.0, 0.3472, 1, 1, min_notional=5.0) * 0.3472 >= 5.0
