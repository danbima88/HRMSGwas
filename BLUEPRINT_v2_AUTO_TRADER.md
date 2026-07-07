# BLUEPRINT v2.2 — GMGN Auto-Trader (Hybrid Copy-Trading)
> **Version:** 2.2 | **Date:** 9 June 2026 | **Status:** CONDITIONAL GO (CB1-CB5 resolved, v2.2 patches applied)
> **Predecessor:** v2.1 (reviewed by Wildan — 2 issues found)
> **Strategy:** Opsi C Hybrid (follow BUY + wallet quality filter)

## v2.1 → v2.2 CHANGELOG

| ID | Severity | Issue | Fix |
|----|----------|-------|-----|
| CB3-v2 | CRITICAL | Field path mismatch — `refresh()` read `w["wr"]`, `w["pnl"]` but detector nests under `w["summary"]`. All fields returned 0 → `is_followable()` always False → zero trades → backtest false-pass | Read from `w["summary"]["winrate"]`, `w["summary"]["pnl"]`, `w["summary"]["tags"]`. Trades = `buy_count + sell_count`. Sort by `w["score"]` (not `rank`). Handle both list and dict input formats |
| CB4-v2 | CRITICAL | Section 6 main loop still `for wallet in WALLETS` sequential — Section 1 time-budget design not applied. Coding from Section 6 would reproduce CB4 | Section 6 rewritten: `wallet_cursor`, `LOOP_A_MAX_DURATION=5s`, `WALLETS_PER_A_BATCH=5`, hard exit on budget exceeded. Single source of truth |

### v2.1 CHANGELOG (from v2.0)
| ID | Issue | Fix |
|----|-------|-----|
| CB1 | Winrate 30-65 scale vs GMGN 0.0-1.0 | `0.30 <= meta.wr <= 0.65` |
| CB2 | token_symbol vs token_address lookups | All state keyed by `token_address` |
| CB3 | top5 only from summary.json | ALL PURE_HUMAN+LIKELY_HUMAN, capped 30 |
| CB4 | Loop A blocks Loop B | Time-budget design in Section 1 |
| CB5 | Helper function stubs | Section 3.10: 8 function specs

---

## 0. STRATEGY THESIS & EVIDENCE

### 0.1 Trading Hypothesis

```
Wallet smart BUY token X (conviction) → Bot BUY token X (latency-tolerated)
Wallet smart SELL token X (exit)       → Bot SELL proportional (mirror exit)
```

**Why this has positive expectancy:**

1. **Entry edge:** Smart wallets buy early (low price). We copy entry — not exit. V3 data confirms: wallet `2szKH7nX...` WCB buy_cost $12, first SELL at $105 (+758%). If we'd bought AT his buy instead of his first sell, our PnL would be massively higher.

2. **Quality filter eliminates noise:** We only follow wallets that pass `human-wallet-detector` (PURE_HUMAN / LIKELY_HUMAN). This eliminates 92% of random wallets and all known bot types.

3. **Risk asymmetry:** Wallet quality filter + token safety filter + SL-50% + max 3 concurrent positions = downside bounded at ~0.25 SOL per worst-case scenario. Upside uncapped via compound entry sizing.

4. **Conviction amplification (Refinement 2):** Boost entry +50% when wallet buy >3× average size. This exploits insider/conviction signals while filtering scout/test buys.

5. **Panic exit protection (Refinement 3):** When wallet sells >50% in single TX → we exit 100%. Avoids "partial exit into rug" scenario.

### 0.2 Backtest Framework (Section 5 — see below)

Full spec in Section 5. Key pass criteria before Phase 1:
- Sharpe (daily) > 1.0
- Max DD < 25%
- Profit Factor > 1.5
- Win Rate > 35%
- Net PnL after fees+slippage > +15% in 60-day period
- BOT wallet control group must be break-even or negative

### 0.3 Capital Tier (Answers Q2)

**Tier: MIDDLE (0.5–10 SOL)**
- Initial deployment: 1 SOL
- Ramp-up maximum: 3 SOL (after 3 positive phases)
- Architecture: SQLite WAL mode, Telegram alerts mandatory, hot wallet isolation
- Private key storage: `.env` file (permission 600, disk encryption ON, hot wallet only)

### 0.4 Use Case Scope (Answers Q3)

**Personal use only.** No public service. No paid signals. No legal disclaimers required.
- Audit log: JSONL append-only (immutable for self-audit)
- No regulatory compliance burden

---

## 1. SYSTEM OVERVIEW

### Architecture Diagram

```
┌─────────────────┐    ┌────────────────────┐    ┌─────────────────────┐
│ Human Wallet    │    │ GMGN API           │    │ Helius Webhook      │
│ Detector        │    │ (Activity Poll)    │    │ (BUY events <5s)    │
│ (4x/day cron)   │    │ (60s loop)         │    │ (fallback if avail) │
└───────┬─────────┘    └────────┬───────────┘    └──────────┬──────────┘
        │                       │                           │
        │ wallet list           │ SELL events               │ BUY events
        ▼                       ▼                           ▼
┌───────────────────────────────────────────────────────────────────┐
│                       AUTO-TRADER DAEMON                           │
│                                                                    │
│  ┌──────────────────┐  ┌──────────────────┐  ┌──────────────────┐ │
│  │ Loop A (60s)      │  │ Loop B (5s)      │  │ Recon Loop       │ │
│  │ Wallet Monitor    │  │ Price Tracker    │  │ (every 5 min)    │ │
│  │ - Poll activity   │  │ - WebSocket SOL  │  │ - Query holdings │ │
│  │ - Detect BUY/SELL  │  │ - Track PnL      │  │ - Compare state  │ │
│  │ - Decide entries   │  │ - Enforce SL/TP  │  │ - Alert mismatch │ │
│  └────────┬─────────┘  └────────┬─────────┘  └────────┬─────────┘ │
│           │                     │                      │           │
│           ▼                     ▼                      ▼           │
│  ┌──────────────────────────────────────────────────────────────┐ │
│  │                    STATE (SQLite WAL)                         │ │
│  │  positions | trade_log | seen_tx | bot_state | tokens_traded │ │
│  └──────────────────────────────────────────────────────────────┘ │
│           │                                                       │
│           ▼                                                       │
│  ┌──────────────────┐  ┌──────────────────┐  ┌──────────────────┐ │
│  │ Risk Engine      │  │ Token Safety     │  │ Kill Switches    │ │
│  │ - Position sizing│  │ - Age/LQ/Holders │  │ - Consec losses  │ │
│  │ - DD-aware       │  │ - Honeypot check │  │ - DD threshold   │ │
│  │ - Max exposure   │  │ - Rugcheck.xyz   │  │ - API error rate │ │
│  └────────┬─────────┘  └────────┬─────────┘  └────────┬─────────┘ │
│           │                     │                      │           │
│           ▼                     ▼                      ▼           │
│  ┌──────────────────────────────────────────────────────────────┐ │
│  │                    EXECUTION ENGINE                            │ │
│  │  gmgn-cli swap --anti-mev --tip-fee (built-in Jito)           │ │
│  └──────────────────────────────────────────────────────────────┘ │
│           │                                                       │
│           ▼                                                       │
│  ┌──────────────────────────────────────────────────────────────┐ │
│  │                    NOTIFICATION LAYER                          │ │
│  │  Telegram Bot API → Chat ID 684426474                         │ │
│  │  Entry / Exit / SL / TP / Error / Kill Switch / Heartbeat     │ │
│  └──────────────────────────────────────────────────────────────┘ │
└───────────────────────────────────────────────────────────────────┘
```

### Loop Architecture (Addresses C1, CB4)

⚠️ **CB4 FIX:** With 30+ wallets (CB3), Loop A sequential processing blocks Loop B from price-checking SL/TP. Two solutions combined:

| Loop | Frequency | Purpose | Threading | Time Budget |
|------|-----------|---------|-----------|-------------|
| **Loop A** | Every 60s, max 5s active | Poll wallet activity, detect BUY/SELL, decide entries/exits | Main thread | **Hard cap: 5s per cycle.** Process wallets until time budget exhausted, queue remainder for next cycle |
| **Loop B** | Sub-5s, runs independently | Track position PnL, enforce SL/TP | `threading.Timer` or daemon thread | N/A — runs independently |
| **Recon** | 5 min | On-chain reconciliation | Main thread (during Loop A idle) | Full cycle, rare enough |

**Time-budget design (avoids full threading complexity):**

```python
import time

LOOP_A_MAX_DURATION = 5.0  # seconds — hard cap
WALLETS_PER_A_BATCH = 5     # process 5 wallets per A cycle
# With 30 wallets × 5/batch: all wallets checked every 6 cycles (6 min)
# If fewer wallets (<10): all checked every cycle

# Track which wallet index to resume from
wallet_cursor = 0

while running:
    now = time.time()
    
    # ══════ LOOP A: Time-budgeted wallet polling ══════
    if now - last_poll >= POLL_INTERVAL:
        loop_a_start = time.time()
        wallets_processed = 0
        
        for i in range(WALLETS_PER_A_BATCH):
            idx = (wallet_cursor + i) % len(WALLETS)
            wallet = WALLETS[idx]
            
            monitor = WalletMonitor(wallet, seen_tx)
            activities = monitor.poll()
            
            for act in activities:
                # ... existing decision logic ...
                pass
            
            wallets_processed += 1
            
            # HARD EXIT if over budget
            if time.time() - loop_a_start >= LOOP_A_MAX_DURATION:
                break
        
        wallet_cursor = (wallet_cursor + wallets_processed) % len(WALLETS)
        db.save_state()
        last_poll = now
    
    # ══════ LOOP B: Always runs (not gated by Loop A) ══════
    if now - last_price_update >= 5:
        # ... existing price tracking logic ...
        last_price_update = now
    
    time.sleep(0.1)  # prevent busy-wait
```

**Why time-budget over threads:**
- No shared-state race conditions (SQLite single-connection)
- No GIL contention for simple I/O-bound work
- Loop B always gets CPU within 0.1s (sleep interval), not blocked by Loop A
- 5s per 60s = 8% CPU budget for activity polling, 92% for price monitoring

**Wallet rotation:** All 30 wallets checked within 6 minutes (6 cycles × 5 wallets). Active wallets detected by recent activity within reasonable window.

**Loop B price feed fallback (Addresses C1, C2):**
- Primary: Birdeye WebSocket price feeds (free tier: 1 token tracked)
- Fallback: Jupiter Price API HTTP polling (GET `/api/v1/price?ids=<token_mints>`)
- Failure mode: if price feed stale > 30s → pause new entries + alert Telegram
- Caching: cache price 2s, reuse across position checks

**Loop B position PnL function (Addresses C2):**
```python
def get_position_pnl_pct(position: dict) -> tuple[float, datetime]:
    """
    Returns (pnl_pct, price_timestamp) for an open position.
    pnl_pct = (current_price / entry_price - 1) * 100
    Price source: Birdeye WS cache → Jupiter API fallback
    """
```

---

## 2. GMGN API ENDPOINTS (EMPIRICALLY VERIFIED)

### 2.1 Portfolio Activity (Addresses Q4)

```bash
gmgn-cli portfolio activity --chain sol --wallet <addr> --limit 200
```

**Empirically tested June 9, 2026:**

| Parameter | Finding |
|-----------|---------|
| **Items per page** | 20 (hard cap — limit param ignored beyond 20) |
| **Pagination** | Cursor-based (`--cursor <base64_value>`) |
| **Max history** | 30+ pages (600+ activities, ~120+ days for active wallet) |
| **Rate limit** | No 429 observed over 30 consecutive page fetches |
| **Oldest activity** | Feb 7, 2026 (121 days) for wallet `2szKH7nX...` |

**Response shape** (same as v1 — verified still accurate):
```json
{
  "activities": [{
    "event_type": "sell",           // ← NOT "type" (docs WRONG)
    "tx_hash": "3j3oJ...",
    "timestamp": 1780755260,
    "cost_usd": "0.021645",         // ← string (docs WRONG: number)
    "buy_cost_usd": "0.040455",     // ← null for buys
    "is_open_or_close": 1,          // 1=full close, 0=partial
    "wallet": "B8jy...",            // ← NOT "maker"
    "token": {"address": "ByQS...pump", "symbol": "GREEN"}
  }],
  "next": "NDI0Njg..."  // base64 cursor
}
```

**Required BUY/SELL classification:**

| Activity | event_type | buy_cost_usd | is_open_or_close | Action |
|----------|-----------|-------------|------------------|--------|
| BUY      | `"buy"`   | `null`       | `0`             | Detect entry |
| SELL (partial) | `"sell"` | non-null, >0 | `0`          | Mirror proportional |
| SELL (full) | `"sell"` | non-null, >0 | `1`           | Exit 100% |

### 2.2 Swap Execution (Addresses C4 — MEV VERIFIED!)

```bash
gmgn-cli swap \
  --chain sol \
  --from <wallet_addr> \
  --input-token <token_address> \
  --output-token So11111111111111111111111111111111111111112 \
  --amount <raw_amount> \
  --percent <pct> \
  --slippage <n> \
  --anti-mev \           # DEFAULT TRUE — built-in MEV protection!
  --priority-fee <sol> \ # >= 0.00001 SOL
  --tip-fee <amount>     # Jito tip, >= 0.00001 SOL
```

**MEV Protection Status: CONFIRMED ✓**
- `--anti-mev` flag exists, **default true**
- `--tip-fee` for Jito bundle tip
- `--priority-fee` for explicit priority
- **No need for direct Jito bundle submission.** GMGN handles it.
- See Section 11: DEVIATIONS for resolved C4.

**Additional swap flags discovered:**
- `--auto-slippage`: automatic slippage calculation
- `--condition-orders`: TP/SL orders embedded in swap command
- `--min-output`: minimum output amount for explicit price protection

### 2.3 Portfolio Stats (for wallet quality verification)

```bash
gmgn-cli portfolio stats --chain sol --wallet <addr> --period 30d
```

Key fields (verified): `buy` (not buy_count), `sell` (not sell_count), `realized_profit` (string), `pnl_stat.winrate` (0.0-1.0), `common.tags` (list of strings), `common.created_at` (unix timestamp).

---

## 3. CORE MODULES

### 3.1 Wallet Monitor — Loop A (Addresses C1, Refinement 1)

```python
# monitor.py — Poll wallet activity, detect BUY/SELL events

class WalletMonitor:
    def __init__(self, wallet_addr: str, seen_tx: set):
        self.wallet = wallet_addr
        self.seen = seen_tx
    
    def poll(self) -> list[Activity]:
        """
        Fetch recent activities (last 50), return new BUY + SELL events.
        Dedup by tx_hash.
        """
        activities = gmgn_fetch_activities(self.wallet, limit=50)
        new = []
        for act in activities:
            if act["tx_hash"] in self.seen:
                continue
            self.seen.add(act["tx_hash"])
            new.append(Activity.from_gmgn(act))
        return new

class Activity:
    event_type: Literal["buy", "sell"]
    tx_hash: str
    token_symbol: str
    token_address: str
    buy_cost_usd: Optional[float]
    cost_usd: float
    timestamp: int  # unix
    is_open_or_close: int  # 1=full close
    launchpad_platform: str
```

**BUY detection nuance (Refinement 2):**
```python
def should_follow_buy(buy: Activity, wallet_stats: WalletStats) -> tuple[bool, str]:
    """
    Filter BUY events — not all BUYs are worth following.
    
    Returns (should_follow, reason)
    """
    # Skip: multiple buys of same token in 5 min (DCA bot pattern)
    if count_recent_buys(buy.token_address, window_seconds=300) > 1:
        return False, "MULTI_BUY_DCA"
    
    # Skip: buy size < 10% of wallet's avg (scout/test buy)
    buy_size_sol = buy.cost_usd / SOL_PRICE
    avg_buy = wallet_stats.avg_buy_size_sol
    if buy_size_sol < avg_buy * 0.10:
        return False, "SCOUT_BUY"
    
    # Normal: 50%-200% of avg
    if avg_buy * 0.50 <= buy_size_sol <= avg_buy * 2.0:
        return True, "NORMAL"
    
    # Conviction: > 3x avg → boost entry
    if buy_size_sol > avg_buy * 3.0:
        return True, "CONVICTION"
    
    return True, "OK"
```

### 3.2 Price Tracker — Loop B (Addresses C1, C2)

```python
# price_tracker.py — Fast loop for position PnL + SL/TP enforcement

class PriceTracker:
    def __init__(self):
        self.cache: dict[str, tuple[float, datetime]] = {}  # token_addr -> (price_usd, ts)
        self.last_update: datetime = None
    
    def update_prices(self, token_addresses: list[str]):
        """
        Fetch current prices for all open positions.
        Primary: Jupiter Price API (single HTTP call for all tokens)
        Fallback: per-token Birdeye WS
        
        Jupiter API: GET /api/v1/price?ids=<comma_separated_token_mints>
        """
        ids = ",".join(token_addresses)
        resp = http_get(f"https://api.jup.ag/price/v2?ids={ids}")
        for addr, data in resp["data"].items():
            self.cache[addr] = (float(data["price"]), datetime.now())
        self.last_update = datetime.now()
    
    def get_position_pnl_pct(self, position: dict) -> tuple[float, datetime]:
        """
        Args:
            position: {token_address, entry_sol, entry_price_usd}
        Returns:
            (pnl_pct, price_timestamp)
            pnl_pct = (current / entry - 1) * 100
        """
        current_price, price_ts = self.cache.get(
            position["token_address"], (None, None)
        )
        if current_price is None:
            raise PriceStaleError(f"No price for {position['token_symbol']}")
        
        pnl_pct = (current_price / position["entry_price_usd"] - 1) * 100
        return pnl_pct, price_ts
    
    def is_stale(self) -> bool:
        """Price feed stale > 30s → pause new entries"""
        if self.last_update is None:
            return True
        return (datetime.now() - self.last_update).seconds > 30
```

### 3.3 Trade Detector (Addresses Refinement 1, Refinement 2, Refinement 4)

```python
# detector.py — Decide which BUY events to copy

class TradeDetector:
    def __init__(self, wallet_quality: WalletQualityChecker, safety: TokenSafetyFilter):
        self.quality = wallet_quality
        self.safety = safety
    
    def evaluate(self, activity: Activity, wallet_stats: WalletStats, 
                 state: TraderState) -> tuple[Decision, float]:
        """
        Returns (decision, entry_sol)
        
        Decision: COPY | SKIP | CONVICTION (1.5x entry)
        """
        # ── WALLET QUALITY FILTER (Refinement 1) ──
        if not self.quality.is_followable(wallet_stats):
            return Decision.SKIP, 0.0
        
        # ── BUY DETECTION FILTER (Refinement 2) ──
        follow, buy_reason = should_follow_buy(activity, wallet_stats)
        if not follow:
            return Decision.SKIP, 0.0
        
        # ── TOKEN SAFETY FILTER (Section 3.6) ──
        safe, reason = self.safety.check(activity.token_address)
        if not safe:
            return Decision.SKIP, 0.0
        
        # ── SKIP CONDITIONS (Refinement 4) ──
        # ⚠️ CRITICAL: ALL state lookups use token_address, NOT token_symbol.
        # token_symbol is ambiguous (same symbol can exist across chains/forked tokens).
        if wallet_stats.consecutive_losses > 3:
            return Decision.SKIP, 0.0
        
        if state.total_exposure_pct > 0.70:
            return Decision.SKIP, 0.0
        
        if state.kill_switch_triggered_today >= 2:
            return Decision.SKIP, 0.0
        
        if activity.token_address in state.tokens_in_cooldown():
            return Decision.SKIP, 0.0
        
        if sol_price_drop_1h() > 0.05:  # SOL -5% in 1h
            return Decision.SKIP, 0.0
        
        # ── SIZING ──
        base = calculate_entry_size(state)
        
        if buy_reason == "CONVICTION":
            return Decision.CONVICTION, base * 1.5
        
        return Decision.COPY, base
```

**Wallet Quality Checker (Refinement 1 — integrated with human-wallet-detector):**
```python
class WalletQualityChecker:
    def __init__(self, detector_data_path: str):
        """
        detector_data_path: path to human-wallet-detector/data/classified_wallets.json
        Loads ALL PURE_HUMAN + LIKELY_HUMAN wallets, NOT just top 5.
        """
        self.data_path = detector_data_path
        self.last_refresh = None
        self.whitelist: dict[str, WalletMeta] = {}
    
    def refresh(self):
        """
        Reload from detector output.
        Call every time detector cron finishes (monitor detector output age).
        
        ⚠️ CB3 FIX: Load ALL wallets with verdict PURE_HUMAN or LIKELY_HUMAN,
        not just top 5 from summary.json. Top 5 alone misses viable wallets.
        Bottom-capped at top 30 by score to prevent dilution.
        
        v2.2 FIX: Field path corrected — detector schema nests wr/pnl/tags
        under w["summary"], NOT at top level. Sorted by "score" (not "rank").
        """
        raw = json.load(open(self.data_path))
        
        # Handle both list (top-level) and dict with "wallets" key
        if isinstance(raw, list):
            all_wallets = raw
        elif isinstance(raw, dict):
            all_wallets = raw.get("wallets", raw.get("data", []))
        else:
            raise ValueError(f"Unexpected detector output format: {type(raw)}")
        
        # Filter to PURE_HUMAN + LIKELY_HUMAN, sorted by score descending
        candidates = []
        for w in all_wallets:
            verdict = w.get("verdict", "")
            if verdict not in ("PURE_HUMAN", "LIKELY_HUMAN"):
                continue
            
            summary = w.get("summary", {})
            tags = summary.get("tags", [])
            
            # Bot tag filter
            BOT_TAGS = {"sniper", "dex_bot", "rat_trader", "bundler", "fresh_wallet"}
            if set(tags) & BOT_TAGS:
                continue
            
            candidates.append({
                "wallet": w["wallet"],
                "verdict": verdict,
                "wr": summary.get("winrate", 0),       # 0.0-1.0
                "pnl": summary.get("pnl", 0),
                "trades": summary.get("buy_count", 0) + summary.get("sell_count", 0),
                "tags": tags,
                "score": w.get("score", 0),
            })
        
        candidates.sort(key=lambda w: w["score"], reverse=True)
        
        # Bottom-cap: max 30 wallets to keep Loop A responsive
        candidates = candidates[:30]
        
        self.whitelist = {
            c["wallet"]: WalletMeta(
                verdict=c["verdict"],
                wr=c["wr"],
                pnl=c["pnl"],
                trades=c["trades"],
                tags=c["tags"],
                rank=c["score"],
            )
            for c in candidates
        }
        self.last_refresh = datetime.now()
    
    def is_followable(self, wallet_stats: WalletStats) -> bool:
        """
        Must satisfy ALL (Refinement 1):
        - Wallet in detector whitelist (PURE_HUMAN or LIKELY_HUMAN)
        - Realized PnL > 0 in 30d
        - Minimum 5 tokens traded
        - Win rate 30-65% (0.30-0.65 scale)
        - No bot tags (sniper, dex_bot, rat_trader, bundler, fresh_wallet)
        - Not always trading pre-migration Pump.fun (check launchpad ratio)
        - Wallet age > 30 days
        """
        if wallet_stats.address not in self.whitelist:
            return False
        
        meta = self.whitelist[wallet_stats.address]
        
        BOT_TAGS = {"sniper", "dex_bot", "rat_trader", "bundler", "fresh_wallet"}
        if set(meta.tags) & BOT_TAGS:
            return False
        
        return (
            meta.pnl > 0
            and meta.trades >= 5
            and 0.30 <= meta.wr <= 0.65
            and wallet_stats.age_days > 30
        )
```

### 3.4 Position Manager (Addresses Refinement 3)

```python
# positions.py — Track open positions, handle exits

class PositionManager:
    def handle_wallet_sell(self, sell: Activity, state: TraderState) -> Optional[Order]:
        """
        Called when wallet SELLs token we hold.
        
        Returns Order(action="sell", percent=n, token=...) or None
        """
        if sell.token_address not in state.open_positions:
            return None  # we don't hold this token
        
        # ⚠️ open_positions keyed by token_address (NOT token_symbol).
        # token_symbol is for display/notification only. All lookups use token_address.
        # ── Refinement 3: Wallet sells >50% in single TX → exit 100% ──
        if sell.is_open_or_close == 1:
            # Full close
            return Order("sell", percent=100, token=sell.token_address,
                        reason="WALLET_FULL_EXIT")
        
        # Check if wallet sold >50% of its remaining position
        sell_pct = calculate_wallet_sell_percentage(sell, state)
        if sell_pct > 0.50:
            return Order("sell", percent=100, token=sell.token_address,
                        reason="WALLET_PANIC_SELL")
        
        # Proportional exit
        return Order("sell", percent=sell_pct, token=sell.token_address,
                    reason="WALLET_PARTIAL_EXIT")
```

**Position lifecycle state diagram:**
```
    [wallet BUY detected]
           │
           ▼
       OPENING ←──── (tx broadcast, waiting confirm)
           │
           ▼ (confirmed)
         OPEN ←──── (monitoring PnL via Loop B)
           │
           ├── [SL hit] ──────▶ CLOSING (SL)
           ├── [TP hit] ──────▶ CLOSING (TP)
           ├── [wallet exit] ──▶ CLOSING (EXIT)
           └── [kill switch] ─▶ CLOSING (KILL)
           │
           ▼ (confirmed)
         CLOSED
           
    If crash at OPENING → reconciliation resolves: check on-chain tx
    If crash at CLOSING → reconciliation: check if position still exists on-chain
```

### 3.5 Risk Engine (Addresses H1, H3)

```python
# risk.py — Sizing, exposure, drawdown scaling, kill switches

def calculate_entry_size(state: TraderState) -> float:
    """
    Drawdown-aware position sizing (Addresses H3).
    """
    base_entry = state.config["sizing"]["base_entry"]  # 0.05 SOL
    total_profit = state.total_realized_pnl_sol
    current_capital = state.capital_remaining
    initial_capital = state.config["capital"]["initial"]
    
    # Profit tier (existing)
    if total_profit > 0.50:
        tier_size = 0.10
    elif total_profit > 0.25:
        tier_size = 0.07
    else:
        tier_size = base_entry
    
    # Drawdown scaling (NEW — H3)
    dd_ratio = current_capital / initial_capital
    if dd_ratio < 0.5:
        tier_size *= 0.25
    elif dd_ratio < 0.7:
        tier_size *= 0.50
    
    return tier_size

def can_open_position(state: TraderState, entry_sol: float) -> bool:
    open_count = db.count_open_positions()
    total_exposure = db.total_open_exposure_sol()
    
    if open_count >= state.config["risk"]["max_positions"]:
        return False
    if total_exposure + entry_sol > state.config["risk"]["max_total_exposure"]:
        return False
    if state.price_tracker.is_stale():
        return False
    return True

# Kill Switch Triggers (Addresses H1 — full spec in Section 13)
KILL_SWITCHES = {
    "consecutive_losses": 5,       # pause 1h
    "daily_drawdown_pct": 15,      # pause until manual
    "api_error_rate_5min": 0.30,   # pause 30m
    "rpc_latency_p95": 2.0,        # pause until normal
    "actual_slippage_ratio": 2.0,  # blacklist token 24h
    "reconciliation_discrepancy": 2  # pause until manual
}
```

### 3.6 Token Safety Filter (Addresses H6)

```python
# safety.py — Pre-buy token checks

class TokenSafetyFilter:
    def __init__(self, config):
        self.MIN_AGE_HOURS = 1
        self.MIN_LIQUIDITY_USD = config.get("min_liquidity_usd", 20000)
        self.MAX_TOP10_HOLDERS_PCT = 0.60
    
    def check(self, token_address: str) -> tuple[bool, str]:
        """
        Pre-buy safety filter. All checks must pass.
        Returns (is_safe, reason).
        """
        # 1. Token age (via GMGN token info or Solana RPC)
        age_hours = get_token_age_hours(token_address)
        if age_hours < self.MIN_AGE_HOURS:
            return False, "TOO_NEW"
        
        # 2. Liquidity (via Jupiter quote API)
        liquidity = get_liquidity_usd(token_address)
        if liquidity < self.MIN_LIQUIDITY_USD:
            return False, f"LOW_LIQUIDITY ({liquidity:.0f} USD)"
        
        # 3. Holder concentration (via GMGN token holders)
        top10_pct = get_top_10_holders_pct(token_address)
        if top10_pct > self.MAX_TOP10_HOLDERS_PCT:
            return False, f"HOLDER_CONCENTRATION ({top10_pct:.0%})"
        
        # 4. Honeypot check (rugcheck.xyz API)
        if is_honeypot_rugcheck(token_address):
            return False, "HONEYPOT"
        
        # 5. Adaptive slippage (Addresses C3)
        slippage = get_adaptive_slippage(liquidity)
        
        return True, "OK"


def get_adaptive_slippage(liquidity_usd: float) -> int:
    """
    Adaptive slippage based on liquidity tier (Addresses C3).
    """
    if liquidity_usd > 500_000:
        return 3
    elif liquidity_usd > 100_000:
        return 5
    elif liquidity_usd > 20_000:
        return 10
    else:
        return None  # HARD REJECT — don't trade


# ── Liquidity data sources ──
def get_liquidity_usd(token_address: str) -> float:
    """Query liquidity via Jupiter or DexScreener API"""
    # Jupiter Quote API: GET /api/v1/quote?inputMint=X&outputMint=Y&amount=...
    # OR: DexScreener pairs API
    pass
```

### 3.7 Execution Engine (Addresses C4, H4)

```python
# executor.py — Trade execution via gmgn-cli swap

class ExecutionEngine:
    def __init__(self, mode: str, wallet_addr: str):
        """
        mode: 'dry' | 'live'
        wallet_addr: GMGN-bound wallet for swap
        """
        if mode not in ("dry", "live"):
            sys.exit("FATAL: EXECUTION_MODE must be 'dry' or 'live'")
        self.mode = mode
        self.wallet = wallet_addr
    
    def buy(self, token_address: str, amount_sol: float, 
            slippage_pct: int) -> OrderResult:
        """
        Execute buy with MEV protection (built-in via gmgn-cli --anti-mev).
        
        Adaptive slippage: determined by TokenSafetyFilter.get_adaptive_slippage()
        """
        if self.mode == "dry":
            return OrderResult(success=True, tx_hash="DRY_RUN", dry_run=True,
                             detail=f"WOULD BUY {amount_sol} SOL of {token_address}")
        
        cmd = [
            "gmgn-cli", "swap",
            "--chain", "sol",
            "--from", self.wallet,
            "--input-token", "So11111111111111111111111111111111111111112",  # SOL
            "--output-token", token_address,
            "--amount", str(int(amount_sol * 1e9)),  # SOL to lamports
            "--slippage", str(slippage_pct),
            "--anti-mev",                     # default true but explicit
            "--tip-fee", "0.00001",           # Jito tip 0.00001 SOL
        ]
        return self._run(cmd)
    
    def sell(self, token_address: str, percent: int, 
             slippage_pct: int) -> OrderResult:
        """Execute sell (percent 1-100)"""
        if self.mode == "dry":
            return OrderResult(success=True, tx_hash="DRY_RUN", dry_run=True,
                             detail=f"WOULD SELL {percent}% of {token_address}")
        
        cmd = [
            "gmgn-cli", "swap",
            "--chain", "sol",
            "--from", self.wallet,
            "--input-token", token_address,
            "--output-token", "So11111111111111111111111111111111111111112",
            "--percent", str(percent),
            "--slippage", str(slippage_pct),
            "--anti-mev",
            "--tip-fee", "0.00001",
        ]
        return self._run(cmd)
    
    def _run(self, cmd: list[str]) -> OrderResult:
        """Execute command, retry on transient failure"""
        for attempt in range(3):
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            if r.returncode == 0:
                data = json.loads(r.stdout)
                return OrderResult(
                    success=True,
                    tx_hash=data.get("tx_hash", ""),
                    detail=data
                )
            # Check for retryable error
            if "rate" in r.stderr.lower() or "timeout" in r.stderr.lower():
                time.sleep(2 ** attempt)
                continue
            return OrderResult(success=False, error=r.stderr[:200])
        
        return OrderResult(success=False, error="MAX_RETRIES")
```

**Execution latency note (Addresses H4):**
- `gmgn-cli swap` uses persistent connection / subprocess, 200-500ms overhead acceptable for entries
- The 60-120s poll interval for activity detection dominates latency, not subprocess overhead
- For BUY copying, wallet's entry timing provides the edge — bot entering 60-120s later still captures significant alpha (wallet holds hours/days)
- Direct HTTP to GMGN API considered but not required at this tier

### 3.8 Reconciliation Engine (Addresses C6)

```python
# reconciliation.py — Compare on-chain state with bot state

class ReconciliationEngine:
    def __init__(self, rpc_url: str, wallet_addr: str):
        self.rpc = rpc_url
        self.wallet = wallet_addr
    
    def reconcile(self) -> list[Discrepancy]:
        """
        Every 5 min:
        1. Query actual token holdings via Solana RPC getTokenAccountsByOwner
        2. Query bot's open positions from SQLite
        3. Compare → report discrepancies
        
        Returns list of Discrepancy objects
        """
        onchain = self._fetch_holdings()
        db_positions = db.get_open_positions()
        
        discrepancies = []
        
        # Bot says open, on-chain missing → POSITION MISSING
        for pos in db_positions:
            if pos.token_address not in onchain:
                discrepancies.append(Discrepancy(
                    type="MISSING", token=pos.token_symbol,
                    bot_value=pos.entry_sol, onchain_value=0,
                    action="MARK_CLOSED"
                ))
            elif abs(pos.amount_sol - onchain[pos.token_address].amount) > 0.05:
                discrepancies.append(Discrepancy(
                    type="MISMATCH", token=pos.token_symbol,
                    bot_value=pos.amount_sol, 
                    onchain_value=onchain[pos.token_address].amount,
                    action="ALERT"
                ))
        
        # On-chain has token, bot doesn't know → UNTRACKED
        for addr, holding in onchain.items():
            if addr not in [p.token_address for p in db_positions]:
                discrepancies.append(Discrepancy(
                    type="UNTRACKED", token=holding.symbol,
                    bot_value=0, onchain_value=holding.value_sol,
                    action="ALERT"
                ))
        
        return discrepancies
```

### 3.9 Fee Accounting (Addresses H2)

```python
# fees.py — Accurate PnL accounting net of all fees

class FeeCalculator:
    SOLANA_BASE_FEE_SOL = 0.000005  # ~$0.0003
    
    def __init__(self, priority_fee_sol: float = 0.00001,
                 jito_tip_sol: float = 0.00001,
                 gmgn_platform_fee_pct: float = 0.01):
        self.priority_fee = priority_fee_sol
        self.jito_tip = jito_tip_sol
        self.gmgn_fee = gmgn_platform_fee_pct
    
    def round_trip_fee_sol(self, entry_sol: float) -> float:
        """
        Total SOL cost for one buy + one sell.
        
        Components:
        - Solana base fee × 2 (buy + sell)
        - Priority fee × 2 (buy + sell)
        - Jito tip × 2 (buy + sell)
        - GMGN platform fee: 1% × entry_sol (buy) + 1% × exit_sol (sell)
          → ~2% × entry_sol for round trip (simplified)
        """
        network = (self.SOLANA_BASE_FEE_SOL + self.priority_fee + self.jito_tip) * 2
        platform = entry_sol * self.gmgn_fee * 2  # simplified: assume same entry/exit
        return network + platform
    
    def net_pnl(self, entry_sol: float, pnl_pct: float) -> float:
        """
        PnL net of ALL fees.
        pnl_pct: decimal (e.g. 0.50 = +50%)
        """
        gross_profit = entry_sol * pnl_pct
        fees = self.round_trip_fee_sol(entry_sol)
        return gross_profit - fees
```

---

### 3.10 Helper Function Specifications (Addresses CB5)

⚠️ **CB5 FIX:** Functions referenced throughout the blueprint must be spec'd before Phase 0 implementation. Each function includes data source, caching, and error handling.

```python
# ── Wallet Activity Helpers ──

def count_recent_buys(token_address: str, wallet_addr: str, 
                      window_seconds: int = 300) -> int:
    """
    Count how many BUY events this wallet has for this token
    in the last window_seconds. Uses seen_tx table in SQLite.
    
    Data source: SQLite seen_tx table (already populated by polling)
    Returns: 0-10 (realistic cap)
    Cache: None (single query, <1ms)
    """
    cutoff = int(time.time()) - window_seconds
    return db.count("""
        SELECT COUNT(*) FROM seen_tx 
        WHERE wallet_addr=? AND token_address=? 
        AND seen_at > ? AND event_type='buy'
    """, (wallet_addr, token_address, cutoff))


def get_wallet_avg_buy_size_sol(wallet_addr: str) -> float:
    """
    Average SOL size of this wallet's buys (last 30 trades).
    
    Data source: GMGN portfolio stats API
    Fallback: calculate from last 50 BUY events in seen_tx
    Returns: float SOL amount
    Error: returns 0.05 (default conservative) on failure
    """
    try:
        stats = fetch_wallet_stats(wallet_addr)
        return stats.get("avg_buy_sol", 0.05)
    except:
        # Fallback: compute from recent activity
        recent = db.query("""
            SELECT AVG(cost_usd / ?) as avg_sol 
            FROM trade_log 
            WHERE wallet_addr=? AND action='ENTRY' 
            ORDER BY timestamp DESC LIMIT 30
        """, (SOL_PRICE, wallet_addr))
        return float(recent.get("avg_sol", 0.05) or 0.05)


def get_wallet_consecutive_losses(wallet_addr: str) -> int:
    """
    Count consecutive losing trades (most recent first).
    Stops at first win.
    
    Data source: SQLite trade_log
    Returns: int (0-N)
    """
    rows = db.query("""
        SELECT realized_pnl_sol FROM trade_log
        WHERE wallet_addr=? AND action='EXIT'
        ORDER BY timestamp DESC LIMIT 20
    """, (wallet_addr,))
    
    count = 0
    for row in rows:
        if row["realized_pnl_sol"] < 0:
            count += 1
        else:
            break
    return count


# ── Market / Price Helpers ──

def sol_price_drop_1h() -> float:
    """
    Calculate SOL price change in last 1 hour.
    Positive = price dropped (e.g., 0.05 = 5% drop).
    
    Data source: Jupiter Price API (SOL/USDC)
    Cache: 60 seconds
    Returns: float (0.0-1.0)
    Error: returns 0.0 (assume no drop) on API failure
    """
    if _sol_price_cache.get("ts", 0) > time.time() - 60:
        return _sol_price_cache["value"]
    
    try:
        resp = http_get("https://api.jup.ag/price/v2?ids=So11111111111111111111111111111111111111112")
        current = float(resp["data"]["So11111111111111111111111111111111111111112"]["price"])
        # Historical: Binance Klines API
        resp2 = http_get("https://api.binance.com/api/v3/klines?symbol=SOLUSDT&interval=1h&limit=2")
        prev = float(resp2.json()[-2][4])  # close price 1h ago
        drop = (prev - current) / prev
        drop = max(0, drop)  # only positive = drops
        _sol_price_cache = {"value": drop, "ts": time.time()}
        return drop
    except:
        return 0.0


# ── Token Safety Helpers ──

def get_token_age_hours(token_address: str) -> float:
    """
    Token age in hours since creation.
    
    Data source: GMGN token info → common.created_at
    Fallback: Solana RPC getSignaturesForAddress → earliest tx
    Returns: float hours
    Error: returns 0 (triggers TOO_NEW rejection) on failure
    """
    try:
        info = fetch_token_info(token_address)
        created = info.get("common", {}).get("created_at", 0)
        if created:
            return (time.time() - created) / 3600
    except:
        pass
    
    # Fallback: Solana RPC first signature timestamp
    try:
        sigs = solana_rpc("getSignaturesForAddress", [token_address, {"limit": 1}])
        if sigs:
            return (time.time() - sigs[0]["blockTime"]) / 3600
    except:
        pass
    
    return 0  # Unknown → reject


def get_top_10_holders_pct(token_address: str) -> float:
    """
    Percentage of supply held by top 10 holders.
    
    Data source: GMGN token holders endpoint (sorted by balance desc)
    Alternative: Solscan API
    Returns: float (0.0-1.0)
    Error: returns 1.0 (triggers rejection) on failure
    """
    try:
        holders = fetch_token_holders(token_address, limit=10)
        total_supply = fetch_token_supply(token_address)
        top10_balance = sum(h["balance"] for h in holders)
        return top10_balance / total_supply
    except:
        return 1.0  # Unknown → reject


def is_honeypot_rugcheck(token_address: str) -> bool:
    """
    Check if token is a honeypot (can buy but cannot sell).
    
    Data source: rugcheck.xyz API
    GET https://api.rugcheck.xyz/v1/tokens/{token_address}/report
    
    Returns: True if honeypot/rug detected
    Error: returns True (conservative — skip if can't verify)
    """
    try:
        resp = http_get(f"https://api.rugcheck.xyz/v1/tokens/{token_address}/report")
        report = resp.json()
        
        # Check for honeypot indicators
        if report.get("risks"):
            for risk in report["risks"]:
                if risk.get("name") in ("Honeypot", "FreezeAuthority"):
                    return True
        
        # Check if top holder concentration is extreme
        if report.get("topHolders"):
            top10 = sum(h.get("pct", 0) for h in report["topHolders"][:10])
            if top10 > 0.90:
                return True
        
        return report.get("score", 0) < 0  # negative score = risky
    except:
        return True  # Can't verify → conservatively reject


# ── Position / Exit Helpers ──

def calculate_wallet_sell_percentage(sell: Activity, 
                                     state: TraderState) -> float:
    """
    Determine what % of wallet's position was sold in this SELL event.
    
    Use buy_cost_usd of this sell vs remaining buy_cost of previous sells.
    If wallet did 5 partial sells with decreasing buy_cost ($12→$10→$8→$6→$4),
    and this sell has buy_cost=$6:
      pct = 6 / (6+4) = 60% of remaining sold
    
    Data source: GMGN activity (buy_cost_usd per SELL)
    Returns: float (0.0-1.0)
    """
    # Get all recent sells of this token by this wallet
    recent_sells = db.query("""
        SELECT buy_cost_usd FROM trade_log
        WHERE wallet_addr=? AND token_address=? AND action='EXIT'
        ORDER BY timestamp DESC LIMIT 10
    """, (sell.wallet, sell.token_address))
    
    remaining_buy_cost = sell.buy_cost_usd
    for s in recent_sells:
        remaining_buy_cost += s["buy_cost_usd"]
    
    if remaining_buy_cost <= 0:
        return 1.0  # Full exit
    
    return sell.buy_cost_usd / remaining_buy_cost
```

### 3.11 State In-Memory Cache Design (Addresses CB2)

⚠️ To avoid token_symbol vs token_address confusion, all in-memory state dicts use **token_address as primary key**:

```python
# ✅ CORRECT — all lookups by token_address
state.open_positions: dict[str, PositionMeta]  # key = token_address
state.tokens_in_cooldown: dict[str, float]      # key = token_address, value = cooldown_until_ts

# token_symbol ONLY for display/notification — never for lookup
pos = state.open_positions.get(sell.token_address)  # ✅
if token_symbol in state.open_positions:             # ❌
```

### 4.1 Database Schema

```sql
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE positions (
    token_address TEXT PRIMARY KEY,
    token_symbol TEXT NOT NULL,
    entry_sol REAL NOT NULL,
    entry_price_usd REAL NOT NULL,
    entry_timestamp INTEGER NOT NULL,
    entry_tx_hash TEXT NOT NULL,
    wallet_buy_cost_usd REAL,
    wallet_addr TEXT NOT NULL,
    wallet_quality_score REAL,
    status TEXT CHECK(status IN ('opening','open','closing','closed','failed')),
    exit_tx_hash TEXT,
    exit_timestamp INTEGER,
    realized_pnl_sol REAL,
    exit_reason TEXT,  -- WALLET_FULL_EXIT | WALLET_PARTIAL | STOP_LOSS | TAKE_PROFIT | KILL
    created_at INTEGER DEFAULT (unixepoch()),
    updated_at INTEGER DEFAULT (unixepoch())
);

CREATE TABLE trade_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp INTEGER NOT NULL,
    action TEXT NOT NULL,  -- ENTRY | EXIT | SKIP | ERROR | KILL_SWITCH
    token_symbol TEXT,
    token_address TEXT,
    amount_sol REAL,
    entry_sol REAL,
    tx_hash TEXT,
    success INTEGER DEFAULT 0,
    reason TEXT,
    detail TEXT,  -- JSON blob for extra data
    created_at INTEGER DEFAULT (unixepoch())
);

CREATE TABLE seen_tx (
    tx_hash TEXT PRIMARY KEY,
    wallet_addr TEXT NOT NULL,
    seen_at INTEGER DEFAULT (unixepoch())
);

CREATE TABLE bot_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at INTEGER DEFAULT (unixepoch())
);

CREATE TABLE kill_switch_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trigger_type TEXT NOT NULL,
    reason TEXT,
    triggered_at INTEGER DEFAULT (unixepoch()),
    resolved_at INTEGER,
    resolved_by TEXT  -- 'auto' | 'manual'
);

CREATE TABLE reconciliation_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    discrepancy_type TEXT NOT NULL,
    token_address TEXT,
    bot_value_sol REAL,
    onchain_value_sol REAL,
    action_taken TEXT,
    reconciled_at INTEGER DEFAULT (unixepoch())
);

-- Indexes
CREATE INDEX idx_positions_status ON positions(status);
CREATE INDEX idx_trade_log_timestamp ON trade_log(timestamp);
CREATE INDEX idx_seen_tx_wallet ON seen_tx(wallet_addr);
```

### 4.2 Atomic Operations

```python
def open_position(token: str, entry_sol: float, entry_tx: str, wallet: str):
    """Atomic: insert position + log trade"""
    with db.connection() as conn:
        conn.execute("BEGIN")
        conn.execute("""
            INSERT INTO positions (token_address, token_symbol, entry_sol, 
                entry_tx_hash, wallet_addr, status)
            VALUES (?, ?, ?, ?, ?, 'opening')
        """, (token.address, token.symbol, entry_sol, entry_tx, wallet))
        conn.execute("""
            INSERT INTO trade_log (timestamp, action, token_symbol, 
                amount_sol, tx_hash, success)
            VALUES (?, 'ENTRY', ?, ?, ?, 1)
        """, (now_unix(), token.symbol, entry_sol, entry_tx))
        conn.execute("COMMIT")

def close_position(token: str, exit_tx: str, pnl_sol: float, reason: str):
    """Atomic: close position + log trade + update stats"""
    with db.connection() as conn:
        conn.execute("BEGIN")
        conn.execute("""
            UPDATE positions SET status='closed', exit_tx_hash=?,
                realized_pnl_sol=?, exit_reason=?, updated_at=?
            WHERE token_address=?
        """, (exit_tx, pnl_sol, reason, now_unix(), token))
        conn.execute("""
            INSERT INTO trade_log (timestamp, action, token_symbol,
                amount_sol, tx_hash, success, reason)
            VALUES (?, 'EXIT', (SELECT token_symbol FROM positions WHERE token_address=?),
                0, ?, 1, ?)
        """, (now_unix(), token, exit_tx, reason))
        conn.execute("""
            UPDATE bot_state SET value=CAST(CAST(value AS REAL) + ? AS TEXT), updated_at=?
            WHERE key='total_realized_pnl_sol'
        """, (pnl_sol, now_unix()))
        conn.execute("COMMIT")
```

### 4.3 Migration from JSON (for Forward Test V3 data)

```python
# migration_v3_to_sqlite.py — one-time import
def migrate_forward_test_data():
    """Import V3 state files into SQLite for backtest analysis"""
    for state_file in glob("forward_test_state_v3_*.json"):
        state = json.load(open(state_file))
        for trade in state.get("trades", []):
            db.execute("INSERT INTO trade_log ...", ...)
```

---

## 5. BACKTEST FRAMEWORK (Addresses H5, Q4)

### 5.1 Data Sourcing (Empirically Verified)

**Source:** GMGN portfolio activity via cursor pagination

| Metric | Empirically Verified (June 9, 2026) |
|--------|--------------------------------------|
| Items per page | 20 (hard cap) |
| Max pages observed | 30+ (600+ activities) |
| Historical depth | 120+ days for active wallets |
| Rate limit | No 429 on 30 consecutive fetches |
| Cursor mechanism | Base64 encoded, pass as `--cursor <value>` |

**Full ingestion script:**
```python
def fetch_full_history(wallet: str) -> list[Activity]:
    """Paginate through all available activity for backtesting."""
    all_activities = []
    cursor = None
    
    while True:
        if cursor:
            r = gmgn_activity(wallet, limit=200, cursor=cursor)
        else:
            r = gmgn_activity(wallet, limit=200)
        
        activities = r["activities"]
        if not activities:
            break
        
        all_activities.extend(activities)
        
        if not r.get("next"):
            break
        cursor = r["next"]
        
        time.sleep(0.3)  # rate limit safety
    
    return all_activities
```

### 5.2 Wallet Sample (10 wallets minimum — Q4 requirement)

| Category | Count | Purpose |
|----------|-------|---------|
| PURE_HUMAN | 3 | Strategy should PROFIT |
| LIKELY_HUMAN | 3 | Strategy should profit (lower magnitude) |
| AMBIGUOUS | 2 | Control negative — strategy should break even or loss |
| BOT | 2 | Control negative — MUST be loss or break even (proves edge from quality) |

**Wallet selection:** Top wallets by score from each verdict category in `classified_wallets.json`.

### 5.3 Replay Mechanics

```python
class BacktestEngine:
    def replay(self, activities: list[Activity], config: dict) -> BacktestResult:
        """
        Process activities chronologically.
        For each BUY: apply strategy logic (quality filter, safety, sizing)
        For each SELL: apply exit logic (proportional / panic / full)
        
        Realistic assumptions:
        - Latency: 30-120s from wallet action to bot action
        - Slippage: adaptive based on liquidity tier (3%-15%)
        - Fees: Solana base + priority + Jito tip + GMGN 1% × 2
        
        Returns: full trade log + equity curve + summary stats
        """
```

### 5.4 Output Format & Pass Criteria

**Output files:**
```
data/backtest/
├── <wallet>_equity_curve.csv      # timestamp, capital_sol, pnl_pct
├── <wallet>_trade_log.csv         # per-trade detail
├── aggregate_summary.json         # all wallets combined
└── control_group_comparison.csv   # HUMAN vs BOT performance
```

**Pass criteria (must ALL pass before Phase 1):**
```
Aggregate Sharpe (daily)       > 1.0
Max Drawdown                   < 25%
Profit Factor                  > 1.5
Win Rate                       > 35%
Net PnL after fees+slippage    > +15% (60-day period)
BOT wallet control group       ≤ 0% PnL (break-even or loss)
```

### 5.5 Backtest Timeline

- **Phase 0 duration:** 2-3 days
  - Day 1: Fetch 10 wallets × 60 days history
  - Day 2: Run backtest engine + analyze results
  - Day 3: Present results to Wildan for GO/NO-GO decision

---

## 6. MAIN LOOP (Addresses C8)

```python
#!/usr/bin/env python3
"""GMGN Auto-Trader v2 — Hybrid Copy-Trading Daemon"""

import os, sys, time, sqlite3, signal
from datetime import datetime, timezone, timedelta
from monitor import WalletMonitor
from price_tracker import PriceTracker
from detector import TradeDetector
from positions import PositionManager
from risk import RiskEngine, KillSwitchManager
from safety import TokenSafetyFilter
from executor import ExecutionEngine
from reconciliation import ReconciliationEngine
from fees import FeeCalculator
from notifier import TelegramNotifier
import database as db

# ── ENVIRONMENT CHECK (Addresses C8) ──
EXECUTION_MODE = os.getenv("EXECUTION_MODE")
if EXECUTION_MODE not in ("dry", "live"):
    sys.exit("FATAL: EXECUTION_MODE must be 'dry' or 'live'")
print(f"🟡 STARTING IN {EXECUTION_MODE.upper()} MODE")
if EXECUTION_MODE == "dry":
    print("   No real trades will be executed. All actions logged only.")

# ── LOAD CONFIG ──
config = load_config("data/config.yaml")
WALLETS = config["wallets"]  # From human-wallet-detector summary.json
POLL_INTERVAL = config.get("poll_interval_seconds", 60)
RECON_INTERVAL = config.get("recon_interval_seconds", 300)
HEARTBEAT_INTERVAL = config.get("heartbeat_interval_seconds", 3600)

# ── INITIALIZE MODULES ──
db.init("data/auto_trader.db")
seen_tx = db.load_seen_tx()
notifier = TelegramNotifier(config["telegram_chat_id"], config["telegram_bot_token"])
executor = ExecutionEngine(EXECUTION_MODE, config["wallet_address"])
price_tracker = PriceTracker()
safety = TokenSafetyFilter(config)
quality = WalletQualityChecker(config["detector_output_path"])
detector = TradeDetector(quality, safety)
position_mgr = PositionManager()
risk = RiskEngine(config)
kill_switch = KillSwitchManager(config["kill_switches"])
recon = ReconciliationEngine(config["rpc_url"], config["wallet_address"])
fees = FeeCalculator()

# ── STARTUP ──
quality.refresh()  # Load latest detector data
notifier.send_heartbeat(positions_count=db.count_open_positions(), 
                        capital=db.get_capital())

# ── GRACEFUL SHUTDOWN ──
running = True
def shutdown(sig, frame):
    global running
    running = False
    notifier.send_error("SYSTEM", "Shutting down gracefully...")
signal.signal(signal.SIGINT, shutdown)
signal.signal(signal.SIGTERM, shutdown)

# ── MAIN LOOPS ──
# ⚠️ v2.2 FIX: Time-budget design (matches Section 1 spec).
# Single source of truth. Prevents Loop A from blocking Loop B.
last_poll = 0
last_recon = 0
last_heartbeat = 0
last_price_update = 0
wallet_cursor = 0
LOOP_A_MAX_DURATION = 5.0
WALLETS_PER_A_BATCH = 5

while running:
    now = time.time()
    
    # ══════ LOOP A: Time-budgeted wallet polling ══════
    if now - last_poll >= POLL_INTERVAL:
        loop_a_start = time.time()
        wallets_processed = 0
        
        for i in range(min(WALLETS_PER_A_BATCH, len(WALLETS))):
            idx = (wallet_cursor + i) % len(WALLETS)
            wallet = WALLETS[idx]
            
            monitor = WalletMonitor(wallet, seen_tx)
            activities = monitor.poll()
            
            for act in activities:
                wallet_stats = quality.get_stats(wallet)
                
                if act.event_type == "buy":
                    decision, entry_sol = detector.evaluate(act, wallet_stats, db.get_state())
                    
                    if decision == Decision.SKIP:
                        db.log_skip(act, reason)
                        continue
                    
                    if decision == Decision.CONVICTION:
                        entry_sol *= 1.5
                    
                    if not risk.can_open_position(db.get_state(), entry_sol):
                        db.log_skip(act, "RISK_LIMIT")
                        continue
                    
                    slippage = safety.get_adaptive_slippage(act.token_address)
                    result = executor.buy(act.token_address, entry_sol, slippage)
                    
                    if result.success:
                        db.open_position(act, entry_sol, result.tx_hash, wallet)
                        notifier.notify_entry(act.token_symbol, entry_sol, 
                                             wallet_stats.wr)
                    else:
                        notifier.notify_error("BUY", f"{act.token_symbol}: {result.error}")
                
                elif act.event_type == "sell":
                    order = position_mgr.handle_wallet_sell(act, db.get_state())
                    if not order:
                        continue
                    
                    slippage = safety.get_adaptive_slippage(order.token_address)
                    result = executor.sell(order.token_address, order.percent, slippage)
                    
                    if result.success:
                        pnl_sol = db.close_position(act, order, result.tx_hash)
                        notifier.notify_exit(act.token_symbol, pnl_sol, order.reason)
                    else:
                        notifier.notify_error("SELL", f"{act.token_symbol}: {result.error}")
            
            wallets_processed += 1
            
            # HARD EXIT if over budget — remaining wallets next cycle
            if time.time() - loop_a_start >= LOOP_A_MAX_DURATION:
                break
        
        wallet_cursor = (wallet_cursor + wallets_processed) % len(WALLETS)
        db.save_state()
        last_poll = now
    
    # ══════ LOOP B: Price Tracking (sub-5s) ══════
    if now - last_price_update >= 5:
        open_positions = db.get_open_positions()
        if open_positions:
            token_addrs = [p.token_address for p in open_positions]
            price_tracker.update_prices(token_addrs)
            
            for pos in open_positions:
                pnl_pct, price_ts = price_tracker.get_position_pnl_pct(pos)
                
                # SL check
                if pnl_pct <= config["risk"]["stop_loss_pct"]:
                    result = executor.sell(pos.token_address, 100, 15)
                    if result.success:
                        db.close_position(pos, result.tx_hash, "STOP_LOSS")
                        notifier.notify_stop_loss(pos.token_symbol, round(pnl_pct, 1))
                
                # TP check
                if pnl_pct >= config["risk"]["take_profit_pct"]:
                    result = executor.sell(pos.token_address, 100, 10)
                    if result.success:
                        db.close_position(pos, result.tx_hash, "TAKE_PROFIT")
                        notifier.notify_take_profit(pos.token_symbol, round(pnl_pct, 1))
            
            # Price feed health
            if price_tracker.is_stale():
                notifier.notify_error("PRICE_FEED", "Price data stale >30s, pausing entries")
                risk.pause_new_entries()
        
        last_price_update = now
    
    # ══════ RECONCILIATION: 5 min ══════
    if now - last_recon >= RECON_INTERVAL:
        discrepancies = recon.reconcile()
        for d in discrepancies:
            db.log_reconciliation(d)
            notifier.notify_reconciliation(d)
        
        # Refresh wallet quality from detector
        quality.refresh()
        
        # Kill switch check
        triggers = kill_switch.evaluate(db.get_state())
        for t in triggers:
            db.log_kill_switch(t)
            notifier.notify_kill_switch(t)
            if t.requires_pause:
                risk.pause_all_trading(t)
        
        last_recon = now
    
    # ══════ HEARTBEAT: 1 hour ══════
    if now - last_heartbeat >= HEARTBEAT_INTERVAL:
        notifier.send_heartbeat(
            positions_count=db.count_open_positions(),
            capital=db.get_capital(),
            pnl=db.get_total_pnl(),
            mode=EXECUTION_MODE
        )
        last_heartbeat = now
    
    # ══════ WATCHDOG HEARTBEAT (Addresses M1) ══════
    with open("/var/run/gmgn-trader-heartbeat", "w") as f:
        f.write(str(int(now)))
    
    time.sleep(0.5)

# ── SHUTDOWN ──
db.close()
notifier.send_error("SYSTEM", "Daemon stopped")
print("Shutdown complete.")
```

---

## 7. NOTIFICATIONS (Addresses C7)

```python
# notifier.py — Telegram notifications via Bot API

class TelegramNotifier:
    def __init__(self, chat_id: int, bot_token: str):
        self.chat_id = chat_id  # 684426474 (Wildan)
        self.token = bot_token  # from env
        self.session = HTTPClient()
    
    def _send(self, text: str, retries: int = 3) -> bool:
        """HTTP POST to Telegram Bot API with retry"""
        for attempt in range(retries):
            try:
                resp = self.session.post(
                    f"https://api.telegram.org/bot{self.token}/sendMessage",
                    json={"chat_id": self.chat_id, "text": text, 
                          "parse_mode": "MarkdownV2"}
                )
                if resp.status_code == 200:
                    return True
            except Exception as e:
                if attempt < retries - 1:
                    time.sleep(2 ** attempt)
        return False
    
    def notify_entry(self, token: str, amount_sol: float, wallet_wr: float):
        """🟢 NEW POSITION: token X | 0.05 SOL | Wallet WR: 34%"""
        text = f"🟢 *ENTRY* {token} \\| {amount_sol} SOL \\| Wallet WR: {wallet_wr:.0f}%"
        self._send(text)
    
    def notify_exit(self, token: str, pnl_sol: float, reason: str):
        """📤 EXIT: token X | +0.023 SOL (+46%) | wallet full exit"""
        emoji = "🟢" if pnl_sol > 0 else "🔴"
        pnl_pct = (pnl_sol / 0.05) * 100  # approximate
        text = f"📤 *EXIT* {token} \\| {emoji} {pnl_sol:+.3f} SOL \\({pnl_pct:+.0f}%\\) \\| {reason}"
        self._send(text)
    
    def notify_stop_loss(self, token: str, pnl_pct: float):
        """🛑 STOP LOSS: token X | -50.0%"""
        text = f"🛑 *STOP LOSS* {token} \\| {pnl_pct:+.1f}%"
        self._send(text)
    
    def notify_take_profit(self, token: str, pnl_pct: float):
        """🎯 TAKE PROFIT: token X | +200%"""
        text = f"🎯 *TAKE PROFIT* {token} \\| +{pnl_pct:.0f}%"
        self._send(text)
    
    def notify_error(self, component: str, error: str):
        """❌ ERROR [BUY]: token X — rate limited"""
        text = f"❌ *ERROR* \\[{component}\\] {error}"
        self._send(text)
    
    def notify_kill_switch(self, trigger: KillTrigger):
        """⛔ KILL SWITCH: consecutive_losses=5 — paused 1h"""
        text = f"⛔ *KILL SWITCH* {trigger.type}\\={trigger.value} — {trigger.action}"
        self._send(text)
    
    def notify_reconciliation(self, d: Discrepancy):
        """⚠️ RECON: POSITION MISSING — token X"""
        text = f"⚠️ *RECON* {d.type} — {d.detail}"
        self._send(text)
    
    def send_heartbeat(self, positions_count: int, capital: float, 
                       pnl: float = None, mode: str = "live"):
        """💓 HEARTBEAT: 3 open | 1.250 SOL (+25.0%) | live mode"""
        pct = ((capital - 1.0) / 1.0 * 100) if capital else 0
        pnl_str = f" \\({pct:+.1f}%\\) " if pct != 0 else ""
        mode_indicator = "🔴 LIVE" if mode == "live" else "🟡 DRY"
        text = f"💓 *HEARTBEAT* {positions_count} open \\| {capital:.3f} SOL{pnl_str}\\| {mode_indicator}"
        self._send(text)
```

**Silence rules (same as Binance bot):**
- ✅ Entry, Exit, Stop Loss, Take Profit, Error, Kill Switch, Heartbeat, Reconciliation issue
- ❌ No notification for: poll cycle, skip decisions, balance check, routine operation

---

## 8. SECURITY & KEY MANAGEMENT (Addresses H7)

### 8.1 Private Key Storage

**Tier: MIDDLE (0.5-10 SOL) — `.env` file acceptable with conditions:**

```bash
# ~/.config/gmgn/.env
GMGN_API_KEY=<key>
GMGN_PRIVATE_KEY=<pem_content>
TELEGRAM_BOT_TOKEN=<token>
TELEGRAM_CHAT_ID=684426474
```

**Security requirements:**
```bash
chmod 600 ~/.config/gmgn/.env
# VM disk encryption must be ON
# Check: sudo dmsetup status | grep crypt
```

### 8.2 Hot Wallet Isolation (MANDATORY)

```
Main wallet (Wildan):     NEVER touch bot. Different private key entirely.
Hot wallet (bot):         Max 1.5 SOL ever deposited. Used only for bot trading.
```

**Funding flow:**
```
Main wallet → transfer to hot wallet → bot trades
Hot wallet profit → transfer back to main wallet weekly
```

**Recovery procedure if VM compromised:**
1. Revoke GMGN API key from gmgn.ai dashboard
2. Transfer all SOL from hot wallet to main wallet (via Phantom or CLI)
3. Rotate API key + private key
4. Re-provision VM with fresh disk
5. Restore from backup: `data/auto_trader.db` + config files

### 8.3 Permissions

```bash
# Bot runs as dedicated user
sudo useradd -m -s /bin/bash gmgn-trader
sudo chown -R gmgn-trader:gmgn-trader /home/ubuntu/projects/gmgn-auto-trader
chmod 700 /home/ubuntu/projects/gmgn-auto-trader/data
```

---

## 9. DEPLOYMENT (Addresses M1, M9)

### 9.1 Systemd Service

```ini
# /etc/systemd/system/gmgn-auto-trader.service
[Unit]
Description=GMGN Auto-Trader v2
After=network.target

[Service]
Type=simple
User=gmgn-trader
WorkingDirectory=/home/ubuntu/projects/gmgn-auto-trader
EnvironmentFile=/home/ubuntu/.config/gmgn/.env
Environment=EXECUTION_MODE=dry
ExecStart=/usr/bin/python3 auto_trader.py
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

### 9.2 Watchdog (Addresses M1)

Heartbeat file approach (replaces `pgrep`):
```bash
# /etc/cron.d/gmgn-watchdog
*/2 * * * * root find /var/run/gmgn-trader-heartbeat -mmin +5 | grep -q . && systemctl restart gmgn-auto-trader
```

Bot writes heartbeat every main loop iteration. Watchdog checks: if heartbeat file older than 5 minutes → daemon hung → restart.

### 9.3 Structured Logging (Addresses M9)

```python
import structlog

logger = structlog.get_logger()
logger.info("trade_executed", action="ENTRY", token="WCB", 
            amount_sol=0.05, tx_hash="abc123...")
```

Log output: JSON to `logs/auto_trader.jsonl` + systemd journal.
Every log entry: `timestamp`, `level`, `component`, `event`, `data`.

### 9.4 Environment Switching

```bash
# Dry mode
sudo sed -i 's/EXECUTION_MODE=.*/EXECUTION_MODE=dry/' /etc/systemd/system/gmgn-auto-trader.service
sudo systemctl daemon-reload
sudo systemctl restart gmgn-auto-trader

# Live mode
sudo sed -i 's/EXECUTION_MODE=.*/EXECUTION_MODE=live/' /etc/systemd/system/gmgn-auto-trader.service
sudo systemctl daemon-reload
sudo systemctl restart gmgn-auto-trader
```

Startup banner in stdout + Telegram: `🟡 STARTING IN DRY MODE` or `🔴 STARTING IN LIVE MODE`.

---

## 10. EDGE CASES & PITFALLS (Expanded from v1)

| # | Scenario | Handling |
|---|----------|----------|
| E1 | `buy_cost_usd = null` (BUY) | Normal — only process for entries, skip PnL calc |
| E2 | `buy_cost_usd = 0` or `cost_usd = 0` | Skip trade + log warning |
| E3 | Token same symbol, different address | Always use `token_address` as primary key, not symbol |
| E4 | Rate limit 429 | Exponential backoff 5s→10s→30s, log if >3 retries |
| E5 | Wallet sells token we never bought | PositionManager skips (no open position) |
| E6 | Position -50% SL hit | Force sell, regardless of wallet status |
| E7 | Price feed stale >30s | Pause new entries (set `risk.paused=True`), alert Telegram |
| E8 | Bot crash at `OPENING` state | Reconciliation resolves: check on-chain tx status |
| E9 | Bot crash at `CLOSING` state | Reconciliation: check if position still exists on-chain |
| E10 | Token migration Pump.fun→Raydium | LP address changes. Use token mint address (stable) |
| E11 | 3 positions open, new BUY signal | Skip + log "MAX_POSITIONS" |
| E12 | SQLite corrupt on crash | WAL mode auto-recovers. If hopeless: restore from backup |
| E13 | JSON parse error on GMGN response | Retry 2x, log raw output to disk, skip if persistent |
| E14 | Wallet sells >50% in single TX | Exit 100% (Refinement 3 — panic sell protection) |
| E15 | SOL price drop >5% in 1h | Skip new entries (Refinement 4 — volatility regime) |
| E16 | Actual slippage >2x expected | Blacklist token for 24h, alert (H1 — per-token circuit breaker) |
| E17 | GMGN swap timeout (30s) | Retry 2x with exponential backoff, then force sell manual |
| E18 | Multiple wallets in config | Primary wallet (LIVE) + observer wallets (log only, no execute) |

---

## 11. JSON FIELD NAME CHEATSHEET (Unchanged from v1)

| Purpose | Docs (WRONG) | REAL (USE THIS) |
|---------|-------------|-----------------|
| Event type | `type` | **`event_type`** |
| Wallet address | `maker` | **`wallet`** |
| Buy count | `buy_count` | **`buy`** |
| Sell count | `sell_count` | **`sell`** |
| Monetary values | number | **string** (`float()` wrap) |
| BUY's buy_cost | `0` | **`null`** (check with `or 0`) |

---

## 12. TESTING PLAN (Addresses v1 Feedback)

### Phase 0: Backtest (NEW — required before Phase 1)

**Duration:** 2-3 days
**Input:** 10 wallets × 60+ days history fetched via GMGN pagination
**Output:** equity curves + summary stats (see Section 5.4)
**Gate:** All pass criteria must be met before proceeding
**Failure:** Stop. Report to Wildan. Discuss strategy adjustment.

### Phase 1: Dry Run (2 weeks MINIMUM)

```
EXECUTION_MODE=dry
```
- Bot runs full logic (detect, decide, "execute") but NO real swaps
- Log every decision: "WOULD BUY X SOL of token Y at price Z"
- Compare dry-run decisions vs forward test V3 results
- Verify: no crashes, no state corruption, reconciliation clean
- **Gate:** 2 weeks zero crashes + Wildan review of dry-run log → approve

### Phase 2: Micro Test (2 weeks MINIMUM)

```
EXECUTION_MODE=live
Entry size = 0.01 SOL (instead of 0.05)
```
- Hot wallet funded with 0.3 SOL
- Verify: execution success rate > 90%, slippage within expected range
- Verify: position tracking accurate (on-chain reconciliation clean)
- Verify: exit logic correct (mirror wallet proportional exits)
- **Gate:** 2 weeks + PnL report → Wildan approve

### Phase 3: Live Ramp-Up

```
Week 1: 0.5 SOL total, entry 0.05 SOL
Week 2: 1.0 SOL total, entry 0.05-0.10 SOL (tier-based)
Week 3: 2.0 SOL total, tier-based
Week 4+: 3.0 SOL max, tier-based (only if all prior weeks profitable)
```

- Daily heartbeat to Telegram
- Weekly PnL report (same format as Binance bot: WR, DD, PF)
- Any kill switch triggered → pause, report, Wildan decide resume

---

## 13. KILL SWITCHES & RECOVERY (Addresses H1)

### 13.1 Trigger Conditions

```yaml
kill_switches:
  consecutive_losses:
    threshold: 5
    action: pause_1h
    resume: auto
    
  daily_drawdown_pct:
    threshold: 15  # % of total capital
    action: pause_manual
    resume: manual_only
    
  api_error_rate_5min:
    threshold: 0.30  # 30% error rate
    action: pause_30m
    resume: auto
    
  rpc_latency_p95:
    threshold: 2.0  # seconds
    action: pause_auto
    resume: auto  # when latency normalizes
    
  actual_slippage_ratio:
    threshold: 2.0  # actual / expected
    action: blacklist_token_24h
    resume: auto  # 24h cooldown per token
    
  reconciliation_discrepancy:
    threshold: 2  # count
    action: pause_manual
    resume: manual_only
```

### 13.2 Recovery Procedures

| Kill Switch | Recovery |
|-------------|----------|
| consecutive_losses | Auto-resume after 1h cooldown. Counter resets on any win. |
| daily_drawdown | Telegram alert. Wildan must send `/resume` command via Telegram. |
| api_error_rate | Auto-resume after 30m. If triggers 3x in 24h → escalate to manual. |
| rpc_latency | Auto-resume when P95 latency <2s for 5 consecutive samples. |
| slippage_ratio | Token blacklisted 24h. Other tokens normal. |
| reconciliation | Telegram alert. Wildan reviews. `/resume` or `/skip token_addr`. |

### 13.3 Manual Override

Telegram commands (received by same bot, parsed from messages):

```
/status        → current positions, capital, PnL, kill switch state
/resume        → clear kill switch, resume trading
/pause         → manual pause (same as kill switch)
/exit <token>  → force exit specific position
/recon         → run reconciliation now
/heartbeat     → immediate heartbeat report
```

---

## DEVIATIONS FROM REVIEW (Addresses Briefing Requirement)

### DEV-1: C4 — GMGN MEV Protection RESOLVED

**Original concern:** No MEV protection → sandwich attacks  
**Finding:** `gmgn-cli swap --anti-mev` exists, default true. `--tip-fee` for Jito tip.  
**Resolution:** No direct Jito bundle needed. Using gmgn-cli built-in MEV protection.  
**Confidence:** High (empirically verified via `gmgn-cli swap --help`)

### DEV-2: H4 — Direct HTTP execution NOT implemented at this tier

**Original concern:** subprocess overhead 500ms  
**Finding:** For copy-trading BUY (not sniper), 60-120s activity poll interval dominates latency. Wallet holds hours/days. 500ms subprocess overhead is noise.  
**Resolution:** Keep subprocess for now. Revisit if we pivot to sub-second sniping.  
**Mitigation:** persistent HTTP client ready as config flag, can switch later.

### DEV-3: Refinement 2 — BUY "skip DCA" heuristics

**Note:** Simplified heuristic. Originally proposed "skip if wallet has multiple buys in 5 min." Implementation uses counter of previous buys per token per wallet from seen_tx. Edge case: wallet genuinely DCA-ing conviction token (should follow). Mitigation: the CONVICTION override (3x avg size) would still catch large DCA entries.

### DEV-4: Token safety filter — Jupiter API dependency

**Note:** `get_liquidity_usd()` depends on Jupiter quote API. If Jupiter rate-limits or is down: fallback to DexScreener pairs API or GMGN token info endpoint. Documented in error handling: skip token + alert if all sources fail.

---

## ACKNOWLEDGEMENTS

- Initial review: Claude (external) via Wildan — HERMES BRIEFING.md
- Strategy refinements: Wildan — HERMES RESPONSE.md
- GMGN pagination test: empirically verified June 9, 2026
- GMGN MEV test: empirically verified June 9, 2026
- Human wallet detector integration: existing system @ `/home/ubuntu/projects/human-wallet-detector/`

---

**END OF BLUEPRINT v2**
