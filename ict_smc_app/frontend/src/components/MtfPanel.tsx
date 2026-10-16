// MtfPanel.tsx — Multi-timeframe (Daily / Weekly / Monthly) read. Display only.
// Polls /api/mtf (live basis-adjusted FUT — same numbers as the chart). Collapsible, bottom-left.
// Hover a timeframe row → the action guide for that horizon. Confluence = the real stacked() D/W/M overlap.
import { useEffect, useState } from "react";

type Trend = { ema9: number | null; ema21: number | null; ema50: number | null; status: string; bars: number };
type TfBlock = { tf: string; available: boolean; bars?: number; last?: number; atr?: number; trend?: Trend;
  volatility?: string; delta10?: number; delta_dir?: string; note?: string };
type Conf = { price: number; lo: number; hi: number; strength: number; label: string; members: string[]; dist: number | null };
type Mtf = { available: boolean; message?: string; error?: string; spot?: number; asof?: string;
  D?: TfBlock; W?: TfBlock; M?: TfBlock; confluence?: Conf[]; source?: string };

function useMtf(ms = 60000) {
  const [data, setData] = useState<Mtf | null>(null);
  useEffect(() => {
    let dead = false;
    const pull = () => fetch("/api/mtf").then((r) => r.json()).then((j) => { if (!dead) setData(j); }).catch(() => { /* */ });
    pull();
    const id = window.setInterval(pull, ms);
    return () => { dead = true; window.clearInterval(id); };
  }, [ms]);
  return data;
}

const trendCls = (s?: string) => s === "BULLISH" ? "text-bull-400" : s === "BEARISH" ? "text-bear-400" : "text-slate-400";
const trendBg = (s?: string) => s === "BULLISH" ? "bg-bull-900/70 ring-bull-500/40" : s === "BEARISH" ? "bg-bear-900/70 ring-bear-500/40" : "bg-ink-800/80 ring-slate-600/40";
const volIcon = (v?: string) => v === "EXPANDING" ? "↑ widening" : v === "CONTRACTING" ? "↓ tightening" : v === "STABLE" ? "~ steady" : "—";
const volCls = (v?: string) => v === "EXPANDING" ? "text-amber-300" : v === "CONTRACTING" ? "text-cyan-300" : "text-slate-400";

// What to DO at each timeframe given its own trend + volatility (concise, actionable).
function guide(tf: string, b?: TfBlock): string {
  if (!b?.available || !b.trend) return "no data yet";
  const t = b.trend.status, v = b.volatility, atr = b.atr ?? 0;
  const wide = v === "EXPANDING";
  const slHint = `SL ~${(atr * (wide ? 1.5 : 1)).toFixed(0)} pts`;
  if (tf === "D") {
    if (t === "BULLISH") return `Intraday up-trend — buy dips only, skip shorts. ${slHint}, target +${(atr * 1.5).toFixed(0)}.`;
    if (t === "BEARISH") return `Intraday down-trend — sell rallies only, skip longs. ${slHint}, target −${(atr * 1.5).toFixed(0)}.`;
    return `Intraday choppy — fade the edges, don't chase. Only trade a confluence zone. ${slHint}.`;
  }
  if (tf === "W") {
    if (t === "BULLISH") return `Swing up — dips into W/M support are the A+ long area. Hold longs, wide SL ok.`;
    if (t === "BEARISH") return `Swing down — rallies into W/M resistance are the short area. Hold shorts.`;
    return `Swing balanced — range-trade between the confluence shelves.`;
  }
  // M
  if (t === "BULLISH") return `Positional up — the bigger bias favours longs; pullbacks are buying chances.`;
  if (t === "BEARISH") return `Positional down — the bigger bias favours shorts; bounces are selling chances.`;
  return `Positional range — price mid-range, both shelves in play. ${wide ? "Range is widening — expect a breakout soon." : ""}`;
}

function Row({ tf, b }: { tf: string; b?: TfBlock }) {
  const [open, setOpen] = useState(false);
  const name = tf === "D" ? "Daily" : tf === "W" ? "Weekly" : "Monthly";
  const sub = tf === "D" ? "intraday · 5m" : tf === "W" ? "swing · 1h" : "positional · 1d";
  return (
    <div className="cursor-help border-t border-ink-700 py-1 first:border-0"
      onMouseEnter={() => setOpen(true)} onMouseLeave={() => setOpen(false)}>
      {/* line 1: name · trend badge · delta (right) */}
      <div className="flex items-center gap-2">
        <span className="w-[58px] shrink-0 font-semibold text-slate-100">{name}</span>
        <span className={`rounded px-1.5 py-[1px] text-[10px] font-bold ring-1 ${trendBg(b?.trend?.status)} ${trendCls(b?.trend?.status)}`}>
          {b?.trend?.status ?? "—"}</span>
        {b?.delta_dir && <span className={`num ml-auto ${b.delta_dir === "BUY" ? "text-bull-400" : b.delta_dir === "SELL" ? "text-bear-400" : "text-slate-500"}`}>
          Δ{(b.delta10 ?? 0) > 0 ? "+" : ""}{b.delta10}</span>}
      </div>
      {/* line 2: atr · volatility · horizon */}
      <div className="num mt-0.5 flex items-center gap-2 pl-[58px] text-[10px] text-slate-500">
        <span>ATR <b className="text-slate-300">{b?.atr ?? "—"}</b></span>
        <span className={volCls(b?.volatility)}>{volIcon(b?.volatility)}</span>
        <span className="text-slate-600">{sub}{tf === "M" && (b?.bars ?? 99) < 50 ? ` · ${b?.bars}-sess*` : ""}</span>
      </div>
      {open && <div className="mt-1 rounded bg-amber-950/60 px-1.5 py-1 text-[11px] leading-snug text-amber-200 ring-1 ring-amber-500/20">▸ {guide(tf, b)}</div>}
    </div>
  );
}

export default function MtfPanel() {
  const d = useMtf();
  const [open, setOpen] = useState<boolean>(() => { try { return localStorage.getItem("ict_mtf_open") !== "0"; } catch { return true; } });
  const toggle = () => { const v = !open; setOpen(v); try { localStorage.setItem("ict_mtf_open", v ? "1" : "0"); } catch { /* */ } };

  if (!d) return null;
  if (!d.available) return (
    <div className="pointer-events-auto rounded bg-[#070b12] px-2 py-1 text-[10.5px] text-slate-400 ring-1 ring-white/10 shadow-xl">
      📊 MTF — {d.message || d.error || "waiting for futures feed"}</div>
  );

  // collapsed = one summary pill
  if (!open) return (
    <button onClick={toggle} className="pointer-events-auto rounded bg-[#070b12] px-2 py-1 text-[10.5px] ring-1 ring-gold-500/40 shadow-xl">
      <span className="font-semibold text-gold-400">▸ MTF</span>
      <span className="ml-1.5"><span className={trendCls(d.D?.trend?.status)}>D·{d.D?.trend?.status?.[0] ?? "—"}</span>{" "}
        <span className={trendCls(d.W?.trend?.status)}>W·{d.W?.trend?.status?.[0] ?? "—"}</span>{" "}
        <span className={trendCls(d.M?.trend?.status)}>M·{d.M?.trend?.status?.[0] ?? "—"}</span></span>
    </button>
  );

  const conf = (d.confluence ?? []).slice(0, 3);
  return (
    <div className="pointer-events-auto w-[320px] rounded-lg bg-[#070b12] px-2.5 py-1.5 text-[11px] leading-snug ring-1 ring-gold-500/40 shadow-2xl">
      <div className="flex items-baseline justify-between">
        <button onClick={toggle} className="font-semibold text-gold-400">▾ Multi-Timeframe</button>
        <span className="num text-slate-400">spot <b className="text-slate-100">{d.spot?.toLocaleString("en-IN")}</b></span>
      </div>
      <div className="mt-1">
        <Row tf="D" b={d.D} />
        <Row tf="W" b={d.W} />
        <Row tf="M" b={d.M} />
      </div>
      {conf.length > 0 && <div className="mt-1 border-t border-ink-700 pt-1">
        <div className="mb-0.5 font-semibold text-gold-300">Confluence zones (D·W·M overlap)</div>
        {conf.map((z, i) => {
          const above = (z.dist ?? 0) > 0;
          return <div key={i} className="flex items-center gap-1.5">
            <span className={`font-bold ${z.strength === 3 ? "text-gold-400" : "text-slate-300"}`}>{z.label}</span>
            <span className="num text-slate-200">{z.price.toLocaleString("en-IN")}</span>
            {z.dist != null && <span className={`num ${above ? "text-bull-400" : "text-bear-400"}`}>
              {above ? "+" : ""}{z.dist} pts {above ? "above" : "below"}</span>}
          </div>;
        })}
        <div className="mt-0.5 text-[10px] text-slate-500">Fade these only when price arrives over-extended (±2σ). A calm drift can break through.</div>
      </div>}
      <div className="mt-1 border-t border-ink-700 pt-0.5 text-[9.5px] text-slate-600">
        {d.source || "live FUT"} · hover a row for the action{(d.M?.bars ?? 99) < 50 ? " · *M = short history" : ""}</div>
    </div>
  );
}
