import { useState } from "react";
import { PLANS, PREMIUM_FEATURES, startCheckout } from "./premium.js";

// Plans and upgrade buttons. Payment happens on Stripe's secure checkout page.
export default function Premium({ premium, onBack }) {
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");

  async function buy(id) {
    setError("");
    setBusy(id);
    try {
      await startCheckout(id);
    } catch (e) {
      setError(e.message);
      setBusy("");
    }
  }

  if (premium) {
    return (
      <section className="card premium-card">
        <h2>You're on Premium ✅</h2>
        <p>Thank you! Healthier options, comparisons, your avoid list and full history are all unlocked on this phone.</p>
        <button type="button" className="btn-primary" onClick={onBack}>Back to scanning</button>
      </section>
    );
  }

  return (
    <section className="card premium-card">
      <h2>Go Premium</h2>
      <p className="muted">Scanning, banned-ingredient flags, allergens and nutrition stay free. Premium adds:</p>
      <ul className="premium-list">
        {PREMIUM_FEATURES.map((f) => <li key={f}>{f}</li>)}
      </ul>
      <div className="plans">
        {PLANS.map((p) => (
          <div key={p.id} className={p.id === "hcs_yearly" ? "plan best" : "plan"}>
            <div>
              <strong>{p.label}</strong>
              {p.note && <span className="plan-note">{p.note}</span>}
              <p className="plan-price">{p.price} <span className="muted small">{p.per}</span></p>
            </div>
            <button type="button" className="btn-primary" disabled={!!busy} onClick={() => buy(p.id)}>
              {busy === p.id ? "Opening…" : "Choose"}
            </button>
          </div>
        ))}
      </div>
      {busy && <p className="muted small">Opening secure checkout. The first time can take up to a minute.</p>}
      {error && <p className="error">{error}</p>}
      <p className="muted small">
        Payments are handled securely by Stripe through CashCow. Monthly and yearly plans renew until you cancel.
        Premium is saved on the phone you buy it on.
      </p>
      <button type="button" className="link" onClick={onBack}>Not now</button>
    </section>
  );
}
