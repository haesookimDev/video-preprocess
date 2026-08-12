# ADR-0022: HTTP Inference v1은 기존 추론 계약을 비동기 job으로 운반한다

- 상태: Accepted
- 결정일: 2026-08-12
- 관련 문서:
  [`../07-execution-inference-contracts.md`](../07-execution-inference-contracts.md),
  [`../openapi/inference-v1.yaml`](../openapi/inference-v1.yaml)

## 배경

Local Provider와 Gateway는 이미 versioned `InferenceRequest`, `InferenceResponse`, capability,
health와 표준 오류를 사용한다. HTTP Provider를 추가하면서 별도 모델별 payload를 만들면 local/remote
결과 의미와 cache fingerprint가 갈라진다. 또한 긴 STT·diarization 요청을 하나의 blocking HTTP
response로만 표현하면 polling, 취소와 중복 제출 복구가 어렵다.

## 결정

### 전송과 job 수명주기

- HTTP `/v1`은 Python domain과 같은 `schema_version: "1"` request/response를 사용한다.
- `POST /v1/inference-jobs`는 새 job에 `202`, 같은 의미의 기존 idempotent job에 `200`을 반환한다.
- job 상태는 `queued`, `running`, `succeeded`, `failed`, `cancelled`이며 terminal job만 완전한
  `InferenceResponse`를 포함한다.
- `GET` polling은 선택적 `Retry-After`와 `retry_after_sec` hint를 제공한다.
- `DELETE`는 cooperative cancellation 요청이며 반복 호출과 terminal job 취소를 idempotent하게
  처리한다.

### idempotency와 deadline

- `Idempotency-Key` header는 필수이고 body `idempotency_key`와 정확히 같아야 한다.
- 같은 key와 같은 request semantics는 기존 job을 반환하고 다른 semantics는 `409`와
  `INVALID_REQUEST`, `details.reason=IDEMPOTENCY_KEY_CONFLICT`로 거부한다.
- `InferenceRequest.timeout_sec`는 capability 조회, submit, polling과 terminal response 수신을 모두
  포함한 client total budget이다. 서버는 이를 작업 deadline으로 사용할 수 있지만 client도 독립적으로
  deadline을 강제한다.

### artifact와 보안

- v1은 공유 Artifact Store의 publish된 `artifact://` URI만 원격 대용량 입력으로 허용한다.
- `file://`, 호스트 절대 경로, base64 media body와 임의 원격 URL fetch는 금지한다.
- 서버는 URI namespace allowlist와 size/checksum을 검증한다. 제한된 업로드 API는 v1 후속 확장이다.
- bearer credential은 Provider 배포 설정에서 주입하며 request, manifest, 오류 details와 로그에
  직렬화하지 않는다.

### 결과와 오류

- 성공 응답은 effective provider, model, resolved revision과 선택적 runtime을 반드시 포함한다.
- capability는 추론 없이 확정 가능한 alias별 effective model만 `effective_models`에 포함하며,
  미확정 alias는 생략해 cache가 안전하게 miss하도록 한다.
- HTTP 오류 body도 공통 `InferenceFailure`이며 안정적인 code와 retryable 여부를 보존한다.
- 429와 일시적 5xx는 `Retry-After`를 사용할 수 있다. 인증, 권한, 입력, idempotency conflict는
  자동 재시도하지 않는다.
- OpenAPI 3.1 문서를 transport의 기준으로 두고 domain example round-trip과 route/ref 검사를 기본
  contract test에 포함한다.

## 결과

긍정적 영향:

- Stage와 task-specific Service는 local/HTTP에 동일한 request/response validator를 재사용한다.
- 긴 추론도 submit/poll/cancel과 idempotent 복구가 가능하다.
- remote effective model fingerprint가 기존 manifest/cache 흐름에 그대로 연결된다.
- local path와 비밀값이 transport schema에 들어갈 여지를 줄인다.

비용과 제약:

- server와 client가 job retention, polling 간격과 idempotency record를 관리해야 한다.
- v1 원격 artifact는 양쪽이 같은 Artifact Store namespace를 해석할 수 있어야 한다.
- OpenAPI의 task별 `inputs` 의미 검증은 공통 envelope 외에 Provider validator가 계속 담당한다.
- client는 408/429/502/503/504와 transport 실패만 bounded retry하고 `Retry-After`, exponential
  backoff·jitter, 5xx circuit breaker를 적용한다. 구체 값은 배포 설정으로 둔다.
- idempotent 복구 시 서버가 기존 remote request ID를 반환할 수 있으므로 client는 그 ID로 poll/cancel한
  뒤 현재 caller request ID로 terminal response를 재결합한다.
- upload transport는 v1 후속 확장으로 남는다.

## 구현 위치

- OpenAPI: `docs/openapi/inference-v1.yaml`
- Python domain: `src/video_preprocess/domain/inference.py`
- HTTP Provider: `src/video_preprocess/inference/http/provider.py`
- stdlib transport: `src/video_preprocess/inference/http/transport.py`
- contract test: `tests/contracts/test_inference_openapi.py`,
  `tests/contracts/test_fake_inference_server.py`, `tests/inference/test_http_provider.py`
