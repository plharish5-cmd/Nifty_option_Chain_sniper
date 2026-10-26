// signalAlerts.tsx — chart SIGNAL alerts (information only — never places orders).
// Uses the same sound · voice · desktop · toast pipeline as the RVOL alerts (useRvolAlerts().fire).
// Fires ONCE per signal, only on the latest CLOSED candle, only while the market is open.
//   ⚖ BIAS ≥ N of 19     — N agreeing factors (choose 10 / 11 / 12 / 13 / 14 / 15 / 16)
//   ✓3 CONFLUENCE        — footprint pill + delta colour + structure mark agree
//   🎯 PE−CE action       — 8501 Action switches to CE or PE
//   SMT                   — NIFTY vs BANKNIFTY divergence
//   IFVG buy / sell · RJB buy / sell · CRT entry (Setup D) · D.O.L target reached
//   ★ A+                  — Bias ≥ {N} AND (✓3 or IFVG or RJB or CRT entry) on the SAME side, same candle
//   🚨 DEEP REVERSAL      — 3-factor extreme reversal: Deep Sweep/HL + Writer Impulse Flip + MSS Break
//   🛡️ TRAIL SL          — Trade management trailing stop shift >= 15 pts
import { useEffect, useState } from "react";
import { api, type AlertRow, type Analysis, type Candle } from "../api";
import type { BiasRow } from "./bias";
import type { FpBar } from "./footprint";
import type { SmtMark } from "./ocfeed";
import type { Dol } from "./dol";
import { type HtfWick, isWickUntested, markWickTested } from "./htfWicks";

export type SigKind = "bias" | "conf" | "ocAct" | "smt" | "ifvg" | "rjb" | "crt" | "dol" | "gamma" | "nodes" | "flow" | "abs" | "wick" | "aplus" | "deep_rev" | "trade_mgmt_trail";
export type SigPrefs = { on: boolean; minBias: number; kinds: Record<SigKind, boolean> };
const KEY = "ict_signal_alert_prefs";
const DEF: SigPrefs = {
  on: false,
  minBias: 12,
  kinds: {
    bias: true,
    conf: true,
    ocAct: true,
    smt: true,
    ifvg: true,
    rjb: true,
    crt: true,
    dol: true,
    gamma: true,
    nodes: true,
    flow: true,
    abs: true,
    wick: true,
    aplus: true,
    deep_rev: true,
    trade_mgmt_trail: true,
  },
};

export function useSigPrefs() {
  const [p, setP] = useState<SigPrefs>(() => {
    try { const s = JSON.parse(localStorage.getItem(KEY) || "{}"); return { ...DEF, ...s, kinds: { ...DEF.kinds, ...(s.kinds ?? {}) } }; }
    catch { return DEF; }
  });
  useEffect(() => { try { localStorage.setItem(KEY, JSON.stringify(p)); } catch { /* private mode */ } }, [p]);
  return [p, setP] as const;
}

// remembered across reloads for the day, so a refresh doesn't repeat the same alert
const SEEN_KEY = "ict_signal_seen";
const seen: Set<string> = (() => { try { return new Set<string>(JSON.parse(sessionStorage.getItem(SEEN_KEY) || "[]")); } catch { return new Set<string>(); } })();
const remember = (k: string) => { seen.add(k); try { sessionStorage.setItem(SEEN_KEY, JSON.stringify([...seen].slice(-500))); } catch { /* ignore */ } };

export type Ev = {
  key: string;
  kind: SigKind;
  side: "BUY" | "SELL" | "INFO" | "HOLD";
  title: string;
  body: string;
  price: number;
  t: number;
  grade?: "trade" | "info" | "mgmt";
};

export function scanSignals(o: {
  tf: string;
  candles: Candle[];
  a: Analysis | null;
  prefs: SigPrefs;
  bias: BiasRow[];
  conf: FpBar[];
  ocActs: { time: number; v: number; txt: string; kind?: string; smartRow?: any; stage?: number }[];
  smt: SmtMark[] | null;
  dols: Dol[];
  gamma?: { time: number; side: "BUY" | "SELL" }[];
  nodes?: { time: number; side: "BUY" | "SELL"; zone: { label: string; lo: number; hi: number }; z: number }[];
  flow?: { time: number; verdict: "BUY" | "SELL" | "WAIT"; prev: "BUY" | "SELL" | "WAIT" | null; bull: number; bear: number; head: string } | null;
  htfWicks?: HtfWick[];
}): Ev[] {
  const { tf, candles, a, prefs, bias, conf, ocActs, smt, dols } = o;
  const closed = candles.filter((c) => c.closed !== false);
  const last = closed[closed.length - 1];
  if (!last) return [];
  const t = last.time, px = last.close, K = prefs.kinds;

  // Freshness Guard: Do not re-fire historical candles older than 20 mins on browser wakeup/reload
  const nowSec = Date.now() / 1000;
  if (Math.abs(nowSec - t) > 1200) return [];

  const hm = (() => { const d = new Date(t * 1000); return `${String(d.getUTCHours()).padStart(2, "0")}:${String(d.getUTCMinutes()).padStart(2, "0")}`; })();
  const side = (dir: string) => (dir === "bull" ? "BUY" : "SELL") as "BUY" | "SELL";
  const leg = (s: string) => (s === "BUY" ? "CE side" : "PE side");

  // Check 15:15–15:30 EOD settlement window
  const istDate = new Date((t + 19800) * 1000);
  const istH = istDate.getUTCHours();
  const istM = istDate.getUTCMinutes();
  const isEodSettlement = (istH === 15 && istM >= 15) || istH > 15;

  const candleRange = last.high - last.low;
  const isGiantCandle = candleRange >= 65; // On 5m NIFTY, > 65 pts is > 1.8x ATR(14)

  const out: Ev[] = [];
  const add = (kind: SigKind, s: Ev["side"], title: string, body: string, grade?: "trade" | "info" | "mgmt") => {
    let finalTitle = `${tf} ${hm} · ${title}`;
    if (isEodSettlement) {
      finalTitle += " [EOD SETTLEMENT / NEXT-DAY BIAS ONLY — DO NOT OPEN INTRADAY OPTIONS]";
    }
    out.push({
      key: `${tf}|${t}|${kind}|${s}`,
      kind,
      side: s,
      title: finalTitle,
      body,
      price: px,
      t,
      grade,
    });
  };

  // ⚖ bias
  const b = bias.find((r) => r.time === t && !r.forming);
  let biasSide: "BUY" | "SELL" | null = null;
  if (b && b.verdict !== "MIXED") {
    const n = b.verdict === "BUY" ? b.bull : b.bear;
    if (n >= prefs.minBias) {
      biasSide = b.verdict;
      if (K.bias) add("bias", b.verdict, `${b.verdict} bias ${n} of ${b.avail}`,
        `${n} factors agree → ${leg(b.verdict)}${b.warn ? ` · ⚠ ${b.warn}` : ""}. Direction filter only — enter on your setup with a fixed SL.`);
    }
  }

  // ✓3 confluence
  const cf = conf.find((x) => x.time === t && x.conf);
  const confSide = cf?.conf ? side(cf.conf.dir) : null;
  if (cf?.conf && K.conf) add("conf", side(cf.conf.dir), `${side(cf.conf.dir)} ✓3 confluence`, `${cf.conf.why.join(" + ")} → ${leg(side(cf.conf.dir))}.`);

  // IFVG / RJB / CRT entry
  let ifvgSide: "BUY" | "SELL" | null = null, crtSide: "BUY" | "SELL" | null = null;
  for (const m of a?.markers ?? []) if (m.time === t && (m.kind === "IFVG_BUY" || m.kind === "IFVG_SELL")) {
    ifvgSide = m.kind === "IFVG_BUY" ? "BUY" : "SELL";
    if (K.ifvg) add("ifvg", ifvgSide, `IFVG ${ifvgSide === "BUY" ? "buy ▲" : "sell ▼"}`, ifvgSide === "BUY" ? "BUY CE · SL below the zone." : "SELL / PE · SL above the zone.");
  }

  // 🛡 Institutional Absorption & Aggression (Delta layer)
  let absSide: "BUY" | "SELL" | null = null;
  for (const m of a?.markers ?? []) {
    if (m.time === t) {
      if (m.kind === "DELTA_ABSORPTION") {
        absSide = side(m.dir);
        if (K.abs) add("abs", absSide, `Absorption ${absSide === "BUY" ? "buy ▲" : "sell ▼"}`,
          `${absSide === "BUY" ? "Aggressive sellers absorbed by limit buyers at support" : "Aggressive buyers absorbed by limit sellers at resistance"}${m.price ? ` @ ${m.price.toFixed(1)}` : ""} → ${leg(absSide)} defense.`);
      } else if (m.kind === "DELTA_AGGRESSION" && !absSide) {
        const aggSide = side(m.dir);
        let note = `Extreme market ${aggSide === "BUY" ? "buying (lifting offer)" : "selling (hitting bid)"}${m.price ? ` @ ${m.price.toFixed(1)}` : ""} → momentum toward ${leg(aggSide)}.`;
        if (isGiantCandle && ((aggSide === "SELL" && last.close <= last.low + 0.3 * candleRange) || (aggSide === "BUY" && last.close >= last.high - 0.3 * candleRange))) {
          note += ` ⚡ Extended ${candleRange.toFixed(0)}pt bar! Exhaustion guard active: DO NOT CHASE at extreme close; wait for pullback/retest.`;
        }
        if (K.abs) add("abs", aggSide, `Aggression ${aggSide === "BUY" ? "buy Δ+" : "sell Δ−"}`, note);
      }
    }
  }

  // ⟂ RJB+ (Clubbed Rejection Block: RJB + ABS and/or RJB + Bias)
  let rjbCandidate: { side: "BUY" | "SELL"; top: number; bottom: number; mid: number; formed: string } | null = null;
  if (a?.zones) {
    for (const z of a.zones) {
      if (z.type !== "RJB" || z.state === "invalid") continue;
      if (z.from > t) continue;
      const mid = (z.top + z.bottom) / 2;
      if (z.dir === "bull" && last.low <= z.top && last.close >= z.bottom && last.close >= mid) {
        rjbCandidate = { side: "BUY", top: z.top, bottom: z.bottom, mid, formed: z.formed };
        break;
      } else if (z.dir === "bear" && last.high >= z.bottom && last.close <= z.top && last.close <= mid) {
        rjbCandidate = { side: "SELL", top: z.top, bottom: z.bottom, mid, formed: z.formed };
        break;
      }
    }
  }

  let rjbSide: "BUY" | "SELL" | null = null;
  if (rjbCandidate) {
    const candSide = rjbCandidate.side;
    const hasAbs = absSide === candSide;
    const hasBias = biasSide === candSide;

    if (hasAbs && hasBias) {
      rjbSide = candSide;
      if (K.rjb) {
        add("rjb", candSide, `RJB + ABS + Bias ${candSide === "BUY" ? "▲ Buy Support" : "▼ Sell Resistance"} ${rjbCandidate.mid.toFixed(0)}`,
          `Institutional limit ${candSide === "BUY" ? "buyers absorbed aggressive selling" : "sellers absorbed aggressive buying"} at RJB 50% midpoint (${rjbCandidate.mid.toFixed(1)}) + Bias ${candSide === "BUY" ? b?.bull : b?.bear}/${b?.avail ?? 19} ${candSide} agrees → ${leg(candSide)}. SL ${candSide === "BUY" ? `below ${rjbCandidate.bottom.toFixed(0)}` : `above ${rjbCandidate.top.toFixed(0)}`}.`);
      }
    } else if (hasAbs) {
      rjbSide = candSide;
      if (K.rjb) {
        add("rjb", candSide, `RJB + ABS ${candSide === "BUY" ? "▲ Buy Support" : "▼ Sell Resistance"} ${rjbCandidate.mid.toFixed(0)}`,
          `Institutional limit ${candSide === "BUY" ? "buyers absorbed aggressive selling" : "sellers absorbed aggressive buying"} at RJB 50% midpoint (${rjbCandidate.mid.toFixed(1)}) → ${leg(candSide)}. SL ${candSide === "BUY" ? `below ${rjbCandidate.bottom.toFixed(0)}` : `above ${rjbCandidate.top.toFixed(0)}`}.`);
      }
    } else if (hasBias) {
      rjbSide = candSide;
      if (K.rjb) {
        add("rjb", candSide, `RJB + Bias ${candSide === "BUY" ? "▲ Buy Support" : "▼ Sell Resistance"} ${rjbCandidate.mid.toFixed(0)}`,
          `RJB bounce confirmed by Bias ${candSide === "BUY" ? b?.bull : b?.bear}/${b?.avail ?? 19} ${candSide} at 50% midpoint (${rjbCandidate.mid.toFixed(1)}) → ${leg(candSide)}. SL ${candSide === "BUY" ? `below ${rjbCandidate.bottom.toFixed(0)}` : `above ${rjbCandidate.top.toFixed(0)}`}.`);
      }
    }
  }

  // 🕯 Higher Timeframe (HTF) Wicks — First-Time Touch only (Hourly 1×, Daily 2×, Weekly 3×)
  if (K.wick && o.htfWicks && o.htfWicks.length) {
    for (const w of o.htfWicks) {
      if (last.low <= w.top && last.high >= w.bottom) {
        if (isWickUntested(w.id)) {
          markWickTested(w.id);
          add("wick", w.side,
            `${w.tf} ${w.tfName} Wick ${w.stars} (${w.weight}×) First Test ${w.side === "BUY" ? "Support" : "Resistance"} ${w.bottom.toFixed(0)}–${w.top.toFixed(0)}`,
            `Price touched virgin ${w.tfName} (${w.tf}) ${w.side === "BUY" ? "lower" : "upper"} wick (50% eq ${w.mid.toFixed(1)}, size ${w.wickPts.toFixed(0)} pts) for the FIRST time. Institutional rejection level. Alerts once only.`);
        }
      }
    }
  }

  for (const m of a?.crt?.markers ?? []) if (m.time === t && m.kind === "CRT_ENTRY") {
    crtSide = side(m.dir);
    if (K.crt) add("crt", crtSide, `CRT entry ${crtSide}`, "Setup D — toward the other side of the CRT box; TP1 50 %, SL beyond the sweep wick.");
  }

  // 🎯 PE−CE Action Switch & 🛡️ Active Trade Management (Trailing SL >= 15 pts)
  if (K.ocAct && ocActs.length >= 1) {
    const curr = ocActs.find((x) => x.time === t);
    const prev = ocActs.filter((x) => x.time < t).pop();

    if (curr) {
      if (curr.kind === "HOLD" || curr.stage === 4) {
        // Trade Management Trailing Stop
        const currSl = curr.smartRow?.dynamicSl;
        const isBear = curr.smartRow?.actionTone === "bear" || curr.txt.includes("PE");
        const trailKey = `ict_trail_sl_${isBear ? "PE" : "CE"}`;
        const lastSlStr = sessionStorage.getItem(trailKey);
        const lastSl = lastSlStr ? parseFloat(lastSlStr) : null;

        let shouldEmitTrail = false;
        if (currSl != null && !isNaN(currSl)) {
          if (lastSl == null) {
            shouldEmitTrail = true;
          } else if (isBear && (lastSl - currSl >= 15)) {
            // For PE: SL stepping down >= 15 pts locks in profit
            shouldEmitTrail = true;
          } else if (!isBear && (currSl - lastSl >= 15)) {
            // For CE: SL stepping up >= 15 pts locks in profit
            shouldEmitTrail = true;
          }
        }

        if (shouldEmitTrail && currSl != null) {
          sessionStorage.setItem(trailKey, String(currSl));
          add("trade_mgmt_trail", "HOLD",
            `🛡️ ${isBear ? "HOLD PE" : "HOLD CE"} · TRAIL SL → ${currSl}`,
            curr.txt,
            "mgmt"
          );
        }
      } else if (curr.kind === "PULLBACK" || curr.kind === "ENTRY" || curr.kind === "ADD") {
        if (!prev || prev.txt !== curr.txt || prev.v !== curr.v) {
          if (curr.v !== 0) {
            const s = curr.v > 0 ? "BUY" : "SELL";
            add("ocAct", s, `PE−CE action → ${s === "BUY" ? "CE" : "PE"}`, curr.txt.slice(0, 160));
          }
        }
      }
    }
  }

  // SMT
  for (const m of smt ?? []) if (m.time === t && K.smt) add("smt", side(m.dir), `SMT ${m.dir === "bull" ? "▲" : "▼"}`, m.text);

  // D.O.L reached
  for (const d of dols) if (d.hit === t && K.dol) add("dol", "INFO", `D.O.L reached ${d.lo.toFixed(0)}`, `${d.label} reached — book profit / trail SL.`);

  // γ ✓✓✓ (recorded live by the gamma layer)
  const gm = (o.gamma ?? []).find((x) => x.time === t);
  const gSide = gm ? gm.side : null;
  if (gm && K.gamma) add("gamma", gm.side, `γ ✓✓✓ ${gm.side}`, `EMA stack + flag at a gamma level + volume ≥ 1.5× → ${leg(gm.side)}. SL at the flag edge, target the next gamma level.`);

  // ● ENTRY — overextension (VWAP ±2σ) into a stacked D/W/M zone
  const ne = (o.nodes ?? []).find((x) => x.time === t);
  const nSide = ne ? ne.side : null;
  if (ne && K.nodes) add("nodes", ne.side, `● ENTRY ${ne.side} at ${ne.zone.label}`, `${ne.z.toFixed(1)}σ stretch into zone ${ne.zone.lo.toFixed(0)}–${ne.zone.hi.toFixed(0)} and closed back → ${leg(ne.side)} reversal. SL beyond the zone, first target VWAP.`);

  // 🧭 order-flow verdict — only when it FLIPS to BUY / SELL
  const f = o.flow && o.flow.time === t ? o.flow : null;
  const fSide = f && f.verdict !== "WAIT" ? f.verdict : null;
  if (f && fSide && f.prev !== f.verdict && K.flow) add("flow", fSide, `Order flow → ${fSide} (${Math.max(f.bull, f.bear)} vs ${Math.min(f.bull, f.bear)})`, f.head.slice(0, 160));

  // 🚨 A+ DEEP-LEVEL LIQUIDITY SWEEP & V-REVERSAL ENGINE
  let deepRevSide: "BUY" | "SELL" | null = null;
  if (K.deep_rev && closed.length >= 10) {
    let sessLow = Infinity;
    let sessHigh = -Infinity;
    let sessLowIdx = -1;
    let sessHighIdx = -1;
    for (let k = 0; k < closed.length; k++) {
      if (closed[k].low < sessLow) {
        sessLow = closed[k].low;
        sessLowIdx = k;
      }
      if (closed[k].high > sessHigh) {
        sessHigh = closed[k].high;
        sessHighIdx = k;
      }
    }

    const sessRange = sessHigh - sessLow;
    const barsSinceLow = closed.length - 1 - sessLowIdx;
    const barsSinceHigh = closed.length - 1 - sessHighIdx;

    // Bullish Deep Reversal:
    if (sessRange >= 100 && barsSinceLow <= 15 && barsSinceLow > 0) {
      const sweepBar = closed[sessLowIdx];
      const hasRejection = sweepBar.close >= sweepBar.low + 0.3 * (sweepBar.high - sweepBar.low);
      const isHigherLow = last.low >= sessLow && last.low <= sessLow + 30;

      if (hasRejection || isHigherLow) {
        const currOc = ocActs.find((x) => x.time === t);
        const ocShockBull = (currOc?.smartRow?.pe_ce_delta_l ?? 0) >= 5 || (currOc?.smartRow?.put_delta_l ?? 0) > (currOc?.smartRow?.call_delta_l ?? 0);
        const writerFlip = ocShockBull || absSide === "BUY" || (b && b.verdict === "BUY");

        const mssBull = (a?.markers ?? []).some((m) => m.time === t && (m.kind === "IFVG_BUY" || m.kind.includes("MSS") || m.kind.includes("CHOCH") || m.kind.includes("BOS")) && m.dir === "bull");

        if (writerFlip && mssBull) {
          deepRevSide = "BUY";
          const atmStrike = Math.round(px / 50) * 50;
          const sl = Math.round(sessLow - 12);
          const tgt = Math.round(sessLow + sessRange * 0.5);
          add("deep_rev", "BUY", `🚨 A+ DEEP REVERSAL SNIPER: BUY ${atmStrike} CE`,
            `1) Deep session low swept @ ${sessLow.toFixed(1)} with rejection/HL; 2) Option writers flipped to Put writing / Call covering; 3) Bullish MSS break @ ${px.toFixed(1)}. Entry ${px.toFixed(1)} · SL ${sl} (below ${sessLow.toFixed(0)}) · Tgt ${tgt} (50% Fib / VWAP).`,
            "trade"
          );
        }
      }
    }

    // Bearish Deep Reversal:
    if (sessRange >= 100 && barsSinceHigh <= 15 && barsSinceHigh > 0 && !deepRevSide) {
      const sweepBar = closed[sessHighIdx];
      const hasRejection = sweepBar.close <= sweepBar.high - 0.3 * (sweepBar.high - sweepBar.low);
      const isLowerHigh = last.high <= sessHigh && last.high >= sessHigh - 30;

      if (hasRejection || isLowerHigh) {
        const currOc = ocActs.find((x) => x.time === t);
        const ocShockBear = (currOc?.smartRow?.pe_ce_delta_l ?? 0) <= -5 || (currOc?.smartRow?.call_delta_l ?? 0) > (currOc?.smartRow?.put_delta_l ?? 0);
        const writerFlip = ocShockBear || absSide === "SELL" || (b && b.verdict === "SELL");

        const mssBear = (a?.markers ?? []).some((m) => m.time === t && (m.kind === "IFVG_SELL" || m.kind.includes("MSS") || m.kind.includes("CHOCH") || m.kind.includes("BOS")) && m.dir === "bear");

        if (writerFlip && mssBear) {
          deepRevSide = "SELL";
          const atmStrike = Math.round(px / 50) * 50;
          const sl = Math.round(sessHigh + 12);
          const tgt = Math.round(sessHigh - sessRange * 0.5);
          add("deep_rev", "SELL", `🚨 A+ DEEP REVERSAL SNIPER: BUY ${atmStrike} PE`,
            `1) Deep session high swept @ ${sessHigh.toFixed(1)} with rejection/LH; 2) Option writers flipped to Call writing / Put covering; 3) Bearish MSS break @ ${px.toFixed(1)}. Entry ${px.toFixed(1)} · SL ${sl} (above ${sessHigh.toFixed(0)}) · Tgt ${tgt} (50% Fib / VWAP).`,
            "trade"
          );
        }
      }
    }
  }

  // ★ A+ : bias + triggers on the same side — weight = how many independent triggers agree
  const trigs = biasSide ? ([[confSide, "✓3"], [ifvgSide, "IFVG"], [rjbSide, "RJB+"], [crtSide, "CRT entry"], [absSide, "ABS"], [gSide, "γ ✓✓✓"], [nSide, "● ENTRY"], [fSide, "order flow"], [deepRevSide, "DEEP REV"]] as const)
    .filter(([sd]) => sd === biasSide).map(([, n]) => n) : [];
  if (K.aplus && biasSide && trigs.length) {
    const w = trigs.length;
    add("aplus", biasSide, `★ A+${w > 1 ? ` ×${w}` : ""} ${biasSide} — bias ≥ ${prefs.minBias} + ${trigs.join(" + ")}`,
      `${w >= 3 ? "Very rare — three independent reads agree. " : w === 2 ? "Two independent triggers agree. " : ""}Still: closed candle only, fixed SL, 1 lot, Kill Switch rules. Paper trade first.`,
      "trade"
    );
  }

  // ── PER-CANDLE CONFLUENCE ARBITER (ELIMINATE SAME-CANDLE CONTRADICTORY ALERTS) ──
  const buys = out.filter((e) => e.side === "BUY");
  const sells = out.filter((e) => e.side === "SELL");

  if (buys.length > 0 && sells.length > 0) {
    const currOc = ocActs.find((x) => x.time === t);
    const dayPeCeCr = currOc?.smartRow?.pe_ce_day_cr ?? 0;
    const isOcBear = dayPeCeCr < -0.3 || (currOc?.smartRow?.actionTone === "bear");
    const isOcBull = dayPeCeCr > 0.3 || (currOc?.smartRow?.actionTone === "bull");

    if (isOcBear) {
      // Bearish writer flow: suppress conflicting BUY entries
      return out.filter((e) => e.side !== "BUY");
    } else if (isOcBull) {
      // Bullish writer flow: suppress conflicting SELL entries
      return out.filter((e) => e.side !== "SELL");
    } else {
      // Neutral writer flow: favor higher conviction side
      const hasAplusBuy = buys.some((e) => e.kind === "aplus" || e.kind === "deep_rev");
      const hasAplusSell = sells.some((e) => e.kind === "aplus" || e.kind === "deep_rev");
      if (hasAplusBuy && !hasAplusSell) return out.filter((e) => e.side !== "SELL");
      if (hasAplusSell && !hasAplusBuy) return out.filter((e) => e.side !== "BUY");
      if (b && b.bull > b.bear) return out.filter((e) => e.side !== "SELL");
      if (b && b.bear > b.bull) return out.filter((e) => e.side !== "BUY");
    }
  }

  return out;
}

// fire the new ones through the shared RVOL pipeline (kind prefixed SIG_ so sound / voice differ)
export function dispatch(evs: Ev[], fire: (a: AlertRow) => void, tf: string) {
  const fresh = evs.filter((e) => !seen.has(e.key));
  // if an A+ or deep_rev fired, skip its separate bias line to avoid double speech
  const hasA = fresh.some((e) => e.kind === "aplus" || e.kind === "deep_rev");
  for (const e of fresh) {
    remember(e.key);
    if (hasA && e.kind === "bias") continue;
    const kindStr = e.kind === "trade_mgmt_trail" ? "SIG_TRADE_MGMT_TRAIL" : `SIG_${e.kind.toUpperCase()}_${e.side}`;
    const gradeStr = e.grade ?? (e.kind === "aplus" || e.kind === "deep_rev" ? "trade" : e.kind === "trade_mgmt_trail" ? "mgmt" : "info");
    const al: AlertRow = {
      id: Date.now() + Math.floor(Math.random() * 1000),
      ts: e.t,
      time: "",
      grade: gradeStr,
      kind: kindStr,
      side: e.side,
      tf,
      title: e.title,
      body: e.body,
      price: e.price,
    };
    fire(al);
    api.postAlert({ ...al, key: e.key }).catch(() => {});
  }
}

export const SIG_SOUND: Record<string, [number, number][]> = {
  BUY: [[660, 0.12], [880, 0.12], [1175, 0.25]],
  SELL: [[1175, 0.12], [880, 0.12], [660, 0.25]],
  HOLD: [[550, 0.15]],
  INFO: [[990, 0.2]],
  APLUS: [[880, 0.1], [1320, 0.1], [880, 0.1], [1320, 0.3]],
  DEEP_REV: [[784, 0.1], [1046, 0.1], [1318, 0.1], [1568, 0.35]],
};

const HELP: Record<SigKind, { when: string; say: string }> = {
  bias: { when: "{N} or more of the 19 factors agree on the closed candle (N = the bias ≥ selector).", say: "“BUY bias 12 of 19” — direction filter only." },
  conf: { when: "Footprint pill, delta colour and a structure mark agree on one candle.", say: "“SELL check 3 confluence”" },
  ocAct: { when: "The 8501 PE−CE 🎯 Action switches to CE or PE.", say: "“PE−CE action → CE”" },
  smt: { when: "NIFTY and BANKNIFTY diverge at a high or low (one sweeps, the other doesn't).", say: "“SMT ▼” — weak sweep, expect a turn." },
  ifvg: { when: "IFVG buy ▲ or sell ▼ prints.", say: "BUY CE · SL below the zone / SELL PE · SL above the zone." },
  rjb: { when: "Clubbed Rejection Block: RJB + ABS (institutional limit absorption at the 50% midpoint) OR RJB + Bias (19-factor Bias agrees with bounce). Raw unconfirmed touches filtered.", say: "“RJB + ABS BUY support” / “RJB + Bias BUY support”" },
  crt: { when: "CRT entry — Setup D.", say: "Toward the other side of the CRT box; TP1 50 %, SL beyond the sweep wick." },
  dol: { when: "The D.O.L target is reached.", say: "“book profit / trail SL”" },
  gamma: { when: "γ ✓✓✓ — EMA 9/21/50 stack + a flag AT a gamma level breaking + volume ≥ 1.5× (needs the γ Gamma 3✓ layer on).", say: "“gamma BUY” — SL flag edge, target next gamma level." },
  nodes: { when: "● ENTRY — price stretched beyond VWAP ±2σ INTO a stacked D/W/M volume zone and closed back (needs the Stacked nodes layer on).", say: "“ENTRY BUY at W M A” — reversal, first target VWAP." },
  flow: { when: "The order-flow read FLIPS to BUY or SELL (delta, CVD, stacked imbalance, big trades, room to the wall). Needs one Order-flow PRO layer on.", say: "“Order flow → BUY (4 vs 1)”" },
  abs: { when: "Institutional Absorption (aggressive volume absorbed at support/resistance) or Aggression spike prints on the closed candle.", say: "“ABSORPTION BUY — aggressive sellers absorbed at support” / “AGGRESSION SELL”" },
  wick: { when: "Price touches a virgin Higher Time Frame (HTF) wick for the FIRST TIME: 1H Hourly ★ (1×), 1D Daily ★★ (2×), or 1W Weekly ★★★ (3×). Alerts once only.", say: "“Hourly Wick first test support” / “Daily Wick first test” / “Weekly Wick first test”" },
  aplus: { when: "Bias ≥ {N} AND at least one of ✓3 · IFVG · RJB+ · CRT · γ ✓✓✓ · ● ENTRY · order flow · ABS on the SAME side, SAME candle. ×2 / ×3 = how many agree.", say: "Different rising tone; desktop pop-up stays until you close it. Strongest alert — ×2 or ×3 even more so." },
  deep_rev: { when: "Deep session low/high swept (>= 100 pts) + writers impulse flip / absorption + micro-structure break (MSS/CHOCH).", say: "“DEEP REVERSAL SNIPER” — High probability reversal at session extremes." },
  trade_mgmt_trail: { when: "Trailing Stop moves by >= 15 pts in favorable direction.", say: "“HOLD · Trail SL updated”" },
};

const LBL: Record<SigKind, string> = {
  bias: "bias",
  conf: "✓3",
  ocAct: "PE−CE",
  smt: "SMT",
  ifvg: "IFVG",
  rjb: "RJB+",
  crt: "CRT",
  dol: "D.O.L",
  gamma: "γ✓✓✓",
  nodes: "●ENTRY",
  flow: "🧭flow",
  abs: "Δ/ABS",
  wick: "HTF Wick",
  aplus: "★A+",
  deep_rev: "🚨Deep Rev",
  trade_mgmt_trail: "🛡️Trail SL",
};

export function SignalControls({ p, setP }: { p: SigPrefs; setP: (f: (x: SigPrefs) => SigPrefs) => void }) {
  return (
    <span className="flex flex-wrap items-center gap-1">
      <button onClick={() => setP((x) => ({ ...x, on: !x.on }))}
        className={`pill ${p.on ? "bg-gold-500 text-ink-900 font-semibold" : "bg-ink-800 text-slate-400"}`}
        title="Signal alerts on the latest closed candle — information only, never places orders">📣 signals {p.on ? "ON" : "off"}</button>
      {p.on && <>
        <select value={p.minBias} onChange={(e) => setP((x) => ({ ...x, minBias: Number(e.target.value) }))}
          className="pill bg-ink-800 text-slate-200" title="Bias alert when at least this many of the 19 factors agree">
          {[9, 10, 11, 12, 13, 14, 15, 16].map((n) => <option key={n} value={n}>bias ≥ {n}</option>)}
        </select>
        {(Object.keys(LBL) as SigKind[]).map((k) => (
          <span key={k} className="group relative">
            <button onClick={() => setP((x) => ({ ...x, kinds: { ...x.kinds, [k]: !x.kinds[k] } }))}
              className={`pill ${p.kinds[k] ? "bg-ink-700 text-slate-100" : "bg-ink-800 text-slate-500 line-through"}`}>{LBL[k]}</button>
            <span className="pointer-events-none invisible absolute right-0 top-full z-50 mt-1 w-[340px] rounded-lg border border-gold-500/50 bg-[#060b16] p-3 text-[14px] leading-snug text-slate-100 shadow-2xl group-hover:visible">
              <b className="text-gold-400 text-[15px]">{LBL[k]}</b> <span className="text-slate-400 text-[12px]">({p.kinds[k] ? "on — click to turn off" : "off — click to turn on"})</span>
              <span className="mt-1 block"><b>Fires when:</b> {HELP[k].when.replace("{N}", String(p.minBias))}</span>
              <span className="mt-1 block text-cyan-200"><b>Says:</b> {HELP[k].say}</span>
            </span>
          </span>
        ))}
      </>}
    </span>
  );
}
