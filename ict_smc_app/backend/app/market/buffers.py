"""
market/buffers.py — Zero-Copy In-Memory Ring Buffers (RAM Rolling Windows).

Uses fixed-length collections.deque to store rolling 1m candles and raw ticks entirely in RAM.
Eliminates disk/SQLite queries during live ICT/SMC mathematical evaluations and high-frequency tick lookups.
Thresholds are sourced from rules.json under "buffers".
"""
from __future__ import annotations

import bisect
import logging
from collections import deque
from typing import Any, Dict, List, Optional, Tuple

try:
    import pandas as pd
    pd.options.mode.copy_on_write = True
except Exception:
    pass

log = logging.getLogger("market.buffers")

DEFAULT_MAX_CANDLES = 25000
DEFAULT_MAX_TICKS = 10000


class CandleRingBuffer:
    """Fixed-capacity in-memory rolling buffer for 1-minute OHLCV candles with DataFrame caching and write-behind staging."""

    def __init__(self, maxlen: int = DEFAULT_MAX_CANDLES) -> None:
        self.maxlen = maxlen
        self._deque: deque[Dict[str, float]] = deque(maxlen=maxlen)
        self._version: int = 0
        self._master_df: Optional[Any] = None
        self._cached_df: Optional[Any] = None
        self._cached_df_version: int = -1
        self._dirty_candles: List[Dict[str, float]] = []

    def __len__(self) -> int:
        return len(self._deque)

    @property
    def version(self) -> int:
        return self._version

    def append(self, candle: Dict[str, float], stage_dirty: bool = False) -> None:
        """Appends a new candle or updates the existing candle if timestamp matches.
        Invalidates cached DataFrame and optionally stages for write-behind persistence."""
        self._version += 1
        if stage_dirty:
            self._dirty_candles.append(dict(candle))

        if not self._deque:
            self._deque.append(dict(candle))
            return
        last = self._deque[-1]
        c_ts = int(candle["ts"])
        l_ts = int(last["ts"])
        if c_ts == l_ts:
            # Update existing bar
            self._deque[-1] = dict(candle)
        elif c_ts > l_ts:
            self._deque.append(dict(candle))
        else:
            # Out-of-order or backfilled bar: re-sort into place if within window
            cand_dict = dict(candle)
            timestamps = [int(c["ts"]) for c in self._deque]
            idx = bisect.bisect_left(timestamps, c_ts)
            if idx < len(self._deque) and int(self._deque[idx]["ts"]) == c_ts:
                self._deque[idx] = cand_dict
            else:
                candles = list(self._deque)
                candles.insert(idx, cand_dict)
                if len(candles) > self.maxlen:
                    candles = candles[-self.maxlen:]
                self._deque = deque(candles, maxlen=self.maxlen)

    def extend(self, candles: List[Dict[str, float]]) -> None:
        """Hydrates or bulk-extends the buffer, ensuring sorted order without duplicates."""
        if not candles:
            return
        self._version += 1
        existing_map = {int(c["ts"]): dict(c) for c in self._deque}
        for c in candles:
            existing_map[int(c["ts"])] = dict(c)
        sorted_candles = [existing_map[k] for k in sorted(existing_map.keys())]
        if len(sorted_candles) > self.maxlen:
            sorted_candles = sorted_candles[-self.maxlen:]
        self._deque = deque(sorted_candles, maxlen=self.maxlen)

    def to_dataframe(self, limit: Optional[int] = None):
        """Returns cached pd.DataFrame representation, rebuilding only on buffer mutation.
        Copy-on-Write guarantees shallow zero-copy read safety without cross-engine mutation."""
        try:
            import pandas as pd
            if not getattr(pd.options.mode, "copy_on_write", False):
                pd.options.mode.copy_on_write = True
        except (ImportError, AttributeError):
            return None
        if self._master_df is None or self._cached_df_version != self._version:
            self._master_df = pd.DataFrame(list(self._deque))
            self._cached_df = self._master_df[:]
            self._cached_df_version = self._version
        elif (
            self._cached_df is None
            or list(self._cached_df.columns) != list(self._master_df.columns)
            or (len(self._cached_df) > 0 and len(self._master_df) > 0 and (
                self._cached_df.iloc[0, 1] != self._master_df.iloc[0, 1]
                or self._cached_df.iloc[-1, -1] != self._master_df.iloc[-1, -1]
            ))
        ):
            self._cached_df = self._master_df[:]

        df = self._cached_df
        if limit is not None and limit > 0 and len(df) > limit:
            return df.iloc[-limit:]
        return df

    def has_dirty(self) -> bool:
        return bool(self._dirty_candles)

    def get_and_clear_dirty(self) -> List[Dict[str, float]]:
        """Returns staged newly closed candles for write-behind persistence and clears the queue."""
        dirty = self._dirty_candles
        self._dirty_candles = []
        return dirty

    def earliest_ts(self) -> Optional[int]:
        return int(self._deque[0]["ts"]) if self._deque else None

    def latest_ts(self) -> Optional[int]:
        return int(self._deque[-1]["ts"]) if self._deque else None

    def slice(self, since_ts: int, until_ts: Optional[int] = None, limit: Optional[int] = None) -> List[Dict[str, float]]:
        """Returns candles with ts >= since_ts (and ts <= until_ts if provided). Supports limit parameter."""
        if not self._deque:
            return []
        if until_ts is None:
            out = [dict(c) for c in self._deque if c["ts"] >= since_ts]
        else:
            out = [dict(c) for c in self._deque if since_ts <= c["ts"] <= until_ts]
        if limit is not None and limit > 0 and len(out) > limit:
            return out[-limit:]
        return out

    def last_n(self, n: int) -> List[Dict[str, float]]:
        """Returns the most recent n candles in O(1) from the tail."""
        if not self._deque:
            return []
        count = min(n, len(self._deque))
        return [dict(self._deque[i]) for i in range(len(self._deque) - count, len(self._deque))]

    def today(self, open_ts: int, limit: Optional[int] = None) -> List[Dict[str, float]]:
        """Returns today's candles from session open timestamp onwards."""
        return self.slice(open_ts, limit=limit)

    def clear(self) -> None:
        self._deque.clear()


class TickRingBuffer:
    """Fixed-capacity in-memory rolling buffer for incoming market ticks."""

    def __init__(self, maxlen: int = DEFAULT_MAX_TICKS) -> None:
        self.maxlen = maxlen
        self._deque: deque[Tuple[str, float, float]] = deque(maxlen=maxlen)  # (sid, ltp, t)
        self._last_price: Dict[str, float] = {}

    def __len__(self) -> int:
        return len(self._deque)

    def append(self, sid: str, ltp: float, t: float) -> None:
        self._deque.append((sid, ltp, t))
        self._last_price[sid] = ltp

    def last_price(self, sid: str) -> Optional[float]:
        return self._last_price.get(sid)

    def last_ticks(self, sid: Optional[str] = None, n: int = 100) -> List[Tuple[str, float, float]]:
        if not self._deque:
            return []
        if sid is None:
            count = min(n, len(self._deque))
            return [self._deque[i] for i in range(len(self._deque) - count, len(self._deque))]
        matched = [x for x in self._deque if x[0] == sid]
        return matched[-n:]

    def clear(self) -> None:
        self._deque.clear()
        self._last_price.clear()
