"""Flusso completo di TrxBot con exchange finto (netting one-way) e orologio
simulato: primo ordine all'avvio, un ordine al minuto, inversione, cambio di
segno della posizione, riallineamento dopo una chiusura manuale."""

import asyncio
import dataclasses

import pytest

import main as bot_main
from exchange import FilledOrder, OpenPosition
from strategy import StrategyConfig


class FakeExchange:
    """Nets orders into ONE position like Bybit one-way mode."""

    def __init__(self, cfg):
        self.price = 0.30
        self.side, self.qty, self.entry = None, 0.0, 0.0
        self.orders = []

    async def setup(self):
        pass

    async def close(self):
        pass

    async def fetch_last_price(self):
        return self.price

    def min_order_qty(self):
        return 1.0

    def min_order_notional(self):
        return 5.0

    def qty_step(self):
        return 1.0

    async def fetch_total_equity(self):
        return 3300.0

    async def fetch_realized_funding(self, symbol=None, since_ms=None, limit=50):
        return []

    async def fetch_position(self):
        return None if self.qty <= 0 else OpenPosition(self.side, self.qty, self.entry, 0.0)

    async def place_market_order(self, side, qty):
        d = "long" if side == "buy" else "short"
        if self.qty <= 0:
            self.side, self.qty, self.entry = d, qty, self.price
        elif d == self.side:
            self.entry = (self.entry * self.qty + self.price * qty) / (self.qty + qty)
            self.qty += qty
        elif qty < self.qty:
            self.qty -= qty
        elif qty == self.qty:
            self.side, self.qty, self.entry = None, 0.0, 0.0
        else:
            self.side, self.qty, self.entry = d, qty - self.qty, self.price
        self.orders.append((side, qty, self.price))
        return FilledOrder(str(len(self.orders)), side, self.price, qty, self.price * qty, None, 0)


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def bot(tmp_path, monkeypatch):
    monkeypatch.setattr(bot_main, "ExchangeClient", FakeExchange)
    cfg = dataclasses.replace(
        StrategyConfig.load("config.json"), notifier_enabled=False, use_testnet=True,
        equity_based_sizing_enabled=True, equity_based_sizing_percentage=1.0,
        trade_history_path=str(tmp_path / "hist.json"), state_export_path=str(tmp_path / "live.json"),
        runtime_state_path=str(tmp_path / "state.json"),
    )
    return bot_main.TrxBot(cfg, clock=Clock(600.5))  # 10:00.5 into minute slot 10


def test_full_flow(bot):
    ex = bot.exchange

    async def run():
        await bot._bootstrap()
        # 1% of 3300 = 33 USDC -> 110 TRX at 0.30, first SHORT fired immediately
        assert bot.state.base_notional == pytest.approx(33.0)
        assert ex.orders == [("sell", 110.0, 0.30)]
        assert bot.state.mode == "short"

        bot.now.t = 630.0                      # 30s after the first order: no new order
        await bot._tick()
        assert len(ex.orders) == 1

        bot.now.t = 661.5                      # 61s after the first order
        await bot._tick()
        assert ex.orders[-1][0] == "sell" and bot.state.book.qty == 220

        bot.now.t = 720.5                      # only 59s after the previous order: waits
        await bot._tick()
        assert len(ex.orders) == 2

        # price drops > 0.5% below the net break-even -> reversal to LONG
        ex.price = 0.297
        bot.now.t = 721.5
        await bot._tick()
        assert bot.state.mode == "long"
        assert ex.orders[-1][0] == "buy"       # this minute's order already fires long
        assert bot.state.book.side == "short" and bot.state.book.qty == 220 - 111
        assert bot.state.book.realized_gross > 0

        # two more minutes of buys: 109 -> flat is crossed, position becomes LONG
        for t in (781.5, 841.5):
            bot.now.t = t
            await bot._tick()
        assert bot.state.book.side == "long"
        assert len(bot.analytics.h.episodes) == 1 and bot.analytics.h.episodes[0].side == "short"
        assert bot.analytics.h.episodes[0].net > 0

        # long in profit past +0.5% -> reversal back to SHORT
        ex.price = 0.30
        bot.now.t = 842.5
        await bot._tick()
        assert bot.state.mode == "short"
        assert len(bot.analytics.h.reversals) == 2

    asyncio.run(run())


def test_manual_close_is_detected_and_resynced(bot):
    ex = bot.exchange

    async def run():
        await bot._bootstrap()
        ex.side, ex.qty, ex.entry = None, 0.0, 0.0   # closed by hand on Bybit
        await bot._tick()
        assert bot.state.book.is_flat and bot.state.book.episode_id == 2
        bot.now.t = 661.5
        await bot._tick()                            # keeps firing in the active direction
        assert bot.state.book.side == "short" and bot.state.book.qty == 110

    asyncio.run(run())


def test_restart_resumes_state(bot, tmp_path):
    async def run():
        await bot._bootstrap()
        bot.state.mode = "long"
        bot._save_state()
        bot2 = bot_main.TrxBot(bot.cfg, clock=Clock(605.0))
        bot2.exchange.side, bot2.exchange.qty, bot2.exchange.entry = "short", 110.0, 0.30
        await bot2._bootstrap()
        assert bot2.state.mode == "long" and bot2.state.base_notional == pytest.approx(33.0)
        assert bot2.exchange.orders == []            # no extra opening order on restart
        assert bot2.state.book.qty == 110

    asyncio.run(run())


def test_minimum_order_sizing(tmp_path, monkeypatch):
    """Current config: equity sizing off, base_notional_usd 0 -> exchange minimum (5 USDC)."""
    monkeypatch.setattr(bot_main, "ExchangeClient", FakeExchange)
    cfg = dataclasses.replace(
        StrategyConfig.load("config.json"), notifier_enabled=False, use_testnet=True,
        trade_history_path=str(tmp_path / "h.json"), state_export_path=str(tmp_path / "l.json"),
        runtime_state_path=str(tmp_path / "s.json"),
    )
    assert cfg.equity_based_sizing_enabled is False and cfg.base_notional_usd == 0
    bot = bot_main.TrxBot(cfg, clock=Clock(600.0))

    async def run():
        await bot._bootstrap()
        assert bot.state.base_notional == pytest.approx(5.0)
        side, qty, price = bot.exchange.orders[0]
        assert side == "sell" and qty == 17 and qty * price >= 5.0   # 5 / 0.30 = 16.7 -> 17 TRX

    asyncio.run(run())
