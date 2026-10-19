// api.ts — typed REST calls + one reconnecting WebSocket shared by the whole app.
import { useEffect, useRef, useState } from "react";
import { decode } from "@msgpack/msgpack";

export type TF = "1m" | "5m" | "15m" | "30m" | "1h" | "4h" | "1D";

export interface Candle { time: number; open: number; high: number; low: number; close: number; closed: boolean }
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
export interface VolumeView {
  available: boolean; message?: string; contract?: string; basis?: number;
  bars?: { time: number; v: number; rvol: number | null; up: boolean; oi?: number | null; doi?: number | null; build?: string | null }[];
  oi_last?: { oi: number; doi?: number; doi_pct?: number; build?: string } | null;
  oi_summary?: { net_doi: number; dominant: string; bias: string | null; counts: Record<string, number>; bars: number; last_build: string; last_doi: number | null; oi: number } | null;
  iv?: { time: number; iv: number }[]; iv_trend?: { from: number; to: number; chg: number; word: string; minutes: number } | null;
  iv_last?: { ts: number; iv: number } | null;
  oi_line?: { time: number; ts: number; oi: number; ema: number; doi: number | null; build: string | null; surge: boolean; surge_x: number | null }[];
  oi_surge_last?: { ts: number; surge_x: number | null } | null;
  banknifty?: { available: boolean; message?: string; contract?: string; summary?: { dominant: string; bias: string | null; net_doi: number; bars: number } | null; last_build?: string | null; oi?: number; recent?: { time: number; build: string | null; doi: number | null }[] };
  oi_compare?: { verdict: string; text: string; nifty: string; banknifty: string } | null;
  rvol_state?: { state: "ACTIVE" | "NORMAL" | "ABSENT" | "UNKNOWN"; trend?: string | null; value?: number | null; c20?: number | null; c5?: number | null; base?: string; text: string; at?: string };
  crvol?: { time: number; c20: number | null; c5: number | null }[];
  rvol_events?: { kind: string; time: number; at: string; title: string; value: number }[];
  rvol_days?: number;
  strike_greeks?: { side: string; label: string; delta: number | null; iv: number | null; delta_trend: { from: number; to: number; word: string } | null }[];
  vwap?: VolPoint[]; bands?: number[]; avwaps?: { label: string; anchor: number; dir: string; points: VolPoint[] }[];
  today?: VolProfile | null; prev?: VolProfile | null; naked?: { day: string; price: number }[];
  last?: { last_rvol: number | null; last_v: number | null; method: string | null }; act_last?: { act: number; raw: number; z: number | null } | null;
  last_vwap?: VolPoint | null; rvol_method?: string | null;
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
export interface Analysis { cct?: CctView; mmxm?: MmxmView; qml?: QmlView; volume?: VolumeView; auction?: AuctionView | null; crt?: CrtView; tf: TF; trend: string; atr: number; last_close: number; zones: Zone[]; pools: PoolLine[]; markers: Marker[]; zigzag: { time: number; price: number; label: string }[]; range: RangeInfo | null; session: Session; experimental?: string; instrument?: Inst; proxy?: { enabled: boolean; available: boolean; label: string; message: string }; strike_flow?: Record<string, { instrument: string; available: boolean; message: string }>; delta?: { adapter: string; available: boolean; mock: boolean; message: string; has_levels: boolean } }
export interface Quote { spot: number | null; prev_close: number | null; chg: number | null; chg_pct: number | null; vix: number | null; vix_chg: number | null; source: string; market_open: boolean; ts: number }
export interface Signal { ctx?: { tp1?: number; rr1?: number; pair?: string; aplus_tbs?: boolean; [k: string]: unknown }; key: string; setup: "A" | "B" | "C" | "D"; name: string; tf: string; side: "long" | "short"; status: "TRADE" | "SKIP" | "INFO"; time: string; entry: number | null; sl: number | null; tp: number | null; risk: number | null; rr: number | null; target_src: string; reasons: string[]; story: string[]; confidence?: number | null; confluence?: string[]; confidence_parts?: { structure: number; options_context: number | null; flow: number; flow_source: string } | null }
export interface AlertRow { id: number; ts: number; time: string; grade: "trade" | "skip" | "info" | "rvol"; kind: string; side: string; tf: string; title: string; body: string; price: number | null }
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

export const api = {
  candles: (tf: TF, days = 3, inst = "NIFTY") => get<{ tf: TF; candles: Candle[] }>(`/api/candles?tf=${tf}&days=${days}&inst=${encodeURIComponent(inst)}`),
  analysis: (tf: TF, inst = "NIFTY") => get<Analysis>(`/api/analysis?tf=${tf}&inst=${encodeURIComponent(inst)}`),
  cct: (tf: TF = "5m") => get<CctView>(`/api/cct?tf=${tf}`),
  mmxm: (tf: TF = "5m") => get<MmxmView>(`/api/mmxm?tf=${tf}`),
  strikes: () => get<Strikes>("/api/strikes"),
  post: async <T = unknown>(path: string, body: unknown): Promise<T> => {
    const r = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    const j = await r.json();
    if (!r.ok) throw new Error(j.detail ?? `HTTP ${r.status}`);
    return j as T;
  },
  dashboard: () => get<Dashboard>("/api/dashboard"),
  alerts: (limit = 100, grade?: string) => get<{ alerts: AlertRow[] }>(`/api/alerts?limit=${limit}${grade ? `&grade=${grade}` : ""}`),
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
