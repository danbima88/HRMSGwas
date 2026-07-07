#!/bin/bash
# GWAS v1.1 — Weekly report cron wrapper
#
# Intended to be run weekly:
#   0 9 * * MON /opt/gwas/cron/gwas_report.sh

set -e

SCRIPT_DIR="/opt/gwas"
LOG_DIR="${SCRIPT_DIR}/logs"
VENV_PYTHON="${SCRIPT_DIR}/venv/bin/python3"
SYSTEM_PYTHON=$(which python3)

if [ -f "$VENV_PYTHON" ]; then
    PYTHON="$VENV_PYTHON"
else
    PYTHON="$SYSTEM_PYTHON"
fi

export PYTHONPATH="/opt/gwas:$PYTHONPATH"

cd "$SCRIPT_DIR"

# Source secrets
if [ -f ~/.gwas_secrets ]; then
    set -a
    source ~/.gwas_secrets
    set +a
fi

# Generate and send report
exec "$PYTHON" scripts/weekly_report.py --send
