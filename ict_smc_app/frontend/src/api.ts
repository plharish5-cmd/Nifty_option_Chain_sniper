// api.ts — typed REST calls + one reconnecting WebSocket shared by the whole app.
import { useEffect, useRef, useState } from "react";
import { decode } from "@msgpack/msgpack";

export type TF = "1m" | "5m" | "15m" | "30m" | "1h" | "4h" | "1D";

export interface Candle { time: number; open: number; high: number; low: number; close: number; closed: boolean; volume?: number; v?: number }
export interface Zone { type: "FVG" | "IFVG" | "RJB" | "OB" | "BREAKER"; dir: "bull" | "bear"; from: number; to: number; top: number; bottom: number; state: string; label: string; formed: string }
export interface PoolLine { side: "BSL" | "SSL"; src: string; price: number; from: number; members: number }
export interface Marker {
  time: number;
  kind: string;
  dir: "bull" | "bear";
  price: number;
  text: string;
  at: string;
  grade?: string;
  context?: string;
  is_counter?: boolean;
  shape?: string;
  color?: string;
  sl?: number;
  tp1?: number;
  confluences?: string[];
  anchor_price?: number;
}
export interface RangeInfo { dir: "bull" | "bear"; event: string; high: number; low: number; eq: number; ote: Record<string, number>; ote_band: [number, number]; pos_pct: number; zone: string; from: number; to: number }
export interface Session { date?: string; open?: number; high?: number; low?: number; prev?: { d: string; h: number; l: number; c: number }; or?: { h: number; l: number }; ib?: { h: number; l: number } }
export interface CrtRange { rvol?: number | null; pair: string; pair_name: string; htf: string; ltf: string; k_ts: number; time: string; win_time: string; chart_from: number; chart_to: number; crth: number; crtl: number; mid: number; body_ratio: number; range_atr: number; aplus: boolean; keylevels: { kind: string; dir: "bull" | "bear"; text: string }[]; biases: ("bull" | "bear")[]; status: string; live: boolean }
export interface CrtView { ranges: CrtRange[]; markers: Marker[]; pairs: { pair: string; name: string; message: string }[] }
export interface TpoProfile { poc: number; vah: number; val: number; high: number; low: number; minutes: number; step: number; hist: [number, number][] }
export interface BalanceBox { start_i: number; end_i: number; hi: number; lo: number; escaped: "up" | "down" | null; from: number; to: number; start: string; end: string; bars: number; active: boolean }
export interface AuctionEvent { kind: "AUCTION_ACCEPT" | "AUCTION_REJECT" | "AUCTION_NO_FOLLOW"; i: number; ts: number; time: number; at: string; day: string; ref: string; level: number; dir: string; text: string; probe?: boolean; progress_atr?: number }
export interface AuctionRead {
  location: { text: string; at_ref: boolean };
  effort: { available: boolean; z: number | null; high: boolean; text: string };
  result: { atr: number; pts: number; text: string };
  response: { kind: string | null; text: string };
  decision: { text: string; bias: string | null; note: string };
}
export interface AuctionView { today: TpoProfile | null; prev: TpoProfile | null; boxes: BalanceBox[]; read: AuctionRead; refs: { name: string; price: number }[]; markers: Marker[] }
export interface AuctionRes extends Partial<AuctionView> { tf: string; available: boolean; message?: string; profile_kind?: string; events?: AuctionEvent[]; pending?: { ref: string; level: number; since: string; side: string }[]; delta_label?: string; last_close?: number; rules?: Record<string, unknown> }
export interface VolPoint { time: number; v: number; sd: number }
export interface VolProfile { kind: string; poc: number; vah: number; val: number; high: number; low: number; minutes: number; volume: number; step: number; hvn: number[]; lvn: number[]; hist?: [number, number][] }
export interface CrvolBar {
  time: number;
  c20: number | null;
  c5: number | null;
  cum?: number;
  cum_base?: number | null;
  slot_rvol?: number | null;
  slot_base?: number | null;
  v?: number;
  slot?: string;
  tier?: "EARTHQUAKE_SPIKE" | "ABOVE_AVERAGE" | "NORMAL_PARTICIPATION" | "ASLEEP_LULL" | "UNKNOWN";
  tier_label?: string;
  tier_color?: string;
  is_real_breakout?: boolean | null;
  breakout_status?: "REAL_BREAKOUT_FUEL" | "FAKEOUT_WARNING" | "NEUTRAL_FLOW";
  breakout_verdict?: string;
}

export interface VolumeView {
  available: boolean; message?: string; contract?: string; basis?: number;
  bars?: {
    time: number;
    v: number;
    rvol: number | null;
    slot_rvol?: number | null;
    slot_base?: number | null;
    cum_rvol?: number | null;
    slot?: string;
    tier?: string;
    tier_label?: string;
    tier_color?: string;
    is_real_breakout?: boolean | null;
    breakout_verdict?: string;
    up: boolean;
    oi?: number | null;
    doi?: number | null;
    build?: string | null;
  }[];
  oi_last?: { oi: number; doi?: number | null; doi_pct?: number | null; dp?: number | null; build?: string } | null;
  oi_summary?: { net_doi: number; dominant: string; bias: string | null; counts: Record<string, number>; bars: number; last_build: string; last_doi: number | null; oi: number } | null;
  iv?: { time: number; iv: number }[]; iv_trend?: { from: number; to: number; chg: number; word: string; minutes: number } | null;
  iv_last?: { ts: number; iv: number } | null;
  oi_health?: { state: string; sample_quality: string; sample_ts?: number | null; age_s?: number | null; contract?: string | null; source?: string; pane_height: number; relative_window: number; freshness_max_age_s: number };
  oi_line?: { time: number; ts: number; oi: number; ema: number; doi: number | null; doi_pct?: number | null; dp?: number | null; build: string | null; surge: boolean; reduction?: boolean; surge_x: number | null; relative?: number | null; relative_base?: number | null; quality?: string; contract?: string | null; source?: string; sample_ts?: number }[];
  oi_surge_last?: { ts: number; surge_x: number | null } | null;
  banknifty?: { available: boolean; message?: string; contract?: string; summary?: { dominant: string; bias: string | null; net_doi: number; bars: number } | null; last_build?: string | null; oi?: number; recent?: { time: number; build: string | null; doi: number | null }[] };
  oi_compare?: { verdict: string; text: string; nifty: string; banknifty: string } | null;
  rvol_state?: { state: "ACTIVE" | "NORMAL" | "ABSENT" | "UNKNOWN"; trend?: string | null; value?: number | null; c20?: number | null; c5?: number | null; base?: string; text: string; at?: string; tier?: string; tier_label?: string; tier_color?: string };
  crvol?: CrvolBar[];
  rvol_events?: { kind: string; time: number; at: string; title: string; value: number }[];
  rvol_days?: number;
  strike_greeks?: { side: string; label: string; delta: number | null; iv: number | null; delta_trend: { from: number; to: number; word: string } | null }[];
  vwap?: VolPoint[]; bands?: number[]; avwaps?: { label: string; anchor: number; dir: string; points: VolPoint[] }[];
  today?: VolProfile | null; prev?: VolProfile | null; naked?: { day: string; price: number }[];
  last?: { last_rvol: number | null; last_v: number | null; method: string | null }; act_last?: { act: number; raw: number; z: number | null } | null;
  last_vwap?: VolPoint | null; rvol_method?: string | null;
  combined_future?: {
    available: boolean;
    oct_contract: string;
    nov_contract: string;
    oct_vol_today: number;
    nov_vol_today: number;
    combined_vol_today: number;
    nov_share_pct: number;
    is_rollover_active: boolean;
  } | null;
  oc_volume?: {
    cur_expiry?: string | null;
    next_expiry?: string | null;
    cur_vol: number;
    cur_ce_vol?: number;
    cur_pe_vol?: number;
    cur_pcr_vol?: number | null;
    next_vol: number;
    next_ce_vol?: number;
    next_pe_vol?: number;
    next_pcr_vol?: number | null;
    both_vol: number;
    both_ce_vol?: number;
    both_pe_vol?: number;
    both_pcr_vol?: number | null;
    next_share_pct: number;
  } | null;
}
export interface QmlSetup { kind: "QML" | "RBS" | "SBR"; dir: "bull" | "bear"; name: string; side: string; level: number; tol: number; status: "ARMED" | "TRIGGERED" | "TP1 HIT" | "TP2 HIT" | "STOPPED" | "INVALID" | "EXPIRED" | "NO-TRADE" | "FAR"; veto?: string; entry: number; sl: number; tp1: number; tp2: number; rr: number; tp1_r: number; from: number; to: number; bos_at: string; trig_at: string | null; rule: string; no_trade: string; strike?: string | null; structure: { label: string; time: number; price: number }[] }
export interface QmlView { setups: QmlSetup[]; markers: Marker[]; summary: string; pivot_n?: number }
export interface CctTrigger {
  id: string;
  dir: "bull" | "bear";
  grade: "A" | "B";
  grade_desc: string;
  context: string;
  is_counter: boolean;
  time: number;
  at: string;
  bar4_open: number;
  sl: number;
  tp1: number;
  risk_pts: number;
  rr: number;
  poi: string;
  outcome: "TP_HIT" | "STOPPED" | "OPEN";
  r_achieved: number;
  mae_pts: number;
  mfe_pts: number;
  instruction: string;
}
export interface CctStats {
  total_setups: number;
  grade_a_count: number;
  grade_b_count: number;
  wins: number;
  losses: number;
  win_rate: number | null;
  avg_r: number;
  avg_mae_pts: number;
}
export interface CctView {
  tf: TF;
  available: boolean;
  curr_price: number;
  day_open: number;
  day_delta: number;
  day_bias: "BULL" | "BEAR" | "NEUTRAL";
  h1_open: number;
  h1_delta: number;
  h1_bias: "BULL" | "BEAR" | "NEUTRAL";
  bar_open: number;
  bar_bias: "BULL" | "BEAR" | "NEUTRAL";
  continuity: string;
  guidance: string;
  in_killzone: boolean;
  killzone_name: string;
  triggers: CctTrigger[];
  markers: Marker[];
  lines: { label: string; price: number; color: string; style: string }[];
  stats: CctStats;
}
export interface MmxmModel {
  id: string;
  model: "MMBM" | "MMSM";
  name: string;
  dir: "bull" | "bear";
  phase: "ORIG_CONS" | "ENGINEERING" | "SMR_CONFIRMED" | "LOW_RISK_BUY" | "LOW_RISK_SELL" | "REACCUMULATION" | "REDISTRIBUTION" | "TARGET_REACHED";
  orig_cons: { from_i: number; to_i: number; top: number; bottom: number; eq: number; start_time: number; end_time: number };
  smr: { time: number; at: string; price: number; confluences: string[]; confluence_score: number };
  mss: { time: number; level: number; displacement: boolean };
  lre_zone: [number, number];
  target: number;
  target_eq: number;
  sl: number;
  r_potential: number;
  summary: string;
}
export interface MmxmStats {
  total_cycles: number;
  completed: number;
  completion_rate: number;
  avg_r_potential: number;
}
export interface MmxmView {
  tf: TF;
  available: boolean;
  models: MmxmModel[];
  primary: MmxmModel | null;
  markers: Marker[];
  lines: { label: string; price: number; dir: "bull" | "bear"; style: string }[];
  stats: MmxmStats;
  message: string;
}
export interface PaAdvancedView {
  tf: string;
  available: boolean;
  message?: string;
  summary: string;
  msb_quality: {
    i: number;
    ts: number;
    time_str: string;
    dir: "bull" | "bear";
    level: number;
    origin_swing_price: number;
    extreme_swing_price: number;
    strong: boolean;
    weak_type?: string | null;
    score: number;
    scorecard: Record<string, boolean>;
    close_price: number;
  }[];
  dragon_fruit: {
    ob_i: number;
    fvg_i: number;
    dir: "bull" | "bear";
    top: number;
    bottom: number;
    risk: "LOW" | "MID" | "HIGH";
    gap_pts: number;
    ts: number;
    time_str: string;
    label: string;
  }[];
  mitigation_blocks: {
    dir: "bull" | "bear";
    i: number;
    ts: number;
    time_str: string;
    top: number;
    bottom: number;
    broken_i: number;
    fail_swing_price: number;
    interim_swing_price: number;
    mitigated: boolean;
    mitigated_i?: number | null;
  }[];
  rote: {
    dir: "bull" | "bear";
    target_type: string;
    target_price: number;
    sweep_price: number;
    eq_050: number;
    entry_low: number;
    entry_high: number;
    tp1_low: number;
    tp1_high: number;
    tp2: number;
    sl: number;
    status: string;
    i: number;
    ts: number;
    time_str: string;
  }[];
  sfp_tests: {
    swing_i: number;
    swing_price: number;
    kind: "H" | "L";
    test_i: number;
    ts: number;
    time_str: string;
    state: string;
    pierce_atr: number;
    bars_hovered: number;
    reversal_confirmed: boolean;
  }[];
  sd_bases: {
    kind: string;
    polarity: "demand" | "supply";
    base_start_i: number;
    base_end_i: number;
    ts: number;
    time_str: string;
    top: number;
    bottom: number;
    bars_count: number;
    height_atr: number;
    has_internal_sfp: boolean;
    score: number;
  }[];
  ftr_zones: {
    dir: "bull" | "bear";
    pause_start_i: number;
    pause_end_i: number;
    ts: number;
    time_str: string;
    top: number;
    bottom: number;
    pause_bars: number;
    ftb_tested: boolean;
    mitigated: boolean;
  }[];
  tt3: {
    side: "bull" | "bear";
    range_high: number;
    range_low: number;
    range_mid: number;
    tap1_price: number;
    tap2_extreme: number;
    hidden_zone_top: number;
    hidden_zone_bot: number;
    status: string;
    sl: number;
    tp1: number;
    tp2: number;
    ts: number;
    time_str: string;
  }[];
  gpx?: {
    htf: string;
    corridor_high: number;
    corridor_low: number;
    corridor_mid: number;
    zone: string;
    veto_short: boolean;
    veto_long: boolean;
    vwap_state: string;
    vwap_val?: number | null;
    wave_bias: string;
    status_text: string;
  } | null;
  traps: {
    zone_kind: string;
    zone_top: number;
    zone_bottom: number;
    magnet_type: string;
    magnet_price: number;
    distance_atr: number;
    veto_entry: boolean;
    warning: string;
  }[];
  bpr_zones?: {
    kind: "BPR" | "SUPER BPR" | string;
    top: number;
    bot: number;
    mid: number;
    bull_fvg_i: number;
    bear_fvg_i: number;
    ts: number;
    hm: string;
    overlaps: number;
    label: string;
  }[];
  sbb_setups?: {
    kind: string;
    dir: "BULL" | "BEAR" | string;
    side: "LONG" | "SHORT" | string;
    top: number;
    bot: number;
    break_level: number;
    ts: number;
    hm: string;
    is_super: boolean;
    sfp_confirmed: boolean;
    fvg_touch: boolean;
    in_aligned_zone: boolean;
    label: string;
  }[];
  trade_management?: {
    entry?: number | null;
    sl?: number | null;
    r_dist?: number | null;
    be_1r?: number | null;
    directive: string;
  } | null;
  zones: {
    type: string;
    dir: "bull" | "bear";
    from: number;
    to: number;
    top: number;
    bottom: number;
    label: string;
    fill: string;
    stroke: string;
    risk?: string;
  }[];
  markers: Marker[];
  strikes?: {
    CE?: { strike?: number; label?: string; ltp?: number; delta?: number; theta?: number; iv?: number; oi?: number; focus?: any } | null;
    PE?: { strike?: number; label?: string; ltp?: number; delta?: number; theta?: number; iv?: number; oi?: number; focus?: any } | null;
  };
  sniper?: SniperView | null;
  bias_20?: Bias20View | null;
}

export interface SniperTrigger {
  name: string;
  weight: number;
  status: "ACTIVE" | "VETOED" | "NEUTRAL";
  dir?: "bull" | "bear" | null;
  val?: number;
  desc: string;
}

export interface Bias20Factor {
  id: number;
  name: string;
  vote?: "bull" | "bear" | null;
  weight?: number;
  desc: string;
}

export interface Bias20View {
  total_factors: number;
  max_score: number;
  bull_score: number;
  bear_score: number;
  net_score: number;
  verdict: "BUY" | "SELL" | "MIXED" | "NEUTRAL";
  confidence_pct: number;
  triggers: {
    IFVG: SniperTrigger;
    "RJB+": SniperTrigger;
    CRT: SniperTrigger;
    DOL: SniperTrigger;
    HTF_Wick: SniperTrigger;
    APlus: SniperTrigger;
    [k: string]: SniperTrigger;
  };
  factors_20?: Bias20Factor[];
}

export interface SniperZone {
  side: "SUPPLY" | "DEMAND" | string;
  bot: number;
  top: number;
  score: number;
  tags: string[];
  ts: number;
  trap?: string | null;
  mid?: number;
  grade?: string;
  label?: string;
}

export interface SniperTradePlan {
  side: "LONG" | "SHORT" | string;
  status?: string;
  strike: string;
  entry: number;
  sl: number;
  be_1r: number;
  tp1: number;
  tp2?: number;
  rr?: number;
}

export interface SniperHoverItem {
  kind: string;
  side: string;
  top: number;
  bot: number;
  price?: number;
  time?: number;
  title: string;
  badge: string;
  what: string;
  why: string;
  how_to_trade: string;
}

export interface SniperView {
  state: string;
  behavior: string;
  action: string;
  headline: string;
  best_supply?: SniperZone | null;
  best_demand?: SniperZone | null;
  trade_plan?: SniperTradePlan | null;
  last_strong_msb?: any;
  last_sfp?: any;
  bias_20?: Bias20View | null;
  hover_items?: SniperHoverItem[];
}

export interface PrahaarSignalView {
  chart_time?: number;
  candle_idx: number;
  time: number;
  time_str: string;
  action: "BUY ATM CALL" | "BUY ATM PUT";
  dir: "bull" | "bear";
  strike: number;
  option_type: "CE" | "PE";
  option_symbol: string;
  entry: number;
  sl: number;
  tp: number;
  rr: number;
  adx: number;
  macd: number;
  macd_signal: number;
  macd_hist: number;
  fast_ema: number;
  slow_ema: number;
  vix: number | null;
  vix_ok: boolean;
  vix_reason: string;
  confluence_rank: string;
  reasons: string[];
}

export interface PrahaarView {
  available: boolean;
  tf: string;
  fast_ema_len: number;
  slow_ema_len: number;
  adx_threshold: number;
  latest_state?: {
    close: number;
    atm_strike: number;
    fast_ema: number;
    slow_ema: number;
    ema_trend: "bullish" | "bearish" | "neutral";
    macd: number;
    macd_signal: number;
    macd_hist: number;
    macd_slope: number;
    macd_momentum: "bullish" | "bearish" | "neutral";
    adx: number;
    adx_trending: boolean;
    plus_di: number;
    minus_di: number;
    vix: number | null;
    vix_ok: boolean;
    vix_reason: string;
    current_verdict: string;
  };
  signals: PrahaarSignalView[];
  markers: Marker[];
  stats: { total_signals: number; bull_signals: number; bear_signals: number };
}

export interface GtfZoneView {
  id: string;
  type: "DEMAND" | "SUPPLY";
  dir: "bull" | "bear";
  from: number;
  to: number;
  from_raw?: number;
  to_raw?: number;
  top: number;
  bottom: number;
  proximal: number;
  distal: number;
  status: "FRESH" | "TESTED" | "MITIGATED" | "BROKEN";
  test_count: number;
  mode: string;
  pattern: "RBR" | "DBR" | "DBD" | "RBD";
  score: number;
  grade: "A+" | "A" | "B";
  base_bars: number;
  explosive_time: number;
  explosive_body_ratio: number;
  label: string;
}

export interface GtfView {
  available: boolean;
  tf: string;
  marking_mode: string;
  current_price: number;
  zones: GtfZoneView[];
  fresh_demands: GtfZoneView[];
  fresh_supplies: GtfZoneView[];
  nearest_demand: GtfZoneView | null;
  nearest_supply: GtfZoneView | null;
  stats: {
    total_detected: number;
    active_count: number;
    fresh_count: number;
    tested_count: number;
    broken_count: number;
  };
}

export interface Analysis { pa_advanced?: PaAdvancedView; cct?: CctView; mmxm?: MmxmView; qml?: QmlView; prahaar?: PrahaarView; gtf?: GtfView; volume?: VolumeView; auction?: AuctionView | null; crt?: CrtView; tf: TF; trend: string; atr: number; last_close: number; zones: Zone[]; pools: PoolLine[]; markers: Marker[]; zigzag: { time: number; price: number; label: string }[]; range: RangeInfo | null; session: Session; experimental?: string; instrument?: Inst; proxy?: { enabled: boolean; available: boolean; label: string; message: string }; strike_flow?: Record<string, { instrument: string; available: boolean; message: string }>; delta?: { adapter: string; available: boolean; mock: boolean; message: string; has_levels: boolean } }
export interface Quote { spot: number | null; prev_close: number | null; chg: number | null; chg_pct: number | null; vix: number | null; vix_chg: number | null; source: string; market_open: boolean; ts: number }
export interface Signal { ctx?: { tp1?: number; rr1?: number; pair?: string; aplus_tbs?: boolean; [k: string]: unknown }; key: string; setup: "A" | "B" | "C" | "D"; name: string; tf: string; side: "long" | "short"; status: "TRADE" | "SKIP" | "INFO"; time: string; entry: number | null; sl: number | null; tp: number | null; risk: number | null; rr: number | null; target_src: string; reasons: string[]; story: string[]; confidence?: number | null; confluence?: string[]; confidence_parts?: { structure: number; options_context: number | null; flow: number; flow_source: string } | null }
export interface AlertRow { id: number; ts: number; time: string; grade: "trade" | "skip" | "info" | "rvol" | "mgmt"; kind: string; side: string; tf: string; title: string; body: string; price: number | null }
export interface Side { ltp: number; chg: number; oi: number; oi_chg: number; oi_chg_pct: number | null; iv: number; vol: number; delta: number; gamma: number; theta: number; vega: number; bid: number; ask: number }
export interface ChainRow { strike: number; ce: Side; pe: Side }
export interface Build { strike: number; side: "CE" | "PE"; oi_chg: number; chg: number; ltp: number; kind: string }
export interface ChainSummary {
  expiry: string;
  ts: number;
  spot: number;
  atm: number;
  totals: { ce_oi: number; pe_oi: number; pcr: number | null; ce_oi_chg: number; pe_oi_chg: number; pcr_chg: number | null };
  max_pain: number;
  call_wall: number | null;
  put_wall: number | null;
  builds: Build[];
  fetched_at?: number;
  spot_at_fetch?: number;
  is_stale?: boolean;
}
export interface Chain extends ChainSummary { rows: ChainRow[]; step: number; status?: string; error?: string }
export interface PaperTrade { slot?: string | null; pair?: string | null; model?: string | null; tp1?: number | null; half_done?: number | null; opt_key?: string | null; opt_entry?: number | null; opt_exit?: number | null; opt_pnl_pts?: number | null; opt_pnl_rs?: number | null; instrument_mode?: string | null; id: number; setup: string; tf: string; side: string; opened: string; closed: string; entry: number; sl: number; tp: number; risk: number; rr: number; status: string; exit: number | null; r: number | null; pnl_pts: number | null; mfe_r: number | null; notes: string[] }
export interface ScoreRow { group: "setup" | "pair" | "model" | "total"; label: string; trades: number; wins: number; losses: number; flat: number; be_after_tp1: number; win_rate: number | null; avg_r: number | null; total_r: number; best_r: number | null; worst_r: number | null; pnl_pts: number; premium_rs: number; enough: boolean }
export interface Scorecard { rows: ScoreRow[]; open: PaperTrade[]; first: string | null; note: string }
export interface PaperStats { premium_trades?: number; premium_pnl_rs?: number; n: number; wins?: number; win_rate?: number; avg_r?: number; total_r?: number; pnl_pts?: number; pnl_rs_proxy?: number; lot_qty: number; note?: string }
export interface Bias { trend: string; zone: string | null; pos_pct: number | null; last_event: string | null }
export interface Inst { type: "INDEX" | "OPTION"; underlying: string; expiry: string | null; strike: number | null; right: "CE" | "PE" | null; security_id: string | null; key: string; label: string }
export interface Pick { instrument: Inst; role: string; ltp: number | null; delta: number | null; gamma: number | null; theta?: number | null; vega?: number | null; iv: number | null; oi: number | null; itm?: number; kind?: string; rr: number | null; fit: number | null; ret_pct?: number; spread?: number | null; prem_tgt?: number; prem_sl?: number; course?: boolean; why: string }
export interface Focus { label: string; available: boolean; kind?: string; lean?: string; line: string; ltp?: number; chg?: number; oi?: number; oi_chg?: number; oi_chg_pct?: number | null; iv?: number; delta?: number; gamma?: number; theta?: number; vega?: number; vol?: number; bid?: number; ask?: number }
export interface Strikes { expiry: string | null; expiries: string[]; auto: boolean; mode: "INDEX" | "OPTION_CHART"; current: { CE: Pick | null; PE: Pick | null }; manual: { CE: boolean; PE: boolean }; candidates: { calls: Pick[]; puts: Pick[] }; reasons: string[]; picks: Record<string, { move: number; target: string; n_strikes: number }>; log: string[]; focus: Focus[]; verdict: { verdict: string; side: string | null; lines: string[] }; switch_rule: string; proxy_on: boolean; proxy_label: string }
export interface DeltaStatus { labels?: Record<string, string>; name: string; available: boolean; mock: boolean; message: string; adapters: string[]; tf: string; available_on_tf: boolean; has_levels: boolean; tf_message: string }
export interface ZoneRec { id: string; type: "LIQ_POOL" | "ORDER_BLOCK" | "FVG"; timeframe: string; side: string; price_low: number; price_high: number; created_at: number; mitigated_at: number | null; invalidated_at: number | null; state: string; source_event_id: string | null; label: string; extra: Record<string, unknown> }
export interface EventRec { id: string; event_type: string; ts: number; time: string; chart_time: number; tf: string; zone_id: string | null; zone?: string | null; direction: "bull" | "bear"; confidence: number | null; explanation: string; price: number; delta?: number | null; z?: number | null; rule?: string; proxy?: boolean; mock?: boolean; zone_record?: ZoneRec }
export interface DeltaBar { time: number; ts: number; delta: number | null; z: number | null; levels: boolean }
export interface DeltaRes { tf: string; available: boolean; mock: boolean; message: string; has_levels: boolean; bars: DeltaBar[]; events: EventRec[]; status: DeltaStatus }
export interface Dashboard { strikes?: Strikes; mode?: "INDEX" | "OPTION_CHART"; proxy?: { enabled: boolean; label: string }; delta?: DeltaStatus; quote: Quote; bias: Record<string, Bias>; narrative: string[]; session: Session; chain: ChainSummary | null; expiries: string[]; signals: Signal[]; alerts: AlertRow[]; paper_open: PaperTrade | null; paper_open_all?: PaperTrade[]; paper_stats: PaperStats; status: Record<string, unknown> }

async function get<T>(path: string): Promise<T> {
  const r = await fetch(path);
  if (!r.ok) throw new Error(`${path}: HTTP ${r.status}`);
  return r.json() as Promise<T>;
}

export interface OptTapeLeg { vol: number; oi: number; oi_chg: number; ltp: number; delta: number | null; class: string }
export interface OptTapeRow { strike: number; zone: string; ce: OptTapeLeg; pe: OptTapeLeg }
export interface OptTapeLadder { expiry: string; spot: number; atm: number; step: number; ladder: OptTapeRow[];
  totals: { ce_vol: number; pe_vol: number; vol_pcr: number | null; ce_oi: number; pe_oi: number; oi_pcr: number | null } }
export interface OptionTape { available: boolean; which?: string; expiries?: string[]; ladders?: OptTapeLadder[];
  option_crvol?: { value: number | null; state: string; trend: string | null; text?: string } | null;
  heatmap?: Record<string, Record<string, { ts: number; v: number }[]>> | null;
  aggression?: (Record<string, OptAggExp> & { _net_flow_total?: number; _note?: string }) | null;
  recording?: { minutes_today: number; first_ts: number | null; note: string }; error?: string }
export interface WriterMetrics { available: boolean; which?: string; pcr_now?: number | null; pcr_prev?: number | null;
  pcr_trend?: "rising" | "falling" | "flat"; vel_min?: number; look_min?: number;
  velocity?: Record<string, { ce: number; pe: number }>; recorded?: number; error?: string }
export interface OptBigEvent { ts: number; strike: number; side: string; vol: number; mult: number; est: string | null }
export interface OptionBigtrades { available: boolean; expiry?: string; mult?: number; events?: OptBigEvent[]; note?: string; error?: string }
export interface OptFpCell { strike: number; ce_vol: number; pe_vol: number; ce_delta: number; pe_delta: number }
export interface OptFpBucket { ts: number; poc: number | null; net_delta: number; cvd: number; vol: number; rvol: number | null; cells: OptFpCell[] }
export interface OptionFootprint { available: boolean; expiry?: string; spot?: number; atm?: number; tf?: string;
  strikes?: number[]; buckets?: OptFpBucket[]; note?: string; error?: string }
export interface OptAggRow { strike: number; ce_buy: number; ce_sell: number; ce_delta: number; pe_buy: number; pe_sell: number; pe_delta: number }
export interface OptAggExp { ladder: OptAggRow[]; cvd: { ts: number; ce: number; pe: number; net: number }[];
  net_flow: number; totals: { ce_delta: number; pe_delta: number } }

export interface InstitutionalLevelsData {
  pdh: number;
  pdl: number;
  pdc: number;
  pdo: number;
  session_open: number;
  midnight_open: number;
}

export interface InstitutionalLevelsRes {
  ok: boolean;
  date: string;
  levels: InstitutionalLevelsData;
  current_price: number;
  high_today: number;
  low_today: number;
  status: string;
  /** "daily" when PDH/PDL came from a real previous session; "fallback" when they are the
   *  placeholder values in rules.json and must not be presented as yesterday's levels. */
  levels_source?: "daily" | "fallback";
  levels_available?: boolean;
  prev_day_date?: string | null;
  status_label: string;
  alert_text: string;
  alert_kind: string;
  action_tone: "bull" | "bear" | "warn" | "flat";
  is_sweep: boolean;
  is_breakout: boolean;
  rvol: number;
  pe_ce_delta_l: number;
  details: string;
}

export const api = {
  institutionalLevels: (secId = "13", rvol?: number, peCeDeltaL?: number) =>
    get<InstitutionalLevelsRes>(
      `/api/desk/institutional-levels?sec_id=${secId}${rvol != null ? `&rvol=${rvol}` : ""}${peCeDeltaL != null ? `&pe_ce_delta_l=${peCeDeltaL}` : ""}`
    ),
  candles: (tf: TF, days = 3, inst = "NIFTY") => get<{ tf: TF; candles: Candle[] }>(`/api/candles?tf=${tf}&days=${days}&inst=${encodeURIComponent(inst)}`),
  analysis: (tf: TF, inst = "NIFTY") => get<Analysis>(`/api/analysis?tf=${tf}&inst=${encodeURIComponent(inst)}`),
  cct: (tf: TF = "5m") => get<CctView>(`/api/cct?tf=${tf}`),
  mmxm: (tf: TF = "5m") => get<MmxmView>(`/api/mmxm?tf=${tf}`),
  prahaar: (tf: TF = "5m") => get<PrahaarView>(`/api/prahaar?tf=${tf}`),
  gtf: (tf: TF = "5m", mode = "conservative") => get<GtfView>(`/api/gtf?tf=${tf}&mode=${mode}`),
  strikes: () => get<Strikes>("/api/strikes"),
  post: async <T = unknown>(path: string, body: unknown): Promise<T> => {
    const r = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    const j = await r.json();
    if (!r.ok) throw new Error(j.detail ?? `HTTP ${r.status}`);
    return j as T;
  },
  dashboard: () => get<Dashboard>("/api/dashboard"),
  alerts: (limit = 500, grade?: string) => get<{ alerts: AlertRow[] }>(`/api/alerts?limit=${limit}${grade ? `&grade=${grade}` : ""}`),
  postAlert: (a: Partial<AlertRow> & { key?: string; payload?: unknown }) => api.post<{ ok: boolean; key: string }>(`/api/alerts`, a),
  chain: (expiry?: string) => get<Chain>(`/api/chain${expiry ? `?expiry=${expiry}` : ""}`),
  optionTape: (which = "both", band = 6, heat = 0, flow = 0) =>
    get<OptionTape>(`/api/option_tape?which=${which}&band=${band}&heat=${heat}&flow=${flow}`),
  optionFootprint: (which = "cur", band = 6, tf = "5m") =>
    get<OptionFootprint>(`/api/option_footprint?which=${which}&band=${band}&tf=${tf}`),
  optionBigtrades: (which = "cur", band = 10, mult = 3) =>
    get<OptionBigtrades>(`/api/option_bigtrades?which=${which}&band=${band}&mult=${mult}`),
  writerMetrics: (which = "cur", look = 30, vel = 15) =>
    get<WriterMetrics>(`/api/writer_metrics?which=${which}&look=${look}&vel=${vel}`),
  paper: () => get<{ trades: PaperTrade[]; stats: PaperStats; open: PaperTrade | null; scorecard?: Scorecard }>("/api/paper"),
  repair: () => fetch("/api/repair", { method: "POST" }).then((r) => r.json()),
  delta: (tf: TF) => get<DeltaRes>(`/api/delta?tf=${tf}`),
  auction: (tf: TF) => get<AuctionRes>(`/api/auction?tf=${tf}`),
  volume: (tf: TF) => get<VolumeView>(`/api/volume?tf=${tf}`),
  events: (tf: TF) => get<{ tf: TF; events: EventRec[] }>(`/api/events?tf=${tf}&limit=300`),
  zones: (tf: TF, active = 1) => get<{ tf: TF; zones: ZoneRec[] }>(`/api/zones?tf=${tf}&active=${active}`),
  setAdapter: async (name: string) => {
    const r = await fetch("/api/delta/adapter", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name }) });
    if (!r.ok) throw new Error((await r.json()).detail ?? `HTTP ${r.status}`);
    return r.json();
  },
  uploadCsv: async (file: File, barSeconds: number, tsIs: string, misaligned: string, inst = "NIFTY") => {
    const r = await fetch(`/api/delta/csv?bar_seconds=${barSeconds}&ts_is=${tsIs}&misaligned=${misaligned}&inst=${encodeURIComponent(inst)}`, { method: "POST", headers: { "Content-Type": "text/csv" }, body: await file.text() });
    const j = await r.json();
    if (!r.ok) throw new Error(j.detail ?? `HTTP ${r.status}`);
    return j;
  },
  replay: (fromSeq: number) => get<{ from_seq: number; messages: LiveMsg[] }>(`/api/replay?from_seq=${fromSeq}`),
};

export type LiveMsg = (
  | { type: "quote"; quote: Quote }
  | { type: "candle"; tf: TF; candle: Candle }
  | { type: "analysis"; tf: TF; analysis: Analysis }
  | { type: "chain"; expiry: string; summary: ChainSummary }
  | { type: "alert"; alert: AlertRow }
  | { type: "paper"; trade: PaperTrade }
  | { type: "strikes"; strikes: Strikes }
) & { seq?: number };

/** One WebSocket with exponential-backoff reconnect and monotonic sequence gap recovery. */
export function useLive(onMsg: (m: LiveMsg) => void): "connecting" | "live" | "down" {
  const [state, setState] = useState<"connecting" | "live" | "down">("connecting");
  const cb = useRef(onMsg);
  cb.current = onMsg;
  const lastSeq = useRef<number | null>(null);

  useEffect(() => {
    let ws: WebSocket | null = null;
    let wait = 1000;
    let stop = false;
    let ping: number | undefined;

    const requestReplay = async (fromSeq: number) => {
      try {
        const res = await fetch(`/api/replay?from_seq=${fromSeq}`);
        if (!res.ok) return;
        const data = await res.json();
        if (Array.isArray(data.messages)) {
          for (const msg of data.messages) {
            const mSeq = (msg as any).seq;
            if (typeof mSeq === "number") {
              if (lastSeq.current === null || mSeq > lastSeq.current) {
                lastSeq.current = mSeq;
                cb.current(msg as LiveMsg);
              }
            } else {
              cb.current(msg as LiveMsg);
            }
          }
        }
      } catch {
        // network hiccup, next retry will attempt
      }
    };

    const handleMessage = (msg: LiveMsg) => {
      const seq = (msg as any).seq;
      if (typeof seq === "number") {
        if (lastSeq.current !== null) {
          if (seq < lastSeq.current - 50) {
            // Server restarted with reset sequence
            lastSeq.current = seq;
          } else if (seq > lastSeq.current + 1 && seq < lastSeq.current + 200) {
            // Small gap detected: request replay for missed frames
            requestReplay(lastSeq.current + 1);
          }
        }
        lastSeq.current = seq;
      }
      cb.current(msg);
    };

    const open = () => {
      const proto = location.protocol === "https:" ? "wss" : "ws";
      ws = new WebSocket(`${proto}://${location.host}/ws?format=msgpack`);
      ws.binaryType = "arraybuffer";
      ws.onopen = () => {
        setState("live");
        wait = 1000;
        ping = window.setInterval(() => ws?.readyState === 1 && ws.send("ping"), 20000);
        // If reconnecting with known sequence, recover missed frames immediately
        if (lastSeq.current !== null) {
          requestReplay(lastSeq.current + 1);
        }
      };
      ws.onmessage = (e) => {
        try {
          if (e.data instanceof ArrayBuffer) {
            const parsed = decode(new Uint8Array(e.data));
            handleMessage(parsed as LiveMsg);
          } else if (typeof e.data === "string") {
            handleMessage(JSON.parse(e.data));
          }
        } catch { /* ignore malformed */ }
      };
      ws.onclose = () => {
        window.clearInterval(ping);
        if (stop) return;
        setState("down");
        window.setTimeout(open, wait);
        wait = Math.min(wait * 2, 15000);
      };
      ws.onerror = () => ws?.close();
    };
    open();

    const onVis = () => {
      if (document.visibilityState === "visible") {
        if (!ws || ws.readyState === WebSocket.CLOSED || ws.readyState === WebSocket.CLOSING) {
          open();
        } else if (ws.readyState === WebSocket.OPEN) {
          ws.send("ping");
          if (lastSeq.current !== null) {
            requestReplay(lastSeq.current + 1);
          }
        }
      }
    };
    document.addEventListener("visibilitychange", onVis);
    window.addEventListener("focus", onVis);

    return () => {
      stop = true;
      window.clearInterval(ping);
      document.removeEventListener("visibilitychange", onVis);
      window.removeEventListener("focus", onVis);
      ws?.close();
    };
  }, []);
  return state;
}

export const fmt = (x: number | null | undefined, d = 1) =>
  x == null || Number.isNaN(x) ? "—" : x.toLocaleString("en-IN", { minimumFractionDigits: d, maximumFractionDigits: d });

export const lakh = (x: number) => {
  const a = Math.abs(x);
  const s = a >= 1e7 ? `${(a / 1e7).toFixed(2)} Cr` : a >= 1e5 ? `${(a / 1e5).toFixed(1)} L` : a >= 1e3 ? `${(a / 1e3).toFixed(1)} K` : `${Math.round(a)}`;
  return (x < 0 ? "−" : "") + s;
};

/** Same 09:15-aligned bucketing as the backend, in chart time (epoch shifted to IST). */
export function bucket(chartTime: number, tf: TF): number {
  const step = { "1m": 60, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "4h": 14400, "1D": 86400 }[tf];
  const day = Math.floor(chartTime / 86400) * 86400;
  if (tf === "1D") return day;
  const open = day + 9 * 3600 + 15 * 60;
  return open + Math.floor((chartTime - open) / step) * step;
}

/** epoch seconds → "HH:MM" IST */
export const istHM = (ts: number | null | undefined) =>
  ts == null ? "—" : new Date((ts + 19800) * 1000).toISOString().slice(11, 16);
