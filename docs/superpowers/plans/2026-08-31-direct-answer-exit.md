# Direct Answer Routing Implementation Plan

**Goal:** Keep simple Discord questions short instead of forcing them through the deep-research loop, without weakening genuine research requests.

**Design:**

1. An explicit trailing request such as `간단히 답해줘` uses one no-tool completion with a concise prompt, `reasoning_effort="none"`, and a 512-token cap.
2. Other requests use a lightweight first autonomous completion (`reasoning_effort="none"`, 1024 tokens). A non-empty no-tool response is accepted only on that first call.
3. Once a tool has run, the existing nudge, checkpoints, rolling compaction, and final synthesis remain unchanged.
4. Empty/error direct calls fall through to the loop but cannot treat the first progress sentence as final. Hidden `<think>` and tool markup never reach Discord.

## Task 1: Implement and verify routing

Files: `bot.py`, `test_bot.py`.

- [x] Reproduce the original behavior from the existing system log: a simple answer was nudged into the previous unfinished workspace research.
- [x] Add handler tests for a generic simple question, an explicit brief request, an empty direct response followed by progress then `finish_task`, and a researched request that continues after a progress message.
- [x] Add a guarded Korean/English brevity matcher. Anchor it to a complete trailing request so negated or multi-clause research requests do not take the direct route.
- [x] Add the no-tool direct completion path with fallback, response sanitization, Discord chunking, and history logging.
- [x] Add the first-call-only no-tool exit and low-cost initial routing parameters; leave post-tool behavior unchanged.
- [x] Verify with the remote Python environment: four tests, syntax compilation, diff whitespace check, matcher edge cases, and a live Qwen direct-response check.

## Task 2: Commit, push, and deploy safely

- [ ] Commit only `bot.py`, `test_bot.py`, and these routing docs.
- [ ] Push local `main` to `origin/main`.
- [ ] On the deployment host, stash only its pre-existing tracked `bot.py` configuration diff, fast-forward, and re-apply the stash. Keep all `bot.py.*` backups untracked.
- [ ] Run the test and compile checks in `/Users/edwin/discord-llm-bot`.
- [ ] Restart `com.edwin.discord-llm-bot`; verify the new process, branch, preserved host diff, and error log.
