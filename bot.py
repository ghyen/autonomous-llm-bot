#!/usr/bin/env python3
"""
Discord LLM Bot - Auto-Extension Goal-Driven Autonomous Agent
Integrated with:
- User's Streaming Completion & Rolling Compaction (Rollup) Architecture
- Chat Template Multi-System Message Sanitizer (400 Error Immunity)
- Continuous Keep-Alive Typing Heartbeat Loop (7s Interval)
- Live Updating Real-Time Dashboard Card (message.edit)
- 10-Step Periodic Checkpoints & Instant Feedback Loop
"""

import os
import sys
import re
import json
import time
import signal
import asyncio
from datetime import datetime
from collections import defaultdict
from typing import List, Dict, Any, Optional

import discord
from discord.ext import commands
import httpx
from openai import AsyncOpenAI
from duckduckgo_search import DDGS
from types import SimpleNamespace

import authz
import outcome as outcome_mod
from deadlines import (
    CancelToken,
    RunCancelled,
    StageBudget,
    StageTimeout,
    _reap,
    stream_chunks,
    with_deadline,
)
from outcome import RunOutcome
from ledger import ResearchLedger
from config import ConfigError, load_config, startup_diagnostics

# Configuration is fully validated before any filesystem or network side effect.
try:
    CONFIG = load_config()
except ConfigError as config_error:
    print(f"[Config Error] {config_error}", file=sys.stderr)
    sys.exit(1)

DISCORD_TOKEN = CONFIG.discord_token
LLM_BASE_URL = CONFIG.llm_base_url
MODEL_NAME = CONFIG.model_name
FREE_RESPONSE_CHANNEL_IDS = set(CONFIG.free_response_channel_ids)
ALLOWED_USER_IDS = set(CONFIG.allowed_user_ids)
ADMIN_USER_IDS = set(CONFIG.admin_user_ids)
TOOLS_ENABLED = CONFIG.tools_enabled
WORKSPACE_DIR = CONFIG.workspace_dir
SYSTEM_LOG_DIR = CONFIG.system_log_dir

for diagnostic_line in startup_diagnostics(CONFIG):
    print(f"[Config] {diagnostic_line}", flush=True)

os.makedirs(WORKSPACE_DIR, exist_ok=True)
os.makedirs(SYSTEM_LOG_DIR, exist_ok=True)

SYSTEM_PROMPT = f"""당신은 터미널 환경과 전용 작업 공간(workspace)에 직접 접근할 수 있는 **완전자율 목표 달성 AI 에이전트**입니다.

[운영 환경]
- 작업 디렉토리: `{WORKSPACE_DIR}`
- 시스템 파일:
  - `{WORKSPACE_DIR}/plan.md`: 에이전트의 목표 달성 체크리스트 및 실시간 진행 상태
  - `{WORKSPACE_DIR}/findings.md`: 수집된 핵심 데이터, 단서, 팩트, 취약점 및 결론 누적 기록
- 사용할 수 있는 도구:
  - `bash_exec(command)`: 쉘 명령어 실행 (curl, python3, nmap, jq, sed, awk, find, grep 등).
  - `read_file(path)`: 파일 읽기
  - `write_file(path, content)`: 파일 생성 및 덮어쓰기
  - `web_search(query)`: DuckDuckGo 웹 검색
  - `record_state(...)`: 목표·증거·가설·결론의 권위 있는 상태를 갱신하는 전용 도구
  - `finish_task(report)`: 사용자의 목표를 100% 달성하여 최종 결론을 낼 때 호출하는 전용 완료 도구

[상태 관리 - 자율 탐색의 절대 규칙]
`[권위 있는 조사 상태]` 블록이 이번 조사에서 무엇이 사실인지에 대한 유일한 권위입니다.
- 판단을 추론(생각)에만 남기지 마세요. 추론은 다음 스텝에 남지 않습니다. 가설을 세우거나 반증하거나 결론을 내릴 때마다 즉시 `record_state`로 짧은 구조화된 갱신을 기록하세요.
- 증거는 먼저 `evidence`에 id·요약·출처로 등록하고, 가설 전이는 그 증거 id를 인용하세요.
- 반증된 가설(`rejected`)을 다시 유망한 후보로 되살리려면, 이전에 인용하지 않은 새 증거를 등록하고 `status="reopen"`으로 요청해야 합니다. 그냥 다시 `active`로 쓰는 요청은 거부됩니다.
- 결론은 `premises`에 근거 가설 id를 명시하세요. 전제가 교체되면 그 결론은 자동으로 무효가 되며, 무효 결론을 현재 사실처럼 보고하지 마세요.
- 요약이나 보고서가 상태 블록과 다르면 상태 블록이 옳습니다.

[요청 라우팅 - 최우선]
먼저 사용자의 요청을 판단하세요.
- 단순한 설명·개념 질문·대화이거나 짧은 답을 요청한 경우: 도구를 호출하지 말고 현재 대화만으로 짧게 답하세요. 웹 검색, 파일 조회, `plan.md`/`findings.md` 확인, 긴 추론을 하지 마세요.
- 최신 정보·외부 사실 검증, 파일·로그·코드 조회, 명령 실행·수정, 다단계 조사가 필요한 경우: 아래 자율 탐색 지침에 따라 도구를 사용하세요.

[딥리서치 요청에만 적용하는 자율 탐색 지침]
1. 목표를 달성할 때까지 멈추지 말고 필요한 도구를 연속적으로 실행하세요.
2. 중간에 추측하지 말고 반드시 도구(`bash_exec`, `web_search` 등)를 통해 사실을 검증하세요.
3. 발견된 사실은 `findings.md`에 지속적으로 기록하고 `plan.md`의 진행 상태를 업데이트하세요.
4. 가설을 세우거나 반증하거나 결론을 내린 스텝에서는 같은 스텝에 `record_state`를 호출해 상태를 갱신하세요.
5. 모든 목표가 완전히 해결되었을 때만 `finish_task(report=...)`를 호출하여 최종 보고서를 제출하세요.
"""

DIRECT_RESPONSE_PATTERN = re.compile(
    r"(?:^|[?!.。,，。！？:：]\s*)"
    r"(?:간단히|간단하게|짧게|한\s*줄(?:로)?|핵심만|답만|결론만)\s*"
    r"(?:답|답변|대답|설명|알려|말해)"
    r"(?:해\s*(?:요|줘|주세요|줘요)?|줘(?:요)?|주세요)?"
    r"\s*(?:[?!.。,，。！？])?\s*$"
)
DIRECT_RESPONSE_EN_PATTERN = re.compile(
    r"^(?:please\s+)?(?:answer|reply)\s+"
    r"(?:briefly|concise(?:ly)?|in a short answer)\s*[?!.。,，。！？]?\s*$",
    re.IGNORECASE,
)
DIRECT_RESPONSE_PROMPT = """사용자가 짧은 답변을 요청했습니다. 현재 대화만 사용해 질문에 바로 짧게 답하세요.
도구, 웹 검색, 파일 조회, 자율 탐색을 하지 말고 추론 과정도 공개하지 마세요.
현재 대화만으로 확실히 답할 수 없으면 추측하지 말고 추가 확인이 필요하다고 짧게 말하세요."""


def wants_direct_response(text: str) -> bool:
    normalized = re.sub(r"\s+", " ", text.lower()).strip()
    return bool(
        DIRECT_RESPONSE_PATTERN.search(normalized)
        or DIRECT_RESPONSE_EN_PATTERN.search(normalized)
    )


def clean_direct_response(text: str) -> str:
    text = re.sub(r"<think>.*?(?:</think>|$)", "", text, flags=re.DOTALL)
    text = re.sub(
        r"<tool_call>.*?</tool_call>|<tool_call>.*|<function=[^>]*>.*?</function>|"
        r"<parameter=[^>]*>.*?</parameter>|</?(function|parameter|tool_call)[^>]*>",
        "",
        text,
        flags=re.DOTALL,
    )
    return text.strip()

TOOLS_SCHEMA = [
    {
        "type": "function",
        "function": {
            "name": "bash_exec",
            "description": f"작업 공간({WORKSPACE_DIR}) 내에서 쉘 명령어를 실행합니다. timeout은 {CONFIG.bash_timeout:g}초입니다.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "실행할 bash 쉘 명령어 (예: curl, python3, find, grep, cat 등)"
                    }
                },
                "required": ["command"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "파일의 내용을 읽어옵니다.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "읽을 파일 경로 (절대경로 또는 workspace 기준 상대경로)"
                    }
                },
                "required": ["path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "파일에 내용을 작성하거나 덮어씁니다.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "작성할 파일 경로"
                    },
                    "content": {
                        "type": "string",
                        "description": "작성할 텍스트 내용"
                    }
                },
                "required": ["path", "content"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "DuckDuckGo를 통해 웹에서 최신 정보나 기술 문서를 검색합니다.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "검색할 키워드 또는 질문"
                    }
                },
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "record_state",
            "description": (
                "목표·증거·가설·결론의 권위 있는 상태를 갱신합니다. 판단은 추론에 남기지 말고 "
                "이 도구로 기록하세요. 반증된 가설을 다시 active로 만드는 요청은 거부되며, "
                "새 증거를 인용한 status=\"reopen\"만 허용됩니다."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "goal": {
                        "type": "string",
                        "description": "현재 조사 목표 (변경이 없으면 생략)"
                    },
                    "evidence": {
                        "type": "array",
                        "description": "새로 확인한 증거. 가설 전이보다 먼저 등록해야 합니다.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {"type": "string", "description": "짧은 증거 식별자 (예: E_NEG)"},
                                "summary": {"type": "string", "description": "증거 요약 한두 문장"},
                                "source": {"type": "string", "description": "출처 URL, 파일 경로 또는 명령어"}
                            },
                            "required": ["id", "summary"]
                        }
                    },
                    "hypotheses": {
                        "type": "array",
                        "description": "가설 선언 또는 상태 전이",
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {"type": "string", "description": "짧은 가설 식별자 (예: H_A)"},
                                "statement": {"type": "string", "description": "가설 진술"},
                                "status": {
                                    "type": "string",
                                    "enum": ["active", "rejected", "confirmed", "reopen"],
                                    "description": "active/rejected/confirmed, 또는 반증된 가설을 되살리는 reopen"
                                },
                                "evidence_id": {"type": "string", "description": "이 전이의 근거 증거 id"},
                                "note": {"type": "string", "description": "짧은 부가 설명"}
                            },
                            "required": ["id"]
                        }
                    },
                    "conclusions": {
                        "type": "array",
                        "description": "결론. premises의 전제 가설이 교체되면 자동으로 무효가 됩니다.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {"type": "string", "description": "짧은 결론 식별자 (예: C_A)"},
                                "statement": {"type": "string", "description": "결론 진술"},
                                "premises": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                    "description": "이 결론이 의존하는 가설 id 목록"
                                }
                            },
                            "required": ["id", "statement"]
                        }
                    }
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "finish_task",
            "description": "사용자의 목표를 100% 완수하여 모든 조사가 끝났을 때 최종 결론 보고서를 제출하며 자율 탐색을 공식 종료합니다.",
            "parameters": {
                "type": "object",
                "properties": {
                    "report": {
                        "type": "string",
                        "description": "사용자에게 전달할 한국어 최종 종합 보고서 전문"
                    }
                },
                "required": ["report"]
            }
        }
    }
]

intents = discord.Intents.default()
intents.message_content = True

class CustomBot(commands.Bot):
    async def setup_hook(self):
        try:
            await self.tree.sync()
            print("[Discord LLM Bot] Slash commands synced successfully!", flush=True)
        except Exception as e:
            print(f"[Discord LLM Bot] Slash sync notice: {e}", flush=True)

bot = CustomBot(command_prefix="!", intents=intents)

channel_history = defaultdict(list)
channel_summary = defaultdict(str)
channel_reasoning = defaultdict(lambda: "high")
# Public control follows the newest active lease when one exists; otherwise it
# follows the newest pending direct lease. The list restores the surviving
# caller-owned controller after either overlap completion order and never gates,
# queues, or delays admission.
channel_cancel_token = {}
channel_run_leases = defaultdict(list)
channel_ledger = defaultdict(ResearchLedger)

channel_active_runs = defaultdict(bool)
channel_user_queue = defaultdict(list)
channel_run_owner = {}


def caller_can_manage_messages(channel, user) -> bool:
    """The caller's own Discord permission - not the bot's.

    DM channels have no permission model, so this is False there and purging in
    a DM is refused.
    """
    permissions_for = getattr(channel, "permissions_for", None)
    if permissions_for is None:
        return False
    try:
        return bool(getattr(permissions_for(user), "manage_messages", False))
    except Exception:
        return False


def authorize_caller(action, user_id, channel_id=None, caller_can_manage_messages=False):
    """Every entry point - text command, slash command, steering - lands here."""
    return authz.authorize(
        action,
        user_id,
        allowed_user_ids=ALLOWED_USER_IDS,
        admin_user_ids=ADMIN_USER_IDS,
        tools_enabled=TOOLS_ENABLED,
        run_owner_id=channel_run_owner.get(channel_id),
        caller_can_manage_messages=caller_can_manage_messages,
    )


def agent_tool_params() -> dict:
    """Honor BOT_TOOLS_ENABLED. With tools off the model is never offered any."""
    if not TOOLS_ENABLED:
        return {}
    return {"tools": TOOLS_SCHEMA, "tool_choice": "auto"}


def request_run_cancel(channel_id, reason=outcome_mod.DETAIL_USER_STOP) -> bool:
    """Cancel the in-flight run for a channel. False when nothing is running."""
    token = channel_cancel_token.get(channel_id)
    if token is None:
        return False
    token.cancel(reason)
    print(f"[Cancel requested] channel={channel_id} reason={reason}", flush=True)
    return True


def clear_channel_state(channel_id) -> None:
    channel_history[channel_id].clear()
    channel_summary[channel_id] = ""
    channel_ledger[channel_id].clear()
    channel_user_queue[channel_id].clear()


MAX_RECENT_TURNS = 8
CHECKPOINT_INTERVAL = 10
MAX_AGENT_LOOPS = 250
MAX_CONSECUTIVE_FAILED_TOOL_CALLS = 2
MAX_TOOL_EXECUTIONS_PER_RUN = 250

MAX_NO_TOOL_RESPONSES = 6


def _blocked_tool_result(reason: str, tool_name: str, limit: int, count: int) -> str:
    return json.dumps(
        {
            "blocked": True,
            "reason": reason,
            "tool": tool_name,
            "limit": limit,
            "count": count,
            "directive": (
                "Change the arguments or use a different approach before retrying."
            ),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _tool_result_failed(tool_name: str, result: str) -> bool:
    if result.startswith("[Error"):
        return True
    if tool_name == "bash_exec":
        exit_code = re.search(r"\[exit code: (-?\d+)\]\s*$", result)
        return bool(exit_code and int(exit_code.group(1)) != 0)
    if tool_name == "record_state":
        return result.startswith("상태 갱신을 거부했습니다:") or bool(re.match(
            r"^(?:반영: [^\n]+\n\n)?거부:\n- ", result
        ))
    return False


ROLLING_COMPACTION_INTERVAL = 10
KEEP_RECENT_TOOL_MESSAGES = 8
ROLLING_SUMMARY_SOURCE_MAX_CHARS = 24000
ROLLING_SUMMARY_MAX_CHARS = 10000

# Transport-level bounds. Application-level stage budgets live in deadlines.py;
# these stop a request from hanging below the layer those budgets can see.
client = AsyncOpenAI(
    base_url=LLM_BASE_URL,
    api_key=CONFIG.llm_api_key,
    timeout=httpx.Timeout(
        CONFIG.model_stage_timeout,
        connect=CONFIG.connect_timeout,
        read=CONFIG.idle_timeout,
    ),
    max_retries=0,
)
# OpenAI initializes this resource lazily; resolve it before the request event
# loop starts so the first request cannot block cancellation progress.
client.chat.completions

async def keep_typing_heartbeat(channel, stop_event: asyncio.Event):
    """상시 '입력 중...' 상태를 7초마다 갱신하여 끊김 없이 유지하는 하트비트 루프"""
    while not stop_event.is_set():
        try:
            await channel.typing()
        except Exception:
            pass
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=7.0)
        except asyncio.TimeoutError:
            pass

def strip_ansi(text: str) -> str:
    ansi_regex = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
    return ansi_regex.sub("", text)

def format_elapsed_time(seconds: float) -> str:
    total_sec = int(seconds)
    if total_sec < 60:
        return f"{seconds:.1f}초"
    elif total_sec < 3600:
        mins = total_sec // 60
        rem_sec = total_sec % 60
        return f"{mins}분 {rem_sec}초"
    else:
        hours = total_sec // 3600
        mins = (total_sec % 3600) // 60
        return f"{hours}시간 {mins}분"

def log_session_event(session_file: str, title: str, content: str):
    try:
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(session_file, "a", encoding="utf-8") as f:
            f.write(f"## [{now_str}] {title}\n\n{content}\n\n---\n\n")
    except Exception as e:
        print(f"[Log Error]: {e}", file=sys.stderr, flush=True)

async def _auto_delete_notice(msg: discord.Message, delay: int = 6):
    await asyncio.sleep(delay)
    try:
        await msg.delete()
    except Exception:
        pass

# --- Tool Execution Functions ---

async def _terminate_process_tree(proc, process_group_id: int) -> None:
    """Kill the child's captured process group and reap its direct child.

    `proc.pid` is the process-group id because the shell is spawned with
    `start_new_session=True`. Capture it at spawn time: once the shell exits,
    looking the group up through that dead leader is no longer reliable.
    """
    if proc is None:
        return
    try:
        os.killpg(process_group_id, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        if proc.returncode is None:
            try:
                proc.kill()
            except Exception:
                pass
    try:
        await asyncio.wait_for(proc.wait(), timeout=5.0)
    except Exception:
        pass

async def tool_bash_exec(command: str) -> str:
    proc = None
    process_group_id = None
    try:
        print(f"[Tool: bash_exec] {command}", flush=True)
        # start_new_session makes the shell PID the stable process-group id for
        # descendants, even if the shell exits before its inherited pipes close.
        proc = await asyncio.create_subprocess_shell(
            command,
            cwd=WORKSPACE_DIR,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        process_group_id = proc.pid
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=CONFIG.bash_timeout
            )
        except asyncio.TimeoutError:
            await _terminate_process_tree(proc, process_group_id)
            return f"[Error: Command timed out after {CONFIG.bash_timeout:g} seconds]"
        except asyncio.CancelledError:
            await _terminate_process_tree(proc, process_group_id)
            raise

        out_str = stdout.decode("utf-8", errors="replace")
        err_str = stderr.decode("utf-8", errors="replace")
        code = proc.returncode

        res = ""
        if out_str:
            res += f"[stdout]\n{strip_ansi(out_str)}\n"
        if err_str:
            res += f"[stderr]\n{strip_ansi(err_str)}\n"
        res += f"[exit code: {code}]"

        if len(res) > 4000:
            res = res[:4000] + "\n... [출력 결과가 너무 길어 4000자로 잘렸습니다. 필요한 경우 grep이나 head/tail로 조회하세요.]"
        return res.strip() or "[Command executed successfully with no output]"
    except Exception as e:
        return f"[Error executing bash command: {e}]"

async def tool_read_file(path: str) -> str:
    try:
        target_path = path if os.path.isabs(path) else os.path.join(WORKSPACE_DIR, path)
        print(f"[Tool: read_file] {target_path}", flush=True)
        if not os.path.exists(target_path):
            return f"[Error: File not found at {target_path}]"
        with open(target_path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        if len(content) > 4000:
            content = content[:4000] + f"\n... [파일 내용이 너무 길어 4000자까지만 표시되었습니다. 전체 크기: {len(content)}자]"
        return content or "[File is empty]"
    except Exception as e:
        return f"[Error reading file: {e}]"

async def tool_write_file(path: str, content: str) -> str:
    try:
        target_path = path if os.path.isabs(path) else os.path.join(WORKSPACE_DIR, path)
        print(f"[Tool: write_file] {target_path} ({len(content)} chars)", flush=True)
        os.makedirs(os.path.dirname(os.path.abspath(target_path)), exist_ok=True)
        with open(target_path, "w", encoding="utf-8") as f:
            f.write(content)
        return f"[Successfully written {len(content)} characters to {target_path}]"
    except Exception as e:
        return f"[Error writing file: {e}]"

async def tool_web_search(query: str) -> str:
    try:
        print(f"[Tool: web_search] {query}", flush=True)

        def _search():
            # ponytail: to_thread cannot be interrupted, so the DDGS timeout is
            # the real bound here. On cancellation the worker thread may outlive
            # the run briefly; it holds no run state. Upgrade path is a
            # cancellable HTTP client instead of the blocking DDGS call.
            with DDGS(timeout=int(CONFIG.tool_stage_timeout)) as ddgs:
                return list(ddgs.text(query, max_results=5))

        results = await asyncio.wait_for(
            asyncio.to_thread(_search), timeout=CONFIG.tool_stage_timeout
        )
        if not results:
            return (
                "검색 결과가 없습니다.\n\n"
                "[💡 자율 탐색 힌트: 검색어를 더 일반적인 키워드로 바꾸거나, "
                "bash_exec를 사용해 curl/python3로 관련 포털이나 타겟 사이트를 직접 조회해 보세요.]"
            )
        formatted = []
        for i, r in enumerate(results, 1):
            formatted.append(f"{i}. [{r.get('title')}]({r.get('href')})\n   {r.get('body')}")
        return "\n\n".join(formatted)
    except Exception as e:
        return f"[Error performing web search: {e}]"

async def tool_record_state(ledger, updates) -> str:
    if ledger is None:
        return "[Error: 이 실행에는 상태 원장이 연결되어 있지 않습니다]"
    if isinstance(updates, str):
        try:
            updates = json.loads(updates)
        except Exception:
            return "[Error: record_state 인자를 JSON 객체로 해석할 수 없습니다]"
    print("[Tool: record_state]", flush=True)
    try:
        return ledger.apply_updates(updates)
    except Exception as e:
        return f"[Error applying state update: {e}]"

async def tool_finish_task(report: str) -> str:
    print(f"[Tool: finish_task Called!]", flush=True)
    return f"[Task Completed Successfully. Final Report Registered ({len(report)} chars)]"

async def execute_tools_in_parallel(tool_calls: list, step_num: int = 1, ledger=None, token=None) -> list:
    async def _exec_single(tc):
        name = tc["name"]
        args = tc["arguments"]
        if name == "bash_exec":
            cmd = args.get("command", "")
            return await tool_bash_exec(cmd)
        elif name == "read_file":
            path = args.get("path", "")
            return await tool_read_file(path)
        elif name == "write_file":
            path = args.get("path", "")
            content = args.get("content", "")
            return await tool_write_file(path, content)
        elif name == "web_search":
            q = args.get("query", "")
            return await tool_web_search(q)
        elif name == "record_state":
            return await tool_record_state(ledger, args)
        elif name == "finish_task":
            r = args.get("report", "")
            return await tool_finish_task(r)
        else:
            return f"[Error: Unknown tool function '{name}']"

    if token is not None:
        token.raise_if_cancelled()
    tasks = [asyncio.ensure_future(_exec_single(tc)) for tc in tool_calls]
    try:
        return await with_deadline(
            asyncio.gather(*tasks), CONFIG.tool_stage_timeout, token, "tool"
        )
    except BaseException:
        # A completed gather propagates one child failure without cancelling
        # pending siblings. Reap the tasks this layer owns on every exit.
        await _reap(*tasks)
        raise

def extract_tool_calls_from_text(text: str) -> list:
    extracted = []
    xml_matches = re.finditer(r"<tool_call>(.*?)</tool_call>", text, re.DOTALL)
    for m in xml_matches:
        raw_json = m.group(1).strip()
        try:
            parsed = json.loads(raw_json)
            extracted.append({
                "name": parsed.get("name"),
                "arguments": parsed.get("arguments", {})
            })
        except Exception:
            pass

    if not extracted:
        func_matches = re.finditer(r"<function=([a-zA-Z0-9_-]+)>\s*(.*?)\s*</function>", text, re.DOTALL)
        for fm in func_matches:
            fname = fm.group(1).strip()
            inner = fm.group(2).strip()
            args_dict = {}
            param_matches = re.finditer(r"<parameter=([a-zA-Z0-9_-]+)>\s*(.*?)\s*</parameter>", inner, re.DOTALL)
            for pm in param_matches:
                pname = pm.group(1).strip()
                pval = pm.group(2).strip()
                try:
                    args_dict[pname] = json.loads(pval)
                except Exception:
                    args_dict[pname] = pval
            if fname:
                extracted.append({"name": fname, "arguments": args_dict})

    return extracted

# --- Helper Functions for Message Roles and Serialization ---

def _msg_role(m) -> str:
    if isinstance(m, dict):
        return m.get("role", "")
    return getattr(m, "role", "") or ""

def _msg_content(m) -> str:
    # `content` is explicitly set to None for assistant tool-call messages, so
    # `.get("content", "")` would still hand back None. Every caller here wants
    # a string.
    if isinstance(m, dict):
        return m.get("content") or ""
    return getattr(m, "content", "") or ""

def _msg_name(m) -> str:
    if isinstance(m, dict):
        return m.get("name", "tool")
    return getattr(m, "name", "tool") or "tool"

def _msg_tool_calls(m) -> list:
    if isinstance(m, dict):
        return m.get("tool_calls") or []
    return getattr(m, "tool_calls", None) or []

def _clip_summary_text(text: str, max_chars: int) -> str:
    text = str(text or "").strip()
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + " ...[생략]"

def _tool_call_summary(call) -> str:
    if isinstance(call, dict):
        function = call.get("function") or {}
        name = function.get("name", "tool")
        arguments = function.get("arguments", "")
    else:
        function = getattr(call, "function", None)
        name = getattr(function, "name", "tool") if function else "tool"
        arguments = getattr(function, "arguments", "") if function else ""
    return f"{name}({_clip_summary_text(arguments, 700)})"

# --- Chat Template Sanitizer (400 Error Immunity) ---

def sanitize_messages_for_chat_template(messages: list) -> list:
    """Qwen chat template의 'System message must be at the beginning' 에러를 100% 원천 차단"""
    if not messages:
        return messages

    sanitized = []
    first_system_found = False

    for msg in messages:
        role = _msg_role(msg)
        content = _msg_content(msg)

        if role == "system":
            if not first_system_found and len(sanitized) == 0:
                sanitized.append(msg)
                first_system_found = True
            else:
                # 0번째가 아닌 위치의 system 메시지는 user 역할로 안전하게 전환
                sanitized.append({
                    "role": "user",
                    "content": f"[시스템 참고 정보]: {content}"
                })
        else:
            sanitized.append(msg)

    return sanitized

# --- User's Rolling Compaction (Rollup) Architecture ---

ROLLING_SUMMARY_LABEL = "누적 작업 요약 및 이전 대화 컨텍스트"
STATE_UPDATE_BLOCK_PATTERN = re.compile(r"```state_update\s*(.*?)```", re.DOTALL)


def build_system_content(base_prompt: str, ledger=None, summary: str = "") -> str:
    """Compose message 0.

    The state block goes last and message 0 sits before the first tool
    message, so `apply_micro_compaction` never rewrites it and every request,
    checkpoint and rollover sees the same authoritative state.
    """
    parts = [base_prompt]
    summary = str(summary or "").strip()
    if summary:
        parts.append(f"[{ROLLING_SUMMARY_LABEL}]\n{summary}")
    state_block = ledger.render() if ledger is not None else ""
    if state_block:
        parts.append(state_block)
    return "\n\n".join(parts)


def parse_state_update_blocks(text: str):
    """Split ```state_update JSON blocks out of a report.

    Returns (update payloads, report text with the blocks removed) so a
    checkpoint correction reaches the ledger instead of only Discord.
    """
    updates = []

    def _collect(match):
        try:
            parsed = json.loads(match.group(1).strip())
        except Exception:
            return ""
        if isinstance(parsed, dict):
            updates.append(parsed)
        elif isinstance(parsed, list):
            updates.extend(item for item in parsed if isinstance(item, dict))
        return ""

    cleaned = STATE_UPDATE_BLOCK_PATTERN.sub(_collect, str(text or ""))
    return updates, cleaned.strip()


def missing_state_markers(text: str, markers) -> list:
    text = str(text or "")
    return [marker for marker in markers or [] if marker not in text]


def build_rollup_source(messages: list, max_chars: int = ROLLING_SUMMARY_SOURCE_MAX_CHARS) -> str:
    """Create a bounded, chronological source for the LLM-generated rollup.

    The budget is spent newest-first and the blocks are re-ordered afterwards.
    Oldest material is already covered by the cumulative summary, so filling
    oldest-first would drop the newest refutation - the one thing the summary
    does not yet know about.
    """
    blocks = []
    used = 0

    for msg in reversed(messages):
        role = _msg_role(msg)
        if role == "system":
            continue

        if role == "assistant" and _msg_tool_calls(msg):
            calls = ", ".join(_tool_call_summary(call) for call in _msg_tool_calls(msg))
            block = f"[assistant 도구 지시]\n{calls}\n{_clip_summary_text(_msg_content(msg), 500)}"
        elif role == "tool":
            block = f"[tool: {_msg_name(msg)}]\n{_clip_summary_text(_msg_content(msg), 1400)}"
        elif role == "user":
            block = f"[user]\n{_clip_summary_text(_msg_content(msg), 1200)}"
        else:
            block = f"[{role}]\n{_clip_summary_text(_msg_content(msg), 800)}"

        if not block.strip():
            continue
        remaining = max_chars - used
        if remaining <= 0:
            break
        if len(block) > remaining:
            block = _clip_summary_text(block, remaining)
        blocks.append(block)
        used += len(block) + 2

    blocks.reverse()
    return "\n\n".join(blocks)

def split_recent_agent_context(messages: list, keep_recent_tool_messages: int = None):
    """Split at a complete assistant-tool group so tool_call_ids stay valid."""
    if keep_recent_tool_messages is None:
        keep_recent_tool_messages = KEEP_RECENT_TOOL_MESSAGES
    tool_indices = [i for i, m in enumerate(messages) if _msg_role(m) == "tool"]
    if len(tool_indices) <= keep_recent_tool_messages:
        return messages, []

    boundary = tool_indices[-keep_recent_tool_messages]
    for i in range(boundary - 1, -1, -1):
        if _msg_role(messages[i]) == "assistant" and _msg_tool_calls(messages[i]):
            boundary = i
            break

    return messages[:boundary], messages[boundary:]

async def rollover_agent_context(messages: list, existing_summary: str, step_num: int, session_file: str = "", ledger=None, token=None):
    """Summarize old steps and replace the live payload with a bounded tail."""
    if token is not None:
        token.raise_if_cancelled()
    old_messages, recent_messages = split_recent_agent_context(messages)
    if not recent_messages:
        return messages, existing_summary

    source = build_rollup_source(old_messages)
    if not source.strip() or source.strip() in (existing_summary or ""):
        # Nothing new to fold in. Asking the model to rewrite the summary from
        # material it already covers is how a summary silently reverts to an
        # older state, so record a no-op instead.
        if session_file:
            log_session_event(
                session_file,
                f"⏭️ [Step {step_num} 롤링 압축 no-op]",
                "압축할 새 실행 기록이 없어 요약을 갱신하지 않고 건너뜁니다.",
            )
        print(f"[Rolling Compaction Skipped at Step {step_num}] no new source content", flush=True)
        return messages, existing_summary

    state_block = ledger.render() if ledger is not None else ""
    required_markers = ledger.state_markers() if ledger is not None else []
    marker_hint = ""
    if required_markers:
        marker_hint = (
            "다음 상태 마커는 요약에 반드시 문자 그대로 남기고, 상태를 과거로 되돌리지 마세요: "
            + ", ".join(required_markers)
            + "\n"
        )

    summary_prompt = (
        "이것은 장시간 자율 에이전트의 컨텍스트 압축 작업입니다.\n"
        "이전 원본 대화를 그대로 반복하지 말고, 다음 에이전트가 작업을 이어갈 수 있는 "
        "정확한 누적 요약만 작성하세요.\n"
        "반드시 포함할 것: 원래 사용자 목표, 완료한 작업, 확인된 사실/수치/URL/파일 경로, "
        "실패와 원인, 반증된 가설과 그 근거, 무효가 된 결론, 아직 검증하지 않은 가정, "
        "다음에 해야 할 구체적인 작업.\n"
        f"{marker_hint}"
        "도구 결과가 불확실하면 추측하지 말고 불확실하다고 표시하세요. "
        "한국어 마크다운으로 10,000자 이내로 작성하고 요약 외의 인사말은 쓰지 마세요.\n\n"
        + (f"{state_block}\n\n" if state_block else "")
        + f"[기존 누적 요약]\n{_clip_summary_text(existing_summary, ROLLING_SUMMARY_MAX_CHARS)}\n\n"
        + f"[이번 구간의 원본 실행 기록]\n{source}"
    )

    new_summary = ""
    validation_notes_prefix = ""
    try:
        summary_resp = await run_completion_stage(
            token=token,
            stage="rollover",
            model=MODEL_NAME,
            messages=[
                {"role": "system", "content": "당신은 자율 에이전트의 정확한 컨텍스트 압축기입니다."},
                {"role": "user", "content": summary_prompt},
            ],
            max_tokens=1536,
            temperature=0.2,
            reasoning_effort="none",
        )
        new_summary = (summary_resp.choices[0].message.content or "").strip()
        new_summary = re.sub(r"<think>.*?</think>", "", new_summary, flags=re.DOTALL).strip()
    except RunCancelled:
        # Cancellation must not silently degrade into a fallback summary.
        raise
    except StageTimeout as compaction_timeout:
        # Rollover is an optimization: the original source is still available,
        # so a bounded timeout can safely use deterministic local compaction.
        validation_notes_prefix = str(compaction_timeout)
        print(f"[Rolling Compaction Timeout at Step {step_num}]: {compaction_timeout}", flush=True)
    except Exception as compaction_error:
        print(f"[Rolling Compaction Summary Error at Step {step_num}]: {compaction_error}", flush=True)

    validation_notes = [validation_notes_prefix] if validation_notes_prefix else []
    if new_summary and existing_summary and new_summary == existing_summary.strip():
        # The source had new content but the compactor echoed the old summary.
        # Accepting it would freeze the state at its previous revision.
        validation_notes.append("압축기가 기존 요약을 그대로 반환하여 거부했습니다.")
        new_summary = ""

    if not new_summary:
        new_summary = _clip_summary_text(
            "\n".join(part for part in [existing_summary, source] if part),
            ROLLING_SUMMARY_MAX_CHARS,
        )
        validation_notes.append("결정적 폴백 요약(기존 요약 + 원본 기록)을 사용했습니다.")
    else:
        new_summary = _clip_summary_text(new_summary, ROLLING_SUMMARY_MAX_CHARS)

    dropped_markers = missing_state_markers(new_summary, required_markers)
    if dropped_markers:
        validation_notes.append("누락된 상태 마커를 권위 있는 상태 블록으로 보정했습니다: " + ", ".join(dropped_markers))
        new_summary = f"{state_block}\n\n{new_summary}".strip()

    # 400 Chat template error 완벽 방지: 단일 시스템 프롬프트로 병합
    replaced_messages = [
        {"role": "system", "content": build_system_content(SYSTEM_PROMPT, ledger, new_summary)},
        {
            "role": "user",
            "content": "[롤링 컨텍스트 재개] 위 요약과 권위 있는 조사 상태를 기준으로 최근 도구 실행 결과를 반영하고 다음 작업을 계속하세요.",
        },
    ]
    replaced_messages.extend(recent_messages)
    replaced_messages = sanitize_messages_for_chat_template(replaced_messages)

    before_chars = sum(len(_msg_content(msg)) for msg in messages)
    after_chars = sum(len(_msg_content(msg)) for msg in replaced_messages)
    compaction_record = (
        f"메시지: {len(messages)} -> {len(replaced_messages)}\n"
        f"내용 문자: {before_chars} -> {after_chars}\n"
        f"최근 도구 메시지 보존: {KEEP_RECENT_TOOL_MESSAGES}\n"
        f"요약 검증: {'; '.join(validation_notes) if validation_notes else '통과'}\n\n"
        f"{new_summary}"
    )
    if session_file:
        log_session_event(session_file, f"🧹 [Step {step_num} 롤링 컨텍스트 교체]", compaction_record)
    print(
        f"[Rolling Compaction at Step {step_num}] messages {len(messages)}->{len(replaced_messages)}, "
        f"chars {before_chars}->{after_chars}"
        + (f", validation: {'; '.join(validation_notes)}" if validation_notes else ""),
        flush=True,
    )
    return replaced_messages, new_summary

# --- User's Streaming Completion Collector ---

async def run_completion_stage(token=None, stage="agent", deadline=None, **kwargs):
    """Run any completion implementation under the stage's total budget.

    This wrapper deliberately sits at the call-site boundary, outside the
    streaming implementation. Alternative backends and test doubles therefore
    cannot accidentally bypass cancellation or the total deadline. A supplied
    monotonic deadline lets recovery attempts share one stage budget.
    """
    from openai import APITimeoutError

    seconds = (
        CONFIG.model_stage_timeout
        if deadline is None
        else max(0.0, deadline - time.monotonic())
    )
    try:
        return await with_deadline(
            create_streaming_completion(token=token, stage=stage, **kwargs),
            seconds,
            token,
            stage,
        )
    except (APITimeoutError, httpx.TimeoutException) as timeout_error:
        phase_error = getattr(timeout_error, "__cause__", None)
        if not isinstance(phase_error, httpx.TimeoutException):
            phase_error = timeout_error
        if isinstance(phase_error, httpx.ConnectTimeout):
            phase, phase_seconds = "connect", CONFIG.connect_timeout
        elif isinstance(phase_error, httpx.ReadTimeout):
            phase, phase_seconds = "read", CONFIG.idle_timeout
        else:
            phase, phase_seconds = "transport", seconds
        raise StageTimeout(f"{stage}:{phase}", phase_seconds) from timeout_error


async def create_streaming_completion(token=None, stage="agent", **kwargs):
    """Collect a streaming response under transport connect and read-idle bounds."""
    budget = StageBudget(stage, total=CONFIG.model_stage_timeout, idle=CONFIG.idle_timeout)
    # HTTPX's phase-specific connect timeout governs DNS/TCP/TLS. Awaiting the
    # SDK here also includes response headers, which belong to read/total time.
    stream = await client.chat.completions.create(stream=True, **kwargs)
    content_parts = []
    reasoning_parts = []
    tool_buffers = {}

    async for chunk in stream_chunks(stream, budget, token):
        choices = getattr(chunk, "choices", None) or []
        if not choices:
            continue
        delta = getattr(choices[0], "delta", None)
        if delta is None:
            continue

        content = getattr(delta, "content", None)
        if content:
            content_parts.append(content if isinstance(content, str) else str(content))

        reasoning = (
            getattr(delta, "reasoning_content", None)
            or getattr(delta, "reasoning", None)
            or ""
        )
        if reasoning:
            reasoning_parts.append(reasoning if isinstance(reasoning, str) else str(reasoning))

        for partial in getattr(delta, "tool_calls", None) or []:
            index = getattr(partial, "index", None)
            if index is None:
                index = len(tool_buffers)
            buf = tool_buffers.setdefault(
                index, {"id": "", "name": "", "arguments": ""}
            )
            call_id = getattr(partial, "id", None)
            if call_id:
                buf["id"] = call_id
            function = getattr(partial, "function", None)
            if function is not None:
                name = getattr(function, "name", None)
                arguments = getattr(function, "arguments", None)
                if name:
                    buf["name"] += name
                if arguments:
                    buf["arguments"] += (
                        arguments if isinstance(arguments, str) else str(arguments)
                    )

    tool_calls = []
    for index in sorted(tool_buffers):
        buf = tool_buffers[index]
        tool_calls.append(
            SimpleNamespace(
                id=buf["id"] or f"call_stream_{index}",
                function=SimpleNamespace(
                    name=buf["name"], arguments=buf["arguments"]
                ),
            )
        )

    message = SimpleNamespace(
        content="".join(content_parts),
        reasoning_content="".join(reasoning_parts),
        reasoning="".join(reasoning_parts),
        tool_calls=tool_calls,
    )
    return SimpleNamespace(choices=[SimpleNamespace(message=message)])

# --- User's Micro Compaction Algorithm ---

def apply_micro_compaction(messages: list, preserve_recent_tool_steps: int = 2) -> list:
    tool_msg_indices = [i for i, m in enumerate(messages) if _msg_role(m) == "tool"]
    preserve_recent_tool_steps = max(0, preserve_recent_tool_steps)
    if len(tool_msg_indices) <= preserve_recent_tool_steps:
        return messages

    first_tool_index = tool_msg_indices[0]
    compact_before = (
        tool_msg_indices[-preserve_recent_tool_steps]
        if preserve_recent_tool_steps
        else len(messages)
    )
    compressed_messages = []

    for i, msg in enumerate(messages):
        if i < first_tool_index or i >= compact_before:
            compressed_messages.append(msg)
            continue

        role = _msg_role(msg)
        if role == "tool":
            compacted = dict(msg)
            content = _msg_content(msg)
            first_line = content.split("\n")[0][:160].strip()
            compacted["content"] = (
                f"[{_msg_name(msg)} 실행 결과 생략: {first_line}]"
                if first_line
                else f"[{_msg_name(msg)} 실행 결과 생략]"
            )
            compressed_messages.append(compacted)
        elif role == "assistant":
            compacted = dict(msg)
            if compacted.get("tool_calls"):
                compacted["content"] = None
                compacted_calls = []
                for call in compacted["tool_calls"]:
                    call_copy = dict(call)
                    function = dict(call_copy.get("function") or {})
                    function["arguments"] = "{}"
                    call_copy["function"] = function
                    compacted_calls.append(call_copy)
                compacted["tool_calls"] = compacted_calls
            else:
                compacted["content"] = "[이전 단계의 추론 내용 생략]"
            compressed_messages.append(compacted)
        elif role == "user":
            compacted = dict(msg)
            content = _msg_content(msg)
            if content.startswith("[🤖") or "중간 진행" in content:
                compacted["content"] = "[이전 자동 진행 지시 생략]"
            else:
                compacted["content"] = content[:400]
            compressed_messages.append(compacted)
        else:
            compressed_messages.append(msg)

    return compressed_messages

DISCORD_CHUNK_MAX_CHARS = 1900
LOCAL_FALLBACK_MAX_CHUNKS = 3
LOCAL_FALLBACK_MAX_CHARS = DISCORD_CHUNK_MAX_CHARS * LOCAL_FALLBACK_MAX_CHUNKS
LOCAL_FALLBACK_OMISSION_MARKER = (
    "\n\n[중간 상세 내용 생략 — 전체 원장은 내부 상태에 유지됨]\n\n"
)


def bound_local_fallback_output(text: str) -> str:
    """Bound user-facing fallback without mutating its authoritative sources."""
    text = str(text or "")
    if len(text) <= LOCAL_FALLBACK_MAX_CHARS:
        return text
    available = LOCAL_FALLBACK_MAX_CHARS - len(LOCAL_FALLBACK_OMISSION_MARKER)
    head_chars = available // 2
    tail_chars = available - head_chars
    return (
        text[:head_chars]
        + LOCAL_FALLBACK_OMISSION_MARKER
        + text[-tail_chars:]
    )


def build_incomplete_report(outcome, ledger, rolling_summary: str, messages_payload: list) -> str:
    """Render collected state without starting another model stage.

    Cancellation and model-timeout paths cannot safely ask the same backend to
    synthesize a report: that would start a follow-up stage after stop and can
    repeat the very hang that ended the run. Keep this deterministic and bounded.
    """
    state = ledger.render() if ledger is not None else ""
    tail = build_rollup_source(messages_payload, max_chars=6000)
    if outcome.is_completed:
        closing_section = (
            "## 결과 안내\n조사는 완료되었습니다. "
            "위 내용은 완료 시점에 보존된 조사 상태와 실행 기록입니다."
        )
    else:
        closing_section = (
            "## 다음 단계\n이 보고서는 완료된 조사 결과가 아닙니다. "
            "미해결 항목을 확인한 뒤 새 요청으로 이어서 진행하세요."
        )
    sections = [
        "## 조사 종료 상태\n- {0}\n- 사유: `{1}`".format(outcome.label, outcome.describe()),
        "## 권위 있는 조사 상태\n" + (state or "기록된 구조화 상태가 없습니다."),
        "## 누적 작업 요약\n" + (rolling_summary.strip() if rolling_summary else "누적 요약이 없습니다."),
        "## 최근 실행 기록\n" + (tail or "보존된 실행 기록이 없습니다."),
        closing_section,
    ]
    return "\n\n".join(sections)


def format_full_discord_output(text: str) -> str:
    if not text:
        return ""

    t = re.sub(r"<tool_call>.*?</tool_call>", "", text, flags=re.DOTALL).strip()
    t = re.sub(r"<tool_call>.*", "", t, flags=re.DOTALL).strip()
    t = re.sub(r"<function=[^>]*>.*?</function>", "", t, flags=re.DOTALL).strip()
    t = re.sub(r"<parameter=[^>]*>.*?</parameter>", "", t, flags=re.DOTALL).strip()
    t = re.sub(r"</?(function|parameter|tool_call)[^>]*>", "", t, flags=re.DOTALL).strip()

    think_match = re.search(r"<think>(.*?)(?:</think>|$)", t, flags=re.DOTALL)
    if think_match:
        think_content = think_match.group(1).strip()
        regular_content = t.split("</think>")[-1].strip() if "</think>" in t else ""
        regular_content = re.sub(r"</?(function|parameter|tool_call)[^>]*>", "", regular_content).strip()

        if think_content and regular_content:
            return f"### 💭 **[심층 추론 과정 (Thinking - High)]**\n```text\n{think_content}\n```\n\n### 📝 **[최종 답변]**\n{regular_content}"
        elif think_content and not regular_content:
            return f"### 💭 **[심층 추론 및 분석 내용 (Thinking - High)]**\n```text\n{think_content}\n```"
        elif regular_content:
            return regular_content

    return t.strip()

@bot.event
async def on_ready():
    print(f"[Discord LLM Bot] Logged in as {bot.user} (ID: {bot.user.id})", flush=True)
    print(f"[Discord LLM Bot] Ready in Auto-Extension Goal-Driven Mode! Workspace: {WORKSPACE_DIR}", flush=True)
    await bot.change_presence(activity=discord.Game(name="Qwen 27B + Auto-Extension"), status=discord.Status.online)

# --- Slash Commands ---
# 슬래시 명령도 텍스트 명령과 동일한 정책 경로(authorize_caller)를 사용한다.

async def deny_interaction(interaction: discord.Interaction, action: str, decision) -> None:
    print(
        f"[Access Denied] user={getattr(interaction.user, 'id', None)} "
        f"channel={interaction.channel_id} action={action} reason={decision.reason}",
        flush=True,
    )
    message = decision.reason if action != authz.ACCESS else authz.DENY_ACCESS_MESSAGE
    await interaction.response.send_message(f"⛔ {message}", ephemeral=True)


def interaction_can_manage_messages(interaction: discord.Interaction) -> bool:
    permissions = getattr(interaction, "permissions", None)
    if permissions is not None and hasattr(permissions, "manage_messages"):
        return bool(permissions.manage_messages)
    return caller_can_manage_messages(interaction.channel, interaction.user)


@bot.tree.command(name="reset", description="대화 기록 및 캐시를 초기화합니다.")
async def slash_reset(interaction: discord.Interaction):
    decision = authorize_caller(
        authz.CONTROL, getattr(interaction.user, "id", None), channel_id=interaction.channel_id
    )
    if not decision:
        await deny_interaction(interaction, authz.CONTROL, decision)
        return
    clear_channel_state(interaction.channel_id)
    await interaction.response.send_message("🧹 **대화 기록과 컨텍스트 캐시가 초기화되었습니다.** 새로운 목표를 입력하세요!")

@bot.tree.command(name="stop", description="현재 단계를 취소하고 보존된 조사 상태를 보고합니다.")
async def slash_stop(interaction: discord.Interaction):
    decision = authorize_caller(
        authz.CONTROL, getattr(interaction.user, "id", None), channel_id=interaction.channel_id
    )
    if not decision:
        await deny_interaction(interaction, authz.CONTROL, decision)
        return
    if not request_run_cancel(interaction.channel_id):
        await interaction.response.send_message("진행 중인 자율 탐색이 없습니다.", ephemeral=True)
        return
    await interaction.response.send_message("🛑 **중단 요청을 수신했습니다.** 진행 중인 단계를 취소하고 수집된 데이터로 보고서를 작성합니다.", ephemeral=True)

@bot.tree.command(name="clear", description="입력한 개수만큼 최근 메시지와 대화 기록을 한 번에 삭제합니다.")
async def slash_clear(interaction: discord.Interaction, count: int = 50):
    decision = authorize_caller(
        authz.PURGE,
        getattr(interaction.user, "id", None),
        channel_id=interaction.channel_id,
        caller_can_manage_messages=interaction_can_manage_messages(interaction),
    )
    if not decision:
        await deny_interaction(interaction, authz.PURGE, decision)
        return

    amt = min(max(1, count), 100)
    await interaction.response.defer(ephemeral=True)
    # 삭제 성공 이후에만 대화 상태를 지운다.
    try:
        deleted = await interaction.channel.purge(limit=amt)
    except discord.Forbidden:
        await interaction.followup.send("⚠️ **권한 부족**: 봇에게 채널의 **'메시지 관리 (Manage Messages)'** 권한이 필요합니다. 대화 기록은 유지되었습니다.", ephemeral=True)
        return
    except Exception as e:
        await interaction.followup.send(f"⚠️ 메시지 삭제 중 오류 발생: `{e}` 대화 기록은 유지되었습니다.", ephemeral=True)
        return

    clear_channel_state(interaction.channel_id)
    await interaction.followup.send(f"🧹 **최근 메시지 {len(deleted)}개와 봇 대화 기록을 모두 삭제했습니다.**", ephemeral=True)

@bot.tree.command(name="reasoning", description="추론 레벨을 변경합니다 (none, low, medium, high)")
async def slash_reasoning(interaction: discord.Interaction, level: str):
    decision = authorize_caller(
        authz.ACCESS, getattr(interaction.user, "id", None), channel_id=interaction.channel_id
    )
    if not decision:
        await deny_interaction(interaction, authz.ACCESS, decision)
        return
    lvl = level.lower().strip()
    if lvl not in ["none", "low", "medium", "high"]:
        await interaction.response.send_message("⚠️ 유효한 레벨은 `none`, `low`, `medium`, `high` 입니다.", ephemeral=True)
        return
    channel_reasoning[interaction.channel_id] = lvl
    await interaction.response.send_message(f"🧠 추론 레벨이 **`{lvl}`**(으)로 설정되었습니다.")

# --- Message Event Handler ---

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    content = message.content.strip()
    if bot.user:
        content = content.replace(f"<@{bot.user.id}>", "").replace(f"<@!{bot.user.id}>", "").strip()

    if not content:
        return

    # [라우팅 판정] DM·멘션·허용 채널은 "어디서 왔는가"만 말한다. 신원이 아니다.
    is_dm = isinstance(message.channel, discord.DMChannel)
    is_free_channel = (message.channel.id in FREE_RESPONSE_CHANNEL_IDS)
    is_mentioned = bot.user in message.mentions

    if not (is_dm or is_free_channel or is_mentioned):
        return

    # [신원 게이트] 로그 기록·상태 변경·큐 적재·purge·모델 호출·도구 실행보다 먼저.
    # 제어 명령도 이 뒤에서만 처리된다.
    caller_id = getattr(message.author, "id", None)
    access = authorize_caller(authz.ACCESS, caller_id, channel_id=message.channel.id)
    if not access:
        print(
            f"[Access Denied] user={caller_id} channel={message.channel.id} action=access "
            f"reason={access.reason}",
            flush=True,
        )
        await message.reply(authz.DENY_ACCESS_MESSAGE)
        return

    parts = content.split()
    cmd_name = parts[0].lower()

    if cmd_name in ["!stop", "!중단", "!멈춰", "/stop"]:
        control = authorize_caller(authz.CONTROL, caller_id, channel_id=message.channel.id)
        if not control:
            await message.reply(f"⛔ {control.reason}")
            return
        if not request_run_cancel(message.channel.id):
            await message.reply("진행 중인 자율 탐색이 없습니다.")
            return
        await message.reply("🛑 **중단 요청을 수신했습니다.** 진행 중인 단계를 취소하고 수집된 데이터로 보고서를 작성합니다.")
        return

    if cmd_name in ["!reset", "!new", "!리셋", "!초기화", "/reset", "/new"]:
        control = authorize_caller(authz.CONTROL, caller_id, channel_id=message.channel.id)
        if not control:
            await message.reply(f"⛔ {control.reason}")
            return
        clear_channel_state(message.channel.id)
        await message.reply("🧹 **대화 기록과 캐시가 초기화되었습니다.** 새로운 목표를 입력하세요!")
        return

    if cmd_name in ["!clear", "!purge", "!삭제", "!청소"]:
        purge = authorize_caller(
            authz.PURGE,
            caller_id,
            channel_id=message.channel.id,
            caller_can_manage_messages=caller_can_manage_messages(message.channel, message.author),
        )
        if not purge:
            print(
                f"[Purge Denied] user={caller_id} channel={message.channel.id} reason={purge.reason}",
                flush=True,
            )
            await message.reply(f"⛔ {purge.reason}")
            return

        amount = 50
        if len(parts) > 1 and parts[1].isdigit():
            amount = int(parts[1])
        amount = min(max(1, amount), 100)

        print(f"[Clear Command]: Purging {amount} messages in channel {message.channel.id}", flush=True)

        # 삭제를 먼저 시도하고, 성공한 뒤에만 대화 상태를 지운다. 예전에는 순서가
        # 반대여서 권한이 없어 삭제가 실패해도 기록은 이미 사라져 있었다.
        try:
            deleted = await message.channel.purge(limit=amount + 1)
        except discord.Forbidden:
            await message.channel.send("⚠️ **권한 부족**: 봇에게 서버의 **'메시지 관리 (Manage Messages)'** 권한이 필요합니다. 대화 기록은 유지되었습니다.")
            return
        except Exception as e:
            await message.channel.send(f"⚠️ 메시지 삭제 실패: `{e}` 대화 기록은 유지되었습니다.")
            return

        clear_channel_state(message.channel.id)
        del_count = max(0, len(deleted) - 1)
        notice = await message.channel.send(f"🧹 **최근 메시지 {del_count}개와 봇 대화 기록을 모두 삭제했습니다.**")
        await asyncio.sleep(3)
        try:
            await notice.delete()
        except Exception:
            pass
        return

    if content.startswith("!"):
        return

    today_str = datetime.now().strftime("%Y-%m-%d")
    session_file = os.path.join(SYSTEM_LOG_DIR, f"{today_str}_channel_{message.channel.id}.md")

    # [실시간 동적 개입 큐 주입]
    if channel_active_runs[message.channel.id]:
        steering = authorize_caller(authz.CONTROL, caller_id, channel_id=message.channel.id)
        if not steering:
            await message.reply(f"⛔ {steering.reason}")
            return
        channel_user_queue[message.channel.id].append((str(message.author), content))
        log_session_event(session_file, f"💬 [사용자 실시간 중간 개입] {message.author}", content)
        print(f"[Mid-Flight User Steering Queued from {message.author}]: {content}", flush=True)

        try:
            await message.add_reaction("📥")
        except Exception:
            pass

        notice = await message.reply(f"📥 **[실시간 개입 수신]** 실행 중인 에이전트의 다음 스텝에 지시사항이 즉시 반영됩니다:\n> 💬 *\"{content[:120]}\"*")
        asyncio.create_task(_auto_delete_notice(notice, delay=6))
        return

    start_time = time.time()
    token = CancelToken()
    lease = {"token": token, "owner": caller_id, "active": False}
    channel_run_leases[message.channel.id].append(lease)

    def publish_run_control():
        leases = channel_run_leases.get(message.channel.id, [])
        channel_active_runs[message.channel.id] = any(
            candidate["active"] for candidate in leases
        )
        if not leases:
            channel_run_leases.pop(message.channel.id, None)
            channel_cancel_token.pop(message.channel.id, None)
            channel_run_owner.pop(message.channel.id, None)
            channel_user_queue[message.channel.id].clear()
            return
        current = next(
            (
                candidate
                for candidate in reversed(leases)
                if candidate["active"]
            ),
            leases[-1],
        )
        channel_cancel_token[message.channel.id] = current["token"]
        channel_run_owner[message.channel.id] = current["owner"]

    publish_run_control()
    channel_user_queue[message.channel.id].clear()

    def release_run():
        leases = channel_run_leases.get(message.channel.id, [])
        if not any(candidate is lease for candidate in leases):
            return
        leases[:] = [candidate for candidate in leases if candidate is not lease]
        publish_run_control()

    async def send_reply_chunks(text, local_fallback=False):
        if local_fallback:
            text = bound_local_fallback_output(text)
        chunks = [
            text[i:i + DISCORD_CHUNK_MAX_CHARS]
            for i in range(0, len(text), DISCORD_CHUNK_MAX_CHARS)
        ]
        await message.reply(chunks[0])
        for chunk in chunks[1:]:
            await message.channel.send(chunk)
    
    log_session_event(session_file, f"👤 [사용자 목표 요청] {message.author}", content)
    print(f"[Goal Request from {message.author}]: {content}", flush=True)

    history = channel_history[message.channel.id]
    history.append({"role": "user", "content": content})

    if len(history) > MAX_RECENT_TURNS * 2:
        overflow_turns = history[:-MAX_RECENT_TURNS * 2]
        recent_turns = history[-MAX_RECENT_TURNS * 2:]
        summary_snippets = []
        for msg_item in overflow_turns:
            role_label = "사용자" if msg_item["role"] == "user" else "AI"
            snippet = msg_item["content"][:150].replace("\n", " ")
            summary_snippets.append(f"{role_label}: {snippet}")
        channel_summary[message.channel.id] = "이전 대화 요약: " + " | ".join(summary_snippets[-8:])
        channel_history[message.channel.id] = recent_turns
        history = recent_turns

    direct_call_failed = False
    if wants_direct_response(content):
        try:
            direct_resp = await run_completion_stage(
                token=token,
                stage="direct",
                model=MODEL_NAME,
                messages=[{"role": "system", "content": DIRECT_RESPONSE_PROMPT}, *history],
                max_tokens=512,
                temperature=0.3,
                reasoning_effort="none",
            )
            token.raise_if_cancelled()
            direct_text = direct_resp.choices[0].message.content or ""
            direct_text = clean_direct_response(direct_text)
        except RunCancelled as direct_cancelled:
            direct_outcome = RunOutcome()
            direct_outcome.settle(outcome_mod.STOPPED, direct_cancelled.reason)
            direct_report = build_incomplete_report(
                direct_outcome,
                channel_ledger[message.channel.id],
                channel_summary[message.channel.id],
                history,
            )
            try:
                await send_reply_chunks(
                    f"**{direct_outcome.label}**\n\n{direct_report}",
                    local_fallback=True,
                )
                log_session_event(session_file, f"🏁 [런 종료: {direct_outcome.describe()}]", direct_report)
            finally:
                release_run()
            return
        except StageTimeout as direct_timeout:
            direct_outcome = RunOutcome()
            direct_outcome.settle(
                outcome_mod.FAILED,
                f"마감 초과: {direct_timeout.stage} {direct_timeout.seconds:g}s",
            )
            direct_report = build_incomplete_report(
                direct_outcome,
                channel_ledger[message.channel.id],
                channel_summary[message.channel.id],
                history,
            )
            try:
                await send_reply_chunks(
                    f"**{direct_outcome.label}**\n\n{direct_report}",
                    local_fallback=True,
                )
                log_session_event(session_file, f"🏁 [런 종료: {direct_outcome.describe()}]", direct_report)
            finally:
                release_run()
            return
        except asyncio.CancelledError:
            release_run()
            raise
        except Exception as direct_error:
            direct_call_failed = True
            print(f"[Direct Reply Fallback]: {direct_error}", file=sys.stderr, flush=True)
        else:
            if direct_text:
                try:
                    await send_reply_chunks(direct_text)
                    history.append({"role": "assistant", "content": direct_text})
                    log_session_event(session_file, "💬 [간단 답변 완료]", direct_text)
                    print(f"[Direct Reply to {message.author} finished]: {len(direct_text)} chars", flush=True)
                finally:
                    release_run()
                return
            direct_call_failed = True

    # 400 Chat template error 방지: 0번째에 단일 시스템 프롬프트로 병합
    ledger = channel_ledger[message.channel.id]
    rolling_summary = channel_summary[message.channel.id]

    messages_payload = [
        {"role": "system", "content": build_system_content(SYSTEM_PROMPT, ledger, rolling_summary)}
    ]
    messages_payload.extend(history)
    messages_payload = sanitize_messages_for_chat_template(messages_payload)

    current_effort = channel_reasoning[message.channel.id]
    try:
        status_msg = await message.reply(f"🚀 **[완전자율 목표 달성 모드]** 모델 초기 추론 및 작업 공간 가동 중... (실시간 지시/개입 가능 / 중단: `!stop`)")
    except BaseException:
        release_run()
        raise
    lease["active"] = True
    publish_run_control()

    total_tools_executed = 0
    last_failed_signature = None
    consecutive_failed_tool_calls = 0

    async def maybe_roll_context(step_num: int):
        nonlocal messages_payload, rolling_summary
        if step_num % ROLLING_COMPACTION_INTERVAL != 0:
            return
        messages_payload, rolling_summary = await rollover_agent_context(
            messages_payload,
            rolling_summary,
            step_num,
            session_file=session_file,
            ledger=ledger,
            token=token,
        )

    # 상시 타이핑 하트비트 루프 백그라운드 구동
    stop_typing = asyncio.Event()
    typing_task = asyncio.create_task(keep_typing_heartbeat(message.channel, stop_typing))

    final_raw = ""
    outcome = RunOutcome()
    refused_companion_calls = []
    consecutive_no_tool_responses = 0

    def settle_stage_failure(error):
        if isinstance(error, RunCancelled):
            outcome.settle(outcome_mod.STOPPED, error.reason)
        elif isinstance(error, StageTimeout):
            outcome.settle(
                outcome_mod.FAILED,
                f"마감 초과: {error.stage} {error.seconds:g}s",
            )
        else:
            failure = _clip_summary_text(
                f"{type(error).__name__}: {error}", 500
            )
            outcome.settle(
                outcome_mod.FAILED,
                f"업스트림 실패: {failure}",
            )
        print(f"[Run settled: {outcome.describe()}]", flush=True)

    try:
        for iteration in range(MAX_AGENT_LOOPS):
            if token.cancelled:
                outcome.settle(outcome_mod.STOPPED, token.reason)
                print(f"[Run settled: {outcome.describe()} at Step {iteration+1}]", flush=True)
                break

            # [실시간 사용자 동적 개입 주입]
            if channel_user_queue[message.channel.id]:
                while channel_user_queue[message.channel.id]:
                    q_author, q_text = channel_user_queue[message.channel.id].pop(0)
                    steering_block = {
                        "role": "user",
                        "content": f"💬 [사용자({q_author}) 실시간 추가 지침/피드백]:\n{q_text}\n\n(이 지침을 바탕으로 현재 작업 방향을 적절히 조정하거나, 요청받은 내용을 우선 처리하세요.)"
                    }
                    messages_payload.append(steering_block)
                    history.append(steering_block)
                    log_session_event(session_file, f"💬 [Step {iteration+1} 실시간 사용자 지침 주입]", f"[{q_author}] {q_text}")
                    try:
                        await status_msg.edit(content=f"🛠️ **[Step {iteration+1}/{MAX_AGENT_LOOPS}]** 💬 **사용자 실시간 지시사항 반영 중...** (*\"{q_text[:60]}...\"*)")
                    except Exception:
                        pass

            extra_params = {}
            if iteration == 0:
                extra_params["reasoning_effort"] = "none"
            elif current_effort and current_effort != "none":
                extra_params["reasoning_effort"] = current_effort

            # 권위 있는 조사 상태를 매 스텝 0번 메시지에 재고정한다.
            # 0번은 첫 tool 메시지보다 앞이라 apply_micro_compaction이 건드리지 않는다.
            if messages_payload and _msg_role(messages_payload[0]) == "system":
                messages_payload[0] = {
                    "role": "system",
                    "content": build_system_content(SYSTEM_PROMPT, ledger, rolling_summary),
                }

            compacted_payload = sanitize_messages_for_chat_template(apply_micro_compaction(messages_payload, preserve_recent_tool_steps=2))
            model_stage_deadline = time.monotonic() + CONFIG.model_stage_timeout

            try:
                resp = await run_completion_stage(
                    token=token,
                    stage="agent",
                    deadline=model_stage_deadline,
                    model=MODEL_NAME,
                    messages=compacted_payload,
                    max_tokens=1024 if iteration == 0 else 4096,
                    temperature=0.7,
                    **agent_tool_params(),
                    **extra_params
                )
            except (RunCancelled, StageTimeout) as stage_error:
                settle_stage_failure(stage_error)
                break
            except Exception as api_err:
                err_str = str(api_err)
                if "tool_call_id" in err_str or "400" in err_str:
                    print(f"[API 400 Auto-Recovery Triggered]: {err_str}", flush=True)
                    sanitized_payload = []
                    for p_msg in compacted_payload:
                        if _msg_role(p_msg) == "tool":
                            sanitized_payload.append({
                                "role": "user",
                                "content": f"[도구 실행 결과: {_msg_name(p_msg)}]\n{_msg_content(p_msg)}"
                            })
                        elif _msg_role(p_msg) == "assistant" and isinstance(p_msg, dict) and "tool_calls" in p_msg:
                            sanitized_payload.append({
                                "role": "assistant",
                                "content": _msg_content(p_msg) or "[도구 실행 지시]"
                            })
                        else:
                            sanitized_payload.append(p_msg)

                    # Never restart a stage after cancellation.
                    try:
                        token.raise_if_cancelled()
                        resp = await run_completion_stage(
                            token=token,
                            stage="agent:retry",
                            deadline=model_stage_deadline,
                            model=MODEL_NAME,
                            messages=sanitize_messages_for_chat_template(sanitized_payload),
                            max_tokens=1024 if iteration == 0 else 4096,
                            temperature=0.7,
                            **agent_tool_params(),
                            **extra_params
                        )
                    except (RunCancelled, StageTimeout) as stage_error:
                        settle_stage_failure(stage_error)
                        break
                    except Exception as retry_error:
                        settle_stage_failure(retry_error)
                        break
                else:
                    settle_stage_failure(api_err)
                    break

            # A response completed concurrently with !stop must not enable
            # logging, counters, messages, or tool work after cancellation.
            try:
                token.raise_if_cancelled()
            except RunCancelled as stage_error:
                settle_stage_failure(stage_error)
                break
            choice = resp.choices[0]
            msg = choice.message

            # [Rapid-MLX / OpenAI Reasoning 필드 추출]
            reasoning_text = (getattr(msg, "reasoning_content", None) or getattr(msg, "reasoning", None) or "")
            content_text = msg.content or ""

            if reasoning_text and content_text:
                full_raw_thought = f"<think>\n{reasoning_text}\n</think>\n\n{content_text}".strip()
            elif reasoning_text:
                full_raw_thought = f"<think>\n{reasoning_text}\n</think>".strip()
            else:
                full_raw_thought = content_text.strip()

            log_session_event(session_file, f"🤖 [Step {iteration+1} 자율 탐색 내용 & Think]", full_raw_thought or "(도구 호출 지시)")

            # [실시간 추론 생각(Thinking Trace) 디스코드 카드 라이브 업데이트]
            thought_snippet = ""
            if reasoning_text:
                thought_snippet = reasoning_text.strip()
            elif "<think>" in content_text:
                think_match = re.search(r"<think>(.*?)(?:</think>|$)", content_text, flags=re.DOTALL)
                thought_snippet = think_match.group(1).strip() if think_match else content_text.strip()
            else:
                thought_snippet = re.sub(r"</?(function|parameter|tool_call)[^>]*>|<tool_call>.*?</tool_call>", "", content_text, flags=re.DOTALL).strip()

            display_thought = ""
            if thought_snippet:
                t_lines = [l.strip() for l in thought_snippet.split("\n") if l.strip()]
                display_thought = "\n".join(t_lines[-6:]) if len(t_lines) > 6 else thought_snippet
                if len(display_thought) > 650:
                    display_thought = display_thought[-650:]

                elapsed_live = format_elapsed_time(time.time() - start_time)
                status_live_text = (
                    f"🤖 **[Qwen 자율 에이전트 실시간 대시보드]**\n"
                    f"> 🔄 **진행 상태**: `Step {iteration+1}/{MAX_AGENT_LOOPS}` (경과: `{elapsed_live}` | 실행 도구: `{total_tools_executed}개`)\n"
                    f"> 🧠 **실시간 추론(`<think>` 최근)**:\n"
                    f"```text\n💭 {display_thought}\n```\n"
                    f"> ⚡ *자율 탐색 및 추론 진행 중... (실시간 지시/피드백 가능 / 중단: `!stop`)*"
                )
                try:
                    await status_msg.edit(content=status_live_text)
                except Exception:
                    pass

            # Discord I/O above can yield to !stop after the model-stage check.
            # Cancellation must win before any terminal decision or dispatch.
            try:
                token.raise_if_cancelled()
            except RunCancelled as stage_error:
                settle_stage_failure(stage_error)
                break

            tool_calls_to_run = []
            if msg.tool_calls:
                for tc in msg.tool_calls:
                    try:
                        args = json.loads(tc.function.arguments) if isinstance(tc.function.arguments, str) else tc.function.arguments
                    except Exception:
                        args = {}
                    tool_calls_to_run.append({
                        "id": tc.id,
                        "name": tc.function.name,
                        "arguments": args
                    })

            if not tool_calls_to_run and content_text:
                extracted = extract_tool_calls_from_text(content_text)
                for i, e in enumerate(extracted):
                    tool_calls_to_run.append({
                        "id": f"call_xml_{iteration}_{i}",
                        "name": e["name"],
                        "arguments": e["arguments"]
                    })

            if iteration == 0 and not direct_call_failed and not tool_calls_to_run and content_text.strip():
                direct_text = clean_direct_response(content_text)
                if direct_text:
                    outcome.settle(outcome_mod.COMPLETED, outcome_mod.DETAIL_DIRECT_ANSWER)
                    final_raw = direct_text
                    break

            # [finish_task = 유일한 구조화된 완료 신호]
            # 동반 도구 호출 정책: finish_task와 같은 응답에 온 다른 도구 호출은
            # 실행하지 않는다. 완료 판단 이후에 부작용을 남기지 않기 위한 것이고,
            # 무엇이 거부되었는지는 로그와 최종 메시지에 남긴다.
            finish_calls = [tc for tc in tool_calls_to_run if tc["name"] == "finish_task"]
            if finish_calls:
                final_completed_report = finish_calls[0]["arguments"].get("report", "")
                companions = [tc["name"] for tc in tool_calls_to_run if tc is not finish_calls[0]]
                if companions:
                    refused_companion_calls = companions
                    log_session_event(
                        session_file,
                        f"⛔ [Step {iteration+1} finish_task 동반 호출 거부]",
                        "실행하지 않은 도구: " + ", ".join(companions),
                    )
                outcome.settle(outcome_mod.COMPLETED, outcome_mod.DETAIL_FINISH_TASK)
                print(f"[Run settled: {outcome.describe()} at Step {iteration+1}]", flush=True)
                final_raw = final_completed_report or full_raw_thought
                break

            if tool_calls_to_run:
                synthetic_tool_calls = [
                    {
                        "id": tc["id"],
                        "type": "function",
                        "function": {
                            "name": tc["name"],
                            "arguments": json.dumps(tc["arguments"], ensure_ascii=False) if isinstance(tc["arguments"], dict) else str(tc["arguments"])
                        }
                    }
                    for tc in tool_calls_to_run
                ]

                elapsed_live = format_elapsed_time(time.time() - start_time)
                tools_display = ", ".join([f"`{tc['name']}`" for tc in tool_calls_to_run[:3]])
                last_th_line = display_thought.splitlines()[-1][:80] if display_thought else "도구 실행 준비"
                tool_live_text = (
                    f"🤖 **[Qwen 자율 에이전트 실시간 대시보드]**\n"
                    f"> 🔄 **진행 상태**: `Step {iteration+1}/{MAX_AGENT_LOOPS}` (경과: `{elapsed_live}` | 현재까지 실행: `{total_tools_executed}개`)\n"
                    f"> 🛠️ **요청 도구**: {tools_display}\n"
                    f"> 💭 **최근 판단**: `{last_th_line}`\n"
                    f"> ⚡ *터미널 및 네트워크 I/O 실행 중... (실시간 지시 가능 / 중단: `!stop`)*"
                )
                try:
                    await status_msg.edit(content=tool_live_text)
                except Exception:
                    pass

                try:
                    token.raise_if_cancelled()
                except RunCancelled as stage_error:
                    settle_stage_failure(stage_error)
                    break

                batch_signatures = set()
                allowed_calls = []
                allowed_indexes = []
                allowed_signatures = []
                merged_results = [None] * len(tool_calls_to_run)
                for call_index, tc in enumerate(tool_calls_to_run):
                    signature = (
                        tc["name"],
                        json.dumps(
                            tc["arguments"],
                            sort_keys=True,
                            separators=(",", ":"),
                            ensure_ascii=False,
                        ),
                    )
                    if signature in batch_signatures:
                        merged_results[call_index] = _blocked_tool_result(
                            "same_batch_duplicate", tc["name"], 1, 1
                        )
                        continue
                    if (
                        signature == last_failed_signature
                        and consecutive_failed_tool_calls
                        >= MAX_CONSECUTIVE_FAILED_TOOL_CALLS
                    ):
                        merged_results[call_index] = _blocked_tool_result(
                            "consecutive_failure_limit",
                            tc["name"],
                            MAX_CONSECUTIVE_FAILED_TOOL_CALLS,
                            consecutive_failed_tool_calls,
                        )
                        continue
                    if (
                        total_tools_executed + len(allowed_calls)
                        >= MAX_TOOL_EXECUTIONS_PER_RUN
                    ):
                        merged_results[call_index] = _blocked_tool_result(
                            "run_tool_budget_exhausted",
                            tc["name"],
                            MAX_TOOL_EXECUTIONS_PER_RUN,
                            total_tools_executed + len(allowed_calls),
                        )
                        continue
                    batch_signatures.add(signature)
                    allowed_calls.append(tc)
                    allowed_indexes.append(call_index)
                    allowed_signatures.append(signature)
                    if (
                        last_failed_signature is not None
                        and signature != last_failed_signature
                    ):
                        last_failed_signature = None
                        consecutive_failed_tool_calls = 0

                total_tools_executed += len(allowed_calls)
                messages_payload.append({
                    "role": "assistant",
                    "content": content_text or None,
                    "tool_calls": synthetic_tool_calls
                })
                for tc in allowed_calls:
                    print(f"Executing tool {tc['name']} with args {tc['arguments']}", flush=True)
                    log_session_event(session_file, f"🛠️ [Step {iteration+1} 도구 호출] {tc['name']}", json.dumps(tc['arguments'], ensure_ascii=False, indent=2))

                parallel_results = []
                if allowed_calls:
                    try:
                        parallel_results = await execute_tools_in_parallel(
                            allowed_calls, step_num=iteration+1, ledger=ledger, token=token
                        )
                    except (RunCancelled, StageTimeout) as stage_error:
                        settle_stage_failure(stage_error)
                        break
                    except Exception as tool_error:
                        settle_stage_failure(tool_error)
                        break

                for call_index, tc, signature, tool_result in zip(
                    allowed_indexes, allowed_calls, allowed_signatures, parallel_results
                ):
                    merged_results[call_index] = tool_result
                    if _tool_result_failed(tc["name"], tool_result):
                        if signature == last_failed_signature:
                            consecutive_failed_tool_calls += 1
                        else:
                            last_failed_signature = signature
                            consecutive_failed_tool_calls = 1
                    else:
                        last_failed_signature = None
                        consecutive_failed_tool_calls = 0

                for tc, tool_result in zip(tool_calls_to_run, merged_results):
                    log_session_event(session_file, f"📥 [Step {iteration+1} 도구 실행 결과] {tc['name']}", tool_result)

                    messages_payload.append({
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "name": tc["name"],
                        "content": tool_result
                    })

                # [옵션 B: 매 10스텝 도달 시 중간 진행 보고서 자동 발행 및 자율 연속 연장]
                if (iteration + 1) % CHECKPOINT_INTERVAL == 0 and (iteration + 1) < MAX_AGENT_LOOPS and not token.cancelled:
                    checkpoint_num = (iteration + 1) // CHECKPOINT_INTERVAL
                    print(f"[Checkpoint {checkpoint_num} Reached at Step {iteration+1} - Generating Intermediate Report]", flush=True)
                    try:
                        await status_msg.edit(content=f"📊 **[Step {iteration+1} 체크포인트 도달]** 중간 진행 상황 종합 보고서 작성 및 다음 구간 자동 연장 준비 중... ▌")
                    except Exception:
                        pass

                    report_prompt = (
                        "당신은 지금까지의 자율 탐색 결과를 사용자에게 브리핑하는 전문 AI 리포터입니다.\n"
                        "절대로 도구를 호출하지 말고, 아래 탐색 기록을 바탕으로 다음 3가지 항목에 맞추어 한국어 마크다운으로 상세한 '중간 진행 상황 보고서'를 작성하세요:\n"
                        "### 1. 📌 현재까지 완료된 핵심 작업\n"
                        "### 2. 🔍 발견된 핵심 데이터 및 보안 단서\n"
                        "### 3. 🎯 향후 진행할 구체적인 작업 계획\n\n"
                        "권위 있는 조사 상태 블록이 주어지면 그것을 사실의 기준으로 삼으세요. "
                        "반증된 가설과 무효 결론을 현재 사실처럼 쓰지 말고, 반증되었다고 명시하세요.\n"
                        "상태 블록을 정정해야 한다면 보고서 끝에 ```state_update 로 시작하는 코드블록 하나를 붙여 "
                        "record_state와 같은 형식(goal, evidence, hypotheses, conclusions)의 JSON을 넣으세요. "
                        "이 블록은 사용자에게 보이지 않고 상태에 반영됩니다. 정정할 것이 없으면 붙이지 마세요."
                    )
                    inter_state_block = ledger.render()
                    inter_source = build_rollup_source(messages_payload[-16:])
                    inter_context = "\n\n".join(
                        part for part in [
                            inter_state_block,
                            f"[현재까지의 탐색 및 도구 실행 기록]\n{inter_source}",
                        ] if part
                    )
                    inter_payload = [
                        {"role": "system", "content": report_prompt},
                        {"role": "user", "content": f"{inter_context}\n\n위 내용을 종합하여 사용자가 한눈에 진행 상황을 파악할 수 있도록 중간 보고서를 작성해 주세요."}
                    ]
                    checkpoint_ok = False
                    inter_text = ""
                    try:
                        inter_resp = await run_completion_stage(
                            token=token,
                            stage="checkpoint",
                            model=MODEL_NAME,
                            messages=inter_payload,
                            max_tokens=2048,
                            temperature=0.4,
                            reasoning_effort="none"
                        )
                        inter_text = inter_resp.choices[0].message.content or ""

                        # 보고서에만 있던 정정을 롤오버 이전에 권위 있는 상태로 반영한다.
                        state_updates, inter_text = parse_state_update_blocks(inter_text)
                        for update_payload in state_updates:
                            state_report = ledger.apply_updates(update_payload)
                            log_session_event(
                                session_file,
                                f"🧾 [Step {iteration+1} 체크포인트 상태 정정 반영]",
                                state_report,
                            )
                            print(f"[Checkpoint State Update Applied at Step {iteration+1}]", flush=True)

                        inter_formatted = format_full_discord_output(inter_text)

                        elapsed_checkpoint = time.time() - start_time
                        elapsed_cp_str = format_elapsed_time(elapsed_checkpoint)

                        cp_message = (
                            f"📊 **[중간 진행 보고서 - {iteration+1}스텝 체크포인트]**\n\n"
                            f"{inter_formatted}\n\n"
                            f"> ⏱️ **경과 시간**: {elapsed_cp_str} (총 {total_tools_executed}개 도구 실행 완료)\n"
                            f"> ⚡ **[자율 연장]** 목표 달성을 위해 다음 구간(Step {iteration+2} ~ {iteration+1+CHECKPOINT_INTERVAL})으로 계속 진행합니다... *(중단: `!stop`)*"
                        )

                        chunks_cp = []
                        rem_cp = cp_message
                        while rem_cp:
                            if len(rem_cp) <= 1900:
                                chunks_cp.append(rem_cp)
                                break
                            split_i = rem_cp.rfind("\n", 0, 1900)
                            if split_i == -1 or split_i < 1000:
                                split_i = 1900
                            chunks_cp.append(rem_cp[:split_i])
                            rem_cp = rem_cp[split_i:].lstrip("\n")

                        for c in chunks_cp:
                            await message.channel.send(c)

                        log_session_event(session_file, f"📊 [Step {iteration+1} 중간 진행 보고서 제출 & 자동 연장]", inter_text)
                        checkpoint_ok = True

                    except (RunCancelled, StageTimeout) as stage_error:
                        settle_stage_failure(stage_error)
                        break
                    except Exception as cp_err:
                        print(f"[Intermediate Report Synthesis Error]: {cp_err}", flush=True)
                        log_session_event(session_file, f"⚠️ [Step {iteration+1} 중간 보고서 실패]", str(cp_err))

                    if checkpoint_ok:
                        # 보고서 본문을 payload에 남긴다. 이것이 없으면 보고서에만 존재한
                        # 정정이 바로 뒤 롤오버의 압축 입력에서 사라진다.
                        messages_payload.append({
                            "role": "assistant",
                            "content": "[중간 보고서 제출 완료]\n" + _clip_summary_text(inter_text, 2000),
                        })
                        messages_payload.append({"role": "user", "content": (
                            f"[🤖 시스템 자율 연장 안내: Step {iteration+1} 중간 보고서가 디스코드에 전송되었습니다. "
                            f"목표를 100% 달성할 때까지 전용 작업 공간(plan.md, findings.md)을 업데이트하며 다음 분석 작업을 계속 실행하세요. "
                            f"판단이 바뀐 부분은 record_state로 상태를 갱신하세요. "
                            f"모든 조사가 완전히 끝나면 finish_task를 호출하세요.]"
                        )})
                    else:
                        # 실패한 체크포인트에 성공 마커를 남기지 않는다.
                        messages_payload.append({
                            "role": "assistant",
                            "content": f"[중간 보고서 생성 실패 - Step {iteration+1} 체크포인트는 제출되지 않았습니다]",
                        })
                        messages_payload.append({"role": "user", "content": (
                            f"[🤖 시스템 안내: Step {iteration+1} 중간 보고서 생성이 실패했습니다. 제출된 것으로 간주하지 마세요. "
                            f"권위 있는 조사 상태 블록을 기준으로 다음 분석 작업을 계속 실행하고, "
                            f"판단이 바뀐 부분은 record_state로 상태를 갱신하세요. "
                            f"모든 조사가 완전히 끝나면 finish_task를 호출하세요.]"
                        )})

                consecutive_no_tool_responses = 0

                # 마지막 스텝이면 여기서 소진으로 확정한다. 불필요한 롤오버를
                # 한 번 더 돌리고 나서 루프 경계로 조용히 끝나지 않도록.
                if token.cancelled:
                    outcome.settle(outcome_mod.STOPPED, token.reason)
                    print(f"[Run settled: {outcome.describe()} at Step {iteration+1}]", flush=True)
                    break
                if iteration + 1 >= MAX_AGENT_LOOPS:
                    outcome.settle(outcome_mod.EXHAUSTED, outcome_mod.DETAIL_STEP_BUDGET)
                    print(f"[Run settled: {outcome.describe()} at Step {iteration+1}]", flush=True)
                    break

                try:
                    await maybe_roll_context(iteration + 1)
                except (RunCancelled, StageTimeout) as stage_error:
                    settle_stage_failure(stage_error)
                    break
                continue

            # [도구 호출 없는 응답] 완료 의사는 문구로 추정하지 않는다.
            # finish_task만이 완료 신호이며, 여기서는 그것을 요구하고 계속 진행한다.
            consecutive_no_tool_responses += 1
            if token.cancelled:
                outcome.settle(outcome_mod.STOPPED, token.reason)
                print(f"[Run settled: {outcome.describe()} at Step {iteration+1}]", flush=True)
                final_raw = full_raw_thought or content_text
                break

            if consecutive_no_tool_responses >= MAX_NO_TOOL_RESPONSES:
                outcome.settle(
                    outcome_mod.EXHAUSTED,
                    f"{outcome_mod.DETAIL_NO_TOOL_STALL} {consecutive_no_tool_responses}회",
                )
                print(f"[Run settled: {outcome.describe()} at Step {iteration+1}]", flush=True)
                final_raw = full_raw_thought or content_text
                break

            if iteration + 1 >= MAX_AGENT_LOOPS:
                outcome.settle(outcome_mod.EXHAUSTED, outcome_mod.DETAIL_STEP_BUDGET)
                print(f"[Run settled: {outcome.describe()} at Step {iteration+1}]", flush=True)
                final_raw = full_raw_thought or content_text
                break

            nudge_content = (
                "[🤖 시스템 자율 루프 유지 안내: 도구가 호출되지 않았습니다. "
                "사용자의 목표를 100% 달성하기 위해 필요한 bash_exec 명령어를 계속 실행하세요. "
                "조사가 완전히 끝났다면 반드시 finish_task(report=...)를 호출하세요. "
                "본문에 '최종 보고서'라고 쓰는 것만으로는 종료되지 않습니다.]"
            )
            messages_payload.append({
                "role": "assistant",
                "content": content_text or "[중간 진행 계획]"
            })
            messages_payload.append({
                "role": "user",
                "content": nudge_content
            })
            log_session_event(session_file, f"🔄 [Step {iteration+1} 자율 루프 지속 추진(Nudge)]", content_text or "(진행 중)")
            try:
                await status_msg.edit(content=f"🛠️ **[Step {iteration+1}/{MAX_AGENT_LOOPS}]** ⚡ 자율 루프 가속 진행 중... ▌")
            except Exception:
                pass
            try:
                await maybe_roll_context(iteration + 1)
            except (RunCancelled, StageTimeout) as stage_error:
                settle_stage_failure(stage_error)
                break
            continue

        # 루프가 break 없이 끝나는 경로도 사유 없이 남지 않게 한다.
        outcome.settle(outcome_mod.EXHAUSTED, outcome_mod.DETAIL_STEP_BUDGET)

        cleaned_check = re.sub(r"<think>.*?</think>|</?(function|parameter|tool_call)[^>]*>", "", final_raw, flags=re.DOTALL).strip()

        # 합성 여부는 길이 추정이 아니라 종료 사유로 결정한다.
        # 취소와 마감 실패는 후속 모델 단계를 시작하지 않는다. 같은 백엔드가
        # 멈춰 있을 수 있으므로 보존된 상태로 결정적 중간 보고서를 만든다.
        no_follow_up_stage = outcome.reason in (outcome_mod.STOPPED, outcome_mod.FAILED)
        uses_local_fallback = no_follow_up_stage
        if no_follow_up_stage:
            final_raw = build_incomplete_report(outcome, ledger, rolling_summary, messages_payload)
            needs_synthesis = False
        else:
            # EXHAUSTED는 정상적으로 응답한 모델이 스텝/정체 예산만 소진한 경우라
            # 한 번의 bounded synthesis가 유용하다. 완료인데 본문이 없을 때도 합성한다.
            needs_synthesis = (not outcome.is_completed) or (not cleaned_check)
        if outcome.detail == outcome_mod.DETAIL_DIRECT_ANSWER:
            needs_synthesis = False

        if needs_synthesis:
            try:
                await status_msg.edit(content=f"🛠️ **[{outcome.label}]** 수집된 결과 종합 보고서 작성 중... ▌")
            except Exception:
                pass

            if outcome.is_completed:
                closing_instruction = (
                    "사용자의 원래 질문에 대해 한국어로 매우 명확하고 완성도 높은 최종 종합 결론 보고서를 "
                    "마크다운으로 상세히 작성하세요."
                )
            else:
                closing_instruction = (
                    f"이 조사는 완료되지 않았습니다 (사유: {outcome.describe()}). 완료된 것처럼 쓰지 마세요.\n"
                    "지금까지 확인된 것과 확인되지 않은 것을 구분해 한국어 마크다운으로 중간 보고서를 작성하고, "
                    "남은 미해결 항목과 다음에 이어서 해야 할 작업을 반드시 명시하세요."
                )

            final_report_prompt = (
                "당신은 지금까지의 모든 자율 탐색 및 분석 결과를 종합하여 보고서를 작성하는 수석 분석가입니다.\n"
                f"절대로 도구를 호출하지 말고, 아래 모든 실행 기록과 수집 데이터를 바탕으로 {closing_instruction}\n"
                "권위 있는 조사 상태 블록이 사실의 기준입니다. 반증된(rejected) 가설을 유망한 후보로 되살리지 말고, "
                "무효 결론을 현재 사실로 제시하지 마세요. 폐기된 방향은 왜 폐기되었는지 근거 증거와 함께 밝히세요."
            )
            # 롤오버 이후 누적 요약은 교체된 system 메시지 안에만 남는데
            # build_rollup_source는 system을 건너뛴다. 그래서 상태 블록과 누적 요약을
            # 최신 tail과 함께 명시적으로 넣는다.
            synth_state_block = ledger.render()
            synth_source = build_rollup_source(messages_payload)
            synth_context = "\n\n".join(
                part for part in [
                    synth_state_block,
                    f"[{ROLLING_SUMMARY_LABEL}]\n{rolling_summary}" if rolling_summary else "",
                    f"[최근 탐색 및 분석 결과 기록]\n{synth_source}",
                ] if part
            )
            synthesis_payload = [
                {"role": "system", "content": final_report_prompt},
                {"role": "user", "content": f"{synth_context}\n\n위 결과를 바탕으로 보고서를 마크다운으로 상세히 작성해 주세요."}
            ]
            try:
                synth_resp = await run_completion_stage(
                    token=token,
                    stage="synthesis",
                    model=MODEL_NAME,
                    messages=synthesis_payload,
                    max_tokens=4096,
                    temperature=0.4,
                    reasoning_effort="none"
                )
                final_raw = synth_resp.choices[0].message.content or ""
                log_session_event(session_file, f"📝 [보고서 합성 - {outcome.describe()}]", final_raw)
            except RunCancelled as synthesis_cancelled:
                uses_local_fallback = True
                final_raw = build_incomplete_report(
                    outcome, ledger, rolling_summary, messages_payload
                )
                final_raw += (
                    "\n\n> 보고서 합성 취소: `"
                    + synthesis_cancelled.reason
                    + "`. 모델 보고서 대신 보존된 상태를 사용했습니다."
                )
                log_session_event(session_file, "🛑 [보고서 합성 취소]", final_raw)
            except StageTimeout as synthesis_timeout:
                uses_local_fallback = True
                final_raw = build_incomplete_report(
                    outcome, ledger, rolling_summary, messages_payload
                )
                final_raw += (
                    "\n\n> 보고서 합성 마감 초과: `"
                    + str(synthesis_timeout)
                    + "`. 모델 보고서 대신 보존된 상태를 사용했습니다."
                )
                log_session_event(session_file, "⏱️ [보고서 합성 마감 초과]", final_raw)
            except Exception as synthesis_error:
                uses_local_fallback = True
                final_raw = build_incomplete_report(
                    outcome, ledger, rolling_summary, messages_payload
                )
                failure = _clip_summary_text(
                    f"{type(synthesis_error).__name__}: {synthesis_error}", 500
                )
                final_raw += (
                    "\n\n> 보고서 합성 업스트림 실패: `"
                    + failure
                    + "`. 모델 보고서 대신 보존된 상태를 사용했습니다."
                )
                log_session_event(session_file, "⚠️ [보고서 합성 업스트림 실패]", final_raw)

        final_text = format_full_discord_output(final_raw)
        if not final_text:
            if outcome.is_completed:
                final_text = "모델이 텍스트를 출력하지 않았습니다. 대화 기록을 !reset 후 다시 시도해 주세요."
            else:
                final_text = (
                    f"수집된 결과 없이 조사가 종료되었습니다 (사유: {outcome.describe()}). "
                    "완료된 작업은 없습니다. 다시 요청하거나 목표를 더 구체적으로 지정해 주세요."
                )

        elapsed = time.time() - start_time
        completion_ts = int(time.time())
        elapsed_str = format_elapsed_time(elapsed)

        # 종료 사유가 라벨과 푸터를 결정한다. 중단·소진·실패에는 완료 라벨도,
        # '완료 시간' 푸터도 붙지 않는다.
        is_direct_answer = outcome.detail == outcome_mod.DETAIL_DIRECT_ANSWER
        if is_direct_answer:
            final_text_with_footer = final_text
        else:
            run_stats = f"총 {total_tools_executed}개 도구 실행 / 소요: {elapsed_str}"
            if outcome.is_completed:
                footer_text = f"\n\n> ⏱️ **완료 시간**: <t:{completion_ts}:T> ({run_stats})"
            else:
                footer_text = (
                    f"\n\n> 🔻 **{outcome.label}** — 종료 시각 <t:{completion_ts}:T> ({run_stats})\n"
                    f"> 사유: `{outcome.describe()}`. 이 보고서는 완료된 조사 결과가 아닙니다."
                )
            if refused_companion_calls:
                footer_text += (
                    "\n> ⛔ finish_task와 함께 온 도구 호출은 실행하지 않았습니다: "
                    + ", ".join(f"`{name}`" for name in refused_companion_calls)
                )
            header_text = "" if outcome.is_completed else f"**{outcome.label}**\n\n"
            final_text_with_footer = header_text + final_text + footer_text

        if uses_local_fallback:
            final_text_with_footer = bound_local_fallback_output(final_text_with_footer)
            chunks = [
                final_text_with_footer[i:i + DISCORD_CHUNK_MAX_CHARS]
                for i in range(0, len(final_text_with_footer), DISCORD_CHUNK_MAX_CHARS)
            ]
        else:
            chunks = []
            remaining = final_text_with_footer
            while remaining:
                if len(remaining) <= DISCORD_CHUNK_MAX_CHARS:
                    chunks.append(remaining)
                    break
                split_idx = remaining.rfind("\n", 0, DISCORD_CHUNK_MAX_CHARS)
                if split_idx == -1 or split_idx < 1000:
                    split_idx = DISCORD_CHUNK_MAX_CHARS
                chunks.append(remaining[:split_idx])
                remaining = remaining[split_idx:].lstrip("\n")

        try:
            await status_msg.delete()
        except Exception:
            pass

        await message.reply(chunks[0])
        for extra_chunk in chunks[1:]:
            await message.channel.send(extra_chunk)

        history.append({"role": "assistant", "content": final_text})
        log_session_event(
            session_file,
            f"🏁 [런 종료: {outcome.describe()}]",
            f"도구 실행 {total_tools_executed}건 / 소요 {elapsed_str} / 응답 {len(final_text)}자",
        )
        print(
            f"[Run finished: {outcome.describe()}] {len(final_text)} chars in {elapsed_str}",
            flush=True,
        )

    except Exception as e:
        outcome.settle(outcome_mod.FAILED, f"예외: {type(e).__name__}")
        err_msg = f"⚠️ **{outcome.label}** — 작업 도중 예외 발생: `{e}`\n📁 현재까지의 추론/도구 실행 기록은 시스템 로그에 저장되었습니다."
        print(f"[Run settled: {outcome.describe()}] {e}", file=sys.stderr, flush=True)
        log_session_event(session_file, f"⚠️ [종료: {outcome.describe()}]", str(e))
        try:
            await status_msg.edit(content=err_msg)
        except Exception:
            await message.reply(err_msg)
    finally:
        stop_typing.set()
        try:
            await typing_task
        except Exception:
            pass
        finally:
            release_run()

async def main():
    async with bot:
        await bot.start(DISCORD_TOKEN)

if __name__ == "__main__":
    asyncio.run(main())
