"""app.errors is imported by workers, so it must stay free of database and web framework imports."""
import subprocess
import sys


def test_errors_module_imports_no_sqlalchemy_or_fastapi() -> None:
    """Importing app.errors in a fresh interpreter loads no SQLAlchemy, FastAPI or Starlette modules."""
    code = ("import sys, app.errors; "
            "print(sorted({m.split('.')[0] for m in sys.modules} & {'sqlalchemy', 'fastapi', 'starlette'}))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout
    assert out.strip() == "[]"
