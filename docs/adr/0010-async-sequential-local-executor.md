# ADR-0010: LocalExecutor는 async handle과 단일 실행 slot을 사용한다

- 상태: Accepted
- 결정일: 2026-08-06
- 후속 변경: Phase 7의 bounded concurrency는
  [`ADR-0029`](./0029-dependency-ready-bounded-local-concurrency.md)에서 확장한다.
- 관련 문서:
  [`../07-execution-inference-contracts.md`](../07-execution-inference-contracts.md)

## 배경

`DAGPlanner`가 실행할 Stage 순서를 만들지만 현재 runner는 Stage 호출 위치와 orchestration을
한 함수에서 함께 소유한다. 향후 remote worker를 추가해도 Engine이 같은 submit/status/result/
cancel 흐름을 사용하려면 먼저 local 실행 위치를 Port 뒤로 옮겨야 한다. 구체 legacy Stage를
바로 연결하기 전에 fake runner로 실행 lifecycle과 오류 경계를 고정할 필요가 있다.

## 결정

### Executor Port

- `submit(StageTask) -> ExecutionHandle`은 완료를 기다리지 않고 즉시 handle을 반환한다.
- `status(handle)`은 `queued`, `running`, `succeeded`, `skipped`, `failed`, `cancelled`와
  `cancel_requested`를 반환한다.
- `result(handle)`은 terminal `StageResult`를 기다린다.
- `cancel(handle)`은 idempotent하며 terminal job에는 영향을 주지 않는다.
- 한 Executor 인스턴스의 job과 handle은 같은 service event loop lifecycle에서 사용한다.

### Stage binding

- `StageSpec` registry는 planning metadata만 보유한다.
- stable Stage name과 injected sync/async runner callable의 연결은 별도
  `StageBindingRegistry`가 소유한다.
- 이를 통해 Executor가 legacy pipeline module이나 모델·저장소 구현을 import하지 않는다.

### Local 실행과 정규화

- `LocalExecutor`는 하나의 async lock으로 current process에서 task를 한 번에 하나씩 실행한다.
- async runner는 직접 await하고 sync runner는 `asyncio.to_thread`에서 호출한다.
- 동일 idempotency key와 동일 `StageTask` 재제출은 같은 handle을 반환한다.
- 같은 key의 다른 task와 같은 `(stage_run_id, attempt)`의 다른 제출은 명시적 충돌이다.
- runner 예외, non-`StageResult`, task와 다른 run/stage/attempt identity는 예외를 caller로
  유출하지 않고 `FAILED` StageResult와 stable reason code로 정규화한다.
- exception message는 result에 포함하지 않고 type만 warning으로 기록한다.

### 취소

- queued job은 runner를 호출하지 않고 즉시 `CANCELLED` 결과가 된다.
- running job에는 cancel 요청을 기록한다. Python thread나 임의 라이브러리 호출을 강제
  종료하지 않으며 runner가 돌아오면 결과를 폐기하고 `CANCELLED`로 완료한다.
- Stage cancellation token과 timeout 전달은 후속 Engine policy slice에서 추가한다.

후속 Phase 3 slice에서 `ExecutionControl`과 thread-safe `CancellationToken`을 Executor submit의
optional context로 추가했다. control-aware runner는 `(task, control)`, 기존 runner는 `(task)`로
호출한다. LocalExecutor cancel은 token을 먼저 설정하지만 sync/native 호출을 강제 종료하지 않는
원래 안전 경계는 유지한다. timeout 판정과 retry는 Engine policy가 소유한다.

## 고려한 대안

### `submit`이 StageResult를 반환할 때까지 대기

구현은 단순하지만 remote job API와 상태 조회·취소 흐름을 표현하지 못한다.

### Stage module을 LocalExecutor가 직접 import

빠르게 legacy runner를 대체할 수 있지만 Executor가 Stage 조립과 `PipelineContext`에 결합한다.
callable injection으로 분리한다.

### Thread를 강제 종료해 running cancel 구현

Python과 native model 호출에서 안전하지 않고 artifact write 중간에 종료될 수 있다. cooperative
token이 추가되기 전에는 결과 폐기 경계만 제공한다.

## 결과

긍정적 영향:

- Engine이 local/remote 위치와 무관한 동일 async lifecycle을 사용할 수 있다.
- 현재 process에서도 동시 submit을 deterministic하게 직렬화한다.
- idempotency와 실패 identity가 Stage 구현 밖에서 일관된다.
- fake Stage runner로 모델·FFmpeg 없이 실행 계약을 검증한다.

비용과 제약:

- job 상태는 메모리에만 있고 process 재시작 복구는 아직 없다.
- sync runner thread는 running cancel 후에도 반환할 때까지 실제 작업을 계속한다.
- worker pool과 병렬 DAG branch는 아직 지원하지 않는다.
- legacy `run(ctx)` binding과 PipelineEngine orchestration은 후속 slice다.

## 구현 위치

- Port contracts: `src/video_preprocess/executors/contracts.py`
- Stage bindings: `src/video_preprocess/executors/bindings.py`
- local implementation: `src/video_preprocess/executors/local.py`
