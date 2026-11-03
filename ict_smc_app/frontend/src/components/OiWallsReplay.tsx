import { useEffect, useMemo, useRef, useState } from 'react';
import { createChart, CandlestickSeries, ColorType, type UTCTimestamp, type Time, type IChartApi, type ISeriesApi } from 'lightweight-charts';
import { buildWalls, OrderFlowPrimitive, type Wall, type OiLevel } from './orderflow';

type Row = { strike: number; ce: { oi: number; oi_chg: number }; pe: { oi: number; oi_chg: number } };
type Candle = { ts: number; o: number; h: number; l: number; c: number };

interface Frame {
  ts: number;
  spot: number;
  expiry: string;
  source?: string;
  rows: Row[];
}

interface BatchPayload {
  ok: boolean;
  message?: string;
  day: string;
  expiry: string;
  expiries: string[];
  candles: Candle[];
  frames: Frame[];
}

const clock = (ts: number) => new Date(ts * 1000).toLocaleTimeString('en-IN', { timeZone: 'Asia/Kolkata', hour12: false });
const units = (n: number) => (n / 1e5).toFixed(2) + ' L';

// In-memory module cache: survives component remounts across slider drags
const BATCH_CACHE = new Map<string, BatchPayload>();

export default function OiWallsReplay({ day, ts }: { day: string; ts: number }) {
  const [expiry, setExpiry] = useState('');
  const [batchData, setBatchData] = useState<BatchPayload | null>(() => {
    const key = `${day}:${expiry || 'default'}`;
    return BATCH_CACHE.get(key) || null;
  });
  const [error, setError] = useState('');
  const [hover, setHover] = useState<Wall | null>(null);

  const host = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const seriesRef = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const primitiveRef = useRef<OrderFlowPrimitive | null>(null);
  const wallsRef = useRef<Wall[]>([]);

  // Fetch full day's batch frames once when day or expiry changes
  useEffect(() => {
    const key = `${day}:${expiry || 'default'}`;
    const cached = BATCH_CACHE.get(key);
    if (cached) {
      setBatchData(cached);
      setError('');
      return;
    }

    const abort = new AbortController();
    setError('');
    const q = new URLSearchParams({ day, mode: 'batch' });
    if (expiry) q.set('expiry', expiry);

    fetch('/api/desk/oi-walls-replay?' + q, { signal: abort.signal })
      .then(async r => {
        if (!r.ok || !r.headers.get('content-type')?.includes('application/json')) {
          throw Error('Restart the ICT backend to load the OI walls replay batch API');
        }
        return r.json();
      })
      .then((d: BatchPayload) => {
        if (!abort.signal.aborted) {
          if (d.ok) {
            BATCH_CACHE.set(key, d);
            setBatchData(d);
          } else {
            setError(d.message || 'No recorded OI replay batch available');
          }
        }
      })
      .catch(e => {
        if (!abort.signal.aborted) {
          setError(e.message || String(e));
        }
      });

    return () => abort.abort();
  }, [day, expiry]);

  // Pure in-memory lookup (< 1 ms): resolve nearest frame where frame.ts <= ts
  const activeFrame = useMemo(() => {
    if (!batchData?.frames?.length) return null;
    let found = batchData.frames[0];
    for (let i = batchData.frames.length - 1; i >= 0; i--) {
      if (batchData.frames[i].ts <= ts) {
        found = batchData.frames[i];
        break;
      }
    }
    return found;
  }, [batchData, ts]);

  // Sliced candles up to current scrubber timestamp
  const activeCandles = useMemo(() => {
    if (!batchData?.candles?.length) return [];
    const filtered = batchData.candles.filter(c => c.ts + 60 <= ts || c.ts <= ts);
    return filtered.length > 0 ? filtered : batchData.candles.slice(0, 1);
  }, [batchData, ts]);

  // Initialize lightweight-charts once
  useEffect(() => {
    if (!host.current) return;
    const chart = createChart(host.current, {
      autoSize: true,
      height: 600,
      layout: { background: { type: ColorType.Solid, color: '#0b1220' }, textColor: '#94a3b8' },
      grid: { vertLines: { color: '#152036' }, horzLines: { color: '#152036' } },
      timeScale: { timeVisible: true, secondsVisible: false, tickMarkFormatter: (t: Time) => clock(Number(t)) },
      localization: { timeFormatter: (t: Time) => clock(Number(t)) }
    });
    const series = chart.addSeries(CandlestickSeries, {
      upColor: '#10b981',
      downColor: '#ef4444',
      borderVisible: false,
      wickUpColor: '#10b981',
      wickDownColor: '#ef4444'
    });
    const primitive = new OrderFlowPrimitive();
    series.attachPrimitive(primitive);

    chart.subscribeCrosshairMove(p => {
      if (!p.point) {
        setHover(null);
        return;
      }
      const price = series.coordinateToPrice(p.point.y);
      if (price == null) return;
      const currentWalls = wallsRef.current;
      setHover([...currentWalls].sort((a, b) => Math.abs(a.p - price) - Math.abs(b.p - price))[0] || null);
    });

    chartRef.current = chart;
    seriesRef.current = series;
    primitiveRef.current = primitive;

    return () => {
      chart.remove();
      chartRef.current = null;
      seriesRef.current = null;
      primitiveRef.current = null;
    };
  }, []);

  // Instant update series & primitive without recreating chart instance
  useEffect(() => {
    if (!chartRef.current || !seriesRef.current || !primitiveRef.current || !activeFrame) return;

    if (activeCandles.length > 0) {
      seriesRef.current.setData(
        activeCandles.map(c => ({
          time: c.ts as UTCTimestamp,
          open: c.o,
          high: c.h,
          low: c.l,
          close: c.c
        }))
      );
    }

    const walls = buildWalls(activeFrame.rows, activeFrame.spot);
    wallsRef.current = walls;

    const oiLevels: OiLevel[] = activeFrame.rows
      .filter(r => Math.abs(r.strike - activeFrame.spot) <= activeFrame.spot * 0.02)
      .flatMap(r => (['CE', 'PE'] as const).map(side => {
        const leg = side === 'CE' ? r.ce : r.pe;
        return {
          p: r.strike,
          side,
          oi: leg.oi,
          chg: leg.oi_chg,
          pct: leg.oi ? (leg.oi_chg / leg.oi) * 100 : null
        };
      }));

    primitiveRef.current.set({ walls, oiLevels });
  }, [activeFrame, activeCandles]);

  return (
    <section>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', flexWrap: 'wrap', gap: '8px', marginBottom: '8px' }}>
        <h2 style={{ margin: 0 }}>Recorded OI walls · call / put levels and percentage bubbles</h2>
        <div style={{
          display: 'inline-flex',
          alignItems: 'center',
          gap: '8px',
          padding: '6px 14px',
          borderRadius: '6px',
          backgroundColor: 'rgba(239, 68, 68, 0.16)',
          border: '1px solid rgba(239, 68, 68, 0.45)',
          color: '#f87171',
          fontWeight: 800,
          fontSize: '13px',
          letterSpacing: '0.05em',
          textTransform: 'uppercase',
          boxShadow: '0 2px 8px rgba(0,0,0,0.25)'
        }}>
          <span>⏪ HISTORICAL REPLAY — NOT LIVE</span>
        </div>
      </div>

      <div style={{ marginBottom: '8px' }}>
        <label>
          Expiry{' '}
          <select value={expiry || batchData?.expiry || ''} onChange={e => setExpiry(e.target.value)}>
            {(batchData?.expiries || []).map(e => (
              <option key={e}>{e}</option>
            ))}
          </select>
        </label>
      </div>

      <p>Use the shared time slider above. Instant batch-cached scrub from browser memory in &lt;1 ms with zero network lag.</p>

      {error ? (
        <p role="alert">{error}</p>
      ) : !batchData ? (
        <p>Loading recorded walls batch…</p>
      ) : !batchData.ok ? (
        <p>{batchData.message}</p>
      ) : !activeFrame ? (
        <p>No snapshot found for selected timestamp.</p>
      ) : (
        <>
          <p>
            OI captured {clock(activeFrame.ts)} IST · target {clock(ts)} · snapshot age {Math.round(ts - activeFrame.ts)}s · recorded spot{' '}
            {activeFrame.spot.toFixed(2)} · {activeFrame.expiry}
          </p>
          <p>
            Original wall selection and bubble formulas reused. Percentage = daily ΔOI ÷ current OI. Historical wall velocity unavailable;
            no cooling/acceleration is inferred.
          </p>
          <div ref={host} />
          <p>
            {hover
              ? `${hover.side} ${hover.p.toLocaleString('en-IN')} · OI ${units(hover.oi)} · daily ΔOI ${units(hover.chg)} · ${(hover.fresh * 100).toFixed(1)}% of current OI · ${hover.status}`
              : 'Move the mouse over the chart to inspect the closest OI wall.'}
          </p>
          <p>
            Walls describe the selected snapshot across the chart, not the walls at each earlier candle. Scrub the slider to change the
            snapshot. OI alone does not identify writer intent.
          </p>
        </>
      )}
    </section>
  );
}
