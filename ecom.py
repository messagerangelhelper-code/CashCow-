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

Database: ecom.sql (tables ecom_warehouses, ecom_sku_rules, ecom_order_meta).
ZIP locations: ecom_zips.json, from the "zipcodes" project by Dav Glass (BSD license).
"""
import json
import math
import os
import re
import time
from pathlib import Path
from typing import Optional

import httpx
from fastapi import HTTPException, Request
from pydantic import BaseModel

ECWID_STORE_ID = (os.environ.get("ECWID_STORE_ID") or "").strip()
ECWID_SECRET_TOKEN = (os.environ.get("ECWID_SECRET_TOKEN") or "").strip()
ECOM_ADMIN_EMAILS = {e.strip().lower() for e in (os.environ.get("ECOM_ADMIN_EMAILS") or os.environ.get("MAIL_ADMIN_EMAILS") or "").split(",") if e.strip()}

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
        raise HTTPException(status_code=502, detail="Ecwid refused the token. Check ECWID_STORE_ID and ECWID_SECRET_TOKEN in Render.")
    if r.status_code == 404:
        raise HTTPException(status_code=404, detail="Not found in Ecwid.")
    if r.status_code >= 400:
        _ctx["logger"].error("Ecwid %s %s failed: %s", method, path, r.status_code)
        raise HTTPException(status_code=502, detail=f"Ecwid said no ({r.status_code}).")
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
            "by_hand": bool(sup and not pool),
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


class ProductUpdate(BaseModel):
    price: Optional[float] = None
    quantity: Optional[int] = None
    enabled: Optional[bool] = None


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
        orders = await fetch_orders(days)
        wh, rules, meta = await load_setup()
        return [route_order(o, wh, rules, meta) for o in orders], wh

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
