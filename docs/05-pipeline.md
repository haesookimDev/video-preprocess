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
| 05_vad | 프레임 단위 음성 확률 계산 → 발화 구간만 추출. 무음 구간을 STT에서 제외해 시간 절약 + Whisper 무음 환각 방지 | Silero VAD (ONNX, faster-whisper 내장) | 0.1s |
| 06_stt | 인접 VAD 구간 병합(gap ≤ 0.5s) 후 구간별 전사. 구간 오프셋을 더해 원본 시간축으로 보정 | faster-whisper `base`(기본)/`small` (CTranslate2 int8) | 5.6s / 13.9s |
| 07_diarize | 음성 구간을 화자 임베딩으로 변환 후 클러스터링 → 발화 턴별 화자 라벨 | pyannote `speaker-diarization-community-1` (HF 게이트) | 27.9s |
| 08_captions | 키프레임을 VLM에 입력해 캡션 생성. 이미지(수백~수천 토큰)를 텍스트(수십 토큰)로 압축 | BLIP `image-captioning-base` | 3.0s |
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
