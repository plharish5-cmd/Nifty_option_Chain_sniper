import type { AutoscaleInfo } from 'lightweight-charts';

type Level = { p: number; oi: number; side: 'CE' | 'PE' };

// Display geometry only. Wall selection, ranks and daily OI calculations are untouched.
export function nearestOiWalls(walls: readonly Level[], spot: number) {
  const valid = (walls || []).filter(
    (w) =>
      w &&
      Number.isFinite(w.p) &&
      w.p > 0 &&
      Number.isFinite(w.oi) &&
      w.oi > 0 &&
      (!Number.isFinite(spot) || spot <= 0 || Math.abs(w.p - spot) <= 1500)
  );
  const ce = valid.filter((w) => w.side === 'CE').sort((a, b) => Math.abs(a.p - spot) - Math.abs(b.p - spot));
  const pe = valid.filter((w) => w.side === 'PE').sort((a, b) => Math.abs(a.p - spot) - Math.abs(b.p - spot));
  return {
    ceiling: ce.find((w) => w.p >= spot) ?? ce[0],
    floor: pe.find((w) => w.p <= spot) ?? pe[0],
  };
}

export function oiWallRange(base: AutoscaleInfo | null, walls: readonly Level[], spot: number): AutoscaleInfo | null {
  if (!base || !base.priceRange || !Number.isFinite(spot) || spot <= 0) return base;
  const minBase = base.priceRange.minValue;
  const maxBase = base.priceRange.maxValue;
  if (!Number.isFinite(minBase) || !Number.isFinite(maxBase) || minBase <= 0 || maxBase <= 0) return base;

  const near = nearestOiWalls(walls, spot);
  const candidatePrices = [near.ceiling?.p, near.floor?.p].filter(
    (p): p is number => p != null && Number.isFinite(p) && p > 0 && Math.abs(p - spot) <= 1500
  );

  if (!candidatePrices.length) return base;

  const prices = [minBase, maxBase, ...candidatePrices];
  const newMin = Math.min(...prices);
  const newMax = Math.max(...prices);

  if (!Number.isFinite(newMin) || !Number.isFinite(newMax) || newMin <= 0 || newMax <= 0 || newMin >= newMax) {
    return base;
  }

  return { ...base, priceRange: { minValue: newMin, maxValue: newMax } };
}
