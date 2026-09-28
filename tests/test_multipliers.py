"""Moltiplicatori cumulativi per gamba: gamba TP x2, gamba perdente x1.5."""

import pytest

from strategy import LONG_LEG, SHORT_LEG, next_round_notionals, qty_for_notional

W, L = 2.0, 1.5

# Sequenza di riferimento (base 100):
# 100S/100L -> TP long -> 150S/200L -> TP short -> 300S/300L -> TP long -> 450S/600L
# -> TP short -> 900S/900L -> TP short -> 1800S/1350L
REFERENCE = [
    (LONG_LEG, (150, 200)),
    (SHORT_LEG, (300, 300)),
    (LONG_LEG, (450, 600)),
    (SHORT_LEG, (900, 900)),
    (SHORT_LEG, (1800, 1350)),
]


def test_reference_sequence():
    notionals = {SHORT_LEG: 100.0, LONG_LEG: 100.0}
    for tp_leg, (exp_short, exp_long) in REFERENCE:
        notionals = next_round_notionals(notionals, tp_leg, W, L)
        assert notionals[SHORT_LEG] == pytest.approx(exp_short)
        assert notionals[LONG_LEG] == pytest.approx(exp_long)


def test_multipliers_are_cumulative_not_from_base():
    n = next_round_notionals({SHORT_LEG: 150.0, LONG_LEG: 200.0}, SHORT_LEG, W, L)
    assert n == {SHORT_LEG: 300.0, LONG_LEG: 300.0}  # 150*2, 200*1.5 -- not 100*2 / 100*1.5


def test_multipliers_are_parametric():
    n = next_round_notionals({SHORT_LEG: 100.0, LONG_LEG: 100.0}, LONG_LEG, 3.0, 1.25)
    assert n == {SHORT_LEG: 125.0, LONG_LEG: 300.0}


def test_unknown_leg_rejected():
    with pytest.raises(ValueError):
        next_round_notionals({SHORT_LEG: 1.0, LONG_LEG: 1.0}, "sideways", W, L)


@pytest.mark.parametrize("notional,expected", [
    (84.0, 0.001),     # base at the exchange minimum
    (126.0, 0.002),    # 1.5 x 0.001 BTC -> rounds UP to 0.002 (nearest step), not truncated
    (100.0, 0.001),
    (168.0, 0.002),
    (10.0, 0.001),     # never below min qty
])
def test_qty_rounding(notional, expected):
    assert qty_for_notional(notional, 84_000.0, qty_step=0.001, min_qty=0.001) == pytest.approx(expected)
