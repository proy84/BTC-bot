# Documentazione tecnica — Only-Short Grid Bot (ETH/USDT Perpetual, Bybit)

> Generata per consumo da parte di un altro assistente AI senza accesso diretto al codice.
> **Questa versione aggiorna una documentazione precedente di qualche giorno fa** — la sezione 7 elenca in dettaglio, con date, tutto ciò che è cambiato da allora (fix di race condition/crash, riscrittura del funding tracking, introduzione dell'equity-based sizing).
> Versione bot al momento della generazione: **v1.2** (log `[INFO] Avvio Bot Trading - v1.2`).
> Riferimenti a file/righe basati sullo stato del repository al commit `a4c8eae` (2026-08-27).

---

## 1. Struttura del progetto

| File | Responsabilità | Stato rispetto a una settimana fa |
|---|---|---|
| `main.py` | Orchestratore. Classe `GridBotOrchestrator`: cicli di vita del bot, due loop asincroni (scheduler griglia + tick loop), esecuzione ordini, trailing stop / take profit, RSI tick mode, Neutral Zone, chiusura/riapertura ciclo, export stato, stop-file, **sizing equity-based**, **funding realizzato**. | **Modificato pesantemente**: 6 fix/feature aggiunti in questi giorni (vedi §7). |
| `strategy.py` | Logica pura, **senza I/O di rete**: `StrategyConfig`, `RangeGrid` (griglia geometrica statica + re-indexing), `PositionManager`, `fibonacci()`, `compute_rsi()`, `evaluate_grid_close()`, `PlannedOrder`. | **Modificato**: `fibonacci()` ora iterativa (non più ricorsiva), nuovi campi config (`equity_based_sizing_*`, `stress_test_rsi_enabled`). Griglia/Break-Even/re-indexing/sizing Fibonacci **invariati** nella logica pura. |
| `exchange.py` | Wrapper async su CCXT/Bybit V5. Classe `ExchangeClient`. | **Modificato pesantemente**: nuovo `fetch_realized_funding()` (sostituisce `fetch_current_funding_rate()`, rimosso), nuovo `fetch_total_equity()`, `min_order_qty()` ora difensiva. |
| `fees.py` | Matematica fee/funding/PnL. | **Modificato**: `FundingPayment` semplificata (contiene il cashflow reale direttamente, non più derivato da `rate*notional`). Formule di Break-Even/PnL **invariate**. |
| `analytics.py` | Persistenza cicli chiusi su `trade_history.json` e statistiche aggregate. | Invariato. |
| `data_exporter.py` | Scrive `bot_state.json` (stato live). | Invariato. |
| `config.json` | Configurazione non sensibile. | **Modificato**: nuova sezione `equity_based_sizing`, nuovo campo `stress_test.rsi_enabled`. |
| `.env` / `.env.example` | Credenziali e override sensibili. | Invariato. |
| `requirements.txt`, `.gitignore` | Dipendenze (`ccxt`, `python-dotenv`), esclusioni git. | Invariato. |
| `bot_state.json`, `trade_history.json` | Output runtime, non tracciati su git. | Invariato (rimossi dal tracking qualche giorno fa). |
| `state.json` | File residuo non referenziato da nessun modulo. | Invariato, ancora presente, ancora dead file. |

Non esiste alcuna test suite nel repository.

---

## 2. Architettura e flusso di esecuzione

**Entry point e struttura generale invariati**: `python main.py` → `main()` → `asyncio.run(_run())` → `GridBotOrchestrator(cfg).start()` → `asyncio.gather(_grid_scheduler_loop(), _tick_loop())`.

**Cosa è cambiato nel flusso:**

1. **Entrambi i loop principali ora girano sotto try/except per-iterazione** (prima solo il tick loop ce l'aveva). Un'eccezione imprevista in QUALSIASI punto di `_grid_scheduler_loop` (main.py:381-423) viene loggata e il loop **continua al giro successivo** invece di propagarsi fino ad `asyncio.gather` e terminare l'intero processo (che avrebbe ucciso anche il tick loop insieme). Prima di questo fix, un errore lì dentro causava un crash silenzioso totale — confermato dal vivo (vedi §7).

2. **Apertura di un ciclo (bootstrap E riapertura dopo chiusura) ora include un passo di sizing basato sull'equity reale**, prima di calcolare/piazzare l'ordine immediato:
   ```
   fetch_mark_price() -> _maybe_update_base_notional_from_equity() -> _effective_base_notional(price) -> ordine immediato
   ```
   Questo passo è nuovo rispetto a una settimana fa (vedi §3 "SIZING").

3. **Chiusura di un ciclo (`_close_cycle`, main.py:833-913) ora è resiliente a un fallimento della registrazione statistiche**: se `analytics.record_cycle()` fallisce (es. errore disco), il reset dello stato locale (posizione, griglia, cycle_id) avviene **comunque**, perché a quel punto la chiusura sull'exchange è già avvenuta con successo — altrimenti il bot resterebbe convinto di avere ancora la vecchia posizione aperta mentre l'exchange è già flat. Prima di questo fix, un fallimento lì avrebbe lasciato lo stato locale disallineato dall'exchange.

4. **Il tracking del funding durante la vita del ciclo è stato riscritto interamente** (vedi §3 "FUNDING") — cambia COSA succede ogni `funding_poll_interval_sec` (300s) mentre il ciclo è aperto, non la struttura dei due loop.

5. **Riavvio del countdown orario sul nuovo ciclo** (introdotto qualche giorno fa, confermato ancora attivo e invariato): quando un ciclo si chiude e se ne riapre uno nuovo, lo scheduler della griglia interrompe il countdown in corso e ne inizia uno pieno da quell'istante (`_cadence_reset_event`, main.py:381-410) — il secondo ordine di ogni ciclo cade sempre esattamente `stress_test_base_interval_sec` dopo il primo ordine immediato di quel ciclo, non secondo un vecchio schema indipendente.

Il resto dell'architettura (due loop indipendenti, lock di posizione, static grid, RSI/tick mode, Neutral Zone) è strutturalmente **invariato** — cambia solo COSA succede dentro ai singoli passaggi, non la forma generale.

---

## 3. Logica della strategia (dettaglio completo, con enfasi sui cambiamenti)

### 3.1 Griglia, Break-Even, re-indexing, sizing Fibonacci — **INVARIATI nella logica pura**

Confermato dalla rilettura completa di `strategy.py`: la formula geometrica della griglia (`AbsoluteLevel_N = base_price * (1+step_pct)^N`), l'ancoraggio statico per ciclo (`RangeGrid.full_reset`), il re-indexing di Range 0 sul Break-Even (`RangeGrid.reindex_to_breakeven`), e la progressione Fibonacci (`Fibonacci(|offset|+1)`) sono **esattamente gli stessi** di una settimana fa. Il calcolo del Break-Even (`fees.compute_breakeven_prices`) è anch'esso invariato nella formula.

**L'unico cambiamento tecnico in quest'area**: `strategy.fibonacci()` (strategy.py:83-95) è passata da un'implementazione **ricorsiva** a una **iterativa**, con **valori di output identici** per ogni n — cambia solo l'implementazione interna (evita un `RecursionError` con offset molto grandi, dato che `unlimited_fib_level=true` non pone alcun tetto a n). Vedi §7 per i dettagli del bug che questo risolve.

### 3.2 SIZING — nuovo meccanismo "Equity-based sizing" (introdotto 2026-08-26)

**Cosa fa**: ad ogni apertura di ciclo (bootstrap iniziale del bot E ogni riapertura immediata dopo una chiusura), il bot rilegge l'**equity totale reale** dell'account e imposta `base_notional_usdt` a una percentuale configurabile di quel valore.

**Meccanismo esatto**:
1. `ExchangeClient.fetch_total_equity()` (exchange.py:254-270) chiama `fetch_balance()` e legge il campo **`info.result.list[0].totalEquity`** di Bybit V5 — NON il saldo USDT unificato di CCXT (`balance['USDT']['total']`), che riflette solo il sotto-conto USDT e sottostima gravemente l'equity reale su un account con collaterale multi-coin (verificato empiricamente: ~16 USDT contro ~2992 USDT reali sullo stesso account demo).
2. `GridBotOrchestrator._maybe_update_base_notional_from_equity()` (main.py:298-331) chiama questo metodo e imposta:
   ```
   self.base_notional_usdt = equity_totale * equity_based_sizing_percentage / 100.0
   ```
3. Log di esempio osservato dal vivo: `"Equity-based sizing: equity totale=2994.31 USDT -> base_notional_usdt 1.00 -> 29.94 (1.00% dell'equity)."`
4. Chiamato da due punti (main.py:271, main.py:292): dentro `_bootstrap_position()` (ramo posizione esistente E ramo flat) e dentro `_execute_immediate_base_order()` (quindi copre sia l'avvio del bot sia ogni riapertura ciclo dopo take profit/trailing stop).
5. **Su qualunque fallimento** (rete, forma di risposta inattesa, equity ≤ 0), `base_notional_usdt` resta **invariato** al valore precedente — nessun blocco, nessun crash.

**Sostituisce o convive con l'auto-compound?** **Lo sostituisce completamente quando attivo.** Config `equity_based_sizing.enabled=true` (default attuale) disattiva l'auto-compound legacy: `_reset_state_after_close()` (main.py:915-936) salta esplicitamente la moltiplicazione per compound quando `equity_based_sizing_enabled=True` (`if not self.cfg.equity_based_sizing_enabled and self.cfg.auto_compound_enabled:`). L'auto-compound resta nel codice/config come **fallback disattivabile** (basta impostare `equity_based_sizing.enabled=false` per tornare al vecchio comportamento).

**Differenza concettuale con l'auto-compound legacy**: l'auto-compound cresceva di una % fissa **ad ogni ciclo chiuso**, indipendentemente dalla performance reale del conto (anche in perdita). L'equity-based sizing è **auto-correttivo**: cresce solo se il conto è realmente cresciuto, e si riduce da solo dopo un drawdown.

**Interazione con il floor di sicurezza esistente**: `_effective_base_notional(price)` (main.py:333-347, **invariato** nella logica, preesistente da prima) continua ad applicarsi SOPRA il valore equity-based: `max(base_notional_usdt_equity_based, minimo_tradabile_exchange)`. Se l'1% dell'equity risultasse sotto il minimo Bybit in quel momento, la sequenza Fibonacci di quel ciclo parte comunque dal minimo, non da un valore troppo piccolo (verificato con test funzionale).

**Config esatta**:
```json
"equity_based_sizing": { "enabled": true, "percentage": 1.0 }
```

### 3.3 FUNDING — riscritto interamente (fix critico, 2026-08-27)

**Come funzionava PRIMA** (bug): `_maybe_poll_funding()` chiamava `fetch_current_funding_rate()` (il tasso di funding **stimato/corrente**, non un pagamento reale) ogni `funding_poll_interval_sec` (300s = 5 minuti) e registrava **ogni singola lettura come un nuovo pagamento distinto**, sommandolo al cashflow cumulato — anche se Bybit liquida il funding reale solo ogni **8 ore** (00:00/08:00/16:00 UTC). Per una posizione tenuta ~73 minuti questo fabbricava ~16 pagamenti fantasma, gonfiando il Break-Even netto abbastanza da far scattare il take profit fisso ~12 USDT troppo presto (confermato dal vivo con dati reali — vedi §7).

**Come funziona ORA**:
1. `ExchangeClient.fetch_realized_funding(since_ms, limit=50)` (exchange.py:212-230) chiama l'endpoint **`fetchFundingHistory`** di CCXT (Bybit), che restituisce le liquidazioni **realmente avvenute e già accreditate/addebitate** sul conto, ciascuna con `id`, `timestamp`, `amount` (il cashflow reale, positivo=ricevuto, negativo=pagato) — confermato empiricamente: i timestamp cadono esattamente su 00:00:00/08:00:00/16:00:00 UTC.
2. `GridBotOrchestrator._maybe_poll_funding()` (main.py:799-829) interroga questo endpoint invece di stimare dal tasso corrente. Per ogni liquidazione **non ancora vista** (deduplicata tramite `self._recorded_funding_ids`, un `set` di id), la registra in `fee_engine` con `FundingPayment(timestamp_ms, cashflow_usdt)` — il cashflow reale, non più derivato da `rate*notional`.
3. **Deduplica by-id**: dato che la query ripete la stessa finestra temporale ad ogni poll, la stessa liquidazione reale può ricomparire nel risultato più volte prima di uscire dalla finestra interrogata — `self._recorded_funding_ids` garantisce che venga sommata **esattamente una volta**, verificato con test funzionale (4 poll consecutivi, stessa liquidazione vista 3 volte, sommata 1 sola volta).
4. **Gestione paginazione per cicli lunghi** (fix del 2026-08-27, stesso giorno, commit successivo): invece di interrogare sempre `since=inizio_ciclo` (che con `limit=50` avrebbe troncato/perso liquidazioni per un ciclo aperto più di ~16 giorni, a 3 liquidazioni/giorno), la finestra di lettura (`self._funding_since_ms`) **avanza progressivamente** oltre l'ultima liquidazione processata ad ogni poll, così un ciclo arbitrariamente lungo non richiede mai più di poche liquidazioni nuove per query.
5. **Nessun fallback silenzioso**: se non ci sono liquidazioni nella finestra interrogata (caso comune per un ciclo breve, es. meno di 8 ore), il cashflow di funding resta **esplicitamente 0.0** — non esiste più, in nessun punto del codice, un calcolo `rate*notional` come ripiego.
6. Reset ad ogni nuovo ciclo: `self._recorded_funding_ids` e `self._funding_since_ms` vengono azzerati in `_reset_state_after_close()` insieme a `fee_engine.reset()`.

`fetch_current_funding_rate()` (il vecchio metodo) è stato **rimosso completamente** da `exchange.py` — non più referenziato da nessuna parte del codice.

### 3.4 Take profit / trailing stop — **logica invariata**

La formula e il comportamento di `_maybe_handle_fixed_take_profit()` e `_maybe_handle_trailing_stop()` (main.py:615-683) sono **identici** a una settimana fa — nessun fix li ha toccati direttamente. L'unico motivo per cui il take profit scattava in modo anomalo era l'input errato (il funding fittizio descritto sopra), non la formula stessa — verificato matematicamente durante l'audit: con lo stesso codice di calcolo ma funding reale, la soglia risulta corretta.

Config attuale: `trailing_stop.enabled=false` (take profit fisso attivo), `activation_pct=0.5`.

---

## 4. Nomi esatti da codice (aggiornato — nuovi/modificati/rimossi)

| Concetto | Nome esatto | File:riga | Stato |
|---|---|---|---|
| Sizing basato su equity | `GridBotOrchestrator._maybe_update_base_notional_from_equity()` | main.py:298 | **NUOVO** |
| Lettura equity reale | `ExchangeClient.fetch_total_equity()` | exchange.py:254 | **NUOVO** |
| Config: attiva equity-based sizing | `StrategyConfig.equity_based_sizing_enabled` ← `config.json: equity_based_sizing.enabled` | strategy.py:117 | **NUOVO** |
| Config: percentuale equity-based | `StrategyConfig.equity_based_sizing_percentage` ← `config.json: equity_based_sizing.percentage` | strategy.py:118 | **NUOVO** |
| Floor su minimo exchange (base per Fibonacci) | `GridBotOrchestrator._effective_base_notional()` | main.py:333 | Invariato (preesistente) |
| Lettura funding REALE | `ExchangeClient.fetch_realized_funding()` | exchange.py:212 | **NUOVO** (sostituisce `fetch_current_funding_rate`) |
| ~~Lettura tasso funding stimato~~ | ~~`ExchangeClient.fetch_current_funding_rate()`~~ | — | **RIMOSSO** |
| Polling funding (ora legge dati reali) | `GridBotOrchestrator._maybe_poll_funding()` | main.py:799 | **Riscritto** (stessa firma, logica interna nuova) |
| Dedup liquidazioni funding | `GridBotOrchestrator._recorded_funding_ids` (set) | main.py:179 | **NUOVO** |
| Finestra di lettura funding avanzante | `GridBotOrchestrator._funding_since_ms` | main.py:189 | **NUOVO** |
| Record di un pagamento funding | `fees.FundingPayment` (ora: `timestamp_ms`, `cashflow_usdt` diretto) | fees.py:32 | **Modificato** (prima: `funding_rate`+`notional_usdt` derivava `cashflow_usdt` via property) |
| Sequenza Fibonacci | `strategy.fibonacci()` (ora iterativa) | strategy.py:84 | **Modificato** (stessa firma/output, implementazione interna) |
| Config: RSI/tick mode on-off | `StrategyConfig.stress_test_rsi_enabled` ← `config.json: stress_test.rsi_enabled` | strategy.py:132 | **NUOVO** |
| Protezione scheduler da crash | try/except in `GridBotOrchestrator._grid_scheduler_loop()` | main.py:390-423 | **NUOVO** (mancava, causa di un crash silenzioso confermato) |
| Protezione desync su fallimento analytics | try/except in `GridBotOrchestrator._close_cycle()` | main.py:870-902 | **NUOVO** |
| Riavvio countdown su nuovo ciclo | `GridBotOrchestrator._cadence_reset_event` | main.py:189 | Invariato (introdotto qualche giorno fa) |
| Minimo qty tradabile (ora difensivo) | `ExchangeClient.min_order_qty()` | exchange.py:236 | **Modificato** (aggiunto try/except, ritorna 0.0 su fallimento invece di propagare) |
| Griglia | `class RangeGrid` | strategy.py:198 | Invariato |
| Sizing ordine (pura funzione) | `evaluate_grid_close()` | strategy.py:301 | Invariato |
| Break-Even lordo/netto | `compute_breakeven_prices()` | fees.py:115 | Invariato (formula) |
| Classe trailing legacy (NON usata) | `TrailingStopController` | strategy.py:390 | Invariato, ancora dead code |

---

## 5. Gestione API ed esecuzione ordini — aggiornamenti

Libreria (`ccxt.async_support`), split pubblico/privato, ordini solo market, retry con backoff: **tutto invariato**. Nuove chiamate API aggiunte:

- **`fetchBalance`** (via `fetch_total_equity()`): usata per il sizing equity-based, letta ad ogni apertura di ciclo (non ad ogni tick). Estrae `info.result.list[0].totalEquity` dalla risposta raw di Bybit V5 Unified Account.
- **`fetchFundingHistory`** (via `fetch_realized_funding()`): sostituisce `fetchFundingRate` per il tracking del funding. Chiamata al massimo ogni `funding_poll_interval_sec` (300s), con `since` che avanza progressivamente e `limit=50` di default.

Entrambe passano attraverso lo stesso wrapper `_retry()` (retry su `NetworkError`, propagazione immediata su `ExchangeError`) di tutte le altre chiamate.

---

## 6. Configurazione — campi nuovi/modificati

| Chiave | Valore attuale | Stato |
|---|---|---|
| `base_notional_usdt` | `1.0` | Ora è solo il **seed iniziale** (usato prima della prima lettura equity, o se una lettura fallisce) quando `equity_based_sizing.enabled=true` — non più il valore che l'auto-compound moltiplica direttamente in quel caso |
| `auto_compound.enabled` / `.percentage` | `true` / `0.5` | **Ignorato interamente** mentre `equity_based_sizing.enabled=true` (kept come fallback) |
| `equity_based_sizing.enabled` | `true` | **NUOVO** |
| `equity_based_sizing.percentage` | `1.0` | **NUOVO** — % dell'equity totale reale usata come `base_notional_usdt` ad ogni apertura ciclo |
| `stress_test.rsi_enabled` | `false` | **NUOVO** — disattiva RSI/tick-mode lasciando invariato il resto della modalità stress test (cadenza base, Neutral Zone, unlimited_fib_level) |
| `stress_test.base_interval_sec` | `3600` | Invariato (1 ora) |
| `trailing_stop.enabled` / `.activation_pct` | `false` / `0.5` | Invariato |

Tutti gli altri parametri (grid_step_pct, leverage, fees, polling, risk.max_fib_level, ecc.) **invariati**.

---

## 7. Bug noti e risolti — CHANGELOG ESPLICITO

| Data | Commit | Bug | Impatto | Fix |
|---|---|---|---|---|
| **2026-08-24 19:17** | `ba66e11` | **Crash silenzioso dello scheduler**: `_grid_scheduler_loop` non aveva protezione da eccezioni (a differenza del tick loop) — un errore imprevisto al suo interno risaliva fino ad `asyncio.gather`, che cancellava anche il tick loop, terminando l'intero processo senza traccia visibile se non riaprendo la sessione. | Confermato dal vivo: bot fermo per ore, nessun nuovo ordine, nessun controllo take profit — sessione tmux sparita perché avviata con `tmux new-session -d -s bot 'python main.py'` (che chiude la sessione quando il comando termina). | Try/except per-iterazione attorno a tutto il corpo del loop; un errore viene ora loggato e il bot riprova al giro successivo. |
| **2026-08-24 19:39** | `7a2b3b6` | **Desync di stato su fallimento della registrazione statistiche**: se `analytics.record_cycle()` falliva (es. errore I/O) DOPO che la chiusura sull'exchange era già riuscita, `_reset_state_after_close()` non veniva mai eseguito — il bot restava convinto di avere ancora la vecchia posizione aperta mentre l'exchange era già flat. | Non osservato in produzione, trovato per ispezione del codice durante un audit richiesto esplicitamente. | Isolata la parte rischiosa in un try/except separato; il reset dello stato locale ora avviene sempre, indipendentemente dal successo della registrazione statistiche. Verificato con test funzionale (fallimento disco simulato). |
| **2026-08-24 19:39** | `7a2b3b6` | **`fibonacci()` ricorsiva senza limite**: con `unlimited_fib_level=true`, un offset molto grande avrebbe potuto superare il limite di ricorsione di Python (1000) e crashare. | Non osservato in produzione (richiede un offset estremo), trovato per ispezione. | Convertita in versione iterativa, stessi valori di output, nessun limite di profondità. Verificato: nessun crash fino a n=2000, valori piccoli identici a prima. |
| **2026-08-26 15:16** | `05a6dd4` | **Floor sulla quantità minima non raggiungibile per notional molto piccoli**: `compute_qty_from_notional()` lanciava `InvalidOrder` (via CCXT) quando notional/prezzo arrotondava a MENO di uno step di precisione intero, PRIMA che il floor già esistente potesse intervenire. | Riprodotto deliberatamente impostando `base_notional_usdt=1` per testare l'auto-adattamento al minimo exchange. | L'eccezione viene ora intercettata e trattata come qty=0, permettendo al floor esistente di alzarla comunque al minimo tradabile. |
| **2026-08-26 15:21** | `b5aabdd` | **La progressione Fibonacci collassava su ordini duplicati quando il notional base era sotto il minimo exchange**: ogni fib_n troppo piccolo si "schiacciava" indipendentemente sullo stesso floor, producendo molti ordini identici invece di una vera progressione. | Osservato durante il test del punto precedente. | Nuovo `_effective_base_notional()`: l'intera sequenza Fibonacci del ciclo si sviluppa a partire dal maggiore tra `base_notional_usdt` e il minimo exchange, non più con il floor applicato ordine per ordine in modo indipendente. |
| **2026-08-27 12:24** | `3939885` | **BUG CRITICO — funding fittizio causava take profit prematuro**: `_maybe_poll_funding` interrogava il tasso di funding stimato ogni 5 minuti e registrava OGNI lettura come un pagamento reale distinto, sommandola al cashflow cumulato — ma Bybit liquida realmente solo ogni 8h. Per una posizione tenuta ~73 minuti questo ha fabbricato 16 pagamenti fantasma (+1.5 USDT mai incassati), gonfiando il Break-Even netto e facendo scattare il take profit ~12 USDT troppo presto. | **Confermato dal vivo** con dati reali Bybit: soglia calcolata dal bot 2485.4771 vs 2473.34 con funding reale (differenza 12.14 USDT); posizione chiusa in sostanziale pareggio (0.0089% di profitto) invece dello 0.5% configurato. **Audit retroattivo su 13 cicli storici**: funding fittizio totale fabbricato 18.57 USDT contro -0.88 USDT realmente dovuti (distorsione cumulata 19.46 USDT); un ciclo da 140.44 USDT di profitto riportato risultava in realtà 124.38 USDT (sovrastima dell'11.4%), un altro aveva perfino il SEGNO del funding invertito. | Riscritto per leggere le liquidazioni REALMENTE avvenute dall'exchange (`fetchFundingHistory`) con deduplica by-id, invece di stimarle da un tasso interrogato ripetutamente. Vedi §3.3 per il meccanismo completo. |
| **2026-08-27 12:37** | `a4c8eae` | **Rischio di troncamento del funding per cicli molto lunghi**: il fix precedente interrogava sempre `since=inizio_ciclo` con `limit=50` — un ciclo aperto oltre ~16 giorni (Bybit liquida 3x/giorno) avrebbe superato il limite pagina e perso silenziosamente liquidazioni reali più vecchie. | Non osservato in produzione (richiede un ciclo aperto per settimane), trovato per ispezione durante l'audit generale. | La finestra di lettura ora avanza progressivamente oltre l'ultima liquidazione processata, invece di ripartire sempre dall'inizio del ciclo. Verificato con test funzionale multi-poll. |
| **2026-08-27 12:37** | `a4c8eae` | **`min_order_qty()` priva di gestione eccezioni**: unica chiamata exchange nel codice senza protezione — un suo fallimento durante il bootstrap avrebbe crashato l'intero processo prima ancora dell'avvio dei loop principali. | Non osservato in produzione, trovato per ispezione. | Aggiunta protezione try/except; ritorna 0.0 ("nessun minimo") su qualunque fallimento invece di propagare. |

**Trovate durante l'audit generale ma NON risolte (limitazioni note, bassa priorità):**
- Posizione riconciliata dopo un riavvio (`_bootstrap_position`) registra fee di apertura a 0 (Bybit non restituisce lo storico fee di una posizione preesistente) — sottostima leggermente le fee reali, bias simile ma molto più piccolo di quello del funding. Richiederebbe recuperare lo storico esecuzioni reale dall'exchange per essere risolto del tutto.
- `trade_history.json` corrotto o di schema incompatibile crasherebbe l'avvio (nessuna protezione in `analytics.py`).
- Edge case rarissimo: un `create_order` che ha successo ma la cui risposta non contiene un order id potrebbe aggiungere una entry a qty/prezzo zero (già loggato come ERROR, matematicamente innocuo per il Break-Even).
- Gap pre-esistente, ancora non affrontato: la griglia si ri-ancora al riavvio se trova una posizione già aperta, invece di preservare l'ancora del ciclo precedente (deliberatamente rimandato dall'utente in una sessione precedente).

---

## 8. Stato attuale

Tutti i fix elencati in §7 sono stati verificati con test funzionali mirati (non solo controllo sintattico) prima del commit — in particolare il fix del funding è stato verificato anche retroattivamente contro lo storico reale di liquidazioni Bybit per confermare l'entità della distorsione su cicli già chiusi. Nessuna test suite automatizzata esiste nel repository; la verifica avviene tramite script Python ad-hoc eseguiti durante lo sviluppo, non conservati nel codebase.
