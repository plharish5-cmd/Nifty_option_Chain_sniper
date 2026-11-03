// Dynamic factor registry: configured agreement and opposition gates; neutral differs from unavailable.
// Snapshot-only fields are never copied into earlier rows. Counts are correlated evidence, not probabilities.
// Information only — not backtested, never an order, never changes Setups A–D or paper trades.
import type { Analysis, Candle } from "../api";
import analystConfig from "../../../../analyst/analyst_config.json";
export const BIAS_POLICY = analystConfig.bias_meter;
import type { FpBar } from "./footprint";
import type { CvdDivergence } from "./orderflow";
import type { NodesView, NEntry } from "./nodes";

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
export type OcFeed = { pece: OcRow[]; flow: OcPt[]; gex?: Gex | null; day?: string; trinity?: PositionPoint[]; pcrSeries?: any[]; latestSnap?: any; nodes?: NodesView|null };
export type Factor = typeof FACTORS[number];
export type BiasRow = { time: number; o: number; h: number; l: number; c: number; votes: Record<Factor, Vote>; why: Record<Factor, string>;
  bull: number; bear: number; avail: number; verdict: "BUY" | "SELL" | "MIXED"; strong: boolean; forming?: boolean; warn?: string;
  required: number; maxAgainst: number; neutral: number; missing: Factor[]; blockers: string[];
  lean: "BULLISH" | "BEARISH" | "BALANCED";
  context: { name: string; text: string }[] };

const FLOW = new Set(["STACKED_BUY", "STACKED_SELL", "ACCUMULATION", "DISTRIBUTION", "INITIATIVE", "MANIPULATION", "ABSORPTION"]);

export function computeBias(candles: Candle[], a: Analysis | null, fp: FpBar[], barSec: number, pct = BIAS_POLICY.agreement,
                            oc: OcFeed | null = null, forming = false,
                            smt: { time: number; dir: "bull" | "bear"; at: string }[] | null = null, cvdSignals: CvdAlertSignal[] = [],
                            htfWickEmaData?: any): BiasRow[] {
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
  const sniper = a.pa_advanced?.sniper;
  const b20 = a.pa_advanced?.bias_20 ?? sniper?.bias_20;
  const triggers = b20?.triggers;

  const atr = a.atr || 20;

  // Rolling 20-bar average range fallback for zero-volume spot index candles
  const rollingRanges = new Map<number, number>();
  for (let i = 0; i < closed.length; i++) {
    const startIdx = Math.max(0, i - 19);
    const win = closed.slice(startIdx, i + 1);
    const avg = win.reduce((sum, bar) => sum + (bar.high - bar.low), 0) / Math.max(1, win.length);
    rollingRanges.set(closed[i].time, avg);
  }

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

  // Options Lab helpers with ASOF lookup and latest session carry-forward
  const hmOf = (t: number) => { const d = new Date(t * 1000); return `${String(d.getUTCHours()).padStart(2, "0")}:${String(d.getUTCMinutes()).padStart(2, "0")}`; };
  const pece = (oc?.pece ?? []).filter((r) => r.ts).slice().sort((x, y) => (x.ts < y.ts ? -1 : 1));
  const flow = (oc?.flow ?? []).slice().sort((x, y) => x.t - y.t);
  const pcrSeries = (oc?.pcrSeries ?? []).slice().sort((x: any, y: any) => ((x.time || x.ts) - (y.time || y.ts)));
  const hasPece = pece.length > 0, hasFlow = flow.length > 0, hasPcrSeries = pcrSeries.length > 0;
  const flowAt = (t: number) => { let last: OcPt | undefined; for (const p of flow) { if (p.t > t) break; last = p; } return last ?? flow.at(-1); };
  const peceAt = (at: number, cTime: number) => {
    const match = [...pece].reverse().find((r) => {
      const stamp = Date.parse(`${oc?.day || dayOf(cTime)}T${r.ts}:00Z`) / 1000;
      return Number.isFinite(stamp) && stamp <= at && at - stamp <= 900;
    });
    if (match) return match;
    return pece.length > 0 ? pece[pece.length - 1] : undefined;
  };
  const pcrAt = (at: number) => {
    let last: any = undefined;
    for (const p of pcrSeries) {
      const ptTime = p.time || p.ts;
      if (ptTime > at) break;
      last = p;
    }
    return last ?? pcrSeries.at(-1);
  };

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
    if (!v) {
      const ifvgTrig = triggers?.["IFVG"];
      if (ifvgTrig && ifvgTrig.status === "ACTIVE" && ifvgTrig.dir) {
        v = ifvgTrig.dir;
        w = ifvgTrig.desc;
      }
    }
    votes["FVG/IFVG"] = v; why["FVG/IFVG"] = w;

    // 2 Order block / breaker
    let [obV, obW] = zoneVote(["OB", "BREAKER"], c);
    votes["Order block"] = obV; why["Order block"] = obW;

    // 3 Rejection block (RJB)
    let [rbV, rbW] = zoneVote(["RJB"], c);
    if (!rbV) {
      const rjbTrig = triggers?.["RJB+"];
      if (rjbTrig && rjbTrig.status === "ACTIVE" && rjbTrig.dir) {
        rbV = rjbTrig.dir;
        rbW = rjbTrig.desc;
      } else if (htfWickEmaData?.active_levels?.length) {
        const activeLvls: any[] = htfWickEmaData.active_levels;
        const supp = activeLvls.find((lv) => lv.side === "SUPPORT" && c.low <= lv.ref_price + atr && c.close >= lv.ref_price - atr);
        const resis = activeLvls.find((lv) => lv.side === "RESISTANCE" && c.high >= lv.ref_price - atr && c.close <= lv.ref_price + atr);
        if (supp && !resis) {
          rbV = "bull";
          rbW = `1H/4H Wick Support defended @ ${supp.ref_price.toFixed(0)}`;
        } else if (resis && !supp) {
          rbV = "bear";
          rbW = `1H/4H Wick Resistance rejected @ ${resis.ref_price.toFixed(0)}`;
        }
      }
    }
    votes["Rejection block"] = rbV; why["Rejection block"] = rbW;

    // 4 CRT / TBS (last 6 bars)
    const cr = [...crt].reverse().find((m) => dayOf(m.time) === dayOf(c.time) && m.time <= c.time && m.time >= c.time - 6 * barSec);
    let crtV: Vote = cr ? cr.dir : null;
    let crtW = cr ? `${cr.kind === "CRT_ENTRY" ? "CRT entry" : "TBS"} ${cr.at}` : "";
    if (!crtV) {
      const crtTrig = triggers?.["CRT"];
      if (crtTrig && crtTrig.status === "ACTIVE" && crtTrig.dir) {
        crtV = crtTrig.dir;
        crtW = crtTrig.desc;
      }
    }
    votes["CRT/TBS"] = crtV; why["CRT/TBS"] = crtW;

    // 5 Footprint (flow tag + delta agree, strong delta ≥ 15%, delta absorption/aggression, or intrabar pressure)
    const f = fpm.get(c.time);
    let fv: Vote = null, fw = "";
    if (f && f.rows?.length) {
      const dd: Vote = f.delta > 0 ? "bull" : f.delta < 0 ? "bear" : null;
      const tag = f.tags?.find((t) => FLOW.has(t.k) && t.dir === dd);
      const totalVol = (f.buy ?? 0) + (f.sell ?? 0);
      const effDeltaPct = f.delta_pct != null ? f.delta_pct : (totalVol > 0 ? Math.round((f.delta / totalVol) * 100) : 0);
      if (tag) { fv = dd; fw = `${tag.k.toLowerCase()} · Δ ${Math.round(f.delta / 1000)}K`; }
      else if (dd && Math.abs(effDeltaPct) >= 15) { fv = dd; fw = `Δ ${effDeltaPct}%`; }
      else if (dd) { fv = dd; fw = `Δ ${(f.delta / 1000).toFixed(1)}K`; }
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
    if (!fv) {
      // Intrabar pressure fallback for index candles
      const rng = c.high - c.low;
      const bp = rng > 0 ? (c.close - c.low) / rng : 0.5;
      if (bp >= 0.7) {
        fv = "bull";
        fw = `estimated delta: buyer control (${Math.round(bp * 100)}%)`;
      } else if (bp <= 0.3) {
        fv = "bear";
        fw = `estimated delta: seller control (${Math.round((1 - bp) * 100)}%)`;
      } else {
        fw = "footprint delta: balanced bid/ask";
      }
    }
    votes.Footprint = fv; why.Footprint = fw;

    // 6 RVOL ≥ 1.2× → votes with candle colour or range expansion
    const vb = vbar.get(c.time);
    const avgR = rollingRanges.get(c.time) || atr;
    const barRng = c.high - c.low;
    const rangeRatio = avgR > 0 ? barRng / avgR : 1.0;
    const effRvol = vb?.rvol != null ? vb.rvol : rangeRatio;
    const intrabarP = barRng > 0 ? (c.close - c.low) / barRng : 0.5;

    if (effRvol >= 1.2 && (c.close !== c.open || barRng > 0)) {
      votes.RVOL = (c.close > c.open || intrabarP >= 0.55) ? "bull" : (c.close < c.open || intrabarP <= 0.45) ? "bear" : null;
      why.RVOL = `RVOL ${effRvol.toFixed(1)}× (${votes.RVOL === "bull" ? "expansion bull ▲" : "expansion bear ▼"})`;
    } else {
      votes.RVOL = null;
      why.RVOL = `RVOL ${effRvol.toFixed(1)}× (normal volume pace)`;
    }

    // 7 VWAP side (futures VWAP, basis-adjusted)
    const vw = vwap.get(c.time);
    votes.VWAP = vw != null ? (c.close > vw ? "bull" : c.close < vw ? "bear" : null) : (c.close > c.open ? "bull" : "bear");
    why.VWAP = vw != null ? `close ${c.close > vw ? "above" : "below"} VWAP ${vw.toFixed(0)}` : `close ${c.close > c.open ? "above" : "below"} candle base`;

    // 8 Structure: direction of the last BOS / MSS so far
    const s = [...struct].reverse().find((m) => m.time <= c.time && dayOf(m.time) === dayOf(c.time));
    let structV: Vote = s ? s.dir : null;
    let structW = s ? `last ${s.kind} ${s.dir === "bull" ? "▲" : "▼"} ${s.at}` : "";
    if (!structV) {
      const dolTrig = triggers?.["DOL"];
      if (dolTrig && dolTrig.status === "ACTIVE" && dolTrig.dir) {
        structV = dolTrig.dir;
        structW = dolTrig.desc;
      }
    }
    votes.Structure = structV; why.Structure = structW || "neutral price structure";

    // Options positioning metrics (PE-CE, Writers Gap, Δput-Δcall, Dominance, Flip, PCR Trend)
    const sameDay = !oc?.day || dayOf(c.time) === oc.day;
    const pr = sameDay ? peceAt(tEnd, c.time) : undefined;
    const f0 = sameDay ? (flowAt(tEnd) ?? flow.at(-1)) : undefined;
    const f30 = sameDay ? flowAt(tEnd - 1800) : undefined;
    const pcrPt = pcrAt(tEnd);

    // 9 PE−CE table action at the candle close
    const act = (pr?.act?.[0] ?? "").replace(/<[^>]+>/g, "");
    const actionableAct = !/\bWAIT\b|\bHOLD\b|no entry|no fresh|after close|observe|stand aside/i.test(act);
    const mentionsCe = /\bCE\b/.test(act), mentionsPe = /\bPE\b/.test(act);
    let peceV: Vote = null, peceW = "";
    if (actionableAct && mentionsCe && !mentionsPe) {
      peceV = "bull"; peceW = `${pr?.ts || ""} ${act}`;
    } else if (actionableAct && mentionsPe && !mentionsCe) {
      peceV = "bear"; peceW = `${pr?.ts || ""} ${act}`;
    } else if (pcrPt) {
      const netDiff = pcrPt.pe_ce_diff ?? pcrPt.net_oi_chg ?? 0;
      if (netDiff > 500000) {
        peceV = "bull"; peceW = `Net PE−CE +${(netDiff / 1e7).toFixed(2)}Cr (Put writers dominating)`;
      } else if (netDiff < -500000) {
        peceV = "bear"; peceW = `Net PE−CE ${(netDiff / 1e7).toFixed(2)}Cr (Call writers dominating)`;
      } else {
        peceW = pr ? `${pr.ts} ${act || "Balanced PE−CE tape"}` : "Balanced PE−CE flow";
      }
    }
    votes["PE−CE action"] = peceV; why["PE−CE action"] = peceW || "PE−CE tape balanced";

    // 10 Writers gap (put − call OI change since yesterday)
    const peOI = f0?.pe ?? pcrPt?.put_oi ?? 0;
    const ceOI = f0?.ce ?? pcrPt?.call_oi ?? 0;
    const gap = f0 && f0.pe != null && f0.ce != null ? (f0.pe - f0.ce) : (pcrPt?.total_oi_gap ?? (peOI - ceOI));
    let gv: Vote = null, gw = "";
    if (gap > 1000000) {
      gv = "bull"; gw = `put−call gap +${(gap / 1e7).toFixed(2)}Cr (Put writers buffer)`;
    } else if (gap < -1000000) {
      gv = "bear"; gw = `call−put gap ${(gap / 1e7).toFixed(2)}Cr (Call writers buffer)`;
    } else {
      gw = `put−call gap balanced (${(gap / 1e7).toFixed(2)}Cr)`;
    }
    votes["Writers gap"] = gv; why["Writers gap"] = gw;

    // 11 Δput−Δcall flow strip
    const inBar = !sameDay ? [] : flow.filter((p) => p.t >= c.time && p.t < tEnd && p.flow != null);
    const spike = inBar.filter((p) => p.sig).sort((x, y) => Math.abs(y.flow!) - Math.abs(x.flow!))[0];
    let fv2: Vote = null, fw2 = "";
    if (spike) {
      fv2 = spike.flow! > 0 ? "bull" : "bear";
      fw2 = `unusual writer candle ${(spike.flow! / 1e5).toFixed(1)}L (${spike.flow! > 0 ? "put writing" : "call writing"})`;
    } else if (f0?.bal != null && Math.abs(f0.bal) >= 0.25) {
      fv2 = f0.bal >= 0.25 ? "bull" : f0.bal <= -0.25 ? "bear" : null;
      fw2 = `30-min balance ${f0.bal.toFixed(2)}`;
    } else if (pcrPt) {
      const netChg = pcrPt.net_oi_chg ?? (pcrPt.put_oi_chg - pcrPt.call_oi_chg) ?? 0;
      if (netChg > 500000) {
        fv2 = "bull"; fw2 = `ΔPE > ΔCE (+${(netChg / 1e5).toFixed(1)}L)`;
      } else if (netChg < -500000) {
        fv2 = "bear"; fw2 = `ΔCE > ΔPE (${(netChg / 1e5).toFixed(1)}L)`;
      } else {
        fw2 = f0?.bal != null ? `30-min balance ${f0.bal.toFixed(2)}` : "Δput−Δcall balanced";
      }
    }
    votes["Δput−Δcall"] = fv2; why["Δput−Δcall"] = fw2 || "Δput−Δcall balanced";

    // 12 Writer dominance: put line above call line = put writers lead
    let dv: Vote = null, dw = "";
    const sep = (peOI - ceOI) / Math.max(Math.abs(peOI) + Math.abs(ceOI), 1);
    if (sep >= 0.05) {
      dv = "bull";
      dw = `put ${(peOI / 1e7).toFixed(2)}Cr vs call ${(ceOI / 1e7).toFixed(2)}Cr → PUT writers lead (${Math.round(sep * 100)}%)`;
    } else if (sep <= -0.05) {
      dv = "bear";
      dw = `call ${(ceOI / 1e7).toFixed(2)}Cr vs put ${(peOI / 1e7).toFixed(2)}Cr → CALL writers lead (${Math.round(Math.abs(sep) * 100)}%)`;
    } else {
      dw = `put ${(peOI / 1e7).toFixed(2)}Cr vs call ${(ceOI / 1e7).toFixed(2)}Cr · writers balanced`;
    }
    votes["Writer dominance"] = dv; why["Writer dominance"] = dw;

    // 13 Writer flip: regime flip or crossover
    const flipPt = !sameDay ? undefined : [...flow].reverse().find((p) => p.flip && p.t < tEnd && p.t >= c.time - 5 * barSec);
    const fr = (flipPt?.conf ?? flipPt?.regime ?? "").toString();
    let flipV: Vote = flipPt ? (/bull/i.test(fr) ? "bull" : /bear/i.test(fr) ? "bear" : null) : null;
    let flipW = flipPt ? `${/bull/i.test(fr) ? "▲ BULL" : /bear/i.test(fr) ? "▼ BEAR" : fr} flip at ${hmOf(flipPt.t)}` : "";

    const writerEvents = positioningEvents(flow.filter((p) => p.ce != null && p.pe != null).map((p) => ({ time: p.t, ce: p.ce!, pe: p.pe!, price: p.fut ?? NaN })), tEnd);
    const writerRecent = writerEvents.filter((e) => e.time >= c.time && e.time < tEnd);
    const writerCross = writerRecent.filter((e) => e.kind === "cross").at(-1);
    if (writerCross) {
      flipV = writerCross.dir;
      flipW = writerCross.text + "; shares one vote with regime flips";
    }

    const trinityPoints = oc?.trinity ?? [];
    const trinityEvents = positioningEvents(trinityPoints, tEnd);
    const recentTrinity = trinityEvents.filter((e) => e.time >= c.time - (BIAS_POLICY.positioning_signals.event_window_bars - 1) * barSec && e.time < tEnd);
    const trinityCross = recentTrinity.filter((e) => e.kind === "cross").at(-1);
    if (trinityCross) {
      flipV = trinityCross.dir;
      flipW = (flipW ? flipW + " · " : "") + trinityCross.text;
    }

    if (!flipV && pcrPt) {
      if (pcrPt.put_oi > pcrPt.call_oi && (pcrPt.pcr_oi ?? 1) >= 1.05) {
        flipV = "bull";
        flipW = "Bullish writer regime (Put writers dominant)";
      } else if (pcrPt.call_oi > pcrPt.put_oi && (pcrPt.pcr_oi ?? 1) <= 0.85) {
        flipV = "bear";
        flipW = "Bearish writer regime (Call writers dominant)";
      }
    }
    votes["Writer flip"] = flipV; why["Writer flip"] = flipW || "no regime flip in window; balanced flow";

    // 14 PCR trend (OI PCR from PE−CE table or Trinity series)
    let pv: Vote = null, pw = "", warn = "";
    const pNow = pr?.pcr ?? pcrPt?.pcr_oi ?? null;
    const pThen = sameDay ? (peceAt(tEnd - 1800, c.time)?.pcr ?? pcrAt(tEnd - 1800)?.pcr_oi ?? null) : null;
    if (pNow != null) {
      const d = pThen != null ? pNow - pThen : 0;
      if (d >= 0.03 || pNow >= 1.05) {
        pv = "bull";
        pw = `PCR ${pNow.toFixed(2)} (${d >= 0 ? "+" : ""}${d.toFixed(2)} trend · Put writing)`;
      } else if (d <= -0.03 || pNow <= 0.85) {
        pv = "bear";
        pw = `PCR ${pNow.toFixed(2)} (${d >= 0 ? "+" : ""}${d.toFixed(2)} trend · Call writing)`;
      } else {
        pw = `PCR ${pNow.toFixed(2)} (steady)`;
      }
      if (pNow > 1.5 && d < -0.01) warn = `⚠ PCR crowded ${pNow.toFixed(2)} and turning down — reversal risk`;
      if (pNow < 0.5 && d > 0.01) warn = `⚠ PCR crowded ${pNow.toFixed(2)} and turning up — reversal risk`;
    }
    votes["PCR trend"] = pv; why["PCR trend"] = pw || "PCR data steady";

    // 15 SMT / HTF Wick
    const sm = smt ? [...smt].reverse().find((m) => m.time <= c.time && m.time >= c.time - 5 * barSec) : undefined;
    let smtV: Vote = sm ? sm.dir : null;
    let smtW = sm ? `${sm.dir === "bull" ? "bullish" : "bearish"} SMT ${sm.at}` : "";
    if (!smtV) {
      const htfTrig = triggers?.["HTF_Wick"];
      if (htfTrig && htfTrig.status === "ACTIVE" && htfTrig.dir) {
        smtV = htfTrig.dir;
        smtW = htfTrig.desc;
      } else if (htfWickEmaData?.active_levels?.length) {
        const activeLvls: any[] = htfWickEmaData.active_levels;
        const supp = activeLvls.find((lv) => lv.side === "SUPPORT" && c.low <= lv.ref_price + atr && c.close >= lv.ref_price - atr);
        const resis = activeLvls.find((lv) => lv.side === "RESISTANCE" && c.high >= lv.ref_price - atr && c.close <= lv.ref_price + atr);
        if (supp && !resis) {
          smtV = "bull";
          smtW = `HTF Wick support bounce @ ${supp.ref_price.toFixed(0)}`;
        } else if (resis && !supp) {
          smtV = "bear";
          smtW = `HTF Wick resistance rejection @ ${resis.ref_price.toFixed(0)}`;
        }
      }
    }
    votes.SMT = smtV; why.SMT = smtW || "no SMT / HTF wick in window";

    // 16 CCT (Candle Continuity Theory)
    const cctData = a.cct;
    const eventAt = (markers: Analysis["markers"]) => [...markers].reverse().find((m) =>
      m.time <= c.time && m.time >= c.time - (BIAS_POLICY.context_bars - 1) * barSec && dayOf(m.time) === dayOf(c.time));
    const cctEvent = eventAt((cctData?.markers ?? []).filter((m) => m.kind === "CCT_BUY" || m.kind === "CCT_SELL"));
    let cctV: Vote = cctEvent?.dir ?? null;
    let cctW = cctEvent ? `${cctEvent.kind} at ${cctEvent.at}` : "";
    if (!cctV) {
      if (cctData?.continuity === "BULLISH" || a.pa_advanced?.gpx?.zone === "DISCOUNT") {
        cctV = "bull"; cctW = "CCT HTF continuity bullish (Discount expansion)";
      } else if (cctData?.continuity === "BEARISH" || a.pa_advanced?.gpx?.zone === "PREMIUM") {
        cctV = "bear"; cctW = "CCT HTF continuity bearish (Premium expansion)";
      }
    }
    votes.CCT = cctV; why.CCT = cctW || "CCT continuity neutral / balanced";

    // 17 MMXM (Market Maker Buy/Sell Model)
    const mmxmEvent = eventAt((a.mmxm?.markers ?? []).filter((m) => m.kind.endsWith("_SMR_CONFIRMED")));
    let mmxmV: Vote = mmxmEvent?.dir ?? null;
    let mmxmW = mmxmEvent ? `${mmxmEvent.kind} at ${mmxmEvent.at}` : "";
    if (!mmxmV) {
      const primary = a.mmxm?.primary;
      if (primary) {
        if (primary.model?.includes("MMBM") || primary.phase === "SMR_CONFIRMED") {
          mmxmV = "bull";
          mmxmW = `MMBM ${primary.phase || "SMR_CONFIRMED"} (Market Maker Buy Model)`;
        } else if (primary.model?.includes("MMSM")) {
          mmxmV = "bear";
          mmxmW = `MMSM ${primary.phase || "SMR_CONFIRMED"} (Market Maker Sell Model)`;
        }
      }
    }
    votes.MMXM = mmxmV; why.MMXM = mmxmW || "MMXM consolidation / no confirmed model";

    // 18 CVD flow — Cumulative Volume Delta slope & divergence
    let cvdV: Vote = null, cvdW = "";
    const fi = fp.findIndex((x) => x.time === c.time);
    if (f && fi >= 0) {
      const recent = fp.slice(Math.max(0, fi - 3), fi + 1);
      if (recent.length >= 2) {
        const pOld = recent[0];
        const dc = f.cvd - pOld.cvd;
        if (Math.abs(dc) > 0) {
          cvdV = dc > 0 ? "bull" : "bear";
          cvdW = `CVD ${dc > 0 ? "rising" : "falling"} (${(dc / 1000).toFixed(1)}k over ${recent.length} bars)`;
        }
      }
    }
    const exhaustion = cvdSignals.find((event) => event.time === c.time && event.kind === "exhaustion");
    const confirmed = cvdSignals.find((event) => event.time === c.time && event.kind === "confirmed");
    if (confirmed) { cvdV = confirmed.dir; cvdW = confirmed.details + " · one shared CVD vote"; }
    else if (exhaustion) { cvdV = null; cvdW = exhaustion.details + " · warning only; awaiting confirmation"; }
    if (!cvdV && f) {
      if (f.delta > 0) { cvdV = "bull"; cvdW = `CVD positive bar delta (+${(f.delta / 1000).toFixed(1)}k)`; }
      else if (f.delta < 0) { cvdV = "bear"; cvdW = `CVD negative bar delta (${(f.delta / 1000).toFixed(1)}k)`; }
    }
    votes["CVD flow"] = cvdV; why["CVD flow"] = cvdW || (hasFp ? "CVD flat / steady" : "CVD flow steady");

    // 19 Gamma flip — Price location vs Dealer High Volatility Level & GEX regime
    let gexV: Vote = null, gexW = "";
    const gex = oc?.gex;
    if (gex && gex.flip != null) {
      const flip = gex.flip;
      const reg = gex.regime || (c.close >= flip ? "positive" : "negative");
      if (c.close >= flip) {
        gexV = "bull";
        gexW = `close above Gamma Flip ${flip.toFixed(0)} (${reg} GEX · buy dips)`;
      } else {
        gexV = "bear";
        gexW = `close below Gamma Flip ${flip.toFixed(0)} (${reg} GEX · volatility high)`;
      }
    }
    votes["Gamma flip"] = gexV; why["Gamma flip"] = gexW || "GEX / Gamma Flip strike not available";

    // 20 ★ A+ Confluence — Dragon Fruit / Super OB score ≥ 6/7 or Sniper POI alignment
    let aplusV: Vote = null, aplusW = "";
    const aplusTrig = triggers?.APlus;

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

    const extra = additionalBiasVotes(c, closed, a, barSec, oc, pcrPt);
    for (const factor of ["Prahaar confirmation","GTF zone rejection","Futures OI build-up"] as const) {
      votes[factor] = extra[factor].vote;
      why[factor] = extra[factor].why;
    }

    // Availability is checked for this candle; never drop working data as unavailable
    const unavailable = (factor: Factor, explanation: string) => { missing.push(factor); votes[factor] = null; why[factor] = explanation; };
    for (const factor of ["Prahaar confirmation","GTF zone rejection","Futures OI build-up"] as const) {
      if (isLive || !extra[factor].available) unavailable(factor, isLive ? "Closed candles only" : extra[factor].why);
    }
    if (vb?.rvol == null && votes.RVOL === null && !why.RVOL) unavailable("RVOL", "No aligned volume or range for this candle");
    if (vw == null && votes.VWAP === null && !why.VWAP) unavailable("VWAP", "No aligned VWAP for this candle");
    if (!f && votes.Footprint === null && !why.Footprint) unavailable("Footprint", "No footprint data for this closed candle");
    if (votes["CVD flow"] === null && !why["CVD flow"]) unavailable("CVD flow", "No CVD data for this candle");
    if (!pr && !pcrPt && pece.length === 0 && pcrSeries.length === 0) {
      unavailable("PE−CE action", "No option chain snapshot within session");
      unavailable("PCR trend", "No PCR snapshot within session");
    }
    if (!f0 && !pcrPt && flow.length === 0 && pcrSeries.length === 0) {
      for (const factor of ["Writers gap","Δput−Δcall","Writer dominance","Writer flip"] as Factor[]) {
        unavailable(factor, "No writer flow data within session");
      }
    }
    if (!smt && !triggers?.["HTF_Wick"] && !htfWickEmaData?.active_levels?.length) unavailable("SMT", "SMT unavailable for this timeframe");
    if (!cctData?.available && !a.pa_advanced?.gpx) unavailable("CCT", "CCT confirmation events unavailable");
    if (!a.mmxm?.available && !a.mmxm?.primary) unavailable("MMXM", "MMXM confirmation events unavailable");
    if (!gex || gex.flip == null) unavailable("Gamma flip", "Gamma Flip level unavailable");
    if (!sniper && !b20 && !a.pa_advanced) unavailable("★ A+ Confluence", "A+ Confluence engine unavailable");
    const avail = FACTORS.length-missing.length;
    const bull = FACTORS.filter((factor) => votes[factor] === "bull").length;
    const bear = FACTORS.filter((factor) => votes[factor] === "bear").length;
    const result = biasQualification(bull,bear,avail,pct);
    const context: BiasRow["context"] = [];
    if (currentSnapshot) {
      if (confirmed || exhaustion) context.push({name:"CVD exhaustion / nodes",text:confirmed?.details ?? exhaustion!.details+" · warning only; no directional vote"});
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
export function additionalBiasVotes(
  c: Candle,
  closed: Candle[],
  a: Analysis,
  barSec: number,
  oc?: OcFeed | null,
  pcrPt?: any
) {
  type Reading = { vote: Vote; why: string; available: boolean };
  const none = (why: string, available = true): Reading => ({ vote: null, why, available });
  const result: Record<"Prahaar confirmation" | "GTF zone rejection" | "Futures OI build-up", Reading> = {
    "Prahaar confirmation": none("Prahaar monitoring active · awaiting confirmation", !!a.prahaar),
    "GTF zone rejection": none("GTF 2.0 monitoring active · awaiting zone interaction", !!a.gtf),
    "Futures OI build-up": none("Futures OI monitoring active · awaiting delta", true),
  };
  if (c.closed === false) return result;
  const settings = BIAS_POLICY.additional_factors;
  const day = (time: number) => new Date(time * 1000).toISOString().slice(0, 10);
  const byTime = new Map(closed.map((bar) => [bar.time, bar]));

  // 21. Prahaar Confirmation
  if (a.prahaar) {
    const signals = (a.prahaar.signals ?? [])
      .map((signal) => ({ signal, time: signal.chart_time ?? signal.time }))
      .filter(
        ({ signal, time }) =>
          byTime.has(time) &&
          day(time) === day(c.time) &&
          time <= c.time &&
          time >= c.time - (settings.prahaar_window_bars - 1) * barSec &&
          signal.vix_ok !== false
      )
      .filter(
        ({ signal, time }) =>
          Number.isFinite(signal.sl) &&
          !closed.some(
            (bar) => bar.time > time && bar.time <= c.time && (signal.dir === "bull" ? bar.low <= signal.sl : bar.high >= signal.sl)
          )
      )
      .sort((x, y) => y.time - x.time);

    if (signals.length) {
      const latest = signals.filter((item) => item.time === signals[0].time);
      const directions = new Set(latest.map((item) => item.signal.dir));
      result["Prahaar confirmation"] =
        directions.size === 1
          ? {
              vote: signals[0].signal.dir,
              why: `Closed Prahaar ${signals[0].signal.dir} confirmation; EMA/MACD/ADX treated as one factor`,
              available: true,
            }
          : none("Conflicting Prahaar confirmations on the same candle", true);
    } else if (a.prahaar.latest_state) {
      const state = a.prahaar.latest_state;
      const verdict = (state.current_verdict || "").toUpperCase();
      const ema = (state.ema_trend || "").toLowerCase();
      const macd = (state.macd_momentum || "").toLowerCase();
      let dir: Vote = null;
      if (verdict.includes("CALL") || (ema === "bullish" && macd === "bullish")) {
        dir = "bull";
      } else if (verdict.includes("PUT") || (ema === "bearish" && macd === "bearish")) {
        dir = "bear";
      }
      result["Prahaar confirmation"] = {
        vote: dir,
        why: `Prahaar: ${state.current_verdict || (dir ? dir.toUpperCase() : "WAIT/NEUTRAL")} · EMA ${state.ema_trend ?? "flat"}, momentum ${state.macd_momentum ?? "flat"}`,
        available: true,
      };
    } else {
      result["Prahaar confirmation"] = {
        vote: null,
        why: "Prahaar engine active · no directional confirmation in window",
        available: true,
      };
    }
  }

  // 22. GTF Zone Rejection
  if (a.gtf) {
    const reactions: { dir: Vote; id: string }[] = [];
    let knownZones = 0;
    for (const zone of a.gtf.zones ?? []) {
      if (!Number.isFinite(zone.from_raw) || !Number.isFinite(zone.explosive_time)) continue;
      const formed = zone.explosive_time + (zone.from - zone.from_raw!);
      if (
        !byTime.has(formed) ||
        (settings.gtf_same_session && day(formed) !== day(c.time)) ||
        formed >= c.time ||
        zone.explosive_body_ratio < settings.gtf_min_explosive_ratio ||
        zone.base_bars > settings.gtf_max_base_bars
      )
        continue;
      const path = closed.filter((bar) => bar.time >= formed && bar.time <= c.time);
      if (path.some((bar, index) => index > 0 && bar.time - path[index - 1].time !== barSec)) continue;
      knownZones++;
      const since = path.filter((bar) => bar.time > formed && bar.time < c.time);
      const invalid = since.some((bar) => (zone.dir === "bull" ? bar.close < zone.bottom : bar.close > zone.top));
      const touches = since.filter((bar) => bar.high >= zone.bottom && bar.low <= zone.top).length;
      if (invalid || touches > settings.gtf_max_prior_touch_bars) continue;
      const touching = c.high >= zone.bottom && c.low <= zone.top;
      const rejection =
        zone.dir === "bull" ? c.close > zone.top && c.close > c.open : c.close < zone.bottom && c.close < c.open;
      if (touching && rejection) reactions.push({ dir: zone.dir, id: zone.id });
    }

    if (reactions.length) {
      const directions = new Set(reactions.map((r) => r.dir));
      result["GTF zone rejection"] =
        directions.size === 1
          ? {
              vote: reactions[0].dir,
              why: `First closed-candle rejection of formed GTF ${reactions[0].dir === "bull" ? "demand" : "supply"}; ${reactions.length} zones count once`,
              available: true,
            }
          : none("Conflicting demand and supply reactions; no vote", true);
    } else {
      const dem = a.gtf.nearest_demand;
      const sup = a.gtf.nearest_supply;
      const atr = a.atr ?? 20;
      let gtfV: Vote = null;
      let gtfW = "";
      if (dem && c.low <= dem.top + atr * 0.5 && c.close >= dem.bottom - atr * 0.2) {
        gtfV = "bull";
        gtfW = `GTF 2.0 Demand [${dem.bottom.toFixed(0)}–${dem.top.toFixed(0)}] (${dem.status || "FRESH"}) holding support`;
      } else if (sup && c.high >= sup.bottom - atr * 0.5 && c.close <= sup.top + atr * 0.2) {
        gtfV = "bear";
        gtfW = `GTF 2.0 Supply [${sup.bottom.toFixed(0)}–${sup.top.toFixed(0)}] (${sup.status || "FRESH"}) active resistance`;
      } else if (dem || sup) {
        gtfW = `GTF 2.0: Demand ${dem ? `${dem.bottom.toFixed(0)}–${dem.top.toFixed(0)} (${dem.status || "FRESH"})` : "none"} · Supply ${sup ? `${sup.bottom.toFixed(0)}–${sup.top.toFixed(0)} (${sup.status || "FRESH"})` : "none"}`;
      } else {
        gtfW = "GTF 2.0 zones monitored (price between zones)";
      }
      result["GTF zone rejection"] = { vote: gtfV, why: gtfW, available: true };
    }
  }

  // 23. Futures OI Build-Up
  const oi = (a.volume?.oi_line ?? []).slice().reverse().find((point) => point.time <= c.time && point.time >= c.time - barSec);
  const deltaOi = oi?.doi ?? pcrPt?.fut_oi_chg ?? (a.volume?.oi_last?.doi != null ? a.volume.oi_last.doi : null);
  const contract = oi?.contract || a.volume?.oi_health?.contract || (pcrPt ? "61471" : "FUT");
  const prev = closed[closed.indexOf(c) - 1];
  const priceDelta = (c.close !== c.open) ? (c.close - c.open) : (prev ? c.close - prev.close : 0);

  if (deltaOi != null && Number.isFinite(deltaOi)) {
    let dir: Vote = null;
    let regime = "flat";
    if (priceDelta > 0.5 && deltaOi > 100) {
      dir = "bull"; regime = "Long Buildup";
    } else if (priceDelta > 0.5 && deltaOi < -100) {
      dir = "bull"; regime = "Short Covering";
    } else if (priceDelta < -0.5 && deltaOi > 100) {
      dir = "bear"; regime = "Short Buildup";
    } else if (priceDelta < -0.5 && deltaOi < -100) {
      dir = "bear"; regime = "Long Unwinding";
    } else if (Math.abs(priceDelta) <= 0.5 && Math.abs(deltaOi) >= 500) {
      regime = deltaOi > 0 ? "OI Buildup (Neutral)" : "OI Unwinding (Neutral)";
    }
    result["Futures OI build-up"] = {
      vote: dir,
      available: true,
      why: `${regime}: Price ${priceDelta >= 0 ? "+" : ""}${priceDelta.toFixed(1)} · contract ${contract} · ΔOI ${deltaOi >= 0 ? "+" : ""}${deltaOi}`,
    };
  } else if (a.volume?.oi_last) {
    const last = a.volume.oi_last;
    const build = (last.build || "").toLowerCase();
    let dir: Vote = null;
    if (build.includes("long build") || build.includes("short cover")) dir = "bull";
    else if (build.includes("short build") || build.includes("unwind")) dir = "bear";
    result["Futures OI build-up"] = {
      vote: dir,
      available: true,
      why: `Futures OI ${last.build || "steady"} · contract ${contract} · ΔOI ${last.doi ?? "—"}`,
    };
  } else if (pcrPt && pcrPt.fut_oi != null) {
    result["Futures OI build-up"] = {
      vote: null,
      available: true,
      why: `Futures OI ${pcrPt.fut_oi.toLocaleString()} (steady) · contract ${contract}`,
    };
  } else {
    result["Futures OI build-up"] = {
      vote: null,
      available: true,
      why: "Futures OI baseline · contract 61471",
    };
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

export type CvdAlertSignal={time:number;sourceTime:number;kind:"exhaustion"|"confirmed";dir:"bull"|"bear";details:string;zone?:string};
// Adapter only: consumes the existing CVD detector and existing node entries unchanged.
export function cvdExhaustionSignals(candles:Candle[],fp:FpBar[],events:CvdDivergence[],nodes:NEntry[],barSec:number):CvdAlertSignal[]{
  const cfg=BIAS_POLICY.cvd_alerts;
  const closed=candles.filter((c)=>c.closed!==false).slice().sort((a,b)=>a.time-b.time);
  const bars=new Map(fp.map((p)=>[p.time,p]));
  const byTime=new Map(closed.map((c)=>[c.time,c]));
  const day=(t:number)=>new Date(t*1000).toISOString().slice(0,10);
  const complete=(t:number)=>Array.from({length:cfg.complete_bars},(_,i)=>t-i*barSec).every((at)=>{
    const b=bars.get(at);
    return byTime.has(at) && day(at)===day(t) && !!b?.rows.length && Number.isFinite(b.cvd) && Number.isFinite(b.delta);
  });
  const raw=events.filter((e)=>e.type==="EXHAUSTION_CLIMAX" && byTime.has(e.time) && complete(e.time)).map((e):CvdAlertSignal=>({
    time:e.time,sourceTime:e.time,kind:"exhaustion",dir:e.dir,
    details:(e.dir==="bull"?"Seller exhaustion — bullish warning":"Buyer exhaustion — bearish warning")+" · estimated CVD · "+e.details,
  }));
  const out:CvdAlertSignal[]=[...raw],used=new Set<number>();
  let lastConfirmation:CvdAlertSignal|undefined;
  for(const candle of closed){
    if(!complete(candle.time))continue;
    const candidates=raw.filter((e)=>e.time<candle.time && day(e.time)===day(candle.time) && candle.time-e.time<=cfg.confirmation_window_bars*barSec && !used.has(e.time)).reverse();
    for(const event of candidates){
      const source=byTime.get(event.time)!;
      const path=closed.filter((c)=>c.time>source.time && c.time<=candle.time);
      if(path.some((c)=>event.dir==="bull"?c.low<source.low:c.high>source.high))continue;
      const breaks=event.dir==="bull"?candle.close>source.high:candle.close<source.low;
      if(!breaks)continue;
      const node=nodes.find((n)=>day(n.time)===day(event.time) && n.time<=candle.time && n.time>=event.time-(cfg.node_window_bars-1)*barSec && n.side===(event.dir==="bull"?"BUY":"SELL") && source.low<=n.zone.hi && source.high>=n.zone.lo);
      if(!node)continue;
      if(lastConfirmation?.dir===event.dir && candle.time-lastConfirmation.time<cfg.confirmation_cooldown_bars*barSec)continue;
      const signal:CvdAlertSignal={time:candle.time,sourceTime:event.time,kind:"confirmed",dir:event.dir,zone:node.zone.label,
        details:(event.dir==="bull"?"Bullish":"Bearish")+" CVD + stacked-node confirmation · "+node.zone.label+" rejection + close beyond exhaustion candle "+(event.dir==="bull"?"high":"low")+"; correlated evidence, not a guaranteed entry"};
      out.push(signal);used.add(event.time);lastConfirmation=signal;break;
    }
  }
  return out.sort((a,b)=>a.time-b.time);
}
