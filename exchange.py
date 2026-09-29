"""
exchange.py

CCXT gateway to Bybit V5 for BTC bot: two linear perpetuals (BTC/USDT SHORT,
BTC/USDC LONG) plus the spot pairs used after a winning sequence (USDC/USDT
conversion, BTC/USDT or BTC/USDC purchase).

IMPORTANT (discovered empirically): Bybit's Demo Trading host
(`https://api-demo.bybit.com`) only supports a narrow set of AUTHENTICATED
endpoints (order/position/wallet management, see
https://bybit-exchange.github.io/docs/v5/demo). Anything else -- including the
currency-metadata lookup CCXT's `load_markets()` triggers once an API key is
present -- is rejected with `retCode 10032 "Demo trading are not supported"`.
Also confirmed on Demo: /v5/account/repay and /v5/account/no-convert-repay
return 10032, which is why BTC bot repays a coin's borrow by crediting it with
a spot order instead (see `strategy.plan_spot_settlement`).

Hence two CCXT instances:
  - `self._public`  -- unauthenticated, production public host: market data
    (tickers, market metadata). Identical to demo's order book.
  - `self._private` -- authenticated (BYBIT_API_KEY / BYBIT_API_SECRET from
    .env), private URL pinned to the DEMO host when USE_TESTNET=true (default).
    USE_TESTNET=false -> production host, REAL FUNDS. Its market catalogue is
    seeded from `self._public` so it never calls `load_markets()` itself.

Every order-placing call goes through `_create_order_with_dedup`: one
clientOrderId per logical order, and on a network error it CHECKS Bybit for
that id before ever resubmitting (a blind resubmit was observed live to open
duplicate positions).
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

import ccxt.async_support as ccxt_async  # type: ignore[import-untyped]
from ccxt.base.errors import ExchangeError, NetworkError  # type: ignore[import-untyped]

from strategy import BOT_NAME, StrategyConfig

logger = logging.getLogger("btc_bot.exchange")

BYBIT_DEMO_BASE_URL = "https://api-demo.bybit.com"


@dataclass(frozen=True)
class FilledOrder:
    order_id: str
    symbol: str
    side: str  # "buy" | "sell"
    price: float
    qty: float
    notional: float
    fee: Optional[float]  # real fee reported by the exchange, None if not reported
    timestamp_ms: int


@dataclass(frozen=True)
class OpenPosition:
    symbol: str
    side: str  # "long" | "short"
    qty: float
    entry_price: float
    mark_price: float
    unrealized_pnl: float


@dataclass(frozen=True)
class CoinBalance:
    coin: str
    wallet_balance: float
    borrow_amount: float
    usd_value: float


class ExchangeClient:
    """Async CCXT wrapper around Bybit V5 (demo trading by default)."""

    def __init__(self, cfg: StrategyConfig, max_retries: int = 5, retry_base_delay_sec: float = 1.0):
        self.cfg = cfg
        self.max_retries = max_retries
        self.retry_base_delay_sec = retry_base_delay_sec
        self.perp_symbols = [leg.symbol for leg in cfg.legs.values()]

        api_key, api_secret = self._resolve_credentials(cfg)

        options = dict(cfg.exchange_options)
        options.setdefault("defaultType", "swap")
        options.setdefault("adjustForTimeDifference", True)

        # ccxt's default 10s is too tight on a mobile/Termux connection: a merely
        # SLOW request would turn into a NetworkError and enter the retry path.
        request_timeout_ms = 25000

        self._public = ccxt_async.bybit({
            "enableRateLimit": True,
            "timeout": request_timeout_ms,
            "options": options,
        })
        private_urls = {"api": {"private": BYBIT_DEMO_BASE_URL}} if cfg.use_testnet else {}
        self._private = ccxt_async.bybit({
            "apiKey": api_key,
            "secret": api_secret,
            "enableRateLimit": True,
            "timeout": request_timeout_ms,
            "options": options,
            "urls": private_urls,
        })

        if cfg.use_testnet:
            logger.info("%s exchange: mode=DEMO (%s) apiKey=%s...", BOT_NAME, BYBIT_DEMO_BASE_URL,
                        api_key[:4])
        else:
            logger.warning("%s exchange: mode=PRODUCTION (LIVE FUNDS) apiKey=%s...", BOT_NAME, api_key[:4])

    @staticmethod
    def _resolve_credentials(cfg: StrategyConfig) -> Tuple[str, str]:
        """Credentials come ONLY from the environment (.env): BYBIT_API_KEY /
        BYBIT_API_SECRET. Fails fast at startup instead of handing CCXT an empty
        string (which surfaced as a confusing AuthenticationError much later)."""
        api_key = (os.environ.get("BYBIT_API_KEY") or cfg.api_key or "").strip()
        api_secret = (os.environ.get("BYBIT_API_SECRET") or cfg.api_secret or "").strip()
        if not api_key or not api_secret:
            raise ValueError("Missing Bybit credentials: set BYBIT_API_KEY / BYBIT_API_SECRET in .env.")
        return api_key, api_secret

    # -- lifecycle -----------------------------------------------------------

    async def setup(self) -> None:
        markets = await self._retry(self._public.load_markets)
        self._private.set_markets(markets)
        await self._retry(self._private.load_time_difference)

        for symbol in self.perp_symbols:
            try:
                await self._retry(self._private.set_margin_mode, self.cfg.margin_mode, symbol,
                                  quiet_exchange_errors=True)
            except ExchangeError as exc:
                logger.debug("set_margin_mode(%s, %s): %s (likely already set)", self.cfg.margin_mode, symbol, exc)
            try:
                await self._retry(self._private.set_leverage, self.cfg.leverage, symbol,
                                  quiet_exchange_errors=True)
            except ExchangeError as exc:
                logger.debug("set_leverage(%dx, %s): %s (likely already set)", self.cfg.leverage, symbol, exc)

        logger.info("Exchange pronto: %s leva=%dx margine=%s", ", ".join(self.perp_symbols),
                    self.cfg.leverage, self.cfg.margin_mode)

    async def close(self) -> None:
        await self._public.close()
        await self._private.close()

    # -- market data / metadata ---------------------------------------------

    async def fetch_last_price(self, symbol: str) -> float:
        ticker = await self._retry(self._public.fetch_ticker, symbol)
        return float(ticker.get("last") or ticker.get("close"))

    async def fetch_last_prices(self, symbols: Iterable[str]) -> Dict[str, float]:
        symbols = list(symbols)
        prices = await asyncio.gather(*(self.fetch_last_price(s) for s in symbols))
        return dict(zip(symbols, prices))

    def min_order_qty(self, symbol: str) -> float:
        """Exchange minimum qty (e.g. 0.001 BTC). Never raises: 0.0 on lookup failure."""
        try:
            market = self._public.market(symbol)
            return float((market.get("limits") or {}).get("amount", {}).get("min") or 0.0)
        except Exception:
            logger.exception("Failed to look up min order qty for %s", symbol)
            return 0.0

    def qty_step(self, symbol: str) -> float:
        """Exchange qty increment (e.g. 0.001 BTC). 0.0 on lookup failure."""
        try:
            market = self._public.market(symbol)
            return float((market.get("precision") or {}).get("amount") or 0.0)
        except Exception:
            logger.exception("Failed to look up qty step for %s", symbol)
            return 0.0

    # -- account -------------------------------------------------------------

    async def _wallet_account(self) -> dict:
        balance = await self._retry(self._private.fetch_balance)
        return balance["info"]["result"]["list"][0]

    async def fetch_total_equity(self) -> float:
        """Unified Account total equity in USD across ALL collateral coins
        (`totalEquity`) -- CCXT's `balance['USDT']` alone would massively
        understate it on an account holding BTC as collateral."""
        try:
            return float((await self._wallet_account())["totalEquity"])
        except (KeyError, IndexError, TypeError, ValueError):
            logger.exception("Unexpected balance response shape while reading totalEquity")
            return 0.0

    async def fetch_coin_balances(self) -> Dict[str, CoinBalance]:
        account = await self._wallet_account()
        result: Dict[str, CoinBalance] = {}
        for c in account.get("coin", []):
            result[c["coin"]] = CoinBalance(
                coin=c["coin"],
                wallet_balance=float(c.get("walletBalance") or 0.0),
                borrow_amount=float(c.get("borrowAmount") or 0.0),
                usd_value=float(c.get("usdValue") or 0.0),
            )
        return result

    async def fetch_realized_funding(self, symbol: str, since_ms: Optional[int] = None,
                                     limit: int = 50) -> List[Tuple[str, int, float]]:
        """REALIZED funding settlements for `symbol` from the exchange's own
        ledger -- (id, timestamp_ms, cashflow in the settle coin, positive =
        RECEIVED). Bybit settles every 8h; a short-lived position correctly
        accrues ~0.

        Sign: ccxt's `amount` here is Bybit's `execFee` for the funding
        execution, i.e. a FEE -- positive when the position PAID funding.
        Confirmed on Demo 2026-09-29 08:00 UTC (funding rate +0.0028%): the
        SHORT reported execFee -0.0118 (received), the LONG +0.0675 (paid).
        So the cashflow is `-amount`."""
        raw = await self._retry(self._private.fetch_funding_history, symbol, since_ms, limit)
        result: List[Tuple[str, int, float]] = []
        for entry in raw:
            eid, ts, amount = entry.get("id"), entry.get("timestamp"), entry.get("amount")
            if eid is None or ts is None or amount is None:
                continue
            result.append((str(eid), int(ts), -float(amount)))
        return result

    # -- positions / perpetual orders ---------------------------------------

    async def fetch_open_positions(self) -> Dict[str, OpenPosition]:
        """Open positions on the bot's perpetual symbols, keyed by symbol. One
        call per symbol: USDT and USDC perps settle in different coins and
        Bybit's position list is queried per settle coin."""
        result: Dict[str, OpenPosition] = {}
        for symbol in self.perp_symbols:
            raw = await self._retry(self._private.fetch_positions, [symbol])
            for p in raw:
                qty = float(p.get("contracts") or 0.0)
                if qty <= 0 or p.get("symbol") != symbol:
                    continue
                result[symbol] = OpenPosition(
                    symbol=symbol,
                    side=str(p.get("side") or ""),
                    qty=qty,
                    entry_price=float(p.get("entryPrice") or 0.0),
                    mark_price=float(p.get("markPrice") or 0.0),
                    unrealized_pnl=float(p.get("unrealizedPnl") or 0.0),
                )
        return result

    async def open_position_market(self, symbol: str, side: str, qty: float) -> FilledOrder:
        """side: "long" | "short"."""
        order_side = "buy" if side == "long" else "sell"
        order = await self._create_order_and_await_fill(symbol, order_side, qty)
        filled = self._to_filled_order(order, symbol, order_side)
        logger.debug("Aperta %s %s: qty=%.6f prezzo=%.2f", side.upper(), symbol, filled.qty, filled.price)
        return filled

    async def close_position_market(self, symbol: str) -> Optional[FilledOrder]:
        """Closes the whole open position on `symbol` at market (reduceOnly).
        Returns None if there is nothing open."""
        position = (await self.fetch_open_positions()).get(symbol)
        if position is None:
            logger.warning("close_position_market: nessuna posizione aperta su %s.", symbol)
            return None
        order_side = "buy" if position.side == "short" else "sell"
        order = await self._create_order_and_await_fill(symbol, order_side, position.qty,
                                                        params={"reduceOnly": True})
        filled = self._to_filled_order(order, symbol, order_side)
        logger.debug("Chiusa %s %s: qty=%.6f prezzo=%.2f", position.side.upper(), symbol, filled.qty,
                     filled.price)
        return filled

    # -- spot (post-sequence settlement) ------------------------------------

    async def spot_market_buy_with_cost(self, symbol: str, cost: float) -> FilledOrder:
        """Market BUY on a spot pair spending `cost` units of the QUOTE coin
        (e.g. BTC/USDT with cost=10 spends 10 USDT)."""
        order = await self._create_order_and_await_fill(symbol, "buy", cost, params={"cost": cost})
        return self._to_filled_order(order, symbol, "buy")

    async def spot_market_sell(self, symbol: str, qty: float) -> FilledOrder:
        """Market SELL of `qty` units of the BASE coin (e.g. USDC/USDT qty=10 sells 10 USDC)."""
        qty = float(self._public.amount_to_precision(symbol, qty))
        order = await self._create_order_and_await_fill(symbol, "sell", qty)
        return self._to_filled_order(order, symbol, "sell")

    async def repay_via_endpoint(self, coin: str, amount: float) -> bool:
        """/v5/account/repay -- PRODUCTION only (Demo answers retCode 10032).
        Returns True if Bybit accepted the request. Never raises."""
        if self.cfg.use_testnet:
            return False
        try:
            resp = await self._retry(self._private.privatePostV5AccountRepay,
                                     {"coin": coin, "amount": str(round(amount, 8))},
                                     quiet_exchange_errors=True)
            return str(resp.get("retCode", "0")) == "0"
        except Exception:
            logger.warning("Repay via /v5/account/repay fallito per %s %.6f", coin, amount, exc_info=True)
            return False

    # -- internals -----------------------------------------------------------

    async def _create_order_and_await_fill(self, symbol: str, side: str, amount: float,
                                           params: Optional[dict] = None,
                                           poll_attempts: int = 5, poll_delay_sec: float = 0.3) -> dict:
        """Bybit V5's create-order response is a bare ack (id only), so the real
        fill (avg price, qty, fee) is read back with `fetch_order`. One
        clientOrderId is generated per LOGICAL order and reused across retries
        (see `_create_order_with_dedup`)."""
        order_params = dict(params or {})
        client_order_id = uuid.uuid4().hex[:24]
        order_params["clientOrderId"] = client_order_id

        order = await self._create_order_with_dedup(symbol, side, amount, order_params, client_order_id)
        order_id = order.get("id")
        if not order_id:
            logger.error("create_order senza order id per %s %s amount=%.6f: %s", side, symbol, amount, order)
            return order

        detail = order
        for _ in range(poll_attempts):
            detail = await self._retry(self._private.fetch_order, order_id, symbol,
                                       params={"acknowledged": True})
            if detail.get("status") == "closed" and float(detail.get("filled") or 0.0) > 0:
                return detail
            await asyncio.sleep(poll_delay_sec)

        logger.warning("Ordine %s (%s %s amount=%.6f) senza fill confermato dopo %d controlli; "
                       "uso l'ultimo stato noto.", order_id, side, symbol, amount, poll_attempts)
        return detail

    @staticmethod
    def _is_duplicate_client_order_id_error(exc: Exception) -> bool:
        """Bybit's 'orderLinkId already used' rejections (110072, 170141, 12141)."""
        text = str(exc).lower()
        if any(code in text for code in ("110072", "170141", "12141")):
            return True
        return "duplicate" in text and ("orderlinkid" in text or "clientorderid" in text or "order-link" in text)

    async def _try_find_order_by_client_id(self, symbol: str, client_order_id: str) -> Optional[dict]:
        for fetch_fn in (self._private.fetch_closed_orders, self._private.fetch_open_orders):
            try:
                orders = await self._retry(fetch_fn, symbol, None, 10, params={"orderLinkId": client_order_id})
            except Exception:
                logger.exception("Lookup per clientOrderId=%s fallito via %s", client_order_id,
                                 getattr(fetch_fn, "__name__", fetch_fn))
                continue
            match = next((o for o in orders if o.get("clientOrderId") == client_order_id), None)
            if match is not None:
                return match
        return None

    async def _create_order_with_dedup(self, symbol: str, side: str, amount: float,
                                       order_params: dict, client_order_id: str) -> dict:
        """create_order with CHECK-BEFORE-RESUBMIT on network error: before any
        resubmission, ask Bybit whether an order with our clientOrderId already
        exists, and use it if so. Relying on Bybit's own duplicate-id rejection
        alone was observed live to be insufficient (two near-simultaneous
        submissions of the same id both filled)."""
        attempt = 0
        while True:
            try:
                return await self._private.create_order(symbol, "market", side, amount, params=order_params)
            except NetworkError as exc:
                attempt += 1
                if attempt > self.max_retries:
                    logger.error("Max retries (%d) superati su create_order: %s", self.max_retries, exc)
                    raise
                delay = self.retry_base_delay_sec * (2 ** (attempt - 1))
                logger.warning("Errore di rete su create_order (tentativo %d/%d): %s -- verifico se "
                               "l'ordine e' passato (clientOrderId=%s) prima di reinviarlo.",
                               attempt, self.max_retries, exc, client_order_id)
                await asyncio.sleep(delay)
                existing = await self._try_find_order_by_client_id(symbol, client_order_id)
                if existing is not None:
                    logger.warning("Trovato l'ordine ORIGINALE (id=%s) per clientOrderId=%s: uso quello.",
                                   existing.get("id"), client_order_id)
                    return existing
                logger.warning("Nessun ordine per clientOrderId=%s: reinvio sicuro.", client_order_id)
            except ExchangeError as exc:
                if not self._is_duplicate_client_order_id_error(exc):
                    logger.error("Exchange ha rifiutato create_order: %s", exc)
                    raise
                existing = await self._try_find_order_by_client_id(symbol, client_order_id)
                if existing is None:
                    raise RuntimeError(
                        f"Bybit segnala clientOrderId={client_order_id} duplicato ma nessun ordine "
                        f"corrispondente trovato su {symbol}."
                    )
                return existing

    @staticmethod
    def _to_filled_order(order: dict, symbol: str, side: str) -> FilledOrder:
        price = float(order.get("average") or order.get("price") or 0.0)
        qty = float(order.get("filled") or order.get("amount") or 0.0)
        ts = order.get("timestamp") or int(time.time() * 1000)
        fee_info = order.get("fee") or {}
        fee = fee_info.get("cost")
        return FilledOrder(order_id=str(order.get("id", "")), symbol=symbol, side=side, price=price, qty=qty,
                           notional=price * qty, fee=float(fee) if fee is not None else None,
                           timestamp_ms=int(ts))

    async def _retry(self, func: Callable[..., Any], *args: Any,
                     quiet_exchange_errors: bool = False, **kwargs: Any) -> Any:
        """Retries transient network errors with exponential backoff. Exchange
        rejections are raised immediately (logged at DEBUG when
        `quiet_exchange_errors`, e.g. 'leverage already set')."""
        attempt = 0
        name = getattr(func, "__name__", str(func))
        while True:
            try:
                return await func(*args, **kwargs)
            except NetworkError as exc:
                attempt += 1
                if attempt > self.max_retries:
                    logger.error("Max retries (%d) superati su %s: %s", self.max_retries, name, exc)
                    raise
                delay = self.retry_base_delay_sec * (2 ** (attempt - 1))
                logger.warning("Errore di rete su %s (tentativo %d/%d): %s -- riprovo tra %.1fs",
                               name, attempt, self.max_retries, exc, delay)
                await asyncio.sleep(delay)
            except ExchangeError as exc:
                (logger.debug if quiet_exchange_errors else logger.error)("Exchange ha rifiutato %s: %s", name, exc)
                raise
