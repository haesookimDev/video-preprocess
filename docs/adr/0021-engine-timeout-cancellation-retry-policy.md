# ADR-0021: Engine은 cooperative timeout·cancel과 분류된 bounded retry를 소유한다

- 상태: Accepted
- 결정일: 2026-08-12
- 관련 문서:
  [`../07-execution-inference-contracts.md`](../07-execution-inference-contracts.md)

## 배경

Executor cancel은 queued/running 상태를 표현했지만 Engine이 Stage deadline이나 run-level 취소를
전달할 방법이 없었다. 또한 모든 실패를 즉시 run 실패로 종료해 일시적인 Executor 전송 오류나
timeout을 제한적으로 재시도할 수 없었다. 반대로 임의의 sync/native model thread를 강제 종료하면
artifact publish 도중 상태가 손상될 수 있다.

## 결정

### 실행 control

- `ExecutionControl`은 manifest에 직렬화하지 않는 attempt별 context로 timeout과
  `CancellationToken`을 가진다.
- Engine은 run-level token을 관찰하고 active attempt token과 `Executor.cancel()`에 신호를 전달한다.
- timeout도 같은 cooperative cancel 경계를 사용한다.
- Engine은 Executor가 terminal result를 반환할 때까지 기다려 background Stage가 artifact를 쓰는
  동안 다음 attempt나 Stage가 겹치지 않게 한다.
- timeout은 `STAGE_TIMEOUT` failed result, 외부 취소는 `ENGINE_CANCELLED` cancelled result로 기록한다.
- token을 무시하는 sync/native 호출은 deadline 뒤에도 safe return까지 wall time이 늘어날 수 있다.

### retry

- `RetryPolicy.max_attempts`는 한 logical Stage의 최초 실행을 포함한 hard limit다.
- 기본 retryable reason은 `EXECUTOR_SUBMIT_FAILED`, `EXECUTOR_RESULT_FAILED`, `STAGE_TIMEOUT`이다.
- 잘못된 입력, 인증/권한, Stage 도메인 실패와 cancellation은 자동 retry하지 않는다.
- retry attempt는 같은 `stage_run_id`와 증가한 `attempt`를 사용해 새 idempotency key를 만든다.
- 각 failed attempt를 먼저 StageManifest와 running RunManifest에 저장한 뒤 backoff하고 다음 attempt를
  제출한다.
- backoff는 bounded exponential이며 run cancellation을 관찰한다. 기본 설정은 기존 동작과 같은
  `max_attempts=1`, timeout 없음이다.

## 결과

긍정적 영향:

- Local/향후 Remote Executor가 동일한 timeout·cancel context를 받는다.
- transient failure를 무한 반복하지 않고 모든 attempt를 감사 가능하게 보존한다.
- unsafe thread termination과 후속 Stage의 artifact race를 피한다.

비용과 제약:

- cooperative token을 지원하지 않는 legacy/native Stage는 timeout 즉시 반환하지 않는다.
- process kill, persisted Executor job과 재시작 후 active cancellation은 아직 지원하지 않는다.
- jitter와 HTTP `Retry-After`는 Phase 4 HTTP Provider 정책에서 추가한다.

## 구현 위치

- execution control: `src/video_preprocess/executors/contracts.py`
- Engine policy: `src/video_preprocess/engine/policies.py`
- orchestration: `src/video_preprocess/engine/pipeline.py`
- Application/CLI config: `src/video_preprocess/services/pipeline.py`, `src/run_pipeline.py`
