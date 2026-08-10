#!/bin/sh
# Railway web process — log config diagnostics before uvicorn so deploy logs
# are never empty when startup fails.
set -eu

echo "=========================================="
echo "LME Monitoring System - Railway startup"
echo "=========================================="
echo "PWD=$(pwd)"
echo "PORT=${PORT:-8000}"
echo "ENVIRONMENT=${ENVIRONMENT:-not set}"
echo "DEBUG=${DEBUG:-not set}"
echo "DATABASE_URL set: $(test -n "${DATABASE_URL:-}" && echo yes || echo NO)"
echo "APP_PUBLIC_URL=${APP_PUBLIC_URL:-not set}"
echo "ALLOWED_ORIGINS=${ALLOWED_ORIGINS:-not set}"

python - <<'PY'
import sys

try:
    from config.settings import settings
except Exception as exc:
    print("FATAL: settings failed to load:", exc, file=sys.stderr)
    print(
        "\nIf ENVIRONMENT=production, you must set:\n"
        "  DEBUG=false\n"
        "  ALLOWED_ORIGINS=https://your-frontend-domain.com\n"
        "  SECRET_KEY=<random 32+ chars>\n",
        file=sys.stderr,
    )
    sys.exit(1)

print(f"Settings OK — environment={settings.ENVIRONMENT} debug={settings.DEBUG}")
print(f"CORS origins={settings.cors_origins}")
PY

echo "Running database migrations (internal DATABASE_URL)..."
unset MIGRATE_USE_PUBLIC_URL
export ENVIRONMENT=development
export DEBUG=false
export ALLOWED_ORIGINS=http://localhost
export SKIP_PRODUCTION_CHECKS=true
python deploy_migrate.py

exec uvicorn main:app --host 0.0.0.0 --port "${PORT:-8000}" --log-level info
