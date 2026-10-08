"""
events_cal.py — "📅 Events" sub-tab of 🧪 Options Lab + the event warnings used by the Telegram alerts.

  data/events.csv   date · time_ist · event · category · impact (CLOSED / HIGH / MEDIUM / LOW) · note
                    created on first run with the dates below — edit it in the tab (add results, budget, elections …)
  Automatic         every NIFTY weekly expiry (Tuesday; the previous trading day when Tuesday is a holiday),
                    the monthly expiry (last Tuesday), and "the day AFTER" for events that happen after Indian hours
                    (Fed decision ≈ 23:30 IST, US CPI 18:00 IST, results announced after 15:30 / on a weekend).
  Read              today's flags + the next 30 days, each with what to do (skip / cut size / wait 30 min).
Dates verified Oct 2026 from NSE holiday list (via ClearTax), RBI MPC FY27 schedule, Federal Reserve FOMC calendar,
BLS CPI schedule, company announcements (via Kotak Neo). India CPI dates are approximate (≈ 12th, 16:00 IST) —
check MoSPI. Reliance / ICICI Bank results dates were not announced yet — add them when they are.
"""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

HOLIDAYS_2026 = {
    "2026-01-15": "Maharashtra municipal elections", "2026-01-26": "Republic Day", "2026-03-03": "Holi",
    "2026-03-26": "Shri Ram Navami", "2026-03-31": "Shri Mahavir Jayanti", "2026-04-03": "Good Friday",
    "2026-04-14": "Dr. Baba Saheb Ambedkar Jayanti", "2026-05-01": "Maharashtra Day", "2026-05-28": "Bakri Id",
    "2026-06-26": "Muharram", "2026-09-14": "Ganesh Chaturthi", "2026-10-02": "Mahatma Gandhi Jayanti",
    "2026-10-20": "Dussehra", "2026-11-10": "Diwali Balipratipada", "2026-11-24": "Guru Nanak Jayanti",
    "2026-12-25": "Christmas",
}
SEED = [  # date, time_ist, event, category, impact, note
    ("2026-11-08", "evening", "Diwali Muhurat trading (special 1-hour session)", "NSE", "MEDIUM", "Thin, symbolic session — no real trading"),
    ("2026-12-04", "10:00", "RBI MPC policy decision", "RBI", "HIGH", "Rate / stance move hits banks (38% of NIFTY)"),
    ("2027-02-05", "10:00", "RBI MPC policy decision", "RBI", "HIGH", ""),
    ("2026-10-28", "23:30", "US Fed (FOMC) decision", "Global", "HIGH", "Impact = NEXT morning's gap"),
    ("2026-12-09", "23:30", "US Fed (FOMC) decision", "Global", "HIGH", "Impact = NEXT morning's gap"),
    ("2027-01-27", "23:30", "US Fed (FOMC) decision", "Global", "HIGH", "tentative date"),
    ("2026-10-14", "18:00", "US CPI (Sep)", "Global", "HIGH", "Impact = NEXT morning's gap"),
    ("2026-11-10", "18:00", "US CPI (Oct)", "Global", "HIGH", "NSE holiday in India that day — gap on 11 Nov"),
    ("2026-12-10", "18:00", "US CPI (Nov)", "Global", "HIGH", "Impact = NEXT morning's gap"),
    ("2026-10-12", "16:00", "India CPI (Sep) — approx date", "India macro", "MEDIUM", "Check MoSPI for the exact day"),
    ("2026-11-12", "16:00", "India CPI (Oct) — approx date", "India macro", "MEDIUM", "Check MoSPI for the exact day"),
    ("2026-12-14", "16:00", "India CPI (Nov) — approx date", "India macro", "MEDIUM", "Check MoSPI for the exact day"),
    ("2026-10-08", "after close", "TCS Q2 FY27 results", "Results", "MEDIUM", "IT ~9% of NIFTY — next morning"),
    ("2026-10-12", "after close", "HCLTech Q2 FY27 results", "Results", "LOW", ""),
    ("2026-10-17", "Saturday", "HDFC Bank Q2 FY27 results (10.5% of NIFTY)", "Results", "HIGH", "Weekend result → Monday 19 Oct gap"),
    ("2026-10-17", "Saturday", "Axis Bank Q2 FY27 results", "Results", "MEDIUM", "Weekend result → Monday 19 Oct gap"),
    ("2026-10-23", "after close", "Infosys Q2 FY27 results", "Results", "MEDIUM", "Next trading day gap"),
]
ACTION = {
    "CLOSED": "Market closed.",
    "EXPIRY": "Your rule: NO trading on expiry day (your biggest Dhan losses were expiry Tuesdays).",
    "HIGH": "Cut size to the minimum, no new trade 30 min before / after the event; expect a gap the next morning if "
            "it lands after 15:30.",
    "MEDIUM": "Normal size, but wait for the first 30 min after the event to settle.",
    "LOW": "Note only.",
}


def _file() -> Path:
    import config
    return Path(config.DATA_DIR) / "events.csv"


def holidays() -> set:
    return {date.fromisoformat(d) for d in HOLIDAYS_2026}


def trading_day(d: date) -> bool:
    return d.weekday() < 5 and d not in holidays()


def next_trading_day(d: date) -> date:
    x = d + timedelta(days=1)
    while not trading_day(x):
        x += timedelta(days=1)
    return x


def expiry_for_week(tue: date) -> date:
    d = tue
    while not trading_day(d):
        d -= timedelta(days=1)
    return d


def auto_events(d0: date, d1: date) -> pd.DataFrame:
    rows = []
    for h, nm in HOLIDAYS_2026.items():
        hd = date.fromisoformat(h)
        if d0 <= hd <= d1:
            rows.append((hd, "all day", f"NSE holiday — {nm}", "NSE", "CLOSED", ""))
    d = d0 - timedelta(days=d0.weekday()) + timedelta(days=1)            # Tuesday of d0's week
    while d <= d1 + timedelta(days=7):
        e = expiry_for_week(d)
        nxt = d + timedelta(days=7)
        monthly = nxt.month != d.month                                    # last Tuesday of the month
        if d0 <= e <= d1:
            rows.append((e, "15:30", ("NIFTY MONTHLY expiry (options + futures)" if monthly else "NIFTY weekly expiry")
                         + ("" if e == d else " — moved (Tuesday holiday)"), "Expiry", "EXPIRY", ""))
        d = nxt
    return pd.DataFrame(rows, columns=["date", "time_ist", "event", "category", "impact", "note"])


def load() -> pd.DataFrame:
    f = _file()
    if not f.exists():
        try:
            pd.DataFrame(SEED, columns=["date", "time_ist", "event", "category", "impact", "note"]).to_csv(f, index=False)
        except Exception:                                     # noqa: BLE001
            pass
    try:
        e = pd.read_csv(f, dtype=str).fillna("")
    except Exception:                                         # noqa: BLE001
        e = pd.DataFrame(SEED, columns=["date", "time_ist", "event", "category", "impact", "note"])
    e["date"] = pd.to_datetime(e["date"], errors="coerce").dt.date
    return e.dropna(subset=["date"])


def save(e: pd.DataFrame) -> None:
    x = e.copy()
    x["date"] = pd.to_datetime(x["date"], errors="coerce").dt.strftime("%Y-%m-%d")
    x.dropna(subset=["date"]).to_csv(_file(), index=False)


def calendar(d0: date, d1: date) -> pd.DataFrame:
    """User events + automatic ones, with the TRADING day each one hits (after-hours / weekend → next session)."""
    e = load()
    e = e[(e.date >= d0 - timedelta(days=3)) & (e.date <= d1)]
    a = auto_events(d0, d1)
    x = pd.concat([e, a], ignore_index=True)

    def hits(r):
        t = str(r.time_ist).lower()
        late = any(k in t for k in ("after", "evening", "sat", "sun", "23:", "22:", "18:", "19:", "20:", "21:", "16:"))
        if r.impact == "CLOSED" or r.category in ("Expiry", "NSE"):
            return r.date
        if late or not trading_day(r.date):
            return next_trading_day(r.date)
        return r.date

    x["trade_day"] = x.apply(hits, axis=1)
    x = x[(x.trade_day >= d0) & (x.trade_day <= d1)]
    x["action"] = x.impact.map(ACTION).fillna("")
    order = {"CLOSED": 0, "EXPIRY": 1, "HIGH": 2, "MEDIUM": 3, "LOW": 4}
    return x.assign(_o=x.impact.map(order).fillna(9)).sort_values(["trade_day", "_o"]).drop(columns="_o")


def day_flags(d: date) -> pd.DataFrame:
    return calendar(d, d)


def flags_text(d: date) -> str:
    f = day_flags(d)
    if f.empty:
        return f"📅 {d:%a %d %b}: no scheduled events — normal day."
    lines = [f"📅 <b>{d:%a %d %b}</b>"]
    for r in f.itertuples():
        when = "" if r.date == d else f" (from {r.date:%a %d %b} {r.time_ist})"
        lines.append(f"• <b>{r.impact}</b> {r.event}{when} — {r.action}")
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════════════
# Streamlit tab
# ═══════════════════════════════════════════════════════════════════════════
def render(day: date | None = None) -> None:
    import streamlit as st
    from options_lab_tab import NAVY, GOLD, WHITE, MUTED, RED

    today = date.today()
    c = st.columns([1.3, 1.3, 3])
    d0 = c[0].date_input("From", today, key="ev_from")
    days = c[1].selectbox("Show", [7, 14, 30, 60, 90], index=2, key="ev_days", format_func=lambda v: f"next {v} days")
    d1 = d0 + timedelta(days=int(days))
    cal = calendar(d0, d1)
    col = {"CLOSED": "#64748b", "EXPIRY": "#f97316", "HIGH": RED, "MEDIUM": GOLD, "LOW": MUTED}
    t = day_flags(today)
    if trading_day(today):
        hi = t[t.impact.isin(["EXPIRY", "HIGH"])]
        msg = ("<b style='color:#f97316'>⚠ TODAY: " + " · ".join(hi.event) + "</b><br>" + "<br>".join(sorted(set(hi.action))))\
            if len(hi) else "Today: no high-impact event — normal rules."
    else:
        msg = "Today the market is closed." + (f" ({t.event.iloc[0]})" if len(t) else "")
    st.markdown(f"<div style='background:{NAVY};border:1px solid {GOLD};border-radius:8px;padding:10px 14px;margin:6px 0;color:{WHITE}'>"
                f"{msg}<br><span style='color:{MUTED}'>Next trading day {next_trading_day(today):%a %d %b}</span></div>",
                unsafe_allow_html=True)
    rows = []
    last = None
    for r in cal.itertuples():
        dlab = f"{r.trade_day:%a %d %b}" if r.trade_day != last else ""
        last = r.trade_day
        src = "" if r.date == r.trade_day else f"<span style='color:{MUTED}'> (event {r.date:%d %b} {r.time_ist})</span>"
        rows.append(f"<tr><td style='padding:4px 8px;color:{GOLD};white-space:nowrap'><b>{dlab}</b></td>"
                    f"<td style='padding:4px 8px'><span style='background:{col.get(r.impact, MUTED)};color:#000;border-radius:4px;"
                    f"padding:1px 6px;font-size:0.75rem;font-weight:700'>{r.impact}</span></td>"
                    f"<td style='padding:4px 8px'>{r.event}{src}<br><span style='color:{MUTED};font-size:0.8rem'>{r.note}</span></td>"
                    f"<td style='padding:4px 8px;font-size:0.82rem'>{r.action}</td></tr>")
    st.markdown(f"<div style='background:{NAVY};border:1px solid {GOLD};border-radius:8px;padding:8px 10px;overflow-x:auto'>"
                f"<table style='color:{WHITE};width:100%;font-size:0.88rem'><tr style='color:{MUTED};text-align:left'>"
                f"<th>Trading day</th><th>Impact</th><th>Event</th><th>What to do</th></tr>{''.join(rows)}</table></div>",
                unsafe_allow_html=True)
    with st.expander("✏️ Edit your events (results, budget, elections …) — saved to data/events.csv"):
        e = load()
        ed = st.data_editor(e, num_rows="dynamic", use_container_width=True, key="ev_edit",
                            column_config={"date": st.column_config.DateColumn("date"),
                                           "impact": st.column_config.SelectboxColumn("impact", options=["HIGH", "MEDIUM", "LOW", "CLOSED"])})
        if st.button("💾 Save events", key="ev_save"):
            save(ed)
            st.success("Saved.")
    st.caption("Sources (Oct 2026): NSE holidays via ClearTax · RBI MPC FY27 schedule · Federal Reserve FOMC calendar · "
               "BLS CPI schedule · company results announcements. India CPI dates are approximate.")
