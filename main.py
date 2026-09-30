"""
main.py

Accumulation bot (ETH bot) -- one-way, one linear perpetual (currently
ETH/USDT 150x; Bybit V5, Demo Trading by default). Strategy rules: see `strategy.py`.

Loop (every `polling.tick_poll_interval_sec`), always under `_position_lock`:
  1. Read the last price and reconcile the local position book with Bybit's
     real net position (a manual close/change on Bybit is detected here and
     the book is resynced instead of trading on a stale picture).
  2. Poll realized funding (every `funding_poll_interval_sec`).
  3. Reversal check: price `reversal_pct`% below the break-even (average
     entry, no fees) -> fire LONG; `reversal_pct`% above -> fire SHORT
     (`strategy.reversal_signal`).
  4. When `timeframe_sec` (60s) have passed since the previous order, fire
     one market order of `base_notional` in the active direction (never below
     the exchange minimum order value). At first start the first order fires
     immediately.

State that must survive a restart (active direction, base notional,
position book, time of the last order) lives in `paths.runtime_state_path`.

Clean remote shutdown: create a file named STOP next to main.py (the open
position is left as is).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Callable, Optional

from analytics import AnalyticsEngine, EpisodeRecord, OrderRecord, ReversalRecord
from data_exporter import DataExporter
from exchange import ExchangeClient
from strategy import (
    BOT_NAME, BotState, ClosedEpisode, PositionBook, StrategyConfig, base_notional_from_equity,
    breakeven, order_side_for, qty_for_notional, reversal_signal, reversal_thresholds,
)

logger = logging.getLogger("bot.main")

BOT_VERSION = "3.0"
CONFIG_PATH = "config.json"
STOP_SIGNAL_PATH = Path("STOP")
MIN_ORDER_MARGIN = 1.01         # keep each order >= 1% above the exchange minimum value (price drift)
HEARTBEAT_INTERVAL_SEC = 300.0
FALLBACK_MIN_ORDER_VALUE = 5.0  # Bybit linear minimum order value, used if metadata lookup fails


class TrxBot:
    def __init__(self, cfg: StrategyConfig, clock: Callable[[], float] = time.time):
        self.cfg = cfg
        self.now = clock
        self.exchange = ExchangeClient(cfg)
        self.analytics = AnalyticsEngine(cfg.trade_history_path)
        self.exporter = DataExporter(cfg.state_export_path, self.analytics)
        self.runtime_state_path = Path(cfg.runtime_state_path)

        self.state: Optional[BotState] = None
        self.last_price: Optional[float] = None
        self._min_order_value = FALLBACK_MIN_ORDER_VALUE
        self._funding_ids: set = set()
        self._funding_since_ms: Optional[int] = None
        self._last_funding_poll = 0.0
        self._last_heartbeat = 0.0
        self._stop_event = asyncio.Event()
        self._position_lock = asyncio.Lock()
        self._background_tasks: set = set()

    # -- lifecycle -----------------------------------------------------------

    async def start(self) -> None:
        logger.info("[INFO] Avvio %s v%s -- %s leva %dx one-way, timeframe %s, inversione a %.2f%% oltre il "
                    "breakeven netto, %s", BOT_NAME, BOT_VERSION, self.cfg.symbol, self.cfg.leverage,
                    self.cfg.timeframe, self.cfg.reversal_pct, "DEMO" if self.cfg.use_testnet else "PRODUZIONE")
        await self.exchange.setup()
        await self._bootstrap()
        self._notify("Avvio", [f"Direzione attiva: <b>{self.state.mode.upper()}</b>",
                               f"Importo per ordine: {self.state.base_notional:.2f} {self.cfg.settle_coin}"])
        await self._tick_loop()

    async def stop(self) -> None:
        self._stop_event.set()
        if self._background_tasks:
            await asyncio.gather(*self._background_tasks, return_exceptions=True)
        await self.exchange.close()

    # -- bootstrap / persistence --------------------------------------------

    async def _bootstrap(self) -> None:
        self.state = self._load_state()
        if self.state is None:
            price = await self.exchange.fetch_last_price()
            base = await self._compute_base_notional(price)
            book = PositionBook()
            pos = await self.exchange.fetch_position()
            if pos is not None:
                book = self._book_from_exchange(pos, episode_id=1)
                logger.warning("Posizione esistente su %s adottata: %s qty=%g entry=%.5f.",
                               self.cfg.symbol, pos.side.upper(), pos.qty, pos.entry_price)
            self.state = BotState(mode=self.cfg.initial_direction, base_notional=base, book=book)
            logger.info("Primo avvio: direzione %s, importo per ordine %.2f %s.", self.state.mode.upper(), base,
                        self.cfg.settle_coin)
            await self._fire_order(price)  # "all'avvio apre la posizione"
        else:
            price = await self.exchange.fetch_last_price()
            base = await self._compute_base_notional(price, last_base=self.state.base_notional)
            if abs(base - self.state.base_notional) > 1e-9:
                logger.info("Importo per ordine aggiornato: %.2f -> %.2f %s.",
                            self.state.base_notional, base, self.cfg.settle_coin)
                self.state.base_notional = base
        self._save_state()

    def _load_state(self) -> Optional[BotState]:
        if not self.runtime_state_path.exists():
            return None
        try:
            state = BotState.from_dict(json.loads(self.runtime_state_path.read_text(encoding="utf-8")))
            logger.info("Stato ripreso: direzione %s, importo %.2f, posizione %s qty=%g, ordini %d.",
                        state.mode.upper(), state.base_notional, (state.book.side or "flat").upper(),
                        state.book.qty, state.orders_count)
            return state
        except Exception:
            logger.exception("Stato %s illeggibile: riparto da zero.", self.runtime_state_path)
            return None

    def _save_state(self) -> None:
        tmp = self.runtime_state_path.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(self.state.to_dict(), indent=2), encoding="utf-8")
            tmp.replace(self.runtime_state_path)
        except OSError:
            logger.exception("Salvataggio stato %s fallito", self.runtime_state_path)

    async def _compute_base_notional(self, price: float, last_base: Optional[float] = None,
                                     verbose: bool = True) -> float:
        """Order size. Equity sizing ON: `percentage`% of the CURRENT total
        equity (called before every order, so it follows the account up and
        down); if the equity read fails, `last_base` is kept. Equity sizing
        OFF: fixed `base_notional_usd` (0 = exchange minimum). Never below the
        exchange minimum order value."""
        min_value = max(self.exchange.min_order_notional() or FALLBACK_MIN_ORDER_VALUE,
                        self.exchange.min_order_qty() * price)
        self._min_order_value = min_value
        equity = 0.0
        if self.cfg.equity_based_sizing_enabled:
            try:
                equity = await self.exchange.fetch_total_equity()
            except Exception:
                logger.warning("Lettura equity fallita.", exc_info=True)
            if equity <= 0 and last_base is not None:
                logger.warning("Equity non disponibile: tengo l'importo precedente %.2f %s.", last_base,
                               self.cfg.settle_coin)
                return last_base
        base = base_notional_from_equity(equity, self.cfg.equity_based_sizing_percentage,
                                         self.cfg.base_notional_usd, min_value)
        (logger.info if verbose else logger.debug)(
            "Importo per ordine: %.2f %s (equity %.2f x %.2f%%, minimo exchange %.2f).",
            base, self.cfg.settle_coin, equity, self.cfg.equity_based_sizing_percentage, min_value)
        return base

    def _book_from_exchange(self, pos, episode_id: int) -> PositionBook:
        fee = self.cfg.taker_rate * pos.entry_price * pos.qty  # real opening fees unknown: estimate
        return PositionBook(side=pos.side, qty=pos.qty, avg_entry=pos.entry_price, fees=fee, episode_id=episode_id)

    # -- main loop -----------------------------------------------------------

    async def _tick_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                await self._tick()
            except Exception:
                logger.exception("Errore durante il tick")
            if await self._wait_or_stop(self.cfg.tick_poll_interval_sec):
                break

    async def _tick(self) -> None:
        price = await self.exchange.fetch_last_price()
        self.last_price = price
        async with self._position_lock:
            await self._reconcile_with_exchange()
            await self._maybe_poll_funding()
            self._maybe_reverse(price)

            if self.now() - self.state.last_order_ts >= self.cfg.timeframe_sec:
                await self._fire_order(price)
                self._maybe_reverse(self.last_price)  # the fill itself can move the break-even

            self._save_state()
            self._export(price)
            self._maybe_heartbeat(price)

    async def _fire_order(self, price: float) -> None:
        st = self.state
        st.last_order_ts = self.now()  # one attempt per timeframe, even if it fails
        if self.cfg.equity_based_sizing_enabled:
            # re-read the equity before EVERY order: the size follows the account
            st.base_notional = await self._compute_base_notional(price, last_base=st.base_notional, verbose=False)
        side = order_side_for(st.mode)
        qty = qty_for_notional(st.base_notional, price, self.exchange.qty_step(), self.exchange.min_order_qty(),
                               min_notional=self._min_order_value * MIN_ORDER_MARGIN)
        try:
            filled = await self.exchange.place_market_order(side, qty)
        except Exception:
            logger.exception("Ordine %s %s qty=%g fallito: riprovo al prossimo minuto.",
                             side.upper(), self.cfg.symbol, qty)
            return
        if filled.qty <= 0 or filled.price <= 0:
            logger.error("Ordine %s senza fill valido: %s", side.upper(), filled)
            return
        fee = filled.fee if filled.fee is not None else self.cfg.taker_rate * filled.notional
        closed = st.book.apply_fill(side, filled.qty, filled.price, fee)
        st.orders_count += 1
        self.last_price = filled.price
        book = st.book
        be = breakeven(book)
        logger.info("Ordine #%d %s %g %s @ %.5f (%.2f %s, fee %.4f) -> posizione %s %g, BE %s",
                    st.orders_count, side.upper(), filled.qty, self.cfg.base_asset, filled.price, filled.notional,
                    self.cfg.settle_coin, fee, (book.side or "flat").upper(), book.qty, f"{be:.5f}" if be else "-")
        try:
            self.analytics.record_order(OrderRecord(
                timestamp_ms=filled.timestamp_ms, mode=st.mode, side=side, qty=filled.qty, price=filled.price,
                fee=fee, position_side=book.side or "flat", position_qty=book.qty))
        except Exception:
            logger.exception("Registrazione ordine nello storico fallita.")
        if closed is not None:
            self._on_episode_closed(closed)

    def _maybe_reverse(self, price: Optional[float]) -> None:
        if price is None:
            return
        st = self.state
        new_mode = reversal_signal(st.mode, st.book, price, self.cfg.reversal_pct)
        if new_mode is None:
            return
        be = breakeven(st.book)
        logger.info("INVERSIONE %s -> %s: prezzo %.5f, posizione %s %g, BE %.5f (oltre il %.2f%%).",
                    st.mode.upper(), new_mode.upper(), price, st.book.side.upper(), st.book.qty, be,
                    self.cfg.reversal_pct)
        try:
            self.analytics.record_reversal(ReversalRecord(
                timestamp_ms=int(self.now() * 1000), from_mode=st.mode, to_mode=new_mode, price=price,
                breakeven=be, position_side=st.book.side, position_qty=st.book.qty))
        except Exception:
            logger.exception("Registrazione inversione nello storico fallita.")
        self._notify("Inversione", [f"{st.mode.upper()} → <b>{new_mode.upper()}</b> a {price:.5f}",
                                    f"Posizione {st.book.side.upper()} {st.book.qty:g} {self.cfg.base_asset}, BE {be:.5f}"])
        st.mode = new_mode

    def _on_episode_closed(self, ep: ClosedEpisode) -> None:
        logger.info("Posizione %s (episodio #%d) chiusa: netto %.4f %s (lordo %.4f, fee %.4f, funding %.4f).",
                    ep.side.upper(), ep.episode_id, ep.net, self.cfg.settle_coin, ep.realized_gross, ep.fees,
                    ep.funding)
        try:
            self.analytics.record_episode(EpisodeRecord(
                timestamp_ms=int(self.now() * 1000), episode_id=ep.episode_id, side=ep.side,
                realized_gross=ep.realized_gross, fees=ep.fees, funding=ep.funding, net=ep.net))
        except Exception:
            logger.exception("Registrazione episodio nello storico fallita.")
        self._notify(f"Posizione {ep.side.upper()} chiusa", [f"Netto: <b>{ep.net:+.4f} {self.cfg.settle_coin}</b>"])
        self._funding_ids.clear()
        self._funding_since_ms = None

    async def _reconcile_with_exchange(self) -> None:
        """Bybit's real net position is the source of truth for SIDE and QTY.
        A mismatch means someone changed the position outside the bot (e.g. a
        manual close): the local book is resynced and the event logged."""
        try:
            pos = await self.exchange.fetch_position()
        except Exception:
            logger.warning("Lettura posizione per la riconciliazione fallita.", exc_info=True)
            return
        book = self.state.book
        step = max(self.exchange.qty_step(), 1e-9)
        if pos is None and book.is_flat:
            return
        if pos is not None and not book.is_flat and pos.side == book.side and abs(pos.qty - book.qty) < step / 2:
            return
        logger.warning("Posizione su Bybit (%s) diversa da quella del bot (%s %g): intervento esterno? "
                       "Riallineo.", f"{pos.side.upper()} {pos.qty:g}" if pos else "FLAT",
                       (book.side or "flat").upper(), book.qty)
        next_id = book.episode_id + 1
        self.state.book = self._book_from_exchange(pos, next_id) if pos else PositionBook(episode_id=next_id)
        self._funding_ids.clear()
        self._funding_since_ms = None
        self._notify("Riallineamento", ["Posizione modificata fuori dal bot: stato riallineato a Bybit."])

    # -- funding -------------------------------------------------------------

    async def _maybe_poll_funding(self) -> None:
        """REALIZED funding from the exchange ledger (every 8h on Bybit),
        deduplicated by settlement id, added to the current episode."""
        if self.state.book.is_flat:
            return
        now = self.now()
        if now - self._last_funding_poll < self.cfg.funding_poll_interval_sec:
            return
        self._last_funding_poll = now
        since = self._funding_since_ms or int(now * 1000) - 8 * 3600 * 1000
        try:
            settlements = await self.exchange.fetch_realized_funding(since_ms=since)
        except Exception:
            logger.exception("Lettura funding realizzato fallita")
            return
        for fid, ts, cashflow in settlements:
            self._funding_since_ms = max(self._funding_since_ms or 0, ts + 1)
            if fid in self._funding_ids:
                continue
            self._funding_ids.add(fid)
            self.state.book.funding += cashflow
            logger.info("Funding realizzato: %+.6f %s", cashflow, self.cfg.settle_coin)

    # -- export / helpers ----------------------------------------------------

    def _export(self, price: float) -> None:
        st = self.state
        be = breakeven(st.book)
        th = reversal_thresholds(st.book, self.cfg.reversal_pct)
        self.exporter.export({
            "timestamp_ms": int(self.now() * 1000),
            "bot": BOT_NAME,
            "version": BOT_VERSION,
            "symbol": self.cfg.symbol,
            "price": price,
            "mode": st.mode,
            "base_notional": st.base_notional,
            "orders_count": st.orders_count,
            "position": st.book.to_dict(),
            "breakeven": be,
            "long_below": th[0] if th else None,
            "short_above": th[1] if th else None,
        })

    def _maybe_heartbeat(self, price: float) -> None:
        now = self.now()
        if now - self._last_heartbeat < HEARTBEAT_INTERVAL_SEC:
            return
        self._last_heartbeat = now
        st = self.state
        be = breakeven(st.book)
        dist = f"{(price / be - 1) * 100:+.3f}%" if be else "-"
        logger.info("Stato: direzione %s | posizione %s %g %s (%.2f %s) | prezzo %.5f | BE %s (%s) | ordini %d",
                    st.mode.upper(), (st.book.side or "flat").upper(), st.book.qty, self.cfg.base_asset,
                    st.book.notional, self.cfg.settle_coin, price, f"{be:.5f}" if be else "-", dist,
                    st.orders_count)

    def _notify(self, title: str, body: list) -> None:
        """Fire-and-forget Telegram message: local import, own try/except,
        strong reference kept until done -- never blocks the trading loop."""
        try:
            import notifier
            task = asyncio.create_task(notifier.notify_text(self.exchange, self.cfg.notifier_enabled, title, body))
            self._background_tasks.add(task)
            task.add_done_callback(self._background_tasks.discard)
        except Exception:
            logger.debug("Notifica '%s' non avviata.", title, exc_info=True)

    async def _wait_or_stop(self, timeout_sec: float) -> bool:
        self._check_stop_file()
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=timeout_sec)
            return True
        except asyncio.TimeoutError:
            return False

    def _check_stop_file(self) -> None:
        if not STOP_SIGNAL_PATH.exists():
            return
        try:
            STOP_SIGNAL_PATH.unlink()
        except OSError:
            pass
        logger.info("File '%s' rilevato: arresto pulito (la posizione resta aperta).", STOP_SIGNAL_PATH)
        self._stop_event.set()


async def _run() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # DEBUG only for the bot's own namespace (LOG_LEVEL=DEBUG): ccxt's own DEBUG
    # output would dump signed requests, API key header included.
    debug = os.environ.get("LOG_LEVEL", "").strip().upper() == "DEBUG"
    logging.getLogger("bot").setLevel(logging.DEBUG if debug else logging.INFO)
    cfg = StrategyConfig.load(CONFIG_PATH)
    bot = TrxBot(cfg)
    try:
        await bot.start()
    finally:
        await bot.stop()


def main() -> None:
    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        logger.info("%s arrestato dall'utente (KeyboardInterrupt).", BOT_NAME)


if __name__ == "__main__":
    main()
