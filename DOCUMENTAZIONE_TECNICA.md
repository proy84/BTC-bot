# Documentazione tecnica — BTC bot v2.0 (hedge a due gambe, Bybit V5)

BTC bot apre insieme **due posizioni perpetual opposte** su Bitcoin:

| Gamba | Simbolo (CCXT) | Lato | Moneta di regolamento | Coppia spot per comprare BTC |
|---|---|---|---|---|
| `short` | `BTC/USDT:USDT` | SHORT | USDT | `BTC/USDT` |
| `long` | `BTC/USDC:USDC` | LONG | USDC | `BTC/USDC` |

Leva **125x** su entrambe (massimo consentito su BTCPERP-USDC; BTCUSDT arriva a 150x), margine cross.
Lavora su **Bybit Demo Trading** (`USE_TESTNET=true` in `.env`, default).

> Il progetto deriva dal grid bot ETH (tag git `pre-btc-bot-v2` = stato di partenza) ma è un progetto separato:
> repository GitHub `proy84/BTC-bot` (remote git `btcbot`), branch `btc-bot-v2`.
> Il remote `origin` (eth_grid_bot) ha il push disattivato.

---

## 1. Struttura del progetto

| File | Ruolo |
|---|---|
| `main.py` | Orchestratore `BtcBot`: ciclo di tick, apertura gambe, TP, CLOSE ALL, regolamento, riapertura, stato persistente. |
| `strategy.py` | Config (`StrategyConfig`) e **logica pura** senza rete: TP netto su due gambe, moltiplicatori cumulativi, decisione dopo la chiusura, sizing, piano di regolamento spot. |
| `fees.py` | Matematica pura: PnL long/short, fee, netto di una gamba con i costi di entrambe (`two_leg_net`), netto realizzato per gamba. |
| `exchange.py` | Gateway CCXT a Bybit V5: due istanze (pubblica produzione per i prezzi, privata Demo per ordini/conto), ordini perpetual e spot con dedup per `clientOrderId`, funding realizzato, saldi/debiti per moneta. |
| `analytics.py` | Storico dei cicli chiusi (`btc_bot_history.json`) e statistiche. |
| `data_exporter.py` | Snapshot live per dashboard (`btc_bot_live.json`). |
| `notifier.py` | Notifiche Telegram opzionali e isolate (avvio, ciclo chiuso, stop). |
| `config.json` | Parametri della strategia (nessun segreto). |
| `.env` | **Solo locale, mai committato**: `BYBIT_API_KEY`, `BYBIT_API_SECRET`, `USE_TESTNET`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`. Modello: `.env.example`. |
| `tests/` | Test pytest (TP netto, moltiplicatori, decisione, flusso completo con exchange finto). |
| `tools/demo_spot_repay_test.py` | Prova manuale su Demo: verifica che un accredito spot azzeri un debito USDC. |

File di runtime (ignorati da git): `btc_bot_state.json`, `btc_bot_history.json`, `btc_bot_live.json`, `STOP`.

Avvio: `python main.py` · Test: `python -m pytest` · Log dettagliati: `LOG_LEVEL=DEBUG python main.py` · Arresto pulito: creare un file `STOP` nella cartella (le posizioni restano aperte, lo stato è salvato).

---

## 2. Vocabolario

- **Ciclo**: le due gambe aperte → una gamba fa TP → **CLOSE ALL** (chiusura di entrambe).
- **Sequenza**: i cicli dall'ultimo reset al base notional fino al ciclo che chiude la sequenza in guadagno netto.
  Per ogni sequenza si accumula il **netto realizzato per moneta**: USDT (gamba short) e USDC (gamba long).
- **Passo** (`step`): quante riaperture con moltiplicatori sono state fatte nella sequenza (0 = base).

---

## 3. Strategia

### 3.1 Apertura
All'avvio (e subito dopo ogni CLOSE ALL) apre a mercato `short` e `long` al **notional target** della sequenza.
- **Base notional** (per gamba, uguale sulle due) = `equity_based_sizing.percentage`% dell'equity totale reale (`totalEquity` Bybit, tutte le monete di collaterale), alzato al minimo exchange (`0.001 BTC × prezzo`, oggi ~84 $). Con ~3.330 $ di equity l'1% (33 $) è sotto il minimo: la base diventa 0,001 BTC.
- Quantità = `qty_for_notional`: notional/prezzo arrotondato al passo exchange **al più vicino** (0,0015 → 0,002), mai sotto il minimo. I moltiplicatori si applicano al **notional target**, mai alla quantità arrotondata, quindi la sequenza resta esatta.
- Una gamba che non si apre viene ritentata dopo 30 s; il TP si valuta solo con entrambe aperte.

### 3.2 Take profit netto su due gambe (punto chiave)
Per ciascuna gamba `X`, a ogni tick (`fees.two_leg_net`, `strategy.check_take_profit`):

```
netto_X = PnL_lordo_X
        − fee apertura (short + long)
        − fee chiusura stimate ai prezzi correnti (short + long)
        + funding realizzato (short + long)

TP su X  ⇔  netto_X ≥ take_profit_net_pct% × notional_X      (default 0,20%)
```

La gamba che fa TP paga **l'intero giro** di entrambe le posizioni, non solo le proprie fee. USDT e USDC sono sommati 1:1.
Esempio (entrambe 0,001 BTC a 100.000, taker 0,055%): la short fa TP solo con prezzo ≤ 99.580,46; a 99.650 con le sole proprie fee sarebbe già a +0,24%, ma con i costi di entrambe no (test `test_own_fees_only_would_trigger_but_two_leg_net_does_not`).
Fee reali: quelle riportate da Bybit nel fill; se assenti, stima `taker_rate × notional`.

### 3.3 CLOSE ALL e netto del ciclo
Al TP il bot chiude **entrambe** le gambe a mercato (`reduceOnly`). Se una chiusura fallisce, viene ritentata al tick successivo; il regolamento parte solo quando entrambe sono chiuse.
Netto realizzato per gamba = lordo − fee apertura − fee chiusura reale + funding, nella propria moneta. Si somma al netto della sequenza.

### 3.4 Decisione dopo il CLOSE ALL (`strategy.decide_after_close`)
- **a) Sequenza in guadagno netto** (netto USDT + netto USDC della sequenza **> 0**) → `new_sequence`:
  la moneta con netto maggiore è la vincente; copre la perdita dell'altra (repay) e con il resto compra BTC spot; poi nuova sequenza al base notional (ricalcolato dall'equity), senza moltiplicatori.
- **b) Altrimenti** → `multiply`: moltiplicatori **cumulativi** per gamba — gamba che ha fatto TP = suo importo precedente × `multipliers.winner` (2,0), l'altra = suo importo precedente × `multipliers.loser` (1,5). Sempre.
- **Tetto** `max_multiplier_steps`: `null` = nessun limite (default, fase di stress test). Se impostato a N e la riapertura supererebbe N passi → `stop`: posizioni chiuse, nessuna riapertura, notifica, il bot si ferma.

Sequenza di riferimento (base 100, test `test_reference_sequence`):
`100S/100L → TP long → 150S/200L → TP short → 300S/300L → TP long → 450S/600L → TP short → 900S/900L → TP short → 1800S/1350L → TP short con guadagno netto → repay + acquisto BTC spot → 100S/100L`.

### 3.5 Repay e acquisto BTC spot (`strategy.plan_spot_settlement`, `BtcBot._spot_settlement`)
**Bybit Demo non supporta il repay**: verificato il 2026-09-28 sul conto Demo,
`/v5/account/repay` e `/v5/account/no-convert-repay` → `retCode 10032 "Demo trading are not supported"`,
`/v5/account/quick-repayment` → `10016`. Coerente con l'elenco ufficiale degli endpoint Demo.

Soluzione adottata (**repay tramite spot**): la moneta vincente **accredita** la moneta perdente con un ordine spot su `settlement.stable_conversion_symbol` (`USDC/USDT`, supportato su Demo). Nel conto Unified un accredito su una moneta con saldo negativo compensa il debito (da confermare con `tools/demo_spot_repay_test.py`).
- vincente USDT, perdente USDC → market **buy** `USDC/USDT` spendendo `importo` USDT;
- vincente USDC, perdente USDT → market **sell** `importo` USDC su `USDC/USDT`.
- Dopo la conversione il bot rilegge il debito: se resta, in **produzione** chiama `/v5/account/repay`; su Demo lo segnala nel log/notifica.
- Col resto: market buy BTC spot sulla coppia della moneta vincente (`BTC/USDT` o `BTC/USDC`).
- Minimo ordine spot Bybit = **5** (USDT/USDC): un repay più piccolo viene alzato a 5,10 (l'eccesso resta sul saldo della moneta perdente); un acquisto BTC sotto 5 viene saltato (il resto resta nella moneta vincente); se il profitto vincente è sotto il minimo, non si fa nessun ordine spot. Non si spende mai più del profitto della moneta vincente.
- Ogni operazione spot è best-effort: un errore viene registrato ma non blocca il ciclo successivo.

### 3.6 Funding
Funding **realizzato** letto per simbolo dal registro di Bybit (`fetch_funding_history` → `/v5/execution/list`, supportato su Demo), ogni `funding_poll_interval_sec`, deduplicato per id di settlement, con finestra temporale che avanza. Entra nel TP (entrambe le gambe) e nel netto realizzato.

---

## 4. Stato persistente e riavvio
`btc_bot_state.json` (`SequenceState`): `sequence_id`, `step`, `base_notional`, `notionals` per gamba, `net_by_coin` della sequenza, `cycle_id`. Salvato dopo ogni regolamento.
Al riavvio:
- stato presente → si riprende la sequenza al passo salvato;
- posizioni aperte sui due simboli → riconciliate come gambe correnti (fee di apertura stimata); una gamba mancante viene aperta al target;
- posizione con il **lato sbagliato** su un simbolo (es. LONG su BTC/USDT) → il bot **non parte** e chiede di chiuderla a mano.

---

## 5. Esecuzione ordini e API
- Due istanze CCXT: `_public` (produzione, non autenticata: prezzi, metadati mercati) e `_private` (autenticata, host `https://api-demo.bybit.com` con `USE_TESTNET=true`). Il catalogo mercati del client privato è copiato dal pubblico (`load_markets()` sul Demo fallisce con 10032).
- Ogni ordine passa da `_create_order_with_dedup`: un `clientOrderId` per ordine logico; su errore di rete **prima verifica** su Bybit se l'ordine esiste, poi eventualmente reinvia. Fill letti con `fetch_order` (prezzo medio, quantità, fee).
- `_retry`: backoff esponenziale sugli errori di rete; i rifiuti dell'exchange non vengono ritentati.
- Tutte le modifiche di posizione avvengono sotto `_position_lock`.

---

## 6. Configurazione (`config.json`)

| Campo | Default | Significato |
|---|---|---|
| `legs.short/long` | vedi tabella iniziale | simbolo, lato, moneta di regolamento, coppia spot BTC |
| `leverage` | 125 | leva su entrambe le gambe |
| `margin_mode` | `cross` | |
| `take_profit_net_pct` | 0.20 | soglia TP netto su due gambe, % del notional della gamba |
| `fees.taker_rate` / `maker_rate` | 0.00055 / 0.0002 | per stime (le fee reali vengono dai fill) |
| `equity_based_sizing.enabled` / `percentage` | true / 1.0 | base notional = % dell'equity totale |
| `base_notional_usd` | 1.0 | ripiego se la lettura equity fallisce |
| `multipliers.winner` / `loser` | 2.0 / 1.5 | moltiplicatori cumulativi |
| `max_multiplier_steps` | null | null = nessun limite; N = stop dopo N passi |
| `settlement.enabled` | true | repay via spot + acquisto BTC dopo una sequenza vincente |
| `settlement.stable_conversion_symbol` | `USDC/USDT` | coppia di conversione tra le due stable |
| `settlement.spot_min_order_value` | 5.0 | minimo ordine spot Bybit |
| `notifier.enabled` | true | Telegram (richiede le variabili in `.env`) |
| `polling.tick_poll_interval_sec` | 2 | frequenza del tick |
| `polling.funding_poll_interval_sec` | 300 | frequenza lettura funding |
| `paths.*` | `btc_bot_*.json` | storico, snapshot live, stato persistente |

---

## 7. Cosa è stato eliminato rispetto al grid bot ETH
RangeGrid, grid step e step table, grid reindex, range offsets, tick mode e RSI, Neutral Zone, stress test, trailing stop, sizing Fibonacci e `level_multiplier`, auto_compound, logica "only short", finestra ONE_ORDER, `eth_spot_accumulator.py`, `state.json`, lettura di `SYMBOL`/`GRID_STEP_PERCENT` da `.env`, e tutte le relative voci di config.
Mantenuti e adattati: retry con dedup per `clientOrderId`, lock delle posizioni, funding realizzato, equity reale, analytics, notifier Telegram, log ridotti (INFO solo per aperture, TP, chiusure, decisioni, riepilogo ogni 5 minuti).

---

## 8. Test
`python -m pytest` — 36 test:
- `tests/test_take_profit_net.py`: soglie esatte TP short/long, fee di entrambe le gambe, funding di entrambe, notional diversi per gamba.
- `tests/test_multipliers.py`: sequenza di riferimento, cumulatività, parametri, arrotondamento quantità.
- `tests/test_cycle_decision.py`: nuova sequenza vs moltiplicatori, zero non è guadagno, `max_multiplier_steps`, sequenza completa fino al reset, piano spot con minimi.
- `tests/test_bot_flow.py`: orchestratore con exchange finto (apertura → TP → CLOSE ALL → moltiplicatori → nuova sequenza), regolamento spot nei due versi, ripresa dopo riavvio, rifiuto posizione di lato sbagliato.

---

## 9. Punti aperti / da verificare su Demo
1. Lanciare `python tools/demo_spot_repay_test.py` per confermare che l'accredito spot azzeri il debito USDC (esiste già un debito USDC di ~0,017 sul conto Demo).
2. Primo avvio reale su Demo: verificare leva 125x su entrambi i simboli, fee riportate nei fill, funding USDC letto correttamente.
3. Rischio intrinseco della strategia: i moltiplicatori cumulativi crescono in modo geometrico (×2 / ×1,5 per passo) e senza `max_multiplier_steps` non c'è limite all'esposizione.
