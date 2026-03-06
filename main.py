#!/usr/bin/env python3
"""Normies sales tracker bot — posts Discord notifications for every sale."""

import os
import time
import json
import urllib.request
import urllib.error
from datetime import datetime, timezone

NORMIES_CONTRACT = "0x9Eb6E2025B64f340691e424b7fe7022fFDE12438"
RESERVOIR_API    = "https://api.reservoir.tools"
NORMIES_IMAGE    = "https://api.normies.art/normie/{id}/image.png"
OPENSEA_URL      = "https://opensea.io/assets/ethereum/{contract}/{id}"
ETHERSCAN_TX     = "https://etherscan.io/tx/{tx}"

DISCORD_WEBHOOK  = os.environ["DISCORD_WEBHOOK"]
RESERVOIR_KEY    = os.environ.get("RESERVOIR_API_KEY", "")
POLL_INTERVAL    = int(os.environ.get("POLL_INTERVAL", "30"))  # seconds

STATE_FILE = "last_sale.json"


# ── State (last seen sale timestamp) ──────────────────────────

def load_last_timestamp() -> int:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f).get("ts", 0)
    return int(time.time()) - 3600  # default: look back 1 hour on first run


def save_last_timestamp(ts: int):
    with open(STATE_FILE, "w") as f:
        json.dump({"ts": ts}, f)


# ── Reservoir API ──────────────────────────────────────────────

def fetch_sales(after_ts: int) -> list[dict]:
    url = (
        f"{RESERVOIR_API}/sales/v6"
        f"?contract={NORMIES_CONTRACT}"
        f"&startTimestamp={after_ts + 1}"
        f"&sortBy=time"
        f"&limit=20"
    )
    headers = {"accept": "application/json"}
    if RESERVOIR_KEY:
        headers["x-api-key"] = RESERVOIR_KEY

    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
            return data.get("sales", [])
    except urllib.error.HTTPError as e:
        print(f"[reservoir] HTTP {e.code}: {e.reason}")
        return []
    except Exception as e:
        print(f"[reservoir] error: {e}")
        return []


# ── Discord ────────────────────────────────────────────────────

def post_discord(sale: dict):
    token_id  = sale.get("token", {}).get("tokenId", "?")
    price_eth = sale.get("price", {}).get("amount", {}).get("native", 0)
    price_usd = sale.get("price", {}).get("amount", {}).get("usd", 0)
    buyer     = sale.get("buyer", "unknown")
    seller    = sale.get("seller", "unknown")
    tx_hash   = sale.get("txHash", "")
    ts        = sale.get("timestamp", int(time.time()))

    image_url = NORMIES_IMAGE.format(id=token_id)
    os_url    = OPENSEA_URL.format(contract=NORMIES_CONTRACT, id=token_id)
    tx_url    = ETHERSCAN_TX.format(tx=tx_hash) if tx_hash else None

    def short(addr: str) -> str:
        return f"{addr[:6]}...{addr[-4:]}" if len(addr) > 10 else addr

    price_str = f"{price_eth:.4f} ETH"
    if price_usd:
        price_str += f"  (${price_usd:,.0f})"

    embed = {
        "title": f"Normie #{token_id} sold",
        "url": os_url,
        "color": 0x48494B,
        "thumbnail": {"url": image_url},
        "fields": [
            {"name": "Price",  "value": price_str,       "inline": True},
            {"name": "Seller", "value": short(seller),   "inline": True},
            {"name": "Buyer",  "value": short(buyer),    "inline": True},
        ],
        "footer": {
            "text": "Normies · Built by Normies, for Normies",
        },
        "timestamp": datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(),
    }

    if tx_url:
        embed["fields"].append({"name": "Tx", "value": f"[etherscan]({tx_url})", "inline": True})

    payload = json.dumps({"embeds": [embed]}).encode()
    req = urllib.request.Request(
        DISCORD_WEBHOOK,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            print(f"[discord] posted Normie #{token_id} sale ({price_eth:.4f} ETH)")
    except urllib.error.HTTPError as e:
        print(f"[discord] HTTP {e.code}: {e.read().decode()}")
    except Exception as e:
        print(f"[discord] error: {e}")


# ── Main loop ──────────────────────────────────────────────────

def main():
    print(f"Normies sales bot starting — polling every {POLL_INTERVAL}s")
    print(f"Contract: {NORMIES_CONTRACT}")

    last_ts = load_last_timestamp()
    print(f"Watching sales after {datetime.fromtimestamp(last_ts).strftime('%Y-%m-%d %H:%M:%S')}")

    while True:
        try:
            sales = fetch_sales(last_ts)

            if sales:
                # Reservoir returns newest first — process oldest first
                for sale in reversed(sales):
                    post_discord(sale)
                    time.sleep(1)  # avoid Discord rate limit

                newest_ts = max(s.get("timestamp", 0) for s in sales)
                if newest_ts > last_ts:
                    last_ts = newest_ts
                    save_last_timestamp(last_ts)
            else:
                print(f"[{datetime.now().strftime('%H:%M:%S')}] no new sales")

        except Exception as e:
            print(f"[loop] unexpected error: {e}")

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
