"""CashCow Mail: email campaigns sent through Amazon SES, with two built-in scanners.

- Campaign scanner: checks every campaign before it can be sent (spam words, risky or
  look-alike links, broken links, misleading subjects, health wording, required pieces).
- Inbox scanner: checks replies like ScamShield does. Scams and spam are quarantined,
  unsubscribe requests are handled automatically, leads and replies are forwarded to you
  with a safety banner.

Everything is stored in Supabase (cashcow_mail.sql). main.py calls register(app, ...).

Render settings (Environment):
  AWS_SES_REGION            e.g. us-east-1
  AWS_SES_ACCESS_KEY_ID     from an AWS user that may only send email
  AWS_SES_SECRET_ACCESS_KEY
  MAIL_FROM                 e.g. CashCow <news@cashcowai.online>
  MAIL_REPLY_TO             optional, e.g. replies@mail.cashcowai.online (scanned inbox)
  MAIL_POSTAL_ADDRESS       your mailing address; the law requires it in every campaign
  MAIL_ADMIN_EMAILS         the CashCow account email(s) allowed into the dashboard
  MAIL_WEBHOOK_KEY          16+ random characters; part of the web address Amazon calls
  MAIL_FORWARD_TO           where scanned replies and leads are forwarded (your Gmail)
  MAIL_SEND_RATE            emails per second (default 10; your SES limit is in the AWS console)
  MAIL_GUARD_ADDRESS        ScamShield Email Guard address, e.g. check@mail.cashcowai.online
"""

import asyncio
import base64
import datetime as _dt
import hashlib
import hmac
import html
import ipaddress
import json
import os
import re
import time
from typing import Optional
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import formataddr, parseaddr
from urllib.parse import quote, urlsplit

import httpx
from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

# ---------------------------------------------------------------- settings

SES_REGION = (os.environ.get("AWS_SES_REGION") or "us-east-1").strip()
SES_KEY_ID = (os.environ.get("AWS_SES_ACCESS_KEY_ID") or "").strip()
SES_SECRET = (os.environ.get("AWS_SES_SECRET_ACCESS_KEY") or "").strip()
MAIL_FROM = (os.environ.get("MAIL_FROM") or "").strip()
MAIL_REPLY_TO = (os.environ.get("MAIL_REPLY_TO") or "").strip()
MAIL_POSTAL_ADDRESS = (os.environ.get("MAIL_POSTAL_ADDRESS") or "").strip()
MAIL_ADMIN_EMAILS = {e.strip().lower() for e in (os.environ.get("MAIL_ADMIN_EMAILS") or "").split(",") if e.strip()}
MAIL_WEBHOOK_KEY = (os.environ.get("MAIL_WEBHOOK_KEY") or "").strip()
MAIL_FORWARD_TO = (os.environ.get("MAIL_FORWARD_TO") or "").strip()
MAIL_SNS_TOPICS = {t.strip() for t in (os.environ.get("MAIL_SNS_TOPIC_ARNS") or "").split(",") if t.strip()}
MAIL_GUARD_ADDRESS = (os.environ.get("MAIL_GUARD_ADDRESS") or "").strip().lower()
GUARD_APP_URL = (os.environ.get("GUARD_APP_URL") or "https://scamshield-email-guard.onrender.com").rstrip("/")
GUARD_DAILY_LIMIT = 300   # checks per person per day (Premium)
GUARD_FREE_MONTHLY = 3    # free ScamShield accounts: forwarded checks per month
try:
    MAIL_SEND_RATE = max(1.0, min(50.0, float(os.environ.get("MAIL_SEND_RATE") or 10)))
except ValueError:
    MAIL_SEND_RATE = 10.0

SITE_URL = "https://cashcowai.online"
APP_TAGS = {
    "scamshield": "ScamShield", "billpilot": "MyBillPilot", "healthscan": "My Health Check Scan",
    "remedies": "My Home Kitchen Remedies", "letters": "My Letter Helper", "recall": "My Recall Check",
    "caregiver": "My Caregiver Circle", "fixit": "My Fix-It Helper", "shopping": "My Shopping Helper",
    "community": "CashCow Community", "allaccess": "All Access news",
}
MERGE_TAGS = {"first_name", "name", "email"}

_ctx = {}  # filled by register(): account_user, auth_headers, supabase_url, check_rate_limit, logger, public_api


def ses_ready() -> bool:
    return bool(SES_KEY_ID and SES_SECRET and MAIL_FROM)


def _log():
    return _ctx["logger"]


# ---------------------------------------------------------------- Amazon request signing (SigV4)

def _hmac(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode(), hashlib.sha256).digest()


def sigv4_headers(method: str, url: str, body: bytes, region: str, service: str,
                  key_id: str, secret: str, amz_date: str = None, extra: dict = None) -> dict:
    """Signs a request the way AWS requires (Signature Version 4). Returns the headers to send."""
    parts = urlsplit(url)
    amz_date = amz_date or _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    day = amz_date[:8]
    headers = {"host": parts.netloc, "x-amz-date": amz_date}
    for k, v in (extra or {}).items():
        headers[k.lower()] = str(v).strip()
    signed = ";".join(sorted(headers))
    canon_headers = "".join(f"{k}:{headers[k]}\n" for k in sorted(headers))
    query = "&".join(sorted(
        f"{quote(k, safe='-_.~')}={quote(v, safe='-_.~')}"
        for k, _, v in (p.partition("=") for p in parts.query.split("&") if p)))
    canonical = "\n".join([method, quote(parts.path or "/", safe="/-_.~"), query, canon_headers, signed,
                           hashlib.sha256(body).hexdigest()])
    scope = f"{day}/{region}/{service}/aws4_request"
    to_sign = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(canonical.encode()).hexdigest()])
    k = _hmac(_hmac(_hmac(_hmac(("AWS4" + secret).encode(), day), region), service), "aws4_request")
    sig = hmac.new(k, to_sign.encode(), hashlib.sha256).hexdigest()
    out = {k2: v for k2, v in headers.items() if k2 != "host"}
    out["Authorization"] = f"AWS4-HMAC-SHA256 Credential={key_id}/{scope}, SignedHeaders={signed}, Signature={sig}"
    return out


class SESError(Exception):
    def __init__(self, message: str, retry: bool = False):
        super().__init__(message)
        self.retry = retry


async def ses_send_raw(raw: bytes, to_addr: str) -> str:
    """Sends one finished email through Amazon SES. Returns the SES message id."""
    if not ses_ready():
        raise SESError("Amazon SES isn't connected yet.")
    url = f"https://email.{SES_REGION}.amazonaws.com/v2/email/outbound-emails"
    body = json.dumps({
        "FromEmailAddress": MAIL_FROM,
        "Destination": {"ToAddresses": [to_addr]},
        "Content": {"Raw": {"Data": base64.b64encode(raw).decode()}},
    }).encode()
    for attempt in range(4):
        headers = sigv4_headers("POST", url, body, SES_REGION, "ses", SES_KEY_ID, SES_SECRET,
                                extra={"content-type": "application/json"})
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(url, content=body, headers=headers)
        except httpx.HTTPError as e:
            if attempt == 3:
                raise SESError(f"Couldn't reach Amazon SES ({type(e).__name__}).", retry=True)
            await asyncio.sleep(2 ** attempt)
            continue
        if resp.status_code == 200:
            try:
                return resp.json().get("MessageId", "")
            except ValueError:
                return ""
        try:
            err = resp.json()
            msg = err.get("message") or err.get("Message") or resp.text[:200]
        except ValueError:
            msg = resp.text[:200]
        if resp.status_code in (429, 500, 502, 503) and attempt < 3:
            await asyncio.sleep(2 ** attempt)
            continue
        raise SESError(f"Amazon SES said no ({resp.status_code}): {msg}")
    raise SESError("Amazon SES is busy. Try again shortly.", retry=True)


# ---------------------------------------------------------------- writing the email

_LINK_MD = re.compile(r"\[([^\]\n]{1,200})\]\((\S{1,2000}?)\)")
_BUTTON = re.compile(r"^\[\[([^|\]\n]{1,80})\|(\S{1,2000})\]\]$")
_BARE_URL = re.compile(r"(?<![\"'=(\[|])\b(https?://[^\s<>()\"'\[\]|]{2,2000})")
_MERGE = re.compile(r"\{\{\s*([a-z_]+)\s*\}\}")


def _safe_href(url: str) -> str:
    url = url.strip()
    return url if re.match(r"^(https?://|mailto:)", url, re.I) else ""


def _merge(text: str, sub: dict) -> str:
    name = (sub.get("name") or "").strip()
    values = {"name": name or "there", "first_name": (name.split()[0] if name else "there"), "email": sub.get("email", "")}
    return _MERGE.sub(lambda m: values.get(m.group(1), m.group(0)), text)


def _inline_html(text: str) -> str:
    out, last = [], 0
    for m in _LINK_MD.finditer(text):
        out.append(_inline_plain_html(text[last:m.start()]))
        href = _safe_href(m.group(2))
        label = _inline_plain_html(m.group(1))
        out.append(f'<a href="{html.escape(href)}" style="color:#3C3489">{label}</a>' if href else label)
        last = m.end()
    out.append(_inline_plain_html(text[last:]))
    return "".join(out)


def _inline_plain_html(text: str) -> str:
    t = html.escape(text)
    t = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", t)
    t = re.sub(r"(?<!\*)\*(?!\s)(.+?)(?<!\s)\*(?!\*)", r"<em>\1</em>", t)
    t = _BARE_URL.sub(lambda m: f'<a href="{m.group(1)}" style="color:#3C3489">{m.group(1)}</a>', t)
    return t


def render_body(body: str):
    """Turns the simple writing format into email HTML and a plain-text copy.
    # Heading   ## Smaller heading   - bullet   --- divider
    **bold**   *italic*   [link text](https://...)   [[Button text|https://...]]"""
    html_parts, text_parts, para, bullets = [], [], [], []

    def flush():
        if para:
            joined = " ".join(para)
            html_parts.append(f'<p style="margin:0 0 16px;line-height:1.6">{_inline_html(joined)}</p>')
            text_parts.append(_plain(joined))
            para.clear()
        if bullets:
            items = "".join(f'<li style="margin:0 0 6px">{_inline_html(b)}</li>' for b in bullets)
            html_parts.append(f'<ul style="margin:0 0 16px;padding-left:22px;line-height:1.6">{items}</ul>')
            text_parts.append("\n".join(f"- {_plain(b)}" for b in bullets))
            bullets.clear()

    for raw_line in body.replace("\r\n", "\n").split("\n"):
        line = raw_line.strip()
        if not line:
            flush()
            continue
        m = _BUTTON.match(line)
        if m:
            flush()
            href = _safe_href(m.group(2))
            label = html.escape(m.group(1))
            if href:
                html_parts.append(
                    f'<p style="margin:8px 0 20px"><a href="{html.escape(href)}" style="display:inline-block;'
                    f'background:#3C3489;color:#ffffff;text-decoration:none;font-weight:bold;padding:12px 22px;'
                    f'border-radius:10px">{label}</a></p>')
                text_parts.append(f"{m.group(1)}: {href}")
            continue
        if line.startswith("## "):
            flush()
            html_parts.append(f'<h2 style="font-size:19px;margin:20px 0 10px">{_inline_html(line[3:])}</h2>')
            text_parts.append(_plain(line[3:]).upper())
        elif line.startswith("# "):
            flush()
            html_parts.append(f'<h1 style="font-size:24px;margin:0 0 14px">{_inline_html(line[2:])}</h1>')
            text_parts.append(_plain(line[2:]).upper())
        elif line in ("---", "***"):
            flush()
            html_parts.append('<hr style="border:0;border-top:1px solid #e5e2da;margin:20px 0">')
            text_parts.append("----------")
        elif line.startswith(("- ", "* ")):
            if para:
                flush()
            bullets.append(line[2:])
        else:
            if bullets:
                flush()
            para.append(line)
    flush()
    return "\n".join(html_parts), "\n\n".join(text_parts)


def _plain(text: str) -> str:
    t = _LINK_MD.sub(lambda m: f"{m.group(1)} ({m.group(2)})", text)
    t = re.sub(r"\*\*(.+?)\*\*", r"\1", t)
    return re.sub(r"(?<!\*)\*(?!\s)(.+?)(?<!\s)\*(?!\*)", r"\1", t)


def unsubscribe_url(token: str) -> str:
    return f"{_ctx.get('public_api', '')}/api/mail/unsubscribe?t={token}"


def build_email(campaign: dict, sub: dict, test: bool = False) -> bytes:
    """Builds the finished email for one person, with the unsubscribe link and address."""
    subject = _merge(campaign.get("subject") or "", sub)
    preheader = _merge(campaign.get("preheader") or "", sub)
    body_html, body_text = render_body(_merge(campaign.get("body") or "", sub))
    unsub = unsubscribe_url(sub.get("token") or "test")
    address = html.escape(MAIL_POSTAL_ADDRESS or "(mailing address)")
    pre = (f'<div style="display:none;max-height:0;overflow:hidden;opacity:0">{html.escape(preheader)}'
           f'{"&nbsp;&zwnj;" * 40}</div>') if preheader else ""
    page = f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(subject)}</title></head>
<body style="margin:0;padding:0;background:#f4f2ec">{pre}
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f4f2ec"><tr><td align="center" style="padding:20px 12px">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:600px;background:#ffffff;border-radius:14px;font-family:Arial,Helvetica,sans-serif;font-size:16px;color:#1b1b1b">
<tr><td style="background:#3C3489;border-radius:14px 14px 0 0;padding:16px 24px;color:#ffffff;font-size:20px;font-weight:bold">Cash<span style="color:#D4A94D">Cow</span></td></tr>
<tr><td style="padding:24px">{body_html}</td></tr>
<tr><td style="padding:16px 24px 24px;border-top:1px solid #eeeae0;font-size:12px;line-height:1.6;color:#6b6b6b">
You're getting this because you signed up for CashCow emails at cashcowai.online.<br>
<a href="{html.escape(unsub)}" style="color:#6b6b6b">Unsubscribe</a> &middot; CashCow, {address}
</td></tr></table></td></tr></table></body></html>"""
    text = (f"{body_text}\n\n--\nYou're getting this because you signed up for CashCow emails at cashcowai.online.\n"
            f"Unsubscribe: {unsub}\nCashCow, {MAIL_POSTAL_ADDRESS or '(mailing address)'}\n")

    msg = EmailMessage()
    msg["From"] = MAIL_FROM or "CashCow <news@cashcowai.online>"
    msg["To"] = formataddr(((sub.get("name") or "").strip(), sub["email"]))
    msg["Subject"] = ("[TEST] " if test else "") + subject
    if MAIL_REPLY_TO:
        msg["Reply-To"] = MAIL_REPLY_TO
    msg["List-Unsubscribe"] = f"<{unsub}>"
    msg["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
    msg.set_content(text)
    msg.add_alternative(page, subtype="html")
    return msg.as_bytes(policy=policy.SMTP)


# ---------------------------------------------------------------- the scanners

SPAM_PHRASES = [
    "act now", "100% free", "risk-free", "risk free", "guaranteed", "you're a winner", "you are a winner",
    "you've been selected", "cash bonus", "double your", "earn \\$", "make money fast", "no cost",
    "limited time only", "click here", "buy now", "order now", "urgent", "congratulations", "free gift",
    "\\$\\$\\$", "no credit check", "once in a lifetime", "miracle", "lowest price", "eliminate debt",
    "work from home", "100% satisfied", "while supplies last", "this isn't spam", "not spam",
]
SPAM_RE = re.compile(r"(?<![a-z])(" + "|".join(SPAM_PHRASES) + r")(?![a-z])", re.I)
HEALTH_RE = re.compile(r"\b(cures?|cured|treats?|treatment for|prevents?|heals?|fda[- ]approved|doctor[- ]recommended|"
                       r"guaranteed results|lose \d+ (pounds|lbs))\b", re.I)
SCAM_PHRASES = [
    "gift card", "wire transfer", "western union", "moneygram", "bitcoin", "crypto", "zelle", "cash app",
    "verify your account", "confirm your password", "account (has been )?suspended", "account (will be )?(locked|closed)",
    "unusual (sign-in|login) activity", "update your (billing|payment)", "login details", "social security number",
    "you have won", "inheritance", "lottery", "act immediately", "within 24 hours", "final notice",
    "invoice attached", "payment overdue", "refund (is )?pending", "send (me )?your (bank|card)",
]
SCAM_RE = re.compile(r"\b(" + "|".join(SCAM_PHRASES) + r")\b", re.I)
SHORTENERS = {"bit.ly", "tinyurl.com", "t.co", "goo.gl", "ow.ly", "is.gd", "buff.ly", "cutt.ly", "rebrand.ly",
              "shorturl.at", "rb.gy", "tiny.cc", "t.ly", "s.id", "lnkd.in"}
BRANDS = ["paypal", "amazon", "apple", "google", "microsoft", "walmart", "netflix", "chase", "wellsfargo",
          "bankofamerica", "facebook", "instagram", "usps", "fedex", "ups", "irs", "venmo", "cashapp",
          "coinbase", "target", "costco", "cashcowai"]
DANGEROUS_EXT = re.compile(r"\.(exe|scr|js|vbs|bat|cmd|com|jar|msi|ps1|hta|iso|img|lnk|html?|svg)$", re.I)
MACRO_EXT = re.compile(r"\.(docm|xlsm|pptm|zip|rar|7z)$", re.I)
_URL_IN_TEXT = re.compile(r"https?://[^\s<>()\"'\[\]]+", re.I)


def _edit_distance(a: str, b: str) -> int:
    if abs(len(a) - len(b)) > 2:
        return 3
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def _registered(host: str) -> str:
    parts = host.lower().strip(".").split(".")
    if len(parts) >= 3 and parts[-2] in ("co", "com", "org", "net", "gov") and len(parts[-1]) == 2:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def lookalike_brand(host: str):
    """Returns the brand a domain is pretending to be, e.g. paypa1.com -> paypal."""
    reg = _registered(host)
    label = reg.split(".")[0].replace("-", "")
    if label in BRANDS:
        return None
    # Letter tricks scammers use: 0 for o, 1 for l, rn for m, vv for w, i for l.
    swapped = label.translate(str.maketrans("0135", "oles"))
    tricks = {swapped, swapped.replace("rn", "m").replace("vv", "w")}
    tricks_i = {t.replace("i", "l") for t in tricks}
    security = re.search(r"(secure|login|verify|account|support|update|billing|signin|wallet)", label)
    for brand in BRANDS:
        if brand in tricks or (len(brand) >= 6 and _edit_distance(label, brand) == 1):
            return brand
        # brand name buried in a longer domain, like paypal-secure-login.com
        if len(brand) >= 5 and security and any(brand in t for t in tricks | tricks_i | {label}):
            return brand
    return None


def link_problems(url: str, text: str = ""):
    """ScamShield-style link checks. Returns (blockers, warnings) as lists of plain-English strings."""
    blockers, warnings = [], []
    if re.match(r"^\s*(javascript|data|vbscript):", url, re.I):
        return [f"A link uses \"{url.split(':')[0]}:\", which email programs treat as an attack."], []
    if not re.match(r"^(https?://|mailto:)", url, re.I):
        return [], [f"\"{url[:60]}\" isn't a full web address (it should start with https://)."]
    if url.lower().startswith("mailto:"):
        return [], []
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ["A link's web address is broken or disguised."], []
    if not host:
        return ["A link has no website in it."], []
    try:
        ipaddress.ip_address(host)
        blockers.append(f"A link goes to a bare number address ({host}) instead of a website name. That's a classic phishing sign.")
    except ValueError:
        pass
    if host.startswith("xn--") or ".xn--" in host:
        blockers.append(f"A link uses a disguised international domain ({host}) that can imitate another site.")
    brand = lookalike_brand(host)
    if brand:
        blockers.append(f"A link goes to {host}, which looks like an imitation of {brand.title()}.")
    if _registered(host) in SHORTENERS or host in SHORTENERS:
        warnings.append(f"A link uses the shortener {host}. Inboxes treat short links as a spam sign; use the full address.")
    if url.lower().startswith("http://"):
        warnings.append(f"A link to {host} isn't secure (http). Use https:// if the site supports it.")
    shown = (text or "").strip().lower()
    m = re.search(r"((?:[a-z0-9-]+\.)+[a-z]{2,})", shown)
    if m and ("." in shown) and len(shown) < 120:
        shown_host = m.group(1).removeprefix("www.")
        if _registered(shown_host) != _registered(host):
            blockers.append(f"A link shows \"{m.group(1)}\" but really goes to {host}. That's exactly what phishing emails do.")
    return blockers, warnings


def find_links(body: str):
    links, seen = [], set()
    for line in body.split("\n"):
        b = _BUTTON.match(line.strip())
        if b:
            links.append((b.group(2), b.group(1)))
    for m in _LINK_MD.finditer(body):
        links.append((m.group(2), m.group(1)))
    stripped = _LINK_MD.sub(" ", re.sub(r"\[\[[^\]\n]*\]\]", " ", body))
    for m in _BARE_URL.finditer(stripped):
        links.append((m.group(1), ""))
    out = []
    for url, text in links:
        key = (url, text)
        if key not in seen:
            seen.add(key)
            out.append({"url": url, "text": text})
    return out


async def _check_live(urls):
    """Makes sure each link actually loads. Returns {url: problem or None}."""
    sem = asyncio.Semaphore(6)
    results = {}

    async def one(u):
        async with sem:
            try:
                async with httpx.AsyncClient(timeout=8, follow_redirects=True,
                                             headers={"User-Agent": "CashCow link checker"}) as client:
                    r = await client.head(u)
                    if r.status_code in (403, 405, 501) or r.status_code >= 500:
                        r = await client.get(u)
                results[u] = None if r.status_code < 400 else f"returned error {r.status_code}"
            except httpx.HTTPError:
                results[u] = "didn't load"

    await asyncio.gather(*(one(u) for u in urls[:25]))
    return results


async def scan_campaign(c: dict, check_live: bool = True) -> dict:
    blockers, warnings, passes = [], [], []
    subject = (c.get("subject") or "").strip()
    pre = (c.get("preheader") or "").strip()
    body = c.get("body") or ""
    words = re.findall(r"[A-Za-z']+", body)

    # Setup
    if not ses_ready():
        blockers.append("Amazon SES isn't connected yet, so nothing can be sent.")
    if not MAIL_POSTAL_ADDRESS:
        blockers.append("Your mailing address isn't set (MAIL_POSTAL_ADDRESS on Render). U.S. law (CAN-SPAM) requires it in every campaign.")
    else:
        passes.append("Unsubscribe link and mailing address are added to every email automatically.")

    # Subject line
    if not subject:
        blockers.append("Add a subject line.")
    else:
        if re.match(r"^\s*(re|fw|fwd)\s*:", subject, re.I):
            blockers.append("The subject starts with RE: or FW:, pretending to be a reply. That's misleading and against CAN-SPAM.")
        if len(subject) > 78:
            warnings.append(f"The subject is {len(subject)} characters; phones cut it off around 40–60.")
        letters = re.sub(r"[^A-Za-z]", "", subject)
        if len(letters) >= 8 and sum(ch.isupper() for ch in letters) / len(letters) > 0.6:
            warnings.append("The subject is mostly CAPITAL LETTERS, a strong spam sign.")
        if subject.count("!") > 1:
            warnings.append("The subject has more than one exclamation point.")
        if not any(w for w in warnings if "subject" in w.lower()):
            passes.append("Subject line looks clean.")
    if not pre:
        warnings.append("Add preview text (the gray line after the subject in the inbox). Emails without it get opened less.")

    # Wording
    found_spam = sorted({m.group(0).lower() for m in SPAM_RE.finditer(subject + "\n" + body)})
    if found_spam:
        warnings.append("Spam-filter trigger words: " + ", ".join(f'"{w}"' for w in found_spam[:8]) + ".")
    else:
        passes.append("No common spam-trigger words.")
    found_health = sorted({m.group(0).lower() for m in HEALTH_RE.finditer(subject + "\n" + body)})
    if found_health:
        warnings.append("Health claims (" + ", ".join(f'"{w}"' for w in found_health[:6]) +
                        "). CashCow's rule: never say a product treats, cures or prevents anything.")
    if len(words) < 30:
        warnings.append("The email is very short. A few real sentences help it get past spam filters.")
    elif len(words) > 2500:
        warnings.append("The email is very long. Over 2,500 words often gets clipped by Gmail.")
    body_letters = re.sub(r"[^A-Za-z]", "", body)
    if len(body_letters) > 200 and sum(ch.isupper() for ch in body_letters) / len(body_letters) > 0.3:
        warnings.append("Lots of CAPITAL LETTERS in the email body.")
    if body.count("!") > 6:
        warnings.append("Lots of exclamation points.")
    unknown = sorted({m.group(1) for m in _MERGE.finditer(subject + pre + body)} - MERGE_TAGS)
    if unknown:
        blockers.append("Unknown name tags: " + ", ".join("{{" + u + "}}" for u in unknown) +
                        ". Use {{first_name}}, {{name}} or {{email}}.")

    # Links
    links = find_links(body)
    for ln in links:
        b, w = link_problems(ln["url"], ln["text"])
        blockers += b
        warnings += w
    if len(links) > 20:
        warnings.append(f"{len(links)} links. Inboxes trust emails with fewer links more.")
    live = {}
    web = sorted({ln["url"] for ln in links if re.match(r"^https?://", ln["url"], re.I)})
    if check_live and web:
        live = await _check_live(web)
        for u, problem in live.items():
            if problem:
                warnings.append(f"The link {u[:80]} {problem}.")
    if links and not any(b for b in blockers if "link" in b.lower()) and not any(w for w in warnings if "link" in w.lower()):
        passes.append(f"All {len(links)} link{'s' if len(links) != 1 else ''} look safe" + (" and load." if live else "."))

    blockers = list(dict.fromkeys(blockers))
    warnings = list(dict.fromkeys(warnings))
    score = max(0, 100 - 25 * len(blockers) - 6 * len(warnings))
    grade = "F" if blockers else "A" if score >= 90 else "B" if score >= 75 else "C" if score >= 60 else "D"
    return {"grade": grade, "score": score, "blockers": blockers, "warnings": warnings, "passes": passes,
            "links": [{"url": ln["url"], "text": ln["text"], "problem": live.get(ln["url"])} for ln in links],
            "scanned_at": _dt.datetime.now(_dt.timezone.utc).isoformat()}


_UNSUB_RE = re.compile(r"^\s*(unsubscribe|stop|remove me|take me off)\b|please (unsubscribe|remove me)|"
                       r"(remove|take) me (off|from) (your|the|this) (list|emails?)|stop (sending|emailing)", re.I)
_AUTO_SUBJ = re.compile(r"^(automatic reply|auto(matic)?[- ]?reply|out of (the )?office|away from|"
                        r"delivery status notification|undeliverable|mail delivery failed)", re.I)
_LEAD_RE = re.compile(r"\b(price|pricing|quote|how much|cost|interested|hire|build (me|us|an?)|rebrand|"
                      r"partner(ship)?|my (business|company|store|shop)|demo|white ?label|buy (the|an|your) app)\b", re.I)
_HTML_TAG = re.compile(r"<[^>]+>")
_ANCHOR = re.compile(r"<a\b[^>]*href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>", re.I | re.S)


def _new_text(text: str) -> str:
    """The part of a reply the person actually wrote (drops quoted earlier messages)."""
    keep = []
    for line in text.replace("\r\n", "\n").split("\n"):
        if line.startswith(">") or re.match(r"^On .+wrote:\s*$", line) or line.strip() == "-- ":
            break
        keep.append(line)
    return "\n".join(keep).strip()


def _scan_core(from_name: str, from_addr: str, reply_to: str, subject: str, text: str, html_body: str,
               filenames, receipt: dict, use_auth: bool = True):
    """The ScamShield checks. Returns (level, flags)."""
    flags, danger, suspicious = [], 0, 0

    def verdict(key):
        return ((receipt.get(key) or {}).get("status") or "").upper()

    if verdict("virusVerdict") == "FAIL":
        flags.append("Amazon's virus scan found a virus")
        danger += 2
    if use_auth:
        if verdict("spamVerdict") == "FAIL":
            flags.append("Amazon's spam filter marked it as spam")
            suspicious += 2
        if verdict("dmarcVerdict") == "FAIL" or (verdict("spfVerdict") == "FAIL" and verdict("dkimVerdict") == "FAIL"):
            flags.append("the sender's address may be faked (failed email authentication)")
            suspicious += 1

    from_domain = from_addr.rsplit("@", 1)[-1].lower() if "@" in from_addr else ""
    if reply_to and "@" in reply_to and _registered(reply_to.rsplit("@", 1)[-1]) != _registered(from_domain or "x.x"):
        flags.append(f"replies would go to a different address ({reply_to})")
        suspicious += 1
    name_l = (from_name or "").lower().replace(" ", "")
    for brand in BRANDS:
        if brand in name_l and brand not in from_domain.replace("-", ""):
            flags.append(f"the sender's name says \"{from_name}\" but the email comes from {from_domain or 'an unknown address'}")
            suspicious += 2
            break
    if from_domain and lookalike_brand(from_domain):
        flags.append(f"the sender's domain {from_domain} imitates {lookalike_brand(from_domain).title()}")
        danger += 1

    scam_hits = sorted({m.group(0).lower() for m in SCAM_RE.finditer(subject + "\n" + text)})
    if scam_hits:
        flags.append("scam wording: " + ", ".join(f'"{h}"' for h in scam_hits[:6]))
        suspicious += len(scam_hits)

    link_pairs = [(m.group(1), _HTML_TAG.sub("", m.group(2))) for m in _ANCHOR.finditer(html_body)]
    link_pairs += [(u, "") for u in _URL_IN_TEXT.findall(text)]
    for url, label in link_pairs[:60]:
        b, _w = link_problems(url, label)
        if b:
            flags.append(b[0][0].lower() + b[0][1:].rstrip("."))
            danger += 1

    for fname in filenames:
        if DANGEROUS_EXT.search(fname or ""):
            flags.append(f"dangerous attachment ({fname})")
            danger += 2
        elif MACRO_EXT.search(fname or ""):
            flags.append(f"risky attachment ({fname})")
            suspicious += 1

    if danger >= 1 or suspicious >= 4:
        level = "dangerous"
    elif suspicious >= 2:
        level = "suspicious"
    else:
        level = "safe"
    return level, list(dict.fromkeys(flags))[:12]


def _bodies(msg):
    text_part = msg.get_body(preferencelist=("plain",))
    html_part = msg.get_body(preferencelist=("html",))
    html_body = html_part.get_content() if html_part else ""
    text = text_part.get_content() if text_part else _HTML_TAG.sub(" ", html_body)
    return re.sub(r"[ \t]+", " ", text), html_body


def _filenames(msg):
    out = []
    for part in msg.iter_attachments():
        if part.get_content_type() != "message/rfc822":
            out.append(part.get_filename() or "")
    return out


def scan_incoming(raw: bytes, receipt: dict = None) -> dict:
    """ScamShield-style check of an email someone sent us. Returns the verdict, category and reasons."""
    receipt = receipt or {}
    msg = BytesParser(policy=policy.default).parsebytes(raw)
    from_name, from_addr = parseaddr(str(msg.get("From", "")))
    _, reply_to = parseaddr(str(msg.get("Reply-To", "")))
    subject = str(msg.get("Subject", ""))[:300]
    text, html_body = _bodies(msg)
    fresh = _new_text(text)
    level, flags = _scan_core(from_name, from_addr, reply_to, subject, fresh, html_body, _filenames(msg), receipt)
    spam_fail = ((receipt.get("spamVerdict") or {}).get("status") or "").upper() == "FAIL"

    auto_hdr = str(msg.get("Auto-Submitted", "no")).lower()
    if auto_hdr not in ("", "no") or _AUTO_SUBJ.search(subject) or msg.get("X-Autoreply") or msg.get("X-Autorespond"):
        category = "auto"
    elif level == "dangerous" or spam_fail:
        category = "spam"
    elif _UNSUB_RE.search(subject) or _UNSUB_RE.search(fresh[:400]):
        category = "unsubscribe"
    elif _LEAD_RE.search(subject + " " + fresh):
        category = "lead"
    else:
        category = "reply"

    return {"from_addr": from_addr.lower()[:254], "from_name": (from_name or "")[:120], "subject": subject,
            "snippet": re.sub(r"\s+", " ", fresh)[:1000], "body": fresh[:20000],
            "verdict": level, "category": category, "flags": flags}


# ---------------------------------------------------------------- ScamShield Email Guard: reading forwarded mail

_FWD_MARK = re.compile(r"^\s*(-{2,}\s*Forwarded message\s*-{2,}|Begin forwarded message:|-{3,}\s*Original Message\s*-{3,}|_{8,})\s*$",
                       re.I | re.M)
_FWD_FIELD = re.compile(r"^\s*\*?(From|Subject|Reply-To|Sent|Date|To|Cc)\s*:\*?\s*(.*)$", re.I)


def _addr_from_field(value: str):
    value = value.replace("[mailto:", "<").replace("]", ">")
    name, addr = parseaddr(value)
    if not addr or "@" not in addr:
        m = re.search(r"[\w.+'-]+@[\w-]+(\.[\w-]+)+", value)
        addr = m.group(0) if m else ""
        name = value.replace(addr, "").strip(" <>\"'") if addr else value.strip()
    return name.strip(" \"'"), addr.lower()


def unwrap_forward(msg):
    """Finds the original email inside a forwarded one. Returns a dict, or None if it isn't a forward."""
    for part in msg.iter_attachments():
        if part.get_content_type() == "message/rfc822":
            inner = part.get_content()
            text, html_body = _bodies(inner)
            from_name, from_addr = parseaddr(str(inner.get("From", "")))
            _, reply_to = parseaddr(str(inner.get("Reply-To", "")))
            return {"from_name": from_name, "from_addr": from_addr.lower(), "reply_to": reply_to,
                    "subject": str(inner.get("Subject", ""))[:300], "text": text, "html": html_body,
                    "files": _filenames(inner), "full_headers": True}
    text, html_body = _bodies(msg)
    m = _FWD_MARK.search(text)
    if not m:
        return None
    fields, body_lines, in_head = {}, [], True
    for line in text[m.end():].lstrip("\n").split("\n"):
        if in_head:
            f = _FWD_FIELD.match(line)
            if f:
                fields.setdefault(f.group(1).lower(), f.group(2).strip())
                continue
            if not line.strip() and fields:
                in_head = False
                continue
            if fields:
                in_head = False
        body_lines.append(line.lstrip("> "))
    if "from" not in fields:
        return None
    from_name, from_addr = _addr_from_field(fields.get("from", ""))
    reply_to = _addr_from_field(fields["reply-to"])[1] if fields.get("reply-to") else ""
    return {"from_name": from_name, "from_addr": from_addr, "reply_to": reply_to,
            "subject": fields.get("subject", "")[:300], "text": "\n".join(body_lines), "html": html_body,
            "files": _filenames(msg), "full_headers": False}


def guard_scan(raw: bytes, receipt: dict = None) -> dict:
    """Scans mail that arrived at the Email Guard address. Works out who sent it to us
    (the owner) and the original email it's about."""
    receipt = receipt or {}
    msg = BytesParser(policy=policy.default).parsebytes(raw)
    _, outer_from = parseaddr(str(msg.get("From", "")))
    outer_from = outer_from.lower()
    owners = []
    for hdr in ("X-Forwarded-For", "X-Forwarded-To"):
        for v in msg.get_all(hdr) or []:
            first = str(v).replace(",", " ").split()
            if first and "@" in first[0]:
                owners.append(first[0].strip("<>").lower())
    envelope = str(receipt.get("_envelope_from") or "").lower()
    if envelope and "@" in envelope:
        owners.append(envelope)
    owners.append(outer_from)
    inner = unwrap_forward(msg)
    if inner and inner["from_addr"] != outer_from:
        mode = "manual"
        level, flags = _scan_core(inner["from_name"], inner["from_addr"], inner["reply_to"], inner["subject"],
                                  inner["text"], inner["html"], inner["files"], receipt, use_auth=False)
        shown = inner
    else:
        # Auto-forwarded (or sent straight to us): the email itself is the original.
        mode = "auto" if owners[0] != outer_from else "manual"
        from_name, _ = parseaddr(str(msg.get("From", "")))
        _, reply_to = parseaddr(str(msg.get("Reply-To", "")))
        text, html_body = _bodies(msg)
        shown = {"from_name": from_name, "from_addr": outer_from, "subject": str(msg.get("Subject", ""))[:300]}
        level, flags = _scan_core(from_name, outer_from, reply_to, shown["subject"], text, html_body,
                                  _filenames(msg), receipt, use_auth=(mode == "auto"))
    return {"owners": list(dict.fromkeys(o for o in owners if "@" in o)), "mode": mode, "verdict": level, "flags": flags,
            "from_addr": (shown.get("from_addr") or "")[:254], "from_name": (shown.get("from_name") or "")[:120],
            "subject": (shown.get("subject") or "")[:300], "is_ours": bool(msg.get("X-CashCow-Guard")),
            "outer_from": outer_from, "outer_subject": str(msg.get("Subject", ""))[:300], "text": _bodies(msg)[0][:4000]}


# ---------------------------------------------------------------- database calls

MAIL_ERRORS = {
    "needs_scan": (409, "Scan this campaign and fix anything marked \"must fix\" before sending."),
    "no_audience": (409, "Nobody subscribed matches this audience yet."),
    "locked": (409, "This campaign has already been sent or is sending, so it can't be changed."),
    "not_found": (404, "Not found."),
    "bad_email": (400, "Please enter a valid email address."),
    "bad_rows": (400, "That list is too big. Add up to 5,000 people at a time."),
    "bad_status": (400, "That change isn't allowed."),
    "address_taken": (409, "That address is already set up on another CashCow account."),
    "too_many": (409, "You can add up to 3 email addresses."),
}


async def rpc(fn: str, args: dict):
    url = _ctx.get("supabase_url")
    if not url:
        raise HTTPException(status_code=503, detail="CashCow Mail isn't set up yet.")
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(f"{url}/rest/v1/rpc/{fn}", headers=_ctx["auth_headers"](), json=args)
    except httpx.HTTPError:
        raise HTTPException(status_code=502, detail="Couldn't reach the mail database. Please try again.")
    if resp.status_code >= 400:
        try:
            msg = str(resp.json().get("message", ""))
        except ValueError:
            msg = ""
        if msg in MAIL_ERRORS:
            code, text = MAIL_ERRORS[msg]
            raise HTTPException(status_code=code, detail=text)
        _log().error("mail rpc %s failed: %s %s", fn, resp.status_code, msg[:200])
        raise HTTPException(status_code=502, detail="Something went wrong. Please try again.")
    if not resp.content:
        return None
    try:
        return resp.json()
    except ValueError:
        return None


# ---------------------------------------------------------------- the sender

_running = set()
_last_tick = [0.0]


async def tick():
    """Starts any campaign that's due. Safe to call often."""
    _last_tick[0] = time.time()
    if not ses_ready():
        return []
    try:
        ids = await rpc("mail_claim_due", {}) or []
    except HTTPException:
        return []
    for cid in ids:
        if cid not in _running:
            _running.add(cid)
            asyncio.create_task(_run_campaign(cid))
    return ids


async def maybe_tick():
    if time.time() - _last_tick[0] > 30:
        await tick()


async def _run_campaign(cid: str):
    try:
        got = await rpc("mail_get_campaign", {"p_id": cid})
        campaign = (got or {}).get("campaign") or {}
        while True:
            batch = await rpc("mail_next_batch", {"p_campaign": cid, "p_limit": 50}) or []
            if not batch:
                break
            for person in batch:
                start = time.time()
                ok, message_id, error = True, "", None
                try:
                    message_id = await ses_send_raw(build_email(campaign, person), person["email"])
                except SESError as e:
                    ok, error = False, str(e)
                except Exception as e:  # never let one address stop the campaign
                    ok, error = False, f"{type(e).__name__}"
                await rpc("mail_mark_send", {"p_campaign": cid, "p_sub": person["id"], "p_ok": ok,
                                             "p_message_id": message_id, "p_error": error})
                await asyncio.sleep(max(0.0, 1 / MAIL_SEND_RATE - (time.time() - start)))
        await rpc("mail_finish", {"p_campaign": cid})
        _log().info("Campaign %s finished.", cid)
    except Exception as e:
        _log().error("Campaign %s stopped: %s: %s", cid, type(e).__name__, e)
    finally:
        _running.discard(cid)


async def _tick_loop():
    while True:
        await asyncio.sleep(60)
        try:
            await tick()
        except Exception as e:
            _log().error("mail tick failed: %s", e)


# ---------------------------------------------------------------- web pages people see

def _page(title: str, body: str, status: int = 200) -> HTMLResponse:
    return HTMLResponse(f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{html.escape(title)} · CashCow</title>
<style>body{{margin:0;background:#f4f2ec;font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;color:#1b1b1b}}
main{{max-width:480px;margin:40px auto;padding:28px 22px;background:#fff;border-radius:16px;text-align:center}}
h1{{font-size:1.4rem;margin:0 0 10px}}p{{line-height:1.55}}a{{color:#3C3489}}
button{{font:inherit;font-weight:700;background:#3C3489;color:#fff;border:0;border-radius:12px;padding:12px 22px;min-height:48px;cursor:pointer}}
.brand{{font-weight:800;font-size:1.2rem;margin-bottom:14px}}.brand span{{color:#D4A94D}}</style></head>
<body><main><div class="brand">Cash<span>Cow</span></div>{body}</main></body></html>""", status_code=status)


_TOKEN_RE = re.compile(r"^[0-9a-f]{36}$")


# ---------------------------------------------------------------- request bodies

class SubscribeRequest(BaseModel):
    email: str
    name: str = ""
    tags: list = []
    source: str = "site"
    hp: str = ""
    elapsed: int = 0


class SubscribersAdd(BaseModel):
    rows: list
    consent: bool = False
    source: str = "import"


class SubscriberUpdate(BaseModel):
    unsubscribe: bool = False
    tags: Optional[list] = None


class CampaignSave(BaseModel):
    id: str = ""
    name: str = ""
    subject: Optional[str] = None
    preheader: Optional[str] = None
    body: Optional[str] = None
    audience: Optional[list] = None


class CampaignSend(BaseModel):
    at: str = ""


class PreviewRequest(BaseModel):
    subject: str = ""
    preheader: str = ""
    body: str = ""


class InboxUpdate(BaseModel):
    status: str


class GuardAddress(BaseModel):
    email: str


class GuardAlerts(BaseModel):
    alerts: str


# ---------------------------------------------------------------- routes

def register(app, *, account_user, auth_headers, supabase_url, check_rate_limit, logger, public_api, user_premium=None):
    async def _no_premium(_uid):
        return False
    _ctx.update(account_user=account_user, auth_headers=auth_headers, supabase_url=supabase_url,
                check_rate_limit=check_rate_limit, logger=logger, public_api=public_api.rstrip("/"),
                user_premium=user_premium or _no_premium)
    logger.info("CashCow Mail config: SES ready=%s, address set=%s, admins=%s, webhook key set=%s",
                ses_ready(), bool(MAIL_POSTAL_ADDRESS), len(MAIL_ADMIN_EMAILS), len(MAIL_WEBHOOK_KEY) >= 16)

    on_event = getattr(app, "on_event", None)
    if on_event:
        @on_event("startup")
        async def _start_mail_loop():
            asyncio.create_task(_tick_loop())

    async def admin(request: Request) -> dict:
        user = await account_user(request.headers.get("authorization", ""))
        if not user:
            raise HTTPException(status_code=401, detail="Please sign in to your CashCow account.")
        if (user.get("email") or "").lower() not in MAIL_ADMIN_EMAILS:
            raise HTTPException(status_code=403, detail="This account isn't allowed into CashCow Mail.")
        await maybe_tick()
        return user

    def uuid_or_404(v: str) -> str:
        if not re.fullmatch(r"[0-9a-fA-F-]{36}", v or ""):
            raise HTTPException(status_code=404, detail="Not found.")
        return v

    def clean_tags(tags) -> list:
        return [t for t in (str(x).strip().lower() for x in (tags or [])[:20]) if re.fullmatch(r"[a-z0-9-]{1,30}", t)]

    # ----- public: sign up, confirm, unsubscribe -----

    @app.post("/api/mail/subscribe")
    async def mail_subscribe(payload: SubscribeRequest, request: Request):
        check_rate_limit(request)
        if payload.hp.strip() or 0 < payload.elapsed < 2000:
            return {"success": True}
        email = payload.email.strip().lower()[:254]
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
            raise HTTPException(status_code=400, detail="Please enter a valid email address.")
        name = re.sub(r"[<>]", "", payload.name or "").strip()[:80]
        ip = request.client.host if request.client else ""
        res = await rpc("mail_subscribe", {"p_email": email, "p_name": name, "p_tags": clean_tags(payload.tags),
                                           "p_source": re.sub(r"[^a-z0-9-]", "", payload.source.lower())[:40] or "site",
                                           "p_ip": ip})
        if res and res.get("send_confirm"):
            link = f"{_ctx['public_api']}/api/mail/confirm?t={res['token']}"
            msg = EmailMessage()
            msg["From"] = MAIL_FROM or "CashCow <news@cashcowai.online>"
            msg["To"] = email
            msg["Subject"] = "Please confirm your CashCow emails"
            msg.set_content(f"Hi{(' ' + name.split()[0]) if name else ''},\n\nTap this link to confirm you'd like "
                            f"CashCow tips and news by email:\n{link}\n\nDidn't sign up? Just ignore this email "
                            f"and you won't hear from us.\n\nCashCow\n{MAIL_POSTAL_ADDRESS}\n")
            msg.add_alternative(
                f'<p>Hi{html.escape((" " + name.split()[0]) if name else "")},</p><p>Tap the button to confirm you\'d like '
                f'CashCow tips and news by email.</p><p><a href="{html.escape(link)}" style="display:inline-block;'
                f'background:#3C3489;color:#fff;text-decoration:none;font-weight:bold;padding:12px 22px;border-radius:10px">'
                f'Yes, sign me up</a></p><p style="color:#666;font-size:13px">Didn\'t sign up? Just ignore this email and '
                f'you won\'t hear from us.<br>CashCow, {html.escape(MAIL_POSTAL_ADDRESS)}</p>', subtype="html")
            try:
                await ses_send_raw(msg.as_bytes(policy=policy.SMTP), email)
            except SESError as e:
                logger.error("Confirmation email not sent: %s", e)
                raise HTTPException(status_code=503, detail="Email sign-up isn't open yet. Please try again soon.")
        # Same answer whether or not they were already on the list, so nobody can check who is.
        return {"success": True, "message": "Almost done! Check your email and tap the link to confirm."}

    @app.get("/api/mail/confirm")
    async def mail_confirm(request: Request, t: str = ""):
        if not _TOKEN_RE.match(t or ""):
            return _page("Link not valid", "<h1>That link isn't valid</h1><p>Please sign up again on "
                         f'<a href="{SITE_URL}">cashcowai.online</a>.</p>', 400)
        res = await rpc("mail_confirm", {"p_token": t, "p_ip": request.client.host if request.client else ""})
        if not res or not res.get("ok"):
            return _page("Link not valid", "<h1>That link isn't valid anymore</h1>"
                         f'<p>Please sign up again on <a href="{SITE_URL}">cashcowai.online</a>.</p>', 400)
        return _page("You're subscribed", "<h1>You're in! ✅</h1><p>Thanks for confirming. You'll get CashCow tips "
                     "and news by email. Every email has a one-tap unsubscribe link.</p>"
                     f'<p><a href="{SITE_URL}">Back to CashCow</a></p>')

    @app.get("/api/mail/unsubscribe")
    async def mail_unsubscribe_page(t: str = ""):
        if not _TOKEN_RE.match(t or ""):
            return _page("Link not valid", "<h1>That link isn't valid</h1>", 400)
        # A button instead of unsubscribing right away, so email security scanners that
        # open links can't unsubscribe people by accident.
        return _page("Unsubscribe", "<h1>Unsubscribe from CashCow emails?</h1>"
                     f'<form method="post" action="/api/mail/unsubscribe?t={t}"><button type="submit">'
                     "Yes, unsubscribe me</button></form>")

    @app.post("/api/mail/unsubscribe")
    async def mail_unsubscribe(t: str = ""):
        # Also handles one-click unsubscribe from Gmail and Yahoo (List-Unsubscribe-Post).
        if not _TOKEN_RE.match(t or ""):
            return _page("Link not valid", "<h1>That link isn't valid</h1>", 400)
        await rpc("mail_unsubscribe", {"p_token": t})
        return _page("Unsubscribed", "<h1>You're unsubscribed</h1><p>You won't get any more CashCow campaign emails. "
                     "Changed your mind? You can sign up again any time on "
                     f'<a href="{SITE_URL}">cashcowai.online</a>.</p>')

    # ----- Amazon notifications: bounces, complaints and incoming mail -----

    @app.post("/api/mail/sns/{key}")
    async def mail_sns(key: str, request: Request):
        if len(MAIL_WEBHOOK_KEY) < 16 or not hmac.compare_digest(key, MAIL_WEBHOOK_KEY):
            raise HTTPException(status_code=404, detail="Not found.")
        raw = await request.body()
        if len(raw) > 200_000:
            raise HTTPException(status_code=413, detail="Too large.")
        try:
            note = json.loads(raw)
        except ValueError:
            raise HTTPException(status_code=400, detail="Bad request.")
        topic = note.get("TopicArn", "")
        if MAIL_SNS_TOPICS and topic not in MAIL_SNS_TOPICS:
            raise HTTPException(status_code=403, detail="Unknown topic.")
        kind = note.get("Type")
        if kind == "SubscriptionConfirmation":
            sub_url = note.get("SubscribeURL", "")
            if re.match(r"^https://sns\.[a-z0-9-]+\.amazonaws\.com/", sub_url):
                async with httpx.AsyncClient(timeout=10) as client:
                    await client.get(sub_url)
                logger.info("Confirmed Amazon SNS subscription for %s", topic)
            return {"ok": True}
        if kind != "Notification":
            return {"ok": True}
        try:
            message = json.loads(note.get("Message") or "{}")
        except ValueError:
            return {"ok": True}
        await handle_ses_event(message)
        return {"ok": True}

    @app.api_route("/api/mail/tick/{key}", methods=["GET", "POST"]) if hasattr(app, "api_route") else (lambda f: f)
    async def mail_tick(key: str):
        if len(MAIL_WEBHOOK_KEY) < 16 or not hmac.compare_digest(key, MAIL_WEBHOOK_KEY):
            raise HTTPException(status_code=404, detail="Not found.")
        started = await tick()
        return {"ok": True, "sending": len(started or [])}

    # ----- the dashboard (admins only) -----

    @app.get("/api/mail/admin/me")
    async def mail_me(request: Request):
        user = await admin(request)
        return {"email": user["email"], "ses_ready": ses_ready(), "from": MAIL_FROM, "reply_to": MAIL_REPLY_TO,
                "address_set": bool(MAIL_POSTAL_ADDRESS), "webhook_set": len(MAIL_WEBHOOK_KEY) >= 16,
                "inbox_ready": bool(MAIL_REPLY_TO and MAIL_FORWARD_TO), "tags": APP_TAGS, "rate": MAIL_SEND_RATE}

    @app.get("/api/mail/admin/summary")
    async def mail_summary(request: Request):
        await admin(request)
        return await rpc("mail_summary", {})

    @app.get("/api/mail/admin/subscribers")
    async def mail_subscribers(request: Request, q: str = "", status: str = "", offset: int = 0):
        await admin(request)
        return await rpc("mail_list_subscribers", {"p_q": re.sub(r"[%_\\]", "", q)[:80], "p_status": status[:20],
                                                   "p_limit": 50, "p_offset": max(0, min(offset, 1_000_000))})

    @app.post("/api/mail/admin/subscribers")
    async def mail_add(payload: SubscribersAdd, request: Request):
        await admin(request)
        if not payload.consent:
            raise HTTPException(status_code=400, detail="Only add people who agreed to get email from CashCow. "
                                                        "Check the box to confirm they did.")
        rows = []
        for r in payload.rows[:5000]:
            if isinstance(r, dict):
                rows.append({"email": str(r.get("email", ""))[:254], "name": re.sub(r"[<>]", "", str(r.get("name", "")))[:80],
                             "tags": clean_tags(r.get("tags"))})
        return await rpc("mail_admin_add", {"p_rows": rows,
                                            "p_source": re.sub(r"[^a-z0-9-]", "", payload.source.lower())[:40] or "import"})

    @app.post("/api/mail/admin/subscribers/{sid}/update")
    async def mail_update_sub(sid: str, payload: SubscriberUpdate, request: Request):
        await admin(request)
        await rpc("mail_update_subscriber", {"p_id": uuid_or_404(sid),
                                             "p_status": "unsubscribed" if payload.unsubscribe else None,
                                             "p_tags": clean_tags(payload.tags) if payload.tags is not None else None})
        return {"ok": True}

    @app.get("/api/mail/admin/campaigns")
    async def mail_campaigns(request: Request):
        await admin(request)
        return {"campaigns": await rpc("mail_list_campaigns", {}) or []}

    @app.post("/api/mail/admin/campaigns")
    async def mail_save(payload: CampaignSave, request: Request):
        await admin(request)
        data = {"name": payload.name[:100]}
        if payload.id:
            data["id"] = uuid_or_404(payload.id)
        for field in ("subject", "preheader", "body"):
            v = getattr(payload, field)
            if v is not None:
                data[field] = v[:50000] if field == "body" else v[:200]
        if payload.audience is not None:
            data["audience"] = clean_tags(payload.audience)
        return await rpc("mail_save_campaign", {"p": data})

    @app.get("/api/mail/admin/campaigns/{cid}")
    async def mail_get(cid: str, request: Request):
        await admin(request)
        got = await rpc("mail_get_campaign", {"p_id": uuid_or_404(cid)})
        if not got:
            raise HTTPException(status_code=404, detail="Not found.")
        return got

    @app.post("/api/mail/admin/preview")
    async def mail_preview(payload: PreviewRequest, request: Request):
        user = await admin(request)
        raw = build_email({"subject": payload.subject, "preheader": payload.preheader, "body": payload.body[:50000]},
                          {"email": user["email"], "name": "Chris Johnson", "token": "0" * 36})
        msg = BytesParser(policy=policy.default).parsebytes(raw)
        part = msg.get_body(preferencelist=("html",))
        return {"html": part.get_content() if part else ""}

    @app.post("/api/mail/admin/campaigns/{cid}/scan")
    async def mail_scan(cid: str, request: Request):
        await admin(request)
        got = await rpc("mail_get_campaign", {"p_id": uuid_or_404(cid)})
        if not got:
            raise HTTPException(status_code=404, detail="Not found.")
        report = await scan_campaign(got["campaign"])
        report["audience_count"] = got.get("audience_count", 0)
        if not got.get("audience_count"):
            report["warnings"].append("Nobody subscribed matches this audience yet.")
        await rpc("mail_set_scan", {"p_id": cid, "p_scan": report})
        return report

    @app.post("/api/mail/admin/campaigns/{cid}/test")
    async def mail_test(cid: str, request: Request):
        user = await admin(request)
        got = await rpc("mail_get_campaign", {"p_id": uuid_or_404(cid)})
        if not got:
            raise HTTPException(status_code=404, detail="Not found.")
        try:
            await ses_send_raw(build_email(got["campaign"], {"email": user["email"], "name": "Chris Johnson",
                                                             "token": "0" * 36}, test=True), user["email"])
        except SESError as e:
            raise HTTPException(status_code=502, detail=str(e))
        return {"ok": True, "to": user["email"]}

    @app.post("/api/mail/admin/campaigns/{cid}/send")
    async def mail_send(cid: str, payload: CampaignSend, request: Request):
        await admin(request)
        if not ses_ready():
            raise HTTPException(status_code=409, detail="Amazon SES isn't connected yet.")
        at = None
        if payload.at:
            try:
                at = _dt.datetime.fromisoformat(payload.at.replace("Z", "+00:00"))
                if at.tzinfo is None:
                    raise ValueError
            except ValueError:
                raise HTTPException(status_code=400, detail="Pick a valid date and time.")
            if at > _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(days=90):
                raise HTTPException(status_code=400, detail="Schedule within the next 90 days.")
        res = await rpc("mail_schedule", {"p_id": uuid_or_404(cid), "p_at": at.isoformat() if at else None})
        if not at:
            await tick()
        return res

    @app.post("/api/mail/admin/campaigns/{cid}/cancel")
    async def mail_cancel(cid: str, request: Request):
        await admin(request)
        await rpc("mail_cancel", {"p_id": uuid_or_404(cid)})
        return {"ok": True}

    @app.post("/api/mail/admin/campaigns/{cid}/delete")
    async def mail_delete(cid: str, request: Request):
        await admin(request)
        await rpc("mail_delete_campaign", {"p_id": uuid_or_404(cid)})
        return {"ok": True}

    @app.get("/api/mail/admin/inbox")
    async def mail_inbox(request: Request):
        await admin(request)
        return {"messages": await rpc("mail_inbox_list", {"p_limit": 100}) or []}

    @app.post("/api/mail/admin/inbox/{mid}/update")
    async def mail_inbox_update(mid: str, payload: InboxUpdate, request: Request):
        await admin(request)
        await rpc("mail_inbox_update", {"p_id": uuid_or_404(mid), "p_status": payload.status[:10]})
        return {"ok": True}

    # ----- ScamShield Email Guard settings (any signed-in CashCow account) -----

    async def guard_user(request: Request) -> dict:
        user = await account_user(request.headers.get("authorization", ""))
        if not user:
            raise HTTPException(status_code=401, detail="Please sign in to your CashCow account.")
        return user

    @app.get("/api/guard/me")
    async def guard_me(request: Request):
        user = await guard_user(request)
        data = await rpc("guard_list", {"p_user": user["id"]}) or {}
        premium = (user.get("email") or "").lower() in MAIL_ADMIN_EMAILS or await _ctx["user_premium"](user["id"])
        return {**data, "premium": bool(premium), "guard_address": MAIL_GUARD_ADDRESS,
                "ready": bool(MAIL_GUARD_ADDRESS and ses_ready()), "daily_limit": GUARD_DAILY_LIMIT,
                "free_monthly": GUARD_FREE_MONTHLY}

    @app.post("/api/guard/addresses")
    async def guard_add(payload: GuardAddress, request: Request):
        user = await guard_user(request)
        check_rate_limit(request)
        if not (MAIL_GUARD_ADDRESS and ses_ready()):
            raise HTTPException(status_code=503, detail="Email Guard isn't open yet. Please check back soon.")
        email = payload.email.strip().lower()[:254]
        if email.endswith("@" + MAIL_GUARD_ADDRESS.split("@")[-1]):
            raise HTTPException(status_code=400, detail="Add the email address you read your mail at.")
        res = await rpc("guard_add", {"p_user": user["id"], "p_email": email})
        if res.get("token"):
            link = f"{_ctx['public_api']}/api/guard/verify?t={res['token']}"
            await ses_send_raw(_guard_message(
                res["email"], "Confirm your email for ScamShield Email Guard",
                f"Tap this link to let ScamShield Email Guard check emails you forward from this address:\n{link}\n\n"
                "Didn't ask for this? Ignore this email and nothing changes.\n\nScamShield by CashCow"), res["email"])
        return {"id": res["id"], "email": res["email"], "verified": res["verified"]}

    @app.get("/api/guard/verify")
    async def guard_verify(t: str = ""):
        if not _TOKEN_RE.match(t or ""):
            return _page("Link not valid", "<h1>That link isn't valid</h1>", 400)
        res = await rpc("guard_verify", {"p_token": t})
        if not res or not res.get("ok"):
            return _page("Link not valid", "<h1>That link isn't valid anymore</h1><p>Add the address again in Email Guard.</p>", 400)
        return _page("Email confirmed", f"<h1>Confirmed ✅</h1><p>{html.escape(res['email'])} is set up for ScamShield "
                     f"Email Guard.</p><p><a href=\"{GUARD_APP_URL}\">Back to Email Guard</a></p>")

    @app.post("/api/guard/addresses/{aid}/delete")
    async def guard_remove(aid: str, request: Request):
        user = await guard_user(request)
        await rpc("guard_remove", {"p_user": user["id"], "p_id": uuid_or_404(aid)})
        return {"ok": True}

    @app.post("/api/guard/addresses/{aid}/alerts")
    async def guard_alerts(aid: str, payload: GuardAlerts, request: Request):
        user = await guard_user(request)
        await rpc("guard_set_alerts", {"p_user": user["id"], "p_id": uuid_or_404(aid), "p_alerts": payload.alerts[:10]})
        return {"ok": True}

    return app


async def handle_ses_event(message: dict):
    """Bounces and complaints keep the list healthy; incoming mail goes through the inbox scanner."""
    kind = message.get("notificationType") or message.get("eventType") or ""
    if kind == "Bounce":
        bounce = message.get("bounce") or {}
        if bounce.get("bounceType") == "Permanent":
            for r in bounce.get("bouncedRecipients") or []:
                if r.get("emailAddress"):
                    await rpc("mail_set_status_by_email", {"p_email": r["emailAddress"], "p_status": "bounced"})
    elif kind == "Complaint":
        for r in (message.get("complaint") or {}).get("complainedRecipients") or []:
            if r.get("emailAddress"):
                await rpc("mail_set_status_by_email", {"p_email": r["emailAddress"], "p_status": "complained"})
    elif kind == "Received":
        content = message.get("content")
        if not content:
            return
        try:
            raw = base64.b64decode(content, validate=True)
        except (ValueError, TypeError):
            raw = content.encode("utf-8", "replace")
        receipt = dict(message.get("receipt") or {})
        receipt["_envelope_from"] = ((message.get("mail") or {}).get("source") or "")
        recipients = [r.lower() for r in receipt.get("recipients") or []]
        if MAIL_GUARD_ADDRESS and MAIL_GUARD_ADDRESS in recipients:
            await handle_guard(raw, receipt)
        else:
            await handle_incoming(raw, receipt)


async def handle_incoming(raw: bytes, receipt: dict):
    if b"X-CashCow-Guard" in raw[:20000]:
        return None  # our own alert coming back around
    scan = scan_incoming(raw, receipt)
    forwarded = False
    if scan["category"] == "unsubscribe" and scan["from_addr"]:
        await rpc("mail_set_status_by_email", {"p_email": scan["from_addr"], "p_status": "unsubscribed"})
    elif scan["category"] in ("lead", "reply") and scan["verdict"] != "dangerous" and MAIL_FORWARD_TO and ses_ready():
        try:
            await ses_send_raw(_forward_email(scan), MAIL_FORWARD_TO)
            forwarded = True
        except SESError as e:
            _log().error("Couldn't forward a scanned reply: %s", e)
    await rpc("mail_inbox_add", {"p": {k: scan[k] for k in ("from_addr", "from_name", "subject", "snippet", "verdict",
                                                            "category", "flags")} | {"forwarded": forwarded}})
    return scan


def _forward_email(scan: dict) -> bytes:
    badge = {"safe": "✅ Looks safe", "suspicious": "⚠️ Be careful", "dangerous": "⛔ Dangerous"}[scan["verdict"]]
    label = "[Lead]" if scan["category"] == "lead" else "[Reply]"
    lines = [f"CashCow inbox scan: {badge}"]
    if scan["flags"]:
        lines += ["Why: " + "; ".join(scan["flags"])]
    lines += ["Attachments and links in the original were not forwarded, for safety.", "",
              f"From: {scan['from_name']} <{scan['from_addr']}>", f"Subject: {scan['subject']}", "",
              _URL_IN_TEXT.sub(lambda m: f"[link to {(urlsplit(m.group(0)).hostname or 'a website')}]", scan["body"])]
    msg = EmailMessage()
    msg["From"] = MAIL_FROM
    msg["To"] = MAIL_FORWARD_TO
    if scan["from_addr"]:
        msg["Reply-To"] = formataddr((scan["from_name"], scan["from_addr"]))
    msg["Subject"] = f"{label} {scan['subject'] or '(no subject)'}"[:200]
    msg.set_content("\n".join(lines))
    return msg.as_bytes(policy=policy.SMTP)


# ---------------------------------------------------------------- ScamShield Email Guard: what happens on arrival

_notice_sent = {}   # address -> time we last told them about Premium (once a day at most)
_GMAIL_CONFIRM = re.compile(r"([\w.+'-]+@[\w-]+(?:\.[\w-]+)+) has requested to automatically forward mail", re.I)


def _auth_ok(receipt: dict) -> bool:
    v = lambda k: ((receipt.get(k) or {}).get("status") or "").upper()
    return v("dkimVerdict") == "PASS" or v("spfVerdict") == "PASS"


def _once_a_day(addr: str) -> bool:
    now = time.time()
    if now - _notice_sent.get(addr, 0) < 86400:
        return False
    _notice_sent[addr] = now
    if len(_notice_sent) > 5000:
        for k in sorted(_notice_sent, key=_notice_sent.get)[:1000]:
            _notice_sent.pop(k, None)
    return True


def _guard_message(to: str, subject: str, text: str) -> bytes:
    msg = EmailMessage()
    msg["From"] = MAIL_FROM
    msg["To"] = to
    msg["Subject"] = subject[:200]
    msg["X-CashCow-Guard"] = "1"            # so it's never scanned again if their mail forwards it back
    msg["Auto-Submitted"] = "auto-generated"
    msg.set_content(text)
    return msg.as_bytes(policy=policy.SMTP)


VERDICT_WORDS = {
    "dangerous": ("⛔ Likely scam", "Don't click any links, open attachments, reply, or call phone numbers in it. If it says it's from a company "
                  "you use, go to their website by typing the address yourself, or call the number on your card or bill. Then delete it.\n\n"
                  "Report it: forward phishing emails to reportphishing@apwg.org, and report fraud at ReportFraud.ftc.gov."),
    "suspicious": ("⚠️ Be careful", "This email has some warning signs. Don't click links or send money or personal details until you've "
                   "checked it another way, like calling the company at a number you already know."),
    "safe": ("✅ No scam signs found", "Our check didn't find warning signs. No automatic check catches everything, so still be careful "
             "with any request for money, passwords or personal details."),
}


def _verdict_text(scan: dict) -> str:
    title, advice = VERDICT_WORDS[scan["verdict"]]
    lines = [f"ScamShield Email Guard: {title}", "",
             f"Email from: {scan['from_name'] + ' ' if scan['from_name'] else ''}<{scan['from_addr'] or 'unknown'}>",
             f"Subject: {scan['subject'] or '(no subject)'}", ""]
    if scan["flags"]:
        lines.append("What we found:")
        lines += [f"- {f[0].upper() + f[1:]}" for f in scan["flags"]]
        lines.append("")
    lines += [advice, "", "This is an automatic check, not a guarantee.",
              f"Manage Email Guard: {GUARD_APP_URL}", "ScamShield by CashCow"]
    return "\n".join(lines)


async def handle_guard(raw: bytes, receipt: dict):
    scan = guard_scan(raw, receipt)
    if scan["is_ours"]:
        return {"skipped": "ours"}

    # Gmail asks the forwarding address to confirm before auto-forwarding starts. Pass the code to the person.
    if scan["outer_from"] == "forwarding-noreply@google.com" and "forwarding confirmation" in scan["outer_subject"].lower():
        m = _GMAIL_CONFIRM.search(scan["text"]) or _GMAIL_CONFIRM.search(scan["outer_subject"])
        code = re.search(r"#(\d{6,12})", scan["outer_subject"])
        if not (m and code and _auth_ok(receipt)):
            return {"skipped": "bad confirmation"}
        owner = await rpc("guard_owner", {"p_emails": [m.group(1).lower()]})
        if not owner:
            return {"skipped": "confirmation for unknown address"}
        await ses_send_raw(_guard_message(
            owner["email"], f"Your Gmail forwarding code: {code.group(1)}",
            f"Here's the code Gmail needs to start sending your email to ScamShield Email Guard:\n\n    {code.group(1)}\n\n"
            "In Gmail on a computer: Settings → See all settings → Forwarding and POP/IMAP → enter the code → Verify. "
            "Then choose \"Forward a copy of incoming mail to\" " + MAIL_GUARD_ADDRESS + " and \"keep Gmail's copy in the Inbox\", "
            f"and Save Changes.\n\nDidn't ask for this? Ignore this email and nothing changes.\n\nScamShield by CashCow\n{GUARD_APP_URL}"),
            owner["email"])
        return {"relayed_code": owner["email"]}

    owner = await rpc("guard_owner", {"p_emails": scan["owners"][:5]}) if scan["owners"] else None
    if not owner:
        # Someone we don't know. Only answer real, unforged manual forwards, at most once a day.
        if scan["mode"] == "manual" and _auth_ok(receipt) and scan["outer_from"] and _once_a_day(scan["outer_from"]):
            await ses_send_raw(_guard_message(
                scan["outer_from"], "Set up ScamShield Email Guard to check emails",
                "Thanks for forwarding an email to ScamShield Email Guard. To get scam checks, first add and confirm your "
                f"email address in Email Guard:\n{GUARD_APP_URL}\n\nScamShield by CashCow"), scan["outer_from"])
        return {"skipped": "unknown sender"}

    is_admin = owner["email"] in MAIL_ADMIN_EMAILS
    premium = is_admin or await _ctx["user_premium"](owner["user_id"])
    if not premium:
        # Free ScamShield: 3 forwarded checks a month. Automatic checking is Premium.
        if scan["mode"] != "manual":
            return {"skipped": "auto needs premium"}
        if int(owner.get("month") or 0) >= GUARD_FREE_MONTHLY:
            if _once_a_day(owner["email"]):
                await ses_send_raw(_guard_message(
                    owner["email"], "You've used your 3 free ScamShield checks this month",
                    f"Free ScamShield accounts get {GUARD_FREE_MONTHLY} email checks a month. ScamShield Premium gives you "
                    "unlimited checks, automatic protection for every new email, and every other Premium feature, "
                    f"for $29 a month or $249 a year.\n\nUpgrade here: {GUARD_APP_URL}\n\nScamShield by CashCow"), owner["email"])
            return {"skipped": "free limit"}
    elif int(owner.get("today") or 0) >= GUARD_DAILY_LIMIT:
        return {"skipped": "daily limit"}

    alert = scan["mode"] == "manual" or scan["verdict"] != "safe" or owner.get("alerts") == "all"
    if alert:
        title = VERDICT_WORDS[scan["verdict"]][0]
        try:
            await ses_send_raw(_guard_message(owner["email"], f"{title}: {scan['subject'] or '(no subject)'}",
                                              _verdict_text(scan)), owner["email"])
        except SESError as e:
            _log().error("Email Guard alert not sent: %s", e)
            alert = False
    await rpc("guard_log", {"p": {"user_id": owner["user_id"], "mode": scan["mode"], "verdict": scan["verdict"],
                                  "from_addr": scan["from_addr"], "subject": scan["subject"], "flags": scan["flags"],
                                  "alerted": alert}})
    return {"owner": owner["email"], "mode": scan["mode"], "verdict": scan["verdict"], "alerted": alert}
