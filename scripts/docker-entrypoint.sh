#!/bin/sh
# Container entrypoint: apply pending Alembic migrations then exec the app.
#
# Running migrations on boot is safe because:
#   - `alembic upgrade head` is idempotent (no-op when already at head).
#   - Concurrent starts are not a concern (single container).
#   - The DB file is a volume, so the migrated schema persists.
set -eu

cd /app
echo "entrypoint: running alembic upgrade head..."
/app/.venv/bin/alembic upgrade head

echo "entrypoint: starting mib..."
exec /app/.venv/bin/python -m mib.main
