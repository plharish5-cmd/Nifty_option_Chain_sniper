// AlertsTable.tsx — Institutional table view of all alerts & signals.
// Displays live signals (★ A+, Bias, Confluence, PE-CE, SMT, IFVG, CRT, DOL, Gamma, Nodes, Order Flow)
// and context alerts (RVOL, Setups). Filterable, searchable, and exportable to CSV.
// 100% null-safe to prevent blank screen renders when database rows have null fields (e.g. side=null for RVOL).

import React, { Component, type ErrorInfo, useEffect, useMemo, useState } from "react";
import { api, type AlertRow } from "../api";
import { BIAS_NAME } from "./bias";

interface AlertsTableProps {
  alerts?: AlertRow[];
  onRefresh?: () => void;
}

interface ExtendedAlertRow extends AlertRow {
  payload?: {
    confluence?: string[];
    story?: string[];
    reasons?: string[];
    sl?: number;
    tp?: number;
    rr?: number;
    risk?: number;
    [k: string]: unknown;
  };
}

class AlertsErrorBoundary extends Component<{ children: React.ReactNode }, { hasError: boolean; error: string }> {
  constructor(props: { children: React.ReactNode }) {
    super(props);
    this.state = { hasError: false, error: "" };
  }
  static getDerivedStateFromError(error: Error) {
    return { hasError: true, error: error?.message || "Unknown error" };
  }
  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error("AlertsTable render error:", error, info);
  }
  render() {
    if (this.state.hasError) {
      return (
        <div className="p-4 text-center text-sm text-slate-400 bg-ink-900 border border-ink-800 rounded m-2">
          <div className="text-bear-400 font-semibold mb-1">Alerts display issue encountered</div>
          <div className="text-xs text-slate-500 mb-3 font-mono">{this.state.error}</div>
          <button
            onClick={() => this.setState({ hasError: false, error: "" })}
            className="pill bg-gold-500/20 text-gold-300 border border-gold-500/40 text-xs px-3 py-1"
          >
            Retry display
          </button>
        </div>
      );
    }
    return this.props.children;
  }
}

const safeStr = (v: unknown): string => (v == null ? "" : String(v));

function AlertsTableInner({ alerts: propAlerts, onRefresh }: AlertsTableProps) {
  const [data, setData] = useState<ExtendedAlertRow[]>(Array.isArray(propAlerts) ? (propAlerts as ExtendedAlertRow[]) : []);
  const [loading, setLoading] = useState(false);
  const [filter, setFilter] = useState<"all" | "entries" | "mgmt" | "aplus" | "buy" | "sell" | "rvol" | "bias" | "rjb" | "abs" | "wick" | "cvd" | "time_series">("all");
  const [search, setSearch] = useState("");
  const [showSkipped, setShowSkipped] = useState(false);
  const [expandedId, setExpandedId] = useState<number | null>(null);

  const fetchAlerts = async () => {
    setLoading(true);
    try {
      const res = await api.alerts(500);
      if (res && Array.isArray(res.alerts)) {
        setData(res.alerts as ExtendedAlertRow[]);
      }
    } catch (err) {
      console.error("fetchAlerts error:", err);
    } finally {
      setLoading(false);
    }
  };

  // Sync when propAlerts update or fetch fresh on mount
  useEffect(() => {
    if (Array.isArray(propAlerts) && propAlerts.length > 0) {
      setData(propAlerts as ExtendedAlertRow[]);
    } else {
      fetchAlerts();
    }
  }, [propAlerts]);

  // Deduplicate entries by unique id or composite signature to prevent double logs
  const deduplicatedData = useMemo(() => {
    if (!Array.isArray(data)) return [];
    const seen = new Set<string>();
    const out: ExtendedAlertRow[] = [];
    for (const a of data) {
      if (!a) continue;
      const key = a.id != null ? `id_${a.id}` : `${a.ts}_${a.tf}_${a.kind}_${a.title}`;
      if (!seen.has(key)) {
        seen.add(key);
        out.push(a);
      }
    }
    return out;
  }, [data]);

  const skippedCount = useMemo(() => {
    return deduplicatedData.filter((a) => {
      const g = safeStr(a.grade).toLowerCase();
      const t = safeStr(a.title).toLowerCase();
      return g === "skip" || (a as any).is_skipped || t.startsWith("[skipped]");
    }).length;
  }, [deduplicatedData]);

  const filtered = useMemo(() => {
    if (!Array.isArray(deduplicatedData)) return [];
    return deduplicatedData.filter((a) => {
      if (!a) return false;
      const k = safeStr(a.kind).toUpperCase();
      const t = safeStr(a.title);
      const s = safeStr(a.side).toUpperCase();
      const g = safeStr(a.grade).toLowerCase();
      const isSkip = g === "skip" || (a as any).is_skipped || t.toLowerCase().startsWith("[skipped]");

      // Hide skipped diagnostics by default unless explicitly toggled on
      if (isSkip && !showSkipped) {
        return false;
      }

      // Quick filter
      if (filter === "time_series") {
        if (!k.startsWith("SIG_TS_")) return false;
      } else if (filter === "cvd") {
        if (!k.includes("CVD")) return false;
      } else if (filter === "entries") {
        if (s === "HOLD" || g === "mgmt" || k.includes("TRAIL") || k.includes("MGMT")) return false;
      } else if (filter === "mgmt") {
        if (s !== "HOLD" && g !== "mgmt" && !k.includes("TRAIL") && !k.includes("MGMT")) return false;
      } else if (filter === "aplus") {
        if (!k.includes("APLUS") && !t.includes("A+") && !k.includes("DEEP_REV") && !t.includes("DEEP REVERSAL") && !k.includes("EARLY_REV")) return false;
      } else if (filter === "buy") {
        if (s !== "BUY" && s !== "LONG" && s !== "CE") return false;
      } else if (filter === "sell") {
        if (s !== "SELL" && s !== "SHORT" && s !== "PE") return false;
      } else if (filter === "rvol") {
        if (g !== "rvol" && !k.includes("RVOL") && !t.includes("RVOL")) return false;
      } else if (filter === "bias") {
        if (!k.includes("BIAS") && !t.toLowerCase().includes("bias")) return false;
      } else if (filter === "rjb") {
        if (!k.includes("RJB") && !t.includes("RJB")) return false;
      } else if (filter === "abs") {
        if (!k.includes("ABS") && !t.toLowerCase().includes("absorption") && !k.includes("AGGRESSION") && !t.toLowerCase().includes("aggression")) return false;
      } else if (filter === "wick") {
        if (!k.includes("WICK") && !t.toLowerCase().includes("wick")) return false;
      }

      // Text search
      if (search.trim()) {
        const q = search.trim().toLowerCase();
        const hay = `${safeStr(a.title)} ${safeStr(a.body)} ${safeStr(a.kind)} ${safeStr(a.side)} ${safeStr(a.tf)} ${a.price ?? ""}`.toLowerCase();
        if (!hay.includes(q)) return false;
      }
      return true;
    });
  }, [deduplicatedData, filter, search, showSkipped]);

  const exportCsv = () => {
    if (!filtered.length) return;
    const headers = ["ID", "Time", "TF", "Kind", "Side", "Grade", "Price", "Title", "Details"];
    const rows = filtered.map((a) => [
      a.id ?? "",
      `"${safeStr(a.time || (a.ts ? new Date(a.ts * 1000).toLocaleTimeString("en-IN") : ""))}"`,
      `"${safeStr(a.tf)}"`,
      `"${safeStr(a.kind)}"`,
      `"${safeStr(a.side)}"`,
      `"${safeStr(a.grade)}"`,
      typeof a.price === "number" && !isNaN(a.price) ? a.price.toFixed(1) : "",
      `"${safeStr(a.title).replace(/"/g, '""')}"`,
      `"${safeStr(a.body).replace(/"/g, '""')}"`,
    ]);
    const csvContent = [headers.join(","), ...rows.map((r) => r.join(","))].join("\n");
    const blob = new Blob([csvContent], { type: "text/csv;charset=utf-8;" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = `nifty_alerts_${new Date().toISOString().slice(0, 10)}.csv`;
    link.click();
    URL.revokeObjectURL(url);
  };

  const getKindBadge = (a: ExtendedAlertRow) => {
    const k = safeStr(a.kind).toUpperCase();
    const t = safeStr(a.title).toUpperCase();
    const g = safeStr(a.grade).toLowerCase();
    const s = safeStr(a.side).toUpperCase();
    const isSkip = g === "skip" || (a as any).is_skipped || t.startsWith("[SKIPPED]");

    if (isSkip) {
      return <span className="pill bg-slate-800 text-slate-400 border border-slate-700">SKIPPED</span>;
    }
    if (k.startsWith("SIG_TS_")) return <span className="pill bg-cyan-500/20 text-cyan-300 border border-cyan-500/40">TIME-SERIES</span>;
    if (k.includes("CVD")) {
      return <span className="pill bg-amber-500/20 text-amber-300 border border-amber-500/40">{k.includes("NODES") ? "CVD + NODES" : "CVD EXHAUSTION"}</span>;
    }
    if (k.includes("EARLY_REV") || t.includes("EARLY REVERSAL")) {
      return <span className="pill bg-emerald-600/20 text-emerald-300 border border-emerald-500/50 font-bold">🎯 EARLY REV</span>;
    }
    if (k.includes("RETEST_FILL") || t.includes("LIMIT RETEST FILLED")) {
      return <span className="pill bg-cyan-600/20 text-cyan-300 border border-cyan-500/50 font-bold">🎯 RETEST FILL</span>;
    }
    if (k.includes("OC_CROSSOVER") || k.includes("TRINITY_LOCK") || t.includes("WRITER FLIP") || t.includes("TRINITY LOCK")) {
      return <span className="pill bg-orange-600/20 text-orange-300 border border-orange-500/50 font-bold">🔥 WRITER FLIP</span>;
    }
    if (k.includes("DEEP_REV") || t.includes("DEEP REVERSAL")) {
      return <span className="pill bg-rose-600/20 text-rose-300 border border-rose-500/50 font-bold">🚨 DEEP REV</span>;
    }
    if (k.includes("TRAIL") || k.includes("MGMT") || s === "HOLD") {
      return <span className="pill bg-slate-700/50 text-slate-300 border border-slate-600">🛡️ TRAIL SL</span>;
    }
    if (k.includes("APLUS") || t.includes("A+")) {
      return <span className="pill bg-amber-500/20 text-amber-300 border border-amber-500/50 font-bold">★ A+</span>;
    }
    if (k.includes("BIAS") || t.includes("BIAS")) {
      return <span className="pill bg-gold-500/20 text-gold-300 border border-gold-500/40">⚖ {BIAS_NAME}</span>;
    }
    if (k.includes("CONF") || t.includes("CONF")) {
      return <span className="pill bg-emerald-500/20 text-emerald-300 border border-emerald-500/40">✓3 CONF</span>;
    }
    if (k.includes("PE") || k.includes("CE") || t.includes("PE−CE") || t.includes("PE-CE")) {
      return <span className="pill bg-cyan-500/20 text-cyan-300 border border-cyan-500/40">🎯 PE−CE</span>;
    }
    if (k.includes("SMT") || t.includes("SMT")) {
      return <span className="pill bg-pink-500/20 text-pink-300 border border-pink-500/40">SMT</span>;
    }
    if (k.includes("IFVG") || t.includes("IFVG")) {
      return <span className="pill bg-sky-500/20 text-sky-300 border border-sky-500/40">IFVG</span>;
    }
    if (k.includes("RJB") || t.includes("RJB")) {
      return <span className="pill bg-fuchsia-500/20 text-fuchsia-300 border border-fuchsia-500/40">⟂ RJB</span>;
    }
    if (k.includes("CRT") || t.includes("CRT")) {
      return <span className="pill bg-purple-500/20 text-purple-300 border border-purple-500/40">CRT</span>;
    }
    if (k.includes("DOL") || t.includes("D.O.L")) {
      return <span className="pill bg-violet-500/20 text-violet-300 border border-violet-500/40">D.O.L</span>;
    }
    if (k.includes("GAMMA") || t.includes("GAMMA")) {
      return <span className="pill bg-violet-500/20 text-violet-300 border border-violet-500/40">γ ✓✓✓</span>;
    }
    if (k.includes("NODES") || t.includes("ENTRY") || k.includes("ENTRY") || k.includes("LIMIT_RETEST_SETUP")) {
      return <span className="pill bg-yellow-500/20 text-yellow-300 border border-yellow-500/40">● ENTRY</span>;
    }
    if (k.includes("FLOW") || t.includes("FLOW")) {
      return <span className="pill bg-emerald-500/20 text-emerald-300 border border-emerald-500/40">🧭 FLOW</span>;
    }
    if (g === "rvol" || k.includes("RVOL") || t.includes("RVOL")) {
      return <span className="pill bg-teal-500/20 text-teal-300 border border-teal-500/40">⚡ RVOL</span>;
    }
    const clean = k.replace("SIG_", "");
    return <span className="pill bg-ink-800 text-slate-300 border border-ink-700">{clean || "ALERT"}</span>;
  };

  const getSideBadge = (side?: string | null, a?: ExtendedAlertRow) => {
    const g = a ? safeStr(a.grade).toLowerCase() : "";
    const t = a ? safeStr(a.title).toLowerCase() : "";
    if (g === "skip" || (a as any)?.is_skipped || t.startsWith("[skipped]")) {
      return <span className="text-slate-500 font-mono text-[10px]">SKIPPED</span>;
    }
    if (!side) return <span className="text-slate-500 font-mono">—</span>;
    const s = safeStr(side).toUpperCase();
    if (s === "BUY" || s === "LONG" || s === "CE") {
      return <span className="font-bold text-bull-400">BUY ▲</span>;
    }
    if (s === "SELL" || s === "SHORT" || s === "PE") {
      return <span className="font-bold text-bear-400">SELL ▼</span>;
    }
    if (s === "HOLD") {
      return <span className="font-bold text-amber-400 font-mono">HOLD 🛡️</span>;
    }
    return <span className="text-slate-400 font-mono">{s}</span>;
  };

  return (
    <div className="flex flex-col h-full bg-[#060b16] text-slate-200">
      {/* Top Controls Toolbar */}
      <div className="flex flex-wrap items-center justify-between gap-2 px-3 py-2 border-b border-ink-800 bg-ink-900/60">
        <div className="flex flex-wrap items-center gap-1.5">
          <span className="font-bold text-sm text-gold-400 flex items-center gap-1.5 mr-2">
            <span>🔔</span>
            <span>Alerts & Signals Log</span>
            <span className="ml-1 text-xs px-1.5 py-0.5 rounded-full bg-ink-800 text-slate-300 font-normal">
              {filtered.length} of {data.length}
            </span>
          </span>

          {/* Quick Filters */}
          <button
            onClick={() => setFilter("all")}
            className={`pill text-[11px] ${filter === "all" ? "bg-gold-500 text-ink-900 font-bold" : "bg-ink-800 text-slate-300"}`}
          >
            All Alerts
          </button>
          <button
            onClick={() => setFilter("entries")}
            className={`pill text-[11px] ${filter === "entries" ? "bg-cyan-500 text-ink-900 font-bold" : "bg-ink-800 text-cyan-300"}`}
            title="Actionable entry setups only (excludes Trail SL / Mgmt)"
          >
            🎯 Entries Only
          </button>
          <button
            onClick={() => setFilter("mgmt")}
            className={`pill text-[11px] ${filter === "mgmt" ? "bg-amber-500 text-ink-900 font-bold" : "bg-ink-800 text-amber-300"}`}
            title="Active Trade Management & Trailing Stop Updates"
          >
            🛡️ Trail / Mgmt
          </button>
          <button
            onClick={() => setFilter("aplus")}
            className={`pill text-[11px] ${filter === "aplus" ? "bg-amber-500 text-ink-900 font-bold" : "bg-ink-800 text-amber-300"}`}
          >
            ★ A+ Setups
          </button>
          <button
            onClick={() => setFilter("buy")}
            className={`pill text-[11px] ${filter === "buy" ? "bg-bull-400 text-ink-900 font-bold" : "bg-ink-800 text-bull-400"}`}
          >
            Buy / CE
          </button>
          <button
            onClick={() => setFilter("sell")}
            className={`pill text-[11px] ${filter === "sell" ? "bg-bear-400 text-ink-900 font-bold" : "bg-ink-800 text-bear-400"}`}
          >
            Sell / PE
          </button>
          <button
            onClick={() => setFilter("rvol")}
            className={`pill text-[11px] ${filter === "rvol" ? "bg-teal-400 text-ink-900 font-bold" : "bg-ink-800 text-teal-300"}`}
          >
            ⚡ RVOL
          </button>
          <button onClick={()=>setFilter("time_series")} className={"pill text-[11px] "+(filter==="time_series"?"bg-cyan-400 text-ink-900 font-bold":"bg-ink-800 text-cyan-300")}>Time-series alerts</button>
          <button onClick={() => setFilter("cvd")} className={`pill text-[11px] ${filter==="cvd"?"bg-amber-400 text-ink-900 font-bold":"bg-ink-800 text-amber-300"}`}>CVD alerts</button>
          <button
            onClick={() => setFilter("bias")}
            className={`pill text-[11px] ${filter === "bias" ? "bg-gold-500 text-ink-900 font-bold" : "bg-ink-800 text-gold-300"}`}
          >
            ⚖ {BIAS_NAME}
          </button>
          <button
            onClick={() => setFilter("rjb")}
            className={`pill text-[11px] ${filter === "rjb" ? "bg-fuchsia-400 text-ink-900 font-bold" : "bg-ink-800 text-fuchsia-300"}`}
          >
            ⟂ RJB
          </button>
          <button
            onClick={() => setFilter("abs")}
            className={`pill text-[11px] ${filter === "abs" ? "bg-amber-400 text-ink-900 font-bold" : "bg-ink-800 text-amber-300"}`}
          >
            🛡 Δ / ABS
          </button>
          <button
            onClick={() => setFilter("wick")}
            className={`pill text-[11px] ${filter === "wick" ? "bg-purple-400 text-ink-900 font-bold" : "bg-ink-800 text-purple-300"}`}
          >
            🕯 HTF Wicks
          </button>
          <button
            onClick={() => setShowSkipped(!showSkipped)}
            className={`pill text-[11px] border ${showSkipped ? "bg-rose-500/20 text-rose-300 border-rose-500/50 font-bold" : "bg-ink-800 text-slate-400 border-ink-700"}`}
            title="Toggle display of skipped / rejected diagnostic setups"
          >
            🔍 Show Skipped ({skippedCount})
          </button>
        </div>

        <div className="flex items-center gap-2">
          {/* Search box */}
          <input
            type="text"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Search alerts…"
            className="px-2.5 py-1 text-xs bg-ink-850 border border-ink-700 rounded text-slate-200 placeholder-slate-500 focus:outline-none focus:border-gold-500/60 w-36 sm:w-48"
          />

          {/* Action buttons */}
          <button
            onClick={exportCsv}
            disabled={!filtered.length}
            className="pill bg-ink-800 hover:bg-ink-700 text-slate-200 border border-ink-700 flex items-center gap-1 text-[11px]"
            title="Export alerts to CSV file"
          >
            <span>⬇ CSV</span>
          </button>
          <button
            onClick={() => {
              if (onRefresh) onRefresh();
              fetchAlerts();
            }}
            disabled={loading}
            className="pill bg-ink-800 hover:bg-ink-700 text-slate-200 border border-ink-700 text-[11px]"
            title="Refresh alerts from database"
          >
            {loading ? "…" : "🔄"}
          </button>
        </div>
      </div>

      {/* Table Content */}
      <div className="flex-1 overflow-auto min-h-0">
        <table className="w-full text-left text-xs border-collapse">
          <thead className="sticky top-0 bg-[#091122] text-slate-400 font-semibold border-b border-ink-800 z-10 text-[11px] uppercase tracking-wider">
            <tr>
              <th className="py-2 px-3 w-28">Time (IST)</th>
              <th className="py-2 px-2.5 w-16">TF</th>
              <th className="py-2 px-3 w-28">Signal</th>
              <th className="py-2 px-2.5 w-20">Side</th>
              <th className="py-2 px-3 w-24">Price</th>
              <th className="py-2 px-3">Title & Details</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-ink-800/60">
            {filtered.length === 0 ? (
              <tr>
                <td colSpan={6} className="py-8 text-center text-slate-500 text-sm">
                  {search ? "No matching alerts found for this filter." : "No alerts recorded yet today. Signals will log automatically here."}
                </td>
              </tr>
            ) : (
              filtered.map((a, idx) => {
                const rowKey = `${a.id ?? "row"}-${a.ts ?? idx}-${idx}`;
                const isExpanded = expandedId === a.id;
                const timeStr = safeStr(a.time) || (a.ts ? new Date(a.ts * 1000).toLocaleTimeString("en-IN", { hour: "2-digit", minute: "2-digit" }) : "—");
                const isAplus = safeStr(a.kind).toUpperCase().includes("APLUS") || safeStr(a.title).includes("A+");

                // Parse payload details if available
                const pl = typeof a.payload === "object" && a.payload !== null ? a.payload : undefined;
                const confluences = Array.isArray(pl?.confluence) ? pl.confluence : [];
                const story = Array.isArray(pl?.story) ? pl.story : [];
                const reasons = Array.isArray(pl?.reasons) ? pl.reasons : [];

                return (
                  <React.Fragment key={rowKey}>
                    <tr
                      onClick={() => setExpandedId(isExpanded ? null : (a.id ?? null))}
                      className={`cursor-pointer transition-colors hover:bg-ink-800/40 ${isAplus ? "bg-amber-950/15" : ""}`}
                    >
                      <td className="py-2 px-3 num text-slate-400 whitespace-nowrap">{timeStr}</td>
                      <td className="py-2 px-2.5">
                        <span className="px-1.5 py-0.5 rounded text-[10px] font-mono bg-ink-800 text-slate-300 border border-ink-700">
                          {safeStr(a.tf) || "5m"}
                        </span>
                      </td>
                      <td className="py-2 px-3 whitespace-nowrap">{getKindBadge(a)}</td>
                      <td className="py-2 px-2.5 whitespace-nowrap">{getSideBadge(a.side, a)}</td>
                      <td className="py-2 px-3 num font-semibold text-slate-100 whitespace-nowrap">
                        {typeof a.price === "number" && a.price > 0 ? a.price.toLocaleString("en-IN", { minimumFractionDigits: 1 }) : "—"}
                      </td>
                      <td className="py-2 px-3">
                        <div className="flex items-center justify-between gap-2">
                          <div className="font-semibold text-slate-100 text-[12.5px] leading-snug">
                            {safeStr(a.title)}
                          </div>
                          <span className="text-[10px] text-slate-500 font-mono shrink-0">
                            {isExpanded ? "▲ close" : "▼ details"}
                          </span>
                        </div>
                        <div className={`text-slate-400 text-[11.5px] mt-0.5 leading-relaxed ${isExpanded ? "" : "line-clamp-1"}`}>
                          {safeStr(a.body)}
                        </div>
                      </td>
                    </tr>

                    {/* Expanded details tray */}
                    {isExpanded && (
                      <tr className="bg-ink-950/80 border-b border-ink-800">
                        <td colSpan={6} className="px-5 py-3">
                          <div className="space-y-2 text-xs">
                            <div className="text-slate-300 leading-relaxed bg-ink-900/70 p-2.5 rounded border border-ink-800">
                              {safeStr(a.body) || "No extra summary available."}
                            </div>

                            {/* Story / ICT Rules */}
                            {story.length > 0 && (
                              <div>
                                <span className="text-[10px] uppercase font-bold text-slate-400 tracking-wider">Setup Story:</span>
                                <ul className="mt-1 space-y-0.5 text-[11px] text-slate-300 pl-2">
                                  {story.map((st, i) => (
                                    <li key={i} className="flex gap-1.5">
                                      <span className="text-gold-400">·</span>
                                      <span>{safeStr(st)}</span>
                                    </li>
                                  ))}
                                </ul>
                              </div>
                            )}

                            {/* Confluences */}
                            {confluences.length > 0 && (
                              <div>
                                <span className="text-[10px] uppercase font-bold text-slate-400 tracking-wider">Confluences:</span>
                                <div className="flex flex-wrap gap-1.5 mt-1">
                                  {confluences.map((c, i) => (
                                    <span key={i} className="pill bg-emerald-950/40 text-emerald-300 border border-emerald-500/30 text-[11px]">
                                      ✓ {safeStr(c)}
                                    </span>
                                  ))}
                                </div>
                              </div>
                            )}

                            {/* Reasons / Skip details */}
                            {reasons.length > 0 && (
                              <div>
                                <span className="text-[10px] uppercase font-bold text-amber-400 tracking-wider">Note / Filters:</span>
                                <div className="flex flex-wrap gap-1.5 mt-1">
                                  {reasons.map((r, i) => (
                                    <span key={i} className="pill bg-amber-950/40 text-amber-300 border border-amber-500/30 text-[11px]">
                                      ✕ {safeStr(r)}
                                    </span>
                                  ))}
                                </div>
                              </div>
                            )}
                          </div>
                        </td>
                      </tr>
                    )}
                  </React.Fragment>
                );
              })
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}

export default function AlertsTable(props: AlertsTableProps) {
  return (
    <AlertsErrorBoundary>
      <AlertsTableInner {...props} />
    </AlertsErrorBoundary>
  );
}
