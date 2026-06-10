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
# Customers endpoint — used to enrich orders with the assigned sales rep (and
# state/license when present), since the orders feed doesn't carry them.
CUSTOMERS_ENDPOINT = os.getenv("LEAFLINK_CUSTOMERS_ENDPOINT", "/api/v2/customers/")
# Customers carry the assigned rep as the `managers` field — a list of user IDs.
# These endpoints (tried in order) resolve those IDs to rep names.
USERS_ENDPOINTS = [e.strip() for e in os.getenv(
    "LEAFLINK_USERS_ENDPOINTS",
    "/api/v2/users/,/api/v2/company-staff/,/api/v2/staff/,/api/v2/team-members/"
).split(",") if e.strip()]
API_KEY = os.getenv("LEAFLINK_API_KEY", "")

# Keep only line items whose product name / brand contains this (case-insensitive).
BRAND_FILTER = os.getenv("LEAFLINK_BRAND", "Chill Medicated")
INCLUDE_CHILDREN = os.getenv("LEAFLINK_INCLUDE_CHILDREN", "line_items")

# Keep only orders on/after this date (matched against created_on). Blank = no floor.
FROM_DATE = os.getenv("LEAFLINK_FROM_DATE", "2025-05-01")

# Restrict to one company by seller id. Medfarms - 100 Shafer Processing = 9105.
# The App token is already scoped to a single company, but this enforces it
# explicitly. Blank = no company filter.
SELLER_ID = os.getenv("LEAFLINK_SELLER_ID", "9105")

# Statuses to exclude — a "products sold" report does not count these. Comma-sep.
EXCLUDE_STATUSES = [s.strip().lower() for s in
                    os.getenv("LEAFLINK_EXCLUDE_STATUSES", "Cancelled,Rejected").split(",")
                    if s.strip()]
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
    # Robust GET: retry on 429 and transient 5xx so a single hiccup never
    # silently truncates the pull.
    last = None
    for attempt in range(6):
        try:
            resp = requests.get(url, headers=auth_headers(), params=params, timeout=120)
        except requests.RequestException as e:
            last = e
            time.sleep(5 * (attempt + 1))
            continue
        if resp.status_code == 429 or 500 <= resp.status_code < 600:
            wait = 5 * (attempt + 1)
            print(f"  {resp.status_code} — backing off {wait}s (attempt {attempt+1})")
            time.sleep(wait)
            last = resp
            continue
        return resp
    if isinstance(last, requests.Response):
        return last
    raise RuntimeError(f"request failed after retries: {last}")


def _month_windows(from_date, to_date):
    """Yield (gte, lt) date-string pairs, one calendar month each, covering the
    range. Windowing keeps every request well under LeafLink's ~6,050-result
    pagination ceiling, so the full history is retrievable."""
    y, m = int(from_date[:4]), int(from_date[5:7])
    windows = []
    while True:
        start = f"{y:04d}-{m:02d}-01"
        ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)
        nxt = f"{ny:04d}-{nm:02d}-01"
        windows.append((start, nxt))
        if start[:7] >= to_date[:7]:
            break
        y, m = ny, nm
    return windows


def _fetch_window(gte, lt):
    """Page through one date window completely, following `next`."""
    params = {"page_size": PAGE_SIZE, "page": 1,
              "created_on__gte": gte, "created_on__lt": lt,
              "ordering": "created_on"}
    if INCLUDE_CHILDREN:
        params["include_children"] = INCLUDE_CHILDREN
    url = f"{API_BASE}{ENDPOINT}"
    out, page, reported = [], 0, None
    resp = _get(url, params)
    if resp.status_code == 400:
        # Date filter rejected — surface clearly rather than silently mis-pulling.
        print(f"ERROR 400 on window {gte}..{lt}: {resp.text[:200]}")
        sys.exit(1)
    if resp.status_code == 401:
        print("ERROR: 401 Unauthorized — key missing/wrong/revoked."); sys.exit(1)
    if resp.status_code == 403:
        print("ERROR: 403 Forbidden — app lacks Orders read permission."); sys.exit(1)
    if resp.status_code != 200:
        print(f"ERROR: status {resp.status_code}\n{resp.text[:300]}"); sys.exit(1)
    while True:
        data = resp.json()
        if reported is None:
            reported = data.get("count")
        batch = data.get("results", data if isinstance(data, list) else [])
        out.extend(batch)
        page += 1
        nxt = data.get("next") if isinstance(data, dict) else None
        if not nxt or (MAX_PAGES and page >= MAX_PAGES):
            break
        resp = _get(nxt, None)
        if resp.status_code != 200:
            # Don't return a half window — fail loudly so partial data is never committed.
            print(f"ERROR: window {gte}..{lt} page fetch returned {resp.status_code}; aborting.")
            sys.exit(1)
    # If a single month ever exceeds the cap, warn (would need finer windows).
    if reported and len(out) < reported:
        print(f"  WARNING: window {gte}..{lt} returned {len(out)} of {reported} "
              "(month exceeds pagination cap — needs finer windows).")
    return out, reported


def fetch_all() -> list:
    if not API_KEY:
        print("ERROR: LEAFLINK_API_KEY is empty.")
        sys.exit(1)
    today = datetime.now().strftime("%Y-%m-%d")
    start = FROM_DATE or "2025-05-01"
    windows = _month_windows(start, today)
    print(f"Pulling {len(windows)} monthly windows ({start} .. {today})")
    seen, orders = set(), []
    for gte, lt in windows:
        batch, reported = _fetch_window(gte, lt)
        new = 0
        for o in batch:
            key = o.get("number") or o.get("id")
            if key in seen:
                continue
            seen.add(key)
            orders.append(o)
            new += 1
        print(f"  {gte} -> {lt}: {len(batch)} fetched ({new} new) | total {len(orders)}")
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


def _payment_status(o):
    if o.get("paid"):
        return "Paid"
    due = _date_key(o.get("payment_due_date"))
    today = datetime.now().strftime("%Y-%m-%d")
    if due and due < today:
        return "Overdue"
    return "Unpaid"


# --- Customer enrichment ----------------------------------------------------
# LeafLink order payloads don't include the sales rep / buyer state. The
# customers endpoint does (it's the seller's customer list). We pull it once,
# build lookup maps keyed by customer id AND normalized buyer name, and stamp
# each order. All best-effort + fail-safe: any failure leaves fields blank.

def _person_name(v):
    """Name of a rep/person. Handles dicts with full_name/name or first+last."""
    if isinstance(v, list):
        return ", ".join(p for p in (_person_name(x) for x in v) if p)
    if isinstance(v, dict):
        n = _first(v, "full_name", "name", "display_name", "title")
        if n:
            return n
        fn = str(v.get("first_name") or "").strip()
        ln = str(v.get("last_name") or "").strip()
        combo = (fn + " " + ln).strip()
        if combo:
            return combo
        return _first(v, "email", "username") or ""
    if isinstance(v, str):
        return v
    return ""


def _manager_ids(c):
    """Assigned rep(s) on a customer = the `managers` field (list of user IDs).
    Also tolerate a few alternate shapes / names."""
    if not isinstance(c, dict):
        return []
    ids = []
    for k in ("managers", "sales_reps", "assigned_sales_reps", "account_managers",
              "sales_rep", "account_manager"):
        v = c.get(k)
        if v in (None, "", []):
            continue
        items = v if isinstance(v, list) else [v]
        for x in items:
            if isinstance(x, bool):
                continue
            if isinstance(x, int):
                ids.append(str(x))
            elif isinstance(x, dict):
                xid = _first(x, "id", "pk", "user_id")
                nm = _person_name(x)
                ids.append(nm if nm else (str(xid) if xid is not None else ""))
            elif isinstance(x, str) and x.strip():
                ids.append(x.strip())
    out, seen = [], set()
    for i in ids:
        if i and i not in seen:
            seen.add(i)
            out.append(i)
    return out


def fetch_users():
    """Build {user_id(str): name} by paging the users endpoint(s). {} on failure."""
    if not API_KEY:
        return {}
    out = {}
    for ep in USERS_ENDPOINTS:
        url = f"{API_BASE}{ep}"
        resp = _get(url, {"page_size": PAGE_SIZE, "page": 1})
        if resp.status_code != 200:
            continue
        try:
            data = resp.json()
        except Exception:
            continue
        added = 0
        while True:
            batch = data.get("results", data if isinstance(data, list) else [])
            for u in batch:
                if not isinstance(u, dict):
                    continue
                uid = _first(u, "id", "pk", "user_id")
                nm = _person_name(u)
                if uid is not None and nm:
                    out.setdefault(str(uid), nm)
                    added += 1
            nxt = data.get("next") if isinstance(data, dict) else None
            if not nxt:
                break
            resp = _get(nxt, None)
            if resp.status_code != 200:
                break
            try:
                data = resp.json()
            except Exception:
                break
        if added:
            print(f"  resolved {added} user name(s) from {ep}")
    return out


def _state_of(c):
    if not isinstance(c, dict):
        return ""
    for k in ("state", "state_code", "region"):
        v = c.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    for k in ("buyer", "company", "address", "billing_address", "shipping_address",
              "default_address", "location"):
        sub = c.get(k)
        if isinstance(sub, dict):
            for kk in ("state", "state_code", "region"):
                v = sub.get(kk)
                if isinstance(v, str) and v.strip():
                    return v.strip()
    return ""


def _license_of(c):
    if not isinstance(c, dict):
        return ""
    for k in ("license", "license_number", "license_no"):
        v = c.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    for sk in ("buyer", "company"):
        sub = c.get(sk)
        if isinstance(sub, dict):
            for k in ("license", "license_number", "license_no"):
                v = sub.get(k)
                if isinstance(v, str) and v.strip():
                    return v.strip()
    return ""


def _city_of(c):
    if not isinstance(c, dict):
        return ""
    v = c.get("city")
    if isinstance(v, str) and v.strip():
        return v.strip()
    for sk in ("address", "delivery_address", "corporate_address", "buyer", "company"):
        sub = c.get(sk)
        if isinstance(sub, dict):
            v = sub.get("city")
            if isinstance(v, str) and v.strip():
                return v.strip()
    return ""


def _cust_names(c):
    """All plausible name strings for a customer, normalized for matching."""
    names = set()
    for nm in (_name_of(c), _name_of(c.get("buyer") if isinstance(c, dict) else None),
               _first(c, "name", "display_name", "company_name") if isinstance(c, dict) else None):
        if nm:
            names.add(str(nm).strip().lower())
    return names


def fetch_customers():
    """Page through the customers endpoint. Returns [] on any failure (fail-safe)."""
    if not API_KEY:
        return []
    url = f"{API_BASE}{CUSTOMERS_ENDPOINT}"
    resp = _get(url, {"page_size": PAGE_SIZE, "page": 1})
    if resp.status_code == 403:
        print("  NOTE: 403 on customers endpoint — the App lacks 'Customers' read "
              "permission. Add it in LeafLink (Settings > Applications) to enable "
              "sales-rep enrichment. Skipping for now.")
        return []
    if resp.status_code == 404:
        print(f"  NOTE: 404 on {CUSTOMERS_ENDPOINT} — endpoint path may differ; "
              "set LEAFLINK_CUSTOMERS_ENDPOINT. Skipping rep enrichment.")
        return []
    if resp.status_code != 200:
        print(f"  NOTE: customers endpoint returned {resp.status_code}; "
              "skipping rep enrichment.")
        return []
    out = []
    while True:
        data = resp.json()
        batch = data.get("results", data if isinstance(data, list) else [])
        out.extend(batch)
        nxt = data.get("next") if isinstance(data, dict) else None
        if not nxt:
            break
        resp = _get(nxt, None)
        if resp.status_code != 200:
            break
    return out


def build_enrichment(customers, user_map):
    """Build id/name -> rep/state/license maps from the customer list.
    Rep = the customer's `managers` (user IDs) resolved via user_map; falls back
    to a 'Rep #<id>' label when a name can't be resolved."""
    enrich = {"rep_by_id": {}, "rep_by_name": {},
              "state_by_id": {}, "state_by_name": {},
              "lic_by_id": {}, "lic_by_name": {},
              "city_by_id": {}, "city_by_name": {}}
    reps_found = 0
    for c in customers:
        cid = _first(c, "id", "pk", "customer_id")
        mids = _manager_ids(c)
        rep = ", ".join(user_map.get(i) or (i if not i.isdigit() else f"Rep #{i}")
                        for i in mids) if mids else ""
        state, lic, city = _state_of(c), _license_of(c), _city_of(c)
        if rep:
            reps_found += 1
        if cid is not None:
            if rep:
                enrich["rep_by_id"].setdefault(str(cid), rep)
            if state:
                enrich["state_by_id"].setdefault(str(cid), state)
            if lic:
                enrich["lic_by_id"].setdefault(str(cid), lic)
            if city:
                enrich["city_by_id"].setdefault(str(cid), city)
        for nm in _cust_names(c):
            if rep:
                enrich["rep_by_name"].setdefault(nm, rep)
            if state:
                enrich["state_by_name"].setdefault(nm, state)
            if lic:
                enrich["lic_by_name"].setdefault(nm, lic)
            if city:
                enrich["city_by_name"].setdefault(nm, city)
    enrich["_reps_found"] = reps_found
    return enrich


def _order_customer_keys(o):
    """Return (customer_id_str, normalized_name) for joining to the enrich maps."""
    cust = o.get("customer")
    cid = ""
    if isinstance(cust, dict):
        v = _first(cust, "id", "pk", "customer_id")
        cid = str(v) if v is not None else ""
    elif isinstance(cust, (int, str)) and str(cust).strip():
        cid = str(cust).strip()
    nm = (_name_of(cust) or _name_of(o.get("buyer")) or "").strip().lower()
    return cid, nm


def flatten(orders, brand_q, from_date="", seller_id="", enrich=None):
    enrich = enrich or {}
    rep_by_id = enrich.get("rep_by_id", {}); rep_by_name = enrich.get("rep_by_name", {})
    state_by_id = enrich.get("state_by_id", {}); state_by_name = enrich.get("state_by_name", {})
    lic_by_id = enrich.get("lic_by_id", {}); lic_by_name = enrich.get("lic_by_name", {})
    city_by_id = enrich.get("city_by_id", {}); city_by_name = enrich.get("city_by_name", {})
    rows = []
    seller_ids, brand_ids_seen = set(), set()
    matched = total_lines = skipped_old = skipped_company = skipped_status = 0
    rep_orders = 0
    recon_order_total = recon_net_total = recon_gross_total = 0.0
    brand_q = (brand_q or "").strip().lower()
    from_date = (from_date or "").strip()
    seller_id = str(seller_id or "").strip()

    for o in orders:
        s = o.get("seller")
        sid = s if not isinstance(s, dict) else s.get("id")
        if s is not None:
            seller_ids.add(sid)
        for b in (o.get("brand_ids") or []):
            brand_ids_seen.add(b)

        # Company filter: only Medfarms (seller id).
        if seller_id and str(sid) != seller_id:
            skipped_company += 1
            continue

        # Status filter: drop non-sold statuses (Cancelled/Rejected by default).
        ostatus = _first(o, "status", "order_status")
        if EXCLUDE_STATUSES and str(ostatus or "").lower() in EXCLUDE_STATUSES:
            skipped_status += 1
            continue

        order_date = _first(o, "created_on", "created", "order_placed_date", "date")
        if from_date:
            dk = _date_key(order_date)
            if dk and dk < from_date:
                skipped_old += 1
                continue

        order_total = _amount(o.get("total"))
        if order_total is not None:
            recon_order_total += order_total

        # Enrich: assigned sales rep / state / license from the customers endpoint,
        # joined by customer id first, then normalized buyer name.
        _cid, _cnm = _order_customer_keys(o)
        _rep = (rep_by_id.get(_cid) or rep_by_name.get(_cnm)
                or _name_of(_first(o, "sales_rep", "sales_reps")) or "")
        _state = state_by_id.get(_cid) or state_by_name.get(_cnm) or ""
        _lic = lic_by_id.get(_cid) or lic_by_name.get(_cnm) or ""
        _city = city_by_id.get(_cid) or city_by_name.get(_cnm) or ""
        if _rep:
            rep_orders += 1

        common = {
            "order_number": _first(o, "short_id", "number", "id"),
            "order_uid": _first(o, "number", "id"),
            "order_status": _first(o, "status", "order_status"),
            "order_date": order_date,
            "delivery_date": _first(o, "ship_date", "delivery_date"),
            "buyer_name": _name_of(o.get("customer")) or _name_of(o.get("buyer")),
            "buyer_state": _state,
            "buyer_city": _city,
            "buyer_license": _lic,
            "sales_rep": _rep,
            "payment_status": _payment_status(o),
            "paid": bool(o.get("paid")),
            "payment_term": o.get("payment_term") or "",
            "order_total": order_total,
        }

        # Pass 1: parse every line, compute gross, and the order's gross subtotal.
        parsed = []
        order_gross = 0.0
        for li in (o.get("line_items") or o.get("lineitems") or []):
            if not isinstance(li, dict):
                continue
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
            gross = (eff or 0) * sold_units
            order_gross += gross
            parsed.append((li, prod, pname, brand, qty, mult, sold_units,
                           unit_price, sale_price, gross))

        # Net allocation: scale each line's gross so the order's lines sum to the
        # actual order total (distributes order-level discount/tax/shipping). This
        # makes the dashboard's revenue tie out to LeafLink's order totals exactly.
        scale = (order_total / order_gross) if (order_total is not None and order_gross) else 1.0
        recon_gross_total += order_gross
        recon_net_total += order_gross * scale

        # Pass 2: emit rows for brand matches.
        for (li, prod, pname, brand, qty, mult, sold_units,
             unit_price, sale_price, gross) in parsed:
            total_lines += 1
            if brand_q and brand_q not in brand.lower():
                continue
            matched += 1
            net = gross * scale
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
                "gross": round(gross, 2),
                "discount": round(gross - net, 2),
                "revenue": round(net, 2),
            })

    stats = {
        "seller_ids": sorted(x for x in seller_ids if x is not None),
        "brand_ids": sorted(brand_ids_seen),
        "matched": matched, "total_lines": total_lines, "skipped_old": skipped_old,
        "skipped_company": skipped_company, "skipped_status": skipped_status,
        "rep_orders": rep_orders,
        "recon_order_total": round(recon_order_total, 2),
        "recon_net_total": round(recon_net_total, 2),
        "recon_gross_total": round(recon_gross_total, 2),
    }
    return rows, stats


# ----------------------------------------------------------------------------
def main():
    print(f"Pulling {ENDPOINT} from {API_BASE}"
          + (f"  (created_on__gte={FROM_DATE})" if (SERVER_DATE_FILTER and FROM_DATE) else ""))
    orders = fetch_all()
    print(f"Fetched {len(orders)} orders.")

    # Enrich with sales rep (and state/license when available) from customers.
    print(f"Fetching customers from {CUSTOMERS_ENDPOINT} for sales-rep enrichment...")
    customers = fetch_customers()
    print("Resolving rep names from users endpoint(s)...")
    user_map = fetch_users() if customers else {}
    enrich = build_enrichment(customers, user_map)
    print(f"Users resolved to names: {len(user_map)} | Customers fetched: {len(customers)} "
          f"| customers with a rep: {enrich.get('_reps_found', 0)}")
    if customers and enrich.get("_reps_found", 0) == 0:
        print("  No 'managers' on customers. First customer keys: "
              f"{sorted((customers[0] or {}).keys())}")

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

    rows, st = flatten(orders, BRAND_FILTER, FROM_DATE, SELLER_ID, enrich)

    if BRAND_FILTER.strip() and st["matched"] == 0 and st["total_lines"] > 0:
        print(f"WARNING: brand '{BRAND_FILTER}' matched 0 of {st['total_lines']} lines.")
        print("Keeping ALL rows so you still get data — check the product-name field.")
        rows, st = flatten(orders, "", FROM_DATE, SELLER_ID, enrich)

    print(f"\nSeller id(s) seen: {st['seller_ids']}  (Medfarms = 9105)")
    print(f"Company filter: seller {SELLER_ID or '(none)'}  ->  skipped {st['skipped_company']} other-company orders")
    print(f"Status filter: excluded {EXCLUDE_STATUSES or '(none)'}  ->  skipped {st['skipped_status']} orders")
    print(f"Brand id(s) in data:  {st['brand_ids']}   (Chill Medicated = 2425)")
    print(f"Date floor: {FROM_DATE or '(none)'}  ->  skipped {st['skipped_old']} older orders")
    print(f"Line items: {st['total_lines']} in range -> {st['matched']} kept (brand '{BRAND_FILTER}')")
    print(f"Sales-rep enrichment: {st['rep_orders']} orders matched a rep "
          f"(0 = customers endpoint had no rep data / not permitted)")
    # After net allocation, sum of net should equal sum of order totals (ratio ~1.000).
    ot, nt, gt = st["recon_order_total"], st["recon_net_total"], st["recon_gross_total"]
    ratio = (nt / ot) if ot else 0
    print(f"RECONCILE (all brands): net ${nt:,.2f} vs order totals ${ot:,.2f} "
          f"(ratio {ratio:.4f}; should be ~1.0000)")
    print(f"  gross (list price) was ${gt:,.2f} -> order-level discounts/adj ${gt-nt:,.2f}")

    payload = {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "source": "leaflink",
        "seller_ids": st["seller_ids"],
        "company_filter": SELLER_ID,
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
