# ADR-0017: 실행 유스케이스를 Application Service로 모으고 local runtime을 요청별 조립한다

- 상태: Accepted
- 결정일: 2026-08-06
- 관련 문서:
  [`../06-target-architecture.md`](../06-target-architecture.md),
  [`../07-execution-inference-contracts.md`](../07-execution-inference-contracts.md)

## 배경

PipelineEngine과 11-stage binding이 구현됐지만 기존 CLI는 context, provider와 stage loop를 직접
조립하는 legacy runner를 호출한다. CLI와 향후 API가 각자 plan, run ID, Store와 inference setup을
구현하면 실행 의미가 달라지고 확장 경계가 다시 결합된다. 선택 실행은 plan 밖의 상위 산출물을
안전하게 찾아야 하므로 단순 CLI 옵션 처리만으로도 해결되지 않는다.

## 결정

### Application request와 orchestration

`PipelineRunRequest`와 `PipelineSettings`를 adapter가 공유하는 입력으로 둔다. Application Service는
video 존재, ID 생성, DAG 선택과 plan별 config/model binding 필터를 담당한다. Stage 순서, cache와
실행 상태는 기존 PipelineEngine에 위임한다. runtime factory가 plan boundary를 해결하지 못하면
Executor submit 전에 application input 오류로 종료한다.

### Local composition root

로컬 factory는 output root별로 다음 객체를 요청마다 조립한다.

```text
LocalArtifactStore + LocalRunStore + ManifestCacheEvaluator
    → local inference services + legacy 11-stage bindings
    → LocalExecutor → PipelineEngine
```

입력 video도 cache integrity 검증이 가능하도록 `00_input/video.<ext>`에 원자적으로 복사하고
ArtifactRef를 발급한다. Stage 01~03은 호환 기간에 원본 `video_path`를 읽지만 실행 전 SHA-256 검증으로
등록된 copy와 내용이 같음을 보장한다.

### Partial execution boundary

partial plan은 같은 `run_id`의 이전 RunManifest와 StageManifest에서 boundary output을 복구한다.
현재 video checksum이 이전 input과 다르거나 필요한 output이 없거나 integrity 검증에 실패하면
부분 실행을 거부한다. 새 run 사이의 재사용은 global cache index가 구현될 때 별도 정책으로 추가한다.

## 고려한 대안

### CLI에서 직접 Engine 조립

API가 동일 코드를 재사용하기 어렵고 요청 검증 의미가 adapter마다 달라지므로 거부했다.

### 원본 video의 외부 ArtifactRef만 생성

LocalArtifactStore가 소유하지 않는 URI는 cache evaluator가 검증할 수 없다. 입력을 store에 등록해
manifest와 실제 bytes가 같은 integrity 경계를 사용하게 한다.

### 부분 실행에서 output path만 탐색

어느 입력·설정으로 생성됐는지 확인할 수 없으므로 manifest에 연결되고 checksum이 유효한 output만
사용한다.

## 결과

긍정적 영향:

- CLI와 API가 같은 request와 service를 호출할 수 있다.
- local 배포 조합이 Engine/Stage에서 분리된다.
- 선택 실행의 boundary가 이전 manifest와 artifact integrity로 검증된다.
- Application Service 테스트는 native tool과 model download 없이 실행된다.

비용과 제약:

- 입력 video가 output 아래에 한 번 복사되므로 저장 공간이 추가로 필요하다.
- 현재 cache 조회 범위는 같은 run이며 model fingerprint 사전 resolver가 없어 model Stage는 안전한
  miss를 유지한다.
- CLI adapter, dry-run view와 legacy `run_summary.json` 변환은 다음 slice에 남아 있다.

## 구현 위치

- service request/use case: `src/video_preprocess/services/pipeline.py`
- local composition: `src/video_preprocess/services/local.py`
- tests: `tests/services/test_pipeline_service.py`
