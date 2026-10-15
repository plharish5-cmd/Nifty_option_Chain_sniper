"""
view_building_tab.py — Institutional Pre-Market View Building & WhatsApp Studio.

Equips traders with:
  1. Executive Pre-Market Verdict Cockpit (18-parameter macro score, market stance, opening gap).
  2. Three-Line Actionable Execution Brief (Chain reading, Gap absorption rule, Key S/R pivots).
  3. WhatsApp Daily Sharing Studio:
     - Instant 1-Click HD Image Download (PNG) matching MJ's published Equity Guys layout.
     - Dark & Light theme toggles for the WhatsApp card.
     - Full-resolution on-screen image preview (right-click / long-press copyable).
     - One-click copyable WhatsApp formatted text summary (with emojis, bolding, and targets).
     - Excel export (.xlsx) for record keeping.
  4. Interactive 18-Parameter Inspection & Dynamic Overrides Form.
  5. Scraper Freshness & Contract Age Audit.
"""
from __future__ import annotations

import io
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pandas as pd
import streamlit as st

import config
import market_data as md
import view_building as vb
from ui_utils import render_html, _render_html

GREEN, RED, SLATE, GOLD = "#2FBF71", "#E8503A", "#8FA6B8", "#C9A227"
BLUE, AMBER, PURPLE = "#4A9BD1", "#E0A030", "#9B51E0"


def generate_whatsapp_text(
    params: list[vb.Param],
    score: int,
    vname: str,
    vcol: str,
    spot: float,
    brief: dict[str, Any] | None = None,
    when: date | None = None,
) -> str:
    """Generates a clean, professional WhatsApp text briefing formatted with emojis & markdown."""
    when = when or date.today()
    stance_emoji = "🟢" if vcol == "green" else "🔴" if vcol == "red" else "🟡"

    # Extract highlights
    gift_obs = next((p.observation for p in params if p.no == 5), "Tracking")
    vix_obs = next((p.observation for p in params if p.no == 3), "Normal")
    p14 = next((p for p in params if p.no == 14), None)
    if p14 and p14.observation:
        fii_lines = [l.strip() for l in p14.observation.splitlines() if l.strip()]
        dedup_fii = []
        for fl in fii_lines:
            if fl not in dedup_fii:
                dedup_fii.append(fl)
        fii_obs = "\n• *FII & DII F&O:* ".join(dedup_fii) if len(dedup_fii) > 1 else (dedup_fii[0] if dedup_fii else "Analyzing")
    else:
        fii_obs = "Analyzing"
    pcr_obs = next((p.observation for p in params if p.no == 13), "Neutral")
    p16 = next((p for p in params if p.no == 16), None)
    p18 = next((p for p in params if p.no == 18), None)
    p8 = next((p for p in params if p.no == 8), None)
    p9 = next((p for p in params if p.no == 9), None)
    s_levels = p8.observation if p8 and p8.observation else "Key Supports"
    r_levels = p9.observation if p9 and p9.observation else "Key Resistances"
    s_strong = f" (Strong Support: {p8.verdict_word})" if p8 and p8.verdict_word not in ("NA", "") else ""
    r_strong = f" (Strong Resistance: {p9.verdict_word})" if p9 and p9.verdict_word not in ("NA", "") else ""

    macro_cues = [
        f"• *GIFT Nifty:* {gift_obs}",
        f"• *India VIX:* {vix_obs}",
        f"• *PCR Ratio:* {pcr_obs}",
    ]
    if p18 and p18.observation:
        macro_cues.append(f"• *USD/INR:* {p18.observation}")
    if p16 and p16.observation:
        g_lines = [l.strip() for l in p16.observation.splitlines() if l.strip()]
        macro_cues.append("• *Global Markets:* " + " · ".join(g_lines))

    lines = [
        f"📊 *NIFTY 50 & BANK NIFTY — PRE-MARKET VIEW*",
        f"📅 *Date:* {when:%d-%b-%Y (%A)} | 08:30 AM IST",
        f"────────────────────────",
        f"🎯 *OVERALL STANCE:* {stance_emoji} *{vname} (Score: {score:+d})*",
        f"────────────────────────",
        f"🌐 *Macro & Opening Cues:*",
        *macro_cues,
        f"",
        f"🏛️ *Smart Money & Institutional Flows:*",
        f"• *FII & DII:* {fii_obs}",
        f"",
        f"📐 *Key Structural Levels (NIFTY):*",
        f"• *Spot Reference:* {spot:,.1f}" if spot else "• *Spot Reference:* Live",
        f"• *Key Supports (S1-S3):* {s_levels}{s_strong}",
        f"• *Key Resistances (R1-R3):* {r_levels}{r_strong}",
    ]

    p15 = next((p for p in params if p.no == 15), None)
    if p15 and p15.observation:
        lines.append("")
        lines.append(f"⚡ *#15 Option Chain Analysis:*")
        lines.append(f"• *Observation:* {p15.observation}")
        if p15.verdict_word:
            lines.append(f"• *Option Chain Stance:* {p15.verdict_word}")
    elif brief and brief.get("condition"):
        c = brief["condition"]
        lines.append("")
        lines.append(f"⚡ *Option Chain Reading:* {c.get('text', 'Analyzing')}")

    lines.extend([
        f"",
        f"💡 *Actionable Trade Plan:*",
        f"• *Execution:* Allow opening 15-min range (ORB) to settle before directional trades.",
        f"• *Bias:* {'Buy dips into VWAP/support with trailing stops' if vcol == 'green' else 'Fade rallies into resistance with trailing stops' if vcol == 'red' else 'Trade rangebound VWAP mean-reversion (exit at 40 pts)'}.",
        f"• *Market State:* {p15.verdict_word if p15 and p15.verdict_word not in ('NA', '') else vname}.",
        f"────────────────────────",
        f"_Generated by AntiGravity Institutional Terminal_",
    ])

    return "\n".join(lines)


def render(
    now: datetime,
    spot: float,
    df: pd.DataFrame,
    vb_daily: pd.DataFrame | None,
    vb_master: pd.DataFrame | None,
    vb_indices: list[dict] | None,
    vb_ad_n: dict[str, Any] | None,
    vb_ad_b: dict[str, Any] | None,
    vb_gift: Any,
    vb_glob: Any,
    vb_crude: Any,
    vb_crude_nse: Any,
    vb_poi: Any,
    vb_vixh: Any,
    vb_nf: Any,
    vb_bnf: Any,
    vb_adrs: Any,
    flows: Any,
    read: Any,
    nse_ts: str,
    refresh_control: Any,
    load_ohlc_cached: Any,
    fetch_vix_cached: Any,
    fetch_flows_cached: Any,
    candles: pd.DataFrame | None = None,
) -> None:
    """Renders the comprehensive, institutional-grade Pre-Market View Building Tab."""

    refresh_control("viewbuild", (load_ohlc_cached, fetch_vix_cached, fetch_flows_cached),
                    "all 18 sources", pull_ohlc=True)

    # 1. Assemble parameters
    if "vb_overrides" not in st.session_state:
        st.session_state.vb_overrides = {}

    _cond = getattr(read, "condition", None)
    if _cond is None and df is not None and not df.empty and spot:
        try:
            import setups as su
            _cond = su.chain_condition(df, spot)
        except Exception:
            pass
    _chain_live = getattr(read, "live", True)

    params = vb.build(
        indices=vb_indices,
        daily=vb_daily,
        master=vb_master,
        nifty_ad=vb_ad_n,
        bn_ad=vb_ad_b,
        pcr=getattr(read, "pcr_oi", None),
        pcr_bnf=getattr(read, "pcr_bnf", None),
        flows=flows,
        poi=vb_poi,
        gift=vb_gift,
        glob=vb_glob,
        crude=vb_crude,
        nifty_fut=vb_nf,
        bn_fut=vb_bnf,
        adrs=vb_adrs,
        crude_nse=vb_crude_nse,
        vixh=vb_vixh,
        spot=spot,
        chain_cond=_cond,
        chain_live=_chain_live,
        chain_stamp=nse_ts,
        candles=candles,
        df=df,
        read=read,
        overrides=st.session_state.vb_overrides,
    )

    auto, tot, man = vb.coverage(params)
    score = vb.total_score(params)
    vname, vcol = vb.verdict(score)

    # Auto-save the sheet so the 7:30 WhatsApp sender (wa_daily) always has today's file. Never blocks the tab.
    try:
        _xl = Path(__file__).resolve().parent / "data" / "view_building" / "View_Building_latest.xlsx"
        vb.to_excel(params, _xl, when=now.date())
    except Exception:
        pass
    v_badge_color = GREEN if vcol == "green" else RED if vcol == "red" else GOLD

    # ═══════════════════════════════════════════════════════════════════════
    # TOP EXECUTIVE VERDICT COCKPIT
    # ═══════════════════════════════════════════════════════════════════════
    _render_html(
        f"""
        <div style="
            background: linear-gradient(135deg, #101726 0%, #172238 100%);
            border: 1px solid #283750;
            border-left: 6px solid {v_badge_color};
            border-radius: 8px;
            padding: 16px 22px;
            margin-bottom: 20px;
        ">
            <div style="display: flex; justify-content: space-between; align-items: flex-start;">
                <div>
                    <span style="font-size: 0.82em; text-transform: uppercase; letter-spacing: 1px; color: #8fa6b8; font-weight: 600;">
                        18-Parameter Institutional Pre-Market Sheet
                    </span>
                    <div style="font-size: 1.65em; font-weight: 800; color: {v_badge_color}; margin-top: 3px;">
                        {vname} BIAS
                    </div>
                </div>
                <div style="text-align: right; background: #0b111c; padding: 6px 18px; border-radius: 6px; border-left: 1px solid #212c40;">
                    <div style="font-size: 0.72em; color: #8fa6b8;">TOTAL SCORE</div>
                    <div style="font-size: 1.55em; font-weight: 800; color: {v_badge_color};">
                        {score:+d}
                    </div>
                </div>
            </div>
            <div style="margin-top: 10px; font-size: 0.9em; color: #cbd5e1;">
                {
                    "Above +14 = Strong Bullish Trend conviction." if vcol == "green"
                    else "Below 0 = Strong Bearish Pressure conviction." if vcol == "red"
                    else "Score between 0 and +14 = Rangebound / Neutral Chop. Trade mean-reversion."
                }
            </div>
        </div>
        """
    )

    h1, h2, h3, h4 = st.columns(4)
    with h1:
        st.metric("Total Score", f"{score:+d}", vname)
    with h2:
        st.metric("Auto-Filled Coverage", f"{auto} / {tot}", f"{auto/tot*100:.0f}% Sourced")
    with h3:
        gift_gap_val = getattr(vb_gift, "gap_points", None)
        gift_pts_str = f"{gift_gap_val:+.0f} pts" if gift_gap_val is not None else "Tracking"
        st.metric("GIFT Nifty Open", gift_pts_str)
    with h4:
        vix_val = next((p.score for p in params if p.no == 3), None)
        st.metric("Manual Inputs", f"{man} parameters")

    try:
        xl_cockpit = vb.to_excel_bytes(params, when=now.date())
        st.download_button(
            label="📥 Download View Building Excel (.xlsx)",
            data=xl_cockpit,
            file_name=f"ViewBuilding_{now:%Y-%m-%d}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            use_container_width=True,
            key="btn_dl_vb_cockpit_excel",
        )
    except Exception:
        pass

    # ═══════════════════════════════════════════════════════════════════════
    # THREE-LINE ACTIONABLE MORNING BRIEF
    # ═══════════════════════════════════════════════════════════════════════
    _br = None
    try:
        _pc = float(vb_daily.iloc[-1]["Close"]) if vb_daily is not None and len(vb_daily) else None
        _br = vb.chain_brief(df, spot, gift=vb_gift, prev_close=_pc, live=True)
    except Exception:
        pass

    if _br:
        st.markdown("---")
        b1, b2, b3 = st.columns(3)
        with b1:
            c = _br.get("condition") or {}
            c_tint = GREEN if c.get("score", 0) > 0 else RED if c.get("score", 0) < 0 else SLATE
            st.markdown(f"**1 · Chain Reading**")
            _render_html(f"<div><span style='color:{c_tint};font-weight:700;font-size:1.05em;'>{c.get('text', 'Analyzing')}</span></div>")
            if not c.get("live"):
                st.caption("Using previous session close — refresh after 09:15.")
        with b2:
            st.markdown(f"**2 · Opening Gap Strategy**")
            if _br.get("gap"):
                st.markdown(f"**Opens** {_br['gap']['text']}")
                st.caption(_br['gap']['caution'])
            else:
                st.caption("Gift Nifty offline — no opening gap bias")
        with b3:
            st.markdown(f"**3 · Key Structural Pivots**")
            if _br.get("levels"):
                st.markdown(f"{_br['levels']['text']}")
            else:
                st.caption("Levels derived from Master Sheet")

        if _br.get("conflict"):
            st.warning(f"⚠️ {_br['conflict']}")

    # ═══════════════════════════════════════════════════════════════════════
    # WHATSAPP DAILY SHARING STUDIO (Direct Image Download & Text Caption)
    # ═══════════════════════════════════════════════════════════════════════
    st.markdown("---")
    st.markdown("### 📱 WhatsApp Daily Sharing Studio")
    st.caption("Generate, preview, and download a high-definition image card to share with your trading desk or WhatsApp groups.")

    wa_col1, wa_col2 = st.columns([1.1, 1])

    img_name = f"ViewBuilding_{now:%Y-%m-%d}.png"
    img_path = config.DATA_DIR / img_name

    with wa_col1:
        st.markdown("##### 🖼️ WhatsApp Image Card")
        theme_choice = st.radio("Image Theme", ["Classic Published Sheet (Light / Gold)", "Institutional Terminal (Dark)"],
                                horizontal=True, key="wa_img_theme")
        is_dark = (theme_choice == "Institutional Terminal (Dark)")

        # Render image to disk
        try:
            vb.to_image(params, img_path, when=now.date(), dark_mode=is_dark)
            img_ready = img_path.exists()
        except Exception as _ie:
            st.error(f"Image generation error: {_ie}")
            img_ready = False

        if img_ready:
            with open(img_path, "rb") as f_img:
                img_bytes = f_img.read()

            st.download_button(
                label="📥 Download WhatsApp Card (HD PNG)",
                data=img_bytes,
                file_name=img_name,
                mime="image/png",
                use_container_width=True,
            )

            st.image(img_bytes, caption=f"Preview: {img_name} (2520×HD Ultra Sharp)", use_column_width=True)

    with wa_col2:
        st.markdown("##### 📋 WhatsApp Text Briefing")
        st.caption("Copy and paste this message alongside the image into WhatsApp:")
        wa_text = generate_whatsapp_text(params, score, vname, vcol, spot, brief=_br, when=now.date())
        st.code(wa_text, language="markdown")

        # Excel Export Option
        st.markdown("##### 📊 Official Excel Sheet (.xlsx)")
        xl_name = f"ViewBuilding_{now:%Y-%m-%d}.xlsx"
        try:
            xl_bytes = vb.to_excel_bytes(params, when=now.date())
            st.download_button(
                label="📥 Download View Building Excel (.xlsx)",
                data=xl_bytes,
                file_name=xl_name,
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                use_container_width=True,
                key="btn_dl_vb_studio_excel",
            )
        except Exception as _xe:
            st.caption(f"Excel export unavailable: {_xe}")

    # ═══════════════════════════════════════════════════════════════════════
    # PARTICIPANT-WISE OPEN INTEREST & OPTION CHAIN MASTER EXCEL GENERATOR
    # ═══════════════════════════════════════════════════════════════════════
    st.markdown("---")
    st.markdown("### 📊 Participant-Wise Open Interest & Option Chain Master Excel")
    st.caption("Generate and download the complete institutional multi-tab workbook (Retailers, DII, FII, PRO, Volume, and Dhan Option Chain Analysis) compiled across all available historical sessions.")

    excel_file = config.DATA_DIR / "NSE_Participant_OI_and_Option_Chain_Master.xlsx"
    p_box1, p_box2 = st.columns([1.1, 1.1])
    with p_box1:
        if st.button("⚡ Generate Master Participant Excel", type="primary", use_container_width=True, key="btn_generate_participant_excel"):
            with st.spinner("Compiling NSE Clearing participant files & pulling Dhan live Option Chain..."):
                try:
                    import build_participant_excel as bpe
                    out_p = bpe.generate_master_excel()
                    st.success(f"✓ Successfully generated! ({out_p.stat().st_size / 1024:.1f} KB, 65+ sessions)")
                except Exception as _bpe_err:
                    st.error(f"Error generating workbook: {_bpe_err}")

    with p_box2:
        if excel_file.exists():
            with open(excel_file, "rb") as _f_xl:
                _xl_data = _f_xl.read()
            st.download_button(
                label="📥 Download Master Excel (.xlsx)",
                data=_xl_data,
                file_name="NSE_Participant_OI_and_Option_Chain_Master.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                use_container_width=True,
                key="btn_download_participant_master_excel",
            )
        else:
            st.info("Click 'Generate' to create the master Excel workbook.")

    if excel_file.exists():
        with st.expander("👁️ View Included Sheets & Summary Breakdown", expanded=False):
            st.markdown("""
            - 📑 **`Retailers`**: Client Index Futures (Long/Short + Changes), Stock Futures, Call/Put Options, Net CE, Net PE, Net Options Positions, Net Futures, Spot.
            - 📑 **`DII`**: Domestic Institutional Investors: Futures, Options, Net Options, Net Futures, Cash Equity Flows (Cr), Spot, % Change.
            - 📑 **`FII`**: Foreign Institutional Investors: Index & Stock Futures, Call/Put Options, Net CE, Net PE, Net Options Positions, Net Futures.
            - 📑 **`PRO`**: Proprietary Trading Desks: Futures & Options Positioning, Net Greeks, Net Options Positions, Net Futures.
            - 📑 **`Participant Wise Open Interest`**: Raw clearing daily snapshot across Client, DII, FII, Pro, and Total.
            - 📑 **`Volume`**: Participant-wise trading volume file from NSE Clearing.
            - 📑 **`Analysis`**: Strike-by-strike Option Chain from Dhan API with Buildup categorizations, Max Pain, PCR, and Spot price.
            """)

    # ═══════════════════════════════════════════════════════════════════════
    # 18 PARAMETERS DETAILED INSPECTION
    # ═══════════════════════════════════════════════════════════════════════
    st.markdown("---")
    t_hdr1, t_hdr2 = st.columns([2.2, 1.2])
    with t_hdr1:
        st.markdown("### 🔍 18-Parameter Quantitative Table")
    with t_hdr2:
        try:
            xl_bytes_t = vb.to_excel_bytes(params, when=now.date())
            st.download_button(
                label="📥 Download Excel (.xlsx)",
                data=xl_bytes_t,
                file_name=f"ViewBuilding_{now:%Y-%m-%d}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                use_container_width=True,
                key="btn_dl_vb_table_excel",
            )
        except Exception:
            pass

    table_filter = st.radio("Filter Parameters", ["All (18)", "Auto-Sourced (14)", "Manual Overrides (4)"], horizontal=True)

    filtered_params = params
    if table_filter == "Auto-Sourced (14)":
        filtered_params = [p for p in params if p.auto]
    elif table_filter == "Manual Overrides (4)":
        filtered_params = [p for p in params if not p.auto]

    p_records = []
    for p in filtered_params:
        vw = p.verdict_word
        sc_str = f"{p.score:+d}" if p.scored else "NA"
        p_records.append({
            "#": p.no,
            "Parameter": p.name,
            "Source": "🤖 Auto-NSE" if p.auto else "✍️ Manual",
            "Observation / Reading": p.observation or "—",
            "Score": sc_str,
            "View": vw,
        })

    st.dataframe(pd.DataFrame(p_records), hide_index=True, use_container_width=True)

    with st.expander("📖 #15 Option Chain Analysis, Market States & Action Rules", expanded=False):
        st.markdown("""
        **Option Chain Analysis Engine:** Synthesizes option chain positioning from the previous market closed day, including quadrant flow, defended Strong Support, Resistance, and Strong Resistance looking for today.
        
        | Market State | Conviction / Flow Characteristics | Score | Actionable Trade Rule |
        | :--- | :--- | :--- | :--- |
        | **BULLISH** | Call Buying Dominant (ITM/OTM Green), Put unwinding | **+2** | 🚀 **TRAIL to 100–400 pts** |
        | **BEARISH** | Put Buying Dominant (ITM/OTM Green), Call unwinding | **-2** | 🔻 **TRAIL to 100–400 pts** |
        | **BULLISH CONSOLIDATION** | Call ITM accumulation, OTM Call/Put selling caps extension | **+1** | 🎯 **Exit strictly at 40 pts only** |
        | **BEARISH CONSOLIDATION** | Put ITM accumulation, Call selling caps upside | **-1** | 🎯 **Exit strictly at 40 pts only** |
        | **CONSOLIDATION** / **""** | Two-way option writing with premium decay, range-bound boundaries | **0** | ⏸️ **Range-bound day, scalp 40 pts max or wait** |

        - 💡 **Action Guidance:** Always adhere to the concrete action to take defined for each option chain state.
        - 📐 **Key Defended Levels:** Uses Strong Support (major Put OI wall), Resistance (nearest Call hurdle), and Strong Resistance (major Call OI wall) to forecast today's operational bounds.
        """)

    # ═══════════════════════════════════════════════════════════════════════
    # MANUAL PARAMETER OVERRIDE FORM
    # ═══════════════════════════════════════════════════════════════════════
    with st.expander("✍️ Adjust / Override Manual Inputs & Option Chain Analysis", expanded=False):
        st.caption("Customize option chain analysis, tweak your action plan, or adjust parameter scores. Your inputs will be reflected immediately in the WhatsApp image.")
        with st.form("vb_manual_form"):
            new_overrides = {}

            # Dedicated #15 Option Chain Analysis Section
            st.markdown("##### ⚡ #15 Option Chain Analysis")
            p15_item = next((p for p in params if p.no == 15), None)
            curr_obs = p15_item.observation if p15_item else ""
            curr_vw = p15_item.view if p15_item else "BEARISH"

            if " — Action: " in curr_obs:
                c_narr, c_act = curr_obs.split(" — Action: ", 1)
                c_act = "Action: " + c_act
            elif " | Action: " in curr_obs:
                c_narr, c_act = curr_obs.split(" | Action: ", 1)
                c_act = "Action: " + c_act
            else:
                c_narr = curr_obs
                c_act = ""

            user_narrative = st.text_area("Option Chain Analysis (Yesterday's Behavior & Defended Levels)", value=c_narr, height=75, key="vb_custom_narrative")
            user_action = st.text_input("Action to take", value=c_act, key="vb_custom_action",
                                        placeholder="e.g. Action: Buy dips near VWAP with trailing SL · Target: 100–400 pts")
            state_options = ["BULLISH", "BEARISH", "BULLISH CONSOLIDATION", "BEARISH CONSOLIDATION", "CONSOLIDATION", ""]
            curr_st_idx = state_options.index(curr_vw) if curr_vw in state_options else 0
            user_state = st.selectbox("Option Chain Stance (Market State)", state_options, index=curr_st_idx, key="vb_custom_state")

            score_map = {
                "BULLISH": 2,
                "BEARISH": -2,
                "BULLISH CONSOLIDATION": 1,
                "BEARISH CONSOLIDATION": -1,
                "CONSOLIDATION": 0,
                "": 0,
            }
            p15_user_score = score_map.get(user_state, 0)
            combined_obs = f"{user_narrative.strip()} — {user_action.strip()}" if user_action.strip() else user_narrative.strip()
            if combined_obs != curr_obs or user_state != curr_vw:
                new_overrides[15] = (combined_obs, p15_user_score, user_state)

            st.markdown("---")
            st.markdown("##### 📝 Other Manual Inputs (Futures, Global, ADRs)")
            for p in params:
                if p.auto and p.no not in (8, 9):
                    continue
                if p.no == 15:
                    continue
                c1, c2 = st.columns([3, 1])
                p_curr_obs = p.observation or ""
                inp_obs = c1.text_input(f"#{p.no} {p.name}", value=p_curr_obs, key=f"vbo_{p.no}")
                inp_score = None
                inp_view = None
                if p.no in (8, 9):
                    inp_view = c2.text_input(f"#{p.no} Strong Level", value=p.view, key=f"vbv_{p.no}")
                elif p.importance != "NA":
                    lim = int(p.importance)
                    score_opts = list(range(lim, -lim - 1, -1))
                    curr_sc = p.score if p.score is not None else 0
                    idx = score_opts.index(curr_sc) if curr_sc in score_opts else 0
                    inp_score = c2.selectbox("Score", score_opts, index=idx, key=f"vbs_{p.no}")
                if inp_obs != p_curr_obs or (inp_score is not None and inp_score != p.score) or (inp_view is not None and inp_view != p.view):
                    new_overrides[p.no] = (inp_obs, inp_score, inp_view)

            if st.form_submit_button("Update Sheet & Regenerate Image", use_container_width=True):
                st.session_state.vb_overrides.update(new_overrides)
                st.rerun()

    # ═══════════════════════════════════════════════════════════════════════
    # DATA FRESHNESS AUDIT
    # ═══════════════════════════════════════════════════════════════════════
    with st.expander("🔍 Scraper Freshness & Contract Age Audit", expanded=False):
        try:
            _vix_row = next((r for r in (vb_indices or []) if "VIX" in str(r.get("index", "")).upper()), None)
            _ohlc_last = None
            if vb_daily is not None and len(vb_daily):
                _lr = vb_daily.iloc[-1]
                _age = (pd.Timestamp.today().normalize() - _lr["Date"]).days
                _ohlc_last = {"close": float(_lr["Close"]), "date": f"{_lr['Date']:%d-%b-%Y}",
                              "note": f"{_age} days old" if _age > 4 else "current", "ok": _age <= 4}
            _stamps = md.freshness_report(
                gift=vb_gift, nf=vb_nf, bnf=vb_bnf, crude_nse=vb_crude_nse,
                crude_proxy=vb_crude, poi=vb_poi, glob=vb_glob, adrs=vb_adrs,
                ad_n=vb_ad_n, vix_row=_vix_row, vix_hist=vb_vixh,
                chain_ts=nse_ts, ohlc_last=_ohlc_last
            )
            _bad = [x for x in _stamps if not x.ok]
            if _bad:
                st.warning(f"{len(_bad)} source(s) flagged — see details below")
            st.dataframe(pd.DataFrame([{
                "": "⚠️" if not x.ok else "✓",
                "Parameter": x.param,
                "Source": x.source,
                "Value": x.value,
                "Contract / Timestamp": x.stamp,
                "Note": x.age_note,
            } for x in _stamps]), hide_index=True, use_container_width=True)
        except Exception as _fe:
            st.caption(f"Freshness report unavailable: {_fe}")
