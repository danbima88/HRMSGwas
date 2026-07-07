"""
GWAS v2.0 — Helius Webhook → Alert Correlator
Matches executed trades from Helius webhooks to GWAS alerts.
Default 4h correlation window, extendable to 24h via "✅ taken" reply.
"""

import logging
import re
from datetime import datetime, timedelta
from typing import Optional

from .db import get_db

logger = logging.getLogger(__name__)

DEFAULT_WINDOW_HOURS = 4
MANUAL_EXTEND_HOURS = 24


def _parse_amount_from_instruction(tx_data: dict) -> tuple[float, str]:
    """
    Extract amount in SOL and action (BUY/SELL) from Helius transaction data.
    """
    amount_sol = 0.0
    action = "UNKNOWN"

    # Helius enhanced transactions have tokenTransfers
    transfers = tx_data.get("tokenTransfers", []) or []
    for transfer in transfers:
        raw_amount = float(transfer.get("tokenAmount", 0) or 0)
        decimals = transfer.get("decimals", 0) or 0
        if decimals > 0:
            amount = raw_amount / (10 ** decimals)
        else:
            amount = raw_amount

        if amount > amount_sol:  # Take largest transfer
            amount_sol = amount

    # Determine action from account data or instruction
    account_data = tx_data.get("accountData", []) or []
    for acct in account_data:
        if acct.get("account") == tx_data.get("feePayer"):
            native_change = float(acct.get("nativeBalanceChange", 0) or 0) / 1e9
            if native_change > 0:
                action = "SELL"
            elif native_change < 0:
                action = "BUY"

    # Check description for swap info
    description = tx_data.get("description", "") or ""
    if "bought" in description.lower() or "buy" in description.lower():
        action = "BUY"
    elif "sold" in description.lower() or "sell" in description.lower():
        action = "SELL"

    # Also check events
    events = tx_data.get("events", {}) or {}
    swap = events.get("swap", {})
    if swap:
        native_out = float(swap.get("nativeOutput", 0) or 0) / 1e9
        native_in = float(swap.get("nativeInput", 0) or 0) / 1e9
        if native_in > 0:
            action = "BUY"
            amount_sol = native_in
        elif native_out > 0:
            action = "SELL"
            amount_sol = native_out

    return amount_sol, action


def extract_trade_from_webhook(webhook_data: list[dict]) -> list[dict]:
    """
    Extract trade information from Helius webhook transaction data.
    Returns list of trade dicts.
    """
    trades = []
    for tx in webhook_data:
        if tx.get("type") not in ("SWAP", "TRANSFER", "UNKNOWN", None):
            continue

        tx_hash = tx.get("signature") or tx.get("transaction", "")
        if not tx_hash:
            continue

        timestamp_str = tx.get("timestamp")
        if timestamp_str:
            try:
                ts = datetime.fromtimestamp(timestamp_str / 1000.0 if timestamp_str > 1e12 else timestamp_str)
                timestamp = ts.isoformat()
            except (ValueError, OSError):
                timestamp = datetime.utcnow().isoformat()
        else:
            timestamp = datetime.utcnow().isoformat()

        amount_sol, action = _parse_amount_from_instruction(tx)

        # Get token address from transfers
        token_address = ""
        for transfer in tx.get("tokenTransfers", []) or []:
            token_address = transfer.get("mint", "") or token_address

        # Fee calculation
        fee_lamports = int(tx.get("fee", 0) or 0)
        fee_sol = fee_lamports / 1e9

        trades.append({
            "tx_hash": tx_hash,
            "wallet_address": tx.get("feePayer", ""),
            "token_address": token_address,
            "action": action,
            "amount_sol": amount_sol,
            "price_usd": 0,  # Not easily available from webhook
            "pnl_sol": 0,    # PnL available later from GMGN API
            "fee_sol": fee_sol,
            "timestamp": timestamp,
            "correlated_alert_id": None,
        })

    return trades


def correlate_trade(trade: dict, window_hours: int = DEFAULT_WINDOW_HOURS) -> Optional[str]:
    """
    Try to match a trade to a GWAS alert.
    CRITICAL: The webhook monitors the USER's wallet, not the smart money wallets.
    Correlation matches by TOKEN_ADDRESS only within the time window,
    NOT by wallet_address (user wallet ≠ smart money wallet).
    Returns alert_id if matched, None otherwise.
    """
    db = get_db()
    trade_ts = datetime.fromisoformat(trade["timestamp"])
    cutoff = trade_ts - timedelta(hours=window_hours)

    # Find ALL unexecuted alerts for this token (any wallet) within window
    # The user copied the smart money's signal → we match by token + time
    all_token_alerts = db.get_alerts_for_token_wallet(trade["token_address"], None)
    
    # If no DB method suports token-only, fallback: query all unexecuted alerts
    if not all_token_alerts:
        all_token_alerts = db.get_unexecuted_alerts_since(cutoff)
        all_token_alerts = [a for a in all_token_alerts if a.get("token_address") == trade["token_address"]]

    for alert in all_token_alerts:
        if alert.get("executed"):
            continue
        alert_ts = datetime.fromisoformat(alert["alert_timestamp"])
        if alert_ts >= cutoff and alert_ts <= trade_ts:
            smart_wallet = alert.get("wallet_address", "")[:10]
            logger.info(
                f"✅ Correlated user trade {trade['tx_hash'][:8]}... → GWAS alert {alert['id'][:12]} "
                f"(smart wallet {smart_wallet}..., within {window_hours}h)"
            )
            return alert["id"]

    return None


def process_webhook_trades(trades: list[dict], window_hours: int = DEFAULT_WINDOW_HOURS) -> int:
    """
    Process trades from webhook:
    1. Insert into trades table
    2. Try to correlate with alerts
    3. Mark correlated alerts as executed

    Returns number of trades correlated.
    """
    db = get_db()
    correlated_count = 0

    for trade in trades:
        # Skip trades without a token (SOL transfers, etc.)
        if not trade["token_address"]:
            db.insert_trade(trade)
            continue

        # Try correlation
        alert_id = correlate_trade(trade, window_hours=window_hours)
        if alert_id:
            trade["correlated_alert_id"] = alert_id
            db.mark_alert_executed(alert_id, trade["tx_hash"], trade["timestamp"])
            correlated_count += 1

        db.insert_trade(trade)

    logger.info(f"Processed {len(trades)} trades, correlated {correlated_count}")
    return correlated_count


def manual_extend_correlation(alert_id: str, user_wallet: str, extend_hours: int = MANUAL_EXTEND_HOURS) -> int:
    """
    Manual extension via Telegram "✅ taken" reply.
    Re-checks recent trades with extended window.
    Returns number of new correlations.
    """
    db = get_db()
    alert = db.get_alert_by_id(alert_id)
    if not alert or alert.get("executed"):
        return 0

    # Find recent trades from user wallet
    trades = db.get_recent_trades(hours=extend_hours)
    correlated = 0
    for trade in trades:
        if trade["wallet_address"] != user_wallet:
            continue
        if trade["token_address"] != alert["token_address"]:
            continue
        if trade.get("correlated_alert_id"):
            continue

        trade["correlated_alert_id"] = alert_id
        db.insert_trade(trade)  # Updates correlated_alert_id
        db.mark_alert_executed(alert_id, trade["tx_hash"], trade["timestamp"])
        correlated += 1

    return correlated
