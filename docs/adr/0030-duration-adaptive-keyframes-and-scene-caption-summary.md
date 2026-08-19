# ADR-0030: 씬 길이 기반 키프레임과 다중 캡션을 호환 가능한 씬 요약으로 만든다

- 상태: Accepted
- 결정일: 2026-08-19
- 관련 문서:
  [`../01-video.md`](../01-video.md),
  [`../07-execution-inference-contracts.md`](../07-execution-inference-contracts.md)

## 배경

기존 03단계는 모든 씬에서 중앙 프레임 한 장만 추출했고 `keyframes_per_scene` 설정을 사용하지
않았다. 짧고 정적인 씬에는 충분하지만, 한 씬 안에서 화면이 바뀌는 긴 구간은 중앙 한 장만으로
앞뒤 정보를 잃을 수 있다. 단순히 프레임 수만 늘리면 기존 `scene_NNN.jpg`, flat caption 배열,
timeline의 단일 `keyframe`·`caption` 필드를 사용하는 소비자와 호환되지 않는다.

또한 03단계의 JPEG는 JSON과 별도의 sidecar다. 프레임 수가 설정에 따라 바뀌어도 deterministic
bundle, cache version과 이전 실행의 stale 파일 정리까지 하나의 계약으로 다뤄야 한다.

## 결정

### 1. 길이 기반 선택 수

`keyframes_per_scene`은 고정 수가 아니라 씬당 최대 수이며 1~3만 허용한다. 실제 선택 수는
`duration-adaptive-v1` 정책으로 계산한다.

| 씬 길이 | 정책상 수 | 설정 적용 후 수 |
|---:|---:|---:|
| `0 < duration < 8s` | 1 | `min(1, max)` |
| `8s <= duration < 20s` | 2 | `min(2, max)` |
| `20s <= duration` | 3 | `min(3, max)` |

기본값 1은 모든 씬에서 기존 동작을 유지한다. 최대값은 Pipeline task config라서 cache key에
포함되며 CLI `--keyframes-per-scene`과 Pipeline API `settings.keyframes_per_scene`이 같은 검증을
사용한다.

### 2. timestamp와 filename

N장을 선택할 때 씬 경계가 아닌 내부 균등 지점 `start + duration * i / (N + 1)`을 사용하고
millisecond 단위로 반올림한다. N=1이면 기존 중앙 시각과 같다.

- 1장: `03_keyframes/frames/scene_NNN.jpg`
- 2~3장: `03_keyframes/frames/scene_NNN_II.jpg`, `II`는 1부터 시작

`keyframes.json`의 각 항목은 기존 `scene_id`, `timestamp_sec`, `path`, `size_bytes`와 함께
`keyframe_index`, `keyframe_count`를 기록한다. 최상위 `selection_policy`에는 정책 이름, 설정 상한,
`[8.0, 20.0]` 경계와 `evenly_spaced_interior_points` timestamp 전략을 기록한다.

03단계가 성공적으로 새 집합을 추출한 뒤 선택되지 않은 이전 `scene_*.jpg`를 제거한다. adapter는
JSON에 열거된 파일만 정렬·고정 metadata의 `keyframe_images.zip`에 포함한다. 03 Stage version은
`1.2.0`이다.

### 3. caption 정규화

08단계는 모든 키프레임을 기존 ordered ArtifactRef batch로 한 번에 전달한다. 다중 프레임의
artifact ID는 `keyframe_scene_NNN_II`, 단일 프레임은 기존 `keyframe_scene_NNN`을 사용한다.

`captions.json`은 기존 flat `captions`를 유지하고 각 항목에 `keyframe_index`와
`keyframe_count`를 추가한다. 새 `scene_captions`는 입력 scene 순서대로 다음 구조를 제공한다.

```json
{
  "scene_id": 1,
  "caption_count": 2,
  "captions": [
    {
      "keyframe_index": 1,
      "keyframe_count": 2,
      "timestamp_sec": 3.333,
      "keyframe": "03_keyframes/frames/scene_001_01.jpg",
      "caption": "a title card"
    }
  ]
}
```

top-level `caption_policy`는 `per-keyframe-scene-group-v1`이고 `scene_count`를 함께 기록한다. 08
Stage version은 `1.2.0`이다. Provider의 batch 크기와 device 자동 선택은 별도 후속 작업으로 둔다.

### 4. timeline 호환 요약

09 scene card는 다음 additive 필드를 제공한다.

- `keyframes`: 씬의 모든 keyframe path
- `visual_captions`: frame index/count, timestamp, path, caption을 보존한 배열
- `keyframe`: 씬 중점에 가장 가까운 대표 path. 동률이면 먼저 선택된 프레임
- `caption`: frame 순서에서 같은 문자열을 한 번만 남기고 ` | `로 연결한 요약

단일 프레임에서는 기존 `keyframe`과 `caption` 값이 그대로다. 기존 10 index, 11 context와 query는
단일 `caption`을 계속 읽기 때문에 다중 시각 정보가 추가 수정 없이 검색·컨텍스트에 포함된다.
최상위 `visual_summary_policy`는 `ordered_unique_caption_join`이며 09 Stage version은 `1.2.0`이다.

## 고려한 대안

### 모든 씬에서 설정 수만큼 고정 추출

짧고 정적인 씬에도 같은 추론 비용이 들고 adaptive 요구를 충족하지 못한다.

### 씬 시작·끝 경계에서 추출

반개구간 끝 경계는 다음 씬 프레임을 읽을 수 있고 전환 프레임이 선택될 가능성이 높다. 내부 균등
지점이 seek와 scene boundary 모두에서 더 안정적이다.

### 다중 캡션만 남기고 기존 scalar 필드 제거

index, context, query와 외부 legacy 소비자를 동시에 변경해야 한다. additive 배열과 deterministic
scalar summary를 함께 제공해 단계적으로 migration한다.

### 다중 캡션을 별도 요약 모델로 다시 생성

추론 비용과 새 model revision/cache 의미가 생긴다. 현재는 결정적인 문자열 요약으로 충분하며,
고품질 scene summarizer는 2-pass 작업으로 분리한다.

## 결과

긍정적 영향:

- 긴 씬의 앞·중간·뒤 시각 정보를 최대 세 지점에서 보존한다.
- 기본값 1과 기존 단일 frame/caption 소비자가 계속 동작한다.
- frame 수 변경이 task config, Stage version, artifact checksum과 ZIP integrity에 반영된다.
- fixture와 실제 sample에서 keyframe→caption→timeline 순서를 재현할 수 있다.

비용과 제약:

- 최대값 3에서는 caption 추론량과 JPEG 저장량이 최대 세 배가 된다.
- 시간 기반 선택은 화면 내용 중복을 제거하지 않는다. 다음 slice에서 perceptual hash를 추가한다.
- 현재 caption Stage는 전체 이미지 배열을 한 batch로 요청한다. Provider batch tuning은 후속 항목이다.

## 구현 위치

- adaptive 추출: `src/pipeline/stages/s03_keyframes.py`
- caption 정규화: `src/pipeline/stages/s08_captions.py`
- scene summary: `src/pipeline/stages/s09_timeline.py`
- Stage version/bundle: `src/video_preprocess/engine/defaults.py`,
  `src/video_preprocess/adapters/legacy_stages.py`
- contract fixture: `tests/fixtures/adaptive_visuals.json`
