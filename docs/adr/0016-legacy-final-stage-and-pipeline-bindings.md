# ADR-0016: final Stage의 companion output을 추적하고 하나의 legacy pipeline으로 조립한다

- 상태: Accepted
- 결정일: 2026-08-06
- 관련 문서:
  [`../07-execution-inference-contracts.md`](../07-execution-inference-contracts.md)

## 배경

01~08 compatibility binding 이후에도 timeline, search index와 final context는 기존 runner에서만
실행됐다. 각 단계의 대표 marker 파일만 추적하면 사람이 읽는 Markdown, 실제 검색 DB 또는 구조화
context가 누락·변조돼도 Stage가 완료된 것으로 보일 수 있다. 또한 embedding 실행의 effective
provider와 revision이 legacy JSON 안에만 남아 Engine cache가 이를 비교할 수 없었다.

## 결정

### Final Stage output

09는 timeline JSON과 Markdown, 10은 SQLite DB와 summary JSON, 11은 context Markdown과 JSON을
각각 독립된 logical `ArtifactRef`로 publish한다. 하나라도 없으면 compatibility runner는
`LegacyStageContractError`로 실패한다. 파일 경로와 내용 형식은 기존 MVP와 동일하게 유지한다.

### Index model contract

10 task는 `embed_model` config와 `embedding.default` binding을 exact match한다. 성공한
`index_summary.json`에는 `embed_provider`, `embed_model`, `embed_revision`이 필요하고 runtime은
선택적이다. adapter는 이를 `embedding` slot의 `ModelExecution`으로 변환한다.

### Pipeline composition

media(01~04), model(05~08), final(09~11) registry는 독립적인 exact plan 테스트에 사용할 수 있다.
실제 전체 실행용 factory는 11개 definition을 하나의 `StageBindingRegistry`로 합치고 하나의
threading lock을 공유한다. 기존 `PipelineContext`는 실행 중 config가 변경되는 mutable 객체이므로,
LocalExecutor 외부에서 runner가 호출되더라도 동시에 mutation되지 않게 한다.

## 고려한 대안

### 대표 marker JSON만 등록

cache hit 후 DB나 Markdown이 없을 수 있고 소비자가 보는 결과와 manifest가 달라지므로 거부했다.

### 세 registry를 Application Service에서 단순 병합

각 registry가 별도 잠금을 가지면 공유 context를 병렬로 변경할 가능성이 남는다. migration
adapter 내부에서 전체 definition에 하나의 잠금을 부여한다.

### embedding metadata를 metrics로만 보존

effective deployment 비교가 구조화되지 않으므로 다른 모델 Stage와 동일한 `ModelExecution`을
사용한다.

## 결과

긍정적 영향:

- 현재 11개 Stage가 하나의 PipelineEngine→LocalExecutor 경로로 실행될 수 있다.
- 최종 사용자 산출물 전체가 manifest integrity와 cache 검증 대상이 된다.
- embedding provider/model revision이 다른 model Stage와 같은 방식으로 기록된다.
- fake 전체 DAG 테스트가 model download나 network 없이 composition 회귀를 검증한다.

비용과 제약:

- legacy Stage는 여전히 host path와 mutable context에 의존하므로 compatibility lock이 필요하다.
- 기본 CLI와 실제 sample은 Application Service 연결 slice에서 새 Engine 경로로 전환·검증한다.
- output publish 자체의 원자성은 기존 Stage 구현의 제약을 유지한다.

## 구현 위치

- binding/composition: `src/video_preprocess/adapters/legacy_stages.py`
- contract tests: `tests/adapters/test_legacy_final_bindings.py`
