"""The dependency points one way (rev 1.4): workers/poc/ may import app/, engine/ and shared/, never the reverse — so
deleting poc/ cannot break the workers."""
import ast
import subprocess
import sys
from pathlib import Path

WORKERS = Path(__file__).resolve().parents[2]


def _poc_offenders(paths: list[Path], root: Path) -> list[str]:
    """Every import of poc, or string naming a poc module (such as a Celery include=), in these files."""
    offenders = []
    for path in paths:
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Import) and any(a.name == "poc" or a.name.startswith("poc.") for a in node.names):
                offenders.append(f"{path.relative_to(root)}: import")
            elif isinstance(node, ast.ImportFrom) and node.module and (node.module == "poc" or
                                                                       node.module.startswith("poc.")):
                offenders.append(f"{path.relative_to(root)}: from-import")
            elif isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value.startswith("poc."):
                offenders.append(f"{path.relative_to(root)}: string {node.value!r}")
    return offenders


def _loaded(modules: str, prefixes: tuple[str, ...]) -> list[str]:
    """Import `modules` in a fresh interpreter; return every loaded module under the given prefixes."""
    code = (f"import sys, {modules}; "
            f"print(sorted(m for m in sys.modules if any(m == p or m.startswith(p + '.') for p in {prefixes!r})))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout
    return eval(out.strip().splitlines()[-1])  # noqa: S307 — our own printed list


def test_the_worker_entry_point_loads_no_poc_module() -> None:
    """In a fresh interpreter, app.tasks (as a worker loads it) pulls in no poc module."""
    code = ("import sys, app.tasks as w; w.app.loader.import_default_modules(); "
            "print(sorted(m for m in sys.modules if m == 'poc' or m.startswith('poc.')))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout
    assert out.strip().splitlines()[-1] == "[]"


def test_worker_source_never_imports_or_includes_poc() -> None:
    """No file in app/ or engine/ imports poc, or names a poc module in a string (such as a Celery include=)."""
    assert _poc_offenders([*WORKERS.glob("app/**/*.py"), *WORKERS.glob("engine/**/*.py")], WORKERS) == []


def test_scan_worker_never_loads_the_fastapi_app() -> None:
    """poc.worker (worker-scan) loads neither FastAPI nor any API module: TASK_SCAN_STUB comes from shared.constants."""
    assert _loaded("poc.worker", ("fastapi", "starlette", "poc.main")) == []
