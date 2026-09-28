"""
main.py

BTC bot -- two-leg hedge orchestrator (Bybit V5, Demo Trading by default).

Loop (every `polling.tick_poll_interval_sec`), always under `_position_lock`:
  1. If a CLOSE ALL is in progress, keep closing whatever is still open; once
     both legs are flat, settle the cycle (see 4).
  2. Otherwise make sure both legs are open at the sequence's target notional
     (BTC/USDT SHORT + BTC/USDC LONG, market orders). A leg that failed to
     open is retried after OPEN_RETRY_DELAY_SEC.
  3. With both legs open: poll realized funding, evaluate the two-leg net TP
     (`strategy.check_take_profit`). A hit starts CLOSE ALL.
  4. Settlement: realized net per coin -> sequence totals -> decision
     (`strategy.decide_after_close`):
       - NEW_SEQUENCE (only when the winning coin can repay the losing coin
         AND has >= the spot minimum left for BTC): repay via a spot
         conversion, buy BTC spot with the rest, restart at base notional;
       - MULTIPLY: cumulative per-leg multipliers (TP leg x2, other x1.5);
       - STOP (only if max_multiplier_steps is set): stay flat and halt.
     Then the next cycle opens immediately.

State that must survive a restart (sequence id, step, per-leg target
notionals, per-coin sequence net) lives in `paths.runtime_state_path`.

Clean remote shutdown: create a file named STOP next to main.py.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Dict, Optional

from analytics import AnalyticsEngine, CycleRecord, LegResult
from data_exporter import DataExporter
from exchange import ExchangeClient, FilledOrder
from fees import LegPosition, gross_pnl, realized_leg_net
from strategy import (
    BOT_NAME, MULTIPLY, NEW_SEQUENCE, STOP, Decision, SequenceState, StrategyConfig,
    check_take_profit, decide_after_close, effective_base_notional, evaluate_legs,
    plan_spot_settlement, qty_for_notional,
)

logger = logging.getLogger("btc_bot.main")

BOT_VERSION = "2.0"
CONFIG_PATH = "config.json"
STOP_SIGNAL_PATH = Path("STOP")
OPEN_RETRY_DELAY_SEC = 30.0
HEARTBEAT_INTERVAL_SEC = 300.0


class BtcBot:
    def __init__(self, cfg: StrategyConfig):
        self.cfg = cfg
        self.fees = cfg.fee_schedule
        self.exchange = ExchangeClient(cfg)
        self.analytics = AnalyticsEngine(cfg.trade_history_path)
        self.exporter = DataExporter(cfg.state_export_path, self.analytics)
        self.runtime_state_path = Path(cfg.runtime_state_path)

        self.seq: Optional[SequenceState] = None
        self.legs: Dict[str, LegPosition] = {}
        self.cycle_start_ts_ms = int(time.time() * 1000)

        # CLOSE ALL in progress: the leg that hit TP, and the close fills
        # collected so far (a leg whose close failed is retried next tick).
        self._closing_tp_leg: Optional[str] = None
        self._close_fills: Dict[str, Optional[FilledOrder]] = {}
        self._close_marks: Dict[str, float] = {}

        # Realized funding, per leg: dedup by settlement id + moving window.
        self._funding_ids: Dict[str, set] = {}
        self._funding_since_ms: Dict[str, int] = {}
        self._last_funding_poll = 0.0

        self._open_retry_after = 0.0
        self._last_heartbeat = 0.0
        self._halted = False
        self._stop_event = asyncio.Event()
        self._position_lock = asyncio.Lock()
        self._background_tasks: set = set()

    # -- lifecycle -----------------------------------------------------------

    async def start(self) -> None:
        short, long_ = self.cfg.legs["short"], self.cfg.legs["long"]
        logger.info("[INFO] Avvio %s v%s -- SHORT %s + LONG %s, leva %dx, TP netto %.2f%%, "
                    "moltiplicatori x%.2f/x%.2f, max_multiplier_steps=%s, %s",
                    BOT_NAME, BOT_VERSION, short.symbol, long_.symbol, self.cfg.leverage,
                    self.cfg.take_profit_net_pct, self.cfg.winner_multiplier, self.cfg.loser_multiplier,
                    self.cfg.max_multiplier_steps, "DEMO" if self.cfg.use_testnet else "PRODUZIONE")
        await self.exchange.setup()
        await self._bootstrap()
        self._notify_text("Avvio", [
            f"Sequenza #{self.seq.sequence_id}, passo {self.seq.step}",
            f"Target: SHORT {self.seq.notionals['short']:.2f} / LONG {self.seq.notionals['long']:.2f}",
        ])
        await self._tick_loop()

    async def stop(self) -> None:
        self._stop_event.set()
        if self._background_tasks:
            await asyncio.gather(*self._background_tasks, return_exceptions=True)
        await self.exchange.close()

    # -- bootstrap / persistence --------------------------------------------

    async def _bootstrap(self) -> None:
        self.seq = self._load_runtime_state()
        positions = await self.exchange.fetch_open_positions()

        if self.seq is None:
            base = await self._compute_base_notional()
            last_seq = max((c.sequence_id for c in self.analytics.cycles), default=0)
            self.seq = SequenceState.new(base, sequence_id=last_seq + 1,
                                         cycle_id=len(self.analytics.cycles) + 1)
            logger.info("Nessuno stato salvato: nuova sequenza #%d, base %.2f per gamba.",
                        self.seq.sequence_id, base)

        for name, leg_cfg in self.cfg.legs.items():
            pos = positions.get(leg_cfg.symbol)
            if pos is None:
                continue
            if pos.side != leg_cfg.side:
                self._halted = True
                raise RuntimeError(
                    f"Posizione esistente su {leg_cfg.symbol} e' {pos.side.upper()}, attesa "
                    f"{leg_cfg.side.upper()}: chiudila a mano prima di avviare {BOT_NAME}."
                )
            self.legs[name] = LegPosition(
                name=name, symbol=leg_cfg.symbol, side=leg_cfg.side, settle_coin=leg_cfg.settle_coin,
                qty=pos.qty, entry_price=pos.entry_price,
                # the real opening fee is not recoverable after a restart: estimate it
                open_fee=self.fees.taker_fee_for_notional(pos.entry_price * pos.qty),
            )
            logger.warning("Posizione esistente riconciliata: %s %s qty=%.6f entry=%.2f (fee apertura stimata).",
                           leg_cfg.side.upper(), leg_cfg.symbol, pos.qty, pos.entry_price)
        self._save_runtime_state()

    def _load_runtime_state(self) -> Optional[SequenceState]:
        if not self.runtime_state_path.exists():
            return None
        try:
            seq = SequenceState.from_dict(json.loads(self.runtime_state_path.read_text(encoding="utf-8")))
            logger.info("Stato ripreso: sequenza #%d passo %d, target SHORT %.2f / LONG %.2f, netto %s.",
                        seq.sequence_id, seq.step, seq.notionals["short"], seq.notionals["long"],
                        seq.net_by_coin)
            return seq
        except Exception:
            logger.exception("Stato %s illeggibile: riparto da una nuova sequenza.", self.runtime_state_path)
            return None

    def _save_runtime_state(self) -> None:
        tmp = self.runtime_state_path.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(self.seq.to_dict(), indent=2), encoding="utf-8")
            tmp.replace(self.runtime_state_path)
        except OSError:
            logger.exception("Salvataggio stato %s fallito", self.runtime_state_path)

    async def _compute_base_notional(self) -> float:
        prices = await self.exchange.fetch_last_prices(l.symbol for l in self.cfg.legs.values())
        mins = {name: self.exchange.min_order_qty(l.symbol) * prices[l.symbol]
                for name, l in self.cfg.legs.items()}
        equity = 0.0
        if self.cfg.equity_based_sizing_enabled:
            try:
                equity = await self.exchange.fetch_total_equity()
            except Exception:
                logger.exception("Lettura equity fallita: uso base_notional_usd=%.2f.", self.cfg.base_notional_usd)
        base = effective_base_notional(equity, self.cfg.equity_based_sizing_percentage,
                                       self.cfg.base_notional_usd, mins)
        logger.info("Base notional per gamba: %.2f (equity %.2f x %.2f%%, minimi exchange %s).",
                    base, equity, self.cfg.equity_based_sizing_percentage,
                    {k: round(v, 2) for k, v in mins.items()})
        return base

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
        if self._halted:
            return
        prices = await self.exchange.fetch_last_prices(l.symbol for l in self.cfg.legs.values())
        marks = {name: prices[l.symbol] for name, l in self.cfg.legs.items()}

        async with self._position_lock:
            if self._closing_tp_leg is not None:
                await self._continue_close_all(marks)
                return

            await self._ensure_legs_open(marks)
            if len(self.legs) != len(self.cfg.legs):
                self._export(marks)
                return

            await self._maybe_poll_funding()
            signal = check_take_profit(self.legs, marks, self.fees, self.cfg.take_profit_net_pct)
            self._export(marks)
            self._maybe_heartbeat(marks)
            if signal is None:
                return

            bd = signal.breakdown
            logger.info("TAKE PROFIT gamba %s: netto due gambe %.4f (%.3f%% su notional %.2f) = lordo %.4f "
                        "- fee apertura %.4f - fee chiusura stimate %.4f + funding %.4f. CLOSE ALL.",
                        signal.leg.upper(), bd.net_pnl, bd.net_pct, bd.notional, bd.gross_pnl,
                        bd.open_fees, bd.close_fees_est, bd.funding)
            self._closing_tp_leg = signal.leg
            self._close_fills = {}
            self._close_marks = dict(marks)
            await self._continue_close_all(marks)

    async def _ensure_legs_open(self, marks: Dict[str, float]) -> None:
        missing = [name for name in self.cfg.legs if name not in self.legs]
        if not missing or time.monotonic() < self._open_retry_after:
            return
        if not self.legs:
            self.cycle_start_ts_ms = int(time.time() * 1000)
            self._reset_funding_tracking()
        for name in missing:
            leg_cfg = self.cfg.legs[name]
            target = self.seq.notionals[name]
            qty = qty_for_notional(target, marks[name], self.exchange.qty_step(leg_cfg.symbol),
                                   self.exchange.min_order_qty(leg_cfg.symbol))
            try:
                filled = await self.exchange.open_position_market(leg_cfg.symbol, leg_cfg.side, qty)
            except Exception:
                logger.exception("Apertura %s %s fallita (qty=%.6f): riprovo tra %.0fs.",
                                 leg_cfg.side.upper(), leg_cfg.symbol, qty, OPEN_RETRY_DELAY_SEC)
                self._open_retry_after = time.monotonic() + OPEN_RETRY_DELAY_SEC
                return
            if filled.qty <= 0 or filled.price <= 0:
                logger.error("Apertura %s senza fill valido (%s): riprovo tra %.0fs.", leg_cfg.symbol, filled,
                             OPEN_RETRY_DELAY_SEC)
                self._open_retry_after = time.monotonic() + OPEN_RETRY_DELAY_SEC
                return
            fee = filled.fee if filled.fee is not None else self.fees.taker_fee_for_notional(filled.notional)
            self.legs[name] = LegPosition(name=name, symbol=leg_cfg.symbol, side=leg_cfg.side,
                                          settle_coin=leg_cfg.settle_coin, qty=filled.qty,
                                          entry_price=filled.price, open_fee=fee)
            logger.info("Aperta %s %s: qty=%.6f @ %.2f (notional %.2f, target %.2f, fee %.4f) -- "
                        "sequenza #%d passo %d, ciclo #%d.",
                        leg_cfg.side.upper(), leg_cfg.symbol, filled.qty, filled.price, filled.notional,
                        target, fee, self.seq.sequence_id, self.seq.step, self.seq.cycle_id)

    # -- close all + settlement ---------------------------------------------

    async def _continue_close_all(self, marks: Dict[str, float]) -> None:
        for name, leg in self.legs.items():
            if name in self._close_fills:
                continue
            try:
                filled = await self.exchange.close_position_market(leg.symbol)
            except Exception:
                logger.exception("Chiusura %s fallita: riprovo al prossimo tick.", leg.symbol)
                return
            if filled is None:
                logger.error("Nessuna posizione trovata su %s in chiusura: uso il prezzo %.2f come uscita.",
                             leg.symbol, marks[name])
            self._close_fills[name] = filled
            self._close_marks[name] = marks[name]
        await self._settle_cycle()

    async def _settle_cycle(self) -> None:
        tp_leg = self._closing_tp_leg
        seq = self.seq
        leg_results = []
        net_by_coin: Dict[str, float] = {}
        for name, leg in self.legs.items():
            filled = self._close_fills.get(name)
            exit_price = filled.price if filled is not None and filled.price > 0 else self._close_marks[name]
            close_fee = (filled.fee if filled is not None and filled.fee is not None
                         else self.fees.taker_fee_for_notional(exit_price * leg.qty))
            net = realized_leg_net(leg, exit_price, close_fee)
            net_by_coin[leg.settle_coin] = net_by_coin.get(leg.settle_coin, 0.0) + net
            leg_results.append(LegResult(
                leg=name, symbol=leg.symbol, side=leg.side, settle_coin=leg.settle_coin, qty=leg.qty,
                entry_price=leg.entry_price, exit_price=exit_price, notional=leg.notional,
                gross_pnl=gross_pnl(leg.side, leg.entry_price, exit_price, leg.qty),
                open_fee=leg.open_fee, close_fee=close_fee, funding=leg.funding, net_pnl=net,
            ))

        seq.add_cycle_result(net_by_coin)
        decision = decide_after_close(seq, tp_leg, self.cfg.winner_multiplier, self.cfg.loser_multiplier,
                                      self.cfg.max_multiplier_steps, self.cfg.spot_min_order_value)
        logger.info("CLOSE ALL completato (ciclo #%d): netto ciclo %s | netto sequenza #%d %s (totale %.4f) "
                    "-> %s", seq.cycle_id, _fmt(net_by_coin), seq.sequence_id, _fmt(seq.net_by_coin),
                    seq.total_net, decision.action.upper())

        spot_actions = []
        if decision.action == NEW_SEQUENCE and self.cfg.settlement_enabled:
            spot_actions = await self._spot_settlement(decision)

        record = CycleRecord(
            cycle_id=seq.cycle_id, sequence_id=seq.sequence_id, step=seq.step,
            start_ts_ms=self.cycle_start_ts_ms, end_ts_ms=int(time.time() * 1000), tp_leg=tp_leg,
            legs=leg_results, net_by_coin=net_by_coin, sequence_net_by_coin=dict(seq.net_by_coin),
            decision=decision.action, spot_actions=spot_actions,
        )
        try:
            self.analytics.record_cycle(record)
        except Exception:
            logger.exception("Registrazione ciclo nello storico fallita (stato del bot comunque aggiornato).")
        self._spawn_background("notifier", "notify_cycle_closed", self.exchange, self.cfg.notifier_enabled, record)

        # -- next cycle --
        if decision.action == NEW_SEQUENCE:
            base = await self._compute_base_notional()
            self.seq = SequenceState.new(base, sequence_id=seq.sequence_id + 1, cycle_id=seq.cycle_id + 1)
        elif decision.action == MULTIPLY:
            seq.notionals = decision.next_notionals
            seq.step += 1
            seq.cycle_id += 1
        else:  # STOP
            seq.cycle_id += 1
            self._halted = True
            logger.warning("max_multiplier_steps=%s raggiunto: %s resta FLAT e si ferma.",
                           self.cfg.max_multiplier_steps, BOT_NAME)
            self._notify_text("STOP", [f"max_multiplier_steps={self.cfg.max_multiplier_steps} raggiunto: "
                                       "posizioni chiuse, nessuna riapertura."])
            self._stop_event.set()

        self.legs = {}
        self._closing_tp_leg = None
        self._close_fills = {}
        self._close_marks = {}
        self._reset_funding_tracking()
        self._save_runtime_state()
        if decision.action != STOP:
            logger.info("Nuovo ciclo #%d (sequenza #%d passo %d): SHORT %.2f / LONG %.2f.",
                        self.seq.cycle_id, self.seq.sequence_id, self.seq.step,
                        self.seq.notionals["short"], self.seq.notionals["long"])
            prices = await self.exchange.fetch_last_prices(l.symbol for l in self.cfg.legs.values())
            await self._ensure_legs_open({n: prices[l.symbol] for n, l in self.cfg.legs.items()})

    async def _spot_settlement(self, decision: Decision) -> list:
        """Winning coin covers the losing coin's sequence loss via a SPOT
        conversion on the stable pair (Bybit Demo rejects /v5/account/repay),
        then buys BTC spot with the rest. Every step is best-effort: a failure
        is logged and reported, never blocks the next cycle."""
        actions = []
        winner, loser = decision.winner_coin, decision.loser_coin
        plan = decision.plan or plan_spot_settlement(
            decision.repay_amount, decision.btc_buy_amount + decision.repay_amount, self.cfg.spot_min_order_value)
        actions.extend(plan.notes)
        stable = self.cfg.stable_conversion_symbol  # e.g. "USDC/USDT"
        base_coin, quote_coin = stable.split("/")

        if plan.convert_amount > 0 and loser:
            try:
                if winner == quote_coin and loser == base_coin:
                    f = await self.exchange.spot_market_buy_with_cost(stable, plan.convert_amount)
                elif winner == base_coin and loser == quote_coin:
                    f = await self.exchange.spot_market_sell(stable, plan.convert_amount)
                else:
                    raise ValueError(f"Coppia {stable} non adatta a convertire {winner}->{loser}")
                msg = f"repay {loser}: convertiti {plan.convert_amount:.4f} {winner} ({f.side} {f.qty:.4f} @ {f.price:.5f})"
                logger.info("Settlement: %s", msg)
                actions.append(msg)
            except Exception:
                logger.exception("Settlement: conversione %s->%s fallita.", winner, loser)
                actions.append(f"repay {loser} FALLITO")

            try:
                balances = await self.exchange.fetch_coin_balances()
                borrow = balances[loser].borrow_amount if loser in balances else 0.0
                if borrow > 0:
                    if await self.exchange.repay_via_endpoint(loser, borrow):
                        actions.append(f"repay endpoint {loser} {borrow:.4f} OK")
                    else:
                        logger.warning("Settlement: debito %s residuo %.6f dopo la conversione.", loser, borrow)
                        actions.append(f"debito {loser} residuo {borrow:.6f}")
            except Exception:
                logger.warning("Settlement: verifica debito %s fallita.", loser, exc_info=True)

        if plan.btc_buy_amount > 0:
            spot_symbol = next(l.spot_btc_symbol for l in self.cfg.legs.values() if l.settle_coin == winner)
            try:
                f = await self.exchange.spot_market_buy_with_cost(spot_symbol, plan.btc_buy_amount)
                msg = f"acquistati {f.qty:.6f} BTC su {spot_symbol} @ {f.price:.2f} ({plan.btc_buy_amount:.4f} {winner})"
                logger.info("Settlement: %s", msg)
                actions.append(msg)
            except Exception:
                logger.exception("Settlement: acquisto BTC spot su %s fallito.", spot_symbol)
                actions.append(f"acquisto BTC su {spot_symbol} FALLITO")
        return actions

    # -- funding -------------------------------------------------------------

    def _reset_funding_tracking(self) -> None:
        self._funding_ids = {name: set() for name in self.cfg.legs}
        self._funding_since_ms = {name: self.cycle_start_ts_ms for name in self.cfg.legs}
        self._last_funding_poll = 0.0

    async def _maybe_poll_funding(self) -> None:
        """REALIZED funding from the exchange ledger (Bybit settles every 8h),
        deduplicated by settlement id, with a per-leg window that advances past
        each processed settlement."""
        now = time.time()
        if now - self._last_funding_poll < self.cfg.funding_poll_interval_sec:
            return
        self._last_funding_poll = now
        for name, leg in self.legs.items():
            since = self._funding_since_ms.get(name, self.cycle_start_ts_ms)
            try:
                settlements = await self.exchange.fetch_realized_funding(leg.symbol, since_ms=since)
            except Exception:
                logger.exception("Lettura funding realizzato %s fallita", leg.symbol)
                continue
            seen = self._funding_ids.setdefault(name, set())
            for fid, ts, cashflow in settlements:
                self._funding_since_ms[name] = max(self._funding_since_ms.get(name, since), ts + 1)
                if fid in seen:
                    continue
                seen.add(fid)
                leg.funding += cashflow
                logger.info("Funding realizzato %s: %.6f %s", leg.symbol, cashflow, leg.settle_coin)

    # -- export / helpers ----------------------------------------------------

    def _export(self, marks: Dict[str, float]) -> None:
        breakdowns = evaluate_legs(self.legs, marks, self.fees) if len(self.legs) == len(self.cfg.legs) else {}
        live = {
            "timestamp_ms": int(time.time() * 1000),
            "bot": BOT_NAME,
            "version": BOT_VERSION,
            "sequence": self.seq.to_dict() if self.seq else None,
            "cycle_start_ts_ms": self.cycle_start_ts_ms,
            "take_profit_net_pct": self.cfg.take_profit_net_pct,
            "legs": {
                name: {
                    "symbol": leg.symbol, "side": leg.side, "settle_coin": leg.settle_coin,
                    "qty": leg.qty, "entry_price": leg.entry_price, "notional": leg.notional,
                    "mark_price": marks.get(name), "open_fee": leg.open_fee, "funding": leg.funding,
                    "two_leg_net": breakdowns[name].net_pnl if name in breakdowns else None,
                    "two_leg_net_pct": breakdowns[name].net_pct if name in breakdowns else None,
                }
                for name, leg in self.legs.items()
            },
            "closing": self._closing_tp_leg,
            "halted": self._halted,
        }
        self.exporter.export(live)

    def _maybe_heartbeat(self, marks: Dict[str, float]) -> None:
        now = time.monotonic()
        if now - self._last_heartbeat < HEARTBEAT_INTERVAL_SEC:
            return
        self._last_heartbeat = now
        bds = evaluate_legs(self.legs, marks, self.fees)
        logger.info("Stato: ciclo #%d seq #%d passo %d | SHORT netto2g %.3f%% | LONG netto2g %.3f%% | "
                    "obiettivo %.2f%%", self.seq.cycle_id, self.seq.sequence_id, self.seq.step,
                    bds["short"].net_pct, bds["long"].net_pct, self.cfg.take_profit_net_pct)

    def _notify_text(self, title: str, body: list) -> None:
        self._spawn_background("notifier", "notify_text", self.exchange, self.cfg.notifier_enabled, title, body)

    def _spawn_background(self, module: str, func: str, *args) -> None:
        """Fire-and-forget optional feature (notifier): local import, own
        try/except, strong reference kept until done -- it can never block or
        crash the trading loop."""
        try:
            mod = __import__(module)
            task = asyncio.create_task(getattr(mod, func)(*args))
            self._background_tasks.add(task)
            task.add_done_callback(self._background_tasks.discard)
        except Exception:
            logger.debug("Task in background %s.%s non avviato.", module, func, exc_info=True)

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
        logger.info("File '%s' rilevato: arresto pulito (le posizioni restano aperte).", STOP_SIGNAL_PATH)
        self._stop_event.set()


def _fmt(d: Dict[str, float]) -> str:
    return ", ".join(f"{k} {v:+.4f}" for k, v in d.items()) or "-"


async def _run() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # DEBUG only for the bot's own namespace (LOG_LEVEL=DEBUG): ccxt's own DEBUG
    # output would dump signed requests, API key header included.
    debug = os.environ.get("LOG_LEVEL", "").strip().upper() == "DEBUG"
    logging.getLogger("btc_bot").setLevel(logging.DEBUG if debug else logging.INFO)
    cfg = StrategyConfig.load(CONFIG_PATH)
    bot = BtcBot(cfg)
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
