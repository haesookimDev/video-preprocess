# 개발 상태와 세션 인수인계

마지막 갱신: **2026-08-06**  
현재 단계: **아키텍처 설계 완료, 구현 시작 전**  
다음 Phase: **Phase 0 — 기준선 고정**

이 문서는 개발 진행 상황의 단일 진입점이다. 새로운 세션은 이 문서를 먼저 읽고, 실제 코드와
Git 상태를 확인한 뒤 작업을 시작한다.

## 1. 현재 제품 상태

현재 저장소는 로컬 단일 프로세스 MVP다.

- `src/run_pipeline.py`: 전처리 CLI
- `src/query.py`: 기존 index 검색·context 조립 CLI
- `src/pipeline/runner.py`: 11단계 순차 실행
- `src/pipeline/context.py`: 경로·설정·JSON I/O 공유
- `src/pipeline/stages/s01_*`~`s11_*`: 단계 구현
- 로컬 파일 존재 여부를 기준으로 단계 스킵
- STT, diarization, caption, embedding 단계가 모델을 직접 로드
- Local/HTTP provider, Executor Port, Artifact Store, Run Store는 아직 구현되지 않음

기존 샘플 산출물은 `output/` 아래에 있으나 생성물이며 Git에 커밋하지 않는다.

## 2. 승인된 목표와 결정

- Engine과 Executor를 분리한다.
- Stage 실행 위치와 모델 추론 위치를 독립적인 확장 축으로 둔다.
- 모델은 alias별 Local 또는 HTTP Provider로 실행할 수 있게 한다.
- 대용량 데이터는 `ArtifactRef`로 전달한다.
- CLI, API와 향후 queue adapter는 동일한 Application Service를 사용한다.
- Big-bang 재작성 대신 기존 출력과 CLI를 유지하며 단계적으로 전환한다.

관련 문서:

- 목표 구조: [`06-target-architecture.md`](./06-target-architecture.md)
- 실행·추론 계약: [`07-execution-inference-contracts.md`](./07-execution-inference-contracts.md)
- 전체 계획: [`08-development-roadmap.md`](./08-development-roadmap.md)
- 결정 기록: [`ADR-0001`](./adr/0001-separate-engine-executor-and-inference-providers.md)

## 3. 완료된 작업

### 기존 MVP

- [x] ffprobe 메타데이터
- [x] 씬 검출
- [x] 중앙 키프레임 추출
- [x] 오디오 정규화와 VAD
- [x] faster-whisper 전사
- [x] pyannote 화자 분리
- [x] BLIP 캡션
- [x] 씬 타임라인 병합
- [x] SQLite FTS5 + embedding index
- [x] RRF 검색과 context 조립
- [x] 전체 context 산출물

### 아키텍처 준비

- [x] 목표 컴포넌트와 의존 방향 문서화
- [x] Stage·Executor·Inference·Artifact 논리 계약 초안
- [x] 10~12주 마이그레이션 로드맵
- [x] ADR-0001 승인 기록
- [x] 세션 인수인계와 문서 갱신 규칙 정의

## 4. 아직 구현되지 않은 작업

- [ ] 누락된 runtime/optional dependency 명세
- [ ] pytest와 legacy/golden fixture
- [ ] runtime preflight
- [ ] domain 계약 타입
- [ ] ArtifactStore와 RunStore
- [ ] LocalInferenceProvider
- [ ] PipelineEngine과 LocalExecutor
- [ ] manifest 기반 cache
- [ ] HTTPInferenceProvider와 모델 서버 계약 구현
- [ ] Application Service와 API adapter
- [ ] 타임라인 경계 정합성 개선
- [ ] 한국어 검색과 평가 체계
- [ ] 실제 token budget

문서가 존재한다고 구현된 것으로 간주하지 않는다. 완료 여부는 코드와 자동 테스트를 기준으로
이 체크리스트에서 갱신한다.

## 5. 다음 작업: Phase 0 첫 번째 변경 묶음

권장 순서:

1. 현재 import 기준으로 필수·선택 의존성 목록 확정
2. 패키지 관리 방식 결정
   - 최소 변경: requirements 파일 분리
   - 권장: `pyproject.toml` + lock 전략
3. `pytest` 구성과 순수 함수 단위 테스트 추가
4. legacy JSON fixture 최소 세트 추가
5. FFmpeg·SQLite FTS5·선택 provider preflight 추가
6. README와 설치 명령 검증

첫 PR은 아키텍처 타입을 바로 만들기보다 설치 재현성과 테스트 기반을 다루는 것이 안전하다.

## 6. 알려진 중요 문제

| 우선순위 | 문제 | 영향 |
|---|---|---|
| P0 | `requirements.txt`에 직접 의존성 누락 | 새 환경에서 07·08·10 및 query 실패 가능 |
| P0 | 파일 존재만으로 cache hit | 입력·설정·모델 변경 후 stale 결과 재사용 |
| P0 | skipped diarization도 marker 생성 | credential 추가 후 자동 재시도되지 않음 |
| P0 | 씬 50:50 경계에서 전사 중복 가능 | timeline과 검색 내용 왜곡 |
| P1 | 한국어 `unicode61` 정확 일치 의존 | 조사·어미가 다른 키워드 검색 누락 |
| P1 | 임베딩 모델을 query마다 로드 | cold query 지연 |
| P1 | 관련도 하한 없음 | 무관 질의도 항상 top-k 반환 |
| P1 | 모든 씬을 context에 포함 | 긴 영상 token budget 초과 |
| P1 | `keyframes_per_scene` 미사용 | 설정과 실제 동작 불일치 |

이 문제는 새 구조에서 해결하되, P0 정확성 문제가 구조 전환을 막으면 Phase 0에서 최소 수정한다.

## 7. 기존 검증 기준선

2026-08-06 문서화 전 점검 결과:

- Python 소스 18개 구문 검사 성공
- 기존 `.venv`의 `pip check` 성공
- 기존 SQLite index 3개 integrity check 성공
- `sample.mp4`: 3개 씬, 3개 STT 세그먼트
- `sample2.mp4`: 3개 씬, 4개 STT 세그먼트
- 기존 query 예제에서 목표 씬 검색 확인
- 자동 테스트 suite는 아직 없음

기존 `.venv` 성공은 새 환경 설치 재현성을 보장하지 않는다. Phase 0에서 반드시 깨끗한 환경을
기준으로 다시 검증한다.

## 8. 새 세션 시작 체크리스트

- [ ] 저장소 루트의 `AGENTS.md`를 읽었다.
- [ ] 이 `STATUS.md`의 현재 단계와 다음 작업을 읽었다.
- [ ] `git status --short`로 사용자 변경을 확인했다.
- [ ] 목표 구조와 계약 문서에서 현재 작업 관련 부분을 읽었다.
- [ ] 관련 ADR을 확인했다.
- [ ] 코드와 STATUS가 다르면 작업 전에 차이를 정리했다.
- [ ] 이번 세션에서 완료할 하나의 Phase slice를 정했다.

## 9. 세션 종료 체크리스트

- [ ] 구현과 테스트가 완료되었는지 확인했다.
- [ ] 실행한 검증 명령과 결과를 아래 작업 기록에 추가했다.
- [ ] 완료 체크리스트와 현재 Phase를 갱신했다.
- [ ] 바로 다음 작업을 구체적인 파일·테스트 단위로 적었다.
- [ ] 계약 변경이면 `07` 문서를 갱신했다.
- [ ] 구조 변경이면 `06` 문서와 ADR을 갱신했다.
- [ ] 일정 변경이면 `08` 문서를 갱신했다.
- [ ] README의 사용자 명령이나 출력 설명이 여전히 맞는지 확인했다.

## 10. 작업 기록

최신 기록을 위에 추가한다. 긴 구현 설명은 PR이나 ADR에 두고 여기에는 다음 세션이 재개하는 데
필요한 정보만 적는다.

### 2026-08-06 — 아키텍처 문서화

- 목표: 엔진·실행기·추론 provider 분리 요구를 지속 가능한 개발 문서로 정리
- 변경: 목표 architecture, contract, roadmap, ADR, session workflow 문서 추가
- 코드 변경: 없음
- 검증: 문서 링크·구조 및 기존 저장소 상태 확인
- 다음 작업: Phase 0의 의존성 명세와 테스트 기반 추가

## 11. 다음 세션 인수인계 템플릿

작업 종료 시 아래 형식으로 최신 작업 기록을 추가한다.

```markdown
### YYYY-MM-DD — 작업 제목

- 목표:
- 완료:
- 미완료/차단:
- 주요 결정:
- 변경 파일:
- 검증 명령과 결과:
- 호환성 또는 migration 주의사항:
- 다음 작업:
```

