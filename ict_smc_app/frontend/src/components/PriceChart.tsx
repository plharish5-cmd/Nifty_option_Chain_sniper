// PriceChart.tsx — Main Candlestick Chart with Institutional ICT Reference Levels (PDH, PDL, Session Open) overlay.
import React from "react";
import Chart, { type Layers } from "./Chart";
import type { Candle, Analysis, TF, AlertRow } from "../api";
import type { SigPrefs } from "./signalAlerts";
import type { StdvLeg } from "./stdv";

export interface PriceChartProps {
  tf: TF;
  candles: Candle[];
  analysis: Analysis | null;
  layers?: Layers;
  spot?: number | null;
  spotTs?: number;
  marketOpen?: boolean;
  onExpand?: () => void;
  expanded?: boolean;
  sig?: SigPrefs;
  onSignal?: (a: AlertRow) => void;
  onAutoSignal?: (a: AlertRow, ctx: { last: Candle; atr: number; nearWall: number | null }) => void;
  expiries?: string[];
  stdvLeg?: StdvLeg | null;
  isStdvPicking?: boolean;
  onStartStdvPicking?: () => void;
  onStdvPicked?: (leg: StdvLeg) => void;
  onCancelStdvPicking?: () => void;
  onResetToAutoStdv?: () => void;
  showIctLevelsByDefault?: boolean;
}

export default function PriceChart(props: PriceChartProps) {
  const { layers, showIctLevelsByDefault = true, ...rest } = props;

  // Ensure ictLevels is active by default if not explicitly defined
  const mergedLayers: Layers = {
    ob: true,
    fvg: true,
    liq: true,
    structure: true,
    swings: false,
    range: true,
    session: true,
    delta: false,
    deltaDetail: false,
    crt: false,
    auction: false,
    volume: false,
    oi: false,
    rvol: false,
    ictLevels: showIctLevelsByDefault,
    ...layers,
  };

  return (
    <div className="relative w-full h-full flex flex-col">
      <Chart
        {...rest}
        layers={mergedLayers}
        spot={props.spot ?? null}
        spotTs={props.spotTs ?? 0}
        marketOpen={props.marketOpen ?? false}
      />
    </div>
  );
}
