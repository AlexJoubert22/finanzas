# PHASE-MAP: FASES 9 a 42 — Mapa Panorámico

> Documento de referencia que vive en `docs/PHASE-MAP.md` del repo. Claude Code lo lee al inicio de cada sesión para situarse en el viaje completo.
>
> **Detalle medio**: cada fase tiene objetivo, scope general, dependencias, y criterios de "fase cerrada". El detalle operativo (archivos, tests, commits) viene en el JSON específico de la fase, generado en sesión estratégica cuando toca.
>
> **Reglas de progresión**: una fase no se considera cerrada hasta que (a) los criterios de aceptación pasan, (b) PR mergeado a `main`, (c) ningún sub-commit pendiente. La siguiente fase arranca en sesión nueva con JSON nuevo.

---

## Cómo leer este mapa

| Campo | Significado |
|-------|-------------|
| **Prio** | CRÍTICA / ALTA / MEDIA / BAJA / OPCIONAL |
| **Bloqueada por** | Otras fases que deben cerrarse antes |
| **Salida esperada** | Qué deja en el repo cuando se cierra |
| **Riesgo principal** | El error más común en esta fase |
| **Capacidad** | Recursos adicionales que añade (RAM, calls/día, etc.) |

---

## FASE 9 — Order Executor

| Atributo | Detalle |
|----------|---------|
| **Prio** | CRÍTICA |
| **Bloqueada por** | FASE 8 mergeada |
| **Duración** | 2-3 semanas |
| **Capacidad** | Conexiones reales a exchange (sandbox primero, prod después) |

**Objetivo.** Ejecutar de verdad. CCXTTrader pasa de skeleton a real, con idempotencia (`clientOrderId`), stops nativos en exchange tras fill confirmado, reconciliación al arranque.

**Sub-commits previstos:**
- 9.1 — CCXTTrader real: lectura de credenciales, conexión, sandbox flag respetado
- 9.2 — `create_order` con clientOrderId UUID + tabla `orders` born append-only
- 9.3 — Native stop placement post-fill (separate stop_market order, reduceOnly)
- 9.4 — Tabla `trades` born append-only + `trade_status_events`
- 9.5 — Reconciliation al arranque: leer positions/orders del exchange, comparar con DB, reportar discrepancias
- 9.6 — Wiring final: signal aprobada + sized → executor → order placed → fill detected → stop placed → trade open

**Criterios de cierre:**
- [ ] Sandbox: 30 órdenes ejecutadas con clientOrderId únicos
- [ ] Stop nativo presente en exchange tras fill (verificable vía `fetch_open_orders`)
- [ ] Reconcile detecta huérfanas y reporta
- [ ] Reintentos con mismo clientOrderId no duplican
- [ ] Append-only para `trades` y `orders` desde commit 1
- [ ] trading_enabled sigue False; modo TradingMode = OFF (FASE 10 enable)

**Riesgo principal.** Falta de stop nativo tras fill. Si el bot crashea entre fill y stop placement, tienes posición abierta sin protección. Mitigación: callback síncrono de fill → stop, con retry inmediato si falla, y alerta crítica si no se coloca en N segundos.

**Decisiones que el JSON de FASE 9 debe firmar:**
1. Exchange inicial: ¿Binance sandbox o Bybit testnet primero?
2. min_notional por exchange: ¿hardcoded o leído de `exchange.markets`?
3. Política si stop nativo falla: ¿reintentar N veces, o cancelar la posición?
4. Cómo manejar partial fills (orden parcialmente llena con resto pendiente)

---

## FASE 10 — Trading Modes (gradual rollout)

| Atributo | Detalle |
|----------|---------|
| **Prio** | CRÍTICA |
| **Bloqueada por** | FASE 9 mergeada |
| **Duración** | 1 semana de código + 6+ semanas de validación temporal |

**Objetivo.** Implementar `TradingMode` enum (ya creado en pre-tweak 1) con transiciones controladas y guards temporales. El operador no puede saltar de SHADOW a LIVE.

**Modos:**
- `OFF`: solo lee, no genera signals
- `SHADOW`: genera signals, las loggea, no envía a Telegram, no ejecuta
- `PAPER`: ejecuta en testnet/sandbox del exchange
- `SEMI_AUTO`: signals reales pero requieren ✅ humano para ejecutar
- `LIVE`: full auto

**Sub-commits previstos:**
- 10.1 — `/mode <name>` Telegram command con guards
- 10.2 — Tabla `mode_transitions` (append-only) con histórico
- 10.3 — Guards temporales: SHADOW→PAPER requiere ≥14 días en SHADOW; PAPER→SEMI_AUTO requiere ≥30 días en PAPER y ≥50 trades cerrados
- 10.4 — `/mode_status` muestra modo actual, días en él, próximo modo permitido y cuándo
- 10.5 — Override por operador: comando `/mode_force` con razón obligatoria, queda en log

**Criterios de cierre:**
- [ ] Cambio de modo registrado en tabla histórica con actor
- [ ] Guards temporales aplicados (test: intentar PAPER con <14 días en SHADOW falla)
- [ ] `/mode_force` requiere razón y queda en audit log
- [ ] Modo persistido entre reinicios

**Riesgo principal.** Saltarse modos por impaciencia. Mitigación: los guards son hard-coded, requieren commit de código para reducirlos.

---

## FASE 11 — AI Validator + News Reactor + Postmortem batch

| Atributo | Detalle |
|----------|---------|
| **Prio** | ALTA |
| **Bloqueada por** | FASE 10 |
| **Duración** | 1-2 semanas |
| **Capacidad** | +200 LLM calls/día estimadas |

**Objetivo.** Activar las task_types `TRADE_VALIDATE` y `TRADE_POSTMORTEM` (creadas en pre-tweak 2 con cadenas vacías). Tres consumidores:

1. **Trade Validator** (per-signal): antes de RiskManager, LLM lee macro+news+signal y devuelve JSON con `approve, confidence, concerns, size_modifier, rationale_short`. Sesgo anti-rubber-stamp: prompt fuerza concerns ≥1, confidence default 0.5.
2. **News Reactor** (event-driven): noticia con sentiment fuerte sobre ticker en posición abierta → LLM evalúa si reducir/cerrar/hold. Genera propuesta, no ejecuta.
3. **Postmortem batch** (nightly): junta los N trades cerrados del día en un solo prompt, pide patrones. Una llamada por día, no por trade.

**Sub-commits previstos:**
- 11.1 — Prompts versionados de los 3 task_types
- 11.2 — Trade Validator wired entre Strategy y Risk
- 11.3 — News Reactor con dedupe por ventana
- 11.4 — Postmortem batch job nocturno
- 11.5 — Tabla `ai_validations` con histórico (append-only)
- 11.6 — `confidence_ai` populated en signals (era NULL desde FASE 7)

**Criterios de cierre:**
- [ ] Validator rechaza al menos 20% de signals en SHADOW (target inicial; ajustable según métricas)
- [ ] Postmortem batch produce reporte legible y persistido
- [ ] News reactor no spammea: un mensaje cada >30min por ticker
- [ ] AI quota respetada: <500 calls/día agregados

**Decisiones para JSON de FASE 11:**
1. Prompt templates exactos
2. Threshold de confidence para considerar "approve"
3. Política si AI Router falla todas las cadenas (degrade a auto-approve, auto-reject, o mantener pending)

---

## FASE 12 — Backtester

| Atributo | Detalle |
|----------|---------|
| **Prio** | ALTA |
| **Bloqueada por** | FASE 8 (Risk) y FASE 9 (Executor estructura) |
| **Duración** | 2 semanas |
| **Capacidad** | +disco para datos históricos (~50GB) |

**Objetivo.** Replay histórico bar-a-bar contra el `StrategyEngine` existente. Mismo motor, executor sustituido por `FillSimulator` con slippage configurable.

**Sub-commits previstos:**
- 12.1 — `Backtester` class con interfaz `run(strategy_id, universe, date_range) -> BacktestReport`
- 12.2 — `FillSimulator` con modelo de slippage (fixed bps + market impact + partial fill probabilistic)
- 12.3 — Métricas estándar: profit factor, max DD, Sharpe, Sortino, win rate, expectancy, R-multiples distribution
- 12.4 — Equity curve generation con y sin fees
- 12.5 — Tabla `backtest_runs` con resultados archivados (no toca tablas de producción)
- 12.6 — Endpoint `/backtest` y comando Telegram para correr y mostrar resumen
- 12.7 — Walk-forward harness: train/test splits, reporte estabilidad de parámetros

**Criterios de cierre:**
- [ ] Backtester corre las 3 estrategias actuales (`scanner.oversold.v1`, `breakout.v1`, `trending.v1`) sobre 2 años de cripto + equity
- [ ] Métricas matchean lo que produciría un backtester de referencia (validación cruzada con backtrader o vectorbt en 1 estrategia simple)
- [ ] Equity curve se renderiza como PNG en Telegram
- [ ] `backtest_runs` archivado, NO contamina `trades` o `signals` de producción

**Riesgo principal.** Look-ahead bias. Test obligatorio: el backtester con feed retrasado 1 bar produce los mismos signals que el live → si no, hay leak.

---

## FASE 13 — Observabilidad básica + comandos pánico + dead-man + incident registry

| Atributo | Detalle |
|----------|---------|
| **Prio** | CRÍTICA |
| **Bloqueada por** | FASE 10 |
| **Duración** | 1-2 semanas |

**Objetivo.** Observabilidad mínima para operar LIVE. Definición operativa de "incidente crítico" del ROADMAP.md Apéndice A.

**Sub-commits previstos:**
- 13.1 — Endpoint `/metrics` Prometheus format con métricas core (signals_generated, orders_placed, pnl_realized, drawdown_pct, latencias)
- 13.2 — Tabla `critical_incidents` + enum `CriticalIncidentType` con los 7 tipos del Apéndice A
- 13.3 — Auto-detection de incidentes: reconcile orphan, balance discrepancy, CB prolonged, missing native stop, kill switch DD, reconcile failed prolonged
- 13.4 — Comando `/incident <type> <reason>` para registro manual
- 13.5 — Función `days_clean_streak()` con la regla del doble criterio (>24h resolución OR tipo grave-reset-siempre)
- 13.6 — Comando `/panic`: cancela todas las órdenes abiertas, cierra todas las posiciones a market, kill switch ON
- 13.7 — Endpoint público `/heartbeat` para dead-man externo (Cloudflare Tunnel + GitHub Actions cron)
- 13.8 — Heartbeat Telegram cada 6h con resumen

**Criterios de cierre:**
- [ ] `/metrics` responde con formato Prometheus válido
- [ ] Incident auto-detection probada con 6 escenarios (uno por tipo automático)
- [ ] `/panic` cierra todo en <3s en sandbox
- [ ] Dead-man externo configurado y testeado: kill el bot y recibir alerta en <10min
- [ ] `days_clean_streak()` retorna valor correcto según reglas

**Riesgo principal.** Dead-man interno en lugar de externo. Si el watchdog vive en el mismo servidor que el bot, un fallo de red local los tira a ambos. Mitigación: GitHub Actions cron es la opción canónica.

---

## FASE 14 — LIVE con capital simbólico

| Atributo | Detalle |
|----------|---------|
| **Prio** | CRÍTICA (es el milestone) |
| **Bloqueada por** | FASES 11, 12, 13 cerradas |
| **Duración** | 60+ días de validación |

**Objetivo.** Activar `trading_enabled=True` y `TradingMode=LIVE` con capital ≤200€. Sizing 0.25% inicial. Las reglas de capital del ROADMAP.md Parte 10 entran en juego.

**Sub-commits previstos:**
- 14.1 — Pre-flight checklist automatizado: todos los gates verdes, todos los tests verdes, reconcile clean, dead-man activo, backups recientes, mode=PAPER pasó >30d con >50 trades
- 14.2 — Activación con audit log: comando `/go_live` requiere razón + confirmación 2FA via segundo comando ≥30s después
- 14.3 — Sizing inicial reducido: 0.25% (la mitad del default 0.5%) durante primeros 30 días LIVE
- 14.4 — Daily report 08:00 Madrid con PnL, trades, drawdown, days_clean_streak

**Criterios de cierre (60 días):**
- [ ] 60 días continuos en LIVE
- [ ] `days_clean_streak() >= 60`
- [ ] Drawdown realizado <15%
- [ ] Coverage de tests no regresada
- [ ] PR de FASE 14 mergeado tras revisión

**Reglas de escalado:** Apéndice A del ROADMAP. N1→N2 requiere `days_clean_streak >= 60`. Aumento de capital es un commit doc en el repo (`STRATEGIES.md` o similar) con justificación.

---

## FASES 15-22 — Inteligencia avanzada (post-LIVE)

A partir de aquí, las fases son **opcionales y dependientes** del éxito de FASE 14. No se planifican en detalle hasta tener data real de operación.

### FASE 15 — Equity/forex via Interactive Brokers

- Integrar `ib_insync` con thread dedicado (síncrono, no se mezcla con asyncio del resto)
- `MarketHoursService` para sesiones equity/forex
- Order types específicos (MOC, LOO, VWAP)
- Routing en `TraderRegistry`: signal de stock → IB, signal de cripto → CCXT
- **Bloqueada por:** FASE 14 estable 60d. **Capacidad:** +CPU thread.

### FASE 16 — Multi-exchange cripto

- Refactor `CCXTTrader` → `TraderRegistry: dict[ExchangeId, CCXTTrader]`
- Tabla `exchange_accounts` con keys vault-referenced
- Routing rules: liquidez, spread, comisión, saldo
- Health checks por venue
- **Bloqueada por:** FASE 14 estable. **Capacidad:** +RAM 200MB por trader extra.

### FASE 17 — Derivatives (perpetuals)

- Leverage gate (≤2x recomendado, ≤3x máximo absoluto)
- Tracking de funding payments en tabla separada
- Liquidation distance check
- **NO options.** Decisión arquitectónica: complejidad 10x sin justificación.
- **Bloqueada por:** FASE 16. **Riesgo principal:** liquidaciones. Mitigación: gate hard de leverage + alerta a 2x distancia de liquidación.

### FASE 18 — Librería de estrategias profesionales

Trabajo continuo, no fase cerrada. Construir 8-12 estrategias en producción, cada una con `strategy_id` versionado, cada una pasando backtest+forward test antes de LIVE.

Estrategias prioritarias (orden de implementación):
1. `pairs.mean_reversion.v1` — pares correlacionados >0.85, z-score
2. `equity.momentum_12_1.v1` — top decil 12m-1m, rebalanceo mensual
3. `fx.carry.v1` — long alta tasa, short baja tasa, vol-targeted
4. `breakout_mtf.v1` — breakout multi-timeframe con confirmación volumen
5. `funding.arb.v1` — arbitraje de funding rate spot+perp

Cada estrategia nueva requiere: backtest 5 años, walk-forward, stress test eventos extremos, 30d en PAPER, capital cap inicial 5%.

### FASE 19 — Smart Order Routing (SOR)

Para órdenes notional >$5K. TWAP slicing, posible split entre exchanges, re-pricing de limits, iceberg para tamaños grandes.

### FASE 20 — Walk-forward optimization

Reemplaza el `WalkForwardOptimizer` simple de FASE 12 con uno completo: grid search train/test rolling, scoring multi-objetivo (Sharpe + estabilidad), heatmap de regiones estables, cross-strategy regime sensitivity.

### FASE 21 — Machine Learning honesto

Aplicaciones legítimas:
1. **Filter classifier** (XGBoost/LightGBM): probabilidad de éxito de signal dada features → filtro pre-RiskManager
2. **Regime detector** (HMM con `hmmlearn`): {low_vol, mid_vol, high_vol_trending, high_vol_choppy}
3. **News embedding** con `sentence-transformers` mini → clustering temas

NO RNN/LSTM/Transformers para predicción de precio. Documentado como antipatrón.

A/B test obligatorio: nuevo modelo coexiste con anterior en paralelo, solo reemplaza si mejora estadísticamente significativa con n>500 signals.

### FASE 22 — Estrategias adaptativas régime-aware

`RegimeAwareStrategy` wrapper. Estrategias declaran `allowed_regimes`. Regime detector activa/desactiva. Tabla `regime_history` para análisis de performance condicionada.

---

## FASES 23-26 — Robustez operacional

### FASE 23 — Reconciliation continuo

Reemplaza el reconcile-at-boot de FASE 9 con loop continuo cada N segundos + WebSocket robusto:
- `watch_orders`, `watch_my_trades`, `watch_balance` via CCXT pro
- Reconnect exponencial con jitter
- Heartbeat tracking
- Buffer in-memory para detección de duplicados post-reconnect
- Fallback a polling REST como red de seguridad

### FASE 24 — Circuit breakers granulares

Más allá del kill switch global:
- Per-exchange CB
- Per-AI-provider (refina la fallback chain existente)
- Per-strategy (N losing trades consecutivos → desactivación automática)
- Per-ticker (errores constantes → blacklist temporal)
- Tabla `circuit_breaker_state`

### FASE 25 — Observability stack profesional

Reemplaza el `/metrics` minimal de FASE 13 con stack completo:
- **Prometheus** scrapeando `/metrics`
- **Loki** o **ClickHouse** para logs (recomendado: ClickHouse si vas en serio)
- **Grafana** con 3 dashboards (Operational, Trading, Strategy performance)
- **AlertManager** → Telegram (críticas) + email (warns)

**Capacidad:** +1GB RAM. Decisión: stack en BambuServer (subir a 16GB) o VPS dedicado para obs.

### FASE 26 — HA operacional

- Backups automáticos cada 6h a S3/B2/R2 con encryption at rest
- Test de restore mensual obligatorio
- Estado replicable en otra máquina <30 min
- Failover manual documentado en `OPERATIONS.md`
- DR drill cada 6 meses

---

## FASES 27-30 — Datos avanzados

### FASE 27 — On-chain data (cripto)

- Glassnode (suscripción si justificado), Dune Analytics (free), Etherscan/Blockscout, DeFiLlama
- Métricas: exchange netflow, stablecoin supply on exchanges, MVRV, SOPR, funding aggregated, OI
- No genera trades; alimenta filter classifier (FASE 21) y gates condicionales

### FASE 28 — Calendar awareness

El gate que aplazamos en FASE 8.7. Implementación tardía porque depende de:
- `CalendarService` con upcoming_events
- Earnings (Finnhub), FOMC (FRED), economic releases
- Crypto: halvings, network upgrades, token unlocks (CryptoRank, Tokenomist)
- Forex: central bank decisions
- Gates: `EarningsBlackoutGate`, `FOMCBlackoutGate`, `MajorReleaseGate`
- También aquí entra el **stale-by-price gate** (TODO de 8.7)

Solo aplica a apertura de posiciones nuevas, NO a cierre (las posiciones abiertas mantienen sus stops nativos).

### FASE 29 — Alternative data

Ruidoso pero potencialmente útil como input ML:
- Twitter/X sentiment (API Basic ~$200/mes o `snscrape`)
- Reddit r/wallstreetbets
- Google Trends (`pytrends`, gratis)
- Order book microstructure (depth + flow)
- GitHub activity (cripto)
- Job postings (equity)

Solo via filter classifier, no como señal directa.

### FASE 30 — Data lake propio

Cuando lleves 12+ meses operando y SQLite se quede corto:
- 30.1 SQLite → PostgreSQL
- 30.2 Hot data en PG + cold data en Parquet/Iceberg sobre S3
- 30.3 ClickHouse para analytics OLAP

Trabajo serio, 6-12 meses post-LIVE estable.

---

## FASES 31-33 — Compliance, fiscal, legal

### FASE 31 — Tax accounting (CRÍTICA pre-capital >10k€)

**Modelo 721, Modelo 100 (IRPF), Modelo 720** según corresponda.

Módulo `src/mib/tax/`:
- `fifo_calculator.py` (FIFO obligatorio en ES)
- `exchange_rate.py` (ECB rates históricos)
- `reports/modelo_721.py`, `modelo_100.py`, `annual_summary.py`

**Plazo no negociable:** implementar antes de capital significativo (>10k€). Reconstruir un año fiscal a posteriori es un infierno.

### FASE 32 — Disclaimer y limitación legal

Si bot es uso personal único: disclaimers actuales cubren. Si hay segundo usuario: abogado obligatorio (CNMV + MiCA en vigor desde dic-2024).

**Recomendación firmada en ROADMAP.md:** mantener uso personal único. Cualquier escalado a terceros es FASE estratégica con asesor legal.

### FASE 33 — Auditabilidad regulatoria

- Append-only ya garantizado desde FASE 8.1 (tablas signals, trades, risk_decisions, orders, mode_transitions)
- Retención mínima 7 años (obligación fiscal española)
- Backups firmados (hash + timestamp en sitio independiente)

Esta fase es prácticamente "documentar y verificar" porque la disciplina ya está embebida. Cero refactor si append-only se respetó desde 8.1.

---

## FASES 34-36 — Performance e infraestructura

### FASE 34 — Performance optimization

Targets:
- Signal generation: <500ms p99
- Order placement: <200ms p99
- Reconcile sweep: <30s p99

Técnicas: connection pooling, profiling con `py-spy`, caching alineado a candle close, polars donde pandas ahogue, TA-Lib en C para hot-path indicators.

### FASE 35 — Hardware planning

BambuServer 4GB → 16-32GB, o VPS dedicado (Hetzner CCX13 ~25€/mes), o híbrido (trading core en VPS, obs en casa).

**Recomendación firmada:** híbrido. Trading core en Hetzner (latencia <50ms a Binance EU); observability + backups + dev en BambuServer.

### FASE 36 — Latencia geográfica

Solo si haces estrategias <1min holding. Co-locación en data center próximo al matching engine del exchange (AWS Frankfurt para Binance EU). Reduce latencia de ~150ms a ~5-10ms.

Para holding >15min es over-engineering. Skip a menos que justificado.

---

## FASES 37-39 — Seguridad de grado producción

### FASE 37 — API key hygiene avanzada

- Permisos mínimos por key (read-only data, trade-only-no-withdraw para trader)
- IP whitelist en exchange (IP fija de VPS)
- Rotación trimestral
- Cuenta exchange dedicada al bot (separada de holdings personales)

### FASE 38 — Vault encriptado

`SOPS + age` (gratis, simple, suficiente). `.env.encrypted` en git privado, desencriptación en memoria solo al arranque.

### FASE 39 — Defensa en profundidad

- Container con read-only filesystem
- Sin shell ni curl/wget en imagen final
- Network policy estricta (egress whitelist)
- `pip-audit` semanal en CI
- SBOM por release
- `falco` en host (opcional, justificado en LIVE)
- 2FA en todo

---

## FASES 40-42 — UX y productización

### FASE 40 — Telegram avanzado

- Comandos contextuales con buttons inline en cada signal
- Conversational flow con `ConversationHandler` (e.g. `/strategy create` step-by-step)
- Reportes programados: daily, weekly, monthly
- Voice notes para alertas críticas (TTS local)
- Inline queries (`@MIBbot AAPL` desde cualquier chat)

### FASE 41 — Web UI mínimo

FastAPI ya existe + SPA estática (React o HTMX+Tailwind). Páginas: Dashboard, Strategies, Signals, Trades, Risk, Backtester. Auth via Cloudflare Access.

### FASE 42 — Mobile app companion (opcional)

**Skip recomendado.** Telegram + web UI cubren todo lo necesario. Solo si te apetece como proyecto paralelo.

---

## Snapshot del progreso

```
[FASES 1-7]   ✅ Completadas — Motor analítico + Signal layer
[FASE 8]      🚧 En curso — Risk Management
[FASES 9-14]  📋 Path crítico hasta LIVE estable
[FASES 15-22] 📋 Inteligencia avanzada (post-LIVE)
[FASES 23-26] 📋 Robustez operacional
[FASES 27-30] 📋 Datos avanzados
[FASES 31-33] 📋 Compliance
[FASES 34-36] 📋 Performance
[FASES 37-39] 📋 Seguridad pro
[FASES 40-42] 📋 UX y productización
```

**Path crítico hasta LIVE estable:** 8 → 9 → 10 → 11 → 12 → 13 → 14 → 23 → 28 → 31 → 37 → 25 → 38

**Estimación temporal honesta:**

| Hito | Duración acumulada |
|------|--------------------|
| FASES 8-13 cerradas | 3-4 meses calendario desde hoy |
| FASE 14 + estabilización 60 días | 5-6 meses |
| Bot Definitivo (Partes 2-9 mayoría) | 24-36 meses |

**Cualquier estimación es optimista.** La realidad: nadie mantiene la cadencia ideal. Ajusta expectativas.

---

## Cómo usar este mapa en cada sesión

1. Al arrancar sesión, lee este archivo tras `MASTER-PROMPT.md`.
2. Identifica fase activa (`git branch -a` te dice).
3. Re-lee la entrada de esa fase en este mapa para refrescar el "qué" general.
4. El JSON de la fase activa te da el "cómo" operativo.
5. Cuando termines una fase y vayas a abrir la siguiente, consulta este mapa para confirmar dependencias.

**Si una fase se desvía del plan original** (e.g. FASE 9 descubre que necesita un sub-commit no previsto), ese desvío se documenta en la siguiente sesión estratégica y se actualiza este mapa. El mapa es vivo, no estático.
