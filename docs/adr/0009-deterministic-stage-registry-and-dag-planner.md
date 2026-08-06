# ADR-0009: Stage registry와 DAG plan은 논리 key와 결정적 순서를 사용한다

- 상태: Accepted
- 결정일: 2026-08-06
- 관련 문서:
  [`../06-target-architecture.md`](../06-target-architecture.md),
  [`../07-execution-inference-contracts.md`](../07-execution-inference-contracts.md)

## 배경

기존 runner는 Python module 배열 순서로 11개 Stage를 실행하고 대표 출력 파일 존재 여부만
확인한다. Stage dependency, logical input/output과 model slot이 실행 가능한 데이터로 등록되어
있지 않아 cycle·누락 input을 사전에 검증하거나 DAG 기반 선택 실행을 만들 수 없다. 다음
LocalExecutor와 cache 구현 전에 실행 순서와 선택 규칙을 구체 Stage 코드에서 분리해야 한다.

## 결정

### Registry

- 현재 11개 Stage는 `StageSpec`의 stable name, version, dependency, logical input/output,
  model slot과 resource hint로 등록한다.
- 원본 video는 Stage output이 아닌 명시적 external input `video`다.
- logical output key는 파이프라인 전체에서 단일 Stage만 소유한다.
- Registry 생성 시 duplicate stage/output, external input 충돌과 unknown dependency를 거부한다.
- 기존 단계 module이나 `run(ctx)` callable은 이 slice의 Registry에 넣지 않는다. 실행 binding은
  LocalExecutor composition에서 별도로 연결한다.

### DAG 검증과 순서

- Planner는 Kahn topological sort를 사용하며 동시에 실행 가능한 Stage는 stable name의
  사전순으로 선택한다.
- 등록 순서와 무관하게 동일한 plan을 생성한다. 현재 숫자 prefix ID에서는 기존 01~11 순서를
  보존한다.
- cycle을 plan 생성 전에 거부한다.
- required input은 external input이거나 해당 Stage ancestor가 소유한 output이어야 한다.
  producer가 없거나 graph상 ancestor가 아니면 구성 오류다.

### 선택 실행

- selector가 없으면 전체 DAG를 plan한다.
- `stage=X`: X만 선택한다.
- `from_stage=X`: X와 모든 descendant를 선택한다.
- `to_stage=X`: 모든 ancestor와 X를 선택한다.
- `from_stage=X, to_stage=Y`: X descendant와 Y ancestor의 교집합, 즉 두 지점 사이 dependency
  path들을 선택한다. 교집합이 없으면 오류다.
- 선택된 Stage가 필요하지만 plan 내부에서 생성하지 않는 logical key는 정렬된
  `boundary_inputs`로 노출한다. Executor/Engine이 기존 artifact 또는 외부 입력을 확인한다.

## 고려한 대안

### 기존 module 배열 순서를 Registry로 재사용

단순하지만 dependency 의미를 표현하지 못하고 등록 순서 변경이 실행 의미를 바꾼다.

### 선택 범위를 숫자 prefix slice로 계산

현재 파일명에서는 작동하지만 독립 branch와 향후 새 Stage 삽입을 올바르게 처리하지 못한다.
DAG ancestor/descendant 관계를 사용한다.

### `--stage`가 모든 ancestor를 자동 실행

편리할 수 있지만 특정 Stage만 재실행하고 기존 upstream artifact를 재사용하는 의미를 표현할
수 없다. 필요한 upstream key를 `boundary_inputs`로 명시해 caller가 선택하도록 한다.

## 결과

긍정적 영향:

- Stage dependency와 logical artifact ownership을 ML·FFmpeg 없이 검증할 수 있다.
- 등록 순서와 무관한 재현 가능한 plan을 얻는다.
- exact/from/to 선택의 의미와 외부 artifact 요구사항이 명확하다.
- 다음 LocalExecutor, cache key와 dry-run 구현이 같은 plan을 사용할 수 있다.

비용과 제약:

- current Stage의 파일 I/O는 아직 logical key 기반 `StageTask`로 전환되지 않았다.
- redundant dependency도 명시적으로 유지하므로 graph metadata를 Stage 계약 변경과 함께
  관리해야 한다.
- `stage_version`은 모두 migration 초기값 `1.0.0`이며 cache 연결 전에 output 의미 변경 시
  증가 규칙을 적용해야 한다.

## 구현 위치

- registry: `src/video_preprocess/engine/registry.py`
- planner: `src/video_preprocess/engine/planner.py`
- current specs: `src/video_preprocess/engine/defaults.py`
