"""Refuse pytest writes to the production 8510 ledger."""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any, Union

_REPO_ROOT = Path(__file__).resolve().parents[1]
PRODUCTION_LEDGER = (_REPO_ROOT / "data/terminal8510/ledger.sqlite3").resolve()


def production_ledger_path() -> Path:
    return PRODUCTION_LEDGER


def _pytest_active() -> bool:
    return bool(os.environ.get("PYTEST_CURRENT_TEST"))


def resolve_ledger_path(db: Union[str, Path]) -> Path:
    p = Path(str(db))
    if not p.is_absolute():
        p = (_REPO_ROOT / p).resolve()
    else:
        p = p.resolve()
    return p


def assert_not_production_ledger_under_pytest(db: Union[str, Path]) -> None:
    if not _pytest_active():
        return
    target = resolve_ledger_path(db)
    if target == PRODUCTION_LEDGER:
        raise AssertionError(
            "Test attempted to open production ledger at data/terminal8510/ledger.sqlite3 — "
            "use tmp_path / conftest private config"
        )


def install_sqlite_connect_guard() -> None:
    if getattr(sqlite3, "_ledger_guard_installed", False):
        return
    _orig = sqlite3.connect

    def guarded_connect(database: Any, *args: Any, **kwargs: Any):
        if isinstance(database, (str, bytes, os.PathLike)):
            assert_not_production_ledger_under_pytest(database)
        return _orig(database, *args, **kwargs)

    sqlite3.connect = guarded_connect  # type: ignore[method-assign]
    sqlite3._ledger_guard_installed = True  # type: ignore[attr-defined]