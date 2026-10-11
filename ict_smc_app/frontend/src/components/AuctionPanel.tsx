// AuctionPanel.tsx — "Read the auction, verify the response". The 5 questions (location · effort · result · response ·
// decision), the TPO value profile, balance boxes and the accept / reject log. Observation only — it never trades.
import { useEffect, useState } from "react";
import { api, type AuctionRes, type TF, type TpoProfile, type VolumeView } from "../api";

const BUILD_CLR: Record<string, string> = { "long build-up": "text-bull-400", "short covering": "text-emerald-300",
  "short build-up": "text-bear-400", "long unwinding": "text-rose-300", flat: "text-slate-500" };

function OiIv({ v }: { v: VolumeView | null }) {
  if (!v) return null;
  const recent = (v.bars ?? []).filter((b) => b.build).slice(-12);
  const s = v.oi_summary;
  return (
    <div className="rounded-lg border border-ink-700 p-2 space-y-1">
      <div className="text-[11px] text-slate-400">NIFTY futures OI build-up (last {recent.length} candles) · BANKNIFTY confirmation · ATM IV · strike delta</div>
      {recent.length === 0 ? <div className="text-slate-500">No futures OI yet{v.message ? ` — ${v.message}` : ""}.</div> : (
        <div className="flex flex-wrap gap-1">
          {recent.map((b) => (
            <span key={b.time} title={`OI ${b.oi?.toLocaleString("en-IN")} · ΔOI ${b.doi?.toLocaleString("en-IN")}`}
              className={`pill bg-ink-800 ${BUILD_CLR[b.build ?? "flat"]}`}>{new Date((b.time - 19800) * 1000).toLocaleTimeString("en-IN", { hour: "2-digit", minute: "2-digit", hour12: false, timeZone: "Asia/Kolkata" })} {b.build === "flat" ? "·" : b.build}</span>
          ))}
        </div>
      )}
      {v.banknifty?.available && (v.banknifty.recent ?? []).length > 0 && (
        <div className="flex flex-wrap gap-1 items-center"><span className="text-slate-400 text-[11px] mr-1">BANKNIFTY fut:</span>
          {(v.banknifty.recent ?? []).map((b) => (
            <span key={b.time} className={`pill bg-ink-800 ${BUILD_CLR[b.build ?? "flat"]}`}>{new Date((b.time - 19800) * 1000).toLocaleTimeString("en-IN", { hour: "2-digit", minute: "2-digit", hour12: false, timeZone: "Asia/Kolkata" })} {b.build === "flat" ? "·" : b.build}</span>
          ))}
        </div>
      )}
      {v.banknifty && !v.banknifty.available && <div className="text-slate-500">{v.banknifty.message}</div>}
      {v.oi_compare && <div>NIFTY vs BANKNIFTY: <b className={v.oi_compare.verdict === "aligned" ? "text-sky-300" : v.oi_compare.verdict === "diverging" ? "text-orange-300" : "text-slate-400"}>{v.oi_compare.verdict}</b> — {v.oi_compare.text}</div>}
      {v.oi_surge_last && <div className="text-amber-300">OI⚡ surge at {new Date(v.oi_surge_last.ts * 1000).toLocaleTimeString("en-IN", { hour: "2-digit", minute: "2-digit", hour12: false, timeZone: "Asia/Kolkata" })} ({v.oi_surge_last.surge_x}× normal ΔOI, OI above its 8-EMA) — big move building; direction from build-up + structure</div>}
      {s && s.dominant !== "flat" && <div>Dominant: <b className={BUILD_CLR[s.dominant]}>{s.dominant}</b> · net ΔOI {s.net_doi.toLocaleString("en-IN")} · OI {s.oi.toLocaleString("en-IN")}</div>}
      <div>ATM IV: {v.iv_last ? <b>{v.iv_last.iv.toFixed(2)}</b> : <span className="text-slate-500">not recorded yet (live only)</span>}
        {v.iv_trend && <> · {v.iv_trend.word} {v.iv_trend.from.toFixed(1)}→{v.iv_trend.to.toFixed(1)} in {v.iv_trend.minutes} min{v.iv_trend.word === "falling" ? " — orderly, cleaner move" : v.iv_trend.word === "rising" ? " — fear: wider swings" : ""}</>}</div>
      {(v.strike_greeks ?? []).map((g) => (
        <div key={g.side}>{g.label}: Δ {g.delta != null ? g.delta.toFixed(2) : "—"} · IV {g.iv != null ? g.iv.toFixed(1) : "—"}
          {g.delta_trend && <span className="text-slate-400"> · today |Δ| {Math.abs(g.delta_trend.from).toFixed(2)}→{Math.abs(g.delta_trend.to).toFixed(2)} ({g.delta_trend.word})</span>}</div>
      ))}
    </div>
  );
}

const KIND: Record<string, [string, string]> = {
  AUCTION_ACCEPT: ["Accepted", "text-sky-300"], AUCTION_REJECT: ["Rejected", "text-orange-300"],
  AUCTION_NO_FOLLOW: ["No follow-through", "text-slate-300"], TESTING: ["Testing", "text-gold-400"],
};

const compact = (n: number) => n >= 1e7 ? `${(n / 1e7).toFixed(1)}Cr` : n >= 1e5 ? `${(n / 1e5).toFixed(1)}L` : n >= 1e3 ? `${(n / 1e3).toFixed(0)}K` : `${n}`;

function Profile({ p, title, px }: { p: TpoProfile & { hvn?: number[]; lvn?: number[] }; title: string; px: number | null }) {
  const rows = [...p.hist].reverse();
  const max = Math.max(1, ...rows.map((r) => r[1]));
  return (
    <div className="min-w-0">
      <div className="text-[11px] text-slate-400 mb-1">{title} · POC <b className="text-slate-200">{p.poc.toFixed(1)}</b> · value {p.val.toFixed(0)}–{p.vah.toFixed(0)}
        {p.hvn?.length ? <> · HVN {p.hvn.map((x) => x.toFixed(0)).join(", ")}</> : null}{p.lvn?.length ? <> · LVN {p.lvn.map((x) => x.toFixed(0)).join(", ")}</> : null}</div>
      <div className="max-h-[220px] overflow-auto pr-1">
        {rows.map(([price, n]) => {
          const inVa = price >= p.val - 1e-6 && price < p.vah, poc = Math.abs(price + p.step / 2 - p.poc) < p.step / 2 + 1e-6;
          const here = px != null && px >= price && px < price + p.step;
          return (
            <div key={price} className="flex items-center gap-1 text-[10px] num leading-[13px]">
              <span className={`w-14 text-right ${here ? "text-gold-400 font-bold" : "text-slate-500"}`}>{price.toFixed(0)}</span>
              <div className="flex-1 h-[9px] bg-ink-800 rounded-sm overflow-hidden">
                <div style={{ width: `${(n / max) * 100}%` }} className={`h-full ${poc ? "bg-sky-400" : inVa ? "bg-sky-700" : "bg-slate-600"}`} />
              </div>
              <span className="w-10 text-slate-500">{compact(n)}</span>
            </div>
          );
        })}
      </div>
    </div>
  );
}

export default function AuctionPanel({ tick }: { tick: number }) {
  const [tf, setTf] = useState<TF>("5m");
  const [a, setA] = useState<AuctionRes | null>(null);
  const [vol, setVol] = useState<VolumeView | null>(null);
  useEffect(() => { api.auction(tf).then(setA).catch(() => setA(null)); api.volume(tf).then(setVol).catch(() => setVol(null)); }, [tf, tick]);
  const r = a?.read;
  const resp = r?.response.kind ? KIND[r.response.kind] : null;
  const px = a?.last_close ?? null;
  const kind = a?.profile_kind?.startsWith("VOLUME") ? "volume, NIFTY FUT" : "TPO";
  const steps: [string, string, React.ReactNode][] = r ? [
    ["1", "Location", r.location.text],
    ["2", "Effort", <span className={r.effort.available ? "" : "text-slate-400"}>{r.effort.text}</span>],
    ["3", "Result", r.result.text],
    ["4", "Response", <span>{resp && <b className={resp[1]}>{resp[0]} · </b>}{r.response.text}</span>],
    ["5", "Decision", <span><b className={r.decision.bias === "bull" ? "text-bull-400" : r.decision.bias === "bear" ? "text-bear-400" : "text-slate-200"}>{r.decision.text}</b> <span className="text-slate-500">({r.decision.note})</span></span>],
  ] : [];
  return (
    <div className="h-full overflow-auto p-3 space-y-3 text-[12px]">
      <div className="flex flex-wrap items-center gap-2">
        {(["5m", "15m", "1h"] as TF[]).map((t) => <button key={t} className={`tab ${t === tf ? "tab-on" : ""}`} onClick={() => setTf(t)}>{t}</button>)}
        <span className="text-[11px] text-slate-500">Read the auction · verify the response · execute your process. Observation only — never opens a trade.</span>
      </div>
      {!a?.available ? <div className="text-slate-500">{a?.message ?? "loading…"}</div> : (
        <>
          <table className="w-full">
            <tbody>
              {steps.map(([n, k, v]) => (
                <tr key={k} className="border-b border-ink-700 align-top">
                  <td className="py-1.5 pr-2 w-6 text-gold-400 font-bold">{n}</td>
                  <td className="py-1.5 pr-3 w-20 text-slate-400">{k}</td>
                  <td className="py-1.5 text-slate-200">{v}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <OiIv v={vol} />
          <div className="grid grid-cols-1 md:grid-cols-[1fr_1fr_1.3fr] gap-3">
            {a.prev ? <Profile p={a.prev} title={`Previous day (${kind})`} px={px} /> : <div className="text-slate-500">No previous-day profile yet.</div>}
            {a.today ? <Profile p={a.today} title={`Today, developing (${kind})`} px={px} /> : <div className="text-slate-500">No candles today yet.</div>}
            <div className="min-w-0 space-y-2">
              <div>
                <div className="text-[11px] text-slate-400 mb-1">References today</div>
                <div className="flex flex-wrap gap-x-3 gap-y-0.5 num">{(a.refs ?? []).map((x) => <span key={x.name}><span className="text-slate-500">{x.name}</span> {x.price.toFixed(1)}</span>)}</div>
                {(a.pending ?? []).length > 0 && <div className="mt-1 text-gold-400">{a.pending!.map((p) => `testing ${p.ref} from ${p.side} since ${p.since}`).join(" · ")}</div>}
              </div>
              <div>
                <div className="text-[11px] text-slate-400 mb-1">Balance (stall) boxes</div>
                {(a.boxes ?? []).length === 0 ? <div className="text-slate-500">none</div> : [...a.boxes!].reverse().map((x) => (
                  <div key={x.start_i} className="num">{x.start}–{x.end} · {x.bars} bars · {x.lo.toFixed(1)}–{x.hi.toFixed(1)} · {x.escaped ? `escaped ${x.escaped}` : x.active ? <b className="text-gold-400">live</b> : "ended"}</div>
                ))}
              </div>
            </div>
          </div>
          <div>
            <div className="text-[11px] text-slate-400 mb-1">Responses at references ({tf}, newest first)</div>
            {(a.events ?? []).length === 0 ? <div className="text-slate-500">none yet</div> : [...a.events!].filter((e) => e.day === a.events![a.events!.length - 1].day).reverse().slice(0, 25).map((e, i) => (
              <div key={i} className="py-0.5 border-b border-ink-800">
                <span className="num text-slate-500">{e.at}</span> <b className={KIND[e.kind][1]}>{KIND[e.kind][0]}{e.probe ? " (probe)" : ""}</b> {e.ref} {e.level.toFixed(1)} — <span className="text-slate-300">{e.text}</span>
              </div>
            ))}
          </div>
          <p className="text-[11px] text-slate-500">{a.profile_kind}. Effort source: {a.delta_label}. Same volume, different story — location and the response decide, not one candle.</p>
        </>
      )}
    </div>
  );
}
