// gtf.ts — GTF Institutional Demand & Supply Indicator (Version 2.0 Equivalent).
// Automates institutional zone detection using core price-action principles:
//   1. Boring Candle Detection: body < ABR(14) * 0.6
//   2. Explosive Candle Detection: body > ABR(14) * 1.5 with Volume Expansion
//   3. Demand (RBR/DBR) and Supply (DBD/RBD) Base Zone Clustering (1-3 boring candles)
//   4. Conservative (Body-Wick), Aggressive (Wick-Wick), and Dynamic (ATR-adjusted) Modes
//   5. Live Retest & Freshness Tracking (FRESH, TESTED, MITIGATED/BROKEN)
//   6. Master Confluence Matrix: GTF Zones + OI Walls + CVD Order Flow + PE-CE Table Action

import type { Candle } from "../api";
import type { Wall, CvdDivergence } from "./orderflow";
import type { FpBar } from "./footprint";
import type { SmartRow } from "./OcLab";

export type GtfMarkingMode = "conservative" | "aggressive" | "dynamic";

export interface GtfZone {
  id: string;
  type: "DEMAND" | "SUPPLY";
  dir: "bull" | "bear";
  from: number;
  to: number;
  top: number;
  bottom: number;
  proximal: number;
  distal: number;
  status: "FRESH" | "TESTED" | "MITIGATED" | "BROKEN";
  testCount: number;
  mode: GtfMarkingMode;
  pattern: "RBR" | "DBR" | "DBD" | "RBD";
  score: number;
  grade: "A+" | "A" | "B";
  baseBars: number;
  explosiveTime: number;
  explosiveRatio: number;
  label: string;
}

export interface GtfResult {
  zones: GtfZone[];
  freshDemands: GtfZone[];
  freshSupplies: GtfZone[];
  nearestDemand: GtfZone | null;
  nearestSupply: GtfZone | null;
  currentPrice: number;
  mode: GtfMarkingMode;
}

export function computeAbr(closes: number[], opens: number[], period = 14): number[] {
  const n = closes.length;
  const abr = new Array<number>(n).fill(0);
  for (let i = 0; i < n; i++) {
    const start = Math.max(0, i - period + 1);
    let sum = 0;
    for (let k = start; k <= i; k++) {
      sum += Math.abs(closes[k] - opens[k]);
    }
    abr[i] = sum / (i - start + 1);
  }
  return abr;
}

export function detectGtfZones(
  candles: Candle[],
  mode: GtfMarkingMode = "conservative",
  boringRatio = 0.60,
  explosiveRatio = 1.50,
  baseMaxBars = 3
): GtfResult {
  const closed = candles.filter((c) => c.closed !== false);
  const n = closed.length;
  if (n < 5) {
    return {
      zones: [],
      freshDemands: [],
      freshSupplies: [],
      nearestDemand: null,
      nearestSupply: null,
      currentPrice: closed.length ? closed[closed.length - 1].close : 0,
      mode,
    };
  }

  const closes = closed.map((c) => c.close);
  const opens = closed.map((c) => c.open);
  const highs = closed.map((c) => c.high);
  const lows = closed.map((c) => c.low);
  const times = closed.map((c) => c.time);
  const vols = closed.map((c) => c.volume ?? 0);

  const abr = computeAbr(closes, opens, 14);

  // Volume SMA 20
  const volSma = new Array<number>(n).fill(0);
  for (let i = 0; i < n; i++) {
    const start = Math.max(0, i - 19);
    let sum = 0;
    for (let k = start; k <= i; k++) sum += vols[k];
    volSma[i] = sum / (i - start + 1);
  }

  // Estimate ATR for dynamic mode
  const atr = new Array<number>(n).fill(15);
  for (let i = 1; i < n; i++) {
    const tr = Math.max(highs[i] - lows[i], Math.abs(highs[i] - closes[i - 1]), Math.abs(lows[i] - closes[i - 1]));
    atr[i] = (atr[i - 1] * 13 + tr) / 14;
  }

  const isBoring = new Array<boolean>(n).fill(false);
  const isExplosiveUp = new Array<boolean>(n).fill(false);
  const isExplosiveDown = new Array<boolean>(n).fill(false);

  for (let i = 0; i < n; i++) {
    const body = Math.abs(closes[i] - opens[i]);
    const cRange = Math.max(highs[i] - lows[i], 1e-4);
    const curAbr = Math.max(abr[i], 5.0);

    if (body <= curAbr * boringRatio || body / cRange <= 0.45) {
      isBoring[i] = true;
    }

    const hasVol = vols[i] === 0 || volSma[i] === 0 || vols[i] >= volSma[i];
    if (body >= curAbr * explosiveRatio && hasVol) {
      if (closes[i] > opens[i]) isExplosiveUp[i] = true;
      else if (closes[i] < opens[i]) isExplosiveDown[i] = true;
    }
  }

  const rawZones: GtfZone[] = [];

  for (let j = 1; j < n; j++) {
    if (!isExplosiveUp[j] && !isExplosiveDown[j]) continue;

    let baseCount = 0;
    for (let k = 1; k <= baseMaxBars; k++) {
      const idx = j - k;
      if (idx < 0) break;
      if (isBoring[idx]) baseCount++;
      else break;
    }

    if (baseCount < 1) continue;

    const baseIndices: number[] = [];
    for (let k = j - baseCount; k < j; k++) baseIndices.push(k);

    const baseFromTs = times[baseIndices[0]];
    const expTs = times[j];
    const expBody = Math.abs(closes[j] - opens[j]);
    const expRatio = expBody / Math.max(abr[j], 1.0);
    const curAtr = atr[j];

    const priorIdx = baseIndices[0] - 1;
    const isPriorGreen = priorIdx >= 0 && closes[priorIdx] > opens[priorIdx];

    let zType: "DEMAND" | "SUPPLY";
    let zDir: "bull" | "bear";
    let pattern: "RBR" | "DBR" | "DBD" | "RBD";
    let proximal: number;
    let distal: number;

    if (isExplosiveUp[j]) {
      zType = "DEMAND";
      zDir = "bull";
      pattern = isPriorGreen ? "RBR" : "DBR";

      const highestBody = Math.max(...baseIndices.map((idx) => Math.max(opens[idx], closes[idx])));
      const lowestWick = Math.min(...baseIndices.map((idx) => lows[idx]));
      const highestWick = Math.max(...baseIndices.map((idx) => highs[idx]));

      if (mode === "conservative") {
        proximal = highestBody;
        distal = lowestWick;
      } else if (mode === "aggressive") {
        proximal = highestWick;
        distal = lowestWick;
      } else {
        // Dynamic
        const pad = curAtr * 0.15;
        proximal = highestBody + pad;
        distal = lowestWick - pad;
      }
    } else {
      zType = "SUPPLY";
      zDir = "bear";
      pattern = !isPriorGreen ? "DBD" : "RBD";

      const lowestBody = Math.min(...baseIndices.map((idx) => Math.min(opens[idx], closes[idx])));
      const highestWick = Math.max(...baseIndices.map((idx) => highs[idx]));
      const lowestWick = Math.min(...baseIndices.map((idx) => lows[idx]));

      if (mode === "conservative") {
        proximal = lowestBody;
        distal = highestWick;
      } else if (mode === "aggressive") {
        proximal = lowestWick;
        distal = highestWick;
      } else {
        // Dynamic
        const pad = curAtr * 0.15;
        proximal = lowestBody - pad;
        distal = highestWick + pad;
      }
    }

    const top = Math.max(proximal, distal);
    const bottom = Math.min(proximal, distal);

    // Retest / mitigation lifecycle
    let status: "FRESH" | "TESTED" | "MITIGATED" | "BROKEN" = "FRESH";
    let testCount = 0;
    let endTs = times[n - 1];

    for (let m = j + 1; m < n; m++) {
      const cC = closes[m];
      const cH = highs[m];
      const cL = lows[m];

      if (zType === "DEMAND") {
        if (cC < distal) {
          status = "BROKEN";
          endTs = times[m];
          break;
        } else if (cL <= proximal) {
          testCount++;
          status = "TESTED";
        }
      } else {
        if (cC > distal) {
          status = "BROKEN";
          endTs = times[m];
          break;
        } else if (cH >= proximal) {
          testCount++;
          status = "TESTED";
        }
      }
    }

    // Score & Grade
    let score = 0;
    if (status === "FRESH") score += 4.0;
    else if (status === "TESTED") score += testCount === 1 ? 2.5 : 1.5;
    else score += 0.5;

    if (expRatio >= 2.0) score += 3.0;
    else if (expRatio >= 1.5) score += 2.0;
    else score += 1.0;

    if (baseCount === 1) score += 2.5;
    else if (baseCount === 2) score += 1.5;
    else score += 1.0;

    const grade: "A+" | "A" | "B" = score >= 8.5 ? "A+" : score >= 6.5 ? "A" : "B";
    const statusLabel = status === "TESTED" ? `TESTED (${testCount}x)` : status;
    const label = `🏛️ GTF ${zType} ${pattern} · ${statusLabel} [${grade}]`;

    rawZones.push({
      id: `gtf_${zType.toLowerCase()}_${baseFromTs}`,
      type: zType,
      dir: zDir,
      from: baseFromTs,
      to: endTs,
      top,
      bottom,
      proximal,
      distal,
      status,
      testCount,
      mode,
      pattern,
      score: Math.min(10, score),
      grade,
      baseBars: baseCount,
      explosiveTime: expTs,
      explosiveRatio: expRatio,
      label,
    });
  }

  const curPrice = closes[n - 1];
  const activeZones = rawZones.filter((z) => z.status !== "BROKEN");
  const brokenZones = rawZones.filter((z) => z.status === "BROKEN");

  activeZones.sort((a, b) => {
    if (a.status === "FRESH" && b.status !== "FRESH") return -1;
    if (b.status === "FRESH" && a.status !== "FRESH") return 1;
    return Math.abs(a.proximal - curPrice) - Math.abs(b.proximal - curPrice);
  });

  const selected = [...activeZones.slice(0, 8), ...brokenZones.slice(-3)].sort((a, b) => a.from - b.from);

  const freshDemands = activeZones.filter((z) => z.type === "DEMAND" && z.status === "FRESH");
  const freshSupplies = activeZones.filter((z) => z.type === "SUPPLY" && z.status === "FRESH");

  const demandsBelow = activeZones.filter((z) => z.type === "DEMAND" && z.proximal <= curPrice + 15.0);
  const suppliesAbove = activeZones.filter((z) => z.type === "SUPPLY" && z.proximal >= curPrice - 15.0);

  const nearestDemand = demandsBelow.length
    ? demandsBelow.sort((a, b) => Math.abs(curPrice - a.proximal) - Math.abs(curPrice - b.proximal))[0]
    : null;
  const nearestSupply = suppliesAbove.length
    ? suppliesAbove.sort((a, b) => Math.abs(a.proximal - curPrice) - Math.abs(b.proximal - curPrice))[0]
    : null;

  return {
    zones: selected,
    freshDemands,
    freshSupplies,
    nearestDemand,
    nearestSupply,
    currentPrice: curPrice,
    mode,
  };
}

export interface GtfMasterConfluence {
  isDemandMaster: boolean;
  isSupplyMaster: boolean;
  zone: GtfZone | null;
  wall: Wall | null;
  cvdText: string;
  tableText: string;
  bannerText: string;
}

export function evaluateGtfMasterConfluence(
  curPrice: number,
  gtf: GtfResult,
  walls: Wall[],
  cvdDivs: CvdDivergence[],
  fpBars: FpBar[],
  smartRows: SmartRow[]
): GtfMasterConfluence {
  const latestSmart = smartRows.length > 0 ? smartRows[0] : null;
  const latestFp = fpBars.length > 0 ? fpBars[fpBars.length - 1] : null;
  const recentCvdDiv = cvdDivs.slice(-4).reverse().find((d) => latestFp && Math.abs(d.time - latestFp.time) <= 900) ?? null;

  const prevFp3 = fpBars.length >= 4 ? fpBars[fpBars.length - 4] : null;
  const dCvd3 = latestFp && prevFp3 ? latestFp.cvd - prevFp3.cvd : 0;

  const isCvdBull = (
    recentCvdDiv?.type === "BULLISH_ABSORPTION" ||
    (latestFp != null && latestFp.cvd <= -150000 && latestFp.delta > 0) ||
    dCvd3 > 15000 ||
    (latestFp != null && latestFp.delta > 10000)
  );

  const isCvdBear = (
    recentCvdDiv?.type === "BEARISH_DISTRIBUTION" ||
    (latestFp != null && latestFp.cvd >= 150000 && latestFp.delta < 0) ||
    dCvd3 < -15000 ||
    (latestFp != null && latestFp.delta < -10000)
  );

  // Check PE-CE Table Alignment
  const isTableDipBuy = latestSmart?.actionTone === "bull" || latestSmart?.stage === 2 || !!latestSmart?.isPullbackToDemand || !!latestSmart?.isWallShiftBull;
  const isTablePullbackSell = latestSmart?.actionTone === "bear" || latestSmart?.stage === 2 || !!latestSmart?.isPullbackToSupply || !!latestSmart?.isWallShiftBear;

  // 1. Demand Master Confluence: Price inside or touching fresh GTF Demand Zone + CVD Absorption + PE-CE Dip Buy
  const activeDemand = gtf.freshDemands.find((z) => curPrice >= z.bottom - 8 && curPrice <= z.top + 8);
  const peWalls = walls.filter((w) => w.side === "PE");
  const nearestPe = peWalls.sort((a, b) => Math.abs(a.p - curPrice) - Math.abs(b.p - curPrice))[0] ?? null;

  const isDemandMaster = !!activeDemand && isCvdBull && isTableDipBuy;

  // 2. Supply Master Confluence: Price inside fresh GTF Supply Zone (or CE Resistance Wall) + CVD Distribution + PE-CE Pullback Sell
  const activeSupply = gtf.freshSupplies.find((z) => curPrice >= z.bottom - 8 && curPrice <= z.top + 8);
  const ceWalls = walls.filter((w) => w.side === "CE");
  const nearestCe = ceWalls.sort((a, b) => Math.abs(a.p - curPrice) - Math.abs(b.p - curPrice))[0] ?? null;
  const isNearCe = nearestCe != null && Math.abs(nearestCe.p - curPrice) <= 30;

  const isSupplyMaster = (!!activeSupply || isNearCe) && isCvdBear && isTablePullbackSell;

  let bannerText = "";
  if (isDemandMaster) {
    const atm = Math.round(curPrice / 50) * 50;
    bannerText = `🎯 GTF DEMAND ZONE + CVD ABSORPTION: BUY ATM CALL (${atm} CE) @ ${curPrice.toFixed(1)}`;
  } else if (isSupplyMaster) {
    const atm = Math.round(curPrice / 50) * 50;
    const wallLabel = nearestCe ? ` · CE Wall ${nearestCe.p}` : "";
    bannerText = `🎯 GTF SUPPLY ZONE + WALL REJECTION: BUY ATM PUT (${atm} PE) @ ${curPrice.toFixed(1)}${wallLabel}`;
  }

  return {
    isDemandMaster,
    isSupplyMaster,
    zone: isDemandMaster ? (activeDemand ?? null) : isSupplyMaster ? (activeSupply ?? null) : null,
    wall: isDemandMaster ? nearestPe : nearestCe,
    cvdText: isCvdBull ? "CVD Bullish Absorption" : isCvdBear ? "CVD Bearish Distribution" : "CVD Neutral",
    tableText: isTableDipBuy ? "PE-CE Dip Buy Aligned" : isTablePullbackSell ? "PE-CE Pullback Sell Aligned" : "Table Neutral",
    bannerText,
  };
}

export interface GtfExplanation {
  patternExplanation: string;
  statusExplanation: string;
}

/**
 * Returns dynamic institutional copywriting explanations for a GTF zone:
 * - Pattern Type: DBD/RBD (Bearish Distribution) vs RBR/DBR (Bullish Accumulation)
 * - Freshness Status: FRESH (Unmitigated) vs TESTED (1x/2x) vs BROKEN/MITIGATED
 */
export function getGtfExplanation(zone: GtfZone): GtfExplanation {
  let patternExplanation = "";
  if (zone.pattern === "DBD" || zone.pattern === "RBD" || zone.type === "SUPPLY") {
    patternExplanation =
      "Supply Zone (Bearish Distribution). Institutional orders accumulated before a sharp drop. Look to short on retest.";
  } else {
    patternExplanation =
      "Demand Zone (Bullish Accumulation). Institutional limit orders waiting at this floor. Look to buy on pullback.";
  }

  let statusExplanation = "";
  if (zone.status === "FRESH") {
    statusExplanation =
      "Status: Fresh (Unmitigated) — Highest probability reaction zone. Orders remain unfilled.";
  } else if (zone.status === "TESTED") {
    const countStr = zone.testCount > 0 ? `${zone.testCount}x` : "1x";
    statusExplanation = `Status: Tested (${countStr}) — Institutional liquidity partially consumed. Exercise caution; higher risk of breakthrough.`;
  } else {
    statusExplanation =
      "Status: Broken — Zone invalidated by a decisive candle close past distal boundary.";
  }

  return { patternExplanation, statusExplanation };
}

/**
 * Hit-Test Engine for Interactive GTF Zone Hover Tooltips:
 * When cursor is within proximity of a GTF zone box, proximal line, or label,
 * returns the matching GtfZone to render an institutional tooltip card.
 */
export function hitTestGtfZone(
  zones: GtfZone[],
  cursorTime: number | undefined,
  cursorPrice: number | null,
  timeToCoordinate?: (time: number) => number | null,
  priceToCoordinate?: (price: number) => number | null,
  cursorX?: number,
  cursorY?: number,
  lastCandleTime?: number
): GtfZone | null {
  if (!zones || !zones.length || cursorPrice == null) return null;

  const matches: { zone: GtfZone; priority: number }[] = [];

  for (const z of zones) {
    const top = Math.max(z.top, z.bottom);
    const bot = Math.min(z.top, z.bottom);
    const isLive = z.status !== "BROKEN" && z.status !== "MITIGATED";

    // 1. Vertical Hit Test (Price Bounds or Y coordinate bounds)
    let yHit = false;
    if (priceToCoordinate && cursorY != null) {
      const yTop = priceToCoordinate(top);
      const yBot = priceToCoordinate(bot);
      if (yTop != null && yBot != null) {
        // Price coordinates in lightweight-charts: top price has lower y pixel value
        const minY = Math.min(yTop, yBot) - 8;
        const maxY = Math.max(yTop, yBot) + 8;
        yHit = cursorY >= minY && cursorY <= maxY;
      }
    }
    // Fallback using price tolerance (within 5 pts of zone bounds)
    if (!yHit) {
      const pad = 5.0;
      yHit = cursorPrice >= bot - pad && cursorPrice <= top + pad;
    }
    if (!yHit) continue;

    // 2. Horizontal Hit Test (Time Bounds or X coordinate bounds)
    let xHit = false;
    if (timeToCoordinate && cursorX != null) {
      const x1 = timeToCoordinate(z.from);
      const x2 = isLive ? null : timeToCoordinate(z.to);
      if (x1 != null) {
        // From formation bar minus 15px to right edge or end of zone plus 15px
        xHit = cursorX >= x1 - 15 && (x2 == null || cursorX <= x2 + 15);
      }
    }
    if (!xHit && cursorTime != null) {
      const zoneEnd = isLive ? (lastCandleTime ? lastCandleTime + 86400 * 30 : 9999999999) : z.to;
      xHit = cursorTime >= z.from - 600 && cursorTime <= zoneEnd;
    }
    // For live zones, if cursor is to the right of the formation bar
    if (!xHit && isLive && cursorX != null && timeToCoordinate) {
      const x1 = timeToCoordinate(z.from);
      if (x1 != null && cursorX >= x1 - 15) {
        xHit = true;
      }
    }

    if (yHit && xHit) {
      let priority = z.score;
      if (z.status === "FRESH") priority += 20;
      else if (z.status === "TESTED") priority += 10;
      matches.push({ zone: z, priority });
    }
  }

  if (!matches.length) return null;

  // Sort by priority descending (fresh first, highest score, then proximity to proximal entry line)
  matches.sort((a, b) => {
    if (b.priority !== a.priority) return b.priority - a.priority;
    return Math.abs(cursorPrice - a.zone.proximal) - Math.abs(cursorPrice - b.zone.proximal);
  });

  return matches[0].zone;
}
