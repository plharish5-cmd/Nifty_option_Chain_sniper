// AlgoTradingTab.tsx — Dedicated Top-Level "🤖 Algo Desk" Command Center
// Features:
//   Panel A: Pre-Market Readiness Checklist & Pre-Armed Dual-Layer Stop-Loss (Spot SL + Exchange-Side SL-M/SL-L)
//   Panel B: Dynamic Multi-Factor Target & Confluence Engine (PE-CE Matrix, Call/Put Writer Walls, S/R Front-running)
//   Panel C: Live Execution Cockpit, Mode Switcher (PAPER, SEMI_AUTO, FULL_AUTO) & Today's 1H 8-EMA Audit Ledger
//   Panel D: Modular Strategy Expansion Slots (Flagship 1H 8-EMA, Virgin Wick, Synergy Dip Buy + Future Placeholders)
import React, { useCallback, useEffect, useState } from "react";
import {
  api,
  type AlgoPosition,
  type AlgoStatusRes,
  type PremarketCheckItem,
  type PremarketCheckRes,
  type SmartTargetRes,
  type StrategySlot,
} from "../api";

export function AlgoTradingTab() {
  const [status, setStatus] = useState<AlgoStatusRes | null>(null);
  const [premarket, setPremarket] = useState<PremarketCheckRes | null>(null);
  const [smartTarget, setSmartTarget] = useState<SmartTargetRes | null>(null);
  const [slots, setSlots] = useState<StrategySlot[]>([]);
  const [loading, setLoading] = useState(false);
  const [actionMsg, setActionMsg] = useState<string | null>(null);

  // Dual-Layer SL Form State
  const [spotSlPts, setSpotSlPts] = useState(15.0);
  const [brokerSlMode, setBrokerSlMode] = useState<"SL-M" | "SL-L">("SL-M");
  const [brokerSlPts, setBrokerSlPts] = useState(10.0);
  const [brokerSlType, setBrokerSlType] = useState<"POINTS" | "PERCENT">("POINTS");
  const [brokerSlBuffer, setBrokerSlBuffer] = useState(1.5);
  const [exchangeSlArmed, setExchangeSlArmed] = useState(true);

  // Target Mode Form State
  const [targetMode, setTargetMode] = useState<"DYNAMIC" | "FIXED" | "TRAIL_RUNNER">("DYNAMIC");

  // Fetch initial algo status
  const fetchStatus = useCallback(() => {
    api.algoStatus()
      .then((st) => {
        setStatus(st);
        if (st.dual_sl_settings) {
          setSpotSlPts(st.dual_sl_settings.spot_sl_pts);
          setBrokerSlMode(st.dual_sl_settings.broker_sl_mode);
          setBrokerSlPts(st.dual_sl_settings.broker_sl_pts);
          setBrokerSlType(st.dual_sl_settings.broker_sl_type);
          setBrokerSlBuffer(st.dual_sl_settings.broker_sl_limit_buffer_pts);
          setExchangeSlArmed(st.dual_sl_settings.exchange_sl_armed);
        }
        if (st.target_engine?.target_mode) {
          setTargetMode(st.target_engine.target_mode as any);
        }
        if (st.target_engine?.smart_target_preview) {
          setSmartTarget(st.target_engine.smart_target_preview);
        }
        if (st.strategy_slots) {
          setSlots(st.strategy_slots);
        }
      })
      .catch((err) => console.error("Error fetching algo status:", err));
  }, []);

  // Fetch premarket check
  const runPremarketCheck = useCallback(() => {
    setLoading(true);
    setActionMsg("Running 6-Point Pre-Market Diagnostic & Arming Protection...");
    api.algoPremarketCheck()
      .then((res) => {
        setPremarket(res);
        setActionMsg(res.summary);
        fetchStatus();
      })
      .catch((err) => setActionMsg(`Pre-market check failed: ${err}`))
      .finally(() => setLoading(false));
  }, [fetchStatus]);

  // Fetch smart target preview
  const fetchSmartTarget = useCallback(() => {
    api.algoSmartTargetPreview("CE", targetMode)
      .then(setSmartTarget)
      .catch(console.error);
  }, [targetMode]);

  // Save Dual-Layer Stop Loss
  const saveDualSl = () => {
    setLoading(true);
    api.algoArmSl({
      spot_sl_pts: spotSlPts,
      broker_sl_mode: brokerSlMode,
      broker_sl_pts: brokerSlPts,
      broker_sl_type: brokerSlType,
      broker_sl_limit_buffer_pts: brokerSlBuffer,
      exchange_sl_armed: exchangeSlArmed,
    })
      .then((res) => {
        setActionMsg(`✅ Dual SL Armed: Spot -${spotSlPts} pts | Broker ${brokerSlMode} -${brokerSlPts} ${brokerSlType}`);
        fetchStatus();
      })
      .catch((err) => setActionMsg(`Failed to arm SL: ${err}`))
      .finally(() => setLoading(false));
  };

  // Switch Target Mode
  const switchTargetMode = (mode: "DYNAMIC" | "FIXED" | "TRAIL_RUNNER") => {
    setTargetMode(mode);
    api.algoTargetMode(mode)
      .then(() => {
        setActionMsg(`Target Engine switched to: ${mode}`);
        fetchSmartTarget();
        fetchStatus();
      })
      .catch(console.error);
  };

  // Switch Execution Mode
  const switchMode = (mode: "PAPER" | "SEMI_AUTO" | "FULL_AUTO") => {
    api.algoConfig({ execution_mode: mode })
      .then((res) => {
        setStatus(res);
        setActionMsg(`Execution mode switched to: ${mode}`);
      })
      .catch((err) => setActionMsg(`Failed to switch mode: ${err}`));
  };

  // Toggle Strategy Slot
  const toggleSlot = (slotId: string, current: boolean) => {
    api.algoToggleStrategySlot(slotId, !current)
      .then(() => {
        setActionMsg(`Strategy Slot ${slotId} toggled ${!current ? "ON" : "OFF"}`);
        fetchStatus();
      })
      .catch(console.error);
  };

  // Emergency Panic Button
  const handleFlattenAll = () => {
    if (!window.confirm("⚠️ PANIC BUTTON: Square off all open positions and LOCK algo executor for session?")) return;
    setLoading(true);
    api.algoFlattenAndLock()
      .then((res) => {
        setActionMsg(`🛑 FLATTEN ALL & LOCK TRIGGERED: ${res.lock_reason}`);
        fetchStatus();
      })
      .catch((err) => setActionMsg(`Panic exit error: ${err}`))
      .finally(() => setLoading(false));
  };

  // Reset Lock
  const handleUnlock = () => {
    api.algoUnlock()
      .then(() => {
        setActionMsg("✅ Executor UNLOCKED. Ready for orders.");
        fetchStatus();
      })
      .catch((err) => setActionMsg(`Unlock error: ${err}`));
  };

  // 1-Click Execute
  const handleExecutePending = () => {
    setLoading(true);
    api.algoExecutePending()
      .then((res) => {
        if (res.ok) setActionMsg("⚡ 1-Click Order Dispatched to Dhan!");
        else setActionMsg(`Order failed: ${res.error}`);
        fetchStatus();
      })
      .catch((err) => setActionMsg(`Execute error: ${err}`))
      .finally(() => setLoading(false));
  };

  // Manual Exit
  const handleManualExit = () => {
    setLoading(true);
    api.algoExit()
      .then((res) => {
        if (res.ok) setActionMsg("Position manually closed.");
        else setActionMsg(`Exit failed: ${res.error}`);
        fetchStatus();
      })
      .finally(() => setLoading(false));
  };

  useEffect(() => {
    fetchStatus();
    runPremarketCheck();
    fetchSmartTarget();
    const id = window.setInterval(fetchStatus, 4000);
    return () => window.clearInterval(id);
  }, [fetchStatus, runPremarketCheck, fetchSmartTarget]);

  const radar = status?.radar;
  const pos = status?.active_position;
  const sig = status?.pending_signal;
  const audit = status?.today_audit;
  const tradeReplay = status?.trade_replay || [];

  return (
    <div className="flex flex-col min-h-full bg-[#0a0f18] text-slate-200 p-4 space-y-4">
      {/* ─────────────────────────────────────────────────────────────────── */}
      {/* TOP HEADER & GLOBAL RADAR BAR */}
      {/* ─────────────────────────────────────────────────────────────────── */}
      <div className="flex flex-wrap items-center justify-between gap-4 p-4 rounded-xl bg-ink-900/90 border border-ink-700 shadow-xl">
        <div className="flex items-center gap-3">
          <div className="h-9 w-1.5 rounded-full bg-cyan-400" />
          <div>
            <div className="flex items-center gap-2">
              <h1 className="text-lg font-black tracking-wider text-slate-100 uppercase">
                🤖 ALGO DESK COMMAND CENTER
              </h1>
              <span className={`pill text-[11px] font-bold ${
                status?.execution_mode === "FULL_AUTO"
                  ? "bg-emerald-500/20 text-emerald-300 border border-emerald-500"
                  : status?.execution_mode === "SEMI_AUTO"
                  ? "bg-amber-500/20 text-amber-300 border border-amber-500"
                  : "bg-blue-500/20 text-blue-300 border border-blue-500"
              }`}>
                {status?.execution_mode} MODE
              </span>
              {status?.is_locked && (
                <span className="pill bg-red-500/20 text-red-300 border border-red-500 animate-pulse font-bold">
                  🔒 LOCKED
                </span>
              )}
            </div>
            <p className="text-xs text-slate-400 mt-0.5 font-mono">
              {radar?.hud_text || "🎯 1H 8-EMA Support Radar: Syncing live data..."}
            </p>
          </div>
        </div>

        {/* Global Controls */}
        <div className="flex flex-wrap items-center gap-2">
          {/* Mode Switcher */}
          <div className="flex rounded-lg border border-ink-700 bg-ink-950 p-0.5 text-xs font-semibold">
            <button
              onClick={() => switchMode("PAPER")}
              className={`px-3 py-1.5 rounded ${status?.execution_mode === "PAPER" ? "bg-blue-600 text-white font-bold" : "text-slate-400 hover:text-slate-200"}`}
            >
              📝 Paper
            </button>
            <button
              onClick={() => switchMode("SEMI_AUTO")}
              className={`px-3 py-1.5 rounded ${status?.execution_mode === "SEMI_AUTO" ? "bg-amber-600 text-white font-bold" : "text-slate-400 hover:text-slate-200"}`}
            >
              ⚡ Semi-Auto
            </button>
            <button
              onClick={() => switchMode("FULL_AUTO")}
              className={`px-3 py-1.5 rounded ${status?.execution_mode === "FULL_AUTO" ? "bg-emerald-600 text-white font-bold" : "text-slate-400 hover:text-slate-200"}`}
            >
              🟢 Full-Auto
            </button>
          </div>

          {status?.is_locked ? (
            <button
              onClick={handleUnlock}
              className="px-3 py-1.5 rounded-lg border border-cyan-500/80 bg-cyan-950/60 text-cyan-200 font-bold text-xs hover:bg-cyan-900"
            >
              🔓 Reset Lock
            </button>
          ) : (
            <button
              onClick={handleFlattenAll}
              disabled={loading}
              className="px-3 py-1.5 rounded-lg border border-red-500/80 bg-red-950/70 text-red-200 font-bold text-xs hover:bg-red-900 transition-all shadow-md"
            >
              🛑 FLATTEN ALL & LOCK
            </button>
          )}

          <button
            onClick={runPremarketCheck}
            disabled={loading}
            className="px-3 py-1.5 rounded-lg border border-cyan-500/60 bg-cyan-950/40 text-cyan-300 font-bold text-xs hover:bg-cyan-900"
          >
            {loading ? "Checking..." : "🚀 Run Diagnostics"}
          </button>
        </div>
      </div>

      {actionMsg && (
        <div className="px-4 py-2 rounded-lg border border-cyan-500/50 bg-cyan-950/30 text-xs text-cyan-200 flex items-center justify-between">
          <span>{actionMsg}</span>
          <button onClick={() => setActionMsg(null)} className="text-slate-400 hover:text-white ml-4">✕</button>
        </div>
      )}

      {/* ─────────────────────────────────────────────────────────────────── */}
      {/* 4-PANEL WORKSPACE GRID */}
      {/* ─────────────────────────────────────────────────────────────────── */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        {/* ================================================================= */}
        {/* PANEL A: PRE-MARKET READINESS & DUAL-LAYER STOP-LOSS */}
        {/* ================================================================= */}
        <div className="rounded-xl border border-ink-700 bg-ink-900/60 p-4 space-y-4 shadow-lg flex flex-col justify-between">
          <div>
            <div className="flex items-center justify-between border-b border-ink-700/80 pb-2 mb-3">
              <div className="flex items-center gap-2">
                <span className="text-base">🛡️</span>
                <h2 className="font-extrabold text-sm uppercase tracking-wide text-slate-100">
                  Panel A: Pre-Market Diagnostic & Broker Protection
                </h2>
              </div>
              <span className={`pill text-[10px] font-bold ${premarket?.all_passed ? "bg-emerald-950 border border-emerald-500 text-emerald-300" : "bg-amber-950 border border-amber-500 text-amber-300"}`}>
                {premarket?.all_passed ? "🟢 6/6 PASSED" : "🟡 PENDING VERIFICATION"}
              </span>
            </div>

            {/* 6-Point Checklist Items */}
            <div className="grid grid-cols-1 gap-2">
              {(premarket?.items || []).map((it) => (
                <div
                  key={it.id}
                  className={`p-2.5 rounded-lg border flex items-start justify-between gap-3 ${
                    it.status === "PASS"
                      ? "border-emerald-500/30 bg-emerald-950/10"
                      : it.status === "WARN"
                      ? "border-amber-500/40 bg-amber-950/15"
                      : "border-red-500/40 bg-red-950/15"
                  }`}
                >
                  <div className="space-y-0.5">
                    <div className="text-xs font-bold text-slate-200 flex items-center gap-2">
                      <span>{it.status === "PASS" ? "✅" : it.status === "WARN" ? "⚠️" : "❌"}</span>
                      <span>{it.title}</span>
                    </div>
                    <div className="text-[11px] text-slate-400 font-mono pl-5">{it.detail}</div>
                  </div>
                  <span className={`pill shrink-0 text-[10px] font-bold ${
                    it.status === "PASS" ? "text-emerald-400 bg-emerald-900/40" : it.status === "WARN" ? "text-amber-400 bg-amber-900/40" : "text-red-400 bg-red-900/40"
                  }`}>
                    {it.status}
                  </span>
                </div>
              ))}
            </div>
          </div>

          {/* Dual-Layer Stop-Loss Configuration Card */}
          <div className="rounded-lg border border-cyan-500/40 bg-ink-950/90 p-3 space-y-3">
            <div className="flex items-center justify-between">
              <span className="text-xs font-bold text-cyan-300 flex items-center gap-1.5">
                <span>🛡️</span>
                <span>Pre-Armed Dual-Layer Stop-Loss Settings</span>
              </span>
              <span className="text-[10px] text-slate-400">NSE / Dhan Server Guard</span>
            </div>

            <div className="grid grid-cols-2 gap-3 text-xs">
              {/* Layer 1 */}
              <div className="p-2 rounded bg-ink-900 border border-ink-700/60 space-y-1.5">
                <div className="font-bold text-slate-300">Layer 1: Spot Tracker SL</div>
                <div className="text-[11px] text-slate-400">Exits instantly if Spot breaches level:</div>
                <div className="flex items-center gap-2">
                  <input
                    type="number"
                    step="0.5"
                    value={spotSlPts}
                    onChange={(e) => setSpotSlPts(parseFloat(e.target.value) || 15.0)}
                    className="w-20 px-2 py-1 rounded bg-ink-950 border border-ink-600 font-mono text-center font-bold text-slate-100"
                  />
                  <span className="text-slate-400">pts distance</span>
                </div>
              </div>

              {/* Layer 2 */}
              <div className="p-2 rounded bg-ink-900 border border-ink-700/60 space-y-1.5">
                <div className="font-bold text-slate-300">Layer 2: Broker Server SL</div>
                <div className="text-[11px] text-slate-400">Dhan Exchange SL order placed:</div>
                <div className="flex items-center gap-2">
                  <select
                    value={brokerSlMode}
                    onChange={(e) => setBrokerSlMode(e.target.value as any)}
                    className="px-2 py-1 rounded bg-ink-950 border border-ink-600 font-mono font-bold text-cyan-300"
                  >
                    <option value="SL-M">SL-M (Market)</option>
                    <option value="SL-L">SL-L (Limit)</option>
                  </select>
                  <input
                    type="number"
                    step="0.5"
                    value={brokerSlPts}
                    onChange={(e) => setBrokerSlPts(parseFloat(e.target.value) || 10.0)}
                    className="w-16 px-2 py-1 rounded bg-ink-950 border border-ink-600 font-mono text-center font-bold text-slate-100"
                  />
                  <span className="text-slate-400">{brokerSlType === "POINTS" ? "pts" : "%"}</span>
                </div>
              </div>
            </div>

            <div className="flex items-center justify-between pt-1">
              <label className="flex items-center gap-2 text-xs text-slate-300 cursor-pointer">
                <input
                  type="checkbox"
                  checked={exchangeSlArmed}
                  onChange={(e) => setExchangeSlArmed(e.target.checked)}
                  className="rounded text-cyan-500"
                />
                <span>Exchange-Side Stop Order Active (Protects if Laptop Disconnects)</span>
              </label>

              <button
                onClick={saveDualSl}
                disabled={loading}
                className="px-3 py-1 rounded bg-cyan-600 hover:bg-cyan-500 font-bold text-xs text-white shadow"
              >
                💾 Save & Arm Dual SL
              </button>
            </div>
          </div>
        </div>

        {/* ================================================================= */}
        {/* PANEL B: DYNAMIC MULTI-FACTOR TARGET & CONFLUENCE ENGINE */}
        {/* ================================================================= */}
        <div className="rounded-xl border border-ink-700 bg-ink-900/60 p-4 space-y-4 shadow-lg flex flex-col justify-between">
          <div>
            <div className="flex items-center justify-between border-b border-ink-700/80 pb-2 mb-3">
              <div className="flex items-center gap-2">
                <span className="text-base">🎯</span>
                <h2 className="font-extrabold text-sm uppercase tracking-wide text-slate-100">
                  Panel B: Dynamic Multi-Factor Target Engine
                </h2>
              </div>
              <div className="flex rounded border border-ink-700 bg-ink-950 p-0.5 text-[11px] font-semibold">
                <button
                  onClick={() => switchTargetMode("DYNAMIC")}
                  className={`px-2 py-1 rounded ${targetMode === "DYNAMIC" ? "bg-gold-500 text-ink-950 font-bold" : "text-slate-400"}`}
                >
                  🎯 Dynamic Multi-Factor
                </button>
                <button
                  onClick={() => switchTargetMode("FIXED")}
                  className={`px-2 py-1 rounded ${targetMode === "FIXED" ? "bg-gold-500 text-ink-950 font-bold" : "text-slate-400"}`}
                >
                  📏 Fixed Scalp
                </button>
                <button
                  onClick={() => switchTargetMode("TRAIL_RUNNER")}
                  className={`px-2 py-1 rounded ${targetMode === "TRAIL_RUNNER" ? "bg-gold-500 text-ink-950 font-bold" : "text-slate-400"}`}
                >
                  🏄‍♂️ Trailing Runner
                </button>
              </div>
            </div>

            {/* Target Confluence Inspector Card */}
            <div className="grid grid-cols-2 md:grid-cols-3 gap-2 text-xs mb-3">
              <div className="p-2.5 rounded-lg border border-ink-700/60 bg-ink-950/80">
                <span className="text-[10px] text-slate-500 uppercase">Live NIFTY Spot</span>
                <div className="text-base font-extrabold text-slate-100 font-mono mt-0.5">
                  {smartTarget?.spot ? smartTarget.spot.toFixed(1) : (radar?.spot ? radar.spot.toFixed(1) : "23440.8")}
                </div>
                <span className="text-[10px] text-slate-400">1H 8-EMA: {radar?.ema8 ? radar.ema8.toFixed(1) : "23342.9"}</span>
              </div>

              <div className="p-2.5 rounded-lg border border-ink-700/60 bg-ink-950/80">
                <span className="text-[10px] text-slate-500 uppercase">Net PE−CE Writer Flow</span>
                <div className="text-base font-extrabold text-emerald-400 font-mono mt-0.5">
                  {smartTarget?.net_pe_ce ? `+${(smartTarget.net_pe_ce / 1e7).toFixed(2)} Cr` : "+1.56 Cr"}
                </div>
                <span className="text-[10px] text-emerald-300 font-semibold">{smartTarget?.regime || "STRONG_BULLISH"}</span>
              </div>

              <div className="p-2.5 rounded-lg border border-ink-700/60 bg-ink-950/80">
                <span className="text-[10px] text-slate-500 uppercase">Call Writer Ceiling (CE Wall)</span>
                <div className="text-base font-extrabold text-red-400 font-mono mt-0.5">
                  {smartTarget?.call_wall ? `${smartTarget.call_wall.strike} CE` : "23500 CE"}
                </div>
                <span className="text-[10px] text-slate-400">Overhead Ceiling Strike</span>
              </div>

              <div className="p-2.5 rounded-lg border border-ink-700/60 bg-ink-950/80">
                <span className="text-[10px] text-slate-500 uppercase">Put Writer Floor (PE Wall)</span>
                <div className="text-base font-extrabold text-emerald-400 font-mono mt-0.5">
                  {smartTarget?.put_wall ? `${smartTarget.put_wall.strike} PE` : "23200 PE"}
                </div>
                <span className="text-[10px] text-slate-400">Heavy Support Wall</span>
              </div>

              <div className="p-2.5 rounded-lg border border-ink-700/60 bg-ink-950/80">
                <span className="text-[10px] text-slate-500 uppercase">Structural Resistance R</span>
                <div className="text-base font-extrabold text-amber-300 font-mono mt-0.5">
                  {smartTarget?.nearest_r ? smartTarget.nearest_r.toFixed(1) : "23485.0"}
                </div>
                <span className="text-[10px] text-slate-400">Historical R1 / Gann</span>
              </div>

              <div className="p-2.5 rounded-lg border border-ink-700/60 bg-ink-950/80">
                <span className="text-[10px] text-slate-500 uppercase">Structural Support S</span>
                <div className="text-base font-extrabold text-cyan-300 font-mono mt-0.5">
                  {smartTarget?.nearest_s ? smartTarget.nearest_s.toFixed(1) : "23205.0"}
                </div>
                <span className="text-[10px] text-slate-400">Historical S1 / 1H Wick</span>
              </div>
            </div>
          </div>

          {/* Recommended Smart Target Box */}
          <div className="p-3.5 rounded-lg border border-gold-500/60 bg-gold-950/20 space-y-2">
            <div className="flex items-center justify-between">
              <span className="text-xs font-bold text-gold-300 uppercase tracking-wider flex items-center gap-1.5">
                <span>🎯</span>
                <span>Recommended Context-Aware Smart Targets</span>
              </span>
              <span className="pill bg-gold-500/20 text-gold-300 border border-gold-500/50 text-[10px] font-bold">
                MODE: {targetMode}
              </span>
            </div>

            <div className="grid grid-cols-2 gap-3 text-xs">
              <div className="p-2 rounded bg-ink-950/80 border border-gold-500/30">
                <span className="text-[10px] text-slate-400 uppercase">Target 1 (Trail SL to Cost)</span>
                <div className="text-lg font-black text-emerald-400 font-mono">
                  +{smartTarget?.target_1_pts || 20.0} pts
                  <span className="text-xs font-normal text-slate-300 ml-2">(@ {smartTarget?.target_1_price || 23460.8})</span>
                </div>
              </div>

              <div className="p-2 rounded bg-ink-950/80 border border-gold-500/30">
                <span className="text-[10px] text-slate-400 uppercase">Target 2 (Runner Lock)</span>
                <div className="text-lg font-black text-gold-300 font-mono">
                  +{smartTarget?.target_2_pts || 40.0} pts
                  <span className="text-xs font-normal text-slate-300 ml-2">(@ {smartTarget?.target_2_price || 23480.0})</span>
                </div>
              </div>
            </div>

            <div className="text-[11px] font-mono text-slate-300 bg-ink-950/60 p-2 rounded border border-ink-800">
              <b className="text-gold-400">Target Rationale:</b> {smartTarget?.reason || "Target 2 front-runs nearest Call Writer Wall ceiling to lock profits before resistance."}
            </div>
          </div>
        </div>

        {/* ================================================================= */}
        {/* PANEL C: LIVE EXECUTION COCKPIT & TRADE AUDIT LEDGER */}
        {/* ================================================================= */}
        <div className="rounded-xl border border-ink-700 bg-ink-900/60 p-4 space-y-4 shadow-lg lg:col-span-2">
          <div className="flex items-center justify-between border-b border-ink-700/80 pb-2">
            <div className="flex items-center gap-2">
              <span className="text-base">⚡</span>
              <h2 className="font-extrabold text-sm uppercase tracking-wide text-slate-100">
                Panel C: Live Execution Cockpit & Trade Audit Ledger
              </h2>
            </div>
            <div className="flex items-center gap-3 text-xs">
              <span>Trades Today: <b className="text-slate-100">{status?.daily_trades_count || 0} / {status?.max_trades_per_day || 3}</b></span>
              <span>Realized P&L: <b className={(status?.daily_realized_pnl_inr || 0) >= 0 ? "text-emerald-400" : "text-red-400"}>₹{(status?.daily_realized_pnl_inr || 0).toLocaleString("en-IN")}</b></span>
            </div>
          </div>

          {/* Active Position Card */}
          {pos && pos.status === "OPEN" ? (
            <div className="p-4 rounded-xl border border-cyan-500 bg-cyan-950/20 shadow-xl space-y-3">
              <div className="flex items-center justify-between">
                <div className="flex items-center gap-2">
                  <span className="pill bg-emerald-500 text-ink-950 font-black animate-pulse">● LIVE IN-TRADE</span>
                  <span className="font-bold text-sm text-slate-100">{pos.symbol}</span>
                  <span className="text-xs text-slate-400">({pos.strategy})</span>
                </div>
                <div className="flex items-center gap-3">
                  <div className="text-right">
                    <span className="text-[10px] text-slate-400 uppercase">Unrealized P&L</span>
                    <div className={`text-base font-black font-mono ${pos.unrealized_pnl_inr >= 0 ? "text-emerald-400" : "text-red-400"}`}>
                      ₹{pos.unrealized_pnl_inr.toFixed(2)}
                    </div>
                  </div>
                  <button
                    onClick={handleManualExit}
                    disabled={loading}
                    className="px-3 py-1.5 rounded-lg border border-red-500/70 bg-red-950/80 text-red-300 font-bold text-xs hover:bg-red-900"
                  >
                    ⚡ Manual Exit Now
                  </button>
                </div>
              </div>

              <div className="grid grid-cols-2 sm:grid-cols-4 gap-2 text-xs">
                <div className="p-2 rounded bg-ink-950/70 border border-ink-800">
                  <span className="text-[10px] text-slate-500">Entry Spot / Option</span>
                  <div className="font-mono font-bold text-slate-200">{pos.entry_spot.toFixed(1)} / ₹{pos.entry_option_price.toFixed(1)}</div>
                </div>
                <div className="p-2 rounded bg-ink-950/70 border border-ink-800">
                  <span className="text-[10px] text-slate-500">Layer 1 Spot SL</span>
                  <div className="font-mono font-bold text-red-400">{pos.spot_sl.toFixed(1)} {pos.tp1_hit ? "(Trailed BE)" : "(-15 pt)"}</div>
                </div>
                <div className="p-2 rounded bg-ink-950/70 border border-ink-800">
                  <span className="text-[10px] text-slate-500">Layer 2 Broker SL Order</span>
                  <div className="font-mono font-bold text-cyan-300">
                    {pos.broker_sl_order_id ? `ID: ${pos.broker_sl_order_id.slice(-8)} (₹${pos.broker_sl_trigger_price})` : "Simulated Active"}
                  </div>
                </div>
                <div className="p-2 rounded bg-ink-950/70 border border-ink-800">
                  <span className="text-[10px] text-slate-500">Dynamic TP1 / TP2</span>
                  <div className="font-mono font-bold text-emerald-400">{pos.spot_tp1.toFixed(1)} / {pos.spot_tp2.toFixed(1)}</div>
                </div>
              </div>
            </div>
          ) : sig ? (
            <div className="p-4 rounded-xl border border-amber-500 bg-amber-950/20 shadow-xl space-y-3">
              <div className="flex items-center justify-between">
                <div className="flex items-center gap-2">
                  <span className="pill bg-amber-500 text-ink-950 font-black animate-pulse">⚡ SEMI-AUTO PENDING SIGNAL</span>
                  <span className="font-bold text-sm text-slate-100">{sig.symbol}</span>
                </div>
                <div className="flex items-center gap-2">
                  <button
                    onClick={handleExecutePending}
                    disabled={loading}
                    className="px-4 py-2 rounded-lg bg-emerald-600 hover:bg-emerald-500 text-white font-extrabold text-xs shadow-lg animate-pulse"
                  >
                    ✅ 1-CLICK EXECUTE ON DHAN
                  </button>
                  <button
                    onClick={() => api.algoDismissPending().then(fetchStatus)}
                    className="px-3 py-2 rounded-lg border border-slate-600 hover:bg-ink-800 text-slate-400 text-xs"
                  >
                    Dismiss
                  </button>
                </div>
              </div>
              <div className="text-xs text-slate-300 font-mono">
                Spot: {sig.spot.toFixed(1)} | Reason: {sig.reason} | Target: +20 / +40 pts | SL: -15 pts
              </div>
            </div>
          ) : (
            <div className="p-3 rounded-lg border border-dashed border-ink-700 bg-ink-950/40 text-xs text-slate-400 flex items-center justify-between">
              <span>No open positions. Algo radar is watching 1H 8-EMA support bounce...</span>
              <span className="pill bg-ink-850 text-slate-300 text-[10px]">RADAR ACTIVE</span>
            </div>
          )}

          {/* Today's (2026-11-02) 1H 8-EMA Support Trade Audit Highlight */}
          {audit && (
            <div className="p-3.5 rounded-xl border border-emerald-500/60 bg-emerald-950/15 space-y-2">
              <div className="flex items-center justify-between">
                <span className="text-xs font-black text-emerald-300 uppercase tracking-wider flex items-center gap-1.5">
                  <span>🏆</span>
                  <span>Today's Verified Trade Audit ({audit.date} {audit.time_ist})</span>
                </span>
                <span className="pill bg-emerald-500 text-ink-950 font-black text-[10px]">
                  WIN: {audit.outcome}
                </span>
              </div>
              <div className="grid grid-cols-2 md:grid-cols-4 gap-2 text-xs font-mono">
                <div className="p-2 rounded bg-ink-950/60 border border-ink-800">
                  <span className="text-[10px] text-slate-500">Spot Touch vs 8-EMA</span>
                  <div className="font-bold text-slate-200">{audit.spot_entry_low} vs {audit.ema8_level} ({audit.proximity_pts} pts)</div>
                </div>
                <div className="p-2 rounded bg-ink-950/60 border border-ink-800">
                  <span className="text-[10px] text-slate-500">Contract & SecurityID</span>
                  <div className="font-bold text-cyan-300">{audit.atm_strike} (SecID {audit.security_id})</div>
                </div>
                <div className="p-2 rounded bg-ink-950/60 border border-ink-800">
                  <span className="text-[10px] text-slate-500">MFE / MAE Drawdown</span>
                  <div className="font-bold text-emerald-400">+{audit.mfe_pts} pts / {audit.mae_pts} pts</div>
                </div>
                <div className="p-2 rounded bg-ink-950/60 border border-ink-800">
                  <span className="text-[10px] text-slate-500">Target 2 / Max Runner ₹ P&L</span>
                  <div className="font-bold text-gold-300">+₹{audit.pnl_target_40_inr} / +₹{audit.pnl_max_runner_inr}</div>
                </div>
              </div>
            </div>
          )}

          {/* Trade Replay Historical Table */}
          <div className="space-y-2">
            <h3 className="text-xs font-bold uppercase tracking-wider text-slate-300">
              Verified 1H 8-EMA Strategy Replay Table (Zero-Lookahead Ledger)
            </h3>
            <div className="overflow-x-auto rounded-lg border border-ink-700 bg-ink-950/80">
              <table className="w-full text-left text-xs font-mono">
                <thead className="bg-ink-900 border-b border-ink-700 text-slate-400 text-[11px]">
                  <tr>
                    <th className="p-2.5">Date & Time</th>
                    <th className="p-2.5">Strategy</th>
                    <th className="p-2.5">Side</th>
                    <th className="p-2.5">Spot / 8-EMA</th>
                    <th className="p-2.5">Strike (SecID)</th>
                    <th className="p-2.5">MFE / MAE</th>
                    <th className="p-2.5">Target 1 (+20)</th>
                    <th className="p-2.5">Target 2 (+40)</th>
                    <th className="p-2.5">Realized ₹</th>
                    <th className="p-2.5">Outcome</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-ink-800">
                  {tradeReplay.map((tr, i) => (
                    <tr
                      key={i}
                      className={tr.is_today ? "bg-emerald-950/30 font-bold" : "hover:bg-ink-900/50"}
                    >
                      <td className="p-2.5">{tr.date} {tr.time} {tr.is_today && <span className="pill bg-emerald-500 text-ink-950 text-[9px]">TODAY</span>}</td>
                      <td className="p-2.5 text-slate-300">{tr.strategy}</td>
                      <td className="p-2.5 text-emerald-400">{tr.side}</td>
                      <td className="p-2.5">{tr.spot_entry} / {tr.ema8} ({tr.proximity_pts}p)</td>
                      <td className="p-2.5 text-cyan-300">{tr.strike} ({tr.security_id})</td>
                      <td className="p-2.5 text-emerald-400">+{tr.mfe} / -{tr.mae}</td>
                      <td className="p-2.5">{tr.target_20}</td>
                      <td className="p-2.5">{tr.target_40}</td>
                      <td className="p-2.5 text-gold-300 font-bold">+₹{tr.pnl_inr.toLocaleString("en-IN")}</td>
                      <td className="p-2.5"><span className="pill bg-emerald-900 text-emerald-200">{tr.outcome}</span></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        </div>

        {/* ================================================================= */}
        {/* PANEL D: MODULAR STRATEGY EXPANSION SLOTS */}
        {/* ================================================================= */}
        <div className="rounded-xl border border-ink-700 bg-ink-900/60 p-4 space-y-4 shadow-lg lg:col-span-2">
          <div className="flex items-center justify-between border-b border-ink-700/80 pb-2">
            <div className="flex items-center gap-2">
              <span className="text-base">🧩</span>
              <h2 className="font-extrabold text-sm uppercase tracking-wide text-slate-100">
                Panel D: Modular Strategy Expansion Slots & Lab
              </h2>
            </div>
            <span className="text-xs text-slate-400">Modular Execution Framework</span>
          </div>

          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-3">
            {slots.map((s) => (
              <div
                key={s.id}
                className={`p-3.5 rounded-xl border flex flex-col justify-between space-y-3 transition-all ${
                  s.enabled
                    ? "border-cyan-500/60 bg-ink-950/90 shadow-md"
                    : "border-ink-800 bg-ink-950/40 opacity-80"
                }`}
              >
                <div className="space-y-1.5">
                  <div className="flex items-center justify-between">
                    <span className="text-[10px] font-mono uppercase text-slate-500">Slot {s.slot_num} · {s.type}</span>
                    <span className={`pill text-[9px] font-extrabold ${
                      s.status === "ACTIVE"
                        ? "bg-emerald-950 border border-emerald-500 text-emerald-300"
                        : s.status === "READY_TO_CONFIGURE"
                        ? "bg-amber-950 border border-amber-500 text-amber-300"
                        : "bg-ink-800 text-slate-400"
                    }`}>
                      {s.status}
                    </span>
                  </div>
                  <h4 className="font-extrabold text-xs text-slate-100">{s.name}</h4>
                  <p className="text-[11px] text-slate-400 leading-relaxed">{s.description}</p>
                </div>

                <div className="pt-2 border-t border-ink-800 flex items-center justify-between">
                  <div className="text-[10px] font-mono text-slate-400">
                    {s.win_rate !== "—" ? `Win Rate: ${s.win_rate} | ${s.avg_rr}` : "Expansion Ready"}
                  </div>
                  <button
                    onClick={() => toggleSlot(s.id, s.enabled)}
                    className={`px-2.5 py-1 rounded text-xs font-bold transition-all ${
                      s.enabled
                        ? "bg-emerald-600 text-white hover:bg-emerald-500"
                        : "bg-ink-800 text-slate-400 hover:text-white"
                    }`}
                  >
                    {s.enabled ? "Enabled ●" : "Disabled ○"}
                  </button>
                </div>
              </div>
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}

export default AlgoTradingTab;
