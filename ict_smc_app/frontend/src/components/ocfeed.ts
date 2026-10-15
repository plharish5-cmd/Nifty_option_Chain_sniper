// ocfeed.ts — Options Lab feed for the bias meter: PE−CE table (3m · ATM ±10) + writer-flow points (1m · all strikes).
// Same queries the two OC panels use, so the server answers from its per-snapshot cache.
import { useEffect, useState } from "react";
import type { OcFeed } from "./bias";

export function useOcFeed(on: boolean): OcFeed | null {
  const [d, setD] = useState<OcFeed | null>(null);
  useEffect(() => {
    if (!on) { setD(null); return; }
    let dead = false;
    const go = () => Promise.all([
      fetch("/api/oclab/pece?tf=3m&mode=auto&n=10").then((r) => r.json()).catch(() => null),
      fetch("/api/oclab/flow?tf=1m&mode=all&n=10").then((r) => r.json()).catch(() => null),
      fetch("/api/oclab/gex?tf=3m&mode=auto&n=10").then((r) => r.json()).catch(() => null),
    ]).then(([p, f, g]) => { if (!dead) setD({ pece: p?.ok ? p.rows ?? [] : [], flow: f?.ok ? f.points ?? [] : [], gex: g, day: p?.day ?? f?.day }); });
    go();
    const id = window.setInterval(go, 30000);
    return () => { dead = true; window.clearInterval(id); };
  }, [on]);
  return d;
}
