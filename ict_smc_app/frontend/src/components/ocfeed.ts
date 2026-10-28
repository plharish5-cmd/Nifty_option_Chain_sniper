// ocfeed.ts — Options Lab feed for the bias meter: PE−CE table (3m · ATM ±10) + writer-flow points (1m · all strikes).
// Same queries the two OC panels use, so the server answers from its per-snapshot cache.
import { useEffect, useState } from "react";
import { BIAS_POLICY, trinitySignalPoints, type OcFeed } from "./bias";

export function useOcFeed(on: boolean): OcFeed | null {
  const [d, setD] = useState<OcFeed | null>(null);
  useEffect(() => {
    if (!on) { setD(null); return; }
    let dead = false;
    const go = () => Promise.all([
      fetch("/api/oclab/pece?tf=3m&mode=auto&n=10").then((r) => r.json()).catch(() => null),
      fetch("/api/oclab/flow?tf=1m&mode=all&n=10").then((r) => r.json()).catch(() => null),
      fetch("/api/oclab/gex?tf=3m&mode=auto&n=10").then((r) => r.json()).catch(() => null),
      fetch("/api/chain/trinity?opt_expiry=both&fut_contract=combined").then((r)=>r.ok?r.json():null).catch(()=>null),
    ]).then(([p, f, g, tr]) => { if (!dead) setD({ pece: p?.ok ? p.rows ?? [] : [], flow: f?.ok ? f.points ?? [] : [], gex: g, trinity: trinitySignalPoints(tr?.series?.both ?? []), day: p?.day ?? f?.day }); });
    go();
    const id = window.setInterval(go, 30000);
    return () => { dead = true; window.clearInterval(id); };
  }, [on]);
  return d;
}

export type SmtMark = { time: number; dir: "bull" | "bear"; text: string; at: string; pivot_at: string; price: number };
export type SmtForming = { time: number; dir: "bull" | "bear"; text: string; at: string; price: number; provisional: true };
export type BankPt = { time: number; c: number };
export function useSmt(tf: string, on: boolean, withBank?: (b: BankPt[]) => void, withForming?: (f: SmtForming[]) => void): SmtMark[] {
  const [d, setD] = useState<SmtMark[]>([]);
  useEffect(() => {
    if (!on || !(tf === "5m" || tf === "15m" || tf === "1h")) { setD([]); withBank?.([]); withForming?.([]); return; }
    let dead = false;
    const go = () => fetch(`/api/smt?tf=${tf}`).then((r) => r.json()).then((j) => { if (!dead) { setD(j.available ? j.markers ?? [] : []); withBank?.(j.available ? j.bank ?? [] : []); withForming?.(j.available ? j.forming ?? [] : []); } }).catch(() => {});
    go();
    const id = window.setInterval(go, 60000);
    return () => { dead = true; window.clearInterval(id); };
  }, [tf, on]); // eslint-disable-line react-hooks/exhaustive-deps
  return d;
}

export function useBiasAgreement(): [number,(value:number)=>void] {
  const read = () => { try { const saved=Number(localStorage.getItem("ict_bias_agreement")); return BIAS_POLICY.agreement_choices.includes(saved) ? saved : BIAS_POLICY.agreement; } catch { return BIAS_POLICY.agreement; } };
  const [value,setValue]=useState(read);
  useEffect(()=>{ const update=()=>setValue(read());window.addEventListener("ict-bias-agreement",update);window.addEventListener("storage",update);return ()=>{window.removeEventListener("ict-bias-agreement",update);window.removeEventListener("storage",update);}; },[]);
  const save=(next:number)=>{ if (!BIAS_POLICY.agreement_choices.includes(next))return;setValue(next);try{localStorage.setItem("ict_bias_agreement",String(next));}catch{/* storage optional */}window.dispatchEvent(new Event("ict-bias-agreement")); };
  return [value,save];
}
