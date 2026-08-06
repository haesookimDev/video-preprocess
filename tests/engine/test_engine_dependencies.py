"""Keep the Engine independent of media, model, and transport libraries."""

import ast
from pathlib import Path


ENGINE_ROOT = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "video_preprocess"
    / "engine"
)

FORBIDDEN_ROOTS = {
    "av",
    "faster_whisper",
    "huggingface_hub",
    "numpy",
    "onnxruntime",
    "pyannote",
    "requests",
    "scenedetect",
    "sentence_transformers",
    "torch",
    "transformers",
}


def test_engine_does_not_import_media_model_or_http_libraries() -> None:
    forbidden_imports = []
    for path in ENGINE_ROOT.glob("*.py"):
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
