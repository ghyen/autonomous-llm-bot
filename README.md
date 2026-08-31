# 🤖 Discord Autonomous LLM Agent Bot

A fully autonomous, self-extending goal-driven AI agent for Discord powered by local LLM backends (like **Rapid-MLX**, **vLLM**, **Ollama**, or **llama.cpp**) or remote OpenAI-compatible endpoints.

Designed for long-horizon autonomous exploration, terminal execution, research, and deep reasoning with rich Discord UI interactions.

---

## ✨ Key Features

- 🧠 **Fully Autonomous Goal-Driven Loop**: Runs up to 250 iterative tool-execution loops with deep reasoning (`<think>`) traces, self-reflection, and goal completion checks.
- 🧾 **Authoritative Research State (Ledger)**: Goals, evidence, hypotheses and conclusions are held in a per-channel ledger outside the message payload, and re-pinned into every request, checkpoint, rollover and final report. A refuted hypothesis cannot return to `active` without an explicit reopen citing new evidence, and a conclusion is automatically invalid the moment a premise revision moves.
- 🔄 **Rolling Context Compaction (Rollup Architecture)**: Every 10 steps, old execution history is dynamically rolled up and summarized by the LLM, keeping context bounded and preventing out-of-memory context blowups. The rollup budget is spent newest-first, and a summary that drops state markers or echoes the previous one is rejected or corrected.
- ⌨️ **Keep-Alive Continuous Typing Heartbeat**: A background 7-second heartbeat maintains Discord's typing state continuously so the user always knows the agent is active.
- 📱 **Real-Time Live Dashboard Card (`message.edit`)**: Continuously updates a single status card in Discord with elapsed time, step progress, real-time thought snippet, and current tool execution.
- 🛠️ **Built-in Power Tools**:
  - `bash_exec`: Run arbitrary shell commands (curl, python3, grep, jq, etc.) in a sandbox workspace with output auto-truncation.
  - `read_file`: Inspect local files with line truncation.
  - `write_file`: Create and modify files in the workspace.
  - `web_search`: Live DuckDuckGo search.
  - `record_state`: Record goals, evidence, hypotheses and conclusions in the authoritative ledger. Judgements belong here, not in the reasoning trace, which does not survive to the next step.
  - `finish_task`: Explicit task completion tool to synthesize the final markdown report.
- 💬 **Mid-Flight Dynamic User Steering**: Users can send messages into the channel while the agent is running; instructions are automatically queued and injected into the agent's next step without restarting.
- 🛡️ **Chat Template Sanitizer (400 Error Immunity)**: Automatically cleanses multi-system messages to adhere strictly to Qwen/DeepSeek Jinja chat templates (`System message must be at the beginning`).
- ⚡ **Streaming Completion Delta Collector**: Reconstructs tool calls and reasoning deltas on the fly.

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
               │   (Max 250 Steps)           │
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
               │  Research Ledger            │  record_state
               │  (hypotheses / evidence /   │◄─────────────
               │   conclusions, per channel) │
               └──────────────┬──────────────┘
                   state block pinned into
                   every payload and report
                              │
               ┌──────────────▼──────────────┐
               │  10-Step Rolling Compaction │
               │  & Checkpoint Synthesis     │
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
```

Startup fails, loudly, when:

- `DISCORD_BOT_TOKEN` is missing or blank.
- `DISCORD_ALLOWED_USER_IDS` is empty while tools are enabled. The agent runs
  shell commands on the host, so there is no safe default. Set
  `BOT_TOOLS_ENABLED=false` to run a tool-free bot without an allowlist.
- Any ID list contains a non-numeric entry.
- `DISCORD_ADMIN_USER_IDS` names someone outside `DISCORD_ALLOWED_USER_IDS`.
- `LLM_BASE_URL` is non-local without `LLM_ALLOW_REMOTE=true`. Conversation
  content and tool results would leave the machine.

See `.env.example` for the full set. On startup the bot prints a secret-free
diagnostic block including a fingerprint of the effective policy.

### 4. Running the Bot

```bash
python bot.py
```

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
| `!stop`, `!reset`, mid-flight steering | The owner of the active run, or a `DISCORD_ADMIN_USER_IDS` admin. With no run in flight, any allowed caller. |
| `!clear` / `/clear` (bulk delete) | An admin **and** the caller's own Discord "Manage Messages" permission. The bot's permission is not the caller's. |

Text commands and slash commands share the same policy path. Deletion is
attempted before any conversation state is cleared, so a permission failure
leaves your history intact and is reported as a failure.

---

## 🎮 Discord Commands & Controls

| Command | Description |
| :--- | :--- |
| `!stop` / `/stop` | Interrupts autonomous execution and synthesizes a final report from collected data. Run owner or admin. |
| `!reset` / `/reset` | Clears conversation history, context cache, and the channel's research ledger. Run owner or admin. |
| `!clear [count]` / `/clear` | Purges recent channel messages, then resets context and the research ledger. Admin with Manage Messages. |
| `/reasoning [level]` | Changes reasoning effort (`none`, `low`, `medium`, `high`). Any allowed caller. |

---

## 🧪 Tests

```bash
python3 -m unittest discover
python3 tools/check_no_credential_defaults.py bot.py config.py ledger.py tools
```

`test_config.py` covers configuration loading and its failure modes, `test_authz.py` and `test_authz_handlers.py` cover the authorization policy and its placement ahead of every side effect, `test_ledger.py` covers the state transition rules, `test_state_flow.py` proves the state markers survive micro compaction, checkpoints, rollover, a following run, and final synthesis, and `test_bot.py` covers request routing. `test_support.py` holds the shared bootstrap and Discord doubles; every identifier in tests is synthetic.

Both commands also run in CI (`.github/workflows/ci.yml`) on Python 3.10 and 3.12.

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
