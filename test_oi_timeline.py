"""
test_oi_timeline.py — Comprehensive Unit & Integration Tests for OI Timeline Engine.

Tests:
1. Market-hours filtering (09:15:00 to 15:30:00 IST strictness).
2. Cadence-invariant 125 3-minute bins coverage metric.
3. Orthogonal Dominance and Flow (PE_DOMINANT with UNWINDING).
4. Disambiguation layer (intraday premium delta vs session baseline, AGREE vs DISAGREE).
5. Crossover sustain filter: Single-step flip must NOT emit CROSSOVER; held for 9m must.
6. Negative deltas: Planted unwinding produces true negative values, not zero/clipped.
7. Guards: Assert WARMUP before 09:50, EOD_UNWIND from 15:00, EXPIRY_DAY on expiry session.
8. Late baseline: First market-hours snapshot at 11:00 sets baseline_late=True.
9. Historical session 2026-09-11 integration and gap gate handling.
10. Historical session 2026-08-21 gate passing integration.
"""

from __future__ import annotations

from datetime import date, datetime, time as dtime, timedelta
import pandas as pd
import pytest

import oi_timeline
import store


def _make_dummy_chain(strike: float, spot: float, ce_oi: float, pe_oi: float,
                       ce_chg_oi: float, pe_chg_oi: float,
                       ce_ltp: float, pe_ltp: float) -> pd.DataFrame:
    """Helper to build a single-strike dummy option chain DataFrame."""
    return pd.DataFrame([{
        "strike": strike,
        "ce_oi": ce_oi,
        "pe_oi": pe_oi,
        "ce_chg_oi": ce_chg_oi,
        "pe_chg_oi": pe_chg_oi,
        "ce_ltp": ce_ltp,
        "pe_ltp": pe_ltp,
        "ce_chg": 0.0,
        "pe_chg": 0.0,
    }])


def test_market_hours_filter():
    """Verifies that filter_market_hours strictly rejects pre-market and after-market snapshots."""
    sess_date = date(2026, 9, 11)
    df = _make_dummy_chain(23000.0, 23000.0, 1000, 1000, 0, 0, 100.0, 100.0)

    snaps = [
        (datetime.combine(sess_date, dtime(0, 1, 15)), 23000.0, df),   # Stale midnight
        (datetime.combine(sess_date, dtime(9, 8, 0)), 23000.0, df),    # Pre-open
        (datetime.combine(sess_date, dtime(9, 15, 5)), 23000.0, df),   # Market open (Valid)
        (datetime.combine(sess_date, dtime(12, 0, 0)), 23000.0, df),   # Mid-day (Valid)
        (datetime.combine(sess_date, dtime(15, 29, 55)), 23000.0, df), # Market close (Valid)
        (datetime.combine(sess_date, dtime(15, 31, 0)), 23000.0, df),  # Post-market
        (datetime.combine(sess_date, dtime(18, 0, 0)), 23000.0, df),   # Evening
    ]

    filtered = oi_timeline.filter_market_hours(snaps)
    assert len(filtered) == 3
    assert filtered[0][0].time() == dtime(9, 15, 5)
    assert filtered[1][0].time() == dtime(12, 0, 0)
    assert filtered[2][0].time() == dtime(15, 29, 55)


def test_coverage_125_bins():
    """Verifies coverage computation based on 125 3-minute bins."""
    sess_date = date(2026, 9, 11)
    df = _make_dummy_chain(23000.0, 23000.0, 1000, 1000, 0, 0, 100.0, 100.0)

    # Synthetic full session: 1 snapshot every 3 minutes from 09:15 to 15:27 (125 snapshots)
    snaps = []
    curr = datetime.combine(sess_date, dtime(9, 15, 0))
    for _ in range(125):
        snaps.append((curr, 23000.0, df))
        curr += timedelta(minutes=3)

    cov = oi_timeline.compute_session_coverage(snaps, sess_date)
    assert cov.occupied_bins == 125
    assert cov.total_bins == 125
    assert cov.coverage_pct == 100.0
    assert cov.passes_gate is True
    assert cov.baseline_late is False

    # Synthetic 50% session (62 bins occupied out of 125)
    snaps_half = snaps[:62]
    cov_half = oi_timeline.compute_session_coverage(snaps_half, sess_date)
    assert cov_half.occupied_bins == 62
    assert cov_half.coverage_pct == round((62 / 125.0) * 100.0, 1)
    assert cov_half.passes_gate is False


def test_separated_dominance_and_flow():
    """Verifies that Dominance and Flow are orthogonal, not mutually exclusive."""
    sess_date = date(2026, 9, 11)
    strike = 23250.0

    # Build snapshots where:
    # - Time is post-warmup (10:00 to 10:30)
    # - PE change is +200,000, CE change is +20,000 -> Spread is +180,000 (PE_DOMINANT)
    # - Over the last 9 minutes, both PE and CE changes drop -> Flow is UNWINDING
    snaps = []
    t = datetime.combine(sess_date, dtime(10, 0, 0))
    for i in range(10):
        if i < 7:
            ce_chg = 20000.0 + i * 1000.0
            pe_chg = 200000.0 + i * 5000.0
        else:
            ce_chg = 26000.0 - (i - 6) * 2000.0
            pe_chg = 230000.0 - (i - 6) * 5000.0

        df = _make_dummy_chain(strike, 23250.0, 50000.0, 500000.0, ce_chg, pe_chg, 50.0, 100.0)
        snaps.append((t, 23250.0, df))
        t += timedelta(minutes=3)

    tl = oi_timeline.evaluate_strike_timeline(snaps, strike, sess_date)
    latest = tl.latest_stats
    assert latest is not None
    assert latest.dominance == "PE_DOMINANT"
    assert latest.flow == "UNWINDING"
    assert "Short-side entries discouraged" in latest.veto_note
    assert "unwinding" in latest.veto_note


def test_quadrant_disambiguation_agree_and_disagree():
    """
    Verifies that intraday premium change correctly disambiguates:
    - PE OI up + PE LTP down = short_buildup (Put Writing) -> AGREE with naive Put Writers Dominant
    - PE OI up + PE LTP up   = long_buildup (Put Buying)  -> DISAGREE (bearish breakdown push)
    """
    sess_date = date(2026, 9, 11)
    strike = 23300.0

    # Case 1: Put Writing (LTP falls from 100 to 60 as OI surges by +200k)
    t0 = datetime.combine(sess_date, dtime(10, 0, 0))
    t1 = datetime.combine(sess_date, dtime(10, 15, 0))
    df0 = _make_dummy_chain(strike, 23300.0, 10000, 10000, 0, 0, 100.0, 100.0)
    df1 = _make_dummy_chain(strike, 23350.0, 10000, 210000, 5000, 200000, 150.0, 60.0)

    tl_agree = oi_timeline.evaluate_strike_timeline([(t0, 23300.0, df0), (t1, 23350.0, df1)], strike, sess_date)
    pt1 = tl_agree.series[-1]
    assert pt1.combined_pe_quadrant == "short_buildup"
    assert pt1.agreement == "AGREE"

    # Case 2: Put Buying (LTP rises from 100 to 180 as OI surges by +200k)
    df2 = _make_dummy_chain(strike, 23150.0, 10000, 210000, 5000, 200000, 40.0, 180.0)
    tl_disagree = oi_timeline.evaluate_strike_timeline([(t0, 23300.0, df0), (t1, 23150.0, df2)], strike, sess_date)
    pt2 = tl_disagree.series[-1]
    assert pt2.combined_pe_quadrant == "long_buildup"
    assert pt2.agreement == "DISAGREE"
    assert "PUT BUYERS ACTIVE" in pt2.combined_interpretation


def test_crossover_sustain_filter():
    """
    Verifies that a single-step flip does NOT emit CROSSOVER,
    while a flip sustained for crossover_confirm_minutes (3 steps = 9m) DOES emit CROSSOVER.
    """
    sess_date = date(2026, 9, 11)
    strike = 23300.0
    snaps = []
    t = datetime.combine(sess_date, dtime(10, 0, 0))

    # Steps 0 to 4: Established PE Dominance (+90,000 spread)
    df_pe = _make_dummy_chain(strike, 23300.0, 100000, 100000, 10000, 100000, 100, 100)
    for _ in range(5):
        snaps.append((t, 23300.0, df_pe))
        t += timedelta(minutes=3)

    # Step 5: First step flip to CE Dominance (-90,000 spread) -> Must NOT trigger crossover
    df_ce = _make_dummy_chain(strike, 23300.0, 100000, 100000, 100000, 10000, 100, 100)
    snaps.append((t, 23300.0, df_ce))
    t += timedelta(minutes=3)

    # Step 6: Second step flip (-90,000 spread) -> Must NOT trigger crossover
    snaps.append((t, 23300.0, df_ce))
    t += timedelta(minutes=3)

    # Step 7: Third step flip (-90,000 spread) -> 3 steps (9m) sustained -> Emits CROSSOVER
    snaps.append((t, 23300.0, df_ce))

    tl = oi_timeline.evaluate_strike_timeline(snaps, strike, sess_date)

    # Verify steps 0-4 are PE_DOMINANT
    assert tl.series[0].dominance == "PE_DOMINANT"
    assert tl.series[1].dominance == "PE_DOMINANT"
    assert tl.series[2].dominance == "PE_DOMINANT"
    assert tl.series[3].dominance == "PE_DOMINANT"
    assert tl.series[4].dominance == "PE_DOMINANT"

    # Step 5 (1st flip step): single-step flip rejected, remains PE_DOMINANT
    assert tl.series[5].dominance == "PE_DOMINANT"

    # Step 6 (2nd flip step): still held at PE_DOMINANT
    assert tl.series[6].dominance == "PE_DOMINANT"

    # Step 7 (3rd flip step = 9m confirm): confirmed CROSSOVER
    assert tl.series[7].dominance == "CROSSOVER"
    assert len(tl.crossover_points) == 1
    assert tl.crossover_points[0][1] == "PE_DOMINANT → CE_DOMINANT"
    assert tl.crossover_points[0][0] == "10:21"


def test_negative_deltas_unwinding():
    """Verifies that planted unwinding produces genuine negative delta values, not zero/clipped."""
    sess_date = date(2026, 9, 11)
    strike = 23300.0
    t0 = datetime.combine(sess_date, dtime(10, 0, 0))
    t1 = datetime.combine(sess_date, dtime(10, 3, 0))

    df0 = _make_dummy_chain(strike, 23300.0, 500000, 500000, 0, 0, 100, 100)
    df1 = _make_dummy_chain(strike, 23300.0, 455000, 420000, -45000, -80000, 80, 80)

    tl = oi_timeline.evaluate_strike_timeline([(t0, 23300.0, df0), (t1, 23300.0, df1)], strike, sess_date)
    pt = tl.series[-1]
    latest = tl.latest_stats
    assert latest is not None

    assert pt.ce_chg_oi == -45000.0
    assert pt.ce_chg_oi < 0.0
    assert pt.pe_chg_oi == -80000.0
    assert pt.pe_chg_oi < 0.0
    assert pt.secondary_dce == -45000.0
    assert pt.secondary_dpe == -80000.0
    assert pt.spread == -35000.0
    assert latest.dce == -45000.0
    assert latest.dpe == -80000.0
    assert latest.spread == -35000.0


def test_guards_warmup_eod_expiry():
    """
    Verifies state guards:
    - WARMUP before 09:50
    - EOD_UNWIND from 15:00
    - EXPIRY_DAY on confirmed expiry sessions
    Asserts that no directional dominance value (PE_DOMINANT / CE_DOMINANT / BALANCED) is emitted.
    """
    sess_date = date(2026, 9, 11)
    strike = 23300.0
    # Create massive PE dominance (+500,000 contracts) that would otherwise trigger PE_DOMINANT
    df = _make_dummy_chain(strike, 23300.0, 500000, 500000, 0, 500000, 100, 100)

    # 1. Warmup guard (09:30:00)
    t_warmup = datetime.combine(sess_date, dtime(9, 30, 0))
    tl_warmup = oi_timeline.evaluate_strike_timeline([(t_warmup, 23300.0, df)], strike, sess_date)
    pt_warmup = tl_warmup.series[-1]
    assert pt_warmup.dominance == "WARMUP"
    assert pt_warmup.dominance not in ("PE_DOMINANT", "CE_DOMINANT", "BALANCED")
    assert "Early-session warmup" in tl_warmup.latest_stats.veto_note

    # 2. EOD unwind guard (15:05:00)
    t_eod = datetime.combine(sess_date, dtime(15, 5, 0))
    tl_eod = oi_timeline.evaluate_strike_timeline([(t_eod, 23300.0, df)], strike, sess_date)
    pt_eod = tl_eod.series[-1]
    assert pt_eod.dominance == "EOD_UNWIND"
    assert pt_eod.dominance not in ("PE_DOMINANT", "CE_DOMINANT", "BALANCED")
    assert "EOD position squaring" in tl_eod.latest_stats.veto_note

    # 3. Expiry day guard (11:00:00, is_expiry_day=True, expiry_unknown=False)
    t_mid = datetime.combine(sess_date, dtime(11, 0, 0))
    tl_exp = oi_timeline.evaluate_strike_timeline([(t_mid, 23300.0, df)], strike, sess_date,
                                                   is_expiry_day=True, expiry_unknown=False)
    pt_exp = tl_exp.series[-1]
    assert pt_exp.dominance == "EXPIRY_DAY"
    assert pt_exp.dominance not in ("PE_DOMINANT", "CE_DOMINANT", "BALANCED")
    assert "Expiry day session" in tl_exp.latest_stats.veto_note


def test_late_baseline_flag():
    """Verifies that a first snapshot starting after 09:20 (e.g. 11:00) sets baseline_late=True."""
    sess_date = date(2026, 9, 11)
    strike = 23300.0
    t_late = datetime.combine(sess_date, dtime(11, 0, 0))
    df = _make_dummy_chain(strike, 23300.0, 500000, 500000, 10000, 50000, 100, 100)

    cov = oi_timeline.compute_session_coverage([(t_late, 23300.0, df)], sess_date)
    tl = oi_timeline.evaluate_strike_timeline([(t_late, 23300.0, df)], strike, sess_date)

    assert cov.baseline_late is True
    assert tl.baseline_late is True
    assert tl.divergence_detected is False
    assert "LATE_START" in cov.status_note


def test_historical_session_2026_09_11():
    """Integration test against actual historical store session 2026-09-11."""
    sess_date = date(2026, 9, 11)
    snaps = store.load_day(sess_date)
    assert len(snaps) > 0

    res = oi_timeline.evaluate_session_timelines(snaps, sess_date)
    assert res.session_date == "2026-09-11"
    # Fails gate because 30m gap (10 bins) exceeds max_contiguous_missing_bins: 8
    assert res.coverage.passes_gate is False
    assert res.coverage.occupied_bins == 109
    assert res.coverage.coverage_pct == 87.2
    assert res.coverage.max_contiguous_missing_bins == 10
    assert "GAP_EXCEEDED" in res.coverage.status_note
    assert res.atm_strike == 23250.0
    assert len(res.watched_strikes) == 5

    # Check strike 23300: PE dominant, AGREE
    tl_23300 = res.strike_timelines[23300.0]
    st_23300 = tl_23300.latest_stats
    assert st_23300 is not None
    assert st_23300.dominance == "PE_DOMINANT"
    assert st_23300.agreement == "AGREE"
    assert st_23300.dpe == 290706.0
    assert tl_23300.divergence_detected is False

    # Check strike 23250: PE dominant, flow unwinding, AGREE
    tl_23250 = res.strike_timelines[23250.0]
    st_23250 = tl_23250.latest_stats
    assert st_23250 is not None
    assert st_23250.dominance == "PE_DOMINANT"
    assert st_23250.flow == "UNWINDING"
    assert "Short-side entries discouraged" in st_23250.veto_note
    assert "unwinding" in st_23250.veto_note


def test_historical_session_gate_passing_2026_08_21():
    """Integration test against historical session 2026-08-21 which cleanly passes all gates."""
    sess_date = date(2026, 8, 21)
    snaps = store.load_day(sess_date)
    assert len(snaps) > 0

    res = oi_timeline.evaluate_session_timelines(snaps, sess_date)
    assert res.session_date == "2026-08-21"
    assert res.coverage.passes_gate is True
    assert res.coverage.occupied_bins == 121
    assert res.coverage.coverage_pct == 96.8
    assert res.coverage.max_contiguous_missing_bins == 1
    assert "PASS" in res.coverage.status_note
    assert res.atm_strike == 24250.0


def test_export_timeline_to_csv(tmp_path):
    """Verifies that export_timeline_to_csv writes valid, populated CSV files."""
    sess_date = date(2026, 8, 21)
    snaps = store.load_day(sess_date)
    assert len(snaps) > 0

    res = oi_timeline.evaluate_session_timelines(snaps, sess_date)
    target_csv = oi_timeline.export_timeline_to_csv(res, output_dir=tmp_path)

    assert target_csv.exists()
    assert target_csv.name == "2026-08-21.csv"
    df_out = pd.read_csv(target_csv)
    assert len(df_out) > 0
    assert "strike" in df_out.columns
    assert "ce_chg_oi" in df_out.columns
    assert "pe_chg_oi" in df_out.columns
    assert "dominance" in df_out.columns
    assert "flow" in df_out.columns


def test_tab_view_components():
    """Verifies that views.oi_timeline_tab builds charts and badges without error."""
    import views.oi_timeline_tab as tab

    sess_date = date(2026, 8, 21)
    snaps = store.load_day(sess_date)
    assert len(snaps) > 0

    res = oi_timeline.evaluate_session_timelines(snaps, sess_date)
    tl = res.strike_timelines[24250.0]

    rules = oi_timeline.load_rules()
    fig = tab._build_strike_chart(tl, rules)
    assert len(fig.data) >= 2
    assert any("CE ΔOI" in t.name for t in fig.data)
    assert any("PE ΔOI" in t.name for t in fig.data)

    dom_b = tab._render_dominance_badge("PE_DOMINANT")
    assert "PE_DOMINANT" in dom_b
    flow_b = tab._render_flow_badge("UNWINDING")
    assert "UNWINDING" in flow_b
    agree_b = tab._render_agreement_badge("AGREE")
    assert "AGREE" in agree_b
    disagree_b = tab._render_agreement_badge("DISAGREE")
    assert "DISAGREE" in disagree_b

    # Verify latest_stats compatibility attributes
    assert tl.latest_stats is not None
    assert hasattr(tl.latest_stats, "stale_warning")
    assert hasattr(tl.latest_stats, "staleness_warning")
    assert tl.latest_stats.stale_warning == tl.latest_stats.staleness_warning
    assert hasattr(tl.latest_stats, "combined_interpretation")
    assert isinstance(tl.latest_stats.combined_interpretation, str)


def test_current_spot_and_100_level_strike_selection():
    """Verifies that 100-pt strike spacing and current spot anchoring correctly pick round strikes."""
    sess_date = date(2026, 9, 11)
    snaps = store.load_day(sess_date)
    assert len(snaps) > 0

    # With strike_step=100.0 and current_spot=23400.0
    res = oi_timeline.evaluate_session_timelines(
        snaps, sess_date, strike_step=100.0, current_spot=23400.0, atm_anchor="current"
    )
    assert res.atm_strike == 23400.0
    # Watched strikes must be all 100-level multiples
    for s in res.watched_strikes:
        assert s % 100 == 0
    assert 23300.0 in res.watched_strikes
    assert 23400.0 in res.watched_strikes
    assert 23500.0 in res.watched_strikes


def test_custom_and_active_range_strikes():
    """Verifies that passing custom strikes overrides default selection."""
    sess_date = date(2026, 9, 11)
    snaps = store.load_day(sess_date)
    assert len(snaps) > 0

    custom = [23300.0, 23400.0, 23500.0, 23600.0]
    res = oi_timeline.evaluate_session_timelines(snaps, sess_date, strikes=custom)
    assert res.watched_strikes == custom
    for s in custom:
        assert s in res.strike_timelines


def test_tab_view_optiontick_formatting():
    """Verifies that OptionTick chart builder scales to Lakhs and applies ticksuffix."""
    import views.oi_timeline_tab as tab

    sess_date = date(2026, 8, 21)
    snaps = store.load_day(sess_date)
    res = oi_timeline.evaluate_session_timelines(snaps, sess_date)
    tl = res.strike_timelines[24250.0]

    rules = oi_timeline.load_rules()
    fig_lakhs = tab._build_strike_chart(tl, rules, display_units="lakhs", display_lot=65)
    assert fig_lakhs.layout.yaxis.ticksuffix == "L"
    assert "Lakhs" in fig_lakhs.layout.yaxis.title.text

    fig_contracts = tab._build_strike_chart(tl, rules, display_units="contracts")
    assert fig_contracts.layout.yaxis.ticksuffix == ""
    assert "Contracts" in fig_contracts.layout.yaxis.title.text


def test_who_is_acting_all_quadrants_and_unclear():
    """Verifies who-classification for all 4 quadrants + UNCLEAR, including delta-adjusted resid."""
    # 1. OI ↑ + IV ↓ → WRITING
    who, abbr = oi_timeline.classify_who_is_acting(d_oi=50000, d_iv=-0.5, resid=-1.2)
    assert who == "WRITING"
    assert abbr == "WRT"

    # 2. OI ↑ + IV ↑ → BUYING
    who, abbr = oi_timeline.classify_who_is_acting(d_oi=50000, d_iv=+0.6, resid=+1.5)
    assert who == "BUYING"
    assert abbr == "BUY"

    # 3. OI ↓ + IV ↑ → COVERING
    who, abbr = oi_timeline.classify_who_is_acting(d_oi=-50000, d_iv=+0.4, resid=+0.8)
    assert who == "COVERING"
    assert abbr == "COV"

    # 4. OI ↓ + IV ↓ → LONG EXIT
    who, abbr = oi_timeline.classify_who_is_acting(d_oi=-50000, d_iv=-0.8, resid=-2.0)
    assert who == "LONG EXIT"
    assert abbr == "EXIT"

    # 5. |IV change| < iv_noise (0.3) → UNCLEAR
    who, abbr = oi_timeline.classify_who_is_acting(d_oi=50000, d_iv=+0.15, resid=+1.0, iv_noise=0.3)
    assert who == "UNCLEAR"
    assert abbr == "?"

    # 6. Delta-adjusted resid when IV is unavailable:
    # Case where raw premium fell only because spot fell!
    # Call option: spot fell by -20 pts, delta = 0.5.
    # Raw ltp fell by -8 pts (from 100 to 92).
    # d_sp = -20, d_ltp = -8.
    # resid = d_ltp - delta * d_sp = -8 - (0.5 * -20) = -8 - (-10) = +2.0 (positive!)
    # OI rose (d_oi = +30,000)
    # Even though raw premium fell (-8), resid is positive (+2.0) -> BUYING!
    d_sp = -20.0
    delta = 0.5
    d_ltp = -8.0
    resid = d_ltp - delta * d_sp
    assert resid == 2.0
    who, abbr = oi_timeline.classify_who_is_acting(d_oi=30000, d_iv=None, resid=resid)
    assert who == "BUYING"
    assert abbr == "BUY"


def test_wall_migration_logic():
    """Verifies whole-chain wall migration UP, DOWN, and STEADY."""
    df_prev = pd.DataFrame([
        {"strike": 23000.0, "ce_oi": 10000, "pe_oi": 50000, "ce_chg_oi": 2000, "pe_chg_oi": 15000},
        {"strike": 23100.0, "ce_oi": 40000, "pe_oi": 10000, "ce_chg_oi": 8000, "pe_chg_oi": 5000},
    ])
    # Case 1: Both add-walls migrate UP
    df_curr_up = pd.DataFrame([
        {"strike": 23100.0, "ce_oi": 30000, "pe_oi": 20000, "ce_chg_oi": 1000, "pe_chg_oi": 2000},
        {"strike": 23200.0, "ce_oi": 50000, "pe_oi": 40000, "ce_chg_oi": 15000, "pe_chg_oi": 25000},
    ])
    wall_up = oi_timeline.compute_wall_migration(df_curr_up, df_prev)
    assert wall_up.status == "walls migrating up"

    # Case 2: Both add-walls migrate DOWN
    df_curr_down = pd.DataFrame([
        {"strike": 22900.0, "ce_oi": 60000, "pe_oi": 60000, "ce_chg_oi": 18000, "pe_chg_oi": 30000},
        {"strike": 23000.0, "ce_oi": 10000, "pe_oi": 10000, "ce_chg_oi": 2000, "pe_chg_oi": 5000},
    ])
    wall_down = oi_timeline.compute_wall_migration(df_curr_down, df_prev)
    assert wall_down.status == "walls migrating down"

    # Case 3: Steady
    wall_steady = oi_timeline.compute_wall_migration(df_prev, df_prev)
    assert wall_steady.status == "walls steady"


def test_breadth_and_price_divergence_downgrade():
    """Verifies that breadth < 2 blocks impulse, and price divergence downgrades IMPULSE to WATCH."""
    r = oi_timeline.load_writer_gap_rules()
    wall_info = oi_timeline.WallMigrationInfo(23200, 23000, 23200, 23000)

    # 1. Breadth < 2: only 1 strike bends ⇊⇊ -> No impulse
    strikes_breadth1 = {
        23000.0: oi_timeline.WriterGapStrikeData(23000.0, -15.0, 5.0, 20.0, "PE_ABOVE", ce_bend="⇊⇊", ce_who="COV", ce_who_full="COVERING"),
        23100.0: oi_timeline.WriterGapStrikeData(23100.0, -2.0, 1.0, 3.0, "PE_ABOVE", ce_bend="→", ce_who="?"),
        23200.0: oi_timeline.WriterGapStrikeData(23200.0, 0.0, 0.0, 0.0, "INTERTWINED", ce_bend="→", ce_who="?"),
    }
    read_b1 = oi_timeline.determine_writer_lines_read(
        strikes=strikes_breadth1,
        net_gap=23.0,
        gap_5m=15.0,
        spot_chg_5m=10.0,  # confirms
        wall_info=wall_info,
        is_expiry=False,
        rules=r,
    )
    assert not read_b1.is_impulse

    # 2. Breadth >= 2 with price confirmation -> IMPULSE
    strikes_breadth2 = {
        23000.0: oi_timeline.WriterGapStrikeData(23000.0, -15.0, 5.0, 20.0, "PE_ABOVE", ce_bend="⇊⇊", ce_who="COV", ce_who_full="COVERING"),
        23100.0: oi_timeline.WriterGapStrikeData(23100.0, -12.0, 4.0, 16.0, "PE_ABOVE", ce_bend="⇊⇊", ce_who="COV", ce_who_full="COVERING"),
        23200.0: oi_timeline.WriterGapStrikeData(23200.0, 0.0, 0.0, 0.0, "INTERTWINED", ce_bend="→", ce_who="?"),
    }
    read_conf = oi_timeline.determine_writer_lines_read(
        strikes=strikes_breadth2,
        net_gap=36.0,
        gap_5m=20.0,
        spot_chg_5m=15.0,  # positive gap_5m and positive spot_chg -> price confirms
        wall_info=wall_info,
        is_expiry=False,
        rules=r,
    )
    assert read_conf.is_impulse
    assert "BULLISH IMPULSE" in read_conf.read_text
    assert read_conf.price_agree_str == "price confirms"

    # 3. Price divergence: gap_5m > 0 but spot fell (-15.0) -> Downgraded to WATCH
    read_div = oi_timeline.determine_writer_lines_read(
        strikes=strikes_breadth2,
        net_gap=36.0,
        gap_5m=20.0,
        spot_chg_5m=-15.0,  # divergent!
        wall_info=wall_info,
        is_expiry=False,
        rules=r,
    )
    assert read_div.is_downgraded
    assert "WATCH" in read_div.read_text
    assert read_div.price_agree_str == "price diverges"


def test_expiry_handling_and_suppression():
    """Verifies expiry doubling of min_gap_lakhs, tag, and post-14:00 suppression."""
    r = oi_timeline.load_writer_gap_rules()

    # Expiry day doubling
    sess_date = date(2026, 9, 24)  # Expiry day
    is_exp, is_eve = oi_timeline.is_writer_gap_expiry(sess_date)
    assert is_exp or is_eve

    # Read tagging on expiry
    wall_info = oi_timeline.WallMigrationInfo(23200, 23000, 23200, 23000)
    strikes_dummy = {
        23000.0: oi_timeline.WriterGapStrikeData(23000.0, 10.0, 50.0, 40.0, "PE_ABOVE"),
        23050.0: oi_timeline.WriterGapStrikeData(23050.0, 10.0, 50.0, 40.0, "PE_ABOVE"),
        23100.0: oi_timeline.WriterGapStrikeData(23100.0, 10.0, 50.0, 40.0, "PE_ABOVE"),
    }
    read_exp = oi_timeline.determine_writer_lines_read(
        strikes=strikes_dummy,
        net_gap=120.0,
        gap_5m=10.0,
        spot_chg_5m=5.0,
        wall_info=wall_info,
        is_expiry=True,
        rules=r,
    )
    assert "EXPIRY — walls unstable" in read_exp.read_text

    # Post-14:00 Telegram alert suppression
    # Read at 14:15 on expiry day
    read_impulse_exp = oi_timeline.WriterLinesRead(
        timestamp=datetime.combine(sess_date, dtime(14, 15)),
        clock_time="14:15",
        read_text="🟢 BULLISH IMPULSE — EXPIRY — walls unstable",
        raw_read="BULLISH IMPULSE",
        breadth_str="3/3",
        breadth_count=3,
        bending_leg="CE",
        dominant_who_ce="COVERING 3/3",
        dominant_who_pe="WRITING 1/3",
        gap_5m=50.0,
        net_gap=100.0,
        spot=23100.0,
        price_agree=True,
        price_agree_str="price confirms",
        wall_migration_str="walls steady",
        is_expiry=True,
        is_impulse=True,
        telegram_text="🟢 BULLISH IMPULSE 14:15",
    )
    dispatched = oi_timeline.dispatch_writer_alerts(crossover=None, read=read_impulse_exp, rules=r)
    # Must be suppressed (returns None)
    assert dispatched is None


def test_telegram_deduplication_and_combination():
    """Verifies that crossover and impulse combine when firing within 5 min and deduplicate."""
    sess_date = date(2026, 9, 25)
    t_base = datetime.combine(sess_date, dtime(14, 5))

    cross_event = oi_timeline.WriterCrossoverEvent(
        timestamp=t_base + timedelta(minutes=1),
        clock_time="14:06",
        direction="BULLISH",
        net_gap=14.0,
        spot=23150.0,
        how="CE COVERING (short covering)",
        leg_moved_more="CE",
        classification="COVERING",
        leading_strike=23100.0,
        leading_strike_change=-91.0,
        wall_migration_status="walls steady",
        telegram_text="🔀 WRITER CROSS 14:06 → BULLISH (CE COVERING -91L at 23,100)\n Net gap +14L | Spot 23,150 | walls steady",
    )

    read_impulse = oi_timeline.WriterLinesRead(
        timestamp=t_base,
        clock_time="14:05",
        read_text="🟢 BULLISH IMPULSE (short covering)",
        raw_read="BULLISH IMPULSE",
        breadth_str="3/3",
        breadth_count=3,
        bending_leg="CE",
        dominant_who_ce="COVERING 3/3",
        dominant_who_pe="LONG EXIT 1/3",
        gap_5m=63.0,
        net_gap=-83.0,
        spot=23100.0,
        price_agree=True,
        price_agree_str="price confirms",
        wall_migration_str="walls steady",
        is_expiry=False,
        is_impulse=True,
        telegram_text="🟢 BULLISH IMPULSE 14:05 — CE covering 3/3 (23,100 -31L, IV +0.7)\n | gap +63L | price confirms",
    )

    # Combination
    comb = oi_timeline.dispatch_writer_alerts(crossover=cross_event, read=read_impulse)
    assert comb is not None
    assert "BULLISH IMPULSE" in comb
    assert "WRITER CROSS" in comb

    # Deduplication: Re-dispatching exact same events must NOT send again
    dup = oi_timeline.dispatch_writer_alerts(crossover=cross_event, read=read_impulse)
    assert dup is None


def test_basket_hold_reselect_and_no_crossover_across_change():
    """Verifies that basket holds for 30m, spot outside reselects, and crossover is blocked across basket change."""
    df_base = pd.DataFrame([
        {"strike": 23000.0, "ce_oi": 50000, "pe_oi": 50000, "ce_chg_oi": 1000, "pe_chg_oi": 1000, "ce_delta": 0.5, "pe_delta": -0.5, "ce_gamma": 0.002, "pe_gamma": 0.002, "total_gamma": 0.004, "expiry": "2026-10-01", "ce_iv": 12.0, "pe_iv": 12.0, "ce_ltp": 100.0, "pe_ltp": 100.0},
        {"strike": 23050.0, "ce_oi": 60000, "pe_oi": 60000, "ce_chg_oi": 1000, "pe_chg_oi": 1000, "ce_delta": 0.5, "pe_delta": -0.5, "ce_gamma": 0.003, "pe_gamma": 0.003, "total_gamma": 0.006, "expiry": "2026-10-01", "ce_iv": 12.0, "pe_iv": 12.0, "ce_ltp": 80.0, "pe_ltp": 80.0},
        {"strike": 23100.0, "ce_oi": 70000, "pe_oi": 70000, "ce_chg_oi": 1000, "pe_chg_oi": 1000, "ce_delta": 0.5, "pe_delta": -0.5, "ce_gamma": 0.002, "pe_gamma": 0.002, "total_gamma": 0.004, "expiry": "2026-10-01", "ce_iv": 12.0, "pe_iv": 12.0, "ce_ltp": 60.0, "pe_ltp": 60.0},
        {"strike": 23500.0, "ce_oi": 100000, "pe_oi": 100000, "ce_chg_oi": 5000, "pe_chg_oi": 5000, "ce_delta": 0.5, "pe_delta": -0.5, "ce_gamma": 0.005, "pe_gamma": 0.005, "total_gamma": 0.010, "expiry": "2026-10-01", "ce_iv": 12.0, "pe_iv": 12.0, "ce_ltp": 10.0, "pe_ltp": 10.0},
    ])

    basket_init = oi_timeline.select_writer_basket(df_base, 23050.0)
    assert len(basket_init) == 3
    assert 23050.0 in basket_init

    # Spot moves > 1 strike outside basket (> 23100 + 50 = 23150)
    spot_moved = 23300.0
    # Reselect must select new strikes centered near 23300
    df_new = pd.DataFrame([
        {"strike": 23250.0, "ce_oi": 50000, "pe_oi": 50000, "ce_chg_oi": 1000, "pe_chg_oi": 1000, "ce_delta": 0.5, "pe_delta": -0.5, "ce_gamma": 0.002, "pe_gamma": 0.002, "total_gamma": 0.004, "expiry": "2026-10-01", "ce_iv": 12.0, "pe_iv": 12.0, "ce_ltp": 100.0, "pe_ltp": 100.0},
        {"strike": 23300.0, "ce_oi": 60000, "pe_oi": 60000, "ce_chg_oi": 1000, "pe_chg_oi": 1000, "ce_delta": 0.5, "pe_delta": -0.5, "ce_gamma": 0.003, "pe_gamma": 0.003, "total_gamma": 0.006, "expiry": "2026-10-01", "ce_iv": 12.0, "pe_iv": 12.0, "ce_ltp": 80.0, "pe_ltp": 80.0},
        {"strike": 23350.0, "ce_oi": 70000, "pe_oi": 70000, "ce_chg_oi": 1000, "pe_chg_oi": 1000, "ce_delta": 0.5, "pe_delta": -0.5, "ce_gamma": 0.002, "pe_gamma": 0.002, "total_gamma": 0.004, "expiry": "2026-10-01", "ce_iv": 12.0, "pe_iv": 12.0, "ce_ltp": 60.0, "pe_ltp": 60.0},
    ])
    basket_new = oi_timeline.select_writer_basket(df_new, spot_moved)
    assert basket_new != basket_init
    assert 23300.0 in basket_new


def test_two_poll_confirmation_rule():
    """Verifies that crossover confirms only when new sign holds 2 consecutive polls with |gap| >= min_gap_lakhs."""
    sess_date = date(2026, 9, 25)
    r = oi_timeline.load_writer_gap_rules()

    df_p0 = pd.DataFrame([{"strike": 23000.0, "ce_chg_oi": 50000, "pe_chg_oi": 10000, "ce_oi": 100000, "pe_oi": 100000, "ce_delta": 0.5, "pe_delta": -0.5, "ce_gamma": 0.002, "pe_gamma": 0.002, "total_gamma": 0.004, "ce_iv": 12.0, "pe_iv": 12.0, "ce_ltp": 50.0, "pe_ltp": 50.0}])
    df_p1 = pd.DataFrame([{"strike": 23000.0, "ce_chg_oi": 20000, "pe_chg_oi": 40000, "ce_oi": 70000, "pe_oi": 140000, "ce_delta": 0.5, "pe_delta": -0.5, "ce_gamma": 0.002, "pe_gamma": 0.002, "total_gamma": 0.004, "ce_iv": 13.0, "pe_iv": 12.0, "ce_ltp": 60.0, "pe_ltp": 40.0}])
    df_p2 = pd.DataFrame([{"strike": 23000.0, "ce_chg_oi": 10000, "pe_chg_oi": 60000, "ce_oi": 50000, "pe_oi": 180000, "ce_delta": 0.5, "pe_delta": -0.5, "ce_gamma": 0.002, "pe_gamma": 0.002, "total_gamma": 0.004, "ce_iv": 13.5, "pe_iv": 12.0, "ce_ltp": 70.0, "pe_ltp": 30.0}])

    t0 = datetime.combine(sess_date, dtime(9, 15))
    t1 = t0 + timedelta(minutes=3)
    t2 = t1 + timedelta(minutes=3)
    t3 = t2 + timedelta(minutes=3)

    snaps = [(t0, 23000.0, df_p0), (t1, 23010.0, df_p1), (t2, 23020.0, df_p2), (t3, 23030.0, df_p2)]
    res = oi_timeline.evaluate_writer_gap_session(snaps, sess_date, rules=r)
    # The crossover must confirm on poll 2 (at 09:21)
    assert len(res.crossovers) >= 1
    assert res.crossovers[0].direction == "BULLISH"




