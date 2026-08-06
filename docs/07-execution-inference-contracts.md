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
[`ADR-0003`](./adr/0003-local-artifact-and-manifest-storage.md)에 기록한다. Stage registry와
DAG planner는 [`src/video_preprocess/engine/`](../src/video_preprocess/engine/)에 구현됐다.
Executor Port와 LocalExecutor는
[`src/video_preprocess/executors/`](../src/video_preprocess/executors/)에 구현됐다. 순차
PipelineEngine과 상태 머신은
[`src/video_preprocess/engine/`](../src/video_preprocess/engine/)에 구현됐다. manifest cache key와
decision, RunStore journal과 같은 run의 cache resume도 구현됐다. 전체 01~11 legacy Stage binding도
구현됐으며 global cache index, 기본 CLI 연결과 HTTP Provider는 아직 구현 완료로 간주하지 않는다.
Inference 공통 계약,
Gateway, `LocalEmbeddingProvider`, `LocalCaptionProvider`, `LocalSTTProvider`,
`LocalDiarizationProvider`와 `LocalVADProvider`는
[`src/video_preprocess/inference/`](../src/video_preprocess/inference/)에 구현되어 있고 결정은
[`ADR-0004`](./adr/0004-async-inference-gateway-and-local-embedding-provider.md)와
[`ADR-0005`](./adr/0005-artifact-batched-local-caption-provider.md),
[`ADR-0006`](./adr/0006-audio-artifact-local-stt-provider.md),
[`ADR-0007`](./adr/0007-audio-artifact-local-diarization-provider.md),
[`ADR-0008`](./adr/0008-audio-artifact-local-vad-provider.md)에 기록한다.

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

### 4.4 Stage registry와 ExecutionPlan

현재 `StageRegistry`는 stable Stage name과 logical output owner를 인덱싱하고 다음 구성을 실행
전에 거부한다.

- duplicate Stage name 또는 logical output
- external input과 Stage output key 충돌
- 등록되지 않은 dependency
- dependency cycle
- producer가 없거나 해당 Stage의 ancestor가 아닌 required input

`DAGPlanner`는 등록 순서와 무관하게 dependency를 우선하고, 동시에 준비된 Stage는 stable
name 사전순으로 정렬한다. 현재 11개 숫자 prefix Stage는 기존 01~11 순서로 plan된다.

선택 규칙:

| 입력 | 선택 집합 |
|---|---|
| selector 없음 | 전체 DAG |
| `stage=X` | X만 |
| `from_stage=X` | X + 모든 descendant |
| `to_stage=X` | 모든 ancestor + X |
| from + to | from descendant와 to ancestor의 교집합 |

`ExecutionPlan.stages`는 topological order의 `StageSpec` 배열이다.
`ExecutionPlan.boundary_inputs`는 선택된 Stage가 요구하지만 plan 내부 Stage가 생성하지 않는
정렬된 logical input key다. Engine/Executor는 실행 전에 이 key에 해당하는 external 또는 기존
artifact가 있는지 확인해야 한다. 결정 근거는
[`ADR-0009`](./adr/0009-deterministic-stage-registry-and-dag-planner.md)에 기록한다.

### 4.5 PipelineEngine orchestration

현재 최소 `PipelineEngine`은 `ExecutionPlan`과 boundary `ArtifactRef` map을 입력받아 다음 계약을
수행한다.

- plan 전체의 boundary input, stage config, model binding과 attempt를 첫 제출 전에 검증한다.
- required input을 logical key로 resolve하고 Stage별 `StageTask`를 순차 생성한다.
- run ID, Stage name, attempt, version, input, config와 model binding으로 deterministic task
  identity와 idempotency key를 만든다. trace ID는 결과 fingerprint에서 제외한다.
- run은 `pending → running → terminal`, 실행 Stage는 `pending → queued → running → terminal`,
  cache hit는 `pending → cached` 전이를 따르며 잘못된 전이를 거부한다.
- `succeeded`와 `skipped` output을 다음 Stage의 artifact map에 합치고 `failed` 또는 `cancelled`면
  후속 제출을 중단한다.
- 선언되지 않은 output을 거부하고 downstream required input이 실제로 없을 때 stable failed
  result로 정규화한다.

RunStore가 주입되면 Engine은 시작, 각 Stage terminal과 run terminal 시점에 manifest를 저장한다.
같은 run/stage attempt의 성공 manifest가 cache 검증을 통과하면 Executor 제출 없이 output을
전달한다. 결정 근거는
[`ADR-0011`](./adr/0011-sequential-pipeline-engine-artifact-orchestration.md)과
[`ADR-0013`](./adr/0013-pipeline-engine-run-journal-and-cache-resume.md)에 기록한다. retry와 global
cache index는 후속 slice다.

### 4.6 Legacy 01~11 compatibility binding

`LegacyStageTaskRunner`는 기존 `run(ctx)` Stage를 LocalExecutor에 연결하는 migration adapter다.
task의 Stage/version, logical input, config와 model binding key를 exact match하고, legacy Stage가
읽는 materialized path의 size/SHA-256을 input ArtifactRef와 비교한 뒤 실행한다. task config는
실행 중에만 run-scoped context에 적용하고 원래 값을 복원한다. 기존 runner의 marker 파일 skip은
사용하지 않는다.

현재 output mapping:

| Stage | outputs |
|---|---|
| `01_probe` | `metadata` JSON |
| `02_scenes` | `scenes` JSON, `scene_stats` CSV |
| `03_keyframes` | `keyframes` JSON, deterministic `keyframe_images` ZIP |
| `04_audio` | `audio` WAV와 `audio_metadata` JSON |

no-audio에서는 `audio`가 `audio_metadata` JSON sentinel과 같은 ArtifactRef를 사용한다. 03의 가변
JPEG sidecar는 정렬·고정 metadata ZIP으로 묶어 manifest에서 누락/변조를 검증한다. 이 계약
추가로 03과 해당 bundle을 required input으로 받는 08의 Stage version은 `1.1.0`이다. 결정 근거는
[`ADR-0014`](./adr/0014-legacy-media-stage-task-bindings.md)에 기록한다.

05~08 model Stage는 task config와 `vad.default`, `stt.default`, `diarization.default`,
`caption.default` binding을 exact match한다. 성공 JSON의 provider/model/revision/runtime을 slot별
`ModelExecution`으로 변환하고 필수 metadata가 없으면 실패한다. no-audio, no-speech, optional
diarization unavailable과 no-keyframe은 sentinel output을 유지한 `skipped` result와 stable reason
code로 반환한다.

08이 실행될 때는 검증된 keyframe ZIP member 집합이 keyframes JSON의 safe path와 정확히 같은지
확인하고 JPEG를 원자적으로 복원한 뒤 caption service를 호출한다. 상세 결정은
[`ADR-0015`](./adr/0015-legacy-model-stage-bindings-and-sidecar-restore.md)에 기록한다.

09~11은 다음 companion output을 marker와 함께 등록한다.

| Stage | outputs |
|---|---|
| `09_timeline` | `timeline` JSON, `timeline_markdown` Markdown |
| `10_index` | `search_index` SQLite DB, `index_summary` JSON |
| `11_context` | `context` Markdown, `context_json` JSON |

10은 `embedding.default`를 exact match하고 `embed_model` config를 task/cache semantics에 포함한다.
성공한 index summary의 embed provider/model/revision/runtime은 `embedding` slot의
`ModelExecution`으로 변환한다. 세 binding 묶음과 전체 11단계 binding은 각각 생성할 수 있고,
전체 registry는 mutable legacy context 보호를 위해 하나의 실행 잠금을 공유한다. 상세 결정은
[`ADR-0016`](./adr/0016-legacy-final-stage-and-pipeline-bindings.md)에 기록한다.

### 4.7 Pipeline Application Service

`PipelineRunRequest`는 local video/output 경로, pipeline 설정, 선택 실행 범위, 선택적 run/trace ID와
forced Stage를 표현한다. Application Service는 같은 `DAGPlanner`로 plan을 만들고 plan에 포함된
Stage의 config/model binding만 Engine에 전달한다. runtime factory는 plan의 `boundary_inputs`를
충족하는 ArtifactRef와 Engine을 제공해야 하며, 하나라도 빠지면 실행 전에 거부한다.

local runtime은 video bytes를 `00_input/`에 원자적으로 publish하고 output root별 stable artifact
namespace를 사용한다. 부분 실행은 명시한 같은 `run_id`의 RunManifest와 StageManifest에서 output을
복구하고 현재 video checksum이 이전 run input과 같으며 artifact integrity가 유효할 때만 boundary로
전달한다. 현재 global cache index와 실행 전 effective model resolver가 없으므로 다른 run의 결과와
model Stage 결과를 무리하게 재사용하지 않는다. 상세 결정은
[`ADR-0017`](./adr/0017-pipeline-application-service-and-local-runtime.md)에 기록한다.

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

현재 구현은 다음 규칙을 사용한다.

- `StageBindingRegistry`로 stable Stage name에 sync/async runner callable을 주입한다.
- `submit`은 `queued` handle을 즉시 반환하고 한 async lock으로 current process에서 순차 실행한다.
- sync runner는 `asyncio.to_thread`, async runner는 현재 event loop에서 실행한다.
- 동일 idempotency key·동일 task는 같은 handle을 반환하고 다른 task면 충돌로 거부한다.
- runner exception, non-StageResult, run/stage/attempt mismatch는 stable reason code를 가진
  `failed` StageResult로 정규화한다.
- queued cancel은 runner를 호출하지 않는다. running cancel은 실제 호출을 강제 중단하지 않고
  반환된 결과를 폐기해 `cancelled`로 완료한다.
- ML 모델 인스턴스, Stage 순서와 retry 정책은 관리하지 않는다.

현재 handle/job은 in-memory이며 같은 service event loop lifecycle에서 사용한다. Engine의
run/stage terminal manifest는 저장되지만 Executor handle status는 저장하지 않는다. Stage별 timeout,
cancellation token과 persisted job status는 후속 slice다. 결정 근거는
[`ADR-0010`](./adr/0010-async-sequential-local-executor.md)에 기록한다.

### 5.2 RemoteExecutor

- 초기 아키텍처에서는 Port와 fake 구현만 둘 수 있다.
- 실제 구현은 HTTP job API 또는 queue 기반 worker를 사용할 수 있다.
- submit 응답은 작업 완료를 기다리지 않고 handle을 반환한다.
- worker heartbeat와 lease 만료 정책이 필요하다.
- 같은 `idempotency_key`로 중복 제출된 작업은 동일 결과를 반환해야 한다.

### 5.3 실행 상태

```text
pending ───────────────────────→ cached
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

VAD를 포함한 모든 현재 모델 task는 동일한 Gateway 계약을 사용한다.

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

현재 STT 요청·응답 규칙:

```json
{
  "task": "speech_to_text",
  "model": {
    "alias": "stt.default",
    "name": "base",
    "revision": "default"
  },
  "inputs": {
    "audio": {"artifact_id": "audio_16k", "uri": "artifact://..."},
    "chunks": [
      {"start_sec": 1.368, "end_sec": 6.6, "source_ids": [1]}
    ]
  },
  "parameters": {
    "language": null,
    "beam_size": 5,
    "sampling_rate": 16000
  }
}
```

- `inputs.audio`: `audio/wav` ArtifactRef 한 개
- `inputs.chunks`: 시간순·비중첩 VAD 구간, `source_ids`는 원본 VAD segment ID
- `outputs.segments`: 절대 시간의 text·log probability·no-speech probability와 source ID
- `outputs.language`, `outputs.language_probability`: 첫 chunk의 감지 결과
- `usage`: audio/speech duration, chunk/segment count
- `timing`: audio decode, model load, inference 시간

현재 VAD 요청·응답 규칙:

```json
{
  "task": "voice_activity_detection",
  "model": {
    "alias": "vad.default",
    "name": "silero-vad-v6",
    "revision": "default"
  },
  "inputs": {
    "audio": {"artifact_id": "audio_16k", "uri": "artifact://..."}
  },
  "parameters": {
    "min_silence_duration_ms": 500,
    "speech_pad_ms": 200,
    "sampling_rate": 16000
  }
}
```

- `inputs.audio`: `audio/wav` ArtifactRef 한 개
- millisecond option은 0~600000 정수, sampling rate는 16000 고정
- `outputs.segments`: 1부터 연속인 ID와 절대 start/end/duration의 시간순·비중첩 배열
- `outputs.total_sec`, `speech_sec`, `speech_ratio`: 오디오·음성 통계
- `usage`: audio duration, sample/segment count
- `timing`: model load, audio decode와 inference 시간

현재 diarization 요청·응답 규칙:

```json
{
  "task": "speaker_diarization",
  "model": {
    "alias": "diarization.default",
    "name": "pyannote/speaker-diarization-community-1",
    "revision": "default"
  },
  "inputs": {
    "audio": {"artifact_id": "audio_16k", "uri": "artifact://..."}
  },
  "parameters": {}
}
```

- `inputs.audio`: `audio/wav` ArtifactRef 한 개
- `outputs.speakers`: 정렬되고 중복 없는 speaker label 배열
- `outputs.turns`: 1부터 연속인 turn ID, 절대 시작·종료 시간과 speaker label
- turn은 시작 시간순이며 overlapping speech 표현을 위해 시간 겹침을 허용
- `usage`: speaker/turn count
- `timing`: model load와 inference 시간
- `HF_TOKEN`은 Provider 설정이며 request·응답·fingerprint에 직렬화하지 않음

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
timeout, 예외 정규화와 response ID 검증을 구현한다. `embedding.default`,
`caption.default`, `stt.default`, `diarization.default`, `vad.default`의 local provider는
lazy model load, process 내 재사용, warmup hook과 idempotent 결과 cache를 제공한다. 동기
CLI/Stage는 task별 동기 Service를 사용하고 async application은 각 Service의 async 메서드를
사용한다.

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
backoff와 circuit breaker는 HTTP Provider 단계에서 추가한다. 현재 Local Provider 실행
thread는 timeout 후 강제 중단할 수 없어 cancellation 미지원으로 capability에 표시한다.

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

### 10.3 Cache key와 decision

현재 `stage-cache-v1:<sha256>` key는 StageTask schema, Stage name/version, input의 내용 식별자
(kind, media type, size, checksum), config와 requested model binding을 canonical JSON으로 hash한다.
run/stage ID, attempt, trace, idempotency key와 artifact 저장 URI는 결과 의미가 아니므로 제외한다.

`ManifestCacheEvaluator`는 다음을 모두 만족할 때만 `hit`을 반환한다.

- manifest task semantics와 저장된 cache key가 현재 task/key와 일치
- result가 `succeeded`
- 현재 기대하는 effective provider/model/revision/runtime과 기록된 `ModelExecution`이 일치
- 현재 input과 저장 output의 존재, size와 checksum을 `ArtifactStore.verify`로 확인

결과는 `hit`, `miss`, `forced`와 stable miss reason 목록이다. 모델 Stage의 현재 effective
fingerprint를 resolve하지 못하면 안전하게 miss 처리한다. 구조화된 recheck fingerprint가 아직
없으므로 모든 `skipped` manifest도 `SKIPPED_RECHECK_REQUIRED` miss다. 상세 결정은
[`ADR-0012`](./adr/0012-content-addressed-manifest-cache-decisions.md)에 기록한다.

PipelineEngine 통합 시 RunStore가 주입되면 running RunManifest, StageManifest, 갱신된 running
RunManifest, terminal RunManifest 순서로 저장한다. cache 후보는 현재 RunStore Port의 조회
범위에 맞춰 같은 run ID와 deterministic stage run ID/attempt에서 찾는다. hit result는 현재 task
identity로 다시 묶고 Engine lifecycle `cached`로 기록한다. `force_stages`는 대상만 실행하며
downstream은 새 checksum이 같으면 독립적으로 hit할 수 있다. 상세 결정은
[`ADR-0013`](./adr/0013-pipeline-engine-run-journal-and-cache-resume.md)에 기록한다.

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
