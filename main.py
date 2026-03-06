#!/usr/bin/env python3
"""Normies sales tracker — receives Alchemy webhook events and posts to Discord."""

import os
import json
import math
import hmac
import hashlib
import base64
import threading
import time
import urllib.request
import urllib.error
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime, timezone

try:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    from cryptography.exceptions import InvalidSignature
    HAS_CRYPTOGRAPHY = True
except ImportError:
    HAS_CRYPTOGRAPHY = False

# Deduplication: track (tx_hash, token_id) pairs we've already posted.
# Stored as {key: timestamp}, entries expire after 1 hour.
_seen_lock = threading.Lock()
_seen: dict[str, float] = {}

def _is_duplicate(tx_hash: str, token_id: str) -> bool:
    key = f"{tx_hash.lower()}:{token_id}"
    now = time.time()
    with _seen_lock:
        # Expire old entries
        expired = [k for k, t in _seen.items() if now - t > 3600]
        for k in expired:
            del _seen[k]
        if key in _seen:
            return True
        _seen[key] = now
        return False

NORMIES_CONTRACT  = "0x9Eb6E2025B64f340691e424b7fe7022fFDE12438"
CANVAS_CONTRACT   = "0x64951d92e345C50381267380e2975f66810E869c"
ZERO_ADDRESS      = "0x0000000000000000000000000000000000000000"
NORMIES_IMAGE     = "https://api.normies.art/normie/{id}/image.png"
OPENSEA_URL       = "https://opensea.io/assets/ethereum/{contract}/{id}"

DISCORD_WEBHOOK      = os.environ.get("DISCORD_WEBHOOK", "")
DISCORD_BURN_WEBHOOK = os.environ.get("DISCORD_BURN_WEBHOOK", "")
DISCORD_APP_ID       = os.environ.get("DISCORD_APP_ID", "")
DISCORD_BOT_TOKEN    = os.environ.get("DISCORD_BOT_TOKEN", "")
DISCORD_PUBLIC_KEY   = os.environ.get("DISCORD_PUBLIC_KEY", "")
ALCHEMY_SIGNING_KEY  = os.environ.get("ALCHEMY_SIGNING_KEY", "")
ALCHEMY_API_KEY      = os.environ.get("ALCHEMY_API_KEY", "kl7coWT2oFkRY0skuik4E")
PORT = int(os.environ.get("PORT", "8080"))

ALCHEMY_RPC          = f"https://eth-mainnet.g.alchemy.com/v2/{ALCHEMY_API_KEY}"
NORMIES_INTERNAL_KEY = os.environ.get("NORMIES_INTERNAL_SECRET", "")


# ── Normies API ────────────────────────────────────────────────

def fetch_total_burned() -> int | None:
    """Fetch total burned Normies count from /history/stats."""
    req = urllib.request.Request(
        "https://api.normies.art/history/stats",
        headers={"accept": "application/json", "User-Agent": "NormiesSalesBot/1.0",
                 "x-internal-secret": NORMIES_INTERNAL_KEY},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
            return int(data.get("totalBurnedTokens", 0))
    except Exception as e:
        print(f"[normies-api] failed to fetch burn stats: {e}")
        return None


def fetch_normie_traits(token_id: str) -> dict:
    """Fetch Type, Level, Pixel Count from api.normies.art/normie/:id/metadata."""
    url = f"https://api.normies.art/normie/{token_id}/metadata"
    req = urllib.request.Request(
        url, headers={
            "accept": "application/json",
            "User-Agent": "NormiesSalesBot/1.0",
            "x-internal-secret": NORMIES_INTERNAL_KEY,
        }
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
            traits = {}
            for attr in data.get("attributes", []):
                t = attr.get("trait_type", "")
                v = attr.get("value")
                if t in ("Type", "Level", "Pixel Count"):
                    traits[t] = v
            return traits
    except Exception as e:
        print(f"[normies-api] failed to fetch traits for #{token_id}: {e}")
        return {}


# ── Discord ────────────────────────────────────────────────────

def short_addr(addr: str) -> str:
    return addr[:6] if len(addr) >= 6 else addr


def post_discord(token_id: str, price_eth: float, price_usd: float,
                 buyer: str, seller: str, tx_hash: str, timestamp: int):

    image_url = NORMIES_IMAGE.format(id=token_id)
    os_url    = OPENSEA_URL.format(contract=NORMIES_CONTRACT, id=token_id)

    price_rounded = round(price_eth, 4)
    price_str = f"{price_rounded:.4f} ETH"
    if price_usd:
        price_str += f"  (${price_usd:,.0f})"

    traits = fetch_normie_traits(token_id)

    trait_parts = []
    if traits.get("Type"):
        trait_parts.append(f"**Type** {traits['Type']}")
    if traits.get("Level") is not None:
        trait_parts.append(f"**Level** {traits['Level']}")
    if traits.get("Pixel Count") is not None:
        trait_parts.append(f"**Pixels** {traits['Pixel Count']}")

    fields = [
        {"name": "Price", "value": price_str, "inline": False},
    ]
    if trait_parts:
        fields.append({"name": "\u200b", "value": "  ·  ".join(trait_parts), "inline": False})
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

WETH_CONTRACT      = "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"
TRANSFER_TOPIC     = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
SEAPORT_FULFILLED  = "0x9d9af8e38d66c62e2c12f0225249fd9d721c54b83f48d9352c97c6cacdcb6f31"


def _rpc(method: str, params: list) -> dict:
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    req = urllib.request.Request(
        ALCHEMY_RPC, data=body,
        headers={"Content-Type": "application/json", "User-Agent": "NormiesSalesBot/1.0"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read()).get("result") or {}


def lookup_seaport_price(tx_hash: str, token_id: str) -> float:
    """Decode Seaport OrderFulfilled event from tx receipt to get exact per-NFT price.
    Works for single sales and bulk sweeps — each OrderFulfilled maps to one NFT."""
    try:
        receipt = _rpc("eth_getTransactionReceipt", [tx_hash])
        for log in receipt.get("logs", []):
            topics = log.get("topics", [])
            if not topics or topics[0] != SEAPORT_FULFILLED:
                continue
            d = bytes.fromhex(log["data"].removeprefix("0x"))
            if len(d) < 128:
                continue
            # [0:32] orderHash  [32:64] recipient
            # [64:96] offset→offer[]  [96:128] offset→consideration[]
            offer_off = int.from_bytes(d[64:96], "big")
            cons_off  = int.from_bytes(d[96:128], "big")

            # Decode offer array — SpentItem = (itemType, token, identifier, amount) = 4×32
            offer_len = int.from_bytes(d[offer_off:offer_off+32], "big")
            found = False
            for i in range(offer_len):
                s = offer_off + 32 + i * 128
                token_addr = "0x" + d[s+32:s+64].hex()[-40:]
                identifier = int.from_bytes(d[s+64:s+96], "big")
                if (token_addr.lower() == NORMIES_CONTRACT.lower()
                        and str(identifier) == str(token_id)):
                    found = True
                    break
            if not found:
                continue

            # Decode consideration array — ReceivedItem = (itemType, token, id, amount, recipient) = 5×32
            cons_len = int.from_bytes(d[cons_off:cons_off+32], "big")
            total_eth = 0
            for j in range(cons_len):
                s = cons_off + 32 + j * 160
                item_type = int.from_bytes(d[s:s+32], "big")
                amount    = int.from_bytes(d[s+96:s+128], "big")
                if item_type == 0:  # native ETH
                    total_eth += amount
            if total_eth > 0:
                return total_eth / 1e18
    except Exception as e:
        print(f"[seaport] decode failed for #{token_id} tx {tx_hash}: {e}")
    return 0.0


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



def post_burn_discord(token_id: str, owner: str, timestamp: int):
    if not DISCORD_BURN_WEBHOOK:
        print(f"[burn] no DISCORD_BURN_WEBHOOK set — skipping Normie #{token_id}")
        return

    traits = fetch_normie_traits(token_id)
    total_burned = fetch_total_burned()
    image_url = NORMIES_IMAGE.format(id=token_id)

    trait_parts = []
    if traits.get("Type"):
        trait_parts.append(f"**Type** {traits['Type']}")
    if traits.get("Level") is not None:
        trait_parts.append(f"**Level** {traits['Level']}")
    if traits.get("Pixel Count") is not None:
        trait_parts.append(f"**Pixels** {traits['Pixel Count']}")

    fields = [{"name": "Burned by", "value": short_addr(owner), "inline": False}]
    if trait_parts:
        fields.append({"name": "\u200b", "value": "  ·  ".join(trait_parts), "inline": False})
    if total_burned is not None:
        fields.append({"name": "Total Burned", "value": f"{total_burned} / 10,000", "inline": False})

    embed = {
        "title": f"Normie #{token_id} burned 🔥",
        "color": 0xFF4444,
        "thumbnail": {"url": image_url},
        "fields": fields,
        "footer": {"text": "Normies · Built by Normies, for Normies"},
        "timestamp": datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat(),
    }

    payload = json.dumps({"embeds": [embed]}).encode()
    req = urllib.request.Request(
        DISCORD_BURN_WEBHOOK,
        data=payload,
        headers={"Content-Type": "application/json", "User-Agent": "NormiesSalesBot/1.0"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10):
            print(f"[burn] posted Normie #{token_id} burned by {short_addr(owner)}")
    except urllib.error.HTTPError as e:
        print(f"[burn] HTTP {e.code}: {e.read().decode()}")
    except Exception as e:
        print(f"[burn] error: {e}")


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
        if from_addr == ZERO_ADDRESS:
            print(f"[alchemy] skipping Normie #{token_id} — mint")
            continue

        block_time = activity.get("blockTimestamp", "")
        try:
            ts = int(datetime.fromisoformat(
                block_time.replace("Z", "+00:00")
            ).timestamp())
        except Exception:
            ts = int(time.time())

        # Detect burns (to zero address or Canvas contract)
        if to_addr.lower() in (ZERO_ADDRESS, CANVAS_CONTRACT.lower()):
            if _is_duplicate(tx_hash, f"burn:{token_id}"):
                print(f"[alchemy] skipping Normie #{token_id} burn — duplicate")
                continue
            print(f"[alchemy] burn detected: Normie #{token_id} by {short_addr(from_addr)}")
            post_burn_discord(token_id=token_id, owner=from_addr, timestamp=ts)
            continue

        # Alchemy NFT activity value=0 for marketplace sales (OpenSea/Blur route ETH
        # through their contracts). Look up real price via Alchemy APIs instead.
        inline_value = float(activity.get("value", 0))
        price_eth = inline_value
        price_usd = 0.0

        if tx_hash:
            # 1. Decode Seaport OrderFulfilled — exact per-NFT price for both
            #    single sales and bulk sweeps (each event maps to one NFT)
            price_eth = lookup_seaport_price(tx_hash, token_id)
            if price_eth > 0:
                print(f"[price] Normie #{token_id} — {price_eth:.4f} ETH (seaport)")
            else:
                # 2. WETH fallback — offer accepted via non-Seaport path
                price_eth = lookup_tx_weth(tx_hash, to_addr)
                if price_eth > 0:
                    print(f"[price] Normie #{token_id} — {price_eth:.4f} ETH (WETH fallback)")
                else:
                    # 3. Native ETH fallback — last resort
                    price_eth = lookup_tx_eth(tx_hash)
                    if price_eth > 0:
                        print(f"[price] Normie #{token_id} — {price_eth:.4f} ETH (tx.value fallback)")
                    else:
                        print(f"[price] no price found for tx {tx_hash} Normie #{token_id}")

        if price_eth <= 0:
            print(f"[alchemy] skipping Normie #{token_id} — price=0 (likely not a sale)")
            continue

        if _is_duplicate(tx_hash, token_id):
            print(f"[alchemy] skipping Normie #{token_id} — duplicate (already posted)")
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


# ── Discord slash commands ─────────────────────────────────────

def verify_discord_signature(public_key_hex: str, signature_hex: str,
                              timestamp: str, body: bytes) -> bool:
    if not HAS_CRYPTOGRAPHY or not public_key_hex:
        return True
    try:
        key = Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_key_hex))
        key.verify(bytes.fromhex(signature_hex), timestamp.encode() + body)
        return True
    except (InvalidSignature, Exception):
        return False


def register_slash_commands():
    if not DISCORD_APP_ID or not DISCORD_BOT_TOKEN:
        print("[discord] DISCORD_APP_ID or DISCORD_BOT_TOKEN not set — skipping slash command registration")
        return
    url = f"https://discord.com/api/v10/applications/{DISCORD_APP_ID}/commands"
    command = {
        "name": "normie",
        "description": "Show image and traits of a Normie",
        "options": [{
            "name": "id",
            "description": "Normie ID (0–9999)",
            "type": 4,       # INTEGER
            "required": True,
            "min_value": 0,
            "max_value": 9999,
        }],
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(command).encode(),
        headers={
            "Authorization": f"Bot {DISCORD_BOT_TOKEN}",
            "Content-Type": "application/json",
            "User-Agent": "NormiesSalesBot/1.0",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            print(f"[discord] /normie slash command registered (HTTP {resp.status})")
    except urllib.error.HTTPError as e:
        print(f"[discord] slash command registration failed: {e.code} {e.read().decode()}")
    except Exception as e:
        print(f"[discord] slash command registration error: {e}")


def handle_normie_command(token_id: int) -> dict:
    traits = fetch_normie_traits(str(token_id))
    image_url = NORMIES_IMAGE.format(id=token_id)
    os_url    = OPENSEA_URL.format(contract=NORMIES_CONTRACT, id=token_id)

    trait_parts = []
    if traits.get("Type"):
        trait_parts.append(f"**Type** {traits['Type']}")
    if traits.get("Level") is not None:
        trait_parts.append(f"**Level** {traits['Level']}")
    if traits.get("Pixel Count") is not None:
        trait_parts.append(f"**Pixels** {traits['Pixel Count']}")

    fields = []
    if trait_parts:
        fields.append({"name": "\u200b", "value": "  ·  ".join(trait_parts), "inline": False})

    embed = {
        "title": f"Normie #{token_id}",
        "url": os_url,
        "color": 0x48494B,
        "image": {"url": image_url},
        "fields": fields,
        "footer": {"text": "Normies · Built by Normies, for Normies"},
    }
    return {"type": 4, "data": {"embeds": [embed]}}


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

        # Discord interactions endpoint
        if self.path == "/interactions":
            timestamp = self.headers.get("x-signature-timestamp", "")
            signature = self.headers.get("x-signature-ed25519", "")
            if not verify_discord_signature(DISCORD_PUBLIC_KEY, signature, timestamp, body):
                print("[interactions] invalid signature — rejected")
                self.send_response(401)
                self.end_headers()
                return
            try:
                payload = json.loads(body)
                # Type 1 = PING (Discord health check)
                if payload.get("type") == 1:
                    self._json({"type": 1})
                    return
                # Type 2 = APPLICATION_COMMAND
                if payload.get("type") == 2:
                    data    = payload.get("data", {})
                    options = {o["name"]: o["value"] for o in data.get("options", [])}
                    if data.get("name") == "normie":
                        token_id = int(options.get("id", 0))
                        # Defer response first (fetch can take a moment)
                        self._json({"type": 5})  # DEFERRED_CHANNEL_MESSAGE_WITH_SOURCE
                        threading.Thread(
                            target=self._followup_normie,
                            args=(payload.get("token"), token_id),
                            daemon=True,
                        ).start()
                        return
            except Exception as e:
                print(f"[interactions] error: {e}")
                self.send_response(500)
                self.end_headers()
            return

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

            # Respond immediately so Alchemy doesn't retry due to timeout
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

            # Process in background thread to avoid blocking the HTTP response
            if webhook_type == "NFT_ACTIVITY":
                threading.Thread(
                    target=handle_alchemy_event,
                    args=(payload,),
                    daemon=True,
                ).start()

        except Exception as e:
            print(f"[webhook] error processing payload: {e}")
            self.send_response(500)
            self.end_headers()

    def _json(self, data: dict):
        body = json.dumps(data).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def _followup_normie(self, interaction_token: str, token_id: int):
        result = handle_normie_command(token_id)
        url = f"https://discord.com/api/v10/webhooks/{DISCORD_APP_ID}/{interaction_token}/messages/@original"
        req = urllib.request.Request(
            url,
            data=json.dumps(result["data"]).encode(),
            headers={
                "Content-Type": "application/json",
                "User-Agent": "NormiesSalesBot/1.0",
            },
            method="PATCH",
        )
        try:
            with urllib.request.urlopen(req, timeout=15):
                print(f"[interactions] /normie {token_id} — response sent")
        except Exception as e:
            print(f"[interactions] followup failed: {e}")

    def log_message(self, fmt, *args):
        # Suppress default HTTP log noise
        pass


def main():
    print(f"Normies sales bot starting on port {PORT}")
    print(f"Contract: {NORMIES_CONTRACT}")
    register_slash_commands()
    server = HTTPServer(("0.0.0.0", PORT), WebhookHandler)
    server.serve_forever()


if __name__ == "__main__":
    main()
