// powerofthree.ts — Power of Three / AMD daily profile (ICT).
//   Accumulation  — the opening range (first ~30 min): price coils near the open, builds orders.
//   Manipulation  — the "Judas swing": the first push that SWEEPS the opening-range high or low (a fake move
//                    that runs stops) before the real move. The Judas direction is the FALSE one.
//   Distribution  — the real trend, usually OPPOSITE the Judas once price reclaims the opening range.
// Intraday, today only. Times are IST-shifted epoch seconds (09:15 open).
import type { Analysis, Candle } from "../api";

export type P3 = {
  open: number; orHigh: number; orLow: number; orFrom: number; orTo: number;
  judas: { time: number; dir: "up" | "down"; price: number } | null;
  distribution: "bull" | "bear" | null;        // expected real-move direction (opposite the Judas)
  reclaimed: boolean;                            // price closed back inside the OR after the Judas (confirmation)
};

const OR_MINUTES = 30;

export function powerOfThree(candles: Candle[], a: Analysis | null): P3 | null {
  const closed = candles.filter((c) => c.closed !== false);
  if (!closed.length) return null;
  const lastDay = Math.floor(closed[closed.length - 1].time / 86400);
  const day = closed.filter((c) => Math.floor(c.time / 86400) === lastDay);
  if (day.length < 2) return null;
  const sessOpen = lastDay * 86400 + 9 * 3600 + 15 * 60;        // 09:15 IST
  const orTo = sessOpen + OR_MINUTES * 60;
  const orBars = day.filter((c) => c.time < orTo);
  if (!orBars.length) return null;
  const open = a?.session?.open ?? orBars[0].open;
  const orHigh = Math.max(...orBars.map((c) => c.high));
  const orLow = Math.min(...orBars.map((c) => c.low));
  const orFrom = orBars[0].time;

  let judas: P3["judas"] = null;
  let reclaimed = false;
  for (const c of day) {
    if (c.time < orTo) continue;                                 // Judas happens AFTER the opening range
    if (!judas) {
      if (c.high > orHigh) { judas = { time: c.time, dir: "up", price: c.high }; }
      else if (c.low < orLow) { judas = { time: c.time, dir: "down", price: c.low }; }
    } else {
      // reclaim = price closes back inside the OR on the opposite side of the Judas → distribution confirmed
      if (judas.dir === "up" && c.close < orHigh) reclaimed = true;
      if (judas.dir === "down" && c.close > orLow) reclaimed = true;
    }
  }
  const distribution: P3["distribution"] = judas ? (judas.dir === "up" ? "bear" : "bull") : null;
  return { open, orHigh, orLow, orFrom, orTo, judas, distribution, reclaimed };
}
