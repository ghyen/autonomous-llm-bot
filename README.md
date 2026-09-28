# 🤖 Discord Autonomous LLM Agent Bot

A fully autonomous, goal-driven AI agent for Discord powered by local LLM backends (**llama.cpp**, Rapid-MLX, vLLM, Ollama) or remote OpenAI-compatible endpoints.

Built for long-horizon autonomous exploration: thousands of steps per run, with a research ledger, tiered procedural memory, and repetition guards that keep it from re-testing dead ends.

---

## ✨ Key Features

- 🧠 **Autonomous loop**: up to 2,000 steps per run with deep reasoning traces, self-reflection, and `finish_task` completion.
- 🧾 **Authoritative Research Ledger**: goals, evidence, hypotheses, and conclusions live outside the message payload and are re-pinned into every request. A refuted hypothesis needs an explicit reopen with new evidence; a conclusion auto-invalidates when a premise moves. Evidence uses `retracted`/`retracts` so corrections invalidate the original instead of duplicating it.
- 🗺️ **Tiered procedural memory**: the last tool groups stay verbatim; older history compacts into a Tier 2 step index and a bounded (2,000-char) Tier 3 procedure log that survives checkpoints. Exact old steps are recoverable with `lookup_trajectory`.
- 🛡️ **Repetition guards**: exact-duplicate calls, consecutive failures, and re-observation of already-answered targets (same URL 3+ times) are blocked with an explicit reason; `force=true` overrides. New evidence that contradicts recent measurements gets a grounding warning (never a block).
- 📉 **Stagnation index**: every checkpoint report shows novel vs re-observed findings over the trailing 100 steps, so a human can tell circling from progress.
- 💾 **Durable runs**: one atomic `state.json` per run; restarts resume the same run id or record exactly one explicit abort.
- 📁 **Owner-bound workspaces** with `sha256:` revision CAS on `plan.md`/`findings.md`/`playbook.md`.
- ⌨️ **Discord UX**: typing heartbeat, live status card, mid-flight steering, checkpoint reports.
- 🛠️ **Power tools**: `bash_exec`, `read_file`, `write_file`, `web_search` (allowlisted), `lookup_trajectory`, `record_state`, `record_playbook`, `think`, `finish_task`.
- ✅ **Payload validator, streaming collector, token-aware request guard** round out dispatch.

---

## 🏗️ Architecture Overview

```
Discord ──► Bot Gateway ──► Agent Loop ◄── Steering Queue
                                 │ 2,000 steps max
        ┌────────────────────────┼────────────────────────┐
        ▼                        ▼                        ▼
  Tool dispatch ──► Guard layer ──► Tool workers ──► Ledger
  (fingerprints,   (exact dup,      (resource-limited,    (record_state,
   loop guard,      consecutive      per-run cwd,           authoritative
   observation      failures,        no Seatbelt)           state block)
   guard, known     record-stale,
   bad, think)      grounding warn)
        │                                                     │
        └───────────── trajectory (traj.jsonl) ◄──────────────┘
                        │ append-only, per-step records
                        ▼
              Tier 2 index + Tier 3 rolling procedure
              (survives checkpoints; lookup_trajectory)
```

Tool results are resource-limited, not sandboxed: there is no Seatbelt/`sandbox-exec`
profile. Ceilings are CPU 30 s, RSS 256 MiB monitor (macOS rejects lowering
`RLIMIT_AS`, so a fail-closed RSS monitor covers the worker tree), 32 processes,
64 threads, 64 open files, 10 MiB/file, 64 KiB response, 50 MiB workspace bytes
(sampled, not a quota). Shell descendants run in their own process group and are
reaped on cancel/timeout/overflow/violation.

`web_search` goes through an explicitly allowlisted broker (`TOOL_NETWORK_ALLOWLIST`,
DuckDuckGo by default). That setting does **not** restrict `bash_exec`: direct
`curl` to external origins works. Service credentials and home credential files
are never handed to the shell.

---

## 🚀 Getting Started

### 1. Prerequisites

- Python 3.10+
- A running OpenAI-compatible LLM server (llama.cpp, Rapid-MLX, Ollama, vLLM)
- A Discord Bot Token ([Developer Portal](https://discord.com/developers/applications))

### 2. Installation

```bash
git clone https://github.com/ghyen/autonomous-llm-bot.git
cd autonomous-llm-bot
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # then fill in the token + allowed user IDs
```

### 3. Configuration

`config.py` reads `.env`, and a real environment variable always wins. Everything
is validated before any directory or socket is created; malformed values fail
startup instead of being silently dropped. See `.env.example` for the full list —
key budgets:

| Variable | Default | Effect |
| :--- | :--- | :--- |
| `MAX_AGENT_LOOPS` | `2000` | Step ceiling per run (not a runtime guarantee) |
| `CHECKPOINT_INTERVAL` | `50` | Context flush cadence (example file sets 25) |
| `REPORT_INTERVAL` | — | Discord report cadence, in steps |
| `MAX_TOOL_EXECUTIONS_PER_RUN` | `2000` | Actual tool executions; blocked calls don't count |
| `AGENT_STEP_MAX_TOKENS` | `2048` | Output cap per agent step |
| `AGENT_MAX_CONTEXT_TOKENS` | `16384` | Total context ceiling; payloads are counted pre-send |
| `LLM_BASE_URL` | `http://127.0.0.1:18080/v1` | Local endpoint |
| `LLM_ALLOW_REMOTE` | `false` | Required for any non-local base URL |

Startup fails loudly on a missing token, an empty `DISCORD_ALLOWED_USER_IDS`
(while tools are enabled), non-numeric IDs, non-positive budgets/timeouts, an
admin outside the allowed list, or a remote URL without `LLM_ALLOW_REMOTE=true`.
On startup the bot writes a secret-free record with the running commit and every
effective policy line.

At 15–30 s per step, 2,000 steps cover roughly 8–17 hours before checkpoint and
compaction overhead; cold prompt processing takes longer.

### 4. Running

```bash
make run        # server + bot (waits for health, then starts bot)
make run-bot    # bot only
make test       # full suite
./scripts/run_all.sh  # all-in-one launcher, Ctrl+C stops both cleanly
```

---

## 🔒 Log Hygiene and Retention

Every log line is one JSON object (`ts`, `rev`, `pid`, `run`, `step`, `kind`).
The default sink stores **metadata only** — no reasoning, arguments, results,
or user text. Raw content reaches disk only via opt-in content-debug.

| Variable | Default | Effect |
| :--- | :--- | :--- |
| `LOG_MAX_BYTES` | `1048576` | Rotates to `<run-id>.1.jsonl`; one generation kept |
| `LOG_RETENTION_DAYS` | `14` | Startup deletes older run logs and inactive workspaces |
| `LOG_CONTENT_DEBUG` | `false` | Writes raw content to `content-debug/`; deny-by-default |
| `LOG_CONTENT_DEBUG_RETENTION_HOURS` | `24` | Retention for that sink, in hours |

Directories are `0700`, files `0600`, corrected in place regardless of umask.

---

## 💾 Durable Run State and Restart Recovery

Recovery uses one durable record per run — `WORKSPACE_DIR/runs/<run-id>/state.json`
(written and read through `run_state.py`, temp-file/fsync/replace, never truncated): run id, message id, state, next-step
cursor, bounded summary + recent tail, interrupt/steering state, the full ledger,
announced call ids, replay fingerprints, and the first trajectory step with
uncertain coverage. A record is never written with a partial parallel group in
its tail.

On startup every unterminated record is settled exactly once: re-selected for
its owner/channel (same run id continues), or one explicit abort. Schema
mismatches are discarded, never migrated. A resumed run announces itself and
logs `run_resumed` before its first step. `!resume <full-run-id>` selects an
exact inactive owned run — **the full 32-hex id is required, prefixes do not
match**. `!reset`/`!new`/`!clear`/`!delete` delete the record.

---

## 🏁 Run Outcomes

| Reason | Reached by | User-facing |
| :--- | :--- | :--- |
| `completed` | `finish_task` (or tool-free direct answer) | `✅ 조사 완료` |
| `stopped` | `!stop` / `/stop` | `🛑 사용자 중단 — 미완료` |
| `exhausted` | Step budget spent, or repeated tool-free responses | `⚠️ 스텝 소진 — 미완료` |
| `failed` | A stage exceeds its deadline, or an unhandled upstream error | `❌ 실패 — 미완료` |

Only `finish_task` ends a run with completion intent — writing "최종 보고서" in
text does not. Companion calls arriving with `finish_task` are refused, not
executed, and listed so nothing is silently dropped. `!stop` cancels the
awaited stage and awaits cleanup; stopped/deadline-failed runs get a bounded
deterministic partial report.

---

## 📁 Workspaces, Files, and Integrity

- Roots only: `WORKSPACE_DIR/runs/<random-run-id>/`, logs under `SYSTEM_LOG_DIR`.
  Runs never derive paths from Discord IDs. No list/share surface; admins get no
  implicit workspace access.
- `plan.md`/`findings.md`/`playbook.md` are canonical with `sha256:` revisions
  and compare-and-swap atomic writes; stale writes get `conflict`. Only
  `playbook.md` is inherited, and only by an automatic successor. `run.json`,
  `state.json`, `traj.jsonl` are reserved run state.
- All file paths resolve through one choke point against the run root after
  `realpath`: absolute host paths, `..` traversal, and planted symlinks fail
  closed. Session logs live outside every run root and are unreachable by
  file tools. `record_state`/`finish_task` run in-process (they own live
  state); everything else needing files goes through tools.
- Per-execution read cache (128 entries, full-byte hashes): unchanged reads
  return a hash reference; `!resume` starts with an empty cache.

---

## 🔐 Access Control

Every Discord entry point passes one deny-by-default gate before anything is
logged, read, changed, queued, or dispatched. **DMs and mentions route; they
never identify.** Identity is `DISCORD_ALLOWED_USER_IDS`.

| Action | Who |
| :--- | :--- |
| Talk to the bot | On `DISCORD_ALLOWED_USER_IDS` |
| `!stop`, `!reset`, `!new`, steering | Active-run owner or admin; any allowed caller with no run in flight |
| `!resume` / `!fork` / `!delete` + full run id | Exact run owner only; admins get no workspace access |
| `!clear` (bulk delete) | Admin **and** the caller's own Manage Messages permission |

---

## 🎮 Discord Commands & Controls

| Command | Description |
| :--- | :--- |
| `!stop` / `/stop` | Cancels the awaited stage; deterministic partial report. Run stays resumable |
| `!reset` / `!new` | Clears channel memory, prepares a blank run; rejects with an active run |
| `!resume <full-run-id>` | Selects an exact inactive owned run (prefixes do not match) |
| `!fork <full-run-id>` | Fresh run inheriting only goal, bounded summary, and ledger |
| Continuation-intent message | e.g. "이전 데이터 참고해서 계속해줘" resumes the newest valid incomplete run |
| `!delete <full-run-id>` | Deletes an inactive owned workspace + log. Active/cross-owner rejected |
| `!clear [count]` | Purges Discord messages, then resets; failure changes nothing |
| `/reasoning [level]` | `none`, `low`, `medium`, `high`. Any allowed caller |

---

## 🧪 Tests

```bash
make test   # python3 -m unittest discover -s . -p "test_*.py"
```

Coverage follows the subsystems: config/deadlines, cancellation flow, authz
placement, terminal-state machine, ledger transitions (incl. retraction and
`retracts`), tiered memory + flush/rollover/resume, trajectory integrity,
observation-repeat guard, stagnation index, grounding warnings, workspace
isolation, tool sandbox ceilings. `test_support.py` holds bootstrap and
Discord doubles; every identifier in tests is synthetic.

CI (`.github/workflows/ci.yml`) runs the suite plus a credential-default scan
and `compileall` on macOS with Python 3.10 and 3.12. The opt-in live check
(`RUN_LOCAL_LLM_SMOKE=1 python -m unittest test_local_llm_smoke -v`, ~10 min)
drives the configured endpoint end-to-end without sending Discord messages.

---

## 🖥️ macOS LaunchAgent Daemon (Optional)

`~/Library/LaunchAgents/com.edwin.discord-llm-bot.plist`:

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
        <string>/path/to/autonomous-llm-bot/bot.py</string>
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

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.edwin.discord-llm-bot.plist  # start
launchctl bootout gui/$(id -u)/com.edwin.discord-llm-bot                                  # stop (never kill -9 a run in flight)
```

---

## 📄 License

MIT License. Feel free to modify and deploy!
