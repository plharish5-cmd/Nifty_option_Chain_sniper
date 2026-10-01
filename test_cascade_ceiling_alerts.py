import sys; sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.abspath(__file__)))
from datetime import datetime, timedelta
import pandas as pd
import scheduler as S

sent, obs = [], []
S.send_telegram_alert = lambda m: sent.append(m)
S.log_floor_ceiling_observation = lambda now, spot, tag, summary, action_text="": obs.append(tag)
_orig_log = S.log
S.log = lambda *a, **k: None

def chain(ce_23450, pe_23350):
    # strikes 23250..23550; contracts; 150L ≈ 230k contracts at lot 65
    rows = []
    for k in range(23250, 23551, 50):
        ce = 200000; pe = 200000
        if k == 23450: ce = ce_23450
        if k == 23500: ce = 400000
        if k == 23350: pe = pe_23350
        rows.append({"strike": float(k), "ce_oi": float(ce), "pe_oi": float(pe)})
    return pd.DataFrame(rows)

def run(scenario):
    sent.clear(); obs.clear()
    S._SESSION_SNAPSHOTS.clear()
    st = {}
    t0 = datetime(2026, 9, 23, 10, 0)
    for i in range(25):              # 25 polls, 1 per minute
        now = t0 + timedelta(minutes=i)
        if scenario == "ceiling":
            spot = 23410 + i * 1.2                # rising toward 23,450
            df = chain(ce_23450=600000 - i * 12000, pe_23350=300000)   # calls leaving
        else:
            spot = 23362 - (i % 3)                 # hovering 12pts above 23,350
            df = chain(ce_23450=600000, pe_23350=300000 + i * 3000)    # puts adding 3k/min
        S._SESSION_SNAPSHOTS.append((now, spot, df))
        fs = S._update_wall_streak("floor", df, spot, now, st)
        cs = S._update_wall_streak("ceiling", df, spot, now, st)
        S.check_cascade_alert(fs, df, spot, now, st)
        S.check_ceiling_breakout_alert(cs, df, spot, now, st)
        S.check_put_writer_defense_alert(df, S._snapshot_minutes_ago(now, 15), spot, now, st)
    return st

st = run("ceiling")
print("CEILING scenario: streak =", st.get("ceiling_weakening_streak"), "| alerts:", obs)
for m in sent: print(m, "\n")
assert "CEILING_BREAKOUT" in obs and "FLOOR_CASCADE" not in obs

st = run("defense")
print("DEFENSE scenario: floor streak =", st.get("floor_weakening_streak"), "| alerts:", obs)
for m in sent: print(m, "\n")
assert "PUT_DEFENSE" in obs and "CEILING_BREAKOUT" not in obs and "FLOOR_CASCADE" not in obs
print("ALL CHECKS PASSED")
S.log = _orig_log
