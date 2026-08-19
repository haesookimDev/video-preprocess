# ADR-0005: caption 입력은 ArtifactRef batch로 전달한다

- 상태: Accepted
- 결정일: 2026-08-06
- 관련 문서:
  [`../07-execution-inference-contracts.md`](../07-execution-inference-contracts.md)

## 배경

기존 `s08_captions`는 keyframe의 로컬 경로를 직접 열고 BLIP processor와 model을 단계 안에서
생성했다. 이 방식은 원격 Provider에서 사용할 수 없는 호스트 경로를 계약에 노출하고, 반복
호출마다 모델 lifecycle을 Stage가 관리하게 한다. 여러 keyframe을 한 번에 처리하면서도 local과
HTTP Provider가 같은 요청을 사용하려면 artifact 배열을 표현하는 공통 계약이 필요하다.

## 결정

### caption 요청과 응답

- alias는 `caption.default`, task는 `image_captioning`이다.
- `inputs.images`는 순서가 보존되는 비어 있지 않은 `ArtifactRef` 배열이다.
- 공통 inference 값은 JSON 내부의 중첩 배열·객체에 `ArtifactRef`를 포함할 수 있다.
- `parameters.max_new_tokens`는 1~512의 정수이며 현재 Stage 기본값은 40이다.
- `outputs.captions`는 입력 이미지와 같은 개수·순서의 비어 있지 않은 문자열 배열이다.
- Gateway는 중첩 artifact에도 개별 크기 제한을 적용하고 `images` 배열을 batch 크기로 해석한다.

후속 [`ADR-0032`](./0032-caption-device-selection-and-ordered-chunking.md)는 이 요청 하나의
capability 제한을 유지하면서, 더 긴 ordered 논리 입력을 Caption Service가 여러 wire request로
나누고 all-or-nothing aggregate로 결합하도록 확장한다.

### 로컬 Provider

- `LocalCaptionProvider`는 `ArtifactStore`를 주입받아 각 입력의 존재 여부, 크기와 SHA-256을
  검증한 후 stream으로 이미지를 읽는다.
- Pillow와 transformers import, BLIP processor/model 생성과 batch generate는 Provider의 lazy
  loader 안에 둔다.
- provider 인스턴스는 processor와 model을 재사용하며 성공 응답을 idempotency key로 보관한다.
- 실제 Hugging Face config commit을 resolved revision으로, transformers 버전을 runtime으로
  기록한다.
- 동기 MVP Stage는 `CaptionService.caption()`, 비동기 application은 `caption_async()`를 사용한다.

### 기존 runner 연결

- runner의 `inference_setup`이 Local Artifact Store, legacy registrar와 Caption Service를
  `PipelineContext`에 주입한다.
- `s08_captions`는 구체 Provider를 생성하지 않고 주입된 서비스만 호출한다.
- 기존 keyframe 파일은 `LegacyOutputAdapter.register_file()`로 다시 쓰지 않고 ArtifactRef로
  등록한다.
- 기존 `model`과 `captions` 필드는 유지하고 `provider`, `revision`, `runtime`을 additive하게
  추가한다.

## 고려한 대안

### keyframe마다 요청 하나 생성

공통 계약 확장은 피할 수 있지만 batch generate를 사용할 수 없고 원격 호출 횟수가 이미지 수만큼
증가한다. 이미지 순서와 제한을 한 요청에서 검증하는 편이 명확해 채택하지 않는다.

### 로컬 절대 경로 배열 전달

현재 프로세스에서는 단순하지만 HTTP Provider와 다른 worker에서 의미가 없고 호스트 디렉터리를
노출한다. ADR-0001의 대용량 ArtifactRef 원칙과 충돌하므로 채택하지 않는다.

### 이미지 byte를 base64 JSON으로 inline 전달

공유 스토리지는 피할 수 있지만 요청 크기와 메모리 복사가 커지고 Gateway의 artifact 무결성
검사를 우회한다. 향후 제한된 업로드 API는 별도 adapter로 제공한다.

## 결과

긍정적 영향:

- Stage가 PIL·transformers와 모델 lifecycle에서 분리됐다.
- local과 향후 HTTP caption Provider가 같은 요청 schema를 사용할 수 있다.
- legacy keyframe byte를 복사하지 않고 checksum이 있는 논리 참조로 전환한다.
- sample의 caption 개수, 순서와 문자열을 유지하면서 실제 모델 정보를 기록한다.

비용과 제약:

- inference value 직렬화가 중첩 ArtifactRef를 재귀적으로 처리해야 한다.
- 현재 Local Artifact Store는 output tree별 namespace라 서로 다른 PipelineContext 사이에서
  Caption Provider 모델 인스턴스를 자동 공유하지 않는다. 장기 실행 service는 공유 Store와
  provider를 composition root에서 재사용해야 한다.
- `asyncio.to_thread`로 시작된 로컬 모델 호출은 timeout 후 강제 중단할 수 없어 cancellation을
  지원하지 않는다.
- 장치 자동 선택과 Provider 최대값을 넘는 ordered 입력은 후속
  [`ADR-0032`](./0032-caption-device-selection-and-ordered-chunking.md)에서 구현했다.

## 구현 위치

- task adapter: `src/video_preprocess/inference/caption.py`
- local provider: `src/video_preprocess/inference/local/caption.py`
- MVP composition: `src/pipeline/inference_setup.py`
- Stage adapter: `src/pipeline/stages/s08_captions.py`
