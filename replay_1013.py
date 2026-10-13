"""Replay 13-Oct briefings through the patched agt_briefing (read-only, Telegram disabled)."""
import io, contextlib, logging
from datetime import date, time as dtime
logging.disable(logging.CRITICAL)
import scheduler; scheduler.send_telegram_alert = lambda *a, **k: None
import agt_briefing as ab
ab.send_telegram_alert = lambda *a, **k: None
d = date(2026, 10, 13); prev = None; flips = 0
for h in range(9, 16):
    for m in range(0, 60, 5):
        t = dtime(h, m)
        if t < dtime(9, 20) or t > dtime(15, 25):
            continue
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            txt = ab.build_briefing(d, t, append_obs=False)
        lines = [l.strip() for l in txt.splitlines()]
        get = lambda k: next((l[len(k):].strip() for l in lines if l.startswith(k)), "?")
        bias, reg = get("BIAS:"), get("REGIME:")
        veto = next((l for l in lines if l.startswith(("❌", "⏸", "✅", "⚠️  EXPIRY", "⚠️  No clear", "⚠️  EOD"))), "")
        if prev is not None and bias != prev:
            flips += 1
        prev = bias
        print(f"{t:%H:%M}  {bias[:34]:34}  {reg[:46]:46}  {veto[:60]}")
print("\nbias changes:", flips)
