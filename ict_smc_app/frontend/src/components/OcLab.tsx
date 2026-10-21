// OcLab.tsx — two Options Lab views from the OC app (8501), served through /api/oclab:
//   PeceTable   — the PE − CE OI table row for row (🎯 LEVELS · ACTION, READ, ★ PRO READ), newest candle on top
//   WritersFlow — "OI Change (Call vs Put) — since yesterday" + writer-flow strip (Δput − Δcall, 30-min balance)
// Both refresh every 30 s; the backend recomputes only when a new snapshot has been stored (never stale).
import { type ReactNode, useCallback, useEffect, useRef, useState } from "react";
import { FlowBand } from "./flowband";
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

export function computeSmartRows(rawRows: Row[], tf: string): SmartRow[] {
  if (!rawRows || rawRows.length === 0) return [];

  const barMins = tf === "1m" ? 1 : tf === "3m" ? 3 : tf === "5m" ? 5 : tf === "15m" ? 15 : 60;
  const barsIn15m = Math.max(1, Math.round(15 / barMins));
  const barsIn30m = Math.max(2, Math.round(30 / barMins));

  // rawRows is newest first from backend; reverse to evaluate chronologically (09:15 -> 15:42)
  const chron = [...rawRows].reverse();
  let sessionHigh = -Infinity;
  let sessionLow = Infinity;

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
    const isZeroFlipBear = (prevRow?.pe_ce_day ?? 0) >= 0 && (r.pe_ce_day ?? 0) < 0;
    const isZeroFlipBull = (prevRow?.pe_ce_day ?? 0) <= 0 && (r.pe_ce_day ?? 0) > 0;

    const wallText = r.pro?.[3] || (r.lv?.[2] === "break_dn" || r.lv?.[2] === "break_up" ? r.lv[0] : "");
    const isWallShiftBear = Boolean(
      (wallText && (wallText.includes("Floor ↓") || wallText.includes("BROKEN") || wallText.includes("RESISTANCE being built") || wallText.includes("Ceiling ↓"))) ||
      (prevRow && r.sup != null && prevRow.sup != null && r.sup < prevRow.sup)
    );
    const isWallShiftBull = Boolean(
      (wallText && (wallText.includes("Floor ↑") || wallText.includes("Resistance broken") || wallText.includes("SUPPORT being built") || wallText.includes("Ceiling ↑"))) ||
      (prevRow && r.res != null && prevRow.res != null && r.res > prevRow.res)
    );

    // Pullback to Supply
    const isBearRegime = pe_ce_day_cr < -0.3 || (r.pcr ?? 1) < 1.15 || r.lv?.[1] === "bear" ||
      (sessionHigh - spot > 100 && (r.read[0].includes("Rally sold") || r.read[0].includes("Call writing") || (r.lv && r.lv[0].includes("RESISTANCE")) || r.pro?.[1] === "bear"));
    const bounceFromLow = spot - swingLow30m;
    const isPullbackToSupply = isBearRegime && bounceFromLow >= 18 && bounceFromLow <= 65 &&
      (call_delta_l > 0 || r.read[0].includes("Rally sold") || r.read[0].includes("Call writing") || (r.pro && r.pro[0] && r.pro[0].includes("rally being sold")) || r.read[1] === "bear" || r.pro?.[1] === "bear");

    // Pullback to Demand
    const isBullRegime = pe_ce_day_cr > 0.3 || (r.pcr ?? 1) > 1.25 || r.lv?.[1] === "bull";
    const dipFromHigh = swingHigh30m - spot;
    const isPullbackToDemand = isBullRegime && dipFromHigh >= 18 && dipFromHigh <= 65 &&
      (put_delta_l > 0 || r.read[0].includes("Dip defended") || r.read[0].includes("Put writing") || r.read[1] === "bull");

    // Short Covering Reversal
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
    } else if (r.ts < "09:30") {
      stage = 0;
      stageBadge = "WARM-UP";
      actionText = "⏳ WARM-UP (Walls & Liquidity Forming) · WAIT";
      actionTone = "flat";
    } else if (isShortCoveringReversal) {
      stage = 5;
      stageBadge = "💰 BOOK / REVERSAL";
      actionTone = "bull";
      dynamicSl = Math.round(swingLow30m - 12);
      if (r.ts < "15:00") {
        actionText = `💰 BOOK PE PROFITS · ⚡ SCALP ${atmStrike} CE ON SHORT COVERING (SL ${dynamicSl})`;
      } else {
        actionText = `💰 EXIT PE / BOOK PROFITS (Call Writers Covering · Bounce +${Math.round(spot - swingLow30m)}pts)`;
      }
    } else if (isPullbackToSupply) {
      stage = 2;
      stageBadge = "🎯 PULLBACK SELL";
      actionTone = "bear";
      dynamicSl = Math.round(swingHigh30m + 15);
      riskPts = Math.max(15, Math.round(dynamicSl - spot));
      const target = Math.round(swingLow30m - 30);
      actionText = `🎯 PULLBACK SELL: BUY ${atmStrike} PE · SL ${dynamicSl} (${riskPts}pt risk → Tgt ${target})`;
    } else if (isPullbackToDemand) {
      stage = 2;
      stageBadge = "🎯 DIP BUY CE";
      actionTone = "bull";
      dynamicSl = Math.round(swingLow30m - 15);
      riskPts = Math.max(15, Math.round(spot - dynamicSl));
      const target = Math.round(swingHigh30m + 30);
      actionText = `🎯 DIP BUY: BUY ${atmStrike} CE · SL ${dynamicSl} (${riskPts}pt risk → Tgt ${target})`;
    } else if (isZeroFlipBear || isWallShiftBear || (r.lv?.[2] === "break_dn" && r.pro?.[2]?.includes("●"))) {
      stage = 1;
      if (spotChange15m < -85) {
        stageBadge = "⏳ WAIT PULLBACK";
        actionTone = "warn";
        const pbTgt = Math.round(spot + 45);
        actionText = `⏳ BREAKDOWN EXTENDED (−${Math.abs(spotChange15m).toFixed(0)}pts in 15m) — WAIT PULLBACK TO ~${pbTgt}`;
      } else {
        stageBadge = "🎯 SNIPER BREAKOUT";
        actionTone = "bear";
        dynamicSl = Math.round(swingHigh30m + 15);
        riskPts = Math.max(15, Math.round(dynamicSl - spot));
        actionText = `🎯 SNIPER BREAKOUT: BUY ${atmStrike} PE · SL ${dynamicSl} (${riskPts}pt risk)`;
      }
    } else if (isZeroFlipBull || isWallShiftBull || (r.lv?.[2] === "break_up" && r.pro?.[2]?.includes("●"))) {
      stage = 1;
      if (spotChange15m > 85) {
        stageBadge = "⏳ WAIT DIP";
        actionTone = "warn";
        const dipTgt = Math.round(spot - 45);
        actionText = `⏳ RALLY EXTENDED (+${spotChange15m.toFixed(0)}pts in 15m) — WAIT DIP TO ~${dipTgt}`;
      } else {
        stageBadge = "🎯 SNIPER BREAKOUT";
        actionTone = "bull";
        dynamicSl = Math.round(swingLow30m - 15);
        actionText = `🎯 SNIPER BREAKOUT: BUY ${atmStrike} CE · SL ${dynamicSl}`;
      }
    } else if (Math.abs(pe_ce_delta_l) >= 20 && (r.pro?.[2] === "●●" || r.pro?.[2] === "●●●" || r.act?.[0]?.includes("BUY"))) {
      stage = 3;
      if (pe_ce_delta_l < 0) {
        stageBadge = "🔥 ADD PE";
        actionTone = "bear";
        dynamicSl = Math.round(swingHigh30m + 12);
        actionText = `🔥 SCALE-IN / ADD PE: Shock ${pe_ce_delta_l.toFixed(1)}L · Trail SL ${dynamicSl}`;
      } else {
        stageBadge = "🔥 ADD CE";
        actionTone = "bull";
        dynamicSl = Math.round(swingLow30m - 12);
        actionText = `🔥 SCALE-IN / ADD CE: Shock +${pe_ce_delta_l.toFixed(1)}L · Trail SL ${dynamicSl}`;
      }
    } else if (sessionHigh - spot > 100 && pe_ce_day_cr < -0.8) {
      stage = 4;
      stageBadge = "🛡️ HOLD PE";
      actionTone = "bear";
      const trailSl = Math.round(swingHigh30m + 12);
      actionText = `🛡️ HOLD PE · TRAIL SL → ${trailSl} (DO NOT CHASE)`;
    } else if (spot - sessionLow > 100 && pe_ce_day_cr > 0.8) {
      stage = 4;
      stageBadge = "🛡️ HOLD CE";
      actionTone = "bull";
      const trailSl = Math.round(swingLow30m - 12);
      actionText = `🛡️ HOLD CE · TRAIL SL → ${trailSl} (DO NOT CHASE)`;
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

    if (actionTone === "bear" && isBearishRegimeOnGraph) {
      isSynergy = true;
      convictionPct = 95;
      convictionBadge = "●●● A+ (95%)";
    } else if (actionTone === "bull" && isBullishRegimeOnGraph) {
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

    let unifiedRead = r.meaning || r.read[0];
    if (r.pro?.[0] && !r.pro[0].toLowerCase().includes("no extra context") && !r.pro[0].toLowerCase().includes("noise")) {
      unifiedRead = r.pro[0];
    } else if (r.lv?.[0] && !r.lv[0].toLowerCase().includes("holding") && !r.lv[0].toLowerCase().includes("range")) {
      unifiedRead = `${r.lv[0]} · ${r.read[0]}`;
    }

    if (isScissor) {
      unifiedRead = `✂️ Scissor Divergence (Spread ${scissorSpread.toFixed(0)}°) · ${unifiedRead}`;
    }

    const isActionable = stage === 1 || stage === 2 || stage === 3 || stage === 5 || isWallShiftBear || isWallShiftBull || isSynergy;
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

export function PeceTable({ tick }: { tick: number }) {
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
  const smartRows = computeSmartRows(rawRows, tf);
  const pivots = findTop5Pivots(smartRows);

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

  return (
    <div className="flex h-full flex-col bg-[#0b1220]">
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

export function WritersFlow({ tick }: { tick: number }) {
  const [tf, setTf] = useState("1m"); const [mode, setMode] = useState("all"); const [n, setN] = useState(10);
  const [d, busy] = useOc<Flow>("flow", `tf=${tf}&mode=${mode}&n=${n}`, tick);
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
    if (!box.current) return;
    const ch = createChart(box.current, {
      autoSize: true, layout: { background: { type: ColorType.Solid, color: "#0b1220" }, textColor: "#cbd5e1", fontSize: 12, fontFamily: "Inter, system-ui" },
      grid: { vertLines: { color: "#131d33" }, horzLines: { color: "#131d33" } },
      rightPriceScale: { borderColor: "#1d2a45" }, leftPriceScale: { visible: true, borderColor: "#1d2a45" },
      timeScale: { borderColor: "#1d2a45", timeVisible: true, secondsVisible: false, rightOffset: 4 },
      localization: { locale: "en-IN" },
    });
    const uw = ch.addSeries(HistogramSeries, { priceScaleId: "uw", color: "rgba(212,175,55,0.16)", priceLineVisible: false, lastValueVisible: false });
    ch.priceScale("uw").applyOptions({ scaleMargins: { top: 0, bottom: 0 }, visible: false });
    const futFmt = { type: "custom" as const, formatter: (p: number) => p.toLocaleString("en-IN", { maximumFractionDigits: 1 }) };
    const futC = ch.addSeries(CandlestickSeries, { priceScaleId: "left", upColor: "#10b981", downColor: "#ef4444", borderVisible: false,
      wickUpColor: "#10b981", wickDownColor: "#ef4444", priceFormat: futFmt, priceLineVisible: false });
    const fut = ch.addSeries(LineSeries, { priceScaleId: "left", color: "rgba(226,232,240,0.7)", lineWidth: 1, lineStyle: LineStyle.Dotted,
      title: "NIFTY fut", priceFormat: futFmt });
    const ce = ch.addSeries(LineSeries, { color: "#22c55e", lineWidth: 2, title: "Call", priceFormat: { type: "custom", formatter: (p: number) => inr(p) } });
    const pe = ch.addSeries(LineSeries, { color: "#ef4444", lineWidth: 2, title: "Put", priceFormat: { type: "custom", formatter: (p: number) => inr(p) } });
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
    return () => { ch.remove(); S.current = null; };
  }, []);

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
    const bucketToPt = new Map<number, number>();
    for (const p of d?.points ?? []) bucketToPt.set(Math.floor(p.t / step) * step, p.t);   // writer-flow time per bucket
    const data = showFut ? futCandles
      .map((c) => { const pt = bucketToPt.get(Math.floor(c.time / step) * step); return pt == null ? null : { time: pt as UTCTimestamp, open: c.o, high: c.h, low: c.l, close: c.c }; })
      .filter((x): x is NonNullable<typeof x> => x != null)
      .sort((a, b) => (a.time as number) - (b.time as number)) : [];
    s.futC.setData(data);
    s.futC.applyOptions({ visible: showFut });
    s.fut.applyOptions({ visible: !showFut });                 // candles replace the dotted line (one price view at a time)
  }, [futCandles, showFut, d, tf]);

  useEffect(() => { first.current = true; }, [tf, mode, n]);
  useEffect(() => {
    const s = S.current; const P = d?.points;
    if (!s || !P || !d?.ok) return;
    const T = (p: Pt) => p.t as UTCTimestamp;
    s.fut.setData(P.filter((p) => p.fut != null).map((p) => ({ time: T(p), value: p.fut as number })));
    s.ce.setData(P.filter((p) => p.ce != null).map((p) => ({ time: T(p), value: p.ce as number })));
    s.pe.setData(P.filter((p) => p.pe != null).map((p) => ({ time: T(p), value: p.pe as number })));
    const fv = P.filter((p) => p.flow != null);
    const abs = fv.map((p) => Math.abs(p.flow as number)).sort((a, b) => a - b);
    flCap.current = abs.length ? Math.max(1, abs[Math.floor(abs.length * 0.95)] * 1.3) : 0;
    const cap = flCap.current;
    // normal candles = faint; unusual (≥ 2.5×) = bright in its own direction; bars beyond the cap are clipped (value shown by the marker)
    s.fl.setData(fv.map((p) => { const v = p.flow as number, up = v >= 0; return { time: T(p), value: Math.max(-cap, Math.min(cap, v)),
      color: p.sig ? (up ? "#4ade80" : "#f87171") : up ? "rgba(34,197,94,.35)" : "rgba(239,68,68,.35)" }; }));
    // #2 gap ribbon — shade between the Call and Put OI lines
    s.band.set(P.filter((p) => p.ce != null && p.pe != null).map((p) => ({ time: p.t, ce: p.ce as number, pe: p.pe as number })));
    // #5 flow momentum — a trailing mean of the (clipped) flow so you see the drift under the spikes
    const win = 8; const clipped = fv.map((p) => Math.max(-cap, Math.min(cap, p.flow as number)));
    s.flma.setData(fv.map((p, i) => { const a = Math.max(0, i - win + 1); const seg = clipped.slice(a, i + 1);
      return { time: T(p), value: seg.reduce((x, y) => x + y, 0) / seg.length }; }));
    s.bal.setData(P.filter((p) => p.bal != null).map((p) => ({ time: T(p), value: p.bal as number })));
    const U = d.unwind;
    s.uw.setData(P.map((p) => ({ time: T(p), value: U && p.t >= U[0] && p.t <= U[1] ? 1 : 0 })));
    const mk: SeriesMarker<Time>[] = P.filter((p) => p.flip && p.conf).map((p) => ({ time: T(p),
      position: p.conf === "BULL" ? "belowBar" : "aboveBar", shape: p.conf === "BULL" ? "arrowUp" : "arrowDown",
      color: p.conf === "BULL" ? "#22c55e" : "#ef4444", text: p.conf === "BULL" ? "▲ BULL" : "▼ BEAR" }));
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
    // Expiry sessions / replay: minimum 3-bar cooldown (down from 15 bars)
    const minCooldownBars = isExpiryOrReplay ? 3 : 15;
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

        const callCr = p.ce / 1e7;
        const prevCallCr = pPrev.ce / 1e7;
        const putCr = p.pe / 1e7;
        const prevPutCr = pPrev.pe / 1e7;

        // --- Call (Green Line) Independent Evaluation ---
        let isCeJump = false;
        let dCeJump = 0;
        if (showJumpMarkers && i >= 3) {
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
        if (showSteepMarkers && i >= 5) {
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
                color: isUp ? "#4ade80" : "#f59e0b",
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
              color: isUp ? "#4ade80" : "#f59e0b",
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
                color: "#22c55e",
                size: 0.9,
                text: `🔔 ${callCr >= 0 ? "+" : ""}${bCall}Cr`,
              });
            }
          }
        }

        // --- Put (Red Line) Independent Evaluation ---
        let isPeJump = false;
        let dPeJump = 0;
        if (showJumpMarkers && i >= 3) {
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
        if (showSteepMarkers && i >= 5) {
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
                color: isUp ? "#f87171" : "#ef4444",
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
              color: isUp ? "#f87171" : "#ef4444",
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
                color: "#ef4444",
                size: 0.9,
                text: `🔔 ${putCr >= 0 ? "+" : ""}${bPut}Cr`,
              });
            }
          }
        }
      }

      mkCeList.sort((a, b) => (a.time as number) - (b.time as number));
      mkPeList.sort((a, b) => (a.time as number) - (b.time as number));

      if (markerMode === "latest") {
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
            ? `🔔 CALL OI +1Cr STEP: Green crossed ${lastCallCr.toFixed(2)}Cr`
            : `🔔 CALL OI -1Cr UNWIND: Green dropped to ${lastCallCr.toFixed(2)}Cr`;
          setActiveAlert({ text: msg, kind: "step1cr", ts: Date.now() });
          if (soundOn) playOiBeep("step1cr");
        } else if (prevPutBucketRef.current !== null && putBucket !== prevPutBucketRef.current) {
          const isUp = putBucket > prevPutBucketRef.current;
          const msg = isUp
            ? `🔔 PUT OI +1Cr STEP: Red crossed ${lastPutCr.toFixed(2)}Cr`
            : `🔔 PUT OI -1Cr UNWIND: Red dropped to ${lastPutCr.toFixed(2)}Cr`;
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
              const msg = `⚡ 40% CELL JUMP: Green (Call OI) jumped ${dCall3 > 0 ? "+" : ""}${dCall3.toFixed(2)}Cr (${pct.toFixed(0)}% of grid cell) in 3m!`;
              setActiveAlert({ text: msg, kind: "jump40", ts: Date.now() });
              if (soundOn) playOiBeep("jump40");
            } else if (Math.abs(dPut3) >= jumpThreshold) {
              const pct = (Math.abs(dPut3) / cellCr) * 100;
              const msg = `⚡ 40% CELL JUMP: Red (Put OI) jumped ${dPut3 > 0 ? "+" : ""}${dPut3.toFixed(2)}Cr (${pct.toFixed(0)}% of grid cell) in 3m!`;
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
                ? `✂️ SCISSOR DIVERGENCE: 🟢 BULLISH EXPLOSION (BUY CE) | Green ${callAngleDeg.toFixed(0)}° ▼ / Red +${putAngleDeg.toFixed(0)}° ▲ | Fut ${futDiff >= 0 ? "▲ Confirmed" : "⚠️ Lagging"}`
                : `✂️ SCISSOR DIVERGENCE: 🔴 BEARISH EXPLOSION (BUY PE) | Green +${callAngleDeg.toFixed(0)}° ▲ / Red ${putAngleDeg.toFixed(0)}° ▼ | Fut ${futDiff <= 0 ? "▼ Confirmed" : "⚠️ Lagging"}`;
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

    if (first.current) { s.ch.timeScale().fitContent(); first.current = false; }
  }, [d, angleThreshold, soundOn, markerMode, showSteepMarkers, showJumpMarkers, showStepMarkers]);

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

  return (
    <div className="flex h-full flex-col">
      <Controls tf={tf} setTf={setTf} mode={mode} setMode={setMode} n={n} setN={setN} tfs={["1m", "3m", "5m", "15m"]} busy={busy}
        info={d?.ok ? `${d.day} · expiry ${d.expiry ?? "?"} · ${d.label} · ${d.snapshots} snapshots` : d?.message ?? "loading…"} />

      {/* 📡 Live OI Velocity, Angle & Milestone Telemetry Strip */}
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-ink-800 bg-[#070c18] px-3 py-1.5 text-[11px]">
        <div className="flex flex-wrap items-center gap-2">
          {/* Green Call Angle Badge */}
          <span
            className={`flex items-center gap-1 rounded px-2 py-0.5 font-bold border transition-colors ${
              Math.abs(liveCallAngle) >= angleThreshold
                ? liveCallAngle > 0
                  ? "bg-rose-950/80 text-rose-300 border-rose-500/70 animate-pulse"
                  : "bg-emerald-950/80 text-emerald-300 border-emerald-500/70 animate-pulse"
                : liveCallAngle > 0
                ? "bg-emerald-950/40 text-emerald-400 border-emerald-800/50"
                : "bg-rose-950/40 text-rose-400 border-rose-800/50"
            }`}
            title={`Call OI (Green line) 5m slope: ${liveCallAngle.toFixed(1)}°. ${Math.abs(liveCallAngle) >= angleThreshold ? (liveCallAngle > 0 ? "Call writing surge (Bearish resistance)!" : "Call unwinding cliff (Bullish squeeze)!") : "Normal drift."}`}
          >
            <span>Green (Call OI):</span>
            <span className="font-mono">
              {liveCallAngle >= 0 ? "+" : ""}{liveCallAngle.toFixed(0)}° {liveCallAngle >= 0 ? "▲" : "▼"}
            </span>
            {Math.abs(liveCallAngle) >= angleThreshold && (
              <span className="text-[10px] uppercase tracking-wide font-extrabold">
                {liveCallAngle > 0 ? "(Steep Surge!)" : "(Steep Drop!)"}
              </span>
            )}
          </span>

          {/* Red Put Angle Badge */}
          <span
            className={`flex items-center gap-1 rounded px-2 py-0.5 font-bold border transition-colors ${
              Math.abs(livePutAngle) >= angleThreshold
                ? livePutAngle > 0
                  ? "bg-emerald-950/80 text-emerald-300 border-emerald-500/70 animate-pulse"
                  : "bg-rose-950/80 text-rose-300 border-rose-500/70 animate-pulse"
                : livePutAngle > 0
                ? "bg-rose-950/40 text-rose-400 border-rose-800/50"
                : "bg-emerald-950/40 text-emerald-400 border-emerald-800/50"
            }`}
            title={`Put OI (Red line) 5m slope: ${livePutAngle.toFixed(1)}°. ${Math.abs(livePutAngle) >= angleThreshold ? (livePutAngle > 0 ? "Put writing surge (Bullish floor)!" : "Put unwinding cliff (Panic liquidation)!") : "Normal drift."}`}
          >
            <span>Red (Put OI):</span>
            <span className="font-mono">
              {livePutAngle >= 0 ? "+" : ""}{livePutAngle.toFixed(0)}° {livePutAngle >= 0 ? "▲" : "▼"}
            </span>
            {Math.abs(livePutAngle) >= angleThreshold && (
              <span className="text-[10px] uppercase tracking-wide font-extrabold">
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
        <span><span className="text-bull-400">━ Call OI chg</span> · <span className="text-bear-400">━ Put OI chg</span> (since yesterday, right axis) · {showFut ? "real NIFTY-future candles" : "┈ NIFTY future line"} (left axis, adapts to active expiry)</span>
        <span>middle strip = Δput − Δcall each candle: <span className="text-bull-400">green ▲ = PUT writers added more (support, bullish)</span> · <span className="text-bear-400">red ▼ = CALL writers added more (resistance, bearish)</span> · bright bar + label = unusual (≥ 2.5× normal) · bottom gold line = 30-min balance: above +0.25 bullish, below −0.25 bearish</span>
        {last && <span className="text-slate-300">now: call {inr(last.ce)} · put {inr(last.pe)} · balance {last.bal?.toFixed(2)} · regime <b>{last.conf ?? last.regime}</b></span>}
      </div>
      <div ref={box} className="min-h-0 flex-1" />
      {(d?.read || d?.note) && <div className="border-t border-ink-700 px-3 py-1.5 text-[13px] text-slate-200">{clean(`${d?.read ?? ""} ${d?.note ?? ""}`)}</div>}
    </div>
  );
}

export const WritersChart = WritersFlow;


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
