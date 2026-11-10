"""Append-only master ledger for algo fills (live). Never UPDATE/DELETE rows."""
from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from analyst.desk_timestamp_8510 import format_desk_timestamp_ist, parse_desk_timestamp

IST = ZoneInfo("Asia/Kolkata")
log = logging.getLogger(__name__)

TABLE = "algo_execution_master"

DATA_SOURCE_IMMUTABLE = "IMMUTABLE_MASTER"
DATA_SOURCE_SYNTHESIZED = "SYNTHESIZED"


def ensure_algo_execution_master(conn: sqlite3.Connection) -> None:
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {TABLE} (
            row_id TEXT PRIMARY KEY,
            position_id TEXT UNIQUE,
            trade_date TEXT NOT NULL,
            strategy_id TEXT,
            setup_name TEXT,
            direction TEXT,
            side TEXT,
            contract_symbol TEXT,
            security_id TEXT,
            strike INTEGER,
            index_entry REAL,
            index_exit REAL,
            option_entry REAL,
            option_exit REAL,
            qty INTEGER,
            entry_time_ist TEXT NOT NULL,
            exit_time_ist TEXT NOT NULL,
            duration_text TEXT,
            gross_pnl_inr REAL,
            dhan_charges_inr REAL,
            net_pnl_inr REAL,
            outcome TEXT,
            mode TEXT,
            source TEXT,
            provenance TEXT NOT NULL,
            created_at_ist TEXT NOT NULL,
            immutable INTEGER NOT NULL DEFAULT 1
        )
        """
    )


def trade_day_from_row(row: Dict[str, Any]) -> str:
    for key in ("trade_date", "date_only", "Date"):
        val = row.get(key)
        if val:
            return str(val)[:10]
    ent = row.get("entry_time_ist") or row.get("entry_ist") or row.get("entry_time")
    if ent:
        return str(ent)[:10]
    return ""


def normalize_synthetic_backtest_row(row: Dict[str, Any]) -> Dict[str, Any]:
    """Spot-only synthetic row: no modeled option premiums or INR P&L."""
    out = dict(row)
    st_pts = float(out.get("st_pnl_pts") or 0.0)
    out["data_source_tag"] = DATA_SOURCE_SYNTHESIZED
    out["Data_Type"] = DATA_SOURCE_SYNTHESIZED
    out["price_type"] = DATA_SOURCE_SYNTHESIZED
    out["source"] = "SYNTHESIZED"
    out["option_entry"] = None
    out["option_exit"] = None
    out["Option_Entry_Display"] = "—"
    out["Option_Exit_Display"] = "—"
    out["gross_pnl"] = None
    out["gross_pnl_inr"] = None
    out["charges"] = None
    out["dhan_charges"] = None
    out["net_pnl"] = None
    out["Net_Pnl_Display"] = f"{st_pts:+.1f} pts (Nifty spot — no option model)"
    out["Contract_Symbol"] = str(out.get("Contract_Symbol") or "").replace(
        "(Synthesized Strike)", "(Spot rule — no option)"
    )
    return out


def tag_immutable_master_row(row: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(row)
    out["data_source_tag"] = DATA_SOURCE_IMMUTABLE
    out["Data_Type"] = f"{DATA_SOURCE_IMMUTABLE} ({out.get('mode') or 'PAPER'})"
    out["price_type"] = "REAL_ALGO_MASTER"
    return out


def merge_master_and_synthetic(
    master_rows: List[Dict[str, Any]],
    synthetic_rows: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Master wins per calendar day: no synthetic rows on days that have master fills."""
    master_days = {trade_day_from_row(r) for r in master_rows if trade_day_from_row(r)}
    synth = [
        normalize_synthetic_backtest_row(r)
        for r in synthetic_rows
        if trade_day_from_row(r) not in master_days
    ]
    tagged_master = [tag_immutable_master_row(r) for r in master_rows]
    merged = tagged_master + synth
    merged.sort(key=lambda r: (trade_day_from_row(r), str(r.get("entry_hm") or r.get("Date") or "")))
    return merged


def backfill_algo_execution_master_from_positions(
    ledger_db: Path,
    *,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """One-time-safe migration: closed positions → append-only master."""
    stats = {"scanned": 0, "inserted": 0, "skipped_duplicate": 0, "skipped_invalid": 0}
    if not ledger_db.is_file():
        return {**stats, "ok": False, "error": "ledger missing"}
    positions: List[Dict[str, Any]] = []
    try:
        with sqlite3.connect(f"file:{ledger_db}?mode=ro", uri=True, timeout=10) as conn:
            conn.row_factory = sqlite3.Row
            positions = [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM positions WHERE status='CLOSED' ORDER BY exit_time ASC"
                )
            ]
    except sqlite3.Error as exc:
        log.warning("backfill master: read positions failed: %s", exc)
        return {**stats, "ok": False, "error": str(exc)}
    stats["scanned"] = len(positions)
    for p in positions:
        ep = float(p.get("entry_price") or 0.0)
        xp = float(p.get("exit_price") or 0.0)
        qty = int(p.get("qty") or 0)
        if ep <= 0 or xp < 0 or qty <= 0 or not p.get("entry_time") or not p.get("exit_time"):
            stats["skipped_invalid"] += 1
            continue
        row = master_row_from_position(p)
        row["provenance"] = "BACKFILL_POSITIONS"
        if dry_run:
            stats["inserted"] += 1
            continue
        rid = append_algo_execution_master(ledger_db, row)
        if rid:
            stats["inserted"] += 1
        else:
            stats["skipped_duplicate"] += 1
    return {**stats, "ok": True}


def duration_between(entry_raw: Any, exit_raw: Any) -> str:
    a = parse_desk_timestamp(entry_raw)
    b = parse_desk_timestamp(exit_raw)
    if not a or not b:
        return "—"
    sec = max(0, int((b - a).total_seconds()))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}h {m:02d}m {s:02d}s"


def append_algo_execution_master(ledger_db: Path, row: Dict[str, Any]) -> Optional[str]:
    """Insert one closed algo trade. Returns row_id or None if duplicate position_id."""
    pos_id = str(row.get("position_id") or "").strip()
    if not pos_id:
        return None
    row_id = str(row.get("row_id") or f"AEM_{uuid.uuid4().hex[:16].upper()}")
    now = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST")
    try:
        with sqlite3.connect(ledger_db, timeout=5) as conn:
            ensure_algo_execution_master(conn)
            conn.execute(
                f"""
                INSERT INTO {TABLE} (
                    row_id, position_id, trade_date, strategy_id, setup_name, direction, side,
                    contract_symbol, security_id, strike, index_entry, index_exit,
                    option_entry, option_exit, qty, entry_time_ist, exit_time_ist, duration_text,
                    gross_pnl_inr, dhan_charges_inr, net_pnl_inr, outcome, mode, source,
                    provenance, created_at_ist, immutable
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)
                """,
                (
                    row_id,
                    pos_id,
                    str(row.get("trade_date") or "")[:10],
                    row.get("strategy_id"),
                    row.get("setup_name"),
                    row.get("direction"),
                    row.get("side"),
                    row.get("contract_symbol"),
                    row.get("security_id"),
                    row.get("strike"),
                    row.get("index_entry"),
                    row.get("index_exit"),
                    row.get("option_entry"),
                    row.get("option_exit"),
                    int(row.get("qty") or 0),
                    format_desk_timestamp_ist(row.get("entry_time_ist")),
                    format_desk_timestamp_ist(row.get("exit_time_ist")),
                    row.get("duration_text") or duration_between(row.get("entry_time_ist"), row.get("exit_time_ist")),
                    float(row.get("gross_pnl_inr") or 0.0),
                    float(row.get("dhan_charges_inr") or 0.0),
                    float(row.get("net_pnl_inr") or 0.0),
                    row.get("outcome"),
                    row.get("mode"),
                    row.get("source"),
                    str(row.get("provenance") or "LIVE_ALGO_PAPER"),
                    now,
                ),
            )
            conn.commit()
        return row_id
    except sqlite3.IntegrityError:
        log.debug("algo_execution_master: position %s already recorded", pos_id)
        return None
    except sqlite3.Error as exc:
        log.warning("algo_execution_master insert failed: %s", exc)
        return None


def master_row_from_position(p: Dict[str, Any]) -> Dict[str, Any]:
    """Build master payload from a closed positions row (dict)."""
    sym = str(p.get("symbol") or "")
    strike = None
    for part in sym.replace(",", " ").split():
        if part.isdigit() and len(part) >= 4:
            strike = int(part)
            break
    direction = "LONG"
    if "PE" in sym.upper() or str(p.get("side", "")).upper() == "SELL":
        direction = "SHORT"
    entry_t = p.get("entry_time")
    exit_t = p.get("exit_time")
    ep = float(p.get("entry_price") or 0.0)
    xp = float(p.get("exit_price") or 0.0)
    qty = int(p.get("qty") or 0)
    gross = float(p.get("gross_pnl") if p.get("gross_pnl") is not None else (xp - ep) * qty)
    charges = float(p.get("dhan_charges") or 0.0)
    if not charges and gross and p.get("net_pnl") is not None:
        charges = round(gross - float(p.get("net_pnl")), 2)
    trade_date = ""
    dt = parse_desk_timestamp(entry_t)
    if dt:
        trade_date = dt.date().isoformat()
    elif entry_t:
        trade_date = str(entry_t)[:10]
    return {
        "position_id": p.get("position_id"),
        "trade_date": trade_date,
        "strategy_id": p.get("strategy_id"),
        "setup_name": p.get("strategy_setup") or p.get("strategy_name"),
        "direction": direction,
        "side": p.get("side") or "BUY",
        "contract_symbol": sym,
        "security_id": p.get("security_id"),
        "strike": strike,
        "index_entry": p.get("spot_entry"),
        "index_exit": p.get("spot_exit"),
        "option_entry": ep,
        "option_exit": xp,
        "qty": qty,
        "entry_time_ist": entry_t,
        "exit_time_ist": exit_t,
        "duration_text": p.get("duration") or duration_between(entry_t, exit_t),
        "gross_pnl_inr": gross,
        "dhan_charges_inr": charges,
        "net_pnl_inr": float(p.get("net_pnl") or 0.0),
        "outcome": p.get("exit_reason") or "CLOSED",
        "mode": p.get("mode"),
        "source": p.get("source") or p.get("placed_by"),
        "provenance": "LIVE_ALGO_PAPER",
        "mode": p.get("mode"),
        "execution_mode": str(p.get("mode") or "PAPER_ONLY"),
    }


def fetch_master_trades(
    ledger_db: Path,
    start: Optional[str] = None,
    end: Optional[str] = None,
    strategy_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    if not ledger_db.is_file():
        return []
    q = f"SELECT * FROM {TABLE} WHERE immutable=1"
    args: List[Any] = []
    if start:
        q += " AND trade_date>=?"
        args.append(start[:10])
    if end:
        q += " AND trade_date<=?"
        args.append(end[:10])
    q += " ORDER BY entry_time_ist ASC"
    try:
        with sqlite3.connect(f"file:{ledger_db}?mode=ro", uri=True, timeout=3) as conn:
            conn.row_factory = sqlite3.Row
            ensure_algo_execution_master(conn)
            rows = [dict(r) for r in conn.execute(q, args)]
    except sqlite3.Error as exc:
        log.warning("algo_execution_master read failed: %s", exc)
        return []
    if strategy_id and str(strategy_id).upper() != "ALL":
        sid = str(strategy_id).upper()
        rows = [r for r in rows if sid in str(r.get("strategy_id") or "").upper()]
    out: List[Dict[str, Any]] = []
    for r in rows:
        mode = str(r.get("mode") or "PAPER_ONLY")
        trade_date = str(r.get("trade_date") or "")[:10]
        net = r.get("net_pnl_inr")
        out.append(
            {
                "trade_id": r["row_id"],
                "position_id": r["position_id"],
                "trade_date": trade_date,
                "Date": trade_date,
                "date_only": trade_date,
                "setup_name": r.get("setup_name"),
                "setup": r.get("setup_name"),
                "Setup_Name": r.get("setup_name"),
                "strategy_id": r.get("strategy_id"),
                "contract_symbol": r.get("contract_symbol"),
                "symbol": r.get("contract_symbol"),
                "Contract_Symbol": r.get("contract_symbol"),
                "direction": r.get("direction"),
                "Direction": r.get("direction"),
                "entry_ist": r.get("entry_time_ist"),
                "exit_ist": r.get("exit_time_ist"),
                "entry_time": r.get("entry_time_ist"),
                "exit_time": r.get("exit_time_ist"),
                "duration": r.get("duration_text"),
                "index_entry": r.get("index_entry"),
                "index_exit": r.get("index_exit"),
                "Index_Entry": r.get("index_entry"),
                "Index_Exit": r.get("index_exit"),
                "option_entry": r.get("option_entry"),
                "option_exit": r.get("option_exit"),
                "gross_pnl": r.get("gross_pnl_inr"),
                "charges": r.get("dhan_charges_inr"),
                "dhan_charges": r.get("dhan_charges_inr"),
                "net_pnl": net,
                "Net_Pnl_INR": net,
                "outcome": r.get("outcome"),
                "result": r.get("outcome"),
                "provenance": r.get("provenance") or "LIVE_ALGO_MASTER",
                "data_source": "algo_execution_master",
                "data_source_tag": DATA_SOURCE_IMMUTABLE,
                "Data_Type": f"{DATA_SOURCE_IMMUTABLE} ({mode})",
                "price_type": "REAL_ALGO_MASTER",
                "mode": mode,
                "execution_mode": mode,
                "replay_mode": "IMMUTABLE_MASTER",
                "source": "MASTER",
            }
        )
    return out


def snapshot_for_audit(ledger_db: Path, limit: int = 500) -> str:
    rows = fetch_master_trades(ledger_db)[-limit:]
    return json.dumps(rows, indent=2, default=str)


def master_to_backtest_row(m: Dict[str, Any]) -> Dict[str, Any]:
    """Map immutable master trade → run_date_range_backtest row shape."""
    ep = m.get("option_entry")
    xp = m.get("option_exit")
    net = float(m.get("net_pnl") or 0.0)
    gross = float(m.get("gross_pnl") if m.get("gross_pnl") is not None else net)
    chg = float(m.get("dhan_charges") or m.get("charges") or 0.0)
    ent = m.get("entry_ist") or m.get("entry_time") or ""
    trade_date = str(ent)[:10] if ent else ""
    return {
        "Trade_ID": m.get("trade_id") or m.get("position_id"),
        "Date": ent,
        "date_only": trade_date,
        "entry_day": trade_date,
        "entry_hm": str(ent)[11:16] if len(str(ent)) >= 16 else "09:30",
        "exit_day": str(m.get("exit_ist") or m.get("exit_time") or trade_date)[:10],
        "exit_hm": str(m.get("exit_ist") or m.get("exit_time") or "")[11:16],
        "Setup_Name": m.get("setup_name") or m.get("setup"),
        "Contract_Symbol": str(m.get("contract_symbol") or m.get("symbol") or "") + " (Immutable Master)",
        "Direction": m.get("direction") or "LONG",
        "Index_Entry": m.get("index_entry"),
        "Index_Exit": m.get("index_exit"),
        "Option_Entry_Display": f"{float(ep):.2f} (Master)" if ep is not None else "—",
        "Option_Exit_Display": f"{float(xp):.2f} (Master)" if xp is not None else "—",
        "option_entry": ep,
        "option_exit": xp,
        "Exit_Reason": m.get("outcome"),
        "exit_reason": m.get("outcome"),
        "Net_Pnl_Display": f"₹{net:,.2f} (Immutable Master)",
        "net_pnl": net,
        "gross_pnl": gross,
        "gross_pnl_inr": gross,
        "charges": chg,
        "dhan_charges": chg,
        "fixed_net_pnl_row": net,
        "st_pnl_pts": 0.0,
        "fx_pnl_pts": 0.0,
        "Trailing_Details": "algo_execution_master (append-only)",
        "All_Fall_Off": "Live fill — not resynthesized",
        "Strategy_Filter_Tag": m.get("strategy_id") or "ALGO",
        "Data_Type": "Algo Real Data (Master)",
        "strategy_id": m.get("strategy_id"),
        "source": "MASTER",
        "price_type": "REAL_ALGO_MASTER",
        "provenance": m.get("provenance") or "LIVE_ALGO_MASTER",
        "data_source_tag": DATA_SOURCE_IMMUTABLE,
        "Data_Type": f"{DATA_SOURCE_IMMUTABLE} ({m.get('execution_mode') or m.get('mode') or 'PAPER'})",
        "mode": m.get("mode") or m.get("execution_mode"),
        "execution_mode": m.get("execution_mode") or m.get("mode"),
    }
