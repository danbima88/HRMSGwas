#!/bin/bash
# GWAS v2.0 — Scanner cron wrapper
# Runs the wallet scanner loop
#
# Intended to be run via systemd timer or cron:
#   */5 * * * * /opt/gwas/cron/gwas_scanner.sh

set -e

SCRIPT_DIR="/opt/gwas"
LOG_DIR="${SCRIPT_DIR}/logs"
VENV_PYTHON="${SCRIPT_DIR}/venv/bin/python3"
SYSTEM_PYTHON=$(which python3)

# Use venv if it exists, otherwise system python
if [ -f "$VENV_PYTHON" ]; then
    PYTHON="$VENV_PYTHON"
else
    PYTHON="$SYSTEM_PYTHON"
fi

export PYTHONPATH="/opt/gwas:$PYTHONPATH"

cd "$SCRIPT_DIR"

# Source secrets if available
if [ -f ~/.gwas_secrets ]; then
    set -a
    source ~/.gwas_secrets
    set +a
fi

# Run a single scan cycle
exec "$PYTHON" scripts/run_scanner.py --once
