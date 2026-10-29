"""Parity tests against the original 8501 functions, using stored session data."""
from datetime import date
import json
from pathlib import Path
import sys
import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from analyst.time_series_chain_export import build
import store
import ts_chain_pro as tc
import ts_signals as signals

@pytest.fixture(scope="module")
def session():
    day = date(2026, 10, 28)
    snaps = store.load_day(day)
    assert snaps, "The parity fixture session must exist"
    return day, snaps, tc.long_frame(snaps, "03-Nov-2026")

@pytest.mark.parametrize("tf", ["1m", "3m", "5m", "15m", "1h"])
@pytest.mark.parametrize("mode", ["OI Change", "OI", "Price", "Volume"])
def test_grid_signal_and_pro_read_parity(session, tf, mode):
    day, snaps, x = session
    params = {"day": str(day), "expiry": "03-Nov-2026", "tf": tf, "mode": mode}
    out = build(params, snaps=snaps, days=[day])
    assert out["ok"]
    mult, _ = tc.share_mult(x)
    end = pd.Timestamp(out["end"])
    g = tc.grid(x, tf, mode, 6, out["selected_strikes"], mult, end)
    assert out["grid"]["cols"] == [pd.Timestamp(c).isoformat() for c in g["cols"]]
    for side in ("ce", "pe"):
        actual = np.array([[np.nan if v is None else v for v in row] for row in out["grid"][side]])
        np.testing.assert_allclose(actual, g[side].values, equal_nan=True, rtol=0, atol=0)
    sg = signals.compute(x, tc.TF[tf], end)
    assert out["signals"] == [
        dict(time=pd.Timestamp(r.ts).isoformat(), close=float(r.close),
             support=None if not pd.notna(r.sup) else float(r.sup),
             resistance=None if not pd.notna(r.res) else float(r.res),
             event=list(r.lv) if isinstance(r.lv, tuple) else None,
             action=list(r.act) if isinstance(r.act, tuple) else None, badge=list(r.badge))
        for r in sg.itertuples()]
    rd = tc.pro_read(g if mode == "OI Change" else g["chg"], out["atm"], 50, out["spot"])
    assert out["pro"]["lines"] == rd["lines"]
    assert out["pro"]["do"] == rd["do"]
    sigmap = {pd.Timestamp(r.ts): r.badge for r in sg.itertuples()}
    assert out["html"].startswith(tc.grid_html(g, mode, out["atm"], tf, {"lot_div":1,"sig":sigmap}))
    assert out["partial"] == g["partial"]
    json.dumps(out, default=str, allow_nan=False)

def test_replay_and_signal_switch(session):
    day, snaps, x = session
    end = pd.Timestamp(sorted(x.ts.unique())[len(x.ts.unique())//2])
    p = dict(day=str(day), expiry="03-Nov-2026", end=end.isoformat())
    out = build(p, snaps=snaps, days=[day])
    assert out["ok"]
    sg = signals.compute(x, 3, end)
    assert out["signals"][-1]["time"] == sg.iloc[-1].ts.isoformat()
    assert pd.Timestamp(out["now"]) <= end
    without = build(dict(p, signals=False), snaps=snaps, days=[day])
    assert without["signals"] == []
    assert "LEVEL SIGNALS" not in without["html"]
    assert without["grid"] == out["grid"]

def test_custom_and_settings_parity(session):
    day, snaps, x = session
    p = dict(day=str(day), expiry="03-Nov-2026", selection="Custom", custom=[22600,22700,22800],
             settings=dict(lot=True, full=True, day=True, pct=True, hl=False, font="Large"))
    out = build(p, snaps=snaps, days=[day])
    assert out["selected_strikes"] == p["custom"]
    g = tc.grid(x, "3m", "OI Change", 6, p["custom"], out["mult"], pd.Timestamp(out["end"]))
    sg = signals.compute(x, 3, pd.Timestamp(out["end"]))
    opts = dict(p["settings"],lot_div=tc.lot_size(),sig={pd.Timestamp(r.ts):r.badge for r in sg.itertuples()})
    assert out["html"].startswith(tc.grid_html(g,"OI Change",out["atm"],"3m",opts))

def test_custom_empty_and_invalid_controls(session):
    day, snaps, _ = session
    p = dict(day=str(day), expiry="03-Nov-2026", selection="Custom", custom=[])
    out = build(p, snaps=snaps, days=[day])
    assert not out["ok"] and "Pick at least" in out["message"] and out["candidates"]
    with pytest.raises(ValueError):
        build(dict(p, tf="2m"), snaps=snaps, days=[day])
    with pytest.raises(ValueError):
        build(dict(p, end="2026-10-29T09:15:00"), snaps=snaps, days=[day])
    invalid = build(dict(p, day="2099-01-01"), snaps=snaps, days=[day])
    assert not invalid["ok"] and invalid["sessions"] == [str(day)]

def test_no_snapshots_and_default_expiry(session):
    day, snaps, _ = session
    assert not build({}, snaps=[], days=[])["ok"]
    out = build({"day":str(day)}, snaps=snaps, days=[day])
    assert out["ok"] and out["expiry"] == "03-Nov-2026"

def test_call_put_mirror_and_source_sections(session):
    day, snaps, _ = session
    out = build(dict(day=str(day), expiry="03-Nov-2026"), snaps=snaps, days=[day])
    for name in ("LEVEL SIGNALS","PRO READ","BUCKET-BY-BUCKET NARRATIVE","WHAT TO DO WITH IT","How to read"):
        assert name in out["html"]
    assert out["grid"]["labels"] == [n["label"].replace(" ⏳","") for n in out["narrative"]]
    assert "⏳" in out["html"]

def test_full_session_window_includes_morning_and_close(session):
    day, snaps, _ = session
    p = dict(day=str(day), expiry="03-Nov-2026")
    all_day = build(dict(p, columns=0), snaps=snaps, days=[day])
    recent = build(p, snaps=snaps, days=[day])
    assert all_day["ok"] and len(all_day["grid"]["cols"]) > 12
    assert all_day["grid"]["labels"][0] < "10:00"
    assert all_day["grid"]["labels"][-1] >= "15:30"
    for side in ("ce","pe"):
        assert [row[-6:] for row in all_day["grid"][side]] == recent["grid"][side]
    assert all_day["times"][0][11:16] == "09:15"

def test_voice_event_feed_is_independent_of_grid_signal_switch(session):
    day, snaps, _ = session
    p = dict(day=str(day), expiry="03-Nov-2026")
    on = build(p, snaps=snaps, days=[day])
    off = build(dict(p, signals=False), snaps=snaps, days=[day])
    assert off["signals"] == []
    assert off["alert_signals"] == on["alert_signals"] == on["signals"]
    assert "LEVEL SIGNALS" not in off["html"]

def test_opening_baseline_replay_and_late_recordings(session):
    day, snaps, x = session
    first = pd.Timestamp(sorted(x[x.ts>=pd.Timestamp(str(day)+" 09:15:00")].ts.unique())[0])
    out = build(dict(day=str(day), expiry="03-Nov-2026", end=first.isoformat(), columns=0), snaps=snaps, days=[day])
    assert out["ok"] and pd.Timestamp(out["now"]) == first
    assert all(pd.Timestamp(r["time"]) <= first.ceil("min") for r in out["alert_signals"])
