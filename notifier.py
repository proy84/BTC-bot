"""
notifier.py

Optional, isolated Telegram notifications for the bot: startup, direction
reversals and closed position episodes, each with account equity.

FAIL-SAFE: every public coroutine runs its whole body under one try/except
and only ever logs a WARNING -- it is scheduled by main.py as a background
task, and nothing that happens here can reach the trading logic. Missing
TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID in .env -> silent no-op.
"""

from __future__ import annotations

import asyncio
import logging
import os
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, List, Optional

from strategy import BOT_NAME

logger = logging.getLogger("bot.notifier")

_TELEGRAM_API_URL = "https://api.telegram.org/bot{token}/sendMessage"
EUR_RATE_SYMBOL = "USDT/EUR"  # Bybit spot pair, no external FX API
_warned_once = False


def _credentials() -> Optional[tuple]:
    global _warned_once
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        if not _warned_once:
            logger.info("Notifiche Telegram disabilitate: TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID non configurati.")
            _warned_once = True
        return None
    return token, chat_id


def _send_sync(token: str, chat_id: str, text: str) -> None:
    url = _TELEGRAM_API_URL.format(token=token)
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": text, "parse_mode": "HTML"}).encode("utf-8")
    with urllib.request.urlopen(url, data=data, timeout=10) as resp:
        resp.read()


def _signed(v: float) -> str:
    return f"{'+' if v >= 0 else ''}{v:.4f}"


async def _equity_lines(exchange_client: Any) -> List[str]:
    lines: List[str] = []
    try:
        equity = await exchange_client.fetch_total_equity()
    except Exception:
        logger.warning("Notifica: lettura equity fallita (omessa).", exc_info=True)
        return lines
    if not equity:
        return lines
    line = f"\U0001F4B0 Equity totale: <b>{equity:.2f} USD</b>"
    try:
        ticker = await exchange_client._retry(exchange_client._public.fetch_ticker, EUR_RATE_SYMBOL)
        line += f" (~<b>{equity * float(ticker.get('last')):.2f} EUR</b>)"
    except Exception:
        logger.warning("Notifica: conversione USD->EUR fallita (omessa).", exc_info=True)
    lines.append(line)
    try:
        balances = await exchange_client.fetch_coin_balances()
        for coin in ("USDT", "USDC", "BTC", "ETH"):
            b = balances.get(coin)
            if b is None:
                continue
            extra = f" (debito {b.borrow_amount:.4f})" if b.borrow_amount > 0 else ""
            lines.append(f"  • {coin}: {b.wallet_balance:.6f}{extra}")
    except Exception:
        logger.warning("Notifica: lettura saldi per moneta fallita (omessa).", exc_info=True)
    return lines


async def notify_text(exchange_client: Any, enabled: bool, title: str, body: List[str],
                      include_equity: bool = True) -> None:
    if not enabled:
        return
    try:
        creds = _credentials()
        if creds is None:
            return
        lines = [f"<b>{BOT_NAME} — {title}</b>", *body]
        if include_equity and exchange_client is not None:
            lines.extend(await _equity_lines(exchange_client))
        lines.append(f"⏱️ {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
        await asyncio.to_thread(_send_sync, creds[0], creds[1], "\n".join(lines))
    except Exception:
        logger.warning("Notifica Telegram '%s' fallita, ignorata.", title, exc_info=True)
