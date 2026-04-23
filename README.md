# Market Intelligence Bot (MIB)

Self-hosted financial intelligence bot with free-tier LLMs, running on BambuServer.

See [`PROJECT.md`](./PROJECT.md) for the full specification.

## Quickstart

```bash
# 1. Clonar
git clone <repo-url> finanzas && cd finanzas

# 2. Configurar entorno
cp .env.example .env
# Edita .env con tus credenciales

# 3. Primer setup del host (ver § "First setup on a new host")
sudo chown -R 1001:1001 ./data

# 4. Levantar
make up         # docker compose up -d --build (aplica migraciones automáticamente)

# 5. Verificar
curl http://127.0.0.1:8000/health
curl http://127.0.0.1:8000/symbol/BTC-USDT
curl http://127.0.0.1:8000/symbol/AAPL
```

## First setup on a new host

The Docker container runs as a non-root user `mib` with **uid 1001** (spec §13).
The bind-mounted volume `./data` on the host must be writable by that uid
before the first `make up`, otherwise the entrypoint's `alembic upgrade head`
fails with "readonly database" and the container enters a restart loop.

Run this **once** per host, right after cloning the repo:

```bash
sudo chown -R 1001:1001 ./data
```

You only need to repeat it if you delete and recreate the `./data` directory.
Subsequent migrations and writes happen inside the container with the correct
ownership already in place.

> **Why uid 1001 instead of matching the host user?** The Dockerfile pins a
> stable uid/gid so image + volume ownership are reproducible across hosts.
> Matching the operator's host uid would couple the image to each machine.

## Endpoints disponibles (FASE 2)

| Method | Path                       | Descripción                                         |
|--------|----------------------------|-----------------------------------------------------|
| GET    | `/health`                  | Liveness + `sources_status` refrescado cada 5 min   |
| GET    | `/symbol/{ticker}`         | Quote + OHLCV (+ TV rating opcional) — auto-detect  |
| GET    | `/docs`                    | Swagger UI (solo loopback)                          |
| GET    | `/openapi.json`            | Schema OpenAPI                                      |

Query params del `/symbol`:
- `timeframe`: `1m` / `5m` / `15m` / `30m` / `1h` / `4h` / `1d` / `1wk` (default `1h`)
- `limit`: 1–500 (default 100)

Heurística del detector (documentada en `mib.services.market.detect_ticker_kind`):

| Entrada           | Destino                                          |
|-------------------|--------------------------------------------------|
| `^GSPC`, `^VIX`   | yfinance (índice)                                |
| `EURUSD=X`        | yfinance (forex)                                 |
| `GC=F`            | yfinance (futuros)                               |
| `BTC/USDT`        | CCXT (cripto)                                    |
| `ETH-USD`         | CCXT (cripto)                                    |
| `BRK-B`, `BF.B`   | yfinance (quote no-cripto tras el separador)     |
| `AAPL`, `SPY`     | yfinance (alfanumérico)                          |

## Comandos `make`

```
help         Mostrar ayuda
dev          Run app locally (sin Docker)
up           docker compose up -d --build
down         docker compose down
logs         tail -f de logs del container
test         pytest con coverage
lint         ruff check + mypy strict en módulos críticos
format       ruff format + auto-fix
migrate      alembic upgrade head (local)
migration    alembic revision --autogenerate (pasa m="mensaje")
backup       scripts/backup.sh (cuando exista)
clean        limpiar caches (no toca .venv ni data/)
```

## Documentación relacionada

- [`PROJECT.md`](./PROJECT.md) — spec completa
- [`scripts/validate_pandas_ta.py`](./scripts/validate_pandas_ta.py) — script de validación
  de indicadores técnicos (cross-check con `ta` de Bukosabino)

## Licencia

MIT.
