import os
import re
import logging
import time
import httpx
import stripe
import smtplib
from email.message import EmailMessage
from datetime import datetime, timezone
from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, EmailStr, field_validator

app = FastAPI(title="CashCow Reviews API")
logger = logging.getLogger("uvicorn.error")

# Locked to the real site(s) instead of "*". A wildcard origin doesn't stop a
# server-to-server bot from posting fake reviews, but it does stop random
# other websites from embedding this API in their own pages and riding on
# it — and it costs nothing to tighten. Add any other real domains this API
# is called from (a staging URL, etc.) to this list.
ALLOWED_ORIGINS = [
    "https://cashcowai.online",
    "https://www.cashcowai.online",
    "https://integrity-records-platform.onrender.com",  # Render address of the same site
    "https://mybillpilot.onrender.com",                 # MyBillPilot app (Premium checkout)
    "https://my-health-check-scan.onrender.com",        # My Health Check Scan app (Premium checkout)
    "https://my-home-kitchen-remedies.onrender.com",    # My Home Kitchen Remedies app (Premium checkout)
    "https://my-letter-helper.onrender.com",            # My Letter Helper app (Premium checkout)
    "https://my-recall-check.onrender.com",             # My Recall Check app (Premium checkout, recall lookups)
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type", "Authorization"],
)


# --- Simple in-memory rate limiter ---
# Works because this runs as a single long-lived Render web service, not
# serverless functions — counts persist across requests on the one
# instance. If this ever scales to multiple instances, swap this for a
# shared store (Redis, a Supabase table, etc.).
_request_log: dict[str, list[float]] = {}
RATE_LIMIT = 5           # max submissions
RATE_WINDOW_SECONDS = 60 # per this many seconds, per IP


def check_rate_limit(request: Request):
    ip = request.client.host if request.client else "unknown"
    now = time.time()
    hits = [t for t in _request_log.get(ip, []) if now - t < RATE_WINDOW_SECONDS]
    if len(hits) >= RATE_LIMIT:
        raise HTTPException(status_code=429, detail="Too many reviews submitted — please slow down.")
    hits.append(now)
    _request_log[ip] = hits


# Reject anything that looks like it's trying to inject markup. The
# frontend also escapes review text before rendering it (defense in
# depth), but rejecting it here means it never even reaches storage.
HTML_TAG_RE = re.compile(r"[<>]")


class ReviewRequest(BaseModel):
    name: str
    product: str  # "ScamShield", "Encompass", "Integrity Records", or "General"
    rating: int  # 1-5
    comment: str

    @field_validator("name", "product", "comment")
    @classmethod
    def no_markup(cls, v: str) -> str:
        if HTML_TAG_RE.search(v):
            raise ValueError("Field cannot contain '<' or '>' characters.")
        return v

    @field_validator("name", "comment")
    @classmethod
    def not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("Field cannot be blank.")
        return v


# --- Supabase config ---
# Get these from your Supabase project: Settings -> API
SUPABASE_URL = os.environ.get("SUPABASE_URL", "REPLACE_WITH_REAL_URL")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "REPLACE_WITH_REAL_KEY")


def supabase_headers():
    return {
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
        "Content-Type": "application/json",
    }


@app.get("/")
def root():
    return {"status": "CashCow Reviews API is running"}


# --- Hire-me lead capture ---
# Emails leads straight to your own inbox over Gmail's SMTP, using your own
# Gmail account and an app password — no third-party form service, nothing
# to sign up for, and nothing to check anywhere else. Set these on Render:
# Environment -> Add Environment Variable.
LEAD_GMAIL_ADDRESS = os.environ.get("LEAD_GMAIL_ADDRESS", "")       # e.g. cashcowaiadmin@gmail.com
LEAD_GMAIL_APP_PASSWORD = os.environ.get("LEAD_GMAIL_APP_PASSWORD", "")  # 16-char Gmail app password
LEAD_NOTIFY_TO = os.environ.get("LEAD_NOTIFY_TO", LEAD_GMAIL_ADDRESS)
# Startup check (logs only whether each setting is present, never the values).
logger.info("Lead email config: address set=%s, app password set=%s",
            bool(LEAD_GMAIL_ADDRESS), bool(LEAD_GMAIL_APP_PASSWORD))


class LeadRequest(BaseModel):
    name: str
    email: EmailStr
    message: str

    @field_validator("name", "message")
    @classmethod
    def no_markup_lead(cls, v: str) -> str:
        if HTML_TAG_RE.search(v):
            raise ValueError("Field cannot contain '<' or '>' characters.")
        return v

    @field_validator("name", "message")
    @classmethod
    def not_blank_lead(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("Field cannot be blank.")
        return v


def send_lead_email(name: str, email: str, message: str):
    if not LEAD_GMAIL_ADDRESS or not LEAD_GMAIL_APP_PASSWORD:
        raise RuntimeError("Lead email isn't configured yet.")

    msg = EmailMessage()
    msg["Subject"] = f"New CashCow lead — {name}"
    msg["From"] = LEAD_GMAIL_ADDRESS
    msg["To"] = LEAD_NOTIFY_TO
    msg["Reply-To"] = email
    msg.set_content(
        f"New message from the Hire Me page on cashcowai.online\n\n"
        f"Name: {name}\n"
        f"Email: {email}\n\n"
        f"Message:\n{message}\n"
    )

    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=10) as smtp:
        smtp.login(LEAD_GMAIL_ADDRESS, LEAD_GMAIL_APP_PASSWORD)
        smtp.send_message(msg)


@app.post("/api/leads")
async def submit_lead(payload: LeadRequest, request: Request):
    check_rate_limit(request)

    name = payload.name.strip()[:80]
    email = str(payload.email).strip()[:120]
    message = payload.message.strip()[:2000]

    try:
        send_lead_email(name, email, message)
    except RuntimeError:
        logger.error("Lead email NOT sent: LEAD_GMAIL_ADDRESS / LEAD_GMAIL_APP_PASSWORD missing from the environment.")
        return {"success": False, "error": "Lead email isn't configured yet."}
    except Exception as e:
        logger.error("Lead email NOT sent: %s: %s", type(e).__name__, e)
        return {"success": False, "error": "Could not send that right now — please try again."}

    return {"success": True}


# --- Stripe checkout ---
# Set these on Render: Environment -> Add Environment Variable.
STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "")
stripe.api_key = STRIPE_SECRET_KEY

SITE_URL = "https://cashcowai.online"

# CashCow's own bundled rent/lease/buy plans. Prices are built inline here
# (price_data) rather than referencing pre-created Stripe Price IDs, so
# nothing extra needs to be set up in the Stripe dashboard beyond the
# secret key — change a price here and it takes effect on the next
# checkout, no dashboard trip required.
PLANS = {
    # The bundle plans below aren't connected to any app yet, so they can't be bought
    # ("enabled": False). They'll become CashCow All Access once accounts are in every app.
    "rent": {
        "enabled": False,
        "label": "Monthly",
        "name": "CashCow App Subscription — Monthly",
        "amount": 2900,      # $29.00, in cents
        "mode": "subscription",
        "interval": "month",
    },
    "lease": {
        "enabled": False,
        "label": "Yearly",
        "name": "CashCow App Subscription — Yearly",
        "amount": 24900,     # $249.00
        "mode": "subscription",
        "interval": "year",
    },
    "buy": {
        "enabled": False,
        "label": "Lifetime",
        "name": "CashCow App — Lifetime Access",
        "amount": 49900,     # $499.00
        "mode": "payment",
        "interval": None,
    },
    # --- MyBillPilot Premium (sold separately, returns to the MyBillPilot app) ---
    "mbp_monthly": {
        "label": "Monthly",
        "name": "MyBillPilot Premium — Monthly",
        "amount": 499,       # $4.99
        "mode": "subscription",
        "interval": "month",
        "app": "mybillpilot",
    },
    "mbp_yearly": {
        "label": "Yearly",
        "name": "MyBillPilot Premium — Yearly",
        "amount": 4900,      # $49.00
        "mode": "subscription",
        "interval": "year",
        "app": "mybillpilot",
    },
    # --- My Health Check Scan Premium (returns to the My Health Check Scan app) ---
    "hcs_monthly": {
        "label": "Monthly",
        "name": "My Health Check Scan Premium — Monthly",
        "amount": 299,       # $2.99
        "mode": "subscription",
        "interval": "month",
        "app": "healthscan",
    },
    "hcs_yearly": {
        "label": "Yearly",
        "name": "My Health Check Scan Premium — Yearly",
        "amount": 1999,      # $19.99
        "mode": "subscription",
        "interval": "year",
        "app": "healthscan",
    },
    "hcs_lifetime": {
        "label": "Lifetime",
        "name": "My Health Check Scan Premium — Lifetime",
        "amount": 3999,      # $39.99, one-time
        "mode": "payment",
        "interval": None,
        "app": "healthscan",
    },
    # --- My Home Kitchen Remedies Premium (returns to the remedies app) ---
    "hkr_monthly": {
        "label": "Monthly",
        "name": "My Home Kitchen Remedies Premium — Monthly",
        "amount": 199,       # $1.99
        "mode": "subscription",
        "interval": "month",
        "app": "homeremedy",
    },
    "hkr_yearly": {
        "label": "Yearly",
        "name": "My Home Kitchen Remedies Premium — Yearly",
        "amount": 1499,      # $14.99
        "mode": "subscription",
        "interval": "year",
        "app": "homeremedy",
    },
    "hkr_lifetime": {
        "label": "Lifetime",
        "name": "My Home Kitchen Remedies Premium — Lifetime",
        "amount": 2999,      # $29.99, one-time
        "mode": "payment",
        "interval": None,
        "app": "homeremedy",
    },
    # --- My Letter Helper Premium (returns to the letter app) ---
    "mlh_monthly": {
        "label": "Monthly",
        "name": "My Letter Helper Premium — Monthly",
        "amount": 199,       # $1.99
        "mode": "subscription",
        "interval": "month",
        "app": "letterhelper",
    },
    "mlh_yearly": {
        "label": "Yearly",
        "name": "My Letter Helper Premium — Yearly",
        "amount": 1499,      # $14.99
        "mode": "subscription",
        "interval": "year",
        "app": "letterhelper",
    },
    "mlh_lifetime": {
        "label": "Lifetime",
        "name": "My Letter Helper Premium — Lifetime",
        "amount": 2999,      # $29.99, one-time
        "mode": "payment",
        "interval": None,
        "app": "letterhelper",
    },
    # --- My Recall Check Premium (returns to the recall app) ---
    "mrc_monthly": {
        "label": "Monthly",
        "name": "My Recall Check Premium — Monthly",
        "amount": 199,       # $1.99
        "mode": "subscription",
        "interval": "month",
        "app": "recallcheck",
    },
    "mrc_yearly": {
        "label": "Yearly",
        "name": "My Recall Check Premium — Yearly",
        "amount": 1499,      # $14.99
        "mode": "subscription",
        "interval": "year",
        "app": "recallcheck",
    },
    "mrc_lifetime": {
        "label": "Lifetime",
        "name": "My Recall Check Premium — Lifetime",
        "amount": 2999,      # $29.99, one-time
        "mode": "payment",
        "interval": None,
        "app": "recallcheck",
    },
}

MYBILLPILOT_URL = os.environ.get("MYBILLPILOT_URL", "https://mybillpilot.onrender.com")
HEALTHSCAN_URL = os.environ.get("HEALTHSCAN_URL", "https://my-health-check-scan.onrender.com")
REMEDIES_URL = os.environ.get("REMEDIES_URL", "https://my-home-kitchen-remedies.onrender.com")
LETTERS_URL = os.environ.get("LETTERS_URL", "https://my-letter-helper.onrender.com")
RECALLS_URL = os.environ.get("RECALLS_URL", "https://my-recall-check.onrender.com")

# Apps that sell their own Premium through CashCow, and where Stripe sends people back.
APP_URLS = {
    "mybillpilot": MYBILLPILOT_URL,
    "healthscan": HEALTHSCAN_URL,
    "homeremedy": REMEDIES_URL,
    "letterhelper": LETTERS_URL,
    "recallcheck": RECALLS_URL,
}


class CheckoutRequest(BaseModel):
    plan: str

    @field_validator("plan")
    @classmethod
    def known_plan(cls, v: str) -> str:
        if v not in PLANS:
            raise ValueError("Unknown plan.")
        return v


@app.get("/api/plans")
def get_plans():
    # Lets the cart page render live prices/labels without hardcoding
    # them a second time in the frontend.
    return {
        key: {"label": p["label"], "name": p["name"], "amount": p["amount"], "mode": p["mode"], "interval": p["interval"]}
        for key, p in PLANS.items()
        if not p.get("app") and p.get("enabled", True)  # app plans and unreleased bundle plans aren't listed
    }


@app.post("/api/create-checkout-session")
async def create_checkout_session(payload: CheckoutRequest, request: Request):
    check_rate_limit(request)

    if not STRIPE_SECRET_KEY:
        raise HTTPException(status_code=500, detail="Payments aren't configured yet.")

    plan = PLANS[payload.plan]
    if not plan.get("enabled", True):
        raise HTTPException(status_code=400, detail="That plan isn't available yet.")

    # Signed in with a CashCow account? Then the purchase is tied to the account,
    # so Premium works on every device the person signs in on.
    user = await account_user(request.headers.get("authorization", ""))
    price_data = {
        "currency": "usd",
        "product_data": {"name": plan["name"]},
        "unit_amount": plan["amount"],
    }
    if plan["mode"] == "subscription":
        price_data["recurring"] = {"interval": plan["interval"]}

    if plan.get("app") in APP_URLS:
        # Stripe fills in {CHECKOUT_SESSION_ID}; the app verifies it with /api/verify-session.
        app_url = APP_URLS[plan["app"]]
        success_url = f"{app_url}/?session_id={{CHECKOUT_SESSION_ID}}"
        cancel_url = f"{app_url}/"
    else:
        success_url = f"{SITE_URL}/success.html?plan={payload.plan}"
        cancel_url = f"{SITE_URL}/cart.html?plan={payload.plan}"

    metadata = {"plan": payload.plan, "app": plan.get("app", "cashcow")}
    extra = {}
    if user:
        metadata["user_id"] = user["id"]
        extra["client_reference_id"] = user["id"]
        if user.get("email"):
            extra["customer_email"] = user["email"]
    if plan["mode"] == "subscription":
        extra["subscription_data"] = {"metadata": metadata}

    try:
        session = stripe.checkout.Session.create(
            mode=plan["mode"],
            line_items=[{"price_data": price_data, "quantity": 1}],
            success_url=success_url,
            cancel_url=cancel_url,
            metadata=metadata,
            **extra,
        )
    except stripe.error.StripeError as e:
        raise HTTPException(status_code=400, detail=str(e))

    return {"url": session.url}


@app.get("/api/verify-session")
async def verify_session(session_id: str, request: Request, app: str = "mybillpilot"):
    """
    Used by the MyBillPilot and My Health Check Scan apps after Stripe checkout
    (and again every few days) to confirm a Premium purchase is paid and the
    subscription is still active. Returns only yes/no and the plan, never
    customer details. ?app= says which app is asking (default: mybillpilot).
    """
    check_rate_limit(request)
    if not STRIPE_SECRET_KEY:
        raise HTTPException(status_code=500, detail="Payments aren't configured yet.")
    if not re.fullmatch(r"cs_(test|live)_[A-Za-z0-9]{10,200}", session_id or ""):
        raise HTTPException(status_code=400, detail="Invalid session.")

    try:
        session = stripe.checkout.Session.retrieve(session_id, expand=["subscription"])
    except stripe.error.InvalidRequestError:
        return {"premium": False, "reason": "not_found"}
    except stripe.error.StripeError:
        raise HTTPException(status_code=502, detail="Couldn't reach Stripe. Try again shortly.")

    if app not in APP_URLS:
        raise HTTPException(status_code=400, detail="Unknown app.")
    metadata = session.get("metadata") or {}
    if metadata.get("app") != app:
        return {"premium": False, "reason": f"not_{app}"}

    paid = session.get("status") == "complete" and session.get("payment_status") in ("paid", "no_payment_required")
    active = paid
    sub = session.get("subscription")
    if sub and not isinstance(sub, str):
        active = sub.get("status") in ("active", "trialing", "past_due")

    # Bought while signed in: remember it on the account.
    if paid and metadata.get("user_id"):
        await save_entitlement(metadata["user_id"], app, metadata.get("plan", ""), session)

    return {"premium": bool(paid and active), "plan": metadata.get("plan")}


@app.post("/api/reviews")
async def submit_review(payload: ReviewRequest, request: Request):
    check_rate_limit(request)

    if SUPABASE_URL == "REPLACE_WITH_REAL_URL":
        return {"success": False, "error": "Reviews storage isn't configured yet."}

    rating = max(1, min(5, payload.rating))  # clamp to 1-5
    review = {
        "name": payload.name.strip()[:80],
        "product": payload.product.strip()[:40],
        "rating": rating,
        "comment": payload.comment.strip()[:1000],
        "submitted_at": datetime.now(timezone.utc).isoformat(),
    }

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"{SUPABASE_URL}/rest/v1/reviews",
                headers={**supabase_headers(), "Prefer": "return=representation"},
                json=review,
            )
            resp.raise_for_status()
            saved = resp.json()
    except httpx.HTTPError:
        return {"success": False, "error": "Could not save review right now."}

    return {"success": True, "review": saved[0] if saved else review}


@app.get("/api/reviews")
async def get_reviews():
    if SUPABASE_URL == "REPLACE_WITH_REAL_URL":
        return {"reviews": []}

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"{SUPABASE_URL}/rest/v1/reviews?order=submitted_at.desc&limit=50",
                headers=supabase_headers(),
            )
            resp.raise_for_status()
            reviews = resp.json()
    except httpx.HTTPError:
        return {"reviews": []}

    return {"reviews": reviews}


# --- CashCow accounts ---
# One account (email + password) works in every CashCow app. Accounts live in the
# same Supabase project ScamShield uses. Purchases made while signed in are saved
# in the cashcow_entitlements table (see cashcow_accounts.sql), so Premium follows
# the person to any phone or computer they sign in on.
# Render settings: AUTH_SUPABASE_URL (the project URL) and AUTH_SUPABASE_SERVICE_KEY
# (the project's secret key, the same one the ScamShield API uses).
AUTH_SUPABASE_URL = (os.environ.get("AUTH_SUPABASE_URL") or "https://wbugdujmydwmzemccbiy.supabase.co").strip().rstrip("/")
AUTH_SUPABASE_SERVICE_KEY = (os.environ.get("AUTH_SUPABASE_SERVICE_KEY") or "").strip()
logger.info("CashCow accounts config: service key set=%s", bool(AUTH_SUPABASE_SERVICE_KEY))


def accounts_enabled() -> bool:
    return bool(AUTH_SUPABASE_URL and AUTH_SUPABASE_SERVICE_KEY)


def _auth_headers() -> dict:
    # Newer Supabase secret keys (sb_secret_...) go only in the apikey header.
    h = {"apikey": AUTH_SUPABASE_SERVICE_KEY, "Content-Type": "application/json"}
    if not AUTH_SUPABASE_SERVICE_KEY.startswith("sb_"):
        h["Authorization"] = f"Bearer {AUTH_SUPABASE_SERVICE_KEY}"
    return h


async def account_user(authorization: str):
    """Returns the signed-in CashCow user ({id, email}) or None."""
    token = (authorization or "").removeprefix("Bearer ").strip()
    if not token or not accounts_enabled():
        return None
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            resp = await client.get(f"{AUTH_SUPABASE_URL}/auth/v1/user",
                                    headers={"apikey": AUTH_SUPABASE_SERVICE_KEY, "Authorization": f"Bearer {token}"})
        if resp.status_code != 200:
            return None
        data = resp.json()
        return {"id": data["id"], "email": data.get("email", "")} if data.get("id") else None
    except (httpx.HTTPError, ValueError, KeyError):
        return None


async def save_entitlement(user_id: str, app_name: str, plan: str, session) -> None:
    if not accounts_enabled():
        return
    sub = session.get("subscription")
    sub_id = sub if isinstance(sub, str) else (sub.get("id") if sub else None)
    row = {
        "user_id": user_id,
        "app": app_name,
        "plan": plan,
        "stripe_session_id": session.get("id"),
        "stripe_subscription_id": sub_id,
        "mode": session.get("mode"),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            await client.post(
                f"{AUTH_SUPABASE_URL}/rest/v1/cashcow_entitlements?on_conflict=stripe_session_id",
                headers={**_auth_headers(), "Prefer": "resolution=merge-duplicates,return=minimal"},
                json=row,
            )
    except httpx.HTTPError as e:
        logger.error("Couldn't save entitlement: %s", type(e).__name__)


def _still_active(row) -> bool:
    """Checks Stripe live, so cancelled or unpaid subscriptions turn off on their own."""
    if row.get("stripe_subscription_id"):
        try:
            sub = stripe.Subscription.retrieve(row["stripe_subscription_id"])
            return sub.get("status") in ("active", "trialing", "past_due")
        except stripe.error.StripeError:
            return True  # Stripe unreachable: don't take Premium away by mistake
    return row.get("mode") == "payment"  # one-time (lifetime) purchase


@app.get("/api/entitlements")
async def get_entitlements(request: Request, app: str = "healthscan"):
    """Is the signed-in person Premium in this app? Checks their own purchases
    for the app, plus CashCow All Access once it exists."""
    if app not in APP_URLS:
        raise HTTPException(status_code=400, detail="Unknown app.")
    user = await account_user(request.headers.get("authorization", ""))
    if not user:
        raise HTTPException(status_code=401, detail="Sign in to your CashCow account.")
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            resp = await client.get(
                f"{AUTH_SUPABASE_URL}/rest/v1/cashcow_entitlements",
                headers=_auth_headers(),
                params={"user_id": f"eq.{user['id']}", "app": f"in.({app},allaccess)", "select": "*", "order": "updated_at.desc"},
            )
        rows = resp.json() if resp.status_code == 200 else []
    except (httpx.HTTPError, ValueError):
        raise HTTPException(status_code=502, detail="Couldn't check your account right now.")
    for row in rows if isinstance(rows, list) else []:
        if _still_active(row):
            return {"premium": True, "plan": row.get("plan"), "via": row.get("app")}
    return {"premium": False}


class ClaimRequest(BaseModel):
    session_id: str


@app.post("/api/entitlements/claim")
async def claim_purchase(payload: ClaimRequest, request: Request, app: str = "healthscan"):
    """Attaches a purchase made on this device (before signing in) to the
    signed-in account, so it also works on their other devices."""
    check_rate_limit(request)
    if app not in APP_URLS:
        raise HTTPException(status_code=400, detail="Unknown app.")
    user = await account_user(request.headers.get("authorization", ""))
    if not user:
        raise HTTPException(status_code=401, detail="Sign in to your CashCow account.")
    if not re.fullmatch(r"cs_(test|live)_[A-Za-z0-9]{10,200}", payload.session_id or ""):
        raise HTTPException(status_code=400, detail="Invalid purchase.")
    try:
        session = stripe.checkout.Session.retrieve(payload.session_id)
    except stripe.error.StripeError:
        raise HTTPException(status_code=404, detail="Purchase not found.")
    metadata = session.get("metadata") or {}
    paid = session.get("status") == "complete" and session.get("payment_status") in ("paid", "no_payment_required")
    if metadata.get("app") != app or not paid:
        raise HTTPException(status_code=400, detail="That purchase isn't for this app.")
    owner = metadata.get("user_id")
    if owner and owner != user["id"]:
        raise HTTPException(status_code=409, detail="That purchase belongs to another account.")
    # Already claimed by someone else?
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            resp = await client.get(f"{AUTH_SUPABASE_URL}/rest/v1/cashcow_entitlements", headers=_auth_headers(),
                                    params={"stripe_session_id": f"eq.{payload.session_id}", "select": "user_id"})
        existing = resp.json() if resp.status_code == 200 else []
    except (httpx.HTTPError, ValueError):
        existing = []
    if existing and existing[0].get("user_id") != user["id"]:
        raise HTTPException(status_code=409, detail="That purchase belongs to another account.")
    await save_entitlement(user["id"], app, metadata.get("plan", ""), session)
    return {"claimed": True}


# --- Health topics from MedlinePlus.gov (search help in My Home Kitchen Remedies) ---
# Free U.S. National Library of Medicine service; no key needed. NLM asks for
# no more than 85 requests a minute and suggests caching results for 12-24 hours,
# and asks apps to say the information is from MedlinePlus.gov.
import html as _html
import xml.etree.ElementTree as ET

MEDLINE_URL = "https://wsearch.nlm.nih.gov/ws/query"
MEDLINE_CACHE_SECONDS = 24 * 60 * 60
MEDLINE_MAX_PER_MINUTE = 60          # stay well under NLM's 85/minute
TOPIC_IP_LIMIT = 30                  # lookups per minute per visitor
_medline_cache: dict[str, tuple[float, list]] = {}
_medline_calls: list[float] = []
_topic_ip_log: dict[str, list[float]] = {}
_TOPIC_QUERY_RE = re.compile(r"^[A-Za-z0-9 '\-]{2,80}$")
_TAG_RE = re.compile(r"<[^>]+>")
_BLOCK_TAG_RE = re.compile(r"</?(p|br|li|ul|ol|div|h[1-6])\b[^>]*>", re.I)


def _plain_text(text: str) -> str:
    text = _html.unescape(text or "")
    text = _BLOCK_TAG_RE.sub(" ", text)   # paragraph and line breaks become spaces
    text = _TAG_RE.sub("", text)          # highlight spans etc. vanish
    text = _html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _short_summary(text: str, limit: int = 420) -> str:
    if len(text) <= limit:
        return text
    cut = text[:limit]
    end = max(cut.rfind(". "), cut.rfind("? "), cut.rfind("! "))
    return cut[: end + 1] if end > 120 else cut.rsplit(" ", 1)[0] + "…"


def parse_medline(xml_text: str, limit: int = 3) -> list:
    root = ET.fromstring(xml_text)
    results = []
    for doc in root.iter("document"):
        url = (doc.get("url") or "").strip().replace("http://", "https://", 1)
        if not url.startswith("https://medlineplus.gov/"):
            continue
        fields = {c.get("name"): "".join(c.itertext()) for c in doc.iter("content")}
        title = _plain_text(fields.get("title", ""))
        summary = _plain_text(fields.get("FullSummary") or fields.get("snippet") or "")
        if title:
            results.append({"title": title[:120], "url": url, "summary": _short_summary(summary)})
        if len(results) >= limit:
            break
    return results


async def _medline_search(client: httpx.AsyncClient, term: str) -> list:
    now = time.time()
    _medline_calls[:] = [t for t in _medline_calls if now - t < 60]
    if len(_medline_calls) >= MEDLINE_MAX_PER_MINUTE:
        raise HTTPException(status_code=503, detail="Health information is busy. Try again in a minute.")
    _medline_calls.append(now)
    resp = await client.get(MEDLINE_URL, params={
        "db": "healthTopics", "term": term, "retmax": 5, "rettype": "brief", "tool": "cashcow-kitchen-remedies",
    })
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail="MedlinePlus isn't answering right now.")
    return parse_medline(resp.text)


@app.get("/api/health-topics")
async def health_topics(q: str, request: Request):
    q = " ".join(q.split())
    if not _TOPIC_QUERY_RE.match(q):
        raise HTTPException(status_code=400, detail="Use 2 to 80 letters or numbers.")
    key = q.lower()
    hit = _medline_cache.get(key)
    if hit and time.time() - hit[0] < MEDLINE_CACHE_SECONDS:
        return {"results": hit[1], "source": "MedlinePlus.gov"}

    ip = request.client.host if request.client else "unknown"
    now = time.time()
    hits = [t for t in _topic_ip_log.get(ip, []) if now - t < 60]
    if len(hits) >= TOPIC_IP_LIMIT:
        raise HTTPException(status_code=429, detail="Too many searches. Please wait a minute.")
    _topic_ip_log[ip] = hits + [now]

    try:
        async with httpx.AsyncClient(timeout=8) as client:
            results = await _medline_search(client, key)
            words = key.split()
            if not results and len(words) > 1:
                results = await _medline_search(client, " OR ".join(words))
    except (httpx.HTTPError, ET.ParseError):
        raise HTTPException(status_code=502, detail="MedlinePlus isn't answering right now.")

    if len(_medline_cache) >= 2000:
        _medline_cache.pop(next(iter(_medline_cache)))
    _medline_cache[key] = (time.time(), results)
    return {"results": results, "source": "MedlinePlus.gov"}


# --- Vehicle recalls from NHTSA (My Recall Check) ---
# Free U.S. government data, no key needed. Answers are cached so repeat
# lookups are fast and NHTSA isn't asked the same thing over and over.
from datetime import date as _date

NHTSA_RECALLS = "https://api.nhtsa.gov/recalls/recallsByVehicle"
NHTSA_MAKES = "https://api.nhtsa.gov/products/vehicle/makes"
NHTSA_MODELS = "https://api.nhtsa.gov/products/vehicle/models"
VPIC_DECODE = "https://vpic.nhtsa.dot.gov/api/vehicles/DecodeVinValues/{vin}"
NHTSA_MAX_PER_MINUTE = 60
RECALL_IP_LIMIT = 40                 # lookups per minute per visitor
_nhtsa_cache: dict[str, tuple[float, object]] = {}
_nhtsa_calls: list[float] = []
_recall_ip_log: dict[str, list[float]] = {}
_NAME_RE = re.compile(r"^[A-Za-z0-9 .&'/\-]{1,60}$")
_VIN_RE = re.compile(r"^[A-HJ-NPR-Z0-9]{17}$")


def _ci(item: dict, *names: str):
    """Reads a field whatever its capitalization (NHTSA mixes styles)."""
    low = {str(k).lower(): v for k, v in item.items()}
    for n in names:
        if n.lower() in low:
            return low[n.lower()]
    return None


def _result_list(data) -> list:
    if isinstance(data, dict):
        v = _ci(data, "results")
        return v if isinstance(v, list) else []
    return data if isinstance(data, list) else []


def _iso_date(text) -> str:
    t = str(text or "").strip()
    m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})$", t)   # NHTSA sends DD/MM/YYYY
    if m:
        try:
            return _date(int(m.group(3)), int(m.group(2)), int(m.group(1))).isoformat()
        except ValueError:
            return ""
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", t)
    return m.group(0) if m else ""


def _flag(v) -> bool:
    return v is True or str(v).strip().lower() in ("true", "yes", "y", "1")


def _recall_guard(request: Request):
    ip = request.client.host if request.client else "unknown"
    now = time.time()
    hits = [t for t in _recall_ip_log.get(ip, []) if now - t < 60]
    if len(hits) >= RECALL_IP_LIMIT:
        raise HTTPException(status_code=429, detail="Too many lookups. Please wait a minute.")
    _recall_ip_log[ip] = hits + [now]


async def _nhtsa_get(url: str, params: dict, ttl: int):
    key = url + "?" + "&".join(f"{k}={v}" for k, v in sorted(params.items()))
    hit = _nhtsa_cache.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    now = time.time()
    _nhtsa_calls[:] = [t for t in _nhtsa_calls if now - t < 60]
    if len(_nhtsa_calls) >= NHTSA_MAX_PER_MINUTE:
        raise HTTPException(status_code=503, detail="Recall lookups are busy. Try again in a minute.")
    _nhtsa_calls.append(now)
    try:
        async with httpx.AsyncClient(timeout=12) as client:
            resp = await client.get(url, params=params)
        if resp.status_code == 400:
            data = {"results": []}       # NHTSA answers 400 when it has nothing for that vehicle
        elif resp.status_code != 200:
            raise HTTPException(status_code=502, detail="NHTSA isn't answering right now. Try again soon.")
        else:
            data = resp.json()
    except (httpx.HTTPError, ValueError):
        raise HTTPException(status_code=502, detail="NHTSA isn't answering right now. Try again soon.")
    if len(_nhtsa_cache) >= 3000:
        _nhtsa_cache.pop(next(iter(_nhtsa_cache)))
    _nhtsa_cache[key] = (time.time(), data)
    return data


def _check_year(year: int):
    if not 1950 <= year <= _date.today().year + 2:
        raise HTTPException(status_code=400, detail="Choose a model year.")


def _check_name(value: str, what: str) -> str:
    value = " ".join(value.split()).upper()
    if not _NAME_RE.match(value):
        raise HTTPException(status_code=400, detail=f"Choose a {what}.")
    return value


@app.get("/api/recalls/makes")
async def recall_makes(year: int, request: Request):
    _check_year(year)
    _recall_guard(request)
    data = await _nhtsa_get(NHTSA_MAKES, {"modelYear": year, "issueType": "r"}, 7 * 86400)
    makes = sorted({str(_ci(i, "make") or "").strip().upper() for i in _result_list(data) if isinstance(i, dict)} - {""})
    return {"year": year, "makes": makes}


@app.get("/api/recalls/models")
async def recall_models(year: int, make: str, request: Request):
    _check_year(year)
    make = _check_name(make, "make")
    _recall_guard(request)
    data = await _nhtsa_get(NHTSA_MODELS, {"modelYear": year, "make": make, "issueType": "r"}, 7 * 86400)
    models = sorted({str(_ci(i, "model") or "").strip().upper() for i in _result_list(data) if isinstance(i, dict)} - {""})
    return {"year": year, "make": make.upper(), "models": models}


@app.get("/api/recalls/vin")
async def recall_vin(vin: str, request: Request):
    vin = vin.strip().upper()
    if not _VIN_RE.match(vin):
        raise HTTPException(status_code=400, detail="A VIN is 17 letters and numbers (never I, O or Q).")
    _recall_guard(request)
    data = await _nhtsa_get(VPIC_DECODE.format(vin=vin), {"format": "json"}, 30 * 86400)
    rows = _result_list(data)
    row = rows[0] if rows and isinstance(rows[0], dict) else {}
    year = str(_ci(row, "ModelYear") or "").strip()
    make = str(_ci(row, "Make") or "").strip().upper()
    model = str(_ci(row, "Model") or "").strip().upper()
    if not (year.isdigit() and make and model):
        raise HTTPException(status_code=404, detail="We couldn't read that VIN. Check it, or pick the year, make and model instead.")
    code = str(_ci(row, "ErrorCode") or "0").strip()
    return {"vin": vin, "year": int(year), "make": make, "model": model, "checkDigitWarning": not code.startswith("0")}


@app.get("/api/recalls/vehicle")
async def recall_vehicle(year: int, make: str, model: str, request: Request):
    _check_year(year)
    make = _check_name(make, "make")
    model = _check_name(model, "model")
    _recall_guard(request)
    data = await _nhtsa_get(NHTSA_RECALLS, {"make": make, "model": model, "modelYear": year}, 12 * 3600)
    recalls, seen = [], set()
    for i in _result_list(data):
        if not isinstance(i, dict):
            continue
        campaign = str(_ci(i, "NHTSACampaignNumber") or "").strip()
        if not campaign or campaign in seen:
            continue
        seen.add(campaign)
        recalls.append({
            "campaign": campaign[:20],
            "date": _iso_date(_ci(i, "ReportReceivedDate")),
            "component": str(_ci(i, "Component") or "")[:300],
            "summary": str(_ci(i, "Summary") or "")[:3000],
            "consequence": str(_ci(i, "Consequence") or "")[:2000],
            "remedy": str(_ci(i, "Remedy") or "")[:2000],
            "notes": str(_ci(i, "Notes") or "")[:2000],
            "manufacturer": str(_ci(i, "Manufacturer") or "")[:200],
            "parkIt": _flag(_ci(i, "parkIt")),
            "parkOutside": _flag(_ci(i, "parkOutSide", "parkOutside")),
            "overTheAir": _flag(_ci(i, "overTheAirUpdate")),
        })
    recalls.sort(key=lambda r: (not r["parkIt"], not r["parkOutside"], r["date"] and -int(r["date"].replace("-", "")) or 0))
    return {"year": year, "make": make.upper(), "model": model.upper(), "count": len(recalls), "recalls": recalls, "source": "NHTSA"}
