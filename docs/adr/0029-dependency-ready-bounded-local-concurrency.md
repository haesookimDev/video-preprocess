# ADR-0029: Engine은 dependency-ready set을, LocalExecutor는 bounded capacity를 소유한다

- 상태: Accepted
- 결정일: 2026-08-12
- 관련 문서:
  [`../06-target-architecture.md`](../06-target-architecture.md),
  [`../07-execution-inference-contracts.md`](../07-execution-inference-contracts.md),
  [`../08-development-roadmap.md`](../08-development-roadmap.md)

## 배경

초기 Engine은 deterministic topological plan을 한 Stage씩 소비했고 LocalExecutor도 단일 lock으로
실행했다. 이 구조는 동등성·cache·retry·cancel 계약을 고정하는 데 유효했지만, 서로 독립적인 visual
경로와 audio 경로도 직렬화했다. Executor 용량만 늘리면 Engine이 한 번에 하나만 submit하므로 병렬이
되지 않고, Engine만 여러 Stage를 submit해도 legacy binding의 pipeline-wide lock이 실제 본문을 다시
직렬화했다.

## 결정

### Engine scheduling

- Engine은 plan 안의 dependency가 모두 `succeeded|skipped|cached`로 끝난 Stage를 ready로 판정한다.
- 같은 wave의 ready Stage는 stable plan 순서로 task를 만들고 Executor에 submit한다.
- Stage output은 해당 Stage가 terminal success가 된 뒤 artifact map에 합치며, 모든 필수 dependency가
  완료되기 전에는 join Stage를 submit하지 않는다.
- cache 평가, timeout과 retry는 Stage별 lifecycle 안에서 기존 의미를 유지한다. retry attempt는 다음
  wave를 열기 전에 terminal success가 되어야 한다.
- 한 Stage가 최종 실패하면 새 Stage 제출을 중단하고 실행 중인 형제 Stage에 cooperative cancel을
  전달한 뒤 안전한 terminal 반환까지 기다린다. 실패 run의 원인은 peer cancellation이 아니라 실제
  failed Stage다.
- 외부 run cancellation도 모든 active Stage에 전달하며 pending Stage와 join은 제출하지 않는다.

### 결정적 기록

- 실제 완료 순서는 wall clock과 실행 환경에 따라 달라도 `PipelineRunResult.stages`는 plan 순서,
  같은 Stage 안에서는 attempt 순서로 반환한다.
- RunManifest의 StageAttemptRef도 같은 순서로 정렬한다. 개별 StageManifest의 실제 시작·완료 timestamp는
  그대로 보존한다.
- 공개 API의 기존 단일 `current_stage`는 호환을 위해 plan상 첫 미완료 Stage를 나타낸다. 동시에 실행
  중인 모든 Stage 목록을 의미하지 않는다.

### Local execution capacity

- LocalExecutor는 검증된 `max_concurrency` 크기의 semaphore로 local 실행 용량을 제한한다.
- 기본값은 1로 유지해 기존 명령의 resource 사용과 순차 실행을 보존한다.
- CLI와 reference server의 `--executor-max-concurrency`는 배포 용량 설정이며 Stage config나 cache key에
  포함하지 않는다.
- legacy binding은 Stage별 lock으로 자기 config 적용·복원을 보호한다. 서로 다른 Stage의 config field는
  분리되어 있으므로 pipeline-wide lock을 사용하지 않는다.
- 첫 구현은 CPU/GPU 종류별 quota가 아닌 전체 Stage 수의 단일 상한이다.

## 고려한 대안

### LocalExecutor가 DAG와 join을 판단

실행 위치 구현이 dependency와 artifact 정책을 알아야 하므로 Engine/Executor 책임 분리를 위반한다.

### 모든 Stage를 무제한 submit

모델 메모리와 native tool 수를 제어할 수 없고 local process 안정성을 해친다. 명시적인 semaphore
capacity를 사용한다.

### 완료 순서대로 manifest 공개

성능 관측에는 직관적이지만 동일 입력의 run manifest 순서가 실행 timing에 따라 달라진다. timestamp는
실제 timing을, reference 배열은 deterministic plan을 표현하도록 분리한다.

### 실패 즉시 asyncio task 강제 취소

sync/native runner가 artifact를 쓰는 중에 중단될 수 있다. 기존 cooperative cancellation과 safe return
경계를 유지한다.

## 결과

- visual/audio 분기와 09 이후 10/11 delivery 분기가 설정한 local capacity 안에서 겹칠 수 있다.
- 09 join은 필요한 transcript, diarization과 caption이 모두 준비된 뒤에만 실행된다.
- 기본 concurrency 1에서는 기존 resource 동작이 유지된다.
- concurrency를 높이면 여러 local 모델이 동시에 메모리를 사용할 수 있으므로 운영자가 host 용량에 맞게
  설정해야 한다.
- resource hint 기반 CPU/GPU별 scheduler와 active stage 목록 공개는 후속 확장 범위다.

## 구현 위치

- ready scheduler: `src/video_preprocess/engine/pipeline.py`
- deterministic journal: `src/video_preprocess/engine/persistence.py`
- bounded executor: `src/video_preprocess/executors/local.py`
- legacy binding guard: `src/video_preprocess/adapters/legacy_stages.py`
- local composition: `src/video_preprocess/services/local.py`
