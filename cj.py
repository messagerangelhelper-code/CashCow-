"""
Fill store shelves from CJdropshipping, automatically.

For a shelf (like "Everyday Essentials > Oral Care") the dashboard searches CJ's catalog for products
stocked in US warehouses, prices them with your markup rule, and after you approve, creates them in
Ecwid with photo, description, SKU (CJ-...), shelf and "Ships from" ribbon.

Render setting: CJ_API_KEY (from your CJdropshipping account → API). Free.
CJ allows about 1 request per second, so imports run in small batches.
"""
import asyncio
import math
import os
import re
import time
from typing import Optional

import httpx
from fastapi import HTTPException, Request
from pydantic import BaseModel

import ecom
from ecom import db, ecwid, clean, save_setting, load_setup, ribbon_for, RIBBON_COLOR, SECTIONS

CJ = "https://developers.cjdropshipping.com/api2.0/v1"
_ctx: dict = {}
_lock = asyncio.Lock()
_last = {"t": 0.0}

# What to search for on each shelf (several words = several searches, best first).
SHELF_WORDS = {
    "Oral Care": ["toothbrush", "electric toothbrush", "water flosser"],
    "Bath & Body": ["bath sponge", "body scrubber", "soap dish", "shower"],
    "Hair Care": ["hair brush", "hair clips", "scalp massager"],
    "Paper Goods": ["napkins", "tissue box", "paper towel holder"],
    "Bathroom Cleaning": ["toilet brush", "bathroom cleaning brush", "shower squeegee"],
    "Kitchen & Dish": ["dish brush", "kitchen sponge", "dish rack"],
    "Laundry": ["laundry basket", "lint remover", "laundry bag"],
    "First Aid & Personal Care": ["first aid kit", "pill organizer", "nail clipper"],
    "Candy": ["candy", "lollipop"],
    "Nostalgia Candy": ["retro candy", "candy dispenser"],
    "Snacks": ["snack", "snack container"],
    "Drinks & Treats": ["drink bottle", "tumbler"],
    "Freeze-Dried Treats": ["freeze dried"],
    "Party & Dollar Items": ["party favors", "party toys"],
    "Party Supplies": ["party plates", "tablecloth party", "party cups"],
    "Goodie Bags & Favors": ["goodie bags", "gift bags", "party favor bags"],
    "Balloons & Decorations": ["balloons", "party banner", "balloon garland"],
    "Scented Candles": ["scented candle", "aromatherapy candle"],
    "Holiday Candles": ["christmas candle", "led candle christmas"],
    "Carved & Pillar Candles": ["pillar candle", "carved candle"],
    "Gift Sets": ["candle gift set", "gift box"],
    "Wax Melts": ["wax melt", "wax warmer"],
}


def cj_ready() -> bool:
    return bool((os.environ.get("CJ_API_KEY") or "").strip())


def _num(v) -> float:
    m = re.search(r"\d+(\.\d+)?", str(v or ""))
    return float(m.group()) if m else 0.0


def price_rule(cost: float, markup: float) -> float:
    """cost × markup, rounded up to the next .99 (never below cost + $1)."""
    p = max(cost * markup, cost + 1)
    return round(math.floor(p) + 0.99, 2)


async def _wait_turn():
    gap = time.time() - _last["t"]
    if gap < 1.1:
        await asyncio.sleep(1.1 - gap)
    _last["t"] = time.time()


async def token() -> str:
    rows = await db("GET", "ecom_settings", params={"key": "eq._cj_token", "select": "value"}) or []
    tok = (rows[0].get("value") if rows else None) or {}
    if tok.get("token") and tok.get("until", 0) > time.time() + 86400:
        return tok["token"]
    key = (os.environ.get("CJ_API_KEY") or "").strip()
    if not key:
        raise HTTPException(status_code=503, detail="Add CJ_API_KEY in Render first.")
    async with _lock:
        await _wait_turn()
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                r = await client.post(f"{CJ}/authentication/getAccessToken", json={"apiKey": key})
            d = r.json()
        except (httpx.HTTPError, ValueError):
            raise HTTPException(status_code=502, detail="Couldn't reach CJ. Try again in a minute.")
    if not d.get("result") or not (d.get("data") or {}).get("accessToken"):
        _ctx["logger"].error("CJ token refused: %s", str(d.get("message"))[:200])
        raise HTTPException(status_code=502, detail=f"CJ refused the API key: {d.get('message') or 'check CJ_API_KEY in Render'}.")
    tok = {"token": d["data"]["accessToken"], "until": time.time() + 170 * 86400}
    await save_setting("_cj_token", tok)
    return tok["token"]


async def cj_get(path: str, params: dict) -> dict:
    tok = await token()
    async with _lock:
        await _wait_turn()
        try:
            async with httpx.AsyncClient(timeout=25) as client:
                r = await client.get(f"{CJ}{path}", params=params, headers={"CJ-Access-Token": tok})
            d = r.json()
        except (httpx.HTTPError, ValueError):
            raise HTTPException(status_code=502, detail="Couldn't reach CJ. Try again in a minute.")
    if not d.get("result"):
        if str(d.get("code")) in ("1600001", "1600003"):        # token expired or invalid: get a fresh one next time
            await save_setting("_cj_token", {})
        raise HTTPException(status_code=502, detail=f"CJ said: {d.get('message') or 'no'}")
    return d.get("data") or {}


def _plain(html: str) -> str:
    t = re.sub(r"(?is)<(script|style).*?</\1>", " ", html or "")
    t = re.sub(r"(?i)<br\s*/?>|</p>|</li>", "\n", t)
    t = re.sub(r"<[^>]+>", " ", t)
    t = re.sub(r"[ \t\r\f\v]+", " ", t)
    lines = [x.strip() for x in t.split("\n") if x.strip()]
    return "\n".join(lines)[:1500]


async def nice_copy(name: str, raw: str) -> tuple:
    """A clean title and a short description, written by Claude when the key is set."""
    if not ecom.ANTHROPIC_API_KEY:
        return name[:120], raw[:600]
    prompt = (f"Supplier product title: {name}\nSupplier description: {raw[:1200]}\n\n"
              "Write a store title (max 70 characters, plain words, no brand names you aren't sure of, no ALL CAPS) and a "
              "2-3 sentence plain-English description for shoppers. Don't promise delivery times or health benefits. "
              "Reply exactly as:\nTITLE: ...\nDESCRIPTION: ...")
    try:
        async with httpx.AsyncClient(timeout=40) as client:
            r = await client.post("https://api.anthropic.com/v1/messages", json={
                "model": ecom.ECOM_CLAUDE_MODEL, "max_tokens": 300, "messages": [{"role": "user", "content": prompt}]},
                headers={"x-api-key": ecom.ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01"})
        if ecom.claude_out_of_credit(r):
            await ecom.note_claude_usage(empty=True)
            return name[:120], raw[:600]
        await ecom.note_claude_usage(r.json())
        text = "".join(b.get("text", "") for b in r.json().get("content", []) if b.get("type") == "text")
        t = re.search(r"TITLE:\s*(.+)", text)
        dsc = re.search(r"DESCRIPTION:\s*(.+)", text, re.S)
        if t and dsc:
            return t.group(1).strip()[:120], dsc.group(1).strip()[:800]
    except Exception:
        pass
    return name[:120], raw[:600]


async def upload_image_from_url(pid: int, url: str) -> bool:
    if not url or not url.startswith("https://"):
        return False
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            img = await client.get(url)
            if img.status_code != 200 or len(img.content) > 8_000_000:
                return False
            ctype = img.headers.get("content-type", "image/jpeg").split(";")[0]
            if ctype not in ("image/jpeg", "image/png", "image/webp", "image/gif"):
                ctype = "image/jpeg"
            r = await client.post(f"https://app.ecwid.com/api/v3/{ecom.ECWID_STORE_ID}/products/{pid}/image", content=img.content,
                                  headers={"Authorization": f"Bearer {ecom.ECWID_SECRET_TOKEN}", "Content-Type": ctype})
        return r.status_code < 300
    except httpx.HTTPError:
        return False


class SearchIn(BaseModel):
    shelf: str                 # "Everyday Essentials > Oral Care"
    keyword: str = ""
    markup: float = 2.0
    limit: int = 12


class ImportItem(BaseModel):
    pid: str
    price: float


class ImportIn(BaseModel):
    shelf: str
    items: list[ImportItem]
    write_copy: bool = True


def register(app, *, admin, logger):
    _ctx.update(logger=logger)

    async def store_skus() -> set:
        out, offset = set(), 0
        while offset < 3000:
            page = await ecwid("GET", "/products", params={"limit": 100, "offset": offset})
            items = page.get("items") or []
            out |= {(p.get("sku") or "").upper() for p in items}
            if len(items) < 100:
                break
            offset += 100
        return out

    @app.get("/api/ecom/cj")
    async def cj_status(request: Request):
        await admin(request)
        rows = await db("GET", "ecom_settings", params={"key": "eq.cj_markup", "select": "value"}) or []
        shelves = [{"path": f"{t['name']} > {sh}", "section": t["name"], "shelf": sh, "icon": t["icon"], "words": SHELF_WORDS.get(sh, [sh.lower()])}
                   for t in SECTIONS.values() for sh in t["shelves"]]
        return {"ready": cj_ready(), "markup": float((rows[0]["value"] if rows else None) or 2.0), "shelves": shelves}

    @app.post("/api/ecom/cj/search")
    async def cj_search(payload: SearchIn, request: Request):
        await admin(request)
        if not cj_ready():
            raise HTTPException(status_code=503, detail="Add CJ_API_KEY in Render first.")
        if not 1.1 <= payload.markup <= 10:
            raise HTTPException(status_code=400, detail="Use a markup between 1.1 and 10.")
        await save_setting("cj_markup", payload.markup)
        shelf = payload.shelf.split(">")[-1].strip()
        words = [payload.keyword.strip()[:60]] if payload.keyword.strip() else SHELF_WORDS.get(shelf, [shelf.lower()])
        have = await store_skus()
        seen, out = set(), []
        for w in words[:3]:
            data = await cj_get("/product/listV2", {"keyWord": w, "page": 1, "size": 30, "countryCode": "US"})
            for block in data.get("content") or []:
                for p in block.get("productList") or []:
                    pid = p.get("id")
                    stock = int(_num(p.get("warehouseInventoryNum")))
                    if not pid or pid in seen or stock <= 0:
                        continue
                    seen.add(pid)
                    cost = _num(p.get("nowPrice")) or _num(p.get("sellPrice"))
                    if cost <= 0:
                        continue
                    sku = f"CJ-{(p.get('sku') or pid[:8]).upper()}"[:60]
                    out.append({"pid": pid, "name": (p.get("nameEn") or "")[:150], "image": p.get("bigImage") or "",
                                "cost": round(cost, 2), "price": price_rule(cost, payload.markup), "us_stock": stock,
                                "sku": sku, "in_store": sku in have})
            if len(out) >= payload.limit:
                break
        out.sort(key=lambda x: (x["in_store"], -min(x["us_stock"], 500)))
        return {"shelf": payload.shelf, "items": out[:max(1, min(payload.limit, 30))]}

    @app.post("/api/ecom/cj/import")
    async def cj_import(payload: ImportIn, request: Request):
        await admin(request)
        if not cj_ready():
            raise HTTPException(status_code=503, detail="Add CJ_API_KEY in Render first.")
        if not payload.items or len(payload.items) > 6:
            raise HTTPException(status_code=400, detail="Send 1 to 6 products at a time.")
        cid = await ecom.category_id_for(payload.shelf)
        wh, rules, _ = await load_setup()
        if not ecom.supplier_for("CJ-X", rules):
            await db("POST", "ecom_sku_rules", params={"on_conflict": "match"}, body={"match": "CJ-", "supplier": "CJ"},
                     prefer="resolution=merge-duplicates,return=minimal")
            rules = rules + [{"match": "CJ-", "supplier": "CJ"}]
        have = await store_skus()
        results = []
        for it in payload.items:
            try:
                d = await cj_get("/product/query", {"pid": it.pid, "countryCode": "US"})
                variants = d.get("variants") or []
                v = next((x for x in variants if any(str(i.get("countryCode")) == "US" and int(_num(i.get("totalInventory"))) > 0
                                                     for i in (x.get("inventories") or []))), variants[0] if variants else {})
                sku = f"CJ-{(v.get('variantSku') or d.get('productSku') or it.pid[:8]).upper()}"[:60]
                if sku in have:
                    results.append({"pid": it.pid, "ok": False, "message": "Already in your store."})
                    continue
                cost = _num(v.get("variantSellPrice")) or _num(d.get("sellPrice"))
                if not 0 < it.price <= 100000 or it.price < cost:
                    results.append({"pid": it.pid, "ok": False, "message": f"Price is below CJ's cost (${cost:.2f})."})
                    continue
                name, desc = d.get("productNameEn") or "", _plain(d.get("description") or "")
                if payload.write_copy:
                    name, desc = await nice_copy(name, desc)
                body = {"name": clean(name, 200) or "New product", "sku": sku, "price": round(it.price, 2), "enabled": True,
                        "unlimited": True, "categoryIds": [cid] if cid else [],
                        "description": "".join(f"<p>{ecom._html_escape(x)}</p>" for x in clean(desc, 3000).split("\n") if x.strip()),
                        "costPrice": round(cost, 2)}
                rib = ribbon_for(sku, wh, rules)
                if rib:
                    body["ribbon"] = {"text": rib, "color": RIBBON_COLOR}
                made = await ecwid("POST", "/products", body=body)
                pic = await upload_image_from_url(made.get("id"), v.get("variantImage") or d.get("bigImage") or "")
                have.add(sku)
                results.append({"pid": it.pid, "ok": True, "name": body["name"], "sku": sku, "photo": pic,
                                "message": "Added" + ("" if pic else " (photo didn't copy; add it in Ecwid)")})
            except HTTPException as e:
                results.append({"pid": it.pid, "ok": False, "message": str(e.detail)})
        return {"results": results, "added": sum(1 for r in results if r["ok"])}
