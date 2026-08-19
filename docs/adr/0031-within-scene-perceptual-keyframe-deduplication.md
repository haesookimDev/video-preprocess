# ADR-0031: 장면 내부 키프레임 후보를 perceptual hash로 중복 제거한다

- 상태: Accepted
- 결정일: 2026-08-19
- 관련 문서:
  [`../01-video.md`](../01-video.md),
  [`../07-execution-inference-contracts.md`](../07-execution-inference-contracts.md),
  [`0030-duration-adaptive-keyframes-and-scene-caption-summary.md`](./0030-duration-adaptive-keyframes-and-scene-caption-summary.md)

## 배경

ADR-0030의 `duration-adaptive-v1`은 긴 장면에서 최대 세 시각을 선택해 화면 변화를 보존하지만,
서로 다른 시각의 화면 내용이 같을 수 있다. 이 후보를 그대로 저장하고 캡션하면 JPEG 크기와 VLM
추론 비용만 늘어난다. 반대로 장면 경계를 넘어 중복을 제거하면 서로 다른 의미 구간의 대표 화면을
잃을 수 있다.

중복 제거 결과는 Stage cache, deterministic ZIP과 08 caption 입력 집합에 영향을 준다. 따라서
알고리즘·임계값·비교 순서뿐 아니라 최소 보존 수, 파일명 재할당, 제거 사유와 통계까지 재현 가능한
Stage 계약이어야 한다.

## 결정

### 1. 해시 알고리즘과 거리

03단계는 FFmpeg로 추출한 각 후보를 32x32 grayscale로 축소하고 2차원 DCT의 좌상단 8x8 계수를
사용하는 `phash-64-dct-v1` 64비트 해시를 계산한다. DC를 제외한 63개 계수의 median보다 큰지를
64개 계수 각각의 bit로 기록하고 16자리 lowercase hexadecimal 문자열로 직렬화한다.

두 해시는 XOR bit count인 Hamming 거리로 비교한다. 거리가 `6` 이하이면 중복이며 `7` 이상이면
보존한다. 이 임계값은 현재 Stage policy 상수다. 사용자 설정으로 노출하지 않고 정책이 달라질 때
Stage version을 올린다.

### 2. 범위, 순서와 대표 선택

중복 제거 범위는 같은 `scene_id` 안으로 제한한다. 후보를 timestamp 오름차순으로 순회하며 첫
후보를 항상 보존한다. 이후 후보는 이미 보존된 후보 전체와 비교한다.

- 최단 거리가 임계값 이하면 현재 후보를 제거하고 가장 가까운 보존 후보를 대표로 기록한다.
- 같은 최단 거리에서는 먼저 나온 보존 후보를 대표로 사용한다.
- 제거 후보는 다음 비교의 대표가 되지 않는다.
- 첫 후보를 무조건 보존하므로 유효한 각 장면은 최소 한 장을 유지한다.

후보 JPEG는 Stage 전용 임시 디렉터리에 추출한다. 모든 후보 추출과 비교가 성공한 뒤 보존 수에
맞춰 `keyframe_index`·`keyframe_count`와 filename을 다시 부여한다. 최종 한 장이면 기존
`scene_NNN.jpg`, 두 장 이상이면 `scene_NNN_II.jpg`를 사용한다. 제거 후보는 최종 frames 디렉터리에
publish하지 않는다.

### 3. 산출물과 관측 계약

보존된 각 `keyframes` 항목에 `perceptual_hash`를 추가한다. 최상위 `deduplication`은 다음 정보를
기록한다.

```json
{
  "algorithm": "phash-64-dct-v1",
  "hash_bits": 64,
  "hamming_distance_threshold": 6,
  "comparison_scope": "within_scene",
  "comparison_order": "timestamp_ascending_against_retained",
  "minimum_retained_per_scene": 1,
  "candidate_count": 3,
  "retained_count": 2,
  "removed_count": 1,
  "scene_statistics": [
    {"scene_id": 4, "candidate_count": 3, "retained_count": 2, "removed_count": 1}
  ],
  "removed": [
    {
      "scene_id": 4,
      "candidate_index": 2,
      "candidate_count": 3,
      "timestamp_sec": 15.0,
      "perceptual_hash": "0000000000000003",
      "duplicate_of_keyframe_index": 1,
      "duplicate_of_timestamp_sec": 7.5,
      "duplicate_of_path": "03_keyframes/frames/scene_004_01.jpg",
      "hamming_distance": 2,
      "reason": "perceptual_hash_distance_lte_threshold"
    }
  ]
}
```

Stage metrics는 후보·보존·제거 수와 scene 수를 제공한다. `keyframe_images` ZIP은 기존처럼
`keyframes` 배열의 path만 포함하고 08단계도 그 배열만 caption batch로 전달하므로 제거 후보는
저장·복원·추론 경계를 통과하지 않는다.

### 4. 버전과 호환성

03 Stage version은 `1.3.0`으로 올려 기존 `1.2.0` cache를 재사용하지 않는다. 08·09는 이미 가변
keyframe 집합과 additive field를 허용하므로 version `1.2.0`을 유지한다. 새 03 output checksum이
downstream task input에 포함되어 실제 시각 집합이 달라지면 08 이후 cache가 자연스럽게 무효화된다.

기본 `keyframes_per_scene=1`에서는 비교 대상이 없으므로 기존 중앙 timestamp와
`scene_NNN.jpg`가 유지된다. JSON의 hash·deduplication metadata와 Stage metrics는 additive 변경이다.

## 고려한 대안

### 인접 후보 하나와만 비교

첫 장면과 셋째 장면이 같고 가운데만 다른 경우 중복을 놓친다. 최대 세 후보라서 모든 보존 후보와의
비교 비용은 작고 결과가 더 안정적이다.

### 영상 전체에서 비교

반복되는 타이틀이나 발표 슬라이드가 서로 다른 scene의 유일한 대표 화면일 수 있다. scene 의미
경계를 보존하기 위해 비교 범위를 장면 내부로 제한한다.

### 제거 후 원래 candidate index와 filename 유지

`keyframe_count`와 실제 index가 불연속이 되어 08·09 정규화와 legacy 단일 filename 계약이
깨진다. 최종 보존 집합을 다시 연속 인덱싱한다.

### image embedding 유사도 사용

별도 모델·revision·device와 inference 비용이 생긴다. 이번 slice는 Pillow만 사용하는 결정적이고
network-free인 pHash로 비용을 먼저 줄인다. 의미 기반 중복 판정이 필요하면 별도 Provider 작업으로
추가한다.

## 결과

긍정적 영향:

- 정적인 긴 scene의 JPEG와 caption inference 수를 줄인다.
- 제거 판단을 JSON과 Stage metrics로 재현하고 scene별 압축률을 관측할 수 있다.
- 최소 한 장, legacy filename, ZIP/input integrity와 downstream 호환성을 유지한다.
- 고정 hash fixture가 임계값 포함 경계와 시간순 대표 선택을 network 없이 검증한다.

비용과 제약:

- 후보는 비교 전에 모두 FFmpeg로 추출하므로 디코딩 횟수 자체는 줄지 않는다.
- grayscale pHash는 색상만 달라지는 화면을 같은 구조로 볼 수 있다. scene 내부 한정으로 손실 범위를
  제한하며 threshold 변경은 새 Stage version과 fixture 검증이 필요하다.
- 후보마다 32x32 DCT 계산이 추가되지만 현재 최대 세 장이라 caption 비용보다 작다.

## 구현 위치

- 추출·해시·중복 제거: `src/pipeline/stages/s03_keyframes.py`
- Stage/cache version: `src/video_preprocess/engine/defaults.py`,
  `src/video_preprocess/adapters/legacy_stages.py`
- 고정 계약 fixture: `tests/fixtures/keyframe_deduplication.json`
- 단위·Stage 회귀: `tests/test_keyframes.py`
