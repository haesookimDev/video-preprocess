# 개발 상태와 세션 인수인계

- 마지막 갱신: **2026-08-12**
- 현재 단계: **Phase 3 — Pipeline Engine과 LocalExecutor**
- 다음 작업: **Stage timeout·cancellation token과 retry policy**

이 문서는 개발 진행 상황의 단일 진입점이다. 새로운 세션은 이 문서를 먼저 읽고, 실제 코드와
Git 상태를 확인한 뒤 작업을 시작한다.

## 1. 현재 제품 상태

현재 저장소는 로컬 단일 프로세스 MVP다.

- `src/run_pipeline.py`: 전처리 CLI
- `src/query.py`: 기존 index 검색·context 조립 CLI
- `src/pipeline/runner.py`: 이전 파일 marker 방식의 compatibility runner
- `src/pipeline/context.py`: 경로·설정·JSON I/O 공유
- `src/pipeline/stages/s01_*`~`s11_*`: 단계 구현
- `src/video_preprocess/domain/`: 버전이 있는 Artifact·Stage 공개 계약
- `src/video_preprocess/storage/`: Artifact·Run Store Port와 로컬 구현
- `src/video_preprocess/engine/`: planner, PipelineEngine, manifest cache와 RunStore journal
- `src/video_preprocess/executors/`: async Executor Port, Stage binding과 순차 LocalExecutor
- `src/video_preprocess/adapters/`: legacy 01~11 StageTask compatibility binding
- `src/video_preprocess/services/`: pipeline Application Service와 local composition root
- 기본 CLI는 manifest·checksum·설정·model binding 기반 cache 사용
- VAD, STT, diarization, caption과 embedding이 `InferenceGateway`와 Local Provider로 실행
- 모든 모델 Stage에서 구체 ML library import와 model lifecycle 제거 완료
- 기본 CLI는 Application Service를 통해 새 Engine과 01~11 binding을 실행
- HTTP provider와 API adapter는 아직 미구현
- Local Store가 input copy, 단계 산출물과 run/stage manifest를 관리

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
- 순차 PipelineEngine과 artifact orchestration 결정:
  [`ADR-0011`](./adr/0011-sequential-pipeline-engine-artifact-orchestration.md)
- content-addressed manifest cache 결정:
  [`ADR-0012`](./adr/0012-content-addressed-manifest-cache-decisions.md)
- PipelineEngine RunStore journal과 cache resume 결정:
  [`ADR-0013`](./adr/0013-pipeline-engine-run-journal-and-cache-resume.md)
- legacy media StageTask binding 결정:
  [`ADR-0014`](./adr/0014-legacy-media-stage-task-bindings.md)
- legacy model Stage binding과 sidecar 복원 결정:
  [`ADR-0015`](./adr/0015-legacy-model-stage-bindings-and-sidecar-restore.md)
- legacy final Stage와 전체 pipeline binding 결정:
  [`ADR-0016`](./adr/0016-legacy-final-stage-and-pipeline-bindings.md)
- Application Service와 local runtime composition 결정:
  [`ADR-0017`](./adr/0017-pipeline-application-service-and-local-runtime.md)
- Engine 기반 CLI 전환 결정:
  [`ADR-0018`](./adr/0018-engine-backed-cli-and-local-run-resume.md)
- 안전한 local effective model resolution 결정:
  [`ADR-0019`](./adr/0019-safe-local-effective-model-resolution.md)
- Run Store global cache index 결정:
  [`ADR-0020`](./adr/0020-run-store-global-cache-index.md)

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
- [x] 순차 PipelineEngine과 run/stage 상태 머신
- [x] manifest cache key와 artifact 검증 기반 cache decision
- [x] PipelineEngine RunStore manifest와 같은 run의 cache resume
- [x] legacy 01 probe~04 audio StageTask binding
- [x] legacy 05 VAD~08 caption StageTask/model result binding
- [x] legacy 09 timeline~11 context/index StageTask binding과 11단계 composition
- [x] pipeline Application Service와 local Engine/Store/inference composition root
- [x] 기본 CLI의 Engine 전환과 stage/from/to/force/run-id/basic dry-run
- [x] Engine의 read-only cache preview와 downstream blocked 판정
- [x] Application Service/CLI cache-aware read-only dry-run
- [x] local provider effective model fingerprint resolver
- [x] global cache index와 run 간 content-addressed 재사용
- [x] Executor `ExecutionControl`과 cooperative cancellation token 전달

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
- [x] 최소 PipelineEngine
- [x] manifest cache key와 독립 evaluator
- [x] PipelineEngine manifest persistence와 같은 run의 cache hit 통합
- [x] legacy 01~04 Engine/LocalExecutor compatibility path
- [x] legacy 05~08 Engine/LocalExecutor compatibility path
- [x] legacy 09~11 Engine/LocalExecutor compatibility path
- [x] global cache index와 run 간 cache 재사용
- [ ] HTTPInferenceProvider와 모델 서버 계약 구현
- [x] Pipeline Application Service와 local runtime factory
- [x] Application Service 기반 선택 실행 CLI
- [ ] API adapter
- [ ] 타임라인 경계 정합성 개선
- [ ] 한국어 검색과 평가 체계
- [ ] 실제 token budget

문서가 존재한다고 구현된 것으로 간주하지 않는다. 완료 여부는 코드와 자동 테스트를 기준으로
이 체크리스트에서 갱신한다.

## 5. 다음 작업: cache-aware dry-run과 model fingerprint slice

권장 순서:

1. [x] Engine의 task/cache preview가 실제 실행과 동일한 task identity·입력 전파 규칙을 사용하도록 공개 API 추가
2. [x] local provider의 현재 effective provider/model/revision/runtime을 안전하게 resolve하는 adapter 구현
3. [x] `--dry-run`에 Stage별 `hit/miss/forced/blocked`, stable reason과 예상 실행 여부 출력
4. [x] model 미로드·credential 변경 시 거짓 hit 없이 reason을 표시하는 contract test 추가
5. [x] global cache index와 run 간 content-addressed manifest 조회를 구현

기본 CLI 전환은 완료됐고, `pipeline.runner`는 명시적 compatibility 구현으로만 남아 있다.
dry-run은 read-only Store와 실제 cache evaluator를 사용하며 model fingerprint를 사전 확정할 수
없는 Stage는 `EFFECTIVE_MODELS_UNAVAILABLE` miss로 표시한다.

## 6. 알려진 중요 문제

| 우선순위 | 문제 | 영향 |
|---|---|---|
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

2026-08-06 Phase 0~Phase 3 legacy media binding 점검 결과:

- 깨끗한 Python 3.13 임시 venv에 `requirements-dev.txt` 설치 성공
- embedding slice 당시 기존 `.venv`와 깨끗한 venv에서 전체 테스트 85개 성공
- caption slice 기본 테스트 97개 성공
- STT slice 기본 테스트 108개 성공
- diarization slice 기본 테스트 124개 성공
- VAD slice 기본 테스트 135개 성공
- registry/planner slice 기본 테스트 157개 성공
- LocalExecutor slice 기본 테스트 177개 성공
- PipelineEngine slice 기본 테스트 194개 성공
- manifest cache decision slice 기본 테스트 218개 성공
- Engine manifest/cache integration slice 기본 테스트 228개 성공
- legacy 01~04 binding slice 기본 테스트 235개 성공
- strict input/config 검증, context 복원, 01~04 logical output 등록, no-audio sentinel,
  deterministic keyframe ZIP과 PipelineEngine→LocalExecutor 연결 확인
- 기존 CLI `sample.mp4 --force` 오프라인 전체 11단계 `ok`(29.4초), VAD/STT/diarization/caption과
  downstream 산출물 회귀 없음; query `음성 구간 검출 --topk 2` top-1 씬 02 확인
- legacy 05~08 model binding slice 기본 테스트 240개 성공
- exact alias/config, effective ModelExecution, no-speech/optional skip, keyframe bundle 복원·extra
  member 거부와 Stage별 PipelineEngine exact plan 확인
- legacy 09~11 final binding slice 기본 테스트 243개 성공
- timeline JSON/Markdown, index DB/summary, context Markdown/JSON을 모두 ArtifactRef로 등록하고
  embedding `ModelExecution`, companion output 누락과 metadata 오류 거부 확인
- 단일 shared binding registry와 LocalExecutor로 fake 11단계 default DAG 전체 성공 및 생성 artifact
  integrity 확인
- Application Service/local runtime slice 기본 테스트 248개 성공
- exact stage 설정·binding 필터, ID 생성, boundary 누락 오류, 입력 video 원자적 등록,
  11-stage local composition과 부분 실행의 이전 manifest 요구 확인
- Engine CLI slice 기본 테스트 252개 성공
- stage/from/to/run-id/force-stage/basic dry-run, compatibility summary와 default stable local run ID 확인
- 새 기본 CLI `sample.mp4 --force` 전체 11단계 `ok`; RunManifest `succeeded` 11개, SQLite integrity
  `ok`, VAD/STT/diarization/caption/index/context 회귀 없음
- offline query `음성 구간 검출 --topk 2` top-1 씬 02 확인; offline flag가 없으면 기존 cached
  Hugging Face HEAD 요청 문제가 재현됨
- running/stage/terminal 저장 순서, same-run cache resume, cached lifecycle, effective model miss,
  force/config invalidation, failed/cancelled persistence와 실제 Local Store 재시작 확인
- run-local identity 제외 cache key, 입력/설정/Stage/model 변화, effective model resolution,
  input/output 누락·크기·checksum·verification 오류와 force/skipped miss reason 확인
- full/partial plan, deterministic task identity, boundary/config/model 사전 검증, logical artifact
  전달, skip/fail/cancel, output 계약과 invalid state transition 확인
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

### 2026-08-12 — Phase 3 Executor cooperative cancellation control

- 목표: Engine policy가 deadline과 취소 신호를 Stage runner까지 전달할 비직렬화 실행 문맥 추가
- 완료: `ExecutionControl`, thread-safe `CancellationToken`, control-aware runner 호출과 legacy
  one-argument runner 호환 구현
- 주요 결정: cancel은 queued 실행을 시작하지 않고 running sync/native 호출은 강제 종료하지 않으며
  cooperative token을 먼저 전달한 뒤 반환 결과를 폐기
- 검증: Executor 계약/구현 테스트 22개 통과; queued pre-cancel과 running token 관찰 확인
- 호환성: 기존 `submit(task)`와 `(task)` Stage runner를 그대로 지원하며 control은 manifest 비포함
- 다음 작업: Engine Stage timeout/cancel orchestration과 bounded retry attempts

### 2026-08-12 — Phase 3 Run Store global cache index

- 목표: 같은 Store의 다른 run에서도 content-addressed Stage 결과를 안전하게 재사용
- 완료: RunStore cache 후보 조회 Port, LocalRunStore atomic index, same-run 우선·global 후보 순회와
  current model/artifact 재검증 구현; ADR-0020
- 주요 결정: index는 manifest 위치만 관리하고 hit 정책은 Engine evaluator가 소유; 같은 key의
  model revision별 후보를 모두 확인해 최신 mismatch가 이전 compatible hit을 가리지 않게 함
- 검증: Engine/LocalRunStore 관련 테스트 22개 통과; offline sample 기존 run으로 index 생성 후 새
  `global-cache-check` run의 dry-run과 실제 실행에서 11개 Stage 모두 cache hit·status ok 확인
- 호환성: index 범위는 같은 output Store root이며 기존 manifest는 다음 저장 시 점진적으로 등록
- 다음 작업: Stage timeout, cooperative cancellation token 전달과 bounded retry policy

### 2026-08-12 — Phase 3 safe local effective model resolver

- 목표: model load·network 요청 없이 현재 local deployment fingerprint를 안전하게 cache에 반영
- 완료: Provider optional effective model capability, Gateway→Engine resolver, Hub cache/VAD asset
  read-only probe와 run/preview composition 구현; ADR-0019
- 주요 결정: loaded·immutable·offline snapshot만 resolve하고 online mutable revision, local directory,
  diarization credential 부재는 `None`으로 두어 `EFFECTIVE_MODELS_UNAVAILABLE` miss 유지
- 검증: 기본 pytest 262개 통과; offline sample dry-run에서 01~11 모두 hit, resolver·mutable online·
  offline snapshot·credential 미확정 contract test 확인
- 호환성: model fingerprint 미확정 시 기존 safe miss 동작을 유지하며 Provider inference API는 변경 없음
- 다음 작업: content-addressed global cache index와 다른 run의 manifest 재사용

### 2026-08-12 — Phase 3 cache-aware local/CLI dry-run

- 목표: Engine preview를 Application Service와 기본 CLI에 연결하되 dry-run의 무상태 보장 유지
- 완료: read-only Local Artifact/Run Store, source video fingerprint와 boundary 검사,
  `cache_decisions` Stage 배열과 stable reason JSON 출력 구현
- 주요 결정: 부분 실행 boundary가 없으면 preview 자체를 실패시키지 않고 해당 Stage를
  `REQUIRED_INPUT_UNAVAILABLE` blocked로 표시
- 검증: 관련 Engine/service/CLI/storage 테스트 43개 통과; 실제 sample dry-run에서 01~04 hit,
  model Stage safe miss와 downstream blocked 확인
- 호환성: `cache_decisions`가 문자열 `evaluated_at_runtime`에서 구조화된 배열로 구체화됨;
  dry-run은 output root를 생성하지 않음
- 다음 작업: local provider의 effective model fingerprint resolver와 안전한 미확정 처리

### 2026-08-12 — Phase 3 Engine cache preview API

- 목표: 실제 실행 없이 동일한 task identity와 cache evaluator로 Stage별 재사용 가능성을 판정
- 완료: `PipelineEngine.preview`, `PipelinePreviewResult`, `StagePreviewRecord`와
  `hit/miss/forced/blocked` disposition 구현
- 주요 결정: 검증된 cache hit output만 downstream input으로 전파하고, upstream miss/force 때문에
  새 checksum을 알 수 없는 Stage는 stale manifest로 추정하지 않고 blocked 처리
- 검증: Engine/cache/persistence 테스트 53개 통과; preview가 Executor와 RunStore write를 호출하지
  않고 hit output을 전파하며 miss/force 뒤 의존 Stage를 차단하는지 확인
- 호환성: 기존 `run()`과 cache decision 계약은 변경하지 않고 read-only API만 추가
- 다음 작업: local read-only runtime과 Application Service/CLI `--dry-run` 연결

### 2026-08-06 — Phase 3 Engine 기반 CLI 전환과 선택 실행

- 목표: 기본 CLI를 legacy marker loop에서 Application Service/Engine 경로로 전환
- 완료: stable local/default 및 explicit run ID, stage/from/to, force-stage/force, basic dry-run,
  Engine result 기반 `run_summary.json` compatibility view와 run-scoped logging 구현
- 주요 결정: 기본 run ID는 output workspace hash로 재개 가능, partial boundary는 manifest로만 복구,
  dry-run cache 판정은 아직 과장하지 않고 runtime 평가로 명시; ADR-0018
- 검증: 기본 pytest 252개 통과; 새 CLI sample 전체 11단계 성공, manifest 11개·SQLite integrity ok,
  offline query top-1 씬 02
- 호환성: 기존 output JSON/Markdown/DB/query와 `--force` 의미 유지; input copy와 `_manifests` 추가,
  stage elapsed는 새 summary에서 아직 제공하지 않음
- 다음 작업: 실제 cache evaluator를 재사용하는 dry-run preview와 local effective model resolver

### 2026-08-06 — Phase 3 Pipeline Application Service와 local runtime

- 목표: CLI/API가 공유할 실행 유스케이스와 로컬 배포 composition을 Engine 앞에 추가
- 완료: typed settings/request, plan·ID·boundary 검증 Application Service, video ingest,
  Local Store/RunStore/cache/Executor/11-stage binding/inference composition factory 구현
- 주요 결정: 부분 실행은 같은 run manifest의 검증된 boundary output만 복구하고 video checksum이
  달라지면 거부; input copy는 `00_input/`에 저장; ADR-0017
- 검증: 기본 pytest 248개 통과; exact selection filtering, missing input, local ingest/composition,
  이전 manifest 없는 partial run 거부 확인
- 호환성: 기존 CLI는 아직 legacy runner를 사용하며 새 service는 다음 slice에서 기본 연결
- 다음 작업: CLI adapter, run resume/선택/force/dry-run과 실제 sample 동등성

### 2026-08-06 — Phase 3 legacy 09~11 final Stage와 전체 composition

- 목표: 최종 조립·검색 산출물을 Engine 계약으로 승격하고 11단계 실행 registry 완성
- 완료: 09~11 binding factory, timeline/context companion output, index DB sidecar, embedding
  `ModelExecution`, 하나의 실행 잠금을 공유하는 01~11 composition 구현
- 주요 결정: 사람이 읽는 문서와 DB도 marker JSON과 동등한 manifest output으로 추적하고,
  successful index summary의 effective embedding metadata를 필수화; ADR-0016
- 검증: 기본 pytest 243개 통과; fake 11단계 default DAG 전체 실행, 모든 생성 artifact integrity,
  context config 복원, companion output과 embedding metadata 누락 거부 확인
- 호환성: 기존 output 경로와 `run(ctx)`는 그대로 유지하며 새 기본 CLI 연결은 아직 하지 않음
- 다음 작업: Application Service composition root, Engine 기반 선택 실행 CLI와 실제 sample 동등성

### 2026-08-06 — Phase 3 legacy 05~08 model Stage binding

- 목표: provider-backed legacy Stage 결과를 Engine의 model/skip/artifact 계약으로 승격
- 완료: 05~08 binding factory, exact alias/config, `LegacyStageOutcome`, `ModelExecution` 변환,
  keyframe bundle 안전 복원과 skip reason 정규화 구현
- 주요 결정: successful JSON의 effective model metadata 필수, sentinel artifact를 가진 explicit
  skip, JSON↔ZIP member exact match 후 atomic JPEG restore; ADR-0015
- 검증: 기본 pytest 240개 통과; model output, context config 복원, no-speech/credential skip,
  잘못된 alias/metadata/bundle 거부와 05~08 PipelineEngine exact plan 확인
- 호환성: 기존 service와 JSON 형식을 유지하며 새 Engine result에 models/status만 구조화해 추가
- 다음 작업: 09 timeline~11 context binding과 index DB sidecar, 전체 11-stage registry composition

### 2026-08-06 — Phase 3 legacy 01~04 media StageTask binding

- 목표: 기존 `run(ctx)` media Stage를 Engine/LocalExecutor 계약에 단계적으로 연결
- 완료: strict `LegacyStageTaskRunner`, 01~04 binding factory, logical output registrar,
  deterministic keyframe image ZIP과 no-audio sentinel 구현
- 주요 결정: task input path SHA-256 확인, config exact match·실행 후 복원, marker skip 미사용,
  03/08 Stage version 1.1.0과 `keyframe_images` required contract; ADR-0014
- 검증: 기본 pytest 235개 통과; fake Stage로 01~04 artifact, input 변조, config/model 거부,
  bundle 결정성, Engine→LocalExecutor 전체 흐름 확인; 기존 CLI sample 전체 `ok`(29.4초), query
  top-1 씬 02
- 호환성: 기존 CLI/runner와 기존 JSON/JPEG/WAV는 유지하며 새 Engine 경로에 ZIP 하나만 추가
- 다음 작업: 05 VAD~08 caption model Stage binding, bundle 복원과 ModelExecution 정규화

### 2026-08-06 — Phase 3 PipelineEngine manifest persistence와 cache resume

- 목표: in-memory Engine 상태와 cache decision을 Local Run/Artifact Store의 durable 실행 경로로 연결
- 완료: 선택적 `RunJournal`, UTC clock, `EffectiveModelResolver`, `force_stages`, cached lifecycle과
  `StageExecutionRecord.cache_decision` 구현
- 주요 결정: running→Stage→running→terminal manifest 저장, 같은 run/stage attempt 후보 조회,
  model fingerprint 미확정 시 miss, content-equal downstream 독립 hit, persistence 오류 명시; ADR-0013
- 검증: 기본 pytest 228개 통과; fake Store 저장 순서와 재시작, model/force/config miss,
  failed/cancelled manifest, 실제 LocalArtifactStore/LocalRunStore cache resume 확인
- 호환성: Store 미주입 시 기존 in-memory Engine 동작 유지; legacy runner/CLI는 아직 새 Engine 미사용
- 다음 작업: 기존 11개 `run(ctx)`를 StageTask/StageResult에 연결하는 compatibility binding

### 2026-08-06 — Phase 3 manifest cache key와 decision 계층

- 목표: 파일 존재 기반 skip을 content·config·model·integrity 기반의 설명 가능한 판정으로 교체
- 완료: `stage-cache-v1` canonical key, `ManifestCacheEvaluator`, hit/miss/forced와 stable reason 구현
- 주요 결정: run-local identity와 URI는 key에서 제외, input/output 모두 verify, effective model
  fingerprint 필수, 구조화된 recheck 계약 전까지 skipped는 항상 miss; ADR-0012
- 검증: 기본 pytest 218개 통과; Stage/input/config/binding/model 변화, cache key 누락·변조,
  input/output 존재·size·checksum·verification 오류와 force 정책 확인
- 호환성: 독립 decision 계층만 추가했으며 기존 runner의 파일 존재 skip 동작은 아직 유지
- 다음 작업: PipelineEngine에 RunStore manifest persistence와 cache hit 흐름 통합

### 2026-08-06 — Phase 3 순차 PipelineEngine과 상태 머신

- 목표: Planner와 Executor 사이에서 StageTask 조립, artifact 전달과 실행 중단 정책을 소유
- 완료: `PipelineEngine`, run/stage 상태 머신, Stage별 execution record와 in-memory run result 구현
- 주요 결정: plan 전체 입력 사전 검증, logical key input resolve, deterministic task identity,
  succeeded/skipped output 전달, failed/cancelled 후속 제출 중단, output 계약 검증; ADR-0011
- 검증: 기본 pytest 194개 통과; full/partial plan, boundary/config/model/attempt 검증,
  idempotency, skip/fail/cancel, 누락·미선언 output과 invalid transition 확인
- 호환성: RunStore/cache와 legacy runner에는 아직 연결하지 않아 기존 CLI·출력 동작 변경 없음
- 다음 작업: manifest cache key와 ArtifactStore 검증을 포함한 cache decision 계층

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
