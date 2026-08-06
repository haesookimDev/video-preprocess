# ADR-0015: legacy model Stage는 effective model을 승격하고 bundle sidecar를 복원한다

- 상태: Accepted
- 결정일: 2026-08-06
- 관련 문서:
  [`../07-execution-inference-contracts.md`](../07-execution-inference-contracts.md)

## 배경

05 VAD~08 caption은 이미 Inference Gateway 기반 service를 사용하지만 호출 경계는 여전히
`run(ctx)`와 legacy JSON이다. Engine cache가 model deployment 변화를 정확히 판단하려면 JSON에
추가된 provider/model/revision/runtime을 StageResult의 구조화된 `ModelExecution`으로 올려야 한다.
또한 03 keyframe Stage가 cache hit한 뒤 JPEG sidecar가 local path에 없을 수 있으므로 08 실행 전에
검증된 bundle을 materialize해야 한다.

## 결정

### Model binding과 config

05~08 compatibility definition은 StageTask의 model binding을 다음 alias와 exact match한다.

| Stage | slot | alias | task config |
|---|---|---|---|
| 05 VAD | `vad` | `vad.default` | silence/padding milliseconds |
| 06 STT | `stt` | `stt.default` | merge gap, language, legacy model name |
| 07 diarization | `diarization` | `diarization.default` | legacy model name |
| 08 captions | `caption` | `caption.default` | legacy model name |

concrete service는 run-scoped context에 주입되며 Stage는 alias의 local/HTTP 배치를 분기하지 않는다.
legacy Stage가 아직 context에서 읽는 concrete model name도 cache key에서 빠지지 않도록 migration
기간에는 task config에 명시한다.

### Effective ModelExecution

성공한 legacy JSON은 provider, model, revision이 모두 있어야 하며 runtime은 선택적이다. adapter는
이를 해당 slot의 `ModelExecution`으로 변환해 StageResult에 기록한다. 필수 metadata가 없으면
성공으로 추정하지 않고 `LegacyStageContractError`를 발생시킨다.

### Skip normalization

실행 조건 불성립은 sentinel artifact를 publish한 뒤 다음 stable status/reason으로 정규화한다.

| 조건 | status | reason code |
|---|---|---|
| audio stream 없음 | `skipped` | `NO_AUDIO` |
| VAD speech 없음 | `skipped` | `NO_SPEECH` |
| optional diarization credential/access 없음 | `skipped` | `OPTIONAL_DIARIZATION_UNAVAILABLE` |
| keyframe 없음 | `skipped` | `NO_KEYFRAMES` |

PipelineEngine은 skipped output을 downstream에 전달하지만 현재 cache 정책은 조건 재평가를 위해
skipped manifest를 hit로 사용하지 않는다.

### Keyframe bundle restore

08 adapter는 `keyframes` JSON과 `keyframe_images` ZIP 자체의 ArtifactRef checksum이 먼저 legacy
path와 일치하는지 검증한다. 그 후 JSON에 열거된 safe path와 ZIP member 집합이 정확히 같을 때만
각 이미지를 임시 파일과 `os.replace`로 복원한다. 추가·누락 member, path traversal, 손상된 ZIP은
caption service 호출 전에 거부한다.

## 고려한 대안

### legacy JSON model metadata를 StageResult metrics에만 보존

cache evaluator가 effective deployment를 구조적으로 비교할 수 없으므로 `models` 필드로 승격한다.

### caption Stage가 ZIP을 직접 읽도록 변경

현재 caption service와 legacy JSON은 개별 image ArtifactRef를 사용한다. migration adapter가 기존
path를 복원하면 Stage 알고리즘과 provider 계약을 변경하지 않고 완전성을 확보할 수 있다.

### skip을 succeeded로 유지

운영자가 credential 미설정과 실제 성공을 구분할 수 없고 recheck 정책도 적용하기 어렵다. output
sentinel은 유지하되 terminal status를 명시한다.

## 결과

긍정적 영향:

- 05~08이 PipelineEngine exact plan과 LocalExecutor에서 동일 StageTask 계약으로 실행된다.
- 실제 provider/revision이 StageManifest와 cache evaluator까지 전달된다.
- cache resume 후 caption 재실행이 keyframe sidecar 부재에 안전하다.
- optional/empty 조건이 검색 가능한 stable reason code로 남는다.

비용과 제약:

- compatibility config에 legacy concrete model name이 일시적으로 중복된다.
- concrete `EffectiveModelResolver` composition은 Application Service 작업에 남아 있다.
- 09~11 output sidecar와 전체 11단계 Engine path는 아직 연결되지 않았다.
- skipped recheck fingerprint가 없어 skipped Stage는 매번 값싼 조건 검사를 다시 수행한다.

## 구현 위치

- binding/outcome/restore: `src/video_preprocess/adapters/legacy_stages.py`
- tests: `tests/adapters/test_legacy_model_bindings.py`
