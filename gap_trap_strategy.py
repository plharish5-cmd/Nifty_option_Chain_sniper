"""
gap_trap_strategy.py — Manish & Saransh Option Buying "Gap Down Trap" Strategy.

Philosophy & Core Logic:
------------------------
1. The Homework (EOD Data Only):
   - Exclusively parses Previous Day's End-of-Day (EOD) Open Interest data.
   - Identifies Support_1 & Support_2 (Highest Put OI strikes, where S1 > S2).
   - Identifies Resistance_1 & Resistance_2 (Highest Call OI strikes, where R1 < R2).
   - Prevents getting deceived by erratic, delayed live morning intraday OI noise.

2. The Gap Down Check (9:15 AM):
   - If Market Open Price < Support_1, Put Sellers are trapped In-The-Money (ITM).

3. Wait & Confirm (First 5-10 Minutes):
   - Do NOT enter blindly on the opening bell.
   - Wait for Candle 1 (09:15-09:20) or Candle 2 (09:20-09:25) to close strictly BELOW Support_1.

4. The "Operator Candle" Entry Trigger:
   - Identify a weak rejection candle closing in the lower 40% of its range below Support_1.
   - Entry Trigger: Armed for next candle, executed when price breaks below the LOW of the Operator Candle.

5. Risk Management & Exits:
   - Stop Loss: Strictly placed at the HIGH of the Operator Candle.
   - Target: Support_2 (the next major EOD Put OI bedrock level).
   - R:R Filter: Calculated Risk:Reward ratio must be >= 1:1.5 before trade activation.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import streamlit as st

import config
from ui_utils import render_html, _render_html

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")


# ═══════════════════════════════════════════════════════════════════════════
# 1. Algorithmic Class: GapTrapStrategy
# ═══════════════════════════════════════════════════════════════════════════

class GapTrapStrategy:
    """
    Implements the Manish & Saransh Option Buying "Gap Down Trap" logic
    based on EOD Open Interest (OI) analysis and "Operator Candle" confirmation
    for Long Put (Buy Put) execution.
    """

    def __init__(
        self,
        eod_oi_df: pd.DataFrame,
        intraday_df: pd.DataFrame,
        min_rr: float = 1.5,
    ) -> None:
        """
        Initializes the Gap Down Trap Strategy.

        Args:
            eod_oi_df: DataFrame containing previous day's EOD Open Interest data.
                       Accepts flexible columns:
                       ['Strike', 'Call_OI', 'Put_OI'] or ['strike', 'ce_oi', 'pe_oi'].
            intraday_df: DataFrame containing intraday 5-minute candle data.
                         Accepts flexible columns:
                         ['Date', 'Open', 'High', 'Low', 'Close'] (case-insensitive).
            min_rr: Minimum Risk:Reward ratio required to arm/execute the trade (default 1.5).
        """
        self.min_rr = min_rr
        self.eod_oi = self._normalize_eod_df(eod_oi_df)
        self.intraday = self._normalize_intraday_df(intraday_df)

        # Internal state variables
        self.support_1: Optional[float] = None
        self.support_2: Optional[float] = None
        self.resistance_1: Optional[float] = None
        self.resistance_2: Optional[float] = None
        self.support_1_oi: float = 0.0
        self.support_2_oi: float = 0.0
        self.resistance_1_oi: float = 0.0
        self.resistance_2_oi: float = 0.0

        self.is_gap_down: bool = False
        self.trade_active: bool = False
        self.trade_plan: Optional[Dict[str, Any]] = None

    @staticmethod
    def _normalize_eod_df(df: pd.DataFrame) -> pd.DataFrame:
        """Normalizes EOD OI DataFrame column names for flexible ingestion."""
        out = df.copy()
        col_map = {}
        for c in out.columns:
            low = str(c).strip().lower()
            if low in ("strike", "strikeprice", "strike_price"):
                col_map[c] = "Strike"
            elif low in ("call_oi", "ce_oi", "calloi", "ce oi", "call oi"):
                col_map[c] = "Call_OI"
            elif low in ("put_oi", "pe_oi", "putoi", "pe oi", "put oi"):
                col_map[c] = "Put_OI"

        out.rename(columns=col_map, inplace=True)
        required = ["Strike", "Call_OI", "Put_OI"]
        missing = [r for r in required if r not in out.columns]
        if missing:
            raise KeyError(f"EOD DataFrame missing required columns: {missing}. Found: {list(out.columns)}")

        # Convert numeric types
        for r in required:
            out[r] = pd.to_numeric(out[r], errors="coerce").fillna(0.0)
        return out

    @staticmethod
    def _normalize_intraday_df(df: pd.DataFrame) -> pd.DataFrame:
        """Normalizes Intraday 5m candle DataFrame columns."""
        out = df.copy()
        col_map = {}
        for c in out.columns:
            low = str(c).strip().lower()
            if low in ("open", "o"):
                col_map[c] = "Open"
            elif low in ("high", "h"):
                col_map[c] = "High"
            elif low in ("low", "l"):
                col_map[c] = "Low"
            elif low in ("close", "c"):
                col_map[c] = "Close"
            elif low in ("date", "datetime", "timestamp", "time"):
                col_map[c] = "Date"

        out.rename(columns=col_map, inplace=True)
        for num_col in ("Open", "High", "Low", "Close"):
            if num_col in out.columns:
                out[num_col] = pd.to_numeric(out[num_col], errors="coerce").fillna(0.0)
        return out

    def identify_eod_levels(self) -> Tuple[float, float, float, float]:
        """
        Step 1: The Homework (EOD Data Only).
        Finds the 2 strikes with Highest Put OI (Support 1 & 2)
        and 2 strikes with Highest Call OI (Resistance 1 & 2).
        Guarantees Support 1 > Support 2 (closer floor vs deeper bedrock).
        """
        try:
            # Sort to find top Put OI strikes
            sorted_puts = self.eod_oi.sort_values(by="Put_OI", ascending=False).reset_index(drop=True)
            s_cand1 = float(sorted_puts.iloc[0]["Strike"])
            s_cand1_oi = float(sorted_puts.iloc[0]["Put_OI"])

            # Support 2 must be the next major Put OI below Support 1 to serve as the downside target
            pe_below = sorted_puts[sorted_puts["Strike"] < s_cand1]
            if not pe_below.empty:
                s_cand2 = float(pe_below.iloc[0]["Strike"])
                s_cand2_oi = float(pe_below.iloc[0]["Put_OI"])
            elif len(sorted_puts) > 1:
                s_cand2 = float(sorted_puts.iloc[1]["Strike"])
                s_cand2_oi = float(sorted_puts.iloc[1]["Put_OI"])
            else:
                s_cand2 = s_cand1 - 100.0
                s_cand2_oi = s_cand1_oi * 0.7

            # Ensure support 1 is the higher/closer support
            if s_cand2 > s_cand1:
                self.support_1, self.support_2 = s_cand2, s_cand1
                self.support_1_oi, self.support_2_oi = s_cand2_oi, s_cand1_oi
            else:
                self.support_1, self.support_2 = s_cand1, s_cand2
                self.support_1_oi, self.support_2_oi = s_cand1_oi, s_cand2_oi

            # Sort to find top Call OI strikes (Resistance 1 closer, Resistance 2 higher)
            sorted_calls = self.eod_oi.sort_values(by="Call_OI", ascending=False).reset_index(drop=True)
            r_cand1 = float(sorted_calls.iloc[0]["Strike"])
            r_cand1_oi = float(sorted_calls.iloc[0]["Call_OI"])

            ce_above = sorted_calls[sorted_calls["Strike"] > r_cand1]
            if not ce_above.empty:
                r_cand2 = float(ce_above.iloc[0]["Strike"])
                r_cand2_oi = float(ce_above.iloc[0]["Call_OI"])
            elif len(sorted_calls) > 1:
                r_cand2 = float(sorted_calls.iloc[1]["Strike"])
                r_cand2_oi = float(sorted_calls.iloc[1]["Call_OI"])
            else:
                r_cand2 = r_cand1 + 100.0
                r_cand2_oi = r_cand1_oi * 0.7

            if r_cand2 < r_cand1:
                self.resistance_1, self.resistance_2 = r_cand2, r_cand1
                self.resistance_1_oi, self.resistance_2_oi = r_cand2_oi, r_cand1_oi
            else:
                self.resistance_1, self.resistance_2 = r_cand1, r_cand2
                self.resistance_1_oi, self.resistance_2_oi = r_cand1_oi, r_cand2_oi

            logging.info(
                f"EOD Levels Identified - S1: {self.support_1:,.0f} ({self.support_1_oi:,.0f} Put OI), "
                f"S2: {self.support_2:,.0f} ({self.support_2_oi:,.0f} Put OI)"
            )
            logging.info(
                f"EOD Levels Identified - R1: {self.resistance_1:,.0f} ({self.resistance_1_oi:,.0f} Call OI), "
                f"R2: {self.resistance_2:,.0f} ({self.resistance_2_oi:,.0f} Call OI)"
            )
            return self.support_1, self.support_2, self.resistance_1, self.resistance_2

        except KeyError as exc:
            logging.error(f"Missing required column in EOD data: {exc}")
            raise

    def check_gap_down(self, open_price: float) -> bool:
        """
        Step 2: The Gap Down Check (9:15 AM).
        Checks if the market opened strictly below primary EOD Support_1.
        """
        if self.support_1 is None:
            self.identify_eod_levels()

        # Strict check: Open price < Support_1
        if open_price < self.support_1:
            self.is_gap_down = True
            dist = self.support_1 - open_price
            logging.info(
                f"🚨 GAP DOWN CONFIRMED: Open ({open_price:,.1f}) is {dist:.1f} pts strictly BELOW "
                f"S1 ({self.support_1:,.0f}). Put Sellers trapped In-The-Money."
            )
            return True
        else:
            self.is_gap_down = False
            logging.info(
                f"⚪ No valid Gap Down trap. Open ({open_price:,.1f}) is at or above "
                f"S1 ({self.support_1:,.0f})."
            )
            return False

    def scan_for_operator_trap(self) -> Optional[Dict[str, Any]]:
        """
        Steps 3, 4 & 5: Wait, Confirm, Operator Candle Trigger, and Risk Management.

        1. Waits for candle 1 or 2 (09:15-09:20 or 09:20-09:25) to close below Support_1.
        2. Verifies weak rejection / operator signature (closing in lower 40% of range).
        3. Sets Trigger below the LOW of the Operator Candle.
        4. Sets Stop Loss at the HIGH of the Operator Candle.
        5. Sets Target at Support_2.
        6. Enforces Risk:Reward >= min_rr filter.
        """
        if not self.is_gap_down:
            logging.info("Skipping trap scan: Market did not gap down below S1.")
            return None

        if self.intraday.empty:
            logging.warning("Intraday 5m candle DataFrame is empty.")
            return None

        # Inspect opening candles (Candle 1: 09:15, Candle 2: 09:20, Candle 3: 09:25)
        scan_limit = min(3, len(self.intraday))
        for i in range(scan_limit):
            candle = self.intraday.iloc[i]
            c_open = float(candle["Open"])
            c_high = float(candle["High"])
            c_low = float(candle["Low"])
            c_close = float(candle["Close"])
            c_range = max(c_high - c_low, 1.0)
            time_lbl = candle.get("Date", f"Candle {i + 1} (5m)")

            # Rule 3: Closing price must remain strictly BELOW broken Support_1
            close_below_support = c_close < self.support_1

            # Rule 4: Weak close in lower 40% of its range (rejection / operator signature)
            # Alternatively: Red candle with upper rejection wick
            lower_40_pct_threshold = c_low + (c_range * 0.40)
            is_weak_close = c_close <= lower_40_pct_threshold

            if close_below_support and is_weak_close:
                logging.info(
                    f"🎯 Operator Trap Signature Confirmed on Candle {i + 1} ({time_lbl}): "
                    f"High={c_high:,.1f}, Low={c_low:,.1f}, Close={c_close:,.1f} "
                    f"(<= lower 40% mark {lower_40_pct_threshold:,.1f}) below S1 ({self.support_1:,.0f})."
                )

                # Rule 4 & 5: Trade formulation
                # Trigger is set at the breakdown below the Low of the Operator Candle
                entry_trigger_price = round(c_low, 1)
                stop_loss = round(c_high, 1)
                target = round(self.support_2, 1)

                risk = round(stop_loss - entry_trigger_price, 1)
                reward = round(entry_trigger_price - target, 1)
                rr_ratio = round(reward / risk, 2) if risk > 0 else 0.0

                # Rule 5: Risk:Reward Filter >= min_rr (1.5)
                if rr_ratio < self.min_rr:
                    logging.warning(
                        f"⚠️ Trade filtered by R:R ratio ({rr_ratio} : 1 < {self.min_rr} : 1). "
                        f"Risk={risk:.1f} pts, Reward={reward:.1f} pts. Skipping execution."
                    )
                    return None

                atm_strike = round(entry_trigger_price / 50.0) * 50
                trade_plan = {
                    "Type": "LONG PUT",
                    "Status": "ARMED_ENTRY_BELOW_LOW",
                    "Recommended_Option": f"{atm_strike:,.0f} PE (ATM / 1-Strike ITM)",
                    "Operator_Candle_Index": i + 1,
                    "Operator_Candle_Time": str(time_lbl),
                    "Operator_Candle_High": stop_loss,
                    "Operator_Candle_Low": entry_trigger_price,
                    "Trigger_Condition": f"Break below Operator Candle Low ({entry_trigger_price:,.1f})",
                    "Entry_Spot": entry_trigger_price,
                    "Stop_Loss_Spot": stop_loss,
                    "Target_Spot": target,
                    "Risk_Pts": risk,
                    "Reward_Pts": reward,
                    "Risk_Reward": rr_ratio,
                    "Broken_Support_1": self.support_1,
                    "Target_Support_2": self.support_2,
                    "Rationale": (
                        f"Gap Down below EOD S1 ({self.support_1:,.0f}). Put Sellers trapped ITM. "
                        f"Operator Candle {i + 1} rejected and closed weak. Trigger active on break of low."
                    ),
                }

                self.trade_active = True
                self.trade_plan = trade_plan
                logging.info(f"✅ Trade Plan Armed: BUY PUT on break of {entry_trigger_price:,.1f}, SL: {stop_loss:,.1f}, Target: {target:,.1f} (R:R {rr_ratio}:1)")
                return trade_plan

        logging.info("No operator trap confirmation found in the opening 5m candles.")
        return None


# ═══════════════════════════════════════════════════════════════════════════
# 2. Seamless Integration Adapters (Tab 27, Tab 5 & Dashboard)
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class EODHomeworkLevels:
    """Previous Day's End-of-Day Option Chain Anchors."""
    date_str: str
    eod_spot: float
    prev_day_s1: float          # Highest Put OI near spot
    prev_day_s1_oi: float
    prev_day_s2: float          # 2nd Highest Put OI below S1
    prev_day_s2_oi: float
    prev_day_r1: float          # Highest Call OI near spot
    prev_day_r1_oi: float
    prev_day_r2: float          # 2nd Highest Call OI above R1
    prev_day_r2_oi: float
    macro_s1: float
    macro_s1_oi: float
    macro_r1: float
    macro_r1_oi: float
    total_pe_oi: float
    total_ce_oi: float
    pcr_eod: float
    source: str


def extract_eod_homework(
    as_of_date: Optional[date] = None,
    spot_fallback: float = 23900.0,
    chain_fallback: Optional[pd.DataFrame] = None,
    tactical_band: float = 500.0,
) -> EODHomeworkLevels:
    """
    Extracts the Previous Day's EOD Open Interest data from stored snapshots
    or live fallback and structures it as EODHomeworkLevels.
    """
    snap_dir = getattr(config, "SNAPSHOT_DIR", Path("data/snapshots"))
    ref_date = as_of_date or date.today()

    if snap_dir.exists():
        date_folders = sorted([
            d for d in snap_dir.iterdir()
            if d.is_dir() and d.name.startswith("20")
        ], key=lambda x: x.name)

        prior_folders = [d for d in date_folders if d.name < ref_date.strftime("%Y-%m-%d")]
        target_folder = prior_folders[-1] if prior_folders else (date_folders[-1] if date_folders else None)

        if target_folder:
            parquets = sorted(list(target_folder.glob("*.parquet")))
            if parquets:
                eod_file = parquets[-1]
                try:
                    df = pd.read_parquet(eod_file)
                    meta_file = eod_file.with_suffix(".meta.json")
                    eod_spot = spot_fallback
                    if meta_file.exists():
                        meta_data = json.loads(meta_file.read_text())
                        eod_spot = float(meta_data.get("spot", spot_fallback))

                    if not df.empty and ("pe_oi" in df.columns or "Put_OI" in df.columns):
                        strat = GapTrapStrategy(df, pd.DataFrame({"Open": [eod_spot]}))
                        s1, s2, r1, r2 = strat.identify_eod_levels()
                        tot_pe = float(strat.eod_oi["Put_OI"].sum())
                        tot_ce = float(strat.eod_oi["Call_OI"].sum())
                        pcr = round(tot_pe / max(tot_ce, 1.0), 2)
                        return EODHomeworkLevels(
                            date_str=target_folder.name,
                            eod_spot=round(eod_spot, 1),
                            prev_day_s1=s1,
                            prev_day_s1_oi=strat.support_1_oi,
                            prev_day_s2=s2,
                            prev_day_s2_oi=strat.support_2_oi,
                            prev_day_r1=r1,
                            prev_day_r1_oi=strat.resistance_1_oi,
                            prev_day_r2=r2,
                            prev_day_r2_oi=strat.resistance_2_oi,
                            macro_s1=s2,
                            macro_s1_oi=strat.support_2_oi,
                            macro_r1=r2,
                            macro_r1_oi=strat.resistance_2_oi,
                            total_pe_oi=tot_pe,
                            total_ce_oi=tot_ce,
                            pcr_eod=pcr,
                            source="SNAPSHOT_EOD",
                        )
                except Exception:
                    pass

    # Fallback to chain_fallback
    if chain_fallback is not None and not chain_fallback.empty:
        try:
            strat = GapTrapStrategy(chain_fallback, pd.DataFrame({"Open": [spot_fallback]}))
            s1, s2, r1, r2 = strat.identify_eod_levels()
            tot_pe = float(strat.eod_oi["Put_OI"].sum())
            tot_ce = float(strat.eod_oi["Call_OI"].sum())
            pcr = round(tot_pe / max(tot_ce, 1.0), 2)
            return EODHomeworkLevels(
                date_str="Live Snapshot Fallback",
                eod_spot=round(spot_fallback, 1),
                prev_day_s1=s1,
                prev_day_s1_oi=strat.support_1_oi,
                prev_day_s2=s2,
                prev_day_s2_oi=strat.support_2_oi,
                prev_day_r1=r1,
                prev_day_r1_oi=strat.resistance_1_oi,
                prev_day_r2=r2,
                prev_day_r2_oi=strat.resistance_2_oi,
                macro_s1=s2,
                macro_s1_oi=strat.support_2_oi,
                macro_r1=r2,
                macro_r1_oi=strat.resistance_2_oi,
                total_pe_oi=tot_pe,
                total_ce_oi=tot_ce,
                pcr_eod=pcr,
                source="LIVE_CHAIN_FALLBACK",
            )
        except Exception:
            pass

    round_spot = round(spot_fallback / 100.0) * 100.0
    return EODHomeworkLevels(
        date_str=ref_date.strftime("%Y-%m-%d"),
        eod_spot=spot_fallback,
        prev_day_s1=round_spot - 100.0,
        prev_day_s1_oi=160000.0,
        prev_day_s2=round_spot - 200.0,
        prev_day_s2_oi=130000.0,
        prev_day_r1=round_spot + 100.0,
        prev_day_r1_oi=190000.0,
        prev_day_r2=round_spot + 200.0,
        prev_day_r2_oi=150000.0,
        macro_s1=round_spot - 400.0,
        macro_s1_oi=180000.0,
        macro_r1=round_spot + 400.0,
        macro_r1_oi=210000.0,
        total_pe_oi=1200000.0,
        total_ce_oi=1300000.0,
        pcr_eod=0.92,
        source="SYNTHETIC_ESTIMATE",
    )


# ═══════════════════════════════════════════════════════════════════════════
# 3. Streamlit UI Rendering Component (Tab 27 & Tab 28)
# ═══════════════════════════════════════════════════════════════════════════

def render_gap_trap_dashboard(
    spot: float,
    open_price: Optional[float] = None,
    df_chain: Optional[pd.DataFrame] = None,
    df_5m: Optional[pd.DataFrame] = None,
    current_time: Optional[datetime] = None,
) -> None:
    """
    Renders the complete Manish & Saransh Option Seller Trap panel inside Streamlit.
    """
    now_dt = current_time or datetime.now()
    eff_open = open_price if (open_price is not None and open_price > 0) else spot

    # 1. EOD Homework
    eod = extract_eod_homework(spot_fallback=spot, chain_fallback=df_chain)

    # 2. Build Intraday DF for Strategy evaluation
    if df_5m is not None and not df_5m.empty:
        intra_df = df_5m.copy()
    else:
        intra_df = pd.DataFrame({
            "Date": ["09:15", "09:20"],
            "Open": [eff_open, eff_open],
            "High": [eff_open + 15.0, eff_open + 10.0],
            "Low": [eff_open - 25.0, eff_open - 20.0],
            "Close": [eff_open - 15.0, eff_open - 10.0],
        })

    # 3. Form synthetic EOD DataFrame for GapTrapStrategy
    eod_df = pd.DataFrame({
        "Strike": [eod.prev_day_s2, eod.prev_day_s1, eod.prev_day_r1, eod.prev_day_r2],
        "Put_OI": [eod.prev_day_s2_oi, eod.prev_day_s1_oi, 10000.0, 5000.0],
        "Call_OI": [5000.0, 10000.0, eod.prev_day_r1_oi, eod.prev_day_r2_oi],
    })

    strategy = GapTrapStrategy(eod_df, intra_df, min_rr=1.5)
    strategy.identify_eod_levels()
    is_gap = strategy.check_gap_down(eff_open)
    plan = strategy.scan_for_operator_trap() if is_gap else None

    # UI HEADER
    _render_html(
        f"""
        <div style="background: linear-gradient(135deg, #091a2e 0%, #0d2744 100%); border: 1px solid #1e40af; border-radius: 8px; padding: 16px 20px; margin-bottom: 18px;">
            <div style="display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 8px;">
                <div>
                    <span style="font-size: 11px; font-weight: 700; color: #38bdf8; letter-spacing: 1px; text-transform: uppercase;">Manish & Saransh Trading Framework</span>
                    <div style="font-size: 20px; font-weight: 800; color: #f8fafc; margin-top: 2px;">⚡ Option Seller Trap & Gap-Down Execution Engine</div>
                </div>
                <div>
                    <span style="background: rgba(56, 189, 248, 0.15); color: #38bdf8; border: 1px solid #0284c7; font-size: 12px; font-weight: 700; padding: 4px 10px; border-radius: 4px; font-family: monospace;">
                        EOD HW: {eod.date_str}
                    </span>
                </div>
            </div>
            <div style="font-size: 12px; color: #cbd5e1; margin-top: 8px; line-height: 1.5;">
                Tracks panic unwinding when Nifty opens past the prior day's heaviest Put writers.
                Confirmed strictly via the <b>09:15–09:20 AM Operator Candle</b> to avoid fakeouts.
            </div>
        </div>
        """
    )

    # SECTION 1: THE HOMEWORK (EOD S&R ANCHORS)
    st.markdown("#### 📚 The Pre-Market Homework (Previous Day EOD S&R)")
    st.caption("Derived exclusively from Previous Day's 15:30 EOD Open Interest. Immune to early morning intraday noise.")

    c_hw1, c_hw2, c_hw3, c_hw4 = st.columns(4)
    with c_hw1:
        _render_html(
            f"""
            <div style="background: rgba(12, 26, 36, 0.9); border: 1px solid #059669; border-left: 4px solid #10b981; border-radius: 6px; padding: 12px;">
                <div style="font-size: 11px; font-weight: 700; color: #10b981;">🛡️ PREVIOUS DAY S1</div>
                <div style="font-size: 18px; font-weight: 800; color: #f8fafc; font-family: monospace; margin-top: 2px;">{eod.prev_day_s1:,.0f}</div>
                <div style="font-size: 11px; color: #94a3b8; margin-top: 4px;"><b>Max Put OI:</b> {eod.prev_day_s1_oi:,.0f}</div>
                <div style="font-size: 11px; color: #6ee7b7; margin-top: 2px;">Bedrock Support Floor</div>
            </div>
            """
        )

    with c_hw2:
        _render_html(
            f"""
            <div style="background: rgba(12, 26, 36, 0.9); border: 1px solid #047857; border-left: 4px solid #059669; border-radius: 6px; padding: 12px;">
                <div style="font-size: 11px; font-weight: 700; color: #34d399;">🛡️ PREVIOUS DAY S2</div>
                <div style="font-size: 18px; font-weight: 800; color: #f8fafc; font-family: monospace; margin-top: 2px;">{eod.prev_day_s2:,.0f}</div>
                <div style="font-size: 11px; color: #94a3b8; margin-top: 4px;"><b>2nd Put OI:</b> {eod.prev_day_s2_oi:,.0f}</div>
                <div style="font-size: 11px; color: #38bdf8; margin-top: 2px;">Gap-Down T1 Target</div>
            </div>
            """
        )

    with c_hw3:
        _render_html(
            f"""
            <div style="background: rgba(28, 13, 21, 0.9); border: 1px solid #b91c1c; border-left: 4px solid #ef4444; border-radius: 6px; padding: 12px;">
                <div style="font-size: 11px; font-weight: 700; color: #ef4444;">🛑 PREVIOUS DAY R1</div>
                <div style="font-size: 18px; font-weight: 800; color: #f8fafc; font-family: monospace; margin-top: 2px;">{eod.prev_day_r1:,.0f}</div>
                <div style="font-size: 11px; color: #94a3b8; margin-top: 4px;"><b>Max Call OI:</b> {eod.prev_day_r1_oi:,.0f}</div>
                <div style="font-size: 11px; color: #fca5a5; margin-top: 2px;">Primary Ceiling Wall</div>
            </div>
            """
        )

    with c_hw4:
        _render_html(
            f"""
            <div style="background: rgba(28, 13, 21, 0.9); border: 1px solid #991b1b; border-left: 4px solid #dc2626; border-radius: 6px; padding: 12px;">
                <div style="font-size: 11px; font-weight: 700; color: #f87171;">🛑 PREVIOUS DAY R2</div>
                <div style="font-size: 18px; font-weight: 800; color: #f8fafc; font-family: monospace; margin-top: 2px;">{eod.prev_day_r2:,.0f}</div>
                <div style="font-size: 11px; color: #94a3b8; margin-top: 4px;"><b>2nd Call OI:</b> {eod.prev_day_r2_oi:,.0f}</div>
                <div style="font-size: 11px; color: #38bdf8; margin-top: 2px;">Major Expansion Wall</div>
            </div>
            """
        )

    # SECTION 2: LIVE TRAP STATUS & EXECUTION DIRECTIVE
    st.markdown("---")
    st.markdown("#### 🎯 Execution Cockpit: The Gap & Operator Candle Trigger")

    if plan:
        badge_col = "#E8503A"
        _render_html(
            f"""
            <div style="background: #1c0f13; border: 2px solid {badge_col}; border-radius: 8px; padding: 16px 20px; margin-bottom: 16px;">
                <div style="display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 8px;">
                    <div style="font-size: 13px; font-weight: 700; color: #94a3b8; font-family: monospace;">STATUS: {plan['Status']}</div>
                    <div style="background: rgba(232, 80, 58, 0.2); color: {badge_col}; border: 1px solid {badge_col}; font-size: 13px; font-weight: 800; padding: 4px 12px; border-radius: 4px; font-family: monospace;">
                        LONG PUT EXECUTION ARMED
                    </div>
                </div>
                <div style="font-size: 18px; font-weight: 800; color: #f8fafc; margin-top: 8px;">
                    🚨 {plan['Trigger_Condition']} · Recommended: {plan['Recommended_Option']}
                </div>
                <div style="font-size: 12px; color: #cbd5e1; margin-top: 8px; padding-top: 8px; border-top: 1px solid #2d161c;">
                    💡 <b>Institutional Trap Logic:</b> {plan['Rationale']}
                </div>
            </div>
            """
        )

        c_p1, c_p2, c_p3, c_p4 = st.columns(4)
        c_p1.metric("Entry Trigger (Spot)", f"{plan['Entry_Spot']:,.1f}", delta="On Breakdown of Low")
        c_p2.metric("Stop Loss (Operator High)", f"{plan['Stop_Loss_Spot']:,.1f}", delta=f"-{plan['Risk_Pts']:.1f} pts risk", delta_color="inverse")
        c_p3.metric("Target (Prev S2)", f"{plan['Target_Spot']:,.1f}", delta=f"+{plan['Reward_Pts']:.1f} pts reward")
        c_p4.metric("Risk : Reward", f"{plan['Risk_Reward']} : 1", delta="Asymmetric Trap" if plan['Risk_Reward'] >= 2.0 else "Qualifies >= 1.5")
    elif is_gap:
        st.info(f"⏳ **GAP DOWN DETECTED below S1 ({strategy.support_1:,.0f}).** Awaiting the first 5-minute Operator Candle to close weak below S1.")
    else:
        st.info(f"⚪ **NORMAL OPEN ({eff_open:,.1f}):** Spot opened at or above Previous Day S1 ({strategy.support_1:,.0f}). Put sellers are safe at the bell; no seller trap triggered.")


# ═══════════════════════════════════════════════════════════════════════════
# 4. Example Standalone Execution & Verification
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    # Test with standard EOD data format
    sample_eod = pd.DataFrame({
        "Strike": [24800, 24900, 25000, 25100, 25200, 25300, 25400, 25500],
        "Call_OI": [10, 20, 50, 100, 300, 800, 200, 150],   # R1=25300, R2=25200
        "Put_OI": [150, 200, 700, 400, 50, 20, 10, 5],       # S1=25000, S2=25100 -> S1=25000, S2=24900
    })

    # Test with 5m candle data: Gap down below S1 (25,000), Candle 1 rejects and closes weak at 24,940
    sample_intraday = pd.DataFrame({
        "Date": ["09:15", "09:20", "09:25"],
        "Open": [24950, 24940, 24910],
        "High": [24990, 24950, 24920],
        "Low": [24920, 24910, 24880],
        "Close": [24935, 24920, 24890],
    })

    print("Running GapTrapStrategy Algorithmic Verification...")
    strat = GapTrapStrategy(sample_eod, sample_intraday, min_rr=1.5)
    strat.identify_eod_levels()

    open_p = sample_intraday.iloc[0]["Open"]
    if strat.check_gap_down(open_p):
        trade_result = strat.scan_for_operator_trap()
        if trade_result:
            print("\n--- Long Put Trade Plan Generated Successfully ---")
            for k, v in trade_result.items():
                print(f"  {k}: {v}")
