// signalAlerts.tsx — chart SIGNAL alerts (information only — never places orders).
// Uses the same sound · voice · desktop · toast pipeline as the RVOL alerts (useRvolAlerts().fire).
// Fires ONCE per signal, only on the latest CLOSED candle, only while the market is open.
//   ⚖ BIAS ≥ N of 14     — N agreeing factors (choose 9 / 10 / 11 / 12)
//   ✓3 CONFLUENCE        — footprint pill + delta colour + structure mark agree
//   🎯 PE−CE action       — 8501 Action switches to CE or PE
//   SMT                   — NIFTY vs BANKNIFTY divergence
//   IFVG buy / sell · CRT entry (Setup D) · D.O.L target reached
//   ★ A+                  — Bias ≥ {N} AND (✓3 or IFVG or CRT entry) on the SAME side, same candle
import { useEffect, useState } from "react";
import type { AlertRow, Analysis, Candle } from "../api";
import type { BiasRow } from "./bias";
import type { FpBar } from "./footprint";
import type { SmtMark } from "./ocfeed";
import type { Dol } from "./dol";

export type SigKind = "bias" | "conf" | "ocAct" | "smt" | "ifvg" | "crt" | "dol" | "gamma" | "nodes" | "flow" | "aplus";
export type SigPrefs = { on: boolean; minBias: number; kinds: Record<SigKind, boolean> };
const KEY = "ict_signal_alert_prefs";
const DEF: SigPrefs = { on: false, minBias: 10,
  kinds: { bias: true, conf: true, ocAct: true, smt: true, ifvg: true, crt: true, dol: true, gamma: true, nodes: true, flow: true, aplus: true } };

export function useSigPrefs() {
  const [p, setP] = useState<SigPrefs>(() => {
    try { const s = JSON.parse(localStorage.getItem(KEY) || "{}"); return { ...DEF, ...s, kinds: { ...DEF.kinds, ...(s.kinds ?? {}) } }; }
    catch { return DEF; }
  });
  useEffect(() => { try { localStorage.setItem(KEY, JSON.stringify(p)); } catch { /* private mode */ } }, [p]);
  return [p, setP] as const;
}

// remembered across reloads for the day, so a refresh doesn't repeat the same alert
const SEEN_KEY = "ict_signal_seen";
const seen: Set<string> = (() => { try { return new Set<string>(JSON.parse(sessionStorage.getItem(SEEN_KEY) || "[]")); } catch { return new Set<string>(); } })();
const remember = (k: string) => { seen.add(k); try { sessionStorage.setItem(SEEN_KEY, JSON.stringify([...seen].slice(-400))); } catch { /* ignore */ } };

type Ev = { key: string; kind: SigKind; side: "BUY" | "SELL" | "INFO"; title: string; body: string; price: number };

export function scanSignals(o: {
  tf: string; candles: Candle[]; a: Analysis | null; prefs: SigPrefs; bias: BiasRow[]; conf: FpBar[];
  ocActs: { time: number; v: number; txt: string }[]; smt: SmtMark[] | null; dols: Dol[];
  gamma?: { time: number; side: "BUY" | "SELL" }[]; nodes?: { time: number; side: "BUY" | "SELL"; zone: { label: string; lo: number; hi: number }; z: number }[];
  flow?: { time: number; verdict: "BUY" | "SELL" | "WAIT"; prev: "BUY" | "SELL" | "WAIT" | null; bull: number; bear: number; head: string } | null;
}): Ev[] {
  const { tf, candles, a, prefs, bias, conf, ocActs, smt, dols } = o;
  const closed = candles.filter((c) => c.closed !== false);
  const last = closed[closed.length - 1];
  if (!last) return [];
  const t = last.time, px = last.close, K = prefs.kinds;
  const hm = (() => { const d = new Date(t * 1000); return `${String(d.getUTCHours()).padStart(2, "0")}:${String(d.getUTCMinutes()).padStart(2, "0")}`; })();
  const side = (dir: string) => (dir === "bull" ? "BUY" : "SELL") as "BUY" | "SELL";
  const leg = (s: string) => (s === "BUY" ? "CE side" : "PE side");
  const out: Ev[] = [];
  const add = (kind: SigKind, s: Ev["side"], title: string, body: string) =>
    out.push({ key: `${tf}|${t}|${kind}|${s}`, kind, side: s, title: `${tf} ${hm} · ${title}`, body, price: px });

  // ⚖ bias
  const b = bias.find((r) => r.time === t && !r.forming);
  let biasSide: "BUY" | "SELL" | null = null;
  if (b && b.verdict !== "MIXED") {
    const n = b.verdict === "BUY" ? b.bull : b.bear;
    if (n >= prefs.minBias) {
      biasSide = b.verdict;
      if (K.bias) add("bias", b.verdict, `${b.verdict} bias ${n} of ${b.avail}`,
        `${n} factors agree → ${leg(b.verdict)}${b.warn ? ` · ⚠ ${b.warn}` : ""}. Direction filter only — enter on your setup with a fixed SL.`);
    }
  }
  // ✓3 confluence
  const cf = conf.find((x) => x.time === t && x.conf);
  const confSide = cf?.conf ? side(cf.conf.dir) : null;
  if (cf?.conf && K.conf) add("conf", side(cf.conf.dir), `${side(cf.conf.dir)} ✓3 confluence`, `${cf.conf.why.join(" + ")} → ${leg(side(cf.conf.dir))}.`);
  // IFVG / CRT entry
  let ifvgSide: "BUY" | "SELL" | null = null, crtSide: "BUY" | "SELL" | null = null;
  for (const m of a?.markers ?? []) if (m.time === t && (m.kind === "IFVG_BUY" || m.kind === "IFVG_SELL")) {
    ifvgSide = m.kind === "IFVG_BUY" ? "BUY" : "SELL";
    if (K.ifvg) add("ifvg", ifvgSide, `IFVG ${ifvgSide === "BUY" ? "buy ▲" : "sell ▼"}`, ifvgSide === "BUY" ? "BUY CE · SL below the zone." : "SELL / PE · SL above the zone.");
  }
  for (const m of a?.crt?.markers ?? []) if (m.time === t && m.kind === "CRT_ENTRY") {
    crtSide = side(m.dir);
    if (K.crt) add("crt", crtSide, `CRT entry ${crtSide}`, "Setup D — toward the other side of the CRT box; TP1 50 %, SL beyond the sweep wick.");
  }
  // 🎯 PE−CE action switch (last row vs the one before)
  if (K.ocAct && ocActs.length >= 2) {
    const i = ocActs.findIndex((x) => x.time === t);
    if (i > 0 && ocActs[i].v !== 0 && ocActs[i].v !== ocActs[i - 1].v) {
      const s = ocActs[i].v > 0 ? "BUY" : "SELL";
      add("ocAct", s, `PE−CE action → ${s === "BUY" ? "CE" : "PE"}`, ocActs[i].txt.slice(0, 160));
    }
  }
  // SMT
  for (const m of smt ?? []) if (m.time === t && K.smt) add("smt", side(m.dir), `SMT ${m.dir === "bull" ? "▲" : "▼"}`, m.text);
  // D.O.L reached
  for (const d of dols) if (d.hit === t && K.dol) add("dol", "INFO", `D.O.L reached ${d.lo.toFixed(0)}`, `${d.label} reached — book profit / trail SL.`);
  // γ ✓✓✓ (recorded live by the gamma layer)
  const gm = (o.gamma ?? []).find((x) => x.time === t);
  const gSide = gm ? gm.side : null;
  if (gm && K.gamma) add("gamma", gm.side, `γ ✓✓✓ ${gm.side}`, `EMA stack + flag at a gamma level + volume ≥ 1.5× → ${leg(gm.side)}. SL at the flag edge, target the next gamma level.`);
  // ● ENTRY — overextension (VWAP ±2σ) into a stacked D/W/M zone
  const ne = (o.nodes ?? []).find((x) => x.time === t);
  const nSide = ne ? ne.side : null;
  if (ne && K.nodes) add("nodes", ne.side, `● ENTRY ${ne.side} at ${ne.zone.label}`, `${ne.z.toFixed(1)}σ stretch into zone ${ne.zone.lo.toFixed(0)}–${ne.zone.hi.toFixed(0)} and closed back → ${leg(ne.side)} reversal. SL beyond the zone, first target VWAP.`);
  // 🧭 order-flow verdict — only when it FLIPS to BUY / SELL
  const f = o.flow && o.flow.time === t ? o.flow : null;
  const fSide = f && f.verdict !== "WAIT" ? f.verdict : null;
  if (f && fSide && f.prev !== f.verdict && K.flow) add("flow", fSide, `Order flow → ${fSide} (${Math.max(f.bull, f.bear)} vs ${Math.min(f.bull, f.bear)})`, f.head.slice(0, 160));
  // ★ A+ : bias + triggers on the same side — weight = how many independent triggers agree
  const trigs = biasSide ? ([[confSide, "✓3"], [ifvgSide, "IFVG"], [crtSide, "CRT entry"], [gSide, "γ ✓✓✓"], [nSide, "● ENTRY"], [fSide, "order flow"]] as const)
    .filter(([sd]) => sd === biasSide).map(([, n]) => n) : [];
  if (K.aplus && biasSide && trigs.length) {
    const w = trigs.length;
    add("aplus", biasSide, `★ A+${w > 1 ? ` ×${w}` : ""} ${biasSide} — bias ≥ ${prefs.minBias} + ${trigs.join(" + ")}`,
      `${w >= 3 ? "Very rare — three independent reads agree. " : w === 2 ? "Two independent triggers agree. " : ""}Still: closed candle only, fixed SL, 1 lot, Kill Switch rules. Paper trade first.`);
  }
  return out;
}

// fire the new ones through the shared RVOL pipeline (kind prefixed SIG_ so sound / voice differ)
export function dispatch(evs: Ev[], fire: (a: AlertRow) => void, tf: string) {
  const fresh = evs.filter((e) => !seen.has(e.key));
  // if an A+ fired, skip its separate bias line to avoid double speech
  const hasA = fresh.some((e) => e.kind === "aplus");
  for (const e of fresh) {
    remember(e.key);
    if (hasA && e.kind === "bias") continue;
    fire({ id: Date.now(), ts: Date.now() / 1000, time: "", grade: e.kind === "aplus" ? "trade" : "info",
      kind: `SIG_${e.kind.toUpperCase()}_${e.side}`, side: e.side, tf, title: e.title, body: e.body, price: e.price });
  }
}

export const SIG_SOUND: Record<string, [number, number][]> = {
  BUY: [[660, 0.12], [880, 0.12], [1175, 0.25]], SELL: [[1175, 0.12], [880, 0.12], [660, 0.25]], INFO: [[990, 0.2]],
  APLUS: [[880, 0.1], [1320, 0.1], [880, 0.1], [1320, 0.3]],
};

const HELP: Record<SigKind, { when: string; say: string }> = {
  bias: { when: "{N} or more of the 14 factors agree on the closed candle (N = the bias ≥ selector).", say: "“BUY bias 10 of 14” — direction filter only." },
  conf: { when: "Footprint pill, delta colour and a structure mark agree on one candle.", say: "“SELL check 3 confluence”" },
  ocAct: { when: "The 8501 PE−CE 🎯 Action switches to CE or PE.", say: "“PE−CE action → CE”" },
  smt: { when: "NIFTY and BANKNIFTY diverge at a high or low (one sweeps, the other doesn't).", say: "“SMT ▼” — weak sweep, expect a turn." },
  ifvg: { when: "IFVG buy ▲ or sell ▼ prints.", say: "BUY CE · SL below the zone / SELL PE · SL above the zone." },
  crt: { when: "CRT entry — Setup D.", say: "Toward the other side of the CRT box; TP1 50 %, SL beyond the sweep wick." },
  dol: { when: "The D.O.L target is reached.", say: "“book profit / trail SL”" },
  gamma: { when: "γ ✓✓✓ — EMA 9/21/50 stack + a flag AT a gamma level breaking + volume ≥ 1.5× (needs the γ Gamma 3✓ layer on).", say: "“gamma BUY” — SL flag edge, target next gamma level." },
  nodes: { when: "● ENTRY — price stretched beyond VWAP ±2σ INTO a stacked D/W/M volume zone and closed back (needs the Stacked nodes layer on).", say: "“ENTRY BUY at W M A” — reversal, first target VWAP." },
  flow: { when: "The order-flow read FLIPS to BUY or SELL (delta, CVD, stacked imbalance, big trades, room to the wall). Needs one Order-flow PRO layer on.", say: "“Order flow → BUY (4 vs 1)”" },
  aplus: { when: "Bias ≥ {N} AND at least one of ✓3 · IFVG · CRT · γ ✓✓✓ · ● ENTRY · order flow on the SAME side, SAME candle. ×2 / ×3 = how many agree.", say: "Different rising tone; desktop pop-up stays until you close it. Strongest alert — ×2 or ×3 even more so." },
};
const LBL: Record<SigKind, string> = { bias: "bias", conf: "✓3", ocAct: "PE−CE", smt: "SMT", ifvg: "IFVG", crt: "CRT", dol: "D.O.L", gamma: "γ✓✓✓", nodes: "●ENTRY", flow: "🧭flow", aplus: "★A+" };
export function SignalControls({ p, setP }: { p: SigPrefs; setP: (f: (x: SigPrefs) => SigPrefs) => void }) {
  return (
    <span className="flex flex-wrap items-center gap-1">
      <button onClick={() => setP((x) => ({ ...x, on: !x.on }))}
        className={`pill ${p.on ? "bg-gold-500 text-ink-900 font-semibold" : "bg-ink-800 text-slate-400"}`}
        title="Signal alerts on the latest closed candle — information only, never places orders">📣 signals {p.on ? "ON" : "off"}</button>
      {p.on && <>
        <select value={p.minBias} onChange={(e) => setP((x) => ({ ...x, minBias: Number(e.target.value) }))}
          className="pill bg-ink-800 text-slate-200" title="Bias alert when at least this many of the 14 factors agree">
          {[8, 9, 10, 11, 12].map((n) => <option key={n} value={n}>bias ≥ {n}</option>)}
        </select>
        {(Object.keys(LBL) as SigKind[]).map((k) => (
          <span key={k} className="group relative">
            <button onClick={() => setP((x) => ({ ...x, kinds: { ...x.kinds, [k]: !x.kinds[k] } }))}
              className={`pill ${p.kinds[k] ? "bg-ink-700 text-slate-100" : "bg-ink-800 text-slate-500 line-through"}`}>{LBL[k]}</button>
            <span className="pointer-events-none invisible absolute right-0 top-full z-50 mt-1 w-[340px] rounded-lg border border-gold-500/50 bg-[#060b16] p-3 text-[14px] leading-snug text-slate-100 shadow-2xl group-hover:visible">
              <b className="text-gold-400 text-[15px]">{LBL[k]}</b> <span className="text-slate-400 text-[12px]">({p.kinds[k] ? "on — click to turn off" : "off — click to turn on"})</span>
              <span className="mt-1 block"><b>Fires when:</b> {HELP[k].when.replace("{N}", String(p.minBias))}</span>
              <span className="mt-1 block text-cyan-200"><b>Says:</b> {HELP[k].say}</span>
            </span>
          </span>
        ))}
      </>}
    </span>
  );
}
