// dol.ts — Draw on Liquidity (ICT IRL ⇄ ERL), closed candles only, information only.
//   ERL taken (liquidity sweep / CRT TBS / CRT entry)            → next D.O.L = nearest unfilled FVG (IRL) in that direction
//   IRL tapped (candle into a live FVG, closed back out of it)   → next D.O.L = nearest old high / low (ERL) beyond
// A D.O.L stays active until price reaches it (✓ hit) or a newer event replaces it.
import type { Analysis, Candle } from "../api";

export type Dol = { from: number; dir: "bull" | "bear"; kind: "IRL" | "ERL"; lo: number; hi: number; label: string;
  why: string; hit?: number };

export function computeDol(candles: Candle[], a: Analysis | null): Dol[] {
  if (!a) return [];
  const closed = candles.filter((c) => c.closed !== false);
  const zones = (a.zones ?? []).filter((z) => z.type === "FVG" || z.type === "IFVG");
  const pools = a.pools ?? [];
  const hm = (t: number) => { const d = new Date(t * 1000); return `${String(d.getUTCHours()).padStart(2, "0")}:${String(d.getUTCMinutes()).padStart(2, "0")}`; };
  const erlEv = new Map<number, { dir: "bull" | "bear"; what: string }>();
  for (const m of a.markers ?? []) if (m.kind === "SWEEP") erlEv.set(m.time, { dir: m.dir, what: "liquidity sweep" });
  for (const m of a.crt?.markers ?? []) if (m.kind === "CRT_TBS" || m.kind === "CRT_ENTRY") erlEv.set(m.time, { dir: m.dir, what: m.kind === "CRT_TBS" ? "CRT TBS" : "CRT entry" });
  const out: Dol[] = [];
  let cur: Dol | null = null;
  const alive = (z: { from: number; to: number }, t: number) => z.from < t && z.to >= t;
  for (const c of closed) {
    // 1. is the active D.O.L reached?
    if (cur && !cur.hit && (cur.dir === "bull" ? c.high >= cur.lo : c.low <= cur.hi)) { cur.hit = c.time; cur = null; }
    // 2. ERL taken → nearest IRL (FVG) in the new direction
    const e = erlEv.get(c.time);
    if (e) {
      const cand = zones.filter((z) => alive(z, c.time) && (e.dir === "bear" ? z.top < c.close : z.bottom > c.close));
      if (cand.length) {
        const z = e.dir === "bear" ? cand.reduce((p, q) => (q.top > p.top ? q : p)) : cand.reduce((p, q) => (q.bottom < p.bottom ? q : p));
        cur = { from: c.time, dir: e.dir, kind: "IRL", lo: z.bottom, hi: z.top, why: `${e.what} ${hm(c.time)} (ERL taken)`,
          label: `D.O.L ${e.dir === "bull" ? "↑" : "↓"} FVG ${z.bottom.toFixed(0)}–${z.top.toFixed(0)}` };
        out.push(cur); continue;
      }
    }
    // 3. IRL tapped and held → nearest ERL (old high / low pool) beyond
    for (const z of zones) {
      if (!alive(z, c.time)) continue;
      const bullTap = z.dir === "bull" && c.low <= z.top && c.close > z.top;
      const bearTap = z.dir === "bear" && c.high >= z.bottom && c.close < z.bottom;
      if (!bullTap && !bearTap) continue;
      const dir = bullTap ? "bull" : "bear";
      const tg = pools.filter((p) => p.from < c.time && (dir === "bull" ? p.side === "BSL" && p.price > c.close : p.side === "SSL" && p.price < c.close))
        .sort((p, q) => (dir === "bull" ? p.price - q.price : q.price - p.price))[0];
      if (tg && (!cur || cur.from !== c.time)) {
        cur = { from: c.time, dir, kind: "ERL", lo: tg.price, hi: tg.price, why: `FVG ${z.bottom.toFixed(0)}–${z.top.toFixed(0)} tapped & held ${hm(c.time)} (IRL)`,
          label: `D.O.L ${dir === "bull" ? "↑" : "↓"} ${dir === "bull" ? "old high" : "old low"} ${tg.price.toFixed(0)}` };
        out.push(cur);
      }
      break;
    }
  }
  return out;
}
