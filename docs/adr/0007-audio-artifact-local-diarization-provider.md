# ADR-0007: Diarization은 audio ArtifactRef를 전달하고 credential은 Provider가 소유한다

- 상태: Accepted
- 결정일: 2026-08-06
- 관련 문서:
  [`../07-execution-inference-contracts.md`](../07-execution-inference-contracts.md)

## 배경

기존 `s07_diarize`는 프로젝트 `.env`에서 `HF_TOKEN`을 읽고 pyannote pipeline을 직접
생성한 뒤 로컬 WAV 절대 경로를 호출했다. 이 구조는 Stage를 Hugging Face 인증, pyannote와
로컬 파일 시스템에 결합하며, 동일 Stage에서 HTTP Provider를 선택할 수 없게 한다. 게이트
모델의 인증 정보는 추론 요청이나 artifact metadata에 포함하지 않으면서 기존 화자 턴 출력을
유지할 경계가 필요하다.

## 결정

### 요청과 응답

- alias는 `diarization.default`, task는 `speaker_diarization`이다.
- `inputs.audio`는 16kHz mono WAV의 단일 `ArtifactRef`다.
- v1은 task parameter를 받지 않는다.
- `outputs.speakers`는 정렬되고 중복 없는 speaker label 배열이다.
- `outputs.turns`는 `turn_id`, `start_sec`, `end_sec`, `speaker`를 가진 시작 시간순 배열이다.
- overlapping speech를 표현할 수 있도록 서로 다른 turn의 시간 겹침은 허용한다.

### Credential과 로컬 Provider

- `HF_TOKEN`은 composition root가 읽어 `LocalDiarizationProvider` 생성 설정으로만 전달한다.
- token은 `InferenceRequest`, idempotency fingerprint, 응답, 로그와 산출물에 넣지 않는다.
- Provider는 입력 artifact의 존재와 checksum을 검증한 후 임시 workspace에 materialize한다.
- pyannote import와 pipeline load는 lazy하게 수행하고 같은 Provider 인스턴스에서 재사용한다.
- Hugging Face 401, 403/gated, 일반 load 오류를 각각 `AUTHENTICATION_FAILED`,
  `MODEL_ACCESS_DENIED`, `MODEL_UNAVAILABLE`로 정규화한다.
- Hub cache의 snapshot 경로에서 실제 commit을 찾아 effective revision으로 기록하고
  pyannote.audio·torch 버전을 runtime으로 기록한다.

### 기존 runner 연결

- runner composition root가 기존 Local Artifact Store를 사용하는 Diarization Service를
  `PipelineContext`에 주입한다.
- `s07_diarize`는 최종 JSON·로그와 선택 기능의 skip 정책만 소유한다.
- credential 누락·인증 실패, gate 접근 거부와 model load 실패는 기존처럼 skip하고,
  artifact·모델 실행 오류는 실패로 전파한다.
- 기존 `available`, `model`, `speakers`, `turns`를 유지하고 성공 출력에 `provider`,
  `revision`, `runtime`을 additive하게 추가한다.

## 고려한 대안

### Token을 InferenceRequest parameter로 전달

HTTP 직렬화와 로그·trace에 비밀값이 노출될 수 있고 idempotency payload에도 포함된다.
credential은 배포별 Provider 설정이므로 채택하지 않는다.

### 로컬 절대 경로를 Provider에 전달

다른 호스트와 HTTP 모델 서버에서 의미가 없고 내부 경로를 노출한다. Artifact Store가 로컬
Provider workspace로 materialize하도록 한다.

### 화자 분리를 Stage에 선택 구현으로 유지

현재는 선택 기능이지만 모델 lifecycle과 gate 오류가 Stage에 남으면 Phase 2의 의존성 제거
완료 조건을 충족하지 못하고 향후 서버 endpoint 전환도 별도 Stage 분기를 요구한다.

## 결과

긍정적 영향:

- Stage에서 pyannote, Hugging Face와 credential 처리가 제거됐다.
- local과 향후 HTTP Provider가 동일한 audio/turn 계약을 사용할 수 있다.
- token 누락과 접근 거부의 기존 graceful degradation을 유지한다.
- 실제 model commit과 runtime을 관측할 수 있다.

비용과 제약:

- Local Provider는 generic Artifact Store 지원을 위해 WAV를 임시 workspace에 materialize한다.
- output tree별 composition은 서로 pipeline cache를 공유하지 않는다.
- `asyncio.to_thread`에서 시작된 pipeline은 timeout 후 강제 중단할 수 없어 cancellation을
  지원하지 않는다.
- pyannote가 참조하는 하위 모델의 개별 revision은 현재 effective model 필드 하나에 모두
  표현하지 않는다.

## 구현 위치

- task adapter: `src/video_preprocess/inference/diarization.py`
- local provider: `src/video_preprocess/inference/local/diarization.py`
- MVP composition: `src/pipeline/inference_setup.py`
- Stage adapter: `src/pipeline/stages/s07_diarize.py`
