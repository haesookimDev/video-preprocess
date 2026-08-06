# 개발 상태와 세션 인수인계

- 마지막 갱신: **2026-08-06**
- 현재 단계: **Phase 3 — Pipeline Engine과 LocalExecutor**
- 다음 작업: **PipelineEngine 순차 orchestration과 상태 머신**

이 문서는 개발 진행 상황의 단일 진입점이다. 새로운 세션은 이 문서를 먼저 읽고, 실제 코드와
Git 상태를 확인한 뒤 작업을 시작한다.

## 1. 현재 제품 상태

현재 저장소는 로컬 단일 프로세스 MVP다.

- `src/run_pipeline.py`: 전처리 CLI
- `src/query.py`: 기존 index 검색·context 조립 CLI
- `src/pipeline/runner.py`: 11단계 순차 실행
- `src/pipeline/context.py`: 경로·설정·JSON I/O 공유
- `src/pipeline/stages/s01_*`~`s11_*`: 단계 구현
- `src/video_preprocess/domain/`: 버전이 있는 Artifact·Stage 공개 계약
- `src/video_preprocess/storage/`: Artifact·Run Store Port와 로컬 구현
- `src/video_preprocess/engine/`: 11개 Stage registry와 deterministic DAG planner
- `src/video_preprocess/executors/`: async Executor Port, Stage binding과 순차 LocalExecutor
- 로컬 파일 존재 여부를 기준으로 단계 스킵
- VAD, STT, diarization, caption과 embedding이 `InferenceGateway`와 Local Provider로 실행
- 모든 모델 Stage에서 구체 ML library import와 model lifecycle 제거 완료
- Registry/planner는 구현됐지만 기존 runner에 아직 연결되지 않음
- Pipeline 상태 머신, legacy Stage binding, HTTP provider는 아직 구현되지 않음
- Local Store는 구현됐고 model Stage compatibility adapter에서 media 등록에 사용하며,
  전체 runner 연결은 Engine 전환 시점까지 보류

기존 샘플 산출물은 `output/` 아래에 있으나 생성물이며 Git에 커밋하지 않는다.

## 2. 승인된 목표와 결정

- Engine과 Executor를 분리한다.
- Stage 실행 위치와 모델 추론 위치를 독립적인 확장 축으로 둔다.
- 모델은 alias별 Local 또는 HTTP Provider로 실행할 수 있게 한다.
- 대용량 데이터는 `ArtifactRef`로 전달한다.
- CLI, API와 향후 queue adapter는 동일한 Application Service를 사용한다.
- Big-bang 재작성 대신 기존 출력과 CLI를 유지하며 단계적으로 전환한다.

관련 문서:

- 목표 구조: [`06-target-architecture.md`](./06-target-architecture.md)
- 실행·추론 계약: [`07-execution-inference-contracts.md`](./07-execution-inference-contracts.md)
- 전체 계획: [`08-development-roadmap.md`](./08-development-roadmap.md)
- 구조 결정: [`ADR-0001`](./adr/0001-separate-engine-executor-and-inference-providers.md)
- 계약 구현 결정: [`ADR-0002`](./adr/0002-use-stdlib-dataclasses-for-domain-contracts.md)
- 로컬 저장 결정: [`ADR-0003`](./adr/0003-local-artifact-and-manifest-storage.md)
- 추론·embedding 결정:
  [`ADR-0004`](./adr/0004-async-inference-gateway-and-local-embedding-provider.md)
- caption ArtifactRef batch 결정:
  [`ADR-0005`](./adr/0005-artifact-batched-local-caption-provider.md)
- STT audio ArtifactRef 결정:
  [`ADR-0006`](./adr/0006-audio-artifact-local-stt-provider.md)
- diarization audio ArtifactRef·credential 결정:
  [`ADR-0007`](./adr/0007-audio-artifact-local-diarization-provider.md)
- VAD audio ArtifactRef·option 결정:
  [`ADR-0008`](./adr/0008-audio-artifact-local-vad-provider.md)
- deterministic Stage registry·DAG 결정:
  [`ADR-0009`](./adr/0009-deterministic-stage-registry-and-dag-planner.md)
- async 순차 LocalExecutor 결정:
  [`ADR-0010`](./adr/0010-async-sequential-local-executor.md)

## 3. 완료된 작업

### 기존 MVP

- [x] ffprobe 메타데이터
- [x] 씬 검출
- [x] 중앙 키프레임 추출
- [x] 오디오 정규화와 VAD
- [x] faster-whisper 전사
- [x] pyannote 화자 분리
- [x] BLIP 캡션
- [x] 씬 타임라인 병합
- [x] SQLite FTS5 + embedding index
- [x] RRF 검색과 context 조립
- [x] 전체 context 산출물

### 아키텍처 준비

- [x] 목표 컴포넌트와 의존 방향 문서화
- [x] Stage·Executor·Inference·Artifact 논리 계약 초안
- [x] 10~12주 마이그레이션 로드맵
- [x] ADR-0001 승인 기록
- [x] 세션 인수인계와 문서 갱신 규칙 정의
- [x] 깨끗한 환경의 설치·테스트·preflight 기준선 검증
- [x] Artifact·Stage domain 계약과 직렬화 테스트
- [x] Local Artifact·Run Store와 legacy output adapter
- [x] Inference 공통 계약·Gateway와 local embedding provider
- [x] ArtifactRef batch와 local caption provider·s08 adapter
- [x] audio ArtifactRef와 local STT provider·s06 adapter
- [x] audio ArtifactRef와 local diarization provider·s07 adapter
- [x] audio ArtifactRef와 local VAD provider·s05 adapter
- [x] 11개 Stage registry와 deterministic DAG planner
- [x] Executor Port와 순차 LocalExecutor

## 4. 아직 구현되지 않은 작업

- [x] 누락된 runtime/optional dependency 명세
- [x] pytest와 최소 legacy fixture 및 단위 테스트
- [x] runtime preflight와 `--preflight-only` CLI
- [x] domain 계약 타입
- [x] ArtifactStore와 RunStore
- [x] LocalEmbeddingProvider
- [x] LocalCaptionProvider
- [x] LocalSTTProvider
- [x] Local diarization Provider
- [x] Local VAD Provider
- [x] Stage registry와 DAG planner
- [x] 순차 LocalExecutor
- [ ] PipelineEngine
- [ ] manifest 기반 cache
- [ ] HTTPInferenceProvider와 모델 서버 계약 구현
- [ ] Application Service와 API adapter
- [ ] 타임라인 경계 정합성 개선
- [ ] 한국어 검색과 평가 체계
- [ ] 실제 token budget

문서가 존재한다고 구현된 것으로 간주하지 않는다. 완료 여부는 코드와 자동 테스트를 기준으로
이 체크리스트에서 갱신한다.

## 5. 다음 작업: PipelineEngine 순차 orchestration과 상태 머신 slice

권장 순서:

1. `ExecutionPlan`과 boundary input ArtifactRef를 받아 StageTask를 순서대로 생성하는
   `PipelineEngine` 구현
2. run/stage 상태를 `pending → queued → running → terminal`로 전이하고 잘못된 전이 거부
3. stage별 config/model binding을 명시적으로 주입하고 required input을 logical key로 resolve
4. 성공 output을 다음 Stage input map에 합치고 failed/cancelled에서 후속 제출 중단
5. fake Executor·ArtifactRef로 전체/부분 plan, skip/fail/cancel과 input 전달 contract test 작성
6. RunStore manifest write와 cache 판정은 다음 slice로 분리

LocalExecutor는 실행 위치와 runner lifecycle만 소유하며 plan 순서, dependency artifact 조립과
run 중단 정책은 다음 PipelineEngine이 소유한다. legacy `run(ctx)` binding은 Engine contract가
고정된 뒤 연결한다.

## 6. 알려진 중요 문제

| 우선순위 | 문제 | 영향 |
|---|---|---|
| P0 | 파일 존재만으로 cache hit | 입력·설정·모델 변경 후 stale 결과 재사용 |
| P0 | skipped diarization도 marker 생성 | credential 추가 후 자동 재시도되지 않음 |
| P0 | 씬 50:50 경계에서 전사 중복 가능 | timeline과 검색 내용 왜곡 |
| P1 | 한국어 `unicode61` 정확 일치 의존 | 조사·어미가 다른 키워드 검색 누락 |
| P1 | 별도 query CLI 프로세스는 embedding 모델을 매번 로드 | 프로세스 간 cold query 지연 |
| P1 | 관련도 하한 없음 | 무관 질의도 항상 top-k 반환 |
| P1 | 모든 씬을 context에 포함 | 긴 영상 token budget 초과 |
| P1 | `keyframes_per_scene` 미사용 | 설정과 실제 동작 불일치 |
| P1 | cached Hugging Face 모델도 metadata HEAD 요청 | offline 환경에서 모델 로드 실패 가능 |
| P2 | macOS에서 OpenCV·PyAV FFmpeg dylib 중복 경고 | 환경에 따라 충돌 또는 불안정 가능 |

이 문제는 새 구조에서 해결하되, P0 정확성 문제가 구조 전환을 막으면 Phase 0에서 최소 수정한다.

## 7. 기존 검증 기준선

2026-08-06 Phase 0~Phase 3 LocalExecutor 점검 결과:

- 깨끗한 Python 3.13 임시 venv에 `requirements-dev.txt` 설치 성공
- embedding slice 당시 기존 `.venv`와 깨끗한 venv에서 전체 테스트 85개 성공
- caption slice 기본 테스트 97개 성공
- STT slice 기본 테스트 108개 성공
- diarization slice 기본 테스트 124개 성공
- VAD slice 기본 테스트 135개 성공
- registry/planner slice 기본 테스트 157개 성공
- LocalExecutor slice 기본 테스트 177개 성공
- concurrent submit 직렬화, sync/async runner, idempotent handle, queued/running cancel,
  exception·result type·identity 정규화 contract 확인
- default 11개 DAG가 기존 01~11 순서이며 cycle·unknown dependency·duplicate output·누락
  producer와 exact/from/to/boundary input contract 확인
- VAD asset revision
  `sha256:4cbf549b8326f60f80f2536d9eefeb450a9abe83365a098031c89719f1be17d2` 기록
- `sample.mp4 --force` 오프라인 캐시 실행 `ok`(29.4초), 기존 VAD 3개·14.8초·0.493과
  downstream STT 3개 확인
- VAD slice 이후 query `음성 구간 검출 --topk 2`도 씬 02를 최상위로 반환
- `sample.mp4 --force` 오프라인 캐시 실행 `ok`(28.2초), diarization 화자 1명·턴 3개와
  resolved commit `3533c8cf8e369892e6b79ff1bf80f7b0286a54ee`, downstream 산출물 확인
- diarization slice 이후 query `음성 구간 검출 --topk 2`도 씬 02를 최상위로 반환
- `sample.mp4 --force` 11단계 전체 실행 `ok`(28.5초), STT 3개와 downstream 산출물 확인
- STT slice 이후 query `음성 구간 검출 --topk 2`도 씬 02를 최상위로 반환
- `sample.mp4 --force` 11단계 전체 실행 `ok`(34.6초), caption 3개와 downstream 산출물 확인
- query `음성 구간 검출 --topk 2`가 기존과 동일하게 씬 02를 최상위로 반환
- 깨끗한 venv에서 `--preflight-only`, `pip check` 성공
- 기존 SQLite index 3개 integrity check 성공
- `sample.mp4`: 3개 씬, 3개 STT 세그먼트
- `sample2.mp4`: 3개 씬, 4개 STT 세그먼트
- 기존 query 예제에서 목표 씬 검색 확인
- domain 패키지가 제3자 라이브러리를 import하지 않는 경계 테스트 성공

## 8. 새 세션 시작 체크리스트

- [ ] 저장소 루트의 `AGENTS.md`를 읽었다.
- [ ] 이 `STATUS.md`의 현재 단계와 다음 작업을 읽었다.
- [ ] `git status --short`로 사용자 변경을 확인했다.
- [ ] 목표 구조와 계약 문서에서 현재 작업 관련 부분을 읽었다.
- [ ] 관련 ADR을 확인했다.
- [ ] 코드와 STATUS가 다르면 작업 전에 차이를 정리했다.
- [ ] 이번 세션에서 완료할 하나의 Phase slice를 정했다.

## 9. 세션 종료 체크리스트

- [ ] 구현과 테스트가 완료되었는지 확인했다.
- [ ] 실행한 검증 명령과 결과를 아래 작업 기록에 추가했다.
- [ ] 완료 체크리스트와 현재 Phase를 갱신했다.
- [ ] 바로 다음 작업을 구체적인 파일·테스트 단위로 적었다.
- [ ] 계약 변경이면 `07` 문서를 갱신했다.
- [ ] 구조 변경이면 `06` 문서와 ADR을 갱신했다.
- [ ] 일정 변경이면 `08` 문서를 갱신했다.
- [ ] README의 사용자 명령이나 출력 설명이 여전히 맞는지 확인했다.

## 10. 작업 기록

최신 기록을 위에 추가한다. 긴 구현 설명은 PR이나 ADR에 두고 여기에는 다음 세션이 재개하는 데
필요한 정보만 적는다.

### 2026-08-06 — Phase 3 Executor Port와 순차 LocalExecutor

- 목표: Stage 실행 위치를 Engine에서 분리하고 local/remote가 공유할 async lifecycle 고정
- 완료: `ExecutionHandle`·`ExecutionStatus`·`Executor` Port, `StageBindingRegistry`,
  single-slot `LocalExecutor` 구현
- 주요 결정: submit 즉시 queued handle, sync runner는 thread, async runner는 await, 동일 task
  idempotency, exception/invalid result 정규화, 강제 thread 종료 없는 cancel; ADR-0010
- 검증: 기본 pytest 177개 통과; 동시 submit 순차성, terminal state mapping, sync/async,
  duplicate/idempotency 충돌, queued/running cancel, unknown binding/handle과 identity 검증
- 호환성: 기존 runner/CLI와 legacy Stage는 연결하지 않아 실제 pipeline 동작 변경 없음
- 다음 작업: fake Executor를 사용하는 PipelineEngine 순차 orchestration과 상태 머신

### 2026-08-06 — Phase 3 Stage registry와 DAG planner

- 목표: module 배열에 암묵적인 실행 순서를 명시적·검증 가능한 DAG plan으로 분리
- 완료: `StageRegistry`, `DAGPlanner`, `ExecutionPlan`, current 11개 `StageSpec` registry 구현
- 주요 결정: logical output 단일 owner, external `video`, stable name tie-break, ancestor input
  검증, exact/from/to selector와 `boundary_inputs`; ADR-0009
- 검증: 기본 pytest 157개 통과; 등록 순서 독립성, 11개 기존 순서, duplicate/unknown/cycle,
  producer graph, selector 조합과 engine dependency boundary 확인
- 호환성: 기존 runner와 CLI에는 연결하지 않아 실행 순서·출력·명령 동작 변경 없음
- 다음 작업: Executor Port와 fake Stage binding을 사용하는 순차 LocalExecutor

### 2026-08-06 — Phase 2 local VAD provider / Phase 2 완료

- 목표: WAV를 ArtifactRef로 전달하고 s05에서 faster-whisper decoder·Silero lifecycle 제거
- 완료: `SpeechSegment`·`VADBatch`·`VADService`, `LocalVADProvider`, runner composition과
  s05 compatibility adapter 구현
- 주요 결정: `vad.default`, 단일 16kHz WAV artifact, portable silence/padding option,
  내장 ONNX content hash revision; ADR-0008
- 검증: 기본 pytest 135개 통과; fake backend로 sample→time 정규화·option·1회 load·
  idempotency·artifact/model/decode/inference 오류·warmup 검증; 실제 sample 기존 VAD 3개,
  14.8초, ratio 0.493 재현; 전체 11단계 `ok`(29.4초), query top-1 씬 02 확인
- 호환성: 기존 has_audio/duration/ratio/options/segments와 no-audio skip 구조를 유지하고
  model/provider/revision/runtime만 additive하게 추가
- Phase 결과: VAD·STT·diarization·caption·embedding의 model lifecycle이 모두 Local Provider로
  이동했고 Stage는 구체 ML library를 import하지 않음
- 다음 작업: Phase 3 stage registry와 deterministic DAG planner

### 2026-08-06 — Phase 2 local diarization provider

- 목표: WAV를 ArtifactRef로 전달하고 s07에서 pyannote lifecycle·credential 처리 제거
- 완료: `SpeakerTurn`·`DiarizationBatch`·`DiarizationService`, `LocalDiarizationProvider`,
  runner composition과 s07 compatibility adapter 구현
- 주요 결정: `diarization.default`, 단일 16kHz WAV artifact, credential은 Provider 설정,
  시작 시간순·overlap 허용 speaker turn, snapshot commit 기록; ADR-0007
- 검증: 기본 pytest 124개 통과; fake pipeline으로 turn 정규화·1회 load·idempotency·artifact·
  credential/gate/model/inference 오류·warmup 검증; 실제 sample 화자 1명·턴 3개 생성;
  실제 resolved commit `3533c8cf8e369892e6b79ff1bf80f7b0286a54ee` 기록; 오프라인 캐시
  전체 11단계 `ok`(28.2초), query top-1 씬 02 확인
- 호환성: 기존 available/model/speakers/turns와 skip 구조를 유지하고 provider·revision·runtime만
  additive하게 추가
- 주의사항: WAV를 임시 workspace에 materialize하고 local thread는 timeout 후 강제 중단할 수
  없으며 output tree별 composition은 pipeline cache를 공유하지 않음
- 다음 작업: audio ArtifactRef를 사용하는 LocalVADProvider와 s05 adapter

### 2026-08-06 — Phase 2 local STT provider

- 목표: WAV를 ArtifactRef로 전달하고 s06에서 faster-whisper lifecycle과 audio decode 제거
- 완료: `SpeechChunk`·`TranscriptSegment`·`STTService`, `LocalSTTProvider`, Gateway chunk
  batch 검사, runner composition과 s06 compatibility adapter 구현
- 주요 결정: `stt.default`, 단일 16kHz WAV artifact + ordered VAD chunks, Provider 절대
  시간축 보정, device `auto`·compute type `int8`, snapshot commit 기록; ADR-0006
- 검증: 기본 pytest 108개 통과; fake decoder/model로 시간 보정·언어·model 1회 load·오류·
  warmup 검증; 오프라인 실제 base 모델로 sample segment 3개를 기존 값과 동일하게 생성하고
  commit `ebe41f70d5b6dfa9166e2c581c45c9c0cfc57b66` 기록; sample 전체 11단계
  `ok`(28.5초), query top-1 씬 02 확인
- 호환성: 기존 model/language/chunk/time/text/probability/source ID를 유지하고 provider·revision·
  runtime·language_probability만 additive하게 추가
- 주의사항: 전체 audio를 한 번 decode하고 local thread는 timeout 후 강제 중단할 수 없으며
  output tree별 composition은 model cache를 공유하지 않음
- 다음 작업: audio ArtifactRef를 사용하는 LocalDiarizationProvider와 s07 adapter

### 2026-08-06 — Phase 2 local caption provider

- 목표: keyframe을 경로가 아닌 ArtifactRef로 전달하고 s08에서 BLIP lifecycle 제거
- 완료: 중첩 ArtifactRef inference 계약, Gateway batch·크기 검사, `CaptionService`,
  `LocalCaptionProvider`, runner composition과 s08 compatibility adapter 구현
- 주요 결정: `caption.default`, ordered ArtifactRef batch, 입력 checksum 선검증, lazy BLIP
  processor/model, idempotent result cache, resolved HF commit 기록; ADR-0005
- 검증: 기본 pytest 97개 통과; fake loader로 batch 순서·1회 load·오류·warmup 검증;
  오프라인 실제 BLIP로 sample keyframe 3개 캡션을 기존 문자열과 동일하게 생성하고 commit
  `82a37760796d32b1411fe092ab5d4e227313294b` 기록; sample 전체 11단계 `ok`(34.6초),
  query top-1 씬 02 확인
- 호환성: 기존 `model`, `captions`, scene/timestamp/keyframe/caption 필드를 유지하고
  provider·revision·runtime만 additive하게 추가
- 주의사항: local caption thread는 timeout 후 강제 중단할 수 없고 output tree별 composition은
  서로 model cache를 공유하지 않음
- 다음 작업: audio ArtifactRef를 사용하는 LocalSTTProvider와 s06 adapter

### 2026-08-06 — Phase 2 Inference Gateway·local embedding

- 목표: 모델 실행 위치를 Stage에서 분리하고 첫 local provider로 embedding 이전
- 완료: Inference request/response·capability·health·오류 계약, alias Gateway,
  `LocalEmbeddingProvider`, 동기/비동기 `EmbeddingService`, s10/query adapter 구현
- 주요 결정: async Provider Port, inline text/vector, `embedding.default`, lazy model cache,
  idempotency conflict 거부, total timeout, resolved HF commit 기록; ADR-0004
- 검증: pytest 85개 통과; fake loader로 model 1회 load·결과 cache·오류·timeout 검증;
  오프라인 실제 모델로 임시 index 3개 카드·384차원 생성 및 query top-1 씬 02 확인
- 호환성: 기존 CLI 명령과 embeddings BLOB, `embed_model`, `embed_dim` 유지;
  provider·revision·runtime meta만 additive하게 추가
- 주의사항: Local embedding thread는 timeout 후 강제 중단할 수 없고 별도 CLI process끼리는
  model cache를 공유하지 않음
- 다음 작업: keyframe ArtifactRef를 사용하는 LocalCaptionProvider와 s08 adapter

### 2026-08-06 — Phase 1 Local Artifact·Run Store

- 목표: 기존 출력 경로를 유지하며 artifact 본문과 실행 manifest 저장을 Port로 분리
- 완료: `ArtifactStore`, `RunStore`, `LocalArtifactStore`, `LocalRunStore`, run/stage manifest,
  `LegacyOutputAdapter` 구현
- 주요 결정: `artifact://<namespace>/<relative-path>`, SHA-256, `put`→`publish` 2단계,
  run-level + stage-attempt-level manifest; ADR-0003 기록
- 검증: pytest 63개 통과; 원자적 교체 실패 시 기존 파일 보존, 경로 탈출 거부, checksum 변조,
  누락 output, legacy fixture와 manifest round-trip 테스트
- 호환성: 물리 출력은 기존 `output/<video>/<stage>/...` 구조를 유지하고 내부 관리용
  `_pending/`, `_manifests/`만 예약; 기존 runner는 아직 새 Store를 사용하지 않음
- 다음 작업: Inference 계약·Gateway·fake contract test 후 embedding local provider 이전

### 2026-08-06 — Phase 1 Artifact·Stage 계약

- 목표: Engine과 Executor가 공유할 저장 위치 독립적이고 버전이 있는 계약 확립
- 완료: `Checksum`, `ArtifactRef`, `StageSpec`, `StageTask`, `StageResult`, terminal status,
  model 실행 정보와 계약 validation 예외 구현
- 주요 결정: 공개 domain 타입은 frozen/slotted dataclass와 명시적 `to_dict()`·`from_dict()`를
  사용하며 표준 라이브러리에만 의존함; ADR-0002 기록
- 변경 파일: `src/video_preprocess/domain/`, `tests/domain/`, 계약·로드맵·상태 문서,
  `docs/adr/0002-use-stdlib-dataclasses-for-domain-contracts.md`
- 검증: 기존 `.venv`와 깨끗한 Python 3.13 임시 venv에서 pytest 39개 통과; 깨끗한
  venv의 preflight와 `pip check` 성공
- 호환성: 기존 runner, CLI와 산출물 형식은 변경하지 않았으며 새 계약은 아직 연결되지 않음
- 다음 작업: ArtifactStore·RunStore Protocol, LocalArtifactStore와 LocalRunStore 구현

### 2026-08-06 — Phase 0 runtime preflight

- 목표: 모델 import 전에 로컬 실행 환경의 누락 조건을 진단
- 완료: Python 3.10+, FFmpeg/ffprobe, SQLite FTS5, 필수 모듈, HF credential과
  diarization 선택 모듈 검사; `--preflight-only` 추가; 환경변수 HF_TOKEN 지원
- 변경 파일: `src/pipeline/preflight.py`, `src/run_pipeline.py`,
  `src/pipeline/stages/s07_diarize.py`, `tests/test_preflight.py`
- 검증: pytest 21개 통과; preflight 전체 OK; 임시 출력에서 sample 11단계 status `ok`;
  query `음성 구간 검출`의 top-1이 씬 02임을 확인
- 주의사항: 첫 모델 접근에는 네트워크가 필요했으며, 캐시된 모델도 Hub metadata HEAD를
  시도했다. macOS에서 OpenCV/PyAV FFmpeg dylib 중복 경고가 발생했으나 실행은 성공했다.
- 호환성: 기존 영상 positional CLI는 유지하고 `--preflight-only`에서만 생략 가능
- 다음 작업: 깨끗한 임시 venv 설치 검증 후 Phase 1 domain 계약 타입 착수

### 2026-08-06 — Phase 0 의존성 및 테스트 기준선

- 목표: 구조 변경 전 설치 의존성과 네트워크 없는 기본 회귀 테스트 확립
- 완료: 기본·diarization·개발 requirements 분리, pytest 설정, 단위 테스트 13개,
  legacy metadata/timeline fixture 추가
- 주요 결정: 현재 MVP 호환을 위해 requirements 파일 방식을 유지하고, provider 분리 시
  packaging/extras 구조를 다시 결정
- 변경 파일: `requirements*.txt`, `pyproject.toml`, `tests/`, `README.md`, `AGENTS.md`
- 검증: `.venv/bin/python -m pytest` — 13 passed; `.venv/bin/pip check` — 성공;
  소스와 테스트 23개 구문 검사 성공
- 호환성: 실행 코드와 기존 산출물 형식 변경 없음
- 다음 작업: 표준 라이브러리 기반 runtime preflight와 CLI 연결

### 2026-08-06 — 아키텍처 문서화

- 목표: 엔진·실행기·추론 provider 분리 요구를 지속 가능한 개발 문서로 정리
- 변경: 목표 architecture, contract, roadmap, ADR, session workflow 문서 추가
- 코드 변경: 없음
- 검증: 문서 링크·구조 및 기존 저장소 상태 확인
- 다음 작업: Phase 0의 의존성 명세와 테스트 기반 추가

## 11. 다음 세션 인수인계 템플릿

작업 종료 시 아래 형식으로 최신 작업 기록을 추가한다.

```markdown
### YYYY-MM-DD — 작업 제목

- 목표:
- 완료:
- 미완료/차단:
- 주요 결정:
- 변경 파일:
- 검증 명령과 결과:
- 호환성 또는 migration 주의사항:
- 다음 작업:
```
