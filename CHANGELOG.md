# 📋 CHANGELOG — GWAS (GMGN Wallet Alert System)

> Riwayat lengkap development dari blueprint sampai production build.
> Format loosely based on [Keep a Changelog](https://keepachangelog.com/).

---

## v2.3.1 — 2026-06-15 — Solauto Bridge V11 Refactor + Job Desk Separation 📋

### Ringkasan

Refactor besar bridge GWAS→Solauto: GWAS sekarang mengirim **RAW wallet/token data only** (tanpa `conviction_score`), semua trade conviction scoring dipindahkan ke Solauto ConvictionEngine. Plus pemisahan job desk secara eksplisit: GWAS = Wallet Discovery Engine, Solauto = Trade Decision & Execution Engine.

### Changed
- **Bridge V11**: GWAS hanya kirim `wallet_address`, `token_address`, `amount_sol`, `wallet_quality_score` + metadata — **NO `conviction_score`** field
- **Conviction scoring**: Sepenuhnya di Solauto ConvictionEngine (sebelumnya dikomputasi di kedua sistem → inkonsisten)
- **Job desk separation**: GWAS = wallet discovery only (scan, quality filter, safety, raw bridge). Solauto = trade decisions, conviction scoring, position tracking
- **ARCHITECTURE.md**: Update v2.2 → v2.3, tambah section GWAS→Solauto Bridge (V11) + Job Desk Separation

### Why
- Single Responsibility: GWAS fokus wallet discovery, Solauto fokus trade execution
- Hindari double-scoring: sebelumnya conviction_score dikomputasi di kedua sistem
- Clean data contract: GWAS kirim RAW → Solauto enrich & decide

### References
- Solauto BLUEPRINT: "GWAS v2.3 Bridge" reference
- Signal path: `/opt/solauto/signals/gwas_{id}.json`
- Latency: ≤5 seconds

---

## v2.3.0 — 2026-06-13 — SELL Suppression + Whitelist Bridge ⭐

### Ringkasan

Dua optimasi besar: **SELL alert gak lagi spam Telegram** (hanya BUY yg dikirim, SELL tetap ke DB + Solauto bridge), dan **wallet whitelist bridge** untuk top 5 wallet copy-trade simulation — bypass conviction threshold, selalu bridge ke Solauto sebagai `whitelist_{id}.json`.

### Added
- **Whitelist bridge** di `run_scanner.py` (line ~340): top 5 wallet SELALU bridge ke `/opt/solauto/signals/whitelist_{id}.json`, regardless of conviction score. Bypass threshold 70 sepenuhnya.
- **`solauto_bridge.wallet_whitelist`** di `config/settings.yaml` — daftar wallet address top 5 untuk copy-trade simulation
- Signal format `whitelist_{id}.json` dengan field `whitelist_alert_id` dan `source: "gwas_whitelist"` — terpisah dari `gwas_{id}.json` normal bridge

### Changed
- **`alert.py` `send_alert()`**: SELL action → return False tanpa kirim Telegram (tetap insert DB + tulis signal bridge). Log: `SELL alert suppressed from Telegram`.
- **Bridge logic refactor**: normal bridge (score ≥70 → `gwas_{id}.json`) + whitelist bridge (always → `whitelist_{id}.json`) sekarang jadi dua blok independen, bukan whitelist gate di dalam normal bridge

### Fixed
- SELL alert spam: sebelumnya SEMUA alert (BUY + SELL) dikirim ke Telegram. Sekarang hanya BUY.

### References
- Top 5 wallets: AGcexQ1Q (WR 94%), 6kpNzDeK (WR 92%), hnu69n6P (WR 85%), 2QeJByxh (WR 81%), 8p4FzY2K (WR 95%)
- Solauto side: `import_whitelist_signals()` di `main.py` + `top5_report.py` weekly report

---

## v2.2.0 — 2026-06-11 — Multi-Timeframe Consistency + Daily Brief 📊

### Ringkasan

GWAS sekarang mempertimbangkan **momentum token di 5 timeframe trending** sebagai bonus conviction — token yang consistently trending di 3+ timeframe dapat bonus +5 s/d +8 poin, mendorong candidate borderline melewati threshold 85. Plus **GMGN Daily Brief** cron: ringkasan pasar Solana pagi hari via Telegram.

### Added
- **`src/consistency.py`** (312 lines) — modul baru: fetch trending 1m/5m/1h/6h/24h via `gmgn-cli`, group by token address, hitung consistency_count (0-5), klasifikasi bonus tier
  - ≥4 timeframe → `multi_tf_strong` (+8 bonus)
  - ≥3 timeframe → `multi_tf_medium` (+5 bonus)
  - ≥2 timeframe → `multi_tf_weak` (+2 bonus)
  - Cached 5 menit (JSON), fail-open, auto-refresh
- **Consistency integration** di `run_scanner.py` — setelah `compute_conviction()` → panggil `get_token_consistency()` → tambah bonus ke final score
- **Alert fields baru**: `consistency_count`, `consistency_bonus`, `consistency_bonus_key` — disimpan di alert dict + DB dedup + Solauto bridge
- **GMGN Daily Brief** (cron `5d239a88d89c`) — 9:00 WIB daily, gabung Trending 24h + Smart Money buys + Fresh grads → Telegram `684426474`

### Changed
- `conviction.py`: threshold `SCORE_THRESHOLD` 70 → 85 (sudah diterapkan sebelumnya, sekarang documented)
- `run_scanner.py`: 411 → 435 lines (+24 lines untuk consistency integration)
- `ARCHITECTURE.md`: update pipeline diagram, component table, module listing, quick stats

### References
- Repo inspirasi: [GMGNAI/gmgn-skills](https://github.com/GMGNAI/gmgn-skills) (335⭐) — workflow analysis
- Repo inspirasi: [yllvar/gmgn-TrendingAnalyzer](https://github.com/yllvar/gmgn-TrendingAnalyzer) (16⭐) — multi-timeframe grouping concept

---

## v2.2.1 — 2026-06-11 — Webhook Correlation Fix 🔧

### Fixed
- **Correlation logic fatal bug**: `correlate_trade()` sebelumnya match by `wallet_address` AND `token_address`, tapi webhook monitor **user wallet** (`F9Br7...`) sedangkan alert dari **smart money wallet** (`5qx7yV4C...`, dll) → gak pernah match. **Fixed**: match by `token_address` only dalam 4h window — user copy-trade smart money signal.
- **`get_alerts_for_token_wallet()`** di `db.py`: support `wallet_address=None` untuk token-only query
- **Webhook `accountAddresses`**: sebelumnya **kosong `[]`** (Helius gak monitor wallet manapun) → update via Helius API PUT ke user wallet
- **`register_webhook.py` config bug**: `os.path.expandvars()` corrupt YAML (expand `${GMGN_API_KEY:-}` → error) → dihapus, pake `yaml.safe_load()` langsung
- **Test E2E correlation**: synthetic trade `test_corr_direct_*` → alert `48456a029fd6` auto-correlated ✅ → test data cleaned up

### Changed
- `src/correlator.py`: token-only matching, enhanced logging (smart wallet info di correlation log)
- `src/db.py`: `get_alerts_for_token_wallet()` parameter `wallet_address` jadi optional
- `scripts/register_webhook.py`: `load_config()` — hapus `os.path.expandvars()`

### Status
- ✅ Webhook server running (gunicorn, port 8080)
- ✅ Firewall open (8080/tcp)
- ✅ Helius webhook active (ID: `b1b489a4-...`)
- ⏳ Waiting for next user trade to trigger first real webhook event

---

## v2.1.0 — 2026-06-10 — Solauto Bridge + Wallet Expansion 🔗

### Ringkasan

GWAS sekarang terhubung langsung ke Solauto Paper Trading melalui bridge signal file. High-conviction alerts (≥70) otomatis jadi paper position di Solauto, bypassing gmgn-cli scanner yang low-tier. 

### Added
- **GWAS → Solauto Bridge**: Signal file JSON ke `/opt/solauto/signals/gwas_{id}.json` untuk score ≥70
- **Solauto Signal Importer**: `import_gwas_signals()` di main.py — baca file, fetch price DexScreener, buat paper position
- **Pooled Signal Engine (Opsi C)**: Group token + dedup wallet + cluster bonus → 1 eksekusi per token
- **Safety Hardening**: max_token_age 24h, min_volume_24h $5K, max_fdv $50M via DexScreener
- **4 Wallet Elite Baru**: CyaE1V (WR 76%), Bi4rd5F (WR 70%), 4vw54B (WR 66%), LuitxR (WR 64%)

### Fixed
- Conviction score float → integer (`round(x)` bukan `round(x,1)`)
- Notifikasi: hapus `$` sebelum SOL, hapus "unknown" untuk tag kosong
- Wallet dedup: same wallet multi-buy = 1 entry di scoring

---

## v2.0.0 — 2026-06-10 — Production Build 🚀

### Ringkasan

GWAS v2.0 adalah **rewrite penuh** dari v1.x blueprint. Semua komponen dibangun dari scratch dengan pendekatan **GMGN-Native** — tidak lagi bergantung pada Helius RPC untuk wallet discovery atau `gmgn-cli` untuk scraping. Arsitektur baru: systemd timer-driven, SQLite-backed, Ed25519 keypair auth ke GMGN OpenAPI.

**Statistik Build:**
- 21 files · 3101 lines of Python
- 8 modul library (`src/`) · 3 script entry point (`scripts/`)
- 1 systemd timer + service · 1 shell wrapper
- 3 SQLite cache database · 7 tabel utama · 8 index
- 3 GMGN API endpoint aktif · 0 Helius RPC untuk discovery

### Perubahan Besar

#### GMGN OpenAPI Integration (Menggantikan gmgn-cli)
- **Sebelum:** v1 blueprint menggunakan `gmgn-cli` command-line tool untuk scrape data wallet.
- **Sekarang:** Semua data dari GMGN OpenAPI v1 (`https://openapi.gmgn.ai`).
  - Auth: Ed25519 keypair → register public key di gmgn.ai → dapat API key
  - Setiap request: `X-APIKEY` header + `timestamp` & `client_id` (UUID) query params
  - Endpoint: `/v1/user/smartmoney`, `/v1/user/wallet_stats`, `/v1/token/info`, `/v1/token/security`

#### Real GMGN Tags Mapping
- **Sebelum:** v1 blueprint menggunakan `pure_human`/`likely_human` dari ml-history detector (tidak ada di GMGN API).
- **Sekarang:** `SENSITIVITY_MAP` menggunakan real GMGN tags dari response `maker_info.tags`:
  ```python
  "PURE_HUMAN":   ["smart_degen"]                        # Conservative
  "LIKELY_HUMAN": ["smart_degen", "sniper"]               # Moderate
  "MEDIUM":       ["smart_degen", "sniper", "padre"]      # Standard
  "ALL":          []                                      # No filter
  ```

#### Direct Smartmoney Endpoint (Menggantikan Wallet Activity Polling)
- **Sebelum:** v1 blueprint polling `wallet_activity` untuk setiap wallet → N+1 API calls.
- **Sekarang:** Single call ke `/v1/user/smartmoney?chain=sol&limit=50` → dapat semua trade dari smart money wallets sekaligus. Group by maker → filter by tags → baru fetch `wallet_stats` untuk candidates yang lolos.

#### SQLite WAL + last_trade_json Column
- Database: SQLite dengan WAL journal mode, 7 tabel, 8 index (HP-G3).
- Kolom `last_trade_json` di tabel `wallets` menyimpan full last trade sebagai JSON string — memungkinkan embedded trade data tanpa join.

#### Systemd Timer Deployment
- **Sebelum:** v1 blueprint spek Flask server + cron job + Telegram bot daemon (over-engineered).
- **Sekarang:** Simple systemd timer:
  ```ini
  # gwas-scanner.timer
  OnCalendar=*:0/5
  Persistent=true
  RandomizedDelaySec=30
  ```
  `gwas-scanner.service` (Type=oneshot) dipanggil setiap 5 menit, menjalankan `run_scanner.py --once`.

#### Token Safety dengan LP/Age Fallback (CB-G4)
- Handle `creation_timestamp=0` → assume ancient (999999 min)
- Handle `creation_timestamp > 10B` → auto-detect milliseconds vs seconds
- Handle `lp_usd=0` atau semua liquidity fields None → skip LP check entirely
- Skip LP check untuk native tokens (SOL) dan uninitialized pools

### Modules Built

| Modul | Lines | Status | Deskripsi |
|-------|-------|--------|-----------|
| `src/__init__.py` | 4 | ✅ Complete | Version 2.0.0 |
| `src/wallet_scanner.py` | 432 | ✅ Complete | GMGN smartmoney + wallet_stats integration |
| `src/safety.py` | 239 | ✅ Complete | Token safety: LP, age, holder concentration, rugcheck |
| `src/conviction.py` | 189 | ✅ Complete | 6-factor scoring engine |
| `src/alert.py` | 266 | ✅ Complete | Hybrid alert format + RateLimiter + file relay |
| `src/db.py` | 489 | ✅ Complete | SQLite WAL: 7 tables, 8 indexes, backup |
| `src/helius_webhook.py` | 159 | ✅ Complete | Flask webhook server: HMAC + IP allowlist |
| `src/correlator.py` | 208 | ✅ Complete | Trade-to-alert matching, 4h/24h window |
| `src/performance.py` | 196 | ✅ Complete | Weekly report compute + Telegram format |
| `scripts/run_scanner.py` | 332 | ✅ Complete | Main scanner loop (--once / --interval) |
| `scripts/weekly_report.py` | 82 | ✅ Complete | Report generator (--send / --json) |
| `scripts/register_webhook.py` | 148 | ✅ Complete | Helius webhook registration |
| `config/settings.yaml` | 96 | ✅ Complete | Full configuration with env var expansion |
| `cron/gwas_scanner.sh` | 34 | ✅ Complete | Shell wrapper with secrets sourcing |

### All CB & HP Fixes (from DEVIATIONS.md)

| ID | Issue | Status |
|----|-------|--------|
| **CB-G1** | Quality Filter — `trades >= 10` check location | ✅ Fixed — enforced in both `quality_filter()` AND `scan_wallets()` |
| **CB-G2** | Conviction Age Bonus — lower bound 1h → 0.5h | ✅ Fixed — `score_token_age()` starts at 30 minutes |
| **CB-G3** | Helius Webhook Security — no auth | ✅ Fixed — HMAC-SHA256 + IP allowlist |
| **CB-G4** | Safety Filter — explicit data sources with caching | ✅ Fixed — 3 cache instances, documented sources, edge case fallbacks |
| **CB-G5** | EXIT_ALERT Type — no exit notification | ✅ Fixed — `check_exit_conditions()` + `send_exit_alert()` |
| **HP-G1** | Dead-Alert Threshold — 5 → 15 alerts, 2 week window | ✅ Fixed |
| **HP-G2** | SQLite Backup — no backup mechanism | ✅ Fixed — SQLite backup API + 30-day rotation |
| **HP-G3** | Database Indexes — no indexes specified | ✅ Fixed — 8 composite indexes |
| **HP-G4** | Token Data Caching — no caching layer | ✅ Fixed — `requests-cache` SQLite, 3 instances |

### Governance Lesson: DEVIATIONS Log

V1 blueprint → V1.1 mengajarkan pentingnya **DEVIATIONS.md**. Awalnya blueprint dianggap "spec final" yang akan diimplementasi exactly. Ternyata:
1. Blueprint punya gaps (auth, caching, data sources — CB-G2, CB-G3, CB-G4)
2. Blueprint punya contradictions (trades >= 10 di Section 2 tapi ga ada di Section 4 — CB-G1)
3. Blueprint punya over-engineering (4-layer auth → simplified ke 2-layer — CB-G3)

**Pelajarannya:** Setiap kali implementasi deviates dari spec, **langsung catat di DEVIATIONS.md dengan justifikasi**. Ini mencegah:
- Auditor/reviewer mengira ada bug ("kok blueprint bilang A, implementasi B?")
- Saling blaming antara blueprint writer dan implementor
- Scope creep tanpa dokumentasi

### Known Limitations (v2.0.0)

1. **GMGN API Rate Limits Unknown** — tidak ada dokumentasi official, bisa tiba-tiba 429
2. **wallet_stats Serial Bottleneck** — 1 wallet per API call, 25-50 detik per cycle
3. **Webhook Server No Process Manager** — Flask dev server, crash = lost events
4. **Single User Wallet** — hardcoded `F9Br7smYRp4fSvoo4c5kwQKai74FtQy7T9pzxrqda494`
5. **`last_trade_json` Implicit Column** — ga ada di CREATE TABLE schema
6. **File Relay No Monitoring** — backlog bisa menumpuk tanpa detection

---

## v1.2 — 2026-06-09 — CB-G3 Revisited

### Perubahan

#### Auth Middleware Before Handler (CB-G3 Fix)
- **Issue:** Original CB-G3 implementation melakukan auth check **di dalam** handler function — kalau check gagal tetap return 401/403, tapi request body sudah diparse duluan. Risk: malformed body bisa bikin exception sebelum auth check.
- **Fix:** Pindahin signature verification + IP check ke **sebelum** `json.loads()`. Body cuma di-parse kalau auth lolos.
  ```python
  # Before (v1.1): body diparse dulu → auth check
  # After (v1.2):  auth check → body diparse
  raw_body = request.get_data()
  if not verify_signature(raw_body, sig): return 401
  if not verify_ip(): return 403
  payload = json.loads(raw_body)  # safe to parse now
  ```

---

## v1.1 — 2026-06-09 — Governance & CB/HP Fixes

### Perubahan

#### DEVIATIONS.md Dibuat
- **File baru:** `/opt/gwas/DEVIATIONS.md` — tracking setiap deviasi dari blueprint.
- Semua CB-G1..CB-G5 dan HP-G1..HP-G4 dicatat dengan justifikasi.
- Termasuk keputusan arsitektur: read-only monitoring, GMGN-native, hybrid alert format, rate limits, correlation window.

#### CB-G1: Quality Filter Redundancy
- `trades >= 10` sekarang dicek di DUA tempat: `quality_filter()` + `scan_wallets()` loop.
- Defensive duplication: kalau salah satu berubah, satunya tetap enforce.

#### CB-G2: Conviction Age Bonus Boundary Fix
- `score_token_age()` starts at 0.5h (30 menit), bukan 1h.
- Linear scaling 0.5h → 24h (0.0 → 1.0).
- Eliminasi dead zone 30-60 menit di mana token lolos safety tapi zero conviction bonus.

#### CB-G3: Webhook HMAC + IP Allowlist
- HMAC-SHA256 signature verification (header: `x-helius-signature`).
- IP allowlist: 5 Helius CIDR ranges.
- Dev mode bypass via `GWAS_DEV_MODE=1`.

#### CB-G4: Safety Filter Data Sources Documented
- `lp_usd` → `/v1/token/info` (price.liquidity)
- `top10_holder_pct` → `/v1/token/security` (top_10_holder_rate)
- `age_minutes` → `/v1/token/info` (creation_timestamp)
- Caching: 5min / 15min / 1h TTL

#### CB-G5: EXIT_ALERT Mechanism
- `check_exit_conditions()` di wallet_scanner: re-fetch stats, deteksi WR drop/PnL negatif.
- `format_exit_alert()` + `send_exit_alert()` di alert module.
- Hanya trigger kalau ada unexecuted alerts (positions still held).

#### HP-G1: Dead Alert Threshold Adjustment
- 5 alerts tanpa execution → 15 alerts, minimum 2-week window.
- SQL: `alert_count >= 15 AND executed_count = 0` dalam 2 minggu.

#### HP-G2: Database Backup
- `Database.backup()` → SQLite backup API → `/opt/gwas/data/backups/gwas_YYYYMMDD.db`
- 30-day retention, auto-rotate.

#### HP-G3: Database Indexes
- 8 indexes: composite untuk correlation query, single untuk lookup.

#### HP-G4: Token Data Caching
- `requests-cache` SQLite backend, 3 instances dengan TTL berbeda.
- Graceful fallback kalau `requests_cache` ga terinstall.

#### Revised Success Criteria
- Phase 2: execute rate 20-40%, net PnL ≥ 0, miss rate < 10%, false rate < 20%.
- Phase 3: execute rate 25-35%, profit factor > 1.2, alert beat independent > +10%.

#### Revised Rate Limits
| Limit | v1.0 | v1.1 |
|-------|------|------|
| Per cycle | — | 10 |
| Per hour | — | 8 |
| Per day | — | 40 |
| Burst cooldown | — | 30min (5 in 10min) |

#### Revised Correlation Window
| Window | Duration |
|--------|----------|
| Default auto-correlation | 4 hours |
| Manual extend (✅ taken) | 24 hours |

---

## v1.0 — (Never Built — Blueprint Only) — Original Concept

### Blueprint Contents

GWAS v1.0 blueprint (BLUEPRINT_GWAS_v1.md) mendefinisikan konsep awal:

1. **Flask Server** — over-engineered: single server untuk webhook + API + scheduler.
2. **4-Layer Auth** — HMAC + API key + IP allowlist + rate limit middleware (CB-G3 later simplified ke 2-layer).
3. **Conviction Formula** — 4-factor scoring (sebelum CB-G2 age bonus fix).
4. **gmgn-cli Scraping** — bergantung pada CLI tool, bukan API (v2.0 rewrite ganti ke GMGN OpenAPI).
5. **Helius RPC untuk Discovery** — v2.0 menghilangkan ini, semua wallet discovery dari GMGN langsung.

### Masalah Blueprint v1.0

| Masalah | Detail | Fixed In |
|---------|--------|----------|
| Over-engineering | 4-layer auth, Flask monolith, scheduler built-in | v2.0 → systemd timer, 2-layer auth |
| gmgn-cli dependency | CLI tool unreliable, no auth, scraping-based | v2.0 → GMGN OpenAPI with Ed25519 auth |
| No caching | Every API call fresh, no rate limit handling | v1.1 → CB-G4, HP-G4 |
| No exit alerts | Wallet degradation silently ignored | v1.1 → CB-G5 |
| Trades >= 10 inconsistency | Spec di Section 2, ga ada di Section 4 | v1.1 → CB-G1 |
| No backup | Database loss risk | v1.1 → HP-G2 |
| Wrong tags | `pure_human`/`likely_human` ga ada di GMGN API | v2.0 → real GMGN tags |

### Why v1.0 Never Reached Build

v1.0 blueprint ditulis sebagai "vision document" — aspirational tapi tidak grounded di API reality. Butuh 2 putaran governance (v1.1 + v1.2) dan 1 full rewrite (v2.0) untuk sampai ke production build.

---

## Timeline Visual

```
  Jun 5-7        Jun 9           Jun 9          Jun 10         Jun 10
  ───────        ─────           ─────          ──────         ──────
  v1.0           v1.1            v1.2           v2.0           v2.0
  Blueprint      Governance      CB-G3          Production     Docs
  written        fixes           revisited      build          written
  (concept)      (DEVIATIONS)    (auth order)   (3101 lines)   (4 docs)

  Status:        Status:         Status:        Status:        Status:
  ❌ Never        ✅ Deviations   ✅ Fix         ✅ Deployed     ✅ Complete
     built          documented      applied        running
```

---

## File Count Evolution

```
v1.0  (blueprint):  1 file    (BLUEPRINT_GWAS_v1.md)
v1.1  (governance): +2 files  (DEVIATIONS.md, partial src/)
v1.2  (fix):        +0 files  (patch existing)
v2.0  (build):      21 files  (8 src/, 3 scripts/, config, cron, data, logs)
v2.0  (docs):       +4 files  (ARCHITECTURE.md, CHANGELOG.md, 2 docs/)
```

---

## Breaking Changes

### v1.x → v2.0

| Change | Impact |
|--------|--------|
| GMGN OpenAPI menggantikan gmgn-cli | Tidak ada backward compat — semua data source baru |
| Real GMGN tags (smart_degen/sniper/padre) | SENSITIVITY_MAP berubah dari `pure_human`/`likely_human` |
| Systemd timer menggantikan Flask scheduler | Deployment model berubah total |
| SQLite WAL menggantikan JSON files | Data persistence format berubah |
| File relay sebagai default delivery | Tidak lagi bergantung pada Telegram Bot API langsung |

---

## Upcoming / Backlog

Berdasarkan BLUEPRINT_GWAS_v2.md (pivot blueprint — vision, belum diimplementasi):

- [ ] **Auto-trader integration** — auto-execute trades based on GWAS alerts (BLUEPRINT_v2_AUTO_TRADER.md → archived as falsified)
- [ ] **Multi-user support** — support lebih dari satu user wallet
- [ ] **Webhook server with gunicorn** — production-grade instead of Flask dev server
- [ ] **Concurrent wallet_stats** — async/threaded fetch untuk mengurangi cycle time
- [ ] **Alert quality scoring** — feedback loop dari user execution rate ke conviction scoring
- [ ] **Dashboard** — web UI untuk melihat alert history dan performance

---

*CHANGELOG maintained by: GWAS development team*
*Last updated: 2026-06-15*
