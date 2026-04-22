# Market Intelligence Bot (MIB)

Self-hosted financial intelligence bot with free-tier LLMs, running on BambuServer.

See [`PROJECT.md`](./PROJECT.md) for the full specification.

## Quickstart (fase 1 — esqueleto)

```bash
# 1. Clonar
git clone <repo-url> finanzas && cd finanzas

# 2. Configurar entorno
cp .env.example .env
# Edita .env con tus credenciales

# 3. Levantar
make migrate    # aplica migraciones de Alembic
make up         # arranca el contenedor

# 4. Verificar
curl http://127.0.0.1:8000/health
```

> Documentación completa, tabla de comandos Telegram, y más detalles
> se añaden al final de cada fase del plan (ver `PROJECT.md` § 15).

## Licencia

MIT.
