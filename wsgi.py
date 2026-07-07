#!/usr/bin/env python3
"""
GWAS v2.0 — Helius Webhook WSGI Entry Point
Dipanggil oleh Gunicorn. PYTHONPATH=/opt/gwas wajib.
"""
import os
import sys

# Ensure src/ is importable
sys.path.insert(0, "/opt/gwas")

from src.helius_webhook import create_app

# Load webhook secret from env
secret = os.environ.get("HELIUS_WEBHOOK_SECRET", "gwas-v1-default")
app = create_app(webhook_secret=secret)
