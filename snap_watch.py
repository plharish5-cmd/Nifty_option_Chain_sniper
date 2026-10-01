"""
snap_watch.py — Command-line launcher for periodic 5m/15m snapshots to watch log.
"""
from datetime import datetime
import agt_briefing as a

now = datetime.now()
tf = a.get_snapshot_tf_for_time(now.time())
if tf:
    res = a.build_periodic_snapshot(tf, now.date(), now.time())   # append_obs=False by default
    text = (res or {}).get("telegram_text")
    if text:
        first_line = text.splitlines()[0]
        print(f"──── {first_line} ────")
        print(text)
        print()
    w_table = (res or {}).get("writer_lines_table")
    if w_table:
        print(w_table)
        print()
