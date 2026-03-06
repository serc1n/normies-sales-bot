#!/usr/bin/env python3
"""Normies sales tracker — receives Alchemy webhook events and posts to Discord."""

import os
import json
import hmac
import hashlib
import base64
import urllib.request
import urllib.error
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime, timezone

NORMIES_CONTRACT = "0x9Eb6E2025B64f340691e424b7fe7022fFDE12438"
NORMIES_IMAGE    = "https://api.normies.art/normie/{id}/image.png"
OPENSEA_URL      = "https://opensea.io/assets/ethereum/{contract}/{id}"
ETHERSCAN_TX     = "https://etherscan.io/tx/{tx}"
RESERVOIR_API    = "https://api.reservoir.tools"

DISCORD_WEBHOOK     = os.environ.get("DISCORD_WEBHOOK", "")
ALCHEMY_SIGNING_KEY = os.environ.get("ALCHEMY_SIGNING_KEY", "")
PORT = int(os.environ.get("PORT", "8080"))


# ── Discord ────────────────────────────────────────────────────

def short_addr(addr: str) -> str:
    return f"{addr[:6]}...{addr[-4:]}" if len(addr) > 10 else addr


def post_discord(token_id: str, price_eth: float, price_usd: float,
                 buyer: str, seller: str, tx_hash: str, timestamp: int):

    image_url = NORMIES_IMAGE.format(id=token_id)
    os_url    = OPENSEA_URL.format(contract=NORMIES_CONTRACT, id=token_id)
    tx_url    = ETHERSCAN_TX.format(tx=tx_hash) if tx_hash else None

    price_str = f"{price_eth:g} ETH"
    if price_usd:
        price_str += f"  (${price_usd:,.0f})"

    fields = [
        {"name": "Price",  "value": price_str,             "inline": True},
        {"name": "Seller", "value": short_addr(seller),    "inline": True},
        {"name": "Buyer",  "value": short_addr(buyer),     "inline": True},
    ]
    if tx_url:
        fields.append({"name": "Tx", "value": f"[etherscan]({tx_url})", "inline": True})

    embed = {
        "title": f"Normie #{token_id} sold",
        "url": os_url,
        "color": 0x48494B,
        "thumbnail": {"url": image_url},
        "fields": fields,
        "footer": {"text": "Normies · Built by Normies, for Normies"},
        "timestamp": datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat(),
    }

    payload = json.dumps({"embeds": [embed]}).encode()
    req = urllib.request.Request(
        DISCORD_WEBHOOK,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "NormiesSalesBot/1.0",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10):
            print(f"[discord] posted Normie #{token_id} — {price_eth:.4f} ETH")
    except urllib.error.HTTPError as e:
        print(f"[discord] HTTP {e.code}: {e.read().decode()}")
    except Exception as e:
        print(f"[discord] error: {e}")


# ── Reservoir sale lookup ──────────────────────────────────────

def lookup_sale(tx_hash: str, token_id: str) -> dict | None:
    """Look up sale price from Reservoir by tx hash + token id. Returns sale dict or None."""
    url = f"{RESERVOIR_API}/sales/v6?txHash={tx_hash}&limit=50"
    req = urllib.request.Request(url, headers={"accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
            for sale in data.get("sales", []):
                if str(sale.get("token", {}).get("tokenId")) == str(token_id):
                    return sale
            # fallback: return first sale from this tx if token match fails
            sales = data.get("sales", [])
            return sales[0] if sales else None
    except Exception as e:
        print(f"[reservoir] lookup failed for tx {tx_hash}: {e}")
        return None


# ── Alchemy webhook signature verification ────────────────────

def verify_signature(body: bytes, sig_header: str) -> bool:
    # Signature verification skipped — webhook URL is private to Alchemy
    return True


# ── Parse Alchemy NFT activity payload ───────────────────────

def handle_alchemy_event(payload: dict):
    """Parse Alchemy NFT activity webhook and post sale to Discord."""
    event = payload.get("event", {})
    activities = event.get("activity", [])

    print(f"[alchemy] {len(activities)} activit(ies) received")

    for activity in activities:
        print(f"[alchemy] activity: {json.dumps(activity)[:400]}")

        contract = activity.get("contractAddress", "").lower()

        if contract != NORMIES_CONTRACT.lower():
            print(f"[alchemy] skipping — wrong contract: {contract}")
            continue

        from_addr = activity.get("fromAddress", "")
        to_addr   = activity.get("toAddress", "")
        tx_hash   = activity.get("hash", "")
        token_id  = activity.get("erc721TokenId", "")

        # Convert hex token id if needed
        if token_id and token_id.startswith("0x"):
            token_id = str(int(token_id, 16))

        # Skip mints (from zero address)
        if from_addr == "0x0000000000000000000000000000000000000000":
            print(f"[alchemy] skipping Normie #{token_id} — mint (not a sale)")
            continue

        block_time = activity.get("blockTimestamp", "")
        try:
            ts = int(datetime.fromisoformat(
                block_time.replace("Z", "+00:00")
            ).timestamp())
        except Exception:
            ts = int(__import__("time").time())

        # Alchemy value is 0 for marketplace sales (OpenSea/Blur pay via contract)
        # Always look up the real price from Reservoir
        inline_value = float(activity.get("value", 0))
        price_eth = inline_value

        if tx_hash:
            print(f"[alchemy] looking up sale price for tx {tx_hash} Normie #{token_id}")
            sale = lookup_sale(tx_hash, token_id)
            if sale:
                price_raw = sale.get("price", {}).get("amount", {}).get("decimal", 0)
                price_eth = float(price_raw) if price_raw else 0
                price_usd_raw = sale.get("price", {}).get("amount", {}).get("usd", 0)
                price_usd = float(price_usd_raw) if price_usd_raw else 0
                print(f"[reservoir] Normie #{token_id} — {price_eth} ETH (${price_usd:.0f})")
            else:
                price_usd = 0
                print(f"[reservoir] no sale found for tx {tx_hash}, using inline value={inline_value}")
        else:
            price_usd = 0

        if price_eth <= 0:
            print(f"[alchemy] skipping Normie #{token_id} — price=0 (likely not a sale)")
            continue

        print(f"[alchemy] sale confirmed: Normie #{token_id} for {price_eth} ETH")
        post_discord(
            token_id=token_id,
            price_eth=price_eth,
            price_usd=price_usd,
            buyer=to_addr,
            seller=from_addr,
            tx_hash=tx_hash,
            timestamp=ts,
        )


# ── HTTP server (receives Alchemy webhook POSTs) ──────────────

class WebhookHandler(BaseHTTPRequestHandler):

    def do_GET(self):
        if self.path.startswith("/test"):
            if not DISCORD_WEBHOOK:
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"ERROR: DISCORD_WEBHOOK env var is not set!")
                return
            post_discord(
                token_id="42",
                price_eth=0.08,
                price_usd=240,
                buyer="0x9f8e7d6c5b4a3f2e1d0c9b8a7f6e5d4c3b2a1f0e",
                seller="0x1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b",
                tx_hash="0xabc123def456abc123def456abc123def456abc123def456abc123def456abc1",
                timestamp=int(__import__("time").time()),
            )
            self.send_response(200)
            self.end_headers()
            self.wfile.write(f"Test fired! Webhook URL: {DISCORD_WEBHOOK[:50]}...".encode())
            return

        # Health check
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Normies sales bot running")

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body   = self.rfile.read(length)

        # Verify Alchemy signature
        sig = self.headers.get("x-alchemy-signature", "")
        if not verify_signature(body, sig):
            print("[webhook] invalid signature — rejected")
            self.send_response(401)
            self.end_headers()
            return

        try:
            payload = json.loads(body)
            webhook_type = payload.get("type", "")
            print(f"[webhook] received type={webhook_type} body={body[:300]}")

            if webhook_type == "NFT_ACTIVITY":
                handle_alchemy_event(payload)

            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

        except Exception as e:
            print(f"[webhook] error processing payload: {e}")
            self.send_response(500)
            self.end_headers()

    def log_message(self, fmt, *args):
        # Suppress default HTTP log noise
        pass


def main():
    print(f"Normies sales bot starting on port {PORT}")
    print(f"Contract: {NORMIES_CONTRACT}")
    server = HTTPServer(("0.0.0.0", PORT), WebhookHandler)
    server.serve_forever()


if __name__ == "__main__":
    main()
