// QmlPanel.tsx — inspector for the line-chart QML · RBS / SBR layer (observation only — no orders).
import { useEffect, useState } from "react";
import type { QmlSetup, QmlView, TF } from "../api";

const ST: Record<string, string> = {
  TRIGGERED: "bg-gold-500 text-black", "TP1 HIT": "bg-bull-500 text-black", "TP2 HIT": "bg-bull-500 text-black", STOPPED: "bg-bear-500 text-white",
  ARMED: "border border-cyan-400 text-cyan-300", INVALID: "text-slate-500 line-through", EXPIRED: "text-slate-500", "NO-TRADE": "text-amber-300", FAR: "text-slate-500",
};

export default function QmlPanel({ tick }: { tick: number }) {
  const [tf, setTf] = useState<TF>("5m");
  const [d, setD] = useState<(QmlView & { available?: boolean; message?: string }) | null>(null);
  useEffect(() => {
    let dead = false;
    fetch(`/api/qml?tf=${tf}`).then((r) => r.json()).then((j) => { if (!dead) setD(j); }).catch(() => {});
    return () => { dead = true; };
  }, [tf, tick]);
  const rows: QmlSetup[] = [...(d?.setups ?? [])].reverse();
  const n = (v: number | null | undefined) => (v == null ? "—" : v.toLocaleString("en-IN", { maximumFractionDigits: 0 }));
  return (
    <div className="flex h-full flex-col text-[12px]">
      <div className="flex flex-wrap items-center gap-2 border-b border-ink-700 px-3 py-1.5">
        {(["1m", "5m", "15m", "1h"] as TF[]).map((t) => <button key={t} className={`tab ${t === tf ? "tab-on" : ""}`} onClick={() => setTf(t)}>{t}</button>)}
        <span className="text-slate-300">{d?.available === false ? d.message : d?.summary}</span>
      </div>
      <div className="px-3 py-1.5 text-[11px] text-slate-500">
        Line chart = CLOSE prices only (wicks are liquidity). QML: H→L→HH→close below L (bear) / L→H→LL→close above H (bull).
        RBS / SBR: a line peak / valley broken by a body close, traded on the first retest. Information only — not backtested, no orders.
      </div>
      <div className="min-h-0 flex-1 overflow-auto px-3 pb-2">
        {rows.length === 0 && <div className="py-6 text-center text-slate-500">No QML / RBS / SBR structure on {tf} in the loaded candles.</div>}
        <div className="grid gap-2 md:grid-cols-2">
          {rows.map((s, i) => (
            <div key={i} className={`rounded-lg border px-3 py-2 ${s.status === "TRIGGERED" || s.status === "TP1 HIT" ? "border-gold-500" : s.status === "ARMED" ? "border-cyan-500/60" : "border-ink-700 opacity-60"}`}>
              <div className="flex items-center gap-2">
                <b className={s.dir === "bear" ? "text-pink-300" : "text-cyan-300"}>{s.name}</b>
                <span className={`rounded px-1.5 text-[10px] font-bold ${ST[s.status]}`}>{s.status}</span>
                <span className="ml-auto num text-slate-400">armed {s.bos_at}{s.trig_at ? ` · trig ${s.trig_at}` : ""}</span>
              </div>
              <div className={`mt-0.5 font-semibold ${s.dir === "bear" ? "text-bear-400" : "text-bull-400"}`}>{s.side}{s.strike ? ` · ${s.strike}` : ""}</div>
              <div className="num mt-1 grid grid-cols-5 gap-1 text-slate-300">
                <span>Level <b>{n(s.level)}</b></span><span>Entry <b>{n(s.entry)}</b></span>
                <span className="text-bear-400">SL <b>{n(s.sl)}</b></span><span className="text-bull-400">TP1 <b>{n(s.tp1)}</b> <span className="text-slate-500">({s.tp1_r}R)</span></span>
                <span className="text-bull-400">TP2 <b>{n(s.tp2)}</b> <span className="text-slate-500">({s.rr}R)</span></span>
              </div>
              {s.structure.length > 1 && <div className="num mt-1 text-[11px] text-slate-400">Structure: {s.structure.map((x) => `${x.label} ${n(x.price)}`).join(" → ")}</div>}
              {s.veto && <div className="mt-1 text-[11px] text-amber-300">⚠ {s.veto}</div>}
              <div className="mt-1 text-[11px] text-gold-400">→ {s.rule}</div>
              <div className="text-[11px] text-amber-300/80">✖ {s.no_trade}</div>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
