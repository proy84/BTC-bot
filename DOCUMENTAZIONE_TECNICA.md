# Documentazione tecnica — ETH bot v3.1 (accumulo one-way, Bybit V5)

Il bot opera su **un solo perpetual**, **ETH/USDT** (Bybit `ETHUSDT`, regolato in USDT), leva **150x** (massimo consentito), margine cross, **modalità one-way** (una sola posizione netta).
Lavora su **Bybit Demo Trading** (`USE_TESTNET=true` in `.env`, default).

> Repository GitHub `proy84/BTC-bot` (remote git `btcbot`), branch **`trx-bot`**.
> Storia: il 29/09 la strategia è nata su TRX/USDC; il 30/09 è passata a ETH/USDT perché TRX/USDC su Bybit era quasi senza scambi (≈18.000 USDC/giorno, prezzo fermo per decine di minuti). La strategia hedge BTC precedente è nel tag `btc-hedge-final`.
> Il remote `origin` (eth_grid_bot) ha il push disattivato: questo progetto è separato dal grid bot ETH.

---

## 1. Strategia

1. **Avvio**: apre subito uno **SHORT** a mercato dell'**importo minimo consentito** (su ETHUSDT la quantità minima è 0,01 ETH ≈ 27 USDT; config `base_notional_usd: 0`, `equity_based_sizing.enabled: false`). Quantità arrotondata per eccesso, mai sotto il minimo.
2. **Ogni minuto** — contato **dall'ordine precedente** (60 s) — spara **un ordine a mercato dello stesso importo** nella **direzione attiva**.
3. **Posizione netta one-way**: gli ordini opposti alla posizione la **riducono** (il guadagno/perdita della parte ridotta viene **realizzato**, cioè incassato nel saldo); se la superano la posizione **cambia segno**.
4. **Inversione** — regola **simmetrica sul prezzo**, con **breakeven = prezzo medio d'ingresso** della posizione netta, **senza fee né funding** (`reversal_pct` = 0,5%):
   - prezzo ≤ BE × (1 − 0,5%) → da quel minuto si sparano **LONG**;
   - prezzo ≥ BE × (1 + 0,5%) → da quel minuto si sparano **SHORT**;
   - in mezzo → si continua nella direzione attuale.
   Vale qualunque sia il lato della posizione: es. short in riduzione (direzione LONG) e prezzo che risale sopra BE + 0,5% → si torna a sparare SHORT.
5. **Nessun take profit e nessun limite** per ora: ordini all'infinito.

**Episodio**: dall'apertura da flat fino al ritorno a flat (o al cambio di segno, che chiude l'episodio e ne apre uno nuovo dall'altro lato al prezzo del fill: il breakeven del nuovo lato riparte da quel prezzo). Il netto di ogni episodio chiuso (PnL realizzato − fee + funding) è registrato nello storico.

---

## 2. Struttura del progetto

| File | Ruolo |
|---|---|
| `main.py` | Orchestratore `TrxBot`: tick ogni 2 s, riconciliazione con Bybit, funding, inversione, un ordine per minuto, stato persistente, notifiche. |
| `strategy.py` | Config (`StrategyConfig`) e logica pura: `PositionBook` (netting one-way), `breakeven_net`, `reversal_signal`, sizing, arrotondamento qty, `BotState`. |
| `fees.py` | Primitive: `FeeSchedule`, `gross_pnl` long/short. |
| `exchange.py` | Gateway CCXT a Bybit V5: istanza pubblica (prezzi, metadati) + privata Demo (ordini, posizione, saldo, funding). Dedup ordini per `clientOrderId`. |
| `analytics.py` | Storico `eth_bot_history.json`: ordini, inversioni, episodi chiusi, riepilogo. |
| `data_exporter.py` | Snapshot live `eth_bot_live.json` (direzione, posizione, BE netto, prezzo di inversione). |
| `notifier.py` | Telegram opzionale e isolato: avvio, inversioni, episodi chiusi, riallineamenti. |
| `config.json` | Parametri (nessun segreto). |
| `.env` | **Solo locale**: `BYBIT_API_KEY`, `BYBIT_API_SECRET`, `USE_TESTNET`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`. Modello: `.env.example`. |
| `tests/` | Test pytest. |

File di runtime (ignorati da git): `eth_bot_state.json`, `eth_bot_history.json`, `eth_bot_live.json`, `STOP`, `bot.log`.

Avvio: `python main.py` · Test: `python -m pytest` · Log dettagliati: `LOG_LEVEL=DEBUG python main.py` · Arresto pulito: creare un file `STOP` nella cartella (la posizione resta aperta, lo stato è salvato).

---

## 3. Dettagli di funzionamento

- **Riconciliazione a ogni tick**: la posizione netta reale su Bybit (lato e quantità) è la fonte di verità. Se non coincide con quella del bot (es. **chiusura manuale** da app), il bot lo segnala nel log/Telegram e si **riallinea** (nuovo episodio; prezzo medio preso da Bybit, fee di apertura stimate). Il prezzo medio del bot viene invece dai **propri fill**, perché sui perpetual USDC il settlement di sessione (ogni 8 h) può riscrivere l'`entryPrice` di Bybit.
- **Un tentativo ogni 60 s**: se un ordine fallisce (es. margine insufficiente) viene loggato e si riprova al minuto successivo, senza raffiche.
- **Fee reali**: quelle riportate da Bybit nel fill; se assenti, stima `taker_rate × notional`.
- **Funding realizzato**: letto dal registro Bybit (`/v5/execution/list`, supportato su Demo) ogni `funding_poll_interval_sec`, deduplicato per id, sommato all'episodio corrente. **Segno**: Bybit/ccxt lo riportano come *fee* (positivo = pagato); il bot lo converte in cashflow (positivo = incassato) — verificato su Demo il 29/09/2026 (test `test_funding_sign.py`).
- **Riavvio**: `eth_bot_state.json` conserva direzione attiva, importo per ordine, posizione/episodio e ultimo minuto servito: nessun ordine extra al riavvio.
- **Due istanze CCXT**: `_public` (produzione, non autenticata) e `_private` (host `https://api-demo.bybit.com` con `USE_TESTNET=true`); il catalogo mercati del privato è copiato dal pubblico (`load_markets()` sul Demo fallisce con 10032).
- **Dedup ordini**: su errore di rete il bot verifica su Bybit se l'ordine col proprio `clientOrderId` esiste prima di reinviarlo.

---

## 4. Configurazione (`config.json`)

| Campo | Default | Significato |
|---|---|---|
| `symbol` | `ETH/USDT:USDT` | perpetual ETHUSDT |
| `leverage` | 150 | leva (massimo Bybit su ETHUSDT) |
| `margin_mode` | `cross` | |
| `timeframe` | `1m` | un ordine per timeframe |
| `initial_direction` | `short` | direzione del primo ordine |
| `reversal_pct` | 0.5 | % di prezzo sotto/sopra il breakeven (prezzo medio, senza fee) che fa sparare LONG/SHORT |
| `fees.taker_rate` / `maker_rate` | 0.00055 / 0.0002 | per stime (le fee reali vengono dai fill) |
| `equity_based_sizing.enabled` / `percentage` | false / 1.0 | se true: importo per ordine = % dell'equity al primo avvio |
| `base_notional_usd` | 0 | 0 = minimo exchange (0,01 ETH ≈ 27 USDT); > 0 = importo fisso (mai sotto il minimo). In modalità fissa il valore viene riletto a ogni riavvio |
| `notifier.enabled` | true | Telegram (richiede le variabili in `.env`) |
| `polling.tick_poll_interval_sec` | 2 | frequenza del tick |
| `polling.funding_poll_interval_sec` | 300 | frequenza lettura funding |
| `paths.*` | `eth_bot_*.json` | storico, snapshot live, stato persistente |

Limiti Bybit su ETHUSDT (verificati 30/09/2026): leva max 150x, qty minima 0,01 ETH, passo 0,01 ETH, ordine minimo 5 USDT.

---

## 5. Test
`python -m pytest`:
- `tests/test_position_book.py`: accumulo e prezzo medio, riduzione con PnL realizzato, chiusura esatta, cambio di segno con fee pro-quota.
- `tests/test_breakeven_reversal.py`: breakeven = prezzo medio senza fee, soglie esatte ±0,5%, regola simmetrica qualunque sia il lato della posizione, direzione invariata tra le soglie, quantità minima ETH e mai sotto il valore minimo d'ordine.
- `tests/test_bot_flow.py`: orchestratore con exchange finto e orologio simulato — primo ordine all'avvio, un ordine ogni 60 s dall'ordine precedente, sizing al minimo exchange, inversione, cambio di segno e chiusura episodio, ritorno a short, riallineamento dopo chiusura manuale, ripresa dopo riavvio.
- `tests/test_funding_sign.py`: conversione del segno del funding con i valori reali Demo.

---

## 6. Punti aperti
1. **Regola del take profit**: da definire.
2. **Nessun limite di esposizione**: la posizione cresce di un ordine al minuto finché il prezzo non va in profitto oltre lo 0,5%; con prezzo contro, margine e rischio crescono senza tetto. Da decidere un limite (qty massima, margine massimo o stop).
