"""
replay_desk_app.py — ⏪ Replay Trading: practise a PAST session on the Paper Desk at 1×–30× speed.

  .venv/bin/streamlit run replay_desk_app.py --server.port 8505                      → your replay account
  REPLAY_FOR=wife .venv/bin/streamlit run replay_desk_app.py --server.port 8506      → your wife's replay account
  phone (Tailscale): http://100.113.93.21:8505   (once: sudo ufw allow in on tailscale0 to any port 8505)
  report:  .venv/bin/python trade_report.py --profile replay        (wife: --profile replay_wife)

Uses a SEPARATE account (profile "replay" / "replay_wife") — your live paper P&L and readiness checklist are untouched.
"""
import os

_who = (os.environ.get("REPLAY_FOR") or "").strip().lower()
os.environ["PAPER_PROFILE"] = "replay" + (f"_{_who}" if _who and _who != "harish" else "")

import streamlit as st  # noqa: E402

st.set_page_config(page_title="Replay Desk", page_icon="⏪", layout="centered")
st.markdown("""<style>
.block-container{max-width:520px;padding:0.6rem 0.6rem 2rem}
header[data-testid="stHeader"]{display:none}
[class*="st-key-pd_mtab"] div[data-testid="stButtonGroup"], [class*="st-key-pd_mtab"] > div > div{flex-wrap:nowrap !important}
[class*="st-key-pd_mtab"] button{padding:4px 7px !important; min-height:34px !important}
[class*="st-key-pd_mtab"] button p, [class*="st-key-pd_mtab"] button div{font-size:0.8rem !important; white-space:nowrap}
@media (max-width: 640px){
  div[data-testid="stHorizontalBlock"]{flex-wrap:nowrap !important; gap:0.35rem !important}
  div[data-testid="stHorizontalBlock"] > div[data-testid="stColumn"]{min-width:0 !important; width:auto !important; flex:1 1 0 !important}
}
</style>""", unsafe_allow_html=True)

import paper_desk  # noqa: E402
import replay_desk  # noqa: E402

replay_desk.install(paper_desk)
st.markdown(paper_desk.CSS, unsafe_allow_html=True)
replay_desk.controls(st)
frag = getattr(st, "fragment", None)


def _chart():
    replay_desk.status(st)
    replay_desk.chart(st)


st.radio("Candles", ["3m", "5m", "15m"], index=1, horizontal=True, key="rp_tf", label_visibility="collapsed")
if frag:
    frag(run_every=3)(_chart)()
    s = paper_desk.load_state()
    frag(run_every=int(s.get("refresh_s", 1)))(lambda: paper_desk.desk_body(mobile=True))()
else:
    _chart()
    paper_desk.desk_body(mobile=True)
