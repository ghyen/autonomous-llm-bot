# Token-aware Context Compaction Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep every local oMLX agent request below a conservative token/KV budget so long runs do not fail during prefill.

**Architecture:** Add a small Anthropic-format request converter and async oMLX token-count preflight beside the existing payload validator. When a request is over budget, reuse tiered rollover once, then persistently trim complete old tool groups and finally clip tool-result bodies before sending. Keep the authoritative system/current-goal prefix and validate the resulting OpenAI payload again; reject the request if the final count still exceeds the budget.

**Tech Stack:** Python 3.10+, existing `httpx`, `unittest`, OpenAI-compatible `AsyncOpenAI`, oMLX local `/v1/messages/count_tokens`, macOS LaunchAgent.

## Global Constraints

- Default total context target: `AGENT_MAX_CONTEXT_TOKENS=10240`.
- Reserve `1024` tokens for prefill/transient memory and subtract each step's `max_tokens` from the input budget.
- Reserve another `256` tokens for differences between the Anthropic count shape and the OpenAI chat request.
- Default verbatim tool-group retention: `KEEP_RECENT_TOOL_GROUPS=2`.
- Minimum retained complete tool groups: `1`.
- No new Python dependency and no kernel iogpu wired-limit increase.
- Normal oMLX concurrency: `--max-concurrent-requests 1`.
- Do not log prompt contents, tool arguments, tool results, or credentials; numeric budget metrics are allowed.

---

### Task 1: Add exact local token-count request conversion

**Files:**
- Create: `test_context_budget.py`
- Modify: `bot.py:1518-1725` (payload helper section)

**Interfaces:**
- Produces `build_token_count_payload(messages: list, tool_params: dict) -> dict` with Anthropic-compatible `system`, `messages`, and `tools` fields.
- Produces `async count_agent_input_tokens(messages: list, tool_params: dict) -> int | None`; it posts to `LLM_BASE_URL.rstrip('/') + '/messages/count_tokens'` and returns `None` on an unavailable/nonconforming endpoint.

- [ ] **Step 1: Write the failing tests**

Add tests that pass an assistant `tool_use` equivalent and a following tool result, then assert the converter preserves the call id, tool name, parsed JSON input, result id/content, and tool schema. Add a fake async HTTP client test that asserts the exact endpoint and returned `input_tokens` are used.

```python
def test_count_payload_preserves_tool_protocol(self):
    payload = bot.build_token_count_payload(messages, {"tools": tools})
    self.assertEqual(payload["messages"][1]["content"][0]["type"], "tool_use")
    self.assertEqual(payload["messages"][1]["content"][0]["id"], "call-1")
    self.assertEqual(payload["messages"][2]["content"][0]["tool_use_id"], "call-1")
    self.assertEqual(payload["tools"][0]["input_schema"]["type"], "object")
```

- [ ] **Step 2: Run the focused test to verify it fails**

Run: `python3 -m unittest test_context_budget.TokenCountPayloadTest -v`

Expected: FAIL because `build_token_count_payload` and `count_agent_input_tokens` do not exist.

- [ ] **Step 3: Implement the minimal converter and HTTP call**

Convert system messages into one `system` string; convert assistant tool calls to `tool_use` blocks with parsed object inputs; convert OpenAI `role=tool` messages to user `tool_result` blocks; and map each OpenAI function schema to Anthropic `input_schema`. Use an existing `httpx.AsyncClient` context with a short five-second transport timeout, `raise_for_status()`, and a strict integer response check. Catch only transport/JSON/schema failures and return `None`.

- [ ] **Step 4: Run the focused test to verify it passes**

Run: `python3 -m unittest test_context_budget.TokenCountPayloadTest -v`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add bot.py test_context_budget.py
git commit -m "feat: count agent context with serving tokenizer"
```

### Task 2: Enforce the token budget at the agent request boundary

**Files:**
- Modify: `bot.py:708-716` (budget defaults)
- Modify: `bot.py:1727-1787` (payload bounds and preparation)
- Modify: `bot.py:3980-4050` (main agent request)
- Test: `test_context_budget.py`

**Interfaces:**
- Produces `prepare_agent_request_payload(workspace, messages, existing_summary, step_num, step_max_tokens, tool_params, ledger=None, token=None, trajectory_gap_step=None) -> SimpleNamespace` with `messages`, `payload`, `summary`, `input_tokens`, `input_budget`, `rollover_used`, `trim_passes`, and `count_fallback`.
- `messages` is the persistent live history; `payload` is the derived request including the lower-trust playbook block.

- [ ] **Step 1: Write the failing preflight tests**

Patch `count_agent_input_tokens` with counts `[9000, 9000, 4000]`, patch `rollover_agent_context` to return the same valid history, and assert one rollover, one complete-group trim, a final valid payload, and the calculated budget `5120` for `max_tokens=4096`.

```python
async def test_over_budget_rolls_once_then_trims_complete_groups(self):
    with patch.object(bot, "count_agent_input_tokens", AsyncMock(side_effect=[9000, 9000, 4000])), \
            patch.object(bot, "rollover_agent_context", AsyncMock(return_value=(messages, "summary"))) as rollover:
        result = await bot.prepare_agent_request_payload(
            workspace, messages, "", 12, 4096, {"tools": tools}
        )
    self.assertEqual(result.input_budget, 4864)
    rollover.assert_awaited_once()
    self.assertTrue(bot.validate_chat_payload(result.messages).ok)
    self.assertEqual(len([m for m in result.messages if bot._msg_role(m) == "tool"]), 1)
```

- [ ] **Step 2: Run the focused test to verify it fails**

Run: `python3 -m unittest test_context_budget.ContextPreflightTest -v`

Expected: FAIL because the preflight function and token-budget constants do not exist.

- [ ] **Step 3: Implement the preflight**

Use `10240 - max_tokens - 1024 - 256` as the input budget, with a floor of `1024`. Count the derived payload; when over budget, call `rollover_agent_context` once and recount. If still over, run `bound_agent_payload(..., max_chars=0)` against persistent history (with minimum retention `1`), rebuild, and recount. If one group remains over, clip only tool-result content in caps `1000`, `400`, and `160` characters, recounting after each cap. Preserve system/current-goal messages, run `validate_chat_payload`, and log only counts, budgets, rollover use, trim passes, and fallback status.

When the count endpoint returns `None`, estimate conservatively from the compact UTF-8 serialization of the converted payload (including tools and arguments), add framing allowance, and run the same trim-or-reject path. Never send a payload that remains over the selected budget.

- [ ] **Step 4: Wire the preflight before every normal agent completion**

Compute `step_max_tokens` before preparation, update `messages_payload` and `rolling_summary` from the returned persistent result, and pass the returned derived `payload` to `run_completion_stage`. Keep checkpoint, synthesis, and rollover model calls outside this preflight so summary generation cannot recurse. Rebuild the retry payload through the same existing derived builder after tool-protocol recovery.

- [ ] **Step 5: Run focused and regression tests**

Run: `python3 -m unittest test_context_budget.py test_payload_integrity.py test_hierarchical_memory.py test_durable_state.py -v`

Expected: PASS with no orphaned tool calls/results and no change to ledger or rollover invariants.

- [ ] **Step 6: Commit**

```bash
git add bot.py test_context_budget.py
git commit -m "feat: enforce a token-aware agent context budget"
```

### Task 3: Apply the single-request and documentation defaults

**Files:**
- Modify: `scripts/run_omlx.sh:20-28`
- Modify: `README.md:16,74,120-160`

**Interfaces:**
- The launch script passes `--max-concurrent-requests 1`.
- README documents tokenizer preflight, two retained groups, `AGENT_MAX_CONTEXT_TOKENS`, and the fact that old trajectory details are retrieved on demand.

- [ ] **Step 1: Write the static configuration check**

Add a focused assertion in `test_context_budget.py` that reads `scripts/run_omlx.sh`, finds `--max-concurrent-requests`, and asserts the following value is `1`.

- [ ] **Step 2: Run the check to verify it fails**

Run: `python3 -m unittest test_context_budget.LaunchConfigurationTest -v`

Expected: FAIL because the script still contains `4`.

- [ ] **Step 3: Change the script and README**

Change only the concurrency argument and stale compaction wording; document that the Mac's 32 GiB memory is intentionally protected by the token cap rather than an iogpu limit increase.

- [ ] **Step 4: Run the check and documentation diff validation**

Run: `python3 -m unittest test_context_budget.LaunchConfigurationTest -v` and `git diff --check`.

Expected: PASS and no whitespace errors.

- [ ] **Step 5: Commit**

```bash
git add README.md scripts/run_omlx.sh test_context_budget.py
git commit -m "ops: limit oMLX to one concurrent request"
```

### Task 4: Verify, publish, merge, and deploy

**Files:**
- Verify: repository test suite and remote LaunchAgent configuration

- [ ] **Step 1: Run the complete local suite**

Run: `python3 -m unittest discover -s . -p 'test_*.py'`

Expected: all tests pass.

- [ ] **Step 2: Review the final diff and identity**

Run: `git diff main...HEAD --stat`, `git diff main...HEAD --check`, and `git log -3 --format='%an <%ae> %s'`.

Expected: only the design/plan docs, `bot.py`, focused tests, README, and oMLX script changed; commits identify as `ghyen <79272189+ghyen@users.noreply.github.com>`.

- [ ] **Step 3: Push and open the PR as ghyen**

Run: `git push -u origin codex/token-aware-context-compaction` followed by `gh pr create --repo ghyen/autonomous-llm-bot --base main --head codex/token-aware-context-compaction` with a body describing the observed `kv_len=12288` prefill rejection, token-aware guard, concurrency `1`, test command, and why the iogpu limit was not raised.

- [ ] **Step 4: Verify the PR checks and merge it**

Run: `gh pr checks <number> --watch` and, after checks pass, `gh pr merge <number> --squash --delete-branch`.

- [ ] **Step 5: Update the remote checkout without touching existing backups**

Verify `/Users/edwin/discord-llm-bot` has no tracked changes, pull `origin main` with `--ff-only`, and confirm its HEAD contains the merged commit. Leave the existing untracked `.zvec-grep/` and `bot.py.*` backups untouched.

- [ ] **Step 6: Set and restart the remote services**

Use the native plist editor to change only `ProgramArguments` value `--max-concurrent-requests` from `4` to `1`, kickstart `gui/501/com.edwin.omlx-server`, then kickstart `gui/501/com.edwin.discord-llm-bot` so the deployed code and server setting are loaded together.

- [ ] **Step 7: Verify deployment before reporting completion**

Check `launchctl print gui/501/com.edwin.omlx-server` for `--max-concurrent-requests 1`, inspect fresh process arguments and startup logs, and confirm the bot revision/log startup line matches the merged `main` commit. Report the PR URL, merge commit, test result, and remote verification evidence.
