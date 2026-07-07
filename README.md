# GWAS v1.1 — GMGN Wallet Alert System

**Read-only wallet discovery and alert system for Solana memecoin trading.**

GWAS discovers profitable wallets via the GMGN API, scores conviction for token
alerts, sends formatted Telegram notifications, and tracks performance via
Helius webhooks.

## Architecture

```
┌─────────────┐     ┌──────────────┐     ┌──────────────┐
│  GMGN API   │────▶│ Wallet       │────▶│ Conviction   │
│  (discovery)│     │ Scanner      │     │ Engine       │
└─────────────┘     └──────────────┘     └──────┬───────┘
                                                │
                    ┌──────────────┐            │
                    │ Telegram     │◀───────────┘
                    │ Alerts       │
                    └──────────────┘
                           │
                    ┌──────▼───────┐     ┌──────────────┐
                    │ User Action  │────▶│ Helius       │
                    │ (GMGN exec)  │     │ Webhook      │
                    └──────────────┘     └──────┬───────┘
                                                │
                    ┌──────────────┐            │
                    │ Performance  │◀───────────┘
                    │ Reports      │
                    └──────────────┘
```

## Directory Structure

```
/opt/gwas/
├── src/                  # Core library
│   ├── wallet_scanner.py # GMGN API wallet discovery
│   ├── conviction.py     # Scoring engine
│   ├── safety.py         # Safety filters (LP, holders, age)
│   ├── alert.py          # Telegram formatter & sender
│   ├── correlator.py     # Helius → alert matching
│   ├── performance.py    # Weekly reports
│   ├── helius_webhook.py # Flask webhook server
│   └── db.py             # SQLite models
├── scripts/              # Entry points
│   ├── run_scanner.py    # Main loop
│   ├── register_webhook.py
│   └── weekly_report.py
├── cron/                 # Cron/systemd wrappers
├── config/settings.yaml  # Configuration
├── data/                 # SQLite DB + backups
├── logs/                 # Log files
└── requirements.txt
```

## Quick Start

### 1. Set up environment

```bash
# Install dependencies
python3 -m venv /opt/gwas/venv
source /opt/gwas/venv/bin/activate
pip install -r /opt/gwas/requirements.txt

# Set required secrets
export HELIUS_API_KEY="your-key"
export TELEGRAM_BOT_TOKEN="your-bot-token"
```

### 2. Create secrets file

```bash
# Generate webhook secret
python3 -c "import secrets; print(secrets.token_hex(32))" >> ~/.gwas_secrets
echo "TELEGRAM_BOT_TOKEN=your-token" >> ~/.gwas_secrets
echo "HELIUS_API_KEY=your-key" >> ~/.gwas_secrets
echo "HELIUS_WEBHOOK_SECRET=generated-secret" >> ~/.gwas_secrets
```

### 3. Register Helius webhook (optional but recommended)

```bash
python3 scripts/register_webhook.py --register
```

### 4. Run scanner

```bash
# Single scan
python3 scripts/run_scanner.py --once

# Continuous loop
python3 scripts/run_scanner.py --interval 300
```

### 5. Install systemd services

```bash
sudo cp /opt/gwas/systemd/*.service /opt/gwas/systemd/*.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable gwas-webhook.service
sudo systemctl enable gwas-scanner.timer
sudo systemctl start gwas-webhook.service
sudo systemctl start gwas-scanner.timer
```

## Configuration

All settings in `/opt/gwas/config/settings.yaml`:

| Section | Key | Default | Description |
|---------|-----|---------|-------------|
| alert.quality_filter | min_wr | 30% | Minimum win rate |
| alert.quality_filter | min_pnl | 0 SOL | Minimum PnL |
| alert.quality_filter | min_trades | 10 | Minimum trade count |
| alert.safety_filter | min_token_age_minutes | 30 | Min token age |
| alert.safety_filter | min_lp_usd | 5000 | Min liquidity |
| alert.safety_filter | max_top10_holder_pct | 50% | Max concentration |
| alert.conviction | score_threshold | 70 | Min alert score |
| rate_limits | per_cycle | 10 | Alerts per scan |
| rate_limits | per_hour | 8 | Alerts per hour |
| rate_limits | per_day | 40 | Alerts per day |
| dead_alert | threshold_alerts | 15 | Alerts → dead |
| dead_alert | min_window_weeks | 2 | Window for dead check |
| correlation | default_window_hours | 4 | Auto-correlate window |

## Alert Format

### Compact Preview
```
🔔 GWAS: Wallet 7MvB... BUY BONK — WR 55% | PnL 12.5 SOL | Score 82/100
```

### Full Context
```
🔔 GWAS ALERT #a1b2c3d4
Wallet: 7MvB...
Token: BONK... (BONK)
Action: BUY
Size: 2.5 SOL
Conviction Score: 82/100
Wallet Stats (7d): WR 55%, PnL 12.5 SOL, Trades 23
Current Open: 3 positions
⚠️ FLAGS: none
Execution: GMGN | Photon
```

## Performance Tracking

Weekly reports compare alert-driven trades against independent trading:
- **Execute rate**: % of alerts acted upon
- **Alert PnL vs Independent**: Primary success gate (>+10%)
- **Win rate**: % of executed trades profitable
- **Profit factor**: Gross profit / gross loss
- **Dead wallets**: Alerts without execution for 2+ weeks

## Version

v1.1.0 — June 2026
