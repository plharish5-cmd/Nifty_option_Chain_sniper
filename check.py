"""
Pre-flight check — run this after ANY edit, before launching Streamlit.

    python check.py

Compiles every module and inspects the AST for the classes of mistake that a
plain import will not catch: duplicate function or class definitions (Python
silently keeps the last one) and repeated keyword arguments in a call.

Streamlit compiles dashboard.py only when it runs, so a syntax error there
surfaces as a red box in the browser rather than at import time. This catches
it in the terminal instead.
"""
from __future__ import annotations

import ast
import pathlib
import py_compile
import sys
import builtins
import tempfile

#: names that legitimately arrive from imports or the runtime
KNOWN_GLOBALS = {
    "st", "pd", "np", "go", "config", "datetime", "date", "time", "timedelta",
    "Path", "json", "sys", "os", "re", "math", "an", "cp", "md", "pa", "su",
    "vb", "cf", "store", "levels", "lv_mod", "make_subplots", "field",
    "dataclass", "asdict", "replace", "norm", "requests", "logging", "ast",
    "py_compile", "importlib", "warnings", "traceback", "annotations",
}


def main() -> int:
    problems = 0
    for f in sorted(pathlib.Path(__file__).parent.glob("*.py")):
        try:
            py_compile.compile(str(f), cfile=tempfile.mktemp(), doraise=True)
        except py_compile.PyCompileError as e:
            print(f"  SYNTAX  {f.name}: {e.msg.strip().splitlines()[-1]}")
            problems += 1
            continue

        tree = ast.parse(f.read_text())

        top = [n.name for n in tree.body
               if isinstance(n, (ast.FunctionDef, ast.ClassDef))]
        for name in {n for n in top if top.count(n) > 1}:
            print(f"  DUPLICATE  {f.name}: '{name}' defined "
                  f"{top.count(name)}x — Python keeps only the last")
            problems += 1

        # Names read at MODULE level before they are ever assigned there.
        #
        # The Streamlit tab-ordering trap: tab bodies execute top to bottom,
        # so a name assigned inside a later tab does not exist for an earlier
        # one. It also catches a failed edit that removed an assignment but
        # left the reference behind.
        #
        # Two subtleties that produced false alarms in the first version:
        #   * ast.walk visits a statement breadth-first, so a tuple target
        #     (`a, b = f()`) can be seen AFTER the call that uses them. So
        #     stores are collected for the whole statement before loads are
        #     checked against them.
        #   * except handlers, comprehension targets, with-items and lambda
        #     arguments all bind names that never appear as a plain Store.
        assigned: set[str] = {"__file__", "__name__", "__doc__"}
        reported: set[str] = set()

        def bind(node) -> set[str]:
            """Every name this statement binds, however it binds it."""
            out: set[str] = set()
            for sub in ast.walk(node):
                if isinstance(sub, ast.Name) and isinstance(sub.ctx,
                                                            (ast.Store, ast.Del)):
                    out.add(sub.id)
                elif isinstance(sub, ast.alias):
                    out.add((sub.asname or sub.name).split(".")[0])
                elif isinstance(sub, ast.ExceptHandler) and sub.name:
                    out.add(sub.name)
                elif isinstance(sub, ast.arg):
                    out.add(sub.arg)
                elif isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef,
                                      ast.ClassDef)):
                    out.add(sub.name)
            return out

        for stmt in tree.body:
            # function and class bodies are their own scope — skip their
            # internals entirely, just record the name they define
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef,
                                 ast.ClassDef, ast.Import, ast.ImportFrom)):
                assigned |= bind(stmt)
                continue

            binds = bind(stmt)
            for sub in ast.walk(stmt):
                if not (isinstance(sub, ast.Name)
                        and isinstance(sub.ctx, ast.Load)):
                    continue
                if (sub.id in assigned or sub.id in binds
                        or sub.id in KNOWN_GLOBALS
                        or sub.id in dir(builtins)
                        or sub.id in reported):
                    continue
                print(f"  USED-BEFORE-ASSIGNED  {f.name}:{sub.lineno} "
                      f"'{sub.id}' read at module level before any assignment")
                problems += 1
                reported.add(sub.id)
            assigned |= binds

        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                kws = [k.arg for k in node.keywords if k.arg]
                for name in {k for k in kws if kws.count(k) > 1}:
                    print(f"  REPEATED KWARG  {f.name}:{node.lineno} "
                          f"'{name}' passed twice")
                    problems += 1

        print(f"  ok  {f.name}")

    print(f"\n{'PASS — nothing to fix' if not problems else f'{problems} problem(s) found'}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
