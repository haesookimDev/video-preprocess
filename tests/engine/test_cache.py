"""Contract tests for manifest-based Stage cache decisions."""

from dataclasses import replace

import pytest

from video_preprocess.domain import (
    ArtifactRef,
    Checksum,
    ModelExecution,
    StageManifest,
    StageResult,
    StageStatus,
    StageTask,
)
from video_preprocess.engine import (
    CacheMissReason,
    CacheStatus,
    EngineInputError,
    ManifestCacheEvaluator,
    compute_stage_cache_key,
)
from video_preprocess.storage import ArtifactVerification


def make_artifact(
    name: str,
    *,
    checksum: str | None = None,
    uri_run: str = "run-old",
) -> ArtifactRef:
    digest = checksum or (name.encode("utf-8").hex() * 8)[:64]
    return ArtifactRef(
        artifact_id=f"art-{name}",
        kind="json",
        uri=f"artifact://{uri_run}/{name}.json",
        media_type="application/json",
        size_bytes=42,
        checksum=Checksum("sha256", digest),
    )


def make_task(
    *,
    run_id: str = "run-new",
    stage_run_id: str = "stage-new",
    attempt: int = 1,
    trace_id: str = "trace-new",
    stage_version: str = "1.0.0",
    input_artifact: ArtifactRef | None = None,
    config: dict[str, object] | None = None,
    binding: str = "stt.default",
) -> StageTask:
    return StageTask(
        run_id=run_id,
        stage_run_id=stage_run_id,
        attempt=attempt,
        stage="06_stt",
        stage_version=stage_version,
        inputs={"audio": input_artifact or make_artifact("audio")},
        config={"language": "ko"} if config is None else config,
        model_bindings={"stt": binding},
        idempotency_key=f"idem-{run_id}",
        trace_id=trace_id,
    )


def expected_model(
    *,
    provider: str = "local.stt",
    revision: str = "rev-1",
) -> ModelExecution:
    return ModelExecution(
        slot="stt",
        provider=provider,
        model="faster-whisper",
        revision=revision,
        runtime="faster-whisper/1.2.1",
    )


def make_manifest(
    task: StageTask,
    *,
    status: StageStatus = StageStatus.SUCCEEDED,
    cache_key: str | None = None,
    output: ArtifactRef | None = None,
    models: tuple[ModelExecution, ...] | None = None,
) -> StageManifest:
    result = StageResult(
        run_id=task.run_id,
        stage_run_id=task.stage_run_id,
        attempt=task.attempt,
        status=status,
        outputs={"transcript": output or make_artifact("transcript")},
        models=(expected_model(),) if models is None else models,
    )
    return StageManifest(
        task=task,
        result=result,
        started_at="2026-08-06T12:00:00Z",
        completed_at="2026-08-06T12:00:01Z",
        cache_key=compute_stage_cache_key(task) if cache_key is None else cache_key,
    )


class FakeArtifactStore:
    def __init__(self) -> None:
        self.outcomes: dict[str, ArtifactVerification | Exception] = {}
        self.verified: list[str] = []

    def verify(self, artifact: ArtifactRef) -> ArtifactVerification:
        self.verified.append(artifact.artifact_id)
        outcome = self.outcomes.get(artifact.artifact_id)
        if isinstance(outcome, Exception):
            raise outcome
        if outcome is not None:
            return outcome
        return ArtifactVerification(
            exists=True,
            expected_size_bytes=artifact.size_bytes,
            actual_size_bytes=artifact.size_bytes,
            expected_checksum=artifact.checksum,
            actual_checksum=artifact.checksum,
        )


def reasons(decision: object) -> set[CacheMissReason]:
    return {miss.reason for miss in decision.misses}


def verification(
    artifact: ArtifactRef,
    *,
    exists: bool = True,
    size_matches: bool = True,
    checksum_matches: bool = True,
) -> ArtifactVerification:
    return ArtifactVerification(
        exists=exists,
        expected_size_bytes=artifact.size_bytes,
        actual_size_bytes=(
            artifact.size_bytes if size_matches and exists else 999
        ),
        expected_checksum=artifact.checksum,
        actual_checksum=(
            artifact.checksum
            if checksum_matches and exists
            else Checksum("sha256", "different")
        ),
    )


def test_cache_key_uses_semantics_but_not_run_local_identity() -> None:
    original = make_task()
    same_content_new_ref = make_artifact("audio", uri_run="run-new")
    another_run = make_task(
        run_id="run-other",
        stage_run_id="stage-other",
        attempt=3,
        trace_id="trace-other",
        input_artifact=same_content_new_ref,
    )

    assert compute_stage_cache_key(original) == compute_stage_cache_key(
        another_run
    )
    assert compute_stage_cache_key(original).startswith("stage-cache-v1:")


@pytest.mark.parametrize(
    "changed",
    [
        make_task(stage_version="2.0.0"),
        make_task(input_artifact=make_artifact("audio", checksum="changed")),
        make_task(config={"language": "en"}),
        make_task(binding="stt.remote"),
    ],
)
def test_cache_key_changes_for_each_result_semantic(
    changed: StageTask,
) -> None:
    assert compute_stage_cache_key(changed) != compute_stage_cache_key(
        make_task()
    )


def test_matching_manifest_and_verified_artifacts_is_a_hit() -> None:
    previous = make_task(run_id="run-old", stage_run_id="stage-old")
    current = make_task()
    store = FakeArtifactStore()
    decision = ManifestCacheEvaluator(store).evaluate(
        current,
        make_manifest(previous),
        expected_models=(expected_model(),),
    )

    assert decision.status is CacheStatus.HIT
    assert decision.hit
    assert set(decision.outputs) == {"transcript"}
    assert store.verified == ["art-audio", "art-transcript"]


def test_stage_without_model_bindings_does_not_require_resolution() -> None:
    task = replace(make_task(), model_bindings={})
    decision = ManifestCacheEvaluator(FakeArtifactStore()).evaluate(
        task,
        make_manifest(task, models=()),
    )

    assert decision.status is CacheStatus.HIT


def test_force_bypasses_manifest_and_artifact_checks() -> None:
    task = make_task()
    store = FakeArtifactStore()
    decision = ManifestCacheEvaluator(store).evaluate(
        task,
        make_manifest(task),
        expected_models=(expected_model(),),
        force=True,
    )

    assert decision.status is CacheStatus.FORCED
    assert reasons(decision) == {CacheMissReason.FORCE_REQUESTED}
    assert store.verified == []


def test_missing_manifest_is_a_classified_miss() -> None:
    decision = ManifestCacheEvaluator(FakeArtifactStore()).evaluate(
        make_task(),
        None,
        expected_models=(expected_model(),),
    )

    assert decision.status is CacheStatus.MISS
    assert reasons(decision) == {CacheMissReason.MANIFEST_NOT_FOUND}


@pytest.mark.parametrize(
    ("current", "expected_reason"),
    [
        (make_task(stage_version="2.0.0"), CacheMissReason.STAGE_VERSION_CHANGED),
        (
            make_task(input_artifact=make_artifact("audio", checksum="new")),
            CacheMissReason.INPUTS_CHANGED,
        ),
        (make_task(config={"language": "en"}), CacheMissReason.CONFIG_CHANGED),
        (
            make_task(binding="stt.remote"),
            CacheMissReason.MODEL_BINDINGS_CHANGED,
        ),
    ],
)
def test_task_semantic_changes_have_specific_miss_reasons(
    current: StageTask,
    expected_reason: CacheMissReason,
) -> None:
    previous = make_task(run_id="run-old", stage_run_id="stage-old")
    decision = ManifestCacheEvaluator(FakeArtifactStore()).evaluate(
        current,
        make_manifest(previous),
        expected_models=(expected_model(),),
    )

    assert expected_reason in reasons(decision)
    assert CacheMissReason.CACHE_KEY_MISMATCH in reasons(decision)


def test_missing_or_modified_cache_key_is_rejected() -> None:
    task = make_task()
    without_key = replace(make_manifest(task), cache_key=None)
    modified_key = replace(make_manifest(task), cache_key="stage-cache-v1:bad")
    evaluator = ManifestCacheEvaluator(FakeArtifactStore())

    missing = evaluator.evaluate(
        task,
        without_key,
        expected_models=(expected_model(),),
    )
    modified = evaluator.evaluate(
        task,
        modified_key,
        expected_models=(expected_model(),),
    )

    assert CacheMissReason.CACHE_KEY_MISSING in reasons(missing)
    assert CacheMissReason.CACHE_KEY_MISMATCH in reasons(modified)


def test_skipped_and_failed_results_are_not_reused() -> None:
    task = make_task()
    evaluator = ManifestCacheEvaluator(FakeArtifactStore())

    skipped = evaluator.evaluate(
        task,
        make_manifest(task, status=StageStatus.SKIPPED),
        expected_models=(expected_model(),),
    )
    failed = evaluator.evaluate(
        task,
        make_manifest(task, status=StageStatus.FAILED),
        expected_models=(expected_model(),),
    )

    assert CacheMissReason.SKIPPED_RECHECK_REQUIRED in reasons(skipped)
    assert CacheMissReason.RESULT_NOT_CACHEABLE in reasons(failed)


def test_effective_model_must_be_resolved_and_unchanged() -> None:
    task = make_task()
    manifest = make_manifest(task)
    evaluator = ManifestCacheEvaluator(FakeArtifactStore())

    unresolved = evaluator.evaluate(task, manifest)
    changed = evaluator.evaluate(
        task,
        manifest,
        expected_models=(expected_model(provider="http.stt"),),
    )

    assert CacheMissReason.EFFECTIVE_MODELS_UNAVAILABLE in reasons(unresolved)
    assert CacheMissReason.EFFECTIVE_MODELS_CHANGED in reasons(changed)


def test_expected_model_slots_must_match_task_bindings() -> None:
    wrong_slot = replace(expected_model(), slot="caption")

    with pytest.raises(EngineInputError, match="slots"):
        ManifestCacheEvaluator(FakeArtifactStore()).evaluate(
            make_task(),
            None,
            expected_models=(wrong_slot,),
        )


@pytest.mark.parametrize(
    ("input_ref", "outcome", "expected_reason"),
    [
        (True, {"exists": False}, CacheMissReason.INPUT_NOT_FOUND),
        (True, {"size_matches": False}, CacheMissReason.INPUT_SIZE_MISMATCH),
        (
            True,
            {"checksum_matches": False},
            CacheMissReason.INPUT_CHECKSUM_MISMATCH,
        ),
        (False, {"exists": False}, CacheMissReason.OUTPUT_NOT_FOUND),
        (False, {"size_matches": False}, CacheMissReason.OUTPUT_SIZE_MISMATCH),
        (
            False,
            {"checksum_matches": False},
            CacheMissReason.OUTPUT_CHECKSUM_MISMATCH,
        ),
    ],
)
def test_artifact_integrity_failure_has_specific_reason(
    input_ref: bool,
    outcome: dict[str, bool],
    expected_reason: CacheMissReason,
) -> None:
    task = make_task()
    manifest = make_manifest(task)
    artifact = (
        task.inputs["audio"]
        if input_ref
        else manifest.result.outputs["transcript"]
    )
    store = FakeArtifactStore()
    store.outcomes[artifact.artifact_id] = verification(artifact, **outcome)

    decision = ManifestCacheEvaluator(store).evaluate(
        task,
        manifest,
        expected_models=(expected_model(),),
    )

    assert expected_reason in reasons(decision)


def test_artifact_verification_exception_is_normalized() -> None:
    task = make_task()
    store = FakeArtifactStore()
    store.outcomes["art-transcript"] = RuntimeError("secret path")

    decision = ManifestCacheEvaluator(store).evaluate(
        task,
        make_manifest(task),
        expected_models=(expected_model(),),
    )

    assert CacheMissReason.OUTPUT_VERIFICATION_FAILED in reasons(decision)
    failure = next(
        miss
        for miss in decision.misses
        if miss.reason is CacheMissReason.OUTPUT_VERIFICATION_FAILED
    )
    assert failure.detail == "error_type=RuntimeError"
