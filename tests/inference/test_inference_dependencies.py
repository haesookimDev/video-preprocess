"""Keep model backends behind the local provider implementation."""

import ast
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _imported_roots(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            roots.add((node.module or "").split(".", 1)[0])
    return roots


def test_embedding_consumers_do_not_import_sentence_transformers() -> None:
    consumers = [
        PROJECT_ROOT / "src" / "pipeline" / "stages" / "s10_index.py",
        PROJECT_ROOT / "src" / "query.py",
    ]

    for consumer in consumers:
        assert "sentence_transformers" not in _imported_roots(consumer)


def test_sentence_transformer_import_is_lazy_inside_local_loader() -> None:
    provider_path = (
        PROJECT_ROOT
        / "src"
        / "video_preprocess"
        / "inference"
        / "local"
        / "embedding.py"
    )
    tree = ast.parse(
        provider_path.read_text(encoding="utf-8"),
        filename=str(provider_path),
    )
    top_level_roots = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            top_level_roots.update(
                alias.name.split(".", 1)[0] for alias in node.names
            )
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            top_level_roots.add((node.module or "").split(".", 1)[0])

    assert "sentence_transformers" not in top_level_roots
    assert "sentence_transformers" in _imported_roots(provider_path)


def test_caption_stage_does_not_import_model_or_image_libraries() -> None:
    stage_path = (
        PROJECT_ROOT
        / "src"
        / "pipeline"
        / "stages"
        / "s08_captions.py"
    )

    imported = _imported_roots(stage_path)

    assert "transformers" not in imported
    assert "PIL" not in imported


def test_transformers_and_pillow_imports_are_lazy_in_caption_provider() -> None:
    provider_path = (
        PROJECT_ROOT
        / "src"
        / "video_preprocess"
        / "inference"
        / "local"
        / "caption.py"
    )
    tree = ast.parse(
        provider_path.read_text(encoding="utf-8"),
        filename=str(provider_path),
    )
    top_level_roots = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            top_level_roots.update(
                alias.name.split(".", 1)[0] for alias in node.names
            )
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            top_level_roots.add((node.module or "").split(".", 1)[0])

    assert "transformers" not in top_level_roots
    assert "PIL" not in top_level_roots
    assert "transformers" in _imported_roots(provider_path)
    assert "PIL" in _imported_roots(provider_path)


def test_stt_stage_does_not_import_faster_whisper() -> None:
    stage_path = (
        PROJECT_ROOT
        / "src"
        / "pipeline"
        / "stages"
        / "s06_stt.py"
    )

    assert "faster_whisper" not in _imported_roots(stage_path)


def test_faster_whisper_imports_are_lazy_in_stt_provider() -> None:
    provider_path = (
        PROJECT_ROOT
        / "src"
        / "video_preprocess"
        / "inference"
        / "local"
        / "stt.py"
    )
    tree = ast.parse(
        provider_path.read_text(encoding="utf-8"),
        filename=str(provider_path),
    )
    top_level_roots = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            top_level_roots.update(
                alias.name.split(".", 1)[0] for alias in node.names
            )
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            top_level_roots.add((node.module or "").split(".", 1)[0])

    assert "faster_whisper" not in top_level_roots
    assert "faster_whisper" in _imported_roots(provider_path)


def test_diarization_stage_does_not_import_model_or_hub_libraries() -> None:
    stage_path = (
        PROJECT_ROOT
        / "src"
        / "pipeline"
        / "stages"
        / "s07_diarize.py"
    )

    imported = _imported_roots(stage_path)

    assert "pyannote" not in imported
    assert "huggingface_hub" not in imported


def test_pyannote_and_hub_imports_are_lazy_in_diarization_provider() -> None:
    provider_path = (
        PROJECT_ROOT
        / "src"
        / "video_preprocess"
        / "inference"
        / "local"
        / "diarization.py"
    )
    tree = ast.parse(
        provider_path.read_text(encoding="utf-8"),
        filename=str(provider_path),
    )
    top_level_roots = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            top_level_roots.update(
                alias.name.split(".", 1)[0] for alias in node.names
            )
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            top_level_roots.add((node.module or "").split(".", 1)[0])

    assert "pyannote" not in top_level_roots
    assert "huggingface_hub" not in top_level_roots
    assert "pyannote" in _imported_roots(provider_path)
    assert "huggingface_hub" in _imported_roots(provider_path)
