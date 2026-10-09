// AuctionPanel.tsx — "Read the auction, verify the response". The 5 questions (location · effort · result · response ·
// decision), the TPO value profile, balance boxes and the accept / reject log. Observation only — it never trades.
import { useEffect, useState } from "react";
import { api, type AuctionRes, type TF, type TpoProfile } from "../api";

const KIND: Record<string, [string, string]> = {
  AUCTION_ACCEPT: ["Accepted", "text-sky-300"], AUCTION_REJECT: ["Rejected", "text-orange-300"],
  AUCTION_NO_FOLLOW: ["No follow-through", "text-slate-300"], TESTING: ["Testing", "text-gold-400"],
};

function Profile({ p, title, px }: { p: TpoProfile; title: string; px: number | null }) {
  const rows = [...p.hist].reverse();
  const max = Math.max(1, ...rows.map((r) => r[1]));
  return (
    <div className="min-w-0">
      <div className="text-[11px] text-slate-400 mb-1">{title} · POC <b className="text-slate-200">{p.poc.toFixed(1)}</b> · value {p.val.toFixed(0)}–{p.vah.toFixed(0)}</div>
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
              <span className="w-8 text-slate-500">{n}</span>
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
  useEffect(() => { api.auction(tf).then(setA).catch(() => setA(null)); }, [tf, tick]);
  const r = a?.read;
  const resp = r?.response.kind ? KIND[r.response.kind] : null;
  const px = a?.last_close ?? null;
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
          <div className="grid grid-cols-1 md:grid-cols-[1fr_1fr_1.3fr] gap-3">
            {a.prev ? <Profile p={a.prev} title="Previous day (TPO)" px={px} /> : <div className="text-slate-500">No previous-day profile yet.</div>}
            {a.today ? <Profile p={a.today} title="Today, developing (TPO)" px={px} /> : <div className="text-slate-500">No candles today yet.</div>}
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
