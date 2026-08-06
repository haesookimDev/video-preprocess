# ADR-0013: PipelineEngine은 RunJournal로 manifest를 저장하고 같은 run attempt를 재개한다

- 상태: Accepted
- 결정일: 2026-08-06
- 관련 문서:
  [`../07-execution-inference-contracts.md`](../07-execution-inference-contracts.md)

## 배경

PipelineEngine의 상태와 cache evaluator가 각각 메모리와 순수 decision으로만 존재해 실제 재시작
시 이전 결과를 사용할 수 없었다. 기존 `LocalRunStore` 계약을 유지하면서 실행 순서, cache
판정과 durable manifest write의 소유권을 연결해야 한다. 동시에 RunStore를 사용하지 않는 기존
fake/embedding 호출 경로를 깨지 않아야 한다.

## 결정

### 선택적 RunJournal

`PipelineEngine`은 선택적으로 RunStore와 UTC clock을 주입받아 `RunJournal`을 만든다. 저장
순서는 다음과 같다.

1. Executor 제출 전에 `running` RunManifest 저장
2. 각 terminal StageResult를 StageManifest로 저장
3. Stage 참조를 추가한 `running` RunManifest 갱신
4. pipeline 종료 시 `succeeded`, `failed` 또는 `cancelled` RunManifest 저장

StageManifest에는 현재 task의 content-addressed cache key를 항상 기록한다. 실패와 취소도 진단을
위해 저장하지만 cache evaluator는 성공 result만 재사용한다. RunStore 또는 clock 오류는
`EnginePersistenceError`로 드러내며 성공으로 정규화하지 않는다.

RunManifest config는 Stage name을 key로 한 JSON 객체, model binding은 `stage.slot` key로
평탄화해 기존 `Mapping[str, str]` 계약을 유지한다.

### Cache resume

현재 RunStore Port에는 cache key global index가 없다. Engine은 현재 run ID와 deterministic
stage run ID/attempt로 후보 StageManifest를 조회한다. 따라서 이번 구현은 같은 run의 재시작과
반복 실행을 지원하며, 다른 run의 content-addressed 재사용은 후속 cache index가 필요하다.

cache hit이면 다음을 수행한다.

- Executor에 제출하지 않는다.
- 저장 result를 현재 task identity로 다시 묶고 output contract를 검증한다.
- Stage lifecycle을 `pending → cached`로 기록한다.
- output ArtifactRef를 downstream logical artifact map에 합친다.
- 현재 실행의 timestamp와 task로 StageManifest를 다시 저장한다.

`StageResult.status`는 원래 성공 의미를 유지한다. cache 여부는 Engine의 `StageLifecycle.CACHED`,
`StageExecutionRecord.from_cache`와 `CacheDecision`으로 표현한다.

### Effective model과 force

모델 Stage cache hit에는 선택적 async `EffectiveModelResolver`가 현재
provider/model/revision/runtime을 반환해야 한다. 후보가 없거나 force인 경우 불필요한 resolver
호출을 하지 않는다. resolver가 없거나 실패하면 cache miss로 실행하며 pipeline 자체를 실패시키지
않는다.

`force_stages`는 plan 안의 stable Stage name 집합이다. 대상 Stage는 `forced` decision으로 실행하며
downstream은 새 output checksum이 이전과 같으면 독립적으로 cache hit할 수 있다.

## 고려한 대안

### 모든 Engine 실행에 RunStore 필수화

순수 orchestration contract test와 아직 전환하지 않은 compatibility 경로를 불필요하게 깨뜨린다.
주입되지 않은 경우 기존 in-memory 동작을 유지한다.

### cache hit을 `StageStatus.SKIPPED`로 표현

Stage 실행 조건 불성립과 검증된 성공 결과 재사용을 혼동한다. StageResult는 succeeded를 유지하고
Engine lifecycle에 cached를 추가한다.

### RunManifest를 마지막에만 저장

process crash 시 시작 여부와 완료된 Stage를 복구할 수 없다. StageManifest 저장 직후 running
manifest의 참조 목록도 갱신한다.

## 결과

긍정적 영향:

- 동일 run을 재실행할 때 검증된 Stage는 Executor 호출 없이 재개한다.
- run/stage terminal 상태, task, model, artifact와 cache key가 durable manifest에 남는다.
- force와 cache miss reason이 Stage execution record에 보존돼 dry-run/CLI의 기반이 된다.
- 실제 LocalArtifactStore/LocalRunStore 조합으로 cache resume가 검증된다.

비용과 제약:

- 현재 RunStore가 동기 Port라 manifest I/O도 Engine event loop에서 짧게 실행된다.
- cache 후보 범위는 같은 run/stage attempt이며 global cache index는 아직 없다.
- effective model resolver의 concrete composition은 legacy/Application Service 연결 시 필요하다.
- legacy 11개 `run(ctx)` Stage와 기본 CLI는 아직 새 Engine을 사용하지 않는다.

## 구현 위치

- orchestration: `src/video_preprocess/engine/pipeline.py`
- RunStore journal: `src/video_preprocess/engine/persistence.py`
- integration tests: `tests/engine/test_pipeline_persistence.py`
