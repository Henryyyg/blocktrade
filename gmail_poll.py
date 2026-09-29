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
from zoneinfo import ZoneInfo

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

def fetch_block_trade_emails_for_date(service, trade_date):
    """Return CME block-trade emails received on the selected New York date."""
    eastern = ZoneInfo("America/New_York")
    today_et = datetime.now(eastern).date()
    days_back = max(1, (today_et - trade_date).days + 2)
    query = f"from:cmegroup.com newer_than:{days_back}d"
    results = service.users().messages().list(userId="me", q=query, maxResults=100).execute()
    emails = []
    for msg_meta in results.get("messages", []):
        msg = service.users().messages().get(userId="me", id=msg_meta["id"], format="full").execute()
        date_ms = int(msg["internalDate"])
        date_utc = datetime.fromtimestamp(date_ms / 1000, tz=timezone.utc)
        if date_utc.astimezone(eastern).date() != trade_date:
            continue
        body = _extract_email_body(msg["payload"])
        emails.append({"id": msg_meta["id"], "date": date_utc, "body": body})
    return emails

def _extract_mime_body(payload, wanted_mime):
    """Recursively find and decode a particular MIME body."""
    if payload.get("mimeType") == wanted_mime and "data" in payload.get("body", {}):
        return base64.urlsafe_b64decode(payload["body"]["data"]).decode("utf-8", errors="replace")
    for part in payload.get("parts", []):
        result = _extract_mime_body(part, wanted_mime)
        if result:
            return result
    return ""


def _extract_email_body(payload):
    """
    Prefer CME's HTML body because multi-leg block trades use HTML rowspan
    cells for TIME/TYPE. Fall back to text/plain only if HTML is unavailable.
    """
    html_body = _extract_mime_body(payload, "text/html")
    if html_body:
        return html_body
    return _extract_mime_body(payload, "text/plain")


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

def debug_table_rows(email_body):
    """Return CME table rows/cells for temporary parser diagnostics."""
    debug = []
    for i, row_html in enumerate(TR_RE.findall(email_body)):
        cells = [clean(x) for x in TD_RE.findall(row_html)]
        if cells:
            debug.append({"row": i, "cell_count": len(cells), "cells": cells})
    return debug


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

        # Header/non-trade rows. CME spread continuation legs have 6 cells.
        if len(cells) < 6:
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
        elif len(cells) == 6:
            # CME row-spans TIME/TYPE and omits NET PRICE on continuation legs.
            # Actual continuation layout:
            # PRODUCT, SYM, QTY, C/P & STRIKE, B/S, PRICE
            if not current_time:
                continue
            time_raw = current_time
            ttype = current_type
            product, sym, qty, cp_strike, side, price = cells
            net_price = ""
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



def _option_details(cp_strike):
    """Convert CME notation such as C107.50 / P106.50 to desk wording."""
    value = cp_strike.strip()
    match = re.match(r"^([CP])\s*([0-9.]+)$", value, re.IGNORECASE)
    if not match:
        return value
    option_type = "calls" if match.group(1).upper() == "C" else "puts"
    return f"{match.group(2)} {option_type}"


def _action_word(side):
    side = side.strip().lower()
    if side == "buy":
        return "Bought"
    if side == "sell":
        return "Sold"
    return side.capitalize()


def _format_leg(r):
    """Format one leg in the normal block-trade headline style."""
    action = _action_word(r["side"])
    if r["cp_strike"]:
        price_word = "for" if r["side"].strip().lower() == "buy" else "at"
        option = _option_details(r["cp_strike"])
        return f"{action} {r['qty']} {r['product']}, {option} ({r['sym']}) {price_word} {r['price']}"
    return f"{action} {r['qty']} {r['product']} ({r['sym']}) at {r['price']}"

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
            spread_groups.setdefault((r.get("email_id"), r["ct_dt"]), []).append(r)
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
        legs = [_format_leg(r) for r in group]
        for i in range(1, len(legs)):
            legs[i] = legs[i][0].lower() + legs[i][1:] if legs[i] else legs[i]
        text = " & ".join(legs)
        add_headline("Spreads", first, text)

    # Chronological order within each section
    for category in headlines_by_category:
        headlines_by_category[category].sort(key=lambda h: h["sort_key"], reverse=True)

    # Keep the display/copy order consistent
    ordered = {}
    for category in ("Futures", "Options", "Spreads", "SOFR", "Fed Funds"):
        if category in headlines_by_category:
            ordered[category] = headlines_by_category[category]
    for category, items in headlines_by_category.items():
        if category not in ordered:
            ordered[category] = items

    return ordered

def get_all_headlines(trade_date=None, seen_ids=None):
    """Return headlines for one selected New York/ET calendar date."""
    seen_ids = seen_ids or set()
    eastern = ZoneInfo("America/New_York")
    if trade_date is None:
        trade_date = datetime.now(eastern).date()
    service = get_gmail_service()
    emails = fetch_block_trade_emails_for_date(service, trade_date)
    new_ids = [e["id"] for e in emails if e["id"] not in seen_ids]
    all_ids = [e["id"] for e in emails]
    all_rows = []
    seen_trade_signatures = set()

    for e in emails:
        email_rows = parse_rows(e["body"], e["date"])
        if not email_rows:
            continue

        # CME can deliver the same block alert more than once. Treat emails
        # containing the exact same trade/legs as one block trade.
        trade_signature = tuple(
            (
                r["ct_dt"],
                r["type"],
                r["product"],
                r["sym"],
                r["qty_raw"],
                r["cp_strike"],
                r["side"],
                r["price"],
            )
            for r in email_rows
        )
        if trade_signature in seen_trade_signatures:
            continue
        seen_trade_signatures.add(trade_signature)

        for row in email_rows:
            row["email_id"] = e["id"]
        all_rows.extend(email_rows)

    # Desk view: newest block trades first.
    all_rows.sort(key=lambda r: r["ct_dt"], reverse=True)
    headlines = build_headlines(all_rows)
    return headlines, new_ids, all_ids, all_rows

