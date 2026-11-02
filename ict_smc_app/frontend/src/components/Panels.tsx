// Panels.tsx — header, playbook signals, alerts, bias grid, narrative, OI builds, paper trades.
import type { AlertRow, Bias, Build, Dashboard, PaperStats, PaperTrade, Quote, Scorecard, Signal } from "../api";
import { fmt, lakh } from "../api";
import AlgoCockpit from "./AlgoCockpit";

const up = (x: number | null | undefined) => (x ?? 0) >= 0;

export function Header({ q, live, status }: { q: Quote | null; live: string; status: Dashboard["status"] | null }) {
  const tokenBad = String(status?.token ?? "").startsWith("expired");
  return (
    <header className="flex flex-wrap items-center gap-x-6 gap-y-2 px-4 py-3 border-b border-ink-700 bg-ink-900">
      <div className="flex items-center gap-2">
        <div className="h-7 w-1 rounded bg-gold-500" />
        <div>
          <div className="text-[10px] uppercase tracking-[0.2em] text-gold-400 font-semibold">ICT · SMC desk</div>
          <div className="text-sm font-semibold text-slate-100">NIFTY 50</div>
        </div>
      </div>
      <div className="flex items-baseline gap-3">
        <span className="num text-2xl font-semibold text-slate-50">{fmt(q?.spot, 2)}</span>
        <span className={`num text-sm font-semibold ${up(q?.chg) ? "text-bull-400" : "text-bear-400"}`}>
          {q?.chg == null ? "—" : `${up(q.chg) ? "+" : ""}${fmt(q.chg, 2)} (${up(q.chg_pct) ? "+" : ""}${fmt(q.chg_pct, 2)} %)`}
        </span>
      </div>
      <div className="text-xs text-slate-400">PDC <span className="num text-slate-200">{fmt(q?.prev_close, 2)}</span></div>
      <div className="text-xs text-slate-400">INDIA VIX <span className="num text-slate-100 font-semibold">{fmt(q?.vix, 2)}</span>
        {q?.vix_chg != null && <span className={`num ml-1 ${q.vix_chg >= 0 ? "text-bear-400" : "text-bull-400"}`}>{q.vix_chg >= 0 ? "+" : ""}{fmt(q.vix_chg, 2)}</span>}
      </div>
      <div className="ml-auto flex flex-wrap items-center gap-2">
        <AlgoCockpit />
        <span className={`pill ${q?.market_open ? "bg-bull-900 text-bull-400" : "bg-ink-800 text-slate-400"}`}>{q?.market_open ? "● Market open" : "Market closed"}</span>
        <span className={`pill ${live === "live" ? "bg-ink-800 text-slate-300" : "bg-bear-900 text-bear-400"}`}>App link: {live}</span>
        <span className="pill bg-ink-800 text-slate-300">Price: {q?.source ?? "—"}</span>
        {(() => {
          const u = (status as unknown as { dhan_calls?: { data: number; data_limit: number; desk_budget: number; data_pct: number; chain: number; rate_limited: number; over_budget: boolean } } | null)?.dhan_calls;
          if (!u) return null;
          const warn = u.over_budget || u.data_pct >= 70;
          return <span title={`This desk today: ${u.data} data calls (Dhan limit ${u.data_limit}/day for the whole account — other apps on your token count too) · ${u.chain} option-chain calls · ${u.rate_limited} rate-limited answers. Over ${u.desk_budget}, strike / futures candle refreshes slow to every 5th minute.`}
            className={`pill ${u.over_budget ? "bg-bear-900 text-bear-400" : warn ? "bg-amber-900/60 text-amber-300" : "bg-ink-800 text-slate-300"}`}>
            Dhan API: {u.data.toLocaleString("en-IN")} / {u.data_limit.toLocaleString("en-IN")}{u.rate_limited ? ` · ${u.rate_limited} limited` : ""}{u.over_budget ? " · saving calls" : ""}</span>;
        })()}
        {tokenBad && <span className="pill bg-bear-900 text-bear-400">Dhan token expired — update .env</span>}
      </div>
    </header>
  );
}

export function SignalCard({ s }: { s: Signal }) {
  if (s.status === "INFO") {
    return (
      <div className="flex items-baseline gap-2 px-1 text-[11px] text-slate-400">
        <span className="num text-slate-500 shrink-0">{s.time}</span>
        <span className={s.side === "long" ? "text-bull-400" : "text-bear-400"}>{s.side === "long" ? "▲" : "▼"}</span>
        <span>{s.name}</span>
      </div>
    );
  }
  const tone = s.status === "TRADE" ? "border-gold-500/70 bg-gold-500/5" : s.status === "SKIP" ? "border-ink-600" : "border-ink-700 bg-ink-850";
  const sideCls = s.side === "long" ? "text-bull-400" : "text-bear-400";
  return (
    <div className={`rounded-lg border ${tone} px-3 py-2`}>
      <div className="flex items-center justify-between gap-2">
        <div className="text-xs font-semibold text-slate-100">
          <span className="text-slate-500 mr-1.5">{s.time}</span>{s.setup === "B" ? s.name : `Setup ${s.setup} · ${s.name}`}
        </div>
        <span className={`pill ${s.status === "TRADE" ? "bg-gold-500 text-ink-950" : s.status === "SKIP" ? "bg-ink-700 text-slate-300" : "bg-ink-800 text-slate-400"}`}>{s.status}</span>
      </div>
      {s.setup !== "B" && (
        <div className="mt-1 text-[12px] num">
          <span className={`font-semibold ${sideCls}`}>{s.side === "long" ? "▲ LONG" : "▼ SHORT"}</span>
          {s.setup === "D" ? (
            <>
              <span className="text-slate-300"> @ {fmt(s.entry, 1)} · SL {fmt(s.sl, 1)} · TP-1 {fmt(s.ctx?.tp1 ?? null, 1)} <span className="text-slate-500">(½ + BE)</span> · TP-2 {fmt(s.tp, 1)}</span>
              <span className="text-slate-500"> · risk {fmt(s.risk, 1)} · R:R {fmt(s.ctx?.rr1 ?? null, 1)} / {fmt(s.rr, 1)}</span>
            </>
          ) : (
            <>
              <span className="text-slate-300"> @ {fmt(s.entry, 1)} · SL {fmt(s.sl, 1)} · TP {fmt(s.tp, 1)}</span>
              <span className="text-slate-500"> · risk {fmt(s.risk, 1)} · R:R {fmt(s.rr, 1)}</span>
            </>
          )}
        </div>
      )}
      {s.confidence != null && (
        <div className="mt-1 text-[11px] space-y-0.5">
          <div className="flex items-center gap-2">
            <span className="text-slate-500 w-20">Confidence</span>
            <div className="h-1.5 w-24 rounded bg-ink-700 overflow-hidden"><div className="h-full bg-gold-500" style={{ width: `${Math.round(s.confidence * 100)}%` }} /></div>
            <span className="num text-slate-300">{Math.round(s.confidence * 100)} %</span>
            <span className="text-slate-600">(never changes the trade)</span>
          </div>
          {s.confidence_parts && <Breakdown p={s.confidence_parts} />}
        </div>
      )}
      <ul className="mt-1 space-y-0.5 text-[11px] text-slate-400">
        {s.story.map((x, i) => <li key={i}>· {x}</li>)}
        {(s.confluence ?? []).map((x, i) => <li key={`c${i}`} className={x.startsWith("+") ? "text-bull-400/90" : x.startsWith("−") ? "text-bear-400/90" : "text-slate-500"}>Δ {x}</li>)}
        {s.reasons.map((x, i) => <li key={`r${i}`} className="text-amber-300/90">✕ {x}</li>)}
      </ul>
    </div>
  );
}

function Breakdown({ p }: { p: NonNullable<Signal["confidence_parts"]> }) {
  const row = (k: string, v: number | null, note = "") => (
    <div className="flex items-center gap-2">
      <span className="text-slate-500 w-20">{k}</span>
      {v == null ? <span className="text-slate-600">not recorded</span> : (
        <>
          <div className="relative h-1.5 w-24 rounded bg-ink-700 overflow-hidden">
            {k === "Structure" ? <div className="h-full bg-slate-400" style={{ width: `${Math.round(v * 100)}%` }} />
              : <div className={`absolute h-full ${v >= 0 ? "bg-bull-400 left-1/2" : "bg-bear-400 right-1/2"}`} style={{ width: `${Math.min(50, Math.abs(v) * 200)}%` }} />}
          </div>
          <span className={`num ${k === "Structure" ? "text-slate-300" : v > 0 ? "text-bull-400" : v < 0 ? "text-bear-400" : "text-slate-400"}`}>{k === "Structure" ? Math.round(v * 100) : `${v >= 0 ? "+" : "−"}${Math.round(Math.abs(v) * 100)}`}</span>
          {note && <span className="text-slate-600">{note}</span>}
        </>
      )}
    </div>
  );
  const src = p.flow_source === "delta" ? "delta / footprint" : p.flow_source === "proxy" ? "PROXY (not real delta)" : "unavailable";
  return <>{row("Structure", p.structure)}{row("Options ctx", p.options_context)}{row("Flow", p.flow, src)}</>;
}

export function Signals({ list }: { list: Signal[] }) {
  const show = [...list].reverse();
  return (
    <div className="space-y-2">
      {show.length === 0 && <div className="text-xs text-slate-500">No setup today yet. The playbook only speaks when a 5m candle closes.</div>}
      {show.map((s) => <SignalCard key={s.key} s={s} />)}
    </div>
  );
}

export function Alerts({ list }: { list: AlertRow[] }) {
  return (
    <ul className="divide-y divide-ink-800 text-[12px]">
      {list.length === 0 && <li className="py-2 text-slate-500">No alerts yet.</li>}
      {list.map((a) => (
        <li key={a.id} className="py-1.5 flex gap-2">
          <span className="text-slate-500 num shrink-0">{a.time}</span>
          <span className={a.grade === "trade" ? "text-gold-400 font-semibold" : a.grade === "rvol" ? "text-emerald-300" : a.grade === "skip" ? "text-slate-400" : "text-slate-300"}>{a.title}</span>
        </li>
      ))}
    </ul>
  );
}

export function BiasGrid({ bias }: { bias: Record<string, Bias> }) {
  const tfs = ["1m", "5m", "15m", "1h"].filter((t) => bias[t]);
  return (
    <div className="grid grid-cols-2 gap-2">
      {tfs.map((t) => {
        const b = bias[t];
        const col = b.trend === "bull" ? "text-bull-400" : b.trend === "bear" ? "text-bear-400" : "text-slate-400";
        return (
          <div key={t} className="rounded-lg bg-ink-850 border border-ink-700 px-3 py-2">
            <div className="flex justify-between text-[11px]"><span className="text-slate-400">{t}</span>
              <span className={`font-semibold uppercase ${col}`}>{b.trend === "none" ? "no structure" : b.trend}</span></div>
            <div className="text-[11px] text-slate-400 mt-0.5">{b.zone ? <>in <b className={b.zone === "premium" ? "text-bear-400" : "text-bull-400"}>{b.zone}</b> ({fmt(b.pos_pct, 0)} %)</> : "—"}</div>
            <div className="text-[10px] text-slate-500">{b.last_event ?? ""}</div>
          </div>
        );
      })}
    </div>
  );
}

export function Builds({ list }: { list: Build[] }) {
  const tone = (k: string) => (k === "Long build" || k === "Short covering" ? "text-bull-400" : k === "—" ? "text-slate-400" : "text-bear-400");
  return (
    <table className="w-full text-[12px] num">
      <tbody>
        {list.slice(0, 8).map((b, i) => (
          <tr key={i} className="border-b border-ink-800">
            <td className="py-1">{b.strike} <span className={b.side === "CE" ? "text-bear-400" : "text-bull-400"}>{b.side}</span></td>
            <td className={`py-1 ${tone(b.kind)}`}>{b.kind}</td>
            <td className="py-1 text-right">{lakh(b.oi_chg)}</td>
            <td className={`py-1 text-right ${b.chg >= 0 ? "text-bull-400" : "text-bear-400"}`}>{b.chg >= 0 ? "+" : ""}{fmt(b.chg, 1)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function ScoreTable({ sc }: { sc: Scorecard }) {
  const r = (v: number | null, d = 2) => (v == null ? "—" : v.toFixed(d));
  const col = (v: number | null) => (v == null || v === 0 ? "text-slate-300" : v > 0 ? "text-bull-400" : "text-bear-400");
  return (
    <div>
      <div className="flex items-baseline justify-between">
        <div className="text-[11px] uppercase tracking-wide text-gold-400 font-semibold">Scorecard — which strategy works</div>
        <div className="text-[10px] text-slate-500">{sc.first ? `since ${sc.first}` : ""}</div>
      </div>
      <table className="w-full text-[12px] num mt-1">
        <thead className="text-[10px] uppercase text-slate-400"><tr>
          <th className="text-left py-1">Strategy</th><th className="text-right">Trades</th><th className="text-right">Win</th><th className="text-right">Loss</th>
          <th className="text-right">Flat</th><th className="text-right" title="D: TP-1 booked, rest stopped at breakeven">BE</th><th className="text-right">Win %</th>
          <th className="text-right">Avg R</th><th className="text-right">Total R</th><th className="text-right">Best / worst R</th><th className="text-right">₹ premium</th><th className="text-center">30+</th></tr></thead>
        <tbody>
          {sc.rows.map((x) => (
            <tr key={x.label} className={`border-b border-ink-800 ${x.group === "setup" || x.group === "total" ? "font-semibold" : "text-slate-400"} ${x.group === "total" ? "border-t border-gold-500/40" : ""}`}>
              <td className={`py-1 ${x.group === "pair" || x.group === "model" ? "pl-4" : ""}`}>{x.label}</td>
              <td className="text-right">{x.trades}</td><td className="text-right text-bull-400">{x.wins}</td><td className="text-right text-bear-400">{x.losses}</td>
              <td className="text-right">{x.flat}</td><td className="text-right">{x.be_after_tp1 || ""}</td>
              <td className="text-right">{x.win_rate == null ? "—" : `${x.win_rate} %`}</td>
              <td className={`text-right ${col(x.avg_r)}`}>{r(x.avg_r)}</td><td className={`text-right ${col(x.total_r)}`}>{r(x.total_r)}</td>
              <td className="text-right">{x.trades ? `${r(x.best_r, 1)} / ${r(x.worst_r, 1)}` : "—"}</td>
              <td className={`text-right ${col(x.premium_rs)}`}>{x.premium_rs ? `₹${fmt(x.premium_rs, 0)}` : "—"}</td>
              <td className="text-center">{x.enough ? <span className="text-bull-400">✓</span> : <span className="text-slate-600">{x.trades}/30</span>}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <div className="text-[10px] text-slate-500 mt-1">Win = R &gt; 0. Avg R matters more than win % — a 40 % strategy at +2R beats a 70 % one at −0.2R. {sc.note} Each strategy has its own slot, so one never blocks another.</div>
    </div>
  );
}

export function Paper({ trades, stats, scorecard }: { trades: PaperTrade[]; stats: PaperStats; scorecard?: Scorecard }) {
  return (
    <div className="p-4 space-y-3 overflow-auto h-full">
      {scorecard && <ScoreTable sc={scorecard} />}
      <div className="flex flex-wrap gap-4 text-xs">
        <Stat k="Closed trades" v={String(stats.n)} />
        <Stat k="Win rate" v={stats.n ? `${stats.win_rate} %` : "—"} />
        <Stat k="Avg R" v={stats.n ? String(stats.avg_r) : "—"} />
        <Stat k="Total R" v={stats.n ? String(stats.total_r) : "—"} />
        <Stat k="Points" v={stats.n ? fmt(stats.pnl_pts, 1) : "—"} />
        <Stat k={`₹ index-pts proxy (${stats.lot_qty} qty)`} v={stats.n ? `₹${fmt(stats.pnl_rs_proxy, 0)}` : "—"} />
        <Stat k={`₹ option premium (${stats.premium_trades ?? 0} trades)`} v={stats.premium_trades ? `₹${fmt(stats.premium_pnl_rs, 0)}` : "—"} />
      </div>
      <div className="text-[11px] text-slate-500">Paper only — this app has no order endpoints. Entries at the 5m close, exits resolved on 1m candles (a candle touching stop and target counts as the stop). Stops and targets are NIFTY levels; ₹ premium = the strike selected at entry (CE for longs, PE for shorts), its 1m close at the entry and exit minutes. {stats.note}</div>
      <table className="w-full text-[12px] num">
        <thead className="text-[10px] uppercase text-slate-400"><tr>
          <th className="text-left py-1">Opened</th><th className="text-left">Setup</th><th className="text-left">Side</th><th className="text-right">Entry</th>
          <th className="text-right">SL</th><th className="text-right">TP</th><th className="text-right">Exit</th><th className="text-left pl-3">Result</th><th className="text-right">R</th><th className="text-right">MFE R</th><th className="text-left pl-3">Strike</th><th className="text-right">Prem in → out</th><th className="text-right">₹ premium</th></tr></thead>
        <tbody>
          {trades.length === 0 && <tr><td colSpan={13} className="py-3 text-slate-500">No paper trades yet.</td></tr>}
          {trades.map((t) => (
            <tr key={t.id} className="border-b border-ink-800">
              <td className="py-1">{t.opened}</td><td>{t.setup} · {t.pair ? t.pair.replace(">", "→") : t.tf}{t.model && t.setup === "D" ? ` · ${t.model}` : ""}</td>
              <td className={t.side === "long" ? "text-bull-400" : "text-bear-400"}>{t.side}</td>
              <td className="text-right">{fmt(t.entry)}</td><td className="text-right">{fmt(t.sl)}</td><td className="text-right">{fmt(t.tp)}</td>
              <td className="text-right">{fmt(t.exit)}</td>
              <td className={`pl-3 ${t.status === "TP" ? "text-bull-400" : t.status === "SL" ? "text-bear-400" : "text-slate-300"}`}>{t.status}{t.closed ? ` ${t.closed}` : ""}</td>
              <td className={`text-right ${(t.r ?? 0) >= 0 ? "text-bull-400" : "text-bear-400"}`}>{t.r ?? "—"}</td>
              <td className="text-right text-slate-400">{t.mfe_r ?? "—"}</td>
              <td className="pl-3 text-slate-300">{t.opt_key ? t.opt_key.split("|").slice(2).join(" ") : "—"}</td>
              <td className="text-right text-slate-300">{t.opt_entry != null ? `${fmt(t.opt_entry, 1)} → ${t.opt_exit != null ? fmt(t.opt_exit, 1) : "…"}` : "—"}</td>
              <td className={`text-right ${(t.opt_pnl_rs ?? 0) >= 0 ? "text-bull-400" : "text-bear-400"}`}>{t.opt_pnl_rs != null ? `₹${fmt(t.opt_pnl_rs, 0)}` : "—"}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function Stat({ k, v }: { k: string; v: string }) {
  return <div className="rounded-lg bg-ink-850 border border-ink-700 px-3 py-1.5"><div className="text-[10px] text-slate-500 uppercase">{k}</div><div className="num text-sm text-slate-100">{v}</div></div>;
}

export { default as AdvPaPanel } from "./AdvPaPanel";
export { default as PaCoursePanel } from "./PaCoursePanel";
export { WritersFlow, WritersChart } from "./OcLab";
