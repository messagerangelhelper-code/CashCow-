"""
The Candy Lady: one store, a crew of free sellers.

Anyone with a CashCow account can join free and gets a code (an Ecwid discount coupon) that gives
customers 5% off. Every paid order that used her code earns her 10% of the order (before shipping and
tax). Sales are recorded from Ecwid orders by the eCommerce dashboard's 5-minute check, so earnings are
kept even after orders age out of Ecwid's 90-day window. You pay Candy Ladies yourself (Cash App,
PayPal, Zelle) and mark them paid in the dashboard.

Database: candy.sql. Settings live in ecom_settings (candy_discount, candy_cut, candy_url).
"""
import asyncio
import re
import secrets
import time

from fastapi import HTTPException, Request
from typing import Optional

from pydantic import BaseModel

import ecom
from ecom import db, ecwid, clean, save_setting

DEFAULTS = {"candy_discount": 5, "candy_cut": 10, "candy_url": "", "candy_credit_bonus": 20}
PAYABLE = {"SHIPPED", "OUT_FOR_DELIVERY", "DELIVERED", "READY_FOR_PICKUP"}     # earned once it's on its way
VOID = {"WILL_NOT_DELIVER", "RETURNED"}
MIN_PAYOUT = 10.0
CREDIT = "Store credit"
PAYOUT_METHODS = [CREDIT, "Cash App", "PayPal", "Zelle", "Venmo", "Check"]
_ctx: dict = {}


async def candy_settings() -> dict:
    rows = await db("GET", "ecom_settings", params={"select": "*", "key": "like.candy_*"}) or []
    vals = {r["key"]: r.get("value") for r in rows}
    out = {k: vals.get(k, v) for k, v in DEFAULTS.items()}
    out["candy_discount"] = float(out["candy_discount"] or 0)
    out["candy_cut"] = float(out["candy_cut"] or 0)
    out["candy_credit_bonus"] = float(out["candy_credit_bonus"] or 0)
    return out


def credit_value(owed: float, bonus_pct: float) -> float:
    return round(owed * (1 + bonus_pct / 100), 2)


def order_code(o: dict) -> str:
    c = o.get("discountCoupon") or {}
    return str(c.get("code") or "").strip().upper()


def commission_base(o: dict) -> float:
    """What the customer paid for the goods: total minus tax and shipping."""
    ship = (o.get("shippingOption") or {}).get("shippingRate") or 0
    try:
        return round(max(0.0, float(o.get("total") or 0) - float(o.get("tax") or 0) - float(ship)), 2)
    except (TypeError, ValueError):
        return 0.0


async def record_sales(raw_orders: list) -> int:
    """Called from the dashboard's 5-minute check: save or update every order that used a Candy Lady code."""
    coded = [o for o in raw_orders if order_code(o)]
    if not coded:
        return 0
    sellers = await db("GET", "candy_sellers", params={"select": "id,code"}) or []
    by_code = {s["code"]: s["id"] for s in sellers}
    if not by_code:
        return 0
    st = await candy_settings()
    existing = {r["order_id"]: r for r in (await db("GET", "candy_sales", params={"select": "order_id,cut_pct,paid_out"}) or [])}
    rows = []
    for o in coded:
        sid = by_code.get(order_code(o))
        if not sid:
            continue
        oid = str(o.get("id") or o.get("orderNumber"))
        old = existing.get(oid) or {}
        cut = float(old.get("cut_pct") or st["candy_cut"])        # the rate when the sale happened stays fixed
        base = commission_base(o)
        status = "void" if o.get("fulfillmentStatus") in VOID or o.get("paymentStatus") in ("REFUNDED", "CANCELLED") \
            else "payable" if o.get("paymentStatus") == "PAID" and o.get("fulfillmentStatus") in PAYABLE \
            else "pending" if o.get("paymentStatus") in ("PAID", "PARTIALLY_REFUNDED") else "unpaid"
        rows.append({"order_id": oid, "order_number": o.get("orderNumber"), "seller_id": sid, "code": order_code(o),
                     "base": base, "cut_pct": cut, "earned": round(base * cut / 100, 2), "status": status,
                     "ordered_at": (o.get("createDate") or "")[:19] or None})
    if rows:
        await db("POST", "candy_sales", params={"on_conflict": "order_id"}, body=rows,
                 prefer="resolution=merge-duplicates,return=minimal")
    return len(rows)


async def seller_totals(seller_ids=None) -> dict:
    params = {"select": "seller_id,earned,status,paid_out"}
    sales = await db("GET", "candy_sales", params=params) or []
    out: dict = {}
    for s in sales:
        if seller_ids and s["seller_id"] not in seller_ids:
            continue
        t = out.setdefault(s["seller_id"], {"orders": 0, "pending": 0.0, "owed": 0.0, "paid": 0.0, "lifetime": 0.0})
        if s["status"] == "void":
            continue
        t["orders"] += 1
        amt = float(s["earned"] or 0)
        if s["paid_out"]:
            t["paid"] += amt
            t["lifetime"] += amt
        elif s["status"] == "payable":
            t["owed"] += amt
            t["lifetime"] += amt
        elif s["status"] == "pending":
            t["pending"] += amt
    for t in out.values():
        for k in ("pending", "owed", "paid", "lifetime"):
            t[k] = round(t[k], 2)
    return out


class JoinIn(BaseModel):
    name: str
    code: str
    phone: str = ""
    payout_method: str = "Cash App"
    payout_handle: str = ""
    agree: bool = False


class SellerUpdate(BaseModel):
    status: str = ""
    payout_method: str = ""
    payout_handle: str = ""


class CandySettingsIn(BaseModel):
    discount: float
    cut: float
    url: str = ""
    bonus: Optional[float] = None


async def make_coupon(code: str, name: str, discount: float) -> dict:
    return await ecwid("POST", "/discount_coupons", body={
        "name": f"Candy Lady {name}"[:100], "code": code, "discountType": "PERCENT", "discount": discount,
        "status": "ACTIVE", "usesLimit": "UNLIMITED", "applicationLimit": "UNLIMITED"})


_retry = {"at": 0.0}


async def activate_waiting_codes() -> int:
    """Hourly: make the Ecwid coupon for Candy Ladies who joined before the store could have coupons."""
    if time.time() - _retry["at"] < 3600:
        return 0
    _retry["at"] = time.time()
    waiting = await db("GET", "candy_sellers", params={"select": "*", "coupon_id": "is.null", "status": "eq.active"}) or []
    if not waiting:
        return 0
    st = await candy_settings()
    done = 0
    for s in waiting:
        try:
            made = await make_coupon(s["code"], s["name"], st["candy_discount"])
        except HTTPException:
            break                  # still not allowed: try again next hour
        await db("PATCH", "candy_sellers", params={"id": f"eq.{s['id']}"}, body={"coupon_id": made.get("id")}, prefer="return=minimal")
        done += 1
    return done


async def set_coupon(seller: dict, status: str = "", discount: float = None):
    if not seller.get("coupon_id"):
        return
    body = {}
    if status:
        body["status"] = status
    if discount is not None:
        body.update(discountType="PERCENT", discount=discount)
    if body:
        await ecwid("PUT", f"/discount_coupons/{seller['coupon_id']}", body=body)


def register(app, *, admin, account_user, logger):
    _ctx.update(logger=logger)

    async def me(request: Request) -> dict:
        user = await account_user(request.headers.get("authorization", ""))
        if not user:
            raise HTTPException(status_code=401, detail="Please sign in first.")
        return user

    async def my_seller(user) -> dict:
        rows = await db("GET", "candy_sellers", params={"user_id": f"eq.{user['id']}", "select": "*"}) or []
        return rows[0] if rows else {}

    # ----- Candy Lady's own page -----
    @app.get("/api/candy/me")
    async def candy_me(request: Request):
        user = await me(request)
        st = await candy_settings()
        s = await my_seller(user)
        out = {"discount": st["candy_discount"], "cut": st["candy_cut"], "methods": PAYOUT_METHODS, "min_payout": MIN_PAYOUT,
               "credit_bonus": st["candy_credit_bonus"],
               "shop_url": st["candy_url"] or "", "seller": None}
        if not out["shop_url"] and ecom.ecwid_ready():
            try:
                prof = await ecom.cached("profile", 3600, lambda: ecwid("GET", "/profile"))
                out["shop_url"] = (prof.get("generalInfo") or {}).get("storeUrl") or ""
            except HTTPException:
                pass
        if s:
            t = (await seller_totals({s["id"]})).get(s["id"], {"orders": 0, "pending": 0, "owed": 0, "paid": 0, "lifetime": 0})
            sales = await db("GET", "candy_sales", params={"seller_id": f"eq.{s['id']}", "select": "order_number,earned,status,paid_out,ordered_at",
                                                           "order": "ordered_at.desc", "limit": "50"}) or []
            payouts = await db("GET", "candy_payouts", params={"seller_id": f"eq.{s['id']}", "select": "amount,method,paid_at",
                                                               "order": "paid_at.desc", "limit": "20"}) or []
            out["seller"] = {k: s.get(k) for k in ("name", "code", "status", "payout_method", "payout_handle", "created_at")}
            out["seller"]["waiting"] = not s.get("coupon_id")
            out.update(totals=t, sales=sales, payouts=payouts)
        return out

    @app.post("/api/candy/join")
    async def candy_join(payload: JoinIn, request: Request):
        user = await me(request)
        if await my_seller(user):
            raise HTTPException(status_code=400, detail="You're already a Candy Lady.")
        if not payload.agree:
            raise HTTPException(status_code=400, detail="Please read and agree to the Candy Lady terms.")
        name = clean(payload.name, 60)
        code = re.sub(r"[^A-Z0-9]", "", (payload.code or "").upper())
        if not name:
            raise HTTPException(status_code=400, detail="Add your name.")
        if not 4 <= len(code) <= 16:
            raise HTTPException(status_code=400, detail="Your code needs 4 to 16 letters or numbers, like MSJONES.")
        if payload.payout_method not in PAYOUT_METHODS:
            raise HTTPException(status_code=400, detail="Pick how you'd like to be paid.")
        taken = await db("GET", "candy_sellers", params={"code": f"eq.{code}", "select": "id"}) or []
        if taken:
            raise HTTPException(status_code=409, detail="That code is taken. Try another, like adding your street or a number.")
        st = await candy_settings()
        made, waiting = {}, False
        try:
            made = await make_coupon(code, name, st["candy_discount"])
        except HTTPException as e:
            logger.error("Candy Lady code %s not made: %s", code, e.detail)
            if "already" in str(e.detail).lower() or "exist" in str(e.detail).lower():
                raise HTTPException(status_code=409, detail="That code already exists in the store. Try another.")
            waiting = True     # e.g. the store's plan doesn't include coupons yet: save her, switch the code on later
        await db("POST", "candy_sellers", body={
            "user_id": user["id"], "email": user.get("email") or "", "name": name, "code": code, "coupon_id": made.get("id"),
            "phone": clean(payload.phone, 30), "payout_method": payload.payout_method,
            "payout_handle": clean(payload.payout_handle, 80), "status": "active"}, prefer="return=minimal")
        logger.info("New Candy Lady joined: %s%s", code, " (code waiting for Ecwid)" if waiting else "")
        return {"ok": True, "code": code, "waiting": waiting}

    @app.post("/api/candy/me")
    async def candy_update_me(payload: SellerUpdate, request: Request):
        user = await me(request)
        s = await my_seller(user)
        if not s:
            raise HTTPException(status_code=404, detail="Join first.")
        change = {}
        if payload.payout_method:
            if payload.payout_method not in PAYOUT_METHODS:
                raise HTTPException(status_code=400, detail="Pick how you'd like to be paid.")
            change["payout_method"] = payload.payout_method
        change["payout_handle"] = clean(payload.payout_handle, 80)
        await db("PATCH", "candy_sellers", params={"id": f"eq.{s['id']}"}, body=change, prefer="return=minimal")
        return {"ok": True}

    # ----- Admin (dashboard 🍬 tab) -----
    @app.get("/api/candy/admin")
    async def candy_admin(request: Request):
        await admin(request)
        st = await candy_settings()
        sellers = await db("GET", "candy_sellers", params={"select": "*", "order": "created_at.desc"}) or []
        totals = await seller_totals()
        empty = {"orders": 0, "pending": 0, "owed": 0, "paid": 0, "lifetime": 0}
        rows = [{**{k: s.get(k) for k in ("id", "name", "code", "email", "phone", "status", "payout_method", "payout_handle", "created_at")},
                 **totals.get(s["id"], empty)} for s in sellers]
        ids_waiting = {s["id"] for s in sellers if not s.get("coupon_id")}
        for r in rows:
            r["waiting"] = r["id"] in ids_waiting
            r["credit"] = credit_value(r["owed"], st["candy_credit_bonus"]) if r.get("payout_method") == CREDIT else None
        return {"settings": st, "sellers": rows, "min_payout": MIN_PAYOUT,
                "to_pay": [r for r in rows if r["owed"] >= MIN_PAYOUT and r["status"] == "active"]}

    @app.post("/api/candy/admin/settings")
    async def candy_admin_settings(payload: CandySettingsIn, request: Request):
        await admin(request)
        if not 0 <= payload.discount <= 50 or not 0 <= payload.cut <= 50:
            raise HTTPException(status_code=400, detail="Use a number from 0 to 50 for each percent.")
        url = clean(payload.url, 300)
        if url and not url.startswith("https://"):
            raise HTTPException(status_code=400, detail="The store link must start with https://")
        old = await candy_settings()
        await save_setting("candy_discount", payload.discount)
        await save_setting("candy_cut", payload.cut)
        await save_setting("candy_url", url)
        if payload.bonus is not None:
            if not 0 <= payload.bonus <= 100:
                raise HTTPException(status_code=400, detail="Use a bonus from 0 to 100 percent.")
            await save_setting("candy_credit_bonus", payload.bonus)
        changed = 0
        if payload.discount != old["candy_discount"]:
            for s in await db("GET", "candy_sellers", params={"select": "*", "status": "eq.active"}) or []:
                try:
                    await set_coupon(s, discount=payload.discount)
                    changed += 1
                except HTTPException:
                    pass
        return {"ok": True, "message": "Saved." + (f" Updated {changed} code{'s' if changed != 1 else ''} in Ecwid." if changed else "")}

    @app.post("/api/candy/admin/sellers/{sid}")
    async def candy_admin_seller(sid: str, payload: SellerUpdate, request: Request):
        await admin(request)
        if not re.fullmatch(r"[0-9a-f-]{36}", sid) or payload.status not in ("active", "paused"):
            raise HTTPException(status_code=400, detail="Unknown change.")
        rows = await db("GET", "candy_sellers", params={"id": f"eq.{sid}", "select": "*"}) or []
        if not rows:
            raise HTTPException(status_code=404, detail="Not found.")
        await set_coupon(rows[0], status="ACTIVE" if payload.status == "active" else "PAUSED")
        await db("PATCH", "candy_sellers", params={"id": f"eq.{sid}"}, body={"status": payload.status}, prefer="return=minimal")
        return {"ok": True, "message": f"{rows[0]['name']} is now {payload.status}."}

    @app.post("/api/candy/admin/sellers/{sid}/paid")
    async def candy_admin_paid(sid: str, request: Request):
        await admin(request)
        if not re.fullmatch(r"[0-9a-f-]{36}", sid):
            raise HTTPException(status_code=404, detail="Not found.")
        rows = await db("GET", "candy_sellers", params={"id": f"eq.{sid}", "select": "*"}) or []
        if not rows:
            raise HTTPException(status_code=404, detail="Not found.")
        due = await db("GET", "candy_sales", params={"seller_id": f"eq.{sid}", "status": "eq.payable", "paid_out": "eq.false",
                                                     "select": "order_id,earned"}) or []
        amount = round(sum(float(d["earned"] or 0) for d in due), 2)
        if amount <= 0:
            raise HTTPException(status_code=400, detail="Nothing is owed right now.")
        seller = rows[0]
        method = seller.get("payout_method") or ""
        note = ""
        if method == CREDIT:
            st = await candy_settings()
            value = credit_value(amount, st["candy_credit_bonus"])
            code = f"CL{seller['code'][:8]}{secrets.token_hex(2).upper()}"
            await ecwid("POST", "/discount_coupons", body={
                "name": f"Candy Lady store credit · {seller['name']}"[:100], "code": code, "discountType": "ABS",
                "discount": value, "status": "ACTIVE", "usesLimit": "SINGLE", "applicationLimit": "UNLIMITED"})
            method = f"{CREDIT} · code {code} · ${value:.2f}"
            note = f" as a ${value:.2f} store credit code {code}"
            if seller.get("email"):
                body = (f"Hi {seller['name']},\n\nThank you for being a Candy Lady! Your ${amount:.2f} in earnings is now "
                        f"${value:.2f} of store credit.\n\nYour store credit code: {code}\n\nType it at checkout. It's good for one order "
                        f"up to ${value:.2f}, and any amount left over doesn't carry over, so fill your cart!\n\n"
                        + (f"Shop here: {st['candy_url']}\n\n" if st["candy_url"] else "") + "The Candy Lady")
                try:
                    sent = await ecom.send_email([seller["email"]], f"🍬 Your ${value:.2f} Candy Lady store credit", body)
                    note += " and emailed it to her" if sent else " (the email didn't send; give her the code)"
                except Exception:
                    note += " (the email didn't send; give her the code)"
        made = await db("POST", "candy_payouts", body={"seller_id": sid, "amount": amount, "method": method,
                                                       "order_ids": [d["order_id"] for d in due]}, prefer="return=representation")
        pid = (made or [{}])[0].get("id")
        ids = ",".join(f'"{d["order_id"]}"' for d in due)
        await db("PATCH", "candy_sales", params={"order_id": f"in.({ids})"}, body={"paid_out": True, "payout_id": pid},
                 prefer="return=minimal")
        return {"ok": True, "message": f"Recorded ${amount:.2f} paid to {seller['name']}{note}."}
