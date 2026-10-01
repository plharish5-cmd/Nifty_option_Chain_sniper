import React from 'react';
import { FourPillarsReport, DealingRange } from '../types/terminal';

interface DecisionCockpitProps {
  report: FourPillarsReport;
  spotPrice: number;
  dealingRange?: DealingRange;
}

export const DecisionCockpit: React.FC<DecisionCockpitProps> = ({
  report,
  spotPrice,
  dealingRange,
}) => {
  const isBullish = report.compositeScore >= 1.0;
  const isBearish = report.compositeScore <= -1.0;

  const scoreColor = isBullish
    ? 'text-emerald-400'
    : isBearish
    ? 'text-rose-400'
    : 'text-slate-400';

  const badgeBorder = isBullish
    ? 'border-emerald-500/40 bg-emerald-500/10'
    : isBearish
    ? 'border-rose-500/40 bg-rose-500/10'
    : 'border-slate-700 bg-slate-800/40';

  return (
    <header className="w-full bg-slate-900 border border-slate-800 rounded-lg p-3 mb-2 shadow-2xl">
      {/* Top row: 4 Pillars Scorecard + Composite Bias Badge */}
      <div className="grid grid-cols-1 md:grid-cols-5 gap-2.5 items-center">
        {/* Four Pillars */}
        {report.pillars.map((pillar) => {
          const pillColor =
            pillar.statusColor === 'EMERALD'
              ? 'bg-emerald-500/15 text-emerald-400 border-emerald-500/30'
              : pillar.statusColor === 'ROSE'
              ? 'bg-rose-500/15 text-rose-400 border-rose-500/30'
              : 'bg-amber-500/15 text-amber-400 border-amber-500/30';

          return (
            <div
              key={pillar.name}
              className="bg-slate-800/80 border border-slate-700/80 rounded-md p-2.5 flex flex-col justify-between"
            >
              <div className="flex justify-between items-center mb-1">
                <span className="text-[11px] font-semibold text-slate-400 uppercase tracking-wider">
                  {pillar.title}
                </span>
                <span
                  className={`text-[9.5px] font-bold px-1.5 py-0.5 rounded border ${pillColor}`}
                >
                  {pillar.regime}
                </span>
              </div>
              <div className="text-base font-bold text-slate-100 font-mono tabular-nums">
                {pillar.rawMetric}
              </div>
              <div className="text-[10px] text-slate-400 truncate mt-0.5">
                {pillar.detail}
              </div>
            </div>
          );
        })}

        {/* Composite Bias Score Badge */}
        <div
          className={`border rounded-md p-2.5 flex flex-col items-center justify-center text-center ${badgeBorder}`}
        >
          <span className="text-[10px] font-bold uppercase tracking-wider text-slate-400">
            COMPOSITE BIAS
          </span>
          <div className={`text-2xl font-extrabold font-mono tabular-nums ${scoreColor}`}>
            {report.compositeScore > 0 ? `+${report.compositeScore.toFixed(1)}` : report.compositeScore.toFixed(1)} / 4.0
          </div>
          <span className={`text-[11px] font-bold ${scoreColor}`}>
            {report.biasLabel}
          </span>
          {dealingRange && (
            <div className="text-[9.5px] text-slate-400 font-mono mt-0.5">
              EQ: <span className="text-slate-200 font-semibold">{dealingRange.equilibrium.toLocaleString()}</span> (
              <span className={dealingRange.currentRegime === 'PREMIUM' ? 'text-rose-400' : 'text-emerald-400'}>
                {dealingRange.currentRegime}
              </span>
              )
            </div>
          )}
        </div>
      </div>

      {/* Bottom row: Dynamic Algo Playbook Directive */}
      <div className="mt-2.5 bg-slate-950/90 border-l-4 border-l-sky-400 border border-slate-800 rounded-r-md px-3 py-2 flex items-center gap-3">
        <span className="bg-sky-600 text-white text-[10px] font-bold uppercase tracking-wider px-2 py-0.5 rounded">
          ALGO PLAYBOOK
        </span>
        <p className="text-xs text-slate-200 font-medium tracking-wide">
          {report.algoDirective}
        </p>
      </div>
    </header>
  );
};
