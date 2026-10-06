"""
apply_future_fix.py — makes every part of the app use the CURRENT NIFTY future, also across rollover.

Bugs fixed (found by checking every place that picks the NIFTY future):
  1. dhan_client.nifty_futures_security_id() looked the contract up ONCE and kept it until the program restarted.
     scheduler.py / the dashboard run for days → after the monthly expiry (last Tuesday) they kept reading the
     DEAD contract: VWAP, RVOL, futures volume, basis all wrong with no error.
     → now re-checked every trading day (IST), and rolled to next month after 15:30 IST on expiry day.
     → new helper nifty_futures_contract() also returns the symbol + expiry (used by the PCR & OI page).
     → if Dhan's file has no instrument-type column it now refuses instead of possibly picking an OPTION.
     → a failed lookup is not retried for 10 minutes (it was re-downloading the big scrip-master every call).
  2. market_data source-check said "EXPIRED 1 days ago" for the future ON its expiry day (time-of-day maths).
     → compares dates now.
  3. market_data.fetch_index_futures (NSE) kept the expiring contract after 15:30 on expiry day, so an evening /
     next-morning View Building on that night scored against a dead contract.  → rolls after 15:30.

Edits YOUR files in place, backups *.bak_futfix, compile-checked, safe to run twice.
  .venv/bin/python apply_future_fix.py --check
  .venv/bin/python apply_future_fix.py
Then restart scheduler.py and the dashboard.
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).parent

DC_OLD_HEAD = '''_futures_cache: dict[str, str] = {}


def nifty_futures_security_id(force: bool = False) -> str | None:'''
DC_OLD_TAIL = '''    except Exception as exc:                                # noqa: BLE001
        log.warning("futures lookup failed (%s) — VWAP will be unavailable", exc)
        return None'''

DC_NEW = '''_futures_cache: dict = {}          # FUTFIX: {id, symbol, expiry (date), checked (date), fail_at}


def _ist_now() -> datetime:
    return datetime.utcnow() + timedelta(hours=5, minutes=30)


def nifty_futures_contract(force: bool = False) -> dict | None:
    """
    CURRENT NIFTY futures contract from Dhan's scrip master: {"id", "symbol", "expiry"}.

    Re-checked every trading day (IST) and rolled to the next month after 15:30 IST on expiry day, so a
    scheduler / dashboard left running over the monthly expiry never keeps reading the dead contract.
    Returns None on failure (VWAP then simply unavailable).
    """
    from datetime import time as _t
    pinned = getattr(config, "DHAN_FUTURES_SECURITY_ID", None)
    if pinned:
        log.warning("config.DHAN_FUTURES_SECURITY_ID is pinned (%s) — it will NOT roll at expiry", pinned)
        return {"id": str(pinned), "symbol": "pinned in config", "expiry": None}
    now = _ist_now()
    today, after_close = now.date(), now.time() >= _t(15, 30)
    c = _futures_cache
    if not force and c.get("id") and c.get("checked") == today and c.get("expiry") \\
            and (c["expiry"] > today or (c["expiry"] == today and not after_close)):
        return {k: c[k] for k in ("id", "symbol", "expiry")}
    if not force and c.get("fail_at") and (now - c["fail_at"]).total_seconds() < 600:
        return None
    try:
        df = pd.read_csv(SCRIP_MASTER, low_memory=False)
        cols = {c_.upper().replace(" ", "_"): c_ for c_ in df.columns}

        def col(*names):
            for n in names:
                if n in cols:
                    return cols[n]
            return None

        seg = col("SEM_EXM_EXCH_ID", "EXCH_ID")
        inst = col("SEM_INSTRUMENT_NAME", "INSTRUMENT_NAME", "SEM_EXCH_INSTRUMENT_TYPE")
        under = col("SEM_TRADING_SYMBOL", "TRADING_SYMBOL")
        expiry = col("SEM_EXPIRY_DATE", "EXPIRY_DATE")
        sid = col("SEM_SMST_SECURITY_ID", "SECURITY_ID")
        if not all([seg, inst, under, expiry, sid]):
            log.warning("scrip master columns unexpected: %s", list(df.columns)[:10])
            c["fail_at"] = now
            return None

        m = df[(df[seg].astype(str).str.upper() == "NSE")
               & (df[under].astype(str).str.upper().str.startswith("NIFTY"))
               & (df[inst].astype(str).str.upper().str.contains("FUT", na=False))]
        m = m[~m[under].astype(str).str.upper().str.contains("BANK|FIN|MID|NXT|FPI|IT|PSE|CPSE|REIT", na=False)]
        m = m.assign(_exp=pd.to_datetime(m[expiry], errors="coerce")).dropna(subset=["_exp"])
        d = m["_exp"].dt.date
        m = m[(d > today) | ((d == today) & (not after_close))].sort_values("_exp")
        if m.empty:
            c["fail_at"] = now
            return None
        chosen = m.iloc[0]
        new_id = str(int(chosen[sid]))
        if c.get("id") and c["id"] != new_id:
            log.warning("NIFTY futures ROLLED: %s -> %s (%s expiry %s)", c["id"], new_id,
                        chosen[under], chosen._exp.date())
        c.update(id=new_id, symbol=str(chosen[under]), expiry=chosen._exp.date(), checked=today, fail_at=None)
        log.info("NIFTY futures: %s expiry %s -> id %s", chosen[under], chosen._exp.date(), new_id)
        return {k: c[k] for k in ("id", "symbol", "expiry")}
    except Exception as exc:                                # noqa: BLE001
        log.warning("futures lookup failed (%s) — VWAP will be unavailable", exc)
        c["fail_at"] = now
        return None


def nifty_futures_security_id(force: bool = False) -> str | None:
    """Security id of the CURRENT NIFTY future (see nifty_futures_contract)."""
    k = nifty_futures_contract(force)
    return k["id"] if k else None'''

MD = [
    ('''    live = [x for x in futs if expiry_key(x).date() >= today]''',
     '''    _after = datetime.now().time() >= datetime.strptime("15:30", "%H:%M").time()      # FUTFIX: roll after close
    live = [x for x in futs if expiry_key(x).date() > today or (expiry_key(x).date() == today and not _after)]'''),
    ('''            days = (e - datetime.now()).days''',
     '''            days = (e.date() - datetime.now().date()).days          # FUTFIX: dates, not times'''),
]


def patch(name: str, fn, check: bool) -> bool:
    p = ROOT / name
    if not p.exists():
        print(f"✘ {name} not found — run from the OC folder"); return False
    t = p.read_text(encoding="utf-8")
    if "FUTFIX" in t:
        print(f"• {name}: already fixed"); return True
    new = fn(t)
    if new is None:
        print(f"✘ {name}: differs from the version I know — NOT changed (send me the file)"); return False
    compile(new, name, "exec")
    if check:
        print(f"✔ {name}: can be fixed"); return True
    shutil.copy2(p, p.with_name(p.name + ".bak_futfix"))
    p.write_text(new, encoding="utf-8")
    print(f"✔ {name}: fixed (backup {name}.bak_futfix)")
    return True


def fix_dc(t: str):
    if t.count(DC_OLD_HEAD) != 1:
        return None
    a = t.index(DC_OLD_HEAD)
    b = t.find(DC_OLD_TAIL, a)
    if b < 0:
        return None
    return t[:a] + DC_NEW + t[b + len(DC_OLD_TAIL):]


def fix_md(t: str):
    old, new = MD[0]
    if t.count(old) != 1 or t.count(MD[1][0]) < 1:
        return None
    t = t.replace(old, new, 1)
    return t.replace(MD[1][0], MD[1][1])


def main():
    check = "--check" in sys.argv
    ok = patch("dhan_client.py", fix_dc, check)
    ok = patch("market_data.py", fix_md, check) and ok
    if ok and not check:
        print("Done — restart scheduler.py and the dashboard so they load the fix.")


if __name__ == "__main__":
    main()
