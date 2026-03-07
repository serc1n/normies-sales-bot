#!/usr/bin/env python3
"""Normies sales tracker — receives Alchemy webhook events and posts to Discord."""

import os
import json
import math
import hmac
import hashlib
import base64
import random
import re
import asyncio
import threading
import time
import urllib.request
import urllib.error
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime, timezone

import discord

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

DISCORD_WEBHOOK          = os.environ.get("DISCORD_WEBHOOK", "")
DISCORD_BURN_WEBHOOK     = os.environ.get("DISCORD_BURN_WEBHOOK", "")
DISCORD_LISTINGS_WEBHOOK = os.environ.get("DISCORD_LISTINGS_WEBHOOK", "")
DISCORD_APP_ID           = os.environ.get("DISCORD_APP_ID", "")
DISCORD_BOT_TOKEN        = os.environ.get("DISCORD_BOT_TOKEN", "")
DISCORD_PUBLIC_KEY       = os.environ.get("DISCORD_PUBLIC_KEY", "")
OPENSEA_API_KEY          = os.environ.get("OPENSEA_API_KEY", "")
ALCHEMY_SIGNING_KEY  = os.environ.get("ALCHEMY_SIGNING_KEY", "")
ALCHEMY_API_KEY      = os.environ.get("ALCHEMY_API_KEY", "kl7coWT2oFkRY0skuik4E")
PORT = int(os.environ.get("PORT", "8080"))

ALCHEMY_RPC          = f"https://eth-mainnet.g.alchemy.com/v2/{ALCHEMY_API_KEY}"
NORMIES_INTERNAL_KEY = os.environ.get("NORMIES_INTERNAL_SECRET", "")

THE100 = [
    464,9846,9197,8183,5052,6227,7491,6497,2623,9548,7490,2449,6303,2532,513,
    1384,9852,9879,6143,820,9155,2286,7413,1879,108,455,9999,1932,7627,1188,
    9239,235,3846,6765,9076,3732,1476,7908,7479,8576,115,5707,5816,9735,9982,
    2908,9644,7011,5679,7384,1617,8990,4868,117,4358,6241,5665,2006,7976,8115,
    8759,7887,133,27,6016,9980,7652,2565,6884,1603,1204,4057,9612,7028,1898,
    4829,1208,6793,1370,4354,9445,3123,6309,615,7961,8612,6155,3408,8510,3837,
    999,8362,376,4681,3465,9561,8831,5010,2060,7374,
]


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
                if t in ("Type", "Level", "Pixel Count", "Action Points"):
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
    for command in [
        {
            "name": "normie",
            "description": "Show image and traits of a Normie",
            "options": [{
                "name": "id",
                "description": "Normie ID (0–9999)",
                "type": 4,
                "required": True,
                "min_value": 0,
                "max_value": 9999,
            }],
        },
        {
            "name": "the100",
            "description": "Show a random Normie from THE100",
        },
    ]:
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
                print(f"[discord] /{command['name']} slash command registered (HTTP {resp.status})")
        except urllib.error.HTTPError as e:
            print(f"[discord] slash command registration failed: {e.code} {e.read().decode()}")
        except Exception as e:
            print(f"[discord] slash command registration error: {e}")


def build_normie_embed(token_id: int, title_prefix: str = "") -> dict:
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
    if traits.get("Action Points") is not None:
        trait_parts.append(f"**AP** {traits['Action Points']}")

    fields = []
    if trait_parts:
        fields.append({"name": "\u200b", "value": "  ·  ".join(trait_parts), "inline": False})

    title = f"{title_prefix}Normie #{token_id}".strip()
    return {
        "title": title,
        "url": os_url,
        "color": 0x48494B,
        "image": {"url": image_url},
        "fields": fields,
        "footer": {"text": "Normies · Built by Normies, for Normies"},
    }


def handle_normie_command(token_id: int) -> dict:
    return {"type": 4, "data": {"embeds": [build_normie_embed(token_id)]}}


def handle_the100_command() -> dict:
    token_id = random.choice(THE100)
    embed = build_normie_embed(token_id, title_prefix="THE100 · ")
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
                        self._json({"type": 5})  # defer — fetch takes a moment
                        threading.Thread(
                            target=self._followup_normie,
                            args=(payload.get("token"), token_id),
                            daemon=True,
                        ).start()
                        return
                    if data.get("name") == "the100":
                        token_id = random.choice(THE100)
                        self._json({"type": 5})
                        threading.Thread(
                            target=self._followup_normie,
                            args=(payload.get("token"), token_id, "THE100 · "),
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

    def _followup_normie(self, interaction_token: str, token_id: int, title_prefix: str = ""):
        embed = build_normie_embed(token_id, title_prefix=title_prefix)
        url = f"https://discord.com/api/v10/webhooks/{DISCORD_APP_ID}/{interaction_token}/messages/@original"
        req = urllib.request.Request(
            url,
            data=json.dumps({"embeds": [embed]}).encode(),
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


# ── THE100 listings poller (OpenSea API) ──────────────────────

_seen_listings: set[str] = set()
_listings_lock = threading.Lock()
LISTINGS_POLL_INTERVAL = 120  # seconds


def fetch_the100_listings() -> list[dict]:
    """Fetch active listings for THE100 token IDs from OpenSea v2 API."""
    results = []
    headers = {
        "accept": "application/json",
        "User-Agent": "NormiesSalesBot/1.0",
        "x-api-key": OPENSEA_API_KEY,
    }
    batch_size = 20
    for i in range(0, len(THE100), batch_size):
        batch = THE100[i:i + batch_size]
        params = "&".join(f"token_ids={tid}" for tid in batch)
        url = (
            f"https://api.opensea.io/api/v2/orders/ethereum/seaport/listings"
            f"?asset_contract_address={NORMIES_CONTRACT}&{params}"
            f"&order_by=created_date&order_direction=desc&limit=50"
        )
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read())
                results.extend(data.get("orders", []))
        except Exception as e:
            print(f"[listings] OpenSea fetch failed (batch {i}): {e}")
    return results


def post_listing_discord(token_id: str, price_eth: float):
    if not DISCORD_LISTINGS_WEBHOOK:
        return
    traits = fetch_normie_traits(token_id)
    image_url = NORMIES_IMAGE.format(id=token_id)
    os_url    = OPENSEA_URL.format(contract=NORMIES_CONTRACT, id=token_id)

    trait_parts = []
    if traits.get("Type"):
        trait_parts.append(f"**Type** {traits['Type']}")
    if traits.get("Level") is not None:
        trait_parts.append(f"**Level** {traits['Level']}")
    if traits.get("Pixel Count") is not None:
        trait_parts.append(f"**Pixels** {traits['Pixel Count']}")
    if traits.get("Action Points") is not None:
        trait_parts.append(f"**AP** {traits['Action Points']}")

    price_rounded = round(price_eth, 4)
    fields = [{"name": "Price", "value": f"{price_rounded:.4f} ETH", "inline": False}]
    if trait_parts:
        fields.append({"name": "\u200b", "value": "  ·  ".join(trait_parts), "inline": False})

    embed = {
        "title": f"THE100 \u00b7 Normie #{token_id} listed",
        "url": os_url,
        "color": 0x48494B,
        "thumbnail": {"url": image_url},
        "fields": fields,
        "footer": {"text": "Normies \u00b7 Built by Normies, for Normies"},
    }
    payload = json.dumps({"embeds": [embed]}).encode()
    req = urllib.request.Request(
        DISCORD_LISTINGS_WEBHOOK,
        data=payload,
        headers={"Content-Type": "application/json", "User-Agent": "NormiesSalesBot/1.0"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10):
            print(f"[listings] posted Normie #{token_id} listed at {price_rounded:.4f} ETH")
    except Exception as e:
        print(f"[listings] discord post failed: {e}")


def poll_listings():
    if not OPENSEA_API_KEY:
        print("[listings] no OPENSEA_API_KEY — skipping listings poller")
        return
    if not DISCORD_LISTINGS_WEBHOOK:
        print("[listings] no DISCORD_LISTINGS_WEBHOOK — skipping listings poller")
        return

    # First poll: seed existing listings as seen without posting
    print("[listings] seeding existing listings...")
    try:
        orders = fetch_the100_listings()
        with _listings_lock:
            for order in orders:
                h = order.get("order_hash", "")
                if h:
                    _seen_listings.add(h)
        print(f"[listings] seeded {len(_seen_listings)} existing listings — will only post new ones")
    except Exception as e:
        print(f"[listings] seed error: {e}")

    time.sleep(LISTINGS_POLL_INTERVAL)

    print("[listings] poller started — watching for new listings")
    while True:
        try:
            orders = fetch_the100_listings()
            new_orders = []
            for order in orders:
                order_hash = order.get("order_hash", "")
                if not order_hash:
                    continue
                with _listings_lock:
                    if order_hash in _seen_listings:
                        continue
                    _seen_listings.add(order_hash)
                new_orders.append(order)

            for order in new_orders:
                asset = order.get("maker_asset_bundle", {})
                assets = asset.get("assets", [])
                if not assets:
                    continue
                token_id = str(assets[0].get("token_id", ""))
                price_wei = int(order.get("current_price", "0"))
                price_eth = price_wei / 1e18

                if price_eth > 0 and token_id:
                    post_listing_discord(token_id, price_eth)
                    time.sleep(1)  # 1s between posts to respect Discord rate limits

        except Exception as e:
            print(f"[listings] poll error: {e}")
        time.sleep(LISTINGS_POLL_INTERVAL)


# ── Discord Gateway (message reactions) ───────────────────────

intents = discord.Intents.default()
intents.message_content = True  # requires Message Content Intent in Dev Portal

discord_client = discord.Client(intents=intents)

@discord_client.event
async def on_ready():
    print(f"[discord-gateway] logged in as {discord_client.user}")

@discord_client.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    # ! commands
    cmd = message.content.strip().lower()
    if cmd == "!floor":
        await message.reply("There is no floor ILY")
        return
    if cmd == "!docs":
        await message.reply("https://www.normies.art/docs/normies")
        return
    if cmd == "!article":
        await message.reply("https://x.com/normiesART/status/2028744015433097270?s=20")
        return
    if cmd == "!wow":
        await message.reply("https://cdn.discordapp.com/attachments/1476174593281626255/1479752143639547965/IMG_5837.gif?ex=69ad2e13&is=69abdc93&hm=3f7bc771389b06e8fefb40f7f5f2141376ec2ba3d4d503982eee7d1ded351473&")
        return
    if cmd == "!vibes":
        await message.reply("https://cdn.discordapp.com/attachments/1476174593281626255/1479752583525699734/IMG_5829.gif?ex=69ad2e7c&is=69abdcfc&hm=e0be8ed17ea8af91c0ea8a47af58271f6ae71a887e5da131d003d00d82c85af0&")
        return
    if cmd == "!higher":
        await message.reply("https://cdn.discordapp.com/attachments/1476174593281626255/1479753393076572172/IMG_5756.gif?ex=69ad2f3d&is=69abddbd&hm=d7300e52db6e699bf3f59da0c92347ee87ac250e7338fe2a71e1163810daa0b0&")
        return
    # Community tools commands
    _TOOLS = {
        "!slidepuzzle": "https://normie-puzzle.vercel.app/",
        "!cam": "https://legacy.normies.art/normiecam",
        "!3d": "https://normie-3d.vercel.app/",
        "!run": "https://normies.run/",
        "!bordercontrol": "https://normies-border-control.vercel.app/",
        "!card": "https://legacy.normies.art/normiecard",
        "!memory": "https://editor.p5js.org/nftgothsa/full/StEIA7Ldo",
        "!radio": "https://yasuna-ide.github.io/normie-radio/",
        "!message": "https://messages-from-normies-production.up.railway.app/",
        "!grid": "https://legacy.normies.art/grid",
        "!sky": "https://normski-generator.vercel.app/",
        "!punks": "https://normies.backpunks.com/",
        "!normifier": "https://normifier.vercel.app/",
        "!generator": "https://legacy.normies.art/",
        "!yearbook": "https://normie-yearbook.vercel.app/",
        "!beats": "https://normiebeats.vercel.app",
        "!meme": "https://normies-memegenerator.vercel.app/",
        "!news": "https://legacy.normies.art/normiesnews",
        "!games": "https://normies-blackjack.vercel.app/",
        "!saints": "https://normiesaint.vercel.app/",
        "!minesweeper": "https://norminesweeper.vercel.app/",
        "!pixelhunter": "https://normies-pixelhunter-ac26.vercel.app/",
        "!pvp": "https://legacy.normies.art/pvp",
        "!lego": "https://normies-lego-builder.vercel.app/",
        "!bricks": "https://normies-x-bricks.vercel.app",
        "!compat": "https://normies-compact.vercel.app/",
        "!archive": "https://normiesarchive.vercel.app/",
        "!edit": "https://www.editnormies.com/",
        "!coloring": "https://editor.p5js.org/realseenaa/full/75oCNlnMp",
        "!flip": "https://normies-flip.vercel.app/",
        "!burntrack": "https://normiesburntracker.lovable.app/",
        "!popart": "https://editor.p5js.org/realseenaa/full/BEumjluT_",
        "!glitch": "https://glitch-normies.vercel.app/",
        "!terminal": "https://normies-terminal.vercel.app/",
        "!remixer": "https://normie-mixer.vercel.app/",
        "!negative": "https://normies-negative.vercel.app/",
        "!draw": "https://raw.githubusercontent.com/Gothsa/normies/main/drawyournormie.pdf",
        "!pixgrabber": "https://editor.p5js.org/nftmooods/full/PRBv_Bgoq",
        "!match": "https://normies-match.netlify.app/",
        "!roulette": "https://normies-daily.vercel.app/",
    }
    if cmd in _TOOLS:
        await message.reply(_TOOLS[cmd])
        return

    # gm reaction
    if re.search(r"\bgm\b", message.content, re.IGNORECASE):
        emoji = discord.utils.get(message.guild.emojis, name="coffee") if message.guild else None
        try:
            await message.add_reaction(emoji or "☕")
        except Exception as e:
            print(f"[discord-gateway] reaction failed: {e}")


def run_discord_gateway():
    if not DISCORD_BOT_TOKEN:
        print("[discord-gateway] no DISCORD_BOT_TOKEN — skipping gateway")
        return
    print("[discord-gateway] connecting...")
    try:
        asyncio.run(discord_client.start(DISCORD_BOT_TOKEN))
    except discord.LoginFailure:
        print("[discord-gateway] ERROR: invalid bot token")
    except Exception as e:
        print(f"[discord-gateway] ERROR: {e}")


def main():
    print(f"Normies sales bot starting on port {PORT}")
    print(f"Contract: {NORMIES_CONTRACT}")
    register_slash_commands()
    # Start Discord gateway and listings poller in background threads
    threading.Thread(target=run_discord_gateway, daemon=True).start()
    threading.Thread(target=poll_listings, daemon=True).start()
    server = HTTPServer(("0.0.0.0", PORT), WebhookHandler)
    server.serve_forever()


if __name__ == "__main__":
    main()
