# Local LLM follow-up verification (2026-09-06)

The handover report's PR #54 was already merged at `cc3084a`. Its 350 unit
tests passed, but a real multi-step run still reproduced a generation stall.

## Findings and changes

- First-turn and brief-answer paths accepted token-truncated text as completion.
  They now retry through the bounded agent loop.
- Tool dispatch ran before checking `finish_reason=length`. Truncated responses
  now reach the existing retry/nudge path before any tool or completion action.
- XML recovery accepted a parameter cut off mid-value. Missing outer wrappers
  remain recoverable, but incomplete parameters are refused.
- Invalid JSON arguments were silently replaced with `{}`, allowing malformed
  `finish_task` calls to finish a run. The original invalid value now reaches
  the existing argument-error feedback path.
- Streaming tests now cover a terminal chunk with no delta followed by a usage
  chunk, ensuring the preserved finish reason reaches the caller.

## Serving configuration

The serving host uses rapid-mlx 0.12.18 and the existing Qwen 27B hybrid model.
With prefix caching enabled, the first smoke-test step completed in 63.3 s.
Step 2 reused 2,843 cached prompt tokens and consumed its full 2,048-token output
budget in 204.9 s without a tool call. The server explicitly logged translation
of `reasoning_effort=none` to `enable_thinking=False` on this request.

Changing only `--enable-prefix-cache` to `--disable-prefix-cache` and restarting
the server let the same first two actions complete in 50.7 s and 51.4 s, without
reasoning output. The second action reached `read_file`; its requested `plan.md`
did not exist in a fresh test workspace. The smoke fixture was corrected to
create and read its own `smoke.txt` before finishing.

This comparison implicates the cache-reuse path in this serving configuration;
it does not isolate the precise internal cache defect. Keep prefix caching
disabled for this model until an upstream fix passes repeated multi-step tests.
The tradeoff is full prompt processing on each step. Model weights, quantization,
memory limits, and bot access policy were not changed.

The engine LaunchAgent's original plist is preserved beside it with suffix
`.before-codex-cache-review`. A changed plist must be reloaded with `bootout`
and `bootstrap`; `kickstart` alone continues using the loaded arguments.
Wait for the old process to exit before bootstrapping the same service label.

## Checks and limits

- Python 3.11.15 on the serving host: 355 unit tests passed; the opt-in live test
  is skipped during ordinary discovery (356 tests discovered).
- Compilation and the repository's credential-default check passed.
- The corrected live test passed in 184.9 seconds: sandboxed file creation,
  successful `read_file`, then `finish_task`. Model stages took 50.8 s, 50.9 s,
  and 83.0 s; all three had `effort=none` and zero reasoning characters.
  Run ID: `74a69bcdf6a846ae7b5fb2e65511b30a`.
- The live test uses real streaming and real sandboxed tools with temporary
  run state and fake Discord I/O. Run it with the bot's Python environment:

  ```bash
  RUN_LOCAL_LLM_SMOKE=1 python -m unittest test_local_llm_smoke -v
  ```

The code changes are in [PR #55](https://github.com/ghyen/autonomous-llm-bot/pull/55).
The serving checkout now tracks `fix/local-llm-cutoff-safety`; the bot was
restarted and logged Discord `ready`. The engine health endpoint reports
`healthy`, `ready=true`, and `model_loaded=true`. The PR remains unmerged.

The 2,000-step limit does not guarantee a 24-hour run. At 15-30 seconds per step
it allows about 8-17 hours, excluding overhead, and cold prompt processing may
take longer. No 24-hour soak test was performed. Context rollover, checkpointing,
cancellation, sandboxing, and durable recovery retain their unit coverage;
short live checks do not establish their long-duration reliability.
