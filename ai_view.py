"""
ai_view.py — hand the computed snapshot to Claude and get a trader's read back.

The numbers are computed in Python on purpose. The model is not asked to do
arithmetic or to invent data; it only interprets a structured snapshot against
your own rulebook. That keeps it auditable and cheap.

Requires:  pip install anthropic   and   export ANTHROPIC_API_KEY=...
Docs:      https://docs.claude.com/en/api/overview
"""

import json
import os

import config

SYSTEM = """You are an experienced Indian index options trader reading a live \
NSE option chain for a discretionary intraday trader. You follow the Equity \
Guys / MJ framework but you are not a cheerleader for it — if the chain \
contradicts the framework's own condition mapping, say so plainly.

Rules you must respect:
- Parameter #15 (option chain) is real-time and OUTRANKS the pre-market view \
score. If they conflict, the instruction is: reduce size or wait until 9:45 AM.
- Conditions 1 and 2 (super bullish / super bearish) permit trailing to \
100-400 pts. Conditions 3 and 4 are exit-at-40 only. Condition 5 is range-bound: \
avoid extension setups.
- The chain gives CONTEXT and sizing, never the entry. The 5-min ICE/EMA setup \
is the actual trigger. Never tell the trader to enter on chain data alone.
- Near a call OI wall, avoid long extensions. Near a put OI wall, avoid short \
extensions.
- Max pain acts as a magnet, strongest on expiry day and weak early in the week.
- Do not invent numbers. Use only the snapshot given. If something is missing, \
say it is missing.

Output format — plain text, no markdown headers, under 250 words:
1. ONE LINE verdict: BULLISH / BEARISH / RANGE / CONFLICTED + confidence \
(high/medium/low).
2. WHY: the 2-3 strongest pieces of evidence, with the actual numbers.
3. AGAINST: the strongest piece of evidence pointing the other way. Always \
name one. If there truly is none, say the chain is one-sided and that itself \
is a crowding risk.
4. LEVELS: nearest support wall, nearest resistance wall, expected range.
5. SIZING: full / half / quarter / stand aside — and the one thing that would \
change your mind in the next 30 minutes.
"""

USER_TEMPLATE = """Live snapshot ({symbol}, spot {spot}, NSE time {ts}, \
expiry {expiry}):

{snapshot_json}

{premarket_block}
{history_block}
Read this chain."""


def _premarket_block(premarket_score, bands):
    if premarket_score is None:
        return "Pre-market view score: not supplied.\n"
    if premarket_score > bands["bullish_above"]:
        label = "BULLISH"
    elif premarket_score < bands["bearish_below"]:
        label = "BEARISH"
    else:
        label = "NEUTRAL"
    return (f"Pre-market 17-parameter view score: {premarket_score} -> {label} "
            f"(bands: >{bands['bullish_above']} bullish, "
            f"<{bands['bearish_below']} bearish). Apply the conflict rule if "
            f"the chain disagrees.\n")


def _history_block(rows):
    if not rows:
        return ""
    lines = ["Earlier polls today (oldest first):"]
    for r in rows[-8:]:
        lines.append(
            f"  {r.get('ts')} spot={r.get('spot')} cond={r.get('condition')} "
            f"pcr={r.get('pcr_band')} straddle={r.get('straddle')} "
            f"maxpain={r.get('max_pain')}")
    return "\n".join(lines) + "\n"


def build_prompt(snap, premarket_score=None, history=None, bands=None):
    bands = bands or config.SCORE_BANDS
    return USER_TEMPLATE.format(
        symbol=snap["symbol"],
        spot=snap["spot"],
        ts=snap["nse_timestamp"],
        expiry=snap["expiry"],
        snapshot_json=json.dumps(snap, indent=2, default=str),
        premarket_block=_premarket_block(premarket_score, bands),
        history_block=_history_block(history),
    )


def get_view(snap, premarket_score=None, history=None):
    """Return the model's text read, or an error string. Never raises."""
    if not config.AI_ENABLED:
        return "[AI disabled in config]"
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return "[ANTHROPIC_API_KEY not set — skipping AI read]"
    try:
        from anthropic import Anthropic
    except ImportError:
        return "[anthropic package not installed: pip install anthropic]"

    try:
        client = Anthropic()
        resp = client.messages.create(
            model=config.AI_MODEL,
            max_tokens=config.AI_MAX_TOKENS,
            system=SYSTEM,
            messages=[{"role": "user",
                       "content": build_prompt(snap, premarket_score, history)}],
        )
        return "".join(b.text for b in resp.content if b.type == "text").strip()
    except Exception as exc:                            # noqa: BLE001
        return f"[AI call failed: {type(exc).__name__}: {exc}]"
