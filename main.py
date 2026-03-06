#!/usr/bin/env python3
"""Normies sales tracker — receives Alchemy webhook events and posts to Discord."""

import os
import json
import hmac
import hashlib
import urllib.request
import urllib.error
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime, timezone

NORMIES_CONTRACT = "0x9Eb6E2025B64f340691e424b7fe7022fFDE12438"
NORMIES_IMAGE    = "https://api.normies.art/normie/{id}/image.png"
OPENSEA_URL      = "https://opensea.io/assets/ethereum/{contract}/{id}"
ETHERSCAN_TX     = "https://etherscan.io/tx/{tx}"

DISCORD_WEBHOOK   = os.environ["DISCORD_WEBHOOK"]
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

    price_str = f"{price_eth:.4f} ETH"
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
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10):
            print(f"[discord] posted Normie #{token_id} — {price_eth:.4f} ETH")
    except urllib.error.HTTPError as e:
        print(f"[discord] HTTP {e.code}: {e.read().decode()}")
    except Exception as e:
        print(f"[discord] error: {e}")


# ── Alchemy webhook signature verification ────────────────────

def verify_signature(body: bytes, sig_header: str) -> bool:
    if not ALCHEMY_SIGNING_KEY:
        return True  # skip verification if key not set
    expected = hmac.new(
        ALCHEMY_SIGNING_KEY.encode(),
        body,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, sig_header)


# ── Parse Alchemy NFT activity payload ───────────────────────

def handle_alchemy_event(payload: dict):
    """Parse Alchemy NFT activity webhook and post sale to Discord."""
    event = payload.get("event", {})
    activities = event.get("activity", [])

    for activity in activities:
        # Only process sales (fromAddress is seller, toAddress is buyer)
        category = activity.get("category", "")
        if category != "token":
            continue

        contract = activity.get("contractAddress", "").lower()
        if contract != NORMIES_CONTRACT.lower():
            continue

        from_addr = activity.get("fromAddress", "")
        to_addr   = activity.get("toAddress", "")
        tx_hash   = activity.get("hash", "")
        value     = float(activity.get("value", 0))
        token_id  = activity.get("erc721TokenId", "")

        # Convert hex token id if needed
        if token_id and token_id.startswith("0x"):
            token_id = str(int(token_id, 16))

        block_time = activity.get("blockTimestamp", "")
        try:
            ts = int(datetime.fromisoformat(
                block_time.replace("Z", "+00:00")
            ).timestamp())
        except Exception:
            ts = int(__import__("time").time())

        # Skip zero-value transfers (not sales)
        if value <= 0:
            continue

        print(f"[alchemy] sale detected: Normie #{token_id} for {value} ETH")
        post_discord(
            token_id=token_id,
            price_eth=value,
            price_usd=0,  # Alchemy doesn't include USD, can add via CoinGecko later
            buyer=to_addr,
            seller=from_addr,
            tx_hash=tx_hash,
            timestamp=ts,
        )


# ── HTTP server (receives Alchemy webhook POSTs) ──────────────

class WebhookHandler(BaseHTTPRequestHandler):

    def do_GET(self):
        # Health check for Railway
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
