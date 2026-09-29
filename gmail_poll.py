"""
Gmail API polling + CME block trade parsing.
Reads credentials from Streamlit secrets (st.secrets) when deployed on Streamlit
Community Cloud, or falls back to local token.json for local runs.
"""
import re
import json
import base64
import html
from datetime import datetime, timedelta, timezone

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

try:
    import streamlit as st
    _HAS_STREAMLIT = True
except ImportError:
    _HAS_STREAMLIT = False

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

CT_TO_ET_HOURS = 1
ET_TO_BST_HOURS = 5

CATEGORY_KEYWORDS = [
    ("SOFR", "SOFR"),
    ("Federal Funds", "Fed Funds"),
    ("T-Note", "Treasuries"),
    ("T-Bond", "Treasuries"),
    ("Ultra", "Treasuries"),
    ("Treasury", "Treasuries"),
]

TR_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.IGNORECASE | re.DOTALL)
TD_RE = re.compile(r"<td[^>]*>(.*?)</td>", re.IGNORECASE | re.DOTALL)
TAG_RE = re.compile(r"<[^>]+>")


# ---------- Gmail auth ----------

def get_gmail_service():
    if _HAS_STREAMLIT and "gmail_token" in st.secrets:
        # token.json contents pasted into Streamlit Cloud's Secrets manager as gmail_token
        token_info = json.loads(st.secrets["gmail_token"]) if isinstance(st.secrets["gmail_token"], str) else dict(st.secrets["gmail_token"])
        creds = Credentials.from_authorized_user_info(token_info, SCOPES)
    else:
        creds = Credentials.from_authorized_user_file("token.json", SCOPES)

    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        if not (_HAS_STREAMLIT and "gmail_token" in st.secrets):
            with open("token.json", "w") as f:
                f.write(creds.to_json())
    return build("gmail", "v1", credentials=creds)

# ---------- Fetching ----------

def fetch_recent_block_trade_emails(service, hours_back=24):
    """Returns list of {id, date (datetime utc), plaintext_body} for CME block trade emails."""
    query = f"from:cmegroup.com newer_than:{max(1, hours_back // 24 + 1)}d"
    results = service.users().messages().list(userId="me", q=query, maxResults=50).execute()
    messages = results.get("messages", [])

    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours_back)
    emails = []
    for msg_meta in messages:
        msg = service.users().messages().get(userId="me", id=msg_meta["id"], format="full").execute()
        date_ms = int(msg["internalDate"])
        date_utc = datetime.fromtimestamp(date_ms / 1000, tz=timezone.utc)
        if date_utc < cutoff:
            continue
        body = _extract_plaintext_body(msg["payload"])
        emails.append({"id": msg_meta["id"], "date": date_utc, "body": body})
    return emails

def _extract_plaintext_body(payload):
    """Walk the MIME parts to find the text/plain body, base64-decoded."""
    if payload.get("mimeType") == "text/plain" and "data" in payload.get("body", {}):
        return base64.urlsafe_b64decode(payload["body"]["data"]).decode("utf-8", errors="replace")
    for part in payload.get("parts", []):
        result = _extract_plaintext_body(part)
        if result:
            return result
    return ""

# ---------- Parsing (same logic validated earlier) ----------

def clean(s):
    # CME table cells contain nested <span> tags and HTML entities.
    text = TAG_RE.sub("", s)
    return html.unescape(text).replace("\xa0", " ").strip()

def price_to_headline_fmt(price_str):
    p = price_str.replace("'", "-")
    if "-" not in p and "." in p:
        p = p.rstrip("0").rstrip(".")
    return p

def qty_to_headline(qty_str):
    n = float(qty_str.replace(",", "").strip())
    if n >= 1000:
        val = n / 1000
        s = f"{val:.1f}".rstrip("0").rstrip(".")
        if "." not in s:
            s += ".0"
        return f"{s}k"
    return str(int(n))

def parse_time_ct(time_str, date_hint):
    t = datetime.strptime(time_str.strip(), "%I:%M:%S %p")
    return t.replace(year=date_hint.year, month=date_hint.month, day=date_hint.day)

def ct_to_et_bst(ct_dt):
    et_dt = ct_dt + timedelta(hours=CT_TO_ET_HOURS)
    bst_dt = et_dt + timedelta(hours=ET_TO_BST_HOURS)
    return et_dt, bst_dt

def fmt_hhmm(dt):
    return dt.strftime("%H:%M")

def categorize(product_text):
    for keyword, header in CATEGORY_KEYWORDS:
        if keyword.lower() in product_text.lower():
            return header
    return product_text

def parse_rows(plaintext_body, email_date_utc):
    """
    Parse CME's HTML table.

    CME uses rowspan for TIME and TYPE on multi-leg spreads. The first row has
    all 9 columns; continuation legs only have the remaining 7 columns.
    Carry the time/type forward so every leg is retained.
    """
    date_hint = email_date_utc - timedelta(hours=5)  # rough UTC->CT calendar date anchor
    parsed = []
    current_time = ""
    current_type = ""

    for row_html in TR_RE.findall(plaintext_body):
        cells = [clean(x) for x in TD_RE.findall(row_html)]

        # Header/non-trade rows.
        if len(cells) < 7:
            continue

        if len(cells) >= 9:
            time_raw, ttype, product, sym, net_price, qty, cp_strike, side, price = cells[:9]

            # Skip the column-header row.
            if time_raw.upper().startswith("TIME"):
                continue

            if time_raw:
                current_time = time_raw
            if ttype:
                current_type = ttype
        elif len(cells) == 7:
            # TIME and TYPE are row-spanned from the first leg of the spread.
            if not current_time:
                continue
            time_raw = current_time
            ttype = current_type
            product, sym, net_price, qty, cp_strike, side, price = cells
        else:
            continue

        try:
            ct_dt = parse_time_ct(time_raw, date_hint)
        except ValueError:
            continue

        et_dt, bst_dt = ct_to_et_bst(ct_dt)
        parsed.append({
            "ct_dt": ct_dt,
            "time_et": fmt_hhmm(et_dt),
            "time_bst": fmt_hhmm(bst_dt),
            "type": ttype,
            "product": product,
            "sym": sym,
            "qty_raw": qty,
            "qty": qty_to_headline(qty),
            "cp_strike": cp_strike,
            "side": side,
            "price": price_to_headline_fmt(price if price else net_price),
            "category": categorize(product),
        })

    return parsed


def build_headlines(parsed_rows):
    """
    Build copy-ready headlines in the desk format:
      Futures
      Options
      Spreads

    CME rows marked as Spread and sharing the same trade timestamp are combined
    into one headline, so an option leg + futures hedge prints on one line.
    """
    spread_groups = {}
    singles = []

    for r in parsed_rows:
        if r["type"].strip().lower() == "spread":
            spread_groups.setdefault(r["ct_dt"], []).append(r)
        else:
            singles.append(r)

    headlines_by_category = {}

    def add_headline(category, r, text):
        headlines_by_category.setdefault(category, []).append({
            "sort_key": r["ct_dt"],
            "line": f"{r['time_et']}ET/{r['time_bst']}BST: {text}"
        })

    # Outright futures / options
    for r in singles:
        if r["cp_strike"]:
            text = _format_leg(r)
            add_headline("Options", r, text)
        else:
            text = f"{r['qty']} {r['product']} ({r['sym']}) at {r['price']}"
            add_headline("Futures", r, text)

    # Multi-leg CME spreads
    for _, group in spread_groups.items():
        first = group[0]
        text = " & ".join(_format_leg(r) for r in group)
        add_headline("Spreads", first, text)

    # Chronological order within each section
    for category in headlines_by_category:
        headlines_by_category[category].sort(key=lambda h: h["sort_key"])

    # Keep the display/copy order consistent
    ordered = {}
    for category in ("Futures", "Options", "Spreads", "SOFR", "Fed Funds"):
        if category in headlines_by_category:
            ordered[category] = headlines_by_category[category]
    for category, items in headlines_by_category.items():
        if category not in ordered:
            ordered[category] = items

    return ordered

def get_all_headlines(hours_back=24, seen_ids=None):
    """
    Main entry point for the Streamlit app.
    Returns (headlines_by_category, new_message_ids, all_message_ids_seen_this_call)
    seen_ids: set of message IDs already processed in a prior call, to skip re-parsing.
    """
    seen_ids = seen_ids or set()
    service = get_gmail_service()
    emails = fetch_recent_block_trade_emails(service, hours_back=hours_back)

    new_ids = [e["id"] for e in emails if e["id"] not in seen_ids]
    all_ids = [e["id"] for e in emails]

    all_rows = []
    for e in emails:
        all_rows.extend(parse_rows(e["body"], e["date"]))

    headlines = build_headlines(all_rows)
    return headlines, new_ids, all_ids
