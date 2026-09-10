# 런 계약·산출물 매니페스트 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 롤링 압축·재시작·자연어 재개 뒤에도 런별 원래 목표와 실제 산출물 위치를 보존하고, 사용자 보고서에 추적 가능한 `run_id`를 표시한다.

**Architecture:** 기존 `state.json` schema 4에 선택적 `task_contract`와 `artifact_manifest`를 추가해 원자적으로 저장한다. `bot.py`가 새 런에서 계약을 한 번 만들고 재개·롤오버에서는 같은 계약과 호스트 관측 매니페스트를 시스템 메시지에 다시 주입한다. 보고서 식별자는 하나의 멱등 helper를 통해 정상·중단·실패·중간 보고서에 붙인다.

**Tech Stack:** Python 3 표준 라이브러리, 기존 `run_state.py` atomic write, `run_workspace.py` sandbox/CAS, `unittest`, 기존 Discord/OpenAI 실행 루프.

## Global Constraints

- `run_state.SCHEMA`는 `4`로 유지한다.
- 새 필드는 선택적으로 읽고, 기존 schema 4 상태에 필드가 없거나 불완전해도 전체 런을 폐기하지 않는다.
- 작업 계약은 새 런의 첫 durable snapshot에서 한 번만 만들고 `resume`·자연어 자동 재개·steering·rollover에서 덮어쓰지 않는다.
- 계약 원문은 bounded text로 저장하고 rolling summary/tail에는 넣지 않는다.
- 매니페스트는 호스트가 성공적으로 관측한 현재 런 내부 경로와 revision만 저장하며 모델이 직접 편집할 수 없다.
- 매니페스트는 canonical 파일과 최신 도구 출력이 우선인 작은 고정 상한을 사용한다.
- 기존 `playbook.md` 상속, loop-breaker, trajectory 재생, 동시 도구 호출 정책은 변경하지 않는다.
- 새 의존성·새 저장 파일·새 모델 도구 schema는 추가하지 않는다.
- 보고서 marker는 정확히 `> 🧾 **run ID**: \`<workspace.run_id>\`` 형식을 사용한다.

---

## File Map

- Modify: `run_state.py` — 선택적 계약·매니페스트의 저장/정규화/하위 호환.
- Modify: `bot.py` — 런 계약 lifecycle, 시스템 프롬프트 주입, 매니페스트 수집, 보고서 marker 전파.
- Modify: `test_durable_state.py` — state round-trip, legacy state, 런별 계약 복구 회귀 테스트.
- Modify: `test_hierarchical_memory.py` — 롤오버 보존, prompt 격리, 매니페스트 관측 테스트.
- Modify: `test_cancellation_flow.py` — deterministic 중단 보고서의 `run_id` 테스트.
- Modify: `test_context_budget.py` — 모든 compaction 경로에서 계약·매니페스트 인자 전달 테스트.

## Task 1: Durable state에 계약·매니페스트 필드 추가

**Files:**
- Modify: `run_state.py:25-180`
- Modify: `test_durable_state.py:200-285`

**Interfaces:**
- `run_state.save(..., task_contract=None, artifact_manifest=None) -> dict`는 기존 positional 인자를 깨지 않고 두 optional keyword를 받는다.
- `run_state.load(workspace) -> dict | None`은 성공 시 항상 `task_contract` 키를 `dict | None`으로, `artifact_manifest` 키를 `{version: 1, items: list}`로 제공한다.

- [ ] **Step 1: Round-trip 실패 테스트를 작성한다.**

`SnapshotRoundTripTest._saved()`의 기본 payload에 아래 값을 넣고, 기존 round-trip 테스트에 저장·복구 동일성 assertion을 추가한다.

```python
task_contract = {
    "version": 1,
    "origin_message_id": ORIGIN_MESSAGE_ID,
    "goal": "장애 원인을 조사해줘",
}
artifact_manifest = {
    "version": 1,
    "items": [
        {
            "path": "plan.md",
            "kind": "workspace_file",
            "step": 7,
            "revision": "sha256:" + "a" * 64,
        },
        {
            "path": "artifacts/out_." + "b" * 64 + ".log",
            "kind": "tool_output",
            "step": 8,
        },
    ],
}
```

호출부는 `run_state.save(workspace, **payload)`에 두 keyword를 전달하고, 복구 후 `restored["task_contract"]`와 `restored["artifact_manifest"]`가 `saved`와 같은지 검사한다.

- [ ] **Step 2: 새 테스트가 의도한 이유로 실패하는지 확인한다.**

Run: `python3 -m unittest test_durable_state.SnapshotRoundTripTest.test_3_goal_state_tail_interrupt_and_cursor_round_trip_exactly -v`

Expected: 현재 `run_state.save()`가 새 keyword를 받지 않아 `TypeError: save() got an unexpected keyword argument 'task_contract'`로 실패한다.

- [ ] **Step 3: legacy schema 4 호환성 실패 테스트를 작성한다.**

기존 유효 record를 저장한 뒤 JSON에서 새 두 필드를 제거하고 다시 쓴 다음, `run_state.load()`가 `None`이 아니며 아래 기본값을 제공하는지 검사한다.

```python
payload = self._saved(workspace)
payload.pop("task_contract", None)
payload.pop("artifact_manifest", None)
run_state.snapshot_path(workspace).write_text(
    json.dumps(payload, ensure_ascii=False), encoding="utf-8"
)

restored = run_state.load(workspace)
self.assertIsNotNone(restored)
self.assertIsNone(restored["task_contract"])
self.assertEqual(restored["artifact_manifest"], {"version": 1, "items": []})
```

- [ ] **Step 4: 최소 정규화 구현을 추가한다.**

`run_state.py`에 `TASK_CONTRACT_VERSION = 1`, `ARTIFACT_MANIFEST_VERSION = 1`을 추가하고, 선택 필드를 전체 state validation과 분리한다. 계약은 version 1, 양의 정수 `origin_message_id`, 비어 있지 않은 goal만 보존하며 goal은 4000자로 자른다. 매니페스트는 version 1의 dict만 사용하고, 각 항목에서 상대 path·`workspace_file`/`tool_output` kind·양의 정수 step·`sha256:` revision만 보존한다. 잘못된 선택 항목은 버리고 전체 state를 `None`으로 만들지 않는다.

구현 형태는 다음 계약을 유지한다.

```python
def save(..., trajectory_gap_step, state=RUNNING,
         task_contract=None, artifact_manifest=None):
    record = {
        # 기존 필드는 그대로 유지
        "task_contract": _normalize_task_contract(task_contract),
        "artifact_manifest": _normalize_artifact_manifest(artifact_manifest),
    }
```

`load()`는 `payload.get("task_contract")`, `payload.get("artifact_manifest")`를 정규화하고, 누락된 legacy record에는 각각 `None`, 빈 version 1 매니페스트를 넣는다. `_REQUIRED`에는 새 필드를 넣지 않는다.

- [ ] **Step 5: state 단위 테스트를 통과시킨다.**

Run: `python3 -m unittest test_durable_state.SnapshotRoundTripTest -v`

Expected: round-trip과 legacy compatibility가 모두 PASS하고 schema는 계속 `4`다.

- [ ] **Step 6: 커밋한다.**

```bash
git add run_state.py test_durable_state.py
git commit -m "feat: persist run contract and artifact manifest"
```

## Task 2: 불변 작업 계약을 생성·복구·주입한다

**Files:**
- Modify: `bot.py:2641-2665, 2890-3025, 1954-2080, 3895-4218, 4430-4478`
- Modify: `test_durable_state.py:286-350`
- Modify: `test_hierarchical_memory.py:135-210`
- Modify: `test_context_budget.py:190-410`

**Interfaces:**
- `resolve_task_contract(restored, message_id, content, same_origin) -> dict | None`은 신규 런이면 현재 요청으로 계약을 만들고, 기존 유효 계약은 그대로 반환하며, legacy에서 명확한 원본만 1회 복구한다.
- `build_system_content(workspace, ledger=None, summary="", task_contract=None, artifact_manifest=None) -> str`는 기존 세 positional 인자를 유지하면서 계약·매니페스트 블록을 bounded하게 추가한다.
- `rollover_agent_context(..., task_contract=None, artifact_manifest=None)`와 `prepare_agent_request_payload(..., task_contract=None, artifact_manifest=None)`는 새 블록을 compaction 경로 전체에 전달한다.

- [ ] **Step 1: 계약 lifecycle 실패 테스트를 작성한다.**

`test_durable_state.py`에 다음 세 경우를 고정한다.

```python
new_contract = bot.resolve_task_contract(
    None, ORIGIN_MESSAGE_ID, "원래 장애 조사", False
)
self.assertEqual(new_contract["goal"], "원래 장애 조사")

saved_contract = {
    "version": 1,
    "origin_message_id": 11,
    "goal": "첫 번째 주제",
}
restored = {"task_contract": saved_contract, "tail": []}
self.assertEqual(
    bot.resolve_task_contract(restored, 22, "두 번째 주제 계속", False),
    saved_contract,
)

legacy = {
    "task_contract": None,
    "message_id": 11,
    "tail": [{"role": "user", "content": "첫 번째 주제"}],
}
self.assertEqual(
    bot.resolve_task_contract(legacy, 22, "이전 데이터 참고해서 계속해줘", False)["goal"],
    "첫 번째 주제",
)
self.assertIsNone(
    bot.resolve_task_contract(
        {"task_contract": None, "message_id": 11,
         "tail": [{"role": "user", "content": "이전 데이터 참고해서 계속해줘"}]},
        22,
        "이전 데이터 참고해서 계속해줘",
        False,
    )
)
```

계약이 있는 legacy가 현재 재개 문구로 덮어써지지 않는다는 assertion도 추가한다.

- [ ] **Step 2: 계약 lifecycle 테스트가 실패하는지 확인한다.**

Run: `python3 -m unittest test_durable_state -v`

Expected: `resolve_task_contract`가 아직 정의되지 않아 `AttributeError`로 실패한다.

- [ ] **Step 3: 계약 helper와 prompt renderer를 구현한다.**

`bot.py`에 bounded 계약 생성·복구와 아래 블록을 추가한다.

```python
TASK_CONTRACT_MAX_CHARS = 4000

def resolve_task_contract(restored, message_id, content, same_origin):
    if restored is None:
        return {
            "version": 1,
            "origin_message_id": message_id,
            "goal": _clip_summary_text(content, TASK_CONTRACT_MAX_CHARS),
        }
    existing = restored.get("task_contract")
    if isinstance(existing, dict) and existing.get("goal"):
        return existing
    if same_origin:
        return {
            "version": 1,
            "origin_message_id": message_id,
            "goal": _clip_summary_text(content, TASK_CONTRACT_MAX_CHARS),
        }
    for item in restored.get("tail", []):
        candidate = _msg_content(item).strip()
        if _msg_role(item) == "user" and candidate and not wants_auto_resume(candidate):
            return {
                "version": 1,
                "origin_message_id": restored.get("message_id"),
                "goal": _clip_summary_text(candidate, TASK_CONTRACT_MAX_CHARS),
            }
    return None
```

복구 문구만 남은 legacy 상태에서는 현재 메시지를 goal로 승격하지 않는다. `build_system_content()`는 run id와 계약 부재 상태를 포함하고, 계약·매니페스트를 summary와 ledger보다 앞에 둬 기존 `ledger.render()` 마지막 assertion을 보존한다.

- [ ] **Step 4: on-message의 첫 snapshot과 모든 prompt 교체 지점을 연결한다.**

`on_message()`에서 `run_state.load()` 직후 아래 상태를 한 번 계산한다.

```python
task_contract = resolve_task_contract(
    restored,
    getattr(message, "id", None),
    content,
    same_origin,
)
artifact_manifest = (
    restored.get("artifact_manifest")
    if restored is not None
    else {"version": 1, "items": []}
)
```

`save_snapshot()`의 `run_state.save()` 호출에 `task_contract=task_contract`와 `artifact_manifest=artifact_manifest`를 추가한다. 다음 호출부를 모두 keyword로 갱신한다.

```python
build_system_content(
    workspace,
    ledger,
    rolling_summary,
    task_contract=task_contract,
    artifact_manifest=artifact_manifest,
)

await rollover_agent_context(
    workspace, live_messages, summary, step_num,
    ledger=ledger, token=token,
    trajectory_gap_step=trajectory_gap_step,
    task_contract=task_contract,
    artifact_manifest=artifact_manifest,
)
```

대상은 첫 system message, 루프 머리의 상태 재고정, `prepare_agent_request_payload()`의 두 summary-compaction 교체, rollover가 만드는 새 system message, tool-protocol retry 직전의 기존 payload다. retry는 이미 계약이 들어간 `messages_payload`를 재사용한다.

- [ ] **Step 5: 롤오버·컨텍스트 예산 테스트를 통과시킨다.**

`test_hierarchical_memory.py`의 rollover payload에 `run_id="run-contract-1"`인 test workspace, 계약과 매니페스트를 주고, 원래 user 메시지가 recent tail에서 제거돼도 rolled system에 다음이 남는지 검사한다.

```python
self.assertIn("[이 런의 불변 작업 계약]", system)
self.assertIn("원래 장애 조사", system)
self.assertIn(workspace.run_id, system)
self.assertIn("plan.md", system)
```

`test_context_budget.py`에서는 `prepare_agent_request_payload()`를 호출할 때 두 keyword를 넣고, mocked `rollover_agent_context`와 compacted system prompt에 같은 객체가 전달되는지 검사한다. 기존 prompt 호출부는 새 인자 없이도 계속 동작해야 한다.

Run: `python3 -m unittest test_durable_state test_hierarchical_memory test_context_budget -v`

Expected: 모든 대상 테스트 PASS.

- [ ] **Step 6: 커밋한다.**

```bash
git add bot.py test_durable_state.py test_hierarchical_memory.py test_context_budget.py
git commit -m "feat: keep immutable goal across context rollover"
```

## Task 3: 호스트 관측 산출물 매니페스트를 갱신한다

**Files:**
- Modify: `bot.py:780-900, 1245-1310, 4207-4218, 5010-5135`
- Modify: `test_hierarchical_memory.py:210-275`
- Modify: `test_tool_artifacts.py:250-310`

**Interfaces:**
- `update_artifact_manifest(manifest, workspace, tool_calls, results, artifact_paths, step_num) -> dict`는 입력 manifest를 복사해 새 version 1 manifest를 반환한다.
- `workspace_file` 항목은 `read_file`의 `success`/`unchanged`와 `write_file`의 `success` envelope에서만 만든다.
- `tool_output` 항목은 `_encapsulate_tool_output()`가 실제 반환한 `artifacts/out_.<sha256>.log` 경로에서만 만든다.

- [ ] **Step 1: 관측 범위와 상한의 실패 테스트를 작성한다.**

`test_hierarchical_memory.py`에 임시 run workspace와 아래 결과를 사용해 매니페스트를 호출하는 테스트를 추가한다.

```python
calls = [
    {"name": "read_file"},
    {"name": "write_file"},
    {"name": "bash_exec"},
]
results = [
    json.dumps({"status": "success", "path": "plan.md", "revision": "sha256:" + "a" * 64}),
    json.dumps({"status": "success", "path": "findings.md", "revision": "sha256:" + "b" * 64}),
    "[호스트가 보관한 긴 출력]",
]
artifact = bot._store_tool_artifact(workspace, "manifest-call", "긴 도구 출력")
self.assertIsNotNone(artifact)
manifest = bot.update_artifact_manifest(
    {"version": 1, "items": []}, workspace, calls, results,
    [None, None, artifact], 8,
)
self.assertEqual({item["path"] for item in manifest["items"]},
                 {"plan.md", "findings.md", artifact})
```

같은 테스트에서 error envelope, untrusted absolute/outside path, 단순 본문에 포함된 가짜 artifact path는 항목을 만들지 않아야 하며, 25개 이상 후보는 canonical 두 파일과 최신 tool output을 남겨야 한다.

- [ ] **Step 2: manifest helper가 아직 없어 의도한 이유로 실패하는지 확인한다.**

Run: `python3 -m unittest test_hierarchical_memory -v`

Expected: `AttributeError: module 'bot' has no attribute 'update_artifact_manifest'`로 실패한다.

- [ ] **Step 3: host-only manifest 갱신 helper를 구현한다.**

`bot.py`에 `ARTIFACT_MANIFEST_MAX_ITEMS = 24`를 두고, JSON envelope는 `_robust_json_loads()`로 해석한다. 각 path는 `workspace.resolve(path)`로 현재 run root 아래인지 확인하고 `relative_to(workspace.root).as_posix()`로 canonical 상대 경로를 만든다. regular file이 아닌 path와 `sha256:` 형식이 아닌 revision은 버린다.

기존 path는 새 revision/step으로 교체하고, 정렬 key는 `path in ("plan.md", "findings.md")` 우선, 그 다음 큰 `step` 우선으로 하여 24개를 남긴다. tool output는 `artifact_paths`에 실제 값이 있고 정규화 파일이 존재할 때만 추가한다. `results` 본문에서 임의 경로를 검색하지 않는다.

```python
def update_artifact_manifest(manifest, workspace, tool_calls, results, artifact_paths, step_num):
    items = {
        item["path"]: dict(item)
        for item in (manifest or {}).get("items", [])
        if isinstance(item, dict) and item.get("path")
    }
    for call, result, artifact_path in zip(tool_calls, results, artifact_paths):
        if call["name"] in ("read_file", "write_file"):
            envelope = _robust_json_loads(result)
            if isinstance(envelope, dict) and envelope.get("status") in ("success", "unchanged"):
                item = _manifest_workspace_item(workspace, envelope, step_num)
                if item:
                    items[item["path"]] = item
        if artifact_path:
            item = _manifest_artifact_item(workspace, artifact_path, step_num)
            if item:
                items[item["path"]] = item
    ordered = sorted(
        items.values(),
        key=lambda item: (item["path"] in ("plan.md", "findings.md"), item["step"]),
        reverse=True,
    )[:ARTIFACT_MANIFEST_MAX_ITEMS]
    return {"version": 1, "items": ordered}
```

`_manifest_workspace_item()`와 `_manifest_artifact_item()`은 위 helper 내부 또는 인접 private helper로 두고, 외부 입력이 state에 직접 들어가는 경계를 한 곳에서 검증한다.

- [ ] **Step 4: 완결 tool group 뒤에만 매니페스트를 저장한다.**

`merged_results`와 `merged_artifact_paths`가 만들어지고 trajectory append가 끝난 뒤, 기존 `save_snapshot(iteration + 2, "tool_group")` 바로 전에 다음을 실행한다.

```python
try:
    artifact_manifest = update_artifact_manifest(
        artifact_manifest,
        workspace,
        tool_calls_to_run,
        merged_results,
        merged_artifact_paths,
        iteration + 1,
    )
except Exception as manifest_error:
    log_session_event(
        workspace,
        "artifact_manifest_update_failed",
        step=iteration + 1,
        error=type(manifest_error).__name__,
    )
```

예외는 `outcome`을 실패로 바꾸지 않고 기존 tool 결과와 state 저장을 계속한다. 이후 system message 재고정에서 최신 `artifact_manifest`를 사용한다. 첫 `run_start` snapshot에는 빈 매니페스트를 저장한다.

- [ ] **Step 5: artifact와 prompt 격리 테스트를 통과시킨다.**

`test_tool_artifacts.py`에 실제 `_store_tool_artifact()` 경로를 helper에 전달해 성공하는 경우와 path-only 가짜가 거절되는 경우를 검사한다. `test_hierarchical_memory.py`에는 두 workspace 각각 다른 `plan.md`와 artifact를 넣고 각 prompt가 자기 manifest만 포함하는 assertion을 추가한다.

Run: `python3 -m unittest test_tool_artifacts test_hierarchical_memory -v`

Expected: 기존 artifact 보안 테스트와 새 manifest 테스트가 모두 PASS.

- [ ] **Step 6: 커밋한다.**

```bash
git add bot.py test_hierarchical_memory.py test_tool_artifacts.py
git commit -m "feat: track host-observed run artifacts"
```

## Task 4: 모든 보고서 경로에 run ID marker를 붙인다

**Files:**
- Modify: `bot.py:3384-3415, 4110-4145, 5210-5235, 5420-5630`
- Modify: `test_cancellation_flow.py:280-305`
- Modify: `test_durable_state.py:330-350`

**Interfaces:**
- `run_id_marker(workspace) -> str`는 정확히 `> 🧾 **run ID**: \`<workspace.run_id>\``를 반환한다.
- `ensure_run_id_marker(text, workspace) -> str`는 marker가 있으면 중복 추가하지 않고 없으면 마지막에 한 번 추가한다.
- `build_incomplete_report(workspace, outcome, ledger, rolling_summary, messages_payload) -> str`는 deterministic report 끝에 marker를 포함한다.

- [ ] **Step 1: marker와 중단 보고서 실패 테스트를 작성한다.**

`test_cancellation_flow.py`에 다음 assertion을 추가한다.

```python
workspace = SimpleNamespace(
    root="/tmp/run-fedcba9876543210fedcba9876543210",
    run_id="fedcba9876543210fedcba9876543210",
)
outcome = bot.RunOutcome()
outcome.settle(outcome_mod.STOPPED, "사용자 중단")
report = bot.build_incomplete_report(workspace, outcome, None, "", [])
self.assertIn(
    "> 🧾 **run ID**: `fedcba9876543210fedcba9876543210`", report
)
self.assertEqual(
    bot.ensure_run_id_marker(report, workspace).count("> 🧾 **run ID**:"), 1
)
```

`test_durable_state.py`의 실제 `drive()` 중간 보고서/종료 보고서 assertion에도 catalog에서 얻은 run id marker가 포함되는지 추가한다.

- [ ] **Step 2: report 테스트가 실패하는지 확인한다.**

Run: `python3 -m unittest test_cancellation_flow test_durable_state -v`

Expected: 새 signature와 helper가 없어 `AttributeError` 또는 기존 호출 signature mismatch로 실패한다.

- [ ] **Step 3: 멱등 marker helper와 deterministic report를 구현한다.**

`bot.py`에 아래 형태를 추가한다.

```python
RUN_ID_MARKER = "> 🧾 **run ID**: `{run_id}`"

def run_id_marker(workspace):
    return RUN_ID_MARKER.format(run_id=str(workspace.run_id))

def ensure_run_id_marker(text, workspace):
    text = str(text or "").rstrip()
    marker = run_id_marker(workspace)
    return text if marker in text else (text + "\n\n" + marker).strip()
```

`build_incomplete_report()`의 기존 sections join 결과를 `ensure_run_id_marker(..., workspace)`로 감싼다. production `RunWorkspace`에는 run id가 항상 있으므로 marker에 다른 식별자를 넣지 않는다.

- [ ] **Step 4: 모든 비직접 보고서 호출부를 연결한다.**

직접 답변 성공은 기존 짧은 응답을 유지한다. 다음 경로에는 helper를 반드시 적용한다.

1. direct stage 취소·timeout의 `build_incomplete_report(workspace, ...)`.
2. main loop의 중단·실패 deterministic fallback.
3. synthesis 취소·timeout·exception fallback.
4. `final_text_with_footer`를 만드는 non-direct 정상 완료·exhausted·failed footer.
5. checkpoint `cp_message`.
6. 최상위 exception의 `err_msg`.

정상 synthesis 본문에 marker가 없어도 footer 조립 직전에 `ensure_run_id_marker()`가 한 번 붙인다. `build_incomplete_report()`가 이미 붙인 marker는 중복되지 않는다. direct stage 자체가 실패한 fallback도 같은 helper를 사용한다.

- [ ] **Step 5: 보고서 회귀 테스트를 통과시킨다.**

Run: `python3 -m unittest test_cancellation_flow test_durable_state test_context_budget -v`

Expected: 중단 보고서에는 `미완료`와 정확한 run id가 있고, 직접 답변 성공 테스트의 기존 짧은 본문에는 강제 footer가 추가되지 않으며, checkpoint/fallback marker가 한 번만 존재한다.

- [ ] **Step 6: 커밋한다.**

```bash
git add bot.py test_cancellation_flow.py test_durable_state.py
git commit -m "feat: identify run in user reports"
```

## Task 5: 전체 회귀·보안·컴파일 검증 및 PR 준비

**Files:**
- Modify: files from Tasks 1-4 only when a failing verification exposes a concrete regression.

**Interfaces:**
- 변경된 public 호출부는 기존 optional 인자 기본값으로 호환된다.
- state 오류와 manifest 오류는 서로 독립적이며 manifest 오류가 조사 런을 실패시키지 않는다.

- [ ] **Step 1: 변경 diff와 계획 문서의 완결성을 검사한다.**

Run:

```bash
git diff --check
```

Expected: `git diff --check`는 출력 없이 성공한다. 계획 문서에는 실제 파일·함수·테스트 명령이 있고 미완성 지시 문구가 없다.

- [ ] **Step 2: Python compile/security 검사를 실행한다.**

Run:

```bash
python3 -m compileall -q bot.py run_state.py run_workspace.py workspace_io.py
python3 tools/check_no_credential_defaults.py .
```

Expected: 두 명령 모두 exit 0.

- [ ] **Step 3: 전체 테스트를 실행한다.**

Run: `make test`

Expected: baseline의 기존 테스트를 포함해 신규 테스트까지 전체 suite가 PASS한다.

- [ ] **Step 4: 상태·prompt·보고서 격리를 최종 확인한다.**

Run:

```bash
rg -n "task_contract|artifact_manifest|run_id_marker|ensure_run_id_marker" run_state.py bot.py test_*.py
git status --short
```

확인할 사실은 (a) `run_state.save()`와 모든 snapshot 경로가 두 필드를 전달하는지, (b) rollover/summary compaction 뒤에도 계약이 system message에 있는지, (c) 매니페스트가 현재 workspace 경로만 가지는지, (d) non-direct 보고서와 fallback에 marker가 있는지다.

- [ ] **Step 5: 구현 커밋과 계획 문서 상태를 확인한다.**

```bash
git log --oneline -5
git diff origin/main...HEAD --stat
```

Expected: Task별 작은 커밋, 의도한 파일만 변경, `origin/main`의 동시 도구 호출 정책·playbook 상속·trajectory schema는 변경되지 않는다.
