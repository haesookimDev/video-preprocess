# ADR-0032: caption 장치 선택과 ordered chunking을 배포 경계에서 처리한다

- 상태: Accepted
- 결정일: 2026-08-19
- 관련 문서:
  [`../06-target-architecture.md`](../06-target-architecture.md),
  [`../07-execution-inference-contracts.md`](../07-execution-inference-contracts.md)

## 배경

adaptive keyframe과 perceptual 중복 제거 이후 08단계는 영상마다 가변 개수의 이미지를 하나의
ordered 집합으로 Caption Service에 전달한다. 기존 LocalCaptionProvider는 장치를 지정하지 않아
transformers 기본값에 의존했고 최대 batch 16장을 넘는 입력은 Gateway에서 거부했다. 긴 영상에서는
실패할 수 있고 CPU, CUDA, MPS 배포별 성능과 Engine cache 의미도 명확하지 않았다.

Stage가 장치나 provider 제한을 알게 하면 로컬·HTTP 배포 경계가 다시 파이프라인 로직에 섞인다.
반대로 모든 이미지를 무조건 한 번에 처리하면 provider memory와 capability 차이를 흡수할 수 없다.

## 결정

### 책임 분리

- 08 Stage는 keyframe 전체를 하나의 ordered 논리 집합으로 Caption Service에 전달한다.
- Caption Service는 Provider capability를 조회하고 ordered chunking, 전체 deadline, 결과 집계를
  소유한다.
- LocalCaptionProvider는 실제 모델 장치 선택, 모델 lifecycle과 한 chunk의 batch 추론을 소유한다.
- CLI와 reference pipeline server의 composition root가 local caption의 `device`와 `batch_size`를
  배포 설정으로 주입한다. 두 값은 PipelineSettings나 공개 Pipeline API 요청 필드가 아니다.

### 장치 선택

- local 기본값은 `auto`다.
- `auto`는 torch가 보고하는 가용성을 기준으로 `CUDA → MPS → CPU` 순서에서 처음 가능한 장치를
  한 번 선택하고 같은 Provider instance에서 재사용한다.
- `cpu`, `cuda`, `mps` 같은 명시적 값은 그대로 사용한다. 명시 장치의 모델 load·연산 실패나
  memory 부족을 CPU로 자동 재시도하지 않는다.
- 따라서 fallback은 `auto`의 가용성 선택에만 존재한다. 실패 시 운영자가 `--caption-device cpu`
  또는 더 작은 `--caption-batch-size`를 선택한다.
- 직접 Provider를 구성할 때 `device=None`은 기존 호환을 위해 model default 의미를 유지한다.

resolved device는 `EffectiveModel.runtime`의
`transformers/<version>;device=<resolved-device>`에 포함한다. Engine은 이를 기존 model execution
fingerprint에 사용하므로 서로 다른 실제 장치 결과를 재사용하지 않는다. `auto`와 명시적 `cpu`가
동일한 runtime으로 resolve되면 같은 모델 실행으로 간주할 수 있다.

### ordered chunking과 실패

- local 기본 batch 크기는 4다.
- Caption Service는 `min(configured_batch_size, provider.max_batch_size)`를 chunk 크기로 사용한다.
  설정값이 없으면 provider 최대값을 사용한다.
- chunk는 입력 순서대로 순차 실행하고 결과를 같은 순서로 결합한다. 각 chunk의 idempotency key는
  그 chunk의 artifact 집합과 요청 의미에서 결정적으로 계산한다.
- capability 조회와 모든 chunk는 하나의 service total deadline을 공유한다.
- 모든 chunk의 effective model metadata는 같아야 한다. 중간 chunk가 실패하거나 metadata가
  바뀌면 aggregate `CaptionBatch`를 반환하지 않고 즉시 실패한다. 성공한 앞 chunk가 Provider 내부
  idempotency cache에 남을 수는 있지만 Stage의 부분 산출물은 publish하지 않는다.
- LocalCaptionProvider가 memory 부족을 감지하면 기존 비재시도 `INFERENCE_FAILED`를 유지하면서
  details에 `reason=DEVICE_OUT_OF_MEMORY`, 실제 device와 provider `max_batch_size`를 기록한다.

성공한 aggregate usage는 `input_count`, 가장 큰 실제 `batch_size`, `batch_count`, `batch_sizes`,
`configured_batch_size`, `provider_max_batch_size`와 가능한 경우 `device`를 제공한다. timing은 service
`total_sec`, chunk 합계 `model_load_sec`·`inference_sec`와 chunk별 `batches`를 제공한다. 08단계는 이를
`captions.json`에 보존하고 `caption_batch_count` metric을 기록한다.

### Stage version과 cache

batch 크기는 동일한 모델·입력에서 처리량을 조절하는 배포 튜닝값이므로 StageTask config나 content
cache key에 넣지 않는다. batch benchmark나 강제 재실행이 필요하면 `--force-stage 08_captions`를
사용한다. resolved device는 위와 같이 effective runtime에 포함하므로 별도의 task config 없이도
cache를 분리한다.

08 Stage version은 `1.3.0`으로 올린다. 이는 `captions.json`의 aggregate usage/timing과 metric 계약,
device-aware runtime 도입을 기존 cache에 한 번 반영하기 위한 변경이다.

## 고려한 대안

### Stage가 provider 최대 batch와 장치를 선택

Stage가 배포 위치와 하드웨어 capability를 알아야 하므로 Engine·Executor·Provider 분리 원칙과
충돌한다.

### Provider가 임의 크기의 전체 배열을 내부 chunking

Gateway가 wire request 하나의 capability limit를 먼저 검증하므로 기존 계약과 맞지 않는다. HTTP를
포함한 Provider 구현마다 집계·deadline·부분 실패 동작도 중복된다. task별 Service에서 공통화한다.

### OOM 발생 시 batch를 줄여 자동 재시도

동일 요청 안에서 실행 전략과 timing이 비결정적으로 바뀌고 이미 성공한 chunk의 비용을 숨긴다.
현재는 stable 오류와 운영 설정을 제공하고 adaptive OOM retry는 별도 정책으로 남긴다.

### batch 크기를 Stage cache key에 포함

캡션 의미가 아닌 처리량 설정 때문에 동일 산출물을 중복 계산한다. model/device/runtime과 Stage
version이 의미 변경을 담당하고 batch는 관측 metadata로만 기록한다.

## 결과

긍정적 영향:

- 긴 ordered 입력도 Provider 제한 안에서 순서를 보존해 처리한다.
- Stage와 공개 pipeline 요청이 local 장치·batch 세부사항에서 분리된다.
- 실제 장치가 manifest cache fingerprint와 산출물 runtime에 남는다.
- batch 경계, 순서, 중간 실패와 metadata 불일치를 모델·네트워크 없이 검증할 수 있다.

비용과 제약:

- capability 조회도 total deadline을 소비한다.
- chunk는 현재 순차 실행하므로 Provider 내부 병렬화 외의 동시 batch 실행은 하지 않는다.
- 자동 OOM recovery는 없고 운영자가 batch/device를 조정해야 한다.
- 현재 개발 장비에서는 CUDA와 MPS가 모두 가용하지 않아 CPU 실제 비교만 수행했다.

## 검증 결과

`samples/sample.mp4`에서 pHash 후 남은 4장을 offline CPU로 비교했다.

| 설정 | chunk | total | model load | inference |
|---|---|---:|---:|---:|
| `cpu`, batch 1 | `[1,1,1,1]` | 10.402s | 7.228s | 3.141s |
| `cpu`, batch 4 | `[4]` | 7.802s | 5.334s | 2.450s |
| `auto`, batch 4 | `[4]` → CPU | 7.229s | 5.085s | 2.127s |

별도 cold process 측정이므로 model load 편차가 포함된다. 같은 caption 순서와 문자열을 확인했고
batch 4의 순수 inference 시간은 batch 1보다 약 22% 짧았다. 현재 장비의 torch probe는 CUDA와
MPS 모두 unavailable이라 실제 MPS 수치는 기록하지 않는다.

## 구현 위치

- ordered Service: `src/video_preprocess/inference/caption.py`
- local device와 batch Provider: `src/video_preprocess/inference/local/caption.py`
- local composition: `src/video_preprocess/services/local.py`
- CLI/server adapter: `src/run_pipeline.py`, `src/serve_pipeline.py`
- Stage output: `src/pipeline/stages/s08_captions.py`
- contract tests: `tests/inference/test_local_caption.py`
