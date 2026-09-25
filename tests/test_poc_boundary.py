"""The dependency points one way (rev 1.4): poc/ may import app/ and workers/, never the reverse — so deleting
poc/ cannot break production."""
import ast
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_production_entry_points_load_no_poc_module() -> None:
    """In a fresh interpreter, app.main and workers.tasks (as a worker loads it) pull in no poc module."""
    code = ("import sys, app.main, workers.tasks as w; w.app.loader.import_default_modules(); "
            "print(sorted(m for m in sys.modules if m == 'poc' or m.startswith('poc.')))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout
    assert out.strip().splitlines()[-1] == "[]"


def test_production_source_never_imports_or_includes_poc() -> None:
    """No file in app/ or workers/ imports poc, or names a poc module in a string (such as a Celery include=)."""
    offenders = []
    for path in [*ROOT.glob("app/**/*.py"), *ROOT.glob("workers/**/*.py")]:
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Import) and any(a.name == "poc" or a.name.startswith("poc.") for a in node.names):
                offenders.append(f"{path.relative_to(ROOT)}: import")
            elif isinstance(node, ast.ImportFrom) and node.module and (node.module == "poc" or
                                                                       node.module.startswith("poc.")):
                offenders.append(f"{path.relative_to(ROOT)}: from-import")
            elif isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value.startswith("poc."):
                offenders.append(f"{path.relative_to(ROOT)}: string {node.value!r}")
    assert offenders == []


def _loaded(modules: str, prefixes: tuple[str, ...]) -> list[str]:
    """Import `modules` in a fresh interpreter; return every loaded module under the given prefixes."""
    code = (f"import sys, {modules}; "
            f"print(sorted(m for m in sys.modules if any(m == p or m.startswith(p + '.') for p in {prefixes!r})))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout
    return eval(out.strip().splitlines()[-1])  # noqa: S307 — our own printed list


def test_scan_worker_never_loads_the_fastapi_app() -> None:
    """poc.worker (worker-scan) loads neither app.main nor FastAPI: TASK_SCAN_STUB comes from poc/__init__.py."""
    assert _loaded("poc.worker", ("app.main", "fastapi", "starlette", "poc.main")) == []


def test_api_never_loads_the_worker_app() -> None:
    """poc.main (the api) loads neither workers.tasks nor Celery's worker machinery (design.md §6.2a)."""
    assert _loaded("poc.main", ("workers", "poc.worker")) == []
