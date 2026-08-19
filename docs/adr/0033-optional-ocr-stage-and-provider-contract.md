# ADR-0033: OCR을 선택적 독립 Stage와 교체 가능한 Provider로 추가한다

- 상태: Accepted
- 결정일: 2026-08-19
- 관련 문서:
  [`../06-target-architecture.md`](../06-target-architecture.md),
  [`../07-execution-inference-contracts.md`](../07-execution-inference-contracts.md)

## 배경

기존 08 caption은 장면의 의미를 문장으로 요약하지만 슬라이드 제목, 표지판, 메뉴처럼 이미지에
직접 적힌 문자열과 위치·신뢰도를 보존하지 않는다. 이 텍스트를 caption 안에서 추측하거나
09 timeline에서 OCR 엔진을 직접 호출하면 Stage 의미, 모델 배포 위치와 downstream 병합 책임이
섞인다. 또한 OCR을 항상 실행하면 텍스트가 거의 없는 영상에서도 불필요한 비용이 발생한다.

목표 아키텍처는 Stage가 구체 엔진이나 local/HTTP 위치를 선택하지 않고, 큰 이미지 입력을
ArtifactRef로 전달하며, 동일한 모델 alias를 설정만으로 교체하도록 요구한다.

## 결정

### 독립 Stage와 DAG

- 새 Stage 이름은 `08_ocr`, logical output은 `ocr`, version은 `1.0.0`이다.
- `03_keyframes`와 `08_captions` 뒤에 실행하고 `09_timeline`이 `ocr`을 필수 logical input으로
  소비한다. 따라서 기본 DAG는 12개 Stage다.
- caption 의존성은 `caption-hints` trigger 입력을 보장하고 두 visual binding이 같은 keyframe
  bundle을 동시에 복원하는 것을 막는다. `all` 모드에서도 DAG를 모드별로 바꾸지 않는다.
- 기본 `ocr_mode=disabled`는 Provider를 호출하지 않고 `08_ocr/ocr.json`에 `executed=false`,
  `reason_code=OCR_DISABLED`와 빈 결과를 기록한다. 기존 설치에 Tesseract를 필수화하지 않는다.
- `all`은 중복 제거 후 남은 모든 keyframe을, `caption-hints`는 caption에서 고정 영문 keyword를
  찾은 keyframe만 처리한다. 일치 항목이 없으면 `NO_OCR_CANDIDATES` skip을 기록한다.

### 추론 계약과 책임

- 모델 slot과 alias는 `ocr` / `ocr.default`, task는
  `optical_character_recognition`이다.
- OCR Service는 ordered image ArtifactRef 집합, 언어 ID 배열, orientation flag, word confidence
  하한을 받아 Provider capability 기반 chunking, 전체 deadline, deterministic idempotency,
  effective model 일치와 all-or-nothing aggregate를 소유한다.
- Provider는 한 요청의 이미지 decode와 OCR 실행을 소유하고 입력 순서대로 결과를 반환한다.
- 각 이미지 결과는 `artifact_id`, 전체 text, image width/height와 1부터 연속인 word region을 가진다.
  region은 비어 있지 않은 text, 0~1 confidence와 image pixel 공간의
  `{x,y,width,height}` bbox를 가진다.
- Stage는 Provider 이름, Tesseract command, HTTP endpoint를 알지 않는다. Stage는 결과를 원래
  scene/keyframe metadata와 결합해 `ocr.json`으로 publish한다.

### Local/HTTP 배포

- 배포 설정에 `ocr.default` endpoint가 없으면 `LocalOCRProvider`, 있으면 기존
  `HTTPInferenceProvider`를 조합한다. 자동 fallback은 없다.
- Local Provider는 Tesseract CLI에 image bytes를 stdin으로 보내고 TSV를 stdout으로 받는다.
  host 절대 경로를 inference contract에 넣지 않는다.
- 언어는 `-l`, orientation 사용 시 page segmentation mode 1, 미사용 시 mode 3을 사용하고 마지막
  config로 `tsv`를 지정한다.
- Tesseract 설치 version이 effective revision이며 runtime은
  `tesseract-cli/<version>`이다. command와 batch 크기는 composition root의 배포 설정이고
  언어·orientation·confidence는 PipelineSettings와 Stage cache semantics다.
- command 누락은 `OCR_COMMAND_NOT_FOUND`, language data 누락은
  `OCR_LANGUAGE_DATA_UNAVAILABLE` stable details로 정규화한다. 중간 이미지 실패 시 부분 Stage
  산출물을 publish하지 않는다.

### Downstream와 호환성

- `09_timeline` version은 `1.3.0`으로 올리고 scene card에 `ocr_text` scalar summary와 전체
  `visual_ocr` 배열을 추가한다. 기존 keyframe/caption/transcript 필드는 유지한다.
- `ocr_text`는 keyframe 순서에서 공백이 아닌 중복 text를 한 번만 남겨 ` | `로 연결한다.
- `10_index` version `1.2.0`은 OCR text를 embedding·FTS card text에 포함한다.
- `11_context` version `1.2.0`과 QueryService는 `화면 텍스트:` 줄을 보존한다.
- OCR이 disabled이거나 text가 없으면 `ocr_text=null`, `visual_ocr=[]`가 되어 기존 검색·context
  의미를 유지한다.

## 고려한 대안

### 08 caption 안에서 OCR 실행

한 Stage가 문장 캡션과 문자 검출의 서로 다른 모델 lifecycle, 오류와 cache를 함께 소유한다.
OCR만 재실행하거나 별도 endpoint로 보내기 어려워 독립 Stage로 분리한다.

### 09 timeline에서 필요할 때 직접 OCR 실행

규칙 기반 join Stage가 concrete model 배포와 Artifact 등록을 알아야 하므로 채택하지 않는다.

### 모든 영상에서 OCR을 기본 실행

기존 설치에 새 native dependency와 처리 비용을 강제한다. 기본 disabled와 명시적 trigger를 사용한다.

### 인식 문자열만 저장

검색에는 충분하지만 UI overlay, 근거 표시와 provider 품질 비교를 할 수 없다. word confidence와
pixel bbox를 함께 보존한다.

### Local Provider에 파일 경로 전달

로컬에서는 단순하지만 HTTP와 공유 Store로 전환할 수 없고 host 경로가 계약에 노출된다.
ArtifactRef 검증 후 bytes를 stdin으로 전달한다.

## 결과

긍정적 영향:

- caption과 OCR을 독립적으로 cache·재실행·배포할 수 있다.
- 같은 Stage가 local Tesseract와 HTTP OCR endpoint에서 변경 없이 동작한다.
- 화면 문자열이 timeline, 검색 index, static/query context에 additive하게 전파된다.
- 기본 실행은 새 native dependency 없이 기존 의미를 유지한다.

비용과 제약:

- 기본 DAG가 11개에서 12개 Stage로 늘고 09 및 downstream cache가 한 번 무효화된다.
- `caption-hints`는 고정 영문 keyword 기반의 저비용 heuristic이며 언어별 의미 분류기가 아니다.
- Homebrew 기본 Tesseract는 `eng`, `osd`, `snum`만 제공한다. 한국어 등은 별도 language data가
  필요하다.
- reference inference server는 아직 embedding backend만 내장하므로 원격 OCR endpoint는 같은
  Inference v1 계약을 구현한 별도 서비스가 필요하다.

## 검증 결과

- fake runner/provider로 ordered `[2,1]` chunk, idempotency, TSV parsing, confidence filtering,
  bbox, orientation mode, command/language/artifact/timeout 실패와 invalid response를 검증했다.
- local/HTTP alias composition과 loopback HTTP OCR Stage 통합을 검증했다.
- Tesseract 5.5.3에서 합성 `OPENAI OCR 2026` 이미지를 3개 word region으로 인식했고 confidence와
  pixel bbox를 반환했다.
- `samples/sample.mp4`의 중복 제거 후 4개 keyframe을 batch `[4]`로 0.6초에 처리했다. 시험 영상에
  문자가 없어 text/region은 0이었지만 전체 12단계, SQLite integrity와 기존 query가 성공했다.

## 구현 위치

- task Service: `src/video_preprocess/inference/ocr.py`
- local Provider: `src/video_preprocess/inference/local/ocr.py`
- local/HTTP composition: `src/video_preprocess/inference/deployment.py`
- Stage: `src/pipeline/stages/s08_ocr.py`
- DAG/binding: `src/video_preprocess/engine/defaults.py`,
  `src/video_preprocess/adapters/legacy_stages.py`
- downstream: `src/pipeline/stages/s09_timeline.py`, `s10_index.py`, `s11_context.py`
- contract/integration tests: `tests/inference/test_local_ocr.py`, `tests/test_ocr.py`,
  `tests/inference/test_ocr_deployment_integration.py`
