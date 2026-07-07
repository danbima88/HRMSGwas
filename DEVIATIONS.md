# GWAS v1.1 — DEVIATIONS FROM BLUEPRINT

This document tracks every intentional deviation from the original GWAS v1.1 
blueprint/specification, with justification for each change.

---

## CB-G1: Quality Filter — `trades >= 10` Check Location

**Blueprint Issue:** Section 2 of the blueprint specified `trades >= 10` as a wallet
quality filter, but Section 4's scan loop omitted this check.

**Resolution:** The check is now enforced in **two places**:
1. `wallet_scanner.py:quality_filter()` — the canonical filter function
2. `wallet_scanner.py:scan_wallets()` — explicitly called in the main scan loop

**Justification:** Defensive duplication ensures the filter cannot be bypassed if
`quality_filter()` changes independently.

---

## CB-G2: Conviction Age Bonus — Lower Bound from 1h → 0.5h

**Blueprint Issue:** Original age bonus formula started at 1 hour, but the safety 
filter `MIN_TOKEN_AGE_MINUTES = 30` meant tokens aged 30-60 minutes would pass
safety checks but receive zero conviction bonus.

**Resolution:** Age bonus now starts at **0.5 hours (30 minutes)**.
Formula: `age_hours * 0.10` where `age_hours >= 0.5`.
Scoring function: `score_token_age()` linearly scales 0.0→1.0 from 0.5h→24h.

**Justification:** Eliminates the dead zone where safe tokens got no age bonus.
Aligns conviction scoring with safety filter boundaries.

---

## CB-G3: Helius Webhook Security

**Blueprint Issue:** Original webhook endpoint had no authentication mechanism.

**Resolution:** Implemented two-layer security:
1. **HMAC-SHA256 signature verification** using `HELIUS_WEBHOOK_SECRET` from config.
   Validated against `x-helius-signature` header.
2. **IP allowlist** for known Helius IP ranges:
   - `34.86.0.0/16`, `34.118.0.0/16`, `34.126.0.0/16`, `35.206.0.0/16`, `34.36.0.0/16`
3. **Development mode bypass** via `GWAS_DEV_MODE=1` env var (for local testing).

**Justification:** Webhook endpoints are public-facing and receive financial data.
HMAC + IP allowlist is the standard Helius-recommended approach.

---

## CB-G4: Safety Filter — Explicit Data Sources with Caching

**Blueprint Issue:** Safety filter had no explicit data source mapping and no caching,
risking API rate limits and stale data.

**Resolution:** All data sources documented and cached:
- `lp_usd` → `GMGN /token/{address}` → 5-minute cache
- `top10_holder_pct` → `GMGN /token/{address}/holders` → 15-minute cache
- `age_minutes` → `GMGN /token/{address}` → 5-minute cache
- Rug check data → `GMGN /token/{address}/rugcheck` → 1-hour cache

Uses `requests-cache` with SQLite backend for all caching.

**Justification:** GMGN rate limits are undocumented but assumed tight. Caching
prevents redundant API calls, and different TTLs balance freshness vs. cost.
5min for token info (LP, age changes fast for new tokens), 15min for holders
(slower-changing), 1h for rug checks (infrequent changes).

---

## CB-G5: EXIT_ALERT Type

**Blueprint Issue:** No mechanism existed for notifying when a previously-followed
wallet should be dropped (WR drops below 30, PnL negative) while positions are
still held from its alerts.

**Resolution:** Added `check_exit_conditions()` in wallet_scanner and
`format_exit_alert()`/`send_exit_alert()` in alert module. The scanner loop
checks each active wallet's current stats via GMGN API and sends an exit alert if:
- Wallet WR dropped below `min_wr` (default 30%) OR PnL negative
- AND there are unexecuted alerts from this wallet (positions still held)

**Justification:** Critical for risk management. Following a wallet's alerts is
a trust signal; when that signal degrades, users need to know to consider
manual exits on any positions still open from that wallet.

---

## HP-G1: Dead-Alert Threshold — 15 alerts + 2 weeks

**Blueprint Issue:** Original threshold was 5 alerts without execution, which
was too aggressive and would mark wallets dead prematurely.

**Resolution:** Changed to **15 unexecuted alerts over a 2-week minimum window**.
SQL: `alert_count >= 15 AND executed_count = 0` within 2-week window.

**Justification:** 5 alerts could happen in a single day for an active wallet that
simply hasn't had its trades executed yet. 15 over 2 weeks means the wallet has
been generating alerts for an extended period with zero follow-through from the
user — a genuine signal of irrelevance.

---

## HP-G2: SQLite Backup

**Blueprint Issue:** No backup mechanism specified.

**Resolution:** 
- `Database.backup()` uses SQLite's backup API for safe copying
- Daily cron via `gwas_scanner.sh` or separate backup job
- 30-day retention with automatic rotation
- Backups stored at `/opt/gwas/data/backups/gwas_YYYYMMDD.db`

**Justification:** Standard operational practice for databases. SQLite backup
API is safer than `shutil.copy` (handles in-progress writes).

---

## HP-G3: Database Indexes

**Blueprint Issue:** Schema defined but no indexes specified for query performance.

**Resolution:** Added eight indexes:
- `idx_alerts_token_ts` on (token_address, alert_timestamp)
- `idx_alerts_id` on (id)
- `idx_trades_tx` on (tx_hash)
- `idx_trades_wallet_ts` on (wallet_address, timestamp)
- `idx_trades_correlated` on (correlated_alert_id)
- `idx_alerts_wallet_ts` on (wallet_address, alert_timestamp)
- `idx_wallets_status` on (status)
- `idx_rate_log_ts` on (timestamp)

**Justification:** The correlation query (matching trades to alerts by wallet+token+time)
is the most frequent operation and needs composite indexes. Rate log queries for rate
limiting also benefit from timestamp indexes.

---

## HP-G4: Token Data Caching

**Blueprint Issue:** No caching layer defined for GMGN API calls.

**Resolution:** `requests-cache` with SQLite backend, multiple cache instances
with different TTLs as documented in CB-G4.

**Justification:** See CB-G4 justification. Additionally, using separate cache
databases for different endpoints prevents TTL conflicts.

---

## Architecture Decision: GMGN-Native (No Trade Execution)

**Decision:** GWAS is **read-only monitoring only**. All trade execution
happens through GMGN's interface manually. GWAS provides:
1. Wallet discovery from GMGN API
2. Conviction scoring for alert worthiness
3. Telegram alerts with GMGN/Photon deep links
4. Performance tracking via Helius webhooks

**Justification:** Clear separation of concerns. GMGN handles the execution
risk and complexity; GWAS focuses on the alpha-generation layer.

---

## Alert Format: Hybrid (Option C)

**Decision:** Compact 1-line preview + expandable full context.
Photon link only included for score ≥ 90 AND trade size > 3x wallet average.

**Justification:** Telegram UX — compact previews allow quick scanning,
expanded view provides full context for decision-making.

---

## Rate Limits (Revised v1.1)

| Limit | v1.0 | v1.1 |
|-------|------|------|
| Per cycle | — | 10 |
| Per hour | — | 8 |
| Per day | — | 40 |
| Burst cooldown | — | 30min (5 in 10min) |

**Justification:** Prevents alert fatigue and Telegram rate limiting.

---

## Correlation Window (Revised v1.1)

| Window | Duration |
|--------|----------|
| Default auto-correlation | 4 hours |
| Manual extend (✅ taken reply) | 24 hours |

**Justification:** 4h covers most immediate trade executions. 24h manual
extension handles cases where a user deliberates before acting on an alert.

---

*Document generated: 2026-06-09*
*GWAS v1.1.0*
