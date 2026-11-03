import { nearestOiWalls, oiWallRange } from "./oiWallViewport";
// Chart.tsx — candlesticks + ICT/SMC overlays (Lightweight Charts v5).
import { useEffect, useMemo, useRef, useState } from "react";
import {
  BaselineSeries, CandlestickSeries, ColorType, createChart, createSeriesMarkers, CrosshairMode, HistogramSeries, LineSeries, LineStyle,
  type AutoscaleInfo, type IChartApi, type ISeriesApi, type ISeriesMarkersPluginApi, type MouseEventParams, type Time, type UTCTimestamp, type IPriceLine,
} from "lightweight-charts";
import type { Analysis, Candle, TF, WriterMetrics, InstitutionalLevelsRes, GannPivotsGapsRes, ItmAtmOiWallsRes } from "../api";
import { bucket } from "../api";
import type { DivergencePayload } from "./divergence";
import { dayRegime } from "./regime";
import { computeBias, cvdExhaustionSignals, FACTORS, BIAS_POLICY, BIAS_NAME, type BiasRow } from "./bias";
import { useOcFeed, useSmt, useBiasAgreement, type SmtForming } from "./ocfeed";
import { computeSmartRows, playOiBeep, type Row, type SmartRow } from "./OcLab";
import { api } from "../api";
import { computeGex, confirm3, emas, GammaPrimitive, gammaHits, type GexMap } from "./gamma";
import { findEntries, NodesPrimitive, nodesRead, type NodesView } from "./nodes";
import { buildBubbles, buildWalls, computeCvdDivergences, OrderFlowPrimitive, readAt, type Wall, type OiLevel, type CvdDivergence } from "./orderflow";
import { computeDol } from "./dol";
import { turtleSoup } from "./turtlesoup";
import { powerOfThree } from "./powerofthree";
import { dispatch, scanSignals, type SigPrefs } from "./signalAlerts";
import { lakh, type AlertRow, type SniperHoverItem } from "../api";
import { EV_LAYER, GUIDE, KIND_ACT } from "./guide";
import { confluence, FootprintPrimitive, FP_SCALE, setFpScale, FP_MEANING, FP_WORD, kfmt, type FpBar } from "./footprint";
import { ZonesPrimitive, type Box, type Seg } from "./zones";
import { BAR_SEC, hitTestSniperHover, index, MEANING, shortText, toMarkers, oiDecisionGuide, type Ev } from "./chartKit";
import MtfPanel from "./MtfPanel";
import { StdvPrimitive, detectManipulationLegs, createManualStdvLeg, type StdvLeg } from "./stdv";
import { aggregateWeeklyCandles, extractWicks, filterVirginWicks, type HtfWick } from "./htfWicks";
import { computePrahaar, type PrahaarResult, type PrahaarSignal } from "./prahaar";
import { detectGtfZones, evaluateGtfMasterConfluence, hitTestGtfZone, getGtfExplanation, type GtfMarkingMode, type GtfResult, type GtfZone } from "./gtf";
import PcrTrinitySuite from "./PcrTrinitySuite";
import OIMatrixSuite from "./OIMatrixSuite";
import AlgoCockpit from "./AlgoCockpit";

export type Layers = { ob: boolean; fvg: boolean; liq: boolean; structure: boolean; swings: boolean; range: boolean; session: boolean; delta: boolean; deltaDetail: boolean; crt: boolean; auction: boolean; volume: boolean; oi: boolean; rvol: boolean; qml?: boolean; fp?: boolean; bias?: boolean; dol?: boolean; smt?: boolean; heat?: boolean; ocAct?: boolean; cvd?: boolean; cells?: boolean; walls?: boolean; bubbles?: boolean; nodes?: boolean; gamma?: boolean; tsoup?: boolean; rblock?: boolean; p3?: boolean; stdv?: boolean; advPa?: boolean; prahaar?: boolean; gtfZones?: boolean; ictLevels?: boolean; rsiDiv?: boolean };

const C = {
  bull: "#10b981", bear: "#ef4444", gold: "#e8c35a", grid: "#131d33", text: "#94a3b8",
  obBull: "rgba(16,185,129,0.14)", obBear: "rgba(239,68,68,0.14)", fvgBull: "rgba(56,189,248,0.14)", fvgBear: "rgba(244,114,182,0.14)",
};

// Shared writer synthesis — one source of truth for both the HUD card and the order-flow box (so they can't disagree).
type WBal = { pcr: number | null; ceOI: number; peOI: number; ceChg: number; peChg: number; net: number;
  maxPain: number | null; trend: "rising" | "falling" | "flat"; pcrPrev: number | null; spot: number | null };
function writerVerdict(wb: WBal, walls: Wall[]) {
  let score = 0; const pros: string[] = []; const cons: string[] = [];
  const { pcr, net, trend, maxPain, spot: wsp } = wb;
  if (pcr != null) { if (pcr >= 1.2) { score += 1; pros.push("put-heavy chain"); } else if (pcr <= 0.8) { score -= 1; cons.push("call-heavy chain"); } }
  if (trend === "rising") { score += 1; pros.push("puts accelerating"); } else if (trend === "falling") { score -= 1; cons.push("calls accelerating"); }
  if (net > 5e6) { score += 1; pros.push("net put writing"); } else if (net < -5e6) { score -= 1; cons.push("net call writing"); }
  if (maxPain != null && wsp != null) { if (wsp < maxPain - 10) { score += 0.5; pros.push("max-pain pull up"); } else if (wsp > maxPain + 10) { score -= 0.5; cons.push("max-pain pull down"); } }
  const sp = wsp ?? 0;
  const peW = walls.filter((w) => w.side === "PE"), ceW = walls.filter((w) => w.side === "CE");
  const support = peW.filter((w) => w.p <= sp).sort((a, b) => b.p - a.p)[0] ?? peW.sort((a, b) => b.oi - a.oi)[0];
  const resistance = ceW.filter((w) => w.p >= sp).sort((a, b) => a.p - b.p)[0] ?? ceW.sort((a, b) => b.oi - a.oi)[0];
  if (support?.status === "building" && resistance?.status === "unwinding") { score += 1; pros.push("support firming, resistance cracking"); }
  if (support?.status === "unwinding" && resistance?.status === "building") { score -= 1; cons.push("support cracking, resistance firming"); }
  const bias: "BULLISH" | "BEARISH" | "NEUTRAL" = score >= 1.5 ? "BULLISH" : score <= -1.5 ? "BEARISH" : "NEUTRAL";
  const reason = (score >= 0 ? pros : cons).slice(0, 2).join(" · ") || "signals offsetting";
  return { score, bias, strong: Math.abs(score) >= 2.5, support, resistance, reason };
}

export default function Chart({ tf, candles, analysis, asof, onAsof, layers, spot, spotTs, marketOpen, onExpand, expanded, sig, onSignal, onAutoSignal, expiries, stdvLeg, isStdvPicking, onStartStdvPicking, onStdvPicked, onCancelStdvPicking, onResetToAutoStdv }: {
  asof?: string | null; onAsof?: (d: string | null) => void;
  sig?: SigPrefs; onSignal?: (a: AlertRow) => void;
  onAutoSignal?: (a: AlertRow, ctx: { last: Candle; atr: number; nearWall: number | null }) => void;
  tf: TF; candles: Candle[]; analysis: Analysis | null; layers: Layers; spot: number | null; spotTs: number; marketOpen: boolean;
  onExpand?: () => void; expanded?: boolean; expiries?: string[]; stdvLeg?: StdvLeg | null;
  isStdvPicking?: boolean; onStartStdvPicking?: () => void; onStdvPicked?: (leg: StdvLeg) => void; onCancelStdvPicking?: () => void;
  onResetToAutoStdv?: () => void;
}) {
  const box = useRef<HTMLDivElement>(null);
  const chart = useRef<IChartApi | null>(null);
  const series = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const zz = useRef<ISeriesApi<"Line"> | null>(null);
  const spine = useRef<ISeriesApi<"Line"> | null>(null);           // QML layer: the close-price "line chart"
  const markers = useRef<ISeriesMarkersPluginApi<Time> | null>(null);
  const zones = useRef(new ZonesPrimitive());
  const fpPrim = useRef(new FootprintPrimitive());
  const ofPrim = useRef(new OrderFlowPrimitive());
  const ndPrim = useRef(new NodesPrimitive());
  const gHitsRef = useRef<{ time: number; side: "BUY" | "SELL"; px: number }[]>([]);
  const nEntRef = useRef<ReturnType<typeof findEntries>>([]);
  const ofArgsRef = useRef<Parameters<typeof readAt>[1]>({ fp: [], bubbles: [], walls: [], zones: [], atr: 20, candles: [] });
  const gmPrim = useRef(new GammaPrimitive());
  const stdvPrim = useRef(new StdvPrimitive());
  const emaS = useRef<ISeriesApi<"Line">[]>([]);
  const [gexMap, setGexMap] = useState<GexMap | null>(null);
  const [nodesV, setNodesV] = useState<NodesView | null>(null);
  const cvdS = useRef<ISeriesApi<"Baseline"> | null>(null);
  const [fpBars, setFpBars] = useState<FpBar[]>([]);
  const [fpMsg, setFpMsg] = useState<string>("");

  // Institutional ICT Reference Levels (PDH, PDL, 09:15 Open) & Breakout Engine
  const [instLevels, setInstLevels] = useState<InstitutionalLevelsRes | null>(null);
  const [localIctLevels, setLocalIctLevels] = useState<boolean>(() => {
    try {
      const s = localStorage.getItem("ict_levels_active");
      return s !== null ? s === "1" : (layers.ictLevels ?? true);
    } catch {
      return layers.ictLevels ?? true;
    }
  });
  const ictLinesRef = useRef<IPriceLine[]>([]);

  // Rejection Blocks (RB): Monotonic R1-R4 / S1-S4 Swing Levels, Unfilled Gaps & Gann Sq9 + Time Cycles
  interface RecalcAudit {
    total_historical_days_scanned: number;
    first_historical_date: string;
    latest_settled_date: string;
    post_1540_settled: boolean;
    last_full_recalc_ist: string;
  }
  interface GannLevel { level: string; degrees: number; price: number | null }
  interface ExtendedGannPivotsGapsRes extends GannPivotsGapsRes {
    recalc_audit?: RecalcAudit;
    gann_pdh_resistances?: GannLevel[];
    gann_pdh_supports?: GannLevel[];
    gann_pdh_anchor?: number | null;
  }
  const [gannData, setGannData] = useState<ExtendedGannPivotsGapsRes | null>(null);
  const [recalcLoading, setRecalcLoading] = useState<boolean>(false);

  const handleForceRecalc = async (e: React.MouseEvent) => {
    e.stopPropagation();
    try {
      setRecalcLoading(true);
      const res = await fetch("/api/desk/gann-pivots-gaps/recalculate", { method: "POST" });
      if (res.ok) {
        const data = await res.json();
        if (data.levels) {
          setGannData(data.levels);
        } else {
          const fresh = await fetch("/api/desk/gann-pivots-gaps");
          if (fresh.ok) {
            setGannData(await fresh.json());
          }
        }
      }
    } catch (err) {
      console.error("Failed to recalculate gann pivots gaps:", err);
    } finally {
      setRecalcLoading(false);
    }
  };
  const [showRbPivotsGaps, setShowRbPivotsGaps] = useState<boolean>(() => {
    try {
      const s = localStorage.getItem("rb_pivots_gaps_active");
      return s !== null ? s === "1" : (layers.rblock ?? true);
    } catch {
      return layers.rblock ?? true;
    }
  });
  const [showRbGann, setShowRbGann] = useState<boolean>(() => {
    try {
      const s = localStorage.getItem("rb_gann_active");
      return s !== null ? s === "1" : false;
    } catch {
      return false;
    }
  });
  // PDH-anchored Gann ladder. Separate from the close-anchored one above because the two were
  // measured separately and did not perform the same (scripts/backtest_gann.py, 33 NIFTY sessions):
  // PDH +5.5pp over a random baseline at 2.6 sd and still +3.3pp with the best three sessions
  // removed; PDC +2.6pp at 1.4 sd. Off by default, like every unproven layer on this desk.
  const [showRbGannPdh, setShowRbGannPdh] = useState<boolean>(() => {
    try {
      const s = localStorage.getItem("rb_gann_pdh_active");
      return s !== null ? s === "1" : false;
    } catch {
      return false;
    }
  });
  const toggleRbGannPdh = () => {
    setShowRbGannPdh((prev) => {
      const next = !prev;
      try { localStorage.setItem("rb_gann_pdh_active", next ? "1" : "0"); } catch { /* private mode */ }
      return next;
    });
  };
  const [showRbGuidance, setShowRbGuidance] = useState<boolean>(false);
  const [showPdhGuidance, setShowPdhGuidance] = useState<boolean>(false);
  // REVIEW MODE. The candles are from a past session, but every server-derived overlay — Gann,
  // wick zones, the 8-EMA engine, stacked nodes — is computed against TODAY and cannot be
  // rewound. Painting today's levels onto October's chart would invent confluences that never
  // existed, so those layers are suppressed here rather than shown misleadingly.
  const reviewing = !!asof;
  // Measured 1H upper-wick zones. A strict SUBSET of the levels the htf-wick-ema engine already
  // publishes — nothing new is fetched and that engine is untouched. Only the zones that actually
  // passed scripts/backtest_wick_zones.py are drawn (38 first touches over 36 NIFTY sessions,
  // 2026-09-08 to 2026-10-30): 1H, upper, a real wick not a shaved body, depth inside the band,
  // and still virgin. Lower wicks are deliberately absent — they measured -0.4 sd and died to
  // -11.2 pts/trade once their best five events were removed. Off by default, like every
  // unproven layer on this desk.
  const [showRbWickTested, setShowRbWickTested] = useState<boolean>(() => {
    try {
      const s = localStorage.getItem("rb_wick_tested_active");
      return s !== null ? s === "1" : false;
    } catch {
      return false;
    }
  });
  const toggleRbWickTested = () => {
    setShowRbWickTested((prev) => {
      const next = !prev;
      try { localStorage.setItem("rb_wick_tested_active", next ? "1" : "0"); } catch { /* private mode */ }
      return next;
    });
  };
  const [showWickTestedGuidance, setShowWickTestedGuidance] = useState<boolean>(false);
  const rbPriceLinesRef = useRef<IPriceLine[]>([]);

  useEffect(() => {
    if (layers.rblock !== undefined) {
      try {
        if (localStorage.getItem("rb_pivots_gaps_active") === null) {
          setShowRbPivotsGaps(layers.rblock);
        }
      } catch {}
    }
  }, [layers.rblock]);

  const toggleRbPivotsGaps = () => {
    setShowRbPivotsGaps((prev) => {
      const next = !prev;
      try { localStorage.setItem("rb_pivots_gaps_active", next ? "1" : "0"); } catch {}
      return next;
    });
  };

  const toggleRbGann = () => {
    setShowRbGann((prev) => {
      const next = !prev;
      try { localStorage.setItem("rb_gann_active", next ? "1" : "0"); } catch {}
      return next;
    });
  };

  interface HtfWickEmaRes {
    ok: boolean;
    as_of_ist: string;
    spot: number;
    ema8_1h: {
      value: number | null;
      slope: string;
      position: string;
      distance_pts: number;
      approach_state: string;
      history: { ts: number; time: string; val: number }[];
    };
    active_levels: {
      id: string;
      tf: string;
      kind: string;
      side: string;
      top: number;
      bottom: number;
      ref_price: number;
      wick_pts: number;
      is_shaved: boolean;
      created_ts: number;
      created_ist: string;
      touch_count: number;
      is_virgin: boolean;
      direction_of_approach?: string;
      mfe_bounce_pts?: number;
      hit_20: boolean;
      hit_40: boolean;
      label: string;
      distance_pts?: number;
    }[];
    live_alerts: {
      id: string;
      level_id: string;
      kind: string;
      side: string;
      tf: string;
      price: number;
      distance_pts: number;
      title: string;
      body: string;
      voice_text: string;
    }[];
    backtest_summary: {
      dataset_range: string;
      total_intraday_candles: number;
      total_1h_candles: number;
      total_4h_candles: number;
      wick_1h: { first_touches: number; target_20_wins: number; win_rate_20_pct: number; target_40_wins: number; win_rate_40_pct: number; avg_mfe_pts: number; stopped_out: number };
      wick_4h: { first_touches: number; target_20_wins: number; win_rate_20_pct: number; target_40_wins: number; win_rate_40_pct: number; avg_mfe_pts: number; stopped_out: number };
      ema8_1h: { first_touches: number; target_20_wins: number; win_rate_20_pct: number; target_40_wins: number; win_rate_40_pct: number; avg_mfe_pts: number; stopped_out: number };
    };
    null_baseline_summary?: {
      metadata: {
        categories_tested: number;
        cells_tested: number;
        statement: string;
        dataset_range: string;
        total_sessions: number;
        total_intraday_candles: number;
        total_1h_candles: number;
        total_4h_candles: number;
      };
      categories: Record<string, any>;
    };
    per_event_tables?: Record<string, any[]>;
    strict_trade_simulation?: {
      ok: boolean;
      audit_report: Record<string, { status: string; description: string; impact: string }>;
      ema8_1h: {
        overall: {
          n: number;
          raw_20_pct: number;
          raw_40_pct: number;
          strict_20_pct: number;
          strict_40_pct: number;
          stopped_pct: number;
          open_pct: number;
          exp_pts: number;
          wins_20: number;
          wins_40: number;
          stopped: number;
        };
        support_bounce: {
          n: number;
          raw_20_pct: number;
          raw_40_pct: number;
          strict_20_pct: number;
          strict_40_pct: number;
          stopped_pct: number;
          open_pct: number;
          exp_pts: number;
          wins_20: number;
          wins_40: number;
          stopped: number;
        };
        resistance_rejection: {
          n: number;
          raw_20_pct: number;
          raw_40_pct: number;
          strict_20_pct: number;
          strict_40_pct: number;
          stopped_pct: number;
          open_pct: number;
          exp_pts: number;
          wins_20: number;
          wins_40: number;
          stopped: number;
        };
        audit_ledger_last_15: {
          date: string;
          time: string;
          dir: string;
          long: boolean;
          entry_price: number;
          ema_val: number;
          mfe_before_sl: number;
          hit_20_first: boolean;
          hit_sl_first: boolean;
          final_outcome: string;
        }[];
      };
    };
  }

  const [htfWickEmaData, setHtfWickEmaData] = useState<HtfWickEmaRes | null>(null);
  const [showRbWickEma, setShowRbWickEma] = useState<boolean>(() => {
    try {
      const s = localStorage.getItem("rb_wick_ema_active");
      return s !== null ? s === "1" : true;
    } catch {
      return true;
    }
  });
  const [selectedRbCategory, setSelectedRbCategory] = useState<string | null>(null);
  const [showStrictAuditLedger, setShowStrictAuditLedger] = useState<boolean>(false);

  const toggleRbWickEma = () => {
    setShowRbWickEma((prev) => {
      const next = !prev;
      try { localStorage.setItem("rb_wick_ema_active", next ? "1" : "0"); } catch {}
      return next;
    });
  };

  useEffect(() => {
    let alive = true;
    const fetchHtfWickEma = () => {
      fetch("/api/desk/htf-wick-ema-rb")
        .then((res) => res.json())
        .then((data: HtfWickEmaRes) => {
          if (alive && data?.ok) setHtfWickEmaData(data);
        })
        .catch(() => {});
    };
    fetchHtfWickEma();
    const timer = setInterval(fetchHtfWickEma, 12000);
    return () => {
      alive = false;
      clearInterval(timer);
    };
  }, []);

  useEffect(() => {
    let alive = true;
    const fetchGannPivots = () => {
      api.gannPivotsGaps().then((res) => {
        if (alive && res?.ok) setGannData(res);
      }).catch(() => {});
    };
    fetchGannPivots();
    const timer = setInterval(fetchGannPivots, 10000);
    return () => {
      alive = false;
      clearInterval(timer);
    };
  }, []);

  const [itmAtmIntel, setItmAtmIntel] = useState<ItmAtmOiWallsRes | null>(null);

  useEffect(() => {
    let alive = true;
    const fetchItmAtm = () => {
      api.itmAtmOiWalls().then((res) => {
        if (alive && res?.ok) setItmAtmIntel(res);
      }).catch(() => {});
    };
    fetchItmAtm();
    const timer = setInterval(fetchItmAtm, 10000);
    return () => {
      alive = false;
      clearInterval(timer);
    };
  }, []);

  // ── RSI divergence watch levels ───────────────────────────────────────────────────────────────
  // Only the ACTIVE watch is drawn, and only two lines: the trigger price must be closed through
  // before the divergence means anything, and the invalidation beyond which the premise is gone.
  // Deliberately NOT a pivot-to-pivot connector: a diagonal line drawn to a past high reads like a
  // forecast, and the point of this engine is that a divergence is a watch, not a prediction.
  const divLinesRef = useRef<IPriceLine[]>([]);
  const [divergence, setDivergence] = useState<DivergencePayload | null>(null);
  // Driven by the layer chip ("📉 RSI Divergence" in the VIEW row), which is where every other
  // overlay is controlled from. The floating button that used to live in the top-right corner was
  // the wrong home: that corner is already shared by Prahaar, GTF Zones and the EXPIRY banner, so a
  // seventh chip there collided with the rest and was effectively invisible.
  const showDiv = !!layers.rsiDiv;

  // NOTE: the fetch itself lives further down, after `smartRows`, because it needs the day regime.
  // Asking the endpoint for `day_regime=none` -- which this did until the review -- disables the
  // counter-trend veto server-side, so the chart drew trigger and invalidation lines for the very
  // reversal watches the desk bar was reporting as VETOED. On a trend day that is the exact failure
  // this engine exists to prevent: a line on the chart saying "short here" on a session running
  // +217 points. The drawing effect lives there too, since it reads the same regime.

  useEffect(() => {
    if (layers.ictLevels !== undefined) {
      setLocalIctLevels(layers.ictLevels);
    }
  }, [layers.ictLevels]);

  const toggleIctLevels = () => {
    setLocalIctLevels((v) => {
      const next = !v;
      try { localStorage.setItem("ict_levels_active", next ? "1" : "0"); } catch {}
      return next;
    });
  };

  useEffect(() => {
    let alive = true;
    const fetchLevels = () => {
      api.institutionalLevels("13").then((res) => {
        if (alive && res?.ok) setInstLevels(res);
      }).catch(() => {});
    };
    fetchLevels();
    const timer = setInterval(fetchLevels, 8000);
    return () => {
      alive = false;
      clearInterval(timer);
    };
  }, []);

  // Draw / update Institutional ICT Reference Levels (PDH, PDL, 09:15 Open)
  useEffect(() => {
    ictLinesRef.current.forEach((line) => {
      try {
        series.current?.removePriceLine(line);
      } catch {}
    });
    ictLinesRef.current = [];

    if (!localIctLevels || !series.current || !instLevels?.levels) return;

    const { pdh, pdl, session_open } = instLevels.levels;
    try {
      if (pdh > 0) {
        const line = series.current.createPriceLine({
          price: pdh,
          color: "#f59e0b",
          lineWidth: 1,
          lineStyle: LineStyle.Dashed,
          axisLabelVisible: true,
          title: "PDH",
        });
        ictLinesRef.current.push(line);
      }
      if (pdl > 0) {
        const line = series.current.createPriceLine({
          price: pdl,
          color: "#f59e0b",
          lineWidth: 1,
          lineStyle: LineStyle.Dashed,
          axisLabelVisible: true,
          title: "PDL",
        });
        ictLinesRef.current.push(line);
      }
      if (session_open > 0) {
        const line = series.current.createPriceLine({
          price: session_open,
          color: "#06b6d4",
          lineWidth: 1,
          lineStyle: LineStyle.Dotted,
          axisLabelVisible: true,
          title: "09:15 Open",
        });
        ictLinesRef.current.push(line);
      }
    } catch (e) {
      console.warn("Failed creating ICT price lines", e);
    }

    return () => {
      ictLinesRef.current.forEach((line) => {
        try {
          series.current?.removePriceLine(line);
        } catch {}
      });
      ictLinesRef.current = [];
    };
  }, [localIctLevels, instLevels, series.current]);

  // Draw / update Rejection Blocks Historical Pivots (R1-R4, S1-S4), Gaps, and Gann Sq9 Price Lines
  useEffect(() => {
    rbPriceLinesRef.current.forEach((line) => {
      try {
        series.current?.removePriceLine(line);
      } catch {}
    });
    rbPriceLinesRef.current = [];

    if (!series.current || !gannData?.ok) return;

    const formatDateShort = (dStr?: string) => {
      if (!dStr) return "";
      try {
        const parts = dStr.split("-");
        if (parts.length === 3) {
          const months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
          const mIdx = parseInt(parts[1], 10) - 1;
          return `${parseInt(parts[2], 10)}-${months[mIdx] || parts[1]}`;
        }
      } catch {}
      return dStr;
    };

    try {
      // 1. Checkbox 1: R1-R4, S1-S4 & Unfilled Gaps
      if (showRbPivotsGaps) {
        // Resistances (R1..R4) - Red #ef4444
        (gannData.resistances || []).forEach((r, idx) => {
          if (r.price > 0) {
            const dateLabel = r.date ? ` (${formatDateShort(r.date)})` : "";
            const hitLabel = r.hit ? " ✓ HIT" : "";
            const line = series.current!.createPriceLine({
              price: r.price,
              color: "#ef4444",
              lineWidth: 1,
              lineStyle: LineStyle.Solid,
              axisLabelVisible: true,
              title: `${r.level || `R${idx + 1}`} ${r.price.toLocaleString("en-IN")}${dateLabel}${hitLabel}`,
            });
            rbPriceLinesRef.current.push(line);
          }
        });

        // Supports (S1..S4) - Green #22c55e
        (gannData.supports || []).forEach((s, idx) => {
          if (s.price > 0) {
            const dateLabel = s.date ? ` (${formatDateShort(s.date)})` : "";
            const hitLabel = s.hit ? " ✓ HIT" : "";
            const line = series.current!.createPriceLine({
              price: s.price,
              color: "#22c55e",
              lineWidth: 1,
              lineStyle: LineStyle.Solid,
              axisLabelVisible: true,
              title: `${s.level || `S${idx + 1}`} ${s.price.toLocaleString("en-IN")}${dateLabel}${hitLabel}`,
            });
            rbPriceLinesRef.current.push(line);
          }
        });

        // Upper Gaps - Gold / Amber dashed #f59e0b
        (gannData.upper_gaps || []).forEach((ug) => {
          if (ug.price > 0) {
            const dateLabel = ug.date ? ` (Unfilled from ${formatDateShort(ug.date)})` : "";
            const hitLabel = ug.hit ? " ✓ HIT" : "";
            const line = series.current!.createPriceLine({
              price: ug.price,
              color: "#f59e0b",
              lineWidth: 1,
              lineStyle: LineStyle.Dashed,
              axisLabelVisible: true,
              title: `GAP UP ${ug.price.toLocaleString("en-IN")}${dateLabel}${hitLabel}`,
            });
            rbPriceLinesRef.current.push(line);
          }
        });

        // Lower Gaps - Cyan/Purple dashed #a855f7
        (gannData.lower_gaps || []).forEach((lg) => {
          if (lg.price > 0) {
            const dateLabel = lg.date ? ` (Unfilled from ${formatDateShort(lg.date)})` : "";
            const hitLabel = lg.hit ? " ✓ HIT" : "";
            const line = series.current!.createPriceLine({
              price: lg.price,
              color: "#a855f7",
              lineWidth: 1,
              lineStyle: LineStyle.Dashed,
              axisLabelVisible: true,
              title: `GAP DN ${lg.price.toLocaleString("en-IN")}${dateLabel}${hitLabel}`,
            });
            rbPriceLinesRef.current.push(line);
          }
        });
      }

      // 2. Checkbox 2: Gann Sq9 R1..R4 & S1..S4
      if (showRbGann) {
        // Gann Resistances - dotted Rose/Crimson #fb7185
        (gannData.gann_resistances || []).forEach((gr, idx) => {
          if (gr.price && gr.price > 0) {
            const hitLabel = gr.hit ? " ✓ HIT" : "";
            const line = series.current!.createPriceLine({
              price: gr.price,
              color: "#fb7185",
              lineWidth: 1,
              lineStyle: LineStyle.Dotted,
              axisLabelVisible: true,
              title: `Gann R${idx + 1} (${gr.degrees}°) ${gr.price.toLocaleString("en-IN")}${hitLabel}`,
            });
            rbPriceLinesRef.current.push(line);
          }
        });

        // Gann Supports - dotted Teal/Emerald #34d399
        (gannData.gann_supports || []).forEach((gs, idx) => {
          if (gs.price && gs.price > 0) {
            const hitLabel = gs.hit ? " ✓ HIT" : "";
            const line = series.current!.createPriceLine({
              price: gs.price,
              color: "#34d399",
              lineWidth: 1,
              lineStyle: LineStyle.Dotted,
              axisLabelVisible: true,
              title: `Gann S${idx + 1} (${gs.degrees}°) ${gs.price.toLocaleString("en-IN")}${hitLabel}`,
            });
            rbPriceLinesRef.current.push(line);
          }
        });
      }

      // 3. Checkbox 3: 1H/4H Wicks & Shaved Extremes + 1H 8-EMA
      if (!reviewing && showRbWickEma && htfWickEmaData?.ok) {
        // 1H 8-EMA Dynamic Line - Electric Cyan #06b6d4, 2px solid
        if (htfWickEmaData.ema8_1h?.value && htfWickEmaData.ema8_1h.value > 0) {
          const emaVal = htfWickEmaData.ema8_1h.value;
          const slope = htfWickEmaData.ema8_1h.slope || "FLAT";
          const line = series.current!.createPriceLine({
            price: emaVal,
            color: "#06b6d4",
            lineWidth: 2,
            lineStyle: LineStyle.Solid,
            axisLabelVisible: true,
            title: `1H 8-EMA ${emaVal.toLocaleString("en-IN")} (${slope}) [Z: +2.0 sd, Silent]`,
          });
          rbPriceLinesRef.current.push(line);
        }

        // Active 1H / 4H Extremes
        (htfWickEmaData.active_levels || []).forEach((lvl) => {
          if (lvl.ref_price > 0) {
            if (lvl.side === "RESISTANCE") {
              // Coral Red #f43f5e, dashed
              const line = series.current!.createPriceLine({
                price: lvl.ref_price,
                color: "#f43f5e",
                lineWidth: lvl.is_virgin ? 2 : 1,
                lineStyle: LineStyle.Dashed,
                axisLabelVisible: true,
                title: lvl.label,
              });
              rbPriceLinesRef.current.push(line);
            } else if (lvl.side === "SUPPORT") {
              // Mint Green #10b981, dashed
              const line = series.current!.createPriceLine({
                price: lvl.ref_price,
                color: "#10b981",
                lineWidth: lvl.is_virgin ? 2 : 1,
                lineStyle: LineStyle.Dashed,
                axisLabelVisible: true,
                title: lvl.label,
              });
              rbPriceLinesRef.current.push(line);
            }
          }
        });
      }
    } catch (err) {
      console.warn("Failed creating Gann/Pivot price lines", err);
    }

    return () => {
      rbPriceLinesRef.current.forEach((line) => {
        try {
          series.current?.removePriceLine(line);
        } catch {}
      });
      rbPriceLinesRef.current = [];
    };
  }, [reviewing, showRbPivotsGaps, showRbGann, showRbWickEma, gannData, htfWickEmaData, series.current]);

  // ── PDH-anchored Gann ladder ────────────────────────────────────────────────────────────────
  // Drawn in its own effect so it never disturbs the close-anchored overlay above.
  //
  // Only the levels price could plausibly reach today are drawn. The ladder spans roughly ±300
  // points, and the backtest found just 2.8 of its 8 levels fall inside a typical session's range;
  // drawing all eight puts five lines where price never goes and turns the chart into the "a level
  // every few points" trap the method's own literature warns about. The window is 1.5x the recent
  // session range around spot, which is generous without being everything.
  const pdhLinesRef = useRef<IPriceLine[]>([]);
  useEffect(() => {
    pdhLinesRef.current.forEach((l) => { try { series.current?.removePriceLine(l); } catch { /* gone */ } });
    pdhLinesRef.current = [];
    if (reviewing || !showRbGannPdh || !series.current || !gannData?.ok) return;

    const ref = spot ?? candles[candles.length - 1]?.close;
    if (!ref) return;
    const recent = candles.slice(-125);
    const dayRange = recent.length
      ? Math.max(...recent.map((c) => c.high)) - Math.min(...recent.map((c) => c.low))
      : 300;
    const reach = Math.max(120, dayRange * 1.5);

    const draw = (lv: { level: string; degrees: number; price: number | null }, up: boolean) => {
      if (!lv.price || lv.price <= 0) return;
      if (Math.abs(lv.price - ref) > reach) return;      // out of plausible reach today
      const line = series.current!.createPriceLine({
        price: lv.price,
        color: up ? "#e879f9" : "#a78bfa",
        lineWidth: 1,
        lineStyle: LineStyle.Dashed,
        axisLabelVisible: true,
        title: `PDH ${up ? "R" : "S"} ${lv.degrees > 0 ? "+" : ""}${lv.degrees}° · context`,
      });
      pdhLinesRef.current.push(line);
    };
    (gannData.gann_pdh_resistances || []).forEach((l) => draw(l, true));
    (gannData.gann_pdh_supports || []).forEach((l) => draw(l, false));
  }, [reviewing, showRbGannPdh, gannData, spot, candles, series.current]);

  // The depth band. Zones shallower than this are not levels — the median 1H upper wick is only
  // 13 pts and a 3-pt wick is noise. Zones deeper than the cap are not levels either: they are
  // neighbourhoods that almost anything touches, and the uncapped test read +1.9 sd against +2.2
  // sd once capped. 40 pts is roughly NIFTY's 90th percentile of upper-wick depth.
  const WICK_MIN_PTS = 3;
  const WICK_MAX_PTS = 40;
  const wickTestedLinesRef = useRef<IPriceLine[]>([]);
  useEffect(() => {
    wickTestedLinesRef.current.forEach((l) => { try { series.current?.removePriceLine(l); } catch { /* gone */ } });
    wickTestedLinesRef.current = [];
    if (reviewing || !showRbWickTested || !series.current) return;

    const zones = (htfWickEmaData?.active_levels || []).filter(
      (lv) =>
        lv.tf === "1H" &&
        lv.side === "RESISTANCE" &&
        !lv.is_shaved &&
        lv.wick_pts >= WICK_MIN_PTS &&
        lv.wick_pts <= WICK_MAX_PTS &&
        lv.touch_count === 0 &&
        lv.top > 0 &&
        lv.bottom > 0,
    );

    zones.forEach((lv) => {
      // Both edges, so the zone reads as a band rather than a single price. Solid on the high
      // (the rejection extreme), dotted on the body edge where price first enters it.
      [
        { price: lv.top, style: LineStyle.Solid, title: `1H wick ${lv.wick_pts.toFixed(0)}p · tested` },
        { price: lv.bottom, style: LineStyle.Dotted, title: "" },
      ].forEach(({ price, style, title }) => {
        const line = series.current!.createPriceLine({
          price,
          color: "#fb923c",
          lineWidth: 1,
          lineStyle: style,
          axisLabelVisible: title !== "",
          title,
        });
        wickTestedLinesRef.current.push(line);
      });
    });
  }, [reviewing, showRbWickTested, htfWickEmaData, series.current]);
  const isDeltaDetailOn = !!layers.deltaDetail;
  const isDeltaOn = !!(layers.delta || isDeltaDetailOn);
  const isBubblesOn = !!(layers.bubbles || isDeltaDetailOn);
  const isCellsOn = !!(layers.cells || isDeltaDetailOn);
  const isCvdOn = !!(layers.cvd || isDeltaDetailOn);
  const isNodesOn = !!(layers.nodes || isDeltaDetailOn);

  const cvdDivergences = useMemo(() => computeCvdDivergences(fpBars), [fpBars]);
  const bubbles = useMemo(
    () => (isBubblesOn || isCvdOn || isCellsOn
      ? buildBubbles(analysis?.volume?.bars ?? [], fpBars, candles)
      : []),
    [isBubblesOn, isCvdOn, isCellsOn, analysis, fpBars, candles]
  );
  const [walls, setWalls] = useState<Wall[]>([]);
  const [wallLoadError, setWallLoadError] = useState("");
  const [wallLoading, setWallLoading] = useState(false);
  const [fitOiWalls, setFitOiWalls] = useState(true);
  // Compare expiry values, not the fresh array returned by dashboard polling.
  const wallCurExpiry = expiries?.[0];
  const wallNextExpiry = expiries?.[1];
  const [wallExp, setWallExp] = useState<"cur" | "next" | "both">("cur");
  const [bookEv, setBookEv] = useState<{ ts: number; price: number; side: string; kind: string; qty: number; why: string }[]>([]);
  const wallPrevRef = useRef<{ status: Record<string, Wall["status"]>; topCE: number | null; topPE: number | null } | null>(null);
  const onSignalRef = useRef(onSignal); onSignalRef.current = onSignal;
  const marketOpenRef = useRef(marketOpen); marketOpenRef.current = marketOpen;
  const sigOnRef = useRef(!!sig?.on); sigOnRef.current = !!sig?.on;
  const tfRef = useRef(tf); tfRef.current = tf;
  const ocFeed = useOcFeed(!!layers.bias || !!layers.ocAct || !!sig?.on || isCvdOn || isNodesOn);
  const cvdSignals=useMemo(()=>cvdExhaustionSignals(candles,fpBars,cvdDivergences,
    ocFeed?.nodes?.available?findEntries(candles,analysis?.volume?.vwap ?? [],ocFeed.nodes):[],BAR_SEC[tf] ?? 300),
    [candles,fpBars,cvdDivergences,ocFeed,analysis,tf]);
  const bankS = useRef<ISeriesApi<"Line"> | null>(null);
  const [bank, setBank] = useState<{ time: number; c: number }[]>([]);
  const [smtForming, setSmtForming] = useState<SmtForming[]>([]);
  const [writerBal, setWriterBal] = useState<{ pcr: number | null; ceOI: number; peOI: number; ceChg: number; peChg: number; net: number;
    maxPain: number | null; trend: "rising" | "falling" | "flat"; pcrPrev: number | null; spot: number | null } | null>(null);
  const netPrevRef = useRef<number | null>(null);
  const [oiLevels, setOiLevels] = useState<OiLevel[]>([]);
  const smtMarks = useSmt(tf, !!layers.smt || !!layers.bias || !!sig?.on, setBank, setSmtForming);
  const smtArg = tf === "5m" || tf === "15m" || tf === "1h" ? smtMarks : null;
  const biasMap = useRef<Map<number, BiasRow>>(new Map());
  const [biasPct,setBiasPct] = useBiasAgreement();
  const [biasStatus,setBiasStatus] = useState<BiasRow | null>(null);
  const ocLane = useRef<ISeriesApi<"Histogram"> | null>(null);
  const ocSlS = useRef<ISeriesApi<"Line"> | null>(null);

  // Synchronized 6-Stage PE−CE Lifecycle Rows from the revamped OcLab engine
  const smartRows = useMemo(() => {
    if (!ocFeed?.pece?.length) return [] as SmartRow[];
    return computeSmartRows(ocFeed.pece as Row[], "3m");
  }, [ocFeed?.pece]);

  // The day's decided direction, from the same cumulative Net PE−CE the desk bar reads (smartRows is
  // newest-first). Sent to the divergence endpoint so a counter-trend reversal watch is VETOED
  // server-side and never reaches the chart as a line.
  const divRegime = useMemo(
    () => dayRegime(smartRows[0]?.pe_ce_day_cr ?? null), [smartRows]);

  useEffect(() => {
    let alive = true;
    const go = () => fetch(`/api/desk/divergence?tf=${tf}&day_regime=${encodeURIComponent(divRegime)}`)
      .then((r) => r.json())
      .then((j) => { if (alive) setDivergence(j); })
      .catch(() => { /* read-only: the chart is unchanged without it */ });
    go();
    const t = setInterval(go, 15000);
    return () => { alive = false; clearInterval(t); };
  }, [tf, divRegime]);

  useEffect(() => {
    divLinesRef.current.forEach((l) => { try { series.current?.removePriceLine(l); } catch { /* gone */ } });
    divLinesRef.current = [];
    const a = divergence?.rsi?.active;
    if (!showDiv || !series.current || !a) return;
    // Second guard, independent of the regime sent above: a vetoed or counter-trend-reversal watch
    // never gets a line, whatever the endpoint returned. One of the two guards failing should not be
    // enough to put a short level on the chart in a bull day.
    if (a.vetoed || a.state === "VETOED") return;
    if (a.is_reversal && divRegime !== "none"
        && ((a.side === "short" && divRegime === "bull") || (a.side === "long" && divRegime === "bear"))) return;
    const short = a.side === "short";
    try {
      divLinesRef.current.push(series.current.createPriceLine({
        price: a.structure_trigger,
        color: short ? "#fb7185" : "#34d399",
        lineWidth: 1, lineStyle: LineStyle.Dashed, axisLabelVisible: true,
        title: `RSI ${short ? "▼" : "▲"} trigger`,
      }));
      divLinesRef.current.push(series.current.createPriceLine({
        price: a.invalidation,
        color: "#94a3b8",
        lineWidth: 1, lineStyle: LineStyle.Dotted, axisLabelVisible: true,
        title: "RSI invalid",
      }));
    } catch (e) {
      console.warn("Failed creating RSI divergence price lines", e);
    }
  }, [divergence, showDiv, divRegime]);

  // Synchronized PE−CE action mapped to chart candles
  const ocActs = useMemo(() => {
    if (!smartRows.length || !ocFeed?.day) return [] as {
      time: number;
      v: number;
      txt: string;
      smartRow: SmartRow;
      stage: number;
      kind: "ENTRY" | "PULLBACK" | "ADD" | "HOLD" | "BOOK" | "NONE";
    }[];

    const [y, m, d] = ocFeed.day.split("-").map(Number);
    const m2 = new Map<number, {
      v: number;
      txt: string;
      t: number;
      smartRow: SmartRow;
      stage: number;
      kind: "ENTRY" | "PULLBACK" | "ADD" | "HOLD" | "BOOK" | "NONE";
    }>();

    for (const r of smartRows) {
      const [hh, mm] = r.ts.split(":").map(Number);
      const t = Date.UTC(y, m - 1, d, hh, mm) / 1000;
      const b = bucket(t, tf);

      let v = 0;
      let kind: "ENTRY" | "PULLBACK" | "ADD" | "HOLD" | "BOOK" | "NONE" = "NONE";

      if (r.stage === 2) {
        kind = "PULLBACK";
        v = r.actionTone === "bear" ? -1 : r.actionTone === "bull" ? 1 : 0;
      } else if (r.stage === 1 && r.actionTone !== "warn") {
        kind = "ENTRY";
        v = r.actionTone === "bear" ? -1 : r.actionTone === "bull" ? 1 : 0;
      } else if (r.stage === 3) {
        if (r.actionTone === "warn") {
          kind = "NONE";
          v = 0;
        } else {
          kind = "ADD";
          v = r.actionTone === "bear" ? -1 : r.actionTone === "bull" ? 1 : 0;
        }
      } else if (r.stage === 5 || r.isShortCoveringReversal) {
        kind = "BOOK";
        v = 1;
      } else if (r.stage === 4) {
        kind = "HOLD";
        v = r.actionTone === "bear" ? -0.5 : 0.5;
      }

      const prev = m2.get(b);
      if (!prev || prev.t < t) {
        m2.set(b, {
          v,
          txt: r.actionText,
          t,
          smartRow: r,
          stage: r.stage,
          kind,
        });
      }
    }

    return [...m2.entries()]
      .sort((a2, b2) => a2[0] - b2[0])
      .map(([time, x]) => ({
        time,
        v: x.v,
        txt: x.txt,
        smartRow: x.smartRow,
        stage: x.stage,
        kind: x.kind,
      }));
  }, [smartRows, ocFeed?.day, tf]);

  // Real-time PE−CE A+ Alert Banner & Acoustic Synthesizer State
  const [ocAlertBanner, setOcAlertBanner] = useState<{
    text: string;
    kind: "bull" | "bear";
    ts: number;
  } | null>(null);

  const lastEvaluatedOcTsRef = useRef<string | null>(null);
  const ocInitialLoadRef = useRef<boolean>(true);

  // Live PE−CE Real-Time Audio Synthesizer & Visual Canvas Banner
  useEffect(() => {
    if (!smartRows.length) return;
    const latestRow = smartRows[0]; // Newest first
    if (!latestRow) return;

    if (ocInitialLoadRef.current) {
      ocInitialLoadRef.current = false;
      lastEvaluatedOcTsRef.current = latestRow.ts;
      return;
    }

    if (latestRow.ts !== lastEvaluatedOcTsRef.current) {
      lastEvaluatedOcTsRef.current = latestRow.ts;

      const isSoundOn = (() => {
        try { return localStorage.getItem("oi_beep_alerts") !== "0"; } catch { return true; }
      })();

      // A+ Setup: Stage 2 Pullback Entry OR Floor/Ceiling Shift OR A+ Synergy
      if (latestRow.stage === 2 || latestRow.isWallShiftBear || latestRow.isWallShiftBull || latestRow.isSynergy) {
        const isBear = latestRow.actionTone === "bear" || latestRow.isWallShiftBear;
        const bannerMsg = isBear
          ? `🎯 A+ PULLBACK PE ENTRY TRIGGERED @ ${Math.round(latestRow.spot)} · SL ${latestRow.dynamicSl ?? Math.round(latestRow.swingHigh30m + 15)} (Tgt ${Math.round(latestRow.swingLow30m - 30)})`
          : `🎯 A+ DIP CE ENTRY TRIGGERED @ ${Math.round(latestRow.spot)} · SL ${latestRow.dynamicSl ?? Math.round(latestRow.swingLow30m - 15)} (Tgt ${Math.round(latestRow.swingHigh30m + 30)})`;

        setOcAlertBanner({
          text: bannerMsg,
          kind: isBear ? "bear" : "bull",
          ts: Date.now(),
        });

        if (isSoundOn) {
          playOiBeep(isBear ? "angleBear" : "angleBull");
        }
      } else if (latestRow.stage === 5 || latestRow.isShortCoveringReversal) {
        setOcAlertBanner({
          text: `💰 SHORT-COVERING REVERSAL ACTIVE @ ${Math.round(latestRow.spot)} — BOOK PE / SCALP CE`,
          kind: "bull",
          ts: Date.now(),
        });
        if (isSoundOn) {
          playOiBeep("scissor");
        }
      }
    }
  }, [smartRows]);

  // Auto-dismiss banner after 15 seconds
  useEffect(() => {
    if (!ocAlertBanner) return;
    const t = setTimeout(() => setOcAlertBanner(null), 15000);
    return () => clearTimeout(t);
  }, [ocAlertBanner]);

  // ── Sniper Trinity: Unified OI Walls + CVD Order Flow + Writer Lifecycle Confluence Engine ──
  const confluenceTrinity = useMemo(() => {
    const curSpot = spot ?? (candles.length ? candles[candles.length - 1].close : null);
    if (curSpot == null || !walls.length) return null;

    const latestSmart = smartRows.length > 0 ? smartRows[0] : null;

    // 1. OI Wall Proximity Check (±30 pts)
    const ceWalls = walls.filter((w) => w.side === "CE");
    const peWalls = walls.filter((w) => w.side === "PE");

    const nearestCe = ceWalls.sort((a, b) => Math.abs(a.p - curSpot) - Math.abs(b.p - curSpot))[0] ?? null;
    const ceDist = nearestCe ? Math.abs(nearestCe.p - curSpot) : 999;
    const isNearCe = nearestCe != null && ceDist <= 30;

    const nearestPe = peWalls.sort((a, b) => Math.abs(a.p - curSpot) - Math.abs(b.p - curSpot))[0] ?? null;
    const peDist = nearestPe ? Math.abs(nearestPe.p - curSpot) : 999;
    const isNearPe = nearestPe != null && peDist <= 30;

    // 2. CVD Order Flow & Divergence Check (10-bar swing pivots & 3-bar momentum)
    const latestFp = fpBars.length > 0 ? fpBars[fpBars.length - 1] : null;
    const prevFp3 = fpBars.length >= 4 ? fpBars[fpBars.length - 4] : null;
    const dCvd3 = (latestFp && prevFp3) ? (latestFp.cvd - prevFp3.cvd) : 0;
    const recentCvdDiv = cvdDivergences.slice(-4).reverse().find((d) => latestFp && Math.abs(d.time - latestFp.time) <= 900) ?? null;

    const isCvdBear = (
      recentCvdDiv?.type === "BEARISH_DISTRIBUTION" ||
      (latestFp != null && latestFp.cvd >= 150000 && latestFp.delta < 0) ||
      dCvd3 < -15000 ||
      (latestFp != null && latestFp.delta < -10000)
    );

    const isCvdBull = (
      recentCvdDiv?.type === "BULLISH_ABSORPTION" ||
      (latestFp != null && latestFp.cvd <= -150000 && latestFp.delta > 0) ||
      dCvd3 > 15000 ||
      (latestFp != null && latestFp.delta > 10000)
    );

    const cvdSummary = (() => {
      if (recentCvdDiv?.type === "BEARISH_DISTRIBUTION") return `🔴 CVD Distribution (${kfmt(latestFp?.cvd ?? 0)})`;
      if (recentCvdDiv?.type === "BULLISH_ABSORPTION") return `🟢 CVD Absorption (${kfmt(latestFp?.cvd ?? 0)})`;
      if (latestFp && Math.abs(latestFp.cvd) >= 150000) return `⚡ CVD Climax (${kfmt(latestFp.cvd)})`;
      if (dCvd3 < -15000) return `🔻 CVD Dump (${kfmt(dCvd3)}/3b)`;
      if (dCvd3 > 15000) return `⚡ CVD Surge (+${kfmt(dCvd3)}/3b)`;
      return latestFp ? `〽️ CVD ${kfmt(latestFp.cvd)}` : "CVD Inactive";
    })();

    // 3. Writer Flow Velocity & Table Lifecycle State (θ >= 25°, Stage 2 Pullback, Scissor)
    const callAngle = latestSmart?.callAngleDeg ?? 0;
    const putAngle = latestSmart?.putAngleDeg ?? 0;
    const isCallSurging = callAngle >= 25;
    const isPutDefending = putAngle >= 25;
    const isScissor = latestSmart?.isScissor ?? false;

    const stage = latestSmart?.stage ?? 0;
    const actionTone = latestSmart?.actionTone ?? "flat";
    const isTableShort = actionTone === "bear" && (stage === 1 || stage === 2 || stage === 3 || !!latestSmart?.isWallShiftBear);
    const isTableLong = actionTone === "bull" && (stage === 1 || stage === 2 || stage === 3 || !!latestSmart?.isWallShiftBull);

    const isWriterBear = (isCallSurging || isScissor || !!latestSmart?.isWallShiftBear) && isTableShort;
    const isWriterBull = (isPutDefending || (isScissor && callAngle < -15) || !!latestSmart?.isWallShiftBull) && isTableLong;

    // 3-Pillar Confluence Matrix Evaluation
    let ceScore = 0;
    if (isNearCe) ceScore += 1;
    if (isCvdBear) ceScore += 1;
    if (isWriterBear) ceScore += 1;

    let peScore = 0;
    if (isNearPe) peScore += 1;
    if (isCvdBull) peScore += 1;
    if (isWriterBull) peScore += 1;

    let trinityType: "SHORT_CE_WALL" | "BUY_PE_SUPPORT" | "MONITORING" = "MONITORING";
    if (isNearCe && isCvdBear && isWriterBear) {
      trinityType = "SHORT_CE_WALL";
    } else if (isNearPe && isCvdBull && isWriterBull) {
      trinityType = "BUY_PE_SUPPORT";
    }

    return {
      curSpot,
      nearestCe,
      ceDist,
      isNearCe,
      nearestPe,
      peDist,
      isNearPe,
      isCvdBear,
      isCvdBull,
      cvdSummary,
      callAngle,
      putAngle,
      isCallSurging,
      isPutDefending,
      isScissor,
      stage,
      stageBadge: latestSmart?.stageBadge ?? "WAIT",
      actionText: latestSmart?.actionText ?? "Wait for setup",
      actionTone,
      dynamicSl: latestSmart?.dynamicSl,
      trinityType,
      confluenceScore: Math.max(ceScore, peScore),
    };
  }, [spot, candles, walls, smartRows, fpBars, cvdDivergences]);

  const lastTrinityTypeRef = useRef<string>("MONITORING");

  useEffect(() => {
    if (!confluenceTrinity) return;
    if (confluenceTrinity.trinityType !== lastTrinityTypeRef.current) {
      lastTrinityTypeRef.current = confluenceTrinity.trinityType;

      const isSoundOn = (() => {
        try { return localStorage.getItem("oi_beep_alerts") !== "0"; } catch { return true; }
      })();

      if (confluenceTrinity.trinityType === "SHORT_CE_WALL") {
        setOcAlertBanner({
          text: `🎯 A+ SNIPER CONFLUENCE SIGNAL: SHORT CE WALL (CVD DISTRIBUTION + WRITER ACCELERATION)`,
          kind: "bear",
          ts: Date.now(),
        });
        if (isSoundOn) playOiBeep("angleBear");
      } else if (confluenceTrinity.trinityType === "BUY_PE_SUPPORT") {
        setOcAlertBanner({
          text: `🎯 A+ SNIPER CONFLUENCE SIGNAL: BUY PE SUPPORT (CVD ABSORPTION + WRITER DEFENSE)`,
          kind: "bull",
          ts: Date.now(),
        });
        if (isSoundOn) playOiBeep("angleBull");
      }
    }
  }, [confluenceTrinity]);
  const dolRef = useRef<ReturnType<typeof computeDol>>([]);
  const [, setFpTick] = useState(0);
  const [heatHelp, setHeatHelp] = useState(() => { try { return localStorage.getItem("ict_heat_help") !== "0"; } catch { return true; } });
  const volS = useRef<ISeriesApi<"Histogram"> | null>(null);
  const vwapS = useRef<ISeriesApi<"Line">[]>([]);
  const ivS = useRef<ISeriesApi<"Line"> | null>(null);
  const rvS = useRef<{ h: ISeriesApi<"Histogram">; l: ISeriesApi<"Line"> } | null>(null);
  const [oiMode, setOiMode] = useState<"raw" | "relative">(() => {
    try { return localStorage.getItem("ict_oi_mode") === "relative" ? "relative" : "raw"; } catch { return "raw"; }
  });
  const [oiClock, setOiClock] = useState(Date.now());
  useEffect(() => {
    if (!layers.oi) return;
    const timer = window.setInterval(() => setOiClock(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [layers.oi]);
  const chooseOiMode = (mode: "raw" | "relative") => {
    setOiMode(mode);
    try { localStorage.setItem("ict_oi_mode", mode); } catch { /* storage optional */ }
  };
  const oiS = useRef<{ doi: ISeriesApi<"Histogram"> } | null>(null);          // [vwap, +1σ, −1σ, +2σ, −2σ, avwap1, avwap2]
  const last = useRef<Candle | null>(null);
  const evRef = useRef<Map<number, Ev[]>>(new Map());
  const cMap = useRef<Map<number, Candle>>(new Map());
  const [hover, setHover] = useState<{ t: number; x: number; y: number; pane?: number } | null>(null);
  const [sniperHover, setSniperHover] = useState<{ item: SniperHoverItem; x: number; y: number } | null>(null);
  const [gtfHover, setGtfHover] = useState<{ zone: GtfZone; x: number; y: number } | null>(null);
  const onCrosshairMoveRef = useRef<(p: MouseEventParams<Time>) => void>(() => {});
  const [latest, setLatest] = useState<Ev[]>([]);
  const [layerNow, setLayerNow] = useState<Record<string, Ev>>({});
  // ⚡ 3-Chart Institutional PCR & OI Trinity Suite Drawer State
  const [trinityDrawerOpen, setTrinityDrawerOpen] = useState<boolean>(false);
  const [trinityDrawerHeight, setTrinityDrawerHeight] = useState<number>(440);
  const [oiDrawerTab, setOiDrawerTab] = useState<"trinity" | "matrix">("trinity");
  const trinityVerdict = (analysis as any)?.pcr_trinity;
  const trinityState = trinityVerdict?.alignment_state;
  const trinityBadge = trinityVerdict?.verdict_badge;
  const [guideOpen, setGuideOpen] = useState<boolean>(() => { try { return localStorage.getItem("ict_guide") !== "0"; } catch { return true; } });
  const isEodCutoff = useMemo(() => {
    const lc = candles[candles.length - 1];
    if (!lc) return false;
    const dt = new Date(lc.time * 1000);
    const utcMins = dt.getUTCHours() * 60 + dt.getUTCMinutes();
    const istMins = (utcMins + 330) % 1440;
    return istMins >= 910; // 15:10 IST (15*60 + 10 = 910)
  }, [candles]);
  const [htfWicks, setHtfWicks] = useState<HtfWick[]>([]);
  const [advPaSniperMode, setAdvPaSniperMode] = useState<boolean>(() => {
    try {
      return localStorage.getItem("ict_adv_pa_mode") !== "raw";
    } catch {
      return true;
    }
  });
  const toggleAdvPaSniperMode = () => {
    setAdvPaSniperMode((prev) => {
      const next = !prev;
      try {
        localStorage.setItem("ict_adv_pa_mode", next ? "sniper" : "raw");
      } catch { /* ignore */ }
      return next;
    });
  };

  // Prahaar Indicator & ATM Option-Buying Directional Engine State
  const [prahaarActive, setPrahaarActive] = useState<boolean>(() => {
    try {
      return localStorage.getItem("ict_prahaar_active") === "1";
    } catch {
      return false;
    }
  });
  const isPrahaarOn = layers.prahaar ?? prahaarActive;
  const togglePrahaar = () => {
    setPrahaarActive((prev) => {
      const next = !prev;
      try {
        localStorage.setItem("ict_prahaar_active", next ? "1" : "0");
      } catch { /* ignore */ }
      return next;
    });
  };

  // Institutional Time-of-Day RVOL & Breakout Authenticity Metrics
  const [rvolPaneMode, setRvolPaneMode] = useState<"slot" | "cumul">(() => {
    try {
      return (localStorage.getItem("ict_rvol_pane_mode") as "slot" | "cumul") || "slot";
    } catch {
      return "slot";
    }
  });
  const setAndSaveRvolPaneMode = (mode: "slot" | "cumul") => {
    setRvolPaneMode(mode);
    try {
      localStorage.setItem("ict_rvol_pane_mode", mode);
    } catch { /* ignore */ }
  };

  const unifiedSlotMap = useMemo(() => {
    const map = new Map<number, number>();
    const vbars = analysis?.volume?.bars ?? [];
    const crvolBars = analysis?.volume?.crvol ?? [];
    for (const b of vbars) {
      const v = b.slot_rvol ?? b.rvol;
      if (v != null) map.set(b.time, v);
    }
    for (const c of crvolBars) {
      const v = c.slot_rvol;
      if (v != null) map.set(c.time, v);
    }
    for (const e of (analysis?.volume?.rvol_events ?? [])) {
      if (e.kind === "RVOL_SPIKE" && e.value != null) {
        const rawT = (e as any).time ?? (e as any).ts;
        if (rawT != null) {
          const bt = bucket(rawT, tf);
          map.set(bt, Math.max(map.get(bt) ?? 0, e.value));
          map.set(rawT, Math.max(map.get(rawT) ?? 0, e.value));
        }
      }
    }
    return map;
  }, [analysis?.volume, tf]);

  const latestVolBar = analysis?.volume?.bars?.length
    ? analysis.volume.bars[analysis.volume.bars.length - 1]
    : undefined;
  const lastBarTs = candles.length ? candles[candles.length - 1].time : 0;
  const curSlotRvol = (lastBarTs ? (unifiedSlotMap.get(lastBarTs) ?? unifiedSlotMap.get(bucket(lastBarTs, tf))) : null) ?? latestVolBar?.slot_rvol ?? latestVolBar?.rvol ?? 1.0;
  const isWeakRvol = curSlotRvol < 0.85;
  const isHighRvolConfirmed = curSlotRvol >= 2.0;
  const hasBigTradesNear = bubbles.some((b) => Math.abs(b.time - lastBarTs) <= 600);
  const rvolConfirmedSuffix = isHighRvolConfirmed && hasBigTradesNear
    ? ` · ⚡ ${curSlotRvol.toFixed(1)}x RVOL CONFIRMED (REAL MOVE)`
    : "";

  const prahaarResult = useMemo(() => {
    const curVix = analysis?.prahaar?.latest_state?.vix ?? null;
    return computePrahaar(candles, curVix, null, walls, cvdDivergences, fpBars);
  }, [candles, walls, cvdDivergences, fpBars, analysis?.prahaar]);

  // Confluence Matrix Synchronization: Syndicate Prahaar with CVD Divergence and OI Walls
  const lastPrahaarMasterTsRef = useRef<number>(0);
  useEffect(() => {
    if (!isPrahaarOn || !prahaarResult.latestState) return;
    const state = prahaarResult.latestState;
    const isSoundOn = (() => {
      try { return localStorage.getItem("oi_beep_alerts") !== "0"; } catch { return true; }
    })();

    const lastCandleTs = candles.length ? candles[candles.length - 1].time : 0;
    if (state.isMasterSniperPut && lastPrahaarMasterTsRef.current !== lastCandleTs) {
      lastPrahaarMasterTsRef.current = lastCandleTs;
      setOcAlertBanner({
        text: `🎯 A+ MASTER SNIPER SIGNAL: BUY ${state.atmStrike} PE — PRAHAAR + CE RESISTANCE WALL (${confluenceTrinity?.nearestCe?.p ?? "WALL"}) + BEARISH CVD DIVERGENCE${rvolConfirmedSuffix}`,
        kind: "bear",
        ts: Date.now(),
      });
      if (isSoundOn) playOiBeep("angleBear");
    } else if (state.isMasterSniperCall && lastPrahaarMasterTsRef.current !== lastCandleTs) {
      lastPrahaarMasterTsRef.current = lastCandleTs;
      setOcAlertBanner({
        text: `🎯 A+ MASTER SNIPER SIGNAL: BUY ${state.atmStrike} CE — PRAHAAR + PE SUPPORT WALL (${confluenceTrinity?.nearestPe?.p ?? "WALL"}) + BULLISH CVD ABSORPTION${rvolConfirmedSuffix}`,
        kind: "bull",
        ts: Date.now(),
      });
      if (isSoundOn) playOiBeep("angleBull");
    }
  }, [isPrahaarOn, prahaarResult, candles, confluenceTrinity, rvolConfirmedSuffix]);

  // 🏛️ GTF Institutional Demand & Supply Engine (Version 2.0 Equivalent)
  const [gtfActive, setGtfActive] = useState<boolean>(() => {
    try {
      return localStorage.getItem("ict_gtf_active") === "1";
    } catch {
      return false;
    }
  });
  const [gtfMode, setGtfMode] = useState<GtfMarkingMode>(() => {
    try {
      const saved = localStorage.getItem("ict_gtf_mode");
      if (saved === "conservative" || saved === "aggressive" || saved === "dynamic") return saved;
    } catch { /* ignore */ }
    return "conservative";
  });
  const isGtfOn = layers.gtfZones ?? gtfActive;
  const toggleGtf = () => {
    setGtfActive((prev) => {
      const next = !prev;
      try {
        localStorage.setItem("ict_gtf_active", next ? "1" : "0");
      } catch { /* ignore */ }
      return next;
    });
  };
  const setAndSaveGtfMode = (mode: GtfMarkingMode) => {
    setGtfMode(mode);
    try {
      localStorage.setItem("ict_gtf_mode", mode);
    } catch { /* ignore */ }
  };

  const gtfResult = useMemo(() => {
    return detectGtfZones(candles, gtfMode);
  }, [candles, gtfMode]);

  // Master Confluence Synchronization: GTF Zones + OI Walls + CVD Divergence + PE-CE Table
  const lastGtfMasterTsRef = useRef<number>(0);
  useEffect(() => {
    if (!isGtfOn || !candles.length) return;
    const curPrice = spot ?? (candles.length ? candles[candles.length - 1].close : 0);
    const conf = evaluateGtfMasterConfluence(curPrice, gtfResult, walls, cvdDivergences, fpBars, smartRows);
    const lastCandleTs = candles.length ? candles[candles.length - 1].time : 0;
    const isSoundOn = (() => {
      try { return localStorage.getItem("oi_beep_alerts") !== "0"; } catch { return true; }
    })();

    if (conf.isDemandMaster && lastGtfMasterTsRef.current !== lastCandleTs) {
      lastGtfMasterTsRef.current = lastCandleTs;
      setOcAlertBanner({
        text: `${conf.bannerText}${rvolConfirmedSuffix}`,
        kind: "bull",
        ts: Date.now(),
      });
      if (isSoundOn) playOiBeep("angleBull");
    } else if (conf.isSupplyMaster && lastGtfMasterTsRef.current !== lastCandleTs) {
      lastGtfMasterTsRef.current = lastCandleTs;
      setOcAlertBanner({
        text: `${conf.bannerText}${rvolConfirmedSuffix}`,
        kind: "bear",
        ts: Date.now(),
      });
      if (isSoundOn) playOiBeep("angleBear");
    }
  }, [isGtfOn, candles, spot, gtfResult, walls, cvdDivergences, fpBars, smartRows, rvolConfirmedSuffix]);

  // Load Higher Timeframe (HTF) wicks: 1H (Hourly 1×), 1D (Daily 2×), 1W (Weekly 3×)
  useEffect(() => {
    let dead = false;
    const fetchHtf = async () => {
      try {
        const [c1h, c1d] = await Promise.all([
          api.candles("1h", 10, "NIFTY"),
          api.candles("1D", 45, "NIFTY"),
        ]);
        if (dead) return;
        const w1h = extractWicks(c1h.candles || [], "1H");
        const dailyCandles = c1d.candles || [];
        const w1d = extractWicks(dailyCandles, "1D");
        const weeklyCandles = aggregateWeeklyCandles(dailyCandles);
        const w1w = extractWicks(weeklyCandles, "1W");
        const combined = [...w1h, ...w1d, ...w1w];
        setHtfWicks(filterVirginWicks(combined, candles));
      } catch (err) {
        console.error("HTF wicks load error:", err);
      }
    };
    fetchHtf();
    const timer = setInterval(fetchHtf, 60000);
    return () => { dead = true; clearInterval(timer); };
  }, [candles]);

  // create once
  useEffect(() => {
    if (!box.current) return;
    const ch = createChart(box.current, {
      autoSize: true,
      layout: { background: { type: ColorType.Solid, color: "#0b1220" }, textColor: C.text, fontFamily: "Inter, system-ui" },
      grid: { vertLines: { color: C.grid }, horzLines: { color: C.grid } },
      crosshair: { mode: CrosshairMode.Normal },
      rightPriceScale: { borderColor: "#1d2a45" },
      timeScale: { borderColor: "#1d2a45", timeVisible: true, secondsVisible: false, rightOffset: 14, barSpacing: 7, minBarSpacing: 2 },
      localization: { locale: "en-IN", priceFormatter: (p: number) => p.toLocaleString("en-IN", { minimumFractionDigits: 1, maximumFractionDigits: 1 }) },
    });
    const s = ch.addSeries(CandlestickSeries, {
      upColor: C.bull, downColor: C.bear, borderUpColor: C.bull, borderDownColor: C.bear, wickUpColor: C.bull, wickDownColor: C.bear,
      priceLineColor: C.gold,
    });
    s.attachPrimitive(zones.current);
    s.attachPrimitive(fpPrim.current);
    s.attachPrimitive(ofPrim.current);
    s.attachPrimitive(ndPrim.current);
    s.attachPrimitive(gmPrim.current);
    s.attachPrimitive(stdvPrim.current);
    bankS.current = ch.addSeries(LineSeries, { color: "rgba(244,114,182,0.85)", lineWidth: 2, priceScaleId: "bank", title: "BANKNIFTY",
      priceLineVisible: false, lastValueVisible: true, crosshairMarkerVisible: false });
    ch.priceScale("bank").applyOptions({ visible: false, scaleMargins: { top: 0.1, bottom: 0.25 } });
    const z = ch.addSeries(LineSeries, { color: "rgba(232,195,90,0.55)", lineWidth: 1, lineStyle: LineStyle.Solid,
      priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false });
    // real volume (NIFTY FUT) in its own strip at the bottom; VWAP + σ bands + anchored VWAPs on the price scale
    const vs = ch.addSeries(HistogramSeries, { priceScaleId: "vol", priceFormat: { type: "volume" }, priceLineVisible: false, lastValueVisible: false });
    ch.priceScale("vol").applyOptions({ scaleMargins: { top: 0.84, bottom: 0 } });
    const line = (color: string, width: 1 | 2, style: LineStyle) => ch.addSeries(LineSeries, { color, lineWidth: width, lineStyle: style,
      priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false });
    // VWAP restarts each session; two line sets (alternate days) so no line bridges the overnight gap
    const set5 = () => [line("#f5f5f4", 2, LineStyle.Solid),
      line("rgba(245,245,244,0.45)", 1, LineStyle.Dashed), line("rgba(245,245,244,0.45)", 1, LineStyle.Dashed),
      line("rgba(245,245,244,0.25)", 1, LineStyle.Dotted), line("rgba(245,245,244,0.25)", 1, LineStyle.Dotted)];
    vwapS.current = [...set5(), line("#c084fc", 2, LineStyle.Solid), line("#fbbf24", 2, LineStyle.Solid), ...set5()];
    volS.current = vs;
    // ATM IV (live only) on its own scale, in a band just above the volume strip
    const iv = ch.addSeries(LineSeries, { color: "#fb923c", lineWidth: 1, priceScaleId: "iv", priceLineVisible: false,
      lastValueVisible: true, crosshairMarkerVisible: false, title: "ATM IV" });
    ch.priceScale("iv").applyOptions({ scaleMargins: { top: 0.66, bottom: 0.18 }, visible: false });
    ivS.current = iv;
    spine.current = ch.addSeries(LineSeries, { color: "rgba(34,211,238,0.75)", lineWidth: 1, priceLineVisible: false,
      lastValueVisible: false, crosshairMarkerVisible: false });
    ocSlS.current = ch.addSeries(LineSeries, {
      color: "rgba(251, 113, 133, 0.8)",
      lineWidth: 1,
      lineStyle: LineStyle.Dashed,
      priceLineVisible: false,
      lastValueVisible: true,
      crosshairMarkerVisible: false,
      title: "PE−CE SL",
    });
    chart.current = ch; series.current = s; zz.current = z;
    markers.current = createSeriesMarkers(s, []);
    const onMove = (p: MouseEventParams<Time>) => {
      if (!p.point || p.point.x < 0) {
        setHover(null);
        setSniperHover(null);
        setGtfHover(null);
        return;
      }
      if (p.time != null) {
        const pane = p.paneIndex ?? 0;
        const offset = ch.panes().slice(0, pane).reduce((height, item) => height + item.getHeight(), 0);
        setHover({ t: p.time as number, x: p.point.x, y: p.point.y + offset, pane });
      } else {
        setHover(null);
      }
      onCrosshairMoveRef.current(p);
    };
    ch.subscribeCrosshairMove(onMove);
    ch.applyOptions({ handleScale: { axisPressedMouseMove: { time: true, price: true }, axisDoubleClickReset: { time: true, price: true },
      mouseWheel: true, pinch: true }, handleScroll: { mouseWheel: true, pressedMouseMove: true, horzTouchDrag: true, vertTouchDrag: true } });
    // wheel over the RIGHT PRICE AXIS → zoom the price range around the cursor (like TradingView);
    // wheel anywhere else → normal time zoom. Double-click the axis to go back to auto-scale.
    const el = box.current;
    const onWheel = (ev: WheelEvent) => {
      const ps = s.priceScale();
      const r = el.getBoundingClientRect();
      if (ev.clientX < r.right - ps.width()) return;
      ev.preventDefault(); ev.stopPropagation();
      const vr = ps.getVisibleRange();
      const at = s.coordinateToPrice(ev.clientY - r.top);
      if (!vr || at == null) return;
      const k = ev.deltaY > 0 ? 1.12 : 1 / 1.12;
      ps.setAutoScale(false);
      ps.setVisibleRange({ from: at - (at - vr.from) * k, to: at + (vr.to - at) * k });
    };
    el.addEventListener("wheel", onWheel, { passive: false, capture: true });
    return () => {
      ch.unsubscribeCrosshairMove(onMove);
      el.removeEventListener("wheel", onWheel, { capture: true } as EventListenerOptions); ch.remove(); chart.current = null; series.current = null; };
  }, []);

  // candles
  useEffect(() => {
    if (!series.current) return;
    series.current.setData(candles.map((c) => ({ time: c.time as UTCTimestamp, open: c.open, high: c.high, low: c.low, close: c.close })));
    last.current = candles.length ? { ...candles[candles.length - 1] } : null;
    cMap.current = new Map(candles.map((c) => [c.time, c]));
  }, [candles]);

  // fit when the timeframe changes
  useEffect(() => {
    const ch = chart.current;
    if (!ch || !candles.length) return;
    const n = candles.length;
    const bars = Math.max(40, Math.min(160, Math.round((box.current?.clientWidth ?? 900) / 7)));  // ~7 px per candle
    ch.timeScale().setVisibleLogicalRange({ from: Math.max(0, n - bars), to: n + 14 });
  }, [tf, candles.length > 0]);   // only when the timeframe changes or the first data arrives

  // live tick → update the forming candle
  useEffect(() => {
    const s = series.current;
    // after the close Dhan can keep sending a static LTP — never grow a phantom candle outside market hours
    if (!s || spot == null || !last.current || !marketOpen) return;
    const t = bucket(spotTs + 19800, tf);
    const lc = last.current;
    if (t < lc.time) return;
    if (t === lc.time) {
      lc.high = Math.max(lc.high, spot); lc.low = Math.min(lc.low, spot); lc.close = spot;
    } else {
      last.current = { time: t, open: spot, high: spot, low: spot, close: spot, closed: false };
    }
    const c = last.current;
    s.update({ time: c.time as UTCTimestamp, open: c.open, high: c.high, low: c.low, close: c.close });
  }, [spot, spotTs, tf, marketOpen]);

  // overlays
  useEffect(() => {
    const s = series.current;
    if (!s || !markers.current || !zz.current) return;
    const a = analysis;
    const boxes: Box[] = [];
    const segs: Seg[] = [];
    const lastTime = candles.length ? candles[candles.length - 1].time : 0;
    if (a) {
      if (layers.range && a.range) {
        const r = a.range;
        const [lo, hi] = r.ote_band;
        boxes.push({ from: r.from, to: r.to, top: r.high, bottom: r.eq, fill: "rgba(239,68,68,0.04)" });
        boxes.push({ from: r.from, to: r.to, top: r.eq, bottom: r.low, fill: "rgba(16,185,129,0.04)" });
        boxes.push({ from: r.from, to: r.to, top: hi, bottom: lo, fill: "rgba(232,195,90,0.10)", stroke: "rgba(232,195,90,0.7)", dash: true,
          label: `OTE 61.8–79 % (${r.dir === "bull" ? "buy" : "sell"} zone)`, labelColor: C.gold });
        segs.push({ from: r.from, price: r.eq, color: "rgba(232,195,90,0.8)", label: `EQ 50 % ${r.eq.toFixed(1)}`, dash: true });
      }
      for (const z of a.zones) {
        const isDead = z.state === "invalid" || z.state === "mitigated" || z.state === "broken" || z.state === "expired";
        const zoneTo = isDead ? z.to : lastTime;
        if (z.type === "RJB") {                      // rejection block: long-wick part of a swing candle (violet)
          if (!(layers.ob || layers.rblock)) continue;   // its own toggle, or shown with Order blocks
          const sell = z.dir === "bear";
          boxes.push({ from: z.from, to: zoneTo, top: z.top, bottom: z.bottom,
            fill: sell ? "rgba(192,132,252,0.13)" : "rgba(129,140,248,0.13)", stroke: sell ? "rgba(192,132,252,0.8)" : "rgba(129,140,248,0.8)",
            dash: true, label: z.label, labelColor: sell ? "#d8b4fe" : "#a5b4fc" });
          if (layers.rblock) {                            // the ICT 50 % of the rejection block — the key entry level
            const mid = (z.top + z.bottom) / 2;
            segs.push({ from: z.from, price: mid, color: sell ? "rgba(192,132,252,0.8)" : "rgba(129,140,248,0.8)", dash: true,
              label: `RB 50% ${mid.toFixed(1)}` });
          }
          continue;
        }
        if (z.type === "IFVG") {                     // inversion FVG: failed gap that flipped role (orange = sell zone, teal = buy zone)
          if (!layers.fvg) continue;
          const sell = z.dir === "bear";
          boxes.push({ from: z.from, to: zoneTo, top: z.top, bottom: z.bottom,
            fill: sell ? "rgba(251,146,60,0.15)" : "rgba(45,212,191,0.13)", stroke: sell ? "rgba(251,146,60,0.85)" : "rgba(45,212,191,0.85)",
            dash: true, label: z.label, labelColor: sell ? "#fdba74" : "#5eead4" });
          continue;
        }
        const isOB = z.type !== "FVG";
        if (isOB && !layers.ob) continue;
        if (!isOB && !layers.fvg) continue;
        const bull = z.dir === "bull";
        boxes.push({
          from: z.from, to: zoneTo, top: z.top, bottom: z.bottom,
          fill: isOB ? (bull ? C.obBull : C.obBear) : (bull ? C.fvgBull : C.fvgBear),
          stroke: isOB ? (bull ? "rgba(16,185,129,0.6)" : "rgba(239,68,68,0.6)") : undefined,
          dash: z.type === "BREAKER", label: z.label, labelColor: isOB ? (bull ? "#6ee7b7" : "#fca5a5") : (bull ? "#7dd3fc" : "#f9a8d4"),
        });
      }
      if (layers.liq) {
        for (const p of a.pools) {
          const strong = p.src !== "SWING";
          segs.push({ from: p.from, price: p.price, color: p.side === "BSL" ? "#f59e0b" : "#a78bfa", dash: !strong, width: strong ? 1.5 : 1,
            label: `${p.side} ${p.src}${p.members > 1 ? `×${p.members}` : ""} ${p.price.toFixed(1)}` });
        }
      }
      if (layers.session && a.session) {
        const S = a.session;
        const t0 = candles.find((c) => Math.floor(c.time / 86400) === Math.floor(lastTime / 86400))?.time ?? lastTime;
        if (S.prev) {
          segs.push({ from: t0, price: S.prev.h, color: "#38bdf8", label: `PDH ${S.prev.h.toFixed(1)}` });
          segs.push({ from: t0, price: S.prev.l, color: "#38bdf8", label: `PDL ${S.prev.l.toFixed(1)}` });
          segs.push({ from: t0, price: S.prev.c, color: "rgba(148,163,184,0.7)", label: `PDC ${S.prev.c.toFixed(1)}`, dash: true });
        }
        if (S.or) {
          boxes.push({ from: t0, to: lastTime, top: S.or.h, bottom: S.or.l, fill: "rgba(148,163,184,0.04)", stroke: "rgba(148,163,184,0.25)", dash: true, label: "Opening range 09:15–09:30" });
        }
      }
      // Power of Three (AMD): ① Accumulation (opening range) · ② Judas sweep (manipulation) · ③ Distribution bias
      if (layers.p3) {
        const p3 = powerOfThree(candles, a);
        if (p3) {
          boxes.push({ from: p3.orFrom, to: lastTime, top: p3.orHigh, bottom: p3.orLow, fill: "rgba(250,204,21,0.05)",
            stroke: "rgba(250,204,21,0.4)", dash: true, label: "① Accumulation (opening range)", labelColor: "#fde047" });
          segs.push({ from: p3.orFrom, price: p3.open, color: "rgba(226,232,240,0.6)", dash: true, label: `Day open ${p3.open.toFixed(1)}` });
          if (p3.judas) {
            segs.push({ from: p3.judas.time, price: p3.judas.price, color: "#f59e0b", width: 1.5,
              label: `② Judas ${p3.judas.dir === "up" ? "↑ fake-up" : "↓ fake-down"} ${p3.judas.price.toFixed(0)} → ③ Distribution ${p3.distribution === "bull" ? "UP (bullish)" : "DOWN (bearish)"}${p3.reclaimed ? " ✓ reclaimed" : " (awaiting reclaim)"}` });
          }
        }
      }
      // setup D — CRT range (CRT candle → end of the manipulation candle), its 50 % (TP-1) and the CRTH / CRTL edges
      if (layers.crt && a.crt) {
        for (const r of a.crt.ranges) {
          const bear = r.biases.includes("bear") && !r.biases.includes("bull");
          const bull = r.biases.includes("bull") && !r.biases.includes("bear");
          const edge = bear ? "rgba(239,68,68,0.75)" : bull ? "rgba(16,185,129,0.75)" : "rgba(232,195,90,0.75)";
          const kl = r.keylevels.map((k) => k.kind).join("+");
          boxes.push({ from: r.chart_from, to: r.chart_to, top: r.crth, bottom: r.crtl, fill: "rgba(232,195,90,0.05)", stroke: edge,
            label: `CRT ${r.htf} ${r.time}${r.aplus ? " A+" : ""} · ${kl} · ${r.status}${r.rvol != null ? ` · vol ${r.rvol.toFixed(1)}×` : ""}`, labelColor: C.gold });
          boxes.push({ from: r.chart_from, to: r.chart_to, top: r.mid, bottom: r.mid, fill: "rgba(232,195,90,0.9)", stroke: "rgba(232,195,90,0.9)", dash: true });
        }
      }
      // auction: previous-day value (VAH / VAL / POC, TPO time profile), today's developing POC, balance boxes
      if (layers.auction && a.auction) {
        const A = a.auction;
        const t0 = candles.find((c) => Math.floor(c.time / 86400) === Math.floor(lastTime / 86400))?.time ?? lastTime;
        if (A.prev) {
          boxes.push({ from: t0, to: lastTime, top: A.prev.vah, bottom: A.prev.val, fill: "rgba(56,189,248,0.04)", stroke: "rgba(56,189,248,0.35)", dash: true,
            label: `Prev value (${(A.prev as { kind?: string }).kind === "volume" ? "volume" : "TPO"}) ${A.prev.val.toFixed(0)}–${A.prev.vah.toFixed(0)}`, labelColor: "#7dd3fc" });
          segs.push({ from: t0, price: A.prev.poc, color: "rgba(56,189,248,0.9)", width: 1.5, label: `pPOC ${A.prev.poc.toFixed(1)}` });
        }
        if (A.today && !(layers.volume && a.volume?.available)) segs.push({ from: t0, price: A.today.poc, color: "rgba(232,121,249,0.85)", dash: true, label: `dev POC ${A.today.poc.toFixed(1)}` });
        for (const x of A.boxes) {
          boxes.push({ from: x.from, to: x.active ? lastTime : x.to, top: x.hi, bottom: x.lo, fill: "rgba(226,232,240,0.05)",
            stroke: x.active ? "rgba(226,232,240,0.7)" : "rgba(226,232,240,0.3)", dash: !x.active,
            label: `Balance ${x.bars} bars${x.escaped ? ` · escaped ${x.escaped}` : x.active ? " · live" : ""}`, labelColor: "#cbd5e1" });
        }
      }
      // volume layer: today's volume POC, HVNs (acceptance) / LVNs (fast-move gaps), naked POCs (magnets)
      if (layers.volume && a.volume?.available) {
        const V = a.volume;
        const t0 = candles.find((c) => Math.floor(c.time / 86400) === Math.floor(lastTime / 86400))?.time ?? lastTime;
        if (V.today) {
          segs.push({ from: t0, price: V.today.poc, color: "rgba(250,204,21,0.9)", width: 2, label: `vPOC ${V.today.poc.toFixed(1)}` });
          for (const h of V.today.hvn) if (Math.abs(h - V.today.poc) > V.today.step) segs.push({ from: t0, price: h, color: "rgba(250,204,21,0.45)", dash: true, label: `HVN ${h.toFixed(0)}` });
          for (const l of V.today.lvn) segs.push({ from: t0, price: l, color: "rgba(148,163,184,0.5)", dash: true, label: `LVN ${l.toFixed(0)}` });
        }
        for (const n of V.naked ?? []) segs.push({ from: t0, price: n.price, color: "rgba(251,146,60,0.9)", dash: true, label: `naked POC ${n.day.slice(5)} ${n.price.toFixed(1)}` });
      }
    }
    if (a && layers.qml && a.qml) {
      const live = a.qml.setups.filter((x) => x.status === "ARMED" || x.status === "TRIGGERED" || x.status === "TP1 HIT").slice(-3);
      for (const x of live) {
        const bear = x.dir === "bear";
        const col = x.kind === "QML" ? (bear ? "#f472b6" : "#22d3ee") : bear ? "#fb7185" : "#34d399";
        boxes.push({ from: x.from, to: lastTime, top: x.level + x.tol, bottom: x.level - x.tol,
          fill: bear ? "rgba(244,114,182,0.10)" : "rgba(34,211,238,0.10)", stroke: col, dash: x.status === "ARMED",
          label: `${x.name} ${x.level.toFixed(0)} · ${x.status.toLowerCase()} · ${x.side}`, labelColor: col });
        segs.push({ from: x.from, price: x.sl, color: "rgba(239,68,68,0.85)", dash: true, label: `${x.kind} SL ${x.sl.toFixed(0)}` });
        segs.push({ from: x.from, price: x.tp1, color: "rgba(34,197,94,0.75)", dash: true, label: `${x.kind} TP1 ${x.tp1.toFixed(0)}` });
        segs.push({ from: x.from, price: x.tp2, color: "rgba(34,197,94,0.5)", dash: true, label: `${x.kind} TP2 ${x.tp2.toFixed(0)} (2.2R)` });
      }
    }
    // Adv PA (10 Core Concepts): Super OB, Dragon Fruit, MiB, S/D Bases, FTR, GPX Corridor, ROTE
    if (a && layers.advPa && a.pa_advanced) {
      const pa = a.pa_advanced;
      const t0 = candles[0]?.time ?? (lastTime - 3600 * 6);

      if (advPaSniperMode) {
        if (pa.sniper) {
          const sniper = pa.sniper;
          // Zero Clutter in 🎯 Sniper Mode:
          // 1. Render ONLY sniper.best_supply (1 clean rose/gold box above price)
          if (sniper.best_supply) {
            const s = sniper.best_supply;
            boxes.push({
              from: Math.max(s.ts || t0, candles[Math.max(0, candles.length - 45)]?.time ?? t0),
              to: lastTime,
              top: s.top,
              bottom: s.bot,
              fill: "rgba(244, 63, 94, 0.14)",
              stroke: "#f43f5e",
              label: s.label || `🎯 ${s.grade || "A+ SNIPER"} SUPPLY [${s.bot.toFixed(0)}–${s.top.toFixed(0)}] · ${s.score.toFixed(1)}/10`,
              labelColor: "#f43f5e",
            });
          }
          // 2. Render ONLY sniper.best_demand (1 clean emerald/teal box below price)
          if (sniper.best_demand) {
            const d = sniper.best_demand;
            boxes.push({
              from: Math.max(d.ts || t0, candles[Math.max(0, candles.length - 45)]?.time ?? t0),
              to: lastTime,
              top: d.top,
              bottom: d.bot,
              fill: "rgba(16, 185, 129, 0.14)",
              stroke: "#10b981",
              label: d.label || `🎯 ${d.grade || "A+ SNIPER"} DEMAND [${d.bot.toFixed(0)}–${d.top.toFixed(0)}] · ${d.score.toFixed(1)}/10`,
              labelColor: "#10b981",
            });
          }
          // 3. TradingView-Style Position Risk-Reward Box (sniper.trade_plan)
          if (sniper.trade_plan) {
            const tp = sniper.trade_plan;
            // Anchor cleanly on right-hand execution strip (last 8 bars -> lastTime)
            const rrFrom = candles[Math.max(0, candles.length - 8)]?.time ?? lastTime;
            const rrTo = lastTime;

            // Stop-loss zone over right ~8 bars
            const slTop = Math.max(tp.entry, tp.sl);
            const slBot = Math.min(tp.entry, tp.sl);
            boxes.push({
              from: rrFrom,
              to: rrTo,
              top: slTop,
              bottom: slBot,
              fill: "rgba(239, 68, 68, 0.16)",
              stroke: "#ef4444",
              dash: false,
              label: `🛑 SL ${tp.sl.toFixed(0)} (-1R)`,
              labelColor: "#ef4444",
            });

            // Target Zone over right ~8 bars
            const tpTop = Math.max(tp.entry, tp.tp1);
            const tpBot = Math.min(tp.entry, tp.tp1);
            boxes.push({
              from: rrFrom,
              to: rrTo,
              top: tpTop,
              bottom: tpBot,
              fill: "rgba(16, 185, 129, 0.15)",
              stroke: "#10b981",
              dash: false,
              label: `🎯 TP1 ${tp.tp1.toFixed(0)} (${tp.rr ? tp.rr.toFixed(1) + "R" : "2R"})`,
              labelColor: "#10b981",
            });

            // Crisp institutional gold Entry Line (#fbbf24, 2px solid)
            segs.push({
              from: rrFrom,
              price: tp.entry,
              color: "#fbbf24",
              dash: false,
              width: 2,
              label: `⚡ ENTRY ${tp.entry.toFixed(0)} · ${tp.strike}`,
            });

            // Dashed neon cyan 1R Breakeven Line (#22d3ee)
            if (tp.be_1r != null) {
              segs.push({
                from: rrFrom,
                price: tp.be_1r,
                color: "#22d3ee",
                dash: true,
                width: 1,
                label: `🛡️ 1R BE ${tp.be_1r.toFixed(0)} (Move SL→0)`,
              });
            }

            // Optional TP2 Runner line
            if (tp.tp2 != null && Math.abs(tp.tp2 - tp.tp1) > 2) {
              segs.push({
                from: rrFrom,
                price: tp.tp2,
                color: "#10b981",
                dash: true,
                width: 1,
                label: `🚀 TP2 ${tp.tp2.toFixed(0)} (Runner)`,
              });
            }
          }
          // Keep subtle GPX Ceiling / EQ / Floor dashed reference lines
          if (pa.gpx) {
            const g = pa.gpx;
            segs.push({
              from: t0,
              price: g.corridor_high,
              color: "rgba(244,63,94,0.6)",
              dash: true,
              label: `GPX Ceiling (Supply) ${g.corridor_high.toFixed(0)}`,
            });
            segs.push({
              from: t0,
              price: g.corridor_mid,
              color: "rgba(147,51,234,0.65)",
              dash: true,
              label: `GPX EQ ${g.corridor_mid.toFixed(0)}`,
            });
            segs.push({
              from: t0,
              price: g.corridor_low,
              color: "rgba(16,185,129,0.6)",
              dash: true,
              label: `GPX Floor (Demand) ${g.corridor_low.toFixed(0)}`,
            });
          }
        }
      } else {
        // Raw All-10 Diagnostic Mode (Cleaned: top 3 Demand & top 3 Supply closest to current price)
        const curPx = candles[candles.length - 1]?.close ?? 0;
        const rawZones = pa.zones || [];
        const demandZones = rawZones
          .filter((z) => (z.top || z.bottom) <= curPx + 20)
          .sort((a, b) => b.top - a.top)
          .slice(0, 3);
        const supplyZones = rawZones
          .filter((z) => (z.bottom || z.top) >= curPx - 20)
          .sort((a, b) => a.bottom - b.bottom)
          .slice(0, 3);
        const activeZones = [...demandZones, ...supplyZones];

        for (const z of activeZones) {
          boxes.push({
            from: Math.max(z.from, t0),
            to: lastTime,
            top: z.top,
            bottom: z.bottom,
            fill: z.fill || "rgba(236,72,153,0.18)",
            stroke: z.stroke || "#ec4899",
            label: z.label,
            labelColor: z.stroke || "#ec4899",
          });
        }
        if (pa.gpx) {
          const g = pa.gpx;
          boxes.push({
            from: t0,
            to: lastTime,
            top: g.corridor_high,
            bottom: g.corridor_low,
            fill: "rgba(147,51,234,0.04)",
            stroke: "rgba(147,51,234,0.45)",
            dash: true,
            label: `GPX ${g.htf} Corridor [${g.corridor_low.toFixed(0)}–${g.corridor_high.toFixed(0)}] · ${g.zone}`,
            labelColor: "#c084fc",
          });
          segs.push({
            from: t0,
            price: g.corridor_high,
            color: "rgba(244,63,94,0.7)",
            dash: true,
            label: `GPX Ceiling (Supply) ${g.corridor_high.toFixed(0)}`,
          });
          segs.push({
            from: t0,
            price: g.corridor_mid,
            color: "rgba(147,51,234,0.75)",
            dash: true,
            label: `GPX EQ ${g.corridor_mid.toFixed(0)}`,
          });
          segs.push({
            from: t0,
            price: g.corridor_low,
            color: "rgba(16,185,129,0.7)",
            dash: true,
            label: `GPX Floor (Demand) ${g.corridor_low.toFixed(0)}`,
          });
        }
        // ROTE targets
        for (const r of (pa.rote || []).slice(-2)) {
          segs.push({
            from: Math.max(r.ts, t0),
            price: r.target_price,
            color: r.dir === "bull" ? "#10b981" : "#ef4444",
            dash: true,
            label: `ROTE Target (${r.target_type}) ${r.target_price.toFixed(0)}`,
          });
        }
      }
    }
    // delta layer: Institutional Absorption defense levels (passive limit orders absorbing aggressive volume)
    if (a && isDeltaOn) {
      const t0 = candles.find((c) => Math.floor(c.time / 86400) === Math.floor(lastTime / 86400))?.time ?? (lastTime - 30 * 300);
      for (const m of a.markers) {
        if (m.kind === "DELTA_ABSORPTION" && m.price && m.time >= t0) {
          const bullAbs = m.dir === "bull";
          segs.push({
            from: m.time,
            price: m.price,
            color: bullAbs ? "#10b981" : "#ef4444",
            dash: true,
            width: 1.5,
            label: `ABS ${bullAbs ? "▲ buy abs" : "▼ sell abs"} ${m.price.toFixed(1)}`,
          });
        }
      }
    }
    // HTF virgin wicks (1H Hourly ★ 1×, 1D Daily ★★ 2×, 1W Weekly ★★★ 3×)
    if (layers.rblock && htfWicks.length) {
      for (const w of htfWicks.filter((x) => !x.tested)) {
        const isBuy = w.side === "BUY";
        boxes.push({
          from: w.candleTime,
          to: lastTime,
          top: w.top,
          bottom: w.bottom,
          fill: isBuy ? "rgba(16,185,129,0.06)" : "rgba(239,68,68,0.06)",
          stroke: isBuy ? "rgba(16,185,129,0.55)" : "rgba(239,68,68,0.55)",
          dash: true,
          label: `${w.tf} ${w.tfName} Wick ${w.stars} (${w.weight}×) ${isBuy ? "Support" : "Resist"}`,
          labelColor: isBuy ? "#6ee7b7" : "#fca5a5",
        });
        segs.push({
          from: w.candleTime,
          price: w.mid,
          color: isBuy ? "#10b981" : "#ef4444",
          dash: true,
          width: w.weight >= 2 ? 1.5 : 1,
          label: `${w.tf} Wick 50% ${w.mid.toFixed(1)}`,
        });
      }
    }
    const dayStarts: number[] = [];
    if (tf !== "1D") for (let i = 1; i < candles.length; i++) if (Math.floor(candles[i].time / 86400) !== Math.floor(candles[i - 1].time / 86400)) dayStarts.push(candles[i].time);
    if (a && layers.dol) {
      const dols = computeDol(candles, a);
      const act = [...dols].reverse().find((d) => !d.hit);
      if (act) {
        if (act.kind === "IRL") boxes.push({ from: act.from, to: lastTime, top: act.hi, bottom: act.lo, fill: "rgba(167,139,250,0.16)",
          stroke: "rgba(167,139,250,0.9)", label: `${act.label} · after ${act.why}`, labelColor: "#c4b5fd", dash: true });
        else segs.push({ from: act.from, price: act.lo, color: "rgba(167,139,250,0.95)", width: 2, dash: true, label: `${act.label} · after ${act.why}` });
      }
      dolRef.current = dols;
    } else dolRef.current = [];

    // 🏛️ GTF Institutional Demand & Supply Zones (v2.0)
    if (isGtfOn && gtfResult.zones.length) {
      for (const z of gtfResult.zones) {
        const isBroken = z.status === "BROKEN" || z.status === "MITIGATED";
        // Recently broken zones extend forward to lastTime so trader sees the flipped S/R floor/ceiling
        const zoneTo = lastTime;
        const isDemand = z.type === "DEMAND";
        const isFresh = z.status === "FRESH";
        const isTested = z.status === "TESTED";

        const fill = isBroken
          ? "rgba(100, 116, 139, 0.08)"
          : isDemand
          ? (isFresh ? "rgba(16, 185, 129, 0.16)" : "rgba(16, 185, 129, 0.09)")
          : (isFresh ? "rgba(239, 68, 68, 0.16)" : "rgba(239, 68, 68, 0.09)");

        const stroke = isBroken
          ? "rgba(148, 163, 184, 0.4)"
          : isDemand
          ? (isFresh ? "#10b981" : "rgba(16, 185, 129, 0.7)")
          : (isFresh ? "#ef4444" : "rgba(239, 68, 68, 0.7)");

        const labelColor = isBroken
          ? "#94a3b8"
          : isDemand
          ? (isFresh ? "#34d399" : "#6ee7b7")
          : (isFresh ? "#f87171" : "#fca5a5");

        boxes.push({
          from: z.from,
          to: zoneTo,
          top: z.top,
          bottom: z.bottom,
          fill,
          stroke,
          dash: isBroken || isTested,
          label: z.label,
          labelColor,
        });

        // Proximal entry boundary line for active zones
        if (!isBroken) {
          segs.push({
            from: z.from,
            price: z.proximal,
            color: isDemand ? "rgba(16, 185, 129, 0.9)" : "rgba(239, 68, 68, 0.9)",
            dash: !isFresh,
            width: isFresh ? 1.5 : 1,
            label: `${isDemand ? "Demand Proximal" : "Supply Proximal"} ${z.proximal.toFixed(1)}`,
          });
        }
      }
    }

    // Delta Imbalance / POC Zones (drawn as subtle support/resistance rays)
    if (isDeltaDetailOn || isCellsOn || isNodesOn) {
      for (const z of ofPrim.current.zones.slice(-6)) {
        if (z.until == null) {
          segs.push({
            from: z.time,
            price: z.bull ? z.lo : z.hi,
            color: z.bull ? "rgba(74,222,128,0.75)" : "rgba(248,113,113,0.75)",
            dash: true,
            width: 1.5,
            label: z.bull ? `Stacked Buy POC ${z.lo.toFixed(1)}` : `Stacked Sell POC ${z.hi.toFixed(1)}`,
          });
        }
      }
    }

    zones.current.set(boxes, segs, lastTime, dayStarts);

    // ── markers: every layer emits Ev records → merged per bar/side, labelled only where it matters ──
    const evs: Ev[] = [];
    const push = (e: Ev) => evs.push(e);
    const side = (bull: boolean): "above" | "below" => (bull ? "below" : "above");
    if (a && (layers.structure || isDeltaOn || layers.fvg)) {
      for (const m of a.markers) {
        const bull = m.dir === "bull";
        if (m.kind.startsWith("IFVG")) {
          if (!layers.fvg) continue;
          const sig = m.kind !== "IFVG_FLIP", sell = m.dir === "bear";
          push({ time: m.time, side: sell ? "above" : "below", dir: m.dir, kind: m.kind, layer: "IFVG", full: m.text,
            short: sig ? (sell ? "IFVG▼ sell" : "IFVG▲ buy") : "inv", pri: sig ? 3 : 1, size: sig ? 1.15 : 0.5,
            shape: sig ? (sell ? "arrowDown" : "arrowUp") : "circle", color: sell ? "#fb923c" : "#2dd4bf" });
          continue;
        }
        if (!(layers.structure || isDeltaOn)) continue;
        const isDelta = m.kind.startsWith("DELTA_") || m.kind === "FOOTPRINT_IMBALANCE" || m.kind.startsWith("PROXY_FLOW");
        if (isDelta ? !isDeltaOn : !layers.structure) continue;
        if (isDelta && !isDeltaDetailOn && (m.kind === "DELTA_FLIP" || m.kind === "FOOTPRINT_IMBALANCE" || m.kind === "PROXY_FLOW_SURGE")) continue;
        const base = { time: m.time, side: side(bull), dir: m.dir, kind: m.kind, full: `${m.text}${m.price ? ` @ ${m.price.toFixed(1)}` : ""}` };
        if (m.kind.startsWith("PROXY_FLOW")) { push({ ...base, layer: "Delta", short: "flow", color: "#fb923c", shape: "circle", size: 0.5, pri: 1, full: base.full + " (proxy)" }); continue; }
        if (isDelta) {
          const abs = m.kind === "DELTA_ABSORPTION";
          const flip = m.kind === "DELTA_FLIP";
          const agg = m.kind === "DELTA_AGGRESSION";
          const imb = m.kind === "FOOTPRINT_IMBALANCE";
          const shortLab = abs
            ? (bull ? "▲ ABS BUY" : "▼ ABS SELL")
            : agg
            ? (bull ? "⚡ +Δ SURGE" : "⚡ -Δ SURGE")
            : imb
            ? (bull ? "IMB▲" : "IMB▼")
            : shortText(m.kind, m.text);
          push({
            ...base,
            layer: "Delta",
            short: shortLab,
            pri: abs ? 3 : agg ? 2 : imb ? 2 : 1,
            size: abs ? 1.25 : agg ? 1.0 : imb ? 0.8 : 0.6,
            shape: abs ? (bull ? "arrowUp" : "arrowDown") : agg ? "square" : "circle",
            color: abs
              ? (bull ? "#10b981" : "#ef4444")
              : agg
              ? (bull ? "#34d399" : "#f87171")
              : flip
              ? "#e2e8f0"
              : imb
              ? (bull ? "#22d3ee" : "#f472b6")
              : (bull ? "#34d399" : "#f87171"),
          });
          continue;
        }
        const isStruct = m.kind === "BOS" || m.kind === "MSS";
        const weak = /weak/i.test(m.text);
        if (isStruct) {
          push({ ...base, layer: "Structure", short: m.kind === "MSS" ? "MSS" : weak ? "bos" : "BOS",
            pri: m.kind === "MSS" ? 3 : weak ? 1 : 2, size: m.kind === "MSS" ? 1.3 : weak ? 0.55 : 1,
            shape: bull ? "arrowUp" : "arrowDown",
            color: weak ? (bull ? "rgba(16,185,129,0.55)" : "rgba(239,68,68,0.55)") : (bull ? C.bull : C.bear) });
        } else {
          push({ ...base, layer: "Liquidity", short: shortText(m.kind, m.text), pri: m.kind === "SWEEP" ? 2 : 1, size: 0.55,
            shape: "circle", color: m.kind === "SWEEP" ? "#f59e0b" : "#a78bfa" });
        }
      }
    }

    // ── All Delta Detail: candle-level Absorption & Aggressive Delta Surge signals ──
    if (isDeltaDetailOn && candles.length > 5) {
      const deltas = candles.map((c) => {
        const fb = fpBars.find((b) => Math.abs(b.time - c.time) < (BAR_SEC[tf] ?? 300));
        return fb ? fb.delta : (c.close >= c.open ? (c.volume || 1000) * 0.4 : -(c.volume || 1000) * 0.4);
      });
      for (let i = 2; i < candles.length; i++) {
        const c = candles[i];
        const barDelta = deltas[i];
        const rng = Math.max(c.high - c.low, 1e-4);
        const lowerWick = Math.min(c.open, c.close) - c.low;
        const upperWick = c.high - Math.max(c.open, c.close);
        const bodyRatio = Math.abs(c.close - c.open) / rng;

        // 1. Bullish Absorption: lower wick >= 35% with negative net delta, closed in upper 50%
        if (lowerWick / rng >= 0.35 && barDelta < 0 && (c.close - c.low) / rng >= 0.5) {
          const hasExisting = evs.some((e) => e.time === c.time && e.kind.includes("ABS"));
          if (!hasExisting) {
            push({
              time: c.time,
              side: "below",
              dir: "bull",
              kind: "DELTA_ABSORPTION_BUY",
              layer: "Delta",
              short: "▲ ABS BUY",
              full: `🛡 Bullish Absorption: Lower wick ${(lowerWick / rng * 100).toFixed(0)}% with sellers slamming bid (${kfmt(barDelta)} delta) absorbed by passive buyers; closed @ ${c.close.toFixed(1)}`,
              pri: 3,
              size: 1.25,
              shape: "arrowUp",
              color: "#10b981",
            });
          }
        }
        // Bearish Absorption: upper wick >= 35% with positive net delta, closed in lower 50%
        else if (upperWick / rng >= 0.35 && barDelta > 0 && (c.close - c.low) / rng <= 0.5) {
          const hasExisting = evs.some((e) => e.time === c.time && e.kind.includes("ABS"));
          if (!hasExisting) {
            push({
              time: c.time,
              side: "above",
              dir: "bear",
              kind: "DELTA_ABSORPTION_SELL",
              layer: "Delta",
              short: "▼ ABS SELL",
              full: `🛡 Bearish Absorption: Upper wick ${(upperWick / rng * 100).toFixed(0)}% with buyers chasing ask (+${kfmt(barDelta)} delta) absorbed by passive sellers; closed @ ${c.close.toFixed(1)}`,
              pri: 3,
              size: 1.25,
              shape: "arrowDown",
              color: "#ef4444",
            });
          }
        }

        // 2. Aggressive Delta Surge Signals (|bar_delta| >= 1.8x 20-bar avg abs delta)
        const win20 = deltas.slice(Math.max(0, i - 20), i).map((v) => Math.abs(v));
        const avg20 = win20.length >= 5 ? win20.reduce((s, v) => s + v, 0) / win20.length : 0;
        if (avg20 > 0 && Math.abs(barDelta) >= 1.8 * avg20 && bodyRatio >= 0.45) {
          if (barDelta > 0 && c.close > c.open) {
            const hasExisting = evs.some((e) => e.time === c.time && (e.kind.includes("SURGE") || e.kind.includes("AGGRESSION")));
            if (!hasExisting) {
              push({
                time: c.time,
                side: "below",
                dir: "bull",
                kind: "DELTA_SURGE_BUY",
                layer: "Delta",
                short: "⚡ +Δ SURGE",
                full: `⚡ Aggressive Buyer Surge: Delta +${kfmt(barDelta)} (1.8× 20-bar avg) with strong upward close`,
                pri: 3,
                size: 1.1,
                shape: "square",
                color: "#10b981",
              });
            }
          } else if (barDelta < 0 && c.close < c.open) {
            const hasExisting = evs.some((e) => e.time === c.time && (e.kind.includes("SURGE") || e.kind.includes("AGGRESSION")));
            if (!hasExisting) {
              push({
                time: c.time,
                side: "above",
                dir: "bear",
                kind: "DELTA_SURGE_SELL",
                layer: "Delta",
                short: "⚡ -Δ SURGE",
                full: `⚡ Aggressive Seller Surge: Delta −${kfmt(Math.abs(barDelta))} (1.8× 20-bar avg) with strong downward close`,
                pri: 3,
                size: 1.1,
                shape: "square",
                color: "#ef4444",
              });
            }
          }
        }
      }
    }
    if (a && layers.qml && a.qml) {
      for (const m of a.qml.markers) {
        const bear = m.dir === "bear", trig = m.kind.endsWith("_TRIG"), armed = m.kind.endsWith("_ARMED");
        const pt = m.kind === "QML_PT", snr = m.kind.startsWith("SNR_");
        const kind = m.kind.split("_")[0];
        push({ time: m.time, side: bear ? "above" : "below", dir: m.dir, kind: m.kind, layer: "QML", full: m.text,
          short: trig ? `${kind}${bear ? "▼" : "▲"}` : armed ? `${kind} armed` : pt ? m.text.split(" ")[0] : snr ? (bear ? "upthrust" : "spring") : m.text.slice(0, 8),
          pri: trig ? 3 : armed || snr ? 2 : 1, size: trig ? 1.2 : pt ? 0.4 : 0.7,
          shape: trig ? (bear ? "arrowDown" : "arrowUp") : pt ? "circle" : "square",
          color: snr ? "#fbbf24" : bear ? "#f472b6" : "#22d3ee" });
      }
    }
    if (a && layers.advPa && a.pa_advanced) {
      const pa = a.pa_advanced;
      if (advPaSniperMode) {
        if (pa.sniper) {
          // Push at most 2 markers: sniper.last_strong_msb and sniper.last_sfp. Never push Weak MSB markers!
          if (pa.sniper.last_strong_msb) {
            const m = pa.sniper.last_strong_msb;
            const bull = m.dir === "bull";
            push({
              time: m.time,
              side: (m as any).side || side(bull),
              dir: m.dir,
              kind: m.kind || "MSB_STRONG",
              layer: "AdvPA",
              full: (m as any).full || m.text,
              short: shortText(m.kind || "MSB_STRONG", m.text),
              pri: 3,
              size: 1.3,
              shape: (m as any).shape || (bull ? "arrowUp" : "arrowDown"),
              color: (m as any).color || (bull ? "#10b981" : "#f43f5e"),
            });
          }
          if (pa.sniper.last_sfp) {
            const s = pa.sniper.last_sfp;
            const bull = s.dir === "bull";
            push({
              time: s.time,
              side: (s as any).side || side(bull),
              dir: s.dir,
              kind: s.kind || "PURE_SFP",
              layer: "AdvPA",
              full: (s as any).full || s.text,
              short: shortText(s.kind || "PURE_SFP", s.text),
              pri: 3,
              size: 1.0,
              shape: (s as any).shape || "square",
              color: (s as any).color || "#06b6d4",
            });
          }
        }
      } else if (pa.markers) {
        for (const m of pa.markers) {
          if (m.kind === "MSB_WEAK" || m.text?.toLowerCase().includes("weak msb")) continue;
          const bull = m.dir === "bull";
          push({
            time: m.time,
            side: (m as any).side || side(bull),
            dir: m.dir,
            kind: m.kind,
            layer: "AdvPA",
            full: (m as any).full || m.text,
            short: shortText(m.kind, m.text),
            pri: 3,
            size: m.kind.startsWith("MSB") ? 1.3 : 1.0,
            shape: (m as any).shape || (bull ? "arrowUp" : "arrowDown"),
            color: (m as any).color || (bull ? "#10b981" : "#f43f5e"),
          });
        }
      }
    }
    if (a && layers.crt && a.crt) {
      for (const m of a.crt.markers) {
        const bull = m.dir === "bull";
        const entry = m.kind === "CRT_ENTRY", tbs = m.kind === "CRT_TBS";
        push({ time: m.time, side: side(bull), dir: m.dir, kind: entry ? "CRT_ENTRY" : tbs ? "CRT_TBS" : "CRT_M", layer: "CRT",
          short: entry ? "CRT▶" : tbs ? "TBS" : m.text.slice(0, 3), full: m.text, pri: entry ? 3 : tbs ? 2 : 1,
          shape: entry ? (bull ? "arrowUp" : "arrowDown") : tbs ? "square" : "circle",
          color: entry ? (bull ? "#22c55e" : "#ef4444") : tbs ? "#e8c35a" : "#cbd5e1", size: entry ? 1.3 : 0.7 });
      }
    }
    if (a && layers.auction && a.auction) {
      for (const m of a.auction.markers) {
        const acc = m.kind === "AUCTION_ACCEPT", rej = m.kind === "AUCTION_REJECT";
        const bull = m.dir === "bull";
        push({ time: m.time, side: side(bull), dir: m.dir, kind: m.kind, layer: "Auction",
          short: acc ? "ACC" : rej ? "REJ" : "–", full: m.text, pri: acc ? 3 : rej ? 2 : 1,
          shape: acc ? (bull ? "arrowUp" : "arrowDown") : "circle", color: acc ? "#38bdf8" : rej ? "#fb923c" : "#94a3b8", size: acc ? 0.9 : 0.7 });
      }
    }
    if (a && layers.dol) {
      for (const d of dolRef.current) {
        push({ time: d.from, side: d.dir === "bull" ? "below" : "above", dir: d.dir, kind: "DOL", layer: "D.O.L",
          short: d.kind === "IRL" ? "→FVG" : "→ERL", full: `${d.label} · after ${d.why}${d.hit ? " · ✓ reached" : " · active"}`,
          pri: 2, shape: "circle", color: "#a78bfa", size: 0.6 });
        if (d.hit) push({ time: d.hit, side: d.dir === "bull" ? "above" : "below", dir: d.dir, kind: "DOL_HIT", layer: "D.O.L",
          short: "DOL✓", full: `${d.label} reached`, pri: 1, shape: "square", color: "#a78bfa", size: 0.5 });
      }
    }
    if (layers.ocAct) {
      let lastKind = "";
      let lastV = 0;
      for (const x of ocActs) {
        if (!x.smartRow) continue;
        const r = x.smartRow;

        // Stage 2: Sniper Pullback Entry (Optimal Low-Risk Entry)
        if (x.kind === "PULLBACK") {
          const isBear = r.actionTone === "bear";
          push({
            time: x.time,
            side: isBear ? "above" : "below",
            dir: isBear ? "bear" : "bull",
            kind: "OC_ACT",
            layer: "PE−CE action",
            short: isBear ? "🎯 PB PE" : "🎯 DIP CE",
            full: r.actionText,
            pri: 3,
            shape: isBear ? "arrowDown" : "arrowUp",
            color: isBear ? "#fb7185" : "#34d399",
            size: 1.4,
          });
        }
        // Stage 1: Sniper Breakout Entry
        else if (x.kind === "ENTRY" && (x.v !== lastV || lastKind !== "ENTRY")) {
          const isBear = r.actionTone === "bear";
          push({
            time: x.time,
            side: isBear ? "above" : "below",
            dir: isBear ? "bear" : "bull",
            kind: "OC_ACT",
            layer: "PE−CE action",
            short: isBear ? "🎯 PE" : "🎯 CE",
            full: r.actionText,
            pri: 3,
            shape: isBear ? "arrowDown" : "arrowUp",
            color: isBear ? "#f43f5e" : "#10b981",
            size: 1.3,
          });
        }
        // Stage 3: Momentum Writer Shock Scale-In / Add
        else if (x.kind === "ADD") {
          const isBear = r.actionTone === "bear";
          push({
            time: x.time,
            side: isBear ? "above" : "below",
            dir: isBear ? "bear" : "bull",
            kind: "OC_ACT",
            layer: "PE−CE action",
            short: isBear ? "🔥 ADD PE" : "🔥 ADD CE",
            full: r.actionText,
            pri: 3,
            shape: isBear ? "arrowDown" : "arrowUp",
            color: "#f97316",
            size: 1.25,
          });
        }
        // Stage 5: Short-Covering Reversal / Book Profits
        else if (x.kind === "BOOK" && lastKind !== "BOOK") {
          push({
            time: x.time,
            side: "below",
            dir: "bull",
            kind: "OC_ACT",
            layer: "PE−CE action",
            short: "💰 BOOK",
            full: r.actionText,
            pri: 3,
            shape: "circle",
            color: "#fbbf24",
            size: 1.3,
          });
        }

        lastKind = x.kind;
        lastV = x.v;
      }
    }
    if (isCvdOn) {
      for (const d of cvdDivergences) {
        if (d.type === "BULLISH_ABSORPTION") {
          push({
            time: d.time,
            side: "below",
            dir: "bull",
            kind: "CVD_ABS",
            layer: "CVD divergence",
            short: "🟣 CVD BULL DIV",
            full: d.details.replace("BULLISH ABSORPTION", "🟣 CVD BULL DIV"),
            pri: 3,
            shape: "arrowUp",
            color: "#c084fc",
            size: 1.35,
          });
        } else if (d.type === "BEARISH_DISTRIBUTION") {
          push({
            time: d.time,
            side: "above",
            dir: "bear",
            kind: "CVD_DIST",
            layer: "CVD divergence",
            short: "🟠 CVD BEAR DIV",
            full: d.details.replace("BEARISH DISTRIBUTION", "🟠 CVD BEAR DIV"),
            pri: 3,
            shape: "arrowDown",
            color: "#fb923c",
            size: 1.35,
          });
        } else if (d.type === "EXHAUSTION_CLIMAX") {
          const isBull = d.dir === "bull";
          push({
            time: d.time,
            side: isBull ? "below" : "above",
            dir: d.dir,
            kind: "CVD_EXHAUST",
            layer: "CVD divergence",
            short: isBull ? "⚡ CVD EXH▲" : "⚡ CVD EXH▼",
            full: d.details,
            pri: 2,
            shape: "circle",
            color: isBull ? "#34d399" : "#fb7185",
            size: 1.1,
          });
        }
      }
    }
    if (isCvdOn || isNodesOn) for (const event of cvdSignals.filter((e)=>e.kind==="confirmed")) {
      push({time:event.time,side:event.dir==="bull"?"below":"above",dir:event.dir,kind:"CVD_NODE_CONFIRMED",
        layer:"CVD + stacked nodes",short:event.dir==="bull"?"CVD + NODE ✓▲":"CVD + NODE ✓▼",
        full:event.details,pri:3,shape:event.dir==="bull"?"arrowUp":"arrowDown",color:"#fbbf24",size:1.2});
    }
    if (layers.smt) {
      for (const m of smtMarks) push({ time: m.time, side: m.dir === "bull" ? "below" : "above", dir: m.dir, kind: "SMT", layer: "SMT",
        short: m.dir === "bull" ? "SMT▲" : "SMT▼", full: `${m.text} (pivot ${m.pivot_at}, confirmed ${m.at})`, pri: 3,
        shape: m.dir === "bull" ? "arrowUp" : "arrowDown", color: m.dir === "bull" ? "#34d399" : "#f472b6", size: 1 });
    }
    if (a && layers.tsoup) {
      for (const t of turtleSoup(candles, a)) push({ time: t.time, side: t.dir === "bull" ? "below" : "above", dir: t.dir, kind: "TURTLE_SOUP", layer: "Turtle soup",
        short: t.dir === "bull" ? "🐢TS▲" : "🐢TS▼",
        full: `Turtle soup ${t.dir === "bull" ? "BUY" : "SELL"} — false break of ${t.name} ${t.level.toFixed(0)} (wick ${t.wick.toFixed(0)}, closed back ${t.dir === "bull" ? "above" : "below"} at ${t.close.toFixed(0)}) → ${t.dir === "bull" ? "sell-side liquidity swept, reversal up" : "buy-side liquidity swept, reversal down"}`,
        pri: 2, shape: t.dir === "bull" ? "arrowUp" : "arrowDown", color: "#2dd4bf", size: 1.3 });
    }
    if (a && layers.rvol) {
      const RC: Record<string, string> = { RVOL_ACTIVATED: "#22c55e", BREAKOUT_WITH_RVOL: "#22c55e", RVOL_SPIKE: "#e8c35a", RVOL_FADING: "#94a3b8", BREAKOUT_NO_RVOL: "#ef4444" };
      const RT: Record<string, string> = { RVOL_ACTIVATED: "RVOL▲", BREAKOUT_WITH_RVOL: "BO✓", RVOL_SPIKE: "⚡", RVOL_FADING: "RVOL▼", BREAKOUT_NO_RVOL: "BO✗" };
      let lastSpikeTime = -999;
      const stepSec = ((tf as string) === "1m") ? 60 : ((tf as string) === "3m") ? 180 : tf === "5m" ? 300 : tf === "15m" ? 900 : 1800;
      for (const e of a.volume?.rvol_events ?? []) {
        const bt = bucket(e.time, tf), cc = cMap.current.get(bt);
        // Throttle non-breakout spike labels to maintain 3-4 bar spacing
        const isNonBoSpike = e.kind === "RVOL_SPIKE" || e.kind === "RVOL_ACTIVATED";
        if (isNonBoSpike && bt - lastSpikeTime < stepSec * 3) {
          continue;
        }
        if (isNonBoSpike) lastSpikeTime = bt;

        const vw = [...(a.volume?.vwap ?? [])].reverse().find((q) => q.time <= bt);
        const up = cc ? cc.close >= cc.open : null;
        const who = up == null ? "" : up ? " ▲" : " ▼";
        const act = up == null ? "" : up ? (vw && cc!.close > vw.v ? " — institutions BUYING above VWAP: CE side only, never PE" : " — buying, but below VWAP: wait")
          : (vw && cc!.close < vw.v ? " — institutions SELLING below VWAP: PE side only, never CE" : " — selling, but above VWAP: likely a dip, wait");
        push({ time: bt, side: "above", dir: up == null ? "flat" : up ? "bull" : "bear", kind: e.kind, layer: "RVOL",
          short: `${RT[e.kind] ?? e.kind} ${e.value.toFixed(1)}×${e.kind === "RVOL_FADING" ? "" : who}`, full: `${RT[e.kind] ?? e.kind} ${e.value.toFixed(2)}×${who} — ${e.title ?? ""}${e.kind === "RVOL_FADING" ? "" : act}`,
          pri: e.kind === "RVOL_FADING" ? 2 : 3, shape: e.kind === "BREAKOUT_NO_RVOL" ? "arrowDown" : "circle", color: RC[e.kind] ?? "#e8c35a", size: 0.7 });
      }
    }
    if (a && layers.oi) {
      for (const x of a.volume?.oi_line ?? []) {
        if (x.surge || x.reduction) push({ time: x.time, side: "above", dir: "flat", kind: "OI_SURGE", layer: "Futures OI", short: `${x.reduction ? "OI REDUCTION" : "OI EXPANSION"} ${x.surge_x ?? ""}×`,
          full: `Unusual OI ${x.reduction ? "reduction" : "expansion"} ${x.surge_x ?? ""}× recent average · context, no trade direction${x.build ? ` · ${x.build}` : ""}`, pri: 1, shape: "square", color: x.reduction ? "#a78bfa" : "#f59e0b", size: 0.8 });
      }
    }
    if (isPrahaarOn) {
      for (const s of prahaarResult.signals) {
        let sigTime = s.time;
        // Accurately align marker with chart candle timestamps (handling raw epoch vs chart_time shift)
        if (!cMap.current.has(sigTime)) {
          if (cMap.current.has(sigTime + 19800)) {
            sigTime = sigTime + 19800;
          } else if (cMap.current.has(sigTime - 19800)) {
            sigTime = sigTime - 19800;
          }
        }
        const isBear = s.dir === "bear";
        if (s.isMasterSniper) {
          push({
            time: sigTime,
            side: isBear ? "above" : "below",
            dir: s.dir,
            kind: isBear ? "PRAHAAR_MASTER_SNIPER_PE" : "PRAHAAR_MASTER_SNIPER_CE",
            layer: "Prahaar Engine",
            short: isBear ? `🎯 A+ ${s.strike} PE` : `🎯 A+ ${s.strike} CE`,
            full: s.masterSniperReason ?? (isBear ? `🔴 BUY ATM PUT (Prahaar): ${s.strike} PE` : `🟢 BUY ATM CALL (Prahaar): ${s.strike} CE`),
            pri: 3,
            shape: isBear ? "arrowDown" : "arrowUp",
            color: isBear ? "#ef4444" : "#10b981",
            size: 1.6,
          });
        } else {
          push({
            time: sigTime,
            side: isBear ? "above" : "below",
            dir: s.dir,
            kind: isBear ? "PRAHAAR_BUY_PE" : "PRAHAAR_BUY_CE",
            layer: "Prahaar Engine",
            short: isBear ? `🔴 BUY ${s.strike} PE` : `🟢 BUY ${s.strike} CE`,
            full: isBear
              ? `🔴 BUY ATM PUT (Prahaar): ${s.strike} PE @ ${s.entry.toFixed(1)} · SL ${s.sl.toFixed(1)} · TP ${s.tp.toFixed(1)} (${s.isImpulseActive ? "Impulse Active" : "EMA Bear + MACD Cross"} · ADX ${s.adx.toFixed(1)})`
              : `🟢 BUY ATM CALL (Prahaar): ${s.strike} CE @ ${s.entry.toFixed(1)} · SL ${s.sl.toFixed(1)} · TP ${s.tp.toFixed(1)} (${s.isImpulseActive ? "Impulse Active" : "EMA Bull + MACD Cross"} · ADX ${s.adx.toFixed(1)})`,
            pri: 3,
            shape: isBear ? "arrowDown" : "arrowUp",
            color: isBear ? "#ef4444" : "#10b981",
            size: 1.35,
          });
        }
      }
    }
    evRef.current = index(evs);
    const lastSig = [...evs].filter((e) => e.pri >= 2).sort((x, y) => Number(y.time) - Number(x.time)).slice(0, 4);
    setLatest(lastSig);
    const ln: Record<string, Ev> = {};
    for (const e of [...evs].sort((x, y) => x.time - y.time)) ln[e.layer] = e;
    setLayerNow(ln);
    markers.current.setMarkers(toMarkers(evs, lastTime, BAR_SEC[tf] ?? 300));

    const V = a?.volume;
    const on = !!(a && layers.volume && V?.available);
    volS.current?.setData(on ? (V!.bars ?? []).map((b) => ({ time: b.time as UTCTimestamp, value: b.v,
      color: b.rvol != null && b.rvol >= 1.5 ? (b.up ? "rgba(16,185,129,0.95)" : "rgba(239,68,68,0.95)")
        : b.rvol != null && b.rvol <= 0.7 ? "rgba(100,116,139,0.35)" : (b.up ? "rgba(16,185,129,0.45)" : "rgba(239,68,68,0.45)") })) : []);
    const vw = on ? V!.vwap ?? [] : [];
    const L = vwapS.current;
    const day = (t: number) => Math.floor(t / 86400);
    const days = [...new Set(vw.map((p) => day(p.time)))].slice(-2);     // today + yesterday: one line set each
    if (L.length) {
      const k = (V?.bands ?? [1, 2]);
      const fs = [(p: { v: number; sd: number }) => p.v, (p: { v: number; sd: number }) => p.v + k[0] * p.sd,
        (p: { v: number; sd: number }) => p.v - k[0] * p.sd, (p: { v: number; sd: number }) => p.v + (k[1] ?? 2) * p.sd,
        (p: { v: number; sd: number }) => p.v - (k[1] ?? 2) * p.sd];
      for (const par of [0, 1]) {
        const pts = vw.filter((p) => days.indexOf(day(p.time)) === par);
        fs.forEach((f, q) => L[(par ? 7 : 0) + q].setData(pts.map((p) => ({ time: p.time as UTCTimestamp, value: f(p) }))));
      }
      const av = on ? V!.avwaps ?? [] : [];
      for (let q = 0; q < 2; q++) L[5 + q].setData((av[q]?.points ?? []).map((p) => ({ time: p.time as UTCTimestamp, value: p.v })));
    }
    ivS.current?.setData(a && layers.volume ? (V?.iv ?? []).map((p) => ({ time: p.time as UTCTimestamp, value: p.iv })) : []);
    // futures OI pane (pane 1): OI line + its 8-EMA; ΔOI bars coloured by build-up (pane 2). Created only when shown,
    // removed when hidden, so the price chart keeps its full height otherwise.
    const ch = chart.current;
    const ol = a && layers.oi ? (a.volume?.oi_line ?? []) : [];
    // Futures OI: the raw OI line (+ EMA 8) is almost flat intraday (OI moves 1–3 % a day), so only the per-bar
    // ΔOI build-up bars are drawn — green long build-up, light green short covering, red short build-up,
    // pink long unwinding, amber = surge.
    if (ch && ol.length && !oiS.current) {
      const d = ch.addSeries(HistogramSeries, { priceLineVisible: false, lastValueVisible: false, title: "FUT OI build-up", priceScaleId: "oi-build-up", priceFormat: { type: "volume" } }, 1);
      oiS.current = { doi: d };
    } else if (ch && !ol.length && oiS.current) {
      ch.removeSeries(oiS.current.doi);
      oiS.current = null;
    }
    if (oiS.current && ol.length) {
      const BC: Record<string, string> = { "long build-up": "#22c55e", "short covering": "#86efac", "short build-up": "#ef4444", "long unwinding": "#fca5a5" };
      oiS.current.doi.applyOptions({
        title: oiMode === "raw" ? "FUT ΔOI (raw)" : "FUT ΔOI / recent avg |ΔOI|",
        priceFormat: oiMode === "raw" ? { type: "volume" } : { type: "custom", formatter: (v: number) => `${v.toFixed(2)}×`, minMove: 0.01 },
      });
      oiS.current.doi.setData(ol.map((x) => {
        const value = oiMode === "raw" ? x.doi : x.relative;
        return value == null ? { time: x.time as UTCTimestamp } : { time: x.time as UTCTimestamp, value,
          color: x.reduction ? "#a78bfa" : x.surge ? "#f59e0b" : (BC[x.build ?? ""] ?? "rgba(148,163,184,0.5)") };
      }));
      oiS.current.doi.priceScale().setAutoScale(true);
    }
    // cumulative RVOL / Slot RVOL pane:
    const cr = a && layers.rvol ? (a.volume?.crvol ?? []).filter((p) => (p.c20 ?? p.c5) != null) : [];
    const hasRvolBars = a && layers.rvol && ((a.volume?.bars?.length ?? 0) > 0 || cr.length > 0);
    if (ch && hasRvolBars && !rvS.current) {
      const idx = oiS.current ? 2 : 1;
      const h = ch.addSeries(HistogramSeries, { priceLineVisible: false, lastValueVisible: true, title: rvolPaneMode === "slot" ? "Slot RVOL" : "cRVOL", base: 0 }, idx);
      const l = ch.addSeries(LineSeries, { color: "#f1f5f9", lineWidth: 1, priceLineVisible: false, lastValueVisible: false, title: "5d" }, idx);
      h.createPriceLine({ price: 1.0, color: "rgba(148,163,184,0.8)", lineWidth: 1, lineStyle: LineStyle.Dashed, axisLabelVisible: true, title: "1.0x Normal" });
      h.createPriceLine({ price: 1.5, color: "#10b981", lineWidth: 1, lineStyle: LineStyle.Dashed, axisLabelVisible: true, title: "1.5x In Play" });
      h.createPriceLine({ price: 2.5, color: "#f59e0b", lineWidth: 1, lineStyle: LineStyle.Dashed, axisLabelVisible: true, title: "2.5x Spike" });
      try { ch.panes()[idx]?.setStretchFactor(1.2); } catch { /* older lib */ }
      rvS.current = { h, l };
    } else if (ch && !hasRvolBars && rvS.current) {
      ch.removeSeries(rvS.current.h); ch.removeSeries(rvS.current.l); rvS.current = null;
    }
    if (rvS.current && hasRvolBars) {
      if (rvolPaneMode === "slot") {
        const vbars = a?.volume?.bars ?? [];
        const seen = new Set<number>();
        const slotData: { time: UTCTimestamp; value: number; color: string }[] = [];
        for (const b of vbars) {
          const val = unifiedSlotMap.get(b.time) ?? b.slot_rvol ?? b.rvol;
          if (val == null || seen.has(b.time)) continue;
          seen.add(b.time);
          const color =
            val >= 2.5 ? "#f59e0b" :
            val >= 1.5 ? "#10b981" :
            val >= 0.8 ? "#3b82f6" :
            "#64748b";
          slotData.push({ time: b.time as UTCTimestamp, value: val, color });
        }
        for (const [t, val] of unifiedSlotMap.entries()) {
          if (seen.has(t) || val == null) continue;
          seen.add(t);
          const color =
            val >= 2.5 ? "#f59e0b" :
            val >= 1.5 ? "#10b981" :
            val >= 0.8 ? "#3b82f6" :
            "#64748b";
          slotData.push({ time: t as UTCTimestamp, value: val, color });
        }
        slotData.sort((x, y) => (x.time as number) - (y.time as number));
        try { rvS.current.h.applyOptions({ title: "Slot RVOL" }); } catch { /* older lib */ }
        rvS.current.h.setData(slotData);
        rvS.current.l.setData([]);
      } else {
        try { rvS.current.h.applyOptions({ title: "cRVOL" }); } catch { /* older lib */ }
        rvS.current.h.setData(cr.map((p) => {
          const v = (p.c20 ?? p.c5) as number;
          const color =
            v >= 2.5 ? "#f59e0b" :
            v >= 1.5 ? "#10b981" :
            v >= 0.8 ? "#3b82f6" :
            "#64748b";
          return { time: p.time as UTCTimestamp, value: v, color };
        }));
        rvS.current.l.setData(cr.filter((p) => p.c5 != null && p.c20 != null).map((p) => ({ time: p.time as UTCTimestamp, value: p.c5 as number })));
      }
    }
    // pane heights: price always dominant, each indicator pane a thin strip
    try {
      const P = ch?.panes() ?? [];
      P.forEach((p, i) => p.setStretchFactor(i === 0 ? 7 : 1.3));
      if (oiS.current && a?.volume?.oi_health?.pane_height) P[1]?.setHeight(a.volume.oi_health.pane_height);
    } catch { /* older lib */ }
    spine.current?.setData(a && layers.qml ? candles.filter((c) => c.closed !== false).map((c) => ({ time: c.time as UTCTimestamp, value: c.close })) : []);
    zz.current.setData(a && layers.swings ? dedupe(a.zigzag).map((p) => ({ time: p.time as UTCTimestamp, value: p.price })) : []);
  }, [analysis, layers, candles, smtMarks, ocActs, advPaSniperMode, isPrahaarOn, prahaarResult, isGtfOn, gtfResult, rvolPaneMode, oiMode, unifiedSlotMap, cvdSignals]);

  // ── DOM book events (for the hover confirm/warn line), every 6 s ──
  useEffect(() => {
    if (!(layers.walls || sig?.on)) { setBookEv([]); return; }
    let dead = false;
    const go = () => fetch("/api/book").then((r) => r.json()).then((j) => { if (!dead && j?.available) setBookEv(j.events || []); }).catch(() => {});
    go(); const id = window.setInterval(go, 6000);
    return () => { dead = true; window.clearInterval(id); };
  }, [layers.walls, sig?.on]);

  // ── Order-flow Pro: OI walls (real chain, every 60 s) ──
  useEffect(() => {
    if (!layers.walls) { setWalls([]); setWriterBal(null); setOiLevels([]); }
    if (!layers.gamma) setGexMap(null);
    if (!layers.walls && !layers.gamma) return;
    let dead = false;
    const fireWall = (kind: string, side: "BUY" | "SELL", title: string, body: string, price: number) => {
      if (!marketOpenRef.current || !sigOnRef.current || !onSignalRef.current) return;
      const ts = Math.floor(Date.now() / 1000);
      onSignalRef.current({ id: ts, ts, time: "", grade: "info", kind, side, tf: tfRef.current, title, body, price });
    };
    const detectWallEvents = (ws: Wall[]) => {
      const prev = wallPrevRef.current;
      const status: Record<string, Wall["status"]> = {};
      for (const w of ws) status[`${w.side}${w.p}`] = w.status;
      const ce = ws.filter((w) => w.side === "CE").sort((a, b) => b.oi - a.oi)[0] ?? null;
      const pe = ws.filter((w) => w.side === "PE").sort((a, b) => b.oi - a.oi)[0] ?? null;
      const topCE = ce?.p ?? null, topPE = pe?.p ?? null;
      if (prev) {
        for (const w of ws) {                                   // BUILDING/holding → UNWINDING = writers capitulating
          const was = prev.status[`${w.side}${w.p}`];
          if (was && was !== "unwinding" && w.status === "unwinding")
            fireWall("WALL_FLIP", w.side === "CE" ? "BUY" : "SELL", `⚠ ${w.side} wall ${w.p} UNWINDING`,
              `Writers are covering the ${w.side} wall at ${w.p} — ${w.side === "CE" ? "upside breakout" : "downside break"} risk. Trade the breakout, don't fade.`, w.p);
        }
        if (topCE != null && prev.topCE != null && topCE !== prev.topCE) {   // writers rolled their defense
          const up = topCE > prev.topCE;
          fireWall("WALL_ROLL", up ? "BUY" : "SELL", `CE wall rolled ${prev.topCE} → ${topCE}`,
            up ? `Call writers moved resistance UP to ${topCE} — room opening above, bullish tilt.` : `Call writers moved resistance DOWN to ${topCE} — capping lower, bearish tilt.`, topCE);
        }
        if (topPE != null && prev.topPE != null && topPE !== prev.topPE) {
          const up = topPE > prev.topPE;
          fireWall("WALL_ROLL", up ? "BUY" : "SELL", `PE wall rolled ${prev.topPE} → ${topPE}`,
            up ? `Put writers moved support UP to ${topPE} — floor rising, bullish tilt.` : `Put writers moved support DOWN to ${topPE} — floor dropping, bearish tilt.`, topPE);
        }
      }
      wallPrevRef.current = { status, topCE, topPE };
    };
    // merge OI across two expiries per strike (for the "Both" wall view — total positioning)
    type OIRow = { strike: number; ce: { oi: number; oi_chg: number }; pe: { oi: number; oi_chg: number } };
    const mergeRows = (a: OIRow[], b: OIRow[]): OIRow[] => {
      const m = new Map<number, OIRow>();
      for (const r of [...a, ...b]) {
        const e = m.get(r.strike);
        if (!e) m.set(r.strike, { strike: r.strike, ce: { oi: r.ce.oi || 0, oi_chg: r.ce.oi_chg || 0 }, pe: { oi: r.pe.oi || 0, oi_chg: r.pe.oi_chg || 0 } });
        else { e.ce.oi += r.ce.oi || 0; e.ce.oi_chg += r.ce.oi_chg || 0; e.pe.oi += r.pe.oi || 0; e.pe.oi_chg += r.pe.oi_chg || 0; }
      }
      return [...m.values()].sort((x, y) => x.strike - y.strike);
    };
    const curExp = wallCurExpiry; const nextExp = wallNextExpiry;
    wallPrevRef.current = null; netPrevRef.current = null;   // reset wall-roll + flip baselines when the source expiry changes
    let inFlight = false;
    const go = async () => {
      if (inFlight) return;
      inFlight = true;
      if (!dead) { setWallLoading(true); setWallLoadError(""); }
      try {
        const cur = await api.chain(curExp);
        if (dead) return;
        const sp = cur?.spot || spot || cur?.atm;
        if (layers.gamma && cur?.rows?.length) setGexMap(computeGex(cur.rows, sp, 65));   // GEX stays on current expiry (needs greeks)
        if (layers.walls) {
          let rows: OIRow[] = cur?.rows ?? [];
          let maxPain: number | null = cur?.max_pain ?? null;
          if (wallExp === "next" && nextExp) { const nx = await api.chain(nextExp); if (nx?.rows?.length) { rows = nx.rows; maxPain = nx.max_pain ?? null; } }
          else if (wallExp === "both" && nextExp) { const nx = await api.chain(nextExp); rows = mergeRows(cur?.rows ?? [], nx?.rows ?? []); }
          if (dead) return;
          if (!rows.length) throw new Error("Selected expiry returned no OI rows; retaining previous walls");
          if (rows.length) {
            // Draw the recorded chain immediately; optional velocity must not block walls.
            const w = buildWalls(rows, sp); setWalls(w); detectWallEvents(w);
            // every near-spot level's % OI change → bubbles (CE & PE). pct = ΔOI ÷ current OI (matches the wall %).
            const lv: OiLevel[] = rows.filter((r) => sp && Math.abs(r.strike - sp) <= sp * 0.02).flatMap((r) => [
              { p: r.strike, side: "CE" as const, oi: r.ce.oi || 0, chg: r.ce.oi_chg || 0, pct: r.ce.oi ? (r.ce.oi_chg || 0) / r.ce.oi * 100 : null },
              { p: r.strike, side: "PE" as const, oi: r.pe.oi || 0, chg: r.pe.oi_chg || 0, pct: r.pe.oi ? (r.pe.oi_chg || 0) / r.pe.oi * 100 : null },
            ]);
            setOiLevels(lv);
            const wm: WriterMetrics | null = await api.writerMetrics(wallExp, 30, 15).catch((error) => {
              console.warn("OI wall velocity unavailable", error);
              return null;
            });
            if (dead) return;
            if (wm?.velocity) setWalls(buildWalls(rows, sp, wm.velocity));
            // writer balance: PCR (+ intraday trend), net PE−CE OI change, max-pain. Respects the expiry toggle.
            const ceOI = rows.reduce((s, r) => s + (r.ce.oi || 0), 0);
            const peOI = rows.reduce((s, r) => s + (r.pe.oi || 0), 0);
            const ceChg = rows.reduce((s, r) => s + (r.ce.oi_chg || 0), 0);
            const peChg = rows.reduce((s, r) => s + (r.pe.oi_chg || 0), 0);
            const net = peChg - ceChg;
            setWriterBal({ pcr: ceOI ? peOI / ceOI : null, ceOI, peOI, ceChg, peChg, net,
              maxPain, trend: wm?.pcr_trend ?? "flat", pcrPrev: wm?.pcr_prev ?? null, spot: sp });
            // writer-flip alert: net writer flow crosses from put-building (+) to call-building (−) or vice versa
            const pn = netPrevRef.current;
            if (pn != null && Math.sign(pn) !== Math.sign(net) && Math.abs(net) > 100000) {
              fireWall("WRITER_FLIP", net >= 0 ? "BUY" : "SELL",
                net >= 0 ? "🖊 Writer flow flipped to PUT building (bullish)" : "🖊 Writer flow flipped to CALL building (bearish)",
                net >= 0 ? `Put writers took over net OI (+${Math.round(net / 1e5)}L) — support building, bullish tilt.`
                         : `Call writers took over net OI (${Math.round(net / 1e5)}L) — resistance building, bearish tilt.`, sp);
            }
            netPrevRef.current = net;
          }
        }
      } catch (error) {
        if (!dead) setWallLoadError(error instanceof Error ? error.message : String(error));
        console.error("OI walls refresh failed", error);
      } finally {
        inFlight = false;
        if (!dead) setWallLoading(false);
      }
    };
    go(); const id = window.setInterval(go, 60000);
    return () => { dead = true; window.clearInterval(id); };
  }, [layers.walls, layers.gamma, wallExp, wallCurExpiry, wallNextExpiry]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    ofPrim.current.set({
      walls,
      bubbles: isBubblesOn ? bubbles : [],
      cells: isCellsOn ? fpBars.filter((b) => b.rows.length) : [],
      oiLevels: layers.walls ? oiLevels : []
    });
  }, [walls, bubbles, fpBars, isBubblesOn, isCellsOn, oiLevels, layers.walls]);
  // Primitives do not participate in candle autoscaling. Include the nearest walls
  // in automatic scale so valid bands/bubbles cannot silently sit off-screen.
  useEffect(() => {
    const s = series.current;
    if (!s) return;
    const wallSpot = spot ?? candles[candles.length - 1]?.close ?? 0;
    s.applyOptions({ autoscaleInfoProvider: (original: () => AutoscaleInfo | null) =>
      layers.walls && fitOiWalls ? oiWallRange(original(), walls, wallSpot) : original() });
  }, [layers.walls, fitOiWalls, walls, spot, candles]);
  useEffect(() => {
    if (layers.walls && fitOiWalls) series.current?.priceScale().setAutoScale(true);
  }, [layers.walls, fitOiWalls]);
  // ── stacked D / W / M volume nodes (refreshed every 5 min) + overextension entries ──
  // Fetched when the LAYER is on OR the ● ENTRY alert is armed. Previously both were tied to the layer, so
  // arming the alert chip and leaving the layer off produced silence — the entries were never computed, and
  // nothing said why. Drawing is still gated on the layer below, so alerts work without the chart clutter.
  // /api/nodes is cached 5 minutes server-side, so the extra arming costs ~1 request per 5 min.
  // Built ON TOP of isNodesOn (layer or delta-detail) rather than replacing it — that flag is also read by
  // the delta-detail and CVD paths, which should keep their own trigger. This only widens the FETCH.
  const wantNodes = !reviewing && (isNodesOn || (!!sig?.on && !!sig.kinds?.nodes));
  useEffect(() => {
    if (!wantNodes) { setNodesV(null); return; }
    let dead = false;
    const go = () => fetch("/api/nodes").then((r) => r.json()).then((j) => { if (!dead) setNodesV(j); }).catch(() => {});
    go(); const id = window.setInterval(go, 300000);
    return () => { dead = true; window.clearInterval(id); };
  }, [wantNodes]);
  const nEntries = useMemo(() => wantNodes && nodesV?.available
    ? findEntries(candles, analysis?.volume?.vwap ?? [], nodesV) : [], [wantNodes, nodesV, candles, analysis]);
  nEntRef.current = nEntries;
  // Entries must be gated on the LAYER exactly like the zones are. This line passed nEntries through
  // unconditionally, which was harmless only because nEntries used to be [] whenever the layer was off.
  // Widening the fetch to arm the ● ENTRY alert made it populate with the layer off, so the markers drew
  // on a chart with nothing selected. The alert still computes from nEntries; only drawing is gated.
  // nodesV may already hold today's zones from before review mode was entered — gate the draw
  // too, not just the fetch, or stale live zones would sit on a historical chart.
  useEffect(() => { const on = isNodesOn && !reviewing; ndPrim.current.set(on ? nodesV : null, on ? nEntries : []); }, [isNodesOn, reviewing, nodesV, nEntries]);
  // ── gamma map + 3 confirmations ──
  const rvMap = useMemo(() => new Map((analysis?.volume?.bars ?? []).map((b) => [b.time, b.rvol])), [analysis]);
  const emaArr = useMemo(() => layers.gamma && candles.length ? [emas(candles, 9), emas(candles, 21), emas(candles, 50)] : null, [layers.gamma, candles]);
  const gHits = useMemo(() => layers.gamma ? gammaHits(candles, rvMap, gexMap, analysis?.atr ?? 20, marketOpen) : [], [layers.gamma, candles, rvMap, gexMap, analysis, marketOpen]);
  gHitsRef.current = gHits;
  useEffect(() => { gmPrim.current.set(layers.gamma ? gexMap : null, gHits); }, [layers.gamma, gexMap, gHits]);
  // ── STDV manipulation leg projections ──
  const systemLeg = useMemo(() => {
    if (!layers.stdv || stdvLeg) return null;
    const legs = detectManipulationLegs(candles, tf, spot ?? undefined);
    return legs[0] || null;
  }, [layers.stdv, stdvLeg, candles, tf, spot]);
  const activeDisplayLeg = stdvLeg || systemLeg;

  useEffect(() => {
    if (!layers.stdv) {
      stdvPrim.current.setLeg(null);
      return;
    }
    stdvPrim.current.setLeg(activeDisplayLeg);
  }, [layers.stdv, activeDisplayLeg]);

  // ── Interactive 2-Click Chart Anchoring for STDV ──
  const [pickStep, setPickStep] = useState<1 | 2>(1);
  const [pickPoint1, setPickPoint1] = useState<{ time: number; price: number } | null>(null);

  useEffect(() => {
    const ch = chart.current;
    const s = series.current;
    if (!ch || !s || !isStdvPicking) {
      setPickStep(1);
      setPickPoint1(null);
      return;
    }

    const onClick = (p: MouseEventParams<Time>) => {
      if (!p.point || p.time == null) return;
      const t = p.time as number;
      const c = cMap.current.get(t) || candles.find((cand) => Math.abs(cand.time - t) < 30) || last.current;
      if (!c) return;

      const yMid = s.priceToCoordinate((c.high + c.low) / 2);
      const isTop = yMid !== null && p.point.y < yMid;
      const clickedPrice = isTop ? c.high : c.low;

      if (pickStep === 1) {
        setPickPoint1({ time: c.time, price: clickedPrice });
        setPickStep(2);
      } else if (pickStep === 2 && pickPoint1) {
        const point2 = { time: c.time, price: clickedPrice };
        const manualLeg = createManualStdvLeg(pickPoint1, point2, tf, spot ?? undefined);
        onStdvPicked?.(manualLeg);
        setPickStep(1);
        setPickPoint1(null);
      }
    };

    ch.subscribeClick(onClick);
    return () => ch.unsubscribeClick(onClick);
  }, [isStdvPicking, pickStep, pickPoint1, candles, tf, spot, onStdvPicked]);

  useEffect(() => {
    if (!isStdvPicking) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onCancelStdvPicking?.();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [isStdvPicking, onCancelStdvPicking]);
  useEffect(() => {
    const ch = chart.current; if (!ch) return;
    if (!layers.gamma || !emaArr) { emaS.current.forEach((x) => ch.removeSeries(x)); emaS.current = []; return; }
    if (!emaS.current.length) emaS.current = [["#facc15", 9], ["#a78bfa", 21], ["#f87171", 50]].map(([c, n]) => ch.addSeries(LineSeries,
      { color: c as string, lineWidth: 1, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false, title: `EMA ${n}` }));
    emaS.current.forEach((x, k) => x.setData(candles.map((c, i) => ({ time: c.time as UTCTimestamp, value: emaArr[k][i] }))));
  }, [layers.gamma, emaArr, candles]);
  // CVD in its own pane under the chart (created / removed with the layer)
  useEffect(() => {
    const ch = chart.current; if (!ch) return;
    if (!isCvdOn) { if (cvdS.current) { ch.removeSeries(cvdS.current); cvdS.current = null; } return; }
    if (!cvdS.current) {
      cvdS.current = ch.addSeries(BaselineSeries, { baseValue: { type: "price", price: 0 }, lineWidth: 2,
        topLineColor: "#10b981", topFillColor1: "rgba(16,185,129,0.40)", topFillColor2: "rgba(16,185,129,0.02)",
        bottomLineColor: "#ef4444", bottomFillColor1: "rgba(239,68,68,0.02)", bottomFillColor2: "rgba(239,68,68,0.40)",
        priceLineVisible: false, lastValueVisible: true, title: "CVD (est.)",
        priceFormat: { type: "custom", formatter: (v: number) => kfmt(v), minMove: 1 } }, 1);
      try { ch.panes()[1]?.setHeight(140); } catch { /* older engine */ }
    }
    const validBars = fpBars.filter((b) => b.rows.length);
    cvdS.current.setData(validBars.map((b) => ({ time: b.time as UTCTimestamp, value: b.cvd })));

    // Dynamic CVD momentum slope & color tinting
    if (validBars.length >= 3) {
      const lastBar = validBars[validBars.length - 1];
      const prevBar = validBars[Math.max(0, validBars.length - 4)];
      const dCvd3 = lastBar.cvd - prevBar.cvd;

      if (dCvd3 > 25000) {
        cvdS.current.applyOptions({
          title: `CVD · ACCEL BUY ⚡ (+${kfmt(dCvd3)}/3b)`,
          topLineColor: "#10b981",
          topFillColor1: "rgba(16,185,129,0.55)",
          topFillColor2: "rgba(16,185,129,0.05)",
          bottomLineColor: "#10b981",
          bottomFillColor1: "rgba(16,185,129,0.05)",
          bottomFillColor2: "rgba(16,185,129,0.25)",
        });
      } else if (dCvd3 < -25000) {
        cvdS.current.applyOptions({
          title: `CVD · AGGRESSIVE DUMP 🔻 (${kfmt(dCvd3)}/3b)`,
          topLineColor: "#ef4444",
          topFillColor1: "rgba(239,68,68,0.25)",
          topFillColor2: "rgba(239,68,68,0.05)",
          bottomLineColor: "#ef4444",
          bottomFillColor1: "rgba(239,68,68,0.05)",
          bottomFillColor2: "rgba(239,68,68,0.55)",
        });
      } else {
        cvdS.current.applyOptions({
          title: `CVD · ABSORPTION / CHOP ⚖️ (${kfmt(dCvd3)}/3b)`,
          topLineColor: "#f59e0b",
          topFillColor1: "rgba(245,158,11,0.40)",
          topFillColor2: "rgba(245,158,11,0.05)",
          bottomLineColor: "#f59e0b",
          bottomFillColor1: "rgba(245,158,11,0.05)",
          bottomFillColor2: "rgba(245,158,11,0.40)",
        });
      }
    }
  }, [isCvdOn, fpBars]);

  // ESTIMATED footprint on the chart (1m / 3m / 5m / 15m / 1h; closed candles from the desk's recorded FUT ticks)
  useEffect(() => {
    const want = !!layers.fp || !!layers.bias || !!layers.heat || !!isCvdOn || !!isCellsOn || !!isBubblesOn || !!isDeltaOn || !!sig?.on;
    // No 3m here: TF (api.ts) has no "3m" — that is a PE-CE table timeframe, not a chart one, even though
    // /api/footprints accepts 3m for other callers.
    const ok = want && (tf === "1m" || tf === "5m" || tf === "15m" || tf === "30m" || tf === "1h");
    if (!ok) { fpPrim.current.set([]); setFpBars([]); setFpMsg(layers.fp ? "Footprint: 1m / 5m / 15m / 30m / 1h only" : ""); if (!want) return; if (!ok) return; }
    let dead = false;
    // cover=session, not a flat bar count. A fixed n truncates by timeframe: 30 bars is 7h30 on 15m but
    // only 2h30 on 5m, which is why the 5m heatmap used to start at 13:00 while 15m showed the whole day
    // from the same recorded ticks. The server sizes the window from rules.json (footprints.heat_sessions).
    const go = () => fetch(`/api/footprints?tf=${tf}&cover=session`).then((r) => r.json()).then((j) => {
      if (dead) return;
      const raw: FpBar[] = j.available ? j.bars ?? [] : [];
      setFpBars(raw);
      setFpMsg(j.available ? "" : `Footprint: ${j.message ?? "no recorded ticks"}`);
    }).catch(() => {});
    go();
    const id = window.setInterval(go, 30000);
    return () => { dead = true; window.clearInterval(id); };
  }, [tf, layers.fp, layers.bias, layers.heat, isCvdOn, isCellsOn, isBubblesOn, isDeltaOn, sig?.on]);

  useEffect(() => {
    const a = analysis;
    const marks: { time: number; dir: string; label: string }[] = [];
    const ok = (k: string) => ["BOS", "MSS", "IFVG_BUY", "IFVG_SELL"].includes(k);
    for (const m of a?.markers ?? []) if (ok(m.kind)) marks.push({ time: m.time, dir: m.dir, label: m.kind.replace("_", " ") });
    for (const m of a?.crt?.markers ?? []) if (m.kind === "CRT_ENTRY") marks.push({ time: m.time, dir: m.dir, label: "CRT entry" });
    for (const m of a?.auction?.markers ?? []) if (m.kind === "AUCTION_ACCEPT") marks.push({ time: m.time, dir: m.dir, label: "auction ACC" });
    for (const m of a?.qml?.markers ?? []) if (m.kind.endsWith("_TRIG")) marks.push({ time: m.time, dir: m.dir, label: m.kind.replace("_TRIG", "") });
    const sigOn = !!sig?.on && !!onSignal && marketOpen;
    const confAll = layers.fp || sigOn ? confluence(fpBars, marks, BAR_SEC[tf] ?? 300) : [];
    const withConf = layers.fp ? confAll : [];
    const allB = layers.bias || sigOn ? computeBias(candles, a, fpBars, BAR_SEC[tf] ?? 300, biasPct, ocFeed, false, smtArg, cvdSignals, htfWickEmaData) : [];
    if (sigOn && sig && onSignal) {
      try { const lc = [...candles].reverse().filter((c) => c.closed !== false);
      const fnow = lc[0] && (layers.cvd || layers.cells || layers.walls || layers.bubbles) ? readAt(lc[0].time, ofArgsRef.current) : null;
      const fprev = lc[1] && fnow ? readAt(lc[1].time, ofArgsRef.current) : null;
      const wrapped = (al: AlertRow) => {
        onSignal(al);
        if (onAutoSignal && lc[0] && !al.kind.startsWith("SIG_CVD_")) {
          const px = al.price && al.price > 0 ? al.price : lc[0].close;
          const buy = al.side === "BUY";
          const wll = walls.filter((w) => buy ? w.side === "CE" && w.p > px : w.side === "PE" && w.p < px)
            .sort((x, y) => buy ? x.p - y.p : y.p - x.p)[0];
          onAutoSignal(al, { last: lc[0], atr: a?.atr ?? 20, nearWall: wll ? wll.p : null });
        }
      };
      dispatch(scanSignals({ tf, candles, a, prefs: sig, bias: allB, conf: confAll, ocActs, smt: smtArg, dols: computeDol(candles, a),
        gamma: gHitsRef.current, nodes: nEntRef.current, htfWicks, cvdSignals, ictLevels: instLevels,
        rbWickEmaAlerts: (showRbWickEma && htfWickEmaData?.live_alerts) ? htfWickEmaData.live_alerts : undefined,
        flow: fnow && lc[0] ? { time: lc[0].time, verdict: fnow.verdict, prev: fprev?.verdict ?? null, bull: fnow.bull, bear: fnow.bear, head: fnow.head } : null }), wrapped, tf); }
      catch { /* alerts must never break the chart */ }
    }
    biasMap.current = new Map(allB.map((r) => [r.time, r]));
    setBiasStatus(allB.at(-1) ?? null);
    const bias = layers.bias ? allB.filter((r) => r.verdict !== "MIXED") : [];
    fpPrim.current.set(withConf, bias, layers.heat ? fpBars.filter((b) => b.rows.length) : []);
  }, [fpBars, analysis, tf, layers.fp, layers.bias, layers.heat, candles, ocFeed, smtArg, sig, onSignal, marketOpen, ocActs, layers.cvd, layers.cells, layers.walls, layers.bubbles, layers.gamma, layers.nodes, layers.tsoup, htfWicks, biasPct, cvdSignals, showRbWickEma, htfWickEmaData]);

  // BANKNIFTY line (own hidden scale, so its shape lines up with NIFTY) — shown with the SMT layer
  useEffect(() => {
    bankS.current?.setData(layers.smt ? bank.map((b) => ({ time: b.time as UTCTimestamp, value: b.c })) : []);
  }, [bank, layers.smt]);

  // which line is the mouse on? (within 7 px) → name, value, meaning
  const LINE_MEAN: Record<string, string> = {
    VWAP: "average price paid today (volume-weighted). Above = buyers winning → prefer CE · below = sellers → prefer PE",
    "VWAP +1σ": "1 standard deviation above VWAP — normal upper stretch; first take-profit zone for longs",
    "VWAP −1σ": "1 standard deviation below VWAP — normal lower stretch; first take-profit zone for shorts",
    "VWAP +2σ": "very stretched above VWAP — don't chase CE here, pullback likely",
    "VWAP −2σ": "very stretched below VWAP — don't chase PE here, bounce likely",
    vPOC: "price with the MOST futures volume today — magnet; strong support / resistance",
    HVN: "high-volume price — smaller magnet / pause level", LVN: "low-volume price — price moves through it fast",
    "naked POC": "an old day's POC never revisited — price often returns to it",
    pPOC: "yesterday's point of control — reference level", "dev POC": "today's developing POC (time-based)",
    PDH: "previous day high — reaction / breakout level", PDL: "previous day low — reaction / breakdown level", PDC: "previous day close",
    EQ: "middle of the swing range — above = premium (sell zone), below = discount (buy zone)",
    BSL: "buy-side liquidity above old highs — stops sit here; expect a sweep", SSL: "sell-side liquidity below old lows — stops sit here; expect a sweep",
    AVWAP: "anchored VWAP — average price of everyone who entered since that swing; price above = those buyers in profit (support)",
    "ATM IV": "implied volatility of the ATM option — falling = premiums shrinking (bad for option buyers)",
    "D.O.L": "current draw-on-liquidity target — take profit there",
  };
  const linesAt = (t: number, y: number): { name: string; value: number; mean: string }[] => {
    const S = series.current; if (!S) return [];
    const out: { name: string; value: number; mean: string; d: number }[] = [];
    const near = (name: string, value: number, mean: string, ser = S as ISeriesApi<"Candlestick"> | ISeriesApi<"Line">) => {
      const yy = ser.priceToCoordinate(value); if (yy == null) return;
      const d = Math.abs((yy as number) - y); if (d <= 7) out.push({ name, value, mean, d });
    };
    const V = analysis?.volume;
    if (layers.volume && V) {
      const p = [...(V.vwap ?? [])].reverse().find((q) => q.time <= t);
      if (p) { const k = V.bands ?? [1, 2];
        near("VWAP", p.v, LINE_MEAN.VWAP); near("VWAP +1σ", p.v + k[0] * p.sd, LINE_MEAN["VWAP +1σ"]); near("VWAP −1σ", p.v - k[0] * p.sd, LINE_MEAN["VWAP −1σ"]);
        near("VWAP +2σ", p.v + (k[1] ?? 2) * p.sd, LINE_MEAN["VWAP +2σ"]); near("VWAP −2σ", p.v - (k[1] ?? 2) * p.sd, LINE_MEAN["VWAP −2σ"]); }
      for (const av of V.avwaps ?? []) { const q = [...av.points].reverse().find((z) => z.time <= t); if (q) near(`AVWAP · ${av.label}`, q.v, LINE_MEAN.AVWAP); }
      const iv = [...(V.iv ?? [])].reverse().find((q) => q.time <= t);
      if (iv && ivS.current) near("ATM IV", iv.iv, LINE_MEAN["ATM IV"], ivS.current);
    }
    for (const g of zones.current.segs) {
      if (g.from > t) continue;
      const key = Object.keys(LINE_MEAN).find((k) => g.label.startsWith(k) || g.label.includes(k)) ?? "";
      near(g.label, g.price, LINE_MEAN[key] ?? "");
    }
    return out.sort((a2, b2) => a2.d - b2.d).slice(0, 2);
  };

  // live reading of every volume line for the hovered candle
  const linesRead = (t: number): { txt: string; tone: "bull" | "bear" | "warn" | "flat" }[] => {
    const V = analysis?.volume, c = cMap.current.get(t);
    if (!layers.volume || !V?.available || !c) return [];
    const out: { txt: string; tone: "bull" | "bear" | "warn" | "flat" }[] = [];
    const p = [...(V.vwap ?? [])].reverse().find((q) => q.time <= t);
    if (p) {
      const z = p.sd > 0 ? (c.close - p.v) / p.sd : 0;
      out.push({ txt: `VWAP ${p.v.toFixed(0)}: close ${c.close > p.v ? "ABOVE" : "BELOW"} by ${Math.abs(c.close - p.v).toFixed(0)} pts → ${c.close > p.v ? "buyers winning, prefer CE" : "sellers winning, prefer PE"}`, tone: c.close > p.v ? "bull" : "bear" });
      out.push({ txt: `Stretch ${z >= 0 ? "+" : ""}${z.toFixed(1)}σ → ${z >= 2 ? "very stretched UP — don't chase CE, pullback likely" : z <= -2 ? "very stretched DOWN — don't chase PE, bounce likely" : z >= 1 ? "upper band — first TP zone for longs" : z <= -1 ? "lower band — first TP zone for shorts" : "normal range around VWAP"}`, tone: Math.abs(z) >= 2 ? "warn" : "flat" });
    }
    const vp = V.today?.poc;
    if (vp) { const d = c.close - vp;
      out.push({ txt: `vPOC ${vp.toFixed(0)} is ${Math.abs(d).toFixed(0)} pts ${d > 0 ? "below" : "above"} → ${Math.abs(d) <= 10 ? "price AT the magnet — expect a reaction" : d > 0 ? "support / magnet below" : "resistance / magnet above"}`, tone: "flat" });
      const hv = (V.today?.hvn ?? []).filter((h) => Math.abs(h - vp) > (V.today?.step ?? 5)).sort((x, y) => Math.abs(x - c.close) - Math.abs(y - c.close))[0];
      if (hv != null) out.push({ txt: `Nearest HVN ${hv.toFixed(0)} (${Math.abs(c.close - hv).toFixed(0)} pts ${c.close > hv ? "below" : "above"}) → pause level`, tone: "flat" });
    }
    for (const av of V.avwaps ?? []) { const q = [...av.points].reverse().find((z) => z.time <= t); if (!q) continue;
      const up = c.close > q.v, buyers = av.dir === "bull";
      out.push({ txt: `AVWAP ${av.label} ${q.v.toFixed(0)}: close ${up ? "above" : "below"} → ${buyers ? (up ? "buyers since that low in profit — up-move healthy" : "buyers since the low now losing — up-move weakening") : (up ? "sellers since that high losing — resistance broken" : "sellers since that high in profit — acts as resistance")}`,
        tone: (buyers ? up : up) ? "bull" : "bear" }); }
    const iv = (V.iv ?? []).filter((q) => q.time <= t); const ivn = iv[iv.length - 1], iv0 = iv[Math.max(0, iv.length - 7)];
    if (ivn && iv0) { const ch = ivn.iv - iv0.iv;
      out.push({ txt: `ATM IV ${ivn.iv.toFixed(1)} (${ch >= 0 ? "+" : ""}${ch.toFixed(1)} recent) → ${ch <= -0.3 ? "falling: premiums shrinking — bad for option BUYERS, need a fast move" : ch >= 0.3 ? "rising: premiums inflating — moves expected" : "flat"}`, tone: ch <= -0.3 ? "warn" : "flat" }); }
    const bars = V.bars ?? [], bi = bars.findIndex((b) => b.time === t), vb = bi >= 0 ? bars[bi] : undefined;
    if (vb) {
      const prev = bars.slice(Math.max(0, bi - 10), bi), avg = prev.length ? prev.reduce((x, b) => x + b.v, 0) / prev.length : 0;
      const rel = avg ? vb.v / avg : 1, body = Math.abs(c.close - c.open), rng = Math.max(c.high - c.low, 0.01);
      const col = vb.rvol != null && vb.rvol >= 1.5 ? "BRIGHT" : vb.rvol != null && vb.rvol <= 0.7 ? "dark / faded" : "medium";
      out.push({ txt: `Volume bar: ${(vb.v / 1000).toFixed(0)}k, ${col} (RVOL ${vb.rvol?.toFixed(1) ?? "—"}× vs normal for this time · ${rel.toFixed(1)}× the last 10 bars) → ${vb.rvol != null && vb.rvol >= 1.5 ? "institutions active — trust this candle's direction" : vb.rvol != null && vb.rvol <= 0.7 ? "thin volume — weak move, don't trust breakouts" : "normal participation"}`,
        tone: vb.rvol != null && vb.rvol >= 1.5 ? (c.close >= c.open ? "bull" : "bear") : "flat" });
      // effort vs result (Wyckoff): big volume + small body = absorption; small volume + big body = easy move / no opposition
      if (rel >= 1.8 && body / rng < 0.35) out.push({ txt: "Effort vs result: BIG volume but small body → absorption — the other side is soaking it up, turn risk", tone: "warn" });
      else if (rel <= 0.6 && body / rng > 0.6) out.push({ txt: `Effort vs result: small volume, big ${c.close >= c.open ? "green" : "red"} body → no opposition, but weak conviction — likely to be retested`, tone: "warn" });
      const last3 = bars.slice(Math.max(0, bi - 2), bi + 1);
      if (last3.length === 3 && last3.every((b) => !b.up) && last3.every((b) => (b.rvol ?? 1) <= 0.8)) out.push({ txt: "3 red bars on LOW volume → selling without participation — a pullback, not a real reversal (yet)", tone: "bull" });
      if (last3.length === 3 && last3.every((b) => b.up) && last3.every((b) => (b.rvol ?? 1) <= 0.8)) out.push({ txt: "3 green bars on LOW volume → buying without participation — rally may stall", tone: "bear" });
    }
    return out;
  };

  // PE−CE Action lane & Dynamic Structural SL Line
  useEffect(() => {
    const ch = chart.current; if (!ch) return;
    const on = !!layers.ocAct && ocActs.length > 0;
    if (on && !ocLane.current) {
      const idx = ch.panes().length;
      ocLane.current = ch.addSeries(HistogramSeries, { priceLineVisible: false, lastValueVisible: false, title: "PE−CE action", base: 0 }, idx);
      try { ch.panes()[idx]?.setStretchFactor(0.6); } catch { /* */ }
    } else if (!on && ocLane.current) { ch.removeSeries(ocLane.current); ocLane.current = null; }

    if (ocLane.current) {
      ocLane.current.setData(ocActs.map((x) => {
        let val = x.v === 0 ? 0.25 : x.v;
        let col = "rgba(100,116,139,0.45)";
        if (x.kind === "PULLBACK") {
          val = x.v > 0 ? 1.5 : -1.5;
          col = x.v > 0 ? "#10b981" : "#f43f5e";
        } else if (x.kind === "ENTRY") {
          val = x.v > 0 ? 1.2 : -1.2;
          col = x.v > 0 ? "#34d399" : "#fb7185";
        } else if (x.kind === "ADD") {
          val = x.v > 0 ? 2.0 : -2.0;
          col = "#f97316";
        } else if (x.kind === "BOOK") {
          val = 1.0;
          col = "#fbbf24";
        } else if (x.kind === "HOLD") {
          val = x.v;
          col = "#64748b";
        }
        return {
          time: x.time as UTCTimestamp,
          value: val,
          color: col,
        };
      }));
    }

    // Dynamic Structural SL Line
    if (ocSlS.current) {
      const slPoints = on
        ? ocActs
            .filter((x) => x.smartRow?.dynamicSl != null && (x.kind === "ENTRY" || x.kind === "PULLBACK" || x.kind === "ADD" || x.kind === "HOLD"))
            .map((x) => ({
              time: x.time as UTCTimestamp,
              value: x.smartRow!.dynamicSl!,
            }))
        : [];
      ocSlS.current.setData(slPoints);
    }
  }, [ocActs, layers.ocAct]);

  // heatmap narrative for one candle: where the volume sat, what that means, what to do
  const heatRead = (t: number): string[] => {
    if (!layers.heat) return [];
    const b = fpBars.find((x) => x.time === t), c = cMap.current.get(t);
    // Say WHY there is nothing rather than returning silence. Hovering a candle outside the recorded
    // window used to show a blank box, which reads as "the heatmap is broken" when the real answer is
    // "no ticks were recorded for this candle".
    if (!c) return [];
    if (!b || !b.rows.length) {
      return [`No footprint for this candle — the desk has no recorded futures ticks for ${hm(t)}.`,
              "⚪ The ladder is built from ticks this desk recorded itself, so a candle from before the desk "
              + "was running (or from a session it missed) has nothing to show. Nothing is wrong with the "
              + "heatmap; there is simply no tape for this bar. Read price structure here instead."];
    }
    const rng = Math.max(c.high - c.low, 0.01), tot = b.rows.reduce((x, r) => x + r.v, 0) || 1;
    const third = (p: number) => ((p - c.low) / rng < 0.34 ? "low" : (p - c.low) / rng > 0.66 ? "high" : "mid");
    const share = { low: 0, mid: 0, high: 0 } as Record<string, number>;
    for (const r of b.rows) share[third(r.p)] += r.v / tot;
    const hot = [...b.rows].sort((x, y) => y.v - x.v)[0];
    const where = third(hot.p), closePos = (c.close - c.low) / rng, green = c.close >= c.open;
    const pct = (x: number) => `${Math.round(x * 100)} %`;
    const out = [`Hottest row ${hot.p.toLocaleString("en-IN")} (${pct(hot.v / tot)} of the candle's volume) — in the ${where.toUpperCase()} of the candle · split low ${pct(share.low)} / mid ${pct(share.mid)} / high ${pct(share.high)}`];
    if (where === "high" && closePos < 0.5) out.push("🔴 Heavy volume at the TOP but the close fell away → buyers got absorbed by sellers up there (distribution). PE bias; don't buy CE above this band.");
    else if (where === "low" && closePos > 0.5) out.push("🟢 Heavy volume at the BOTTOM and a strong close → sellers got absorbed by buyers (accumulation). CE bias; this band is support.");
    else if (where === "high" && closePos >= 0.5) out.push(`🟢 Volume built at the top AND it closed there → buyers ACCEPTED higher prices (initiative). Trend ${green ? "up" : ""} is healthy; next pullback to this band = CE entry area.`);
    else if (where === "low" && closePos <= 0.5) out.push("🔴 Volume built at the bottom AND it closed there → sellers accepted lower prices. Weak; this band is now resistance on a bounce.");
    else out.push(`⚪ Volume concentrated in the MIDDLE → two-sided, balanced candle (no one won). Wait for the next candle to leave this band.`);
    if (share.high + share.low > 0.75 && share.mid < 0.2) out.push("Both ends hot, empty middle → price jumped through the middle fast; expect a retest of that gap.");
    const bodyHot = b.rows.filter((r) => r.p <= Math.max(c.open, c.close) && r.p >= Math.min(c.open, c.close)).reduce((x, r) => x + r.v, 0) / tot;
    if (Math.abs(c.close - c.open) / rng > 0.6 && bodyHot < 0.35) out.push("Big body but little volume inside it → easy move with no fight, often retested.");
    if (b.delta !== 0) out.push(`Delta ${kfmt(b.delta)} → ${b.delta > 0 ? "aggressive buyers" : "aggressive sellers"} ${(b.delta > 0) === green ? "agree with the candle colour (genuine)" : "DISAGREE with the candle colour (absorption — be careful)"}.`);

    // The read above describes the candle; these two lines give you something to actually place an order
    // against. The value band is where ~70 % of the candle's volume traded — the part price has to leave
    // for the bar to mean anything, and the natural place to put a stop beyond.
    const sorted = [...b.rows].sort((x, y) => y.v - x.v);
    let acc = 0;
    const va: number[] = [];
    for (const r of sorted) { if (acc / tot >= 0.7) break; acc += r.v; va.push(r.p); }
    const vaLo = Math.min(...va), vaHi = Math.max(...va);
    const inr0 = (x: number) => x.toLocaleString("en-IN", { maximumFractionDigits: 0 });
    out.push(`LEVELS — POC ${inr0(hot.p)} · value band ${inr0(vaLo)}–${inr0(vaHi)} (70 % of volume). `
      + `A stop belongs beyond the band, not inside it: ${inr0(vaLo)} for longs, ${inr0(vaHi)} for shorts.`);

    const last = candles.length ? candles[candles.length - 1].close : null;
    if (last != null) {
      const d = last - hot.p;
      out.push(Math.abs(d) <= Math.max(rng * 0.25, 2)
        ? `NOW — price ${inr0(last)} is sitting ON this POC (${d >= 0 ? "+" : ""}${d.toFixed(1)}). This is the fairest price in the band: the worst place to initiate and the best place to be filled. Let it leave the band first.`
        : d > 0
          ? `NOW — price ${inr0(last)} is ${d.toFixed(1)} ABOVE this POC, so this band is below the market and acts as support while it holds. Losing it turns it into resistance.`
          : `NOW — price ${inr0(last)} is ${Math.abs(d).toFixed(1)} BELOW this POC, so this band is overhead supply. Rallies into it are where sellers were last active; it must be reclaimed on volume before a long is worth taking.`);
    }
    return out;
  };

  const hm = (t: number) => { const d = new Date(t * 1000); return `${String(d.getUTCHours()).padStart(2, "0")}:${String(d.getUTCMinutes()).padStart(2, "0")}`; };
  const formatTickerTime = (t: number) => {
    const d = new Date(t * 1000);
    const lastCandle = candles.length ? candles[candles.length - 1] : null;
    const lastD = lastCandle ? new Date(lastCandle.time * 1000) : new Date();
    const isToday = d.getUTCFullYear() === lastD.getUTCFullYear() &&
                    d.getUTCMonth() === lastD.getUTCMonth() &&
                    d.getUTCDate() === lastD.getUTCDate();
    const timeStr = `${String(d.getUTCHours()).padStart(2, "0")}:${String(d.getUTCMinutes()).padStart(2, "0")}`;
    if (isToday) return `Today ${timeStr}`;
    const months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
    return `${d.getUTCDate()} ${months[d.getUTCMonth()]} ${timeStr}`;
  };
  const hc = hover ? cMap.current.get(hover.t) : undefined;
  const hev = hover ? evRef.current.get(hover.t) ?? [] : [];
  const vb = hover && analysis?.volume?.bars ? analysis.volume.bars.find((b) => b.time === hover.t) : undefined;
  const hcCrvolBar = hover && analysis?.volume?.crvol ? analysis.volume.crvol.find((b) => b.time === hover.t) : undefined;
  const ofOn = !!(isCvdOn || isCellsOn || layers.walls || isBubblesOn);
  const ofArgs = { fp: fpBars, bubbles, walls, zones: ofPrim.current.zones, atr: analysis?.atr ?? 20, candles };
  ofArgsRef.current = ofArgs;
  const ofr = hover && ofOn ? readAt(hover.t, { ...ofArgs, px: series.current?.coordinateToPrice(hover.y) ?? null }) : null;
  const lastClosed = ofOn ? [...candles].reverse().find((c) => c.closed !== false) : undefined;
  const ofNow = lastClosed ? readAt(lastClosed.time, ofArgs) : null;
  const VC: Record<string, string> = { BUY: "bg-bull-500 text-ink-900", SELL: "bg-bear-500 text-white", WAIT: "bg-slate-600 text-white" };
  const TC: Record<string, string> = { bull: "text-bull-400", bear: "text-bear-400", warn: "text-amber-300", info: "text-slate-300" };
  const gIdx = hover && layers.gamma && emaArr ? candles.findIndex((c) => c.time === hover.t) : -1;
  const gconf = gIdx >= 60 && emaArr ? confirm3(gIdx, candles, emaArr[0], emaArr[1], emaArr[2], rvMap, gexMap, analysis?.atr ?? 20) : null;
  const gLast = layers.gamma && emaArr ? (() => { const i = candles.map((c) => c.closed !== false).lastIndexOf(true);
    return i >= 60 ? confirm3(i, candles, emaArr[0], emaArr[1], emaArr[2], rvMap, gexMap, analysis?.atr ?? 20) : null; })() : null;
  const nread = hover && isNodesOn ? nodesRead(hover.t, series.current?.coordinateToPrice(hover.y) ?? null, nodesV, nEntries, candles[candles.length - 1]?.close ?? null) : [];
  // DOM book confirm / warn at the hovered price (last 5 min of events near this level)
  const bookNote: { ok: boolean; txt: string } | null = (() => {
    const hp = hover ? (series.current?.coordinateToPrice(hover.y) ?? null) : null;
    if (hp == null || !bookEv.length) return null;
    const now = Date.now() / 1000, tol = Math.max(5, (analysis?.atr ?? 20) * 0.25);
    const near = bookEv.filter((e) => Math.abs(e.price - hp) <= tol && now - e.ts < 300);
    if (!near.length) return null;
    const spoof = near.find((e) => e.kind === "SPOOF");
    const pulls = near.filter((e) => e.kind === "PULLED");
    const walls = near.filter((e) => e.kind === "WALL");
    if (spoof) return { ok: false, txt: spoof.why };
    if (pulls.length >= 2) return { ok: false, txt: `this level was pulled ${pulls.length}× in the last few min — spoofed, don't lean on it` };
    if (walls.length) { const w = [...walls].sort((a, b) => b.qty - a.qty)[0];
      return { ok: true, txt: `real ${w.side} wall here — ${Math.round(w.qty / 65)} lots held 20s+, genuine size behind this level` }; }
    if (pulls.length === 1) return { ok: false, txt: `${Math.round(pulls[0].qty / 65)} lots pulled here once — watch for a fake` };
    return null;
  })();
  const ob = hover && layers.oi ? (analysis?.volume?.oi_line ?? []).find((x) => x.time === hover.t) : undefined;
  const oiHealth = analysis?.volume?.oi_health;
  const oiLatest = oiHealth?.sample_quality === "ok" ? analysis?.volume?.oi_line?.at(-1) : undefined;
  const oiAge = oiHealth?.sample_ts != null ? Math.max(0, Math.floor(oiClock / 1000 - oiHealth.sample_ts - 60)) : null;
  const oiState = oiHealth?.state !== "market_closed" && oiAge != null && oiHealth?.freshness_max_age_s != null && oiAge > oiHealth.freshness_max_age_s ? "stale" : oiHealth?.state;
  const oiAt = oiHealth?.sample_ts ? new Date(oiHealth.sample_ts * 1000).toLocaleTimeString("en-IN", { timeZone: "Asia/Kolkata", hour12: false }) : "—";
  const oiNumber = (value: number | null | undefined, suffix = "") => value == null ? "—" : `${value >= 0 ? "+" : ""}${value.toLocaleString("en-IN", { maximumFractionDigits: 2 })}${suffix}`;
  const OI_READ: Record<string, [string, string, string]> = {
    "long build-up": ["🟢 LONG BUILD-UP", "chart price ▲ + OI ▲ — conventional long build-up; positioning context, not proof of who initiated.", "text-bull-400"],
    "short covering": ["🟢 SHORT COVERING", "chart price ▲ + OI ▼ — conventional short covering; not an automatic entry or exit.", "text-green-300"],
    "short build-up": ["🔴 SHORT BUILD-UP", "chart price ▼ + OI ▲ — conventional short build-up; positioning context only.", "text-bear-400"],
    "long unwinding": ["🔴 LONG UNWINDING", "chart price ▼ + OI ▼ — conventional long unwinding; a rebound is not guaranteed.", "text-red-300"],
  };
  const lns = hover ? linesAt(hover.t, hover.y) : [];
  const lread = hover ? linesRead(hover.t) : [];
  const hread = hover ? heatRead(hover.t) : [];
  const br = hover && layers.bias ? biasMap.current.get(hover.t) : undefined;
  const fb = hover && layers.fp ? fpPrim.current.bars.find((b) => b.time === hover.t) : undefined;
  const W = box.current?.clientWidth ?? 900;
  const legend = LEGEND.filter(([k]) => layers[k]);
  const hoveredOcAct = hover && layers.ocAct ? ocActs.find((x) => x.time === hover.t) : null;
  const hasOcMarker = !!hoveredOcAct && (hoveredOcAct.kind !== "NONE" || hoveredOcAct.v !== 0);
  const cursorPriceAtHover = hover && series.current ? series.current.coordinateToPrice(hover.y) : null;
  const hoveredWall = layers.walls && cursorPriceAtHover != null
    ? walls.find((w) => Math.abs(cursorPriceAtHover - w.p) <= 18)
    : null;
  const oiPane = oiS.current?.doi.getPane().paneIndex();
  const hasOiMarker = !!(hover && (hover.pane ?? 0) === 0 && hev.some((event) => event.kind === "OI_SURGE") && hc && cursorPriceAtHover != null && cursorPriceAtHover >= hc.high);
  const hasOiCard = !!(layers.oi && hover && ((hover.pane != null && hover.pane === oiPane) || hasOiMarker));
  const oiHistorical = !!(ob && (ob.time !== candles.filter((c) => c.closed !== false).at(-1)?.time || oiState === "market_closed"));
  const oiGuide = oiDecisionGuide(ob, { historical: oiHistorical, unavailable: !oiHistorical && (oiState === "stale" || oiState === "missing"), high: hc?.high, low: hc?.low });
  const hoveredCvdDiv = (hover && isCvdOn)
    ? cvdDivergences.find((d) => Math.abs(d.time - hover.t) < (BAR_SEC[tf] ?? 300))
    : null;
  const hoveredFpBar = (hover && (isCvdOn || layers.fp || isCellsOn || isDeltaOn))
    ? fpBars.find((b) => Math.abs(b.time - hover.t) < (BAR_SEC[tf] ?? 300))
    : null;
  const isHoveringCvdPane = !!(hover && isCvdOn && cvdS.current && hover.y > (box.current?.clientHeight ?? 500) - 140);
  const hasCvdCard = !!(hover && isCvdOn && (hoveredCvdDiv || (isHoveringCvdPane && hoveredFpBar)));
  const isHoveringRvolPane = !!(hover && layers.rvol && rvS.current && hover.y > (box.current?.clientHeight ?? 500) - (oiS.current ? 160 : 120));
  const hoveredCrvolBar = hover && layers.rvol ? (
    (analysis?.volume?.crvol ?? []).find((b) => b.time === hover.t) ??
    (analysis?.volume?.crvol ?? []).find((b) => Math.abs(b.time - hover.t) < ((BAR_SEC[tf] ?? 300) / 2))
  ) : undefined;
  const hoveredVolBar = hover ? (
    (analysis?.volume?.bars ?? []).find((b) => b.time === hover.t) ??
    (analysis?.volume?.bars ?? []).find((b) => Math.abs(b.time - hover.t) < ((BAR_SEC[tf] ?? 300) / 2))
  ) : undefined;

  // Keep crosshair move hit-testing engine synchronized with latest data
  onCrosshairMoveRef.current = (p: MouseEventParams<Time>) => {
    if ((p.paneIndex ?? 0) !== 0) {
      if (sniperHover) setSniperHover(null);
      if (gtfHover) setGtfHover(null);
      return;
    }
    const pt = p.point;
    if (!pt) {
      if (sniperHover) setSniperHover(null);
      if (gtfHover) setGtfHover(null);
      return;
    }
    const cursorPrice = series.current ? series.current.coordinateToPrice(pt.y) : null;
    if (cursorPrice == null) {
      if (sniperHover) setSniperHover(null);
      if (gtfHover) setGtfHover(null);
      return;
    }

    const cursorTime = p.time as number | undefined;

    // 1. Interactive GTF Institutional Zone Hit-Testing
    if (isGtfOn && gtfResult.zones.length && series.current) {
      const lastTime = candles.length ? candles[candles.length - 1].time : undefined;
      const gtfMatched = hitTestGtfZone(
        gtfResult.zones,
        cursorTime,
        cursorPrice,
        (t) => chart.current?.timeScale().timeToCoordinate(t as Time) ?? null,
        (px) => series.current?.priceToCoordinate(px) ?? null,
        pt.x,
        pt.y,
        lastTime
      );
      if (gtfMatched) {
        setGtfHover({ zone: gtfMatched, x: pt.x, y: pt.y });
      } else {
        if (gtfHover) setGtfHover(null);
      }
    } else {
      if (gtfHover) setGtfHover(null);
    }

    // 2. Sniper Hover Hit-Testing (Adv PA)
    if (!layers.advPa || !analysis?.pa_advanced?.sniper?.hover_items?.length || !series.current) {
      if (sniperHover) setSniperHover(null);
      return;
    }

    const hoverItems = analysis.pa_advanced.sniper.hover_items;
    const atr = analysis.atr || 15.0;
    const barSec = BAR_SEC[tf] ?? 300;

    const matched = hitTestSniperHover(
      hoverItems,
      cursorTime,
      cursorPrice,
      atr,
      barSec,
      (px) => series.current?.priceToCoordinate(px) ?? null,
      pt.y
    );

    if (matched) {
      setSniperHover({ item: matched, x: pt.x, y: pt.y });
    } else {
      if (sniperHover) setSniperHover(null);
    }
  };

  return (
    <div
      className="relative h-full w-full"
      onMouseLeave={() => {
        setHover(null);
        setSniperHover(null);
        setGtfHover(null);
      }}
    >
      <div ref={box} className="h-full w-full" />
      {/* cursor TIME badge (top-centre) — date + HH:MM of the hovered candle, all timeframes */}
      {hover && (() => {
        const d = new Date((hover.t as number) * 1000);   // chart times are IST-shifted → getUTC* gives IST
        const mon = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"][d.getUTCMonth()];
        const lbl = tf === "1D" ? `${d.getUTCDate()} ${mon} ${d.getUTCFullYear()}`
          : `${d.getUTCDate()} ${mon} · ${String(d.getUTCHours()).padStart(2, "0")}:${String(d.getUTCMinutes()).padStart(2, "0")}`;
        return (
          <div className="pointer-events-none absolute left-1/2 top-1.5 z-30 -translate-x-1/2 rounded bg-[#060b16]/90 px-2 py-0.5 text-[11px] font-semibold tracking-wide text-slate-200 ring-1 ring-white/10">
            🕒 {lbl} <span className="text-slate-500">· {tf}</span>
          </div>
        );
      })()}

      {/* 🎯 Real-Time PE−CE A+ Setup Canvas Banner */}
      {ocAlertBanner && (
        <div
          className={`pointer-events-auto absolute top-7 left-1/2 -translate-x-1/2 z-40 flex items-center gap-2.5 px-3.5 py-1.5 rounded-lg border shadow-2xl backdrop-blur-md animate-bounce duration-1000 ${
            ocAlertBanner.kind === "bear"
              ? "bg-rose-950/95 border-rose-500/80 text-rose-200 ring-2 ring-rose-500/40"
              : "bg-emerald-950/95 border-emerald-500/80 text-emerald-200 ring-2 ring-emerald-500/40"
          }`}
        >
          <span className="text-[12px] font-extrabold tracking-wide">{ocAlertBanner.text}</span>
          <button
            onClick={() => setOcAlertBanner(null)}
            className="ml-1 text-xs font-bold text-slate-400 hover:text-white px-1"
            title="Dismiss banner"
          >
            ✕
          </button>
        </div>
      )}
      {/* ONE top-right stack. These were three separately-positioned clusters, two of them anchored
          at the SAME spot -- this control bar at top-2 z-20, the pane/engine toggles at top-2
          z-[60], and the ceiling pill at top-12 -- so they drew on top of each other and the
          labels became unreadable. Adding R1-R4/S1-S4, Gaps and Gann Sq9 to this bar made it
          worse, because 55% of the width could not hold them.
          They are now children of a single wrapping flex row: items sit side by side and wrap onto
          a new line when the width runs out, so the corner grows downward instead of piling up.
          A flex child must not position itself, so the two inner blocks keep their markup and lose
          only their absolute anchors. Nested popovers are untouched -- they position against their
          own relative parents. */}
      {/* Top-Right Control Bar: ⚡ 3-Chart Trinity Suite + ⚡ Strike Map 2.0 + Expiry Selector */}
      <div className="pointer-events-auto absolute right-2 top-2 z-[60] flex flex-wrap items-center justify-end gap-x-2 gap-y-1.5 max-w-[80%]">
        <AlgoCockpit />
        <button
          onClick={toggleIctLevels}
          className={`flex items-center gap-1.5 rounded bg-[#060b16]/90 px-2 py-1 text-[10px] font-black border transition-all shadow-lg backdrop-blur ${
            localIctLevels
              ? "bg-amber-500/20 border-amber-500/80 text-amber-300 ring-1 ring-amber-500/40"
              : "border-slate-700/60 text-slate-400 hover:text-slate-200"
          }`}
          title="Toggle Institutional ICT Reference Levels: PDH / PDL (Dashed Gold) and 09:15 Open (Dotted Cyan)"
        >
          <span>{localIctLevels ? "☑" : "☐"} ICT Levels: PDH/PDL/Open</span>
          {instLevels && (
            <span className={`text-[9px] px-1 rounded font-bold ${
              instLevels.is_breakout ? "bg-emerald-500/30 text-emerald-300" :
              instLevels.is_sweep ? "bg-amber-500/30 text-amber-300" :
              "bg-slate-800 text-slate-300"
            }`}>
              {instLevels.status === "PDH_TRUE_BREAKOUT" ? "PDH BREAKOUT" :
               instLevels.status === "PDL_TRUE_BREAKDOWN" ? "PDL BREAKDOWN" :
               instLevels.status === "PDH_SWEEP_REJECTION" ? "PDH SWEEP" :
               instLevels.status === "PDL_SWEEP_REJECTION" ? "PDL SWEEP" : "RANGE"}
            </span>
          )}
        </button>

        {/* Rejection Blocks (RB) Section: Checkbox 1, Checkbox 2 & Gann Time Cycle HUD */}
        <button
          onClick={toggleRbPivotsGaps}
          onMouseEnter={() => setShowRbGuidance(true)}
          onMouseLeave={() => setShowRbGuidance(false)}
          className={`flex items-center gap-1.5 rounded bg-[#060b16]/90 px-2 py-1 text-[10px] font-black border transition-all shadow-lg backdrop-blur ${
            showRbPivotsGaps
              ? "bg-rose-500/20 border-rose-500/80 text-rose-300 ring-1 ring-rose-500/40"
              : "border-slate-700/60 text-slate-400 hover:text-slate-200"
          }`}
          title="Toggle Rejection Blocks: Historical Swing R1-R4 (Red), S1-S4 (Green) & Unfilled Gaps (Gold/Purple)"
        >
          <span>{showRbPivotsGaps ? "☑" : "☐"} R1–R4 / S1–S4 & Gaps</span>
          {gannData?.resistances?.[0]?.hit && (
            <span className="text-[9px] px-1 rounded font-bold bg-rose-500/30 text-rose-300">
              R1 HIT
            </span>
          )}
        </button>

        <button
          onClick={toggleRbGann}
          onMouseEnter={() => setShowRbGuidance(true)}
          onMouseLeave={() => setShowRbGuidance(false)}
          className={`flex items-center gap-1.5 rounded bg-[#060b16]/90 px-2 py-1 text-[10px] font-black border transition-all shadow-lg backdrop-blur ${
            showRbGann
              ? "bg-teal-500/20 border-teal-500/80 text-teal-300 ring-1 ring-teal-500/40"
              : "border-slate-700/60 text-slate-400 hover:text-slate-200"
          }`}
          title="Toggle Gann Square of 9 Levels (±45°, ±90°, ±135°, ±180°)"
        >
          <span>{showRbGann ? "☑" : "☐"} Gann Sq9 & Time Cycles</span>
        </button>

        <button
          onClick={toggleRbGannPdh}
          onMouseEnter={() => setShowPdhGuidance(true)}
          onMouseLeave={() => setShowPdhGuidance(false)}
          className={`flex items-center gap-1.5 rounded bg-[#060b16]/90 px-2 py-1 text-[10px] font-black border transition-all shadow-lg backdrop-blur cursor-help ${
            showRbGannPdh
              ? "bg-fuchsia-500/20 border-fuchsia-500/80 text-fuchsia-300 ring-1 ring-fuchsia-500/40"
              : "border-slate-700/60 text-slate-400 hover:text-slate-200"
          }`}
          title={
            "GANN Sq9 ANCHORED ON YESTERDAY'S HIGH — context, not a signal.\n\n"
            + "WHAT IT IS\n"
            + "Levels from (√PDH + angle/180)², at ±45°, ±90°, ±135°, ±180°. Only the ones price "
            + "could plausibly reach today are drawn; the rest of the ladder is off-screen by design.\n\n"
            + "HOW TO USE IT\n"
            + "As a place to WATCH, never a place to enter. Price arriving at a level is not a "
            + "trade. Wait for the 15m to give you a doji, hammer or engulfing candle AT the level "
            + "first — the same confirmation your zones already require. Then the level is your "
            + "invalidation reference, not your entry reason.\n\n"
            + "It earns its place when it COINCIDES with a zone you already marked from price. "
            + "It should never create a zone on its own.\n\n"
            + "WHY PDH AND NOT THE CLOSE\n"
            + "Measured over 33 NIFTY sessions and 471 turning points against a random baseline of "
            + "equal density: PDH +5.5pp (2.6 sd), close +2.6pp (1.4 sd), low +1.9pp. PDH held in "
            + "both halves of the period and kept +3.3pp with its best three sessions removed.\n\n"
            + "WHAT IT IS NOT\n"
            + "It gives no direction — a level says look, price says act. BANKNIFTY passed the "
            + "split but FAILED the concentration test, so this is not confirmed across markets. "
            + "33 sessions is two months of one regime. Do not size against it."
          }
        >
          <span>{showRbGannPdh ? "☑" : "☐"} 📐 Gann PDH</span>
          <span className="text-[8px] font-bold opacity-70">CONTEXT</span>
        </button>

        <button
          onClick={toggleRbWickTested}
          onMouseEnter={() => setShowWickTestedGuidance(true)}
          onMouseLeave={() => setShowWickTestedGuidance(false)}
          className={`flex items-center gap-1.5 rounded bg-[#060b16]/90 px-2 py-1 text-[10px] font-black border transition-all shadow-lg backdrop-blur cursor-help ${
            showRbWickTested
              ? "bg-orange-500/20 border-orange-500/80 text-orange-300 ring-1 ring-orange-500/40"
              : "border-slate-700/60 text-slate-400 hover:text-slate-200"
          }`}
          title={
            "1H UPPER WICK ZONES, THE MEASURED SUBSET — context, not a signal.\n\n"
            + "WHAT IS DRAWN\n"
            + `Only 1H UPPER wick zones [max(Open,Close) → High] between ${WICK_MIN_PTS} and `
            + `${WICK_MAX_PTS} points deep that are still untouched. Solid line on the high, `
            + "dotted on the body edge. Nothing else — these are the only zones that survived "
            + "testing. The full wick/EMA engine is the separate checkbox beside this one.\n\n"
            + "WHY NO LOWER WICKS\n"
            + "They were measured and they do not work. NIFTY lower wicks came in at -0.4 sd "
            + "against a matched random baseline and fell to -11.2 points per trade once their "
            + "best five events were removed. Drawing them would be drawing noise.\n\n"
            + "THE EVIDENCE\n"
            + "38 first touches over 36 NIFTY sessions (8 Sep - 30 Oct 2026). Median move after "
            + "the touch 55 pts against 36 ± 7.9 for a random entry at the same hour (+2.4 sd). "
            + "Net pull, median MFE minus MAE, +32 vs +3 ± 14.3 (+2.0 sd). Shorting with a 15-pt "
            + "stop and 60-pt target: +12.9 pts/trade at 34% wins, +2.2 sd above random, still "
            + "+5.8 with its best five events removed, and positive in BOTH halves of the period "
            + "(+11.7 then +14.2). Baseline resamples whole sessions, so same-day clustering is "
            + "already accounted for.\n\n"
            + "HOW IT BEHAVES\n"
            + "The 15-pt stop is hit in 63% of cases, median 14 minutes. The 60-pt target takes a "
            + "median of 64 minutes. Two in three attempts stop out fast; the winners need an "
            + "hour. 34% at 4:1 is hard to sit through — know that before you use it.\n\n"
            + "WHAT IT IS NOT\n"
            + "BANKNIFTY does NOT confirm it: +1.5 sd, and its halves ran +40.0 then -4.3. The "
            + "whole sample is a downtrend in both instruments (NIFTY 23644 → 23000), so no "
            + "control here can separate 'upper wicks are resistance' from 'upper wicks are "
            + "resistance while the market falls'. 18 hypotheses were tested against these same "
            + "36 sessions; this is the one that survived, which is also how a false positive "
            + "would look. Re-tested in January on fresh sessions. Do not size against it."
          }
        >
          <span>{showRbWickTested ? "☑" : "☐"} 🔶 1H Wick ≤{WICK_MAX_PTS}p</span>
          <span className="text-[8px] font-bold opacity-70">TESTED</span>
        </button>

        {onAsof && (
          <div
            className={`flex items-center gap-1.5 rounded bg-[#060b16]/90 px-2 py-1 text-[10px] font-black border shadow-lg backdrop-blur ${
              reviewing
                ? "bg-amber-500/25 border-amber-400 text-amber-200 ring-1 ring-amber-400/50"
                : "border-slate-700/60 text-slate-400"
            }`}
            title={
              "REVIEW A PAST SESSION\n\n"
              + "Pick a date and the chart loads the window ENDING that day, straight from the "
              + "stored 1-minute candles. Clear it to return to live.\n\n"
              + "WHAT YOU GET: price action exactly as it printed, with every overlay that is "
              + "computed from the candles themselves.\n\n"
              + "WHAT YOU DO NOT GET: the server-side layers — Gann, the wick zones, the 8-EMA "
              + "engine, stacked nodes — and the analysis strip. Those are all computed against "
              + "TODAY and cannot be rewound. Drawing them over an old session would invent "
              + "confluences that never existed at the time, so they switch off here. That is a "
              + "real limitation, not an oversight: replaying every signal as it actually fired "
              + "needs each engine to accept an as-of date, which is a much larger job.\n\n"
              + "History goes back as far as your stored candles. Run "
              + "scripts/backfill_history.py to extend it."
            }
          >
            <span>🕰</span>
            <input
              type="date"
              value={asof ?? ""}
              max={new Date().toISOString().slice(0, 10)}
              onChange={(e) => onAsof(e.target.value || null)}
              className="bg-transparent text-[10px] font-bold outline-none cursor-pointer [color-scheme:dark]"
            />
            {reviewing && (
              <button
                onClick={() => onAsof(null)}
                className="rounded bg-amber-400/30 px-1 text-[9px] font-black hover:bg-amber-400/50"
                title="Back to live"
              >
                LIVE ✕
              </button>
            )}
          </div>
        )}

        <button
          onClick={toggleRbWickEma}
          onMouseEnter={() => setShowRbGuidance(true)}
          onMouseLeave={() => setShowRbGuidance(false)}
          className={`flex items-center gap-1.5 rounded bg-[#060b16]/90 px-2 py-1 text-[10px] font-black border transition-all shadow-lg backdrop-blur ${
            showRbWickEma
              ? "bg-cyan-500/20 border-cyan-500/80 text-cyan-300 ring-1 ring-cyan-500/40"
              : "border-slate-700/60 text-slate-400 hover:text-slate-200"
          }`}
          title="Toggle Institutional 1H/4H Wicks & Shaved Extremes + 1H 8-EMA Rejection Blocks"
        >
          <span>{showRbWickEma ? "☑" : "☐"} 1H/4H Wicks + 1H 8EMA</span>
          {htfWickEmaData?.ema8_1h?.value && (
            <span className="text-[9px] px-1 rounded font-mono font-bold bg-cyan-500/30 text-cyan-200">
              EMA8: {htfWickEmaData.ema8_1h.value.toLocaleString("en-IN", { maximumFractionDigits: 0 })}
            </span>
          )}
          {htfWickEmaData?.strict_trade_simulation?.ema8_1h?.overall && (
            <span className="text-[9px] px-1 rounded font-mono font-bold bg-emerald-500/25 text-emerald-300 border border-emerald-500/30" title="Strict Stop-First 1H 8-EMA Backtest: 55.2% Win20 (+5.7 pts exp)">
              Sim: {htfWickEmaData.strict_trade_simulation.ema8_1h.overall.strict_20_pct}% (+{htfWickEmaData.strict_trade_simulation.ema8_1h.overall.exp_pts}pt)
            </span>
          )}
        </button>

        {gannData?.time_cycles && (
          <div
            onMouseEnter={() => setShowRbGuidance(true)}
            onMouseLeave={() => setShowRbGuidance(false)}
            className={`flex items-center gap-1.5 rounded bg-[#060b16]/95 px-2 py-1 text-[10px] font-black border transition-all shadow-lg backdrop-blur cursor-pointer ${
              gannData.time_cycles.alert_text !== "None"
                ? "bg-amber-500/20 border-amber-500/80 text-amber-300 ring-1 ring-amber-500/50 animate-pulse shadow-amber-500/20"
                : "border-slate-700/60 text-slate-400 hover:text-slate-300"
            }`}
            title="Gann Time Cycles from Historical Major High & Major Low. Hover for full trader action guide."
          >
            <span>⏳ Time Cycle:</span>
            <span className={gannData.time_cycles.alert_text !== "None" ? "text-amber-300 font-extrabold" : "text-slate-300"}>
              {gannData.time_cycles.alert_text}
            </span>
            <span className="opacity-75 font-mono text-[9px] text-slate-400">
              | High+{gannData.time_cycles.days_from_major_high}d · Low+{gannData.time_cycles.days_from_major_low}d
            </span>
          </div>
        )}

        <button
          onClick={() => {
            if (trinityDrawerOpen && oiDrawerTab === "matrix") {
              setTrinityDrawerOpen(false);
            } else {
              setOiDrawerTab("matrix");
              setTrinityDrawerOpen(true);
            }
          }}
          className={`flex items-center gap-1.5 rounded bg-[#060b16]/90 px-2 py-1 text-[10px] font-black border transition-all shadow-lg backdrop-blur ${
            trinityDrawerOpen && oiDrawerTab === "matrix"
              ? "bg-gold-500/30 border-gold-500/80 text-gold-300 ring-1 ring-gold-500/40"
              : "border-gold-500/50 text-gold-300 hover:bg-gold-950/40"
          }`}
          title="Toggle Institutional Open Interest Matrix & Strike Map 2.0"
        >
          <span>⚡ Strike Map 2.0</span>
          <span className="text-[9px] opacity-75">{trinityDrawerOpen && oiDrawerTab === "matrix" ? "▼" : "▲"}</span>
        </button>

        <button
          onClick={() => {
            if (trinityDrawerOpen && oiDrawerTab === "trinity") {
              setTrinityDrawerOpen(false);
            } else {
              setOiDrawerTab("trinity");
              setTrinityDrawerOpen(true);
            }
          }}
          className={`flex items-center gap-1.5 rounded bg-[#060b16]/90 px-2 py-1 text-[10px] font-black border transition-all shadow-lg backdrop-blur ${
            trinityDrawerOpen && oiDrawerTab === "trinity"
              ? trinityState === "BULLISH_TRINITY_LOCK"
                ? "bg-emerald-950/90 border-emerald-500/80 text-emerald-300 ring-1 ring-emerald-500/40"
                : trinityState === "BEARISH_TRINITY_LOCK"
                ? "bg-rose-950/90 border-rose-500/80 text-rose-300 ring-1 ring-rose-500/40"
                : trinityState === "PRICE_PCR_DIVERGENCE"
                ? "bg-amber-950/90 border-amber-500/80 text-amber-300 ring-1 ring-amber-500/40"
                : "bg-sky-500/30 border-sky-500/80 text-sky-200 ring-1 ring-sky-500/40"
              : trinityState === "BULLISH_TRINITY_LOCK"
              ? "bg-emerald-950/90 border-emerald-500/80 text-emerald-300 ring-1 ring-emerald-500/40"
              : trinityState === "BEARISH_TRINITY_LOCK"
              ? "bg-rose-950/90 border-rose-500/80 text-rose-300 ring-1 ring-rose-500/40"
              : trinityState === "PRICE_PCR_DIVERGENCE"
              ? "bg-amber-950/90 border-amber-500/80 text-amber-300 ring-1 ring-amber-500/40"
              : "border-sky-500/50 text-sky-300 hover:bg-sky-950/40"
          }`}
          title="Toggle 3-Chart Institutional PCR & Open Interest Positioning Suite"
        >
          <span>{trinityBadge || "⚡ 3-Chart Trinity Suite"}</span>
          <span className="text-[9px] opacity-75">{trinityDrawerOpen && oiDrawerTab === "trinity" ? "▼" : "▲"}</span>
        </button>

        {(layers.walls || layers.volume || layers.rvol) && (expiries?.length ?? 0) > 1 && (
          <div className="flex items-center gap-1 rounded bg-[#060b16]/90 px-2 py-1 text-[10px] ring-1 ring-white/10 shadow-lg backdrop-blur">
            <span className="text-slate-400 font-semibold">{layers.walls ? "Expiry / Walls:" : "Option Expiry:"}</span>
            {(["cur", "next", "both"] as const).map((e) => (
              <button key={e} onClick={() => setWallExp(e)}
                className={`rounded px-1.5 py-0.5 font-bold transition-colors ${wallExp === e ? "bg-gold-500/30 text-gold-200 border border-gold-500/50" : "text-slate-400 hover:text-slate-200"}`}>
                {e === "cur" ? "This wk" : e === "next" ? "Next wk" : "Both"}
              </button>
            ))}
          </div>
        )}
      {layers.walls && <button className="tab !py-0.5 !text-[11px] pointer-events-auto" aria-pressed={fitOiWalls}
        title="Fit the nearest loaded CE and PE walls into the candle price scale. This changes display only; turn off for candle-only scaling."
        onClick={() => setFitOiWalls(v => !v)}>{fitOiWalls ? "☑" : "☐"} Fit OI walls</button>}
      {layers.walls && walls.length > 0 && (() => {
        const near = nearestOiWalls(walls, spot ?? candles[candles.length - 1]?.close ?? 0);
        return <span className="pointer-events-auto rounded border border-slate-600 bg-ink-900/95 px-2 py-1 text-xs text-slate-200"
          title={walls.map(w => `${w.side} ${w.p}: OI ${(w.oi / 1e5).toFixed(2)}L, daily Δ ${(w.chg / 1e5).toFixed(2)}L, ${w.status}`).join("\n")}>
          OI: {walls.length} walls · CE {near.ceiling?.p.toLocaleString("en-IN") ?? "—"} · PE {near.floor?.p.toLocaleString("en-IN") ?? "—"}
        </span>;
      })()}
      {layers.walls && (wallLoadError || (!walls.length && wallLoading)) && <div role={wallLoadError ? "alert" : "status"} className="pointer-events-auto rounded border border-amber-500/60 bg-ink-900/95 px-2 py-1 text-xs text-amber-300">{wallLoadError ? `OI walls refresh failed: ${wallLoadError}${walls.length ? " · keeping previous walls" : ""}` : "Loading OI walls…"}</div>}
      {/* Pinned Ceiling Pill: when major Call wall is > 150 pts above Spot (off-screen) */}
      {layers.walls && (() => {
        const wsp = writerBal?.spot ?? spot ?? candles[candles.length - 1]?.close;
        if (!wsp) return null;
        const ceWalls = walls.filter((w) => w.side === "CE").sort((a, b) => b.oi - a.oi);
        const topCe = ceWalls[0];
        if (!topCe || topCe.p <= wsp + 150) return null;
        return (
          <div
            className="pointer-events-auto flex items-center gap-1.5 rounded-lg border border-rose-500/60 bg-[#16060a]/92 px-2.5 py-1 text-[10.5px] font-bold text-rose-300 shadow-xl backdrop-blur"
            title={`Major Overhead Call Resistance Wall: ${topCe.p} CE with ${(topCe.oi / 1e7).toFixed(2)}Cr OI (${Math.round(topCe.p - wsp)} pts above current spot)`}
          >
            <span>🏛️ CEILING WALL:</span>
            <span className="font-mono text-white font-black">{topCe.p.toLocaleString("en-IN")} CE</span>
            <span className="text-[10px] text-rose-400">({(topCe.oi / 1e7).toFixed(2)}Cr · +{Math.round(topCe.p - wsp)}p)</span>
          </div>
        );
      })()}
      <div className="pointer-events-auto flex flex-wrap items-center justify-end gap-1">
        {layers.oi && <div className="flex items-center rounded border border-slate-600 bg-ink-900/95">
          <span className="px-2 text-[10px] text-slate-400" title="OI is outstanding futures positions, not executed buy/sell volume. Hover a square above a candle or a histogram bar for its meaning and next confirmation.">FUT OI</span>
          <button className={`tab !py-0.5 !text-[11px] ${oiMode === "raw" ? "!text-amber-300" : ""}`} title="Raw change in outstanding positions per closed candle. Opening spikes can compress later bars. Green/red describe price/OI classification, not an entry instruction." onClick={() => chooseOiMode("raw")}>Raw ΔOI</button>
          <button className={`tab !py-0.5 !text-[11px] ${oiMode === "relative" ? "!text-cyan-300" : ""}`} title="Relative × = this bar’s ΔOI / the earlier average absolute ΔOI. +2× means additions twice the recent baseline; −2× means reductions twice it. Blank means warm-up or incomparable data. No signal thresholds change." onClick={() => chooseOiMode("relative")}>Relative ×</button>
          <details className="relative">
            <summary className="cursor-pointer px-2 text-[11px] text-cyan-300" aria-label="OI guide">? OI guide</summary>
            <div className="absolute right-0 top-7 z-50 w-[340px] max-h-[60vh] overflow-auto rounded-lg border border-slate-600 bg-[#080f1e] p-3 text-[12px] leading-relaxed shadow-2xl">
              <b>How to read these shapes</b>
              <p className="mt-2 text-amber-300">🟧 Amber square: unusually fast OI expansion. Positions are being added; price supplies the conventional direction.</p>
              <p className="mt-2 text-violet-300">🟪 Violet square: unusually fast OI reduction. Positions are being closed; it is not automatically a reversal.</p>
              <p className="mt-2">Green: price up + OI up (long build-up). Light green: price up + OI down (short covering).</p>
              <p className="mt-2">Red: price down + OI up (short build-up). Pink: price down + OI down (long unwinding). Grey: threshold not met or no directional classification.</p>
              <p className="mt-2">Above/below the histogram zero line means OI addition/reduction. It does not directly mean buy/sell. Relative mode changes scale, not the underlying classifications.</p>
              <p className="mt-2 text-cyan-200">Decision support: BUY-SIDE / SELL-SIDE WATCH identifies context. Wait for a later closed-candle structure break and retest. Any hold, profit-taking or exit must use your actual position and price-risk plan.</p>
              <p className="mt-2 text-slate-400">Historical cards describe what was knowable then, not an instruction now. Missing/stale/unverified data means WAIT. Hover the square’s candle column or an OI histogram bar for details.</p>
            </div>
          </details>
        </div>}
        {/* 📊 RVOL Sub-Pane Mode Toggle (Slot Spikes vs Cumul Pace) */}
        {layers.rvol && (
          <div className="flex items-center rounded border border-ink-700 bg-ink-900/60 overflow-hidden">
            <button
              className={`tab !py-0.5 !text-[11px] font-bold flex items-center gap-1 transition-all border-0 ${
                rvolPaneMode === "slot"
                  ? "!bg-amber-950/80 !text-amber-300 shadow-md ring-1 ring-amber-400/40"
                  : "!text-slate-400 hover:!text-slate-200"
              }`}
              onClick={() => setAndSaveRvolPaneMode("slot")}
              title="Slot RVOL Mode: Bar-by-bar Time-of-Day spikes (shows 3.7x, 3.4x spikes past 2.5x threshold)"
            >
              <span>⚡</span>
              <span>Slot RVOL</span>
            </button>
            <button
              className={`tab !py-0.5 !text-[11px] font-bold flex items-center gap-1 transition-all border-0 border-l border-ink-700 ${
                rvolPaneMode === "cumul"
                  ? "!bg-emerald-950/80 !text-emerald-300 shadow-md ring-1 ring-emerald-400/40"
                  : "!text-slate-400 hover:!text-slate-200"
              }`}
              onClick={() => setAndSaveRvolPaneMode("cumul")}
              title="Cumulative RVOL Mode: Running session pace vs historical 20d/5d baseline"
            >
              <span>🌊</span>
              <span>Cumul cRVOL</span>
            </button>
          </div>
        )}
        <button
          className={`tab !py-0.5 !text-[11px] font-bold flex items-center gap-1 transition-all border ${
            isPrahaarOn
              ? "!border-emerald-500 !bg-emerald-950/80 !text-emerald-300 shadow-md ring-1 ring-emerald-400/40"
              : "!border-ink-700 !bg-ink-900/60 !text-slate-400 hover:!text-slate-200"
          }`}
          onClick={togglePrahaar}
          title="Toggle Prahaar Directional ATM Option-Buying Engine (EMA + MACD + ADX + VIX)"
        >
          <span>⚔️</span>
          <span>Prahaar Engine</span>
        </button>
        {/* 🏛️ GTF D&S Zones Toggle & Mode Dropdown */}
        <div className="flex items-center rounded border border-ink-700 bg-ink-900/60 overflow-hidden">
          <button
            className={`tab !py-0.5 !text-[11px] font-bold flex items-center gap-1 transition-all border-0 ${
              isGtfOn
                ? "!bg-emerald-950/80 !text-emerald-300 shadow-md ring-1 ring-emerald-400/40"
                : "!text-slate-400 hover:!text-slate-200"
            }`}
            onClick={toggleGtf}
            title="Toggle GTF Demand & Supply Engine 2.0 (Boring/Explosive Institutional Zones)"
          >
            <span>🏛️</span>
            <span>GTF Zones</span>
          </button>
          {isGtfOn && (
            <select
              value={gtfMode}
              onChange={(e) => setAndSaveGtfMode(e.target.value as GtfMarkingMode)}
              className="bg-ink-950/90 text-amber-300 text-[10px] font-semibold px-1 py-0.5 border-l border-ink-700 outline-none cursor-pointer hover:bg-ink-900"
              title="GTF Zone Marking Mode (Conservative: Body-Wick, Aggressive: Wick-Wick, Dynamic: ATR-Adjusted)"
            >
              <option value="conservative">Conservative</option>
              <option value="aggressive">Aggressive</option>
              <option value="dynamic">Dynamic</option>
            </select>
          )}
        </div>
        {stdvLeg && onResetToAutoStdv && (
          <button
            className="tab !py-0.5 !text-[11px] font-bold flex items-center gap-1 !border-cyan-500/70 !text-cyan-300 hover:!bg-cyan-950/40"
            title="Clear manual marking and revert to system automatic STDV"
            onClick={onResetToAutoStdv}
          >
            <span>🤖</span>
            <span>Use System Auto</span>
          </button>
        )}
        <button
          className={`tab !py-0.5 !text-[11px] font-bold flex items-center gap-1 transition-all ${
            isStdvPicking
              ? "!bg-red-600 !text-white animate-pulse ring-2 ring-red-400"
              : stdvLeg
              ? "!border-amber-500/70 !text-amber-300"
              : "!border-red-500/70 !text-red-400"
          }`}
          onClick={isStdvPicking ? onCancelStdvPicking : () => onStartStdvPicking?.()}
          title="Interactive 2-Click STDV Tool: Click any 2 candle wicks on chart to anchor custom STDV manipulation leg"
        >
          <span>{isStdvPicking ? "✕" : "📍"}</span>
          <span>{isStdvPicking ? "Cancel Pick" : "2-Click STDV"}</span>
        </button>
        {(layers.fp || layers.heat || layers.bias) && <>
          <button className="tab !py-0.5 !text-[11px]" title="Smaller footprint / heatmap / bias labels"
            onClick={() => { setFpScale(FP_SCALE - 0.25); fpPrim.current.refresh(); setFpTick((n) => n + 1); }}>A−</button>
          <span className="tab !py-0.5 !text-[11px] text-slate-400">{Math.round(FP_SCALE * 100)}%</span>
          <button className="tab !py-0.5 !text-[11px]" title="Bigger footprint / heatmap / bias labels"
            onClick={() => { setFpScale(FP_SCALE + 0.25); fpPrim.current.refresh(); setFpTick((n) => n + 1); }}>A+</button>
        </>}
        <button className="tab !py-0.5 !text-[11px]" title="Fit all candles + auto price scale"
          onClick={() => { chart.current?.timeScale().fitContent(); series.current?.priceScale().setAutoScale(true); }}>Fit</button>
        <button className="tab !py-0.5 !text-[11px]" title="Back to automatic price scale (or double-click the price axis)"
          onClick={() => series.current?.priceScale().setAutoScale(true)}>Auto ↕</button>
        {onExpand && <button className="tab !py-0.5 !text-[11px]" title="Full screen (Esc to exit)" onClick={onExpand}>{expanded ? "▣ Normal" : "⛶ Full"}</button>}
      </div>
      </div>

      {/* Mouse-Over Trader Guidance for Rejection Blocks (RB) Pivots, Gaps & Gann Sq9 */}
      {/* Hover card for the PDH ladder: how to use it, and what it is not. */}
      {reviewing && (
        <div className="pointer-events-none absolute left-1/2 top-2 z-[80] -translate-x-1/2 rounded-lg border-2 border-amber-400 bg-amber-950/95 px-4 py-1.5 text-center shadow-2xl backdrop-blur">
          <div className="text-[13px] font-black tracking-wide text-amber-200">
            🕰 REVIEWING {asof} — NOT LIVE
          </div>
          <div className="text-[9.5px] font-semibold text-amber-300/80">
            server layers off (Gann · wick zones · 8-EMA · nodes · analysis) — they cannot be rewound
          </div>
        </div>
      )}

      {showWickTestedGuidance && (
        <div
          onMouseEnter={() => setShowWickTestedGuidance(true)}
          onMouseLeave={() => setShowWickTestedGuidance(false)}
          className="pointer-events-auto absolute right-2 top-[52px] z-[70] w-[440px] max-w-[92%] rounded-xl border border-orange-500/50 bg-[#0a0e1c]/97 p-3.5 text-[11.5px] leading-relaxed text-slate-200 shadow-2xl backdrop-blur-md"
        >
          <div className="flex items-center justify-between gap-2 mb-2">
            <span className="font-black text-orange-300 text-[12px]">🔶 1H upper wick zones · the measured subset</span>
            <span className="text-[9px] font-bold px-1.5 py-0.5 rounded bg-slate-700/60 text-slate-300">TESTED · NOT A SIGNAL</span>
          </div>

          <div className="font-mono text-[11px] text-slate-400 mb-2">
            zone = [max(Open,Close) → High] · depth {WICK_MIN_PTS}–{WICK_MAX_PTS} pts · untouched only
            {(() => {
              const n = (htfWickEmaData?.active_levels || []).filter(
                (lv) => lv.tf === "1H" && lv.side === "RESISTANCE" && !lv.is_shaved
                  && lv.wick_pts >= WICK_MIN_PTS && lv.wick_pts <= WICK_MAX_PTS && lv.touch_count === 0,
              ).length;
              return <>{"  ·  "}<span className="text-white font-bold">{n}</span> live now</>;
            })()}
          </div>

          <div className="mb-2">
            <div className="text-orange-300/90 font-bold text-[10px] uppercase tracking-wide mb-0.5">Upper only — lower wicks were tested and failed</div>
            NIFTY lower wicks read <span className="font-mono text-rose-300">−0.4 sd</span> against a matched
            random baseline and fell to <span className="font-mono text-rose-300">−11.2 pts/trade</span> with their
            best five events removed. They are <strong className="text-white">not drawn</strong>, because drawing them
            would be drawing noise.
          </div>

          <div className="mb-2">
            <div className="text-orange-300/90 font-bold text-[10px] uppercase tracking-wide mb-0.5">The evidence</div>
            <div className="font-mono text-[10.5px] text-slate-300">
              38 touches · 36 sessions · median move <span className="text-emerald-300">55</span> vs <span className="text-slate-400">36 ± 7.9</span> random (<span className="text-emerald-300">+2.4 sd</span>)
              <br />net pull <span className="text-emerald-300">+32</span> vs <span className="text-slate-400">+3 ± 14.3</span> (<span className="text-emerald-300">+2.0 sd</span>) · 15/60 short <span className="text-emerald-300">+12.9</span> pts (<span className="text-emerald-300">+2.2 sd</span>)
              <br />best-5 removed <span className="text-emerald-300">+5.8</span> · halves <span className="text-emerald-300">+11.7 / +14.2</span>
            </div>
          </div>

          <div className="mb-2">
            <div className="text-orange-300/90 font-bold text-[10px] uppercase tracking-wide mb-0.5">How it behaves — know this first</div>
            The 15-pt stop is hit <strong className="text-white">63%</strong> of the time, median 14 min. The 60-pt
            target takes a median of <strong className="text-white">64 min</strong>. Two in three attempts stop out
            fast and the winners need an hour. <strong className="text-white">34% at 4:1</strong> is hard to sit through.
          </div>

          <div>
            <div className="text-orange-300/90 font-bold text-[10px] uppercase tracking-wide mb-0.5">What it is not</div>
            BANKNIFTY does <strong className="text-white">not</strong> confirm it (+1.5 sd; halves +40.0 then −4.3).
            The whole sample is a downtrend in both instruments, so nothing here separates
            &ldquo;upper wicks are resistance&rdquo; from &ldquo;…while the market falls&rdquo;. 18 hypotheses were
            tested against these same 36 sessions and this is the one that survived — which is also
            what a false positive looks like. Re-test in January. <strong className="text-white">Do not size against it.</strong>
          </div>
        </div>
      )}

      {showPdhGuidance && (
        <div
          onMouseEnter={() => setShowPdhGuidance(true)}
          onMouseLeave={() => setShowPdhGuidance(false)}
          className="pointer-events-auto absolute right-2 top-[52px] z-[70] w-[420px] max-w-[92%] rounded-xl border border-fuchsia-500/50 bg-[#0a0e1c]/97 p-3.5 text-[11.5px] leading-relaxed text-slate-200 shadow-2xl backdrop-blur-md"
        >
          <div className="flex items-center justify-between gap-2 mb-2">
            <span className="font-black text-fuchsia-300 text-[12px]">📐 Gann Sq9 · anchored on yesterday's high</span>
            <span className="text-[9px] font-bold px-1.5 py-0.5 rounded bg-slate-700/60 text-slate-300">CONTEXT · NOT A SIGNAL</span>
          </div>
          {gannData?.gann_pdh_anchor ? (
            <div className="font-mono text-[11px] text-slate-400 mb-2">
              anchor PDH <span className="text-white font-bold">{gannData.gann_pdh_anchor.toLocaleString("en-IN")}</span>
              {"  ·  "}levels = (√PDH + angle/180)²
            </div>
          ) : null}

          <div className="mb-2">
            <div className="text-fuchsia-300/90 font-bold text-[10px] uppercase tracking-wide mb-0.5">Is it support / resistance?</div>
            It behaves like one <em>only after price reacts there</em>. Treat a level as a place to
            <strong className="text-white"> watch</strong>, never a place to enter. Price touching it is not a trade.
          </div>

          <div className="mb-2">
            <div className="text-fuchsia-300/90 font-bold text-[10px] uppercase tracking-wide mb-0.5">How to use it</div>
            Wait for the 15m to print a <strong className="text-white">doji, hammer or engulfing candle at the level</strong> —
            the same confirmation your zones already need. Then the level is your invalidation
            reference, not your reason for entering. It earns its keep when it <strong className="text-white">lands inside a
            zone you already marked from price</strong>; it should never create a zone by itself.
          </div>

          <div className="mb-2">
            <div className="text-fuchsia-300/90 font-bold text-[10px] uppercase tracking-wide mb-0.5">Why the high, not the close</div>
            <div className="font-mono text-[10.5px] text-slate-300">
              PDH <span className="text-emerald-300">+5.5pp</span> (2.6 sd) · close <span className="text-slate-400">+2.6pp</span> (1.4 sd) · low <span className="text-slate-400">+1.9pp</span>
            </div>
            <div className="text-slate-400 text-[10.5px] mt-0.5">
              33 NIFTY sessions, 471 turning points, against a random baseline of equal density.
              PDH held in both halves and kept +3.3pp with its best three sessions removed.
            </div>
          </div>

          <div className="rounded border border-slate-700/70 bg-slate-900/50 px-2 py-1.5 text-[10.5px] text-slate-300">
            <strong className="text-amber-300">Not confirmed.</strong> No direction — a level says look, price says act.
            BANKNIFTY passed the split but <strong className="text-white">failed the concentration test</strong>. Two months,
            one regime, one market. Only 2–3 of the 8 levels are drawn: the rest sit where price does not go.
          </div>
        </div>
      )}

      {showRbGuidance && gannData && (
        <div
          onMouseEnter={() => setShowRbGuidance(true)}
          onMouseLeave={() => setShowRbGuidance(false)}
          className="pointer-events-auto absolute right-2 top-10 z-50 w-[560px] max-w-[95vw] max-h-[85vh] overflow-y-auto rounded-xl border border-gold-500/50 bg-[#060c18]/98 p-3 shadow-2xl backdrop-blur-xl ring-1 ring-gold-500/30 font-sans text-xs"
        >
          <div className="mb-2 flex items-center justify-between border-b border-gold-500/30 pb-1.5">
            <span className="flex items-center gap-1.5 font-bold text-gold-300 text-[12px]">
              <span>⟂</span>
              <span>REJECTION BLOCKS · HISTORICAL PIVOTS, GAPS & GANN SQ9 MATRIX</span>
            </span>
            <span className="font-mono text-[10px] text-slate-400">
              Ref: {gannData.ref_date} (Close {gannData.ref_close.toLocaleString("en-IN")})
            </span>
          </div>

          {/* Time Cycle Alert Badge */}
          <div className={`mb-2.5 rounded-lg p-2 border ${
            gannData.time_cycles.alert_text !== "None"
              ? "bg-amber-950/70 border-amber-500/80 text-amber-200"
              : "bg-slate-900/80 border-slate-700/60 text-slate-300"
          }`}>
            <div className="flex items-center justify-between font-bold text-[11px] mb-1">
              <span className="flex items-center gap-1">
                <span>⏳</span>
                <span>GANN TIME CYCLE VIBRATION:</span>
              </span>
              <span className={`px-1.5 py-0.5 rounded text-[10px] font-extrabold ${
                gannData.time_cycles.alert_text !== "None" ? "bg-amber-500 text-black animate-pulse" : "bg-slate-800 text-slate-400"
              }`}>
                {gannData.time_cycles.alert_text !== "None" ? `ALERT: ${gannData.time_cycles.alert_text}` : "NEUTRAL CYCLE"}
              </span>
            </div>
            <div className="text-[10.5px] leading-relaxed text-slate-300">
              Major High ({gannData.time_cycles.major_high_date} @ {gannData.time_cycles.major_high_price.toLocaleString("en-IN")}): <b>+{gannData.time_cycles.days_from_major_high}d</b> ·
              Major Low ({gannData.time_cycles.major_low_date} @ {gannData.time_cycles.major_low_price.toLocaleString("en-IN")}): <b>+{gannData.time_cycles.days_from_major_low}d</b>.
              <div className="mt-0.5 text-amber-300/90 italic">
                Note: Gann calendar day vibrations (30, 45, 60, 90, 120, 135, 144, 180, 225, 270, 315, 360) mark high-probability inflection windows.
              </div>
            </div>
          </div>

          {/* Quantitative Backtest Summary for 1H/4H Wicks & 1H 8-EMA Scored Against Null Baselines */}
          {htfWickEmaData?.null_baseline_summary?.categories ? (
            <div className="rounded-lg bg-cyan-950/40 border border-cyan-500/50 p-2.5 mb-2.5 shadow-lg">
              <div className="font-bold text-cyan-300 text-[11px] mb-1 flex items-center justify-between border-b border-cyan-500/20 pb-1">
                <span className="flex items-center gap-1.5">
                  <span>📊</span>
                  <span>NULL-BASELINE QUANTITATIVE BACKTEST (1H / 4H WICKS & 1H 8-EMA)</span>
                </span>
                <span className="text-[9px] text-cyan-400 font-mono">
                  {htfWickEmaData.null_baseline_summary.metadata.dataset_range} ({htfWickEmaData.null_baseline_summary.metadata.total_sessions} sessions, {htfWickEmaData.null_baseline_summary.metadata.total_intraday_candles.toLocaleString()} 1m bars)
                </span>
              </div>
              <div className="text-[9px] text-amber-300/90 bg-amber-950/30 border border-amber-500/30 rounded px-1.5 py-0.5 mb-1.5 font-mono">
                ⚠️ <b>DISCLOSURE:</b> {htfWickEmaData.null_baseline_summary.metadata.statement}
              </div>
              <div className="overflow-x-auto max-h-64 overflow-y-auto">
                <table className="w-full text-[9.5px] text-left border-collapse font-mono">
                  <thead>
                    <tr className="border-b border-slate-700/80 text-slate-400 font-semibold bg-slate-900/60 sticky top-0">
                      <th className="py-1 px-1">Category</th>
                      <th className="py-1 px-1 text-center">Cell</th>
                      <th className="py-1 px-1 text-center">N</th>
                      <th className="py-1 px-1 text-center">Win%</th>
                      <th className="py-1 px-1 text-right">Real Exp</th>
                      <th className="py-1 px-1 text-center">Rand Null</th>
                      <th className="py-1 px-1 text-center">Edge Z</th>
                      <th className="py-1 px-1 text-center">Dir Z</th>
                      <th className="py-1 px-1 text-center">Drop5</th>
                      <th className="py-1 px-1 text-center">OOS (H1/H2)</th>
                      <th className="py-1 px-1 text-center">Med Tgt</th>
                      <th className="py-1 px-1 text-center">Voice Status</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-slate-800/60">
                    {Object.entries(htfWickEmaData.null_baseline_summary.categories).map(([catKey, cat]: [string, any]) => {
                      if (cat.sample_n < 5) {
                        return (
                          <tr key={catKey} className="hover:bg-cyan-950/20 text-slate-400">
                            <td className="py-1 px-1 font-semibold text-slate-300">{catKey}</td>
                            <td className="py-1 px-1 text-center">—</td>
                            <td className="py-1 px-1 text-center">{cat.sample_n}</td>
                            <td className="py-1 px-1 text-center">—</td>
                            <td className="py-1 px-1 text-right">—</td>
                            <td className="py-1 px-1 text-center">—</td>
                            <td className="py-1 px-1 text-center">—</td>
                            <td className="py-1 px-1 text-center">—</td>
                            <td className="py-1 px-1 text-center">—</td>
                            <td className="py-1 px-1 text-center">—</td>
                            <td className="py-1 px-1 text-center">—</td>
                            <td className="py-1 px-1 text-center">
                              <span className="text-[8px] px-1 py-0.5 rounded bg-slate-800 text-slate-400">N &lt; 5 UNPROVEN</span>
                            </td>
                          </tr>
                        );
                      }
                      return ["s15_t20", "s15_t40"].map((cellKey) => {
                        const cell = cat[cellKey];
                        if (!cell) return null;
                        const isSelected = selectedRbCategory === catKey;
                        return (
                          <tr
                            key={`${catKey}-${cellKey}`}
                            onClick={() => setSelectedRbCategory(isSelected ? null : catKey)}
                            className={`cursor-pointer transition-colors ${
                              isSelected ? "bg-cyan-900/40" : "hover:bg-cyan-950/30"
                            }`}
                            title="Click to view detailed per-event table and resolution times"
                          >
                            <td className="py-1 px-1 font-semibold text-cyan-200">
                              {catKey} {isSelected ? "▼" : "▶"}
                            </td>
                            <td className="py-1 px-1 text-center text-slate-300">
                              s{cell.stop}/t{cell.target}
                            </td>
                            <td className="py-1 px-1 text-center text-slate-300">{cell.sample_n}</td>
                            <td className="py-1 px-1 text-center text-emerald-400 font-bold">{cell.win_pct}%</td>
                            <td className={`py-1 px-1 text-right font-bold ${cell.real_exp >= 0 ? "text-emerald-300" : "text-rose-400"}`}>
                              {cell.real_exp > 0 ? `+${cell.real_exp}` : cell.real_exp} pt
                            </td>
                            <td className="py-1 px-1 text-center text-slate-400">
                              {cell.rand_mean > 0 ? `+${cell.rand_mean}` : cell.rand_mean}±{cell.rand_sd}
                            </td>
                            <td className={`py-1 px-1 text-center font-bold ${cell.edge_z >= 2.0 ? "text-emerald-400" : "text-slate-300"}`}>
                              {cell.edge_z > 0 ? `+${cell.edge_z}` : cell.edge_z} sd
                            </td>
                            <td className={`py-1 px-1 text-center font-bold ${cell.dir_z >= 2.0 ? "text-emerald-400" : "text-slate-300"}`}>
                              {cell.dir_z > 0 ? `+${cell.dir_z}` : cell.dir_z} sd
                            </td>
                            <td className={`py-1 px-1 text-center ${cell.drop5 > 0 ? "text-emerald-300" : "text-rose-400"}`}>
                              {cell.drop5 > 0 ? `+${cell.drop5}` : cell.drop5}
                            </td>
                            <td className="py-1 px-1 text-center text-slate-300">
                              {cell.oos_h1_exp}/{cell.oos_h2_exp}
                            </td>
                            <td className="py-1 px-1 text-center text-cyan-300">
                              {cell.median_time_target_min != null ? `${cell.median_time_target_min}m` : "—"}
                            </td>
                            <td className="py-1 px-1 text-center">
                              {cell.voice_armed ? (
                                <span className="text-[8px] font-black px-1.5 py-0.5 rounded bg-emerald-500 text-slate-950">
                                  ✓ VOICE ARMED
                                </span>
                              ) : (
                                <span className="text-[8px] font-bold px-1.5 py-0.5 rounded bg-slate-800 text-slate-400 border border-slate-700">
                                  SILENT (Z&lt;2sd)
                                </span>
                              )}
                            </td>
                          </tr>
                        );
                      });
                    })}
                  </tbody>
                </table>
              </div>

              {/* Expandable Per-Event Table & Time-to-Resolution */}
              {selectedRbCategory && htfWickEmaData.per_event_tables?.[selectedRbCategory] && (
                <div className="mt-2 p-2 bg-slate-950/80 border border-cyan-500/40 rounded">
                  <div className="flex items-center justify-between pb-1 border-b border-slate-800 mb-1.5">
                    <span className="font-bold text-cyan-300 text-[10px]">
                      📋 PER-EVENT LOG: {selectedRbCategory} ({htfWickEmaData.per_event_tables[selectedRbCategory].length} events)
                    </span>
                    <button
                      onClick={() => setSelectedRbCategory(null)}
                      className="text-[9px] px-1 text-slate-400 hover:text-white"
                    >
                      ✕ Close Log
                    </button>
                  </div>
                  <div className="text-[8.5px] text-cyan-400/80 italic mb-1">
                    Forward walk starts strictly AFTER signal candle close. Zero-minute lookahead eliminated.
                  </div>
                  <div className="max-h-40 overflow-y-auto">
                    <table className="w-full text-[9px] text-left border-collapse font-mono">
                      <thead>
                        <tr className="border-b border-slate-800 text-slate-400">
                          <th className="py-0.5">Date</th>
                          <th className="py-0.5">Time</th>
                          <th className="py-0.5">Dir</th>
                          <th className="py-0.5 text-right">Entry</th>
                          <th className="py-0.5 text-right">MFE</th>
                          <th className="py-0.5 text-right">MAE</th>
                          <th className="py-0.5">Outcome (15/20)</th>
                          <th className="py-0.5">Outcome (15/40)</th>
                        </tr>
                      </thead>
                      <tbody className="divide-y divide-slate-900">
                        {htfWickEmaData.per_event_tables[selectedRbCategory].map((ev: any, idx: number) => (
                          <tr key={idx} className="hover:bg-slate-900/60">
                            <td className="py-0.5 text-slate-300">{ev.date}</td>
                            <td className="py-0.5 text-slate-400">{ev.time}</td>
                            <td className={`py-0.5 font-bold ${ev.dir === "LONG" ? "text-emerald-400" : "text-rose-400"}`}>
                              {ev.dir}
                            </td>
                            <td className="py-0.5 text-right text-slate-200">{ev.entry.toFixed(1)}</td>
                            <td className="py-0.5 text-right text-emerald-300">+{ev.mfe.toFixed(0)}</td>
                            <td className="py-0.5 text-right text-rose-300">-{ev.mae.toFixed(0)}</td>
                            <td className="py-0.5 text-slate-300">{ev.outcome_20}</td>
                            <td className="py-0.5 text-slate-300">{ev.outcome_40}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </div>
              )}
            </div>
          ) : htfWickEmaData?.backtest_summary && (
            <div className="rounded-lg bg-cyan-950/40 border border-cyan-500/50 p-2.5 mb-2.5 shadow-lg">
              <div className="font-bold text-cyan-300 text-[11px] mb-1.5 flex items-center justify-between border-b border-cyan-500/20 pb-1">
                <span className="flex items-center gap-1.5">
                  <span>📊</span>
                  <span>QUANTITATIVE FIRST-TOUCH BACKTEST (1H / 4H WICKS & 1H 8-EMA)</span>
                </span>
                <span className="text-[9px] text-cyan-400 font-mono">
                  {htfWickEmaData.backtest_summary.dataset_range} ({htfWickEmaData.backtest_summary.total_intraday_candles.toLocaleString()} bars)
                </span>
              </div>
              <div className="overflow-x-auto">
                <table className="w-full text-[10px] text-left border-collapse">
                  <thead>
                    <tr className="border-b border-slate-700/60 text-slate-400 font-mono">
                      <th className="py-1">Setup / Level</th>
                      <th className="py-1 text-center">Touches</th>
                      <th className="py-1 text-center">Win ≥20pt</th>
                      <th className="py-1 text-center">Win ≥40pt</th>
                      <th className="py-1 text-right">Avg MFE</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-slate-800/60 font-mono">
                    <tr className="hover:bg-cyan-950/20">
                      <td className="py-1 text-rose-300 font-semibold">1H Wick & Shaved Extr</td>
                      <td className="py-1 text-center text-slate-300">{htfWickEmaData.backtest_summary.wick_1h.first_touches}</td>
                      <td className="py-1 text-center text-emerald-400 font-bold">{htfWickEmaData.backtest_summary.wick_1h.win_rate_20_pct}%</td>
                      <td className="py-1 text-center text-emerald-400 font-bold">{htfWickEmaData.backtest_summary.wick_1h.win_rate_40_pct}%</td>
                      <td className="py-1 text-right text-cyan-300 font-bold">+{htfWickEmaData.backtest_summary.wick_1h.avg_mfe_pts} pts</td>
                    </tr>
                    <tr className="hover:bg-cyan-950/20">
                      <td className="py-1 text-rose-300 font-semibold">4H Wick & Shaved Extr</td>
                      <td className="py-1 text-center text-slate-300">{htfWickEmaData.backtest_summary.wick_4h.first_touches}</td>
                      <td className="py-1 text-center text-emerald-400 font-bold">{htfWickEmaData.backtest_summary.wick_4h.win_rate_20_pct}%</td>
                      <td className="py-1 text-center text-emerald-400 font-bold">{htfWickEmaData.backtest_summary.wick_4h.win_rate_40_pct}%</td>
                      <td className="py-1 text-right text-cyan-300 font-bold">+{htfWickEmaData.backtest_summary.wick_4h.avg_mfe_pts} pts</td>
                    </tr>
                    <tr className="hover:bg-cyan-950/20">
                      <td className="py-1 text-cyan-300 font-semibold">1H 8-EMA Dynamic Line</td>
                      <td className="py-1 text-center text-slate-300">{htfWickEmaData.backtest_summary.ema8_1h.first_touches}</td>
                      <td className="py-1 text-center text-emerald-400 font-bold">{htfWickEmaData.backtest_summary.ema8_1h.win_rate_20_pct}%</td>
                      <td className="py-1 text-center text-emerald-400 font-bold">{htfWickEmaData.backtest_summary.ema8_1h.win_rate_40_pct}%</td>
                      <td className="py-1 text-right text-cyan-300 font-bold">+{htfWickEmaData.backtest_summary.ema8_1h.avg_mfe_pts} pts</td>
                    </tr>
                  </tbody>
                </table>
              </div>
              <div className="mt-1 text-[9.5px] text-cyan-400/80 italic">
                Quantitative backtest evaluated across entire historical dataset for first-touch MFE reactions before 15pt adverse stop.
              </div>
            </div>
          )}

          {/* Strict Zero-Lookahead, Stop-First Trade Simulation (Tables 1, 2, 3) */}
          {htfWickEmaData?.strict_trade_simulation?.ema8_1h && (() => {
            const sim = htfWickEmaData.strict_trade_simulation.ema8_1h;
            const ov = sim.overall;
            const supp = sim.support_bounce;
            const res = sim.resistance_rejection;
            return (
              <div className="rounded-lg bg-emerald-950/30 border border-emerald-500/50 p-2.5 mb-2.5 shadow-lg">
                <div className="font-bold text-emerald-300 text-[11px] mb-1.5 flex items-center justify-between border-b border-emerald-500/20 pb-1">
                  <span className="flex items-center gap-1.5">
                    <span>🛡️</span>
                    <span>STRICT STOP-FIRST 1H 8-EMA SIMULATION (ZERO LOOKAHEAD)</span>
                  </span>
                  <span className="text-[9px] text-emerald-400 font-mono">
                    N={ov.n} touches (15pt SL kills trade)
                  </span>
                </div>

                {/* Audit Bias Clearance Callout */}
                <div className="text-[9px] text-emerald-200/90 bg-emerald-950/40 border border-emerald-500/30 rounded px-2 py-1 mb-2 font-mono">
                  <div className="font-bold text-emerald-300 mb-0.5">✓ 4 BACKTEST BIASES AUDITED & NEUTRALIZED:</div>
                  <div className="grid grid-cols-2 gap-x-2 gap-y-0.5 text-[8.5px] text-slate-300">
                    <div>• <b>Bug A (Stop-Loss Race)</b>: SL kills trade immediately (post-SL bounces eliminated).</div>
                    <div>• <b>Bug B (Same-Bar Spike)</b>: Dual breach treated conservatively as STOPPED.</div>
                    <div>• <b>Bug C (Zero Lookahead)</b>: 1H EMA strictly uses prior completed 1H closes.</div>
                    <div>• <b>Bug D (Touch Execution)</b>: Exact fill price at touch (no unearned 5pt offset).</div>
                  </div>
                </div>

                {/* Table 1: Raw MFE vs Strict Stop-First Comparison */}
                <div className="mb-2">
                  <div className="text-[10px] font-bold text-slate-300 mb-1 flex items-center justify-between">
                    <span>TABLE 1: Raw Unconditional MFE vs Strict Trade Simulation</span>
                    <span className="text-[9px] text-slate-400 font-normal">N = {ov.n} trades</span>
                  </div>
                  <table className="w-full text-[9px] text-left border-collapse font-mono bg-slate-900/60 rounded">
                    <thead>
                      <tr className="border-b border-slate-800 text-slate-400">
                        <th className="py-1 px-1.5">Simulation Mode</th>
                        <th className="py-1 px-1 text-center">Win ≥20pt</th>
                        <th className="py-1 px-1 text-center">Win ≥40pt</th>
                        <th className="py-1 px-1 text-center">Stopped Out (-15p)</th>
                        <th className="py-1 px-1 text-right">Expectancy</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-slate-800/60">
                      <tr className="hover:bg-slate-800/40 text-slate-400">
                        <td className="py-1 px-1.5 font-semibold text-rose-300/80">
                          Raw MFE (SL Ignored)
                        </td>
                        <td className="py-1 px-1 text-center font-bold text-slate-400">{ov.raw_20_pct}%</td>
                        <td className="py-1 px-1 text-center font-bold text-slate-400">{ov.raw_40_pct}%</td>
                        <td className="py-1 px-1 text-center text-slate-500">0.0% (ignored)</td>
                        <td className="py-1 px-1 text-right text-slate-500">—</td>
                      </tr>
                      <tr className="hover:bg-emerald-950/30 text-slate-200">
                        <td className="py-1 px-1.5 font-bold text-emerald-300">
                          Strict Stop-First (Real)
                        </td>
                        <td className="py-1 px-1 text-center font-bold text-emerald-400">{ov.strict_20_pct}%</td>
                        <td className="py-1 px-1 text-center font-bold text-emerald-400">{ov.strict_40_pct}%</td>
                        <td className="py-1 px-1 text-center text-rose-400 font-bold">{ov.stopped_pct}%</td>
                        <td className={`py-1 px-1 text-right font-black ${ov.exp_pts >= 0 ? "text-emerald-300" : "text-rose-400"}`}>
                          {ov.exp_pts > 0 ? `+${ov.exp_pts}` : ov.exp_pts} pts/tr
                        </td>
                      </tr>
                    </tbody>
                  </table>
                  <div className="text-[8px] text-amber-300/80 italic mt-0.5">
                    * Raw 76.1% was inflated by post-SL bounces. Real institutional execution wins 55.2% (+5.7 pts expectancy).
                  </div>
                </div>

                {/* Table 2: Direction Breakdown */}
                <div className="mb-2">
                  <div className="text-[10px] font-bold text-slate-300 mb-1 flex items-center justify-between">
                    <span>TABLE 2: Direction Breakdown (Long vs Short)</span>
                    <span className="text-[9px] text-slate-400 font-normal">Support vs Resistance</span>
                  </div>
                  <table className="w-full text-[9px] text-left border-collapse font-mono bg-slate-900/60 rounded">
                    <thead>
                      <tr className="border-b border-slate-800 text-slate-400">
                        <th className="py-1 px-1.5">Direction</th>
                        <th className="py-1 px-1 text-center">N</th>
                        <th className="py-1 px-1 text-center">Strict Win ≥20</th>
                        <th className="py-1 px-1 text-center">Strict Win ≥40</th>
                        <th className="py-1 px-1 text-center">Stopped (-15)</th>
                        <th className="py-1 px-1 text-right">Expectancy</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-slate-800/60">
                      <tr className="hover:bg-slate-800/40">
                        <td className="py-1 px-1.5 font-bold text-emerald-400">
                          Support Bounce (from above)
                        </td>
                        <td className="py-1 px-1 text-center text-slate-300">{supp.n}</td>
                        <td className="py-1 px-1 text-center font-bold text-emerald-400">{supp.strict_20_pct}%</td>
                        <td className="py-1 px-1 text-center font-bold text-emerald-400">{supp.strict_40_pct}%</td>
                        <td className="py-1 px-1 text-center text-rose-400">{supp.stopped_pct}%</td>
                        <td className="py-1 px-1 text-right font-bold text-emerald-300">
                          {supp.exp_pts > 0 ? `+${supp.exp_pts}` : supp.exp_pts} pts
                        </td>
                      </tr>
                      <tr className="hover:bg-slate-800/40">
                        <td className="py-1 px-1.5 font-bold text-rose-400">
                          Resistance Rejection (from below)
                        </td>
                        <td className="py-1 px-1 text-center text-slate-300">{res.n}</td>
                        <td className="py-1 px-1 text-center font-bold text-emerald-400">{res.strict_20_pct}%</td>
                        <td className="py-1 px-1 text-center font-bold text-emerald-400">{res.strict_40_pct}%</td>
                        <td className="py-1 px-1 text-center text-rose-400">{res.stopped_pct}%</td>
                        <td className="py-1 px-1 text-right font-bold text-emerald-300">
                          {res.exp_pts > 0 ? `+${res.exp_pts}` : res.exp_pts} pts
                        </td>
                      </tr>
                    </tbody>
                  </table>
                </div>

                {/* Table 3 Toggle: Last 15 Trades Audit Ledger */}
                <div className="pt-1 border-t border-emerald-500/20">
                  <div className="flex items-center justify-between">
                    <span className="text-[9.5px] font-bold text-slate-300">
                      TABLE 3: Institutional Audit Ledger
                    </span>
                    <button
                      onClick={() => setShowStrictAuditLedger((prev) => !prev)}
                      className="px-2 py-0.5 text-[9px] font-bold rounded bg-emerald-500/20 text-emerald-300 border border-emerald-500/40 hover:bg-emerald-500/30 transition-colors"
                    >
                      {showStrictAuditLedger ? "Hide Audit Ledger ▲" : "View Last 15 Touches Audit Ledger ▼"}
                    </button>
                  </div>

                  {showStrictAuditLedger && sim.audit_ledger_last_15 && (
                    <div className="mt-2 p-1.5 bg-slate-950/90 border border-emerald-500/30 rounded max-h-48 overflow-y-auto">
                      <table className="w-full text-[8.5px] text-left border-collapse font-mono">
                        <thead>
                          <tr className="border-b border-slate-800 text-slate-400">
                            <th className="py-0.5 px-1">Date</th>
                            <th className="py-0.5 px-1">Time</th>
                            <th className="py-0.5 px-1">Dir</th>
                            <th className="py-0.5 px-1 text-right">Entry</th>
                            <th className="py-0.5 px-1 text-right">EMA8</th>
                            <th className="py-0.5 px-1 text-right">MFE bef SL</th>
                            <th className="py-0.5 px-1 text-center">+20 First?</th>
                            <th className="py-0.5 px-1 text-center">-15 SL First?</th>
                            <th className="py-0.5 px-1 text-right">Outcome</th>
                          </tr>
                        </thead>
                        <tbody className="divide-y divide-slate-900">
                          {sim.audit_ledger_last_15.map((tr: any, idx: number) => {
                            const isWin = tr.final_outcome.startsWith("WIN");
                            const isLoss = tr.final_outcome.startsWith("LOSS");
                            return (
                              <tr key={idx} className="hover:bg-slate-900/70">
                                <td className="py-0.5 px-1 text-slate-300">{tr.date}</td>
                                <td className="py-0.5 px-1 text-slate-400">{tr.time}</td>
                                <td className={`py-0.5 px-1 font-bold ${tr.long ? "text-emerald-400" : "text-rose-400"}`}>
                                  {tr.dir}
                                </td>
                                <td className="py-0.5 px-1 text-right text-slate-200">{tr.entry_price.toFixed(1)}</td>
                                <td className="py-0.5 px-1 text-right text-cyan-300">{tr.ema_val.toFixed(1)}</td>
                                <td className="py-0.5 px-1 text-right text-emerald-300">+{tr.mfe_before_sl.toFixed(1)}</td>
                                <td className="py-0.5 px-1 text-center">
                                  {tr.hit_20_first ? <span className="text-emerald-400 font-bold">YES</span> : <span className="text-slate-500">NO</span>}
                                </td>
                                <td className="py-0.5 px-1 text-center">
                                  {tr.hit_sl_first ? <span className="text-rose-400 font-bold">YES</span> : <span className="text-slate-500">NO</span>}
                                </td>
                                <td className={`py-0.5 px-1 text-right font-bold ${isWin ? "text-emerald-400" : isLoss ? "text-rose-400" : "text-slate-400"}`}>
                                  {tr.final_outcome}
                                </td>
                              </tr>
                            );
                          })}
                        </tbody>
                      </table>
                    </div>
                  )}
                </div>
              </div>
            );
          })()}

          {/* Resistances & Supports Grid */}
          <div className="grid grid-cols-2 gap-2 mb-2.5">
            {/* Monotonic Resistances (Red) */}
            <div className="rounded-lg bg-rose-950/30 border border-rose-500/40 p-2">
              <div className="font-bold text-rose-300 text-[11px] mb-1.5 flex items-center justify-between border-b border-rose-500/20 pb-1">
                <span>🔴 HISTORICAL RESISTANCES (R1–R4)</span>
                <span className="text-[9px] text-rose-400">Strictly Higher Highs</span>
              </div>
              <div className="space-y-1.5">
                {gannData.resistances.map((r, idx) => (
                  <div key={idx} className="flex flex-col text-[10.5px]">
                    <div className="flex items-center justify-between font-mono">
                      <span className="font-bold text-rose-200">{r.level}: {r.price.toLocaleString("en-IN")}</span>
                      <div className="flex items-center gap-1">
                        {r.date && <span className="text-[9px] text-slate-400">{r.date}</span>}
                        {r.hit && <span className="bg-rose-500 text-white text-[8px] font-black px-1 rounded">✓ HIT</span>}
                      </div>
                    </div>
                    <span className="text-[9.5px] text-rose-300/80 leading-tight">
                      {idx === 0 ? "Yesterday High — test of recent liquidity & immediate cap." :
                       idx === 1 ? "2nd Historical Swing High — High-probability institutional exhaustion zone if paired with Call writing or Put unwinding." :
                       idx === 2 ? "3rd Historical Swing High — Multi-session expansion ceiling." :
                       "4th Historical Swing High — Institutional macro extreme."}
                    </span>
                  </div>
                ))}
              </div>
            </div>

            {/* Monotonic Supports (Green) */}
            <div className="rounded-lg bg-emerald-950/30 border border-emerald-500/40 p-2">
              <div className="font-bold text-emerald-300 text-[11px] mb-1.5 flex items-center justify-between border-b border-emerald-500/20 pb-1">
                <span>🟢 HISTORICAL SUPPORTS (S1–S4)</span>
                <span className="text-[9px] text-emerald-400">Strictly Lower Lows</span>
              </div>
              <div className="space-y-1.5">
                {gannData.supports.map((s, idx) => (
                  <div key={idx} className="flex flex-col text-[10.5px]">
                    <div className="flex items-center justify-between font-mono">
                      <span className="font-bold text-emerald-200">{s.level}: {s.price.toLocaleString("en-IN")}</span>
                      <div className="flex items-center gap-1">
                        {s.date && <span className="text-[9px] text-slate-400">{s.date}</span>}
                        {s.hit && <span className="bg-emerald-500 text-black text-[8px] font-black px-1 rounded">✓ HIT</span>}
                      </div>
                    </div>
                    <span className="text-[9.5px] text-emerald-300/80 leading-tight">
                      {idx === 0 ? "Yesterday Low — test of recent liquidity & immediate floor." :
                       idx === 1 ? "2nd Historical Swing Low — High-probability institutional absorption zone if supported by Put writers." :
                       idx === 2 ? "3rd Historical Swing Low — Multi-session dip accumulation floor." :
                       "4th Historical Swing Low — Institutional macro floor."}
                    </span>
                  </div>
                ))}
              </div>
            </div>
          </div>

          {/* Unfilled Gaps Section */}
          <div className="rounded-lg bg-amber-950/20 border border-amber-500/30 p-2 mb-2.5">
            <div className="font-bold text-amber-300 text-[11px] mb-1 flex items-center justify-between border-b border-amber-500/20 pb-1">
              <span>⚡ UNFILLED DAILY OPENING GAPS (&gt; 5 pts)</span>
              <span className="text-[9px] text-amber-400">Institutional Vacuums & Magnets</span>
            </div>
            <div className="grid grid-cols-2 gap-2 text-[10.5px]">
              <div>
                <b className="text-amber-400 text-[10px]">Upper Gaps (Overhead Targets):</b>
                {gannData.upper_gaps.length === 0 ? <div className="text-slate-500 text-[10px]">None nearby</div> : (
                  gannData.upper_gaps.map((g, i) => (
                    <div key={i} className="flex items-center justify-between font-mono py-0.5">
                      <span className="text-amber-200">{g.level}: {g.price.toLocaleString("en-IN")}</span>
                      <span className="text-[9px] text-slate-400">{g.date} {g.hit ? "✓ HIT" : ""}</span>
                    </div>
                  ))
                )}
              </div>
              <div>
                <b className="text-purple-400 text-[10px]">Lower Gaps (Downside Targets):</b>
                {gannData.lower_gaps.length === 0 ? <div className="text-slate-500 text-[10px]">None nearby</div> : (
                  gannData.lower_gaps.map((g, i) => (
                    <div key={i} className="flex items-center justify-between font-mono py-0.5">
                      <span className="text-purple-200">{g.level}: {g.price.toLocaleString("en-IN")}</span>
                      <span className="text-[9px] text-slate-400">{g.date} {g.hit ? "✓ HIT" : ""}</span>
                    </div>
                  ))
                )}
              </div>
            </div>
          </div>

          {/* Gann Sq9 Vibrations */}
          <div className="rounded-lg bg-teal-950/20 border border-teal-500/30 p-2">
            <div className="font-bold text-teal-300 text-[11px] mb-1 flex items-center justify-between border-b border-teal-500/20 pb-1">
              <span>📐 GANN SQUARE OF 9 (±45°, ±90°, ±135°, ±180°)</span>
              <span className="text-[9px] text-teal-400">Formula: (√Close + deg/180)²</span>
            </div>
            <div className="grid grid-cols-2 gap-2 text-[10px] font-mono">
              <div>
                <span className="text-rose-300 font-bold block mb-0.5">Resistances (+degrees):</span>
                {gannData.gann_resistances.map((gr, i) => (
                  <div key={i} className="flex items-center justify-between py-0.2">
                    <span className="text-rose-200">+{gr.degrees}° ({gr.level.split("(")[0]}):</span>
                    <span className="text-slate-200">{gr.price != null ? gr.price.toLocaleString("en-IN") : "—"} {gr.hit ? "✓" : ""}</span>
                  </div>
                ))}
              </div>
              <div>
                <span className="text-teal-300 font-bold block mb-0.5">Supports (-degrees):</span>
                {gannData.gann_supports.map((gs, i) => (
                  <div key={i} className="flex items-center justify-between py-0.2">
                    <span className="text-teal-200">{gs.degrees}° ({gs.level.split("(")[0]}):</span>
                    <span className="text-slate-200">{gs.price != null ? gs.price.toLocaleString("en-IN") : "—"} {gs.hit ? "✓" : ""}</span>
                  </div>
                ))}
              </div>
            </div>
          </div>

          {/* Full-History Recalc Audit Footer */}
          {gannData.recalc_audit && (
            <div className="mt-2.5 pt-2 border-t border-gold-500/30 flex items-center justify-between text-[10px] text-slate-400">
              <span className="flex items-center gap-1.5 truncate">
                <span className="text-emerald-400">✅</span>
                <span>
                  Full History Scanned: <b className="text-slate-200">{gannData.recalc_audit.total_historical_days_scanned}</b> days (
                  <span className="font-mono text-slate-300">{gannData.recalc_audit.first_historical_date}</span> →{" "}
                  <span className="font-mono text-slate-300">{gannData.recalc_audit.latest_settled_date}</span>) · Auto-updates daily at 15:40 IST
                </span>
              </span>
              <button
                onClick={handleForceRecalc}
                disabled={recalcLoading}
                className="ml-2 shrink-0 rounded px-1.5 py-0.5 text-[9.5px] font-bold bg-slate-800 hover:bg-slate-700 border border-slate-600/60 text-slate-200 hover:text-white transition-colors flex items-center gap-1 disabled:opacity-50"
                title="Trigger full 5-year dataset recalculation & sync now"
              >
                <span>{recalcLoading ? "⏳" : "🔄"}</span>
                <span>Recalc</span>
              </button>
            </div>
          )}
        </div>
      )}

      {/* legend + latest signals (top-left) */}
      <div className="pointer-events-none absolute left-2 top-2 z-10 flex max-w-[70%] flex-col gap-1">
        <div className="flex flex-wrap gap-1">
          {legend.map(([k, label, color]) => (
            <span key={k} className="rounded bg-[#060b16]/80 px-1.5 py-0.5 text-[10px] text-slate-300 ring-1 ring-white/5">
              <span className="mr-1 inline-block h-2 w-2 rounded-sm align-middle" style={{ background: color }} />{label}
            </span>
          ))}
          {layers.advPa && (
            <button
              onClick={toggleAdvPaSniperMode}
              title="Toggle Sniper Mode (Clean POIs + TV Risk-Reward box) vs All 10 Raw Diagnostic Mode"
              className={`pointer-events-auto rounded px-2 py-0.5 text-[10px] font-bold border transition-colors ${
                advPaSniperMode
                  ? "bg-gold-500/20 text-gold-300 border-gold-500/60 hover:bg-gold-500/30"
                  : "bg-purple-500/20 text-purple-300 border-purple-500/60 hover:bg-purple-500/30"
              }`}
            >
              {advPaSniperMode ? "🎯 Sniper Mode (Clean)" : "🔬 Raw 10-Core"}
            </button>
          )}
        </div>
        {layers.smt && smtForming.map((f, idx) => (
          <span key={`smtf-${idx}`} title={f.text}
            className="rounded border border-dashed border-pink-400/70 bg-pink-500/10 px-1.5 py-0.5 text-[10px] text-pink-200">
            ⚡ SMT forming {f.dir === "bull" ? "▲" : "▼"} {f.price.toFixed(0)} · <i>provisional, not a signal</i>
          </span>
        ))}
        {layers.walls && writerBal && (() => {
          const cr = (v: number) => { const a = Math.abs(v); return a >= 1e7 ? `${(a / 1e7).toFixed(2)}Cr` : a >= 1e5 ? `${(a / 1e5).toFixed(1)}L` : `${Math.round(a)}`; };
          const { pcr, net, trend, maxPain, spot: wsp } = writerBal;
          const { score, bias, strong, support, resistance, reason } = writerVerdict(writerBal, walls);
          const tone = bias === "BULLISH" ? { bar: "#10b981", bg: "rgba(6,78,59,0.55)", txt: "text-emerald-300", badge: "bg-emerald-500/25 text-emerald-200" }
            : bias === "BEARISH" ? { bar: "#ef4444", bg: "rgba(76,5,25,0.55)", txt: "text-rose-300", badge: "bg-rose-500/25 text-rose-200" }
            : { bar: "#eab308", bg: "rgba(60,50,10,0.5)", txt: "text-amber-200", badge: "bg-amber-500/20 text-amber-200" };
          const action = bias === "NEUTRAL"
            ? `Balanced — fade the range. Long only on a close > ${resistance ? resistance.p.toLocaleString("en-IN") : "resistance"}, short on a close < ${support ? support.p.toLocaleString("en-IN") : "support"}.`
            : bias === "BULLISH"
              ? (net >= 30000000 || (pcr != null && pcr >= 1.15)
                  ? `🔥 WRITERS BULLISH (+${cr(net)} Net Put Support) — Staircase Floor Rising (Buy Dips to Put Wall ${support ? support.p.toLocaleString("en-IN") : ""}; Do Not Fade Rally)`
                  : `Writers favour upside. Buy dips toward ${support ? support.p.toLocaleString("en-IN") : "support"}; invalid on a close below it.`)
              : (net <= -30000000 || (pcr != null && pcr <= 0.85)
                  ? `🩸 WRITERS BEARISH (−${cr(Math.abs(net))} Net Call Resistance) — Staircase Ceiling Dropping (Sell Rallies to Call Wall ${resistance ? resistance.p.toLocaleString("en-IN") : ""}; Do Not Chase Dips)`
                  : `Writers favour downside. Sell rallies toward ${resistance ? resistance.p.toLocaleString("en-IN") : "resistance"}; invalid on a close above it.`);
          return (
            <div className="rounded-md px-2 py-1 text-[10px] ring-1 ring-white/10" style={{ background: tone.bg, borderLeft: `3px solid ${tone.bar}` }}>
              <div className="flex flex-wrap items-center gap-x-2 gap-y-0.5">
                <span className={`rounded px-1.5 py-0.5 text-[11px] font-bold ${tone.badge}`}>🖊 WRITERS: {strong ? "STRONG " : ""}{bias}</span>
                {support && resistance && (
                  <b className="text-slate-200">{support.p.toLocaleString("en-IN")} <span className="text-slate-500">⟷</span> {resistance.p.toLocaleString("en-IN")}</b>
                )}
                {maxPain != null && <span className="text-amber-300/90">🎯 {maxPain.toLocaleString("en-IN")}{wsp != null ? ` (${wsp >= maxPain ? "↓" : "↑"}${cr(Math.abs((wsp ?? 0) - maxPain))})` : ""}</span>}
                <span className={tone.txt}>— {reason}</span>
              </div>
              <div className={`mt-0.5 ${tone.txt}`}>{action}</div>
              <div className="mt-0.5 flex flex-wrap items-center gap-x-2 text-[9px] text-slate-400">
                <span>PCR <b className={pcr != null && pcr >= 1 ? "text-emerald-300" : "text-rose-300"}>{pcr != null ? pcr.toFixed(2) : "—"}</b>{trend !== "flat" ? (trend === "rising" ? " ↗" : " ↘") : ""}</span>
                <span>PE Δ <b className="text-emerald-300">+{cr(writerBal.peChg)}</b></span>
                <span>CE Δ <b className="text-rose-300">+{cr(writerBal.ceChg)}</b></span>
                <span>net <b className={net >= 0 ? "text-emerald-300" : "text-rose-300"}>{net >= 0 ? "+" : "−"}{cr(net)}</b></span>
                <span className="text-slate-500">score {score > 0 ? "+" : ""}{score}</span>
              </div>
            </div>
          );
        })()}
        {(() => { const act = (Object.keys(GUIDE) as (keyof Layers)[]).filter((k) => layers[k]); if (!act.length) return null;
          const hm2 = (t: number) => { const d = new Date(t * 1000); return `${String(d.getUTCHours()).padStart(2, "0")}:${String(d.getUTCMinutes()).padStart(2, "0")}`; };
          const nowOf = (k: string): string => {
            const a = analysis;
            if (k === "range" && a?.range) { const r = a.range;
              return `price in ${r.zone.toUpperCase()} (${Math.round(r.pos_pct)} %) · EQ ${r.eq.toFixed(0)} · OTE ${r.ote_band[0].toFixed(0)}–${r.ote_band[1].toFixed(0)} → ${/prem/i.test(r.zone) ? "don't buy CE here; PE / wait" : "discount: CE on a bullish trigger"}`; }
            if (k === "volume") { const v = a?.volume?.vwap?.slice(-1)[0]; const c = candles[candles.length - 1];
              return v && c ? `close ${c.close > v.v ? "ABOVE" : "BELOW"} VWAP ${v.v.toFixed(0)} → ${c.close > v.v ? "prefer CE" : "prefer PE"}` : ""; }
            if (k === "dol") { const d = [...dolRef.current].reverse().find((x) => !x.hit); return d ? `${d.label} (after ${d.why})` : "no active target"; }
            if (k === "bias") { const b = [...biasMap.current.values()].filter((r) => !r.forming).slice(-1)[0];
              return b ? `${hm2(b.time)}: ${b.verdict === "MIXED" ? "mixed" : b.verdict} ${b.bull}▲ ${b.bear}▼ of ${b.avail}${b.warn ? " · " + b.warn : ""}` : ""; }
            if ((k === "delta" || k === "deltaDetail") && a?.delta && !a.delta.available) return `⚠ no delta data — ${a.delta.message || "set Delta source = Estimated (FUT quote rule)"}`;
            if (k === "fp") { const f = fpPrim.current.bars.slice(-1)[0]; return f ? `${f.at}: Δ ${kfmt(f.delta)} · ${f.read}` : ""; }
            const ev = (EV_LAYER[k] ?? []).map((n) => layerNow[n]).filter(Boolean).sort((x, y) => y.time - x.time)[0];
            return ev ? `${hm2(ev.time)} ${ev.short}${KIND_ACT[ev.kind] ? ` → ${KIND_ACT[ev.kind]}` : ""}` : "";
          };
          return <div className="pointer-events-auto max-h-[46vh] w-[460px] max-w-full overflow-auto rounded bg-[#060b16]/90 px-2 py-1 text-[10.5px] ring-1 ring-gold-500/30">
            <button className="font-semibold text-gold-400" onClick={() => { const v = !guideOpen; setGuideOpen(v); try { localStorage.setItem("ict_guide", v ? "1" : "0"); } catch { /* */ } }}>
              {guideOpen ? "▾" : "▸"} ⓘ What am I looking at? ({act.length} layer{act.length > 1 ? "s" : ""})</button>
            {guideOpen && act.map((k) => { const g = GUIDE[k]; const now = nowOf(k); return (
              <div key={k} className="mt-1 border-t border-ink-700 pt-1">
                <b className="text-slate-100">{g.name}</b> <span className="text-slate-400">— {g.what}</span>
                {now && <div className="text-cyan-200">▸ now: {now}</div>}
                <div className="text-amber-200">▸ do: {g.act}</div>
              </div>); })}
          </div>; })()}
        {layers.heat && <div className="pointer-events-auto w-[520px] max-w-full rounded bg-[#060b16]/92 px-2.5 py-1.5 text-[13px] leading-snug ring-1 ring-orange-400/40">
          <button className="font-semibold text-orange-300" onClick={() => { const v = !heatHelp; setHeatHelp(v); try { localStorage.setItem("ict_heat_help", v ? "1" : "0"); } catch { /* */ } }}>
            {heatHelp ? "▾" : "▸"} 🔥 How to read the heatmap</button>
          {heatHelp && <div className="mt-1 space-y-1 text-slate-200">
            <div><b>Where the volume sat:</b> the hottest price row, and how the volume splits across the low, middle and high of the candle. Hover a candle to read it.</div>
            <div className="text-bear-400">🔴 <b>Hot at the top, weak close</b> — sellers absorbed the buyers (distribution). PE bias.</div>
            <div className="text-bull-400">🟢 <b>Hot at the bottom, strong close</b> — buyers absorbed the sellers (accumulation). CE bias; that band is support.</div>
            <div className="text-bull-400">🟢 <b>Hot at the top and closed there</b> — buyers accepted higher prices. Healthy trend; the next pullback to that band is the CE area.</div>
            <div className="text-bear-400">🔴 <b>Hot at the bottom and closed there</b> — sellers accepted lower prices. That band becomes resistance.</div>
            <div className="text-slate-300">⚪ <b>Hot in the middle</b> — balanced, no winner. Wait for the next candle.</div>
            <div className="text-amber-300">⚠ <b>Extra warnings:</b> both ends hot with an empty middle (a gap that will get retested) · a big body with little volume inside (easy move, often retested) · delta disagreeing with the candle colour (absorption).</div>
          </div>}
        </div>}
        {layers.fp && !fpMsg && <div className="pointer-events-auto rounded bg-[#060b16]/85 px-2 py-1 text-[10px] ring-1 ring-white/5">
          <span className="mr-1 font-semibold text-amber-300">Footprint key</span>
          {([["A", "ABSORB", "big orders soak, price stuck"], ["D", "DISTRIB", "sellers unload at top"], ["C", "ACCUM", "buyers load at bottom"],
            ["M", "TRAP", "wick sweep, closed back"], ["S", "STACK", "one-sided push"], ["I", "PUSH", "aggressive side drives"]] as const).map(([l, w, m]) => (
            <span key={l} className="mr-2" title={m}><b className="rounded bg-teal-500/80 px-1 text-[#0b1220]">{l}</b> {w}</span>))}
          <span className="text-slate-500">teal = bull · pink = bear · gold tick = POC · <b className="text-slate-300">BUY/SELL ✓3</b> = footprint + delta + structure agree</span>
        </div>}
        {layers.bias && ocFeed?.gex?.ok && (() => { const g = ocFeed.gex!; const sp = g.spot ?? 0;
          const pinNear = g.pin != null && Math.abs(sp - g.pin) <= sp * 0.0025;
          const txt = pinNear ? `📌 PIN zone ${g.pin!.toLocaleString("en-IN")} — biggest gamma, spot ${Math.abs(sp - g.pin!).toFixed(0)} pts away: expect a range, fade the edges, don't chase breakouts`
            : g.regime === "negative" ? `⚡ TREND zone (negative gamma${g.flip ? `, flip ${g.flip.toLocaleString("en-IN")}` : ""}) — moves can run, breakout badges more reliable`
            : `〰 Range-prone (positive gamma${g.flip ? `, flip ${g.flip.toLocaleString("en-IN")}` : ""}) — moves get absorbed; magnet ${g.pin?.toLocaleString("en-IN")}`;
          return <div className="pointer-events-auto rounded bg-[#060b16]/85 px-2 py-0.5 text-[10.5px] text-violet-200 ring-1 ring-violet-400/30"
            title="GEX from 8501 Options Lab (latest snapshot). Context, not a vote. Dealer-sign convention is the US one — treat as a hint.">{txt}</div>; })()}
        {fpMsg && <div className="rounded bg-[#060b16]/85 px-2 py-0.5 text-[10px] text-amber-300">{fpMsg}</div>}
        {latest.length > 0 && (
          <div className="rounded bg-[#060b16]/85 px-2 py-1 text-[11px] ring-1 ring-white/5">
            <span className="mr-1 font-semibold text-gold-400">Latest</span>
            {latest.map((e, i) => (
              <span key={i} className="mr-2" style={{ color: e.color }}>
                <span className="text-slate-500">{formatTickerTime(e.time)}</span> {e.short}
              </span>
            ))}
          </div>
        )}
      </div>
      {/* Multi-timeframe panel (bottom-left) */}
      <div className="pointer-events-none absolute bottom-9 left-2 z-10 flex">
        <MtfPanel />
      </div>

      {/* Top Sniper Decision HUD */}
      {layers.advPa && analysis?.pa_advanced?.sniper && (() => {
        const sniper = analysis.pa_advanced.sniper;
        const isRejPe = !isEodCutoff && (sniper.action.includes("BUY PE") || sniper.state.includes("REJECTION") || sniper.state.includes("SHORT"));
        const isRejCe = !isEodCutoff && (sniper.action.includes("BUY CE") || (sniper.state.includes("REJECTION") && sniper.action.includes("CE")));
        const isBreak = !isEodCutoff && (sniper.state.includes("BREAKOUT") || sniper.state.includes("BREAKDOWN"));
        const isTrap = !isEodCutoff && sniper.state.includes("TRAP");

        const displayState = isEodCutoff ? "🏁 EOD CLOSING FLOW" : sniper.state;
        const displayBehavior = isEodCutoff ? "BOOK / TRAIL ONLY" : sniper.behavior;
        const displayHeadline = isEodCutoff
          ? "🏁 EOD CLOSING FLOW (15:10+ IST) — Book/Trail Existing Positions Only (No Fresh Intraday Option Entries)"
          : sniper.headline;

        const badgeClass = isEodCutoff
          ? "bg-slate-900/90 text-amber-300 border-amber-500/70"
          : isTrap
          ? "bg-rose-950/90 text-rose-300 border-rose-600/70"
          : isBreak
          ? "bg-cyan-950/90 text-cyan-300 border-cyan-500/70"
          : isRejCe
          ? "bg-emerald-950/90 text-emerald-300 border-emerald-500/70"
          : isRejPe
          ? "bg-rose-950/90 text-rose-300 border-rose-500/70"
          : "bg-amber-950/90 text-amber-300 border-amber-500/70";

        const badgeIcon = isEodCutoff ? "🏁" : isTrap ? "🚫" : isBreak ? "⚡" : isRejCe || isRejPe ? "🎯" : "⏳";

        return (
          <div className="pointer-events-auto absolute left-1/2 top-2 z-30 -translate-x-1/2 max-w-[calc(100%-190px)]">
            <div className="flex flex-wrap items-center gap-2 rounded-xl border border-gold-500/50 bg-[#060b16]/95 px-3 py-1.5 shadow-2xl backdrop-blur-md ring-1 ring-gold-500/30 text-[12px]">
              <span className={`flex items-center gap-1 rounded px-2 py-0.5 font-extrabold text-[11px] border ${badgeClass}`}>
                <span>{badgeIcon}</span>
                <span>{displayState}</span>
              </span>

              <span className="rounded bg-slate-900/90 px-2 py-0.5 text-[11px] text-slate-300 border border-slate-700/60 font-medium">
                🛡️ {displayBehavior}
              </span>

              <span className="text-slate-100 font-semibold text-[11.5px] truncate max-w-[480px] xl:max-w-[620px]" title={displayHeadline}>
                {displayHeadline}
              </span>

              <button
                onClick={toggleAdvPaSniperMode}
                title="Toggle between clean Confluence Sniper Mode and full 10-core diagnostic view"
                className={`ml-auto flex items-center gap-1 rounded px-2 py-0.5 text-[11px] font-bold border transition-colors ${
                  advPaSniperMode
                    ? "bg-gold-500/20 text-gold-300 border-gold-500/60 hover:bg-gold-500/30"
                    : "bg-purple-500/20 text-purple-300 border-purple-500/60 hover:bg-purple-500/30"
                }`}
              >
                <span>{advPaSniperMode ? "🎯 Sniper View" : "🔬 Raw 10-Core"}</span>
              </button>
            </div>
          </div>
        );
      })()}

      {/* 🎯 Master Confluence HUD Card (OI Walls + CVD Order Flow + PE−CE Lifecycle Synergy) */}
      {(layers.walls || layers.ocAct || layers.cvd) && confluenceTrinity && (
        <div className={`pointer-events-auto absolute left-1/2 z-20 -translate-x-1/2 max-w-[calc(100%-190px)] ${layers.advPa && analysis?.pa_advanced?.sniper ? "top-[48px]" : "top-2"}`}>
          <div className={`flex flex-wrap items-center gap-2 rounded-xl px-3 py-1.5 shadow-2xl backdrop-blur-md border transition-all ${
            confluenceTrinity.trinityType === "SHORT_CE_WALL"
              ? "bg-gradient-to-r from-rose-950/95 via-[#180a14]/95 to-ink-950/95 border-rose-500 ring-2 ring-rose-500/50 animate-pulse"
              : confluenceTrinity.trinityType === "BUY_PE_SUPPORT"
              ? "bg-gradient-to-r from-emerald-950/95 via-[#0a1814]/95 to-ink-950/95 border-emerald-500 ring-2 ring-emerald-500/50 animate-pulse"
              : ofNow?.verdict === "BUY"
              ? "bg-bull-900/90 border-bull-500/70 ring-1 ring-bull-400"
              : ofNow?.verdict === "SELL"
              ? "bg-bear-900/90 border-bear-500/70 ring-1 ring-bear-400"
              : "bg-[#060b16]/95 border-ink-700 ring-1 ring-white/10"
          }`}>
            {/* Verdict Badge */}
            {confluenceTrinity.trinityType === "SHORT_CE_WALL" ? (
              <span className="flex items-center gap-1.5 rounded-lg bg-rose-600 px-2 py-0.5 font-black text-white text-[11px] shadow">
                <span>🎯</span>
                <span>A+ SNIPER CONFLUENCE: SHORT CE WALL (CVD DISTRIBUTION + WRITER ACCELERATION)</span>
              </span>
            ) : confluenceTrinity.trinityType === "BUY_PE_SUPPORT" ? (
              <span className="flex items-center gap-1.5 rounded-lg bg-emerald-600 px-2 py-0.5 font-black text-white text-[11px] shadow">
                <span>🎯</span>
                <span>A+ SNIPER CONFLUENCE: BUY PE SUPPORT (CVD ABSORPTION + WRITER DEFENSE)</span>
              </span>
            ) : ofNow ? (
              <span className={`rounded px-2 py-0.5 text-[14px] font-extrabold ${VC[ofNow.verdict]}`}>
                {ofNow.verdict}
              </span>
            ) : (
              <span className="rounded bg-ink-800 px-2 py-0.5 text-[11px] font-bold text-slate-300 border border-ink-700">
                SNIPER CONFLUENCE ({confluenceTrinity.confluenceScore}/3)
              </span>
            )}

            {/* 3 Pillars Telemetry */}
            <div className="flex items-center gap-1.5 text-[10.5px] font-mono flex-wrap">
              {/* Pillar 1: Wall Proximity */}
              <span className={`px-1.5 py-0.2 rounded border ${
                confluenceTrinity.isNearCe
                  ? "bg-rose-950/80 text-rose-300 border-rose-600/70 font-bold"
                  : confluenceTrinity.isNearPe
                  ? "bg-emerald-950/80 text-emerald-300 border-emerald-600/70 font-bold"
                  : "bg-ink-850 text-slate-300 border-ink-700"
              }`}>
                🏛️ {confluenceTrinity.nearestCe && (!confluenceTrinity.nearestPe || confluenceTrinity.ceDist <= confluenceTrinity.peDist)
                  ? `${confluenceTrinity.nearestCe.p} CE (${confluenceTrinity.ceDist.toFixed(0)}p, ${confluenceTrinity.nearestCe.status === "building" ? "CAP STACKING" : "WALL"})`
                  : confluenceTrinity.nearestPe
                  ? `${confluenceTrinity.nearestPe.p} PE (${confluenceTrinity.peDist.toFixed(0)}p, ${confluenceTrinity.nearestPe.status === "building" ? "FLOOR FORTIFYING" : "SUPPORT"})`
                  : "Walls Active"}
              </span>

              {/* Pillar 2: CVD Order Flow */}
              <span className={`px-1.5 py-0.2 rounded border ${
                confluenceTrinity.isCvdBear
                  ? "bg-rose-950/80 text-rose-300 border-rose-600/70 font-bold"
                  : confluenceTrinity.isCvdBull
                  ? "bg-emerald-950/80 text-emerald-300 border-emerald-600/70 font-bold"
                  : "bg-ink-850 text-slate-300 border-ink-700"
              }`}>
                〽️ {confluenceTrinity.cvdSummary}
              </span>

              {/* Pillar 3: Table Lifecycle State & Slope */}
              <span className={`px-1.5 py-0.2 rounded border ${
                confluenceTrinity.actionTone === "bear"
                  ? "bg-rose-950/80 text-rose-300 border-rose-600/70 font-bold"
                  : confluenceTrinity.actionTone === "bull"
                  ? "bg-emerald-950/80 text-emerald-300 border-emerald-600/70 font-bold"
                  : "bg-ink-850 text-slate-300 border-ink-700"
              }`}>
                📐 <span className={confluenceTrinity.callAngle >= 25 ? "text-rose-400 font-bold" : "text-slate-300"}>Call +{confluenceTrinity.callAngle.toFixed(0)}°</span> | <span className={confluenceTrinity.putAngle >= 25 ? "text-emerald-400 font-bold" : "text-slate-300"}>Put {confluenceTrinity.putAngle >= 0 ? "+" : ""}{confluenceTrinity.putAngle.toFixed(0)}°</span>
                {confluenceTrinity.isScissor ? " · ✂️ Scissor" : ""} · 🎯 S{confluenceTrinity.stage} {confluenceTrinity.stageBadge}
                {confluenceTrinity.dynamicSl ? ` · SL ${confluenceTrinity.dynamicSl}` : ""}
              </span>

              {/* Pillar 4: ATM/ITM Writer Conviction & Vacuum */}
              {itmAtmIntel && (
                <span className={`px-1.5 py-0.2 rounded border font-bold flex items-center gap-1 ${
                  itmAtmIntel.vacuum_active
                    ? "bg-purple-950/90 text-purple-300 border-purple-500 animate-pulse"
                    : itmAtmIntel.is_panic_unwinding
                    ? "bg-red-950/90 text-red-300 border-red-500 animate-pulse"
                    : itmAtmIntel.is_hollow_rally
                    ? "bg-rose-950/90 text-rose-300 border-rose-500 animate-pulse"
                    : itmAtmIntel.is_pcr_overbought
                    ? "bg-amber-950/90 text-amber-300 border-amber-500"
                    : "bg-ink-850 text-slate-300 border-ink-700"
                }`} title={itmAtmIntel.hollow_rally_warning || itmAtmIntel.vacuum_signal || itmAtmIntel.panic_unwinding_banner || itmAtmIntel.pcr_shield_banner || itmAtmIntel.iron_wall_star_divergence_alert || "ATM/ITM Writer Conviction Active"}>
                  {itmAtmIntel.vacuum_active
                    ? "🚀 OTM VACUUM ACTIVE"
                    : itmAtmIntel.is_panic_unwinding
                    ? "🩸 LONG UNWINDING (< 23,220)"
                    : itmAtmIntel.is_hollow_rally
                    ? "⚠️ HOLLOW RALLY (PE Parked)"
                    : itmAtmIntel.is_pcr_overbought
                    ? `🛑 OVERBOUGHT PCR (${itmAtmIntel.pcr})`
                    : "🏛️ WRITER CONVICTION SOLID"}
                </span>
              )}
            </div>
          </div>
        </div>
      )}

      {/* ⚔️ Prahaar Directional Option-Buying HUD Card */}
      {isPrahaarOn && prahaarResult.latestState && (
        <div className={`pointer-events-auto absolute left-1/2 z-20 -translate-x-1/2 max-w-[calc(100%-190px)] ${
          layers.advPa && analysis?.pa_advanced?.sniper
            ? ((layers.walls || layers.ocAct || layers.cvd) && confluenceTrinity ? "top-[94px]" : "top-[48px]")
            : ((layers.walls || layers.ocAct || layers.cvd) && confluenceTrinity ? "top-[48px]" : "top-2")
        }`}>
          <div className={`flex flex-wrap items-center gap-2 rounded-xl px-3 py-1 shadow-2xl backdrop-blur-md border transition-all ${
            prahaarResult.latestState.isMasterSniperPut
              ? "bg-gradient-to-r from-rose-950/95 via-[#180a14]/95 to-ink-950/95 border-rose-500 ring-2 ring-rose-500/50 animate-pulse text-rose-200"
              : prahaarResult.latestState.isMasterSniperCall
              ? "bg-gradient-to-r from-emerald-950/95 via-[#0a1814]/95 to-ink-950/95 border-emerald-500 ring-2 ring-emerald-500/50 animate-pulse text-emerald-200"
              : prahaarResult.latestState.currentVerdict === "BUY ATM PUT"
              ? "bg-gradient-to-r from-rose-950/90 to-ink-950/90 border-rose-600/70 text-rose-300"
              : prahaarResult.latestState.currentVerdict === "BUY ATM CALL"
              ? "bg-gradient-to-r from-emerald-950/90 to-ink-950/90 border-emerald-600/70 text-emerald-300"
              : prahaarResult.latestState.isImpulseActive
              ? (prahaarResult.latestState.emaTrend === "bearish"
                  ? "bg-gradient-to-r from-rose-950/85 to-ink-950/90 border-rose-600/60 text-rose-200 shadow-rose-950/40"
                  : "bg-gradient-to-r from-emerald-950/85 to-ink-950/90 border-emerald-600/60 text-emerald-200 shadow-emerald-950/40")
              : "bg-[#060b16]/90 border-ink-700 text-slate-300"
          }`}>
            <span className="font-extrabold uppercase tracking-wider text-[11px] flex items-center gap-1">
              <span>⚔️ PRAHAAR</span>
            </span>
            <span className={`px-2 py-0.5 rounded text-[10px] font-mono font-black ${
              isEodCutoff
                ? "bg-amber-500 text-black shadow"
                : prahaarResult.latestState.currentVerdict === "BUY ATM PUT"
                ? "bg-rose-500 text-black shadow"
                : prahaarResult.latestState.currentVerdict === "BUY ATM CALL"
                ? "bg-emerald-500 text-black shadow"
                : prahaarResult.latestState.isImpulseActive
                ? (prahaarResult.latestState.emaTrend === "bearish"
                    ? "bg-rose-600 text-white shadow ring-1 ring-rose-400"
                    : "bg-emerald-600 text-white shadow ring-1 ring-emerald-400")
                : "bg-ink-800 text-slate-400"
            }`}>
              {isEodCutoff
                ? "🏁 EOD CLOSING (NO FRESH ENTRIES)"
                : prahaarResult.latestState.currentVerdict === "BUY ATM PUT"
                ? `🔴 BUY ${prahaarResult.latestState.atmStrike} PE`
                : prahaarResult.latestState.currentVerdict === "BUY ATM CALL"
                ? `🟢 BUY ${prahaarResult.latestState.atmStrike} CE`
                : prahaarResult.latestState.isImpulseActive
                ? `⚡ IMPULSE ACTIVE / ${prahaarResult.latestState.emaTrend === "bearish" ? "BEARISH TREND" : prahaarResult.latestState.emaTrend === "bullish" ? "BULLISH TREND" : "EXPANDING"}`
                : "CHOP / STANDBY"}
            </span>
            {prahaarResult.latestState.isImpulseActive && (
              <span className="px-1.5 py-0.5 rounded text-[9.5px] font-black bg-cyan-500/20 text-cyan-300 border border-cyan-400/60 animate-pulse">
                ⚡ IMPULSE ACTIVE
              </span>
            )}
            {(prahaarResult.latestState.isMasterSniperPut || prahaarResult.latestState.isMasterSniperCall) && (
              <span className="px-1.5 py-0.5 rounded text-[9.5px] font-black bg-gold-500/30 text-gold-300 border border-gold-400/80 animate-pulse">
                🎯 A+ MASTER SNIPER
              </span>
            )}
            {isHighRvolConfirmed && hasBigTradesNear && (
              <span className="px-1.5 py-0.5 rounded text-[9.5px] font-black bg-emerald-500/20 text-emerald-300 border border-emerald-400/60 animate-pulse">
                ⚡ {curSlotRvol.toFixed(1)}x RVOL CONFIRMED
              </span>
            )}
            {isWeakRvol && (
              <span className="px-1.5 py-0.5 rounded text-[9.5px] font-bold bg-amber-500/20 text-amber-300 border border-amber-400/60">
                ⚠️ WEAK RVOL ({curSlotRvol.toFixed(2)}x) — DO NOT CHASE
              </span>
            )}
            <span className="text-[10.5px] font-mono text-slate-400 flex items-center gap-1.5">
              <span>EMA <b className={prahaarResult.latestState.emaTrend === "bullish" ? "text-emerald-300" : prahaarResult.latestState.emaTrend === "bearish" ? "text-rose-300" : "text-slate-300"}>{prahaarResult.latestState.emaTrend.toUpperCase()}</b></span>
              <span>·</span>
              <span>MACD <b className={prahaarResult.latestState.macdMomentum === "bullish" ? "text-emerald-300" : prahaarResult.latestState.macdMomentum === "bearish" ? "text-rose-300" : "text-slate-300"}>{prahaarResult.latestState.macdHist >= 0 ? "+" : ""}{prahaarResult.latestState.macdHist.toFixed(2)}</b></span>
              <span>·</span>
              <span>ADX <b className={prahaarResult.latestState.adxTrending ? "text-cyan-300" : "text-slate-500"}>{prahaarResult.latestState.adx.toFixed(1)}</b> {prahaarResult.latestState.adxTrending ? "⚡" : "💤"}</span>
              <span>·</span>
              <span>VIX <b className={prahaarResult.latestState.vixOk ? "text-amber-300" : "text-rose-400"}>{prahaarResult.latestState.vix ? prahaarResult.latestState.vix.toFixed(1) : "—"}</b></span>
            </span>
          </div>
        </div>
      )}

      {/* 🏛️ GTF Institutional Demand & Supply HUD Card */}
      {isGtfOn && (
        <div className={`pointer-events-auto absolute left-1/2 z-20 -translate-x-1/2 max-w-[calc(100%-190px)] ${
          isPrahaarOn && prahaarResult.latestState
            ? (layers.advPa && analysis?.pa_advanced?.sniper
                ? ((layers.walls || layers.ocAct || layers.cvd) && confluenceTrinity ? "top-[132px]" : "top-[86px]")
                : ((layers.walls || layers.ocAct || layers.cvd) && confluenceTrinity ? "top-[86px]" : "top-[40px]"))
            : (layers.advPa && analysis?.pa_advanced?.sniper
                ? ((layers.walls || layers.ocAct || layers.cvd) && confluenceTrinity ? "top-[94px]" : "top-[48px]")
                : ((layers.walls || layers.ocAct || layers.cvd) && confluenceTrinity ? "top-[48px]" : "top-2"))
        }`}>
          <div className="flex flex-wrap items-center gap-2 rounded-xl px-3 py-1 shadow-2xl backdrop-blur-md border bg-[#060b16]/90 border-emerald-500/40 text-slate-200">
            <span className="font-extrabold uppercase tracking-wider text-[11px] flex items-center gap-1 text-emerald-400">
              <span>🏛️ GTF 2.0</span>
            </span>
            <span className="px-1.5 py-0.5 rounded text-[10px] font-mono font-bold uppercase tracking-wider bg-ink-800 text-amber-300 border border-amber-500/30">
              {gtfMode}
            </span>
            {gtfResult.nearestDemand && (
              <span className="text-[10.5px] font-mono text-emerald-300 flex items-center gap-1">
                <span className="text-slate-400">Demand:</span>
                <b>[{gtfResult.nearestDemand.bottom.toFixed(0)}–{gtfResult.nearestDemand.top.toFixed(0)}]</b>
                <span className="text-[9px] px-1 py-0.2 rounded bg-emerald-950/80 border border-emerald-500/30 text-emerald-300 font-semibold">{gtfResult.nearestDemand.status} ({gtfResult.nearestDemand.grade})</span>
              </span>
            )}
            {gtfResult.nearestSupply && (
              <span className="text-[10.5px] font-mono text-rose-300 flex items-center gap-1">
                <span className="text-slate-400">Supply:</span>
                <b>[{gtfResult.nearestSupply.bottom.toFixed(0)}–{gtfResult.nearestSupply.top.toFixed(0)}]</b>
                <span className="text-[9px] px-1 py-0.2 rounded bg-rose-950/80 border border-rose-500/30 text-rose-300 font-semibold">{gtfResult.nearestSupply.status} ({gtfResult.nearestSupply.grade})</span>
              </span>
            )}
            <span className="text-[10px] text-slate-400 font-mono">
              Fresh: <b className="text-emerald-400">{gtfResult.freshDemands.length}D</b> · <b className="text-rose-400">{gtfResult.freshSupplies.length}S</b>
            </span>
            {isHighRvolConfirmed && hasBigTradesNear && (
              <span className="px-1.5 py-0.5 rounded text-[9.5px] font-black bg-emerald-500/20 text-emerald-300 border border-emerald-400/60 animate-pulse">
                ⚡ {curSlotRvol.toFixed(1)}x RVOL CONFIRMED
              </span>
            )}
            {isWeakRvol && (
              <span className="px-1.5 py-0.5 rounded text-[9.5px] font-bold bg-amber-500/20 text-amber-300 border border-amber-400/60">
                ⚠️ WEAK RVOL ({curSlotRvol.toFixed(2)}x) — CHOP RISK
              </span>
            )}
          </div>
        </div>
      )}

      {/* Fallback when layers.walls is off */}
      {!layers.walls && ofNow && (
        <div className={`pointer-events-none absolute left-1/2 z-10 -translate-x-1/2 ${layers.advPa && analysis?.pa_advanced?.sniper ? "top-[48px]" : "top-2"}`}>
          <div className={`flex max-w-[560px] items-center gap-2 rounded-lg px-3 py-1.5 shadow-xl ring-2 ${ofNow.verdict === "BUY" ? "bg-bull-900/90 ring-bull-400" : ofNow.verdict === "SELL" ? "bg-bear-900/90 ring-bear-400" : "bg-ink-850/90 ring-slate-500"}`}>
            <span className={`rounded px-2 py-0.5 text-[16px] font-extrabold ${VC[ofNow.verdict]}`}>{ofNow.verdict}</span>
            <span className="text-[12.5px] leading-tight text-slate-100"><b className="text-cyan-200">ORDER FLOW · last closed {hm(lastClosed!.time)}</b><br />{ofNow.head}</span>
          </div>
        </div>
      )}
      {layers.gamma && <div className={`pointer-events-none absolute left-1/2 z-10 -translate-x-1/2 ${layers.advPa && analysis?.pa_advanced?.sniper ? (ofNow ? "top-[96px]" : "top-[54px]") : "top-[64px]"}`}>
        <div className={`max-w-[600px] rounded-lg px-3 py-1 text-[12.5px] shadow-xl ring-2 ${gLast?.ok ? "bg-violet-900/90 ring-violet-400 text-white" : "bg-ink-850/90 ring-slate-600 text-slate-200"}`}>
          <b className="text-violet-300">γ MAP</b>{gexMap ? <> · <b className={gexMap.regime === "positive" ? "text-bull-400" : "text-amber-300"}>{gexMap.regime === "positive" ? "+γ PIN day" : "−γ TREND day"}</b>
            {gexMap.flip ? ` · flip ${gexMap.flip}` : ""}</> : " · loading option chain…"}
          {gLast && <> · last closed: <b>{gLast.ok ? `✓✓✓ ${gLast.side}` : `WALK AWAY (${gLast.checks.filter((x) => !x.pass).map((x) => x.name).join(", ")})`}</b></>}
        </div>
      </div>}
      {layers.stdv && (
        <div className="absolute left-2 top-11 z-20 flex items-center gap-2 rounded border border-red-500/40 bg-[#060b16]/90 px-2.5 py-1 text-[11.5px] shadow-lg backdrop-blur">
          <span className="font-extrabold text-red-400">📐 STDV</span>
          <span className={`px-1.5 py-0.5 rounded text-[10px] font-bold uppercase tracking-wider ${
            activeDisplayLeg?.anchorMode === "manual"
              ? "bg-amber-500/20 text-amber-300 border border-amber-500/40"
              : "bg-cyan-500/20 text-cyan-300 border border-cyan-500/40"
          }`}>
            {activeDisplayLeg?.anchorMode === "manual" ? "📍 Manual Leg" : "🤖 System Auto"}
          </span>
          {activeDisplayLeg ? (
            <span className="text-slate-200">
              <b className={activeDisplayLeg.dir === "bull" ? "text-bull-400" : "text-bear-400"}>
                {activeDisplayLeg.dir === "bull" ? "▲ BULL" : "▼ BEAR"}
              </b>{" "}
              [{activeDisplayLeg.tf}] · {activeDisplayLeg.anchorMode === "orderblock" ? "Order Block Run" : activeDisplayLeg.anchorMode === "swing" ? "Full Judas" : "Custom Manual"} ·
              Anchor 1 <b className="text-white">{activeDisplayLeg.anchor1.toFixed(1)}</b> → Anchor 0 <b className="text-white">{activeDisplayLeg.anchor0.toFixed(1)}</b>
            </span>
          ) : (
            <span className="text-slate-400">Auto-detecting manipulation leg…</span>
          )}
          {stdvLeg && onResetToAutoStdv && (
            <button
              onClick={onResetToAutoStdv}
              title="Clear custom 2-click leg and let the system automatically detect & mark the ICT manipulation leg"
              className="rounded bg-cyan-950/90 px-2 py-0.5 font-bold text-cyan-300 hover:bg-cyan-800 border border-cyan-500/50 transition-colors shadow flex items-center gap-1"
            >
              <span>🤖</span>
              <span>Use System Auto</span>
            </button>
          )}
          <button
            onClick={isStdvPicking ? onCancelStdvPicking : () => onStartStdvPicking?.()}
            className={`rounded px-2 py-0.5 font-bold transition-all ${
              isStdvPicking
                ? "bg-red-600 text-white animate-pulse"
                : "bg-red-950/80 text-red-300 hover:bg-red-900 border border-red-500/50"
            }`}
          >
            {isStdvPicking ? "✕ Cancel" : "📍 2-Click Chart Tool"}
          </button>
        </div>
      )}
      {isStdvPicking && (
        <div className="absolute left-1/2 top-4 z-40 -translate-x-1/2">
          <div className="flex items-center gap-3 rounded-lg border-2 border-red-500 bg-[#060b16]/95 px-4 py-2 text-[12.5px] shadow-2xl animate-pulse">
            <span className="font-extrabold text-red-400">📍 2-CLICK STDV TOOL</span>
            <span className="text-white">
              {pickStep === 1
                ? "👉 Click Point 1 on chart: The Sweep Wick (Anchor 1)"
                : `✓ Anchor 1 saved (${pickPoint1?.price.toFixed(1)}) → Click Point 2: Shift High/Low (Anchor 0)`}
            </span>
            <button
              onClick={onCancelStdvPicking}
              className="rounded bg-red-600 px-2.5 py-0.5 font-bold text-white hover:bg-red-500 text-[11px]"
            >
              Cancel (Esc)
            </button>
          </div>
        </div>
      )}
      {/* chart tools (top-right, left of the price axis) */}

      {layers.oi && <div className="pointer-events-none absolute left-3 z-20 max-w-[88%] rounded border border-slate-600 bg-ink-950/95 px-2 py-1 text-[10px] text-slate-300" style={{ bottom: layers.rvol ? 175 : 28 }}>
        <div>FUT OI · ΔOI <b>{oiNumber(oiLatest?.doi)}</b> · {oiNumber(oiLatest?.doi_pct, "%")} · chart ΔP {oiNumber(oiLatest?.dp)} · <b>{oiLatest?.build || "No comparable bar"}</b></div>
        <div>Contract {oiHealth?.contract || "unverified"} · {oiHealth?.source || "—"} · sampled {oiAt} IST · {oiState || "quality unavailable"}{oiHealth?.sample_quality && oiHealth.sample_quality !== "ok" ? ` / ${oiHealth.sample_quality}` : ""} · {oiAge != null ? `age ${oiAge}s` : ""}</div>
        <div className="text-slate-400">Amber: expansion · violet: reduction · relative uses earlier {oiHealth?.relative_window ?? "—"} bars; warm-up stays blank. Context only.</div>
      </div>}
      {layers.bias && <div className="absolute left-3 z-30 max-w-[90%] rounded border border-gold-500/50 bg-[#080f1e]/95 px-2 py-1 text-[11px]" style={{bottom:layers.oi ? 180 : 28}}>
        <div className="flex flex-wrap items-center gap-2"><b>{BIAS_NAME}: {biasStatus?.verdict === "MIXED" ? `${biasStatus.lean} · WAIT` : biasStatus?.verdict || "waiting for data"}</b>
          <label>Agreement <select aria-label="Bias agreement" className="rounded bg-slate-800 px-1" value={biasPct} onChange={(e)=>setBiasPct(Number(e.target.value))}>{BIAS_POLICY.agreement_choices.map((v)=><option key={v} value={v}>{Math.round(v*100)}%</option>)}</select></label>
          <span>{biasStatus ? `${biasStatus.bull} buy · ${biasStatus.bear} sell · ${biasStatus.neutral} neutral · ${biasStatus.missing.length} unavailable` : ""}</span>
        </div>
        {biasStatus && <div className="text-amber-200" title="Counts are correlated evidence, not a probability. No markers are drawn for an unqualified lean.">{biasStatus.blockers.length ? biasStatus.blockers.join(" · ") : `Qualified: need ${biasStatus.required}/${biasStatus.avail}; max ${biasStatus.maxAgainst} opposing`} · Alert floor {sig?.minBias ?? "—"}{sig?.minBias != null && sig.minBias > biasStatus.avail ? ` exceeds ${biasStatus.avail} available` : ""}{!marketOpen ? " · alerts paused: market closed" : !sig?.on ? " · alerts off" : ""}</div>}
        {biasStatus?.context.length ? <div className="text-slate-400" title={biasStatus.context.map((item)=>`${item.name}: ${item.text}`).join("; ")}>Prahaar / GTF / futures OI criteria are in the expanded matrix. Correlated inputs are not independent confidence.</div> : null}
      </div>}
      {/* 📊 Floating RVOL Mode Selector directly in the Sub-Pane */}
      {layers.rvol && rvS.current && (
        <div
          className="pointer-events-auto absolute z-20 flex items-center gap-1 rounded-md border border-ink-700 bg-ink-900/90 px-2 py-0.5 shadow-lg backdrop-blur"
          style={{
            left: 12,
            bottom: (oiS.current ? 125 : 85),
          }}
        >
          <span className="text-[10px] font-bold uppercase tracking-wider text-slate-400">RVOL:</span>
          <button
            onClick={() => setAndSaveRvolPaneMode("slot")}
            className={`rounded px-1.5 py-0.2 text-[10px] font-bold transition-all ${
              rvolPaneMode === "slot"
                ? "bg-amber-500 text-black shadow-sm"
                : "text-slate-400 hover:text-slate-200"
            }`}
            title="Slot RVOL: Bar-by-bar Time-of-Day spikes"
          >
            ⚡ Slot RVOL (Spikes)
          </button>
          <button
            onClick={() => setAndSaveRvolPaneMode("cumul")}
            className={`rounded px-1.5 py-0.2 text-[10px] font-bold transition-all ${
              rvolPaneMode === "cumul"
                ? "bg-emerald-500 text-black shadow-sm"
                : "text-slate-400 hover:text-slate-200"
            }`}
            title="Cumulative RVOL: Running session pace"
          >
            🌊 Cumul cRVOL (Pace)
          </button>
          {analysis?.volume?.combined_future && (
            <span
              className={`ml-1 flex items-center gap-1 rounded px-1.5 py-0.2 font-mono text-[9.5px] border ${
                analysis.volume.combined_future.is_rollover_active
                  ? "bg-amber-950/70 text-amber-300 border-amber-500/60"
                  : "bg-ink-800 text-slate-400 border-ink-700"
              }`}
              title={`Oct FUT: ${lakh(analysis.volume.combined_future.oct_vol_today)} | Nov FUT: ${lakh(analysis.volume.combined_future.nov_vol_today)} (${analysis.volume.combined_future.nov_share_pct}% Rollover) · Combined FUT: ${lakh(analysis.volume.combined_future.combined_vol_today)}`}
            >
              <span>🔄</span>
              <span>{analysis.volume.combined_future.is_rollover_active ? `Roll: Nov ${analysis.volume.combined_future.nov_share_pct}%` : `${analysis.volume.combined_future.oct_contract}`}</span>
            </span>
          )}
          {analysis?.volume?.oc_volume?.both_vol ? (
            <span
              className="ml-0.5 rounded border border-cyan-800/60 bg-cyan-950/60 px-1.5 py-0.2 font-mono text-[9.5px] text-cyan-300"
              title={`Options Chain Volume: This Wk ${lakh(analysis.volume.oc_volume.cur_vol)} (PCR ${analysis.volume.oc_volume.cur_pcr_vol ?? "—"}) | Next Wk ${lakh(analysis.volume.oc_volume.next_vol)} (PCR ${analysis.volume.oc_volume.next_pcr_vol ?? "—"}) · Both: ${lakh(analysis.volume.oc_volume.both_vol)}`}
            >
              📊 OC {wallExp === "cur" ? `This Wk ${lakh(analysis.volume.oc_volume.cur_vol)}` : wallExp === "next" ? `Next Wk ${lakh(analysis.volume.oc_volume.next_vol)}` : `Both ${lakh(analysis.volume.oc_volume.both_vol)}`}
            </span>
          ) : null}
        </div>
      )}
      {/* 🎯 Sleek Bloomberg/TradingView Pro Glassmorphic Sniper Hover Inspector Card */}
      {layers.advPa && sniperHover && (
        <div
          className="pointer-events-none absolute z-50 w-[340px] rounded-xl border border-gold-500/60 bg-[#070b14]/95 p-3 text-[11.5px] text-slate-200 shadow-2xl backdrop-blur-md ring-1 ring-gold-400/20 transition-all duration-75"
          style={{
            left: Math.min(Math.max(12, sniperHover.x + 18), (box.current?.clientWidth ?? 900) - 360),
            top: Math.min(Math.max(54, sniperHover.y - 20), (box.current?.clientHeight ?? 500) - 190),
          }}
        >
          <div className="mb-1.5 flex items-center justify-between gap-2 border-b border-slate-800 pb-1.5">
            <span className="font-extrabold text-gold-300">{sniperHover.item.title}</span>
            <span className="shrink-0 rounded bg-gold-500/20 px-1.5 py-0.5 font-mono text-[10px] font-bold text-gold-300 border border-gold-500/40">
              {sniperHover.item.badge}
            </span>
          </div>
          <div className="space-y-1.5 leading-relaxed">
            <div>
              <span className="font-bold text-cyan-300">What it is: </span>
              <span className="text-slate-300">{sniperHover.item.what}</span>
            </div>
            <div>
              <span className="font-bold text-amber-300">Why it formed: </span>
              <span className="text-slate-300">{sniperHover.item.why}</span>
            </div>
            <div className="rounded bg-emerald-950/50 border border-emerald-500/30 px-2 py-1 text-emerald-200">
              <span className="font-bold text-emerald-300">🎯 How to trade: </span>
              <span>{sniperHover.item.how_to_trade}</span>
            </div>
          </div>
        </div>
      )}

      {/* 🏛️ Sleek Institutional GTF Zone Hover Tooltip Inspector */}
      {isGtfOn && gtfHover && !sniperHover && !hasOcMarker && (
        <div
          className={`pointer-events-none absolute z-50 w-[350px] rounded-xl border p-3 text-[11.5px] text-slate-200 shadow-2xl backdrop-blur-md ring-1 transition-all duration-75 ${
            gtfHover.zone.type === "DEMAND"
              ? "bg-[#06140f]/95 border-emerald-500/60 ring-emerald-400/20"
              : "bg-[#140609]/95 border-rose-500/60 ring-rose-400/20"
          }`}
          style={{
            left: Math.min(Math.max(12, gtfHover.x + 18), (box.current?.clientWidth ?? 900) - 370),
            top: Math.min(Math.max(54, gtfHover.y - 20), (box.current?.clientHeight ?? 500) - 230),
          }}
        >
          {/* Header */}
          <div className="mb-2 flex items-center justify-between border-b border-ink-700/80 pb-1.5">
            <div className="flex items-center gap-1.5">
              <span className={`px-2 py-0.5 rounded text-[11px] font-extrabold border ${
                gtfHover.zone.type === "DEMAND"
                  ? "bg-emerald-950/90 text-emerald-300 border-emerald-500/80"
                  : "bg-rose-950/90 text-rose-300 border-rose-500/80"
              }`}>
                {gtfHover.zone.type === "DEMAND" ? "🟢 GTF DEMAND" : "🔴 GTF SUPPLY"} [{gtfHover.zone.pattern}]
              </span>
              <span className="text-[10px] font-mono px-1.5 py-0.5 rounded bg-ink-800 text-gold-300 border border-gold-500/30 font-bold">
                {gtfHover.zone.grade} ({gtfHover.zone.score.toFixed(1)}/10)
              </span>
            </div>
            <span className={`px-1.5 py-0.5 rounded text-[9.5px] font-mono font-bold uppercase tracking-wider ${
              gtfHover.zone.status === "FRESH"
                ? "bg-emerald-500/20 text-emerald-300 border border-emerald-500/40 animate-pulse"
                : gtfHover.zone.status === "TESTED"
                ? "bg-amber-500/20 text-amber-300 border border-amber-500/40"
                : "bg-slate-700/30 text-slate-400 border border-slate-600/40"
            }`}>
              {gtfHover.zone.status === "TESTED" ? `TESTED ${gtfHover.zone.testCount}x` : gtfHover.zone.status}
            </span>
          </div>

          {/* Price Range & Metrics Grid */}
          <div className="grid grid-cols-2 gap-x-2 gap-y-1 rounded bg-ink-900/60 p-2 font-mono text-[10.5px] border border-ink-800/80 mb-2">
            <div>
              <span className="text-slate-400">Proximal (Entry): </span>
              <b className={gtfHover.zone.type === "DEMAND" ? "text-emerald-300" : "text-rose-300"}>
                {gtfHover.zone.proximal.toFixed(1)}
              </b>
            </div>
            <div>
              <span className="text-slate-400">Distal (SL): </span>
              <b className="text-slate-200">{gtfHover.zone.distal.toFixed(1)}</b>
            </div>
            <div>
              <span className="text-slate-400">Zone Range: </span>
              <b className="text-white">
                {gtfHover.zone.bottom.toFixed(0)} – {gtfHover.zone.top.toFixed(0)}
              </b>
            </div>
            <div>
              <span className="text-slate-400">Width: </span>
              <b className="text-cyan-300">
                {Math.abs(gtfHover.zone.top - gtfHover.zone.bottom).toFixed(1)} pts
              </b>
            </div>
            <div>
              <span className="text-slate-400">Base Formation: </span>
              <b className="text-slate-200">{gtfHover.zone.baseBars} bar{gtfHover.zone.baseBars > 1 ? "s" : ""}</b>
            </div>
            <div>
              <span className="text-slate-400">Marking Mode: </span>
              <b className="text-amber-300 capitalize">{gtfHover.zone.mode}</b>
            </div>
          </div>

          {/* Institutional Copywriting Explanations */}
          <div className="space-y-1.5 leading-relaxed text-[10.5px]">
            <div className={`p-1.5 rounded border ${
              gtfHover.zone.type === "DEMAND"
                ? "bg-emerald-950/40 border-emerald-700/40 text-emerald-200/90"
                : "bg-rose-950/40 border-rose-700/40 text-rose-200/90"
            }`}>
              <div className="font-semibold text-white/90 mb-0.5">Price Action Structure:</div>
              {getGtfExplanation(gtfHover.zone).patternExplanation}
            </div>
            <div className="p-1.5 rounded border bg-ink-900/70 border-ink-700/70 text-slate-300">
              <div className="font-semibold text-slate-200 mb-0.5">Institutional Order Flow:</div>
              {getGtfExplanation(gtfHover.zone).statusExplanation}
            </div>
          </div>
        </div>
      )}

      {/* 🎯 Sleek Glassmorphic PE−CE Institutional Telemetry Card on Marker Hover */}
      {hover && !hasOiCard && hoveredOcAct && hasOcMarker && (
        <div
          className="pointer-events-none absolute z-50 w-[350px] rounded-xl border border-gold-500/60 bg-[#080d1a]/95 p-3 text-[11.5px] text-slate-200 shadow-2xl backdrop-blur-md ring-1 ring-gold-400/20 transition-all duration-75"
          style={{
            left: Math.min(Math.max(12, hover.x + 18), (box.current?.clientWidth ?? 900) - 370),
            top: Math.min(Math.max(54, hover.y - 20), (box.current?.clientHeight ?? 500) - 220),
          }}
        >
          {/* Header */}
          <div className="mb-2 flex items-center justify-between border-b border-ink-700/80 pb-1.5">
            <div className="flex items-center gap-1.5 flex-wrap">
              <span className={`px-1.5 py-0.5 rounded text-[10px] font-extrabold border ${
                hoveredOcAct.smartRow?.stage === 2 || hoveredOcAct.smartRow?.stage === 5
                  ? "bg-rose-950/90 text-rose-300 border-rose-500 animate-pulse"
                  : hoveredOcAct.smartRow?.stage === 1
                  ? "bg-amber-950/90 text-amber-300 border-amber-500"
                  : hoveredOcAct.smartRow?.stage === 3
                  ? "bg-orange-950/90 text-orange-300 border-orange-500"
                  : "bg-ink-850 text-slate-300 border-ink-700"
              }`}>
                STAGE {hoveredOcAct.smartRow?.stage} · {hoveredOcAct.smartRow?.stageBadge}
              </span>
              {hoveredOcAct.smartRow?.isSynergy && (
                <span className="px-1.5 py-0.5 rounded text-[9.5px] font-extrabold bg-gold-500/20 text-gold-300 border border-gold-500/60">
                  ⚡ A+ Synergy
                </span>
              )}
            </div>
            <span className="font-mono text-[11px] font-bold text-slate-400">{hoveredOcAct.smartRow?.ts}</span>
          </div>

          {/* Institutional Telemetry Grid */}
          <div className="grid grid-cols-2 gap-x-2 gap-y-1 font-mono text-[11px] mb-2 bg-ink-900/60 p-2 rounded-lg border border-ink-800">
            <div>
              <span className="text-slate-400">PE−CE Day: </span>
              <b className={(hoveredOcAct.smartRow?.pe_ce_day_cr ?? 0) < 0 ? "text-rose-400" : "text-emerald-400"}>
                {hoveredOcAct.smartRow?.pe_ce_day_cr != null ? `${hoveredOcAct.smartRow.pe_ce_day_cr.toFixed(2)}Cr` : "—"}
              </b>
            </div>
            <div>
              <span className="text-slate-400">PE−CE Bar Δ: </span>
              <b className={(hoveredOcAct.smartRow?.pe_ce_delta_l ?? 0) < 0 ? "text-rose-400" : "text-emerald-400"}>
                {hoveredOcAct.smartRow?.pe_ce_delta_l != null ? `${hoveredOcAct.smartRow.pe_ce_delta_l > 0 ? "+" : ""}${hoveredOcAct.smartRow.pe_ce_delta_l.toFixed(1)}L` : "—"}
              </b>
            </div>
            <div>
              <span className="text-slate-400">Writer Slopes: </span>
              <span className="text-slate-200">
                <span className={(hoveredOcAct.smartRow?.putAngleDeg ?? 0) > 0 ? "text-emerald-400" : "text-rose-400"}>Put {hoveredOcAct.smartRow?.putAngleDeg != null ? `${hoveredOcAct.smartRow.putAngleDeg >= 0 ? "+" : ""}${hoveredOcAct.smartRow.putAngleDeg.toFixed(0)}°` : "—"}</span> · <span className={(hoveredOcAct.smartRow?.callAngleDeg ?? 0) > 0 ? "text-rose-400" : "text-emerald-400"}>Call {hoveredOcAct.smartRow?.callAngleDeg != null ? `${hoveredOcAct.smartRow.callAngleDeg >= 0 ? "+" : ""}${hoveredOcAct.smartRow.callAngleDeg.toFixed(0)}°` : "—"}</span>
              </span>
            </div>
            <div>
              <span className="text-slate-400">Conviction: </span>
              <b className="text-gold-300">{hoveredOcAct.smartRow?.convictionBadge ?? "—"}</b>
            </div>
            <div>
              <span className="text-slate-400">Dynamic SL: </span>
              <b className="text-amber-400">
                {hoveredOcAct.smartRow?.dynamicSl ?? (hoveredOcAct.smartRow ? Math.round(hoveredOcAct.smartRow.swingHigh30m + 15) : "—")}
                {hoveredOcAct.smartRow?.riskPts ? ` (${hoveredOcAct.smartRow.riskPts}pt)` : ""}
              </b>
            </div>
            <div>
              <span className="text-slate-400">ATM Strike: </span>
              <b className="text-cyan-300">
                {hoveredOcAct.smartRow?.atmStrike} {hoveredOcAct.smartRow?.actionTone === "bear" ? "PE" : "CE"}
              </b>
            </div>
          </div>

          {/* Action Verdict */}
          <div className="rounded bg-rose-950/60 border border-rose-500/40 px-2 py-1 mb-1 text-[11px] font-bold text-rose-200">
            {hoveredOcAct.smartRow?.actionText ?? hoveredOcAct.txt}
          </div>

          {/* Tape Read */}
          {hoveredOcAct.smartRow?.unifiedRead && (
            <div className="text-[10.5px] text-slate-300 leading-snug">
              {hoveredOcAct.smartRow.unifiedRead}
            </div>
          )}
        </div>
      )}

      {/* 🧱 Sleek Glassmorphic OI Wall & Writer Flow Cross-Reference Inspector */}
      {hover && !hasOiCard && hoveredWall && (
        <div
          className="pointer-events-none absolute z-50 w-[360px] rounded-xl border border-ink-600 bg-[#070c18]/95 p-3 text-[11.5px] text-slate-200 shadow-2xl backdrop-blur-md ring-1 ring-white/10 transition-all duration-75"
          style={{
            left: Math.min(Math.max(12, hover.x + 18), (box.current?.clientWidth ?? 900) - 380),
            top: Math.min(Math.max(54, hover.y - 20), (box.current?.clientHeight ?? 500) - 220),
          }}
        >
          {/* Header */}
          <div className="mb-2 flex items-center justify-between border-b border-ink-700 pb-1.5">
            <div className="flex items-center gap-1.5">
              <span className={`px-2 py-0.5 rounded text-[10.5px] font-extrabold border ${
                hoveredWall.side === "CE"
                  ? "bg-rose-950/90 text-rose-300 border-rose-500/80"
                  : "bg-emerald-950/90 text-emerald-300 border-emerald-500/80"
              }`}>
                {hoveredWall.side === "CE" ? "🧱 CE RESISTANCE WALL" : "🧱 PE SUPPORT FLOOR"}
              </span>
              <span className="font-mono text-white text-[13px] font-black">
                {hoveredWall.p.toLocaleString("en-IN")}
              </span>
            </div>
            <span className={`font-mono text-[10px] font-extrabold px-1.5 py-0.5 rounded border ${
              hoveredWall.status === "building"
                ? "bg-purple-950/90 text-purple-300 border-purple-500/70"
                : hoveredWall.status === "unwinding"
                ? "bg-amber-950/90 text-amber-300 border-amber-500/70"
                : "bg-ink-850 text-slate-300 border-ink-700"
            }`}>
              {hoveredWall.status === "building"
                ? "CAP STACKING (HARDENING)"
                : hoveredWall.status === "unwinding"
                ? "CRACKING (UNWINDING)"
                : "HOLDING (EQUILIBRIUM)"}
            </span>
          </div>

          {/* Wall Metrics Grid */}
          <div className="grid grid-cols-2 gap-x-2 gap-y-1 font-mono text-[11px] mb-2 bg-ink-900/60 p-2 rounded-lg border border-ink-800">
            <div>
              <span className="text-slate-400">Total OI: </span>
              <b className="text-slate-100">{(hoveredWall.oi / 1e7).toFixed(2)} Cr</b>
            </div>
            <div>
              <span className="text-slate-400">Chain Share: </span>
              <b className="text-cyan-300">{(hoveredWall.share * 100).toFixed(1)}% (Rank #{hoveredWall.rank})</b>
            </div>
            <div>
              <span className="text-slate-400">Day ΔOI: </span>
              <b className={hoveredWall.chg >= 0 ? "text-emerald-400" : "text-rose-400"}>
                {hoveredWall.chg >= 0 ? "+" : ""}{(hoveredWall.chg / 1e5).toFixed(1)}L
              </b>
            </div>
            <div>
              <span className="text-slate-400">Fresh Momentum: </span>
              <b className={hoveredWall.fresh >= 0 ? "text-emerald-400" : "text-rose-400"}>
                {hoveredWall.fresh >= 0 ? "+" : ""}{hoveredWall.fresh.toFixed(1)}%
              </b>
            </div>
          </div>

          {/* Writer Correlation Section */}
          <div className="rounded bg-ink-850/80 border border-ink-700 p-2 text-[11px] space-y-1">
            <div className="flex items-center justify-between font-mono">
              <span className="text-slate-400">Writer Flow Angle:</span>
              <b className={
                hoveredWall.side === "CE"
                  ? ((smartRows[0]?.callAngleDeg ?? 0) >= 25 ? "text-rose-400" : "text-slate-300")
                  : ((smartRows[0]?.putAngleDeg ?? 0) >= 25 ? "text-emerald-400" : "text-slate-300")
              }>
                {hoveredWall.side === "CE"
                  ? `Call +${(smartRows[0]?.callAngleDeg ?? 0).toFixed(0)}° (${(smartRows[0]?.callAngleDeg ?? 0) >= 25 ? "Surging" : "Normal"})`
                  : `Put +${(smartRows[0]?.putAngleDeg ?? 0).toFixed(0)}° (${(smartRows[0]?.putAngleDeg ?? 0) >= 25 ? "Defending" : "Normal"})`}
              </b>
            </div>
            <div className="flex items-center justify-between font-mono">
              <span className="text-slate-400">15m Net Impulse:</span>
              <b className={(smartRows[0]?.impulse15mL ?? 0) < 0 ? "text-rose-400" : "text-emerald-400"}>
                {(smartRows[0]?.impulse15mL ?? 0) > 0 ? "+" : ""}{(smartRows[0]?.impulse15mL ?? 0).toFixed(1)}L
              </b>
            </div>
            <div className="pt-1 text-[10.5px] text-slate-300 leading-snug border-t border-ink-700/60">
              {hoveredWall.side === "CE"
                ? (smartRows[0]?.impulse15mL ?? 0) < -15
                  ? "🛡️ 15m Call impulse confirms heavy overhead cap. Fades and PE pullback entries favored."
                  : "⚠️ Caution: Call writers not aggressive enough; watch for short-covering squeeze above this strike."
                : (smartRows[0]?.impulse15mL ?? 0) > 15
                  ? "🛡️ 15m Put impulse confirms aggressive dip defending. Support bounces and CE scalps favored."
                  : "⚠️ Caution: Put writers not defending firmly; watch for floor breakdown below this strike."}
            </div>
          </div>
        </div>
      )}

      {/* 〽️ Sleek Glassmorphic CVD Divergence & Order Flow Inspector Card */}
      {hover && !hasOiCard && hasCvdCard && !hasOcMarker && !hoveredWall && !sniperHover && !gtfHover && (
        <div
          className="pointer-events-none absolute z-50 w-[360px] rounded-xl border border-ink-600 bg-[#070c18]/95 p-3 text-[11.5px] text-slate-200 shadow-2xl backdrop-blur-md ring-1 ring-white/10 transition-all duration-75"
          style={{
            left: Math.min(Math.max(12, hover.x + 18), (box.current?.clientWidth ?? 900) - 380),
            top: Math.min(Math.max(54, hover.y - 20), (box.current?.clientHeight ?? 500) - 240),
          }}
        >
          {/* Header */}
          <div className="mb-2 flex items-center justify-between border-b border-ink-700 pb-1.5">
            <div className="flex items-center gap-1.5">
              <span className={`px-2 py-0.5 rounded text-[10.5px] font-extrabold border ${
                hoveredCvdDiv?.type === "BEARISH_DISTRIBUTION"
                  ? "bg-rose-950/90 text-rose-300 border-rose-500/80"
                  : hoveredCvdDiv?.type === "BULLISH_ABSORPTION"
                  ? "bg-emerald-950/90 text-emerald-300 border-emerald-500/80"
                  : "bg-cyan-950/90 text-cyan-300 border-cyan-500/80"
              }`}>
                {hoveredCvdDiv?.type === "BEARISH_DISTRIBUTION"
                  ? "🔴 BEARISH CVD DISTRIBUTION"
                  : hoveredCvdDiv?.type === "BULLISH_ABSORPTION"
                  ? "🟢 BULLISH CVD ABSORPTION"
                  : "〽️ CVD ORDER FLOW TELEMETRY"}
              </span>
              <span className="font-mono text-white text-[11.5px] font-bold">
                {hm(hover.t)}
              </span>
            </div>
            <span className="font-mono text-[10px] text-slate-400">
              {tf.toUpperCase()}
            </span>
          </div>

          {/* Metric Telemetry Grid */}
          <div className="grid grid-cols-2 gap-2 mb-2 font-mono text-[11px]">
            <div className="rounded bg-ink-850/80 border border-ink-700/80 p-1.5">
              <div className="text-slate-400 text-[9.5px]">Cumulative Delta (CVD)</div>
              <div className={`text-[13px] font-bold ${
                (hoveredFpBar?.cvd ?? hoveredCvdDiv?.cvd ?? 0) >= 0 ? "text-emerald-400" : "text-rose-400"
              }`}>
                {kfmt(hoveredFpBar?.cvd ?? hoveredCvdDiv?.cvd ?? 0)}
              </div>
            </div>

            <div className="rounded bg-ink-850/80 border border-ink-700/80 p-1.5">
              <div className="text-slate-400 text-[9.5px]">Candle Delta</div>
              <div className={`text-[13px] font-bold ${
                (hoveredFpBar?.delta ?? hoveredCvdDiv?.delta ?? 0) >= 0 ? "text-emerald-400" : "text-rose-400"
              }`}>
                {(hoveredFpBar?.delta ?? hoveredCvdDiv?.delta ?? 0) >= 0 ? "+" : ""}
                {kfmt(hoveredFpBar?.delta ?? hoveredCvdDiv?.delta ?? 0)}
              </div>
            </div>

            <div className="rounded bg-ink-850/80 border border-ink-700/80 p-1.5">
              <div className="text-slate-400 text-[9.5px]">Traded Volume</div>
              <div className="text-[12px] font-bold text-slate-200">
                {kfmt(hoveredFpBar?.vol ?? 0)} contracts
              </div>
            </div>

            <div className="rounded bg-ink-850/80 border border-ink-700/80 p-1.5">
              <div className="text-slate-400 text-[9.5px]">Swing Pivot Diff</div>
              <div className="text-[12px] font-bold text-amber-300">
                {hoveredCvdDiv ? `${hoveredCvdDiv.priceDiff >= 0 ? "+" : ""}${hoveredCvdDiv.priceDiff.toFixed(1)} pts` : "In Trend"}
              </div>
            </div>
          </div>

          {/* Institutional Bias Banner */}
          <div className="rounded bg-ink-850/90 border border-ink-700 p-2 mb-2 text-[11px] leading-snug">
            <div className="flex items-center justify-between mb-1">
              <span className="text-slate-400 font-mono text-[10px]">Institutional Bias:</span>
              <span className={`font-mono text-[10.5px] font-black ${
                hoveredCvdDiv?.dir === "bear" ? "text-rose-400" : hoveredCvdDiv?.dir === "bull" ? "text-emerald-400" : "text-cyan-300"
              }`}>
                {hoveredCvdDiv?.bias ?? "ORDER FLOW BALANCED"}
              </span>
            </div>
            <div className="text-slate-300 text-[10.5px]">
              {hoveredCvdDiv?.details ?? (
                (hoveredFpBar?.delta ?? 0) >= 0
                  ? "Aggressive market buyers lifting the offer with positive candle delta."
                  : "Aggressive market sellers hitting the bid with negative candle delta."
              )}
            </div>
          </div>

          {/* Triad Confluence Note */}
          <div className="text-[10px] text-slate-400 font-mono flex items-center justify-between border-t border-ink-700/60 pt-1.5">
            <span>Confluence Triad:</span>
            <span className={
              confluenceTrinity?.confluenceScore === 3
                ? "text-emerald-400 font-bold"
                : "text-slate-400"
            }>
              {confluenceTrinity?.confluenceScore === 3 ? "🎯 A+ SNIPER TRIAD LOCKED" : `${confluenceTrinity?.confluenceScore ?? 0}/3 Pillars Aligned`}
            </span>
          </div>
        </div>
      )}

      {/* 📊 Sleek Glassmorphic 3-Pillars Institutional RVOL Inspector Card */}
      {hover && !hasOiCard && isHoveringRvolPane && (hoveredCrvolBar || hoveredVolBar) && !gtfHover && !sniperHover && !hasOcMarker && !hoveredWall && (
        <div
          className="pointer-events-none absolute z-50 w-[380px] rounded-xl border border-indigo-500/60 bg-[#070e1b]/95 p-3 text-[11.5px] text-slate-200 shadow-2xl backdrop-blur-md ring-1 ring-indigo-400/30 transition-all duration-75"
          style={{
            left: Math.min(Math.max(12, hover.x + 18), (box.current?.clientWidth ?? 900) - 400),
            top: Math.max(10, Math.min(hover.y - 280, (box.current?.clientHeight ?? 500) - 320)),
          }}
        >
          {/* Header */}
          <div className="mb-2 flex items-center justify-between border-b border-ink-700/80 pb-1.5">
            <div className="flex items-center gap-1.5">
              <span className="px-2 py-0.5 rounded text-[10.5px] font-extrabold bg-indigo-950/90 text-indigo-300 border border-indigo-500/80">
                📊 3-PILLAR RVOL ENGINE
              </span>
              <span className="font-mono text-white text-[12px] font-bold">
                {hoveredCrvolBar?.slot ?? hoveredVolBar?.slot ?? hm(hover.t)}
              </span>
            </div>
            <div className="flex flex-col items-end">
              <span className="font-mono text-[10px] text-slate-300 font-bold">
                {analysis?.volume?.combined_future?.is_rollover_active
                  ? `${analysis.volume.combined_future.oct_contract} + ${analysis.volume.combined_future.nov_contract} (${analysis.volume.combined_future.nov_share_pct}% Roll)`
                  : (analysis?.volume?.contract?.split(" ")[0] ?? "NIFTY FUT")} · {tf.toUpperCase()}
              </span>
              {analysis?.volume?.oc_volume?.both_vol ? (
                <span className="font-mono text-[9px] text-cyan-300">
                  OC: {lakh(wallExp === "cur" ? analysis.volume.oc_volume.cur_vol : wallExp === "next" ? analysis.volume.oc_volume.next_vol : analysis.volume.oc_volume.both_vol)} ({wallExp === "cur" ? "This Wk" : wallExp === "next" ? "Next Wk" : "Both"})
                </span>
              ) : null}
            </div>
          </div>

          {/* Multi-Expiry Rollover & Options Participation Sub-strip */}
          {(analysis?.volume?.combined_future?.is_rollover_active || analysis?.volume?.oc_volume) && (
            <div className="mb-2 rounded bg-ink-900/90 border border-ink-800 p-1.5 text-[10px] space-y-0.5">
              {analysis.volume.combined_future?.is_rollover_active && (
                <div className="flex items-center justify-between text-amber-300">
                  <span>🔄 <b>Futures Rollover:</b> {analysis.volume.combined_future.oct_contract} ({lakh(analysis.volume.combined_future.oct_vol_today)}) + {analysis.volume.combined_future.nov_contract} ({lakh(analysis.volume.combined_future.nov_vol_today)})</span>
                  <b className="font-mono text-white">{analysis.volume.combined_future.nov_share_pct}% Rolled</b>
                </div>
              )}
              {analysis.volume.oc_volume?.both_vol ? (
                <div className="flex items-center justify-between text-cyan-300">
                  <span>📊 <b>Option Chain Vol:</b> This Wk {lakh(analysis.volume.oc_volume.cur_vol)} · Next Wk {lakh(analysis.volume.oc_volume.next_vol)}</span>
                  <b className="font-mono text-slate-200">PCR {analysis.volume.oc_volume.both_pcr_vol ?? "—"}</b>
                </div>
              ) : null}
            </div>
          )}

          {/* Pillar 1: Time-of-Day (Slot-Matched) RVOL */}
          {(() => {
            const rawSlotRvol = hoveredVolBar?.slot_rvol ?? hoveredCrvolBar?.slot_rvol ?? hoveredVolBar?.rvol ?? 1.0;
            const targetTime = hoveredCrvolBar?.time ?? hoveredVolBar?.time ?? hover.t;
            const sRvol = unifiedSlotMap.get(targetTime) ?? unifiedSlotMap.get(bucket(targetTime, tf)) ?? rawSlotRvol;
            const sTier =
              sRvol >= 2.5 ? { label: "EARTHQUAKE SPIKE", col: "#f59e0b" } :
              sRvol >= 1.5 ? { label: "ABOVE AVERAGE", col: "#10b981" } :
              sRvol >= 0.8 ? { label: "NORMAL PARTICIPATION", col: "#3b82f6" } :
              { label: "ASLEEP / LULL", col: "#94a3b8" };
            const sVol = hoveredVolBar?.v ?? hoveredCrvolBar?.v ?? 0;
            const sBase = (sRvol > 0 && sVol > 0)
              ? (Math.abs(sRvol - rawSlotRvol) > 0.01
                  ? Math.round(sVol / sRvol)
                  : (hoveredVolBar?.slot_base ?? hoveredCrvolBar?.slot_base ?? Math.round(sVol / sRvol)))
              : null;

            return (
              <div className="mb-2 rounded-lg bg-ink-900/70 border border-ink-800 p-2">
                <div className="flex items-center justify-between text-[10px] uppercase font-bold tracking-wider text-slate-400 mb-1">
                  <span>Pillar 1: Time-of-Day Slot RVOL</span>
                  <span
                    className="px-1.5 py-0.2 rounded font-mono font-extrabold"
                    style={{
                      color: sTier.col,
                      backgroundColor: `${sTier.col}20`,
                      border: `1px solid ${sTier.col}50`,
                    }}
                  >
                    [{sTier.label}]
                  </span>
                </div>
                <div className="grid grid-cols-2 gap-x-2 gap-y-1 font-mono text-[11px]">
                  <div>
                    <span className="text-slate-400">Bar Volume: </span>
                    <b className="text-white">{lakh(sVol)}</b>
                  </div>
                  <div>
                    <span className="text-slate-400">Slot Normal (20d): </span>
                    <b className="text-slate-300">{sBase ? lakh(sBase) : "—"}</b>
                  </div>
                  <div className="col-span-2 flex items-center justify-between pt-1 border-t border-ink-800/80">
                    <span className="text-slate-400">Slot RVOL Multiple:</span>
                    <b className="text-[13px] font-black" style={{ color: sTier.col }}>
                      {sRvol.toFixed(2)}x
                    </b>
                  </div>
                </div>
                <div className="mt-1 text-[9.5px] text-slate-400 italic">
                  *Normalized against historical {hoveredCrvolBar?.slot ?? hoveredVolBar?.slot ?? hm(hover.t)} slot average to eliminate intraday U-shape volume bias.
                </div>
              </div>
            );
          })()}

          {/* Pillar 2: Cumulative Session Progress (cRVOL) */}
          {(() => {
            const c20 = hoveredCrvolBar?.c20;
            const c5 = hoveredCrvolBar?.c5;
            const cumVal = c20 ?? c5 ?? 1.0;
            const hoverTime = hover.t;
            const getDayKey = (t: number) => {
              const d = new Date(t * 1000);
              return `${d.getUTCFullYear()}-${d.getUTCMonth()}-${d.getUTCDate()}`;
            };
            const targetDay = getDayKey(hoverTime);
            const intradayCandles = candles.filter((c) => getDayKey(c.time) === targetDay && c.time <= hoverTime);
            let todayCumVol = intradayCandles.reduce((s, c) => s + (c.volume || c.v || 0), 0);
            if (!todayCumVol) {
              const vbars = analysis?.volume?.bars ?? [];
              const intradayBars = vbars.filter((b) => getDayKey(b.time) === targetDay && b.time <= hoverTime);
              todayCumVol = intradayBars.reduce((s, b) => s + (b.v || 0), 0);
            }
            const cumVol = (hoveredCrvolBar?.cum && hoveredCrvolBar.cum > 0) ? hoveredCrvolBar.cum : (todayCumVol > 0 ? todayCumVol : null);
            const cumBase = (hoveredCrvolBar?.cum_base && hoveredCrvolBar.cum_base > 0)
              ? hoveredCrvolBar.cum_base
              : (cumVal > 0 && cumVol ? Math.round(cumVol / cumVal) : null);

            return (
              <div className="mb-2 rounded-lg bg-ink-900/70 border border-ink-800 p-2 font-mono text-[11px]">
                <div className="flex items-center justify-between text-[10px] uppercase font-bold tracking-wider text-slate-400 mb-1">
                  <span>Pillar 2: Cumulative Progress (cRVOL)</span>
                  <span className={`px-1.5 py-0.2 rounded font-bold ${
                    cumVal >= 1.5
                      ? "bg-emerald-950/80 text-emerald-300 border border-emerald-500/60"
                      : cumVal >= 1.0
                      ? "bg-blue-950/80 text-blue-300 border border-blue-500/60"
                      : "bg-ink-800 text-slate-400 border border-slate-700"
                  }`}>
                    {cumVal >= 1.5
                      ? "🔥 IN PLAY"
                      : cumVal >= 1.0
                      ? "🟢 NORMAL PACE"
                      : "💤 SUB-PAR PACE"}
                  </span>
                </div>
                <div className="grid grid-cols-2 gap-x-2 gap-y-0.5">
                  <div>
                    <span className="text-slate-400">Cum Vol Today: </span>
                    <b className="text-cyan-300">{cumVol ? lakh(cumVol) : "—"}</b>
                  </div>
                  <div>
                    <span className="text-slate-400">20d Avg Pace: </span>
                    <b className="text-slate-300">{cumBase ? lakh(cumBase) : "—"}</b>
                  </div>
                  <div>
                    <span className="text-slate-400">20d cRVOL: </span>
                    <b className="text-white">{c20 != null ? `${c20.toFixed(2)}x` : "—"}</b>
                  </div>
                  <div>
                    <span className="text-slate-400">5d Fast cRVOL: </span>
                    <b className="text-slate-200">{c5 != null ? `${c5.toFixed(2)}x` : "—"}</b>
                  </div>
                </div>
              </div>
            );
          })()}

          {/* Pillar 3: Breakout Authenticity Verdict */}
          {(() => {
            const rawSlotRvol = hoveredVolBar?.slot_rvol ?? hoveredCrvolBar?.slot_rvol ?? hoveredVolBar?.rvol ?? 1.0;
            const targetTime = hoveredCrvolBar?.time ?? hoveredVolBar?.time ?? hover.t;
            const sRvol = unifiedSlotMap.get(targetTime) ?? unifiedSlotMap.get(bucket(targetTime, tf)) ?? rawSlotRvol;
            const isReal = sRvol >= 1.8;
            const isFake = sRvol < 0.85;

            return (
              <div className={`rounded-lg border p-2 mb-2 ${
                isReal
                  ? "bg-emerald-950/60 border-emerald-500/50"
                  : isFake
                  ? "bg-rose-950/60 border-rose-500/50"
                  : "bg-ink-900/70 border-ink-800"
              }`}>
                <div className="flex items-center justify-between mb-1">
                  <span className="text-[10px] uppercase font-bold tracking-wider text-slate-400">Pillar 3: Breakout Verdict</span>
                  <span className={`px-2 py-0.5 rounded text-[10.5px] font-black ${
                    isReal
                      ? "bg-emerald-500 text-black animate-pulse"
                      : isFake
                      ? "bg-rose-500 text-white"
                      : "bg-ink-800 text-slate-300"
                  }`}>
                    {isReal
                      ? "⚡ REAL BREAKOUT FUEL"
                      : isFake
                      ? "⚠️ FAKEOUT TRAP RISK"
                      : "⚖️ NEUTRAL FLOW"}
                  </span>
                </div>
                <div className="text-[10.5px] text-slate-200 leading-snug">
                  {isReal
                    ? "⚡ REAL INSTITUTIONAL EXPANSION (Slot >= 1.8x) — Moves past levels backed by genuine conviction."
                    : isFake
                    ? "⚠️ FAKEOUT TRAP RISK (Slot < 0.85x — Do Not Chase) — Breakouts lack volume sponsorship; high sweep risk."
                    : "⚖️ NEUTRAL FLOW (0.85x–1.8x) — Normal intraday churn; await volume surge before expansion trades."}
                </div>
              </div>
            );
          })()}

          {/* Big Trades Cluster Readout (if present) */}
          {(() => {
            const nearBubbles = bubbles.filter((b) => Math.abs(b.time - hover.t) <= (BAR_SEC[tf] ?? 300) * 2);
            if (!nearBubbles.length) return null;
            const totalContracts = nearBubbles.reduce((s, b) => s + (b.vol || 0), 0);
            return (
              <div className="rounded bg-amber-950/40 border border-amber-500/50 px-2 py-1 text-[10.5px] text-amber-200 flex items-center justify-between">
                <span>🐳 <b>Big Trade Cluster:</b> {nearBubbles.length} block trade(s) nearby</span>
                <b className="text-white font-mono">{lakh(totalContracts)} contracts</b>
              </div>
            );
          })()}
        </div>
      )}

      {hover && hasOiCard && <div role="tooltip" aria-label="Futures OI explanation" className="pointer-events-none absolute z-50 w-[390px] max-w-[92%] rounded-lg border border-violet-500/70 bg-[#070d19]/95 p-3 text-[12px] leading-snug shadow-2xl"
        style={{ left: Math.max(8, Math.min(hover.x + 18, W - 406)), top: Math.max(42, Math.min(hover.y - 260, (box.current?.clientHeight ?? 500) - 340)) }}>
        <div className="flex justify-between border-b border-slate-700 pb-2 font-bold"><span>{ob?.reduction ? "🟪 OI REDUCTION" : ob?.surge ? "🟧 OI EXPANSION" : "FUTURES OI BAR"}</span><span>{hm(hover.t)}</span></div>
        <div className={`my-2 font-bold ${oiGuide.tone === "bull" ? "text-emerald-300" : oiGuide.tone === "bear" ? "text-rose-300" : "text-amber-300"}`}>{oiGuide.label}</div>
        <div className="text-slate-200">{oiGuide.meaning}</div>
        <div className="my-2 rounded bg-slate-800/70 p-2 font-mono">ΔOI {oiNumber(ob?.doi)} · {oiNumber(ob?.doi_pct, "%")}<br />Chart ΔP {oiNumber(ob?.dp)} · relative {oiNumber(ob?.relative, "×")}<br />Contract {ob?.contract || "unverified"} · sample {ob?.quality || "missing"}</div>
        <div className="text-cyan-200"><b>Next confirmation:</b> {oiGuide.next}</div>
        <div className="mt-2 text-slate-300"><b>Holding / exit review:</b> {oiGuide.manage}</div>
        <div className="mt-2 border-t border-slate-700 pt-1 text-[10px] text-slate-400">Closed-bar positioning context. No breakout, trade, position or profitability is confirmed by this card.</div>
      </div>}
      {/* hover card */}
      {hover && hc && !hasOiCard && !sniperHover && !hasOcMarker && !hoveredWall && !hasCvdCard && !gtfHover && !isHoveringRvolPane && (
        <div className="pointer-events-none absolute z-20 w-[420px] rounded-lg border border-ink-700 bg-[#060b16]/95 p-2.5 text-[13px] leading-snug shadow-xl"
          style={{
            left: hover.x > W - 450 ? hover.x - 430 : hover.x + 16,
            top: (() => {
              const H = box.current?.clientHeight ?? 500;
              const estH = (layers.rvol || vb || hcCrvolBar) ? 460 : 320;
              const bottomSubPaneH = (rvS.current ? 120 : 0) + (oiS.current ? 40 : 0);
              const maxAllowedTop = Math.max(8, H - estH - bottomSubPaneH - 12);
              if (hover.y > H * 0.45) {
                const aboveCursor = hover.y - estH - 12;
                return Math.max(8, Math.min(aboveCursor > 0 ? aboveCursor : maxAllowedTop, maxAllowedTop));
              }
              return Math.max(8, Math.min(hover.y - 10, maxAllowedTop));
            })(),
          }}>
          {lns.length > 0 && <div className="mb-1 rounded bg-violet-900/40 px-1.5 py-1">
            {lns.map((l, i) => <div key={i}><b className="text-violet-200">〰 {l.name}</b> <span className="num text-slate-200">{l.name === "ATM IV" ? l.value.toFixed(1) : l.value.toLocaleString("en-IN", { maximumFractionDigits: 1 })}</span>
              {l.mean && <div className="text-slate-300">{l.mean}</div>}</div>)}
          </div>}
          <div className="mb-1 flex items-baseline justify-between">
            <b className="text-slate-200">{hm(hc.time)}</b>
            <span className={`num ${hc.close >= hc.open ? "text-bull-400" : "text-bear-400"}`}>
              {(hc.close - hc.open >= 0 ? "+" : "")}{(hc.close - hc.open).toFixed(1)} pts
            </span>
          </div>
          <div className="num grid grid-cols-4 gap-x-1 text-slate-400">
            <span>O <b className="text-slate-200">{hc.open.toFixed(1)}</b></span><span>H <b className="text-slate-200">{hc.high.toFixed(1)}</b></span>
            <span>L <b className="text-slate-200">{hc.low.toFixed(1)}</b></span><span>C <b className="text-slate-200">{hc.close.toFixed(1)}</b></span>
          </div>
          {layers.dol && (() => {
            const d = [...dolRef.current].reverse().find((x) => !x.hit) ?? [...dolRef.current].reverse()[0];
            if (!d) return null;
            const arrow = d.dir === "bull" ? "↑" : "↓";
            const tgt = d.kind === "IRL" ? `FVG ${d.lo.toFixed(0)}–${d.hi.toFixed(0)}` : `${d.dir === "bull" ? "old high" : "old low"} ${d.hi.toFixed(0)}`;
            const swept = d.trig != null ? `swept ${d.trig.toFixed(0)} (${d.trigWhat ?? "liquidity"})` : d.why;
            return <div className="mt-1.5 rounded-md border border-violet-500/40 bg-violet-950/30 p-1.5">
              <div className="text-[13px] font-bold text-violet-200">🪝 D.O.L story {arrow}</div>
              <div className="text-[12.5px] text-slate-100">{swept} → drew to <b className="text-violet-200">{tgt}</b>{d.hit ? " → ✓ reached" : " · target still open"}</div>
              <div className="text-[12px] text-amber-200/80">{d.hit ? "Draw complete — book profit / trail SL (filled gaps often react here)." : "Active draw — price is being pulled toward this zone; it's the next magnet."}</div>
            </div>;
          })()}
          {bookNote && <div className={`mt-1.5 rounded px-1.5 py-1 text-[12.5px] ${bookNote.ok ? "bg-emerald-950/40 text-emerald-300 ring-1 ring-emerald-500/30" : "bg-amber-950/40 text-amber-300 ring-1 ring-amber-500/30"}`}>
            <b>📖 Book {bookNote.ok ? "confirms" : "warns"}:</b> {bookNote.txt}</div>}
          {gconf && <div className={`mt-1.5 rounded-md border p-1.5 ${gconf.ok ? "border-violet-400 bg-violet-950/40" : "border-slate-600 bg-ink-850/60"}`}>
            <div className={`text-[13.5px] font-bold ${gconf.ok ? "text-violet-200" : "text-slate-300"}`}>γ {gconf.head}</div>
            {gconf.checks.map((x, i) => <div key={i} className={`text-[13px] ${x.pass ? "text-bull-400" : "text-bear-400"}`}>{x.pass ? "✓" : "✗"} <b>{x.name}</b> — {x.why}</div>)}
            {gconf.plan && <div className="text-[13px] font-semibold text-cyan-200">🎯 {gconf.plan} — paper first.</div>}
            <div className="text-[12px] text-amber-200/80">✓✓✓ badges are recorded live only — this hover re-checks with the CURRENT chain, so on past candles treat it as study, not as a signal you had.</div>
            {gexMap && <div className="text-[12px] text-slate-400">Regime: {gexMap.regime === "positive" ? "+γ (pinning — price drawn to the big levels, fades work)" : "−γ (dealers chase — moves accelerate through levels)"}{gexMap.flip ? ` · flip ${gexMap.flip}` : ""}</div>}
          </div>}
          {nread.length > 0 && <div className="mt-1.5 rounded-md border border-yellow-400/40 bg-yellow-950/20 p-1.5">
            <div className="text-[13.5px] font-semibold text-yellow-300">🟨 Stacked volume nodes (Day · Week · Month)</div>
            {nread.map((l, i) => <div key={i} className={`text-[13px] ${l.startsWith("●") ? "font-semibold text-bull-400" : "text-slate-100"}`}>• {l}</div>)}
          </div>}
          {ofr && <div className="mt-1.5 rounded-md border border-cyan-500/40 bg-cyan-950/30 p-1.5">
            {ofr.wall && <div className="mb-1 rounded bg-ink-800 px-1.5 py-1 text-[13px] text-slate-100">{ofr.wall}</div>}
            <div className="flex items-center gap-2">
              <span className={`rounded px-2 py-0.5 text-[14px] font-extrabold ${VC[ofr.verdict]}`}>{ofr.verdict}</span>
              <span className="text-[12px] text-slate-300">🧭 Order flow · <b className="text-bull-400">{ofr.bull}▲</b> <b className="text-bear-400">{ofr.bear}▼</b></span>
            </div>
            <div className="mt-0.5 text-[13.5px] font-semibold text-slate-100">{ofr.head}</div>
            {ofr.lines.map((l, i) => <div key={i} className={`text-[13px] ${TC[l.tone]}`}>• {l.txt}</div>)}
            {ofr.plan && <div className="mt-0.5 text-[13px] font-semibold text-cyan-200">🎯 {ofr.plan} — paper first, 1 lot max.</div>}
          </div>}
          {(layers.rvol || vb || hcCrvolBar) && (
            <div className="mt-2 rounded-lg border border-indigo-500/50 bg-[#070e1b]/95 p-2.5 font-mono text-[11px] leading-snug shadow-md">
              <div className="mb-1.5 flex items-center justify-between border-b border-ink-700/80 pb-1">
                <span className="flex items-center gap-1 font-bold text-indigo-300 text-[11px]">
                  <span>📊 3-PILLAR INSTITUTIONAL RVOL</span>
                </span>
                <div className="flex flex-col items-end">
                  <span className="font-mono text-[10px] text-slate-300 font-bold">
                    {analysis?.volume?.combined_future?.is_rollover_active
                      ? `${analysis.volume.combined_future.oct_contract} + ${analysis.volume.combined_future.nov_contract} (${analysis.volume.combined_future.nov_share_pct}% Roll)`
                      : (analysis?.volume?.contract?.split(" ")[0] ?? "NIFTY FUT")} · {tf.toUpperCase()}
                  </span>
                  {analysis?.volume?.oc_volume?.both_vol ? (
                    <span className="font-mono text-[9px] text-cyan-300">
                      OC: {lakh(wallExp === "cur" ? analysis.volume.oc_volume.cur_vol : wallExp === "next" ? analysis.volume.oc_volume.next_vol : analysis.volume.oc_volume.both_vol)} ({wallExp === "cur" ? "This Wk" : wallExp === "next" ? "Next Wk" : "Both"})
                    </span>
                  ) : null}
                </div>
              </div>

              {/* Multi-Expiry Rollover & Options Participation Sub-strip */}
              {(analysis?.volume?.combined_future?.is_rollover_active || analysis?.volume?.oc_volume) && (
                <div className="mb-2 rounded bg-ink-900/90 border border-ink-800 p-1.5 text-[10px] space-y-0.5">
                  {analysis.volume.combined_future?.is_rollover_active && (
                    <div className="flex items-center justify-between text-amber-300">
                      <span>🔄 <b>Futures Rollover:</b> {analysis.volume.combined_future.oct_contract} ({lakh(analysis.volume.combined_future.oct_vol_today)}) + {analysis.volume.combined_future.nov_contract} ({lakh(analysis.volume.combined_future.nov_vol_today)})</span>
                      <b className="font-mono text-white">{analysis.volume.combined_future.nov_share_pct}% Rolled</b>
                    </div>
                  )}
                  {analysis.volume.oc_volume?.both_vol ? (
                    <div className="flex items-center justify-between text-cyan-300">
                      <span>📊 <b>Option Chain Vol:</b> This Wk {lakh(analysis.volume.oc_volume.cur_vol)} · Next Wk {lakh(analysis.volume.oc_volume.next_vol)}</span>
                      <b className="font-mono text-slate-200">PCR {analysis.volume.oc_volume.both_pcr_vol ?? "—"}</b>
                    </div>
                  ) : null}
                </div>
              )}

              {/* Pillar 1: Clock-Slot RVOL */}
              {(() => {
                const sSlot = vb?.slot ?? hcCrvolBar?.slot ?? hm(hc.time);
                const sVol = vb?.v ?? hcCrvolBar?.v ?? 0;
                const rawSlotRvol = vb?.slot_rvol ?? vb?.rvol ?? hcCrvolBar?.slot_rvol ?? 1.0;
                const sRvol = unifiedSlotMap.get(hc.time) ?? unifiedSlotMap.get(bucket(hc.time, tf)) ?? rawSlotRvol;
                const sBase = (sRvol > 0 && sVol > 0)
                  ? (Math.abs(sRvol - rawSlotRvol) > 0.01
                      ? Math.round(sVol / sRvol)
                      : (vb?.slot_base ?? hcCrvolBar?.slot_base ?? Math.round(sVol / sRvol)))
                  : null;
                const sTier =
                  sRvol >= 2.5 ? { label: "EARTHQUAKE SPIKE", col: "#f59e0b" } :
                  sRvol >= 1.5 ? { label: "ABOVE AVERAGE", col: "#10b981" } :
                  sRvol >= 0.8 ? { label: "NORMAL PARTICIPATION", col: "#3b82f6" } :
                  { label: "ASLEEP / LULL", col: "#94a3b8" };

                return (
                  <div className="mb-1.5 rounded bg-ink-900/80 p-1.5 border border-ink-800">
                    <div className="flex items-center justify-between text-[10px] font-bold text-slate-400 mb-0.5">
                      <span>PILLAR 1 · CLOCK-SLOT RVOL ({sSlot})</span>
                      <span className="font-extrabold" style={{ color: sTier.col }}>
                        [{sTier.label}]
                      </span>
                    </div>
                    <div className="text-[11px] text-slate-200">
                      Slot ({sSlot}): Today <b className="text-white">{lakh(sVol)}</b> vs 20d Slot Avg <b className="text-slate-300">{sBase ? lakh(sBase) : "—"}</b> → <b className="text-[12px]" style={{ color: sTier.col }}>{sRvol.toFixed(2)}x</b>
                    </div>
                  </div>
                );
              })()}

              {/* Pillar 2: Cumulative Session Pace */}
              {(() => {
                const sSlot = vb?.slot ?? hcCrvolBar?.slot ?? hm(hc.time);
                const c20 = hcCrvolBar?.c20;
                const c5 = hcCrvolBar?.c5;
                const cumVal = c20 ?? c5 ?? 1.0;
                const hoverTime = hc.time;
                const getDayKey = (t: number) => {
                  const d = new Date(t * 1000);
                  return `${d.getUTCFullYear()}-${d.getUTCMonth()}-${d.getUTCDate()}`;
                };
                const targetDay = getDayKey(hoverTime);
                const intradayCandles = candles.filter((c) => getDayKey(c.time) === targetDay && c.time <= hoverTime);
                let todayCumVol = intradayCandles.reduce((s, c) => s + (c.volume || c.v || 0), 0);
                if (!todayCumVol) {
                  const vbars = analysis?.volume?.bars ?? [];
                  const intradayBars = vbars.filter((b) => getDayKey(b.time) === targetDay && b.time <= hoverTime);
                  todayCumVol = intradayBars.reduce((s, b) => s + (b.v || 0), 0);
                }
                const cumVol = (hcCrvolBar?.cum && hcCrvolBar.cum > 0) ? hcCrvolBar.cum : (todayCumVol > 0 ? todayCumVol : null);
                const cumBase = (hcCrvolBar?.cum_base && hcCrvolBar.cum_base > 0)
                  ? hcCrvolBar.cum_base
                  : (cumVal > 0 && cumVol ? Math.round(cumVol / cumVal) : null);
                const cTier =
                  cumVal >= 2.5 ? { label: "EARTHQUAKE SPIKE", col: "#f59e0b" } :
                  cumVal >= 1.5 ? { label: "IN PLAY (EXPANSION)", col: "#10b981" } :
                  cumVal >= 0.8 ? { label: "NORMAL PARTICIPATION", col: "#3b82f6" } :
                  { label: "ASLEEP / LULL", col: "#94a3b8" };

                return (
                  <div className="mb-1.5 rounded bg-ink-900/80 p-1.5 border border-ink-800">
                    <div className="flex items-center justify-between text-[10px] font-bold text-slate-400 mb-0.5">
                      <span>PILLAR 2 · CUMULATIVE PACE (09:15–{sSlot})</span>
                      <span className="font-extrabold" style={{ color: cTier.col }}>
                        [{cTier.label}]
                      </span>
                    </div>
                    <div className="text-[11px] text-slate-200">
                      Cumul Vol: Today <b className="text-cyan-300">{cumVol ? lakh(cumVol) : "—"}</b> vs 20d Avg <b className="text-slate-300">{cumBase ? lakh(cumBase) : "—"}</b> → <b className="text-[12px]" style={{ color: cTier.col }}>{cumVal.toFixed(2)}x</b>
                      {c5 != null && <span className="text-slate-400 text-[10px]"> (5d: {c5.toFixed(2)}x)</span>}
                    </div>
                  </div>
                );
              })()}

              {/* Pillar 3: Breakout Verdict */}
              {(() => {
                const rawSlotRvol = vb?.slot_rvol ?? vb?.rvol ?? hcCrvolBar?.slot_rvol ?? 1.0;
                const sRvol = unifiedSlotMap.get(hc.time) ?? unifiedSlotMap.get(bucket(hc.time, tf)) ?? rawSlotRvol;
                const isReal = sRvol >= 1.8;
                const isFake = sRvol < 0.85;

                return (
                  <div className={`rounded p-1.5 border ${
                    isReal
                      ? "bg-emerald-950/70 border-emerald-500/60 text-emerald-200"
                      : isFake
                      ? "bg-rose-950/70 border-rose-500/60 text-rose-200"
                      : "bg-ink-900/80 border-ink-800 text-slate-300"
                  }`}>
                    <div className="flex items-center justify-between text-[10px] font-bold mb-0.5">
                      <span className="text-slate-400">PILLAR 3 · BREAKOUT VERDICT</span>
                      <span className={`px-1.5 py-0.2 rounded font-black text-[10.5px] ${
                        isReal ? "bg-emerald-500 text-black animate-pulse" : isFake ? "bg-rose-500 text-white" : "bg-ink-800 text-slate-300"
                      }`}>
                        {isReal ? "⚡ REAL BREAKOUT FUEL" : isFake ? "⚠️ FAKEOUT TRAP RISK" : "⚖️ NEUTRAL FLOW"}
                      </span>
                    </div>
                    <div className="text-[10.5px] leading-snug">
                      {isReal
                        ? "⚡ REAL INSTITUTIONAL EXPANSION (Slot >= 1.8x) — Moves past levels backed by genuine conviction."
                        : isFake
                        ? "⚠️ FAKEOUT TRAP RISK (Slot < 0.85x — Do Not Chase) — Breakouts lack volume sponsorship; high sweep risk."
                        : "⚖️ NEUTRAL FLOW (0.85x–1.8x) — Normal intraday churn; await volume surge before expansion trades."}
                    </div>
                  </div>
                );
              })()}

              {/* Big Trades Cluster Readout */}
              {(() => {
                const nearBubbles = bubbles.filter((b) => Math.abs(b.time - hc.time) <= (BAR_SEC[tf] ?? 300) * 2);
                if (!nearBubbles.length) return null;
                const totalContracts = nearBubbles.reduce((s, b) => s + (b.vol || 0), 0);
                return (
                  <div className="mt-1.5 rounded bg-amber-950/40 border border-amber-500/50 px-2 py-0.5 text-[10px] text-amber-200 flex items-center justify-between">
                    <span>🐳 <b>Big Trade Cluster:</b> {nearBubbles.length} block trade(s) nearby</span>
                    <b className="text-white font-mono">{lakh(totalContracts)} contracts</b>
                  </div>
                );
              })()}
            </div>
          )}
          {(isDeltaDetailOn || isDeltaOn || isCellsOn || isCvdOn) && (() => {
            const fpb = fpBars.find((b) => Math.abs(b.time - hc.time) < (BAR_SEC[tf] ?? 300));
            const cIdx = candles.findIndex((c) => c.time === hc.time);
            const prevC = cIdx > 0 ? candles[cIdx - 1] : null;
            const priceUp = prevC ? hc.close >= prevC.close : hc.close >= hc.open;
            const priceDown = !priceUp;
            const barDelta = fpb?.delta ?? (hc.close >= hc.open ? Math.round((hc.volume || 1000) * 0.4) : -Math.round((hc.volume || 1000) * 0.4));
            const barVol = fpb?.vol ?? hc.volume ?? Math.max(Math.abs(barDelta) * 2, 1000);
            const buyVol = fpb?.buy ?? Math.round(Math.max(0, (barVol + barDelta) / 2));
            const sellVol = fpb?.sell ?? Math.max(0, barVol - buyVol);
            const deltaPct = barVol > 0 ? ((barDelta / barVol) * 100).toFixed(1) : "0.0";
            const sessionCvd = fpb?.cvd ?? 0;
            const prevFpb = cIdx > 0 ? fpBars.find((b) => Math.abs(b.time - prevC!.time) < (BAR_SEC[tf] ?? 300)) : null;
            const cvdRising = prevFpb ? sessionCvd >= prevFpb.cvd : barDelta >= 0;
            const cvdSlopeArrow = cvdRising ? "↗ Rising" : "↘ Falling";

            const rng = Math.max(hc.high - hc.low, 1e-4);
            const lowerWick = Math.min(hc.open, hc.close) - hc.low;
            const upperWick = hc.high - Math.max(hc.open, hc.close);
            const lowerWickPct = (lowerWick / rng) * 100;
            const upperWickPct = (upperWick / rng) * 100;
            const isLowerWickAbs = lowerWickPct >= 35 && barDelta < 0;
            const isUpperWickAbs = upperWickPct >= 35 && barDelta > 0;
            const deltaTail = isLowerWickAbs
              ? "🛡 Lower Wick Absorption (Passive Institutional Buyers Soaked Bid)"
              : isUpperWickAbs
              ? "⚠️ Upper Wick Absorption (Passive Institutional Sellers Capped Ask)"
              : lowerWickPct >= 35
              ? `Lower Wick Rejection (${lowerWickPct.toFixed(0)}%)`
              : upperWickPct >= 35
              ? `Upper Wick Rejection (${upperWickPct.toFixed(0)}%)`
              : "Neutral / Balanced Wicks";

            // 4 Action Guidance cases
            let caseTitle = "⚖️ ORDER FLOW BALANCED";
            let caseBadge = "CHURN / DIGESTION";
            let caseTone = "border-slate-700 bg-ink-900/80 text-slate-300";
            let caseBadgeTone = "bg-ink-800 text-slate-300";
            let caseAction = barDelta >= 0
              ? "Slight buyer delta edge. Wait for aggressive delta surge or key level sweep before executing."
              : "Slight seller delta edge. Consolidation within range; wait for directional breakout.";

            if (priceUp && barDelta > 0 && cvdRising) {
              caseTitle = "🟢 BULLISH CONVICTION CONFIRMED";
              caseBadge = "BUY DIPS / HOLD CE";
              caseTone = "border-emerald-500/60 bg-emerald-950/70 text-emerald-200";
              caseBadgeTone = "bg-emerald-500 text-black";
              caseAction = "Price rising + positive bar delta (+Δ) + session CVD expanding higher. Aggressive buyers in control with institutional backing. Favor Longs/CE; dips into VWAP/support are buyable.";
            } else if ((lowerWickPct >= 35 || !priceUp) && barDelta < 0 && (hc.close - hc.low) / rng >= 0.45) {
              caseTitle = "🛡 BULLISH ABSORPTION TRAP";
              caseBadge = "ABS BUY FLOOR";
              caseTone = "border-teal-500/60 bg-teal-950/70 text-teal-200";
              caseBadgeTone = "bg-teal-500 text-black";
              caseAction = "Sellers slammed the bid (negative delta), yet price defended support with a lower wick and closed in upper 50%. Passive institutional buyers absorbed all selling. High-probability reversal floor!";
            } else if ((upperWickPct >= 35 && barDelta > 0 && (hc.close - hc.low) / rng <= 0.55) || (priceUp && !cvdRising && Math.abs(barDelta) < 0.2 * barVol)) {
              caseTitle = "⚠️ BEARISH ABSORPTION / FALSE UPSIDE";
              caseBadge = "ABS SELL CAP";
              caseTone = "border-amber-500/60 bg-amber-950/70 text-amber-200";
              caseBadgeTone = "bg-amber-500 text-black";
              caseAction = "Buyers chased the ask (positive delta) into resistance, but price rejected with an upper wick and closed weak, or price pushed up while CVD declined. Trapped buyers / distribution. Do not chase breakouts; tighten CE stops.";
            } else if (priceDown && barDelta < 0 && !cvdRising) {
              caseTitle = "🔴 GENUINE SELLING PRESSURE";
              caseBadge = "SELL RALLIES / BOOK CE";
              caseTone = "border-rose-500/60 bg-rose-950/70 text-rose-200";
              caseBadgeTone = "bg-rose-500 text-white";
              caseAction = "Price falling + negative bar delta (−Δ) + session CVD plunging lower. Aggressive sellers initiating displacement. Fade rallies into resistance / favor PE; avoid catching falling knives.";
            }

            return (
              <div className="mt-2 rounded-lg border border-teal-500/50 bg-[#06111e]/95 p-2.5 font-mono text-[11px] leading-snug shadow-xl">
                <div className="mb-1.5 flex items-center justify-between border-b border-teal-500/30 pb-1">
                  <span className="flex items-center gap-1 font-bold text-teal-300 text-[11px]">
                    <span>⚡ ALL DELTA DETAIL & ORDER FLOW GUIDANCE</span>
                  </span>
                  <span className="font-mono text-[10px] text-slate-400">
                    {tf.toUpperCase()} · {hm(hc.time)}
                  </span>
                </div>

                {/* Telemetry Strip 1: Bar Delta & Volume Split */}
                <div className="mb-1.5 rounded bg-ink-900/90 p-1.5 border border-ink-800">
                  <div className="flex items-center justify-between text-[10px] font-bold text-slate-400 mb-0.5">
                    <span>BAR DELTA & AGGRESSION TELEMETRY</span>
                    <span className={barDelta >= 0 ? "text-emerald-300 font-extrabold" : "text-rose-300 font-extrabold"}>
                      {barDelta >= 0 ? "+" : ""}{kfmt(barDelta)} ({deltaPct}%)
                    </span>
                  </div>
                  <div className="text-[10.5px] text-slate-200 flex flex-wrap gap-x-2">
                    <span>Bar Delta: <b className={barDelta >= 0 ? "text-emerald-400" : "text-rose-400"}>{barDelta >= 0 ? "+" : ""}{kfmt(barDelta)}</b></span>
                    <span>·</span>
                    <span>Buy Vol: <b className="text-emerald-300">{kfmt(buyVol)}</b></span>
                    <span>vs</span>
                    <span>Sell Vol: <b className="text-rose-300">{kfmt(sellVol)}</b></span>
                    <span>·</span>
                    <span>Total: <b className="text-white">{kfmt(barVol)}</b></span>
                  </div>
                </div>

                {/* Telemetry Strip 2: Session CVD & Delta Tail */}
                <div className="mb-1.5 rounded bg-ink-900/90 p-1.5 border border-ink-800">
                  <div className="flex items-center justify-between text-[10px] font-bold text-slate-400 mb-0.5">
                    <span>SESSION CVD & WICK TAIL ABSORPTION</span>
                    <span className={cvdRising ? "text-emerald-300 font-bold" : "text-rose-300 font-bold"}>
                      {cvdSlopeArrow}
                    </span>
                  </div>
                  <div className="text-[10.5px] text-slate-200 flex flex-wrap gap-x-2">
                    <span>Session CVD: <b className={sessionCvd >= 0 ? "text-emerald-400" : "text-rose-400"}>{sessionCvd >= 0 ? "+" : ""}{kfmt(sessionCvd)}</b> ({cvdSlopeArrow})</span>
                    <span>·</span>
                    <span className={isLowerWickAbs ? "text-teal-300 font-bold" : isUpperWickAbs ? "text-amber-300 font-bold" : "text-slate-400"}>
                      {deltaTail}
                    </span>
                  </div>
                </div>

                {/* Action Guidance Card */}
                <div className={`rounded p-1.5 border ${caseTone}`}>
                  <div className="flex items-center justify-between text-[10px] font-bold mb-0.5">
                    <span>{caseTitle}</span>
                    <span className={`px-1.5 py-0.2 rounded font-black text-[10px] ${caseBadgeTone}`}>
                      {caseBadge}
                    </span>
                  </div>
                  <div className="text-[10.5px] leading-snug">
                    {caseAction}
                  </div>
                </div>
              </div>
            );
          })()}
          {ob && <div className="mt-1.5 border-t border-ink-700 pt-1.5">
            <div className="flex items-baseline justify-between"><span className="font-semibold text-slate-200">📊 FUT OI bar</span>
              <span className="num text-slate-300">ΔOI <b className={(ob.doi ?? 0) >= 0 ? "text-bull-400" : "text-bear-400"}>{oiNumber(ob.doi)}</b> · OI {(ob.oi / 1e5).toFixed(1)}L</span></div>
            {ob.build && OI_READ[ob.build] ? <div className={OI_READ[ob.build][2]}><b>{OI_READ[ob.build][0]}</b> — {OI_READ[ob.build][1]}</div>
              : <div className="text-slate-400">⚪ Threshold not met, or OI is not comparable. Check sample quality; this does not establish an absence of positioning.</div>}
            {(ob.surge || ob.reduction) && <div className="font-semibold text-amber-300">OI {ob.reduction ? "REDUCTION" : "EXPANSION"} {ob.surge_x ?? ""}× recent average. Unusual positioning change; this marker does not establish direction.</div>}
          </div>}
          {hread.length > 0 && <div className="mt-1.5 border-t border-ink-700 pt-1.5">
            <div className="text-[14px] font-semibold text-orange-300">🔥 Heatmap read ({tf})</div>
            {hread.map((l, i) => <div key={i} className={`text-[14px] ${i === 0 ? "text-slate-300" : "text-slate-100"}`}>{i === 0 ? "" : "• "}{l}</div>)}
          </div>}
          {lread.length > 0 && <div className="mt-1.5 border-t border-ink-700 pt-1.5">
            <div className="font-semibold text-slate-200">📏 Lines right now</div>
            {lread.map((l, i) => <div key={i} className={l.tone === "bull" ? "text-bull-400" : l.tone === "bear" ? "text-bear-400" : l.tone === "warn" ? "text-amber-300" : "text-slate-300"}>• {l.txt}</div>)}
          </div>}
          {br && <div className="mt-1.5 border-t border-ink-700 pt-1.5">
            <div className="mb-0.5 flex items-baseline justify-between">
              <span className="font-semibold text-gold-400">⚖ Bias</span>
              <span className={`font-bold ${br.verdict === "BUY" ? "text-bull-400" : br.verdict === "SELL" ? "text-bear-400" : "text-slate-400"}`}>
                {br.verdict === "MIXED" ? "mixed" : br.verdict} · <span className="text-bull-400">{br.bull}▲</span> <span className="text-bear-400">{br.bear}▼</span> of {br.avail}</span>
            </div>
            {br.warn && <div className="mb-0.5 font-semibold text-amber-300">{br.warn}</div>}
            {FACTORS.map((f) => { const v = br.votes[f]; return (
              <div key={f} className="flex gap-1 leading-tight">
                <span className={`w-3 shrink-0 font-bold ${v === "bull" ? "text-bull-400" : v === "bear" ? "text-bear-400" : "text-slate-600"}`}>{v === "bull" ? "▲" : v === "bear" ? "▼" : "·"}</span>
                <span className={`w-[100px] shrink-0 ${v ? "text-slate-200" : "text-slate-500"}`}>{f}</span>
                <span className="text-slate-400">{br.why[f] || "no vote"}</span>
              </div>); })}
          </div>}
          {fb && fb.rows.length > 0 && <div className="mt-1.5 border-t border-ink-700 pt-1.5">
            <div className="num"><span className="font-semibold text-amber-300">Footprint (est.)</span>{" "}
              <b className={fb.delta >= 0 ? "text-bull-400" : "text-bear-400"}>Δ {kfmt(fb.delta)}</b>
              {fb.delta_pct != null && <span className="text-slate-500"> ({fb.delta_pct > 0 ? "+" : ""}{fb.delta_pct}%)</span>}
              {fb.poc != null && <span className="text-gold-400"> · POC {fb.poc}</span>}</div>
            {fb.tags.map((t, i) => <div key={i} className={t.dir === "bear" ? "text-rose-300" : "text-teal-300"}>
              <b>{FP_WORD[t.k] ?? t.k.toLowerCase().replace("_", " ")}</b> <span className="text-slate-400">{(FP_MEANING[t.k] ?? "").split(" — ")[1] ?? ""}</span></div>)}
            <div className="num mt-0.5 grid grid-cols-[52px_1fr] gap-x-1 text-[10px]">
              {[...fb.rows].sort((r1, r2) => r2.p - r1.p).slice(0, 10).map((r) => [
                <span key={r.p + "p"} className={`text-right ${r.poc ? "text-gold-400 font-bold" : "text-slate-500"}`}>{r.p}</span>,
                <span key={r.p + "v"}><span className={r.si ? "font-bold text-bear-400" : "text-rose-200"}>{(r.sell / 1000).toFixed(1)}K</span>
                  <span className="text-slate-600"> × </span><span className={r.bi ? "font-bold text-bull-400" : "text-emerald-200"}>{(r.buy / 1000).toFixed(1)}K</span></span>])}
            </div>
            <div className="text-slate-400">{fb.read}</div>
            {fb.conf && <div className={`mt-1 rounded px-1.5 py-0.5 font-bold ${fb.conf.dir === "bull" ? "bg-bull-900/70 text-bull-400" : "bg-bear-900/70 text-bear-400"}`}>
              {fb.conf.dir === "bull" ? "BUY ✓3 (CE bias)" : "SELL ✓3 (PE bias)"}: {fb.conf.why.join(" + ")}
              <div className="font-normal text-slate-400">3 agree — still needs your SL. Info only, not an order.</div></div>}
          </div>}
          {layers.ocAct && hoveredOcAct?.smartRow && (
            <div className="mt-1.5 border-t border-ink-700 pt-1.5">
              <div className="flex items-center justify-between text-[11px] font-mono">
                <span className="font-semibold text-slate-300">🏛️ PE−CE: STAGE {hoveredOcAct.smartRow.stage} ({hoveredOcAct.smartRow.stageBadge})</span>
                <span className={hoveredOcAct.smartRow.pe_ce_delta_l < 0 ? "text-rose-400" : "text-emerald-400"}>
                  {hoveredOcAct.smartRow.pe_ce_delta_l > 0 ? "+" : ""}{hoveredOcAct.smartRow.pe_ce_delta_l.toFixed(1)}L
                </span>
              </div>
              <div className="text-[11px] text-slate-400 mt-0.5">{hoveredOcAct.smartRow.actionText}</div>
            </div>
          )}
          {hev.length > 0 && <div className="mt-1.5 space-y-1 border-t border-ink-700 pt-1.5">
            {hev.sort((a2, b2) => b2.pri - a2.pri).slice(0, 6).map((e, i) => (
              <div key={i}>
                <span className="font-semibold" style={{ color: e.color }}>{e.layer} · {e.full}</span>
                {MEANING[e.kind] && <div className="text-slate-400">{MEANING[e.kind]}</div>}
              </div>
            ))}
          </div>}
        </div>
      )}

      {/* ⚡ 3-Chart Institutional PCR & Open Interest Positioning Suite Drawer */}
      {trinityDrawerOpen && (
        <div
          className="pointer-events-auto absolute bottom-0 left-0 right-0 z-40 flex flex-col bg-[#070c17]/95 border-t-2 border-sky-500/60 shadow-2xl backdrop-blur-md transition-all duration-200"
          style={{ height: trinityDrawerHeight }}
        >
          <div className="flex items-center justify-between bg-[#0a1222] px-3 py-1 border-b border-slate-800 text-xs">
            <div className="flex items-center gap-2">
              <div className="flex items-center rounded bg-[#0b1324] p-0.5 border border-slate-700">
                <button
                  onClick={() => setOiDrawerTab("trinity")}
                  className={`rounded px-2.5 py-0.5 text-[11px] font-extrabold transition-all ${
                    oiDrawerTab === "trinity"
                      ? "bg-sky-500/30 text-sky-200 border border-sky-500/60 shadow-sm"
                      : "text-slate-400 hover:text-slate-200"
                  }`}
                >
                  ⚡ 3-Chart Trinity Suite
                </button>
                <button
                  onClick={() => setOiDrawerTab("matrix")}
                  className={`rounded px-2.5 py-0.5 text-[11px] font-extrabold transition-all ${
                    oiDrawerTab === "matrix"
                      ? "bg-gold-500/30 text-gold-200 border border-gold-500/60 shadow-sm"
                      : "text-slate-400 hover:text-slate-200"
                  }`}
                >
                  🎯 Strike Map 2.0 (OI Matrix)
                </button>
              </div>
              <span className="text-[10px] text-slate-400 hidden md:inline">
                {oiDrawerTab === "trinity"
                  ? "Put-Call Ratio • Fresh OI Δ • Cumulative Total OI • Dual-Axis Overlays"
                  : "Strike Buildups • Support/Resistance Walls • Max Pain • Sentiment Speedometer"}
              </span>
            </div>
            <div className="flex items-center gap-2">
              <button
                onClick={() => setTrinityDrawerHeight((h) => (h >= 480 ? 280 : 520))}
                className="text-[10px] px-2 py-0.5 rounded bg-slate-800 text-slate-300 hover:text-white font-semibold"
              >
                {trinityDrawerHeight >= 480 ? "Compact" : "Expand"}
              </button>
              <button
                onClick={() => setTrinityDrawerOpen(false)}
                className="text-slate-400 hover:text-rose-400 font-bold px-1.5"
                title="Close Drawer"
              >
                ✕
              </button>
            </div>
          </div>
          <div className="flex-1 min-h-0 flex flex-col relative w-full h-full overflow-hidden">
            {oiDrawerTab === "trinity" ? (
              <PcrTrinitySuite onClose={() => setTrinityDrawerOpen(false)} />
            ) : (
              <OIMatrixSuite onClose={() => setTrinityDrawerOpen(false)} />
            )}
          </div>
        </div>
      )}
    </div>
  );
}

const LEGEND: [keyof Layers, string, string][] = [
  ["ob", "Order block · Breaker · RJB (violet)", "rgba(192,132,252,0.8)"], ["fvg", "FVG · IFVG (orange sell / teal buy)", "rgba(251,146,60,0.85)"], ["liq", "Liquidity BSL/SSL", "#f59e0b"],
  ["structure", "BOS ▲▼ · MSS (big) · bos (weak, faint)", C.bull], ["range", "OTE / EQ", C.gold], ["session", "PDH · PDL · OR", "#38bdf8"],
  ["delta", "ABS ● · Δ± ■", "#e8c35a"], ["crt", "CRT box · TBS ■ · CRT▶", C.gold], ["auction", "Value · ACC / REJ", "#38bdf8"],
  ["volume", "VWAP · vPOC · vol", "#f5f5f4"], ["oi", "FUT OI build-up bars", "#22c55e"], ["rvol", "cRVOL 3-Pillars: Slot (Time-of-Day) + Cumulative + Breakout Authenticity (Spike ≥2.5x, Above Avg ≥1.5x, Normal, Asleep)", "#10b981"], ["qml", "QML · RBS/SBR · cyan = close line", "#22d3ee"],
  ["fp", "Footprint (est.): Δ under candle · gold = POC · A/D/C/M/S/I", "#facc15"],
  ["dol", "D.O.L: ERL taken → FVG target · FVG tapped → old high/low", "#a78bfa"], ["smt", "SMT ▲▼ · pink line = BANKNIFTY (own scale)", "#f472b6"],
  ["heat", "Heatmap: traded volume per price (blue cold → red hot)", "#f97316"], ["ocAct", "PE−CE 6-Stage Lifecycle: 🎯 PB PE/CE · 🔥 ADD · 💰 BOOK · SL Line · bottom lane", "#fb7185"],
  ["bias", `⚖ ${BIAS_NAME}: BUY/SELL requires shared agreement + opposition limit; see the status for blockers`, "#e8c35a"],
  ["walls", "🧱 OI walls: 🟥 CE = resistance (cap) · 🟩 PE = support (floor) · bar thicker = hardening, thinner/dashed = cracking · left bubbles = each strike's % OI change (+ writers adding / − covering)", "#ef4444"], ["bubbles", "🫧 Big trades ≥2× vol · yellow ring = absorbed", "#a3e635"],
  ["cells", "▦ FP cells: bid | ask · bright = imbalance", "#f472b6"], ["cvd", "〽 CVD order flow: 🟢 absorption / 🔴 distribution divergences · ⚡ momentum tinting", "#10b981"], ["gamma", "γ map: red call res · green put sup · yellow flip · ✓✓✓ = EMA stack + flag at level + volume", "#c084fc"], ["nodes", "🟨 Stacked nodes D·W·M (gold = A+) · ● ENTRY = ±2σ into zone", "#facc15"],
  ["tsoup", "🐢 Turtle soup: false break of PDH/PDL/OR/pool that closes back inside → stop-run reversal (▲ buy / ▼ sell)", "#2dd4bf"],
  ["rblock", "⟂ Rejection blocks: big-wick zone (violet) · lower wick = support, upper wick = resistance · dashed line = 50% entry", "#c084fc"],
  ["p3", "📐 Power of 3 (AMD): ① yellow box = accumulation (opening range) · ② orange line = Judas sweep (fake move) · ③ distribution = opposite the Judas", "#fde047"],
  ["stdv", "📐 STDV: 0/1 sweep anchors · -2..-2.5 Minor Reversal · -4..-4.5 Major Reversal", "#ef4444"],
  ["prahaar", "⚔ Prahaar Engine: 🟢 BUY CE · 🔴 BUY PE · 🎯 A+ Master Sniper", "#10b981"],
  ["gtfZones", "🏛️ GTF Zones 2.0: 🟩 Demand · 🟥 Supply · Conservative/Aggressive/Dynamic · 🎯 Master Confluence", "#10b981"],
];

function dedupe(pts: { time: number; price: number }[]) {
  const out: { time: number; price: number }[] = [];
  for (const p of pts) {
    if (out.length && out[out.length - 1].time >= p.time) continue;     // line series needs strictly increasing time
    out.push(p);
  }
  return out;
}
