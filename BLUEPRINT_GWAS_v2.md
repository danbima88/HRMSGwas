# GWAS v2.0 — GMGN-Integrated Wallet Alert System

**Status**: DRAFT — diskusi  
**Pendahulu**: GWAS v1.2 (superseded)  
**Tanggal**: 10 Juni 2026  
**Insight kunci**: GMGN copy-trade handle 80% dari apa yang GWAS v1.2 coba bangun sendiri

---

## 1. The Pivot

GWAS v1.2 over-engineered karena gak tahu GMGN udah punya:
- Token filter (MC, liq, age, holders, platform)
- Buy mode (fixed, ratio, increase times, skip holdings)
- Sell rules (copy sell, multi-level TP/SL, trailing stop, dev sell, migrated sell)
- Anti-MEV protection
- Auto-pause setelah consecutive failures

GWAS v2.0 = **GMGN-aware**. GMGN adalah execution engine. GWAS adalah brain.

```
┌─────────────────────────────────────────────────────┐
│ GWAS v2.0                                           │
│                                                     │
│  ┌──────────┐   ┌──────────┐   ┌──────────────────┐│
│  │ SCANNER  │──▶│  SCORER  │──▶│  ALERTER         ││
│  │ detect   │   │ rank     │   │  "Setup wallet X" ││
│  │ wallet   │   │ wallets  │   │  ke Telegram      ││
│  └──────────┘   └──────────┘   └──────┬───────────┘│
│                                       │             │
│                     ┌─────────────────▼───────────┐ │
│                     │ GMGN COPY-TRADE (eksekusi)  │ │
│                     │ Buy/Sell/Filter/TP-SL/AntiMEV│ │
│                     └─────────────────┬───────────┘ │
│                                       │             │
│  ┌──────────┐   ┌─────────────────────▼───────────┐│
│  │ REPORTER │◀──│  CORRELATOR (Helius webhook)    ││
│  │ weekly   │   │  Track on-chain Wildan trades    ││
│  └──────────┘   └─────────────────────────────────┘│
└─────────────────────────────────────────────────────┘
```

---

## 2. What GMGN Handles (No GWAS Code Needed)

| Kategori | Fitur GMGN | Setting Rekomendasi |
|----------|-----------|-------------------|
| **Buy** | Fixed Buy mode | 0.05 SOL |
| **Buy** | Increase Times | 0 (first buy only) |
| **Buy** | Skip Holdings | ON |
| **Sell** | Copy Sell | ON |
| **Sell** | Trailing Stop Loss | 25% |
| **Sell** | Dev Sell | ≥30% dump → sell 50% |
| **Filter** | MC range | 5K – 500K |
| **Filter** | Liquidity min | 5K |
| **Filter** | Copy Buy Amount | 0.5 – 5 SOL |
| **Filter** | Holders min | 50 |
| **Filter** | Age | 1m – 2d |
| **Filter** | Min Burnt LP | ≥50% |
| **Platform** | Allowed | Pumpfun + Raydium |
| **Platform** | Token Blacklist | (per wallet) |
| **Fees** | Slippage | Auto |
| **Fees** | Priority Fee | 0.0004 SOL |
| **Fees** | Tip Fee | 0.0001 SOL |
| **Anti-MEV** | Boost Mode | Sec |

GWAS **tidak menulis kode untuk satupun fitur di atas**. Semua di-handle GMGN UI.

---

## 3. What GWAS v2.0 Actually Builds

### 3.1 Scanner (existing — reuse)
- Sudah jalan: `human-wallet-detector/` + 4x daily cron
- 724 wallet classified, dikirim ke @Hrmsgmgnbot
- **No change needed**

### 3.2 Wallet Scorer (NEW — lightweight)
- Rank wallet berdasarkan metrik on-chain
- Score 0-100 berdasarkan: 7d WR, 7d PnL, total trades, median hold time, token diversity
- Output: `data/wallet_scores.json`
- Re-scored setiap scanner run
- Notifikasi jika ada wallet baru yang layak (★80+) atau existing wallet turun (★ turun ke bawah threshold)

### 3.3 Alerter (NEW — lightweight)
- Format:

```
⚡ GMGN SETUP | 2szKH7nX ★87

Top wallet detected. Rekomendasi setup copy-trade di GMGN:

Buy: Fixed 0.05 SOL | Increase: 0
Filter: MC 5K-500K | Liq ≥5K | Age 1m-2d
Sell: Copy Sell ON | Trailing 25%
Anti-MEV: Sec

🔗 GMGN: https://gmgn.ai/...
📊 7D: WR 74% · +46.7 SOL · 12 trades
```

- Trigger: wallet ★80+ yang belum di-follow (belum ada strategi GMGN)

### 3.4 Strategy Tracker (NEW)
- Record wallet mana yang udah lo setup di GMGN
- Input: lo reply "✅" ke alert → GWAS catat sebagai "followed"
- Track: status (active/paused/closed), setup date, alert count
- File: `data/followed_wallets.json`

### 3.5 Correlator (SIMPLIFIED from v1.2)
- Helius webhook monitor wallet Wildan (read-only)
- **Hanya track 1 wallet** (Wildan's Solana wallet)
- Pair trade dengan alert: "apakah Wildan trade token X dalam 4 jam setelah alert wallet Y?"
- Window: default 4h, extend 24h via "✅" reply
- Output: correlation data → weekly report

### 3.6 Weekly Reporter
- Setiap Senin 9 AM ke Telegram
- Isi:
  - Followed wallets performance (total PnL per wallet)
  - Alert → execute rate per wallet
  - Wallet pool health (active, dead, pending)
  - Rekomendasi: add/remove wallets
- Format compact, actionable

---

## 4. Deliverables (D1-D6)

| ID | Komponen | Reuse/New | ETA |
|----|----------|-----------|-----|
| D1 | Scanner (no change) | Reuse 100% | 0 |
| D2 | `scorer.py` — wallet ranking | New | 30 min |
| D3 | `alerter.py` — Telegram alert with GMGN settings | New | 30 min |
| D4 | `tracker.py` — strategy tracking + "✅" handler | New | 30 min |
| D5 | `correlator.py` — Helius webhook + trade matching | Simplify from v1.2 | 1 jam |
| D6 | `reporter.py` — weekly report generator | New | 30 min |

**Total: ~3 jam development** (vs 7-8 jam di v1.2)

---

## 5. Strategy Flow

```
DAY 1: Scanner detects wallet 2szKH7nX ★87
       ↓
       Alerter: "⚡ GMGN SETUP | 2szKH7nX ★87"
       ↓
       Wildan: reply "✅" → tracker records as followed
       Wildan: buka GMGN → setup strategy (30 detik)
       ↓
DAY 1-7: GMGN auto-executes all wallet trades
         Helius correlator silently tracks on-chain
       ↓
MONDAY 9AM: Weekly report → Telegram
            "2szKH7nX: 12 alerts, 4 executed, +0.51 SOL"
            "4WNKVxa: 15 alerts, 0 executed → RECOMMEND REMOVAL"
```

---

## 6. Success Criteria

| Phase | Timing | Criteria |
|-------|--------|----------|
| **Phase 1: Deploy** | Day 0 | All 6 components running |
| **Phase 2: Calibrate** | Day 7 | Execute rate ≥20%, correlation working |
| **Phase 3: Prove Edge** | Day 28 | Followed wallets PnL > 0 SOL (net of all fees) |

**Go/No-Go setelah Day 28**: kalau net PnL positif + paling gak 1 wallet profitable → lanjut scale. Kalau semua wallet merah → evaluasi ulang wallet selection criteria.

---

## 7. File Structure

```
gmgn-auto-trader/
├── gwas/
│   ├── scorer.py           # D2 — wallet ranking
│   ├── alerter.py          # D3 — Telegram alert
│   ├── tracker.py          # D4 — strategy tracker + "✅" handler
│   ├── correlator.py       # D5 — Helius webhook correlator
│   ├── reporter.py         # D6 — weekly report
│   └── config.py           # Shared config
├── data/
│   ├── wallet_scores.json  # Scorer output
│   ├── followed_wallets.json  # Tracker output
│   ├── correlations.json   # Correlator output
│   └── gwas.db             # SQLite (correlator primary store)
├── BLUEPRINT_GWAS_v2.md    # This file
├── BLUEPRINT_GWAS_v1.md    # Archived — superseded
└── human-wallet-detector/  # Existing — untouched
```

---

## 8. Cron Jobs

| Name | Schedule | What |
|------|----------|------|
| GWAS-SCORER | After scanner runs (13:00, 19:00, 01:00, 07:00) | Re-score wallets, trigger alerter |
| GWAS-CORRELATOR | Daemon | Listen Helius webhook, match trades |
| GWAS-REPORTER | Monday 9 AM | Generate + send weekly report |

---

## 9. What We're NOT Building (v1.2 waste)

- ❌ Flask webhook server (Helius webhook → simple listener, not full HTTP server)
- ❌ 4-layer auth middleware (Helius handles auth natively)
- ❌ SQLite schema with 3 tables 6 indexes (simplified to JSON + light SQLite)
- ❌ Conviction scoring formula (simplified — GMGN filters handle token-level)
- ❌ Safety filter code (GMGN UI handles all filters)
- ❌ Notification relay (Telegram alert direct, no relay needed)
- ❌ Rate limiter (GMGN handles execution throttling)
- ❌ 10-file architecture (6 components, 4 reuse existing)

---

## 10. Cost

| Komponen | Cost/bulan |
|----------|-----------|
| Helius webhook (free tier) | $0 |
| Telegram bot (existing) | $0 |
| Cron execution (Hermes) | ~$2-3 |
| GMGN platform | 1% trade fee (built-in) |
| **TOTAL** | ~$2-3/bulan |

**v1.2 projected**: $12-15/bulan (Flask server + extra LLM calls for conviction + notification relay)
**Savings vs v1.2**: ~$10-12/bulan

---

## 11. Risks

| Risk | Mitigation |
|------|-----------|
| GMGN changes UI/API | GWAS is GMGN-independent — hanya send alert. Strategi manual di GMGN unaffected |
| Helius webhook downtime | Correlator has 24h backfill via GMGN transaction history |
| Wallet quality degrades | Scorer re-evaluates weekly, dead-alert auto-flag |
| User doesn't setup strategy | "✅" reply mechanism confirms setup; 48h no-reply = reminder |

---

## 12. Next Steps

1. ✅ Diskusi + approval Wildan
2. ⬜ Build D1-D6 (3 jam)
3. ⬜ Setup Helius webhook (target: Wildan's wallet)
4. ⬜ Register 3 cron jobs
5. ⬜ Smoke test: scanner → scorer → alert → Helius → correlator → report
6. ⬜ Phase 2: observasi 7 hari
7. ⬜ Phase 3: evaluasi 28 hari → go/no-go

---

**END OF BLUEPRINT GWAS v2.0**
**END OF BLUEPRINT GWAS v2.0**
