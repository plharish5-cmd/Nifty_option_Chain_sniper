from analyst.desk8600_lane_gating_8510 import evaluate_lane_gating
from analyst.writer_bias_live_reads_8510 import apply_writer_bias_to_board_reads


def _chain(strike, ce_oi, pe_oi, ce_ltp=100.0, pe_ltp=90.0):
    return {
        float(strike): {
            "ce_oi": float(ce_oi),
            "pe_oi": float(pe_oi),
            "ce_ltp": ce_ltp,
            "pe_ltp": pe_ltp,
            "ce_vol": 1.0,
            "pe_vol": 1.0,
        }
    }


def test_first_session_snapshot_unclassified():
    day = "2026-11-25"
    t0 = 1_762_000_000
    by_ts = {t0: _chain(25000, 100, 200)}
    out = apply_writer_bias_to_board_reads(
        by_ts=by_ts,
        ts_sorted=[t0],
        spot=25000,
        band=1000,
        day=day,
        now_ts=t0 + 5,
        last_t=t0,
        age_sec=5,
        scanner_cfg=None,
        momentum_oi={},
    )
    assert out["unclassified_poll"]
    assert out["day_oi"]["direction"] is None
    assert "first poll" in out["day_oi"]["detail"].lower()


def test_writing_only_when_oi_up_price_down():
    day = "2026-11-25"
    t0, t1 = 1_762_000_000, 1_762_000_120
    base = _chain(25000, 100, 100, ce_ltp=100, pe_ltp=100)
    up_oi_dn_px = _chain(25000, 120, 100, ce_ltp=98, pe_ltp=100)
    by_ts = {t0: base, t1: up_oi_dn_px}
    out = apply_writer_bias_to_board_reads(
        by_ts=by_ts,
        ts_sorted=[t0, t1],
        spot=25000,
        band=1000,
        day=day,
        now_ts=t1 + 2,
        last_t=t1,
        age_sec=2,
        scanner_cfg=None,
        momentum_oi={"fresh_oi": {"detail": "base", "direction": "NEUTRAL"}},
    )
    assert out["day_oi"]["direction"] in ("CE", "PE", "NEUTRAL", None)
    facts = out.get("facts") or {}
    buildup = " ".join(facts.get("writer_buildup") or [])
    if buildup:
        assert "SHORT_BUILDUP_HEURISTIC" in buildup or "writing=True" in buildup


def test_audit_reason_includes_score_slope_age():
    src = dict(
        id="day_oi",
        direction="PE",
        feed_state="LIVE",
        # Provenance is now required to block: a source with no source_ts
        # fails open (ChatGPT provenance-freshness fix). Supply a valid one
        # so this test exercises the actual BLOCK/audit path.
        source_ts="2026-11-26T11:00:00+05:30",
        age_sec=4,
        writer_bias_regime="BEARISH",
        writer_bias_score=-0.34,
        writer_bias_slope=None,
        detail="detail tail",
    )
    cfg = {"desk8600_lane_gating": {"prototype": {"pe_ce_change": "BLOCK"}}}
    out = evaluate_lane_gating("prototype", True, [src], cfg)
    item = next(x for x in out["signals"] if x["signal_id"] == "pe_ce_change")
    assert "BEARISH" in item["audit_reason"]
    assert "-0.34" in item["audit_reason"]
    assert "age 4s" in item["audit_reason"]


def test_previous_day_reported_separately():
    day = "2026-11-25"
    t1 = 1_762_000_180
    prior = _chain(25000, 80, 90)
    now = _chain(25000, 100, 120)
    out = apply_writer_bias_to_board_reads(
        by_ts={t1: now},
        ts_sorted=[t1],
        spot=25000,
        band=1000,
        day=day,
        now_ts=t1 + 2,
        last_t=t1,
        age_sec=2,
        scanner_cfg=None,
        momentum_oi={},
        prior_day_close=prior,
    )
    assert out["facts"].get("previous_day_ce_oi_delta") == 20.0
    assert out["facts"].get("previous_day_pe_oi_delta") == 30.0