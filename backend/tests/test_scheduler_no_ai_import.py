"""Phase D, item 7: the scheduler module must have ZERO import path to
app.ai.* (the AI orchestrator / anything that calls /api/investigate/*)
-- structurally, not just "it currently doesn't happen to". Two
independent checks: a static source-grep of the module's own import
statements, AND a real runtime check that app.ai is never present in
sys.modules as a result of importing app.scheduler alone (catches an
indirect import through some other module just as well as a direct
one)."""
from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

SCHEDULER_PATH = Path(__file__).resolve().parents[1] / "app" / "scheduler.py"


def _imported_module_names(path: Path) -> set:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                # Resolve a relative "from . import X" / "from .ai import Y"
                # to a dotted form good enough for the "contains .ai" check
                # below -- level>0 relative imports inside this package.
                names.add(("." * node.level) + node.module)
    return names


def test_scheduler_module_source_has_no_direct_ai_import():
    imports = _imported_module_names(SCHEDULER_PATH)
    offending = [n for n in imports if n.endswith(".ai") or ".ai." in n or n == "ai"]
    assert not offending, f"app/scheduler.py directly imports AI-orchestrator-adjacent modules: {offending}"


def test_scheduler_module_source_never_calls_investigate_route_in_code():
    """Belt-and-suspenders: the scheduler's ACTUAL CODE (not its
    docstrings/comments, which legitimately discuss this constraint in
    prose) must never reference the /api/investigate/ path string as a
    real call target, e.g. via a stray requests.post someone adds later
    bypassing the import check above. Walks the AST and inspects only
    string literals, which docstrings are excluded from by using
    ast.get_docstring/module-level Expr-of-Constant-str filtering."""
    tree = ast.parse(SCHEDULER_PATH.read_text(encoding="utf-8"))
    docstring_nodes = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if (node.body and isinstance(node.body[0], ast.Expr)
                    and isinstance(node.body[0].value, ast.Constant)
                    and isinstance(node.body[0].value.value, str)):
                docstring_nodes.add(id(node.body[0].value))
    offending_literals = [
        node.value for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
        and id(node) not in docstring_nodes and "/api/investigate" in node.value
    ]
    assert not offending_literals, f"found /api/investigate referenced in real code: {offending_literals}"


def test_importing_scheduler_alone_never_pulls_in_app_ai_package():
    """Runs in a FRESH subprocess (so sys.modules starts empty) and
    imports ONLY app.scheduler -- then checks that app.ai and its
    submodules were never loaded as a side effect. This catches an
    INDIRECT import (through some helper module) just as well as a
    direct one, which the static AST check above cannot."""
    script = (
        "import sys; sys.path.insert(0, %r)\n"
        "import os; os.environ['TIP_DB_PATH'] = ':memory:'\n"
        "import app.scheduler\n"
        "ai_modules = [m for m in sys.modules if m == 'app.ai' or m.startswith('app.ai.')]\n"
        "print('AI_MODULES=' + ','.join(sorted(ai_modules)))\n"
    ) % str(SCHEDULER_PATH.parents[1])
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, f"import failed: {result.stderr}"
    line = [l for l in result.stdout.splitlines() if l.startswith("AI_MODULES=")][0]
    loaded = line.split("=", 1)[1]
    assert loaded == "", f"importing app.scheduler alone pulled in app.ai modules: {loaded}"
