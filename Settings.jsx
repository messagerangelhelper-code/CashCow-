import { ALLERGENS, AVOID_OPTIONS } from "./ingredients.js";

function Chips({ options, selected, onToggle, label }) {
  return (
    <div className="chips" role="group" aria-label={label}>
      {options.map((o) => {
        const on = selected.includes(o.id);
        return (
          <button key={o.id} type="button" className={on ? "chip on" : "chip"} aria-pressed={on} onClick={() => onToggle(o.id)}>
            {on ? "✓ " : ""}{o.name}
          </button>
        );
      })}
    </div>
  );
}

// "Who are you shopping for?" - allergies, things to avoid and big text.
export default function Settings({ settings, onChange, onDone, firstTime, premium, onUpgrade }) {
  const toggle = (field) => (id) => {
    const list = settings[field].includes(id) ? settings[field].filter((x) => x !== id) : [...settings[field], id];
    onChange({ ...settings, [field]: list });
  };

  return (
    <section className="card settings">
      <h2>{firstTime ? "Set up your checks" : "Your checks"}</h2>
      <p className="muted">
        Every scan always shows ingredients that are banned or being removed. Pick anything else you or your family need to watch for.
        This is saved on this phone only.
      </p>

      <h3>Allergies</h3>
      <Chips options={ALLERGENS} selected={settings.allergens} onToggle={toggle("allergens")} label="Allergies" />

      <h3>Things I'd rather avoid {!premium && <span className="pill-premium">Premium</span>}</h3>
      {premium ? (
        <Chips options={AVOID_OPTIONS} selected={settings.avoid} onToggle={toggle("avoid")} label="Things to avoid" />
      ) : (
        <>
          <div className="chips locked" aria-hidden="true">
            {AVOID_OPTIONS.map((o) => <span key={o.id} className="chip">{o.name}</span>)}
          </div>
          <p className="muted small">Get alerts for the things you choose to avoid with Premium.</p>
          <button type="button" className="btn-secondary" onClick={onUpgrade}>See Premium</button>
        </>
      )}

      <h3>Easier reading</h3>
      <label className="toggle">
        <input type="checkbox" checked={settings.bigText} onChange={(e) => onChange({ ...settings, bigText: e.target.checked })} />
        <span>Bigger text and buttons</span>
      </label>

      <button type="button" className="btn-primary" onClick={onDone}>{firstTime ? "Save and start scanning" : "Done"}</button>
    </section>
  );
}
