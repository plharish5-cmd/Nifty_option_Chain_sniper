# MULTI-AGENT GIT RULES — READ THIS BEFORE YOUR FIRST COMMIT

**Applies to every AI agent working in this repository: Claude, ChatGPT/Codex, Gemini, agy, and any other.**

Harish works with two or three agents in parallel, often on the same files, often
days apart. These rules exist because work has already been lost or corrupted in
exactly these ways. Follow them even when they slow you down.

---

## 1. NEVER COMMIT TO A SHARED BRANCH

Work only on your own branch, named `<agent>/<version>-<topic>`:

```
claude/v1.9.4b-live-switch
chatgpt/v1.9.4d
gemini/v1.9.5-pcr-study
```

**`integrate/v1.9` and `main` are read-only to you.** You may merge *into* them
only when Harish explicitly says to. You never push to them on your own
initiative, and you never rebase or force-push a branch another agent created.

---

## 2. BEFORE YOU START, LOOK AT WHAT THE OTHERS DID

Your first three commands, every session:

```bash
git fetch --all --tags
git log --oneline --graph --all -25
cat AGENT_STATUS.md
```

`AGENT_STATUS.md` is the single source of truth for who built what and where.
If another agent's branch touches the files you are about to change, **say so to
Harish before you write anything.**

---

## 3. TAG BEFORE AND AFTER

```bash
git tag pre-<agent>-<version>      # before your first edit
# ... work ...
git tag post-<agent>-<version>     # when you are done
git push origin <your-branch> --tags
```

This is how Harish can tell, weeks later, which agent produced which line. It
has already mattered more than once.

---

## 4. UPDATE `AGENT_STATUS.md` AS YOUR LAST ACT

Add one block per session. Do not rewrite other agents' blocks — append only.
This costs you thirty seconds and saves Harish an hour of archaeology.

---

## 5. MERGING — ALWAYS ASK, NEVER DISCARD

- Merge **only into `integrate/v1.9`**, never agent-branch into agent-branch.
- Use `git merge --no-ff` so each agent's work stays a visible unit in the history.
- **If a merge conflicts in a file another agent wrote, STOP AND ASK.** Do not
  resolve it by keeping your side. You do not know why their line is there.
- After merging, re-run the full verification in §8 and report the numbers
  against the previous baseline.

Recommended merge order when several agents have been working:

```bash
git checkout integrate/v1.9
git merge --no-ff origin/<agent-a>/<branch>
# verify, report
git merge --no-ff origin/<agent-b>/<branch>
# verify, report
git tag integrate-v1.9-<date>
```

---

## 6. CONFIG FILES ARE WHERE WORK GETS SILENTLY LOST

`analyst/terminal8510_config.json` holds live runtime settings: the broker IP,
strike selections, trailing preferences, arming state. Two agents committing
whole-file versions of it will overwrite each other's settings, and nobody
notices until the desk misbehaves.

**Deliver config changes as a narrow patch script, not as a whole-file commit.**
`analyst/apply_v194d_config.py` (ChatGPT) is the pattern to copy: it patches only
the specific keys it owns and leaves every other setting alone.

This has already gone wrong twice:
- A whole-file config checkout overwrote the live runtime config.
- `whitelisted_ip` was committed as `127.0.0.1`, which breaks Dhan auth.

`analyst/test_config_not_polluted_8510.py` is the tripwire. Do not disable it.

---

## 7. NEVER COMMIT RUNTIME DATA

```
data/**            analyst/replay_data/**            *.sqlite3            .env
```

The test suite **rewrites these files when it runs.** A test run has already
shrunk a recorded replay file by 286 lines and written dummy credentials into the
config. Before every commit:

```bash
git status --short      # must show no data/, no replay_data/, no .sqlite3
```

Never `git add -A`. Stage named paths only.

---

## 8. VERIFY, AND REPORT AGAINST THE PREVIOUS BASELINE

```bash
python3 scripts/verify_locks.py                     # must be 11/11
python3 -m pytest analyst/ -q --ignore=analyst/test_time_series_bridge.py
python3 -m pytest ict_smc_app/backend/tests/test_immutable_feature_lock.py -q
python3 -m analyst.verify_ict_smc_parity_8510       # 9,059 rows, 100%, 0 mismatches
git diff --stat integrate/v1.9..HEAD
```

State your pass/fail counts **next to the previous agent's counts** from
`AGENT_STATUS.md`. A number with no baseline beside it is not a result.

**Never edit a test assertion to make a suite green.** If a test is wrong, say
why it is wrong and leave it failing until Harish decides.

---

## 9. DO NOT ADD RULES, GATES OR LIMITS HARISH DID NOT ASK FOR

This one is written from a real failure. An agent added a mandatory dry-run gate,
a daily trade cap and a daily loss cap that were never requested, then Harish
spent four rounds having them removed — and three of those rounds introduced new
bugs in the removal.

If you believe a safeguard is needed, **say so in one sentence and let him
decide.** Do not implement it and do not make it the default. His risk
parameters are his.

---

## 10. WHEN YOU CHANGE A STANDING RULE, CHANGE THE DOC IN THE SAME COMMIT

`AGENTS.md`, `CLAUDE.md`, `PROTECTED_MODULES.md` and this file are read by the
next agent as fact. A stale rule here is worse than no rule. Rule 8 of
`AGENTS.md` said "Alert-only — nothing places orders" for weeks after real-money
arming was built.

---

## QUICK CHECKLIST

```
[ ] git fetch --all --tags && git log --oneline --graph --all -25
[ ] read AGENT_STATUS.md
[ ] own branch <agent>/<version>-<topic>, tagged pre-<agent>-<version>
[ ] no data/, replay_data/, *.sqlite3, .env in git status
[ ] config changed via a narrow patch script, not a whole-file commit
[ ] verify_locks 11/11, pytest + parity reported against the previous baseline
[ ] no test assertion loosened
[ ] no unrequested limit or gate added
[ ] AGENT_STATUS.md appended, tagged post-<agent>-<version>, pushed with --tags
[ ] merges into integrate/v1.9 only, only when Harish says so
```
