# ADR-0003: 로컬 artifact와 manifest 저장 규칙을 확정한다

- 상태: Accepted
- 결정일: 2026-08-06
- 관련 문서:
  [`../07-execution-inference-contracts.md`](../07-execution-inference-contracts.md)

## 배경

현재 MVP는 각 Stage가 `output/<video_stem>/` 아래에 직접 파일을 쓰고 대표 파일 존재 여부로
완료를 판단한다. 이 방식은 입력·설정 변경과 부분 쓰기를 구분할 수 없고, 다른 저장 backend로
이동할 때 로컬 절대 경로가 계약에 노출된다. Phase 1에서는 기존 출력 경로를 유지하면서도
Engine과 Stage가 저장 위치에 의존하지 않도록 로컬 기준 구현이 필요하다.

## 결정

### Artifact 저장

- 공개 URI는 `artifact://<namespace>/<relative-path>` 형식을 사용한다.
- `namespace`는 run이나 출력 단위를 식별하는 안전한 영문·숫자 ID다.
- URI의 상대 경로는 `LocalArtifactStore`의 설정된 root 아래 동일한 경로로 매핑한다.
- 절대 경로, `..`, 역슬래시와 예약 경로 `_pending`, `_manifests`는 거부한다.
- checksum은 SHA-256을 사용하고 byte stream을 임시 파일에 쓰는 동안 한 번 계산한다.
- `put()`은 비공개 `PendingArtifact`를 만들고 `publish()`가 같은 파일시스템의 `os.replace`로
  공개한 뒤 `ArtifactRef`를 반환한다.
- `discard()`는 publish되지 않은 임시 데이터만 제거한다. 공개 artifact 삭제는 보존 정책과
  함께 별도 관리 기능으로 둔다.

### Manifest 저장

- run-level manifest와 stage-attempt manifest를 분리한다.
- 로컬 manifest는 output root의 `_manifests/` 아래에 저장한다.
- Stage manifest는 `StageTask`, terminal `StageResult`, 시작·종료 시각과 선택적 cache key를
  포함한다.
- Run manifest는 입력 artifact, 설정, 요청 model binding과 Stage attempt 참조를 포함한다.
- JSON은 목적지와 같은 디렉터리의 임시 파일에 쓰고 flush·fsync 후 `os.replace`한다.
- Stage output artifact가 모두 존재하고 size·checksum이 일치한 뒤에만 manifest를 기록한다.
- manifest 기록 후 output이 삭제되거나 변조되면 해당 Stage는 완료로 판정하지 않는다.

### 기존 출력 호환

`LegacyOutputAdapter`는 기존 JSON 파일을 다시 쓰지 않고 SHA-256 `ArtifactRef`로 등록하며
`legacy_schema: v1` metadata를 추가한다. 현재 runner는 마이그레이션 기간에 기존 쓰기 방식을
유지하고, 새 Engine이 도입될 때 Local Store를 주입받는다.

## 고려한 대안

### content-addressed 전용 디렉터리로 즉시 이동

중복 제거에는 유리하지만 기존 CLI 산출물 경로와 query 호환을 바로 깨뜨린다. 먼저 논리 URI와
Port를 확립하고, content-addressed backend는 이후 구현으로 추가한다.

### 파일 존재 여부만 유지하고 manifest를 나중에 도입

구현량은 적지만 부분 출력과 stale cache 문제를 해결하지 못한다. Engine 전환 전에 저장 원자성과
검증 의미를 고정하기 위해 채택하지 않는다.

### MD5 또는 mtime·size fingerprint

계산 비용은 낮을 수 있지만 외부 저장소와 서비스 사이의 무결성 식별자로는 약하다. artifact를
쓰는 한 번의 streaming pass에서 SHA-256을 계산하면 추가 전체 읽기를 피할 수 있어 채택하지
않는다.

## 결과

긍정적 영향:

- 기존 `output/<video_stem>/<stage>/...` 파일 배치를 유지한다.
- 원격 adapter에 로컬 절대 경로를 전달하지 않는다.
- 부분 쓰기와 checksum 불일치를 완료 상태에서 제외할 수 있다.
- Run Store와 Artifact Store의 fake 또는 원격 구현을 같은 Port로 추가할 수 있다.

비용과 제약:

- publish 전 임시 파일만 process-local token으로 추적하므로 재시작 후 남은 `_pending` 파일은
  운영 정리 정책이 필요하다.
- SHA-256 검증은 artifact 전체를 다시 읽으므로 cache 판정 시 I/O 비용이 발생한다.
- 현재 runner 연결은 Engine 전환까지 미뤄져 새 manifest와 기존 `run_summary.json`이 잠시
  병존한다.

## 구현 위치

- 계약: `src/video_preprocess/storage/artifacts.py`, `runs.py`
- 로컬 구현: `src/video_preprocess/storage/local_artifacts.py`, `local_runs.py`
- 기존 출력 adapter: `src/video_preprocess/storage/legacy.py`
- manifest 타입: `src/video_preprocess/domain/manifests.py`
