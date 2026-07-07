"""
GWAS v2.0 — Wallet Scanner
Discovers profitable wallets from GMGN API.
Filters by win rate, PnL, trade count, and quality score.
"""

import logging
import json
import requests
import yaml
from datetime import datetime, timedelta
from typing import Optional

from .db import get_db
from .safety import check_token_safety, fetch_token_info

logger = logging.getLogger(__name__)


# ─── GMGN API Configuration ─────────────────────────────────────────────

GMGN_BASE = "https://openapi.gmgn.ai"

# Sensitivity levels map to GMGN follower categories
# Actual GMGN tags: smart_degen, padre, axiom, sniper, etc.
SENSITIVITY_MAP = {
    "PURE_HUMAN": ["smart_degen"],                              # Conservative
    "LIKELY_HUMAN": ["smart_degen", "sniper"],                  # Moderate
    "MEDIUM": ["smart_degen", "sniper", "padre"],               # Standard
    "ALL": [],                                                  # No tag filter — everything
}

# ─── GMGN Auth Helpers ──────────────────────────────────────────────────
import os
import uuid
import time as _time

def _gmgn_headers():
    """Build X-APIKEY auth headers."""
    api_key = os.environ.get("GMGN_API_KEY", "")
    return {"X-APIKEY": api_key, "Content-Type": "application/json"}

def _gmgn_auth_params():
    """Build timestamp + client_id query params for GMGN OpenAPI."""
    return {
        "timestamp": int(_time.time()),
        "client_id": str(uuid.uuid4()),
    }

def _gmgn_get(endpoint: str, params: dict = None, timeout: int = 15) -> dict:
    """GET request to GMGN OpenAPI with auth params."""
    url = f"{GMGN_BASE}{endpoint}"
    all_params = {}
    all_params.update(_gmgn_auth_params())
    if params:
        all_params.update(params)
    try:
        resp = requests.get(url, params=all_params, headers=_gmgn_headers(), timeout=timeout)
        if resp.status_code == 200:
            data = resp.json()
            if data.get("code") == 0:
                return data.get("data", {})
            else:
                logger.warning(f"GMGN API error: {data.get('error')} — {data.get('message')}")
                return {}
        else:
            logger.warning(f"GMGN {endpoint} returned {resp.status_code}")
            return {}
    except requests.RequestException as e:
        logger.error(f"GMGN request failed {endpoint}: {e}")
        return {}


def _fetch_vybe_top_traders(api_key: str, resolution: str = "7d", limit: int = 1000) -> dict[str, dict]:
    """Fetch Vybe top-traders leaderboard and return a lookup dict keyed by wallet address.
    
    Uses FREE-tier endpoint: GET /v4/wallets/top-traders
    Returns dict: {wallet_address: {realized_pnl_usd, win_rate, trade_count, ...}}
    Only wallets in the top-N by realized PnL are included.
    Wallets NOT in this dict have low/negative PnL.
    
    Returns empty dict on failure.
    """
    if not api_key:
        return {}
    url = "https://api.vybenetwork.xyz/v4/wallets/top-traders"
    headers = {"X-API-KEY": api_key, "Accept": "application/json"}
    params = {"resolution": resolution, "limit": limit}
    try:
        resp = requests.get(url, headers=headers, params=params, timeout=15)
        if resp.status_code == 200:
            data = resp.json()
            traders = data.get("data", [])
            lookup = {}
            for t in traders:
                addr = t.get("accountAddress", "")
                metrics = t.get("metrics", {})
                if addr:
                    lookup[addr] = {
                        "realized_pnl_usd": float(metrics.get("realizedPnlUsd", 0)),
                        "unrealized_pnl_usd": float(metrics.get("unrealizedPnlUsd", 0)),
                        "win_rate": float(metrics.get("winRate", 0)),
                        "trade_count": int(metrics.get("tradesCount", 0)),
                        "trades_volume_usd": float(metrics.get("tradesVolumeUsd", 0)),
                        "unique_tokens": int(metrics.get("uniqueTokensTraded", 0)),
                    }
            logger.info(f"Vybe top-traders: fetched {len(lookup)} wallets")
            return lookup
        elif resp.status_code == 403:
            logger.warning(f"Vybe 403: endpoint may require paid tier — {resp.text[:200]}")
            return {}
        else:
            logger.warning(f"Vybe top-traders returned {resp.status_code}")
            return {}
    except requests.RequestException as e:
        logger.error(f"Vybe top-traders request failed: {e}")
        return {}


def _vybe_cross_validate(gmgn_wr: float, gmgn_pnl: float, wallet_address: str, vybe_lookup: dict[str, dict], config: dict) -> float:
    """Cross-validate GMGN wallet stats against Vybe on-chain top-traders data.
    
    vybe_lookup is a dict from _fetch_vybe_top_traders() keyed by wallet address.
    If wallet is NOT in the lookup, it's not in the top-N by realized PnL → penalty.
    If wallet IS in the lookup, compare WR → penalty if gap > threshold.
    
    Returns a penalty multiplier: 1.0 = no penalty, lower = more penalty.
    """
    if not vybe_lookup:
        return 1.0  # No Vybe data available, skip validation
    
    penalty = 0.0
    vybe_data = vybe_lookup.get(wallet_address)
    
    if vybe_data is None:
        # Wallet NOT in Vybe top-traders → has low/negative PnL
        # Only suspicious if GMGN claims substantial PnL (>$100)
        cfg = config.get("cross_validate", {})
        not_found_threshold = cfg.get("not_found_pnl_threshold", 100.0)
        if gmgn_pnl > not_found_threshold:
            not_found_penalty = cfg.get("not_found_penalty", 0.05)
            penalty = not_found_penalty
            logger.info(
                f"🔍 Vybe: {wallet_address[:8]} NOT in top-traders — GMGN=${gmgn_pnl:.0f} > {not_found_threshold:.0f} → penalty={penalty:.0%}"
            )
        return 1.0 - min(penalty, config.get("cross_validate", {}).get("max_penalty", 0.40))
    
    vybe_wr = vybe_data.get("win_rate", 0.0)
    vybe_pnl = vybe_data.get("realized_pnl_usd", 0.0)
    
    cfg = config.get("cross_validate", {})
    wr_threshold = cfg.get("wr_gap_threshold", 25)
    negative_penalty = cfg.get("negative_pnl_penalty", 0.15)
    max_penalty = cfg.get("max_penalty", 0.40)
    
    # 1. WR gap: GMGN claims much higher WR than on-chain reality
    if vybe_wr > 0 and gmgn_wr > vybe_wr:
        wr_gap = gmgn_wr - vybe_wr
        if wr_gap > wr_threshold:
            # Linear penalty: 0 at threshold, up to max_penalty at 100% gap
            gap_ratio = min(1.0, (wr_gap - wr_threshold) / (100 - wr_threshold))
            wr_penalty = gap_ratio * max_penalty
            penalty = max(penalty, wr_penalty)
            logger.info(
                f"🔍 Vybe WR gap: GMGN={gmgn_wr:.0f}% vs Vybe={vybe_wr:.0f}% → penalty={wr_penalty:.0%}"
            )
    
    # 2. Negative on-chain PnL while GMGN shows positive
    if vybe_pnl < 0 and gmgn_pnl > 0:
        penalty = max(penalty, negative_penalty)
        logger.info(
            f"🔍 Vybe negative PnL: ${vybe_pnl:.2f} vs GMGN ${gmgn_pnl:.2f} → penalty={negative_penalty:.0%}"
        )
    
    return 1.0 - min(penalty, max_penalty)


def _fetch_smartmoney_trades(chain: str = "sol", limit: int = 50) -> list[dict]:
    """
    Low-level: fetch raw trades from smartmoney endpoint.
    Returns list of individual trade dicts (maker, maker_info.tags, base_address, side, etc).
    Used by scan_wallets() for grouping/filtering.
    """
    data = _gmgn_get("/v1/user/smartmoney", params={"chain": chain, "limit": limit})
    return data.get("list", []) if isinstance(data, dict) else []


def fetch_smartmoney_wallets(chain: str = "sol", limit: int = 50) -> list[dict]:
    """
    Fetch unique smart money wallets from GMGN OpenAPI.
    Endpoint: GET /v1/user/smartmoney
    Returns list of unique wallet dicts with tags and latest trade info.
    """
    trades = _fetch_smartmoney_trades(chain=chain, limit=limit)
    wallets = {}
    for trade in trades:
        maker = trade.get("maker", "")
        if not maker or maker in wallets:
            continue
        maker_info = trade.get("maker_info", {})
        tags = maker_info.get("tags", [])
        wallets[maker] = {
            "address": maker,
            "tags": tags,
            "name": maker_info.get("name", ""),
            "last_trade_ts": trade.get("timestamp", 0),
            "last_token": trade.get("base_address", ""),
            "last_side": trade.get("side", ""),
            "last_amount_usd": trade.get("amount_usd", 0),
        }
    return list(wallets.values())


def fetch_kol_wallets(chain: str = "sol", limit: int = 30) -> list[dict]:
    """
    Fetch KOL/influencer wallets from GMGN OpenAPI.
    Endpoint: GET /v1/user/kol
    """
    data = _gmgn_get("/v1/user/kol", params={"chain": chain, "limit": limit})
    trades = data.get("list", []) if isinstance(data, dict) else []
    wallets = {}
    for trade in trades:
        maker = trade.get("maker", "")
        if not maker or maker in wallets:
            continue
        maker_info = trade.get("maker_info", {})
        wallets[maker] = {
            "address": maker,
            "tags": maker_info.get("tags", []),
            "name": maker_info.get("name", ""),
            "last_trade_ts": trade.get("timestamp", 0),
            "last_token": trade.get("base_address", ""),
            "last_side": trade.get("side", ""),
            "last_amount_usd": trade.get("amount_usd", 0),
        }
    return list(wallets.values())


def fetch_wallet_stats(wallet_addresses: list[str], chain: str = "sol", period: str = "7d") -> list[dict]:
    """
    Fetch wallet performance stats from GMGN OpenAPI.
    Endpoint: GET /v1/user/wallet_stats
    Returns list of wallet stats dicts with wr, pnl, trades_count.
    
    Note: GMGN API takes single wallet_address per call.
    We fetch in serial for each address.
    """
    results = []
    for addr in wallet_addresses:
        data = _gmgn_get("/v1/user/wallet_stats", params={
            "chain": chain,
            "wallet_address": addr,
            "period": period,
        })
        if data:
            # Map GMGN response to our internal format
            pnl_stat = data.get("pnl_stat", {})
            common = data.get("common", {})
            results.append({
                "address": data.get("wallet_address") or addr,
                "wr_7d": float(pnl_stat.get("winrate", 0)),  # 0-1 range
                "pnl_7d": float(data.get("realized_profit", 0)),
                "trades_7d": int(data.get("buy", 0)) + int(data.get("sell", 0)),
                "token_num": int(pnl_stat.get("token_num", 0)),
                "tags": common.get("tags", []),
                "name": common.get("name") or common.get("ens", ""),
                "native_balance": data.get("native_balance", "0"),
            })
    return results


def fetch_wallet_activity(wallet_address: str, chain: str = "sol", limit: int = 50) -> list[dict]:
    """
    Fetch recent trades for a wallet from GMGN OpenAPI.
    Endpoint: GET /v1/user/wallet_activity
    
    Actual response: data.activities[] (NOT data.list[])
    Each activity has: wallet, tx_hash, timestamp, event_type (buy/sell),
    token {address, symbol, logo}, quote_amount, cost_usd, buy_cost_usd,
    price_usd, is_open_or_close, etc.
    """
    data = _gmgn_get("/v1/user/wallet_activity", params={
        "chain": chain,
        "wallet_address": wallet_address,
        "limit": limit,
    })
    if isinstance(data, list):
        return data
    # Real API key is "activities", not "list"
    if isinstance(data, dict):
        return data.get("activities", data.get("list", []))
    return []


# ─── Backward-compatible aliases ────────────────────────────────────────

def fetch_trending_wallets(limit: int = 50) -> list[dict]:
    """Alias: fetch trending/smartmoney wallets (backward compat)."""
    return fetch_smartmoney_wallets(chain="sol", limit=limit)


def fetch_wallet_detail(wallet_address: str) -> Optional[dict]:
    """Fetch single wallet stats (backward compat)."""
    stats = fetch_wallet_stats([wallet_address], period="7d")
    if stats and isinstance(stats, list) and len(stats) > 0:
        return stats[0]
    return None


def fetch_wallet_recent_trades(wallet_address: str, limit: int = 50) -> list[dict]:
    """Fetch recent trades for a wallet (backward compat)."""
    return fetch_wallet_activity(wallet_address, limit=limit)


def _detect_bot(wr_7d: float, pnl_7d: float, trades_7d: int) -> float:
    """Detect bot/MEV probability based on statistical anomalies (0.0-1.0).

    Human traders cannot sustain >80% WR over 30+ trades, or execute
    40+ trades/day on meme coins. These patterns indicate bots/MEV.

    V8.1 — tightened thresholds + increased penalty multiplier.
    Returns:
        Bot probability 0.0 (human) to 1.0 (definitely bot).
    """
    bot_score = 0.0

    # 1. WR anomaly: >80% WR with >30 trades → statistically impossible for human
    if wr_7d > 80.0 and trades_7d > 30:
        excess_wr = min(1.0, (wr_7d - 80.0) / 20.0)
        bot_score = max(bot_score, 0.6 + excess_wr * 0.4)

    # 2. Ultra-high frequency: >300 trades/week (>40/day)
    if trades_7d > 300:
        freq_score = min(1.0, (trades_7d - 300) / 400.0)
        bot_score = max(bot_score, 0.5 + freq_score * 0.5)

    # 3. High frequency: >100 trades/week with decent WR
    if trades_7d > 100 and wr_7d > 65.0:
        freq_score = min(1.0, (trades_7d - 100) / 300.0)
        bot_score = max(bot_score, 0.4 + freq_score * 0.4)

    # 4. MEV pattern: high PnL + high WR + high frequency
    if pnl_7d > 1000.0 and wr_7d > 70.0 and trades_7d > 50:
        bot_score = max(bot_score, 0.85)

    # 5. Near-perfect WR: >=95% WR with >10 trades
    if wr_7d >= 95.0 and trades_7d > 10:
        bot_score = max(bot_score, 0.8)

    return min(1.0, bot_score)


def compute_wallet_quality(wr_7d: float, pnl_7d: float, trades_7d: int) -> float:
    """Compute a composite quality score (0-100) from wallet performance metrics.

    V8 — Rebalanced + bot detection:
    - Win rate:  0-60 points (linear, wr_7d is 0-100%)
    - PnL:       0-25 points (log scale: $0→0, $100→12.5, $1000→25)
    - Trades:    0-15 points (saturates at 50+ trades)
    - Bot penalty: up to 50% reduction for detected bots (V8.1)

    Returns float rounded to 1 decimal place.
    """
    import math

    # Win rate component: 0-60 points
    wr_score = min(max(wr_7d, 0.0), 100.0) * 0.6

    # PnL component: 0-25 points, log scale for better differentiation
    if pnl_7d <= 0:
        pnl_score = 0.0
    elif pnl_7d >= 1000.0:
        pnl_score = 25.0
    else:
        pnl_score = min(25.0, math.log10(pnl_7d + 1) / math.log10(1001) * 25.0)

    # Trade count component: 0-15 points, 50+ trades = full score
    trades_score = min(15.0, max(0.0, trades_7d / 50.0 * 15.0))

    quality = wr_score + pnl_score + trades_score

    # ── V8.1: Bot penalty — up to 50% reduction ──
    bot_prob = _detect_bot(wr_7d, pnl_7d, trades_7d)
    if bot_prob > 0.0:
        penalty = bot_prob * 0.5  # up to 50% reduction
        quality *= (1.0 - penalty)
        logger = logging.getLogger(__name__)
        logger.debug(
            "🤖 Bot detected: WR=%.0f%% PnL=%.0f trades=%d → prob=%.1f → penalty=%.0f%%",
            wr_7d, pnl_7d, trades_7d, bot_prob, penalty * 100,
        )

    # ── V8.3: Vybe on-chain cross-validation ──
    # This parameter is passed by caller; default to no-op if not provided
    vybe_data = locals().get("vybe_data")  # injected by scan_wallets
    if vybe_data is not None:
        import os
        try:
            cfg_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config", "settings.yaml")
            with open(cfg_path) as f:
                settings = yaml.safe_load(f)
            vybe_cfg = settings.get("vybe", {})
            if vybe_cfg.get("enabled", False):
                multiplier = _vybe_cross_validate(wr_7d, pnl_7d, vybe_data, vybe_cfg)
                if multiplier < 1.0:
                    quality *= multiplier
                    logger.info(
                        f"🔍 Vybe validated: quality {quality/multiplier:.1f} → {quality:.1f} (×{multiplier:.2f})"
                    )
        except Exception:
            pass  # Graceful — Vybe validation is optional

    return round(quality, 1)


def normalize_wallet_data(raw: dict) -> dict:
    """
    Normalize wallet data from GMGN wallet_stats endpoint to internal format.
    Handles both raw GMGN response and merged brief+stats format.
    wr_7d comes as 0-1 ratio from GMGN.
    """
    address = raw.get("address") or raw.get("wallet_address") or raw.get("wallet") or ""

    wr_7d = raw.get("wr_7d") or raw.get("winrate") or raw.get("wr") or raw.get("winrate_7d", 0)
    pnl_7d = raw.get("pnl_7d") or raw.get("pnl") or raw.get("realized_profit", 0)
    trades_7d = raw.get("trades_7d") or raw.get("trade_count") or raw.get("trades", 0)
    quality = raw.get("quality_score") or raw.get("score", 0)

    try:
        wr_7d = float(wr_7d)
        # If 0-1 range (GMGN native), convert to percentage
        if 0 < wr_7d <= 1:
            wr_7d = wr_7d * 100
    except (TypeError, ValueError):
        wr_7d = 0.0
    try:
        pnl_7d = float(pnl_7d)
    except (TypeError, ValueError):
        pnl_7d = 0.0
    try:
        trades_7d = int(float(trades_7d))
    except (TypeError, ValueError):
        trades_7d = 0
    try:
        quality = float(quality)
    except (TypeError, ValueError):
        quality = 0.0

    # V8: Always compute our own quality score (with bot detection + rebalanced weights).
    # GMGN's quality_score has no bot filtering and uses different weighting.
    computed = compute_wallet_quality(wr_7d, pnl_7d, trades_7d)
    # Blend: if API provided a score, blend it 50/50 with ours
    if quality > 0.0:
        quality = round((quality + computed) / 2, 1)
    else:
        quality = computed

    label = raw.get("label") or raw.get("name") or raw.get("ens") or address[:8]

    return {
        "address": address,
        "label": label,
        "quality_score": quality,
        "wr_7d": wr_7d,
        "pnl_7d": pnl_7d,
        "trades_7d": trades_7d,
    }


def quality_filter(wallet: dict, min_wr: float = 30, min_pnl: float = 0, min_trades: int = 10) -> bool:
    """
    Apply quality filter to a wallet.
    CB-G1 FIX: trades >= 10 check is explicitly here and enforced in scan loop.
    """
    if wallet["wr_7d"] < min_wr:
        return False
    if wallet["pnl_7d"] < min_pnl:
        return False
    # CB-G1: Explicit trades >= 10 check
    if wallet["trades_7d"] < min_trades:
        return False
    return True


def scan_wallets(
    min_wr: float = 30,
    min_pnl: float = 0,
    min_trades: int = 10,
    sensitivity: str = "MEDIUM",
    limit: int = 50,
) -> list[dict]:
    """
    Main wallet discovery loop.
    1. Fetch smart money wallets from GMGN (addresses + trades + tags)
    2. For wallets with matching tags, fetch wallet_stats for performance
    3. Merge trade data + performance, apply quality filter
    4. Return qualified wallets WITH their latest trade (ready for alerting)
    
    Returns list of dicts: {wallet fields..., last_trade: {trade fields...}}
    """
    db = get_db()

    # Step 1: Get smart money wallets with their trades
    # Step 1: Get smart money trades (raw, with maker + tags)
    trades = _fetch_smartmoney_trades(chain="sol", limit=max(50, limit))
    if not trades:
        logger.warning("No wallets returned from GMGN smartmoney endpoint")
        return []

    # Group by maker address
    wallet_trades = {}
    for t in trades:
        maker = t.get("maker", "")
        if not maker:
            continue
        if maker not in wallet_trades:
            wallet_trades[maker] = []
        wallet_trades[maker].append(t)

    logger.info(f"Fetched {len(trades)} trades from {len(wallet_trades)} wallets")

    # Step 2: Filter wallets by tags (sensitivity), fetch stats
    allowed_tags = set(SENSITIVITY_MAP.get(sensitivity, ["smart_degen"]))
    candidate_addrs = []
    for maker, tlist in wallet_trades.items():
        if not allowed_tags:
            # ALL sensitivity — no tag filter
            candidate_addrs.append(maker)
            continue
        for t in tlist:
            tags = set((t.get("maker_info", {}) or {}).get("tags", []))
            if tags & allowed_tags:
                candidate_addrs.append(maker)
                break

    if not candidate_addrs:
        logger.warning(f"No wallets match sensitivity {sensitivity} tags")
        return []

    # Limit candidates to avoid excessive API calls
    target_addrs = candidate_addrs[:limit]

    # Step 3: Fetch wallet_stats for candidates
    stats_list = fetch_wallet_stats(target_addrs, period="7d")
    stats_map = {}
    for s in stats_list:
        addr = s.get("address") or s.get("wallet_address", "")
        if addr:
            stats_map[addr] = s

    # Step 4: Merge, normalize, filter
    qualified_wallets = []
    for addr in target_addrs:
        stats = stats_map.get(addr, {})
        merged = {"address": addr, **stats}
        wallet = normalize_wallet_data(merged)

        # ── Quality Filter (CB-G1: trades >= 10 in filter) ──
        if not quality_filter(wallet, min_wr=min_wr, min_pnl=min_pnl, min_trades=min_trades):
            continue

        # Attach most recent trade
        trades_for_wallet = sorted(
            wallet_trades.get(addr, []),
            key=lambda t: int(t.get("timestamp", 0)),
            reverse=True
        )
        if not trades_for_wallet:
            continue

        latest = trades_for_wallet[0]
        base_token = latest.get("base_token", {}) or {}
        wallet["last_trade"] = {
            "token_address": latest.get("base_address", ""),
            "token_symbol": base_token.get("symbol", ""),
            "action": latest.get("side", "BUY"),
            "amount_sol": float(latest.get("quote_amount", 0)),
            "amount_usd": float(latest.get("amount_usd", 0)),
            "tx_hash": latest.get("transaction_hash") or "",
            "timestamp": int(latest.get("timestamp", 0)),
        }

        qualified_wallets.append(wallet)
        # Upsert without last_trade (DB can't store nested dict)
        db_wallet = {k: v for k, v in wallet.items() if k != "last_trade"}
        db_wallet["last_trade_json"] = json.dumps(wallet.get("last_trade", {}))
        db.upsert_wallet(addr, db_wallet)

    logger.info(f"Quality filter: {len(qualified_wallets)} / {len(target_addrs)} wallets")

    # V8.3: Vybe on-chain cross-validation for all qualified wallets
    import os as _os
    try:
        cfg_path = _os.path.join(_os.path.dirname(_os.path.dirname(__file__)), "config", "settings.yaml")
        with open(cfg_path) as f:
            settings = yaml.safe_load(f)
    except Exception:
        settings = {}
    vybe_cfg = settings.get("vybe", {})
    if vybe_cfg.get("enabled", False) and vybe_cfg.get("api_key"):
        # Fetch top-traders leaderboard ONCE (free tier, 1000 wallets)
        vybe_lookup = _fetch_vybe_top_traders(vybe_cfg["api_key"])
        if vybe_lookup:
            validated_count = 0
            penalized_count = 0
            for w in qualified_wallets:
                multiplier = _vybe_cross_validate(
                    w["wr_7d"], w["pnl_7d"], w["address"], vybe_lookup, vybe_cfg
                )
                if multiplier < 1.0:
                    orig_quality = w["quality_score"]
                    w["quality_score"] = round(orig_quality * multiplier, 1)
                    w["vybe_validated"] = True
                    vybe_data = vybe_lookup.get(w["address"], {})
                    w["vybe_wr"] = vybe_data.get("win_rate", 0)
                    w["vybe_pnl"] = vybe_data.get("realized_pnl_usd", 0)
                    penalized_count += 1
                validated_count += 1
            logger.info(f"Vybe validated: {validated_count} wallets, {penalized_count} penalized")

    return qualified_wallets


def get_wallet_last_trade(wallet_address: str) -> Optional[dict]:
    """
    Get last trade for a wallet. Prefer embedded last_trade from scan_wallets().
    Falls back to filtering smartmoney endpoint if called standalone.
    """
    # Try DB first (saved by scan_wallets)
    db = get_db()
    wallet = db.get_wallet(wallet_address)
    if wallet and wallet.get("last_trade"):
        return wallet["last_trade"]
    
    # Fallback: filter smartmoney
    trades = _fetch_smartmoney_trades(chain="sol", limit=200)
    wallet_trades = [t for t in trades if t.get("maker") == wallet_address]
    if not wallet_trades:
        return None
    
    wallet_trades.sort(key=lambda t: int(t.get("timestamp", 0)), reverse=True)
    trade = wallet_trades[0]
    token_addr = trade.get("base_address") or trade.get("token_address") or trade.get("mint", "")
    if not token_addr:
        return None
    
    base_token = trade.get("base_token", {}) or {}
    return {
        "token_address": token_addr,
        "token_symbol": base_token.get("symbol", "") or trade.get("token_symbol", ""),
        "action": trade.get("side") or trade.get("action") or "BUY",
        "amount_sol": float(trade.get("quote_amount") or 0),
        "amount_usd": float(trade.get("amount_usd") or 0),
        "timestamp": trade.get("timestamp", 0),
        "tx_hash": trade.get("transaction_hash") or "",
    }


def check_exit_conditions(wallet_address: str, min_wr: float = 30, min_pnl: float = 0) -> bool:
    """
    CB-G5: Check if a previously-followed wallet should trigger EXIT_ALERT.
    Returns True if wallet WR dropped below 30 OR PnL went negative.
    """
    db = get_db()
    wallet = db.get_wallet(wallet_address)
    if not wallet:
        return False
    if wallet["status"] != "ACTIVE":
        return False

    # Re-fetch fresh data
    detail = fetch_wallet_detail(wallet_address)
    if not detail:
        return False

    w = normalize_wallet_data(detail)
    # Update DB with latest
    db.upsert_wallet(wallet_address, w)

    # Check if still held tokens from this wallet
    alerts = db.get_alerts_for_token_wallet("", wallet_address)
    has_unexecuted = any(not a.get("executed") for a in alerts)

    if has_unexecuted and (w["wr_7d"] < min_wr or w["pnl_7d"] < min_pnl):
        return True
    return False
