"""
CashCow eCommerce control dashboard (server side).

The store itself runs on Ecwid. This module reads the Ecwid store through its API and adds what
Ecwid doesn't do: it matches every order's ZIP code to the nearest supplier warehouse (where the
item ships from) and the nearest local logistics warehouse (the hub that handles the area), then
groups open orders into delivery routes by hub.

Render settings (on the CashCow server):
  ECWID_STORE_ID        the Ecwid store number
  ECWID_SECRET_TOKEN    the custom app's secret token (secret_...)
  ECOM_ADMIN_EMAILS     CashCow account email(s) allowed in (defaults to MAIL_ADMIN_EMAILS)
  ECOM_MANUAL_SUPPLIERS suppliers you order from by hand (default: CROV)
  ECOM_AUTO_SUPPLIERS   suppliers whose Ecwid app sends them orders itself (default: Printful)
  ANTHROPIC_API_KEY     turns on the "Ask Claude" tab (key from console.anthropic.com)
  ECOM_CLAUDE_MODEL     optional; which Claude model answers (default: claude-haiku-4-5-20251001)

Database: ecom.sql, then ecom2.sql (adds ecom_settings and order tracking columns).
Daily email uses the Gmail settings the server already has (LEAD_GMAIL_ADDRESS, LEAD_GMAIL_APP_PASSWORD).
ZIP locations: ecom_zips.json, from the "zipcodes" project by Dav Glass (BSD license).
"""
import asyncio
import json
import math
import smtplib
import os
import re
import time
from datetime import datetime, timedelta
from email.message import EmailMessage
from email.utils import formataddr
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import httpx
from fastapi import HTTPException, Request
from pydantic import BaseModel

ECWID_STORE_ID = (os.environ.get("ECWID_STORE_ID") or "").strip()
ECWID_SECRET_TOKEN = (os.environ.get("ECWID_SECRET_TOKEN") or "").strip()
ECOM_ADMIN_EMAILS = {e.strip().lower() for e in (os.environ.get("ECOM_ADMIN_EMAILS") or os.environ.get("MAIL_ADMIN_EMAILS") or "").split(",") if e.strip()}

# Suppliers with no automatic ordering: their items are always flagged "order by hand".
# Suppliers whose Ecwid app takes orders on its own (they never go on the To-order list).
AUTO_SUPPLIERS = {x.strip().lower() for x in (os.environ.get("ECOM_AUTO_SUPPLIERS") or "Printful").split(",") if x.strip()}
MANUAL_SUPPLIERS = {x.strip().lower() for x in (os.environ.get("ECOM_MANUAL_SUPPLIERS") or "CROV").split(",") if x.strip()}
ANTHROPIC_API_KEY = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
ECOM_CLAUDE_MODEL = (os.environ.get("ECOM_CLAUDE_MODEL") or "claude-haiku-4-5-20251001").strip()
_ask_log: dict = {}
# Claude credit tracking (Anthropic doesn't let apps read the balance, so we count what each answer costs).
PRICE_IN = float(os.environ.get("ECOM_CLAUDE_PRICE_IN") or 1.0)     # $ per million input tokens (Claude Haiku 4.5)
PRICE_OUT = float(os.environ.get("ECOM_CLAUDE_PRICE_OUT") or 5.0)   # $ per million output tokens
BILLING_URL = "https://console.anthropic.com/settings/billing"


async def note_claude_usage(data: dict = None, empty: bool = False):
    """Add one Claude answer's cost to the running total; remember if Anthropic said the balance is empty."""
    try:
        rows = await db("GET", "ecom_settings", params={"key": "eq._claude_spend", "select": "value"}) or []
        cur = dict((rows[0].get("value") if rows else None) or {})
        if data:
            u = data.get("usage") or {}
            cost = (u.get("input_tokens", 0) + u.get("cache_creation_input_tokens", 0)) * PRICE_IN / 1e6 \
                + u.get("cache_read_input_tokens", 0) * PRICE_IN * 0.1 / 1e6 + u.get("output_tokens", 0) * PRICE_OUT / 1e6
            cur["spent"] = round(float(cur.get("spent") or 0) + cost, 5)
            cur["calls"] = int(cur.get("calls") or 0) + 1
        if empty:
            cur["empty_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        await save_setting("_claude_spend", cur)
    except HTTPException:
        pass


def claude_out_of_credit(r) -> bool:
    try:
        msg = str(((r.json() or {}).get("error") or {}).get("message") or "").lower()
    except ValueError:
        msg = ""
    return r.status_code in (400, 402, 403) and ("credit balance" in msg or "billing" in msg)
ASK_LIMIT = 30   # questions per admin per hour

GMAIL = (os.environ.get("LEAD_GMAIL_ADDRESS") or "").strip()
GMAIL_PASSWORD = (os.environ.get("LEAD_GMAIL_APP_PASSWORD") or "").strip()
CJ_API_KEY = (os.environ.get("CJ_API_KEY") or "").strip()
TWILIO_READY = bool(os.environ.get("TWILIO_AUTH_TOKEN"))
TZ = ZoneInfo(os.environ.get("ECOM_TIMEZONE") or "America/Chicago")
DASHBOARD_URL = "https://cashcow-ecommerce.onrender.com"
TICK_SECONDS = 300

# The Automation tab's switches. "free" ones work now; the others are upgrades for later.
SWITCHES = [
    {"key": "auto_route", "label": "Check for new orders every 5 minutes", "group": "free", "default": True,
     "about": "New paid orders are matched to the nearest supplier warehouse and local hub, and land on your To-order list."},
    {"key": "auto_processing", "label": "Mark new paid orders \"Processing\" in Ecwid", "group": "free", "default": True,
     "about": "Customers see their order is being worked on. Needs the switch above."},
    {"key": "daily_email", "label": "Daily summary email at 7 AM", "group": "free", "default": True,
     "about": "New orders, what still needs ordering from suppliers, and anything stuck, sent to your admin email."},
    {"key": "auto_ribbons", "label": "\"Ships from\" ribbons on product pictures", "group": "free", "default": True,
     "about": "Each product gets a label like \"📍 Ships from Cranbury, NJ 08512\" from its supplier's warehouse, so nearby customers see it arrives fast. Checked every hour; ribbons you typed yourself in Ecwid are never changed."},
    {"key": "ask_actions", "label": "Ask Claude can set things up for you", "group": "free", "default": True,
     "about": "Claude suggests warehouses, SKU rules, products from photos and order updates. Nothing changes until you tap Confirm."},
    {"key": "cj_auto_order", "label": "Send CJ orders to CJ automatically", "group": "upgrade", "default": False, "coming": True,
     "about": "Orders for CJ items are placed with CJdropshipping and paid from your CJ balance. Needs CJ_API_KEY."},
    {"key": "cj_sync", "label": "Sync CJ tracking numbers and stock", "group": "upgrade", "default": False, "coming": True,
     "about": "Tracking goes to Ecwid (which emails the customer) and stock levels stay current. Needs CJ_API_KEY."},
    {"key": "ai_writer", "label": "AI writes listings for new products", "group": "upgrade", "default": False, "coming": True,
     "about": "Products added without a description get one written by Claude. A few cents each."},
    {"key": "hub_texts", "label": "Text each hub its delivery route", "group": "upgrade", "default": False, "coming": True,
     "about": "Morning text with the route link to each hub's phone, through your Twilio number."},
    {"key": "brevo_sync", "label": "Add customers to your Brevo email list", "group": "upgrade", "default": False, "coming": True,
     "about": "Customers who agree to emails at checkout, and new Candy Lady sellers, are added to your CashCow list in Brevo, ready for your next campaign. Free plan: 300 emails a day."},
    {"key": "channels", "label": "Sell everywhere through Ecwid's sales channels", "group": "channels", "default": True, "external": True,
     "about": "Facebook, Instagram, Google Shopping, TikTok, Amazon, eBay, Walmart, Pinterest and more. Turn each one on in Ecwid → Sales channels. Every channel's orders come into this dashboard automatically, labeled by channel."},
]


def switch_ready(key: str) -> str:
    """Empty string when the switch can be used, otherwise what's missing."""
    if key == "daily_email" and not (GMAIL and GMAIL_PASSWORD):
        return "Needs LEAD_GMAIL_ADDRESS and LEAD_GMAIL_APP_PASSWORD in Render."
    if key == "ask_actions" and not ANTHROPIC_API_KEY:
        return "Needs ANTHROPIC_API_KEY in Render."
    sw = next((x for x in SWITCHES if x["key"] == key), {})
    if sw.get("coming"):
        return "Coming in a later update."
    return ""

FULFILLMENT = ["AWAITING_PROCESSING", "PROCESSING", "SHIPPED", "OUT_FOR_DELIVERY", "DELIVERED",
               "READY_FOR_PICKUP", "WILL_NOT_DELIVER", "RETURNED"]
DONE = {"SHIPPED", "DELIVERED", "WILL_NOT_DELIVER", "RETURNED"}
_ctx: dict = {}
_cache: dict = {}
_zips: Optional[dict] = None


# ---------- ZIP codes and distance ----------
def zips() -> dict:
    global _zips
    if _zips is None:
        try:
            _zips = json.loads((Path(__file__).parent / "ecom_zips.json").read_text())
        except (OSError, ValueError):
            _zips = {}
    return _zips


def zip_point(z) -> Optional[dict]:
    z = re.sub(r"\D", "", str(z or ""))[:5]
    if len(z) != 5:
        return None
    v = zips().get(z)
    if not v:
        return None
    return {"zip": z, "lat": v[0], "lng": v[1], "city": v[2], "state": v[3]}


def miles(a, b) -> float:
    la1, lo1, la2, lo2 = map(math.radians, (a["lat"], a["lng"], b["lat"], b["lng"]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return round(3958.8 * 2 * math.asin(math.sqrt(h)), 1)


def est_days(mi: Optional[float]) -> str:
    if mi is None:
        return "unknown"
    if mi <= 150:
        return "1-2 days"
    if mi <= 500:
        return "2-3 days"
    if mi <= 1200:
        return "3-5 days"
    return "5-7 days"


def nearest(point, places):
    best = None
    for w in places:
        if w.get("lat") is None:
            continue
        d = miles(point, w)
        if best is None or d < best[1]:
            best = (w, d)
    return best


# ---------- Ecwid ----------
def ecwid_ready() -> bool:
    return bool(re.fullmatch(r"\d{3,12}", ECWID_STORE_ID) and ECWID_SECRET_TOKEN)


async def ecwid(method: str, path: str, params=None, body=None):
    if not ecwid_ready():
        raise HTTPException(status_code=503, detail="Ecwid isn't connected yet. Add ECWID_STORE_ID and ECWID_SECRET_TOKEN in Render.")
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.request(method, f"https://app.ecwid.com/api/v3/{ECWID_STORE_ID}{path}", params=params, json=body,
                                     headers={"Authorization": f"Bearer {ECWID_SECRET_TOKEN}", "Accept": "application/json"})
    except httpx.HTTPError:
        raise HTTPException(status_code=502, detail="Couldn't reach Ecwid. Try again in a minute.")
    if r.status_code in (401, 403):
        _ctx["logger"].error("Ecwid %s %s refused (%s): %s", method, path, r.status_code, r.text[:300])
        if r.status_code == 403 and path != "/profile":
            raise HTTPException(status_code=502, detail=f"Ecwid said this store or app isn't allowed to do that ({path.split('/')[1]}). "
                                                          "Your Ecwid plan may not include it, or the custom app is missing that permission.")
        raise HTTPException(status_code=502, detail="Ecwid refused the token. Check ECWID_STORE_ID and ECWID_SECRET_TOKEN in Render.")
    if r.status_code == 404:
        raise HTTPException(status_code=404, detail="Not found in Ecwid.")
    if r.status_code >= 400:
        _ctx["logger"].error("Ecwid %s %s failed: %s %s", method, path, r.status_code, r.text[:300])
        msg = ""
        try:
            msg = str((r.json() or {}).get("errorMessage") or "")[:200]
        except ValueError:
            pass
        raise HTTPException(status_code=502, detail=f"Ecwid said no ({r.status_code}){': ' + msg if msg else ''}.")
    try:
        return r.json()
    except ValueError:
        return {}


async def cached(key: str, seconds: int, fn):
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < seconds:
        return hit[1]
    val = await fn()
    _cache[key] = (time.time(), val)
    return val


async def fetch_orders(days: int = 90) -> list:
    async def load():
        out, offset = [], 0
        since = int(time.time()) - days * 86400
        while offset < 1000:
            page = await ecwid("GET", "/orders", params={"limit": 100, "offset": offset, "createdFrom": since})
            items = page.get("items") or []
            out.extend(items)
            if len(items) < 100:
                break
            offset += 100
        return out
    return await cached(f"orders{days}", 60, load)


# ---------- Supabase ----------
async def db(method: str, table: str, params=None, body=None, prefer: str = ""):
    headers = {**_ctx["auth_headers"]()}
    if prefer:
        headers["Prefer"] = prefer
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.request(method, f"{_ctx['supabase_url']}/rest/v1/{table}", params=params, json=body, headers=headers)
    except httpx.HTTPError:
        raise HTTPException(status_code=502, detail="Couldn't reach the database.")
    if r.status_code >= 400:
        _ctx["logger"].error("ecom db %s %s: %s %s", method, table, r.status_code, r.text[:200])
        raise HTTPException(status_code=502, detail="The database said no. Was ecom.sql run in Supabase?")
    try:
        return r.json()
    except ValueError:
        return None


async def load_setup():
    wh = await db("GET", "ecom_warehouses", params={"select": "*", "order": "name.asc"}) or []
    rules = await db("GET", "ecom_sku_rules", params={"select": "*", "order": "match.asc"}) or []
    meta = await db("GET", "ecom_order_meta", params={"select": "*"}) or []
    return wh, rules, {m["order_id"]: m for m in meta}


def supplier_for(sku: str, rules) -> Optional[str]:
    sku = (sku or "").upper()
    best = None
    for r in rules:
        m = (r.get("match") or "").upper()
        if m and sku.startswith(m) and (best is None or len(m) > len(best["match"])):
            best = r
    return best["supplier"] if best else None


CHANNEL_NAMES = {"FACEBOOK": "Facebook", "INSTAGRAM": "Instagram", "GOOGLE": "Google", "GOOGLE_SHOPPING": "Google Shopping",
                 "TIKTOK": "TikTok", "AMAZON": "Amazon", "EBAY": "eBay", "WALMART": "Walmart", "PINTEREST": "Pinterest",
                 "SNAPCHAT": "Snapchat", "ETSY": "Etsy", "YAHOO": "Yahoo", "BING": "Microsoft Bing", "MICROSOFT": "Microsoft"}


def order_channel(o: dict) -> str:
    """Where the sale came from, as far as Ecwid tells us."""
    ext = o.get("externalOrderData") or {}
    raw = str(ext.get("refererChannel") or ext.get("refererId") or o.get("refererId") or "").strip()
    if raw:
        key = re.sub(r"[^A-Z]", "_", raw.upper())
        for k, name in CHANNEL_NAMES.items():
            if k in key:
                return name
        return raw.replace("_", " ").title()[:40]
    url = str(o.get("referrerUrl") or o.get("refererUrl") or "").lower()
    for k, name in CHANNEL_NAMES.items():
        if k.lower().split("_")[0] in url:
            return name
    return "Online store"


def route_order(o: dict, wh: list, rules: list, meta: dict) -> dict:
    sp = o.get("shippingPerson") or o.get("billingPerson") or {}
    oid = str(o.get("id") or o.get("orderNumber"))
    point = zip_point(sp.get("postalCode"))
    m = meta.get(oid) or {}
    logistics = [w for w in wh if w.get("active") and w.get("kind") == "logistics"]
    suppliers = [w for w in wh if w.get("active") and w.get("kind") == "supplier"]

    hub, hub_mi = None, None
    if m.get("hub_id"):
        hub = next((w for w in logistics if w["id"] == m["hub_id"]), None)
        if hub and point:
            hub_mi = miles(point, hub)
    elif point:
        n = nearest(point, logistics)
        if n:
            hub, hub_mi = n

    items = []
    for it in o.get("items") or []:
        sup = supplier_for(it.get("sku"), rules)
        pool = [w for w in suppliers if not sup or (w.get("supplier") or "").lower() == sup.lower()]
        src = nearest(point, pool) if point else None
        items.append({
            "name": it.get("name"), "sku": it.get("sku"), "quantity": it.get("quantity"), "price": it.get("price"),
            "image": it.get("smallThumbnailUrl") or it.get("imageUrl"),
            "supplier": sup or (src[0].get("supplier") if src else None),
            "ships_from": ({"name": src[0]["name"], "city": src[0].get("city"), "state": src[0].get("state"),
                            "miles": src[1], "days": est_days(src[1])} if src else None),
            "by_hand": bool(sup and sup.lower() not in AUTO_SUPPLIERS and (not pool or sup.lower() in MANUAL_SUPPLIERS)),
            "auto": bool(sup and sup.lower() in AUTO_SUPPLIERS),
        })
    return {
        "id": oid,
        "number": o.get("orderNumber"),
        "created": o.get("createDate"),
        "total": o.get("total"),
        "payment": o.get("paymentStatus"),
        "status": o.get("fulfillmentStatus"),
        "tracking": o.get("trackingNumber") or "",
        "customer": sp.get("name") or o.get("email") or "",
        "phone": sp.get("phone") or "",
        "address": ", ".join(x for x in [sp.get("street"), sp.get("city"), sp.get("stateOrProvinceCode"), sp.get("postalCode")] if x),
        "zip": (point or {}).get("zip") or sp.get("postalCode") or "",
        "lat": (point or {}).get("lat"), "lng": (point or {}).get("lng"),
        "state": sp.get("stateOrProvinceCode") or (point or {}).get("state") or "",
        "zip_found": bool(point),
        "hub": ({"id": hub["id"], "name": hub["name"], "miles": hub_mi} if hub else None),
        "hub_manual": bool(m.get("hub_id")),
        "notes": m.get("notes") or "",
        "channel": order_channel(o),
        "ordered": m.get("ordered") or {},
        "processed": bool(m.get("processed_at")),
        "ship_to": "\n".join(x for x in [sp.get("name"), sp.get("companyName"), sp.get("street"),
                                          " ".join(y for y in [f"{sp.get('city') or ''},", sp.get("stateOrProvinceCode"), sp.get("postalCode")] if y),
                                          sp.get("countryName") or sp.get("countryCode"), sp.get("phone")] if x and x.strip(",")),
        "items": items,
        "open": (o.get("fulfillmentStatus") or "") not in DONE,
    }


def order_stops(hub, orders):
    """Nearest-next ordering from the hub, so the driver isn't zig-zagging."""
    left = [o for o in orders if o.get("lat") is not None]
    no_point = [o for o in orders if o.get("lat") is None]
    here, out = hub, []
    while left:
        nxt = min(left, key=lambda o: miles(here, o))
        out.append(nxt)
        left.remove(nxt)
        here = nxt
    return out + no_point


def map_links(hub, stops):
    """Directions links, 9 stops per link (map apps cap the number of stops)."""
    pts = [o for o in stops if o.get("lat") is not None]
    links, start = [], f"{hub['lat']},{hub['lng']}"
    for i in range(0, len(pts), 9):
        chunk = pts[i:i + 9]
        path = "/".join([start] + [f"{o['lat']},{o['lng']}" for o in chunk])
        links.append(f"https://www.google.com/maps/dir/{path}")
        start = f"{chunk[-1]['lat']},{chunk[-1]['lng']}"
    return links


# ---------- automation ----------
async def all_routed(days: int = 90):
    orders = await fetch_orders(days)
    wh, rules, meta = await load_setup()
    return [route_order(o, wh, rules, meta) for o in orders], wh


async def load_settings() -> dict:
    rows = await db("GET", "ecom_settings", params={"select": "*"}) or []
    vals = {r["key"]: r.get("value") for r in rows}
    out = {sw["key"]: bool(vals.get(sw["key"], sw["default"])) for sw in SWITCHES}
    out["_last_run"] = vals.get("_last_run") or {}
    out["_last_daily"] = vals.get("_last_daily") or ""
    return out


async def save_setting(key: str, value):
    await db("POST", "ecom_settings", params={"on_conflict": "key"}, body={"key": key, "value": value},
             prefer="resolution=merge-duplicates,return=minimal")


async def save_meta(oid: str, fields: dict):
    row = {"order_id": oid, "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **fields}
    await db("POST", "ecom_order_meta", params={"on_conflict": "order_id"}, body=row,
             prefer="resolution=merge-duplicates,return=minimal")


RIBBON_MARK = "📍"
RIBBON_COLOR = "#1F7A4D"


def _rlen(t: str) -> int:
    return len(t.encode("utf-16-le")) // 2      # count the emoji as 2, the way browsers do


def ribbon_for(sku: str, wh: list, rules: list) -> str:
    """'📍 Ships from Cranbury, NJ 08512' (Ecwid allows 30 characters), from the product's supplier warehouses."""
    sup = supplier_for(sku, rules)
    if not sup:
        return ""
    places = [w for w in wh if w.get("active") and w.get("kind") == "supplier" and (w.get("supplier") or "").lower() == sup.lower()]
    if not places:
        return ""
    if len(places) == 1:
        w = places[0]
        for text in (f"{RIBBON_MARK} Ships from {w.get('city')}, {w.get('state')} {w.get('zip')}",
                     f"{RIBBON_MARK} From {w.get('city')}, {w.get('state')} {w.get('zip')}",
                     f"{RIBBON_MARK} Ships from {w.get('state')} {w.get('zip')}"):
            if _rlen(text) <= 30:
                return text
        return f"{RIBBON_MARK} Ships from {w.get('state')}"
    states = []
    for w in places:
        if w.get("state") and w["state"] not in states:
            states.append(w["state"])
    text = f"{RIBBON_MARK} Ships from {' & '.join(states)}"
    return text if _rlen(text) <= 30 else f"{RIBBON_MARK} Ships from {len(places)} US hubs"


async def apply_ribbons(force: bool = False) -> dict:
    """Set or refresh our 📍 ribbons. Ribbons the owner typed themselves (not starting with 📍) are left alone."""
    wh, rules, _ = await load_setup()
    out = {"updated": 0, "kept": 0, "no_warehouse": 0, "yours": 0}
    offset = 0
    while offset < 2000:
        page = await ecwid("GET", "/products", params={"limit": 100, "offset": offset})
        items = page.get("items") or []
        for p in items:
            cur = ((p.get("ribbon") or {}).get("text") or "").strip()
            if cur and not cur.startswith(RIBBON_MARK) and not force:
                out["yours"] += 1
                continue
            want = ribbon_for(p.get("sku"), wh, rules)
            if not want:
                out["no_warehouse"] += 1
                if cur.startswith(RIBBON_MARK):      # supplier warehouse removed: take our old ribbon off
                    await ecwid("PUT", f"/products/{p['id']}", body={"ribbon": {"text": "", "color": RIBBON_COLOR}})
                continue
            if cur == want:
                out["kept"] += 1
                continue
            try:
                await ecwid("PUT", f"/products/{p['id']}", body={"ribbon": {"text": want, "color": RIBBON_COLOR}})
                out["updated"] += 1
            except HTTPException:
                out["failed"] = out.get("failed", 0) + 1
        if len(items) < 100:
            break
        offset += 100
    return out


def needs_ordering(o: dict) -> list:
    """Suppliers this open order still has to be placed with (by hand, until auto-ordering exists)."""
    if not o["open"] or o["payment"] != "PAID":
        return []
    sups = []
    for i in o["items"]:
        sup = i["supplier"] or "Unknown supplier"
        if sup.lower() in AUTO_SUPPLIERS:
            continue
        if sup not in sups and not o["ordered"].get(sup):
            sups.append(sup)
    return sups


def _gmail_send(msg) -> None:
    """Gmail over SSL (465); if that connection is closed, try STARTTLS (587)."""
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=20) as smtp:
            smtp.login(GMAIL, GMAIL_PASSWORD)
            smtp.send_message(msg)
            return
    except (smtplib.SMTPServerDisconnected, OSError):
        pass
    with smtplib.SMTP("smtp.gmail.com", 587, timeout=20) as smtp:
        smtp.starttls()
        smtp.login(GMAIL, GMAIL_PASSWORD)
        smtp.send_message(msg)


async def send_email(to: list, subject: str, body: str) -> int:
    """Gmail first; if Gmail can't be reached, Amazon SES (CashCow Mail's sender)."""
    to = [t for t in to if t]
    if not to:
        return 0
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["To"] = ", ".join(to)
    msg.set_content(body)
    problems = []
    if GMAIL and GMAIL_PASSWORD:
        msg["From"] = formataddr(("CashCow eCommerce", GMAIL))
        try:
            await asyncio.to_thread(_gmail_send, msg)
            return len(to)
        except Exception as e:
            problems.append(f"Gmail: {type(e).__name__}")
    try:
        import mail as _mail
        if _mail.ses_ready():
            del msg["From"]
            msg["From"] = os.environ.get("MAIL_FROM") or "CashCow <news@cashcowai.online>"
            for t in to:
                await _mail.ses_send_raw(msg.as_bytes(), t)
            return len(to)
    except Exception as e:
        problems.append(f"SES: {type(e).__name__}")
    _ctx["logger"].error("Daily email not sent: %s", "; ".join(problems) or "no sender set up")
    return 0


def daily_text(orders: list, today: str) -> str:
    yday = [o for o in orders if o["payment"] == "PAID" and (o["created"] or "")[:10] >= today]
    open_o = [o for o in orders if o["open"]]
    todo = [(o, needs_ordering(o)) for o in open_o]
    todo = [(o, s) for o, s in todo if s]
    lines = [f"CashCow eCommerce daily summary (new paid orders since {today}: {len(yday)})", ""]
    lines.append(f"Open orders: {len(open_o)}")
    lines.append(f"Still need ordering from suppliers: {len(todo)}")
    if todo:
        for o, sups in todo[:25]:
            lines.append(f"  - #{o['number']} ({o['state']} {o['zip']}): {', '.join(sups)}")
    stuck = [o for o in open_o if not o["hub"] or not o["zip_found"]]
    if stuck:
        lines.append(f"Need a look (no hub or bad ZIP): {len(stuck)}")
        for o in stuck[:15]:
            lines.append(f"  - #{o['number']} {o['zip'] or 'no ZIP'}")
    unpaid = [o for o in open_o if o["payment"] != "PAID"]
    if unpaid:
        lines.append(f"Waiting on payment: {len(unpaid)}")
    lines += ["", f"Open the dashboard: {DASHBOARD_URL}", "", "Turn this email off in the dashboard's Automation tab."]
    return "\n".join(lines)


_running = {"busy": False}


async def tick(force: bool = False) -> dict:
    """One automation pass: process new paid orders, then the daily email when it's due."""
    if _running["busy"] or not ecwid_ready():
        return {}
    _running["busy"] = True
    report = {"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "processed": 0, "emailed": False, "error": ""}
    try:
        st = await load_settings()
        if st["auto_route"] or force:
            _cache.pop("orders90", None)
            orders, _ = await all_routed()
            for o in orders:
                if o["processed"] or o["payment"] != "PAID" or o["status"] != "AWAITING_PROCESSING":
                    continue
                if st["auto_processing"]:
                    await ecwid("PUT", f"/orders/{o['id']}", body={"fulfillmentStatus": "PROCESSING"})
                await save_meta(o["id"], {"processed_at": report["at"]})
                report["processed"] += 1
            if report["processed"]:
                _cache.pop("orders90", None)
        try:                      # The Candy Lady: record sales made with a Candy Lady's code
            import candy
            report["candy"] = await candy.record_sales(await fetch_orders())
            report["candy_codes"] = await candy.activate_waiting_codes()
        except HTTPException:
            pass                  # candy.sql not run yet
        if st["auto_ribbons"] and time.time() - _running.get("ribbons", 0) > 3600:
            _running["ribbons"] = time.time()
            report["ribbons"] = (await apply_ribbons())["updated"]
        now = datetime.now(TZ)
        today = now.strftime("%Y-%m-%d")
        if (st["daily_email"] and not switch_ready("daily_email") and now.hour >= 7 and st["_last_daily"] != today
                and time.time() - _running.get("mail_try", 0) > 3600):     # if sending fails, retry hourly, not every 5 minutes
            _running["mail_try"] = time.time()
            orders, _ = await all_routed()
            sent = await send_email(sorted(ECOM_ADMIN_EMAILS), f"CashCow eCommerce: {today} summary",
                                    daily_text(orders, (now - timedelta(days=1)).strftime("%Y-%m-%d")))
            if sent:
                await save_setting("_last_daily", today)
                report["emailed"] = True
            else:
                report["error"] = "The daily email couldn't be sent. Will try again in an hour."
    except HTTPException as e:
        report["error"] = str(e.detail)
    except Exception as e:  # keep the loop alive
        report["error"] = type(e).__name__
        _ctx["logger"].error("ecom tick failed: %s", e)
    finally:
        _running["busy"] = False
    try:
        await save_setting("_last_run", report)
    except HTTPException:
        pass
    return report


async def tick_loop():
    await asyncio.sleep(30)
    while True:
        try:
            await tick()
        except Exception as e:
            _ctx["logger"].error("ecom loop: %s", e)
        await asyncio.sleep(TICK_SECONDS)


# ---------- request models ----------
class WarehouseIn(BaseModel):
    id: str = ""
    name: str
    kind: str = "logistics"          # logistics (local hub) or supplier (where products ship from)
    supplier: str = ""
    zip: str
    address: str = ""
    phone: str = ""
    categories: str = ""
    active: bool = True
    notes: str = ""


class RuleIn(BaseModel):
    match: str
    supplier: str


class OrderUpdate(BaseModel):
    status: str = ""
    tracking: Optional[str] = None
    hub_id: Optional[str] = None
    notes: Optional[str] = None


class AskIn(BaseModel):
    question: str = ""
    history: list = []
    image: str = ""          # optional photo, base64 (JPEG/PNG/WebP), from the camera button
    image_type: str = ""


ASK_SYSTEM = """You are the assistant inside the CashCow eCommerce control dashboard. The store runs on Ecwid and sells
furniture, electronics and more from US dropship suppliers (CJdropshipping, CROV and others). The dashboard matches each
order's ZIP code to the nearest supplier warehouse (where items ship from) and the nearest local logistics hub (which
handles delivery in that area), and groups open orders into delivery routes by hub. CROV items are always ordered by hand
on crov.com.

Answer the owner's question using the store data below. Be short and practical: plain sentences or a few bullets, no
tables, no markdown headings. Quote order numbers, states and dollar amounts exactly as given. If the data doesn't answer
the question, say what's missing. When the owner asks you to set something up or change something, use the matching tool to propose it (you may propose
several); they see each one with a Confirm button, and nothing changes until they tap it. In your text, say briefly what you
proposed. Only propose what they asked for or clearly need. When a photo is attached (a product, label, packing slip, invoice or screenshot), read it
carefully and use what you see: for a product, suggest a store title, a short description, a sensible SKU start for its supplier, and a store section
and shelf (sections: Everyday Essentials, The Candy Lady, Candles, or another fitting one). You can't change anything yourself: when an action is needed, say which dashboard tab
does it (Orders, Routes, Warehouses, Products, ZIP check) or that it's done in Ecwid. Don't invent numbers."""


# One-tap store sections: a main category with its shelves, created in Ecwid.
SECTIONS = {
    "essentials": {"name": "Everyday Essentials", "icon": "🧼",
                   "about": "Everything for the home and personal care in one place, so customers stock up in one trip.",
                   "shelves": ["Oral Care", "Bath & Body", "Hair Care", "Paper Goods", "Bathroom Cleaning",
                               "Kitchen & Dish", "Laundry", "First Aid & Personal Care"]},
    "candy": {"name": "The Candy Lady", "icon": "🍬",
              "about": "Candy, snacks, drinks and party treats, shipped to your door.",
              "shelves": ["Candy", "Nostalgia Candy", "Snacks", "Drinks & Treats", "Freeze-Dried Treats", "Party & Dollar Items",
                          "Party Supplies", "Goodie Bags & Favors", "Balloons & Decorations"]},
    "candles": {"name": "Candles", "icon": "🕯️",
                "about": "Hand-poured and custom-label scented candles.",
                "shelves": ["Scented Candles", "Holiday Candles", "Carved & Pillar Candles", "Gift Sets", "Wax Melts"]},
}


async def ecwid_categories() -> list:
    out, offset = [], 0
    while offset < 2000:
        page = await ecwid("GET", "/categories", params={"limit": 100, "offset": offset})
        items = page.get("items") or []
        out.extend(items)
        if len(items) < 100:
            break
        offset += 100
    return out


async def ensure_category(name: str, parent_id: int = 0, description: str = "", cats: Optional[list] = None) -> dict:
    """Find a category by name under its parent, or create it. Returns {id, name, url, created}."""
    cats = cats if cats is not None else await ecwid_categories()
    for c in cats:
        if (c.get("name") or "").strip().lower() == name.strip().lower() and int(c.get("parentId") or 0) == int(parent_id or 0):
            return {"id": c["id"], "name": c["name"], "url": c.get("url") or "", "created": False}
    body = {"name": name, "enabled": True}
    if parent_id:
        body["parentId"] = parent_id
    if description:
        body["description"] = f"<p>{_html_escape(description)}</p>"
    made = await ecwid("POST", "/categories", body=body)
    cats.append({"id": made.get("id"), "name": name, "parentId": parent_id})
    return {"id": made.get("id"), "name": name, "url": "", "created": True}


async def build_section(key: str) -> dict:
    t = SECTIONS[key]
    cats = await ecwid_categories()
    top = await ensure_category(t["name"], 0, t["about"], cats)
    made = [s for s in [await ensure_category(sh, top["id"], "", cats) for sh in t["shelves"]] if s["created"]]
    if not top["url"]:
        try:
            top["url"] = (await ecwid("GET", f"/categories/{top['id']}")).get("url") or ""
        except HTTPException:
            pass
    return {"section": t["name"], "id": top["id"], "url": top["url"], "new_shelves": len(made) + (1 if top["created"] else 0)}


async def category_id_for(path: str) -> Optional[int]:
    """'Everyday Essentials > Oral Care' or 'Oral Care' -> category id (created if missing)."""
    parts = [p.strip() for p in re.split(r">|/", path or "") if p.strip()][:2]
    if not parts:
        return None
    cats = await ecwid_categories()
    if len(parts) == 2:
        top = await ensure_category(parts[0], 0, "", cats)
        return (await ensure_category(parts[1], top["id"], "", cats))["id"]
    hit = next((c for c in cats if (c.get("name") or "").lower() == parts[0].lower()), None)
    return hit["id"] if hit else (await ensure_category(parts[0], 0, "", cats))["id"]


ASK_TOOLS = [
    {"name": "add_warehouse", "description": "Propose adding a warehouse: a supplier warehouse (where products ship from) or a local logistics hub (delivers in its area). Needs a real 5-digit US ZIP.",
     "input_schema": {"type": "object", "properties": {
         "name": {"type": "string"}, "kind": {"type": "string", "enum": ["supplier", "logistics"]},
         "supplier": {"type": "string", "description": "Supplier name for supplier warehouses, e.g. CJ or CROV"},
         "zip": {"type": "string"}, "categories": {"type": "string"}}, "required": ["name", "kind", "zip"]}},
    {"name": "add_sku_rule", "description": "Propose a SKU rule: products whose SKU starts with `match` come from `supplier`.",
     "input_schema": {"type": "object", "properties": {"match": {"type": "string"}, "supplier": {"type": "string"}}, "required": ["match", "supplier"]}},
    {"name": "create_product", "description": "Propose creating a product in the Ecwid store, e.g. from a photo. Use a SKU that starts with the supplier's letters (like CJ- or CROV-). Price in US dollars.",
     "input_schema": {"type": "object", "properties": {
         "name": {"type": "string"}, "sku": {"type": "string"}, "price": {"type": "number"},
         "description": {"type": "string", "description": "2-4 short sentences, plain text"},
         "quantity": {"type": "integer", "description": "Stock count; leave out for unlimited (dropship)"},
         "use_photo": {"type": "boolean", "description": "true to use the photo the owner attached as the product image"},
         "category": {"type": "string", "description": "Store section and shelf, e.g. 'Everyday Essentials > Oral Care' or 'The Candy Lady > Snacks'"}},
         "required": ["name", "sku", "price", "description"]}},
    {"name": "update_order", "description": "Propose changing an order's status and/or tracking number in Ecwid.",
     "input_schema": {"type": "object", "properties": {
         "order_number": {"type": "integer"},
         "status": {"type": "string", "enum": ["AWAITING_PROCESSING", "PROCESSING", "SHIPPED", "OUT_FOR_DELIVERY", "DELIVERED", "READY_FOR_PICKUP", "WILL_NOT_DELIVER", "RETURNED"]},
         "tracking": {"type": "string"}}, "required": ["order_number"]}},
    {"name": "mark_supplier_ordered", "description": "Propose marking that an order was placed with a supplier (after the owner says they ordered it).",
     "input_schema": {"type": "object", "properties": {"order_number": {"type": "integer"}, "supplier": {"type": "string"}, "ref": {"type": "string"}},
                      "required": ["order_number", "supplier"]}},
]


def action_summary(t: str, i: dict) -> str:
    if t == "add_warehouse":
        kind = "local hub" if i.get("kind") == "logistics" else f"supplier warehouse ({i.get('supplier') or '?'})"
        return f"Add {kind} \"{i.get('name')}\" at ZIP {i.get('zip')}"
    if t == "add_sku_rule":
        return f"Add SKU rule: {str(i.get('match', '')).upper()} → {i.get('supplier')}"
    if t == "create_product":
        q = "unlimited stock" if i.get("quantity") is None else f"stock {i.get('quantity')}"
        return (f"Create product \"{i.get('name')}\" · SKU {i.get('sku')} · ${i.get('price')} · {q}" + (" · with your photo" if i.get("use_photo") else "")
                + (f" · in {i['category']}" if i.get("category") else ""))
    if t == "update_order":
        bits = [b for b in [i.get("status") and f"status {i['status'].replace('_', ' ').lower()}", i.get("tracking") and f"tracking {i['tracking']}"] if b]
        return f"Update order #{i.get('order_number')}: " + (", ".join(bits) or "no change")
    if t == "mark_supplier_ordered":
        return f"Mark order #{i.get('order_number')} as ordered from {i.get('supplier')}" + (f" (ref {i['ref']})" if i.get("ref") else "")
    return t


class SwitchIn(BaseModel):
    key: str
    on: bool


class OrderedIn(BaseModel):
    supplier: str
    ref: str = ""
    undo: bool = False


class ActionIn(BaseModel):
    type: str
    input: dict = {}
    image: str = ""
    image_type: str = ""


class ProductUpdate(BaseModel):
    price: Optional[float] = None
    quantity: Optional[int] = None
    enabled: Optional[bool] = None


def _html_escape(t: str) -> str:
    return t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def clean(s, n):
    return re.sub(r"[<>]", "", str(s or "")).strip()[:n]


def register(app, *, account_user, auth_headers, supabase_url, logger):
    _ctx.update(auth_headers=auth_headers, supabase_url=supabase_url, logger=logger)
    logger.info("CashCow eCommerce config: Ecwid ready=%s, admins=%s, zips=%s", ecwid_ready(), len(ECOM_ADMIN_EMAILS), len(zips()))

    async def admin(request: Request):
        user = await account_user(request.headers.get("authorization", ""))
        if not user:
            raise HTTPException(status_code=401, detail="Please sign in to your CashCow account.")
        if (user.get("email") or "").lower() not in ECOM_ADMIN_EMAILS:
            raise HTTPException(status_code=403, detail="This account isn't allowed into the eCommerce dashboard.")
        return user

    async def routed(days=90):
        return await all_routed(days)

    on_event = getattr(app, "on_event", None)
    if on_event:
        @on_event("startup")
        async def _start_ecom_loop():
            asyncio.create_task(tick_loop())

    import candy                  # The Candy Lady sellers program (candy.py, candy.sql)
    candy.register(app, admin=admin, account_user=account_user, logger=logger)

    import cj                     # fill shelves from CJdropshipping (cj.py)
    cj.register(app, admin=admin, logger=logger)

    @app.get("/api/ecom/overview")
    async def ecom_overview(request: Request):
        await admin(request)
        out = {"ecwid": ecwid_ready(), "store": None}
        if not ecwid_ready():
            return out
        prof = await cached("profile", 3600, lambda: ecwid("GET", "/profile"))
        out["store"] = (prof.get("generalInfo") or {}).get("storeUrl") or (prof.get("account") or {}).get("accountName")
        orders, wh = await routed()
        cutoff = time.strftime("%Y-%m-%d", time.gmtime(time.time() - 30 * 86400))
        paid30 = [o for o in orders if o["payment"] == "PAID" and (o["created"] or "") >= cutoff]
        states: dict = {}
        chans: dict = {}
        for o in paid30:
            c = chans.setdefault(o["channel"], [0, 0.0])
            c[0] += 1
            c[1] = round(c[1] + float(o["total"] or 0), 2)
        for o in orders:
            if o["open"]:
                states[o["state"] or "?"] = states.get(o["state"] or "?", 0) + 1
        out.update({
            "open": sum(1 for o in orders if o["open"]),
            "to_process": sum(1 for o in orders if o["status"] == "AWAITING_PROCESSING"),
            "by_hand": sum(1 for o in orders if o["open"] and any(i["by_hand"] for i in o["items"])),
            "no_hub": sum(1 for o in orders if o["open"] and not o["hub"]),
            "bad_zip": sum(1 for o in orders if o["open"] and not o["zip_found"]),
            "sales30": round(sum(float(o["total"] or 0) for o in paid30), 2),
            "orders30": len(paid30),
            "open_by_state": sorted(states.items(), key=lambda kv: -kv[1])[:12],
            "by_channel": sorted(chans.items(), key=lambda kv: -kv[1][1]),
            "hubs": sum(1 for w in wh if w.get("kind") == "logistics" and w.get("active")),
            "supplier_warehouses": sum(1 for w in wh if w.get("kind") == "supplier" and w.get("active")),
        })
        return out

    @app.get("/api/ecom/orders")
    async def ecom_orders(request: Request, view: str = "open"):
        await admin(request)
        orders, _ = await routed()
        if view == "open":
            orders = [o for o in orders if o["open"]]
        orders.sort(key=lambda o: o["created"] or "", reverse=True)
        return {"orders": orders[:300]}

    @app.post("/api/ecom/orders/{oid}")
    async def ecom_update_order(oid: str, payload: OrderUpdate, request: Request):
        await admin(request)
        if not re.fullmatch(r"[A-Za-z0-9-]{1,40}", oid):
            raise HTTPException(status_code=404, detail="Not found.")
        change = {}
        if payload.status:
            if payload.status not in FULFILLMENT:
                raise HTTPException(status_code=400, detail="Unknown status.")
            change["fulfillmentStatus"] = payload.status
        if payload.tracking is not None:
            change["trackingNumber"] = clean(payload.tracking, 80)
        if change:
            await ecwid("PUT", f"/orders/{oid}", body=change)
            _cache.pop("orders90", None)
        if payload.hub_id is not None or payload.notes is not None:
            row = {"order_id": oid, "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
            if payload.hub_id is not None:
                if payload.hub_id and not re.fullmatch(r"[0-9a-f-]{36}", payload.hub_id):
                    raise HTTPException(status_code=400, detail="Unknown hub.")
                row["hub_id"] = payload.hub_id or None
            if payload.notes is not None:
                row["notes"] = clean(payload.notes, 500)
            await db("POST", "ecom_order_meta", params={"on_conflict": "order_id"}, body=row,
                     prefer="resolution=merge-duplicates,return=minimal")
        return {"ok": True}

    @app.get("/api/ecom/routes")
    async def ecom_routes(request: Request):
        await admin(request)
        orders, wh = await routed()
        open_orders = [o for o in orders if o["open"]]
        out = []
        for hub in [w for w in wh if w.get("kind") == "logistics" and w.get("active")]:
            mine = [o for o in open_orders if o["hub"] and o["hub"]["id"] == hub["id"]]
            if not mine:
                out.append({"hub": hub, "stops": [], "links": [], "miles": 0})
                continue
            stops = order_stops(hub, mine)
            total, here = 0.0, hub
            for s in stops:
                if s.get("lat") is not None:
                    total += miles(here, s)
                    here = s
            out.append({"hub": hub, "stops": stops, "links": map_links(hub, stops), "miles": round(total, 1)})
        out.sort(key=lambda r: -len(r["stops"]))
        return {"routes": out, "unrouted": [o for o in open_orders if not o["hub"]]}

    @app.get("/api/ecom/zip/{z}")
    async def ecom_zip(z: str, request: Request):
        await admin(request)
        p = zip_point(z)
        if not p:
            raise HTTPException(status_code=404, detail="That ZIP code wasn't found.")
        wh, _, _ = await load_setup()
        def ranked(kind):
            rows = [(w, miles(p, w)) for w in wh if w.get("active") and w.get("kind") == kind and w.get("lat") is not None]
            rows.sort(key=lambda x: x[1])
            return [{"name": w["name"], "supplier": w.get("supplier"), "city": w.get("city"), "state": w.get("state"),
                     "miles": d, "days": est_days(d)} for w, d in rows[:5]]
        return {"place": p, "hubs": ranked("logistics"), "suppliers": ranked("supplier")}

    @app.get("/api/ecom/warehouses")
    async def ecom_warehouses(request: Request):
        await admin(request)
        wh, rules, _ = await load_setup()
        return {"warehouses": wh, "rules": rules}

    @app.post("/api/ecom/warehouses")
    async def ecom_save_warehouse(payload: WarehouseIn, request: Request):
        await admin(request)
        p = zip_point(payload.zip)
        if not p:
            raise HTTPException(status_code=400, detail="That ZIP code wasn't found. Use a 5-digit US ZIP.")
        if payload.kind not in ("logistics", "supplier"):
            raise HTTPException(status_code=400, detail="Type must be logistics or supplier.")
        name = clean(payload.name, 80)
        if not name:
            raise HTTPException(status_code=400, detail="Give the warehouse a name.")
        row = {"name": name, "kind": payload.kind, "supplier": clean(payload.supplier, 60), "zip": p["zip"],
               "city": p["city"], "state": p["state"], "lat": p["lat"], "lng": p["lng"],
               "address": clean(payload.address, 200), "phone": clean(payload.phone, 40),
               "categories": clean(payload.categories, 200), "active": payload.active, "notes": clean(payload.notes, 500)}
        if payload.id:
            if not re.fullmatch(r"[0-9a-f-]{36}", payload.id):
                raise HTTPException(status_code=404, detail="Not found.")
            await db("PATCH", "ecom_warehouses", params={"id": f"eq.{payload.id}"}, body=row, prefer="return=minimal")
        else:
            await db("POST", "ecom_warehouses", body=row, prefer="return=minimal")
        return {"ok": True, "place": p}

    @app.post("/api/ecom/warehouses/{wid}/delete")
    async def ecom_delete_warehouse(wid: str, request: Request):
        await admin(request)
        if not re.fullmatch(r"[0-9a-f-]{36}", wid):
            raise HTTPException(status_code=404, detail="Not found.")
        await db("DELETE", "ecom_warehouses", params={"id": f"eq.{wid}"}, prefer="return=minimal")
        return {"ok": True}

    @app.post("/api/ecom/rules")
    async def ecom_save_rule(payload: RuleIn, request: Request):
        await admin(request)
        m, s = clean(payload.match, 40).upper(), clean(payload.supplier, 60)
        if not m or not s:
            raise HTTPException(status_code=400, detail="Fill in both the SKU start and the supplier.")
        await db("POST", "ecom_sku_rules", params={"on_conflict": "match"}, body={"match": m, "supplier": s},
                 prefer="resolution=merge-duplicates,return=minimal")
        return {"ok": True}

    @app.post("/api/ecom/rules/{rid}/delete")
    async def ecom_delete_rule(rid: str, request: Request):
        await admin(request)
        if not re.fullmatch(r"[0-9a-f-]{36}", rid):
            raise HTTPException(status_code=404, detail="Not found.")
        await db("DELETE", "ecom_sku_rules", params={"id": f"eq.{rid}"}, prefer="return=minimal")
        return {"ok": True}

    @app.post("/api/ecom/ribbons")
    async def ecom_ribbons(request: Request, force: bool = False):
        await admin(request)
        r = await apply_ribbons(force)
        _running["ribbons"] = time.time()
        return {**r, "message": f"{r['updated']} ribbon{'s' if r['updated'] != 1 else ''} added or updated, {r['kept']} already right"
                + (f", {r['no_warehouse']} with no supplier warehouse yet" if r["no_warehouse"] else "")
                + (f", {r['yours']} with your own ribbon left alone" if r["yours"] else "") + "."}

    @app.get("/api/ecom/sections")
    async def ecom_sections(request: Request):
        await admin(request)
        return {"sections": [{"key": k, "name": t["name"], "icon": t["icon"], "about": t["about"], "shelves": t["shelves"]}
                             for k, t in SECTIONS.items()]}

    @app.post("/api/ecom/sections/{key}")
    async def ecom_build_section(key: str, request: Request):
        await admin(request)
        if key not in SECTIONS:
            raise HTTPException(status_code=404, detail="Unknown section.")
        r = await build_section(key)
        if key == "candy" and r["url"]:
            try:
                import candy
                if not (await candy.candy_settings())["candy_url"]:
                    await save_setting("candy_url", r["url"])
                    r["candy_link_set"] = True
            except HTTPException:
                pass
        r["message"] = (f"{r['section']} is ready in your store" + (f" ({r['new_shelves']} new shelves)" if r["new_shelves"] else " (it was already there)")
                        + (". It's now the link every Candy Lady shares." if r.get("candy_link_set") else "."))
        return r

    @app.get("/api/ecom/products")
    async def ecom_products(request: Request, q: str = "", offset: int = 0):
        await admin(request)
        params = {"limit": 50, "offset": max(0, min(offset, 5000))}
        if q.strip():
            params["keyword"] = q.strip()[:80]
        page = await ecwid("GET", "/products", params=params)
        _, rules, _ = await load_setup()
        items = [{
            "id": p.get("id"), "name": p.get("name"), "sku": p.get("sku"), "price": p.get("price"),
            "quantity": p.get("quantity"), "unlimited": p.get("unlimited", False), "enabled": p.get("enabled", True),
            "image": p.get("thumbnailUrl") or p.get("imageUrl"), "url": p.get("url"),
            "supplier": supplier_for(p.get("sku"), rules),
            "ribbon": (p.get("ribbon") or {}).get("text") or "",
        } for p in page.get("items") or []]
        return {"items": items, "total": page.get("total", len(items)), "offset": params["offset"]}

    @app.post("/api/ecom/products/{pid}")
    async def ecom_update_product(pid: int, payload: ProductUpdate, request: Request):
        await admin(request)
        change = {}
        if payload.price is not None:
            if not 0 <= payload.price <= 100000:
                raise HTTPException(status_code=400, detail="Price looks wrong.")
            change["price"] = round(payload.price, 2)
        if payload.quantity is not None:
            change["quantity"] = max(0, min(payload.quantity, 1000000))
            change["unlimited"] = False
        if payload.enabled is not None:
            change["enabled"] = payload.enabled
        if not change:
            raise HTTPException(status_code=400, detail="Nothing to change.")
        await ecwid("PUT", f"/products/{pid}", body=change)
        return {"ok": True}

    async def store_snapshot() -> str:
        """Compact store data for Claude: no street addresses, phones, emails or names."""
        orders, wh = await routed()
        _, rules, _ = await load_setup()
        lines = [f"Today (UTC): {time.strftime('%Y-%m-%d %H:%M')}"]
        lines.append(f"Warehouses ({len(wh)}):")
        for w in wh:
            lines.append(f"- {w['name']} | {'local hub' if w.get('kind') == 'logistics' else 'supplier: ' + (w.get('supplier') or '?')}"
                         f" | {w.get('city')}, {w.get('state')} {w.get('zip')} | {'active' if w.get('active') else 'off'}"
                         f"{' | carries ' + w['categories'] if w.get('categories') else ''}")
        lines.append("SKU rules: " + (", ".join(f"{r['match']}->{r['supplier']}" for r in rules) or "none"))
        lines.append(f"Orders from the last 90 days ({len(orders)}), newest first:")
        for o in sorted(orders, key=lambda x: x["created"] or "", reverse=True)[:150]:
            items = "; ".join(
                f"{i['quantity']}x {i['name']} ({i['sku'] or 'no sku'}, supplier {i['supplier'] or '?'}"
                f"{', BY HAND' if i['by_hand'] else ''}"
                f"{', ships from ' + i['ships_from']['name'] + ' ' + str(i['ships_from']['miles']) + 'mi ' + i['ships_from']['days'] if i['ships_from'] else ''})"
                for i in o["items"])
            lines.append(f"- #{o['number']} | {o['created']} | {o['channel']} | ${o['total']} | pay {o['payment']} | {o['status']}"
                         f" | {(zip_point(o['zip']) or {}).get('city', '?')}, {o['state']} {o['zip']}"
                         f" | hub {o['hub']['name'] + ' ' + str(o['hub']['miles']) + 'mi' if o['hub'] else 'NONE'}"
                         f"{' | tracking ' + o['tracking'] if o['tracking'] else ''} | {items}")
        try:
            page = await ecwid("GET", "/products", params={"limit": 100})
            prods = page.get("items") or []
            lines.append(f"Products (first {len(prods)} of {page.get('total', len(prods))}):")
            for p in prods:
                lines.append(f"- {p.get('name')} | sku {p.get('sku')} | ${p.get('price')} | "
                             f"{'unlimited' if p.get('unlimited') else 'stock ' + str(p.get('quantity'))} | "
                             f"{'for sale' if p.get('enabled', True) else 'hidden'} | supplier {supplier_for(p.get('sku'), rules) or '?'}")
        except HTTPException:
            lines.append("Products: couldn't load.")
        return "\n".join(lines)[:60000]

    @app.get("/api/ecom/claude-credit")
    async def ecom_claude_credit(request: Request):
        await admin(request)
        rows = await db("GET", "ecom_settings", params={"key": "in.(_claude_spend,claude_credit,claude_alert)", "select": "*"}) or []
        v = {r["key"]: r.get("value") for r in rows}
        sp = v.get("_claude_spend") or {}
        credit = v.get("claude_credit") or {}
        added = float(credit.get("amount") or 0)
        spent_since = max(0.0, float(sp.get("spent") or 0) - float(credit.get("spent_at_add") or 0))
        left = round(added - spent_since, 2) if added else None
        alert = float(v.get("claude_alert") or 2)
        empty = bool(sp.get("empty_at") and (not credit.get("at") or sp["empty_at"] > credit["at"]))
        return {"ready": bool(ANTHROPIC_API_KEY), "added": added or None, "spent": round(spent_since, 2), "left": left,
                "alert": alert, "low": empty or (left is not None and left <= alert), "empty": empty,
                "calls": int(sp.get("calls") or 0), "billing_url": BILLING_URL}

    class CreditIn(BaseModel):
        amount: float
        alert: Optional[float] = None

    @app.post("/api/ecom/claude-credit")
    async def ecom_claude_credit_set(payload: CreditIn, request: Request):
        await admin(request)
        if not 0 < payload.amount <= 10000:
            raise HTTPException(status_code=400, detail="Enter the dollar amount you have in Claude credit, like 10.")
        rows = await db("GET", "ecom_settings", params={"key": "eq._claude_spend", "select": "value"}) or []
        sp = (rows[0].get("value") if rows else None) or {}
        await save_setting("claude_credit", {"amount": round(payload.amount, 2), "spent_at_add": float(sp.get("spent") or 0),
                                             "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
        if payload.alert is not None and 0 <= payload.alert <= 1000:
            await save_setting("claude_alert", payload.alert)
        return {"ok": True, "message": f"Got it: ${payload.amount:.2f} of Claude credit. You'll get a reminder when it runs low."}

    @app.get("/api/ecom/ask")
    async def ecom_ask_status(request: Request):
        await admin(request)
        return {"ready": bool(ANTHROPIC_API_KEY)}

    @app.post("/api/ecom/ask")
    async def ecom_ask(payload: AskIn, request: Request):
        user = await admin(request)
        if not ANTHROPIC_API_KEY:
            raise HTTPException(status_code=503, detail="Ask Claude isn't switched on yet. Add ANTHROPIC_API_KEY in Render.")
        q = clean(payload.question, 1500)
        img = None
        if payload.image:
            if payload.image_type not in ("image/jpeg", "image/png", "image/webp") or len(payload.image) > 7_000_000 \
                    or not re.fullmatch(r"[A-Za-z0-9+/=]+", payload.image[:2000]):
                raise HTTPException(status_code=400, detail="That photo couldn't be read. Try taking it again.")
            img = {"type": "image", "source": {"type": "base64", "media_type": payload.image_type, "data": payload.image}}
            q = q or "What is in this photo? Help me use it for my store."
        if not q:
            raise HTTPException(status_code=400, detail="Type a question or add a photo first.")
        now = time.time()
        hits = [t for t in _ask_log.get(user["id"], []) if now - t < 3600]
        if len(hits) >= ASK_LIMIT:
            raise HTTPException(status_code=429, detail="That's 30 questions this hour. Please try again a bit later.")
        _ask_log[user["id"]] = hits + [now]

        messages = []
        for turn in (payload.history or [])[-6:]:
            if isinstance(turn, dict) and turn.get("role") in ("user", "assistant") and isinstance(turn.get("content"), str):
                messages.append({"role": turn["role"], "content": turn["content"][:3000]})
        while messages and messages[0]["role"] != "user":
            messages.pop(0)
        if messages and messages[-1]["role"] == "user":
            messages.pop()
        snapshot = await store_snapshot()
        use_tools = (await load_settings())["ask_actions"]
        text = {"type": "text", "text": f"<store_data>\n{snapshot}\n</store_data>\n\nQuestion: {q}"}
        messages.append({"role": "user", "content": [img, text] if img else [text]})
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                r = await client.post("https://api.anthropic.com/v1/messages", json={
                    "model": ECOM_CLAUDE_MODEL, "max_tokens": 1500, "system": ASK_SYSTEM, "messages": messages,
                    **({"tools": ASK_TOOLS} if use_tools else {}),
                }, headers={"x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"})
        except httpx.HTTPError:
            raise HTTPException(status_code=502, detail="Couldn't reach Claude. Try again in a minute.")
        if claude_out_of_credit(r):
            await note_claude_usage(empty=True)
            raise HTTPException(status_code=402, detail="Your Claude credit has run out. Add funds at console.anthropic.com (Billing), then try again.")
        if r.status_code == 401:
            raise HTTPException(status_code=502, detail="Claude refused the API key. Check ANTHROPIC_API_KEY in Render.")
        if r.status_code == 429:
            raise HTTPException(status_code=429, detail="Claude is busy or your spending limit was reached. Try again shortly.")
        if r.status_code >= 400:
            logger.error("Ask Claude failed: %s %s", r.status_code, r.text[:300])
            raise HTTPException(status_code=502, detail="Claude couldn't answer right now. Try again in a minute.")
        data = r.json()
        await note_claude_usage(data)
        answer = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text").strip()
        actions = [{"type": b["name"], "input": b.get("input") or {}, "summary": action_summary(b["name"], b.get("input") or {})}
                   for b in data.get("content", []) if b.get("type") == "tool_use" and b.get("name") in {t["name"] for t in ASK_TOOLS}]
        if not answer and actions:
            answer = "Here's what I can set up. Tap Confirm on each one you want."
        return {"answer": answer or "No answer came back. Try asking another way.", "actions": actions[:8]}

    async def ecwid_image(pid: int, b64: str, ctype: str) -> bool:
        import base64 as _b
        try:
            raw = _b.b64decode(b64, validate=True)
            async with httpx.AsyncClient(timeout=30) as client:
                r = await client.post(f"https://app.ecwid.com/api/v3/{ECWID_STORE_ID}/products/{pid}/image", content=raw,
                                      headers={"Authorization": f"Bearer {ECWID_SECRET_TOKEN}", "Content-Type": ctype})
            return r.status_code < 300
        except (httpx.HTTPError, ValueError):
            return False

    @app.post("/api/ecom/action")
    async def ecom_action(payload: ActionIn, request: Request):
        await admin(request)
        if not (await load_settings())["ask_actions"]:
            raise HTTPException(status_code=403, detail="Turn on \"Ask Claude can set things up\" in the Automation tab first.")
        t, i = payload.type, payload.input or {}
        if t == "add_warehouse":
            return await ecom_save_warehouse(WarehouseIn(name=str(i.get("name", "")), kind=str(i.get("kind", "logistics")),
                                                         supplier=str(i.get("supplier", "")), zip=str(i.get("zip", "")),
                                                         categories=str(i.get("categories", ""))), request) | {"message": "Warehouse added."}
        if t == "add_sku_rule":
            await ecom_save_rule(RuleIn(match=str(i.get("match", "")), supplier=str(i.get("supplier", ""))), request)
            return {"ok": True, "message": "SKU rule added."}
        if t == "create_product":
            try:
                price = round(float(i.get("price")), 2)
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail="The price is missing.")
            name, sku = clean(i.get("name"), 200), clean(i.get("sku"), 60).upper()
            if not name or not sku or not 0 <= price <= 100000:
                raise HTTPException(status_code=400, detail="The product needs a name, SKU and price.")
            body = {"name": name, "sku": sku, "price": price, "enabled": True,
                    "description": "".join(f"<p>{_html_escape(x)}</p>" for x in clean(i.get("description"), 3000).split("\n") if x.strip())}
            if isinstance(i.get("quantity"), int):
                body.update(quantity=max(0, i["quantity"]), unlimited=False)
            else:
                body["unlimited"] = True
            wh_, rules_, _ = await load_setup()
            rib = ribbon_for(sku, wh_, rules_)
            if rib:
                body["ribbon"] = {"text": rib, "color": RIBBON_COLOR}
            if i.get("category"):
                cid = await category_id_for(str(i["category"]))
                if cid:
                    body["categoryIds"] = [cid]
            made = await ecwid("POST", "/products", body=body)
            pid = made.get("id")
            pic = ""
            if pid and i.get("use_photo") and payload.image and payload.image_type in ("image/jpeg", "image/png", "image/webp"):
                pic = " with your photo" if await ecwid_image(pid, payload.image, payload.image_type) else " (the photo didn't upload; add it in Ecwid)"
            return {"ok": True, "message": f"Product created in Ecwid{pic}.", "id": pid}
        if t == "update_order":
            num = str(i.get("order_number", ""))
            await ecom_update_order(num, OrderUpdate(status=str(i.get("status") or ""),
                                                     tracking=(str(i["tracking"]) if i.get("tracking") else None)), request)
            return {"ok": True, "message": f"Order #{num} updated."}
        if t == "mark_supplier_ordered":
            num = str(i.get("order_number", ""))
            orders, _ = await routed()
            o = next((x for x in orders if str(x["number"]) == num), None)
            if not o:
                raise HTTPException(status_code=404, detail=f"Order #{num} wasn't found.")
            return await ecom_mark_ordered(o["id"], OrderedIn(supplier=str(i.get("supplier", "")), ref=str(i.get("ref", ""))), request)
        raise HTTPException(status_code=400, detail="Unknown action.")

    # ----- Automation tab -----
    @app.get("/api/ecom/automation")
    async def ecom_automation(request: Request):
        await admin(request)
        st = await load_settings()
        return {"switches": [{**sw, "on": st[sw["key"]], "blocked": switch_ready(sw["key"])} for sw in SWITCHES],
                "last_run": st["_last_run"], "last_daily": st["_last_daily"], "every_minutes": TICK_SECONDS // 60}

    @app.post("/api/ecom/automation")
    async def ecom_set_switch(payload: SwitchIn, request: Request):
        await admin(request)
        if payload.key not in {sw["key"] for sw in SWITCHES}:
            raise HTTPException(status_code=400, detail="Unknown switch.")
        if payload.on and switch_ready(payload.key):
            raise HTTPException(status_code=400, detail=switch_ready(payload.key))
        await save_setting(payload.key, payload.on)
        return {"ok": True}

    @app.post("/api/ecom/automation/run")
    async def ecom_run_now(request: Request):
        await admin(request)
        rep = await tick(force=True)
        if not rep:
            raise HTTPException(status_code=409, detail="A check is already running. Try again in a moment.")
        return rep

    # ----- To-order list -----
    @app.get("/api/ecom/to-order")
    async def ecom_to_order(request: Request):
        await admin(request)
        orders, _ = await routed()
        groups: dict = {}
        for o in sorted(orders, key=lambda x: x["created"] or ""):
            for sup in needs_ordering(o):
                groups.setdefault(sup, []).append({**o, "items": [i for i in o["items"] if (i["supplier"] or "Unknown supplier") == sup]})
        return {"groups": [{"supplier": k, "orders": v} for k, v in sorted(groups.items(), key=lambda kv: -len(kv[1]))]}

    @app.post("/api/ecom/orders/{oid}/ordered")
    async def ecom_mark_ordered(oid: str, payload: OrderedIn, request: Request):
        await admin(request)
        if not re.fullmatch(r"[A-Za-z0-9-]{1,40}", oid):
            raise HTTPException(status_code=404, detail="Not found.")
        sup = clean(payload.supplier, 60)
        if not sup:
            raise HTTPException(status_code=400, detail="Which supplier?")
        rows = await db("GET", "ecom_order_meta", params={"order_id": f"eq.{oid}", "select": "ordered"}) or []
        ordered = dict((rows[0].get("ordered") if rows else None) or {})
        if payload.undo:
            ordered.pop(sup, None)
        else:
            ordered[sup] = {"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "ref": clean(payload.ref, 80)}
        await save_meta(oid, {"ordered": ordered})
        return {"ok": True, "message": f"Marked as ordered from {sup}." if not payload.undo else "Undone."}
