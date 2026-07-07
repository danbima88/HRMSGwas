# 🏗️ Arsitektur GWAS v2.3 — GMGN Wallet Alert System

> **Dokumentasi komprehensif** — seluruh modul, data flow, systemd orchestration, dan infrastruktur.
> Terakhir diperbarui: 15 Juni 2026 (v2.3: Solauto bridge V11 + job desk separation)

---

## 📦 Daftar Isi

1. [Ringkasan Sistem](#-ringkasan-sistem)
2. [Infrastruktur](#-infrastruktur)
3. [Diagram Data Flow](#-diagram-data-flow)
4. [Komponen Inti](#-komponen-inti)
5. [GMGN API Integration Layer](#-gmgn-api-integration-layer)
6. [Pipeline: Wallet → Safety → Conviction → Alert](#-pipeline-wallet--safety--conviction--alert)
7. [GWAS→Solauto Bridge (V11)](#-gwasolauto-bridge-v11--june-2026)
8. [Job Desk Separation](#-job-desk-separation)
9. [Database & Caching](#-database--caching)
10. [Helius Webhook & Trade Correlation](#-helius-webhook--trade-correlation)
11. [Systemd / Cron Orchestration](#-systemd--cron-orchestration)
12. [Notification Relay](#-notification-relay)
13. [Performance Tracking](#-performance-tracking)
14. [Konfigurasi](#-konfigurasi)
15. [Edge Cases & Error Handling](#-edge-cases--error-handling)
16. [Poin Kritis & Potensi Masalah](#-poin-kritis--potensi-masalah)

---

## 📌 Ringkasan Sistem

**GWAS v2.3 (GMGN Wallet Alert System)** adalah *Wallet Discovery Engine* yang menemukan wallet Solana profitable dari GMGN API, men-skoring kualitas wallet, dan mengirimkan RAW data ke Solauto untuk trade decisions. GWAS **tidak mengeksekusi trade** dan **tidak melakukan conviction scoring untuk trading** — semua trade entry dan conviction scoring dilakukan oleh Solauto.

### Filosofi Desain

| Prinsip | Implementasi |
|---------|-------------|
| **Read-only** | Zero trade execution. GWAS = wallet discovery layer, Solauto = trade decision & execution layer |
| **GMGN-Native** | Semua data wallet/token dari GMGN OpenAPI v1, bukan Helius RPC |
| **Conviction-gated alerts** | Hanya kirim alert Telegram kalau score ≥ 70/100 (6-factor scoring) |
| **Job desk separation** | GWAS: scan wallets, quality filter, raw bridge. Solauto: ConvictionEngine, trade decisions, position tracking |
| **Safety-first** | Token wajib lolos LP check, age check, holder concentration |
| **File relay fallback** | Kalau Telegram API gagal → save ke file → cron relay pickup |
| **Rate-limited** | Per-cycle, hourly, daily, burst cooldown — anti spam |

### Quick Stats

```
24 files · ~3500 lines · 9 modul src/ · 3 script · 1 config YAML · 1 systemd timer
Database: SQLite WAL mode · 7 tabel · 8 index · 3 cache SQLite · daily backup
GMGN API: 3 endpoint aktif + gmgn-cli trending · Ed25519 keypair auth · 3 sensitivity level
External: GMGN Daily Brief cron (Hermes) · Solauto Bridge V11 (RAW data, ≤5s latency)
```

---

## 🖥️ Infrastruktur

| Komponen | Detail |
|----------|--------|
| **VPS** | Linux (Ubuntu), path project: `/opt/gwas/` |
| **Python venv** | `/opt/gwas/venv/` (Python 3.11) |
| **GMGN API** | `https://openapi.gmgn.ai` — X-APIKEY auth + timestamp/client_id params |
| **Helius API** | `https://api.helius.xyz/v0` — Webhook + RPC (monitoring only) |
| **Telegram** | Bot API via file relay (`/opt/gwas/data/pending_alerts/`) |
| **Systemd timer** | `gwas-scanner.timer` — OnCalendar `*:0/5` (every 5 min) |
| **Database** | SQLite WAL mode di `/opt/gwas/data/gwas.db` |
| **Secrets** | `/home/ubuntu/.gwas_secrets` — HELIUS_API_KEY, GMGN_API_KEY, WEBHOOK_SECRET |

### Dependencies Python (`requirements.txt`)

```
requests          # HTTP client (GMGN API, Helius, Telegram, rugcheck.xyz)
requests-cache    # SQLite-backed HTTP caching
flask             # Helius webhook server
pyyaml            # Config loading (settings.yaml)
```

### External API Services

| API | Fungsi | Rate Limit | Auth |
|-----|--------|-----------|------|
| GMGN OpenAPI | Wallet discovery, token info, wallet stats | Undocumented (assumed tight) | X-APIKEY + timestamp + client_id (UUID) |
| Helius | Webhook receiver, RPC | 50K req/day (free tier) | API key in URL param |
| rugcheck.xyz | Token security/rug check | Uncached | None (via GMGN `/v1/token/security`) |
| Telegram Bot API | Alert delivery | ~30 msg/sec | Bot token |

---

## 🌊 Diagram Data Flow

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                           GWAS v2.0 DATA PIPELINE                            │
│                                                                              │
│  SYSTEMD TIMER (every 5min)                                                  │
│       │                                                                      │
│       ▼                                                                      │
│  scripts/run_scanner.py --once                                               │
│       │                                                                      │
│       ▼                                                                      │
│  ┌─────────────────────────────────────────────────────────────────────┐    │
│  │  1. WALLET SCANNER  (wallet_scanner.py)                             │    │
│  │                                                                      │    │
│  │  _fetch_smartmoney_trades()                                          │    │
│  │    └── GET /v1/user/smartmoney?chain=sol&limit=50                   │    │
│  │         ◄── GMGN OpenAPI (X-APIKEY auth)                             │    │
│  │                                                                      │    │
│  │  ├── Group by maker address → wallet_trades{}                        │    │
│  │  ├── Filter by SENSITIVITY_MAP tags (smart_degen/sniper/padre)       │    │
│  │  ├── fetch_wallet_stats() per wallet                                 │    │
│  │  │     └── GET /v1/user/wallet_stats (serial, one per wallet)       │    │
│  │  ├── normalize_wallet_data() → wr_7d, pnl_7d, trades_7d              │    │
│  │  ├── quality_filter() → WR≥30%, PnL>0, trades≥10                    │    │
│  │  └── return qualified wallets [{...last_trade...}]                   │    │
│  └──────────────────────┬──────────────────────────────────────────────┘    │
│                         │                                                    │
│                         ▼                                                    │
│  ┌─────────────────────────────────────────────────────────────────────┐    │
│  │  2. SAFETY CHECK  (safety.py)                                       │    │
│  │                                                                      │    │
│  │  check_token_safety(token_address)                                   │    │
│  │    ├── fetch_token_info()                                            │    │
│  │    │     └── GET /v1/token/info (cached 5min)                       │    │
│  │    ├── LP check → ≥ $5,000                                           │    │
│  │    ├── Age check → ≥ 30 menit (fallback 999999 for creation_ts=0)    │    │
│  │    ├── Holder concentration → top10 < 50%                            │    │
│  │    │     └── GET /v1/token/security (cached 1h)                     │    │
│  │    └── return SafetyResult(passed, reason, flags)                    │    │
│  └──────────────────────┬──────────────────────────────────────────────┘    │
│                         │ (only if passed)                                   │
│                         ▼                                                    │
│  ┌─────────────────────────────────────────────────────────────────────┐    │
│  │  3. CONVICTION SCORING  (conviction.py)                             │    │
│  │                                                                      │    │
│  │  compute_conviction() → 6-factor scoring + multi-timeframe consistency bonus  │    │
│  │    ├── WR score (0-30% → 0, 80%+ → 1.0)                             │    │
│  │    ├── PnL score (logarithmic, 100 SOL → 1.0)                        │    │
│  │    ├── Win streak (last 10 trades hit rate)                          │    │
│  │    ├── Token age bonus (0.5h → 24h linear)                           │    │
│  │    ├── Volume consistency (CV of trade sizes)                        │    │
│  │    └── Wallet diversity (unique tokens ratio)                        │    │
│  │                                                                      │    │
│  │  Weighted sum → score 0-100                                          │    │
│  │                                                                      │    │
│  │  ── Multi-Timeframe Consistency Bonus (consistency.py) ──────────    │    │
│  │    ├── Fetch trending 1m/5m/1h/6h/24h via gmgn-cli (cached 5m)     │    │
│  │    ├── Group tokens by address → consistency_count (0-5)            │    │
│  │    ├── ≥4 timeframes → +8 bonus  (multi_tf_strong)                  │    │
│  │    ├── ≥3 timeframes → +5 bonus  (multi_tf_medium)                  │    │
│  │    ├── ≥2 timeframes → +2 bonus  (multi_tf_weak)                    │    │
│  │    └── Token tanpa consistency → bonus 0 (no penalty)               │    │
│  │                                                                      │    │
│  │  Final score = base_score + consistency_bonus                       │    │
│  │  Threshold: 85 (default)                                             │    │
│  └──────────────────────┬──────────────────────────────────────────────┘    │
│                         │ (only if score ≥ 85)                               │
│                         ▼                                                    │
│  ┌─────────────────────────────────────────────────────────────────────┐    │
│  │  4. ALERT SENDER  (alert.py)                                        │    │
│  │                                                                      │    │
│  │  AlertSender.send_alert()                                            │    │
│  │    ├── Rate limit check (per_cycle=10, per_hour=8, per_day=40)       │    │
│  │    ├── Duplicate check (same wallet+token in last 1h)                 │    │
│  │    ├── Format: compact 1-line + expanded full (Hybrid Option C)       │    │
│  │    ├── Send: Telegram API with Markdown parse_mode                    │    │
│  │    └── Fallback: save JSON to data/pending_alerts/                   │    │
│  │                                                                      │    │
│  │  DB: insert_alert() → alerts table                                   │    │
│  │  DB: upsert_wallet() → wallets table (with last_trade_json)          │    │
│  └──────────────────────┬──────────────────────────────────────────────┘    │
│                         │                                                    │
│                         ▼                                                    │
│  ┌─────────────────────────────────────────────────────────────────────┐    │
│  │  GWAS-NOTIFICATION-RELAY (separate cron)                             │    │
│  │    - Pick up JSON files from data/pending_alerts/                    │    │
│  │    - Hermes send_message → Telegram channel                          │    │
│  └─────────────────────────────────────────────────────────────────────┘    │
│                                                                              │
│  ═══════════════════════════════════════════════════════════════════════    │
│  SEPARATE THREAD: HELIUS WEBHOOK SERVER                                      │
│                                                                              │
│  ┌─────────────────────────────────────────────────────────────────────┐    │
│  │  Flask app (helius_webhook.py) on port 8080                          │    │
│  │                                                                      │    │
│  │  POST /webhook                                                       │    │
│  │    ├── HMAC-SHA256 signature check (CB-G3)                           │    │
│  │    ├── IP allowlist check (Helius IP ranges)                         │    │
│  │    ├── Parse SWAP transactions                                       │    │
│  │    └── extract_trade_from_webhook() → process_webhook_trades()       │    │
│  │                                                                      │    │
│  │  POST /webhook/direct (alternative, no Helius envelope)              │    │
│  │  GET /health                                                         │    │
│  └──────────────────────────┬──────────────────────────────────────────┘    │
│                             │                                                │
│                             ▼                                                │
│  ┌─────────────────────────────────────────────────────────────────────┐    │
│  │  TRADE CORRELATOR (correlator.py)                                    │    │
│  │                                                                      │    │
│  │  process_webhook_trades()                                            │    │
│  │    ├── correlate_trade() → match by token+wallet within 4h window     │    │
│  │    ├── mark_alert_executed() → alerts.executed=TRUE                  │    │
│  │    ├── insert_trade() → trades table                                 │    │
│  │    └── manual_extend_correlation() → via "✅ taken" reply (24h)     │    │
│  └─────────────────────────────────────────────────────────────────────┘    │
│                                                                              │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## 🔗 GWAS→Solauto Bridge (V11 — June 2026)

GWAS mengirimkan **RAW wallet/token data** ke Solauto melalui bridge signal file. Bridge V11 (refactor 15 Juni 2026) memisahkan secara tegas: GWAS hanya forward data mentah, Solauto yang melakukan semua trade scoring.

### Bridge Flow

```
GWAS (wallet discovery)
    │
    ├── Quality filter lolos? (WR≥30%, PnL>0, trades≥10)
    │
    ├── Safety check lolos? (LP≥$5K, age≥30min, top10<50%)
    │
    ├── Write RAW signal JSON
    │     └── /opt/solauto/signals/gwas_{id}.json
    │
    ▼
Solauto Signal Importer
    ├── Baca gwas_{id}.json
    ├── ConvictionEngine → compute trade conviction
    ├── Paper position decision (entry/skip)
    └── Position tracking
```

### Signal Format (`gwas_{id}.json`)

```json
{
  "id": "abc123",
  "source": "gwas",
  "wallet_address": "AGcexQ1Q...",
  "token_address": "So11111111111111111111111111111111111111112",
  "token_symbol": "BONK",
  "action": "BUY",
  "amount_sol": 2.50,
  "wallet_quality_score": 87.5,
  "wallet_wr_7d": 55.0,
  "wallet_pnl_7d": 12.5,
  "wallet_trades_7d": 23,
  "gmgn_link": "https://gmgn.ai/...",
  "photon_link": null,
  "timestamp": "2026-06-15T12:00:00Z"
}
```

### Key Bridge Properties

| Property | Value | Notes |
|----------|-------|-------|
| **Data type** | RAW wallet/token data only | NO `conviction_score` field |
| **Conviction scoring** | Done by Solauto ConvictionEngine | GWAS does NOT compute trade conviction |
| **Latency** | ≤5 seconds | File write → Solauto pickup |
| **Signal path** | `/opt/solauto/signals/gwas_{id}.json` | Single file per signal |
| **Whitelist path** | `/opt/solauto/signals/whitelist_{id}.json` | Top 5 wallets bypass threshold (v2.3.0) |

### What GWAS Does NOT Send

- ❌ `conviction_score` — Solauto's ConvictionEngine handles all trade scoring
- ❌ Trade entry decisions — Solauto's paper trading engine decides
- ❌ Position tracking data — Solauto maintains its own position ledger

---

## 📋 Job Desk Separation

Pemisahan tanggung jawab antara GWAS dan Solauto per V11 refactor (15 Juni 2026):

### GWAS = Wallet Discovery Engine

| Tanggung Jawab | Detail |
|----------------|--------|
| **Wallet scanning** | Fetch smart money wallets dari GMGN API |
| **Quality filtering** | WR≥30%, PnL>0, trades≥10 |
| **Safety checking** | LP, token age, holder concentration |
| **Raw data forwarding** | Bridge wallet/token data ke Solauto (NO conviction_score) |
| **Telegram alerts** | Notifikasi wallet discovery (BUY only, conviction-gated ≥70) |
| **Performance tracking** | Weekly reports, trade correlation via Helius webhook |

### BUKAN Tanggung Jawab GWAS

| Bukan GWAS | Ditangani Oleh |
|------------|----------------|
| ❌ Trade entry decisions | Solauto ConvictionEngine |
| ❌ Conviction scoring untuk trading | Solauto ConvictionEngine |
| ❌ Position tracking (entry/exit) | Solauto position ledger |
| ❌ PnL tracking per position | Solauto paper trading engine |
| ❌ Risk management (stop-loss, sizing) | Solauto risk engine |

### Solauto = Trade Decision & Execution Engine

| Tanggung Jawab | Detail |
|----------------|--------|
| **Signal import** | Baca `gwas_{id}.json` dan `whitelist_{id}.json` dari bridge |
| **Conviction scoring** | Compute trade conviction via ConvictionEngine |
| **Paper trading** | Entry/exit decisions, position tracking |
| **Risk management** | Stop-loss, position sizing, exposure limits |
| **Performance** | PnL tracking, trade journal |

### Why This Separation?

1. **Single Responsibility**: GWAS fokus pada wallet discovery quality, Solauto fokus pada trade execution quality
2. **Independent evolution**: Scoring model bisa di-iterate di Solauto tanpa menyentuh GWAS
3. **Clean data contract**: GWAS kirim RAW data → Solauto consume dan enrich
4. **Avoid double-scoring**: Sebelumnya conviction_score dikomputasi di kedua sistem (inkonsisten)

---

## 🧠 Komponen Inti

### 1. `src/wallet_scanner.py` — Wallet Discovery (432 lines)

| Aspek | Detail |
|-------|--------|
| **Fungsi utama** | `scan_wallets()` — fetch smart money trades dari GMGN, group by maker, filter by tags, fetch stats, apply quality filter, return qualified wallets |
| **Input** | GMGN OpenAPI: `/v1/user/smartmoney`, `/v1/user/wallet_stats` |
| **Output** | List of wallet dicts dengan embedded `last_trade` + fields `wr_7d`, `pnl_7d`, `trades_7d` |
| **Dependencies** | `requests`, `db.py` (get_db, upsert_wallet) |
| **Dipanggil oleh** | `scripts/run_scanner.py` |

**Fungsi Kunci:**

| Fungsi | Lines | Deskripsi |
|--------|-------|-----------|
| `_gmgn_headers()` | 37-39 | Build X-APIKEY auth header |
| `_gmgn_auth_params()` | 42-46 | Build timestamp + client_id (UUID) query params |
| `_gmgn_get()` | 49-70 | Generic GET wrapper dengan auth injection + error handling |
| `_fetch_smartmoney_trades()` | 73-80 | Low-level: fetch raw trades dari `/v1/user/smartmoney` |
| `fetch_smartmoney_wallets()` | 83-106 | Group trades by maker → unique wallet dicts |
| `fetch_wallet_stats()` | 134-163 | Serial fetch wallet_stats per address dari `/v1/user/wallet_stats` |
| `normalize_wallet_data()` | 202-244 | Normalize ke internal format, WR 0-1 → 0-100 conversion |
| `quality_filter()` | 247-259 | CB-G1: WR≥30%, PnL>0, trades≥10 (explicit di dua tempat) |
| `scan_wallets()` | 262-367 | **Main pipeline**: fetch → tag filter → stats → normalize → quality → return |
| `get_wallet_last_trade()` | 370-402 | Get last trade: embedded > DB > fallback smartmoney filter |
| `check_exit_conditions()` | 405-432 | CB-G5: Re-fetch wallet stats, trigger EXIT_ALERT if WR/PnL degraded |

**SENSITIVITY_MAP:**

```python
SENSITIVITY_MAP = {
    "PURE_HUMAN": ["smart_degen"],                              # Conservative
    "LIKELY_HUMAN": ["smart_degen", "sniper"],                  # Moderate
    "MEDIUM": ["smart_degen", "sniper", "padre"],               # Standard
    "ALL": [],                                                  # No tag filter
}
```

Tags ini adalah **real GMGN tags** dari response `maker_info.tags` — bukan `pure_human`/`likely_human` dari detector lama.

---

### 2. `src/safety.py` — Token Safety Filter (239 lines)

| Aspek | Detail |
|-------|--------|
| **Fungsi utama** | `check_token_safety(token_address)` → `SafetyResult` |
| **Input** | Token address (Solana mint) |
| **Output** | Dataclass: `passed: bool`, `token_age_minutes`, `lp_usd`, `top10_holder_pct`, `flags: list[str]` |
| **Caching** | `requests-cache` SQLite — 3 instance dengan TTL berbeda |

**Safety Checks:**

| Check | Threshold | Data Source | Cache TTL |
|-------|-----------|-------------|-----------|
| LP Liquidity | ≥ $5,000 | `/v1/token/info` → `price.liquidity` / `pool.liquidity` | 5 min |
| Token Age | ≥ 30 menit | `/v1/token/info` → `creation_timestamp` | 5 min |
| Holder Concentration | Top 10 < 50% | `/v1/token/security` → `top_10_holder_rate` | 1 hour |

**Cache Instances (SQLite-backed):**

```python
_token_cache    → /opt/gwas/data/cache_token.sqlite    (TTL: 300s  = 5 min)
_holder_cache   → /opt/gwas/data/cache_holders.sqlite   (TTL: 900s  = 15 min)
_rugcheck_cache → /opt/gwas/data/cache_rugcheck.sqlite  (TTL: 3600s = 1 hour)
```

**Edge Cases Handled (CB-G4):**

- `creation_timestamp = 0` → unknown token age → assume old (age_minutes = 999999)
- `creation_timestamp > 10B` → milliseconds instead of seconds → auto-detect and convert
- `lp_usd = 0` → skip LP check entirely (native tokens like SOL, or uninitialized pools)
- Semua field None/missing → graceful fallback, tidak crash

**Helius IP Allowlist (digunakan oleh helius_webhook.py):**

```python
HELIUS_IPS = [
    "34.86.0.0/16", "34.118.0.0/16", "34.126.0.0/16",
    "35.206.0.0/16", "34.36.0.0/16",
]
```

---

### 3. `src/conviction.py` — Conviction Scoring Engine (222 lines)

| Aspek | Detail |
|-------|--------|
| **Fungsi utama** | `compute_conviction(wallet_addr, token_addr, wr_7d, pnl_7d, safety_result)` → `(score_0_to_100, components_dict)` |
| **Input** | Wallet stats + token address + SafetyResult |
| **Output** | Weighted score 0-100 + per-component breakdown |
| **Threshold** | `SCORE_THRESHOLD = 85` (min to alert) |

**6-Factor Scoring + Consistency Bonus:**

| Component | Weight | Function | Range | Description |
|-----------|--------|----------|-------|-------------|
| `wr` | 0.30 | `score_wr()` | 0-1 | Linear: 30%WR→0, 80%WR→1.0 |
| `pnl` | 0.20 | `score_pnl()` | 0-1 | Logarithmic: log10(pnl+1)/2, 0 SOL→0, 100 SOL→1.0 |
| `win_streak` | 0.15 | `score_win_streak()` | 0-1 | Recent win ratio dari last 10 trades |
| `token_age_bonus` | 0.10 | `score_token_age()` | 0-1 | CB-G2: Linear 0.5h→24h (0→1.0) |
| `volume_consistency` | 0.15 | `score_volume_consistency()` | 0-1 | CV of trade sizes: CV=0→1.0, CV≥2→0 |
| `wallet_diversity` | 0.10 | `score_wallet_diversity()` | 0-1 | Unique tokens / total trades ratio × 2 |
| *consistency_bonus* | *bonus* | `src/consistency.py` | +0/+2/+5/+8 | Multi-timeframe trending consistency (v2.2) |

**Weighted Formula:**
```
raw_score = 0.30*wr + 0.20*pnl + 0.15*win_streak + 0.10*token_age + 0.15*volume_consistency + 0.10*diversity
score_100 = round(raw_score * 100) + consistency_bonus
```

**Photon Link Logic:**
```
PHOTON_THRESHOLD = 90
Include photon link ONLY IF: score ≥ 90 AND trade_size > 3× wallet_average
```

---

### 4. `src/alert.py` — Alert Formatter & Sender (266 lines)

| Aspek | Detail |
|-------|--------|
| **Fungsi utama** | `AlertSender.send_alert(alert_dict)` |
| **Format** | Hybrid Option C: compact 1-line preview + expanded full context |
| **Delivery** | Telegram Bot API → file relay fallback |
| **Rate Limiting** | `RateLimiter` class: per_cycle=10, per_hour=8, per_day=40, burst_cooldown=30min |

**RateLimiter Logic:**

```python
class RateLimiter:
    per_cycle: 10          # Max alerts per scan cycle
    per_hour: 8            # Max alerts per sliding hour
    per_day: 40            # Max alerts per calendar day
    burst_cooldown: 30min  # If 5 alerts in 10min window → cooldown
```

**Alert Format:**

```
# Compact (1-line preview)
🔔 GWAS: Wallet 7MvB... BUY BONK — WR 55% | PnL 12.5 SOL | Score 82/100

# Expanded (full context)
🔔 GWAS ALERT #a1b2c3d4
Wallet: `7MvB...`
Token: `BONK...` (BONK)
Action: BUY
Size: 2.50 SOL
Conviction Score: 82/100
Wallet Stats (7d): WR 55%, PnL 12.5 SOL, Trades 23
Current Open: 0 positions
⚠️ FLAGS: none
Execution: [GMGN](https://gmgn.ai/token/...) | [Photon](https://photon-sol.tinyastro.io/en/lp/...)
```

**EXIT_ALERT (CB-G5):**
```
⚠️ GWAS EXIT: Wallet `AbCd...` removed. Still holding BONK (`token...`) — consider manual exit.
Wallet AbCd... WR dropped or PnL negative.
```

**Delivery Pipeline:**

```
send_alert()
  ├── rate_limiter.can_send() → per_cycle / per_hour / per_day / burst check
  ├── db.has_recent_alert() → duplicate check (same wallet+token, 1 jam)
  ├── format_alert_compact() + format_alert_full()
  ├── _send_telegram() → POST https://api.telegram.org/bot{token}/sendMessage
  │     ├── 200 OK → return True
  │     ├── Markdown parse error → retry without parse_mode
  │     └── All retries fail → _save_to_file() fallback
  └── rate_limiter.record_send() → insert rate_log
```

**File Relay Fallback:**

```json
{
  "user_id": "684426474",
  "text": "🔔 GWAS: Wallet ...",
  "category": "alert",
  "timestamp": "2026-06-10T00:46:00"
}
// Saved to: /opt/gwas/data/pending_alerts/alert_20260610_004600.json
```

---

### 5. `src/db.py` — Database Layer (489 lines)

| Aspek | Detail |
|-------|--------|
| **Engine** | SQLite 3, WAL journal mode |
| **Path** | `/opt/gwas/data/gwas.db` |
| **Connection** | Context manager with auto-commit/rollback |
| **Tables** | 7 tables: wallets, alerts, trades, weekly_reports, alert_queue, rate_log |
| **Indexes** | 8 composite/single indexes |
| **Backup** | Daily via SQLite backup API, 30-day retention |

**Complete Schema:**

```sql
-- Wallets: tracked wallet addresses with performance stats
CREATE TABLE wallets (
    address TEXT PRIMARY KEY,
    label TEXT,
    quality_score REAL,
    wr_7d REAL,
    pnl_7d REAL,
    trades_7d INTEGER,
    status TEXT DEFAULT 'ACTIVE',
    added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Alerts: every alert sent, with execution tracking
CREATE TABLE alerts (
    id TEXT PRIMARY KEY,
    wallet_address TEXT,
    token_address TEXT,
    token_symbol TEXT,
    action TEXT,
    conviction_score REAL,
    gmgn_link TEXT,
    photon_link TEXT,
    flags TEXT,                          -- JSON array
    alert_timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    executed BOOLEAN DEFAULT FALSE,
    execute_tx_hash TEXT,
    execute_timestamp TIMESTAMP
);

-- Trades: correlated trades from Helius webhooks
CREATE TABLE trades (
    tx_hash TEXT PRIMARY KEY,
    wallet_address TEXT,
    token_address TEXT,
    action TEXT,
    amount_sol REAL,
    price_usd REAL,
    pnl_sol REAL,
    fee_sol REAL,
    timestamp TIMESTAMP,
    correlated_alert_id TEXT             -- FK to alerts.id
);

-- Weekly reports: persisted performance snapshots
CREATE TABLE weekly_reports (
    week_start DATE PRIMARY KEY,
    alerts_sent INTEGER,
    alerts_executed INTEGER,
    execute_rate REAL,
    executed_pnl_sol REAL,
    independent_pnl_sol REAL,
    profit_factor REAL,
    win_rate REAL,
    best_wallet TEXT,
    worst_wallet TEXT,
    dead_wallets_count INTEGER,
    report_json TEXT
);

-- Alert queue: staging table (optional)
CREATE TABLE alert_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    alert_id TEXT,
    wallet_address TEXT,
    token_address TEXT,
    token_symbol TEXT,
    action TEXT,
    conviction_score REAL,
    gmgn_link TEXT,
    photon_link TEXT,
    flags TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Rate log: every alert send event for rate limiting
CREATE TABLE rate_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

**Indexes (HP-G3):**

```sql
CREATE INDEX idx_alerts_token_ts ON alerts(token_address, alert_timestamp);
CREATE INDEX idx_alerts_id ON alerts(id);
CREATE INDEX idx_trades_tx ON trades(tx_hash);
CREATE INDEX idx_trades_wallet_ts ON trades(wallet_address, timestamp);
CREATE INDEX idx_trades_correlated ON trades(correlated_alert_id);
CREATE INDEX idx_alerts_wallet_ts ON alerts(wallet_address, alert_timestamp);
CREATE INDEX idx_wallets_status ON wallets(status);
CREATE INDEX idx_rate_log_ts ON rate_log(timestamp);
```

**Key Operations:**

| Method | Purpose |
|--------|---------|
| `upsert_wallet()` | Insert/update wallet record (handles last_trade_json) |
| `insert_alert()` | Insert alert with JSON-serialized flags |
| `mark_alert_executed()` | Set executed=TRUE with tx_hash |
| `get_alerts_for_token_wallet()` | Composite lookup for correlation |
| `has_recent_alert()` | Duplicate check within N hours |
| `is_burst_cooldown()` | Check burst cooldown state |
| `backup()` | SQLite backup API + 30-day rotation |
| `get_weekly_stats()` | Aggregated stats for weekly report |

---

### 6. `src/helius_webhook.py` — Webhook Server (159 lines)

| Aspek | Detail |
|-------|--------|
| **Framework** | Flask |
| **Port** | 8080 |
| **Auth** | HMAC-SHA256 + IP allowlist (CB-G3) |
| **Endpoints** | `POST /webhook`, `POST /webhook/direct`, `GET /health` |

**Auth Flow (CB-G3):**

```
1. Extract x-helius-signature header
2. Compute HMAC-SHA256(WEBHOOK_SECRET, raw_body)
3. hmac.compare_digest() constant-time comparison
4. Verify client IP in HELIUS_IPS CIDR ranges
5. Dev mode bypass: GWAS_DEV_MODE=1
```

---

### 7. `src/correlator.py` — Trade Correlator (208 lines)

| Aspek | Detail |
|-------|--------|
| **Fungsi utama** | `process_webhook_trades(trades)` |
| **Window** | 4h default, 24h manual extend via "✅ taken" |
| **Matching** | wallet_address + token_address + time window |

**Trade Extraction:**

```
Helius enhanced transaction
  ├── tokenTransfers[] → extract mint + amount + decimals
  ├── accountData[] → nativeBalanceChange → BUY/SELL direction
  ├── events.swap → nativeInput/nativeOutput → amount confirmation
  └── description → string match "bought"/"sold" → direction fallback
```

**Correlation Logic:**

```python
correlate_trade(trade, window_hours=4):
    cutoff = trade_ts - 4h
    alerts = db.get_alerts_for_token_wallet(token, wallet)
    for alert in alerts (unexecuted, within window):
        return alert.id
    return None
```

---

### 8. `src/performance.py` — Performance Engine (196 lines)

| Aspek | Detail |
|-------|--------|
| **Fungsi utama** | `compute_weekly_report()` → report dict |
| **Metrics** | Alerts sent/executed, PnL, win rate, profit factor, best/worst wallets |
| **Output** | `weekly_reports` table + Telegram formatted message |

**Report Metrics:**

```
Alerts Sent:        N  /  Executed: N  (X% rate)
Executed PnL:       X.XXXX SOL  vs  Independent: X.XXXX SOL
Alert Beat:         ✅ Beating independent  /  ⚠️ Below independent
Win Rate:           X% (executed trades only)
Profit Factor:      X.XX  (gross_profit / |gross_loss|)
Best Wallet:        <address>
Worst Wallet:       <address>
Dead Wallets:       N
```

---

## 🔌 GMGN API Integration Layer

### Auth Flow

```
1. Generate Ed25519 keypair
2. Register public key on gmgn.ai → receive API key
3. Store GMGN_API_KEY in ~/.gwas_secrets

Every request:
  Headers:  X-APIKEY: {GMGN_API_KEY}
  Params:   timestamp={unix_epoch}&client_id={UUID4}
```

### Verified Endpoints

| Endpoint | Method | Params | Used By |
|----------|--------|--------|---------|
| `/v1/user/smartmoney` | GET | `chain=sol`, `limit=50` | wallet_scanner |
| `/v1/user/wallet_stats` | GET | `chain=sol`, `wallet_address=X`, `period=7d` | wallet_scanner |
| `/v1/user/wallet_activity` | GET | `chain=sol`, `wallet_address=X`, `limit=50` | conviction (win_streak, volume, diversity) |
| `/v1/token/info` | GET | `chain=sol`, `address=X` | safety |
| `/v1/token/security` | GET | `chain=sol`, `address=X` | safety (rugcheck) |

### Response Envelope

```json
{
  "code": 0,
  "message": "success",
  "data": { ... }
}
```

Error handling: `code != 0` → log warning + return empty `{}`

### Data Quality Issues & Fallbacks

| Issue | Field | Fallback |
|-------|-------|----------|
| `creation_timestamp = 0` | Token info | Assume ancient (age=999999 min) |
| `creation_timestamp > 10B` | Token info | Convert ms → s |
| `lp_usd = 0` or missing | Token info | Skip LP check entirely |
| `top_10_holder_rate = null` | Security | Skip holder check |
| No tags in maker_info | Smartmoney | Wallet skipped (unless ALL sensitivity) |
| `wr_7d` = 0-1 range | Wallet stats | Convert to 0-100 internally |

---

## 📊 Database & Caching

### Caching Strategy (CB-G4, HP-G4)

```
┌───────────────┐    ┌───────────────┐    ┌───────────────┐
│ cache_token   │    │ cache_holders │    │ cache_rugcheck│
│ .sqlite       │    │ .sqlite       │    │ .sqlite       │
│ TTL: 5 min    │    │ TTL: 15 min   │    │ TTL: 1 hour   │
├───────────────┤    ├───────────────┤    ├───────────────┤
│ LP, age,      │    │ Holder        │    │ Rug/security  │
│ symbol, price │    │ concentration │    │ data          │
│ (fast-changing│    │ (medium churn)│    │ (slow churn)  │
│  for new      │    │               │    │               │
│  tokens)      │    │               │    │               │
└───────────────┘    └───────────────┘    └───────────────┘
```

### Backup Strategy (HP-G2)

```
Daily: Database.backup() → /opt/gwas/data/backups/gwas_YYYYMMDD.db
Retention: 30 days (auto-delete files older than 30 days)
Method: SQLite backup API (not shutil.copy — handles in-progress writes)
```

---

## ⏱️ Systemd / Cron Orchestration

### gwas-scanner.service

```ini
[Unit]
Description=GWAS v2.0 Wallet Scanner (single cycle)
After=network.target

[Service]
Type=oneshot
User=ubuntu
WorkingDirectory=/opt/gwas
EnvironmentFile=/home/ubuntu/.gwas_secrets
Environment=PYTHONPATH=/opt/gwas
ExecStart=/opt/gwas/venv/bin/python3 scripts/run_scanner.py --once
StandardOutput=append:/opt/gwas/logs/scanner.log
StandardError=append:/opt/gwas/logs/scanner_error.log
```

### gwas-scanner.timer

```ini
[Unit]
Description=GWAS v2.0 Scanner Timer (every 5 minutes)
Requires=gwas-scanner.service

[Timer]
OnCalendar=*:0/5
Persistent=true
RandomizedDelaySec=30

[Install]
WantedBy=timers.target
```

**Key:** `RandomizedDelaySec=30` — spreads API calls across 30-second window to avoid thundering herd. `Persistent=true` — missed cycles (e.g., after reboot) fire immediately.

### Shell Wrapper (optional)

```
/opt/gwas/cron/gwas_scanner.sh
  ├── source ~/.gwas_secrets (bila ada)
  ├── set PYTHONPATH=/opt/gwas
  └── exec python3 scripts/run_scanner.py --once
```

---

## 📬 Notification Relay

GWAS menggunakan **file relay** sebagai default delivery mechanism:

```
GWAS AlertSender
    │
    ├── Telegram API (jika TELEGRAM_BOT_TOKEN diset)
    │     ├── sukses → done
    │     ├── Markdown parse error → retry plaintext
    │     └── gagal → fallback ke file relay
    │
    └── File Relay: simpan JSON ke /opt/gwas/data/pending_alerts/
          │
          ▼
    Separate cron: GWAS-NOTIFICATION-RELAY
          │
          ├── Pick up *.json files dari pending_alerts/
          ├── Hermes send_message → Telegram channel
          └── Delete processed files
```

---

## 📈 Performance Tracking

### Weekly Report Pipeline

```
scripts/weekly_report.py [--week YYYY-MM-DD] [--send] [--json]
    │
    ├── compute_weekly_report()
    │     ├── Query alerts sent/executed in week window
    │     ├── Query trades PnL (correlated + independent)
    │     ├── Compute win rate, profit factor
    │     ├── Find best/worst wallets
    │     └── Count dead wallets
    │
    ├── insert_weekly_report() → persist ke DB
    │
    └── format_report_telegram() / JSON output
```

### Success Criteria (dari settings.yaml)

```
Phase 2 (Day 7):
  execute_rate:    20-40%
  net_pnl:         ≥ 0 SOL gross-of-fees
  miss_rate:       < 10%
  false_rate:      < 20%

Phase 3 (Week 4):
  execute_rate:              25-35%
  profit_factor:             > 1.2
  alert_vs_independent_pnl:  > +10%  ← BIGGEST GATE
  wr_executed:               > 35%
  dead_wallets:              < 20% pool
```

---

## ⚙️ Konfigurasi

### `/opt/gwas/config/settings.yaml` (96 lines)

```yaml
# Key sections:
helius:
  api_key: "${HELIUS_API_KEY}"
  webhook_secret: "${HELIUS_WEBHOOK_SECRET:-gwas-v1-default}"

solana:
  user_wallet: "F9Br7smYRp4fSvoo4c5kwQKai74FtQy7T9pzxrqda494"

alert:
  sensitivity: "MEDIUM"              # smart_degen + sniper + padre
  quality_filter:
    min_wr: 30                       # %
    min_pnl: 0                       # SOL
    min_trades: 10
  safety_filter:
    min_token_age_minutes: 30
    min_lp_usd: 5000
    max_top10_holder_pct: 50
  conviction:
    weights:
      wr: 0.30
      pnl: 0.20
      win_streak: 0.15
      token_age_bonus: 0.10
      volume_consistency: 0.15
      wallet_diversity: 0.10
    score_threshold: 70
    photon_threshold: 90

rate_limits:
  per_cycle: 10
  per_hour: 8
  per_day: 40
  burst_cooldown_minutes: 30        # if 5 alerts in 10min

dead_alert:
  threshold_alerts: 15              # alerts without execution = dead
  min_window_weeks: 2
  auto_recommend_threshold: 0.20    # >20% pool dead → recommend removal

correlation:
  default_window_hours: 4
  manual_extend_hours: 24           # via "✅ taken"

database:
  path: "/opt/gwas/data/gwas.db"
  backup:
    daily: true
    retention_days: 30
    path: "/opt/gwas/data/backups/"
```

### Secrets File (`/home/ubuntu/.gwas_secrets`)

```bash
HELIUS_API_KEY="ebba198e-..."
GMGN_API_KEY="your-gmgn-key"
GMGN_PRIVATE_KEY="your-ed25519-private-key"
HELIUS_WEBHOOK_SECRET="random-64-char-hex"
TELEGRAM_BOT_TOKEN="123:abc..."
```

---

## 🛡️ Edge Cases & Error Handling

### Wallet Scanner

| Edge Case | Handling |
|-----------|----------|
| GMGN API returns `code != 0` | Log warning, return `{}`, continue |
| Smartmoney endpoint returns 0 trades | Log warning, return `[]` |
| No wallets match sensitivity tags | Log warning, return `[]` |
| Wallet has no tags at all | Skip (unless ALL sensitivity) |
| `wr_7d` in 0-1 range from GMGN | Auto-convert to percentage (×100) |
| `pnl_7d` negative | Quality filter rejects (≥ 0) |
| `trades_7d < 10` | CB-G1: explicit reject |
| `last_trade.token_address` missing | Skip this wallet |

### Safety Filter

| Edge Case | Handling |
|-----------|----------|
| Token info 404/unavailable | `SafetyResult.passed=False`, flags=["no_token_data"] |
| `creation_timestamp = 0` | Assume ancient (age=999999 min) |
| `creation_timestamp > 10B` (ms vs s) | Auto-convert to seconds |
| `lp_usd = 0` or all liquidity fields None | Skip LP check entirely |
| `top_10_holder_rate` null | Skip holder concentration check |
| `requests_cache` not installed | Graceful fallback to regular `requests.Session` |

### Alert Sender

| Edge Case | Handling |
|-----------|----------|
| `TELEGRAM_BOT_TOKEN` not set | Auto-switch to file relay mode |
| Telegram API returns non-200 | Log error, retry without Markdown parse_mode |
| Both Telegram + fallback fail | Log error, alert is lost (DB already inserted) |
| Duplicate alert (same wallet+token < 1h) | `has_recent_alert()` → skip |
| Rate limit hit (per_cycle/per_hour/per_day) | `RateLimiter.can_send()` → skip |
| Burst cooldown active | `is_burst_cooldown()` → skip |
| `pending_alerts/` directory missing | Auto-create via `os.makedirs(exist_ok=True)` |

### Webhook

| Edge Case | Handling |
|-----------|----------|
| Missing `x-helius-signature` header | Return 401 |
| Invalid HMAC signature | Return 401 |
| Non-Helius IP (production) | Return 403 |
| Non-Helius IP (dev mode: `GWAS_DEV_MODE=1`) | Allow through |
| Invalid JSON body | Return 400 |
| Empty transaction array | Return 200 with message |
| Transaction has no token transfers | Insert trade without correlation |

### Correlation

| Edge Case | Handling |
|-----------|----------|
| Trade with no token_address (SOL transfer) | Insert into trades table, skip correlation |
| No unexecuted alerts match within 4h window | Insert trade, no correlation |
| Alert already executed | Skip in correlation loop |
| Manual extend via "✅ taken" | `manual_extend_correlation()` with 24h window |

---

## 🔴 Poin Kritis & Potensi Masalah

### 1. GMGN API Rate Limits (Unknown)

GMGN OpenAPI tidak mendokumentasikan rate limit. Semua pemanggilan menggunakan:
- Caching agresif (3 cache instance dengan TTL berbeda)
- Serial fetch untuk `wallet_stats` (bukan concurrent)
- Limit 50 wallet per scan (sensitivity filter membatasi lebih jauh)

**Risiko:** Rate limit hit bisa menyebabkan data kosong → scan cycle menghasilkan 0 alerts.

### 2. `wallet_stats` Serial Bottleneck

`fetch_wallet_stats()` memanggil API **satu per satu** untuk setiap wallet address. Dengan 50 wallet × ~500ms response time = 25 detik per cycle. Bisa di-optimalkan dengan concurrent requests di masa depan.

### 3. File Relay — No Guaranteed Delivery

File relay bergantung pada cron terpisah (GWAS-NOTIFICATION-RELAY) yang pickup file dari `pending_alerts/`. Jika cron ini mati atau lambat, alerts bisa menumpuk. Tidak ada monitoring untuk backlog ini.

### 4. `last_trade_json` Column (Implicit Schema)

`wallets` table menyimpan `last_trade` sebagai JSON string di kolom `last_trade_json`, tapi kolom ini **tidak didefinisikan di schema CREATE TABLE**. Bergantung pada SQLite dynamic typing — akan tetap work tapi bisa membingungkan saat debugging.

### 5. Ed25519 Keypair — Private Key Exposure

GMGN auth membutuhkan Ed25519 private key yang disimpan di `~/.gwas_secrets`. Jika file ini exposed, attacker bisa impersonate GMGN API calls.

### 6. Webhook Server — No Process Manager

`helius_webhook.py` adalah Flask dev server — tidak production-ready. Tidak ada gunicorn/systemd untuk webhook service. Jika server crash, Helius webhook events akan lost sampai server di-restart manual.

### 7. Single User Wallet

Sistem dirancang untuk satu user wallet (`F9Br7smYRp4fSvoo4c5kwQKai74FtQy7T9pzxrqda494`). Tidak ada multi-tenant support.

### 8. Markdown Parsing Failures

Telegram Markdown parsing bisa gagal untuk karakter khusus di token symbol/address. Sistem sudah punya retry tanpa parse_mode, tapi ini berarti kehilangan formatting di alert.

---

## 📁 File Inventory

```
/opt/gwas/
├── src/                          # Core library (9 modul, 2494 lines)
│   ├── __init__.py               # v2.2.0
│   ├── wallet_scanner.py         # 432 lines — GMGN wallet discovery
│   ├── safety.py                 # 239 lines — Token safety checks
│   ├── conviction.py             # 222 lines — 6-factor scoring + bonus
│   ├── consistency.py            # 312 lines — Multi-timeframe trending consistency (v2.2)
│   ├── alert.py                  # 266 lines — Alert format & send
│   ├── db.py                     # 489 lines — SQLite operations
│   ├── helius_webhook.py         # 159 lines — Flask webhook server
│   ├── correlator.py             # 208 lines — Trade correlation
│   └── performance.py            # 196 lines — Weekly reports
├── scripts/                      # Entry points (3 script, 586 lines)
│   ├── run_scanner.py            # 435 lines — Main scanner loop (v2.2: +consistency bonus)
│   ├── weekly_report.py          # 82 lines — Report generator
│   └── register_webhook.py       # 148 lines — Helius webhook registration
├── cron/
│   └── gwas_scanner.sh           # 34 lines — Shell wrapper
├── config/
│   └── settings.yaml             # 96 lines — All configuration
├── data/
│   ├── gwas.db                   # Main database (SQLite WAL)
│   ├── cache_token.sqlite        # Token info cache (TTL: 5min)
│   ├── cache_holders.sqlite      # Holder data cache (TTL: 15min)
│   ├── cache_rugcheck.sqlite     # Rug check cache (TTL: 1h)
│   ├── pending_alerts/           # File relay queue
│   └── backups/                  # Daily DB backups (30-day retention)
├── logs/
│   ├── gwas.log                  # Main log (RotatingFileHandler, 10MB)
│   ├── scanner.log               # Systemd stdout
│   └── scanner_error.log         # Systemd stderr
├── docs/                         # Documentation (this file + others)
├── README.md                     # Project overview
├── DEVIATIONS.md                 # Design decisions log
├── BLUEPRINT_GWAS_v1.md          # Original v1.2 blueprint (history)
├── BLUEPRINT_GWAS_v2.md          # Pivot blueprint (vision)
├── requirements.txt              # Python dependencies
└── venv/                         # Python 3.11 virtual environment
```

---

## 🔗 System Interactions

```
┌──────────────┐     ┌──────────────┐     ┌──────────────┐
│   systemd    │     │  run_scanner │     │   GMGN API   │
│   timer      │────▶│  .py --once  │────▶│  OpenAPI v1  │
│ (every 5min) │     │              │     │              │
└──────────────┘     └──────┬───────┘     └──────────────┘
                            │
              ┌─────────────┼─────────────┐
              ▼             ▼             ▼
        ┌──────────┐ ┌──────────┐ ┌──────────┐
        │ Safety   │ │Conviction│ │  Alert   │
        │ Check    │ │ Scoring  │ │ Sender   │
        └──────────┘ └──────────┘ └────┬─────┘
                                       │
         ┌─────────────────────────────┼─────────────────────────────┐
         ▼                             ▼                             ▼
   ┌──────────┐               ┌────────────┐               ┌──────────────┐
   │Telegram  │               │File Relay  │               │   SQLite     │
   │Bot API   │               │(pending/)  │               │   gwas.db    │
   └──────────┘               └─────┬──────┘               └──────────────┘
                                    │
                                    ▼
                          ┌─────────────────┐
                          │GWAS-NOTIFICATION│
                          │-RELAY cron      │
                          │→ Hermes → TG    │
                          └─────────────────┘

━━━ Solauto Bridge V11 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

┌──────────────┐          ┌──────────────────────┐          ┌──────────────┐
│  GWAS Alert  │          │  /opt/solauto/signals/│          │   Solauto    │
│  Sender      │─────────▶│  gwas_{id}.json       │─────────▶│ Conviction   │
│  (RAW data)  │  ≤5 sec  │  (NO conviction_score)│  import  │ Engine       │
└──────────────┘          └──────────────────────┘          └──────┬───────┘
                                                                  │
                                                                  ▼
                                                          ┌──────────────┐
                                                          │ Paper Trade  │
                                                          │ Position     │
                                                          │ (Solauto)    │
                                                          └──────────────┘

┌──────────────┐     ┌──────────────┐     ┌──────────────┐
│   Helius     │     │  Flask       │     │  Correlator  │
│   Webhook    │────▶│  Webhook     │────▶│  match trades│
│   Service    │     │  Server      │     │  → alerts    │
└──────────────┘     └──────────────┘     └──────┬───────┘
                                                 │
                                                 ▼
                                          ┌──────────────┐
                                          │   SQLite     │
                                          │   trades +   │
                                          │   alerts (ex)│
                                          └──────────────┘
```

---

*Dokumen ini merefleksikan kode AKTUAL yang ter-build di `/opt/gwas/` per 15 Juni 2026.*
*Untuk keputusan desain dan deviasi dari blueprint, lihat [DEVIATIONS.md](DEVIATIONS.md).*
