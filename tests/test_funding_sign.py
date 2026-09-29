"""Segno del funding: Bybit/ccxt riportano il funding come FEE (positivo =
pagato); il bot lo usa come cashflow (positivo = incassato)."""

import asyncio

from exchange import ExchangeClient


class _FakePrivate:
    def __init__(self, entries):
        self.entries = entries

    async def fetch_funding_history(self, symbol, since, limit):
        return self.entries


def make_client(entries):
    client = object.__new__(ExchangeClient)  # no network, no credentials
    client.max_retries = 0
    client.retry_base_delay_sec = 0.0
    client._private = _FakePrivate(entries)
    return client


def test_funding_fee_is_converted_to_cashflow():
    # Real Demo values, 2026-09-29 08:00 UTC, funding rate positive (longs pay shorts)
    short_client = make_client([{"id": "a", "timestamp": 1, "amount": -0.01181635}])
    long_client = make_client([{"id": "b", "timestamp": 1, "amount": 0.06749251}])
    (_, _, short_cf), = asyncio.run(short_client.fetch_realized_funding("BTC/USDT:USDT"))
    (_, _, long_cf), = asyncio.run(long_client.fetch_realized_funding("BTC/USDC:USDC"))
    assert short_cf > 0 and abs(short_cf - 0.01181635) < 1e-12   # SHORT received
    assert long_cf < 0 and abs(long_cf + 0.06749251) < 1e-12     # LONG paid


def test_incomplete_entries_skipped():
    client = make_client([{"id": None, "timestamp": 1, "amount": 1.0}, {"id": "x", "timestamp": 2, "amount": None}])
    assert asyncio.run(client.fetch_realized_funding("BTC/USDT:USDT")) == []
