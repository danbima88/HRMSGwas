# 🔌 GMGN OpenAPI Integration — Technical Reference

> **Dokumen ini:** referensi teknis integrasi GMGN OpenAPI v1 untuk GWAS v2.0.
> Mencakup auth flow, semua endpoint yang digunakan, real field names, response structures,
> data quality issues, caching strategy, dan error handling.
>
> **Target audience:** Developer yang maintain/extend GWAS, atau siapapun yang mau integrasi
> GMGN API untuk wallet discovery di Solana.
>
> Last updated: 10 Juni 2026

---

## Daftar Isi

1. [Auth Flow: Ed25519 Keypair](#-auth-flow-ed25519-keypair)
2. [Request Structure](#-request-structure)
3. [Endpoint Reference](#-endpoint-reference)
4. [Real Field Names vs Docs](#-real-field-names-vs-docs)
5. [Response Examples](#-response-examples)
6. [Data Quality Issues & Fallbacks](#-data-quality-issues--fallbacks)
7. [Rate Limits (Observed)](#-rate-limits-observed)
8. [Caching Strategy](#-caching-strategy)
9. [Error Handling Patterns](#-error-handling-patterns)
10. [GMGN Tags Reference](#-gmgn-tags-reference)

---

## 🔐 Auth Flow: Ed25519 Keypair

### Step 1: Generate Keypair

```bash
# Generate Ed25519 keypair
python3 -c "
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization
import base64

key = Ed25519PrivateKey.generate()
private_bytes = key.private_bytes(
    encoding=serialization.Encoding.Raw,
    format=serialization.PrivateFormat.Raw,
    encryption_algorithm=serialization.NoEncryption()
)
public_bytes = key.public_key().public_bytes(
    encoding=serialization.Encoding.Raw,
    format=serialization.PublicFormat.Raw
)

print(f'Private key (base64): {base64.b64encode(private_bytes).decode()}')
print(f'Public key (base64):  {base64.b64encode(public_bytes).decode()}')
"
```

### Step 2: Register Public Key di gmgn.ai

1. Buka [gmgn.ai](https://gmgn.ai) → Settings → API
2. Register public key (base64-encoded)
3. Dapat **API Key** (simpan sebagai `GMGN_API_KEY`)

### Step 3: Setiap Request

Setiap request ke GMGN OpenAPI membutuhkan:

```
Headers:
  X-APIKEY: {GMGN_API_KEY}
  Content-Type: application/json

Query Params (setiap request):
  timestamp={unix_epoch_seconds}
  client_id={random_uuid4}
```

### Implementasi GWAS

```python
# File: src/wallet_scanner.py (lines 33-70)

import os
import uuid
import time as _time

GMGN_BASE = "https://openapi.gmgn.ai"

def _gmgn_headers():
    api_key = os.environ.get("GMGN_API_KEY", "")
    return {"X-APIKEY": api_key, "Content-Type": "application/json"}

def _gmgn_auth_params():
    return {
        "timestamp": int(_time.time()),
        "client_id": str(uuid.uuid4()),
    }

def _gmgn_get(endpoint: str, params: dict = None, timeout: int = 15) -> dict:
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
```

---

## 📡 Request Structure

### Base URL

```
https://openapi.gmgn.ai
```

### Generic GET Pattern

```
GET /v1/{resource}?chain=sol&{params}&timestamp={now}&client_id={uuid}
Headers:
  X-APIKEY: {key}
  Content-Type: application/json
```

### Response Envelope

Semua endpoint membungkus response dalam envelope yang sama:

```json
{
  "code": 0,
  "message": "success",
  "data": { ... }
}
```

- `code = 0` → sukses
- `code != 0` → error (cek `message` field)

---

## 📋 Endpoint Reference

### 1. `/v1/user/smartmoney` — Smart Money Wallets

**Purpose:** Fetch recent trades dari smart money wallets.

**Request:**
```
GET /v1/user/smartmoney?chain=sol&limit=50
```

**Parameters:**
| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `chain` | string | `"sol"` | Blockchain (only "sol" supported) |
| `limit` | int | 50 | Max trades to return |

**Response `data.list[]`:**
```json
[
  {
    "maker": "7MvB...wallet_address",
    "base_address": "BONK...token_mint",
    "base_token": {
      "symbol": "BONK",
      "name": "Bonk",
      "decimals": 5
    },
    "side": "buy",
    "amount_usd": 1234.56,
    "quote_amount": 5.5,
    "timestamp": 1718123456,
    "transaction_hash": "5x...tx_sig",
    "maker_info": {
      "name": "Smart Degen #42",
      "tags": ["smart_degen", "sniper"],
      "avatar": "https://..."
    }
  }
]
```

**Used by:** `wallet_scanner.py:_fetch_smartmoney_trades()` → `scan_wallets()`

**Key Behavior:**
- Setiap item di `list` adalah **satu trade**, bukan satu wallet
- Satu wallet bisa muncul multiple kali (multiple trades)
- GWAS melakukan **grouping by `maker`** untuk deduplicate
- Tags ada di `maker_info.tags` (bukan di top-level)

---

### 2. `/v1/user/wallet_stats` — Wallet Performance Stats

**Purpose:** Fetch 7-day performance stats untuk satu wallet.

**Request:**
```
GET /v1/user/wallet_stats?chain=sol&wallet_address={ADDR}&period=7d
```

**Parameters:**
| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `chain` | string | `"sol"` | Blockchain |
| `wallet_address` | string | **Required** | Single wallet address |
| `period` | string | `"7d"` | Stats period (7d, 30d) |

⚠️ **PENTING:** Endpoint ini hanya menerima **satu** `wallet_address` per call. GWAS fetch secara **serial** untuk setiap wallet. Ini adalah bottleneck utama (~500ms per wallet).

**Response `data`:**
```json
{
  "wallet_address": "7MvB...",
  "native_balance": "100.5",
  "realized_profit": 45.67,
  "unrealized_profit": 12.34,
  "buy": 15,
  "sell": 8,
  "pnl_stat": {
    "winrate": 0.65,
    "token_num": 12,
    "avg_hold_minutes": 45
  },
  "common": {
    "name": "Smart Degen #42",
    "ens": "",
    "tags": ["smart_degen", "sniper"],
    "avatar": "https://..."
  }
}
```

**GWAS Field Mapping (`fetch_wallet_stats()`):**

| GMGN Field Path | GWAS Field | Type | Notes |
|-----------------|------------|------|-------|
| `wallet_address` | `address` | str | |
| `pnl_stat.winrate` | `wr_7d` | float | **0-1 range** → GWAS converts to 0-100 |
| `realized_profit` | `pnl_7d` | float | SOL |
| `buy + sell` | `trades_7d` | int | Total trades = buy count + sell count |
| `pnl_stat.token_num` | `token_num` | int | Unique tokens traded |
| `common.tags` | `tags` | list[str] | GMGN category tags |
| `common.name` | `name` | str | Fallback: `common.ens` |
| `native_balance` | `native_balance` | str | SOL balance (string!) |

---

### 3. `/v1/user/wallet_activity` — Wallet Recent Trades

**Purpose:** Fetch recent trades list untuk satu wallet.

**Request:**
```
GET /v1/user/wallet_activity?chain=sol&wallet_address={ADDR}&limit=50
```

**Parameters:**
| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `chain` | string | `"sol"` | Blockchain |
| `wallet_address` | string | **Required** | |
| `limit` | int | 50 | Max trades |

**Used by:** `conviction.py` → `score_win_streak()`, `score_volume_consistency()`, `score_wallet_diversity()`

**Response `data.list[]` (atau `data` jika langsung array):**
```json
[
  {
    "transaction_hash": "5x...",
    "base_address": "TOKEN...",
    "side": "buy",
    "amount_usd": 500.0,
    "amount_sol": 2.5,
    "pnl_sol": 1.2,
    "pnl": 1.2,
    "timestamp": 1718123456
  }
]
```

⚠️ **Response format bisa `list` langsung atau `{"list": [...]}`** — GWAS handles both di `fetch_wallet_activity()`:
```python
if isinstance(data, list):
    return data
return data.get("list", []) if isinstance(data, dict) else []
```

---

### 4. `/v1/token/info` — Token Information

**Purpose:** Fetch token metadata, price, LP, creation timestamp.

**Request:**
```
GET /v1/token/info?chain=sol&address={TOKEN_MINT}
```

**Response `data`:**
```json
{
  "address": "BONK...",
  "symbol": "BONK",
  "name": "Bonk",
  "decimals": 5,
  "total_supply": "100000000000000",
  "price": {
    "price": 0.00001234,
    "price_24h_change": 0.15,
    "liquidity": 50000.0,
    "liquidity_usd": 50000.0
  },
  "pool": {
    "liquidity": 50000.0,
    "liquidity_usd": 50000.0
  },
  "creation_timestamp": 1718123000,
  "created_at": "2024-06-11T12:30:00Z",
  "launchpad": "pump.fun"
}
```

**Used by:** `safety.py:fetch_token_info()` → `check_token_safety()`

**GWAS LP Extraction (multi-field fallback):**
```python
# safety.py lines 171-177
lp_usd = (price.get("liquidity") or price.get("liquidity_usd")
          or token_info.get("liquidity")
          or pool.get("liquidity")
          or pool.get("liquidity_usd")
          or token_info.get("lp_usd", 0))
```

---

### 5. `/v1/token/security` — Token Security / Rugcheck

**Purpose:** Fetch security analysis (holder concentration, rug risk).

**Request:**
```
GET /v1/token/security?chain=sol&address={TOKEN_MINT}
```

**Response `data`:**
```json
{
  "address": "BONK...",
  "top_10_holder_rate": 0.35,
  "rug_risk": "low",
  "is_honeypot": false,
  "is_mintable": false,
  "is_mutable": true,
  "dex_paid": true,
  "holders_count": 50000
}
```

**Used by:** `safety.py:fetch_rugcheck()` → `check_token_safety()` (holder concentration)

**Key Field:**
- `top_10_holder_rate` → **0-1 range** — GWAS converts `× 100` untuk percentage display
- Nilai null → GWAS skip holder concentration check entirely

---

## 🏷️ Real Field Names vs Docs

### API Consistency Issues

GMGN API field names **tidak konsisten** antar endpoint. Berikut mapping yang ditemukan selama development:

| Logical Field | Endpoint 1 | Endpoint 2 | Notes |
|---------------|------------|------------|-------|
| Wallet address | `maker` (smartmoney) | `wallet_address` (wallet_stats) | Berbeda! |
| Token address | `base_address` (smartmoney) | `address` (token/info) | Berbeda! |
| Win rate | `pnl_stat.winrate` (wallet_stats) | N/A | 0-1 range |
| PnL | `realized_profit` (wallet_stats) | `pnl_sol` / `pnl` (wallet_activity) | Berbeda! |
| Tags | `maker_info.tags` (smartmoney) | `common.tags` (wallet_stats) | Berbeda nesting! |
| Wallet name | `maker_info.name` (smartmoney) | `common.name` / `common.ens` (wallet_stats) | Berbeda nesting! |
| LP amount | `price.liquidity` / `price.liquidity_usd` | `pool.liquidity` / `pool.liquidity_usd` | Multiple paths |
| Token age | `creation_timestamp` (Unix epoch) | `created_at` (ISO string) | Bisa ms atau s |
| Trade amount | `amount_usd` + `quote_amount` (smartmoney) | `amount_sol` / `amount_usd` (wallet_activity) | Currency beda |

### GWAS Field Mapping Strategy

GWAS menggunakan **multi-path fallback** untuk setiap field:

```python
# wallet_scanner.py:normalize_wallet_data()
wr_7d = raw.get("wr_7d") or raw.get("winrate") or raw.get("wr") or raw.get("winrate_7d", 0)
pnl_7d = raw.get("pnl_7d") or raw.get("pnl") or raw.get("realized_profit", 0)
trades_7d = raw.get("trades_7d") or raw.get("trade_count") or raw.get("trades", 0)
```

Ini memungkinkan GWAS handle response dari berbagai endpoint dengan field names berbeda.

---

## 📦 Response Examples

### Full Smartmoney Response

```json
{
  "code": 0,
  "message": "success",
  "data": {
    "list": [
      {
        "maker": "7MvB3gB1xQn6SxGkHU5gGtFNYA8C7zVkYE2eZmpF9rJu",
        "base_address": "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263",
        "base_token": {
          "symbol": "BONK",
          "name": "Bonk",
          "decimals": 5,
          "logo": "https://..."
        },
        "quote_address": "So11111111111111111111111111111111111111112",
        "quote_token": {
          "symbol": "SOL",
          "decimals": 9
        },
        "side": "buy",
        "amount_usd": 1234.56,
        "quote_amount": 7.89,
        "base_amount": 100000000,
        "price_usd": 0.00001234,
        "timestamp": 1718123456,
        "transaction_hash": "5xQn6SxGkHU5gGtFNYA8C7zVkYE2eZmpF9rJu...",
        "maker_info": {
          "name": "xXSmartDegenXx",
          "tags": ["smart_degen", "sniper"],
          "avatar": "https://...",
          "followers": 420
        }
      }
    ]
  }
}
```

### Full wallet_stats Response

```json
{
  "code": 0,
  "data": {
    "wallet_address": "7MvB3gB1xQn6SxGkHU5gGtFNYA8C7zVkYE2eZmpF9rJu",
    "native_balance": "100.500000000",
    "realized_profit": 45.678,
    "unrealized_profit": 12.345,
    "buy": 15,
    "sell": 8,
    "pnl_stat": {
      "winrate": 0.6522,
      "token_num": 12,
      "avg_hold_minutes": 45,
      "best_token": "BONK...",
      "best_token_pnl": 25.0
    },
    "common": {
      "name": "Smart Degen #42",
      "ens": "smartdegen.sol",
      "tags": ["smart_degen", "sniper"],
      "avatar": "https://..."
    }
  }
}
```

---

## ⚠️ Data Quality Issues & Fallbacks

### Issue 1: `creation_timestamp = 0`

**Problem:** Beberapa token (native tokens, very old tokens) return `creation_timestamp = 0`.

**GMGN Response:**
```json
{
  "creation_timestamp": 0,
  "created_at": null
}
```

**GWAS Fallback:**
```python
if created_ts > 0:
    if created_ts > 10_000_000_000:
        created_ts = created_ts / 1000  # ms → s
    age_minutes = (now - fromtimestamp(created_ts)).total_seconds() / 60
else:
    age_minutes = 999999  # Assume ancient — passthrough safety check
```

**Rationale:** Kalau timestamp unknown, token is probably old → no reason to block. Fresh tokens always have valid creation_timestamp.

### Issue 2: `creation_timestamp` in Milliseconds

**Problem:** GMGN sometimes returns milliseconds instead of seconds (`1718123456000` vs `1718123456`).

**GWAS Detection:**
```python
if created_ts > 10_000_000_000:
    created_ts = created_ts / 1000
```

Threshold `10B` = year 2286 in seconds → safe heuristic.

### Issue 3: `lp_usd = 0` or Missing

**Problem:** Native tokens (SOL) atau tokens dengan pool belum initialized return `lp_usd = 0` atau semua liquidity fields null.

**GWAS Fallback:**
```python
if lp_usd > 0 and lp_usd < MIN_LP_USD:
    flags.append(f"low_lp_${lp_usd:.0f}")
    result.passed = False
# If lp_usd == 0 → skip LP check entirely
```

**Rationale:** Don't penalize tokens just because GMGN doesn't have LP data. Tokens with actual low LP (< $5K) will be caught when `lp_usd > 0`.

### Issue 4: Winrate in 0-1 Range

**Problem:** `pnl_stat.winrate` returns 0.65 (65%) instead of 65.

**GWAS Normalization:**
```python
wr_7d = float(wr_7d)
if 0 < wr_7d <= 1:
    wr_7d = wr_7d * 100
```

### Issue 5: `native_balance` is a String

**Problem:** `wallet_stats.native_balance` returns `"100.500000000"` (string, not float).

**GWAS:** Doesn't use this field currently, tapi documented untuk awareness.

### Issue 6: Response Format Inconsistency

**Problem:** Beberapa endpoint return `{"list": [...]}`, yang lain return `[...]` langsung.

**GWAS:** Semua fungsi data-access punya dual-path parsing:
```python
# wallet_activity
if isinstance(data, list):
    return data
return data.get("list", []) if isinstance(data, dict) else []

# smartmoney
return data.get("list", []) if isinstance(data, dict) else []
```

---

## 🚦 Rate Limits (Observed)

GMGN **tidak mendokumentasikan** rate limit secara publik. Berdasarkan observasi:

| Limit | Value | Evidence |
|-------|-------|----------|
| Per-second | ~2-3 req/s | Occasional 429 setelah burst > 3 req dalam 1 detik |
| Per-minute | ~50-100 req | Tidak pernah kena 429 dalam scan cycle normal |
| Per-hour | Unknown | Belum pernah trigger |

**GWAS Mitigation:**
1. **Caching agresif** — 3 cache instance dengan TTL berbeda (5min/15min/1h)
2. **Serial wallet_stats** — 1 request per wallet, bukan concurrent
3. **Max 50 wallets per cycle** — `limit=50` di smartmoney endpoint
4. **Tag filter pre-fetch** — hanya fetch stats untuk wallets dengan matching tags (mengurangi ~60% calls)

---

## 💾 Caching Strategy

### Architecture

```
┌─────────────────────────────────────────────────────────┐
│                   requests-cache SQLite                   │
│                                                          │
│  ┌─────────────────┐  ┌─────────────────┐  ┌──────────┐ │
│  │ cache_token     │  │ cache_holders   │  │ cache_   │ │
│  │ .sqlite         │  │ .sqlite         │  │ rugcheck │ │
│  │                 │  │                 │  │ .sqlite  │ │
│  │ TTL: 5 min      │  │ TTL: 15 min     │  │ TTL: 1h  │ │
│  ├─────────────────┤  ├─────────────────┤  ├──────────┤ │
│  │ /v1/token/info  │  │ /v1/token/      │  │ /v1/token│ │
│  │ LP, age, price, │  │ security        │  │ /security│ │
│  │ symbol, supply  │  │ top10_holders   │  │ rug_risk │ │
│  └─────────────────┘  └─────────────────┘  └──────────┘ │
│                                                          │
│  Each cache is a SEPARATE SQLite database file.          │
│  Different TTLs → different databases → no conflicts.    │
└─────────────────────────────────────────────────────────┘
```

### Implementation

```python
# safety.py lines 25-41

def _get_cached_session(cache_name: str, expire_after: int = 300) -> requests.Session:
    if HAS_CACHE:
        return requests_cache.CachedSession(
            cache_name=cache_name,
            backend="sqlite",
            expire_after=expire_after,
            allowable_methods=("GET",),
            allowable_codes=(200,),
        )
    return requests.Session()  # Graceful fallback

_token_cache    = _get_cached_session("/opt/gwas/data/cache_token", expire_after=300)
_holder_cache   = _get_cached_session("/opt/gwas/data/cache_holders", expire_after=900)
_rugcheck_cache = _get_cached_session("/opt/gwas/data/cache_rugcheck", expire_after=3600)
```

### Fallback: requests_cache Not Installed

```python
try:
    import requests_cache
    HAS_CACHE = True
except ImportError:
    HAS_CACHE = False
```

Kalau `requests-cache` ga terinstall, GWAS fallback ke `requests.Session()` biasa — tidak ada caching, setiap request fresh. Log warning direkomendasikan.

### Cache Hit/Miss Behavior

- **Cache hit:** Response dari SQLite, 0ms latency, 0 API call
- **Cache miss:** API call normal, response disimpan di SQLite
- **Cache expired:** Stale data dihapus, API call baru
- **API error (non-200):** Tidak dicache (`allowable_codes=(200,)`)

---

## 🛡️ Error Handling Patterns

### Pattern 1: Envelope Check (Semua Endpoint)

```python
if resp.status_code == 200:
    data = resp.json()
    if data.get("code") == 0:
        return data.get("data", {})
    else:
        logger.warning(f"GMGN API error: {data.get('error')} — {data.get('message')}")
        return {}
```

### Pattern 2: Network Error (Timeout/DNS/Connection)

```python
except requests.RequestException as e:
    logger.error(f"GMGN request failed {endpoint}: {e}")
    return {}
```

Semua error mengembalikan empty `{}` atau `[]` — **tidak pernah raise exception**. Calling code handle empty return.

### Pattern 3: Missing Data in Safe Pipeline

```python
# safety.py: check_token_safety()
token_info = fetch_token_info(token_address)
if token_info is None:
    result.passed = False
    result.flags = ["no_token_data"]
    return result
```

Safety check **fail-closed** — kalau data tidak tersedia, token dianggap tidak aman.

### Pattern 4: None-Safe Field Access

```python
# wallet_scanner.py: get_wallet_last_trade()
base_token = trade.get("base_token", {}) or {}
return {
    "token_symbol": base_token.get("symbol", "") or trade.get("token_symbol", ""),
    ...
}
```

Semua field access menggunakan `.get()` dengan default. `or {}` pattern untuk handle `None` response dari API.

---

## 🏷️ GMGN Tags Reference

### Known Real Tags (from GMGN API response)

| Tag | Description | Sensitivity |
|-----|-------------|-------------|
| `smart_degen` | Wallet dengan track record profitable di degen plays | PURE_HUMAN, LIKELY_HUMAN, MEDIUM |
| `sniper` | Wallet yang sering snipe token baru | LIKELY_HUMAN, MEDIUM |
| `padre` | High-conviction traders — bigger bets, fewer trades | MEDIUM |
| `axiom` | Upper-tier smart money (belum di-mapping) | Not in default sensitivity |
| `kol` | Key Opinion Leader / influencer wallets | Not in default sensitivity (separate endpoint) |

### Tags yang TIDAK ADA di GMGN API

Tags ini muncul di blueprint v1 tapi **tidak ada** di GMGN API response:

- ❌ `pure_human` — Tidak ada. Diganti `smart_degen`.
- ❌ `likely_human` — Tidak ada. Diganti `smart_degen` + `sniper`.

v1 blueprint mengasumsikan tags ini dari ml-history detector yang ternyata tidak tersedia di API.

### Sensitivity Mapping di GWAS

```python
SENSITIVITY_MAP = {
    "PURE_HUMAN":   ["smart_degen"],                        # Conservative: only proven degens
    "LIKELY_HUMAN": ["smart_degen", "sniper"],              # Moderate: degens + snipers
    "MEDIUM":       ["smart_degen", "sniper", "padre"],     # Standard: all quality wallets
    "ALL":          [],                                     # No filter: everything
}
```

**Rekomendasi:** Gunakan `MEDIUM` untuk daily use — mencakup 3 kategori wallet berkualitas. `ALL` bisa terlalu noisy.

---

## 📝 Usage Notes & Best Practices

### 1. Selalu Cek `code == 0`

GMGN API tidak menggunakan HTTP status codes untuk business logic errors. Response 200 dengan `code: 1` masih mungkin.

### 2. Jangan Concurrent wallet_stats

`/v1/user/wallet_stats` adalah single-wallet endpoint. Concurrent requests = risk rate limit. GWAS menggunakan serial loop:

```python
for addr in wallet_addresses:
    data = _gmgn_get("/v1/user/wallet_stats", params={...})
    if data:
        results.append(...)
```

### 3. Cache Token Data

Token info jarang berubah untuk token > 1 jam. Caching 5 menit aman untuk token baru (LP/age bisa berubah), tapi 15 menit lebih baik untuk holder data.

### 4. Handle Missing Tags

Tidak semua wallet punya tags. Selalu cek `maker_info.tags` exists:

```python
maker_info = trade.get("maker_info", {}) or {}
tags = maker_info.get("tags", [])
```

### 5. WR 0-1 vs 0-100 Conversion

`pnl_stat.winrate` selalu 0-1 range. Jangan asumsikan 0-100:

```python
if 0 < wr_7d <= 1:
    wr_7d = wr_7d * 100
```

### 6. Timestamp ms vs s Detection

Selalu cek magnitude timestamp sebelum parsing:

```python
if created_ts > 10_000_000_000:
    created_ts = created_ts / 1000
```

---

## 🔗 Related Files

- `/opt/gwas/src/wallet_scanner.py` — Semua GMGN API calls untuk wallet discovery
- `/opt/gwas/src/safety.py` — Token info + security API calls + caching
- `/opt/gwas/src/conviction.py` — Wallet activity API calls untuk scoring
- `/opt/gwas/config/settings.yaml` — GMGN section config
- `/home/ubuntu/.gwas_secrets` — GMGN_API_KEY storage
- `/opt/gwas/ARCHITECTURE.md` — Full system architecture
- `/opt/gwas/DEVIATIONS.md` — CB-G4, HP-G4 (caching + data sources)

---

*Referensi ini berdasarkan pengalaman integrasi langsung dengan GMGN OpenAPI v1.*
*Field names dan response structures bisa berubah tanpa pemberitahuan. Jika ada perubahan,*
*update field mapping di `wallet_scanner.py:normalize_wallet_data()` dan `safety.py:check_token_safety()`.*
