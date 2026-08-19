# ADR-0035: 오디오 이벤트를 선택적 Provider Stage와 canonical taxonomy로 처리한다

- 상태: Accepted
- 결정일: 2026-08-19
- 관련 문서:
  [`../06-target-architecture.md`](../06-target-architecture.md),
  [`../07-execution-inference-contracts.md`](../07-execution-inference-contracts.md)

## 배경

기존 audio 분기는 VAD·STT·화자 분리만 수행해 음악, 박수, 경보처럼 발화가 아닌 단서를 검색과
context에 전달하지 못했다. 모델마다 label 집합과 시간 해상도가 다르므로 Stage가 특정 모델 label이나
로컬 runtime을 직접 알면 local/HTTP 교체와 결과 호환성이 깨진다. 또한 오디오 이벤트 모델은 기본
파이프라인에 추가 다운로드와 실행 비용을 강제해서는 안 된다.

## 결정

### 추론 계약과 책임

- `audio_event_detection` task와 `audio_event.default` alias를 추가한다.
- Stage는 16 kHz mono WAV를 `ArtifactRef`로 등록하며 bytes·절대 경로를 경계 밖으로 보내지 않는다.
- AudioEventService는 전체 길이를 반개구간 window로 나누고 Provider capability와 설정 중 작은 batch로
  순차 요청한다. deterministic idempotency, 하나의 total deadline, effective model 일치와
  all-or-nothing aggregate를 소유한다.
- Provider는 한 window의 모델 추론과 model 고유 label을 `audio-events-v1`로 바꾸는 책임을 가진다.
  Engine과 Executor는 taxonomy나 windowing을 해석하지 않는다.
- canonical label은 `music`, `applause`, `laughter`, `alarm`, `siren`, `vehicle`, `animal`, `door`,
  `impact`, `noise`다. confidence는 0~1이고 요청한 label 밖의 응답은 계약 위반이다.

### 시간과 겹침 정책

- Provider 결과는 입력과 같은 순서·개수의 `window_id`와 label/confidence 배열이다.
- `merge-same-label-overlap-v1`은 동일 label의 겹치거나 맞닿은 양성 window만 합친다. 구간은 합집합,
  confidence는 최댓값, 원본 window ID는 `source_window_ids`로 보존한다.
- 서로 다른 label의 중첩은 유지한다. 최종 event는 `(start_sec,end_sec,label)` 순서와 1부터 연속인
  `event_id`를 사용한다.
- timeline은 기존 최대 겹침·중점 tie-break로 event를 scene 하나에 배정한다. scene card의 전체
  `audio_events`가 source/confidence를 보존하고 `audio_event_text`는 ordered unique label 요약이다.

### Stage와 배포

- 독립 `05_audio_events` version `1.0.0`은 `04_audio`의 `audio`, `audio_metadata`를 받고
  `audio_events` JSON을 publish한다.
- 기본 `audio_event_mode=disabled`는 Provider를 만들거나 호출하지 않고
  `AUDIO_EVENTS_DISABLED` sentinel을 publish한다. audio stream이 없으면 `NO_AUDIO`다.
- mode `all`은 endpoint가 없으면 LocalAudioEventProvider, 있으면 `HTTPInferenceProvider`를 사용한다.
  양쪽 사이의 자동 fallback은 없다.
- pipeline CLI와 server는 local device, endpoint, bearer-token 환경변수, 허용 Artifact namespace와
  service batch를 composition root에 전달한다. Stage는 이를 알지 못한다.
- 기본 DAG는 14개 Stage다. 09 version `1.5.0`, 10/11 version `1.4.0`이 event label과 confidence를
  timeline, index, static/query context에 additive하게 전달한다.

### Local reference Provider

- reference model은 BSD-3-Clause
  [`MIT/ast-finetuned-audioset-10-10-0.4593`](https://huggingface.co/MIT/ast-finetuned-audioset-10-10-0.4593)이다.
  86.6M parameter·F32 weight 약 346MB이고 Transformers AST/AutoFeatureExtractor를 사용한다.
- AudioSet의 527개 class와 index 순서를 load 시 검증하고
  `ast-audioset-527-to-audio-events-v1`로 canonical taxonomy에 매핑한다. ontology와 label 의미는
  [AudioSet 공식 자료](https://research.google.com/audioset/download.html)를 기준으로 한다.
- AST logits에는 softmax를 적용한다. 한 canonical label의 confidence는 그 label로 매핑된 source
  class 확률 중 최댓값이다. model 고유 세부 label은 public output에 노출하지 않는다.
- 입력은 uncompressed signed PCM16 mono 16 kHz WAV다. metadata와 실제 sample 길이 차이는 10ms까지만
  끝점을 clamp하고 400 sample 미만의 유효 tail은 feature frame 생성을 위해 zero padding한다.
- model과 같은 checksum의 decoded audio는 process 안에서 lazy reuse한다. batch 최대값은 8이고
  `auto` device는 CUDA→MPS→CPU 순서다. 실제 device, mapping version과 resolved Hub commit을 effective
  model에 기록한다.
- 로컬 cache에 model config가 있으면 `local_files_only` load를 먼저 시도한다. cache가 불완전한 경우에만
  Hub load를 재시도하며, 기본 disabled 경로는 cache 확인이나 download를 하지 않는다.

## 고려한 대안

### Stage가 Transformers audio-classification pipeline을 직접 실행

모델 lifecycle·device·label mapping과 pipeline join 책임이 결합되어 채택하지 않았다.

### 모델 원본 label을 그대로 저장

모델 교체 시 index 의미와 API 결과가 달라진다. 제한된 versioned taxonomy와 Provider mapping을
선택했다.

### 전체 audio를 한 요청으로 분류

Provider별 최대 길이와 시간 위치가 불명확하다. ArtifactRef 하나와 inline window batch를 분리했다.

### 계약과 local 모델을 한 번에 도입

모델 정확도, 라이선스, taxonomy mapping과 resource profile 검증 전에 Stage 계약이 모델에 종속될 수
있었다. 기본 비용을 늘리지 않는 disabled sentinel과 HTTP 계약을 먼저 고정하고 local 구현을 후속
slice로 검증하는 단계적 도입을 선택했다.

## 결과

긍정적 영향:

- Engine·Executor·Stage 변경 없이 in-process 또는 HTTP Provider를 같은 task 계약에 연결할 수 있다.
- 모델별 label 차이가 canonical taxonomy 뒤로 숨고 source window와 confidence가 downstream까지 남는다.
- 기본 실행은 새 모델 다운로드·network·GPU 비용 없이 기존 결과에 빈 additive field만 추가한다.

비용과 제약:

- local 활성화의 첫 실행은 약 346MB model weight download와 AST 메모리·연산 비용이 필요하다.
- AudioSet 세부 class를 10개 canonical label로 축약하므로 source class별 설명 가능성은 제한된다.
- 긴 event도 timeline version 1에서는 scene 하나에만 배정된다.
- 새 Stage와 09/10/11 version 상승으로 관련 cache가 한 번 무효화된다.

## 검증 결과

- fake in-process Provider로 window batch, capability chunk, taxonomy/confidence/순서 오류,
  idempotency, model 일치와 같은-label 병합을 검증했다.
- disabled/no-audio/enabled Stage sentinel과 strict StageTask binding, 14-stage DAG boundary를 검증했다.
- loopback HTTP Inference v1에서 Artifact namespace, 2+1 chunk, effective model과 변경 없는 Stage 실행을
  검증했다.
- timeline 단일 배정, unassigned ID, provenance/confidence, index와 static/query context 전파를
  network-free 테스트로 검증했다.
- fake local Provider에서 527-label mapping, ordered chunk, PCM16 decode, 10ms end clamp, short-tail padding,
  artifact 오류, OOM, lazy health/warmup/effective revision과 device 우선순위를 검증했다.
- 실제 CPU AST smoke test는 1개 window를 35.47초(cold download 포함)에 처리했고 resolved revision
  `f826b80d28226b62986cc218e5cec390b1096902`를 기록했다.
- `sample.mp4`는 13개 window를 `[8,5]`로 처리해 confidence 0.5에서 event 0개를 정상 publish했고,
  local AST를 포함한 14-stage 전체 pipeline과 query 회귀가 성공했다.

## 구현 위치

- contract/service: `src/video_preprocess/inference/audio_event.py`
- local Provider: `src/video_preprocess/inference/local/audio_event.py`
- Stage: `src/pipeline/stages/s05_audio_events.py`
- DAG/binding/composition: `src/video_preprocess/engine/defaults.py`,
  `src/video_preprocess/adapters/legacy_stages.py`, `src/video_preprocess/services/local.py`
- downstream: `src/pipeline/stages/s09_timeline.py`, `s10_index.py`, `s11_context.py`,
  `src/video_preprocess/services/query.py`
