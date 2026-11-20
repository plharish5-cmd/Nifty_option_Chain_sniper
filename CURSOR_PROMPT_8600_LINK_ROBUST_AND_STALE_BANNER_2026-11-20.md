# Cursor Work Order — v2.0.45: 8600 link auto-recovery + loud stale banner (owner can't tell 8600 froze)

> ROOT CAUSE (diagnosed from code 20 Nov): the 8600 app stays connected/answering but its OWN market
> feed freezes (Dhan token/websocket), so snapshots stop (~11 min old) and all 5 OI filters read "stale".
> desk8600_link_8510.py already classifies this as STALE with fix text "restart the 8600 app / check its
> feed/token" — but the owner only sees five quiet "— stale" rows and feels the app is broken. This job:
> (1) auto-recover the 8510->8600 link (re-poll/reconnect), (2) show ONE loud actionable banner the moment
> 8600 goes stale, (3) log staleness so it's diagnosable. It does NOT (cannot) fix the 8600 app's own feed
> — that is the owner's restart. Branch gemini/v2.0.45-8600-robust off the latest consolidated tip. Tags
> pre-/post-. Push. Do NOT merge. No protected modules. verify_locks 11/11. DISPLAY + link-resilience
> only — OI stays LOG mode / fail-open; this NEVER makes 8600 gate a trade.

```
WHAT 8510 CANNOT FIX (state clearly in the UI, don't pretend): if the 8600 app's own Dhan feed/token has
stalled, only restarting the 8600 app (sudo systemctl restart paperdesk-ict.service) + refreshing its
token makes data fresh again. 8510's job is to detect it, recover its own link, and TELL the owner loudly.

PART A — AUTO-RECOVER THE 8510->8600 LINK
A1. When the link state is NOT_ANSWERING or the poll errors, auto-retry on a backoff (e.g. 5s,10s,20s,
    cap 30s) instead of sitting dead until the next manual action. On recovery, log "8600 LINK RECOVERED".
A2. Distinguish clearly (already partly in desk8600_link_8510.py) and keep the states honest:
      LIVE | STALE (connected, data old) | NOT_ANSWERING | NO_SESSION | OFF.
    STALE means 8600's feed froze — an 8510 re-poll will NOT refresh it; do not spin pointlessly, just
    surface it. NOT_ANSWERING means the 8600 process/endpoint is down — retry the connection.
A3. Never block or change trading on any of these states (OI stays fail-open/log). Link resilience only.

PART B — ONE LOUD, ACTIONABLE STALE BANNER (replace five quiet "— stale" rows as the only signal)
B1. When 8600 is STALE or all OI reads are stale > staleness_alert_min (default 3), show a single top-of-
    card banner on the Prototype AND Reversal cards:
      "⚠ 8600 data is STALE (last snapshot 11 min old). OI filters are being IGNORED (fail-open) — trades
       still run. To restore: restart the 8600 app (sudo systemctl restart paperdesk-ict.service) and
       check its Dhan token."
    Include the exact age and the exact fix command. Make it dismissible but auto-reappear if still stale.
B2. Keep the per-filter rows, but the banner is the headline so the owner is never left guessing whether
    the app is broken vs the upstream feed is just old.
B3. Push the same stale alert to the Telegram/alert hook if configured (one alert per stale episode,
    deduped), so the owner knows to restart 8600 even when not watching the screen.

PART C — LOG IT (so a stale episode is diagnosable later)
C1. Write stale onset/offset to data/terminal8510/desk8600_staleness.log:
    "STALE START 11:31 (last bar 11:20)" ... "STALE END 11:46 (recovered, bar 11:46)". Include the link
    state and the snapshot age at onset. This turns "it was stale today" into a timestamped record.

GUARDRAILS
- Display + link-resilience only. OI filters stay LOG mode / fail-open — this NEVER makes 8600 gate,
  skip, or exit a trade. No change to entry/exit rules, EMA maths, or the OI gating logic. No protected
  modules. verify_locks 11/11. Config via patch (staleness_alert_min=3, link retry backoff keys).
- Do NOT attempt to fix the 8600 app's feed/token from 8510 (impossible) — detect, recover the link,
  surface the owner action. Do NOT touch ChatGPT's pricing path or the v2.0.44 trail work.

TESTS (analyst/test_8600_robust_8510.py)
- NOT_ANSWERING → auto-retry fires on backoff → on mock recovery logs "8600 LINK RECOVERED".
- STALE (snapshot 11 min old) → banner text includes the age + the restart command; OI still fail-open
  (a trade is NOT blocked).
- Stale > 3 min → one deduped Telegram alert; stale onset/offset written to the staleness log.
- STALE never changes a trade decision (gate stays fail-open).
- verify_locks 11/11; full pytest diffed vs baseline — no NEW failures.

DELIVERABLE
Commit on gemini/v2.0.45-8600-robust, tags, push, AGENT_STATUS. Paste back the banner text and the
staleness-log format. State plainly: 8510 now auto-recovers its link and shows ONE loud banner with the
exact restart command when 8600 freezes — but the owner must still restart the 8600 app to refresh its
own feed. Do not merge.

CONFIRM before coding: auto-retry the 8600 link on NOT_ANSWERING with backoff + "LINK RECOVERED" log;
one loud dismissible banner on both cards when 8600 is STALE, naming the age and the exact restart
command; Telegram alert deduped; staleness onset/offset logged; OI stays fail-open (never gates); do not
touch pricing or trail work; no rule/EMA/gating changes.
```

---

## For you, Harish — plainly
- **8600 stale = the 8600 app's own feed froze, not an 8510 bug.** 8510 is correctly saying "this data is
  old." Nothing I change in 8510 makes a frozen upstream fresh.
- **The real fix is 2 minutes, yours:** `sudo systemctl restart paperdesk-ict.service`, then check the
  8600 app's OC Lab is filling and its Dhan token is valid (if 8600's token expired, its feed dies —
  same 24-hr token story as your 8510/Dhan).
- **What this prompt fixes** is the *experience*: instead of five quiet "— stale" rows that make you feel
  cheated, you get ONE loud banner — "8600 data is STALE, OI is being ignored, trades still run, restart
  the 8600 app" — plus a Telegram ping so you know to restart it. And 8510 auto-reconnects its own link.
- **Most important reassurance:** the stale OI is **not costing you trades.** It's log-only and fail-open
  — a stale filter is simply ignored, so it cannot block or exit anything. Your today's exits and
  rejections had nothing to do with it.
