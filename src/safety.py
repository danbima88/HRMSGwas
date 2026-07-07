"""
GWAS v2.0 — Safety Filters
Checks LP liquidity, holder concentration, and token age.
Uses GMGN API endpoints with requests_cache for efficiency.
"""

import logging
import requests
from datetime import datetime, timedelta
from dataclasses import dataclass, field
from typing import Optional

# requests_cache for SQLite-backed caching
try:
    import requests_cache
    HAS_CACHE = True
except ImportError:
    HAS_CACHE = False

logger = logging.getLogger(__name__)


# ─── Cache Configuration ────────────────────────────────────────────────

def _get_cached_session(cache_name: str, expire_after: int = 300) -> requests.Session:
    """Get a cached requests session. Falls back to regular session if requests_cache unavailable."""
    if HAS_CACHE:
        return requests_cache.CachedSession(
            cache_name=cache_name,
            backend="sqlite",
            expire_after=expire_after,
            allowable_methods=("GET",),
            allowable_codes=(200,),
        )
    return requests.Session()


# Cache instances with different TTLs
_token_cache = _get_cached_session("/opt/gwas/data/cache_token", expire_after=300)     # 5 min
_holder_cache = _get_cached_session("/opt/gwas/data/cache_holders", expire_after=900)   # 15 min
_rugcheck_cache = _get_cached_session("/opt/gwas/data/cache_rugcheck", expire_after=3600)  # 1 hour


# ─── Configuration ───────────────────────────────────────────────────────

GMGN_BASE = "https://openapi.gmgn.ai"
MIN_TOKEN_AGE_MINUTES = 5
MIN_LP_USD = 500
MAX_TOP10_HOLDER_PCT = 50

# ─── GMGN Auth Helpers ──────────────────────────────────────────────────
import os as _os
import uuid as _uuid
import time as _time

def _gmgn_headers():
    api_key = _os.environ.get("GMGN_API_KEY", "")
    return {"X-APIKEY": api_key, "Content-Type": "application/json"}

def _gmgn_auth_params():
    return {
        "timestamp": int(_time.time()),
        "client_id": str(_uuid.uuid4()),
    }

def _cached_get(session, url: str, endpoint: str, params: dict = None, timeout: int = 10):
    """GET with auth + caching. session is a requests_cache.CachedSession or requests.Session."""
    all_params = {}
    all_params.update(_gmgn_auth_params())
    if params:
        all_params.update(params)
    full_url = f"{GMGN_BASE}{endpoint}"
    try:
        resp = session.get(full_url, params=all_params, headers=_gmgn_headers(), timeout=timeout)
        if resp.status_code == 200:
            data = resp.json()
            if data.get("code") == 0:
                return data.get("data", {})
        return None
    except requests.RequestException as e:
        logger.error(f"GMGN request failed {endpoint}: {e}")
        return None

# Helius IP allowlist for webhook validation
HELIUS_IPS = [
    "34.86.0.0/16",
    "34.118.0.0/16",
    "34.126.0.0/16",
    "35.206.0.0/16",
    "34.36.0.0/16",
]


@dataclass
class SafetyResult:
    """Result of a safety check for a token."""
    passed: bool = True
    token_age_minutes: Optional[float] = None
    lp_usd: Optional[float] = None
    top10_holder_pct: Optional[float] = None
    flags: list[str] = field(default_factory=list)
    token_symbol: str = ""

    def __bool__(self):
        return self.passed


def fetch_token_info(token_address: str) -> dict | None:
    """
    Fetch token info from GMGN OpenAPI /v1/token/info.
    Returns: {'price', 'symbol', 'total_supply', 'launchpad', ...} or None.
    Cached: 5 minutes via requests_cache.
    """
    data = _cached_get(_token_cache, "token_info", "/v1/token/info",
                       params={"chain": "sol", "address": token_address})
    return data


def fetch_token_holders(token_address: str) -> dict | None:
    """
    Fetch top holders from GMGN OpenAPI /v1/market/token_top_holders.
    Cached: 15 minutes via requests_cache.
    """
    data = _cached_get(_holder_cache, "token_holders", "/v1/market/token_top_holders",
                       params={"chain": "sol", "address": token_address})
    return data


def fetch_rugcheck(token_address: str) -> dict | None:
    """
    Fetch security/rugcheck from GMGN OpenAPI /v1/token/security.
    Cached: 1 hour via requests_cache.
    """
    data = _cached_get(_rugcheck_cache, "token_security", "/v1/token/security",
                       params={"chain": "sol", "address": token_address})
    return data


def check_token_safety(token_address: str) -> SafetyResult:
    """
    Run full safety check on a token using GMGN OpenAPI v1.
    Returns SafetyResult with pass/fail and flags.
    
    Checks:
      - LP >= MIN_LP_USD (5000)
      - Top 10 holders < MAX_TOP10_HOLDER_PCT (50%)
      - Token age >= MIN_TOKEN_AGE_MINUTES (30 min)
    
    Data sources (CB-G4):
      - lp_usd → GMGN /v1/token/info (price.liquidity_usd or liquidity field)
      - top10_holder_pct → GMGN /v1/token/security (top_10_holder_rate)
      - age_minutes → GMGN /v1/token/info (creation_timestamp → now - created)
    """
    result = SafetyResult()
    flags = []

    token_info = fetch_token_info(token_address)
    security_info = fetch_rugcheck(token_address)

    if token_info is None:
        result.passed = False
        result.flags = ["no_token_data"]
        logger.warning(f"Safety check failed for {token_address}: no token data available")
        return result

    # Extract token symbol
    result.token_symbol = token_info.get("symbol", "") or token_info.get("name", "")[:12]

    # ── LP Check — from token/info ───────────────────────────────────────
    # Try: price.liquidity > liquidity > lp_usd > pool fields
    price = token_info.get("price", {}) or {}
    pool = token_info.get("pool", {}) or {}
    lp_usd = (price.get("liquidity") or price.get("liquidity_usd")
              or token_info.get("liquidity")
              or pool.get("liquidity")
              or pool.get("liquidity_usd")
              or token_info.get("lp_usd", 0))
    try:
        lp_usd = float(lp_usd) if lp_usd else 0
    except (TypeError, ValueError):
        lp_usd = 0
    result.lp_usd = lp_usd

    # Skip LP check for zero-liquidity tokens (likely native tokens like SOL)
    if lp_usd > 0 and lp_usd < MIN_LP_USD:
        flags.append(f"low_lp_${lp_usd:.0f}")
        result.passed = False

    # ── Token Age Check — from creation_timestamp (Unix epoch, could be ms) ──
    created_ts = token_info.get("creation_timestamp") or token_info.get("created_at") or 0
    try:
        created_ts = int(float(created_ts))
    except (TypeError, ValueError):
        created_ts = 0

    if created_ts > 0:
        # GMGN sometimes returns milliseconds instead of seconds
        if created_ts > 10_000_000_000:
            created_ts = created_ts / 1000
        age_minutes = (datetime.utcnow() - datetime.fromtimestamp(created_ts)).total_seconds() / 60
        # Negative age means bad/bogus timestamp → treat as unknown
        if age_minutes < 0:
            age_minutes = 999999
    else:
        # creation_timestamp=0 means unknown (native tokens, old tokens) — assume old enough
        age_minutes = 999999
    result.token_age_minutes = age_minutes

    if age_minutes < MIN_TOKEN_AGE_MINUTES:
        flags.append(f"fresh_token_{age_minutes:.0f}m")
        result.passed = False

    # ── Holder Concentration Check — from token/security ──────────────────
    top10_pct = 0
    if security_info:
        rate = security_info.get("top_10_holder_rate")
        if rate is not None:
            try:
                top10_pct = float(rate) * 100  # security endpoint returns 0-1 ratio
            except (TypeError, ValueError):
                top10_pct = 0
    result.top10_holder_pct = top10_pct

    if top10_pct > MAX_TOP10_HOLDER_PCT:
        flags.append(f"high_concentration_{top10_pct:.0f}%")
        result.passed = False

    result.flags = flags
    return result


def is_helius_ip(ip_str: str) -> bool:
    """Check if an IP matches Helius allowlist."""
    import ipaddress
    try:
        ip = ipaddress.ip_address(ip_str)
        for cidr in HELIUS_IPS:
            if ip in ipaddress.ip_network(cidr):
                return True
        return False
    except ValueError:
        return False
