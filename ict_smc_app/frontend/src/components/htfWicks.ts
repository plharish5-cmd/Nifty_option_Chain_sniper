// htfWicks.ts — Institutional Higher Timeframe (HTF) Wicks & First-Touch Detection
// Tracks prominent rejection wicks on 1H (Hourly ★ 1×), 1D (Daily ★★ 2×), and 1W (Weekly ★★★ 3×).
// Detects when price touches a virgin (untested) HTF wick for the FIRST TIME and ensures it alerts only once.

import type { Candle } from "../api";

export interface HtfWick {
  id: string; // unique ID: e.g. "1H-1792419300-lower"
  tf: "1H" | "1D" | "1W";
  tfName: "Hourly" | "Daily" | "Weekly";
  stars: "★" | "★★" | "★★★";
  weight: 1 | 2 | 3;
  side: "BUY" | "SELL"; // BUY = lower wick (support), SELL = upper wick (resistance)
  top: number;
  bottom: number;
  mid: number;
  candleTime: number;
  wickPts: number;
  ratio: number;
  tested: boolean;
}

const STORAGE_KEY = "ict_htf_wicks_first_touched";

function getTestedSet(): Set<string> {
  try {
    const raw = sessionStorage.getItem(STORAGE_KEY) || localStorage.getItem(STORAGE_KEY) || "[]";
    return new Set(JSON.parse(raw));
  } catch {
    return new Set<string>();
  }
}

export function isWickUntested(id: string): boolean {
  return !getTestedSet().has(id);
}

export function markWickTested(id: string): void {
  try {
    const set = getTestedSet();
    set.add(id);
    const arr = [...set].slice(-500);
    sessionStorage.setItem(STORAGE_KEY, JSON.stringify(arr));
    localStorage.setItem(STORAGE_KEY, JSON.stringify(arr));
  } catch {
    // Ignore storage quota errors
  }
}

/** Aggregate daily candles into weekly candles (Monday to Friday trading week) */
export function aggregateWeeklyCandles(daily: Candle[]): Candle[] {
  if (!daily || !daily.length) return [];
  const weeks = new Map<string, Candle[]>();
  for (const c of daily) {
    const dt = new Date(c.time * 1000);
    // Determine Monday of this week in UTC
    const day = dt.getUTCDay();
    const diff = (day === 0 ? -6 : 1) - day;
    const mon = new Date(dt);
    mon.setUTCDate(dt.getUTCDate() + diff);
    const key = mon.toISOString().slice(0, 10);
    if (!weeks.has(key)) weeks.set(key, []);
    weeks.get(key)!.push(c);
  }
  const weekly: Candle[] = [];
  const sortedKeys = [...weeks.keys()].sort();
  for (let i = 0; i < sortedKeys.length; i++) {
    const key = sortedKeys[i];
    const days = weeks.get(key)!;
    days.sort((a, b) => a.time - b.time);
    const open = days[0].open;
    const close = days[days.length - 1].close;
    let high = -Infinity;
    let low = Infinity;
    for (const d of days) {
      if (d.high > high) high = d.high;
      if (d.low < low) low = d.low;
    }
    const isCurrentWeek = i === sortedKeys.length - 1;
    weekly.push({
      time: days[0].time,
      open,
      high,
      low,
      close,
      closed: !isCurrentWeek || days.length >= 5,
    });
  }
  return weekly.sort((a, b) => a.time - b.time);
}

/** Extract prominent institutional wicks from a series of candles */
export function extractWicks(candles: Candle[], tf: "1H" | "1D" | "1W"): HtfWick[] {
  if (!candles || candles.length < 2) return [];
  const out: HtfWick[] = [];

  // Thresholds per timeframe
  const minRng = tf === "1H" ? 12 : tf === "1D" ? 35 : 75;
  const minWick = tf === "1H" ? 6 : tf === "1D" ? 15 : 30;
  const minRatio = tf === "1H" ? 0.22 : tf === "1D" ? 0.18 : 0.18;
  const stars = tf === "1H" ? "★" : tf === "1D" ? "★★" : "★★★";
  const weight = tf === "1H" ? 1 : tf === "1D" ? 2 : 3;
  const tfName = tf === "1H" ? "Hourly" : tf === "1D" ? "Daily" : "Weekly";

  for (const c of candles) {
    if (c.closed === false) continue;
    const rng = c.high - c.low;
    if (rng < minRng) continue;

    const bodyTop = Math.max(c.open, c.close);
    const bodyBottom = Math.min(c.open, c.close);

    // Upper wick (Resistance / Sellers' rejection)
    const upperWick = c.high - bodyTop;
    if (upperWick >= minWick && upperWick / rng >= minRatio) {
      const top = c.high;
      const bottom = bodyTop;
      out.push({
        id: `${tf}-${c.time}-upper`,
        tf,
        tfName,
        stars,
        weight,
        side: "SELL",
        top,
        bottom,
        mid: (top + bottom) / 2,
        candleTime: c.time,
        wickPts: upperWick,
        ratio: upperWick / rng,
        tested: false,
      });
    }

    // Lower wick (Support / Buyers' rejection)
    const lowerWick = bodyBottom - c.low;
    if (lowerWick >= minWick && lowerWick / rng >= minRatio) {
      const top = bodyBottom;
      const bottom = c.low;
      out.push({
        id: `${tf}-${c.time}-lower`,
        tf,
        tfName,
        stars,
        weight,
        side: "BUY",
        top,
        bottom,
        mid: (top + bottom) / 2,
        candleTime: c.time,
        wickPts: lowerWick,
        ratio: lowerWick / rng,
        tested: false,
      });
    }
  }
  return out;
}

/** Check if historical candles after wick formation have already touched it */
export function filterVirginWicks(wicks: HtfWick[], barsAfter: Candle[]): HtfWick[] {
  if (!wicks.length || !barsAfter.length) return wicks;
  return wicks.map((w) => {
    // Look at candles strictly after the wick formation candle
    const subsequent = barsAfter.filter((b) => b.time > w.candleTime && b.closed !== false);
    // Did any candle prior to the latest candle touch this wick?
    const alreadyTested = subsequent.slice(0, -1).some((b) => b.low <= w.top && b.high >= w.bottom);
    return { ...w, tested: alreadyTested };
  });
}
