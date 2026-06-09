"""
LeafLink Sales Report Scraper  (Chill Medicated / Medfarms)
-----------------------------------------------------------
Pulls Orders Received from the LeafLink Marketplace V2 API, flattens to
per-line-item rows, keeps May-2025-onward + the Chill Medicated brand, trims
to the fields the dashboard needs, and writes a compact sales_data.json.

Field mapping is based on the real LeafLink response:
  - order:  number (uuid), short_id (display #), created_on (date),
            status, customer.display_name (buyer), total.amount, brand_ids
  - line:   ordered_unit_price.amount, sale_price.amount, quantity,
            unit_multiplier, is_sample, frozen_data.product.{name,sku,
            product_line_name,price,...}
  - revenue per line = effective_price * (quantity / unit_multiplier)
"""

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

API_BASE = os.getenv("LEAFLINK_API_BASE", "https://www.leaflink.com")
ENDPOINT = os.getenv("LEAFLINK_ENDPOINT", "/api/v2/orders-received/")
API_KEY = os.getenv("LEAFLINK_API_KEY", "")

# Keep only line items whose product name / brand contains this (case-insensitive).
BRAND_FILTER = os.getenv("LEAFLINK_BRAND", "Chill Medicated")
INCLUDE_CHILDREN = os.getenv("LEAFLINK_INCLUDE_CHILDREN", "line_items")

# Keep only orders on/after this date (matched against created_on). Blank = no floor.
FROM_DATE = os.getenv("LEAFLINK_FROM_DATE", "2025-05-01")
# Send the date floor to the server too (created_on__gte) to avoid pulling all
# history. If LeafLink rejects it (400), the scraper drops it and falls back to
# client-side filtering automatically. Set "0" to disable.
SERVER_DATE_FILTER = os.getenv("LEAFLINK_SERVER_DATE_FILTER", "1") != "0"

PAGE_SIZE = int(os.getenv("LEAFLINK_PAGE_SIZE", "500"))
MAX_PAGES = int(os.getenv("LEAFLINK_MAX_PAGES", "0"))

OUTPUT_FILE = Path(__file__).parent / "sales_data.json"


# ----------------------------------------------------------------------------
def auth_headers() -> dict:
    return {
        "Authorization": f"App {API_KEY}",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "chill-sales-dashboard",
    }


def _get(url, params):
    for attempt in range(4):
        resp = requests.get(url, headers=auth_headers(), params=params, timeout=120)
        if resp.status_code == 429:
            wait = 5 * (attempt + 1)
            print(f"  rate limited (429) — backing off {wait}s")
            time.sleep(wait)
            continue
        return resp
    return resp


def fetch_all() -> list:
    if not API_KEY:
        print("ERROR: LEAFLINK_API_KEY is empty.")
        sys.exit(1)

    base_params = {"page_size": PAGE_SIZE, "page": 1}
    if INCLUDE_CHILDREN:
        base_params["include_children"] = INCLUDE_CHILDREN

    use_server_date = SERVER_DATE_FILTER and bool(FROM_DATE)
    if use_server_date:
        base_params["created_on__gte"] = FROM_DATE

    url = f"{API_BASE}{ENDPOINT}"

    # First request, with a one-time fallback if the date param is rejected.
    resp = _get(url, base_params)
    if resp.status_code == 400 and use_server_date:
        print("NOTE: server rejected created_on__gte — falling back to client-side date filter.")
        base_params.pop("created_on__gte", None)
        use_server_date = False
        resp = _get(url, base_params)

    if resp.status_code == 401:
        print("ERROR: 401 Unauthorized — key missing/wrong/revoked."); sys.exit(1)
    if resp.status_code == 403:
        print("ERROR: 403 Forbidden — app lacks Orders read permission."); sys.exit(1)
    if resp.status_code != 200:
        print(f"ERROR: status {resp.status_code}\n{resp.text[:500]}"); sys.exit(1)

    orders, page = [], 0
    while True:
        data = resp.json()
        batch = data.get("results", data if isinstance(data, list) else [])
        orders.extend(batch)
        page += 1
        total = data.get("count", "?")
        print(f"  page {page}: +{len(batch)} orders (running {len(orders)} / {total})")

        nxt = data.get("next") if isinstance(data, dict) else None
        if not nxt or (MAX_PAGES and page >= MAX_PAGES):
            break
        resp = _get(nxt, None)
        if resp.status_code != 200:
            print(f"  stopping: page fetch returned {resp.status_code}")
            break
    return orders


# ----------------------------------------------------------------------------
def _first(d, *keys):
    for k in keys:
        if isinstance(d, dict) and d.get(k) not in (None, "", []):
            return d.get(k)
    return None


def _amount(v):
    if isinstance(v, dict):
        v = v.get("amount")
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _name_of(v):
    if isinstance(v, list):
        return ", ".join(p for p in (_name_of(x) for x in v) if p)
    if isinstance(v, dict):
        return _first(v, "name", "title", "display_name", "full_name") or ""
    if isinstance(v, str):
        return v
    return ""


def _date_key(s):
    if not s:
        return None
    s = str(s)
    if len(s) >= 10 and s[4] == "-" and s[7] == "-":
        return s[:10]
    return None


def _frozen_product(li):
    fd = li.get("frozen_data")
    prod = fd.get("product") if isinstance(fd, dict) else None
    return prod if isinstance(prod, dict) else {}


def flatten(orders, brand_q, from_date=""):
    rows = []
    seller_ids, brand_ids_seen = set(), set()
    matched = total_lines = skipped_old = 0
    recon_order_total = recon_line_total = 0.0
    brand_q = (brand_q or "").strip().lower()
    from_date = (from_date or "").strip()

    for o in orders:
        s = o.get("seller")
        if s is not None:
            seller_ids.add(s if not isinstance(s, dict) else s.get("id"))
        for b in (o.get("brand_ids") or []):
            brand_ids_seen.add(b)

        order_date = _first(o, "created_on", "created", "order_placed_date", "date")
        if from_date:
            dk = _date_key(order_date)
            if dk and dk < from_date:
                skipped_old += 1
                continue

        ot = _amount(o.get("total"))
        if ot is not None:
            recon_order_total += ot

        common = {
            "order_number": _first(o, "short_id", "number", "id"),
            "order_uid": _first(o, "number", "id"),
            "order_status": _first(o, "status", "order_status"),
            "order_date": order_date,
            "delivery_date": _first(o, "ship_date", "delivery_date"),
            "buyer_name": _name_of(o.get("customer")) or _name_of(o.get("buyer")),
            "buyer_state": "",   # not in order payload; enrich via customers endpoint later
            "buyer_license": "",
            "sales_rep": _name_of(_first(o, "sales_rep", "sales_reps")),
        }

        for li in (o.get("line_items") or o.get("lineitems") or []):
            if not isinstance(li, dict):
                continue
            total_lines += 1
            prod = _frozen_product(li)
            pname = prod.get("name") or _first(li, "product_name") or ""
            brand = (_name_of(prod.get("brand")) or _name_of(prod.get("brand_name")) or pname)

            qty = _amount(li.get("quantity")) or 0.0
            mult = _amount(li.get("unit_multiplier")) or 1.0
            sold_units = qty / mult if mult else qty
            unit_price = _amount(li.get("ordered_unit_price"))
            sale_price = _amount(li.get("sale_price"))
            on_sale = bool(li.get("on_sale")) or (sale_price or 0) > 0
            eff = sale_price if (on_sale and (sale_price or 0) > 0) else unit_price
            line_rev = (eff or 0) * sold_units
            recon_line_total += line_rev

            if brand_q and brand_q not in brand.lower():
                continue
            matched += 1
            rows.append({
                **common,
                "brand": (_name_of(prod.get("brand")) or _name_of(prod.get("brand_name"))
                          or (BRAND_FILTER if brand_q else "")),
                "product_name": pname,
                "product_sku": prod.get("sku") or "",
                "product_line": prod.get("product_line_name") or "",
                "product_category": _name_of(prod.get("category")) or prod.get("product_line_name") or "",
                "product_type": prod.get("product_line_name") or "",
                "quantity": qty,
                "unit_multiplier": mult,
                "units_sold": sold_units,
                "unit_price": unit_price,
                "sale_price": sale_price,
                "is_sample": bool(li.get("is_sample")),
                "line_total": round(line_rev, 2),
            })

    stats = {
        "seller_ids": sorted(x for x in seller_ids if x is not None),
        "brand_ids": sorted(brand_ids_seen),
        "matched": matched, "total_lines": total_lines, "skipped_old": skipped_old,
        "recon_order_total": round(recon_order_total, 2),
        "recon_line_total": round(recon_line_total, 2),
    }
    return rows, stats


# ----------------------------------------------------------------------------
def main():
    print(f"Pulling {ENDPOINT} from {API_BASE}"
          + (f"  (created_on__gte={FROM_DATE})" if (SERVER_DATE_FILTER and FROM_DATE) else ""))
    orders = fetch_all()
    print(f"Fetched {len(orders)} orders.")

    if orders:
        first = orders[0]
        order_lite = {k: v for k, v in first.items() if k not in ("line_items", "lineitems")}
        print("\n--- FIRST ORDER (line_items removed) ---")
        print(json.dumps(order_lite, default=str)[:2500])
        lis = first.get("line_items") or first.get("lineitems") or []
        if lis and isinstance(lis[0], dict):
            print("\n--- FIRST LINE ITEM ---")
            print(json.dumps(lis[0], default=str)[:2500])
        print("--- end sample ---\n")

    rows, st = flatten(orders, BRAND_FILTER, FROM_DATE)

    if BRAND_FILTER.strip() and st["matched"] == 0 and st["total_lines"] > 0:
        print(f"WARNING: brand '{BRAND_FILTER}' matched 0 of {st['total_lines']} lines.")
        print("Keeping ALL rows so you still get data — check the product-name field.")
        rows, st = flatten(orders, "", FROM_DATE)

    print(f"\nSeller id(s) in data: {st['seller_ids']}  (Medfarms expected)")
    print(f"Brand id(s) in data:  {st['brand_ids']}   (Chill Medicated = 2425)")
    print(f"Date floor: {FROM_DATE or '(none)'}  ->  skipped {st['skipped_old']} older orders")
    print(f"Line items: {st['total_lines']} in range -> {st['matched']} kept (brand '{BRAND_FILTER}')")
    # Reconciliation: across ALL brands in range, line revenue should ~= order totals.
    ot, lt = st["recon_order_total"], st["recon_line_total"]
    ratio = (lt / ot) if ot else 0
    print(f"RECONCILE (all brands): sum line revenue ${lt:,.2f} vs sum order totals "
          f"${ot:,.2f}  (ratio {ratio:.3f}; ~1.000 means the unit math is correct)")

    payload = {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "source": "leaflink",
        "seller_ids": st["seller_ids"],
        "brand_filter": BRAND_FILTER,
        "from_date": FROM_DATE,
        "order_count": len(orders),
        "row_count": len(rows),
        "rows": rows,
    }
    OUTPUT_FILE.write_text(json.dumps(payload, separators=(",", ":"), default=str))
    size_mb = OUTPUT_FILE.stat().st_size / 1e6
    print(f"\nSaved -> {OUTPUT_FILE} ({size_mb:.2f} MB, {len(rows)} rows)")


if __name__ == "__main__":
    main()
