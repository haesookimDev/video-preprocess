# ADR-0026: timeline은 반개구간 최대 겹침 단일 배정을 사용한다

- 상태: Accepted
- 결정일: 2026-08-12
- 관련 문서:
  [`../05-pipeline.md`](../05-pipeline.md),
  [`../07-execution-inference-contracts.md`](../07-execution-inference-contracts.md)

## 배경

기존 09 timeline은 각 씬을 순회하며 전사 길이의 50% 이상 겹치는지를 독립적으로 검사했다. 전사가
인접한 두 씬의 경계에 정확히 절반씩 걸리면 두 조건이 모두 참이어서 같은 문장이 두 scene card와
검색 index에 중복됐다. 화자 턴 동률은 시간 경계 의미를 명시하지 않고 입력의 첫 턴을 선택했다.

## 결정

scene, transcript와 speaker turn의 시간 구간을 모두 `[start_sec,end_sec)`로 해석한다. transcript마다
양의 overlap이 가장 큰 scene 하나만 선택한다. 최대 overlap이 같으면 transcript midpoint를 포함하는
구간을 선택하고, midpoint로도 구분할 수 없는 overlapping interval은 입력 순서를 안정적인 최종
tie-break로 사용한다. 따라서 midpoint가 인접 scene 경계와 정확히 같으면 오른쪽 scene에 속한다.

speaker turn도 같은 선택 함수를 사용한다. scene과 양의 overlap이 전혀 없는 transcript는 임의의
scene에 붙이지 않고 source ID를 `unassigned_source_segment_ids`에 기록한다. 배정된 transcript line은
`source_segment_id`와 존재하는 `vad_source_ids`, `avg_logprob`, `no_speech_prob`를 보존한다.

결과 의미가 변경되므로 09 timeline Stage version을 1.1.0으로 올린다. 10 index와 11 context는 timeline
artifact checksum에 의존하므로 별도 강제 무효화 없이 downstream cache가 갱신된다.

## 고려한 대안

### 50% 비교를 `>`로만 변경

중복은 없어지지만 정확한 50:50 segment가 어느 씬에도 배정되지 않아 누락으로 바뀐다.

### 전사를 씬 경계에서 분할

word timestamp가 없는 현재 segment를 문자 비율로 자르면 발화 의미와 confidence 근거가 왜곡된다.
향후 word timestamp가 공통 STT 계약에 추가되면 실제 단어 경계 분할을 별도 version으로 검토한다.

### 겹치는 모든 씬에 source ID만 공유

검색과 context에서 같은 발화가 반복되는 기존 문제를 유지하므로 선택하지 않았다.

## 결과

- 정확한 50:50 경계를 포함해 source transcript 하나가 최대 한 scene card에만 나타난다.
- 경계에 닿기만 하는 interval은 overlap 0으로 처리된다.
- timeline에서 원본 STT confidence와 VAD provenance를 추적할 수 있다.
- 기존 scene card 소비자는 기존 필드를 그대로 읽을 수 있고 새 metadata를 선택적으로 사용할 수 있다.
