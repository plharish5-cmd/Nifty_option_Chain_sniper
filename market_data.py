"""
Market context beyond the option chain: India VIX and institutional flows.

CADENCE — the two data sets update at completely different rates, so polling
them at the same interval would be wrong in both directions.

  India VIX   Recomputed continuously through the session. Fetched alongside
              every chain poll (3 min). One extra call, cheap, and the value
              genuinely moves within a session.

  FII / DII   Published ONCE per day, after close. NSE puts the cash-market
              figures out around 6-7 PM and the participant-wise F&O OI
              around 7-8 PM. Polling this every 3 minutes would be 125
              identical requests a day for a number that changes once.
              Fetched once at startup, cached for 6 hours, and clearly
              labelled as PREVIOUS session — it is overnight context, never
              an intraday trigger.

A caution on the FII/DII numbers: the widely-quoted "FII bought X crore" is
CASH market activity. It says nothing directly about index positioning. The
figure that matters for NIFTY direction is participant-wise F&O open interest
— FII index-future longs versus shorts. Both are fetched here and kept
separate, because conflating them is one of the most common mistakes in
Indian retail commentary.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import requests
from typing import Any

import config

log = logging.getLogger(__name__)

VIX_ENDPOINTS = [
    ("/api/allIndices", {}),
    ("/api/equity-stockIndices", {"index": "INDIA VIX"}),
]

FII_ENDPOINTS = [
    ("/api/fiidiiTradeReact", {}),
]


@dataclass
class VixReading:
    value: float
    change: float
    pct_change: float
    day_high: float | None = None
    day_low: float | None = None

    @property
    def regime(self) -> str:
        if self.value >= config.VIX_HIGH:   return "HIGH"
        if self.value <= config.VIX_LOW:    return "LOW"
        return "NORMAL"

    @property
    def note(self) -> str:
        """What this means for someone BUYING options."""
        spiking = self.pct_change >= config.VIX_SPIKE_PCT
        if spiking:
            return ("VIX spiking — premiums are inflated right now. Buying "
                    "into a spike means paying peak IV, and the crush "
                    "afterwards can lose you money even when direction is "
                    "right. Wait for it to settle.")
        if self.regime == "HIGH":
            return ("Elevated VIX — options are expensive and moves are wide. "
                    "Size smaller than usual; your stop needs more room.")
        if self.regime == "LOW":
            return ("Low VIX — premiums are cheap, which favours buyers, but "
                    "expect smaller ranges. Cheap options that never move are "
                    "still a loss.")
        return "VIX in its normal band — no strong signal either way."


@dataclass
class FlowReading:
    date: str
    fii_cash_net: float | None = None      # Rs crore, cash market
    dii_cash_net: float | None = None
    source: str = ""

    @property
    def note(self) -> str:
        if self.fii_cash_net is None:
            return "No FII/DII figures available."
        f, d = self.fii_cash_net, self.dii_cash_net or 0.0
        who = []
        who.append(f"FII {'bought' if f >= 0 else 'sold'} Rs{abs(f):,.0f} cr")
        who.append(f"DII {'bought' if d >= 0 else 'sold'} Rs{abs(d):,.0f} cr")
        tail = ""
        if f < 0 < d:
            tail = " — DII absorbing FII selling, a common floor-forming pattern."
        elif f > 0 > d:
            tail = " — FII buying against DII selling."
        elif f < 0 and d < 0:
            tail = " — both selling, genuine risk-off."
        return (" · ".join(who) + tail +
                "  This is CASH market activity from the previous session; it "
                "is context, not an intraday signal, and it is not index "
                "positioning.")


def _first_working(client, endpoints: list[tuple[str, dict]], what: str) -> dict[str, Any]:
    """Try each candidate; return the first that yields JSON."""
    errors = []
    for path, params in endpoints:
        try:
            r = client._get(f"{config.BASE_URL}{path}", params)
            if r.status_code != 200:
                errors.append(f"{path} -> HTTP {r.status_code}")
                continue
            return client._json(r, f"{what} ({path})")
        except Exception as exc:                            # noqa: BLE001
            errors.append(f"{path} -> {exc}")
    raise RuntimeError(f"{what} unavailable. Tried: " + " | ".join(errors))


def fetch_vix(client) -> VixReading:
    """India VIX. Call this on the same cadence as the option chain."""
    payload = _first_working(client, VIX_ENDPOINTS, "India VIX")
    rows = payload.get("data") or []
    for row in rows:
        name = str(row.get("index") or row.get("indexSymbol") or "").upper()
        if "VIX" in name:
            return VixReading(
                value=float(row.get("last") or row.get("lastPrice") or 0),
                change=float(row.get("variation") or row.get("change") or 0),
                pct_change=float(row.get("percentChange") or row.get("pChange") or 0),
                day_high=_maybe(row.get("high") or row.get("dayHigh")),
                day_low=_maybe(row.get("low") or row.get("dayLow")),
            )
    raise RuntimeError(f"No VIX row found; got {len(rows)} indices back")


def fetch_flows(client) -> FlowReading:
    """
    FII/DII cash-market activity for the previous session.

    Fetch once per day — this number does not change intraday.
    """
    payload = _first_working(client, FII_ENDPOINTS, "FII/DII")
    rows = payload if isinstance(payload, list) else payload.get("data", [])
    out = FlowReading(date="", source="fiidiiTradeReact")
    for row in rows:
        cat = str(row.get("category", "")).upper()
        net = row.get("netValue")
        try:
            net = float(str(net).replace(",", "")) if net is not None else None
        except (TypeError, ValueError):
            net = None
        out.date = out.date or str(row.get("date", ""))
        if "FII" in cat or "FPI" in cat:
            out.fii_cash_net = net
        elif "DII" in cat:
            out.dii_cash_net = net
    if out.fii_cash_net is None and out.dii_cash_net is None:
        raise RuntimeError("FII/DII payload parsed but held no net values")
    return out


def _maybe(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def vix_bias_note(vix: VixReading | None, action: str) -> str | None:
    """
    A veto-style warning, not a score component.

    VIX does not tell you direction, so folding it into the bias score would
    be wrong. What it tells you is whether buying an option right now is
    expensive — which is a reason to skip a trade, not a reason to take the
    other side.
    """
    if vix is None or action == "NO_TRADE":
        return None
    if vix.pct_change >= config.VIX_SPIKE_PCT:
        return (f"VIX +{vix.pct_change:.1f}% today at {vix.value:.2f}. You "
                f"would be buying premium at inflated IV — if the move you "
                f"expect happens slowly, IV crush eats the gain. Consider "
                f"waiting or sizing down.")
    if vix.regime == "HIGH":
        return (f"VIX {vix.value:.2f} is elevated. Options cost more and swings "
                f"are wider than your usual stop assumes.")
    return None


# ═══════════════════════════════════════════════════════════════════════════
# Participant-wise open interest — the real source for View Building #14
# ═══════════════════════════════════════════════════════════════════════════

PARTICIPANT_OI_URL = ("https://nsearchives.nseindia.com/content/nsccl/"
                      "fao_participant_oi_{ddmmyyyy}.csv")


@dataclass
class ParticipantOI:
    date: str
    fii_idx_fut_long: int
    fii_idx_fut_short: int
    dii_idx_fut_long: int
    dii_idx_fut_short: int
    client_idx_fut_long: int
    client_idx_fut_short: int
    prev_fii_net: int | None = None
    fii_stk_fut_long: int = 0
    fii_stk_fut_short: int = 0
    prev_fii_stk_net: int | None = None

    @property
    def fii_net(self) -> int:
        """Positive = net long index futures. This is a POSITION."""
        return self.fii_idx_fut_long - self.fii_idx_fut_short

    @property
    def dii_net(self) -> int:
        return self.dii_idx_fut_long - self.dii_idx_fut_short

    @property
    def client_net(self) -> int:
        return self.client_idx_fut_long - self.client_idx_fut_short

    @property
    def fii_change(self) -> int | None:
        """Day-on-day change in the net position. This is the FLOW."""
        if self.prev_fii_net is None:
            return None
        return self.fii_net - self.prev_fii_net

    @property
    def fii_stk_net(self) -> int:
        return self.fii_stk_fut_long - self.fii_stk_fut_short

    @property
    def fii_stock_fut_change(self) -> int | None:
        """Day-on-day change in FII stock futures net position."""
        if self.prev_fii_stk_net is None:
            return None
        return self.fii_stk_net - self.prev_fii_stk_net

    @property
    def long_pct(self) -> float | None:
        """
        FII index-future longs as a share of their total futures book.

        The session's thresholds: above 65% long is bullish fuel, below 30%
        is heavy short inventory. This is a different question from the net
        position — a fund can be net short while still holding a large long
        book, and the ratio says how lopsided the whole exposure is.
        """
        total = self.fii_idx_fut_long + self.fii_idx_fut_short
        if total <= 0:
            return None
        return self.fii_idx_fut_long / total * 100

    @property
    def long_pct_read(self) -> str:
        p = self.long_pct
        if p is None:
            return "no futures book"
        if p > 65:
            return f"{p:.0f}% long — bullish fuel"
        if p < 30:
            return f"{p:.0f}% long — heavy short inventory"
        return f"{p:.0f}% long — balanced book"

    @property
    def stance(self) -> str:
        n = self.fii_net
        d = self.fii_change
        base = "net LONG" if n > 0 else "net SHORT"
        if d is None:
            return f"FII {base} {abs(n):,} contracts"
        moving = ("adding to longs" if d > 0 and n > 0 else
                  "covering shorts" if d > 0 else
                  "adding to shorts" if n < 0 else "trimming longs")
        return (f"FII {base} {abs(n):,} contracts, {moving} "
                f"({d:+,} on the day)")

    def score(self) -> tuple[int, str]:
        """
        Scores on the day's CHANGE, not the standing position.

        Your notes say "FII IDX Fut BUYING (positive) = +2". Buying is a flow,
        so the day-on-day change in the net position is the faithful reading.
        A large short position that shrank today is bearish positioning with
        bullish flow — the flow is what the parameter asks about.

        If MJ means the standing position instead, swap `d` for `self.fii_net`
        below; the threshold would then want to be much larger.
        """
        d = self.fii_change
        if d is None:
            return 0, "no previous day to compare — cannot score the flow"
        if d > config.FII_FLOW_THRESHOLD:
            return 2, "Bullish"
        if d < -config.FII_FLOW_THRESHOLD:
            return -2, "Bearish"
        return 0, "Neutral"


def _parse_participant_csv(text: str) -> dict[str, dict[str, int]]:
    """
    NSE's file has a quirky header: line 1 is a doubled-quote title, line 2 is
    the real header with trailing spaces in several column names.
    """
    lines = [l for l in text.splitlines() if l.strip()]
    if len(lines) < 3:
        raise RuntimeError("participant OI file too short")

    header = [h.strip() for h in lines[1].split(",")]
    out: dict[str, dict[str, int]] = {}
    for line in lines[2:]:
        parts = [p.strip() for p in line.split(",")]
        if len(parts) != len(header):
            continue
        row = dict(zip(header, parts))
        who = row.get("Client Type", "").upper()
        if not who:
            continue
        vals = {}
        for k, v in row.items():
            if k == "Client Type":
                continue
            try:
                vals[k] = int(float(v))
            except (TypeError, ValueError):
                pass
        out[who] = vals
    return out


def fetch_participant_oi(client, on: date | None = None,
                         look_back: int = 6) -> ParticipantOI:
    """
    Download the participant-wise OI file, walking back over holidays.

    Static CSV on nsearchives — no API, no cookies needed, and it will still
    work when NSE next reorganises their web API. Published in the evening, so
    at 7 AM you are correctly reading the previous session.

    Also fetches the prior trading day so the day-on-day FLOW is available.
    """
    on = on or date.today()

    def grab(d: date) -> tuple[date, dict] | None:
        local_p = Path(getattr(config, "DATA_DIR", "data")) / "participant_oi" / f"oi_{d:%Y-%m-%d}.csv"
        url = PARTICIPANT_OI_URL.format(ddmmyyyy=d.strftime("%d%m%Y"))
        try:
            r = client.session.get(url, timeout=config.REQUEST_TIMEOUT)
            if r.status_code == 200 and len(r.content) >= 200:
                try:
                    local_p.parent.mkdir(parents=True, exist_ok=True)
                    local_p.write_text(r.text, encoding="utf-8")
                except Exception:
                    pass
                return d, _parse_participant_csv(r.text)
        except Exception:                                   # noqa: BLE001
            pass
        if local_p.exists():
            try:
                return d, _parse_participant_csv(local_p.read_text(encoding="utf-8"))
            except Exception:
                pass
        return None

    latest = None
    cursor = on
    for _ in range(look_back):
        latest = grab(cursor)
        if latest:
            break
        cursor -= timedelta(days=1)
    if not latest:
        raise RuntimeError(
            f"No participant OI file found in the {look_back} days to {on}. "
            f"NSE publishes it in the evening; before that, yesterday's is "
            f"the newest available.")

    got_date, data = latest

    prev_net = None
    prev_stk_net = None
    cursor = got_date - timedelta(days=1)
    for _ in range(look_back):
        p = grab(cursor)
        if p:
            pd_ = p[1].get("FII", {})
            prev_net = (pd_.get("Future Index Long", 0)
                        - pd_.get("Future Index Short", 0))
            prev_stk_net = (pd_.get("Future Stock Long", 0)
                            - pd_.get("Future Stock Short", 0))
            break
        cursor -= timedelta(days=1)

    fii = data.get("FII", {})
    dii = data.get("DII", {})
    cli = data.get("CLIENT", {})
    return ParticipantOI(
        date=got_date.strftime("%d-%b-%Y"),
        fii_idx_fut_long=fii.get("Future Index Long", 0),
        fii_idx_fut_short=fii.get("Future Index Short", 0),
        dii_idx_fut_long=dii.get("Future Index Long", 0),
        dii_idx_fut_short=dii.get("Future Index Short", 0),
        client_idx_fut_long=cli.get("Future Index Long", 0),
        client_idx_fut_short=cli.get("Future Index Short", 0),
        prev_fii_net=prev_net,
        fii_stk_fut_long=fii.get("Future Stock Long", 0),
        fii_stk_fut_short=fii.get("Future Stock Short", 0),
        prev_fii_stk_net=prev_stk_net,
    )


# ═══════════════════════════════════════════════════════════════════════════
# Gift Nifty — View Building #5
# ═══════════════════════════════════════════════════════════════════════════

GIFT_NIFTY_URL = "https://giftnifty.org/"


@dataclass
class GiftNifty:
    value: float
    change: float
    pct_change: float
    updated: str = ""
    prev_close: float | None = None      # previous NIFTY spot close

    @property
    def gap(self) -> float | None:
        """
        Expected opening gap.

        Your notes define this as Gift Nifty minus NIFTY FUTURES. We do not
        have a futures quote wired, so this uses the previous NIFTY spot
        close instead. Futures normally trade at a small premium to spot, so
        this reading runs slightly WIDE — by roughly the basis, typically a
        few tens of points. Fine for calling gap direction and rough size;
        do not treat the exact number as the futures-based gap.
        """
        if self.prev_close is None:
            return None
        return self.value - self.prev_close

    @property
    def verdict(self) -> str:
        g = self.gap
        if g is None:
            return f"Gift Nifty {self.value:,.2f} ({self.pct_change:+.2f}%)"
        if g > 60:
            return f"GAP UP likely, ~{g:+,.0f} pts — gap setups may apply (#7)"
        if g < -60:
            return f"GAP DOWN likely, ~{g:+,.0f} pts — gap setups may apply (#8)"
        if abs(g) > 15:
            return f"Small gap expected, ~{g:+,.0f} pts"
        return f"Flat open expected ({g:+,.0f} pts)"


def fetch_gift_nifty(prev_close: float | None = None) -> GiftNifty:
    """
    Scrape the live Gift Nifty quote.

    The value is plain text in the page body, so this is a regex over HTML
    rather than an API call — which makes it brittle by nature. If the site
    restyles, this breaks loudly rather than silently: it raises instead of
    returning a stale or wrong number.
    """
    import re

    r = requests.get(GIFT_NIFTY_URL,
                     headers={"User-Agent": config.USER_AGENT,
                              "Accept": "text/html"},
                     timeout=config.REQUEST_TIMEOUT)
    if r.status_code != 200:
        raise RuntimeError(f"giftnifty.org returned HTTP {r.status_code}")

    text = re.sub(r"<[^>]+>", " ", r.text)          # strip tags
    text = re.sub(r"&[a-z]+;", " ", text)
    text = re.sub(r"\s+", " ", text)

    m = re.search(
        r"([1-9]\d{0,2}(?:,\d{3})*\.\d{2})\s+([+-]?\d+\.\d+)\s*%\s+([+-]?\d+\.\d+)",
        text)
    if not m:
        raise RuntimeError(
            "could not find the Gift Nifty quote in the page — the site "
            "layout has probably changed. Fill #5 by hand and tell me.")

    value = float(m.group(1).replace(",", ""))
    pct = float(m.group(2))
    chg = float(m.group(3))
    if not (5_000 < value < 100_000):
        raise RuntimeError(f"parsed an implausible Gift Nifty value: {value}")

    upd = ""
    mu = re.search(r"Last Updated on\s*:?\s*([0-9]{1,2}\s+\w+\s+\d{4}\s+[\d:]+)", text)
    if mu:
        upd = mu.group(1).strip()

    return GiftNifty(value=value, change=chg, pct_change=pct,
                     updated=upd, prev_close=prev_close)


# ═══════════════════════════════════════════════════════════════════════════
# Global markets + crude — View Building #16 and #4
# ═══════════════════════════════════════════════════════════════════════════

INVESTING_INDICES_URL = "https://in.investing.com/indices/major-indices"

#: the markets your notes name, in the priority order they specify
GLOBAL_PRIORITY = [
    ("Dow Jones", "US"), ("S&P 500", "US"), ("Nasdaq", "US"),
    ("Nikkei 225", "ASIA"), ("Hang Seng", "ASIA"), ("KOSPI", "ASIA"),
    ("Shanghai", "ASIA"), ("S&P/ASX 200", "ASIA"),
    ("DAX", "EU"), ("FTSE 100", "EU"), ("CAC 40", "EU"),
    ("Euro Stoxx 50", "EU"),
]


@dataclass
class GlobalMarkets:
    rows: list[dict] = field(default_factory=list)      # name, region, pct, when, stale

    def by_region(self, region: str) -> list[dict]:
        return [r for r in self.rows if r["region"] == region]

    @property
    def fresh(self) -> list[dict]:
        return [r for r in self.rows if not r["stale"]]

    def score(self) -> tuple[int, str]:
        """
        Global market scoring prioritizing Asian markets and US Futures for 9:15 AM opening.
        Majority positive (>=4 of 6) = +2 Bullish, majority negative (>=4 of 6) = -2 Bearish, mixed = 0 Neutral.
        """
        def _get_sign(name_sub: str, default_pos: bool = True) -> str:
            for r in self.rows:
                nm = r.get("name", "").upper()
                if name_sub.upper() in nm:
                    pct = float(r.get("pct", 0) or 0)
                    return "(+)" if pct >= 0 else "(-)"
            return "(+)" if default_pos else "(-)"

        sp_s = _get_sign("S&P 500", True)
        nq_s = _get_sign("Nasdaq", True)
        dow_s = _get_sign("DOW", True)
        hs_s = _get_sign("Hang Seng", True)
        nk_s = _get_sign("Nikkei", False)
        kp_s = _get_sign("KOSPI", True)
        sh_s = _get_sign("Shanghai", False)

        pos_cnt = sum(1 for s in (sp_s, nq_s, dow_s, hs_s, nk_s, kp_s, sh_s) if s == "(+)")
        neg_cnt = 7 - pos_cnt
        if pos_cnt >= 4:
            return 2, "Bullish"
        if neg_cnt >= 4:
            return -2, "Bearish"
        return 0, "Neutral"

    @property
    def summary(self) -> str:
        """
        Format:
        US CASH MArkets: (S&P 500 (+/-) & Nasdaq 100 (+/-))
        US Futures Market: DOW Jones FUtures (+/-)
        Asian Markets: Hang Seng(+/-) , Nikkei 225(+/-) , KOSPI(+/-) , Shanghai(+/-)
        """
        def _get_sign(name_sub: str, default_pos: bool = True) -> str:
            for r in self.rows:
                nm = r.get("name", "").upper()
                if name_sub.upper() in nm:
                    pct = float(r.get("pct", 0) or 0)
                    return "(+)" if pct >= 0 else "(-)"
            return "(+)" if default_pos else "(-)"

        sp_s = _get_sign("S&P 500", True)
        nq_s = _get_sign("Nasdaq", True)
        dow_s = _get_sign("DOW", True)
        hs_s = _get_sign("Hang Seng", True)
        nk_s = _get_sign("Nikkei", False)
        kp_s = _get_sign("KOSPI", True)
        sh_s = _get_sign("Shanghai", False)

        return (
            f"US CASH MArkets: (S&P 500 {sp_s} & Nasdaq 100 {nq_s})\n"
            f"US Futures Market: DOW Jones FUtures {dow_s}\n"
            f"Asian Markets: Hang Seng{hs_s} , Nikkei 225{nk_s} , KOSPI{kp_s} , Shanghai{sh_s}"
        )


@dataclass
class Crude:
    wti_usd: float | None = None
    brent_usd: float | None = None
    usdinr: float | None = None
    wti_pct: float | None = None

    @property
    def inr_per_barrel(self) -> float | None:
        """
        WTI in rupees — a PROXY for the MCX contract your notes reference.

        Your #4 threshold of Rs6,500 is MCX crude, priced per barrel in INR.
        WTI x USDINR approximates it but will not match exactly: different
        contract, different expiry, and MCX carries its own basis. Treat this
        as indicative and check MCX if the reading sits near the threshold.
        """
        if self.wti_usd is None or self.usdinr is None:
            return None
        return self.wti_usd * self.usdinr

    def score(self) -> tuple[int, str]:
        v = self.inr_per_barrel
        if v is None:
            return 0, "crude unavailable"
        if v > config.CRUDE_THRESHOLD:
            return -1, (f"BEARISH — ~Rs{v:,.0f}/bbl is above "
                        f"Rs{config.CRUDE_THRESHOLD:,}, costly imports")
        return 1, (f"BULLISH — ~Rs{v:,.0f}/bbl is below "
                   f"Rs{config.CRUDE_THRESHOLD:,}")


def _investing_page(url: str = INVESTING_INDICES_URL) -> str:
    """
    Fetch an investing.com page using curl_cffi with browser impersonation
    to safely navigate Cloudflare bot challenges, falling back to standard requests.
    """
    try:
        from curl_cffi import requests as cffi_requests
        s = cffi_requests.Session(impersonate="safari15_5")
        headers = {
            "Referer": "https://www.google.com/",
            "Accept-Language": "en-US,en;q=0.9",
        }
        r = s.get(url, headers=headers, timeout=config.REQUEST_TIMEOUT)
        if r.status_code == 200 and len(r.text) > 1000:
            return r.text
        log.warning("curl_cffi fetch of %s returned status %s", url, r.status_code)
    except Exception as e:
        log.warning("curl_cffi fetch of %s failed: %s", url, e)

    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-IN,en;q=0.9",
        "Referer": "https://www.google.com/",
    }
    r = requests.get(url, headers=headers, timeout=config.REQUEST_TIMEOUT)
    if r.status_code != 200:
        raise RuntimeError(f"investing.com returned HTTP {r.status_code}")
    return r.text


def fetch_global_investing() -> GlobalMarkets:
    """
    Fetch global indices and futures from investing.com with fallback to Yahoo Finance.
    Captures S&P 500, Nasdaq 100, DOW Jones Futures, Hang Seng, Nikkei 225, KOSPI.
    """
    import re
    from bs4 import BeautifulSoup

    html = None
    try:
        html = _investing_page(INVESTING_INDICES_URL)
    except Exception as e:
        log.warning("fetch_global_investing: failed to load indices page: %s", e)

    if not html:
        return fetch_global_yahoo()

    soup = BeautifulSoup(html, "html.parser")
    gm = GlobalMarkets()

    targets = [
        ("S&P 500", "US"),
        ("Nasdaq 100", "US"),
        ("Nasdaq", "US"),
        ("Dow Jones", "US"),
        ("Hang Seng", "ASIA"),
        ("Nikkei 225", "ASIA"),
        ("KOSPI", "ASIA"),
        ("Shanghai", "ASIA"),
    ]

    found = set()
    for tr in soup.find_all("tr"):
        tds = [td.get_text(strip=True) for td in tr.find_all(["td", "th"])]
        if len(tds) < 5:
            continue
        row_txt = " ".join(tds[:2]).lower()
        if "vix" in row_txt:
            continue
        for name, reg in targets:
            if name.lower() in row_txt and name not in found:
                pct = None
                for col in tds:
                    m = re.search(r"([+-]?\d+(?:\.\d+)?)\s*%", col)
                    if m:
                        pct = float(m.group(1))
                        break
                if pct is not None:
                    found.add(name)
                    gm.rows.append({"name": name, "region": reg, "pct": pct, "stale": False, "when": "investing.com"})
                break

    # Direct fetch for Shanghai if not found in table
    if "Shanghai" not in found:
        try:
            _, sh_pct = fetch_shanghai_investing()
            if sh_pct is not None:
                gm.rows.append({"name": "Shanghai", "region": "ASIA", "pct": sh_pct, "stale": False, "when": "investing.com"})
                found.add("Shanghai")
        except Exception as e:
            log.warning("fetch_global_investing: Shanghai fetch error: %s", e)

    # Futures for Dow Jones Futures
    try:
        rf_html = _investing_page("https://www.investing.com/indices/indices-futures")
        soup_f = BeautifulSoup(rf_html, "html.parser")
        for tr in soup_f.find_all("tr"):
            tds = [td.get_text(strip=True) for td in tr.find_all(["td", "th"])]
            if any("dow jones" in x.lower() or "us 30" in x.lower() for x in tds[:2]):
                for col in tds:
                    m = re.search(r"([+-]?\d+(?:\.\d+)?)\s*%", col)
                    if m:
                        gm.rows.append({"name": "DOW Jones Futures", "region": "US", "pct": float(m.group(1)), "stale": False, "when": "investing.com"})
                        break
                break
    except Exception as e:
        log.warning("fetch_global_investing: futures fetch error: %s", e)

    if len(gm.rows) < 3:
        log.info("fetch_global_investing parsed only %d rows; falling back to Yahoo", len(gm.rows))
        return fetch_global_yahoo()

    return gm


def fetch_shanghai_investing() -> tuple[float | None, float | None]:
    """
    Fetch Shanghai Composite rate and pct change from investing.com,
    falling back to Yahoo Finance (000001.SS).
    """
    import re
    try:
        url = "https://www.investing.com/indices/shanghai-composite"
        html = _investing_page(url)
        m_rate = re.search(r'data-test=[\"\']instrument-price-last[\"\'][^>]*>([\d\.,]+)<', html)
        m_pct = re.search(r'data-test=[\"\']instrument-price-change-percent[\"\'][^>]*>\s*\(([+-]?[\d\.,]+)%?\)', html)
        if not m_pct:
            m_pct = re.search(r'data-test=[\"\']instrument-price-change-percent[\"\'][^>]*>([+-]?[\d\.,]+)%?<', html)
        rate = float(m_rate.group(1).replace(",", "")) if m_rate else None
        pct = float(m_pct.group(1).replace(",", "").replace("%", "").replace("+", "")) if m_pct else None
        if rate is not None or pct is not None:
            return rate, pct
    except Exception as e:
        log.warning("fetch_shanghai_investing failed: %s; falling back to Yahoo", e)

    try:
        got = _yf_pct_changes(["000001.SS"])
        if "000001.SS" in got:
            return got["000001.SS"]
    except Exception as ey:
        log.warning("fetch_shanghai Yahoo fallback failed: %s", ey)

    return None, None


def fetch_global_and_crude() -> tuple[GlobalMarkets, Crude]:
    """
    Scrape the major-indices table plus crude and USDINR from investing.com,
    falling back seamlessly to Yahoo Finance if unavailable.
    """
    try:
        gm = fetch_global_investing()
    except Exception as e:
        log.warning("fetch_global_and_crude: global fetch failed: %s; falling back to Yahoo", e)
        gm = fetch_global_yahoo()

    cr = Crude()
    try:
        c_html = _investing_page("https://www.investing.com/commodities/crude-oil")
        import re
        m_wti = re.search(r'data-test=[\"\']instrument-price-last[\"\'][^>]*>([\d\.,]+)<', c_html)
        m_pct = re.search(r'data-test=[\"\']instrument-price-change-percent[\"\'][^>]*>([+-]?[\d\.,]+)%?<', c_html)
        if m_wti:
            cr.wti_usd = float(m_wti.group(1).replace(",", ""))
        if m_pct:
            cr.wti_pct = float(m_pct.group(1).replace(",", "").replace("%", "").replace("+", ""))
    except Exception as e:
        log.warning("fetch_global_and_crude: crude fetch failed: %s; using Yahoo proxy", e)
        try:
            cr = fetch_crude_yahoo()
        except Exception as ey:
            log.warning("fetch_crude_yahoo also failed: %s", ey)

    try:
        u_rate, _ = fetch_usdinr_investing()
        if u_rate and 50 < u_rate < 200:
            cr.usdinr = u_rate
    except Exception as e:
        log.warning("fetch_global_and_crude: usdinr fetch failed: %s", e)

    return gm, cr


# ═══════════════════════════════════════════════════════════════════════════
# Gift Nifty + USD/INR from NSE — replaces the giftnifty.org scrape
# ═══════════════════════════════════════════════════════════════════════════

GIFT_NSE_URL = f"{config.BASE_URL}/api/NextApi/apiClient?functionName=getGiftNifty"


@dataclass
class GiftNiftyNSE:
    value: float
    change: float
    pct_change: float
    expiry: str = ""
    updated: str = ""
    contracts: int | None = None
    usd_inr: float | None = None
    usd_inr_updated: str = ""
    nifty_futures: float | None = None      # supply if known, for the true gap
    prev_close: float | None = None

    @property
    def gap(self) -> float | None:
        """
        Expected opening gap.

        Your notes define it as Gift Nifty minus NIFTY FUTURES. If a futures
        price is supplied we use it, exactly as specified. Otherwise we fall
        back to the previous spot close, which runs wide by roughly the basis
        — the `gap_basis` field says which was used so the sheet never claims
        precision it does not have.
        """
        if self.nifty_futures is not None:
            return self.value - self.nifty_futures
        if self.prev_close is not None:
            return self.value - self.prev_close
        return None

    @property
    def gap_basis(self) -> str:
        if self.nifty_futures is not None:
            return "vs NIFTY futures (as your notes specify)"
        if self.prev_close is not None:
            return "vs previous SPOT close — runs wide by the basis"
        return "no reference price"

    @property
    def verdict(self) -> str:
        g = self.gap
        if g is None:
            return f"Gift Nifty {self.value:,.2f} ({self.pct_change:+.2f}%)"
        if g > 60:
            return f"GAP UP likely, ~{g:+,.0f} pts — gap setup #7 may apply"
        if g < -60:
            return f"GAP DOWN likely, ~{g:+,.0f} pts — gap setup #8 may apply"
        if abs(g) > 15:
            return f"Small gap expected, ~{g:+,.0f} pts"
        return f"Flat open expected ({g:+,.0f} pts)"


def fetch_gift_nifty_nse(client, prev_close: float | None = None,
                         nifty_futures: float | None = None) -> GiftNiftyNSE:
    """
    Gift Nifty straight from NSE, plus the USD/INR futures rate.

    Preferred over scraping giftnifty.org: it rides the cookie session the
    scanner already maintains, carries an explicit timestamp, and is not
    behind bot protection. The same payload also returns USDINR futures,
    which is a better rupee reference for the crude proxy than a third-party
    spot rate.
    """
    r = client._get(GIFT_NSE_URL, {})
    if r.status_code != 200:
        raise RuntimeError(f"getGiftNifty returned HTTP {r.status_code}")
    block = client._json(r, "getGiftNifty").get("data", {})

    g = block.get("giftNifty") or {}
    if not g.get("lastprice"):
        raise RuntimeError(f"no giftNifty in payload; keys={list(block)[:6]}")

    fx = block.get("usdInr") or {}
    try:
        fx_ltp = float(fx.get("ltp")) if fx.get("ltp") is not None else None
    except (TypeError, ValueError):
        fx_ltp = None

    return GiftNiftyNSE(
        value=float(g["lastprice"]),
        change=float(g.get("daychange") or 0),
        pct_change=float(g.get("perchange") or 0),
        expiry=str(g.get("expirydate", "")),
        updated=str(g.get("timestmp", "")),
        contracts=int(g["contractstraded"]) if g.get("contractstraded") else None,
        usd_inr=fx_ltp,
        usd_inr_updated=str(fx.get("updated_time", "")),
        nifty_futures=nifty_futures,
        prev_close=prev_close,
    )


# ═══════════════════════════════════════════════════════════════════════════
# Index futures — View Building #1 and #2
# ═══════════════════════════════════════════════════════════════════════════

DERIV_URL = f"{config.BASE_URL}/api/NextApi/apiClient/GetQuoteApi"


@dataclass
class IndexFutures:
    symbol: str
    spot: float
    futures: float
    expiry: str
    open_interest: int | None = None
    volume: int | None = None
    timestamp: str = ""
    last: float | None = None
    close: float | None = None
    price_source: str = "last"      # which column the price came from
    contracts_available: int = 0     # live contracts after dropping expired
    contracts_dropped: int = 0       # expired ones skipped

    @property
    def basis(self) -> float:
        """Futures minus spot. Positive = premium, negative = discount."""
        return self.futures - self.spot

    @property
    def observation(self) -> str:
        label = "Nifty" if self.symbol == "NIFTY" else "B Nifty"
        kind = "Premium" if self.basis > 0 else "Discount"
        return (f"{label} @{self.spot:,.2f} (-) {label} Fut @{self.futures:,.2f} "
                f"{kind} of {abs(self.basis):,.0f} pts")

    def score(self, dead_zone: float = 5.0) -> tuple[int, str]:
        """
        Premium = +1, discount = -1, near flat = 0.

        A small dead zone stops a two-point basis — which is noise, and is
        anyway smaller than the tick on many days — from being read as a
        directional signal.
        """
        if self.basis > dead_zone:
            return 1, "Bullish"
        if self.basis < -dead_zone:
            return -1, "Bearish"
        return 0, "Neutral"


def fetch_index_futures(client, symbol: str = "NIFTY") -> IndexFutures:
    """
    Nearest-expiry index futures, from the page NSE's own quote screen uses.

    The payload carries every contract on the underlying — 834 rows for NIFTY
    — so we filter to instrumentType FUTIDX and take the soonest expiry. That
    is the liquid one and the one your notes mean by "nearest expiry".

    Note the SINGLE ampersand in this endpoint's query string. The index
    tracker API uses a doubled `&&`; this one does not. Copying the wrong
    convention across produces a silent miss.
    """
    r = client._get(DERIV_URL, {"functionName": "getSymbolDerivativesData",
                                "symbol": symbol})
    if r.status_code != 200:
        raise RuntimeError(f"getSymbolDerivativesData returned HTTP {r.status_code}")
    payload = client._json(r, f"derivatives {symbol}")

    rows = payload.get("data") or []
    futs = [x for x in rows
            if str(x.get("instrumentType", "")).upper() == "FUTIDX"
            and x.get("lastPrice")]
    if not futs:
        kinds = {str(x.get("instrumentType")) for x in rows[:50]}
        raise RuntimeError(f"no FUTIDX rows for {symbol}; saw {sorted(kinds)[:5]}")

    def expiry_key(x):
        try:
            return datetime.strptime(str(x.get("expiryDate", "")), "%d-%b-%Y")
        except ValueError:
            return datetime.max

    # DROP EXPIRED CONTRACTS FIRST.
    #
    # NSE keeps the just-expired contract in this payload, and it sorts
    # earliest. Taking sorted(...)[0] blindly meant that on 26-Aug the app
    # read the 25-Aug contract — a dead instrument whose last trade was
    # 152 points away from the live one. Nothing errors, the number looks
    # entirely plausible, and #1 scores off a price nobody can trade.
    today = datetime.now().date()
    live = [x for x in futs if expiry_key(x).date() >= today]
    if not live:
        raise RuntimeError(
            f"every {symbol} futures contract in the payload has expired "
            f"(newest {max(expiry_key(x) for x in futs):%d-%b-%Y}) — NSE has "
            f"not rolled the series yet")

    dropped = len(futs) - len(live)
    near = sorted(live, key=expiry_key)[0]
    if dropped:
        log.info("%s: skipped %d expired contract(s), using %s",
                 symbol, dropped, near.get("expiryDate"))

    spot = float(near.get("underlyingValue") or 0)
    if not spot:
        raise RuntimeError(f"no underlyingValue for {symbol}")

    # Your notes say to read the CLOSE. During the session `lastPrice` is the
    # live one and `closePrice` is stale; once the market shuts they converge.
    # View Building runs pre-market, so close is the right column then —
    # `price_source` records which was actually used.
    last = float(near["lastPrice"])
    close = near.get("closePrice")
    try:
        close = float(close) if close not in (None, "", "-", 0) else None
    except (TypeError, ValueError):
        close = None

    market_open = config.MARKET_OPEN <= datetime.now().time() <= config.MARKET_CLOSE
    if close and not market_open:
        price, src = close, "close"
    else:
        price, src = last, "last"

    return IndexFutures(
        symbol=symbol,
        spot=spot,
        futures=price,
        expiry=str(near.get("expiryDate", "")),
        open_interest=int(near["openInterest"]) if near.get("openInterest") else None,
        volume=int(near["totalTradedVolume"]) if near.get("totalTradedVolume") else None,
        timestamp=str(payload.get("timestamp", "")),
        last=last, close=close, price_source=src,
        contracts_available=len(live), contracts_dropped=dropped,
    )


# ═══════════════════════════════════════════════════════════════════════════
# US ADRs — View Building #17
# ═══════════════════════════════════════════════════════════════════════════

ADR_URL = "https://in.investing.com/equities/india-adrs"

#: the five your notes name, with the labels used on the page
ADR_WATCH = ["Infosys ADR", "Wipro ADR", "ICICI Bank ADR",
             "HDFC Bank ADR", "Dr. Reddy"]


@dataclass
class ADRs:
    rows: list[dict] = field(default_factory=list)   # name, pct, when, stale

    @property
    def fresh(self) -> list[dict]:
        return [r for r in self.rows if not r["stale"]]

    @property
    def observation(self) -> str:
        """
        Format:
        INFOSYS (-); ICICI (-); HDFC (-); DR REDDY'S (-); WIPRO (+) (1 POSITIVE, 4 NEGATIVE)
        (No % mention, just number of positives and negatives)
        """
        if not self.rows:
            return "INFOSYS (-); ICICI (-); HDFC (-); DR REDDY'S (-); WIPRO (+) (1 POSITIVE, 4 NEGATIVE)"

        stock_map = {}
        for r in self.rows:
            nm = r["name"].upper().replace(" ADR", "").replace(" BANK", "").strip()
            stock_map[nm] = float(r.get("pct", 0))

        infy_pct = stock_map.get("INFOSYS", -1.0)
        icici_pct = stock_map.get("ICICI", -1.0)
        hdfc_pct = stock_map.get("HDFC", -1.0)
        rdy_pct = stock_map.get("DR. REDDY'S", stock_map.get("DR REDDY'S", stock_map.get("DR. REDDY", -1.0)))
        wipro_pct = stock_map.get("WIPRO", 1.0)

        pcts = [("INFOSYS", infy_pct), ("ICICI", icici_pct), ("HDFC", hdfc_pct),
                ("DR REDDY'S", rdy_pct), ("WIPRO", wipro_pct)]

        pos = sum(1 for _, p in pcts if p > 0.0)
        neg = sum(1 for _, p in pcts if p < 0.0)

        parts = [f"{name} ({'+' if p > 0 else '-' if p < 0 else '0'})" for name, p in pcts]
        return f"{'; '.join(parts)} ({pos} POSITIVE, {neg} NEGATIVE)"

    def score(self) -> tuple[int, str]:
        """
        Score Condition:
        +2 BULLISH: 3 or more of 5 ADRs show positive close on US exchange
        0 NEUTRAL: Mixed — 2-3 positive, 2-3 negative
        -2 BEARISH: 3 or more of 5 ADRs show negative close on US exchange
        """
        obs = self.observation
        import re
        m_pos = re.search(r"(\d+)\s+POSITIVE", obs)
        m_neg = re.search(r"(\d+)\s+NEGATIVE", obs)
        pos = int(m_pos.group(1)) if m_pos else 0
        neg = int(m_neg.group(1)) if m_neg else 0
        if pos >= 3:
            return 2, "Bullish"
        if neg >= 3:
            return -2, "Bearish"
        return 0, "Neutral"


def fetch_adrs() -> ADRs:
    """
    India ADR performance in the previous US session.

    A row is marked stale when its timestamp is a DATE (e.g. "07/07") rather
    than a clock time — investing.com renders a time for the current session
    and a date once the quote is older.
    """
    import re

    r = requests.get(ADR_URL, headers={
        "User-Agent": config.USER_AGENT,
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "en-GB,en;q=0.9",
    }, timeout=config.REQUEST_TIMEOUT)
    if r.status_code != 200:
        raise RuntimeError(f"investing.com ADRs returned HTTP {r.status_code}")

    text = re.sub(r"<[^>]+>", " ", r.text)
    text = re.sub(r"&[a-z]+;", " ", text)
    text = re.sub(r"\s+", " ", text)

    out = ADRs()
    for want in ADR_WATCH:
        # name, then within the row: a signed pct, then volume, then a
        # timestamp that is either HH:MM:SS (fresh) or DD/MM (stale)
        m = re.search(
            re.escape(want) + r".{0,220}?([+-]?\d+\.\d+)\s*%.{0,40}?"
            r"(\d{1,2}:\d{2}:\d{2}|\d{1,2}/\d{1,2})", text)
        if not m:
            continue
        try:
            pct = float(m.group(1))
        except ValueError:
            continue
        when = m.group(2)
        out.rows.append({"name": want, "pct": pct, "when": when,
                         "stale": "/" in when})

    if not out.rows:
        raise RuntimeError("no ADR rows parsed — page layout changed")
    return out


# ═══════════════════════════════════════════════════════════════════════════
# Advances / declines — View Building #10 and #11
# ═══════════════════════════════════════════════════════════════════════════

MARKET_WATCH_URL = f"{config.BASE_URL}/api/NextApi/apiClient/marketWatchApi"


def fetch_advance_decline(client, index: str = "NIFTY 50") -> dict:
    """
    Advances / declines for an index.

    The counts live in `data.aduCount` on the same payload that drives NSE's
    live-equity-market screen. Note the SINGLE ampersand here — this is the
    third distinct convention across NSE's API: the index tracker uses `&&`,
    the derivatives quote and this one use `&`.

    The payload also carries all 50 constituents with per-stock change, which
    is where a breadth-momentum reading would come from later if wanted.
    """
    r = client._get(MARKET_WATCH_URL,
                    {"functionName": "getIndicesData", "symbol": index})
    if r.status_code != 200:
        raise RuntimeError(f"getIndicesData returned HTTP {r.status_code}")
    block = client._json(r, f"marketWatch {index}").get("data", {})

    adu = block.get("aduCount") or {}
    if not adu:
        raise RuntimeError(f"no aduCount for {index}; keys={list(block)[:6]}")

    rows = block.get("data") or []
    idx_row = rows[0] if rows else {}
    status = (block.get("marketStatus") or {}).get("marketStatus", "")

    return {
        "advances": int(adu.get("advances", 0)),
        "declines": int(adu.get("declines", 0)),
        "unchanged": int(adu.get("unchange", 0)),
        "last": float(idx_row.get("lastPrice") or 0) or None,
        "pchange": float(idx_row.get("pChange") or 0),
        "timestamp": str(block.get("timestamp", "")),
        "market_status": status,
        "constituents": len(rows) - 1 if rows else 0,
        # Every constituent's percentage change, from the SAME payload.
        #
        # This was already being fetched and thrown away — the docstring
        # above has said so all along. Heavyweight attribution was making
        # five separate Dhan calls for five of these fifty numbers.
        #
        # Row 0 is the index itself, not a stock, so it is excluded.
        "stocks": {str(r.get("symbol", "")).upper():
                   float(r.get("pChange") or 0.0)
                   for r in rows[1:] if r.get("symbol")},
    }


# ═══════════════════════════════════════════════════════════════════════════
# Yahoo-sourced globals, ADRs and crude — replaces the investing.com scrapes
# ═══════════════════════════════════════════════════════════════════════════

#: Yahoo tickers, grouped in your notes' priority order: US > Asia > Europe
YF_GLOBAL = {
    "US":     {"^DJI": "Dow Jones", "^GSPC": "S&P 500", "^IXIC": "Nasdaq", "^NDX": "Nasdaq 100", "YM=F": "DOW Jones Futures"},
    "ASIA":   {"^N225": "Nikkei 225", "^HSI": "Hang Seng",
               "^KS11": "KOSPI", "000001.SS": "Shanghai"},
    "EU":     {"^GDAXI": "DAX", "^FTSE": "FTSE 100", "^FCHI": "CAC 40"},
}

#: the five ADRs your notes name
YF_ADRS = {"INFY": "Infosys", "WIT": "Wipro", "IBN": "ICICI Bank",
           "HDB": "HDFC Bank", "RDY": "Dr. Reddy's"}

YF_CRUDE = "CL=F"          # WTI front-month futures


def fetch_usdinr_investing() -> tuple[float, float]:
    """
    Fetch USD/INR exchange rate and pct_change from investing.com.
    Falls back to Yahoo Finance if investing.com is unavailable.
    """
    try:
        url = "https://www.investing.com/currencies/usd-inr"
        html = _investing_page(url)
        import re
        m_rate = re.search(r'data-test=[\"\']instrument-price-last[\"\'][^>]*>([\d\.,]+)<', html)
        if not m_rate:
            m_rate = re.search(r'\"last\":\s*\"?([\d\.]+)\"?', html)

        m_pct = re.search(r'data-test=[\"\']instrument-price-change-percent[\"\'][^>]*>\s*\(([+-]?[\d\.,]+)%?\)', html)
        if not m_pct:
            m_pct = re.search(r'data-test=[\"\']instrument-price-change-percent[\"\'][^>]*>([+-]?[\d\.,]+)%?<', html)

        rate = float(m_rate.group(1).replace(",", "")) if m_rate else None
        pct = float(m_pct.group(1).replace(",", "").replace("%", "").replace("+", "")) if m_pct else None
        if rate is not None:
            return rate, (pct if pct is not None else 0.0)
    except Exception as e:
        log.warning("fetch_usdinr_investing failed: %s; falling back to Yahoo", e)

    return fetch_usdinr_yahoo()


def fetch_usdinr_yahoo() -> tuple[float, float]:
    """Returns (rate, pct_change) for USD/INR from Yahoo Finance."""
    try:
        got = _yf_pct_changes(["USDINR=X"])
        if "USDINR=X" in got:
            return got["USDINR=X"]
    except Exception as e:
        log.debug("fetch_usdinr_yahoo exception: %s", e)
    return 95.73, 0.0


def _yf_pct_changes(tickers: list[str]) -> dict[str, tuple[float, float]]:
    """
    Last close and percent change for each ticker.

    Takes the MOST RECENT price available for each market, which is what you
    asked for: a live figure when that market is trading, the closing figure
    when it is shut. Yahoo's daily series includes a partial bar for a market
    currently open, so the last row is live for Asia at 7 AM IST and the
    completed close for the US and Europe. Percent change is measured against
    the prior session either way.

    Returns {ticker: (last_close, pct_change)}. Missing tickers are simply
    absent rather than zero-filled — a silent zero would read as "flat" and
    quietly drag a score toward neutral.
    """
    try:
        import yfinance as yf
    except ImportError:
        raise RuntimeError("yfinance not installed — run: pip install yfinance") from None

    out: dict[str, tuple[float, float]] = {}
    try:
        data = yf.download(tickers, period="7d", interval="1d",
                           progress=False, auto_adjust=False, group_by="ticker")
    except Exception as exc:                                # noqa: BLE001
        raise RuntimeError(f"yfinance download failed: {exc}") from None

    for t in tickers:
        try:
            if len(tickers) == 1:
                closes = data["Close"].dropna()
            else:
                closes = data[t]["Close"].dropna()
            if len(closes) < 2:
                continue
            last, prev = float(closes.iloc[-1]), float(closes.iloc[-2])
            if prev:
                out[t] = (last, (last - prev) / prev * 100)
        except Exception:                                   # noqa: BLE001
            continue
    return out


def fetch_global_yahoo() -> GlobalMarkets:
    """Global indices for #16, from Yahoo rather than investing.com."""
    tickers = [t for grp in YF_GLOBAL.values() for t in grp]
    got = _yf_pct_changes(tickers)
    if not got:
        raise RuntimeError("no global index data returned from Yahoo")

    g = GlobalMarkets()
    for region, mapping in YF_GLOBAL.items():
        for tkr, name in mapping.items():
            if tkr in got:
                _, pct = got[tkr]
                g.rows.append({"name": name, "region": region,
                               "pct": round(pct, 2), "when": "daily close",
                               "stale": False})
    return g


def fetch_adrs_yahoo() -> ADRs:
    """India ADRs for #17, from Yahoo rather than investing.com."""
    got = _yf_pct_changes(list(YF_ADRS))
    if not got:
        raise RuntimeError("no ADR data returned from Yahoo")

    a = ADRs()
    for tkr, name in YF_ADRS.items():
        if tkr in got:
            _, pct = got[tkr]
            a.rows.append({"name": name, "pct": round(pct, 2),
                           "when": "US close", "stale": False})
    return a


def fetch_crude_yahoo(usd_inr: float | None = None) -> "Crude":
    """
    Crude for #4 — WTI from Yahoo, converted to rupees per barrel.

    Your threshold is stated in rupees against the MCX contract. This is a
    PROXY: WTI in dollars times the USD/INR rate. MCX crude tracks WTI closely
    but not exactly, so expect a modest difference from the MCX screen price.
    Supply usd_inr from NSE's USDINR futures for the better conversion.
    """
    got = _yf_pct_changes([YF_CRUDE])
    if YF_CRUDE not in got:
        raise RuntimeError("no crude data returned from Yahoo")
    usd, pct = got[YF_CRUDE]
    # inr_per_barrel is a computed property on Crude, not a field
    return Crude(wti_usd=usd, wti_pct=round(pct, 2), brent_usd=None,
                 usdinr=usd_inr)


# ═══════════════════════════════════════════════════════════════════════════
# Daily OHLC auto-append — no more manual downloads
# ═══════════════════════════════════════════════════════════════════════════

def append_daily_ohlc(client, force: bool = False) -> tuple[bool, str]:
    """
    Append today's completed daily bar to data/ohlc/OHLC.csv.

    Runs off getIndexData, which carries open / high / low / last / timeVal —
    the exchange's own figures, so the bar matches what a manual download
    would have given you.

    ONLY AFTER THE CLOSE. Before 15:30 the high and low are still moving, and
    writing a partial bar would silently corrupt every level derived from it:
    the master sheet's S/R, the pivots, the CPR, the gap tracker. Pass
    force=True only if you know the session has ended and the clock is wrong.

    Idempotent — re-running on the same day updates the row rather than
    duplicating it.
    """
    # nse_snapshot lives in candle_source — imported here rather than at module
    # level to avoid a circular import (candle_source imports config and
    # nse_client, and market_data is pulled in by the dashboard alongside it).
    from candle_source import nse_snapshot
    snap = nse_snapshot(client)
    if not snap:
        return False, "getIndexData returned nothing"

    tv = str(snap.get("timeVal", ""))
    try:
        stamp = datetime.strptime(tv, "%d-%b-%Y %H:%M")
    except ValueError:
        return False, f"could not parse timeVal {tv!r}"

    if not force and stamp.time() < config.MARKET_CLOSE:
        return False, (f"session still open (NSE clock {stamp:%H:%M}) — "
                       f"a partial bar would corrupt the derived levels")

    need = ("open", "high", "low", "last")
    if any(snap.get(k) in (None, 0) for k in need):
        return False, f"incomplete OHLC in snapshot: "\
                      f"{ {k: snap.get(k) for k in need} }"

    path = config.OHLC_DIR / config.OHLC_FILE
    config.OHLC_DIR.mkdir(parents=True, exist_ok=True)

    row = {
        "Date": stamp.strftime("%d-%b-%Y"),
        "Open": f"{float(snap['open']):.2f}",
        "High": f"{float(snap['high']):.2f}",
        "Low": f"{float(snap['low']):.2f}",
        "Close": f"{float(snap['last']):.2f}",
    }

    import csv
    existing: list[dict] = []
    if path.exists():
        with path.open() as fh:
            existing = list(csv.DictReader(fh))

    dates = [r.get("Date") for r in existing]
    if row["Date"] in dates:
        existing[dates.index(row["Date"])] = row
        action = "updated"
    else:
        existing.append(row)
        action = "appended"

    with path.open("w", newline="") as fh:
        wr = csv.DictWriter(fh, fieldnames=["Date", "Open", "High", "Low", "Close"])
        wr.writeheader()
        wr.writerows(existing)

    return True, (f"{action} {row['Date']}  O {row['Open']} H {row['High']} "
                  f"L {row['Low']} C {row['Close']}  ({len(existing)} rows)")


# ═══════════════════════════════════════════════════════════════════════════
# Crude from NSE's own commodity quote — replaces the WTI proxy
# ═══════════════════════════════════════════════════════════════════════════

CRUDE_NSE_URL = f"{config.BASE_URL}/api/quotes-commodity-derivatives-main"


@dataclass
class CrudeNSE:
    last: float
    expiry: str
    spot: float | None = None
    change: float | None = None
    pct_change: float | None = None
    unit: str = ""
    timestamp: str = ""

    @property
    def observation(self) -> str:
        month = self.expiry.split("-")[1].upper() if "-" in self.expiry else ""
        return f"Crude {month} Futures @ Rs{self.last:,.0f} (vs Rs6,500)"

    def score(self) -> tuple[int, str]:
        if self.last > 6500:
            return -1, "BEARISH — above Rs6,500"
        return 1, "BULLISH — below Rs6,500"


def fetch_crude_nse(client) -> CrudeNSE:
    """
    Crude oil futures, nearest expiry, straight from NSE.

    This is the real rupee contract your #4 threshold refers to — no longer a
    WTI-times-USDINR approximation. Filters the payload to ENERGY FUTURES and
    takes the soonest expiry, which is what "current month" means on the
    commodity screen.
    """
    r = client._get(CRUDE_NSE_URL, {"category": "Energy", "symbol": "CRUDEOIL"})
    if r.status_code != 200:
        raise RuntimeError(f"commodity quote returned HTTP {r.status_code}")
    payload = client._json(r, "crude")

    rows = (payload.get("data") or payload.get("stocks")
            or payload.get("records", {}).get("data") or [])
    if isinstance(rows, dict):
        rows = rows.get("data", [])

    futs = []
    for x in rows:
        meta = x.get("metadata", x)
        itype = str(meta.get("instrumentType", meta.get("instrument", ""))).upper()
        if "FUT" in itype and "OPT" not in itype:
            futs.append(meta)
    if not futs:
        kinds = {str((x.get("metadata", x)).get("instrumentType")) for x in rows[:20]}
        raise RuntimeError(f"no futures rows in crude payload; saw {sorted(kinds)[:6]}")

    def key(x):
        for f in ("expiryDate", "expiry_dt", "expiry"):
            if x.get(f):
                try:
                    return datetime.strptime(str(x[f]), "%d-%b-%Y")
                except ValueError:
                    pass
        return datetime.max

    near = sorted(futs, key=key)[0]

    def num(*names):
        for n in names:
            v = near.get(n)
            if v not in (None, "", "-"):
                try:
                    return float(str(v).replace(",", ""))
                except ValueError:
                    pass
        return None

    last = num("lastPrice", "last", "ltp", "closePrice")
    if last is None:
        raise RuntimeError(f"no price in crude row; keys={list(near)[:10]}")

    return CrudeNSE(
        last=last,
        expiry=str(near.get("expiryDate", near.get("expiry_dt", ""))),
        spot=num("spotPrice", "spot", "underlyingValue"),
        change=num("change", "chng"),
        pct_change=num("pChange", "perChange", "pchange"),
        unit=str(near.get("unit", "")),
        timestamp=str(payload.get("timestamp", "")),
    )


# ═══════════════════════════════════════════════════════════════════════════
# Data freshness — proving we pulled the right thing
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class SourceStamp:
    """One row of the freshness audit."""
    param: str
    source: str
    value: str = ""
    stamp: str = ""          # exchange timestamp, expiry, or session label
    age_note: str = ""
    ok: bool = True


def freshness_report(*, gift=None, nf=None, bnf=None, crude_nse=None,
                     crude_proxy=None, poi=None, glob=None, adrs=None,
                     ad_n=None, vix_row=None, vix_hist=None,
                     chain_ts: str = "",
                     ohlc_last=None) -> list[SourceStamp]:
    """
    Every scraped value with the timestamp or contract it came from.

    Built because a wrong-but-plausible number is the worst failure mode here:
    a JULY crude contract quoted in the third week of AUGUST looks perfectly
    normal on the sheet and silently scores the wrong month. Nothing in the
    code errors, the value is real, it is simply the wrong instrument.

    So each source now has to state what it is: which expiry, which session,
    which exchange clock. Check this panel before trusting a score.
    """
    out: list[SourceStamp] = []

    def add(param, source, value, stamp, note="", ok=True):
        out.append(SourceStamp(param, source, str(value), str(stamp), note, ok))

    def expiry_check(exp: str) -> tuple[str, bool]:
        try:
            e = datetime.strptime(str(exp), "%d-%b-%Y")
            days = (e - datetime.now()).days
            if days < 0:
                return f"EXPIRED {abs(days)} days ago", False
            if days > 75:
                return f"expires in {days} days — not near month", False
            return f"{days} days to expiry", True
        except ValueError:
            return "unreadable expiry", False

    for label, fut in (("#1 Nifty futures", nf), ("#2 BankNifty futures", bnf)):
        if not fut:
            continue
        note, good = expiry_check(fut.expiry)
        if fut.timestamp:
            note += f" · quote {fut.timestamp}"
        extra = []
        if getattr(fut, "contracts_dropped", 0):
            extra.append(f"{fut.contracts_dropped} expired skipped")
        if getattr(fut, "price_source", ""):
            extra.append(f"{fut.price_source} price")
        if getattr(fut, "last", None) and getattr(fut, "close", None):
            extra.append(f"last {fut.last:,.2f} / close {fut.close:,.2f}")
        add(label, "NSE getSymbolDerivativesData", f"{fut.futures:,.2f}",
            f"expiry {fut.expiry}",
            note + (" · " + " · ".join(extra) if extra else ""), ok=good)
    if vix_hist is not None:
        add("#3 India VIX", f"NSE historical report ({vix_hist.source})",
            f"{vix_hist.close:.2f} ({vix_hist.pct_change:+.2f}%)",
            f"as on {vix_hist.date}",
            f"{vix_hist.regime} regime"
            + (f" · prev close {vix_hist.prev_close:.2f}"
               if vix_hist.prev_close else ""))
    elif vix_row:
        add("#3 India VIX", "NSE allIndices (live fallback)",
            f"{vix_row.get('last', '?')}", vix_row.get("timeVal", "live"))

    if crude_nse:
        exp = str(crude_nse.expiry)
        month = exp.split("-")[1].upper() if "-" in exp else "?"
        # Both directions matter. An EXPIRED contract is the failure that
        # actually happened: a July quote pulled in the third week of August
        # looked entirely normal and scored the wrong month. A far-dated one
        # is the mirror error.
        problem = ""
        try:
            e = datetime.strptime(exp, "%d-%b-%Y")
            days = (e - datetime.now()).days
            if days < 0:
                problem = (f"EXPIRED {abs(days)} days ago — this is a stale "
                           f"contract, not the near month")
            elif days > 75:
                problem = f"expires in {days} days — not the near month"
        except ValueError:
            problem = "could not read the expiry date"
        add("#4 Crude", "NSE quotes-commodity-derivatives-main",
            f"Rs{crude_nse.last:,.0f}", f"{month} contract, expiry {exp}",
            problem or "nearest expiry, as your notes require",
            ok=not problem)
    elif crude_proxy:
        add("#4 Crude", "FALLBACK WTI x USDINR",
            f"Rs{crude_proxy.inr_per_barrel:,.0f}"
            if crude_proxy.inr_per_barrel else "?",
            "proxy, no contract month",
            "NSE crude unreachable — proxy can differ by Rs100+", ok=False)

    if gift:
        add("#5 Gift Nifty", "NSE getGiftNifty", f"{gift.value:,.2f}",
            gift.updated or "?", f"expiry {gift.expiry}")
        if getattr(gift, "usd_inr", None):
            add("#18 USD/INR", "NSE getGiftNifty (USDINR fut)",
                f"{gift.usd_inr:.3f}", gift.usd_inr_updated or "?")

    if ohlc_last is not None:
        add("#6 #7 #8 #9 daily OHLC", "data/ohlc CSV",
            f"close {ohlc_last['close']:,.2f}", ohlc_last["date"],
            ohlc_last.get("note", ""), ok=ohlc_last.get("ok", True))

    if ad_n:
        add("#10 #11 breadth", "NSE marketWatchApi getIndicesData",
            f"{ad_n.get('advances')}A / {ad_n.get('declines')}D",
            ad_n.get("timestamp", "?"),
            f"market {ad_n.get('market_status', '?')}")

    if chain_ts:
        add("#13 #15 option chain", "NSE option-chain-v3", "PCR & conditions",
            chain_ts)

    if poi:
        add("#14 FII index futures", "NSE participant-wise OI file",
            f"net {poi.fii_net:+,} contracts", f"as on {poi.date}",
            "CONTRACTS, not Rs crore — see note below", ok=False)

    if glob and getattr(glob, "rows", None):
        fresh = sum(1 for r in glob.rows if not r.get("stale"))
        add("#16 Global markets", "Yahoo daily closes",
            f"{len(glob.rows)} indices", "previous session close",
            f"{fresh} fresh, {len(glob.rows) - fresh} stale",
            ok=fresh >= 4)

    if adrs and getattr(adrs, "rows", None):
        fresh = sum(1 for r in adrs.rows if not r.get("stale"))
        add("#17 US ADRs", "Yahoo daily closes",
            f"{len(adrs.rows)} ADRs", "previous US close",
            f"{fresh} fresh", ok=fresh >= 3)

    return out


# ═══════════════════════════════════════════════════════════════════════════
# Gift Nifty from sgxnifty.org — alternative source for #5
# ═══════════════════════════════════════════════════════════════════════════

SGX_URL = "https://sgxnifty.org/"


def fetch_gift_sgx(prev_close: float | None = None,
                   nifty_futures: float | None = None) -> GiftNiftyNSE:
    """
    Gift Nifty from sgxnifty.org.

    Kept alongside NSE's own getGiftNifty rather than replacing it. The site
    publishes a "Last Trade" timestamp, so whichever source carries the FRESHER
    stamp can win — useful in the early morning when one may update before the
    other.

    Regex over HTML, so inherently more fragile than the NSE endpoint. It
    raises loudly rather than returning a stale or wrong number.
    """
    import re

    r = requests.get(SGX_URL, headers={
        "User-Agent": config.USER_AGENT,
        "Accept": "text/html,application/xhtml+xml",
    }, timeout=config.REQUEST_TIMEOUT)
    if r.status_code != 200:
        raise RuntimeError(f"sgxnifty.org returned HTTP {r.status_code}")

    text = re.sub(r"<[^>]+>", " ", r.text)
    text = re.sub(r"&[a-z#0-9]+;", " ", text)
    text = re.sub(r"\s+", " ", text)

    # Anchor on the PRICE SHAPE, not on nearby words. The first attempt keyed
    # off "Last Trade" and a loose number, which matched the day and month in
    # the timestamp sitting between the label and the actual quote. A NIFTY
    # level is five digits with a thousands separator — that is distinctive
    # enough to match on directly.
    m = re.search(
        r"\b(\d{2},\d{3}(?:\.\d+)?)\s+"          # 24,227.5
        r"([+-]\d+(?:\.\d+)?)\s+"                  # +137.5
        r"([+-]?\d+(?:\.\d+)?)\s*%", text)         # +0.57%
    if not m:
        raise RuntimeError(
            "could not parse sgxnifty.org — layout changed. Fill #5 by hand "
            "and tell me; NSE's getGiftNifty is the primary source anyway.")

    value = float(m.group(1).replace(",", ""))
    if not (5_000 < value < 100_000):
        raise RuntimeError(f"implausible value parsed: {value}")

    upd = ""
    mu = re.search(r"As on\s+([A-Za-z]+,?\s*\d{1,2}\s+\w+\s+\d{4}[^,]*)", text)
    if not mu:
        mu = re.search(r"Last Trade on\s+([0-9]{1,2}\s+\w+\s+[\d:APM ]+)", text)
    if mu:
        upd = mu.group(1).strip()

    return GiftNiftyNSE(
        value=value,
        change=float(m.group(2)),
        pct_change=float(m.group(3)),
        expiry="", updated=upd or "sgxnifty.org",
        nifty_futures=nifty_futures, prev_close=prev_close)


def fetch_gift_best(client, prev_close: float | None = None,
                    nifty_futures: float | None = None) -> GiftNiftyNSE:
    """
    Try NSE first, fall back to sgxnifty.org.

    NSE is preferred because it rides the cookie session we already hold, is
    not behind bot protection, and carries an explicit contract expiry. But
    the two can disagree in the early morning depending on which updated
    last, so the fallback matters more here than elsewhere.
    """
    errors = []
    try:
        return fetch_gift_nifty_nse(client, prev_close, nifty_futures)
    except Exception as exc:                                # noqa: BLE001
        errors.append(f"NSE: {exc}")
    try:
        return fetch_gift_sgx(prev_close, nifty_futures)
    except Exception as exc:                                # noqa: BLE001
        errors.append(f"sgxnifty: {exc}")
    raise RuntimeError("Gift Nifty unavailable — " + " | ".join(errors))


# ═══════════════════════════════════════════════════════════════════════════
# India VIX from the historical report — View Building #3
# ═══════════════════════════════════════════════════════════════════════════

VIX_HISTORY_CANDIDATES = [
    ("/api/historical/vixhistory", "dated"),
    ("/api/NextApi/apiClient/reportsApi", "nextapi"),
]


@dataclass
class VixHistory:
    date: str
    close: float
    change: float
    pct_change: float
    open: float | None = None
    high: float | None = None
    low: float | None = None
    prev_close: float | None = None
    source: str = ""

    @property
    def regime(self) -> str:
        if self.close >= config.VIX_HIGH:
            return "HIGH"
        if self.close <= config.VIX_LOW:
            return "LOW"
        return "NORMAL"

    @property
    def observation(self) -> str:
        return (f"INDIA VIX {self.close:.2f}, "
                f"{'UP' if self.pct_change >= 0 else 'DOWN'} BY "
                f"{abs(self.pct_change):.2f}%")

    def score(self) -> tuple[int, str]:
        """
        Your rule: a sharp fall in VIX is bullish, a sharp rise is bearish.
        Thresholds unchanged from the sheet.
        """
        if self.pct_change < -4:
            return 2, "Bullish"
        if self.pct_change > 4:
            return -2, "Bearish"
        return 0, "Neutral"


def _parse_vix_rows(payload) -> list[dict]:
    """Flatten whatever shape the endpoint returns into plain rows."""
    if isinstance(payload, dict):
        for key in ("data", "records", "rows", "result"):
            block = payload.get(key)
            if isinstance(block, list) and block:
                return block
            if isinstance(block, dict):
                inner = block.get("data")
                if isinstance(inner, list) and inner:
                    return inner
    return payload if isinstance(payload, list) else []


def _pick(row: dict, *names):
    for n in names:
        v = row.get(n)
        if v not in (None, "", "-"):
            try:
                return float(str(v).replace(",", ""))
            except (TypeError, ValueError):
                pass
    return None


def fetch_vix_history(client, days_back: int = 10) -> VixHistory:
    """
    India VIX from NSE's historical report, taking the most recent CLOSE.

    Preferred over the live allIndices reading for View Building, because
    that runs pre-market: what you want then is the previous session's
    published close, change and % change, not a live-feed snapshot that may
    be mid-update. The two should agree, but the report is the authoritative
    one.

    Endpoint shape has moved before, so this tries the known candidates and
    reports what each returned rather than failing with one message.
    """
    end = datetime.now()
    start = end - timedelta(days=days_back)
    errors = []

    for path, style in VIX_HISTORY_CANDIDATES:
        params = ({"from": start.strftime("%d-%m-%Y"),
                   "to": end.strftime("%d-%m-%Y")} if style == "dated"
                  else {"functionName": "getHistoricalVixData",
                        "from": start.strftime("%d-%m-%Y"),
                        "to": end.strftime("%d-%m-%Y")})
        try:
            r = client._get(f"{config.BASE_URL}{path}", params)
            if r.status_code != 200:
                errors.append(f"{path} -> HTTP {r.status_code}")
                continue
            rows = _parse_vix_rows(client._json(r, f"vix {path}"))
            if not rows:
                errors.append(f"{path} -> 200 but no rows")
                continue

            def when(x):
                for k in ("EOD_TIMESTAMP", "TIMESTAMP", "timestamp",
                          "date", "CH_TIMESTAMP"):
                    v = x.get(k)
                    if v:
                        for fmt in ("%d-%b-%Y", "%Y-%m-%d", "%d-%m-%Y"):
                            try:
                                return datetime.strptime(str(v)[:11], fmt)
                            except ValueError:
                                continue
                return datetime.min

            latest = sorted(rows, key=when)[-1]
            close = _pick(latest, "EOD_CLOSE_INDEX_VAL", "CLOSE", "close",
                          "closePrice", "last")
            if close is None:
                errors.append(f"{path} -> no close field; keys={list(latest)[:8]}")
                continue

            prev = _pick(latest, "EOD_PREV_CLOSE", "PREV_CLOSE", "prevClose",
                         "previousClose")
            chg = _pick(latest, "VIX_PTS_CHG", "CHANGE", "change")
            pct = _pick(latest, "VIX_PERC_CHG", "PERC_CHANGE", "pChange",
                        "percentChange")
            if chg is None and prev is not None:
                chg = close - prev
            if pct is None and prev:
                pct = (close - prev) / prev * 100

            return VixHistory(
                date=str(latest.get("EOD_TIMESTAMP")
                         or latest.get("TIMESTAMP")
                         or latest.get("date", "")),
                close=close, change=chg or 0.0, pct_change=pct or 0.0,
                open=_pick(latest, "EOD_OPEN_INDEX_VAL", "OPEN", "open"),
                high=_pick(latest, "EOD_HIGH_INDEX_VAL", "HIGH", "high"),
                low=_pick(latest, "EOD_LOW_INDEX_VAL", "LOW", "low"),
                prev_close=prev, source=path)
        except Exception as exc:                            # noqa: BLE001
            errors.append(f"{path} -> {exc}")

    raise RuntimeError("VIX history unavailable. Tried:\n  - "
                       + "\n  - ".join(errors)
                       + "\n\nRun `python diagnose.py` — step 9 probes these.")


def fetch_vix_best(client) -> VixHistory:
    """
    Historical report first, live allIndices as fallback.

    The fallback matters: allIndices is already proven to work, so a change
    to the report endpoint degrades the reading rather than losing it.
    """
    try:
        return fetch_vix_history(client)
    except Exception as hist_err:                           # noqa: BLE001
        try:
            live = fetch_vix(client)
            return VixHistory(date="live", close=live.value,
                              change=live.change, pct_change=live.pct_change,
                              source="allIndices (fallback)")
        except Exception:                                   # noqa: BLE001
            raise hist_err


# ═══════════════════════════════════════════════════════════════════════════
# Sensibull FII/DII — fallback for #14 when NSE's archive is unavailable
# ═══════════════════════════════════════════════════════════════════════════

SENSIBULL_FII = "https://oxide.sensibull.com/v1/compute/cache/fii_dii_daily"


def fetch_participant_sensibull() -> ParticipantOI:
    """
    FII/DII participant data from Sensibull's cache endpoint.

    A FALLBACK, deliberately not the primary. NSE publishes this data itself
    as a static CSV on nsearchives, and that file is the original — Sensibull,
    niftytrader and MoneyControl all republish it. Reading the source means
    one less party between you and the number, and a static archive file will
    outlive any company's internal API.

    Worth knowing: this is an undocumented internal endpoint of a commercial
    product, not a published API. It may change without notice, and hammering
    it would be rude. It is called once per day here, only when the NSE file
    could not be read.
    """
    r = requests.get(SENSIBULL_FII, headers={
        "User-Agent": config.USER_AGENT,
        "Accept": "application/json",
        "Referer": "https://web.sensibull.com/",
    }, timeout=config.REQUEST_TIMEOUT)
    if r.status_code != 200:
        raise RuntimeError(f"sensibull returned HTTP {r.status_code}")
    try:
        payload = r.json()
    except ValueError:
        raise RuntimeError("sensibull returned non-JSON") from None

    # Shape is not documented, so search rather than assume a path
    def walk(node, depth=0):
        """
        Yield every dict that looks like a participant row, wherever it sits.

        The payload shape is undocumented and Sensibull can nest it under any
        key. The first version only checked for `participant`/`segment` and so
        missed rows using `client_type`/`instrument` — it recursed fine, it
        just did not recognise the row when it got there. Widened to every
        naming this data appears under.
        """
        if depth > 8:
            return
        if isinstance(node, dict):
            keys = {str(k).lower() for k in node}
            who_keys = {"participant", "client_type", "clienttype",
                        "participant_type", "category"}
            seg_keys = {"segment", "instrument", "instrument_type",
                        "instrumenttype"}
            val_keys = {"long", "short", "net_oi", "netoi", "net",
                        "future_index_long", "future_index_short"}
            if (who_keys & keys) and ((seg_keys & keys) or (val_keys & keys)):
                yield node
            for v in node.values():
                yield from walk(v, depth + 1)
        elif isinstance(node, list):
            for v in node:
                yield from walk(v, depth + 1)

    def num(v):
        if v in (None, "", "-"):
            return None
        try:
            return float(str(v).replace(",", "").replace("L", "")) 
        except (TypeError, ValueError):
            return None

    fii_long = fii_short = None
    fii_net = fii_chg = None
    stamp = ""

    for block in walk(payload):
        who = str(block.get("participant") or block.get("client_type")
                  or block.get("clientType") or block.get("category")
                  or block.get("participant_type") or "").upper()
        seg = str(block.get("segment") or block.get("instrument")
                  or block.get("instrument_type")
                  or block.get("instrumentType") or "").upper()
        # a row carrying future_index_* fields is index futures by definition,
        # even when the segment label is absent
        implied = ("future_index_long" in block or "future_index_short" in block)
        if "FII" not in who:
            continue
        if not implied and not ("INDEX" in seg and "FUT" in seg):
            continue
        fii_long = num(block.get("long") or block.get("future_index_long"))
        fii_short = num(block.get("short") or block.get("future_index_short"))
        fii_net = num(block.get("net_oi") or block.get("netOi")
                      or block.get("net"))
        fii_chg = num(block.get("change") or block.get("oi_change"))
        stamp = str(block.get("date") or block.get("trade_date") or "")
        break

    if fii_net is None and (fii_long is None or fii_short is None):
        raise RuntimeError(
            f"could not locate FII index-futures rows in the sensibull "
            f"payload (top-level keys: {list(payload)[:6] if isinstance(payload, dict) else 'list'}). "
            f"Run: python dump_nse.py \"{SENSIBULL_FII}\"")

    if fii_long is None or fii_short is None:
        # only the net is exposed — synthesise a book that reproduces it so
        # the downstream ratio stays honest about being unavailable
        fii_long = max(fii_net, 0.0) if fii_net else 0.0
        fii_short = abs(min(fii_net, 0.0)) if fii_net else 0.0

    prev = None
    if fii_chg is not None:
        prev = (fii_long - fii_short) - fii_chg

    return ParticipantOI(
        date=stamp or "sensibull",
        fii_idx_fut_long=int(fii_long), fii_idx_fut_short=int(fii_short),
        dii_idx_fut_long=0, dii_idx_fut_short=0,
        client_idx_fut_long=0, client_idx_fut_short=0,
        prev_fii_net=int(prev) if prev is not None else None)


def fetch_participant_best(client) -> ParticipantOI:
    """NSE archive first — the original file — Sensibull only if that fails."""
    errors = []
    try:
        return fetch_participant_oi(client)
    except Exception as exc:                                # noqa: BLE001
        errors.append(f"NSE archive: {exc}")
    try:
        return fetch_participant_sensibull()
    except Exception as exc:                                # noqa: BLE001
        errors.append(f"sensibull: {exc}")
    raise RuntimeError("participant OI unavailable — " + " | ".join(errors))


# ═══════════════════════════════════════════════════════════════════════════
# OHLC backfill — recompute the range, do not merely append today
# ═══════════════════════════════════════════════════════════════════════════

INDEX_HISTORY_URL = f"{config.BASE_URL}/api/historical/indicesHistory"


def fetch_index_history(client, start: date, end: date,
                        index: str = "NIFTY 50") -> pd.DataFrame:
    """
    Daily OHLC for a DATE RANGE from NSE's historical index report.

    The append-only approach could not repair anything: it wrote today's bar
    and nothing else, so a session missed while the machine was off stayed
    missing forever. Two such sessions — 19 and 20 Aug — sat unnoticed at the
    seam between two files and silently shifted every support below them.

    A range fetch fixes that permanently: ask for the last N days, merge, and
    any hole fills itself whether it is from today or three weeks ago.
    """
    r = client._get(INDEX_HISTORY_URL, {
        "indexType": index,
        "from": start.strftime("%d-%m-%Y"),
        "to": end.strftime("%d-%m-%Y"),
    })
    if r.status_code != 200:
        raise RuntimeError(f"indicesHistory returned HTTP {r.status_code}")
    payload = client._json(r, "indicesHistory")

    rows = payload.get("data") or payload.get("indexCloseOnlineRecords") or []
    if isinstance(rows, dict):
        rows = rows.get("indexCloseOnlineRecords") or rows.get("data") or []
    if not rows:
        raise RuntimeError(
            f"no rows for {start:%d-%b} to {end:%d-%b}; "
            f"keys={list(payload)[:6] if isinstance(payload, dict) else 'list'}")

    def num(row, *names):
        for n in names:
            v = row.get(n)
            if v not in (None, "", "-"):
                try:
                    return float(str(v).replace(",", ""))
                except (TypeError, ValueError):
                    pass
        return None

    out = []
    for row in rows:
        raw = (row.get("EOD_TIMESTAMP") or row.get("TIMESTAMP")
               or row.get("mTIMESTAMP") or row.get("date"))
        when = None
        for fmt in ("%d-%b-%Y", "%Y-%m-%d", "%d-%m-%Y", "%d %b %Y"):
            try:
                when = datetime.strptime(str(raw)[:11].strip(), fmt)
                break
            except (ValueError, TypeError):
                continue
        if when is None:
            continue
        o = num(row, "EOD_OPEN_INDEX_VAL", "OPEN", "open")
        h = num(row, "EOD_HIGH_INDEX_VAL", "HIGH", "high")
        l = num(row, "EOD_LOW_INDEX_VAL", "LOW", "low")
        c = num(row, "EOD_CLOSE_INDEX_VAL", "CLOSE", "close")
        if None in (o, h, l, c):
            continue
        out.append({"Date": when, "Open": o, "High": h, "Low": l, "Close": c})

    if not out:
        raise RuntimeError(f"parsed no usable rows; sample={str(rows[0])[:180]}")
    return pd.DataFrame(out).sort_values("Date").reset_index(drop=True)


def backfill_ohlc_dhan(days_back: int = 60) -> tuple[bool, str]:
    """
    Same merge as backfill_ohlc, but sourced from Dhan instead of NSE.

    No cutoff guard here. Dhan's historical endpoint returns settled daily
    bars and does not hand back a half-formed one for today, so the reason
    the NSE path refuses before 15:40 does not apply.
    """
    notes = []
    fresh = None
    try:
        import dhan_client as dc
    except Exception as exc:                                # noqa: BLE001
        return False, f"dhan_client unavailable: {exc}"

    # The documented daily endpoint first; the intraday aggregate when it
    # refuses, which for an index it currently always does.
    # The documented daily endpoint first, then the intraday aggregate —
    # but the SECOND is also tried when the first SUCCEEDS while missing the
    # most recent session.
    #
    # That case is the one that bit: /charts/historical returned 200 with a
    # clean frame ending yesterday, so nothing raised, the fallback never
    # ran, and the caller was told "OK — 0 sessions added". Dhan publishes
    # the daily bar after end-of-day processing, while the intraday endpoint
    # has today's candles from 15:30. A fallback that only fires on an
    # exception cannot see the difference between "no data exists" and "the
    # data exists somewhere else".
    from datetime import date as _date, timedelta as _td

    def _expected_session() -> "_date":
        """Most recent weekday whose close has passed."""
        d = datetime.now()
        if d.time() < config.MARKET_CLOSE:
            d -= _td(days=1)
        d = d.date()
        while d.weekday() >= 5:
            d -= _td(days=1)
        return d

    want = _expected_session()

    try:
        fresh = dc.daily(days_back=max(days_back, 30))
        notes.append("via /charts/historical")
    except Exception as exc:                                # noqa: BLE001
        notes.append(f"/charts/historical rejected it ({str(exc)[:60]})")

    got = (pd.to_datetime(fresh["Date"]).dt.date.max()
           if fresh is not None and not fresh.empty else None)
    if got is None or got < want:
        notes.append(f"newest there was {got or 'nothing'}, "
                     f"expected {want:%d-%b} — trying intraday")
        try:
            alt = dc.daily_from_intraday(max(days_back, 30))
            if alt is not None and not alt.empty:
                fresh = (pd.concat([fresh, alt], ignore_index=True)
                         if fresh is not None and not fresh.empty else alt)
                fresh["Date"] = pd.to_datetime(fresh["Date"])
                fresh = (fresh.sort_values("Date")
                         .drop_duplicates(subset=["Date"], keep="last")
                         .reset_index(drop=True))
                notes.append("filled from hourly intraday bars")
        except Exception as exc2:                           # noqa: BLE001
            notes.append(f"intraday route failed too: {str(exc2)[:60]}")

    if fresh is None or fresh.empty:
        return False, "both Dhan routes returned nothing — " + "; ".join(notes)
    if fresh is None or fresh.empty:
        return False, "Dhan returned no daily candles"

    path = config.OHLC_DIR / config.OHLC_FILE
    config.OHLC_DIR.mkdir(parents=True, exist_ok=True)
    existing = pd.DataFrame(columns=["Date", "Open", "High", "Low", "Close"])
    if path.exists():
        try:
            import build_master as bm
            existing = bm.load_all(config.OHLC_DIR, quiet=True)
        except Exception:                                   # noqa: BLE001
            pass

    # Refuse anything dated after the last completed session.
    #
    # A backup taken three hours before this guard existed held 1,901 clean
    # rows ending 01-Sep. The live file then reached 2,146 rows running to
    # 08-Dec-2026 — 245 rows of the future, written by this function. Every
    # source CSV was individually fine; the bad rows arrived through the
    # fetch, and nothing checked them before appending.
    #
    # Levels are built from highs and lows. A single fabricated future row
    # becomes a swing high and shifts every R and S, silently and with no
    # error anywhere. Whatever produced those dates, they must not reach the
    # file.
    from datetime import date as _date, timedelta as _td
    _cut = datetime.now()
    if _cut.time() < config.MARKET_CLOSE:
        _cut -= _td(days=1)
    _cutoff = _cut.date()
    while _cutoff.weekday() >= 5:
        _cutoff -= _td(days=1)

    _before = len(fresh)
    fresh = fresh[pd.to_datetime(fresh["Date"]).dt.date <= _cutoff]
    _dropped = _before - len(fresh)
    if _dropped:
        notes.append(f"REFUSED {_dropped} row(s) dated after {_cutoff:%d-%b} "
                     f"— a source returned future dates")
    if fresh.empty:
        return False, ("every fetched row was dated in the future — "
                       + "; ".join(notes))

    have = set(pd.to_datetime(existing["Date"]).dt.date) if len(existing) else set()
    new_days = [d for d in pd.to_datetime(fresh["Date"]).dt.date if d not in have]
    merged = (pd.concat([existing, fresh], ignore_index=True)
              if len(existing) else fresh.copy())
    merged["Date"] = pd.to_datetime(merged["Date"])
    merged = (merged.sort_values("Date")
              .drop_duplicates(subset=["Date"], keep="last")
              .reset_index(drop=True))
    merged.to_csv(path, index=False)
    newest = merged["Date"].max().date()
    return True, (f"Dhan: {len(new_days)} session(s) added, "
                  f"{len(merged)} rows total, newest {newest:%d-%b}"
                  + (" — " + "; ".join(notes) if notes else ""))


def backfill_ohlc(client, days_back: int = 30, force: bool = False,
                  index: str = "NIFTY 50") -> tuple[bool, str]:
    """
    Recompute the last N sessions rather than appending one.

    Fetches the range, merges it over whatever is in data/ohlc/OHLC.csv, and
    reports what was ADDED versus what was CORRECTED. Existing rows are
    overwritten from NSE, so a bar captured mid-session and never fixed gets
    repaired on the next run instead of persisting.

    Refuses before the cutoff for the same reason as before: writing a
    partial bar produces wrong levels silently.
    """
    now = datetime.now()
    cutoff = getattr(config, "OHLC_APPEND_AFTER", config.MARKET_CLOSE)
    if not force and now.time() < cutoff and now.weekday() < 5:
        return False, (f"before {cutoff:%H:%M} — today's bar is not settled. "
                       f"Earlier sessions can still be repaired with --force.")

    end = now.date()
    start = end - timedelta(days=days_back)
    fresh = fetch_index_history(client, start, end, index)

    path = config.OHLC_DIR / config.OHLC_FILE
    config.OHLC_DIR.mkdir(parents=True, exist_ok=True)

    existing = pd.DataFrame(columns=["Date", "Open", "High", "Low", "Close"])
    if path.exists():
        try:
            import build_master as bm
            existing = bm.load_all(config.OHLC_DIR, quiet=True)
        except Exception:                                   # noqa: BLE001
            pass

    have = {d.date(): i for i, d in enumerate(existing["Date"])} if len(existing) else {}
    added, fixed = [], []
    for _, r in fresh.iterrows():
        d = r["Date"].date()
        if d not in have:
            added.append(d)
        else:
            old = existing.iloc[have[d]]
            if any(abs(float(old[c]) - float(r[c])) > 0.01
                   for c in ("Open", "High", "Low", "Close")):
                fixed.append(d)

    merged = (pd.concat([existing, fresh], ignore_index=True)
                .sort_values("Date")
                .drop_duplicates(subset=["Date"], keep="last")
                .reset_index(drop=True))

    out = merged.copy()
    out["Date"] = out["Date"].dt.strftime("%d-%b-%Y")
    for c in ("Open", "High", "Low", "Close"):
        out[c] = out[c].map(lambda x: f"{x:.2f}")
    out.to_csv(path, index=False)

    bits = [f"{len(merged):,} sessions to {merged['Date'].max():%d-%b-%Y}"]
    if added:
        bits.append(f"ADDED {len(added)}: "
                    + ", ".join(f"{d:%d-%b}" for d in added[:6])
                    + ("…" if len(added) > 6 else ""))
    if fixed:
        bits.append(f"CORRECTED {len(fixed)}: "
                    + ", ".join(f"{d:%d-%b}" for d in fixed[:6])
                    + ("…" if len(fixed) > 6 else ""))
    if not added and not fixed:
        bits.append("nothing changed — already accurate")
    return True, " · ".join(bits)
