import React from 'react';
import { ActiveSetupCard } from '../types/terminal';

interface ActiveSetupsFeedProps {
  setups: ActiveSetupCard[];
}

export const ActiveSetupsFeed: React.FC<ActiveSetupsFeedProps> = ({ setups }) => {
  return (
    <div className="bg-slate-800/90 border border-slate-700/80 rounded-lg p-3 h-full flex flex-col">
      <div className="flex justify-between items-center mb-2 pb-1 border-b border-slate-700/60">
        <span className="text-xs font-bold text-slate-300 uppercase tracking-wider">
          Active Setups Feed (SMC + OI Convergence)
        </span>
        <span className="text-[10px] bg-sky-500/20 text-sky-400 font-bold px-2 py-0.5 rounded border border-sky-500/30">
          {setups.length} ARMED
        </span>
      </div>

      <div className="flex flex-col gap-2.5 overflow-y-auto pr-0.5">
        {setups.map((setup, idx) => {
          const isBearish = setup.direction === 'BEARISH';
          const isBullish = setup.direction === 'BULLISH';

          const cardBorder = isBearish
            ? 'border-l-4 border-l-rose-500 border border-slate-700/80 bg-slate-900/90'
            : isBullish
            ? 'border-l-4 border-l-emerald-500 border border-slate-700/80 bg-slate-900/90'
            : 'border-l-4 border-l-sky-500 border border-slate-700/80 bg-slate-900/90';

          const dirBadge = isBearish
            ? 'bg-rose-500/15 text-rose-400 border-rose-500/30'
            : isBullish
            ? 'bg-emerald-500/15 text-emerald-400 border-emerald-500/30'
            : 'bg-sky-500/15 text-sky-400 border-sky-500/30';

          return (
            <div key={idx} className={`p-2.5 rounded-r-md ${cardBorder}`}>
              {/* Card Header */}
              <div className="flex justify-between items-center mb-1">
                <div className="flex items-center gap-2">
                  <span
                    className={`w-2 h-2 rounded-full ${
                      isBearish ? 'bg-rose-400' : isBullish ? 'bg-emerald-400' : 'bg-sky-400'
                    }`}
                  />
                  <h4 className="text-xs font-bold text-slate-100">{setup.setupName}</h4>
                </div>
                <div className="flex items-center gap-1.5">
                  <span className="text-[9.5px] font-mono font-semibold px-1.5 py-0.5 rounded bg-slate-800 text-slate-300 border border-slate-700">
                    {setup.timeframe}
                  </span>
                  <span className={`text-[9.5px] font-bold px-1.5 py-0.5 rounded border ${dirBadge}`}>
                    {setup.conviction}
                  </span>
                </div>
              </div>

              {/* Exact format required */}
              <div className="text-[11.5px] text-slate-300 mt-1 leading-relaxed">
                <span className="font-bold text-slate-400 uppercase text-[10.5px]">Logic:</span>{' '}
                {setup.logic}
              </div>
              <div className="text-[11.5px] text-slate-200 mt-1 leading-relaxed">
                <span className="font-bold text-sky-400 uppercase text-[10.5px]">Action:</span>{' '}
                <span className="font-medium text-sky-200">{setup.action}</span>
              </div>

              {/* Numerical Parameters Row */}
              <div className="grid grid-cols-4 gap-1 mt-2 pt-1.5 border-t border-slate-800 font-mono text-[10.5px] text-slate-400">
                <div>
                  Entry:{' '}
                  <span className="font-bold text-slate-100 tabular-nums">
                    {setup.triggerLevel.toLocaleString()}
                  </span>
                </div>
                <div>
                  Stop:{' '}
                  <span className="font-bold text-rose-400 tabular-nums">
                    {setup.stopLoss.toLocaleString()}
                  </span>
                </div>
                <div>
                  Target:{' '}
                  <span className="font-bold text-emerald-400 tabular-nums">
                    {setup.targetLevel.toLocaleString()}
                  </span>
                </div>
                <div className="text-right">
                  R:R:{' '}
                  <span className="font-bold text-amber-400 tabular-nums">
                    {setup.riskRewardRatio}
                  </span>
                </div>
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
};
