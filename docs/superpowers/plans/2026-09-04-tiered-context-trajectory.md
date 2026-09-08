# Tiered Context Compaction and Trajectory Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Keep 70+ step runs bounded without forgetting earlier commands, failed approaches, URLs, or parameters by combining a 10-step verbatim tail, a deterministic 20-step micro index, a procedural macro summary, and a queryable append-only trajectory.

**Architecture:** `trajectory.py` owns a 0600 append-only `runs/<run-id>/traj.jsonl` chain and bounded lookup/index rendering; it never prints result content to stdout. `bot.py` records each complete assistant/tool group, exposes `lookup_trajectory`, and builds the system summary from trajectory-backed Tier 2 and Tier 3 sections while preserving complete assistant/tool groups as Tier 1. `ResearchLedger.render()` remains the sole authority for goals, facts, and hypothesis state and stays last in message 0.

**Tech Stack:** Python 3.10+, stdlib JSON/OS/hashlib/fcntl, `unittest`, existing Discord/OpenAI harness.

## Global Constraints

- Do not preserve backward compatibility; replace obsolete milestone/recent-summary formats rather than parsing both.
- Never rewrite text in a surviving historical message; Tier 2 and Tier 3 live in message 0, while rollover may drop a complete old prefix.
- Never cut between an assistant tool-call instruction and its tool results.
- Tier 3 contains procedural history only. It must not restate goals, confirmed facts, conclusions, or hypothesis status.
- The `[권위 있는 조사 상태]` ledger block remains the sole authority and the final section of message 0.
- Trajectory result content stays in the run workspace and is never sent through `session_log.log_session_event` or printed to stdout.
- No new dependency.

---

### Task 1: Append-only trajectory storage

**Files:**
- Create: `trajectory.py`
- Create: `test_trajectory.py`

**Interfaces:**
- Produces: `append_tool_group(workspace, step: int, tool_calls: list, results: list, executed_ids: set) -> list[dict]`
- Produces: `lookup(workspace, step: int, call_id: str = "") -> dict`
- Produces: `micro_index(workspace, start_step: int, end_step: int) -> list[str]`
- Produces: `procedural_source(workspace, end_step: int, max_chars: int) -> str`

- [x] **Step 1: Write failing storage tests**

Create `test_trajectory.py` using a real `RunCatalog` workspace. Assert that two appended groups produce newline-delimited JSON records with `schema`, `id`, `parent`, `step`, `call_id`, `tool`, normalized `arguments`, `result`, `failed`, and `executed`; every record after the first points to the previous record id; mode is 0600; a later append preserves the original bytes as an exact prefix.

- [x] **Step 2: Run the tests and verify RED**

Run: `/Users/edwin/Documents/p2ach/autonomous-llm-bot/venv/bin/python -m unittest test_trajectory -v`

Expected: import failure because `trajectory.py` does not exist.

- [x] **Step 3: Implement append-only records**

Use `workspace.root / "traj.jsonl"`, `os.open` with `O_CREAT | O_APPEND | O_WRONLY` (and `O_NOFOLLOW` when available), mode 0600, plus `fcntl.flock` around one encoded batch. Compute each id as SHA256 over the prior id and canonical record body. Bound one stored result to 4,000 characters with an explicit omission marker; keep normalized arguments because URLs and failed parameters are the memory this feature exists to preserve.

- [x] **Step 4: Add lookup and rendering tests**

Assert lookup by step returns all calls in order, lookup by call id narrows the result, missing data returns `status="not_found"` rather than an error, `micro_index` emits one bounded line per step, and `procedural_source` respects its character budget newest-first.

- [x] **Step 5: Implement lookup and renderers**

Read records defensively, ignore malformed lines, and return JSON-safe dictionaries. A micro line has the exact shape `[Step N: tool(arg-preview) -> result-preview]`; parallel calls at one step are joined on the same line.

- [x] **Step 6: Run Task 1 tests**

Run: `/Users/edwin/Documents/p2ach/autonomous-llm-bot/venv/bin/python -m unittest test_trajectory -v`

Expected: all trajectory tests pass.

---

### Task 2: Record groups and expose lookup to the model

**Files:**
- Modify: `bot.py` (`TOOLS_SCHEMA`, tool handlers, dispatcher, completed tool-group path)
- Modify: `test_trajectory.py`

**Interfaces:**
- Consumes: Task 1 trajectory functions.
- Produces: `async tool_lookup_trajectory(workspace, step, call_id="") -> str`

- [x] **Step 1: Write failing integration tests**

Assert `lookup_trajectory` appears in `agent_tool_params()`, dispatch returns a parseable `status="success"` envelope, and a real agent step writes both executed and pre-dispatch-blocked calls to `traj.jsonl` only after every result has been paired.

- [x] **Step 2: Verify RED**

Run the new test methods and expect missing schema/handler failures.

- [x] **Step 3: Add schema, handler, and dispatcher branch**

The schema requires integer `step >= 1` and accepts optional string `call_id`. The handler calls `trajectory.lookup` and JSON-serializes it. `not_found` is a neutral result and must not feed `_tool_result_failed`.

- [x] **Step 4: Append each completed group**

After `merged_results` is complete and before `save_snapshot`, call `append_tool_group` with the original ordered calls/results and `set(executed_call_ids)` narrowed to that batch. On `OSError`/`ValueError`, emit a metadata-only `trajectory_write_failed` session event and continue the run.

- [x] **Step 5: Run integration tests**

Run: `/Users/edwin/Documents/p2ach/autonomous-llm-bot/venv/bin/python -m unittest test_trajectory test_duplicate_tool_policy -v`

Expected: all pass and existing tool-result pairing remains unchanged.

---

### Task 3: Replace the obsolete two-level summary with explicit tiers

**Files:**
- Modify: `bot.py` (summary constants/helpers and `rollover_agent_context`)
- Modify: `test_hierarchical_memory.py`
- Modify: `test_state_flow.py`
- Modify: `test_durable_state.py`
- Modify: `test_workspace_integrity.py`

**Interfaces:**
- Consumes: `trajectory.micro_index` and `trajectory.procedural_source`.
- Produces: `format_tiered_summary(tier2_lines: list[str], tier3: str, discoveries: list[str]) -> str`
- Produces: `parse_tiered_summary(text: str) -> dict`

- [x] **Step 1: Replace summary-helper tests first**

Assert the new format contains `## 🗺️ 장기 절차 요약 (Tier 3)`, the fixed authority notice, `## 🧭 중기 스텝 인덱스 (Tier 2)`, and the artifacts section. Assert parsing returns `tier3`, `tier2`, and `discoveries`. Assert goal/fact/hypothesis markers appear only in the final ledger block of `build_system_content`, never in Tier 3.

- [x] **Step 2: Verify RED**

Run: `/Users/edwin/Documents/p2ach/autonomous-llm-bot/venv/bin/python -m unittest test_hierarchical_memory test_state_flow -v`

Expected: missing new constants/functions and old section assertions.

- [x] **Step 3: Implement the new format and remove old helpers**

Delete `MILESTONES_SECTION_HEADER`, `RECENT_PHASE_SECTION_HEADER`, and their backward-compatible parser path. Add `TIER3_SECTION_HEADER`, `TIER2_SECTION_HEADER`, and `TIER3_AUTHORITY_NOTICE`. Keep `_clip_summary_text` and artifact extraction.

- [x] **Step 4: Build tiers during rollover**

Set Tier 1 retention to 10 complete tool groups. For step `S`, derive Tier 2 from trajectory steps `max(1, S-29)` through `S-10`; derive Tier 3 input only from steps `<= S-30`. Ask the compactor for procedural attempts, blockers, and next alternatives only. On timeout/error, group deterministic source into bounded 10-step procedural lines. Never prepend `ledger.render()` into the summary; `build_system_content` already appends it last.

- [x] **Step 5: Update state-flow assertions**

Change tests that required ledger markers inside the summary: assert markers are absent from the summary and present in `rolled[0]`. Keep immutable historical message assertions byte-for-byte unchanged.

- [x] **Step 6: Run all affected tests**

Run: `/Users/edwin/Documents/p2ach/autonomous-llm-bot/venv/bin/python -m unittest test_hierarchical_memory test_state_flow test_durable_state test_workspace_integrity test_cancellation_flow test_terminal_state -v`

Expected: all pass.

---

### Task 4: Documentation and full verification

**Files:**
- Modify: `README.md`

- [x] **Step 1: Update the architecture description**

Replace the current inaccurate “milestone/recent/artifact” claim with the exact Tier 1/Tier 2/Tier 3 boundaries, explain `traj.jsonl` and `lookup_trajectory`, and state that facts/hypotheses remain exclusively in the authoritative ledger.

- [x] **Step 2: Run the full suite**

Run: `/Users/edwin/Documents/p2ach/autonomous-llm-bot/venv/bin/python -m unittest discover -v`

Expected: all tests pass.

- [x] **Step 3: Run CI-equivalent gates**

Run: `/Users/edwin/Documents/p2ach/autonomous-llm-bot/venv/bin/python tools/check_no_credential_defaults.py bot.py config.py ledger.py authz.py outcome.py deadlines.py run_workspace.py workspace_io.py steering.py session_log.py run_state.py tool_sandbox.py tool_worker.py trajectory.py tools`

Run: `/Users/edwin/Documents/p2ach/autonomous-llm-bot/venv/bin/python -m compileall -q bot.py trajectory.py`

Expected: no credential defaults and clean compilation.

- [x] **Step 4: Commit the implementation**

Stage only `bot.py`, `trajectory.py`, affected tests, `README.md`, and this plan. Commit with a message tying the change to issue #49.
