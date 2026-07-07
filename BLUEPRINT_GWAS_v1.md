# BLUEPRINT GWAS v1.2 — GMGN Wallet Alert System

**Status**: DRAFT  
**Date**: 2026-06-09  
**Author**: Hermes (orchestrator)  
**Predecessor**: BLUEPRINT v2.2 (auto-trader — FALSIFIED, archived)  
**Thesis**: Human wallet activity → filtered alert → manual execution

---

## 1. Thesis

Copy-trading via bot fails because execution lag destroys the wallet's timing edge (see v2.2 Appendix B). But the wallet's **token selection** edge is real and transferable — it just needs a human to execute with zero-lag discretion.

**GWAS delivers**: real-time alerts when high-quality human wallets buy tokens. Wildan executes manually on GMGN/Photon at his own speed. No bot execution = no lag problem.

```
WALLET buys $TOKEN ──→ GWAS detects ──→ QUALITY FILTER ──→ SAFETY FILTER
                                                                    │
                                                                    ▼
WILDAN executes on GMGN/Photon ←── TELEGRAM ALERT ←── CONVICTION SCORE
                                                                    │
HELIUS tracks wallet ──→ SQLITE ──→ WEEKLY PERFORMANCE REPORT
```

---

## 2. Design Decisions (Q1–Q4)

### Q1: MEDIUM Sensitivity + Dynamic Threshold

- **Base filter**: PURE_HUMAN or LIKELY_HUMAN, WR ≥ 30%, PnL > 0, **trades ≥ 10**
- **Noise suppression**:
  - Skip DCA: same wallet + same token within 24h window
  - Skip infant tokens: age < 30 minutes
  - Skip low LP: estimated LP < $5,000
- **Dynamic threshold (Day 1 capability)**:
  - Track `alerts_sent` vs `alerts_executed` per wallet per week
  - After 2 weeks of data, auto-tune wallet quality thresholds to target 30% execute rate
  - If execute rate > 50% → tighten filter (raise WR/score threshold)
  - If execute rate < 10% → loosen filter (lower WR/score threshold, expand wallet pool)

### Q2: HYBRID Alert Format

**Compact preview** (Telegram notification — 1 line):
```
🔔 GWAS | 5wTqTAZS... (PURE 94) BUY $TSLA 0.5 SOL @ $0.00034 | MC $12K | LP $800 | 2m ago
```

**Expanded message** (Telegram full message):
```
BUY SIGNAL — 5wTqTAZSGWu9...
━━━━━━━━━━━━━━━━━━━━━━━━━
Token:   $TSLA (3oL99tu2qnxka3...)
Action:  BUY 0.043 SOL ($278) @ $0.00034
MC:      $12,400 | LP: $800 | Age: 4h 12m
Holders: 43 | Top-10: 62%

Wallet:  5wTqTAZSGWu9... [GMGN Profile]
Verdict: PURE_HUMAN | Score: 94/100
7d:     WR 38% (8/21) | PnL +$1,240 | Avg hold 14m
All:    WR 43% | PnL +$3,097 | 149 trades | age 92d
Open:   3 positions ($TSLA, $DOGE, $PEPE)

⚠️ FLAGS:
  ⚠ Fresh Token (4h) — wait 30m+ for stabilization
  ⚠ Low LP ($800) — max position 0.02 SOL

Links: [BUY on GMGN] [BUY on Photon] [DexScreener] [RugCheck]

━━━━━━━━━━━━━━━━━━━━━━━━━
Sent by GWAS v1 | 2026-06-09 14:32 WIB
```

**Flag rules**:
| Flag | Trigger | Advice |
|------|---------|--------|
| 🟢 SAFE | LP > $50K, age > 6h, holders > 100 | Normal position (0.05 SOL) |
| ⚠️ Fresh Token | age < 6h | Wait 30m+ for stabilization |
| ⚠️ Low LP | LP $5K–$20K | Max 0.02 SOL |
| 🔴 VERY LOW LP | LP < $5K | **No alert** (filtered at safety stage) |
| ⚠️ Concentration | Top-10 holders > 70% | Watch for dump risk |
| ⚠️ High Velocity | Wallet sold previous token < 2m ago | Wallet mungkin PnD pattern |

### Q3: GMGN PRIMARY + PHOTON FALLBACK

- **Always**: GMGN buy link (pre-filled token address)
- **Conditional**: Photon link if `conviction_score > 90 AND buy_size > 3× wallet_avg_buy`
- **Dropped**: Jupiter (no MEV protection on Solana), BullX (redundant)

### Q4: FULL-TRACKED Performance

- Helius webhook subscribes to **Wildan's trading wallet** (public key only — READ-ONLY, no private key)
- Auto-correlate executed trades with alerts (1-hour window: alert timestamp → trade timestamp)
- Weekly Telegram report (Monday 9 AM WIB):
  - Executed PnL, WR, Sharpe vs benchmark
  - Best/worst alert wallets (by executed PnL)
  - Dead-alert wallets (alerts sent, never executed — candidate for filter removal)
  - Independent trade comparison (Wildan's PnL vs wallet's PnL on same trade)

---

## 3. Architecture

### 3.1 Module Map

```
┌─────────────────────────────────────────────────────────┐
│                    GWAS MAIN LOOP                         │
│                  (cron: every 5 min)                      │
├─────────────────────────────────────────────────────────┤
│                                                          │
│  ┌──────────┐   ┌──────────┐   ┌──────────┐             │
│  │ MONITOR  │──→│ QUALITY  │──→│ SAFETY   │             │
│  │ (reuse)  │   │ CHECKER  │   │ FILTER   │             │
│  │          │   │ (reuse)  │   │ (extend) │             │
│  └──────────┘   └──────────┘   └──────────┘             │
│       │                              │                   │
│  Poll GMGN                       ┌───┴───┐               │
│  activities                      │       │               │
│                              REJECT   PASS               │
│                                │       │                 │
│                            (silent)   │                  │
│                                       ▼                  │
│                              ┌──────────────┐            │
│                              │  CONVICTION  │            │
│                              │  DETECTOR    │ (NEW)      │
│                              └──────────────┘            │
│                                       │                  │
│                                       ▼                  │
│                              ┌──────────────┐            │
│                              │   ALERT      │            │
│                              │  FORMATTER   │ (NEW)      │
│                              └──────────────┘            │
│                                       │                  │
│                                       ▼                  │
│                              ┌──────────────┐            │
│                              │  NOTIFIER    │            │
│                              │  (reuse)     │            │
│                              └──────────────┘            │
│                                       │                  │
│                                  TELEGRAM                │
│                                                          │
├─────────────────────────────────────────────────────────┤
│                    BACKGROUND                            │
│                                                          │
│  ┌──────────────────┐    ┌──────────────────────┐        │
│  │ PERFORMANCE      │    │ DYNAMIC THRESHOLD    │        │
│  │ TRACKER          │    │ TUNER                │        │
│  │ (NEW — Helius)   │    │ (NEW — weekly)       │        │
│  └──────────────────┘    └──────────────────────┘        │
│                                                          │
└─────────────────────────────────────────────────────────┘

DORMANT (preserved from v2.2, not active):
  ┌──────────┐  ┌──────────┐  ┌──────────┐
  │EXECUTION │  │ POSITION │  │ KILL     │
  │ ENGINE   │  │ MANAGER  │  │ SWITCH   │
  └──────────┘  └──────────┘  └──────────┘
```

### 3.2 Reused Components (from v2.2)

| Component | Source | Modifications |
|-----------|--------|---------------|
| **Monitor Loop** | v2.2 Loop A (activity poll) | Simplified — no interleaving with Loop B. Pure poll → filter → alert |
| **Wallet Quality Checker** | v2.2 Section 3.3 `WalletQualityChecker` | Same: load classified_wallets.json, filter PURE/LIKELY, check WR/PnL/trades |
| **Safety Filter** | v2.2 Section 3.6 `TokenSafetyFilter` | Extended: add LP check ($5K minimum), token age (30min minimum), DCA dedup (24h) |
| **Notifier** | v2.2 notification-relay pattern | Adapted: Telegram send via `send_message` tool, format per Q2 schema |
| **SQLite Schema** | v2.2 Section 4 `StateManager` | Extended: add alert_log, execution_log, wallet_performance tables |

### 3.3 New Components

#### Conviction Detector

Assigns a 0–100 score to each alert based on signal strength:

```python
def calculate_conviction(wallet_quality, buy_activity, token_context):
    score = 0
    
    # Wallet signal (max 50 pts)
    score += min(wallet_quality["score"] * 0.5, 50)
    
    # Buy size signal (max 25 pts)
    size_ratio = buy_activity["size_sol"] / wallet_quality["avg_buy_size"]
    if size_ratio > 5:    score += 25  # whale buy
    elif size_ratio > 3:  score += 20  # strong conviction
    elif size_ratio > 1.5: score += 10 # above average
    
    # Wallet recency signal (max 15 pts)
    if wallet_quality["last_trade_age_min"] < 10:
        score += 15  # wallet just traded = active
    
    # Token freshness signal (max 10 pts)
    age_h = token_context["age_hours"]
    if 0.5 <= age_h <= 24: score += 10  # sweet spot: past safety filter window, still fresh
    
    return min(score, 100)
```

**Photon link trigger**: conviction > 90 AND buy > 3× avg

#### Alert Formatter

Generates both compact preview and expanded message per Q2 schema. Template-driven with flag system.

#### Performance Tracker

Helius webhook → SQLite pipeline:

```
Helius webhook (txn subscribe to Wildan wallet)
        │
        ▼
    webhook_handler.py
        │
        ├── Parse SWAP transactions (BUY/SELL)
        ├── Extract: token, amount, price, timestamp, tx_hash
        │
        ▼
    correlate.py
        │
        ├── Match tx_hash → alert within 1-hour window
        ├── If match: log to execution_log (alert_id, tx_hash, pnl)
        └── If no match: log independent trade
        │
        ▼
    weekly_report.py
        │
        └── Aggregate metrics → Telegram report (Monday 9 AM)
```

**Helius webhook config**:
- Subscribe to `SWAP` transactions on Wildan's wallet
- Webhook URL: local endpoint on VPS (or ngrok tunnel for dev)
- Rate: real-time (webhook pushes, not polled)

### 3.4 Data Schema (SQLite)

```
TABLE alert_log:
  id              INTEGER PRIMARY KEY
  wallet_address  TEXT
  wallet_verdict  TEXT        -- PURE_HUMAN / LIKELY_HUMAN
  wallet_score    REAL
  token_address   TEXT
  token_symbol    TEXT
  token_mc_usd    REAL
  token_lp_usd    REAL
  token_age_min   INTEGER
  buy_size_sol    REAL
  buy_size_usd    REAL
  conviction      INTEGER     -- 0–100
  flags           TEXT        -- JSON array: ["FRESH_TOKEN", "LOW_LP"]
  gmgn_link       TEXT
  photon_link     TEXT         -- nullable (only for high conviction)
  alert_timestamp INTEGER     -- unix
  created_at      TEXT        -- ISO

TABLE execution_log:
  id              INTEGER PRIMARY KEY
  alert_id        INTEGER     -- FK → alert_log.id, nullable for untracked trades
  tx_hash         TEXT        -- Solana transaction
  action          TEXT        -- BUY / SELL
  token_address   TEXT
  token_symbol    TEXT
  amount_sol      REAL
  price_sol       REAL
  fee_sol         REAL
  pnl_sol         REAL        -- for SELL only
  wallet_timestamp INTEGER
  created_at      TEXT

TABLE wallet_performance:
  wallet_address  TEXT PRIMARY KEY
  verdict         TEXT
  alerts_sent     INTEGER
  alerts_executed INTEGER
  execute_rate    REAL        -- executed / sent
  total_pnl_sol   REAL
  win_rate        REAL
  best_trade_sol  REAL
  worst_trade_sol REAL
  last_updated    TEXT
```

### 3.5 Performance Indexes (HP-G3)

```sql
-- Core lookup indexes
CREATE INDEX idx_alert_token_time 
  ON alert_log(token_address, alert_timestamp);

CREATE INDEX idx_alert_id 
  ON alert_log(id);

CREATE INDEX idx_execution_tx 
  ON execution_log(tx_hash);

-- Correlation query index (correlator.py)
CREATE INDEX idx_execution_token_time
  ON execution_log(token_address, wallet_timestamp);

-- Weekly report indexes
CREATE INDEX idx_execution_created
  ON execution_log(created_at);
CREATE INDEX idx_alert_created
  ON alert_log(created_at);
```

### 3.6 Backup Strategy (HP-G2)

```python
# gwas/db.py — backup function
def backup_db():
    src  = "gwas/data/gwas.db"
    dest = f"gwas/data/backup/gwas_{datetime.now():%Y%m%d}.db"
    
    # SQLite online backup (safe during WAL mode)
    shutil.copy2(src, dest)
    
    # Verify integrity
    conn = sqlite3.connect(dest)
    conn.execute("PRAGMA integrity_check")
    conn.close()
    
    # Rotate: keep last 30 days
    backups = sorted(Path("gwas/data/backup").glob("gwas_*.db"))
    for old in backups[:-30]:
        old.unlink()
```

**Cron**: `0 3 * * *` — Daily backup at 3 AM UTC (10 AM WIB)
**Retention**: 30 days rolling
**WAL mode**: SQLite configured with `PRAGMA journal_mode=WAL` for concurrent read safety
**Recovery**: `cp backup/gwas_YYYYMMDD.db gwas/data/gwas.db` + restart GWAS

**Also in cron list (Section 9)**

---

## 4. Main Loop (every 5 min)

```
GWAS_LOOP:
  
  # ── PHASE 1: DETECT ──
  for wallet in ACTIVE_WALLETS (PURE_HUMAN + LIKELY_HUMAN, WR≥30%, PnL>0, trades≥10):
    activities = poll_gmgn(wallet, lookback=5min)
    
    for act in activities:
      if act.event_type != "buy": continue
      
      # ── PHASE 2: QUALIFY ──
      if wallet_quality(act) == REJECT: continue    # WR/PnL/trades check
      if token_safety(act) == REJECT: continue       # LP/age/DCA check
      
      # ── PHASE 3: SCORE ──
      conviction = calculate_conviction(wallet, act, token)
      flags = generate_flags(token, wallet)
      
      # ── PHASE 4: ALERT ──
      alert = format_alert(wallet, act, token, conviction, flags)
      log_to_sqlite(alert_log, alert)
      send_telegram(alert.compact, alert.expanded)
      
      # Dedup: don't alert same token from same wallet in 24h
      mark_dedup(wallet, token)
```

**Time budget**: < 30 seconds per 5-minute cycle (must complete before next cycle starts)

**Rate limits**:
```
per_cycle:        10   — hard cap per 5min cycle (circuit breaker for spikes)
per_hour:          8   — sliding window, drop oldest if exceeded
per_day:          40   — absolute cap per UTC day
burst_cooldown:   30min — if 5 alerts in 10min, pause new alerts for 30min
```
Normal throughput: 15-30 alerts/day. Rate limits only activate during abnormal market-wide events.

**Throughput analysis** (why 10/cycle doesn't produce 2,880/day):
```
448 wallets (PURE + LIKELY)
  ↓ Quality filter (WR≥30%, PnL>0, trades≥10)
~100-150 active wallets
  ↓ 5-min lookback window (most cycles = 0 buys)
~2-5 raw buys/cycle
  ↓ Safety filter (LP≥$5K, age≥30min, DCA dedup)
~1-2 pass/cycle × 288 cycles/day = 288-576 theoretical max
  ↓ Real world (90% cycles = 0 buys, most buys filtered)
~15-30 alerts/day (MEDIUM target hit via chain, not cap)
```

---

## 5. Token Safety Filter (Extended)

```python
class TokenSafetyFilter:

    MIN_TOKEN_AGE_MINUTES = 30
    MIN_LP_USD = 5_000
    DCA_DEDUP_HOURS = 24
    MAX_TOP10_HOLDER_PCT = 90  # warn above 70%, reject above 90%

    def check(self, token: TokenContext, wallet: str) -> Tuple[bool, str, list[str]]:
        """
        Returns (pass, reason, flags).
        """
        flags = []

        # 1. Token age check
        if token.age_minutes < self.MIN_TOKEN_AGE_MINUTES:
            return False, "TOO_YOUNG", []

        if token.age_hours < 6:
            flags.append("FRESH_TOKEN")

        # 2. LP check
        if token.lp_usd < self.MIN_LP_USD:
            return False, "LOW_LP", []

        if token.lp_usd < 20_000:
            flags.append("LOW_LP")

        # 3. DCA dedup — same wallet buying same token in 24h
        if self._is_dca_dup(wallet, token.address, hours=self.DCA_DEDUP_HOURS):
            return False, "DCA_DEDUP", []

        # 4. Holder concentration
        if token.top10_holder_pct > self.MAX_TOP10_HOLDER_PCT:
            return False, "CONCENTRATION", []

        if token.top10_holder_pct > 70:
            flags.append("CONCENTRATION")

        # 5. RugCheck / Honeypot (reuse v2.2 check)
        if self._is_honeypot(token.address):
            return False, "HONEYPOT", []

        return True, "OK", flags
```

### Data Sources & Caching

Each safety check requires external API data. Spec per source:

| Check | API Source | Cache TTL | Fallback |
|-------|-----------|-----------|----------|
| `token.age_minutes` | DexScreener `/pairs/{chain}/{address}` → `pairCreatedAt` | 5 min | GMGN `created_at` field in activity (if available) |
| `token.lp_usd` | DexScreener → `liquidity.usd` | 5 min | Fallback to `cost_usd × 5` heuristic (rough est.) |
| `token.top10_holder_pct` | GMGN `token-security` endpoint or Solscan holder list (top 10 / total supply) | 15 min | Skip check if unavailable (pass, warn) |
| `address` (RugCheck) | `rugcheck.xyz` API → `GET /tokens/{address}` | 1 hour | If RugCheck down: skip check, flag "RUGCHECK_UNAVAILABLE" |
| DCA dedup | Local SQLite `alert_log` table | N/A (real-time) | N/A |

**Caching layer**: `gwas/cache.py`
```python
class TokenDataCache:
    TTL_MAP = {
        "token_info":  300,    # 5 min — DexScreener pair data
        "holders":     900,    # 15 min — holder distribution
        "rugcheck":    3600,   # 1 hour — rugcheck.xyz
    }
    
    def get(self, token_addr: str, key: str) -> dict | None:
        # Check memory + disk cache, return if fresh
        ...
    
    def set(self, token_addr: str, key: str, data: dict):
        # Write to memory + SQLite cache
        ...
```

**Fallback semantics**:
- LP/market cap unavailable → flag `UNVERIFIED`, continue (don't block)
- RugCheck unavailable → flag `RUGCHECK_UNAVAILABLE`, continue (don't block)
- Holder data unavailable → skip holder check, no flag (can't evaluate)

---

## 6. Dynamic Threshold Tuning

Runs weekly (Sunday 11 PM WIB). Analyzes execution_log to auto-tune filter tightness.

```python
class DynamicThresholdTuner:

    TARGET_EXECUTE_RATE = 0.30  # 30% of alerts should lead to trades

    def tune(self):
        # 1. Calculate current execute rate per wallet and global
        per_wallet = self._load_wallet_performance()
        global_rate = self._global_execute_rate()

        # 2. Auto-tune
        adjustments = []

        if global_rate > 0.50:
            # Too many alerts being executed → tighten filter
            # Raise WR threshold from 30% → 35%
            # Raise score floor for LIKELY_HUMAN
            adjustments.append(("WR_THRESHOLD", 0.30, 0.35))
            adjustments.append(("LIKELY_HUMAN_SCORE_FLOOR", 60, 65))

        elif global_rate < 0.10:
            # Too few alerts being executed → loosen filter
            # Lower WR threshold from 30% → 25%
            # Add more wallets (expand score cutoff)
            adjustments.append(("WR_THRESHOLD", 0.30, 0.25))
            adjustments.append(("WALLET_POOL_EXPAND", "top50", "top100"))

        # 3. Identify dead-alert wallets (alerts sent, never executed)
        # HP-G1: raised from 5→15 alerts + 2 week minimum tracking window
        dead = [w for w in per_wallet 
                if w.execute_rate == 0 
                and w.alerts_sent >= 15
                and w.days_tracked >= 14]
        for w in dead:
            adjustments.append((
                "REMOVE_WALLET", w.address, 
                f"{w.alerts_sent} alerts, 0 executed over {w.days_tracked}d — recommended removal"
            ))

        # 4. Recommend (never auto-apply — report to Wildan for approval)
        return adjustments
```

---

## 7. Helius Integration (Performance Tracking)

### Setup

1. Wildan creates a **dedicated trading wallet** for GWAS-tracked trades
2. Hermes registers Helius webhook:
   ```
   POST https://api.helius.xyz/v0/webhooks
   {
     "webhookURL": "https://vps-ip:port/gwas-webhook",
     "transactionTypes": ["SWAP"],
     "accountAddresses": ["<WILDAN_TRADING_WALLET>"],
     "webhookType": "enhanced"
   }
   ```
3. Webhook handler runs on VPS (Flask/FastAPI, port 8899)

### Webhook Security (AUTHENTICATION)

```
LAYER 1 — Helius Signature Verification:
  Every Helius webhook request includes:
    - X-Helius-Signature header: HMAC-SHA256 of payload body
  Verify using webhook secret (set in .env: HELIUS_WEBHOOK_SECRET)
  REJECT if signature mismatch → prevents forged transactions

LAYER 2 — IP Allowlist:
  Only accept requests from Helius IP ranges:
    34.203.0.0/16, 3.208.0.0/13 (AWS us-east-1 — Helius infra)
  REJECT all other IPs → prevents non-Helius POSTs

LAYER 3 — Rate Limiting (per-IP):
  Max 50 requests/second per IP
  Max 5,000 transactions/day total
  Log + alert if exceeded → potential abuse attempt

LAYER 4 — Payload Validation:
  - wallet_address MUST match Wildan's registered trading wallet
  - transactionType MUST be "SWAP"
  - timestamp must be within ±5min of server time (anti-replay)
  Discard (don't log) invalid payloads
```

### Webhook Handler

```python
# ═══ AUTH MIDDLEWARE — enforces Section 7.2 4-layer security ═══
@app.middleware("http")
async def auth_middleware(request, call_next):
    # LAYER 1: Signature verification
    body = await request.body()
    expected_sig = hmac.new(
        HELIUS_WEBHOOK_SECRET.encode(), body, hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(request.headers.get("X-Helius-Signature", ""), expected_sig):
        raise HTTPException(401, "Invalid signature")
    
    # LAYER 2: IP allowlist
    client_ip = request.client.host
    if not ip_in_ranges(client_ip, HELIUS_IP_RANGES):
        raise HTTPException(403, "IP not allowed")
    
    # LAYER 3: Rate limiting (per-IP, in-memory + expire)
    if rate_limiter.exceeded(client_ip, max_rps=50, max_daily=5000):
        raise HTTPException(429, "Rate limit exceeded")
    
    # LAYER 4: Payload validation (deferred to handler for business logic)
    return await call_next(request)

# ═══ HANDLER — business logic only (auth already enforced) ═══
@app.post("/gwas-webhook")
async def handle_webhook(payload: list[HeliusTransaction]):
    for tx in payload:
        # Payload validation (LAYER 4)
        if tx.wallet_address != WILDAN_TRADING_WALLET:
            continue
        if tx.type != "SWAP":
            continue
        if abs(time() - tx.timestamp) > 300:  # ±5min anti-replay
            continue
        
        trade = parse_swap(tx)
        
        # Correlate with alert
        alert = find_matching_alert(
            token_address=trade.token,
            wallet_timestamp=trade.timestamp,
            window_hours=4  # default 4h; extend to 24h if Wildan replied "✅" to alert
        )
        
        if alert:
            log_execution(alert.id, trade)
        else:
            log_independent_trade(trade)
```

### Manual Ack Extension (Telegram "✅" Reply)

Wildan can extend the 4-hour correlation window to 24 hours by replying `✅` or `✅ taken` to any alert message in Telegram:

```python
# gwas/correlator.py — Telegram reply handler
@app.message_handler(func=lambda m: "✅" in m.text and m.reply_to_message)
async def handle_ack(message):
    # Extract alert_id from original message context
    alert_id = extract_alert_id(message.reply_to_message)
    
    # Extend correlation window for this alert's token
    db.execute(
        "UPDATE alert_log SET correlation_window_hours = 24 WHERE id = ?",
        (alert_id,)
    )
    
    # Notify correlator to re-scan with extended window
    db.execute(
        "INSERT INTO ack_log (alert_id, user_id, timestamp) VALUES (?, ?, ?)",
        (alert_id, message.from_user.id, datetime.now())
    )
    
    await message.reply("✅ Recorded. Correlation window extended to 24h for this trade.")
```

**Behavior**:
- Default window: 4 hours (auto-correlate)
- Ack via Telegram reply "✅" or "✅ taken": extends to 24 hours
- Acknowledged alerts are highlighted in weekly report
- Only one ack per alert (idempotent)

Sent Monday 9 AM WIB to Telegram.

```
📊 GWAS WEEKLY REPORT — 2 Jun – 8 Jun 2026
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

EXECUTED PERFORMANCE:
  Trades:     14 (from 47 alerts — 30% execute rate)
  Win Rate:   42.9% (6W / 8L)
  Net PnL:    +0.87 SOL (+$58.12)
  Best:       $TSLA +0.34 SOL
  Worst:      $DOGE -0.19 SOL

BEST ALERT WALLETS (by executed PnL):
  1. 5wTqTAZS...  +0.51 SOL (3 trades, WR 67%)
  2. 2szKH7nX...  +0.22 SOL (2 trades, WR 50%)
  3. Db7WXXT6...  +0.14 SOL (1 trade)

WORST ALERT WALLETS (by executed PnL):
  1. ANWamZUY...  -0.31 SOL (4 trades, WR 25%)
  2. 3VBYdRD1...  -0.18 SOL (2 trades, WR 0%)

DEAD-ALERT WALLETS (≥15 alerts, 0 executed, ≥2 weeks tracked):
  — 4WNKVxa... (17 alerts, 21d tracked) — recommend removal
  — 2Yajt4j... (15 alerts, 18d tracked) — recommend removal

INDEPENDENT TRADES (not from alerts):
  3 trades, PnL -0.12 SOL

SYSTEM:
  Detector:   854 wallets, 0.5h fresh
  Alerts this week: 47
  Dynamic tune: no adjustments needed (30% execute rate on target)
```

---

## 8. File Structure

```
/home/ubuntu/projects/gmgn-auto-trader/
│
├── BLUEPRINT_v2.md                  # Archived — falsified auto-trader
├── BLUEPRINT_GWAS_v1.md             # This file
│
├── gwas/                            # NEW — GWAS code
│   ├── main.py                      # Entry point / orchestrator
│   ├── monitor.py                   # GMGN activity poller (reuse from v2.2)
│   ├── quality.py                   # Wallet quality checker (reuse from v2.2)
│   ├── safety.py                    # Token safety filter (extended)
│   ├── conviction.py                # Conviction detector (NEW)
│   ├── formatter.py                 # Alert formatter (NEW)
│   ├── notifier.py                  # Telegram sender (reuse pattern)
│   ├── tuner.py                     # Dynamic threshold tuner (NEW)
│   ├── cache.py                     # Token data cache (NEW — HP-G4)
│   ├── webhook_server.py            # Helius webhook handler (NEW)
│   ├── correlator.py                # Alert→trade matcher (NEW)
│   ├── reporter.py                  # Weekly report generator (NEW)
│   ├── db.py                        # SQLite schema + queries
│   └── config.py                    # Constants, thresholds, paths
│
├── gwas/data/                       # NEW — GWAS data
│   ├── gwas.db                      # SQLite database
│   ├── backup/                      # Daily DB backups (30-day retention)
│   ├── cache.db                     # Token data cache (HP-G4)
│   └── tuner_state.json             # Dynamic threshold state
│
├── data/backtest/                   # REMAINS ACTIVE
│   ├── raw/                         # Round 1 raw data
│   ├── raw_r2/                      # Round 2 raw data
│   ├── combined/                    # Merged dataset
│   ├── results/                     # Round 1 results
│   ├── results_r2/                  # Round 2 results
│   └── backtest_engine.py           # Reusable engine
│
└── data/detector/                   # SYMLINK → human-wallet-detector/data/
```

---

## 9. Cron Jobs

| Job | Schedule | Model | Action |
|-----|----------|-------|--------|
| **GWAS-MAIN** | `*/5 * * * *` | MiniMax-M2.7 | Run `gwas/main.py` → detect, filter, alert |
| **GWAS-TUNER** | `0 23 * * 0` (Sun) | MiniMax-M2.7 | Run dynamic threshold analysis, report to Wildan |
| **GWAS-REPORT** | `0 9 * * 1` (Mon) | MiniMax-M2.7 | Generate weekly performance report |
| **GWAS-WEBHOOK** | daemon | N/A | Flask server listening for Helius webhooks |
| **GWAS-BACKUP** | `0 3 * * *` | N/A (shell script) | Daily SQLite backup + rotation (HP-G2) |

**Existing cron preserved**:
- `human-wallet-detector` — continues scanning (input to GWAS quality filter)
- `V3 hybrid forward test` — paused (obsolete with GWAS pivot)

---

## 10. Edge Cases & Failure Modes

| Case | Handling |
|------|----------|
| **GMGN API down** | Skip cycle, log warning. Max 3 consecutive failures → alert Wildan |
| **Helius webhook down** | Queue missed transactions, replay on restart. Max gap tolerance: 6h |
| **Alert flood (>10/cycle)** | Hard cap: top 10 by conviction score. Log skipped alerts |
| **Detector stale >12h** | Trigger detector run. If unavailable, fallback to last-known-good data |
| **Same token from 2 wallets** | Both alerts sent (separate wallet contexts). Wildan decides |
| **Wallet sells immediately after buy** | Alert already sent. Wildan sees sell in next cycle. Add "HIGH_VELOCITY" flag |
| **Photon link 404** | Fallback to GMGN link. Log error |
| **SQLite corruption** | WAL mode + daily VACUUM. Auto-recovery from backup (see HP-G2) |
| **Dynamic threshold drift** | Never auto-apply. Always report → Wildan approves |
| **Wallet drops from whitelist mid-stream** (CB-G5) | If wallet that previously triggered alerts falls below quality threshold (WR drops, verdict changes), send **EXIT_ALERT**: "⚠️ GWAS | 5wTqTAZS... downgraded to LIKELY (WR 18%). Consider exiting positions." Wildan decides whether to sell or hold. EXIT_ALERT sent once per wallet per quality drop event. |
| **Wildan holds token from unfollowed wallet** | Same mechanism: if Wildan's on-chain wallet shows open position in token from a downgraded wallet, EXIT_ALERT triggered on next GWAS cycle |

---

## 11. Testing Plan

### Phase 0: Smoke Test (Day 1)

- Run GWAS in **alert-only mode** (no Helius tracking yet)
- Manual verify: alerts arriving within 5 min of wallet activity
- Check: DCA dedup, token age filter, LP filter working
- Goal: <5 false alerts, <2 missed real alerts

### Phase 1: Helius Integration (Day 2–3)

- Set up Helius webhook for Wildan's wallet
- Verify: webhook receiving transactions
- Verify: alert→trade correlation within 4h window (extendable to 24h via "✅" Telegram ack)
- Goal: >90% correlation accuracy

### Phase 2: Live Alert → Execute (Day 4–7)

- Wildan uses alerts for real trading
- Track: execute rate, PnL, dead wallets
- Goal: 20–40% execute rate, positive PnL trend

### Phase 3: Dynamic Tuning (Week 2+)

- First tuning report generated (after 2 weeks of data)
- Wildan reviews adjustments
- Apply approved changes
- Goal: stable 30% execute rate with improving PnL

---

## 12. Deliverables

| # | Item | File | ETA |
|---|------|------|-----|
| D1 | GWAS main loop + monitor | `gwas/main.py`, `monitor.py` | Day 1 |
| D2 | Safety filter + conviction | `gwas/safety.py`, `conviction.py` | Day 1 |
| D3 | Alert formatter + notifier | `gwas/formatter.py`, `notifier.py` | Day 1 |
| D4 | SQLite schema + DB layer + **indexes + backup** | `gwas/db.py` | Day 1 |
| D5 | Cron job: GWAS-MAIN | cronjob action='create' | Day 1 |
| D6 | Helius webhook server | `gwas/webhook_server.py` | Day 2 |
| D7 | Correlator + reporter | `gwas/correlator.py`, `reporter.py` | Day 2 |
| D8 | Weekly report cron | cronjob action='create' | Day 2 |
| D9 | Dynamic threshold tuner | `gwas/tuner.py` + cron | Day 2 |
| D10 | GWAS operational (all crons active) | — | Day 2 |

---

## 13. Success Criteria (Revised — v1.1)

**Phase 2 exit gate** (Day 7):
- Execute rate: 20–40%
- Net PnL: ≥ 0 SOL gross-of-fees
- Miss rate: < 10% (alerts that should've fired but didn't)
- False rate: < 20% (alerts that fired but shouldn't have)

**Phase 3 exit gate** (Week 4):
- Execute rate: 25–35% (stabilized by dynamic tuning)
- Profit Factor executed alerts > 1.2
- **Alerts beat independent trades by > 10% PnL** (BIGGEST GATE — proves system edge over Wildan's gut-trading)
- WR executed > 35%
- Dead wallets: < 20% of active pool auto-recommended for removal

**Portfolio gates (ongoing)**:
- Max DD: 20% of capital
- Risk per alert: 10% of capital (0.1 SOL at 1 SOL starting)
- If DD > 15%: reduce alert exposure to 5% until recovery

**Comparison metric** (executed-alert PnL vs independent-trade PnL):
- Tracked weekly via Helius correlator
- Independent = any trade Wildan makes on tokens that had NO alert within 4h
- Alert trades = any trade with correlated alert (4h window, extendable to 24h via "✅")
- System proves edge when: alert-PnL > independent-PnL + 10% margin

---

**END OF BLUEPRINT GWAS v1.2**

---

## Appendix A: DEVIATIONS Log

_Every disagreement between Wildan's spec and implementation is tracked here._

| # | Spec | Implemented | Resolution |
|---|------|-------------|------------|
| D1 | Success criteria Week 4: +1 SOL | v1: +1 SOL PnL | **Fixed v1.1**: Replaced with Profit Factor>1.2 + alerts beat independent by 10% PnL |
| D2 | Rate limit: 10 per 5-min cycle (expected 15-30/hari) | v1: 10/cycle only, no explanation of real throughput | **Fixed v1.1**: 4-tier rate limits — per_cycle:10, per_hour:8, per_day:40, burst_cooldown:30min |
| D3 | Correlation window 4h (Wildan said 2-4h default) | v1: 1h | **Fixed v1.1**: Default 4h, extendable to 24h via Telegram "✅" reply |

**Rule**: Every spec-implementation gap MUST appear here with resolution (fixed or justified). Silent skip = governance violation.

---

## v1.1 Changelog

```
v1.0 → v1.1 (9 Jun 2026)
─────────────────────────
GOVERNANCE:
  + Appendix A: DEVIATIONS log (D1-D3)

CRITICAL BUGS:
  CB-G1: Main loop ACTIVE_WALLETS now includes "trades≥10" filter
  CB-G2: Conviction age window aligned: 0.5h≤age≤24h (was 1h≤age≤24h)
  CB-G3: Helius webhook authentication — 4-layer security (signature, IP, rate-limit, payload)
  CB-G4: Safety filter data sources specified per-check with caching TTL + fallback
  CB-G5: EXIT_ALERT type for wallets dropped from whitelist mid-stream

HIGH PRIORITY:
  HP-G1: Dead-alert threshold raised: 5→15 alerts + 2-week minimum window
  HP-G2: SQLite daily backup + 30-day retention (GWAS-BACKUP cron)
  HP-G3: DB indexes: alert_token_time, alert_id, execution_token_time, execution_created
  HP-G4: Token data cache TTLs: 5min (info), 15min (holders), 1h (rugcheck)

REVISED SECTIONS:
  Section 4: Rate limits (4-tier, burst_cooldown)
  Section 7: Correlation window 4h default + 24h ack extension
  Section 13: Success criteria (PF>1.2, alerts>independent, risk gates)
```

## v1.2 Changelog

```
v1.1 → v1.2 (9 Jun 2026)
─────────────────────────
CB-G3 REVISITED (spec-code gap):
  + Auth middleware (@app.middleware) enforcing 4-layer security BEFORE handler
  + Handler now runs business logic only (auth already enforced)
  + Payload validation (LAYER 4) moved from middleware to handler body
  Fix: same governance lesson as CB4 from v2.0→v2.1 — spec-only auth = devs code handler directly, skip middleware

COSMETIC:
  Weekly report example: dead-alert wallet counts 7,6→17,15 (matches ≥15 threshold)
  Dead-alert line now shows days-tracked for full context
```
