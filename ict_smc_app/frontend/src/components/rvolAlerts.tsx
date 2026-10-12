// rvolAlerts.tsx — every way a browser can get your attention, for RVOL (and other context) alerts:
// chime (Web Audio), spoken voice (speech synthesis), desktop notification, flashing tab title, phone vibration,
// and an on-screen toast. Browsers block sound until you click once, so the header has an "Enable alerts" button.
// Settings are per browser (localStorage); the alert itself is also in the Alerts log and on Telegram (if on).
import { useCallback, useEffect, useRef, useState } from "react";
import type { AlertRow } from "../api";

export type AlertPrefs = { sound: boolean; voice: boolean; desktop: boolean; enabled: boolean };
const KEY = "ict_rvol_alert_prefs";
const load = (): AlertPrefs => {
  try { return { sound: true, voice: true, desktop: true, enabled: false, ...JSON.parse(localStorage.getItem(KEY) || "{}") }; }
  catch { return { sound: true, voice: true, desktop: true, enabled: false }; }
};

let ctx: AudioContext | null = null;
function tones(seq: [number, number][]) {                     // [frequency Hz, duration s]
  try {
    ctx = ctx ?? new (window.AudioContext || (window as unknown as { webkitAudioContext: typeof AudioContext }).webkitAudioContext)();
    let t = ctx.currentTime + 0.02;
    for (const [f, d] of seq) {
      const o = ctx.createOscillator(), g = ctx.createGain();
      o.type = "sine"; o.frequency.value = f;
      g.gain.setValueAtTime(0.0001, t); g.gain.exponentialRampToValueAtTime(0.35, t + 0.02); g.gain.exponentialRampToValueAtTime(0.0001, t + d);
      o.connect(g).connect(ctx.destination); o.start(t); o.stop(t + d + 0.05);
      t += d + 0.06;
    }
  } catch { /* no audio */ }
}
const SOUND: Record<string, [number, number][]> = {
  RVOL_ACTIVATED: [[660, 0.18], [880, 0.18], [1100, 0.32]],          // rising: participation arrived
  BREAKOUT_WITH_RVOL: [[880, 0.15], [1100, 0.15], [1320, 0.3]],
  RVOL_SPIKE: [[990, 0.12], [990, 0.12], [990, 0.25]],
  RVOL_FADING: [[880, 0.2], [660, 0.2], [440, 0.32]],                // falling: participation leaving
  BREAKOUT_NO_RVOL: [[300, 0.3], [0, 0.08], [300, 0.45]],            // low warning
};
const SAY: Record<string, string> = {
  RVOL_ACTIVATED: "NIFTY R VOL activated", BREAKOUT_WITH_RVOL: "Breakout confirmed by R VOL",
  RVOL_SPIKE: "R VOL spike", RVOL_FADING: "R VOL fading", BREAKOUT_NO_RVOL: "Warning. Breakout without volume",
};

export function useRvolAlerts() {
  const [prefs, setPrefs] = useState<AlertPrefs>(load);
  const [toasts, setToasts] = useState<AlertRow[]>([]);
  const titleTimer = useRef<number | null>(null);
  useEffect(() => { try { localStorage.setItem(KEY, JSON.stringify(prefs)); } catch { /* private mode */ } }, [prefs]);

  const enable = useCallback(async () => {
    tones([[880, 0.12]]);                                           // the click unlocks audio for this page
    if ("Notification" in window && Notification.permission === "default") {
      try { await Notification.requestPermission(); } catch { /* ignore */ }
    }
    setPrefs((p) => ({ ...p, enabled: true }));
  }, []);

  const fire = useCallback((a: AlertRow) => {
    const kind = a.kind;
    setToasts((t) => [a, ...t].slice(0, 4));
    window.setTimeout(() => setToasts((t) => t.filter((x) => x !== a)), 45000);
    if (!prefs.enabled) return;
    if (prefs.sound) tones(SOUND[kind] ?? [[880, 0.2], [880, 0.2]]);
    if (prefs.voice && "speechSynthesis" in window) {
      try { const u = new SpeechSynthesisUtterance(`${SAY[kind] ?? "Alert"}. ${a.title.replace(/×/g, " times").replace(/RVOL/g, "R VOL")}`); u.rate = 1.05; speechSynthesis.speak(u); } catch { /* ignore */ }
    }
    if (prefs.desktop && "Notification" in window && Notification.permission === "granted") {
      try { new Notification(a.title, { body: a.body, tag: a.title, requireInteraction: kind === "RVOL_ACTIVATED" || kind === "BREAKOUT_NO_RVOL" }); } catch { /* ignore */ }
    }
    try { navigator.vibrate?.([200, 100, 200, 100, 400]); } catch { /* ignore */ }
    const base = document.title.replace(/^⚡ .*? · /, "");
    let on = false, n = 0;
    if (titleTimer.current) window.clearInterval(titleTimer.current);
    titleTimer.current = window.setInterval(() => {
      on = !on; n += 1;
      document.title = on ? `⚡ ${a.title.slice(0, 40)} · ${base}` : base;
      if (n > 40 || document.hasFocus()) { document.title = base; if (titleTimer.current) window.clearInterval(titleTimer.current); }
    }, 800);
  }, [prefs]);

  const dismiss = (a: AlertRow) => setToasts((t) => t.filter((x) => x !== a));
  return { prefs, setPrefs, enable, fire, toasts, dismiss };
}

const TONE: Record<string, string> = {
  RVOL_ACTIVATED: "border-bull-400 text-bull-400", BREAKOUT_WITH_RVOL: "border-bull-400 text-bull-400",
  RVOL_SPIKE: "border-amber-400 text-amber-300", RVOL_FADING: "border-slate-400 text-slate-300",
  BREAKOUT_NO_RVOL: "border-bear-400 text-bear-400",
};

export function AlertToasts({ toasts, dismiss }: { toasts: AlertRow[]; dismiss: (a: AlertRow) => void }) {
  if (!toasts.length) return null;
  return (
    <div className="fixed top-3 right-3 z-50 flex flex-col gap-2 w-[min(420px,calc(100vw-24px))]">
      {toasts.map((a, i) => (
        <div key={`${a.title}-${i}`} className={`rounded-lg border-2 bg-ink-900/95 shadow-xl p-3 ${TONE[a.kind] ?? "border-gold-500 text-gold-400"} animate-pulse-once`}>
          <div className="flex items-start gap-2">
            <div className="font-semibold text-[13px] flex-1">⚡ {a.title}</div>
            <button onClick={() => dismiss(a)} className="text-slate-400 hover:text-slate-100 text-sm" aria-label="Dismiss">✕</button>
          </div>
          <div className="text-[12px] text-slate-300 mt-1">{a.body}</div>
        </div>
      ))}
    </div>
  );
}

export function AlertControls({ prefs, setPrefs, enable }: { prefs: AlertPrefs; setPrefs: (f: (p: AlertPrefs) => AlertPrefs) => void; enable: () => void }) {
  if (!prefs.enabled) {
    return <button onClick={enable} className="pill bg-gold-500 text-ink-900 font-semibold animate-pulse">🔔 Enable RVOL alerts (sound · voice · desktop)</button>;
  }
  const t = (k: keyof AlertPrefs, label: string) => (
    <button onClick={() => setPrefs((p) => ({ ...p, [k]: !p[k] }))} className={`pill ${prefs[k] ? "bg-ink-700 text-slate-100" : "bg-ink-800 text-slate-500 line-through"}`}>{label}</button>
  );
  const blocked = "Notification" in window && Notification.permission === "denied";
  return (
    <span className="flex items-center gap-1">
      <span className="text-[11px] text-slate-400">🔔 alerts:</span>{t("sound", "sound")}{t("voice", "voice")}{t("desktop", blocked ? "desktop (blocked)" : "desktop")}
      <button title="Sends a test alert through the server: log, sound, voice, desktop notification and Telegram (if on)"
        onClick={() => { fetch("/api/alerts/test-rvol", { method: "POST" }).catch(() => tones(SOUND.RVOL_ACTIVATED)); }}
        className="pill bg-ink-800 text-slate-300">test</button>
    </span>
  );
}
