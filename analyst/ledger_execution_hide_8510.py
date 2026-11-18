"""Mark paper execution rows hidden in the append-only ledger (no DELETE)."""
from __future__ import annotations

import json
import sqlite3
from datetime import date
from typing import Any, Dict, Iterable, Optional, Set

HIDE_CATEGORY_OFF_MARKET = "OFF_MARKET_TEST"
HIDE_CATEGORY_EXPIRED = "EXPIRED_CONTRACT"
HIDE_CATEGORY_DISABLED_LANE = "DISABLED_LANE"
HIDE_CATEGORY_TEST_FIXTURE = "TEST_FIXTURE"


def ensure_execution_provenance(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS execution_provenance(
            entity TEXT NOT NULL,
            id TEXT NOT NULL,
            category TEXT,
            parent_position TEXT,
            payload TEXT,
            hidden INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(entity, id)
        )
        """
    )


def hidden_execution_ids(conn: sqlite3.Connection, entity: str) -> Set[str]:
    ensure_execution_provenance(conn)
    rows = conn.execute(
        "SELECT id FROM execution_provenance WHERE entity=? AND hidden=1",
        (entity,),
    ).fetchall()
    return {str(r[0]) for r in rows if r and r[0]}


def is_execution_hidden(conn: sqlite3.Connection, entity: str, row_id: str) -> bool:
    ensure_execution_provenance(conn)
    row = conn.execute(
        "SELECT hidden FROM execution_provenance WHERE entity=? AND id=?",
        (entity, row_id),
    ).fetchone()
    return bool(row and int(row[0] or 0) == 1)


def hide_execution_rows(
    conn: sqlite3.Connection,
    entity: str,
    row_ids: Iterable[str],
    *,
    category: str,
    reason: str,
) -> int:
    """Upsert hidden=1 provenance rows. Returns count updated."""
    ensure_execution_provenance(conn)
    payload = json.dumps({"reason": reason}, ensure_ascii=False)
    n = 0
    for rid in row_ids:
        rid_s = str(rid or "").strip()
        if not rid_s:
            continue
        conn.execute(
            """
            INSERT INTO execution_provenance(entity, id, category, parent_position, payload, hidden)
            VALUES (?, ?, ?, ?, ?, 1)
            ON CONFLICT(entity, id) DO UPDATE SET
                category=excluded.category,
                payload=excluded.payload,
                hidden=1
            """,
            (entity, rid_s, category, "", payload),
        )
        n += 1
    return n


def pick_hide_category(reasons: Iterable[str]) -> str:
    rs = list(reasons)
    if "test_fixture" in rs:
        return HIDE_CATEGORY_TEST_FIXTURE
    if "expired_contract" in rs:
        return HIDE_CATEGORY_EXPIRED
    if "disabled_lane" in rs:
        return HIDE_CATEGORY_DISABLED_LANE
    return HIDE_CATEGORY_OFF_MARKET


def order_book_row_hidden_flags(row: Dict[str, Any], hidden_ids: Optional[Set[str]] = None) -> bool:
    rid = str(row.get("order_id") or row.get("position_id") or "")
    if hidden_ids is not None and rid and rid in hidden_ids:
        return True
    if row.get("execution_hidden") or row.get("book_hidden"):
        return True
    return False