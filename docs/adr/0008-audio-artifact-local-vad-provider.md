# ADR-0008: VAD는 audio ArtifactRef와 portable option을 전달한다

- 상태: Accepted
- 결정일: 2026-08-06
- 관련 문서:
  [`../07-execution-inference-contracts.md`](../07-execution-inference-contracts.md)

## 배경

기존 `s05_vad`는 faster-whisper의 audio decoder, `VadOptions`와 Silero timestamp 함수를
직접 import해 로컬 WAV 경로를 처리했다. STT·diarization 등 나머지 모델 Stage를 Provider로
옮긴 뒤에도 이 의존성이 남아 Phase 2 완료 조건을 충족하지 못하고, VAD만 원격 endpoint로
전환할 수도 없었다. 기존 음성 구간과 설정 의미를 바꾸지 않는 작은 계약이 필요하다.

## 결정

### 요청과 응답

- alias는 `vad.default`, task는 `voice_activity_detection`이다.
- `inputs.audio`는 16kHz mono WAV의 단일 `ArtifactRef`다.
- v1 parameter는 `min_silence_duration_ms`, `speech_pad_ms`, `sampling_rate`이며 sampling
  rate는 16000으로 고정한다.
- `outputs.segments`는 1부터 연속인 `segment_id`, 절대 `start_sec`, `end_sec`,
  `duration_sec`을 가진 시간순·비중첩 배열이다.
- `outputs.total_sec`, `speech_sec`, `speech_ratio`를 함께 반환한다.

### 로컬 Provider

- `LocalVADProvider`는 Artifact Store에서 오디오 존재 여부와 checksum을 검증한 뒤 stream으로
  decode한다.
- faster-whisper, PyAV, NumPy와 ONNX Runtime import는 Provider의 lazy loader 안에 둔다.
- loader는 faster-whisper의 process-level cached Silero session을 준비하고 Provider가 backend
  binding을 재사용한다.
- 패키지에 포함된 `silero_vad_v6.onnx` 파일의 SHA-256을 effective revision으로 기록한다.
- faster-whisper·ONNX Runtime 버전을 runtime으로 기록한다.

### 기존 runner 연결

- runner composition root가 기존 Local Artifact Store를 사용하는 VAD Service를
  `PipelineContext`에 주입한다.
- `s05_vad`는 기존 option 선택, 로그와 JSON 조립만 소유하고 구체 모델을 import하지 않는다.
- 기존 `has_audio`, duration·ratio, `options`, `segments`를 유지하고 성공 출력에 `model`,
  `provider`, `revision`, `runtime`을 additive하게 추가한다.
- 오디오가 없는 경우의 기존 skip 출력은 그대로 유지한다.

## 고려한 대안

### VAD를 Stage 내부의 작은 연산으로 유지

코드량은 적지만 local/HTTP 전환 축에서 VAD만 예외가 되고 Stage가 구체 ML 라이브러리를 직접
소유한다. Phase 2 경계를 일관되게 끝내기 위해 채택하지 않는다.

### Decode된 float 배열을 요청에 inline으로 전달

payload와 직렬화 비용이 크고 대용량 입력은 ArtifactRef로 전달한다는 원칙을 위반한다.

### STT Provider가 VAD까지 내부 수행

한 endpoint로 합칠 수 있지만 VAD 결과 artifact와 설정별 cache가 사라지고, diarization이나
다른 후속 처리에서 독립 VAD 결과를 재사용할 수 없다. 두 task를 별도로 유지한다.

## 결과

긍정적 영향:

- 모든 모델 Stage에서 구체 ML 라이브러리 직접 import가 제거되어 Phase 2가 완료된다.
- local과 향후 HTTP VAD Provider가 동일한 artifact/option 계약을 사용한다.
- 기존 VAD 구간과 downstream STT 동작을 유지한다.
- 패키지 내장 모델도 content hash로 정확한 revision을 추적한다.

비용과 제약:

- 모델 import와 ONNX session 생성 시간이 runner 시작이 아니라 첫 VAD timing에 포함된다.
- 전체 WAV를 메모리에 decode하는 기존 동작을 유지한다.
- output tree별 composition은 Provider 객체를 공유하지 않지만 faster-whisper의 process-level
  Silero session cache는 같은 프로세스에서 공유될 수 있다.
- 실행 thread는 timeout 후 강제 중단할 수 없어 cancellation을 지원하지 않는다.

## 구현 위치

- task adapter: `src/video_preprocess/inference/vad.py`
- local provider: `src/video_preprocess/inference/local/vad.py`
- MVP composition: `src/pipeline/inference_setup.py`
- Stage adapter: `src/pipeline/stages/s05_vad.py`
