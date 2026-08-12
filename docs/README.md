# 개발 문서 안내

이 디렉터리는 방법론, 현재 구현, 목표 아키텍처, 개발 상태를 분리해 관리한다.
새로운 개발 세션에서는 문서를 번호순으로 전부 읽기보다 아래 읽기 순서를 따른다.

## 새 세션 필수 읽기 순서

1. [`STATUS.md`](./STATUS.md): 현재 단계, 완료 사항, 다음 작업, 알려진 문제
2. [`06-target-architecture.md`](./06-target-architecture.md): 목표 구조와 컴포넌트 경계
3. [`07-execution-inference-contracts.md`](./07-execution-inference-contracts.md): 단계·실행기·추론 계약
4. [`08-development-roadmap.md`](./08-development-roadmap.md): 구현 순서와 단계별 완료 조건
5. 현재 작업과 관련된 [`adr/`](./adr/) 문서

기존 전처리 방법론이나 현재 단계 동작을 변경하는 작업이라면 `00`~`05` 문서도 함께
확인한다.

## 문서 분류

| 문서 | 역할 | 갱신 시점 |
|---|---|---|
| [`00-overview.md`](./00-overview.md) | 전처리 방법론 개요 | 제품 방향이나 처리 원칙 변경 시 |
| [`01-video.md`](./01-video.md) | 비주얼 처리 방법론 | 씬·키프레임·VLM 전략 변경 시 |
| [`02-audio.md`](./02-audio.md) | 오디오 처리 방법론 | VAD·STT·화자 분리 전략 변경 시 |
| [`03-metadata.md`](./03-metadata.md) | 메타데이터·시간축 방법론 | 산출물 스키마나 시간축 변경 시 |
| [`04-integration.md`](./04-integration.md) | 검색·컨텍스트 방법론 | 검색·조립 전략 변경 시 |
| [`05-pipeline.md`](./05-pipeline.md) | 현재 11단계 구현 설명 | 실제 단계 동작이 변경될 때 |
| [`06-target-architecture.md`](./06-target-architecture.md) | 목표 시스템 아키텍처 | 컴포넌트 책임이나 의존 방향 변경 시 |
| [`07-execution-inference-contracts.md`](./07-execution-inference-contracts.md) | 실행·추론 인터페이스 계약 | API, 스키마, 오류 정책 변경 시 |
| [`08-development-roadmap.md`](./08-development-roadmap.md) | 단계별 개발 계획 | 범위·순서·완료 조건 변경 시 |
| [`openapi/inference-v1.yaml`](./openapi/inference-v1.yaml) | HTTP 추론 v1 전송 계약 | endpoint·payload·HTTP 오류 변경 시 |
| [`openapi/pipeline-v1.yaml`](./openapi/pipeline-v1.yaml) | Pipeline REST v1 전송 계약 | run·artifact·query API 변경 시 |
| [`STATUS.md`](./STATUS.md) | 살아 있는 개발 현황 | 의미 있는 개발 작업을 마칠 때마다 |
| [`adr/`](./adr/) | 되돌리기 어려운 결정의 근거 | 새로운 아키텍처 결정 시 |

## 문서 우선순위

정보가 충돌할 경우 다음 순서로 판단한다.

1. 실제 코드와 자동 테스트
2. `STATUS.md`에 기록된 현재 구현 상태
3. 승인된 ADR
4. `06`~`08` 설계·계획 문서
5. `00`~`05` 방법론 문서

코드와 문서가 다르면 코드를 무조건 정답으로 간주하고 끝내지 않는다. 변경 의도가
코드에 반영된 것인지 확인한 뒤, 같은 작업에서 관련 문서를 함께 수정한다.

## 개발 종료 시 갱신 규칙

의미 있는 작업을 마칠 때 다음을 수행한다.

1. `STATUS.md`의 현재 단계, 완료 항목, 다음 작업과 검증 결과를 갱신한다.
2. 공개 계약 또는 설정이 바뀌면 `07-execution-inference-contracts.md`를 갱신한다.
3. 목표 구조나 의존 방향이 바뀌면 `06-target-architecture.md`와 ADR을 갱신한다.
4. 일정이나 작업 순서가 바뀌면 `08-development-roadmap.md`를 갱신한다.
5. 실제 단계 동작이 바뀌면 `05-pipeline.md`와 `README.md`를 갱신한다.

단순 오탈자 수정이나 내부 리팩터링처럼 외부 동작·계약·다음 작업에 영향이 없는
변경은 `STATUS.md`에 기록하지 않아도 된다.
