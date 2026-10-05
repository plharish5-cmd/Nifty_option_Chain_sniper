"""
terminal_tables.py — draws the briefing as SEPARATE BOXED TABLES, one box per topic.
Presentation only: it re-lays-out text the existing modules already produce (agt_briefing, smc_brief, nifty_scanner).
No trading logic, no thresholds, no data fetching. If a box cannot be parsed it falls back to showing that part's raw lines
inside the box, so nothing is ever lost. Python 3.9 compatible.

  render_dashboard(brief_text, smc_text, scanner=None, alerts=None, data_note="", now=None, width=112) -> str
  colorize(text) -> str      # ANSI colours for a real terminal (green = bull/pass, red = bear/fail, gold = box titles)

Information only — not financial advice.
"""
from __future__ import annotations

import os
import re
import shutil
import sys
import textwrap
from datetime import datetime

# ── text cleaning: emoji have unpredictable width in terminals and break box borders, so use narrow symbols ──────────────────
_MAP = {"✅": "✔", "❌": "✘", "✗": "✘", "⚠️": "!", "⚠": "!", "⏳": "…", "⏸": "‖", "🔴": "●", "🟢": "●", "🟨": "●",
        "↔": "↔", "\t": " "}
_STRIP = re.compile("[\U0001F000-\U0001FAFF\u2600-\u26FF\u2B00-\u2BFF\uFE0E\uFE0F\u200d]")


def clean(s) -> str:
    s = str(s)
    for k, v in _MAP.items():
        s = s.replace(k, v)
    return _STRIP.sub("", s)


def wrap(text: str, width: int) -> list:
    out = []
    for para in str(text).split("\n"):
        if not para.strip():
            out.append("")
            continue
        ind = len(para) - len(para.lstrip())
        ind = min(ind, max(0, width // 3))
        hang = 2 if para.strip()[:1] in ("→", "•", "—", "-") else 0
        lines = textwrap.wrap(para.strip(), width=max(8, width), initial_indent=" " * ind, subsequent_indent=" " * (ind + hang),
                              break_long_words=True, break_on_hyphens=False)
        out += lines or [""]
    return out or [""]


def _pad(s: str, n: int, align: str = "l") -> str:
    s = s[:n]
    if align == "r":
        return s.rjust(n)
    if align == "c":
        return s.center(n)
    return s.ljust(n)


def grid(title, headers, rows, w, aligns=None, flex=None, sub=None) -> list:
    """One box. headers=None → no header row. rows = list of lists of cells (cells may hold '\\n'). Returns a list of lines, each exactly w wide."""
    rows = [[clean(c) for c in r] for r in rows]
    headers = [clean(h) for h in headers] if headers else None
    ncol = len(headers) if headers else (len(rows[0]) if rows else 1)
    rows = [(r + [""] * ncol)[:ncol] for r in rows]
    inner = w - 4
    overhead = 3 * (ncol - 1)
    avail = inner - overhead
    flex = ncol - 1 if flex is None else flex

    def nat_of(i):
        cells = ([headers[i]] if headers else []) + [r[i] for r in rows]
        return max([len(x) for c in cells for x in c.split("\n")] + [1])

    cw = [nat_of(i) for i in range(ncol)]
    guard = 0
    while sum(cw) > avail and guard < 500:
        guard += 1
        j = flex if cw[flex] > 10 else cw.index(max(cw))
        cw[j] = max(4, cw[j] - 1)
    slack = avail - sum(cw)
    if slack > 0:
        cw[flex] += slack
    al = list(aligns or ["l"] * ncol)
    al += ["l"] * (ncol - len(al))

    t = clean(title)[: max(0, w - 8)]
    out = ["┌─ " + t + " " + "─" * max(0, w - 5 - len(t)) + "┐"]
    if sub:
        for ln in wrap(clean(sub), inner):
            out.append("│ " + ln.ljust(inner) + " │")
        out.append("├─" + "─┬─".join("─" * c for c in cw) + "─┤" if ncol > 1 else "├" + "─" * (w - 2) + "┤")
    if headers:
        out.append("│ " + " │ ".join(_pad(h, cw[i], "c" if al[i] == "r" else "l") for i, h in enumerate(headers)) + " │")
        out.append("├─" + "─┼─".join("─" * c for c in cw) + "─┤")
    if not rows:
        out.append("│ " + "(nothing to show)".ljust(inner) + " │")
    for r in rows:
        wr = [wrap(r[i], cw[i]) for i in range(ncol)]
        for k in range(max(len(x) for x in wr)):
            out.append("│ " + " │ ".join(_pad(wr[i][k] if k < len(wr[i]) else "", cw[i], al[i]) for i in range(ncol)) + " │")
    out.append("└" + "─" * (w - 2) + "┘")
    return out


def panel(title, lines, w, sub=None) -> list:
    return grid(title, None, [[l] for l in lines], w, flex=0, sub=sub)


def kv(title, pairs, w, sub=None, keyw=None) -> list:
    pairs = [(clean(a), clean(b)) for a, b in pairs]
    return grid(title, None, [[a, b] for a, b in pairs], w, flex=1, sub=sub)


# ── generic parsing helpers ────────────────────────────────────────────────────────────────────────────────────────────────
def lines_to_kv(lines) -> list:
    rows = []
    for ln in lines:
        s = clean(ln)
        body = s.strip()
        if not body or re.match(r"^[─═\-]{3,}$", body):
            continue
        ind = len(s) - len(s.lstrip())
        if body.startswith("→") or (ind >= 5 and rows):
            rows[-1][1] += "\n" + body
            continue
        m = re.match(r"^([^:]{1,34}):\s*(.*)$", body)
        if m and not re.match(r"^\d", body):
            rows.append([m.group(1).strip(), m.group(2).strip()])
        else:
            rows.append(["", body])
    return rows


def pipe_table(lines):
    hdr, rows, notes = None, [], []
    for ln in lines:
        s = clean(ln)
        if "|" in s:
            cells = [c.strip() for c in s.split("|")]
            if hdr is None and any(x.lower().startswith("strike") for x in cells):
                hdr = cells
            else:
                rows.append(cells)
        elif s.strip() and not re.match(r"^[─═\-\s]+$", s):
            notes.append(s.strip())
    return hdr, rows, notes


def _num(s):
    try:
        return float(str(s).replace(",", "").replace("+", ""))
    except Exception:
        return None


# ══ the scheduler briefing (agt_briefing.build_briefing text) ═══════════════════════════════════════════════════════════════
_HDRS = [("walls", "MAX INSTITUTIONAL WALLS"), ("sr", "SUPPORT / RESISTANCE"), ("oiflow", "OI FLOW"), ("inst", "INSTITUTIONAL READ"),
         ("gap", "── WRITER GAP"), ("chain", "OPTION CHAIN SNAPSHOT"), ("veto", "VETO / ACTION GUIDANCE"), ("health", "DATA HEALTH")]
_KV = re.compile(r"^([A-Z][A-Za-z0-9 ()%,./-]*?):\s{2,}(\S.*)$")


def split_briefing(text: str) -> dict:
    sec = {"pre": []}
    cur = "pre"
    for raw in str(text).splitlines():
        s = clean(raw)
        st = s.strip()
        if re.match(r"^[═=]{10,}$", st):
            continue
        if any(ch in s for ch in "╔╗╚╝╠╣║"):
            sec.setdefault("box", []).append(s)
            cur = "box"
            continue
        hit = next((k for k, h in _HDRS if st.startswith(h)), None)
        if hit:
            cur = hit
            sec.setdefault(cur, [])
            continue
        sec.setdefault(cur, []).append(s)
    return sec


def _ladder(sec, spot):
    """Walls + S/R + prior day as one price ladder, highest price first, with the spot row in place."""
    rows = {}

    def add(strike, role, kind, oi):
        if strike is None:
            return
        d = rows.setdefault(round(strike, 2), {"roles": [], "kind": kind, "oi": []})
        d["roles"].append(role)
        if oi and oi not in d["oi"]:
            d["oi"].append(oi)

    for ln in sec.get("walls", []):
        m = re.search(r"(MAX RESISTANCE|MAX SUPPORT):\s*([\d,\.]+)\s+(CE|PE)\s+\[([^\]]+)\]", ln)
        if m:
            add(_num(m.group(2)), m.group(1).title(), "RESIST" if "RESIST" in m.group(1) else "SUPPORT", f"{m.group(3)} {m.group(4)}")
    atm = None
    for ln in sec.get("sr", []):
        m = re.search(r"\b(R1|R2|S1|S2):\s*([\d,\.]+)\s+\[OI wall:\s*([\d\.]+L?)\s+(CE|PE)", ln)
        if m:
            add(_num(m.group(2)), m.group(1), "RESIST" if m.group(1)[0] == "R" else "SUPPORT", f"{m.group(4)} {m.group(3)}")
            continue
        m = re.search(r"ATM:\s*([\d,\.]+)", ln)
        if m:
            atm = _num(m.group(1))
            continue
        m = re.search(r"MAJOR WALL:\s*([\d,\.]+)\s+(CE|PE)\s+\[([^\]]+)\]", ln)
        if m:
            k_ = _num(m.group(1))
            add(k_, "Major wall", ("RESIST" if (spot is not None and k_ > spot) else "SUPPORT") if spot is not None else ("RESIST" if m.group(2) == "CE" else "SUPPORT"), f"{m.group(2)} {m.group(3)}")
            continue
        m = re.search(r"PRIOR DAY:\s*(.*)$", ln)
        if m:
            for nm, val in re.findall(r"(PDH|PDL|PDC|PDO)\s+([\d,\.]+)", m.group(1)):
                add(_num(val), {"PDH": "Prior-day high", "PDL": "Prior-day low", "PDC": "Prior-day close", "PDO": "Prior-day open"}[nm], "REF", "")
    out = []
    items = sorted(rows.items(), key=lambda kv_: -kv_[0])
    placed = False
    for k, d in items:
        if spot is not None and not placed and k < spot:
            out.append(["", f"{spot:,.0f}", "◀ SPOT" + (f" (ATM {atm:,.0f})" if atm else ""), "", ""])
            placed = True
        dist = f"{k - spot:+,.0f}" if spot is not None else ""
        out.append([d["kind"], f"{k:,.0f}", " · ".join(d["roles"]), "  ".join(d["oi"]), dist])
    if spot is not None and not placed:
        out.append(["", f"{spot:,.0f}", "◀ SPOT", "", ""])
    return out


def briefing_panels(text: str, w: int):
    """Returns (panels dict name→lines, header facts dict)."""
    P, F = {}, {}
    if not text or text.startswith("Market closed") or text.startswith("BRIEFING: N/A"):
        P["BRIEFING"] = panel("OPTION CHAIN · INSTITUTIONAL · LEVELS", [text or "No briefing data."], w)
        return P, F
    sec = split_briefing(text)
    kvs, head, notes = [], "", []
    for s in sec.get("pre", []):
        m = _KV.match(s)
        if m:
            kvs.append((m.group(1).strip(), m.group(2).strip()))
        elif "NIFTY LIVE BRIEFING" in s:
            head = s
        elif s.strip():
            notes.append(s.strip())
    spot = None
    m = re.search(r"Spot\s+([\d,]+(?:\.\d+)?)", head)
    if m:
        spot = _num(m.group(1))
    m = re.search(r"\|\s*(\d\d:\d\d)", head)
    F["time"] = m.group(1) if m else ""
    F["spot"] = spot
    d = dict(kvs)
    F["bias"] = re.sub(r"\s+", " ", d.get("BIAS", "")).strip()
    F["regime"] = d.get("REGIME", "")
    m = re.search(r"Low\s+(\S+)\s+\((\S+)\)\s+↔\s+High\s+(\S+)\s+\((\S+)\)\s+\((\d+) pts\)", d.get("DAY RANGE", ""))
    F["range"] = f"{_num(m.group(1)):,.0f} ↔ {_num(m.group(3)):,.0f} ({m.group(5)} pts)" if m else d.get("DAY RANGE", "")

    # 1 · option chain + OI flow
    hdr, rows, cnotes = pipe_table(sec.get("chain", []))
    if hdr and rows:
        def _lakh(x):
            m_ = re.match(r"^([\d\.]+)\s*([Lk]?)", str(x).strip())
            if not m_:
                return 0.0
            return float(m_.group(1)) * (100.0 if m_.group(2) == "L" else (0.1 if m_.group(2) == "k" else 0.01))
        ci = next((i for i, h in enumerate(hdr) if h.strip().upper() == "CE OI"), None)
        pi = next((i for i, h in enumerate(hdr) if h.strip().upper() == "PE OI"), None)
        top_ce = max(range(len(rows)), key=lambda i: _lakh(rows[i][ci])) if (ci is not None and rows) else None
        top_pe = max(range(len(rows)), key=lambda i: _lakh(rows[i][pi])) if (pi is not None and rows) else None
        rr = []
        for i, r in enumerate(rows):
            r = list(r)
            if r and "←" in r[-1]:
                r[-1] = r[-1].split("←")[0].strip()
                r[0] = f"{r[0]} ◀ ATM"
            note = []
            if i == top_ce:
                note.append("most CE OI = ceiling")
            if i == top_pe:
                note.append("most PE OI = floor")
            rr.append(r + [" · ".join(note)])
        P["OPTION CHAIN"] = grid("OPTION CHAIN · ATM ± 2 strikes (L = lakh contracts, k = thousand)", hdr + ["Note"], rr, w,
                                 aligns=["r"] * len(hdr) + ["l"], flex=len(hdr))
    else:
        P["OPTION CHAIN"] = panel("OPTION CHAIN", sec.get("chain", ["(not available)"]), w)
    fl = lines_to_kv(sec.get("oiflow", []))
    if fl:
        P["OI FLOW"] = kv("OPTION CHAIN · OI FLOW, last 15 min", fl, w)

    # 2 · ladder
    lad = _ladder(sec, spot)
    if lad:
        P["LADDER"] = grid("LEVELS · walls, support / resistance, prior day (price ladder, distance from spot in pts)",
                           ["Type", "Strike", "What", "OI wall", "From spot"], lad, w, aligns=["l", "r", "l", "l", "r"], flex=2)

    # 3 · institutional
    ir = lines_to_kv(sec.get("inst", []))
    if ir:
        P["INSTITUTIONAL"] = kv("INSTITUTIONAL · who is writing options and how firmly", ir, w)
    gh, gr, gn = pipe_table(sec.get("gap", []))
    if gh and gr:
        P["WRITER GAP"] = grid("INSTITUTIONAL · writer gap (L = lakh contracts)", gh, gr, w, aligns=["r", "r", "r", "r", "l"], flex=4,
                               sub="  ·  ".join(gn) if gn else None)

    # 4 · key metrics
    if kvs:
        P["METRICS"] = kv("MARKET METRICS · gamma, volatility, PCR, writers", kvs, w)

    # 5 · trade check
    veto = []
    for s in sec.get("veto", []):
        b = s.strip()
        if not b:
            continue
        sym = b[0] if b[0] in "✔✘‖!" else ""
        veto.append([sym, b[1:].strip() if sym else b])
    chk, score, missing = [], "", ""
    for s in sec.get("box", []):
        b = re.sub(r"[║╔╗╚╝═╠╣]", "", s).strip()
        m = re.search(r"□\s*(.+?):\s+(PASS|FAIL)", b)
        if m:
            chk.append([("✔" if m.group(2) == "PASS" else "✘"), m.group(1).strip(), m.group(2)])
        m = re.search(r"SCORE:\s*(.+)$", b)
        if m:
            score = re.sub(r"\s*✘$|\s*✔$", "", m.group(1)).strip()
        m = re.search(r"Missing:\s*(.+)$", b)
        if m:
            missing = m.group(1).strip()
    if veto:
        P["VETO"] = grid("TRADE CHECK · what the option chain allows / forbids", ["", "Guidance"], veto, w, flex=1)
    if chk:
        P["CHECKLIST"] = grid("TRADE CHECK · pre-trade checklist", ["", "Check", "Result"], chk, w, flex=1,
                              sub=f"SCORE {score}" + (f"   ·   still missing: {missing}" if missing else ""))
        F["checklist"] = score
    hl = lines_to_kv(sec.get("health", []))
    if hl:
        P["HEALTH"] = kv("DATA HEALTH", hl, w)
    if notes:
        P["NOTES"] = panel("NOTES", notes, w)
    return P, F


# ══ the SMC block (smc_brief.build_block text) ════════════════════════════════════════════════════════════════════════════════
def split_smc(text: str) -> dict:
    sec = {}
    cur = "raw"

    def mark(line):
        st = line.strip()
        if line.startswith("──── SMC"):
            return "hdr"
        if line.startswith(("Spot ", "BIG TREND", "TREND CHECK", "WHERE IN RANGE")):
            return "head"
        if line.startswith("GARCH RANGE"):
            return "garch"
        if line.startswith("EVIDENCE BOARD"):
            return "evid"
        if line.startswith("CANDLES"):
            return "candles"
        if line.startswith(">>> ACTION"):
            return "action"
        if st.startswith("BUY checklist"):
            return "buy"
        if st.startswith("SELL checklist"):
            return "sell"
        if st.startswith("TRIGGER LEVELS"):
            return "trig"
        if st.startswith("SELL-AT-TOP"):
            return "szone"
        if st.startswith("BUY-AT-BOTTOM"):
            return "bzone"
        if re.match(r"^Last \S+ event:", st):
            return "last"
        if st.startswith("Info only"):
            return "drop"
        if line.startswith("5-RULE"):
            return "five"
        if line.startswith("ORDER FLOW"):
            return "flow"
        return None

    for raw in str(text).splitlines():
        s = clean(raw)
        k = mark(s)
        if k:
            cur = k
            if k == "drop":
                continue
            sec.setdefault(k, [])
            sec[k].append(s)
            if k in ("last",):
                cur = "after_last"
            continue
        if s.strip():
            sec.setdefault(cur, []).append(s)
    return sec


_CANDLE = re.compile(r"^\s*(\S+)\s+(CLOSED|FORMING)\s+(\d\d:\d\d)(?:–(\d\d:\d\d))?(?:\s+\((\d+)/(\d+) min, not final\))?:\s*(\w+)\s+"
                     r"O\s*([\d,.]+)\s+H\s*([\d,.]+)\s+L\s*([\d,.]+)\s+C\s*([\d,.]+)\s*·\s*body\s*(\d+)%\s*·\s*upper wick\s*(\d+)%\s*·\s*lower wick\s*(\d+)%(.*)$")


def _smc_panels(text: str, w: int) -> dict:
    P = {}
    if not text:
        return P
    sec = split_smc(text)
    if "hdr" not in sec and "head" not in sec:
        P["SMC"] = panel("SMC · plain read", [clean(text)], w)
        return P
    hdr = (sec.get("hdr") or [""])[0].strip("─ ").strip()

    # read
    rows = []
    for s in sec.get("head", []):
        if s.startswith("Spot "):
            rows.append(["Spot / last candle", s[5:].strip()])
        elif s.startswith("BIG TREND"):
            rows.append(["Trend · 15m swings", s.split(":", 1)[1].strip()])
        elif s.startswith("TREND CHECK"):
            rows.append(["Trend check", s.split(":", 1)[1].strip()])
        elif s.startswith("WHERE IN RANGE"):
            rows.append(["Where in range", s.split(":", 1)[1].strip()])
    act = sec.get("action", [])
    if act:
        first = act[0].replace(">>>", "").replace("ACTION:", "").strip()
        rest = [x.strip() for x in act[1:]]
        rows.append(["ACTION", first + ("\n" + "\n".join(rest) if rest else "")])
    if rows:
        P["SMC READ"] = kv("SMC · plain read  (" + hdr.replace("SMC PLAIN READ · ", "") + ")", rows, w)

    # checklists
    crow = []
    for key, name in (("buy", "BUY"), ("sell", "SELL")):
        lines = sec.get(key, [])
        if not lines:
            continue
        m = re.search(r"\((\d)/4\)", lines[0])
        for s in lines[1:]:
            b = s.strip()
            if b and b[0] in "✔✘":
                crow.append([f"{name} {m.group(1) + '/4' if m else ''}".strip(), b[0], b[1:].strip()])
    if crow:
        P["SMC CHECKLIST"] = grid("SMC · BUY / SELL checklist (3 of 4 = get ready, 4 of 4 = act)", ["Side", "", "Condition"], crow, w, flex=2)

    # levels
    lv = []
    for s in sec.get("trig", [])[1:]:
        b = s.strip()
        m = re.match(r"([▲▼])\s+(\S+) close (ABOVE|BELOW) ([\d,\.]+)\s*→\s*(.*)$", b)
        if m:
            lv.append(["TRIGGER " + m.group(1), m.group(4), f"{m.group(2)} candle must CLOSE {m.group(3).lower()} → {m.group(5)}"])
    for key, lab in (("szone", "SELL zone ▲"), ("bzone", "BUY zone ▼")):
        for s in sec.get(key, [])[1:]:
            m = re.match(r"•\s*([\d,]+(?:–[\d,]+)?)\s+(.*)$", s.strip())
            if m:
                lv.append([lab, m.group(1), m.group(2)])
    if sec.get("last"):
        lv.append(["Last event", "", sec["last"][0].strip()])
    if lv:
        P["SMC LEVELS"] = grid("SMC · levels to watch (zones are where to LOOK, the CLOSE is the trigger)", ["What", "Price", "Meaning"], lv, w,
                               aligns=["l", "r", "l"], flex=2)

    # 5-rule
    fv = sec.get("five", [])
    if fv:
        steps, other = [], []
        for s in fv[1:]:
            m = re.match(r"^\s*([✔✘])\s*(\d)\s*·\s*(.+?):\s+(.*)$", s)
            if m:
                steps.append([m.group(2), m.group(1), m.group(3).strip(), m.group(4).strip()])
            elif s.strip():
                other.append(s.strip())
        if steps:
            g = grid("SMC · 5-RULE MODEL (checklist only — UNPROVEN, not a signal)", ["#", "", "Rule", "Detail"], steps, w, flex=3, sub=fv[0].strip())
            P["5-RULE"] = g
            if other:
                m2 = [[(o.split(":", 1)[0] if ":" in o[:12] else ""), (o.split(":", 1)[1].strip() if ":" in o[:12] else o)] for o in other]
                P["5-RULE PLAN"] = kv("SMC · 5-RULE plan and backtest", m2, w)
        else:
            P["5-RULE"] = panel("SMC · 5-RULE MODEL", [x.strip() for x in fv], w)

    # garch + evidence
    gl = [x.strip() for x in sec.get("garch", [])]
    if gl:
        P["GARCH"] = panel("SIZE OF THE MOVE · GARCH range (size, not direction)", gl, w)
    ev = sec.get("evid", [])
    if ev:
        body = ev[0].split(":", 1)[1] if ":" in ev[0] else ev[0]
        body = re.sub(r"\s*\(full:.*\)\s*$", "", body).strip()
        parts = re.split(r"\s·\s(?=[✔✘…])", body)
        er = []
        lab = {"✔": "✔ PROVEN on your data", "✘": "✘ NO EDGE found", "…": "… PENDING (not enough data yet)"}
        for p in parts:
            p = p.strip()
            if not p:
                continue
            items = [x.strip() for x in p[1:].split(",") if x.strip()]
            er.append([lab.get(p[0], p[0]), str(len(items)), ", ".join(items)])
        if er:
            P["EVIDENCE"] = grid("EVIDENCE BOARD · what has been tested on YOUR NIFTY data", ["Verdict", "n", "Ideas"], er, w, aligns=["l", "r", "l"], flex=2,
                                 sub="Only ideas under PROVEN deserve weight. Full list: .venv/bin/python evidence_board.py")

    # candles
    cl = sec.get("candles", [])
    if cl:
        tab, reads, last, ok = [], [], None, False
        for s in cl[1:]:
            m = _CANDLE.match(s)
            if m:
                ok = True
                tf, st, a, b, mm, ee, colr, o, h, l, c, bd, uw, lw, rest = m.groups()
                win = f"{a}–{b}" if st == "CLOSED" else f"{a} ({mm}/{ee}m)"
                tab.append([tf, st.title(), win, colr, o, h, l, c, bd + "%", uw + "%", lw + "%"])
                last = (tf, st.title())
                if "◀" in rest:
                    reads.append([f"{tf} {st.title()}", rest.split("◀", 1)[1].strip().replace(" — ", "\n— "),
                              "no proven edge (see note above)" if "no pattern has a proven edge" in cl[0] else "—"])
                continue
            b = s.strip()
            if b.startswith("▸") and last:
                b = b[1:].strip()
                pre = ""
                if b.startswith("if it closed now:"):
                    pre = "if it closed now: "
                    b = b[len("if it closed now:"):].strip()
                segs = [x.strip() for x in b.split(" · ")]
                tag = segs[-1] if len(segs) > 1 else ""
                name = " · ".join(segs[:-1]) if len(segs) > 1 else b
                reads.append([f"{last[0]} {last[1]}", pre + name, tag])
            elif b:
                reads.append(["", b, ""])
        if ok:
            P["CANDLES"] = grid("CANDLES · last closed bar and the bar forming now, 15m / 30m / 1h",
                                ["TF", "Bar", "Window", "Colour", "Open", "High", "Low", "Close", "Body", "Up wick", "Low wick"], tab, w,
                                aligns=["l", "l", "l", "l", "r", "r", "r", "r", "r", "r", "r"], flex=2, sub=re.sub(r"^CANDLES\s*·\s*", "", cl[0].strip()))
            if reads:
                P["CANDLE READS"] = grid("CANDLES · wicks and patterns on those bars", ["Bar", "Read", "Tested on your NIFTY data"], reads, w, flex=1)
        else:
            P["CANDLES"] = panel("CANDLES", [x.strip() for x in cl], w)

    # order flow
    fl = sec.get("flow", [])
    if fl:
        rows, evs = [], []
        for s in fl[1:]:
            b = s.strip()
            m = re.match(r"^•\s*(\d\d:\d\d)\s+(.+?)\s+\[([^\]]+)\]$", b)
            if m:
                evs.append([m.group(1), m.group(2), m.group(3)])
                continue
            m = re.match(r"^Day CVD\s+(\S+ Cr)\s*·\s*last candle (\d\d:\d\d) delta (\S+ Cr) \(([^)]+)\)\s*·\s*breadth (.+)$", b)
            if m:
                rows += [["Day CVD (cumulative buy−sell)", m.group(1)], [f"Last candle {m.group(2)}", f"delta {m.group(3)} ({m.group(4)})"],
                         ["Breadth (stocks up / down)", m.group(5)]]
                continue
            m = re.match(r"^Flow VWAP\s+(.*)$", b)
            if m:
                rows.append(["Flow VWAP", m.group(1)])
                continue
            m = re.match(r"^([A-Za-z+/ ]+(?:idea)?[A-Za-z+/ ]*):\s+(.*)$", b)
            rows.append([m.group(1).strip(), m.group(2).strip()] if m else ["", b])
        if rows:
            P["ORDER FLOW"] = kv("ORDER FLOW · NIFTY 50 aggressive buy / sell · " + fl[0].split("·")[-1].strip() + " candles", rows, w,
                                 sub="Delta = money bought at the ask minus sold at the bid. Tags: UNTESTED / not proven means no backtest edge yet.")
        if evs:
            P["FLOW EVENTS"] = grid("ORDER FLOW · recent events", ["Time", "Event", "Status"], evs, w, flex=1)
        if not rows and not evs:
            P["ORDER FLOW"] = panel("ORDER FLOW", [x.strip() for x in fl], w)
    if "raw" in sec and not P:
        P["SMC"] = panel("SMC", sec["raw"], w)
    return P


# ══ assembly ══════════════════════════════════════════════════════════════════════════════════════════════════════════════
def _alerts_panel(alerts, w):
    if not alerts:
        return None
    return grid("ALERTS · latest from the option-chain / price-action watchers", ["Time", "Source", "Alert"], alerts, w, flex=2)


def _tape_panel(tape, w):
    """SIGNAL TAPE — every price-action / 8 EMA signal of the day with its stop/target result (signal_tape.py)."""
    if tape is None:
        return None
    rows, note = tape
    hdr = ["Time", "Tier", "Side", "Entry", "SL", "T1", "Status", "Best", "Setup / app backtest"]
    if not rows:
        return panel("SIGNAL TAPE · price-action and 8 EMA signals today", [note or "No signals yet today."], w)
    return grid("SIGNAL TAPE · today's price-action / 8 EMA signals · result on 1-min candles", hdr, rows, w,
                aligns=["l", "l", "l", "r", "r", "r", "l", "r", "l"], flex=8, sub=note or None)


def _scanner_panel(scanner, w):
    if scanner is None:
        return None
    rows, note = scanner
    hdr = ["Time", "Kind", "Idea", "Level", "Order flow", "Chain", "Bias", "Score"]
    if not rows:
        return panel("SCANNER · ICT sweep→FVG and fake-breakout events", [note or "No events yet today."], w)
    return grid("SCANNER · ICT sweep→FVG / fake-breakout events + order flow + option chain (logged, UNVALIDATED)", hdr, rows, w,
                aligns=["l", "l", "l", "l", "l", "l", "r", "r"], flex=3, sub=note or None)


def _tiles(F, smc_action, w, now, candle_close="", oi_action=""):
    chain = f"{F['spot']:,.0f} @ {F.get('time', '')}" if F.get("spot") else "—"
    rows = [[f"{now:%H:%M}", chain, candle_close or "—", F.get("bias", "—"), smc_action or "—", oi_action or "—", F.get("checklist", "—"), F.get("regime", "—")]]
    return grid("NIFTY · at a glance", ["Now", "Chain spot", "Candle", "Bias", "SMC", "OI action", "Checklist", "Regime"], rows, w,
                aligns=["l"] * 8, flex=7)


def render_dashboard(brief_text="", smc_text="", scanner=None, alerts=None, data_note="", now=None, width=112, oi_lab=None, tape=None) -> str:
    now = now or datetime.now()
    w = max(80, int(width))
    BP, F = briefing_panels(brief_text, w) if brief_text else ({}, {})
    SP = _smc_panels(smc_text, w)
    action = ""
    m = re.search(r">>>\s*ACTION:\s*(.*?)(?:\s+—|$)", clean(smc_text or ""), re.M)
    if m:
        action = re.split(r"\s{2,}|\(", m.group(1).strip())[0].strip()
    out = []

    def add(lines, gap=True):
        if lines:
            out.extend(lines)

    banner = f" NIFTY DASHBOARD · {now:%a %d %b %H:%M} "
    out.append("═" * ((w - len(banner)) // 2) + banner + "═" * (w - len(banner) - (w - len(banner)) // 2))
    if data_note:
        for ln in wrap(clean(data_note), w):
            out.append(ln)
    cc = ""
    mm = re.search(r"^Spot\s+([\d,\.]+)\s+\(last closed \S+ candle (\d\d:\d\d)\)", clean(smc_text or ""), re.M)
    if mm:
        cc = f"{_num(mm.group(1)):,.0f} @ {mm.group(2)}"
    OP = {}
    oi_action = ""
    if oi_lab is not None:
        try:
            import oi_lab_view
            OP = oi_lab_view.panels(oi_lab, w)
            oi_action = oi_lab.get("action", "") if oi_lab.get("ok") else ""
        except Exception as _e:                                    # noqa: BLE001
            OP = {"OI ACTION": panel("OPTIONS LAB · writers and OI", [f"N/A — {type(_e).__name__}: {_e}"], w)}
    add(_tiles(F, action, w, now, cc, oi_action))
    add(_tape_panel(tape, w))
    add(_alerts_panel(alerts, w))
    for k in ("OPTION CHAIN", "OI FLOW"):
        add(BP.get(k))
    for k in ("OI ACTION", "OI SCOREBOARD", "OI STRIKES"):
        add(OP.get(k))
    for k in ("LADDER", "INSTITUTIONAL", "WRITER GAP", "METRICS", "VETO", "CHECKLIST", "BRIEFING", "NOTES"):
        add(BP.get(k))
    for k in ("SMC", "SMC READ", "SMC CHECKLIST", "SMC LEVELS", "5-RULE", "5-RULE PLAN", "CANDLES", "CANDLE READS", "ORDER FLOW", "FLOW EVENTS"):
        add(SP.get(k))
    add(_scanner_panel(scanner, w))
    for k in ("GARCH", "EVIDENCE"):
        add(SP.get(k))
    add(BP.get("HEALTH"))
    out.append("Information only — not financial advice.")
    return "\n".join(out)


# ── colours (screen only; the log file stays plain) ────────────────────────────────────────────────────────────────────────
_G, _R, _Y, _O, _D, _X = "\033[32m", "\033[31m", "\033[33m", "\033[38;5;220m", "\033[2m", "\033[0m"


def colorize(text: str) -> str:
    out = []
    for ln in text.split("\n"):
        if ln.startswith("┌─") or ln.startswith("═"):
            out.append(_O + ln + _X)
            continue
        ln = re.sub(r"(BUY PE side|BUY PE|LEAN PE side|LEAN PE)", _R + r"\1" + _X, ln)
        ln = re.sub(r"(✔|▲|\bT1 HIT\b|\bBULLISH\b|\bPASS\b|\bBUY\b(?! PE)|LEAN CE(?: side)?|\bUP\b)", _G + r"\1" + _X, ln)
        ln = re.sub(r"(✘|▼|\bSL HIT\b|\bBEARISH\b|\bFAIL\b|\bSELL\b|\bDOWN\b)", _R + r"\1" + _X, ln)
        ln = re.sub(r"(◀ SPOT|◀ ATM|\bWAIT\b|\bSTAND ASIDE\b|UNPROVEN|UNTESTED)", _Y + r"\1" + _X, ln)
        out.append(ln)
    return "\n".join(out)


def screen_width(default=112) -> int:
    try:
        if sys.stdout.isatty():
            return max(88, min(130, shutil.get_terminal_size((default, 40)).columns))
    except Exception:
        pass
    return default


def use_color() -> bool:
    return bool(sys.stdout.isatty()) and not os.environ.get("NO_COLOR")
