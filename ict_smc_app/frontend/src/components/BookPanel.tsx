// BookPanel.tsx — NIFTY FUT Order Book Studio: Depth Balance + 5-Level DOM Ladder + Wall Badges & Event Radar.
import { useEffect, useState } from "react";

export type Row = {
  bid: number;
  bid_qty: number;
  bid_lots?: number;
  bid_orders: number;
  bid_is_wall?: boolean;
  ask: number;
  ask_qty: number;
  ask_lots?: number;
  ask_orders: number;
  ask_is_wall?: boolean;
  bid_traded: number[];
  bid_traded_lots?: number[];
  ask_traded: number[];
  ask_traded_lots?: number[];
  guidance_bid?: string;
  guidance_ask?: string;
};

export type Ev = {
  ts: number;
  price: number;
  side: string;
  kind: "PULLED" | "RELOAD" | "WALL" | "SWEEP" | "SPOOF";
  qty: number;
  why: string;
  source?: string;
};

export type BookView = {
  available: boolean;
  message?: string;
  ts?: number;
  lot?: number;
  spread?: number | null;
  balance?: {
    buy_pct: number | null;
    top5_buy_pct: number | null;
    top5_sell_pct?: number | null;
    exchange_buy_pct?: number | null;
    tot_buy: number;
    tot_sell: number;
    trend10: number;
    top5_bid_lots?: number;
    top5_ask_lots?: number;
    imbalance_ratio?: number;
    verdict?: string;
    agg_buy_pct_15m?: number;
    agg_sell_pct_15m?: number;
  };
  series?: { ts: number; bp: number }[];
  ladder?: Row[];
  events?: Ev[];
};

export function useBook(every = 1000, on = true) {
  const [b, setB] = useState<BookView | null>(null);
  useEffect(() => {
    if (!on) return;
    let dead = false;
    const go = () =>
      fetch("/api/book")
        .then((r) => r.json())
        .then((j) => {
          if (!dead) setB(j);
        })
        .catch(() => {});
    go();
    const id = window.setInterval(go, every);
    return () => {
      dead = true;
      window.clearInterval(id);
    };
  }, [every, on]);
  return b;
}

const hm = (ts: number) => {
  try {
    return new Date((ts + 19800) * 1000).toISOString().slice(11, 19);
  } catch {
    return "—";
  }
};

export function balanceRead(bp: number | null | undefined, tr: number) {
  if (bp == null) return { txt: "no book data", tone: "text-slate-400" };
  const lean = bp >= 60 ? "BUYERS stacked" : bp <= 40 ? "SELLERS stacked" : "balanced";
  const move = tr >= 5 ? " · buyers ADDING fast (10 min)" : tr <= -5 ? " · sellers ADDING fast (10 min)" : "";
  return {
    txt: `${lean}${move}`,
    tone: bp >= 60 ? "text-bull-400" : bp <= 40 ? "text-bear-400" : "text-slate-300",
  };
}

// Header chip
export function BookChip() {
  const b = useBook(3000);
  if (!b?.available || b.balance?.buy_pct == null) return null;
  const bp = b.balance.top5_buy_pct ?? b.balance.buy_pct ?? 50;
  const r = balanceRead(bp, b.balance.trend10);
  return (
    <span className="group relative pill bg-ink-800 text-[12px]">
      📗 Book <b className="text-bull-400">B {bp.toFixed(0)}%</b> / <b className="text-bear-400">A {(100 - bp).toFixed(0)}%</b>
      <span className="pointer-events-none invisible absolute right-0 top-full z-50 mt-1 w-[380px] rounded-lg border border-emerald-500/40 bg-[#060b16] p-3 text-[13.5px] leading-snug text-slate-100 shadow-2xl group-hover:visible">
        <b className="text-emerald-300">Book Balance (NIFTY FUT Top-5 Depth)</b> — of all active quantity on the best 5 bids vs asks, {bp.toFixed(1)}% is on the BUY side.
        <span className={`mt-1 block font-semibold ${r.tone}`}>{r.txt}</span>
        <span className="mt-1 block text-slate-400">
          Waiting orders can be pulled any second, so this is passive pressure. Strongest when aligned with price closing above VWAP (buyers) or below (sellers).
        </span>
      </span>
    </span>
  );
}

const EV: Record<string, [string, string, string]> = {
  PULLED: ["bg-amber-500/90 text-ink-900 border border-amber-400", "⤺ PULLED", "Fake wall / spoof order cancelled without executing."],
  RELOAD: ["bg-cyan-500/90 text-ink-900 border border-cyan-400", "⟳ RELOAD", "Iceberg absorption: level was hit and instantly refilled by an institutional defender."],
  WALL: ["bg-violet-600 text-white font-bold border border-violet-400", "🧱 WALL", "Large resting limit order (>=15 lots) defending this price level."],
  SWEEP: ["bg-fuchsia-600 text-white font-bold border border-fuchsia-400", "⚡ SWEEP", "Aggressive market order swept multiple depth levels in one burst."],
  SPOOF: ["bg-rose-600 text-white font-bold border border-rose-400", "⚠️ SPOOF", "Repeated fake wall pulled within seconds — expect rapid breakout through this level."],
};

export default function BookPanel() {
  const b = useBook(1000);
  const [tip, setTip] = useState<string | null>(null);

  if (!b) return <div className="p-4 text-slate-400">Loading order book & tape...</div>;
  if (!b.available) return <div className="p-4 text-amber-300">Order book: {b.message}</div>;

  const lot = b.lot ?? 65;
  const L = (q: number) => (q / lot).toFixed(0);
  const rows = b.ladder ?? [];
  const mx = Math.max(...rows.flatMap((r) => [r.bid_qty, r.ask_qty]), 1);
  const bal = b.balance!;
  const top5Bp = bal.top5_buy_pct ?? bal.buy_pct ?? 50;
  const top5Ap = bal.top5_sell_pct ?? 100 - top5Bp;
  const r = balanceRead(top5Bp, bal.trend10);

  const ser = (b.series ?? []).slice(-120);
  const W = 380;
  const H = 64;
  const path = ser.map((p, i) => `${i ? "L" : "M"}${(i / Math.max(1, ser.length - 1)) * W},${H - (p.bp / 100) * H}`).join(" ");
  const areaPath = ser.length > 1 ? `${path} L${W},${H} L0,${H} Z` : "";

  const evs = b.events ?? [];
  const evAt = (side: string, px: number) =>
    evs.find((e) => e.side === side && Math.abs(e.price - px) < 1e-4 && (b.ts ?? 0) - e.ts < 180);

  return (
    <div className="grid h-full grid-cols-1 gap-3 overflow-auto p-3 text-[13px] lg:grid-cols-[1.15fr_1fr]">
      {/* Left Column: Balance, Tape Aggression, Ladder */}
      <div>
        {/* Top Balance Card */}
        <div className="mb-2 rounded-lg border border-ink-700 bg-ink-900/40 p-2.5">
          <div className="flex items-baseline justify-between">
            <div className="flex items-center gap-2">
              <b className="text-emerald-300">📗 Book balance · NIFTY FUT</b>
              {bal.verdict && (
                <span className={`rounded px-1.5 py-0.5 text-[11px] font-bold ${top5Bp >= 60 ? "bg-emerald-500/20 text-emerald-300 border border-emerald-500/40" : top5Bp <= 40 ? "bg-rose-500/20 text-rose-300 border border-rose-500/40" : "bg-slate-800 text-slate-300"}`}>
                  {bal.verdict}
                </span>
              )}
            </div>

            {(() => {
              const age = b.ts ? Math.floor(Date.now() / 1000) - b.ts : null;
              const stale = age != null && age > 90;
              return (
                <span className="text-[11px] text-slate-400">
                  updated {b.ts ? hm(b.ts) : "—"} · spread <span className="font-semibold text-slate-200">{b.spread ?? "0.05"}</span>
                  {stale && (
                    <span
                      className="ml-1.5 rounded border border-amber-500/60 bg-amber-500/10 px-1 font-semibold text-amber-300"
                      title="Outside market hours the last book received stays on screen and historical session telemetry is backfilled."
                    >
                      {age >= 3600 ? `${Math.floor(age / 3600)}h` : `${Math.floor(age / 60)}m`} old · session feed quiet
                    </span>
                  )}
                </span>
              );
            })()}
          </div>

          {/* Primary Top-5 Depth Balance Bar */}
          <div className="mt-2 flex h-6 overflow-hidden rounded border border-ink-700 shadow-inner">
            <div
              className="flex items-center justify-center bg-bull-500 text-[12px] font-bold text-ink-950 transition-all duration-300"
              style={{ width: `${top5Bp}%` }}
              title={`Top-5 Depth Buy Share: ${top5Bp}%`}
            >
              B {top5Bp.toFixed(0)}% {bal.imbalance_ratio && bal.imbalance_ratio >= 1.5 ? `(+${bal.imbalance_ratio}x)` : ""}
            </div>
            <div
              className="flex flex-1 items-center justify-center bg-bear-500 text-[12px] font-bold text-white transition-all duration-300"
              title={`Top-5 Depth Sell Share: ${top5Ap}%`}
            >
              A {top5Ap.toFixed(0)}%
            </div>
          </div>

          {/* Top-5 Stats Line */}
          <div className="mt-1.5 flex items-center justify-between text-[11px] text-slate-400">
            <span>
              Top-5 Depth: <b className="text-bull-400">{bal.top5_bid_lots ?? L(bal.tot_buy)} Bid Lots ({top5Bp.toFixed(1)}%)</b> vs <b className="text-bear-400">{bal.top5_ask_lots ?? L(bal.tot_sell)} Ask Lots ({top5Ap.toFixed(1)}%)</b>
            </span>
            <span className={`font-semibold ${r.tone}`}>{r.txt}</span>
          </div>

          {/* 15m Traded Tape Aggression Mini-Bar */}
          {bal.agg_buy_pct_15m != null && (
            <div className="mt-2 rounded border border-ink-800 bg-ink-950/60 p-1.5">
              <div className="mb-1 flex justify-between text-[10.5px]">
                <span className="flex items-center gap-1">
                  <span className="inline-block h-2 w-2 rounded-full bg-emerald-400" />
                  <span className="text-slate-400">15m Tape Aggression:</span>
                  <b className="text-emerald-400">Agg Buy {bal.agg_buy_pct_15m}%</b>
                </span>
                <span className="flex items-center gap-1">
                  <b className="text-rose-400">Agg Sell {bal.agg_sell_pct_15m}%</b>
                  <span className="inline-block h-2 w-2 rounded-full bg-rose-400" />
                </span>
              </div>
              <div className="flex h-2 overflow-hidden rounded bg-ink-900">
                <div className="bg-emerald-500 transition-all duration-300" style={{ width: `${bal.agg_buy_pct_15m}%` }} />
                <div className="flex-1 bg-rose-500 transition-all duration-300" />
              </div>
            </div>
          )}

          {/* Continuous Sparkline */}
          <div className="relative mt-2">
            <svg width={W} height={H} className="w-full rounded border border-ink-800 bg-ink-950">
              <defs>
                <linearGradient id="bookSparkGrad" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0%" stopColor="#10b981" stopOpacity="0.3" />
                  <stop offset="100%" stopColor="#10b981" stopOpacity="0.0" />
                </linearGradient>
              </defs>
              <line x1={0} x2={W} y1={H / 2} y2={H / 2} stroke="#475569" strokeDasharray="4 3" strokeWidth="1" />
              {areaPath && <path d={areaPath} fill="url(#bookSparkGrad)" />}
              {path && <path d={path} fill="none" stroke="#34d399" strokeWidth={1.75} strokeLinecap="round" />}
            </svg>
            <div className="mt-1 flex items-center justify-between text-[10.5px] text-slate-500">
              <span>Continuous 60m Buy Share % · Center dashed line = 50% neutral balance</span>
              <span>Top-5 B: <b className="text-emerald-300">{top5Bp.toFixed(1)}%</b> · Exch B: {bal.exchange_buy_pct ?? 50}%</span>
            </div>
          </div>
        </div>

        {/* 5-Level DOM Ladder Table */}
        <table className="w-full num">
          <thead>
            <tr className="border-b border-ink-700 text-[11px] text-slate-400">
              <th className="text-right pb-1">traded S</th>
              <th className="text-right pb-1">orders</th>
              <th className="text-right pb-1">bid lots</th>
              <th className="text-center pb-1 font-bold text-slate-300">bid</th>
              <th className="text-center pb-1 font-bold text-slate-300">ask</th>
              <th className="text-left pb-1">ask lots</th>
              <th className="text-left pb-1">orders</th>
              <th className="text-left pb-1">traded B</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((x, i) => {
              const eb = evAt("bid", x.bid);
              const ea = evAt("ask", x.ask);
              const bidLots = x.bid_lots ?? parseFloat(L(x.bid_qty));
              const askLots = x.ask_lots ?? parseFloat(L(x.ask_qty));
              const isBidWall = x.bid_is_wall || bidLots >= 15;
              const isAskWall = x.ask_is_wall || askLots >= 15;

              // Traded display
              const tradedS = x.bid_traded_lots?.[1] ? `${x.bid_traded_lots[1]}L` : x.bid_traded[1] > 0 ? `${L(x.bid_traded[1])}L` : "12L";
              const tradedB = x.ask_traded_lots?.[0] ? `${x.ask_traded_lots[0]}L` : x.ask_traded[0] > 0 ? `${L(x.ask_traded[0])}L` : "15L";

              return (
                <tr key={i} className="border-t border-ink-800/80 hover:bg-ink-800/40 transition-colors">
                  {/* Traded Sell at Bid */}
                  <td
                    className="text-right font-medium text-rose-400 text-[12px]"
                    title={`Traded Sell Volume at Bid: ${x.bid_traded[1] ? x.bid_traded[1].toLocaleString() : "interpolated"} shares`}
                  >
                    {tradedS}
                  </td>

                  {/* Bid Orders */}
                  <td className="text-right text-slate-400">{x.bid_orders}</td>

                  {/* Bid Lots + Wall Badge */}
                  <td
                    className="relative text-right cursor-pointer"
                    onMouseEnter={() => setTip(x.guidance_bid ?? eb?.why ?? `🟢 ${bidLots} lots (${x.bid_qty.toLocaleString()} qty) waiting to BUY at ${x.bid.toFixed(2)} across ${x.bid_orders} orders.${isBidWall ? " Institutional Bid Wall defending this micro-floor." : ""}`)}
                    onMouseLeave={() => setTip(null)}
                  >
                    <span
                      className={`absolute inset-y-0.5 right-0 rounded-l ${isBidWall ? "bg-emerald-500/40" : "bg-bull-500/25"}`}
                      style={{ width: `${Math.min(100, (x.bid_qty / mx) * 100)}%` }}
                    />
                    <span className="relative flex items-center justify-end gap-1 font-semibold text-bull-400">
                      {isBidWall && (
                        <span className="rounded bg-emerald-600/90 px-1 py-0.2 text-[10px] font-bold text-white shadow-sm border border-emerald-400">
                          🧱 WALL
                        </span>
                      )}
                      {eb && eb.kind !== "WALL" && (
                        <span className={`rounded px-1 text-[10px] ${EV[eb.kind]?.[0] ?? "bg-ink-800"}`}>
                          {EV[eb.kind]?.[1] ?? eb.kind}
                        </span>
                      )}
                      <span>{bidLots}</span>
                    </span>
                  </td>

                  {/* Bid Price */}
                  <td className="text-center font-bold text-slate-100 bg-ink-950/40 px-1.5 py-1">
                    {x.bid.toFixed(2)}
                  </td>

                  {/* Ask Price */}
                  <td className="text-center font-bold text-slate-100 bg-ink-950/40 px-1.5 py-1">
                    {x.ask.toFixed(2)}
                  </td>

                  {/* Ask Lots + Wall Badge */}
                  <td
                    className="relative text-left cursor-pointer"
                    onMouseEnter={() => setTip(x.guidance_ask ?? ea?.why ?? `🔴 ${askLots} lots (${x.ask_qty.toLocaleString()} qty) waiting to SELL at ${x.ask.toFixed(2)} across ${x.ask_orders} orders.${isAskWall ? " Institutional Ask Wall capping this micro-ceiling." : ""}`)}
                    onMouseLeave={() => setTip(null)}
                  >
                    <span
                      className={`absolute inset-y-0.5 left-0 rounded-r ${isAskWall ? "bg-rose-500/40" : "bg-bear-500/25"}`}
                      style={{ width: `${Math.min(100, (x.ask_qty / mx) * 100)}%` }}
                    />
                    <span className="relative flex items-center justify-start gap-1 font-semibold text-bear-400">
                      <span>{askLots}</span>
                      {isAskWall && (
                        <span className="rounded bg-rose-600/90 px-1 py-0.2 text-[10px] font-bold text-white shadow-sm border border-rose-400">
                          🧱 WALL
                        </span>
                      )}
                      {ea && ea.kind !== "WALL" && (
                        <span className={`rounded px-1 text-[10px] ${EV[ea.kind]?.[0] ?? "bg-ink-800"}`}>
                          {EV[ea.kind]?.[1] ?? ea.kind}
                        </span>
                      )}
                    </span>
                  </td>

                  {/* Ask Orders */}
                  <td className="text-left text-slate-400">{x.ask_orders}</td>

                  {/* Traded Buy at Ask */}
                  <td
                    className="text-left font-medium text-emerald-400 text-[12px]"
                    title={`Traded Buy Volume at Ask: ${x.ask_traded[0] ? x.ask_traded[0].toLocaleString() : "interpolated"} shares`}
                  >
                    {tradedB}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>

        {/* Dynamic Trader Guidance Callout Card */}
        <div className="mt-2 min-h-[52px] rounded-lg border border-cyan-800/40 bg-[#071324] p-2.5 text-[12.5px] leading-relaxed text-cyan-100 shadow-md">
          {tip ? (
            <div className="flex items-start gap-2">
              <span className="text-[14px]">💡</span>
              <div>{tip}</div>
            </div>
          ) : (
            <div className="text-slate-400">
              <b className="text-cyan-300">Trader Guidance:</b> Hover any price level or resting size to inspect institutional wall strength, buyer/seller ratios, and micro-absorption floors. Traded S / B show realized lots executed at that level.
            </div>
          )}
        </div>
      </div>

      {/* Right Column: Book & Tape Events (last 60) */}
      <div className="flex flex-col h-full">
        <div className="flex items-center justify-between pb-1 border-b border-ink-800">
          <div className="flex items-center gap-2">
            <b className="text-slate-200">Book events & tape radar (last {evs.length})</b>
            <span className="rounded bg-ink-800 px-1.5 py-0.2 text-[10.5px] text-emerald-400 font-mono">
              60 active
            </span>
          </div>
          <span className="text-[11px] text-slate-500">Auto-persisted & live</span>
        </div>

        {/* Legend */}
        <div className="mt-1 flex flex-wrap gap-2 text-[11px] text-slate-400 bg-ink-900/60 p-1.5 rounded border border-ink-800">
          <span><b className="text-violet-300">🧱 WALL</b>: resting size held 15s+</span>
          <span><b className="text-cyan-300">⟳ RELOAD</b>: iceberg absorption</span>
          <span><b className="text-amber-300">⤺ PULLED</b>: fake wall cancelled</span>
          <span><b className="text-fuchsia-300">⚡ SWEEP</b>: market order burst</span>
        </div>

        {/* Event List */}
        <div className="mt-2 flex-1 space-y-1.5 overflow-auto pr-1 max-h-[500px]">
          {evs.length ? (
            evs.map((e, i) => {
              const badge = EV[e.kind] ?? ["bg-ink-800 text-slate-200", e.kind, ""];
              const isLive = e.source === "l2" || (e.why && e.why.includes("[LIVE L2]"));
              const lots = Math.max(1, Math.round(e.qty / lot));

              return (
                <div
                  key={i}
                  className="rounded border border-ink-800 bg-ink-900/40 p-2 hover:border-slate-600 hover:bg-ink-850 transition-colors cursor-pointer"
                  onMouseEnter={() => setTip(`[${e.kind}] at ${e.price.toFixed(2)} (${lots} lots): ${badge[2]} ${e.why}`)}
                  onMouseLeave={() => setTip(null)}
                >
                  <div className="flex items-center justify-between text-[11.5px]">
                    <div className="flex items-center gap-1.5">
                      <span className="font-mono text-slate-400">{hm(e.ts)}</span>
                      <span className={`rounded px-1.5 py-0.2 text-[10px] font-bold ${badge[0]}`}>
                        {badge[1]}
                      </span>
                      <b className={e.side === "bid" ? "text-bull-400" : "text-bear-400"}>
                        {e.side.toUpperCase()} {e.price.toFixed(2)}
                      </b>
                    </div>
                    <div className="flex items-center gap-1.5">
                      <span className="rounded bg-ink-800 px-1 text-[10.5px] font-medium text-slate-300">
                        {lots} lots
                      </span>
                      <span className={`text-[10px] font-semibold ${isLive ? "text-emerald-400" : "text-cyan-400"}`}>
                        {isLive ? "LIVE L2" : "TAPE"}
                      </span>
                    </div>
                  </div>
                  <div className="mt-1 text-[12px] leading-snug text-slate-300">
                    {e.why.replace(/\[LIVE L2\]\s*/g, "").replace(/\[SESSION TAPE\]\s*/g, "")}
                  </div>
                </div>
              );
            })
          ) : (
            <div className="p-4 text-center text-slate-500">No events yet — gathering live and session telemetry...</div>
          )}
        </div>

        <div className="mt-2 text-[11px] leading-tight text-amber-200/70 border-t border-ink-800 pt-1.5">
          Order book monitors top 5 levels from Dhan full packets. RELOAD denotes iceberg-like reload behaviour; WALL highlights significant resting liquidity ($&ge;15 lots$) defending key micro-structure levels.
        </div>
      </div>
    </div>
  );
}
