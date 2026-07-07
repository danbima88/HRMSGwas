#!/usr/bin/env python3
"""
GWAS v2.0 — Helius Webhook Registration Script
Registers a webhook with Helius to monitor the user wallet.

Usage:
    python3 scripts/register_webhook.py [--register] [--list] [--delete WEBHOOK_ID]

Requires HELIUS_API_KEY in environment.
"""

import sys
import os
import json
import argparse
import requests
import yaml

HELIUS_API_KEY = os.environ.get("HELIUS_API_KEY", "")
HELIUS_API = "https://api.helius.xyz/v0"


def load_config() -> dict:
    config_path = "/opt/gwas/config/settings.yaml"
    with open(config_path) as f:
        return yaml.safe_load(f)


def register_webhook(webhook_url: str, wallet_addresses: list[str], webhook_secret: str) -> dict:
    """
    Register a webhook with Helius API.
    Monitors SWAP transactions for the given wallets.
    """
    url = f"{HELIUS_API}/webhooks?api-key={HELIUS_API_KEY}"
    payload = {
        "webhookURL": webhook_url,
        "transactionTypes": ["SWAP", "TRANSFER"],
        "accountAddresses": wallet_addresses,
        "webhookType": "enhanced",
        "authHeader": webhook_secret,
    }

    print(f"Registering webhook at {webhook_url}...")
    print(f"Wallets: {wallet_addresses}")
    resp = requests.post(url, json=payload, timeout=15)

    if resp.status_code in (200, 201):
        data = resp.json()
        print(f"✅ Webhook registered! ID: {data.get('webhookID', 'unknown')}")
        return data
    else:
        print(f"❌ Registration failed: {resp.status_code} {resp.text}")
        return {}


def list_webhooks():
    """List all registered webhooks."""
    url = f"{HELIUS_API}/webhooks?api-key={HELIUS_API_KEY}"
    resp = requests.get(url, timeout=10)
    if resp.status_code == 200:
        webhooks = resp.json()
        print(f"\n📋 Registered Webhooks ({len(webhooks)}):")
        for wh in webhooks:
            print(f"  ID: {wh.get('webhookID', '?')}")
            print(f"  URL: {wh.get('webhookURL', '?')}")
            print(f"  Wallets: {wh.get('accountAddresses', [])}")
            print(f"  Types: {wh.get('transactionTypes', [])}")
            print()
        return webhooks
    else:
        print(f"❌ List failed: {resp.status_code} {resp.text}")
        return []


def delete_webhook(webhook_id: str):
    """Delete a webhook by ID."""
    url = f"{HELIUS_API}/webhooks/{webhook_id}?api-key={HELIUS_API_KEY}"
    resp = requests.delete(url, timeout=10)
    if resp.status_code == 200:
        print(f"✅ Webhook {webhook_id} deleted")
    else:
        print(f"❌ Delete failed: {resp.status_code} {resp.text}")


def get_public_url() -> str:
    """
    Try to determine a public URL for the webhook.
    In production this would be a domain or ngrok URL.
    """
    import socket
    hostname = socket.gethostname()
    try:
        local_ip = socket.gethostbyname(hostname)
    except socket.gaierror:
        local_ip = "localhost"

    return os.environ.get("GWAS_WEBHOOK_URL", f"http://{local_ip}:8080/webhook")


def main():
    parser = argparse.ArgumentParser(description="Helius Webhook Manager")
    parser.add_argument("--register", action="store_true", help="Register webhook")
    parser.add_argument("--list", action="store_true", help="List webhooks")
    parser.add_argument("--delete", type=str, help="Delete webhook by ID")
    parser.add_argument("--url", type=str, help="Override webhook URL (default: auto-detect)")
    parser.add_argument("--wallet", type=str, help="Override wallet address to monitor")
    args = parser.parse_args()

    if not HELIUS_API_KEY:
        print("❌ HELIUS_API_KEY not set in environment")
        sys.exit(1)

    config = load_config()

    if args.delete:
        delete_webhook(args.delete)
        return

    if args.list or (not args.register):
        list_webhooks()

    if args.register:
        webhook_url = args.url or get_public_url()
        wallet = args.wallet or config.get("solana", {}).get(
            "user_wallet", "F9Br7smYRp4fSvoo4c5kwQKai74FtQy7T9pzxrqda494"
        )
        secret = os.environ.get("HELIUS_WEBHOOK_SECRET", "")

        result = register_webhook(
            webhook_url=webhook_url,
            wallet_addresses=[wallet],
            webhook_secret=secret,
        )

        if result:
            # Update config with webhook ID
            webhook_id = result.get("webhookID", "")
            if webhook_id:
                config["solana"]["helius_webhook_id"] = webhook_id
                config_path = "/opt/gwas/config/settings.yaml"
                with open(config_path, "w") as f:
                    yaml.dump(config, f, default_flow_style=False)
                print(f"Updated settings.yaml with webhook_id: {webhook_id}")


if __name__ == "__main__":
    main()
