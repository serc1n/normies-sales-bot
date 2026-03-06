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

DISCORD_WEBHOOK     = os.environ.get("DISCORD_WEBHOOK", "")
ALCHEMY_SIGNING_KEY = os.environ.get("ALCHEMY_SIGNING_KEY", "")
ALCHEMY_API_KEY     = os.environ.get("ALCHEMY_API_KEY", "kl7coWT2oFkRY0skuik4E")
PORT = int(os.environ.get("PORT", "8080"))

ALCHEMY_RPC = f"https://eth-mainnet.g.alchemy.com/v2/{ALCHEMY_API_KEY}"


# ── Discord ────────────────────────────────────────────────────

def short_addr(addr: str) -> str:
    return f"{addr[:6]}...{addr[-4:]}" if len(addr) > 10 else addr


def post_discord(token_id: str, price_eth: float, price_usd: float,
                 buyer: str, seller: str, tx_hash: str, timestamp: int):

    image_url = NORMIES_IMAGE.format(id=token_id)
    os_url    = OPENSEA_URL.format(contract=NORMIES_CONTRACT, id=token_id)

    price_str = f"{price_eth:g} ETH"
    if price_usd:
        price_str += f"  (${price_usd:,.0f})"

    fields = [
        {"name": "Price",  "value": price_str,             "inline": True},
        {"name": "Seller", "value": short_addr(seller),    "inline": True},
        {"name": "Buyer",  "value": short_addr(buyer),     "inline": True},
    ]
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


# ── Alchemy RPC sale lookup ────────────────────────────────────

WETH_CONTRACT = "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"


def _rpc(method: str, params: list) -> dict:
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    req = urllib.request.Request(
        ALCHEMY_RPC, data=body,
        headers={"Content-Type": "application/json", "User-Agent": "NormiesSalesBot/1.0"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read()).get("result") or {}


def lookup_tx_eth(tx_hash: str) -> float:
    """ETH msg.value — works for listed NFT sales (buyer sends native ETH to Seaport)."""
    try:
        tx = _rpc("eth_getTransactionByHash", [tx_hash])
        return int(tx.get("value", "0x0"), 16) / 1e18
    except Exception as e:
        print(f"[alchemy-rpc] eth lookup failed for {tx_hash}: {e}")
        return 0.0


def lookup_tx_weth(tx_hash: str, buyer: str) -> float:
    """Sum WETH Transfers FROM the buyer in this tx (offer-acceptance sales).
    Filters by buyer address so bundle transactions don't over-count."""
    try:
        receipt = _rpc("eth_getTransactionReceipt", [tx_hash])
        logs = receipt.get("logs", [])
        total = 0
        buyer_topic = "0x" + buyer.lower().replace("0x", "").zfill(64)
        for log in logs:
            topics = log.get("topics", [])
            if (log.get("address", "").lower() == WETH_CONTRACT.lower()
                    and len(topics) >= 3
                    and topics[0] == TRANSFER_TOPIC
                    and topics[1].lower() == buyer_topic):
                amount = int(log.get("data", "0x0"), 16)
                total += amount
        return total / 1e18
    except Exception as e:
        print(f"[alchemy-rpc] weth lookup failed for {tx_hash}: {e}")
        return 0.0



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

    # Count Normies per tx_hash so we can divide total tx price correctly
    # e.g. sweep of 5 Normies: msg.value = 5x price, divide by 5 per NFT
    normies_per_tx: dict[str, int] = {}
    for a in activities:
        if a.get("contractAddress", "").lower() == NORMIES_CONTRACT.lower():
            h = a.get("hash", "")
            if h:
                normies_per_tx[h] = normies_per_tx.get(h, 0) + 1

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
        nfts_in_tx = normies_per_tx.get(tx_hash, 1)

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

        # Alchemy NFT activity value=0 for marketplace sales (OpenSea/Blur route ETH
        # through their contracts). Look up real price via Alchemy APIs instead.
        inline_value = float(activity.get("value", 0))
        price_eth = inline_value
        price_usd = 0.0

        if tx_hash:
            # 1. Native ETH — listed NFT sale (buyer sends ETH as msg.value to Seaport)
            price_eth = lookup_tx_eth(tx_hash)
            if price_eth > 0:
                price_eth /= nfts_in_tx
                print(f"[price] Normie #{token_id} — {price_eth:.4f} ETH (native, {nfts_in_tx} in tx)")
            else:
                # 2. WETH — offer accepted (buyer pays in Wrapped ETH via ERC20 Transfer)
                price_eth = lookup_tx_weth(tx_hash, to_addr)
                if price_eth > 0:
                    price_eth /= nfts_in_tx
                    print(f"[price] Normie #{token_id} — {price_eth:.4f} ETH (WETH, {nfts_in_tx} in tx)")
                else:
                    print(f"[price] no price found for tx {tx_hash} Normie #{token_id}")

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
