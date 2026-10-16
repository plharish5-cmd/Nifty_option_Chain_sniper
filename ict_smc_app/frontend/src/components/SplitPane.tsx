// SplitPane.tsx — vertical split with a draggable divider + one-click maximize/restore.
// Used by the "📊 Both" view: candles on top, OI·writers below. Remembers the split per viewer.
import { type ReactNode, useRef, useState } from "react";

export default function SplitPane({ top, bottom, storageKey = "ict_both_split" }: {
  top: ReactNode; bottom: ReactNode; storageKey?: string;
}) {
  const [pct, setPct] = useState<number>(() => { try { const v = Number(localStorage.getItem(storageKey)); return v >= 12 && v <= 88 ? v : 60; } catch { return 60; } });
  const box = useRef<HTMLDivElement>(null);
  const save = (v: number) => { const c = Math.max(12, Math.min(88, Math.round(v))); setPct(c); try { localStorage.setItem(storageKey, String(c)); } catch { /* */ } };

  const onDrag = (e: React.MouseEvent) => {
    e.preventDefault();
    const move = (ev: MouseEvent) => { const r = box.current?.getBoundingClientRect(); if (r) save(((ev.clientY - r.top) / r.height) * 100); };
    const up = () => { window.removeEventListener("mousemove", move); window.removeEventListener("mouseup", up); document.body.style.cursor = ""; document.body.style.userSelect = ""; };
    document.body.style.cursor = "row-resize"; document.body.style.userSelect = "none";
    window.addEventListener("mousemove", move); window.addEventListener("mouseup", up);
  };

  const stop = (e: React.MouseEvent) => e.stopPropagation();
  return (
    <div ref={box} className="flex h-full flex-col">
      <div style={{ height: `${pct}%` }} className="min-h-0 overflow-hidden">{top}</div>
      {/* divider */}
      <div onMouseDown={onDrag} onDoubleClick={() => save(60)} title="Drag up / down to resize · double-click to reset 60/40"
        className="group relative z-10 flex h-2.5 shrink-0 cursor-row-resize items-center justify-center bg-ink-700/70 hover:bg-gold-500/40">
        <div className="flex items-center gap-1.5">
          <button onMouseDown={stop} onClick={() => save(88)} title="Maximize candles (slide down)"
            className="rounded px-1.5 text-[10px] leading-none text-slate-300 hover:bg-gold-500/30 hover:text-gold-300">▲ candles</button>
          <span className="h-0.5 w-6 rounded bg-slate-500 group-hover:bg-gold-400" />
          <button onMouseDown={stop} onClick={() => save(12)} title="Maximize OI · writers (slide up)"
            className="rounded px-1.5 text-[10px] leading-none text-slate-300 hover:bg-emerald-500/30 hover:text-emerald-300">▼ OI · writers</button>
          <span className="h-0.5 w-6 rounded bg-slate-500 group-hover:bg-gold-400" />
          <button onMouseDown={stop} onClick={() => save(60)} title="Reset 60 / 40"
            className="rounded px-1.5 text-[10px] leading-none text-slate-400 hover:bg-white/10 hover:text-slate-200">reset</button>
        </div>
      </div>
      <div className="min-h-0 flex-1 overflow-hidden">{bottom}</div>
    </div>
  );
}
