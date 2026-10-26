import { useCallback, useEffect, useState } from "react";
import Scanner from "./Scanner.jsx";
import Settings from "./Settings.jsx";
import ResultView from "./ResultView.jsx";
import { analyze } from "./analyze.js";
import { fetchProduct, cleanBarcode, productName } from "./foodApi.js";
import { loadSettings, saveSettings, loadHistory, addToHistory, clearHistory } from "./storage.js";
import { ALLERGENS, AVOID_OPTIONS } from "./ingredients.js";
import { BRAND } from "./brand.js";
import Premium from "./Premium.jsx";
import { isPremium, checkPremiumOnce, FREE_HISTORY, PREMIUM_HISTORY } from "./premium.js";

const VERDICT_ICON = { red: "⛔", amber: "⚠️", green: "✅", unknown: "❔" };

function ManualCheck({ onCheck, title = "Check an ingredient list", hint }) {
  const [text, setText] = useState("");
  return (
    <form className="manual" onSubmit={(e) => { e.preventDefault(); if (text.trim().length > 3) onCheck(text); }}>
      <label htmlFor="manual-text" className="label">{title}</label>
      {hint && <p className="muted small">{hint}</p>}
      <textarea id="manual-text" rows={4} value={text} onChange={(e) => setText(e.target.value)}
        placeholder="e.g. Sugar, enriched flour, Red 40, Yellow 5, BHA…" />
      <button type="submit" className="btn-secondary" disabled={text.trim().length <= 3}>Check ingredients</button>
    </form>
  );
}

export default function App() {
  const [settings, setSettings] = useState(loadSettings);
  const [history, setHistory] = useState(loadHistory);
  const [view, setView] = useState(() => (loadSettings().setupDone ? "home" : "settings"));
  const [barcode, setBarcode] = useState("");
  const [product, setProduct] = useState(null);
  const [error, setError] = useState("");
  const [lastCode, setLastCode] = useState("");
  const [premium, setPremium] = useState(isPremium);
  const [notice, setNotice] = useState("");

  useEffect(() => {
    checkPremiumOnce().then((status) => {
      setPremium(isPremium());
      if (status === "activated") {
        setNotice("Premium is active. Thank you! Healthier options, comparisons, your avoid list and full history are unlocked.");
      } else if (status === "expired") {
        setNotice("Your Premium plan has ended. Scanning and banned-ingredient flags are still free.");
      }
    });
  }, []);

  useEffect(() => {
    const root = document.documentElement.style;
    root.setProperty("--green", BRAND.colors.main);
    root.setProperty("--green-2", BRAND.colors.mainHover);
    root.setProperty("--mint", BRAND.colors.soft);
    document.title = BRAND.name;
  }, []);

  useEffect(() => {
    document.documentElement.classList.toggle("big-text", !!settings.bigText);
  }, [settings.bigText]);

  useEffect(() => {
    window.scrollTo({ top: 0 });
  }, [view]);

  const updateSettings = (s) => {
    setSettings(s);
    saveSettings(s);
  };

  const lookUp = useCallback(async (code) => {
    const clean = cleanBarcode(code);
    setError("");
    setLastCode(clean);
    setView("loading");
    try {
      const p = await fetchProduct(clean);
      if (!p) {
        setView("notfound");
        return;
      }
      setProduct(p);
      const s0 = loadSettings();
      const r = analyze(p, isPremium() ? s0 : { ...s0, avoid: [] });
      setHistory(addToHistory({ code: p.code, name: productName(p), brand: p.brands || "", verdict: r.verdict, time: Date.now() }));
      setView("result");
    } catch (e) {
      setError(e.message || "Something went wrong. Please try again.");
      setView("home");
    }
  }, []);

  const onScanned = useCallback((code) => lookUp(code), [lookUp]);
  const closeScanner = useCallback(() => setView("home"), []);

  function checkText(text, base = {}) {
    setProduct({ ...base, product_name: base.product_name || "Ingredients you entered", ingredients_text: text });
    setView("result");
  }

  // Free users: the avoid list is a Premium feature, so it's left out of the checks.
  const effective = premium ? settings : { ...settings, avoid: [] };
  const result = product ? analyze(product, effective) : null;
  const chosen = [
    ...ALLERGENS.filter((a) => effective.allergens.includes(a.id)).map((a) => a.name),
    ...AVOID_OPTIONS.filter((o) => effective.avoid.includes(o.id)).map((o) => o.name),
  ];
  const shownHistory = history.slice(0, premium ? PREMIUM_HISTORY : FREE_HISTORY);
  const goPremium = () => setView("premium");

  return (
    <div className="app">
      <header className="top">
        <button type="button" className="brand" onClick={() => setView("home")} aria-label={`${BRAND.name} home`}>
          <span className="logo" aria-hidden="true">✓</span>
          <span>{BRAND.name}</span>
        </button>
        {view !== "settings" && (
          <button type="button" className="link light" onClick={() => setView("settings")}>My checks</button>
        )}
      </header>

      <main>
        {notice && (
          <div className="notice" role="status">
            <span>{notice}</span>
            <button type="button" className="link" onClick={() => setNotice("")} aria-label="Dismiss">✕</button>
          </div>
        )}

        {view === "premium" && <Premium premium={premium} onBack={() => setView(product ? "result" : "home")} />}

        {view === "settings" && (
          <Settings
            settings={settings}
            firstTime={!settings.setupDone}
            premium={premium}
            onUpgrade={goPremium}
            onChange={updateSettings}
            onDone={() => { updateSettings({ ...settings, setupDone: true }); setView(product ? "result" : "home"); }}
          />
        )}

        {view === "home" && (
          <>
            <section className="card hero">
              <h1>What's really in your food?</h1>
              <p>{BRAND.tagline}</p>
              <button type="button" className="btn-primary big" onClick={() => { setError(""); setView("scan"); }}>
                <span aria-hidden="true">📷</span> Scan a barcode
              </button>
              <form className="barcode-form" onSubmit={(e) => { e.preventDefault(); if (cleanBarcode(barcode).length >= 6) lookUp(barcode); }}>
                <label htmlFor="barcode" className="label">Or type the barcode numbers</label>
                <div className="inline">
                  <input id="barcode" inputMode="numeric" autoComplete="off" value={barcode}
                    onChange={(e) => setBarcode(e.target.value)} placeholder="e.g. 016000275287" />
                  <button type="submit" className="btn-secondary" disabled={cleanBarcode(barcode).length < 6}>Look up</button>
                </div>
              </form>
              {error && <p className="error">{error}</p>}
            </section>

            <section className="card">
              <div className="row-between">
                <h2>Your checks</h2>
                <button type="button" className="link" onClick={() => setView("settings")}>Change</button>
              </div>
              <p className="muted">Always checking for banned and restricted ingredients{chosen.length ? ", plus:" : "."}</p>
              {chosen.length > 0 && <div className="chips">{chosen.map((c) => <span key={c} className="chip on static">{c}</span>)}</div>}
            </section>

            <section className="card">
              <ManualCheck onCheck={(t) => checkText(t)} hint="No barcode? Type or paste the ingredients from the package." />
            </section>

            <section className={premium ? "card premium-teaser on" : "card premium-teaser"}>
              {premium ? (
                <p><strong>Premium ✓</strong> <span className="muted">Healthier options, comparisons, avoid list and full history are on.</span></p>
              ) : (
                <>
                  <h2>Go Premium</h2>
                  <p className="muted">Healthier options, side-by-side comparisons, your personal avoid list and full scan history. From $2.99 a month.</p>
                  <button type="button" className="btn-primary" onClick={goPremium}>See plans</button>
                </>
              )}
            </section>

            {history.length > 0 && (
              <section className="card">
                <div className="row-between">
                  <h2>Recent scans</h2>
                  <button type="button" className="link" onClick={() => setHistory(clearHistory())}>Clear</button>
                </div>
                <ul className="history">
                  {shownHistory.map((h) => (
                    <li key={h.code}>
                      <button type="button" onClick={() => lookUp(h.code)}>
                        <span aria-hidden="true">{VERDICT_ICON[h.verdict] || "•"}</span>
                        <span className="h-name">{h.name}<span className="muted small">{h.brand}</span></span>
                      </button>
                    </li>
                  ))}
                </ul>
                {!premium && history.length > FREE_HISTORY && (
                  <p className="muted small">Showing your last {FREE_HISTORY}. <button type="button" className="link" onClick={goPremium}>Premium</button> keeps your last {PREMIUM_HISTORY}.</p>
                )}
              </section>
            )}

            <p className="disclaimer">
              Information only, not medical advice. Product data comes from Open Food Facts, a free public database.
            </p>
          </>
        )}

        {view === "scan" && (
          <section className="card">
            <h2>Scan a barcode</h2>
            <Scanner onCode={onScanned} onClose={closeScanner} />
          </section>
        )}

        {view === "loading" && (
          <section className="card center">
            <div className="spinner" aria-hidden="true" />
            <p>Looking up barcode {lastCode}…</p>
          </section>
        )}

        {view === "notfound" && (
          <section className="card">
            <h2>We couldn't find that product</h2>
            <p>Barcode {lastCode} isn't in the food database yet. You can still check it: type or paste the ingredients from the package.</p>
            <ManualCheck onCheck={(t) => checkText(t, { product_name: `Product ${lastCode}` })} title="Ingredients from the package" />
            <div className="button-row">
              <button type="button" className="btn-primary" onClick={() => setView("scan")}>Scan again</button>
              <button type="button" className="link" onClick={() => setView("home")}>Back</button>
            </div>
          </section>
        )}

        {view === "result" && product && result && (
          <>
            <ResultView key={(product.code || product.ingredients_text) + String(premium)} product={product} result={result} settings={effective}
              onScanAgain={() => setView("scan")} premium={premium} onUpgrade={goPremium} />
            {result.verdict === "unknown" && (
              <section className="card">
                <ManualCheck onCheck={(t) => checkText(t, { ...product })} title="Ingredients from the package" />
              </section>
            )}
          </>
        )}
      </main>
    </div>
  );
}
