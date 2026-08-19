# ADR-0034: 내장 텍스트 자막과 챕터를 독립 Artifact로 정규화한다

- 상태: Accepted
- 결정일: 2026-08-19
- 관련 문서:
  [`../06-target-architecture.md`](../06-target-architecture.md),
  [`../07-execution-inference-contracts.md`](../07-execution-inference-contracts.md)

## 배경

`01_probe`의 ffprobe 결과에는 subtitle stream과 chapter metadata가 이미 있지만 기존 파이프라인은
개수만 표시하고 downstream에서 사용하지 않았다. 내장 자막은 STT보다 원문에 가까울 수 있고,
챕터는 제작자가 넣은 상위 구조이므로 검색과 context에서 유용하다. 그러나 `09_timeline`이 원본
video를 다시 열거나 FFmpeg command를 직접 선택하면 join 책임과 미디어 추출 책임이 섞인다.

자막은 텍스트 기반 코덱과 비트맵 기반 코덱의 처리 방식도 다르다. PGS/DVD/DVB 자막은 이미지
OCR이 필요해 단순 텍스트 변환과 같은 실패·정확도 계약을 쓸 수 없다.

## 결정

### Stage와 Artifact 경계

- `04_embedded_text` version `1.0.0`을 `01_probe` 뒤의 독립 규칙 기반 미디어 Stage로 둔다.
- logical input은 `video`, `metadata`, output은 `embedded_text` JSON이다. `09_timeline`만 이
  Artifact를 join input으로 소비한다.
- `04_audio`와 의존성이 없으므로 Executor capacity가 2 이상이면 오디오·비주얼 분기와 병렬 실행할
  수 있다. Engine은 DAG/cache를, Executor는 실행 위치를, Stage의 FFmpeg adapter는 자막 변환을
  소유한다.
- 기본 DAG는 13개 Stage가 된다. 모델 Provider를 추가하지 않으며 host 절대 경로는 Artifact에
  기록하지 않는다.

### 자막 코덱과 변환

- version 1의 지원 source codec은 `ass`, `ssa`, `subrip`, `srt`, `mov_text`, `text`, `ttml`,
  `webvtt`다.
- 각 지원 stream은 absolute ffprobe stream index로 `-map 0:<index>`하고 FFmpeg WebVTT encoder/muxer를
  거친다. `-copyts -start_at_zero`로 media 시작 기준 시간축을 유지한다.
- WebVTT cue는 반개구간 `[start_sec,end_sec)`과 UTF-8 plain text로 정규화한다. presentation tag,
  ASS override와 HTML entity는 검색용 text에서 제거한다.
- 지원 목록 밖의 PGS/DVD/DVB/XSUB 등은 stream별 `status=skipped`,
  `reason_code=UNSUPPORTED_SUBTITLE_CODEC`으로 보존하고 실패로 취급하지 않는다. 지원한다고 선언한
  stream의 변환·UTF-8·cue 계약 오류는 전체 Stage를 실패시켜 부분 결과를 publish하지 않는다.

### Source identity와 챕터

- stream source ID는 `subtitle:stream:<ffprobe stream index>`, cue source ID는 여기에
  `:cue:<one-based ordinal>`을 붙인다. cue는 source stream ID, stream/cue index, language,
  start/end와 text를 보존한다.
- chapter source ID는 ffprobe 배열의 stable 위치인 `chapter:<zero-based index>`다. 컨테이너의
  원래 `id`는 `source_chapter_id`로 별도 보존한다.
- chapter는 start/end, title과 optional language를 갖는다. title이 없으면 `Chapter N`을 사용한다.
- top-level은 `schema_version=1`, interval/extraction policy, `executed`, `available`, stream/cue/chapter
  배열과 count 통계를 제공한다.

### Skip과 downstream 병합

- subtitle stream과 chapter가 모두 없으면 빈 Artifact를 publish하고 StageResult는
  `skipped/NO_EMBEDDED_TEXT`다.
- 지원되지 않는 subtitle stream만 있으면 stream metadata를 보존하고
  `skipped/NO_EXTRACTABLE_EMBEDDED_TEXT`다. chapter 또는 지원 stream을 처리하면 `succeeded`다.
- `09_timeline` version `1.4.0`은 subtitle cue를 기존 반개구간 최대 겹침·중점 tie-break로 정확히
  한 scene에 배정한다. scene도 같은 규칙으로 가장 많이 겹치는 chapter 하나를 갖는다.
- scene card는 전체 `subtitles`, ordered unique `subtitle_text`와 `chapter`를 additive하게 제공한다.
  source와 겹치지 않은 cue ID는 timeline 최상위 통계에 남긴다.
- `10_index`와 `11_context` version `1.3.0`, QueryService는 chapter title과 subtitle text를 각각
  검색 card text와 `챕터:`·`내장 자막:` context 줄에 포함한다.
- 이 slice에서는 STT를 자동으로 생략하지 않는다. 자막 품질·커버리지 검증 없이 STT를 대체하면
  forced commentary나 불완전 자막에서 정보가 사라질 수 있기 때문이다.

## 고려한 대안

### `01_probe`가 자막 본문까지 추출

빠른 container probe의 책임과 codec 변환 실패·비용이 결합되고 metadata cache를 재사용하기
어려워진다. probe 결과를 입력으로 받는 별도 Stage를 선택했다.

### `09_timeline`이 FFmpeg를 직접 호출

join Stage가 원본 video와 concrete media command를 알아야 하고 부분 실행 boundary도 흐려져
채택하지 않았다.

### 모든 subtitle codec을 WebVTT로 시도

bitmap subtitle은 text encoder로 변환할 수 없으며 환경별 decoder 지원 차이도 크다. versioned
allowlist와 명시적 unsupported stream을 사용한다.

### 비트맵 자막을 기존 `08_ocr`로 자동 처리

`08_ocr`은 scene keyframe OCR이며 subtitle bitmap packet의 palette·position·timestamp decode
계약이 없다. 별도 후속 slice가 필요하므로 이번 범위에서 제외한다.

### 내장 자막이 있으면 STT 자동 skip

language, forced disposition, 구간 coverage와 품질을 먼저 측정해야 한다. 현재는 additive source로만
사용하고 이후 검증 정책이 마련될 때 독립 결정으로 다룬다.

## 결과

긍정적 영향:

- 무료 container text와 제작자 chapter 구조가 timeline·검색·context에서 사용된다.
- 미디어 추출과 timeline join 책임이 분리되고 partial execution도 ArtifactRef 경계를 유지한다.
- 자막/챕터가 없는 기존 영상은 stable sentinel로 동일하게 처리된다.
- language·source identity·원본 구간이 보존돼 이후 품질 비교와 UI 근거 표시에 사용할 수 있다.

비용과 제약:

- 기본 DAG가 12개에서 13개로 늘고 09/10/11 cache version이 한 번 무효화된다.
- text codec allowlist 밖의 자막은 검색되지 않는다.
- 한 scene이 여러 chapter 경계를 넘더라도 version 1 scene card에는 최대 겹침 chapter 하나만 붙는다.
- FFmpeg build에 WebVTT encoder/muxer와 source decoder가 필요하다.

## 검증 결과

- WebVTT header/timestamp/positive-duration, cue ID/settings, markup 제거, Unicode와 invalid payload를
  network-free fixture로 검증했다.
- 지원/비지원 stream 혼합, language/disposition/source identity, chapter fallback, 빈 input과 bitmap-only
  sentinel을 Stage fixture로 검증했다.
- 실제 FFmpeg 8.1.2로 `mov_text` cue 2개와 ffmetadata chapter 2개를 가진 MP4를 생성해 probe→Artifact
  시간·Unicode·title을 검증했다.
- 기본 suite 451개가 통과했고, `samples/sample.mp4`는 새 Stage의 `NO_EMBEDDED_TEXT`와 함께 13단계
  `ok`, SQLite integrity `ok`, 기존 질의 scene 02 top-1을 유지했다.

stream 선택·subtitle codec과 chapter metadata 형식은 공식
[FFmpeg 문서](https://ffmpeg.org/ffmpeg.html),
[FFmpeg format 문서](https://ffmpeg.org/ffmpeg-formats.html),
[ffprobe 문서](https://ffmpeg.org/ffprobe-all.html)를 기준으로 했다.

## 구현 위치

- extraction Stage: `src/pipeline/stages/s04_embedded_text.py`
- DAG/binding: `src/video_preprocess/engine/defaults.py`,
  `src/video_preprocess/adapters/legacy_stages.py`
- downstream: `src/pipeline/stages/s09_timeline.py`, `s10_index.py`, `s11_context.py`,
  `src/video_preprocess/services/query.py`
- tests: `tests/test_embedded_text.py`, `tests/test_embedded_text_ffmpeg.py`,
  `tests/test_timeline.py`
