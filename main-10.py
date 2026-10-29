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
    "https://my-caregiver-circle.onrender.com",         # My Caregiver Circle app (shared care circles)
    "https://my-fix-it-helper.onrender.com",            # My Fix-It Helper app (Premium checkout)
    "https://my-shopping-helper.onrender.com",          # My Shopping Helper app (Premium checkout)
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
    phone: str = ""      # optional (chat receptionist)
    source: str = ""     # where it came from: hire, chat, allaccess...
    topic: str = ""      # what it's about, e.g. "Hire me", "Help with an app"

    @field_validator("name", "message", "phone", "source", "topic")
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


LEAD_SOURCES = {
    "hire": "the Hire Me page on cashcowai.online",
    "chat": "the chat receptionist on cashcowai.online",
    "allaccess": "the All Access sign-up on cashcowai.online",
    "phone": "the CashCow phone line",
}


def send_lead_email(name: str, email: str, message: str, source: str = "hire", phone: str = "", topic: str = ""):
    if not LEAD_GMAIL_ADDRESS or not LEAD_GMAIL_APP_PASSWORD:
        raise RuntimeError("Lead email isn't configured yet.")

    where = LEAD_SOURCES.get(source, LEAD_SOURCES["hire"])
    msg = EmailMessage()
    msg["Subject"] = f"New CashCow lead — {name}" + (f" ({topic})" if topic else "")
    msg["From"] = LEAD_GMAIL_ADDRESS
    msg["To"] = LEAD_NOTIFY_TO
    if email:
        msg["Reply-To"] = email
    lines = [f"New message from {where}", "", f"Name: {name}"]
    if email:
        lines.append(f"Email: {email}")
    if phone:
        lines.append(f"Phone: {phone}")
    if topic:
        lines.append(f"About: {topic}")
    lines += ["", "Message:", message, ""]
    msg.set_content("\n".join(lines))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=10) as smtp:
        smtp.login(LEAD_GMAIL_ADDRESS, LEAD_GMAIL_APP_PASSWORD)
        smtp.send_message(msg)


@app.post("/api/leads")
async def submit_lead(payload: LeadRequest, request: Request):
    check_rate_limit(request)

    name = payload.name.strip()[:80]
    email = str(payload.email).strip()[:120]
    message = payload.message.strip()[:2000]
    phone = re.sub(r"[^0-9+()\-. ]", "", payload.phone)[:30].strip()
    source = payload.source.strip().lower()[:20]
    source = source if source in LEAD_SOURCES and source != "phone" else "hire"
    topic = payload.topic.strip()[:60]

    try:
        send_lead_email(name, email, message, source=source, phone=phone, topic=topic)
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
    # --- My Caregiver Circle Premium (returns to the caregiver app) ---
    "cgc_monthly": {
        "label": "Monthly",
        "name": "My Caregiver Circle Premium — Monthly",
        "amount": 299,       # $2.99
        "mode": "subscription",
        "interval": "month",
        "app": "caregiver",
    },
    "cgc_yearly": {
        "label": "Yearly",
        "name": "My Caregiver Circle Premium — Yearly",
        "amount": 1999,      # $19.99
        "mode": "subscription",
        "interval": "year",
        "app": "caregiver",
    },
    "cgc_lifetime": {
        "label": "Lifetime",
        "name": "My Caregiver Circle Premium — Lifetime",
        "amount": 3999,      # $39.99, one-time
        "mode": "payment",
        "interval": None,
        "app": "caregiver",
    },
    # --- My Fix-It Helper Premium (returns to the fix-it app) ---
    "fih_monthly": {
        "label": "Monthly",
        "name": "My Fix-It Helper Premium — Monthly",
        "amount": 199,       # $1.99
        "mode": "subscription",
        "interval": "month",
        "app": "fixit",
    },
    "fih_yearly": {
        "label": "Yearly",
        "name": "My Fix-It Helper Premium — Yearly",
        "amount": 1499,      # $14.99
        "mode": "subscription",
        "interval": "year",
        "app": "fixit",
    },
    "fih_lifetime": {
        "label": "Lifetime",
        "name": "My Fix-It Helper Premium — Lifetime",
        "amount": 2999,      # $29.99, one-time
        "mode": "payment",
        "interval": None,
        "app": "fixit",
    },
    # --- My Shopping Helper Premium (returns to the shopping app) ---
    "msh_monthly": {
        "label": "Monthly",
        "name": "My Shopping Helper Premium — Monthly",
        "amount": 199,       # $1.99
        "mode": "subscription",
        "interval": "month",
        "app": "shopping",
    },
    "msh_yearly": {
        "label": "Yearly",
        "name": "My Shopping Helper Premium — Yearly",
        "amount": 1499,      # $14.99
        "mode": "subscription",
        "interval": "year",
        "app": "shopping",
    },
    "msh_lifetime": {
        "label": "Lifetime",
        "name": "My Shopping Helper Premium — Lifetime",
        "amount": 2999,      # $29.99, one-time
        "mode": "payment",
        "interval": None,
        "app": "shopping",
    },
}

MYBILLPILOT_URL = os.environ.get("MYBILLPILOT_URL", "https://mybillpilot.onrender.com")
HEALTHSCAN_URL = os.environ.get("HEALTHSCAN_URL", "https://my-health-check-scan.onrender.com")
REMEDIES_URL = os.environ.get("REMEDIES_URL", "https://my-home-kitchen-remedies.onrender.com")
LETTERS_URL = os.environ.get("LETTERS_URL", "https://my-letter-helper.onrender.com")
RECALLS_URL = os.environ.get("RECALLS_URL", "https://my-recall-check.onrender.com")
CAREGIVER_URL = os.environ.get("CAREGIVER_URL", "https://my-caregiver-circle.onrender.com")
FIXIT_URL = os.environ.get("FIXIT_URL", "https://my-fix-it-helper.onrender.com")
SHOPPING_URL = os.environ.get("SHOPPING_URL", "https://my-shopping-helper.onrender.com")

# Apps that sell their own Premium through CashCow, and where Stripe sends people back.
APP_URLS = {
    "mybillpilot": MYBILLPILOT_URL,
    "healthscan": HEALTHSCAN_URL,
    "homeremedy": REMEDIES_URL,
    "letterhelper": LETTERS_URL,
    "recallcheck": RECALLS_URL,
    "caregiver": CAREGIVER_URL,
    "fixit": FIXIT_URL,
    "shopping": SHOPPING_URL,
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


# --- Phone receptionist (Twilio) ---
# A free, scripted phone line: it greets callers, tells them about the apps,
# and takes a spoken message that's emailed to you like any other lead.
# It stays OFF until TWILIO_AUTH_TOKEN is set on Render, and every request is
# checked with Twilio's signature, so nobody else can trigger lead emails.
# Twilio setup: buy a number, and under "A call comes in" choose Webhook,
# HTTP POST, https://cashcow-reviews-api.onrender.com/api/voice/incoming
import base64 as _b64
import hashlib as _hashlib
import hmac as _hmac
from urllib.parse import parse_qs as _parse_qs
from fastapi.responses import Response as _Response
from xml.sax.saxutils import escape as _xml_escape

TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", "")
PUBLIC_API_URL = os.environ.get("PUBLIC_API_URL", "https://cashcow-reviews-api.onrender.com").rstrip("/")
FORWARD_TO = os.environ.get("RECEPTIONIST_FORWARD_TO", "")   # optional: your cell, e.g. +12145550123
VOICE = "Polly.Joanna"

PHONE_APPS = (
    "ScamShield checks messages, links and phone numbers for scams. "
    "MyBillPilot keeps your bills in one place and reminds you when they're due. "
    "My Health Check Scan scans food barcodes for banned ingredients and allergens. "
    "My Home Kitchen Remedies has simple remedies from your kitchen, with safety checks. "
    "My Letter Helper writes letters for billing disputes, refunds and cancellations. "
    "And My Recall Check checks your car for safety recalls. "
    "Most features are free. You'll find them all at cash cow A I dot online."
)


def _twilio_valid(request: Request, params: dict) -> bool:
    if not TWILIO_AUTH_TOKEN:
        return False
    url = PUBLIC_API_URL + request.url.path + (f"?{request.url.query}" if request.url.query else "")
    data = url + "".join(k + params[k] for k in sorted(params))
    digest = _hmac.new(TWILIO_AUTH_TOKEN.encode(), data.encode(), _hashlib.sha1).digest()
    expected = _b64.b64encode(digest).decode()
    return _hmac.compare_digest(expected, request.headers.get("X-Twilio-Signature", ""))


async def _twilio_params(request: Request) -> dict:
    body = (await request.body()).decode("utf-8", "replace")
    params = {k: v[0] for k, v in _parse_qs(body, keep_blank_values=True).items()}
    if not _twilio_valid(request, params):
        raise HTTPException(status_code=403, detail="Not allowed.")
    return params


def _twiml(inner: str) -> _Response:
    return _Response(content=f'<?xml version="1.0" encoding="UTF-8"?><Response>{inner}</Response>', media_type="application/xml")


def _say(text: str) -> str:
    return f'<Say voice="{VOICE}">{_xml_escape(text)}</Say>'


def _menu_prompt(first: bool) -> str:
    opening = "Thanks for calling CashCow, simple apps for everyday problems. " if first else ""
    talk = " To talk to a person, press 0." if FORWARD_TO else ""
    return _say(
        opening
        + "To hear about our apps, press 1 or say apps. "
        + "To have us build an app for your business, press 2 or say build. "
        + "For help with one of our apps, press 3 or say help. "
        + "To leave a message, press 4 or say message." + talk
    )


def _ask_message(topic: str) -> str:
    return (
        f'<Gather input="speech" action="/api/voice/message?topic={topic}&amp;try=1" method="POST" speechTimeout="auto" timeout="8">'
        + _say("Please say your name, the best phone number or email to reach you, and how we can help. When you're done, just stop talking.")
        + "</Gather>"
        + f'<Redirect method="POST">/api/voice/message?topic={topic}&amp;try=1</Redirect>'
    )


@app.post("/api/voice/incoming")
async def voice_incoming(request: Request):
    await _twilio_params(request)
    retry = request.query_params.get("retry") == "1"
    gather = (
        '<Gather input="speech dtmf" numDigits="1" action="/api/voice/menu" method="POST" timeout="6" speechTimeout="auto" '
        'hints="apps, build, help, message, person">' + _menu_prompt(first=not retry) + "</Gather>"
    )
    after = _ask_message("message") if retry else '<Redirect method="POST">/api/voice/incoming?retry=1</Redirect>'
    return _twiml(gather + after)


def _menu_choice(digit: str, said: str) -> str:
    """Keypresses first; for speech, check the specific words before "app",
    so "I need help with an app" goes to help, not the app list."""
    said = (said or "").lower()
    if digit:
        return {"1": "apps", "2": "build", "3": "help", "4": "message", "0": "person"}.get(digit, "message")
    if any(w in said for w in ("person", "human", "someone", "representative", "operator")):
        return "person"
    if any(w in said for w in ("build", "business", "custom", "rebrand")):
        return "build"
    if any(w in said for w in ("help", "support", "problem", "trouble", "issue", "cancel", "refund")):
        return "help"
    if "message" in said:
        return "message"
    if "app" in said:
        return "apps"
    return "message"


MENU_LINES = {
    "apps": [PHONE_APPS, "If you'd like us to call you back, leave a message now."],
    "build": ["Great. CashCow builds custom apps and can rebrand any of ours for your business."],
    "help": ["Sorry you're having trouble. Tell us which app and what's happening."],
}


@app.post("/api/voice/menu")
async def voice_menu(request: Request):
    p = await _twilio_params(request)
    choice = _menu_choice(p.get("Digits", ""), p.get("SpeechResult", ""))
    if choice in MENU_LINES:
        return _twiml("".join(_say(t) for t in MENU_LINES[choice]) + _ask_message(choice))
    if choice == "person" and FORWARD_TO:
        return _twiml(_say("Connecting you now.") + f'<Dial timeout="20">{_xml_escape(FORWARD_TO)}</Dial>'
                      + _say("Sorry, nobody's available right now.") + _ask_message("message"))
    return _twiml(_ask_message("message"))


VOICE_TOPICS = {"apps": "Apps question (phone)", "build": "Build an app (phone)", "help": "Help with an app (phone)", "message": "Phone message"}


@app.post("/api/voice/message")
async def voice_message(request: Request):
    p = await _twilio_params(request)
    topic = request.query_params.get("topic", "message")
    topic = topic if topic in VOICE_TOPICS else "message"
    said = p.get("SpeechResult", "").strip()
    caller = re.sub(r"[^0-9+]", "", p.get("From", ""))[:20]
    if not said:
        if request.query_params.get("try") == "1":
            return _twiml(_say("Sorry, I didn't catch that.") + _ask_message(topic).replace("try=1", "try=2"))
        return _twiml(_say("Sorry, we didn't get a message. Please call back, or visit cash cow A I dot online. Goodbye.") + "<Hangup/>")
    try:
        send_lead_email(
            name=f"Caller {caller or 'unknown number'}",
            email="",
            message=f"Transcribed by the phone line (check for mistakes):\n\n{said[:2000]}",
            source="phone",
            phone=caller,
            topic=VOICE_TOPICS[topic],
        )
    except Exception as e:  # still thank the caller; the log shows what went wrong
        logger.error("Phone lead email NOT sent: %s: %s", type(e).__name__, e)
    return _twiml(_say("Thank you. We got your message and we'll get back to you within one business day. Goodbye.") + "<Hangup/>")


# --- Phone receptionist (Plivo) ---
# The same scripted phone line as the Twilio one above, for a Plivo number
# (Plivo's pay-as-you-go plan starts with free trial credit). It stays OFF
# until PLIVO_WEBHOOK_KEY is set on Render. The key is part of the web address
# you give Plivo, so only Plivo (which knows the address) can reach it:
#   Answer URL: https://cashcow-reviews-api.onrender.com/api/plivo/<your key>/incoming  (POST)
PLIVO_WEBHOOK_KEY = os.environ.get("PLIVO_WEBHOOK_KEY", "")
PLIVO_VOICE = os.environ.get("PLIVO_VOICE", "WOMAN")


def _plivo_check(key: str):
    if len(PLIVO_WEBHOOK_KEY) < 16 or not _hmac.compare_digest(key, PLIVO_WEBHOOK_KEY):
        raise HTTPException(status_code=404, detail="Not found.")


async def _plivo_params(request: Request) -> dict:
    params = dict(request.query_params)
    if request.method == "POST":
        body = (await request.body()).decode("utf-8", "replace")
        params.update({k: v[0] for k, v in _parse_qs(body, keep_blank_values=True).items()})
    return params


def _plivo_url(path: str) -> str:
    return f"{PUBLIC_API_URL}/api/plivo/{PLIVO_WEBHOOK_KEY}/{path}"


def _pspeak(text: str) -> str:
    return f'<Speak voice="{_xml_escape(PLIVO_VOICE)}" language="en-US">{_xml_escape(text)}</Speak>'


def _plivo_xml(inner: str) -> _Response:
    return _Response(content=f'<?xml version="1.0" encoding="UTF-8"?><Response>{inner}</Response>', media_type="application/xml")


def _plivo_ask_message(topic: str, attempt: int = 1) -> str:
    action = _xml_escape(_plivo_url(f"message?topic={topic}&try={attempt}"))
    return (
        f'<GetInput action="{action}" method="POST" inputType="speech" executionTimeout="30" '
        f'speechEndTimeout="3" redirect="true">'
        + _pspeak("Please say your name, the best phone number or email to reach you, and how we can help. When you're done, just stop talking.")
        + "</GetInput>"
        + f'<Redirect method="POST">{action}</Redirect>'
    )


def _plivo_menu_prompt(first: bool) -> str:
    opening = "Thanks for calling CashCow, simple apps for everyday problems. " if first else ""
    talk = " To talk to a person, press 0." if FORWARD_TO else ""
    return _pspeak(
        opening
        + "To hear about our apps, press 1 or say apps. "
        + "To have us build an app for your business, press 2 or say build. "
        + "For help with one of our apps, press 3 or say help. "
        + "To leave a message, press 4 or say message." + talk
    )


@app.get("/api/plivo/{key}/incoming")
@app.post("/api/plivo/{key}/incoming")
async def plivo_incoming(key: str, request: Request):
    _plivo_check(key)
    p = await _plivo_params(request)
    retry = p.get("retry") == "1"
    action = _xml_escape(_plivo_url("menu"))
    gather = (
        f'<GetInput action="{action}" method="POST" inputType="dtmf speech" numDigits="1" executionTimeout="8" '
        f'redirect="true" hints="apps,build,help,message,person">' + _plivo_menu_prompt(first=not retry) + "</GetInput>"
    )
    after = _plivo_ask_message("message") if retry else f'<Redirect method="POST">{_xml_escape(_plivo_url("incoming?retry=1"))}</Redirect>'
    return _plivo_xml(gather + after)


@app.get("/api/plivo/{key}/menu")
@app.post("/api/plivo/{key}/menu")
async def plivo_menu(key: str, request: Request):
    _plivo_check(key)
    p = await _plivo_params(request)
    choice = _menu_choice(p.get("Digits", ""), p.get("Speech", ""))
    if choice in MENU_LINES:
        return _plivo_xml("".join(_pspeak(t) for t in MENU_LINES[choice]) + _plivo_ask_message(choice))
    if choice == "person" and FORWARD_TO:
        return _plivo_xml(_pspeak("Connecting you now.") + f"<Dial><Number>{_xml_escape(FORWARD_TO)}</Number></Dial>"
                          + _pspeak("Sorry, nobody's available right now.") + _plivo_ask_message("message"))
    return _plivo_xml(_plivo_ask_message("message"))


@app.get("/api/plivo/{key}/message")
@app.post("/api/plivo/{key}/message")
async def plivo_message(key: str, request: Request):
    _plivo_check(key)
    p = await _plivo_params(request)
    topic = p.get("topic", "message")
    topic = topic if topic in VOICE_TOPICS else "message"
    said = p.get("Speech", "").strip()
    caller = re.sub(r"[^0-9+]", "", p.get("From", ""))[:20]
    if not said:
        if p.get("try") == "1":
            return _plivo_xml(_pspeak("Sorry, I didn't catch that.") + _plivo_ask_message(topic, attempt=2))
        return _plivo_xml(_pspeak("Sorry, we didn't get a message. Please call back, or visit cash cow A I dot online. Goodbye.") + "<Hangup/>")
    try:
        send_lead_email(
            name=f"Caller {caller or 'unknown number'}",
            email="",
            message=f"Transcribed by the phone line (check for mistakes):\n\n{said[:2000]}",
            source="phone",
            phone=caller,
            topic=VOICE_TOPICS[topic],
        )
    except Exception as e:  # still thank the caller; the log shows what went wrong
        logger.error("Phone lead email NOT sent: %s: %s", type(e).__name__, e)
    return _plivo_xml(_pspeak("Thank you. We got your message and we'll get back to you within one business day. Goodbye.") + "<Hangup/>")


# --- My Caregiver Circle: shared family care plans ---
# Data lives in the Supabase tables from caregiver_circle.sql. Only this server can
# reach them: it checks who is signed in, then the database functions check that
# the person belongs to the circle before showing or changing anything.
import uuid as _uuid

CARE_APP = "caregiver"
CARE_FREE_MEMBERS, CARE_PREMIUM_MEMBERS = 2, 10
CARE_FREE_CIRCLES, CARE_PREMIUM_CIRCLES = 1, 3
CARE_WRITE_LIMIT = 60   # changes per minute per person
_care_write_log: dict[str, list[float]] = {}

CARE_ERRORS = {
    "not_member": (403, "You're not in this circle anymore."),
    "not_owner": (403, "Only the circle's organizer can do that."),
    "limit_circles": (403, "You've reached your circle limit. Premium lets you organize up to 3 circles."),
    "limit_members": (403, "This circle is full. The organizer can upgrade to Premium for up to 10 members."),
    "bad_code": (404, "That invite code didn't match a circle. Check it and try again."),
    "already_member": (409, "You're already in this circle."),
    "not_found": (404, "That item was already removed."),
    "owner_cannot_leave": (400, "The organizer can't leave. Delete the circle instead, or keep it."),
    "bad_assignee": (400, "That person isn't in this circle."),
    "too_many_items": (400, "This circle has too many items. Delete some old ones first."),
}

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TIME_RE = re.compile(r"^\d{2}:\d{2}$")

# Allowed fields per kind: (name, max length or "date"/"time").
CARE_FIELDS = {
    "appointment": [("title", 80), ("date", "date"), ("time", "time"), ("place", 120), ("with", 80), ("notes", 500)],
    "medicine": [("name", 80), ("dose", 80), ("when", 120), ("purpose", 120), ("prescriber", 80), ("pharmacy", 80), ("notes", 500)],
    "todo": [("title", 120), ("due", "date"), ("notes", 300)],
    "card": [("person", 60), ("birth_date", "date"), ("phone", 40), ("address", 200), ("conditions", 500),
             ("allergies", 300), ("doctors", 500), ("insurance", 300), ("pharmacy", 200), ("contacts", 500),
             ("hospital", 120), ("notes", 800)],
}
CARE_REQUIRED = {"appointment": "title", "medicine": "name", "todo": "title"}


def _care_clean(kind: str, data) -> dict:
    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="Something was missing. Please try again.")
    out = {}
    for name, rule in CARE_FIELDS[kind]:
        v = data.get(name)
        if v is None:
            continue
        v = str(v).strip()
        if not v:
            continue
        if rule == "date":
            if not _DATE_RE.match(v):
                raise HTTPException(status_code=400, detail="Please pick a valid date.")
        elif rule == "time":
            if not _TIME_RE.match(v):
                raise HTTPException(status_code=400, detail="Please pick a valid time.")
        else:
            v = v[:rule]
        out[name] = v
    need = CARE_REQUIRED.get(kind)
    if need and not out.get(need):
        raise HTTPException(status_code=400, detail=f"Please add a {need}.")
    return out


def _uuid_or_400(v: str) -> str:
    try:
        return str(_uuid.UUID(str(v)))
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="That link doesn't look right.")


async def _care_user(request: Request) -> dict:
    user = await account_user(request.headers.get("authorization", ""))
    if not user:
        raise HTTPException(status_code=401, detail="Please sign in to your CashCow account.")
    return user


def _care_write_guard(user_id: str):
    now = time.time()
    hits = [t for t in _care_write_log.get(user_id, []) if now - t < 60]
    if len(hits) >= CARE_WRITE_LIMIT:
        raise HTTPException(status_code=429, detail="That's a lot of changes at once. Please wait a minute.")
    _care_write_log[user_id] = hits + [now]


async def _care_rpc(fn: str, args: dict):
    if not accounts_enabled():
        raise HTTPException(status_code=503, detail="Circles aren't set up yet.")
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(f"{AUTH_SUPABASE_URL}/rest/v1/rpc/{fn}", headers=_auth_headers(), json=args)
    except httpx.HTTPError:
        raise HTTPException(status_code=502, detail="Couldn't reach your circle right now. Please try again.")
    if resp.status_code >= 400:
        try:
            msg = str(resp.json().get("message", ""))
        except ValueError:
            msg = ""
        if msg in CARE_ERRORS:
            code, text = CARE_ERRORS[msg]
            raise HTTPException(status_code=code, detail=text)
        logger.error("care rpc %s failed: %s %s", fn, resp.status_code, msg[:200])
        raise HTTPException(status_code=502, detail="Something went wrong saving that. Please try again.")
    if not resp.content:
        return None
    try:
        return resp.json()
    except ValueError:
        return None


async def _user_premium(user_id: str, app_name: str) -> bool:
    """Same check as /api/entitlements, for any user id (used for circle limits)."""
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            resp = await client.get(
                f"{AUTH_SUPABASE_URL}/rest/v1/cashcow_entitlements",
                headers=_auth_headers(),
                params={"user_id": f"eq.{user_id}", "app": f"in.({app_name},allaccess)", "select": "*", "order": "updated_at.desc"},
            )
        rows = resp.json() if resp.status_code == 200 else []
    except (httpx.HTTPError, ValueError):
        return False
    return any(_still_active(r) for r in (rows if isinstance(rows, list) else []))


class CareCircleIn(BaseModel):
    name: str
    display_name: str


class CareJoinIn(BaseModel):
    code: str
    display_name: str


class CareItemIn(BaseModel):
    kind: str
    data: dict
    assigned_to: str | None = None


class CareItemUpdate(BaseModel):
    data: dict | None = None
    done: bool | None = None
    set_assigned: bool = False
    assigned_to: str | None = None


class CareCardIn(BaseModel):
    card: dict


class CareNameIn(BaseModel):
    name: str


class CareMemberIn(BaseModel):
    user_id: str


@app.get("/api/care/limits")
async def care_limits(request: Request):
    user = await _care_user(request)
    premium = await _user_premium(user["id"], CARE_APP)
    return {
        "premium": premium,
        "max_members": CARE_PREMIUM_MEMBERS if premium else CARE_FREE_MEMBERS,
        "max_circles": CARE_PREMIUM_CIRCLES if premium else CARE_FREE_CIRCLES,
    }


@app.get("/api/care/circles")
async def care_circles(request: Request):
    user = await _care_user(request)
    return {"circles": await _care_rpc("care_my_circles", {"p_user": user["id"]}) or []}


@app.post("/api/care/circles")
async def care_create_circle(payload: CareCircleIn, request: Request):
    user = await _care_user(request)
    _care_write_guard(user["id"])
    premium = await _user_premium(user["id"], CARE_APP)
    return await _care_rpc("care_create", {
        "p_user": user["id"], "p_name": payload.name[:200], "p_display": payload.display_name[:200],
        "p_max_owned": CARE_PREMIUM_CIRCLES if premium else CARE_FREE_CIRCLES,
    })


@app.post("/api/care/join")
async def care_join_circle(payload: CareJoinIn, request: Request):
    user = await _care_user(request)
    _care_write_guard(user["id"])
    code = re.sub(r"[^A-Za-z0-9]", "", payload.code).upper()[:12]
    if len(code) != 8:
        raise HTTPException(status_code=400, detail="Invite codes are 8 letters and numbers.")
    found = await _care_rpc("care_code_owner", {"p_code": code})
    if not found:
        raise HTTPException(status_code=404, detail=CARE_ERRORS["bad_code"][1])
    owner_premium = await _user_premium(found["owner"], CARE_APP)
    return await _care_rpc("care_join", {
        "p_user": user["id"], "p_code": code, "p_display": payload.display_name[:200],
        "p_max_members": CARE_PREMIUM_MEMBERS if owner_premium else CARE_FREE_MEMBERS,
    })


@app.get("/api/care/circles/{circle_id}")
async def care_get_circle(circle_id: str, request: Request):
    user = await _care_user(request)
    cid = _uuid_or_400(circle_id)
    data = await _care_rpc("care_get", {"p_user": user["id"], "p_circle": cid})
    if not data:
        raise HTTPException(status_code=404, detail="That circle was deleted.")
    owner_premium = await _user_premium(data["circle"]["owner"], CARE_APP)
    data["limits"] = {"premium": owner_premium, "max_members": CARE_PREMIUM_MEMBERS if owner_premium else CARE_FREE_MEMBERS}
    return data


@app.post("/api/care/circles/{circle_id}/items")
async def care_add_item(circle_id: str, payload: CareItemIn, request: Request):
    user = await _care_user(request)
    _care_write_guard(user["id"])
    if payload.kind not in CARE_REQUIRED:
        raise HTTPException(status_code=400, detail="Unknown item type.")
    return await _care_rpc("care_add_item", {
        "p_user": user["id"], "p_circle": _uuid_or_400(circle_id), "p_kind": payload.kind,
        "p_data": _care_clean(payload.kind, payload.data),
        "p_assigned": _uuid_or_400(payload.assigned_to) if payload.assigned_to else None,
    })


@app.post("/api/care/items/{item_id}/update")
async def care_update_item(item_id: str, payload: CareItemUpdate, request: Request):
    user = await _care_user(request)
    _care_write_guard(user["id"])
    data = None
    if payload.data is not None:
        kind = str(payload.data.get("_kind", ""))
        if kind not in CARE_REQUIRED:
            raise HTTPException(status_code=400, detail="Unknown item type.")
        data = _care_clean(kind, payload.data)
    await _care_rpc("care_update_item", {
        "p_user": user["id"], "p_item": _uuid_or_400(item_id), "p_data": data, "p_done": payload.done,
        "p_set_assigned": payload.set_assigned,
        "p_assigned": _uuid_or_400(payload.assigned_to) if (payload.set_assigned and payload.assigned_to) else None,
    })
    return {"ok": True}


@app.post("/api/care/items/{item_id}/delete")
async def care_delete_item(item_id: str, request: Request):
    user = await _care_user(request)
    _care_write_guard(user["id"])
    await _care_rpc("care_delete_item", {"p_user": user["id"], "p_item": _uuid_or_400(item_id)})
    return {"ok": True}


@app.post("/api/care/circles/{circle_id}/card")
async def care_set_card(circle_id: str, payload: CareCardIn, request: Request):
    user = await _care_user(request)
    _care_write_guard(user["id"])
    await _care_rpc("care_set_card", {"p_user": user["id"], "p_circle": _uuid_or_400(circle_id),
                                      "p_card": _care_clean("card", payload.card)})
    return {"ok": True}


@app.post("/api/care/circles/{circle_id}/rename")
async def care_rename_circle(circle_id: str, payload: CareNameIn, request: Request):
    user = await _care_user(request)
    _care_write_guard(user["id"])
    await _care_rpc("care_rename", {"p_user": user["id"], "p_circle": _uuid_or_400(circle_id), "p_name": payload.name[:200]})
    return {"ok": True}


@app.post("/api/care/circles/{circle_id}/code")
async def care_new_code(circle_id: str, request: Request):
    user = await _care_user(request)
    _care_write_guard(user["id"])
    code = await _care_rpc("care_new_code", {"p_user": user["id"], "p_circle": _uuid_or_400(circle_id)})
    return {"invite_code": code}


@app.post("/api/care/circles/{circle_id}/remove")
async def care_remove_member(circle_id: str, payload: CareMemberIn, request: Request):
    user = await _care_user(request)
    _care_write_guard(user["id"])
    await _care_rpc("care_remove_member", {"p_user": user["id"], "p_circle": _uuid_or_400(circle_id),
                                           "p_member": _uuid_or_400(payload.user_id)})
    return {"ok": True}


@app.post("/api/care/circles/{circle_id}/leave")
async def care_leave_circle(circle_id: str, request: Request):
    user = await _care_user(request)
    await _care_rpc("care_leave", {"p_user": user["id"], "p_circle": _uuid_or_400(circle_id)})
    return {"ok": True}


@app.post("/api/care/circles/{circle_id}/delete")
async def care_delete_circle(circle_id: str, request: Request):
    user = await _care_user(request)
    await _care_rpc("care_delete_circle", {"p_user": user["id"], "p_circle": _uuid_or_400(circle_id)})
    return {"ok": True}
