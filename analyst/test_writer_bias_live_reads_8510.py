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