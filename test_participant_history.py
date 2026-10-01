"""
test_participant_history.py — Unit tests and quantitative verification for participant positioning history.
"""

from datetime import date, timedelta
from pathlib import Path
import tempfile

import numpy as np
import pandas as pd
import pytest

import participant_history as ph


@pytest.fixture(name="participant_df")
def get_test_df():
    """Load the participant dataset."""
    df = ph.load(force_reload=True)
    assert not df.empty, "Participant dataframe should not be empty"
    return df


def test_loader_schema_and_integrity(participant_df):
    """Verify tidy schema, columns, participants, and DII handling."""
    required_cols = [
        "date", "participant", "fut_long", "fut_short",
        "ce_long", "ce_short", "pe_long", "pe_short",
        "net_ce", "net_pe", "net_fut"
    ]
    for col in required_cols:
        assert col in participant_df.columns, f"Missing required column {col}"

    # Verify participants
    parts = set(participant_df["participant"].unique())
    assert {"FII", "PRO", "Retailers", "DII"}.issubset(parts)

    # Verify distinct dates (at least 65 historical sessions)
    unique_dates = participant_df["date"].unique()
    assert len(unique_dates) >= 65, f"Expected >= 65 sessions, got {len(unique_dates)}"

    # Verify DII short columns absence is handled without imputation
    dii_rows = participant_df[participant_df["participant"] == "DII"]
    assert dii_rows["ce_short"].isna().all(), "DII ce_short should be NaN (not imputed)"
    assert dii_rows["pe_short"].isna().all(), "DII pe_short should be NaN (not imputed)"
    assert dii_rows["net_ce"].isna().all(), "DII net_ce should be NaN when short is absent"
    assert dii_rows["net_pe"].isna().all(), "DII net_pe should be NaN when short is absent"
    # DII futures should be present
    assert dii_rows["net_fut"].notna().all(), "DII net_fut should be present"


def test_three_empirical_truths(participant_df):
    """Verify the 3 core findings that this module exists to surface."""
    base = ph.compute_structural_baseline(participant_df, window=65)
    assert base["total_sessions"] == 65

    truths = {t["code"]: t for t in base["structural_truths"]}

    # Truth 1: FIIs match US dealer convention 65 of 65 sessions
    assert "FII_DEALER_CONVENTION" in truths
    t1 = truths["FII_DEALER_CONVENTION"]
    assert "65 of 65" in t1["finding"]
    assert "100.0%" in t1["finding"]
    assert t1["validates"] == "GEX_DEALER_SIGN = -1"

    # Truth 2: PRO desks net long both sides (debunking short-both myth)
    assert "PRO_LONG_BOTH_SIDES" in truths
    t2 = truths["PRO_LONG_BOTH_SIDES"]
    assert "PRO desks are net LONG both calls and puts" in t2["finding"]
    assert "of 65 sessions" in t2["finding"]
    assert "only 1 of 65" in t2["finding"] or "of 65 sessions" in t2["finding"]

    # Truth 3: Clients are net short puts on 65 of 65 sessions
    assert "RETAIL_NET_PUT_WRITERS" in truths
    t3 = truths["RETAIL_NET_PUT_WRITERS"]
    assert "65 of 65" in t3["finding"]
    assert "100.0%" in t3["finding"]


def test_percentile_engine(participant_df):
    """Test percentile calculations, z-scores, DoD changes, and labels."""
    pcts = ph.compute_percentiles(participant_df, window=65)
    assert not pcts.empty

    for _, r in pcts.iterrows():
        pct = r["percentile_rank"]
        lbl = r["label"]
        assert 0.0 <= pct <= 100.0
        if pct >= 95.0:
            assert lbl == "EXTREME HIGH"
        elif pct >= 80.0:
            assert lbl in ("HIGH", "EXTREME HIGH")
        elif pct <= 5.0:
            assert lbl == "EXTREME LOW"
        elif pct <= 20.0:
            assert lbl in ("LOW", "EXTREME LOW")
        else:
            assert lbl == "NORMAL"


def test_divergence_spreads(participant_df):
    """Test FII-minus-Retail spreads, 10th/90th thresholds, and historical trigger rate."""
    div = ph.compute_divergence(participant_df, window=65)
    assert div["known"] is True
    spreads = div["spreads"]

    for k in ["pe_spread", "ce_spread", "fut_spread"]:
        assert k in spreads
        sp = spreads[k]
        assert sp["sample_size"] == 65
        assert sp["p10"] < sp["p90"]
        # In a 10th/90th percentile design, ~20% of historical sessions trigger
        assert 15.0 <= sp["historical_triggers_pct"] <= 25.0


def test_forward_outcome_scoring_guards(participant_df):
    """Test strict enforcement: buckets with n < 10 MUST be marked INSUFFICIENT SAMPLE."""
    res = ph.score_forward_outcomes(participant_df, min_sample=10)
    assert res["known"] is True
    assert "NO STATISTICALLY SOUND DIRECTIONAL PREDICTIVE EDGE CAN BE CLAIMED" in res["statistical_verdict"]

    for o in res["outcomes"]:
        # Since n ~ 7 in decile buckets on 65 sessions, sample is < 10
        if o["sample_size"] < 10:
            assert o["is_valid_sample"] is False
            assert o["hit_rate_1d"] == "INSUFFICIENT SAMPLE", f"Failed for {o}"
            assert o["hit_rate_3d"] == "INSUFFICIENT SAMPLE", f"Failed for {o}"


def test_refuse_future_rows(participant_df):
    """Test refusing rows dated after last completed session."""
    future_date = date.today() + timedelta(days=5)
    dummy_row = pd.DataFrame([{
        "date": future_date,
        "participant": "FII",
        "fut_long": 1000,
        "fut_short": 500,
        "ce_long": 2000,
        "ce_short": 1000,
        "pe_long": 3000,
        "pe_short": 1500,
        "net_ce": 1000,
        "net_pe": 1500,
        "net_fut": 500,
    }])

    with tempfile.NamedTemporaryFile(suffix=".parquet") as tmp:
        added, refused = ph.append(dummy_row, parquet_path=tmp.name)
        assert added == 0
        assert refused == 1


def test_missing_ohlc_graceful_handling(participant_df):
    """Ensure missing OHLC returns known=False without crashing."""
    res = ph.score_forward_outcomes(participant_df, ohlc_path="/tmp/non_existent_ohlc_file.csv")
    assert res["known"] is False
    assert "OHLC file not found" in res["note"]


if __name__ == "__main__":
    print("Running test_participant_history.py suite...")
    df = get_test_df()
    print("  [1/7] Testing loader schema and data integrity...")
    test_loader_schema_and_integrity(df)
    print("  [2/7] Testing three empirical truths...")
    test_three_empirical_truths(df)
    print("  [3/7] Testing percentile engine and labels...")
    test_percentile_engine(df)
    print("  [4/7] Testing divergence spreads and threshold trigger rate...")
    test_divergence_spreads(df)
    print("  [5/7] Testing forward outcome scoring guards (n < 10)...")
    test_forward_outcome_scoring_guards(df)
    print("  [6/7] Testing refusing future rows...")
    test_refuse_future_rows(df)
    print("  [7/7] Testing graceful missing OHLC handling...")
    test_missing_ohlc_graceful_handling(df)
    print("All 7 tests passed successfully! [OK]")

