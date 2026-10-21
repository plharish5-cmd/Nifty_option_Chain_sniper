// chartKit.ts — makes the chart readable: one marker per bar & side (merged), short labels, text only where it
// matters (recent / important), and a full-text event index for the hover card.
import type { SeriesMarker, Time, UTCTimestamp } from "lightweight-charts";
import type { SniperHoverItem } from "../api";

export type Ev = {
  time: number; side: "above" | "below"; layer: string; kind: string; dir: "bull" | "bear" | "flat";
  short: string;        // what is printed on the chart (≤ ~10 chars)
  full: string;         // what the hover card says
  color: string; shape: SeriesMarker<Time>["shape"]; size: number;
  pri: number;          // 3 = always labelled, 2 = labelled if recent, 1 = shape only
};

// Plain-English meaning for the hover card, by kind
export const MEANING: Record<string, string> = {
  BOS: "Break of structure — price closed past the last swing: trend continues",
  MSS: "Market structure shift — first break AGAINST the trend: possible reversal",
  SWEEP: "Liquidity sweep — wick took out stops beyond a high/low, then closed back",
  FALSE_BO: "False breakout — closed beyond a level, then failed back inside",
  DELTA_ABSORPTION: "Absorption — heavy aggressive volume but price didn't move: passive orders soaking it up",
  DELTA_AGGRESSION: "Aggression — buyers/sellers lifting the offer / hitting the bid hard",
  DELTA_FLIP: "Delta flip — cumulative delta changed sign",
  FOOTPRINT_IMBALANCE: "Footprint imbalance — one side ≥ 3× the other at a price",
  CRT_ENTRY: "CRT entry — manipulation of the range reclaimed: trade toward the other side",
  CRT_TBS: "TBS — turtle-body soup: candle body back inside the CRT range",
  CRT_M: "Manipulation candle — took the CRT high/low",
  AUCTION_ACCEPT: "Acceptance — two closes beyond the level: value is moving there",
  AUCTION_REJECT: "Rejection — probed the level and closed back: value refused",
  AUCTION_NO_FOLLOW: "No follow-through after the probe",
  RVOL_ACTIVATED: "RVOL turned on — today's volume ≥ 1.5× normal for this time of day",
  RVOL_SPIKE: "RVOL spike — ≥ 2.5× normal: institutions active",
  RVOL_FADING: "RVOL fading — participation dropping back",
  BREAKOUT_WITH_RVOL: "Breakout WITH volume — more likely to hold",
  BREAKOUT_NO_RVOL: "Breakout WITHOUT volume — trap risk",
  IFVG_FLIP: "FVG inverted — price CLOSED through the gap, so the old support is now resistance (or vice versa)",
  IFVG_SELL: "IFVG SELL — price came back into the inverted gap (now resistance) and closed back below it: sell / buy PE, SL above the zone",
  IFVG_BUY: "IFVG BUY — price came back into the inverted gap (now support) and closed back above it: buy CE, SL below the zone",
  QML_ARMED: "Quasimodo armed — H→L→HH→close below L (or L→H→LL→close above H) on the LINE chart; wait for price to come back to the QML level",
  QML_TRIG: "QML triggered — price tested the QML level and closed back away from it: entry, SL beyond the HH/LL wick",
  QML_PT: "Line-chart structure point of the Quasimodo (close prices, wicks ignored)",
  RBS_ARMED: "Resistance became support — a line-chart peak broken by a body close; buy the first retest",
  RBS_TRIG: "RBS triggered — retest of the broken peak held with a green close: buy CE, SL below the level",
  SBR_ARMED: "Support became resistance — a line-chart valley broken by a body close; sell the first retest",
  SBR_TRIG: "SBR triggered — retest of the broken valley held with a red close: buy PE, SL above the level",
  SNR_UPTHRUST: "Upthrust fakeout — wick above a line-chart peak, body closed back below: bearish trap",
  SNR_SPRING: "Spring fakeout — wick below a line-chart valley, body closed back above: bullish trap",
  OI_SURGE: "Futures OI surge — unusual position change this bar",
  OC_ACT: "PE−CE table action switched — option writers' positioning now favours this side (from 8501). Strongest when price structure agrees",
  DOL: "Draw on liquidity — after an ERL sweep price tends to rebalance into the nearest FVG (IRL); after an FVG tap it tends to run to the next old high/low (ERL)",
  DOL_HIT: "D.O.L reached — the target was traded into",
  SMT: "SMT divergence — NIFTY and BANKNIFTY disagree at a swing (one makes a new high/low, the other doesn't): the sweep is weak, expect a turn",
  MSB_STRONG: "Strong MSB — decisive impulse body close beyond origin swing leaving imbalance (High Quality)",
  MSB_WEAK: "Weak MSB — wick struggle or internal sub-range break (caution for fakeouts / stop hunts)",
  PURE_SFP: "Pure Swing Failure Pattern — wick sweeps liquidity, candle closes firmly back inside",
  CSFP: "Considerable SFP — tiny pierce (<=0.18 ATR), immediately reversed by next bar",
  GRAY_ACCUMULATION: "Gray Accumulation — 3+ candles hovering tightly: order absorption, breakout watch (VETO counter-trend)",
  DRAGON_FRUIT: "Dragon Fruit — lowest risk sweet spot: displacement FVG touching origin Order Block",
  MITIGATION_BLOCK: "Mitigation Block — failure swing supply/demand zone where trapped traders exit at breakeven",
  ROTE: "ROTE — Reverse Optimal Trade Entry 0.70-0.80 Fibonacci retracement to target",
  TT3: "Three Tap Setup — Tap 1 hidden zone retest inside consolidation range",
  POI_SUPPLY: "A+ Sniper Supply Zone — Institutional sell-side order block & imbalance cluster",
  POI_DEMAND: "A+ Sniper Demand Zone — Institutional buy-side accumulation zone where unfilled limit orders rest",
  LINE_ENTRY: "Sniper Entry Line — Optimal institutional execution price aligned with proximal POI edge",
  LINE_SL: "Hard Stop-Loss Zone — Structural invalidation level placed beyond manipulation wick + ATR buffer",
  LINE_BE1R: "1R Breakeven Trigger — Dynamic Trade Management: move Stop-Loss to Entry for 0 risk",
  LINE_TP1: "Target 1 (TP1) — Primary opposing liquidity pool / unmitigated imbalance magnet",
  LINE_TP2: "Runner Target (TP2) — Extended swing target at opposing GPX corridor extreme",
  GPX_CEILING: "GPX Macro Ceiling — HTF Extreme Premium supply boundary",
  GPX_EQ: "GPX Equilibrium — 50% fair value divider between Premium and Discount",
  GPX_FLOOR: "GPX Macro Floor — HTF Extreme Discount demand boundary",
  PRAHAAR_BUY_CE: "Prahaar BUY ATM CALL — Bullish EMA trend, positive MACD crossover, ADX >= 22 & supportive VIX",
  PRAHAAR_BUY_PE: "Prahaar BUY ATM PUT — Bearish EMA trend, negative MACD crossover, ADX >= 22 & supportive VIX",
  PRAHAAR_MASTER_SNIPER_CE: "🎯 A+ MASTER SNIPER (CALL) — Prahaar BUY ATM CALL + PE Support Wall + Bullish CVD Absorption",
  PRAHAAR_MASTER_SNIPER_PE: "🎯 A+ MASTER SNIPER (PUT) — Prahaar BUY ATM PUT + CE Resistance Wall + Bearish CVD Distribution",
};

// shorten the backend's text for the chart: "BOS·weak lowV" → "BOS", "Sweep EQH" → "Sweep EQH"
export function shortText(kind: string, text: string): string {
  const t = (text || "").replace(/\s+/g, " ").trim();
  if (kind === "BOS" || kind === "MSS") return kind;
  if (kind === "MSB_STRONG") return "Strong MSB";
  if (kind === "MSB_WEAK") return "Weak MSB";
  if (kind === "PURE_SFP") return "Pure SFP";
  if (kind === "CSFP") return "cSFP";
  if (kind === "GRAY_ACCUMULATION") return "Gray Accum";
  if (kind === "SWEEP") return t.replace(/V\d+(\.\d+)?/g, "").replace(/Sweep\s*/i, "Sweep ").trim().slice(0, 12);
  if (kind === "DELTA_ABSORPTION") return "ABS";
  if (kind === "DELTA_AGGRESSION") return t.includes("−") || t.includes("-") ? "Δ−" : "Δ+";
  return t.length > 12 ? t.slice(0, 11) + "…" : t;
}

/** Merge events on the same bar & side into ONE marker; label only the important / recent ones. */
export function toMarkers(evs: Ev[], lastTime: number, barSec: number, recentBars = 24): SeriesMarker<Time>[] {
  const g = new Map<string, Ev[]>();
  for (const e of evs) {
    const k = `${e.time}|${e.side}`;
    (g.get(k) ?? g.set(k, []).get(k)!).push(e);
  }
  const out: SeriesMarker<Time>[] = [];
  const recentFrom = lastTime - recentBars * barSec;
  for (const list of g.values()) {
    list.sort((a, b) => b.pri - a.pri || b.size - a.size);
    const top = list[0];
    const recent = top.time >= recentFrom;
    const labelled = list.filter((e) => e.pri >= 3 || (e.pri >= 2 && recent));
    let names = [...new Set(labelled.map((e) => e.short))];
    names = names.filter((n) => !names.some((o) => o !== n && o.startsWith(n)));   // "Sweep" inside "Sweep PDL"
    const text = names.length > 2 ? `${names.slice(0, 2).join(" · ")} +${names.length - 2}` : names.join(" · ");
    out.push({
      time: top.time as UTCTimestamp, position: top.side === "above" ? "aboveBar" : "belowBar",
      shape: top.shape, color: top.color, size: top.size, text,
    });
  }
  out.sort((a, b) => (a.time as number) - (b.time as number));
  return out;
}

export function index(evs: Ev[]): Map<number, Ev[]> {
  const m = new Map<number, Ev[]>();
  for (const e of evs) (m.get(e.time) ?? m.set(e.time, []).get(e.time)!).push(e);
  return m;
}

export const BAR_SEC: Record<string, number> = { "1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "4h": 14400, "1D": 86400, "1W": 604800 };

/**
 * Hit-Test Engine for Interactive Sniper Hover Tooltips:
 * When cursor is within ±8 pixels or price tolerance of any active Sniper POI Box,
 * Trade Plan Line (Entry, SL, 1R BE, TP1/TP2, GPX), or Marker (Strong MSB, Pure SFP),
 * returns the matching SniperHoverItem to render an institutional tooltip card.
 */
export function hitTestSniperHover(
  items: SniperHoverItem[],
  cursorTime: number | undefined,
  cursorPrice: number,
  atr: number,
  barSec: number,
  priceToCoordinate?: (price: number) => number | null,
  cursorY?: number
): SniperHoverItem | null {
  if (!items || !items.length) return null;

  // 1. Check Markers (Strong MSB & Pure SFP) - within ~1.8 bars of time & 0.40 * atr of price, or within 12px
  for (const it of items) {
    if (it.kind.startsWith("MARKER_") && it.price != null) {
      const pDist = Math.abs(cursorPrice - it.price);
      let yPixelDist = 999;
      if (priceToCoordinate && cursorY != null) {
        const yc = priceToCoordinate(it.price);
        if (yc != null) yPixelDist = Math.abs(cursorY - yc);
      }
      let timeMatch = true;
      if (it.time != null && cursorTime != null) {
        timeMatch = Math.abs(cursorTime - Number(it.time)) <= barSec * 1.8;
      }
      if ((timeMatch && pDist <= 0.40 * atr) || yPixelDist <= 12) {
        return it;
      }
    }
  }

  // 2. Check Key Lines (Entry, SL, 1R BE, TP1, TP2, GPX Ceiling, GPX EQ, GPX Floor) - within 0.18 * atr or ±8px
  for (const it of items) {
    if ((it.kind.startsWith("LINE_") || it.kind.startsWith("GPX_")) && it.price != null) {
      const pDist = Math.abs(cursorPrice - it.price);
      let yPixelDist = 999;
      if (priceToCoordinate && cursorY != null) {
        const yc = priceToCoordinate(it.price);
        if (yc != null) yPixelDist = Math.abs(cursorY - yc);
      }
      if (pDist <= 0.18 * atr || yPixelDist <= 8) {
        return it;
      }
    }
  }

  // 3. Check POI Boxes (Supply & Demand Zones) - cursor inside vertical bounds [bot..top]
  for (const it of items) {
    if (it.kind.startsWith("POI_")) {
      const top = Math.max(it.top, it.bot);
      const bot = Math.min(it.top, it.bot);
      if (cursorPrice >= bot && cursorPrice <= top) {
        return it;
      }
    }
  }

  return null;
}
