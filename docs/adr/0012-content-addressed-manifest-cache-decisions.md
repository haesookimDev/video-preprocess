# ADR-0012: cache key는 Stage 결과 의미만 hash하고 hit 전에 artifact를 검증한다

- 상태: Accepted
- 결정일: 2026-08-06
- 관련 문서:
  [`../07-execution-inference-contracts.md`](../07-execution-inference-contracts.md)

## 배경

기존 runner는 output 경로의 파일 존재만으로 Stage를 건너뛴다. 이 방식은 입력 영상, 상위
artifact, 설정 또는 모델이 바뀌어도 stale 결과를 재사용하며, 파일 일부가 삭제되거나 변조된
상태도 정확히 설명하지 못한다. PipelineEngine에 persistence를 연결하기 전에 cache key와
hit/miss 판정을 독립적으로 검증할 수 있어야 한다.

## 결정

### Content-addressed cache key

`compute_stage_cache_key`는 다음 Stage 결과 의미를 canonical JSON으로 만들고 SHA-256으로
hash한다.

- cache와 StageTask schema version
- stable Stage name과 Stage version
- logical input key별 schema, kind, media type, size와 checksum
- Stage config
- requested model binding

run ID, stage run ID, attempt, trace, idempotency key, artifact ID와 URI는 제외한다. 따라서 다른
run과 저장 위치라도 입력 내용과 실행 의미가 같으면 같은 key를 갖는다.

### Manifest cache decision

`ManifestCacheEvaluator`는 manifest 검색이나 실행을 소유하지 않는다. 호출자가 현재 StageTask,
후보 `StageManifest`, 현재 기대하는 effective model fingerprint와 force 여부를 전달한다.

hit은 다음 조건을 모두 만족해야 한다.

- 저장된 task semantics와 cache key가 현재 task와 일치한다.
- result status가 `succeeded`다.
- 모델 Stage는 현재 기대 provider/model/revision/runtime과 manifest의 `ModelExecution`이 같다.
- 현재 input과 manifest output을 `ArtifactStore.verify`로 검사했을 때 존재, 크기와 checksum이
  모두 일치한다.

판정은 `hit`, `miss`, `forced` 중 하나와 stable `CacheMissReason`, 관련 logical key와 안전한
detail을 반환한다. verification exception은 예외 본문이나 경로를 노출하지 않고 type만 남긴다.

### Skip 정책

현재 `StageResult`에는 credential 또는 provider 상태를 비교할 수 있는 구조화된 recheck
fingerprint가 없다. 따라서 `skipped` manifest는 이유와 관계없이
`SKIPPED_RECHECK_REQUIRED` miss로 처리한다. 이는 불필요한 재평가보다 stale skip을 피하는
정확성을 우선한다. recheck 계약이 추가되면 조건이 동일한 skip만 제한적으로 재사용한다.

## 고려한 대안

### StageTask idempotency key를 cache key로 재사용

idempotency key에는 run-local identity와 attempt가 포함돼 다른 run의 동일 결과를 재사용할 수
없다. 실행 중복 방지와 결과 호환성은 별도 identity로 유지한다.

### requested model alias만 비교

같은 alias가 local에서 HTTP로, 또는 새 revision으로 재배치될 수 있어 결과 호환성을 보장하지
못한다. composition 계층이 현재 effective model fingerprint를 resolve해 제공해야 한다.

### output 파일 존재만 확인

부분 write와 변조를 구분하지 못하므로 size와 checksum을 함께 검증한다.

## 결과

긍정적 영향:

- 입력·설정·Stage·model 변화와 artifact 손상을 명시적인 miss로 분류한다.
- cache 판단을 fake ArtifactStore로 빠르고 네트워크 없이 검증할 수 있다.
- dry-run, metrics와 운영 로그가 같은 reason code를 사용할 수 있다.
- 다른 run의 동일 content를 재사용할 수 있는 key 기반을 제공한다.

비용과 제약:

- hit마다 input/output checksum 검증 비용이 든다.
- 모델 Stage의 hit에는 현재 effective model fingerprint를 먼저 resolve해야 한다.
- RunStore 후보 manifest 조회, PipelineEngine skip/persistence와 legacy runner 연결은 후속 작업이다.
- skipped result는 구조화된 recheck 계약 전까지 항상 다시 평가한다.

## 구현 위치

- cache key와 evaluator: `src/video_preprocess/engine/cache.py`
- contract tests: `tests/engine/test_cache.py`
