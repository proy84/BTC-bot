# Documentazione tecnica — TRX bot v3.0 (accumulo one-way, Bybit V5)

TRX bot opera su **un solo perpetual**, **TRX/USDC** (Bybit `TRXPERP`, regolato in USDC), leva **75x** (massimo consentito), margine cross, **modalità one-way** (una sola posizione netta).
Lavora su **Bybit Demo Trading** (`USE_TESTNET=true` in `.env`, default).

> Repository GitHub `proy84/BTC-bot` (remote git `btcbot`), branch **`trx-bot`**.
> La strategia precedente (hedge BTC a due gambe) è archiviata nel tag `btc-hedge-final` (branch `btc-bot-v2`).
> Il remote `origin` (eth_grid_bot) ha il push disattivato.

---

## 1. Strategia

1. **Avvio**: apre subito uno **SHORT** a mercato dell'**importo minimo consentito da Bybit** (5 USDC su TRXPERP; config `base_notional_usd: 0`, `equity_based_sizing.enabled: false`). La quantità è arrotondata **per eccesso** con +1% di margine (es. 16–17 TRX), così l'ordine non scende mai sotto il minimo nemmeno se il prezzo si muove. In alternativa: importo fisso (`base_notional_usd > 0`) o % dell'equity (`equity_based_sizing.enabled: true`, letto al primo avvio).
2. **Ogni minuto** — contato **dall'ordine precedente** (60 s), non allineato all'orologio — spara **un ordine a mercato dello stesso importo** nella **direzione attiva**. (Correzione 29/09: con l'allineamento all'orologio il primo e il secondo ordine partivano a pochi secondi di distanza.)
3. **Posizione netta one-way**: gli ordini nella direzione opposta alla posizione la **riducono** (realizzando il PnL della parte ridotta); se la superano la posizione **cambia segno**.
4. **Inversione** (`reversal_pct`, default 0,5%): quando la posizione netta è in profitto oltre lo 0,5% rispetto al suo **breakeven netto**:
   - posizione **SHORT** e prezzo ≤ BE × (1 − 0,5%) → direzione attiva **LONG**;
   - posizione **LONG** e prezzo ≥ BE × (1 + 0,5%) → direzione attiva **SHORT**.
   L'ordine del minuto in corso parte già nella nuova direzione.
5. **Nessun take profit e nessun limite** per ora: ordini all'infinito (regola del TP da definire).

### Breakeven netto (`strategy.breakeven_net`)
Prezzo al quale chiudendo **tutta** la posizione a mercato l'episodio corrente finisce a **zero netto**: include il PnL già realizzato con le riduzioni, **tutte le fee pagate**, il **funding** e la **fee di chiusura stimata** (taker).

```
SHORT:  p = (R + A·Q) / (Q·(1 + f))
LONG:   p = (A·Q − R) / (Q·(1 − f))
R = realizzato lordo − fee + funding dell'episodio,  A = prezzo medio,  Q = quantità,  f = taker rate
```

**Episodio**: dall'apertura da flat fino al ritorno a flat (o al cambio di segno, che chiude l'episodio e ne apre uno nuovo dall'altro lato con la quantità in eccesso; la fee di quell'ordine è ripartita pro-quota). Il netto di ogni episodio chiuso è registrato nello storico.

---

## 2. Struttura del progetto

| File | Ruolo |
|---|---|
| `main.py` | Orchestratore `TrxBot`: tick ogni 2 s, riconciliazione con Bybit, funding, inversione, un ordine per minuto, stato persistente, notifiche. |
| `strategy.py` | Config (`StrategyConfig`) e logica pura: `PositionBook` (netting one-way), `breakeven_net`, `reversal_signal`, sizing, arrotondamento qty, `BotState`. |
| `fees.py` | Primitive: `FeeSchedule`, `gross_pnl` long/short. |
| `exchange.py` | Gateway CCXT a Bybit V5: istanza pubblica (prezzi, metadati) + privata Demo (ordini, posizione, saldo, funding). Dedup ordini per `clientOrderId`. |
| `analytics.py` | Storico `trx_bot_history.json`: ordini, inversioni, episodi chiusi, riepilogo. |
| `data_exporter.py` | Snapshot live `trx_bot_live.json` (direzione, posizione, BE netto, prezzo di inversione). |
| `notifier.py` | Telegram opzionale e isolato: avvio, inversioni, episodi chiusi, riallineamenti. |
| `config.json` | Parametri (nessun segreto). |
| `.env` | **Solo locale**: `BYBIT_API_KEY`, `BYBIT_API_SECRET`, `USE_TESTNET`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`. Modello: `.env.example`. |
| `tests/` | Test pytest. |

File di runtime (ignorati da git): `trx_bot_state.json`, `trx_bot_history.json`, `trx_bot_live.json`, `STOP`, `bot.log`.

Avvio: `python main.py` · Test: `python -m pytest` · Log dettagliati: `LOG_LEVEL=DEBUG python main.py` · Arresto pulito: creare un file `STOP` nella cartella (la posizione resta aperta, lo stato è salvato).

---

## 3. Dettagli di funzionamento

- **Riconciliazione a ogni tick**: la posizione netta reale su Bybit (lato e quantità) è la fonte di verità. Se non coincide con quella del bot (es. **chiusura manuale** da app), il bot lo segnala nel log/Telegram e si **riallinea** (nuovo episodio; prezzo medio preso da Bybit, fee di apertura stimate). Il prezzo medio del bot viene invece dai **propri fill**, perché sui perpetual USDC il settlement di sessione (ogni 8 h) può riscrivere l'`entryPrice` di Bybit.
- **Un tentativo ogni 60 s**: se un ordine fallisce (es. margine insufficiente) viene loggato e si riprova al minuto successivo, senza raffiche.
- **Fee reali**: quelle riportate da Bybit nel fill; se assenti, stima `taker_rate × notional`.
- **Funding realizzato**: letto dal registro Bybit (`/v5/execution/list`, supportato su Demo) ogni `funding_poll_interval_sec`, deduplicato per id, sommato all'episodio corrente. **Segno**: Bybit/ccxt lo riportano come *fee* (positivo = pagato); il bot lo converte in cashflow (positivo = incassato) — verificato su Demo il 29/09/2026 (test `test_funding_sign.py`).
- **Riavvio**: `trx_bot_state.json` conserva direzione attiva, importo per ordine, posizione/episodio e ultimo minuto servito: nessun ordine extra al riavvio.
- **Due istanze CCXT**: `_public` (produzione, non autenticata) e `_private` (host `https://api-demo.bybit.com` con `USE_TESTNET=true`); il catalogo mercati del privato è copiato dal pubblico (`load_markets()` sul Demo fallisce con 10032).
- **Dedup ordini**: su errore di rete il bot verifica su Bybit se l'ordine col proprio `clientOrderId` esiste prima di reinviarlo.

---

## 4. Configurazione (`config.json`)

| Campo | Default | Significato |
|---|---|---|
| `symbol` | `TRX/USDC:USDC` | perpetual TRXPERP |
| `leverage` | 75 | leva (massimo Bybit su TRXPERP) |
| `margin_mode` | `cross` | |
| `timeframe` | `1m` | un ordine per timeframe |
| `initial_direction` | `short` | direzione del primo ordine |
| `reversal_pct` | 0.5 | % oltre il breakeven netto che fa invertire la direzione |
| `fees.taker_rate` / `maker_rate` | 0.00055 / 0.0002 | per stime (le fee reali vengono dai fill) |
| `equity_based_sizing.enabled` / `percentage` | false / 1.0 | se true: importo per ordine = % dell'equity al primo avvio |
| `base_notional_usd` | 0 | 0 = minimo exchange (5 USDC); > 0 = importo fisso (mai sotto il minimo). In modalità fissa il valore viene riletto a ogni riavvio |
| `notifier.enabled` | true | Telegram (richiede le variabili in `.env`) |
| `polling.tick_poll_interval_sec` | 2 | frequenza del tick |
| `polling.funding_poll_interval_sec` | 300 | frequenza lettura funding |
| `paths.*` | `trx_bot_*.json` | storico, snapshot live, stato persistente |

Limiti Bybit su TRXPERP (verificati 29/09/2026): leva max 75x, qty minima 1 TRX, passo 1 TRX, ordine minimo 5 USDC.

---

## 5. Test
`python -m pytest`:
- `tests/test_position_book.py`: accumulo e prezzo medio, riduzione con PnL realizzato, chiusura esatta, cambio di segno con fee pro-quota.
- `tests/test_breakeven_reversal.py`: breakeven netto che azzera l'episodio (fee, funding, parte realizzata), soglie di inversione esatte short→long e long→short, nessuna inversione in perdita, arrotondamento qty TRX, quantità mai sotto il valore minimo d'ordine.
- `tests/test_bot_flow.py`: orchestratore con exchange finto e orologio simulato — primo ordine all'avvio, un ordine ogni 60 s dall'ordine precedente, sizing al minimo exchange, inversione, cambio di segno e chiusura episodio, ritorno a short, riallineamento dopo chiusura manuale, ripresa dopo riavvio.
- `tests/test_funding_sign.py`: conversione del segno del funding con i valori reali Demo.

---

## 6. Punti aperti
1. **Regola del take profit**: da definire.
2. **Nessun limite di esposizione**: la posizione cresce di un ordine al minuto finché il prezzo non va in profitto oltre lo 0,5%; con prezzo contro, margine e rischio crescono senza tetto. Da decidere un limite (qty massima, margine massimo o stop).
