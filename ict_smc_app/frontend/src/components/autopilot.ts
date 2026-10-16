// autopilot.ts — PAPER auto-trader engine. INFORMATION ONLY. Never places a real order.
// Runs in the browser while the desk is open. On a qualifying signal it opens a simulated trade
// (entry/SL/TP in index points), then on each CLOSED candle checks TP / SL / reversal / EOD and
// books the result in R. A daily R cap stops new entries. Fills are assumed at the candle price /
// level — optimistic vs real slippage — so treat the P&L as a strategy test, not a promise.
import type { AlertRow, Candle } from "../api";

export type AutoCfg = { on: boolean; maxOpen: 1 | 3; rCapLoss: number; rCapWin: number; eodHHMM: number; rr: number;
  triggers: Record<string, boolean>; sound: boolean; premium: number; delta: number };
export type AutoPos = { id: number; t0: number; side: "BUY" | "SELL"; kind: string; label: string;
  entry: number; sl: number; tp: number; risk: number; status: "OPEN" | "TP" | "SL" | "REV" | "EOD"; exit?: number; r?: number; t1?: number; desk?: number | null; entryPrem?: number;
  opt?: "CE" | "PE"; strike?: number; mfe?: number; mae?: number };   // CE/PE bucket + best(MFE)/worst(MAE) excursion in points
export type AutoState = { day: string; cfg: AutoCfg; open: AutoPos[]; closed: AutoPos[]; stopped: string | null; seq: number };

export const DEFAULT_CFG: AutoCfg = {
  on: false, maxOpen: 1, rCapLoss: -3, rCapWin: 5, eodHHMM: 1515, rr: 1.5,
  triggers: { aplus: true, nodes: true, flow: true, gamma: true, conf: true, crt: true, ifvg: true },
  sound: true, premium: 120, delta: 0.5,      // option-mode: assumed ATM premium ₹ and option delta
};

const KEY = "ict_autopilot_v1";
const istDay = (t = Date.now()) => new Date(t).toISOString().slice(0, 10);   // chart times are IST-shifted
const hhmm = (t: number) => { const d = new Date(t * 1000); return d.getUTCHours() * 100 + d.getUTCMinutes(); };

// map a fired SIG_ alert to a trigger key + side; returns null if the robot shouldn't act on it
export function classify(a: AlertRow): { key: string; side: "BUY" | "SELL" } | null {
  const k = a.kind || "";
  if (!k.startsWith("SIG_")) return null;
  const side = a.side === "BUY" || a.side === "SELL" ? a.side : null;
  if (!side) return null;
  const map: Record<string, string> = {
    SIG_APLUS: "aplus", SIG_NODES: "nodes", SIG_FLOW: "flow", SIG_GAMMA: "gamma",
    SIG_CONF: "conf", SIG_CRT: "crt", SIG_IFVG: "ifvg",
  };
  const base = Object.keys(map).find((p) => k.startsWith(p));
  return base ? { key: map[base], side } : { key: "other", side };
}

export function load(): AutoState {
  const fresh: AutoState = { day: istDay(), cfg: DEFAULT_CFG, open: [], closed: [], stopped: null, seq: 1 };
  try {
    const s = JSON.parse(localStorage.getItem(KEY) || "null") as AutoState | null;
    if (!s) return fresh;
    if (s.day !== istDay()) return { ...fresh, cfg: { ...DEFAULT_CFG, ...s.cfg, on: false } };  // new day → reset trades, keep settings, pause
    return { ...fresh, ...s, cfg: { ...DEFAULT_CFG, ...s.cfg } };
  } catch { return fresh; }
}
export function save(s: AutoState) { try { localStorage.setItem(KEY, JSON.stringify(s)); } catch { /* */ } }

export const bookedR = (s: AutoState) => +s.closed.reduce((a, p) => a + (p.r ?? 0), 0).toFixed(2);
export const wins = (s: AutoState) => s.closed.filter((p) => (p.r ?? 0) > 0).length;

// is the robot allowed to open a new trade right now?
export function canOpen(s: AutoState): { ok: boolean; why: string } {
  if (!s.cfg.on) return { ok: false, why: "autopilot OFF" };
  if (s.stopped) return { ok: false, why: s.stopped };
  const r = bookedR(s);
  if (r <= s.cfg.rCapLoss) return { ok: false, why: `daily stop: ${r}R ≤ ${s.cfg.rCapLoss}R` };
  if (r >= s.cfg.rCapWin) return { ok: false, why: `daily target: ${r}R ≥ +${s.cfg.rCapWin}R` };
  if (s.open.length >= s.cfg.maxOpen) return { ok: false, why: `max ${s.cfg.maxOpen} open` };
  return { ok: true, why: "" };
}

// open a paper position from a signal + the latest closed candle. Returns the new position or null.
export function openFromSignal(s: AutoState, sig: { key: string; side: "BUY" | "SELL" }, a: AlertRow,
  last: Candle, atr: number, nearWall?: number | null): AutoPos | null {
  if (!s.cfg.triggers[sig.key]) return null;                              // this trigger is disabled
  if (!canOpen(s).ok) return null;
  // one position per side at a time (don't stack the same direction on back-to-back pings)
  if (s.open.some((p) => p.side === sig.side)) return null;
  const buy = sig.side === "BUY";
  const entry = a.price && a.price > 0 ? a.price : last.close;
  const rng = Math.max(last.high - last.low, atr * 0.6);
  const risk = Math.max(atr, rng);                                        // SL = ~1 ATR / candle range, whichever is wider
  const sl = buy ? entry - risk : entry + risk;
  let tp = buy ? entry + s.cfg.rr * risk : entry - s.cfg.rr * risk;       // RR target…
  if (nearWall && nearWall > 0) {                                         // …but never past the opposing wall (take the nearer)
    tp = buy ? Math.min(tp, nearWall) : Math.max(tp, nearWall);
    if (buy ? nearWall <= entry : nearWall >= entry) tp = buy ? entry + s.cfg.rr * risk : entry - s.cfg.rr * risk;
  }
  return { id: s.seq, t0: last.time, side: sig.side, kind: a.kind, label: a.title.slice(0, 48),
    entry: +entry.toFixed(1), sl: +sl.toFixed(1), tp: +tp.toFixed(1), risk: +risk.toFixed(1), status: "OPEN", desk: null,
    entryPrem: s.cfg.premium, opt: buy ? "CE" : "PE", strike: Math.round(entry / 50) * 50, mfe: 0, mae: 0 };
}

// evaluate every open position against a newly CLOSED candle → close any that hit TP/SL; returns closed ids
export function stepCandle(s: AutoState, c: Candle): AutoPos[] {
  const done: AutoPos[] = [];
  for (const p of s.open) {
    if (c.time <= p.t0) continue;                                        // only candles AFTER entry
    const buy = p.side === "BUY";
    // best (MFE) / worst (MAE) excursion in points, from this candle's high/low
    const fav = buy ? c.high - p.entry : p.entry - c.low;
    const adv = buy ? p.entry - c.low : c.high - p.entry;
    p.mfe = Math.max(p.mfe ?? 0, +fav.toFixed(1));
    p.mae = Math.max(p.mae ?? 0, +adv.toFixed(1));
    let hit: "TP" | "SL" | null = null, px = 0;
    if (buy) { if (c.low <= p.sl) { hit = "SL"; px = p.sl; } else if (c.high >= p.tp) { hit = "TP"; px = p.tp; } }
    else { if (c.high >= p.sl) { hit = "SL"; px = p.sl; } else if (c.low <= p.tp) { hit = "TP"; px = p.tp; } }
    if (hit) { close(p, hit, px, c.time); done.push(p); }
  }
  return done;
}

// reversal: an opposite-side signal closes open trades at the candle close
export function stepReversal(s: AutoState, side: "BUY" | "SELL", px: number, t: number): AutoPos[] {
  const done: AutoPos[] = [];
  for (const p of s.open) if (p.side !== side) { close(p, "REV", px, t); done.push(p); }
  return done;
}

// end of day: flatten everything at the given price
export function stepEOD(s: AutoState, px: number, t: number): AutoPos[] {
  const done = [...s.open]; for (const p of s.open) close(p, "EOD", px, t); return done;
}

function close(p: AutoPos, status: AutoPos["status"], px: number, t: number) {
  p.status = status; p.exit = +px.toFixed(1); p.t1 = t;
  p.r = +(((p.side === "BUY" ? px - p.entry : p.entry - px) / (p.risk || 1))).toFixed(2);
}

export const isEod = (t: number, hhmmCap: number) => hhmm(t) >= hhmmCap;
