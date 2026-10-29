// OptionTimeFootprint.tsx — candle-by-candle option footprint. Time buckets (columns, left→right) × strikes (rows),
// each cell = that side's volume shaded by ESTIMATED buy/sell (green=buy-dominant, red=sell). Per-column footer:
// POC strike, net delta-weighted flow, running CVD. Mirrors the futures footprint but on the strike axis. Est. · no backfill.
import { useEffect, useRef, useState } from "react";
import { api, type OptionFootprint as Fp, type OptFpBucket } from "../api";

const cr = (v: number) => (v >= 1e7 ? `${(v / 1e7).toFixed(2)}Cr` : v >= 1e5 ? `${(v / 1e5).toFixed(1)}L` : `${Math.round(v)}`);
// +5:30 here, NOT in the caller: these panels read /api/option_tape and /api/combined_vol?heat=1,
// which send RAW epoch seconds (unlike most endpoints, which pre-shift with chart_time). Formatting a
// raw epoch with getUTC* printed UTC, so a 09:25 IST candle column was labelled 03:55 and the whole
// tape looked like it covered the evening.
const IST_OFFSET_S = 19800;
const istHM = (ts: number) => { const d = new Date((ts + IST_OFFSET_S) * 1000); return `${String(d.getUTCHours()).padStart(2, "0")}:${String(d.getUTCMinutes()).padStart(2, "0")}`; };

function useFp(which: string, band: number, tf: string, ms = 20000): Fp | null {
  const [f, setF] = useState<Fp | null>(null);
  useEffect(() => {
    let dead = false;
    const go = () => api.optionFootprint(which, band, tf).then((r) => { if (!dead) setF(r); }).catch(() => {});
    go();
    const id = window.setInterval(go, ms);
    return () => { dead = true; window.clearInterval(id); };
  }, [which, band, tf, ms]);
  return f;
}

export default function OptionTimeFootprint() {
  const [which, setWhich] = useState("cur");
  const [band, setBand] = useState(6);
  const [tf, setTf] = useState("5m");
  const [side, setSide] = useState<"ce" | "pe">("ce");
  const fp = useFp(which, band, tf);
  const seen = useRef(false);
  if (fp?.available) seen.current = true;

  const buckets: OptFpBucket[] = fp?.buckets ?? [];
  const strikes = fp?.strikes ?? [];
  // index: strike → ts → cell
  const idx: Record<number, Record<number, { vol: number; delta: number }>> = {};
  let maxVol = 1;
  for (const b of buckets) for (const c of b.cells) {
    const vol = side === "ce" ? c.ce_vol : c.pe_vol;
    const delta = side === "ce" ? c.ce_delta : c.pe_delta;
    (idx[c.strike] ??= {})[b.ts] = { vol, delta };
    if (vol > maxVol) maxVol = vol;
  }

  return (
    <div className="h-full overflow-auto p-3 text-slate-200">
      <div className="mb-2 flex flex-wrap items-center gap-2 text-[11px]">
        <b className="text-indigo-300">⏱ Option footprint · candle-by-candle</b>
        <span className="rounded bg-amber-500/15 px-1.5 py-0.5 text-amber-300">ESTIMATED</span>
        <span className="mx-1 w-px self-stretch bg-ink-700" />
        {["cur", "next"].map((w) => <button key={w} onClick={() => setWhich(w)} className={`tab ${which === w ? "tab-on" : ""}`}>{w === "cur" ? "This week" : "Next week"}</button>)}
        <span className="mx-1 w-px self-stretch bg-ink-700" />
        {["ce", "pe"].map((s) => <button key={s} onClick={() => setSide(s as "ce" | "pe")} className={`tab ${side === s ? "tab-on" : ""} ${s === "ce" ? "!text-rose-300" : "!text-emerald-300"}`}>{s.toUpperCase()}</button>)}
        <span className="mx-1 w-px self-stretch bg-ink-700" />
        {["5m", "15m", "30m", "1h"].map((t) => <button key={t} onClick={() => setTf(t)} className={`tab ${tf === t ? "tab-on" : ""}`}>{t}</button>)}
        <span className="mx-1 w-px self-stretch bg-ink-700" />
        {[6, 10, 15].map((b) => <button key={b} onClick={() => setBand(b)} className={`tab ${band === b ? "tab-on" : ""}`}>±{b}</button>)}
      </div>

      {!fp && <div className="text-[12px] text-slate-500">loading…</div>}
      {fp && !fp.available && <div className="text-[12px] text-amber-300">{fp.error ?? "unavailable"}</div>}
      {fp?.available && buckets.length === 0 && (
        <div className="rounded-lg border border-ink-700 bg-ink-850 p-4 text-[12px] text-slate-400">
          <b className="text-slate-300">No recorded candles yet.</b>
          <p className="mt-1">This footprint is built from the per-minute option tape, which records only while the market
            is open and has no backfill. Columns appear during the next live session ({fp.expiry}).</p>
        </div>
      )}

      {fp?.available && buckets.length > 0 && (
        <div className="overflow-auto">
          <table className="border-collapse text-[10px]">
            <thead>
              <tr>
                <th className="sticky left-0 bg-ink-900 px-1 text-right text-slate-500">{side.toUpperCase()} vol \ time →</th>
                {buckets.map((b) => <th key={b.ts} className="px-1 text-center font-normal text-slate-400">{istHM(b.ts)}</th>)}
              </tr>
            </thead>
            <tbody>
              {strikes.map((s) => (
                <tr key={s}>
                  <td className={`sticky left-0 bg-ink-900 px-1 text-right num ${s === fp.atm ? "text-gold-300 font-semibold" : "text-slate-400"}`}>{s}</td>
                  {buckets.map((b) => {
                    const cell = idx[s]?.[b.ts];
                    if (!cell || cell.vol <= 0) return <td key={b.ts} className="px-1 text-center text-slate-700">·</td>;
                    const buy = cell.delta >= 0;
                    const a = 0.15 + 0.75 * (cell.vol / maxVol);
                    const bg = buy ? `rgba(52,211,153,${a})` : `rgba(251,113,133,${a})`;
                    const isPoc = b.poc === s;
                    return (
                      <td key={b.ts} title={`${s} @ ${istHM(b.ts)} · ${side.toUpperCase()} vol ${cr(cell.vol)} · est Δ ${cell.delta >= 0 ? "+" : ""}${cr(cell.delta)}`}
                        className={`px-1 text-center num ${isPoc ? "ring-1 ring-gold-400" : ""}`} style={{ background: bg }}>
                        {cr(cell.vol)}
                      </td>
                    );
                  })}
                </tr>
              ))}
              <tr className="border-t border-ink-600">
                <td className="sticky left-0 bg-ink-900 px-1 text-right text-slate-500">POC</td>
                {buckets.map((b) => <td key={b.ts} className="px-1 text-center text-gold-300 num">{b.poc ?? "—"}</td>)}
              </tr>
              <tr>
                <td className="sticky left-0 bg-ink-900 px-1 text-right text-slate-500">net Δ (f-eq)</td>
                {buckets.map((b) => <td key={b.ts} className={`px-1 text-center num ${b.net_delta >= 0 ? "text-emerald-300" : "text-rose-300"}`}>{b.net_delta >= 0 ? "+" : ""}{cr(b.net_delta)}</td>)}
              </tr>
              <tr>
                <td className="sticky left-0 bg-ink-900 px-1 text-right text-slate-500">CVD</td>
                {buckets.map((b) => <td key={b.ts} className={`px-1 text-center num ${b.cvd >= 0 ? "text-emerald-400" : "text-rose-400"}`}>{cr(b.cvd)}</td>)}
              </tr>
              <tr>
                <td className="sticky left-0 bg-ink-900 px-1 text-right text-slate-500">vol</td>
                {buckets.map((b) => <td key={b.ts} className="px-1 text-center num text-slate-400">{cr(b.vol)}</td>)}
              </tr>
              <tr className="border-t border-ink-700">
                <td className="sticky left-0 bg-ink-900 px-1 text-right text-slate-500" title="intraday RVOL: bucket volume ÷ the day's running-average bucket">RVOL</td>
                {buckets.map((b) => <td key={b.ts} className={`px-1 text-center num font-semibold ${b.rvol == null ? "text-slate-600" : b.rvol >= 1.5 ? "text-amber-300" : b.rvol >= 1 ? "text-slate-200" : "text-slate-500"}`}>{b.rvol != null ? `${b.rvol.toFixed(1)}×` : "—"}</td>)}
              </tr>
            </tbody>
          </table>
          <p className="mt-2 text-[10px] text-slate-500">
            Each cell = {side.toUpperCase()} volume at that strike in that {tf} bucket; green = estimated buy-dominant,
            red = sell-dominant, brightness = volume. Gold ring = bucket POC (busiest strike). net Δ = delta-weighted
            flow (futures-equivalent); CVD = its running total. {fp.note}
          </p>
        </div>
      )}
    </div>
  );
}
