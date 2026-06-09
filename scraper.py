"""
LeafLink Sales Report Scraper
-----------------------------
Pulls your "Orders Received" from the LeafLink API and saves the result as
sales_data.json for the dashboard.

Unlike the Apex scraper (which used a browser session cookie that expired every
few weeks), this uses a long-lived LeafLink **Application API key**. Create the
app under  Settings > Applications > Create an App  with READ access to Orders
(and Products / Customers), then copy the key.

USAGE:
    1. Put your key in .env:   LEAFLINK_API_KEY="your-key-here"
    2. Run:                    python scraper.py
    3. Output:                 ./sales_data.json   (the dashboard reads this)

In GitHub Actions the key comes from the LEAFLINK_API_KEY repository secret.
"""

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

# ----------------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------------
load_dotenv()

API_BASE = os.getenv("LEAFLINK_API_BASE", "https://api.leaflink.com")

# The Application API key from Settings > Applications. NEVER commit this.
# Auth scheme is:  Authorization: App <key>   (note the single space after App)
API_KEY = os.getenv("LEAFLINK_API_KEY", "")

# Which list endpoint to pull. "orders-received" is the seller's incoming orders
# (i.e. your sales). Each order carries its line items, buyer, status and dates.
# NOTE: the LeafLink API lives under the /api/v2/ namespace.
ENDPOINT = os.getenv("LEAFLINK_ENDPOINT", "/api/v2/orders-received/")

# Pull the line items nested inside each order (product, qty, prices).
# Comma-separate to include more, e.g. "line_items,customer".
INCLUDE_CHILDREN = os.getenv("LEAFLINK_INCLUDE_CHILDREN", "line_items")

# Page size. LeafLink allows 1..500 (anything else 404s); defaults to 50.
PAGE_SIZE = int(os.getenv("LEAFLINK_PAGE_SIZE", "500"))

# Optional: stop paginating after this many pages (safety valve). 0 = no cap.
MAX_PAGES = int(os.getenv("LEAFLINK_MAX_PAGES", "0"))

OUTPUT_FILE = Path(__file__).parent / "sales_data.json"


def auth_headers() -> dict:
    return {
        "Authorization": f"App {API_KEY}",
        "Accept": "application/json",
        "Content-Type": "application/json",
        # LeafLink best practices ask integrations to identify themselves.
        "User-Agent": "chill-sales-dashboard",
    }


def _handle_status(resp: requests.Response) -> None:
    """Translate the common failure codes into a clear message and exit."""
    if resp.status_code == 401:
        print("ERROR: 401 Unauthorized.")
        print("The API key is missing, wrong, or was revoked. Re-check the key")
        print("copied from Settings > Applications and the LEAFLINK_API_KEY value.")
        sys.exit(1)
    if resp.status_code == 403:
        print("ERROR: 403 Forbidden.")
        print("The key is valid but the app lacks permission for this data.")
        print("In Settings > Applications, give the app READ access to Orders")
        print("(and Products / Customers), then try again.")
        sys.exit(1)
    if resp.status_code == 404:
        print(f"ERROR: 404 Not Found for {resp.url}")
        print("Check LEAFLINK_ENDPOINT. Note LeafLink is picky about trailing")
        print("slashes — try toggling the trailing '/' if this persists.")
        sys.exit(1)
    if resp.status_code == 429:
        # Rate limited (300 req/min). Caller handles the retry; this is a guard.
        return
    if resp.status_code != 200:
        print(f"ERROR: unexpected status {resp.status_code}")
        print(resp.text[:500])
        sys.exit(1)


def fetch_all() -> list:
    """GET the list endpoint and follow LeafLink's `next` pagination links."""
    if not API_KEY:
        print("ERROR: LEAFLINK_API_KEY is empty.")
        print("Add it to .env (local) or the repo secret (GitHub Actions).")
        sys.exit(1)

    # First request: build the URL with LeafLink's pagination params. Subsequent
    # requests follow the fully-formed `next` URL it returns, so we don't re-pass.
    url = f"{API_BASE}{ENDPOINT}"
    params = {"page_size": PAGE_SIZE, "page": 1}
    if INCLUDE_CHILDREN:
        params["include_children"] = INCLUDE_CHILDREN

    results = []
    page = 0
    while url:
        for attempt in range(4):
            resp = requests.get(
                url,
                headers=auth_headers(),
                params=params if page == 0 else None,
                timeout=60,
            )
            if resp.status_code == 429:
                wait = 5 * (attempt + 1)
                print(f"  rate limited (429) — backing off {wait}s")
                time.sleep(wait)
                continue
            break
        _handle_status(resp)

        data = resp.json()
        # List endpoints return {count, next, previous, results:[...]}.
        # Be tolerant in case a raw list ever comes back.
        batch = data.get("results", data if isinstance(data, list) else [])
        results.extend(batch)
        page += 1
        total = data.get("count", "?")
        print(f"  page {page}: +{len(batch)} rows (running total {len(results)} / {total})")

        url = data.get("next") if isinstance(data, dict) else None
        if MAX_PAGES and page >= MAX_PAGES:
            print(f"  reached MAX_PAGES={MAX_PAGES}, stopping early")
            break

    return results


def main():
    print(f"Pulling {ENDPOINT} from {API_BASE} ...")
    rows = fetch_all()
    print(f"Fetched {len(rows)} rows.")

    payload = {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "source": "leaflink",
        "endpoint": ENDPOINT,
        "row_count": len(rows),
        "rows": rows,
    }
    OUTPUT_FILE.write_text(json.dumps(payload, indent=2, default=str))
    print(f"Saved -> {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
