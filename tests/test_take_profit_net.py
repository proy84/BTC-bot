"""TP netto su due gambe: la gamba che fa TP paga fee (apertura + chiusura)
e funding di ENTRAMBE le posizioni, non solo le proprie."""

import pytest

from fees import LONG, SHORT, FeeSchedule, LegPosition, realized_leg_net, two_leg_net
from strategy import LONG_LEG, SHORT_LEG, check_take_profit

TAKER = 0.00055
SCHED = FeeSchedule(taker_rate=TAKER)
TP_PCT = 0.20


def make_legs(short_qty=0.001, long_qty=0.001, entry=100_000.0, short_funding=0.0, long_funding=0.0):
    return {
        SHORT_LEG: LegPosition(SHORT_LEG, "BTC/USDT:USDT", SHORT, "USDT", short_qty, entry,
                               open_fee=entry * short_qty * TAKER, funding=short_funding),
        LONG_LEG: LegPosition(LONG_LEG, "BTC/USDC:USDC", LONG, "USDC", long_qty, entry,
                              open_fee=entry * long_qty * TAKER, funding=long_funding),
    }


def marks(p):
    return {SHORT_LEG: p, LONG_LEG: p}


def test_breakdown_charges_both_legs_fees():
    legs = make_legs()
    bd = two_leg_net(legs, marks(99_700.0), SHORT_LEG, SCHED)
    assert bd.gross_pnl == pytest.approx(0.3)
    assert bd.open_fees == pytest.approx(2 * 0.055)                 # both legs opened at 100 notional
    assert bd.close_fees_est == pytest.approx(2 * 99.7 * TAKER)      # both legs closed at 99.7 notional
    assert bd.net_pnl == pytest.approx(0.3 - 0.11 - 2 * 99.7 * TAKER)
    assert bd.notional == pytest.approx(100.0)


def test_tp_threshold_exact_boundary_short():
    # net_short(p) = (100000 - p)*0.001 - 0.11 - 2*0.00055*p*0.001 >= 0.2  <=>  p <= 99580.46
    legs = make_legs()
    hit = check_take_profit(legs, marks(99_580.0), SCHED, TP_PCT)
    assert hit is not None and hit.leg == SHORT_LEG
    assert hit.breakdown.net_pnl >= 0.2
    assert check_take_profit(legs, marks(99_581.0), SCHED, TP_PCT) is None


def test_tp_threshold_exact_boundary_long():
    # symmetric: net_long(p) = (p - 100000)*0.001 - 0.11 - 0.0011*p/1000 >= 0.2 <=> p >= 100420.9...
    legs = make_legs()
    assert check_take_profit(legs, marks(100_420.0), SCHED, TP_PCT) is None
    hit = check_take_profit(legs, marks(100_422.0), SCHED, TP_PCT)
    assert hit is not None and hit.leg == LONG_LEG


def test_own_fees_only_would_trigger_but_two_leg_net_does_not():
    """At 99650 the short leg would be at +0.24% counting ONLY its own fees,
    but the TP must NOT fire because the long leg's fees are charged too."""
    legs = make_legs()
    p = 99_650.0
    short = legs[SHORT_LEG]
    own_only = realized_leg_net(short, p, close_fee=p * short.qty * TAKER)
    assert own_only >= 0.2
    assert check_take_profit(legs, marks(p), SCHED, TP_PCT) is None


def test_funding_of_both_legs_is_included():
    base = two_leg_net(make_legs(), marks(99_600.0), SHORT_LEG, SCHED).net_pnl
    with_funding = two_leg_net(make_legs(short_funding=0.05, long_funding=-0.10),
                               marks(99_600.0), SHORT_LEG, SCHED).net_pnl
    assert with_funding == pytest.approx(base + 0.05 - 0.10)


def test_paid_funding_on_the_other_leg_can_block_tp():
    p = 99_580.0  # TP hit with no funding (see boundary test)
    assert check_take_profit(make_legs(), marks(p), SCHED, TP_PCT) is not None
    assert check_take_profit(make_legs(long_funding=-0.01), marks(p), SCHED, TP_PCT) is None


def test_tp_percentage_is_on_the_tp_legs_own_notional():
    """Different sizes (e.g. 150S/200L): the target is 0.20% of the TP leg's
    notional, while fees are charged on both legs' (different) notionals."""
    legs = make_legs(short_qty=0.0015, long_qty=0.002)
    p = 100_500.0
    bd = two_leg_net(legs, marks(p), LONG_LEG, SCHED)
    expected_fees = (150 + 200) * TAKER + (0.0015 + 0.002) * p * TAKER
    assert bd.net_pnl == pytest.approx(500 * 0.002 - expected_fees)
    assert bd.notional == pytest.approx(200.0)
    hit = check_take_profit(legs, marks(p), SCHED, TP_PCT)
    assert hit is not None and hit.leg == LONG_LEG
    assert hit.breakdown.net_pnl >= 0.002 * 200.0


def test_no_tp_with_a_single_leg_open():
    legs = make_legs()
    del legs[LONG_LEG]
    assert check_take_profit(legs, {SHORT_LEG: 90_000.0}, SCHED, TP_PCT) is None


def test_realized_leg_net_long_and_short():
    legs = make_legs(short_funding=0.01, long_funding=-0.02)
    assert realized_leg_net(legs[SHORT_LEG], 99_000.0, 0.05) == pytest.approx(1.0 - 0.055 - 0.05 + 0.01)
    assert realized_leg_net(legs[LONG_LEG], 99_000.0, 0.05) == pytest.approx(-1.0 - 0.055 - 0.05 - 0.02)
