"""
signal_tape.py — today's price-action / 8 EMA signals as ONE table (the SIGNAL TAPE in the briefing terminal).

Why: the scheduler writes every alert into data/agt_briefing_watch.log, but the 15-minute SUPER signals sit among ~190 other blocks
(an old dashboard showed only the last 700 log lines, so a 10:04 signal was gone by 10:30). This reads the WHOLE day, keeps one row per
signal, and checks each row against today's 1-minute candles: did the target (T1) or the stop (SL) come first?

Rules (nothing tuned):
  · a "PA TRIGGERED" line is an entry at the printed price, at the minute it was printed;
  · an "8 EMA SIGNAL" line is an entry only once price breaks the printed level after the signal;
  · stop and target on the same 1-minute candle -> the STOP counts first (the pessimistic reading);
  · rows without a target (8 EMA) are scored on the stop only.
A day of signals is a tape, not a backtest: the "Backtest" column is the app's own verdict from its history tests.

  .venv/bin/python signal_tape.py            # print today's tape from data/agt_briefing_watch.log
  .venv/bin/python signal_tape.py --log some.log
Information only — not financial advice.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

_NUM = r"([\d,]+(?:\.\d+)?)"


def _f(x):
    try:
        return float(str(x).replace(",", ""))
    except Exception:                                           # noqa: BLE001
        return None


def _today_lines(lines):
    """The log is appended day after day: keep only the lines after the last time the briefing clock went backwards (a new day)."""
    start, prev = 0, None
    for i, ln in enumerate(lines):
        m = re.search(r"NIFTY LIVE BRIEFING\s*\|\s*(\d\d):(\d\d)", ln)
        if m:
            t = int(m[1]) * 60 + int(m[2])
            if prev is not None and t < prev - 30:
                start = max(0, i - 2)
            prev = t
    return lines[start:]


def _verdict(text: str) -> str:
    u = text.upper()
    if "NO EDGE" in u:
        return "no edge"
    if "BREAKEVEN" in u or "BETTER THAN RANDOM" in u:
        return "≈ breakeven after costs"
    if "EDGE" in u:
        return "edge"
    if "INVERSE" in u or "WORSE" in u:
        return "worse than random"
    return ""


def parse(lines) -> list:
    """All signals in the (today's) log lines → list of dicts. One row per (time, tier, side, entry)."""
    lines = [x.rstrip("\n") for x in _today_lines(list(lines))]
    out, seen = [], set()
    hdr_t, tier, back, avoid, i = "", "", "", False, 0
    block = []                                                  # signals found in the current block (to attach the backtest line)
    while i < len(lines):
        ln = lines[i].strip()
        if ln.startswith("──── "):
            m = re.search(r"(\d\d:\d\d)", ln)
            hdr_t, tier, back, avoid, block = (m[1] if m else ""), "", "", False, []
        m = re.match(r"^\[(PA \d+m(?: SUPER)?|8EMA \d+m(?: SUPER)?)\]", ln)
        if m:
            tag = m[1]
            tier = ("8EMA 15m" if "8EMA" in tag else "15m SUPER" if "15m" in tag else "5m" if "5m" in tag else "3m")
        if "you avoid this side" in ln:
            avoid = True
        if re.match(r"^(Backtest|backtest)", ln):
            v = _verdict(ln)
            for s in block:
                if not s.get("verdict"):
                    s["verdict"] = v
            back = v
        m = re.match(r"^✅ PA TRIGGERED · (PE|CE) at " + _NUM + r" \((\d\d:\d\d) (.*?)\)", ln)
        if m:
            nxt = lines[i + 1] if i + 1 < len(lines) else ""
            sl = re.search(r"SL " + _NUM, nxt)
            t1 = re.search(r"T1 " + _NUM, nxt)
            s = dict(time=m[3], logged=hdr_t, tier=tier or "?", kind="PA", side=m[1], entry=_f(m[2]), sl=_f(sl[1]) if sl else None,
                     t1=_f(t1[1]) if t1 else None, note=m[4].strip(), verdict=back, avoid=False, wait=False)
            k = (s["time"], s["tier"], s["side"], s["entry"])
            if k not in seen:
                seen.add(k)
                out.append(s)
                block.append(s)
        m = re.match(r"^[🟢🔴]\s*8 EMA SIGNAL · (PE|CE) · (.*)$", ln)
        if m:
            nxt = lines[i + 1].strip() if i + 1 < len(lines) else ""
            e = re.search(r"entry on break of (?:high|low) " + _NUM, nxt)
            sl = re.search(r"SL candle (?:low|high) " + _NUM, nxt)
            s = dict(time=hdr_t, logged=hdr_t, tier=tier or "8EMA 15m", kind="8EMA", side=m[1], entry=_f(e[1]) if e else None,
                     sl=_f(sl[1]) if sl else None, t1=None, note=m[2].split("(")[0].strip(), verdict=back, avoid=False, wait=True)
            k = (s["time"], s["tier"], s["side"], s["entry"])
            if k not in seen and s["entry"]:
                seen.add(k)
                out.append(s)
                block.append(s)
            # the gap-up "avoid this side" flag is on the next lines of the same paragraph
            for j in range(i + 1, min(i + 5, len(lines))):
                if "you avoid this side" in lines[j]:
                    s["avoid"] = True
        i += 1
    return out


def journal(lines) -> list:
    """The app's own "8 EMA JOURNAL" (trailing-stop results of its 8 EMA trades), last copy in today's log."""
    out = []
    for ln in _today_lines(list(lines)):
        m = re.match(r"^\s*#\d+ (PE|CE) in (\d\d:\d\d) @ " + _NUM + r" -> out (\d\d:\d\d) @ " + _NUM + r" \((.*?)\) ([+-]\d+) pts", ln)
        if m:
            out.append(dict(side=m[1], t_in=m[2], entry=_f(m[3]), t_out=m[4], pts=int(m[7]), why=m[6]))
    # the journal is re-printed with every new trade: keep one row per (side, t_in), the latest
    d = {}
    for j in out:
        d[(j["side"], j["t_in"])] = j
    return list(d.values())


def apply_journal(sigs: list, jr: list) -> list:
    """8 EMA rows have no fixed target — show the app's own trailing-stop result when its journal has the trade."""
    def mins(x):
        h, m = map(int, x.split(":"))
        return h * 60 + m
    for s in sigs:
        if s["kind"] != "8EMA":
            continue
        for j in jr:
            if j["side"] == s["side"] and abs(mins(j["t_in"]) - mins(s["time"])) <= 5 and abs((j["entry"] or 0) - (s["entry"] or 0)) <= 4:
                why = "trailed" if "trailing" in j["why"] else "stop"
                s["status"], s["out_t"] = f"{why.upper()} {j['pts']:+d} pts", j["t_out"]
                s["journal"] = True
                break
    return sigs


def evaluate(sigs: list, m1) -> list:
    """Adds status / best move / outcome time to each signal using today's 1-minute candles (columns timestamp, high, low, close)."""
    for s in sigs:
        s.update(status="no candles", best=None, now=None, out_t="")
    if m1 is None or len(m1) == 0:
        return sigs
    df = m1.copy()
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    day = df["timestamp"].dt.date.max()
    df = df[df["timestamp"].dt.date == day].sort_values("timestamp")
    for s in sigs:
        try:
            h, mi = map(int, s["time"].split(":"))
        except Exception:                                       # noqa: BLE001
            continue
        sign = 1 if s["side"] == "CE" else -1
        after = df[(df["timestamp"].dt.hour * 60 + df["timestamp"].dt.minute) > h * 60 + mi]
        if s.get("wait"):                                       # 8 EMA: enters only when the level breaks
            hit = after[after["high"] >= s["entry"]] if sign > 0 else after[after["low"] <= s["entry"]]
            if hit.empty:
                s["status"] = "WAITING (not broken)"
                continue
            after = after[after["timestamp"] >= hit["timestamp"].iloc[0]]
        if after.empty:
            s["status"] = "RUNNING"
            continue
        e, sl, t1 = s["entry"], s["sl"], s["t1"]
        best, status, out_t = 0.0, "RUNNING", ""
        for r in after.itertuples():
            fav = (r.high - e) if sign > 0 else (e - r.low)
            adv_hit = sl is not None and ((r.low <= sl) if sign > 0 else (r.high >= sl))
            tgt_hit = t1 is not None and ((r.high >= t1) if sign > 0 else (r.low <= t1))
            if adv_hit:
                status, out_t = "SL HIT", f"{r.timestamp:%H:%M}"
                break
            best = max(best, fav)
            if tgt_hit:
                status, out_t = "T1 HIT", f"{r.timestamp:%H:%M}"
                break
        last = float(after["close"].iloc[-1])
        if status == "RUNNING" and after["timestamp"].iloc[-1].hour * 60 + after["timestamp"].iloc[-1].minute >= 15 * 60 + 25:
            status = f"OPEN AT CLOSE {round((last - e) * sign):+d}"
        s["status"], s["out_t"], s["best"] = status, out_t, round(best)
        s["now"] = round((last - e) * sign)
    return sigs


MERGE_MIN, MERGE_PTS = 5, 15      # same tier + side, fired within 5 min and 15 pts of each other = ONE setup (chosen, not tuned)


def _mins(x: str) -> int:
    h, m = map(int, x.split(":"))
    return h * 60 + m


def merge(sigs: list) -> list:
    """The price-action engine often fires the same setup 2–3 times a few points apart (13:24 PE ×3 on 05 Oct).
    Keep the FIRST trigger of each cluster and remember how many were folded into it — the count stays honest."""
    out = []
    for s in sorted(sigs, key=lambda x: (x["time"], x["entry"] or 0)):
        twin = next((o for o in out if o["tier"] == s["tier"] and o["side"] == s["side"] and o["kind"] == s["kind"]
                     and 0 <= _mins(s["time"]) - _mins(o["last_t"]) <= MERGE_MIN
                     and abs((s["entry"] or 0) - (o["entry"] or 0)) <= MERGE_PTS), None)
        if twin:
            twin["dups"] = twin.get("dups", 1) + 1
            twin["last_t"] = s["time"]
            twin["lo"], twin["hi"] = min(twin["lo"], s["entry"]), max(twin["hi"], s["entry"])
            continue
        s = dict(s, dups=1, last_t=s["time"], lo=s["entry"], hi=s["entry"])
        out.append(s)
    return out


def tape_rows(sigs: list, limit: int = 12):
    """(rows, note) for the SIGNAL TAPE grid. Newest first; every 15m row is kept."""
    if not sigs:
        return [], "No price-action / 8 EMA signals in today's log yet."
    ordered = sorted(sigs, key=lambda s: (s["time"], s["tier"]), reverse=True)
    keep = [s for s in ordered if "15m" in s["tier"]]
    for s in ordered:
        if len(keep) >= limit:
            break
        if s not in keep:
            keep.append(s)
    keep = sorted(keep, key=lambda s: (s["time"], s["tier"]), reverse=True)
    rows = []
    for s in keep:
        arrow = "▲ CE" if s["side"] == "CE" else "▼ PE"
        stt = s["status"] + (f" {s['out_t']}" if s.get("out_t") else "")
        best = "—" if s.get("best") is None else f"+{s['best']}"
        note = s["note"] + (" · gap-up: app says avoid this side" if s.get("avoid") else "")
        if s.get("dups", 1) > 1:
            note = f"×{s['dups']} same setup ({s['lo']:,.0f}–{s['hi']:,.0f}) · " + note
        if s.get("verdict"):
            note += f" · backtest: {s['verdict']}"
        rows.append([s["time"], s["tier"], arrow, f"{s['entry']:,.0f}", "—" if not s["sl"] else f"{s['sl']:,.0f}",
                     "—" if not s["t1"] else f"{s['t1']:,.0f}", stt, best, note])
    n = len(sigs)
    raw = sum(s.get("dups", 1) for s in sigs)
    t1 = sum(1 for s in sigs if s["status"] == "T1 HIT")
    sl = sum(1 for s in sigs if s["status"] == "SL HIT")
    jr = [s for s in sigs if s.get("journal")]
    run = sum(1 for s in sigs if s["status"].startswith(("RUNNING", "WAITING", "OPEN AT CLOSE")) and not s.get("journal"))
    big = [s for s in sigs if s["tier"] == "15m SUPER" and s["t1"]]
    bt1 = sum(1 for s in big if s["status"] == "T1 HIT")
    note = (f"{n} setup(s) today" + (f" ({raw} raw triggers, repeats merged)" if raw > n else "") + f": target first {t1} · stop first {sl} · open/waiting {run}"
            + (f" · 8 EMA journal trades {len(jr)}: {sum(s['status'].split()[1].lstrip('+') and int(s['status'].split()[1]) for s in jr):+d} pts net" if jr else "")
            + (f" · 15m SUPER with a target: {bt1} of {len(big)} reached it" if big else "")
            + ". 8 EMA rows show the app's own trailing-stop result from its 8 EMA journal. One day is a tape, not a test — see the backtest column. Stop and target on one candle count as the stop.")
    return rows, note


def build(now=None, log_path=None, m1=None):
    """(rows, note) ready for the dashboard; never raises."""
    try:
        p = Path(log_path) if log_path else None
        if p is None:
            import config
            p = Path(config.DATA_DIR) / "agt_briefing_watch.log"
        if not p.exists():
            return [], "No watch log yet."
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        sigs = parse(lines)
        if m1 is None:
            try:
                import smc_brief
                from datetime import date
                m1 = smc_brief._read_1m(1, (now.date() if now is not None else date.today()))
            except Exception:                                   # noqa: BLE001
                m1 = None
        evaluate(sigs, m1)
        apply_journal(sigs, journal(lines))
        sigs = merge(sigs)
        return tape_rows(sigs)
    except Exception as e:                                      # noqa: BLE001
        return [], f"N/A ({type(e).__name__}: {str(e)[:100]})"


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--log")
    ap.add_argument("--candles", help="CSV of today's 1-minute candles (timestamp,high,low,close) — default: the stored ones")
    a = ap.parse_args()
    m = pd.read_csv(a.candles) if a.candles else None
    rows, note = build(log_path=a.log, m1=m)
    import terminal_tables as tt
    print("\n".join(tt.grid("SIGNAL TAPE", ["Time", "Tier", "Side", "Entry", "SL", "T1", "Status", "Best", "Setup / app backtest"], rows, 120, flex=8)))
    print(note)
