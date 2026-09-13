"""
Snapshot persistence and session state.

The single most valuable thing this app produces is not any one screen — it
is the accumulated log. One snapshot tells you where the walls are. A series
tells you what the walls are doing, which is the part that trades. Keep the
files; in a couple of months they become the dataset you use to work out
whether any of this actually predicts anything on your data.
"""
from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime, date, timedelta
from pathlib import Path

import pandas as pd

import config


def _ensure() -> None:
    config.SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)


def save_snapshot(df: pd.DataFrame, spot: float, ts: datetime, expiry: str | None = None,
                  base_dir: Path | None = None) -> Path | None:
    # WEEKEND IMMUNITY: Never store snapshots on Saturday (5) or Sunday (6) unless explicitly enabled
    if ts.weekday() >= 5 and not getattr(config, "ALLOW_WEEKEND_STORAGE", False):
        return None
    root_dir = base_dir or config.SNAPSHOT_DIR
    root_dir.mkdir(parents=True, exist_ok=True)
    day = root_dir / ts.strftime("%Y-%m-%d")
    day.mkdir(exist_ok=True)
    path = day / f"{ts.strftime('%H%M%S')}.parquet"
    out = df.copy()
    out.attrs["spot"] = spot
    
    # Store expiry in DataFrame attrs, column, and meta.json if available
    exp_val = expiry or out.attrs.get("expiry")
    if "expiry" not in out.columns or out["expiry"].isna().all():
        if exp_val:
            out["expiry"] = str(exp_val)
    if exp_val:
        out.attrs["expiry"] = str(exp_val)
        
    out.to_parquet(path, index=False)
    meta_payload = {"spot": spot, "timestamp": ts.isoformat()}
    if exp_val:
        meta_payload["expiry"] = str(exp_val)
    (day / f"{ts.strftime('%H%M%S')}.meta.json").write_text(
        json.dumps(meta_payload)
    )
    return path


def load_day(day: date | None = None) -> list[tuple[datetime, float, pd.DataFrame]]:
    """All snapshots for a day, oldest first."""
    _ensure()
    day = day or date.today()
    folder = config.SNAPSHOT_DIR / day.strftime("%Y-%m-%d")
    if not folder.exists():
        return []
    out = []
    for meta in sorted(folder.glob("*.meta.json")):
        info = json.loads(meta.read_text())
        pq = meta.with_suffix("").with_suffix(".parquet")
        if pq.exists():
            df_snap = pd.read_parquet(pq)
            if "expiry" in info:
                df_snap.attrs["expiry"] = info["expiry"]
            elif "expiry" in df_snap.columns:
                valid_exp = df_snap["expiry"].dropna()
                if not valid_exp.empty:
                    df_snap.attrs["expiry"] = str(valid_exp.iloc[0])
            out.append((datetime.fromisoformat(info["timestamp"]),
                        float(info["spot"]), df_snap))
    return out


def load_day_multi(day: date | None = None) -> list[tuple[datetime, float, pd.DataFrame]]:
    """All multi-expiry snapshots for a day from snapshots_multi, oldest first."""
    day = day or date.today()
    folder = getattr(config, "SNAPSHOT_MULTI_DIR", config.DATA_DIR / "snapshots_multi") / day.strftime("%Y-%m-%d")
    if not folder.exists():
        return []
    out = []
    for meta in sorted(folder.glob("*.meta.json")):
        info = json.loads(meta.read_text())
        pq = meta.with_suffix("").with_suffix(".parquet")
        if pq.exists():
            df_snap = pd.read_parquet(pq)
            if "expiry" in info:
                df_snap.attrs["expiry"] = info["expiry"]
            # Multi-expiry integrity guard: hard raise on corrupt data conditions
            if "strike" in df_snap.columns and "expiry" in df_snap.columns:
                if df_snap.duplicated(subset=["strike", "expiry"]).any():
                    raise ValueError(f"Corrupt multi-expiry snapshot {pq.name}: duplicate strikes found within the same expiry")
            elif "strike" in df_snap.columns and "expiry" not in df_snap.columns:
                if df_snap["strike"].duplicated().any():
                    raise ValueError(f"Corrupt multi-expiry snapshot {pq.name}: lacks 'expiry' column and contains duplicate strikes")
            out.append((datetime.fromisoformat(info["timestamp"]),
                        float(info["spot"]), df_snap))
    return out


def prune_multi_snapshots(retention_days: int = 90, dry_run: bool = False,
                          base_dir: Path | None = None) -> list[str]:
    """
    Remove snapshot directories in SNAPSHOT_MULTI_DIR older than retention_days.
    
    SAFETY CONSTRAINTS:
    - Target directory must literally be named 'snapshots_multi'.
    - If pointed at 'snapshots' or any other folder, execution REFUSES immediately with ValueError.
    - Returns list of deleted (or candidate in dry_run) folder names.
    - Logs every deletion to data/scheduler_heartbeat.jsonl.
    """
    multi_dir = base_dir if base_dir is not None else getattr(config, "SNAPSHOT_MULTI_DIR", config.DATA_DIR / "snapshots_multi")
    resolved_name = multi_dir.resolve().name
    if resolved_name != "snapshots_multi":
        raise ValueError(
            f"SAFETY VIOLATION: prune_multi_snapshots refused execution. "
            f"Target directory name must literally be 'snapshots_multi', but got '{resolved_name}' ({multi_dir}). "
            f"Primary snapshots under 'snapshots/' can NEVER be deleted."
        )
    if not multi_dir.exists():
        return []
    cutoff_date = date.today() - timedelta(days=retention_days)
    pruned = []
    for day_folder in sorted(multi_dir.iterdir()):
        if not day_folder.is_dir():
            continue
        try:
            folder_date = datetime.strptime(day_folder.name, "%Y-%m-%d").date()
            if folder_date < cutoff_date:
                if not dry_run:
                    import shutil
                    shutil.rmtree(day_folder)
                    # Log deletion to scheduler_heartbeat.jsonl
                    hb_line = {
                        "timestamp": datetime.now().isoformat(),
                        "job": "retention_prune",
                        "outcome": "PRUNED",
                        "folder": day_folder.name,
                        "retention_days": retention_days,
                    }
                    hb_file = config.DATA_DIR / "scheduler_heartbeat.jsonl"
                    try:
                        with hb_file.open("a", encoding="utf-8") as f:
                            f.write(json.dumps(hb_line) + "\n")
                    except Exception:                       # noqa: BLE001
                        pass
                pruned.append(day_folder.name)
        except ValueError:
            continue
    return pruned


def append_log(row: dict, path: Path | None = None) -> None:
    # WEEKEND IMMUNITY: Never store signal logs on Saturday (5) or Sunday (6) no matter what!
    row_ts = row.get("timestamp") or row.get("time")
    if row_ts:
        if isinstance(row_ts, (datetime, date)) and row_ts.weekday() >= 5:
            return
        elif isinstance(row_ts, str):
            try:
                if datetime.fromisoformat(row_ts.split("+")[0]).weekday() >= 5:
                    return
            except Exception:
                pass
    path = path or config.SIGNAL_LOG
    path.parent.mkdir(parents=True, exist_ok=True)
    df = pd.DataFrame([row])
    df.to_csv(path, mode="a", header=not path.exists(), index=False)


def load_log(path: Path | None = None) -> pd.DataFrame:
    path = path or config.SIGNAL_LOG
    if not path.exists():
        return pd.DataFrame()
    return pd.read_csv(path)


class SessionState:
    """
    Carries the things that only exist across polls: the session baseline for
    measuring wall migration, the running peak OI at each strike for detecting
    a wall breaking, and the confirmation counter that stops single-cycle
    noise from being reported as a signal.
    """

    def __init__(self) -> None:
        self.baseline = None          # Levels from the first post-9:30 poll
        self.peaks: dict[str, float] = {}
        self.spot_prev: float | None = None
        self.last_regime: str | None = None
        self.regime_streak = 0
        self.rangebound_trades = 0
        self.stops_today = 0
        self.history: list[dict] = []
        # Polarity-flip tracker. Lives here because it is the only thing that
        # remembers a zone WAS support before price went through it — every
        # poll otherwise re-derives levels from scratch.
        self.flips = None

    def update_peaks(self, df) -> None:
        for _, r in df.iterrows():
            k = int(r.strike)
            for side in ("ce", "pe"):
                oi = r[f"{side}_oi"]
                if oi == oi:                       # not NaN
                    key = f"{side}_{k}"
                    self.peaks[key] = max(self.peaks.get(key, 0.0), float(oi))

    def note_regime(self, regime: str) -> bool:
        """Returns True once a regime has held for CONFIRM_SNAPSHOTS polls."""
        if regime == self.last_regime:
            self.regime_streak += 1
        else:
            self.last_regime = regime
            self.regime_streak = 1
        return self.regime_streak >= config.CONFIRM_SNAPSHOTS

    def push(self, ts: datetime, read) -> None:
        self.history.append({
            "timestamp": ts, "spot": read.levels.spot,
            "s1": read.levels.s1, "r1": read.levels.r1,
            "max_pain": read.levels.max_pain,
            "bias_score": read.bias_score, "pcr_oi": read.pcr_oi,
            "regime": read.regime,
        })

    @property
    def frame(self) -> pd.DataFrame:
        return pd.DataFrame(self.history)


def recent_snapshots(day, limit: int = 12):
    """
    Load today's stored chain snapshots, oldest first.

    Reads the parquet files save_snapshot() already writes, so the heatmap
    costs no extra fetching — the history is a by-product of the scanner
    running.
    """
    folder = config.SNAPSHOT_DIR / str(day)
    if not folder.exists():
        return []
    files = sorted(folder.glob("*.parquet"))[-limit:]
    out = []
    for f in files:
        try:
            when = datetime.strptime(f.stem, "%H%M%S").replace(
                year=day.year, month=day.month, day=day.day)
            out.append((when, pd.read_parquet(f)))
        except Exception:                                   # noqa: BLE001
            continue
    return out


def load_snapshots(day: date | None = None, limit: int = 40
                   ) -> list[tuple[datetime, "pd.DataFrame"]]:
    """
    Today's stored chain snapshots, oldest first.

    State duration and the OI heatmap both need history rather than a single
    poll, and this is where it comes from. Capped so a long session does not
    load a hundred files on every rerun.
    """
    import pandas as pd
    day = day or date.today()
    folder = config.DATA_DIR / "snapshots" / day.isoformat()
    if not folder.exists():
        return []
    out = []
    for f in sorted(folder.glob("*.parquet"))[-limit:]:
        try:
            stamp = datetime.combine(
                day, datetime.strptime(f.stem.split(".")[0], "%H%M%S").time())
            out.append((stamp, pd.read_parquet(f)))
        except Exception:                                   # noqa: BLE001
            continue
    return out


def spot_only(day: date | None = None) -> list[tuple[datetime, float]]:
    """
    Timestamp and spot for a session, without touching the parquet files.

    load_day() reads the full option chain for every snapshot, which is the
    right thing when the chain is wanted and badly wrong when it is not:
    scoring a month of outcomes needs one float per snapshot and would
    otherwise deserialise several thousand chains to get them. The spot is
    already in the sidecar JSON, so read that.
    """
    _ensure()
    day = day or date.today()
    folder = config.SNAPSHOT_DIR / day.strftime("%Y-%m-%d")
    if not folder.exists():
        return []
    out = []
    for meta in sorted(folder.glob("*.meta.json")):
        try:
            info = json.loads(meta.read_text())
            out.append((datetime.fromisoformat(info["timestamp"]),
                        float(info["spot"])))
        except Exception:                                   # noqa: BLE001
            continue
    return sorted(out)
