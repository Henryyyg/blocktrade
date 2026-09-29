"""
Block Trade Headlines — auto-refreshing Streamlit app.

Setup: see gmail_api_setup.md. Requires credentials.json + token.json in this folder.
Run: streamlit run app.py
"""
import time
from datetime import datetime
from zoneinfo import ZoneInfo
import streamlit as st
from gmail_poll import get_all_headlines

st.set_page_config(page_title="Block Trade Headlines", layout="wide")


# --- Simple access gate ---
if not st.session_state.get("authenticated", False):
    st.title("Block Trade Headlines")
    st.subheader("Newsquawk access")
    username = st.text_input("Username")
    password = st.text_input("Password", type="password")

    if st.button("Log in"):
        if (
            username == st.secrets["app_username"]
            and password == st.secrets["app_password"]
        ):
            st.session_state.authenticated = True
            st.rerun()
        else:
            st.error("Incorrect username or password.")

    st.stop()


REFRESH_SECONDS = 60

st.title("Block Trade Headlines")

# --- Sidebar controls ---
with st.sidebar:
    st.header("Settings")
    today_et = datetime.now(ZoneInfo("America/New_York")).date()
    trade_date = st.date_input("Trade date (ET)", value=today_et, max_value=today_et)
    notifications_on = st.checkbox("Sound notification for new blocks", value=True)
    manual_refresh = st.button("Refresh now")
    st.caption("Live feed checks automatically every 60 seconds.")


# --- Session state for tracking seen emails ---
if "seen_ids" not in st.session_state:
    st.session_state.seen_ids = set()
if "alerts_initialized" not in st.session_state:
    st.session_state.alerts_initialized = False
if "last_headlines" not in st.session_state:
    st.session_state.last_headlines = {}
if "last_trade_rows" not in st.session_state:
    st.session_state.last_trade_rows = []
if "last_checked" not in st.session_state:
    st.session_state.last_checked = None

# --- Live polling ---
@st.fragment(run_every=REFRESH_SECONDS)
def live_block_feed():
    error = None
    try:
        headlines, new_ids, all_ids, trade_rows = get_all_headlines(
            trade_date=trade_date,
            seen_ids=st.session_state.seen_ids
        )
        st.session_state.last_headlines = headlines
        st.session_state.last_trade_rows = trade_rows
        st.session_state.seen_ids.update(all_ids)
        st.session_state.last_checked = time.strftime("%H:%M:%S")
        # On the first load/reload, establish today's existing emails as the baseline.
        # Only alert for IDs that appear on a later poll in the same Streamlit session.
        if st.session_state.alerts_initialized and new_ids:
            st.toast(f"{len(new_ids)} new block trade email(s) found", icon="📬")
            if notifications_on:
                st.components.v1.html('<audio autoplay><source src="data:audio/wav;base64,UklGRiQAAABXQVZFZm10IBAAAAABAAEAQB8AAEAfAAABAAgAZGF0YQAAAAA=" type="audio/wav"></audio>', height=0)
        st.session_state.alerts_initialized = True
    except FileNotFoundError:
        error = "credentials.json or token.json not found. Follow gmail_api_setup.md first."
    except Exception as e:
        error = f"Error fetching from Gmail: {e}"
    
    if error:
        st.error(error)
    else:
        st.caption(f"Last checked: {st.session_state.last_checked} · trade date {trade_date.strftime('%d/%m/%Y')} ET")
    
        headlines = st.session_state.last_headlines
        if not headlines:
            st.info("No block trades found for this ET date.")
        else:
            # Build plain-text version for easy copy-paste to clients
            full_text_parts = []
            for category, items in headlines.items():
                full_text_parts.append(category)
                full_text_parts.append("")
                for h in items:
                    full_text_parts.append(f"* {h['line']}")
                full_text_parts.append("")
            full_text = "\n".join(full_text_parts).strip()
    
            # CME-style view. Group spread legs together so it is visually clear
            # that rows sharing a spread timestamp belong to one block trade.
            st.subheader("Block Trades")
            rows = st.session_state.last_trade_rows
            display_groups = []
            used_spreads = set()
    
            for r in rows:
                if r["type"].strip().lower() == "spread":
                    spread_key = r["ct_dt"]
                    if spread_key in used_spreads:
                        continue
                    used_spreads.add(spread_key)
                    group = [
                        x for x in rows
                        if x["type"].strip().lower() == "spread" and x["ct_dt"] == spread_key
                    ]
                    display_groups.append(("spread", group))
                else:
                    display_groups.append(("single", [r]))
    
            for group_type, group in display_groups:
                if group_type == "spread":
                    st.markdown(
                        f"**Spread trade · {group[0]['time_et']} ET · {len(group)} legs**"
                    )
    
                table_rows = []
                for r in group:
                    table_rows.append({
                        "Time (ET/BST)": f"{r['time_et']}/{r['time_bst']}",
                        "Type": r["type"],
                        "Product": r["product"],
                        "Symbol": r["sym"],
                        "Qty": r["qty_raw"],
                        "C/P & Strike": r["cp_strike"] or "",
                        "B/S": r["side"],
                        "Price": r["price"].replace("-", "'"),
                    })
    
                st.dataframe(
                    table_rows,
                    use_container_width=True,
                    hide_index=True,
                    column_order=["Time (ET/BST)", "Type", "Product", "Symbol", "Qty", "C/P & Strike", "B/S", "Price"],
                )
    
            st.divider()
            st.text_area("Copy for headline", value=full_text, height=300)
    
    

live_block_feed()

if manual_refresh:
    st.rerun()
