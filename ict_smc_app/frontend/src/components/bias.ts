// Dynamic factor registry: configured agreement and opposition gates; neutral differs from unavailable.
// Snapshot-only fields are never copied into earlier rows. Counts are correlated evidence, not probabilities.
// Information only — not backtested, never an order, never changes Setups A–D or paper trades.
import type { Analysis, Candle } from "../api";
import analystConfig from "../../../../analyst/analyst_config.json";
export const BIAS_POLICY = analystConfig.bias_meter;
import type { FpBar } from "./footprint";

export type Vote = "bull" | "bear" | null;
export const FACTORS = [
  "FVG/IFVG", "Order block", "Rejection block", "CRT/TBS", "Footprint", "RVOL", "VWAP",
  "Structure", "PE−CE action", "Writers gap", "Δput−Δcall", "Writer dominance", "Writer flip",
  "PCR trend", "SMT", "CCT", "MMXM", "CVD flow", "Gamma flip", "★ A+ Confluence",
  "Prahaar confirmation", "GTF zone rejection", "Futures OI build-up"
] as const;
export const BIAS_NAME = `Bias ${FACTORS.length}`;
import type { Row } from "./OcLab";

// Options Lab feeds (from 8501 via /api/oclab): PE−CE table rows (3m, ATM ±10) and the writer-flow points (1m, all strikes)
export type OcRow = Row;
export type OcPt = { t: number; ce: number | null; pe: number | null; flow: number | null; bal: number | null; sig: boolean;
  fut?: number | null; flip?: boolean; regime?: string; conf?: string | null };
export type Gex = { ok: boolean; spot?: number; flip?: number | null; regime?: string; pin?: number; accel?: number | null; message?: string; ts?: string };
export type OcFeed = { pece: OcRow[]; flow: OcPt[]; gex?: Gex | null; day?: string; trinity?: PositionPoint[] };
export type Factor = typeof FACTORS[number];
export type BiasRow = { time: number; o: number; h: number; l: number; c: number; votes: Record<Factor, Vote>; why: Record<Factor, string>;
  bull: number; bear: number; avail: number; verdict: "BUY" | "SELL" | "MIXED"; strong: boolean; forming?: boolean; warn?: string;
  required: number; maxAgainst: number; neutral: number; missing: Factor[]; blockers: string[];
  lean: "BULLISH" | "BEARISH" | "BALANCED";
  context: { name: string; text: string }[] };

const FLOW = new Set(["STACKED_BUY", "STACKED_SELL", "ACCUMULATION", "DISTRIBUTION", "INITIATIVE", "MANIPULATION", "ABSORPTION"]);

export function computeBias(candles: Candle[], a: Analysis | null, fp: FpBar[], barSec: number, pct = BIAS_POLICY.agreement,
                            oc: OcFeed | null = null, forming = false,
                            smt: { time: number; dir: "bull" | "bear"; at: string }[] | null = null): BiasRow[] {
  if (!a) return [];
  const closed = candles.filter((c) => c.closed !== false);
  const latestClosed = closed.at(-1)?.time;
  const dayOf = (t: number) => new Date(t * 1000).toISOString().slice(0,10);
  const live = forming ? candles.filter((c) => c.closed === false).slice(-1) : [];
  const zones = a.zones ?? [];
  const vbar = new Map((a.volume?.bars ?? []).map((b) => [b.time, b]));
  const vwap = new Map((a.volume?.vwap ?? []).map((p) => [p.time, p.v]));
  const fpm = new Map(fp.map((b) => [b.time, b]));
  const struct = (a.markers ?? []).filter((m) => m.kind === "BOS" || m.kind === "MSS").sort((x, y) => x.time - y.time);
  const crt = (a.crt?.markers ?? []).filter((m) => m.kind === "CRT_ENTRY" || m.kind === "CRT_TBS").sort((x, y) => x.time - y.time);
  const ifvgSig = (a.markers ?? []).filter((m) => m.kind === "IFVG_BUY" || m.kind === "IFVG_SELL");

  const atr = a.atr || 20;
  const zoneVote = (types: string[], c: Candle): [Vote, string] => {
    let bull = "", bear = "", bullD = Infinity, bearD = Infinity;
    for (const z of zones) {
      if (!types.includes(z.type) || z.from >= c.time || z.to < c.time) continue;   // zone alive on this candle
      if (z.dir === "bull" && c.close >= z.bottom) {           // support below (tested & held, or within 1 ATR above it)
        const d = c.low <= z.top ? 0 : c.close - z.top;
        if (d <= atr && d < bullD) { bullD = d; bull = `${z.type} support ${z.bottom.toFixed(0)}–${z.top.toFixed(0)} ${d === 0 ? "tested & held" : "just below"}`; }
      }
      if (z.dir === "bear" && c.close <= z.top) {
        const d = c.high >= z.bottom ? 0 : z.bottom - c.close;
        if (d <= atr && d < bearD) { bearD = d; bear = `${z.type} resistance ${z.bottom.toFixed(0)}–${z.top.toFixed(0)} ${d === 0 ? "tested & held" : "just above"}`; }
      }
    }
    if (bull && bear) return bullD < bearD ? ["bull", bull] : bearD < bullD ? ["bear", bear] : [null, "between support and resistance"];
    return bull ? ["bull", bull] : bear ? ["bear", bear] : [null, ""];
  };
  const hasVol = (a.volume?.bars ?? []).length > 0, hasVwap = (a.volume?.vwap ?? []).length > 0, hasFp = fp.length > 0;

  // Options Lab helpers
  const hmOf = (t: number) => { const d = new Date(t * 1000); return `${String(d.getUTCHours()).padStart(2, "0")}:${String(d.getUTCMinutes()).padStart(2, "0")}`; };
  const pece = (oc?.pece ?? []).filter((r) => r.ts).slice().sort((x, y) => (x.ts < y.ts ? -1 : 1));
  const flow = (oc?.flow ?? []).slice().sort((x, y) => x.t - y.t);
  const hasPece = pece.length > 0, hasFlow = flow.length > 0;
  const flowAt = (t: number) => { let last: OcPt | undefined; for (const p of flow) { if (p.t > t) break; last = p; } return last; };

  return [...closed.slice(-80), ...live].map((c) => {
    const isLive = c.closed === false;
    const currentSnapshot = !isLive && c.time === latestClosed;
    const missing: Factor[] = [];
    const tEnd = c.time + barSec;                                   // candle close time (chart clock)
    const votes = {} as Record<Factor, Vote>, why = {} as Record<Factor, string>;
    // 1 FVG / IFVG (zone held, or an IFVG buy/sell signal on this or the previous 2 bars)
    let [v, w] = zoneVote(["FVG", "IFVG"], c);
    const sig = ifvgSig.find((m) => m.time <= c.time && m.time >= c.time - 2 * barSec);
    if (sig) { v = sig.kind === "IFVG_BUY" ? "bull" : "bear"; w = sig.kind.replace("_", " "); }
    votes["FVG/IFVG"] = v; why["FVG/IFVG"] = w;
    // 2 Order block / breaker
    [v, w] = zoneVote(["OB", "BREAKER"], c); votes["Order block"] = v; why["Order block"] = w;
    // 3 Rejection block (RJB)
    [v, w] = zoneVote(["RJB"], c); votes["Rejection block"] = v; why["Rejection block"] = w;
    // 4 CRT / TBS (last 6 bars)
    const cr = [...crt].reverse().find((m) => dayOf(m.time) === dayOf(c.time) && m.time <= c.time && m.time >= c.time - 6 * barSec);
    votes["CRT/TBS"] = cr ? cr.dir : null; why["CRT/TBS"] = cr ? `${cr.kind === "CRT_ENTRY" ? "CRT entry" : "TBS"} ${cr.at}` : "";
    // 4 Footprint (flow tag + delta agree, or a strong delta ≥ 15 %, or delta absorption/aggression)
    const f = fpm.get(c.time);
    let fv: Vote = null, fw = "";
    if (f && f.rows.length) {
      const dd: Vote = f.delta > 0 ? "bull" : f.delta < 0 ? "bear" : null;
      const tag = f.tags.find((t) => FLOW.has(t.k) && t.dir === dd);
      if (tag) { fv = dd; fw = `${tag.k.toLowerCase()} · Δ ${Math.round(f.delta / 1000)}K`; }
      else if (dd && Math.abs(f.delta_pct ?? 0) >= 15) { fv = dd; fw = `Δ ${f.delta_pct}%`; }
    }
    if (!fv) {
      const dmk = (a.markers ?? []).filter((m) => m.time === c.time);
      const absM = dmk.find((m) => m.kind === "DELTA_ABSORPTION");
      const aggM = dmk.find((m) => m.kind === "DELTA_AGGRESSION");
      if (absM) {
        fv = absM.dir;
        fw = `absorption ${absM.dir === "bull" ? "buyers defended ▲" : "sellers defended ▼"}`;
      } else if (aggM) {
        fv = aggM.dir;
        fw = `aggression ${aggM.dir === "bull" ? "market buys Δ+" : "market sells Δ−"}`;
      }
    }
    votes.Footprint = fv; why.Footprint = fw;
    // 5 RVOL ≥ 1.2× → votes with the candle colour
    const vb = vbar.get(c.time);
    votes.RVOL = vb?.rvol != null && vb.rvol >= 1.2 && c.close !== c.open ? (c.close > c.open ? "bull" : "bear") : null;
    why.RVOL = vb?.rvol != null ? `RVOL ${vb.rvol.toFixed(1)}×` : "";
    // 6 VWAP side (futures VWAP, basis-adjusted)
    const vw = vwap.get(c.time);
    votes.VWAP = vw != null ? (c.close > vw ? "bull" : c.close < vw ? "bear" : null) : null;
    why.VWAP = vw != null ? `close ${c.close > vw ? "above" : "below"} VWAP ${vw.toFixed(0)}` : "";
    // 7 Structure: direction of the last BOS / MSS so far
    const s = [...struct].reverse().find((m) => m.time <= c.time && dayOf(m.time) === dayOf(c.time));
    votes.Structure = s ? s.dir : null; why.Structure = s ? `last ${s.kind} ${s.dir === "bull" ? "▲" : "▼"} ${s.at}` : "";

    // 8 PE−CE table action at the candle close ("BUY CE…", "CE on dip…" → BUY · "…PE…" → SELL · WAIT / no entry → no vote)
    const cut = hmOf(tEnd);
    const sameDay = !oc?.day || dayOf(c.time) === oc.day;           // 8501 data is for ONE session — never borrow it for other days
    const peceAt = (at: number) => [...pece].reverse().find((r) => {
      const stamp = Date.parse(`${oc?.day || dayOf(c.time)}T${r.ts}:00Z`) / 1000;
      return Number.isFinite(stamp) && stamp < at && at-stamp <= BIAS_POLICY.feed_max_age_s;
    });
    const pr = sameDay ? peceAt(tEnd) : undefined;
    const act = (pr?.act?.[0] ?? "").replace(/<[^>]+>/g, "");
    const actionableAct = !/\bWAIT\b|\bHOLD\b|no entry|no fresh|after close|observe|stand aside/i.test(act);
    const mentionsCe = /\bCE\b/.test(act), mentionsPe = /\bPE\b/.test(act);
    votes["PE−CE action"] = actionableAct && mentionsCe && !mentionsPe ? "bull" : actionableAct && mentionsPe && !mentionsCe ? "bear" : null;
    why["PE−CE action"] = pr ? `${pr.ts} ${act || "—"}` : "";
    // 9 Writers gap (put − call OI change since yesterday): which way the gap moved over the last 30 min
    const flowFreshAt = (at: number) => { const point = flowAt(at-1); return point && dayOf(point.t) === dayOf(c.time) && at-point.t <= BIAS_POLICY.feed_max_age_s ? point : undefined; };
    const f0 = sameDay ? flowFreshAt(tEnd) : undefined, f30 = sameDay ? flowFreshAt(tEnd - 1800) : undefined;
    let gv: Vote = null, gw = "";
    if (f0 && f0.pe != null && f0.ce != null) {
      const gap = f0.pe - f0.ce, prev = f30 && f30.pe != null && f30.ce != null ? f30.pe - f30.ce : gap, ch = gap - prev;
      const big = Math.abs(ch) >= 0.03 * Math.max(Math.abs(gap), 1e6);
      gv = big ? (ch > 0 ? "bull" : "bear") : gap > 0 ? "bull" : gap < 0 ? "bear" : null;
      gw = `put−call gap ${(gap / 1e7).toFixed(2)}Cr, ${big ? (ch > 0 ? "widening" : "narrowing") : "steady"} over 30 min`;
    }
    votes["Writers gap"] = gv; why["Writers gap"] = gw;
    // 10 Δput−Δcall flow strip: an unusual (≥2.5×) writer candle inside this candle decides; else the 30-min balance (±0.25)
    const inBar = !sameDay ? [] : flow.filter((p) => p.t >= c.time && p.t < tEnd && p.flow != null);
    const spike = inBar.filter((p) => p.sig).sort((x, y) => Math.abs(y.flow!) - Math.abs(x.flow!))[0];
    let fv2: Vote = null, fw2 = "";
    if (spike) { fv2 = spike.flow! > 0 ? "bull" : "bear"; fw2 = `unusual writer candle ${(spike.flow! / 1e5).toFixed(1)}L (${spike.flow! > 0 ? "put writing" : "call writing"})`; }
    else if (f0?.bal != null) { fv2 = f0.bal >= 0.25 ? "bull" : f0.bal <= -0.25 ? "bear" : null; fw2 = `30-min balance ${f0.bal.toFixed(2)}`; }
    votes["Δput−Δcall"] = fv2; why["Δput−Δcall"] = fw2;

    // 11 Writer dominance: put line above call line = put writers lead = BUY (and vice versa); degree = separation
    //    separation = (put − call) / (|put| + |call|) → ≥ 0.2 votes; ≥ 0.5 moderate; ≥ 0.75 strong
    let dv: Vote = null, dw = "";
    if (f0 && f0.pe != null && f0.ce != null) {
      const sep = (f0.pe - f0.ce) / Math.max(Math.abs(f0.pe) + Math.abs(f0.ce), 1);
      const deg = Math.abs(sep) >= 0.75 ? "strong" : Math.abs(sep) >= 0.5 ? "moderate" : Math.abs(sep) >= 0.2 ? "mild" : "balanced";
      dv = Math.abs(sep) >= 0.2 ? (sep > 0 ? "bull" : "bear") : null;
      dw = `put ${(f0.pe / 1e7).toFixed(2)}Cr vs call ${(f0.ce / 1e7).toFixed(2)}Cr · gap ${(Math.abs(f0.pe - f0.ce) / 1e7).toFixed(2)}Cr → ${sep > 0 ? "PUT writers lead" : "CALL writers lead"}, ${deg} (${Math.round(Math.abs(sep) * 100)}%)`;
    }
    votes["Writer dominance"] = dv; why["Writer dominance"] = dw;
    // 12 Writer flip: the ▲ BULL / ▼ BEAR regime-flip marker on the writers chart, if it fired in the last 6 candles
    const flipPt = !sameDay ? undefined : [...flow].reverse().find((p) => p.flip && p.t < tEnd && p.t >= c.time - 5 * barSec);
    const fr = (flipPt?.conf ?? flipPt?.regime ?? "").toString();
    votes["Writer flip"] = flipPt ? (/bull/i.test(fr) ? "bull" : /bear/i.test(fr) ? "bear" : null) : null;
    why["Writer flip"] = flipPt ? `${/bull/i.test(fr) ? "▲ BULL" : /bear/i.test(fr) ? "▼ BEAR" : fr} flip at ${hmOf(flipPt.t)}` : "no flip in the last 6 candles";

    // Crossovers extend the existing flip factor; never add a second vote for the same writer event.
    const writerEvents=positioningEvents(flow.filter((p)=>p.ce!=null && p.pe!=null).map((p)=>({time:p.t,ce:p.ce!,pe:p.pe!,price:p.fut ?? NaN})),tEnd);
    const writerRecent=writerEvents.filter((e)=>e.time>=c.time && e.time<tEnd);
    const writerCross=writerRecent.filter((e)=>e.kind==="cross").at(-1);
    if (writerCross) {
      votes["Writer flip"]=writerCross.dir;
      why["Writer flip"]=writerCross.text+"; shares one vote with regime flips";
    }
    const writerDiv=writerRecent.filter((e)=>e.kind==="divergence").at(-1);
    if (writerDiv) why["Writer flip"]+=" · "+writerDiv.text+" (warning only)";
    const trinityPoints=oc?.trinity ?? [];
    const trinityEvents=positioningEvents(trinityPoints,tEnd);
    const recentTrinity=trinityEvents.filter((e)=>e.time>=c.time-(BIAS_POLICY.positioning_signals.event_window_bars-1)*barSec && e.time<tEnd);
    const trinityCross=recentTrinity.filter((e)=>e.kind==="cross").at(-1);
    const trinityLatest=trinityPoints.filter((p)=>p.time<tEnd).at(-1);
    const trinityFresh=!!trinityLatest && tEnd-trinityLatest.time<=BIAS_POLICY.positioning_signals.max_gap_s && dayOf(trinityLatest.time)===dayOf(c.time);
    if(trinityFresh && trinityCross){
      const prior=votes["Writer flip"];
      votes["Writer flip"]=prior && prior!==trinityCross.dir ? null : trinityCross.dir;
      why["Writer flip"]+=(prior && prior!==trinityCross.dir ? " · Conflicting writer/Trinity events: abstain. " : " · ")+trinityCross.text+" · Trinity: both expiries / combined futures; one shared vote";
    }
    const trinityDiv=recentTrinity.filter((e)=>e.kind==="divergence").at(-1);
    if (trinityDiv) why["Writer flip"]+=" · "+trinityDiv.text+" (warning only)";
    // 13 PCR trend (OI PCR from the PE−CE table, ATM ±10): rising over 30 min = put writing = BUY, falling = SELL
    //    + ⚠ crowding: PCR > 1.5 and turning down, or < 0.5 and turning up → reversal risk (shown on the badge, not a vote)
    let pv: Vote = null, pw = "", warn = "";
    const pNow = pr?.pcr != null ? pr : undefined;
    const pThen = sameDay ? peceAt(tEnd-1800) : undefined;
    if (pNow?.pcr != null) {
      const d = pThen?.pcr != null ? pNow.pcr - pThen.pcr : 0;
      pv = d >= 0.03 ? "bull" : d <= -0.03 ? "bear" : null;
      pw = `PCR ${pNow.pcr.toFixed(2)} (${d >= 0 ? "+" : ""}${d.toFixed(2)} in 30 min)`;
      const settled = cut >= "10:15";                         // PCR (from OI change) always starts tiny at the open
      if (settled && pNow.pcr > 1.5 && d < -0.01) warn = `⚠ PCR crowded ${pNow.pcr.toFixed(2)} and turning down — reversal risk`;
      if (settled && pNow.pcr < 0.5 && d > 0.01) warn = `⚠ PCR crowded ${pNow.pcr.toFixed(2)} and turning up — reversal risk`;
    }
    votes["PCR trend"] = pv; why["PCR trend"] = pw;

    // 14 SMT (NIFTY vs BANKNIFTY divergence at a swing) in the last 6 candles
    const sm = smt ? [...smt].reverse().find((m) => m.time <= c.time && m.time >= c.time - 5 * barSec) : undefined;
    votes.SMT = sm ? sm.dir : null; why.SMT = sm ? `${sm.dir === "bull" ? "bullish" : "bearish"} SMT ${sm.at}` : "no SMT in the last 6 candles";

    // Use timestamped confirmations, never the latest engine state on older rows.
    const cctData = a.cct;
    const eventAt = (markers: Analysis["markers"]) => [...markers].reverse().find((m) =>
      m.time <= c.time && m.time >= c.time-(BIAS_POLICY.context_bars-1)*barSec && dayOf(m.time)===dayOf(c.time));
    const cctEvent = eventAt((cctData?.markers ?? []).filter((m)=>m.kind === "CCT_BUY" || m.kind === "CCT_SELL"));
    votes.CCT = cctEvent?.dir ?? null;
    why.CCT = cctEvent ? `${cctEvent.kind} at ${cctEvent.at}` : "No timestamped CCT confirmation in this window";
    const mmxmEvent = eventAt((a.mmxm?.markers ?? []).filter((m)=>m.kind.endsWith("_SMR_CONFIRMED")));
    votes.MMXM = mmxmEvent?.dir ?? null;
    why.MMXM = mmxmEvent ? `${mmxmEvent.kind} at ${mmxmEvent.at}` : "No timestamped MMXM confirmation in this window";

    // 17 CVD flow — Cumulative Volume Delta 3-bar slope & 6-bar divergence (exhaustion / absorption trap)
    let cvdV: Vote = null, cvdW = "";
    const fi = fp.findIndex((x) => x.time === c.time);
    let cvdReady = false;
    if (f && fi >= 0) {
      const recent = fp.slice(Math.max(0,fi-3),fi+1);
      cvdReady = recent.length === 4 && recent.every((point,index)=>point.rows.length>0 && dayOf(point.time)===dayOf(c.time) && point.time===c.time-(3-index)*barSec);
      const p3 = cvdReady ? fp[fi - 3] : undefined;
      const dc = p3 ? f.cvd - p3.cvd : 0;
      const win = fp.slice(Math.max(0, fi - 6), fi).filter((point)=>point.rows.length && dayOf(point.time)===dayOf(c.time) && point.time>=c.time-6*barSec);
      let div: Vote = null, divW = "";
      if (cvdReady && win.length >= 4) {
        const hh = c.high > Math.max(...win.map((x) => x.h ?? 0));
        const ll = c.low < Math.min(...win.map((x) => x.l ?? Infinity));
        if (hh && f.cvd < Math.max(...win.map((x) => x.cvd))) {
          div = "bear"; divW = "Possible bearish CVD divergence; not confirmed absorption";
        } else if (ll && f.cvd > Math.min(...win.map((x) => x.cvd))) {
          div = "bull"; divW = "Possible bullish CVD divergence; not confirmed absorption";
        }
      }
      if (div) {
        cvdV = div; cvdW = divW;
      } else if (p3 && Math.abs(dc) > 0) {
        cvdV = dc > 0 ? "bull" : "bear";
        cvdW = `CVD ${dc > 0 ? "rising" : "falling"} (${(dc / 1000).toFixed(1)}k over 3 bars)`;
      }
    }
    votes["CVD flow"] = cvdV; why["CVD flow"] = cvdW || (hasFp ? "CVD flat / steady" : "no footprint/CVD data");

    // 18 Gamma flip — Price location vs Dealer High Volatility Level & GEX regime
    let gexV: Vote = null, gexW = "";
    const gex = oc?.gex;
    const gammaAt = gex?.ts ? Date.parse(gex.ts.replace(" ", "T") + "Z")/1000 : NaN;
    const gammaKnown = Number.isFinite(gammaAt) && gammaAt <= tEnd && tEnd-gammaAt <= BIAS_POLICY.feed_max_age_s;
    if (gex && gex.ok && gex.flip != null && currentSnapshot && sameDay && gammaKnown) {
      const flip = gex.flip;
      const reg = gex.regime || (c.close >= flip ? "positive" : "negative");
      if (c.close >= flip) {
        gexV = "bull";
        gexW = `close above Gamma Flip ${flip} (${reg} GEX cushion · buy dips)`;
      } else {
        gexV = "bear";
        gexW = `close below Gamma Flip ${flip} (${reg} GEX acceleration · volatility high)`;
      }
    }
    votes["Gamma flip"] = gexV; why["Gamma flip"] = gexW || "GEX / Gamma Flip not available";

    // 20 ★ A+ Confluence — Dragon Fruit / Super OB score ≥ 6/7 or Sniper POI alignment
    const sniper = a.pa_advanced?.sniper;
    const b20 = a.pa_advanced?.bias_20 ?? sniper?.bias_20;
    const aplusTrig = b20?.triggers?.APlus;
    let aplusV: Vote = null, aplusW = "";

    if (currentSnapshot && aplusTrig && aplusTrig.status === "ACTIVE") {
      aplusV = aplusTrig.dir ?? null;
      aplusW = aplusTrig.desc;
    } else if (currentSnapshot && aplusTrig && aplusTrig.status === "VETOED") {
      aplusW = `⚠ VETOED: ${aplusTrig.desc}`;
    } else if (currentSnapshot) {
      const bestSup = sniper?.best_supply;
      const bestDem = sniper?.best_demand;
      const inSupply = bestSup && c.high >= bestSup.bot && c.low <= bestSup.top + atr;
      const inDemand = bestDem && c.low <= bestDem.top && c.high >= bestDem.bot - atr;

      if (inDemand && (!bestDem?.score || bestDem.score >= 4.0)) {
        aplusV = "bull";
        aplusW = `★ A+ Demand [${bestDem.bot.toFixed(0)}–${bestDem.top.toFixed(0)}] · ${(bestDem.score ?? 0).toFixed(1)}/10 (${bestDem.tags?.slice(0, 2).join(" + ") || "POI"})`;
      } else if (inSupply && (!bestSup?.score || bestSup.score >= 4.0)) {
        aplusV = "bear";
        aplusW = `★ A+ Supply [${bestSup.bot.toFixed(0)}–${bestSup.top.toFixed(0)}] · ${(bestSup.score ?? 0).toFixed(1)}/10 (${bestSup.tags?.slice(0, 2).join(" + ") || "POI"})`;
      } else {
        const dfs = a.pa_advanced?.dragon_fruit ?? [];
        const activeDf = dfs.find((d) => d.risk === "LOW" && d.ts <= c.time);
        if (activeDf) {
          aplusV = activeDf.dir === "bull" ? "bull" : "bear";
          aplusW = `Dragon Fruit A+ (${activeDf.dir.toUpperCase()}) [${activeDf.bottom.toFixed(0)}–${activeDf.top.toFixed(0)}]`;
        }
      }
    }
    votes["★ A+ Confluence"] = aplusV;
    why["★ A+ Confluence"] = aplusW || "no active A+ POI / setup";

    const extra = additionalBiasVotes(c,closed,a,barSec);
    for (const factor of ["Prahaar confirmation","GTF zone rejection","Futures OI build-up"] as const) {
      votes[factor]=extra[factor].vote;
      why[factor]=extra[factor].why;
    }

    // Availability is checked for this candle, not inferred from data elsewhere in the session.
    const unavailable = (factor: Factor, explanation: string) => { missing.push(factor); votes[factor]=null; why[factor]=explanation; };
    for (const factor of ["Prahaar confirmation","GTF zone rejection","Futures OI build-up"] as const) {
      if (isLive || !extra[factor].available) unavailable(factor,isLive ? "Closed candles only" : extra[factor].why);
    }
    if (vb?.rvol == null) unavailable("RVOL", "No aligned futures volume for this candle");
    if (vw == null) unavailable("VWAP", "No aligned VWAP for this candle");
    if (!f?.rows.length || isLive) {
      if (!fv || isLive) unavailable("Footprint", "No complete footprint for this closed candle");
      unavailable("CVD flow", "No complete CVD history for this candle");
    }
    if (!cvdReady && !missing.includes("CVD flow")) unavailable("CVD flow", "CVD requires four complete, contiguous same-session bars");
    if (!pr) {
      unavailable("PE−CE action", "No same-session option snapshot within the freshness limit");
      unavailable("PCR trend", "No same-session PCR snapshot within the freshness limit");
    }
    if (!f0) for (const factor of ["Writers gap","Δput−Δcall","Writer dominance",...(!trinityFresh ? ["Writer flip"] : [])] as Factor[]) unavailable(factor,"No fresh same-session writer flow");
    if (!smt) unavailable("SMT", "SMT unavailable for this timeframe");
    if (!cctData?.available) unavailable("CCT", "CCT confirmation events unavailable");
    if (!a.mmxm?.available) unavailable("MMXM", "MMXM confirmation events unavailable");
    unavailable("Gamma flip", "Current gamma snapshot is supporting context only; as-of history is not recorded");
    unavailable("★ A+ Confluence", "Current A+ snapshot is context only; no as-of history recorded");
    const avail = FACTORS.length-missing.length;
    const bull = FACTORS.filter((factor) => votes[factor] === "bull").length;
    const bear = FACTORS.filter((factor) => votes[factor] === "bear").length;
    const result = biasQualification(bull,bear,avail,pct);
    const context: BiasRow["context"] = [];
    if (currentSnapshot) {
      const state=a.prahaar?.latest_state;
      context.push({name:"Prahaar",text:state ? `${state.current_verdict} · EMA ${state.ema_trend}, momentum ${state.macd_momentum}` : "No current engine state"});
      const demand=a.gtf?.nearest_demand, supply=a.gtf?.nearest_supply;
      context.push({name:"GTF 2.0",text:a.gtf?.available ? `Demand ${demand ? `${demand.bottom}–${demand.top} (${demand.status})` : "none"} · Supply ${supply ? `${supply.bottom}–${supply.top} (${supply.status})` : "none"}` : "No current zones"});
      if (cctData?.available) context.push({name:"CCT snapshot",text:cctData.continuity});
      if (a.mmxm?.primary) context.push({name:"MMXM snapshot",text:`${a.mmxm.primary.model} ${a.mmxm.primary.phase}`});
      if (gex?.ok) context.push({name:"Gamma snapshot",text:`Flip ${gex.flip ?? "—"} · sampled ${gex.ts || "unknown time"}`});
      if (aplusTrig) context.push({name:"A+ snapshot",text:`${aplusTrig.status}: ${aplusTrig.desc}`});
      const health=a.volume?.oi_health;
      context.push({name:"Futures OI",text:health?.sample_quality === "ok" ? `${a.volume?.oi_last?.build || "baseline / flat"} · contract ${health.contract}` : `Not comparable: ${health?.sample_quality || "unverified"}`});
    }
    return { time:c.time,o:c.open,h:c.high,l:c.low,c:c.close,votes,why,bull,bear,avail,
      ...result,missing,neutral:avail-bull-bear,forming:isLive,warn:warn || undefined,context };

  });
}

export function biasQualification(bull:number,bear:number,avail:number,pct=BIAS_POLICY.agreement) {
  const required=Math.max(BIAS_POLICY.minimum_votes,Math.ceil(pct*avail));
  const maxAgainst=BIAS_POLICY.max_opposition.find((tier)=>avail>=tier.min_available)?.votes ?? 1;
  const lean=bull>bear ? "BULLISH" as const : bear>bull ? "BEARISH" as const : "BALANCED" as const;
  const dominant=Math.max(bull,bear),opposing=Math.min(bull,bear);
  const blockers:string[]=[];
  if (avail<BIAS_POLICY.minimum_votes) blockers.push(`Insufficient data: ${avail} available`);
  if (dominant<required) blockers.push(`Need ${required} agreeing; have ${dominant}`);
  if (opposing>maxAgainst) blockers.push(`${opposing} opposing; maximum ${maxAgainst}`);
  const verdict=blockers.length || bull===bear ? "MIXED" as const : bull>bear ? "BUY" as const : "SELL" as const;
  return {required,maxAgainst,blockers,lean,verdict,strong:verdict!=="MIXED" && dominant>=Math.ceil(BIAS_POLICY.strong_agreement*avail)};
}

// New factors are composite, correlated criteria—not three independent confirmations.
export function additionalBiasVotes(c: Candle, closed: Candle[], a: Analysis, barSec: number) {
  type Reading = {vote: Vote; why:string; available:boolean};
  const none=(why:string,available=true):Reading=>({vote:null,why,available});
  const result:Record<"Prahaar confirmation"|"GTF zone rejection"|"Futures OI build-up",Reading>={
    "Prahaar confirmation":none("No timestamped Prahaar confirmation in the window",!!a.prahaar?.available),
    "GTF zone rejection":none("No first-touch GTF zone rejection on this candle",!!a.gtf?.available),
    "Futures OI build-up":none("No aligned, contract-verified OI delta",false),
  };
  if (c.closed===false) return result;
  const settings=BIAS_POLICY.additional_factors;
  const day=(time:number)=>new Date(time*1000).toISOString().slice(0,10);
  const byTime=new Map(closed.map((bar)=>[bar.time,bar]));
  if (a.prahaar?.available) {
    const signals=(a.prahaar.signals ?? []).map((signal)=>({signal,time:signal.chart_time ?? signal.time}))
      .filter(({signal,time})=>byTime.has(time) && day(time)===day(c.time) && time<=c.time && time>=c.time-(settings.prahaar_window_bars-1)*barSec && signal.vix_ok!==false)
      .filter(({signal,time})=>Number.isFinite(signal.sl) && !closed.some((bar)=>bar.time>time && bar.time<=c.time && (signal.dir==="bull" ? bar.low<=signal.sl : bar.high>=signal.sl)))
      .sort((x,y)=>y.time-x.time);
    if (signals.length) {
      const latest=signals.filter((item)=>item.time===signals[0].time);
      const directions=new Set(latest.map((item)=>item.signal.dir));
      result["Prahaar confirmation"]=directions.size===1 ? {vote:signals[0].signal.dir,why:`Closed Prahaar ${signals[0].signal.dir} confirmation; EMA/MACD/ADX treated as one factor`,available:true} : none("Conflicting Prahaar confirmations on the same candle");
    }
  }
  if (a.gtf?.available) {
    const reactions:{dir:Vote;id:string}[]=[];
    let knownZones=0;
    for (const zone of a.gtf.zones ?? []) {
      if (!Number.isFinite(zone.from_raw) || !Number.isFinite(zone.explosive_time)) continue;
      // from is chart-clock; from_raw/explosive_time share exchange epoch. No timezone guessing.
      const formed=zone.explosive_time+(zone.from-zone.from_raw!);
      if (!byTime.has(formed) || (settings.gtf_same_session && day(formed)!==day(c.time)) || formed>=c.time || zone.explosive_body_ratio<settings.gtf_min_explosive_ratio || zone.base_bars>settings.gtf_max_base_bars) continue;
      const path=closed.filter((bar)=>bar.time>=formed && bar.time<=c.time);
      if (path.some((bar,index)=>index>0 && bar.time-path[index-1].time!==barSec)) continue;
      knownZones++;
      const since=path.filter((bar)=>bar.time>formed && bar.time<c.time);
      const invalid=since.some((bar)=>zone.dir==="bull" ? bar.close<zone.bottom : bar.close>zone.top);
      const touches=since.filter((bar)=>bar.high>=zone.bottom && bar.low<=zone.top).length;
      if (invalid || touches>settings.gtf_max_prior_touch_bars) continue;
      const touching=c.high>=zone.bottom && c.low<=zone.top;
      const rejection=zone.dir==="bull" ? c.close>zone.top && c.close>c.open : c.close<zone.bottom && c.close<c.open;
      if (touching && rejection) reactions.push({dir:zone.dir,id:zone.id});
    }
    if (reactions.length) {
      const directions=new Set(reactions.map((r)=>r.dir));
      result["GTF zone rejection"]=directions.size===1 ? {vote:reactions[0].dir,why:`First closed-candle rejection of formed GTF ${reactions[0].dir==="bull" ? "demand" : "supply"}; ${reactions.length} zones count once`,available:true} : none("Conflicting demand and supply reactions; no vote");
    } else if ((a.gtf.zones ?? []).length && !knownZones) {
      result["GTF zone rejection"]=none("No eligible GTF formation with loaded price history; latest zone status is not backfilled",false);
    }
  }
  const oi=(a.volume?.oi_line ?? []).find((point)=>point.time===c.time);
  const oiStaleNow = c.time===closed.at(-1)?.time && a.volume?.oi_health?.state==="stale";
  if (!oiStaleNow && oi && oi.quality==="ok" && oi.contract && oi.doi!=null && Number.isFinite(oi.doi) && oi.sample_ts!=null && oi.sample_ts>=oi.ts && oi.sample_ts<oi.ts+barSec) {
    const directions:Record<string,Vote>={"long build-up":"bull","short covering":"bull","short build-up":"bear","long unwinding":"bear"};
    result["Futures OI build-up"]={vote:directions[oi.build ?? ""] ?? null,available:true,
      why:`${oi.build || "baseline / flat"} · contract ${oi.contract} · ΔOI ${oi.doi}; conventional price/OI inference`};
  }
  return result;
}

// Shared causal event detector: positive PE−CE is conventionally bullish, irrespective of line colour.
// Divergence is a paired-endpoint warning, not proof of absorption or an extra directional vote.
export type PositionPoint={time:number;ce:number;pe:number;price:number;sample?:number;duration?:number};
export type PositionEvent={time:number;dir:"bull"|"bear";kind:"cross"|"divergence";text:string};
export function positioningEvents(input:PositionPoint[],cutoff=Infinity):PositionEvent[] {
  const cfg=BIAS_POLICY.positioning_signals;
  const points=input.filter((p)=>p.time+(p.duration ?? 0)<=cutoff && p.time<cutoff && Number.isFinite(p.ce) && Number.isFinite(p.pe)).slice().sort((a,b)=>a.time-b.time)
    .filter((p,i,all)=>!i || p.time!==all[i-1].time);
  const events:PositionEvent[]=[];
  const day=(t:number)=>new Date(t*1000).toISOString().slice(0,10);
  const contiguous=(slice:PositionPoint[])=>slice.every((p,i)=>!i || (day(p.time)===day(slice[i-1].time) && p.time-slice[i-1].time<=cfg.max_gap_s && (p.sample==null || p.sample!==slice[i-1].sample)));
  const n=cfg.confirmation_points;
  for(let i=n;i<points.length;i++){
    const window=points.slice(i-n,i+1),p=points[i],before=window[0],gap=p.pe-p.ce;
    if(!contiguous(window))continue;
    const sign=Math.sign(gap);
    if(sign && Math.sign(before.pe-before.ce)!==sign && window.slice(1).every((x)=>Math.sign(x.pe-x.ce)===sign)){
      const dir=sign>0?"bull":"bear";
      events.push({time:p.time,dir,kind:"cross",text:sign>0?"Bullish crossover confirmed: Put > Call (red above green)":"Bearish crossover confirmed: Call > Put (green above red)"});
    }
    if(i>=cfg.divergence_points-1){
      const segment=points.slice(i-cfg.divergence_points+1,i+1),first=segment[0];
      if(!contiguous(segment) || !segment.every((x)=>Number.isFinite(x.price) && x.price>0))continue;
      const priceChange=(p.price-first.price)/first.price*100;
      const gapChange=gap-(first.pe-first.ce);
      const size=Math.max(Math.abs(first.ce)+Math.abs(first.pe),Math.abs(p.ce)+Math.abs(p.pe));
      if(size>0 && Math.abs(gapChange)/size>=cfg.min_gap_change_fraction && Math.abs(priceChange)>=cfg.min_price_change_pct && Math.sign(priceChange)!==Math.sign(gapChange)){
        const dir=gapChange>0?"bull":"bear";
        events.push({time:p.time,dir,kind:"divergence",text:dir==="bull"?"Bullish divergence warning: price down, PE−CE gap improving":"Bearish divergence warning: price up, PE−CE gap weakening"});
      }
    }
  }
  return events;
}
export type TrinitySignalRow={time:number;fut_close:number;signal_point?:{sample_time:number;ce:number;pe:number;ce_change:number;pe_change:number}|null};
export function trinitySignalPoints(rows:TrinitySignalRow[],fresh=true,cutoff=Infinity):PositionPoint[]{
  return rows.filter((p)=>p.signal_point && p.time<cutoff).map((p)=>({time:p.time,duration:BIAS_POLICY.positioning_signals.trinity_bar_seconds,price:p.fut_close,sample:p.signal_point!.sample_time,
    ce:fresh?p.signal_point!.ce_change:p.signal_point!.ce,pe:fresh?p.signal_point!.pe_change:p.signal_point!.pe}));
}
