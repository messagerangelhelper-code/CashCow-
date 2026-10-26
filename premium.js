// Premium for My Health Check Scan, paid through CashCow (Stripe checkout).
// After paying, Stripe sends the person back here with ?session_id=..., and the
// app asks CashCow whether that purchase is paid (and, for subscriptions, still active).
// Premium is remembered on this phone; it's re-checked every few days.
const API = "https://cashcow-reviews-api.onrender.com";
const APP = "healthscan";
const KEY = "mhcs-premium";
const RECHECK_MS = 3 * 24 * 60 * 60 * 1000;

export const PLANS = [
  { id: "hcs_monthly", label: "Monthly", price: "$2.99", per: "/month" },
  { id: "hcs_yearly", label: "Yearly", price: "$19.99", per: "/year", note: "Save 44%" },
  { id: "hcs_lifetime", label: "Lifetime", price: "$39.99", per: "one time", note: "Pay once" },
];

export const PREMIUM_FEATURES = [
  "Healthier options for every product",
  "Side-by-side comparison chart",
  "Your personal avoid list (dyes, added sugar, seed oils and more)",
  "Scan history of your last 50 products",
];

export const FREE_HISTORY = 3;
export const PREMIUM_HISTORY = 50;

function read() {
  try {
    return JSON.parse(localStorage.getItem(KEY) || "null");
  } catch {
    return null;
  }
}

function write(v) {
  try {
    if (v) localStorage.setItem(KEY, JSON.stringify(v));
    else localStorage.removeItem(KEY);
  } catch {
    /* storage not available */
  }
}

export function isPremium() {
  const s = read();
  return !!(s && s.premium);
}

async function verify(sessionId) {
  const res = await fetch(`${API}/api/verify-session?session_id=${encodeURIComponent(sessionId)}&app=${APP}`);
  if (!res.ok) throw new Error("verify failed");
  return res.json();
}

// Runs the check only once per page load, even if asked twice.
let once = null;
export function checkPremiumOnce() {
  if (!once) once = checkPremium();
  return once;
}

// Call once when the app opens. Returns "activated", "expired" or null.
export async function checkPremium() {
  const params = new URLSearchParams(window.location.search);
  const fresh = params.get("session_id");
  if (fresh) {
    params.delete("session_id");
    const rest = params.toString();
    window.history.replaceState(null, "", window.location.pathname + (rest ? `?${rest}` : ""));
    try {
      const r = await verify(fresh);
      if (r.premium) {
        write({ premium: true, sessionId: fresh, plan: r.plan || "", checkedAt: Date.now() });
        return "activated";
      }
    } catch {
      // CashCow may be waking up; keep the session so we can try again next time.
      write({ premium: false, sessionId: fresh, checkedAt: 0 });
      return null;
    }
  }

  const s = read();
  if (!s || !s.sessionId || Date.now() - (s.checkedAt || 0) < RECHECK_MS) return null;
  try {
    const r = await verify(s.sessionId);
    write({ ...s, premium: !!r.premium, plan: r.plan || s.plan, checkedAt: Date.now() });
    if (s.premium && !r.premium) return "expired";
    if (!s.premium && r.premium) return "activated";
  } catch {
    /* offline or server asleep: keep what we have */
  }
  return null;
}

// Sends the person to Stripe checkout. Throws a friendly message on failure.
export async function startCheckout(planId) {
  let res;
  try {
    res = await fetch(`${API}/api/create-checkout-session`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ plan: planId }),
    });
  } catch {
    throw new Error("Couldn't reach checkout. Check your connection and try again.");
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok || !data.url) throw new Error(data.detail || "Checkout isn't available right now. Please try again in a minute.");
  window.location.href = data.url;
}
