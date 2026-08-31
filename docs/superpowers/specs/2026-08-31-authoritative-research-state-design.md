# Authoritative Research State Design

## Problem

Long autonomous runs resurrect hypotheses they already refuted, and keep publishing conclusions whose premise was corrected. The cause is not model drift: `bot.py` has no authoritative record of what is currently true. Hypotheses, refutations and conclusions exist only as free text inside the request payload, and every context transformation is allowed to drop that text.

Five concrete leaks:

- Judgements that only appear in `reasoning_content` never reach the payload; only `content` and `tool_calls` are appended.
- `apply_micro_compaction()` rewrites every old tool result down to its first line, so a refutation on line two is gone by the next step.
- `build_rollup_source()` spends its 24000-character budget oldest-first and breaks when full, so the newest refutation is the first thing dropped.
- The checkpoint report is sent to Discord only. Rollover runs immediately afterwards on a payload that never saw the correction, and the success marker is appended even when the checkpoint call raised.
- `rollover_agent_context()` accepts any non-empty summary, and the resulting cumulative summary is stored inside the replaced `system` message, which `build_rollup_source()` skips — so final synthesis loses it.

## Decision

Hold the state outside the message payload, in a per-channel `ResearchLedger` (`ledger.py`), and render it into every context transformation and every report.

**Ledger.** Goals, evidence, hypotheses and conclusions. A hypothesis carries a status and a monotonically increasing revision; each transition records the evidence that caused it. A conclusion records the premise revisions it was derived from, and its validity is *derived*, never stored: a conclusion is invalid as soon as any premise's current revision differs from the recorded one. There is no code path that can leave a stale conclusion marked valid.

**Monotonic refutation.** `rejected` never returns to `active` through an ordinary update. Reopening requires an explicit `reopen` transition citing an evidence id that no earlier transition of that hypothesis already cited. Illegal transitions are refused and the refusal text is returned to the model as the tool result, so it learns the rule in-band.

**How state gets written.** A `record_state` tool. Raw chain-of-thought is never persisted as state; the model is instructed to emit short structured updates instead. The checkpoint reporter may additionally emit one fenced `state_update` JSON block, which is applied to the ledger *before* rollover and stripped from the Discord output.

**Where state gets read.** The rendered ledger is pinned as the tail of message 0 (the single merged `system` message) and rebuilt every step. Message 0 sits before the first tool message, so `apply_micro_compaction()` never touches it, and `sanitize_messages_for_chat_template()` keeps it in place. The same render is prepended to the rollover source, the checkpoint report input, and the final synthesis input.

**Supporting fixes.**

- `build_rollup_source()` allocates its budget newest-first and restores chronological order afterwards. The oldest material is already covered by the cumulative summary.
- Rollover validates its own output: a summary missing the ledger's state markers is corrected by re-attaching the render; a summary byte-identical to the previous one while the source has new content is rejected in favour of a deterministic fallback; an empty source skips compaction and is logged as a no-op.
- Checkpoint failure appends a failure marker, not `[중간 보고서 제출 완료]`.
- Final synthesis is fed the ledger, the cumulative summary, and the recent tail — not the tail alone.

## Verification

Ledger unit tests for the transition rules: refused reactivation, refused reopen on already-cited evidence, accepted reopen on new evidence, derived invalidation when a premise revision moves, and marker presence in the render.

One integration test reproduces the issue's synthetic scenario — `H_A` rejected at `v2` by `E_NEG` on the second line of an old tool result, conclusion `C_A` premised on `H_A@v1`, and a compactor stubbed to return the previous summary verbatim — and asserts the markers survive micro-compaction, checkpoint, rollover, a following run, and final synthesis.

## Out of Scope

Termination judgement and user-facing labels. Tool isolation and log exposure. No persistence to disk, no new dependency, no compatibility path for the old free-text behaviour.
