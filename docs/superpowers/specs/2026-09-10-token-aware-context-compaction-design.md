# Token-aware context compaction design

**Date:** 2026-09-10
**Scope:** Prevent local oMLX prefill rejection during long autonomous runs.

## Problem

The bot currently rolls context by step count and character count. Those limits
do not include the tool schema, serialized tool calls, or the tokenizer's actual
tokenization. In the failed run, compaction had completed repeatedly, but the
next request still reached `kv_len=12288`; oMLX rejected the prefill before the
model could answer. The Mac has 32 GiB unified memory, so raising the kernel
GPU-wired limit would trade a recoverable request failure for swap pressure or
system instability.

## Goals

- Keep every agent request below a conservative local-model context budget.
- Count the exact request shape, including tools, using oMLX's loaded tokenizer.
- Preserve complete assistant/tool groups and the authoritative ledger.
- Keep normal requests to one model request at a time.
- Work without adding a Python dependency and retain a safe fallback when the
  tokenizer endpoint is unavailable.
- Make the chosen budget and any emergency trimming observable in session logs.

## Design

### 1. Token-aware preflight

Immediately before the main agent completion, convert the OpenAI-compatible
messages and tool definitions to the local Anthropic-compatible
`/messages/count_tokens` request. This endpoint is already exposed by oMLX at
the configured `/v1` base URL and counts system text, messages, and tools with
the serving model's tokenizer.

The preflight uses a conservative total KV target of 10,240 tokens by default.
The current step's `max_tokens` and a 1,024-token transient reserve are deducted
from that target to calculate the input budget. The budget is configurable via
`AGENT_MAX_CONTEXT_TOKENS` so it can be tuned after observing the real model.

If the input count exceeds its budget, the loop performs the existing tiered
rollover once, rebuilds, and counts again. If it still exceeds the budget,
complete old assistant/tool groups are removed until the request fits. The
authoritative ledger and current user goal remain in the system/current-message
prefix. A failure to count uses a conservative character-bound fallback and is
logged; it never raises the memory ceiling.

### 2. Smaller verbatim tail

The default `KEEP_RECENT_TOOL_GROUPS` changes from 5 to 2. Tier 2/3 trajectory
summaries and `lookup_trajectory` remain the recovery path for older details.
Tool protocol validation runs after every trim, so no orphaned call/result can
reach the API.

### 3. Single-request server configuration

`scripts/run_omlx.sh` and the deployed launch agent use
`--max-concurrent-requests 1`. The bot does not intentionally issue parallel
model calls for one run; keeping the server at one request avoids competing KV
caches on the 32 GiB unified-memory Mac.

### 4. Observability

Log the measured input token count, calculated budget, whether rollover was
needed, and the number of emergency trim passes. Do not log prompt contents or
credentials.

## Non-goals

- Raising `machdep.cpu.iogpu.wired_limit_mb`.
- Replacing the existing tiered trajectory/ledger design.
- Adding a tokenizer package or implementing a second model-specific tokenizer.
- Changing the bot's user-visible failure wording in this patch.

## Acceptance criteria

1. A payload with tool calls and results is converted to a valid count request,
   including tools and preserving tool-call/result relationships.
2. An over-budget payload triggers at most one rollover, then deterministic
   complete-group trimming, and the final payload is protocol-valid.
3. The preflight is skipped for non-agent stages and does not recurse through
   rollover's summary completion.
4. Existing payload-integrity, rollover, durable-state, and full test suites
   pass.
5. The deployed oMLX launch arguments report concurrency `1` after restart.

