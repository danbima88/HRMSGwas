#!/usr/bin/env python3
"""
GWAS v2.0 — Manual Weekly Report Trigger
Generates and sends the weekly performance report.

Usage:
    python3 scripts/weekly_report.py [--week YYYY-MM-DD] [--send]

    --week    Specify week start (default: last completed week)
    --send    Send to Telegram (requires TELEGRAM_BOT_TOKEN)
"""

import sys
import os
import argparse
import logging
import yaml
from datetime import datetime

sys.path.insert(0, "/opt/gwas")

from src.db import Database
from src.performance import compute_weekly_report, format_report_telegram, send_weekly_report
from src.alert import AlertSender


def load_config() -> dict:
    config_path = "/opt/gwas/config/settings.yaml"
    with open(config_path) as f:
        raw = os.path.expandvars(f.read())
    return yaml.safe_load(raw)


def main():
    parser = argparse.ArgumentParser(description="GWAS Weekly Report Generator")
    parser.add_argument("--week", type=str, help="Week start date (YYYY-MM-DD)")
    parser.add_argument("--send", action="store_true", help="Send report to Telegram")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
    logger = logging.getLogger(__name__)

    config = load_config()

    week_start = None
    if args.week:
        try:
            week_start = datetime.fromisoformat(args.week)
        except ValueError:
            print(f"Invalid date format: {args.week}. Use YYYY-MM-DD.")
            sys.exit(1)

    db = Database(config.get("database", {}).get("path", "/opt/gwas/data/gwas.db"))

    if args.send:
        bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        if not bot_token:
            print("❌ TELEGRAM_BOT_TOKEN not set")
            sys.exit(1)
        telegram_config = config.get("telegram", {})
        sender = AlertSender(
            bot_token=bot_token,
            user_id=telegram_config.get("user_id", ""),
        )
        success = send_weekly_report(sender, week_start=week_start)
        if success:
            print("✅ Weekly report sent to Telegram")
        else:
            print("❌ Failed to send report")
    else:
        report = compute_weekly_report(week_start=week_start)
        if args.json:
            import json
            print(json.dumps(report, indent=2, default=str))
        else:
            text = format_report_telegram(report)
            print(text)


if __name__ == "__main__":
    main()
