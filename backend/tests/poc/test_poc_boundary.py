"""The dependency points one way (rev 1.4): backend/poc/ may import app/ and shared/, never the reverse — so
deleting poc/ cannot break the API."""
import ast
import subprocess
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2]
SHARED = BACKEND.parent / "shared"


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


def test_the_api_entry_point_loads_no_poc_module() -> None:
    """In a fresh interpreter, app.main pulls in no poc module."""
    assert _loaded("app.main", ("poc",)) == []


def test_api_and_shared_source_never_import_or_include_poc() -> None:
    """No file in app/ or shared/ imports poc, or names a poc module in a string."""
    paths = [*BACKEND.glob("app/**/*.py"), *SHARED.glob("*.py")]
    assert _poc_offenders(paths, BACKEND.parent) == []


def test_api_never_loads_the_worker_app() -> None:
    """poc.main (the api) loads no worker module nor the scan stub (design.md §6.2a)."""
    assert _loaded("poc.main", ("app.tasks", "app.clients", "engine", "poc.worker")) == []
