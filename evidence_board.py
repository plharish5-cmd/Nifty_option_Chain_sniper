"""
evidence_board.py — 🧾 Evidence Board: which ideas in this app are PROVEN on your NIFTY data, which failed,
which are still being tested — read live from the backtest result files (data/backtests/*.json).

  .venv/bin/python evidence_board.py        # full board in the terminal (also saved to data/logs/)
  smc_brief.py prints a one-line summary in every briefing block; the dashboard shows it as a tab.

Nothing here is typed in by hand: every verdict comes from a saved backtest file, so the board updates itself
whenever you rerun a test. Information only — not financial advice.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).parent
BT = ROOT / "data" / "backtests"

NAVY, PANEL, GRID = "#0b1a33", "#10223f", "#1e3358"
GOLD, WHITE, MUTED = "#d4af37", "#f1f5f9", "#94a3b8"
GREEN, RED, AMBER, BLUE = "#22c55e", "#ef4444", "#f59e0b", "#38bdf8"
STYLE = {"EDGE": ("✔", GREEN), "SMALL EDGE": ("✔", GREEN), "NO EDGE": ("✘", RED), "PENDING": ("⏳", AMBER),
         "NOT RUN": ("—", MUTED)}


def _j(name: str) -> dict | None:
    try:
        return json.loads((BT / name).read_text())
    except Exception:
        return None


def _date(d: dict | None) -> str:
    g = (d or {}).get("generated", "")
    return str(g)[:10] if g else ""


def _row(name, group, verdict, detail, used, cmd, when=""):
    return dict(name=name, group=group, verdict=verdict, detail=detail, used=used, cmd=cmd, when=when)


def collect() -> list[dict]:
    rows = []
    # ── daily bias ───────────────────────────────────────────────────────
    db = _j("daily_bias.json")
    cmd_db = ".venv/bin/python backtest_daily_bias.py"
    if db:
        o = db.get("outcomes", {})
        d = o.get("dir", {})
        rows.append(_row("Daily candle → tomorrow's DIRECTION", "Daily bias",
                         "EDGE" if d.get("passed") else "NO EDGE",
                         f"{d.get('accuracy', 0) * 100:.1f}% right vs {d.get('base_rate', 0) * 100:.1f}% by chance "
                         f"(p={d.get('p_value', 1):.3f}) · {db.get('days', 0)} days {db.get('from', '')}→{db.get('to', '')}",
                         "Ignored for direction" if not d.get("passed") else "Used in 5-Rule Rule 1", cmd_db, db.get("to", "")))
        dr = o.get("draw", {})
        bt = dr.get("by_type", {})
        sides = []
        for k, lvl in (("BEAR", "PDL"), ("BULL", "PDH")):
            x = bt.get(k, {})
            if x:
                sides.append(f"{k}→{lvl} {x.get('acc', 0) * 100:.0f}% vs {x.get('chance', 0) * 100:.0f}% (p={x.get('p', 1):.3f})")
        rows.append(_row("Daily DRAW — PDL / PDH gets taken next day", "Daily bias",
                         "SMALL EDGE" if dr.get("passed") else "NO EDGE",
                         " · ".join(sides) or f"edge {dr.get('edge_pts', 0):+.1f} pts",
                         "PDL used as TP1 after a BEAR day (5-Rule, Decision Desk)" if dr.get("passed") else "Not used", cmd_db,
                         db.get("to", "")))
    else:
        rows.append(_row("Daily candle bias & draw", "Daily bias", "NOT RUN", "no result file yet", "—", cmd_db))

    # ── 5-Rule SMC ───────────────────────────────────────────────────────
    cmd5 = ".venv/bin/python backtest_five_rule.py"
    for fn, nm, cmd in (("five_rule.json", "5-Rule SMC model (headline settings)", cmd5),
                        ("five_rule_oos.json", "5-Rule SMC · out-of-sample year",
                         cmd5 + " --before 2025-10-03 --entry limit --tag oos")):
        f = _j(fn)
        if not f:
            rows.append(_row(nm, "SMC", "NOT RUN", "no result file yet", "—", cmd))
            continue
        h = f.get("headline", {})
        s, n = h.get("stats", {}), h.get("null", {})
        ent = f.get("headline_entry", "market")
        rows.append(_row(f"{nm} · {ent} entry", "SMC", "EDGE" if f.get("passed") else "NO EDGE",
                         f"{s.get('n', 0)} trades / {f.get('scored_sessions', 0)} sessions"
                         f"{(' ' + str(f.get('scored_from')) + '→' + str(f.get('scored_to'))) if f.get('scored_from') else ''} · "
                         f"avg {s.get('avg_R', 0):+.2f}R · win {s.get('win_rate', 0) * 100:.0f}% · "
                         f"vs random time p={n.get('p_time', 1):.2f} · vs coin-flip side p={n.get('p_side', 1):.2f}",
                         "Checklist only — cannot trigger trades" if not f.get("passed") else "May trigger (auto)", cmd,
                         _date(f)))
        ft = f.get("fresh_test")
        if ft and fn == "five_rule.json":
            rows.append(_row("5-Rule freshness rule (sweep ≤90m, CHoCH ≤30m)", "SMC",
                             "EDGE" if ft.get("passed") else "NO EDGE",
                             " · ".join(f"{k}: {v['stats'].get('avg_R', 0):+.2f}R (n={v['stats'].get('n', 0)})"
                                        for k, v in ft.items() if isinstance(v, dict) and "stats" in v),
                             "Off" if not ft.get("passed") else "Can be switched on", cmd5, _date(f)))
    f = _j("five_rule.json")
    sw = (f or {}).get("session_sweeps")
    if sw:
        bits, ok = [], False
        for k, v in sw.items():
            bits.append(f"{k.split(' (')[0]}: both sides {v['both'] * 100:.0f}% vs {v['chance'] * 100:.0f}% chance (p={v['p']:.2f})")
            ok = ok or (v["p"] < 0.05 and v["edge_pts"] > 0)
        rows.append(_row("Session sweeps — both sides of OR / first hour taken", "SMC",
                         "EDGE" if ok else "NO EDGE", " · ".join(bits), "Not used as an expectation", cmd5, _date(f)))

    # ── GARCH ───────────────────────────────────────────────────────────
    g = _j("garch.json")
    cmdg = ".venv/bin/python garch_range.py --test"
    if g:
        gg, ve, vh = g.get("garch", {}), g.get("vs_ewma", {}), g.get("vs_hv20", {})
        verdict = "EDGE" if (vh.get("better") or ve.get("better")) else "NO EDGE"
        rows.append(_row("GARCH expected-move box", "Volatility", verdict,
                         f"close inside 1σ {gg.get('cover1_close', 0) * 100:.0f}% / 2σ {gg.get('cover2_close', 0) * 100:.0f}% · "
                         f"day's high–low inside 1σ {gg.get('cover1_range', 0) * 100:.0f}% · beats 20-day HV (t={vh.get('t', 0):+.1f})"
                         f" · vs EWMA t={ve.get('t', 0):+.1f} ({'better' if ve.get('better') else 'about equal'}) · "
                         f"half-life {g.get('latest_fit', {}).get('half_life', 0):.0f} days",
                         "Box in briefing + Decision Desk context (size of move, not direction)", cmdg, _date(g)))
    else:
        rows.append(_row("GARCH expected-move box", "Volatility", "NOT RUN", "no result file yet", "Shown untested", cmdg))

    # ── order flow ───────────────────────────────────────────────────────
    cmdo = ".venv/bin/python backtest_orderflow.py"
    try:
        import nifty_orderflow as nf
        nrec = len(nf.recorded_days())
        need = int(json.loads(nf.RULES_PATH.read_text()).get("validation", {}).get("min_sessions", 20))
    except Exception:
        nrec, need = 0, 20
    o = _j("orderflow.json")
    if not o:
        rows.append(_row("NIFTY 50 order-flow signals (14 types)", "Order flow", "PENDING",
                         f"{nrec}/{need} recorded sessions — keep orderflow_recorder.py running daily",
                         "Shown as UNTESTED, weight 0", cmdo))
        rows.append(_row("Delta + VWAP setup", "Order flow", "PENDING", f"{nrec}/{need} recorded sessions",
                         "Shown as UNTESTED", cmdo))
    else:
        sg = o.get("signals", {})
        ok = [k for k, v in sg.items() if v.get("passed")]
        few = [k for k, v in sg.items() if 0 < v.get("n", 0) < 30]
        rows.append(_row("NIFTY 50 order-flow signals", "Order flow", "EDGE" if ok else "NO EDGE",
                         f"{o.get('sessions', 0)} sessions · passed: {', '.join(ok) if ok else 'none'} · too few events: {len(few)}",
                         "Passed types get a VALIDATED badge" if ok else "Context only", cmdo, _date(o)))
        su = o.get("setup", {})
        rows.append(_row("Delta + VWAP setup", "Order flow", "EDGE" if su.get("passed") else ("PENDING" if su.get("n", 0) < 30 else "NO EDGE"),
                         f"{su.get('n', 0)} trades · avg {su.get('avg_R', 0):+.2f}R · vs random time p={su.get('p_time', 1):.2f} · "
                         f"vs coin-flip p={su.get('p_side', 1):.2f}", "Card + trades tab", cmdo, _date(o)))

    # ── ICT 2022 model + time windows ────────────────────────────────────
    cmdi = ".venv/bin/python backtest_ict.py"
    for fn, nm, cmd in (("ict.json", "ICT 2022 model (sweep → MSS → FVG limit entry)", cmdi),
                        ("ict_test.json", "ICT 2022 model · untouched year",
                         cmdi + " --after 2025-10-03 --tag test")):
        f = _j(fn)
        if not f:
            rows.append(_row(nm, "ICT 2022", "NOT RUN", "no result file yet", "Research only — not in the app", cmd))
            continue
        h = f.get("headline", {})
        pr = f.get("primary", {})
        n = int(h.get("n", 0) or 0)
        verdict = "EDGE" if h.get("passed") else "NO EDGE"
        pdv = f.get("pd_variants", {})
        pdtxt = " · ".join(f"{k}: {v.get('avg_R', 0):+.2f}R (n={v.get('n', 0)})" for k, v in pdv.items() if v.get("n"))
        rows.append(_row(nm, "ICT 2022", verdict,
                         f"{n} trades / {f.get('sessions', 0)} sessions {f.get('from', '')}→{f.get('to', '')} · "
                         f"limit {pr.get('entry', '')} · {pr.get('tp', '')}R · {pr.get('mode', '')} · filter {pr.get('pd_filter', '')} · "
                         f"avg {h.get('avg_R', 0):+.2f}R · win {h.get('win_rate', 0) * 100:.0f}% · "
                         f"vs random time p={h.get('p_time', 1):.2f} · vs coin-flip p={h.get('p_side', 1):.2f}"
                         + (f" · filters → {pdtxt}" if pdtxt else "")
                         + (f" · grid: {f.get('grid_passed', 0)}/18 variants pass" if "grid_passed" in f else ""),
                         "May be added to the briefing" if h.get("passed") else "Research only — not in the app", cmd, _date(f)))
    f = _j("ict.json")
    ws = (f or {}).get("window_study")
    if ws:
        from math import comb
        testable = [r for r in ws if r.get("cont_p") is not None]
        sig = [r for r in testable if r["cont_p"] < 0.05]
        k, m = len(sig), len(testable)
        tail = sum(comb(m, i) * 0.05 ** i * 0.95 ** (m - i) for i in range(k, m + 1)) if m else 1.0
        bits = [f"{r['bucket']} {'continues' if r['cont_pts'] > 0 else 'reverses'} {r['cont_pts']:+.1f} pts (p={r['cont_p']:.3f})"
                for r in sig[:4]]
        first = ws[0] if ws else {}
        rows.append(_row("Time windows — do 30-min buckets continue or reverse?", "ICT 2022",
                         "EDGE" if (k >= 2 and tail < 0.05) else "NO EDGE",
                         f"{k} of {m} buckets significant at p<0.05 ({0.05 * m:.1f} expected by luck; chance of this many = {tail:.2f})"
                         + (" · " + " · ".join(bits) if bits else "")
                         + f" · opening 30 min = {first.get('share_of_day', 0) * 100:.0f}% of the day's range",
                         "Not used — no window rule in the app" if k < 2 or tail >= 0.05 else "Window rule can be tested further",
                         cmdi, _date(f)))
    else:
        rows.append(_row("Time windows — do 30-min buckets continue or reverse?", "ICT 2022", "NOT RUN", "no result file yet",
                         "Research only", cmdi))

    # ── turtle soup (fake breakouts) ─────────────────────────────────────
    cmdt = ".venv/bin/python backtest_turtle_soup.py --fetch-bn 730"
    for fn, nm, cmd in (("turtle_soup.json", "Turtle soup — do fake breakouts (wick 2-10 pts past a pool, close back inside) reverse?", cmdt),
                        ("turtle_soup_test.json", "Turtle soup · untouched year", cmdt + " --after 2025-10-03 --tag test"),
                        ("turtle_soup_open.json", "Fake move through the day's open — Power of 3 (T.A.M. Ep. 1)",
                         ".venv/bin/python backtest_turtle_soup.py --pools open --tag open")):
        f = _j(fn)
        if not f:
            rows.append(_row(nm, "ICT 2022", "NOT RUN", "no result file yet", "Research only — not in the app", cmd))
            continue
        h = f.get("headline", {})
        n = int(h.get("n", 0) or 0)
        beyond = f.get("closed_beyond", {})
        smt = f.get("smt", {})
        smt_txt = " · ".join(f"{k}: {v.get('avg_pts', 0):+.1f} pts (n={v.get('n', 0)})" for k, v in smt.items() if v.get("n"))
        rows.append(_row(nm, "ICT 2022", "EDGE" if h.get("passed") else ("PENDING" if n < 30 else "NO EDGE"),
                         f"{n} events / {f.get('sessions', 0)} sessions {f.get('from', '')}→{f.get('to', '')} · "
                         f"{f.get('primary_horizon_min', 30)} min after the close · avg {h.get('avg_pts', 0):+.1f} pts "
                         f"(random same-time {h.get('null_avg', 0):+.1f}) · hit {h.get('hit', 0) * 100:.0f}% · p={h.get('p', 1):.3f} · "
                         f"halves {h.get('half1')}/{h.get('half2')}"
                         + (f" · closed BEYOND the pool (real breakout): {beyond.get('avg_pts', 0):+.1f} pts" if beyond.get("n") else "")
                         + (f" · SMT split → {smt_txt}" if smt_txt else ""),
                         "May be added to the briefing" if h.get("passed") else "Research only — not in the app", cmd, _date(f)))
    f = _j("ict.json")
    sp = (f or {}).get("smt_split") or {}
    if sp:
        a_, b_ = sp.get("SMT present", {}), sp.get("no SMT", {})
        ok = bool(a_.get("passed"))
        rows.append(_row("ICT + SMT — does Bank Nifty not confirming the sweep improve the ICT model?", "ICT 2022",
                         "EDGE" if ok else ("PENDING" if int(a_.get("n", 0) or 0) < 30 else "NO EDGE"),
                         f"with SMT: {a_.get('n', 0)} trades avg {a_.get('avg_R', 0):+.2f}R (p vs random time {a_.get('p_time', 1):.2f}) · "
                         f"without: {b_.get('n', 0)} trades avg {b_.get('avg_R', 0):+.2f}R",
                         "Can be set as smt_filter=require in ict_rules.json" if ok else "Research only — not in the app",
                         ".venv/bin/python backtest_ict.py --fetch-bn 730", _date(f)))
    else:
        rows.append(_row("ICT + SMT — does Bank Nifty not confirming the sweep improve the ICT model?", "ICT 2022", "NOT RUN",
                         "no Bank Nifty split yet", "Research only", ".venv/bin/python backtest_ict.py --fetch-bn 730"))
    c = _j("confluence.json")
    if c:
        t = c.get("test", {})
        st = c.get("status", "PENDING")
        by = c.get("by_score", {})
        bytxt = " · ".join(f"score {k}: n={v['n']} avg {v['avg_pts']:+.1f} pts" for k, v in sorted(by.items()))
        rows.append(_row("Confluence — ICT event + order flow + option chain (NiftyScanner)", "ICT 2022", st,
                         f"{c.get('events', 0)} events / {c.get('sessions', 0)} sessions logged · order-flow sessions {c.get('flow_sessions', 0)} · "
                         f"chain sessions {c.get('chain_sessions', 0)}" + (f" · {bytxt}" if bytxt else "")
                         + (f" · score-3 vs ≤1: {t.get('diff_pts', 0):+.1f} pts, p={t.get('p', 1):.3f}" if t else
                            f" · needs ≥{c.get('min_events', 30)} events at score 3 and at ≤1"),
                         "Logged only — never a signal until EDGE", ".venv/bin/python nifty_scanner.py --report", _date(c)))
    else:
        rows.append(_row("Confluence — ICT event + order flow + option chain (NiftyScanner)", "ICT 2022", "PENDING",
                         "scanner has not logged any events yet", "Logged only — never a signal until EDGE",
                         ".venv/bin/python nifty_scanner.py --backfill 30"))

    # ── trap / break-block entries on 30m / 1h / 4h zones ─────────────────
    cmdz = ".venv/bin/python backtest_trap_ob.py"
    for fn, nm, cmd in (("trap_ob.json", "Trap & break-block entries on 30m/1h/4h zones (AirForexOne)", cmdz),
                        ("trap_ob_test.json", "Trap & break-block entries · untouched year", cmdz + " --after 2025-10-03 --tag test")):
        f = _j(fn)
        if not f:
            rows.append(_row(nm, "Price action", "NOT RUN", "no result file yet", "Research only — not in the app", cmd))
            continue
        h = f.get("headline", {})
        n = int(h.get("n", 0) or 0)
        per = " · ".join(f"{tf}m: n={r['headline'].get('n', 0)} avg {r['headline'].get('avg_R', 0):+.2f}R"
                         for tf, r in f.get("tfs", {}).items() if r.get("headline", {}).get("n"))
        rows.append(_row(nm, "Price action", "EDGE" if h.get("passed") else ("PENDING" if n < 30 else "NO EDGE"),
                         f"{n} trades pooled / {f.get('sessions', 0)} sessions {f.get('from', '')}→{f.get('to', '')} · "
                         f"{f.get('rules', {}).get('tp', '2')}R · {f.get('rules', {}).get('mode', '')} · avg {h.get('avg_R', 0):+.2f}R · "
                         f"win {h.get('win_rate', 0) * 100:.0f}% · vs random time p={h.get('p_time', 1):.2f} · vs coin-flip p={h.get('p_side', 1):.2f}"
                         + (f" · {per}" if per else "") + f" · grid {f.get('grid_passed', 0)}/{f.get('grid_n', 0)} pass",
                         "May be added to the briefing" if h.get("passed") else "Research only — not in the app", cmd, _date(f)))

    # ── positional breakout from consolidation, NIFTY 50 stocks ───────────
    cmdp = ".venv/bin/python backtest_positional.py"
    for fn, nm, cmd in (("positional.json", "Positional: breakout from consolidation in an uptrend (NIFTY 50 stocks, daily)", cmdp),
                        ("positional_test.json", "Positional breakout · untouched period", cmdp + " --after 2023-01-01 --tag test")):
        f = _j(fn)
        if not f:
            rows.append(_row(nm, "Positional", "NOT RUN", "no result file yet — first: positional_scan.py --fetch", "Research only — not in the app", cmd))
            continue
        h = f.get("headline", {})
        n = int(h.get("n", 0) or 0)
        rows.append(_row(nm, "Positional", "EDGE" if h.get("passed") else ("PENDING" if n < 30 else "NO EDGE"),
                         f"{n} breakouts / {f.get('stocks', 0)} stocks {f.get('from', '')}→{f.get('to', '')} · stop box low · "
                         f"{f.get('rules', {}).get('target_r', 2)}R · avg {h.get('avg_R', 0):+.2f}R · win {h.get('win_rate', 0) * 100:.0f}% · "
                         f"+40d {h.get('avg_f40', 0):+.1f}% · vs random date p={h.get('p_a_R', 1):.2f} · vs trending-day p={h.get('p_b_R', 1):.2f}"
                         f" · grid {f.get('grid_passed', 0)}/4 pass",
                         "Daily list can be used as a watch-list" if h.get("passed") else "Research only — not in the app", cmd, _date(f)))

    # ── candlestick patterns (15m / 30m / 1h) ─────────────────────────────
    cmdc = ".venv/bin/python backtest_candles.py"
    for fn, nm, cmd in (("candles.json", "Candle patterns: does price move the textbook way after hammer, engulfing, stars … (15m/30m/1h)", cmdc),
                        ("candles_test.json", "Candle patterns · untouched period", cmdc + " --after 2025-10-03 --tag test")):
        f = _j(fn)
        if not f:
            rows.append(_row(nm, "Price action", "NOT RUN", "no result file yet", "Names shown in the briefing; no direction claimed", cmd))
            continue
        ps = f.get("patterns", {})
        npass, nlean, ninv = f.get("passed", []), f.get("lean", []), f.get("inverse", [])
        npl, nll = f.get("passed_level", []), f.get("lean_level", [])
        rows.append(_row(nm, "Price action", "EDGE" if (npass or npl) else "NO EDGE",
                         f"{f.get('events', 0)} events / {f.get('sessions', 0)} sessions {f.get('from', '')}→{f.get('to', '')} · {f.get('n_tests', 0)} patterns tested, p corrected for that · "
                         + (f"proven: {', '.join(npass)}" if npass else "none proven")
                         + (f" · weak lead only: {', '.join(nlean)}" if nlean else "") + (f" · opposite to textbook: {', '.join(ninv)}" if ninv else "")
                         + (f" · AT A LEVEL proven: {', '.join(npl)}" if npl else " · at a level: none proven") + (f" (weak leads: {', '.join(nll)})" if nll else ""),
                         "Briefing shows the pattern with this verdict" if (npass or npl) else "Names only — direction not claimed", cmd, _date(f)))

    # ── never tested on their own ────────────────────────────────────────
    for nm, used in (("SMC sniper (BUY NOW / SELL NOW in the briefing)", "Shown as is — treat as untested"),
                     ("SMC Trade Desk (BUY / SELL / AVOID)", "Can trigger Decision Desk — untested"),
                     ("Decision Desk confluence score", "Untested — weights are judgement, not fitted")):
        rows.append(_row(nm, "Not yet tested", "NOT RUN", "no backtest written for this yet", used, "—"))
    return rows


def summary_line(rows: list[dict] | None = None) -> str:
    rows = rows or collect()
    pick = {"EDGE": [], "SMALL EDGE": [], "NO EDGE": [], "PENDING": []}
    short = {"ICT 2022 model · untouched": "ICT test", "ICT 2022 model": "ICT model", "Time windows": "time windows", "Turtle soup · untouched": "fake-breakout test", "Fake move through the day": "open fake-move",
             "Turtle soup": "fake breakouts", "Trap & break-block entries · untouched": "trap/OB test", "Trap & break-block": "trap/OB", "Positional breakout · untouched": "positional test", "Candle patterns · untouched": "candles test", "Candle patterns": "candle patterns", "Positional": "positional", "ICT + SMT": "ICT+SMT", "Confluence": "confluence",
             "Daily DRAW": "daily draw", "GARCH": "GARCH box", "5-Rule SMC model (headline": "5-Rule",
             "5-Rule SMC · out-of-sample": "5-Rule OOS", "Session sweeps": "session sweeps",
             "NIFTY 50 order-flow": "order flow", "Delta + VWAP": "Delta+VWAP", "Daily candle → tomorrow": "daily direction",
             "5-Rule freshness": "freshness"}
    for r in rows:
        s = next((v for k, v in short.items() if r["name"].startswith(k)), None)
        if s and r["verdict"] in pick:
            pick[r["verdict"]].append(s)
    parts = []
    if pick["EDGE"] + pick["SMALL EDGE"]:
        parts.append("✔ " + ", ".join(pick["EDGE"] + pick["SMALL EDGE"]))
    if pick["NO EDGE"]:
        parts.append("✘ " + ", ".join(pick["NO EDGE"]))
    if pick["PENDING"]:
        parts.append("⏳ " + ", ".join(pick["PENDING"]))
    return "EVIDENCE BOARD: " + " · ".join(parts) + "  (full: .venv/bin/python evidence_board.py)"


def text_board() -> str:
    rows = collect()
    L = [f"🧾 EVIDENCE BOARD · what is proven on YOUR NIFTY data · {datetime.now():%Y-%m-%d %H:%M}", "═" * 110]
    grp = None
    for r in rows:
        if r["group"] != grp:
            grp = r["group"]
            L.append(f"\n[{grp}]")
        ic = STYLE[r["verdict"]][0]
        L.append(f" {ic} {r['verdict']:<10} {r['name']}" + (f"   (tested {r['when']})" if r["when"] else ""))
        L.append(f"     {r['detail']}")
        L.append(f"     → in the app: {r['used']}" + (f" · rerun: {r['cmd']}" if r["cmd"] != "—" else ""))
    L.append("\nRule of the board: only ✔ rows may influence a trade decision. ✘ rows stay as checklists or are removed;"
             " ⏳ / — rows are shown as UNTESTED.\nInformation only — not financial advice.")
    return "\n".join(L)


# ═══════════════════════════════════════════════════════════════════════════
# Dashboard tab
# ═══════════════════════════════════════════════════════════════════════════
def render() -> None:
    import streamlit as st
    rows = collect()
    cnt = {k: sum(1 for r in rows if (r["verdict"] in (k, "SMALL EDGE") if k == "EDGE" else r["verdict"] == k))
           for k in ("EDGE", "NO EDGE", "PENDING", "NOT RUN")}
    st.markdown(f"""<div style="background:linear-gradient(90deg,{NAVY},{PANEL});border:1px solid {GRID};border-left:4px solid {GOLD};
      border-radius:10px;padding:12px 18px;margin-bottom:10px">
      <div style="color:{GOLD};font-weight:800;font-size:1.15rem">🧾 EVIDENCE BOARD — what is actually proven on your NIFTY data</div>
      <div style="color:#cbd5e1;font-size:0.85rem;margin-top:4px">Every verdict is read from a saved backtest file
      (data/backtests/*.json) — rerun a test and this page updates. Each test compares the idea with RANDOM entries or
      shuffled days, so a ✔ means "better than luck", not "looks good on a chart".</div></div>""", unsafe_allow_html=True)
    cols = st.columns(4)
    for c, (k, lab, col) in zip(cols, (("EDGE", "PROVEN", GREEN), ("NO EDGE", "FAILED", RED),
                                        ("PENDING", "COLLECTING DATA", AMBER), ("NOT RUN", "NOT TESTED YET", MUTED))):
        c.markdown(f"<div style='background:{PANEL};border:1px solid {GRID};border-top:3px solid {col};border-radius:8px;"
                   f"padding:10px 12px'><div style='color:{MUTED};font-size:0.75rem;font-weight:700'>{lab}</div>"
                   f"<div style='color:{col};font-size:1.6rem;font-weight:800'>{cnt[k]}</div></div>", unsafe_allow_html=True)
    grp = None
    for r in rows:
        if r["group"] != grp:
            grp = r["group"]
            st.markdown(f"<div style='color:{GOLD};font-weight:800;margin:14px 0 4px 0'>{grp}</div>", unsafe_allow_html=True)
        ic, col = STYLE[r["verdict"]]
        cmd_html = (f"<div style='color:{MUTED};font-size:0.75rem;margin-top:2px'>Rerun: <code>{r['cmd']}</code></div>"
                    if r['cmd'] != '—' else "")
        when_html = f"<span style='color:{MUTED};font-size:0.75rem;margin-left:8px'>tested {r['when']}</span>" if r['when'] else ""
        html = f"""<div style="background:{NAVY};border:1px solid {GRID};border-left:5px solid {col};border-radius:8px;
          padding:9px 14px;margin:5px 0">
          <div style="display:flex;justify-content:space-between;flex-wrap:wrap;gap:6px">
            <span style="color:{WHITE};font-weight:700">{r['name']}</span>
            <span><span style="background:{col};color:#000;font-weight:900;padding:2px 10px;border-radius:5px">{ic} {r['verdict']}</span>
            {when_html}</span></div>
          <div style="color:#cbd5e1;font-size:0.85rem;margin-top:4px">{r['detail']}</div>
          <div style="color:{BLUE};font-size:0.8rem;margin-top:3px">In the app: {r['used']}</div>
          {cmd_html}
        </div>"""
        st.markdown("".join(x.strip() for x in html.splitlines()), unsafe_allow_html=True)
    st.markdown(f"""<div style="background:{PANEL};border:1px solid {GRID};border-radius:8px;padding:10px 14px;margin-top:12px;
      color:{WHITE};font-size:0.88rem;line-height:1.5"><b style="color:{GOLD}">How to use this page</b><br>
      • Only <b style="color:{GREEN}">✔ PROVEN</b> rows should influence a trade decision.<br>
      • <b style="color:{RED}">✘ FAILED</b> means it did no better than random entries on two-sided tests — keep it as a learning
        checklist, never as a trigger.<br>
      • <b style="color:{AMBER}">⏳</b> / <b style="color:{MUTED}">—</b> rows are UNTESTED: watch them, journal them, don't size up on them.<br>
      • A ✔ can still fail in future markets — rerun the tests every few months.<br>Information only — not financial advice.</div>""",
                unsafe_allow_html=True)


if __name__ == "__main__":
    import runlog
    runlog.start("evidence_board")
    print(text_board())
