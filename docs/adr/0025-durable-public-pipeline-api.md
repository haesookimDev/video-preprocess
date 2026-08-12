# ADR-0025: 공개 Pipeline API는 media ID와 영속 run snapshot을 사용한다

- 상태: Accepted
- 결정일: 2026-08-12
- 관련 문서:
  [`../openapi/pipeline-v1.yaml`](../openapi/pipeline-v1.yaml),
  [`../07-execution-inference-contracts.md`](../07-execution-inference-contracts.md)

## 배경

Phase 5에서는 기존 CLI와 같은 `PipelineApplicationService`/Engine 경로를 외부 서비스에 공개해야
한다. 내부 application request는 로컬 `video_path`와 `output_root`가 필요하지만, 이를 공개 계약으로
노출하면 호출자가 서버 파일시스템 구조를 알아야 하고 경로 탈출과 정보 노출 위험이 생긴다. 장시간
실행은 HTTP 연결과 프로세스 메모리보다 오래 유지될 수 있으므로 상태와 멱등성 정보도 영속화해야 한다.

## 결정

- 공개 create 요청은 경로 대신 server-side media catalog에서 해석하는 opaque `media_id`를 받는다.
- API adapter가 허용된 media root 안에서만 ID를 해석하고 서버가 run별 output root를 결정한다.
- 생성 요청은 `Idempotency-Key` header와 동일한 body 값을 필수로 가진다. 같은 key와 같은 의미의
  요청은 기존 run을 반환하고, 다른 요청은 `IDEMPOTENCY_CONFLICT`로 거부한다.
- API용 run repository는 queued 시점부터 진행률·warning·failure·artifact snapshot을 원자적으로
  저장한다. process restart 후 완료되지 않은 local 실행은 `RUN_INTERRUPTED` terminal failure로
  조정하며 조용히 재실행하지 않는다.
- 공개 artifact는 `artifact://` 참조만 반환하고 실제 materialize 경로와 output root는 내부에 둔다.
- v1 요청은 JSON만 허용한다. request byte limit, 동시 실행 capacity, bearer auth와 완료 run retention은
  deployment 설정이며 secret과 절대 경로를 payload 또는 manifest에 기록하지 않는다.
- reference local repository의 retention은 최근 terminal control snapshot 개수로 설정한다(기본 1000).
  한도를 넘으면 오래된 API 상태와 idempotency record만 제거하고 Engine manifest, workspace와 artifact
  본문은 삭제하지 않는다. 미디어/artifact 삭제 lifecycle은 별도 관리 기능의 책임이다.
- pipeline provider 배포 설정은 서버 composition이 소유한다. 클라이언트가 inference endpoint나
  credential을 요청별로 주입할 수 없게 한다.
- v1은 단일 process reference adapter다. durable distributed queue와 media upload protocol은 별도
  adapter로 추가하고 application contract를 바꾸지 않는다.

## 결과

외부 서비스는 로컬 출력 구조 없이 run을 생성하고 상태·아티팩트·검색 결과를 조회할 수 있다. 서버
재시작 후에도 마지막 상태를 조회할 수 있고 중단은 명시적 오류로 관측된다. 반면 media는 API 호출 전
허용된 catalog에 등록되어 있어야 하며, process restart 시 진행 중 작업 자동 재개는 지원하지 않는다.
