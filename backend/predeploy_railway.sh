#!/bin/sh
# Railway pre-deploy: run DB migrations without tripping production settings checks.
set -eu

echo "=========================================="
echo "LME Monitoring System - pre-deploy migrations"
echo "=========================================="
echo "PWD=$(pwd)"
echo "DATABASE_URL set: $(test -n "${DATABASE_URL:-}" && echo yes || echo NO)"
echo "DATABASE_PUBLIC_URL set: $(test -n "${DATABASE_PUBLIC_URL:-}" && echo yes || echo NO)"

# Pre-deploy cannot resolve postgres.railway.internal — use the public proxy.
if [ -n "${DATABASE_PUBLIC_URL:-}" ]; then
  echo "Using DATABASE_PUBLIC_URL for pre-deploy migrations"
  export DATABASE_URL="$DATABASE_PUBLIC_URL"
fi

# Migrations import config.settings via SQLAlchemy — force safe values so a
# misconfigured production env (DEBUG=true, ALLOWED_ORIGINS=*) cannot abort deploy.
export ENVIRONMENT=development
export DEBUG=false
export ALLOWED_ORIGINS=http://localhost
export SKIP_PRODUCTION_CHECKS=true

python deploy_migrate.py
