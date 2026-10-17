// dol.ts — Draw on Liquidity (ICT IRL ⇄ ERL), closed candles only, information only.
// Tight version — one clean chain, no target on every small FVG touch:
//   1. ERL taken (liquidity SWEEP / CRT TBS / CRT entry)  → D.O.L = nearest unfilled FVG (IRL) in the new direction
//   2. that FVG is REACHED and the candle closes back out of it on the far side (held) → D.O.L = the old high / low (ERL)
//      on the other side (the move back out of the gap)
// A target stays active until reached (✓) or a newer ERL sweep replaces it.
import type { Analysis, Candle } from "../api";

export type Dol = { from: number; dir: "bull" | "bear"; kind: "IRL" | "ERL"; lo: number; hi: number; label: string;
  why: string; hit?: number; trig?: number; trigWhat?: string };

// Higher-timeframe (daily) key liquidity — the external draws ICT targets beyond the current chart's own pools.
// Built from session levels: PDH/PDL (prior day), the running day high/low, and the opening-range edges.
function htfPools(sess: Analysis["session"] | undefined): { side: "BSL" | "SSL"; src: string; price: number }[] {
  if (!sess) return [];
  const out: { side: "BSL" | "SSL"; src: string; price: number }[] = [];
  const add = (p: number | undefined, side: "BSL" | "SSL", src: string) => { if (p && isFinite(p)) out.push({ side, src, price: p }); };
  add(sess.prev?.h, "BSL", "PDH"); add(sess.prev?.l, "SSL", "PDL");
  add(sess.high, "BSL", "day high"); add(sess.low, "SSL", "day low");
  add(sess.or?.h, "BSL", "OR high"); add(sess.or?.l, "SSL", "OR low");
  return out;
}

export function computeDol(candles: Candle[], a: Analysis | null, useHtf = true): Dol[] {
  if (!a) return [];
  const closed = candles.filter((c) => c.closed !== false);
  const zones = (a.zones ?? []).filter((z) => z.type === "FVG" || z.type === "IFVG");
  const pools = a.pools ?? [];
  const htf = useHtf ? htfPools(a.session) : [];
  const hm = (t: number) => { const d = new Date(t * 1000); return `${String(d.getUTCHours()).padStart(2, "0")}:${String(d.getUTCMinutes()).padStart(2, "0")}`; };
  const erlEv = new Map<number, { dir: "bull" | "bear"; what: string; px: number }>();
  for (const m of a.markers ?? []) if (m.kind === "SWEEP") erlEv.set(m.time, { dir: m.dir, what: "liquidity sweep", px: m.price });
  for (const m of a.crt?.markers ?? []) if (m.kind === "CRT_TBS" || m.kind === "CRT_ENTRY") erlEv.set(m.time, { dir: m.dir, what: m.kind === "CRT_TBS" ? "CRT TBS" : "CRT entry", px: m.price });
  const alive = (z: { from: number; to: number }, t: number) => z.from < t && z.to >= t;
  const out: Dol[] = [];
  let cur: Dol | null = null;
  for (const c of closed) {
    // reached?
    if (cur && !cur.hit && (cur.dir === "bull" ? c.high >= cur.lo : c.low <= cur.hi)) {
      cur.hit = c.time;
      const was: Dol = cur; cur = null;
      // IRL reached and the candle closed back out on the far side → next draw is the ERL behind us
      if (was.kind === "IRL") {
        const held = was.dir === "bear" ? c.close > was.hi : c.close < was.lo;
        if (held) {
          const dir: "bull" | "bear" = was.dir === "bear" ? "bull" : "bear";
          // ERL candidates = current-tf pools + the HTF daily levels (PDH/PDL/day-extreme/OR), nearest in the draw direction
          const cands: { side: "BSL" | "SSL"; src: string; price: number }[] = [
            ...pools.filter((p) => p.from < c.time).map((p) => ({ side: p.side, src: p.src, price: p.price })),
            ...htf,
          ].filter((p) => (dir === "bull" ? p.side === "BSL" && p.price > c.close : p.side === "SSL" && p.price < c.close));
          const tg = cands.sort((p, q) => (dir === "bull" ? p.price - q.price : q.price - p.price))[0];
          if (tg) {
            const name = tg.src && /PDH|PDL|day|OR/i.test(tg.src) ? tg.src : (dir === "bull" ? "old high" : "old low");
            cur = { from: c.time, dir, kind: "ERL", lo: tg.price, hi: tg.price,
              why: `FVG ${was.lo.toFixed(0)}–${was.hi.toFixed(0)} reached & held ${hm(c.time)} (IRL)`,
              label: `D.O.L ${dir === "bull" ? "↑" : "↓"} ${name} ${tg.price.toFixed(0)}` };
            out.push(cur);
          }
        }
      }
      continue;
    }
    // ERL taken → nearest IRL in the new direction (replaces any older target)
    const e = erlEv.get(c.time);
    if (!e) continue;
    const cand = zones.filter((z) => alive(z, c.time) && (e.dir === "bear" ? z.top < c.close : z.bottom > c.close));
    if (!cand.length) continue;
    const z = e.dir === "bear" ? cand.reduce((p, q) => (q.top > p.top ? q : p)) : cand.reduce((p, q) => (q.bottom < p.bottom ? q : p));
    cur = { from: c.time, dir: e.dir, kind: "IRL", lo: z.bottom, hi: z.top, why: `${e.what} ${hm(c.time)} (ERL taken)`,
      trig: e.px, trigWhat: e.what,
      label: `D.O.L ${e.dir === "bull" ? "↑" : "↓"} FVG ${z.bottom.toFixed(0)}–${z.top.toFixed(0)}` };
    out.push(cur);
  }
  return out;
}
