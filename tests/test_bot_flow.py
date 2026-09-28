"""Flusso completo dell'orchestratore con un exchange finto (nessuna rete):
apertura due gambe -> TP -> CLOSE ALL -> moltiplicatori -> TP -> nuova
sequenza, piu' il regolamento spot (repay via conversione + acquisto BTC)."""

import asyncio
import dataclasses
import json
import time

import pytest

import main as bot_main
from exchange import CoinBalance, FilledOrder, OpenPosition
from strategy import LONG_LEG, MULTIPLY, NEW_SEQUENCE, SHORT_LEG, Decision, StrategyConfig

SHORT_SYM, LONG_SYM = "BTC/USDT:USDT", "BTC/USDC:USDC"


class FakeExchange:
    def __init__(self, cfg):
        self.prices = {SHORT_SYM: 100_000.0, LONG_SYM: 100_000.0}
        self.positions = {}
        self.spot_calls = []
        self.orders = []

    async def setup(self):
        pass

    async def close(self):
        pass

    async def fetch_last_prices(self, symbols):
        return {s: self.prices[s] for s in symbols}

    def min_order_qty(self, symbol):
        return 0.001

    def qty_step(self, symbol):
        return 0.001

    async def fetch_total_equity(self):
        return 3330.0

    async def fetch_realized_funding(self, symbol, since_ms=None, limit=50):
        return []

    async def fetch_coin_balances(self):
        return {"USDC": CoinBalance("USDC", 0.0, 0.0, 0.0)}

    async def fetch_open_positions(self):
        return {s: OpenPosition(s, p[0], p[1], p[2], self.prices[s], 0.0) for s, p in self.positions.items()}

    def _fill(self, symbol, side, qty, price):
        self.orders.append((symbol, side, qty, price))
        return FilledOrder(str(len(self.orders)), symbol, side, price, qty, price * qty, None,
                           int(time.time() * 1000))

    async def open_position_market(self, symbol, side, qty):
        price = self.prices[symbol]
        self.positions[symbol] = (side, qty, price)
        return self._fill(symbol, "buy" if side == "long" else "sell", qty, price)

    async def close_position_market(self, symbol):
        side, qty, _ = self.positions.pop(symbol)
        return self._fill(symbol, "buy" if side == "short" else "sell", qty, self.prices[symbol])

    async def spot_market_buy_with_cost(self, symbol, cost):
        self.spot_calls.append(("buy", symbol, cost))
        return FilledOrder("s", symbol, "buy", 1.0, cost, cost, None, 0)

    async def spot_market_sell(self, symbol, qty):
        self.spot_calls.append(("sell", symbol, qty))
        return FilledOrder("s", symbol, "sell", 1.0, qty, qty, None, 0)

    async def repay_via_endpoint(self, coin, amount):
        return False


@pytest.fixture
def bot(tmp_path, monkeypatch):
    monkeypatch.setattr(bot_main, "ExchangeClient", FakeExchange)
    cfg = dataclasses.replace(
        StrategyConfig.load("config.json"),
        notifier_enabled=False, use_testnet=True,
        trade_history_path=str(tmp_path / "hist.json"),
        state_export_path=str(tmp_path / "live.json"),
        runtime_state_path=str(tmp_path / "state.json"),
    )
    return bot_main.BtcBot(cfg)


def set_price(bot, p):
    bot.exchange.prices = {SHORT_SYM: p, LONG_SYM: p}


def test_full_flow(bot, tmp_path):
    async def run():
        await bot._bootstrap()
        # 1% of 3330 = 33.3 < exchange minimum 0.001 BTC x 100000 = 100 -> base 100
        assert bot.seq.notionals == {SHORT_LEG: 100.0, LONG_LEG: 100.0}

        await bot._tick()
        assert set(bot.exchange.positions) == {SHORT_SYM, LONG_SYM}
        assert bot.exchange.positions[SHORT_SYM][0] == "short"
        assert bot.exchange.positions[LONG_SYM][0] == "long"

        set_price(bot, 100_200.0)  # long net two-leg = 0.2 - ~0.22 < 0.2 -> no TP
        await bot._tick()
        assert bot.seq.cycle_id == 1 and len(bot.legs) == 2

        # Cycle 1: TP long -> sequence in loss -> multipliers 150S / 200L
        set_price(bot, 100_500.0)
        await bot._tick()
        c1 = bot.analytics.cycles[-1]
        assert c1.tp_leg == LONG_LEG and c1.decision == MULTIPLY
        assert c1.net_by_coin["USDC"] > 0 > c1.net_by_coin["USDT"]
        assert bot.seq.step == 1 and bot.seq.cycle_id == 2
        assert bot.seq.notionals == pytest.approx({SHORT_LEG: 150.0, LONG_LEG: 200.0})
        # reopened immediately: 150/100500 -> 0.001, 200/100500 -> 0.002
        assert bot.exchange.positions[SHORT_SYM][1] == pytest.approx(0.001)
        assert bot.exchange.positions[LONG_SYM][1] == pytest.approx(0.002)
        saved = json.loads((tmp_path / "state.json").read_text())
        assert saved["step"] == 1 and saved["notionals"]["long"] == pytest.approx(200.0)

        # Cycle 2: TP long again -> sequence USDT+USDC > 0 -> new sequence at base
        set_price(bot, 101_100.0)
        await bot._tick()
        c2 = bot.analytics.cycles[-1]
        assert c2.decision == NEW_SEQUENCE
        assert sum(c2.sequence_net_by_coin.values()) > 0
        # winner profit (~1.4 USDC) is below the 5 USDC spot minimum -> no spot orders
        assert bot.exchange.spot_calls == []
        assert any("saltat" in a for a in c2.spot_actions)
        assert bot.seq.sequence_id == 2 and bot.seq.step == 0
        assert bot.seq.notionals[SHORT_LEG] == bot.seq.notionals[LONG_LEG]
        assert set(bot.exchange.positions) == {SHORT_SYM, LONG_SYM}

    asyncio.run(run())


def test_spot_settlement_usdt_winner(bot):
    d = Decision(action=NEW_SEQUENCE, next_notionals={}, winner_coin="USDT", loser_coin="USDC",
                 repay_amount=12.0, btc_buy_amount=18.0)
    actions = asyncio.run(bot._spot_settlement(d))
    assert bot.exchange.spot_calls == [("buy", "USDC/USDT", 12.0), ("buy", "BTC/USDT", 18.0)]
    assert len(actions) == 2


def test_spot_settlement_usdc_winner(bot):
    d = Decision(action=NEW_SEQUENCE, next_notionals={}, winner_coin="USDC", loser_coin="USDT",
                 repay_amount=7.0, btc_buy_amount=20.0)
    asyncio.run(bot._spot_settlement(d))
    assert bot.exchange.spot_calls == [("sell", "USDC/USDT", 7.0), ("buy", "BTC/USDC", 20.0)]


def test_restart_resumes_sequence(bot, tmp_path):
    async def run():
        await bot._bootstrap()
        bot.seq.step, bot.seq.notionals = 3, {SHORT_LEG: 450.0, LONG_LEG: 600.0}
        bot._save_runtime_state()
        bot2 = bot_main.BtcBot(bot.cfg)
        bot2.exchange.positions = {SHORT_SYM: ("short", 0.005, 100_000.0), LONG_SYM: ("long", 0.006, 100_000.0)}
        await bot2._bootstrap()
        assert bot2.seq.step == 3 and bot2.seq.notionals[LONG_LEG] == 600.0
        assert set(bot2.legs) == {SHORT_LEG, LONG_LEG}
        assert bot2.legs[LONG_LEG].qty == pytest.approx(0.006)

    asyncio.run(run())


def test_wrong_side_position_refuses_to_start(bot):
    bot.exchange.positions = {SHORT_SYM: ("long", 0.001, 100_000.0)}
    with pytest.raises(RuntimeError):
        asyncio.run(bot._bootstrap())
