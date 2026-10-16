// multiTimeframe.ts — Multi-timeframe analysis panel (M/W/D)
// Phase 1 + 2: Order flow, confluence, false breakout detection, volatility, heatmap, trends, time analytics

import type { IChartApi, IPrimitivePaneRenderer, IPrimitivePaneView, ISeriesApi, ISeriesPrimitive, PrimitivePaneViewZOrder,
  SeriesAttachedParameter, SeriesType, Time } from "lightweight-charts";

export type TimeframeData = { tf: string; candles: any[]; vwap: any; vprofile: any; orderflow: any; atr: number; emaStack: string };
export type ConfluenceZone = { price: number; strength: number; label: string; reason: string[] };
export type HoverGuide = { zone: string; action: string; entryStrategy: string; riskMgmt: string; confluence: string };

const TIMEFRAMES = [
  { key: 'M', label: 'Monthly', color: 'rgb(251, 146, 60)', offset: -20 },
  { key: 'W', label: 'Weekly', color: 'rgb(226, 232, 240)', offset: 0 },
  { key: 'D', label: 'Daily', color: 'rgb(56, 189, 248)', offset: 20 }
];

// ─────────────────────────────────────────────────────────────────────────────
// CALCULATIONS
// ─────────────────────────────────────────────────────────────────────────────

export function detectFalseBreakout(candles: any[], vprofile: any, atarget: number): boolean {
  if (candles.length < 3) return false;
  const [c1, c2, c3] = candles.slice(-3);

  // Price broke level but volume dried up + closed back inside
  const brokeOut = Math.max(c2.high, c3.high) > atarget && Math.min(c2.low, c3.low) < atarget;
  const lowVol = (c2.volume || 0) < (c1.volume || 0) * 0.7 && (c3.volume || 0) < (c1.volume || 0) * 0.7;
  const rejectionWick = c3.close < c3.open && (c3.high - c3.close) > (c3.close - c3.low) * 1.5;

  return brokeOut && (lowVol || rejectionWick);
}

export function calculateConfluence(mZones: any[], wZones: any[], dZones: any[]): ConfluenceZone[] {
  const allZones = [...mZones, ...wZones, ...dZones];
  const grouped = new Map<number, { strength: number; sources: string[] }>();

  allZones.forEach(z => {
    const key = Math.round(z.mid / 5) * 5; // Group nearby zones
    const curr = grouped.get(key) || { strength: 0, sources: [] };
    curr.strength++;
    curr.sources.push(z.members?.join('+') || 'zone');
    grouped.set(key, curr);
  });

  return Array.from(grouped.entries())
    .filter(([_, v]) => v.strength >= 2)
    .map(([price, { strength, sources }]) => ({
      price,
      strength,
      label: strength === 3 ? '★ A+ (M+W+D)' : '★ A (2 agree)',
      reason: sources
    }))
    .sort((a, b) => b.strength - a.strength);
}

export function detectTrendStrength(candles: any[]): { ema9: number; ema21: number; ema50: number; status: string } {
  const ema = (data: number[], period: number): number => {
    const mult = 2 / (period + 1);
    let em = data.slice(0, period).reduce((a, b) => a + b) / period;
    for (let i = period; i < data.length; i++) em = data[i] * mult + em * (1 - mult);
    return em;
  };

  const closes = candles.map(c => c.close);
  const e9 = ema(closes, 9);
  const e21 = ema(closes, 21);
  const e50 = ema(closes, 50);

  const status = e9 > e21 && e21 > e50 ? 'BULLISH' : e9 < e21 && e21 < e50 ? 'BEARISH' : 'CHOPPY';
  return { ema9: e9, ema21: e21, ema50: e50, status };
}

export function detectLiquidityCluster(vprofile: any[]): { price: number; intensity: number }[] {
  if (!vprofile || !vprofile.length) return [];
  const max = Math.max(...vprofile.map(v => v.volume || 0));
  return vprofile.map(v => ({
    price: v.price,
    intensity: (v.volume || 0) / max
  }));
}

export function generateHoverGuide(price: number, tf: string, isConfluence: boolean, isFalseBreak: boolean, atr: number, trend: string): HoverGuide {
  const guides: Record<string, any> = {
    'M': {
      zone: 'MONTHLY SUPPORT/RESISTANCE — Macro-level price acceptance zone',
      action: isConfluence ? '🎯 BUY if daily bounces here + stacked nodes zone' : '⚠️ Check weekly before entering',
      entryStrategy: `Entry: Bounce confirmation + VWAP +2σ close above this level\nTarget: Next monthly resistance (±${(atr * 2).toFixed(0)} pts)\nSL: Just below this zone`,
      riskMgmt: `Risk: ${(atr * 1.5).toFixed(0)} pts | Reward: ${(atr * 3).toFixed(0)} pts+ | RR: 1:2+\nUse fixed SL only, no trailing`,
      confluence: isConfluence ? '★ A+ — All 3 timeframes agree' : '⚠️ Only monthly'
    },
    'W': {
      zone: 'WEEKLY SUPPORT/RESISTANCE — Intermediate-term equilibrium',
      action: isFalseBreak ? '⛔ REJECT: Price breaks + returns = fake out' : '✓ Dips here bounce (70% of time)',
      entryStrategy: `Entry: Daily ±2σ stretch INTO this zone\nTarget: Weekly resistance above (±${(atr * 1.5).toFixed(0)} pts)\nSL: Weekly low of this zone`,
      riskMgmt: `Max loss: ${(atr * 2).toFixed(0)} pts | Target min: ${(atr * 2).toFixed(0)} pts\nClose 50% at ±1σ, ride 50% to target`,
      confluence: isConfluence ? '★ A+ — Monthly + Weekly + Daily align' : '★ A — At least 2 timeframes'
    },
    'D': {
      zone: 'DAILY SUPPORT/RESISTANCE — Intraday price action zone',
      action: isFalseBreak ? '🔴 SELL INTO STRENGTH: Breaking on low vol = reversal' : '🟢 BUY DIPS: High confluence zone',
      entryStrategy: `Entry: Price close above VWAP +2σ, zone confirmed\nTarget: Daily resistance (±${(atr * 1).toFixed(0)} pts)\nSL: Daily low or VWAP -2σ`,
      riskMgmt: `Risk: ${(atr * 1).toFixed(0)} pts | Reward: ${(atr * 1.5).toFixed(0)}+ pts\nTake profit at ±1σ or trend break`,
      confluence: isConfluence ? '🏆 PRIME ENTRY: All 3 timeframes converge here' : '⚠️ Single timeframe'
    }
  };

  return guides[tf] || guides['D'];
}

// ─────────────────────────────────────────────────────────────────────────────
// UI COMPONENT
// ─────────────────────────────────────────────────────────────────────────────

export class MultiTimeframePrimitive implements ISeriesPrimitive<Time> {
  chart: IChartApi | null = null;
  series: ISeriesApi<SeriesType> | null = null;
  data: Record<string, TimeframeData> = {};
  confluenceZones: ConfluenceZone[] = [];
  hoverData: HoverGuide | null = null;
  hoverPrice: number | null = null;

  private views = [new TimeframeCanvasView(this)];
  private request: (() => void) | null = null;

  attached(p: SeriesAttachedParameter<Time>) {
    this.chart = p.chart as IChartApi;
    this.series = p.series;
    this.request = p.requestUpdate;
  }

  detached() {
    this.chart = null;
    this.series = null;
    this.request = null;
  }

  paneViews() { return this.views; }

  setData(data: Record<string, TimeframeData>, confluence: ConfluenceZone[]) {
    this.data = data;
    this.confluenceZones = confluence;
    this.request?.();
  }

  setHover(price: number | null, guide: HoverGuide | null) {
    this.hoverPrice = price;
    this.hoverData = guide;
    this.request?.();
  }
}

class TimeframeCanvasView implements IPrimitivePaneView {
  constructor(private p: MultiTimeframePrimitive) {}

  zOrder(): PrimitivePaneViewZOrder { return "top"; }

  renderer() {
    return {
      draw: (target: any) => {
        const { series, data, confluenceZones, hoverPrice, hoverData } = this.p;
        if (!series) return;

        target.useMediaCoordinateSpace(({ context: ctx, mediaSize }) => {
          const W = mediaSize.width, H = mediaSize.height;

          // ─ Side panel: 3 timeframes (M/W/D) ─
          const panelX = W - 280, panelW = 260;

          // Background
          ctx.fillStyle = 'rgba(10, 14, 20, 0.95)';
          ctx.fillRect(panelX, 0, panelW, H);
          ctx.strokeStyle = 'rgba(45, 55, 72, 0.5)';
          ctx.lineWidth = 1;
          ctx.strokeRect(panelX, 0, panelW, H);

          // Title
          ctx.font = 'bold 14px Inter, system-ui';
          ctx.fillStyle = '#fbbf24';
          ctx.textAlign = 'left';
          ctx.fillText('M / W / D Analysis', panelX + 12, 24);

          let y = 50;

          // Draw each timeframe
          TIMEFRAMES.forEach((tf, idx) => {
            const tfData = data[tf.key];
            if (!tfData) return;

            const tfH = 100;

            // Timeframe header
            ctx.fillStyle = tf.color;
            ctx.font = 'bold 12px Inter, system-ui';
            ctx.fillText(tf.label, panelX + 12, y + 16);

            // Trend indicator
            const trend = detectTrendStrength(tfData.candles);
            ctx.fillStyle = trend.status === 'BULLISH' ? '#22c55e' : trend.status === 'BEARISH' ? '#dc2626' : '#94a3b8';
            ctx.font = '11px Inter, system-ui';
            ctx.fillText(`${trend.status} (EMA: ${trend.ema9.toFixed(0)}/${trend.ema21.toFixed(0)}/${trend.ema50.toFixed(0)})`, panelX + 12, y + 32);

            // ATR + Volatility
            ctx.fillStyle = '#94a3b8';
            ctx.font = '10px Inter, system-us';
            ctx.fillText(`ATR: ±${tfData.atr.toFixed(1)} | Vol: ${tfData.atr > tfData.atr * 1.2 ? '📈 EXPANDING' : '📉 CONTRACTING'}`, panelX + 12, y + 48);

            // Mini volume profile visualization
            const miniVH = 40;
            const miniY = y + 60;
            if (tfData.vprofile && tfData.vprofile.length) {
              const vmax = Math.max(...tfData.vprofile.map((v: any) => v.volume || 0));
              const volBars = tfData.vprofile.slice(-5); // Last 5 levels

              volBars.forEach((v: any, i: number) => {
                const barH = (v.volume || 0) / vmax * miniVH;
                const barX = panelX + 12 + (i * 40);
                ctx.fillStyle = `rgba(${tf.color === 'rgb(251, 146, 60)' ? '251,146,60' : tf.color === 'rgb(226, 232, 240)' ? '226,232,240' : '56,189,248'},0.6)`;
                ctx.fillRect(barX, miniY + miniVH - barH, 35, barH);

                // Hover detection
                if (hoverPrice && Math.abs(v.price - hoverPrice) < tfData.atr) {
                  ctx.strokeStyle = '#fbbf24';
                  ctx.lineWidth = 2;
                  ctx.strokeRect(barX - 2, miniY - 2, 39, miniVH + 4);
                }
              });
            }

            y += tfH + 20;
          });

          // ─ Confluence zones on right side ─
          ctx.fillStyle = '#fbbf24';
          ctx.font = 'bold 11px Inter, system-ui';
          ctx.textAlign = 'left';
          ctx.fillText('Confluence Zones', panelX + 12, y + 12);

          y += 28;
          confluenceZones.slice(0, 3).forEach(z => {
            const isHovered = hoverPrice && Math.abs(z.price - hoverPrice) < 10;
            ctx.fillStyle = isHovered ? '#fbbf24' : z.strength === 3 ? 'rgba(251, 191, 36, 0.7)' : 'rgba(148, 163, 184, 0.5)';
            ctx.font = isHovered ? 'bold 11px Inter' : '10px Inter';
            ctx.fillText(`${z.label} @ ${z.price.toFixed(0)}`, panelX + 12, y);
            y += 20;
          });

          // ─ Hover guide (bottom right) ─
          if (hoverData && hoverPrice) {
            const guideY = H - 180, guideH = 160;
            ctx.fillStyle = 'rgba(10, 14, 20, 0.98)';
            ctx.fillRect(panelX, guideY, panelW, guideH);
            ctx.strokeStyle = 'rgba(251, 191, 36, 0.6)';
            ctx.lineWidth = 1;
            ctx.strokeRect(panelX, guideY, panelW, guideH);

            ctx.fillStyle = '#fbbf24';
            ctx.font = 'bold 10px Inter';
            ctx.textAlign = 'left';
            ctx.fillText('📌 ACTION GUIDE', panelX + 8, guideY + 14);

            ctx.fillStyle = '#e2e8f0';
            ctx.font = '9px Inter';
            const lines = hoverData.action.split('\n');
            let gY = guideY + 28;
            lines.forEach(line => {
              ctx.fillText(line, panelX + 8, gY);
              gY += 12;
            });

            ctx.fillStyle = '#22c55e';
            ctx.fillText(`Entry: ${hoverData.entryStrategy.split('\n')[0]}`, panelX + 8, gY);
            gY += 24;

            ctx.fillStyle = '#dc2626';
            ctx.fillText(`Risk: ${hoverData.riskMgmt.split('\n')[0]}`, panelX + 8, gY);
          }
        });
      }
    };
  }
}

// ─────────────────────────────────────────────────────────────────────────────
// READER (for hover)
// ─────────────────────────────────────────────────────────────────────────────

export function readMultiTimeframe(price: number | null, mtData: Record<string, TimeframeData>, confluence: ConfluenceZone[]): string[] {
  if (!price || !Object.keys(mtData).length) return [];

  const out: string[] = [];
  const isConfluent = confluence.some(z => Math.abs(z.price - price) < 10 && z.strength === 3);

  TIMEFRAMES.forEach(tf => {
    const tfData = mtData[tf.key];
    if (!tfData) return;

    const isFalseBreak = detectFalseBreakout(tfData.candles, tfData.vprofile, price);
    const guide = generateHoverGuide(price, tf.key, isConfluent, isFalseBreak, tfData.atr, detectTrendStrength(tfData.candles).status);
    out.push(`${tf.label}: ${guide.action}`);
  });

  return out;
}
