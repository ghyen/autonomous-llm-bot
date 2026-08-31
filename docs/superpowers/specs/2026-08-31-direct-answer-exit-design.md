# Direct Answer Exit Design

## Problem

Every accepted Discord message enters the autonomous loop. When the LLM returns a complete answer without tools, the wrapper rejects it unless it is a 400+ character report and injects a command to continue researching.

## Decision

Use two small routing guards:

1. An explicit brevity request such as `간단히 답해줘` gets one direct LLM call with a concise-answer prompt, no tools, `reasoning_effort="none"`, and a 512-token cap.
2. Every other request gets a lightweight first autonomous call (`reasoning_effort="none"`, 1024 tokens). If that first call returns useful text without tools, accept it as the answer. If it starts tool work, keep the existing autonomous loop and its progress-message nudge unchanged.

Strip inline `<think>` and tool markup before sending a direct answer. The explicit route is a wording heuristic anchored to a complete trailing answer request, not a second classifier; requests that negate or continue into research (for example, `간단히 말하지 말고 조사해줘`) do not match it. If the direct call is empty or errors, the normal loop retries and does not accept its first progress sentence as a final answer.

## Verification

Add handler-level regression tests with fake Discord messages and stubbed external responses. They prove that an explicit brief request uses one tool-free call, a normal simple question stops after its first no-tool response, an empty direct response falls back to the autonomous loop, and a researched request continues after a progress message until `finish_task`.

## Out of Scope

No separate classifier call, semantic intent model, new configuration, compatibility path, or autonomous-loop refactor.
