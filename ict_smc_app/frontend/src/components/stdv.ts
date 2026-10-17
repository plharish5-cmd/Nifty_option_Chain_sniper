// stdv.ts — ICT Standard Deviation (STDV) Manipulation Leg Projections
// Mechanical identification of high-probability expansion targets after liquidity manipulation.
// Per ICT rules & TradingView STDV specifications:
//   Bullish: Anchor 1 at Low, Anchor 0 at High (downclose manipulation leg sweeping liquidity)
//   Bearish: Anchor 1 at High, Anchor 0 at Low (upclose manipulation leg sweeping liquidity)
//   -1.0, -1.5: Intermediate continuation levels
//   -2.0 to -2.5: MINOR REVERSAL ZONE (normal expansion exhaustion / TP1 / reaction band)
//   -4.0 to -4.5: MAJOR REVERSAL ZONE (extreme expansion exhaustion / TP2 / macro reversal band)

import type {
  IChartApi, IPrimitivePaneRenderer, IPrimitivePaneView, ISeriesApi, ISeriesPrimitive,
  SeriesAttachedParameter, SeriesType, Time,
} from "lightweight-charts";
import type { Candle } from "../api";

export interface StdvLevel {
  mult: number;       // 0, 1, -1, -1.5, -2, -2.25, -2.5, -3, -3.5, -4, -4.25, -4.5
  price: number;
  label: string;
  color: string;
  isZone?: "minor" | "major" | "anchor";
  dash?: boolean;
}

export type StdvStatus =
  | "PENDING"
  | "EXPANDING"
  | "AT_MINOR_ZONE"
  | "PAST_MINOR"
  | "AT_MAJOR_ZONE"
  | "EXTREME_OVERSHOOT";

export type StdvAnchorMode = "orderblock" | "swing" | "manual";

export interface StdvLeg {
  id: string;
  dir: "bull" | "bear";
  tf: string;
  anchorMode: StdvAnchorMode;
  from: number;         // Start time of manipulation leg
  to: number;           // End time of manipulation leg
  anchor0Time: number;  // Exact timestamp of Anchor 0 candle
  anchor1Time: number;  // Exact timestamp of Anchor 1 candle
  high: number;
  low: number;
  anchor0: number;      // 0 level (for bull: high; for bear: low)
  anchor1: number;      // 1 level (for bull: low; for bear: high)
  range: number;        // |anchor0 - anchor1|
  candlesCount: number;
  status: StdvStatus;
  statusText: string;
  levels: StdvLevel[];
  minorZone: [number, number];   // [minPrice, maxPrice]
  majorZone: [number, number];   // [minPrice, maxPrice]
  distMinorPts: number;
  distMajorPts: number;
  hitMinor: boolean;
  hitMajor: boolean;
  confluences: string[];
}

// ── Math & Levels Calculation ────────────────────────────────────────────────
export function computeStdvLevels(
  dir: "bull" | "bear",
  anchor0: number,
  anchor1: number,
  spot?: number,
): { levels: StdvLevel[]; minorZone: [number, number]; majorZone: [number, number]; status: StdvStatus; statusText: string } {
  const range = Math.abs(anchor0 - anchor1);
  const px = (mult: number) => {
    // For Bullish: multiplier -m expands UP: anchor0 + m * range
    // For Bearish: multiplier -m expands DOWN: anchor0 - m * range
    if (dir === "bull") {
      return anchor0 - mult * range;
    } else {
      return anchor0 + mult * range;
    }
  };

  const rawLevels: { mult: number; label: string; color: string; isZone?: "minor" | "major" | "anchor"; dash?: boolean }[] = [
    { mult: 1, label: dir === "bull" ? "1.0 (Low Anchor)" : "1.0 (High Anchor)", color: "#94a3b8", isZone: "anchor", dash: true },
    { mult: 0, label: dir === "bull" ? "0.0 (High / Shift)" : "0.0 (Low / Shift)", color: "#cbd5e1", isZone: "anchor" },
    { mult: -1.0, label: "-1.0 Expansion", color: "#64748b", dash: true },
    { mult: -1.5, label: "-1.5 Expansion", color: "#64748b", dash: true },
    { mult: -2.0, label: "-2.0 Minor Rev", color: "#ef4444", isZone: "minor" },
    { mult: -2.25, label: "-2.25 Mid Rev", color: "#dc2626", isZone: "minor", dash: true },
    { mult: -2.5, label: "-2.5 Minor Rev", color: "#b91c1c", isZone: "minor" },
    { mult: -3.0, label: "-3.0 Expansion", color: "#64748b", dash: true },
    { mult: -3.5, label: "-3.5 Expansion", color: "#64748b", dash: true },
    { mult: -4.0, label: "-4.0 Major Rev", color: "#a855f7", isZone: "major" },
    { mult: -4.25, label: "-4.25 Mid Major", color: "#9333ea", isZone: "major", dash: true },
    { mult: -4.5, label: "-4.5 Major Rev", color: "#7e22ce", isZone: "major" },
  ];

  const levels: StdvLevel[] = rawLevels.map((l) => ({
    ...l,
    price: round(px(l.mult), 1),
    label: `${l.label} (${round(px(l.mult), 1)})`,
  }));

  const p20 = px(-2.0), p25 = px(-2.5);
  const minorZone: [number, number] = [Math.min(p20, p25), Math.max(p20, p25)];
  const p40 = px(-4.0), p45 = px(-4.5);
  const majorZone: [number, number] = [Math.min(p40, p45), Math.max(p40, p45)];

  let status: StdvStatus = "PENDING";
  let statusText = "Pending expansion";

  if (spot != null && range > 0) {
    if (dir === "bull") {
      if (spot < anchor0) {
        status = "PENDING";
        statusText = `Consolidating in manipulation leg (${round(anchor0 - spot, 1)} pts below 0)`;
      } else if (spot < minorZone[0]) {
        status = "EXPANDING";
        statusText = `Expanding towards Minor Reversal (${round(minorZone[0] - spot, 1)} pts to TP1)`;
      } else if (spot <= minorZone[1]) {
        status = "AT_MINOR_ZONE";
        statusText = "★ INSIDE MINOR REVERSAL ZONE (-2 to -2.5) — Watch for reaction / MSS";
      } else if (spot < majorZone[0]) {
        status = "PAST_MINOR";
        statusText = `Expansion continuing to Major Reversal (${round(majorZone[0] - spot, 1)} pts to TP2)`;
      } else if (spot <= majorZone[1]) {
        status = "AT_MAJOR_ZONE";
        statusText = "⚠️ INSIDE MAJOR REVERSAL ZONE (-4 to -4.5) — Extreme exhaustion / Macro reversal risk";
      } else {
        status = "EXTREME_OVERSHOOT";
        statusText = "Extended beyond -4.5 STD DEV";
      }
    } else {
      if (spot > anchor0) {
        status = "PENDING";
        statusText = `Consolidating in manipulation leg (${round(spot - anchor0, 1)} pts above 0)`;
      } else if (spot > minorZone[1]) {
        status = "EXPANDING";
        statusText = `Expanding downwards towards Minor Reversal (${round(spot - minorZone[1], 1)} pts to TP1)`;
      } else if (spot >= minorZone[0]) {
        status = "AT_MINOR_ZONE";
        statusText = "★ INSIDE MINOR REVERSAL ZONE (-2 to -2.5) — Watch for reaction / MSS";
      } else if (spot > majorZone[1]) {
        status = "PAST_MINOR";
        statusText = `Downward expansion continuing to Major Reversal (${round(spot - majorZone[1], 1)} pts to TP2)`;
      } else if (spot >= majorZone[0]) {
        status = "AT_MAJOR_ZONE";
        statusText = "⚠️ INSIDE MAJOR REVERSAL ZONE (-4 to -4.5) — Extreme exhaustion / Macro reversal risk";
      } else {
        status = "EXTREME_OVERSHOOT";
        statusText = "Extended beyond -4.5 STD DEV";
      }
    }
  }

  return { levels, minorZone, majorZone, status, statusText };
}

function round(n: number, d = 1): number {
  const p = Math.pow(10, d);
  return Math.round(n * p) / p;
}

// ── Manipulation Leg Detection ───────────────────────────────────────────────
export function detectManipulationLegs(
  candles: Candle[],
  tfName = "15m",
  spot?: number,
  anchorMode: "orderblock" | "swing" = "orderblock",
): StdvLeg[] {
  const closed = candles.filter((c) => c.closed !== false);
  if (closed.length < 6) return [];

  const legs: StdvLeg[] = [];

  // Find swing pivots (3-bar lookback on both sides)
  for (let i = 4; i < closed.length - 1; i++) {
    const cur = closed[i];

    // Check for Bullish Manipulation Leg:
    // A swing low that swept a prior swing low, preceded by consecutive downclose candles
    const isSwingLow =
      cur.low <= closed[i - 1].low &&
      cur.low <= closed[i - 2].low &&
      cur.low <= closed[i + 1].low;

    if (isSwingLow) {
      // Check if it swept a previous low within prior 35 bars
      let sweptLow: Candle | null = null;
      let swingHighBeforeDrop: Candle = closed[i - 1];
      for (let j = Math.max(0, i - 35); j < i; j++) {
        if (closed[j].high > swingHighBeforeDrop.high) {
          swingHighBeforeDrop = closed[j];
        }
        if (j < i - 2 && closed[j].low > cur.low && closed[j].low <= cur.low + (cur.high - cur.low) * 2.5) {
          sweptLow = closed[j];
        }
      }

      // Trace consecutive downclose candles leading into cur (Order Block run)
      let k = i;
      let downCount = 0;
      let obHigh = cur.high;
      let obHighTime = cur.time;
      while (k >= 0 && closed[k].close <= closed[k].open && downCount < 10) {
        downCount++;
        if (closed[k].high >= obHigh) {
          obHigh = closed[k].high;
          obHighTime = closed[k].time;
        }
        k--;
      }

      if (downCount >= 2 || sweptLow) {
        // Look for subsequent delivery shift
        const shifted = closed.slice(i + 1, Math.min(closed.length, i + 8)).some((c) => c.close > (obHigh + cur.low) / 2);
        if (shifted || i >= closed.length - 4) {
          const from = anchorMode === "swing" ? swingHighBeforeDrop.time : closed[Math.max(0, k + 1)].time;
          const to = cur.time;
          const anchor0 = round(anchorMode === "swing" ? swingHighBeforeDrop.high : obHigh, 1);
          const anchor0Time = anchorMode === "swing" ? swingHighBeforeDrop.time : obHighTime;
          const anchor1 = round(cur.low, 1);
          const anchor1Time = cur.time;
          const range = round(anchor0 - anchor1, 1);

          if (range > 5) {
            const currentSpot = spot ?? closed[closed.length - 1].close;
            const computed = computeStdvLevels("bull", anchor0, anchor1, currentSpot);
            const distMinor = currentSpot < computed.minorZone[0] ? computed.minorZone[0] - currentSpot : currentSpot > computed.minorZone[1] ? currentSpot - computed.minorZone[1] : 0;
            const distMajor = currentSpot < computed.majorZone[0] ? computed.majorZone[0] - currentSpot : currentSpot > computed.majorZone[1] ? currentSpot - computed.majorZone[1] : 0;
            const hitMinor = currentSpot >= computed.minorZone[0];
            const hitMajor = currentSpot >= computed.majorZone[0];

            legs.push({
              id: `stdv-bull-${tfName}-${from}-${anchorMode}`,
              dir: "bull",
              tf: tfName,
              anchorMode,
              from,
              to,
              anchor0Time,
              anchor1Time,
              high: anchor0,
              low: anchor1,
              anchor0,
              anchor1,
              range,
              candlesCount: i - k,
              status: computed.status,
              statusText: computed.statusText,
              levels: computed.levels,
              minorZone: computed.minorZone,
              majorZone: computed.majorZone,
              distMinorPts: round(distMinor, 1),
              distMajorPts: round(distMajor, 1),
              hitMinor,
              hitMajor,
              confluences: [
                sweptLow ? `Swept liquidity (${round(sweptLow.low, 1)})` : "Swing low sweep",
                anchorMode === "orderblock" ? `${downCount} downclose candles (OB)` : "Full Judas swing",
                `Range: ${range} pts`,
              ],
            });
          }
        }
      }
    }

    // Check for Bearish Manipulation Leg:
    // A swing high that swept a prior swing high, preceded by consecutive upclose candles
    const isSwingHigh =
      cur.high >= closed[i - 1].high &&
      cur.high >= closed[i - 2].high &&
      cur.high >= closed[i + 1].high;

    if (isSwingHigh) {
      let sweptHigh: Candle | null = null;
      let swingLowBeforeRally: Candle = closed[i - 1];
      for (let j = Math.max(0, i - 35); j < i; j++) {
        if (closed[j].low < swingLowBeforeRally.low) {
          swingLowBeforeRally = closed[j];
        }
        if (j < i - 2 && closed[j].high < cur.high && closed[j].high >= cur.high - (cur.high - cur.low) * 2.5) {
          sweptHigh = closed[j];
        }
      }

      let k = i;
      let upCount = 0;
      let obLow = cur.low;
      let obLowTime = cur.time;
      while (k >= 0 && closed[k].close >= closed[k].open && upCount < 10) {
        upCount++;
        if (closed[k].low <= obLow) {
          obLow = closed[k].low;
          obLowTime = closed[k].time;
        }
        k--;
      }

      if (upCount >= 2 || sweptHigh) {
        const shifted = closed.slice(i + 1, Math.min(closed.length, i + 8)).some((c) => c.close < (cur.high + obLow) / 2);
        if (shifted || i >= closed.length - 4) {
          const from = anchorMode === "swing" ? swingLowBeforeRally.time : closed[Math.max(0, k + 1)].time;
          const to = cur.time;
          const anchor0 = round(anchorMode === "swing" ? swingLowBeforeRally.low : obLow, 1);
          const anchor0Time = anchorMode === "swing" ? swingLowBeforeRally.time : obLowTime;
          const anchor1 = round(cur.high, 1);
          const anchor1Time = cur.time;
          const range = round(anchor1 - anchor0, 1);

          if (range > 5) {
            const currentSpot = spot ?? closed[closed.length - 1].close;
            const computed = computeStdvLevels("bear", anchor0, anchor1, currentSpot);
            const distMinor = currentSpot > computed.minorZone[1] ? currentSpot - computed.minorZone[1] : currentSpot < computed.minorZone[0] ? computed.minorZone[0] - currentSpot : 0;
            const distMajor = currentSpot > computed.majorZone[1] ? currentSpot - computed.majorZone[1] : currentSpot < computed.majorZone[0] ? computed.majorZone[0] - currentSpot : 0;
            const hitMinor = currentSpot <= computed.minorZone[1];
            const hitMajor = currentSpot <= computed.majorZone[1];

            legs.push({
              id: `stdv-bear-${tfName}-${from}-${anchorMode}`,
              dir: "bear",
              tf: tfName,
              anchorMode,
              from,
              to,
              anchor0Time,
              anchor1Time,
              high: anchor1,
              low: anchor0,
              anchor0,
              anchor1,
              range,
              candlesCount: i - k,
              status: computed.status,
              statusText: computed.statusText,
              levels: computed.levels,
              minorZone: computed.minorZone,
              majorZone: computed.majorZone,
              distMinorPts: round(distMinor, 1),
              distMajorPts: round(distMajor, 1),
              hitMinor,
              hitMajor,
              confluences: [
                sweptHigh ? `Swept liquidity (${round(sweptHigh.high, 1)})` : "Swing high sweep",
                anchorMode === "orderblock" ? `${upCount} upclose candles (OB)` : "Full Judas swing",
                `Range: ${range} pts`,
              ],
            });
          }
        }
      }
    }
  }

  // Sort by most recent leg first
  return legs.sort((a, b) => b.to - a.to);
}

// ── Manual / Click-to-Anchor Leg Creator ──────────────────────────────────────
export function createManualStdvLeg(
  p1: { time: number; price: number },
  p2: { time: number; price: number },
  tfName = "15m",
  spot?: number,
): StdvLeg {
  // If p1 (sweep) is lower than p2 (shift high) -> Bullish Low to High
  const isBull = p1.price < p2.price;
  const dir: "bull" | "bear" = isBull ? "bull" : "bear";

  const anchor1 = round(p1.price, 1);
  const anchor1Time = p1.time;
  const anchor0 = round(p2.price, 1);
  const anchor0Time = p2.time;
  const range = round(Math.abs(anchor0 - anchor1), 1);

  const currentSpot = spot ?? anchor0;
  const computed = computeStdvLevels(dir, anchor0, anchor1, currentSpot);

  const distMinor = isBull
    ? currentSpot < computed.minorZone[0] ? computed.minorZone[0] - currentSpot : currentSpot > computed.minorZone[1] ? currentSpot - computed.minorZone[1] : 0
    : currentSpot > computed.minorZone[1] ? currentSpot - computed.minorZone[1] : currentSpot < computed.minorZone[0] ? computed.minorZone[0] - currentSpot : 0;

  const distMajor = isBull
    ? currentSpot < computed.majorZone[0] ? computed.majorZone[0] - currentSpot : currentSpot > computed.majorZone[1] ? currentSpot - computed.majorZone[1] : 0
    : currentSpot > computed.majorZone[1] ? currentSpot - computed.majorZone[1] : currentSpot < computed.majorZone[0] ? computed.majorZone[0] - currentSpot : 0;

  return {
    id: `stdv-manual-${tfName}-${Date.now()}`,
    dir,
    tf: tfName,
    anchorMode: "manual",
    from: Math.min(p1.time, p2.time),
    to: Math.max(p1.time, p2.time),
    anchor0Time,
    anchor1Time,
    high: Math.max(anchor0, anchor1),
    low: Math.min(anchor0, anchor1),
    anchor0,
    anchor1,
    range,
    candlesCount: 1,
    status: computed.status,
    statusText: computed.statusText,
    levels: computed.levels,
    minorZone: computed.minorZone,
    majorZone: computed.majorZone,
    distMinorPts: round(distMinor, 1),
    distMajorPts: round(distMajor, 1),
    hitMinor: isBull ? currentSpot >= computed.minorZone[0] : currentSpot <= computed.minorZone[1],
    hitMajor: isBull ? currentSpot >= computed.majorZone[0] : currentSpot <= computed.majorZone[1],
    confluences: ["Interactive 2-Click Chart Anchor", `Range: ${range} pts`],
  };
}

// ── Multi-Timeframe Candle Aggregator ─────────────────────────────────────────
export function aggregateBars(candles: Candle[], bucketSec: number): Candle[] {
  if (!candles.length) return [];
  const map = new Map<number, Candle>();

  for (const c of candles) {
    const k = Math.floor(c.time / bucketSec) * bucketSec;
    const existing = map.get(k);
    if (!existing) {
      map.set(k, { time: k, open: c.open, high: c.high, low: c.low, close: c.close, closed: c.closed });
    } else {
      existing.high = Math.max(existing.high, c.high);
      existing.low = Math.min(existing.low, c.low);
      existing.close = c.close;
      existing.closed = c.closed;
    }
  }

  return Array.from(map.values()).sort((a, b) => a.time - b.time);
}

// ── Lightweight Charts v5 Canvas Primitive: StdvPrimitive ───────────────────
type MediaScope = { context: CanvasRenderingContext2D; mediaSize: { width: number; height: number } };
type Target = { useMediaCoordinateSpace: (f: (s: MediaScope) => void) => void };

class StdvRenderer implements IPrimitivePaneRenderer {
  constructor(private p: StdvPrimitive) {}

  draw() { /* drawn in drawBackground so candles stay on top */ }

  drawBackground(target: Target) {
    const { chart, series, activeLeg } = this.p;
    if (!chart || !series || !activeLeg) return;

    const ts = chart.timeScale();

    target.useMediaCoordinateSpace(({ context: ctx, mediaSize }) => {
      const W = mediaSize.width;
      const H = mediaSize.height;

      const x = (t: number, fallback: number) => {
        const c = ts.timeToCoordinate(t as Time);
        if (c !== null) return c as number;
        const r = ts.getVisibleRange();
        if (!r) return fallback;
        return t < (r.from as number) ? 0 : W;
      };

      const y = (p: number) => series.priceToCoordinate(p);

      ctx.save();
      ctx.font = "10px Inter, system-ui, sans-serif";

      // 1. Shaded Manipulation Leg Box (Vertical Column like Screenshot 6)
      const legY1 = y(activeLeg.high);
      const legY2 = y(activeLeg.low);
      const legX1 = x(activeLeg.from, 0);
      const legX2 = Math.max(legX1 + 14, x(activeLeg.to, W));

      if (legY1 !== null && legY2 !== null && legX2 > 0 && legX1 < W) {
        const boxTop = Math.min(legY1, legY2);
        const boxH = Math.max(2, Math.abs(legY2 - legY1));
        const boxW = Math.max(6, legX2 - legX1);

        // Shaded manipulation box (pink/burgundy column per Screenshot 6)
        ctx.fillStyle = activeLeg.dir === "bull" ? "rgba(244, 63, 94, 0.16)" : "rgba(56, 189, 248, 0.16)";
        ctx.fillRect(legX1, boxTop, boxW, boxH);

        ctx.strokeStyle = activeLeg.dir === "bull" ? "rgba(244, 63, 94, 0.7)" : "rgba(56, 189, 248, 0.7)";
        ctx.lineWidth = 1;
        ctx.strokeRect(legX1 + 0.5, boxTop + 0.5, boxW - 1, boxH - 1);

        // Label above the manipulation box
        ctx.fillStyle = activeLeg.dir === "bull" ? "#fb7185" : "#38bdf8";
        ctx.fillText(`Manipulation Leg (${activeLeg.dir === "bull" ? "Low→High" : "High→Low"})`, legX1 + 4, Math.max(12, boxTop - 4));
      }

      // 2. Shaded Minor Reversal Zone (-2.0 to -2.5)
      const minRevY1 = y(activeLeg.minorZone[0]);
      const minRevY2 = y(activeLeg.minorZone[1]);
      if (minRevY1 !== null && minRevY2 !== null) {
        const mTop = Math.min(minRevY1, minRevY2);
        const mH = Math.max(2, Math.abs(minRevY2 - minRevY1));
        ctx.fillStyle = "rgba(185, 28, 28, 0.22)";     // Burgundy red band
        ctx.fillRect(legX1, mTop, W - legX1, mH);

        ctx.strokeStyle = "rgba(239, 68, 68, 0.5)";
        ctx.strokeRect(legX1, mTop, W - legX1, mH);

        // Label banner
        ctx.fillStyle = "rgba(185, 28, 28, 0.9)";
        ctx.fillRect(legX1, mTop - 13, 205, 13);
        ctx.fillStyle = "#ffffff";
        ctx.fillText("🎯 MINOR REVERSAL ZONE (-2 to -2.5)", legX1 + 4, mTop - 3);
      }

      // 3. Shaded Major Reversal Zone (-4.0 to -4.5)
      const majRevY1 = y(activeLeg.majorZone[0]);
      const majRevY2 = y(activeLeg.majorZone[1]);
      if (majRevY1 !== null && majRevY2 !== null) {
        const majTop = Math.min(majRevY1, majRevY2);
        const majH = Math.max(2, Math.abs(majRevY2 - majRevY1));
        ctx.fillStyle = "rgba(126, 34, 206, 0.22)";    // Deep purple band
        ctx.fillRect(legX1, majTop, W - legX1, majH);

        ctx.strokeStyle = "rgba(168, 85, 247, 0.5)";
        ctx.strokeRect(legX1, majTop, W - legX1, majH);

        // Label banner
        ctx.fillStyle = "rgba(126, 34, 206, 0.9)";
        ctx.fillRect(legX1, majTop - 13, 215, 13);
        ctx.fillStyle = "#ffffff";
        ctx.fillText("💥 MAJOR REVERSAL ZONE (-4 to -4.5)", legX1 + 4, majTop - 3);
      }

      // 4. Horizontal Projection Lines with Right-Hand Labels
      for (const lvl of activeLeg.levels) {
        const yy = y(lvl.price);
        if (yy === null || yy < -15 || yy > H + 15) continue;

        ctx.strokeStyle = lvl.color;
        ctx.lineWidth = lvl.isZone ? 1.5 : 1;
        ctx.setLineDash(lvl.dash ? [4, 3] : []);

        ctx.beginPath();
        ctx.moveTo(legX1, Math.round(yy) + 0.5);
        ctx.lineTo(W - 8, Math.round(yy) + 0.5);
        ctx.stroke();
        ctx.setLineDash([]);

        // Label chip on right side
        const tag = `${lvl.mult}σ · ${lvl.price.toFixed(1)}`;
        const tw = ctx.measureText(tag).width;
        const chipW = tw + 8;
        const chipX = W - chipW - 4;
        const chipY = Math.round(yy) - 6;

        ctx.fillStyle = "rgba(11, 18, 32, 0.9)";
        ctx.fillRect(chipX, chipY, chipW, 13);

        ctx.fillStyle = lvl.color;
        ctx.fillRect(chipX, chipY, 2, 13);
        ctx.fillText(tag, chipX + 5, chipY + 10);
      }

      // 5. Prominent Red Circular Anchor Badges (0) and (1) matching Screenshot 8!
      const a0X = x(activeLeg.anchor0Time || activeLeg.from, legX1);
      const a0Y = y(activeLeg.anchor0);
      const a1X = x(activeLeg.anchor1Time || activeLeg.to, legX2);
      const a1Y = y(activeLeg.anchor1);

      // Connect Anchor 0 and Anchor 1 with dotted line
      if (a0Y !== null && a1Y !== null) {
        ctx.strokeStyle = "rgba(239, 68, 68, 0.75)";
        ctx.lineWidth = 1.5;
        ctx.setLineDash([3, 3]);
        ctx.beginPath();
        ctx.moveTo(a0X, a0Y);
        ctx.lineTo(a1X, a1Y);
        ctx.stroke();
        ctx.setLineDash([]);
      }

      // Draw Anchor Badge (0)
      if (a0Y !== null) {
        ctx.beginPath();
        ctx.arc(a0X, a0Y, 9, 0, Math.PI * 2);
        ctx.fillStyle = "#dc2626"; // Vibrant red per Screenshot 8
        ctx.fill();
        ctx.strokeStyle = "#ffffff";
        ctx.lineWidth = 1.5;
        ctx.stroke();

        ctx.fillStyle = "#ffffff";
        ctx.font = "bold 10px Inter, sans-serif";
        ctx.textAlign = "center";
        ctx.textBaseline = "middle";
        ctx.fillText("0", a0X, a0Y);

        ctx.textAlign = "left";
        ctx.font = "bold 10px Inter, sans-serif";
        ctx.fillStyle = "#fca5a5";
        ctx.fillText(`0.0 [${activeLeg.dir === "bull" ? "High Shift" : "Low Shift"}]`, a0X + 13, a0Y + 3);
      }

      // Draw Anchor Badge (1)
      if (a1Y !== null) {
        ctx.beginPath();
        ctx.arc(a1X, a1Y, 9, 0, Math.PI * 2);
        ctx.fillStyle = "#dc2626"; // Vibrant red per Screenshot 8
        ctx.fill();
        ctx.strokeStyle = "#ffffff";
        ctx.lineWidth = 1.5;
        ctx.stroke();

        ctx.fillStyle = "#ffffff";
        ctx.font = "bold 10px Inter, sans-serif";
        ctx.textAlign = "center";
        ctx.textBaseline = "middle";
        ctx.fillText("1", a1X, a1Y);

        ctx.textAlign = "left";
        ctx.font = "bold 10px Inter, sans-serif";
        ctx.fillStyle = "#fca5a5";
        ctx.fillText(`1.0 [Sweep Wick]`, a1X + 13, a1Y + 3);
      }

      ctx.restore();
    });
  }
}

class StdvView implements IPrimitivePaneView {
  private r: StdvRenderer;
  constructor(p: StdvPrimitive) {
    this.r = new StdvRenderer(p);
  }
  zOrder() {
    return "bottom" as const;
  }
  renderer() {
    return this.r;
  }
}

export class StdvPrimitive implements ISeriesPrimitive<Time> {
  chart: IChartApi | null = null;
  series: ISeriesApi<SeriesType> | null = null;
  activeLeg: StdvLeg | null = null;
  private views = [new StdvView(this)];
  private request: (() => void) | null = null;

  attached(p: SeriesAttachedParameter<Time>) {
    this.chart = p.chart as IChartApi;
    this.series = p.series;
    this.request = p.requestUpdate;
  }

  detached() {
    this.chart = null;
    this.series = null;
    this.request = null;
  }

  paneViews() {
    return this.views;
  }

  setLeg(leg: StdvLeg | null) {
    this.activeLeg = leg;
    this.request?.();
  }
}
