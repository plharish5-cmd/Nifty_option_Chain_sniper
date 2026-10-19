import React, { useEffect, useRef } from 'react';
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
  const canvasRef = useRef<HTMLCanvasElement | null>(null);

  // Decoupled mutable state ref: prevents React state-mutation lag during high-frequency ticks
  const stateRef = useRef({
    spotPrice,
    targetSpot: spotPrice,
    animSpot: spotPrice,
    dealingRange,
    gammaProfile,
    lastTime: performance.now(),
    fps: 60,
    isDirty: true,
  });

  // Cached dimensions and DPR from ResizeObserver: zero DOM layout queries inside the 60 FPS RAF loop
  const dimsRef = useRef({ w: 0, h: 0, dpr: 1 });

  // Update target values in mutable ref with zero React re-render overhead; mark canvas dirty
  useEffect(() => {
    stateRef.current.targetSpot = spotPrice;
    stateRef.current.dealingRange = dealingRange;
    stateRef.current.gammaProfile = gammaProfile;
    stateRef.current.isDirty = true;
  }, [spotPrice, dealingRange, gammaProfile]);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext('2d');
    if (!ctx) return;

    let animId: number;

    // Zero-query ResizeObserver: caches canvas width, height, and DPR
    const observer = new ResizeObserver((entries) => {
      const entry = entries[0];
      if (!entry) return;
      const rect = entry.contentRect;
      const dpr = window.devicePixelRatio || 1;
      const w = Math.floor(rect.width);
      const h = Math.floor(rect.height);
      dimsRef.current = { w, h, dpr };
      canvas.width = Math.floor(w * dpr);
      canvas.height = Math.floor(h * dpr);
      stateRef.current.isDirty = true;
    });

    observer.observe(canvas);

    // Initial dimension seed
    const initialRect = canvas.getBoundingClientRect();
    const initDpr = window.devicePixelRatio || 1;
    dimsRef.current = {
      w: Math.floor(initialRect.width),
      h: Math.floor(initialRect.height),
      dpr: initDpr,
    };
    canvas.width = Math.floor(initialRect.width * initDpr);
    canvas.height = Math.floor(initialRect.height * initDpr);

    // 60 FPS requestAnimationFrame loop with dirty-flag & LERP convergence check
    const render = (time: number) => {
      const s = stateRef.current;
      const spotDiff = Math.abs(s.targetSpot - s.animSpot);
      const isMoving = spotDiff >= 0.05;

      if (isMoving) {
        // Smooth LERP (linear interpolation) towards target spot price
        s.animSpot += (s.targetSpot - s.animSpot) * 0.14;
        s.isDirty = true;
      } else if (s.animSpot !== s.targetSpot) {
        // Snap to target on convergence
        s.animSpot = s.targetSpot;
        s.isDirty = true;
      }

      // Dirty-flag optimization: skip redraw frames entirely when price and zones are stationary
      if (!s.isDirty && !isMoving) {
        animId = requestAnimationFrame(render);
        return;
      }
      s.isDirty = false;

      // Use cached dimensions from ResizeObserver (zero DOM queries)
      const { w, h, dpr } = dimsRef.current;
      if (w <= 0 || h <= 0) {
        animId = requestAnimationFrame(render);
        return;
      }

      const dt = Math.max(1, time - s.lastTime);
      s.lastTime = time;
      s.fps = Math.round(1000 / dt);

      ctx.save();
      ctx.scale(dpr, dpr);

      // Background: Deep institutional dark theme
      ctx.fillStyle = '#060a12';
      ctx.fillRect(0, 0, w, h);

      // Ambient gradient
      const bgGrad = ctx.createLinearGradient(0, 0, 0, h);
      bgGrad.addColorStop(0, 'rgba(15, 23, 42, 0.6)');
      bgGrad.addColorStop(0.5, 'rgba(2, 6, 23, 0.4)');
      bgGrad.addColorStop(1, 'rgba(15, 23, 42, 0.6)');
      ctx.fillStyle = bgGrad;
      ctx.fillRect(0, 0, w, h);

      // Determine price coordinate scale
      const callWall = s.gammaProfile.callWallStrike || 24300;
      const putWall = s.gammaProfile.putWallStrike || 24000;
      const highP = Math.max(callWall + 60, s.dealingRange?.high || callWall + 50);
      const lowP = Math.min(putWall - 60, s.dealingRange?.low || putWall - 50);
      const span = Math.max(1, highP - lowP);

      const toY = (price: number) => {
        const norm = (highP - price) / span;
        return 20 + norm * (h - 40);
      };

      // Draw horizontal grid lines
      ctx.strokeStyle = 'rgba(51, 65, 85, 0.25)';
      ctx.lineWidth = 1;
      ctx.setLineDash([3, 4]);
      for (let p = Math.ceil(lowP / 50) * 50; p <= highP; p += 50) {
        const y = toY(p);
        ctx.beginPath();
        ctx.moveTo(40, y);
        ctx.lineTo(w - 20, y);
        ctx.stroke();

        ctx.fillStyle = 'rgba(100, 116, 139, 0.6)';
        ctx.font = '9px monospace';
        ctx.textAlign = 'left';
        ctx.fillText(`${p}`, 8, y + 3);
      }
      ctx.setLineDash([]);

      // 1. CE Wall + Breaker Block Resistance Zone
      const ceY = toY(callWall);
      const ceH = 34;
      const ceGrad = ctx.createLinearGradient(0, ceY - ceH / 2, 0, ceY + ceH / 2);
      ceGrad.addColorStop(0, 'rgba(244, 63, 94, 0.22)');
      ceGrad.addColorStop(1, 'rgba(244, 63, 94, 0.08)');
      ctx.fillStyle = ceGrad;
      ctx.fillRect(50, ceY - ceH / 2, w - 70, ceH);
      ctx.strokeStyle = 'rgba(244, 63, 94, 0.55)';
      ctx.lineWidth = 1;
      ctx.strokeRect(50, ceY - ceH / 2, w - 70, ceH);

      ctx.fillStyle = '#fda4af';
      ctx.font = 'bold 10px monospace';
      ctx.textAlign = 'left';
      ctx.fillText(`CE WALL + BREAKER  [${callWall}]`, 60, ceY - 3);
      ctx.fillStyle = 'rgba(203, 213, 225, 0.8)';
      ctx.font = '9px sans-serif';
      ctx.fillText('15m Bearish Breaker + Unwinding Call Barrier', 60, ceY + 10);
      ctx.fillStyle = '#34d399';
      ctx.textAlign = 'right';
      ctx.font = 'bold 9px monospace';
      ctx.fillText('SMC & OI AGREE (HEAVY CEILING)', w - 30, ceY + 3);

      // 2. Dragon Fruit Overlap Zone (FVG + OB)
      const dfPrice = 24215;
      if (dfPrice <= highP && dfPrice >= lowP) {
        const dfY = toY(dfPrice);
        const dfH = 26;
        ctx.fillStyle = 'rgba(245, 158, 11, 0.16)';
        ctx.fillRect(50, dfY - dfH / 2, w - 70, dfH);
        ctx.strokeStyle = 'rgba(245, 158, 11, 0.45)';
        ctx.strokeRect(50, dfY - dfH / 2, w - 70, dfH);

        ctx.fillStyle = '#fde68a';
        ctx.font = 'bold 9.5px monospace';
        ctx.textAlign = 'left';
        ctx.fillText(`DRAGON FRUIT COMBO  [${dfPrice}]`, 60, dfY - 1);
        ctx.fillStyle = 'rgba(226, 232, 240, 0.75)';
        ctx.font = '8.5px sans-serif';
        ctx.fillText('15m Imbalance (FVG) + Bearish OB Overlap', 60, dfY + 9);
        ctx.fillStyle = '#fbbf24';
        ctx.textAlign = 'right';
        ctx.font = 'bold 9px monospace';
        ctx.fillText('SNIPER SHORT CONVICTION', w - 30, dfY + 3);
      }

      // 3. 50% Dealing Range Equilibrium & OTE Golden Pocket
      if (s.dealingRange) {
        const eqY = toY(s.dealingRange.equilibrium);
        ctx.strokeStyle = 'rgba(148, 163, 184, 0.7)';
        ctx.lineWidth = 1;
        ctx.setLineDash([4, 4]);
        ctx.beginPath();
        ctx.moveTo(50, eqY);
        ctx.lineTo(w - 20, eqY);
        ctx.stroke();
        ctx.setLineDash([]);

        ctx.fillStyle = '#cbd5e1';
        ctx.font = 'bold 9px monospace';
        ctx.textAlign = 'left';
        ctx.fillText(`50% EQ: ${s.dealingRange.equilibrium.toLocaleString()}`, 60, eqY - 4);
        ctx.textAlign = 'right';
        ctx.fillStyle = '#94a3b8';
        ctx.fillText(`OTE 70.5%: ${s.dealingRange.ote705.toFixed(1)}`, w - 30, eqY - 4);

        // OTE Band shading
        if (s.dealingRange.ote790 && s.dealingRange.ote705) {
          const oteY1 = toY(s.dealingRange.ote705);
          const oteY2 = toY(s.dealingRange.ote790);
          ctx.fillStyle = 'rgba(56, 189, 248, 0.08)';
          ctx.fillRect(50, Math.min(oteY1, oteY2), w - 70, Math.abs(oteY2 - oteY1));
        }
      }

      // 4. Zero Gamma Level
      if (s.gammaProfile.zeroGammaLevel) {
        const zgY = toY(s.gammaProfile.zeroGammaLevel);
        ctx.strokeStyle = 'rgba(56, 189, 248, 0.75)';
        ctx.lineWidth = 1;
        ctx.setLineDash([2, 3]);
        ctx.beginPath();
        ctx.moveTo(50, zgY);
        ctx.lineTo(w - 20, zgY);
        ctx.stroke();
        ctx.setLineDash([]);

        ctx.fillStyle = '#38bdf8';
        ctx.font = 'bold 9px monospace';
        ctx.textAlign = 'right';
        ctx.fillText(`ZERO GAMMA FLIP: ${s.gammaProfile.zeroGammaLevel} (Pinning)`, w - 30, zgY + 11);
      }

      // 5. PE Wall + Demand OB Support Floor
      const peY = toY(putWall);
      const peH = 34;
      const peGrad = ctx.createLinearGradient(0, peY - peH / 2, 0, peY + peH / 2);
      peGrad.addColorStop(0, 'rgba(16, 185, 129, 0.1)');
      peGrad.addColorStop(1, 'rgba(16, 185, 129, 0.22)');
      ctx.fillStyle = peGrad;
      ctx.fillRect(50, peY - peH / 2, w - 70, peH);
      ctx.strokeStyle = 'rgba(16, 185, 129, 0.55)';
      ctx.lineWidth = 1;
      ctx.strokeRect(50, peY - peH / 2, w - 70, peH);

      ctx.fillStyle = '#6ee7b7';
      ctx.font = 'bold 10px monospace';
      ctx.textAlign = 'left';
      ctx.fillText(`PE WALL + DEMAND OB  [${putWall}]`, 60, peY - 3);
      ctx.fillStyle = 'rgba(203, 213, 225, 0.8)';
      ctx.font = '9px sans-serif';
      ctx.fillText('15m Bullish Demand OB + Put Writing Floor', 60, peY + 10);
      ctx.fillStyle = '#34d399';
      ctx.textAlign = 'right';
      ctx.font = 'bold 9px monospace';
      ctx.fillText('SMC & OI AGREE (ANCHOR FLOOR)', w - 30, peY + 3);

      // 6. Active Spot Cursor (High-Frequency LERP Interpolated)
      const spotY = toY(s.animSpot);

      // Laser beam line
      ctx.strokeStyle = '#38bdf8';
      ctx.lineWidth = 1.5;
      ctx.beginPath();
      ctx.moveTo(40, spotY);
      ctx.lineTo(w - 20, spotY);
      ctx.stroke();

      // Pulsing laser dot
      ctx.fillStyle = '#38bdf8';
      ctx.beginPath();
      ctx.arc(45, spotY, 3.5, 0, Math.PI * 2);
      ctx.fill();

      // Spot Price Pill Tag
      ctx.fillStyle = 'rgba(14, 165, 233, 0.9)';
      const pillW = 100;
      const pillH = 18;
      ctx.beginPath();
      ctx.roundRect(55, spotY - pillH / 2, pillW, pillH, 4);
      ctx.fill();

      ctx.fillStyle = '#ffffff';
      ctx.font = 'bold 10px monospace';
      ctx.textAlign = 'left';
      ctx.fillText(`SPOT: ${s.animSpot.toFixed(1)}`, 62, spotY + 3.5);

      if (s.dealingRange) {
        ctx.fillStyle = '#38bdf8';
        ctx.font = '9px monospace';
        ctx.textAlign = 'right';
        ctx.fillText(
          `Depth: ${s.dealingRange.discountDepthPct.toFixed(1)}% (${s.dealingRange.currentRegime})`,
          w - 30,
          spotY - 5
        );
      }

      // Micro Institutional Telemetry Footer
      ctx.fillStyle = 'rgba(100, 116, 139, 0.7)';
      ctx.font = '8.5px monospace';
      ctx.textAlign = 'right';
      ctx.fillText(`RAF ${s.fps} FPS · DIRTY-FLAG OPTIMIZED`, w - 10, h - 6);

      ctx.restore();

      animId = requestAnimationFrame(render);
    };

    animId = requestAnimationFrame(render);

    return () => {
      cancelAnimationFrame(animId);
      observer.disconnect();
    };
  }, []);

  return (
    <div className="bg-slate-900/95 border border-slate-800 rounded-lg p-3 flex flex-col gap-2 shadow-xl">
      {/* Institutional Top Control Bar */}
      <div className="flex justify-between items-center pb-2 border-b border-slate-800 text-xs">
        <div className="flex items-center gap-2">
          <span className="font-bold text-slate-200 tracking-wider uppercase font-mono text-[11px]">
            SMC &amp; Price Action Canvas
          </span>
          <span className="text-[9.5px] bg-emerald-500/10 text-emerald-400 font-mono px-1.5 py-0.5 rounded border border-emerald-500/30">
            60 FPS &bull; LERP &bull; DIRTY-FLAG CACHED
          </span>
        </div>
        <div className="flex items-center gap-3 font-mono text-[11px]">
          <span className="text-rose-400 font-semibold">CE Wall: {gammaProfile.callWallStrike}</span>
          <span className="text-emerald-400 font-semibold">PE Wall: {gammaProfile.putWallStrike}</span>
          <span className="text-sky-400 font-semibold">Zero Gamma: {gammaProfile.zeroGammaLevel}</span>
        </div>
      </div>

      {/* Ultra-Low Latency HTML5 Canvas Container */}
      <div className="relative w-full h-[320px] rounded overflow-hidden border border-slate-800/80 bg-slate-950">
        <canvas
          ref={canvasRef}
          className="w-full h-full block"
          style={{ imageRendering: 'auto' }}
        />
      </div>
    </div>
  );
};
