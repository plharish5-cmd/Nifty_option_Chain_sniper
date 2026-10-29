// OptionTape.tsx — REAL CE/PE option tape. Footprint ladder (CE vol left · strike · PE vol right, with OI/ΔOI and
// build-unwind colour) and a strike×time volume heatmap, both from /api/option_tape (actual option volume, not the
// futures estimate). Greek delta is Dhan's own value; Dhan returns 0 for ITM-call greeks, shown as "—". Display only.
import { useEffect, useRef, useState } from "react";
import { api, type OptionTape as Tape, type OptTapeLadder, type OptTapeLeg, type OptAggExp } from "../api";
import OptionTimeFootprint from "./OptionTimeFootprint";

const cr = (v: number) => (v >= 1e7 ? `${(v / 1e7).toFixed(2)}Cr` : v >= 1e5 ? `${(v / 1e5).toFixed(1)}L` : `${Math.round(v)}`);
// +5:30 here, NOT in the caller: these panels read /api/option_tape and /api/combined_vol?heat=1,
// which send RAW epoch seconds (unlike most endpoints, which pre-shift with chart_time). Formatting a
// raw epoch with getUTC* printed UTC, so a 09:25 IST candle column was labelled 03:55 and the whole
// tape looked like it covered the evening.
const IST_OFFSET_S = 19800;
const istHM = (ts: number) => { const d = new Date((ts + IST_OFFSET_S) * 1000); return `${String(d.getUTCHours()).padStart(2, "0")}:${String(d.getUTCMinutes()).padStart(2, "0")}`; };

// build/unwind → colour (writers adding = bright, covering = faded), mirroring the OI-wall coloring
function legTone(leg: OptTapeLeg, base: string): string {
  const fresh = leg.oi > 0 ? leg.oi_chg / leg.oi : 0;
  if (leg.oi_chg > 0 && fresh >= 0.12) return base;          // building
  if (leg.oi_chg < 0 && fresh <= -0.1) return base + "40";   // unwinding (faded)
  return base + "99";
}

function useTape(which: string, band: number, heat: boolean, flow: boolean, ms = 20000): Tape | null {
  const [t, setT] = useState<Tape | null>(null);
  useEffect(() => {
    let dead = false;
    const go = () => api.optionTape(which, band, heat ? 1 : 0, flow ? 1 : 0).then((r) => { if (!dead) setT(r); }).catch(() => {});
    go();
    const id = window.setInterval(go, ms);
    return () => { dead = true; window.clearInterval(id); };
  }, [which, band, heat, flow, ms]);
  return t;
}

function Ladder({ L }: { L: OptTapeLadder }) {
  const maxVol = Math.max(1, ...L.ladder.flatMap((r) => [r.ce.vol, r.pe.vol]));
  return (
    <div className="mb-4">
      <div className="mb-1 flex items-center gap-3 text-[12px]">
        <b className="text-slate-200">{L.expiry}</b>
        <span className="text-slate-400">spot {L.spot?.toFixed(0)} · ATM {L.atm}</span>
        <span className="ml-auto flex gap-3">
          <span className="text-rose-300">CE vol {cr(L.totals.ce_vol)}</span>
          <span className="text-emerald-300">PE vol {cr(L.totals.pe_vol)}</span>
          <span className="text-slate-300">vol-PCR {L.totals.vol_pcr ?? "—"}</span>
          <span className="text-slate-400">OI-PCR {L.totals.oi_pcr ?? "—"}</span>
        </span>
      </div>
      <table className="w-full border-collapse text-[11px]">
        <thead className="text-slate-500">
          <tr>
            <th className="text-right font-normal">CE Δ</th><th className="text-right font-normal">CE ΔOI</th>
            <th className="text-right font-normal">CE OI</th><th className="text-right font-normal">CE vol</th>
            <th className="w-28 text-center font-normal"></th>
            <th className="text-left font-normal">PE vol</th><th className="text-left font-normal">PE OI</th>
            <th className="text-left font-normal">PE ΔOI</th><th className="text-left font-normal">PE Δ</th>
          </tr>
        </thead>
        <tbody>
          {L.ladder.map((r) => {
            const atm = r.zone === "ATM";
            const ced = r.ce.delta && r.ce.delta !== 0 ? r.ce.delta.toFixed(2) : "—";
            const ped = r.pe.delta && r.pe.delta !== 0 ? r.pe.delta.toFixed(2) : "—";
            return (
              <tr key={r.strike} className={atm ? "bg-gold-500/10" : ""}>
                <td className="text-right text-slate-500 num">{ced}</td>
                <td className={`text-right num ${r.ce.oi_chg >= 0 ? "text-rose-300" : "text-slate-500"}`}>{r.ce.oi_chg >= 0 ? "+" : ""}{cr(r.ce.oi_chg)}</td>
                <td className="text-right text-slate-400 num">{cr(r.ce.oi)}</td>
                <td className="relative text-right num text-slate-200">
                  <span className="absolute inset-y-0.5 right-0 rounded-sm" style={{ width: `${(r.ce.vol / maxVol) * 100}%`, background: legTone(r.ce, "#fb718560") }} />
                  <span className="relative pr-1">{cr(r.ce.vol)}</span>
                </td>
                <td className={`text-center font-semibold num ${atm ? "text-gold-300" : "text-slate-300"}`}>{r.strike}</td>
                <td className="relative text-left num text-slate-200">
                  <span className="absolute inset-y-0.5 left-0 rounded-sm" style={{ width: `${(r.pe.vol / maxVol) * 100}%`, background: legTone(r.pe, "#34d39960") }} />
                  <span className="relative pl-1">{cr(r.pe.vol)}</span>
                </td>
                <td className="text-left text-slate-400 num">{cr(r.pe.oi)}</td>
                <td className={`text-left num ${r.pe.oi_chg >= 0 ? "text-emerald-300" : "text-slate-500"}`}>{r.pe.oi_chg >= 0 ? "+" : ""}{cr(r.pe.oi_chg)}</td>
                <td className="text-left text-slate-500 num">{ped}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function Heatmap({ tape }: { tape: Tape }) {
  const hm = tape.heatmap;
  if (!hm) return null;
  const blocks = Object.entries(hm);
  const anyData = blocks.some(([, m]) => Object.keys(m).length > 0);
  if (!anyData)
    return (
      <div className="rounded-lg border border-ink-700 bg-ink-850 p-4 text-[12px] text-slate-400">
        <b className="text-slate-300">Volume heatmap — tape is still filling.</b>
        <p className="mt-1">Per-minute option volume records only while the market is open and has no backfill, so the grid
          populates during the next live session. Strike × time cells will show where option volume concentrates.</p>
      </div>
    );
  return (
    <div className="space-y-4">
      {blocks.map(([exp, m]) => {
        const strikes = Object.keys(m).map(Number).sort((a, b) => b - a);
        const times = Array.from(new Set(Object.values(m).flatMap((a) => a.map((p) => p.ts)))).sort((a, b) => a - b);
        const maxV = Math.max(1, ...Object.values(m).flatMap((a) => a.map((p) => p.v)));
        const at: Record<string, Record<number, number>> = {};
        for (const [s, arr] of Object.entries(m)) { at[s] = {}; for (const p of arr) at[s][p.ts] = p.v; }
        return (
          <div key={exp}>
            <div className="mb-1 text-[12px] font-semibold text-slate-200">{exp}</div>
            <div className="overflow-auto">
              <table className="border-collapse text-[9px]">
                <tbody>
                  {strikes.map((s) => (
                    <tr key={s}>
                      <td className="pr-1 text-right text-slate-400 num sticky left-0 bg-ink-900">{s}</td>
                      {times.map((t) => {
                        const v = at[String(s)]?.[t] ?? 0;
                        const a = v > 0 ? 0.12 + 0.88 * (v / maxV) : 0;
                        return <td key={t} title={`${s} @ ${istHM(t)} · ${cr(v)}`} style={{ background: `rgba(99,102,241,${a})`, width: 6, height: 11 }} />;
                      })}
                    </tr>
                  ))}
                  <tr><td /> {times.filter((_, i) => i % 15 === 0).map((t) => <td key={t} colSpan={15} className="pt-0.5 text-left text-slate-500">{istHM(t)}</td>)}</tr>
                </tbody>
              </table>
            </div>
          </div>
        );
      })}
    </div>
  );
}

function Flow({ tape }: { tape: Tape }) {
  const agg = tape.aggression;
  if (!agg) return null;
  const exps = Object.keys(agg).filter((k) => !k.startsWith("_"));
  const anyData = exps.some((e) => (agg[e] as OptAggExp)?.ladder?.length);
  const net = agg._net_flow_total ?? 0;
  return (
    <div className="space-y-4">
      <div className="rounded-lg border border-amber-500/40 bg-amber-500/5 px-3 py-2 text-[11px] text-amber-200/90">
        <b>Estimated.</b> {agg._note}
      </div>
      <div className="flex items-center gap-2 text-[12px]">
        <span className="text-slate-300">Net directional option flow (futures-equivalent):</span>
        <b className={`num ${net > 0 ? "text-emerald-300" : net < 0 ? "text-rose-300" : "text-slate-400"}`}>
          {net > 0 ? "▲ +" : net < 0 ? "▼ " : ""}{cr(Math.abs(net))} {net > 0 ? "bullish" : net < 0 ? "bearish" : "flat"}
        </b>
      </div>
      {!anyData && (
        <div className="rounded-lg border border-ink-700 bg-ink-850 p-4 text-[12px] text-slate-400">
          <b className="text-slate-300">No aggression recorded yet.</b>
          <p className="mt-1">Buy/sell classification needs consecutive live snapshots (bid/ask + volume), so this fills
            during the next market session. It has no backfill.</p>
        </div>
      )}
      {exps.map((e) => {
        const a = agg[e] as OptAggExp;
        if (!a?.ladder?.length) return null;
        const mx = Math.max(1, ...a.ladder.flatMap((r) => [Math.abs(r.ce_delta), Math.abs(r.pe_delta)]));
        return (
          <div key={e}>
            <div className="mb-1 flex items-center gap-3 text-[12px]">
              <b className="text-slate-200">{e}</b>
              <span className="ml-auto flex gap-3">
                <span className="text-rose-300">CE Δ {a.totals.ce_delta >= 0 ? "+" : ""}{cr(a.totals.ce_delta)}</span>
                <span className="text-emerald-300">PE Δ {a.totals.pe_delta >= 0 ? "+" : ""}{cr(a.totals.pe_delta)}</span>
              </span>
            </div>
            <table className="w-full border-collapse text-[11px]">
              <thead className="text-slate-500"><tr>
                <th className="text-right font-normal">CE sell</th><th className="text-right font-normal">CE buy</th>
                <th className="w-24 text-center font-normal">strike</th>
                <th className="text-left font-normal">PE buy</th><th className="text-left font-normal">PE sell</th>
              </tr></thead>
              <tbody>
                {a.ladder.map((r) => (
                  <tr key={r.strike}>
                    <td className="relative text-right num text-slate-300">
                      <span className="absolute inset-y-0.5 right-0 rounded-sm bg-slate-500/30" style={{ width: `${(r.ce_sell / (r.ce_buy + r.ce_sell || 1)) * 50}%` }} />
                      <span className="relative pr-1">{cr(r.ce_sell)}</span>
                    </td>
                    <td className="relative text-right num text-rose-200">
                      <span className="absolute inset-y-0.5 right-0 rounded-sm bg-rose-500/30" style={{ width: `${(Math.abs(r.ce_delta) / mx) * 100}%` }} />
                      <span className="relative pr-1">{cr(r.ce_buy)}</span>
                    </td>
                    <td className="text-center font-semibold num text-slate-300">{r.strike}</td>
                    <td className="relative text-left num text-emerald-200">
                      <span className="absolute inset-y-0.5 left-0 rounded-sm bg-emerald-500/30" style={{ width: `${(Math.abs(r.pe_delta) / mx) * 100}%` }} />
                      <span className="relative pl-1">{cr(r.pe_buy)}</span>
                    </td>
                    <td className="relative text-left num text-slate-300">
                      <span className="absolute inset-y-0.5 left-0 rounded-sm bg-slate-500/30" style={{ width: `${(r.pe_sell / (r.pe_buy + r.pe_sell || 1)) * 50}%` }} />
                      <span className="relative pl-1">{cr(r.pe_sell)}</span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            {a.cvd.length > 0 && (
              <div className="mt-1 text-[10px] text-slate-500">
                CVD net (futures-equiv) latest: <b className={a.cvd[a.cvd.length - 1].net >= 0 ? "text-emerald-300" : "text-rose-300"}>{cr(a.cvd[a.cvd.length - 1].net)}</b>
                {" "}· {a.cvd.length} min recorded
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}

function BigTrades() {
  const [which, setWhich] = useState("cur");
  const [mult, setMult] = useState(3);
  const [d, setD] = useState<import("../api").OptionBigtrades | null>(null);
  useEffect(() => {
    let dead = false;
    const go = () => api.optionBigtrades(which, 10, mult).then((r) => { if (!dead) setD(r); }).catch(() => {});
    go();
    const id = window.setInterval(go, 20000);
    return () => { dead = true; window.clearInterval(id); };
  }, [which, mult]);
  const ev = d?.events ?? [];
  return (
    <div>
      <div className="mb-2 flex flex-wrap items-center gap-2 text-[11px]">
        <span className="rounded bg-amber-500/15 px-1.5 py-0.5 text-amber-300">ESTIMATED</span>
        {["cur", "next"].map((w) => <button key={w} onClick={() => setWhich(w)} className={`tab ${which === w ? "tab-on" : ""}`}>{w === "cur" ? "This week" : "Next week"}</button>)}
        <span className="mx-1 w-px self-stretch bg-ink-700" />
        <span className="text-slate-400">burst ≥</span>
        {[2, 3, 5].map((m) => <button key={m} onClick={() => setMult(m)} className={`tab ${mult === m ? "tab-on" : ""}`}>{m}×</button>)}
      </div>
      {d && !d.available && <div className="text-[12px] text-amber-300">{d.error}</div>}
      {d?.available && ev.length === 0 && (
        <div className="rounded-lg border border-ink-700 bg-ink-850 p-4 text-[12px] text-slate-400">
          <b className="text-slate-300">No bursts yet.</b>
          <p className="mt-1">A burst = a minute where a strike's option volume ≥ {mult}× its own median minute today.
            Needs live recorded minutes, so this fills during the next session. No backfill.</p>
        </div>
      )}
      {ev.length > 0 && (
        <table className="w-full border-collapse text-[11px]">
          <thead className="text-slate-500"><tr>
            <th className="text-left font-normal">time</th><th className="text-right font-normal">strike</th>
            <th className="text-center font-normal">side</th><th className="text-right font-normal">volume</th>
            <th className="text-right font-normal">× median</th><th className="text-center font-normal">est</th>
          </tr></thead>
          <tbody>
            {ev.map((e, i) => (
              <tr key={i} className="border-t border-ink-800">
                <td className="text-left text-slate-400 num">{istHM(e.ts)}</td>
                <td className="text-right num text-slate-200">{e.strike}</td>
                <td className={`text-center font-semibold ${e.side === "CE" ? "text-rose-300" : "text-emerald-300"}`}>{e.side}</td>
                <td className="text-right num text-slate-200">{cr(e.vol)}</td>
                <td className="text-right num text-amber-300">{e.mult}×</td>
                <td className={`text-center ${e.est === "buy" ? "text-emerald-400" : e.est === "sell" ? "text-rose-400" : "text-slate-500"}`}>{e.est === "buy" ? "▲ buy" : e.est === "sell" ? "▼ sell" : "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {d?.note && <p className="mt-2 text-[10px] text-slate-500">{d.note}</p>}
    </div>
  );
}

export default function OptionTape() {
  const [which, setWhich] = useState("both");
  const [band, setBand] = useState(6);
  const [view, setView] = useState<"ladder" | "tfoot" | "heat" | "flow" | "big">("ladder");
  const tape = useTape(which, band, view === "heat", view === "flow");
  const seen = useRef(false);
  if (tape?.available) seen.current = true;

  return (
    <div className="h-full overflow-auto p-3 text-slate-200">
      <div className="mb-2 flex flex-wrap items-center gap-2 text-[11px]">
        <b className="text-indigo-300">🪜 Option tape · real CE/PE volume</b>
        <span className="mx-1 w-px self-stretch bg-ink-700" />
        {(["ladder", "tfoot", "heat", "flow", "big"] as const).map((v) => (
          <button key={v} onClick={() => setView(v)} className={`tab ${view === v ? "tab-on" : ""}`}>{v === "ladder" ? "Footprint ladder" : v === "tfoot" ? "⏱ Candle-by-candle" : v === "heat" ? "Heatmap" : v === "flow" ? "Delta / CVD (est.)" : "💥 Big trades"}</button>
        ))}
        <span className="mx-1 w-px self-stretch bg-ink-700" />
        {["cur", "next", "both"].map((w) => (
          <button key={w} onClick={() => setWhich(w)} className={`tab ${which === w ? "tab-on" : ""}`}>{w === "cur" ? "This week" : w === "next" ? "Next week" : "Both"}</button>
        ))}
        <span className="mx-1 w-px self-stretch bg-ink-700" />
        {[6, 10, 15].map((b) => (
          <button key={b} onClick={() => setBand(b)} className={`tab ${band === b ? "tab-on" : ""}`}>±{b}</button>
        ))}
        {tape?.option_crvol && (
          <span className="ml-auto rounded border border-indigo-500/50 bg-indigo-500/10 px-1.5 py-0.5 text-indigo-300"
            title={tape.option_crvol.text ?? ""}>
            Opt cRVOL {tape.option_crvol.value != null ? `${tape.option_crvol.value.toFixed(2)}×` : tape.option_crvol.state}
          </span>
        )}
      </div>

      {view !== "tfoot" && view !== "big" && !tape && <div className="text-[12px] text-slate-500">loading option tape…</div>}
      {view !== "tfoot" && view !== "big" && tape && !tape.available && <div className="text-[12px] text-amber-300">option tape unavailable: {tape.error ?? "no chain yet"}</div>}

      {tape?.available && view === "ladder" && (
        <>
          {tape.ladders?.map((L) => <Ladder key={L.expiry} L={L} />)}
          <p className="mt-1 text-[10px] text-slate-500">
            Volume bars = real traded option volume (CE red · PE green). Bar brightness follows OI: bright = writers building,
            faded = unwinding/covering. CE Δ shows “—” where Dhan returns no ITM-call greek. ATM row highlighted.
          </p>
        </>
      )}
      {view === "tfoot" && <OptionTimeFootprint />}
      {view === "big" && <BigTrades />}
      {tape?.available && view === "heat" && <Heatmap tape={tape} />}
      {tape?.available && view === "flow" && <Flow tape={tape} />}
    </div>
  );
}
