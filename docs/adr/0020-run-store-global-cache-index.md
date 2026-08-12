# ADR-0020: Run Store는 content cache key별 manifest 후보를 인덱싱한다

- 상태: Accepted
- 결정일: 2026-08-12
- 관련 문서:
  [`../07-execution-inference-contracts.md`](../07-execution-inference-contracts.md)

## 배경

Engine은 deterministic cache key를 만들지만 기존 RunStore 조회는 현재 `run_id`와
`stage_run_id/attempt`에 한정됐다. 따라서 같은 Artifact Store에 동일한 입력·설정·binding으로 새
run을 만들어도 결과를 재사용할 수 없었다. model revision과 artifact integrity 판정은 Store가
아니라 기존 cache evaluator에 계속 남아야 한다.

## 결정

- RunStore Port에 `find_stages_by_cache_key(cache_key)` 후보 조회를 추가한다.
- LocalRunStore는 성공하고 cache key가 있는 StageManifest를
  `_manifests/_cache/<encoded-key>.json`에 원자적으로 인덱싱한다.
- 인덱스는 `run_id`, `stage_run_id`, `attempt`만 저장하며 artifact나 model 유효성을 판정하지 않는다.
- Engine은 같은 run 후보를 먼저 평가하고, 이어서 최신 순의 indexed 후보를 평가한다.
- 동일 cache key에 effective model revision이 다른 manifest가 여러 개 있을 수 있으므로 첫 miss에서
  중단하지 않고 검증된 hit를 찾을 때까지 후보를 확인한다.
- 최종 hit은 기존 `ManifestCacheEvaluator`가 task semantics, 현재 effective model fingerprint와
  모든 input/output checksum을 확인한 경우에만 허용한다.
- 재사용 범위는 같은 Run/Artifact Store root다. 다른 output namespace나 외부 object store 사이의
  공유는 해당 Store 구현의 별도 index 정책으로 확장한다.

## 결과

긍정적 영향:

- 다른 run ID도 동일 content와 execution semantics의 산출물을 Executor 호출 없이 재사용한다.
- Store는 후보 검색만, Engine/evaluator는 정책과 무결성 판단만 담당한다.
- model revision별 후보가 공존해 최신 후보가 현재 runtime과 다르더라도 이전 호환 후보를 찾는다.

비용과 제약:

- 기존 manifest는 같은 run이 한 번 재저장되기 전까지 자동 backfill되지 않는다.
- local index 갱신은 process 내 lock과 atomic replace를 사용하지만 cross-process transaction/lease는
  제공하지 않는다.
- 삭제·보존 정책이 추가되면 stale index entry 정리 작업이 필요하다.

## 구현 위치

- RunStore Port: `src/video_preprocess/storage/runs.py`
- local index: `src/video_preprocess/storage/local_runs.py`
- Engine candidate selection: `src/video_preprocess/engine/pipeline.py`
