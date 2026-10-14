// Story.tsx — the day told as a trade narrative, rebuilt every time a 5m candle closes.
// Reads ONLY closed-candle engine output (5m analysis + playbook signals + option-chain summary).
// It never invents a level: every number comes from the engine.
import { useEffect, useState } from "react";
import { api, fmt, lakh, type Analysis, type Dashboard, type Marker, type Signal } from "../api";
import type { OcSummary } from "./OcLab";

type Tone = "bull" | "bear" | "wait" | "info" | "trade";
interface Beat { time: string; tone: Tone; title: string; meaning: string; action: string }

const toneCls: Record<Tone, string> = {
  bull: "border-bull-400/60", bear: "border-bear-400/60", wait: "border-amber-400/60",
  info: "border-ink-600", trade: "border-gold-500",
};
const dot: Record<Tone, string> = {
  bull: "bg-bull-400", bear: "bg-bear-400", wait: "bg-amber-400", info: "bg-slate-500", trade: "bg-gold-500",
};

function markerBeat(m: Marker): Beat {
  const p = fmt(m.price, 1);
  const bear = m.dir === "bear";
  switch (m.kind) {
    case "SWEEP":
      return bear
        ? { time: m.at, tone: "wait", title: `Stop hunt above ${p}`,
            meaning: "Price wicked above the high where buyers keep their stops, then closed back below. Those stops were the fuel smart money needed to SELL.",
            action: "Do not short yet. A sweep alone is only a warning. Wait for a bearish MSS (a strong red candle breaking the last swing low). If a candle closes back above " + p + ", the hunt failed — forget it." }
        : { time: m.at, tone: "wait", title: `Stop hunt below ${p}`,
            meaning: "Price wicked below the low where sellers keep their stops, then closed back above. Smart money collected those sell stops to BUY.",
            action: "Do not buy yet. Wait for a bullish MSS (a strong green candle breaking the last swing high). If a candle closes back below " + p + ", the hunt failed — forget it." };
    case "FALSE_BREAKOUT":
      return bear
        ? { time: m.at, tone: "wait", title: `Fake breakout above ${p}`,
            meaning: "Price closed above the level, pulled in breakout buyers, then closed back below within 2 candles. Those buyers are now trapped and their stops sit just under price.",
            action: "Trapped buyers become sellers. Bias turns down — but still wait for MSS + FVG retest before a short." }
        : { time: m.at, tone: "wait", title: `Fake breakdown below ${p}`,
            meaning: "Price closed below the level, pulled in breakdown sellers, then closed back above within 2 candles. Those sellers are trapped.",
            action: "Trapped sellers must buy back. Bias turns up — wait for MSS + FVG retest before a long." };
    case "MSS":
      return bear
        ? { time: m.at, tone: "bear", title: `Character change DOWN — MSS through ${p}`,
            meaning: "A strong red candle closed below the last swing low that the up-move was defending. The buyers lost control.",
            action: "Now think SHORT only. Do not chase this candle. Mark the FVG it left behind and wait for price to come back UP into it, in premium, and CLOSE inside it — that is the entry." }
        : { time: m.at, tone: "bull", title: `Character change UP — MSS through ${p}`,
            meaning: "A strong green candle closed above the last swing high that the down-move was defending. The sellers lost control.",
            action: "Now think LONG only. Do not chase this candle. Mark the FVG it left and wait for price to come back DOWN into it, in discount, and CLOSE inside it — that is the entry." };
    default: // BOS
      return bear
        ? { time: m.at, tone: "bear", title: `Trend continues DOWN — BOS through ${p}`,
            meaning: "Sellers broke another swing low. The downtrend is healthy.",
            action: "Never buy against this. Sell rallies: wait for a pull-back into premium / a supply OB / the OTE band, then a closed red candle." }
        : { time: m.at, tone: "bull", title: `Trend continues UP — BOS through ${p}`,
            meaning: "Buyers broke another swing high. The uptrend is healthy.",
            action: "Never short against this. Buy dips: wait for a pull-back into discount / a demand OB / the OTE band, then a closed green candle." };
  }
}

function signalBeat(s: Signal): Beat | null {
  if (s.status === "INFO") return null;
  const side = s.side === "long" ? "LONG" : "SHORT";
  if (s.status === "TRADE") {
    return { time: s.time, tone: "trade", title: `ENTRY — Setup ${s.setup} ${side} @ ${fmt(s.entry, 1)}`,
      meaning: s.story.join(" · ") || s.name,
      action: `Stop ${fmt(s.sl, 1)} · target ${fmt(s.tp, 1)} (${s.target_src}) · risk ${fmt(s.risk, 1)} pts · R:R ${fmt(s.rr, 1)}. Place the stop the moment you enter. Do not move it further away.` };
  }
  return { time: s.time, tone: "info", title: `Setup ${s.setup} ${side} looked good — SKIPPED`,
    meaning: s.story.join(" · ") || s.name,
    action: "Why not: " + (s.reasons.join("; ") || "a filter failed") + ". A pro is happy to skip — the missed trade costs nothing." };
}

/** The verdict for the next candle: what a disciplined ICT trader is doing right now. */
function verdict(a: Analysis | null, d: Dashboard): { tone: Tone; head: string; lines: string[] } {
  const q = d.quote;
  const lines: string[] = [];
  if (!a) return { tone: "info", head: "Loading the 5m read…", lines };
  const b15 = d.bias["15m"]?.trend ?? "none";
  const t5 = a.trend;
  const r = a.range;
  const px = a.last_close;

  const above = a.pools.filter((p) => p.price > px).sort((x, y) => x.price - y.price)[0];
  const below = a.pools.filter((p) => p.price < px).sort((x, y) => y.price - x.price)[0];
  if (above) lines.push(`Nearest buy-side liquidity (stops above): ${fmt(above.price, 1)} ${above.src} — ${fmt(above.price - px, 0)} pts away. Shorts target nothing above it; longs aim at it.`);
  if (below) lines.push(`Nearest sell-side liquidity (stops below): ${fmt(below.price, 1)} ${below.src} — ${fmt(px - below.price, 0)} pts away. Longs aim nothing below it; shorts aim at it.`);

  const c = d.chain;
  if (c) {
    if (c.call_wall) lines.push(`Option writers' ceiling (call wall): ${c.call_wall}. Floor (put wall): ${c.put_wall ?? "—"}. Max pain ${c.max_pain}. PCR ${c.totals.pcr ?? "—"}.`);
    const ceShort = c.builds.filter((x) => x.side === "CE" && x.kind === "Short build");
    const peBear = c.builds.filter((x) => x.side === "PE" && (x.kind === "Short covering" || x.kind === "Long build"));
    const peBull = c.builds.filter((x) => x.side === "PE" && x.kind === "Short build");
    const ceBull = c.builds.filter((x) => x.side === "CE" && (x.kind === "Short covering" || x.kind === "Long build"));
    const bearVotes = ceShort.length + peBear.length, bullVotes = peBull.length + ceBull.length;
    if (bearVotes + bullVotes > 0) {
      const top = c.builds[0];
      lines.push(bearVotes > bullVotes
        ? `OI flow is BEARISH (${bearVotes} vs ${bullVotes}): call writers are adding positions${top ? `, biggest at ${top.strike} ${top.side} (${lakh(top.oi_chg)})` : ""}. Rallies into those strikes are likely to be sold.`
        : bullVotes > bearVotes
          ? `OI flow is BULLISH (${bullVotes} vs ${bearVotes}): put writers are adding positions${top ? `, biggest at ${top.strike} ${top.side} (${lakh(top.oi_chg)})` : ""}. Dips into those strikes are likely to be bought.`
          : "OI flow is mixed — writers on both sides. Expect a range.");
    }
  }

  if (!q.market_open) {
    return { tone: "info", head: "Market closed — use this to plan tomorrow", lines: [
      `Today closed with 5m trend ${t5.toUpperCase()}, 15m trend ${b15.toUpperCase()}${r ? `, price in ${r.zone} (${r.pos_pct} %) of ${fmt(r.low, 0)}–${fmt(r.high, 0)}` : ""}.`,
      "Tomorrow, the first liquidity to watch is the previous-day high and low (PDH / PDL) and the levels below.", ...lines] };
  }

  const hhmm = (() => {
    const m = String(d.status?.now_ist ?? "").match(/(\d{2}):(\d{2})/);
    if (m) return +m[1] * 60 + +m[2];
    const n = new Date(Date.now() + 5.5 * 3600e3); return n.getUTCHours() * 60 + n.getUTCMinutes();
  })();
  const inWin = (hhmm >= 570 && hhmm <= 690) || (hhmm >= 810 && hhmm <= 885);
  if (!inWin) {
    return { tone: "wait", head: hhmm < 570 ? "WAIT — opening range forming (no trades before 09:30)" : hhmm > 885 ? "NO NEW TRADES — after 14:45" : "WAIT — lunch chop (11:30–13:30)",
      lines: ["Outside the trade windows the playbook will not fire. Watch which liquidity gets taken; it tells you the direction for the next window.", ...lines] };
  }
  if (t5 !== "none" && b15 !== "none" && t5 !== b15) {
    return { tone: "wait", head: `WAIT — timeframes disagree (5m ${t5}, 15m ${b15})`,
      lines: ["The small timeframe is fighting the bigger one. Usually this is a pull-back inside the 15m trend. Wait until 5m prints an MSS back in the 15m direction.", ...lines] };
  }
  const ote = r ? `${fmt(r.ote_band[0], 1)}–${fmt(r.ote_band[1], 1)}` : "—";
  if (t5 === "bear" && r) {
    return r.zone === "premium"
      ? { tone: "bear", head: "LOOK FOR A SHORT — bearish trend, price in premium",
          lines: [`This is where sellers like to sell. Entry only on a CLOSED 5m candle inside a bearish FVG or the OTE band ${ote}. Stop above ${fmt(r.high, 1)}. First target the sell-side liquidity below.`, ...lines] }
      : { tone: "wait", head: "WAIT — bearish, but price is already cheap (discount)",
          lines: [`Shorting here means selling low with a far stop. Wait for a pull-back UP to the EQ ${fmt(r.eq, 1)} or the OTE band ${ote}, then look for the short.`, ...lines] };
  }
  if (t5 === "bull" && r) {
    return r.zone === "discount"
      ? { tone: "bull", head: "LOOK FOR A LONG — bullish trend, price in discount",
          lines: [`This is where buyers like to buy. Entry only on a CLOSED 5m candle inside a bullish FVG or the OTE band ${ote}. Stop below ${fmt(r.low, 1)}. First target the buy-side liquidity above.`, ...lines] }
      : { tone: "wait", head: "WAIT — bullish, but price is already expensive (premium)",
          lines: [`Buying here means buying high with a far stop. Wait for a pull-back DOWN to the EQ ${fmt(r.eq, 1)} or the OTE band ${ote}, then look for the long.`, ...lines] };
  }
  return { tone: "wait", head: "WAIT — no clear structure yet", lines: ["No confirmed BOS/MSS on 5m. Let the market show its hand: which side's liquidity gets swept first?", ...lines] };
}

export default function Story({ dash, tick, oc }: { dash: Dashboard | null; tick: number; oc?: OcSummary | null }) {
  const [a5, setA5] = useState<Analysis | null>(null);
  useEffect(() => { api.analysis("5m").then(setA5).catch(() => {}); }, [tick]);
  if (!dash) return null;

  const lastDay = a5?.markers.length ? Math.floor(Math.max(...a5.markers.map((m) => m.time)) / 86400) : 0;
  const beats: Beat[] = [
    ...(a5?.markers ?? []).filter((m) => Math.floor(m.time / 86400) === lastDay).map(markerBeat),
    ...dash.signals.map(signalBeat).filter((x): x is Beat => !!x),
    ...((oc?.beats ?? []) as Beat[]),                      // OI writers: level built / broken, writer-flow flips
  ].sort((x, y) => x.time.localeCompare(y.time))
   .filter((b, i, arr) => arr.findIndex((o) => o.time === b.time && o.title === b.title) === i);
  const v0 = verdict(a5, dash);
  const v = { ...v0, lines: [...v0.lines, ...(oc?.lines ?? []).map((l) => `OI writers — ${l}`)] };

  return (
    <div className="card">
      <div className="card-h"><span className="card-t">Market story · how a pro reads today (5m)</span>
        <span className="text-[10px] text-slate-500">updates on every closed 5m candle</span></div>
      <div className="p-3 space-y-3">
        <div className={`rounded-lg border-2 ${toneCls[v.tone]} bg-ink-850 px-3 py-2`}>
          <div className="flex items-center gap-2">
            <span className={`h-2.5 w-2.5 rounded-full ${dot[v.tone]}`} />
            <span className="text-[10px] uppercase tracking-wider text-slate-400">Right now</span>
          </div>
          <div className={`mt-1 text-sm font-semibold ${v.tone === "bull" ? "text-bull-400" : v.tone === "bear" ? "text-bear-400" : v.tone === "wait" ? "text-amber-300" : "text-slate-100"}`}>{v.head}</div>
          <ul className="mt-1 space-y-1 text-[12px] text-slate-300">{v.lines.map((l, i) => <li key={i}>· {l}</li>)}</ul>
        </div>

        <ol className="relative border-l border-ink-700 ml-2 space-y-3 max-h-[60vh] overflow-auto pr-1">
          {beats.length === 0 && <li className="ml-4 text-xs text-slate-500">Nothing has happened on 5m yet today. The story starts with the first sweep or structure break.</li>}
          {beats.map((b, i) => (
            <li key={i} className="ml-4">
              <span className={`absolute -left-[5px] mt-1.5 h-2.5 w-2.5 rounded-full ${dot[b.tone]}`} />
              <div className={`rounded-lg border ${toneCls[b.tone]} px-3 py-2`}>
                <div className="text-[12px] font-semibold text-slate-100"><span className="num text-slate-500 mr-2">{b.time}</span>{b.title}</div>
                <div className="mt-0.5 text-[12px] text-slate-400">{b.meaning}</div>
                <div className="mt-1 text-[12px] text-gold-400">→ {b.action}</div>
              </div>
            </li>
          ))}
        </ol>
      </div>
    </div>
  );
}
