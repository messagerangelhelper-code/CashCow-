import os
import re
import time
import httpx
from datetime import datetime, timezone
from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, field_validator

app = FastAPI(title="CashCow Reviews API")

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
