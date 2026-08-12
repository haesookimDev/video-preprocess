# ADR-0024: reference inference server는 Provider를 bounded in-memory job runtime으로 공개한다

- 상태: Accepted
- 결정일: 2026-08-12
- 관련 문서:
  [`../openapi/inference-v1.yaml`](../openapi/inference-v1.yaml),
  [`0022-http-inference-v1-job-contract.md`](./0022-http-inference-v1-job-contract.md),
  [`0023-alias-based-inference-deployment-settings.md`](./0023-alias-based-inference-deployment-settings.md)

## 배경

fake server는 client contract 검증에는 충분하지만 실제 모델 backend를 다른 프로세스에서 서비스하는
배포 진입점으로 사용할 수 없다. production adapter는 기존 `InferenceProvider`를 재사용하고 HTTP
handler thread와 async Provider의 event loop 소유권을 명확히 해야 한다. 동시에 Phase 4 범위에서
분산 queue나 별도 database까지 도입하면 Engine/Executor 분리보다 운영 인프라 구현이 앞서게 된다.

## 결정

- `InferenceHTTPService`가 단일 model alias, Gateway, idempotency index와 bounded in-memory job
  registry를 소유한다.
- 모든 Provider coroutine은 전용 background event loop 하나에서 실행한다. stdlib HTTP handler
  thread는 thread-safe future로 capability, health, submit, poll과 cancel 결과를 받는다.
- 새 job은 즉시 queued snapshot을 반환하고 background task가 running과 terminal response를 기록한다.
- 같은 idempotency key와 같은 의미의 요청은 기존 job을 반환하며 다른 입력은 conflict다.
- capacity에 도달하면 오래된 terminal job부터 제거하고 모두 non-terminal이면 429를 반환한다.
- cancel은 Gateway의 cooperative cancel을 먼저 요청하고 server task를 취소한 뒤 terminal cancelled
  response를 저장한다.
- `/capabilities`는 Provider의 optional `effective_model()`을 합쳐 remote Engine cache fingerprint로
  노출한다.
- `serve_inference.py`는 첫 production backend로 `LocalEmbeddingProvider`를 구성한다. 기본 bind는
  loopback이며 bearer token은 환경변수에서만 읽는다.

## 결과

긍정적 영향:

- Local Provider 구현을 복제하지 않고 HTTP 모델 서비스로 실행할 수 있다.
- fake fixture가 아닌 production client/server 코드로 실제 SentenceTransformer E2E를 검증한다.
- handler별 event loop 생성과 모델 중복 로드를 피하고 Provider의 process-local model cache를 유지한다.
- OpenAPI job, error, auth와 effective model 계약을 그대로 사용한다.

비용과 제약:

- job과 idempotency record는 process restart 후 복구되지 않는다.
- 단일 process reference server이며 multi-replica routing, durable queue, admission control과 metric export는
  후속 운영 adapter 범위다.
- Local Provider의 native inference는 cooperative cancellation 후에도 worker thread가 안전한 반환
  경계까지 실행될 수 있다.
- TLS는 server 자체가 아니라 reverse proxy 또는 service mesh가 종료해야 한다.

## 구현 위치

- server adapter: `src/video_preprocess/inference/http/server.py`
- embedding server CLI: `src/serve_inference.py`
- network-free job tests: `tests/inference/test_http_server.py`
- HTTP integration: `tests/inference/test_http_server_integration.py`
- real model E2E: `tests/inference/test_http_server_model.py`
