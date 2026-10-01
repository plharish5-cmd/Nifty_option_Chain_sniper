import React from 'react';
import { ZoneMatrixRow, ZoneAlignmentStatus } from '../types/terminal';

interface ZoneMatrixProps {
  matrix: ZoneMatrixRow[];
}

export const ZoneMatrix: React.FC<ZoneMatrixProps> = ({ matrix }) => {
  const getStatusBadge = (status: ZoneAlignmentStatus) => {
    switch (status) {
      case 'CONFIRMED':
        return 'bg-emerald-500/15 text-emerald-400 border-emerald-500/40';
      case 'CONTESTED':
        return 'bg-amber-500/15 text-amber-400 border-amber-500/40';
      case 'WEAKENING':
        return 'bg-rose-500/15 text-rose-400 border-rose-500/40';
    }
  };

  return (
    <div className="bg-slate-800/90 border border-slate-700/80 rounded-lg p-3 h-full flex flex-col">
      <div className="flex justify-between items-center mb-2 pb-1 border-b border-slate-700/60">
        <span className="text-xs font-bold text-slate-300 uppercase tracking-wider">
          Zone Matrix (Liquidity Pools & Option S/R)
        </span>
        <span className="text-[10px] text-amber-400 font-semibold">
          DEALER CONFLUENCE
        </span>
      </div>

      <div className="flex flex-col gap-2 overflow-y-auto pr-0.5">
        {matrix.map((row, idx) => {
          const isResistance = row.zoneType === 'RESISTANCE';
          const isSupport = row.zoneType === 'SUPPORT';

          const levelColor = isResistance
            ? 'text-rose-400'
            : isSupport
            ? 'text-emerald-400'
            : 'text-amber-400';

          return (
            <div
              key={idx}
              className={`p-2 rounded border bg-slate-900/80 ${
                row.isImmediate
                  ? 'border-sky-500/50 shadow-sm shadow-sky-500/10'
                  : 'border-slate-700/70'
              }`}
            >
              <div className="flex justify-between items-start">
                <div className="flex items-center gap-2">
                  <span className={`text-sm font-bold font-mono tabular-nums ${levelColor}`}>
                    {row.level.toLocaleString()}
                  </span>
                  <span className="text-xs font-semibold text-slate-200">
                    {row.label}
                  </span>
                  <span
                    className={`text-[9px] font-bold uppercase tracking-wider px-1.5 py-0.5 rounded border ${getStatusBadge(
                      row.alignment
                    )}`}
                  >
                    {row.alignment}
                  </span>
                </div>

                <div className="text-right font-mono text-[10.5px]">
                  <span
                    className={`font-semibold tabular-nums ${
                      row.isImmediate ? 'text-sky-400' : 'text-slate-400'
                    }`}
                  >
                    {row.distancePts > 0 ? `+${row.distancePts.toFixed(0)}` : row.distancePts.toFixed(0)} pts
                  </span>
                  <div className="text-[9px] text-slate-400 uppercase tracking-wider">
                    {row.zoneType}
                  </div>
                </div>
              </div>

              {/* Evidence lines */}
              <div className="mt-1 text-[11px] text-slate-400 leading-tight">
                <span className="text-slate-300 font-medium">OI Evidence:</span> {row.oiEvidence}
              </div>
              <div className="text-[11px] text-slate-400 leading-tight">
                <span className="text-slate-300 font-medium">SMC Evidence:</span> {row.smcEvidence}
              </div>
              <div className="text-[10px] text-slate-400 mt-1 italic">
                ↳ {row.alignmentReason}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
};
