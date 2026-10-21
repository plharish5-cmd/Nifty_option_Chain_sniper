// bias.ts — 7-factor confluence per CLOSED candle: FVG/IFVG · Order block · CRT/TBS · Footprint · RVOL · VWAP · Structure.
// Each factor votes bull / bear / none. A candle gets a BUY/SELL bias badge when ≥ 5 of 7 agree and ≤ 1 disagrees.
// Information only — not backtested, never an order, never changes Setups A–D or paper trades.
import type { Analysis, Candle } from "../api";
import type { FpBar } from "./footprint";

export type Vote = "bull" | "bear" | null;
export const FACTORS = [
  "FVG/IFVG", "Order block", "Rejection block", "CRT/TBS", "Footprint", "RVOL", "VWAP",
  "Structure", "PE−CE action", "Writers gap", "Δput−Δcall", "Writer dominance", "Writer flip",
  "PCR trend", "SMT", "CCT", "MMXM", "CVD flow", "Gamma flip", "★ A+ Confluence"
] as const;
import type { Row } from "./OcLab";

// Options Lab feeds (from 8501 via /api/oclab): PE−CE table rows (3m, ATM ±10) and the writer-flow points (1m, all strikes)
export type OcRow = Row;
export type OcPt = { t: number; ce: number | null; pe: number | null; flow: number | null; bal: number | null; sig: boolean;
  flip?: boolean; regime?: string; conf?: string | null };
export type Gex = { ok: boolean; spot?: number; flip?: number | null; regime?: string; pin?: number; accel?: number | null; message?: string };
export type OcFeed = { pece: OcRow[]; flow: OcPt[]; gex?: Gex | null; day?: string };
export type Factor = typeof FACTORS[number];
export type BiasRow = { time: number; o: number; h: number; l: number; c: number; votes: Record<Factor, Vote>; why: Record<Factor, string>;
  bull: number; bear: number; avail: number; verdict: "BUY" | "SELL" | "MIXED"; strong: boolean; forming?: boolean; warn?: string };

const FLOW = new Set(["STACKED_BUY", "STACKED_SELL", "ACCUMULATION", "DISTRIBUTION", "INITIATIVE", "MANIPULATION", "ABSORPTION"]);

export function computeBias(candles: Candle[], a: Analysis | null, fp: FpBar[], barSec: number, pct = 0.7,
                            oc: OcFeed | null = null, forming = false,
                            smt: { time: number; dir: "bull" | "bear"; at: string }[] | null = null): BiasRow[] {
  if (!a) return [];
  const closed = candles.filter((c) => c.closed !== false);
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
    const cr = [...crt].reverse().find((m) => m.time <= c.time && m.time >= c.time - 6 * barSec);
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
    const s = [...struct].reverse().find((m) => m.time <= c.time);
    votes.Structure = s ? s.dir : null; why.Structure = s ? `last ${s.kind} ${s.dir === "bull" ? "▲" : "▼"} ${s.at}` : "";

    // 8 PE−CE table action at the candle close ("BUY CE…", "CE on dip…" → BUY · "…PE…" → SELL · WAIT / no entry → no vote)
    const cut = hmOf(tEnd);
    const dayOf = (t: number) => new Date(t * 1000).toISOString().slice(0, 10);
    const sameDay = !oc?.day || dayOf(c.time) === oc.day;           // 8501 data is for ONE session — never borrow it for other days
    const pr = sameDay ? [...pece].reverse().find((r) => r.ts <= cut) : undefined;
    const act = (pr?.act?.[0] ?? "").replace(/<[^>]+>/g, "");
    votes["PE−CE action"] = /\bCE\b/.test(act) && !/no fresh|after close/i.test(act) ? "bull" : /\bPE\b/.test(act) && !/no fresh|after close/i.test(act) ? "bear" : null;
    why["PE−CE action"] = pr ? `${pr.ts} ${act || "—"}` : "";
    // 9 Writers gap (put − call OI change since yesterday): which way the gap moved over the last 30 min
    const f0 = sameDay ? flowAt(tEnd) : undefined, f30 = sameDay ? flowAt(tEnd - 1800) : undefined;
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

    // 13 PCR trend (OI PCR from the PE−CE table, ATM ±10): rising over 30 min = put writing = BUY, falling = SELL
    //    + ⚠ crowding: PCR > 1.5 and turning down, or < 0.5 and turning up → reversal risk (shown on the badge, not a vote)
    let pv: Vote = null, pw = "", warn = "";
    const pNow = sameDay ? [...pece].reverse().find((r) => r.ts <= cut && r.pcr != null) : undefined;
    const pThen = !sameDay ? undefined : [...pece].reverse().find((r) => r.ts <= hmOf(tEnd - 1800) && r.pcr != null);
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

    // 15 CCT — Candle Continuity Theory (HTF open benchmark & 4-candle execution)
    const cctData = a.cct;
    let cctV: Vote = null, cctW = "no CCT data";
    if (cctData?.available) {
      if (cctData.continuity.includes("BULL")) {
        cctV = "bull";
        cctW = `CCT ${cctData.continuity} (Day Open ${cctData.day_open} · ${cctData.guidance})`;
      } else if (cctData.continuity.includes("BEAR")) {
        cctV = "bear";
        cctW = `CCT ${cctData.continuity} (Day Open ${cctData.day_open} · ${cctData.guidance})`;
      } else {
        cctW = `CCT Neutral / Chop (${cctData.guidance})`;
      }
    }
    votes.CCT = cctV; why.CCT = cctW;

    // 16 MMXM — Market Maker Models (MMBM / MMSM State Machine)
    const mmxmModel = a.mmxm?.primary;
    let mmxmV: Vote = null, mmxmW = "no active MMXM cycle";
    if (mmxmModel) {
      if (mmxmModel.dir === "bull" && ["SMR_CONFIRMED", "LOW_RISK_BUY", "REACCUMULATION"].includes(mmxmModel.phase)) {
        mmxmV = "bull";
        mmxmW = `MMBM ${mmxmModel.phase} → Target ${mmxmModel.target} (EQ ${mmxmModel.target_eq})`;
      } else if (mmxmModel.dir === "bear" && ["SMR_CONFIRMED", "LOW_RISK_SELL", "REDISTRIBUTION"].includes(mmxmModel.phase)) {
        mmxmV = "bear";
        mmxmW = `MMSM ${mmxmModel.phase} → Target ${mmxmModel.target} (EQ ${mmxmModel.target_eq})`;
      } else {
        mmxmW = `${mmxmModel.model} ${mmxmModel.phase}`;
      }
    }
    votes.MMXM = mmxmV; why.MMXM = mmxmW;

    // 17 CVD flow — Cumulative Volume Delta 3-bar slope & 6-bar divergence (exhaustion / absorption trap)
    let cvdV: Vote = null, cvdW = "";
    const fi = fp.findIndex((x) => x.time === c.time);
    if (f && fi >= 0) {
      const p3 = fp[fi - 3];
      const dc = p3 ? f.cvd - p3.cvd : 0;
      const win = fp.slice(Math.max(0, fi - 6), fi);
      let div: Vote = null, divW = "";
      if (win.length >= 4) {
        const hh = c.high > Math.max(...win.map((x) => x.h ?? 0));
        const ll = c.low < Math.min(...win.map((x) => x.l ?? Infinity));
        if (hh && f.cvd < Math.max(...win.map((x) => x.cvd))) {
          div = "bear"; divW = "⚠ bearish CVD divergence (exhaustion high)";
        } else if (ll && f.cvd > Math.min(...win.map((x) => x.cvd))) {
          div = "bull"; divW = "⚠ bullish CVD divergence (absorption low)";
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
    if (gex && gex.ok && gex.flip != null) {
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

    if (aplusTrig && aplusTrig.status === "ACTIVE") {
      aplusV = aplusTrig.dir ?? null;
      aplusW = aplusTrig.desc;
    } else if (aplusTrig && aplusTrig.status === "VETOED") {
      aplusW = `⚠ VETOED: ${aplusTrig.desc}`;
    } else {
      const bestSup = sniper?.best_supply;
      const bestDem = sniper?.best_demand;
      const inSupply = bestSup && c.high >= bestSup.bot && c.low <= bestSup.top + atr;
      const inDemand = bestDem && c.low <= bestDem.top && c.high >= bestDem.bot - atr;

      if (inDemand && (!bestDem?.score || bestDem.score >= 4.0)) {
        aplusV = "bull";
        aplusW = `★ A+ Demand [${bestDem.bot.toFixed(0)}–${bestDem.top.toFixed(0)}] · ${bestDem.score.toFixed(1)}/10 (${bestDem.tags?.slice(0, 2).join(" + ") || "POI"})`;
      } else if (inSupply && (!bestSup?.score || bestSup.score >= 4.0)) {
        aplusV = "bear";
        aplusW = `★ A+ Supply [${bestSup.bot.toFixed(0)}–${bestSup.top.toFixed(0)}] · ${bestSup.score.toFixed(1)}/10 (${bestSup.tags?.slice(0, 2).join(" + ") || "POI"})`;
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

    // factors with no data today don't count (e.g. futures volume missing → RVOL & VWAP can't vote)
    if (!hasVol) why.RVOL = "no futures volume today";
    if (!hasVwap) why.VWAP = "no VWAP (futures volume missing)";
    if (!hasFp) { why.Footprint = "no recorded ticks for this tf"; why["CVD flow"] = "no footprint/CVD data"; }
    if (!hasPece) { why["PE−CE action"] = "Options Lab (8501) data not available"; why["PCR trend"] = "Options Lab (8501) data not available"; }
    if (!hasFlow) for (const k of ["Writers gap", "Δput−Δcall", "Writer dominance", "Writer flip"] as Factor[]) why[k] = "Options Lab (8501) data not available";
    if (!smt) why.SMT = "SMT: 5m / 15m / 1h only (BANKNIFTY candles)";
    if (!cctData?.available) why.CCT = "CCT not computed for this timeframe";
    if (!a.mmxm?.available) why.MMXM = "MMXM not computed for this timeframe";
    const hasAdvPa = Boolean(a.pa_advanced?.available);
    if (!hasAdvPa) why["★ A+ Confluence"] = "Adv PA not available for this timeframe";
    if (isLive) { why.Footprint = "footprint only for closed candles"; why["CVD flow"] = "CVD flow only for closed candles"; }
    const hasCct = Boolean(cctData?.available);
    const hasMmxm = Boolean(a.mmxm?.available);
    const hasCvd = hasFp && !isLive;
    const hasGex = Boolean(oc?.gex?.ok && oc?.gex?.flip != null);
    const avail = FACTORS.length
      - (smt ? 0 : 1)
      - (hasVol ? 0 : 1)
      - (hasVwap ? 0 : 1)
      - (hasFp && !isLive ? 0 : 1)
      - (hasPece ? 0 : 2)
      - (hasFlow ? 0 : 4)
      - (hasCct ? 0 : 1)
      - (hasMmxm ? 0 : 1)
      - (hasCvd ? 0 : 1)
      - (hasGex ? 0 : 1)
      - (hasAdvPa ? 0 : 1);
    const vals = Object.values(votes);
    const bull = vals.filter((x) => x === "bull").length, bear = vals.filter((x) => x === "bear").length;
    const req = Math.max(3, Math.round(pct * avail));             // e.g. 10 factors → 7 must agree; 7 → 5
    const maxAgainst = avail >= 10 ? 3 : avail >= 8 ? 2 : 1;
    const verdict = bull >= req && bear <= maxAgainst ? "BUY" : bear >= req && bull <= maxAgainst ? "SELL" : "MIXED";
    return { time: c.time, o: c.open, h: c.high, l: c.low, c: c.close, votes, why, bull, bear, avail, verdict,
      strong: Math.max(bull, bear) >= Math.round(0.85 * avail), forming: isLive, warn: warn || undefined };
  });
}
