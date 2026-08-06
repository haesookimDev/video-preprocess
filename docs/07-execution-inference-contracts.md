# 실행기·단계·추론 계약

상태: **v1 구현 진행 중**
상위 설계: [`06-target-architecture.md`](./06-target-architecture.md)

이 문서는 구현 언어나 전송 방식보다 오래 유지되어야 하는 논리 계약을 정의한다.
Python Protocol, HTTP OpenAPI, 작업 큐 메시지는 모두 이 계약을 동일하게 표현해야 한다.

현재 `ArtifactRef`, `StageSpec`, `StageTask`, `StageResult`와 manifest 타입은
[`src/video_preprocess/domain/`](../src/video_preprocess/domain/)에 구현되어 있다. 표준
라이브러리 dataclass와 명시적 JSON 직렬화를 사용하는 결정은
[`ADR-0002`](./adr/0002-use-stdlib-dataclasses-for-domain-contracts.md)에 기록한다.
Artifact·Run Store Port와 로컬 구현은
[`src/video_preprocess/storage/`](../src/video_preprocess/storage/)에 있고 저장 규칙은
[`ADR-0003`](./adr/0003-local-artifact-and-manifest-storage.md)에 기록한다. Executor와
나머지 Provider 계약은 아직 설계 상태이며 구현 완료로 간주하지 않는다. Inference 공통 계약,
Gateway, `LocalEmbeddingProvider`와 `LocalCaptionProvider`는
[`src/video_preprocess/inference/`](../src/video_preprocess/inference/)에 구현되어 있고 결정은
[`ADR-0004`](./adr/0004-async-inference-gateway-and-local-embedding-provider.md)와
[`ADR-0005`](./adr/0005-artifact-batched-local-caption-provider.md)에 기록한다.

## 1. 계약 설계 원칙

1. 모든 공개 객체는 `schema_version`을 갖는다.
2. 실행 요청은 재시도해도 안전하도록 idempotent해야 한다.
3. 대용량 데이터는 본문이 아니라 `ArtifactRef`로 전달한다.
4. 모델 alias와 실제 model revision을 구분한다.
5. 오류는 문자열 메시지가 아니라 분류 가능한 code를 갖는다.
6. 취소와 timeout은 CLI뿐 아니라 원격 실행까지 전달된다.
7. provider 고유 응답은 `metadata`에 보존할 수 있지만 공통 필드는 항상 정규화한다.
8. 비밀값은 어떤 계약 객체에도 직렬화하지 않는다.

## 2. 공통 식별자

| 필드 | 의미 | 생성 주체 |
|---|---|---|
| `run_id` | 전체 파이프라인 실행 식별자 | Application Service |
| `stage_run_id` | 한 단계 실행 식별자 | Engine |
| `attempt` | 동일 단계의 시도 번호, 1부터 시작 | Engine |
| `request_id` | 한 추론 요청 식별자 | Inference Gateway |
| `idempotency_key` | 중복 실행 방지 키 | Engine 또는 Gateway |
| `trace_id` | 서비스 간 추적 식별자 | 진입 Adapter |

ID는 로그, 이벤트, 상태 조회, 원격 요청에서 동일하게 전달한다.

## 3. Artifact 계약

### 3.1 ArtifactRef

논리 예시:

```json
{
  "schema_version": "1",
  "artifact_id": "art_01_probe_metadata",
  "kind": "json",
  "uri": "artifact://runs/run_123/01_probe/metadata.json",
  "media_type": "application/json",
  "size_bytes": 15842,
  "checksum": {
    "algorithm": "sha256",
    "value": "..."
  },
  "metadata": {
    "stage": "01_probe"
  }
}
```

규칙:

- `uri`는 소비자가 직접 신뢰해서 임의로 열지 않고 Artifact Store를 통해 해석한다.
- 로컬 구현은 `artifact://`를 실제 경로로 매핑할 수 있다.
- HTTP 서버에 `file:///...` 또는 호스트의 절대 경로를 전달하지 않는다.
- checksum은 cache key와 무결성 검증에 사용한다.
- 임시 업로드와 publish된 산출물을 상태로 구분하며 Engine에는 publish된 참조만 반환한다.

### 3.2 Artifact Store Port

필수 동작:

```text
put(stream, identity, relative_path, metadata) -> PendingArtifact
publish(pending_artifact) -> ArtifactRef
discard(pending_artifact) -> None
open(artifact_ref) -> readable stream
materialize(artifact_ref, workspace) -> local path
exists(artifact_ref) -> bool
verify(artifact_ref) -> verification result
```

삭제는 보존 정책과 연관되므로 초기 공통 Port에 넣지 않고 관리 기능으로 분리한다.
`discard`는 공개 artifact 삭제가 아니라 publish되지 않은 임시 byte 정리만 담당한다.

### 3.3 Local Artifact 규칙

- URI: `artifact://<namespace>/<percent-encoded-relative-path>`
- 물리 경로: 설정된 `root/<relative-path>`
- checksum: SHA-256
- 내부 예약 경로: `_pending/`, `_manifests/`
- publish: root와 같은 파일시스템의 임시 파일을 `os.replace`로 원자적 교체
- 기존 출력: `LegacyOutputAdapter`가 내용을 변경하지 않고 참조와 checksum을 생성

namespace가 다르거나 절대 경로·상위 이동을 포함하는 URI는 처리하지 않는다. `open`은 읽기
성능을 위해 자동 checksum 검사를 하지 않으며, 완료·cache 판정은 `verify`를 명시적으로
호출한다.

## 4. Stage 계약

### 4.1 StageSpec

Stage 등록 시 사용하는 불변 메타데이터다.

```json
{
  "schema_version": "1",
  "name": "06_stt",
  "stage_version": "1.0.0",
  "dependencies": ["04_audio", "05_vad"],
  "required_inputs": ["audio", "vad_segments"],
  "outputs": ["transcript"],
  "model_slots": ["stt"],
  "resource_hints": {
    "cpu": 2,
    "memory_mb": 4096,
    "gpu_optional": true
  }
}
```

`stage_version`은 결과 의미나 캐시 호환성이 바뀔 때 증가시킨다. 단순 내부 정리로 결과와
계약이 동일하면 올리지 않아도 된다.

### 4.2 StageTask

```json
{
  "schema_version": "1",
  "run_id": "run_123",
  "stage_run_id": "stage_456",
  "attempt": 1,
  "stage": "06_stt",
  "stage_version": "1.0.0",
  "inputs": {
    "audio": {"artifact_id": "...", "uri": "artifact://..."},
    "vad_segments": {"artifact_id": "...", "uri": "artifact://..."}
  },
  "config": {
    "language": "ko",
    "merge_gap_sec": 0.5
  },
  "model_bindings": {
    "stt": "stt.default"
  },
  "idempotency_key": "...",
  "trace_id": "..."
}
```

Stage는 `StageTask`에 없는 전역 설정을 몰래 읽지 않는다. 환경변수는 비밀값이나 런타임
인프라 설정에만 사용하고 결과에 영향을 주는 옵션은 task에 명시한다.

### 4.3 StageResult

```json
{
  "schema_version": "1",
  "run_id": "run_123",
  "stage_run_id": "stage_456",
  "attempt": 1,
  "status": "succeeded",
  "outputs": {
    "transcript": {"artifact_id": "...", "uri": "artifact://..."}
  },
  "metrics": {
    "segment_count": 42,
    "elapsed_sec": 13.9
  },
  "models": [
    {
      "slot": "stt",
      "provider": "local",
      "model": "faster-whisper",
      "revision": "small"
    }
  ],
  "warnings": []
}
```

허용 상태:

- `succeeded`: 정상 산출물 생성
- `skipped`: 실행 조건이 성립하지 않아 의도적으로 생략
- `failed`: 재시도 여부와 무관하게 이번 attempt 실패
- `cancelled`: 취소 신호를 처리하고 종료

Engine 내부 상태인 `pending`, `queued`, `running`은 최종 `StageResult`에 사용하지 않는다.

## 5. Executor 계약

Executor는 `StageTask` 실행 위치를 추상화한다.

```python
class Executor(Protocol):
    async def submit(self, task: StageTask) -> ExecutionHandle: ...
    async def status(self, handle: ExecutionHandle) -> ExecutionStatus: ...
    async def result(self, handle: ExecutionHandle) -> StageResult: ...
    async def cancel(self, handle: ExecutionHandle) -> None: ...
```

### 5.1 LocalExecutor

- 초기 버전은 현재 프로세스 또는 제한된 worker pool을 사용한다.
- Stage Runner에 의존 서비스를 주입한다.
- ML 모델 인스턴스를 직접 관리하지 않는다.
- Stage별 timeout과 cancellation token을 전달한다.

### 5.2 RemoteExecutor

- 초기 아키텍처에서는 Port와 fake 구현만 둘 수 있다.
- 실제 구현은 HTTP job API 또는 queue 기반 worker를 사용할 수 있다.
- submit 응답은 작업 완료를 기다리지 않고 handle을 반환한다.
- worker heartbeat와 lease 만료 정책이 필요하다.
- 같은 `idempotency_key`로 중복 제출된 작업은 동일 결과를 반환해야 한다.

### 5.3 실행 상태

```text
pending → queued → running → succeeded
                         ├→ skipped
                         ├→ failed
                         └→ cancelled
```

`failed` 이후 재시도는 같은 `stage_run_id`의 새 attempt 또는 새 `stage_run_id` 중 한 정책을
선택해야 한다. 권장 방식은 논리 stage run은 유지하고 attempt만 증가시키는 것이다.

## 6. 추론 계약

모델마다 입력이 다르므로 공통 envelope와 task별 payload를 조합한다.

### 6.1 InferenceRequest

```json
{
  "schema_version": "1",
  "request_id": "infer_789",
  "idempotency_key": "...",
  "run_id": "run_123",
  "stage_run_id": "stage_456",
  "task": "speech_to_text",
  "model": {
    "alias": "stt.default",
    "name": "faster-whisper",
    "revision": "small"
  },
  "inputs": {
    "audio": {"artifact_id": "...", "uri": "artifact://..."}
  },
  "parameters": {
    "language": "ko",
    "word_timestamps": true,
    "beam_size": 5
  },
  "timeout_sec": 900,
  "trace_id": "..."
}
```

초기 task 종류:

| task | 현재 모델 | 주요 출력 |
|---|---|---|
| `voice_activity_detection` | Silero VAD | 음성 구간 |
| `speech_to_text` | faster-whisper | 전사·언어·신뢰도 |
| `speaker_diarization` | pyannote | 화자 턴 |
| `image_captioning` | BLIP 또는 대체 VLM | 캡션 |
| `text_embedding` | SentenceTransformer | 정규화 벡터 |

VAD는 초기에는 Stage 내부 로컬 연산으로 유지할 수 있지만, 동일한 gateway를 통해 원격화할
수 있도록 task schema를 예약한다.

`inputs`는 `ArtifactRef`와 JSON 값을 함께 허용한다. JSON 배열·객체 안에도 ArtifactRef를
중첩할 수 있으며 직렬화 시 각 참조를 동일한 artifact schema로 표현한다. 오디오·이미지처럼
큰 입력은 반드시 artifact로 전달하고, text embedding의 `texts`처럼 작은 값만 inline JSON을
사용한다.

### 6.2 InferenceResponse

```json
{
  "schema_version": "1",
  "request_id": "infer_789",
  "status": "succeeded",
  "outputs": {
    "transcript": {"artifact_id": "...", "uri": "artifact://..."}
  },
  "model": {
    "provider": "http",
    "name": "faster-whisper",
    "revision": "small",
    "runtime": "ctranslate2"
  },
  "usage": {
    "input_duration_sec": 57.6,
    "batch_size": 1
  },
  "timing": {
    "queue_sec": 0.2,
    "model_load_sec": 0.0,
    "inference_sec": 13.9
  },
  "warnings": []
}
```

Gateway는 response의 model 정보를 StageResult와 manifest로 전달한다.

현재 embedding 응답은 `outputs.vectors`에 정규화된 float 배열을 inline으로 반환하고
`outputs.dimension`, `usage.input_count`, model provider·resolved revision·runtime을 함께
기록한다. 큰 batch의 artifact 출력은 이후 capability로 추가한다.

현재 caption 요청·응답 규칙:

```json
{
  "task": "image_captioning",
  "model": {
    "alias": "caption.default",
    "name": "Salesforce/blip-image-captioning-base",
    "revision": "default"
  },
  "inputs": {
    "images": [
      {"artifact_id": "keyframe_scene_001", "uri": "artifact://..."}
    ]
  },
  "parameters": {"max_new_tokens": 40}
}
```

- `inputs.images`: 1개 이상의 ArtifactRef 배열, 입력 순서 보존
- 지원 media type: `image/jpeg`, `image/png`, `image/webp`
- `max_new_tokens`: 1~512 정수
- `outputs.captions`: 입력과 같은 개수·순서의 비어 있지 않은 문자열 배열
- `usage.input_count`, `usage.batch_size`, effective provider·revision·runtime 기록

## 7. Provider 계약

```python
class InferenceProvider(Protocol):
    async def capabilities(self) -> ProviderCapabilities: ...
    async def infer(self, request: InferenceRequest) -> InferenceResponse: ...
    async def cancel(self, request_id: str) -> None: ...
    async def health(self) -> HealthStatus: ...
```

현재 `InferenceGateway`는 alias binding, capability·batch·중첩 artifact 크기 검증, 전체
timeout, 예외 정규화와 response ID 검증을 구현한다. `embedding.default`와
`caption.default`의 local provider는 lazy model load, process 내 재사용, warmup hook과
idempotent 결과 cache를 제공한다. 동기 CLI/Stage는 task별 동기 Service를 사용하고 async
application은 `embed_async()` 또는 `caption_async()`를 사용한다.

### 7.1 capability 확인

최소 정보:

- 지원 task와 model alias
- 입력 media type
- 최대 artifact 크기와 batch 크기
- word timestamp 등 선택 기능
- 동기·비동기 처리 방식
- contract version
- 현재 서비스 가능 여부

Engine은 모델별 세부 capability를 해석하지 않는다. Gateway가 binding 검증과 provider 선택을
담당하고, 실행 전에 지원하지 않는 조합을 명확하게 거부한다.

### 7.2 HTTP API 권장 형태

짧은 요청과 긴 요청을 같은 비동기 job 모델로 표현한다.

```text
GET    /v1/health
GET    /v1/capabilities
POST   /v1/inference-jobs
GET    /v1/inference-jobs/{request_id}
DELETE /v1/inference-jobs/{request_id}
```

`POST`는 `202 Accepted`와 request ID를 반환할 수 있다. 이미 완료된 동일
`idempotency_key` 요청은 기존 결과를 반환한다.

## 8. 오류 계약과 재시도

공통 오류 객체:

```json
{
  "schema_version": "1",
  "code": "PROVIDER_TIMEOUT",
  "message": "inference did not finish before timeout",
  "retryable": true,
  "details": {},
  "request_id": "infer_789"
}
```

| 오류 code | 예시 | 자동 재시도 |
|---|---|---|
| `INVALID_REQUEST` | 누락 필드, 잘못된 파라미터 | 아니요 |
| `UNSUPPORTED_CAPABILITY` | word timestamp 미지원 | 아니요 |
| `ARTIFACT_NOT_FOUND` | 입력 artifact 누락 | 조건부, 기본 아니요 |
| `ARTIFACT_INTEGRITY_ERROR` | checksum 불일치 | 아니요 |
| `AUTHENTICATION_FAILED` | 잘못된 서버 인증 | 아니요 |
| `MODEL_ACCESS_DENIED` | 게이트 모델 권한 없음 | 아니요 |
| `MODEL_UNAVAILABLE` | 모델 로드 불가 | 조건부 |
| `PROVIDER_RATE_LIMITED` | HTTP 429 | 예, Retry-After 준수 |
| `PROVIDER_TIMEOUT` | deadline 초과 | 예, 횟수 제한 |
| `PROVIDER_UNAVAILABLE` | 연결 실패, 일부 5xx | 예 |
| `INFERENCE_FAILED` | 모델 실행 오류 | 기본 아니요 |
| `CANCELLED` | 사용자 또는 상위 실행 취소 | 아니요 |

재시도는 exponential backoff와 jitter를 사용하고 최대 횟수와 전체 deadline을 모두 제한한다.
현재 Gateway는 한 요청의 capability 확인과 inference를 합친 전체 timeout만 구현한다. retry,
backoff와 circuit breaker는 HTTP Provider 단계에서 추가한다. Local embedding과 caption의
실행 thread는 timeout 후 강제 중단할 수 없어 cancellation 미지원으로 capability에 표시한다.

## 9. Skip과 Fallback

Skip은 성공과 구분되는 정상 상태다.

```json
{
  "status": "skipped",
  "reason_code": "OPTIONAL_CREDENTIAL_MISSING",
  "reason": "HF credential is not configured",
  "recheck": {
    "credential": "HF_TOKEN",
    "provider": "diarization.default"
  }
}
```

`recheck` 조건이 바뀌면 이전 skipped manifest를 cache hit로 취급하지 않는다.

Fallback 정책 예시:

```yaml
models:
  caption:
    primary: caption.remote
    fallback: caption.local
    fallback_on:
      - PROVIDER_TIMEOUT
      - PROVIDER_UNAVAILABLE
```

fallback 결과에는 실제 provider와 모델이 반드시 기록되어야 하며, 인증 실패나 잘못된 입력은
fallback으로 숨기지 않는다.

## 10. Manifest 계약

각 단계 성공 또는 skip 후 다음 정보를 저장한다.

```text
manifest schema version
run/stage identifiers and attempt
stage name and version
input artifact refs and checksums
relevant configuration
requested model bindings
effective providers and model revisions
output artifact refs and checksums
status, reason, warnings
start/end time and metrics
```

manifest는 모든 출력이 publish된 뒤 마지막에 원자적으로 기록한다. manifest만 존재하거나 출력
일부가 누락된 상태는 완료로 간주하지 않는다.

### 10.1 구현된 manifest 타입

- `StageManifest`: `StageTask`, terminal `StageResult`, 시작·종료 시각, 선택적 cache key
- `RunManifest`: run 상태, 입력 artifact, 설정, model binding, Stage attempt 참조
- 모든 시각은 UTC offset이 포함된 ISO 8601 문자열
- run은 `pending`, `running`, `succeeded`, `failed`, `cancelled` 상태 사용

### 10.2 Run Store Port

```text
save_run(run_manifest) -> None
load_run(run_id) -> RunManifest | None
save_stage(stage_manifest) -> None
load_stage(run_id, stage_attempt_ref) -> StageManifest | None
is_stage_complete(run_id, stage_attempt_ref) -> bool
```

`LocalRunStore`는 output을 Artifact Store로 검증한 뒤 Stage manifest를 원자적으로 기록한다.
성공한 run을 저장할 때 참조된 모든 Stage가 `succeeded` 또는 `skipped`이고 output 검증을
통과해야 한다. manifest 이후 artifact가 사라지거나 변조되면 `is_stage_complete`는 false다.

## 11. 버전 호환 정책

- contract 변경은 `schema_version`으로 관리한다.
- 필드 추가는 같은 major version에서 허용하되 소비자는 알 수 없는 필드를 무시한다.
- 필드 삭제·의미 변경은 major version을 증가시킨다.
- HTTP 경로의 `/v1`과 payload `schema_version`을 함께 검증한다.
- model revision과 contract version은 별개다.
- 기존 산출물은 migration adapter를 통해 `v1 legacy`로 읽는다.

## 12. 계약 테스트

모든 Executor와 Provider 구현은 동일한 contract test suite를 통과해야 한다.

필수 시나리오:

1. 정상 요청과 결과 정규화
2. 동일 idempotency key 중복 요청
3. timeout과 제한된 retry
4. 취소 전파
5. 잘못된 artifact와 checksum 불일치
6. capability 불일치
7. provider model revision 기록
8. 비밀값과 로컬 절대 경로 비노출
9. skip 조건 변경 후 재평가
10. 구버전 payload 호환 또는 명확한 거부

실제 모델 품질은 provider 계약 테스트와 분리해 sample/golden 통합 테스트로 검증한다.
