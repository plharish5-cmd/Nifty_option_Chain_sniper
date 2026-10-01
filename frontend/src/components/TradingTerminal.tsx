import React, { useState } from 'react';
import { TerminalSnapshot } from '../types/terminal';
import { mockSnapshot } from '../mockData';
import { DecisionCockpit } from './DecisionCockpit';
import { RealTimeOptionFlow } from './RealTimeOptionFlow';
import { SMCPriceActionCanvas } from './SMCPriceActionCanvas';
import { ActiveSetupsFeed } from './ActiveSetupsFeed';
import { ZoneMatrix } from './ZoneMatrix';

interface TradingTerminalProps {
  initialSnapshot?: TerminalSnapshot;
}

export const TradingTerminal: React.FC<TradingTerminalProps> = ({
  initialSnapshot = mockSnapshot,
}) => {
  const [snapshot] = useState<TerminalSnapshot>(initialSnapshot);
  const [activeSymbol, setActiveSymbol] = useState<'NIFTY' | 'BANKNIFTY'>('NIFTY');
  const [activeTab, setActiveTab] = useState<'TERMINAL' | 'SMC_RAW' | 'GEX_FLOW'>('TERMINAL');

  return (
    <div className="min-h-screen bg-slate-900 text-slate-200 font-sans p-3 flex flex-col gap-3 selection:bg-emerald-500/30 selection:text-emerald-200">
      {/* Institutional Top Control & Status Bar */}
      <header className="flex flex-wrap items-center justify-between gap-3 px-3 py-2 bg-slate-800/90 border border-slate-700/80 rounded-lg shadow-sm">
        <div className="flex items-center gap-3">
          <div className="flex items-center gap-2">
            <span className="w-2.5 h-2.5 rounded-full bg-emerald-400 animate-pulse" />
            <h1 className="text-sm font-black tracking-wider uppercase text-white font-mono flex items-center gap-1.5">
              <span>QUANT.SNIPER</span>
              <span className="text-[10px] px-1.5 py-0.5 rounded bg-slate-700 text-slate-300 font-normal">
                v2.5 PRO
              </span>
            </h1>
          </div>

          <div className="h-4 w-px bg-slate-700" />

          {/* Symbol Selectors */}
          <div className="flex items-center gap-1 bg-slate-900/80 p-0.5 rounded border border-slate-700">
            {(['NIFTY', 'BANKNIFTY'] as const).map((sym) => (
              <button
                key={sym}
                onClick={() => setActiveSymbol(sym)}
                className={`px-2.5 py-0.5 text-xs font-mono font-bold rounded transition-colors ${
                  activeSymbol === sym
                    ? 'bg-slate-700 text-emerald-400 shadow-sm'
                    : 'text-slate-400 hover:text-slate-200'
                }`}
              >
                {sym}
              </button>
            ))}
          </div>

          <div className="h-4 w-px bg-slate-700" />

          {/* View Mode Switches */}
          <div className="hidden sm:flex items-center gap-1">
            <button
              onClick={() => setActiveTab('TERMINAL')}
              className={`px-2 py-0.5 text-[11px] font-semibold rounded ${
                activeTab === 'TERMINAL'
                  ? 'bg-slate-700/80 text-white'
                  : 'text-slate-400 hover:text-slate-200'
              }`}
            >
              Unified Terminal
            </button>
            <button
              onClick={() => setActiveTab('SMC_RAW')}
              className={`px-2 py-0.5 text-[11px] font-semibold rounded ${
                activeTab === 'SMC_RAW'
                  ? 'bg-slate-700/80 text-white'
                  : 'text-slate-400 hover:text-slate-200'
              }`}
            >
              SMC Raw
            </button>
            <button
              onClick={() => setActiveTab('GEX_FLOW')}
              className={`px-2 py-0.5 text-[11px] font-semibold rounded ${
                activeTab === 'GEX_FLOW'
                  ? 'bg-slate-700/80 text-white'
                  : 'text-slate-400 hover:text-slate-200'
              }`}
            >
              Gamma Flow
            </button>
          </div>
        </div>

        {/* Real-time Status Badges */}
        <div className="flex items-center gap-4 text-xs font-mono">
          <div className="flex items-center gap-1.5">
            <span className="text-slate-400">SPOT:</span>
            <span className="font-bold text-white tabular-nums tracking-tight text-sm">
              {snapshot.spotPrice.toLocaleString('en-IN', {
                minimumFractionDigits: 2,
                maximumFractionDigits: 2,
              })}
            </span>
          </div>

          <div className="hidden md:flex items-center gap-1.5">
            <span className="text-slate-400">DEALER GEX:</span>
            <span
              className={`font-bold tabular-nums ${
                snapshot.gammaProfile.netGexCrores >= 0
                  ? 'text-emerald-400'
                  : 'text-rose-400'
              }`}
            >
              {snapshot.gammaProfile.netGexCrores > 0 ? '+' : ''}
              {snapshot.gammaProfile.netGexCrores.toFixed(1)} Cr (
              {snapshot.gammaProfile.dealerPosition.replace('_', ' ')})
            </span>
          </div>

          <div className="hidden lg:flex items-center gap-1.5">
            <span className="text-slate-400">REGIME:</span>
            <span className="px-1.5 py-0.5 rounded bg-slate-700/70 text-slate-200 font-semibold text-[10.5px]">
              {snapshot.gammaProfile.currentRegime.replace(/_/g, ' ')}
            </span>
          </div>

          <div className="flex items-center gap-1.5 text-slate-400 text-[11px]">
            <span className="w-1.5 h-1.5 rounded-full bg-emerald-400" />
            <span>T: {snapshot.timestamp}</span>
          </div>
        </div>
      </header>

      {/* Decision Cockpit: Institutional Scorecard & Algo Playbook Directive */}
      <section>
        <DecisionCockpit
          report={snapshot.fourPillars}
          spotPrice={snapshot.spotPrice}
          dealingRange={snapshot.dealingRange}
        />
      </section>

      {/* Primary Workspace: 2-Column Responsive Layout */}
      <div className="grid grid-cols-1 lg:grid-cols-12 gap-3 flex-1">
        {/* Left Column: Real-Time Options Flow (Divergent OI Bar Chart + Strike Evidence Table) */}
        <section className="lg:col-span-5 flex flex-col">
          <RealTimeOptionFlow
            chartData={snapshot.divergentOiChart}
            tableData={snapshot.strikeEvidenceTable}
            spotPrice={snapshot.spotPrice}
          />
        </section>

        {/* Right Column: SMC Canvas & Confluence Analytics */}
        <section className="lg:col-span-7 flex flex-col gap-3">
          {/* SMC Price Action & Confluence Canvas */}
          <SMCPriceActionCanvas
            spotPrice={snapshot.spotPrice}
            dealingRange={snapshot.dealingRange}
            gammaProfile={snapshot.gammaProfile}
          />

          {/* Bottom Dual Grid: Active Setups Feed & Zone Matrix */}
          <div className="grid grid-cols-1 md:grid-cols-2 gap-3 flex-1">
            {/* Active Setup Cards (Setup / Logic / Action) */}
            <div className="flex flex-col min-h-[360px]">
              <ActiveSetupsFeed setups={snapshot.activeSetups} />
            </div>

            {/* Zone Matrix (Confirmed / Contested / Weakening S/R Confluence) */}
            <div className="flex flex-col min-h-[360px]">
              <ZoneMatrix matrix={snapshot.zoneMatrix} />
            </div>
          </div>
        </section>
      </div>

      {/* Institutional Micro Footer */}
      <footer className="flex items-center justify-between text-[10px] text-slate-300 font-mono px-3 py-1.5 border-t border-slate-800">
        <div className="flex items-center gap-3">
          <span>LATENCY: 14ms</span>
          <span>DATA FEED: NSE REALTIME TICK</span>
          <span>SMC ENGINE: ZERO-LOOKAHEAD FRACTAL PIVOTS</span>
        </div>
        <div>
          <span>INSTITUTIONAL EXECUTION PROTOCOL &bull; NO LOOKAHEAD BIAS</span>
        </div>
      </footer>
    </div>
  );
};

export default TradingTerminal;
