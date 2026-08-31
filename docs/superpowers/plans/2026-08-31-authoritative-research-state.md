# Authoritative Research State Implementation Plan

**Goal:** Give the agent one authoritative record of what is currently true, so a refutation cannot be lost by a context transformation and a conclusion cannot outlive its premise.

**Design:** See `docs/superpowers/specs/2026-08-31-authoritative-research-state-design.md`.

## Task 1: Ledger

Files: `ledger.py`, `test_ledger.py`.

- [x] Write failing transition tests: `rejected` cannot go back to `active`; `reopen` is refused when its evidence id was already cited for that hypothesis; `reopen` with new evidence advances the revision and reactivates; a conclusion premised on `H_A@v1` becomes invalid once `H_A` reaches `v2`; `render()` emits `H_A=rejected@v2`, the evidence id, and the conclusion's invalid marker.
- [x] Implement `ResearchLedger` with derived conclusion validity — no stored valid/invalid flag.
- [x] Add `state_markers()` for the summary validator and `apply_updates()` for the tool and checkpoint entry points.

## Task 2: Wire the ledger into the agent loop

Files: `bot.py`.

- [x] Add `channel_ledger` keyed by channel id so state survives into the next run; clear it in `!reset`, `/reset`, `!clear` and `/clear` alongside the other channel state.
- [x] Add the `record_state` tool: schema entry, dispatch in `execute_tools_in_parallel`, and system-prompt instructions telling the model to record judgements there instead of in its reasoning. Return the refusal text as the tool result when a transition is illegal.
- [x] Add `build_system_content()` and use it for the initial payload, for the per-step refresh of message 0, and for the rollover replacement, so the render is always pinned ahead of the first tool message.

## Task 3: Stop the transformations from dropping the newest state

Files: `bot.py`.

- [x] Change `build_rollup_source()` to allocate its budget newest-first, then restore chronological order.
- [x] Feed the checkpoint reporter the ledger render; parse and apply its optional fenced `state_update` block before rollover; strip the block from the Discord output.
- [x] Move the `[중간 보고서 제출 완료]` marker into the checkpoint success path and append a failure marker instead when the call raises.
- [x] Validate the rollover summary: re-attach the render when state markers are missing, reject a summary identical to the previous one when the source has new content, and skip compaction as a logged no-op when the source is empty.
- [x] Feed final synthesis the render, the cumulative summary, and the recent tail.

## Task 4: Prove the markers survive

Files: `test_state_flow.py`.

- [x] Build the issue's synthetic payload and ledger, stub the compactor to echo the previous summary, and drive micro-compaction, checkpoint, rollover, a following run and final synthesis.
- [x] Assert `E_NEG`, `H_A=rejected@v2` and `C_A`'s invalid state appear in every captured request payload, and that no path reactivates `H_A`.
- [x] Run `python3 -m unittest discover` and `python3 -m compileall` on the changed modules.

## Task 5: Ship

- [x] Update `README.md` for the new tool and the state guarantee.
- [ ] Commit, push the branch, open the PR against `main` referencing issue #1.


## Found while implementing

`_msg_content()` returned `None` for assistant tool-call messages, because
`content` is explicitly set to `None` there and `dict.get("content", "")` only
substitutes the default for a *missing* key. `rollover_agent_context()` then
called `len()` on it, so every rollover that followed a tool step aborted the
whole run through the loop's exception handler. `apply_micro_compaction()` had
the same exposure on `.split()` and `.startswith()`. Fixed once in the shared
accessor rather than at each call site.
