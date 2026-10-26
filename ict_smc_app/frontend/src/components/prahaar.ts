// prahaar.ts — Prahaar Indicator & ATM Option-Buying Directional Engine.
// Combines EMA trend alignment, MACD momentum, ADX trend strength, and India VIX filters.
// Confluence integration with OI Walls and CVD Order Flow for 🎯 A+ Master Sniper signals.

import type { Candle } from "../api";
import type { Wall, CvdDivergence } from "./orderflow";
import type { FpBar } from "./footprint";

export interface PrahaarConfig {
  fastEmaLen?: number;
  slowEmaLen?: number;
  macdFast?: number;
  macdSlow?: number;
  macdSignal?: number;
  adxPeriod?: number;
  adxThreshold?: number;
  adxImpulseThreshold?: number;
  adxImpulseDxMin?: number;
  adxImpulseDiSpreadMin?: number;
  adxImpulseAtrMult?: number;
  vixFilterEnabled?: boolean;
  vixMin?: number;
  vixMax?: number;
  vixDropMax?: number;
  strikeStep?: number;
  slAtrMult?: number;
  tpAtrMult?: number;
}

export interface PrahaarSignal {
  time: number;
  candleIdx: number;
  timeStr: string;
  dateStr?: string;
  action: "BUY ATM CALL" | "BUY ATM PUT";
  dir: "bull" | "bear";
  strike: number;
  optionType: "CE" | "PE";
  optionSymbol: string;
  entry: number;
  sl: number;
  tp: number;
  rr: number;
  adx: number;
  dx?: number;
  diSpread?: number;
  isImpulseActive?: boolean;
  macd: number;
  macdSignal: number;
  macdHist: number;
  fastEma: number;
  slowEma: number;
  vix?: number | null;
  vixOk: boolean;
  vixReason: string;
  isMasterSniper: boolean;
  masterSniperReason?: string;
  reasons: string[];
}

export interface PrahaarLatestState {
  close: number;
  atmStrike: number;
  fastEma: number;
  slowEma: number;
  emaTrend: "bullish" | "bearish" | "neutral";
  macd: number;
  macdSignal: number;
  macdHist: number;
  macdSlope: number;
  macdMomentum: "bullish" | "bearish" | "neutral";
  adx: number;
  adxSlope: number;
  adxTrending: boolean;
  isImpulseActive: boolean;
  dx: number;
  diSpread: number;
  plusDi: number;
  minusDi: number;
  vix: number | null;
  vixOk: boolean;
  vixReason: string;
  currentVerdict: "BUY ATM CALL" | "BUY ATM PUT" | "NEUTRAL";
  isMasterSniperCall: boolean;
  isMasterSniperPut: boolean;
  masterSniperReason?: string;
}

export interface PrahaarResult {
  signals: PrahaarSignal[];
  latestState: PrahaarLatestState | null;
  config: Required<PrahaarConfig>;
}

export function computeEma(values: number[], period: number): number[] {
  const n = values.length;
  if (!n) return [];
  const out = new Array<number>(n);
  const alpha = 2 / (period + 1);
  out[0] = values[0];
  for (let i = 1; i < n; i++) {
    out[i] = alpha * values[i] + (1 - alpha) * out[i - 1];
  }
  return out;
}

export function computeMacd(
  closes: number[],
  fast = 12,
  slow = 26,
  signal = 9
): { macd: number[]; signal: number[]; hist: number[] } {
  const fastEma = computeEma(closes, fast);
  const slowEma = computeEma(closes, slow);
  const n = closes.length;
  const macd = new Array<number>(n);
  for (let i = 0; i < n; i++) macd[i] = fastEma[i] - slowEma[i];
  const sig = computeEma(macd, signal);
  const hist = new Array<number>(n);
  for (let i = 0; i < n; i++) hist[i] = macd[i] - sig[i];
  return { macd, signal: sig, hist };
}

export function computeAdx(
  highs: number[],
  lows: number[],
  closes: number[],
  period = 14
): { adx: number[]; plusDi: number[]; minusDi: number[] } {
  const n = closes.length;
  const adx = new Array<number>(n).fill(0);
  const plusDi = new Array<number>(n).fill(0);
  const minusDi = new Array<number>(n).fill(0);
  if (n < period + 1) return { adx, plusDi, minusDi };

  const tr = new Array<number>(n);
  tr[0] = highs[0] - lows[0];
  const plusDm = new Array<number>(n).fill(0);
  const minusDm = new Array<number>(n).fill(0);

  for (let i = 1; i < n; i++) {
    const prevC = closes[i - 1];
    tr[i] = Math.max(highs[i] - lows[i], Math.abs(highs[i] - prevC), Math.abs(lows[i] - prevC));
    const up = highs[i] - highs[i - 1];
    const down = lows[i - 1] - lows[i];
    plusDm[i] = up > down && up > 0 ? up : 0;
    minusDm[i] = down > up && down > 0 ? down : 0;
  }

  let sTr = 0;
  let sPdm = 0;
  let sMdm = 0;
  for (let i = 1; i <= period; i++) {
    sTr += tr[i];
    sPdm += plusDm[i];
    sMdm += minusDm[i];
  }

  const dx = new Array<number>(n).fill(0);
  const calcDx = (i: number, strVal: number, sp: number, sm: number) => {
    const safeTr = strVal > 1e-4 ? strVal : 1e-4;
    const p = 100 * (sp / safeTr);
    const m = 100 * (sm / safeTr);
    plusDi[i] = p;
    minusDi[i] = m;
    const sum = p + m;
    return sum > 1e-4 ? (100 * Math.abs(p - m)) / sum : 0;
  };

  dx[period] = calcDx(period, sTr, sPdm, sMdm);

  for (let i = period + 1; i < n; i++) {
    sTr = sTr - sTr / period + tr[i];
    sPdm = sPdm - sPdm / period + plusDm[i];
    sMdm = sMdm - sMdm / period + minusDm[i];
    dx[i] = calcDx(i, sTr, sPdm, sMdm);
  }

  if (n >= 2 * period) {
    let sumDx = 0;
    for (let i = period; i < 2 * period; i++) sumDx += dx[i];
    adx[2 * period - 1] = sumDx / period;
    for (let i = 2 * period; i < n; i++) {
      adx[i] = (adx[i - 1] * (period - 1) + dx[i]) / period;
    }
  } else {
    for (let i = period; i < n; i++) {
      let sum = 0;
      for (let k = period; k <= i; k++) sum += dx[k];
      adx[i] = sum / (i - period + 1);
    }
  }

  return { adx, plusDi, minusDi };
}

export function checkVixContext(
  vix?: number | null,
  vixChg?: number | null,
  cfg?: { enabled: boolean; min: number; max: number; dropMax: number }
): { ok: boolean; reason: string } {
  if (!cfg?.enabled) return { ok: true, reason: "VIX filter toggled off" };
  if (vix == null || !isFinite(vix)) return { ok: true, reason: "VIX not active (bypassed)" };
  if (vix < cfg.min) return { ok: false, reason: `VIX ${vix.toFixed(1)} < ${cfg.min} (low volatility)` };
  if (vix > cfg.max) return { ok: false, reason: `VIX ${vix.toFixed(1)} > ${cfg.max} (extreme crush risk)` };
  if (vixChg != null && vixChg < cfg.dropMax) return { ok: false, reason: `VIX dropping (${vixChg > 0 ? "+" : ""}${vixChg.toFixed(1)})` };
  return { ok: true, reason: `VIX ${vix.toFixed(1)} supportive` };
}

export function roundAtmStrike(price: number, step = 50): number {
  return Math.round(price / step) * step;
}

export function computePrahaar(
  candles: Candle[],
  vix?: number | null,
  vixChg?: number | null,
  walls: Wall[] = [],
  cvdDivs: CvdDivergence[] = [],
  fpBars: FpBar[] = [],
  customCfg?: PrahaarConfig
): PrahaarResult {
  const config: Required<PrahaarConfig> = {
    fastEmaLen: customCfg?.fastEmaLen ?? 9,
    slowEmaLen: customCfg?.slowEmaLen ?? 21,
    macdFast: customCfg?.macdFast ?? 12,
    macdSlow: customCfg?.macdSlow ?? 26,
    macdSignal: customCfg?.macdSignal ?? 9,
    adxPeriod: customCfg?.adxPeriod ?? 14,
    adxThreshold: customCfg?.adxThreshold ?? 20.0,
    adxImpulseThreshold: customCfg?.adxImpulseThreshold ?? 18.0,
    adxImpulseDxMin: customCfg?.adxImpulseDxMin ?? 25.0,
    adxImpulseDiSpreadMin: customCfg?.adxImpulseDiSpreadMin ?? 12.0,
    adxImpulseAtrMult: customCfg?.adxImpulseAtrMult ?? 1.3,
    vixFilterEnabled: customCfg?.vixFilterEnabled ?? true,
    vixMin: customCfg?.vixMin ?? 10.0,
    vixMax: customCfg?.vixMax ?? 28.0,
    vixDropMax: customCfg?.vixDropMax ?? -1.5,
    strikeStep: customCfg?.strikeStep ?? 50,
    slAtrMult: customCfg?.slAtrMult ?? 1.5,
    tpAtrMult: customCfg?.tpAtrMult ?? 3.0,
  };

  const closed = candles.filter((c) => c.closed !== false);
  const n = closed.length;
  if (n < 15) {
    return { signals: [], latestState: null, config };
  }

  const closes = closed.map((c) => c.close);
  const highs = closed.map((c) => c.high);
  const lows = closed.map((c) => c.low);

  const fastEma = computeEma(closes, config.fastEmaLen);
  const slowEma = computeEma(closes, config.slowEmaLen);
  const { macd, signal, hist } = computeMacd(closes, config.macdFast, config.macdSlow, config.macdSignal);
  const { adx, plusDi, minusDi } = computeAdx(highs, lows, closes, config.adxPeriod);

  // ATR 14
  const effAtr = new Array<number>(n);
  const tr = new Array<number>(n);
  tr[0] = highs[0] - lows[0];
  for (let k = 1; k < n; k++) {
    const pc = closes[k - 1];
    tr[k] = Math.max(highs[k] - lows[k], Math.abs(highs[k] - pc), Math.abs(lows[k] - pc));
  }
  for (let k = 0; k < n; k++) {
    let sumTr = 0;
    const start = Math.max(0, k - 13);
    for (let j = start; j <= k; j++) sumTr += tr[j];
    effAtr[k] = Math.max(10, sumTr / (k - start + 1));
  }

  const vixVerdict = checkVixContext(vix, vixChg, {
    enabled: config.vixFilterEnabled,
    min: config.vixMin,
    max: config.vixMax,
    dropMax: config.vixDropMax,
  });

  const signals: PrahaarSignal[] = [];
  let lastSignalIdx = -999;
  let lastSignalDir = "";

  for (let i = 1; i < n; i++) {
    const c = closes[i];
    const cBar = closed[i];
    const fEma = fastEma[i];
    const sEma = slowEma[i];
    const mVal = macd[i];
    const sVal = signal[i];
    const hVal = hist[i];
    const prevH = hist[i - 1];
    const curAdx = adx[i];
    const prevAdx = adx[i - 1];
    const adxSlope = curAdx - prevAdx;
    const pDi = plusDi[i];
    const mDi = minusDi[i];
    const diSum = pDi + mDi;
    const curDx = diSum > 1e-4 ? (100 * Math.abs(pDi - mDi)) / diSum : 0;
    const diSpread = Math.abs(mDi - pDi);
    const curAtr = effAtr[i];

    const cRange = highs[i] - lows[i];
    const cBody = Math.abs(c - cBar.open);
    const expansionCandle = cRange >= config.adxImpulseAtrMult * curAtr || cBody >= config.adxImpulseAtrMult * curAtr;

    const emaBull = fEma > sEma && c >= fEma;
    const emaBear = fEma < sEma && c <= fEma;

    // Impulse Expansion Qualifying Gate
    const impulseGate1 = curAdx >= config.adxImpulseThreshold && adxSlope > 0;
    const impulseGate2 = (curDx >= config.adxImpulseDxMin || diSpread >= config.adxImpulseDiSpreadMin) && expansionCandle;
    const isImpulseQualified = impulseGate1 || impulseGate2;

    const adxActive = curAdx >= config.adxThreshold || isImpulseQualified;
    const adxBull = adxActive && pDi >= mDi;
    const adxBear = adxActive && mDi >= pDi;

    // MACD crossover in last 4 bars or histogram expanding
    let macdCrossBull = false;
    let macdCrossBear = false;
    for (let k = Math.max(1, i - 4); k <= i; k++) {
      if (macd[k] >= signal[k] && macd[k - 1] < signal[k - 1]) macdCrossBull = true;
      if (hist[k] > 0 && hist[k - 1] <= 0) macdCrossBull = true;
      if (macd[k] <= signal[k] && macd[k - 1] > signal[k - 1]) macdCrossBear = true;
      if (hist[k] < 0 && hist[k - 1] >= 0) macdCrossBear = true;
    }

    const adxJustTrending = (curAdx >= config.adxThreshold && (i === 1 || adx[i - 1] < config.adxThreshold)) || isImpulseQualified;
    if (!macdCrossBull && mVal > sVal && hVal > 0 && (adxJustTrending || hVal > prevH)) {
      macdCrossBull = true;
    }
    if (!macdCrossBear && mVal < sVal && hVal < 0 && (adxJustTrending || hVal < prevH)) {
      macdCrossBear = true;
    }

    const isBull = emaBull && macdCrossBull && adxBull && vixVerdict.ok;
    const isBear = emaBear && macdCrossBear && adxBear && vixVerdict.ok;

    if (isBull && (i - lastSignalIdx >= 3 || lastSignalDir !== "bull")) {
      const strike = roundAtmStrike(c, config.strikeStep);
      const estAtr = curAtr;
      const sl = c - config.slAtrMult * estAtr;
      const tp = c + config.tpAtrMult * estAtr;
      const rr = (tp - c) / Math.max(c - sl, 1);
      const d = new Date(cBar.time * 1000);
      const tStr = `${String(d.getUTCHours()).padStart(2, "0")}:${String(d.getUTCMinutes()).padStart(2, "0")}`;
      const lastBar = closed[n - 1];
      const lastD = new Date(lastBar.time * 1000);
      const isToday = d.getUTCFullYear() === lastD.getUTCFullYear() &&
                      d.getUTCMonth() === lastD.getUTCMonth() &&
                      d.getUTCDate() === lastD.getUTCDate();
      const months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
      const dateStr = isToday ? `Today ${tStr}` : `${d.getUTCDate()} ${months[d.getUTCMonth()]} ${tStr}`;

      // Confluence Check for A+ Master Sniper Elevation
      const peWalls = walls.filter((w) => w.side === "PE");
      const nearestPe = peWalls.sort((a, b) => Math.abs(a.p - c) - Math.abs(b.p - c))[0] ?? null;
      const peDist = nearestPe ? Math.abs(nearestPe.p - c) : 999;
      const isNearPe = nearestPe != null && peDist <= 30;

      const recentDiv = cvdDivs.slice(-4).reverse().find((d2) => Math.abs(d2.time - cBar.time) <= 900) ?? null;
      const latestFp = fpBars.find((f) => Math.abs(f.time - cBar.time) <= 300);
      const isCvdBull = recentDiv?.type === "BULLISH_ABSORPTION" || (latestFp != null && latestFp.delta > 10000);

      const isMasterSniper = isNearPe && isCvdBull;
      const masterReason = isMasterSniper
        ? `🎯 A+ MASTER SNIPER: BUY ${strike} CE (Prahaar + PE Wall ${nearestPe?.p} Support Bounce + Bullish CVD Absorption)`
        : undefined;

      const adxReason = curAdx >= config.adxThreshold
        ? `ADX Trend Strength (${curAdx.toFixed(1)} >= ${config.adxThreshold})`
        : `⚡ ADX Impulse Active (${curAdx.toFixed(1)} [slope ${adxSlope > 0 ? "+" : ""}${adxSlope.toFixed(2)}], DX ${curDx.toFixed(1)})`;

      signals.push({
        time: cBar.time,
        candleIdx: i,
        timeStr: tStr,
        dateStr,
        action: "BUY ATM CALL",
        dir: "bull",
        strike,
        optionType: "CE",
        optionSymbol: `${strike} CE`,
        entry: c,
        sl,
        tp,
        rr,
        adx: curAdx,
        dx: curDx,
        diSpread,
        isImpulseActive: isImpulseQualified,
        macd: mVal,
        macdSignal: sVal,
        macdHist: hVal,
        fastEma: fEma,
        slowEma: sEma,
        vix,
        vixOk: vixVerdict.ok,
        vixReason: vixVerdict.reason,
        isMasterSniper,
        masterSniperReason: masterReason,
        reasons: [
          `EMA ${config.fastEmaLen} > ${config.slowEmaLen} Bullish Trend (${fEma.toFixed(1)} > ${sEma.toFixed(1)})`,
          `MACD Positive Momentum (${mVal.toFixed(2)} > ${sVal.toFixed(2)}, Hist ${hVal > 0 ? "+" : ""}${hVal.toFixed(2)})`,
          adxReason,
          vixVerdict.reason,
        ],
      });
      lastSignalIdx = i;
      lastSignalDir = "bull";
    } else if (isBear && (i - lastSignalIdx >= 3 || lastSignalDir !== "bear")) {
      const strike = roundAtmStrike(c, config.strikeStep);
      const estAtr = curAtr;
      const sl = c + config.slAtrMult * estAtr;
      const tp = c - config.tpAtrMult * estAtr;
      const rr = (c - tp) / Math.max(sl - c, 1);
      const d = new Date(cBar.time * 1000);
      const tStr = `${String(d.getUTCHours()).padStart(2, "0")}:${String(d.getUTCMinutes()).padStart(2, "0")}`;
      const lastBar = closed[n - 1];
      const lastD = new Date(lastBar.time * 1000);
      const isToday = d.getUTCFullYear() === lastD.getUTCFullYear() &&
                      d.getUTCMonth() === lastD.getUTCMonth() &&
                      d.getUTCDate() === lastD.getUTCDate();
      const months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
      const dateStr = isToday ? `Today ${tStr}` : `${d.getUTCDate()} ${months[d.getUTCMonth()]} ${tStr}`;

      // Confluence Check for A+ Master Sniper Elevation
      const ceWalls = walls.filter((w) => w.side === "CE");
      const nearestCe = ceWalls.sort((a, b) => Math.abs(a.p - c) - Math.abs(b.p - c))[0] ?? null;
      const ceDist = nearestCe ? Math.abs(nearestCe.p - c) : 999;
      const isNearCe = nearestCe != null && ceDist <= 30;

      const recentDiv = cvdDivs.slice(-4).reverse().find((d2) => Math.abs(d2.time - cBar.time) <= 900) ?? null;
      const latestFp = fpBars.find((f) => Math.abs(f.time - cBar.time) <= 300);
      const isCvdBear = recentDiv?.type === "BEARISH_DISTRIBUTION" || (latestFp != null && latestFp.delta < -10000);

      const isMasterSniper = isNearCe && isCvdBear;
      const masterReason = isMasterSniper
        ? `🎯 A+ MASTER SNIPER: BUY ${strike} PE (Prahaar + CE Wall ${nearestCe?.p} Rejection + Bearish CVD Distribution)`
        : undefined;

      const adxReason = curAdx >= config.adxThreshold
        ? `ADX Trend Strength (${curAdx.toFixed(1)} >= ${config.adxThreshold})`
        : `⚡ ADX Impulse Active (${curAdx.toFixed(1)} [slope ${adxSlope > 0 ? "+" : ""}${adxSlope.toFixed(2)}], DX ${curDx.toFixed(1)})`;

      signals.push({
        time: cBar.time,
        candleIdx: i,
        timeStr: tStr,
        dateStr,
        action: "BUY ATM PUT",
        dir: "bear",
        strike,
        optionType: "PE",
        optionSymbol: `${strike} PE`,
        entry: c,
        sl,
        tp,
        rr,
        adx: curAdx,
        dx: curDx,
        diSpread,
        isImpulseActive: isImpulseQualified,
        macd: mVal,
        macdSignal: sVal,
        macdHist: hVal,
        fastEma: fEma,
        slowEma: sEma,
        vix,
        vixOk: vixVerdict.ok,
        vixReason: vixVerdict.reason,
        isMasterSniper,
        masterSniperReason: masterReason,
        reasons: [
          `EMA ${config.fastEmaLen} < ${config.slowEmaLen} Bearish Trend (${fEma.toFixed(1)} < ${sEma.toFixed(1)})`,
          `MACD Negative Momentum (${mVal.toFixed(2)} < ${sVal.toFixed(2)}, Hist ${hVal.toFixed(2)})`,
          adxReason,
          vixVerdict.reason,
        ],
      });
      lastSignalIdx = i;
      lastSignalDir = "bear";
    }
  }

  // Sort signals strictly by full Unix timestamp (descending)
  signals.sort((a, b) => Number(b.time) - Number(a.time));

  // Diagnostic state for the latest closed candle
  const lastC = closes[n - 1];
  const lastAtm = roundAtmStrike(lastC, config.strikeStep);
  const lastF = fastEma[n - 1];
  const lastS = slowEma[n - 1];
  const lastM = macd[n - 1];
  const lastSig = signal[n - 1];
  const lastH = hist[n - 1];
  const prevH = n >= 2 ? hist[n - 2] : lastH;
  const lastAdx = adx[n - 1];
  const prevAdx = n >= 2 ? adx[n - 2] : lastAdx;
  const lastAdxSlope = lastAdx - prevAdx;
  const lastPdi = plusDi[n - 1];
  const lastMdi = minusDi[n - 1];
  const lastDiSum = lastPdi + lastMdi;
  const lastDx = lastDiSum > 1e-4 ? (100 * Math.abs(lastPdi - lastMdi)) / lastDiSum : 0;
  const lastDiSpread = Math.abs(lastMdi - lastPdi);
  const lastAtr = effAtr[n - 1];
  const lastCRange = highs[n - 1] - lows[n - 1];
  const lastCBody = Math.abs(lastC - closed[n - 1].open);
  const lastExpansion = lastCRange >= config.adxImpulseAtrMult * lastAtr || lastCBody >= config.adxImpulseAtrMult * lastAtr;

  const lastImpulse1 = lastAdx >= config.adxImpulseThreshold && lastAdxSlope > 0;
  const lastImpulse2 = (lastDx >= config.adxImpulseDxMin || lastDiSpread >= config.adxImpulseDiSpreadMin) && lastExpansion;
  const lastIsImpulseActive = lastImpulse1 || lastImpulse2;

  const trend: "bullish" | "bearish" | "neutral" = lastF > lastS ? "bullish" : lastF < lastS ? "bearish" : "neutral";
  const momentum: "bullish" | "bearish" | "neutral" = lastM > lastSig ? "bullish" : lastM < lastSig ? "bearish" : "neutral";
  const adxTrending = lastAdx >= config.adxThreshold || lastIsImpulseActive;

  let currentVerdict: "BUY ATM CALL" | "BUY ATM PUT" | "NEUTRAL" = "NEUTRAL";
  if (trend === "bullish" && momentum === "bullish" && adxTrending && lastPdi > lastMdi && vixVerdict.ok) {
    currentVerdict = "BUY ATM CALL";
  } else if (trend === "bearish" && momentum === "bearish" && adxTrending && lastMdi > lastPdi && vixVerdict.ok) {
    currentVerdict = "BUY ATM PUT";
  }

  // Live Confluence Synergy on latest candle
  const ceWalls = walls.filter((w) => w.side === "CE");
  const nearestCe = ceWalls.sort((a, b) => Math.abs(a.p - lastC) - Math.abs(b.p - lastC))[0] ?? null;
  const isNearCe = nearestCe != null && Math.abs(nearestCe.p - lastC) <= 30;

  const peWalls = walls.filter((w) => w.side === "PE");
  const nearestPe = peWalls.sort((a, b) => Math.abs(a.p - lastC) - Math.abs(b.p - lastC))[0] ?? null;
  const isNearPe = nearestPe != null && Math.abs(nearestPe.p - lastC) <= 30;

  const lastBar = closed[n - 1];
  const recentDiv = cvdDivs.slice(-4).reverse().find((d) => Math.abs(d.time - lastBar.time) <= 900) ?? null;
  const latestFp = fpBars[fpBars.length - 1];
  const isCvdBear = recentDiv?.type === "BEARISH_DISTRIBUTION" || (latestFp != null && latestFp.delta < -10000);
  const isCvdBull = recentDiv?.type === "BULLISH_ABSORPTION" || (latestFp != null && latestFp.delta > 10000);

  const isMasterSniperPut = currentVerdict === "BUY ATM PUT" && isNearCe && isCvdBear;
  const isMasterSniperCall = currentVerdict === "BUY ATM CALL" && isNearPe && isCvdBull;

  const masterSniperReason = isMasterSniperPut
    ? `🎯 A+ MASTER SNIPER: BUY ${lastAtm} PE (Prahaar + CE Resistance Wall ${nearestCe?.p} + Bearish CVD Distribution)`
    : isMasterSniperCall
    ? `🎯 A+ MASTER SNIPER: BUY ${lastAtm} CE (Prahaar + PE Support Wall ${nearestPe?.p} + Bullish CVD Absorption)`
    : undefined;

  const latestState: PrahaarLatestState = {
    close: lastC,
    atmStrike: lastAtm,
    fastEma: lastF,
    slowEma: lastS,
    emaTrend: trend,
    macd: lastM,
    macdSignal: lastSig,
    macdHist: lastH,
    macdSlope: lastH - prevH,
    macdMomentum: momentum,
    adx: lastAdx,
    adxSlope: lastAdxSlope,
    adxTrending,
    isImpulseActive: lastIsImpulseActive,
    dx: lastDx,
    diSpread: lastDiSpread,
    plusDi: lastPdi,
    minusDi: lastMdi,
    vix: vix ?? null,
    vixOk: vixVerdict.ok,
    vixReason: vixVerdict.reason,
    currentVerdict,
    isMasterSniperCall,
    isMasterSniperPut,
    masterSniperReason,
  };

  return { signals, latestState, config };
}
