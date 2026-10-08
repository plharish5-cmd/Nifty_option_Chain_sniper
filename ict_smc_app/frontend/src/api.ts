// api.ts — typed REST calls + one reconnecting WebSocket shared by the whole app.
import { useEffect, useRef, useState } from "react";

export type TF = "1m" | "5m" | "15m" | "1h" | "1D";

export interface Candle { time: number; open: number; high: number; low: number; close: number; closed: boolean }
export interface Zone { type: "FVG" | "OB" | "BREAKER"; dir: "bull" | "bear"; from: number; to: number; top: number; bottom: number; state: string; label: string; formed: string }
export interface PoolLine { side: "BSL" | "SSL"; src: string; price: number; from: number; members: number }
export interface Marker { time: number; kind: string; dir: "bull" | "bear"; price: number; text: string; at: string }
export interface RangeInfo { dir: "bull" | "bear"; event: string; high: number; low: number; eq: number; ote: Record<string, number>; ote_band: [number, number]; pos_pct: number; zone: string; from: number; to: number }
export interface Session { date?: string; open?: number; high?: number; low?: number; prev?: { d: string; h: number; l: number; c: number }; or?: { h: number; l: number }; ib?: { h: number; l: number } }
export interface Analysis { tf: TF; trend: string; atr: number; last_close: number; zones: Zone[]; pools: PoolLine[]; markers: Marker[]; zigzag: { time: number; price: number; label: string }[]; range: RangeInfo | null; session: Session }
export interface Quote { spot: number | null; prev_close: number | null; chg: number | null; chg_pct: number | null; vix: number | null; vix_chg: number | null; source: string; market_open: boolean; ts: number }
export interface Signal { key: string; setup: "A" | "B" | "C"; name: string; tf: string; side: "long" | "short"; status: "TRADE" | "SKIP" | "INFO"; time: string; entry: number | null; sl: number | null; tp: number | null; risk: number | null; rr: number | null; target_src: string; reasons: string[]; story: string[] }
export interface AlertRow { id: number; ts: number; time: string; grade: "trade" | "skip" | "info"; kind: string; side: string; tf: string; title: string; body: string; price: number | null }
export interface Side { ltp: number; chg: number; oi: number; oi_chg: number; oi_chg_pct: number | null; iv: number; vol: number; delta: number; gamma: number; theta: number; vega: number; bid: number; ask: number }
export interface ChainRow { strike: number; ce: Side; pe: Side }
export interface Build { strike: number; side: "CE" | "PE"; oi_chg: number; chg: number; ltp: number; kind: string }
export interface ChainSummary { expiry: string; ts: number; spot: number; atm: number; totals: { ce_oi: number; pe_oi: number; pcr: number | null; ce_oi_chg: number; pe_oi_chg: number; pcr_chg: number | null }; max_pain: number; call_wall: number | null; put_wall: number | null; builds: Build[] }
export interface Chain extends ChainSummary { rows: ChainRow[]; step: number; status?: string; error?: string }
export interface PaperTrade { id: number; setup: string; tf: string; side: string; opened: string; closed: string; entry: number; sl: number; tp: number; risk: number; rr: number; status: string; exit: number | null; r: number | null; pnl_pts: number | null; mfe_r: number | null; notes: string[] }
export interface PaperStats { n: number; wins?: number; win_rate?: number; avg_r?: number; total_r?: number; pnl_pts?: number; pnl_rs_proxy?: number; lot_qty: number; note?: string }
export interface Bias { trend: string; zone: string | null; pos_pct: number | null; last_event: string | null }
export interface Dashboard { quote: Quote; bias: Record<string, Bias>; narrative: string[]; session: Session; chain: ChainSummary | null; expiries: string[]; signals: Signal[]; alerts: AlertRow[]; paper_open: PaperTrade | null; paper_stats: PaperStats; status: Record<string, unknown> }

async function get<T>(path: string): Promise<T> {
  const r = await fetch(path);
  if (!r.ok) throw new Error(`${path}: HTTP ${r.status}`);
  return r.json() as Promise<T>;
}

export const api = {
  candles: (tf: TF, days = 3) => get<{ tf: TF; candles: Candle[] }>(`/api/candles?tf=${tf}&days=${days}`),
  analysis: (tf: TF) => get<Analysis>(`/api/analysis?tf=${tf}`),
  dashboard: () => get<Dashboard>("/api/dashboard"),
  chain: (expiry?: string) => get<Chain>(`/api/chain${expiry ? `?expiry=${expiry}` : ""}`),
  paper: () => get<{ trades: PaperTrade[]; stats: PaperStats; open: PaperTrade | null }>("/api/paper"),
  repair: () => fetch("/api/repair", { method: "POST" }).then((r) => r.json()),
};

export type LiveMsg =
  | { type: "quote"; quote: Quote }
  | { type: "candle"; tf: TF; candle: Candle }
  | { type: "analysis"; tf: TF; analysis: Analysis }
  | { type: "chain"; expiry: string; summary: ChainSummary }
  | { type: "alert"; alert: AlertRow }
  | { type: "paper"; trade: PaperTrade };

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
