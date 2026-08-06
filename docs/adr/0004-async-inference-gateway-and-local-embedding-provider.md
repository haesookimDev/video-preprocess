# ADR-0004: 비동기 추론 Gateway와 로컬 embedding provider를 도입한다

- 상태: Accepted
- 결정일: 2026-08-06
- 관련 문서:
  [`../07-execution-inference-contracts.md`](../07-execution-inference-contracts.md)

## 배경

기존 `s10_index`와 `query.py`는 `SentenceTransformer`를 직접 import하고 호출할 때마다 모델을
생성한다. 이 구조에서는 같은 Stage를 HTTP 모델 서버로 전환하기 어렵고, 장기 실행 서비스에서
모델을 재사용할 수 없다. 동시에 현재 CLI와 SQLite schema는 동기 방식이므로 전체 runner를
비동기로 바꾸지 않고 점진적으로 연결할 호환 계층이 필요하다.

## 결정

### 공통 추론 계약

- `InferenceRequest`와 `InferenceResponse`는 `schema_version: 1`인 dataclass로 구현한다.
- 모델 요청은 논리 alias, 이름, 요청 revision을 포함하며 응답은 실제 provider, model,
  resolved revision과 runtime을 기록한다.
- 텍스트처럼 작은 값은 inline JSON으로 전달할 수 있고 오디오·이미지 같은 대용량 입력은
  `ArtifactRef`로 전달한다.
- 실패는 `InferenceFailure`와 안정적인 `InferenceErrorCode`로 정규화한다.
- Provider는 capability와 health를 명시한다.

### Gateway와 Provider

- `InferenceProvider` Port는 향후 HTTP job과 취소 전파를 위해 async 메서드를 사용한다.
- `InferenceGateway`는 model alias로 provider를 선택하고 capability, 전체 timeout과 응답
  `request_id`를 검증한다.
- 선언된 batch·artifact 크기 한도를 provider 호출 전에 검사한다.
- 예상하지 못한 provider 예외는 원문이나 비밀값을 노출하지 않고 `PROVIDER_UNAVAILABLE`로
  변환한다.

### 로컬 embedding

- 첫 alias는 `embedding.default`, task는 `text_embedding`이다.
- `SentenceTransformer` import와 생성은 `LocalEmbeddingProvider`의 기본 loader 안에서만
  lazy하게 수행한다.
- 동기 모델 실행은 `asyncio.to_thread`로 async Port 바깥을 막지 않게 한다.
- provider 인스턴스는 모델을 한 번 로드하고 이후 요청에서 재사용한다.
- 기본 factory는 process 내 binding별 service를 최대 8개까지 재사용한다.
- 성공 응답은 idempotency key 기준으로 최대 256개를 보관하며, 같은 key의 다른 입력은
  `INVALID_REQUEST`로 거부한다.
- backend 출력 벡터를 finite float와 동일 dimension으로 검증하고 최종 L2 정규화한다.
- revision 미지정 시 요청 binding은 `default`로 유지하되, 모델 로드 후 Hugging Face config의
  commit hash를 resolved revision으로 기록한다.

### 기존 CLI 호환

- `EmbeddingService.embed()`는 동기 CLI에서 async Gateway를 호출하는 compatibility API다.
- async application은 `embed_async()`를 직접 사용한다.
- `s10_index`와 `query.py`는 이 Service만 사용하고 `sentence_transformers`를 import하지 않는다.
- 기존 `embed_model`, `embed_dim`, embeddings BLOB schema는 유지한다.
- `embed_provider`, `embed_revision`, `embed_runtime` meta 필드를 additive하게 추가한다.

## 고려한 대안

### Provider를 동기 Protocol로 정의

현재 로컬 코드에는 단순하지만 HTTP polling, timeout과 취소를 도입할 때 별도 Port가 필요하다.
공통 경계를 async로 유지하고 기존 CLI만 동기 adapter를 쓰는 편이 확장 경로가 명확해 채택하지
않는다.

### Stage에서 local/HTTP 조건 분기

빠르게 연결할 수 있지만 Stage마다 endpoint, timeout과 오류 변환이 반복된다. alias routing을
Gateway에 집중한다는 ADR-0001 결정과 충돌하므로 채택하지 않는다.

### 벡터를 항상 artifact로 저장

대규모 batch에는 필요할 수 있지만 현재 scene/query embedding은 응답 크기가 작다. v1에서는
inline JSON을 허용하고 provider capability나 크기 정책에 따라 이후 artifact 출력으로 확장한다.

## 결과

긍정적 영향:

- index와 query가 구체적인 ML 라이브러리에서 분리됐다.
- provider binding만 바꿔 향후 HTTP embedding 서버를 연결할 수 있다.
- 장기 실행 프로세스에서 모델과 동일 idempotent 결과를 재사용한다.
- 실제 model commit과 runtime이 index metadata에 남는다.
- 기존 SQLite reader와 CLI 사용법을 유지한다.

비용과 제약:

- 동기 compatibility API는 실행 중 event loop 안에서 사용할 수 없으며 그 경우 `embed_async()`를
  사용해야 한다.
- `asyncio.to_thread` timeout은 caller 대기를 끝내지만 이미 시작한 모델 thread를 강제 종료하지
  못한다. 로컬 embedding provider는 cancellation 미지원으로 capability에 명시한다.
- 서로 다른 CLI 프로세스는 메모리 모델 cache를 공유하지 않는다. API 같은 장기 실행 서비스에서
  provider 인스턴스를 재사용해야 cold load를 줄일 수 있다.
- `default` revision에서 commit hash를 찾지 못하면 resolved revision도 `default`로 남는다.

## 구현 위치

- 계약: `src/video_preprocess/domain/inference.py`
- Provider Port·Gateway: `src/video_preprocess/inference/provider.py`, `gateway.py`
- embedding Service: `src/video_preprocess/inference/embedding.py`
- 로컬 provider: `src/video_preprocess/inference/local/embedding.py`
- 기존 adapter: `src/pipeline/stages/s10_index.py`, `src/query.py`

