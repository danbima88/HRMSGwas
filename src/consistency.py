"""
GWAS v2.0 — Multi-Timeframe Consistency Scoring
Fetches GMGN trending tokens across 5 timeframes, computes
consistency scoring per token, and caches results for fast lookup.
"""

import json
import logging
import os
import subprocess
import time
from typing import Optional

logger = logging.getLogger(__name__)


# ─── Configuration ────────────────────────────────────────────────────────

CACHE_PATH = "/opt/gwas/data/consistency_cache.json"
CACHE_TTL_SECONDS = 300  # 5 minutes

# Bonus classification tiers
BONUS_TIERS = [
    (4, "multi_tf_strong", 8),
    (3, "multi_tf_medium", 5),
    (2, "multi_tf_weak", 2),
]

DEFAULT_TIMEFRAMES = ["1m", "5m", "1h", "6h", "24h"]
DEFAULT_LIMIT = 100


# ─── Internal Helpers ─────────────────────────────────────────────────────

def _call_gmgn_trending(
    chain: str = "sol",
    interval: str = "1m",
    order_by: str = "volume",
    limit: int = 100,
    timeout: int = 15,
) -> dict | None:
    """
    Call gmgn-cli market trending for a single timeframe.

    Returns parsed JSON as a dict, or None on failure.
    """
    cmd = [
        "gmgn-cli", "market", "trending",
        "--chain", chain,
        "--interval", interval,
        "--order-by", order_by,
        "--limit", str(limit),
        "--raw",
    ]
    env = os.environ.copy()
    env.setdefault("HOME", os.path.expanduser("~"))

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
        if result.returncode != 0:
            stderr = result.stderr.strip()[:200] if result.stderr else "no stderr"
            logger.warning(
                f"gmgn-cli trending ({interval}) exited {result.returncode}: {stderr}"
            )
            return None

        data = json.loads(result.stdout)
        return data

    except subprocess.TimeoutExpired:
        logger.warning(f"gmgn-cli trending ({interval}) timed out after {timeout}s")
        return None
    except json.JSONDecodeError:
        logger.warning(f"gmgn-cli trending ({interval}) returned invalid JSON")
        return None
    except Exception as exc:
        logger.warning(f"gmgn-cli trending ({interval}) failed: {exc}")
        return None


def _extract_tokens_from_response(data: dict) -> list[dict]:
    """
    Extract token entries from a gmgn-cli trending response.

    Walks data.rank[] and pulls address, symbol, volume, and price_change.
    Returns a list of flat dicts keyed by lowercase field names.
    """
    tokens = []
    rank_list = data.get("data", {}).get("rank", [])
    if not isinstance(rank_list, list):
        return tokens

    for entry in rank_list:
        address = entry.get("address", "")
        if not address:
            continue
        tokens.append({
            "address": address,
            "symbol": entry.get("symbol", ""),
            "volume": entry.get("volume") or entry.get("volume_24h", 0),
            "price_change": entry.get("price_change")
                           or entry.get("price_change_24h")
                           or entry.get("price_change_percent", 0),
        })
    return tokens


def _classify_bonus(consistency: int) -> tuple[Optional[str], int]:
    """
    Classify a consistency count into a bonus key and score bonus.

    Returns (bonus_key, score_bonus).  bonus_key is None when consistency <= 1.
    """
    for threshold, key, bonus in BONUS_TIERS:
        if consistency >= threshold:
            return key, bonus
    return None, 0


def _read_cache() -> dict | None:
    """Read cached consistency data if present and within TTL. Returns None if stale."""
    try:
        stat = os.stat(CACHE_PATH)
        age = time.time() - stat.st_mtime
        if age > CACHE_TTL_SECONDS:
            logger.debug(
                f"Consistency cache is {age:.0f}s old (TTL={CACHE_TTL_SECONDS}s), "
                f"will refetch"
            )
            return None

        with open(CACHE_PATH, "r") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            return data
    except FileNotFoundError:
        logger.debug("No consistency cache found, will fetch fresh data")
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning(f"Failed to read consistency cache: {exc}")
    return None


def _write_cache(data: dict) -> None:
    """Atomically write consistency data to the JSON cache file."""
    os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
    tmp_path = CACHE_PATH + ".tmp"
    try:
        with open(tmp_path, "w") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
        os.replace(tmp_path, CACHE_PATH)
    except OSError as exc:
        logger.warning(f"Failed to write consistency cache: {exc}")


# ─── Public API ───────────────────────────────────────────────────────────

def fetch_trending_consistency(
    chain: str = "sol",
    timeframes: list[str] | None = None,
    limit: int = DEFAULT_LIMIT,
) -> dict[str, dict]:
    """
    Fetch GMGN trending tokens across multiple timeframes and compute
    multi-timeframe consistency scoring per token.

    Args:
        chain: Blockchain to query (default "sol").
        timeframes: List of timeframe intervals to check.
                    Defaults to ["1m","5m","1h","6h","24h"].
        limit: Number of trending tokens per timeframe (default 100).

    Returns:
        Dict keyed by token address, each value is:
        {
            "consistency": int,          # number of timeframes this token appears in
            "timeframes": [str],         # which timeframes the token appeared in
            "symbol": str,               # token symbol
            "avg_volume": float,         # average volume across timeframes
            "avg_price_change": float,   # average price change across timeframes
        }

        On total failure (all timeframes errored), returns an empty dict.
        Results are cached to disk for CACHE_TTL_SECONDS (5 min).
    """
    # ── Check cache first ──────────────────────────────────────────────────
    cached = _read_cache()
    if cached is not None:
        logger.debug(f"Using cached consistency data ({len(cached)} tokens)")
        return cached

    # ── Fetch from each timeframe ──────────────────────────────────────────
    if timeframes is None:
        timeframes = DEFAULT_TIMEFRAMES

    # Intermediate: address -> {list of (symbol, volume, price_change, timeframe)}
    accumulator: dict[str, dict] = {}

    for tf in timeframes:
        response = _call_gmgn_trending(
            chain=chain, interval=tf, limit=limit
        )
        if response is None:
            logger.warning(f"Skipping timeframe {tf} — fetch failed")
            continue

        tokens = _extract_tokens_from_response(response)
        if not tokens:
            logger.warning(f"Timeframe {tf} returned no tokens")
            continue

        logger.debug(f"Timeframe {tf}: got {len(tokens)} tokens")

        for t in tokens:
            addr = t["address"]
            if addr not in accumulator:
                accumulator[addr] = {
                    "symbol": "",
                    "volumes": [],
                    "price_changes": [],
                    "timeframes": [],
                }

            entry = accumulator[addr]
            # Keep the first non-empty symbol we encounter
            if not entry["symbol"] and t["symbol"]:
                entry["symbol"] = t["symbol"]
            entry["volumes"].append(float(t["volume"] or 0))
            entry["price_changes"].append(float(t["price_change"] or 0))
            entry["timeframes"].append(tf)

    # ── If every timeframe failed, return empty dict ──────────────────────
    if not accumulator:
        logger.warning(
            "All timeframes failed — returning empty consistency data"
        )
        return {}

    # ── Build output dict ──────────────────────────────────────────────────
    result: dict[str, dict] = {}
    for addr, entry in accumulator.items():
        n = len(entry["timeframes"])
        avg_vol = (
            sum(entry["volumes"]) / n if entry["volumes"] else 0.0
        )
        avg_pc = (
            sum(entry["price_changes"]) / n if entry["price_changes"] else 0.0
        )
        result[addr] = {
            "consistency": n,
            "timeframes": sorted(
                entry["timeframes"],
                key=lambda x: DEFAULT_TIMEFRAMES.index(x)
                if x in DEFAULT_TIMEFRAMES else 99,
            ),
            "symbol": entry["symbol"],
            "avg_volume": round(avg_vol, 2),
            "avg_price_change": round(avg_pc, 4),
        }

    logger.info(
        f"consistency cache refreshed: {len(result)} tokens "
        f"across {len(timeframes)} timeframes"
    )

    # ── Persist cache ──────────────────────────────────────────────────────
    _write_cache(result)

    return result


def get_token_consistency(
    maker_token: str,
) -> tuple[int, Optional[str]]:
    """
    Look up a token in the consistency cache and return its
    (consistency_count, bonus_key).

    The cache is refreshed automatically if older than CACHE_TTL_SECONDS.

    Args:
        maker_token: Token address (mint) to look up.

    Returns:
        Tuple of (consistency_count, bonus_key):
        - consistency_count: integer 0–5
        - bonus_key: one of "multi_tf_strong", "multi_tf_medium",
          "multi_tf_weak", or None

        Cache miss returns (0, None).
    """
    data = fetch_trending_consistency()

    if maker_token not in data:
        logger.debug(f"token {maker_token} not found in consistency data")
        return 0, None

    entry = data[maker_token]
    consistency = entry["consistency"]
    bonus_key, score_bonus = _classify_bonus(consistency)

    logger.info(
        f"token {maker_token} found with consistency "
        f"{consistency}/{len(DEFAULT_TIMEFRAMES)} (+{score_bonus} bonus)"
    )

    return consistency, bonus_key
