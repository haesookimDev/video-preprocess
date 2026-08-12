# ADR-0028: context는 주입된 target tokenizer의 실제 token budget을 지킨다

- 상태: Accepted
- 결정일: 2026-08-12
- 관련 문서:
  [`../05-pipeline.md`](../05-pipeline.md),
  [`../06-target-architecture.md`](../06-target-architecture.md),
  [`../07-execution-inference-contracts.md`](../07-execution-inference-contracts.md)

## 배경

11 context는 한국어 혼합 문자의 길이를 2.5로 나눈 추정치만 기록하고 모든 scene을 포함했다. query도
전체 scene 목차와 top-k card를 제한 없이 조립했다. 긴 영상에서는 target LLM의 context window를
넘을 수 있고, 글자 수 비율은 tokenizer에 따라 실제 token 수와 크게 다르다.

## 결정

`TokenCounter` Port는 target model name과 `count`, `truncate`를 제공한다. local adapter는
`AutoTokenizer`를 lazy load해 process 안에서 재사용하고 special token 없는 실제 encode 길이를
사용한다. tokenization은 모델 추론이 아니며 Stage와 검색 조립기는 concrete Transformers class를
import하지 않고 주입된 Port만 호출한다. 기본 테스트는 fake counter로 network/model과 분리한다.

query context는 기본 4096 token이다. 검색 hit마다 timeline의 앞뒤 scene 1개를 기본 확장하고 scene
ID를 중복 제거한다. 후보 우선순위는 hit rank, 원 scene, 가까운 neighbor 순이다. 높은 우선순위의 full
card, compact card를 차례로 시도하고 맞지 않는 낮은 우선순위 card를 제외한다. context stats는
requested, expanded, included, excluded와 truncated scene ID 및 실제 token count를 공개한다.

11 context의 budget은 opt-in이다. `max_context_tokens`가 없으면 기존 전체 context를 유지하고,
지정하면 `context_tokenizer_model` 또는 embedding model의 canonical Hub ID를 사용한다. 설정과 결과
의미가 달라 Stage 11 version을 1.1.0으로 올린다.

## 고려한 대안

### 문자 수 비율 유지

언어와 tokenizer vocabulary에 따라 오차 방향과 크기가 달라 상한을 보장하지 못한다.

### 고정 tokenizer를 코드에 내장

실제 target LLM과 다른 token 수를 보장하게 되므로 배포·요청별 target tokenizer 설정을 허용한다.

### 모든 scene을 먼저 넣고 문서 끝을 절단

Markdown 구조와 scene 단위를 깨고 중요도가 높은 검색 결과가 문서 앞부분 때문에 잘릴 수 있어
scene block 우선순위 선택을 사용한다.

## 결과

- 생성 context의 실제 token 수가 지정 상한을 넘지 않는다.
- 긴 영상 query는 전체 목차 대신 검색 scene과 제한된 neighbor만 사용한다.
- tokenizer 파일은 첫 budget 사용 전에 cache 또는 network에서 확보돼야 하며 오류를 추정치 fallback으로
  숨기지 않는다.
- API의 budget, adjacency와 stats는 v1 additive 필드다.
