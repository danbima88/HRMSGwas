# 🔗 Helius Webhook Setup — GWAS v2.0

> **Panduan setup Helius webhook untuk GWAS v2.0.**
> Mencakup: akun Helius, API key, webhook registration, auth flow, parsing transaksi,
> korelasi dengan alerts, dan troubleshooting.
>
> Last updated: 10 Juni 2026

---

## Daftar Isi

1. [Overview](#-overview)
2. [Helius Account Setup](#-helius-account-setup)
3. [Webhook Registration](#-webhook-registration)
4. [Auth Flow: HMAC + IP Allowlist](#-auth-flow-hmac--ip-allowlist)
5. [Webhook Handler Architecture](#-webhook-handler-architecture)
6. [Transaction Parsing](#-transaction-parsing)
7. [Correlation with Alerts](#-correlation-with-alerts)
8. [Error Handling & Backfill](#-error-handling--backfill)
9. [Cost](#-cost)
10. [Troubleshooting](#-troubleshooting)

---

## 📌 Overview

### What Helius Does for GWAS

```
User sees GWAS alert → executes trade on GMGN → Helius detects the SWAP
  → webhook sends to GWAS → correlator matches trade to alert → marks executed
  → weekly report shows "X% execute rate"
```

**Tanpa Helius:** GWAS tetap berfungsi untuk discovery + alert, tapi tidak bisa track apakah alerts di-follow-up user. Performance metrics (execute rate, alert PnL vs independent) tidak tersedia.

### Architecture

```
┌─────────────────┐    HTTPS POST    ┌──────────────────┐
│  Helius Cloud   │───────┬─────────▶│  Flask Webhook    │
│  (webhook       │       │          │  Server (port     │
│   service)      │       │          │  8080)            │
└─────────────────┘       │          └────────┬─────────┘
                          │                   │
                HMAC-SHA256           ┌──────▼─────────┐
                signature             │  Correlator     │
                verification          │  4h window      │
                          │           └──────┬─────────┘
                IP allowlist                  │
                check               ┌────────▼─────────┐
                          │         │  SQLite gwas.db  │
                          │         │  alerts.executed │
                                     │  trades table   │
                                     └─────────────────┘
```

---

## 🔐 Helius Account Setup

### Step 1: Create Account

1. Buka [dev.helius.xyz](https://dev.helius.xyz)
2. Sign up dengan email / Google / GitHub
3. Masuk ke dashboard

### Step 2: Create API Key

1. Dashboard → **API Keys** → **Create New API Key**
2. Beri nama (contoh: `GWAS v2.0`)
3. Copy API key — format: `ebba198e-xxxx-xxxx-xxxx-xxxxxxxxxxxx`

### Step 3: Simpan API Key

```bash
# Tambahkan ke secrets file
echo "HELIUS_API_KEY=ebba198e-xxxx-xxxx-xxxx-xxxxxxxxxxxx" >> ~/.gwas_secrets

# Atau export untuk testing
export HELIUS_API_KEY="ebba198e-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
```

### Step 4: Generate Webhook Secret

```bash
# Generate random 64-char hex secret
python3 -c "import secrets; print(secrets.token_hex(32))"

# Simpan di secrets
echo "HELIUS_WEBHOOK_SECRET=a1b2c3d4e5f6..." >> ~/.gwas_secrets
```

### Free Tier Limits

| Resource | Limit |
|----------|-------|
| Requests/day | 50,000 |
| Webhooks | Unlimited |
| Webhook types | Enhanced (full transaction data) |
| RPC endpoints | Standard + Enhanced |
| Archive data | Not included (live only) |

**Untuk GWAS:** 50K req/day lebih dari cukup — webhook adalah **inbound** (Helius → kita), jadi ga consume API credits. Credits cuma kepakai kalau kita pake Helius RPC (yang GWAS v2.0 **tidak lakukan** — semua data dari GMGN).

---

## 📡 Webhook Registration

### Menggunakan `register_webhook.py`

Script sudah disediakan di `/opt/gwas/scripts/register_webhook.py`:

```bash
cd /opt/gwas
source venv/bin/activate

# Pastikan env vars diset
export HELIUS_API_KEY="ebba198e-..."
export HELIUS_WEBHOOK_SECRET="your-64-char-secret"

# Register webhook
python3 scripts/register_webhook.py --register

# List semua webhook terdaftar
python3 scripts/register_webhook.py --list

# Delete webhook (butuh webhook ID)
python3 scripts/register_webhook.py --delete {webhook_id}
```

### Registration Payload

Script mengirim request ini ke Helius API:

```json
POST https://api.helius.xyz/v0/webhooks?api-key={HELIUS_API_KEY}

{
  "webhookURL": "http://your-server-ip:8080/webhook",
  "transactionTypes": ["SWAP", "TRANSFER"],
  "accountAddresses": ["F9Br7smYRp4fSvoo4c5kwQKai74FtQy7T9pzxrqda494"],
  "webhookType": "enhanced",
  "authHeader": "a1b2c3d4e5f6..."
}
```

**Field Explanations:**

| Field | Value | Notes |
|-------|-------|-------|
| `webhookURL` | URL publik server GWAS | Harus bisa diakses dari internet! |
| `transactionTypes` | `["SWAP", "TRANSFER"]` | SWAP = DEX trades, TRANSFER = token transfers |
| `accountAddresses` | `[user_wallet]` | Wallet yang di-monitor |
| `webhookType` | `"enhanced"` | Dapat full transaction data (bukan cuma signature) |
| `authHeader` | Secret string | Dikirim sebagai `x-helius-signature` header |

### Webhook URL

Server GWAS harus punya URL publik yang bisa diakses Helius:

**Opsi 1: VPS dengan public IP**
```
http://123.456.789.0:8080/webhook
```

**Opsi 2: Ngrok (development/testing)**
```bash
ngrok http 8080
# Dapat URL: https://abc123.ngrok.io
# Register dengan: https://abc123.ngrok.io/webhook
```

**Opsi 3: Custom domain dengan reverse proxy (production)**
```nginx
# /etc/nginx/sites-available/gwas-webhook
server {
    listen 443 ssl;
    server_name webhook.yourdomain.com;

    location /webhook {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header Host $host;
    }
}
```

### Menyimpan Webhook ID

Setelah registrasi sukses, script auto-update `config/settings.yaml`:

```yaml
solana:
  helius_webhook_id: "wh_abc123..."
```

Ini berguna untuk tracking — tau webhook mana yang aktif tanpa harus list dari Helius API.

---

## 🔒 Auth Flow: HMAC + IP Allowlist

### Two-Layer Security (CB-G3)

```
Incoming Request
      │
      ▼
┌─────────────────────────┐
│ Layer 1: HMAC-SHA256    │
│                         │
│ 1. Get raw body bytes   │
│ 2. Get x-helius-        │
│    signature header     │
│ 3. Compute HMAC:        │
│    SHA256(secret, body) │
│ 4. Constant-time        │
│    compare              │
│                         │
│ Fail → 401 Unauthorized │
└──────────┬──────────────┘
           │ (pass)
           ▼
┌─────────────────────────┐
│ Layer 2: IP Allowlist   │
│                         │
│ 1. Get client IP from   │
│    X-Forwarded-For or   │
│    remote_addr          │
│ 2. Check against Helius │
│    CIDR ranges:         │
│    - 34.86.0.0/16       │
│    - 34.118.0.0/16      │
│    - 34.126.0.0/16      │
│    - 35.206.0.0/16      │
│    - 34.36.0.0/16       │
│ 3. Dev mode bypass:     │
│    GWAS_DEV_MODE=1      │
│                         │
│ Fail → 403 Forbidden    │
└──────────┬──────────────┘
           │ (pass)
           ▼
       Process webhook
```

### Implementation

```python
# src/helius_webhook.py lines 29-63

def verify_helius_signature(payload: bytes, signature_header: str) -> bool:
    if not signature_header:
        return False
    try:
        computed = hmac.new(
            WEBHOOK_SECRET.encode("utf-8"),
            payload,
            hashlib.sha256,
        ).hexdigest()
        return hmac.compare_digest(computed, signature_header)
    except Exception as e:
        return False

def verify_helius_ip() -> bool:
    forwarded = request.headers.get("X-Forwarded-For", "")
    client_ip = forwarded.split(",")[0].strip() if forwarded else request.remote_addr
    if not client_ip:
        return os.environ.get("GWAS_DEV_MODE", "") == "1"
    if is_helius_ip(client_ip):
        return True
    return os.environ.get("GWAS_DEV_MODE", "") == "1"
```

### Configuration

```bash
# Production (strict auth)
export HELIUS_WEBHOOK_SECRET="your-64-char-secret"
# GWAS_DEV_MODE not set → IP allowlist enforced

# Development (allow all IPs)
export GWAS_DEV_MODE=1
# IP check skipped, but HMAC signature still required
```

---

## 🖥️ Webhook Handler Architecture

### Endpoints

| Route | Method | Auth | Purpose |
|-------|--------|------|---------|
| `/webhook` | POST | HMAC + IP | Main Helius webhook receiver |
| `/webhook/direct` | POST | None | Manual testing, direct transaction data |
| `/health` | GET | None | Health check |

### Main Handler Flow

```python
@app.route("/webhook", methods=["POST"])
def webhook():
    raw_body = request.get_data()

    # 1. HMAC verification (fail → 401)
    signature = request.headers.get("x-helius-signature", "")
    if not verify_helius_signature(raw_body, signature):
        return jsonify({"error": "invalid signature"}), 401

    # 2. IP allowlist (fail → 403, unless dev mode)
    if not verify_helius_ip():
        return jsonify({"error": "unauthorized IP"}), 403

    # 3. Parse JSON (fail → 400)
    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError:
        return jsonify({"error": "invalid json"}), 400

    # 4. Extract transactions
    transactions = (
        payload if isinstance(payload, list)
        else payload.get("transactions", [payload])
    )

    # 5. Parse trades → correlate → persist
    trades = extract_trade_from_webhook(transactions)
    correlated = process_webhook_trades(trades)

    return jsonify({
        "status": "ok",
        "trades_processed": len(trades),
        "trades_correlated": correlated,
    })
```

### Running the Webhook Server

```bash
# Manual (development)
cd /opt/gwas
source venv/bin/activate
export HELIUS_WEBHOOK_SECRET="your-secret"
python3 -c "
from src.helius_webhook import run_server
run_server(host='0.0.0.0', port=8080)
"

# Daemon (production) — BELUM ADA SYSTEMD SERVICE UNTUK INI
# Gunakan screen/tmux atau tambahkan systemd service secara manual:
# sudo systemctl enable gwas-webhook.service
```

⚠️ **PENTING:** GWAS v2.0 tidak include systemd service untuk webhook server. Flask dev server bukan production-grade. Untuk production, gunakan gunicorn:

```bash
gunicorn -w 2 -b 0.0.0.0:8080 src.helius_webhook:app
```

---

## 🔍 Transaction Parsing

### What Helius Sends

Setiap webhook call berisi array transaksi dalam format **enhanced**:

```json
[
  {
    "signature": "5xQn6SxGkHU5gGtFNYA8C7zVkYE2eZmpF9rJu...",
    "timestamp": 1718123456000,
    "type": "SWAP",
    "description": "Bought 1,000,000 BONK for 7.89 SOL",
    "fee": 5000,
    "feePayer": "F9Br7smYRp4fSvoo4c5kwQKai74FtQy7T9pzxrqda494",
    "tokenTransfers": [
      {
        "mint": "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263",
        "tokenAmount": 1000000,
        "decimals": 5,
        "fromUserAccount": "...",
        "toUserAccount": "..."
      }
    ],
    "accountData": [
      {
        "account": "F9Br7smYRp4fSvoo4c5kwQKai74FtQy7T9pzxrqda494",
        "nativeBalanceChange": -7890000000
      }
    ],
    "events": {
      "swap": {
        "nativeInput": 7890000000,
        "nativeOutput": null,
        "tokenInput": null,
        "tokenOutput": 100000000000
      }
    }
  }
]
```

### GWAS Parsing (`extract_trade_from_webhook()`)

```
Transaction data
      │
      ▼
┌─────────────────────────────────────────────┐
│ 1. Filter type: SWAP / TRANSFER only        │
│    Skip: UNKNOWN, None (non-trade txs)       │
├─────────────────────────────────────────────┤
│ 2. Extract signature → tx_hash               │
├─────────────────────────────────────────────┤
│ 3. Parse timestamp (ms → s jika > 1e12)      │
├─────────────────────────────────────────────┤
│ 4. Parse amount + direction (BUY/SELL):      │
│                                                │
│    Priority order:                            │
│    a. events.swap.nativeInput/Output         │
│    b. accountData.nativeBalanceChange        │
│    c. description string match               │
│    d. tokenTransfers tokenAmount             │
│                                                │
│    Direction logic:                           │
│    - nativeBalanceChange > 0 → SELL          │
│    - nativeBalanceChange < 0 → BUY           │
│    - nativeInput > 0 → BUY                   │
│    - nativeOutput > 0 → SELL                 │
├─────────────────────────────────────────────┤
│ 5. Extract token address from transfers      │
├─────────────────────────────────────────────┤
│ 6. Calculate fee (lamports / 1e9)            │
├─────────────────────────────────────────────┤
│ 7. Return trade dict:                        │
│    {tx_hash, wallet_address, token_address,  │
│     action, amount_sol, fee_sol, timestamp,  │
│     correlated_alert_id: None}               │
└─────────────────────────────────────────────┘
```

### Direction Detection (Multi-Source)

```python
def _parse_amount_from_instruction(tx_data):
    # Source 1: events.swap (most reliable)
    swap = events.get("swap", {})
    native_in = float(swap.get("nativeInput", 0)) / 1e9
    native_out = float(swap.get("nativeOutput", 0)) / 1e9
    if native_in > 0:
        action = "BUY"; amount_sol = native_in
    elif native_out > 0:
        action = "SELL"; amount_sol = native_out

    # Source 2: accountData.nativeBalanceChange
    for acct in account_data:
        if acct["account"] == feePayer:
            native_change = float(acct["nativeBalanceChange"]) / 1e9
            if native_change > 0: action = "SELL"
            elif native_change < 0: action = "BUY"

    # Source 3: description string (fallback)
    if "bought" in desc.lower() or "buy" in desc.lower():
        action = "BUY"
    elif "sold" in desc.lower() or "sell" in desc.lower():
        action = "SELL"
```

---

## 🔗 Correlation with Alerts

### How It Works

```
Trade arrives from webhook
  │
  ▼
correlate_trade(trade, window_hours=4)
  │
  ├── Query DB: get_alerts_for_token_wallet(token, wallet)
  │     └── SELECT * FROM alerts
  │         WHERE token_address = ? AND wallet_address = ?
  │         AND executed = FALSE
  │         AND alert_timestamp BETWEEN (trade_ts - 4h) AND trade_ts
  │
  ├── Match found? → mark alert executed
  │     └── UPDATE alerts SET executed=TRUE, execute_tx_hash=?, execute_timestamp=?
  │     └── INSERT INTO trades (correlated_alert_id = alert.id)
  │
  └── No match? → insert trade without correlation
        └── INSERT INTO trades (correlated_alert_id = NULL)
```

### Correlation Windows

| Window | Duration | Trigger |
|--------|----------|---------|
| Default auto-correlation | 4 hours | Automatic via webhook |
| Manual extend | 24 hours | User replies "✅ taken" to alert |

**Default (4h):** Cukup untuk kebanyakan kasus — user lihat alert, buka GMGN, eksekusi trade dalam hitungan menit/jam.

**Manual extend (24h):** Untuk kasus di mana user deliberasi lebih lama atau trade terjadi di session berbeda.

### Manual Extension via `manual_extend_correlation()`

```python
# correlator.py lines 181-208

def manual_extend_correlation(alert_id, user_wallet, extend_hours=24):
    alert = db.get_alert_by_id(alert_id)
    if not alert or alert.get("executed"):
        return 0

    trades = db.get_recent_trades(hours=extend_hours)
    correlated = 0
    for trade in trades:
        if trade["wallet_address"] != user_wallet: continue
        if trade["token_address"] != alert["token_address"]: continue
        if trade.get("correlated_alert_id"): continue

        trade["correlated_alert_id"] = alert_id
        db.insert_trade(trade)
        db.mark_alert_executed(alert_id, trade["tx_hash"], trade["timestamp"])
        correlated += 1

    return correlated
```

### Matching Precision

Matching menggunakan **exact match** pada:
1. `wallet_address` — harus sama persis
2. `token_address` — harus sama persis
3. Time window — `alert_timestamp BETWEEN (trade_ts - 4h) AND trade_ts`

**Tidak menggunakan:** fuzzy matching, partial address, atau amount tolerance. Ini by design — menghindari false correlation.

### What Happens When No Match

- Trade tetap disimpan di `trades` table
- `correlated_alert_id = NULL`
- Trade masuk ke **independent PnL** (bukan executed PnL)
- Weekly report membandingkan: alert PnL vs independent PnL

---

## 🛡️ Error Handling & Backfill

### Webhook Delivery Failures

**Skenario 1: Server GWAS down**

Helius akan **retry** webhook delivery dengan exponential backoff:
- Retry 1: ~5 detik
- Retry 2: ~30 detik
- Retry 3: ~5 menit
- Retry 4: ~30 menit
- ...up to ~24 jam

Setelah 24 jam gagal, Helius **drop** event tersebut.

**Mitigasi:** Webhook server harus selalu running. Rekomendasi: systemd service + gunicorn.

### Manual Backfill

Kalau ada gap (server down > 24 jam), tidak ada automatic backfill. Opsi manual:

```bash
cd /opt/gwas
source venv/bin/activate
export HELIUS_API_KEY="..."

# Fetch recent signatures untuk user wallet
curl "https://api.helius.xyz/v0/addresses/F9Br7smYRp4fSvoo4c5kwQKai74FtQy7T9pzxrqda494/transactions?api-key=$HELIUS_API_KEY&limit=100"

# Parse transactions → POST ke /webhook/direct
curl -X POST http://localhost:8080/webhook/direct \
  -H "Content-Type: application/json" \
  -d @transactions.json
```

### Webhook Health Check

```bash
# Cek webhook server status
curl http://localhost:8080/health
# → {"status": "ok", "timestamp": "2026-06-10T00:46:00"}

# Cek registered webhooks via Helius API
python3 /opt/gwas/scripts/register_webhook.py --list
```

### Missing Correlation Debugging

Kalau trade tidak ter-correlate padahal harusnya:

1. **Cek waktu:** alert_timestamp vs trade timestamp. Pastikan dalam 4 jam.
2. **Cek wallet:** pastikan wallet_address di trade = wallet_address di alert.
3. **Cek token:** pastikan token_address persis sama (case-sensitive!).
4. **Cek executed:** alert mungkin sudah di-mark executed oleh trade sebelumnya.

```bash
# Debug query langsung ke SQLite
sqlite3 /opt/gwas/data/gwas.db "
SELECT id, wallet_address, token_address, alert_timestamp, executed
FROM alerts
WHERE wallet_address = 'F9Br7smYRp4fSvoo4c5kwQKai74FtQy7T9pzxrqda494'
ORDER BY alert_timestamp DESC
LIMIT 5;
"

sqlite3 /opt/gwas/data/gwas.db "
SELECT tx_hash, token_address, action, timestamp, correlated_alert_id
FROM trades
WHERE wallet_address = 'F9Br7smYRp4fSvoo4c5kwQKai74FtQy7T9pzxrqda494'
ORDER BY timestamp DESC
LIMIT 5;
"
```

---

## 💰 Cost

### Helius Pricing

| Tier | Request/Day | Webhook | Monthly Cost | GWAS Usage |
|------|-------------|---------|-------------|------------|
| **Free** | 50,000 | ✅ Unlimited | **$0** | ✅ Cukup |
| Developer | 250,000 | ✅ Unlimited | $49 | Overkill |
| Business | 1,000,000 | ✅ Unlimited | $199 | Overkill |

**GWAS v2.0 menggunakan Free Tier — $0/month.**

Kenapa cukup:
- Webhook adalah **inbound** (Helius → kita) — tidak consume API credits
- GWAS tidak menggunakan Helius RPC untuk wallet discovery (semua dari GMGN)
- Helius RPC hanya digunakan untuk webhook registration (1 call) dan occasional health check

### Alternative: Helius RPC (Not Used by GWAS)

Kalau suatu saat GWAS perlu Helius RPC (misal: fetch token metadata, get account info):

```bash
# Example RPC call (tidak digunakan GWAS v2.0)
curl "https://mainnet.helius-rpc.com/?api-key=$HELIUS_API_KEY" \
  -X POST -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"getAccountInfo","params":["..."]}'
```

Setiap RPC call consume 1 credit dari 50K/day free tier.

---

## 🔧 Troubleshooting

### Problem: Webhook Registration Failed

```
❌ Registration failed: 401 Unauthorized
```

**Fix:** Cek `HELIUS_API_KEY` — pastikan format UUID: `ebba198e-xxxx-xxxx-xxxx-xxxxxxxxxxxx`

```
❌ Registration failed: 400 Bad Request
```

**Fix:** Cek `webhookURL` bisa diakses dari internet. Helius melakukan **verification ping** saat registrasi — URL harus return 200.

### Problem: Webhook Receives No Events

**Checklist:**
1. Webhook server running? `curl http://localhost:8080/health`
2. Webhook URL accessible from internet? `curl -X POST https://yourserver.com/webhook -d '[]'`
3. Wallet address benar? Cek `settings.yaml → solana.user_wallet`
4. Ada transaksi di wallet? Cek di Solscan/SolanaFM
5. Webhook types benar? Harusnya `["SWAP", "TRANSFER"]`

### Problem: HMAC Signature Invalid

```
WARNING - Webhook rejected: invalid signature
```

**Fix:**
1. Pastikan `HELIUS_WEBHOOK_SECRET` sama dengan yang dikirim saat register
2. Pastikan Helius mengirim `x-helius-signature` header
3. Debug: enable `GWAS_DEV_MODE=1` untuk skip IP check, cek apakah HMAC masih fail

### Problem: IP Allowlist Blocking

```
WARNING - Request from non-Helius IP: 123.456.789.0
```

**Possible causes:**
1. Reverse proxy (nginx) tidak forward `X-Forwarded-For` header
2. Helius menambah IP ranges baru (update `HELIUS_IPS` di `safety.py`)

**Fix sementara:** `export GWAS_DEV_MODE=1`

### Problem: Transactions Not Correlating

**Debug query:**
```sql
-- Cek unexecuted alerts
SELECT id, token_address, wallet_address, alert_timestamp
FROM alerts WHERE executed = FALSE
ORDER BY alert_timestamp DESC LIMIT 10;

-- Cek recent trades
SELECT tx_hash, token_address, wallet_address, timestamp, correlated_alert_id
FROM trades
ORDER BY timestamp DESC LIMIT 10;

-- Manual cross-reference: apakah ada trade yang match alert?
SELECT a.id as alert_id, t.tx_hash
FROM alerts a
JOIN trades t ON a.token_address = t.token_address
  AND a.wallet_address = t.wallet_address
WHERE a.executed = FALSE
  AND t.timestamp BETWEEN datetime(a.alert_timestamp, '-4 hours') AND datetime(a.alert_timestamp, '+1 hour');
```

---

## 📁 Related Files

- `/opt/gwas/src/helius_webhook.py` — Flask webhook server (159 lines)
- `/opt/gwas/src/correlator.py` — Trade correlation logic (208 lines)
- `/opt/gwas/src/safety.py` — `is_helius_ip()` dan `HELIUS_IPS` (lines 84-91, 229-239)
- `/opt/gwas/scripts/register_webhook.py` — Webhook registration CLI (148 lines)
- `/opt/gwas/config/settings.yaml` — Helius section + `helius_webhook_id`
- `/home/ubuntu/.gwas_secrets` — `HELIUS_API_KEY`, `HELIUS_WEBHOOK_SECRET`
- `/opt/gwas/ARCHITECTURE.md` — Webhook section in full architecture
- `/opt/gwas/DEVIATIONS.md` — CB-G3: HMAC + IP allowlist rationale

---

*Helius free tier is sufficient for GWAS v2.0 monitoring needs.*
*Jika scaling up (multiple wallets, higher frequency), pertimbangkan upgrade ke Developer tier.*
