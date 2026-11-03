import type { AutoscaleInfo } from 'lightweight-charts';

type Level = { p: number; oi: number; side: 'CE' | 'PE' };
// Display geometry only. Wall selection, ranks and daily OI calculations are untouched.
export function nearestOiWalls(walls: readonly Level[], spot: number) {
  const valid = walls.filter(w => Number.isFinite(w.p) && w.p > 0 && Number.isFinite(w.oi) && w.oi > 0);
  const ce = valid.filter(w => w.side === 'CE').sort((a,b) => Math.abs(a.p-spot)-Math.abs(b.p-spot));
  const pe = valid.filter(w => w.side === 'PE').sort((a,b) => Math.abs(a.p-spot)-Math.abs(b.p-spot));
  return { ceiling: ce.find(w => w.p >= spot) ?? ce[0], floor: pe.find(w => w.p <= spot) ?? pe[0] };
}
export function oiWallRange(base: AutoscaleInfo | null, walls: readonly Level[], spot: number): AutoscaleInfo | null {
  if (!base || !base.priceRange || !Number.isFinite(spot) || spot <= 0) return base;
  const near = nearestOiWalls(walls, spot);
  const prices = [base.priceRange.minValue, base.priceRange.maxValue, near.ceiling?.p, near.floor?.p]
    .filter((p): p is number => p != null && Number.isFinite(p));
  return {...base, priceRange: {minValue: Math.min(...prices), maxValue: Math.max(...prices)}};
}
