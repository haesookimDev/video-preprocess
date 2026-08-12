# 파이프라인 단계별 처리 로직·사용 모델

구현된 11단계 파이프라인의 처리 흐름. **파란 계열 = 규칙 기반(모델 없음)**,
**보라 계열 = ML 모델 사용** 단계이다.

```mermaid
flowchart TD
    V(["원본 영상 (mp4 등)"]) --> S01

    S01["<b>01_probe</b><br/>ffprobe로 컨테이너 분석<br/>길이·코덱·챕터·내장 자막 확인<br/><i>도구: ffprobe</i>"]

    S01 --> S02
    S01 --> S04

    subgraph VIDEO ["비주얼 경로"]
        S02["<b>02_scenes</b><br/>인접 프레임 색상 변화량(content_val)이<br/>임계값(27.0)을 넘는 지점을 씬 경계로 검출<br/><i>도구: PySceneDetect ContentDetector</i>"]
        S03["<b>03_keyframes</b><br/>씬 중앙 시각으로 시크해<br/>씬당 대표 프레임 1장 추출<br/><i>도구: ffmpeg seek</i>"]
        S08["<b>08_captions</b><br/>키프레임을 VLM에 넣어<br/>영어 캡션 생성 (이미지→텍스트 압축)<br/><i>모델: BLIP image-captioning-base</i>"]
        S02 --> S03 --> S08
    end

    subgraph AUDIO ["오디오 경로"]
        S04["<b>04_audio</b><br/>오디오 디먹싱 후<br/>16kHz mono WAV로 정규화<br/><i>도구: ffmpeg</i>"]
        S05["<b>05_vad</b><br/>음성 확률로 발화 구간만 검출<br/>무음·비음성 제거 → STT 대상 축소<br/><i>모델: Silero VAD (ONNX)</i>"]
        S06["<b>06_stt</b><br/>VAD 구간을 병합(gap≤0.5s) 후<br/>구간별 전사, 타임스탬프 원본 보정<br/><i>모델: faster-whisper base/small</i>"]
        S07["<b>07_diarize</b><br/>화자 임베딩 클러스터링으로<br/>발화 턴별 화자 라벨 부여<br/><i>모델: pyannote community-1</i>"]
        S04 --> S05 --> S06 --> S07
    end

    S08 --> S09
    S07 --> S09
    S06 --> S09

    S09["<b>09_timeline</b><br/>씬을 골격으로 전사·캡션·화자를<br/>겹침(overlap) 기준 병합 → 씬 카드<br/><i>규칙 기반 (모델 없음)</i>"]

    S09 --> S10
    S09 --> S11

    S10["<b>10_index</b><br/>씬 카드 텍스트를 키워드 역색인 +<br/>의미 벡터로 이중 인덱싱<br/><i>모델: multilingual-MiniLM 임베딩<br/>도구: SQLite FTS5</i>"]

    S11["<b>11_context</b><br/>포맷 안내 + 메타데이터 + 씬 목차 +<br/>씬 카드 전문으로 최종본 조립<br/><i>규칙 기반 (모델 없음)</i>"]

    S11 --> OUT(["context.md / context.json<br/>= LLM 입력 컨텍스트 최종본"])

    S10 -.-> Q
    S09 -.-> Q
    Q["<b>query.py</b> (질의 시점)<br/>FTS bm25 + 임베딩 코사인을 RRF 융합<br/>→ top-k 씬 선별 → 축약 컨텍스트 조립<br/><i>모델: multilingual-MiniLM 임베딩</i>"]

    classDef rule fill:#dbeafe,stroke:#3b82f6,color:#1e3a5f
    classDef ml fill:#ede9fe,stroke:#8b5cf6,color:#3b2a6e
    classDef io fill:#f1f5f9,stroke:#64748b,color:#334155
    class S01,S02,S03,S04,S09,S11 rule
    class S05,S06,S07,S08,S10,Q ml
    class V,OUT io
```

## 단계별 상세

| 단계 | 처리 로직 | 모델 / 도구 | 실측 (57초 쇼츠) |
|---|---|---|---|
| 01_probe | 컨테이너 메타데이터 추출. 챕터·내장 자막 유무를 감지해 후속 단계 최적화 근거 제공 | ffprobe | 0.02s |
| 02_scenes | 인접 프레임 간 HSV 색상 변화량이 임계값(27.0)을 넘으면 씬 경계로 판정. 프레임별 통계를 CSV로 저장해 임계값 튜닝 지원 | PySceneDetect `ContentDetector` | 0.9s |
| 03_keyframes | 씬 중앙 타임스탬프로 입력 시크 후 1프레임만 디코딩 (전체 디코딩 회피) | ffmpeg `-ss` | 0.3s |
| 04_audio | 오디오 트랙 분리 후 모든 음성 모델의 공통 입력인 16kHz mono PCM으로 정규화 | ffmpeg | 0.1s |
| 05_vad | WAV ArtifactRef와 silence/padding option을 VAD Provider에 전달. 음성 구간만 추출해 Whisper 무음 환각 방지 | Local Silero VAD Provider (faster-whisper 내장 ONNX) | 0.1s (+ 첫 session load) |
| 06_stt | 인접 VAD 구간 병합(gap ≤ 0.5s) 후 WAV ArtifactRef와 구간을 STT Provider에 전달. Provider가 원본 시간축으로 보정 | Local faster-whisper Provider `base`(기본)/`small` (CTranslate2 int8) | 5.6s / 13.9s |
| 07_diarize | WAV ArtifactRef를 Diarization Provider에 전달. Provider가 화자 임베딩·클러스터링 후 발화 턴별 라벨 반환 | Local pyannote Provider `speaker-diarization-community-1` (HF 게이트) | 27.9s |
| 08_captions | keyframe ArtifactRef batch를 VLM Provider에 입력해 캡션 생성. 이미지(수백~수천 토큰)를 텍스트(수십 토큰)로 압축 | Local BLIP Provider `image-captioning-base` | 3.0s |
| 09_timeline | 씬을 골격으로 병합. 전사→씬은 겹침 ≥ 50% 기준 귀속, 전사→화자는 최대 겹침 턴의 라벨 채택 | 규칙 기반 | 0.0s |
| 10_index | 씬 카드 텍스트(캡션+전사)를 ① FTS5 역색인 ② 정규화 384차원 벡터로 이중 저장 | sentence-transformers `paraphrase-multilingual-MiniLM-L12-v2`, SQLite FTS5 | 0.2s |
| 11_context | 포맷 규칙 전문 + 메타데이터 + 씬 목차 + 씬 카드 전문을 하나의 자기완결 문서로 조립 | 규칙 기반 | 0.0s |
| query.py | 키워드(bm25)·의미(코사인) 순위를 RRF(k=60)로 융합 → top-k 씬 + 앞뒤 문맥으로 축약 컨텍스트 조립 | MiniLM 임베딩 (질의 인코딩) | ~10s (모델 로드 포함) |

## 설계 대응 관계

- **조기 압축**: 05_vad (무음 제거), 02_scenes (씬 단위 처리)
- **텍스트화 우선**: 06_stt (음성→텍스트), 08_captions (이미지→텍스트)
- **질의 시점 선별**: 10_index + query.py (전처리는 전부, 입력은 top-k만)
- **우아한 성능 저하**: 07_diarize는 토큰/오디오가 없으면 사유를 기록하고 스킵,
  나머지 파이프라인은 화자 라벨 없이 계속 동작

`08_captions/captions.json`은 기존 `model`, `captions`를 유지하며 실제 실행 정보를 나타내는
`provider`, `revision`, `runtime` 필드를 추가로 기록한다.

`06_stt/transcript.json`은 기존 segment 구조를 유지하며 실제 `provider`, model `revision`,
`runtime`, 감지 언어 확률인 `language_probability`를 추가로 기록한다.

`07_diarize/diarization.json`은 기존 speaker·turn 구조를 유지하며 실제 `provider`, model
`revision`, `runtime`을 추가로 기록한다. HF token은 Provider 설정에만 존재하며 산출물이나
추론 요청에 포함되지 않는다.

`05_vad/vad_segments.json`은 기존 duration·ratio·option·segment 구조를 유지하며 실제
`model`, `provider`, ONNX asset SHA-256 `revision`, `runtime`을 추가로 기록한다.

## Engine 기반 실행과 선택 범위

기본 `src/run_pipeline.py`는 Pipeline Application Service를 통해 같은 DAG를 계획하고
PipelineEngine→LocalExecutor에서 실행한다. 입력 영상은 cache integrity 확인을 위해 output의
`00_input/`에 등록되고 run/stage manifest는 `_manifests/`에 저장된다.

```bash
# 전체 실행 또는 같은 output workspace 재개
.venv/bin/python src/run_pipeline.py samples/sample.mp4

# 선택 실행에는 같은 run의 이전 boundary artifact가 필요하다
.venv/bin/python src/run_pipeline.py samples/sample.mp4 --stage 10_index
.venv/bin/python src/run_pipeline.py samples/sample.mp4 --from-stage 06_stt
.venv/bin/python src/run_pipeline.py samples/sample.mp4 --to-stage 09_timeline

# 강제 실행과 실행 없는 plan 확인
.venv/bin/python src/run_pipeline.py samples/sample.mp4 --force-stage 07_diarize
.venv/bin/python src/run_pipeline.py samples/sample.mp4 --dry-run
```

`--force`는 선택된 plan 전체를 강제하고 `--force-stage`는 지정 단계만 강제한다. dry-run은
Artifact/Run Store를 read-only로 열고 실제 task/cache identity로 `hit`, `miss`, `forced`, `blocked`,
예상 실행 여부와 stable reason을 출력한다. 검증된 hit output만 다음 Stage 입력으로 전달하므로
상위 Stage를 실행해야 checksum을 알 수 있는 경우 downstream은 `blocked`다.
