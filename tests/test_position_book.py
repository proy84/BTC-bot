"""Registro della posizione netta one-way: accumulo, riduzione, inversione di segno."""

import pytest

from strategy import PositionBook


def test_accumulate_short_average_entry():
    b = PositionBook()
    assert b.apply_fill("sell", 100, 0.30, 0.0165) is None
    assert b.apply_fill("sell", 100, 0.32, 0.0176) is None
    assert b.side == "short" and b.qty == 100 + 100
    assert b.avg_entry == pytest.approx(0.31)
    assert b.fees == pytest.approx(0.0165 + 0.0176)
    assert b.realized_gross == 0.0


def test_opposite_order_reduces_and_realizes_without_changing_average():
    b = PositionBook()
    b.apply_fill("sell", 200, 0.31, 0.0)
    assert b.apply_fill("buy", 50, 0.30, 0.01) is None
    assert b.side == "short" and b.qty == 150
    assert b.avg_entry == pytest.approx(0.31)                 # unchanged on reduce
    assert b.realized_gross == pytest.approx(50 * 0.01)       # (0.31 - 0.30) * 50
    assert b.fees == pytest.approx(0.01)


def test_exact_close_ends_episode():
    b = PositionBook()
    b.apply_fill("sell", 100, 0.30, 0.02)
    closed = b.apply_fill("buy", 100, 0.29, 0.02)
    assert closed is not None and closed.side == "short" and closed.episode_id == 1
    assert closed.realized_gross == pytest.approx(1.0)
    assert closed.net == pytest.approx(1.0 - 0.04)
    assert b.is_flat and b.episode_id == 2 and b.fees == 0.0


def test_order_larger_than_position_flips_side_and_splits_fee():
    b = PositionBook()
    b.apply_fill("sell", 60, 0.30, 0.0)
    closed = b.apply_fill("buy", 100, 0.29, 0.10)
    assert closed is not None and closed.side == "short"
    assert closed.realized_gross == pytest.approx(60 * 0.01)
    assert closed.fees == pytest.approx(0.10 * 60 / 100)      # pro rata on the closing part
    assert b.side == "long" and b.qty == 40 and b.avg_entry == pytest.approx(0.29)
    assert b.fees == pytest.approx(0.10 * 40 / 100)           # rest on the new episode
    assert b.episode_id == 2


def test_roundtrip_dict():
    b = PositionBook()
    b.apply_fill("sell", 100, 0.3, 0.01)
    b.funding = 0.002
    assert PositionBook.from_dict(b.to_dict()) == b
