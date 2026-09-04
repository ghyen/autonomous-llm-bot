#!/usr/bin/env python3
"""
Discord LLM Bot - Auto-Extension Goal-Driven Autonomous Agent
Integrated with:
- User's Streaming Completion & Rolling Compaction (Rollup) Architecture
- Pre-Send Tool Payload Validator (tool_call 상관관계 + chat template)
- Continuous Keep-Alive Typing Heartbeat Loop (7s Interval)
- Live Updating Real-Time Dashboard Card (message.edit)
- 10-Step Periodic Checkpoints & Instant Feedback Loop
"""

import os
import sys
import re
import json
import time
import asyncio
import tempfile
from collections import defaultdict
from typing import List, Dict, Any, Optional

import discord
from discord.ext import commands
import httpx
from openai import AsyncOpenAI
from types import SimpleNamespace

import authz
import outcome as outcome_mod
import run_state
import session_log
import steering as steering_mod
from deadlines import (
    CancelToken,
    RunCancelled,
    StageTimeout,
    _reap,
    stream_chunks,
    with_deadline,
)
from outcome import RunOutcome
from ledger import ResearchLedger
from run_workspace import RunActiveError, RunCatalog, RunNotFoundError
from session_log import log_content_debug, log_session_event
from config import ConfigError, load_config, startup_diagnostics
import tool_sandbox

# Configuration is fully validated before any filesystem or network side effect.
try:
    CONFIG = load_config()
except ConfigError as config_error:
    # 시작 실패에도 시각과 리비전이 붙어야 한다. 타임스탬프 없는 print 때문에
    # 과거 시작 실패가 언제 있었는지 사후에 특정할 수 없었다(이슈 #11).
    log_session_event(None, "config_error", detail=str(config_error))
    sys.exit(1)

DISCORD_TOKEN = CONFIG.discord_token
LLM_BASE_URL = CONFIG.llm_base_url
MODEL_NAME = CONFIG.model_name
FREE_RESPONSE_CHANNEL_IDS = set(CONFIG.free_response_channel_ids)
ALLOWED_USER_IDS = set(CONFIG.allowed_user_ids)
ADMIN_USER_IDS = set(CONFIG.admin_user_ids)
TOOLS_ENABLED = CONFIG.tools_enabled
TOOL_LIMITS = {
    "cpu_seconds": CONFIG.tool_cpu_seconds,
    "memory_bytes": CONFIG.tool_memory_bytes,
    "process_limit": CONFIG.tool_process_limit,
    "thread_limit": CONFIG.tool_thread_limit,
    "open_files": CONFIG.tool_open_files,
    "file_bytes": CONFIG.tool_file_bytes,
    "output_bytes": CONFIG.tool_output_bytes,
    "disk_bytes": CONFIG.tool_disk_bytes,
}
TOOL_NETWORK_ALLOWLIST = CONFIG.tool_network_allowlist

# 로그 정책은 어떤 파일 부작용보다 먼저 선다. 바로 아래 RUN_CATALOG 생성이
# 이미 로그 트리를 만들기 때문에, 모드·회전·보관 정책이 그 전에 있어야 한다.
session_log.configure(
    CONFIG.system_log_dir,
    content_debug=CONFIG.log_content_debug,
    max_bytes=CONFIG.log_max_bytes,
    retention_days=CONFIG.log_retention_days,
    content_retention_hours=CONFIG.log_content_debug_retention_hours,
)

for diagnostic_line in startup_diagnostics(CONFIG):
    log_session_event(None, "config", line=diagnostic_line)

RUN_CATALOG = RunCatalog(CONFIG.workspace_dir, CONFIG.system_log_dir)

SYSTEM_PROMPT_TEMPLATE = """당신은 터미널 환경과 현재 실행 전용 작업 공간(workspace)에 직접 접근할 수 있는 **완전자율 목표 달성 AI 에이전트**입니다.

[운영 환경]
- 현재 실행 작업 디렉토리: `{workspace_root}`
- 시스템 파일:
  - `{workspace_root}/skills/`: 현재 실행 전용 재사용 스크립트 및 도구 저장소 (`.py`, `.sh`, `.bash`, `.md`)
  - `{workspace_root}/plan.md`: 에이전트의 목표 달성 체크리스트 및 실시간 진행 상태
  - `{workspace_root}/findings.md`: 수집된 핵심 데이터, 단서, 팩트, 취약점 및 결론 누적 기록
- 사용할 수 있는 도구:
  - `bash_exec(command)`: 현재 실행 작업 공간에서 쉘 명령어 실행 (zg, curl, python3, nmap, jq, sed, awk, find, grep 등).
  - `read_file(path)`: 파일 읽기
  - `write_file(path, content, expected_revision)`: 파일 생성 및 덮어쓰기
  - `web_search(query)`: DuckDuckGo 웹 검색
  - `record_state(...)`: 목표·증거·가설·결론의 권위 있는 상태를 갱신하는 전용 도구
  - `finish_task(report)`: 사용자의 목표를 100% 달성하여 최종 결론을 낼 때 호출하는 전용 완료 도구
- 루트 `plan.md`와 `findings.md`를 쓸 때는 직전 읽기에서 받은 `sha256:<64자리 해시>`를 `expected_revision`으로 그대로 전달하세요. 파일이 없을 때 최초 생성은 `absent`를 사용하세요.
- 두 파일의 변경된 읽기는 전체 내용과 revision을 반환하고, 변경 없는 재읽기는 내용 대신 hash reference만 반환합니다. conflict이면 최신 내용을 다시 읽고 병합하세요.

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
3. 로컬 코드나 프로젝트 문서 탐색 시, 키워드를 정확히 모르는 상태에서 무작정 grep/find를 반복하지 마세요. 로컬 온디바이스 하이브리드(시맨틱+BM25) 검색 CLI인 `zg query "<자연어 의도>"`를 `bash_exec`로 우선 실행하여 관련 코드와 심볼 위치를 빠르게 특정하세요.
4. 반복되거나 복잡한 데이터 파싱, 스크래핑, 쉘 작업은 `write_file`로 `skills/<name>.py` 또는 `skills/<name>.sh`에 스크립트화하여 저장하고 `bash_exec`로 실행하여 재사용하세요.
5. 발견된 사실은 `findings.md`에 지속적으로 기록하고 `plan.md`의 진행 상태를 업데이트하세요.
6. 가설을 세우거나 반증하거나 결론을 내린 스텝에서는 같은 스텝에 `record_state`를 호출해 상태를 갱신하세요.
7. 모든 목표가 완전히 해결되었을 때만 `finish_task(report=...)`를 호출하여 최종 보고서를 제출하세요.
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
            "description": f"현재 실행 작업 공간에서 쉘 명령어를 실행합니다. timeout은 {CONFIG.bash_timeout:g}초입니다.",
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
            "description": "현재 실행 작업 공간 안의 파일 내용을 읽어옵니다. 작업 공간 밖 경로는 거부됩니다.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "읽을 파일 경로 (현재 실행 작업 공간 기준 상대경로)"
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
            "description": "현재 실행 작업 공간의 파일을 작성합니다. 루트 plan.md/findings.md는 expected_revision이 필수입니다.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "작성할 파일 경로 (현재 실행 작업 공간 기준 상대경로)"
                    },
                    "content": {
                        "type": "string",
                        "description": "작성할 텍스트 내용"
                    },
                    "expected_revision": {
                        "type": "string",
                        "description": "루트 plan.md/findings.md 전용: absent 또는 직전 읽기의 sha256:<64 hex> revision"
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
            log_session_event(None, "slash_sync", status="ok")
        except Exception as e:
            log_session_event(None, "slash_sync", status="notice", error=type(e).__name__)

bot = CustomBot(command_prefix="!", intents=intents)

channel_history = defaultdict(list)
channel_summary = defaultdict(str)
channel_reasoning = defaultdict(lambda: "high")
# A run marks its lease active before its first await, so the admission check and
# the mailbox it publishes are decided in one event-loop turn: a message arriving
# during a live run is always steering, never a second run. The list keeps every
# lease identity-scoped so a run's cleanup can only ever remove its own.
channel_cancel_token = {}
channel_run_leases = defaultdict(list)
channel_ledger = defaultdict(ResearchLedger)

channel_active_runs = defaultdict(bool)
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
    log_session_event(None, "cancel_requested", reason=reason, channel=channel_id)
    return True


def clear_channel_state(channel_id) -> None:
    channel_history[channel_id].clear()
    channel_summary[channel_id] = ""
    channel_ledger[channel_id].clear()
    # 메모리만 지우면 지운 런이 재시작 뒤 디스크의 durable 레코드에서 되살아난다.
    for workspace in RUN_CATALOG.workspaces(channel_id):
        run_state.discard(workspace)


def steering_receipt_notice(receipt, text: str) -> str:
    """접수 결과를 실제 처리 결과대로 알린다.

    거절된 지시에 "다음 스텝에 반영"이라고 답하면 안 된다. 반영할 스텝이
    없다는 것이 바로 거절 사유이기 때문이다.

    지시 본문은 되돌려 인용하지 않는다. 사용자가 방금 입력한 것이라 잃는 정보가
    없고, 인용하면 채널에 원문이 한 번 더 남는다(이슈 #11).
    """
    if receipt.state == steering_mod.QUEUED:
        return (
            f"📥 **[실시간 개입 접수]** 대기열 {receipt.depth}번째로 등록했습니다 ({len(text)}자). "
            "다음 스텝에 반영하며, 반영되지 못하면 종료 시 알려드립니다."
        )
    if receipt.state == steering_mod.COALESCED:
        return (
            f"📥 **[실시간 개입 병합]** 대기 중인 동일 지시에 병합했습니다 (대기 {receipt.depth}건). "
            "같은 지시를 두 번 주입하지 않습니다."
        )
    if receipt.reason == steering_mod.REASON_QUEUE_FULL:
        return (
            f"⛔ **[실시간 개입 거절]** 대기열이 상한({receipt.depth}건)까지 찼습니다. "
            "이 지시는 **적용되지 않았습니다**. 대기 중인 지시가 반영된 뒤 다시 보내주세요."
        )
    if receipt.reason == steering_mod.REASON_TERMINAL:
        return (
            "⛔ **[실시간 개입 미적용]** 실행이 종료 단계(보고서 합성·전송)에 들어가 "
            "반영할 다음 스텝이 없습니다. 이 지시는 **적용되지 않았습니다**. "
            "런이 끝난 뒤 다시 보내주세요."
        )
    return (
        "⛔ **[실시간 개입 미적용]** 이 실행은 단문 직접 답변이라 지시를 반영할 스텝이 없습니다. "
        "이 지시는 **적용되지 않았습니다**. 답변이 끝난 뒤 다시 보내주세요."
    )


MAX_RECENT_TURNS = 8
CHECKPOINT_INTERVAL = 30
MAX_AGENT_LOOPS = 250
MAX_CONSECUTIVE_FAILED_TOOL_CALLS = 2
MAX_TOOL_EXECUTIONS_PER_RUN = 250

# 대기 중인 지시는 각각 별도의 steering 블록으로 프롬프트에 실린다. 상한이 없으면
# 한 채널이 이후 모든 스텝의 프롬프트를 대기 깊이만큼 부풀릴 수 있다.
STEERING_QUEUE_MAX = 8


def _robust_json_loads(raw: str):
    """Parse JSON with resilience to markdown fences, unescaped newlines, and trailing commas."""
    if not raw or not isinstance(raw, str):
        return None
    s = raw.strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z0-9_-]*\s*", "", s)
        s = re.sub(r"\s*```$", "", s).strip()
    if not s:
        return None

    # 1. Standard json parse with strict=False (allows unescaped newlines/control chars in strings)
    try:
        return json.loads(s, strict=False)
    except Exception:
        pass

    # 2. Fix trailing commas before closing braces or brackets (repeatedly for nested objects)
    cleaned = s
    while re.search(r",\s*([\}\]])", cleaned):
        cleaned = re.sub(r",\s*([\}\]])", r"\1", cleaned)
    try:
        return json.loads(cleaned, strict=False)
    except Exception:
        pass

    return None


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


def _invalid_tool_arguments_result(reason: str, tool_name: str) -> str:
    return json.dumps(
        {
            "error": "invalid_tool_arguments",
            "reason": reason,
            "tool": tool_name,
            "directive": "Provide tool arguments as a valid JSON object.",
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _tool_result_failed(tool_name: str, result: str) -> bool:
    if tool_name in ("read_file", "write_file"):
        try:
            envelope = json.loads(result)
        except (TypeError, ValueError):
            return False
        return (
            isinstance(envelope, dict)
            and envelope.get("status") in ("error", "conflict", "resource_limit")
        )
    if result.startswith("[Error"):
        return True
    if tool_name == "bash_exec":
        exit_code = re.search(r"\[exit code: (-?\d+)\]\s*$", result)
        return bool(exit_code and int(exit_code.group(1)) != 0)
    if tool_name == "record_state":
        return result.partition("\n")[0] == "[record_state status: refused]"
    return False


ROLLING_COMPACTION_INTERVAL = 10
KEEP_RECENT_TOOL_MESSAGES = 8
ROLLING_SUMMARY_SOURCE_MAX_CHARS = 24000
ROLLING_SUMMARY_MAX_CHARS = 10000
DEFAULT_TOOL_OUTPUT_MAX_CHARS = 2500

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

async def _auto_delete_notice(msg: discord.Message, delay: int = 6):
    await asyncio.sleep(delay)
    try:
        await msg.delete()
    except Exception:
        pass

# --- Tool Execution Functions ---

async def tool_bash_exec(workspace, command: str) -> str:
    try:
        result = await tool_sandbox.run_worker(
            workspace,
            {
                "operation": "bash_exec",
                "workspace": str(workspace.root),
                "command": command,
                "limits": TOOL_LIMITS,
                "network_allowlist": list(TOOL_NETWORK_ALLOWLIST),
                "timeout": CONFIG.bash_timeout,
            },
            CONFIG.bash_timeout,
        )
        if result.get("status") != "success":
            if result.get("error") == "worker_timeout":
                return "[Error: Command timed out after {0:g} seconds]".format(
                    CONFIG.bash_timeout
                )
            return "[Error: {0}]".format(result.get("error", "worker_unavailable"))

        out_str = str(result.get("stdout", ""))
        err_str = str(result.get("stderr", ""))
        code = result.get("exit_code", 1)

        payload = ""
        if out_str:
            payload += f"[stdout]\n{strip_ansi(out_str)}\n"
        if err_str:
            payload += f"[stderr]\n{strip_ansi(err_str)}\n"

        if len(payload) > DEFAULT_TOOL_OUTPUT_MAX_CHARS:
            payload = (
                payload[:DEFAULT_TOOL_OUTPUT_MAX_CHARS]
                + f"\n... [출력 결과가 너무 길어 {DEFAULT_TOOL_OUTPUT_MAX_CHARS}자로 잘렸습니다. 필요한 경우 grep이나 head/tail로 조회하세요.]"
            )
        payload = payload.strip()
        return f"{payload}\n[exit code: {code}]" if payload else f"[exit code: {code}]"
    except Exception as e:
        return f"[Error: worker_unavailable ({type(e).__name__})]"


def _workspace_result(payload) -> str:
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


async def tool_read_file(workspace, path: str) -> str:
    try:
        result = await tool_sandbox.run_worker(
            workspace,
            {
                "operation": "read_file",
                "workspace": str(workspace.root),
                "path": path,
                "limits": TOOL_LIMITS,
            },
            CONFIG.tool_stage_timeout,
        )
        return _workspace_result(workspace.remember_worker_read(result))
    except Exception as e:
        return _workspace_result({
            "status": "error",
            "path": path,
            "error": type(e).__name__,
        })


async def tool_write_file(
    workspace, path: str, content: str, expected_revision
) -> str:
    try:
        request = {
            "operation": "write_file",
            "workspace": str(workspace.root),
            "path": path,
            "content": content,
            "expected_revision": expected_revision,
            "limits": TOOL_LIMITS,
        }
        lock = workspace.write_lock(path)
        if lock is None:
            result = await tool_sandbox.run_worker(
                workspace, request, CONFIG.tool_stage_timeout
            )
        else:
            async with lock:
                result = await tool_sandbox.run_worker(
                    workspace, request, CONFIG.tool_stage_timeout
                )
        return _workspace_result(workspace.remember_worker_write(result))
    except Exception as e:
        return _workspace_result({
            "status": "error",
            "path": path,
            "error": type(e).__name__,
        })


async def tool_web_search(query: str) -> str:
    try:
        with tempfile.TemporaryDirectory(prefix=".tool-web-") as root:
            result = await tool_sandbox.run_worker(
                root,
                {
                    "operation": "web_search",
                    "query": query,
                    "limits": TOOL_LIMITS,
                    "network_allowlist": list(TOOL_NETWORK_ALLOWLIST),
                    "timeout": CONFIG.tool_stage_timeout,
                },
                CONFIG.tool_stage_timeout,
            )
        if result.get("status") != "success":
            return "[Error: {0}]".format(result.get("error", "worker_unavailable"))
        results = result.get("results") or []
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
        return f"[Error: worker_unavailable ({type(e).__name__})]"

async def tool_record_state(ledger, updates) -> str:
    if ledger is None:
        return "[Error: 이 실행에는 상태 원장이 연결되어 있지 않습니다]"
    if isinstance(updates, str):
        try:
            updates = json.loads(updates)
        except Exception:
            return "[Error: record_state 인자를 JSON 객체로 해석할 수 없습니다]"
    try:
        report, had_refusal = ledger.apply_updates_with_status(updates)
        status = "refused" if had_refusal else "success"
        return f"[record_state status: {status}]\n{report}"
    except Exception as e:
        return f"[Error applying state update: {e}]"

async def tool_finish_task(workspace, report: str, step_num: int = 0) -> str:
    # 완료 신호는 stdout 한 줄이 아니라 런 로그의 레코드로 남는다. 종료 경로를
    # 사후에 구분하려면 이 호출이 어느 스텝에서 왔는지가 있어야 한다.
    log_session_event(
        workspace,
        "tool_finish_task",
        step=step_num,
        status="ok",
        tool="finish_task",
        report_chars=len(report),
    )
    log_content_debug(workspace, "finish_report", report, step=step_num)
    return f"[Task Completed Successfully. Final Report Registered ({len(report)} chars)]"

async def execute_tools_in_parallel(workspace, tool_calls: list, step_num: int = 1, ledger=None, token=None) -> list:
    async def _exec_single(tc):
        name = tc["name"]
        args = tc["arguments"]
        if name == "bash_exec":
            cmd = args.get("command", "")
            return await tool_bash_exec(workspace, cmd)
        elif name == "read_file":
            path = args.get("path", "")
            return await tool_read_file(workspace, path)
        elif name == "write_file":
            path = args.get("path", "")
            content = args.get("content", "")
            expected_revision = args.get("expected_revision")
            return await tool_write_file(
                workspace, path, content, expected_revision
            )
        elif name == "web_search":
            q = args.get("query", "")
            return await tool_web_search(q)
        elif name == "record_state":
            return await tool_record_state(ledger, args)
        elif name == "finish_task":
            r = args.get("report", "")
            return await tool_finish_task(workspace, r, step_num)
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
        parsed = _robust_json_loads(raw_json)
        if isinstance(parsed, dict):
            args = parsed.get("arguments", {})
            if isinstance(args, str):
                sub_parsed = _robust_json_loads(args)
                if isinstance(sub_parsed, dict):
                    args = sub_parsed
            extracted.append({
                "name": parsed.get("name"),
                "arguments": args if isinstance(args, dict) else {}
            })

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
                parsed_val = _robust_json_loads(pval)
                args_dict[pname] = parsed_val if parsed_val is not None else pval
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
    if max_chars <= 0:
        return ""
    if len(text) <= max_chars:
        return text
    omission = " ...[생략]"
    if max_chars <= len(omission):
        return text[:max_chars]
    return text[:max_chars - len(omission)].rstrip() + omission

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

# --- Pre-Send Payload Validator (tool 상관관계 + chat template) ---

TOOL_PAYLOAD_MISSING_RESULT = "[Error: 도구 결과가 기록되지 않았습니다]"
TOOL_CORRELATION_ERROR_MARKERS = ("tool_call_id", "tool_calls", "tool call")


def _call_parts(call):
    """(id, name, arguments). payload의 tool_call은 dict지만 객체 형태도 받는다."""
    if isinstance(call, dict):
        call_id = call.get("id")
        function = call.get("function") or {}
    else:
        call_id = getattr(call, "id", None)
        function = getattr(call, "function", None) or {}
    if isinstance(function, dict):
        return call_id, function.get("name"), function.get("arguments")
    return call_id, getattr(function, "name", None), getattr(function, "arguments", None)


def _tool_result_id(message):
    if isinstance(message, dict):
        return message.get("tool_call_id")
    return getattr(message, "tool_call_id", None)


def _payload_fingerprint(messages: list) -> str:
    """역할과 도구 id 시퀀스만 남긴 마스킹 구조 지문.

    실패를 서버·템플릿 문제와 클라이언트 payload 문제로 가르려면 구조가 필요하다.
    내용과 실제 id는 남기지 않는다.
    """
    aliases = {}

    def alias(call_id):
        if not isinstance(call_id, str) or not call_id:
            return "t?"
        return aliases.setdefault(call_id, "t{0}".format(len(aliases) + 1))

    parts = []
    for message in messages:
        role = _msg_role(message) or "?"
        calls = _msg_tool_calls(message)
        if role == "assistant" and calls:
            parts.append(
                "assistant[" + ",".join(alias(_call_parts(call)[0]) for call in calls) + "]"
            )
        elif role == "tool":
            parts.append("tool:" + alias(_tool_result_id(message)))
        else:
            parts.append(role)
    return "|".join(parts)


def validate_chat_payload(messages: list) -> SimpleNamespace:
    """전송 직전 payload의 구조를 검증하고, 결함을 제거한 payload를 함께 돌려준다.

    호출 수와 결과 수가 같다는 것은 유효성 근거가 되지 못한다. 같은 id를 두 번
    알리거나 알리지 않은 id에 답하는 payload도 개수는 맞는다. 그래서 id 유일성,
    호출-결과 1:1, 그룹 인접성, system 위치를 한 곳에서 함께 본다. 결함이 없으면
    메시지를 그대로 두므로 정상 이력은 복구가 건드리지 않는다.
    """
    if not messages:
        return SimpleNamespace(messages=list(messages), defects=(), ok=True)

    defects = set()

    # 1) 알림 수집. id는 비어 있지 않은 문자열이고 payload 전체에서 유일해야 한다.
    announced = {}
    kept_calls = {}
    for position, message in enumerate(messages):
        if _msg_role(message) != "assistant":
            continue
        calls = _msg_tool_calls(message)
        if not calls:
            continue
        surviving = []
        for call in calls:
            call_id, name, arguments = _call_parts(call)
            if not isinstance(call_id, str) or not call_id:
                defects.add("tool_call_id_missing")
                continue
            if call_id in announced:
                defects.add("tool_call_id_duplicate")
                continue
            if not isinstance(name, str) or not name:
                defects.add("tool_name_missing")
                continue
            if not isinstance(arguments, str):
                defects.add("tool_arguments_not_string")
                try:
                    arguments = json.dumps(arguments, ensure_ascii=False)
                except Exception:
                    arguments = str(arguments)
            announced[call_id] = position
            surviving.append({
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": arguments},
            })
        kept_calls[position] = surviving

    # 2) 결과 수집. 알린 id 하나에 결과 하나. 고아 결과는 본문을 잃지 않도록
    #    user 메시지로 내리고, 내용이 문자열이 아닌 결과는 문자열로 채운다.
    answers = {}
    answer_positions = {}
    demoted_orphans = {}
    for position, message in enumerate(messages):
        if _msg_role(message) != "tool":
            continue
        call_id = _tool_result_id(message)
        if not isinstance(call_id, str) or call_id not in announced:
            defects.add("tool_result_orphan")
            demoted_orphans[position] = {
                "role": "user",
                "content": "[도구 실행 결과: {0}]\n{1}".format(
                    _msg_name(message), _msg_content(message)
                ),
            }
            continue
        if call_id in answers:
            defects.add("tool_result_duplicate")
            continue
        if isinstance(message, dict):
            content = message.get("content")
        else:
            content = getattr(message, "content", None)
        if not isinstance(content, str):
            defects.add("tool_content_missing")
            repaired_result = dict(message) if isinstance(message, dict) else {
                "role": "tool", "tool_call_id": call_id, "name": _msg_name(message)
            }
            repaired_result["content"] = TOOL_PAYLOAD_MISSING_RESULT
            answers[call_id] = repaired_result
        else:
            answers[call_id] = message
        answer_positions[call_id] = position

    # 3) 재조립. 결과는 자기 assistant 알림 바로 뒤에, 알린 순서대로만 놓인다.
    repaired = []
    for position, message in enumerate(messages):
        role = _msg_role(message)
        if role == "tool":
            if position in demoted_orphans:
                repaired.append(demoted_orphans[position])
            continue
        if role == "system" and repaired:
            # 0번이 아닌 system은 chat template이 거부하므로 user로 내린다.
            defects.add("system_position")
            repaired.append({
                "role": "user",
                "content": "[시스템 참고 정보]: {0}".format(_msg_content(message)),
            })
            continue
        calls = kept_calls.get(position)
        if calls is None:
            repaired.append(message)
            continue
        if calls == list(_msg_tool_calls(message)):
            repaired.append(message)
        elif calls:
            repaired.append({
                "role": "assistant",
                "content": _msg_content(message) or None,
                "tool_calls": calls,
            })
        else:
            repaired.append({
                "role": "assistant",
                "content": _msg_content(message) or "[도구 실행 지시]",
            })

        group_positions = [
            answer_positions[call["id"]]
            for call in calls
            if call["id"] in answer_positions
        ]
        if group_positions and (
            group_positions != sorted(group_positions)
            or set(range(position + 1, max(group_positions) + 1)) - set(group_positions)
        ):
            defects.add("tool_group_split")
        for call in calls:
            answer = answers.get(call["id"])
            if answer is None:
                defects.add("tool_call_unanswered")
                answer = {
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "name": call["function"]["name"],
                    "content": TOOL_PAYLOAD_MISSING_RESULT,
                }
            repaired.append(answer)

    if not defects:
        # 결함이 없으면 메시지는 그대로 두고 목록만 새로 만든다. 호출자가 받은
        # payload가 이후에 append되는 살아 있는 이력과 같은 객체이면 안 된다.
        return SimpleNamespace(messages=list(messages), defects=(), ok=True)
    return SimpleNamespace(messages=repaired, defects=tuple(sorted(defects)), ok=False)


def is_tool_correlation_error(error) -> bool:
    """도구 호출-결과 상관관계 오류인지 판정한다.

    예외 문자열에 400이 있다는 사실만으로는 원인이 되지 못한다. 문맥 길이 초과와
    파라미터 오류도 같은 상태 코드를 쓰므로, 그런 실패까지 도구 프로토콜 제거로
    처리하면 근거 없이 대화의 도구 이력을 지운다.
    """
    text = str(error).lower()
    return any(marker in text for marker in TOOL_CORRELATION_ERROR_MARKERS)


def flatten_tool_protocol(messages: list) -> list:
    """도구 프로토콜을 지운 payload.

    로컬 검증을 통과한 payload에도 서버가 상관관계 오류를 낼 때 남는 마지막
    수단이다. 결과 본문은 user 메시지로 옮겨 정보를 잃지 않는다.
    """
    flattened = []
    for message in messages:
        role = _msg_role(message)
        if role == "tool":
            flattened.append({
                "role": "user",
                "content": "[도구 실행 결과: {0}]\n{1}".format(
                    _msg_name(message), _msg_content(message)
                ),
            })
        elif role == "assistant" and _msg_tool_calls(message):
            flattened.append({
                "role": "assistant",
                "content": _msg_content(message) or "[도구 실행 지시]",
            })
        else:
            flattened.append(message)
    return flattened

# --- Workspace Skills & Reusable Tools Architecture ---

def _extract_skill_description(content: str, ext: str) -> str:
    content = str(content or "").strip()
    if not content:
        return "스크립트 도구"

    for line in content.splitlines():
        line = line.strip()
        m = re.match(r"^#\s*(?:description|설명)\s*:\s*(.+)$", line, re.IGNORECASE)
        if m:
            return _clip_summary_text(m.group(1).strip(), 120)

    if ext == ".py":
        doc_m = re.search(r'^(?:#[^\n]*\n)*\s*(?:"""|\'\'\')(.*?)(?:"""|\'\'\')', content, re.DOTALL)
        if doc_m:
            doc = " ".join(doc_m.group(1).strip().split())
            if doc:
                return _clip_summary_text(doc, 120)

    if ext == ".md":
        for line in content.splitlines():
            line = line.strip()
            if line.startswith("#"):
                clean = line.lstrip("#").strip()
                if clean:
                    return _clip_summary_text(clean, 120)

    for line in content.splitlines():
        line = line.strip()
        if line.startswith("#") and not line.startswith("#!"):
            clean = line.lstrip("#").strip()
            if clean:
                return _clip_summary_text(clean, 120)

    if ext == ".py":
        return "Python 스크립트 도구"
    elif ext in (".sh", ".bash"):
        return "Shell 스크립트 도구"
    elif ext == ".md":
        return "스킬 가이드 문서"
    return "작업 공간 도구"


def discover_workspace_skills(workspace) -> list:
    target_dir = os.path.join(workspace.root, "skills")
    if not os.path.isdir(target_dir):
        return []
    skills = []
    try:
        entries = sorted(os.listdir(target_dir))
    except Exception:
        return []
    for entry in entries:
        if entry.startswith(".") or entry.startswith("_"):
            continue
        full_path = os.path.join(target_dir, entry)
        if not os.path.isfile(full_path):
            continue
        ext = os.path.splitext(entry)[1].lower()
        if ext not in (".py", ".sh", ".bash", ".md"):
            continue
        try:
            size = os.path.getsize(full_path)
            with open(full_path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read(2048)
        except Exception:
            continue
        desc = _extract_skill_description(content, ext)
        skill_type = "python" if ext == ".py" else ("shell" if ext in (".sh", ".bash") else "markdown")
        skills.append({
            "name": f"skills/{entry}",
            "path": full_path,
            "type": skill_type,
            "description": desc,
            "size": size,
        })
    return skills


def render_skills_block(workspace) -> str:
    skills = discover_workspace_skills(workspace)
    header = "[재사용 가능한 작업 공간 스킬 (Workspace Skills)]"
    if not skills:
        return (
            f"{header}\n"
            "(현재 등록된 스킬 없음. 복잡하거나 반복되는 작업은 write_file을 사용해 "
            "skills/<name>.py 또는 .sh로 스크립트화하여 저장하고 bash_exec로 재사용하세요.)"
        )
    lines = [header]
    for s in skills:
        lines.append(f"- `{s['name']}`: {s['description']} ({s['size']}B)")
    lines.append(
        "* 실행 방법: bash_exec(command=\"python3 skills/<name>.py ...\") 또는 "
        "bash_exec(command=\"bash skills/<name>.sh ...\")\n"
        "* 새 스킬 생성: write_file로 skills/<name>.py 또는 .sh를 작성하고 재사용하세요."
    )
    return "\n".join(lines)


# --- Hierarchical Trajectory & Multi-level Compaction ---

MILESTONES_SECTION_HEADER = "## 🏛️ 장기 마일스톤 색인"
RECENT_PHASE_SECTION_HEADER = "## 🔍 직전 구간 상세 요약"
ARTIFACTS_SECTION_HEADER = "## 📁 핵심 발견 및 산출물 색인"


def format_hierarchical_summary(
    milestones: list = None,
    recent_summary: str = "",
    discoveries: list = None,
) -> str:
    sections = []
    milestone_lines = [str(m).strip() for m in (milestones or []) if str(m).strip()]
    milestone_content = "\n".join(milestone_lines) if milestone_lines else "(초기 탐색 단계 - 이전 마일스톤 없음)"
    sections.append(f"{MILESTONES_SECTION_HEADER}\n{milestone_content}")

    recent_text = str(recent_summary or "").strip()
    sections.append(f"{RECENT_PHASE_SECTION_HEADER}\n{recent_text or '(진행 중인 세부 작업 없음)'}")

    discovery_lines = [str(d).strip() for d in (discoveries or []) if str(d).strip()]
    if discovery_lines:
        sections.append(f"{ARTIFACTS_SECTION_HEADER}\n" + "\n".join(discovery_lines))

    return "\n\n".join(sections)


def parse_hierarchical_summary(text: str) -> dict:
    text = str(text or "").strip()
    if not text:
        return {"milestones": [], "recent_summary": "", "discoveries": []}

    milestones = []
    recent_summary = ""
    discoveries = []

    if MILESTONES_SECTION_HEADER not in text and RECENT_PHASE_SECTION_HEADER not in text:
        return {"milestones": [], "recent_summary": text, "discoveries": []}

    curr_section = None
    curr_lines = []

    for line in text.splitlines():
        trimmed = line.strip()
        if trimmed.startswith(MILESTONES_SECTION_HEADER):
            if curr_section == "recent":
                recent_summary = "\n".join(curr_lines).strip()
            elif curr_section == "artifacts":
                discoveries.extend([l for l in curr_lines if l.strip()])
            curr_section = "milestones"
            curr_lines = []
        elif trimmed.startswith(RECENT_PHASE_SECTION_HEADER):
            if curr_section == "milestones":
                milestones.extend([l for l in curr_lines if l.strip() and not l.strip().startswith("(초기")])
            elif curr_section == "artifacts":
                discoveries.extend([l for l in curr_lines if l.strip()])
            curr_section = "recent"
            curr_lines = []
        elif trimmed.startswith(ARTIFACTS_SECTION_HEADER):
            if curr_section == "milestones":
                milestones.extend([l for l in curr_lines if l.strip() and not l.strip().startswith("(초기")])
            elif curr_section == "recent":
                recent_summary = "\n".join(curr_lines).strip()
            curr_section = "artifacts"
            curr_lines = []
        else:
            curr_lines.append(line)

    if curr_section == "milestones":
        milestones.extend([l for l in curr_lines if l.strip() and not l.strip().startswith("(초기")])
    elif curr_section == "recent":
        recent_summary = "\n".join(curr_lines).strip()
    elif curr_section == "artifacts":
        discoveries.extend([l for l in curr_lines if l.strip()])

    return {
        "milestones": milestones,
        "recent_summary": recent_summary,
        "discoveries": discoveries,
    }


def update_hierarchical_summary(
    existing_summary: str,
    new_recent_summary: str,
    step_range: str = "",
    discoveries: list = None,
) -> str:
    parsed = parse_hierarchical_summary(existing_summary)
    old_milestones = list(parsed["milestones"])
    old_recent = parsed["recent_summary"].strip()

    if old_recent and not old_recent.startswith("(진행") and not old_recent.startswith("(초기"):
        condensed = _clip_summary_text(old_recent.replace("\n", " "), 250)
        prefix = f"• 구간 ({step_range}): " if step_range else "• 이전 구간: "
        if not any(condensed in m for m in old_milestones):
            old_milestones.append(f"{prefix}{condensed}")

    merged_discoveries = list(parsed["discoveries"])
    for d in (discoveries or []):
        if d not in merged_discoveries:
            merged_discoveries.append(d)

    return format_hierarchical_summary(
        milestones=old_milestones,
        recent_summary=new_recent_summary,
        discoveries=merged_discoveries,
    )


def extract_discovered_artifacts(text: str, workspace) -> list:
    artifacts = []
    for m in re.finditer(r"(`(?:skills/[a-zA-Z0-9_.-]+|findings\.md|plan\.md|[a-zA-Z0-9_.-]+\.(?:py|sh|json|md|txt))`|https?://[^\s)\]]+)", text):
        found = m.group(1).strip()
        item = f"- 참조/산출물: {found}"
        if item not in artifacts:
            artifacts.append(item)
    skills = discover_workspace_skills(workspace)
    for s in skills:
        skill_item = f"- 스킬: `{s['name']}` ({s['description']})"
        if skill_item not in artifacts:
            artifacts.append(skill_item)
    return artifacts[:10]


# --- User's Rolling Compaction (Rollup) Architecture ---

ROLLING_SUMMARY_LABEL = "누적 작업 요약 및 이전 대화 컨텍스트"
STATE_UPDATE_BLOCK_PATTERN = re.compile(r"```state_update\s*(.*?)(?:```|$)", re.DOTALL)


def build_system_content(workspace, ledger=None, summary: str = "") -> str:
    """Compose message 0 for one explicit run workspace.

    The state block goes last and message 0 sits before the first tool
    message, so every request, checkpoint and rollover sees the same authoritative state.
    """
    parts = [SYSTEM_PROMPT_TEMPLATE.format(workspace_root=workspace.root)]
    skills_block = render_skills_block(workspace)
    if skills_block:
        parts.append(skills_block)
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
        raw = match.group(1).strip()
        parsed = _robust_json_loads(raw)
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


# --- Durable run state boundaries (issue #6) ---

# ponytail: tail은 메시지 12개·메시지당 2000자로 자른다. 복원한 런은 그보다
# 오래된 도구 결과 원문을 다시 보지 못하고 누적 요약만 본다. 더 긴 tail이
# 필요하다면 상한을 올리기 전에 요약 품질을 먼저 봐야 한다.
SNAPSHOT_TAIL_MESSAGES = 12
SNAPSHOT_TAIL_CHARS = 2000


def _tool_call_id(call):
    if isinstance(call, dict):
        return call.get("id")
    return getattr(call, "id", None)


def _snapshot_message(message) -> dict:
    """Reduce one payload message to a bounded JSON-safe record."""
    record = {"role": _msg_role(message)}
    raw = message.get("content") if isinstance(message, dict) else getattr(message, "content", None)
    # assistant 도구 지시는 content가 의도적으로 None이다. 빈 문자열로 바꾸면
    # 같은 메시지가 다른 종류가 된다.
    record["content"] = (
        None if raw is None else _clip_summary_text(str(raw), SNAPSHOT_TAIL_CHARS)
    )
    for key in ("name", "tool_call_id"):
        value = message.get(key) if isinstance(message, dict) else getattr(message, key, None)
        if value:
            record[key] = str(value)
    calls = _msg_tool_calls(message)
    if calls:
        record["tool_calls"] = []
        for call in calls:
            function = (call.get("function") or {}) if isinstance(call, dict) else {}
            record["tool_calls"].append({
                "id": str(_tool_call_id(call) or ""),
                "type": "function",
                "function": {
                    "name": str(function.get("name") or "tool"),
                    "arguments": _clip_summary_text(
                        str(function.get("arguments") or ""), SNAPSHOT_TAIL_CHARS
                    ),
                },
            })
    return record


def snapshot_tail(messages: list, max_messages: int = None) -> list:
    """Bounded tail that only ever holds whole assistant/tool groups.

    A save boundary must never cut across a parallel call/result group. Results
    without their instruction are rejected by the chat template, and an
    instruction without its results would make a resumed run dispatch tool calls
    whose side effects already happened - so either half is dropped with the
    other.
    """
    if max_messages is None:
        max_messages = SNAPSHOT_TAIL_MESSAGES
    # 0번 system 메시지는 매 스텝 원장과 요약으로 새로 만들어지므로 저장하지 않는다.
    tail = [message for message in messages if _msg_role(message) != "system"]
    tail = tail[-max_messages:]
    while tail and _msg_role(tail[0]) == "tool":
        tail.pop(0)
    for index in range(len(tail) - 1, -1, -1):
        calls = _msg_tool_calls(tail[index])
        if not calls:
            continue
        settled = {
            (item.get("tool_call_id") if isinstance(item, dict) else None)
            for item in tail[index + 1:]
            if _msg_role(item) == "tool"
        }
        if any(_tool_call_id(call) not in settled for call in calls):
            tail = tail[:index]
        # 결과가 붙지 않은 그룹은 마지막 그룹뿐이다. 그 앞의 그룹들은 다음 지시가
        # 실리기 전에 결과가 모두 채워졌다.
        break
    return [_snapshot_message(message) for message in tail]


def recover_interrupted_runs() -> dict:
    """Settle every unterminated record once, at startup.

    Each one either becomes the selection the channel's next goal consumes - so
    the same run id continues at its next cursor - or gets exactly one explicit
    abort. There is deliberately no third path: silently falling back to Step 1
    is what made a restart indistinguishable from a new request (issue #6).
    """
    aborted = 0

    def abort(workspace, reason):
        run_state.discard(workspace)
        log_session_event(workspace, "run_abort", status="aborted", reason=reason)

    candidates = {}
    for workspace in RUN_CATALOG.workspaces():
        record = run_state.load(workspace)
        if record is None:
            # 읽을 수 없거나 스키마가 다른 레코드는 마이그레이션하지 않고 버린다.
            # 남겨 두면 시작마다 같은 abort를 다시 남긴다.
            if run_state.discard(workspace):
                log_session_event(
                    workspace, "run_abort", status="aborted", reason="unusable_record"
                )
                aborted += 1
            continue
        if record["state"] != run_state.RUNNING:
            # 종료 이벤트를 남긴 런이다. 명시적 !resume 대상으로는 남지만 자동
            # 복구 대상은 아니다.
            continue
        key = (workspace.owner_id, workspace.channel_id)
        previous = candidates.get(key)
        if previous is None:
            candidates[key] = (workspace, record)
            continue
        # 한 채널은 한 번에 하나만 이어갈 수 있다. 최신 레코드가 이기고, 밀린
        # 쪽은 조용히 남지 않고 abort로 종결된다.
        stale, winner = previous, (workspace, record)
        if str(previous[1].get("updated_at", "")) > str(record.get("updated_at", "")):
            stale, winner = winner, previous
        abort(stale[0], "superseded_by_newer_run")
        aborted += 1
        candidates[key] = winner

    recovered = 0
    for workspace, record in candidates.values():
        try:
            RUN_CATALOG.resume(workspace.owner_id, workspace.channel_id, workspace.run_id)
        except (RunNotFoundError, RunActiveError):
            abort(workspace, "not_resumable")
            aborted += 1
            continue
        log_session_event(
            workspace,
            "run_resume_armed",
            step=record["next_step"],
            next_step=record["next_step"],
            tail_msgs=len(record["tail"]),
            calls=len(record["executed_call_ids"]),
        )
        recovered += 1
    return {"recovered": recovered, "aborted": aborted}


async def rollover_agent_context(workspace, messages: list, existing_summary: str, step_num: int, ledger=None, token=None):
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
        log_session_event(workspace, "rollover_skipped", step=step_num, reason="no_new_source")
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

    start_step = max(1, step_num - ROLLING_COMPACTION_INTERVAL + 1)
    step_range = f"Step {start_step}-{step_num}"
    discoveries = extract_discovered_artifacts(
        source + "\n" + (existing_summary or ""), workspace
    )

    summary_prompt = (
        "이것은 장시간 자율 에이전트의 다단계 계층형 컨텍스트 압축 작업입니다.\n"
        "이전 원본 대화를 그대로 반복하지 말고, 다음 에이전트가 작업을 이어갈 수 있도록 "
        "아래 3개 섹션 구조로 명확하게 작성하세요:\n\n"
        f"1. `{MILESTONES_SECTION_HEADER}`: 이전 구간들의 핵심 결정, 반증된 가설, 주요 마일스톤을 간결한 불릿 포인트(•)로 요약\n"
        f"2. `{RECENT_PHASE_SECTION_HEADER}`: 이번 구간({step_range})의 구체적인 도구 실행 결과, 확인된 사실/수치/에러, 다음 할 일 상세 기술\n"
        f"3. `{ARTIFACTS_SECTION_HEADER}`: 생성/수정한 파일(`plan.md`, `findings.md`, `skills/...`), 확인한 URL, 주요 명령어 목록\n\n"
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
        log_session_event(
            workspace, "rollover_timeout", step=step_num, stage=compaction_timeout.stage
        )
    except Exception as compaction_error:
        log_session_event(
            workspace,
            "rollover_error",
            step=step_num,
            error=type(compaction_error).__name__,
        )

    validation_notes = [validation_notes_prefix] if validation_notes_prefix else []
    if new_summary and existing_summary and new_summary == existing_summary.strip():
        # The source had new content but the compactor echoed the old summary.
        # Accepting it would freeze the state at its previous revision.
        validation_notes.append("압축기가 기존 요약을 그대로 반환하여 거부했습니다.")
        new_summary = ""

    if not new_summary:
        new_summary = update_hierarchical_summary(
            existing_summary=existing_summary,
            new_recent_summary=source,
            step_range=step_range,
            discoveries=discoveries,
        )
        validation_notes.append("결정적 폴백 계층 요약(기존 요약 + 원본 기록)을 사용했습니다.")
    else:
        if MILESTONES_SECTION_HEADER not in new_summary and RECENT_PHASE_SECTION_HEADER not in new_summary:
            new_summary = update_hierarchical_summary(
                existing_summary=existing_summary,
                new_recent_summary=new_summary,
                step_range=step_range,
                discoveries=discoveries,
            )

    new_summary = _clip_summary_text(new_summary, ROLLING_SUMMARY_MAX_CHARS)
    dropped_markers = missing_state_markers(new_summary, required_markers)
    if dropped_markers:
        validation_notes.append("누락된 상태 마커를 권위 있는 상태 블록으로 보정했습니다: " + ", ".join(dropped_markers))
        new_summary = f"{state_block}\n\n{new_summary}".strip()

    # 400 Chat template error 완벽 방지: 단일 시스템 프롬프트로 병합
    replaced_messages = [
        {"role": "system", "content": build_system_content(workspace, ledger, new_summary)},
        {
            "role": "user",
            "content": "[롤링 컨텍스트 재개] 위 요약과 권위 있는 조사 상태를 기준으로 최근 도구 실행 결과를 반영하고 다음 작업을 계속하세요.",
        },
    ]
    replaced_messages.extend(recent_messages)
    replaced_messages = validate_chat_payload(replaced_messages).messages

    before_chars = sum(len(_msg_content(msg)) for msg in messages)
    after_chars = sum(len(_msg_content(msg)) for msg in replaced_messages)
    log_session_event(
        workspace,
        "rollover",
        step=step_num,
        msgs_before=len(messages),
        msgs_after=len(replaced_messages),
        chars_before=before_chars,
        chars_after=after_chars,
        kept_tool_msgs=KEEP_RECENT_TOOL_MESSAGES,
        summary_chars=len(new_summary),
        validation="; ".join(validation_notes) if validation_notes else "pass",
    )
    # 압축된 요약 본문은 명시적 opt-in 싱크에만 남는다. 기본 배포에서는 이 줄이
    # 아무것도 쓰지 않는다.
    log_content_debug(workspace, "rollover_summary", new_summary, step=step_num)
    return replaced_messages, new_summary

# --- User's Streaming Completion Collector ---

# 대체 id는 completion 하나가 아니라 대화 전체에서 유일해야 한다. completion마다
# 0으로 되돌아가는 카운터는 서로 다른 스텝에서 같은 call_stream_0을 만들고, 그
# 둘이 한 payload에 같이 실리면 tool_call_id가 중복된다.
_fallback_call_serial = 0


def _next_fallback_call_id() -> str:
    global _fallback_call_serial
    _fallback_call_serial += 1
    return "call_stream_{0}".format(_fallback_call_serial)


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
    # HTTPX's phase-specific connect timeout governs DNS/TCP/TLS. Awaiting the
    # SDK here also includes response headers, which belong to read/total time.
    stream = await client.chat.completions.create(stream=True, **kwargs)
    content_parts = []
    reasoning_parts = []
    tool_buffers = []
    buffers_by_index = {}
    current = None

    async for chunk in stream_chunks(stream, stage, CONFIG.idle_timeout, token):
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
            call_id = getattr(partial, "id", None)
            function = getattr(partial, "function", None)
            name = getattr(function, "name", None) if function is not None else None
            arguments = getattr(function, "arguments", None) if function is not None else None

            if index is not None:
                # int 0과 "0"을 같은 호출로 본다. 정렬 대신 도착 순서를 유지하므로
                # 백엔드가 두 표기를 섞어도 순서 비교가 필요 없다.
                buf = buffers_by_index.get(str(index))
                if buf is None:
                    buf = {"id": "", "name": "", "arguments": ""}
                    buffers_by_index[str(index)] = buf
                    tool_buffers.append(buf)
                current = buf
            elif current is None or (call_id and name):
                # ponytail: index 없는 스트림의 호출 경계는 "id와 이름을 함께 들고
                # 온 조각이 새 호출"이라는 규칙 하나로만 잡는다(천장). id와 이름을
                # 서로 다른 조각으로 나눠 보내는 백엔드는 앞 호출에 병합되고, 그
                # 결과는 인자 파싱에서 걸려 실행 전에 거부된다. 그런 백엔드가
                # 생기면 index를 요구하는 쪽으로 올린다.
                current = {"id": "", "name": "", "arguments": ""}
                tool_buffers.append(current)

            # 조각난 id는 이어 붙여야 한다. 매 조각에 전체 id를 다시 보내는
            # 백엔드에서 같은 값을 두 번 붙이지 않도록 꼬리 중복만 건너뛴다.
            if call_id and not current["id"].endswith(call_id):
                current["id"] += call_id
            if name:
                current["name"] += name
            if arguments:
                current["arguments"] += (
                    arguments if isinstance(arguments, str) else str(arguments)
                )

    tool_calls = []
    for buf in tool_buffers:
        if not buf["name"]:
            # 이름 없는 버퍼는 재구성 실패다. 어떤 도구를 부르려 했는지 알 수 없어
            # 모델에 돌려줄 정정 근거도 없으므로 도구 실행 전에 거부한다.
            # (파싱 불가한 인자는 호출 직전 argument_error 경로가 이미 거부한다.)
            print(
                "[Streaming tool call refused: 이름 없는 조각]",
                file=sys.stderr,
                flush=True,
            )
            continue
        tool_calls.append(
            SimpleNamespace(
                id=buf["id"] or _next_fallback_call_id(),
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


DISCORD_CHUNK_MAX_CHARS = 1900
LOCAL_FALLBACK_MAX_CHUNKS = 3
LOCAL_FALLBACK_MAX_CHARS = DISCORD_CHUNK_MAX_CHARS * LOCAL_FALLBACK_MAX_CHUNKS
LOCAL_FALLBACK_OMISSION_MARKER = (
    "\n\n[중간 상세 내용 생략 — 전체 원장은 내부 상태에 유지됨]\n\n"
)


def get_open_code_fence(text: str) -> Optional[str]:
    """Return the opening fence string if the text ends inside a code block, else None."""
    open_fence = None
    for line in text.split("\n"):
        stripped = line.strip()
        if open_fence is None:
            match = re.match(r"^(`{3,}|~{3,})(\S*)", stripped)
            if match:
                f_chars = match.group(1)
                rest = stripped[match.end():]
                if f_chars not in rest:
                    open_fence = match.group(0)
        else:
            c_chars = open_fence[:3]
            match = re.match(r"^(`{3,}|~{3,})\s*$", stripped)
            if match and match.group(1).startswith(c_chars) and len(match.group(1)) >= len(c_chars):
                open_fence = None
    return open_fence


def split_markdown_chunks(text: str, max_chars: int = DISCORD_CHUNK_MAX_CHARS) -> List[str]:
    """Split text into Discord chunks while preserving and balancing markdown code fences."""
    text = str(text or "")
    if not text:
        return [""]
    if len(text) <= max_chars:
        open_fence = get_open_code_fence(text)
        if open_fence:
            return [text + "\n" + open_fence[:3]]
        return [text]

    lines = text.split("\n")
    chunks = []
    current_lines = []
    current_len = 0
    active_fence = None

    def close_fence_str(fence: Optional[str]) -> str:
        return fence[:3] if fence else "```"

    for line in lines:
        stripped = line.strip()
        is_opening_fence = False
        is_closing_fence = False
        new_fence = None

        if active_fence is None:
            match = re.match(r"^(`{3,}|~{3,})(\S*)", stripped)
            if match:
                f_chars = match.group(1)
                rest = stripped[match.end():]
                if f_chars not in rest:
                    is_opening_fence = True
                    new_fence = match.group(0)
        else:
            c_chars = close_fence_str(active_fence)
            match = re.match(r"^(`{3,}|~{3,})\s*$", stripped)
            if match and match.group(1).startswith(c_chars) and len(match.group(1)) >= len(c_chars):
                is_closing_fence = True

        line_cost = len(line) + (1 if current_lines else 0)
        fence_overhead = (1 + len(close_fence_str(active_fence))) if active_fence is not None else 0

        if current_lines and (current_len + line_cost + fence_overhead > max_chars):
            chunk_text = "\n".join(current_lines)
            if active_fence is not None:
                chunk_text += "\n" + close_fence_str(active_fence)
            chunks.append(chunk_text)

            current_lines = []
            current_len = 0
            if active_fence is not None:
                current_lines.append(active_fence)
                current_len = len(active_fence)

        avail = max_chars - current_len - (1 if current_lines else 0) - fence_overhead
        if len(line) > avail and len(line) > (max_chars // 2):
            if current_lines:
                chunk_text = "\n".join(current_lines)
                if active_fence is not None:
                    chunk_text += "\n" + close_fence_str(active_fence)
                chunks.append(chunk_text)
                current_lines = []
                current_len = 0
                if active_fence is not None:
                    current_lines.append(active_fence)
                    current_len = len(active_fence)

            rem_line = line
            while len(rem_line) > (max_chars - (1 + len(close_fence_str(active_fence)) if active_fence else 0)):
                chunk_budget = max_chars - current_len - (1 if current_lines else 0) - (
                    (1 + len(close_fence_str(active_fence))) if active_fence else 0
                )
                if chunk_budget <= 100:
                    chunk_text = "\n".join(current_lines)
                    if active_fence is not None:
                        chunk_text += "\n" + close_fence_str(active_fence)
                    chunks.append(chunk_text)
                    current_lines = []
                    current_len = 0
                    if active_fence is not None:
                        current_lines.append(active_fence)
                        current_len = len(active_fence)
                    chunk_budget = max_chars - current_len - (1 if current_lines else 0) - (
                        (1 + len(close_fence_str(active_fence))) if active_fence else 0
                    )

                split_at = rem_line.rfind(" ", 0, chunk_budget)
                if split_at == -1 or split_at < chunk_budget // 3:
                    split_at = chunk_budget

                piece = rem_line[:split_at]
                rem_line = rem_line[split_at:].lstrip(" ")
                current_lines.append(piece)
                chunk_text = "\n".join(current_lines)
                if active_fence is not None:
                    chunk_text += "\n" + close_fence_str(active_fence)
                chunks.append(chunk_text)
                current_lines = []
                current_len = 0
                if active_fence is not None:
                    current_lines.append(active_fence)
                    current_len = len(active_fence)

            line = rem_line

        if line or not current_lines:
            if current_lines:
                current_len += 1 + len(line)
            else:
                current_len += len(line)
            current_lines.append(line)

        if is_opening_fence:
            active_fence = new_fence
        elif is_closing_fence:
            active_fence = None

    if current_lines:
        chunk_text = "\n".join(current_lines)
        if active_fence is not None:
            chunk_text += "\n" + close_fence_str(active_fence)
        chunks.append(chunk_text)

    return chunks if chunks else [text]


def bound_local_fallback_output(text: str) -> str:
    """Bound user-facing fallback without mutating its authoritative sources."""
    text = str(text or "")
    if len(text) <= LOCAL_FALLBACK_MAX_CHARS:
        return text
    available = LOCAL_FALLBACK_MAX_CHARS - len(LOCAL_FALLBACK_OMISSION_MARKER) - 64
    head_chars = available // 2
    tail_chars = available - head_chars

    head_cut = text.rfind("\n", 0, head_chars)
    if head_cut == -1 or head_cut < head_chars // 2:
        head_cut = head_chars
    head = text[:head_cut]

    head_fence = get_open_code_fence(head)
    if head_fence:
        head += "\n" + head_fence[:3]

    tail_start = len(text) - tail_chars
    tail_cut = text.find("\n", tail_start)
    if tail_cut == -1 or tail_cut > tail_start + (tail_chars // 2):
        tail_cut = tail_start
    else:
        tail_cut += 1
    tail = text[tail_cut:]

    prefix_fence = get_open_code_fence(text[:tail_cut])
    if prefix_fence:
        tail = prefix_fence + "\n" + tail

    tail_fence = get_open_code_fence(tail)
    if tail_fence:
        tail += "\n" + tail_fence[:3]

    return (
        head
        + LOCAL_FALLBACK_OMISSION_MARKER
        + tail
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
        # 추론 원문은 채널로 나가지 않는다. 여기서 렌더링하면 로그를 정리해도
        # 같은 내용이 디스코드에 그대로 남는다(이슈 #11).
        thought_chars = len(think_match.group(1).strip())
        t = re.sub(r"<think>.*?(?:</think>|$)", "", t, flags=re.DOTALL).strip()
        t = re.sub(r"</?(function|parameter|tool_call)[^>]*>", "", t, flags=re.DOTALL).strip()
        if not t:
            # 추론만 나온 응답. 원문 대신 제한된 상태 표현으로 진행 상황을 알린다.
            return (
                f"🧠 **[내부 추론만 반환됨]** 공개할 본문이 없습니다 (추론 {thought_chars}자). "
                "다시 요청하거나 목표를 더 구체적으로 지정해 주세요."
            )

    return t.strip()

@bot.event
async def on_ready():
    log_session_event(
        None,
        "ready",
        user=str(bot.user),
        user_id=getattr(bot.user, "id", None),
        run_root=str(RUN_CATALOG.runs_root),
    )
    await bot.change_presence(activity=discord.Game(name="Qwen 27B + Auto-Extension"), status=discord.Status.online)

# --- Slash Commands ---
# 슬래시 명령도 텍스트 명령과 동일한 정책 경로(authorize_caller)를 사용한다.

async def deny_interaction(interaction: discord.Interaction, action: str, decision) -> None:
    log_session_event(
        None,
        "access_denied",
        action=action,
        reason=decision.reason,
        user=getattr(interaction.user, "id", None),
        channel=interaction.channel_id,
        via="slash",
    )
    # DENY_ACCESS_MESSAGE already carries the ⛔ marker; the per-decision reasons do
    # not. Prefixing both, as this used to, double-marked every access denial. The
    # text command path at the bottom of on_message follows the same split.
    if action == authz.ACCESS:
        message = authz.DENY_ACCESS_MESSAGE
    else:
        message = f"⛔ {decision.reason}"
    await interaction.response.send_message(message, ephemeral=True)


def interaction_can_manage_messages(interaction: discord.Interaction) -> bool:
    # Interaction.permissions is the caller's own resolved channel permission and
    # has existed since discord.py 2.0, below the floor requirements.txt sets.
    return bool(interaction.permissions.manage_messages)


def prepare_new_run(owner_id, channel_id):
    workspace = RUN_CATALOG.prepare(owner_id, channel_id)
    clear_channel_state(channel_id)
    return workspace


def resume_run(owner_id, channel_id, run_id):
    return RUN_CATALOG.resume(owner_id, channel_id, run_id)


def delete_run(owner_id, run_id):
    RUN_CATALOG.delete(owner_id, run_id)


async def _slash_prepare_run(interaction):
    owner_id = getattr(interaction.user, "id", None)
    decision = authorize_caller(
        authz.CONTROL, owner_id, channel_id=interaction.channel_id
    )
    if not decision:
        await deny_interaction(interaction, authz.CONTROL, decision)
        return
    try:
        workspace = prepare_new_run(owner_id, interaction.channel_id)
    except RunActiveError:
        await interaction.response.send_message(
            "An active run must stop before reset/new.", ephemeral=True
        )
        return
    await interaction.response.send_message(
        f"🧹 대화 상태를 초기화하고 새 run `{workspace.run_id}`을 다음 목표로 선택했습니다."
    )


@bot.tree.command(name="reset", description="대화 기록을 초기화하고 새 run을 준비합니다.")
async def slash_reset(interaction: discord.Interaction):
    await _slash_prepare_run(interaction)


@bot.tree.command(name="new", description="대화 기록을 초기화하고 새 run을 준비합니다.")
async def slash_new(interaction: discord.Interaction):
    await _slash_prepare_run(interaction)


@bot.tree.command(name="resume", description="소유한 run을 다음 목표에서 재개합니다.")
async def slash_resume(interaction: discord.Interaction, run_id: str):
    owner_id = getattr(interaction.user, "id", None)
    decision = authorize_caller(
        authz.CONTROL, owner_id, channel_id=interaction.channel_id
    )
    if not decision:
        await deny_interaction(interaction, authz.CONTROL, decision)
        return
    try:
        workspace = resume_run(owner_id, interaction.channel_id, run_id)
    except RunNotFoundError:
        await interaction.response.send_message("run not found", ephemeral=True)
        return
    except RunActiveError:
        await interaction.response.send_message("run is active", ephemeral=True)
        return
    await interaction.response.send_message(
        f"▶️ run `{workspace.run_id}`을 다음 목표로 선택했습니다."
    )


@bot.tree.command(name="delete", description="소유한 비활성 run을 삭제합니다.")
async def slash_delete(interaction: discord.Interaction, run_id: str):
    owner_id = getattr(interaction.user, "id", None)
    decision = authorize_caller(
        authz.CONTROL, owner_id, channel_id=interaction.channel_id
    )
    if not decision:
        await deny_interaction(interaction, authz.CONTROL, decision)
        return
    try:
        delete_run(owner_id, run_id)
    except RunNotFoundError:
        await interaction.response.send_message("run not found", ephemeral=True)
        return
    except RunActiveError:
        await interaction.response.send_message("run is active", ephemeral=True)
        return
    await interaction.response.send_message(f"🗑️ run `{run_id}`을 삭제했습니다.")

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
    owner_id = getattr(interaction.user, "id", None)
    try:
        reservation = RUN_CATALOG.reserve_reset(owner_id, interaction.channel_id)
    except RunActiveError:
        await interaction.response.send_message(
            "An active run must stop before clear.", ephemeral=True
        )
        return

    amt = min(max(1, count), 100)
    purged = False
    try:
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
        purged = True
    finally:
        if not purged:
            RUN_CATALOG.cancel_reset(
                owner_id, interaction.channel_id, reservation
            )

    workspace = RUN_CATALOG.prepare_reserved(
        owner_id, interaction.channel_id, reservation
    )
    clear_channel_state(interaction.channel_id)
    await interaction.followup.send(
        f"🧹 최근 메시지 {len(deleted)}개와 대화 상태를 삭제하고 새 run `{workspace.run_id}`을 선택했습니다.",
        ephemeral=True,
    )

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
        log_session_event(
            None,
            "access_denied",
            action=authz.ACCESS,
            reason=access.reason,
            user=caller_id,
            channel=message.channel.id,
            via="text",
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
        try:
            workspace = prepare_new_run(caller_id, message.channel.id)
        except RunActiveError:
            await message.reply("An active run must stop before reset/new.")
            return
        await message.reply(
            f"🧹 대화 상태를 초기화하고 새 run `{workspace.run_id}`을 다음 목표로 선택했습니다."
        )
        return

    if cmd_name in ["!resume", "/resume"]:
        control = authorize_caller(authz.CONTROL, caller_id, channel_id=message.channel.id)
        if not control:
            await message.reply(f"⛔ {control.reason}")
            return
        if len(parts) != 2:
            await message.reply("사용법: `!resume <run-id>`")
            return
        try:
            workspace = resume_run(caller_id, message.channel.id, parts[1])
        except RunNotFoundError:
            await message.reply("run not found")
            return
        except RunActiveError:
            await message.reply("run is active")
            return
        await message.reply(f"▶️ run `{workspace.run_id}`을 다음 목표로 선택했습니다.")
        return

    if cmd_name in ["!delete", "/delete"]:
        control = authorize_caller(authz.CONTROL, caller_id, channel_id=message.channel.id)
        if not control:
            await message.reply(f"⛔ {control.reason}")
            return
        if len(parts) != 2:
            await message.reply("사용법: `!delete <run-id>`")
            return
        try:
            delete_run(caller_id, parts[1])
        except RunNotFoundError:
            await message.reply("run not found")
            return
        except RunActiveError:
            await message.reply("run is active")
            return
        await message.reply(f"🗑️ run `{parts[1]}`을 삭제했습니다.")
        return

    if cmd_name in ["!clear", "!purge", "!삭제", "!청소"]:
        purge = authorize_caller(
            authz.PURGE,
            caller_id,
            channel_id=message.channel.id,
            caller_can_manage_messages=caller_can_manage_messages(message.channel, message.author),
        )
        if not purge:
            log_session_event(
                None,
                "access_denied",
                action=authz.PURGE,
                reason=purge.reason,
                user=caller_id,
                channel=message.channel.id,
                via="text",
            )
            await message.reply(f"⛔ {purge.reason}")
            return
        amount = 50
        if len(parts) > 1:
            try:
                amount = int(parts[1])
            except ValueError:
                pass
        amount = min(max(1, amount), 100)

        try:
            reservation = RUN_CATALOG.reserve_reset(caller_id, message.channel.id)
        except RunActiveError:
            await message.reply("An active run must stop before clear.")
            return

        log_session_event(
            None, "purge_requested", channel=message.channel.id, count=amount
        )

        # 삭제를 먼저 시도하고, 성공한 뒤에만 대화 상태를 지운다. 예전에는 순서가
        # 반대여서 권한이 없어 삭제가 실패해도 기록은 이미 사라져 있었다.
        purged = False
        try:
            try:
                deleted = await message.channel.purge(limit=amount + 1)
            except discord.Forbidden:
                await message.channel.send("⚠️ **권한 부족**: 봇에게 서버의 **'메시지 관리 (Manage Messages)'** 권한이 필요합니다. 대화 기록은 유지되었습니다.")
                return
            except Exception as e:
                await message.channel.send(f"⚠️ 메시지 삭제 실패: `{e}` 대화 기록은 유지되었습니다.")
                return
            purged = True
        finally:
            if not purged:
                RUN_CATALOG.cancel_reset(
                    caller_id, message.channel.id, reservation
                )

        workspace = RUN_CATALOG.prepare_reserved(
            caller_id, message.channel.id, reservation
        )
        clear_channel_state(message.channel.id)
        del_count = max(0, len(deleted) - 1)
        notice = await message.channel.send(
            f"🧹 최근 메시지 {del_count}개와 대화 상태를 삭제하고 새 run `{workspace.run_id}`을 선택했습니다."
        )
        await asyncio.sleep(3)
        try:
            await notice.delete()
        except Exception:
            pass
        return

    if content.startswith("!"):
        return

    # [실시간 동적 개입 큐 주입]
    if channel_active_runs[message.channel.id]:
        control = authorize_caller(authz.CONTROL, caller_id, channel_id=message.channel.id)
        if not control:
            await message.reply(f"⛔ {control.reason}")
            return
        active_lease = next(
            candidate
            for candidate in reversed(channel_run_leases[message.channel.id])
            if candidate["active"]
        )
        mailbox = active_lease["steering"]
        steering_workspace = active_lease["workspace"]
        receipt = mailbox.offer(str(message.author), content)
        # 지시 본문은 세지기만 한다. 이 줄이 원문을 실었던 것이 로그에 사용자
        # 텍스트가 쌓인 경로였다(이슈 #11).
        log_session_event(
            steering_workspace,
            "steering_received",
            status=receipt.state,
            reason=receipt.reason or None,
            depth=receipt.depth,
            chars=len(content),
        )
        log_content_debug(steering_workspace, "steering_text", content)
        # 큐 관측은 내용 없이 남긴다. 깊이와 접수/적용 시각만으로도 반영 지연을
        # 판별할 수 있고, 지시 본문은 이 줄에 들어가지 않는다.
        log_session_event(
            steering_workspace, "steering_queue", queue=mailbox.stats()
        )

        if not receipt.accepted:
            # 미적용 안내는 자동 삭제하지 않는다. 사용자가 반드시 봐야 한다.
            await message.reply(steering_receipt_notice(receipt, content))
            return

        try:
            await message.add_reaction("📥")
        except Exception:
            pass

        notice = await message.reply(steering_receipt_notice(receipt, content))
        asyncio.create_task(_auto_delete_notice(notice, delay=6))
        return

    start_time = time.time()
    token = CancelToken()
    try:
        workspace = RUN_CATALOG.acquire(caller_id, message.channel.id)
    except RunActiveError:
        await message.reply(
            "A reset/clear operation is in progress; retry this goal."
        )
        return
    # 재시작 전에 남은 durable 레코드. 있으면 이 런은 같은 런 id로 다음 커서에서
    # 이어간다. 신규 런에는 레코드가 없으므로 평소처럼 Step 1부터다.
    restored = run_state.load(workspace)
    resume_from = restored["next_step"] if restored is not None else 1
    same_origin = (
        restored is not None
        and restored.get("message_id") == getattr(message, "id", None)
    )
    # 승인과 메일박스를 한 턴에 함께 소유한다. 이 지점부터 첫 await까지 사이가
    # 없으므로 같은 채널의 다음 메시지는 언제나 steering으로 판정된다. 실패 시
    # 아래 status 응답 실패 경로의 release_run()이 승인을 되돌린다.
    lease = {
        "token": token,
        "owner": caller_id,
        "active": True,
        "workspace": workspace,
        "steering": steering_mod.SteeringMailbox(
            workspace.run_id, max_depth=STEERING_QUEUE_MAX
        ),
    }
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

    # 종료 사유는 런 하나당 하나다. 직접 답변 빠른 경로도 같은 것을 쓴다.
    # 예전에는 그 경로만 RunOutcome을 만들지 않아, 사유를 남기지 않는 유일한
    # 종료 경로였다.
    outcome = RunOutcome()
    total_tools_executed = 0
    current_step = 0
    # 이미 실행한 호출 식별자. 재시작 뒤 모델이 같은 호출을 다시 요청해도 부작용을
    # 두 번 일으키지 않는다.
    executed_call_ids = list(restored["executed_call_ids"]) if restored is not None else []
    run_end_logged = False
    released = False

    def log_run_end(run_outcome, status=None, abnormal=False, **fields):
        """종료 이벤트를 정확히 한 번 남긴다.

        정상 성공·강제 종료·예외 전 중단을 사후에 구분할 수 있어야 하므로 모든
        종료 경로가 이 하나를 지난다. 멱등이라 상세를 아는 경로가 먼저 부르고
        release_run()이 뒤에서 다시 불러도 두 번 남지 않는다.

        `status`는 예외 경로 때문에 따로 받는다. settle()은 선착순이라 이미
        완료로 확정된 런이 전달 실패로 끝나면 reason이 completed로 남지만, 그
        경로는 완료된 조사를 전달하지 못했으므로 종료 기록은 실패여야 한다.
        """
        nonlocal run_end_logged
        if run_end_logged:
            return
        run_end_logged = True
        ignored = [
            f"{reason}:{detail}" for reason, detail in run_outcome.ignored_attempts
        ]
        log_session_event(
            workspace,
            "run_end",
            step=current_step,
            status=status or run_outcome.reason or outcome_mod.FAILED,
            duration_ms=int((time.time() - start_time) * 1000),
            detail=run_outcome.detail or "-",
            abnormal=bool(abnormal),
            tools=total_tools_executed,
            # 선착순에 밀린 종료 시도는 지금까지 기록되지 않았다. 완료 뒤에 온
            # 실패가 어떤 것이었는지는 이 필드에만 남는다.
            ignored_settles=ignored or None,
            **fields,
        )

    def release_run(status="interrupted"):
        nonlocal released
        if released:
            return
        released = True
        # 종료 기록의 최후 보장. 상세를 아는 경로가 이미 남겼으면 무시된다.
        log_run_end(outcome)
        # 런이 끝나면 반영할 스텝도 없다. 남은 항목을 여기서 종결시켜야 어떤
        # 항목도 상태 없이 남지 않는다(정상 경로는 루프 직후에 이미 닫는다).
        lease["steering"].close(steering_mod.CANCELLED)
        # 종료한 런은 자동 복구 대상이 아니다. 레코드 자체는 남겨 명시적 !resume은
        # 계속 가능하게 하고, 삭제는 !reset/!clear/!delete가 맡는다.
        run_state.terminate(workspace, status)
        try:
            RUN_CATALOG.finish(workspace, status)
        finally:
            leases = channel_run_leases.get(message.channel.id, [])
            leases[:] = [candidate for candidate in leases if candidate is not lease]
            publish_run_control()

    async def send_reply_chunks(text, local_fallback=False):
        if local_fallback:
            text = bound_local_fallback_output(text)
        chunks = split_markdown_chunks(text, max_chars=DISCORD_CHUNK_MAX_CHARS)
        await message.reply(chunks[0])
        for chunk in chunks[1:]:
            await message.channel.send(chunk)

    wants_short_answer = wants_direct_response(content)
    if restored is not None:
        # 이어갈 런이 있으면 단문 빠른 경로로 빠지지 않는다. 그 경로는 즉시
        # 종료하므로, 복구한 런을 조용히 버리는 것과 같다.
        wants_short_answer = False
    log_session_event(
        workspace,
        "run_start",
        goal_chars=len(content),
        direct=wants_short_answer,
        effort=channel_reasoning[message.channel.id],
    )
    log_content_debug(workspace, "user_goal", content)

    if restored is not None:
        # 재개는 조용히 일어나지 않는다. 이 레코드가 없으면 Step 기록만으로는
        # 재시작 때문인지 새 요청 때문인지 구분할 수 없다.
        log_session_event(
            workspace,
            "run_resumed",
            step=resume_from,
            next_step=resume_from,
            same_origin=same_origin,
            tail_msgs=len(restored["tail"]),
            calls=len(executed_call_ids),
            summary_chars=len(restored["summary"]),
        )
        # 재시작으로 비어 있던 채널 메모리를 레코드의 값으로 되돌린다.
        channel_summary[message.channel.id] = restored["summary"]
        channel_ledger[message.channel.id] = restored["ledger"]
        try:
            await message.channel.send(
                f"▶️ **[중단된 실행 재개]** run `{workspace.run_id}`을 Step {resume_from}에서 "
                f"이어갑니다. 이미 실행한 도구 {len(executed_call_ids)}건은 다시 실행하지 않습니다."
            )
        except Exception:
            pass

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
        # 병합이지 교체가 아니다. 예전의 `=`는 계층 요약과 그 안에 실린 상태
        # 마커를 스니펫으로 덮어써서, 복원한 요약이 첫 긴 대화에서 사라졌다.
        channel_summary[message.channel.id] = update_hierarchical_summary(
            existing_summary=channel_summary[message.channel.id],
            new_recent_summary="이전 대화 요약: " + " | ".join(summary_snippets[-8:]),
            step_range="대화 이력 초과분",
        )
        channel_history[message.channel.id] = recent_turns
        history = recent_turns

    direct_call_failed = False
    if wants_short_answer:
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
            outcome.settle(outcome_mod.STOPPED, direct_cancelled.reason)
            direct_report = build_incomplete_report(
                outcome,
                channel_ledger[message.channel.id],
                channel_summary[message.channel.id],
                history,
            )
            try:
                await send_reply_chunks(
                    f"**{outcome.label}**\n\n{direct_report}",
                    local_fallback=True,
                )
                log_run_end(outcome, phase="direct")
            finally:
                release_run("stopped")
            return
        except StageTimeout as direct_timeout:
            outcome.settle(
                outcome_mod.FAILED,
                f"마감 초과: {direct_timeout.stage} {direct_timeout.seconds:g}s",
            )
            direct_report = build_incomplete_report(
                outcome,
                channel_ledger[message.channel.id],
                channel_summary[message.channel.id],
                history,
            )
            try:
                await send_reply_chunks(
                    f"**{outcome.label}**\n\n{direct_report}",
                    local_fallback=True,
                )
                log_run_end(outcome, phase="direct")
            finally:
                release_run("failed")
            return
        except asyncio.CancelledError:
            release_run("interrupted")
            raise
        except Exception as direct_error:
            direct_call_failed = True
            log_session_event(
                workspace,
                "direct_fallback",
                status="error",
                error=type(direct_error).__name__,
            )
        else:
            if direct_text:
                try:
                    await send_reply_chunks(direct_text)
                    history.append({"role": "assistant", "content": direct_text})
                    # 이 경로는 종료 사유 없이 stdout 한 줄만 남기고 돌아갔다.
                    # 사유를 갖는 종료 기록을 남기는 유일한 방법은 다른 경로와
                    # 같은 outcome을 확정하는 것이다.
                    outcome.settle(
                        outcome_mod.COMPLETED, outcome_mod.DETAIL_DIRECT_ANSWER
                    )
                    log_run_end(outcome, phase="direct", chars=len(direct_text))
                    log_content_debug(workspace, "direct_answer", direct_text)
                except asyncio.CancelledError:
                    release_run("interrupted")
                    raise
                except Exception:
                    release_run("failed")
                    raise
                else:
                    release_run("completed")
                return
            direct_call_failed = True

    # 400 Chat template error 방지: 0번째에 단일 시스템 프롬프트로 병합
    ledger = channel_ledger[message.channel.id]
    rolling_summary = channel_summary[message.channel.id]

    messages_payload = [
        {"role": "system", "content": build_system_content(workspace, ledger, rolling_summary)}
    ]
    if restored is not None:
        # 복원한 tail은 완결된 그룹만 담으므로 그대로 이어 붙일 수 있다. 이전 대화
        # 턴까지 다시 붙이지는 않는다: tail과 누적 요약이 이미 담고 있어 같은 맥락이
        # 두 번 실린다.
        messages_payload.extend(restored["tail"])
        if not same_origin:
            # 같은 원본 메시지가 다시 배달된 경우 목표는 이미 tail 안에 있다.
            messages_payload.append(history[-1])
    else:
        messages_payload.extend(history)
    messages_payload = validate_chat_payload(messages_payload).messages

    current_effort = channel_reasoning[message.channel.id]

    def save_snapshot(next_step, reason):
        """완결된 그룹 경계에서만 부르는 원자적 durable 저장.

        원자적 교체이므로 중단이 파일을 자를 수 없고, tail은 항상 완결된
        assistant/tool 그룹만 담는다(snapshot_tail이 구조적으로 보장한다).
        """
        tail = snapshot_tail(messages_payload)
        try:
            run_state.save(
                workspace,
                message_id=getattr(message, "id", None),
                next_step=next_step,
                summary=rolling_summary,
                tail=tail,
                ledger=ledger,
                interrupt={
                    "cancelled": bool(token.cancelled),
                    "reason": token.reason or "",
                    "steering": lease["steering"].stats(),
                },
                executed_call_ids=executed_call_ids,
            )
        except OSError as snapshot_error:
            # 저장 실패가 런을 죽이지는 않는다. 다만 조용히 넘어가지도 않는다:
            # 이 시점 이후 이 런은 복구 불가능하다는 뜻이다.
            log_session_event(
                workspace,
                "snapshot_failed",
                step=current_step,
                reason=reason,
                error=type(snapshot_error).__name__,
            )
            return
        log_session_event(
            workspace,
            "snapshot",
            step=current_step,
            reason=reason,
            next_step=next_step,
            tail_msgs=len(tail),
            summary_chars=len(rolling_summary),
            calls=len(executed_call_ids),
        )

    # 여기부터 에이전트 루프가 보장되므로 지시를 받는다. 직접 답변 런은 이 지점에
    # 오지 않으므로 큐가 닫힌 상태로 남고, 반영할 스텝이 없다는 사실이 접수
    # 단계에서 그대로 통지된다.
    lease["steering"].open()
    # 첫 모델 호출 전에 레코드를 남긴다. 이것이 없으면 첫 스텝에서 죽은 런은
    # 흔적 없이 사라지고, 다음 요청이 조용히 Step 1을 다시 낸다.
    save_snapshot(resume_from, "run_start")
    try:
        status_msg = await message.reply(f"🚀 **[완전자율 목표 달성 모드]** 모델 초기 추론 및 작업 공간 가동 중... (실시간 지시/개입 가능 / 중단: `!stop`)")
    except BaseException:
        release_run()
        raise

    def apply_steering(step_num: int):
        """대기 중인 지시를 도착 순서대로 흡수한다.

        루프 머리에서만 소비하면 스텝 하나가 긴 동안 접수된 지시가 모델 호출과
        도구 실행을 모두 기다린다. 도구 결과 직후에도 불러서 체크포인트·롤오버
        모델 단계까지 기다리지 않게 한다.
        """
        applied = lease["steering"].drain()
        for item in applied:
            steering_block = {
                "role": "user",
                "content": f"💬 [사용자({item.author}) 실시간 추가 지침/피드백]:\n{item.text}\n\n(이 지침을 바탕으로 현재 작업 방향을 적절히 조정하거나, 요청받은 내용을 우선 처리하세요.)"
            }
            messages_payload.append(steering_block)
            history.append(steering_block)
            log_session_event(
                workspace,
                "steering_applied",
                step=step_num,
                chars=len(item.text),
                waited_ms=int(max(0.0, time.time() - item.received_at) * 1000),
            )
        if applied:
            log_session_event(
                workspace,
                "steering_queue",
                step=step_num,
                queue=lease["steering"].stats(),
            )
        return applied

    async def close_steering():
        """종료 단계에 들어갈 때 큐를 닫고 미적용 사실을 알린다.

        닫은 뒤 도착하는 지시는 접수 시점에 거절되고, 이미 대기 중이던 항목은
        여기서 취소로 종결된다. 예전에는 이 구간에서도 접수와 "다음 스텝 반영"
        안내가 나갔지만 소비할 반복이 없어 항목이 조용히 사라졌다.
        """
        unapplied = lease["steering"].close(steering_mod.CANCELLED)
        log_session_event(
            workspace,
            "steering_closed",
            step=current_step,
            unapplied=len(unapplied),
            queue=lease["steering"].stats(),
        )
        if not unapplied:
            return
        try:
            await message.channel.send(
                f"📭 **[실시간 개입 미적용]** 대기 중이던 지시 {len(unapplied)}건은 "
                "런이 종료되어 **적용되지 않았습니다**. 새 목표 요청으로 다시 보내주세요."
            )
        except Exception:
            pass

    last_failed_signature = None
    consecutive_failed_tool_calls = 0

    async def maybe_roll_context(step_num: int):
        nonlocal messages_payload, rolling_summary
        if step_num % ROLLING_COMPACTION_INTERVAL != 0:
            return
        messages_payload, rolling_summary = await rollover_agent_context(
            workspace,
            messages_payload,
            rolling_summary,
            step_num,
            ledger=ledger,
            token=token,
        )
        # 롤오버는 누적 요약이 바뀌는 유일한 지점이다. 되돌려 쓰지 않으면 이 런의
        # 모든 롤오버 요약이 함수 종료와 함께 사라지고, 같은 프로세스의 다음
        # 메시지조차 낡은 요약에서 시작한다.
        channel_summary[message.channel.id] = rolling_summary
        save_snapshot(step_num + 1, "rollover")

    # 상시 타이핑 하트비트 루프 백그라운드 구동
    stop_typing = asyncio.Event()
    typing_task = asyncio.create_task(keep_typing_heartbeat(message.channel, stop_typing))

    final_raw = ""
    refused_companion_calls = []
    # 업스트림 예외 본문은 사용자에게는 필요하고 로그에는 위험하다. 종료 사유는
    # 종류만 담아 로그로 가고, 잘라낸 본문은 사용자 보고서에만 붙는다(이슈 #11).
    stage_failure_note = ""

    def settle_stage_failure(error):
        nonlocal stage_failure_note
        if isinstance(error, RunCancelled):
            outcome.settle(outcome_mod.STOPPED, error.reason)
        elif isinstance(error, StageTimeout):
            outcome.settle(
                outcome_mod.FAILED,
                f"마감 초과: {error.stage} {error.seconds:g}s",
            )
        else:
            stage_failure_note = _clip_summary_text(
                f"{type(error).__name__}: {error}", 500
            )
            outcome.settle(
                outcome_mod.FAILED,
                f"업스트림 실패: {type(error).__name__}",
            )

    try:
        # 다음 커서에서 시작한다. 복원한 런은 Step 1을 다시 내지 않고, 신규 런은
        # resume_from이 1이라 종전과 같다.
        for iteration in range(resume_from - 1, MAX_AGENT_LOOPS):
            current_step = iteration + 1
            if token.cancelled:
                outcome.settle(outcome_mod.STOPPED, token.reason)
                break

            # [실시간 사용자 동적 개입 주입]
            applied_steering = apply_steering(iteration + 1)
            if applied_steering:
                # 지시 본문은 되돌려 인용하지 않는다. 사용자가 방금 입력한 것이라
                # 잃는 정보가 없고, 인용하면 채널에 원문이 한 번 더 남는다.
                try:
                    await status_msg.edit(content=f"🛠️ **[Step {iteration+1}/{MAX_AGENT_LOOPS}]** 💬 **사용자 실시간 지시사항 {len(applied_steering)}건 반영 중...**")
                except Exception:
                    pass

            extra_params = {}
            if iteration == 0:
                extra_params["reasoning_effort"] = "none"
            elif current_effort and current_effort != "none":
                extra_params["reasoning_effort"] = current_effort

            # 권위 있는 조사 상태를 매 스텝 0번 메시지에 재고정한다.
            if messages_payload and _msg_role(messages_payload[0]) == "system":
                messages_payload[0] = {
                    "role": "system",
                    "content": build_system_content(workspace, ledger, rolling_summary),
                }

            # 구조 결함은 이번 요청만의 문제가 아니다. 복구 결과를 messages_payload에
            # 되돌려야 다음 스텝이 같은 결함을 다시 만들어 같은 400을 반복하지 않는다.
            base_verdict = validate_chat_payload(messages_payload)
            if not base_verdict.ok:
                messages_payload = base_verdict.messages
                log_session_event(
                    workspace,
                    "payload_repaired",
                    step=iteration + 1,
                    defects=list(base_verdict.defects),
                    fingerprint=_payload_fingerprint(messages_payload),
                )

            # 불변(Append-only) 컨텍스트 보존: 중간 텍스트를 변조하지 않아 Prefix Cache(KV Cache) HIT를 극대화한다.
            compacted_payload = validate_chat_payload(messages_payload).messages
            model_stage_deadline = time.monotonic() + CONFIG.model_stage_timeout
            model_stage_started = time.monotonic()

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
                # 실패 원인을 구조로 남긴다. 마스킹된 역할/id 시퀀스와 실행 리비전이
                # 있어야 서버·템플릿 문제와 클라이언트 payload 문제를 구분할 수 있다.
                correlation = is_tool_correlation_error(api_err)
                log_session_event(
                    workspace,
                    "model_stage_failure",
                    step=iteration + 1,
                    revision=ledger.revision,
                    error=type(api_err).__name__,
                    failure_class="tool_correlation" if correlation else "other",
                    cause="client" if base_verdict.defects else "server",
                    defects=list(base_verdict.defects) or None,
                    fingerprint=_payload_fingerprint(compacted_payload),
                )
                if not correlation:
                    settle_stage_failure(api_err)
                    break

                # 로컬 검증을 통과한 payload에도 상관관계 오류가 나면 남는 수단은
                # 도구 프로토콜 제거뿐이다. 이 결과도 messages_payload에 남긴다.
                messages_payload = flatten_tool_protocol(messages_payload)
                save_snapshot(iteration + 1, "tool_protocol_recovery")
                retry_payload = validate_chat_payload(messages_payload).messages

                # Never restart a stage after cancellation.
                try:
                    token.raise_if_cancelled()
                    # 도구 이력을 지운 payload에 도구를 다시 제시하면 모델이 같은
                    # 오류로 되돌아간다. 재시도는 한 번, 도구 없이 한다.
                    resp = await run_completion_stage(
                        token=token,
                        stage="agent:retry",
                        deadline=model_stage_deadline,
                        model=MODEL_NAME,
                        messages=retry_payload,
                        max_tokens=1024 if iteration == 0 else 4096,
                        temperature=0.7,
                        **extra_params
                    )
                except (RunCancelled, StageTimeout) as stage_error:
                    settle_stage_failure(stage_error)
                    break
                except Exception as retry_error:
                    settle_stage_failure(retry_error)
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

            log_session_event(
                workspace,
                "model_response",
                step=iteration + 1,
                duration_ms=int((time.monotonic() - model_stage_started) * 1000),
                reasoning_chars=len(reasoning_text),
                content_chars=len(content_text),
                tool_calls=len(msg.tool_calls or []),
                effort=extra_params.get("reasoning_effort", current_effort),
            )
            # 추론과 응답 원문은 opt-in 싱크에만 간다. 기본 배포에서는 아무것도
            # 쓰지 않는다.
            log_content_debug(
                workspace, "model_response", full_raw_thought, step=iteration + 1
            )

            # [실시간 진행 상황 디스코드 카드 라이브 업데이트]
            # 추론 스니펫은 더 이상 싣지 않는다. 로그에서 원문을 지워도 같은
            # 내용이 채널에 남으면 아무것도 해결되지 않는다(이슈 #11).
            elapsed_live = format_elapsed_time(time.time() - start_time)
            status_live_text = (
                f"🤖 **[Qwen 자율 에이전트 실시간 대시보드]**\n"
                f"> 🔄 **진행 상태**: `Step {iteration+1}/{MAX_AGENT_LOOPS}` (경과: `{elapsed_live}` | 실행 도구: `{total_tools_executed}개`)\n"
                f"> 🧠 **실시간 추론 규모**: 추론 `{len(reasoning_text)}자` / 본문 `{len(content_text)}자` (추론 원문은 공개하지 않습니다)\n"
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
                    raw_value = tc.function.arguments
                    if isinstance(raw_value, str):
                        raw_arguments = raw_value
                        try:
                            args = json.loads(raw_value, strict=False)
                        except Exception:
                            args = _robust_json_loads(raw_value)
                            if args is None:
                                argument_error = _invalid_tool_arguments_result(
                                    "malformed_json", tc.function.name
                                )
                            else:
                                argument_error = None
                        else:
                            argument_error = None
                    else:
                        args = raw_value
                        try:
                            raw_arguments = json.dumps(raw_value, ensure_ascii=False)
                        except Exception:
                            raw_arguments = str(raw_value)
                        argument_error = None
                    if argument_error is None and not isinstance(args, dict):
                        argument_error = _invalid_tool_arguments_result(
                            "arguments_not_object", tc.function.name
                        )
                    tool_calls_to_run.append({
                        "id": tc.id,
                        "name": tc.function.name,
                        "arguments": args,
                        "raw_arguments": raw_arguments,
                        "argument_error": argument_error,
                    })

            if not tool_calls_to_run and content_text:
                extracted = extract_tool_calls_from_text(content_text)
                for i, e in enumerate(extracted):
                    args = e["arguments"]
                    tool_calls_to_run.append({
                        "id": f"call_xml_{iteration}_{i}",
                        "name": e["name"],
                        "arguments": args,
                        "raw_arguments": json.dumps(args, ensure_ascii=False),
                        "argument_error": (
                            None
                            if isinstance(args, dict)
                            else _invalid_tool_arguments_result(
                                "arguments_not_object", e["name"]
                            )
                        ),
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
            finish_calls = [
                tc for tc in tool_calls_to_run
                if tc["name"] == "finish_task" and tc["argument_error"] is None
            ]
            if finish_calls:
                final_completed_report = finish_calls[0]["arguments"].get("report", "")
                companions = [tc["name"] for tc in tool_calls_to_run if tc is not finish_calls[0]]
                if companions:
                    refused_companion_calls = companions
                    log_session_event(
                        workspace,
                        "finish_companions_refused",
                        step=iteration + 1,
                        tools=companions,
                    )
                outcome.settle(outcome_mod.COMPLETED, outcome_mod.DETAIL_FINISH_TASK)
                final_raw = final_completed_report or full_raw_thought
                break

            if tool_calls_to_run:
                synthetic_tool_calls = [
                    {
                        "id": tc["id"],
                        "type": "function",
                        "function": {
                            "name": tc["name"],
                            "arguments": tc["raw_arguments"],
                        }
                    }
                    for tc in tool_calls_to_run
                ]

                elapsed_live = format_elapsed_time(time.time() - start_time)
                tools_display = ", ".join([f"`{tc['name']}`" for tc in tool_calls_to_run[:3]])
                tool_live_text = (
                    f"🤖 **[Qwen 자율 에이전트 실시간 대시보드]**\n"
                    f"> 🔄 **진행 상태**: `Step {iteration+1}/{MAX_AGENT_LOOPS}` (경과: `{elapsed_live}` | 현재까지 실행: `{total_tools_executed}개`)\n"
                    f"> 🛠️ **요청 도구**: {tools_display}\n"
                    f"> 💭 **판단 규모**: 추론 `{len(reasoning_text)}자` / 본문 `{len(content_text)}자`\n"
                    f"> ⚡ *도구 요청 검토 중... (실시간 지시 가능 / 중단: `!stop`)*"
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
                    if tc["argument_error"] is not None:
                        merged_results[call_index] = tc["argument_error"]
                        continue
                    if tc["id"] in executed_call_ids:
                        # 재시작 이전에 이미 실행한 호출이다. 다시 보내면 같은
                        # 부작용이 두 번 일어난다.
                        merged_results[call_index] = _blocked_tool_result(
                            "already_executed", tc["name"], 1, 1
                        )
                        continue
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
                        batch_signatures.add(signature)
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
                for tc in allowed_calls:
                    executed_call_ids.append(tc["id"])
                    # 인자 원문 대신 어떤 인자가 왔는지만 남긴다. 도구 인자 JSON을
                    # 그대로 적는 것이 셸 명령과 파일 내용이 로그로 들어온 경로였다.
                    log_session_event(
                        workspace,
                        "tool_call",
                        step=iteration + 1,
                        tool=tc["name"],
                        arg_keys=sorted(tc["arguments"].keys()),
                        args_chars=len(tc["raw_arguments"] or ""),
                    )
                    log_content_debug(
                        workspace,
                        "tool_arguments",
                        tc["raw_arguments"],
                        step=iteration + 1,
                    )

                parallel_results = []
                if allowed_calls:
                    tool_batch_started = time.monotonic()
                    try:
                        parallel_results = await execute_tools_in_parallel(
                            workspace,
                            allowed_calls,
                            step_num=iteration+1,
                            ledger=ledger,
                            token=token,
                        )
                    except (RunCancelled, StageTimeout) as stage_error:
                        settle_stage_failure(stage_error)
                        break
                    except Exception as tool_error:
                        settle_stage_failure(tool_error)
                        break
                    # ponytail: 배치 단위 소요만 남긴다. 병렬 실행이라 어느 도구가
                    # 오래 걸렸는지는 구분되지 않는다. 도구별 소요가 필요하면
                    # _exec_single이 결과와 함께 시간을 돌려주도록 확장한다.
                    log_session_event(
                        workspace,
                        "tool_batch",
                        step=iteration + 1,
                        duration_ms=int((time.monotonic() - tool_batch_started) * 1000),
                        count=len(allowed_calls),
                        tools=[tc["name"] for tc in allowed_calls],
                    )

                messages_payload.append({
                    "role": "assistant",
                    "content": content_text or None,
                    "tool_calls": synthetic_tool_calls,
                })
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
                    if not isinstance(tool_result, str):
                        tool_result = TOOL_PAYLOAD_MISSING_RESULT
                    # 도구 결과 원문 대신 실패 여부와 규모만 남긴다. 명령 출력,
                    # 파일 내용, HTTP 응답이 이 줄로 들어오던 것을 끊는다.
                    log_session_event(
                        workspace,
                        "tool_result",
                        step=iteration + 1,
                        tool=tc["name"],
                        status="error" if _tool_result_failed(tc["name"], tool_result) else "ok",
                        result_chars=len(tool_result or ""),
                    )
                    log_content_debug(
                        workspace, "tool_result", tool_result, step=iteration + 1
                    )

                    messages_payload.append({
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "name": tc["name"],
                        "content": tool_result
                    })

                # 도구 실행 중에 접수된 지시를 여기서 흡수한다. 다음 루프 머리까지
                # 미루면 체크포인트 보고서와 롤오버 모델 단계를 모두 기다린다.
                apply_steering(iteration + 1)

                # 그룹이 완결됐다: 모든 호출에 결과가 붙었고 대기 지시도 흡수됐다.
                # 저장 경계는 여기이며, 병렬 호출/결과 그룹 중간이 아니다.
                save_snapshot(iteration + 2, "tool_group")

                # [매 30스텝 도달 시 중간 진행 보고서 자동 발행 및 자율 연속 연장]
                # 이 보고서는 사용자용 진행 브리핑이며 복구 지점이 아니다. 복구에
                # 쓰이는 것은 바로 위 save_snapshot이 남긴 durable 레코드다.
                if (iteration + 1) % CHECKPOINT_INTERVAL == 0 and (iteration + 1) < MAX_AGENT_LOOPS and not token.cancelled:
                    checkpoint_num = (iteration + 1) // CHECKPOINT_INTERVAL
                    log_session_event(
                        workspace,
                        "checkpoint_start",
                        step=iteration + 1,
                        checkpoint=checkpoint_num,
                    )
                    try:
                        await status_msg.edit(content=f"📊 **[Step {iteration+1} 중간 보고 구간 도달]** 중간 진행 상황 종합 보고서 작성 및 다음 구간 자동 연장 준비 중... ▌")
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
                            max_tokens=4096,
                            temperature=0.4,
                            reasoning_effort="none"
                        )
                        inter_text = inter_resp.choices[0].message.content or ""

                        # 보고서에만 있던 정정을 롤오버 이전에 권위 있는 상태로 반영한다.
                        state_updates, inter_text = parse_state_update_blocks(inter_text)
                        for update_payload in state_updates:
                            state_report = ledger.apply_updates(update_payload)
                            log_session_event(
                                workspace,
                                "checkpoint_state_update",
                                step=iteration + 1,
                                report_chars=len(state_report),
                            )
                            log_content_debug(
                                workspace,
                                "checkpoint_state_update",
                                state_report,
                                step=iteration + 1,
                            )
                        # 정정이 반영된 직후 저장한다. 다음 구간에서 죽어도 잃는
                        # 것은 한 구간뿐이고, 정정은 남는다.
                        save_snapshot(iteration + 2, "interim_report")

                        inter_formatted = format_full_discord_output(inter_text)

                        elapsed_checkpoint = time.time() - start_time
                        elapsed_cp_str = format_elapsed_time(elapsed_checkpoint)

                        cp_message = (
                            f"📊 **[중간 진행 보고서 - {iteration+1}스텝]**\n\n"
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

                        log_session_event(
                            workspace,
                            "checkpoint_report",
                            step=iteration + 1,
                            status="ok",
                            checkpoint=checkpoint_num,
                            chars=len(inter_text),
                            chunks=len(chunks_cp),
                        )
                        log_content_debug(
                            workspace, "checkpoint_report", inter_text, step=iteration + 1
                        )
                        checkpoint_ok = True

                    except (RunCancelled, StageTimeout) as stage_error:
                        settle_stage_failure(stage_error)
                        break
                    except Exception as cp_err:
                        log_session_event(
                            workspace,
                            "checkpoint_report",
                            step=iteration + 1,
                            status="error",
                            checkpoint=checkpoint_num,
                            error=type(cp_err).__name__,
                        )

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
                        # 실패한 중간 보고서에 성공 마커를 남기지 않는다.
                        messages_payload.append({
                            "role": "assistant",
                            "content": f"[중간 보고서 생성 실패 - Step {iteration+1} 중간 보고서는 제출되지 않았습니다]",
                        })
                        messages_payload.append({"role": "user", "content": (
                            f"[🤖 시스템 안내: Step {iteration+1} 중간 보고서 생성이 실패했습니다. 제출된 것으로 간주하지 마세요. "
                            f"권위 있는 조사 상태 블록을 기준으로 다음 분석 작업을 계속 실행하고, "
                            f"판단이 바뀐 부분은 record_state로 상태를 갱신하세요. "
                            f"모든 조사가 완전히 끝나면 finish_task를 호출하세요.]"
                        )})

                # 마지막 스텝이면 여기서 소진으로 확정한다. 불필요한 롤오버를
                # 한 번 더 돌리고 나서 루프 경계로 조용히 끝나지 않도록.
                if token.cancelled:
                    outcome.settle(outcome_mod.STOPPED, token.reason)
                    break
                if iteration + 1 >= MAX_AGENT_LOOPS:
                    outcome.settle(outcome_mod.EXHAUSTED, outcome_mod.DETAIL_STEP_BUDGET)
                    break

                try:
                    await maybe_roll_context(iteration + 1)
                except (RunCancelled, StageTimeout) as stage_error:
                    settle_stage_failure(stage_error)
                    break
                continue

            # [도구 호출 없는 내부 추론 응답]
            if token.cancelled:
                outcome.settle(outcome_mod.STOPPED, token.reason)
                final_raw = full_raw_thought or content_text
                break

            if iteration + 1 >= MAX_AGENT_LOOPS:
                outcome.settle(outcome_mod.EXHAUSTED, outcome_mod.DETAIL_STEP_BUDGET)
                final_raw = full_raw_thought or content_text
                break

            # 모델이 도구 없이 내부 추론(thought/plan)만 진행한 경우:
            # 강제 넛지나 정체(stall) 실패 없이 자율적으로 다음 스텝으로 추론을 잇는다.
            messages_payload.append({
                "role": "assistant",
                "content": content_text or "[자율 내부 추론]",
            })
            log_session_event(
                workspace,
                "internal_thought",
                step=iteration + 1,
                content_chars=len(content_text or ""),
            )
            try:
                await status_msg.edit(content=f"🧠 **[Step {iteration+1}/{MAX_AGENT_LOOPS}]** ⚡ 자율 내부 추론 및 분석 진행 중... ▌")
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

        # 여기부터 종료 단계다. 합성·전송 중 도착분은 접수 시점에 거절되고,
        # 대기 중이던 항목은 미적용으로 종결된다.
        await close_steering()

        cleaned_check = re.sub(r"<think>.*?</think>|</?(function|parameter|tool_call)[^>]*>", "", final_raw, flags=re.DOTALL).strip()

        # 합성 여부는 길이 추정이 아니라 종료 사유로 결정한다.
        # 취소와 마감 실패는 후속 모델 단계를 시작하지 않는다. 같은 백엔드가
        # 멈춰 있을 수 있으므로 보존된 상태로 결정적 중간 보고서를 만든다.
        no_follow_up_stage = outcome.reason in (outcome_mod.STOPPED, outcome_mod.FAILED)
        uses_local_fallback = no_follow_up_stage
        if no_follow_up_stage:
            final_raw = build_incomplete_report(outcome, ledger, rolling_summary, messages_payload)
            if stage_failure_note:
                # 왜 보고서가 없는지는 사용자가 알아야 한다. 이 줄은 채널로만 가고
                # 종료 기록에는 예외 종류만 남는다.
                final_raw += f"\n\n> 업스트림 실패 상세: `{stage_failure_note}`"
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
                log_session_event(
                    workspace,
                    "synthesis",
                    step=current_step,
                    status=outcome.reason,
                    chars=len(final_raw),
                )
                log_content_debug(workspace, "synthesis", final_raw, step=current_step)
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
                log_session_event(
                    workspace,
                    "synthesis",
                    step=current_step,
                    status="stopped",
                    reason=synthesis_cancelled.reason,
                )
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
                log_session_event(
                    workspace,
                    "synthesis",
                    step=current_step,
                    status="timeout",
                    stage=synthesis_timeout.stage,
                )
            except Exception as synthesis_error:
                uses_local_fallback = True
                final_raw = build_incomplete_report(
                    outcome, ledger, rolling_summary, messages_payload
                )
                failure = _clip_summary_text(
                    f"{type(synthesis_error).__name__}: {synthesis_error}", 500
                )
                # 사용자에게는 왜 보고서가 없는지 알려야 하므로 업스트림 메시지를
                # 그대로 전달한다. 로그에는 종류만 남긴다.
                final_raw += (
                    "\n\n> 보고서 합성 업스트림 실패: `"
                    + failure
                    + "`. 모델 보고서 대신 보존된 상태를 사용했습니다."
                )
                log_session_event(
                    workspace,
                    "synthesis",
                    step=current_step,
                    status="error",
                    error=type(synthesis_error).__name__,
                )

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
        chunks = split_markdown_chunks(final_text_with_footer, max_chars=DISCORD_CHUNK_MAX_CHARS)

        try:
            await status_msg.delete()
        except Exception:
            pass

        await message.reply(chunks[0])
        for extra_chunk in chunks[1:]:
            await message.channel.send(extra_chunk)

        history.append({"role": "assistant", "content": final_text})
        log_run_end(outcome, chars=len(final_text), chunks=len(chunks))

    except Exception as e:
        outcome.settle(outcome_mod.FAILED, f"예외: {type(e).__name__}")
        # The run may already have settled COMPLETED before the exception - a failed
        # Discord delivery is the common case. settle() is first-wins, so outcome.label
        # would still read "조사 완료" on a message that reports a failure. Whatever the
        # earlier reason was, this path did not deliver a finished investigation.
        # 예외 문자열 대신 종류만 전달한다. 예외 본문은 실패 지점의 값을 그대로
        # 물고 오므로 채널·로그·표준 출력 어디에도 남기지 않는다(이슈 #11).
        err_msg = f"⚠️ **{outcome_mod.LABELS[outcome_mod.FAILED]}** — 작업 도중 예외 발생: `{type(e).__name__}`\n📁 현재까지의 실행 기록은 시스템 로그에 저장되었습니다."
        # 같은 이유로 종료 기록도 FAILED로 남긴다. outcome.reason은 선착순이라
        # 이미 completed일 수 있는데, 이 경로는 완료된 조사를 전달하지 못했다.
        log_run_end(
            outcome,
            status=outcome_mod.FAILED,
            abnormal=True,
            error=type(e).__name__,
        )
        # 루프 안에서 예외로 빠져나오면 위의 종료 단계 마감을 지나치므로 여기서
        # 닫는다. 이미 닫혀 있으면 아무 일도 하지 않는다.
        await close_steering()
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
            release_run(outcome.reason or "interrupted")


def startup_maintenance():
    """실행 리비전·의존성·유효 설정을 남기고, 만료된 로그와 런을 정리한 뒤
    미종료 런을 판정한다.

    시작 기록을 먼저 쓴다. 정리 도중 죽어도 어떤 배포본이 무엇을 지우려 했는지는
    남아 있어야 한다. 설정 지문 해시는 넣지 않는다: startup_diagnostics가 정책
    필드를 하나씩 그대로 출력하므로, 값이 이미 다 있는데 해시는 두 배포본이
    무엇이 달랐는지 알려주지 못한다.

    복구 판정은 정리 뒤에 온다. 먼저 하면 보관 기간이 지난 런이 prepared로 올라가
    만료를 피해 버린다.
    """
    path = session_log.write_startup_record(startup_diagnostics(CONFIG))
    swept = session_log.sweep_retention()
    # 로그만 지우면 런이 수집한 파일은 영구히 남는다. 보관 기간은 둘 다 덮는다.
    swept["runs"] = RUN_CATALOG.sweep_retention(CONFIG.log_retention_days * 86400.0)
    log_session_event(None, "retention_sweep", **swept)
    log_session_event(None, "run_recovery", **recover_interrupted_runs())
    return path


async def main():
    startup_maintenance()
    async with bot:
        await bot.start(DISCORD_TOKEN)

if __name__ == "__main__":
    asyncio.run(main())
