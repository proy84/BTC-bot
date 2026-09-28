"""
tools/demo_spot_repay_test.py -- BTC bot, prova manuale "repay tramite spot" su Bybit DEMO.

Bybit Demo NON supporta /v5/account/repay ne' /v5/account/no-convert-repay
(retCode 10032). Il BTC bot ripaga quindi il debito di una moneta accreditandola
con un ordine SPOT. Questo script verifica che su un conto Unified l'accredito
azzeri davvero il saldo negativo (borrowAmount).

Cosa fa (solo host DEMO, si rifiuta di partire se USE_TESTNET non e' true):
  1. legge walletBalance / borrowAmount di USDC;
  2. vende a mercato ~SELL_USDC USDC di BTC su BTC/USDC (spot, minimo Bybit = 5 USDC);
  3. rilegge walletBalance / borrowAmount di USDC e dice se il debito e' sparito.

Uso:   python tools/demo_spot_repay_test.py
Le chiavi vengono lette da .env (mai stampate).
"""

import asyncio
import os
import sys
from pathlib import Path

import ccxt.async_support as ccxt  # type: ignore[import-untyped]
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

DEMO_HOST = "https://api-demo.bybit.com"
SPOT_SYMBOL = "BTC/USDC"
SELL_USDC = 6.0  # sopra il minimo spot Bybit di 5 USDC


def _usdc_state(wallet: dict) -> tuple[float, float]:
    for c in wallet["result"]["list"][0]["coin"]:
        if c["coin"] == "USDC":
            return float(c.get("walletBalance") or 0.0), float(c.get("borrowAmount") or 0.0)
    return 0.0, 0.0


async def main() -> None:
    if os.environ.get("USE_TESTNET", "true").strip().lower() not in ("1", "true", "yes", "on"):
        sys.exit("USE_TESTNET non e' true: questo script gira SOLO su Bybit Demo. Interrotto.")

    public = ccxt.bybit({"enableRateLimit": True})
    private = ccxt.bybit({
        "apiKey": os.environ["BYBIT_API_KEY"],
        "secret": os.environ["BYBIT_API_SECRET"],
        "enableRateLimit": True,
        "urls": {"api": {"private": DEMO_HOST}},
        "options": {"adjustForTimeDifference": True},
    })
    try:
        private.set_markets(await public.load_markets())
        await private.load_time_difference()

        before = _usdc_state(await private.privateGetV5AccountWalletBalance({"accountType": "UNIFIED"}))
        print(f"PRIMA : USDC walletBalance={before[0]:.8f} borrowAmount={before[1]:.8f}")

        price = float((await public.fetch_ticker(SPOT_SYMBOL))["last"])
        qty = float(private.amount_to_precision(SPOT_SYMBOL, SELL_USDC / price))
        print(f"Vendo {qty} BTC su {SPOT_SYMBOL} a mercato (~{qty * price:.2f} USDC, prezzo ~{price:.2f})...")
        order = await private.create_order(SPOT_SYMBOL, "market", "sell", qty)
        print(f"Ordine inviato: id={order.get('id')}")

        await asyncio.sleep(3)
        after = _usdc_state(await private.privateGetV5AccountWalletBalance({"accountType": "UNIFIED"}))
        print(f"DOPO  : USDC walletBalance={after[0]:.8f} borrowAmount={after[1]:.8f}")

        if before[1] > 0 and after[1] == 0:
            print("RISULTATO: OK -- l'accredito spot ha azzerato il debito USDC.")
        elif before[1] == 0:
            print("RISULTATO: nessun debito USDC prima della prova, test non conclusivo.")
        else:
            print("RISULTATO: il debito USDC NON si e' azzerato con l'accredito spot.")
    finally:
        await public.close()
        await private.close()


if __name__ == "__main__":
    asyncio.run(main())
