# ADR-0027: 정규화 n-gram과 semantic 하한으로 hybrid 검색을 판정한다

- 상태: Accepted
- 결정일: 2026-08-12
- 관련 문서:
  [`../05-pipeline.md`](../05-pipeline.md),
  [`../07-execution-inference-contracts.md`](../07-execution-inference-contracts.md)

## 배경

기존 keyword index는 SQLite FTS5 `unicode61` token의 완전 일치에만 의존했다. 한국어 조사·어미가
붙으면 질의와 카드의 token이 달라져 같은 어근도 놓칠 수 있었다. 의미 검색은 모든 scene을 cosine
순으로 정렬한 뒤 하한 없이 RRF에 넣어, 영상과 무관한 질의도 항상 top-k 결과를 반환했다. 공개
응답에는 최종 RRF 점수만 있어 어떤 검색 신호로 선택됐는지 확인하기 어려웠다.

## 결정

index와 query가 같은 `normalize_search_text`를 사용한다. Unicode NFKC와 casefold를 적용하고 letter와
number 이외 문자를 공백으로 바꾼 뒤 공백을 하나로 합친다. 각 정규화 word 안에서 문자 2~3-gram을
생성해 단어 token과 별도의 FTS5 column에 저장한다. n-gram은 공백 경계를 넘지 않는다.

QueryService는 keyword ranking 전체와 cosine이 `min_similarity` 이상인 semantic ranking만 RRF에
입력한다. 기본 하한은 0.35이며 -1~1에서 요청별 조정할 수 있다. keyword hit는 semantic 하한과 무관하게
유지한다. 두 ranking에 후보가 없으면 결과는 `matches=[]`, `no_answer=true`다.

match에는 최종 RRF score 외에 keyword rank/BM25-derived score, semantic rank/cosine과 실제 선택에
사용된 `keyword`, `semantic` reason을 기록한다. 10 index 결과 의미와 schema가 달라 Stage version을
1.1.0으로 올린다. QueryService는 migration 기간에 기존 `card_text` 단일-column FTS도 읽는다.

## 고려한 대안

### 한국어 형태소 분석기 추가

정확도 잠재력은 높지만 native/JVM 의존성과 사전 version이 현재 로컬·서버 배포를 복잡하게 한다.
고정 평가에서 n-gram의 한계가 확인되면 provider 형태로 추가한다.

### SQLite trigram tokenizer 사용

SQLite build option/version별 제공 여부가 달라 현재 FTS5 preflight만으로 보장할 수 없다. unicode61에
명시적으로 생성한 n-gram token을 넣어 기존 환경과 contract test를 유지한다.

### 모든 embedding 순위를 RRF에 넣고 최종 score만 자르기

RRF score는 corpus 크기·ranking 조합에 좌우되어 의미 유사도의 절대 하한을 표현하지 못하므로
semantic candidate를 fusion 전에 거른다.

## 결과

- 한국어 조사·문장부호·Unicode 폭 차이가 있어도 lexical 후보를 찾을 수 있다.
- 무관 semantic-only 질의를 빈 결과로 명시할 수 있다.
- CLI `--json`과 REST 응답에서 각 scene의 선택 근거를 재현할 수 있다.
- n-gram의 부분 일치가 만드는 false positive는 고정 retrieval 평가로 계속 측정해야 한다.
