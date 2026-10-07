"""
paper_desk_app.py — the Paper Desk on its own page, for the fastest clicks.

The desk inside the big dashboard has to wait whenever the dashboard redraws its scanners and charts.
This page runs ONLY the desk (same account, positions, orders and settings), so a BUY / EXIT lands in ~0.1 s.

  .venv/bin/streamlit run paper_desk_app.py --server.port 8502
  → open http://localhost:8502 in its own browser tab (or on your phone on the same Wi-Fi: http://<PC-IP>:8502)

Trade from ONE place at a time (this page OR the dashboard sidebar) so two pages never write the account together.
"""
import streamlit as st

st.set_page_config(page_title="Paper Desk", page_icon="📝", layout="centered")
st.markdown("""<style>
.block-container{max-width:480px;padding:0.6rem 0.6rem 2rem}
header[data-testid="stHeader"]{display:none}
/* Dhan-style tab bar on one row */
[class*="st-key-pd_mtab"] div[data-testid="stButtonGroup"], [class*="st-key-pd_mtab"] > div > div{flex-wrap:nowrap !important}
[class*="st-key-pd_mtab"] button{padding:4px 7px !important; min-height:34px !important}
[class*="st-key-pd_mtab"] button p, [class*="st-key-pd_mtab"] button div{font-size:0.8rem !important; white-space:nowrap}
/* phone: keep button rows side by side instead of stacking */
@media (max-width: 640px){
  div[data-testid="stHorizontalBlock"]{flex-wrap:nowrap !important; gap:0.35rem !important}
  div[data-testid="stHorizontalBlock"] > div[data-testid="stColumn"]{min-width:0 !important; width:auto !important; flex:1 1 0 !important}
}
</style>""", unsafe_allow_html=True)

import paper_desk  # noqa: E402

paper_desk.render_page()
