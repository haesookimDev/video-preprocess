"""Keep the domain contract package free of third-party imports."""

import ast
import sys
from pathlib import Path


DOMAIN_ROOT = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "video_preprocess"
    / "domain"
)


def test_domain_modules_only_import_standard_library() -> None:
    external_imports = []
    for path in DOMAIN_ROOT.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    root = alias.name.split(".", 1)[0]
                    if root not in sys.stdlib_module_names:
                        external_imports.append((path.name, alias.name))
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                root = (node.module or "").split(".", 1)[0]
                if root not in sys.stdlib_module_names:
                    external_imports.append((path.name, node.module))

    assert external_imports == []

