import { useCallback, useEffect, useState } from "react";

type MinuteRow = {
  time: string;
  spot: number | null;
  nifty_ad: string;
  banknifty_ad: string;
  mid_ad: string;
  all_nse_ad: string;
  most_active_oi_top5?: { side: string; strike: number; oi: number; label: string; tone: string }[];
  net_delta_oi_raw: number | null;
  ce_vol_minus_pe_vol: number | null;
  futures_oi: number | null;
  futures_ltp: number | null;
  winner_min: string;
  writer_bias_score: number | null;
  writer_bias_regime: string;
  writer_bias_slope: number | null;
  feed_state: string;
  age_sec: number | null;
};

type GateSig = { signal_id: string; label: string; mode: string; vote: string; audit_reason: string; detail?: string; age_sec?: number };
type Data = {
  state: string;
  age_sec: number | null;
  expiry: string;
  expiries: string[];
  minute_table: MinuteRow[];
  gate_snapshot: { state: string; reason?: string; lanes: Record<string, Record<string, { blocked: boolean; signals: GateSig[] }>> };
};

const fmt = (v: unknown) =>
  v == null ? "—" : typeof v === "number" ? v.toLocaleString("en-IN", { maximumFractionDigits: 4 }) : String(v);

const tone = (vote: string) => (vote === "AGREE" ? "#34d399" : vote === "OPPOSE" ? "#f87171" : "#fbbf24");

export default function DhanLiveSheet() {
  const [data, setData] = useState<Data | null>(null);
  const [error, setError] = useState("");
  const [expiry, setExpiry] = useState("");
  const [tab, setTab] = useState<"minute" | "gates">("minute");

  useEffect(() => {
    let alive = true;
    let timer: ReturnType<typeof setTimeout>;
    const load = async () => {
      try {
        const r = await fetch(`/api/dhan-live-sheet?expiry=${encodeURIComponent(expiry)}`, { cache: "no-store" });
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        const d = (await r.json()) as Data;
        if (alive) {
          setData(d);
          setError("");
        }
      } catch (e) {
        if (alive) setError(String(e));
      } finally {
        if (alive) timer = setTimeout(load, 3000);
      }
    };
    void load();
    return () => {
      alive = false;
      clearTimeout(timer);
    };
  }, [expiry]);

  const exportCsv = useCallback(() => {
    const url = `/api/dhan-live-sheet/minutes.csv?expiry=${encodeURIComponent(expiry)}`;
    const a = document.createElement("a");
    a.href = url;
    a.download = `nifty_minute_sheet_${new Date().toISOString().slice(0, 10)}.csv`;
    a.click();
  }, [expiry]);

  const rows = data?.minute_table ?? [];

  return (
    <section className="dhan-live-sheet h-full overflow-auto p-4 bg-[#0b1220] text-sky-100 text-xs">
      <header className="flex flex-wrap items-center gap-3 mb-3">
        <strong className="text-cyan-300">Dhan live sheet · NIFTY-50 minute log</strong>
        <select
          className="bg-slate-800 border border-slate-600 rounded px-2 py-1"
          value={expiry}
          onChange={(e) => setExpiry(e.target.value)}
        >
          <option value="">Desk expiry</option>
          {data?.expiries?.map((x) => (
            <option key={x}>{x}</option>
          ))}
        </select>
        <button type="button" className="tab" onClick={exportCsv}>
          Export CSV
        </button>
        <span className={error || data?.state !== "RECEIPT_RECENT" ? "text-amber-300" : "text-sky-400"}>
          ● {error ? "UNAVAILABLE" : data?.state ?? "WAITING"} · receipt age {fmt(data?.age_sec)}s
        </span>
      </header>
      <div className="flex gap-2 mb-3">
        <button type="button" className={`tab ${tab === "minute" ? "tab-on" : ""}`} onClick={() => setTab("minute")}>
          Nifty minute table
        </button>
        <button type="button" className={`tab ${tab === "gates" ? "tab-on" : ""}`} onClick={() => setTab("gates")}>
          8510 gate decisions
        </button>
      </div>
      {tab === "gates" ? (
        <div>
          <p className="text-slate-400 mb-2">Mirrors GET /api/dhan-live-sheet/gates on 8510 · BLOCK vetoes fresh OPPOSE only.</p>
          {data?.gate_snapshot?.state !== "AVAILABLE" ? (
            <p className="text-amber-300">{data?.gate_snapshot?.reason}</p>
          ) : (
            Object.entries(data?.gate_snapshot?.lanes ?? {}).map(([lane, sides]) => (
              <article key={lane} className="mb-4 border border-slate-700 rounded p-3">
                <h3 className="font-bold mb-2">{lane.toUpperCase()}</h3>
                <table className="w-full text-left">
                  <thead>
                    <tr>
                      {["Signal", "CE", "PE", "Audit"].map((c) => (
                        <th key={c} className="p-2 text-slate-400">
                          {c}
                        </th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {sides.CE?.signals?.map((s, i) => (
                      <tr key={s.signal_id} className="border-t border-slate-800">
                        <td className="p-2">{s.label}</td>
                        <td style={{ color: tone(s.vote) }}>
                          {s.mode} / {s.vote}
                        </td>
                        <td style={{ color: tone(sides.PE.signals[i]?.vote) }}>
                          {sides.PE.signals[i]?.mode} / {sides.PE.signals[i]?.vote}
                        </td>
                        <td className="p-2">{s.audit_reason}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </article>
            ))
          )}
        </div>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full whitespace-nowrap text-right">
            <thead>
              <tr className="bg-slate-800 text-slate-400">
                {[
                  "Time",
                  "Spot",
                  "Nifty AD",
                  "BankNifty",
                  "Mid",
                  "All NSE",
                  "Top-5 active OI",
                  "Net ΔOI",
                  "CE−PE vol",
                  "Fut OI",
                  "Fut LTP",
                  "Winner",
                  "Δ-wt score",
                  "Regime",
                    "180s slope",
                  "age",
                  "feed",
                ].map((h) => (
                  <th key={h} className="p-2 text-left">
                    {h}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r.time} className="border-b border-slate-800">
                  <td className="p-2 text-left">{r.time}</td>
                  <td className="p-2">{fmt(r.spot)}</td>
                  <td className="p-2">{r.nifty_ad}</td>
                  <td className="p-2">{r.banknifty_ad}</td>
                  <td className="p-2">{r.mid_ad}</td>
                  <td className="p-2">{r.all_nse_ad}</td>
                  <td className="p-2 text-left max-w-[200px] truncate">
                    {(r.most_active_oi_top5 ?? []).map((x) => (
                      <span
                        key={x.label}
                        style={{ color: x.side === "PE" ? "#22c55e" : "#ef4444" }}
                        className="mr-2"
                      >
                        {x.label}
                      </span>
                    ))}
                  </td>
                  <td className="p-2">{fmt(r.net_delta_oi_raw)}</td>
                  <td className="p-2">{fmt(r.ce_vol_minus_pe_vol)}</td>
                  <td className="p-2">{fmt(r.futures_oi)}</td>
                  <td className="p-2">{fmt(r.futures_ltp)}</td>
                  <td className="p-2">{r.winner_min}</td>
                  <td className="p-2">{fmt(r.writer_bias_score)}</td>
                  <td className="p-2">{r.writer_bias_regime}</td>
                  <td className="p-2">{fmt(r.writer_bias_slope)}</td>
                  <td className="p-2">{fmt(r.age_sec)}</td>
                  <td className="p-2" style={{ color: r.feed_state === "LIVE" ? "#38bdf8" : "#fbbf24" }}>
                    {r.feed_state}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {rows.length === 0 && <p className="mt-3 text-slate-400">No minutes recorded yet — observations append each chain poll during market hours.</p>}
        </div>
      )}
      <footer className="mt-4 text-slate-500">Display only · zero orders · BankNifty/Midselect/rho unavailable without verified feeds.</footer>
    </section>
  );
}