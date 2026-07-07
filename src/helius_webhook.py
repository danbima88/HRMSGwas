"""
GWAS v2.0 — Helius Webhook Server (Flask)
Receives Helius webhook events, verifies HMAC signatures (CB-G3),
and processes transactions for trade correlation.

CB-G3: HMAC signature verification + Helius IP allowlist.
"""

import os
import json
import hmac
import hashlib
import logging
import sqlite3
import threading
import time
from datetime import datetime

from flask import Flask, request, jsonify

from .correlator import extract_trade_from_webhook, process_webhook_trades
from .safety import is_helius_ip

logger = logging.getLogger(__name__)

app = Flask("gwas_webhook")

# Config — loaded from settings at startup
WEBHOOK_SECRET = os.environ.get("HELIUS_WEBHOOK_SECRET", "gwas-v1-default")

# Solauto real-time bridge: signal directory for webhook → Solauto forwarding
SOLAUTO_SIGNAL_DIR = "/opt/solauto/data/signals"
os.makedirs(SOLAUTO_SIGNAL_DIR, exist_ok=True)
os.makedirs(os.path.join(SOLAUTO_SIGNAL_DIR, "processed"), exist_ok=True)

GWAS_DB_PATH = "/opt/gwas/data/gwas.db"

# Tracked wallet cache (refreshed periodically)
_tracked_wallets: dict[str, float] = {}  # address → quality_score
_tracked_lock = threading.Lock()


def _get_tracked_wallets() -> dict[str, float]:
    """Load tracked smart-money wallets from GWAS DB."""
    global _tracked_wallets
    with _tracked_lock:
        if _tracked_wallets:
            return _tracked_wallets
        try:
            conn = sqlite3.connect(GWAS_DB_PATH)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT address, quality_score FROM wallets WHERE status='ACTIVE'"
            ).fetchall()
            conn.close()
            _tracked_wallets = {r["address"]: r["quality_score"] or 0 for r in rows}
            logger.info(f"Loaded {len(_tracked_wallets)} tracked wallets from GWAS DB")
        except Exception as e:
            logger.error(f"Error loading tracked wallets: {e}")
        return _tracked_wallets


def _refresh_wallet_cache():
    """Periodic cache refresh thread."""
    global _tracked_wallets
    while True:
        time.sleep(300)  # refresh every 5 minutes
        with _tracked_lock:
            _tracked_wallets = {}
        _get_tracked_wallets()


_refresh_thread = threading.Thread(target=_refresh_wallet_cache, daemon=True)
_refresh_thread.start()


def _parse_webhook_tx(tx: dict) -> dict | None:
    """Extract trade info from a single Helius enhanced transaction.
    
    Returns dict with wallet_address, token_address, action, amount_sol, tx_hash,
    or None if not a trade we care about.
    """
    tx_type = tx.get("type", "")
    if tx_type not in ("SWAP", "TRANSFER", "UNKNOWN", None):
        return None

    tx_hash = tx.get("signature") or tx.get("transaction", "")
    if not tx_hash:
        return None

    wallet_address = tx.get("feePayer", "")
    if not wallet_address:
        return None

    # Extract token address from tokenTransfers
    token_address = ""
    for transfer in tx.get("tokenTransfers", []) or []:
        token_address = transfer.get("mint", "") or token_address

    # Determine action and amount
    amount_sol = 0.0
    action = "UNKNOWN"

    # Check events.swap first (most reliable)
    events = tx.get("events", {}) or {}
    swap = events.get("swap", {})
    if swap:
        native_in = float(swap.get("nativeInput", 0) or 0) / 1e9
        native_out = float(swap.get("nativeOutput", 0) or 0) / 1e9
        if native_in > 0:
            action = "BUY"
            amount_sol = native_in
        elif native_out > 0:
            action = "SELL"
            amount_sol = native_out

    # Fallback: accountData nativeBalanceChange
    if action == "UNKNOWN":
        account_data = tx.get("accountData", []) or []
        for acct in account_data:
            if acct.get("account") == wallet_address:
                native_change = float(acct.get("nativeBalanceChange", 0) or 0) / 1e9
                if native_change > 0:
                    action = "SELL"
                    amount_sol = native_change
                elif native_change < 0:
                    action = "BUY"
                    amount_sol = abs(native_change)

    # Fallback: description
    if action == "UNKNOWN":
        description = (tx.get("description", "") or "").lower()
        if "bought" in description or "buy" in description:
            action = "BUY"
        elif "sold" in description or "sell" in description:
            action = "SELL"

    # Get amount from tokenTransfers if still 0
    if amount_sol == 0.0:
        for transfer in tx.get("tokenTransfers", []) or []:
            raw_amount = float(transfer.get("tokenAmount", 0) or 0)
            decimals = transfer.get("decimals", 0) or 0
            if decimals > 0:
                amount_sol = max(amount_sol, raw_amount / (10 ** decimals))
            else:
                amount_sol = max(amount_sol, raw_amount)

    if not token_address or action == "UNKNOWN":
        return None

    return {
        "wallet_address": wallet_address,
        "token_address": token_address,
        "action": action,
        "amount_sol": amount_sol,
        "tx_hash": tx_hash,
    }


def forward_to_solauto(transactions: list[dict]) -> int:
    """Forward buy/sell signals from webhook transactions to Solauto.
    
    Runs in a background thread to avoid blocking the webhook response.
    Returns number of signals forwarded.
    """
    try:
        tracked = _get_tracked_wallets()
        forwarded = 0
        now = datetime.utcnow()

        for tx in transactions:
            parsed = _parse_webhook_tx(tx)
            if not parsed:
                continue

            wallet = parsed["wallet_address"]
            token = parsed["token_address"]
            action = parsed["action"]

            # Check if wallet is tracked
            quality = tracked.get(wallet)
            if quality is None:
                continue

            # For BUY: skip low-quality wallets
            if action == "BUY" and quality < 55:
                logger.debug(
                    f"⏭️ Webhook BUY from {wallet[:12]}...: quality={quality:.0f} < 55, skip"
                )
                continue

            # Build signal filename: timestamp_token.json
            ts_str = now.strftime("%Y%m%d_%H%M%S_%f")
            safe_token = token[:16] if len(token) > 16 else token
            fname = f"webhook_{ts_str}_{safe_token}.json"
            fpath = os.path.join(SOLAUTO_SIGNAL_DIR, fname)

            signal = {
                "timestamp": now.isoformat(),
                "wallet_address": wallet,
                "token_address": token,
                "action": action,
                "amount_sol": parsed["amount_sol"],
                "quality_score": quality,
                "source": "helius_webhook",
                "tx_hash": parsed["tx_hash"],
            }

            try:
                with open(fpath, "w") as f:
                    json.dump(signal, f)
                forwarded += 1
                logger.info(
                    f"🔔 Webhook → Solauto: {action} {token[:12]}... from wallet "
                    f"{wallet[:12]}... score={quality:.0f}"
                )
            except OSError as e:
                logger.error(f"Failed to write signal {fpath}: {e}")

        return forwarded
    except Exception as e:
        logger.error(f"Error in forward_to_solauto: {e}", exc_info=True)
        return 0


def verify_helius_signature(payload: bytes, signature_header: str) -> bool:
    """
    CB-G3: Verify HMAC-SHA256 signature from Helius webhook.
    Helius signs with x-helius-signature header.
    """
    if not signature_header:
        logger.warning("Missing x-helius-signature header")
        return False
    try:
        computed = hmac.new(
            WEBHOOK_SECRET.encode("utf-8"),
            payload,
            hashlib.sha256,
        ).hexdigest()
        return hmac.compare_digest(computed, signature_header)
    except Exception as e:
        logger.error(f"HMAC verification error: {e}")
        return False


def verify_helius_ip() -> bool:
    """CB-G3: Verify request comes from a known Helius IP range."""
    forwarded = request.headers.get("X-Forwarded-For", "")
    client_ip = forwarded.split(",")[0].strip() if forwarded else request.remote_addr

    if not client_ip:
        # Can't verify, but in development we allow
        return os.environ.get("GWAS_DEV_MODE", "") == "1"

    if is_helius_ip(client_ip):
        return True

    logger.warning(f"Request from non-Helius IP: {client_ip}")
    # In dev mode, allow through
    return os.environ.get("GWAS_DEV_MODE", "") == "1"


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "timestamp": datetime.utcnow().isoformat()})


@app.route("/webhook", methods=["POST"])
def webhook():
    """
    Main Helius webhook endpoint.
    Verifies signature, extracts trades, correlates with alerts.
    """
    raw_body = request.get_data()

    # CB-G3: HMAC signature verification
    signature = request.headers.get("x-helius-signature", "")
    if not verify_helius_signature(raw_body, signature):
        logger.warning("Webhook rejected: invalid signature")
        return jsonify({"error": "invalid signature"}), 401

    # CB-G3: IP allowlist check
    if not verify_helius_ip():
        logger.warning("Webhook rejected: non-Helius IP")
        return jsonify({"error": "unauthorized IP"}), 403

    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError as e:
        logger.error(f"Invalid JSON in webhook: {e}")
        return jsonify({"error": "invalid json"}), 400

    # Helius sends an array of transaction objects
    transactions = payload if isinstance(payload, list) else payload.get("transactions", [payload])

    if not transactions:
        return jsonify({"status": "ok", "message": "no transactions"})

    logger.info(f"Webhook received: {len(transactions)} transactions")

    # Extract and process trades
    trades = extract_trade_from_webhook(transactions)
    if trades:
        correlated = process_webhook_trades(trades)
        logger.info(f"Correlated {correlated} / {len(trades)} trades")
    else:
        correlated = 0

    # ── Forward signals to Solauto real-time bridge (fire-and-forget) ──
    try:
        fwd = forward_to_solauto(transactions)
        if fwd:
            logger.info(f"🚀 Forwarded {fwd} webhook signals to Solauto")
    except Exception as e:
        logger.error(f"Solauto forward error: {e}")

    return jsonify({
        "status": "ok",
        "trades_processed": len(trades) if trades else 0,
        "trades_correlated": correlated,
    })


@app.route("/webhook/direct", methods=["POST"])
def webhook_direct():
    """
    Alternative endpoint for raw transaction data without Helius envelope.
    Used for direct testing and manual correlation.
    """
    raw_body = request.get_data()
    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError:
        return jsonify({"error": "invalid json"}), 400

    transactions = payload if isinstance(payload, list) else [payload]
    trades = extract_trade_from_webhook(transactions)
    if trades:
        correlated = process_webhook_trades(trades)
    else:
        correlated = 0

    # Forward to Solauto
    try:
        fwd = forward_to_solauto(transactions)
        if fwd:
            logger.info(f"🚀 Direct: Forwarded {fwd} signals to Solauto")
    except Exception as e:
        logger.error(f"Solauto forward error (direct): {e}")

    return jsonify({
        "status": "ok",
        "trades_processed": len(trades) if trades else 0,
        "trades_correlated": correlated,
    })


def create_app(webhook_secret: str = None) -> Flask:
    """Create and configure the Flask app."""
    global WEBHOOK_SECRET
    if webhook_secret:
        WEBHOOK_SECRET = webhook_secret
        os.environ["HELIUS_WEBHOOK_SECRET"] = webhook_secret
    return app


def run_server(host: str = "0.0.0.0", port: int = 8080, secret: str = None):
    """Run the webhook server (blocking)."""
    if secret:
        global WEBHOOK_SECRET
        WEBHOOK_SECRET = secret

    logger.info(f"Starting Helius webhook server on {host}:{port}")
    app.run(host=host, port=port, debug=False)
