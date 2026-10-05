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
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
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
    "rent": {
        "label": "Rent",
        "name": "CashCow — Rent",
        "amount": 2900,      # $29.00, in cents
        "mode": "subscription",
        "interval": "month",
    },
    "lease": {
        "label": "Lease",
        "name": "CashCow — Lease",
        "amount": 24900,     # $249.00
        "mode": "subscription",
        "interval": "year",
    },
    "buy": {
        "label": "Buy",
        "name": "CashCow — Buy",
        "amount": 49900,     # $499.00
        "mode": "payment",
        "interval": None,
    },
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
    }


@app.post("/api/create-checkout-session")
async def create_checkout_session(payload: CheckoutRequest, request: Request):
    check_rate_limit(request)

    if not STRIPE_SECRET_KEY:
        raise HTTPException(status_code=500, detail="Payments aren't configured yet.")

    plan = PLANS[payload.plan]
    price_data = {
        "currency": "usd",
        "product_data": {"name": plan["name"]},
        "unit_amount": plan["amount"],
    }
    if plan["mode"] == "subscription":
        price_data["recurring"] = {"interval": plan["interval"]}

    try:
        session = stripe.checkout.Session.create(
            mode=plan["mode"],
            line_items=[{"price_data": price_data, "quantity": 1}],
            success_url=f"{SITE_URL}/success.html?plan={payload.plan}",
            cancel_url=f"{SITE_URL}/cart.html?plan={payload.plan}",
        )
    except stripe.error.StripeError as e:
        raise HTTPException(status_code=400, detail=str(e))

    return {"url": session.url}


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
