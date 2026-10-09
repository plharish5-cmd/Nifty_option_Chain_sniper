// api.ts — typed REST calls + one reconnecting WebSocket shared by the whole app.
import { useEffect, useRef, useState } from "react";

export type TF = "1m" | "5m" | "15m" | "1h" | "1D";

export interface Candle { time: number; open: number; high: number; low: number; close: number; closed: boolean }
export interface Zone { type: "FVG" | "OB" | "BREAKER"; dir: "bull" | "bear"; from: number; to: number; top: number; bottom: number; state: string; label: string; formed: string }
export interface PoolLine { side: "BSL" | "SSL"; src: string; price: number; from: number; members: number }
export interface Marker { time: number; kind: string; dir: "bull" | "bear"; price: number; text: string; at: string }
export interface RangeInfo { dir: "bull" | "bear"; event: string; high: number; low: number; eq: number; ote: Record<string, number>; ote_band: [number, number]; pos_pct: number; zone: string; from: number; to: number }
export interface Session { date?: string; open?: number; high?: number; low?: number; prev?: { d: string; h: number; l: number; c: number }; or?: { h: number; l: number }; ib?: { h: number; l: number } }
export interface CrtRange { pair: string; pair_name: string; htf: string; ltf: string; k_ts: number; time: string; win_time: string; chart_from: number; chart_to: number; crth: number; crtl: number; mid: number; body_ratio: number; range_atr: number; aplus: boolean; keylevels: { kind: string; dir: "bull" | "bear"; text: string }[]; biases: ("bull" | "bear")[]; status: string; live: boolean }
export interface CrtView { ranges: CrtRange[]; markers: Marker[]; pairs: { pair: string; name: string; message: string }[] }
export interface Analysis { crt?: CrtView; tf: TF; trend: string; atr: number; last_close: number; zones: Zone[]; pools: PoolLine[]; markers: Marker[]; zigzag: { time: number; price: number; label: string }[]; range: RangeInfo | null; session: Session; experimental?: string; instrument?: Inst; proxy?: { enabled: boolean; available: boolean; label: string; message: string }; strike_flow?: Record<string, { instrument: string; available: boolean; message: string }>; delta?: { adapter: string; available: boolean; mock: boolean; message: string; has_levels: boolean } }
export interface Quote { spot: number | null; prev_close: number | null; chg: number | null; chg_pct: number | null; vix: number | null; vix_chg: number | null; source: string; market_open: boolean; ts: number }
export interface Signal { ctx?: { tp1?: number; rr1?: number; pair?: string; aplus_tbs?: boolean; [k: string]: unknown }; key: string; setup: "A" | "B" | "C" | "D"; name: string; tf: string; side: "long" | "short"; status: "TRADE" | "SKIP" | "INFO"; time: string; entry: number | null; sl: number | null; tp: number | null; risk: number | null; rr: number | null; target_src: string; reasons: string[]; story: string[]; confidence?: number | null; confluence?: string[]; confidence_parts?: { structure: number; options_context: number | null; flow: number; flow_source: string } | null }
export interface AlertRow { id: number; ts: number; time: string; grade: "trade" | "skip" | "info"; kind: string; side: string; tf: string; title: string; body: string; price: number | null }
export interface Side { ltp: number; chg: number; oi: number; oi_chg: number; oi_chg_pct: number | null; iv: number; vol: number; delta: number; gamma: number; theta: number; vega: number; bid: number; ask: number }
export interface ChainRow { strike: number; ce: Side; pe: Side }
export interface Build { strike: number; side: "CE" | "PE"; oi_chg: number; chg: number; ltp: number; kind: string }
export interface ChainSummary { expiry: string; ts: number; spot: number; atm: number; totals: { ce_oi: number; pe_oi: number; pcr: number | null; ce_oi_chg: number; pe_oi_chg: number; pcr_chg: number | null }; max_pain: number; call_wall: number | null; put_wall: number | null; builds: Build[] }
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

export const api = {
  candles: (tf: TF, days = 3, inst = "NIFTY") => get<{ tf: TF; candles: Candle[] }>(`/api/candles?tf=${tf}&days=${days}&inst=${encodeURIComponent(inst)}`),
  analysis: (tf: TF, inst = "NIFTY") => get<Analysis>(`/api/analysis?tf=${tf}&inst=${encodeURIComponent(inst)}`),
  strikes: () => get<Strikes>("/api/strikes"),
  post: async <T = unknown>(path: string, body: unknown): Promise<T> => {
    const r = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    const j = await r.json();
    if (!r.ok) throw new Error(j.detail ?? `HTTP ${r.status}`);
    return j as T;
  },
  dashboard: () => get<Dashboard>("/api/dashboard"),
  chain: (expiry?: string) => get<Chain>(`/api/chain${expiry ? `?expiry=${expiry}` : ""}`),
  paper: () => get<{ trades: PaperTrade[]; stats: PaperStats; open: PaperTrade | null; scorecard?: Scorecard }>("/api/paper"),
  repair: () => fetch("/api/repair", { method: "POST" }).then((r) => r.json()),
  delta: (tf: TF) => get<DeltaRes>(`/api/delta?tf=${tf}`),
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
};

export type LiveMsg =
  | { type: "quote"; quote: Quote }
  | { type: "candle"; tf: TF; candle: Candle }
  | { type: "analysis"; tf: TF; analysis: Analysis }
  | { type: "chain"; expiry: string; summary: ChainSummary }
  | { type: "alert"; alert: AlertRow }
  | { type: "paper"; trade: PaperTrade }
  | { type: "strikes"; strikes: Strikes };

/** One WebSocket with exponential-backoff reconnect. Every subscriber gets every message. */
export function useLive(onMsg: (m: LiveMsg) => void): "connecting" | "live" | "down" {
  const [state, setState] = useState<"connecting" | "live" | "down">("connecting");
  const cb = useRef(onMsg);
  cb.current = onMsg;
  useEffect(() => {
    let ws: WebSocket | null = null;
    let wait = 1000;
    let stop = false;
    let ping: number | undefined;
    const open = () => {
      const proto = location.protocol === "https:" ? "wss" : "ws";
      ws = new WebSocket(`${proto}://${location.host}/ws`);
      ws.onopen = () => {
        setState("live");
        wait = 1000;
        ping = window.setInterval(() => ws?.readyState === 1 && ws.send("ping"), 20000);
      };
      ws.onmessage = (e) => {
        try { cb.current(JSON.parse(e.data)); } catch { /* ignore malformed */ }
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
    return () => { stop = true; window.clearInterval(ping); ws?.close(); };
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
  const step = { "1m": 60, "5m": 300, "15m": 900, "1h": 3600, "1D": 86400 }[tf];
  const day = Math.floor(chartTime / 86400) * 86400;
  if (tf === "1D") return day;
  const open = day + 9 * 3600 + 15 * 60;
  return open + Math.floor((chartTime - open) / step) * step;
}

/** epoch seconds → "HH:MM" IST */
export const istHM = (ts: number | null | undefined) =>
  ts == null ? "—" : new Date((ts + 19800) * 1000).toISOString().slice(11, 16);
