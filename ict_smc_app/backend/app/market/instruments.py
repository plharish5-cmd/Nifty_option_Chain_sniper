"""
market/instruments.py — one identity for everything the desk can look at.

    INDEX   NIFTY 50 (IDX_I / 13)                        → structure, zones, playbook (always)
    OPTION  NIFTY <expiry> <strike> <CE|PE> (NSE_FNO)    → flow / delta / proxy, OI context, premium P&L,
                                                           and the EXPERIMENTAL option chart

Resolver: the security id comes from Dhan's own option-chain payload (every strike carries `security_id`), so an
option is only "known" once the chain for its expiry has been fetched. Nothing is guessed or hard-coded.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Dict, Optional

INDEX_KEY = "NIFTY"


@dataclass(frozen=True)
class Instrument:
    type: str                        # "INDEX" | "OPTION"
    underlying: str = "NIFTY"
    expiry: Optional[str] = None     # YYYY-MM-DD
    strike: Optional[int] = None
    right: Optional[str] = None      # "CE" | "PE"
    security_id: Optional[str] = None
    segment: str = "IDX_I"
    instrument: str = "INDEX"        # Dhan 'instrument' field for /charts/intraday

    @property
    def key(self) -> str:
        """Stable key used in SQLite (delta rows, picks, paper trades). Independent of the security id."""
        if self.type == "INDEX":
            return INDEX_KEY
        return f"{self.underlying}|{self.expiry}|{int(self.strike)}|{self.right}"

    @property
    def label(self) -> str:
        if self.type == "INDEX":
            return "NIFTY 50"
        d = datetime.strptime(self.expiry, "%Y-%m-%d").strftime("%d %b")
        return f"{self.underlying} {d} {int(self.strike)} {self.right}"

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["key"], d["label"] = self.key, self.label
        return d


def index(security_id: str = "13") -> Instrument:
    return Instrument("INDEX", security_id=str(security_id), segment="IDX_I", instrument="INDEX")


def option(expiry: str, strike: float, right: str, security_id: Optional[str] = None) -> Instrument:
    right = right.upper()
    if right not in ("CE", "PE"):
        raise ValueError("right must be CE or PE")
    datetime.strptime(expiry, "%Y-%m-%d")
    return Instrument("OPTION", "NIFTY", expiry, int(round(float(strike))), right,
                      str(security_id) if security_id not in (None, "") else None, "NSE_FNO", "OPTIDX")


def parse_key(key: str) -> Instrument:
    if key in ("", INDEX_KEY, "INDEX"):
        return index()
    parts = key.split("|")
    if len(parts) != 4:
        raise ValueError(f"bad instrument key '{key}' — expected NIFTY|YYYY-MM-DD|strike|CE/PE")
    return option(parts[1], float(parts[2]), parts[3])


def resolve(inst: Instrument, chains: Dict[str, Dict[str, Any]]) -> Instrument:
    """Fill the Dhan security id from the stored chain of that expiry. Raises LookupError if unknown."""
    if inst.type == "INDEX" or inst.security_id:
        return inst
    ch = chains.get(inst.expiry)
    if not ch:
        raise LookupError(f"no option chain loaded for expiry {inst.expiry}")
    side = inst.right.lower()
    for r in ch.get("rows", []):
        if int(round(r["strike"])) == inst.strike:
            sid = (r.get(side) or {}).get("sid")
            if sid in (None, "", 0):
                raise LookupError(f"Dhan chain has no security_id for {inst.label}")
            return option(inst.expiry, inst.strike, inst.right, str(int(float(sid))))
    raise LookupError(f"strike {inst.strike} not in the {inst.expiry} chain")


def chain_row(inst: Instrument, chains: Dict[str, Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    ch = chains.get(inst.expiry or "")
    if not ch or inst.type != "OPTION":
        return None
    for r in ch.get("rows", []):
        if int(round(r["strike"])) == inst.strike:
            return r[inst.right.lower()]
    return None
