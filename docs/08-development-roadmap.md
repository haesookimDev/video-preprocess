# 엔진·실행기 분리 개발 로드맵

상태: **Phase 4 완료 — 다음 Phase 5**
기준일: 2026-08-12
대상 설계: [`06-target-architecture.md`](./06-target-architecture.md)  
공개 계약: [`07-execution-inference-contracts.md`](./07-execution-inference-contracts.md)

## 1. 목표와 일정 가정

1인 개발 기준 10~12주를 기본으로 한다. 1~9주는 아키텍처 MVP, 10~12주는 기존 품질 개선을
새 구조 위에서 진행한다. 실제 일정은 모델 다운로드와 원격 서버 배포 환경에 따라 달라질 수
있으며, 각 Phase의 완료 조건을 통과하기 전 다음 Phase를 시작하지 않는다.

최종 목표:

- 기존 로컬 CLI와 산출물 호환
- Engine과 LocalExecutor 분리
- STT, diarization, caption, embedding의 local/HTTP provider 선택
- manifest 기반 캐시와 단계별 선택 실행
- 다른 서비스가 API 또는 queue adapter로 실행·상태·결과 조회
- 검색·긴 영상 개선이 새 계약을 통해 확장 가능

## 2. 개발 원칙

1. **동작 고정 후 구조 변경**: 기존 sample 결과를 기준으로 회귀를 감지한다.
2. **계약 우선**: 구체 구현 전에 타입, 오류, 버전과 ownership을 확정한다.
3. **로컬 동등성 우선**: HTTP나 원격 실행 전에 Local 구현으로 현재 기능을 재현한다.
4. **단계적 전환**: 한 모델·한 단계씩 옮기고 매 단계 통합 테스트를 통과한다.
5. **호환성 명시**: 호환을 깨야 하면 migration과 ADR을 먼저 작성한다.
6. **관측 가능한 실패**: fallback이나 skip을 성공처럼 숨기지 않는다.
7. **문서 동반 변경**: 완료한 작업은 같은 PR에서 `STATUS.md`와 관련 설계를 갱신한다.

## 3. 전체 Phase

| Phase | 예상 | 핵심 결과 | 릴리스 기준 |
|---|---:|---|---|
| 0. 기준선 고정 | 1주 | 재현 가능한 설치와 golden 테스트 | `v0.2.0` |
| 1. 계약·스토리지 | 1주 | domain 계약, Local stores | 내부 마일스톤 |
| 2. 로컬 Provider | 2주 | 모델 직접 로드 제거 | 내부 마일스톤 |
| 3. Engine·LocalExecutor | 2주 | DAG, 상태, 캐시, 선택 실행 | `v0.3.0` |
| 4. HTTP 추론 | 2주 | HTTP provider와 예제 서버 | `v0.4.0-rc1` |
| 5. 서비스 연동 | 1주 | API, 상태·결과 조회 | `v0.4.0` |
| 6. 검색·긴 영상 | 2주 | 검색 평가와 토큰 예산 | `v0.5.0` |
| 7. 성능·멀티모달 | 1주+ | 병렬화, 적응형 처리 | 후속 릴리스 |

RemoteExecutor의 실제 구현은 `v1.0` 후보로 둔다. `v0.3`에서는 Port와 fake/contract test를
만들고 LocalExecutor를 운영 기준으로 사용한다. 모델의 HTTP 원격 실행은 RemoteExecutor 없이
HTTP Inference Provider로 지원한다.

## 4. Phase 0: 기준선 고정

### 목적

리팩터링 과정에서 알고리즘 회귀와 구조 변경을 구분할 수 있는 기준을 만든다.

### 작업

- 직접 사용하는 의존성을 모두 명시한다.
- 기본·선택·개발 의존성 분리 방식을 결정한다.
- Python과 주요 라이브러리 지원 버전을 정한다.
- `pytest`와 테스트 디렉터리를 추가한다.
- 순수 함수 단위 테스트를 작성한다.
  - VAD 구간 병합
  - 화자 겹침 계산
  - 타임라인 배정
  - FTS query 생성
  - RRF
  - context 시간 포맷
- `samples/sample.mp4`, `samples/sample2.mp4` 통합 테스트 경로를 정의한다.
- 비결정적 ML 텍스트는 완전 문자열 비교 대신 구조·구간·필수 필드와 허용 오차로 검증한다.
- 기존 JSON 산출물을 `tests/fixtures/legacy_v1/`의 최소 fixture로 정리한다.
- preflight 검사를 추가한다.
  - Python version
  - FFmpeg/ffprobe
  - SQLite FTS5
  - 선택 provider의 dependency·credential

### 완료 조건

- 새 가상환경에서 문서의 설치 절차가 성공한다.
- ML 모델 다운로드 없이 순수 단위 테스트를 실행할 수 있다.
- sample 통합 테스트가 명시적 marker로 분리되어 필요할 때 실행된다.
- 현재 출력 JSON을 읽는 fixture 테스트가 존재한다.
- `STATUS.md`에 실제 실행 명령과 결과가 기록된다.

### 권장 PR

1. `build: declare runtime and optional dependencies`
2. `test: add unit and legacy fixture coverage`
3. `feat: add runtime preflight checks`

## 5. Phase 1: Domain 계약과 저장소 Port

### 목적

현재 `PipelineContext`에 결합된 경로·설정·저장을 분리한다.

### 작업

- [x] `ArtifactRef`, `StageSpec`, `StageTask`, `StageResult` 타입 구현
- [x] 계약 validation 오류와 지원하지 않는 schema version 예외 구현
- [x] 명시적 JSON 직렬화 round-trip과 domain dependency 경계 테스트
- [x] `ArtifactStore`와 `RunStore` Protocol 구현
- [x] 현재 출력 구조를 유지하는 `LocalArtifactStore` 구현
- [x] JSON manifest를 사용하는 `LocalRunStore` 구현
- [x] 임시 artifact → publish 원자적 쓰기 구현
- [x] schema version과 stage version 규칙 구현
- [x] 기존 JSON을 읽는 legacy adapter 구현
- [x] 직렬화 round-trip 및 checksum 테스트

### 결정이 필요한 항목

- 공개 계약은 표준 라이브러리 dataclass로 구현한다.
  ([`ADR-0002`](./adr/0002-use-stdlib-dataclasses-for-domain-contracts.md))
- checksum은 SHA-256이며 artifact write와 같은 streaming pass에서 계산한다.
- manifest는 run-level + stage-attempt-level로 분리한다.
- URI는 `artifact://<namespace>/<relative-path>`를 local root 아래 동일 경로로 매핑한다.
  ([`ADR-0003`](./adr/0003-local-artifact-and-manifest-storage.md))

### 완료 조건

- domain 패키지가 ML·HTTP·FFmpeg 라이브러리를 import하지 않는다.
- 기존 output 구조에 새 Artifact Store로 동일한 산출물을 쓸 수 있다.
- manifest가 마지막에 기록되고 불완전 출력은 완료로 판정되지 않는다.
- 구버전 산출물 fixture를 새 reader가 읽는다.

## 6. Phase 2: Local Inference Provider

### 목적

Stage에서 모델 lifecycle과 구체 라이브러리 의존성을 제거한다.

### 순서

위험이 낮고 검증하기 쉬운 순서로 이동한다.

1. embedding provider
2. caption provider
3. STT provider
4. diarization provider
5. VAD provider

### 공통 작업

- [x] `InferenceRequest`, `InferenceResponse`, capability 타입 구현
- [x] `InferenceProvider` Protocol과 Gateway 구현
- [x] model alias → provider binding 설정 구현
- [x] embedding provider의 model instance cache와 lazy load 구현
- [x] embedding provider warmup service hook 구현
- [x] caption provider의 processor/model cache, batch와 warmup hook 구현
- [x] STT provider의 model cache, audio decoder와 warmup hook 구현
- [x] diarization provider의 pipeline cache, credential 설정과 warmup hook 구현
- [x] VAD provider의 Silero backend cache, audio decoder와 warmup hook 구현
- [x] device·compute type 설정 검증
- [x] embedding의 resolved model revision과 runtime metadata 기록
- [x] caption의 resolved model revision과 runtime metadata 기록
- [x] STT의 resolved model revision과 runtime metadata 기록
- [x] diarization의 resolved model revision과 runtime metadata 기록
- [x] VAD asset SHA-256 revision과 runtime metadata 기록
- [x] Gateway timeout과 local embedding cancellation 경계 정의
- [x] Gateway·local embedding provider contract test 작성
- [x] 중첩 ArtifactRef와 local caption provider contract test 작성
- [x] audio ArtifactRef와 local STT provider contract test 작성
- [x] audio ArtifactRef와 local diarization provider contract test 작성
- [x] audio ArtifactRef와 local VAD provider contract test 작성

### Stage 변경

- [x] `s05_vad`: faster-whisper VAD·decoder 제거, VAD request와 응답 조립만 담당
- [x] `s06_stt`: `WhisperModel` 생성 제거, STT request 생성과 응답 정규화만 담당
- [x] `s07_diarize`: token·모델 로드를 provider로 이동
- [x] `s08_captions`: BLIP processor/model과 배치 추론을 provider로 이동
- [x] `s10_index`, query: SentenceTransformer 로드를 embedding provider로 이동
- Stage가 provider 종류를 조건문으로 분기하지 않도록 한다.

### 완료 조건

- 해당 Stage에서 ML 라이브러리 직접 import가 제거된다.
- 모델이 같은 프로세스의 반복 요청에서 재사용된다.
- 모든 LocalProvider가 공통 contract test를 통과한다.
- sample 결과의 구조와 의미가 기존 baseline과 호환된다.
- 실제 model revision이 manifest에 기록된다.

Phase 2는 2026-08-06에 VAD까지 Local Provider로 이동하고 sample 회귀를 통과해 완료했다.

## 7. Phase 3: Pipeline Engine과 LocalExecutor

### 목적

현재 순차 runner를 명시적 DAG·상태 머신·캐시 기반 Engine으로 교체한다.

### 작업

- [x] stage registry와 DAG planner 구현
- [x] 의존성 cycle과 누락된 input 검증
- [x] exact/from/to 선택과 boundary input plan 구현
- [x] run/stage 상태 머신 구현
- [x] Executor Port와 순차 LocalExecutor 구현
- [x] PipelineEngine 순차 StageTask 생성과 logical artifact orchestration 구현
- [x] stage timeout과 cancellation token 전달
- [x] manifest cache key 계산과 artifact integrity 기반 decision 구현
- [x] cache miss 사유 기록
- [x] skipped 결과의 안전한 재평가 기본 정책 구현
- [x] RunStore manifest persistence와 같은 run의 PipelineEngine cache hit 통합
- [x] local provider effective model fingerprint resolver
- [x] legacy 01 probe~04 audio StageTask compatibility binding 구현
- [x] keyframe sidecar의 deterministic artifact bundle 계약 구현
- [x] legacy 05 VAD~08 caption StageTask/model result binding 구현
- [x] legacy 09~11 StageTask compatibility binding 구현
- [x] 하나의 registry에서 11개 legacy Stage 전체 composition 구현
- [x] Pipeline Application Service와 local runtime composition 구현
- [x] global cache key index와 run 간 재사용
- [x] retry policy 구현
- [x] Application Service 기반 선택 실행 CLI 구현
- [x] cache decision reason을 포함하는 dry-run preview 구현
  - [x] Engine read-only preview와 `hit/miss/forced/blocked` 판정
  - [x] local/Application Service/CLI 연결

```text
--stage 06_stt
--from-stage 06_stt
--to-stage 09_timeline
--force-stage 07_diarize
--dry-run
```

- 비주얼·오디오 병렬화는 먼저 인터페이스만 준비하고 초기 버전은 순차 실행한다.
- 기존 runner를 compatibility adapter로 유지한 뒤 동등성이 확인되면 제거한다.

### 필수 캐시 테스트

- 입력 내용 변경
- scene threshold 변경
- Whisper model 또는 language 변경
- provider 또는 model revision 변경
- 상위 artifact checksum 변경
- output 일부 삭제
- skipped diarization 이후 credential 추가
- stage schema version 증가

### 완료 조건

- 현재 11단계가 새 Engine과 LocalExecutor로 실행된다.
- CLI 기본 명령과 출력 경로가 호환된다.
- `--dry-run`이 실행·스킵과 그 이유를 보여준다.
- 영향받는 단계와 하위 단계만 다시 실행된다.
- 실패 중간 산출물이 이전 성공 결과를 덮어쓰지 않는다.

Phase 3는 2026-08-12에 완료했다. 기본 테스트 282개, offline sample 11단계 강제 실행,
새 run의 11단계 global cache hit, query top-1 회귀와 SQLite integrity를 확인했다. Stage timeout,
run cancellation과 분류된 bounded retry는 attempt별 manifest를 보존하고 기본값에서는 기존 실행
동작을 유지한다. 다음 작업은 Phase 4의 OpenAPI v1과 local fake model server contract다.

## 8. Phase 4: HTTP Inference Provider

### 목적

동일한 Stage를 수정하지 않고 모델별 로컬·서버 실행을 선택한다.

### 작업

- [x] OpenAPI v1 문서 작성
- [x] local fake `/health`, `/capabilities`, inference job API 구현
- [x] HTTP client provider 구현
- [x] 인증 헤더 주입과 오류 redaction
- [x] artifact 전달 방식 계약 확정
  - [x] v1: 공유 Artifact Store
  - [ ] 후속 확장: 제한된 업로드 API
- [x] async job submit/poll/cancel 구현
- [x] idempotency header/body와 conflict 계약 확정
- [x] deadline, retry, backoff, circuit breaker 정책 구현
- [x] local fake server로 contract test
- [x] embedding alias local/HTTP typed deployment 설정과 CLI 전환
- [x] remote effective model fingerprint의 Engine resolver 연결
- [x] fake model server와 production HTTP Provider/EmbeddingService end-to-end 검증
- [x] production model server adapter와 실제 embedding backend end-to-end 검증

### 첫 원격화 대상

caption 또는 embedding을 권장한다. 출력이 비교적 작고 STT·diarization보다 실행 시간이 짧아
계약과 장애 처리를 검증하기 쉽다. 그 다음 STT, diarization 순으로 확장한다.

### 완료 조건

- 설정 변경만으로 같은 Stage가 local/HTTP provider를 선택한다.
- timeout, 429, 5xx, 인증 실패, 취소가 표준 오류로 변환된다.
- 동일 idempotency key가 중복 추론을 만들지 않는다.
- 대용량 미디어와 비밀값이 로그·manifest에 노출되지 않는다.
- 원격 응답의 effective model revision이 cache key에 반영된다.

Phase 4는 2026-08-12에 완료했다. HTTP Inference v1 OpenAPI, production client/reference server,
embedding alias local/HTTP 설정, 오류·재시도·취소·멱등성·보안 경계와 remote cache fingerprint를
검증했다. 기본 suite 318개, non-model HTTP integration 12개와 cached 실제 multilingual
SentenceTransformer HTTP E2E 1개가 통과했다.

## 9. Phase 5: 외부 서비스 연동

### 작업

- [x] `PipelineService` 유스케이스 구현
  - [x] 실행 생성
  - [x] 실행 취소
  - [x] 상태·진행률 조회
  - [x] 결과 artifact 조회
- [ ] `QueryService` 구현
- [ ] 기존 CLI를 Service adapter로 변경
- [ ] REST API adapter 추가
  - [x] pipeline create/status/cancel/artifact route와 server composition
  - [ ] query route 연결
- [ ] 필요 시 queue consumer adapter 추가
- [x] 입력 요청과 결과의 공개 schema 문서화
- [x] 사용자 제공 idempotency key 계약 정의
- [x] 인증·업로드 제한·보존 정책 정의

공개 schema는 [`openapi/pipeline-v1.yaml`](./openapi/pipeline-v1.yaml), media ID·영속 snapshot·
재시작 정책은 [`ADR-0025`](./adr/0025-durable-public-pipeline-api.md)에 기록한다. 초기 v1은 허용된
media catalog를 사용하며 직접 upload와 분산 queue consumer는 실제 배포 요구가 생길 때 후속
adapter로 추가한다.

### API 최소 범위

```text
POST   /v1/pipeline-runs
GET    /v1/pipeline-runs/{run_id}
DELETE /v1/pipeline-runs/{run_id}
GET    /v1/pipeline-runs/{run_id}/artifacts
POST   /v1/pipeline-runs/{run_id}/queries
```

### 완료 조건

- CLI와 API가 같은 Service와 Engine 경로를 사용한다.
- API 요청이 프로세스 종료와 무관한 run 상태로 조회된다.
- 상태 응답에 현재 단계, progress, warning, 실패 code가 포함된다.
- 외부 서비스가 로컬 output 경로를 알 필요가 없다.

## 10. Phase 6: 기존 품질 계획 재개

아키텍처 전환 이후 다음 순서로 품질을 개선한다.

### 타임라인

- 반개구간 `[start, end)` 통일
- 최대 겹침 단일 배정 또는 word timestamp 기반 분할
- 화자 턴 경계 정렬
- source segment와 confidence 보존

### 검색

- 한국어 Unicode·공백·문장부호 정규화
- 문자 n-gram FTS
- 유사도 하한과 관련 결과 없음 판정
- 점수·선택 근거 JSON 출력
- 30~50개 평가 질의로 Recall@k, MRR, no-answer precision 측정

### 컨텍스트

- 대상 모델 토크나이저 기반 실제 토큰 계산
- `--max-context-tokens`
- 인접 씬 확장과 중복 제거
- 예산 초과 시 낮은 순위 카드 축약·제거
- 실제 사용·제외 통계 기록

### 완료 조건

- 경계 테스트에서 전사 중복·누락 없음
- Recall@3 목표 90% 이상 또는 baseline 대비 개선 근거 기록
- 무관 질의 거부 정확도 측정
- 생성 context가 지정 예산을 넘지 않음

## 11. Phase 7: 성능과 멀티모달 확장

후속 우선순위:

1. Executor의 비주얼·오디오 분기 병렬 실행
2. 씬 길이에 따른 키프레임 1~3장 추출
3. perceptual hash 중복 제거
4. caption device 자동 선택과 batch 크기 tuning
5. OCR provider
6. 내장 자막·챕터 활용
7. 오디오 이벤트 provider
8. 질의 기반 2-pass 고품질 재처리
9. 필요 시 RemoteExecutor와 분산 worker

## 12. 테스트 전략

| 계층 | 목적 | 모델/네트워크 |
|---|---|---|
| Domain unit | 병합·상태·cache key·직렬화 | 사용하지 않음 |
| Contract | 모든 provider/executor의 동일 동작 | fake 우선 |
| Stage unit | request 생성·응답 정규화 | fake provider |
| Engine integration | DAG·cache·retry·resume | fake executor/provider |
| Local integration | FFmpeg와 실제 로컬 provider | 선택 실행 |
| HTTP integration | API·timeout·취소·artifact | 로컬 fake server |
| Golden E2E | sample 전체 결과 회귀 | 실제 모델, 수동/CI 분리 |
| Retrieval evaluation | Recall@k·MRR·latency | 고정 index 또는 실제 embedding |

CI의 기본 경로는 모델 다운로드와 네트워크 없이 동작해야 한다. 실제 모델 검증은 별도 marker나
nightly/manual job으로 분리한다.

## 13. PR 완료 체크리스트

- [ ] 변경 범위가 한 Phase의 명확한 slice인가
- [ ] 공개 schema 또는 설정 변경에 버전·호환 정책이 있는가
- [ ] 단위·계약·통합 테스트 중 필요한 테스트를 추가했는가
- [ ] 기존 CLI·산출물 호환 여부를 확인했는가
- [ ] 비밀값·로컬 절대 경로가 로그나 payload에 노출되지 않는가
- [ ] 실패·skip·fallback이 관측 가능한가
- [ ] `docs/STATUS.md`를 갱신했는가
- [ ] 설계 변경이라면 architecture/contracts/ADR을 갱신했는가
- [ ] 실행한 검증 명령을 기록했는가

## 14. 주요 위험과 대응

| 위험 | 영향 | 대응 |
|---|---|---|
| 추상화가 실제 요구보다 커짐 | 일정 지연 | Local 구현과 현재 Stage에 필요한 최소 Port부터 시작 |
| 원격 artifact 전달 지연 | 처리 성능 저하 | 공유 store, checksum dedup, 본문 전송 금지 |
| 모델별 출력 차이 | 캐시·품질 회귀 | 정규화 schema, effective revision, golden 평가 |
| 중복 원격 실행 | GPU 비용·결과 충돌 | idempotency key, atomic publish |
| 자동 fallback으로 재현성 저하 | 결과 추적 불가 | 명시 설정, manifest 기록, 조용한 fallback 금지 |
| Big-bang 리팩터링 | 장기간 미동작 | provider와 stage를 하나씩 strangler 방식으로 이동 |
| 테스트에서 모델 다운로드 | 느리고 불안정한 CI | fake contract test와 실제 모델 job 분리 |

## 15. 전체 완료 정의

아키텍처 마이그레이션은 디렉터리 이동만으로 완료되지 않는다. 다음을 모두 만족해야 한다.

- 현재 샘플 파이프라인과 query가 새 Engine에서 동작한다.
- Engine 테스트가 ML 의존성 없이 실행된다.
- 네 모델 slot이 LocalProvider로 동작한다.
- 최소 한 모델 slot이 HTTP Provider E2E로 검증된다.
- 설정만으로 local/HTTP를 전환한다.
- cache, retry, timeout, cancel, skip 재평가가 테스트된다.
- CLI와 API가 같은 Application Service를 사용한다.
- 신규 session이 `STATUS.md`와 문서만으로 다음 작업을 재개할 수 있다.
