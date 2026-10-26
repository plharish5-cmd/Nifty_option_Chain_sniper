// PeceMatrix.tsx — Institutional PE-CE Difference & Skew Matrix 2.0.
// Surpasses commercial platforms like StockMojo with:
//   1. Auto-ATM Strike Range Filtering (ATM ±10, ATM ±5, Full Chain)
//   2. Center-Zero Diverging Visual Bars (Day Net Bar + Interval Impulse Bar)
//   3. Magnitude Position-Sizing & Regime Classifier (<5L, 5L-20L, 20L-30L, >30L)
//   4. 15-30m Slope Lead-Time Reversal Detector
//   5. Equidistant ATM PE vs CE Premium Skew Meter
//   6. Futures LTP + Day OI Change + Buildup Classification (LB/SB/SC/LU)
//   7. 6-Stage Action Lifecycle Verdicts
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { inr, playOiBeep } from "./OcLab";

export interface MatrixRow {
  time: number;
  time_label: string;
  put_oi_day_chg: number;
  put_oi_step_chg: number;
  call_oi_day_chg: number;
  call_oi_step_chg: number;
  pe_ce_day_diff: number;
  pe_ce_step_diff: number;
  pcr_day: number;
  fut_ltp: number;
  fut_oi_day_chg: number;
  fut_buildup: "LONG_BUILDUP" | "SHORT_BUILDUP" | "SHORT_COVERING" | "LONG_UNWINDING" | "NEUTRAL";
  stage_label: string;
}

export interface ExpiryPlaybookPayload {
  ok: boolean;
  is_expiry_day: boolean;
  force_expiry_mode?: boolean;
  expiry: string;
  time_label?: string;
  spot?: number;
  vwap?: number;
  straddle?: {
    atm_strike: number;
    atm_ce_ltp: number;
    atm_pe_ltp: number;
    atm_straddle_ltp: number;
    open_straddle_ltp: number;
    straddle_decay_pct: number;
    upper_breakeven: number;
    lower_breakeven: number;
    pin_zone_high: number;
    pin_zone_low: number;
  };
  pin?: {
    pin_state: string;
    pin_label: string;
    pin_text: string;
    pin_strike: number;
    pin_zone_high: number;
    pin_zone_low: number;
    dist_to_pin: number;
    is_pinned: boolean;
  };
  trapped?: {
    trapped_side: string;
    trapped_strike: number;
    unwinding_str: string;
    fortified_str: string;
    is_covering?: boolean;
    delta_available?: boolean;
    covering_step_oi?: number;
    covering_day_oi?: number;
    put_floor_strike?: number;
    call_ceiling_strike?: number;
  };
  flow?: {
    pe_ce_day_diff: number;
    pe_ce_step_diff: number;
    source: string;
    slot_rvol: number;
    c_rvol: number;
    rvol_available: boolean;
    snapshots_in_session: number;
  };
  hero_zero?: {
    state: string;
    score: number;
    checklist: {
      window: boolean;
      writer_squeeze: boolean;
      breakout: boolean;
      rvol: boolean;
      sweet_spot: boolean;
    };
    contract: string;
    opt_type: string;
    chosen_strike: number;
    badge: string;
    message: string;
    entry_ltp: number;
    sl_ltp: number;
    tp1_ltp: number;
    tp2_ltp: number;
    index_sl: number;
  };
  settlement?: {
    active: boolean;
    samples: number;
    estimated_settlement_price: number;
    itm_ce_strikes: number[];
    itm_pe_strikes: number[];
    message: string;
  };
  message?: string;
}

export interface PeceMatrixPayload {
  ok: boolean;
  day: string;
  expiry: string;
  tf: string;
  scope: "atm_10" | "atm_5" | "full";
  strike_range_label: string;
  spot: number;
  atm_strike: number;
  regime: {
    tier_key: "BALANCED_RANGE" | "MODERATE_CONVICTION" | "LARGE_HIGH_CONVICTION" | "EXTREME_CROWDED_ALERT";
    tier_text: string;
    sizing_mult: number;
    current_diff: number;
    slope_flip_state: "NONE" | "BULLISH_SLOPE_FLIP" | "BEARISH_SLOPE_FLIP" | "EOD_MUTUAL_UNWINDING";
    slope_flip_text: string;
  };
  skew: {
    skew_state: "STEEP_PUT_SKEW" | "NORMAL_PUT_SKEW" | "FLAT_SKEW" | "CALL_SKEW_NARROWING" | "EXTREME_CALL_SKEW";
    skew_label: string;
    skew_pct: number;
    pe_ltp: number;
    ce_ltp: number;
    pe_extrinsic?: number;
    ce_extrinsic?: number;
    interpretation: string;
  };
  first_hour: {
    bias: "BULLISH_FIRST_HOUR" | "BEARISH_FIRST_HOUR" | "BALANCED_FIRST_HOUR" | "FORMING";
    label: string;
    diff_l: number;
    text: string;
    anchor_hhmm: string;
  };
  rows: MatrixRow[];
  snapshots: number;
  expiry_playbook?: ExpiryPlaybookPayload;
  message?: string;
}

// ───────────────────────────── Hover Guidance: what each number means and what to DO ─────────────────────────────
// Every string answers three questions in order: what it measures · what action it implies · when to stand aside.
// Alert-only desk: these are read-aloud trade rules, never order instructions.
export const HOVER = {
  // ── Main ledger columns ──
  time:
    "Snapshot time (IST), newest on top.\n\nUSE: Compare consecutive rows — direction matters more than any single row.\nFirst 30 min (09:15–09:45) is noise-heavy; the 10:30 anchor is the first trustworthy read.\nWAIT: Do not act on a single row — demand 2–3 rows pointing the same way.",
  putOi:
    "PUT OI (FLOOR) — total Put open interest and its change.\n\nRISING (green) = put writers selling puts = they expect price to HOLD ABOVE. Floor building = support.\nFALLING = put writers walking away = the floor is being withdrawn; support below is thinner than it looks.\n\nDO: Rising Put OI + price holding → buy dips toward the strongest put strike.\nWAIT: Falling Put OI on a down move → no floor; stand aside, do not catch the knife.",
  callOi:
    "CALL OI (CAP) — total Call open interest and its change.\n\nRISING (red) = call writers selling calls = they expect price to STAY BELOW. Ceiling building = resistance.\nFALLING / NEGATIVE = call writers covering = the cap is being removed — the single most bullish OI event on expiry day.\n\nDO: Call OI going sharply negative while price rises → that is a genuine short-covering squeeze; CE side has room.\nWAIT: Call OI building hard into a rally → you are buying into a wall; let it break first.",
  peCeDiff:
    "PE − CE DIFFERENCE = Put OI change minus Call OI change (the day's net writer verdict).\n\nPOSITIVE & RISING = writers are net bullish (building floors, removing caps) → favour CE.\nNEGATIVE & FALLING = writers are net bearish → favour PE.\nFLAT/CHOPPY = no institutional conviction → no trade.\n\nSIZE GUIDE: under ±5Cr = weak · ±5–15Cr = tradeable · over ±30Cr = crowded, late — trail, don't initiate.\n\n⚠️ KNOWN LIMIT: this single number cannot tell PUT BUILDING apart from CALL UNWINDING — both widen it identically. Always read the Put OI and Call OI columns beside it before sizing.",
  dayNetBar:
    "DAY NET BAR — the PE−CE difference drawn from a centre-zero axis.\n\nBar growing right (green) = bullish pressure accumulating all session.\nBar growing left (red) = bearish pressure.\n\nDO: Trade WITH a bar that is visibly extending row over row.\nWAIT: A bar that stopped extending = the move is mature; manage the position you have, do not add.",
  intervalBar:
    "INTERVAL Δ BAR — what changed in THIS interval only (not cumulative).\n\nThis is your early-warning column: it turns before the Day Net Bar does.\n\nDO: Interval bar flipping against a strong day bar = first sign of exhaustion → tighten the stop, book partials.\nWAIT: Interval bars alternating green/red = chop; the desk is being whipsawed, sit out.",
  pcr:
    "PCR = total Put OI ÷ total Call OI.\n\nAbove 1.20 = put-heavy, writers defending the downside → bullish lean.\nBelow 0.80 = call-heavy, writers capping upside → bearish lean.\n0.90–1.10 = balanced, no edge.\n\nDO: Use PCR to confirm direction, never to trigger. Rising PCR + rising price = healthy trend.\n⚠️ Extreme PCR (over 1.5 or under 0.6) is a CROWD warning, not a buy signal — reversals start at extremes.",
  futBuildup:
    "NIFTY FUTURE & BUILDUP — futures price with futures OI change.\n\nLONG BUILDUP (price ↑ OI ↑) = fresh money long → strongest continuation signal.\nSHORT COVERING (price ↑ OI ↓) = shorts fleeing, not fresh buying → move is real but has a shelf life; book into strength.\nSHORT BUILDUP (price ↓ OI ↑) = fresh shorts → favour PE.\nLONG UNWINDING (price ↓ OI ↓) = longs exiting → weak, fading, not a short signal on its own.\n\nDO: Align option direction with the buildup tag. Disagreement between buildup and PE−CE = no trade.",
  actionVerdict:
    "6-STAGE ACTION VERDICT — the desk's current stage read.\n\nStage 1 Accumulation → observe only, positioning is forming.\nStage 2 Dip Buy → the better entry: buy weakness while flow stays supportive.\nStage 3 Acceleration → trend confirmed; add only with a trailing stop, never at full size.\nStage 4 Distribution → stop adding; book into strength.\nStage 5 Digestion → hold and trail, do not initiate fresh.\nStage 6 / EOD Closing (15:10+) → trail or book; no new positions.\n\nDO: Stage 2 and 3 are the only entry stages. Everything else is management.",

  // ── Executive banner ──
  sizingCard:
    "SIZING & MAGNITUDE — how much conviction the day's net PE−CE flow actually carries, converted into position size.\n\n1.0x = normal · under 1.0x = reduce · 'Extreme/Crowded' = the move is late and one-sided.\n\nDO: Let this set your lot count BEFORE you pick a strike.\nWAIT: On an Extreme (30Cr+) reading, do not chase a fresh entry — crowded positioning mean-reverts hard.",
  slopeCard:
    "SLOPE & 10:30 ANCHOR — the session's directional spine plus any flip in flow slope.\n\n10:30 anchor = the first institutionally reliable bias of the day; dips/rallies against it usually fail.\nBullish/Bearish Slope Flip = flow has reversed direction mid-session → exit positions aligned with the OLD direction.\nEOD Mutual Unwinding = BOTH sides booking into the close; day structure stays intact but there is no fresh fuel.\n\nDO: Trade in the anchor's direction until a slope flip prints.",
  skewCard:
    "EQUIDISTANT SKEW METER — PE vs CE extrinsic (time) value at equal distance from spot, after stripping intrinsic value.\n\nPUT SKEW (PE richer) = puts cost more = market paying up for downside protection = writers expect a firmer floor.\nCALL SKEW (CE richer) = upside being chased.\n\nDO: Buying the CHEAPER side gives better risk-reward when direction is already confirmed elsewhere.\n⚠️ Skew is a pricing read, not a direction signal. Never enter on skew alone.",

  // ── 0-DTE Expiry Cockpit ──
  cockpitHeader:
    "0-DTE EXPIRY COCKPIT — expiry-day physics override normal intraday rules.\n\nTheta destroys option buyers while price is pinned; gamma pays them explosively once a written strike breaks.\nThis strip tells you which of those two regimes you are in right now.",
  spotVwap:
    "SPOT · ATM STRIKE · SESSION VWAP.\n\nVWAP is the institutional fair-value line of the day.\n\nDO: Only buy CE while price holds ABOVE VWAP; only buy PE while it holds BELOW. Price oscillating across VWAP = no-trade chop.",
  straddleCard:
    "ATM STRADDLE = ATM Call premium + ATM Put premium — the market's own priced-in range for the day.\n\nIt is the cost of being wrong about direction, and on expiry day it melts by the hour.",
  decayPct:
    "STRADDLE DECAY % — how much of the morning straddle premium theta has already eaten.\n\nDecay accelerating while price goes nowhere = option BUYERS are being bled; this is the writers' day.\n\nDO: Decay over 15% with price inside the pin box → do not buy naked options at all.\nDO: Decay STALLING or reversing (premium expanding) after 12:30 → gamma is beating theta; that is the real entry cue.",
  breakevenBox:
    "MM BREAKEVEN BOX = ATM ± full straddle premium. The range market makers priced in this morning.\n\nInside the box: option sellers are winning; buyers pay theta for nothing.\nOutside the box: sellers are losing and must hedge — that is where multi-bagger moves live.\n\nDO: Treat the upper edge as the CE target and the lower edge as the PE target, not as an entry.",
  pinBox:
    "PIN TRAP RANGE = ATM ± 35% of the straddle. The magnet zone where market makers hedge price back to the strike.\n\nDO NOT buy naked CE or PE while price sits inside this band — every small move gets absorbed and you pay theta for the privilege.\nWAIT for a decisive close OUTSIDE this band, then trade the escape direction.",
  pinState:
    "PIN vs ESCAPE STATE.\n\n🧲 GAMMA PIN LOCKED = price magnetised to the strike, premium melting → NO naked buying. Sit out or trade only writer strategies.\n🚀 BREAKING UPPER = escaping toward the upper breakeven → CE side, trade the expansion.\n🩸 BREAKING LOWER = escaping toward the lower breakeven → PE side.\n🔓 UNPINNED = open range, no pin lock; normal intraday rules apply.",
  trappedCard:
    "TRAPPED OPTION WRITERS — who is underwater and whether they are actually covering.\n\nCALL writers trapped = price has run above their strike; when they cover they must BUY, fuelling the up move → CE.\nPUT writers trapped = price below their strike; their covering fuels the down move → PE.\n\n⚠️ 'STANDING, not covering' means the wall is big but has NOT started evacuating — that is NOT a squeeze. Only 'COVERING' (interval ΔOI clearing the unwind threshold) is tradeable.",
  wallsLine:
    "PUT FLOOR & CALL CEILING — the largest real OI walls above and below spot, read straight from the chain.\n\nDO: Use the put floor as your downside invalidation and the call ceiling as your upside target.\nA break and HOLD beyond either wall is the highest-quality expiry-day trade there is.",
  heroZeroCard:
    "0-DTE HERO-ZERO SNIPER — the one expiry setup worth buying options for: trapped writers forced to cover between roughly 12:45 and 14:50 IST.\n\nOutside that window, theta wins and this card should be ignored.",
  checklist:
    "5-FACTOR CHECKLIST — every gate must be real before the ticket is valid.\n\nwindow = inside the 12:45–14:50 IST gamma window\nwriter squeeze = PE−CE flow confirms one side is capitulating\nbreakout = price through the 30m extreme and on the right side of VWAP\nrvol = genuine volume participation (RVOL ≥ 1.4 slot / 1.15 cumulative)\nsweet spot = option priced ₹10–₹75 (never ₹2 lottery tickets)\n\nDO: 5/5 = full planned size. 4/5 = half size. 3/5 or less = NO TRADE, keep watching.",
  ticket:
    "EXECUTION TICKET — strike, entry premium, stop and targets.\n\nPREMIUM SL −35%: hard stop on the option price; on 0-DTE a premium can halve in minutes, so this stop is non-negotiable.\nT1 = 2x → book HALF and trail the stop to cost; the rest is free.\nT2 = 3.5x → book the remainder.\nHARD TIME EXIT 14:55 IST regardless of price.\n\n⚠️ Alert-only: this desk never places orders. Size so a total loss of the premium is survivable.",
  settlement:
    "NSE SETTLEMENT ESTIMATE — expiry settlement is the average NIFTY spot across 15:00–15:30 IST, not the 15:30 print.\n\nBy 15:10 roughly a third of it is already locked, by 15:20 two thirds — so ATM premiums stop responding to price.\n\nDO: Close all 0-DTE positions by 15:05. Holding past that is a coin flip you cannot manage; OTM options settle at ₹0.05.",
} as const;

// ────────────────────────────────────────── Center-Zero Diverging Bars ──────────────────────────────────────────
export function DayNetBar({ val, maxVal }: { val: number; maxVal: number }) {
  const normMax = Math.max(1000000, maxVal);
  const pct = Math.min(100, Math.max(0, (Math.abs(val) / normMax) * 50));
  const isPos = val >= 0;

  return (
    <div className="relative flex items-center h-4 w-28 bg-[#060b16] rounded border border-slate-800/80 overflow-hidden shadow-inner">
      {/* Center 0 Axis */}
      <div className="absolute left-1/2 top-0 bottom-0 w-[1px] bg-slate-600 z-10 opacity-70" />

      {/* Bar extending Left if Negative, Right if Positive */}
      {val !== 0 && (
        <div
          className={`absolute top-0.5 bottom-0.5 rounded-sm transition-all duration-300 ${
            isPos
              ? "left-1/2 bg-gradient-to-r from-emerald-600 to-emerald-400 shadow-[0_0_8px_rgba(16,185,129,0.5)]"
              : "right-1/2 bg-gradient-to-l from-rose-600 to-rose-400 shadow-[0_0_8px_rgba(239,68,68,0.5)]"
          }`}
          style={{ width: `${pct}%` }}
        />
      )}
    </div>
  );
}

export function IntervalImpulseBar({ val, maxVal }: { val: number; maxVal: number }) {
  const normMax = Math.max(500000, maxVal);
  const pct = Math.min(100, Math.max(0, (Math.abs(val) / normMax) * 50));
  const isPos = val >= 0;

  return (
    <div className="relative flex items-center h-4 w-20 bg-[#060b16] rounded border border-slate-800/80 overflow-hidden shadow-inner">
      {/* Center 0 Axis */}
      <div className="absolute left-1/2 top-0 bottom-0 w-[1px] bg-slate-600 z-10 opacity-70" />

      {/* Interval Capsule */}
      {val !== 0 && (
        <div
          className={`absolute top-0.5 bottom-0.5 rounded-sm transition-all duration-300 ${
            isPos
              ? "left-1/2 bg-emerald-400 shadow-[0_0_6px_rgba(16,185,129,0.6)]"
              : "right-1/2 bg-rose-400 shadow-[0_0_6px_rgba(239,68,68,0.6)]"
          }`}
          style={{ width: `${pct}%` }}
        />
      )}
    </div>
  );
}

export interface PeceMatrixProps {
  tick?: number;
  onClose?: () => void;
}

export default function PeceMatrix({ tick = 0 }: PeceMatrixProps) {
  const [tf, setTf] = useState<string>("3m");
  const [scope, setScope] = useState<"atm_10" | "atm_5" | "full">(() => {
    try {
      const saved = localStorage.getItem("ict_pece_scope");
      if (saved === "atm_10" || saved === "atm_5" || saved === "full") return saved;
    } catch { /* ignore */ }
    return "atm_10";
  });
  const [expiryTab, setExpiryTab] = useState<"cur" | "next" | "both">("cur");
  const [displayMode, setDisplayMode] = useState<"day_chg" | "total_oi">("day_chg");

  // Audio chimes
  const [soundOn, setSoundOn] = useState<boolean>(() => {
    try { return localStorage.getItem("oi_beep_alerts") !== "0"; } catch { return true; }
  });

  const toggleSound = () => {
    setSoundOn((prev) => {
      const next = !prev;
      try { localStorage.setItem("oi_beep_alerts", next ? "1" : "0"); } catch { /* ignore */ }
      return next;
    });
  };

  // 0-DTE Expiry Playbook Mode Toggle
  const [forceExpiryMode, setForceExpiryMode] = useState<boolean>(() => {
    try { return localStorage.getItem("ict_force_expiry_mode") === "1"; } catch { return false; }
  });

  const toggleForceExpiryMode = () => {
    setForceExpiryMode((prev) => {
      const next = !prev;
      try { localStorage.setItem("ict_force_expiry_mode", next ? "1" : "0"); } catch { /* ignore */ }
      return next;
    });
  };

  const [data, setData] = useState<PeceMatrixPayload | null>(null);
  const [busy, setBusy] = useState<boolean>(false);
  const lastTopTsRef = useRef<string | null>(null);

  const fetchMatrix = useCallback(() => {
    setBusy(true);
    fetch(`/api/chain/pece-matrix?expiry=${expiryTab}&tf=${tf}&scope=${scope}&force_expiry_mode=${forceExpiryMode}`)
      .then((r) => r.json())
      .then((json: PeceMatrixPayload) => {
        if (json && json.ok) {
          setData(json);
        }
      })
      .catch((e) => console.error("fetch pece-matrix error:", e))
      .finally(() => setBusy(false));
  }, [expiryTab, tf, scope, forceExpiryMode]);

  useEffect(() => {
    fetchMatrix();
    const interval = window.setInterval(fetchMatrix, 15000); // 15s refresh
    return () => window.clearInterval(interval);
  }, [fetchMatrix, tick]);

  // Audio chime trigger on new row or slope flip
  useEffect(() => {
    if (!data?.rows || data.rows.length === 0) return;
    const topRow = data.rows[0];
    if (topRow.time_label === "Prev Day") return;

    if (lastTopTsRef.current && topRow.time_label !== lastTopTsRef.current) {
      if (soundOn) {
        if (data.regime.slope_flip_state === "BULLISH_SLOPE_FLIP") {
          playOiBeep("angleBull");
        } else if (data.regime.slope_flip_state === "BEARISH_SLOPE_FLIP") {
          playOiBeep("angleBear");
        } else if (
          topRow.stage_label.includes("Dip Buy") ||
          topRow.stage_label.includes("Bullish Acceleration") ||
          topRow.stage_label.includes("Bullish Breakout")
        ) {
          playOiBeep("angleBull");
        } else if (
          topRow.stage_label.includes("Pullback Sell") ||
          topRow.stage_label.includes("Bearish Acceleration") ||
          topRow.stage_label.includes("Bearish Breakdown")
        ) {
          playOiBeep("angleBear");
        } else if (
          topRow.stage_label.includes("Book") ||
          topRow.stage_label.includes("Digestion")
        ) {
          playOiBeep("scissor");
        }
      }
    }
    lastTopTsRef.current = topRow.time_label;
  }, [data, soundOn]);

  const setScopeFilter = (s: "atm_10" | "atm_5" | "full") => {
    setScope(s);
    try { localStorage.setItem("ict_pece_scope", s); } catch { /* ignore */ }
  };

  const rows = data?.rows || [];

  // Max values for auto-scaling center-zero diverging bars
  const maxDayDiff = useMemo(() => {
    const vals = rows.map((r) => Math.abs(r.pe_ce_day_diff)).filter((v) => v > 0);
    return vals.length ? Math.max(...vals) : 2000000;
  }, [rows]);

  const maxStepDiff = useMemo(() => {
    const vals = rows.map((r) => Math.abs(r.pe_ce_step_diff)).filter((v) => v > 0);
    return vals.length ? Math.max(...vals) : 1000000;
  }, [rows]);

  const th = "sticky top-0 z-20 bg-[#070d18] px-2.5 py-1.5 text-[11px] font-bold uppercase tracking-wider text-slate-400 whitespace-nowrap border-b border-[#1b273d]";
  const td = "px-2.5 py-1.5 whitespace-nowrap text-[11.5px] border-b border-[#111c2e]";

  return (
    <div className="flex h-full flex-col bg-[#070c17] text-slate-200 select-none overflow-hidden">
      {/* ────────────────────────────────── Top Control Bar ────────────────────────────────── */}
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-[#162238] bg-[#091122] px-3 py-1.5 text-[11px]">
        {/* Left: Timeframe Interval Pills */}
        <div className="flex items-center gap-1">
          <span className="text-slate-400 font-bold uppercase text-[10px] mr-1">Interval:</span>
          {["1m", "3m", "5m", "15m", "1h"].map((t) => (
            <button
              key={t}
              onClick={() => setTf(t)}
              className={`rounded px-2 py-0.5 text-[11px] font-bold transition-all ${
                tf === t
                  ? "bg-sky-500/30 text-sky-200 border border-sky-500/60 shadow-sm"
                  : "bg-[#0c1424] text-slate-400 border border-slate-800 hover:text-slate-200"
              }`}
            >
              {t}
            </button>
          ))}
        </div>

        {/* Middle: Strike Scope Selector */}
        <div className="flex items-center gap-1.5">
          <span className="text-slate-400 font-bold uppercase text-[10px] mr-0.5">Scope:</span>
          <button
            onClick={() => setScopeFilter("atm_10")}
            className={`rounded px-2.5 py-0.5 text-[11px] font-bold border transition-all ${
              scope === "atm_10"
                ? "bg-emerald-500/30 text-emerald-200 border-emerald-500/60 shadow-sm"
                : "bg-[#0c1424] text-slate-400 border-slate-800 hover:text-slate-200"
            }`}
            title="Auto ATM ±10 Strikes (±500 pts) — Filters deep OTM margin hedges"
          >
            Auto ATM ±10 {data?.atm_strike ? `(${data.atm_strike - 500}–${data.atm_strike + 500})` : ""}
          </button>
          <button
            onClick={() => setScopeFilter("atm_5")}
            className={`rounded px-2.5 py-0.5 text-[11px] font-bold border transition-all ${
              scope === "atm_5"
                ? "bg-emerald-500/30 text-emerald-200 border-emerald-500/60 shadow-sm"
                : "bg-[#0c1424] text-slate-400 border-slate-800 hover:text-slate-200"
            }`}
            title="Auto ATM ±5 Strikes (±250 pts) — Fast Gamma Scalping Window"
          >
            Auto ATM ±5
          </button>
          <button
            onClick={() => setScopeFilter("full")}
            className={`rounded px-2.5 py-0.5 text-[11px] font-bold border transition-all ${
              scope === "full"
                ? "bg-emerald-500/30 text-emerald-200 border-emerald-500/60 shadow-sm"
                : "bg-[#0c1424] text-slate-400 border-slate-800 hover:text-slate-200"
            }`}
            title="Full Chain (All Strikes including deep OTM)"
          >
            Full Chain
          </button>
        </div>

        {/* Right: Expiry Filter + Sound + Refresh */}
        <div className="flex items-center gap-2">
          {/* Expiry Selector */}
          <div className="flex rounded bg-[#0c1424] p-0.5 border border-slate-800">
            {(["cur", "next", "both"] as const).map((e) => (
              <button
                key={e}
                onClick={() => setExpiryTab(e)}
                className={`rounded px-2 py-0.5 text-[11px] font-bold transition-all ${
                  expiryTab === e
                    ? "bg-sky-500/30 text-sky-200 border border-sky-500/60 shadow-sm"
                    : "text-slate-400 hover:text-slate-200"
                }`}
              >
                {e === "cur" ? "This Wk" : e === "next" ? "Next Wk" : "Both"}
              </button>
            ))}
          </div>

          {/* 0-DTE Expiry Playbook Toggle */}
          <button
            onClick={toggleForceExpiryMode}
            className={`rounded px-2 py-0.5 text-[10.5px] font-bold border transition-all ${
              data?.expiry_playbook?.is_expiry_day || forceExpiryMode
                ? "bg-amber-950/70 text-amber-300 border-amber-600/70 ring-1 ring-amber-500/30"
                : "bg-[#0c1424] text-slate-400 border-slate-800 hover:text-slate-200"
            }`}
            title="Toggle Expiry Day (0-DTE) Playbook: Gamma Pin, Breakeven Box & Hero-Zero Radar"
          >
            ⚡ 0-DTE Cockpit: {data?.expiry_playbook?.is_expiry_day || forceExpiryMode ? "ACTIVE" : "AUTO"}
          </button>

          {/* Sound Toggle */}
          <button
            onClick={toggleSound}
            className={`rounded px-2 py-0.5 text-[10.5px] font-bold border transition-all ${
              soundOn
                ? "bg-emerald-950/70 text-emerald-300 border-emerald-600/70"
                : "bg-[#0c1424] text-slate-400 border-slate-800 hover:text-slate-200"
            }`}
            title="Toggle acoustic alert chimes on Stage 2 / Slope Flips"
          >
            {soundOn ? "🔊 Audio: ON" : "🔇 Audio: OFF"}
          </button>

          <button
            onClick={fetchMatrix}
            className="rounded bg-[#0c1424] px-2 py-0.5 text-[11px] font-bold text-slate-300 border border-slate-800 hover:bg-slate-800"
            title="Refresh now"
          >
            {busy ? "⟳" : "↻"}
          </button>
        </div>
      </div>

      {/* ────────────────────────────────── ⚡ EXPIRY 0-DTE GAMMA & HERO-ZERO COCKPIT STRIP ────────────────────────────────── */}
      {(data?.expiry_playbook?.is_expiry_day || forceExpiryMode) && data?.expiry_playbook?.ok && (
        <div className="border-b border-[#2a1c0d] bg-gradient-to-r from-[#140c06] via-[#1a1008] to-[#0c0704] p-2.5 shadow-lg">
          {/* Cockpit Header Line */}
          <div className="flex flex-wrap items-center justify-between gap-2 mb-2 pb-1.5 border-b border-amber-900/40 text-[11px]">
            <div className="flex items-center gap-2">
              <span className="flex items-center gap-1 font-black uppercase tracking-wider text-amber-400 bg-amber-950/80 px-2 py-0.5 rounded border border-amber-600/60 shadow-sm animate-pulse cursor-help" title={HOVER.cockpitHeader}>
                ⚡ 0-DTE EXPIRY COCKPIT
              </span>
              <span className="font-mono text-slate-300 text-[10.5px]">
                {data.expiry_playbook.expiry} ({data.expiry_playbook.time_label || "15:27"} IST)
              </span>
              <span className="text-slate-500">|</span>
              <span className="text-slate-300 text-[11px] cursor-help" title={HOVER.spotVwap}>
                Spot: <b className="text-white font-mono">{data.expiry_playbook.spot?.toFixed(1)}</b> · ATM Strike: <b className="text-amber-300 font-mono">{data.expiry_playbook.straddle?.atm_strike}</b> · Session VWAP: <b className="text-sky-300 font-mono">{data.expiry_playbook.vwap?.toFixed(1)}</b>
              </span>
            </div>

            <div className="flex items-center gap-2">
              <span
                title={HOVER.pinState}
                className={`cursor-help px-2 py-0.5 rounded text-[10px] font-black border ${
                  data.expiry_playbook.pin?.is_pinned
                    ? "bg-purple-950 text-purple-200 border-purple-500/80 ring-1 ring-purple-500/30"
                    : (data.expiry_playbook.pin?.pin_state || "").includes("UPPER")
                    ? "bg-emerald-950 text-emerald-300 border-emerald-500"
                    : (data.expiry_playbook.pin?.pin_state || "").includes("LOWER")
                    ? "bg-rose-950 text-rose-300 border-rose-500"
                    : "bg-slate-900 text-slate-300 border-slate-700"
                }`}
              >
                {data.expiry_playbook.pin?.pin_label}
              </span>
            </div>
          </div>

          {/* 3 Cockpit Cards Grid */}
          <div className="grid grid-cols-1 md:grid-cols-3 gap-2.5">
            {/* Card 1: ATM Straddle & Breakeven Box */}
            <div className="rounded-lg border border-amber-900/50 bg-[#120a05] p-2.5 shadow flex flex-col justify-between">
              <div className="flex items-center justify-between text-[11px] mb-1">
                <span className="font-extrabold uppercase tracking-wider text-amber-300/90 cursor-help" title={HOVER.straddleCard}>
                  🧮 ATM Straddle & Decay
                </span>
                <span
                  title={HOVER.decayPct}
                  className={`cursor-help px-1.5 py-0.5 rounded text-[10px] font-mono font-bold border ${
                    (data.expiry_playbook.straddle?.straddle_decay_pct ?? 0) >= 20
                      ? "bg-emerald-950 text-emerald-300 border-emerald-700"
                      : "bg-slate-900 text-slate-300 border-slate-700"
                  }`}
                >
                  🔻 -{data.expiry_playbook.straddle?.straddle_decay_pct?.toFixed(1)}% Decay
                </span>
              </div>
              <div>
                <div className="text-xs font-black text-slate-100 flex items-center justify-between mb-1">
                  <span>
                    Premium: <b className="text-amber-400 font-mono">₹{data.expiry_playbook.straddle?.atm_straddle_ltp?.toFixed(1)}</b>
                    <span className="text-[10px] text-slate-400 ml-1 font-normal">
                      (CE ₹{data.expiry_playbook.straddle?.atm_ce_ltp} + PE ₹{data.expiry_playbook.straddle?.atm_pe_ltp})
                    </span>
                  </span>
                  <span className="text-[10px] text-slate-500 font-mono">
                    Open: ₹{data.expiry_playbook.straddle?.open_straddle_ltp?.toFixed(1)}
                  </span>
                </div>
                <div className="text-[10.5px] text-slate-300 space-y-0.5 font-mono">
                  <div className="flex justify-between cursor-help" title={HOVER.breakevenBox}>
                    <span className="text-slate-400">MM Breakeven Box:</span>
                    <span className="text-amber-200 font-bold">
                      {data.expiry_playbook.straddle?.lower_breakeven} ↔ {data.expiry_playbook.straddle?.upper_breakeven}
                    </span>
                  </div>
                  <div className="flex justify-between cursor-help" title={HOVER.pinBox}>
                    <span className="text-slate-400">Pin Trap Range:</span>
                    <span className="text-purple-300 font-bold">
                      {data.expiry_playbook.straddle?.pin_zone_low} ↔ {data.expiry_playbook.straddle?.pin_zone_high}
                    </span>
                  </div>
                </div>
              </div>
            </div>

            {/* Card 2: Trapped Writers Radar */}
            <div className="rounded-lg border border-amber-900/50 bg-[#120a05] p-2.5 shadow flex flex-col justify-between">
              <div className="flex items-center justify-between text-[11px] mb-1">
                <span className="font-extrabold uppercase tracking-wider text-amber-300/90 cursor-help" title={HOVER.trappedCard}>
                  🎯 Trapped Option Writers
                </span>
                <span
                  title={HOVER.trappedCard}
                  className={`cursor-help px-1.5 py-0.5 rounded text-[10px] font-black border ${
                    !data.expiry_playbook.trapped?.is_covering
                      ? "bg-slate-900 text-amber-400/90 border-amber-800/70"
                      : data.expiry_playbook.trapped?.trapped_side === "CALLS"
                      ? "bg-emerald-950 text-emerald-300 border-emerald-600 animate-pulse"
                      : data.expiry_playbook.trapped?.trapped_side === "PUTS"
                      ? "bg-rose-950 text-rose-300 border-rose-600 animate-pulse"
                      : "bg-slate-900 text-slate-400 border-slate-700"
                  }`}
                >
                  {data.expiry_playbook.trapped?.trapped_side === "CALLS"
                    ? data.expiry_playbook.trapped?.is_covering
                      ? "🔥 CALL WRITERS COVERING"
                      : "⚠️ CALL WRITERS STANDING"
                    : data.expiry_playbook.trapped?.trapped_side === "PUTS"
                    ? data.expiry_playbook.trapped?.is_covering
                      ? "🩸 PUT WRITERS COVERING"
                      : "⚠️ PUT WRITERS STANDING"
                    : "BALANCED WRITERS"}
                </span>
              </div>
              <div>
                <div className="text-[11px] font-bold text-amber-200 mb-0.5 cursor-help" title={HOVER.trappedCard}>
                  {data.expiry_playbook.trapped?.unwinding_str}
                </div>
                <div className="text-[10.5px] text-slate-300 font-mono cursor-help" title={HOVER.wallsLine}>
                  {data.expiry_playbook.trapped?.fortified_str}
                </div>
                <div className="text-[10px] text-slate-400 mt-1 italic leading-tight cursor-help" title={HOVER.pinState}>
                  {data.expiry_playbook.pin?.pin_text}
                </div>
              </div>
            </div>

            {/* Card 3: Hero-Zero Sniper Ticket */}
            <div className="rounded-lg border border-amber-900/50 bg-[#120a05] p-2.5 shadow flex flex-col justify-between">
              <div className="flex items-center justify-between text-[11px] mb-1">
                <span className="font-extrabold uppercase tracking-wider text-amber-300/90 cursor-help" title={HOVER.heroZeroCard}>
                  🚀 0-DTE Hero-Zero Sniper
                </span>
                <span
                  className={`px-1.5 py-0.5 rounded text-[10px] font-black border ${
                    data.expiry_playbook.hero_zero?.score === 5
                      ? "bg-emerald-950 text-emerald-300 border-emerald-500 ring-1 ring-emerald-500/40 animate-pulse"
                      : data.expiry_playbook.hero_zero?.score === 4
                      ? "bg-sky-950 text-sky-300 border-sky-500"
                      : "bg-slate-900 text-slate-400 border-slate-800"
                  }`}
                >
                  {data.expiry_playbook.hero_zero?.badge}
                </span>
              </div>
              <div>
                {/* 5-Factor Checklist Pills */}
                <div className="flex flex-wrap items-center gap-1 mb-1.5 cursor-help" title={HOVER.checklist}>
                  {Object.entries(data.expiry_playbook.hero_zero?.checklist || {}).map(([key, val]) => (
                    <span
                      key={key}
                      className={`text-[9.5px] px-1 py-0.2 rounded border font-mono ${
                        val
                          ? "bg-emerald-950/80 text-emerald-300 border-emerald-600/80"
                          : "bg-slate-900/80 text-slate-500 border-slate-800"
                      }`}
                    >
                      {val ? "✓" : "✗"} {key.replace("_", " ")}
                    </span>
                  ))}
                </div>

                {data.expiry_playbook.hero_zero?.state?.startsWith("ACTIVE_HERO_ZERO_BUY_") ? (
                  <div className="text-[11px] bg-[#1a0e06] p-1.5 rounded border border-amber-700/50 space-y-0.5 cursor-help" title={HOVER.ticket}>
                    <div className="font-black text-amber-300 flex justify-between">
                      <span>BUY {data.expiry_playbook.hero_zero.contract}</span>
                      <span className="text-white font-mono">₹{data.expiry_playbook.hero_zero.entry_ltp}</span>
                    </div>
                    <div className="text-[10px] font-mono text-slate-300 flex justify-between">
                      <span className="text-rose-400">Prem SL: ₹{data.expiry_playbook.hero_zero.sl_ltp} (-35%)</span>
                      <span className="text-emerald-400">T1: ₹{data.expiry_playbook.hero_zero.tp1_ltp} (2x) · T2: ₹{data.expiry_playbook.hero_zero.tp2_ltp} (3.5x)</span>
                    </div>
                  </div>
                ) : (
                  <div className="text-[10.5px] text-slate-300 leading-snug cursor-help" title={HOVER.ticket}>
                    {data.expiry_playbook.hero_zero?.message}
                  </div>
                )}

                {/* Telemetry provenance — a failed gate must never be mistaken for a quiet market */}
                {data.expiry_playbook.flow && !data.expiry_playbook.flow.rvol_available && (
                  <div
                    className="mt-1 text-[9.5px] text-amber-500/90 italic leading-tight cursor-help"
                    title={
                      "RVOL is not being supplied on this request, so the volume gate reads ✗ by design.\n\n" +
                      "This is a DATA state, not a market state — it does not mean volume is low.\n" +
                      "Live RVOL reaches the sniper through the hub alert path during market hours.\n\n" +
                      "DO: Treat a 4/5 with ✗ rvol as 'unverified', confirm volume on the chart yourself before sizing."
                    }
                  >
                    ⓘ RVOL not supplied on this request — volume gate unverified (not a low-volume reading)
                  </div>
                )}

                {/* Settlement VWAP Tracker if active */}
                {data.expiry_playbook.settlement?.active && (
                  <div className="mt-1 pt-1 border-t border-amber-900/40 text-[10px] font-mono text-sky-300 flex justify-between cursor-help" title={HOVER.settlement}>
                    <span>NSE Settlement Est:</span>
                    <span className="font-bold text-white">
                      ₹{data.expiry_playbook.settlement.estimated_settlement_price?.toFixed(2)} ({data.expiry_playbook.settlement.samples}/30 samples)
                    </span>
                  </div>
                )}
              </div>
            </div>
          </div>
        </div>
      )}

      {/* ────────────────────────────────── Executive Regime, Magnitude & Skew Banner (3 Cards) ────────────────────────────────── */}
      <div className="grid grid-cols-1 md:grid-cols-3 gap-2.5 p-2.5 border-b border-[#162238] bg-[#070d1a]">
        {/* Card A: Magnitude & Position-Sizing Verdict */}
        <div className="rounded-lg border border-slate-800 bg-gradient-to-b from-[#0a1224] to-[#060b16] p-2.5 shadow flex flex-col justify-between">
          <div className="flex items-center justify-between text-[11px] mb-1">
            <span className="font-extrabold uppercase tracking-wider text-slate-400 cursor-help" title={HOVER.sizingCard}>
              ⚡ Sizing & Magnitude
            </span>
            <span
              className={`px-2 py-0.5 rounded text-[10px] font-black border ${
                data?.regime.tier_key === "LARGE_HIGH_CONVICTION"
                  ? "bg-emerald-950 text-emerald-300 border-emerald-500/80 ring-1 ring-emerald-500/30"
                  : data?.regime.tier_key === "EXTREME_CROWDED_ALERT"
                  ? "bg-amber-950 text-amber-300 border-amber-500/80 ring-1 ring-amber-500/30"
                  : data?.regime.tier_key === "MODERATE_CONVICTION"
                  ? "bg-sky-950 text-sky-300 border-sky-500/80"
                  : "bg-slate-900 text-slate-400 border-slate-800"
              }`}
            >
              {data?.regime.sizing_mult ? `${data.regime.sizing_mult}x Size` : "1.0x Size"}
            </span>
          </div>
          <div>
            <div className="text-sm font-black text-slate-100 flex items-center gap-1.5 mb-1">
              <span className={data?.regime.current_diff && data.regime.current_diff > 0 ? "text-emerald-400" : "text-rose-400"}>
                {inr(data?.regime.current_diff, true)}
              </span>
              <span className="text-xs font-semibold text-slate-400">Net Day Difference</span>
            </div>
            <div className="text-[11px] text-slate-300 leading-snug">
              {data?.regime.tier_text || "Evaluating institutional conviction..."}
            </div>
          </div>
        </div>

        {/* Card B: Slope & 10:30 First-Hour Bias */}
        <div className="rounded-lg border border-slate-800 bg-gradient-to-b from-[#0a1224] to-[#060b16] p-2.5 shadow flex flex-col justify-between">
          <div className="flex items-center justify-between text-[11px] mb-1">
            <span className="font-extrabold uppercase tracking-wider text-slate-400 cursor-help" title={HOVER.slopeCard}>
              📐 Slope & 10:30 Anchor
            </span>
            {data?.regime.slope_flip_state !== "NONE" && (
              <span
                className={`px-2 py-0.5 rounded text-[9.5px] font-black border ${
                  data?.regime.slope_flip_state === "BULLISH_SLOPE_FLIP"
                    ? "bg-emerald-950 text-emerald-300 border-emerald-500 ring-1 ring-emerald-500/40 animate-pulse"
                    : data?.regime.slope_flip_state === "EOD_MUTUAL_UNWINDING"
                    ? "bg-slate-900 text-sky-300 border-sky-600/70"
                    : "bg-rose-950 text-rose-300 border-rose-500 ring-1 ring-rose-500/40 animate-pulse"
                }`}
              >
                {data?.regime.slope_flip_state === "BULLISH_SLOPE_FLIP"
                  ? "🚀 Bullish Flip"
                  : data?.regime.slope_flip_state === "EOD_MUTUAL_UNWINDING"
                  ? "🏁 EOD Unwinding"
                  : "🩸 Bearish Flip"}
              </span>
            )}
          </div>
          <div>
            <div className="text-xs font-bold text-sky-300 mb-1 flex items-center gap-1">
              <span>{data?.first_hour.label || "10:30 Anchor Forming"}</span>
            </div>
            <div className="text-[11px] text-slate-300 leading-snug">
              {data?.regime.slope_flip_text || data?.first_hour.text || "Tracking slope momentum across session."}
            </div>
          </div>
        </div>

        {/* Card C: ATM PE vs CE Premium Skew Meter */}
        <div className="rounded-lg border border-slate-800 bg-gradient-to-b from-[#0a1224] to-[#060b16] p-2.5 shadow flex flex-col justify-between">
          <div className="flex items-center justify-between text-[11px] mb-1">
            <span className="font-extrabold uppercase tracking-wider text-slate-400 cursor-help" title={HOVER.skewCard}>
              ⚖️ Equidistant Skew Meter
            </span>
            <span className="font-mono text-[10px] text-slate-400">
              {data?.skew.pe_extrinsic !== undefined && data?.skew.pe_extrinsic !== data?.skew.pe_ltp
                ? `Extrinsic: PE ₹${data.skew.pe_extrinsic} vs CE ₹${data.skew.ce_extrinsic}`
                : `PE ₹${data?.skew.pe_ltp ?? 0} vs CE ₹${data?.skew.ce_ltp ?? 0}`}
            </span>
          </div>
          <div>
            <div className="text-xs font-bold text-amber-300 mb-1">
              {data?.skew.skew_label || "⚖️ Evaluating Premium Skew..."}
            </div>
            <div className="text-[11px] text-slate-300 leading-snug">
              {data?.skew.interpretation || "Comparing extrinsic Put vs Call premiums."}
            </div>
          </div>
        </div>
      </div>

      {/* ────────────────────────────────── Enhanced PE-CE Table with Center-Zero Diverging Bars ────────────────────────────────── */}
      <div className="min-h-0 flex-1 overflow-auto">
        <table className="w-full border-separate border-spacing-0 text-[11.5px] num">
          <thead>
            <tr>
              <th className={`${th} w-20 sticky left-0 z-30 cursor-help`} title={HOVER.time}>Time</th>
              <th className={`${th} w-32 cursor-help`} title={HOVER.putOi}>Put OI (Floor)</th>
              <th className={`${th} w-32 cursor-help`} title={HOVER.callOi}>Call OI (Cap)</th>
              <th className={`${th} w-28 cursor-help`} title={HOVER.peCeDiff}>PE − CE Difference</th>
              <th className={`${th} w-32 text-center cursor-help`} title={HOVER.dayNetBar}>Day Net Bar</th>
              <th className={`${th} w-24 text-center cursor-help`} title={HOVER.intervalBar}>Interval Δ Bar</th>
              <th className={`${th} w-16 text-center cursor-help`} title={HOVER.pcr}>PCR</th>
              <th className={`${th} w-44 cursor-help`} title={HOVER.futBuildup}>NIFTY Future & Buildup</th>
              <th className={`${th} min-w-[200px] cursor-help`} title={HOVER.actionVerdict}>6-Stage Action Verdict</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r, idx) => {
              const isPrevDay = r.time_label === "Prev Day";
              const isPeLead = r.pe_ce_day_diff >= 0;
              const isStepPos = r.pe_ce_step_diff >= 0;

              return (
                <tr
                  key={`${r.time_label}-${idx}`}
                  className={`border-b border-[#111c2e] transition-colors ${
                    isPrevDay
                      ? "bg-[#0b101c] opacity-60 text-slate-400"
                      : idx % 2 === 0
                      ? "bg-[#070c17] hover:bg-[#0d1627]"
                      : "bg-[#080e1b] hover:bg-[#0d1627]"
                  }`}
                >
                  {/* 1. Time */}
                  <td className={`${td} font-bold text-slate-300 sticky left-0 z-10 bg-inherit border-r border-[#152033]`}>
                    {r.time_label}
                  </td>

                  {/* 2. Put OI (Day + Step) */}
                  <td className={td}>
                    <div className="flex flex-col">
                      <span className="font-extrabold text-emerald-400">
                        {inr(r.put_oi_day_chg, true)}
                      </span>
                      {!isPrevDay && (
                        <span className={`text-[10px] ${r.put_oi_step_chg >= 0 ? "text-emerald-500" : "text-slate-500"}`}>
                          Δ {inr(r.put_oi_step_chg, true)}
                        </span>
                      )}
                    </div>
                  </td>

                  {/* 3. Call OI (Day + Step) - Color Discipline: Crimson for writing, Green for covering */}
                  <td className={td}>
                    <div className="flex flex-col">
                      <span
                        className={`font-extrabold ${
                          r.call_oi_day_chg > 0 ? "text-rose-400" : r.call_oi_day_chg < 0 ? "text-emerald-400" : "text-slate-400"
                        }`}
                      >
                        {inr(r.call_oi_day_chg, true)}
                      </span>
                      {!isPrevDay && (
                        <span
                          className={`text-[10px] ${
                            r.call_oi_step_chg > 0 ? "text-rose-500" : r.call_oi_step_chg < 0 ? "text-emerald-400" : "text-slate-500"
                          }`}
                        >
                          Δ {inr(r.call_oi_step_chg, true)}
                        </span>
                      )}
                    </div>
                  </td>

                  {/* 4. PE - CE Difference */}
                  <td className={td}>
                    <div className="flex flex-col">
                      <span
                        className={`font-black ${
                          isPeLead ? "text-emerald-300" : "text-rose-300"
                        }`}
                      >
                        {inr(r.pe_ce_day_diff, true)}
                      </span>
                      {!isPrevDay && (
                        <span className={`text-[10px] font-semibold ${isStepPos ? "text-emerald-400" : "text-rose-400"}`}>
                          ({isStepPos ? "+" : ""}{inr(r.pe_ce_step_diff)})
                        </span>
                      )}
                    </div>
                  </td>

                  {/* 5. Center-Zero Day Net Bar */}
                  <td className={`${td} text-center`}>
                    <div className="flex items-center justify-center">
                      <DayNetBar val={r.pe_ce_day_diff} maxVal={maxDayDiff} />
                    </div>
                  </td>

                  {/* 6. Center-Zero Interval Impulse Bar */}
                  <td className={`${td} text-center`}>
                    <div className="flex items-center justify-center">
                      <IntervalImpulseBar val={r.pe_ce_step_diff} maxVal={maxStepDiff} />
                    </div>
                  </td>

                  {/* 7. PCR */}
                  <td className={`${td} text-center font-mono font-bold text-sky-300`}>
                    {r.pcr_day.toFixed(2)}
                  </td>

                  {/* 8. Future LTP + Buildup */}
                  <td className={td}>
                    <div className="flex items-center gap-1.5">
                      <span className="font-mono text-slate-100 font-bold">{r.fut_ltp.toFixed(1)}</span>
                      {!isPrevDay && (
                        <span
                          className={`px-1.5 py-0.2 rounded text-[9.5px] font-black border ${
                            r.fut_buildup === "LONG_BUILDUP"
                              ? "bg-emerald-950 text-emerald-300 border-emerald-600/70"
                              : r.fut_buildup === "SHORT_BUILDUP"
                              ? "bg-rose-950 text-rose-300 border-rose-600/70"
                              : r.fut_buildup === "SHORT_COVERING"
                              ? "bg-cyan-950 text-cyan-300 border-cyan-600/70"
                              : "bg-amber-950 text-amber-300 border-amber-600/70"
                          }`}
                        >
                          {r.fut_buildup === "LONG_BUILDUP"
                            ? "LB"
                            : r.fut_buildup === "SHORT_BUILDUP"
                            ? "SB"
                            : r.fut_buildup === "SHORT_COVERING"
                            ? "SC"
                            : "LU"}
                          {r.fut_oi_day_chg !== 0 && ` ${inr(r.fut_oi_day_chg, true)}`}
                        </span>
                      )}
                    </div>
                  </td>

                  {/* 9. 6-Stage Action Verdict */}
                  <td className={td}>
                    <span
                      className={`inline-flex items-center gap-1 px-2 py-0.5 rounded text-[11px] font-black border ${
                        r.stage_label.includes("Dip Buy")
                          ? "bg-emerald-950 text-emerald-300 border-emerald-500/80 ring-1 ring-emerald-500/30"
                          : r.stage_label.includes("Pullback Sell")
                          ? "bg-rose-950 text-rose-300 border-rose-500/80 ring-1 ring-rose-500/30"
                          : r.stage_label.includes("Add CE") || r.stage_label.includes("Bullish Acceleration")
                          ? "bg-emerald-900/60 text-emerald-200 border-emerald-600/60 ring-1 ring-emerald-500/20"
                          : r.stage_label.includes("Add PE") || r.stage_label.includes("Bearish Acceleration")
                          ? "bg-rose-900/60 text-rose-200 border-rose-600/60 ring-1 ring-rose-500/20"
                          : r.stage_label.includes("Bullish Breakout")
                          ? "bg-emerald-950 text-emerald-300 border-emerald-400/80"
                          : r.stage_label.includes("Bearish Breakdown")
                          ? "bg-rose-950 text-rose-300 border-rose-400/80"
                          : r.stage_label.includes("Book") || r.stage_label.includes("Digestion")
                          ? "bg-amber-950 text-amber-300 border-amber-500/70"
                          : r.stage_label.includes("EOD")
                          ? "bg-indigo-950 text-indigo-300 border-indigo-700/70 ring-1 ring-indigo-500/30"
                          : "bg-slate-900 text-slate-400 border-slate-800"
                      }`}
                    >
                      {r.stage_label}
                    </span>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      {/* ────────────────────────────────── Bottom Status Bar ────────────────────────────────── */}
      <div className="flex flex-wrap items-center justify-between border-t border-[#162238] bg-[#070c17] px-3 py-1 text-[11px] text-slate-400">
        <div className="flex items-center gap-3">
          <span>Active Scope: <b className="text-emerald-300">{data?.strike_range_label}</b></span>
          <span>Expiry: <b className="text-sky-300">{data?.expiry}</b></span>
          <span>NIFTY Spot: <b className="text-white font-mono">{data?.spot}</b></span>
        </div>
        <div className="flex items-center gap-2 text-slate-500 text-[10px]">
          <span>Put OI = Emerald Support • Call OI = Crimson Resistance (Green on Covering)</span>
        </div>
      </div>
    </div>
  );
}
