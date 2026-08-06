# Repository Guidelines

## Project Structure & Module Organization

The Python entry points are `src/run_pipeline.py` for preprocessing and `src/query.py` for searching an existing index. Shared orchestration, configuration, and logging live in `src/pipeline/`. Processing steps are ordered modules under `src/pipeline/stages/`, named `s01_probe.py` through `s11_context.py`. Each stage exposes `NAME`, `OUTPUT`, and `run(ctx)` and is registered in `pipeline/runner.py`. Design notes are in `docs/`; small media fixtures are in `samples/`. Generated artifacts belong under `output/<video_stem>/` and must not be committed.

The current code is a local single-process MVP. The approved target architecture separates the Pipeline Engine, Executor, Artifact/Run Stores, and local/HTTP Inference Providers. Do not treat the target package layout as already implemented. Use `docs/STATUS.md` to distinguish current behavior from planned work.

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

## Architecture Migration & Session Continuity

At the start of any implementation session, read these sources in order:

1. `docs/STATUS.md` for the active phase, completed work, known issues, and next task.
2. `docs/06-target-architecture.md` for component ownership and dependency rules.
3. `docs/07-execution-inference-contracts.md` for Stage, Executor, Artifact, and Inference contracts.
4. `docs/08-development-roadmap.md` for sequencing and phase exit criteria.
5. Relevant records under `docs/adr/` for durable architectural decisions.

Before changing files, compare those documents with the actual code and inspect `git status --short`. Preserve user changes and do not mark planned components as implemented without code and verification.

The architectural boundary is intentional:

- The Engine owns DAG planning, run state, cache decisions, and execution policy.
- An Executor owns where and how a Stage task runs.
- An Inference Provider owns how one model inference runs locally or through an endpoint.
- Stages must not choose deployment locations or instantiate concrete providers.
- Large inputs and outputs cross boundaries as Artifact references, not host-specific absolute paths.
- CLI, API, and queue adapters must call the same Application Service.

After any material implementation task, update `docs/STATUS.md` in the same change. Record what completed, validation commands/results, compatibility concerns, and the next concrete task. Also update:

- `docs/07-execution-inference-contracts.md` when a public schema, configuration, error, or API contract changes.
- `docs/06-target-architecture.md` and an ADR when ownership or dependency direction changes.
- `docs/08-development-roadmap.md` when scope, order, or exit criteria change.
- `docs/05-pipeline.md` and `README.md` when actual user-visible stage behavior or commands change.

Keep the default test path independent of model downloads and network access. Use fakes for Engine, Executor, and Provider contract tests; keep real-model and end-to-end media validation as explicit integration runs.
