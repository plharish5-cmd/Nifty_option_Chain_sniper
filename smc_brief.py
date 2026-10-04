"""
smc_brief.py — SMC in plain English for the briefing terminal.

Prints ONE short block: BUY / SELL / GET READY / WAIT, with the exact trigger
levels, stop and target. No SMC jargon in the main lines — jargon is in
brackets only so you can match it to Tab 19.

    .venv/bin/python smc_brief.py          # auto: 15m on :00/:15/:30/:45, 5m on other 5-min marks, else 3m
    .venv/bin/python smc_brief.py 3m       # force a timeframe (3m | 5m | 15m)
    .venv/bin/python smc_brief.py --replay 2026-09-29            # replay a past day every 15 min (09:30–15:15)
    .venv/bin/python smc_brief.py --replay 2026-09-29 --every 5 --tf 5m --from 09:20 --to 15:00
    .venv/bin/python smc_brief.py --at "2026-10-01 11:05"        # one block as it would have printed then
    Replay prints every block plus a SIGNAL TIMELINE with what NIFTY did 30 / 60 min after each call,
    and saves it to data/replay/smc_brief_<day>.txt. Order flow appears only for days orderflow_recorder.py ran.

HOW IT DECIDES (MJ / Stock Unlimited multi-timeframe rule)
  * 15m candles  -> the TREND (big picture). Never trade against it.
  * 3m/5m/15m    -> the TRIGGER: the 4-condition sniper from smc.py
                    1. price on the cheap side (buy) / expensive side (sell) of the range
                    2. stop hunt done (wick through a low/high, closed back inside)
                    3. trend flip confirmed (candle CLOSED beyond last swing)
                    4. price pulled back into a buy zone / sell zone
  * 4/4 + 15m trend agrees      -> BUY NOW / SELL NOW
  * 3/4 + 15m trend agrees      -> GET READY (one thing missing — it tells you which)
  * anything else               -> WAIT

Rules kept from agt_briefing.py: alert-only, ZERO web calls (reads stored
candle CSVs only), never crashes (prints N/A), thresholds from
strategy1_rules.json ("smc_brief" block). Closed candles only.
Information only — not financial advice.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, time as dtime, timedelta
from pathlib import Path

import pandas as pd

import config

RULES_PATH = Path(__file__).parent / "strategy1_rules.json"
_DEFAULTS = {"days_back": 3, "pivot_n": 2, "trend_tf": "15m",
             "min_status_ready": "ARMED", "session_start": "09:15", "session_end": "15:30"}
_RANK = {"MONITORING": 0, "FORMING": 1, "ARMED": 2, "TRIGGERED": 3}
_TF_MIN = {"3m": 3, "5m": 5, "15m": 15}


def _rules() -> dict:
    try:
        r = json.loads(RULES_PATH.read_text()).get("smc_brief", {})
    except Exception:
        r = {}
    return {**_DEFAULTS, **{k: v for k, v in r.items() if not k.startswith("_")}}


# ── candles (stored CSVs only — no web) ───────────────────────────────────
def _read_dir(d: Path | None, days: int, upto=None) -> pd.DataFrame | None:
    """Last `days` session files on or before `upto` (a date) — so a replay of a past day sees only its past."""
    if not d or not Path(d).exists():
        return None
    files = sorted(Path(d).glob("20*.csv"))
    if upto is not None:
        files = [f for f in files if f.stem[:10] <= f"{upto:%Y-%m-%d}"]
    files = files[-days:]
    frames = []
    for f in files:
        try:
            x = pd.read_csv(f)
            if not x.empty and {"timestamp", "open", "high", "low", "close"} <= set(x.columns):
                frames.append(x)
        except Exception:
            continue
    if not frames:
        return None
    out = pd.concat(frames, ignore_index=True)
    out["timestamp"] = pd.to_datetime(out["timestamp"], errors="coerce")
    try:
        out["timestamp"] = out["timestamp"].dt.tz_localize(None)
    except (TypeError, AttributeError):
        pass
    for c in ("open", "high", "low", "close"):
        out[c] = pd.to_numeric(out[c], errors="coerce")
    return (out.dropna(subset=["timestamp", "close"])
               .drop_duplicates("timestamp").sort_values("timestamp").reset_index(drop=True))


def _read_1m(days: int, upto=None) -> pd.DataFrame | None:
    """1-min NIFTY candles: live folder (config.CANDLE_DIR) + data/candles_hist (backtest download); live wins."""
    parts = [x for x in (_read_dir(Path(config.DATA_DIR) / "candles_hist", days, upto),
                         _read_dir(getattr(config, "CANDLE_DIR", None), days, upto)) if x is not None]
    if not parts:
        return None
    out = pd.concat(parts, ignore_index=True).drop_duplicates("timestamp", keep="last")
    out = out.sort_values("timestamp")
    keep = sorted(out["timestamp"].dt.date.unique())[-days:]
    return out[out["timestamp"].dt.date.isin(keep)].reset_index(drop=True)


def _resample(df1: pd.DataFrame, minutes: int) -> pd.DataFrame:
    parts = []
    for _, day in df1.groupby(df1["timestamp"].dt.date):
        g = (day.set_index("timestamp")
                .resample(f"{minutes}min", origin="start_day", offset="15min")
                .agg({"open": "first", "high": "max", "low": "min", "close": "last"})
                .dropna().reset_index())
        parts.append(g)
    return pd.concat(parts, ignore_index=True) if parts else df1.iloc[0:0]


def load_candles(tf: str, now: datetime, r: dict) -> pd.DataFrame | None:
    mins = _TF_MIN[tf]
    days = int(r["days_back"])
    native = {"5m": getattr(config, "CANDLE_5M_DIR", None),
              "15m": getattr(config, "CANDLE_15M_DIR", None)}.get(tf)
    df = _read_dir(native, days, now.date()) if native else None
    if df is not None and now.date() not in set(df["timestamp"].dt.date):
        df = None                                   # native folder lacks this day → rebuild from 1-min
    if df is None:
        one = _read_1m(days, now.date())
        if one is None:
            return None
        df = _resample(one, mins)
    s0 = dtime(*map(int, r["session_start"].split(":")))
    s1 = dtime(*map(int, r["session_end"].split(":")))
    t = df["timestamp"].dt.time
    df = df[(t >= s0) & (t < s1)]
    # closed candles only: bar start + tf must be <= now
    df = df[df["timestamp"] + timedelta(minutes=mins) <= now]
    df = df[df["timestamp"].dt.date <= now.date()]
    return df.reset_index(drop=True) if len(df) >= 10 else None


# ── plain-English helpers ─────────────────────────────────────────────────
def _plain(line: str) -> str:
    """Turn smc.py condition text into a plain sentence."""
    s = line.lower()
    if "discount" in s and ("below 50%" in s or "pts below" in s):
        return "Price is on the CHEAP side of the range (good place to buy)"
    if "premium" in s and "pts above" in s:
        return "Price is on the EXPENSIVE side of the range (good place to sell)"
    if "not in discount" in s:
        return "Price not yet on the cheap side of the range"
    if "not in premium" in s:
        return "Price not yet on the expensive side of the range"
    if "ssl sweep" in s and "awaiting" not in s:
        return "Stop hunt below the low DONE — sellers' stops taken, price came back up"
    if "bsl sweep" in s and "awaiting" not in s:
        return "Stop hunt above the high DONE — buyers' stops taken, price came back down"
    if "sell-side liquidity" in s:
        return "Waiting for a stop hunt BELOW a recent low (fake breakdown)"
    if "buy-side liquidity" in s:
        return "Waiting for a stop hunt ABOVE a recent high (fake breakout)"
    if "bullish mss" in s and "awaiting" not in s:
        return "Trend flipped UP — a candle CLOSED above the last swing high"
    if "bearish mss" in s and "awaiting" not in s:
        return "Trend flipped DOWN — a candle CLOSED below the last swing low"
    if "awaiting bullish market structure" in s:
        return "Waiting for a candle to CLOSE above the last swing high (trend flip up)"
    if "awaiting bearish market structure" in s:
        return "Waiting for a candle to CLOSE below the last swing low (trend flip down)"
    if "awaiting pullback into bullish" in s:
        return "Waiting for price to dip back into a BUY zone"
    if "awaiting pullback into bearish" in s or "awaiting rally into bearish" in s:
        return "Waiting for price to bounce back into a SELL zone"
    if "retest" in s or "ote" in s:
        return "Price is inside a " + ("BUY" if ("demand" in s or "bullish" in s) else "SELL") + " zone now"
    if "equilibrium undefined" in s:
        return "Range not clear yet (too few swings)"
    return line


def _trend_word(t: str) -> str:
    return {"BULLISH": "UP   (higher highs + higher lows)",
            "BEARISH": "DOWN (lower highs + lower lows)"}.get(t, "SIDEWAYS (no clear direction)")


def _zones(rd: dict, spot: float) -> tuple[list[str], list[str]]:
    """Nearest SELL zones above (sell at top) and BUY zones below (buy at bottom)."""
    sell, buy = [], []
    for ob in rd.get("open_obs", []):
        if ob.direction == "BEARISH" and ob.low > spot:
            sell.append((ob.low, f"{ob.low:,.0f}–{ob.high:,.0f}  sellers' zone (order block)"))
        if ob.direction == "BULLISH" and ob.high < spot:
            buy.append((-ob.high, f"{ob.low:,.0f}–{ob.high:,.0f}  buyers' zone (order block)"))
    for g in rd.get("open_fvgs", []):
        if g.direction == "BEARISH" and g.low > spot:
            sell.append((g.low, f"{g.low:,.0f}–{g.high:,.0f}  gap price may refill (FVG)"))
        if g.direction == "BULLISH" and g.high < spot:
            buy.append((-g.high, f"{g.low:,.0f}–{g.high:,.0f}  gap price may refill (FVG)"))
    for p in rd.get("open_pools", []):
        if p.kind == "EQH" and p.level > spot:
            sell.append((p.level, f"{p.level:,.0f}  double top — stops sit above, may get hunted (EQH)"))
        if p.kind == "EQL" and p.level < spot:
            buy.append((-p.level, f"{p.level:,.0f}  double bottom — stops sit below, may get hunted (EQL)"))
    def top2(xs):
        seen, out = set(), []
        for _, t in sorted(xs):
            if t not in seen:
                seen.add(t); out.append(t)
        return out[:2]
    return top2(sell), top2(buy)


# ── main block ────────────────────────────────────────────────────────────
def build_block(tf: str, now: datetime | None = None) -> str:
    now = now or datetime.now()
    r = _rules()
    hdr = f"──── SMC PLAIN READ · {tf} trigger · 15m trend · {now:%H:%M} ────"
    try:
        import smc
        n = int(r["pivot_n"])
        trig = load_candles(tf, now, r)
        if trig is None:
            return f"{hdr}\nSMC: N/A — not enough closed {tf} candles stored yet."
        trend_df = trig if tf == r["trend_tf"] else load_candles(r["trend_tf"], now, r)
        rt = smc.read(trig, n=n)
        rT = smc.read(trend_df, n=n) if trend_df is not None else rt
        spot = float(trig["close"].iloc[-1])
        trend = getattr(rT.get("structure"), "trend", "TRANSITION")
        sc = rt.get("sniper_confluence") or {}
        bull, bear = sc.get("bullish"), sc.get("bearish")

        out = [hdr, f"Spot {spot:,.1f}  (last closed {tf} candle {trig['timestamp'].iloc[-1]:%H:%M})",
               f"BIG TREND (15m): {_trend_word(trend)}"]
        dr = rt.get("dealing_range")
        if dr:
            side = "CHEAP side → look to BUY only" if dr.position_pct < 0.5 else "EXPENSIVE side → look to SELL only"
            out.append(f"WHERE IN RANGE: {dr.position_pct*100:.0f}% of {dr.low:,.0f}–{dr.high:,.0f} · {side}")
        try:
            import garch_range
            out.append(garch_range.text_line(now.date(), spot))
        except Exception as _ge:                              # noqa: BLE001
            out.append(f"GARCH RANGE: N/A ({_ge})")
        try:
            import evidence_board
            out.append(evidence_board.summary_line())
        except Exception:                                     # noqa: BLE001
            pass

        # Decide
        need = _RANK.get(str(r["min_status_ready"]).upper(), 2)
        def ok(setup, want):  # trend filter: never against 15m trend
            return setup is not None and trend in (want, "TRANSITION") and trend != ("BEARISH" if want == "BULLISH" else "BULLISH")
        cands = []
        if ok(bull, "BULLISH"):
            cands.append(("BUY", bull))
        if ok(bear, "BEARISH"):
            cands.append(("SELL", bear))
        cands.sort(key=lambda x: _RANK.get(x[1].status, 0), reverse=True)
        best = cands[0] if cands else None
        both_hot = len(cands) == 2 and _RANK[cands[0][1].status] == _RANK[cands[1][1].status] >= need

        if best is None or both_hot or _RANK.get(best[1].status, 0) < need:
            blocked = [(a, s) for a, s in (("BUY", bull), ("SELL", bear))
                       if s is not None and _RANK.get(s.status, 0) >= need
                       and (a, s) not in cands]
            if both_hot:
                why = "buy and sell both look half-ready — conflicting, stay out"
            elif blocked:
                a, s = blocked[0]
                why = (f"{a} setup has {s.score}/4 but the 15m trend is "
                       f"{'DOWN' if a == 'BUY' else 'UP'} — don't {a.lower()} against the big trend")
            else:
                why = "no setup has 3 of 4 checks yet"
            out.append(f"\n>>> ACTION: WAIT  — {why}")
            show = [("BUY", bull), ("SELL", bear)]
        else:
            act, s = best
            if s.status == "TRIGGERED":
                out.append(f"\n>>> ACTION: {act} NOW  (all 4 checks ✔, 15m trend agrees)")
                out.append(f"    Entry ~{s.entry_price:,.0f} · Stop {s.stop_loss:,.0f} "
                           f"({s.risk_pts:.0f} pts) · Target 1 {s.target_1:,.0f} · Target 2 {s.target_2:,.0f} ({s.target_name})")
            else:
                out.append(f"\n>>> ACTION: GET READY TO {act}  (3 of 4 checks ✔ — one missing)")
                out.append(f"    If it completes: Stop ~{s.stop_loss:,.0f} · Target {s.target_2:,.0f} ({s.target_name})")
            show = [(act, s)]

        for act, s in show:
            if s is None:
                continue
            out.append(f"\n  {act} checklist ({s.score}/4):")
            out += [f"    ✔ {_plain(x)}" for x in s.conditions_met]
            out += [f"    ✘ {_plain(x)}" for x in s.conditions_pending]

        # Trigger levels
        ah, al = rt.get("active_high"), rt.get("active_low")
        out.append("\n  TRIGGER LEVELS (candle must CLOSE beyond, not just touch):")
        if ah:
            out.append(f"    ▲ {tf} close ABOVE {ah.price:,.0f} → buyers take control (BOS/CHoCH up)")
        if al:
            out.append(f"    ▼ {tf} close BELOW {al.price:,.0f} → sellers take control (BOS/CHoCH down)")
        sell_z, buy_z = _zones(rt, spot)
        if sell_z:
            out.append("  SELL-AT-TOP zones above:")
            out += [f"    • {z}" for z in sell_z]
        if buy_z:
            out.append("  BUY-AT-BOTTOM zones below:")
            out += [f"    • {z}" for z in buy_z]
        last_ev = getattr(rt.get("structure"), "last", None)
        if last_ev:
            what = "trend flip" if (last_ev.is_mss or last_ev.kind in ("CHOCH", "MSS")) else "trend continued"
            out.append(f"  Last {tf} event: {what} {last_ev.direction.lower()} at {last_ev.level:,.0f} ({last_ev.at:%H:%M}) [{last_ev.kind}]")
        out.append("  Info only — not advice. Zones are where to LOOK, the trigger is the CLOSE.")
        try:
            import smc_five_rule as fr
            one = _read_1m(int(r["days_back"]), now.date())
            if one is not None:
                one = one[one["timestamp"] + timedelta(minutes=1) <= now].reset_index(drop=True)
            d5 = load_candles("5m", now, r)
            d15 = load_candles("15m", now, r)
            if d5 is not None:
                res = fr.evaluate(one, d5, d15, now, fr.best_daily(one if one is not None else d5))
                out.append("")
                out.append(fr.text_block(res))
        except Exception as _e:                              # noqa: BLE001
            out.append(f"5-RULE: N/A ({_e})")
        out.append("")
        out.append(orderflow_block(now))
        return "\n".join(out)
    except Exception as e:
        return f"{hdr}\nSMC: N/A ({type(e).__name__}: {e})"


# ── NIFTY 50 order flow (orderflow_recorder.py) ───────────────────────────
def orderflow_block(now: datetime, minutes: int = 5) -> str:
    hdr = f"ORDER FLOW · NIFTY 50 (50 stocks, real aggressive buy/sell) · {minutes}m"
    try:
        import nifty_orderflow as nf
        day = f"{now:%Y-%m-%d}"
        if day not in nf.recorded_days():
            first = (nf.recorded_days() or [None])[0]
            return (f"{hdr}\n  N/A — no order-flow recording for {day}. Order flow cannot be downloaded for past days; it exists"
                    f" only for days orderflow_recorder.py ran" + (f" (first recorded day: {first})." if first else "."))
        data = nf.load_day(day)
        cut = pd.Timestamp(now)
        for k in ("ticks", "index"):
            if len(data[k]):
                data[k] = data[k][data[k]["t"] < cut].reset_index(drop=True)
        if len(data["book"]):
            data["book"] = data["book"][data["book"]["t"] < cut]
        # only fully closed candles
        start = cut.floor(f"{minutes}min") if minutes > 1 else cut.floor("min")
        for k in ("ticks", "index"):
            if len(data[k]):
                data[k] = data[k][data[k]["t"] < start]
        f = nf.compute(data, minutes, day=day)
        if f is None or f.candles.empty:
            return f"{hdr}\n  N/A — no closed candles with classified trades yet."
        R, val = nf.rules(), nf.validation()
        c = f.candles.iloc[-1]
        vw = c.get("vwap")
        L = [hdr,
             f"  Day CVD ₹{f.totals['cvd']:+,.0f} Cr · last candle {c['t']:%H:%M} delta ₹{c['delta']:+,.0f} Cr ({c['delta_pct']:+.0f}%) · "
             f"breadth {int(c['breadth_up'])}↑/{int(c['breadth_dn'])}↓",
             f"  Price vs delta: {f.state['label']}",
             (f"  Flow VWAP {vw:,.1f} → price {'ABOVE (buys only)' if c['c'] > vw else 'BELOW (sells only)'} · "
              f"last {int(R['roll_delta_candles'])} candles delta {c.get('d_roll_pct', 0):+.0f}%") if pd.notna(vw) else "  Flow VWAP: n/a"]
        su = f.setup or {}
        sv = (val.get("setup") or {})
        sst = "VALIDATED" if sv.get("passed") else ("UNTESTED" if not sv else "not proven")
        L.append(f"  DELTA+VWAP SETUP: {su.get('state', 'NONE')} — {su.get('text', '')} [{sst}]")
        for d_, nm in ((1, "LONG / BUY CE"), (-1, "SHORT / BUY PE")):
            s_, why = nf.confirm(f, d_, R)
            L.append(f"  For a {nm} idea: {s_} — {why}")
        ev = f.events[f.events["i"] >= len(f.candles) - 6] if len(f.events) else f.events
        for e in ev.tail(4).itertuples():
            L.append(f"   • {e.t:%H:%M} {nf.SIGNALS[e.kind][0]} — {e.detail} [{nf.status_of(e.kind, val)}]")
        return "\n".join(L)
    except Exception as e:                                    # noqa: BLE001
        return f"{hdr}\n  N/A ({type(e).__name__}: {e})"


# ── replay a past day ─────────────────────────────────────────────────────
def _future_move(one: pd.DataFrame | None, at: datetime, mins: int) -> float | None:
    if one is None or one.empty:
        return None
    a = one[one["timestamp"] <= pd.Timestamp(at) - timedelta(minutes=1)]
    b = one[(one["timestamp"] <= pd.Timestamp(at) + timedelta(minutes=mins - 1)) & (one["timestamp"].dt.date == at.date())]
    if a.empty or b.empty or b["timestamp"].iloc[-1] <= a["timestamp"].iloc[-1]:
        return None
    return float(b["close"].iloc[-1] - a["close"].iloc[-1])


def _summ(block: str) -> tuple[str, str, str]:
    act = next((l.split(">>> ACTION:")[1].strip() for l in block.splitlines() if ">>> ACTION:" in l), "N/A")
    five = next((l.split("→")[1].strip() for l in block.splitlines() if l.startswith("5-RULE SMC MODEL") and "→" in l), "N/A")
    of = next((l.split("DELTA+VWAP SETUP:")[1].split("—")[0].strip() for l in block.splitlines() if "DELTA+VWAP SETUP:" in l),
              "no recording")
    return act, five, of


def replay(day: str, tf: str | None, every: int, t_from: str, t_to: str) -> str:
    d = datetime.strptime(day, "%Y-%m-%d")
    t = datetime.combine(d.date(), dtime(*map(int, t_from.split(":"))))
    end = datetime.combine(d.date(), dtime(*map(int, t_to.split(":"))))
    one = _read_1m(1, d.date())
    if one is None or one.empty or d.date() not in set(one["timestamp"].dt.date):
        return (f"No stored 1-min NIFTY candles for {day}. Download them first:\n"
                f"  .venv/bin/python backtest_five_rule.py --fetch 10   (saves data/candles_hist/)")
    blocks, rows = [], []
    while t <= end:
        tf_ = tf or auto_tf(t.time())
        b = build_block(tf_, t)
        blocks.append(f"\n{'═' * 100}\nREPLAY {day} {t:%H:%M}\n{b}")
        act, five, of = _summ(b)
        rows.append((t, tf_, act, five, of, _future_move(one, t, 30), _future_move(one, t, 60)))
        t += timedelta(minutes=every)
    L = [f"\n{'═' * 100}\nSIGNAL TIMELINE {day} · what NIFTY did AFTER each call (close-to-close, points)\n{'═' * 100}",
         f"{'time':<6}{'tf':<5}{'SMC action':<44}{'5-Rule':<34}{'Order-flow setup':<18}{'+30m':>7}{'+60m':>7}"]
    for t_, tf_, act, five, of, m30, m60 in rows:
        f30 = f"{m30:+.0f}" if m30 is not None else "—"
        f60 = f"{m60:+.0f}" if m60 is not None else "—"
        L.append(f"{t_:%H:%M} {tf_:<5}{act[:43]:<44}{five[:33]:<34}{of[:17]:<18}{f30:>7}{f60:>7}")
    calls = [(a, m) for _, _, a, _, _, _, m in rows if a.startswith(("BUY NOW", "SELL NOW")) and m is not None]
    if calls:
        good = sum(1 for a, m in calls if (m > 0) == a.startswith("BUY"))
        L.append(f"\nSMC BUY NOW / SELL NOW calls: {len(calls)} · right direction after 60 min: {good}/{len(calls)}")
    takes = [(f_, m) for _, _, _, f_, _, _, m in rows if f_.startswith("TAKE TRADE") and m is not None]
    if takes:
        good = sum(1 for f_, m in takes if (m > 0) == ("BUY" in f_))
        L.append(f"5-Rule TAKE TRADE calls: {len(takes)} · right direction after 60 min: {good}/{len(takes)}")
    if not calls and not takes:
        L.append("\nNo firm calls today (SMC BUY/SELL NOW or 5-Rule TAKE TRADE).")
    L.append("One day is an anecdote, not a test — use backtest_five_rule.py / backtest_orderflow.py for evidence. "
             "Information only — not financial advice.")
    return "\n".join(blocks + L)


def auto_tf(t: dtime) -> str:
    if t.minute % 15 == 0:
        return "15m"
    if t.minute % 5 == 0:
        return "5m"
    return "3m"


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("tf", nargs="?", choices=list(_TF_MIN))
    ap.add_argument("--replay", help="YYYY-MM-DD: replay a past session")
    ap.add_argument("--at", help='"YYYY-MM-DD HH:MM": one block as of that moment')
    ap.add_argument("--tf", dest="tf2", choices=list(_TF_MIN), help="fixed trigger timeframe in replay")
    ap.add_argument("--every", type=int, default=15)
    ap.add_argument("--from", dest="t_from", default="09:30")
    ap.add_argument("--to", dest="t_to", default="15:15")
    a = ap.parse_args()
    if a.replay or a.at:
        import runlog
        runlog.start(f"smc_brief_{'replay_' + a.replay if a.replay else 'at'}")
    if a.replay:
        txt = replay(a.replay, a.tf2 or a.tf, a.every, a.t_from, a.t_to)
        print(txt)
        try:
            out = Path(config.DATA_DIR) / "replay"
            out.mkdir(parents=True, exist_ok=True)
            (out / f"smc_brief_{a.replay}.txt").write_text(txt)
            print(f"\nSaved {out / f'smc_brief_{a.replay}.txt'}")
        except Exception:
            pass
    else:
        now = datetime.strptime(a.at, "%Y-%m-%d %H:%M") if a.at else datetime.now()
        tf = a.tf or a.tf2 or auto_tf(now.time())
        print(build_block(tf, now))
