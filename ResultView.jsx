import { useState } from "react";
import { analyze, NOVA } from "./analyze.js";
import { fetchSimilar, productName, mainCategory, categoryLabel } from "./foodApi.js";
import { LAST_REVIEWED } from "./ingredients.js";

const VERDICT_ICON = { red: "⛔", amber: "⚠️", green: "✅", unknown: "❔" };

function fmt(n) {
  if (n === null || n === undefined) return "—";
  return n >= 10 ? n.toFixed(0) : n.toFixed(1).replace(/\.0$/, "");
}

function NutriScore({ grade }) {
  if (!grade) return <span className="muted">Not rated</span>;
  return <span className={`nutri nutri-${grade}`}>{grade.toUpperCase()}</span>;
}

function Summary({ r }) {
  const parts = [];
  if (r.flags.length) parts.push(`${r.flags.length} flagged ingredient${r.flags.length === 1 ? "" : "s"}`);
  const mine = r.myAllergens.filter((a) => a.contains).length;
  if (mine) parts.push(`${mine} of your allergen${mine === 1 ? "" : "s"}`);
  if (r.avoid.length) parts.push(`${r.avoid.length} thing${r.avoid.length === 1 ? "" : "s"} on your avoid list`);
  return parts.length ? parts.join(" · ") : "Nothing banned, restricted or on your lists.";
}

// Short reasons an alternative is better than the scanned product.
function whyBetter(alt, cur) {
  const out = [];
  if (alt.flags.length < cur.flags.length) out.push(alt.flags.length === 0 ? "No flagged ingredients" : `${alt.flags.length} flagged vs ${cur.flags.length}`);
  if (alt.myAllergens.filter((a) => a.contains).length < cur.myAllergens.filter((a) => a.contains).length) out.push("Free of your allergens");
  if (alt.avoid.length < cur.avoid.length) out.push(alt.avoid.length === 0 ? "Nothing on your avoid list" : "Fewer avoid-list items");
  if (alt.nutriscore && cur.nutriscore && alt.nutriscore < cur.nutriscore) out.push(`Nutri-Score ${alt.nutriscore.toUpperCase()} vs ${cur.nutriscore.toUpperCase()}`);
  if (alt.nova && cur.nova && alt.nova < cur.nova) out.push("Less processed");
  const s = (r) => r.nutrition.find((n) => n.id === "sugars")?.value;
  if (s(alt) !== null && s(cur) !== null && s(alt) !== undefined && s(cur) !== undefined && s(alt) < s(cur) * 0.75) out.push("Less sugar");
  return out.slice(0, 3);
}

function CompareTable({ a, b, onClose }) {
  const ra = a.result;
  const rb = b.result;
  const allergenText = (r) => (r.myAllergens.filter((x) => x.contains).map((x) => x.name).join(", ") || "None of yours");
  const rows = [
    ["Flagged ingredients", ra.flags.length ? ra.flags.map((f) => f.name.split(" (")[0]).join(", ") : "None", rb.flags.length ? rb.flags.map((f) => f.name.split(" (")[0]).join(", ") : "None", ra.flags.length - rb.flags.length],
    ["Your allergens", allergenText(ra), allergenText(rb), ra.myAllergens.filter((x) => x.contains).length - rb.myAllergens.filter((x) => x.contains).length],
    ["On your avoid list", ra.avoid.map((x) => x.name).join(", ") || "None", rb.avoid.map((x) => x.name).join(", ") || "None", ra.avoid.length - rb.avoid.length],
    ["Nutri-Score", <NutriScore key="a" grade={ra.nutriscore} />, <NutriScore key="b" grade={rb.nutriscore} />, ra.nutriscore && rb.nutriscore ? (ra.nutriscore > rb.nutriscore ? 1 : ra.nutriscore < rb.nutriscore ? -1 : 0) : 0],
    ["Processing", ra.nova ? NOVA[ra.nova] : "—", rb.nova ? NOVA[rb.nova] : "—", ra.nova && rb.nova ? ra.nova - rb.nova : 0],
  ];
  for (const id of ["sugars", "salt", "satfat", "fiber", "proteins"]) {
    const na = ra.nutrition.find((n) => n.id === id);
    const nb = rb.nutrition.find((n) => n.id === id);
    const higherIsBetter = id === "fiber" || id === "proteins";
    let diff = 0;
    if (na.value !== null && nb.value !== null) diff = higherIsBetter ? nb.value - na.value : na.value - nb.value;
    rows.push([`${na.name} per ${na.unit}`, na.value === null ? "—" : `${fmt(na.value)} g`, nb.value === null ? "—" : `${fmt(nb.value)} g`, Math.abs(diff) < 0.05 ? 0 : diff > 0 ? 1 : -1]);
  }
  return (
    <section className="card compare">
      <div className="row-between">
        <h2>Side by side</h2>
        <button type="button" className="link" onClick={onClose}>Close</button>
      </div>
      <div className="compare-scroll">
        <table>
          <thead>
            <tr>
              <th scope="col"><span className="sr-only">What</span></th>
              <th scope="col">{productName(a.product)}<span className="muted small">Scanned</span></th>
              <th scope="col">{productName(b.product)}<span className="muted small">Option</span></th>
            </tr>
          </thead>
          <tbody>
            {rows.map(([label, va, vb, better]) => (
              <tr key={label}>
                <th scope="row">{label}</th>
                <td className={better < 0 ? "better" : ""}>{va}</td>
                <td className={better > 0 ? "better" : ""}>{vb}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="muted small">Green shading marks the better value in each row.</p>
    </section>
  );
}

export default function ResultView({ product, result, settings, onScanAgain, premium, onUpgrade }) {
  const r = result;
  const [alts, setAlts] = useState(null); // null = not loaded, [] = none found
  const [altsBusy, setAltsBusy] = useState(false);
  const [altsError, setAltsError] = useState("");
  const [comparing, setComparing] = useState(null);
  const others = r.allergens.filter((a) => !a.chosen);
  const isManual = !product.code;

  async function findBetter() {
    setAltsBusy(true);
    setAltsError("");
    try {
      const list = await fetchSimilar(product);
      const ranked = list
        .map((p) => ({ product: p, result: analyze(p, settings) }))
        .filter((x) => x.result.hasIngredients && x.result.score < r.score - 0.5 && !x.result.myAllergens.some((a) => a.contains))
        .sort((x, y) => x.result.score - y.result.score)
        .slice(0, 5);
      setAlts(ranked);
    } catch {
      setAltsError("Couldn't load other products right now. Please try again in a minute.");
    } finally {
      setAltsBusy(false);
    }
  }

  return (
    <div className="result">
      <section className="card product-head">
        {product.image_front_small_url && <img src={product.image_front_small_url} alt="" className="product-img" />}
        <div>
          <h2>{productName(product)}</h2>
          {(product.brands || product.quantity) && <p className="muted">{[product.brands, product.quantity].filter(Boolean).join(" · ")}</p>}
          {product.code && <p className="muted small">Barcode {product.code}</p>}
        </div>
      </section>

      <section className={`card verdict verdict-${r.verdict}`} role="status">
        <p className="verdict-title"><span aria-hidden="true">{VERDICT_ICON[r.verdict]}</span> {r.headline}</p>
        {r.verdict === "unknown" ? (
          <p>This product is in the database, but nobody has added its ingredient list yet. Type or paste the ingredients from the package below to check them.</p>
        ) : (
          <>
            <p><Summary r={r} /></p>
            {r.nova === 4 && r.verdict !== "red" && (
              <p className="small">Note: this is an ultra-processed food. Nothing on it is banned, but eating fewer ultra-processed foods is a common health goal.</p>
            )}
          </>
        )}
      </section>

      {r.myAllergens.length > 0 && (
        <section className="card alert-allergen">
          <h3>Your allergens</h3>
          <ul className="plain">
            {r.myAllergens.map((a) => (
              <li key={a.id}><strong>{a.contains ? "Contains" : "May contain"} {a.name.toLowerCase()}</strong>{a.found ? ` (listed as "${a.found}")` : ""}</li>
            ))}
          </ul>
          <p className="muted small">Always double-check the package. Recipes change, and the database can be out of date.</p>
        </section>
      )}

      {r.flags.length > 0 && (
        <section className="card">
          <h3>Flagged ingredients</h3>
          <ul className="flags">
            {r.flags.map((f) => (
              <li key={f.id} className={`flag flag-${f.level}`}>
                <div className="flag-head">
                  <strong>{f.name}</strong>
                  <span className={`badge badge-${f.level}`}>{f.levelInfo.short}</span>
                </div>
                <p className="small muted">Found on label as: {f.found}</p>
                <p>{f.status}</p>
                <details className="flag-more">
                  <summary>Why it's flagged and sources</summary>
                  <p className="muted">{f.why}</p>
                  <p className="sources small">
                    Sources:{" "}
                    {f.sources.map((s, i) => (
                      <span key={s.url}>{i > 0 && " · "}<a href={s.url} target="_blank" rel="noopener noreferrer">{s.name}</a></span>
                    ))}
                  </p>
                </details>
              </li>
            ))}
          </ul>
        </section>
      )}

      {r.avoid.length > 0 && (
        <section className="card">
          <h3>On your avoid list</h3>
          <ul className="plain">
            {r.avoid.map((o) => <li key={o.id}><strong>{o.name}:</strong> {o.hits.join(", ")}</li>)}
          </ul>
        </section>
      )}

      {others.length > 0 && (
        <section className="card">
          <h3>Other allergens in this product</h3>
          <p>{others.map((a) => `${a.maybe ? "may contain " : ""}${a.name.toLowerCase()}`).join(", ")}</p>
        </section>
      )}

      {!isManual && (
        <section className="card">
          <h3>Nutrition</h3>
          <div className="scores">
            <div><span className="muted small">Nutri-Score</span><NutriScore grade={r.nutriscore} /></div>
            <div><span className="muted small">Processing</span><strong>{r.nova ? NOVA[r.nova] : "Not rated"}</strong></div>
          </div>
          <table className="nutrition">
            <tbody>
              {r.nutrition.filter((n) => n.value !== null).map((n) => (
                <tr key={n.id}>
                  <th scope="row">{n.name}</th>
                  <td>{fmt(n.value)} g <span className="muted small">per {n.unit}</span></td>
                  <td>{n.level && <span className={`level level-${n.level}`}>{n.level}</span>}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {r.nutrition.every((n) => n.value === null) && <p className="muted">No nutrition facts in the database yet.</p>}
        </section>
      )}

      {r.ingredientsText && (
        <details className="card">
          <summary>Full ingredient list</summary>
          <p className="ingredients">{r.ingredientsText}</p>
        </details>
      )}

      {!isManual && mainCategory(product) && r.verdict !== "unknown" && (
        <section className="card">
          <h3>Healthier options {!premium && <span className="pill-premium">Premium</span>}</h3>
          {!premium ? (
            <>
              <p className="muted">See similar products ({categoryLabel(mainCategory(product)).toLowerCase()}) with cleaner ingredients, and compare them side by side.</p>
              <button type="button" className="btn-secondary" onClick={onUpgrade}>Unlock with Premium</button>
            </>
          ) : alts === null ? (
            <>
              <p className="muted">Look for similar products ({categoryLabel(mainCategory(product)).toLowerCase()}) with cleaner ingredients.</p>
              <button type="button" className="btn-primary" onClick={findBetter} disabled={altsBusy}>{altsBusy ? "Looking…" : "Find healthier options"}</button>
            </>
          ) : alts.length === 0 ? (
            <p className="muted">We didn't find a clearly better option in this category. That can mean this is already a good pick, or the database doesn't have enough similar products yet.</p>
          ) : (
            <ul className="alts">
              {alts.map((x) => (
                <li key={x.product.code} className="alt">
                  {x.product.image_front_small_url && <img src={x.product.image_front_small_url} alt="" />}
                  <div className="alt-body">
                    <strong>{productName(x.product)}</strong>
                    {x.product.brands && <span className="muted small">{x.product.brands}</span>}
                    <span className={`chip-mini chip-${x.result.verdict}`}>{VERDICT_ICON[x.result.verdict]} {x.result.headline}</span>
                    <ul className="why">{whyBetter(x.result, r).map((w) => <li key={w}>{w}</li>)}</ul>
                    <button type="button" className="link" onClick={() => setComparing(x)}>Compare side by side</button>
                  </div>
                </li>
              ))}
            </ul>
          )}
          {altsError && <p className="error">{altsError}</p>}
        </section>
      )}

      {comparing && <CompareTable a={{ product, result: r }} b={comparing} onClose={() => setComparing(null)} />}

      <button type="button" className="btn-primary wide" onClick={onScanAgain}>Scan another product</button>

      <p className="disclaimer">
        Information only, not medical advice. Product data comes from Open Food Facts, a free public database, and may be incomplete or out of date,
        so always check the package. Ingredient rules last reviewed {LAST_REVIEWED}.
      </p>
    </div>
  );
}
