"""
apply_obs_fixes.py — three fixes to the Daily Observation Log, applied IN PLACE to YOUR current agt_briefing.py and scheduler.py
(so any newer changes you made to those files are kept). Each file is backed up first as <name>.bak_obsfix.

  1. No READY LONG / READY SHORT while EXPIRY UNWIND is active (expiry day after the unwind time, default 14:00).
     The 5-min / 15-min snapshot entries now say "EXPIRY UNWIND ⚠️ — STAND ASIDE", same as the main briefing.
  2. Change-only logging: an entry is skipped when its label AND action are the same as the previous entry of the same kind
     (briefing / 5m / 15m) less than `observations_heartbeat_minutes` ago (default 15) — so a quiet market writes one line
     every 15 minutes instead of every minute. Set it to 0 in strategy1_rules.json to log everything again.
  3. MAJOR_FLOOR_TEST fires once per test: after it fires for a level it stays quiet until spot has moved more than
     `major_floor_rearm_pts` (default 25) beyond the proximity band, or `major_floor_rearm_minutes` (default 60) have passed.
     (Same for the Telegram alert, which uses the same check.)

  .venv/bin/python apply_obs_fixes.py --check     # only report whether each fix can be applied
  .venv/bin/python apply_obs_fixes.py             # apply (safe to run twice — already-applied fixes are skipped)
Restart scheduler.py afterwards. Undo: copy the .bak_obsfix files back.
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).parent
MARK = "# OBSFIX"

HELPERS = '''
# OBSFIX helpers ─────────────────────────────────────────────────────────────
def _obs_is_repeat(content: str, entry_text: str, tf, latest_ts, rules: dict) -> bool:
    """OBSFIX-2: True when this entry has the same label and action as the previous entry of the same kind
    (briefing / 5m / 15m), written less than observations_heartbeat_minutes ago."""
    try:
        hb = int(rules.get("observations_heartbeat_minutes", 15))
        if hb <= 0:
            return False
        want = f"tf: {tf}" if tf is not None else None
        last = None
        for blk in re.findall(r"^##\\s+\\d\\d:\\d\\d\\b.*?(?=(?:\\n##\\s|\\Z))", content, flags=re.MULTILINE | re.DOTALL):
            if "- Checklist:" not in blk and "- GEX:" not in blk:
                continue                                   # alert-only chunks are not entries of this kind
            if (want and want in blk) or (want is None and "<!-- tf:" not in blk):
                last = blk
        if not last:
            return False
        hdr = lambda s: re.match(r"##\\s+(\\d\\d):(\\d\\d)\\s*\\|[^|]*\\|\\s*(.*)", s)
        act = lambda s: (re.search(r"^- Action:\\s*(.*)$", s, flags=re.MULTILINE) or [None, ""])[1].strip()
        h_old, h_new = hdr(last), hdr(entry_text.lstrip())
        if not h_old or not h_new:
            return False
        if h_old.group(3).strip() != h_new.group(3).strip() or act(last) != act(entry_text):
            return False
        gap = (latest_ts.hour * 60 + latest_ts.minute) - (int(h_old.group(1)) * 60 + int(h_old.group(2)))
        return 0 <= gap < hb
    except Exception:
        return False


def _expiry_unwind_now(target_date, latest_ts, latest_df, rules: dict) -> bool:
    """OBSFIX-1: expiry day and at/after the unwind time (rules expiry_unwind_time, default 14:00)."""
    try:
        exp = None
        if latest_df is not None and "expiry" in getattr(latest_df, "columns", []):
            vals = latest_df["expiry"].dropna()
            exp = str(vals.iloc[0]) if len(vals) else None
        if not exp:
            folder = config.SNAPSHOT_DIR / target_date.strftime("%Y-%m-%d")
            metas = sorted(folder.glob("*.meta.json"))
            if metas:
                exp = json.loads(metas[-1].read_text()).get("expiry")
        exp_d = parse_expiry_date(exp)
        if exp_d is None or exp_d != target_date:
            return False
        cut = str(rules.get("expiry_unwind_time", "14:00"))
        return latest_ts.time() >= datetime.strptime(cut, "%H:%M").time()
    except Exception:
        return False


'''


def patch_agt(text: str):
    done, notes = [], []
    if "OBSFIX helpers" not in text:
        anchor = "\ndef build_periodic_snapshot("
        if anchor not in text:
            return None, ["agt_briefing.py: cannot find 'def build_periodic_snapshot(' — not patched"]
        text = text.replace(anchor, "\n" + HELPERS.strip("\n") + "\n\n" + anchor.lstrip("\n"), 1)
        done.append("helpers")
    # fix 2 — change-only write
    old2 = '''    with open(file_path, "a", encoding="utf-8") as f:
        f.write(entry_text + "\\n\\n")

    # Milestone 2: EOD Summary separator (15:25)'''
    new2 = '''    if not _obs_is_repeat(content, entry_text, tf, latest_ts, rules):   # OBSFIX-2 change-only logging
        with open(file_path, "a", encoding="utf-8") as f:
            f.write(entry_text + "\\n\\n")

    # Milestone 2: EOD Summary separator (15:25)'''
    if "if not _obs_is_repeat(content, entry_text" not in text:
        if old2 in text:
            text = text.replace(old2, new2, 1)
            done.append("fix 2 (change-only logging)")
        else:
            notes.append("agt_briefing.py: write block of append_observation_log not found — fix 2 skipped")
    # fix 1 — no READY during expiry unwind (periodic snapshots)
    old1 = '''    # 10. Action Line (STAND ASIDE / WATCH FOR PULLBACK / READY SHORT / READY LONG)
    if is_pre_market or is_data_gap or is_freeze or is_eod or is_stale_feed:
        act_str = "STAND ASIDE"'''
    new1 = '''    # 10. Action Line (STAND ASIDE / WATCH FOR PULLBACK / READY SHORT / READY LONG)
    _exp_unwind = _expiry_unwind_now(target_date, latest_ts, latest_df, rules)   # OBSFIX-1
    if _exp_unwind:
        bias_word = "EXPIRY UNWIND ⚠️"
    if is_pre_market or is_data_gap or is_freeze or is_eod or is_stale_feed or _exp_unwind:
        act_str = "STAND ASIDE"'''
    if "_exp_unwind = _expiry_unwind_now(" not in text:
        if old1 in text:
            text = text.replace(old1, new1, 1)
            done.append("fix 1 (no READY during expiry unwind)")
        else:
            notes.append("agt_briefing.py: periodic-snapshot action block not found — fix 1 skipped")
    return text, done + notes


def patch_sched(text: str):
    if "major_floor_test_armed_" in text:
        return text, []
    a = text.find("def check_major_floor_test_alert(")
    if a < 0:
        return None, ["scheduler.py: check_major_floor_test_alert not found — fix 3 skipped"]
    b = text.find("\ndef ", a + 10)
    body = text[a:b if b > 0 else len(text)]
    old_a = '''        pe_near = df[(df["strike"] >= spot - prox) & (df["strike"] <= spot)]'''
    new_a = '''        rearm_pts = float(fc_cfg.get("major_floor_rearm_pts", 25.0))          # OBSFIX-3 once per test
        rearm_sec = int(fc_cfg.get("major_floor_rearm_minutes", 60)) * 60
        for _k in [k for k in list(st.keys()) if str(k).startswith("major_floor_test_armed_")]:
            try:
                if abs(spot - float(str(_k).rsplit("_", 1)[1])) > prox + rearm_pts:
                    st[_k] = True                                         # price left the level → next touch is a new test
            except Exception:
                pass
''' + old_a
    old_b = '''            cd_key = f"major_floor_test_{level_int}"
            if (now.timestamp() - st.get(cd_key, 0)) < cooldown_sec:
                continue
            st[cd_key] = now.timestamp()'''
    new_b = '''            cd_key = f"major_floor_test_{level_int}"
            arm_key = f"major_floor_test_armed_{level_int}"
            if (now.timestamp() - st.get(cd_key, 0)) < cooldown_sec:
                continue
            if not st.get(arm_key, True) and (now.timestamp() - st.get(cd_key, 0)) < rearm_sec:
                continue                                                  # same test still running — stay quiet
            st[cd_key] = now.timestamp()
            st[arm_key] = False'''
    if old_a not in body or old_b not in body:
        return None, ["scheduler.py: the MAJOR_FLOOR_TEST code differs from the version I know — fix 3 skipped"]
    body2 = body.replace(old_a, new_a, 1).replace(old_b, new_b, 1)
    return text[:a] + body2 + text[a + len(body):], ["fix 3 (MAJOR_FLOOR_TEST once per test)"]


def main():
    check = "--check" in sys.argv
    jobs = [("agt_briefing.py", patch_agt), ("scheduler.py", patch_sched)]
    for name, fn in jobs:
        p = ROOT / name
        if not p.exists():
            print(f"✘ {name} not found next to this script — run it from the OC folder")
            continue
        old = p.read_text(encoding="utf-8")
        new, msgs = fn(old)
        if new is None or new == old:
            print(f"• {name}: " + ("; ".join(msgs) if msgs else "already patched — nothing to do"))
            continue
        try:
            compile(new, name, "exec")
        except SyntaxError as e:
            print(f"✘ {name}: patched text would not compile ({e}) — NOT written")
            continue
        if check:
            print(f"✔ {name}: can apply — " + "; ".join(msgs))
            continue
        shutil.copy2(p, p.with_name(p.name + ".bak_obsfix"))
        p.write_text(new, encoding="utf-8")
        print(f"✔ {name}: applied — " + "; ".join(msgs) + f"  (backup: {p.name}.bak_obsfix)")
    if not check:
        print("Restart scheduler.py so the running process picks up the change.")


if __name__ == "__main__":
    main()
