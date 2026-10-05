"""
terminal_extras.py — ONE readable dashboard for your briefing terminal, every 5 minutes during market hours, drawn as separate
boxed tables (terminal_tables.py): at-a-glance · alerts · OPTION CHAIN · levels ladder · INSTITUTIONAL · metrics · trade check · SMC
· 5-RULE · CANDLES · ORDER FLOW · SCANNER · GARCH · EVIDENCE BOARD · data health.

It re-draws what the existing modules already produce:

  SMC plain read · GARCH size-of-move box · Evidence Board line · CANDLES (wicks, forming candle, patterns at levels, tested verdicts)
  ORDER FLOW (NIFTY 50 delta / CVD / Delta+VWAP) · SCANNER (ICT + fake-breakout events with order-flow and option-chain context)

Your existing terminal shows the scheduler's alert stream (data/agt_briefing_watch.log). This script adds its own block to
that same stream, so any terminal tailing the log shows it, and the one-click launcher shows it too.

  .venv/bin/python terminal_extras.py --once            # one block now (also appended to the watch log)
  .venv/bin/python terminal_extras.py --once --no-write # just print it
  .venv/bin/python terminal_extras.py --loop            # every 5 min, 25 s after the boundary, 09:20-15:35 on weekdays
  .venv/bin/python terminal_extras.py --once --force    # outside market hours (uses the latest stored candles)
Information only — not financial advice.
"""
from __future__ import annotations

import argparse
import sys
import time as _t
import traceback
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))
START, END = "09:20", "15:35"
OFFSET_S = 25                                  # let the 1-minute candle of the boundary land in the store first


def _watch_log() -> Path:
    try:
        import config
        return Path(config.DATA_DIR) / "agt_briefing_watch.log"
    except Exception:
        return ROOT / "data" / "agt_briefing_watch.log"


def _refresh_candles(now: datetime) -> str:
    """Today's 1-minute index candles are NOT being saved by the scheduler, so the SMC / CANDLES blocks were reading YESTERDAY's
    last candle. Fetch today's 1m from Dhan (same call the dashboard uses) and save them to data/candles_hist/YYYY-MM-DD.csv —
    the research folder, not data/candles, because only scheduler.py may write there (single-writer rule)."""
    try:
        import dhan_client as dc
        try:
            import config
            hist = Path(config.DATA_DIR) / "candles_hist"
        except Exception:
            hist = ROOT / "data" / "candles_hist"
        idx = dc.nifty_index_candles(days_back=1)
        if idx is None or idx.empty:
            return "DATA: Dhan returned no 1-minute candles (token expired? market closed?) — blocks below may be stale."
        today = pd.Timestamp(now).date()
        d = idx[idx["timestamp"].dt.date == today].copy()
        if d.empty:
            last = idx["timestamp"].max()
            return f"DATA: Dhan has no candles for today yet (latest {last:%a %d %b %H:%M}) — blocks below use the last session."
        hist.mkdir(parents=True, exist_ok=True)
        d["timestamp"] = d["timestamp"].dt.strftime("%Y-%m-%d %H:%M:%S")
        d.to_csv(hist / f"{today:%Y-%m-%d}.csv", index=False)
        return f"DATA: today's 1-minute candles refreshed from Dhan · last candle {d['timestamp'].iloc[-1][11:16]} · {len(d)} bars"
    except Exception as e:                      # noqa: BLE001
        return f"DATA: could not refresh 1-minute candles ({type(e).__name__}: {str(e)[:90]}) — blocks below use whatever is stored."


def _smc_part(now: datetime) -> str:
    try:
        import smc_brief
        return smc_brief.build_block(smc_brief.auto_tf(now.time()), now)
    except Exception as e:                      # noqa: BLE001
        return f"SMC / CANDLES: N/A ({type(e).__name__}: {e})"


def _flow_part(now: datetime) -> str:
    try:
        import smc_brief
        return smc_brief.orderflow_block(now)
    except Exception as e:                      # noqa: BLE001
        return f"ORDER FLOW: N/A ({type(e).__name__}: {e})"


def _scanner_part(now: datetime) -> str:
    hdr = "SCANNER · ICT sweep→FVG and fake-breakout events, with order flow + option chain at that moment (logged, UNVALIDATED)"
    try:
        import nifty_scanner as ns
        day = f"{now:%Y-%m-%d}"
        sc = ns.NiftyScanner()
        import smc_brief
        m1 = smc_brief._read_1m(8, now.date())                      # a week of context + today so far (the backtest loader skips partial days)
        sc.scan(day, pd.Timestamp(now), m1, quiet=True)             # logs any new event
        path = ns.LOG_DIR / f"events_{day}.csv"
        if not path.exists() or path.stat().st_size == 0:
            return f"{hdr}\n  No events yet today (checked {now:%H:%M})."
        ev = pd.read_csv(path)
        if ev.empty:
            return f"{hdr}\n  No events yet today (checked {now:%H:%M})."
        L = [hdr, f"  {len(ev)} event(s) today — last {min(5, len(ev))}:"]
        for r in ev.tail(5).itertuples():
            arrow = "▲ up-idea" if r.side == "BULLISH" else "▼ down-idea"
            bias = "—" if pd.isna(r.net_bias) else f"{r.net_bias:+.2f}"
            L.append(f"   {str(r.event_t)[11:16]}  {r.kind:<11} {arrow:<11} {r.level_kind}@{r.level:,.1f}  flow:{r.flow} chain:{r.chain} bias:{bias}  score {r.score}/3")
        L.append("  Score 3 = event + order flow CONFIRMED + option-chain bias agrees. Not a signal until confluence.json says so.")
        return "\n".join(L)
    except Exception as e:                      # noqa: BLE001
        return f"{hdr}\n  N/A ({type(e).__name__}: {e})"


REFRESH = True


def _brief_text(now: datetime) -> str:
    """The scheduler's own briefing text (option chain, institutional, levels, checklist) — same call the scheduler makes, no side effects."""
    try:
        import agt_briefing as ab
        t = ab.build_briefing(now.date(), now.time(), append_obs=False)
        return t if isinstance(t, str) else ""
    except Exception as e:                      # noqa: BLE001
        return f"BRIEFING: N/A ({type(e).__name__}: {str(e)[:100]})"


def _scanner_data(now: datetime):
    """(rows, note) for the SCANNER table — rows are lists of strings."""
    try:
        import nifty_scanner as ns
        import smc_brief
        day = f"{now:%Y-%m-%d}"
        sc = ns.NiftyScanner()
        m1 = smc_brief._read_1m(8, now.date())                      # a week of context + today so far (the backtest loader skips partial days)
        sc.scan(day, pd.Timestamp(now), m1, quiet=True)             # logs any new event
        path = ns.LOG_DIR / f"events_{day}.csv"
        if not path.exists() or path.stat().st_size == 0:
            return [], f"No events yet today (checked {now:%H:%M})."
        ev = pd.read_csv(path)
        if ev.empty:
            return [], f"No events yet today (checked {now:%H:%M})."
        rows = []
        for r in ev.tail(6).itertuples():
            arrow = "▲ up-idea" if r.side == "BULLISH" else "▼ down-idea"
            bias = "—" if pd.isna(r.net_bias) else f"{r.net_bias:+.2f}"
            rows.append([str(r.event_t)[11:16], r.kind, arrow, f"{r.level_kind}@{r.level:,.1f}", r.flow, r.chain, bias, f"{r.score}/3"])
        return rows, f"{len(ev)} event(s) today, last {len(rows)} shown. Score 3 = event + order flow CONFIRMED + option-chain bias agrees. Not a signal until confluence.json says so."
    except Exception as e:                      # noqa: BLE001
        return [], f"N/A ({type(e).__name__}: {str(e)[:100]})"


_SKIP_HDR = ("5-MIN", "15-MIN", "DASHBOARD", "SMC · CANDLES", "SMC PLAIN READ")


def _parse_alert_rows(lines) -> list:
    """Turn raw watch-log lines into alert rows [time, source, text]. MENTOR blocks are split into one row per alert; any other alert
    block (not a 5-MIN / 15-MIN snapshot, not the briefing) gives one row per line. Pure function — also used by --alerts."""
    import re
    blocks, cur, stop = [], None, False
    for ln in lines:
        ln = ln.rstrip("\n")
        if ln.startswith("──── "):
            cur, stop = [ln, []], False
            blocks.append(cur)
        elif cur is not None:
            if re.match(r"^═+$", ln.strip()) or "NIFTY LIVE BRIEFING" in ln:
                stop = True
            if not stop:
                cur[1].append(ln)
    rows = []
    for hdr, body in blocks:
        if any(k in hdr for k in _SKIP_HDR):
            continue
        m = re.search(r"(\d\d:\d\d)", hdr)
        t = m.group(1) if m else ""
        label = re.sub(r"[─\s]+$|^[─\s]+", "", hdr)
        label = re.sub(r"\d\d:\d\d", "", label).strip() or "ALERT"
        paras, para = [], []
        for ln in body + [""]:
            if ln.strip():
                para.append(ln.strip())
            elif para:
                paras.append(para)
                para = []
        for para in paras:
            para = [x for x in para if not re.match(r"^(PA 5-min|OC \(mentor\))", x)]
            if not para:
                continue
            if para[0].startswith("🟪🟪 PA 15-MIN SUPER SIGNAL") or para[0].startswith("🟪🟪 PA 5-MIN"):
                continue                                    # just a heading line; the signal itself is in the next paragraph
            if para[0].startswith("[PA") or para[0].startswith("[8EMA"):
                tag = para[0].split("]")[0].lstrip("[")
                src = tag.replace("8EMA 15m SUPER", "8EMA 15m")[:12]
                keep = []
                for x in para[1:]:
                    if x.startswith("Backtest"):
                        continue
                    if x.startswith("Confluence"):
                        x = x.split(":", 1)[0]
                    keep.append(x)
                txt = " · ".join(keep) if keep else para[0].split("]", 1)[-1].strip()
                rows.append([t, src, txt])
            else:
                for x in para:
                    rows.append([t, "OC" if "MENTOR" in hdr else label[:12], x])
    return rows


def _alerts(now: datetime, limit: int = 8):
    """Latest alerts from the watch log (for the dashboard's ALERTS table)."""
    try:
        p = _watch_log()
        if not p.exists():
            return []
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            tail = f.readlines()[-700:]
        return _parse_alert_rows(tail)[-limit:]
    except Exception:                           # noqa: BLE001
        return []


def watch_alerts(width: int = 0) -> None:
    """Print each NEW alert from the scheduler's stream as a small boxed table, the moment it appears (checks every 4 s)."""
    import terminal_tables as tt
    p = _watch_log()
    pos = p.stat().st_size if p.exists() else 0
    print(f"alerts: watching {p}", flush=True)
    import re as _re
    recent = []                                             # (minute, source, side, entry) of triggers already shown

    def _repeat(r) -> bool:
        """Same setup re-fired: same source + side, within 5 min and 15 pts of one already shown (signal_tape.MERGE_*)."""
        m = _re.search(r"PA TRIGGERED · (PE|CE) at ([\d,]+)(?: \((\d\d:\d\d))?", r[2])
        tt_ = (m.group(3) if m and m.group(3) else r[0]) if m else ""
        if not m or not _re.match(r"\d\d:\d\d", tt_ or ""):
            return False
        mn = int(tt_[:2]) * 60 + int(tt_[3:5])
        side, e = m.group(1), float(m.group(2).replace(",", ""))
        del recent[:-30]
        for (t0, s0, sd0, e0) in recent:
            if s0 == r[1] and sd0 == side and 0 <= mn - t0 <= 5 and abs(e - e0) <= 15:
                return True
        recent.append((mn, r[1], side, e))
        return False
    while True:
        _t.sleep(4)
        try:
            if not p.exists():
                continue
            size = p.stat().st_size
            if size < pos:
                pos = 0                                     # log rotated / truncated
            if size == pos:
                continue
            with open(p, "r", encoding="utf-8", errors="replace") as f:
                f.seek(pos)
                chunk = f.read()
                pos = f.tell()
            rows = [r for r in _parse_alert_rows(chunk.splitlines()) if not _repeat(r)]
            if rows:
                w = width or tt.screen_width()
                sig = any(("PA TRIGGERED" in r[2] or "8 EMA SIGNAL" in r[2]) for r in rows)
                title = ("SIGNAL" if sig else "ALERT") + f" · {rows[-1][0] or datetime.now().strftime('%H:%M')}"
                txt = "\n".join(tt.grid(title, ["Time", "Source", "Alert"], rows, w, flex=2))
                print(("\a" if sig and tt.use_color() else "") + (tt.colorize(txt) if tt.use_color() else txt), flush=True)
        except Exception:                                   # noqa: BLE001
            traceback.print_exc()


def _tape(now: datetime):
    """SIGNAL TAPE rows for the dashboard — the whole day's signals, not just the last 700 log lines."""
    try:
        import signal_tape
        return signal_tape.build(now, _watch_log())
    except Exception as e:                      # noqa: BLE001
        return [], f"N/A ({type(e).__name__}: {str(e)[:100]})"


def _oi_lab(now: datetime):
    """Options Lab readings + strike-by-strike OI + BUY / WAIT / WAIT-FOR-REVERSAL score (oi_lab_view.py)."""
    try:
        import oi_lab_view
        return oi_lab_view.compute(now)
    except Exception as e:                      # noqa: BLE001
        return {"ok": False, "why": f"{type(e).__name__}: {str(e)[:100]}"}


def _gex_note() -> str:
    """How often the GEX regime label changed in today's briefings (from the watch log)."""
    import re
    try:
        import signal_tape
        with open(_watch_log(), "r", encoding="utf-8", errors="replace") as f:
            txt = "".join(signal_tape._today_lines(f.readlines()))
        seq = re.findall(r"^GEX:\s+[+-]₹[\d.,]+Cr \((SHORT|LONG|NEUTRAL) gamma", txt, re.M)
        if len(seq) < 2:
            return "UNVALIDATED regime proxy (no edge found, p 0.68 / 0.74)"
        ch = sum(1 for a, b in zip(seq, seq[1:]) if a != b)
        cnt = ", ".join(f"{k} {seq.count(k)}" for k in ("SHORT", "LONG", "NEUTRAL") if seq.count(k))
        return f"UNVALIDATED regime proxy (no edge found, p 0.68 / 0.74) · label changed {ch}× in {len(seq)} briefings today ({cnt})"
    except Exception:                           # noqa: BLE001
        return "UNVALIDATED regime proxy (no edge found, p 0.68 / 0.74)"


def build(now: datetime, width: int = 112) -> str:
    data = _refresh_candles(now) if REFRESH else ""
    brief = _brief_text(now)
    try:
        import re as _re
        brief = _re.sub(r"^(GEX:\s+.*)$", lambda m: m.group(1) + " · " + _gex_note(), brief, count=1, flags=_re.M)
    except Exception:                           # noqa: BLE001
        pass
    smc = _smc_part(now)
    if "ORDER FLOW" not in smc:                 # the SMC block normally carries the order-flow section; add it if not
        smc = smc + "\n\n" + _flow_part(now)
    scan = _scanner_data(now)
    try:
        import terminal_tables as tt
        return tt.render_dashboard(brief, smc, scan, _alerts(now), data, now, width, _oi_lab(now), _tape(now))
    except Exception as e:                      # noqa: BLE001  — never lose the information: fall back to the plain text blocks
        traceback.print_exc()
        parts = ([data] if data else []) + [brief, smc, f"(table view failed: {type(e).__name__}: {e})", "Information only — not financial advice."]
        return "\n\n".join(parts)


def emit(now: datetime, write: bool = True, show: bool = True):
    """Returns the text to print on screen (coloured when the screen is a terminal). The log file always gets the plain version."""
    import terminal_tables as tt
    plain = build(now, 112 if write else tt.screen_width())
    if write:
        try:
            p = _watch_log()
            p.parent.mkdir(parents=True, exist_ok=True)
            with open(p, "a", encoding="utf-8") as f:
                f.write("\n──── 🟫 DASHBOARD " + f"{now:%H:%M} ────\n" + plain + "\n\n")
        except Exception as e:                  # noqa: BLE001
            print(f"(could not write {p}: {e})", file=sys.stderr)
    return tt.colorize(plain) if tt.use_color() else plain


def in_market(now: datetime) -> bool:
    return now.weekday() < 5 and START <= now.strftime("%H:%M") <= END


def next_run(now: datetime) -> datetime:
    base = now.replace(second=0, microsecond=0)
    t = base + timedelta(minutes=(5 - base.minute % 5) % 5, seconds=OFFSET_S)
    if t <= now:
        t += timedelta(minutes=5)
    return t


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--no-write", action="store_true", help="print only; do not append to the watch log")
    ap.add_argument("--force", action="store_true", help="run outside market hours")
    ap.add_argument("--no-refresh", action="store_true", help="do not fetch fresh 1-minute candles from Dhan first")
    ap.add_argument("--alerts", action="store_true", help="only watch the scheduler stream and print each new alert as a boxed table")
    a = ap.parse_args()
    if a.alerts:
        watch_alerts()
        return 0
    global REFRESH
    REFRESH = not a.no_refresh
    write = not a.no_write
    if a.once or not a.loop:
        print(emit(datetime.now(), write), flush=True)
        return 0
    print(f"terminal_extras: every 5 min, {START}-{END} weekdays · writing to {_watch_log() if write else '(screen only)'}", flush=True)
    if a.force or in_market(datetime.now()):
        print(emit(datetime.now(), write), flush=True)
    while True:
        now = datetime.now()
        wake = next_run(now)
        _t.sleep(max(1.0, (wake - now).total_seconds()))
        now = datetime.now()
        if not (a.force or in_market(now)):
            continue
        try:
            print(emit(now, write), flush=True)
        except Exception:                       # noqa: BLE001
            traceback.print_exc()


if __name__ == "__main__":
    sys.exit(main())
