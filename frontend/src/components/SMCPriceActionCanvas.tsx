import React from 'react';
import { DealingRange, GammaProfile } from '../types/terminal';

interface SMCPriceActionCanvasProps {
  spotPrice: number;
  dealingRange?: DealingRange;
  gammaProfile: GammaProfile;
}

export const SMCPriceActionCanvas: React.FC<SMCPriceActionCanvasProps> = ({
  spotPrice,
  dealingRange,
  gammaProfile,
}) => {
  return (
    <div className="bg-slate-800/90 border border-slate-700/80 rounded-lg p-3">
      <div className="flex justify-between items-center mb-2 pb-1 border-b border-slate-700/60">
        <div className="flex items-center gap-2">
          <span className="text-xs font-bold text-slate-300 uppercase tracking-wider">
            SMC & Price Action Canvas (Confluence Architecture)
          </span>
          <span className="text-[9.5px] bg-emerald-500/10 text-emerald-400 font-mono px-1.5 py-0.5 rounded border border-emerald-500/30">
            SMC &bull; GEX &bull; WALLS
          </span>
        </div>
        <div className="flex items-center gap-3 font-mono text-[11px]">
          <span className="text-rose-400 font-semibold">CE Wall: {gammaProfile.callWallStrike}</span>
          <span className="text-emerald-400 font-semibold">PE Wall: {gammaProfile.putWallStrike}</span>
          <span className="text-sky-400 font-semibold">Zero Gamma: {gammaProfile.zeroGammaLevel}</span>
        </div>
      </div>

      {/* Visual Alignment Flow Diagram */}
      <div className="w-full bg-slate-950 border border-slate-800 rounded-md p-3 relative flex flex-col gap-2 font-mono">
        {/* Upper Resistance Band */}
        <div className="border border-rose-500/30 bg-rose-500/10 rounded p-2 flex justify-between items-center text-xs">
          <div className="flex items-center gap-2">
            <span className="px-1.5 py-0.5 bg-rose-500/20 text-rose-400 rounded text-[10px] font-bold">
              CE WALL + BREAKER
            </span>
            <span className="text-slate-200 font-bold">{gammaProfile.callWallStrike}</span>
            <span className="text-[10px] text-slate-400 font-sans">
              15m Bearish Breaker [24,280 - 24,310] + Unwinding Call Barrier
            </span>
          </div>
          <span className="text-[10px] font-bold text-emerald-400 bg-emerald-500/10 px-2 py-0.5 rounded border border-emerald-500/20">
            SMC &amp; OI AGREE (HEAVY CEILING)
          </span>
        </div>

        {/* Dragon Fruit Overlap Zone */}
        <div className="border border-amber-500/30 bg-amber-500/10 rounded p-2 flex justify-between items-center text-xs">
          <div className="flex items-center gap-2">
            <span className="px-1.5 py-0.5 bg-amber-500/20 text-amber-400 rounded text-[10px] font-bold">
              DRAGON FRUIT COMBO
            </span>
            <span className="text-slate-200 font-bold">24,215</span>
            <span className="text-[10px] text-slate-400 font-sans">
              15m Imbalance (FVG) + Bearish OB Overlap + 24200 CE Writing
            </span>
          </div>
          <span className="text-[10px] font-bold text-amber-400 bg-amber-500/10 px-2 py-0.5 rounded border border-amber-500/20">
            HIGH CONVICTION SNIPER SHORT
          </span>
        </div>

        {/* Current Spot Active Cursor */}
        <div className="border border-sky-500/60 bg-sky-500/10 rounded p-2.5 flex justify-between items-center text-xs my-0.5 shadow-sm shadow-sky-500/20">
          <div className="flex items-center gap-2">
            <span className="w-2.5 h-2.5 rounded-full bg-sky-400 animate-pulse" />
            <span className="text-sky-300 font-bold text-sm">SPOT: {spotPrice.toFixed(1)}</span>
            <span className="text-[10.5px] text-slate-400 font-sans">
              Dealing Range Depth: {dealingRange?.discountDepthPct.toFixed(1)}% ({dealingRange?.currentRegime})
            </span>
          </div>
          <div className="text-[10px] font-mono text-slate-300">
            Zero Gamma Flip: <span className="text-sky-400 font-bold">{gammaProfile.zeroGammaLevel}</span> (Pinning)
          </div>
        </div>

        {/* 50% Dealing Range Equilibrium */}
        {dealingRange && (
          <div className="border border-slate-700 bg-slate-900/60 rounded p-1.5 flex justify-between items-center text-[11px] text-slate-400">
            <div className="flex items-center gap-2">
              <span className="px-1 py-0.5 bg-slate-800 text-slate-300 rounded text-[9px] font-bold">
                50% EQ
              </span>
              <span className="text-slate-300 font-bold">{dealingRange.equilibrium.toLocaleString()}</span>
              <span className="text-[10px] font-sans">
                Fair Value Boundary (Premium &gt; {dealingRange.equilibrium} | Discount &lt; {dealingRange.equilibrium})
              </span>
            </div>
            <span className="text-[10px] font-mono text-slate-400">
              OTE 70.5%: {dealingRange.ote705.toFixed(1)}
            </span>
          </div>
        )}

        {/* Lower Support Band */}
        <div className="border border-emerald-500/30 bg-emerald-500/10 rounded p-2 flex justify-between items-center text-xs">
          <div className="flex items-center gap-2">
            <span className="px-1.5 py-0.5 bg-emerald-500/20 text-emerald-400 rounded text-[10px] font-bold">
              PE WALL + DEMAND OB
            </span>
            <span className="text-slate-200 font-bold">{gammaProfile.putWallStrike}</span>
            <span className="text-[10px] text-slate-400 font-sans">
              15m Bullish Demand OB [23,980 - 24,020] + 16.8L Put Writing Floor
            </span>
          </div>
          <span className="text-[10px] font-bold text-emerald-400 bg-emerald-500/10 px-2 py-0.5 rounded border border-emerald-500/20">
            SMC &amp; OI AGREE (ANCHOR FLOOR)
          </span>
        </div>
      </div>
    </div>
  );
};
