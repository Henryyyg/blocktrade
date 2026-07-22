"""
Block Trade Headlines — auto-refreshing Streamlit app.

Setup: see gmail_api_setup.md. Requires credentials.json + token.json in this folder.
Run: streamlit run app.py
"""
import time
import streamlit as st
from gmail_poll import get_all_headlines

st.set_page_config(page_title="Block Trade Headlines", layout="wide")

REFRESH_SECONDS = 60
HOURS_BACK = 24

st.title("Block Trade Headlines")

# --- Sidebar controls ---
with st.sidebar:
    st.header("Settings")
    hours_back = st.number_input("Look back (hours)", min_value=1, max_value=72, value=HOURS_BACK)
    refresh_seconds = st.number_input("Auto-refresh every (seconds)", min_value=15, max_value=600, value=REFRESH_SECONDS)
    manual_refresh = st.button("Refresh now")
    st.caption("Auto-refreshing. Turn this tab's auto-refresh off by closing it — no data is lost, it just re-polls on reopen.")

# --- Session state for tracking seen emails ---
if "seen_ids" not in st.session_state:
    st.session_state.seen_ids = set()
if "last_headlines" not in st.session_state:
    st.session_state.last_headlines = {}
if "last_checked" not in st.session_state:
    st.session_state.last_checked = None

# --- Fetch ---
error = None
try:
    headlines, new_ids, all_ids = get_all_headlines(
        hours_back=hours_back,
        seen_ids=st.session_state.seen_ids
    )
    st.session_state.last_headlines = headlines
    st.session_state.seen_ids.update(all_ids)
    st.session_state.last_checked = time.strftime("%H:%M:%S")
    if new_ids:
        st.toast(f"{len(new_ids)} new block trade email(s) found", icon="📬")
except FileNotFoundError:
    error = "credentials.json or token.json not found. Follow gmail_api_setup.md first."
except Exception as e:
    error = f"Error fetching from Gmail: {e}"

if error:
    st.error(error)
else:
    st.caption(f"Last checked: {st.session_state.last_checked} · looking back {hours_back}h")

    headlines = st.session_state.last_headlines
    if not headlines:
        st.info("No block trades found in this window yet.")
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

        st.text_area("Copy for client email", value=full_text, height=300)

        st.divider()

        for category, items in headlines.items():
            st.subheader(category)
            for h in items:
                st.markdown(f"- {h['line']}")

# --- Auto-refresh loop ---
if not manual_refresh:
    time.sleep(refresh_seconds)
    st.rerun()
else:
    st.rerun()
