"""
alerts.py — multi-timeframe alignment, and telling you about it once.

TWO PROBLEMS, AND WHY THE SECOND IS HARDER
──────────────────────────────────────────
The first problem is easy: the app computes 1m, 5m and 15m separately and
never says whether they agree. A 5-minute trend flip while the 15-minute is
still pointing the other way is a pullback. The same flip with the 15-minute
already turned is a trend change. Same candle, opposite meaning, and nothing
on screen distinguished them. `alignment()` fixes that.

The second problem is alerting, and it is where this module could easily do
more harm than the whole app does good.

An alert on every state change would have fired dozens of times on a normal
session — the Timeline log has shown Regime alone turning over thirty-three
times in a day. Thirty-three alarms is not information, it is a slot machine,
and the trader on the other end of it takes thirty-three looks at the screen
and eventually places a trade out of sheer accumulated tension. On ₹20,000
with a 13%-per-stop position, that is how an account dies in a fortnight.

So every gate below exists to make this thing quiet:

  Only ALIGNED changes fire. The 5m and 15m must agree. A 5-minute wiggle
  is not an event.

  A cooldown per event type. Once an alignment alert has fired, the same
  type stays silent for the cooldown regardless of how many times the state
  flickers back and forth.

  A hard daily ceiling. When it is hit, alerting stops for the session. If
  the market produced more than that many genuine aligned trend changes,
  the honest reading is that it is chopping and you should not be trading
  it at all — which is exactly when a beginner trades most.

  Nothing fires in the buyer-freeze window, because an alert you are not
  allowed to act on is pure temptation.

WHAT AN ALERT MEANS
───────────────────
It means: look at the chart. It does not mean: enter. There is no version of
this that tells you to place a trade, and the sound is deliberately a plain
two-tone beep rather than anything urgent.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

import config

# How long each event type stays quiet after firing.
COOLDOWN = {
    "alignment": timedelta(minutes=getattr(config, "ALERT_COOLDOWN_MINS", 20)),
    # Strategy 1 speaks once per closed 15-minute candle and no faster, so
    # its cooldown is one candle. Anything shorter would re-announce the
    # same candle on every 3-minute poll; anything longer would swallow a
    # trigger that legitimately follows an arm on the very next candle.
    "s1_arm": timedelta(minutes=14),
    "s1_trigger": timedelta(minutes=14),
    "s1_exit": timedelta(minutes=14),
    "breakout": timedelta(minutes=15),
    "level": timedelta(minutes=15),
    "iceberg": timedelta(minutes=10),
    "smc_sniper": timedelta(minutes=15),
    "smc_bridge": timedelta(minutes=15),
}

MAX_PER_DAY = getattr(config, "ALERT_MAX_PER_DAY", 8)


# ═══════════════════════════════════════════════════════════════════════════
# Alignment
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class Alignment:
    tf: dict[str, str]                 # {"1m": "UP", "5m": "UP", "15m": "DOWN"}
    state: str                         # ALIGNED UP / ALIGNED DOWN / ...
    strength: str                      # STRONG / MODERATE / WEAK / CONFLICT
    note: str = ""

    @property
    def directional(self) -> bool:
        return self.state.startswith("ALIGNED")

    @property
    def direction(self) -> str | None:
        if not self.directional:
            return None
        return "UP" if self.state.endswith("UP") else "DOWN"


def alignment(reads: dict) -> Alignment:
    """
    Do the timeframes agree, and how much does that agreement weigh?

    The 15-minute gets the deciding vote and the 1-minute gets almost none.
    That ordering is not arbitrary: the 1-minute flips several times an hour
    on ordinary noise, so letting it vote would turn every pullback into a
    trend change. It is reported because it is useful for timing an entry
    you have already decided on, and it is excluded from the verdict.
    """
    tf = {}
    for label in ("1m", "5m", "15m"):
        r = reads.get(label)
        tf[label] = getattr(r, "trend", "NO DATA") if r else "NO DATA"

    five, fifteen = tf.get("5m"), tf.get("15m")
    if "NO DATA" in (five, fifteen):
        return Alignment(tf, "NO DATA", "WEAK",
                         "not enough candles on 5m or 15m yet")

    if five == fifteen and five in ("UP", "DOWN"):
        agree_1m = tf.get("1m") == five
        return Alignment(
            tf, f"ALIGNED {five}", "STRONG" if agree_1m else "MODERATE",
            (f"5m and 15m both {five.lower()}"
             + (", 1m agreeing — all three pointing the same way"
                if agree_1m else
                f", 1m is {str(tf.get('1m')).lower()} — a pullback inside "
                f"the larger trend, which is where entries usually come from")))

    if five in ("UP", "DOWN") and fifteen in ("UP", "DOWN"):
        return Alignment(
            tf, "CONFLICT", "CONFLICT",
            (f"5m is {five.lower()} against a {fifteen.lower()} 15m. One of "
             f"them is wrong and there is no way to know which yet. This is "
             f"the state that produces the most losing trades, because the "
             f"5m chart looks clean on its own"))

    flat = "5m" if five == "FLAT" else "15m"
    return Alignment(tf, "UNCLEAR", "WEAK",
                     f"{flat} has no trend — nothing to align with")


# ═══════════════════════════════════════════════════════════════════════════
# The gate
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class Alert:
    kind: str
    title: str
    detail: str
    at: datetime
    urgent: bool = False

    @property
    def key(self) -> str:
        return f"{self.at.isoformat(timespec='minutes')}|{self.kind}|{self.title}"


@dataclass
class Gate:
    """Decides what is worth interrupting you for. Mostly: nothing."""
    day: date | None = None
    fired: int = 0
    last: dict[str, datetime] = field(default_factory=dict)
    log: list[Alert] = field(default_factory=list)

    def _roll(self, now: datetime) -> None:
        if self.day != now.date():
            self.day, self.fired, self.last, self.log = now.date(), 0, {}, []

    def in_freeze(self, now: datetime) -> bool:
        def _p(v):
            if isinstance(v, time):
                return v
            h, m = str(v).split(":")
            return time(int(h), int(m))
        try:
            a = _p(getattr(config, "BUYER_FREEZE_FROM", "11:30"))
            b = _p(getattr(config, "BUYER_FREEZE_UNTIL", "13:15"))
        except Exception:                                   # noqa: BLE001
            return False
        return a <= now.time() <= b

    def offer(self, alert: Alert) -> bool:
        """Let an alert through, or swallow it. Swallowing is the default."""
        self._roll(alert.at)
        if self.fired >= MAX_PER_DAY:
            return False
        if self.in_freeze(alert.at) and not alert.urgent:
            return False
        prev = self.last.get(alert.kind)
        cool = COOLDOWN.get(alert.kind, timedelta(minutes=15))
        if prev is not None and alert.at - prev < cool:
            return False
        self.last[alert.kind] = alert.at
        self.fired += 1
        self.log.append(alert)
        return True

    @property
    def budget(self) -> str:
        left = max(MAX_PER_DAY - self.fired, 0)
        if left == 0:
            return (f"Alert limit reached ({MAX_PER_DAY}). Silent for the rest "
                    f"of the session. A day that generates this many aligned "
                    f"trend changes is a day that is chopping, and the right "
                    f"response is to stop trading it rather than to raise the "
                    f"limit.")
        return f"{left} of {MAX_PER_DAY} alerts left today"


def from_strategy1(res, now: datetime) -> list[Alert]:
    """
    Turn Strategy 1's own alerts into gate candidates.

    Only ARM, TRIGGER and EXIT come through. PATTERN and GATE stay inside
    the tab: a pattern is context for a decision you are already making, and
    interrupting you for one trains you to look at the screen when nothing
    has to be done.

    Everything here is keyed to a CLOSED candle. Strategy 1 refuses to speak
    about a forming one — a candle that has crossed the 8 EMA can cross back
    before it closes, and an alert on that is an alert on something that
    never happened. That refusal is the reason these are worth hearing.
    """
    out = []
    for a in getattr(res, "alerts", []) or []:
        kind = getattr(a, "kind", "")
        if kind not in ("ARM", "TRIGGER", "EXIT"):
            continue
        out.append(Alert(
            f"s1_{kind.lower()}",
            {"ARM": "Strategy 1 — setup armed",
             "TRIGGER": "Strategy 1 — triggered",
             "EXIT": "Strategy 1 — exit"}[kind],
            f"{getattr(a, 'text', '')} {getattr(a, 'detail', '')}".strip(),
            now, urgent=(kind == "TRIGGER")))
    return out


def check(gate: Gate, now: datetime, align: Alignment,
          prev_align: str | None, breakout=None, icebergs=None,
          s1_result=None, smc_sigs: list[dict] | None = None) -> list[Alert]:
    """
    Build the candidate alerts for this poll and see which survive the gate.

    Only a CHANGE in alignment is a candidate. A market that has been aligned
    up for two hours is not news, and re-announcing it every poll is how an
    alerting system trains you to ignore it.
    """
    out = []

    if align.directional and prev_align != align.state:
        out.append(Alert(
            "alignment",
            f"Trend aligned {align.direction}",
            f"{align.strength} — {align.note}", now))

    if breakout is not None and getattr(breakout, "state", "") == "REAL":
        out.append(Alert(
            "breakout", "Breakout confirmed REAL",
            f"at {getattr(breakout, 'level', '?')} — all six conditions met",
            now))

    if s1_result is not None:
        out.extend(from_strategy1(s1_result, now))

    if icebergs:
        big = icebergs[0]
        out.append(Alert(
            "iceberg", "Institutional size in one poll",
            f"{getattr(big, 'strike', '?')} {getattr(big, 'side', '')} — "
            f"{getattr(big, 'pillar', 'position')}", now))

    if smc_sigs:
        for s in smc_sigs:
            out.append(Alert(
                "smc_sniper",
                s.get("title", "SMC Sniper Alert"),
                s.get("detail", ""),
                now,
                urgent=s.get("urgent", True)))

    return [a for a in out if gate.offer(a)]


# ═══════════════════════════════════════════════════════════════════════════
# Sound
# ═══════════════════════════════════════════════════════════════════════════

def beep_html(key: str, urgent: bool = False) -> str:
    """
    A two-tone beep via WebAudio, keyed so it plays once per alert.

    Deliberately plain. An alert means look at the chart; it does not mean
    something is wrong, and a sound that implies emergency would push you
    toward acting before reading.
    """
    f1, f2 = (880, 1180) if urgent else (660, 880)
    return f"""
<script>
(function() {{
  var k = "beep_{key}";
  if (window.sessionStorage.getItem(k)) return;
  window.sessionStorage.setItem(k, "1");
  try {{
    var C = window.AudioContext || window.webkitAudioContext;
    var ctx = new C();
    [[{f1}, 0.0], [{f2}, 0.16]].forEach(function(p) {{
      var o = ctx.createOscillator(), g = ctx.createGain();
      o.type = "sine"; o.frequency.value = p[0];
      o.connect(g); g.connect(ctx.destination);
      var t = ctx.currentTime + p[1];
      g.gain.setValueAtTime(0.0001, t);
      g.gain.exponentialRampToValueAtTime(0.22, t + 0.02);
      g.gain.exponentialRampToValueAtTime(0.0001, t + 0.14);
      o.start(t); o.stop(t + 0.15);
    }});
  }} catch (e) {{}}
}})();
</script>"""
