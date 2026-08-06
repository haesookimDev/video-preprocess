# Repository Guidelines

## Project Structure & Module Organization

The Python entry points are `src/run_pipeline.py` for preprocessing and `src/query.py` for searching an existing index. Shared orchestration, configuration, and logging live in `src/pipeline/`. Processing steps are ordered modules under `src/pipeline/stages/`, named `s01_probe.py` through `s11_context.py`. Each stage exposes `NAME`, `OUTPUT`, and `run(ctx)` and is registered in `pipeline/runner.py`. Design notes are in `docs/`; small media fixtures are in `samples/`. Generated artifacts belong under `output/<video_stem>/` and must not be committed.

## Setup, Run, and Development Commands

This repository has no separate build step. Use Python 3.10+ and install the native FFmpeg dependency first:

```bash
brew install ffmpeg
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python src/run_pipeline.py samples/sample.mp4
.venv/bin/python src/query.py output/sample "음성 구간 검출" --topk 2
```

Add `--force` to rerun completed stages. Use `--language ko`, `--whisper-model base`, or `--scene-threshold 27` when validating configuration changes. Inspect `output/<name>/run_summary.json` and the timestamped files in `logs/` after a run.

## Coding Style & Naming Conventions

Follow PEP 8 with four-space indentation, type hints, `pathlib.Path`, and UTF-8 file access. Use `snake_case` for functions and variables, `PascalCase` for classes, and uppercase names for module constants. Keep stage filenames numerically prefixed and output directories aligned with the stage number. Prefer short module docstrings and structured logging over `print`; reserve `print` for CLI results or errors. No formatter or linter is configured, so keep imports grouped and lines readable (roughly 88 characters).

## Testing Guidelines

There is currently no automated test suite or coverage threshold. Before submitting, run the full pipeline against `samples/sample.mp4`, confirm the summary status is `ok`, and exercise `src/query.py`. For stage-specific changes, remove that stage's generated directory or run with `--force`, then inspect its JSON, Markdown, database, or media outputs. If adding tests, place them in `tests/` and name files `test_<module>.py` for pytest discovery.

## Commit & Pull Request Guidelines

Recent history uses concise Conventional Commit prefixes such as `feat:` and `chore:`; use `fix:`, `docs:`, or `test:` where appropriate. Keep each commit focused. Pull requests should explain the behavior change, list commands run, link related issues, and include representative logs or output paths. Attach before/after screenshots when keyframes, captions, or timeline rendering changes.

## Security & Configuration

Store the Hugging Face credential as `HF_TOKEN` in the ignored `.env` file. Never commit tokens, downloaded models, generated databases, logs, or user-provided media.
