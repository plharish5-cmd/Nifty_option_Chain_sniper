import React, { useMemo, useRef, useState } from "react";
import type { OiStudioPayload, StrikeRow } from "./WriterMap";
import "./strikePositioning.css";
import analystConfig from "../../../../analyst/analyst_config.json";

export interface ObservedSpot { value: number; observed_ts: number; candle_ts: number }
export interface PositionBucket { start_ts: number; end_ts: number; label: string; partial: boolean; spot: ObservedSpot | null }
export interface PositionLeg { delta: number; start_oi: number; end_oi: number; start_observed_ts: number; end_observed_ts: number }
export interface PositionCell { ce: PositionLeg | null; pe: PositionLeg | null }
export interface PositionRow { strike: number; quality: Record<"ce" | "pe", { total_valid: boolean; change_valid: boolean }>; cells: PositionCell[] }
export interface PositionHistory { ok: boolean; message?: string; bucket_minutes: number; buckets: PositionBucket[]; rows: PositionRow[]; session_date: string; asof_ts: number; spot: ObservedSpot | null; source: string; baseline: string; note: string; stale_after_seconds: number; max_snapshot_age_seconds: number }
export function units(value: number | null, lots: boolean, lot: number, signed = false): string {
  if (value === null || !Number.isFinite(value)) return "—";
  const sign = value < 0 ? "−" : signed && value > 0 ? "+" : "";
  const a = Math.abs(value);
  if (lots) return sign + Math.round(a / lot).toLocaleString("en-IN") + " lots";
  return sign + (a >= 1e7 ? (a / 1e7).toFixed(2) + "Cr" : a >= 1e5 ? (a / 1e5).toFixed(1) + "L" : a >= 1e3 ? (a / 1e3).toFixed(1) + "K" : a.toFixed(0));
}
export function priceY(value: number, strikes: number[]): number | null {
  if (!strikes.length || value > strikes[0] || value < strikes[strikes.length - 1]) return null;
  if (strikes.length === 1) return value === strikes[0] ? 34 : null;
  for (let i = 0; i < strikes.length - 1; i++) {
    if (value <= strikes[i] && value >= strikes[i + 1]) return (i + (strikes[i] - value) / (strikes[i] - strikes[i + 1])) * 68 + 34;
  }
  return null;
}
function stamp(ts: number) { return new Date(ts * 1000).toLocaleString("en-IN", { timeZone: "Asia/Kolkata", month: "short", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false }); }
function Mini({ value, max, side, label }: { value: number | null; max: number; side: "ce" | "pe"; label: string }) {
  return <div className={`sp-mini ${side}`} title={label}>
    <span>{side.toUpperCase()}</span>
    {value === null ? <span className="sp-missing">no snap</span> : value === 0 ? <span className="sp-zero">0</span> : <div className="sp-track"><i className={value < 0 ? "removed" : "added"} style={{ width: `${Math.abs(value) / max * 100}%` }} /><small>{value < 0 ? "−" : "+"}</small></div>}
  </div>;
}
export default function StrikePositioning({ data, lots, bucket, options, onBucket, onReplay, busy, error }: { data: OiStudioPayload | null; lots: boolean; bucket: number; options: number[]; onBucket: (n: number) => void; onReplay: (ts: number) => void; busy: boolean; error: string }) {
  const [selected, setSelected] = useState<number | null>(null);
  const [near, setNear] = useState(true);
  const scrollBox = useRef<HTMLDivElement>(null);
  const [compare, setCompare] = useState(false);
  const [pins, setPins] = useState<number[]>([]);
  const history = data?.positioning;
  const lot = data?.lot_size || 1;
  const fmt = (n: number | null, signed = false) => units(n, lots, lot, signed);
  const paired = useMemo(() => {
    const index = new Map(data?.strikes.map(s => [s.strike, s]));
    return (history?.rows || []).flatMap(h => { const s = index.get(h.strike); return s ? [{ h, s }] : []; });
  }, [data, history]);
  const spot = history?.spot?.value ?? null;
  const nearest = useMemo(() => [...paired].sort((a, b) => Math.abs(a.s.strike - (spot ?? data?.spot ?? 0)) - Math.abs(b.s.strike - (spot ?? data?.spot ?? 0))), [paired, spot, data?.spot]);
  const nearSet = new Set(nearest.slice(0, analystConfig.oi_studio.positioning.near_strikes).map(r => r.s.strike));
  const rows = paired.filter(r => (!near || nearSet.has(r.s.strike)) && (!compare || pins.includes(r.s.strike)));
  const active = rows.find(r => r.s.strike === selected) || [...rows].sort((a, b) => Math.abs(a.s.strike - (spot ?? data?.spot ?? 0)) - Math.abs(b.s.strike - (spot ?? data?.spot ?? 0)))[0];
  const totalMax = Math.max(1, ...paired.flatMap(r => [r.h.quality.ce.total_valid ? r.s.call_oi_total : 0, r.h.quality.pe.total_valid ? r.s.put_oi_total : 0]));
  const deltaMax = Math.max(1, ...paired.flatMap(r => r.h.cells.flatMap(c => [Math.abs(c.ce?.delta ?? 0), Math.abs(c.pe?.delta ?? 0)])));
  const netMax = Math.max(1, ...paired.map(r => Math.abs(r.s.net_pe_ce_chg)));
  const rowStrikes = rows.map(r => r.s.strike);
  const validTotal = (s: StrikeRow, h: PositionRow, side: "ce" | "pe") => h.quality[side].total_valid ? (side === "ce" ? s.call_oi_total : s.put_oi_total) : null;
  const validDelta = (s: StrikeRow, h: PositionRow, side: "ce" | "pe") => h.quality[side].change_valid ? (side === "ce" ? s.call_oi_chg : s.put_oi_chg) : null;
  const age = history ? Math.max(0, Math.floor(Date.now() / 1000 - history.asof_ts)) : null;
  const pin = (strike: number) => setPins(p => p.includes(strike) ? p.filter(v => v !== strike) : [...p, strike]);
  if (error) return <div className="sp-empty" role="alert">Strike Positioning unavailable: {error}. The existing histogram is still available.</div>;
  if (!history) return <div className="sp-empty">{busy ? "Loading recorded positioning history…" : "History not available. If the backend is running an older build, restart it to load this optional view."}</div>;
  if (!history.ok) return <div className="sp-empty">{history.message || "No recorded positioning history"}</div>;
  return <section className="sp-root" aria-label="Strike Positioning Map">
    <header className="sp-heading"><div><h2>STRIKE POSITIONING <span>SIZE → CHANGE → TIME</span></h2><p>One strike per row. Standing size, window change, and recorded history — both legs kept separate.</p></div>
      <div className="sp-chips"><span>Expiry <b>{data?.expiries_meta?.active_expiry_str}</b></span><span>{history.session_date} · {data?.window_start_label} → {data?.window_end_label} IST</span><span>Recorded NIFTY <b>{spot === null ? "unavailable" : spot.toLocaleString("en-IN")}</b></span><span className={age !== null && age > history.stale_after_seconds ? "sp-stale" : ""}>{age !== null && age > history.stale_after_seconds ? "Recorded / stale" : "Snapshot age"} <b>{age === null ? "—" : age >= 86400 ? `${Math.floor(age / 86400)}d ${Math.floor(age % 86400 / 3600)}h` : age >= 3600 ? `${Math.floor(age / 3600)}h ${Math.floor(age % 3600 / 60)}m` : `${age}s`}</b></span></div>
    </header>
    <div className="sp-tools"><div className="sp-legend"><span className="ce">● Amber = CE</span><span className="pe">● Cyan = PE</span><span>Filled + added · outline − removed</span><span className="sp-missing">no snap</span><span>0 measured zero</span><span>● recorded NIFTY close · guides break at missing price data</span></div><label>Bucket <select aria-label="Positioning bucket" value={bucket} onChange={e => onBucket(Number(e.target.value))}>{options.map(n => <option key={n} value={n}>{n}m</option>)}</select></label></div>
    <div className="sp-actions"><button aria-pressed={!near && !compare} onClick={() => { setNear(false); setCompare(false); }}>Overview</button><button aria-pressed={near} onClick={() => { setNear(v => !v); setCompare(false); }}>Near spot</button><button disabled={!pins.length} aria-pressed={compare} onClick={() => { setCompare(v => !v); setNear(false); }}>Compare pinned ({pins.length})</button><button onClick={() => scrollBox.current?.scrollTo({left: 0, behavior: "smooth"})}>← Opening buckets</button><button onClick={() => scrollBox.current?.scrollTo({left: scrollBox.current.scrollWidth, behavior: "smooth"})}>Latest buckets →</button><span>Pin rows to compare · time headers set replay · {lots ? "lots" : "shares (L / Cr)"}</span></div>
    {!rows.length ? <div className="sp-empty">No strikes in this selection.</div> : <div className="sp-scroll" ref={scrollBox} tabIndex={0} aria-label="Scrollable positioning history">
      <div className="sp-grid" style={{ width: 620 + history.buckets.length * 112 }}>
        <div className="sp-summary"><div className="sp-row sp-head"><span>Strike</span><span>Standing OI · CE / PE</span><span>Window ΔOI<br /><small>% = Δ / starting OI</small></span><span>Net ΔPE − ΔCE</span></div>
          {rows.map(({ s, h }) => { const net = h.quality.ce.change_valid && h.quality.pe.change_valid ? s.net_pe_ce_chg : null; return <div key={s.strike} className={`sp-row ${active?.s.strike === s.strike ? "sp-selected" : ""}`}>
            <div><button className="sp-strike" onClick={() => setSelected(s.strike)}>{s.strike.toLocaleString("en-IN")}</button>{s.strike === data?.max_pain && <abbr title="Max pain is computed from the same OI. It is not independent confirmation.">MP · same OI</abbr>}<button aria-label={`Pin strike ${s.strike}`} aria-pressed={pins.includes(s.strike)} onClick={() => pin(s.strike)} className="sp-pin">{pins.includes(s.strike) ? "◆" : "◇"}</button></div>
            <div>{(["ce", "pe"] as const).map(side => { const value = validTotal(s, h, side); return <div key={side} className={`sp-standing ${side}`} title={`${side.toUpperCase()} standing OI: ${fmt(value)}`}><i style={{ width: `${(value ?? 0) / totalMax * 62}%` }} /><span>{side.toUpperCase()} {fmt(value)}</span></div>; })}</div>
            <div>{(["ce", "pe"] as const).map(side => { const value = validDelta(s, h, side), base = side === "ce" ? s.call_oi_base : s.put_oi_base; return <div key={side} className={`sp-change ${side}`} title={`ΔOI divided by starting OI (${fmt(base)}). Zero starting OI: percentage undefined.`}>{side.toUpperCase()} <b>{fmt(value, true)}</b><small>{value === null || base <= 0 ? "—" : `${(value / base * 100).toFixed(1)}%`}</small></div>; })}</div>
            <div className="sp-net"><i style={{ width: `${Math.abs(net ?? 0) / netMax * 42}%`, left: net !== null && net < 0 ? `${50 - Math.abs(net) / netMax * 42}%` : "50%" }} /><b>{fmt(net, true)}</b></div>
          </div>; })}
        </div>
        <div className="sp-history" style={{ width: history.buckets.length * 112 }}><div className="sp-timehead">{history.buckets.map(b => <button key={b.end_ts} onClick={() => onReplay(b.end_ts)} title={`${stamp(b.start_ts)} → ${stamp(b.end_ts)}${b.partial ? " • provisional / partial" : ""}`}>{b.label}{b.partial && <small>partial</small>}</button>)}</div>
          <div className="sp-body">{rows.map(({ s, h }) => <div className={`sp-history-row ${active?.s.strike === s.strike ? "sp-selected" : ""}`} key={s.strike}>{h.cells.map((c, i) => <button key={i} className="sp-cell" aria-label={`${s.strike} ${history.buckets[i].label}: CE ${fmt(c.ce?.delta ?? null, true)}, PE ${fmt(c.pe?.delta ?? null, true)}`} onClick={() => setSelected(s.strike)}>{(["ce", "pe"] as const).map(side => { const leg = c[side]; return <Mini key={side} side={side} value={leg?.delta ?? null} max={deltaMax} label={leg ? `${side.toUpperCase()} ΔOI ${fmt(leg.delta, true)} • ${fmt(leg.start_oi)} → ${fmt(leg.end_oi)} • observations ${stamp(leg.start_observed_ts)} → ${stamp(leg.end_observed_ts)}` : `${side.toUpperCase()}: no comparable endpoints within ${history.max_snapshot_age_seconds}s; no zero substituted`} />; })}</button>)}</div>)}
            <svg className="sp-price" width={history.buckets.length * 112} height={rows.length * 68} aria-label="Recorded bucket-end NIFTY closes" role="img">{history.buckets.map((b, i) => {
              const y = b.spot ? priceY(b.spot.value, rowStrikes) : null;
              const prev = history.buckets[i - 1];
              const py = prev?.spot ? priceY(prev.spot.value, rowStrikes) : null;
              if (y === null) return null;
              return <g key={b.end_ts}>{i > 0 && py !== null && <line x1={(i - 1) * 112 + 56} y1={py} x2={i * 112 + 56} y2={y} stroke="#e2e8f0" strokeOpacity=".65" strokeDasharray="4 4" />}<circle cx={i * 112 + 56} cy={y} r="4" fill="#fff"><title>NIFTY {b.spot?.value} · closed minute observed {stamp(b.spot!.observed_ts)}</title></circle></g>;
            })}</svg>
          </div>
        </div>
      </div>
    </div>}
    {!history.buckets.length && <div className="sp-empty">One snapshot only — two comparable observations are needed to show change.</div>}
    <div className="sp-scales">Common scales across the full selected strike range: largest interval |ΔOI| {fmt(deltaMax)} · largest standing OI {fmt(totalMax)}. Tiny values are not inflated. Endpoint observations may precede bucket edges by up to {history.max_snapshot_age_seconds}s.</div>
    {active && <div className="sp-detail"><div><h3>SELECTED {active.s.strike.toLocaleString("en-IN")}</h3><p>{spot === null ? "Spot distance unavailable" : `${Math.abs(active.s.strike - spot).toFixed(2)} points ${active.s.strike > spot ? "above" : "below"} recorded NIFTY`}</p></div>{(["ce", "pe"] as const).map(side => <div key={side} className={side}><h3>{side.toUpperCase()} • OI, not traded volume</h3><p>Standing <b>{fmt(validTotal(active.s, active.h, side))}</b></p><p>Window Δ <b>{fmt(validDelta(active.s, active.h, side), true)}</b></p></div>)}<div><h3>WHY NET IS NOT ENOUGH</h3><p>PE +12L / CE +2L → net +10L</p><p>PE −2L / CE −12L → net +10L</p><p>Same net; both legs tell you what changed.</p></div></div>}
    <footer className="sp-foot"><p>{history.source}. {history.baseline}. {history.note}</p><p>OI does not identify buyer/seller intent. Max pain uses the same OI source. Bucket-end closes cannot prove an intrabar touch, rejection or breakout. No trade advice is inferred from these bars.</p></footer>
  </section>;
}
