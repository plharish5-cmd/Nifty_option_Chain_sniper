"""Ban PEP 604 unions (`str | None`) anywhere in the backend. The runtime venv is Python 3.9.

WHY THIS EXISTS
On 2026-11-03 a single `asof: str | None = Query(None)` in api/main.py crash-looped the desk two
hours before the open. Nothing caught it:

  * py_compile passed      — the syntax is legal, it is the EVALUATION that fails
  * pytest passed          — no test imported the FastAPI app
  * the dev machine passed — it runs 3.13, where the annotation evaluates fine

FastAPI and pydantic call get_type_hints() on every route signature at import time, so a PEP 604
union raises TypeError before the server can bind. `from __future__ import annotations` does NOT
rescue it: that defers evaluation, it does not teach 3.9 how to perform it.

requirements.txt line 1 says "Python 3.9+ (tested on 3.9.23 — same as your OC .venv)". This test
enforces that rather than trusting anyone to remember it.

SCOPE: app/api/ only. Elsewhere — engine/, market/ — PEP 604 unions are harmless on 3.9 under
`from __future__ import annotations`, because nothing ever evaluates them; nodes.py and
gann_pivots_gaps.py already use them and run fine. It is specifically FastAPI's route
registration that forces evaluation, so that is what this guards. Widening it would fail working
code for no benefit.

Use Optional[X] and Union[X, Y] instead.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parent.parent / "app" / "api"


def _annotations(tree: ast.AST):
    """Every expression that is used as a type annotation, with its line number."""
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = node.args
            for a in [*args.posonlyargs, *args.args, *args.kwonlyargs,
                      args.vararg, args.kwarg]:
                if a is not None and a.annotation is not None:
                    yield a.annotation, a.lineno
            if node.returns is not None:
                yield node.returns, node.lineno
        elif isinstance(node, ast.AnnAssign) and node.annotation is not None:
            yield node.annotation, node.lineno


def _has_bitor(expr: ast.AST) -> bool:
    return any(isinstance(n, ast.BinOp) and isinstance(n.op, ast.BitOr)
               for n in ast.walk(expr))


def _py_files():
    return sorted(p for p in APP.rglob("*.py") if "__pycache__" not in p.parts)


def test_app_directory_is_present():
    assert APP.is_dir(), APP
    assert _py_files(), "no python files found to scan"
    assert any(p.name == "main.py" for p in _py_files()), "main.py must be in scope"


@pytest.mark.parametrize("path", _py_files(), ids=lambda p: str(p.name))
def test_no_pep604_unions_in_annotations(path: Path):
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError as e:                      # a genuinely broken file is a different test
        pytest.fail(f"{path}: {e}")
    bad = [line for expr, line in _annotations(tree) if _has_bitor(expr)]
    assert not bad, (
        f"{path.relative_to(APP.parent)} uses `X | Y` in an annotation at line(s) "
        f"{bad}. The runtime venv is Python 3.9 and FastAPI/pydantic evaluate annotations at "
        f"import, so this crash-loops the service. Use Optional[X] / Union[X, Y]."
    )
