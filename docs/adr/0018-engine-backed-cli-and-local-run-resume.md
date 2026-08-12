# ADR-0018: 기본 CLI는 Application Service를 호출하고 local workspace run을 재개한다

- 상태: Accepted
- 결정일: 2026-08-06
- 관련 문서:
  [`../05-pipeline.md`](../05-pipeline.md),
  [`../07-execution-inference-contracts.md`](../07-execution-inference-contracts.md)

## 배경

새 Application Service가 구현됐어도 기본 CLI가 legacy runner를 호출하면 파일 존재 기반 skip,
선택 실행 불가와 구조화되지 않은 상태가 계속 사용자 동작이 된다. 반대로 매 CLI invocation마다
새 run ID를 만들면 global cache가 없는 현재 단계에서 같은 output의 재개가 불가능하고 모든 모델을
다시 실행하게 된다.

## 결정

기본 `run_pipeline.py`는 `PipelineRunRequest`를 만들고 Application Service를 호출한다. CLI가 run ID를
받지 않으면 resolved output workspace path의 SHA-256으로 stable `local_<digest>` ID를 만든다.
`--run-id`로 명시적 run을 선택할 수 있다.

선택 옵션은 planner 의미를 그대로 노출한다.

- `--stage`: 정확히 한 Stage
- `--from-stage`, `--to-stage`: descendant/ancestor 범위
- `--force-stage`: 지정 Stage의 cache 무시
- 기존 `--force`: 선택된 plan 전체의 cache 무시

basic `--dry-run`은 runtime을 조립하거나 Stage를 실행하지 않고 plan, boundary와 force 대상을 JSON으로
출력한다. 현재 Engine에는 sequential task/cache preview API가 없으므로 cache decision은
`evaluated_at_runtime`이라고 명시하고 hit/miss를 추정하지 않는다.

후속 Phase 3 slice에서 Engine의 read-only preview API가 추가됐다. CLI dry-run은 이제
Application Service의 preview 유스케이스를 호출하고 Local Artifact/Run Store를 read-only로 열어
`hit`, `miss`, `forced`, `blocked`, 예상 실행 여부와 stable reason을 출력한다. 입력 영상은 output에
복사하지 않고 동일한 checksum/URI semantics의 참조로 기술한다. 검증된 hit output만 downstream에
전파하므로 새 output을 알 수 없는 Stage는 stale manifest로 추정하지 않는다.

Engine result는 기존 위치의 `run_summary.json`에 status, metrics, logical output URI와 cache 상태를
담은 compatibility view로 저장한다. 상세 상태와 timing의 원본은 `_manifests/`다.

## 결과

긍정적 영향:

- 기본 CLI, 향후 API와 queue adapter가 같은 Application Service를 사용할 수 있다.
- 선택 실행과 force가 DAG·manifest 계약을 따른다.
- 같은 local output workspace를 중단 후 재개할 수 있다.
- dry-run이 실제 planner와 다른 Stage 순서를 보여줄 가능성이 없다.

비용과 제약:

- workspace 기반 ID는 같은 output에 대한 하나의 논리 run을 전제로 하며 동시 실행 lease는 없다.
- basic dry-run은 아직 cache reason을 계산하지 않는다.
- 실행 전 effective model resolver가 없어 model Stage는 불확실할 때 안전한 miss다.
- 새 compatibility summary에는 legacy stage별 elapsed가 없고 timing 원본은 manifest에 있다.

## 구현 위치

- CLI adapter: `src/run_pipeline.py`
- plan use case: `src/video_preprocess/services/pipeline.py`
- local logging/runtime: `src/video_preprocess/services/local.py`
- tests: `tests/test_run_pipeline_cli.py`
