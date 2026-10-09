// Guide.tsx — "How to read this desk", built into the app as collapsible sections (closed by default).
import type { ReactNode } from "react";

function S({ title, children, open = false }: { title: string; children: ReactNode; open?: boolean }) {
  return (
    <details open={open} className="group rounded-lg border border-ink-700 bg-ink-850">
      <summary className="cursor-pointer select-none list-none px-3 py-2 text-[13px] font-semibold text-slate-100 flex items-center gap-2">
        <span className="text-gold-400 transition-transform group-open:rotate-90">▶</span>{title}
      </summary>
      <div className="px-4 pb-3 text-[12px] leading-relaxed text-slate-300 space-y-2">{children}</div>
    </details>
  );
}
const G = ({ children }: { children: ReactNode }) => <b className="text-bull-400">{children}</b>;
const R = ({ children }: { children: ReactNode }) => <b className="text-bear-400">{children}</b>;
const Y = ({ children }: { children: ReactNode }) => <b className="text-gold-400">{children}</b>;

function T({ head, rows }: { head: string[]; rows: ReactNode[][] }) {
  return (
    <table className="w-full text-[12px] border border-ink-700">
      <thead className="bg-ink-900 text-[10px] uppercase text-slate-400"><tr>{head.map((h) => <th key={h} className="text-left px-2 py-1">{h}</th>)}</tr></thead>
      <tbody>{rows.map((r, i) => <tr key={i} className="border-t border-ink-800">{r.map((c, j) => <td key={j} className="px-2 py-1 align-top">{c}</td>)}</tr>)}</tbody>
    </table>
  );
}

export default function Guide() {
  return (
    <details className="card group/g">
      <summary className="card-h cursor-pointer list-none select-none">
        <span className="card-t">📘 How to read this desk — tap to open</span>
        <span className="text-[10px] text-slate-500 group-open/g:hidden">closed</span>
      </summary>
      <div className="p-3 space-y-2">

        <S title="1 · The one idea behind everything" open>
          <p>Big players cannot buy 10 lakh units at one price. They need many sellers at once. Where are those sellers? At the <Y>stop-losses</Y> of retail longs, just below every obvious low. So before a big move UP, price is often pushed DOWN first to trigger those stops — that is the <Y>sweep</Y>. Then it reverses hard.</p>
          <p>Every day has three acts: <Y>hunt the stops → break the structure → come back to the gap and go</Y>. This desk draws each act. You only enter in act three.</p>
        </S>

        <S title="2 · What every drawing on the chart means">
          <T head={["On the chart", "Name", "What it tells you"]} rows={[
            ["Dashed line above price", "BSL — buy-side liquidity", "Stops of short sellers sit here. Price is attracted to it. A target for longs."],
            ["Dashed line below price", "SSL — sell-side liquidity", "Stops of buyers sit here. A target for shorts."],
            ["PDH / PDL / OR lines", "Previous-day high/low, opening range", "The most hunted levels of the day. Watch them first."],
            ["Marker 'Sweep'", "Stop hunt", "Wick through the line, close back inside. A WARNING of reversal, not an entry."],
            ["Marker 'False BO'", "Fake breakout", "Closed through, then closed back inside within 2 candles. Breakout traders are trapped."],
            [<span><G>▲</G>/<R>▼</R> BOS</span>, "Break of structure", "Trend CONTINUES. Trade only with it."],
            [<span><G>▲</G>/<R>▼</R> MSS</span>, "Market structure shift", "Trend CHANGED, with a big candle. Now look for entry in the new direction."],
            ["Small box (green/red)", "FVG — fair value gap", "A gap left by a fast move. Price likes to come back to it. THIS is the entry zone."],
            ["Bigger box 'Demand/Supply OB'", "Order block", "Last opposite candle before the big move. Institutions defend it. 'fresh' = untouched = strongest."],
            ["'Breaker'", "Broken order block", "Old supply became demand (or reverse). Weaker; don't rely on it alone."],
            ["Shaded upper / lower half", "Premium / Discount", "Above the 50 % EQ line = expensive (sell zone). Below = cheap (buy zone)."],
            ["Band 61.8–79 %", "OTE", "Optimal trade entry — the best pull-back zone after a break."],
          ]} />
          <p className="text-slate-500">Use the layer buttons above the chart to hide what you don't need. Beginners: keep FVG, Liquidity, BOS/MSS, Premium/OTE on; switch Swings off.</p>
        </S>

        <S title="3 · Sweep vs real breakout — the most important skill">
          <T head={["What the candle does", "It is", "You do"]} rows={[
            ["Wick through the level, body closes back inside", <Y>Sweep (trap)</Y>, "Get ready for the opposite direction. Wait for MSS."],
            ["Closes beyond, next 1–2 candles close back inside", <Y>False breakout</Y>, "Same as sweep — trapped traders fuel the reversal."],
            ["Big body closes beyond, leaves a gap, does not come back", <Y>Real break (BOS)</Y>, "Do not fade it. Trade WITH it on the pull-back."],
            ["After a sweep, a later candle closes back beyond the swept level", <R>Sweep cancelled</R>, "The hunt failed. The app drops the setup. So do you."],
          ]} />
          <p>Never decide on a candle that is still forming. The desk only marks <Y>closed</Y> candles — a wick that looks like a sweep at 10:03 can be a full breakout by 10:05.</p>
        </S>

        <S title="4 · The decision checklist — BUY, SELL or WAIT">
          <ol className="list-decimal ml-4 space-y-1">
            <li><b>Higher timeframe first.</b> Look at <i>Bias by timeframe</i>. If 15m and 1h are bearish, you only look for shorts today. If they disagree → <Y>WAIT</Y>.</li>
            <li><b>Was liquidity taken?</b> A sweep / false BO of BSL (for a short) or SSL (for a long). No sweep → Setup A is not possible; only Setup C (OTE) can still work.</li>
            <li><b>Did structure shift?</b> An MSS in your direction with displacement (big body). No MSS → <Y>WAIT</Y>.</li>
            <li><b>Is there an FVG from that move?</b> Mark it. That box is your entry area.</li>
            <li><b>Right zone?</b> Short only in <R>premium</R>, long only in <G>discount</G>. Wrong zone → <Y>WAIT</Y> for a pull-back.</li>
            <li><b>Entry candle CLOSES inside the FVG</b> (or OTE band) → <G>ENTER</G> at that close.</li>
            <li><b>Stop</b> = beyond the sweep extreme + 2 pts. <b>Target</b> = next opposite liquidity pool. If R:R &lt; 1.5 → <Y>skip</Y>.</li>
            <li><b>Time</b>: only 09:30–11:30 and 13:30–14:45. Nothing new after 14:45. Exit by 15:15.</li>
          </ol>
          <p>The <i>Market story</i> panel runs this checklist for you on every 5m close and writes the answer in the "Right now" box.</p>
        </S>

        <S title="5 · The three setups in the Playbook panel">
          <p><Y>Setup A — Raid → MSS → FVG</Y> (the main trade). Sweep → MSS with a big candle → price returns and closes in the FVG → entry. Stop beyond the raid. Highest quality.</p>
          <p><Y>Setup B — BOS / MSS</Y> (information). Tells you who is in control. Not an entry. Grey lines in the playbook like "MSS bearish through 22,460.4 (displacement)".</p>
          <p><Y>Setup C — OTE pull-back</Y>. After a break, the first closed candle inside the 61.8–79 % band. Stop beyond the range origin, target the range extreme.</p>
          <p><b>TRADE</b> (gold card) = every filter passed; an alert and a paper trade are made. <b>SKIP</b> = shows the reason in amber (✕). Read the skips — they teach more than the trades.</p>
        </S>

        <S title="6 · Reading your screenshot — playbook lines + OI builds">
          <p>The playbook lines from the session:</p>
          <T head={["Time", "Line", "Meaning"]} rows={[
            ["09:15", <R>▼ MSS bearish through 22,574.4</R>, "Right at the open, sellers broke the last swing low that held the prior up-move. Control flipped to sellers."],
            ["09:25", <R>▼ BOS bearish through 22,514.9</R>, "Another low broken — the down-trend is confirmed, not a one-candle accident."],
            ["09:40", <R>▼ BOS bearish through 22,475.8</R>, "Third break. Trend is healthy. Pro plan: sell pull-backs into premium, never buy."],
            ["10:20", <R>▼ MSS bearish through 22,460.4</R>, "Price had bounced enough to make a small up-structure; this candle broke it again. The bounce was a pull-back, and sellers resumed."],
          ]} />
          <p>The OI table (expiry 13 Oct) — how to read each word:</p>
          <T head={["Label", "Price", "OI", "Who is doing it", "Read"]} rows={[
            [<R>CE short build</R>, "CE premium falls", "OI rises", "Call writers SELLING calls", "They expect NIFTY to stay BELOW that strike → resistance"],
            [<G>CE long build</G>, "rises", "rises", "Call buyers", "Bullish"],
            [<G>CE short covering</G>, "rises", "falls", "Call writers running", "Bullish — resistance breaking"],
            [<G>PE short build</G>, "PE premium falls", "rises", "Put writers", "Support below that strike"],
            [<R>PE short covering</R>, "rises", "falls", "Put writers running", "Support breaking → bearish"],
            [<R>PE long build</R>, "rises", "rises", "Put buyers", "Bearish"],
          ]} />
          <p>Your screenshot: <R>CE short build</R> at 22500 (+1.01 Cr), 22400 (+99 L), 22300 (+64 L), 22450, 22600, 22700 — writers are stacking calls on every strike above price. 22600 PE shows <R>short covering</R> (put writers leaving, −34 L) and 22000 PE a <R>long build</R> (put buyers). Note: the green colour in the table means "bullish for the option price", but for NIFTY those two PE rows are bearish too.</p>
          <p><Y>Every row points the same way: bearish.</Y> 22,300–22,500 is now a ceiling of written calls. Combined with four bearish structure breaks, a pro does not buy this market. The only trade is a short on a pull-back into premium (toward 22,300–22,400), after a closed candle inside a bearish FVG or the OTE band — or nothing at all.</p>
        </S>

        <S title="8 · Delta / Footprint — who is attacking, and did it work?">
          <p><G>Positive delta</G> = buyers attacking (more market buys). <R>Negative delta</R> = sellers attacking. On its own that means little. What matters is <Y>what price did about it, at a zone</Y>.</p>
          <T head={["Marker", "Name", "Read"]} rows={[
            [<Y>ABS</Y>, "Absorption", "Heavy attack at an OB / FVG / pool, but the candle went nowhere, or the next candle didn't follow. The other side absorbed it. Expect the attackers to give up."],
            ["FLIP", "Delta flip", "Delta changed sign at a zone or right after a sweep. Control is changing hands."],
            ["Δ+ / Δ−", "Aggression (Δ detail)", "Delta far outside its normal range. Information only."],
            ["IMB / pIMB", "Footprint / proxy imbalance (Δ detail)", "Where the attack hit (stacked imbalances, or a delta spike plus a wick when there is no footprint)."],
          ]} />
          <p><b>The money pattern:</b> price comes back up into a <R>supply OB</R>, buyers attack (big positive delta), but the candle closes weak → <Y>ABS</Y>. With the bearish MSS already done, that short has more weight.</p>
          <p>Delta <b>never</b> creates a trade. It only moves the confidence bar on Setup A / C, with the reason written on the card. No delta source = "Delta unavailable" (the NIFTY index has no volume; use a CSV or a futures footprint feed). <b>MOCK</b> = fake numbers for testing the screen only.</p>
        </S>

        <S title="9 · Setup D — CRT + TBS (Candle Range Theory + Turtle Body Soup)">
          <p>AR's method, a <Y>separate setup</Y>. It never changes A / B / C. Two timeframes: the <b>HTF</b> gives the bias, the <b>LTF</b> gives the entry.</p>
          <T head={["Step", "Where", "Rule"]} rows={[
            ["1 · CRT candle", "HTF", "A closed candle whose body is bigger than its two wicks together, and thick (≥ 0.8× ATR; ≥ 1.2× = A+). CRTH = high, CRTL = low, 50 % = middle."],
            ["2 · Keylevel", "same HTF", <>The CRT candle purged an old high (<R>OHP</R>), an old low (<G>OLP</G>), or traded into an order block / FVG. Sets the bias. No keylevel → no setup.</>],
            ["3 · Manipulation", "next HTF candle", "Only inside this candle do we look for the entry."],
            ["4 · TBS", "LTF", <>The first candle whose <b>body closes</b> beyond the level — above CRTH for a sell, below CRTL for a buy. A wick poke is <b>not</b> a TBS.</>],
            ["5 · Model #1", "LTF", "Sell: the thick buy candle at / after the TBS (it is the order block). Buy: the thick sell candle."],
            ["6 · Entry", "LTF", "The first close below Model #1's low (sell) or above its high (buy). Entry = that close."],
            ["7 · Exits", "", <>Stop at the extreme high / low (no tiny stops). <Y>TP-1 = 50 %</Y>: book half, then stop → entry (breakeven) — never before TP-1. TP-2 = the other CRT extreme.</>],
          ]} />
          <T head={["Pair", "Style"]} rows={[["1h → 1m", "Scalping"], ["4h → 5m", "Short-term (NSE 4h = 09:15–13:15, 13:15–15:30)"], ["1D → 1h", "Day trading"]]} />
          <p>On the chart: the <Y>gold box</Y> is the CRT range (edge red = bearish, green = bullish), the dashed line is the 50 %. <Y>■ TBS</Y>, ● M1 and the arrow <b>CRT▶</b> mark the LTF steps; a 1m event sits on its 5m candle on a 5m chart. Toggle with "CRT + TBS".</p>
          <p className="text-slate-400">Our reading of two course points, both shown on every card: "A+ TBS" = 2–6 candles closed inside the level just before the TBS (reported, not required); the trade windows, risk limits and R:R ≥ 1 to TP-1 are this desk's filters, not AR's.</p>
        </S>

        <S title="7 · Pro habits that keep you alive">
          <ul className="list-disc ml-4 space-y-1">
            <li>No setup = no trade. Zero trades is a good day if the setups were not there (8 Oct was exactly that).</li>
            <li>Never chase the MSS candle. The entry is the return to the gap.</li>
            <li>Never trade the first 15 minutes, lunch (11:30–13:30) or after 14:45.</li>
            <li>Stop goes in with the entry, and never moves away from price.</li>
            <li>Two losses in a row → stop for the day.</li>
            <li>Paper-trade 30 setups before believing any win rate. This app has no order button on purpose.</li>
          </ul>
        </S>
      </div>
    </details>
  );
}
