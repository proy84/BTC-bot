"""Decisione dopo il CLOSE ALL: nuovo ciclo (repay + BTC spot) vs moltiplicatori."""

import pytest

from strategy import (
    LONG_LEG, MULTIPLY, NEW_SEQUENCE, SHORT_LEG, STOP, SequenceState, decide_after_close,
    effective_base_notional, next_round_notionals, plan_spot_settlement,
)

W, L = 2.0, 1.5
MIN = 5.0  # minimo ordine spot Bybit


def decide(seq, tp_leg, max_steps=None):
    return decide_after_close(seq, tp_leg, W, L, max_steps, MIN)


def test_net_loss_on_sequence_means_multiply():
    seq = SequenceState.new(100.0)
    seq.add_cycle_result({"USDC": 0.20, "USDT": -0.55})
    d = decide(seq, LONG_LEG)
    assert d.action == MULTIPLY
    assert d.next_notionals == {SHORT_LEG: 150.0, LONG_LEG: 200.0}


def test_exactly_zero_is_not_a_gain():
    seq = SequenceState.new(100.0)
    seq.add_cycle_result({"USDC": 0.5, "USDT": -0.5})
    assert decide(seq, LONG_LEG).action == MULTIPLY


def test_repay_and_btc_margin_means_new_sequence():
    seq = SequenceState.new(100.0)
    seq.add_cycle_result({"USDT": 20.0, "USDC": -12.0})
    d = decide(seq, SHORT_LEG)
    assert d.action == NEW_SEQUENCE
    assert d.winner_coin == "USDT" and d.loser_coin == "USDC"
    assert d.repay_amount == pytest.approx(12.0)
    assert d.plan.convert_amount == pytest.approx(12.0)
    assert d.btc_buy_amount == pytest.approx(8.0)   # 20 - 12 = sequence net profit, >= 5
    assert d.next_notionals == {}                   # base is re-read from equity


def test_sequence_positive_but_btc_rest_below_minimum_means_multiply():
    """Net positive (+4.5) but after repaying 2.5 (rounded up to the 5.1 spot
    minimum) only 1.9 would be left for BTC: below 5 -> keep multiplying."""
    seq = SequenceState.new(100.0)
    seq.add_cycle_result({"USDT": 7.0, "USDC": -2.5})
    d = decide(seq, SHORT_LEG)
    assert d.action == MULTIPLY
    assert d.next_notionals == {SHORT_LEG: 200.0, LONG_LEG: 150.0}


def test_small_repay_rounded_up_still_leaves_btc_margin():
    seq = SequenceState.new(100.0)
    seq.add_cycle_result({"USDT": 11.0, "USDC": -2.0})
    d = decide(seq, SHORT_LEG)
    assert d.action == NEW_SEQUENCE
    assert d.plan.convert_amount == pytest.approx(5.1)
    assert d.btc_buy_amount == pytest.approx(5.9)


def test_winner_profit_below_minimum_means_multiply():
    seq = SequenceState.new(100.0)
    seq.add_cycle_result({"USDT": 3.0, "USDC": 1.0})
    assert decide(seq, SHORT_LEG).action == MULTIPLY


def test_both_coins_positive_no_repay():
    seq = SequenceState.new(100.0)
    seq.add_cycle_result({"USDT": 6.0, "USDC": 1.0})
    d = decide(seq, SHORT_LEG)
    assert d.action == NEW_SEQUENCE and d.winner_coin == "USDT"
    assert d.repay_amount == 0.0 and d.plan.convert_amount == 0.0
    assert d.btc_buy_amount == pytest.approx(6.0)


def test_no_step_limit_by_default():
    seq = SequenceState.new(100.0)
    seq.step = 50
    seq.add_cycle_result({"USDT": -1.0, "USDC": 0.5})
    assert decide(seq, LONG_LEG, max_steps=None).action == MULTIPLY


def test_step_limit_stops_when_set():
    seq = SequenceState.new(100.0)
    seq.step = 3
    seq.add_cycle_result({"USDT": -1.0, "USDC": 0.5})
    assert decide(seq, LONG_LEG, max_steps=3).action == STOP
    seq.step = 2
    assert decide(seq, LONG_LEG, max_steps=3).action == MULTIPLY


def test_full_reference_sequence_with_decisions():
    """Drives the reference sequence end-to-end through the decision function.
    Per-cycle nets: the TP leg earns +0.20% of its notional, the other leg
    loses 0.45% of its own (price move + all fees) -- so the sequence stays
    in net loss until the 1800S/1350L cycle, whose TP short turns it positive."""
    seq = SequenceState.new(100.0)
    coin = {SHORT_LEG: "USDT", LONG_LEG: "USDC"}
    tps = [LONG_LEG, SHORT_LEG, LONG_LEG, SHORT_LEG, SHORT_LEG]
    expected = [(150, 200), (300, 300), (450, 600), (900, 900), (1800, 1350)]

    def cycle_nets(tp_leg, notionals, winner_pct, loser_pct):
        other = LONG_LEG if tp_leg == SHORT_LEG else SHORT_LEG
        return {coin[tp_leg]: notionals[tp_leg] * winner_pct / 100.0,
                coin[other]: -notionals[other] * loser_pct / 100.0}

    for tp_leg, (exp_s, exp_l) in zip(tps, expected):
        seq.add_cycle_result(cycle_nets(tp_leg, seq.notionals, 0.20, 0.45))
        d = decide(seq, tp_leg)
        assert d.action == MULTIPLY, (tp_leg, seq.net_by_coin)
        seq.notionals, seq.step = d.next_notionals, seq.step + 1
        assert (seq.notionals[SHORT_LEG], seq.notionals[LONG_LEG]) == pytest.approx((exp_s, exp_l))

    # 1800S/1350L -> TP short with a big move: short +2.0%, long -1.0%
    seq.add_cycle_result(cycle_nets(SHORT_LEG, seq.notionals, 2.0, 1.0))
    assert seq.total_net > 0
    d = decide(seq, SHORT_LEG)
    assert d.action == NEW_SEQUENCE
    assert d.winner_coin == "USDT" and d.loser_coin == "USDC"
    assert d.repay_amount == pytest.approx(-seq.net_by_coin["USDC"])
    assert d.btc_buy_amount == pytest.approx(seq.total_net)

    # ... and the new sequence restarts at base on both legs, no multipliers
    fresh = SequenceState.new(100.0, sequence_id=seq.sequence_id + 1)
    assert fresh.notionals == {SHORT_LEG: 100.0, LONG_LEG: 100.0} and fresh.step == 0


def test_sequence_state_roundtrip():
    seq = SequenceState.new(84.0, sequence_id=3, cycle_id=17)
    seq.step = 2
    seq.notionals = next_round_notionals(seq.notionals, SHORT_LEG, W, L)
    seq.add_cycle_result({"USDT": 1.25, "USDC": -3.5})
    assert SequenceState.from_dict(seq.to_dict()) == seq


def test_effective_base_notional_uses_exchange_minimum():
    assert effective_base_notional(3330.0, 1.0, 1.0, {"short": 84.0, "long": 84.2}) == pytest.approx(84.2)
    assert effective_base_notional(20_000.0, 1.0, 1.0, {"short": 84.0, "long": 84.2}) == pytest.approx(200.0)
    assert effective_base_notional(0.0, 1.0, 50.0, {"short": 10.0}) == pytest.approx(50.0)


def test_spot_plan_normal():
    p = plan_spot_settlement(repay_amount=12.0, winner_profit=30.0, min_order_value=5.0)
    assert p.convert_amount == pytest.approx(12.0) and p.btc_buy_amount == pytest.approx(18.0)


def test_spot_plan_small_repay_rounded_up_to_minimum():
    p = plan_spot_settlement(repay_amount=1.0, winner_profit=30.0, min_order_value=5.0)
    assert p.convert_amount == pytest.approx(5.1)
    assert p.btc_buy_amount == pytest.approx(24.9)


def test_spot_plan_small_rest_skips_btc_buy():
    p = plan_spot_settlement(repay_amount=10.0, winner_profit=12.0, min_order_value=5.0)
    assert p.convert_amount == pytest.approx(10.0) and p.btc_buy_amount == 0.0


def test_spot_plan_profit_below_minimum_skips_everything():
    p = plan_spot_settlement(repay_amount=1.0, winner_profit=3.0, min_order_value=5.0)
    assert p.convert_amount == 0.0 and p.btc_buy_amount == 0.0
