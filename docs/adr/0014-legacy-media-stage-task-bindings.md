# ADR-0014: legacy media Stage는 strict StageTask adapter와 keyframe bundle로 연결한다

- 상태: Accepted
- 결정일: 2026-08-06
- 관련 문서:
  [`../07-execution-inference-contracts.md`](../07-execution-inference-contracts.md)

## 배경

현재 11개 Stage는 `PipelineContext`의 host path와 mutable 설정을 읽고 output tree에 직접 쓴다.
새 Engine/Executor는 명시적인 StageTask와 ArtifactRef/StageResult를 사용하므로 기존 Stage를 한 번에
재작성하지 않고 연결할 compatibility boundary가 필요하다. 단순히 `run(ctx)`를 호출하면 task
config/input을 무시하고 marker 파일만 cache하는 문제가 다시 생긴다.

특히 03 keyframes는 `keyframes.json` 외에 가변 개수 JPEG를 생성한다. JSON만 manifest output으로
등록하면 이미지 일부가 사라져도 cache hit가 되어 downstream caption 실행이 실패하거나 stale
결과를 사용할 수 있다.

## 결정

### Strict compatibility runner

outer `adapters` package의 `LegacyStageTaskRunner`가 다음 경계를 강제한다.

- task의 Stage name/version, logical input key, config key와 model binding을 definition과 정확히 비교
- legacy Stage가 읽을 host path의 size/SHA-256이 input ArtifactRef와 같은지 실행 직전 확인
- StageTask config만 실행 동안 `PipelineContext`에 주입하고 성공·실패와 관계없이 기존 값 복원
- 같은 run-scoped context를 공유하므로 하나의 lock으로 legacy 실행을 직렬화
- legacy `run(ctx)` return을 StageResult metrics로, 생성 파일을 logical output ArtifactRef로 정규화
- output 누락과 input mismatch를 `LegacyStageContractError`로 명시

adapter는 기존 runner의 marker 존재 skip을 호출하지 않는다. 실행 여부는 PipelineEngine cache
decision만 소유한다.

### 01~04 output mapping

| Stage | logical output | legacy path |
|---|---|---|
| 01 probe | `metadata` | `01_probe/metadata.json` |
| 02 scenes | `scenes` | `02_scenes/scenes.json` |
| 02 scenes | `scene_stats` | `02_scenes/scene_stats.csv` |
| 03 keyframes | `keyframes` | `03_keyframes/keyframes.json` |
| 03 keyframes | `keyframe_images` | `03_keyframes/keyframe_images.zip` |
| 04 audio | `audio` | WAV 또는 no-audio metadata sentinel |
| 04 audio | `audio_metadata` | `04_audio/audio.json` |

오디오가 없으면 `audio`와 `audio_metadata`가 같은 JSON ArtifactRef를 가리킨다. 따라서 downstream은
required input을 유지하면서 `has_audio: false`를 해석할 수 있고, 과거 실행의 stale WAV를 잘못
등록하지 않는다.

### Deterministic keyframe bundle

03 adapter는 Stage 실행 후 `keyframes.json`에 열거된 이미지만 정렬해 ZIP_STORED archive로 만든다.
entry timestamp와 permission을 고정하고 임시 파일을 원자적으로 교체해 같은 이미지 집합은 같은
checksum을 갖는다. 경로는 `03_keyframes/frames/` 아래의 안전한 POSIX relative path만 허용한다.

이 output 계약 변경으로 03 Stage version과 이를 요구하는 08 caption Stage version을 `1.1.0`으로
올린다. 08 task는 `keyframes`와 `keyframe_images`를 모두 required input으로 받아 bundle 누락이나
변조가 cache key와 integrity 검사에 반영되게 한다.

## 고려한 대안

### keyframes.json만 marker로 등록

JPEG 누락을 감지하지 못해 manifest 기반 cache의 완전성 조건을 위반한다.

### 각 JPEG를 동적 logical output으로 등록

현재 StageSpec은 고정 output key 집합을 사용한다. 동적 key 규칙을 추가하면 planner와 output
contract가 복잡해지므로 compatibility 기간에는 하나의 deterministic bundle을 사용한다.

### legacy context 설정을 그대로 신뢰

실제 설정이 StageTask/cache key에 포함되지 않을 수 있다. Stage별 config key를 exact match하고
task 값만 임시 적용한다.

## 결과

긍정적 영향:

- 01~04가 PipelineEngine과 LocalExecutor에서 기존 파일 형식을 유지하며 실행된다.
- task input/config와 legacy host path 사이의 숨은 불일치를 실행 전에 차단한다.
- keyframe sidecar와 no-audio 조건도 manifest/cache 의미에 포함된다.
- 기존 CLI와 runner는 변경하지 않아 단계적 동등성 검증이 가능하다.

비용과 제약:

- keyframe ZIP만큼 추가 local storage와 write 비용이 든다.
- adapter는 migration 전용이며 새 native Stage는 host path가 아닌 ArtifactStore를 직접 사용해야 한다.
- 05~11 binding, cache hit 후 bundle 복원, Application Service와 CLI 전환은 아직 남아 있다.
- video ArtifactRef ingest/materialization은 Application Service composition에서 고정해야 한다.

## 구현 위치

- adapter: `src/video_preprocess/adapters/legacy_stages.py`
- default StageSpec: `src/video_preprocess/engine/defaults.py`
- contract tests: `tests/adapters/test_legacy_stage_bindings.py`
