# ADR-0001: 엔진·실행기·추론 공급자를 분리한다

- 상태: Accepted
- 결정일: 2026-08-06
- 관련 문서: [`../06-target-architecture.md`](../06-target-architecture.md)

## 배경

현재 파이프라인은 순차 runner가 단계 모듈의 `run(ctx)`를 직접 호출한다. 각 단계는 로컬
파일 시스템을 직접 사용하며 일부 단계는 ML 라이브러리를 import하고 모델을 직접 로드한다.
이 방식은 단일 호스트 MVP에는 적합하지만 다음 요구를 동시에 수용하기 어렵다.

- 다른 서비스에서 파이프라인 실행·조회
- 모델별 로컬 또는 서버 엔드포인트 선택
- 단계의 로컬 또는 원격 워커 실행
- 모델 인스턴스 재사용과 워밍업
- 공유 스토리지와 비동기 작업
- 정확한 캐시 무효화, 재시도, 취소, 관측성

## 결정

다음 경계를 도입한다.

1. Pipeline Engine은 DAG, 상태, 캐시와 실행 정책을 소유한다.
2. Executor는 StageTask의 실행 위치와 실행 수명주기를 소유한다.
3. Stage는 배포 위치에 독립적인 도메인 변환만 수행한다.
4. Inference Gateway는 모델 alias를 Inference Provider로 라우팅한다.
5. Provider는 로컬 모델 또는 원격 endpoint 호출을 캡슐화한다.
6. 대용량 데이터는 ArtifactRef로 전달하고 Artifact Store가 실제 저장 위치를 추상화한다.
7. CLI, API, queue consumer는 같은 Application Service를 호출한다.

Executor와 Inference Provider는 별개의 확장 축으로 유지한다. 원격 모델 호출을 위해 전체
단계를 원격화하도록 강제하지 않는다.

## 고려한 대안

### 현재 runner에 HTTP 분기만 추가

구현은 빠르지만 각 단계에 로컬/HTTP 조건과 인증·retry 코드가 퍼진다. 모델 서버가 늘어날수록
Stage가 인프라 세부사항에 결합되므로 채택하지 않는다.

### 모든 단계를 독립 마이크로서비스로 분리

최대 독립성은 얻지만 현재 규모에서 배포·관측·스키마 관리 비용이 과도하다. 먼저 모듈 경계를
확립한 후 필요한 부분만 프로세스 또는 서비스로 분리한다.

### Executor와 Provider를 하나의 backend 개념으로 통합

단순해 보이지만 `로컬 단계 + 원격 모델`, `원격 단계 + 워커 로컬 모델` 조합을 표현하기 어렵다.
두 축을 독립적으로 유지한다.

## 결과

긍정적 영향:

- 새 모델 서버와 실행 환경을 기존 Stage 수정 없이 추가할 수 있다.
- 모델 lifecycle, retry, timeout, 인증을 provider에 집중할 수 있다.
- Engine을 무거운 ML 의존성 없이 테스트할 수 있다.
- CLI와 서비스 API가 동일한 실행 경로를 사용한다.
- 캐시와 실행 상태를 명시적으로 관리할 수 있다.

비용과 제약:

- 데이터 계약, 버전, 상태 머신과 artifact 관리 코드가 추가된다.
- 초기 MVP보다 추상화와 테스트 범위가 커진다.
- 원격 추론에는 공유 artifact 접근 또는 업로드 계층이 필요하다.
- 분산 실행을 실제 도입하면 heartbeat, lease, 중복 실행 처리까지 필요하다.

## 구현 전략

Big-bang 재작성 대신 기존 로컬 실행을 기준으로 다음 순서로 이동한다.

1. golden fixture와 계약 타입 추가
2. LocalArtifactStore와 LocalRunStore 도입
3. 로컬 모델을 Provider로 추출
4. Engine과 LocalExecutor 도입
5. HTTP Provider 도입
6. API·queue adapter 추가
7. 필요 시 RemoteExecutor 구현

현재 CLI와 산출물 구조는 마이그레이션 기간 동안 호환 계층으로 유지한다.

