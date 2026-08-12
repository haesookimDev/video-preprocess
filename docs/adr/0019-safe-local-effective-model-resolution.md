# ADR-0019: local model fingerprint는 실행과 동일함을 증명할 때만 resolve한다

- 상태: Accepted
- 결정일: 2026-08-12
- 관련 문서:
  [`../07-execution-inference-contracts.md`](../07-execution-inference-contracts.md)

## 배경

manifest cache는 requested alias뿐 아니라 실제 provider/model/revision/runtime이 같아야 안전하다.
그러나 CLI process가 새로 시작될 때 local provider는 아직 model을 로드하지 않았고, Hub의
`main`이나 tag는 원격에서 바뀔 수 있다. dry-run을 위해 model을 로드하거나 metadata 요청을 하면
read-only·offline 동작을 깨고, 단순히 이전 manifest 값을 신뢰하면 거짓 cache hit가 발생한다.

## 결정

- Provider의 `effective_model()`은 현재 실행이 사용할 fingerprint를 부작용 없이 증명할 수 있을
  때만 `EffectiveModel`을 반환하는 optional capability다.
- 이미 로드된 provider는 실제 load 결과의 revision과 현재 runtime을 반환한다.
- 40자리 immutable Hub commit은 local cache에 해당 snapshot이 있을 때 resolve한다.
- `HF_HUB_OFFLINE` 또는 `TRANSFORMERS_OFFLINE` 환경에서는 local Hub ref가 실행 중 갱신되지
  않으므로 cached snapshot commit을 resolve한다.
- 온라인의 mutable `default`, `main`, tag는 사전 네트워크 조회 없이 `None`을 반환한다.
- VAD는 packaged ONNX asset을 읽기만 하여 SHA-256 revision을 계산한다.
- diarization credential이 없으면 cached file이 있어도 접근 가능한 현재 deployment를 증명할 수
  없으므로 `None`을 반환한다.
- `InferenceGateway.effective_model()`이 optional capability를 호출하고,
  `GatewayEffectiveModelResolver`가 Stage slot별 `ModelExecution`으로 변환한다.
- resolver가 없거나 실패하거나 한 slot이라도 미확정이면 Engine cache evaluator는 기존
  `EFFECTIVE_MODELS_UNAVAILABLE` miss를 유지한다.

## 결과

긍정적 영향:

- dry-run과 실제 실행이 같은 provider instance 또는 같은 local snapshot fingerprint를 사용한다.
- model load, network 요청, cache mutation 없이 model Stage cache hit을 확인할 수 있다.
- 온라인 mutable revision과 credential 변경이 stale model 결과 재사용으로 숨지 않는다.

비용과 제약:

- 온라인에서 revision을 pin하지 않은 모델은 cached snapshot이 있어도 안전한 miss가 된다.
- local directory model은 content hash 계약이 없어 load 전에는 resolve하지 않는다.
- HTTP Provider는 Phase 4에서 서버 capability/health 응답으로 같은 optional capability를 구현해야
  한다.

## 구현 위치

- local fingerprint probes: `src/video_preprocess/inference/local/fingerprints.py`
- Gateway adapter: `src/video_preprocess/inference/model_resolver.py`
- local composition: `src/video_preprocess/services/local.py`
