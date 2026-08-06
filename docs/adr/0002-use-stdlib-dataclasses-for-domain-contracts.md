# ADR-0002: 도메인 계약은 표준 라이브러리 dataclass로 구현한다

- 상태: Accepted
- 결정일: 2026-08-06
- 관련 문서:
  [`../07-execution-inference-contracts.md`](../07-execution-inference-contracts.md)

## 배경

Engine, Executor, Stage와 외부 adapter가 공유할 공개 계약 타입이 필요하다. 후보는 Python
표준 라이브러리의 `dataclass`와 Pydantic 모델이었다. 이 계약 계층은 ML·HTTP·FFmpeg
라이브러리 없이 import되어야 하고, 향후 HTTP나 queue payload와 동일한 JSON 표현을 사용해야
한다.

## 결정

도메인 계약은 `frozen=True`, `slots=True`인 dataclass와 명시적인 `to_dict()`·`from_dict()`로
구현한다.

- 지원하는 `schema_version`은 역직렬화 시 검증한다.
- 공개 필드의 필수값, 범위, JSON 호환성은 생성 시 검증한다.
- 계약 모듈은 Python 표준 라이브러리에만 의존한다.
- HTTP API 경계에서는 필요할 때 Pydantic 같은 adapter 전용 validation 도구를 사용할 수 있다.
- adapter가 도메인 객체를 직접 대체하지 않고 변환 계층을 둔다.

## 고려한 대안

### Pydantic 모델을 도메인 계약으로 사용

강력한 validation과 JSON schema 생성은 장점이다. 그러나 모든 로컬 실행과 순수 domain
테스트에 제3자 런타임 의존성을 추가하고, 라이브러리 major version 변화가 핵심 계약에 영향을
준다. OpenAPI schema가 필요한 시점에 API adapter에서 사용하는 편이 경계를 더 명확히
유지하므로 채택하지 않는다.

### 타입 없는 dict만 사용

기존 JSON과 연결하기는 쉽지만 필수 필드와 상태를 코드에서 보장할 수 없고, Engine과 Executor
사이의 오류가 늦게 발견된다. 공개 계약을 명시한다는 Phase 1 목적에 맞지 않아 채택하지 않는다.

## 결과

긍정적 영향:

- domain 패키지를 별도 dependency 설치 없이 import하고 테스트할 수 있다.
- 직렬화 형식과 schema version 처리 방식이 코드에 명시된다.
- 로컬 호출, HTTP와 queue adapter가 같은 도메인 타입을 사용할 수 있다.

비용과 제약:

- validation과 직렬화 코드를 직접 유지해야 한다.
- dataclass가 frozen이어도 내부 mapping은 불변 객체가 아니므로 외부에 넘길 때 변경하지 않는
  규칙을 지켜야 한다.
- OpenAPI schema는 추후 API adapter 모델에서 별도로 생성해야 한다.

## 구현 위치와 검증

- 구현: `src/video_preprocess/domain/`
- 테스트: `tests/domain/`
- dependency 경계는 AST 기반 테스트로 제3자 import가 없는지 검증한다.
