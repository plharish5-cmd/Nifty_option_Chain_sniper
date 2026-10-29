// OcLab.tsx — two Options Lab views from the OC app (8501), served through /api/oclab:
//   PeceTable   — the PE − CE OI table row for row (🎯 LEVELS · ACTION, READ, ★ PRO READ), newest candle on top
//   WritersFlow — "OI Change (Call vs Put) — since yesterday" + writer-flow strip (Δput − Δcall, 30-min balance)
// Both refresh every 30 s; the backend recomputes only when a new snapshot has been stored (never stale).
import { type ReactNode, useCallback, useEffect, useRef, useState } from "react";
import { positioningEvents } from "./bias";
import { FlowBand } from "./flowband";
import PcrTrinitySuite from "./PcrTrinitySuite";
import PeceMatrix from "./PeceMatrix";
import SmartOISuite from "./SmartOISuite";
import { GapSniperTab } from "./GapSniperTab";
import { COUNTER_TREND_CAP, counterTrendNote, dayRegime, fightsTheDay } from "./regime";
import {
  CandlestickSeries, ColorType, createChart, createSeriesMarkers, HistogramSeries, LineSeries, LineStyle,
  type IChartApi, type ISeriesApi, type SeriesMarker, type Time, type UTCTimestamp,
} from "lightweight-charts";

export type Tup = [string, string, string?, string?];
export type Row = {
  ts: string; pe_day: number | null; pe_chg: number | null; ce_day: number | null; ce_chg: number | null;
  pe_ce_day: number | null; pe_ce_chg: number | null; pcr: number | null; vol_pcr: number | null; close: number | null;
  sup: number | null; res: number | null; lv: Tup | null; act: Tup | null; read: Tup; meaning: string; pro: Tup | null;
};
export type Pece = { ok: boolean; message?: string; range?: string; spot?: number; rows?: Row[]; snapshots?: number; day?: string };
type Pt = { t: number; hm: string; fut: number | null; ce: number | null; pe: number | null; flow: number | null; bal: number | null;
  regime: string; sig: boolean; flip: boolean; conf: string | null };
type Flow = { ok: boolean; message?: string; label?: string; expiry?: string; unwind?: [number, number] | null; read?: string; note?: string; points?: Pt[]; snapshots?: number; day?: string };

export function inr(v: number | null | undefined, sign = false): string {
  if (v == null || !isFinite(v)) return "—";
  const a = Math.abs(v), s = v < 0 ? "-" : sign && v > 0 ? "+" : "";
  if (a < 0.5) return "0";
  if (a >= 1e7) return `${s}${(a / 1e7).toFixed(2)}Cr`;
  if (a >= 1e5) return `${s}${(a / 1e5).toFixed(2)}L`;
  if (a >= 1e3) return `${s}${(a / 1e3).toFixed(1)}K`;
  return `${s}${a.toFixed(0)}`;
}
const KC: Record<string, string> = { bull: "text-bull-400", bear: "text-bear-400", skip: "text-slate-500", warn: "text-amber-300", flat: "text-slate-400" };
const KB: Record<string, string> = { bull: "bg-bull-900/40", bear: "bg-bear-900/40", skip: "bg-ink-850", flat: "" };
const clean = (s: string) => s.replace(/<[^>]+>/g, "").replace(/\*\*/g, "");

function useOc<T>(what: "pece" | "flow", q: string, tick: number): [T | null, boolean] {
  const [d, setD] = useState<T | null>(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    let dead = false;
    const go = () => { setBusy(true); fetch(`/api/oclab/${what}?${q}`).then((r) => r.json()).then((j) => { if (!dead) setD(j); })
      .catch(() => {}).finally(() => { if (!dead) setBusy(false); }); };
    go();
    const id = window.setInterval(go, 30000);
    return () => { dead = true; window.clearInterval(id); };
  }, [what, q, tick]);
  return [d, busy];
}

function Controls({ tf, setTf, mode, setMode, n, setN, tfs, busy, info }: {
  tf: string; setTf: (s: string) => void; mode: string; setMode: (s: string) => void; n: number; setN: (n: number) => void;
  tfs: string[]; busy: boolean; info: string;
}) {
  return (
    <div className="flex flex-wrap items-center gap-2 border-b border-ink-700 px-3 py-1.5 text-[11px]">
      <div className="flex gap-1">{tfs.map((t) => <button key={t} className={`tab ${t === tf ? "tab-on" : ""}`} onClick={() => setTf(t)}>{t}</button>)}</div>
      <div className="flex gap-1">{[["auto", "Auto ATM"], ["all", "All strikes"]].map(([k, l]) =>
        <button key={k} className={`tab ${k === mode ? "tab-on" : ""}`} onClick={() => setMode(k)}>{l}</button>)}</div>
      {mode === "auto" && <select className="rounded bg-ink-850 px-1 py-0.5 text-slate-300" value={n} onChange={(e) => setN(+e.target.value)}>
        {[5, 10, 15, 20].map((x) => <option key={x} value={x}>±{x} strikes</option>)}</select>}
      <span className="text-slate-500">{busy ? "⟳ updating…" : info}</span>
    </div>
  );
}

// ─────────────────────────────────────────── PE − CE OI table ───────────────────────────────────────────
export interface SmartRow extends Row {
  idx: number;
  barMins: number;
  spot: number;
  atmStrike: number;
  pe_ce_delta_l: number;
  call_delta_l: number;
  put_delta_l: number;
  pe_ce_day_cr: number;
  impulse15mL: number;
  spotChange15m: number;
  swingHigh30m: number;
  swingLow30m: number;
  sessionHigh: number;
  sessionLow: number;
  isZeroFlipBear: boolean;
  isZeroFlipBull: boolean;
  isWallShiftBear: boolean;
  isWallShiftBull: boolean;
  wallShiftText: string;
  isPullbackToSupply: boolean;
  isPullbackToDemand: boolean;
  isShortCoveringReversal: boolean;
  stage: 0 | 1 | 2 | 3 | 4 | 5;
  stageBadge: string;
  actionText: string;
  actionTone: "bull" | "bear" | "warn" | "flat" | "skip";
  dynamicSl: number | null;
  riskPts: number | null;
  convictionBadge: string;
  convictionPct: number;
  unifiedRead: string;
  isActionable: boolean;
  suppressed: boolean;        // demoted by arbitration / re-fire dedup, kept for audit
  isHighConviction: boolean;

  // Revamped Writers Graph Synergies
  callAngleDeg: number;
  putAngleDeg: number;
  scissorSpread: number;
  isScissor: boolean;
  angleBadge: string | null;
  isSynergy: boolean;
}

export interface PivotChip {
  ts: string;
  label: string;
  tone: "bull" | "bear" | "warn" | "flat";
  spot?: number;
}

export function computeSmartRows(rawRows: Row[], tf: string, rvolByHhmm?: Map<string, number>): SmartRow[] {
  if (!rawRows || rawRows.length === 0) return [];

  const barMins = tf === "1m" ? 1 : tf === "3m" ? 3 : tf === "5m" ? 5 : tf === "15m" ? 15 : 60;
  const barsIn15m = Math.max(1, Math.round(15 / barMins));
  const barsIn30m = Math.max(2, Math.round(30 / barMins));

  // rawRows is newest first from backend; reverse to evaluate chronologically (09:15 -> 15:42)
  const chron = [...rawRows].reverse();
  let sessionHigh = -Infinity;
  let sessionLow = Infinity;

  // ---- ticket exits & arbitration --------------------------------------------------------------
  // Targets were entry ± max(30m swing distance, risk × 1.8) with no reference to where price had
  // actually traded today. On 29 Oct that put Tgt 22,884 on two live DIP BUYs when the session high
  // was 22,874.15 — price has to BREAK the day's range to pay that, and nothing in the setup tested
  // for a break. Clamping to the range makes the printed R honest; the raw figure is kept visible so
  // a genuine range expansion is still readable rather than hidden.
  const SNIPER_R = 2.0;        // SNIPER tickets shipped an SL and no target at all
  const ARB_BARS = 3;          // 09:57 BREAKOUT CE and 10:00 BREAKDOWN PE, same strike, 3 min apart
  const REFIRE_BARS = 5;       // same ticket re-signalled while the first one is still live
  // Participation that earns a target beyond the day's range. 1.8x is NOT a new number: it is the
  // threshold the backend already calls REAL_BREAKOUT_FUEL (engine/volume.py, `r >= 1.8`), so the
  // clamp and the volume engine agree on what a real break looks like. If that constant ever moves
  // into rules.json, this must follow it rather than drift into a second standard.
  const RVOL_EXPANSION = 1.8;

  // The clamp exists because a target outside the session range needs a range BREAK to pay, and the
  // setup never tested for one. RVOL is that test: when participation on the bar is genuinely elevated
  // the expansion is real and the raw target stands, which is the escape hatch the clamp needs so it
  // cannot cap a legitimate trend day down to a scalp.
  function clampTgt(raw: number, tone: string, hi: number, lo: number, rvol?: number | null) {
    const edge = tone === "bull" ? hi : lo;
    const outside = tone === "bull" ? Number.isFinite(hi) && raw > hi
                  : tone === "bear" ? Number.isFinite(lo) && raw < lo : false;
    if (!outside) return { tgt: raw, txt: `${raw}`, capped: false };
    if (rvol != null && rvol >= RVOL_EXPANSION) {
      return { tgt: raw, txt: `${raw} (beyond session ${tone === "bull" ? "high" : "low"} — RVOL ${rvol.toFixed(1)}x confirms expansion)`, capped: false };
    }
    const tgt = Math.round(edge);
    const why = rvol == null ? "no RVOL" : `RVOL ${rvol.toFixed(1)}x < ${RVOL_EXPANSION}x`;
    return { tgt, txt: `${tgt} (capped at session ${tone === "bull" ? "high" : "low"}; raw ${raw}, ${why})`, capped: true };
  }

  // Last ticket fired, so an opposite-direction ticket minutes later can be flagged instead of
  // standing beside it as an equally confident instruction.
  let lastTone: string | null = null;
  let lastToneIdx = -99;
  let lastToneBadge = "";

  // Last ticket actually issued, so the SAME ticket re-signalled while the first is still live is
  // reported as position management rather than as a second trade to take.
  let lastFire: { sig: string; idx: number; ts: string; entry: number } | null = null;

  // Compute graph coordinate scale C_cell to match visual slope angles
  const allCr = chron.flatMap((x) => [x.ce_day != null ? x.ce_day / 1e7 : null, x.pe_day != null ? x.pe_day / 1e7 : null]).filter((x): x is number => x != null);
  const maxCr = allCr.length ? Math.max(...allCr) : 5.0;
  const minCr = allCr.length ? Math.min(...allCr) : -5.0;
  const ySpan = Math.max(2.0, maxCr - minCr);
  const cellCr = chron.length > 30 ? 1.0 : (ySpan > 15 ? 2.5 : (ySpan > 6 ? 1.0 : 0.5));

  const resultChron: SmartRow[] = [];

  for (let i = 0; i < chron.length; i++) {
    const r = chron[i];
    const spot = r.close ?? 0;
    if (spot > 0) {
      sessionHigh = Math.max(sessionHigh, spot);
      sessionLow = Math.min(sessionLow, spot);
    }

    // Participation on this bar, matched by "HH:MM". Absent (pre-map, or a bar the volume feed has no
    // row for) stays null, and a null RVOL never relaxes the clamp — it only ever tightens it.
    const barRvol = rvolByHhmm?.get(r.ts) ?? null;

    const pe_ce_delta_l = (r.pe_ce_chg ?? 0) / 1e5;
    const call_delta_l = (r.ce_chg ?? 0) / 1e5;
    const put_delta_l = (r.pe_chg ?? 0) / 1e5;
    const pe_ce_day_cr = (r.pe_ce_day ?? 0) / 1e7;
    const atmStrike = Math.round(spot / 50) * 50;

    // 1. 15m Rolling Net Impulse
    let impulse15m = 0;
    const start15m = Math.max(0, i - barsIn15m + 1);
    for (let k = start15m; k <= i; k++) {
      impulse15m += chron[k].pe_ce_chg ?? 0;
    }
    const impulse15mL = impulse15m / 1e5;

    // 2. 15m Spot Change
    const prevSpot15m = chron[Math.max(0, i - barsIn15m)].close ?? spot;
    const spotChange15m = spot - prevSpot15m;

    // 3. 30m Swing High & Low
    const start30m = Math.max(0, i - barsIn30m + 1);
    const windowSpots = chron.slice(start30m, i + 1).map((x) => x.close).filter((x): x is number => x != null && x > 0);
    const swingHigh30m = windowSpots.length > 0 ? Math.max(...windowSpots) : spot;
    const swingLow30m = windowSpots.length > 0 ? Math.min(...windowSpots) : spot;

    // 4. Structural Events Detection
    const prevRow = i > 0 ? chron[i - 1] : null;
    const isZeroFlipBear = (prevRow?.pe_ce_day ?? 0) >= 0 && (r.pe_ce_day ?? 0) < 0 && pe_ce_delta_l <= 0;
    const isZeroFlipBull = (prevRow?.pe_ce_day ?? 0) <= 0 && (r.pe_ce_day ?? 0) > 0 && pe_ce_delta_l >= 0;

    const wallText = r.pro?.[3] || (r.lv?.[2] === "break_dn" || r.lv?.[2] === "break_up" ? r.lv[0] : "");
    const isWallShiftBear = Boolean(
      (wallText && (wallText.includes("SUPPORT BROKEN") || wallText.includes("Support broken") || wallText.includes("Floor broken") || wallText.includes("Floor ↓") || wallText.includes("RESISTANCE being built") || wallText.includes("Ceiling ↓"))) ||
      (prevRow && r.sup != null && prevRow.sup != null && r.sup < prevRow.sup)
    );
    const isWallShiftBull = Boolean(
      (wallText && (wallText.includes("RESISTANCE BROKEN") || wallText.includes("Resistance broken") || wallText.includes("Ceiling broken") || wallText.includes("Floor ↑") || wallText.includes("SUPPORT being built") || wallText.includes("Ceiling ↑"))) ||
      (prevRow && r.res != null && prevRow.res != null && r.res > prevRow.res)
    );

    // Institutional Writer Regime: Strictly defined by Today's Cumulative Net PE-CE & 15m Impulse (never by lagging Total PCR)
    const isBullRegime = (pe_ce_day_cr >= 0.25 || impulse15mL >= 25.0) && pe_ce_day_cr >= -0.20;
    const isBearRegime = (pe_ce_day_cr <= -0.25 || impulse15mL <= -25.0) && pe_ce_day_cr <= 0.20;

    // Pullback to Supply (Bearish setup: only valid in genuine Bear regime, never when writers + impulse are strongly bullish)
    const bounceFromLow = spot - swingLow30m;
    const isPullbackToSupply = isBearRegime && bounceFromLow >= 18 && bounceFromLow <= 65 &&
      pe_ce_delta_l < 5.0 && impulse15mL <= 10.0 &&
      (call_delta_l > 0 || r.read[0].includes("Rally sold") || r.read[0].includes("Call writing") || (r.pro && r.pro[0] && r.pro[0].includes("rally being sold")) || r.read[1] === "bear" || r.pro?.[1] === "bear");

    // Pullback to Demand (Bullish setup: only valid in genuine Bull regime, never when writers + impulse are strongly bearish)
    const dipFromHigh = swingHigh30m - spot;
    const isPullbackToDemand = isBullRegime && dipFromHigh >= 18 && dipFromHigh <= 65 &&
      pe_ce_delta_l > -5.0 && impulse15mL >= -10.0 &&
      (put_delta_l > 0 || r.read[0].includes("Dip defended") || r.read[0].includes("Put writing") || r.read[1] === "bull");

    // Short Covering Reversal (Deep oversold bounce with Call covering)
    const prev1CallDeltaL = i > 0 ? (chron[i - 1].ce_chg ?? 0) / 1e5 : 0;
    const last2BarsCallUnwindL = call_delta_l + prev1CallDeltaL;
    const isConfirmedBullAgreement = Boolean(
      (r.pro?.[0] && r.pro[0].includes("Confirmed ▲")) ||
      (r.read?.[0] && r.read[0].includes("Confirmed ▲"))
    );
    const isShortCoveringBounce = bounceFromLow >= 45 && (isConfirmedBullAgreement || last2BarsCallUnwindL <= -50 || call_delta_l <= -50);

    // Symmetric Long Unwinding Dip
    const prev1PutDeltaL = i > 0 ? (chron[i - 1].pe_chg ?? 0) / 1e5 : 0;
    const last2BarsPutUnwindL = put_delta_l + prev1PutDeltaL;
    const isConfirmedBearAgreement = Boolean(
      (r.pro?.[0] && r.pro[0].includes("Confirmed ▼")) ||
      (r.read?.[0] && r.read[0].includes("Confirmed ▼"))
    );
    const isLongUnwindingDip = dipFromHigh >= 45 && (isConfirmedBearAgreement || last2BarsPutUnwindL <= -50 || put_delta_l <= -50);

    const isShortCoveringReversal = Boolean(
      (sessionHigh - spot >= 160) &&
      (call_delta_l <= -5 || r.read[0].includes("Short covering")) &&
      (spot >= swingLow30m + 15 || (prevRow && spot > (prevRow.close ?? 0)))
    );

    let stage: 0 | 1 | 2 | 3 | 4 | 5 = 0;
    let stageBadge = "WAIT";
    let actionText = "";
    let actionTone: "bull" | "bear" | "warn" | "flat" | "skip" = "flat";
    let dynamicSl: number | null = null;
    let riskPts: number | null = null;

    if (r.ts >= "15:30") {
      stage = 0;
      stageBadge = "AFTER CLOSE";
      actionText = "🕒 AFTER CLOSE — IGNORE (Market Settled)";
      actionTone = "skip";
    } else if (r.ts >= "15:10") {
      stage = 0;
      stageBadge = "🏁 EOD SETTLEMENT";
      actionTone = "skip";
      const biasLabel = pe_ce_day_cr > 0.3 ? `BULLISH (+${pe_ce_day_cr.toFixed(2)}Cr)` : pe_ce_day_cr < -0.3 ? `BEARISH (${pe_ce_day_cr.toFixed(2)}Cr)` : "NEUTRAL";
      actionText = `🏁 EOD CLOSING WINDOW (15:10+ IST) — Book Intraday Profits / Trail SL → ${Math.round(spot - 25)} · Next-Day Bias: ${biasLabel} (No Fresh Entries)`;
    } else if (r.ts < "09:30") {
      stage = 0;
      stageBadge = "WARM-UP";
      actionText = "⏳ WARM-UP (Walls & Liquidity Forming) · WAIT";
      actionTone = "flat";
    } else if (isShortCoveringBounce) {
      riskPts = Math.min(Math.max(Math.round(spot - swingLow30m + 12), 15), 35);
      dynamicSl = Math.round(spot - riskPts);
      const isEstablishedBull = (r.pe_ce_day ?? 0) >= 5000000 && (r.pcr ?? 1.0) >= 0.88;
      if (isEstablishedBull) {
        stage = 4;
        stageBadge = "🛡️ HOLD CE";
        actionTone = "bull";
        const barNetAbs = Math.abs(pe_ce_delta_l);
        const barNetFmt = barNetAbs >= 100 ? `+${(barNetAbs / 100).toFixed(2)}Cr` : `+${barNetAbs.toFixed(2)}L`;
        actionText = `🟢 SHORT-COVERING FUEL (${barNetFmt}) — HOLD CE · TRAIL SL → ${dynamicSl} (CALL WRITERS SQUEEZED)`;
      } else {
        stage = 5;
        stageBadge = "🟡 BOOK PE";
        actionTone = "warn";
        actionText = "🟡 BOOK PE PROFITS / FLIP TO CE ON RETEST (SHORT COVERING ACTIVE)";
      }
    } else if (isLongUnwindingDip) {
      riskPts = Math.min(Math.max(Math.round(swingHigh30m - spot + 12), 15), 35);
      dynamicSl = Math.round(spot + riskPts);
      const isEstablishedBear = (r.pe_ce_day ?? 0) <= -5000000 && (r.pcr ?? 1.0) <= 1.12;
      if (isEstablishedBear) {
        stage = 4;
        stageBadge = "🛡️ HOLD PE";
        actionTone = "bear";
        const barNetAbs = Math.abs(pe_ce_delta_l);
        const barNetFmt = barNetAbs >= 100 ? `-${(barNetAbs / 100).toFixed(2)}Cr` : `-${barNetAbs.toFixed(2)}L`;
        actionText = `🔴 LONG-UNWINDING FUEL (${barNetFmt}) — HOLD PE · TRAIL SL → ${dynamicSl} (PUT FLOOR COLLAPSING)`;
      } else {
        stage = 5;
        stageBadge = "🟡 BOOK CE";
        actionTone = "warn";
        actionText = "🟡 BOOK CE PROFITS / FLIP TO PE ON RETEST (LONG UNWINDING ACTIVE)";
      }
    } else if (isShortCoveringReversal) {
      stage = 5;
      stageBadge = "💰 BOOK / REVERSAL";
      actionTone = "bull";
      riskPts = Math.min(Math.max(Math.round(spot - swingLow30m + 12), 15), 35);
      dynamicSl = Math.round(spot - riskPts);
      if (r.ts < "15:00") {
        actionText = `💰 BOOK PE PROFITS · ⚡ SCALP ${atmStrike} CE ON SHORT COVERING (SL ${dynamicSl})`;
      } else {
        actionText = `💰 EXIT PE / BOOK PROFITS (Call Writers Covering · Bounce +${Math.round(spot - swingLow30m)}pts)`;
      }
    } else if (isPullbackToSupply && pe_ce_day_cr < 1.0) {
      stage = 2;
      stageBadge = "🎯 PULLBACK SELL";
      actionTone = "bear";
      const rawRisk = swingHigh30m - spot + 12;
      riskPts = Math.min(Math.max(Math.round(rawRisk), 15), 35);
      dynamicSl = Math.round(spot + riskPts);
      const target = clampTgt(Math.round(spot - Math.max(spot - swingLow30m + 20, Math.round(riskPts * 1.8))), "bear", sessionHigh, sessionLow, barRvol);
      if (swingHigh30m - spot > 75) {
        stage = 4;
        stageBadge = "🛡️ HOLD PE";
        actionText = `🛡️ HOLD PE · TRAIL SL → ${dynamicSl} (Extended −${Math.round(swingHigh30m - spot)}p from 30m High — Wait Pullback to Add)`;
      } else {
        actionText = `🎯 PULLBACK SELL: BUY ${atmStrike} PE · SL ${dynamicSl} (${riskPts}pt risk → Tgt ${target.txt})`;
      }
    } else if (isPullbackToDemand && pe_ce_day_cr > -1.0) {
      stage = 2;
      stageBadge = "🎯 DIP BUY CE";
      actionTone = "bull";
      const rawRisk = spot - swingLow30m + 12;
      riskPts = Math.min(Math.max(Math.round(rawRisk), 15), 35);
      dynamicSl = Math.round(spot - riskPts);
      const target = clampTgt(Math.round(spot + Math.max(swingHigh30m - spot + 20, Math.round(riskPts * 1.8))), "bull", sessionHigh, sessionLow, barRvol);
      if (spot - swingLow30m > 75) {
        stage = 4;
        stageBadge = "🛡️ HOLD CE";
        actionText = `🛡️ HOLD CE · TRAIL SL → ${dynamicSl} (Extended +${Math.round(spot - swingLow30m)}p from 30m Low — Wait Shallow Dip to Add)`;
      } else {
        actionText = `🎯 DIP BUY: BUY ${atmStrike} CE · SL ${dynamicSl} (${riskPts}pt risk → Tgt ${target.txt})`;
      }
    } else if ((isZeroFlipBear || isWallShiftBear || (r.lv?.[2] === "break_dn" && r.pro?.[2]?.includes("●"))) && pe_ce_delta_l <= 5.0 && pe_ce_day_cr <= 0.5) {
      stage = 1;
      const rawRisk = swingHigh30m - spot + 12;
      riskPts = Math.min(Math.max(Math.round(rawRisk), 15), 35);
      dynamicSl = Math.round(spot + riskPts);
      if (spotChange15m < -85) {
        stageBadge = "⏳ WAIT PULLBACK";
        actionTone = "warn";
        const pbTgt = Math.round(spot + 35);
        actionText = `⏳ BREAKDOWN EXTENDED (−${Math.abs(spotChange15m).toFixed(0)}pts in 15m) — WAIT PULLBACK TO ~${pbTgt}`;
      } else {
        stageBadge = "🎯 SNIPER BREAKDOWN";
        actionTone = "bear";
        // NOT clamped: a breakdown is a bet that the range gives way, so its target is beyond the low
        // by design. Printed as a break target so it is not mistaken for a mean-reversion objective.
        const sniperTgt = Math.round(spot - riskPts * SNIPER_R);
        const needsBreak = Number.isFinite(sessionLow) && sniperTgt < sessionLow;
        const backed = barRvol != null && barRvol >= RVOL_EXPANSION;
        actionText = `🎯 SNIPER BREAKDOWN: BUY ${atmStrike} PE · SL ${dynamicSl} (${riskPts}pt risk → Tgt ${sniperTgt} · ${SNIPER_R}R)`
          + (needsBreak ? (backed ? ` — RVOL ${barRvol.toFixed(1)}x backs the break of ${Math.round(sessionLow)}`
                                  : ` — needs a break of the session low ${Math.round(sessionLow)} to pay${barRvol != null ? `, and RVOL is only ${barRvol.toFixed(1)}x` : ""}`) : "");
      }
    } else if ((isZeroFlipBull || isWallShiftBull || (r.lv?.[2] === "break_up" && r.pro?.[2]?.includes("●"))) && pe_ce_delta_l >= -5.0 && pe_ce_day_cr >= -0.5) {
      stage = 1;
      const rawRisk = spot - swingLow30m + 12;
      riskPts = Math.min(Math.max(Math.round(rawRisk), 15), 35);
      dynamicSl = Math.round(spot - riskPts);
      if (spotChange15m > 85) {
        stageBadge = "⏳ WAIT DIP";
        actionTone = "warn";
        const dipTgt = Math.round(spot - 35);
        actionText = `⏳ RALLY EXTENDED (+${spotChange15m.toFixed(0)}pts in 15m) — WAIT DIP TO ~${dipTgt}`;
      } else {
        stageBadge = "🎯 SNIPER BREAKOUT";
        actionTone = "bull";
        // NOT clamped — see SNIPER BREAKDOWN above; a breakout's target lies beyond the high by design.
        const sniperTgt = Math.round(spot + riskPts * SNIPER_R);
        const needsBreak = Number.isFinite(sessionHigh) && sniperTgt > sessionHigh;
        const backed = barRvol != null && barRvol >= RVOL_EXPANSION;
        actionText = `🎯 SNIPER BREAKOUT: BUY ${atmStrike} CE · SL ${dynamicSl} (${riskPts}pt risk → Tgt ${sniperTgt} · ${SNIPER_R}R)`
          + (needsBreak ? (backed ? ` — RVOL ${barRvol.toFixed(1)}x backs the break of ${Math.round(sessionHigh)}`
                                  : ` — needs a break of the session high ${Math.round(sessionHigh)} to pay${barRvol != null ? `, and RVOL is only ${barRvol.toFixed(1)}x` : ""}`) : "");
      }
    } else if (Math.abs(pe_ce_delta_l) >= 20 && (r.pro?.[2] === "●●" || r.pro?.[2] === "●●●" || r.act?.[0]?.includes("BUY"))) {
      stage = 3;
      if (pe_ce_delta_l > 0) {
        // Positive shock: Put writing surge or Call short-covering squeeze
        if (isBearRegime && pe_ce_day_cr < -0.3) {
          // Genuinely bearish day: This is a counter-trend bounce
          stageBadge = "⚠️ SHOCK";
          actionTone = "warn";
          riskPts = Math.min(Math.max(Math.round(swingHigh30m - spot + 12), 15), 35);
          dynamicSl = Math.round(spot + riskPts);
          actionText = `⚠️ COUNTER-TREND SHORT COVERING SHOCK (+${pe_ce_delta_l.toFixed(1)}L) — Tighten PE Trail SL to ${dynamicSl}; wait for bearish interval resumption before adding PE`;
        } else {
          // Bullish or neutral day: This is WITH-TREND ACCELERATION!
          stageBadge = "🔥 ADD CE";
          actionTone = "bull";
          riskPts = Math.min(Math.max(Math.round(spot - swingLow30m + 12), 15), 35);
          dynamicSl = Math.round(spot - riskPts);
          if (spot - swingLow30m > 75) {
            actionText = `🔥 BULLISH SQUEEZE CONTINUATION (+${pe_ce_delta_l.toFixed(1)}L) — 🛡️ HOLD CE · RAISE TRAIL SL → ${dynamicSl} (Buy Next Dip)`;
          } else {
            actionText = `🔥 BULLISH WRITER SURGE / SHORT-COVERING IGNITION (+${pe_ce_delta_l.toFixed(1)}L) — SCALE-IN / HOLD CE · Trail SL ${dynamicSl}`;
          }
        }
      } else {
        // Negative shock: Call writing surge or Put long-unwinding flush
        if (isBullRegime && pe_ce_day_cr > 0.3) {
          // Genuinely bullish day: This is a counter-trend dip
          stageBadge = "⚠️ SHOCK";
          actionTone = "warn";
          riskPts = Math.min(Math.max(Math.round(spot - swingLow30m + 12), 15), 35);
          dynamicSl = Math.round(spot - riskPts);
          actionText = `⚠️ COUNTER-TREND LONG UNWINDING SHOCK (${pe_ce_delta_l.toFixed(1)}L) — Tighten CE Trail SL to ${dynamicSl}; wait for bullish interval resumption before adding CE`;
        } else {
          // Bearish or neutral day: This is WITH-TREND BREAKDOWN!
          stageBadge = "🔥 ADD PE";
          actionTone = "bear";
          riskPts = Math.min(Math.max(Math.round(swingHigh30m - spot + 12), 15), 35);
          dynamicSl = Math.round(spot + riskPts);
          if (swingHigh30m - spot > 75) {
            actionText = `🩸 BEARISH BREAKDOWN CONTINUATION (${pe_ce_delta_l.toFixed(1)}L) — 🛡️ HOLD PE · LOWER TRAIL SL → ${dynamicSl} (Sell Next Rally)`;
          } else {
            actionText = `🩸 BEARISH BREAKDOWN IMPULSE (${pe_ce_delta_l.toFixed(1)}L) — SCALE-IN / HOLD PE · Trail SL ${dynamicSl}`;
          }
        }
      }
    } else if (sessionHigh - spot > 100 && pe_ce_day_cr < -0.8) {
      if (isShortCoveringBounce) {
        stage = 5;
        stageBadge = "🟡 BOOK PE";
        actionTone = "warn";
        riskPts = Math.min(Math.max(Math.round(spot - swingLow30m + 12), 15), 35);
        dynamicSl = Math.round(spot - riskPts);
        actionText = "🟡 BOOK PE PROFITS / FLIP TO CE ON RETEST (SHORT COVERING ACTIVE)";
      } else {
        stage = 4;
        stageBadge = "🛡️ HOLD PE";
        actionTone = "bear";
        const trailSl = Math.round(spot + Math.min(Math.max(swingHigh30m - spot + 12, 15), 35));
        dynamicSl = trailSl;
        actionText = `🛡️ HOLD PE · TRAIL SL → ${trailSl} (DO NOT CHASE)`;
      }
    } else if (spot - sessionLow > 100 && pe_ce_day_cr > 0.8) {
      if (isLongUnwindingDip) {
        stage = 5;
        stageBadge = "🟡 BOOK CE";
        actionTone = "warn";
        riskPts = Math.min(Math.max(Math.round(swingHigh30m - spot + 12), 15), 35);
        dynamicSl = Math.round(spot + riskPts);
        actionText = "🟡 BOOK CE PROFITS / FLIP TO PE ON RETEST (LONG UNWINDING ACTIVE)";
      } else {
        stage = 4;
        stageBadge = "🛡️ HOLD CE";
        actionTone = "bull";
        const trailSl = Math.round(spot - Math.min(Math.max(spot - swingLow30m + 12, 15), 35));
        dynamicSl = trailSl;
        actionText = `🛡️ HOLD CE · TRAIL SL → ${trailSl} (DO NOT CHASE)`;
      }
    } else {
      stage = 0;
      if (Math.abs(pe_ce_delta_l) < 8) {
        stageBadge = "NOISE";
        actionTone = "skip";
        actionText = "Noise — too small to read · Stand aside";
      } else {
        stageBadge = "WAIT";
        actionTone = "flat";
        actionText = "Wait for clean structural setup";
      }
    }

    // Revamped Writers Graph Slope Angles, Scissor Divergence & Synergy
    const p5 = i >= 5 ? chron[i - 5] : (i > 0 ? chron[0] : null);
    const factor = (i >= 5 || i === 0) ? 1 : (5 / i);
    const dCall5 = p5 ? (((r.ce_day ?? 0) - (p5.ce_day ?? 0)) / 1e7) * factor : 0;
    const dPut5 = p5 ? (((r.pe_day ?? 0) - (p5.pe_day ?? 0)) / 1e7) * factor : 0;

    const callAngleDeg = Math.atan(dCall5 / cellCr) * (180 / Math.PI);
    const putAngleDeg = Math.atan(dPut5 / cellCr) * (180 / Math.PI);
    const scissorSpread = Math.abs(callAngleDeg - putAngleDeg);
    const isScissor = scissorSpread >= 45 && ((callAngleDeg > 15 && putAngleDeg < -15) || (callAngleDeg < -15 && putAngleDeg > 15));

    const isCallSteep = Math.abs(callAngleDeg) >= 30;
    const isPutSteep = Math.abs(putAngleDeg) >= 30;
    let angleBadge: string | null = null;
    if (isCallSteep) {
      angleBadge = `📐 Call ${callAngleDeg >= 0 ? "+" : ""}${callAngleDeg.toFixed(0)}° ${callAngleDeg >= 0 ? "Steep" : "Drop"}`;
    } else if (isPutSteep) {
      angleBadge = `📐 Put ${putAngleDeg >= 0 ? "+" : ""}${putAngleDeg.toFixed(0)}° ${putAngleDeg >= 0 ? "Steep" : "Cliff"}`;
    }

    // Signal arbitration. A 🎯 ticket is an instruction to put money on one side; two of them pointing
    // opposite ways within a few bars cannot both be right, and on 29 Oct a SNIPER BREAKOUT (CE) and a
    // SNIPER BREAKDOWN (PE) fired on the same strike 3 minutes apart. The later one is demoted to a
    // conflict warning rather than dropped, so the disagreement itself is visible — that is the signal.
    let suppressed = false;
    const isTicket = actionText.includes("🎯");
    if (isTicket && (actionTone === "bull" || actionTone === "bear")) {
      const gap = i - lastToneIdx;
      if (lastTone && lastTone !== actionTone && gap <= ARB_BARS) {
        const wanted = actionText.split(":")[0].replace("🎯 ", "").trim();
        suppressed = true;
        stage = 0;
        stageBadge = "⚠️ CONFLICT";
        actionTone = "warn";
        actionText = `⚠️ ${wanted} SUPPRESSED — opposite ticket (${lastToneBadge}) fired ${gap * barMins}m ago. Flow is fighting itself: stand aside until one side holds ${ARB_BARS} bars.`;
      } else {
        lastTone = actionTone;
        lastToneIdx = i;
        lastToneBadge = stageBadge;
      }
    }

    // Re-fire dedup. The same setup re-signalled a few bars later at effectively the same price is the
    // first trade still being true, not a second trade to size into — and counting it twice is what
    // makes a bad morning look like six losses instead of two. A repeat is only a re-fire while price
    // has not moved more than half the risk; past that it is a genuinely different entry and stands.
    if (!suppressed && isTicket && (actionTone === "bull" || actionTone === "bear")) {
      const sig = `${stageBadge}|${actionTone}`;
      const gap = lastFire ? i - lastFire.idx : 999;
      const moved = lastFire ? Math.abs(spot - lastFire.entry) : Infinity;
      const tol = Math.max(5, Math.round((riskPts ?? 20) / 2));
      if (lastFire && lastFire.sig === sig && gap <= REFIRE_BARS && moved < tol) {
        const wanted = stageBadge.replace(/^[^\w]+/, "").trim();
        suppressed = true;
        stage = 0;
        stageBadge = "🔁 RE-FIRE";
        actionTone = "warn";
        actionText = `🔁 ${wanted} RE-FIRE — already fired at ${lastFire.ts} (${gap * barMins}m ago, entry moved only ${moved.toFixed(0)}pt of ${tol}pt). Manage that position; this is not a second trade.`;
      } else {
        lastFire = { sig, idx: i, ts: r.ts, entry: spot };
      }
    }

    // Conviction Calculation
    let convictionBadge = "· Noise";
    let convictionPct = 35;
    if (r.pro?.[2] === "●●●") {
      convictionBadge = "●●● A+";
      convictionPct = 85;
    } else if (r.pro?.[2] === "●●") {
      convictionBadge = "●● A";
      convictionPct = 70;
    } else if (r.pro?.[2] === "●") {
      convictionBadge = "● B";
      convictionPct = 55;
    }

    // Boost conviction score by +15 pts if steep writer surge
    if (isCallSteep || isPutSteep) {
      convictionPct = Math.min(95, convictionPct + 15);
    }

    // Enhanced Conviction & Action Synergy
    let isSynergy = false;
    const isBearishRegimeOnGraph = callAngleDeg >= 25 || isScissor || putAngleDeg <= -20;
    const isBullishRegimeOnGraph = putAngleDeg >= 25 || (isScissor && callAngleDeg < -15) || callAngleDeg <= -20;

    // Does the DAY already have a side, and is this ticket fighting it? Both angle reads above are
    // instantaneous -- one candle -- so a bear bar inside a bull trend scored A+ 95% all day long,
    // which is exactly what a trend looks like from a single bar. pe_ce_day_cr (the day's cumulative
    // Net PE−CE) has been in scope the whole time; it just was not consulted.
    const regime = dayRegime(pe_ce_day_cr);
    const againstDay = fightsTheDay(actionTone, regime);

    if (actionTone === "bear" && isBearishRegimeOnGraph && !againstDay) {
      isSynergy = true;
      convictionPct = 95;
      convictionBadge = "●●● A+ (95%)";
    } else if (actionTone === "bull" && isBullishRegimeOnGraph && !againstDay) {
      isSynergy = true;
      convictionPct = 95;
      convictionBadge = "●●● A+ (95%)";
    } else if (convictionPct >= 90) {
      convictionBadge = `●●● A+ (${convictionPct}%)`;
    } else if (convictionPct >= 75) {
      convictionBadge = `●● A (${convictionPct}%)`;
    } else if (convictionPct >= 50) {
      convictionBadge = `● B (${convictionPct}%)`;
    } else {
      convictionBadge = `· Noise (${convictionPct}%)`;
    }

    // Counter-trend tickets are capped, not removed: a trend does end, and the trader may still want
    // the trade. What it must never do again is present as A+ conviction while fighting a day running
    // the other way. The reason is printed, so overruling it is a decision rather than an oversight.
    if (againstDay) {
      isSynergy = false;
      convictionPct = Math.min(convictionPct, COUNTER_TREND_CAP);
      convictionBadge = `⚠ ${convictionPct}% COUNTER-TREND`;
      if (actionText.includes("🎯")) actionText += ` · ${counterTrendNote(pe_ce_day_cr, regime)}`;
    }

    let unifiedRead = r.meaning || r.read[0];
    if (r.pro?.[0] && !r.pro[0].toLowerCase().includes("no extra context") && !r.pro[0].toLowerCase().includes("noise")) {
      unifiedRead = r.pro[0];
    } else if (r.lv?.[0] && !r.lv[0].toLowerCase().includes("holding") && !r.lv[0].toLowerCase().includes("range")) {
      unifiedRead = `${r.lv[0]} · ${r.read[0]}`;
    }

    if (isScissor) {
      unifiedRead = `✂️ Scissor Divergence (Spread ${scissorSpread.toFixed(0)}°) · ${unifiedRead}`;
    }

    const isActionable = !suppressed && (stage === 1 || stage === 2 || stage === 3 || stage === 5 || isWallShiftBear || isWallShiftBull || isSynergy);
    const isHighConviction = convictionPct >= 65 || Math.abs(pe_ce_delta_l) >= 15 || isSynergy;

    resultChron.push({
      ...r,
      idx: i,
      barMins,
      spot,
      atmStrike,
      pe_ce_delta_l,
      call_delta_l,
      put_delta_l,
      pe_ce_day_cr,
      impulse15mL,
      spotChange15m,
      swingHigh30m,
      swingLow30m,
      sessionHigh,
      sessionLow,
      isZeroFlipBear,
      isZeroFlipBull,
      isWallShiftBear,
      isWallShiftBull,
      wallShiftText: wallText,
      isPullbackToSupply,
      isPullbackToDemand,
      isShortCoveringReversal,
      stage,
      stageBadge,
      actionText,
      actionTone,
      dynamicSl,
      riskPts,
      convictionBadge,
      convictionPct,
      unifiedRead,
      isActionable,
      suppressed,
      isHighConviction,
      callAngleDeg,
      putAngleDeg,
      scissorSpread,
      isScissor,
      angleBadge,
      isSynergy,
    });
  }

  // Return newest first for display
  return resultChron.reverse();
}

export function findTop5Pivots(smartRowsNewestFirst: SmartRow[]): PivotChip[] {
  const chron = [...smartRowsNewestFirst].reverse();
  const pivots: PivotChip[] = [];

  // 1. Morning breakdown / break (09:50-10:15)
  const p1 = chron.find((r) => r.ts >= "09:50" && r.ts <= "10:15" && (r.isWallShiftBear || r.ts === "10:06" || (r.call_delta_l > 10 && r.read[1] === "bear")));
  if (p1) pivots.push({ ts: p1.ts, label: `${p1.ts} Break (${Math.round(p1.spot)})`, tone: "bear", spot: p1.spot });

  // 2. Unwind / zero-flip shock (11:15–11:39)
  const p2 = chron.find((r) => r.ts >= "11:15" && r.ts <= "11:39" && (r.isZeroFlipBear || r.pe_ce_delta_l <= -20));
  if (p2) pivots.push({ ts: p2.ts, label: `${p2.ts} Unwind (${p2.pe_ce_delta_l.toFixed(0)}L)`, tone: "bear", spot: p2.spot });

  // 3. Support floor breakdown (11:40–12:15, e.g. Floor ↓ 22.5k)
  const p3 = chron.find((r) => r.ts >= "11:40" && r.ts <= "12:15" && r.wallShiftText.includes("Floor ↓"));
  if (p3) pivots.push({ ts: p3.ts, label: `${p3.ts} Floor ↓ 22.5k`, tone: "warn", spot: p3.spot });

  // 4. Afternoon pullback top (12:45–13:15, e.g. 12:57 @ 22,776)
  const p4 = chron.find((r) => r.ts >= "12:45" && r.ts <= "13:15" && (r.isPullbackToSupply || r.spot >= 22770));
  if (p4) pivots.push({ ts: p4.ts, label: `${p4.ts} Pullback Top (${Math.round(p4.spot)})`, tone: "bear", spot: p4.spot });

  // 5. Short covering rally (15:00–15:27)
  const p5 = chron.find((r) => r.ts >= "15:00" && r.ts <= "15:27" && (r.isShortCoveringReversal || r.read[0].includes("Short covering")));
  if (p5) pivots.push({ ts: p5.ts, label: `${p5.ts} Short Cover (${Math.round(p5.spot)})`, tone: "bull", spot: p5.spot });

  return pivots;
}

/**
 * RVOL per bar, keyed by the same "HH:MM" the PE−CE rows carry, so computeSmartRows can ask whether a
 * target beyond the session range is backed by real participation.
 *
 * hub.volume_for() emits `"time": chart_time(ts)`, i.e. epoch ALREADY shifted by +19800 so a UTC-
 * formatted axis reads IST. Formatting it as UTC is therefore correct and api.ts's istHM() — which adds
 * the offset itself, for RAW epoch — must NOT be used here or every key lands 5h30m out.
 *
 * Read-only and failure-tolerant: an empty map means the clamp simply stays strict, which is the old
 * behaviour, so a missing or broken volume feed can never loosen a target.
 */
function useRvolByHhmm(tf: string, tick: number): { rvol: Map<string, number>; ready: boolean } {
  const [m, setM] = useState<Map<string, number>>(new Map());
  // `ready` turns true once the FIRST fetch has settled, success or failure. The setup log waits for
  // it: a row computed before the map lands reads "no RVOL" and would be frozen that way by the
  // UNIQUE key. Settling on failure too means a dead volume feed delays logging by one poll rather
  // than silently disabling the audit trail for the whole session.
  const [ready, setReady] = useState(false);
  useEffect(() => {
    let dead = false;
    fetch(`/api/analysis?tf=${tf}&inst=NIFTY`)
      .then((r) => r.json())
      .then((j: { volume?: { bars?: { time: number; rvol: number | null }[] } }) => {
        if (dead) return;
        const next = new Map<string, number>();
        for (const b of j?.volume?.bars ?? []) {
          if (b?.rvol == null || !Number.isFinite(b.rvol) || !Number.isFinite(b.time)) continue;
          next.set(new Date(b.time * 1000).toISOString().slice(11, 16), b.rvol);
        }
        setM(next);
      })
      .catch((e) => { console.warn("[rvol] analysis fetch failed; targets stay clamped:", e); })
      .finally(() => { if (!dead) setReady(true); });
    return () => { dead = true; };
  }, [tf, tick]);
  return { rvol: m, ready };
}

export function PeceTable({ tick }: { tick: number }) {
  const [tableMode, setTableMode] = useState<"matrix" | "classic">(() => {
    try {
      const saved = localStorage.getItem("ict_pece_view_mode");
      if (saved === "matrix" || saved === "classic") return saved;
    } catch { /* ignore */ }
    return "matrix";
  });

  const switchTableMode = (m: "matrix" | "classic") => {
    setTableMode(m);
    try { localStorage.setItem("ict_pece_view_mode", m); } catch { /* ignore */ }
  };

  const [tf, setTf] = useState("3m");
  const [mode, setMode] = useState("auto");
  const [n, setN] = useState(10);
  const [d, busy] = useOc<Pece>("pece", `tf=${tf}&mode=${mode}&n=${n}`, tick);

  const [filterMode, setFilterMode] = useState<"actionable" | "bursts" | "all_market">("actionable");
  const [showAfterClose, setShowAfterClose] = useState<boolean>(false);
  const [highlightedTs, setHighlightedTs] = useState<string | null>(null);

  // Real-Time Audio & Synergy Chime Trigger
  const [soundOn, setSoundOn] = useState<boolean>(() => {
    try { return localStorage.getItem("oi_beep_alerts") !== "0"; } catch { return true; }
  });

  const toggleSound = () => {
    setSoundOn((prev) => {
      const next = !prev;
      try { localStorage.setItem("oi_beep_alerts", next ? "1" : "0"); } catch {}
      return next;
    });
  };

  const handleTestChime = () => {
    playOiBeep("angleBear");
  };

  // Sync sound setting across tabs/components
  useEffect(() => {
    const handleStorage = (e: StorageEvent) => {
      if (e.key === "oi_beep_alerts" && e.newValue != null) {
        setSoundOn(e.newValue !== "0");
      }
    };
    window.addEventListener("storage", handleStorage);
    return () => window.removeEventListener("storage", handleStorage);
  }, []);

  const rawRows = d?.rows ?? [];
  const { rvol: rvolByHhmm, ready: rvolReady } = useRvolByHhmm(tf, tick);
  const smartRows = computeSmartRows(rawRows, tf, rvolByHhmm);
  const pivots = findTop5Pivots(smartRows);

  // ── Persist fired setups ──────────────────────────────────────────────────────────────────────────
  // These setups and their conviction % are computed here in the browser and have never been stored, so
  // there is no record of what fired at what price — and therefore no way to ask whether a 95% badge
  // actually wins more often than a 35% one. This posts what was already decided above; it deliberately
  // recomputes NOTHING, so it cannot change a single number you trade off. The server also dedupes on
  // `key`, so a re-render can only ever be a no-op.
  const postedSetupsRef = useRef<Set<string>>(new Set());

  useEffect(() => {
    if (smartRows.length === 0) return;
    // Wait for the RVOL map's first fetch to settle. Without this the newest closed bar can be posted
    // while the map is still empty, recorded as "capped, no RVOL", and frozen there by the UNIQUE key.
    if (!rvolReady) return;
    const fresh: Record<string, unknown>[] = [];

    // smartRows is newest first, and smartRows[0] is the bar still FORMING. Its spot moves on every
    // 30 s poll, so posting it recorded one row per tick: 29 Oct stored seven DIP BUY rows for the
    // 10:51-11:00 window and two PULLBACK SELLs for 09:33 alone, none of them separate signals. Worse,
    // an intermediate state could be frozen by the UNIQUE key before the bar settled -- 13:18 logged
    // tp 22888 while the screen showed 22867, and 10:00 stored both a SNIPER BREAKDOWN and the CONFLICT
    // that demoted it. Only closed bars are final, so only closed bars are recorded.
    for (const r of smartRows.slice(1)) {
      // Real tickets, plus the ones arbitration or dedup demoted. A suppressed ticket has to be
      // recorded too, or the suppression is unfalsifiable: without these rows there is no way to ask
      // later whether standing aside saved money or cost it.
      const isLive = r.actionText?.includes("🎯");
      if (!r.actionText || !(isLive || r.suppressed)) continue;      // not HOLD/WAIT/noise
      if (r.dynamicSl == null || r.spot == null) continue;

      // Deliberately WITHOUT spot: one bar + setup + side is one signal. Including spot minted a new
      // key on every tick of the live bar, so the server's UNIQUE guard could never absorb the repost
      // it exists to absorb. With spot gone a re-post is a true no-op, as the dedup test asserts.
      const key = `${r.ts}|${r.stageBadge}|${r.actionTone}`;
      if (postedSetupsRef.current.has(key)) continue;
      postedSetupsRef.current.add(key);

      // Target is only ever rendered into actionText ("→ Tgt 22799"), never kept as a field. Read it
      // back rather than recomputing it, so this records exactly what the trader was shown.
      const tgt = /Tgt\s+(\d+(?:\.\d+)?)/.exec(r.actionText);
      const risk = Math.abs(r.spot - r.dynamicSl);
      const fallbackTp = r.actionTone === "bull"
        ? Math.round((r.spot + 2.0 * risk) * 10) / 10
        : r.actionTone === "bear"
        ? Math.round((r.spot - 2.0 * risk) * 10) / 10
        : null;
      const targetPrice = tgt ? Number(tgt[1]) : fallbackTp;

      let cleanSl = r.dynamicSl;
      if (r.spot > 15000 && cleanSl != null && cleanSl > 0 && cleanSl < 10000) {
        const prefix = Math.floor(r.spot / 10000) * 10000;
        if (Math.abs(r.spot - (prefix + cleanSl)) <= 500) {
          cleanSl = prefix + cleanSl;
        }
      }

      fresh.push({
        hhmm: r.ts,          // the row holds only "HH:MM"; the server resolves it against today (IST)
        key,
        setup: String(r.stageBadge || "").replace(/^[^\w]+/, "").trim(),
        side: r.actionTone === "bear" ? "bear" : r.actionTone === "bull" ? "bull" : String(r.actionTone),
        entry: r.spot,
        sl: cleanSl,
        tp: targetPrice,
        conviction_pct: r.convictionPct ?? null,
        conviction_band: String(r.convictionBadge || ""),
        synergy: Boolean(r.isSynergy),
        scissor: Boolean(r.isScissor),
        spot: r.spot,
        payload: {
          action: r.actionText,
          risk_pts: r.riskPts ?? null,
          call_angle: r.callAngleDeg ?? null,
          put_angle: r.putAngleDeg ?? null,
          session_high: r.sessionHigh ?? null,
          session_low: r.sessionLow ?? null,
          suppressed: Boolean(r.suppressed),
        },
      });
    }

    if (fresh.length === 0) return;
    fetch("/api/setups/log", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ setups: fresh.slice(0, 50) }),
    }).catch((e) => {
      // Logging is observational. If it fails the desk must carry on exactly as before — but say so,
      // rather than swallowing it, or a silently empty table looks like "no setups fired".
      console.warn("[setup-log] post failed, setups not recorded:", e);
    });
  }, [smartRows, rvolReady]);

  // Audio acoustic alert on new candle arrival for A+ setups
  const lastEvaluatedTableTsRef = useRef<string | null>(null);
  const initialLoadRef = useRef(true);

  useEffect(() => {
    if (smartRows.length === 0) return;
    const latestRow = smartRows[0]; // smartRows is newest first
    if (!latestRow) return;

    if (initialLoadRef.current) {
      initialLoadRef.current = false;
      lastEvaluatedTableTsRef.current = latestRow.ts;
      return;
    }

    if (latestRow.ts !== lastEvaluatedTableTsRef.current) {
      lastEvaluatedTableTsRef.current = latestRow.ts;
      if (soundOn) {
        // 💰 Short-Covering Reversal / Book Profits
        if (latestRow.stage === 5 || latestRow.isShortCoveringReversal) {
          playOiBeep("scissor");
        }
        // 🎯 Stage 2 Pullback Entry or 🔥 Floor/Ceiling Shift or ⚡ A+ Synergy
        else if (latestRow.stage === 2 || latestRow.isWallShiftBear || latestRow.isWallShiftBull || latestRow.isSynergy) {
          if (latestRow.actionTone === "bear" || latestRow.isWallShiftBear) {
            playOiBeep("angleBear");
          } else if (latestRow.actionTone === "bull" || latestRow.isWallShiftBull) {
            playOiBeep("angleBull");
          }
        }
      }
    }
  }, [smartRows, soundOn]);

  // Card 1 active trade plan row (locks onto last market-hours row if viewing after 15:30)
  const isEodSnapshot = smartRows.length > 0 && smartRows[0].ts >= "15:30";
  const activePlanRow = (() => {
    if (smartRows.length === 0) return null;
    if (isEodSnapshot) {
      const lastActive = smartRows.find((r) => r.ts < "15:30");
      return lastActive ?? smartRows[0];
    }
    return smartRows[0];
  })();

  const handlePivotClick = (ts: string) => {
    setHighlightedTs(ts);
    if (ts >= "15:30" && !showAfterClose) {
      setShowAfterClose(true);
    }
    setTimeout(() => {
      const el = document.getElementById(`pece-row-${ts}`);
      if (el) {
        el.scrollIntoView({ behavior: "smooth", block: "center" });
      }
    }, 50);
  };

  const c = (v: number | null | undefined) => ((v ?? 0) >= 0 ? "text-bull-400" : "text-bear-400");
  const th = "sticky top-0 z-10 bg-ink-900 px-2.5 py-1.5 text-[10.5px] font-semibold uppercase tracking-wider text-slate-400 whitespace-nowrap border-b border-ink-700";
  const td = "px-2.5 py-1.5 whitespace-nowrap text-[11.5px]";

  // Filter rows according to 1-click modes
  const filteredRows = smartRows.filter((r, idx) => {
    if (!showAfterClose && r.ts >= "15:30") {
      return false;
    }
    if (filterMode === "actionable") {
      return r.isActionable || idx < 3;
    }
    if (filterMode === "bursts") {
      return r.isHighConviction;
    }
    return true; // all_market
  });

  const actionableCount = smartRows.filter((r) => (showAfterClose || r.ts < "15:30") && r.isActionable).length;
  const burstCount = smartRows.filter((r) => (showAfterClose || r.ts < "15:30") && r.isHighConviction).length;
  const marketRowsCount = smartRows.filter((r) => showAfterClose || r.ts < "15:30").length;

  if (tableMode === "matrix") {
    return (
      <div className="flex h-full flex-col bg-[#070c17]">
        <div className="flex items-center justify-between border-b border-ink-800 bg-[#091122] px-3 py-1 text-[11px]">
          <div className="flex items-center gap-2">
            <span className="font-extrabold text-emerald-400">⚡ PE-CE Institutional Matrix 2.0</span>
            <span className="text-slate-500 hidden sm:inline">• Center-Zero Diverging Bars • Auto ATM Scope • Futures Buildup</span>
          </div>
          <div className="flex rounded bg-[#0b1324] p-0.5 border border-slate-700">
            <button
              onClick={() => switchTableMode("matrix")}
              className="rounded px-2.5 py-0.5 font-extrabold text-[11px] bg-emerald-500/30 text-emerald-200 border border-emerald-500/60 shadow-sm"
            >
              ⚡ Matrix 2.0 (Diverging Bars)
            </button>
            <button
              onClick={() => switchTableMode("classic")}
              className="rounded px-2.5 py-0.5 font-bold text-[11px] text-slate-400 hover:text-slate-200"
            >
              Classic PE-CE Table
            </button>
          </div>
        </div>
        <div className="flex-1 min-h-0">
          <PeceMatrix tick={tick} />
        </div>
      </div>
    );
  }

  return (
    <div className="flex h-full flex-col bg-[#0b1220]">
      <div className="flex items-center justify-between border-b border-ink-800 bg-[#091122] px-3 py-1 text-[11px]">
        <div className="flex items-center gap-2">
          <span className="font-bold text-slate-400">Classic PE − CE OI Table</span>
        </div>
        <div className="flex rounded bg-[#0b1324] p-0.5 border border-slate-700">
          <button
            onClick={() => switchTableMode("matrix")}
            className="rounded px-2.5 py-0.5 font-bold text-[11px] text-slate-400 hover:text-emerald-300"
          >
            ⚡ Matrix 2.0 (Diverging Bars)
          </button>
          <button
            onClick={() => switchTableMode("classic")}
            className="rounded px-2.5 py-0.5 font-extrabold text-[11px] bg-slate-700 text-slate-200 border border-slate-600 shadow-sm"
          >
            Classic PE-CE Table
          </button>
        </div>
      </div>
      <Controls
        tf={tf}
        setTf={setTf}
        mode={mode}
        setMode={setMode}
        n={n}
        setN={setN}
        tfs={["1m", "3m", "5m", "15m", "1h"]}
        busy={busy}
        info={d?.ok ? `${d.day} · ${rawRows.length} bars · ${d.range} · NIFTY ${d.spot?.toLocaleString("en-IN")} · ${d.snapshots} snapshots` : d?.message ?? "loading…"}
      />

      {/* ───────────────── TOP STICKY EXECUTIVE DECISION COCKPIT (4 CARDS) ───────────────── */}
      <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-4 gap-2.5 p-3 border-b border-ink-800 bg-[#070c18]">
        {/* Card 1: Live Execution Command & Trade Plan */}
        <div className="rounded-lg border border-ink-700 bg-gradient-to-b from-ink-900/90 to-[#070c18] p-2.5 shadow-md flex flex-col justify-between">
          <div className="flex items-center justify-between text-[11px] mb-1">
            <span className="font-extrabold uppercase tracking-wider text-slate-300 flex items-center gap-1">
              <span>⚡ Trade Command</span>
            </span>
            {isEodSnapshot && (
              <span className="px-1.5 py-0.2 rounded text-[9.5px] font-extrabold bg-amber-500/20 text-amber-300 border border-amber-500/40">
                🕒 EOD Snapshot ({activePlanRow?.ts})
              </span>
            )}
          </div>
          {activePlanRow ? (
            <div>
              <div className="flex items-center gap-1.5 mb-1.5 flex-wrap">
                <span className={`px-2 py-0.5 rounded text-[10px] font-extrabold border ${
                  activePlanRow.stage === 2 || activePlanRow.stage === 5
                    ? "bg-rose-950/90 text-rose-300 border-rose-500 animate-pulse"
                    : activePlanRow.stage === 1
                    ? "bg-amber-950/90 text-amber-300 border-amber-500"
                    : activePlanRow.stage === 3
                    ? "bg-orange-950/90 text-orange-300 border-orange-500"
                    : "bg-ink-850 text-slate-300 border-ink-700"
                }`}>
                  STAGE {activePlanRow.stage} · {activePlanRow.stageBadge}
                </span>
                {activePlanRow.isSynergy && (
                  <span className="px-1.5 py-0.5 rounded text-[9.5px] font-extrabold bg-gold-500/20 text-gold-300 border border-gold-500/60 flex items-center gap-0.5">
                    ⚡ A+ Synergy
                  </span>
                )}
                <span className={`px-1.5 py-0.5 rounded text-[9.5px] font-extrabold flex items-center gap-0.5 border ${
                  activePlanRow.actionTone === "bear"
                    ? "bg-rose-950/80 text-rose-300 border-rose-500/70"
                    : activePlanRow.actionTone === "bull"
                    ? "bg-emerald-950/80 text-emerald-300 border-emerald-500/70"
                    : "bg-ink-850 text-slate-400 border-ink-700"
                }`}>
                  ⚔️ Prahaar ATM: {activePlanRow.actionTone === "bear" ? `BUY ${activePlanRow.atmStrike} PE` : activePlanRow.actionTone === "bull" ? `BUY ${activePlanRow.atmStrike} CE` : "Standby"}
                </span>
                <span className="text-gold-300 text-[13px] font-mono font-black tracking-wide">
                  {activePlanRow.actionTone === "bear"
                    ? `BUY ${activePlanRow.atmStrike} PE`
                    : activePlanRow.actionTone === "bull"
                    ? `BUY ${activePlanRow.atmStrike} CE`
                    : "WAIT / NO SETUP"}
                </span>
              </div>
              <div className="grid grid-cols-2 gap-x-2 gap-y-0.5 text-[11px]">
                <div>
                  <span className="text-slate-400">Entry: </span>
                  <b className="text-slate-100 font-mono">
                    {activePlanRow.actionTone === "bear"
                      ? `${Math.round(activePlanRow.swingHigh30m - 15)}–${Math.round(activePlanRow.swingHigh30m + 5)}`
                      : `${Math.round(activePlanRow.swingLow30m - 5)}–${Math.round(activePlanRow.swingLow30m + 15)}`}
                  </b>
                </div>
                <div>
                  <span className="text-slate-400">Swing SL: </span>
                  <b className="text-amber-400 font-mono">
                    {activePlanRow.dynamicSl ?? Math.round(activePlanRow.swingHigh30m + 15)}
                    {activePlanRow.riskPts ? ` (${activePlanRow.riskPts}pt)` : ""}
                  </b>
                </div>
                <div className="col-span-2 pt-0.5">
                  <span className="text-slate-400">Target: </span>
                  <b className="text-emerald-400 font-mono">
                    {activePlanRow.actionTone === "bear"
                      ? `${Math.round(activePlanRow.swingLow30m - 30)} (Wall: ${activePlanRow.sup?.toLocaleString("en-IN") ?? "22,500"})`
                      : `${Math.round(activePlanRow.swingHigh30m + 30)} (Wall: ${activePlanRow.res?.toLocaleString("en-IN") ?? "23,000"})`}
                  </b>
                </div>
              </div>
            </div>
          ) : (
            <div className="text-slate-500 text-[11px]">Loading trade plan…</div>
          )}
        </div>

        {/* Card 2: Cumulative Writer Dominance */}
        <div className="rounded-lg border border-ink-700 bg-gradient-to-b from-ink-900/90 to-[#070c18] p-2.5 shadow-md flex flex-col justify-between">
          <div className="flex items-center justify-between text-[11px] mb-1">
            <span className="font-extrabold uppercase tracking-wider text-slate-300">⚔️ Writer Tug-of-War</span>
            <span className="text-[10.5px] text-slate-400 font-mono">
              PCR <b className={c(activePlanRow?.pcr ? activePlanRow.pcr - 1 : 0)}>{activePlanRow?.pcr?.toFixed(2) ?? "—"}</b> · Vol <b className="text-amber-300">{activePlanRow?.vol_pcr?.toFixed(2) ?? "—"}</b>
            </span>
          </div>
          {activePlanRow && (() => {
            const pDay = activePlanRow.pe_day ?? 0;
            const cDay = activePlanRow.ce_day ?? 0;
            const tot = Math.max(1, Math.abs(pDay) + Math.abs(cDay));
            const pPct = Math.round((Math.abs(pDay) / tot) * 100);
            const cPct = 100 - pPct;
            return (
              <div>
                <div className="flex justify-between text-[11px] font-mono font-bold">
                  <span className="text-emerald-400">Put +{inr(pDay)} ({pPct}%)</span>
                  <span className="text-rose-400">Call +{inr(cDay)} ({cPct}%)</span>
                </div>
                <div className="flex h-2.5 w-full overflow-hidden rounded bg-ink-800 my-1.5 border border-ink-700">
                  <div style={{ width: `${pPct}%` }} className="bg-emerald-500 transition-all duration-300" title={`Put Writer Share: ${pPct}%`} />
                  <div style={{ width: `${cPct}%` }} className="bg-rose-500 transition-all duration-300" title={`Call Writer Share: ${cPct}%`} />
                </div>
                <div className="text-[11px] flex justify-between items-center">
                  <span className="text-slate-400">Net Day:</span>
                  <b className={`font-mono ${c(activePlanRow.pe_ce_day)}`}>
                    {inr(activePlanRow.pe_ce_day)} ({activePlanRow.pe_ce_day && activePlanRow.pe_ce_day < 0 ? "Bearish Dominated" : "Bullish Dominated"})
                  </b>
                </div>
              </div>
            );
          })()}
        </div>

        {/* Card 3: 15m Rolling Velocity & Pullback Radar */}
        <div className="rounded-lg border border-ink-700 bg-gradient-to-b from-ink-900/90 to-[#070c18] p-2.5 shadow-md flex flex-col justify-between">
          <div className="flex items-center justify-between text-[11px] mb-1">
            <span className="font-extrabold uppercase tracking-wider text-slate-300">📡 Pullback Radar</span>
            <span className="text-[10px] text-slate-400">
              15m Net: <b className={`font-mono ${activePlanRow && activePlanRow.impulse15mL < 0 ? "text-rose-400" : "text-emerald-400"}`}>{activePlanRow ? `${activePlanRow.impulse15mL > 0 ? "+" : ""}${activePlanRow.impulse15mL.toFixed(1)}L` : "—"}</b>
            </span>
          </div>
          {activePlanRow && (
            <div>
              <div className="flex items-center justify-between text-[11px] mb-1">
                <span className="text-slate-400">30m Range:</span>
                <span className="font-mono text-slate-200">
                  {activePlanRow.swingLow30m.toFixed(0)} – {activePlanRow.swingHigh30m.toFixed(0)}{" "}
                  <span className="text-slate-500">({(activePlanRow.swingHigh30m - activePlanRow.swingLow30m).toFixed(0)} pts)</span>
                </span>
              </div>
              <div className="flex items-center justify-between text-[10px] text-slate-400 font-mono mb-1">
                <span>Call Angle: <b className={activePlanRow.callAngleDeg > 0 ? "text-rose-400" : "text-emerald-400"}>{activePlanRow.callAngleDeg >= 0 ? "+" : ""}{activePlanRow.callAngleDeg.toFixed(0)}°</b></span>
                <span>Put Angle: <b className={activePlanRow.putAngleDeg > 0 ? "text-emerald-400" : "text-rose-400"}>{activePlanRow.putAngleDeg >= 0 ? "+" : ""}{activePlanRow.putAngleDeg.toFixed(0)}°</b></span>
              </div>
              <div className="mt-1">
                {activePlanRow.isPullbackToSupply ? (
                  <div className="rounded px-2 py-0.5 text-[10.5px] font-extrabold bg-amber-950/90 text-amber-300 border border-amber-500/80 animate-pulse text-center">
                    🎯 AT PULLBACK SELL ZONE (Optimal Low-Risk Entry)
                  </div>
                ) : activePlanRow.spotChange15m < -80 ? (
                  <div className="rounded px-2 py-0.5 text-[10.5px] font-extrabold bg-rose-950/80 text-rose-300 border border-rose-500/70 text-center">
                    ⚠️ OVER-EXTENDED DROP (Wait for Bounce)
                  </div>
                ) : activePlanRow.isShortCoveringReversal ? (
                  <div className="rounded px-2 py-0.5 text-[10.5px] font-extrabold bg-emerald-950/80 text-emerald-300 border border-emerald-500/70 text-center">
                    💰 SHORT-COVERING REVERSAL ACTIVE
                  </div>
                ) : (
                  <div className="rounded px-2 py-0.5 text-[10.5px] font-bold bg-ink-850 text-slate-300 border border-ink-700 text-center">
                    ↔️ MID-RANGE CONSOLIDATION (Hold / Trail)
                  </div>
                )}
              </div>
            </div>
          )}
        </div>

        {/* Card 4: Active Wall Corridor & Key Pivots Timeline */}
        <div className="rounded-lg border border-ink-700 bg-gradient-to-b from-ink-900/90 to-[#070c18] p-2.5 shadow-md flex flex-col justify-between">
          <div className="flex items-center justify-between text-[11px] mb-1">
            <span className="font-extrabold uppercase tracking-wider text-slate-300">🏛️ Active Corridor</span>
            <span className="text-[10px] text-slate-400 font-mono">Spot: <b className="text-cyan-300">{activePlanRow?.spot.toLocaleString("en-IN")}</b></span>
          </div>
          {activePlanRow && (() => {
            const sup = activePlanRow.sup ?? 22500;
            const res = activePlanRow.res ?? 23000;
            const span = Math.max(50, res - sup);
            const posPct = Math.min(100, Math.max(0, ((activePlanRow.spot - sup) / span) * 100));
            return (
              <div>
                <div className="flex justify-between text-[10px] font-mono text-slate-400">
                  <span className="text-emerald-400 font-bold">Sup {sup.toLocaleString("en-IN")}</span>
                  <span className="text-rose-400 font-bold">Res {res.toLocaleString("en-IN")}</span>
                </div>
                <div className="relative h-2 w-full rounded bg-ink-800 my-1 overflow-hidden border border-ink-700">
                  <div className="absolute top-0 bottom-0 bg-cyan-500/25" style={{ left: 0, width: `${posPct}%` }} />
                  <div className="absolute top-0 bottom-0 w-1.5 bg-cyan-400 rounded-full shadow" style={{ left: `calc(${posPct}% - 3px)` }} />
                </div>
                <div className="flex flex-wrap gap-1 mt-1">
                  {pivots.map((p) => (
                    <button
                      key={p.ts}
                      onClick={() => handlePivotClick(p.ts)}
                      className={`px-1.5 py-0.2 rounded text-[9.5px] font-mono font-bold transition-all border ${
                        highlightedTs === p.ts
                          ? "bg-gold-500 text-ink-950 border-gold-300 font-extrabold ring-1 ring-gold-400"
                          : p.tone === "bear"
                          ? "bg-rose-950/60 text-rose-300 border-rose-800/60 hover:bg-rose-900/70"
                          : p.tone === "bull"
                          ? "bg-emerald-950/60 text-emerald-300 border-emerald-800/60 hover:bg-emerald-900/70"
                          : "bg-amber-950/60 text-amber-300 border-amber-800/60 hover:bg-amber-900/70"
                      }`}
                      title={`Click to jump and highlight ${p.label} in the table`}
                    >
                      {p.label}
                    </button>
                  ))}
                </div>
              </div>
            );
          })()}
        </div>
      </div>

      {/* ───────────────── 1-CLICK FILTER MODES & OPTIONS BAR ───────────────── */}
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-ink-800 bg-[#070c18] px-3 py-1.5 text-[11px]">
        <div className="flex items-center gap-1.5">
          <span className="text-[10px] uppercase font-bold text-slate-400 mr-1">Views:</span>
          <button
            onClick={() => setFilterMode("actionable")}
            className={`px-2.5 py-0.5 rounded font-bold transition-colors ${
              filterMode === "actionable"
                ? "bg-gold-500/25 text-gold-300 border border-gold-500/70"
                : "bg-ink-850 text-slate-400 border border-ink-700 hover:text-slate-200"
            }`}
            title="Show only Stage 1 Breakouts, Stage 2 Pullbacks, Stage 3 Adds, Stage 5 Reversals, Wall Shifts, & Latest rows"
          >
            🎯 Actionable Setups ({actionableCount})
          </button>
          <button
            onClick={() => setFilterMode("bursts")}
            className={`px-2.5 py-0.5 rounded font-bold transition-colors ${
              filterMode === "bursts"
                ? "bg-rose-500/25 text-rose-300 border border-rose-500/70"
                : "bg-ink-850 text-slate-400 border border-ink-700 hover:text-slate-200"
            }`}
            title="Show only rows with High Conviction (●●/●●●) or large OI Shock (|PE−CE Δ| >= 15L)"
          >
            🔥 High-Conviction Bursts ({burstCount})
          </button>
          <button
            onClick={() => setFilterMode("all_market")}
            className={`px-2.5 py-0.5 rounded font-bold transition-colors ${
              filterMode === "all_market"
                ? "bg-cyan-500/25 text-cyan-300 border border-cyan-500/70"
                : "bg-ink-850 text-slate-400 border border-ink-700 hover:text-slate-200"
            }`}
            title="Show all intraday rows up to 15:30 (Noise rows dimmed)"
          >
            📋 All Market Rows ({marketRowsCount})
          </button>
        </div>

        <div className="flex items-center gap-2">
          <button
            onClick={toggleSound}
            className={`px-2 py-0.5 rounded text-[10.5px] font-bold border transition-colors ${
              soundOn
                ? "bg-emerald-950/70 text-emerald-300 border-emerald-600/70 hover:bg-emerald-900/80"
                : "bg-ink-850 text-slate-400 border-ink-700 hover:text-slate-200"
            }`}
            title="Toggle Web Audio acoustic chimes on Stage 2 Pullbacks, Wall Shifts & Reversals"
          >
            {soundOn ? "🔊 Audio: ON" : "🔇 Audio: OFF"}
          </button>
          <button
            onClick={handleTestChime}
            className="px-2 py-0.5 rounded text-[10.5px] font-bold border bg-ink-850 text-gold-300 border-ink-700 hover:bg-ink-800"
            title="Test acoustic A+ setup harmonic chime"
          >
            🔔 Test Chime
          </button>
          <button
            onClick={() => playOiBeep("angleBear")}
            className="px-2 py-0.5 rounded text-[10px] font-bold border bg-rose-950/60 text-rose-300 border-rose-800/60 hover:bg-rose-900/70 flex items-center gap-1"
            title="Test Prahaar Bearish Sniper Acoustic Chime (angleBear)"
          >
            <span>🔴</span>
            <span>Sniper PE Chime</span>
          </button>
          <button
            onClick={() => playOiBeep("angleBull")}
            className="px-2 py-0.5 rounded text-[10px] font-bold border bg-emerald-950/60 text-emerald-300 border-emerald-800/60 hover:bg-emerald-900/70 flex items-center gap-1"
            title="Test Prahaar Bullish Sniper Acoustic Chime (angleBull)"
          >
            <span>🟢</span>
            <span>Sniper CE Chime</span>
          </button>
          <label className="flex items-center gap-1.5 cursor-pointer text-[11px] text-slate-400 select-none hover:text-slate-200 ml-1">
            <input
              type="checkbox"
              checked={showAfterClose}
              onChange={(e) => setShowAfterClose(e.target.checked)}
              className="rounded border-ink-700 bg-ink-800 text-gold-500 focus:ring-0"
            />
            <span>🕒 Show After-Close (15:30+)</span>
          </label>
        </div>
      </div>

      {/* ───────────────── STREAMLINED 7 HIGH-IMPACT COLUMNS TABLE ───────────────── */}
      <div className="min-h-0 flex-1 overflow-auto">
        <table className="w-full border-separate border-spacing-0 text-[11.5px] num">
          <thead>
            <tr>
              <th className={`${th} w-24 sticky left-0 z-20`}>Time</th>
              <th className={`${th} w-44`}>NIFTY & 30m Swing</th>
              <th className={`${th} w-48`}>Bar Flow (Put Δ | Call Δ)</th>
              <th className={`${th} w-44`}>Net PE−CE (Day & Δ)</th>
              <th className={`${th} w-36`}>15m Impulse & PCR</th>
              <th className={`${th} min-w-[280px]`}>🏛️ Unified Tape Read & Conviction</th>
              <th className={`${th} min-w-[310px] !text-gold-400`}>🎯 Trader Action & Dynamic SL</th>
            </tr>
          </thead>
          <tbody>
            {filteredRows.map((r) => {
              const isHighlighted = highlightedTs === r.ts;
              const isDim = filterMode === "all_market" && r.stage === 0 && !r.isActionable;
              const swingSpan = Math.max(1, r.swingHigh30m - r.swingLow30m);
              const swingPos = Math.min(100, Math.max(0, ((r.spot - r.swingLow30m) / swingSpan) * 100));

              return (
                <tr
                  key={r.ts}
                  id={`pece-row-${r.ts}`}
                  className={`border-b border-ink-800/80 transition-colors ${
                    isHighlighted
                      ? "!bg-gold-950/60 ring-2 ring-gold-400 z-10"
                      : Math.abs(r.pe_ce_delta_l) >= 20
                      ? "bg-ink-850/80 hover:bg-ink-800"
                      : "hover:bg-ink-850/50"
                  } ${isDim ? "opacity-50" : ""}`}
                >
                  {/* Column 1: Time + Micro Badges */}
                  <td className={`${td} sticky left-0 z-10 bg-ink-900 border-r border-ink-800 font-semibold text-slate-200`}>
                    <div className="flex flex-col gap-0.5">
                      <span className="font-mono text-slate-100 font-bold">{r.ts}</span>
                      <div className="flex flex-wrap gap-0.5">
                        {r.isSynergy && (
                          <span className="text-[9px] px-1 py-0.2 rounded font-extrabold bg-gold-950/80 text-gold-300 border border-gold-600/60">
                            ⚡ Synergy
                          </span>
                        )}
                        {r.isScissor && (
                          <span className="text-[9px] px-1 py-0.2 rounded font-extrabold bg-rose-950/80 text-rose-300 border border-rose-600/60">
                            ✂️ Scissor
                          </span>
                        )}
                        {r.isPullbackToSupply && (
                          <span className="text-[9px] px-1 py-0.2 rounded font-extrabold bg-amber-950/80 text-amber-300 border border-amber-600/60">
                            🎯 Pullback
                          </span>
                        )}
                        {r.isWallShiftBear && (
                          <span className="text-[9px] px-1 py-0.2 rounded font-extrabold bg-purple-950/80 text-purple-300 border border-purple-600/60">
                            🔥 Wall Shift
                          </span>
                        )}
                        {r.isZeroFlipBear && (
                          <span className="text-[9px] px-1 py-0.2 rounded font-extrabold bg-cyan-950/80 text-cyan-300 border border-cyan-600/60">
                            ⚡ Zero Flip
                          </span>
                        )}
                        {r.isShortCoveringReversal && (
                          <span className="text-[9px] px-1 py-0.2 rounded font-extrabold bg-emerald-950/80 text-emerald-300 border border-emerald-600/60">
                            💰 Reversal
                          </span>
                        )}
                      </div>
                    </div>
                  </td>

                  {/* Column 2: NIFTY Spot & 30m Swing */}
                  <td className={td}>
                    <div className="flex flex-col gap-0.5 font-mono">
                      <div className="flex items-center justify-between">
                        <span className="font-bold text-slate-100 text-[12px]">
                          {r.spot > 0 ? r.spot.toLocaleString("en-IN", { maximumFractionDigits: 1 }) : "—"}
                        </span>
                        <span className="text-[10px] text-slate-400">
                          {r.spot >= r.swingLow30m ? `+${(r.spot - r.swingLow30m).toFixed(0)}p` : ""}
                        </span>
                      </div>
                      <div className="flex items-center gap-1.5 text-[10px] text-slate-400">
                        <span>L: {r.swingLow30m.toFixed(0)}</span>
                        <div className="h-1.5 w-12 bg-ink-800 rounded-full overflow-hidden flex flex-1">
                          <div style={{ width: `${swingPos}%` }} className="bg-cyan-400 h-full rounded-full" />
                        </div>
                        <span>H: {r.swingHigh30m.toFixed(0)}</span>
                      </div>
                    </div>
                  </td>

                  {/* Column 3: Bar Flow (Put Δ | Call Δ) with Mini Inline Bars & 5-Bar Graph Slope Angles */}
                  <td className={td}>
                    <div className="flex flex-col gap-0.5">
                      <div className="flex items-center gap-2 font-mono">
                        <div className="flex-1 flex flex-col items-end">
                          <span className={`font-bold text-[11px] ${c(r.pe_chg)}`}>{inr(r.pe_chg, true)}</span>
                          <div className="h-1 bg-ink-800 w-full rounded overflow-hidden flex justify-end">
                            <div
                              style={{ width: `${Math.min(100, (Math.abs(r.pe_chg ?? 0) / 2e6) * 100)}%` }}
                              className="bg-emerald-500 h-full rounded"
                            />
                          </div>
                        </div>
                        <span className="text-slate-600 font-bold">|</span>
                        <div className="flex-1 flex flex-col items-start">
                          <span className={`font-bold text-[11px] ${c(r.ce_chg)}`}>{inr(r.ce_chg, true)}</span>
                          <div className="h-1 bg-ink-800 w-full rounded overflow-hidden flex justify-start">
                            <div
                              style={{ width: `${Math.min(100, (Math.abs(r.ce_chg ?? 0) / 2e6) * 100)}%` }}
                              className="bg-rose-500 h-full rounded"
                            />
                          </div>
                        </div>
                      </div>
                      <div className="flex items-center justify-between text-[9.5px] text-slate-400 font-mono">
                        <span className={r.putAngleDeg > 0 ? "text-emerald-400 font-semibold" : r.putAngleDeg < 0 ? "text-rose-400 font-semibold" : "text-slate-500"}>
                          Put {r.putAngleDeg >= 0 ? "+" : ""}{r.putAngleDeg.toFixed(0)}°
                        </span>
                        <span className="text-slate-600">·</span>
                        <span className={r.callAngleDeg > 0 ? "text-rose-400 font-semibold" : r.callAngleDeg < 0 ? "text-emerald-400 font-semibold" : "text-slate-500"}>
                          Call {r.callAngleDeg >= 0 ? "+" : ""}{r.callAngleDeg.toFixed(0)}°
                        </span>
                      </div>
                    </div>
                  </td>

                  {/* Column 4: Net PE−CE (Day & Bar Δ) */}
                  <td className={`${td} ${Math.abs(r.pe_ce_delta_l) >= 15 ? (r.pe_ce_delta_l < 0 ? "bg-rose-950/20" : "bg-emerald-950/20") : ""}`}>
                    <div className="flex flex-col font-mono">
                      <div className="flex items-center justify-between">
                        <span className={`font-black text-[12px] ${c(r.pe_ce_day)}`}>{inr(r.pe_ce_day)}</span>
                        {Math.abs(r.pe_ce_delta_l) >= 15 && (
                          <span className="text-[9px] px-1 rounded font-extrabold bg-rose-900/60 text-rose-300 border border-rose-700/60">
                            Shock
                          </span>
                        )}
                      </div>
                      <span className={`text-[10.5px] font-semibold ${c(r.pe_ce_chg)}`}>
                        Bar: {inr(r.pe_ce_chg, true)}
                      </span>
                    </div>
                  </td>

                  {/* Column 5: 15m Rolling Impulse & PCR */}
                  <td className={td}>
                    <div className="flex flex-col font-mono text-[11px]">
                      <span className={`font-bold ${r.impulse15mL < 0 ? "text-rose-400" : "text-emerald-400"}`}>
                        {r.impulse15mL > 0 ? "+" : ""}{r.impulse15mL.toFixed(1)}L{" "}
                        <span className="text-[10px]">
                          {r.impulse15mL < -25 ? "▼▼" : r.impulse15mL > 25 ? "▲▲" : r.impulse15mL < 0 ? "▼" : "▲"}
                        </span>
                      </span>
                      <span className="text-[10px] text-slate-400">
                        PCR {r.pcr?.toFixed(2) ?? "—"} · Vol {r.vol_pcr?.toFixed(2) ?? "—"}
                      </span>
                    </div>
                  </td>

                  {/* Column 6: 🏛️ Unified Tape Read & Conviction */}
                  <td className="px-2.5 py-1.5">
                    <div className="flex flex-col gap-0.5">
                      <div className="flex items-center gap-1.5 flex-wrap">
                        <span className={`px-1.5 py-0.2 rounded text-[10px] font-extrabold border ${
                          r.convictionPct >= 90
                            ? "bg-gold-500/20 text-gold-300 border-gold-500/60"
                            : r.convictionPct >= 70
                            ? "bg-emerald-500/20 text-emerald-300 border-emerald-500/50"
                            : r.convictionPct >= 50
                            ? "bg-ink-850 text-slate-300 border-ink-700"
                            : "bg-ink-900 text-slate-500 border-ink-800"
                        }`}>
                          {r.convictionBadge}
                        </span>
                        {r.angleBadge && (
                          <span className="px-1.5 py-0.2 rounded text-[10px] font-extrabold bg-blue-950/80 text-blue-300 border border-blue-600/60">
                            {r.angleBadge}
                          </span>
                        )}
                        {r.isScissor && (
                          <span className="px-1.5 py-0.2 rounded text-[10px] font-extrabold bg-rose-950/80 text-rose-300 border border-rose-600/60 animate-pulse">
                            ✂️ Scissor ({r.scissorSpread.toFixed(0)}°)
                          </span>
                        )}
                        {r.wallShiftText && (
                          <span className="px-1.5 py-0.2 rounded text-[10px] font-extrabold bg-purple-950/80 text-purple-300 border border-purple-600/60">
                            {r.wallShiftText}
                          </span>
                        )}
                      </div>
                      <span className="text-[11px] text-slate-300 leading-tight">
                        {r.unifiedRead}
                      </span>
                    </div>
                  </td>

                  {/* Column 7: 🎯 Trader Action & Dynamic SL */}
                  <td className="px-2.5 py-1.5">
                    <div className="flex flex-col gap-0.5">
                      <div className={`px-2 py-1 rounded text-[11px] font-bold border ${
                        r.isSynergy
                          ? "bg-gold-950/90 text-gold-200 border-gold-500 font-extrabold shadow ring-1 ring-gold-400"
                          : r.stage === 2
                          ? "bg-rose-950/90 text-rose-200 border-rose-500 font-extrabold shadow"
                          : r.stage === 5
                          ? "bg-emerald-950/90 text-emerald-200 border-emerald-500 font-extrabold shadow"
                          : r.stage === 1 && r.actionTone === "warn"
                          ? "bg-amber-950/80 text-amber-300 border-amber-600/70"
                          : r.stage === 1
                          ? "bg-rose-950/80 text-rose-300 border-rose-600/70"
                          : r.stage === 3
                          ? "bg-orange-950/80 text-orange-300 border-orange-600/70"
                          : r.stage === 4
                          ? "bg-ink-850 text-slate-300 border-ink-700 font-mono"
                          : "bg-ink-900/60 text-slate-500 border-ink-800"
                      }`}>
                        {r.actionText}
                      </div>
                    </div>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}

// ─────────────────────────────────────── OI change · writer flow chart ───────────────────────────────────────
export type OiAlertKind = "step1cr" | "jump40" | "angleBull" | "angleBear" | "scissor" | "trendDivergence";

export interface TrendDivergenceResult {
  isTrendDiv: boolean;
  isCallAccum: boolean;
  isPutAccum: boolean;
  oiGap: number;
  maxWidening: number;
}

export function detectTrendDivergence(points: { ce?: number | null; pe?: number | null }[], curIdx?: number): TrendDivergenceResult {
  const empty: TrendDivergenceResult = { isTrendDiv: false, isCallAccum: false, isPutAccum: false, oiGap: 0, maxWidening: 0 };
  const idx = curIdx !== undefined ? curIdx : points.length - 1;
  if (idx < 15 || idx >= points.length) return empty;

  const curPt = points[idx];
  if (!curPt || curPt.ce == null || curPt.pe == null) return empty;

  const curCallCr = curPt.ce / 1e7;
  const curPutCr = curPt.pe / 1e7;
  const oiGap = Math.abs(curCallCr - curPutCr);

  // Track widening across a 15 to 30 bars rolling window
  const minLookback = 15;
  const maxLookback = Math.min(30, idx);
  let maxWidening = 0;

  for (let b = minLookback; b <= maxLookback; b++) {
    const pRef = points[idx - b];
    if (pRef && pRef.ce != null && pRef.pe != null) {
      const refGap = Math.abs((pRef.ce - pRef.pe) / 1e7);
      const widening = oiGap - refGap;
      if (widening > maxWidening) {
        maxWidening = widening;
      }
    }
  }

  if (maxWidening < 1.5) {
    return { isTrendDiv: false, isCallAccum: false, isPutAccum: false, oiGap, maxWidening };
  }

  // Directional persistence across the last 10 bars
  let callUpBars = 0;
  let putUpBars = 0;
  for (let k = idx - 9; k <= idx; k++) {
    const pCurr = points[k];
    const pPrev = points[k - 1];
    if (pCurr && pPrev && pCurr.ce != null && pPrev.ce != null && pCurr.ce > pPrev.ce) callUpBars++;
    if (pCurr && pPrev && pCurr.pe != null && pPrev.pe != null && pCurr.pe > pPrev.pe) putUpBars++;
  }

  const p10 = points[idx - 10];
  const callNet10 = p10 && p10.ce != null ? (curPt.ce - p10.ce) / 1e7 : 0;
  const putNet10 = p10 && p10.pe != null ? (curPt.pe - p10.pe) / 1e7 : 0;

  // Call writers accumulating: call increased across >= 8 of last 10 bars while put is flat or declining
  const isCallAccum = callUpBars >= 8 && putNet10 <= 0.10;
  // Put writers accumulating: put increased across >= 8 of last 10 bars while call is flat or declining
  const isPutAccum = putUpBars >= 8 && callNet10 <= 0.10;

  return {
    isTrendDiv: isCallAccum || isPutAccum,
    isCallAccum,
    isPutAccum,
    oiGap,
    maxWidening,
  };
}

export function playOiBeep(kind: OiAlertKind) {
  try {
    const Ctx = window.AudioContext || (window as any).webkitAudioContext;
    if (!Ctx) return;
    const ctx = new Ctx();
    const tone = (freq: number, start: number, dur: number, type: OscillatorType = "sine") => {
      const osc = ctx.createOscillator();
      const gain = ctx.createGain();
      osc.type = type;
      osc.frequency.setValueAtTime(freq, ctx.currentTime + start);
      gain.gain.setValueAtTime(0.18, ctx.currentTime + start);
      gain.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + start + dur);
      osc.connect(gain);
      gain.connect(ctx.destination);
      osc.start(ctx.currentTime + start);
      osc.stop(ctx.currentTime + start + dur);
    };
    if (kind === "step1cr") {
      tone(880, 0, 0.12, "triangle");
    } else if (kind === "jump40") {
      tone(1046, 0, 0.10, "square");
      tone(1318, 0.12, 0.14, "square");
    } else if (kind === "angleBull") {
      tone(587, 0, 0.12, "sine");
      tone(880, 0.13, 0.18, "sine");
    } else if (kind === "angleBear") {
      tone(880, 0, 0.12, "sawtooth");
      tone(523, 0.13, 0.18, "sawtooth");
    } else if (kind === "scissor") {
      tone(784, 0, 0.10, "triangle");
      tone(1046, 0.11, 0.10, "triangle");
      tone(1568, 0.22, 0.22, "triangle");
    } else if (kind === "trendDivergence") {
      // Unique medium-pitch dual chime: C5 (523 Hz) then G5 (784 Hz) harmonious chime
      tone(523.25, 0, 0.18, "sine");
      tone(783.99, 0.15, 0.25, "sine");
    }
  } catch { /* ignore audio autoplay block */ }
}

// ─────────────────────────────────────────── LIVE DESK BIAS FLASH-CARD ───────────────────────────────────────────
export interface DeskBiasPayload {
  ok: boolean;
  bias_title: string;
  bias_theme: "bullish" | "bearish" | "neutral";
  bias_color: string;
  narrative: string;
  target_wall: string;
  risk_note: string;
  supporting_spike: string;
  spot: number;
  vwap: number;
  pcr: number;
  net_oi_chg_cr: string;
  put_oi_chg_cr: string;
  call_oi_chg_cr: string;
  pe_wall: number;
  ce_wall: number;
  updated_at: string;
  opt_expiry?: string;
  fut_contract?: string;
  error?: string;
}

export function DeskBiasBanner({
  optExpiry,
  futContract,
  tick,
}: {
  optExpiry: "cur" | "next" | "both";
  futContract?: "near" | "next" | "combined";
  tick: number;
}) {
  const [data, setData] = useState<DeskBiasPayload | null>(null);
  const [collapsed, setCollapsed] = useState<boolean>(() => {
    try {
      return localStorage.getItem("ict_desk_bias_collapsed") === "true";
    } catch {
      return false;
    }
  });

  const toggleCollapsed = () => {
    setCollapsed((c) => {
      const next = !c;
      try {
        localStorage.setItem("ict_desk_bias_collapsed", String(next));
      } catch {}
      return next;
    });
  };

  const fetchBias = useCallback(() => {
    fetch(`/api/desk/bias?opt_expiry=${optExpiry}&fut_contract=${futContract || "combined"}`)
      .then((r) => r.json())
      .then((j) => {
        if (j?.ok) setData(j);
      })
      .catch((err) => {
        console.error("Failed to fetch desk bias:", err);
      });
  }, [optExpiry, futContract]);

  useEffect(() => {
    fetchBias();
    const interval = setInterval(fetchBias, 15000);
    return () => clearInterval(interval);
  }, [fetchBias, tick]);

  if (!data?.ok) return null;

  const color = data.bias_color || "#38bdf8";

  if (collapsed) {
    return (
      <div
        className="mx-2 my-1 flex items-center justify-between rounded-lg border px-3 py-1 text-xs shadow-md shrink-0 transition-all duration-300"
        style={{
          backgroundColor: "#0b0f19",
          borderColor: `${color}66`,
          boxShadow: `0 0 10px ${color}20`,
        }}
      >
        <div className="flex items-center gap-2">
          <span
            className="px-2 py-0.5 rounded text-[11px] font-black uppercase tracking-wider border"
            style={{
              backgroundColor: `${color}20`,
              borderColor: `${color}88`,
              color: color,
            }}
          >
            {data.bias_title}
          </span>
          <span className="text-[11px] text-slate-300 hidden md:inline truncate max-w-xl">
            {data.narrative}
          </span>
        </div>
        <div className="flex items-center gap-3">
          <span className="text-[11px] text-cyan-400 font-bold hidden sm:inline">
            🎯 {data.target_wall}
          </span>
          <button
            onClick={toggleCollapsed}
            className="text-[10px] text-slate-400 hover:text-white px-1.5 py-0.5 rounded bg-slate-800 border border-slate-700"
            title="Expand Desk Flash-Card"
          >
            Expand ▾
          </button>
        </div>
      </div>
    );
  }

  return (
    <div
      className="mx-2 my-1 rounded-lg border px-3 py-1.5 text-xs shadow-lg shrink-0 transition-all duration-300"
      style={{
        backgroundColor: "#0b0f19",
        borderColor: `${color}66`,
        boxShadow: `0 0 16px ${color}22`,
      }}
    >
      <div className="flex flex-wrap items-center justify-between gap-2.5">
        {/* Left Column: Bias Badge & Key Quantitative Pillars */}
        <div className="flex flex-wrap items-center gap-2">
          <span
            className="px-2.5 py-1 rounded-md text-[11px] font-black tracking-wider uppercase border shadow-sm flex items-center gap-1.5"
            style={{
              backgroundColor: `${color}20`,
              borderColor: `${color}99`,
              color: color,
            }}
          >
            <span className="inline-block w-2 h-2 rounded-full animate-ping" style={{ backgroundColor: color }} />
            {data.bias_title}
          </span>

          {/* Quantitative Pillars Pill */}
          <div className="flex items-center gap-1.5 rounded bg-[#101827] border border-slate-800 px-2 py-0.5 text-[10px] text-slate-300">
            <span>Spot: <b className="text-white font-mono">{data.spot?.toFixed(1) ?? "—"}</b></span>
            <span className="text-slate-600">•</span>
            <span>VWAP: <b className="text-slate-200 font-mono">{data.vwap?.toFixed(1) ?? "—"}</b></span>
            <span className="text-slate-600">•</span>
            <span>PCR: <b className="text-cyan-300 font-mono">{data.pcr?.toFixed(2) ?? "—"}</b></span>
            <span className="text-slate-600">•</span>
            <span>
              Net Δ:{" "}
              <b className={`font-mono ${data.net_oi_chg_cr?.startsWith("-") ? "text-rose-400" : "text-emerald-400"}`}>
                {data.net_oi_chg_cr}
              </b>
            </span>
          </div>
        </div>

        {/* Center: Live Desk Narrative & Flash Insight */}
        <div className="flex-1 min-w-[260px] flex flex-col justify-center">
          <div className="text-[11px] leading-tight text-slate-200">
            {data.narrative}
          </div>
          {data.supporting_spike && (
            <div className="flex items-center gap-1.5 mt-0.5 text-[10px] text-amber-300 font-medium">
              <span className="px-1 py-0.2 rounded bg-amber-950/80 border border-amber-600/50 text-[9px] uppercase font-bold text-amber-300">
                ⚡ Flash Trigger
              </span>
              <span className="text-slate-300 truncate">{data.supporting_spike}</span>
            </div>
          )}
        </div>

        {/* Right Column: Target OI Wall, Risk SL & Clock Controls */}
        <div className="flex items-center gap-2">
          {/* Target Wall Pill */}
          <div className="flex items-center gap-1 bg-[#0c1a2e] border border-sky-700/60 px-2 py-0.5 rounded text-[10px]">
            <span className="text-sky-400 font-bold">🎯 Target:</span>
            <span className="text-sky-100 font-extrabold">{data.target_wall}</span>
          </div>

          {/* Risk Note Pill */}
          <div className="flex items-center gap-1 bg-[#1f1614] border border-amber-700/60 px-2 py-0.5 rounded text-[10px]">
            <span className="text-amber-300 font-medium">{data.risk_note}</span>
          </div>

          {/* Heartbeat & Expiry Tag */}
          <div className="hidden lg:flex items-center gap-1.5 text-[10px] text-slate-400 font-mono bg-slate-900/60 px-2 py-0.5 rounded border border-slate-800">
            <span className="w-1.5 h-1.5 rounded-full bg-emerald-400 inline-block animate-pulse" />
            <span>{data.updated_at}</span>
            <span className="text-slate-500 uppercase font-sans text-[9px]">[{data.opt_expiry}]</span>
          </div>

          {/* Collapse Button */}
          <button
            onClick={toggleCollapsed}
            className="text-[10px] text-slate-400 hover:text-white px-1.5 py-0.5 rounded bg-slate-800/80 border border-slate-700 ml-1 transition-colors"
            title="Collapse into thin strip"
          >
            ▴
          </button>
        </div>
      </div>
    </div>
  );
}

function formatIST(epoch: number): string {
  const d = new Date(epoch * 1000);
  return `${String(d.getUTCHours()).padStart(2, "0")}:${String(d.getUTCMinutes()).padStart(2, "0")} IST`;
}

export function WritersFlow({ tick }: { tick: number }) {
  const [suiteView, setSuiteView] = useState<"smart_oi" | "trinity" | "classic" | "gap_sniper">(() => {
    try {
      const v = localStorage.getItem("ict_writers_view_mode") as "smart_oi" | "trinity" | "classic" | "gap_sniper";
      if (v === "smart_oi" || v === "trinity" || v === "classic" || v === "gap_sniper") return v;
    } catch { return "smart_oi"; }
    return "smart_oi";
  });
  const setViewMode = (m: "smart_oi" | "trinity" | "classic" | "gap_sniper") => {
    setSuiteView(m);
    try { localStorage.setItem("ict_writers_view_mode", m); } catch { /* ignore */ }
  };

  // Shared Dual Expiry & Futures Contract State across all 3 suites
  const [optExpiry, setOptExpiry] = useState<"cur" | "next" | "both">(() => {
    try {
      const saved = localStorage.getItem("ict_writers_opt_expiry") as "cur" | "next" | "both";
      if (saved === "cur" || saved === "next" || saved === "both") return saved;
    } catch { /* ignore */ }
    return "both";
  });

  const [futContract, setFutContract] = useState<"near" | "next" | "combined">(() => {
    try {
      const saved = localStorage.getItem("ict_writers_fut_contract") as "near" | "next" | "combined";
      if (saved === "near" || saved === "next" || saved === "combined") return saved;
    } catch { /* ignore */ }
    return "combined";
  });

  const [expMeta, setExpMeta] = useState<{
    cur_date: string;
    next_date: string;
    cur_expiry?: string;
    next_expiry?: string;
    near_fut: string;
    next_fut: string;
    rollover_pct: number;
    fut_src?: string;
  } | null>(null);

  const handleOptExpiryChange = (exp: "cur" | "next" | "both") => {
    setOptExpiry(exp);
    try { localStorage.setItem("ict_writers_opt_expiry", exp); } catch { /* ignore */ }
  };

  const handleFutContractChange = (fut: "near" | "next" | "combined") => {
    setFutContract(fut);
    try { localStorage.setItem("ict_writers_fut_contract", fut); } catch { /* ignore */ }
  };

  useEffect(() => {
    fetch(`/api/chain/trinity?opt_expiry=${optExpiry}&fut_contract=${futContract}`)
      .then((r) => r.json())
      .then((j) => {
        if (j?.expiries_meta) setExpMeta(j.expiries_meta);
      })
      .catch(() => {});
  }, [optExpiry, futContract, tick]);

  const [tf, setTf] = useState("1m"); const [mode, setMode] = useState("all"); const [n, setN] = useState(10);
  const [d, busy] = useOc<Flow>("flow", `tf=${tf}&mode=${mode}&n=${n}&opt_expiry=${optExpiry}&fut_contract=${futContract}`, tick);
  const box = useRef<HTMLDivElement>(null);
  const S = useRef<{
    ch: IChartApi; fut: ISeriesApi<"Line">; futC: ISeriesApi<"Candlestick">; ce: ISeriesApi<"Line">; pe: ISeriesApi<"Line">; uw: ISeriesApi<"Histogram">;
    fl: ISeriesApi<"Histogram">; flma: ISeriesApi<"Line">; bal: ISeriesApi<"Line">; band: FlowBand;
    mk: ReturnType<typeof createSeriesMarkers<Time>>; mk2: ReturnType<typeof createSeriesMarkers<Time>>;
    mkCe: ReturnType<typeof createSeriesMarkers<Time>>; mkPe: ReturnType<typeof createSeriesMarkers<Time>>;
  } | null>(null);
  const first = useRef(true);
  const flCap = useRef(0);
  const [showFut, setShowFut] = useState(true);   // real NIFTY-future candles overlaid on the left price scale
  const [futCandles, setFutCandles] = useState<{ time: number; o: number; h: number; l: number; c: number }[]>([]);
  const [hoverPt, setHoverPt] = useState<Pt | null>(null);
  const ptMapRef = useRef<Map<number, Pt>>(new Map());
  const validPtsRef = useRef<Pt[]>([]);

  // Real-Time Audio & Angle Alert State
  const [soundOn, setSoundOn] = useState<boolean>(() => {
    try { return localStorage.getItem("oi_beep_alerts") !== "0"; } catch { return true; }
  });
  const [angleThreshold, setAngleThreshold] = useState<number>(() => {
    try {
      const v = Number(localStorage.getItem("oi_angle_threshold"));
      return v === 45 ? 45 : (v === 30 ? 30 : 25);
    } catch {
      return 25;
    }
  });
  const [activeAlert, setActiveAlert] = useState<{ text: string; kind: OiAlertKind; ts: number } | null>(null);

  // Marker visibility & declutter filter mode: "off" | "latest" | "all"
  const [markerMode, setMarkerMode] = useState<"off" | "latest" | "all">(() => {
    try {
      const saved = localStorage.getItem("ict_oi_markers_mode");
      if (saved === "off" || saved === "latest" || saved === "all") return saved;
      return "all"; // Default to "all" so historical markers are immediately visible!
    } catch {
      return "all";
    }
  });

  // Individual category sub-toggles (active when markerMode !== "off")
  const [showSteepMarkers, setShowSteepMarkers] = useState<boolean>(() => {
    try {
      return localStorage.getItem("ict_oi_marker_steep") !== "0"; // default ON
    } catch {
      return true;
    }
  });
  const [showJumpMarkers, setShowJumpMarkers] = useState<boolean>(() => {
    try {
      return localStorage.getItem("ict_oi_marker_jump") !== "0"; // default ON
    } catch {
      return true;
    }
  });
  const [showStepMarkers, setShowStepMarkers] = useState<boolean>(() => {
    try {
      return localStorage.getItem("ict_oi_marker_step") === "1"; // default OFF
    } catch {
      return false;
    }
  });

  const lastEvaluatedTimeRef = useRef<number>(0);
  const prevCallBucketRef = useRef<number | null>(null);
  const prevPutBucketRef = useRef<number | null>(null);
  const lastTrendDivAlertBarRef = useRef<number>(-999);

  const toggleSound = () => {
    setSoundOn((prev) => {
      const next = !prev;
      try { localStorage.setItem("oi_beep_alerts", next ? "1" : "0"); } catch { /* ignore */ }
      return next;
    });
  };

  const toggleAngleThreshold = (thresh: number) => {
    setAngleThreshold(thresh);
    try { localStorage.setItem("oi_angle_threshold", String(thresh)); } catch { /* ignore */ }
  };

  const cycleMarkerMode = () => {
    setMarkerMode((prev) => {
      const next = prev === "off" ? "latest" : prev === "latest" ? "all" : "off";
      try { localStorage.setItem("ict_oi_markers_mode", next); } catch { /* ignore */ }
      return next;
    });
  };

  const toggleSteepMarkers = () => {
    setShowSteepMarkers((prev) => {
      const next = !prev;
      try { localStorage.setItem("ict_oi_marker_steep", next ? "1" : "0"); } catch { /* ignore */ }
      return next;
    });
  };

  const toggleJumpMarkers = () => {
    setShowJumpMarkers((prev) => {
      const next = !prev;
      try { localStorage.setItem("ict_oi_marker_jump", next ? "1" : "0"); } catch { /* ignore */ }
      return next;
    });
  };

  const toggleStepMarkers = () => {
    setShowStepMarkers((prev) => {
      const next = !prev;
      try { localStorage.setItem("ict_oi_marker_step", next ? "1" : "0"); } catch { /* ignore */ }
      return next;
    });
  };

  const handleTestBeep = () => {
    playOiBeep("trendDivergence");
    setActiveAlert({
      text: "🌊 TREND DIVERGENCE (Gap: 2.8Cr): Audio Medium-Pitch Dual Chime & Visual Badge Engine Verified!",
      kind: "trendDivergence",
      ts: Date.now(),
    });
  };

  useEffect(() => {
    if (suiteView !== "classic" || !box.current) return;
    if (!S.current) {
      const ch = createChart(box.current, {
        autoSize: true, layout: { background: { type: ColorType.Solid, color: "#0b1220" }, textColor: "#cbd5e1", fontSize: 12, fontFamily: "Inter, system-ui" },
        grid: { vertLines: { color: "#131d33" }, horzLines: { color: "#131d33" } },
        rightPriceScale: { borderColor: "#1d2a45" }, leftPriceScale: { visible: true, borderColor: "#1d2a45" },
        timeScale: {
          borderColor: "#1d2a45",
          timeVisible: true,
          secondsVisible: false,
          fixLeftEdge: true,
          fixRightEdge: true,
          rightOffset: 2,
          lockVisibleTimeRangeOnResize: true,
        },
        localization: {
          locale: "en-IN",
          priceFormatter: (p: number) => inr(p, true),
        },
      });

      ch.subscribeCrosshairMove((param) => {
        if (!param.time) {
          setHoverPt(null);
          return;
        }
        const t = param.time as number;
        const pt = ptMapRef.current.get(t);
        if (pt) {
          setHoverPt(pt);
        } else {
          const pts = validPtsRef.current;
          if (pts.length) {
            const nearest = pts.reduce((prev, curr) => Math.abs(curr.t - t) < Math.abs(prev.t - t) ? curr : prev);
            if (Math.abs(nearest.t - t) <= 900) {
              setHoverPt(nearest);
            } else {
              setHoverPt(null);
            }
          }
        }
      });
      const uw = ch.addSeries(HistogramSeries, { priceScaleId: "uw", color: "rgba(212,175,55,0.16)", priceLineVisible: false, lastValueVisible: false });
      ch.priceScale("uw").applyOptions({ scaleMargins: { top: 0, bottom: 0 }, visible: false });
      const futFmt = { type: "custom" as const, formatter: (p: number) => p.toLocaleString("en-IN", { maximumFractionDigits: 1 }) };
      const futC = ch.addSeries(CandlestickSeries, { priceScaleId: "left", upColor: "#10b981", downColor: "#ef4444", borderVisible: false,
        wickUpColor: "#10b981", wickDownColor: "#ef4444", priceFormat: futFmt, priceLineVisible: false });
      const fut = ch.addSeries(LineSeries, { priceScaleId: "left", color: "#94a3b8", lineWidth: 2, lineStyle: LineStyle.Dotted,
        title: "Future", priceFormat: futFmt });
      const ce = ch.addSeries(LineSeries, { color: "#22c55e", lineWidth: 2, title: "Call OI Change (Call Writers)", priceFormat: { type: "custom", formatter: (p: number) => inr(p) } });
      const pe = ch.addSeries(LineSeries, { color: "#ef4444", lineWidth: 2, title: "Put OI Change (Put Writers)", priceFormat: { type: "custom", formatter: (p: number) => inr(p) } });
      const fl = ch.addSeries(HistogramSeries, { priceLineVisible: false, lastValueVisible: false, title: "flow Δput−Δcall",
        priceFormat: { type: "custom", formatter: (p: number) => inr(p) },
        // clip the scale at ~the 95th-percentile bar so one huge opening print doesn't flatten every other bar
        autoscaleInfoProvider: () => (flCap.current ? { priceRange: { minValue: -flCap.current, maxValue: flCap.current } } : null) }, 1);
      const flma = ch.addSeries(LineSeries, { color: "rgba(226,232,240,0.55)", lineWidth: 1, title: "flow avg", priceLineVisible: false, lastValueVisible: false }, 1);
      const band = new FlowBand(); pe.attachPrimitive(band);          // shaded gap between Call & Put OI
      const bal = ch.addSeries(LineSeries, { color: "#e8c35a", lineWidth: 2, title: "balance 30m", priceLineVisible: false }, 2);
      bal.createPriceLine({ price: 0.25, color: "rgba(34,197,94,0.6)", lineStyle: LineStyle.Dashed, lineWidth: 1, axisLabelVisible: false, title: "▲ PUT writers lead = bullish" });
      bal.createPriceLine({ price: -0.25, color: "rgba(239,68,68,0.6)", lineStyle: LineStyle.Dashed, lineWidth: 1, axisLabelVisible: false, title: "▼ CALL writers lead = bearish" });
      bal.createPriceLine({ price: 0, color: "rgba(148,163,184,0.25)", lineWidth: 1, axisLabelVisible: false, title: "" });
      try { const P = ch.panes(); P[0]?.setStretchFactor(4); P[1]?.setStretchFactor(2); P[2]?.setStretchFactor(1.6); } catch { /* */ }
      S.current = {
        ch, fut, futC, ce, pe, uw, fl, flma, bal, band,
        mk: createSeriesMarkers(fut, []),
        mk2: createSeriesMarkers(fl, []),
        mkCe: createSeriesMarkers(ce, []),
        mkPe: createSeriesMarkers(pe, []),
      };
    }

    const timer = setTimeout(() => {
      if (box.current && S.current) {
        const w = box.current.clientWidth;
        const h = box.current.clientHeight;
        if (w > 0 && h > 0) {
          S.current.ch.applyOptions({ width: w, height: h });
          S.current.ch.timeScale().fitContent();
        }
      }
    }, 50);

    return () => clearTimeout(timer);
  }, [suiteView]);

  // Clean unmount of chart
  useEffect(() => {
    return () => {
      if (S.current) {
        S.current.ch.remove();
        S.current = null;
      }
    };
  }, []);

  // ResizeObserver for Classic Chart container
  useEffect(() => {
    if (!box.current || suiteView !== "classic") return;
    const ro = new ResizeObserver((entries) => {
      for (const entry of entries) {
        const { width, height } = entry.contentRect;
        if (width > 0 && height > 0 && S.current) {
          S.current.ch.applyOptions({ width, height });
          S.current.ch.timeScale().fitContent();
        }
      }
    });
    ro.observe(box.current);
    return () => ro.disconnect();
  }, [suiteView]);

  // fetch the real future candles (adapts to the active expiry automatically) and overlay them on the left scale
  useEffect(() => {
    let dead = false;
    fetch(`/api/fut_candles?tf=${tf}&days=3`).then((r) => r.json())
      .then((j) => { if (!dead) setFutCandles(j.available ? j.candles ?? [] : []); }).catch(() => {});
    return () => { dead = true; };
  }, [tf, tick]);
  // push candle data + toggle candles vs dotted line. Candles are SNAPPED to the exact writer-flow bucket times
  // (same tf grid) so each candle sits precisely under the Call/Put OI point for that candle — no drift at 15m etc.
  useEffect(() => {
    const s = S.current; if (!s) return;
    const tfs: Record<string, number> = { "1m": 60, "3m": 180, "5m": 300, "15m": 900 };
    const step = tfs[tf] ?? 60;

    const isMarketSec = (t: number) => {
      const sec = t % 86400;
      return sec >= 9 * 3600 + 15 * 60 && sec <= 15 * 3600 + 35 * 60; // 09:15 to 15:35 IST
    };

    const validPts = (d?.points ?? [])
      .filter((p) => Number.isFinite(p.t) && isMarketSec(p.t))
      .sort((a, b) => a.t - b.t);

    const candleMap = new Map<number, { time: UTCTimestamp; open: number; high: number; low: number; close: number }>();
    if (showFut && validPts.length) {
      for (const c of futCandles) {
        const cTime = Number(c.time);
        if (!Number.isFinite(cTime) || !isMarketSec(cTime)) continue;
        let matchedT: number | null = null;
        for (let i = validPts.length - 1; i >= 0; i--) {
          if (validPts[i].t <= cTime + step) {
            matchedT = validPts[i].t;
            break;
          }
        }
        if (matchedT != null) {
          candleMap.set(matchedT, {
            time: matchedT as UTCTimestamp,
            open: c.o,
            high: c.h,
            low: c.l,
            close: c.c,
          });
        }
      }
    }
    const cleanCandles = Array.from(candleMap.values()).sort((a, b) => Number(a.time) - Number(b.time));
    s.futC.setData(cleanCandles);
    s.futC.applyOptions({ visible: showFut });
    s.fut.applyOptions({ visible: !showFut });                 // candles replace the dotted line (one price view at a time)
  }, [futCandles, showFut, d, tf, suiteView]);

  useEffect(() => { first.current = true; }, [tf, mode, n]);
  useEffect(() => {
    const s = S.current; const rawP = d?.points;
    if (!s || !rawP || !d?.ok) return;
    const isMarketHours = (t: number) => {
      const sec = t % 86400;
      return sec >= 9 * 3600 + 15 * 60 && sec <= 15 * 3600 + 35 * 60; // 09:15 to 15:35 IST
    };
    const rawFiltered = rawP.filter((p) => Number.isFinite(p.t) && isMarketHours(p.t));
    if (!rawFiltered.length) return;

    // Deduplicate snapshots by timestamp and sort strictly ascending
    const ptMap = new Map<number, Pt>();
    for (const p of rawFiltered) {
      ptMap.set(p.t, p);
    }
    const P = Array.from(ptMap.values()).sort((a, b) => a.t - b.t);
    ptMapRef.current = ptMap;
    validPtsRef.current = P;
    const T = (p: Pt) => p.t as UTCTimestamp;

    function dedupeSeries<TItem extends { time: Time; value: number }>(arr: TItem[]): TItem[] {
      const m = new Map<number, TItem>();
      for (const item of arr) {
        const t = Number(item.time);
        if (Number.isFinite(t) && Number.isFinite(item.value)) {
          m.set(t, item);
        }
      }
      return Array.from(m.values()).sort((a, b) => Number(a.time) - Number(b.time));
    }

    s.fut.setData(dedupeSeries(P.filter((p) => p.fut != null).map((p) => ({ time: T(p), value: p.fut as number }))));
    s.ce.setData(dedupeSeries(P.filter((p) => p.ce != null).map((p) => ({ time: T(p), value: p.ce as number }))));
    s.pe.setData(dedupeSeries(P.filter((p) => p.pe != null).map((p) => ({ time: T(p), value: p.pe as number }))));
    const fv = P.filter((p) => p.flow != null && Number.isFinite(p.flow));
    const abs = fv.map((p) => Math.abs(p.flow as number)).sort((a, b) => a - b);
    flCap.current = abs.length ? Math.max(1, abs[Math.floor(abs.length * 0.95)] * 1.3) : 0;
    const cap = flCap.current;
    // normal candles = faint; unusual (≥ 2.5×) = bright in its own direction; bars beyond the cap are clipped (value shown by the marker)
    s.fl.setData(dedupeSeries(fv.map((p) => {
      const v = p.flow as number, up = v >= 0;
      return {
        time: T(p),
        value: Math.max(-cap, Math.min(cap, v)),
        color: p.sig ? (up ? "#4ade80" : "#f87171") : up ? "rgba(34,197,94,.35)" : "rgba(239,68,68,.35)",
      };
    })));
    // #2 gap ribbon — shade between the Call and Put OI lines
    const cleanBandMap = new Map<number, { time: number; ce: number; pe: number }>();
    for (const p of P) {
      if (p.ce != null && p.pe != null && Number.isFinite(p.ce) && Number.isFinite(p.pe)) {
        cleanBandMap.set(p.t, { time: p.t, ce: p.ce as number, pe: p.pe as number });
      }
    }
    s.band.set(Array.from(cleanBandMap.values()).sort((a, b) => a.time - b.time));
    // #5 flow momentum — a trailing mean of the (clipped) flow so you see the drift under the spikes
    const win = 8; const clipped = fv.map((p) => Math.max(-cap, Math.min(cap, p.flow as number)));
    s.flma.setData(dedupeSeries(fv.map((p, i) => {
      const a = Math.max(0, i - win + 1);
      const seg = clipped.slice(a, i + 1);
      return { time: T(p), value: seg.reduce((x, y) => x + y, 0) / seg.length };
    })));
    s.bal.setData(dedupeSeries(P.filter((p) => p.bal != null).map((p) => ({ time: T(p), value: p.bal as number }))));
    const U = d.unwind;
    s.uw.setData(dedupeSeries(P.map((p) => ({ time: T(p), value: U && p.t >= U[0] && p.t <= U[1] ? 1 : 0 }))));
    const mk: SeriesMarker<Time>[] = P.filter((p) => p.flip && p.conf).map((p) => ({ time: T(p),
      position: p.conf === "BULL" ? "belowBar" : "aboveBar", shape: p.conf === "BULL" ? "arrowUp" : "arrowDown",
      color: p.conf === "BULL" ? "#22c55e" : "#ef4444", text: p.conf === "BULL" ? "▲ BULL" : "▼ BEAR" }));
    const positionEvents=positioningEvents(P.filter((p)=>p.ce!=null && p.pe!=null).map((p)=>({time:p.t,ce:p.ce!,pe:p.pe!,price:p.fut ?? NaN})),Date.now()/1000+19800);
    for(const e of positionEvents) mk.push({time:e.time as UTCTimestamp,position:e.dir==="bull"?"belowBar":"aboveBar",
      shape:e.kind==="divergence"?"circle":e.dir==="bull"?"arrowUp":"arrowDown",
      color:e.kind==="divergence"?"#f59e0b":e.dir==="bull"?"#22c55e":"#ef4444",text:e.text});
    if (U) mk.push({ time: U[0] as UTCTimestamp, position: "aboveBar", shape: "square", color: "#e8c35a", text: "EXPIRY UNWIND →" });
    mk.sort((a, b) => (a.time as number) - (b.time as number));
    s.mk.setMarkers(mk);
    // #5 declutter — only the TOP 6 unusual prints get a label (biggest by size), so the strip isn't a forest of text
    const labelled = fv.filter((p) => p.sig && Math.abs(p.flow as number) >= cap * 0.6)
      .sort((a, b) => Math.abs(b.flow as number) - Math.abs(a.flow as number)).slice(0, 6);
    s.mk2.setMarkers([...labelled].sort((a, b) => a.t - b.t).map((p) => { const up = (p.flow as number) >= 0; return {
      time: T(p), position: up ? "aboveBar" : "belowBar", shape: up ? "arrowUp" : "arrowDown", color: up ? "#4ade80" : "#f87171", size: 0.8,
      text: `${up ? "PUT" : "CALL"} ${inr(Math.abs(p.flow as number))}` }; }));

    // ────────────────── Real-Time OI Velocity, Angle & Milestone Markers ──────────────────
    const allCr = P.flatMap((p) => [p.ce != null ? p.ce / 1e7 : null, p.pe != null ? p.pe / 1e7 : null]).filter((x): x is number => x != null);
    const maxCr = allCr.length ? Math.max(...allCr) : 5.0;
    const minCr = allCr.length ? Math.min(...allCr) : -5.0;
    const ySpan = Math.max(2.0, maxCr - minCr);

    const isExpiry = Boolean(
      d?.unwind ||
      (d?.expiry && d?.day && d.expiry === d.day) ||
      d?.label?.toLowerCase().includes("expiry")
    );
    const isHistoricalOrReplay = Boolean(
      d?.label?.toLowerCase().includes("saved") ||
      (P.length > 50)
    );
    const isExpiryOrReplay = isExpiry || isHistoricalOrReplay;

    // Standardize C_cell to 1.0Cr during expiry sessions & historical replay so visual angles & jumps reflect institutional grid intervals
    const cellCr = isExpiryOrReplay ? 1.0 : (ySpan > 15 ? 2.5 : (ySpan > 6 ? 1.0 : 0.5));
    // Expiry sessions: 0.30 x C_cell (down from 0.40) to catch sudden option writing spikes
    const jumpThreshold = (isExpiryOrReplay ? 0.30 : 0.40) * cellCr;
    // Enforce minimum horizontal spacing between markers on the same line to prevent clutter (10 bars in de-cluttered all mode, 6 bars otherwise)
    const minCooldownBars = markerMode === "all" ? 10 : 6;
    // Sensitivity: catch any slope where |θ| >= 25° during high-velocity expiry hours/replay
    const effAngleThreshold = isExpiryOrReplay ? Math.min(angleThreshold, 25) : angleThreshold;

    if (markerMode === "off") {
      s.mkCe.setMarkers([]);
      s.mkPe.setMarkers([]);
    } else {
      const mkCeList: SeriesMarker<Time>[] = [];
      const mkPeList: SeriesMarker<Time>[] = [];
      let lastCeAnchorBar = -999;
      let lastPeAnchorBar = -999;
      let lastCeMag = 0;
      let lastPeMag = 0;
      let lastStepCeBar = -999;
      let lastStepPeBar = -999;
      let lastMarkedCallCr: number | null = null;
      let lastMarkedPutCr: number | null = null;
      let lastMarkedCallBucket: number | null = null;
      let lastMarkedPutBucket: number | null = null;

      // Scan every single historical snapshot chronologically from index 1 to the end
      for (let i = 1; i < P.length; i++) {
        const p = P[i];
        const pPrev = P[i - 1];
        if (p.ce == null || pPrev.ce == null || p.pe == null || pPrev.pe == null) continue;

        // Never fire jump or steep markers on opening reset bars (09:15-09:18) where overnight resets
        const isSessionStart = i <= 3 || p.hm === "09:15" || p.hm === "09:16" || p.hm === "09:17" || p.hm === "09:18";

        const callCr = p.ce / 1e7;
        const prevCallCr = pPrev.ce / 1e7;
        const putCr = p.pe / 1e7;
        const prevPutCr = pPrev.pe / 1e7;

        // --- Call (Crimson Red Line - Resistance) Independent Evaluation ---
        let isCeJump = false;
        let dCeJump = 0;
        if (!isSessionStart && showJumpMarkers && i >= 3) {
          const p3 = P[i - 3];
          if (p3.ce != null) {
            const dCall3 = (p.ce - p3.ce) / 1e7;
            if (Math.abs(dCall3) >= jumpThreshold) {
              isCeJump = true;
              dCeJump = dCall3;
            }
          }
        }

        let isCeSteep = false;
        let ceAngle = 0;
        if (!isSessionStart && showSteepMarkers && i >= 5) {
          const p5 = P[i - 5];
          if (p5.ce != null) {
            const dCall5 = (p.ce - p5.ce) / 1e7;
            ceAngle = Math.atan(dCall5 / cellCr) * (180 / Math.PI);
            if (Math.abs(ceAngle) >= effAngleThreshold) {
              isCeSteep = true;
            }
          }
        }

        if (isCeJump || isCeSteep) {
          const mag = Math.max(Math.abs(dCeJump), Math.abs(ceAngle) / 30.0);
          const txt = isCeJump && isCeSteep
            ? `⚡ ${dCeJump > 0 ? "+" : ""}${dCeJump.toFixed(2)}Cr · 📐 ${ceAngle > 0 ? "+" : ""}${ceAngle.toFixed(0)}°`
            : isCeJump
            ? `⚡ 40% JUMP (${dCeJump > 0 ? "+" : ""}${dCeJump.toFixed(2)}Cr)`
            : `📐 ${ceAngle > 0 ? "+" : ""}${ceAngle.toFixed(0)}° STEEP`;
          const isUp = isCeJump ? dCeJump > 0 : ceAngle > 0;

          if (i - lastCeAnchorBar < minCooldownBars) {
            if (mag > lastCeMag) {
              if (mkCeList.length > 0) mkCeList.pop();
              lastCeMag = mag;
              mkCeList.push({
                time: p.t as UTCTimestamp,
                position: isUp ? "aboveBar" : "belowBar",
                shape: isUp ? "arrowUp" : "arrowDown",
                color: isUp ? "#ef4444" : "#f87171",
                size: isCeJump && isCeSteep ? 1.2 : 1.0,
                text: txt,
              });
            }
          } else {
            lastCeAnchorBar = i;
            lastCeMag = mag;
            mkCeList.push({
              time: p.t as UTCTimestamp,
              position: isUp ? "aboveBar" : "belowBar",
              shape: isUp ? "arrowUp" : "arrowDown",
              color: isUp ? "#ef4444" : "#f87171",
              size: isCeJump && isCeSteep ? 1.2 : 1.0,
              text: txt,
            });
          }
        } else if (showStepMarkers && i - lastStepCeBar >= minCooldownBars && i - lastCeAnchorBar >= minCooldownBars) {
          // Milestone 1.0 Cr crossings
          const bCall = Math.trunc(callCr);
          const prevBCall = Math.trunc(prevCallCr);
          if (bCall !== prevBCall && Math.abs(callCr) >= 1.0) {
            const traversed = lastMarkedCallCr === null || Math.abs(callCr - lastMarkedCallCr) >= 0.85 || (i - lastStepCeBar >= 10);
            const bucketChanged = lastMarkedCallBucket === null || bCall !== lastMarkedCallBucket;
            if (traversed && bucketChanged) {
              lastCeAnchorBar = i;
              lastStepCeBar = i;
              lastMarkedCallCr = callCr;
              lastMarkedCallBucket = bCall;
              mkCeList.push({
                time: p.t as UTCTimestamp,
                position: callCr >= prevCallCr ? "aboveBar" : "belowBar",
                shape: "square",
                color: "#ef4444",
                size: 0.9,
                text: `🔔 ${callCr >= 0 ? "+" : ""}${bCall}Cr`,
              });
            }
          }
        }

        // --- Put (Emerald Green Line - Support) Independent Evaluation ---
        let isPeJump = false;
        let dPeJump = 0;
        if (!isSessionStart && showJumpMarkers && i >= 3) {
          const p3 = P[i - 3];
          if (p3.pe != null) {
            const dPut3 = (p.pe - p3.pe) / 1e7;
            if (Math.abs(dPut3) >= jumpThreshold) {
              isPeJump = true;
              dPeJump = dPut3;
            }
          }
        }

        let isPeSteep = false;
        let peAngle = 0;
        if (!isSessionStart && showSteepMarkers && i >= 5) {
          const p5 = P[i - 5];
          if (p5.pe != null) {
            const dPut5 = (p.pe - p5.pe) / 1e7;
            peAngle = Math.atan(dPut5 / cellCr) * (180 / Math.PI);
            if (Math.abs(peAngle) >= effAngleThreshold) {
              isPeSteep = true;
            }
          }
        }

        if (isPeJump || isPeSteep) {
          const mag = Math.max(Math.abs(dPeJump), Math.abs(peAngle) / 30.0);
          const txt = isPeJump && isPeSteep
            ? `⚡ ${dPeJump > 0 ? "+" : ""}${dPeJump.toFixed(2)}Cr · 📐 ${peAngle > 0 ? "+" : ""}${peAngle.toFixed(0)}°`
            : isPeJump
            ? `⚡ 40% JUMP (${dPeJump > 0 ? "+" : ""}${dPeJump.toFixed(2)}Cr)`
            : `📐 ${peAngle > 0 ? "+" : ""}${peAngle.toFixed(0)}° STEEP`;
          const isUp = isPeJump ? dPeJump > 0 : peAngle > 0;

          if (i - lastPeAnchorBar < minCooldownBars) {
            if (mag > lastPeMag) {
              if (mkPeList.length > 0) mkPeList.pop();
              lastPeMag = mag;
              mkPeList.push({
                time: p.t as UTCTimestamp,
                position: isUp ? "aboveBar" : "belowBar",
                shape: isUp ? "arrowUp" : "arrowDown",
                color: isUp ? "#10b981" : "#34d399",
                size: isPeJump && isPeSteep ? 1.2 : 1.0,
                text: txt,
              });
            }
          } else {
            lastPeAnchorBar = i;
            lastPeMag = mag;
            mkPeList.push({
              time: p.t as UTCTimestamp,
              position: isUp ? "aboveBar" : "belowBar",
              shape: isUp ? "arrowUp" : "arrowDown",
              color: isUp ? "#10b981" : "#34d399",
              size: isPeJump && isPeSteep ? 1.2 : 1.0,
              text: txt,
            });
          }
        } else if (showStepMarkers && i - lastStepPeBar >= minCooldownBars && i - lastPeAnchorBar >= minCooldownBars) {
          // Milestone 1.0 Cr crossings
          const bPut = Math.trunc(putCr);
          const prevBPut = Math.trunc(prevPutCr);
          if (bPut !== prevBPut && Math.abs(putCr) >= 1.0) {
            const traversed = lastMarkedPutCr === null || Math.abs(putCr - lastMarkedPutCr) >= 0.85 || (i - lastStepPeBar >= 10);
            const bucketChanged = lastMarkedPutBucket === null || bPut !== lastMarkedPutBucket;
            if (traversed && bucketChanged) {
              lastPeAnchorBar = i;
              lastStepPeBar = i;
              lastMarkedPutCr = putCr;
              lastMarkedPutBucket = bPut;
              mkPeList.push({
                time: p.t as UTCTimestamp,
                position: putCr >= prevPutCr ? "aboveBar" : "belowBar",
                shape: "square",
                color: "#10b981",
                size: 0.9,
                text: `🔔 ${putCr >= 0 ? "+" : ""}${bPut}Cr`,
              });
            }
          }
        }
      }

      mkCeList.sort((a, b) => (a.time as number) - (b.time as number));
      mkPeList.sort((a, b) => (a.time as number) - (b.time as number));

      if (markerMode === "all") {
        const topCe = mkCeList.length > 8 ? [...mkCeList].sort((a, b) => (b.size || 1) - (a.size || 1)).slice(0, 8).sort((a, b) => (a.time as number) - (b.time as number)) : mkCeList;
        const topPe = mkPeList.length > 8 ? [...mkPeList].sort((a, b) => (b.size || 1) - (a.size || 1)).slice(0, 8).sort((a, b) => (a.time as number) - (b.time as number)) : mkPeList;
        s.mkCe.setMarkers(topCe);
        s.mkPe.setMarkers(topPe);
      } else if (markerMode === "latest") {
        s.mkCe.setMarkers(mkCeList.slice(-2));
        s.mkPe.setMarkers(mkPeList.slice(-2));
      } else {
        s.mkCe.setMarkers(mkCeList);
        s.mkPe.setMarkers(mkPeList);
      }
    }

    // ────────────────── Real-Time Live Alert Engine ──────────────────
    const lastPt = P[P.length - 1];
    if (lastPt && lastPt.ce != null && lastPt.pe != null) {
      const lastCallCr = lastPt.ce / 1e7;
      const lastPutCr = lastPt.pe / 1e7;

      // Only fire acoustic beeps and live alerts on real updates
      if (lastEvaluatedTimeRef.current !== 0 && lastPt.t > lastEvaluatedTimeRef.current) {
        const callBucket = Math.trunc(lastCallCr);
        const putBucket = Math.trunc(lastPutCr);

        // A. 1Cr Milestone Crossing
        if (prevCallBucketRef.current !== null && callBucket !== prevCallBucketRef.current) {
          const isUp = callBucket > prevCallBucketRef.current;
          const msg = isUp
            ? `🔔 CALL OI +1Cr STEP: Crimson crossed ${lastCallCr.toFixed(2)}Cr`
            : `🔔 CALL OI -1Cr UNWIND: Crimson dropped to ${lastCallCr.toFixed(2)}Cr`;
          setActiveAlert({ text: msg, kind: "step1cr", ts: Date.now() });
          if (soundOn) playOiBeep("step1cr");
        } else if (prevPutBucketRef.current !== null && putBucket !== prevPutBucketRef.current) {
          const isUp = putBucket > prevPutBucketRef.current;
          const msg = isUp
            ? `🔔 PUT OI +1Cr STEP: Emerald crossed ${lastPutCr.toFixed(2)}Cr`
            : `🔔 PUT OI -1Cr UNWIND: Emerald dropped to ${lastPutCr.toFixed(2)}Cr`;
          setActiveAlert({ text: msg, kind: "step1cr", ts: Date.now() });
          if (soundOn) playOiBeep("step1cr");
        }

        // B. 40% Cell Jump (3-bar burst)
        if (P.length >= 4) {
          const p3 = P[P.length - 4];
          if (p3.ce != null && p3.pe != null) {
            const dCall3 = (lastPt.ce - p3.ce) / 1e7;
            const dPut3 = (lastPt.pe - p3.pe) / 1e7;
            if (Math.abs(dCall3) >= jumpThreshold) {
              const pct = (Math.abs(dCall3) / cellCr) * 100;
              const msg = `⚡ 40% CELL JUMP: Crimson (Call OI) jumped ${dCall3 > 0 ? "+" : ""}${dCall3.toFixed(2)}Cr (${pct.toFixed(0)}% of grid cell) in 3m!`;
              setActiveAlert({ text: msg, kind: "jump40", ts: Date.now() });
              if (soundOn) playOiBeep("jump40");
            } else if (Math.abs(dPut3) >= jumpThreshold) {
              const pct = (Math.abs(dPut3) / cellCr) * 100;
              const msg = `⚡ 40% CELL JUMP: Emerald (Put OI) jumped ${dPut3 > 0 ? "+" : ""}${dPut3.toFixed(2)}Cr (${pct.toFixed(0)}% of grid cell) in 3m!`;
              setActiveAlert({ text: msg, kind: "jump40", ts: Date.now() });
              if (soundOn) playOiBeep("jump40");
            }
          }
        }

        // C & D. Scissor Divergence & Steep Angle Formation (5-bar window)
        if (P.length >= 6) {
          const p5 = P[P.length - 6];
          if (p5.ce != null && p5.pe != null) {
            const dCall5 = (lastPt.ce - p5.ce) / 1e7;
            const dPut5 = (lastPt.pe - p5.pe) / 1e7;
            const callAngleDeg = Math.atan(dCall5 / cellCr) * (180 / Math.PI);
            const putAngleDeg = Math.atan(dPut5 / cellCr) * (180 / Math.PI);
            const futDiff = (lastPt.fut ?? 0) - (p5.fut ?? 0);

            // Scissor Divergence: opposite directions with spread >= 45°
            if (Math.abs(callAngleDeg - putAngleDeg) >= 45 && ((callAngleDeg > 15 && putAngleDeg < -15) || (callAngleDeg < -15 && putAngleDeg > 15))) {
              const isBullScissor = callAngleDeg < 0 && putAngleDeg > 0;
              const msg = isBullScissor
                ? `✂️ SCISSOR DIVERGENCE: 🟢 BULLISH EXPLOSION (BUY CE) | Crimson ${callAngleDeg.toFixed(0)}° ▼ / Emerald +${putAngleDeg.toFixed(0)}° ▲ | Fut ${futDiff >= 0 ? "▲ Confirmed" : "⚠️ Lagging"}`
                : `✂️ SCISSOR DIVERGENCE: 🔴 BEARISH EXPLOSION (BUY PE) | Crimson +${callAngleDeg.toFixed(0)}° ▲ / Emerald ${putAngleDeg.toFixed(0)}° ▼ | Fut ${futDiff <= 0 ? "▼ Confirmed" : "⚠️ Lagging"}`;
              setActiveAlert({ text: msg, kind: "scissor", ts: Date.now() });
              if (soundOn) playOiBeep("scissor");
            } else if (Math.abs(callAngleDeg) >= angleThreshold || Math.abs(putAngleDeg) >= angleThreshold) {
              if (callAngleDeg >= angleThreshold) {
                const msg = `🔴 CALL WRITERS STEEP SURGE (+${callAngleDeg.toFixed(0)}°) → BEARISH PRESSURE (BUY PE) | Fut ${futDiff <= 0 ? "▼ Confirmed" : "⚠️ Diverging"}`;
                setActiveAlert({ text: msg, kind: "angleBear", ts: Date.now() });
                if (soundOn) playOiBeep("angleBear");
              } else if (callAngleDeg <= -angleThreshold) {
                const msg = `🟢 CALL UNWINDING CLIFF (${callAngleDeg.toFixed(0)}°) → BULLISH LIFT (BUY CE) | Fut ${futDiff >= 0 ? "▲ Confirmed" : "⚠️ Diverging"}`;
                setActiveAlert({ text: msg, kind: "angleBull", ts: Date.now() });
                if (soundOn) playOiBeep("angleBull");
              } else if (putAngleDeg >= angleThreshold) {
                const msg = `🟢 PUT WRITERS STEEP SURGE (+${putAngleDeg.toFixed(0)}°) → BULLISH SUPPORT (BUY CE) | Fut ${futDiff >= 0 ? "▲ Confirmed" : "⚠️ Diverging"}`;
                setActiveAlert({ text: msg, kind: "angleBull", ts: Date.now() });
                if (soundOn) playOiBeep("angleBull");
              } else if (putAngleDeg <= -angleThreshold) {
                const msg = `🔴 PUT UNWINDING CLIFF (${putAngleDeg.toFixed(0)}°) → PANIC DUMP (BUY PE) | Fut ${futDiff <= 0 ? "▼ Confirmed" : "⚠️ Diverging"}`;
                setActiveAlert({ text: msg, kind: "angleBear", ts: Date.now() });
                if (soundOn) playOiBeep("angleBear");
              }
            }
          }
        }

        // E. Cumulative Gap Widening / Sustained Trend Divergence (15-30 bar window, 30-bar cooldown)
        const trendDiv = detectTrendDivergence(P, P.length - 1);
        if (trendDiv.isTrendDiv) {
          const curIdx = P.length - 1;
          const barsSinceLastAlert = curIdx - lastTrendDivAlertBarRef.current;
          if (barsSinceLastAlert >= 30) {
            lastTrendDivAlertBarRef.current = curIdx;
            const divDesc = trendDiv.isCallAccum
              ? "Progressive CALL Accumulation (Bearish Wall / Resistance)"
              : "Progressive PUT Accumulation (Bullish Floor / Support)";
            const msg = `🌊 TREND DIVERGENCE (Gap: ${trendDiv.oiGap.toFixed(1)}Cr) · Steady institutional widening (+${trendDiv.maxWidening.toFixed(1)}Cr over 15-30 bars) | ${divDesc}`;
            setActiveAlert({ text: msg, kind: "trendDivergence", ts: Date.now() });
            if (soundOn) playOiBeep("trendDivergence");
          }
        }
      }

      lastEvaluatedTimeRef.current = lastPt.t;
      prevCallBucketRef.current = Math.trunc(lastCallCr);
      prevPutBucketRef.current = Math.trunc(lastPutCr);
    }

    if (first.current || suiteView === "classic") {
      s.ch.timeScale().fitContent();
      first.current = false;
    }
  }, [d, angleThreshold, soundOn, markerMode, showSteepMarkers, showJumpMarkers, showStepMarkers, suiteView]);

  const P = d?.points ?? [];
  const last = P[P.length - 1];

  // Live angle & cell metrics for the HUD strip
  const allCr = P.flatMap((p) => [p.ce != null ? p.ce / 1e7 : null, p.pe != null ? p.pe / 1e7 : null]).filter((x): x is number => x != null);
  const maxCr = allCr.length ? Math.max(...allCr) : 5.0;
  const minCr = allCr.length ? Math.min(...allCr) : -5.0;
  const ySpan = Math.max(2.0, maxCr - minCr);

  const isExpiry = Boolean(
    d?.unwind ||
    (d?.expiry && d?.day && d.expiry === d.day) ||
    d?.label?.toLowerCase().includes("expiry")
  );
  const isHistoricalOrReplay = Boolean(
    d?.label?.toLowerCase().includes("saved") ||
    (P.length > 50)
  );
  const isExpiryOrReplay = isExpiry || isHistoricalOrReplay;
  const cellCr = isExpiryOrReplay ? 1.0 : (ySpan > 15 ? 2.5 : (ySpan > 6 ? 1.0 : 0.5));
  const effAngleThreshold = isExpiryOrReplay ? Math.min(angleThreshold, 25) : angleThreshold;

  const lastCallCr = (last?.ce ?? 0) / 1e7;
  const lastPutCr = (last?.pe ?? 0) / 1e7;
  const prev5 = P.length >= 6 ? P[P.length - 6] : (P[0] ?? last);
  const dCallNow = last && prev5 && last.ce != null && prev5.ce != null ? (last.ce - prev5.ce) / 1e7 : 0;
  const dPutNow = last && prev5 && last.pe != null && prev5.pe != null ? (last.pe - prev5.pe) / 1e7 : 0;
  const liveCallAngle = Math.atan(dCallNow / cellCr) * (180 / Math.PI);
  const livePutAngle = Math.atan(dPutNow / cellCr) * (180 / Math.PI);
  const isScissorNow = Math.abs(liveCallAngle - livePutAngle) >= 45 && ((liveCallAngle > 15 && livePutAngle < -15) || (liveCallAngle < -15 && livePutAngle > 15));
  const liveOiGap = Math.abs(lastCallCr - lastPutCr);
  const trendDivNow = detectTrendDivergence(P);

  const activePt = hoverPt ?? last;
  const flowVal = activePt?.flow ?? 0;
  const balVal = activePt?.bal ?? 0;
  let coachVerdict = "";
  if (flowVal > 0 && balVal > 0.25) {
    coachVerdict = `🟢 BULLISH DOMINANCE: 3m Flow (+${inr(flowVal)}) + 30m Balance (+${balVal.toFixed(2)}) aligned Bullish — Put writers actively building support floor (Buy Dips / Hold CE).`;
  } else if (flowVal < 0 && balVal < -0.25) {
    coachVerdict = `🔴 BEARISH DOMINANCE: 3m Flow (${inr(flowVal)}) + 30m Balance (${balVal.toFixed(2)}) aligned Bearish — Call writers actively hammering overhead resistance (Sell Rallies / Hold PE).`;
  } else if (flowVal > 0 && balVal < -0.25) {
    coachVerdict = `🟡 COUNTER-FLOW / COVERING: 3m Flow positive (+${inr(flowVal)}) against bearish 30m balance (${balVal.toFixed(2)}) — Watch for trapped Call writer short-covering bounce!`;
  } else if (flowVal < 0 && balVal > 0.25) {
    coachVerdict = `🟡 PROFIT-BOOKING / DIP: 3m Flow negative (${inr(flowVal)}) into bullish 30m balance (+${balVal.toFixed(2)}) — Normal pullback or dip test in uptrend.`;
  } else {
    coachVerdict = `⚪ NEUTRAL ROTATION: Two-way writer rotation (${inr(flowVal)}) with balance at ${balVal.toFixed(2)} — No aggressive institutional dominance in this candle.`;
  }

  const switcher = (
    <div className="flex rounded bg-[#0b1324] p-0.5 border border-slate-700">
      <button
        onClick={() => setViewMode("smart_oi")}
        className={`rounded px-2.5 py-0.5 font-extrabold text-[11px] transition-all ${
          suiteView === "smart_oi"
            ? "bg-gold-500/30 text-gold-200 border border-gold-500/60 shadow-sm"
            : "text-slate-400 hover:text-slate-200"
        }`}
        title="Smart OI noise-filtered confluence suite"
      >
        ⚡ Smart OI
      </button>
      <button
        onClick={() => setViewMode("trinity")}
        className={`rounded px-2.5 py-0.5 font-extrabold text-[11px] transition-all ${
          suiteView === "trinity"
            ? "bg-sky-500/30 text-sky-200 border border-sky-500/60 shadow-sm"
            : "text-slate-400 hover:text-slate-200"
        }`}
        title="3-Chart institutional PCR & positioning suite"
      >
        3-Chart Trinity Suite
      </button>
      <button
        onClick={() => setViewMode("classic")}
        className={`rounded px-2.5 py-0.5 font-bold text-[11px] transition-all ${
          suiteView === "classic"
            ? "bg-slate-700 text-slate-100 border border-slate-600 shadow-sm"
            : "text-slate-400 hover:text-slate-200"
        }`}
        title="Classic Option Writer Flow"
      >
        Classic Writer Flow
      </button>
      <button
        onClick={() => setViewMode("gap_sniper")}
        className={`rounded px-2.5 py-0.5 font-bold text-[11px] transition-all ${
          suiteView === "gap_sniper"
            ? "bg-emerald-600/30 text-emerald-200 border border-emerald-500/60 shadow-sm"
            : "text-slate-400 hover:text-slate-200"
        }`}
        title="Opening Gap & Confluence Sniper Playbook"
      >
        🎯 Gap Sniper
      </button>
    </div>
  );

  const dualSelectors = (
    <div className="flex flex-wrap items-center gap-2 text-xs">
      {/* Options Expiry Selector */}
      <div className="flex items-center rounded bg-[#0f172a] p-0.5 border border-slate-800">
        <span className="text-[10px] uppercase font-bold text-slate-500 px-1">Options:</span>
        {(["cur", "next", "both"] as const).map((e) => (
          <button
            key={e}
            onClick={() => handleOptExpiryChange(e)}
            className={`rounded px-2 py-0.5 text-[11px] font-bold transition-all ${
              optExpiry === e
                ? "bg-sky-500/30 text-sky-200 border border-sky-500/60 shadow-sm"
                : "text-slate-400 hover:text-slate-200"
            }`}
          >
            {e === "cur"
              ? `This Wk (${expMeta?.cur_date || "27 Oct"})`
              : e === "next"
              ? `Next Wk (${expMeta?.next_date || "03 Nov"})`
              : "Both"}
          </button>
        ))}
      </div>

      {/* Futures Contract Selector */}
      <div className="flex items-center rounded bg-[#0f172a] p-0.5 border border-slate-800">
        <span className="text-[10px] uppercase font-bold text-slate-500 px-1">Futures:</span>
        {(["near", "next", "combined"] as const).map((f) => (
          <button
            key={f}
            onClick={() => handleFutContractChange(f)}
            className={`rounded px-2 py-0.5 text-[11px] font-bold transition-all ${
              futContract === f
                ? "bg-indigo-500/30 text-indigo-200 border border-indigo-500/60 shadow-sm"
                : "text-slate-400 hover:text-slate-200"
            }`}
          >
            {f === "near"
              ? (expMeta?.near_fut ? expMeta.near_fut.replace("NIFTY-", "").replace("-FUT", "") + " FUT" : "Oct FUT")
              : f === "next"
              ? (expMeta?.next_fut ? expMeta.next_fut.replace("NIFTY-", "").replace("-FUT", "") + " FUT" : "Nov FUT")
              : `Oct+Nov (${expMeta?.rollover_pct != null ? "Roll " + expMeta.rollover_pct + "%" : "Combined"})`}
          </button>
        ))}
      </div>
    </div>
  );

  return (
    <div className="flex flex-1 min-h-0 flex-col h-full w-full relative" style={{ paddingLeft: '16px' }}>
      {/* ── View 0: Smart OI Intelligence Suite ── */}
      <div className={`flex-1 min-h-0 flex-col h-full w-full bg-[#070c17] ${suiteView === "smart_oi" ? "flex" : "hidden"}`}>
        <SmartOISuite
          tick={tick}
          modeToggle={switcher}
          optExpiry={optExpiry}
          setOptExpiry={handleOptExpiryChange}
          futContract={futContract}
          setFutContract={handleFutContractChange}
          expMeta={expMeta}
          onMetaUpdate={setExpMeta}
        />
      </div>

      {/* ── View 1: 3-Chart Trinity Suite ── */}
      <div className={`flex-1 min-h-0 flex-col h-full w-full bg-[#070c17] box-border overflow-x-hidden ${suiteView === "trinity" ? "flex" : "hidden"}`}>
        <div className="flex items-center justify-between border-b border-ink-800 bg-[#091122] px-4 py-1 text-[11px] shrink-0 box-border w-full" style={{ paddingLeft: '16px' }}>
          <div className="flex items-center gap-2 pl-2">
            <span className="font-extrabold text-sky-400">⚡ 3-Chart Institutional PCR & OI Trinity Suite</span>
            <span className="text-slate-500 hidden sm:inline">• Live PCR • Fresh OI Δ • Cumulative Total OI</span>
          </div>
          {switcher}
        </div>
        {/* 🎯 LIVE DESK BIAS: Institutional Flash-Card Banner */}
        <DeskBiasBanner optExpiry={optExpiry} futContract={futContract} tick={tick} />
        <div className="flex-1 min-h-0 w-full relative box-border overflow-x-hidden px-2">
          <PcrTrinitySuite
            tick={tick}
            optExpiry={optExpiry}
            setOptExpiry={handleOptExpiryChange}
            futContract={futContract}
            setFutContract={handleFutContractChange}
            expMeta={expMeta}
            onMetaUpdate={setExpMeta}
          />
        </div>
      </div>

      {/* ── View 2: Classic Option Writer Flow ── */}
      <div className={`flex-1 min-h-0 flex-col h-full w-full ${suiteView === "classic" ? "flex" : "hidden"}`}>
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-ink-800 bg-[#091122] px-3 py-1 text-[11px]">
        <div className="flex flex-wrap items-center gap-3">
          <span className="font-bold text-slate-400">Classic Option Writer Flow</span>
          {dualSelectors}
        </div>
        {switcher}
      </div>
      {/* 🎯 LIVE DESK BIAS: Institutional Flash-Card Banner */}
      <DeskBiasBanner optExpiry={optExpiry} futContract={futContract} tick={tick} />
      <Controls tf={tf} setTf={setTf} mode={mode} setMode={setMode} n={n} setN={setN} tfs={["1m", "3m", "5m", "15m"]} busy={busy}
        info={d?.ok ? `${d.day} · expiry ${optExpiry === "both" ? "Both (" + (expMeta?.cur_date || "27 Oct") + " + " + (expMeta?.next_date || "03 Nov") + ")" : (optExpiry === "next" ? (expMeta?.next_date || "03 Nov") : (expMeta?.cur_date || "27 Oct"))} · NIFTY future ${futContract === "near" ? (expMeta?.near_fut || "Oct FUT") : (futContract === "next" ? (expMeta?.next_fut || "Nov FUT") : "Oct+Nov Combined")} · ${d.snapshots} snapshots` : d?.message ?? "loading…"} />

      {/* 📡 Live OI Velocity, Angle & Milestone Telemetry Strip */}
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-ink-800 bg-[#070c18] px-3 py-1.5 text-[11px]">
        <div className="flex flex-wrap items-center gap-2">
          {/* Green Call Writers Angle Badge */}
          <span
            className={`flex items-center gap-1 rounded px-2 py-0.5 font-bold border transition-colors ${
              Math.abs(liveCallAngle) >= angleThreshold
                ? "bg-emerald-950 text-emerald-200 border-emerald-500/80 ring-1 ring-emerald-500/30 animate-pulse"
                : "bg-emerald-950/40 text-emerald-300 border-emerald-800/50"
            }`}
            title={`Call OI (Green line) 5m slope: ${liveCallAngle.toFixed(1)}°. ${Math.abs(liveCallAngle) >= angleThreshold ? (liveCallAngle > 0 ? "Call writing surge (Bearish resistance)!" : "Call unwinding cliff (Bullish squeeze)!") : "Normal drift."}`}
          >
            <span className="text-emerald-400">🟢 Green (Call Writers):</span>
            <span className="font-mono text-emerald-200">
              {liveCallAngle >= 0 ? "+" : ""}{liveCallAngle.toFixed(0)}° {liveCallAngle >= 0 ? "▲" : "▼"}
            </span>
            {Math.abs(liveCallAngle) >= angleThreshold && (
              <span className="text-[10px] uppercase tracking-wide font-extrabold text-emerald-300">
                {liveCallAngle > 0 ? "(Steep Surge!)" : "(Steep Drop!)"}
              </span>
            )}
          </span>

          {/* Red Put Writers Angle Badge */}
          <span
            className={`flex items-center gap-1 rounded px-2 py-0.5 font-bold border transition-colors ${
              Math.abs(livePutAngle) >= angleThreshold
                ? "bg-rose-950 text-rose-200 border-rose-500/80 ring-1 ring-rose-500/30 animate-pulse"
                : "bg-rose-950/40 text-rose-300 border-rose-800/50"
            }`}
            title={`Put OI (Red line) 5m slope: ${livePutAngle.toFixed(1)}°. ${Math.abs(livePutAngle) >= angleThreshold ? (livePutAngle > 0 ? "Put writing surge (Bullish floor)!" : "Put unwinding cliff (Panic liquidation)!") : "Normal drift."}`}
          >
            <span className="text-rose-400">🔴 Red (Put Writers):</span>
            <span className="font-mono text-rose-200">
              {livePutAngle >= 0 ? "+" : ""}{livePutAngle.toFixed(0)}° {livePutAngle >= 0 ? "▲" : "▼"}
            </span>
            {Math.abs(livePutAngle) >= angleThreshold && (
              <span className="text-[10px] uppercase tracking-wide font-extrabold text-rose-300">
                {livePutAngle > 0 ? "(Steep Surge!)" : "(Steep Drop!)"}
              </span>
            )}
          </span>

          {/* Scissor Status */}
          {isScissorNow && (
            <span className="flex items-center gap-1 rounded px-2 py-0.5 font-extrabold text-[10.5px] bg-amber-950/90 text-amber-300 border border-amber-500/70 animate-bounce">
              <span>✂️ SCISSOR DIVERGENCE ACTIVE</span>
            </span>
          )}

          {/* Progressive Accumulation / Trend Divergence Badge */}
          {trendDivNow.isTrendDiv && (
            <span
              className={`flex items-center gap-1 rounded px-2 py-0.5 font-extrabold text-[10.5px] border animate-pulse ${
                trendDivNow.isCallAccum
                  ? "bg-rose-950/90 text-rose-300 border-rose-500/70"
                  : "bg-emerald-950/90 text-emerald-300 border-emerald-500/70"
              }`}
              title={`Institutional Trend Divergence: Active gap ${liveOiGap.toFixed(1)}Cr steadily widened +${trendDivNow.maxWidening.toFixed(1)}Cr over 15-30 bars (${trendDivNow.isCallAccum ? "Bearish Call accumulation" : "Bullish Put accumulation"})`}
            >
              <span>🌊 TREND DIVERGENCE (Gap: {liveOiGap.toFixed(1)}Cr)</span>
            </span>
          )}

          {/* Cell & Jump Status */}
          <span className="hidden sm:inline-flex items-center gap-1 px-2 py-0.5 rounded bg-ink-900 border border-ink-800 text-slate-300">
            <span>Grid Cell: <b className="text-cyan-300">{cellCr.toFixed(1)}Cr</b></span>
            <span className="text-slate-500">·</span>
            <span>{isExpiryOrReplay ? "30%" : "40%"} Jump: <b className="text-amber-300">{(cellCr * (isExpiryOrReplay ? 0.30 : 0.40)).toFixed(2)}Cr</b></span>
          </span>

          {/* 1Cr Milestone Counter */}
          <span className="hidden md:inline-flex items-center gap-1 px-2 py-0.5 rounded bg-ink-900 border border-ink-800 text-slate-300">
            <span>1Cr Cross:</span>
            <span>Call <b className="text-emerald-400">{lastCallCr >= 0 ? "+" : ""}{lastCallCr.toFixed(1)}Cr</b></span>
            <span className="text-slate-500">|</span>
            <span>Put <b className="text-rose-400">{lastPutCr >= 0 ? "+" : ""}{lastPutCr.toFixed(1)}Cr</b></span>
          </span>

          {/* Active OI Gap Counter */}
          <span
            className="hidden lg:inline-flex items-center gap-1 px-2 py-0.5 rounded bg-ink-900 border border-ink-800 text-slate-300"
            title={`Active OI Gap: |Call (${lastCallCr.toFixed(2)}Cr) − Put (${lastPutCr.toFixed(2)}Cr)| = ${liveOiGap.toFixed(2)}Cr`}
          >
            <span>OI Gap:</span>
            <b className={liveOiGap >= 1.5 ? "text-cyan-300" : "text-slate-300"}>{liveOiGap.toFixed(1)}Cr</b>
          </span>
        </div>

        {/* Interactive Controls */}
        <div className="flex flex-wrap items-center gap-2">
          {/* Main Marker Mode Toggle */}
          <button
            onClick={cycleMarkerMode}
            className={`flex items-center gap-1 rounded px-2 py-0.5 text-[10.5px] font-bold border transition-colors ${
              markerMode === "off"
                ? "bg-ink-900 text-slate-300 border-ink-700 hover:border-slate-500"
                : markerMode === "latest"
                ? "bg-cyan-950/80 text-cyan-300 border-cyan-500/70 hover:bg-cyan-900/80"
                : "bg-indigo-950/80 text-indigo-300 border-indigo-500/70 hover:bg-indigo-900/80"
            }`}
            title="Cycle Marker Mode: All History (De-cluttered) → Latest Only (Last 2) → OFF (Clean)"
          >
            <span>
              {markerMode === "off"
                ? "📍 Markers: OFF (Clean)"
                : markerMode === "latest"
                ? "🎯 Markers: Latest Only"
                : "🔬 Markers: All (De-cluttered)"}
            </span>
          </button>

          {/* Sub-Toggles when Markers are NOT OFF */}
          {markerMode !== "off" && (
            <div className="flex items-center gap-1 rounded bg-ink-900 border border-ink-800 p-0.5">
              <button
                onClick={toggleSteepMarkers}
                className={`px-1.5 py-0.5 rounded text-[10px] font-bold transition-colors ${
                  showSteepMarkers
                    ? "bg-emerald-500/25 text-emerald-300 border border-emerald-500/50"
                    : "text-slate-500 hover:text-slate-300"
                }`}
                title="Toggle 📐 Steep Angle Markers on Chart"
              >
                📐 Steep
              </button>
              <button
                onClick={toggleJumpMarkers}
                className={`px-1.5 py-0.5 rounded text-[10px] font-bold transition-colors ${
                  showJumpMarkers
                    ? "bg-amber-500/25 text-amber-300 border border-amber-500/50"
                    : "text-slate-500 hover:text-slate-300"
                }`}
                title="Toggle ⚡ 40% Jump Markers on Chart"
              >
                ⚡ 40% Jump
              </button>
              <button
                onClick={toggleStepMarkers}
                className={`px-1.5 py-0.5 rounded text-[10px] font-bold transition-colors ${
                  showStepMarkers
                    ? "bg-blue-500/25 text-blue-300 border border-blue-500/50"
                    : "text-slate-500 hover:text-slate-300"
                }`}
                title="Toggle 🔔 1Cr Step Milestone Markers on Chart"
              >
                🔔 1Cr Step
              </button>
            </div>
          )}

          {/* Angle Threshold Selector */}
          <div className="flex items-center gap-0.5 rounded bg-ink-900 border border-ink-800 p-0.5">
            <span className="text-[10px] text-slate-400 px-1">Angle:</span>
            {[25, 30, 45].map((ang) => (
              <button
                key={ang}
                onClick={() => toggleAngleThreshold(ang)}
                className={`px-1.5 py-0.5 rounded text-[10px] font-bold transition-colors ${
                  angleThreshold === ang
                    ? "bg-gold-500/30 text-gold-300 border border-gold-500/60"
                    : "text-slate-400 hover:text-slate-200"
                }`}
              >
                ≥{ang}°
              </button>
            ))}
          </div>

          {/* Audio Beep Toggle */}
          <button
            onClick={toggleSound}
            className={`flex items-center gap-1 rounded px-2 py-0.5 text-[10.5px] font-bold border transition-colors ${
              soundOn
                ? "bg-emerald-950/80 text-emerald-300 border-emerald-500/60 hover:bg-emerald-900/80"
                : "bg-ink-900 text-slate-400 border-ink-700 hover:text-slate-200"
            }`}
            title="Toggle Web Audio real-time acoustic alerts (1Cr Step, 40% Jump, Steep Angle, Scissor, Trend Divergence)"
          >
            <span>{soundOn ? "🔊 Audio: ON" : "🔇 Audio: OFF"}</span>
          </button>

          {/* Test Beep Button */}
          <button
            onClick={handleTestBeep}
            className="flex items-center gap-1 rounded px-2 py-0.5 text-[10.5px] font-bold border border-gold-500/50 bg-gold-500/15 text-gold-300 hover:bg-gold-500/25 transition-colors"
            title="Test audio alert synthesis immediately"
          >
            <span>🔔 Test Beep</span>
          </button>
        </div>
      </div>

      {/* 🚨 Active Audio & Visual OI Alert Banner */}
      {activeAlert && Date.now() - activeAlert.ts < 12000 && (
        <div className="flex items-center justify-between gap-2 px-3 py-1.5 bg-gradient-to-r from-ink-950 via-gold-950/50 to-ink-950 border-b border-gold-500/60 text-[12px] shadow-lg animate-pulse">
          <div className="flex items-center gap-2 font-bold text-gold-300">
            <span className="text-[14px]">
              {activeAlert.kind === "scissor"
                ? "✂️"
                : activeAlert.kind === "jump40"
                ? "⚡"
                : activeAlert.kind === "step1cr"
                ? "🔔"
                : activeAlert.kind === "trendDivergence"
                ? "🌊"
                : activeAlert.kind === "angleBull"
                ? "🟢"
                : "🔴"}
            </span>
            <span>{activeAlert.text}</span>
          </div>
          <button
            onClick={() => setActiveAlert(null)}
            className="text-slate-400 hover:text-white text-[11px] font-bold px-1.5 py-0.5 rounded bg-ink-800 border border-ink-700"
            title="Dismiss Alert"
          >
            ✕
          </button>
        </div>
      )}

      <div className="flex flex-wrap items-center gap-x-4 gap-y-1 px-3 py-1 text-[12.5px] text-slate-300">
        <button onClick={() => setShowFut((v) => !v)}
          className={`rounded border px-2 py-0.5 text-[11px] ${showFut ? "border-emerald-500/60 bg-emerald-500/15 text-emerald-200" : "border-ink-700 text-slate-400"}`}>
          {showFut ? "📈 Future candles" : "┈ Future line"}
        </button>
        <span><span className="text-emerald-400 font-semibold">━ Call OI Change (Call Writers, Green)</span> · <span className="text-rose-400 font-semibold">━ Put OI Change (Put Writers, Red)</span> (since yesterday, right axis) · {showFut ? "real NIFTY-future candles" : "┈ NIFTY future line (Dotted Gray)"} (left axis, adapts to active expiry)</span>
        <span>middle strip = <b className="text-slate-200">flow Δput − Δcall</b>: <span className="text-emerald-400 font-medium">green ▲ = PUT writers added more (support, bullish)</span> · <span className="text-rose-400 font-medium">red ▼ = CALL writers added more (resistance, bearish)</span> · bright bar + label = unusual (≥ 2.5× normal) · bottom gold line = 30-min balance: above +0.25 bullish, below −0.25 bearish</span>
        {last && <span className="text-slate-300">now: call {inr(last.ce)} · put {inr(last.pe)} · balance {last.bal?.toFixed(2)} · regime <b>{last.conf ?? last.regime}</b></span>}
      </div>
      {/* 🧭 Interactive Plain-English Hover Coach Banner Across All 3 Synced Strips */}
      {activePt && (
        <div className="mx-3 my-1 rounded bg-[#091122]/95 border border-cyan-500/50 p-2 shadow-xl backdrop-blur text-[11px] shrink-0">
          <div className="flex flex-wrap items-center gap-x-3 gap-y-1 font-mono text-slate-300">
            <span className="font-bold text-white pr-2 border-r border-slate-700">
              {hoverPt ? `🕒 ${activePt.hm || formatIST(activePt.t)}` : `⚡ LIVE ${activePt.hm || formatIST(activePt.t)}`}
            </span>
            {activePt.fut != null && (
              <span>⚪ Future: <b className="text-white">{activePt.fut.toFixed(1)}</b></span>
            )}
            <span className="text-[#22c55e]">
              🟢 Call OI Chg: <b>{inr(activePt.ce, true)}</b>
            </span>
            <span className="text-[#ef4444]">
              🔴 Put OI Chg: <b>{inr(activePt.pe, true)}</b>
            </span>
            <span className="text-purple-300">
              Net (PE−CE): <b>{inr((activePt.pe ?? 0) - (activePt.ce ?? 0), true)}</b>
            </span>
            <span className="text-slate-600">|</span>
            <span className={flowVal >= 0 ? "text-[#22c55e]" : "text-[#ef4444]"}>
              ⚡ 3m Flow (ΔPut−ΔCall): <b>{inr(flowVal, true)}</b> ({flowVal >= 0 ? "🟢 Put led bar" : "🔴 Call led bar"})
            </span>
            <span className="text-slate-600">|</span>
            <span className={balVal > 0.25 ? "text-emerald-400" : balVal < -0.25 ? "text-rose-400" : "text-amber-300"}>
              ⚖️ 30m Balance: <b>{balVal >= 0 ? "+" : ""}{balVal.toFixed(2)}</b> ({balVal > 0.25 ? "🟢 Bullish (> +0.25)" : balVal < -0.25 ? "🔴 Bearish (< -0.25)" : "⚪ Neutral band"})
            </span>
          </div>
          <div className="mt-1 font-medium text-[11px] text-cyan-300">
            {coachVerdict}
          </div>
        </div>
      )}

      <div ref={box} className="min-h-0 flex-1 w-full" style={{ minHeight: "220px" }} />
      {(d?.read || d?.note) && <div className="border-t border-ink-700 px-3 py-1.5 text-[13px] text-slate-200">{clean(`${d?.read ?? ""} ${d?.note ?? ""}`)}</div>}
      </div>

      {/* ── View 3: Gap & Confluence Sniper ── */}
      <div className={`flex-1 min-h-0 flex-col h-full w-full bg-[#070c17] ${suiteView === "gap_sniper" ? "flex" : "hidden"}`}>
        <GapSniperTab
          tick={tick}
          modeToggle={switcher}
          optExpiry={optExpiry}
          futContract={futContract}
        />
      </div>
    </div>
  );
}

export const WritersChart = WritersFlow;
export { default as PcrTrinitySuite } from "./PcrTrinitySuite";
export type { PcrPoint, PcrCheckpoint, PcrTrinityData } from "./PcrTrinitySuite";
export { default as PeceMatrix } from "./PeceMatrix";
export type { MatrixRow, PeceMatrixPayload } from "./PeceMatrix";
export { default as OIMatrixSuite } from "./OIMatrixSuite";
export { default as StrikeMap2_0 } from "./StrikeMap2.0";
export type { StrikeData, OIMatrixPayload, OIMatrixSuiteProps } from "./OIMatrixSuite";
export { default as SmartOISuite } from "./SmartOISuite";
export type { SmartOIStrike, SmartOICandle, SmartOITimeSeriesPoint, SmartOIPayload } from "./SmartOISuite";
export { GapSniperTab } from "./GapSniperTab";
export type { GapConfluenceData, GapChecklistItem, GapTimelineItem, GapPlaybook } from "./GapSniperTab";


// ─────────────────────────────── a card with its own Full view / Normal view ───────────────────────────────
export function FullPanel({ title, hint, h, children }: { title: string; hint?: string; h: number; children: ReactNode }) {
  const ref = useRef<HTMLDivElement>(null);
  const [full, setFull] = useState(false);
  const toggle = useCallback(() => {
    if (document.fullscreenElement) { document.exitFullscreen().catch(() => {}); return; }
    if (full) { setFull(false); return; }
    const el = ref.current;
    if (el?.requestFullscreen) el.requestFullscreen().then(() => setFull(true)).catch(() => setFull(true)); else setFull(true);
  }, [full]);
  useEffect(() => {
    const fs = () => { if (!document.fullscreenElement) setFull(false); };
    const k = (e: KeyboardEvent) => { if (e.key === "Escape" && !document.fullscreenElement) setFull(false); };
    document.addEventListener("fullscreenchange", fs); window.addEventListener("keydown", k);
    return () => { document.removeEventListener("fullscreenchange", fs); window.removeEventListener("keydown", k); };
  }, []);
  return (
    <div ref={ref} className={full ? "card fixed inset-0 z-50 flex flex-col rounded-none !bg-[#0b1220]" : "card flex flex-col min-w-0"}
      style={full ? undefined : { height: h }}>
      <div className="card-h">
        <span className="card-t">{title}</span>
        {hint && <span className="text-[10px] text-slate-500">{hint}</span>}
        <button onClick={toggle} className="tab ml-auto !border-gold-500/70 !text-gold-400 font-semibold">{full ? "▣ Normal view" : "⛶ Full view"}</button>
      </div>
      <div className="min-h-0 flex-1">{children}</div>
    </div>
  );
}

export type OcBeat = { time: string; tone: "bull" | "bear" | "wait" | "info" | "trade"; title: string; meaning: string; action: string };
export type OcSummary = { ok: boolean; lines: string[]; beats: OcBeat[]; message?: string };

export function useOcSummary(tick: number): OcSummary | null {
  const [d, setD] = useState<OcSummary | null>(null);
  useEffect(() => {
    let dead = false;
    const go = () => fetch("/api/oclab-summary").then((r) => r.json()).then((j) => { if (!dead) setD(j); }).catch(() => {});
    go();
    const id = window.setInterval(go, 60000);
    return () => { dead = true; window.clearInterval(id); };
  }, [tick]);
  return d;
}


// ─────────────────────────── drag bar: pull up / down to resize the block above it ───────────────────────────
export function useStoredHeight(key: string, def: number): [number, (v: number) => void] {
  const [h, setH] = useState<number>(() => { try { const v = Number(localStorage.getItem(key)); return v > 0 ? v : def; } catch { return def; } });
  const set = useCallback((v: number) => { setH(v); try { localStorage.setItem(key, String(Math.round(v))); } catch { /* ignore */ } }, [key]);
  return [h, set];
}

export function DragBar({ h, setH, min = 260, max = 1600, label }: { h: number; setH: (v: number) => void; min?: number; max?: number; label: string }) {
  const start = useRef<{ y: number; h: number } | null>(null);
  const onDown = (e: React.PointerEvent<HTMLDivElement>) => {
    start.current = { y: e.clientY, h };
    (e.target as HTMLElement).setPointerCapture(e.pointerId);
    document.body.style.cursor = "row-resize"; document.body.style.userSelect = "none";
  };
  const onMove = (e: React.PointerEvent<HTMLDivElement>) => {
    if (!start.current) return;
    setH(Math.max(min, Math.min(max, start.current.h + e.clientY - start.current.y)));
  };
  const onUp = () => { start.current = null; document.body.style.cursor = ""; document.body.style.userSelect = ""; };
  return (
    <div onPointerDown={onDown} onPointerMove={onMove} onPointerUp={onUp} onDoubleClick={() => setH(-1)}
      title={`Drag up / down to resize the ${label} · double-click to reset`}
      className="group -my-1.5 flex h-3 cursor-row-resize items-center justify-center select-none">
      <div className="h-1 w-24 rounded-full bg-ink-600 transition group-hover:w-40 group-hover:bg-gold-500" />
    </div>
  );
}
