# Market Intelligence Bot (MIB) — Especificación Completa

> **Para Claude Code**: este documento es la fuente única de verdad del proyecto.
> Léelo entero antes de escribir código. Si algo no está aquí, **pregunta**, no asumas.
> Al final de cada FASE, para y espera confirmación antes de avanzar.

---

## 0. Contexto y objetivo

Construir un **bot de inteligencia financiera** self-hosted que:

- Agrega datos de múltiples fuentes gratuitas (cripto, stocks, forex, macro, noticias).
- Calcula indicadores técnicos localmente con `pandas-ta`.
- Usa LLMs vía free tiers (Groq + OpenRouter + Gemini) para análisis cualitativo, con fallback automático entre providers.
- Se consulta y envía alertas vía Telegram.
- Expone API REST local (FastAPI) para uso programático.
- **Coste operativo: 0 €.** Cualquier dependencia de pago queda rechazada.

El bot corre 24/7 en servidor casero Linux (specs detalladas en sección 11bis) bajo Docker. **No hace trading real** — solo análisis, alertas y respuestas a consultas. Ejecutar órdenes queda **explícitamente fuera de alcance**.

---

## 1. Principios de diseño (no negociables)

1. **Graceful degradation**: si una fuente cae o agota cuota, el bot sigue funcionando con el resto.
2. **Cache agresivo**: nunca llamar dos veces a la misma fuente dentro del TTL. SQLite como cache persistente.
3. **Rate limiting consciente**: cada fuente y cada LLM tiene su propio limiter con backoff exponencial y respeto de headers `Retry-After`.
4. **Seguridad por defecto**: secrets solo en `.env`, whitelist de usuarios Telegram, cero endpoints expuestos a internet (salvo polling outbound a Telegram).
5. **Observabilidad first-class**: logs JSON estructurados, métricas de uso por fuente/LLM, errores trazables.
6. **Testeable**: lógica de negocio separada de IO. Mocks para fuentes externas. Cobertura mínima 70 % en módulos core.
7. **12-factor app**: config vía entorno, logs a stdout, stateless salvo SQLite.
8. **Async end-to-end**: `asyncio` + `httpx` + `python-telegram-bot` v21+ async.
9. **RAM-aware**: el host tiene solo 4 GB compartidos. El bot debe funcionar cómodamente dentro de 512 MB.
10. **Hot-reload de config**: RSS feeds, símbolos por defecto y scanner presets se leen desde YAML y se pueden recargar sin reiniciar.

---

## 2. Stack técnico

### Core

- **Python 3.12** (pinned en Dockerfile y `pyproject.toml` via `requires-python`)
- **uv** como gestor de paquetes y virtualenv
- **FastAPI** + **uvicorn** (API REST local, 1 worker async)
- **Pydantic v2** + **pydantic-settings** (validación y config)
- **SQLAlchemy 2.0 async** + **aiosqlite** (cache + persistencia)
- **Alembic** (migraciones DB)
- **APScheduler** (jobs periódicos async)
- **httpx** (HTTP async) con pool de conexiones limitado
- **tenacity** (retry con backoff exponencial + jitter)
- **loguru** (logging estructurado JSON a stdout)

### Datos financieros

- **ccxt** async (cripto, 100+ exchanges)
- **yfinance** (stocks, ETFs, forex, índices — sin key)
- **pandas** + **pandas-ta** (indicadores técnicos)
- **tradingview-ta** (technical ratings de TradingView — sin key)
- **feedparser** (RSS de noticias — sin key)

### IA

- **groq** (cliente oficial)
- **openai** (cliente compatible para OpenRouter via base_url override)
- **google-generativeai** (Gemini directo)

### Telegram

- **python-telegram-bot** v21+ (async, modo polling)
- **mplfinance** (gráficos de velas)
- **Pillow** (procesamiento imágenes, ya viene con mplfinance)

### Testing & calidad

- **pytest** + **pytest-asyncio** + **pytest-mock**
- **respx** (mock de httpx)
- **ruff** (lint + format, reemplaza black+isort+flake8)
- **mypy** (type checking, strict en `src/mib/ai/`, `src/mib/sources/base.py` y `src/mib/services/`)
- **pre-commit** hooks (ruff check, ruff format, mypy, detect-secrets)

### Deploy

- **Docker** + **docker-compose** (imagen base `python:3.12-slim`)
- Multi-stage build para imagen final ligera (<200 MB)

---

## 3. Arquitectura

```
┌─────────────────────────────────────────────────────────────┐
│                      TELEGRAM BOT                           │
│         (comandos + alertas push + consultas natural)       │
└───────────────────┬─────────────────────────────────────────┘
                    │
┌───────────────────┴─────────────────────────────────────────┐
│                     FASTAPI (127.0.0.1:8000)                │
│              (routers: symbol, scan, news, macro, ask)      │
└───────────────────┬─────────────────────────────────────────┘
                    │
         ┌──────────┴──────────┐
         │                     │
┌────────▼────────┐   ┌────────▼────────┐
│   CORE SERVICES │   │    AI ROUTER    │
│  (orquestación) │   │ (Groq/OR/Gemini)│
└────────┬────────┘   └─────────────────┘
         │
┌────────▼──────────────────────────────────────┐
│              DATA SOURCES LAYER                │
│ ┌──────┬─────────┬──────────┬──────┬────────┐ │
│ │ CCXT │yfinance │CoinGecko │ FRED │Finnhub │ │
│ ├──────┼─────────┼──────────┼──────┼────────┤ │
│ │Alpha │  TV-TA  │   RSS    │      │        │ │
│ │Vant. │         │          │      │        │ │
│ └──────┴─────────┴──────────┴──────┴────────┘ │
└────────┬──────────────────────────────────────┘
         │
┌────────▼────────┐
│  CACHE (SQLite) │
└─────────────────┘
```

---

## 4. Fuentes de datos — especificación detallada

Cada fuente implementa la ABC `DataSource` con métodos async, rate limiter propio y devuelve modelos Pydantic tipados.

| Fuente         | API Key  | Free Tier                   | Uso                          | TTL cache |
|----------------|----------|-----------------------------|------------------------------|-----------|
| CCXT           | No       | Sin límite (endpoints pub.) | Precio cripto, OHLCV, ordbk  | 30 s      |
| yfinance       | No       | Sin límite documentado      | Stocks, ETF, forex, índices  | 60 s      |
| CoinGecko      | Opcional | 10-30 calls/min             | Market cap, trending, cat.   | 2 min     |
| Alpha Vantage  | Sí       | 25 req/día, 5/min           | Fundamentals empresas        | 24 h      |
| Finnhub        | Sí       | 60 calls/min                | Noticias + sentiment         | 5 min     |
| FRED           | Sí       | Ilimitado (razonable)       | Macro (tasas, CPI, empleo)   | 6 h       |
| tradingview-ta | No       | Sin límite documentado      | Technical ratings TV         | 5 min     |
| RSS feeds      | No       | Ilimitado                   | Reuters, CoinDesk, etc.      | 10 min    |

### RSS por defecto (`config/rss_feeds.yaml`)

- Reuters Business, Reuters Markets
- CoinDesk, CoinTelegraph, Decrypt
- MarketWatch Top Stories
- SEC EDGAR filings (Atom feed)
- Investing.com Economic Calendar (opcional)

### Símbolos por defecto (`config/default_symbols.yaml`)

- **Cripto**: BTC/USDT, ETH/USDT, SOL/USDT
- **Stocks**: SPY, QQQ, AAPL, MSFT, NVDA, TSLA
- **Índices**: ^GSPC (S&P500), ^VIX, ^DXY, ^TNX (10Y yield)
- **Forex**: EURUSD=X, USDJPY=X
- **Commodities**: GC=F (oro), CL=F (petróleo)

### Scanner presets (`config/scanner_presets.yaml`)

- `oversold`: RSI(14) < 30 en 1h, volumen > media 20
- `breakout`: precio > EMA50 y EMA20 cruzando al alza EMA50 en 4h
- `trending`: ADX(14) > 25 y MACD histograma positivo en 1d

---

## 5. Capa de IA — multi-provider free

### Proveedores y modelos

**Groq** (rápido, free tier generoso):

- `llama-3.3-70b-versatile` → análisis técnico, clasificación señales
- `llama-3.1-8b-instant` → tareas ligeras, resumen rápido

**OpenRouter** (variedad, modelos `:free` reales):

- `deepseek/deepseek-chat-v3:free` → reasoning complejo
- `google/gemini-2.0-flash-exp:free` → contextos largos, multimodal
- `meta-llama/llama-3.3-70b-instruct:free` → fallback

**Google AI Studio** (Gemini directo):

- `gemini-2.0-flash` → 15 RPM, ~1 M tokens/día free
- `gemini-1.5-flash-8b` → ligero, 15 RPM

> Los nombres exactos de modelos se centralizan en `src/mib/ai/models.py` como constantes.
> Si un modelo deja de estar disponible, solo se cambia en ese archivo.

### AI Router (`src/mib/ai/router.py`)

Clase `AIRouter` con método principal:

```python
async def complete(self, task: AITask) -> AIResponse: ...
```

Donde `AITask` tiene: `prompt`, `system`, `task_type` (Enum), `max_tokens`, `temperature`, `metadata`.

**Mapa `task_type → cadena de providers ordenada`**:

| task_type      | 1º preferencia         | 2º fallback           | 3º fallback         |
|----------------|------------------------|-----------------------|---------------------|
| fast_classify  | Groq 8B                | Gemini Flash 8B       | OpenRouter Llama    |
| analysis       | Groq 70B               | OpenRouter Llama 70B  | Gemini 2.0 Flash    |
| reasoning      | OpenRouter DeepSeek V3 | Gemini 2.0 Flash      | Groq 70B            |
| summary        | Gemini Flash 8B        | Groq 8B               | OpenRouter          |

**Comportamiento**:

1. Intenta el primero. Si falla con 429, 5xx o timeout → salta al siguiente.
2. Si todos fallan → devuelve `AIResponse` con `success=False` y `error` poblado. El llamador decide si degradar a respuesta sin IA.
3. Registra cada intento en `ai_calls` (tabla SQL) con: timestamp, task_type, provider, model, input_tokens, output_tokens, latency_ms, success, error.
4. `UsageTracker` consulta `ai_calls` para saber uso diario por provider y evitar exceder cuotas (leído de `config/ai_limits.yaml`).
5. Si un provider supera el 90 % de su cuota diaria → se salta automáticamente el resto del día.

### Prompts de sistema base (`src/mib/ai/prompts.py`)

Constantes versionadas. Incluir al menos:

- `SYSTEM_MARKET_ANALYST` — analista neutral, cita solo datos provistos
- `SYSTEM_NEWS_SENTIMENT` — clasifica bullish/bearish/neutral + justificación 1 frase
- `SYSTEM_QUERY_ROUTER` — convierte pregunta natural en plan de llamadas a fuentes (devuelve JSON)
- `SYSTEM_SUMMARIZER` — resume X en Y palabras, tono técnico

**Cláusula obligatoria en TODOS los prompts** (incluyendo en el output de `/ask`):

> "No proporcionas consejos financieros ni de inversión. Solo análisis descriptivo de los datos provistos. El usuario debe consultar a un profesional cualificado antes de tomar decisiones de inversión."

---

## 6. Telegram Bot — especificación

### Setup

- Token en `TELEGRAM_BOT_TOKEN` (`.env`)
- Whitelist en `TELEGRAM_ALLOWED_USERS` (`.env`, CSV de user IDs)
- Modo **polling** (outbound only, no requiere puerto abierto)
- Middleware `AuthMiddleware` rechaza updates de user_id no autorizado con mensaje genérico *"Acceso no autorizado"*
- Logging de cada comando (sin loguear payload completo ni contenido sensible)

### Comandos

| Comando                  | Descripción                                       | Ejemplo                      |
|--------------------------|---------------------------------------------------|------------------------------|
| `/start`                 | Bienvenida + lista de comandos                    | `/start`                     |
| `/help`                  | Ayuda detallada (opcional: por comando)           | `/help scan`                 |
| `/price <ticker>`        | Precio + indicadores + TV rating + resumen IA     | `/price BTC/USDT`            |
| `/chart <ticker> [tf]`   | Gráfico velas PNG (tf: 1h, 4h, 1d; default 1h)    | `/chart ETH/USDT 4h`         |
| `/scan [preset]`         | Screener (presets: oversold, breakout, trending)  | `/scan oversold`             |
| `/news <ticker>`         | 3 últimas noticias + sentiment agregado           | `/news AAPL`                 |
| `/macro`                 | Snapshot macro del día                            | `/macro`                     |
| `/watch <tkr> <op> <p>`  | Alerta precio (op: `>`, `<`)                      | `/watch BTC/USDT > 100000`   |
| `/alerts`                | Lista alertas activas con botones inline          | `/alerts`                    |
| `/ask <pregunta>`        | Pregunta en lenguaje natural al AI Router         | `/ask cómo está cripto hoy?` |
| `/status`                | Uptime, uso cuotas IA, fuentes activas/caídas     | `/status`                    |

### Alertas push automáticas

**Job cada 60 s** (`price_alerts.py`):

1. Lee alertas activas de `price_alerts`.
2. Consulta precio (respetando cache).
3. Si se cumple condición → envía mensaje + marca como `triggered`.
4. Alertas triggered se archivan 24 h y luego se borran.

**Job cada 5 min** (`watchlist_monitor.py`):

1. Lee watchlist por usuario.
2. Detecta cambios >5 % en 1 h o >10 % en 24 h.
3. Envía alerta si no se ha enviado una en la última hora para ese ticker (deduplicación en `sent_alerts`).

**Job cada 15 min** (`news_monitor.py`):

1. Fetch RSS feeds.
2. Para cada noticia nueva, si menciona ticker de alguna watchlist → resume (task_type=summary) + sentiment (task_type=fast_classify).
3. Si sentiment es fuerte → envía al usuario.
4. Deduplicación por hash de URL en tabla `processed_news`.

### Formato de mensajes

- `parse_mode='HTML'` (más robusto que MarkdownV2).
- Emojis mínimos y funcionales: 🟢 subida, 🔴 bajada, ⚪ neutro, ⚠️ alerta, 📊 datos, 📰 noticia.
- Mensajes largos → dividir en chunks <4000 chars (límite Telegram 4096, margen de seguridad).
- Botones inline para acciones repetitivas (refrescar, ver chart, borrar alerta).
- Nunca enviar stack traces al usuario; solo *"Ha ocurrido un error, revisa logs"*.
- Todos los strings visibles al usuario en **español**.

### Ejemplo de respuesta `/price BTC/USDT`

```
📊 <b>BTC/USDT</b> · Binance
💰 $98,432.50 <b>🟢 +2.34%</b> (24h)

<b>Indicadores (1h)</b>
RSI(14): 58.2 · Neutral
MACD: Bullish cross hace 3h
EMA20/50/200: 98.100 / 97.200 / 94.500

<b>TradingView Rating (1h):</b> Buy (8 señales)

<b>Análisis IA:</b>
Tendencia alcista de corto plazo con soporte en EMA50.
Volumen 15% sobre la media. RSI sin sobrecompra.

<i>Datos: Binance · 2026-04-22 14:32 UTC · No es consejo financiero</i>

[🔄 Refrescar] [📊 Chart 4h] [👁 Añadir a watchlist]
```

---

## 7. Modelo de datos (SQLite)

Tablas principales, gestionadas con Alembic:

```sql
-- Cache genérico con TTL
cache (
  key TEXT PRIMARY KEY,
  value BLOB,
  expires_at TIMESTAMP,
  source TEXT
)

-- Usuarios Telegram
users (
  telegram_id BIGINT PRIMARY KEY,
  username TEXT,
  created_at TIMESTAMP,
  is_active BOOLEAN DEFAULT 1,
  preferences JSON
)

-- Watchlist
watchlist_items (
  id INTEGER PRIMARY KEY,
  user_id BIGINT REFERENCES users(telegram_id),
  ticker TEXT,
  added_at TIMESTAMP,
  UNIQUE(user_id, ticker)
)

-- Alertas de precio
price_alerts (
  id INTEGER PRIMARY KEY,
  user_id BIGINT REFERENCES users(telegram_id),
  ticker TEXT,
  operator TEXT CHECK(operator IN ('>', '<')),
  target_price REAL,
  created_at TIMESTAMP,
  triggered_at TIMESTAMP,
  is_active BOOLEAN DEFAULT 1
)

-- Deduplicación de alertas enviadas
sent_alerts (
  id INTEGER PRIMARY KEY,
  user_id BIGINT,
  ticker TEXT,
  alert_type TEXT,
  sent_at TIMESTAMP
)

-- Log de llamadas a IA (para UsageTracker y debugging)
ai_calls (
  id INTEGER PRIMARY KEY,
  timestamp TIMESTAMP,
  task_type TEXT,
  provider TEXT,
  model TEXT,
  input_tokens INTEGER,
  output_tokens INTEGER,
  latency_ms INTEGER,
  success BOOLEAN,
  error TEXT
)

-- Log de llamadas a fuentes (para métricas)
source_calls (
  id INTEGER PRIMARY KEY,
  timestamp TIMESTAMP,
  source TEXT,
  endpoint TEXT,
  latency_ms INTEGER,
  success BOOLEAN,
  cached BOOLEAN,
  error TEXT
)

-- Noticias procesadas (deduplicación)
processed_news (
  id INTEGER PRIMARY KEY,
  url_hash TEXT UNIQUE,
  ticker TEXT,
  sentiment TEXT,
  processed_at TIMESTAMP
)
```

SQLite configurado con:

```sql
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA foreign_keys = ON;
PRAGMA temp_store = MEMORY;
```

---

## 8. Estructura de carpetas

```
mib/
├── src/mib/
│   ├── __init__.py
│   ├── main.py                  # entrypoint: orquesta FastAPI + Telegram + scheduler
│   ├── config.py                # Settings pydantic-settings
│   ├── db/
│   │   ├── __init__.py
│   │   ├── session.py           # async engine, session factory, pragmas
│   │   ├── models.py            # SQLAlchemy models
│   │   └── migrations/          # alembic
│   ├── sources/
│   │   ├── __init__.py
│   │   ├── base.py              # ABC DataSource + RateLimiter
│   │   ├── ccxt_source.py
│   │   ├── yfinance_source.py
│   │   ├── coingecko.py
│   │   ├── alphavantage.py
│   │   ├── finnhub.py
│   │   ├── fred.py
│   │   ├── tradingview_ta.py
│   │   └── rss.py
│   ├── indicators/
│   │   ├── __init__.py
│   │   ├── technical.py         # wrappers pandas-ta
│   │   └── charting.py          # mplfinance PNG generator
│   ├── ai/
│   │   ├── __init__.py
│   │   ├── router.py            # AIRouter
│   │   ├── models.py            # constantes de nombres de modelos
│   │   ├── providers/
│   │   │   ├── __init__.py
│   │   │   ├── base.py
│   │   │   ├── groq_provider.py
│   │   │   ├── openrouter_provider.py
│   │   │   └── gemini_provider.py
│   │   ├── prompts.py
│   │   └── usage_tracker.py
│   ├── services/
│   │   ├── __init__.py
│   │   ├── market.py            # orquesta sources + indicators
│   │   ├── news.py
│   │   ├── scanner.py
│   │   └── alerts.py
│   ├── api/
│   │   ├── __init__.py
│   │   ├── app.py               # FastAPI app factory
│   │   ├── dependencies.py
│   │   └── routers/
│   │       ├── symbol.py
│   │       ├── scan.py
│   │       ├── news.py
│   │       ├── macro.py
│   │       ├── ask.py
│   │       └── health.py
│   ├── telegram/
│   │   ├── __init__.py
│   │   ├── bot.py               # Application setup
│   │   ├── middleware.py        # auth
│   │   ├── handlers/
│   │   │   ├── start.py
│   │   │   ├── help.py
│   │   │   ├── price.py
│   │   │   ├── chart.py
│   │   │   ├── scan.py
│   │   │   ├── news.py
│   │   │   ├── macro.py
│   │   │   ├── watch.py
│   │   │   ├── ask.py
│   │   │   └── status.py
│   │   ├── jobs/
│   │   │   ├── price_alerts.py
│   │   │   ├── watchlist_monitor.py
│   │   │   └── news_monitor.py
│   │   └── formatters.py        # helpers HTML + chunking
│   ├── cache/
│   │   ├── __init__.py
│   │   └── store.py             # get_or_set con TTL
│   └── models/                  # Pydantic schemas (≠ db.models)
│       ├── __init__.py
│       ├── market.py
│       ├── news.py
│       └── ai.py
├── tests/
│   ├── conftest.py
│   ├── unit/
│   └── integration/
├── config/
│   ├── rss_feeds.yaml
│   ├── default_symbols.yaml
│   ├── scanner_presets.yaml
│   └── ai_limits.yaml
├── scripts/
│   ├── init_db.py
│   ├── check_quotas.py
│   └── backup.sh
├── data/                        # SQLite + charts tmp (gitignored)
├── .env.example
├── .gitignore
├── .pre-commit-config.yaml
├── .dockerignore
├── pyproject.toml               # uv + ruff + mypy + pytest config
├── Dockerfile
├── docker-compose.yml
├── Makefile
└── README.md
```

---

## 9. Configuración (`.env.example`)

Ver **Apéndice A** al final del documento. El `.env` real lo rellena el operador humano en local, NUNCA commitear.

---

## 10. Tests requeridos

- **Unit tests** por cada `DataSource` usando `respx` para mockear httpx.
- **Unit tests** del `AIRouter` con fallback chain simulando 429s.
- **Unit tests** de indicadores con fixtures OHLCV conocidos (valores verificables a mano).
- **Unit tests** del formateador HTML Telegram (snapshot tests).
- **Integration test** end-to-end: `/price BTC/USDT` mockeando red pero ejercitando todo el stack.
- **Integration test** del `UsageTracker` usando SQLite en memoria.

**Cobertura mínima**:
- 70 % global.
- 85 % en `sources/` y `ai/router.py`.
- Medida con `pytest-cov`.

---

## 11. Deploy general

### Dockerfile multi-stage

- Stage 1 `builder`: `python:3.12-slim`, instala `uv`, copia `pyproject.toml` + `uv.lock`, `uv sync --frozen --no-dev`.
- Stage 2 `runtime`: `python:3.12-slim`, copia solo `.venv` y `src/`, usuario no-root `mib`, `HEALTHCHECK` apuntando a `/health`.
- Imagen final objetivo: <200 MB.

### docker-compose.yml (base)

- Servicio `mib` con volume `./data:/app/data`.
- `restart: unless-stopped`.
- Logs con driver `json-file`, `max-size: 10m`, `max-file: 3`.
- Port `127.0.0.1:8000:8000` (NO a 0.0.0.0).
- Healthcheck cada 60 s.

### Makefile

```makefile
.PHONY: dev up down logs test lint format migrate backup

dev:        ## Arranca local sin Docker
	uv run python -m mib.main

up:         ## docker-compose up -d
	docker compose up -d

down:       ## docker-compose down
	docker compose down

logs:       ## Sigue logs del contenedor
	docker compose logs -f mib

test:       ## Ejecuta tests
	uv run pytest --cov=src/mib --cov-report=term-missing

lint:       ## Lint + type check
	uv run ruff check .
	uv run mypy src/mib/ai src/mib/sources/base.py src/mib/services

format:     ## Format código
	uv run ruff format .
	uv run ruff check --fix .

migrate:    ## Aplica migraciones
	uv run alembic upgrade head

backup:     ## Backup manual de la DB
	./scripts/backup.sh
```

---

## 11bis. Deploy específico — BambuServer

### Host

- **Hardware**: Fujitsu PRIMERGY RX100 S7, Xeon E3-1220 (4c/4t @ 3.1 GHz), 4 GB DDR3 ECC, 2× 500 GB SATA.
- **OS**: Ubuntu 24.04.4 LTS, kernel 6.8.
- **IP LAN**: 192.168.0.24 (fija).
- **Restricción crítica**: 4 GB RAM compartidos con otros servicios (n8n, etc.).

### Límites de recursos en `docker-compose.yml`

```yaml
services:
  mib:
    deploy:
      resources:
        limits:
          memory: 512M
          cpus: '1.5'
        reservations:
          memory: 256M
          cpus: '0.5'
    mem_swappiness: 10
```

### Persistencia y backup

- SQLite en `./data/mib.db` (volume montado en contenedor).
- Disco secundario `/dev/sdb` sin usar → montar en `/mnt/backup`:
```bash
  # Instrucciones que van en el README, NO ejecutar automáticamente:
  sudo mkfs.ext4 /dev/sdb         # solo si está vacío
  sudo mkdir -p /mnt/backup
  sudo blkid /dev/sdb             # obtener UUID
  # Añadir a /etc/fstab:
  # UUID=xxx /mnt/backup ext4 defaults,nofail 0 2
  sudo mount -a
```
- Script `scripts/backup.sh`:
  - Copia `data/mib.db` a `/mnt/backup/mib/mib-YYYYMMDD-HHMMSS.db.gz` (comprimido).
  - Rotación: mantiene últimos 14 días, borra anteriores.
  - Usa `sqlite3 .backup` (no `cp` en caliente) para consistencia.
- Cron host documentado en README:
```
  0 3 * * * /home/USER/bambuserver/mib/scripts/backup.sh >> /var/log/mib-backup.log 2>&1
```

### Optimizaciones por RAM limitada

- `uvicorn --workers 1` (async, suficiente para uso personal).
- APScheduler con `max_instances=1` por job.
- Pool `httpx` con `limits=httpx.Limits(max_connections=20, max_keepalive_connections=5)`.
- Cache en memoria LRU limitada a 50 MB (resto va a SQLite).
- `MALLOC_ARENA_MAX=2` como env var en Docker para reducir fragmentación de memoria Python.

### Red

- FastAPI bindeado a `127.0.0.1:8000` (no accesible desde LAN).
- Telegram polling (outbound only, sin puertos abiertos).
- Si en el futuro se quiere acceso LAN → documentar cómo ponerlo detrás de nginx con auth básica, pero **fuera de alcance ahora**.

### Monitorización ligera

- Endpoint `/health` devuelve JSON con: `status`, `db_ok`, `sources_status` (map de fuente → ok/degraded/down), `ai_quotas` (uso %), `uptime_seconds`.
- El comando `/status` de Telegram consume este endpoint y lo formatea.

---

## 12. Calidad de código — criterios de aceptación

- **Todas las funciones async públicas tipadas** con type hints completos.
- **Pydantic v2** para modelos de dominio y respuestas API.
- **Sin `print()`**; siempre `logger` de loguru.
- **Sin secrets hardcoded**, ni siquiera en tests (fixtures con fakes).
- **Sin `except:` vacío ni `except Exception: pass`**; todo error se loguea con contexto.
- **Docstrings Google-style** en módulos, clases y funciones públicas.
- **`ruff`** pasa con reglas `E, F, I, N, UP, B, A, C4, RET, SIM, ARG` (configurar en `pyproject.toml`).
- **`mypy --strict`** pasa en `src/mib/ai/`, `src/mib/sources/base.py`, `src/mib/services/`.
- **`pre-commit`** instalado con: ruff, mypy, detect-secrets, trailing-whitespace, end-of-file-fixer.
- **`detect-secrets`** como último guardián antes de commits.

---

## 13. Seguridad — checklist

- [ ] `.env` en `.gitignore` (y en `.dockerignore`).
- [ ] `.env.example` presente, sin valores reales.
- [ ] `detect-secrets` en pre-commit con baseline.
- [ ] README con sección *"Qué hacer si te filtras un secret"* (revocar + regenerar).
- [ ] Middleware `AuthMiddleware` en Telegram rechaza user_id no whitelisted.
- [ ] FastAPI bindeado solo a `127.0.0.1`.
- [ ] Contenedor Docker con usuario no-root.
- [ ] Healthcheck no expone datos sensibles (no listar tickers de usuarios, no versiones detalladas de libs).
- [ ] Logs no contienen tokens, keys ni contenido de mensajes privados.
- [ ] SQLite con `PRAGMA foreign_keys = ON`.

---

## 14. Documentación

`README.md` debe incluir:

1. Descripción breve + screenshot ASCII de una respuesta Telegram.
2. Diagrama de arquitectura (copiado de sección 3).
3. **Quickstart**:
   - Clone del repo.
   - Copiar `.env.example` a `.env` y rellenar.
   - Enlaces directos para obtener cada key (tabla con URLs).
   - `make migrate && make up`.
   - Abrir Telegram y hablar con el bot.
4. Tabla de comandos Telegram.
5. Tabla de fuentes con free tiers y límites.
6. Sección *"Añadir una fuente nueva"* (cómo extender la ABC).
7. Sección *"Añadir un comando Telegram nuevo"*.
8. Sección *"Montar el disco de backup"* (instrucciones exactas de sección 11bis).
9. Sección *"Qué hacer si filtras un secret"*.
10. **Disclaimer legal**: no es consejo financiero, uso bajo responsabilidad propia.
11. Licencia: MIT.

---

## 15. Entregable por fases

Al final de cada fase, **parar** y listar:
- ✅ Qué se ha hecho.
- ❓ Decisiones tomadas que no estaban explícitas en el spec.
- ⚠️ Qué queda pendiente para la siguiente fase.

No avanzar sin confirmación humana.

### FASE 1 — Esqueleto (sin ninguna API key)

- Estructura de carpetas completa.
- `pyproject.toml` con uv y todas las dependencias.
- `pre-commit` config.
- `.env.example`, `.gitignore`, `.dockerignore`.
- `Makefile`.
- `Dockerfile` multi-stage + `docker-compose.yml` con límites de recursos.
- Alembic inicializado con primera migración (tablas vacías).
- `config.py` con pydantic-settings.
- Logger loguru configurado (JSON a stdout).
- FastAPI app factory con endpoint `/health` funcional.
- README stub con quickstart básico.
- **Criterio de aceptación**: `make up` levanta el contenedor y `curl localhost:8000/health` devuelve 200.

### FASE 2 — Fuentes core (sin API keys)

- ABC `DataSource` con rate limiter y cache store.
- `CCXTSource` implementado (Binance por defecto, configurable).
- `YFinanceSource` implementado.
- `TradingViewTASource` implementado.
- Tests unitarios con respx mockeando respuestas.
- Endpoint `/symbol/{ticker}` devuelve precio + OHLCV básico (sin indicadores aún, sin IA).
- **Criterio de aceptación**: `curl localhost:8000/symbol/BTC-USDT` devuelve JSON con precio real de Binance.

### FASE 3 — Resto de fuentes + indicadores (requiere keys de Finnhub, Alpha Vantage, FRED, CoinGecko)

- `CoinGeckoSource`, `AlphaVantageSource`, `FinnhubSource`, `FREDSource`, `RSSSource`.
- Tests unitarios de cada una.
- `indicators/technical.py` con wrappers pandas-ta (RSI, MACD, EMA, Bollinger, ADX, volumen).
- `indicators/charting.py` genera PNG con mplfinance.
- `/symbol/{ticker}` enriquecido con indicadores.
- Endpoint `/macro` funcional.
- Endpoint `/news/{ticker}` sin sentiment aún (eso es fase 4).
- **Criterio de aceptación**: `/macro` devuelve SPX, VIX, DXY, 10Y yield, BTC dominance con datos reales.

### FASE 4 — Capa IA (requiere keys de Groq, OpenRouter, Gemini)

- Providers `GroqProvider`, `OpenRouterProvider`, `GeminiProvider` implementando ABC `AIProvider`.
- `AIRouter` con fallback chain completa.
- `UsageTracker` consultando tabla `ai_calls`.
- Prompts versionados en `prompts.py`.
- `/symbol/{ticker}` enriquecido con resumen IA.
- `/news/{ticker}` enriquecido con sentiment.
- Endpoint `/ask` funcional con `SYSTEM_QUERY_ROUTER`.
- Scanner presets funcionando vía `/scan`.
- **Criterio de aceptación**: `/ask "¿cómo está el mercado cripto?"` devuelve respuesta coherente en <10 s, y si se fuerza fallo en Groq, el Router cae a OpenRouter automáticamente.

### FASE 5 — Telegram

- Bot setup con python-telegram-bot v21+ en modo polling.
- `AuthMiddleware` con whitelist.
- Todos los comandos de la sección 6.
- Jobs APScheduler: `price_alerts`, `watchlist_monitor`, `news_monitor`.
- Formateo HTML con emojis.
- Botones inline para refresh, chart, delete alert.
- Gráficos PNG enviados con `send_photo`.
- **Criterio de aceptación**: usuario whitelisted puede usar los 11 comandos y recibe alerta push cuando se cumple una condición de precio.

### FASE 6 — Hardening

- Tests faltantes hasta cumplir cobertura mínima.
- `mypy --strict` pasa en módulos marcados.
- README completo con todos los puntos de la sección 14.
- Docker build final verificado en máquina limpia (simular con `docker build --no-cache`).
- Script `scripts/backup.sh` testeado.
- Sección de README con ejemplos reales de output.
- **Criterio de aceptación**: alguien que NO conozca el proyecto puede clonarlo, seguir el quickstart del README y tener el bot funcionando en <20 min.

---

## 16. Restricciones duras

- ❌ **Cero dependencias de pago.** Si una lib requiere suscripción, fuera.
- ❌ **Cero trading real.** Solo lectura + análisis.
- ❌ **Cero secrets en código o commits.** `.env` en `.gitignore`.
- ❌ **Cero promesas de rentabilidad** en prompts IA ni en respuestas Telegram.
- ❌ **Cero exposición a internet** más allá del polling outbound a Telegram.
- ❌ **Cero llamadas síncronas bloqueantes** en handlers async (usar `asyncio.to_thread` para pandas pesado).
- ❌ **Cero modelos de IA locales** (Ollama, llama.cpp, etc.) — el host no tiene RAM.
- ❌ **Cero binding de FastAPI a 0.0.0.0.**
- ✅ **Sí**, todo el código en inglés (variables, funciones, clases, comentarios). Solo strings visibles al usuario en español.
- ✅ **Sí**, cada PR/fase debe pasar `make lint` y `make test` antes de considerarse completa.

---

## 17. Primera tarea para Claude Code

1. Lee este documento entero, incluido el Apéndice A.
2. Confirma en 5 bullets tu entendimiento del alcance.
3. Lista dudas o ambigüedades detectadas ANTES de escribir código.
4. Espera respuesta del operador humano.
5. Empieza por **FASE 1** únicamente. No toques FASES 2+ hasta que se confirme.

No asumas nada que no esté aquí. Si algo no está especificado, **pregunta**.

---

## Apéndice A — Plantilla `.env.example`

```bash
# === APP ===
APP_ENV=production
LOG_LEVEL=INFO
TIMEZONE=Europe/Madrid

# === DATABASE ===
DATABASE_URL=sqlite+aiosqlite:///./data/mib.db

# === TELEGRAM ===
# Obtener de @BotFather en Telegram (/newbot)
# User IDs: hablar con @userinfobot para obtener el tuyo
TELEGRAM_BOT_TOKEN=
TELEGRAM_ALLOWED_USERS=

# === API SERVER ===
API_HOST=127.0.0.1
API_PORT=8000

# === IA — Groq ===
# https://console.groq.com/keys
GROQ_API_KEY=
GROQ_DAILY_LIMIT=14000

# === IA — OpenRouter ===
# https://openrouter.ai/keys
OPENROUTER_API_KEY=
OPENROUTER_DAILY_LIMIT=200

# === IA — Google Gemini ===
# https://aistudio.google.com/app/apikey
GEMINI_API_KEY=
GEMINI_DAILY_LIMIT=1500

# === DATOS ===
# https://www.alphavantage.co/support/#api-key
ALPHA_VANTAGE_API_KEY=

# https://finnhub.io/register
FINNHUB_API_KEY=

# https://fred.stlouisfed.org/docs/api/api_key.html
FRED_API_KEY=

# https://www.coingecko.com/en/developers/dashboard (opcional)
COINGECKO_API_KEY=

# === SCHEDULER ===
PRICE_ALERTS_INTERVAL_SEC=60
WATCHLIST_INTERVAL_SEC=300
NEWS_MONITOR_INTERVAL_SEC=900

# === RUNTIME ===
MALLOC_ARENA_MAX=2
```
