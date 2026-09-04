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
from datetime import datetime, date
from pathlib import Path

import pandas as pd

import config


def _ensure() -> None:
    config.SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)


def save_snapshot(df: pd.DataFrame, spot: float, ts: datetime) -> Path:
    _ensure()
    day = config.SNAPSHOT_DIR / ts.strftime("%Y-%m-%d")
    day.mkdir(exist_ok=True)
    path = day / f"{ts.strftime('%H%M%S')}.parquet"
    out = df.copy()
    out.attrs["spot"] = spot
    out.to_parquet(path, index=False)
    (day / f"{ts.strftime('%H%M%S')}.meta.json").write_text(
        json.dumps({"spot": spot, "timestamp": ts.isoformat()})
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
            out.append((datetime.fromisoformat(info["timestamp"]),
                        float(info["spot"]), pd.read_parquet(pq)))
    return out


def append_log(row: dict, path: Path | None = None) -> None:
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
