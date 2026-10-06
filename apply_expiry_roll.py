"""
apply_expiry_roll.py — weekly expiry rollover for the WHOLE app.

Problem: every module took the FIRST expiry in Dhan's / NSE's list (exps[0], dates[0]). On expiry day (Tuesday) the
list still contains today's expiry after the close, so on Tuesday evening — and until the broker drops it — the
scanner, Options Lab, briefing and Google-Sheet-style views kept showing the EXPIRED chain instead of next week's.
NIFTY's next weekly series (strikes) appear in the list automatically; the app just has to step past the dead one.

Fix (one helper, two call sites — everything else uses these):
  dhan_client.fetch_dhan_expiries()   → returns only LIVE expiries, sorted (drops past dates, and today's after 16:00 IST)
  chain_parser.parse_json(expiry=None) → "nearest" = first LIVE expiry, same rule
The roll is at 16:00 (not 15:30) so the scheduler's after-close snapshots (15:30–15:45) stay on the same expiry and
your day tables do not jump. Change ROLL_AT in dhan_client.py if you want another time.

Edits YOUR files in place (backups *.bak_expiryroll), compile-checked, safe to run twice.
  .venv/bin/python apply_expiry_roll.py --check
  .venv/bin/python apply_expiry_roll.py
Then restart scheduler.py and the dashboard.
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).parent

HELPER = '''
# EXPIRYROLL — one rule for the whole app: an expiry is LIVE until ROLL_AT IST on its own day.
ROLL_AT = "16:00"


def live_expiries(exps) -> list:
    """Drop expired dates (and today's after ROLL_AT IST), keep the original strings, sort nearest first."""
    import pandas as _pd
    from datetime import datetime as _dt, timedelta as _td
    now = _dt.utcnow() + _td(hours=5, minutes=30)
    roll = _dt.strptime(ROLL_AT, "%H:%M").time()
    keep = []
    for e in exps or []:
        d = _pd.to_datetime(str(e), errors="coerce", dayfirst=not str(e)[:4].isdigit())
        if _pd.isna(d):
            continue
        if d.date() > now.date() or (d.date() == now.date() and now.time() < roll):
            keep.append((d, e))
    keep.sort(key=lambda x: x[0])
    return [e for _, e in keep] or list(exps or [])

'''

DC_OLD = '''            return r.json().get("data", [])'''
DC_NEW = '''            return live_expiries(r.json().get("data", []))          # EXPIRYROLL'''
CP_OLD = '''        target_expiry = dates[0] if dates else None'''
CP_NEW = '''        try:                                                    # EXPIRYROLL — first LIVE expiry
            from dhan_client import live_expiries
            target_expiry = (live_expiries(dates) or [None])[0] if dates else None
        except Exception:                                       # noqa: BLE001
            target_expiry = dates[0] if dates else None'''


def patch(name, fn, check):
    p = ROOT / name
    if not p.exists():
        print(f"✘ {name} not found — run from the OC folder"); return False
    t = p.read_text(encoding="utf-8")
    if "EXPIRYROLL" in t:
        print(f"• {name}: already done"); return True
    new = fn(t)
    if new is None:
        print(f"✘ {name}: differs from the version I know — NOT changed (send me the file)"); return False
    compile(new, name, "exec")
    if check:
        print(f"✔ {name}: can be fixed"); return True
    shutil.copy2(p, p.with_name(p.name + ".bak_expiryroll"))
    p.write_text(new, encoding="utf-8")
    print(f"✔ {name}: fixed (backup {name}.bak_expiryroll)")
    return True


def fix_dc(t):
    if t.count(DC_OLD) != 1 or t.count("def fetch_dhan_expiries(") != 1:
        return None
    i = t.index("def fetch_dhan_expiries(")
    return (t[:i] + HELPER.lstrip("\n") + "\n" + t[i:]).replace(DC_OLD, DC_NEW, 1)


def fix_cp(t):
    if t.count(CP_OLD) != 1:
        return None
    return t.replace(CP_OLD, CP_NEW, 1)


def main():
    check = "--check" in sys.argv
    ok = patch("dhan_client.py", fix_dc, check)
    ok = patch("chain_parser.py", fix_cp, check) and ok
    if ok and not check:
        print("Done — restart scheduler.py and the dashboard. On expiry day after 16:00 the app shows next week's chain.")


if __name__ == "__main__":
    main()
