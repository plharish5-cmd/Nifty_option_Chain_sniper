"""Backtest Lab: statistics, hold-out split, verdict rule, job lifecycle.

Nothing here runs the real strategy engine (that is covered elsewhere); these pin
the honesty of the reporting layer and the background-job plumbing.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from analyst import backtest_lab_8510 as lab  # noqa: E402


def _raw(pts, day, hm="10:00", direction="LONG", sid="EMA8_PROTOTYPE_SHORT", reason="SPOT_TARGET_HIT",
         amb=False, entry=25200.0):
    return {"st_pnl_pts": pts, "entry_day": day, "entry_hm": hm, "Direction": direction, "Index_Entry": entry,
            "Index_Exit": entry + (pts if direction == "LONG" else -pts), "strategy_id": sid, "Setup_Name": "x",
            "exit_reason": reason, "is_ambiguous": amb, "spot_target_pts": 40.0}


def test_lane_routing():
    assert lab.lane_of("EMA8_PROTOTYPE_SHORT") == "EMA8_PROTOTYPE"
    assert lab.lane_of("STAGE_2_LONG", "MJ Sir Stage 2") == "STAGE_2"
    assert lab.lane_of("STAGE_3_SHORT") == "STAGE_3"
    assert lab.lane_of("CROSSOVER_BULLISH") == "CROSSOVER"
    assert lab.lane_of("H1_8EMA_SUPPORT_LONG", "Flagship") == "FLAGSHIP"
    assert lab.lane_of("PA_SR_HAMMER", "Homework") == "PA_SR"
    assert lab.lane_of("MANUAL") == "OTHER"


def test_normalize_filters_and_shapes_rows():
    rows = normalize = lab.normalize_rows([
        _raw(40, "2026-09-01"), _raw(-40, "2026-09-01", direction="SHORT"),
        {"st_pnl_pts": None, "entry_day": "2026-09-01"},            # no points -> dropped
        {"st_pnl_pts": 10, "entry_day": "bad"},                     # bad day -> dropped
    ])
    assert len(rows) == 2
    assert rows[0]["side"] == "CE" and rows[1]["side"] == "PE"
    assert rows[0]["gross"] == 40.0 and rows[0]["lane"] == "EMA8_PROTOTYPE"


def test_stats_core_numbers():
    tr = lab.normalize_rows([_raw(40, "2026-09-01"), _raw(40, "2026-09-01"), _raw(-40, "2026-09-02"),
                             _raw(-40, "2026-09-02"), _raw(40, "2026-09-03")])
    s = lab.stats(tr, 2.0)
    assert s["trades"] == 5
    assert s["gross_pts"] == 40.0 and s["cost_pts_total"] == 10.0 and s["net_pts"] == 30.0
    assert s["wins"] == 3 and s["losses"] == 2 and s["win_rate_pct"] == 60.0
    # profit factor = gross wins / gross losses, after cost: wins 38*3=114, losses 42*2=84
    assert s["profit_factor"] == round(114.0 / 84.0, 2)
    assert s["max_drawdown_pts"] == 84.0 and s["longest_losing_run"] == 2
    assert s["days_traded"] == 3 and s["green_days"] == 2
    assert lab.stats([], 2.0)["trades"] == 0


def test_hold_out_split_never_breaks_a_day():
    days = [f"2026-09-{d:02d}" for d in range(1, 11)]
    earlier, held = lab.split_days(days, 0.3)
    assert held == days[-3:] and earlier == days[:7]
    assert lab.split_days(days, 0.0) == (days, [])
    # a trade on a held day is never in the earlier set
    alld, none = lab.split_days(days, 0.9)      # clamped to 0.8
    assert len(none) <= 8


def test_verdict_rule():
    spec = {"min_holdout_trades": 30, "pass_t_stat": 2.0}
    # too few
    assert lab.verdict({"net_pts": 10}, {"trades": 5, "net_pts": 10, "t_stat": 3}, spec)["grade"] == "NOT ENOUGH DATA"
    # held-out loses
    assert lab.verdict({"net_pts": 10}, {"trades": 40, "net_pts": -5, "t_stat": 1}, spec)["grade"] == "FAIL"
    # held-out wins strongly and earlier positive
    assert lab.verdict({"net_pts": 10}, {"trades": 40, "net_pts": 50, "t_stat": 2.5}, spec)["grade"] == "PASS"
    # held-out wins but weak t
    assert lab.verdict({"net_pts": 10}, {"trades": 40, "net_pts": 50, "t_stat": 1.0}, spec)["grade"] == "WEAK"
    # held-out wins strongly but earlier lost
    assert lab.verdict({"net_pts": -3}, {"trades": 40, "net_pts": 50, "t_stat": 3.0}, spec)["grade"] == "WEAK"


def test_build_report_structure():
    raw = []
    for i in range(80):
        pts = [40, -40, 40, -12.5, 40, -40, 40, -40][i % 8]
        raw.append(_raw(pts, f"2026-09-{(i % 20) + 1:02d}", amb=(pts == -12.5), reason="OPP" if pts == -12.5 else "SPOT_TARGET_HIT"))
    tr = lab.normalize_rows(raw)
    cov = {"from": "2026-09-01", "to": "2026-09-20", "days_with_data": 20, "weekdays_without_data": 1,
           "days": sorted({t["day"] for t in tr})}
    spec = lab.clean_spec({"from": "2026-09-01", "to": "2026-09-20", "lanes": ["EMA8_PROTOTYPE"]})
    rep = lab.build_report(tr, spec, cov)
    assert rep["all"]["trades"] == 80
    assert rep["earlier"]["trades"] + rep["held_out"]["trades"] == 80
    assert rep["verdict"]["grade"] in ("PASS", "WEAK", "FAIL", "NOT ENOUGH DATA")
    assert {m["key"] for m in rep["by_month"]} == {"2026-09"}
    assert any(r["key"] == "CE" for r in rep["by_side"])
    assert len(rep["equity"]) >= 2 and rep["equity"][-1]["i"] == 80
    assert rep["all"]["ambiguous"] == 10 and rep["all"]["net_if_ambiguous_were_targets"] is not None
    assert rep["per_lane"][0]["lane"] == "EMA8_PROTOTYPE"
    # hold-out days are marked
    assert any(d["held_out"] for d in rep["days"]) and any(not d["held_out"] for d in rep["days"])


def test_clean_spec_validation():
    ok = lab.clean_spec({"from": "2026-09-01", "to": "2026-10-01", "lanes": ["STAGE_2", "bogus"], "cost_pts": 3})
    assert ok["lanes"] == ["STAGE_2"] and ok["cost_pts"] == 3.0
    for bad, msg in (({"from": "x", "to": "y", "lanes": ["STAGE_2"]}, "From and a To"),
                     ({"from": "2026-02-01", "to": "2026-01-01", "lanes": ["STAGE_2"]}, "before"),
                     ({"from": "2026-01-01", "to": "2026-02-01", "lanes": []}, "at least one"),
                     ({"from": "2026-01-01", "to": "2026-02-01", "lanes": ["STAGE_2"], "cost_pts": 99}, "between 0 and 50"),
                     ({"from": "2026-01-01", "to": "2026-02-01", "lanes": ["STAGE_2"], "holdout_frac": 0.95}, "between 0% and 80%")):
        try:
            lab.clean_spec(bad)
            assert False, f"expected ValueError for {bad}"
        except ValueError as exc:
            assert msg in str(exc)


def test_month_chunks():
    assert lab.month_chunks(["2026-08-30", "2026-08-31", "2026-09-01", "2026-09-02"]) == [
        ("2026-08-30", "2026-08-31"), ("2026-09-01", "2026-09-02")]
    assert lab.month_chunks([]) == []


def test_job_lifecycle_with_a_fake_engine(tmp_path, monkeypatch):
    """start -> run (in-process, fake backend) -> status DONE -> result/list/cancel-after."""
    jobs = tmp_path / "jobs"
    jobs.mkdir()

    class FakeBackend:
        market_db = str(tmp_path / "market.sqlite3")

        def run_date_range_backtest(self, c_from, c_to, **kw):
            rows = [_raw([40, -40, 40, -40, -12.5][i % 5], d) for i, d in enumerate(
                [f"{c_from[:7]}-{x:02d}" for x in range(1, 21)]) for _ in range(2)]
            return {"ok": True, "trades": rows}

    # coverage/trading_days read the market db; stub them to a fixed set spanning two months
    days = [f"2026-09-{d:02d}" for d in range(1, 21)] + [f"2026-10-{d:02d}" for d in range(1, 21)]
    monkeypatch.setattr(lab, "coverage_of", lambda db, f, t: {"from": f, "to": t, "days_with_data": len(days),
                        "weekdays_without_data": 0, "missing_sample": [], "days": days})
    monkeypatch.setattr(lab, "_private_backend", lambda td: FakeBackend())

    started = lab.start_job({"from": "2026-09-01", "to": "2026-10-20", "lanes": ["EMA8_PROTOTYPE"],
                             "cost_pts": 2.0, "holdout_frac": 0.3}, jobs_dir=jobs, spawn=False)
    assert started["ok"]
    jid = started["id"]
    # a second start is refused while this one is pending
    busy = lab.start_job({"from": "2026-09-01", "to": "2026-10-20", "lanes": ["STAGE_2"]}, jobs_dir=jobs, spawn=False)
    assert not busy["ok"] and "already running" in busy["error"]

    rc = lab.run_job(lab._paths(jobs, jid)["spec"], backend=FakeBackend())
    assert rc == 0
    st = lab.job_status(jid, jobs)
    assert st["state"] == "DONE" and st["done_chunks"] == st["total_chunks"] == 2
    res = lab.job_result(jid, jobs)
    assert res["ok"] and res["report"]["all"]["trades"] > 0
    assert "trades" not in res                       # omitted unless asked
    assert "trades" in lab.job_result(jid, jobs, with_trades=True)
    rows = lab.list_jobs(jobs)
    assert rows and rows[0]["id"] == jid and rows[0]["verdict"] in ("PASS", "WEAK", "FAIL", "NOT ENOUGH DATA")
    # the result file never carries a ledger path or live-config leak
    blob = (jobs / f"{jid}.result.json").read_text(encoding="utf-8")
    assert "ledger" not in blob.lower()


def test_cancel_before_run_stops_the_job(tmp_path, monkeypatch):
    jobs = tmp_path / "jobs"
    jobs.mkdir()
    days = [f"2026-09-{d:02d}" for d in range(1, 21)]
    monkeypatch.setattr(lab, "coverage_of", lambda db, f, t: {"from": f, "to": t, "days_with_data": len(days),
                        "weekdays_without_data": 0, "missing_sample": [], "days": days})

    class B:
        market_db = str(tmp_path / "m.sqlite3")

        def run_date_range_backtest(self, *a, **k):
            return {"ok": True, "trades": []}

    started = lab.start_job({"from": "2026-09-01", "to": "2026-09-20", "lanes": ["STAGE_2"]}, jobs_dir=jobs, spawn=False)
    lab.cancel_job(started["id"], jobs)
    rc = lab.run_job(lab._paths(jobs, started["id"])["spec"], backend=B())
    assert rc == 0 and lab.job_status(started["id"], jobs)["state"] == "CANCELLED"


def test_engine_endpoints_and_tab5_removed():
    eng = (ROOT / "engine_8510_live.py").read_text(encoding="utf-8")
    for ep in ("/api/backtest_lab/meta", "/api/backtest_lab/start", "/api/backtest_lab/status",
               "/api/backtest_lab/result", "/api/backtest_lab/day", "/api/backtest_lab/cancel"):
        assert ep in eng, ep
    assert "/api/trend_lab/run" not in eng
    html = (ROOT / "Algorithmic_Trading_Workspace_8510.html").read_text(encoding="utf-8")
    assert 'id="tabTrendLab"' not in html and "128 Combos" not in html and "runTrendLabAPI" not in html
    assert 'id="btlLanes"' in html and "function btlStart" in html and "btlInit();" in html
    be = (ROOT / "analyst/terminal8510_backend.py").read_text(encoding="utf-8")
    assert "def run_trend_lab" not in be and "trend_lab_variables" not in be
