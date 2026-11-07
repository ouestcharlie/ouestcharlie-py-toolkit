"""Package layering rules (OEC-72).

A module imports only from its own sub-package or a lower layer. Inside the
toolkit, modules import leaf modules, never a sub-package ``__init__``: those
re-export for agents, and importing them from inside would create cycles.
"""

import ast
import subprocess
import sys
from pathlib import Path

import pytest

PACKAGE = "ouestcharlie_toolkit"
SRC = Path(__file__).resolve().parents[1] / "src" / PACKAGE

# Lowest layer first.
LAYERS = ["storage", "model", "media", "xmp", "index", "renditions", "agent"]


def _modules() -> list[Path]:
    return sorted(p for p in SRC.rglob("*.py") if p.parent != SRC)


def _toolkit_imports(path: Path) -> list[str]:
    tree = ast.parse(path.read_text())
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.append(node.module)
        elif isinstance(node, ast.Import):
            found.extend(a.name for a in node.names)
    return [m for m in found if m == PACKAGE or m.startswith(PACKAGE + ".")]


@pytest.mark.parametrize("path", _modules(), ids=lambda p: str(p.relative_to(SRC)))
def test_imports_follow_layers(path: Path) -> None:
    layer = path.relative_to(SRC).parts[0]
    rank = LAYERS.index(layer)
    for module in _toolkit_imports(path):
        parts = module.split(".")
        assert len(parts) > 1, f"{module}: import a sub-package module, not the package root"
        target = parts[1]
        assert target in LAYERS, f"{module}: unknown sub-package {target!r}"
        assert LAYERS.index(target) <= rank, f"{layer} must not import {module} (higher layer)"


@pytest.mark.parametrize("path", _modules(), ids=lambda p: str(p.relative_to(SRC)))
def test_internal_imports_target_leaf_modules(path: Path) -> None:
    if path.name == "__init__.py":
        return
    for module in _toolkit_imports(path):
        target = SRC.joinpath(*module.split(".")[1:])
        assert not target.is_dir(), f"{module}: import the leaf module, not the sub-package"


def test_index_and_xmp_do_not_load_imageproc() -> None:
    """Only renditions/ needs the native image-proc binary."""
    code = (
        "import sys\n"
        "import ouestcharlie_toolkit, ouestcharlie_toolkit.index, ouestcharlie_toolkit.xmp\n"
        "sys.exit('ouestcharlie_imageproc' in sys.modules)\n"
    )
    assert subprocess.run([sys.executable, "-c", code]).returncode == 0
