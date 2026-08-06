# video-preprocess

긴 영상을 로컬 LLM으로 분석하기 위한 전처리 파이프라인 프로토타입.
방법론은 [docs/](docs/00-overview.md) 참고.

현재 기본 CLI는 Pipeline Application Service, Engine, LocalExecutor와 manifest cache를 사용한다.
모델별 로컬/서버 추론과 다른 서비스 연동을 지원하기 위한 아키텍처 전환은 계속 진행 중이다.
현재 VAD, STT, diarization, caption과 embedding은 Local Inference Provider를 사용한다. 문서는 다음
순서로 확인한다.

- [개발 문서 안내](docs/README.md)
- [현재 개발 상태와 다음 작업](docs/STATUS.md)
- [목표 아키텍처](docs/06-target-architecture.md)
- [실행기·단계·추론 계약](docs/07-execution-inference-contracts.md)
- [상세 개발 로드맵](docs/08-development-roadmap.md)

## 최소 파이프라인 구성

| 단계 | 처리 | 출력 |
|---|---|---|
| 01_probe | ffprobe 메타데이터 추출 | `metadata.json` |
| 02_scenes | PySceneDetect 씬 경계 검출 | `scenes.json`, `scene_stats.csv` |
| 03_keyframes | 씬별 중앙 키프레임 추출 | `keyframes.json`, `frames/*.jpg` |
| 04_audio | 오디오 디먹싱·16kHz mono 정규화 | `audio.json`, `audio_16k.wav` |
| 05_vad | Silero VAD 음성 구간 검출 | `vad_segments.json` |
| 06_stt | faster-whisper 전사 (VAD 구간만) | `transcript.json` |
| 07_diarize | pyannote 화자 분리 (HF_TOKEN 필요) | `diarization.json` |
| 08_captions | BLIP 씬 키프레임 캡셔닝 (영어) | `captions.json` |
| 09_timeline | 씬 카드 병합 (공통 시간축, 화자 라벨 포함) | `timeline.json`, `timeline.md` |
| 10_index | SQLite FTS5 + 임베딩 인덱스 | `index.db`, `index_summary.json` |
| 11_context | **LLM 입력 컨텍스트 최종본 조립** | `context.md`, `context.json` |

## 설치

```bash
brew install ffmpeg
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

화자 분리까지 로컬에서 실행하려면 선택 의존성을 설치한다:

```bash
.venv/bin/pip install -r requirements-diarization.txt
```

개발 환경은 전체 로컬 의존성과 pytest를 함께 설치한다:

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest
```

화자 분리(07_diarize)는 선택 의존성과 Hugging Face 게이트 모델을 사용한다:
1. [pyannote/speaker-diarization-community-1](https://huggingface.co/pyannote/speaker-diarization-community-1)
   페이지에서 약관 동의
2. `HF_TOKEN=hf_...` 환경변수를 설정하거나 프로젝트 루트 `.env`에 추가

토큰이 없으면 해당 단계만 자동 스킵되고 나머지 파이프라인은 정상 동작한다.

## 실행

```bash
.venv/bin/python src/run_pipeline.py <video.mp4>

# 모델을 로드하지 않고 Python·FFmpeg·SQLite·패키지·credential만 검사
.venv/bin/python src/run_pipeline.py --preflight-only

# 옵션
#   --out DIR            출력 루트 (기본: output)
#   --force              기존 단계 출력 무시하고 전부 재실행
#   --force-stage NAME   지정 Stage만 캐시를 무시 (여러 번 지정 가능)
#   --stage NAME         정확히 한 단계만 실행
#   --from-stage NAME    지정 단계와 그 하위 단계 실행
#   --to-stage NAME      지정 단계까지 필요한 상위 단계 실행
#   --run-id ID          같은 run manifest 재개 (기본: 출력 경로 기반 ID)
#   --dry-run            실행 없이 Stage plan과 boundary input 출력
#   --whisper-model M    tiny/base/small/medium/large (기본: base)
#   --language ko        전사 언어 고정 (기본: 자동 감지)
#   --scene-threshold N  씬 검출 민감도, 낮을수록 민감 (기본: 27.0)
#   --preflight-only     실행 환경만 검사하고 종료
```

## 출력 구조

```
output/<video_stem>/
├── 00_input/                  # cache integrity용 입력 영상 copy
├── 01_probe/ … 11_context/    # 단계별 개별 산출물 (위 표 참고)
├── _manifests/                # run/stage 상태, cache key와 ArtifactRef
├── logs/run_<run_id>.log      # 상세 로그 (DEBUG, 프레임/세그먼트 단위)
└── run_summary.json           # CLI 호환 단계별 상태·metrics view
```

- 콘솔에는 INFO 로그(진행 상황·통계), 파일에는 DEBUG 로그(개별 씬/세그먼트/명령)가 기록된다.
- cache는 파일 존재만 보지 않고 Stage version, 입력 checksum, 설정, model binding과 output
  integrity를 검증한다. `--force-stage`로 한 단계만, `--force`로 계획된 모든 단계를 강제한다.
- 부분 실행은 같은 `run_id`의 검증된 이전 manifest가 필요하다. 기본 local run ID는 output
  workspace에서 안정적으로 만들어지므로 같은 명령의 재개에 사용할 수 있다.
- 현재 model Stage는 실행 전 effective revision을 안전하게 확정할 수 없으면 재실행한다.
  run 간 global cache와 cache-aware dry-run은 후속 Phase 3 작업이다.
- 사람이 결과를 빠르게 확인할 때는 `09_timeline/timeline.md` 를 본다.
- **최종 산출물은 `11_context/context.md`** — 포맷 안내 전문(preamble) + 메타데이터 +
  씬 목차 + 씬 카드 전문으로 구성된 자기완결 문서로, 그대로 LLM 프롬프트에 넣어
  요약·질의응답·이벤트 분석에 사용한다. `context.json`은 동일 내용의 구조화 버전.
- `08_captions/captions.json`에는 기존 캡션 구조와 함께 실제 `provider`, model `revision`,
  `runtime`이 기록된다.
- `06_stt/transcript.json`에도 실제 `provider`, model `revision`, `runtime`, 감지 언어 확률이
  기록된다.
- `07_diarize/diarization.json`에도 실제 `provider`, model `revision`, `runtime`이 기록된다.
  `HF_TOKEN`은 Provider 설정으로만 사용되며 요청·산출물에 저장되지 않는다.
- `05_vad/vad_segments.json`에도 실제 `model`, `provider`, ONNX asset SHA-256 `revision`,
  `runtime`이 기록된다.

## 검색 + 컨텍스트 조립

전처리가 끝난 영상에 대해 하이브리드 검색(FTS5 키워드 + 임베딩 의미, RRF 융합)으로
관련 씬을 찾고 LLM 입력 컨텍스트를 조립해 출력한다 (LLM 호출은 하지 않음):

```bash
.venv/bin/python src/query.py output/sample "음성 구간 검출 얘기는 어디서 해?" --topk 2
```

- 검색 순위 근거(FTS bm25, 코사인 유사도, RRF 점수)는 `logs/query_<timestamp>.log`에 DEBUG로 기록된다.
- 조립 구조: `영상 개요(씬 목차)` + `관련 씬 카드 원문` (최상위 씬을 맨 뒤에 배치해
  lost-in-the-middle 완화).

## 테스트 영상 생성 (macOS)

```bash
cd samples
say -v Yuna -o ph1.aiff "첫 번째 장면입니다. 영상 전처리 파이프라인 테스트를 시작합니다."
say -v Yuna -o ph2.aiff "두 번째 장면에서는 음성 구간 검출이 잘 되는지 확인합니다."
say -v Yuna -o ph3.aiff "마지막 장면입니다. 전사 결과가 씬 타임라인에 병합됩니다."
ffmpeg -v error \
  -f lavfi -i "testsrc=duration=10:size=640x360:rate=25" \
  -f lavfi -i "smptebars=duration=10:size=640x360:rate=25" \
  -f lavfi -i "rgbtestsrc=duration=10:size=640x360:rate=25" \
  -i ph1.aiff -i ph2.aiff -i ph3.aiff \
  -filter_complex "[0:v][1:v][2:v]concat=n=3:v=1:a=0[v];[3:a]aresample=22050,adelay=1500:all=1[a0];[4:a]aresample=22050,adelay=12000:all=1[a1];[5:a]aresample=22050,adelay=22000:all=1[a2];[a0][a1][a2]amix=inputs=3:duration=longest:normalize=0,apad=whole_dur=30[a]" \
  -map "[v]" -map "[a]" -c:v libx264 -preset fast -pix_fmt yuv420p -c:a aac -t 30 -y sample.mp4
```

주의: `say`의 신형 보이스(Flo, Eddy 등)는 `-o` 파일 출력 시 무음이 되는 경우가 있다 — Yuna 사용.

## 아직 없는 것 (다음 단계 후보)

- 오디오 이벤트 태깅 (박수·음악 등)
- 한국어 캡셔닝 VLM 교체 (현재 BLIP은 영어 캡션)
- 긴 영상 대응: 컨텍스트 최종본의 토큰 예산 관리 (씬 수가 많을 때 목차 + 선별
  씬 카드로 축약하는 예산 배분 로직)

아키텍처 마이그레이션과 위 기능의 정확한 구현 순서는
[`docs/08-development-roadmap.md`](docs/08-development-roadmap.md)를 기준으로 하며, 실제
완료 여부는 [`docs/STATUS.md`](docs/STATUS.md)에서 관리한다.
