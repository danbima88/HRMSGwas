"""
GWAS v2.0 — Conviction Scoring Engine
Scores wallet-token pairs for alert worthiness.

CB-G2 FIX: Age bonus starts at 0.5h (30min) instead of 1h.
Formula: age_hours * 0.10 where age_hours >= 0.5
"""

import logging
from datetime import datetime, timedelta
from typing import Optional

from .safety import check_token_safety, fetch_token_info, SafetyResult
from .db import get_db

logger = logging.getLogger(__name__)


# ─── Conviction Weights ─────────────────────────────────────────────────

DEFAULT_WEIGHTS = {
    "wr": 0.30,
    "pnl": 0.20,
    "win_streak": 0.15,
    "token_age_bonus": 0.10,
    "volume_consistency": 0.15,
    "wallet_diversity": 0.10,
}

SCORE_THRESHOLD = 85     # Minimum to alert (silent below, DB only)
PHOTON_THRESHOLD = 90    # Only include Photon link above this


def _clamp(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, value))


def score_wr(wr_7d: float) -> float:
    """Win rate score: linear from 0 at 30% to 1.0 at 80%+."""
    if wr_7d < 30:
        return 0.0
    if wr_7d >= 80:
        return 1.0
    return _clamp((wr_7d - 30) / 50)


def score_pnl(pnl_7d: float) -> float:
    """PnL score: 0 at 0 SOL, 1.0 at 100+ SOL. Logarithmic-ish."""
    if pnl_7d <= 0:
        return 0.0
    if pnl_7d >= 100:
        return 1.0
    import math
    return _clamp(math.log10(pnl_7d + 1) / 2)  # ~0.5 at 10 SOL, ~1.0 at 100


def score_win_streak(wallet_address: str) -> float:
    """
    Recent win streak from last 10 sell trades.
    
    Wallet_activity returns sells with cost_usd (revenue) and 
    buy_cost_usd (original purchase cost). A sell is a "win" 
    when cost_usd > buy_cost_usd (sold for profit).
    Buys are excluded — they have no PnL until sold.
    """
    from .wallet_scanner import fetch_wallet_recent_trades
    trades = fetch_wallet_recent_trades(wallet_address, limit=20)
    if not trades:
        return 0.0
    
    # Only evaluate sells — they have both cost_usd and buy_cost_usd
    sells = [t for t in trades if t.get("event_type") == "sell"]
    if len(sells) < 2:
        return 0.0
    
    wins = 0
    for t in sells:
        cost = float(t.get("cost_usd") or 0)
        buy_cost = float(t.get("buy_cost_usd") or 0)
        if buy_cost > 0 and cost > buy_cost:
            wins += 1
    
    streak_ratio = wins / len(sells)
    return _clamp(streak_ratio)


def score_token_age(age_minutes: float) -> float:
    """
    CB-G2 FIX: Age bonus from 0.5h (30min) instead of 1h.
    Formula: age_hours * 0.10 where age_hours >= 0.5.
    Max bonus at 24h+.
    """
    if age_minutes is None or age_minutes < 30:
        return 0.0
    age_hours = age_minutes / 60
    if age_hours < 0.5:
        return 0.0
    # Linear 0.0 → 1.0 from 0.5h to 24h
    if age_hours >= 24:
        return 1.0
    return _clamp((age_hours - 0.5) / 23.5)


def score_volume_consistency(wallet_address: str) -> float:
    """
    Consistency of trade sizes in USD (lower variance = higher score).
    Uses cost_usd for uniform USD comparison across all quote tokens.
    """
    from .wallet_scanner import fetch_wallet_recent_trades
    trades = fetch_wallet_recent_trades(wallet_address, limit=20)
    if len(trades) < 3:
        return 0.5  # Neutral with insufficient data

    amounts = []
    for t in trades:
        amt = float(t.get("cost_usd") or 0)
        if amt > 0:
            amounts.append(amt)

    if len(amounts) < 3:
        return 0.5

    import statistics
    mean_val = statistics.mean(amounts)
    if mean_val == 0:
        return 0.0
    try:
        cv = statistics.stdev(amounts) / mean_val  # Coefficient of variation
    except statistics.StatisticsError:
        return 0.5

    # Lower CV = more consistent = higher score
    # 1.0 at CV=0, 0.0 at CV>=2
    return _clamp(1.0 - (cv / 2))


def score_wallet_diversity(wallet_address: str) -> float:
    """
    How diverse are the wallet's recent token picks.
    token is an object {address, symbol, logo} — extract .address.
    Falls back to base_address for other endpoints.
    """
    from .wallet_scanner import fetch_wallet_recent_trades
    trades = fetch_wallet_recent_trades(wallet_address, limit=20)
    unique_tokens = set()
    for t in trades:
        # token is a dict {address, symbol, logo} in wallet_activity
        token_obj = t.get("token", {})
        if isinstance(token_obj, dict):
            token = token_obj.get("address", "")
        else:
            token = str(token_obj) if token_obj else ""
        if not token:
            token = t.get("base_address") or t.get("token_address") or t.get("mint", "")
        if token:
            unique_tokens.add(token)

    if not unique_tokens:
        return 0.0
    ratio = len(unique_tokens) / len(trades)
    # More unique tokens relative to total = better (not just churning one pump)
    return _clamp(ratio * 2)  # 0.5 ratio → 1.0 score


def compute_conviction(
    wallet_address: str,
    token_address: str,
    wr_7d: float,
    pnl_7d: float,
    safety_result: SafetyResult,
    weights: Optional[dict] = None,
) -> tuple[float, dict]:
    """
    Compute conviction score for a wallet-token pair.
    
    Returns (score_0_to_100, component_scores_dict)
    
    Components:
      - wr: win rate scaled 0-1
      - pnl: profitability scaled 0-1
      - win_streak: recent win streak 0-1
      - token_age_bonus: based on token age in hours (CB-G2: starts 0.5h)
      - volume_consistency: trade size consistency
      - wallet_diversity: token diversity
    """
    w = weights or DEFAULT_WEIGHTS

    components = {}
    components["wr"] = score_wr(wr_7d)
    components["pnl"] = score_pnl(pnl_7d)
    components["win_streak"] = score_win_streak(wallet_address)
    components["token_age_bonus"] = score_token_age(safety_result.token_age_minutes or 0)
    components["volume_consistency"] = score_volume_consistency(wallet_address)
    components["wallet_diversity"] = score_wallet_diversity(wallet_address)

    raw_score = sum(w[k] * components[k] for k in w)
    score_100 = round(raw_score * 100)  # integer 0-100

    logger.debug(
        f"Conviction for {wallet_address[:8]}... → {token_address[:8]}...: "
        f"raw={raw_score:.3f} score={score_100}"
    )

    return score_100, components


def should_alert(score: float, threshold: float = SCORE_THRESHOLD) -> bool:
    """Determine if conviction score meets alert threshold."""
    return score >= threshold


def should_include_photon(score: float, amount_sol: float, avg_trade_sol: float) -> bool:
    """
    Photon link included only if:
    - score >= 90 (PHOTON_THRESHOLD)
    - trade size > 3x wallet average
    """
    if score < PHOTON_THRESHOLD:
        return False
    if amount_sol > avg_trade_sol * 3:
        return True
    return False
