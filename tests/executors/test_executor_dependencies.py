"""Keep Executor infrastructure independent of concrete Stage libraries."""

import ast
from pathlib import Path


EXECUTOR_ROOT = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "video_preprocess"
    / "executors"
)

FORBIDDEN_ROOTS = {
    "av",
    "faster_whisper",
    "huggingface_hub",
    "numpy",
    "onnxruntime",
    "pipeline",
    "pyannote",
    "scenedetect",
    "sentence_transformers",
    "torch",
    "transformers",
}


def test_executors_do_not_import_legacy_stage_or_model_libraries() -> None:
    forbidden_imports = []
    for path in EXECUTOR_ROOT.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots = {
                    alias.name.split(".", 1)[0]
                    for alias in node.names
                }
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                roots = {(node.module or "").split(".", 1)[0]}
            else:
                continue
            for root in roots & FORBIDDEN_ROOTS:
                forbidden_imports.append((path.name, root))

    assert forbidden_imports == []
