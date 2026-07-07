"""
GWAS v2.0 — Telegram Alert Formatter & Sender
Hybrid format (Option C): compact 1-line preview + expanded full context.
Includes EXIT_ALERT support (CB-G5).
"""

import logging
import hashlib
import os
import time
from datetime import datetime, timedelta
from typing import Optional

from .db import get_db

logger = logging.getLogger(__name__)


# ─── Alert ID Generation ────────────────────────────────────────────────

def generate_alert_id(wallet_address: str, token_address: str, action: str) -> str:
    """Generate a unique alert ID."""
    ts = str(int(time.time() * 1000))
    raw = f"{wallet_address}:{token_address}:{action}:{ts}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


# ─── Link Builders ──────────────────────────────────────────────────────

def build_gmgn_link(token_address: str) -> str:
    return f"https://gmgn.ai/token/{token_address}"


def build_photon_link(token_address: str) -> str:
    return f"https://photon-sol.tinyastro.io/en/lp/{token_address}"


# ─── Rate Limiter ───────────────────────────────────────────────────────

class RateLimiter:
    """Tracks alert rate limits. Checks DB for burst cooldown."""

    def __init__(self, per_cycle: int = 10, per_hour: int = 8, per_day: int = 40,
                 burst_cooldown_minutes: int = 30, burst_threshold: int = 5,
                 burst_window_minutes: int = 10):
        self.per_cycle = per_cycle
        self.per_hour = per_hour
        self.per_day = per_day
        self.burst_cooldown_minutes = burst_cooldown_minutes
        self.burst_threshold = burst_threshold
        self.burst_window_minutes = burst_window_minutes
        self.cycle_start: Optional[datetime] = None

    def start_cycle(self):
        self.cycle_start = datetime.utcnow()

    def can_send(self, db) -> tuple[bool, str]:
        """Returns (can_send, reason_if_blocked)."""
        now = datetime.utcnow()

        # Check per_day
        today_count = db.count_alerts_today()
        if today_count >= self.per_day:
            return False, f"Daily limit reached ({self.per_day})"

        # Check per_hour
        hour_count = db.count_alerts_this_hour()
        if hour_count >= self.per_hour:
            return False, f"Hourly limit reached ({self.per_hour})"

        # Check per_cycle
        if self.cycle_start:
            cycle_count = db.count_alerts_this_cycle(self.cycle_start)
            if cycle_count >= self.per_cycle:
                return False, f"Cycle limit reached ({self.per_cycle})"

        # Check burst cooldown
        if db.is_burst_cooldown(
            cooldown_minutes=self.burst_cooldown_minutes,
            burst_threshold=self.burst_threshold,
            burst_window=self.burst_window_minutes,
        ):
            return False, f"Burst cooldown active ({self.burst_cooldown_minutes}min)"

        return True, "ok"

    def record_send(self, db):
        db.log_alert_sent()


# ─── Alert Formatter ────────────────────────────────────────────────────

def format_alert_compact(alert: dict) -> str:
    """
    Compact 1-line preview:
    "🔔 GWAS: Wallet `93kgxY...` buy drooling — WR 68% | PnL 6433.6 SOL | Score 60/100"
    """
    addr_short = alert["wallet_address"][:6] + "..."
    symbol = alert.get("token_symbol", "") or alert["token_address"][:8]
    action = alert.get("action", "BUY")
    wr = alert.get("wr_7d", 0)
    pnl = alert.get("pnl_7d", 0)
    score = alert.get("conviction_score", 0)

    return (
        f"🔔 GWAS: `{addr_short}` {action} {symbol} — "
        f"WR {wr:.0f}% | PnL {pnl:,.1f} SOL | Score {score}/100"
    )


def format_alert_full(alert: dict) -> str:
    """
    Clean full alert — wallet in code block for easy copy.
    """
    a = alert
    symbol = a.get("token_symbol", "?")
    token = a["token_address"]
    score = a.get("conviction_score", 0)
    wr = a.get("wr_7d", 0)
    pnl = a.get("pnl_7d", 0)
    trades = a.get("trades_7d", 0)
    action = a.get("action", "BUY")
    size = a.get("amount_sol", 0)
    flags_str = ", ".join(a.get("flags", [])) or "none"
    gmgn = a.get("gmgn_link", "")
    photon = a.get("photon_link", "")

    lines = [
        f"🔔 GWAS #{a.get('id', '?')[:8]} • {symbol} • Score {score}/100",
        f"```\n{a['wallet_address']}\n```",
        f"{action} {size:.2f} SOL | WR {wr:.0f}% | PnL {pnl:,.1f} SOL | {trades} trades",
    ]
    if flags_str != "none":
        lines.append(f"⚠️ {flags_str}")
    links = []
    if gmgn:
        links.append(f"[GMGN]({gmgn})")
    if photon:
        links.append(f"[Photon]({photon})")
    if links:
        lines.append(" | ".join(links))

    return "\n".join(lines)


def format_exit_alert(wallet_address: str, token_address: str, token_symbol: str) -> str:
    """
    CB-G5: EXIT_ALERT when a wallet is removed but we still hold positions.
    """
    addr_short = wallet_address[:6] + "..."
    return (
        f"⚠️ GWAS EXIT: Wallet `{wallet_address}` removed. "
        f"Still holding {token_symbol} (`{token_address[:8]}...`) — consider manual exit.\n"
        f"Wallet {addr_short} WR dropped or PnL negative."
    )


# ─── Alert Sender ───────────────────────────────────────────────────────

class AlertSender:
    """
    Sends formatted alerts to Telegram.
    Uses dedicated bot if TELEGRAM_BOT_TOKEN is set,
    otherwise saves to file for Hermes NOTIFICATION-RELAY cron pickup.
    """

    PENDING_DIR = "/opt/gwas/data/pending_alerts"

    def __init__(self, bot_token: str, user_id: str, rate_limiter: Optional[RateLimiter] = None):
        self.bot_token = bot_token
        self.user_id = user_id
        self.rate_limiter = rate_limiter or RateLimiter()
        self.use_file_relay = not bot_token
        if self.use_file_relay:
            os.makedirs(self.PENDING_DIR, exist_ok=True)
            logger.info(f"AlertSender using FILE RELAY (no bot token) — alerts saved to {self.PENDING_DIR}")

    def _save_to_file(self, text: str, category: str = "alert") -> bool:
        """Save alert as JSON file for Hermes cron pickup."""
        import json as _json
        ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S_%f")
        filename = f"{category}_{ts}.json"
        filepath = os.path.join(self.PENDING_DIR, filename)
        payload = {
            "user_id": self.user_id,
            "text": text,
            "category": category,
            "timestamp": datetime.utcnow().isoformat(),
        }
        try:
            with open(filepath, "w") as f:
                _json.dump(payload, f)
            return True
        except OSError as e:
            logger.error(f"Failed to save alert file: {e}")
            return False

    def _send_telegram(self, text: str) -> bool:
        """Send via Telegram Bot API, with file relay fallback."""
        if self.use_file_relay:
            return self._save_to_file(text)

        import requests
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        payload = {
            "chat_id": self.user_id,
            "text": text,
            "parse_mode": "Markdown",
            "disable_web_page_preview": False,
        }
        try:
            # Use data= not json= — Telegram API silently 400s with json=
            resp = requests.post(url, data=payload, timeout=10)
            if resp.status_code == 200:
                return True
            else:
                logger.error(f"Telegram send failed: {resp.status_code} {resp.text}")
                # Retry without markdown
                payload["parse_mode"] = ""
                resp2 = requests.post(url, data=payload, timeout=10)
                if resp2.status_code == 200:
                    return True
                # Fallback to file
                logger.warning("Telegram failed, falling back to file relay")
                return self._save_to_file(text)
        except requests.RequestException as e:
            logger.error(f"Telegram send exception: {e}, falling back to file relay")
            return self._save_to_file(text)

    def send_alert(self, alert: dict) -> bool:
        """Send a full alert (compact preview + expanded). SELL alerts are suppressed from Telegram."""
        db = get_db()

        # SELL alerts → skip Telegram (still go to DB + Solauto bridge)
        action = alert.get("action", "BUY").upper()
        if action == "SELL":
            logger.info(f"SELL alert suppressed from Telegram: #{alert.get('id', '?')}")
            return False

        # Rate limit check
        can_send, reason = self.rate_limiter.can_send(db)
        if not can_send:
            logger.info(f"Alert suppressed: {reason}")
            return False

        # Check for duplicate (same wallet+token in last hour)
        if db.has_recent_alert(alert["wallet_address"], alert["token_address"], within_hours=1):
            logger.debug(f"Duplicate alert suppressed: {alert['wallet_address'][:8]}... / {alert['token_address'][:8]}...")
            return False

        compact = format_alert_compact(alert)
        full = format_alert_full(alert)
        message = compact + "\n\n" + full

        success = self._send_telegram(message)
        if success:
            self.rate_limiter.record_send(db)
            logger.info(f"Alert sent: #{alert.get('id', '?')} score={alert.get('conviction_score', 0)}")
        return success

    def send_exit_alert(self, wallet_address: str, token_address: str, token_symbol: str = "?") -> bool:
        """CB-G5: Send exit alert."""
        text = format_exit_alert(wallet_address, token_address, token_symbol)
        return self._send_telegram(text)

    def send_report(self, report_text: str) -> bool:
        """Send a weekly report."""
        return self._send_telegram(report_text)

    def send_simple(self, text: str) -> bool:
        """Send a simple text message."""
        return self._send_telegram(text)
