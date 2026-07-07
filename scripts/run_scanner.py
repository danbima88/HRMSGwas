#!/usr/bin/env python3
"""
GWAS v2.0 — Main Scanner Loop
Discovers wallets, scores convictions, sends alerts.

Usage:
    python3 scripts/run_scanner.py [--once] [--interval SECONDS]
    
    --once      Run a single scan cycle and exit
    --interval  Seconds between cycles (default: 300 = 5min)
"""

import sys
import os
import time
import signal
import argparse
import logging
import yaml
from datetime import datetime
from pathlib import Path

# Ensure /opt/gwas/src is importable
sys.path.insert(0, "/opt/gwas")

from src.db import get_db, Database
from src.wallet_scanner import (
    scan_wallets,
    get_wallet_last_trade,
    check_exit_conditions,
    quality_filter,
    normalize_wallet_data,
    fetch_wallet_detail,
    fetch_wallet_activity,
)
from src.conviction import compute_conviction, should_alert, should_include_photon
from src.consistency import fetch_trending_consistency, get_token_consistency, DEFAULT_TIMEFRAMES
from src.safety import check_token_safety, MIN_LP_USD, MIN_TOKEN_AGE_MINUTES, MAX_TOP10_HOLDER_PCT
from src.alert import (
    AlertSender,
    RateLimiter,
    generate_alert_id,
    build_gmgn_link,
    build_photon_link,
)
from src.correlator import DEFAULT_WINDOW_HOURS


def load_config() -> dict:
    """Load config from /opt/gwas/config/settings.yaml"""
    config_path = "/opt/gwas/config/settings.yaml"
    with open(config_path) as f:
        raw = f.read()
    # Expand env vars
    raw = os.path.expandvars(raw)
    config = yaml.safe_load(raw)
    return config


def setup_logging(config: dict):
    """Setup logging from config."""
    log_config = config.get("logging", {})
    level = getattr(logging, log_config.get("level", "INFO"))
    log_file = log_config.get("file", "/opt/gwas/logs/gwas.log")
    max_size = log_config.get("max_size_mb", 10) * 1024 * 1024
    backup_count = log_config.get("backup_count", 5)

    os.makedirs(os.path.dirname(log_file), exist_ok=True)

    from logging.handlers import RotatingFileHandler

    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = RotatingFileHandler(
        log_file, maxBytes=max_size, backupCount=backup_count
    )
    file_handler.setFormatter(formatter)
    file_handler.setLevel(level)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    console_handler.setLevel(level)

    root_logger = logging.getLogger()
    root_logger.setLevel(level)
    root_logger.addHandler(file_handler)
    root_logger.addHandler(console_handler)


def run_scan_cycle(config: dict, alert_sender: AlertSender, db: Database):
    """
    Execute one complete scan cycle:
    1. Scan wallets from GMGN
    2. For each qualified wallet, get last trade
    3. Run safety check on token
    4. Compute conviction score
    5. If above threshold, send alert
    6. Check exit conditions for stale wallets
    """
    logger = logging.getLogger(__name__)
    cycle_start = datetime.utcnow()
    logger.info(f"=== Scan cycle started at {cycle_start.isoformat()} ===")

    alert_config = config.get("alert", {})
    quality = alert_config.get("quality_filter", {})
    conviction_config = alert_config.get("conviction", {})
    rate_config = config.get("rate_limits", {})
    dead_config = config.get("dead_alert", {})
    bridge_config = config.get("solauto_bridge", {})
    bridge_whitelist = bridge_config.get("wallet_whitelist", [])
    bridge_whitelist_set = set(bridge_whitelist)  # for O(1) lookup

    # Reset rate limiter cycle
    alert_sender.rate_limiter.start_cycle()

    # ── Step 1: Scan wallets ────────────────────────────────────────────
    wallets = scan_wallets(
        min_wr=quality.get("min_wr", 30),
        min_pnl=quality.get("min_pnl", 0),
        min_trades=quality.get("min_trades", 10),
        sensitivity=alert_config.get("sensitivity", "MEDIUM"),
        limit=50,
    )

    if not wallets:
        logger.info("No qualified wallets found this cycle")
        return

    logger.info(f"Processing {len(wallets)} qualified wallets for alert generation")

    # ── Step 2-5: For each wallet, check safety, score, alert ───
    alerts_sent = 0
    exit_alerts_sent = 0
    score_threshold = conviction_config.get("score_threshold", 70)
    seen_tokens = set()  # dedup: 1 alert per token per cycle

    for wallet in wallets:
        wallet_addr = wallet["address"]

        # Use embedded last_trade from scan_wallets()
        last_trade = wallet.get("last_trade")
        if not last_trade or not last_trade.get("token_address"):
            continue

        token_address = last_trade["token_address"]

        # Skip if already alerted recently
        if db.has_recent_alert(wallet_addr, token_address, within_hours=1):
            logger.debug(f"Skipping {wallet_addr[:8]}... / {token_address[:8]}... — recent alert exists")
            continue

        # ── Safety Check ────────────────────────────────────────────────
        safety = check_token_safety(token_address)
        if not safety.passed:
            logger.info(
                f"Safety failed for {token_address[:8]}... ({safety.token_symbol}): "
                f"{safety.flags}"
            )
            continue

        # ── Conviction Score ────────────────────────────────────────────
        score, components = compute_conviction(
            wallet_address=wallet_addr,
            token_address=token_address,
            wr_7d=wallet["wr_7d"],
            pnl_7d=wallet["pnl_7d"],
            safety_result=safety,
            weights=conviction_config.get("weights"),
        )

        # ── Multi-Timeframe Consistency Bonus ─────────────────────────
        consistency_count, consistency_bonus_key = get_token_consistency(token_address)
        consistency_bonus = 0
        if consistency_bonus_key == "multi_tf_strong":
            consistency_bonus = 8
        elif consistency_bonus_key == "multi_tf_medium":
            consistency_bonus = 5
        elif consistency_bonus_key == "multi_tf_weak":
            consistency_bonus = 2

        if consistency_bonus > 0:
            score += consistency_bonus
            logger.info(
                f"📊 Consistency bonus: {safety.token_symbol} found in {consistency_count}/{len(DEFAULT_TIMEFRAMES)} "
                f"timeframes → +{consistency_bonus} (new score: {score})"
            )

        if not should_alert(score, threshold=score_threshold):
            logger.debug(
                f"Alert skipped for {wallet_addr[:8]}... / {token_address[:8]}... — "
                f"score {score} < {score_threshold}"
            )
            continue

        # Dedup: skip if this token already alerted this cycle
        if token_address in seen_tokens:
            logger.debug(
                f"Alert dedup: {wallet_addr[:8]}... / {token_address[:8]}... — "
                f"token already alerted this cycle (score {score})"
            )
            # Still persist to DB for data
            db.insert_alert({
                "id": generate_alert_id(wallet_addr, token_address, last_trade.get("action", "BUY")),
                "wallet_address": wallet_addr,
                "token_address": token_address,
                "token_symbol": safety.token_symbol or token_address[:8],
                "action": last_trade.get("action", "BUY"),
                "amount_sol": round(last_trade.get("amount_sol", 0), 4),
                "conviction_score": score,
                "consistency_count": consistency_count,
                "consistency_bonus": consistency_bonus,
                "wr_7d": wallet["wr_7d"],
                "pnl_7d": wallet["pnl_7d"],
                "trades_7d": wallet["trades_7d"],
                "open_count": 0,
                "flags": safety.flags,
                "gmgn_link": build_gmgn_link(token_address),
                "photon_link": "",
                "alert_timestamp": datetime.utcnow().isoformat(),
            })
            continue
        seen_tokens.add(token_address)

        # ── Build Alert ─────────────────────────────────────────────────
        action = last_trade.get("action", "BUY")
        amount_sol = last_trade.get("amount_sol", 0)
        alert_id = generate_alert_id(wallet_addr, token_address, action)

        # ── Staleness check: verify wallet hasn't already exited ────────
        if action.upper() == "BUY":
            recent_activity = fetch_wallet_activity(wallet_addr, limit=10)
            already_sold = False
            for activity in recent_activity:
                act_token = activity.get("token", {}).get("address", "") if isinstance(activity.get("token"), dict) else ""
                act_event = activity.get("event_type", "").lower()
                if act_token == token_address and act_event == "sell":
                    already_sold = True
                    break
            if already_sold:
                logger.info(
                    f"⏭️ Staleness skip: {wallet_addr[:8]}... bought {safety.token_symbol} "
                    f"but already sold — signal stale, skipping"
                )
                continue

        # Get avg trade size for photon threshold
        avg_trade_sol = 0
        if wallet["trades_7d"] > 0 and wallet["pnl_7d"] > 0:
            # Rough estimate
            avg_trade_sol = abs(wallet["pnl_7d"]) / wallet["trades_7d"] if wallet["trades_7d"] > 0 else 0
        avg_trade_sol = max(avg_trade_sol, 0.1)  # Floor

        alert = {
            "id": alert_id,
            "wallet_address": wallet_addr,
            "token_address": token_address,
            "token_symbol": safety.token_symbol or token_address[:8],
            "action": action,
            "amount_sol": round(amount_sol, 4),
            "conviction_score": score,
            "consistency_count": consistency_count,
            "consistency_bonus": consistency_bonus,
            "consistency_bonus_key": consistency_bonus_key or "",
            "wr_7d": wallet["wr_7d"],
            "pnl_7d": wallet["pnl_7d"],
            "trades_7d": wallet["trades_7d"],
            "open_count": 0,  # Would need wallet-specific position tracking
            "flags": safety.flags,
            "gmgn_link": build_gmgn_link(token_address),
            "photon_link": (
                build_photon_link(token_address)
                if should_include_photon(score, amount_sol, avg_trade_sol)
                else ""
            ),
            "alert_timestamp": datetime.utcnow().isoformat(),
        }

        # Send alert first (so has_recent_alert doesn't block itself)
        success = alert_sender.send_alert(alert)
        
        # Persist to DB
        db.insert_alert(alert)
        
        # ── GWAS → Solauto Bridge ────────────────────────────────────────
        # Feed high-conviction signals to Solauto for autonomous execution
        solauto_threshold = int(os.environ.get("SOLAUTO_BRIDGE_THRESHOLD",
                                str(bridge_config.get("threshold", 70))))
        if score >= solauto_threshold:
            try:
                import json as _json
                signal_dir = "/opt/solauto/signals"
                os.makedirs(signal_dir, exist_ok=True)
                signal_file = os.path.join(signal_dir, f"gwas_{alert_id}.json")
                # V10: GWAS sends RAW wallet/token data only — NO conviction_score.
                # Solauto's ConvictionEngine is the sole decision maker.
                signal_data = {
                    "source": "gwas",
                    "gwas_alert_id": alert_id,
                    "token_address": token_address,
                    "token_symbol": safety.token_symbol or token_address[:8],
                    "wallet_address": wallet_addr,
                    "wallet_short": wallet_addr[:6] + "..." + wallet_addr[-4:],
                    "wallet_quality_score": wallet.get("quality_score", 50),
                    "wr_7d": wallet["wr_7d"],
                    "pnl_7d": wallet["pnl_7d"],
                    "trades_7d": wallet["trades_7d"],
                    "amount_sol": round(last_trade.get("amount_sol", 0), 4),
                    "action": action,
                    "flags": safety.flags,
                    "timestamp": datetime.utcnow().isoformat(),
                }
                with open(signal_file, "w") as f:
                    _json.dump(signal_data, f)
                logger.info(
                    f"🔗 GWAS→Solauto bridge: {safety.token_symbol} wallet_q={wallet.get('quality_score', 50)} → {signal_file}"
                )
            except Exception as e:
                logger.warning(f"GWAS→Solauto bridge write failed: {e}")

        # ── Whitelist Bridge (bypass threshold) ──────────────────────────
        # Top wallets always bridged to Solauto for copy-trade simulation,
        # regardless of conviction score. Written as whitelist_{id}.json
        if bridge_whitelist_set and wallet_addr in bridge_whitelist_set and action.upper() == "BUY":
            try:
                import json as _json
                signal_dir = "/opt/solauto/signals"
                os.makedirs(signal_dir, exist_ok=True)
                wl_file = os.path.join(signal_dir, f"whitelist_{alert_id}.json")
                # V10: Whitelist sends RAW wallet/token data only — NO conviction_score.
                # Solauto's ConvictionEngine is the sole decision maker.
                wl_data = {
                    "source": "gwas_whitelist",
                    "whitelist_alert_id": alert_id,
                    "token_address": token_address,
                    "token_symbol": safety.token_symbol or token_address[:8],
                    "wallet_address": wallet_addr,
                    "wallet_short": wallet_addr[:6] + "..." + wallet_addr[-4:],
                    "wallet_quality_score": wallet.get("quality_score", 50),
                    "wr_7d": wallet["wr_7d"],
                    "pnl_7d": wallet["pnl_7d"],
                    "trades_7d": wallet["trades_7d"],
                    "amount_sol": round(last_trade.get("amount_sol", 0), 4),
                    "action": action,
                    "timestamp": datetime.utcnow().isoformat(),
                }
                with open(wl_file, "w") as f:
                    _json.dump(wl_data, f)
                logger.info(
                    f"⭐ Whitelist bridge: {safety.token_symbol} by {wallet_addr[:8]}... wallet_q={wallet.get('quality_score', 50)} → {wl_file}"
                )
            except Exception as e:
                logger.warning(f"Whitelist bridge write failed: {e}")
        
        if success:
            alerts_sent += 1
            logger.info(
                f"✅ Alert #{alert_id} sent: {wallet_addr[:8]}... → {safety.token_symbol} "
                f"score={score} WR={wallet['wr_7d']:.0f}% PnL={wallet['pnl_7d']:.1f}"
            )

        # ── Rate limit check ────────────────────────────────────────────
        if alerts_sent >= rate_config.get("per_cycle", 10):
            logger.info(f"Cycle limit reached ({alerts_sent} alerts)")
            break

    # ── Step 6: Check exit conditions (CB-G5) ────────────────────────────
    exit_threshold = dead_config.get("auto_recommend_threshold", 0.20)
    dead_wallets = db.get_dead_wallets(
        threshold_alerts=dead_config.get("threshold_alerts", 15),
        min_window_weeks=dead_config.get("min_window_weeks", 2),
    )

    # Also check active wallets for exit conditions
    active_wallets = db.get_active_wallets()
    for wallet in active_wallets:
        if check_exit_conditions(
            wallet["address"],
            min_wr=quality.get("min_wr", 30),
            min_pnl=quality.get("min_pnl", 0),
        ):
            # Find unexecuted alerts for this wallet's tokens
            with db._connection() as conn:
                token_rows = conn.execute(
                    "SELECT DISTINCT token_address, token_symbol FROM alerts WHERE wallet_address = ? AND executed = FALSE",
                    (wallet["address"],),
                ).fetchall()
                for row in token_rows:
                    alert_sender.send_exit_alert(
                        wallet["address"], row["token_address"], row["token_symbol"]
                    )
                    exit_alerts_sent += 1
                    logger.info(f"Exit alert sent for wallet {wallet['address'][:8]}...")

    # Mark dead wallets
    if len(dead_wallets) / max(len(wallets), 1) >= exit_threshold:
        for dw in dead_wallets:
            db.mark_wallet_status(dw["address"], "DEAD")
            logger.info(f"Marked wallet {dw['address'][:8]}... as DEAD")

    cycle_duration = (datetime.utcnow() - cycle_start).total_seconds()
    logger.info(
        f"=== Scan cycle complete: {alerts_sent} alerts, "
        f"{exit_alerts_sent} exit alerts, {cycle_duration:.1f}s ==="
    )


def main():
    parser = argparse.ArgumentParser(description="GWAS v2.0 Wallet Scanner")
    parser.add_argument("--once", action="store_true", help="Run single cycle and exit")
    parser.add_argument("--interval", type=int, default=300, help="Seconds between cycles (default: 300)")
    args = parser.parse_args()

    # Load config
    config = load_config()
    setup_logging(config)
    logger = logging.getLogger(__name__)

    logger.info("GWAS v2.0 Scanner starting...")

    # Initialize DB
    db = Database(config.get("database", {}).get("path", "/opt/gwas/data/gwas.db"))

    # Initialize alert sender
    telegram_config = config.get("telegram", {})
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not bot_token:
        logger.error("TELEGRAM_BOT_TOKEN not set in environment!")
        # Continue anyway, alerts will just fail to send

    rate_config = config.get("rate_limits", {})
    rate_limiter = RateLimiter(
        per_cycle=rate_config.get("per_cycle", 10),
        per_hour=rate_config.get("per_hour", 8),
        per_day=rate_config.get("per_day", 40),
        burst_cooldown_minutes=rate_config.get("burst_cooldown_minutes", 30),
    )
    alert_sender = AlertSender(
        bot_token=bot_token,
        user_id=telegram_config.get("user_id", ""),
        rate_limiter=rate_limiter,
    )

    logger.info(f"Bot token: {'set' if bot_token else 'NOT SET'}, User ID: {telegram_config.get('user_id', 'NOT SET')}")

    # Graceful shutdown
    running = True

    def _shutdown(sig, frame):
        nonlocal running
        logger.info(f"Received signal {sig}, shutting down...")
        running = False

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    # Main loop
    while running:
        try:
            run_scan_cycle(config, alert_sender, db)
        except Exception as e:
            logger.exception(f"Scan cycle failed: {e}")

        if args.once:
            break

        if running:
            logger.info(f"Sleeping {args.interval}s until next cycle...")
            time.sleep(args.interval)


if __name__ == "__main__":
    main()
