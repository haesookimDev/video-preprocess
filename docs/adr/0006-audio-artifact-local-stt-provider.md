# ADR-0006: STT는 audio ArtifactRef와 VAD chunk를 함께 전달한다

- 상태: Accepted
- 결정일: 2026-08-06
- 관련 문서:
  [`../07-execution-inference-contracts.md`](../07-execution-inference-contracts.md)

## 배경

기존 `s06_stt`는 WAV 경로를 직접 decode하고 `WhisperModel`을 생성한 뒤 VAD 구간마다
`transcribe()`를 호출했다. 이 구조는 Stage를 faster-whisper와 로컬 파일 시스템에 결합하며,
HTTP Provider가 같은 요청을 처리할 수 없다. 전체 오디오 byte를 inline으로 보내지 않으면서
기존의 VAD 구간 최적화와 절대 시간축 출력을 유지할 계약이 필요하다.

## 결정

### STT 요청과 응답

- alias는 `stt.default`, task는 `speech_to_text`다.
- `inputs.audio`는 16kHz mono WAV의 단일 `ArtifactRef`다.
- `inputs.chunks`는 `start_sec`, `end_sec`, `source_ids`를 가진 정렬되고 겹치지 않는 배열이다.
- `parameters`는 `language`, `beam_size`, `sampling_rate`를 포함하며 v1 sampling rate는
  16000으로 고정한다.
- Provider는 chunk-relative Whisper 시간을 원본의 절대 시간으로 보정한다.
- `outputs.segments`는 기존 `start_sec`, `end_sec`, `text`, `avg_logprob`,
  `no_speech_prob`, `vad_source_ids`를 유지한다.
- 첫 chunk의 감지 언어와 확률을 `outputs.language`, `outputs.language_probability`에 기록한다.

### 로컬 Provider

- `LocalSTTProvider`는 Artifact Store에서 오디오의 존재 여부, 크기와 checksum을 검증한 뒤
  stream으로 한 번 decode하고 각 chunk의 sample slice만 모델에 전달한다.
- faster-whisper import, model download/load와 audio decoder는 Provider의 lazy loader 안에 둔다.
- model은 provider 인스턴스에서 재사용하며 성공 응답은 idempotency key로 보관한다.
- `device`와 `compute_type`은 Provider binding 설정이며 기본값은 `auto`, `int8`이다.
- 기본 loader는 Hugging Face snapshot 경로에서 실제 commit을 찾아 resolved revision으로
  기록하고 faster-whisper·CTranslate2 버전을 runtime으로 기록한다.

### 기존 runner 연결

- runner composition root가 Caption Service와 같은 Local Artifact Store를 사용하는 STT Service를
  `PipelineContext`에 주입한다.
- `s06_stt`는 VAD 병합과 최종 JSON·로그 조립을 유지하되 faster-whisper를 import하거나 구체
  Provider를 생성하지 않는다.
- 기존 WAV는 `LegacyOutputAdapter.register_file()`로 byte를 복사하지 않고 ArtifactRef로
  등록한다.
- 기존 transcript 필드를 유지하고 `provider`, `revision`, `runtime`,
  `language_probability`를 additive하게 추가한다.

## 고려한 대안

### VAD chunk마다 별도 오디오 artifact 생성

Provider 구현은 단순해지지만 Stage가 WAV slicing과 임시 artifact 수명주기를 떠안고 원격 저장
I/O가 chunk 수만큼 증가한다. 하나의 immutable audio 참조와 작은 구간 JSON을 전달하는 편이
효율적이므로 채택하지 않는다.

### decode된 float 배열을 inline JSON으로 전달

모델 호출은 쉬워지지만 payload와 직렬화 비용이 매우 커지고 대용량 데이터는 ArtifactRef로
전달한다는 ADR-0001 원칙을 위반한다.

### Provider가 자체 VAD를 다시 실행

원격 모델 서버에는 편리할 수 있지만 현재 `s05_vad` 결과와 설정을 무시해 출력과 cache 의미가
달라진다. v1은 Engine이 전달한 명시적 chunk만 전사한다.

## 결과

긍정적 영향:

- Stage에서 faster-whisper·PyAV와 모델 lifecycle이 제거됐다.
- local과 향후 HTTP STT Provider가 같은 audio/chunk 요청을 사용할 수 있다.
- 기존 segment 구조와 절대 시간축을 그대로 유지한다.
- device, compute type, resolved model commit과 runtime을 관측할 수 있다.

비용과 제약:

- Provider가 audio 전체를 한 번 decode하므로 매우 긴 영상은 decoded buffer 메모리를 사용한다.
- 첫 chunk의 언어를 전체 실행 언어로 사용하는 기존 동작을 유지한다.
- output tree별 composition은 서로 model cache를 공유하지 않는다.
- `asyncio.to_thread`에서 시작된 모델 호출은 timeout 후 강제 중단할 수 없어 cancellation을
  지원하지 않는다.

## 구현 위치

- task adapter: `src/video_preprocess/inference/stt.py`
- local provider: `src/video_preprocess/inference/local/stt.py`
- MVP composition: `src/pipeline/inference_setup.py`
- Stage adapter: `src/pipeline/stages/s06_stt.py`
