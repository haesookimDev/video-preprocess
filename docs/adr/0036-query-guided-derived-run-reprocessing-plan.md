# ADR-0036: 질의 기반 2-pass 재처리는 provenance가 고정된 파생 run으로 계획한다

- 상태: Accepted
- 결정일: 2026-08-19
- 관련 문서:
  [`../06-target-architecture.md`](../06-target-architecture.md),
  [`../07-execution-inference-contracts.md`](../07-execution-inference-contracts.md),
  [`../08-development-roadmap.md`](../08-development-roadmap.md)

## 배경

1-pass pipeline은 전체 영상에서 adaptive keyframe, caption, OCR, timeline, index와 context를 만든다.
검색 결과를 이용하면 사용자가 실제로 관심 있는 소수 scene에 더 많은 시각 추론 비용을 집중할 수
있다. 하지만 현재 `QueryService`는 검색과 context 조립만 하는 read-only 유스케이스이고, 03/08
Stage는 scene 전체 Artifact를 원자적으로 다시 만든다. 검색 호출이 기존 run의 파일을 직접
덮어쓰게 하거나 선택 scene의 일부 JSON만 수정하면 다음 문제가 생긴다.

- 어떤 1-pass Artifact와 검색 결과를 근거로 재처리했는지 재현할 수 없다.
- 부모 run의 manifest/checksum과 실제 파일이 달라져 cache integrity가 깨진다.
- 선택하지 않은 scene과 선택한 scene의 출처를 구분할 수 없다.
- CLI와 API가 서로 다른 방식으로 Stage와 Provider를 직접 호출할 가능성이 생긴다.
- 같은 요청의 재시도, profile 변경과 Stage version 변경을 cache에서 구분하기 어렵다.

## 결정

### 1. 검색과 mutation을 분리한다

기존 `PipelineQueryRequest`와 `/queries`는 계속 read-only다. 2-pass는 별도
`PipelineReprocessingSubmission`과 `QueryReprocessingApplicationService`가 소유한다. 실제 실행
adapter가 추가될 때 CLI는 별도 reprocess command를 사용하고 API는
`POST /v1/pipeline-runs/{source_run_id}/reprocessing-runs`를 사용한다. API mutation은 기존 create와
같이 header/body의 `Idempotency-Key`를 일치시킨다. 이 경로는 아직 실행 구현과 OpenAPI에 공개하지
않으며, 현재 구현은 typed plan까지만 제공한다.

### 2. 결과는 부모를 수정하지 않는 파생 run이다

재처리 실행은 새 `run_id`와 workspace를 만들고 `source_run_id`를 provenance로 보존한다. 1-pass
workspace, Engine manifest와 Artifact 본문은 read-only다. source resolver는 succeeded run에서
필요한 ArtifactRef를 checksum과 함께 고정하고, importer는 이를 파생 namespace로 검증·import한다.
이 경계는 `verified-derived-copy-v1`로 구현됐다. source 13개를 모두
검증한 뒤 파생 `00_source/`에 publish하고 `reprocessing-source-manifest-v1`을 마지막 commit marker로
publish한다. source Artifact가 없거나 checksum 검증이 실패하면 Stage 실행 전에 거부한다.

### 3. 후보 선택은 direct query match만 사용한다

submission v1 필드는 다음과 같다.

| 필드 | 규칙 |
|---|---|
| `source_run_id` | 완료된 1-pass run |
| `query` | 1~4000자의 검색 문자열 |
| `quality_profile` | server-owned immutable profile 이름 |
| `max_scenes` | 1~20, 기본 3 |
| `min_similarity` | -1~1, 기본 0.35 |
| `idempotency_key` | 최대 200자, semantic fingerprint에서는 제외 |

planning query는 `top_k=max_scenes`, `adjacent_scenes=0`, 최소 context budget으로 기존
`QueryService`를 호출한다. context neighbor는 LLM 입력 확장용이지 고비용 재처리 후보가 아니므로
선택하지 않는다. match 순서에서 scene ID를 중복 제거하고 direct match만 후보로 사용한다. 결과가
`no_answer`이거나 후보가 비면 `ReprocessingNoCandidatesError`로 mutation을 만들지 않는다.

### 4. 첫 profile은 `visual-detail-v1`이다

profile은 요청자가 model endpoint나 임의 Stage config를 넣는 통로가 아니라 배포자가 소유하는
versioned 품질 정책이다.

- 후보 scene: `keyframes_per_scene=3`, `ocr_mode=all`
- 후보 단위 고비용 Stage: `03_keyframes`, `08_captions`, `08_ocr`
- 전체 결과 재물질화 Stage: `09_timeline`, `10_index`, `11_context`
- 선택하지 않은 scene: 1-pass visual Artifact를 그대로 복사
- 출력 정책: `derived-run-no-parent-overwrite-v1`
- overlay 정책: `copy-unselected-from-source-v1`

전용 reprocessing DAG는 다음 6개 Stage와 11개 boundary input을 만든다.

| 구분 | 이름 |
|---|---|
| selected-scene Stage | `03_keyframes`, `08_captions`, `08_ocr` |
| full-materialization Stage | `09_timeline`, `10_index`, `11_context` |
| boundary input | `audio_events`, `diarization`, `embedded_text`, `metadata`, `scenes`, `transcript`, `video`, `source_keyframes`, `source_keyframe_images`, `source_captions`, `source_ocr` |
| overlay source | `keyframes`, `keyframe_images`, `captions`, `ocr` |
| query provenance | `timeline`, `search_index` |

`source_*` boundary는 같은 이름의 1-pass visual Artifact를 파생 namespace로 import한 참조다.
profile의 Stage 집합은 전용 DAG와 exact match해야 한다. 새 descendant가 추가되면 비용·결과 의미를
검토하고 profile version 또는 계약을 명시적으로 갱신한다.

### 5. plan은 남은 Application runtime capability를 명시한다

`ReprocessingPlan`은 후보, Stage name/version과 scope, boundary input, source ArtifactRef 전체,
request/plan fingerprint를 직렬화한다. source import와 03/08 overlay는 구현됐지만 plan→import→Engine,
파생 run 상태·idempotency를 한 유스케이스로 조합하는 다음 capability가 구현되지 않았으므로
`execution.ready=false`다.

- `derived-run-application-runtime-v1`

CLI/API는 이 capability가 구현되기 전에 plan을 성공한 pipeline run처럼 제출하면 안 된다. source
import와 03/08 overlay는 fake Engine/Local Store에서 parent 불변성과 merge 결과를 검증했다. 다음
slice에서 파생 Application runtime과 상태 저장을 완성한 뒤에만 실행 adapter를 연다.

### 6. cache와 version은 요청이 아니라 결과 의미를 따른다

submission fingerprint는 idempotency key를 제외한 source run, query, profile과 후보 설정을 포함한다.
plan fingerprint는 여기에 정규화 query, 최종 scene ID, profile 정책, Stage version과 모든 source
ArtifactRef checksum을 포함한다. 따라서 같은 semantic request와 동일 source는 같은 plan ID를 만들고,
source bytes, 선택 scene, profile 또는 Stage version이 바뀌면 다른 plan/cache identity를 만든다.

선택 scene ID, overlay policy, profile version과 source checksum은 향후 03/08 StageTask config 또는
명시적 provenance input을 통해 cache key에 들어가야 한다. overlay의 의미가 바뀌면 해당 Stage version을
올린다. 09/10/11은 완성된 overlay Artifact checksum을 입력으로 받아 전체 결과를 다시 만들며, 부모
run의 cache manifest를 덮어쓰지 않는다.

## 고려한 대안

### `/queries`에 `reprocess=true`를 추가한다

GET과 같은 read-only 기대를 깨고 검색 재시도만으로 비용과 파일 mutation이 발생하므로 거부했다.

### source run을 같은 `run_id`로 강제 재실행한다

부모 산출물과 provenance를 잃고 부분 실패 시 기존 성공 결과를 복구하기 어려워 거부했다.

### 선택 scene별 독립 index만 반환한다

기존 query/context 소비자는 하나의 완성된 timeline과 index를 기대한다. 후보 Stage만 비싸게 실행하되
09/10/11은 전체 overlay를 materialize하는 방식을 선택했다.

### client가 Stage 목록과 model endpoint를 지정한다

profile 의미와 보안·배포 경계를 깨고 Stage/Provider 선택 책임이 adapter로 새므로 거부했다.

## 결과

긍정적 영향:

- 검색은 계속 read-only이고 비용이 큰 작업은 명시적 mutation으로만 발생한다.
- 1-pass/2-pass Artifact의 출처와 checksum을 재현할 수 있다.
- profile, Stage version과 source bytes가 cache identity에 반영된다.
- API, CLI와 향후 queue adapter가 같은 Application 계약을 사용할 수 있다.

비용과 제약:

- source Artifact를 파생 namespace로 import할 저장·I/O 비용이 있다.
- 공개 Application runtime이 구현되기 전 plan은 의도적으로 실행할 수 없다.
- OCR을 포함한 profile 실행은 배포 환경의 configured Provider와 언어 data가 필요하다.

## 구현 위치

- submission/profile/plan/service:
  `src/video_preprocess/services/reprocessing.py`
- source import:
  `src/video_preprocess/services/reprocessing_artifacts.py`
- 전용 DAG와 strict binding:
  `src/video_preprocess/engine/defaults.py`,
  `src/video_preprocess/adapters/legacy_stages.py`
- selected-scene overlay:
  `src/pipeline/stages/s03_keyframes.py`,
  `src/pipeline/stages/s08_captions.py`,
  `src/pipeline/stages/s08_ocr.py`
- fake Application 계약:
  `tests/services/test_reprocessing_service.py`
- import/parent 불변성·Engine/overlay 계약:
  `tests/services/test_reprocessing_artifacts.py`,
  `tests/adapters/test_reprocessing_bindings.py`
- 파생 Application runtime과 CLI/API/OpenAPI mutation adapter: 다음 구현 slice
