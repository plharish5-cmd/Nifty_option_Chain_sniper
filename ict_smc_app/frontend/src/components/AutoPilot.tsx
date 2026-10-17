// AutoPilot.tsx — PAPER auto-trader control panel + hook. INFORMATION ONLY, never a real order.
import { useCallback, useRef, useState } from "react";
import type { AlertRow, Candle } from "../api";
import { type AutoCfg, type AutoState, type AutoPos, bookedR, canOpen, classify, isEod, load, openFromSignal,
  save, stepCandle, stepEOD, stepReversal, wins } from "./autopilot";

export type AutoCtx = { last: Candle; atr: number; nearWall: number | null };

// alert sound — entry (rising) / exit (falling). Needs a prior user click to unlock audio.
let _actx: AudioContext | null = null;
export function beep(kind: "entry" | "exit") {
  try {
    _actx = _actx || new (window.AudioContext || (window as unknown as { webkitAudioContext: typeof AudioContext }).webkitAudioContext)();
    const seq: [number, number][] = kind === "entry" ? [[660, 0], [990, 0.09]] : [[880, 0], [520, 0.11]];
    for (const [f, t] of seq) {
      const o = _actx.createOscillator(), g = _actx.createGain();
      o.frequency.value = f; o.type = "sine"; o.connect(g); g.connect(_actx.destination);
      const s = _actx.currentTime + t; g.gain.setValueAtTime(0.001, s); g.gain.exponentialRampToValueAtTime(0.28, s + 0.01);
      g.gain.exponentialRampToValueAtTime(0.001, s + 0.12); o.start(s); o.stop(s + 0.13);
    }
  } catch { /* audio locked */ }
}

// log a CLOSED paper trade to the 8610 desk (permanent history + funds + CSV) in OPTION mode. Best-effort.
const DESK = "http://localhost:8610";
async function logClosedToDesk(p: AutoPos, delta: number) {
  try {
    const status = p.status === "REV" ? "MANUAL" : p.status;      // 8610 counts TP/SL/EOD/MANUAL
    const entry_prem = p.entryPrem ?? 0;
    const exit_prem = entry_prem > 0 ? Math.max(0, +(entry_prem + (p.r ?? 0) * p.risk * delta).toFixed(2)) : 0;  // premium ≈ entry + favourable-pts × delta
    const strike = Math.round(p.entry / 50) * 50;
    const r = await fetch(`${DESK}/api/trades`, { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ tf: "auto", side: p.side, signal: "auto:" + p.kind, entry: p.entry, sl: p.sl, tp: p.tp, note: p.label, lots: 1,
        opt_type: p.side === "BUY" ? "CE" : "PE", strike, entry_prem }) });
    const j = await r.json();
    if (j?.id && p.exit != null)
      await fetch(`${DESK}/api/trades/${j.id}/close`, { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ exit_px: p.exit, status, exit_prem }) });
  } catch { /* desk offline — the browser ledger still holds it */ }
}

export function useAutopilot() {
  const [s, setS] = useState<AutoState>(() => load());
  const ref = useRef(s); ref.current = s;
  const commit = useCallback((next: AutoState) => { save(next); ref.current = next; setS(next); }, []);

  const setCfg = useCallback((patch: Partial<AutoCfg>) => {
    const next = { ...ref.current, cfg: { ...ref.current.cfg, ...patch } };
    if (patch.on) next.stopped = null;                                   // turning on clears a prior stop
    commit(next);
  }, [commit]);

  const reset = useCallback(() => { const n = load(); commit({ ...n, open: [], closed: [], stopped: null, seq: 1 }); }, [commit]);

  // a signal fired → close opposites (reversal), then maybe open
  const onSignal = useCallback((a: AlertRow, ctx: AutoCtx) => {
    const cur = ref.current;
    if (!cur.cfg.on) return;
    const sig = classify(a); if (!sig) return;
    let next = { ...cur, open: [...cur.open], closed: [...cur.closed] };
    const px = a.price && a.price > 0 ? a.price : ctx.last.close;
    const rev = stepReversal(next, sig.side, px, ctx.last.time);          // opposite-side signal exits open trades
    if (rev.length) { next.open = next.open.filter((p) => !rev.includes(p)); next.closed = [...rev, ...next.closed]; rev.forEach((p) => logClosedToDesk(p, cur.cfg.delta)); if (cur.cfg.sound) beep("exit"); }
    const pos = openFromSignal(next, sig, a, ctx.last, ctx.atr, ctx.nearWall);
    if (pos) { next.seq = next.seq + 1; next.open = [pos, ...next.open]; if (cur.cfg.sound) beep("entry"); }
    // a guardrail trip stamps the reason once
    if (!next.stopped) { const c = canOpen(next); if (!c.ok && (c.why.startsWith("daily"))) next.stopped = c.why; }
    commit(next);
  }, [commit]);

  // new closed candle(s) → TP/SL checks + EOD flatten
  const onCandles = useCallback((candles: Candle[], spot: number | null) => {
    const cur = ref.current;
    if (!cur.cfg.on || !cur.open.length) return;
    const closed = candles.filter((c) => c.closed !== false);
    const last = closed[closed.length - 1]; if (!last) return;
    let next = { ...cur, open: [...cur.open], closed: [...cur.closed] };
    const tp = stepCandle(next, last);
    if (tp.length) { next.open = next.open.filter((p) => !tp.includes(p)); next.closed = [...tp, ...next.closed]; tp.forEach((p) => logClosedToDesk(p, cur.cfg.delta)); if (cur.cfg.sound) beep("exit"); }
    if (isEod(last.time, cur.cfg.eodHHMM) && next.open.length) {
      const eod = stepEOD(next, spot ?? last.close, last.time);
      next.open = []; next.closed = [...eod, ...next.closed]; next.stopped = next.stopped ?? "EOD 15:15 — flat for the day"; eod.forEach((p) => logClosedToDesk(p, cur.cfg.delta)); if (cur.cfg.sound) beep("exit");
    }
    if (tp.length || next.open.length !== cur.open.length || next.open.length) commit(next);  // also persists MFE/MAE updates
  }, [commit]);

  return { s, setCfg, reset, onSignal, onCandles };
}

// ── panel ──────────────────────────────────────────────────────────────────
const rCls = (r: number | undefined | null) => (r ?? 0) > 0 ? "text-bull-400" : (r ?? 0) < 0 ? "text-bear-400" : "text-slate-400";
const stBadge: Record<string, string> = { OPEN: "bg-indigo-900/70 text-indigo-300", TP: "bg-bull-900/70 text-bull-400",
  SL: "bg-bear-900/70 text-bear-400", REV: "bg-amber-900/70 text-amber-300", EOD: "bg-slate-700/70 text-slate-300" };

const LOT = 65;   // NIFTY lot size — live floating M2M = points × lot
function liveOf(p: AutoPos, spot: number | null) {
  if (spot == null) return null;
  const pts = p.side === "BUY" ? spot - p.entry : p.entry - spot;
  return { pts, r: +(pts / (p.risk || 1)).toFixed(2), rs: Math.round(pts * LOT) };
}

export function AutoPilotPanel({ ap, spot = null }: { ap: ReturnType<typeof useAutopilot>; spot?: number | null }) {
  const { s, setCfg, reset } = ap;
  const r = bookedR(s), n = s.closed.length, w = wins(s), gate = canOpen(s);
  const openPnl = spot == null ? null : s.open.reduce((a, p) => a + (liveOf(p, spot)?.rs ?? 0), 0);
  return (
    <div className="flex h-full flex-col text-[12px]">
      <div className="flex flex-wrap items-center gap-2 border-b border-ink-700 px-3 py-2">
        <button onClick={() => setCfg({ on: !s.cfg.on })}
          className={`rounded px-3 py-1 text-[13px] font-bold ${s.cfg.on ? "bg-bull-600 text-white" : "bg-ink-700 text-slate-300"}`}>
          {s.cfg.on ? "● AUTOPILOT ON" : "○ AUTOPILOT OFF"}</button>
        <span className="rounded bg-amber-950/60 px-2 py-0.5 text-[11px] font-semibold text-amber-300 ring-1 ring-amber-500/30">PAPER ONLY · never places a real order</span>
        <span className="ml-auto flex items-center gap-1 text-[11px]">
          <span className="text-slate-500">concurrent</span>
          {[1, 3].map((m) => <button key={m} onClick={() => setCfg({ maxOpen: m as 1 | 3 })}
            className={`tab !py-0.5 ${s.cfg.maxOpen === m ? "tab-on" : ""}`}>{m}</button>)}
          <button onClick={() => { setCfg({ sound: !s.cfg.sound }); beep("entry"); }} title="Beep on entry / exit (click also unlocks sound)"
            className={`tab !py-0.5 ${s.cfg.sound ? "tab-on" : ""}`}>{s.cfg.sound ? "🔔 sound" : "🔕 muted"}</button>
          <button onClick={() => setCfg({ slMode: (s.cfg.slMode ?? "structural") === "structural" ? "atr" : "structural" })}
            title="Stop-loss placement: structural = beyond the sweep-wick extreme (ICT invalidation); ATR = ~1 ATR / candle range"
            className={`tab !py-0.5 ${(s.cfg.slMode ?? "structural") === "structural" ? "tab-on" : ""}`}>SL: {(s.cfg.slMode ?? "structural") === "structural" ? "🎯 structural" : "ATR"}</button>
          <span className="ml-2 text-slate-500">stop</span>
          <span className="rounded bg-ink-800 px-1.5 py-0.5">{s.cfg.rCapLoss}R / +{s.cfg.rCapWin}R</span>
        </span>
      </div>

      {/* scorecard */}
      <div className="grid grid-cols-6 gap-2 border-b border-ink-700 px-3 py-2">
        <Stat label="Booked" val={`${r > 0 ? "+" : ""}${r}R`} cls={rCls(r)} />
        <Stat label="Open P&L (live)" val={openPnl == null ? "—" : `${openPnl >= 0 ? "+" : ""}₹${Math.abs(openPnl).toLocaleString("en-IN")}`} cls={rCls(openPnl)} />
        <Stat label="Open" val={`${s.open.length}`} cls="text-indigo-300" />
        <Stat label="Closed" val={`${n}`} />
        <Stat label="Win %" val={n ? `${Math.round(100 * w / n)}%` : "—"} cls={n && w / n >= 0.5 ? "text-bull-400" : "text-slate-300"} />
        <Stat label="Status" val={gate.ok ? "armed" : "idle"} cls={gate.ok ? "text-bull-400" : "text-amber-300"} />
      </div>
      {!gate.ok && <div className="border-b border-ink-700 bg-amber-950/30 px-3 py-1 text-[11px] text-amber-300">⏸ not opening new trades — {gate.why}. {s.stopped && <button onClick={() => setCfg({ on: true })} className="underline">re-arm</button>}</div>}

      {/* positions */}
      <div className="min-h-0 flex-1 overflow-auto px-3 py-2">
        {s.open.length === 0 && s.closed.length === 0
          ? <div className="py-6 text-center text-slate-500">{s.cfg.on ? "Armed — waiting for the next actionable signal…" : "Switch ON to let it paper-trade every signal. It auto-exits on TP / SL / reversal / EOD."}</div>
          : <table className="w-full num">
            <thead><tr className="text-left text-[10px] uppercase tracking-wide text-slate-500">
              <th className="py-1">Side</th><th>Bucket</th><th>Signal</th><th>Entry</th><th>SL</th><th>TP</th><th>LTP / Exit</th><th>P&L ₹</th><th>R</th><th>Best ▲ / Worst ▼</th><th>Status</th></tr></thead>
            <tbody>
              {[...s.open, ...s.closed].map((p) => <Row key={p.id} p={p} spot={spot} />)}
            </tbody>
          </table>}
      </div>

      <div className="flex items-center gap-3 border-t border-ink-700 px-3 py-1.5 text-[11px] text-slate-500">
        <span>Triggers: all actionable (★A+, ●ENTRY, flow, γ✓✓✓, ✓3, CRT, IFVG)</span>
        <button onClick={reset} className="ml-auto rounded bg-ink-800 px-2 py-0.5 text-slate-400 hover:text-slate-200">reset day</button>
        <span>R in index points · fills at candle price (optimistic) · strategy test only</span>
      </div>
    </div>
  );
}

function Stat({ label, val, cls = "text-slate-200" }: { label: string; val: string; cls?: string }) {
  return <div><div className="text-[10px] uppercase tracking-wide text-slate-500">{label}</div><div className={`text-[18px] font-bold ${cls}`}>{val}</div></div>;
}
function Row({ p, spot }: { p: AutoPos; spot: number | null }) {
  const open = p.status === "OPEN";
  const live = open ? liveOf(p, spot) : null;
  const showR = open ? live?.r ?? null : p.r ?? null;
  const rs = open ? live?.rs ?? null : (p.exit != null ? Math.round((p.r ?? 0) * p.risk * LOT) : null);
  // best (MFE) / worst (MAE) in points — for an open trade, blend the stored value with the live excursion
  const liveFav = open && spot != null ? (p.side === "BUY" ? spot - p.entry : p.entry - spot) : -Infinity;
  const liveAdv = open && spot != null ? (p.side === "BUY" ? p.entry - spot : spot - p.entry) : -Infinity;
  const best = Math.max(p.mfe ?? 0, liveFav);
  const worst = Math.max(p.mae ?? 0, liveAdv);
  const bestRs = Math.round(best * LOT), worstRs = Math.round(worst * LOT);
  return <tr className="border-t border-ink-800">
    <td className="py-1"><span className={`rounded px-1.5 py-0.5 text-[10px] font-bold ${p.side === "BUY" ? "bg-bull-900/70 text-bull-400" : "bg-bear-900/70 text-bear-400"}`}>{p.side}</span></td>
    <td><span className={`rounded px-1.5 py-0.5 text-[10px] font-bold ${p.opt === "CE" ? "bg-bull-950/70 text-bull-300 ring-1 ring-bull-500/30" : "bg-bear-950/70 text-bear-300 ring-1 ring-bear-500/30"}`}>{p.opt ?? "—"} {p.strike ?? ""}</span></td>
    <td className="max-w-[170px] truncate text-slate-300" title={p.label}>{p.label}</td>
    <td className="text-gold-400">{p.entry}</td><td className="text-bear-400">{p.sl}</td><td className="text-bull-400">{p.tp}</td>
    <td className="text-slate-200">{open ? (spot != null ? <span className="text-cyan-300">{spot.toFixed(0)}<span className="text-[9px] text-slate-500"> live</span></span> : "…") : (p.exit ?? "—")}</td>
    <td className={rCls(rs)}>{rs != null ? `${rs > 0 ? "+" : ""}₹${Math.abs(rs).toLocaleString("en-IN")}` : "—"}</td>
    <td className={rCls(showR)}>{showR != null ? `${showR > 0 ? "+" : ""}${showR}R` : "—"}</td>
    <td className="whitespace-nowrap"><span className="text-bull-400">▲{best > 0 ? `+${best.toFixed(0)}` : best.toFixed(0)}</span>
      <span className="text-slate-600"> / </span><span className="text-bear-400">▼−{worst > 0 ? worst.toFixed(0) : 0}</span>
      <div className="text-[9px] text-slate-500">₹{bestRs.toLocaleString("en-IN")} / −₹{Math.max(0, worstRs).toLocaleString("en-IN")}</div></td>
    <td><span className={`rounded px-1.5 py-0.5 text-[10px] font-bold ${stBadge[p.status]}`}>{open && live ? (live.rs >= 0 ? "▲ " : "▼ ") : ""}{p.status}</span></td>
  </tr>;
}
