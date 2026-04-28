# Market Intelligence Bot — El Bot Definitivo

> Documento maestro: todo lo que falta para llevar MIB de "motor analítico bien construido" a "sistema de trading autónomo de grado profesional, robusto, medible y mantenible a 5 años vista".
>
> Este documento NO sustituye a `PROJECT.md`. Es el complemento estratégico: define el horizonte completo, las decisiones que aún hay que tomar, y las trampas conocidas que han hundido a otros sistemas similares.
>
> **Estado de partida (a fecha de este documento):** FASES 1-7 cerradas. Motor analítico operativo, capa Signal con invalidation/targets ATR-derivados, persistencia y approval flow Telegram. Sin ejecución cableada.

---

## Índice

- [Parte 0 — Filosofía y principios del Bot Definitivo](#parte-0--filosofía-y-principios-del-bot-definitivo)
- [Parte 1 — Roadmap acordado: FASES 8 a 14 (resumen ejecutivo)](#parte-1--roadmap-acordado-fases-8-a-14-resumen-ejecutivo)
- [Parte 2 — Extensión multi-mercado (FASES 15-17)](#parte-2--extensión-multi-mercado-fases-15-17)
- [Parte 3 — Capa de inteligencia avanzada (FASES 18-22)](#parte-3--capa-de-inteligencia-avanzada-fases-18-22)
- [Parte 4 — Robustez operacional y alta disponibilidad (FASES 23-26)](#parte-4--robustez-operacional-y-alta-disponibilidad-fases-23-26)
- [Parte 5 — Capa de datos avanzada (FASES 27-30)](#parte-5--capa-de-datos-avanzada-fases-27-30)
- [Parte 6 — Compliance, fiscal y legal (FASES 31-33)](#parte-6--compliance-fiscal-y-legal-fases-31-33)
- [Parte 7 — Performance e infraestructura (FASES 34-36)](#parte-7--performance-e-infraestructura-fases-34-36)
- [Parte 8 — Seguridad de grado producción (FASES 37-39)](#parte-8--seguridad-de-grado-producción-fases-37-39)
- [Parte 9 — UX, observabilidad y productización (FASES 40-42)](#parte-9--ux-observabilidad-y-productización-fases-40-42)
- [Parte 10 — Disciplina de proceso (no es código pero define éxito o fracaso)](#parte-10--disciplina-de-proceso-no-es-código-pero-define-éxito-o-fracaso)
- [Apéndice A — Tabla maestra de fases con prioridad y dependencias](#apéndice-a--tabla-maestra-de-fases-con-prioridad-y-dependencias)
- [Apéndice B — Decisiones pendientes que necesitan respuesta antes de FASE N+1](#apéndice-b--decisiones-pendientes-que-necesitan-respuesta-antes-de-fase-n1)
- [Apéndice C — Anti-patrones documentados (qué NO hacer)](#apéndice-c--anti-patrones-documentados-qué-no-hacer)

---

## Parte 0 — Filosofía y principios del Bot Definitivo

Un sistema de trading "definitivo" no es uno con más features. Es uno que cumple cinco propiedades simultáneamente:

1. **Operacional 24/7 sin intervención manual rutinaria.** Un bot que requiere que mires Telegram cada hora no es un bot, es un asistente. El definitivo solo te interrumpe cuando algo es genuinamente excepcional.
2. **Defensivo por construcción.** Cada componente asume que el de al lado puede fallar. No hay un solo punto donde "si esto cae, perdemos dinero".
3. **Auditable hasta el último tick.** Cada decisión, cada orden, cada modificación de estado queda registrada con timestamp, contexto e inputs. Tres meses después puedes responder "¿por qué entró aquí?" sin adivinar.
4. **Evolutivo sin reescrituras.** Añadir una estrategia, un mercado, un proveedor de datos o un gate de riesgo es un módulo nuevo, no un refactor. La arquitectura ya construida en FASES 1-7 es exactamente esto.
5. **Honesto consigo mismo.** Mide su propio rendimiento sin sesgo. Si una estrategia no funciona, lo sabe — porque tiene los datos para saberlo, y la disciplina de mirarlos.

### Principios no negociables que se mantienen desde el spec original

- Cero dependencias de pago **donde haya alternativa libre comparable**. Esta regla se relaja parcialmente en FASE 27+ (alternative data) donde el coste-beneficio puede justificarlo, pero se documenta cada excepción.
- Cero secrets en código. Vault encriptado en FASE 38.
- Cero binding a 0.0.0.0 fuera de la red Docker.
- Cero modelos LLM locales (el host no tiene RAM). Esto se revisa en FASE 35 si migras hardware.
- Doble seatbelt en cualquier path con efectos en exchange (`trading_enabled` global + `dry_run` por instancia + circuit breakers de FASE 23).
- Todo el código en inglés, strings de usuario en español.

### Principios nuevos que añade este documento

- **Reversibilidad por defecto.** Toda acción automática debe tener un comando de reversión documentado y testeado. Si abres una posición, hay un comando que la cierra. Si activas un modo, hay un comando que lo desactiva.
- **Estado canónico vs. estado derivado.** El exchange es la fuente de verdad para posiciones y saldo. Tu DB es la fuente de verdad para signals, decisions e historial. Nunca al revés. Reconciliar ambos en arranque y en intervalo continuo (FASE 23).
- **Idempotencia universal.** Cualquier operación que pueda repetirse por reintento debe ser idempotente. `clientOrderId` para órdenes; UUIDs para signals; deduplicación por hash para news.
- **Observabilidad antes que features.** Todo módulo nuevo emite métricas estructuradas antes de considerarse completo. "Funciona" no es lo mismo que "es observable cuando deja de funcionar".
- **Capacidad presupuestada.** Cada módulo declara su consumo esperado de RAM, calls/día a APIs externas y queries/segundo a la DB. Sumas presupuestos antes de añadir, no después.
- **Append-only para tablas con lifecycle.** Toda tabla cuyos registros tengan transiciones de estado (`signals`, `trades`, `risk_decisions`, `orders`, futuras lifecycle tables) usa el patrón append-only desde su nacimiento: tabla principal con cache denormalizado del último estado + tabla histórica `<entity>_status_events` con cada transición. Mutaciones siempre vía helper `transition(entity_id, to_status, actor, event_type, reason)` que escribe el evento histórico y actualiza el cache atómicamente. Cero `UPDATE` directo desde callers de negocio. Esto sostiene la auditabilidad regulatoria de FASE 33 sin refactor doloroso, y soporta debugging forense ("¿quién/cuándo/por qué cambió esta signal?") desde el día uno.

---

## Parte 1 — Roadmap acordado: FASES 8 a 14 (resumen ejecutivo)

Estas fases ya están definidas en sesiones anteriores y aprobadas. Las incluyo aquí solo para referencia — el detalle por commit ya está pactado.

| Fase | Alcance | Estado |
|------|---------|--------|
| 8 | Risk Management (TTL signals, PortfolioState, RiskManager + Gate Protocol, sizing, kill switches) | Próxima |
| 9 | Order Executor (CCXTTrader real, idempotencia, stops nativos, reconciliación al arranque) | Pendiente |
| 10 | Modos graduales (OFF → SHADOW → PAPER → SEMI_AUTO → LIVE) con thresholds temporales | Pendiente |
| 11 | AI Validator + News Reactor + Post-mortem batch | Pendiente |
| 12 | Backtester con métricas estándar | Pendiente |
| 13 | Observabilidad básica + comandos de pánico + dead-man switch externo | Pendiente |
| 14 | LIVE con capital simbólico (≤200€), sizing 0.25%, escalado gradual | Pendiente |

**Hito de validación tras FASE 14:** 60 días continuos en LIVE con capital simbólico sin incidente operacional crítico. Solo entonces se considera FASE 14 cerrada y se procede a las extensiones de las Partes 2-9.

---

## Parte 2 — Extensión multi-mercado (FASES 15-17)

Hasta FASE 14, todo apunta a CCXT (cripto spot). El bot definitivo opera en múltiples clases de activo y múltiples cuentas, porque la diversificación de plataforma es tan importante como la de instrumento.

### FASE 15 — Equity/forex execution via Interactive Brokers

**Por qué IB y no Alpaca:** IB cubre 150+ mercados globales, ofrece API estable (`ib_insync` en Python), comisiones competitivas, soporta forex spot, futuros, opciones, bonos. Alpaca es más simple pero limitado a equity USA y cripto USA. Para un sistema definitivo IB es el camino.

**Coste real:** la cuenta IB Lite es gratis (sin comisiones en US equity). IB Pro cobra por API access en algunos casos pero compensa con execution quality mejor. El compromiso: arrancar con Lite, migrar a Pro cuando volumen lo justifique.

**Componentes nuevos:**

```
src/mib/sources/ib_source.py        # market data via IB (alternativa a yfinance)
src/mib/trading/ib_trader.py        # execution via ib_insync
src/mib/trading/router.py           # decide si una signal va a CCXT o IB según ticker
```

**Decisiones de diseño:**

1. **`ib_insync` en thread separado.** IB API es síncrona y mantiene una conexión TCP persistente. La integración correcta es: thread dedicado con su propio event loop síncrono, comunicado con el resto del bot async vía `asyncio.Queue`. NO uses `loop.run_in_executor` por cada call — la conexión se rompe.

2. **Sesiones de mercado.** Cripto opera 24/7. Equity USA opera 9:30-16:00 ET con pre-market 4:00-9:30 y after-hours 16:00-20:00. Forex opera dom 17:00 ET → vie 17:00 ET. El `MarketHoursService` debe saber:
   - Si el mercado está abierto para ese ticker
   - Cuándo abre el siguiente
   - Si hay festivo (NYSE: NYSE holiday calendar, hard-coded por año o vía `pandas_market_calendars`)
   - Cuánto falta para el cierre (relevante para no abrir 5 min antes del close)

3. **Order types específicos de equity.**
   - **Market on Close (MOC)** — ejecuta al precio de cierre, útil para estrategias EOD
   - **Limit on Open (LOO)** — para fadear el gap de apertura
   - **VWAP / TWAP** — para órdenes grandes con execution algorítmica

   Tu `Signal` actual no soporta estos types. La extensión es opcional en `Signal.execution_hint: ExecutionHint | None`.

4. **Margin & short selling.** Para shorts en equity necesitas margin account. Esto introduce **margin call risk** que el RiskManager debe gestionar (gate nuevo: `margin_buffer_min_pct`). En cripto spot esto no existe; en futuros sí.

**Comisiones realistas a modelar en el sizer y el backtester:**

| Mercado | Comisión típica |
|---------|-----------------|
| US Equity (IB Pro) | $0.005/share, mínimo $1 |
| US Equity (IB Lite) | $0 |
| Forex IB | 0.2 pips típico majors |
| Cripto Binance spot | 0.10% maker/taker (0.075% con BNB) |
| Cripto Binance VIP1+ | 0.09%/0.10% (con volumen mensual >$1M) |
| Cripto Bybit spot | 0.10% |

**Acceptance:** una signal sobre `AAPL` se rutea a IB, otra sobre `BTC/USDT` a Binance, ambas con sizing correcto considerando comisión, ambas reconciliadas al arranque, ambas con stop nativo.

### FASE 16 — Multi-exchange / multi-account cripto

Operar todo el capital en Binance es un riesgo concentrado. Binance ha tenido outages de varias horas, problemas regulatorios y restricciones por jurisdicción. La diversificación correcta es:

- **Binance** — pares amplios, profundidad de libro, derivatives
- **Bybit** — alternativa principal, copy-trading desactivado, funding rates competitivos en perp
- **Kraken** — para EUR/fiat on-ramp y cumplimiento UE
- **OKX** — backup, especialmente fuerte en derivatives

**Refactor necesario en CCXTTrader:**

```python
# Antes (FASE 9)
class CCXTTrader:
    def __init__(self, exchange_id: str, ...): ...

# Después (FASE 16)
class TraderRegistry:
    def __init__(self, traders: dict[ExchangeId, CCXTTrader]): ...

    def for_signal(self, signal: Signal) -> CCXTTrader:
        # Decide ruta según:
        # 1. Disponibilidad del par en cada exchange
        # 2. Profundidad de libro actual (best_bid/ask + size)
        # 3. Comisión efectiva considerando el tier VIP de cada cuenta
        # 4. Saldo disponible
        # 5. Reglas explícitas (e.g. "perpetuals → Bybit")
```

**Tabla nueva en DB:**

```sql
exchange_accounts (
    id, exchange_id, account_label, api_key_ref, api_secret_ref,
    enabled, tier, max_capital_eur, last_health_check_at, notes
)
```

`api_key_ref` apunta al vault de FASE 38, no al secret directo.

**Reglas de routing inteligente:**

1. Si un exchange está en mantenimiento programado (consultar status pages: status.binance.com, etc., scrape RSS), saltarlo.
2. Si la spread del libro es >0.5% en un exchange y <0.1% en otro, ir al líquido (excepto si la signal exige uno específico).
3. Para órdenes >$10K de notional, hacer **smart order routing** (dividir entre exchanges) — pero esto se delega a FASE 19.

### FASE 17 — Derivatives (perpetuals + options)

Este es un salto cualitativo, no cuantitativo. Spot trading y derivatives tienen mecánicas distintas:

**Perpetuals:**
- Leverage (típicamente 1x-20x; nunca >5x con sizing por riesgo)
- Funding rate cada 8h (puede ser positivo o negativo, afecta PnL)
- Liquidation price (más estricto que stop-loss; el exchange te liquida si tocas)
- Mark price vs. index price vs. last price (los stops disparan sobre mark, no last)

**Options (mucho más adelante, posiblemente nunca):**
- Greeks (delta, gamma, theta, vega)
- Implied volatility surface
- Strategies: covered call, protective put, spreads, straddles
- Liquidez fragmentada (en cripto: Deribit es el único líquido para BTC/ETH)

**Mi recomendación seria:** **NO añadas options al bot.** Son una clase de instrumento con complejidad 10x sobre spot/perps y muy poco margen de error. Dejarlo fuera no es una limitación, es disciplina.

**Para perpetuals sí, con cuidado:**

- Gate de leverage máximo en RiskManager (≤3x recomendado)
- Tracking de funding rate como métrica de PnL separada
- Tabla `funding_payments` con histórico
- Liquidation distance check: si el precio se acerca a liquidación, alerta crítica antes de stop-loss

**Acceptance FASE 17:** una signal en `BTC/USDT:USDT` (perpetual) se ejecuta con leverage 2x, se respeta el gate de leverage máximo, los funding payments se contabilizan en PnL.

---

## Parte 3 — Capa de inteligencia avanzada (FASES 18-22)

Las FASES 1-14 te dan un sistema con **3 estrategias didácticas** (`oversold`, `breakout`, `trending` v1). Eso no es "el bot definitivo". Es el cascarón. La inteligencia real son las siguientes 5 fases.

### FASE 18 — Librería de estrategias profesionales

El objetivo es tener **8-12 estrategias en producción**, cada una con su `strategy_id` versionado, cada una con backtest+forward test demostrando edge antes de pasar a LIVE. Esto es trabajo continuo de meses, no una fase cerrada.

**Familias de estrategias a implementar (en orden de prioridad):**

#### 1. Mean reversion en pares correlacionados (cripto + equity)

**Idea:** dos activos con correlación histórica >0.85 (ej: ETH/USDT vs SOL/USDT, o XOM vs CVX) tienden a converger cuando divergen. Cuando el spread (z-score normalizado) supera ±2σ, abres long del barato y short del caro, esperas reversión.

```
strategy_id: "pairs.mean_reversion.v1"
inputs:
  - lookback: 60 días
  - z_entry: ±2.0
  - z_exit: ±0.5
  - correlation_min: 0.85
  - cointegration_pvalue_max: 0.05  (test ADF de Engle-Granger)
```

**Trampas:**
- Correlación rota — el par deja de correlacionar (cambio régimen, fundamentals divergentes). Mitigación: rebalanceo mensual del universo de pares + check de cointegración.
- Asimetría short — en cripto spot no puedes shortear sin margin. Variante "long-only spread" usando el ratio de los dos.

#### 2. Momentum sectorial en equity

**Idea:** comprar el top decil de stocks/ETFs por retorno a 12-1 meses (12m menos último mes para evitar mean reversion de corto plazo), rebalanceo mensual.

```
strategy_id: "equity.momentum_12_1.v1"
universe: S&P500 components + sector ETFs
ranking: rolling_return(252) - rolling_return(21)
top_decile: 50 nombres
weighting: equal-weight | volatility-targeted
rebalance: monthly, last business day
```

**Trampas:**
- Crashes de momentum (2009, 2020 marzo) — pérdidas brutales. Mitigación: filtro de mercado (solo activo si SPY > MA200) o stop a nivel portfolio.
- Survivorship bias en backtest — usa CRSP o Norgate Data, no la composición actual del S&P proyectada hacia atrás.

#### 3. Carry en forex

**Idea:** long divisas con tipos altos (BRL, MXN, ZAR), short divisas con tipos bajos (JPY, CHF, EUR). El differential de tipos te paga vía swap.

```
strategy_id: "fx.carry.v1"
universe: G10 + emergentes líquidos
ranking: tasa policy + tasa real esperada (FRED para datos macro)
position: long top 3, short bottom 3
size: equal-weight, vol-targeted al 10% anualizado
```

**Trampas:**
- Carry crashes — JPY se aprecia 15% en una semana cuando hay risk-off (caso clásico: agosto 2024). Mitigación: filtro de VIX (>30 → flat).

#### 4. Breakout multi-timeframe (cripto)

Versión avanzada de tu `breakout v1`:

```
strategy_id: "breakout_mtf.v1"
entry:
  - 4h: precio > donchian_high(20)
  - 1d: ADX(14) > 25 + dirección coincidente
  - 1h: confirmación de volumen >1.5× MA20
exit:
  - stop: 2×ATR(14) en 4h
  - target: trailing stop con chandelier exit (3×ATR desde el high)
```

#### 5. Funding rate arbitrage (perpetuals cripto)

**Idea:** cuando el funding rate de un perpetual está extremo (>0.1% por 8h, anualizado >100%), abres posición opuesta en perp + posición spot, capturas el funding.

```
strategy_id: "funding.arb.v1"
entry:
  - funding_rate(BTC perp Binance) > +0.1% per 8h
  - acción: short perp + long spot (cantidad equivalente)
exit:
  - funding < +0.02% (3 períodos consecutivos) o stop loss en spread
```

**Trampas:**
- Costes de transacción: la posición spot+perp tiene 4 fills (entrada + salida × 2). Solo rentable si funding × tiempo > 4 × fees + slippage.
- Riesgo de basis: el spread spot vs. perp puede ampliarse antes de cerrar.

#### 6-12. Otras a explorar

- **Volatility risk premium** — vender vol cuando IV > RV (en options, no recomendado por ahora).
- **News-driven momentum** — entrada tras noticia de earnings beat con sentiment muy positivo, exit a 5 días.
- **VWAP reversion intraday** — desviaciones extremas de VWAP en equity líquido revierten a final del día.
- **Cross-exchange arbitrage cripto** — diferencias de precio entre Binance y Coinbase (cuando >0.3%, ejecutas arbitraje).
- **Liquidation cascades** — cuando hay liquidaciones masivas en perps, el precio sobreajusta. Entrada contraria con stop estricto.
- **Term structure trades** — futuros de commodities con contango/backwardation extremo.

#### Disciplina obligatoria por estrategia nueva

Antes de que cualquier estrategia entre en LIVE, debe pasar:

1. **Backtest completo** — mínimo 5 años de datos donde el activo tenga histórico suficiente; reportar las métricas estándar (FASE 12).
2. **Walk-forward validation** — partición temporal: optimizas en [2018-2022], validas en [2023-2024]. Si la performance se degrada >50%, rechaza.
3. **Stress test** — simular ejecución durante eventos extremos (flash crash de mayo 2010, COVID marzo 2020, FTX nov 2022, etc.). Las estrategias que se hubieran arruinado en esos episodios necesitan kill switches específicos.
4. **Forward test paper** — 30 días mínimo en PAPER antes de LIVE.
5. **Capital inicial cap** — toda estrategia nueva en LIVE arranca con max 5% del capital total. Solo aumenta si tras 90 días el Sharpe realizado >1.0.

### FASE 19 — Smart Order Routing (SOR)

Cuando el sizing supera ciertos umbrales, una orden en un solo libro mueve el precio. SOR la divide:

**Componentes:**

```python
class SmartOrderRouter:
    async def execute(self, signal: Signal, sized: SizedDecision) -> ExecutionPlan:
        """
        Recibe una decisión de sizing y produce un plan de ejecución:
        - 1 sola orden si notional < threshold
        - división TWAP si notional grande
        - división entre exchanges si SOR multi-venue activo
        - re-pricing de limit orders si no fillean en N segundos
        """
```

**Algoritmos de ejecución:**

1. **TWAP (Time-Weighted Average Price)** — divide la orden en N slices iguales, una cada T minutos. Útil cuando no hay urgencia.
2. **VWAP** — slices proporcionales al volumen histórico de cada hora del día. Útil en equity.
3. **POV (Percent Of Volume)** — ejecutas como X% del volumen actual. Adaptativo.
4. **Iceberg** — muestras solo una parte del tamaño en el libro.
5. **Implementation Shortfall** — modelo Almgren-Chriss balanceando market impact vs. timing risk.

**Cuándo activar SOR:**
- Notional > $5K en cripto majors → TWAP en 4 slices
- Notional > $20K en cripto majors → TWAP en 10 slices, posible split entre Binance + Bybit
- Notional > $50K en cualquier cosa → SOR completo + revisión manual obligatoria

### FASE 20 — Optimización de parámetros (walk-forward)

Los `k_invalidation=1.5`, `r_multiples=(1.0, 3.0)`, RSI<30, ADX>25 son **magic numbers**. Son razonables pero no necesariamente óptimos. La optimización correcta es:

**NO HACER:**
- Optimizar sobre todo el histórico y usar los parámetros ganadores en LIVE → curve-fitting, garantía de fracaso.

**HACER:**

```python
class WalkForwardOptimizer:
    def __init__(
        self,
        strategy_class: type[Strategy],
        param_grid: ParamGrid,
        train_window: timedelta = timedelta(days=730),  # 2 años
        test_window: timedelta = timedelta(days=90),    # 3 meses
        step: timedelta = timedelta(days=30),
        objective: Objective = SharpeRatio,
    ): ...

    def run(self, data: pd.DataFrame) -> WalkForwardReport:
        """
        1. Partición rolling: train [t-730d, t], test [t, t+90d]
        2. Grid search en train, score en test
        3. Avanza step (30d) y repite
        4. Reporta:
           - Estabilidad de parámetros (si los óptimos cambian salvajemente cada step → estrategia frágil)
           - Performance out-of-sample agregada
           - Heatmap de Sharpe por combinación de parámetros (para detectar regiones estables vs. picos aislados)
        """
```

**Regla clave:** un parámetro solo se considera "robusto" si su Sharpe en test > Sharpe sample media de combinaciones aleatorias **y** la región vecina (±20% del valor) tiene Sharpe similar. Picos aislados = curve fit.

**Capacidad computacional:** un grid de 5 parámetros × 5 valores cada uno = 3125 combinaciones × N steps × M tickers. Con BambuServer y pandas-ta esto es lento. Soluciones:
- Cachear OHLCV histórico en parquet local (disco no es problema)
- `joblib.Parallel` con todos los cores
- Si sigue siendo demasiado lento → migrar este módulo a `polars` (10x sobre pandas en operaciones columnares)

### FASE 21 — Machine Learning (con honestidad sobre límites)

ML en finanzas es casi siempre overfit camuflado. Pero hay aplicaciones legítimas:

**Sí merece la pena:**

1. **Filtro de signals con clasificador** — no genera signals, las filtra. Modelo binario "esta signal hizo TP o SL en backtest histórico, dada las features X, Y, Z (vol regime, correlación con SPY, sentiment news, hora del día)". Entrenas en signals históricas (las tuyas, generadas por las estrategias de FASE 18). Output: probabilidad de éxito. Filtras las signals con prob < umbral.

2. **Volatility regime detection** — modelo no supervisado (HMM, GMM) que clasifica el régimen actual del mercado en {low_vol, mid_vol, high_vol_trending, high_vol_choppy}. Las estrategias activan/desactivan según régimen. P.ej., mean reversion solo en `high_vol_choppy`.

3. **News embedding y clustering** — vectorizas titulares con `sentence-transformers` (modelo pequeño tipo `all-MiniLM-L6-v2`), agrupas por proximidad semántica. Detecta cuándo "todo el mundo está hablando de lo mismo" → señal de hype/capitulación.

**NO merece la pena (todavía, posiblemente nunca):**

1. Predicción de precios con RNN/LSTM/Transformers. La literatura es clara: no funciona out-of-sample en mercados líquidos. El tiempo invertido es siempre mejor en otra parte.
2. Reinforcement learning para descubrir estrategias. Inestable, sample-inefficient, transferencia paper→live brutalmente mala.

**Stack pragmático:**

```
src/mib/ml/
├── features.py          # feature engineering reproducible
├── filter_classifier.py # XGBoost / LightGBM
├── regime_detector.py   # HMM con hmmlearn
├── training.py          # pipeline reentrenamiento
├── inference.py         # carga modelo + predict en producción
└── registry.py          # versionado de modelos (qué modelo está en producción)
```

**Reglas estrictas:**
- Modelos versionados (`v1`, `v2`, …) con checkpoint persistido.
- Reentrenamiento programado (mensual) y manual.
- Métricas de performance del modelo separadas de PnL: AUC en filter classifier, log-likelihood en HMM.
- A/B test obligatorio: 50% de signals con filtro nuevo, 50% sin (o con anterior). Solo cambias a v2 si la mejora es estadísticamente significativa con n>500 signals.

### FASE 22 — Estrategias adaptativas (régime-aware)

Una estrategia que funciona siempre no existe. Una estrategia que funciona en su régimen y se desactiva fuera, sí.

```python
class RegimeAwareStrategy:
    def __init__(self, base_strategy: Strategy, allowed_regimes: set[Regime]): ...

    async def evaluate(self, ticker: str) -> Signal | None:
        current_regime = await self._regime_detector.current()
        if current_regime not in self.allowed_regimes:
            return None  # estrategia desactivada en este régimen
        return await self.base_strategy.evaluate(ticker)
```

Tabla `regime_history` con timestamps de transiciones. Permite analizar performance condicionada al régimen.

---

## Parte 4 — Robustez operacional y alta disponibilidad (FASES 23-26)

El motor más fino del mundo no sirve si se cae los lunes. Esta parte es lo que separa "bot funcional" de "infraestructura confiable".

### FASE 23 — Reconciliation loop continuo

La reconciliación al arranque (FASE 9) cubre el caso "el bot se reinicia". No cubre:

- Exchange con mantenimiento de 4h durante el cual el bot estuvo "pensando" pero no podía mandar órdenes
- WebSocket de fills desconectado y reconectado perdiendo eventos
- Stop-loss disparado en el exchange sin que el bot lo registre porque iba en el WS perdido
- Doble fill por reintento mal idempotente
- Posición cerrada manualmente desde la app del exchange por el usuario

**Solución: reconciliation continuo + WebSocket robusto.**

```python
class ContinuousReconciler:
    async def run_forever(self):
        while True:
            await asyncio.sleep(self._interval_seconds)
            try:
                await self._reconcile_once()
            except Exception as e:
                logger.error("reconcile failed", exc_info=e)
                # No raise — el loop debe continuar

    async def _reconcile_once(self):
        # 1. Snapshot atómico de DB local
        local_state = await self._snapshot_local()

        # 2. Snapshot del exchange (positions + open_orders + balance)
        exchange_state = await self._snapshot_exchange()

        # 3. Diff
        diff = compute_diff(local_state, exchange_state)

        # 4. Resolución
        for discrepancy in diff:
            await self._resolve(discrepancy)

        # 5. Métrica
        metrics.gauge("reconcile.discrepancies_found", len(diff))
        metrics.gauge("reconcile.last_run_seconds_ago", 0)
```

**Tipos de discrepancias y resolución:**

| Discrepancia | Acción |
|--------------|--------|
| Posición en exchange, no en DB | INVESTIGATE: alerta crítica, NO auto-cerrar |
| Posición en DB, no en exchange | INVESTIGATE: posiblemente stop disparado, marcar trade como `closed` con `exit_source='reconciled'` |
| Orden en exchange, no en DB | Cancelar (es huérfana) |
| Orden en DB, no en exchange | Marcar como `expired_orphan` |
| Saldo cambió sin trade asociado | ALERTA CRÍTICA — posible hack o transferencia manual no registrada |

**WebSocket robusto:**

CCXT pro tiene `watch_orders`, `watch_my_trades`, `watch_balance` para algunos exchanges. Implementación correcta:
- Reconnect exponencial con jitter en disconnect
- Heartbeat tracking (si no hay ningún mensaje en 60s y el exchange dice tener actividad → forzar reconnect)
- Buffer in-memory de últimos N events para detectar duplicados tras reconnect
- Fallback a polling REST cada 30s como red de seguridad

### FASE 24 — Circuit breakers y degradation

Más allá del kill switch global, un sistema definitivo tiene **circuit breakers granulares**:

```python
class CircuitBreaker:
    def __init__(
        self,
        name: str,
        failure_threshold: int = 5,
        timeout_seconds: int = 60,
        half_open_max_calls: int = 1,
    ): ...
```

**Aplicaciones:**

1. **Per-exchange.** Si Binance falla 5 veces seguidas en `create_order` en 1 min → CB abre, todas las signals que iban a Binance se redirigen a Bybit (si soporta el par) o se descartan (si no).
2. **Per-AI-provider.** Ya tienes algo similar con la fallback chain, pero refinarlo: si Groq falla 3 veces en 5 min → skip durante 10 min antes de reintentar.
3. **Per-strategy.** Si una estrategia ha producido N signals consecutivas que perdieron todas → desactivación automática + alerta. La estrategia "pausada" requiere reactivación manual con `/strategy resume <id>`.
4. **Per-ticker.** Si un ticker concreto produce errores constantes (símbolo deslistado, datos corruptos) → blacklist temporal.

**Tabla `circuit_breaker_state`:**

```sql
circuit_breaker_state (
    id, name, state TEXT,  -- closed | open | half_open
    failure_count, last_failure_at, opened_at, last_success_at
)
```

### FASE 25 — Observability stack profesional

Lo que tienes hoy (logs JSON + tabla `source_calls` + tabla `ai_calls`) está bien para FASE 13, no para definitivo. La extensión:

**Stack mínimo:**

1. **Métricas → Prometheus.** Endpoint `/metrics` exponiendo Prometheus format. Métricas clave:
   - `mib_signals_generated_total{strategy_id, status}`
   - `mib_orders_placed_total{exchange, status}`
   - `mib_pnl_realized_eur{strategy_id, ticker}` (gauge)
   - `mib_pnl_unrealized_eur{strategy_id, ticker}` (gauge)
   - `mib_drawdown_pct{strategy_id}` (gauge)
   - `mib_active_positions{exchange}` (gauge)
   - `mib_api_latency_seconds{exchange, endpoint}` (histogram)
   - `mib_reconcile_discrepancies_found_total`
   - `mib_circuit_breaker_state{name}` (0=closed, 1=half, 2=open)
   - `mib_ai_provider_quota_used_pct{provider}`

2. **Logs → Loki o ClickHouse.** Loki si te quedas en stack Prometheus; ClickHouse si quieres queries SQL ricas sobre logs (recomendado, ClickHouse comprime brutal y es free).

3. **Traces → opcional, pero útil.** OpenTelemetry tracing en el path crítico signal → risk → executor → fill. Te permite responder "cuánto tarda exactamente desde que el scheduler genera la signal hasta que el exchange confirma fill".

4. **Dashboards → Grafana.** Tres dashboards mínimos:
   - **Operational** — health de fuentes, IA quota, reconcile status, CBs abiertos, latencias
   - **Trading** — equity curve, drawdown actual, PnL por estrategia, posiciones abiertas, fills recientes
   - **Strategy performance** — Sharpe rolling, win rate por estrategia, distribución de R-multiples, expectancy

5. **Alerting → AlertManager + Telegram.** Alertas críticas a Telegram, alertas warn a email/Slack si lo tienes.

**Recursos del BambuServer:** Prometheus + Loki + Grafana + ClickHouse no caben en 4GB. Opciones:
- Subir hardware (16GB) — la más simple
- Mover stack de obs a una VPS pequeña (Hetzner CPX11, ~5€/mes)
- Stack reducido: Prometheus + Grafana + logs en ClickHouse cloud free tier

### FASE 26 — Alta disponibilidad operacional

"HA" en un bot doméstico no significa cluster Kubernetes. Significa:

1. **Backups automáticos a sitio externo.** SQLite a S3 / Backblaze B2 / Cloudflare R2 cada 6h. Test de restore mensual obligatorio.
2. **Estado replicable.** El bot debe poder migrarse a otra máquina en <30 min con los backups + el `.env` cifrado + el repo git.
3. **Failover manual documentado.** Runbook escrito: "si BambuServer muere, sigue estos N pasos para levantar el bot en Hetzner". Probarlo dos veces antes de necesitarlo.
4. **Dead-man switch externo.** GitHub Actions con cron cada 5 min haciendo HTTP a un endpoint público minimalista del bot. Si falla 3 veces → notificación email + Telegram desde Actions.

**Endpoint público para dead-man:**

```python
@app.get("/heartbeat", include_in_schema=False)
async def heartbeat():
    """
    Endpoint público para health checks externos.
    Devuelve 200 + timestamp si el scheduler está vivo y
    el último reconcile fue hace <10 min.
    Si algo está mal, 503 con razón breve.
    """
    last_scheduler_tick = scheduler_health.last_tick
    last_reconcile = reconciler.last_run_at
    if (now - last_scheduler_tick) > 60:
        return Response(503, "scheduler stalled")
    if (now - last_reconcile) > 600:
        return Response(503, "reconcile stalled")
    return {"status": "ok", "ts": now.isoformat()}
```

Expuesto a internet vía Cloudflare Tunnel (gratis, no abre puertos en tu router). Auth: rate limit + un token simple en query param o header. NO expongas `/health` completo, solo este endpoint reducido.

---

## Parte 5 — Capa de datos avanzada (FASES 27-30)

Las fuentes actuales (yfinance, CCXT, Finnhub, FRED) son base. El bot definitivo añade:

### FASE 27 — On-chain data (cripto)

Para activos cripto, hay datos que ningún technical indicator captura: flujos hacia/desde exchanges, balances de whales, dominance metrics, network value.

**Fuentes:**

1. **Glassnode** — el estándar. API rica pero pago ($30-800/mes). Tier free muy limitado. Comprometer si haces cripto seriamente.
2. **CryptoQuant** — alternativa, similar.
3. **Dune Analytics** — queries SQL custom sobre datos on-chain. Free tier suficiente para queries no-tiempo-real.
4. **Etherscan / Blockscout / equivalentes** — gratis. Datos básicos de transacciones, contratos, holders. Útil pero requiere más procesamiento.
5. **DeFiLlama** — TVL, stablecoin metrics, free.

**Métricas valiosas:**

- **Exchange netflow** (BTC entrando/saliendo de exchanges) — netflow positivo grande = posible venta
- **Stablecoin supply en exchanges** — proxy de "polvo seco" listo para comprar
- **MVRV ratio** — valoración market vs realized; >3 históricamente marca tops
- **SOPR** — spent output profit ratio; <1 indica capitulación
- **Funding rates agregados** (cross-exchange)
- **Open interest** en derivatives

**Integración:**

```
src/mib/sources/glassnode.py        # si tienes suscripción
src/mib/sources/dune.py             # queries SQL programadas
src/mib/sources/etherscan.py        # contratos, balances
src/mib/services/onchain.py         # agrega y normaliza
```

Estas señales **no generan trades por sí solas** pero son inputs valiosos para:
- Filtros del filter classifier (FASE 21)
- Gates condicionales en RiskManager ("no abrir longs cripto si exchange netflow >X std positivo")
- Estrategias específicas (la FASE 18 #5 ya usa funding rates)

### FASE 28 — Calendar awareness (gate FASE 8.4e que aplazamos)

Eventos que cambian régimen de mercado y deben modular o bloquear trading:

**Cripto:**
- Halvings (cada 4 años en BTC)
- Network upgrades (merge de ETH, hard forks)
- Token unlocks (Vesting calendars en CryptoRank, Tokenomist)
- Regulatory hearings (SEC vs $exchange)

**Equity:**
- Earnings (Finnhub `/calendar/earnings` ya disponible)
- FOMC decisions (FRED, calendar Investing.com)
- Economic releases: CPI, NFP, PMI, retail sales
- Index rebalancing

**Forex:**
- Central bank decisions (BOJ, BOE, ECB, Fed, RBA)
- Major economic releases por divisa

**Implementación:**

```python
class CalendarService:
    async def upcoming_events(
        self,
        within: timedelta,
        for_ticker: str | None = None,
    ) -> list[CalendarEvent]:
        """Eventos próximos relevantes para un ticker o globales."""

    async def is_blackout(self, ticker: str, lookhead: timedelta = timedelta(hours=24)) -> bool:
        """¿Hay algún evento en blackout window que afecta este ticker?"""
```

**Gates:**
- `EarningsBlackoutGate` — no abrir nuevas posiciones en stock individual con earnings en <48h.
- `FOMCBlackoutGate` — no abrir posiciones nuevas globales en las 4h previas a anuncio FOMC.
- `MajorReleaseGate` — para forex y commodities, blackout 30min antes de release relevante.

Nota crítica: blackout solo aplica a **apertura** de nuevas posiciones. Las posiciones abiertas siguen con sus stops nativos. Bloquear el cierre durante eventos sería peligroso.

### FASE 29 — Alternative data

Datos no-tradicionales con potencial alpha:

1. **Sentiment de redes sociales:** Twitter/X (cripto-twitter es un feed riquísimo de hype/capitulación), Reddit r/wallstreetbets, Telegram channels.
   - Stack: API de X (Basic plan ~$200/mes; alternativa: scraping con `snscrape` mientras siga funcionando)
   - Procesamiento: `sentence-transformers` para embeddings + clustering + sentiment

2. **Google Trends** — `pytrends` library, gratis. Útil para detectar interés retail en altcoins (suele preceder pump).

3. **Order book microstructure** (depth + flow):
   - WebSocket de book updates en tiempo real
   - Métricas: bid-ask spread evolution, book imbalance, large orders insertion/cancellation
   - Detecta spoofing y manipulación

4. **GitHub activity** (cripto):
   - Commit frequency en repos del proyecto del token → proxy de health
   - APIs gratis vía GitHub REST.

5. **Job postings** (equity):
   - LinkedIn / Indeed scraping → growth proxy de la empresa

**Disclaimer interno:** alternative data es muy ruidosa. Útil como input de modelo (FASE 21), peligrosa como señal directa.

### FASE 30 — Data lake propio

Cuando lleves 12+ meses operando, vas a tener TBs de tick data, fills, signals históricas. SQLite no escala más allá de ~100GB efectivos.

**Migración path:**

1. SQLite → PostgreSQL (FASE 30.1) — todavía monolítico pero relacional serio
2. Data caliente en PostgreSQL + data fría en Parquet/Iceberg sobre S3 (FASE 30.2)
3. ClickHouse para analytics OLAP (FASE 30.3) — queries de agregación 100x más rápidas

**Esto es trabajo serio, posiblemente 6-12 meses tras LIVE estable.** No lo afrontes prematuramente.

---

## Parte 6 — Compliance, fiscal y legal (FASES 31-33)

Esta parte se ignora con frecuencia y tiene consecuencias reales en España.

### FASE 31 — Tax accounting automatizado

Hacienda en España exige:

**Para cripto:**
- **Modelo 721** — declaración informativa anual de saldos en exchanges extranjeros >50.000€ (a 31-dic). Plazo: enero-marzo del año siguiente.
- **Modelo 100** (IRPF) — ganancias/pérdidas patrimoniales por venta de cripto. Tipo según importe (19-28% en 2024, escalado).
- **Método de cálculo:** FIFO obligatorio (no LIFO ni media ponderada).
- **Permutas:** intercambiar BTC por ETH es una venta. Tributable aunque no toques fiat.

**Para equity:**
- Si broker es español: te lo informa al modelo 100 directamente.
- Si broker extranjero (IB con cuenta no española): tú declaras manualmente. Ganancias patrimoniales mismo tipo.
- **Modelo 720** — bienes en el extranjero (incluye broker IB si saldo >50k).

**Implementación:**

```
src/mib/tax/
├── fifo_calculator.py     # FIFO matching de compras vs. ventas
├── exchange_rate.py       # ECB rates históricos para conversión a EUR
├── reports/
│   ├── modelo_721.py      # Generador CSV/PDF para gestor
│   ├── modelo_100.py      # Ídem
│   └── annual_summary.py  # Resumen P&L anual
```

**Fuentes de datos:**
- Trades del bot: tabla `trades`
- Trades manuales (los que hagas fuera del bot): import desde CSV del exchange
- Tipos de cambio: ECB historical rates (gratis vía API o CSV)

**Output esperado:**
- CSV listo para gestor con todas las operaciones del año
- PDF con resumen de P&L por activo y total
- JSON estructurado por si Hacienda lo pide

**Plazo:** implementarlo **ANTES** de que pases capital significativo (>10k€) a LIVE. Reconstruir un año fiscal a posteriori sin esto es un infierno.

### FASE 32 — Disclaimer y limitación legal

Si el bot es solo para uso personal-whitelist, los disclaimers actuales en prompts cubren. Si en algún momento abres acceso a terceros (familia, amigos, comunidad), entras en territorio regulatorio:

- **CNMV en España** considera "asesoramiento financiero" cualquier recomendación personalizada. Genérica + bien disclaimerizada queda fuera.
- **MiCA UE** (en vigor completo desde diciembre 2024) regula servicios sobre cripto. Sistemas que ejecutan órdenes por cuenta de terceros pueden requerir licencia.
- **Si solo eres tú**, cero problema legal con el bot.
- **Si hay un segundo usuario**, abogado obligatorio antes de avanzar.

Mi recomendación práctica: **manten el bot como uso personal único**. Es 100x menos complicado que cualquier alternativa.

### FASE 33 — Auditabilidad regulatoria

Aunque sea uso personal, llevar logs auditables:

- Toda decisión con timestamp, inputs, outputs, razón
- Inmutabilidad: tablas `signals`, `trades`, `risk_decisions`, `orders` no permiten UPDATE (solo append). Cambios de status van a tabla histórica.
- Retención: mínimo 7 años (obligación fiscal española)
- Backups firmados (hash del backup + timestamp en otro sitio independiente)

---

## Parte 7 — Performance e infraestructura (FASES 34-36)

### FASE 34 — Optimización de hot paths

El primer año tu cuello de botella no es performance, es lógica. A partir del año 2 con multi-strategy + multi-exchange + reconciliation continuo + optimización + ML, sí lo será. Targets:

- Signal generation end-to-end: <500ms p99 (desde candle close hasta Signal persistida)
- Order placement: <200ms p99 (desde RiskDecision approve hasta exchange confirm)
- Reconciliation full sweep: <30s p99

**Técnicas:**

1. **Connection pooling** en httpx para data sources frecuentes (Binance, IB, Glassnode).
2. **Async I/O bien hecho** — perfilar con `py-spy` para detectar bloqueos. Pandas-ta es síncrono; envolver en `asyncio.to_thread` para no bloquear loop.
3. **Caching agresivo de OHLCV** con TTL alineado a candle close (no recomputar indicadores en cada call si la vela no cambió).
4. **Polars para feature engineering** y backtests si pandas se queda corto.
5. **C extensions** para indicadores hot-path (TA-Lib en C es 10x más rápido que pandas-ta puro).

### FASE 35 — Hardware planning

BambuServer 4GB se queda corto cuando:
- Tienes 8+ estrategias activas simultáneas
- Multi-exchange con 3+ traders
- Reconciliation continuo
- Stack de observabilidad
- ML inference local

**Opciones:**

| Opción | Coste | Pros | Contras |
|--------|-------|------|---------|
| Upgrade BambuServer a 16-32GB RAM | 50-200€ una vez | Sigues self-hosted, control total | Sigue habiendo SPOF de tu casa |
| VPS dedicado (Hetzner CCX13, 8GB) | ~25€/mes | Fuera de SPOF doméstico, latencia mejor a exchanges | Coste mensual, dependencia proveedor |
| Híbrido: bot core en VPS, observabilidad en BambuServer | ~25€/mes | Trading independiente del hogar; obs barata | Más complejo |

**Mi recomendación cuando pases LIVE:** híbrido. Trading core en Hetzner VPS (latencia <50ms a Binance EU); observability + backups + dev environment en BambuServer.

### FASE 36 — Latencia geográfica

Para estrategias intraday <1 min, la latencia importa:

- AWS / OVH / Hetzner data centers próximos al matching engine del exchange
- Binance: matching en AWS Tokyo (asia.binance.com) y AWS Frankfurt (api.binance.com)
- IB: Stamford, CT (USA)
- Bybit: AWS Singapore

Co-locar el bot en el mismo región del exchange reduce latencia de ~150ms a ~5-10ms. **Solo merece la pena si haces estrategias <1 min holding period**. Para holding >15min es over-engineering.

---

## Parte 8 — Seguridad de grado producción (FASES 37-39)

Donde hay dinero, hay atacantes. Un bot de trading bien posicionado (cuenta exchange con permisos de orden) es un objetivo atractivo.

### FASE 37 — API key hygiene avanzada

**Reglas duras:**

1. **Permisos mínimos por key.** En Binance:
   - Read-only key (datos de mercado): sin trade, sin withdrawal
   - Trade key (CCXTTrader): trade ON, withdrawal OFF, futures opcional según FASE 17
   - **NUNCA habilitar withdrawal en una key del bot.** Withdrawal solo desde la app/web manualmente.

2. **IP whitelist en exchange.** La trade key solo acepta requests desde la IP fija de tu VPS. Si la cambias, primero actualizas en Binance, luego despliegas. Esto bloquea casi todos los ataques remotos incluso si la key se filtra.

3. **Rotación trimestral.** Cron job recordatorio cada 90 días: rota keys, actualiza vault, redeploy.

4. **Una cuenta exchange dedicada al bot.** Tu cuenta personal con holdings largos NO es la cuenta del bot. Cuenta separada con solo el capital operativo. Si compromiso, pierdes operativo, no holdings.

### FASE 38 — Vault encriptado para secrets

`.env` en disco plano no está mal mientras nadie acceda al server, pero no escala:

**Opciones:**

1. **HashiCorp Vault** (overkill para uno solo, pero correcto)
2. **Mozilla SOPS + age** (gratis, simple, suficiente)
3. **Doppler / Infisical** (SaaS, free tiers existen)

**Stack recomendado: SOPS + age.**

```bash
# Generar clave age
age-keygen -o ~/.age/key.txt

# Encriptar .env
sops -e -i .env

# El bot al arranque:
# 1. Lee la clave age de un keyring (no del filesystem)
# 2. Desencripta .env en memoria
# 3. NUNCA escribe el .env desencriptado a disco
```

Beneficio: el `.env.encrypted` puede vivir en git (privado). Backups y migraciones triviales. Solo el agente con la clave age desencripta.

### FASE 39 — Defensa en profundidad

1. **Bot en contenedor con read-only filesystem** excepto `/data` (DB) y `/tmp`.
2. **Sin shell ni curl/wget en imagen final** (multi-stage build limpia esto).
3. **Network policy estricta:** bot solo puede salir a IPs whitelisted (exchanges, Telegram, AI providers, data sources). Bloqueo egress a todo lo demás. Iptables o firewall del proveedor.
4. **Auditoría de dependencias.** `pip-audit` semanalmente en CI. `safety check` también.
5. **SBOM (Software Bill of Materials)** generado por release. Útil cuando aparece CVE en una transitive dependency.
6. **Monitoring de comportamiento anómalo.**
   - Outbound a IP no whitelisted → alerta crítica
   - Modificación de `/etc/passwd` o keys → alerta
   - Proceso desconocido en el container → alerta

   `falco` en host hace esto. Overkill para etapa temprana, justificado en FASE 14+.

7. **2FA en todo:** GitHub, exchange, AI providers, VPS provider, dominio, email.

---

## Parte 9 — UX, observabilidad y productización (FASES 40-42)

### FASE 40 — Telegram bot avanzado

Tu Telegram actual cubre comandos básicos. Definitivo:

1. **Comandos contextuales con buttons inline en cada mensaje.** Cada signal recibida lleva 4-6 botones: aprobar / rechazar / modificar size / ver chart / ver indicadores / ver razonamiento IA.

2. **Conversational flow.** Estados con `ConversationHandler`:
   ```
   /strategy create
   ↓
   "¿Qué estrategia base? [breakout] [mean_rev] [custom]"
   ↓ usuario tap "breakout"
   "¿Universo de tickers? Envía CSV"
   ↓ usuario envía "BTC/USDT,ETH/USDT,SOL/USDT"
   "Confirma: nueva estrategia 'breakout.user_v1' sobre 3 tickers? [✅] [❌]"
   ```

3. **Reportes programados.**
   - Daily report (08:00 ES): equity, PnL D-1, posiciones abiertas, eventos del día
   - Weekly report (lunes 08:00): performance por estrategia, win rate, expectancy, próxima semana
   - Monthly report: P&L mensual, drawdowns, mejor/peor trade, estado fiscal estimado

4. **Voice notes para alertas críticas.** Mensaje de voz generado con TTS local (`pyttsx3`) para que en móvil suene cuando hay drawdown crítico. Capta atención mejor que texto.

5. **Inline queries.** En cualquier chat de Telegram puedes escribir `@MIBbot AAPL` y obtener una mini-card. Útil para consulta rápida sin abrir el chat del bot.

### FASE 41 — Web UI mínimo

Telegram es excelente para alertas y comandos rápidos pero malo para análisis. Una web UI mínima cubre lo otro:

**Stack:** FastAPI ya tienes, monta SPA estática (React, o más simple: HTMX + Tailwind).

**Páginas necesarias:**

1. **Dashboard** — equity curve interactiva, P&L del día, posiciones abiertas
2. **Strategies** — lista, performance histórica, switches activar/desactivar, edición de parámetros
3. **Signals** — feed reciente con filtros (estrategia, status, ticker)
4. **Trades** — tabla con filtros, drilldown a fills, P&L
5. **Risk** — gates state, kill switch, exposure breakdown
6. **Backtester** — UI para correr backtest interactivo, ver métricas, comparar versiones

**Acceso:** Cloudflare Tunnel + Cloudflare Access (free tier) para auth Google. **NO expongas Telegram bot token, credenciales exchange ni la app entera a internet** sin auth.

### FASE 42 — Mobile app companion (opcional)

Realmente innecesario si Telegram + web UI cubren. Solo si te apetece un proyecto paralelo. Saltable.

---

## Parte 10 — Disciplina de proceso (no es código pero define éxito o fracaso)

Esta es la parte que más bots caseros ignoran y donde se decide si tu bot funciona en 3 años o muere.

### Versionado riguroso

- **Toda estrategia es `<family>.<name>.v<n>`.** Cuando cambies el algoritmo, incrementas N. La v anterior no se borra; sigue en histórico para análisis.
- **Modelos ML versionados** con checkpoint persistido + metadata (fecha entrenamiento, dataset usado, hyperparams).
- **Migraciones DB siempre forward-compatible.** Nunca DROP COLUMN; siempre añadir nuevo campo y deprecar el viejo en N versiones.
- **Tags de release semver.** `v1.0.0` para FASE 14 LIVE inicial. Bumps mayor en breaking changes (cambio de schema risk). Bumps menor en features. Patch en bugfixes.

### Diario de operación

Tabla mental: 5 minutos al día revisando:

1. ¿Hubo alguna alerta crítica? Si sí, ¿se resolvió?
2. ¿El reconcile reportó alguna discrepancia? Si sí, ¿qué fue?
3. ¿Alguna estrategia ha entrado en circuit breaker?
4. ¿La equity curve muestra algo raro vs. esperado?

Esto es lo único que cuesta tiempo continuo y es no negociable.

### Reviews periódicas

- **Semanal (30 min):** P&L por estrategia, win rate, gates disparados, ajustes propuestos.
- **Mensual (2h):** revisión profunda. ¿Las estrategias mantienen edge? ¿El régimen ha cambiado? ¿Hay correlación inesperada entre estrategias? ¿Hay drift en parámetros? Decisiones: pausar, ajustar, retirar, escalar capital.
- **Trimestral (1 día completo):** revisión arquitectónica. ¿Hay deuda técnica acumulada? ¿Algo merece refactor? Roadmap del próximo trimestre.

### Backup y disaster recovery

**Backups:**

- DB SQLite: cada 6h a S3/B2/R2 con encryption at rest
- Configs (YAML, .env encriptado): versionados en git privado
- Modelos ML: cada nueva versión a object storage
- Logs históricos: rotación a object storage, retención 7 años

**DR drill cada 6 meses:**

1. Simular: "BambuServer está muerto"
2. Levantar el bot en VPS limpia desde cero
3. Restaurar último backup
4. Verificar reconcile contra exchange
5. Cronometrar el proceso. Target: <30 min para tener bot operativo en modo SHADOW.

Si cronometras y supera 1h, hay algo en el proceso que automatizar.

### Documentación viva

`PROJECT.md` es el spec base; este documento es el roadmap; necesitas tres más:

1. **`OPERATIONS.md`** — runbooks. Qué hacer si X falla. Cómo rotar keys. Cómo añadir un exchange nuevo. Cómo hacer un release.
2. **`STRATEGIES.md`** — catálogo de estrategias activas. Cada una con: descripción, hipótesis, parámetros, métricas históricas, condiciones de pausa.
3. **`POSTMORTEMS.md`** — incidentes. Cada vez que algo se rompe, escribir 5 párrafos: qué pasó, impacto, causa raíz, fix, prevención. Sin culpa, sólo aprendizaje.

### Reglas de capital

Una vez en LIVE, regla de 5 niveles que NO se rompen:

| Nivel | Capital | Trigger para escalar |
|-------|---------|----------------------|
| 1 | 200€ | 60 días sin incidente operacional |
| 2 | 1.000€ | 90 días con Sharpe >1.0 a nivel portfolio |
| 3 | 5.000€ | 180 días con max drawdown <15% |
| 4 | 25.000€ | 365 días con Sharpe >1.0 y max DD <20% |
| 5 | >25.000€ | Decisión consciente con asesor / abogado / fiscalista |

**Cualquier drawdown >25% activa cool-down obligatorio:** pausa de 30 días en LIVE, vuelta a SHADOW para reconfirmar comportamiento.

### Salida controlada

Plan documentado para apagar el bot:

1. Comando `/wind_down` que: deja de generar signals nuevas, deja todas las posiciones abiertas con sus stops nativos, espera a que los stops o targets cierren naturalmente, alerta cuando todas las posiciones estén flat.
2. Una vez flat: `/shutdown` que para servicios y emite reporte final.
3. Tiempo esperado wind-down: dependiente de timeframe de estrategias activas, típicamente 1-7 días.

---

## Apéndice A — Tabla maestra de fases con prioridad y dependencias

| Fase | Nombre | Prioridad | Bloqueada por | Capacidad esperada | Estado |
|------|--------|-----------|---------------|---------------------|--------|
| 1-7 | Motor analítico + Signal layer | — | — | — | ✅ Completada |
| 8 | Risk management | CRÍTICA | 7 | — | Próxima |
| 9 | Order Executor | CRÍTICA | 8 | — | Pendiente |
| 10 | Modos graduales | CRÍTICA | 9 | — | Pendiente |
| 11 | AI Validator + Postmortem | ALTA | 10 | +200 LLM calls/día | Pendiente |
| 12 | Backtester | ALTA | 8 | +disco para datos históricos | Pendiente |
| 13 | Observabilidad básica + dead-man | CRÍTICA | 10 | — | Pendiente |
| 14 | LIVE capital simbólico | CRÍTICA | 11, 12, 13 | <200€ | Pendiente |
| 15 | Equity/forex via IB | MEDIA | 14 estable 60d | +CPU thread dedicado | Roadmap |
| 16 | Multi-exchange cripto | MEDIA | 14 estable 60d | +RAM 200MB por trader | Roadmap |
| 17 | Derivatives (perps) | BAJA | 14 + 16 | Capital adicional para margin | Opcional |
| 18 | Librería estrategias profesionales | ALTA continua | 12 | Trabajo continuo | Roadmap |
| 19 | Smart Order Routing | MEDIA | 16 | — | Roadmap |
| 20 | Walk-forward optimization | ALTA | 12 | +CPU intensivo | Roadmap |
| 21 | Machine Learning | MEDIA | 18 con n>500 signals | +RAM 500MB para inference | Roadmap |
| 22 | Estrategias adaptativas | MEDIA | 21 | — | Roadmap |
| 23 | Reconciliation continuo | CRÍTICA | 14 | — | Roadmap |
| 24 | Circuit breakers granulares | ALTA | 13 | — | Roadmap |
| 25 | Observability stack pro | ALTA | 14 | +RAM ~1GB | Roadmap |
| 26 | HA operacional | ALTA | 14 | +VPS o disco backup | Roadmap |
| 27 | On-chain data | MEDIA | 18 | +Glassnode subs (opcional) | Roadmap |
| 28 | Calendar awareness | ALTA | 8 estable | — | Roadmap |
| 29 | Alternative data | BAJA | 21 | +costes API X opcional | Roadmap |
| 30 | Data lake propio | BAJA | 12+ meses LIVE | +infra DB seria | Roadmap |
| 31 | Tax accounting | CRÍTICA pre-capital >10k€ | — | — | Roadmap |
| 32 | Disclaimer/legal | DEPENDE | Si hay 2do usuario | — | Condicional |
| 33 | Auditabilidad | ALTA | — | — | Roadmap |
| 34 | Performance optimization | MEDIA | 18 + 25 | — | Roadmap |
| 35 | Hardware planning | ALTA | 14 | +50-300€/mes | Roadmap |
| 36 | Latencia geográfica | BAJA | Solo si hay estrategias <1min | — | Roadmap |
| 37 | API key hygiene | CRÍTICA | 9 | — | Roadmap |
| 38 | Vault encriptado | ALTA | 14 | — | Roadmap |
| 39 | Defensa en profundidad | ALTA | 14 | — | Roadmap |
| 40 | Telegram avanzado | MEDIA | 14 | — | Roadmap |
| 41 | Web UI mínimo | MEDIA | 14 | — | Roadmap |
| 42 | Mobile app | BAJA | Opcional | — | Skip recomendado |

**Path crítico hasta LIVE estable:** 8 → 9 → 10 → 11 → 12 → 13 → 14 → 23 → 14-estable → 28 → 31 → 37 → 25 → 38

**Path crítico para "definitivo":** Path-LIVE + 18 (continuo) + 16 + 15 + 24 + 26 + 39 + 40 + 41

**Estimación temporal honesta:**

| Hito | Duración |
|------|----------|
| FASES 8-10 | 4-6 semanas |
| FASES 11-13 | 4 semanas |
| FASE 14 + estabilización | 2 meses (mucho de eso es esperar) |
| Path crítico LIVE estable completo | **5-7 meses calendario** desde hoy |
| Bot definitivo Parts 2-9 completas | **24-36 meses calendario** desde hoy |

Y eso asumiendo que mantienes la cadencia. Realidad: nadie la mantiene. Ajusta expectativas.

### Definición operativa de incidente crítico

La regla "60 días sin incidente operacional crítico" usada en las reglas de capital (Parte 10) requiere una definición precisa, no interpretativa. La métrica `mib_critical_incident_total` se incrementa solo bajo los siguientes 7 tipos:

| Type enum | Disparador |
|-----------|------------|
| `RECONCILE_ORPHAN_UNRESOLVED` | Reconcile detecta posición/orden huérfana sin auto-resolver |
| `BALANCE_DISCREPANCY` | Discrepancia de saldo no atribuible a un trade registrado |
| `CIRCUIT_BREAKER_PROLONGED` | Circuit breaker abierto >15 min en cualquier exchange |
| `NATIVE_STOP_MISSING_AFTER_FILL` | Stop-loss nativo no presente en exchange tras un fill confirmado |
| `KILL_SWITCH_DD_DAILY` | Kill switch activado por drawdown diario |
| `MANUAL_INTERVENTION_REQUIRED` | Operador emite `/incident <razón>` (registro manual) |
| `RECONCILE_FAILED_PROLONGED` | Reconcile no puede ejecutarse durante >30 min (operando a ciegas) |

**Regla del contador `days_clean_streak()`:**

```python
def days_clean_streak() -> int:
    """Días desde el último incidente que reseteó el streak.

    Reset triggers (cualquiera de los dos):
    1. Cualquier incidente con resolved_at - occurred_at > 24h
    2. Cualquier incidente de tipo BALANCE_DISCREPANCY o RECONCILE_ORPHAN_UNRESOLVED
       (estos resetean siempre, sea cual sea el tiempo de resolución)
    """
```

**Surface:**
- Tabla `critical_incidents` (id, type, occurred_at, resolved_at, auto_detected, severity, context_json, resolution_notes)
- Métrica Prometheus `mib_critical_incident_total{type}` (counter)
- Métrica Prometheus `mib_days_clean_streak` (gauge)
- Endpoint `/incidents` (lista incidentes recientes con filtros)
- Comando Telegram `/incident <type> <reason>` para registro manual
- `/status` Telegram responde "días limpios: N, próximo escalado posible: M días"

**Regla de escalado:** N→N+1 requiere `days_clean_streak() >= 60`. Implementación entra en FASE 13 (observabilidad básica). Sin esta métrica explícita, las reglas de capital son fe, no proceso.

---

## Apéndice B — Decisiones pendientes que necesitan respuesta antes de FASE N+1

Lista viva de decisiones que tendrás que tomar; documenta cada una cuando la tomes.

| # | Decisión | Fase que la requiere | Estado |
|---|----------|---------------------|--------|
| 1 | Universo final de tickers para scanner | FASE 8.1 (al activar scheduler) | ⏳ Plantilla propuesta |
| 2 | Capital total a destinar al bot (target 12 meses) | FASE 14 antes de LIVE | ❓ |
| 3 | Riesgo por trade % (default 0.5%) | FASE 8.5 | ⏳ Default propuesto |
| 4 | Drawdown diario máximo % (default 3%) | FASE 8.3 | ⏳ Default propuesto |
| 5 | Drawdown total máximo % antes de cool-down (default 25%) | FASE 14 | ⏳ Default propuesto |
| 6 | Glassnode sí/no | FASE 27 | ❓ Depende de presupuesto |
| 7 | IB Lite vs Pro | FASE 15 | ❓ Depende de volumen |
| 8 | VPS Hetzner sí/no | FASE 35 | ❓ Depende de progreso FASE 14 |
| 9 | Stack observabilidad: Prom+Loki+Grafana vs ClickHouse | FASE 25 | ❓ |
| 10 | ¿Se admite leverage en cripto? Si sí, max | FASE 17 | ⏳ Recomendado: máx 2x |
| 11 | ¿Multi-account o single account exchange? | FASE 16 | ⏳ Recomendado: 2 cuentas, 1 trading 1 holdings |
| 12 | Política de impuestos: gestor o solo? | FASE 31 | ❓ |
| 13 | ¿Acceso a terceros al bot, sí/no? | FASE 32 | ⚠️ Recomendado: NO |
| 14 | Frecuencia rotación API keys (default 90d) | FASE 37 | ⏳ Default propuesto |

---

## Apéndice C — Anti-patrones documentados (qué NO hacer)

Lecciones acumuladas de bots que han fracasado:

1. **Optimizar todo el histórico y desplegar.** Curve-fitting puro. Walk-forward o nada.
2. **Aumentar capital tras una racha buena.** La racha buena es estadística, no señal de que el bot mejoró. Sigue las reglas de escalado del Apéndice A.
3. **Cambiar parámetros tras una racha mala.** Igual: ruido, no señal. Solo ajustas si la causa raíz es identificable y reproducible.
4. **Añadir estrategias sin retirar las que no funcionan.** Cada estrategia tiene coste mental + computacional. Las que llevan 90d sin alpha demostrada se retiran.
5. **Hacer trading manual con la cuenta del bot.** Contamina la P&L, rompe reconciliation, descalibra métricas. Si quieres tradear manual, otra cuenta.
6. **Saltarse modos.** SHADOW → PAPER → SEMI_AUTO → LIVE existe por una razón. Saltar de SHADOW a LIVE porque "ya he visto que funciona" es suicidio estadístico.
7. **Obviar reconciliación.** Vas a tener fantasmas y huérfanos. La reconciliación los caza temprano; sin ella se acumulan en silencio.
8. **Confiar en un solo exchange.** Binance ha tenido outages, problemas regulatorios, restricciones de retiro. La diversificación de venue es defensiva, no oportunista.
9. **Escribir estrategias propias antes de tener backtester.** Sin medir, no sabes si tienen edge. Estás adivinando.
10. **Ignorar el componente psicológico.** Aunque el bot opera autónomo, las decisiones de "pausar" / "pivotar" / "escalar" las tomas tú. Cuando vas perdiendo, vas a tener el impulso de pausar precisamente en el peor momento (= drawdown que precede al recovery). Reglas escritas + revisión semanal te protegen de tu yo emocional.
11. **No hacer tax accounting hasta el último día.** Reconstruir un año de FIFO con cientos de trades a posteriori es brutal. Hazlo continuo desde el día 1 de LIVE.
12. **Permitir withdrawal en API key.** No hay razón legítima. Si una key con withdrawal se filtra, te vacían la cuenta antes de que te enteres.
13. **Stops solo en el bot, no en el exchange.** Si el bot muere a las 3am y el precio se cae, sin stop nativo en exchange te despiertas con pérdida brutal.
14. **Logs sin retention policy.** Discos llenos paran el bot. Rotación + archivado a object storage desde día 1.
15. **Olvidar que esto es un proyecto de software.** Tests, lint, mypy, code review (aunque sea contigo mismo en PR), commits atómicos, branches por feature. El día que esto se descuida, el bot deja de ser mantenible.

---

## Cierre

Este documento describe **3-5 años de trabajo serio**. No es un plan para acabar; es un horizonte para guiarte. Lo importante es:

- **Avanzar en el path crítico hasta LIVE estable** (FASES 8-14 + 23 + 28 + 31 + 37). 6-7 meses bien invertidos.
- **Pasar 60-90 días en cada modo** antes de avanzar al siguiente.
- **No perseguir features sin haber estabilizado lo que ya tienes.**
- **Mantener disciplina** de proceso: commits atómicos, tests verdes, reconciliación continua, reviews periódicas.

El bot definitivo no es uno con todo lo descrito aquí. Es el que **ejecuta consistentemente, sobrevive a regímenes adversos, y mejora con cada release**. Mucho de lo descrito es opcional, condicional, o saltable según tu tolerancia a coste/complejidad.

Lee este documento dos veces al año. Marca lo hecho. Re-prioriza. Algunas fases las descartarás cuando llegues. Otras se redefinirán. Es un mapa, no una receta.

Adelante con FASE 8 cuando venga.
