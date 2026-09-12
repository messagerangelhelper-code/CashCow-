import os
import httpx
from datetime import datetime, timezone
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

app = FastAPI(title="CashCow Reviews API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # public review site, no sensitive data involved
    allow_methods=["*"],
    allow_headers=["*"],
)


class ReviewRequest(BaseModel):
    name: str
    product: str  # "ScamShield", "Encompass", "Integrity Records", or "General"
    rating: int  # 1-5
    comment: str


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
async def submit_review(payload: ReviewRequest):
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
