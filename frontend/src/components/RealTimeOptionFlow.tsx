import React from 'react';
import { DivergentOIBarItem, StrikeOIClassification, StrikeMigrationState } from '../types/terminal';

interface RealTimeOptionFlowProps {
  chartData: DivergentOIBarItem[];
  tableData: StrikeOIClassification[];
  spotPrice: number;
}

export const RealTimeOptionFlow: React.FC<RealTimeOptionFlowProps> = ({
  chartData,
  tableData,
  spotPrice,
}) => {
  const atmStrike = Math.round(spotPrice / 50) * 50;

  // Compute max absolute OI change for symmetric divergent chart scaling
  const maxOiChg = Math.max(
    ...chartData.map((d) => Math.max(Math.abs(d.callOiChgRaw), Math.abs(d.putOiChgRaw))),
    10000
  );

  const getStateBadgeStyle = (state: StrikeMigrationState) => {
    switch (state) {
      case 'LONG BUILD-UP':
        return 'bg-emerald-500/15 text-emerald-400 border border-emerald-500/30';
      case 'SHORT BUILD-UP':
        return 'bg-rose-500/15 text-rose-400 border border-rose-500/30';
      case 'SHORT COVERING':
        return 'bg-sky-500/15 text-sky-400 border border-sky-500/30';
      case 'LONG UNWINDING':
        return 'bg-amber-500/15 text-amber-400 border border-amber-500/30';
      default:
        return 'text-slate-500';
    }
  };

  return (
    <div className="flex flex-col gap-2">
      {/* 1. Divergent OI Bar Chart */}
      <div className="bg-slate-800/90 border border-slate-700/80 rounded-lg p-3">
        <div className="flex justify-between items-center mb-2 pb-1 border-b border-slate-700/60">
          <span className="text-xs font-bold text-slate-300 uppercase tracking-wider">
            Divergent OI Flow (ATM Centered)
          </span>
          <span className="text-[11px] font-mono text-sky-400 font-bold">
            ATM: {atmStrike} (Spot {spotPrice.toFixed(1)})
          </span>
        </div>

        {/* Legend */}
        <div className="flex justify-between text-[10px] text-slate-400 font-medium px-2 mb-1.5">
          <span className="text-rose-400 font-semibold">◀ CE ΔOI (Calls Written / Sold)</span>
          <span className="text-emerald-400 font-semibold">PE ΔOI (Puts Written / Sold) ▶</span>
        </div>

        {/* Horizontal Divergent Chart Grid */}
        <div className="flex flex-col gap-1.5 font-mono text-[11px]">
          {chartData.map((row) => {
            const isAtm = row.strike === atmStrike;
            const callPct = (Math.abs(row.callOiChgRaw) / maxOiChg) * 100;
            const putPct = (Math.abs(row.putOiChgRaw) / maxOiChg) * 100;

            return (
              <div
                key={row.strike}
                className={`grid grid-cols-12 items-center py-0.5 px-1 rounded ${
                  isAtm ? 'bg-sky-950/40 border border-sky-500/40 font-bold' : 'hover:bg-slate-700/40'
                }`}
              >
                {/* Left Side: Call OI Bar (Negative/Left) */}
                <div className="col-span-5 flex items-center justify-end gap-1.5">
                  <span className="text-[10px] text-slate-400 tabular-nums">
                    {(row.callOiChgRaw / 1000).toFixed(0)}k
                  </span>
                  <div className="w-full bg-slate-900 h-3 rounded-l flex justify-end overflow-hidden">
                    <div
                      className={`h-full ${row.callOiChgRaw > 0 ? 'bg-rose-500' : 'bg-sky-500/60'}`}
                      style={{ width: `${Math.min(callPct, 100)}%` }}
                    />
                  </div>
                </div>

                {/* Center Strike */}
                <div className="col-span-2 text-center text-[11px] tabular-nums font-semibold text-slate-200">
                  {isAtm ? <span className="text-sky-400">★ {row.strike}</span> : row.strike}
                </div>

                {/* Right Side: Put OI Bar (Positive/Right) */}
                <div className="col-span-5 flex items-center justify-start gap-1.5">
                  <div className="w-full bg-slate-900 h-3 rounded-r flex justify-start overflow-hidden">
                    <div
                      className={`h-full ${row.putOiChgRaw > 0 ? 'bg-emerald-500' : 'bg-amber-500/60'}`}
                      style={{ width: `${Math.min(putPct, 100)}%` }}
                    />
                  </div>
                  <span className="text-[10px] text-slate-400 tabular-nums">
                    {(row.putOiChgRaw / 1000).toFixed(0)}k
                  </span>
                </div>
              </div>
            );
          })}
        </div>
      </div>

      {/* 2. Strike Evidence Table */}
      <div className="bg-slate-800/90 border border-slate-700/80 rounded-lg p-3">
        <div className="flex justify-between items-center mb-2 pb-1 border-b border-slate-700/60">
          <span className="text-xs font-bold text-slate-300 uppercase tracking-wider">
            Strike Evidence Table (OI Migration)
          </span>
          <span className="text-[10px] text-slate-400 font-mono">
            Tabular Numerals
          </span>
        </div>

        <div className="overflow-x-auto">
          <table className="w-full text-left font-mono text-[11px] border-collapse">
            <thead>
              <tr className="border-b border-slate-700 text-[10px] text-slate-400 uppercase tracking-wider">
                <th className="py-1 px-1.5 text-right">CE LTP</th>
                <th className="py-1 px-1.5 text-right">CE ΔOI%</th>
                <th className="py-1 px-1.5 text-center font-sans">CE State</th>
                <th className="py-1 px-1.5 text-center text-sky-400">Strike</th>
                <th className="py-1 px-1.5 text-center font-sans">PE State</th>
                <th className="py-1 px-1.5 text-right">PE ΔOI%</th>
                <th className="py-1 px-1.5 text-right">PE LTP</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-800">
              {tableData.map((s) => {
                const isAtm = s.strike === atmStrike;
                return (
                  <tr
                    key={s.strike}
                    className={`${
                      isAtm ? 'bg-sky-950/30 font-bold' : 'hover:bg-slate-700/30'
                    }`}
                  >
                    <td className="py-1 px-1.5 text-right text-slate-200 tabular-nums">
                      {s.ceLtp.toFixed(1)}
                    </td>
                    <td
                      className={`py-1 px-1.5 text-right tabular-nums ${
                        s.ceOiPct > 0 ? 'text-emerald-400' : 'text-rose-400'
                      }`}
                    >
                      {s.ceOiPct > 0 ? `+${s.ceOiPct.toFixed(1)}` : s.ceOiPct.toFixed(1)}%
                    </td>
                    <td className="py-1 px-1.5 text-center">
                      <span
                        className={`text-[9px] font-sans font-bold px-1.5 py-0.5 rounded ${getStateBadgeStyle(
                          s.ceState
                        )}`}
                      >
                        {s.ceState}
                      </span>
                    </td>
                    <td className="py-1 px-1.5 text-center text-slate-100 tabular-nums font-bold">
                      {isAtm ? <span className="text-sky-400">★ {s.strike}</span> : s.strike}
                    </td>
                    <td className="py-1 px-1.5 text-center">
                      <span
                        className={`text-[9px] font-sans font-bold px-1.5 py-0.5 rounded ${getStateBadgeStyle(
                          s.peState
                        )}`}
                      >
                        {s.peState}
                      </span>
                    </td>
                    <td
                      className={`py-1 px-1.5 text-right tabular-nums ${
                        s.peOiPct > 0 ? 'text-emerald-400' : 'text-rose-400'
                      }`}
                    >
                      {s.peOiPct > 0 ? `+${s.peOiPct.toFixed(1)}` : s.peOiPct.toFixed(1)}%
                    </td>
                    <td className="py-1 px-1.5 text-right text-slate-200 tabular-nums">
                      {s.peLtp.toFixed(1)}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
};
