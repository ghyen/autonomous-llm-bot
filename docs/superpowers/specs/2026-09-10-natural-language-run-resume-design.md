# 자연어 런 재개 설계

## 목표

사용자가 `!resume <run-id>`를 입력하지 않고도 “이전 데이터 참고해서 계속해줘”처럼
계속 의도가 담긴 자연어를 보내면, 같은 사용자·채널의 최신 미완료 런을 안전하게
선택하고 다음 스텝부터 실행한다.

## 범위와 제약

- 자동 재개는 계속 의도가 명시된 자연어에만 적용한다. 일반적인 새 질문은 새 런으로
  시작한다.
- 대상은 같은 `owner_id`와 `channel_id`의 최신 `stopped`, `exhausted`, `failed`,
  `interrupted` 런이다. `completed`, `active`, `prepared` 런은 자동 후보에서 제외한다.
- 런 선택 전 `state.json`을 검증한다. 유효한 durable record가 없는 런은 자동 재개하지
  않고 새 런으로 처리한다.
- `state.json`의 ledger, next step, 중복 실행 방지 식별자, trajectory gap은 보존한다.
  조사 사실·결론·가설 상태는 ledger를 유일한 권위 상태로 유지한다.
- 기존 `!resume`/`/resume`, `!new`/`!reset`, steering 및 active-run 충돌 정책은
  변경하지 않는다.

## 선택한 접근

### 1. 자연어 의도 판별

텍스트 명령 분기 전에 제어 명령이 아닌 메시지를 정규화하고, 한국어·영어의 보수적인
계속 표현(예: `이전`, `계속`, `이어`, `재개`, `나머지`, `resume`, `continue`)이 하나
이상 포함될 때만 자동 재개를 요청한다. 판별기는 외부 모델을 호출하지 않는 순수
함수로 두어 새 런을 잘못 이어받는 비용을 막는다.

### 2. 원자적인 최신 런 선택

`RunCatalog`에 owner/channel로 후보를 좁히고 lifecycle timestamp로 최신순을 반환하는
작은 조회를 추가한다. 메시지 처리기는 후보마다 `run_state.load()`를 호출해 유효한
상태를 확인한 뒤 catalog의 기존 resume/acquire 수명주기를 사용한다. 준비된 런이
이미 선택되어 있으면 이를 먼저 소비해 `!new`와 `!reset`의 blank-run 의미를 보존한다.

자동 선택된 런도 기존 복구 경로와 동일하게 `run_resumed` 이벤트, 재개 안내, durable
ledger 복원, 다음 cursor, announced tool-call guard를 사용한다. 후보가 모두 무효하면
자동 재개 없이 새 런을 만들고, 잘못된 상태를 새 Step 1 실행으로 재사용하지 않는다.

### 3. 재개 전 결정적 압축

복원한 tail은 이미 완결된 assistant/tool 그룹만 포함하므로 유지한다. 누적 summary가
큰 경우에는 모델 호출 없이 tiered summary를 다시 포맷한다.

- Tier 3 절차 요약은 유지하되 기존 상한 안에서 보존한다.
- Tier 2는 최신 몇 개의 스텝 인덱스만 남긴다.
- artifact discovery는 제한된 최신 항목만 남긴다.
- 목표·사실·결론·가설 상태는 summary를 줄여도 ledger에서 복원된다.

그 뒤 현재 serving tokenizer를 포함한 기존 `prepare_agent_request_payload()` 검사를
통과시킨다. 그래도 초과하면 기존 완결 그룹 제거 및 tool-result clipping을 순서대로
적용하고, 끝까지 예산을 넘으면 안전하게 실패시킨다. 이 경로는 oMLX의 context
상한을 늘려 해결하지 않는다.

## 오류 및 동시성

- 자동 재개 후보가 이미 active이거나 catalog 상태가 바뀌면 해당 후보를 건너뛰고
  다음 후보를 검사한다.
- `state.json`이 손상되었거나 스키마가 맞지 않으면 후보를 재개하지 않는다. 기존
  명시적 resume의 검증·오류 semantics는 그대로 둔다.
- 자동 재개 선택은 첫 await 전에 수행하고, 선택된 런은 기존 lease가 보호한다.
- 복원 안내 전송 실패는 실행 자체를 취소하지 않는다. 기존 실행 종료/상태 저장
  경로를 그대로 사용한다.

## 검증 기준

- 계속 의도가 있는 자연어가 최신 유효 미완료 런의 run id와 next step을 재사용한다.
- 일반 자연어는 이전 런을 건드리지 않고 새 런을 만든다.
- completed 런, 다른 사용자·채널의 런, 손상된 state는 자동 재개하지 않는다.
- 복원 summary가 큰 경우에도 serving-token budget 아래로 줄고 ledger/tail/cursor와
  tool-call 중복 방지 정보가 보존된다.
- 기존 전체 unittest suite와 새 회귀 테스트가 모두 통과한다.
