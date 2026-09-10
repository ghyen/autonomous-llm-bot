# 자연어 런 재개 구현 계획

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 계속 의도가 담긴 자연어 요청이 같은 사용자·채널의 최신 미완료 런을 자동으로 안전하게 재개하도록 만든다.

**Architecture:** RunCatalog의 prepared 선택을 먼저 소비하고, 선택된 런이 없을 때만 유효한 최신 미완료 런을 활성화한다. bot.py는 순수 의도 판별 후 기존 durable restore 경로를 재사용한다. 복원 summary는 모델 호출 없이 축약하고 기존 serving-token preflight를 통과시킨다.

**Tech Stack:** Python 3.10+, 기존 unittest, RunCatalog, run_state, oMLX /messages/count_tokens, OpenAI-compatible payload validator.

## Global Constraints

- 자동 재개는 계속 의도가 명시된 비제어 자연어에만 적용한다.
- 후보는 같은 owner/channel의 stopped, exhausted, failed, interrupted 런이다. completed, active, prepared는 제외한다.
- 후보 선택 전에 run_state.load()가 성공해야 한다.
- prepared 런과 명시적 !resume 선택은 자동 후보보다 우선한다.
- ledger, next step, announced call ids, tool fingerprints, trajectory gap은 보존한다.
- 새 의존성·oMLX concurrency·iogpu limit 변경은 하지 않는다.
- 모든 커밋은 ghyen <79272189+ghyen@users.noreply.github.com>으로 작성한다.

---

### Task 1: catalog의 최신 미완료 런 선택

**Files:**
- Modify: run_workspace.py:208-465
- Test: test_durable_state.py

**Interfaces:**
- RunCatalog.resumable_workspaces(owner_id, channel_id) -> list[RunWorkspace]: eligible 상태를 최신순으로 반환한다.
- RunCatalog.acquire(owner_id, channel_id, resume_run_id=None) -> RunWorkspace: prepared 선택을 우선하고, 없을 때만 일치하는 resume id를 active로 전환한다.

- [x] **Step 1: 실패 테스트와 공용 fixture 작성**

test_durable_state.py의 DurableStateTestCase에 다음 fixture를 추가하고, 새 RunCatalogResumeSelectionTest를 만든다.

```python
def _save_valid_record(
    self, workspace, next_step=1, state="running", summary=""
):
    return run_state.save(
        workspace,
        message_id=ORIGIN_MESSAGE_ID,
        next_step=next_step,
        summary=summary,
        tail=[],
        ledger=ResearchLedger(),
        interrupt={},
        announced_call_ids=[],
        tool_fingerprints=[],
        trajectory_gap_step=None,
        state=state,
    )

def test_resumable_workspaces_filters_scope_and_status(self):
    catalog = self.catalog()
    older = catalog.acquire(TEST_USER_ID, CHANNEL_ID)
    self._save_valid_record(older, next_step=3, state="failed")
    older.updated_at = "2026-09-10T01:00:00+00:00"
    catalog.finish(older, "failed")

    newest = catalog.acquire(TEST_USER_ID, CHANNEL_ID)
    self._save_valid_record(newest, next_step=8, state="exhausted")
    newest.updated_at = "2026-09-10T02:00:00+00:00"
    catalog.finish(newest, "exhausted")

    other_channel = catalog.acquire(TEST_USER_ID, CHANNEL_ID + 1)
    self._save_valid_record(other_channel, state="stopped")
    catalog.finish(other_channel, "stopped")

    completed = catalog.acquire(TEST_USER_ID, CHANNEL_ID)
    self._save_valid_record(completed, state="completed")
    catalog.finish(completed, "completed")

    self.assertEqual(
        [item.run_id for item in catalog.resumable_workspaces(
            TEST_USER_ID, CHANNEL_ID
        )],
        [newest.run_id, older.run_id],
    )

def test_acquire_prefers_prepared_over_auto_resume_id(self):
    catalog = self.catalog()
    failed = catalog.acquire(TEST_USER_ID, CHANNEL_ID)
    self._save_valid_record(failed, next_step=7, state="failed")
    catalog.finish(failed, "failed")
    prepared = catalog.prepare(TEST_USER_ID, CHANNEL_ID)

    selected = catalog.acquire(
        TEST_USER_ID, CHANNEL_ID, resume_run_id=failed.run_id
    )

    self.assertEqual(selected.run_id, prepared.run_id)
    self.assertEqual(selected.status, "active")
```

- [x] **Step 2: 실패 확인**

Run: `python3 -m unittest test_durable_state.RunCatalogResumeSelectionTest -v`

Expected: 새 메서드와 resume_run_id 인자가 없어 FAIL한다.

- [x] **Step 3: 최소 구현**

TERMINAL_STATUSES에서 completed를 제외한 상태를 후보로 사용한다. 후보는 owner/channel로 필터링하고 (updated_at, created_at, run_id)를 역순 정렬한다. acquire는 기존 _selected의 prepared 런을 먼저 활성화한 뒤, 그 선택이 없을 때만 전달된 resume id가 catalog 내부에 존재하며 owner/channel/status가 맞는지 확인한다. 검증되지 않은 id는 무시하고 기존 fresh-run 생성으로 돌아간다. 공통 active 전환은 private helper로 중복을 제거한다.

- [x] **Step 4: 통과 확인 및 커밋**

Run: `python3 -m unittest test_durable_state.RunCatalogResumeSelectionTest test_durable_state.SnapshotRoundTripTest -v`

Expected: PASS.

```bash
git add run_workspace.py test_durable_state.py
git commit -m "feat: expose resumable run candidates"
```

### Task 2: 자연어 의도 판별과 자동 재개 라우팅

**Files:**
- Modify: bot.py의 resume helper 및 on_message()
- Test: test_durable_state.py

**Interfaces:**
- wants_auto_resume(content: str) -> bool: 외부 호출 없는 보수적 판별기.
- find_auto_resume_run(owner_id, channel_id) -> Optional[RunWorkspace]: 최신 후보부터 유효한 durable record를 찾는다.
- on_message()는 후보 run id를 RUN_CATALOG.acquire(..., resume_run_id=...)에 전달한다.

- [x] **Step 1: 실패 테스트 작성**

RunCatalogResumeSelectionTest와 같은 파일에 다음 순수 판별 및 on_message 회귀 테스트를 추가한다.

```python
def test_continue_intent_is_not_a_new_goal(self):
    self.assertTrue(bot.wants_auto_resume("이전 데이터 참고해서 계속해줘"))
    self.assertTrue(bot.wants_auto_resume("resume the remaining work"))
    self.assertFalse(bot.wants_auto_resume("새로운 서버 장애를 분석해줘"))
    self.assertFalse(bot.wants_auto_resume("!resume deadbeef"))

async def test_natural_language_continue_reuses_failed_run(self):
    catalog = self.catalog()
    failed = catalog.acquire(TEST_USER_ID, CHANNEL_ID)
    self._save_valid_record(
        failed, next_step=7, summary="이전 실행 요약", state="failed"
    )
    catalog.finish(failed, "failed")
    self.restart()

    await self.drive(
        catalog,
        [_response(content="재개 결과")],
        request="이전 데이터 참고해서 계속해줘",
        max_loops=7,
    )

    self.assertEqual(self.only_run(catalog).run_id, failed.run_id)
    resumed = [
        item for item in self.records(failed)
        if item["kind"] == "run_resumed"
    ]
    self.assertEqual(resumed[0]["next_step"], 7)
```

같은 테스트에서 failed 런이 있어도 새 조사 문장은 새 run id를 만들고, completed·손상된 state·다른 owner/channel 후보는 자동 재개하지 않는지 확인한다.

- [x] **Step 2: 실패 확인**

Run: `python3 -m unittest test_durable_state.NaturalLanguageResumeTest -v`

Expected: wants_auto_resume()와 자동 후보 route 부재로 FAIL한다.

- [x] **Step 3: 최소 구현**

unicodedata.normalize("NFKC", content).casefold() 결과에서 이전, 계속, 이어, 재개, 나머지, resume, continue를 검사한다. !로 시작하는 텍스트와 기존 제어 명령은 false다. find_auto_resume_run은 resumable_workspaces()를 순회하며 run_state.load()가 성공한 첫 후보만 반환한다. on_message는 제어 명령·steering 분기 이후 일반 목표를 acquire하기 직전에 후보를 구해 resume_run_id로 전달한다. 기존 restored 처리, 재개 안내, ledger/cursor/call-id 복원은 재사용하고 run_resumed 이벤트에 automatic=true만 추가한다.

- [x] **Step 4: 통과 확인 및 커밋**

Run: `python3 -m unittest test_durable_state.NaturalLanguageResumeTest test_durable_state.RestartRecoveryTest -v`

Expected: PASS.

```bash
git add bot.py test_durable_state.py
git commit -m "feat: resume incomplete runs from natural language"
```

### Task 3: 재개 summary의 token-aware 축약

**Files:**
- Modify: bot.py의 tiered summary, prepare_agent_request_payload(), agent loop 호출부
- Test: test_context_budget.py, test_durable_state.py

**Interfaces:**
- compact_resume_summary(summary, tier2_limit, tier3_chars, discovery_limit) -> str: tiered summary의 최신 절차 정보만 결정적으로 줄인다.
- prepare_agent_request_payload(..., resume_context=False)는 summary_compactions를 반환한다.

- [x] **Step 1: 실패 테스트 작성**

test_context_budget.py에 큰 tiered summary와 serving count를 이용한 회귀 테스트를 추가한다.

```python
async def test_resume_context_compacts_summary_before_group_trim(self):
    full_summary = bot.format_tiered_summary(
        tier3="절차 " * 500,
        tier3_through=20,
        tier2_lines=[
            f"Step {i}: 상세 인덱스" + (" 내용" * 20)
            for i in range(1, 21)
        ],
        discoveries=[f"- 발견 {i}" for i in range(1, 11)],
    )
    workspace = SimpleNamespace(root="/tmp")
    messages = [{
        "role": "system",
        "content": bot.build_system_content(workspace, summary=full_summary),
    }, {"role": "user", "content": "이전 작업을 이어서 진행해줘"}]
    with patch.object(
        bot,
        "count_agent_input_tokens",
        AsyncMock(side_effect=[9000, 4000]),
    ):
        result = await bot.prepare_agent_request_payload(
            workspace, messages, full_summary, 76, 4096,
            {"tools": []}, resume_context=True,
        )

    self.assertLessEqual(result.input_tokens, result.input_budget)
    self.assertGreater(result.summary_compactions, 0)
    self.assertIn("Step 20", result.summary)
    self.assertNotIn("Step 1:", result.summary)
```

추가 테스트는 tiered format version, ledger system block, tail/tool 그룹 보존을 확인한다.

- [x] **Step 2: 실패 확인**

Run: `python3 -m unittest test_context_budget.ContextPreflightTest -v`

Expected: compact_resume_summary(), resume_context, summary_compactions가 없어 FAIL한다.

- [x] **Step 3: 최소 구현**

파싱 가능한 summary는 format_tiered_summary()로 (tier2=6, tier3=1200, discoveries=4), (3, 800, 2), (0, 400, 0) 후보를 순서대로 만든다. 파싱 불가 summary는 같은 단계별 문자 상한으로 clipping한다. 첫 count가 budget 초과이고 resume_context=True이면 system 메시지를 build_system_content(workspace, ledger, compacted_summary)로 재작성하고 serving tokenizer로 다시 센다. 통과하면 즉시 종료하고, 남으면 기존 rollover → complete-group trim → tool-result clipping을 그대로 실행한다. agent loop는 resume_context=restored is not None을 전달하고 summary가 줄었으면 snapshot도 저장한다.

- [x] **Step 4: 통과 확인 및 커밋**

Run: `python3 -m unittest test_context_budget.py test_durable_state.py -v`

Expected: PASS; 최종 payload는 budget 이하이며 authoritative ledger와 duplicate guard 값은 변하지 않는다.

```bash
git add bot.py test_context_budget.py test_durable_state.py
git commit -m "fix: compact restored summaries before agent preflight"
```

### Task 4: 문서·전체 검증·PR

**Files:**
- Modify: README.md의 lifecycle/commands/compaction 설명

- [x] **Step 1: 사용자 동작 문서화**

!resume <run-id>는 exact 선택으로 유지하고, 계속 의도가 있는 자연어는 최신 유효 미완료 런을 자동 재개하며 일반 새 요청은 새 런이라는 계약을 lifecycle과 commands 섹션에 기록한다. summary를 줄여도 조사 사실·결론·가설 상태는 ledger가 보존한다는 설명을 추가한다.

- [x] **Step 2: 전체 검증**

Run: `git diff --check` 및 `make test`

Expected: whitespace 오류 없이 전체 unittest가 0 failures/errors로 통과한다.

- [x] **Step 3: author·diff 확인**

Run: `git status --short; git log --format='%h %an <%ae> %s' -8; git diff origin/codex/token-aware-context-compaction...HEAD --stat`

Expected: 계획된 파일만 변경되고 커밋 author가 모두 ghyen이다.

- [x] **Step 4: 문서 커밋**

```bash
git add README.md
git commit -m "docs: describe natural language run resume"
```

- [x] **Step 5: PR 생성**

```bash
git push -u origin codex/natural-language-run-resume
gh pr create --base main --head codex/natural-language-run-resume --title "feat: resume failed runs from natural language" --body "Automatically resumes the latest valid incomplete run for explicit continuation messages and compacts restored context before model preflight."
```

PR 생성 전 fresh full test output과 diff를 확인하고 PR URL을 전달한다. 사용자가 이미 요청한 범위에 따라 PR 생성 뒤 머지하고 원격 main도 갱신한다.
