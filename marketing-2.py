"""
CashCow marketing report: what people look for, per app, every Monday.

Two free sources, no sign-ups:
  1. Your own sites. One line added to each app's page sends anonymous visits and searches
     (which page, which website they came from, what they typed in a search box). No names,
     emails, IP addresses or cookies, and visitors with "Do Not Track" / Global Privacy Control on are skipped.
  2. Public search suggestions. For each app's starting keywords, the words people type into
     DuckDuckGo and Bing search boxes. Suggestions that weren't there last week are marked "new".

Ad-platform keywords (search volumes and costs) are an upgrade switch for later, once an ad account is connected.
Tables: marketing.sql (mkt_apps, mkt_events). The report is kept in ecom_settings (_mkt_report, _mkt_seen).
"""
import asyncio
import json
import re
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from urllib.parse import quote, urljoin

import httpx
from fastapi import HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel

import ecom
from ecom import db, clean, save_setting

API_BASE = "https://cashcow-reviews-api.onrender.com"
MODIFIERS = ["", "best ", "how to ", "free ", " near me", " app"]
MAX_SEEDS = 5
KEEP_DAYS = 120
_ctx: dict = {}
_state = {"building": False, "apps": [], "apps_at": 0.0, "pruned": 0.0}
_hits: dict = {}

TRACKER = r"""(function(){try{
if(window.__ccMkt)return;window.__ccMkt=1;
var A="%s/api/mkt/t";
if(navigator.doNotTrack==="1"||window.doNotTrack==="1"||navigator.globalPrivacyControl)return;
var lastT="",lastAt=0;
function send(o){o.h=location.hostname;o.p=location.pathname;var b=JSON.stringify(o);
try{if(navigator.sendBeacon&&navigator.sendBeacon(A,new Blob([b],{type:"text/plain"})))return;}catch(e){}
try{fetch(A,{method:"POST",body:b,mode:"no-cors",keepalive:true,headers:{"Content-Type":"text/plain"}});}catch(e){}}
var u=new URLSearchParams(location.search),ref="",rq=null;
try{if(document.referrer){var r=new URL(document.referrer);if(r.hostname!==location.hostname){ref=r.hostname;rq=r.searchParams;}}}catch(e){}
var term=u.get("utm_term")||u.get("q")||u.get("search")||u.get("query")||(rq&&(rq.get("q")||rq.get("p")))||"";
send({k:"visit",r:u.get("utm_campaign")||u.get("utm_source")||u.get("ref")||ref,t:term});
var path=location.pathname;
setInterval(function(){if(location.pathname!==path){path=location.pathname;send({k:"visit",r:"",t:""});}},1500);
function isSearch(el){return el&&el.tagName==="INPUT"&&(el.type==="search"||/search|query|^q$/i.test((el.name||"")+" "+(el.id||"")+" "+(el.placeholder||"")+" "+(el.getAttribute("aria-label")||"")));}
function searched(v){v=(v||"").trim().slice(0,80);if(!v)return;var n=Date.now();if(v===lastT&&n-lastAt<3000)return;lastT=v;lastAt=n;send({k:"search",t:v});}
document.addEventListener("keydown",function(e){if(e.key==="Enter"&&isSearch(e.target))searched(e.target.value);},true);
document.addEventListener("submit",function(e){var f=e.target,i=f&&f.querySelectorAll?f.querySelectorAll("input"):[];for(var k=0;k<i.length;k++){if(isSearch(i[k])){searched(i[k].value);break;}}},true);
}catch(e){}})();""" % API_BASE

SNIPPET = f'<script src="{API_BASE}/api/mkt/t.js" defer></script>'

EMAIL_RE = re.compile(r"\S+@\S+")
DIGITS_RE = re.compile(r"\d[\d\s().-]{6,}\d")


def safe_term(t: str) -> str:
    """Search words only: drop anything that looks like an email address or phone/card number."""
    t = re.sub(r"\s+", " ", str(t or "")).strip().lower()[:80]
    if EMAIL_RE.search(t) or DIGITS_RE.search(t):
        return ""
    return clean(t, 80)


def host_of(s: str) -> str:
    s = re.sub(r"^https?://", "", str(s or "").strip().lower())
    return s.split("/")[0].split(":")[0][:100]


def split_list(s: str) -> list:
    return [x.strip() for x in str(s or "").split(",") if x.strip()]


def match_app(apps: list, host: str, path: str) -> str:
    """The app whose web address fits this page best (a listed path beats a plain host)."""
    best, best_len = "", -1
    host = host.lower()
    for a in apps:
        if not a.get("active", True):
            continue
        for entry in split_list(a.get("hosts")):
            entry = re.sub(r"^https?://", "", entry.lower())
            h, _, p = entry.partition("/")
            p = "/" + p if p else ""
            if h == host and path.lower().startswith(p) and len(p) > best_len:
                best, best_len = a["name"], len(p)
    return best


async def load_apps(force: bool = False) -> list:
    if force or time.time() - _state["apps_at"] > 300:
        _state["apps"] = await db("GET", "mkt_apps", params={"select": "*", "order": "name.asc"}) or []
        _state["apps_at"] = time.time()
    return _state["apps"]


async def get_setting(key: str, default=None):
    rows = await db("GET", "ecom_settings", params={"key": f"eq.{key}", "select": "value"}) or []
    return rows[0]["value"] if rows and rows[0].get("value") is not None else default


def week_label(now: datetime) -> str:
    y, w, _ = now.isocalendar()
    return f"{y}-W{w:02d}"


# ---------- public search suggestions ----------
async def suggestions_for(client: httpx.AsyncClient, q: str) -> list:
    out = []
    for url in (f"https://duckduckgo.com/ac/?q={quote(q)}&type=list&kl=us-en",
                f"https://api.bing.com/osjson.aspx?query={quote(q)}&market=en-US"):
        try:
            r = await client.get(url)
            data = r.json() if r.status_code == 200 else None
            if isinstance(data, list) and len(data) > 1 and isinstance(data[1], list):
                out += [str(x).strip().lower() for x in data[1] if str(x).strip()]
        except (httpx.HTTPError, ValueError):
            pass
        await asyncio.sleep(0.4)
    return out


async def public_terms(seeds: list) -> list:
    """Most-suggested search phrases around these keywords: [(phrase, times suggested)]."""
    count: Counter = Counter()
    headers = {"User-Agent": "Mozilla/5.0 (compatible; CashCowReport/1.0)"}
    async with httpx.AsyncClient(timeout=8, headers=headers, follow_redirects=True) as client:
        for seed in seeds[:MAX_SEEDS]:
            for m in MODIFIERS:
                q = (m + seed if not m.startswith(" ") else seed + m).strip()
                for s in await suggestions_for(client, q):
                    if s != seed.lower():
                        count[clean(s, 90)] += 1
    return count.most_common(15)


# ---------- SEO check: the page the way a search engine first sees it ----------
class _Page(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title, self.meta, self.links, self.h1, self.imgs, self.no_alt = "", {}, {}, [], 0, 0
        self.lang, self.text, self._in, self._skip = "", [], "", 0

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "html":
            self.lang = a.get("lang", "")
        elif tag == "meta":
            key = (a.get("name") or a.get("property") or "").lower()
            if key:
                self.meta[key] = a.get("content", "")
        elif tag == "link" and a.get("rel"):
            self.links[a["rel"].lower()] = a.get("href", "")
        elif tag == "img":
            self.imgs += 1
            self.no_alt += 0 if a.get("alt", "").strip() else 1
        elif tag in ("script", "style", "noscript", "template"):
            self._skip += 1
        if tag in ("title", "h1"):
            self._in = tag
            if tag == "h1":
                self.h1.append("")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript", "template"):
            self._skip = max(0, self._skip - 1)
        if tag == self._in:
            self._in = ""

    def handle_data(self, data):
        if self._in == "title":
            self.title += data
        elif self._in == "h1" and self.h1:
            self.h1[-1] += data
        if not self._skip and self._in != "title":
            self.text.append(data)


def page_url(app: dict) -> str:
    hosts = split_list(app.get("hosts"))
    if not hosts:
        return ""
    h = re.sub(r"^https?://", "", hosts[0])
    if "/" in h and "." not in h.split("/")[-1]:
        h += ".html"                       # cashcow-ecommerce.onrender.com/candylady -> candylady.html
    return "https://" + h


def seo_score(html: str, url: str, secs: float, keywords: list, robots: str, sitemap: bool) -> dict:
    p = _Page()
    try:
        p.feed(html)
    except Exception:
        pass
    title = re.sub(r"\s+", " ", p.title).strip()
    desc = re.sub(r"\s+", " ", p.meta.get("description", "")).strip()
    words = re.findall(r"[A-Za-z]{2,}", " ".join(p.text))
    body = " ".join(words).lower()
    h1 = [re.sub(r"\s+", " ", x).strip() for x in p.h1 if x.strip()]
    have = set(re.findall(r"[a-z]{3,}", (title + " " + desc + " " + body).lower()))
    found = [k for k in keywords if k and all(w in have or w.rstrip("s") in have or w + "s" in have
                                                for w in re.findall(r"[a-z]{3,}", k.lower()))]
    blocked = bool(re.search(r"(?im)^user-agent:\s*\*\s*$(?:\n(?!user-agent).*)*?\n\s*disallow:\s*/\s*$", robots or ""))
    checks = [
        (bool(title) and 30 <= len(title) <= 60, "Page title", f'"{title[:70]}" ({len(title)} letters)' if title else "missing",
         "Make the title 30 to 60 letters and start it with what the app does, e.g. \"Check Scam Texts Free | ScamShield\"."),
        (70 <= len(desc) <= 160, "Search description", f"{len(desc)} letters" if desc else "missing",
         "Add a 70 to 160 letter description in index.html: <meta name=\"description\" content=\"...\">. Google often shows it under your link."),
        (len(h1) == 1, "Main heading (H1)", f"{len(h1)} found",
         "Give the page exactly one main heading in its HTML that says what the app does."),
        (len(words) >= 150, "Words search engines can read", f"{len(words)} words",
         "This app builds its page in the browser, so search engines first see almost no text. Add a short paragraph about the app inside index.html."),
        (bool(found), "Your keywords on the page", ", ".join(found[:4]) if found else "none of them",
         "Use your top search phrases (listed above) in the title, description and first paragraph."),
        ("viewport" in p.meta, "Works on phones", "yes" if "viewport" in p.meta else "no viewport tag",
         "Add <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">."),
        (bool(p.links.get("canonical")), "Main address (canonical)", p.links.get("canonical", "missing")[:60],
         "Add <link rel=\"canonical\" href=\"your main web address\"> so Google doesn't split your ranking across addresses."),
        (bool(p.meta.get("og:title") and p.meta.get("og:image")), "Share preview (Facebook, texts)", "set" if p.meta.get("og:image") else "missing",
         "Add og:title, og:description and og:image tags so shared links show a picture and title."),
        (p.no_alt == 0, "Picture descriptions (alt text)", f"{p.no_alt} of {p.imgs} missing" if p.imgs else "no pictures in the HTML",
         "Describe each picture with alt=\"...\". It helps Google Images and people using screen readers."),
        (bool(p.lang), "Language set", p.lang or "missing", "Add lang=\"en\" to the <html> tag."),
        ("noindex" not in p.meta.get("robots", "").lower() and not blocked, "Allowed in search", "yes" if not blocked else "blocked",
         "Something is telling search engines to skip this page. Remove noindex / Disallow: /."),
        (sitemap, "Sitemap", "found" if sitemap else "missing",
         "Add a sitemap.xml listing your pages, then submit it in Bing Webmaster Tools."),
        (url.startswith("https://"), "Secure (https)", "yes" if url.startswith("https://") else "no", "Serve the page over https."),
        (secs < 2.0, "Speed", f"{secs:.1f} seconds",
         "The page took a while to answer. On Render's free plan a sleeping app wakes slowly; a paid plan or a ping keeps it fast."),
    ]
    passed = sum(1 for c in checks if c[0])
    return {"url": url, "score": round(100 * passed / len(checks)),
            "checks": [{"ok": ok, "label": label, "found": found_txt, "tip": "" if ok else tip} for ok, label, found_txt, tip in checks]}


async def seo_check(app: dict, keywords: list) -> dict:
    url = page_url(app)
    if not url:
        return {}
    headers = {"User-Agent": "Mozilla/5.0 (compatible; CashCowSEO/1.0)"}
    try:
        async with httpx.AsyncClient(timeout=20, headers=headers, follow_redirects=True) as client:
            t0 = time.time()
            r = await client.get(url)
            secs = time.time() - t0
            if r.status_code >= 400:
                return {"url": url, "error": f"The page answered with error {r.status_code}."}
            root = f"{r.url.scheme}://{r.url.host}"
            robots = ""
            try:
                rr = await client.get(urljoin(root, "/robots.txt"))
                robots = rr.text if rr.status_code == 200 and "html" not in rr.headers.get("content-type", "") else ""
            except httpx.HTTPError:
                pass
            sitemap = "sitemap:" in robots.lower()
            if not sitemap:
                try:
                    sm = await client.get(urljoin(root, "/sitemap.xml"))
                    sitemap = sm.status_code == 200 and "<urlset" in sm.text[:2000].lower()
                except httpx.HTTPError:
                    pass
            return seo_score(r.text[:500000], str(r.url), secs, keywords, robots, sitemap)
    except httpx.HTTPError:
        return {"url": url, "error": "Couldn't open the page. It may be asleep; it's checked again next week."}
    except Exception as e:                 # one odd page must never stop the whole report
        _ctx.get("logger") and _ctx["logger"].error("seo check %s: %s", url, e)
        return {"url": url, "error": "Couldn't check this page this week."}


# ---------- the weekly report ----------
def summarize(events: list, since: str) -> dict:
    now_ev = [e for e in events if e["at"] >= since]
    old_ev = [e for e in events if e["at"] < since]
    visits = [e for e in now_ev if e["kind"] == "visit"]
    terms = Counter(e["term"] for e in now_ev if e.get("term"))
    return {
        "visits": len(visits),
        "visits_before": sum(1 for e in old_ev if e["kind"] == "visit"),
        "searches": sum(1 for e in now_ev if e["kind"] == "search"),
        "pages": Counter(e["path"] or "/" for e in visits).most_common(5),
        "sources": Counter(e["source"] or "Direct or bookmark" for e in visits).most_common(5),
        "terms": terms.most_common(10),
    }


async def build_report() -> dict:
    if _state["building"]:
        return {"started": False}
    _state["building"] = True
    try:
        apps = [a for a in await load_apps(force=True) if a.get("active", True)]
        seen = await get_setting("_mkt_seen", {}) or {}
        now = datetime.now(timezone.utc)
        since = (now - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
        start = (now - timedelta(days=14)).strftime("%Y-%m-%dT%H:%M:%SZ")
        out = []
        for a in apps:
            events = await db("GET", "mkt_events", params={
                "select": "at,kind,path,source,term", "app": f"eq.{a['name']}", "at": f"gte.{start}", "limit": "20000"}) or []
            row = {"app": a["name"], **summarize(events, since)}
            pub = await public_terms(split_list(a.get("seeds")))
            before = set(seen.get(a["name"]) or [])
            row["public"] = [{"term": t, "count": c, "new": bool(before) and t not in before} for t, c in pub]
            seen[a["name"]] = sorted(set(before) | {t for t, _ in pub})[-300:]
            row["seo"] = await seo_check(a, split_list(a.get("seeds")) + [t for t, _ in pub[:5]])
            out.append(row)
        report = {"week": week_label(datetime.now(ecom.TZ)), "built_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "apps": out}
        await save_setting("_mkt_report", report)
        await save_setting("_mkt_seen", seen)
        return {"started": True, "report": report}
    finally:
        _state["building"] = False


async def weekly_if_due(st: dict) -> bool:
    """Called by the dashboard's 5-minute loop: build the report Monday morning, once a week."""
    if not st.get("mkt_weekly", True) or _state["building"]:
        return False
    now = datetime.now(ecom.TZ)
    if time.time() - _state["pruned"] > 86400:
        _state["pruned"] = time.time()
        cutoff = (datetime.now(timezone.utc) - timedelta(days=KEEP_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
        try:
            await db("DELETE", "mkt_events", params={"at": f"lt.{cutoff}"})
        except HTTPException:
            pass
    if now.weekday() != 0 or now.hour < 6:
        return False
    if await get_setting("_mkt_week", "") == week_label(now):
        return False
    await save_setting("_mkt_week", week_label(now))
    asyncio.create_task(build_report())
    return True


# ---------- endpoints ----------
class AppIn(BaseModel):
    id: str = ""
    name: str
    hosts: str = ""
    seeds: str = ""
    active: bool = True


def register(app, *, admin, logger):
    _ctx["logger"] = logger

    @app.get("/api/mkt/t.js")
    async def mkt_tracker():
        return Response(TRACKER, media_type="application/javascript",
                        headers={"Cache-Control": "public, max-age=3600"})

    @app.post("/api/mkt/t")
    async def mkt_event(request: Request):
        ip = request.client.host if request.client else "?"          # kept in memory only, for the rate limit
        now = time.time()
        hits = [t for t in _hits.get(ip, []) if now - t < 60]
        if len(hits) >= 30:
            return Response(status_code=204)
        _hits[ip] = hits + [now]
        if len(_hits) > 5000:
            _hits.clear()
        try:
            raw = (await request.body())[:2000]
            data = json.loads(raw.decode("utf-8", "ignore") or "{}")
        except ValueError:
            return Response(status_code=204)
        if not isinstance(data, dict):
            return Response(status_code=204)
        kind = "search" if data.get("k") == "search" else "visit"
        host = host_of(data.get("h"))
        path = clean(str(data.get("p") or "/").split("?")[0], 120)
        try:
            name = match_app(await load_apps(), host, path)
        except HTTPException:
            return Response(status_code=204)
        if not name:
            return Response(status_code=204)
        source = clean(host_of(data.get("r")) if "." in str(data.get("r") or "") else data.get("r"), 60)
        if host_of(source) == host:
            source = ""
        term = safe_term(data.get("t"))
        if kind == "search" and not term:
            return Response(status_code=204)
        try:
            await db("POST", "mkt_events", body={"app": name, "kind": kind, "path": path, "source": source, "term": term},
                     prefer="return=minimal")
        except HTTPException:
            pass
        return Response(status_code=204)

    @app.get("/api/ecom/marketing")
    async def mkt_overview(request: Request):
        await admin(request)
        st = await ecom.load_settings()
        return {"apps": await load_apps(force=True), "report": await get_setting("_mkt_report", None),
                "building": _state["building"], "snippet": SNIPPET, "weekly": st.get("mkt_weekly", True),
                "ads_ready": False}

    @app.post("/api/ecom/marketing/run")
    async def mkt_run(request: Request):
        await admin(request)
        if _state["building"]:
            return {"ok": True, "message": "Already building. Check back in a few minutes."}
        asyncio.create_task(build_report())
        return {"ok": True, "message": "Building the report. It takes a few minutes; tap Refresh to see it."}

    @app.post("/api/ecom/marketing/apps")
    async def mkt_save_app(payload: AppIn, request: Request):
        await admin(request)
        name = clean(payload.name, 60)
        if not name:
            raise HTTPException(status_code=400, detail="Give the app a name.")
        hosts = ", ".join(re.sub(r"^https?://", "", h.lower()).rstrip("/") for h in split_list(clean(payload.hosts, 500)))
        seeds = ", ".join(split_list(clean(payload.seeds, 300))[:MAX_SEEDS])
        row = {"name": name, "hosts": hosts, "seeds": seeds, "active": payload.active}
        if payload.id:
            await db("PATCH", "mkt_apps", params={"id": f"eq.{payload.id}"}, body=row, prefer="return=minimal")
        else:
            await db("POST", "mkt_apps", body=row, prefer="return=minimal")
        await load_apps(force=True)
        return {"ok": True}

    @app.post("/api/ecom/marketing/apps/{aid}/delete")
    async def mkt_delete_app(aid: str, request: Request):
        await admin(request)
        await db("DELETE", "mkt_apps", params={"id": f"eq.{clean(aid, 40)}"})
        await load_apps(force=True)
        return {"ok": True}
