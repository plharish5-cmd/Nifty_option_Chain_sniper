"""
mentor_oc_watch.py — his "refresh every 3-5 minutes" done for you: reads the latest stored
option-chain snapshot and sends a Telegram message ONLY when something changes:
  - writer side flips (put writers <-> call writers aggressive)
  - the zone state changes (breaks above the resistance zone / below the support zone)
  - an ITM trap appears or ITM writers start closing
  - (Lecture 5) a new BOS / CHoCH on the 15-min structure, a new order-block zone, or a zone tap
  - (Lecture 7) a new two-candle double-bottom/top zone that passes 1:1, its retest, a swing sweep,
    and the first 15-min close outside the swing range
The first run of the session sends one summary. Alert-only: it never places orders.

  .venv/bin/python mentor_oc_watch.py            (the systemd timer runs it every 3 min, 09:20-15:30)
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import date, datetime

import pandas as pd

import mentor_option_chain as m

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("mentor_oc_watch")


def _log_to_watch_file(text: str) -> None:
    """Also write every mentor message into data/agt_briefing_watch.log (and so into the dated daily copies)."""
    try:
        import config
        p = config.DATA_DIR / "agt_briefing_watch.log"
        with open(p, "a", encoding="utf-8") as f:
            f.write(f"\n──── 🟧 MENTOR {datetime.now():%H:%M} ────\n{text}\n\n")
    except Exception as exc:
        log.warning("could not write the mentor message to agt_briefing_watch.log: %s", exc)


def _send(text: str) -> None:
    _log_to_watch_file(text)
    try:
        from scheduler import send_telegram_alert
        if not send_telegram_alert(text):
            log.warning("Telegram not sent (see scheduler log); message was:\n%s", text)
    except Exception as exc:
        log.warning("Telegram unavailable (%s); message was:\n%s", exc, text)


_DAY = {}


def day_trend() -> int:
    """Day structure (+1 bullish / -1 bearish / 0) from the daily chart, once per run."""
    if "t" not in _DAY:
        _DAY["t"] = 0
        try:
            import participant_correlation as pc
            import mentor_structure as ms
            import mentor_sweeps as msw
            dd = pc.load_ohlc()
            if dd is not None and len(dd) > 30:
                _DAY["t"] = int(ms.analyse(msw.daily_frame(dd).reset_index(drop=True)).trend)
        except Exception as exc:
            log.info("day trend unavailable: %s", exc)
    return _DAY["t"]


def against_day(side: str) -> bool:
    """True when the signal fights the day trend and the rules say to keep those off Telegram."""
    try:
        import mentor_sweeps as msw
        on = msw.rules().get("telegram", {}).get("only_with_day_trend", True)
    except Exception:
        on = True
    t = day_trend()
    return bool(on and t != 0 and t == (-1 if side == "bull" else 1))


def structure_changes(old: dict) -> tuple[list[str], dict]:
    """Lecture 5 events on today's 15-min candles since the last run."""
    try:
        import mentor_structure as ms
        R = ms.rules()
        c, src = ms.recent_candles(3, R)
        if c is None or len(c) < 20:
            return [], {}
        r = ms.analyse(c, R)
        ms.simulate(c, r, R)
        ts = c["timestamp"]
        today = date.today()
        ev = [f"{e['kind']}@{ts.iloc[e['idx']]:%H:%M}:{e['price']:.0f}" for e in r.events
              if e["kind"] in ("BOS_UP", "BOS_DN", "CHOCH_UP", "CHOCH_DN", "SHIFT") and ts.iloc[e["idx"]].date() == today]
        zones = {f"{o.side}:{ts.iloc[o.ob_idx]:%m%d%H%M}": o for o in r.obs if ts.iloc[o.valid_from].date() == today
                 and o.rank != "MIDDLE" and not o.result.startswith("CANCELLED")}      # Lecture 6: he deletes middle OBs
        taps = [k for k, o in zones.items() if o.tap_idx is not None]
        v = ms.load_verdict(R)
        ob_tag = (f" [{v['verdict'].lower()}: {v['avg_r']:+.2f} R per trade vs {v['control_r']:+.2f} random, "
                  f"{v['trades']} trades]" if v and v.get("verdict") != "BEATS RANDOM" else "")
        msgs = []
        for e in sorted(set(ev) - set(old.get("s_events", []))):
            kind, rest = e.split("@", 1)
            msgs.append(f"Structure ({src} 15m): {kind.replace('_', ' ')} at {rest.split(':')[0]}:{rest.split(':')[1]} "
                        f"level {rest.split(':')[2]}")
        for k in sorted(set(zones) - set(old.get("s_zones", []))):
            o = zones[k]
            if against_day(o.side):
                continue
            msgs.append(f"New {'BUY' if o.side == 'bull' else 'SELL'} zone {o.zone_lo:,.0f}-{o.zone_hi:,.0f} ({o.rank}"
                        f"{', sweep' if o.sweep else ''}) SL {o.sl:,.0f} T1 {o.t1:,.0f} T2 {o.t2:,.0f}{ob_tag}")
        for k in sorted(set(taps) - set(old.get("s_taps", []))):
            o = zones[k]
            if against_day(o.side):
                continue
            msgs.append(f"Zone TAPPED: {'BUY' if o.side == 'bull' else 'SELL'} {o.zone_lo:,.0f}-{o.zone_hi:,.0f} "
                        f"-> SL {o.sl:,.0f}, T1 {o.t1:,.0f}, T2 {o.t2:,.0f} (confirm with price action){ob_tag}")
        return msgs, {"s_events": sorted(set(ev)), "s_zones": sorted(zones), "s_taps": sorted(taps)}
    except Exception as exc:
        log.warning("structure check skipped: %s", exc)
        return [], {}


def sweep_changes(old: dict) -> tuple[list[str], dict]:
    """Lecture 7 events on today's 15-min candles since the last run."""
    try:
        import mentor_structure as ms
        import mentor_sweeps as msw
        R = msw.rules()
        c, src = ms.recent_candles(3)
        if c is None or len(c) < 20:
            return [], {}
        c = c.reset_index(drop=True)
        ts = c["timestamp"]
        today = date.today()
        dz = msw.double_zones(c, R)
        zones, retests = {}, {}
        for _, z in dz.iterrows():
            if ts.iloc[int(z["formed_idx"])].date() != today:
                continue
            bull = z["side"] == "bull"
            entry = z["zone_hi"] if bull else z["zone_lo"]
            sl = z["zone_lo"] - R["risk"]["sl_buffer_points"] if bull else z["zone_hi"] + R["risk"]["sl_buffer_points"]
            rr = abs(z["target"] - entry) / abs(entry - sl) if entry != sl else 0
            if rr < R["risk"]["min_rr"]:
                continue
            key = f"{z['side']}:{ts.iloc[int(z['bottom_idx'])]:%H%M}"
            zones[key] = (z, entry, sl, rr)
            if z["entry_idx"] is not None and not pd.isna(z["entry_idx"]):
                retests[key] = (z, entry, sl, rr)
        V = msw.load_verdicts(R).get("rules", {})
        day_trend_ = day_trend()
        dbl = V.get("double_15m", {})
        weak_dbl = (f" [weak in backtest: {dbl['verdict'].lower()}]"
                    if dbl and dbl.get("level", 0) <= 0 else "")
        sw = msw.sweep_signals(c, R)
        swings = {}
        for _, x in sw[sw["kind"] == "swing"].iterrows() if len(sw) else []:
            if ts.iloc[int(x["idx"])].date() == today:
                swings[f"{x['side']}:{ts.iloc[int(x['idx'])]:%H%M}"] = x
        sr = msw.swing_range_days(c, R)
        brk = ""
        if len(sr) and sr.iloc[-1]["day"] == today and sr.iloc[-1]["break"]:
            brk = str(sr.iloc[-1]["break"])
        # Lecture 6: body-void rejections today (tagged with the backtest verdict)
        bvs = {}
        bv = msw.body_voids(c, R)
        if len(bv):
            for _, x in bv[bv["entry"].notna()].iterrows():
                k_ = int(x["touch_idx"])
                if ts.iloc[k_].date() == today and x["passes_rr"]:
                    bvs[f"{x['side']}:{ts.iloc[k_]:%H%M}"] = x
        vb = V.get("body_void_15m", {})
        tag_bv = f" [backtest: {vb['verdict'].lower()}]" if vb else " [not backtested yet]"
        vp_msg = None
        if datetime.now().strftime("%H:%M") >= R["volume_profile"].get("alert_after", "15:00"):
            fm = msw.futures_minutes(3)
            if fm is not None:
                vp = msw.vp_divergence(fm, R)
                if len(vp) and vp.iloc[-1]["day"] == today and vp.iloc[-1].get("divergence"):
                    vd = V.get("vp_divergence", {})
                    vp_msg = (f"Volume-profile divergence forming today: {vp.iloc[-1]['divergence']} (close "
                              f"{vp.iloc[-1]['close']:,.0f}, POC {vp.iloc[-1]['poc']:,.0f}) -> he expects momentum "
                              f"tomorrow (buys a call AND a put)" + (f" [backtest: {vd['verdict'].lower()}]" if vd else ""))
        msgs = []
        for k in sorted(set(bvs) - set(old.get("w_bvoids", []))):
            x = bvs[k]
            if against_day(x["side"]):
                continue
            body = (f"Body void {x['bottom']:,.0f}-{x['top']:,.0f}: no close beyond -> {'BUY' if x['side'] == 'bull' else 'SELL'} "
                    f"at {x['entry']:,.0f}, SL {x['sl']:,.0f} (wick), target {x['target']:,.0f} (R:R {x['rr']:.1f})")
            sgn = 1 if x["side"] == "bull" else -1
            trend_txt = ("with the day trend" if day_trend_ == sgn else "against the day trend" if day_trend_ == -sgn
                         else "day trend undecided")
            if vb and vb.get("level", 0) > 0:
                msgs.append(f"★ MAIN SIGNAL (body void, Lecture 6; backtest {vb['detail']}; {trend_txt}):\n" + body)
            else:
                msgs.append(body + tag_bv)
        if vp_msg and not old.get("w_vp"):
            msgs.append(vp_msg)
        for k in sorted(set(zones) - set(old.get("w_zones", []))):
            z, entry, sl, rr = zones[k]
            if against_day(z["side"]):
                continue
            msgs.append(f"New {'BUY' if z['side'] == 'bull' else 'SELL'} two-candle zone {z['zone_lo']:,.0f}-{z['zone_hi']:,.0f}: "
                        f"entry on retest {entry:,.0f}, SL {sl:,.0f}, target {z['target']:,.0f} (R:R {rr:.1f}){weak_dbl}")
        for k in sorted(set(retests) - set(old.get("w_retests", []))):
            z, entry, sl, rr = retests[k]
            if against_day(z["side"]):
                continue
            msgs.append(f"Two-candle zone RETESTED: {'BUY' if z['side'] == 'bull' else 'SELL'} {entry:,.0f}, SL {sl:,.0f}, "
                        f"target {z['target']:,.0f}{weak_dbl}")
        for k in sorted(set(swings) - set(old.get("w_swings", []))):
            x = swings[k]
            if against_day(x["side"]):
                continue
            sgn = 1 if x["side"] == "bull" else -1
            with_day = day_trend_ == sgn
            vs0 = V.get("swing_sweep_15m", {})
            main_sig = with_day and bool(x["passes_rr"]) and vs0.get("level", 0) > 0   # star only if the backtest backs it
            vs = V.get("swing_sweep_15m", {})
            body = (f"Swing {'LOW' if x['side'] == 'bull' else 'HIGH'} {x['level']:,.0f} swept and closed back "
                    f"-> {'BUY' if sgn > 0 else 'SELL'} at {x['entry']:,.0f}, SL {x['sl']:,.0f}, target {x['target']:,.0f} "
                    f"(R:R {x['rr']:.1f}{'' if x['passes_rr'] else ', below his 1:1'})")
            if main_sig:
                msgs.append("★ MAIN SIGNAL (swing sweep WITH the day trend" + (f"; backtest {vs['detail']}" if vs else "")
                            + "):\n" + body)
            else:
                why = ("against the day trend" if day_trend_ == -sgn else "day trend undecided" if day_trend_ == 0 else "")
                if not x["passes_rr"]:
                    why = (why + ", " if why else "") + "R:R below 1"
                msgs.append(body + f" [secondary: {why}]")
        if brk and brk != old.get("w_break", ""):
            r = sr.iloc[-1]
            msgs.append(f"15-min close {'ABOVE the swing high' if brk == 'UP' else 'BELOW the swing low'} "
                        f"{r['swing_high'] if brk == 'UP' else r['swing_low']:,.0f} at {r['break_time']:%H:%M}: "
                        + (f"entry {r['entry']:,.0f}, SL {r['stop']:,.0f}, T1 (imbalance edge) {r['target']:,.0f}, "
                           f"R:R {r['rr']}" + ("" if r["rr"] and r["rr"] >= R["risk"]["min_rr"] else " - below his 1:1, skip")
                           if r["target"] is not None and not pd.isna(r["target"]) else "no imbalance target - no trade"))
        return msgs, {"w_zones": sorted(zones), "w_retests": sorted(retests), "w_swings": sorted(swings), "w_break": brk,
                      "w_bvoids": sorted(bvs), "w_vp": bool(vp_msg) or bool(old.get("w_vp"))}
    except Exception as exc:
        log.warning("Lecture 7 check skipped: %s", exc)
        return [], {}


def level_changes(old: dict) -> tuple[list[str], dict]:
    """Rejection wick at a support/resistance level + follow-through (15-min and 3-min), new since the last run."""
    try:
        import mentor_levels as ml
        sig = ml.live_signals()
        R = ml.rules()
        msgs, keys = [], []
        for x in sig:
            k = f"{x['tf']}:{pd.Timestamp(x['time']):%H:%M}:{x['side']}:{x['level']:.0f}"
            keys.append(k)
            if k in old.get("l_sigs", []) or not x["passes"]:
                continue
            if R["telegram"].get("only_with_day_trend") and against_day(x["side"]):
                continue
            sgn = 1 if x["side"] == "bull" else -1
            dt = day_trend()
            trend_txt = "with the day trend" if dt == sgn else "AGAINST the day trend" if dt == -sgn else "day trend undecided"
            body = ml.text(x) + f" [{trend_txt}]"
            if x.get("verdict_level", 0) > 0:
                msgs.append(f"★ MAIN SIGNAL (rejection at level; backtest {x['verdict_detail']}):\n" + body)
            else:
                msgs.append(body + f" [backtest: {x.get('verdict', 'not tested').lower()}]")
        return msgs, {"l_sigs": sorted(set(keys) | set(old.get("l_sigs", [])))}
    except Exception as exc:
        log.warning("level check skipped: %s", exc)
        return [], {}


def champion_changes(old: dict) -> tuple[list[str], dict]:
    """World-champion video (champion_dat.py): direction -> discount/premium zone -> 15-min pin bar trigger."""
    try:
        import champion_dat as cd
        L = cd.live()
        if L.get("error"):
            return [], {}
        msgs = []
        z = L.get("zone")
        zkey = f"{L['dir']}:{round(z['far']) if z else ''}"
        if zkey != old.get("c_zone") and z:
            msgs.append("DAT (champion): " + cd.text({**L, "trigger": None}).replace("\n", " | "))
        x = L.get("trigger")
        tkey = f"{pd.Timestamp(x['time']):%H:%M}:{x['side']}" if x else ""
        if x and tkey not in old.get("c_trig", []):
            V = cd.load_verdict()
            v = V.get("volume_hours" if x["in_volume_hours"] else "all", {})
            body = cd.text({**L, "zone": None}).split("\n")[-1]
            if v.get("level", 0) > 0:
                msgs.append(f"★ MAIN SIGNAL (champion D-A-T; backtest {v['detail']}):\n" + body)
            else:
                msgs.append(body + f" [backtest: {v.get('verdict', 'not tested').lower()}]")
        return msgs, {"c_zone": zkey, "c_trig": sorted(set(old.get("c_trig", [])) | ({tkey} if tkey else set()))}
    except Exception as exc:
        log.warning("champion check skipped: %s", exc)
        return [], {}


def cliff_changes(old: dict) -> tuple[list[str], dict]:
    """A max-OI wall broken since the last snapshot, with how thin the OI behind it is (mentor_traps.py)."""
    try:
        import mentor_traps as mt
        x = mt.live_cliff()
        if not x or not x["break"]:
            return [], {}
        b = x["break"]
        k = f"{b['side']}:{b['strike']:.0f}"
        if k in old.get("c_breaks", []):
            return [], {}
        v = mt.load_verdicts().get("cliff", {})
        tag = f" [backtest: {v.get('verdict', 'not tested').lower()}" + (f", {v['detail']}" if v.get("detail") else "") + "]"
        msg = mt.cliff_text(x) + tag
        if v.get("level", 0) > 0:
            msg = "★ MAIN SIGNAL (thin cliff behind a broken wall):\n" + msg
        return [msg], {"c_breaks": old.get("c_breaks", []) + [k]}
    except Exception as exc:
        log.warning("cliff check skipped: %s", exc)
        return [], {}


def pa_changes(old: dict, spot: float | None = None) -> tuple[dict, dict]:
    """Your price-action method (pa_signals.py) on 15-min (SUPER), 5-min and 3-min candles: new setups on closed
    candles, the follow-through trigger (live spot, so it fires within ~3 min), and exit hints for fired trades.
    Returns ({tf: [messages]}, state)."""
    try:
        import pa_signals as pa
        base = pa.rules()
        Tg = base["telegram"]
        L = pa.live()
        ctx = L["ctx"]
        seen, trig, exits = set(old.get("pa_seen", [])), set(old.get("pa_trig", [])), set(old.get("pa_exit", []))
        trig_t = dict(old.get("pa_trig_t", {}))          # when each trade fired: exit hints only on LATER candles
        out = {}
        for tf in base.get("timeframes", ["15m", "5m", "3m"]):
            if tf not in L or (tf != "15m" and not Tg.get(f"send_{tf}", True)):
                continue
            s, R, last, c = L[tf]["setups"], L[tf]["R"], L[tf]["last_idx"], L[tf]["candles"]
            if not len(s):
                continue
            verdict = pa.verdict_text(tf)
            msgs = []
            for _, x in s.iterrows():
                k = f"{tf}|{pd.Timestamp(x['time']):%H:%M}:{x['side']}"
                buy = x["side"] == "CE"
                fresh = last - int(x["idx"]) <= R["trigger"]["trigger_bars"]
                if k not in seen:
                    seen.add(k)
                    if Tg.get("send_setups", True) and fresh:
                        msgs.append(pa.fmt_setup(x.to_dict(), ctx, R, verdict, tf))
                if k in trig or not fresh:
                    continue
                fired = bool(x["triggered"])
                if not fired and spot is not None and not isinstance(x.get("skip"), str):
                    brk = x["pattern_high"] + R["trigger"]["buffer_points"] if buy else x["pattern_low"] - R["trigger"]["buffer_points"]
                    if (spot >= brk) if buy else (spot <= brk):
                        sl = x["pattern_low"] - R["risk"]["sl_buffer_points"] if buy else x["pattern_high"] + R["risk"]["sl_buffer_points"]
                        risk = abs(brk - sl)
                        if R["risk"]["min_sl_points"] <= risk <= R["risk"]["max_sl_points"]:
                            x = x.copy()
                            x["entry"], x["sl"], x["risk"], x["trigger_time"] = brk, sl, risk, pd.Timestamp.now()
                            x["t1"] = brk + (1 if buy else -1) * R["exit"]["fallback_r"] * risk
                            fired = True
                if fired and Tg.get("send_triggers", True):
                    trig.add(k)
                    trig_t[k] = pd.Timestamp(x["trigger_time"]).isoformat()
                    msgs.append(pa.fmt_trigger(x.to_dict(), tf))
            # exit hints for this timeframe's fired trades
            mine = sorted(k for k in trig if k.startswith(tf + "|"))
            if Tg.get("send_exit_hints", True) and mine:
                ema = c["close"].ewm(span=R["exit"]["ema_span"], adjust=False).mean()
                lc, le = float(c["close"].iloc[-1]), float(ema.iloc[-1])
                tlast = pd.Timestamp(c["timestamp"].iloc[-1])
                for k in mine:
                    side, t0 = k.split(":")[-1], k.split("|")[1][:5]
                    fired_at = pd.Timestamp(trig_t.get(k, "1970-01-01"))
                    if tlast + pd.Timedelta(minutes=pa.TF_MIN[tf]) <= fired_at:     # that candle closed before the entry
                        continue
                    opp = s[(s["side"] != side) & (s["idx"] == last)]
                    if len(opp) and f"{k}|opp{tlast:%H%M}" not in exits:
                        exits.add(f"{k}|opp{tlast:%H%M}")
                        y = opp.iloc[0]
                        msgs.append(f"{pa.TF_TAG[tf]} ⚠️ EXIT HINT for the {t0} {side}: {y['pattern']} at {y['levels']} "
                                    f"on the {tlast:%H:%M} candle - you exit {side} trades here")
                    if ((lc < le) if side == "CE" else (lc > le)) and f"{k}|ema" not in exits:
                        exits.add(f"{k}|ema")
                        msgs.append(f"{pa.TF_TAG[tf]} ⚠️ EXIT HINT for the {t0} {side}: {tlast:%H:%M} candle closed "
                                    f"{'below' if side == 'CE' else 'above'} the {tf} 8 EMA ({lc:,.0f} vs {le:,.0f})")
            if msgs:
                out[tf] = msgs
        return out, {"pa_seen": sorted(seen), "pa_trig": sorted(trig), "pa_exit": sorted(exits), "pa_trig_t": trig_t}
    except Exception as exc:
        log.warning("price-action check skipped: %s", exc)
        return {}, {}


def ema8_changes(old: dict, spot: float | None = None) -> tuple[dict, dict]:
    """Your 8 EMA method (ema8_signals.py): close above / below the 8 EMA, follow-through break, SL = signal candle
    low / high; exit hints (TSL / doji / BB / 8 EMA) for fired trades. Returns ({tf: [messages]}, state)."""
    try:
        import ema8_signals as e8
        base = e8.rules()
        Tg = base["telegram"]
        L = e8.live()
        seen, trig, exits = set(old.get("e8_seen", [])), set(old.get("e8_trig", [])), set(old.get("e8_exit", []))
        trig_t = dict(old.get("e8_trig_t", {}))
        out = {}
        for tf in base.get("timeframes", ["15m", "5m", "3m"]):
            if tf not in L or (tf != "15m" and not Tg.get(f"send_{tf}", True)):
                continue
            s, R, last, c = L[tf]["signals"], L[tf]["R"], L[tf]["last_idx"], L[tf]["candles"]
            msgs = []
            verdict = e8.verdict_text(tf)
            for _, x in (s.iterrows() if len(s) else []):
                k = f"{tf}|{x['time']:%H:%M}:{x['side']}"
                fresh = last - int(x["idx"]) <= R["trigger"]["trigger_bars"]
                if k not in seen:
                    seen.add(k)
                    if Tg.get("send_signals", True) and fresh:
                        msgs.append(e8.fmt_signal(x.to_dict(), R, verdict, tf))
                if k in trig or not fresh or isinstance(x.get("skip"), str):
                    continue
                buy = x["side"] == "CE"
                fired = bool(x["triggered"])
                if not fired and spot is not None and ((spot >= x["entry"]) if buy else (spot <= x["entry"])):
                    x = x.copy()
                    x["risk"], x["trigger_time"], fired = abs(x["entry"] - x["sl"]), pd.Timestamp.now(), True
                if fired and Tg.get("send_triggers", True):
                    trig.add(k)
                    trig_t[k] = pd.Timestamp(x["trigger_time"]).isoformat()
                    msgs.append(e8.fmt_trigger(x.to_dict(), tf))
            if Tg.get("send_exit_hints", True):
                tlast = pd.Timestamp(c["timestamp"].iloc[-1])
                for k in sorted(q for q in trig if q.startswith(tf + "|")):
                    if tlast + pd.Timedelta(minutes=e8.TF_MIN[tf]) <= pd.Timestamp(trig_t.get(k, "1970-01-01")):
                        continue
                    for hid, txt in e8.exit_hints(tf, k, k.split(":")[-1], c, R):
                        if hid not in exits:
                            exits.add(hid)
                            msgs.append(txt)
            if msgs:
                out[tf] = msgs
        # CSV journal (data/ema8_journal): fill missed days once a day, rewrite today's file on each new 15-min candle
        jlast, jfill = old.get("e8_jlast", ""), old.get("e8_jfill", "")
        try:
            import ema8_journal as ej
            if jfill != f"{datetime.now():%Y-%m-%d}":
                ej.fill(only_missing=True)
                jfill = f"{datetime.now():%Y-%m-%d}"
            if "15m" in L:
                c15 = L["15m"]["candles"]
                t15 = f"{pd.Timestamp(c15['timestamp'].iloc[-1]):%Y-%m-%d %H:%M}"
                if t15 != jlast and ej.update_today(c15):
                    jlast = t15
        except Exception as exc:
            log.warning("8 EMA journal CSV skipped: %s", exc)
        jday = old.get("e8_journal", "")
        if Tg.get("send_day_journal", True) and datetime.now().strftime("%H:%M") >= Tg.get("day_journal_after", "15:20") \
                and jday != f"{datetime.now():%Y-%m-%d}":
            txt = e8.day_journal_text()
            if txt:
                out.setdefault("15m", []).append(txt)
                jday = f"{datetime.now():%Y-%m-%d}"
        return out, {"e8_seen": sorted(seen), "e8_trig": sorted(trig), "e8_exit": sorted(exits), "e8_trig_t": trig_t,
                     "e8_journal": jday, "e8_jlast": jlast, "e8_jfill": jfill}
    except Exception as exc:
        log.warning("8 EMA check skipped: %s", exc)
        return {}, {}


def expiry_changes(old: dict) -> tuple[list[str], dict]:
    """Final class: on the Nifty monthly-expiry day send the +-300 straddle levels at the open, and after 15:15
    a note if the day is closing red (student's BTST rule). Alert-only, backtest numbers in the message."""
    try:
        import participant_correlation as pc
        import mentor_extras as mx
        import mentor_structure as ms
        today = pd.Timestamp(date.today())
        days = pd.bdate_range(today - pd.Timedelta(days=14), today + pd.Timedelta(days=21))
        cal = pc.expiry_calendar(days, pc.rules())
        if not bool(cal.loc[today, "is_monthly_expiry"]) if today in cal.index else True:
            return [], {}
        X = mx.rules()
        c, _ = ms.recent_candles(1)
        if c is None or not len(c):
            return [], {}
        c = c[pd.to_datetime(c["timestamp"]).dt.date == date.today()]
        if not len(c):
            return [], {}
        o, last = float(c["open"].iloc[0]), float(c["close"].iloc[-1])
        pts = X["expiry_straddle"]["points"]
        msgs, st = [], {"e_open": True, "e_red": bool(old.get("e_red"))}
        if not old.get("e_open"):
            msgs.append(f"MONTHLY EXPIRY today. His straddle: open {o:,.0f} -> targets {o + pts:,.0f} / {o - pts:,.0f} "
                        f"(+-{pts}) before the next weekly expiry; next-week call + put ~50 pts OTM, exit both at the "
                        f"target. Backtest: hit in 75% of months since 2019 (82% since 2023) - option cost not included, "
                        f"check premiums. Levels from the index open; he uses the futures open.")
        if datetime.now().strftime("%H:%M") >= "15:15" and last < o and not old.get("e_red"):
            msgs.append(f"Monthly expiry closing RED ({last:,.0f} < open {o:,.0f}). Student's BTST rule: next day "
                        f"often goes up - backtest: next-day high >= close+100 in 61% (normal red day 45%), 44 cases.")
            st["e_red"] = True
        return msgs, st
    except Exception as exc:
        log.warning("expiry check skipped: %s", exc)
        return [], {}


def main() -> int:
    R = m.rules()
    W = R["watch"]
    now = datetime.now()
    if not (W["start"] <= now.strftime("%H:%M") <= W["end"]) or now.weekday() >= 5:
        return 0
    r = m.latest_reading(date.today(), R)
    if not r:
        log.info("no snapshots yet")
        return 0
    state_path = m.HERE / W["state_file"]
    old = {}
    if state_path.exists():
        try:
            old = json.loads(state_path.read_text())
        except ValueError as exc:
            log.warning("state file unreadable (%s) — starting fresh", exc)
    if old.get("day") != str(date.today()):
        old = {}
    traps = sorted(f"{x['side']}:{x['strike']}:{x['state']}" for x in r["traps"])
    new = {"day": str(date.today()), "writer": r["totals"]["writer_side"], "zone": r["zones"]["zone_state"],
           "traps": traps, "last": r["time"].isoformat()}
    same_snapshot = new["last"] == old.get("last")
    msgs = []
    if not old:
        msgs.append("First read of the session:\n" + m.summary_text(r))
    elif not same_snapshot:
        if new["writer"] != old.get("writer"):
            msgs.append(f"Writer side flipped: {old.get('writer')} -> {new['writer']} "
                        f"(net chg OI {r['totals']['net_chg_oi']:+,.0f})")
        if new["zone"] != old.get("zone"):
            msgs.append(f"Zone: {new['zone']}")
        added = set(traps) - set(old.get("traps", []))
        for key in sorted(added):
            tr = next(x for x in r["traps"] if f"{x['side']}:{x['strike']}:{x['state']}" == key)
            msgs.append(f"ITM {tr['side']} writers {tr['state']} at {tr['strike']} ({tr['pts_itm']} pts ITM): {tr['meaning']}")
    s_msgs, s_state = structure_changes(old)
    msgs += s_msgs
    new.update(s_state or {k: old.get(k, []) for k in ("s_events", "s_zones", "s_taps")})
    w_msgs, w_state = sweep_changes(old)
    msgs += w_msgs
    new.update(w_state or {k: old.get(k, "" if k == "w_break" else False if k == "w_vp" else [])
                           for k in ("w_zones", "w_retests", "w_swings", "w_break", "w_bvoids", "w_vp")})
    c_msgs, c_state = champion_changes(old)
    msgs += c_msgs
    new.update(c_state or {"c_zone": old.get("c_zone", ""), "c_trig": old.get("c_trig", [])})
    l_msgs, l_state = level_changes(old)
    msgs += l_msgs
    new.update(l_state or {"l_sigs": old.get("l_sigs", [])})
    p_msgs, p_state = pa_changes(old, r.get("spot"))
    e8_msgs, e8_state = ema8_changes(old, r.get("spot"))
    for tf, e8m in e8_msgs.items():                # 8 EMA signals ride in the same 15-min / 5+3-min messages
        p_msgs[tf] = e8m + p_msgs.get(tf, [])
    new.update(e8_state or {**{k: old.get(k, []) for k in ("e8_seen", "e8_trig", "e8_exit")},
                            "e8_trig_t": old.get("e8_trig_t", {}), "e8_journal": old.get("e8_journal", ""),
                            "e8_jlast": old.get("e8_jlast", ""), "e8_jfill": old.get("e8_jfill", "")})
    if p_msgs.get("15m"):                        # 15-min SUPER signals: their own Telegram message
        _send(f"🟪🟪 PA 15-MIN SUPER SIGNAL {r['time']:%H:%M} spot {r['spot']:,.1f}\n\n" + "\n\n".join(p_msgs["15m"]))
    small = [m for tf in ("5m", "3m") for m in p_msgs.get(tf, [])]
    if small:                                    # 5-min + 3-min together, second message
        _send(f"PA 5-min / 3-min {r['time']:%H:%M} spot {r['spot']:,.1f}\n\n" + "\n\n".join(small))
    new.update(p_state or {**{k: old.get(k, []) for k in ("pa_seen", "pa_trig", "pa_exit")},
                           "pa_trig_t": old.get("pa_trig_t", {})})
    c_msgs, c_state = cliff_changes(old)
    msgs += c_msgs
    new.update(c_state or {"c_breaks": old.get("c_breaks", [])})
    e_msgs, e_state = expiry_changes(old)
    msgs = e_msgs + msgs
    new.update(e_state or {k: old.get(k, False) for k in ("e_open", "e_red")})
    msgs.sort(key=lambda t: not (t.startswith("★") or t.startswith("MONTHLY")))          # main signals first
    if msgs:
        _send(f"OC (mentor) {r['time']:%H:%M} spot {r['spot']:,.1f}\n" + "\n".join(msgs))
        log.info("sent %d change(s)", len(msgs))
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(new))
    return 0


if __name__ == "__main__":
    sys.exit(main())
