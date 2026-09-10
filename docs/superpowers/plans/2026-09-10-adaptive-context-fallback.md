# Adaptive Context Fallback Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep an over-budget agent step alive by shrinking its output reservation and, when necessary, rebuilding a minimal authoritative context before failing.

**Architecture:** Extend the existing token-aware preflight at `prepare_agent_request_payload`. After the current rollover and safe trims, lower the per-request output limit to the available budget; if fewer than the minimum output tokens remain, replace old message history with the current system state, bounded summary, continuation instruction, and the latest complete tool group, then recount. Only the minimal-payload overflow raises `AgentContextBudgetExceeded`.

**Tech Stack:** Python 3.10+, existing `unittest`, current oMLX token-count endpoint.

## Global Constraints

- Keep `AGENT_MAX_CONTEXT_TOKENS=10240` and existing reserves unchanged.
- Preserve tool-call/result pairing and authoritative ledger state.
- Do not add dependencies or log prompt/tool contents.
- Do not change unrelated rollover or resume behavior.

---

### Task 1: Add failing regression tests

**Files:**
- Modify: `test_context_budget.py`

- [ ] **Step 1: Add the adaptive-output test**

Patch the counter to return an over-budget count after safe trims where at least
the minimum output remains. Assert the result exposes a smaller
`output_max_tokens`, fits the recalculated input budget, and preserves a valid
payload.

- [ ] **Step 2: Add the emergency-rebase test**

Use a count sequence that stays over budget until a minimal system/summary/user
payload is rebuilt. Assert old tool groups are removed, the latest complete
group is retained when possible, and the final count is accepted.

- [ ] **Step 3: Run the focused tests and confirm RED**

Run `python3 -m unittest test_context_budget.ContextPreflightTest -v`.
The new tests must fail because the current preflight has no adaptive output or
emergency rebase path.

### Task 2: Implement the smallest fallback path

**Files:**
- Modify: `bot.py:1950-2055` (request preflight)
- Modify: `bot.py:4300-4345` (completion call)
- Test: `test_context_budget.py`

- [ ] **Step 1: Add a bounded emergency context builder**

Create one helper that keeps the freshly rendered system message, a single
continuation user message, and the latest complete assistant/tool group. Clip
the retained tool results with the existing helper and validate the payload.

- [ ] **Step 2: Adapt the output reservation**

After the existing trims, compute available output tokens from the measured
input count. If it is at least `MIN_AGENT_OUTPUT_TOKENS`, lower the returned
`output_max_tokens` and recompute `input_budget` instead of raising.

- [ ] **Step 3: Rebase and recount below the minimum**

When available output is below the minimum, rebuild the emergency context,
recount it, and apply the same adaptive output calculation. Raise only if that
minimal payload still cannot leave the minimum output budget.

- [ ] **Step 4: Pass the prepared output limit to the model call**

Use `prepared.output_max_tokens` for the agent completion, and log only the
mode (`adaptive_output` or `emergency_rebase`) plus numeric budget fields.

- [ ] **Step 5: Run focused tests and confirm GREEN**

Run `python3 -m unittest test_context_budget.py -v`.

### Task 3: Verify the repository

**Files:**
- Verify: all tracked source and tests

- [ ] **Step 1: Run syntax and focused checks**

Run `python3 -m py_compile bot.py test_context_budget.py` and
`git diff --check`.

- [ ] **Step 2: Run the full suite**

Run `python3 -m unittest discover -s . -p 'test_*.py'` and record the fresh
pass/skip counts.

- [ ] **Step 3: Request code review**

Review the branch diff for protocol pairing, output-budget propagation, and
the no-content-logging constraint before reporting completion.
