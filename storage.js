// Saved on this phone only: your check settings and recent scans.
const SETTINGS_KEY = "mhcs-settings";
const HISTORY_KEY = "mhcs-history";

function read(key, fallback) {
  try {
    const v = JSON.parse(localStorage.getItem(key) || "null");
    return v ?? fallback;
  } catch {
    return fallback;
  }
}

function write(key, value) {
  try {
    localStorage.setItem(key, JSON.stringify(value));
  } catch {
    /* storage not available (private window); the app still works */
  }
}

export const DEFAULT_SETTINGS = { allergens: [], avoid: [], bigText: false, setupDone: false };

export function loadSettings() {
  return { ...DEFAULT_SETTINGS, ...read(SETTINGS_KEY, {}) };
}

export function saveSettings(s) {
  write(SETTINGS_KEY, s);
}

export function loadHistory() {
  const h = read(HISTORY_KEY, []);
  return Array.isArray(h) ? h : [];
}

export function addToHistory(entry) {
  const list = [entry, ...loadHistory().filter((h) => h.code !== entry.code)].slice(0, 50);
  write(HISTORY_KEY, list);
  return list;
}

export function clearHistory() {
  write(HISTORY_KEY, []);
  return [];
}
