// BookPanel.tsx — NIFTY FUT order book: balance gauge + 5-level DOM ladder + PULLED / RELOAD / WALL events. Display only.
import { useEffect, useState } from "react";

type Row = { bid: number; bid_qty: number; bid_orders: number; ask: number; ask_qty: number; ask_orders: number; bid_traded: number[]; ask_traded: number[] };
type Ev = { ts: number; price: number; side: string; kind: "PULLED" | "RELOAD" | "WALL"; qty: number; why: string };
export type BookView = { available: boolean; message?: string; ts?: number; lot?: number; spread?: number | null;
  balance?: { buy_pct: number | null; tot_buy: number; tot_sell: number; trend10: number; top5_buy_pct: number | null };
  series?: { ts: number; bp: number }[]; ladder?: Row[]; events?: Ev[] };

export function useBook(every: number, on = true) {
  const [b, setB] = useState<BookView | null>(null);
  useEffect(() => {
    if (!on) return;
    let dead = false;
    const go = () => fetch("/api/book").then((r) => r.json()).then((j) => { if (!dead) setB(j); }).catch(() => {});
    go(); const id = window.setInterval(go, every);
    return () => { dead = true; window.clearInterval(id); };
  }, [every, on]);
  return b;
}

const hm = (ts: number) => new Date((ts + 19800) * 1000).toISOString().slice(11, 19);
export function balanceRead(bp: number | null | undefined, tr: number) {
  if (bp == null) return { txt: "no book data", tone: "text-slate-400" };
  const lean = bp >= 60 ? "BUYERS stacked" : bp <= 40 ? "SELLERS stacked" : "balanced";
  const move = tr >= 5 ? " · buyers ADDING fast (10 min)" : tr <= -5 ? " · sellers ADDING fast (10 min)" : "";
  return { txt: `${lean}${move}`, tone: bp >= 60 ? "text-bull-400" : bp <= 40 ? "text-bear-400" : "text-slate-300" };
}

// header chip
export function BookChip() {
  const b = useBook(3000);
  if (!b?.available || b.balance?.buy_pct == null) return null;
  const bp = b.balance.buy_pct, r = balanceRead(bp, b.balance.trend10);
  return (
    <span className="group relative pill bg-ink-800 text-[12px]">
      📗 Book <b className="text-bull-400">B {bp.toFixed(0)}%</b> / <b className="text-bear-400">A {(100 - bp).toFixed(0)}%</b>
      <span className="pointer-events-none invisible absolute right-0 top-full z-50 mt-1 w-[360px] rounded-lg border border-emerald-500/40 bg-[#060b16] p-3 text-[13.5px] leading-snug text-slate-100 shadow-2xl group-hover:visible">
        <b className="text-emerald-300">Book balance (NIFTY FUT)</b> — of all quantity waiting to trade, {bp.toFixed(0)} % is on the BUY side.
        <span className={`mt-1 block font-semibold ${r.tone}`}>{r.txt}</span>
        <span className="mt-1 block text-slate-400">Waiting orders can be pulled any second, so this is pressure, not a signal. Strongest when it agrees with price closing above VWAP (buyers) or below (sellers); a lopsided book that price ignores = absorption.</span>
      </span>
    </span>
  );
}

const EV: Record<Ev["kind"], [string, string]> = { PULLED: ["bg-amber-500/90 text-ink-900", "⤺ PULLED"], RELOAD: ["bg-cyan-500/90 text-ink-900", "⟳ RELOAD"], WALL: ["bg-violet-500/90 text-white", "▮ WALL"] };

export default function BookPanel() {
  const b = useBook(1000);
  const [tip, setTip] = useState<string | null>(null);
  if (!b) return <div className="p-4 text-slate-400">loading order book…</div>;
  if (!b.available) return <div className="p-4 text-amber-300">Order book: {b.message}</div>;
  const lot = b.lot ?? 65, L = (q: number) => (q / lot).toFixed(0);
  const rows = b.ladder ?? [], mx = Math.max(...rows.flatMap((r) => [r.bid_qty, r.ask_qty]), 1);
  const bal = b.balance!, r = balanceRead(bal.buy_pct, bal.trend10);
  const ser = (b.series ?? []).slice(-120), W = 340, H = 60;
  const path = ser.map((p, i) => `${i ? "L" : "M"}${(i / Math.max(1, ser.length - 1)) * W},${H - (p.bp / 100) * H}`).join(" ");
  const evs = b.events ?? [];
  const evAt = (side: string, px: number) => evs.find((e) => e.side === side && Math.abs(e.price - px) < 1e-6 && (b.ts ?? 0) - e.ts < 120);
  return (
    <div className="grid h-full grid-cols-1 gap-3 overflow-auto p-3 text-[13px] lg:grid-cols-[1.1fr_1fr]">
      <div>
        <div className="mb-2 rounded-lg border border-ink-700 p-2">
          <div className="flex items-baseline justify-between">
            <b className="text-emerald-300">📗 Book balance · NIFTY FUT</b>
            {/* A frozen timestamp and a broken feed look identical, so say which one this is. The book only
                moves while NSE is sending full packets; outside market hours the last book stays on screen
                and the age below is the honest reading of it. */}
            {(() => {
              const age = b.ts ? Math.floor(Date.now() / 1000) - b.ts : null;
              const stale = age != null && age > 90;
              return <span className="text-[11px] text-slate-500">
                updated {b.ts ? hm(b.ts) : "—"} · spread {b.spread ?? "—"}
                {stale && <span className="ml-1.5 rounded border border-amber-500/60 px-1 font-semibold text-amber-300"
                  title={"The order book updates only while NSE is sending full packets — during market hours.\n\n"
                    + "Outside 09:15–15:30 the last book received stays on screen and nothing moves. That is "
                    + "normal: the prices and sizes below are the real closing book, not a fault.\n\n"
                    + "If this badge is still showing DURING market hours, the futures feed is genuinely down."}>
                  {age >= 3600 ? `${Math.floor(age / 3600)}h` : `${Math.floor(age / 60)}m`} old · feed quiet
                </span>}
              </span>;
            })()}
          </div>
          <div className="mt-1 flex h-5 overflow-hidden rounded">
            <div className="bg-bull-500 text-center text-[12px] font-bold text-ink-900" style={{ width: `${bal.buy_pct ?? 50}%` }}>B {bal.buy_pct?.toFixed(0)}%</div>
            <div className="flex-1 bg-bear-500 text-center text-[12px] font-bold text-white">A {(100 - (bal.buy_pct ?? 50)).toFixed(0)}%</div>
          </div>
          <div className={`mt-1 font-semibold ${r.tone}`}>{r.txt}</div>
          <svg width={W} height={H} className="mt-1 rounded bg-ink-900">
            <line x1={0} x2={W} y1={H / 2} y2={H / 2} stroke="#475569" strokeDasharray="4 3" />
            <path d={path} fill="none" stroke="#34d399" strokeWidth={1.5} />
          </svg>
          <div className="text-[11px] text-slate-500">buy share per minute (line above the dashed 50 % = buyers stacked) · top-5 levels only: B {bal.top5_buy_pct ?? "—"}%</div>
        </div>
        <table className="w-full num">
          <thead><tr className="text-[11px] text-slate-500">
            <th className="text-right">traded S</th><th className="text-right">orders</th><th className="text-right">bid lots</th><th className="text-center">bid</th>
            <th className="text-center">ask</th><th className="text-left">ask lots</th><th className="text-left">orders</th><th className="text-left">traded B</th></tr></thead>
          <tbody>{rows.map((x, i) => { const eb = evAt("bid", x.bid), ea = evAt("ask", x.ask); return (
            <tr key={i} className="border-t border-ink-800">
              <td className="text-right text-bear-400">{x.bid_traded[1] ? L(x.bid_traded[1]) : ""}</td>
              <td className="text-right text-slate-500">{x.bid_orders}</td>
              <td className="relative text-right" onMouseEnter={() => setTip(eb?.why ?? `${L(x.bid_qty)} lots waiting to BUY at ${x.bid} from ${x.bid_orders} orders${x.bid_qty / Math.max(1, x.bid_orders) > 3 * lot ? " — big average order size: one large player" : ""}.`)} onMouseLeave={() => setTip(null)}>
                <span className="absolute inset-y-0.5 right-0 rounded-l bg-bull-500/30" style={{ width: `${(x.bid_qty / mx) * 100}%` }} />
                <span className="relative font-semibold text-bull-400">{eb && <span className={`mr-1 rounded px-1 text-[10px] ${EV[eb.kind][0]}`}>{EV[eb.kind][1]}</span>}{L(x.bid_qty)}</span></td>
              <td className="text-center font-semibold text-slate-100">{x.bid.toFixed(2)}</td>
              <td className="text-center font-semibold text-slate-100">{x.ask.toFixed(2)}</td>
              <td className="relative text-left" onMouseEnter={() => setTip(ea?.why ?? `${L(x.ask_qty)} lots waiting to SELL at ${x.ask} from ${x.ask_orders} orders${x.ask_qty / Math.max(1, x.ask_orders) > 3 * lot ? " — big average order size: one large player" : ""}.`)} onMouseLeave={() => setTip(null)}>
                <span className="absolute inset-y-0.5 left-0 rounded-r bg-bear-500/30" style={{ width: `${(x.ask_qty / mx) * 100}%` }} />
                <span className="relative font-semibold text-bear-400">{L(x.ask_qty)}{ea && <span className={`ml-1 rounded px-1 text-[10px] ${EV[ea.kind][0]}`}>{EV[ea.kind][1]}</span>}</span></td>
              <td className="text-left text-slate-500">{x.ask_orders}</td>
              <td className="text-left text-bull-400">{x.ask_traded[0] ? L(x.ask_traded[0]) : ""}</td>
            </tr>); })}</tbody>
        </table>
        <div className="mt-1 min-h-[40px] rounded bg-ink-850 px-2 py-1 text-[13px] text-cyan-100">{tip ?? "Hover a size to read it. Lots = qty ÷ " + lot + ". Traded B / S = lots bought at the ask / sold at the bid at that price today (estimated by the quote rule)."}</div>
      </div>
      <div>
        <b className="text-slate-200">Book events (last 60)</b>
        <div className="mt-1 text-[11.5px] text-slate-400">⤺ PULLED = size cancelled without trading (fake wall) · ⟳ RELOAD = hit and refilled (defended) · ▮ WALL = big size held 20 s+</div>
        <div className="mt-1 space-y-1">{evs.length ? evs.map((e, i) => (
          <div key={i} className="rounded border border-ink-800 px-2 py-1">
            <span className="text-slate-500">{hm(e.ts)}</span> <span className={`rounded px-1 text-[11px] font-bold ${EV[e.kind][0]}`}>{EV[e.kind][1]}</span>{" "}
            <b className={e.side === "bid" ? "text-bull-400" : "text-bear-400"}>{e.side} {e.price.toFixed(2)}</b> <span className="text-slate-300">{L(e.qty)} lots</span>
            <div className="text-[12.5px] text-slate-200">{e.why}</div>
          </div>)) : <div className="text-slate-500">no events yet — they appear during market hours.</div>}</div>
        <div className="mt-2 text-[11.5px] text-amber-200/80">Only the best 5 levels are visible (that's what Dhan's Full packet carries), so walls and pulls are judged right at the touch. NSE gives retail feeds no order ids — RELOAD is iceberg-LIKE behaviour, not a confirmed iceberg.</div>
      </div>
    </div>
  );
}
