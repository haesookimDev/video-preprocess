# ADR-0011: PipelineEngine은 plan 순서와 logical artifact map을 소유한다

- 상태: Accepted
- 결정일: 2026-08-06
- 후속 변경: Phase 7의 dependency-ready scheduling은
  [`ADR-0029`](./0029-dependency-ready-bounded-local-concurrency.md)에서 확장한다.
- 관련 문서:
  [`../07-execution-inference-contracts.md`](../07-execution-inference-contracts.md)

## 배경

Planner와 Executor를 분리했지만 두 컴포넌트만으로는 StageTask input, config/model binding,
실행 중단과 downstream artifact 전달의 소유자가 없다. 이 책임을 Executor나 Stage binding에
넣으면 실행 위치와 정책이 다시 결합한다. manifest/cache와 legacy Stage를 연결하기 전에 fake
Executor로 검증 가능한 최소 PipelineEngine orchestration을 고정해야 한다.

## 결정

### 입력 검증과 StageTask 조립

- Engine은 `ExecutionPlan`, run/trace ID, logical `ArtifactRef` map, stage별 config/model binding과
  attempt를 입력받는다.
- plan의 모든 `boundary_inputs`가 실행 전에 있어야 한다.
- config/model binding/attempt는 plan에 포함된 Stage만 지정할 수 있다.
- 각 Stage의 model binding key는 `StageSpec.model_slots`와 정확히 일치해야 한다.
- 모든 config와 binding value는 첫 Stage 제출 전에 `StageTask` 계약으로 검증한다.
- required input은 logical key로 현재 artifact map에서 선택하며 host path를 조합하지 않는다.

### Identity와 idempotency

- `stage_run_id`는 run ID와 stable Stage name에서 결정적으로 계산한다.
- idempotency key는 run/stage/attempt/version, input ArtifactRef, config와 model binding의 canonical
  JSON hash다. trace ID는 결과 의미가 아니므로 fingerprint에서 제외한다.
- retry/cache 정책이 추가되기 전에도 같은 입력으로 동일한 StageTask identity를 재현한다.

### 순차 실행과 상태

- Engine은 topological plan 순서대로 한 Stage씩 Executor에 submit하고 terminal result를 기다린다.
- run state는 `pending → running → succeeded|failed|cancelled`다.
- 일반 Stage state는 `pending → queued → running → terminal`이다.
- missing input 또는 submit 실패는 실행 전 인프라 오류이므로 `pending → failed` 단축 전이를
  허용한다. terminal 이후 전이는 거부한다.
- `succeeded`와 `skipped` result output을 artifact map에 합치고 다음 Stage를 계속한다.
- `failed`와 `cancelled`는 후속 Stage를 제출하지 않고 각각 run을 실패·취소로 종료한다.

### 결과 경계

- result identity는 task의 run/stage/attempt와 일치해야 한다.
- StageResult output key는 해당 `StageSpec.outputs`에 선언되어야 한다.
- 성공 Stage가 downstream required output을 반환하지 않으면 실제 consumer Stage를 제출하기
  전에 `MISSING_REQUIRED_INPUT`으로 실패한다.
- Executor submit/result 예외는 type만 warning에 남기고 stable failed reason code로 정규화한다.
- 최종 in-memory result는 Stage별 task/handle/result/transition history와 누적 artifact map을
  포함한다.

## 고려한 대안

### Executor가 다음 Stage input을 조립

Executor가 graph와 artifact policy를 알아야 하므로 실행 위치 분리 원칙을 위반한다.

### Stage가 이전 결과를 전역 Store에서 직접 검색

숨은 dependency가 생기고 partial plan의 boundary requirement를 검증할 수 없다.

### 모든 declared output이 없으면 즉시 실패

현재 optional media/skip 의미를 StageSpec이 아직 표현하지 못한다. 이 slice에서는 undeclared
output을 거부하고, 누락 output은 실제 downstream required input이 될 때 실패한다.

## 결과

긍정적 영향:

- Planner, Executor와 ArtifactRef 계약이 하나의 순차 orchestration으로 연결된다.
- full/partial plan이 같은 Engine 경로를 사용한다.
- config/model/input 변화가 deterministic task identity에 반영된다.
- 실패·취소 후 불필요한 downstream 모델 실행을 막는다.

비용과 제약:

- 상태와 결과는 아직 메모리에만 있고 RunStore manifest를 쓰지 않는다.
- cache hit, retry, timeout과 Engine 외부 cancel API는 아직 없다.
- legacy `run(ctx)` Stage는 StageTask runner로 연결되지 않았다.
- optional/required output을 StageSpec에서 별도로 표현하지 않는다.

## 구현 위치

- orchestration/state: `src/video_preprocess/engine/pipeline.py`
- fake contract tests: `tests/engine/test_pipeline_engine.py`
