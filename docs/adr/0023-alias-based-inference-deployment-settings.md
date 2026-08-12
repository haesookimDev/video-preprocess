# ADR-0023: 추론 배포 설정은 모델 alias별로 Stage 외부에서 선택한다

- 상태: Accepted
- 결정일: 2026-08-12
- 관련 문서:
  [`../06-target-architecture.md`](../06-target-architecture.md),
  [`../07-execution-inference-contracts.md`](../07-execution-inference-contracts.md),
  [`0022-http-inference-v1-job-contract.md`](./0022-http-inference-v1-job-contract.md)

## 배경

HTTP Provider client가 생겨도 `s10_index`가 `get_local_embedding_service()`를 직접 호출하면 같은
Stage를 원격 실행으로 전환할 수 없다. 알고리즘 설정인 embedding model 이름과 배포 설정인 endpoint,
credential, timeout을 하나의 Stage config에 넣으면 manifest와 로그에 비밀값이 섞이고 local/remote에
따라 cache key 의미도 달라진다.

## 결정

- `PipelineSettings`는 기존 Stage 알고리즘 설정만 소유한다.
- `PipelineRunRequest.deployments`는 별도 `InferenceDeploymentSettings`를 받는다.
- `http_providers[alias]`에 `HTTPProviderSettings`가 있으면 composition root가 해당 alias를 HTTP
  Provider에 연결하고, 없으면 기존 Local Provider를 사용한다.
- 현재 첫 적용 alias는 `embedding.default`다. 같은 map을 caption, STT, diarization과 VAD로 확장한다.
- Stage는 Provider 종류를 import하거나 선택하지 않고 `PipelineContext`에 주입된 task-specific
  Service만 호출한다.
- endpoint는 공개 배포 metadata지만 bearer token은 `repr`, 공개 설정, manifest와 dry-run 출력에서
  제외한다. CLI는 token 값이 아니라 값을 읽을 환경변수 이름만 받는다.
- local→HTTP 장애 시 자동 fallback하지 않는다. 의도하지 않은 모델 변경과 cache 오염을 피하기 위해
  선택한 Provider의 표준 실패를 그대로 반환한다.
- remote capability의 effective model은 기존 `GatewayEffectiveModelResolver`를 통해 Engine cache
  evaluator에 전달한다.

## 결과

긍정적 영향:

- `s10_index` 코드 변경 없이 CLI와 query가 같은 embedding alias를 local/HTTP로 전환한다.
- 알고리즘 config와 배포 credential의 수명주기와 노출 범위가 분리된다.
- endpoint 기반 설정을 Application Service request에 넣어 향후 API adapter도 같은 composition을
  재사용할 수 있다.
- 원격 resolved revision이 기존 model fingerprint와 cache miss/hit 규칙에 참여한다.

비용과 제약:

- 현재 배포 map을 실제로 해석하는 alias는 embedding 하나뿐이다.
- remote endpoint는 `embedding.default` capability와 HTTP Inference v1을 제공해야 한다.
- secret manager 연동 전까지 CLI credential source는 환경변수다.
- provider-aware preflight와 production model server lifecycle은 후속 작업이다.

## 구현 위치

- 설정 계약: `src/video_preprocess/inference/deployment.py`
- CLI 환경변수 adapter: `src/pipeline/deployment.py`
- runtime composition: `src/video_preprocess/services/local.py`
- Stage 주입점: `src/pipeline/context.py`, `src/pipeline/stages/s10_index.py`
- 통합 검증: `tests/inference/test_embedding_deployment_integration.py`
