# 🤖 Discord Autonomous LLM Agent Bot

A fully autonomous, self-extending goal-driven AI agent for Discord powered by local LLM backends (like **Rapid-MLX**, **vLLM**, **Ollama**, or **llama.cpp**) or remote OpenAI-compatible endpoints.

Designed for long-horizon autonomous exploration, terminal execution, research, and deep reasoning with rich Discord UI interactions. OS-facing tools are implemented for the macOS LaunchAgent deployment described below.

---

## ✨ Key Features

- 🧠 **Fully Autonomous Goal-Driven Loop**: Runs up to 2,000 iterative tool-execution loops by default with deep reasoning (`<think>`) traces, self-reflection, and goal completion checks.
- 🧾 **Authoritative Research State (Ledger)**: Goals, evidence, hypotheses and conclusions are held in a per-channel ledger outside the message payload, and re-pinned into every request, interim report, rollover and final report. A refuted hypothesis cannot return to `active` without an explicit reopen citing new evidence, and a conclusion is automatically invalid the moment a premise revision moves. The whole ledger round-trips through the run's durable record, so a restart cannot resurrect a rejected hypothesis as fact.
- 💾 **Durable Run State**: Each run keeps one atomic record (`runs/<run-id>/state.json`) carrying its run id, originating message id, state, next step cursor, bounded summary and tail, interrupt state, ledger, every announced tool-call id, replay-guard fingerprints, and the earliest trajectory coverage gap. It is written before the first model call, before uncertain dispatches, and after every completed assistant/tool group, never with a partial group in its tail. A restart resumes the same run id at its next step or records exactly one explicit abort.
- 📁 **Owner-Bound Run Workspaces**: Every accepted top-level request receives an opaque `runs/<run-id>/` directory and opaque per-run log. Runs never derive paths from Discord IDs; exact owners can resume or delete inactive runs, while admins receive no implicit workspace access.
- 🔄 **Canonical File Revisions**: Root `plan.md`, `findings.md`, and `playbook.md` use exact-byte `sha256:` revisions, compare-and-swap writes, and atomic replacement. `playbook.md` is inherited only by an automatic successor created when no prepared or resumed run is selected for that owner and channel; explicit `!new` and `!reset` prepared runs start blank. `run.json`, `state.json`, and `traj.jsonl` remain run-local reserved state. Per-execution read hashes return bounded references for unchanged content.
- 🗺️ **Token-aware Tiered Trajectory Compaction**: Before each normal agent request, the bot counts the complete payload (including tools) with the serving tokenizer. It keeps the latest 2 complete assistant/tool groups by default, rolls the older history into Tier 2/3 trajectory summaries when the input budget is exceeded, and trims complete groups before clipping only tool-result bodies as an emergency guard. A restored run first applies deterministic summary compaction (latest Tier 2 lines, bounded Tier 3/discovery lines) and recounts before removing any complete groups. Every completed tool group also appends bounded arguments and results to the run-local mode-`0600` `traj.jsonl`; the model can recover an exact old step with `lookup_trajectory`. Goals, facts, conclusions, and hypothesis status remain exclusively in the authoritative ledger, which is always the final section of the system message. Artifact pointers may enter rollover discovery only when they were host-recorded for the current run and still validate as regular files.
- 🧩 **Run-Local Workspace Skills**: Reusable Python (`.py`), Shell (`.sh`/`.bash`), and Markdown (`.md`) skills are discovered from the current run's `skills/` directory and rendered into its system prompt. They are isolated from other runs and retained only when that exact run is resumed.
- ⌨️ **Keep-Alive Continuous Typing Heartbeat**: A background 7-second heartbeat maintains Discord's typing state continuously so the user always knows the agent is active.
- 📱 **Real-Time Live Dashboard Card (`message.edit`)**: Continuously updates a single status card in Discord with elapsed time, step progress, real-time thought snippet, and current tool execution.
- 🛠️ **Built-in Power Tools**:
  - `bash_exec`: Run arbitrary shell commands (curl, python3, grep, jq, etc.) in a disposable macOS Seatbelt worker rooted at the current run directory.
  - `read_file`: Read relative paths from the current run. First or changed reads include a full-byte revision; unchanged reads return a hash reference.
  - `write_file`: Write ordinary run files, including reusable scripts in `skills/`, directly; root `plan.md`, `findings.md`, and `playbook.md` require an optimistic `expected_revision`.
  - `web_search`: Live DuckDuckGo search through the explicitly allowlisted worker broker.
  - `lookup_trajectory`: Recover bounded records from an exact compacted trajectory step; oversized aggregate results are stored as run artifacts.
  - `record_state`: Record goals, evidence, hypotheses and conclusions in the authoritative ledger. Judgements belong here, not in the reasoning trace, which does not survive to the next step.
  - `record_playbook`: Merge one model-authored environment constraint, invalid path, or successful pattern into canonical `playbook.md` as lower-trust procedural context; system policy and the current user request always take precedence.
  - `finish_task`: Explicit task completion tool to synthesize the final markdown report.
- 💬 **Mid-Flight Dynamic User Steering**: Users can send messages into the channel while the agent is running; instructions are automatically queued and injected into the agent's next step without restarting.
- 🛡️ **Pre-Send Payload Validator**: One local validator checks every outgoing payload before it leaves — tool_call ids non-empty and unique, exactly one result per announced call, groups adjacent, no orphan results, `system` only at index 0 — and repairs the live history in place. A tool-correlation error retries once with the tool protocol erased; other 400s settle like any other failure and leave a masked role/id fingerprint.
- ⚡ **Streaming Completion Delta Collector**: Reconstructs tool calls and reasoning deltas on the fly. Index-less fragments continue the call in progress instead of splitting it, fallback ids are unique for the whole process, and a fragment that never named a function is refused before dispatch.

---

## 🏗️ Architecture Overview

```
User Prompt (Discord) ────────┐
                              ▼
                   ┌───────────────────────┐
                   │  Discord Bot Gateway  │
                   └──────────┬────────────┘
                              │
               ┌──────────────▼──────────────┐
               │   Autonomous Agent Loop     │◄────── Dynamic User Steering Queue
               │  2,000 Steps (configurable) │
               └──────────────┬──────────────┘
                              │
          ┌───────────────────┼───────────────────┐
          ▼                   ▼                   ▼
    ┌───────────┐       ┌───────────┐       ┌───────────┐
    │ bash_exec │       │ read_file │       │web_search │
    │(Workspace)│       │write_file │       │ (DDGS)    │
    └───────────┘       └───────────┘       └───────────┘
          │                   │                   │
          └───────────────────┼───────────────────┘
                              │
                              ▼
               ┌─────────────────────────────┐
               │ macOS Seatbelt Tool Worker  │
               │ one-shot / deny-by-default  │
               └──────────────┬──────────────┘
                              │
                              ▼
               ┌─────────────────────────────┐
               │  Research Ledger            │  record_state
               │  (hypotheses / evidence /   │◄─────────────
               │   conclusions, per channel) │
               └──────────────┬──────────────┘
                   state block pinned into
                   every payload and report
                              │
               ┌──────────────▼──────────────┐
               │ Token-aware Request Guard   │
               │ & Tiered Compaction         │
               └──────────────┬──────────────┘
                              │
                              ▼
                      Local Rapid-MLX /
                      OpenAI API Server
```

---

## 🚀 Getting Started

### 1. Prerequisites

- Python 3.10+
- A running OpenAI-compatible local LLM server (e.g. [Rapid-MLX](https://github.com/alexw/rapid-mlx) with Qwen 2.5 / Qwen 3.8 / DeepSeek, Ollama, or vLLM)
- A Discord Bot Token ([Discord Developer Portal](https://discord.com/developers/applications))

### 2. Installation

```bash
# Clone repository
git clone https://github.com/ghyen/autonomous-llm-bot.git
cd autonomous-llm-bot

# Create and activate virtual environment
python3 -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt
```

### 3. Configuration

Create `.env` from `.env.example`:

```bash
cp .env.example .env
```

`config.py` reads `.env` and the process environment; a real environment variable
always wins over the file. Configuration is fully validated before the bot
creates any directory or opens any socket, and malformed values fail startup
rather than being silently dropped.

```env
DISCORD_BOT_TOKEN=your_discord_bot_token_here
DISCORD_ALLOWED_USER_IDS=111111111111111111,222222222222222222
LLM_BASE_URL=http://127.0.0.1:18080/v1
MODEL_NAME=default
DISCORD_FREE_RESPONSE_CHANNELS=123456789012345678
MAX_AGENT_LOOPS=2000
CHECKPOINT_INTERVAL=50
MAX_TOOL_EXECUTIONS_PER_RUN=2000
AGENT_STEP_MAX_TOKENS=4096
AGENT_MAX_CONTEXT_TOKENS=10240
REASONING_MAX_TOKENS=1536
DEFAULT_REASONING_EFFORT=high
ADAPTIVE_REASONING=true
LLM_CONNECT_TIMEOUT_SECONDS=15
LLM_IDLE_TIMEOUT_SECONDS=3600
MODEL_STAGE_TIMEOUT_SECONDS=3600
TOOL_STAGE_TIMEOUT_SECONDS=120
BASH_TIMEOUT_SECONDS=60
TOOL_CPU_SECONDS=30
TOOL_MEMORY_BYTES=268435456
TOOL_PROCESS_LIMIT=32
TOOL_THREAD_LIMIT=64
TOOL_OPEN_FILES=64
TOOL_FILE_BYTES=10485760
TOOL_OUTPUT_BYTES=65536
TOOL_DISK_BYTES=52428800
LOG_MAX_BYTES=1048576
LOG_RETENTION_DAYS=14
LOG_CONTENT_DEBUG=false
LOG_CONTENT_DEBUG_RETENTION_HOURS=24
```

The autonomous loop defaults to 2,000 model iterations, checkpoints every 50
steps, allows at most 2,000 actual tool executions per run, limits each
agent step to 4,096 output tokens, caps internal reasoning to 1,536
tokens, and dynamically adapts reasoning effort during tool execution. Before
each normal agent request it targets a conservative 10,240-token total context,
reserving the configured output budget, 1,024 transient tokens, and 256 tokens
for request-format counting differences; older complete tool groups are
summarized or trimmed when needed. The serving tokenizer is used when
available, and a conservative UTF-8-aware tool/schema estimate is used
otherwise;
an unsafe request is rejected before send. The 32 GiB macOS deployment
therefore stays within the model's memory headroom without raising the kernel
iogpu wired limit.
Override those limits with `MAX_AGENT_LOOPS`, `CHECKPOINT_INTERVAL`,
`MAX_TOOL_EXECUTIONS_PER_RUN`, `AGENT_STEP_MAX_TOKENS`,
`AGENT_MAX_CONTEXT_TOKENS`, `REASONING_MAX_TOKENS`, `DEFAULT_REASONING_EFFORT`,
and `ADAPTIVE_REASONING`.

Network is deny-by-default. Enable `web_search` only when the operator wants
the worker to use DuckDuckGo:

```env
TOOL_NETWORK_ALLOWLIST=https://html.duckduckgo.com:443
```

The allowlist accepts only HTTP(S) origins without credentials, query strings,
fragments, or non-root paths. Direct `bash_exec` access to external origins is
not enabled by this setting on macOS; external destinations that cannot be
represented by the Seatbelt profile fail closed. `web_search` uses a fixed
parent loopback CONNECT broker for the single configured DuckDuckGo origin.

The wait budgets are separate: connection establishment is 15 seconds, the
maximum idle gap between stream chunks is 3600 seconds, one model stage totals
3600 seconds, a tool batch totals 120 seconds, and a shell process totals 60
seconds. A tool-correlation recovery retry consumes only the remaining time from
the original model-stage budget. Timed-out or cancelled shell commands kill and
reap their whole process group.

Startup fails, loudly, when:

- `DISCORD_BOT_TOKEN` is missing or blank.
- `DISCORD_ALLOWED_USER_IDS` is empty while tools are enabled. The agent can
  dispatch OS-facing work to a sandboxed worker, so there is no safe default.
  Set
  `BOT_TOOLS_ENABLED=false` to run a tool-free bot without an allowlist.
- Any ID list contains a non-numeric entry.
- Any timeout is not a finite number greater than zero.
- Any of `MAX_AGENT_LOOPS`, `CHECKPOINT_INTERVAL`,
  `MAX_TOOL_EXECUTIONS_PER_RUN`, or `AGENT_STEP_MAX_TOKENS` is not a whole
  integer greater than zero.
- `LOG_MAX_BYTES` is not a whole number greater than zero.
- `LOG_RETENTION_DAYS` or `LOG_CONTENT_DEBUG_RETENTION_HOURS` is not a finite
  number greater than zero.
- `DISCORD_ADMIN_USER_IDS` names someone outside `DISCORD_ALLOWED_USER_IDS`.
- `LLM_BASE_URL` is non-local without `LLM_ALLOW_REMOTE=true`. Conversation
  content and tool results would leave the machine.
- Any `TOOL_*` limit is missing a positive finite value, or a network origin is
  malformed.

See `.env.example` for the full set. On startup the bot writes a secret-free
`startup` record carrying the running commit, dependency versions, and every
effective policy line.

### 4. Running the Bot

You can run the bot and the backend server easily using `make` or the helper scripts in `scripts/`:

```bash
# Run both rapid-mlx server and bot (starts server, waits for health check, then runs bot)
make run

# Or run components individually:
make run-rapid  # Starts rapid-mlx server standalone (port 18080)
make run-bot    # Starts autonomous-llm-bot standalone

# Run test suite:
make test
```

Direct script execution is also supported:

```bash
# All-in-one launcher with graceful shutdown (Ctrl+C terminates both cleanly)
./scripts/run_all.sh

# Standalone server with customizable model path
MODEL_PATH="/path/to/model" ./scripts/run_rapid.sh

# Standalone bot
./scripts/run_bot.sh
```

---

## 🔒 Log Hygiene and Retention

Every log line and every console line is one JSON object carrying `ts`, `rev`
(running commit), `pid`, `run`, `step`, and `kind`. The default sink stores
**metadata only**: event kind, tool name, status, duration, and sizes. Raw
reasoning, tool arguments, tool results, and user text have no route into it,
and raw reasoning is not sent to Discord either — progress is reported as a
bounded status line instead.

| Variable | Default | Effect |
| :--- | :--- | :--- |
| `LOG_MAX_BYTES` | `1048576` | A run log rotates to `<run-id>.1.jsonl` past this size. One previous generation is kept. |
| `LOG_RETENTION_DAYS` | `14` | Startup deletes run logs and inactive run workspaces older than this. |
| `LOG_CONTENT_DEBUG` | `false` | Turning this on writes raw reasoning, tool arguments, and tool results to `SYSTEM_LOG_DIR/content-debug/<run-id>.jsonl`. |
| `LOG_CONTENT_DEBUG_RETENTION_HOURS` | `24` | Retention for that content sink — hours, not days. |

Directories are `0700` and files are `0600` regardless of the umask, and paths
that predate this are corrected in place. `LOG_CONTENT_DEBUG` is the one setting
that puts content back on disk, so it is deny-by-default and its state is printed
in the startup diagnostics.

---

## 💾 Durable Run State and Restart Recovery

**The 50-step Discord interim progress report is a briefing, not a recovery
point.** It is sent to a channel and recorded in the log, and nothing ever read
it back. Recovery uses one separate durable record per run:

- Record: `WORKSPACE_DIR/runs/<run-id>/state.json`, inside the run's own `0700`
  workspace next to `run.json`, written with the same temp-file/fsync/replace
  used for canonical files — so an interrupt during a write cannot truncate it.
- Contents: run id, originating Discord message id, run state, next step cursor,
  the bounded cumulative summary, a bounded tail of recent payload messages, the
  interrupt state (cancellation reason and steering queue counters), the full
  ledger, every announced tool-call id, replay-guard fingerprints, and the first
  trajectory step whose durable coverage is uncertain.
- Write boundaries: before the first model call, before dispatch when reserving
  uncertain fingerprints and a conservative trajectory gap, after every
  completed assistant/tool group, right after an interim report's ledger
  corrections are applied, and right after a rollover writes its new summary
  back. A record is **never** written with a partial parallel call/result group
  in its tail — an incomplete final group is dropped whole, because restoring
  half a group both breaks the next request and could re-run side effects.
- Bound: the tail keeps 12 messages of at most 2,000 characters each. Anything
  older is represented by the cumulative summary, which is what the summary is
  for.

On startup every unterminated record (`state` still `running`) is settled
exactly once. Either the run is re-selected for its owner and channel, so the
next accepted goal consumes the same run id and continues at its next cursor, or
exactly one `run_abort` record is written and the record is deleted. A record
that does not match the schema is discarded, never migrated. There is no third
path: silently restarting at Step 1 is what made a restart indistinguishable
from a new request. A resumed run announces itself in the channel and logs a
`run_resumed` record before its first step. Any reused id that was already
announced is refused with a deterministic `already_announced` result before
dispatch.

A run that ends normally marks its record ended, so startup leaves it alone.
`!resume <run-id>` remains the exact inactive-run selector. A non-command
message with explicit continuation intent such as “이전 데이터 참고해서
계속해줘” automatically selects the newest valid incomplete run for the same
owner and channel; an ordinary new goal always starts a fresh run. `!reset`,
`!new`, `!clear` and `!delete` delete the record, so a discarded run cannot
come back after a restart.

---

## 🏁 Run Outcomes

A run settles on exactly one terminal reason, and that single value decides
everything after it — whether nudges, tool dispatch, interim reports and rollover
continue, which synthesis prompt is used, and what label the user sees.

| Reason | Reached by | User-facing |
| :--- | :--- | :--- |
| `completed` | `finish_task`, or a tool-free direct answer on the first call | `✅ 조사 완료` + 완료 시간 footer |
| `stopped` | `!stop` / `/stop` | `🛑 사용자 중단 — 미완료`, no completion footer |
| `exhausted` | Step budget spent, or too many consecutive tool-free responses | `⚠️ 스텝 소진 — 미완료` |
| `failed` | A model, tool, interim-report or rollover stage exceeds its deadline, or an unhandled upstream exception occurs | `❌ 실패 — 미완료` |

**Completion intent is a structured signal only.** Writing "최종 보고서" in the
response body does not end a run; `finish_task` does. Previously a run counted as
finished if the text contained 최종/보고서/결론 and exceeded 400 characters, which
both ignored short correct completions and dressed up interrupted and
step-exhausted runs with a completion label.

**Companion call policy:** tool calls arriving in the same response as
`finish_task` are **not executed** — no side effects land after a completion
decision. The refused tool names are recorded in the session log and listed in
the final message, so nothing is silently dropped.

**Stop and deadline policy:** `!stop` and `/stop` cancel the currently awaited
model, stream, tool batch, interim report, rollover, or synthesis stage and await
its cleanup. No retry, tool, interim report, rollover, or model synthesis starts after
cancellation. Stopped and deadline-failed runs use a deterministic bounded
partial report from preserved state; an exhausted run may use one bounded final
synthesis. A rollover summary timeout is handled inside that bounded stage with
deterministic local compaction so the run can continue without losing context;
rollover cancellation still stops the run. User cancellation, stage timeout,
upstream failure, exhaustion, and normal completion remain distinct in the final
status.

**Tool execution guardrails:** ordinary tool calls are filtered only after the
post-dashboard cancellation check and before dispatch. A reused id that was
already announced is answered with a deterministic `already_announced` result
and never dispatched. A fingerprint is derived from the tool's effective
side-effecting fields rather than ignored metadata. Within one model response,
only the first identical action can execute; every original call ID still gets
a tool result, with blocked calls receiving deterministic structured JSON. The
same unchanged call may fail twice consecutively, but its third and later
immediately consecutive attempts are blocked until a different signature is
dispatched or a call succeeds. Failures are limited to the existing explicit
contracts: a leading `[Error`, a nonzero `bash_exec` exit-code marker, or a
structured failure from a workspace, trajectory, or state tool. Each run
dispatches at most `MAX_TOOL_EXECUTIONS_PER_RUN` actual tool executions (2,000
by default); blocked calls consume no budget. The consecutive failure cap is
`MAX_CONSECUTIVE_FAILED_TOOL_CALLS = 2`.

---

## 📁 Run Workspace Lifecycle and Integrity

`WORKSPACE_DIR` and `SYSTEM_LOG_DIR` are roots only. Each accepted top-level
(non-steering) request atomically consumes a prepared/resumed run or creates a
fresh opaque run:

- Workspace: `WORKSPACE_DIR/runs/<random-run-id>/`
- Log: `SYSTEM_LOG_DIR/runs/<random-run-id>.jsonl`
- Metadata: atomic `run.json` containing owner, channel, lifecycle status, and
  timestamps. Startup changes stale `active` metadata to resumable
  `interrupted`; legacy root files are not migrated or used as a fallback.

A direct-only answer still owns a run identity and log but executes no tools. A
direct-answer fallback into autonomous mode keeps the same run. Cleanup persists
`completed`, `stopped`, `exhausted`, `failed`, or `interrupted` while retaining
workspace bytes. `!new`/`!reset` prepare a blank run and keep old bytes; `!stop`
retains the stopped run; `!resume <run-id>` selects an exact inactive owned run
with an empty read cache; and `!delete <run-id>` removes an inactive owned
workspace and log, including rotated log generations. `!clear` purges Discord
first and performs the same reset only on success. `!reset`, `!new`, `!clear`
and `!delete` also delete the run's durable state record. A selected run is consumed once, and one run cannot be active twice.
Continuation-intent natural language requests select the newest valid
stopped, exhausted, failed, or interrupted run for the same owner and channel;
generic new requests do not. Summary compaction during this automatic or
explicit resume only shortens procedural context—the ledger remains the
authoritative source for facts, conclusions, and hypothesis state.
There is no list/share surface, and admin control authority is not workspace
read/delete authority: cross-owner IDs return `run not found`.

Both file tools resolve every path through one choke point: a relative path is
joined to the current run root, an absolute path is taken as given, and the
result is compared against the root after `realpath` on both sides. Anything that
lands outside the root is refused before any byte is read or written, so absolute
host paths, `..` traversal, and symlinks planted inside the root by `bash_exec`
all fail. A contained target is then re-expressed under the run root, so an
absolute or symlinked alias of `plan.md`/`findings.md` cannot skip their revision
check. Session logs live under the system log directory, outside every run root,
so no file-tool path reaches them and past-run bytes cannot re-enter the current
context through `read_file`. OS-facing tools run in a fresh one-shot macOS
Seatbelt worker with an explicit environment allowlist (`PATH`, `LANG`, `LC_ALL`,
`HOME`, and `TMPDIR` pointed at the run root, plus the Python no-user-site
flags). Service credentials in the bot process environment and home credential
files are not handed to the shell. `record_state` and `finish_task` are the
explicit parent-process exceptions because they own the live ledger and
terminal state; they do not run shell commands or open arbitrary files.

### Tool-worker contract

Every `bash_exec`, `read_file`, `write_file`, and `web_search` call starts a new
disposable tool worker and sends exactly one JSON request. Tool operations execute
directly through the dedicated worker process without macOS Seatbelt (`sandbox-exec`)
kernel restrictions, eliminating operation-not-permitted errors while preserving
resource limits and structured execution envelopes.

Shell descendants stay in a dedicated process group and are reaped on
cancellation, timeout, output overflow, or a resource violation.

The default ceilings are CPU 30 seconds, memory RSS 256 MiB, 32 processes, 64
threads, 64 open files, 10 MiB per file, 65,536 response bytes, and 50 MiB of
workspace bytes. CPU, open-file, file-size, and core-dump limits use kernel
resource limits. macOS rejects lowering `RLIMIT_AS`, so the worker enforces the
memory ceiling with a fail-closed RSS monitor over the worker and its child
tree. Workspace bytes are sampled every 50 ms; this is a bounded monitoring
ceiling, not a filesystem quota.

Root `plan.md`, `findings.md`, and `playbook.md` are canonical. Their revision is
`sha256:<64 lowercase hex>` over exact bytes, or `absent` before creation. A
write must provide the exact last revision; comparison occurs under that file's
lock, stale writes return `conflict` without changing bytes, and valid writes
flush/fsync a sibling temporary file before atomic replacement. Concurrent
writers using the same revision yield one success and one conflict. Only
`playbook.md` is inherited, and only by an automatic successor created when no
prepared or resumed run is selected for that owner and channel. Explicit `!new`
and `!reset` prepared runs start blank. `plan.md` and `findings.md` start absent,
while `run.json`, `state.json`, and `traj.jsonl` remain reserved run state. First
or changed canonical reads return complete content and revision; unchanged reads
return only a hash reference. Ordinary changed reads retain the 4,000-character
display cap. Every read hashes full bytes, successful writes seed the cache, and
the per-execution LRU holds at most 128 entries; resume begins empty.

---

## 🔐 Access Control

The agent runs shell commands and reads and writes files on the host, so every
Discord entry point passes a single deny-by-default gate (`authz.py`) *before*
anything is logged, any state is read or changed, any message is queued or
deleted, and any model or tool call happens.

**DMs and mentions are routing signals, not identity.** They decide where a
message came from, never who sent it. Identity is `DISCORD_ALLOWED_USER_IDS`.

| Action | Who |
| :--- | :--- |
| Talk to the bot at all | On `DISCORD_ALLOWED_USER_IDS` |
| `!stop`, `!reset`, `!new`, mid-flight steering | The owner of the active run, or a `DISCORD_ADMIN_USER_IDS` admin. With no run in flight, any allowed caller. |
| `!resume <run-id>` / `!fork <run-id>` / `!delete <run-id>` | Exact run owner only after normal caller authorization; admin status does not grant workspace access. |
| `!clear` / `/clear` (bulk delete) | An admin **and** the caller's own Discord "Manage Messages" permission. The bot's permission is not the caller's. |

Text commands and slash commands share the same policy path. Deletion is
attempted before any conversation state is cleared, so a permission failure
leaves your history intact and is reported as a failure.

---

## 🎮 Discord Commands & Controls

| Command | Description |
| :--- | :--- |
| `!stop` / `/stop` | Cancels and reaps the in-flight stage, then sends a deterministic partial report. The stopped run remains resumable. |
| `!reset` / `/reset` | Clears channel memory and prepares a blank run for the next goal; rejects while the caller/channel owns an active run. |
| `!new` / `/new` | Alias of reset with the same authorization and active-run preflight. |
| `!resume <run-id>` / `/resume` | Selects the exact inactive run for its owner; the next accepted goal consumes it with an empty read cache. |
| `!fork <run-id>` / `/fork` | Creates a fresh run with only the source run's recovered goal, bounded summary, and ledger; old files and full history stay isolated. |
| Continuation-intent message | A message such as “이전 데이터 참고해서 계속해줘” resumes the newest valid incomplete run in the same owner/channel; an ordinary new goal starts a new run. |
| `!delete <run-id>` / `/delete` | Deletes an exact-owner inactive workspace and its run log. Active runs are rejected; cross-owner IDs are not disclosed. |
| `!clear [count]` / `/clear` | Preflights active state, purges recent Discord messages, then prepares a blank run and clears channel memory. Failure changes nothing. |
| `/reasoning [level]` | Changes reasoning effort (`none`, `low`, `medium`, `high`). Any allowed caller. |

---

## 🧪 Tests

```bash
./venv/bin/python -m unittest discover -v
./venv/bin/python -m compileall -q bot.py config.py authz.py outcome.py deadlines.py ledger.py run_state.py run_workspace.py workspace_io.py session_log.py steering.py tool_sandbox.py tool_worker.py tools
./venv/bin/python tools/check_no_credential_defaults.py bot.py config.py authz.py outcome.py deadlines.py ledger.py run_state.py run_workspace.py workspace_io.py session_log.py steering.py tool_sandbox.py tool_worker.py tools
```

`test_config.py` covers configuration loading and deadline defaults,
`test_deadlines.py` covers cancellation/deadline cleanup, and
`test_cancellation_flow.py` covers stop latency, shared retry budgets, process
reclamation, and model/tool/interim-report/rollover/synthesis boundaries.
`test_authz.py` and `test_authz_handlers.py` cover the authorization policy and
its placement ahead of every side effect, `test_outcome.py` and
`test_terminal_state.py` cover the terminal state machine and end-of-run
scenarios, `test_ledger.py` covers the state transition rules,
`test_state_flow.py` proves state markers survive micro compaction, interim
reports, rollover, a following run, and final synthesis,
`test_durable_state.py` covers the durable run record in `run_state.py`: the
round trip, the parallel-group save boundary, atomic replacement, restart
recovery and explicit abort, the rollover write-back, and record deletion on
reset, `test_workspace_integrity.py`
covers opaque identity, lifecycle/privacy, lexical paths, canonical CAS, bounded
hash caching, per-run prompt/log/dispatcher wiring, concurrent isolation, and
sandbox-supervisor routing, and `test_tool_sandbox.py` covers the real macOS
Seatbelt profile, network policy, resource ceilings, and process cleanup.
`test_bot.py` covers request routing. `test_support.py` holds the shared
bootstrap, temporary catalog helper, and Discord doubles; every identifier in
tests is synthetic.

All three commands also run in CI (`.github/workflows/ci.yml`) on macOS with
Python 3.10 and 3.12.

### Local LLM Integration Check

With the local server running, use the bot's Python environment:

```bash
RUN_LOCAL_LLM_SMOKE=1 python -m unittest test_local_llm_smoke -v
```

This opt-in check uses the configured endpoint and model, real streaming and
sandboxed tools, a temporary run workspace, and fake Discord messages. It checks
`bash_exec`, `read_file`, and `finish_task` across multiple steps, including
`reasoning_effort=none` and the absence of reasoning output. It sends no Discord
messages and has a ten-minute total timeout. Ordinary test discovery skips it.

The default 2,000-step/tool budgets are ceilings, not a 24-hour runtime guarantee.
At 15-30 seconds per step, 2,000 steps cover roughly 8-17 hours before checkpoint
and compaction overhead. Cold prompt processing can take longer. Token caps bound
output size; `MODEL_STAGE_TIMEOUT_SECONDS` bounds request time. A passing smoke
check does not replace a 24-hour soak test.

For the Qwen hybrid / rapid-mlx 0.12.18 deployment, prefix-cache reuse still
reproduced a token-exhaustion stall with thinking disabled. The serving host
currently uses `--disable-prefix-cache`; see the
[verification report](docs/local-llm-verification-20260906.md) for measurements
and the successful three-step live check.

---

## 🖥️ macOS LaunchAgent Daemon (Optional)

To keep the bot running 24/7 in the background on macOS:

Create `~/Library/LaunchAgents/com.edwin.discord-llm-bot.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.edwin.discord-llm-bot</string>
    <key>ProgramArguments</key>
    <array>
        <string>/path/to/venv/bin/python</string>
        <string>-u</string>
        <string>/path/to/discord-autonomous-llm-bot/bot.py</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>DISCORD_BOT_TOKEN</key>
        <string>YOUR_TOKEN</string>
        <key>LLM_BASE_URL</key>
        <string>http://127.0.0.1:18080/v1</string>
    </dict>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>/path/to/bot.log</string>
    <key>StandardErrorPath</key>
    <string>/path/to/bot.error.log</string>
</dict>
</plist>
```

Load and start the service:

```bash
launchctl load -w ~/Library/LaunchAgents/com.edwin.discord-llm-bot.plist
```

---

## 📄 License

MIT License. Feel free to modify and deploy!
