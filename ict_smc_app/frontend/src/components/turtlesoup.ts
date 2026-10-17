// turtlesoup.ts — Turtle Soup (ICT): a FALSE break of a key level that reclaims = stop-run reversal.
//   Bearish TS: a candle WICKS above a key high (PDH / day-high / OR-high / BSL pool) but CLOSES back below it
//               → buy-side liquidity swept, breakout failed → reversal DOWN (sell the reclaim).
//   Bullish TS: a candle WICKS below a key low (PDL / day-low / OR-low / SSL pool) but CLOSES back above it
//               → sell-side liquidity swept, breakdown failed → reversal UP (buy the reclaim).
// Closed candles only. One marker per (level, direction) — the FIRST clean false-break is the signal.
import type { Analysis, Candle } from "../api";

export type TSoup = { time: number; dir: "bull" | "bear"; level: number; name: string; wick: number; close: number };

export function turtleSoup(candles: Candle[], a: Analysis | null): TSoup[] {
  if (!a) return [];
  const closed = candles.filter((c) => c.closed !== false);
  if (!closed.length) return [];
  const last = closed[closed.length - 1].close;
  const atr = a.atr || 20;
  const minPen = Math.max(2, atr * 0.05);                       // wick must clear the level by a real margin, not a tick
  const near = (p: number) => Math.abs(p - last) <= last * 0.02; // only levels within ~2 % of price matter today

  const lv: { p: number; name: string; high: boolean }[] = [];
  const s = a.session;
  const add = (p: number | undefined, name: string, high: boolean) => { if (p && isFinite(p) && near(p)) lv.push({ p, name, high }); };
  add(s?.prev?.h, "PDH", true); add(s?.prev?.l, "PDL", false);
  add(s?.high, "day high", true); add(s?.low, "day low", false);
  add(s?.or?.h, "OR high", true); add(s?.or?.l, "OR low", false);
  for (const pool of a.pools ?? []) add(pool.price, pool.side === "BSL" ? "BSL" : "SSL", pool.side === "BSL");

  const best = new Map<string, TSoup>();                        // keep the most recent per (level, dir)
  for (const c of closed) {
    for (const L of lv) {
      if (L.high && c.high > L.p + minPen && c.close < L.p) {
        best.set(`${L.name}|bear`, { time: c.time, dir: "bear", level: L.p, name: L.name, wick: c.high, close: c.close });
      } else if (!L.high && c.low < L.p - minPen && c.close > L.p) {
        best.set(`${L.name}|bull`, { time: c.time, dir: "bull", level: L.p, name: L.name, wick: c.low, close: c.close });
      }
    }
  }
  return [...best.values()].sort((x, y) => x.time - y.time);
}
