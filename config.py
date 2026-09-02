"""Typed configuration loading, validated before any side effect.

The previous loader read `os.environ` at import time, dropped malformed channel
ids without a word, created directories immediately, and called `sys.exit(1)`
on failure - so there was nothing to test and nothing to trust. It also told
operators to use a `.env` file that no code ever read.

This module owns the whole surface: parse, validate, and hand back an immutable
`BotConfig`. It creates no directories and opens no sockets. Security-relevant
values are deny-by-default: with tools enabled, a missing or malformed user
policy is a startup failure, not a silently open door.
"""

import math
import os
from dataclasses import dataclass
from typing import Dict, FrozenSet, List, Mapping, Optional, Tuple
from urllib.parse import urlsplit

DEFAULT_LLM_BASE_URL = "http://127.0.0.1:18080/v1"
DEFAULT_MODEL_NAME = "default"
DEFAULT_WORKSPACE_DIR = "~/discord-llm-bot/workspace"
DEFAULT_SYSTEM_LOG_DIR = "~/discord-llm-bot/.system_logs"

LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "[::1]", "0.0.0.0"})

# Per-stage deadlines. Separated on purpose: connecting, waiting for the next
# stream chunk, and the whole stage are different failure modes.
DEFAULT_CONNECT_TIMEOUT = 15.0
DEFAULT_IDLE_TIMEOUT = 300.0
DEFAULT_MODEL_STAGE_TIMEOUT = 600.0
DEFAULT_TOOL_STAGE_TIMEOUT = 120.0
DEFAULT_BASH_TIMEOUT = 60.0

# Tool workers are disposable and deny-by-default. These are ceilings, not
# tuning hints: lowering them is safe, while removing them is refused.
DEFAULT_TOOL_CPU_SECONDS = 30.0
DEFAULT_TOOL_MEMORY_BYTES = 268435456
DEFAULT_TOOL_PROCESS_LIMIT = 32
DEFAULT_TOOL_THREAD_LIMIT = 64
DEFAULT_TOOL_OPEN_FILES = 64
DEFAULT_TOOL_FILE_BYTES = 10485760
DEFAULT_TOOL_OUTPUT_BYTES = 65536
DEFAULT_TOOL_DISK_BYTES = 52428800

# Log hygiene. Content-level capture is off by default: it is the one setting
# that puts raw reasoning, tool arguments, and tool results back on disk.
DEFAULT_LOG_MAX_BYTES = 1048576
DEFAULT_LOG_RETENTION_DAYS = 14.0
DEFAULT_LOG_CONTENT_DEBUG_RETENTION_HOURS = 24.0

TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
FALSE_VALUES = frozenset({"0", "false", "no", "off"})


class ConfigError(Exception):
    """A configuration value is missing, malformed, or unsafe by default."""


NetworkAllowlistEntry = Tuple[str, str, int]


@dataclass(frozen=True)
class BotConfig:
    discord_token: str
    llm_base_url: str
    llm_api_key: str
    model_name: str
    free_response_channel_ids: FrozenSet[int]
    allowed_user_ids: FrozenSet[int]
    admin_user_ids: FrozenSet[int]
    workspace_dir: str
    system_log_dir: str
    tools_enabled: bool
    allow_remote_llm: bool
    connect_timeout: float
    idle_timeout: float
    model_stage_timeout: float
    tool_stage_timeout: float
    bash_timeout: float
    tool_cpu_seconds: float
    tool_memory_bytes: int
    tool_process_limit: int
    tool_thread_limit: int
    tool_open_files: int
    tool_file_bytes: int
    tool_output_bytes: int
    tool_disk_bytes: int
    tool_network_allowlist: Tuple[NetworkAllowlistEntry, ...]
    log_max_bytes: int
    log_retention_days: float
    log_content_debug: bool
    log_content_debug_retention_hours: float

    @property
    def llm_is_local(self) -> bool:
        return _host_of(self.llm_base_url) in LOCAL_HOSTS


def _host_of(url: str) -> str:
    remainder = str(url or "").split("://", 1)[-1]
    authority = remainder.split("/", 1)[0]
    authority = authority.rsplit("@", 1)[-1]
    if authority.startswith("["):
        return authority.split("]", 1)[0] + "]"
    return authority.rsplit(":", 1)[0] if ":" in authority else authority


def parse_id_set(raw, field: str) -> FrozenSet[int]:
    """Parse a comma-separated Discord id list, failing loudly on junk."""
    values = set()
    for entry in str(raw or "").split(","):
        entry = entry.strip()
        if not entry:
            continue
        if not entry.isdigit():
            raise ConfigError(
                "{0}: '{1}'은 숫자 ID가 아닙니다. 잘못된 항목을 무시하지 않고 시작을 중단합니다.".format(
                    field, entry
                )
            )
        value = int(entry)
        if value <= 0:
            raise ConfigError("{0}: '{1}'은 유효한 Discord ID가 아닙니다.".format(field, entry))
        values.add(value)
    return frozenset(values)


def parse_bool(raw, field: str, default: bool) -> bool:
    if raw is None or str(raw).strip() == "":
        return default
    value = str(raw).strip().lower()
    if value in TRUE_VALUES:
        return True
    if value in FALSE_VALUES:
        return False
    raise ConfigError(
        "{0}: '{1}'은 참/거짓 값이 아닙니다. 허용: {2}.".format(
            field, raw, ", ".join(sorted(TRUE_VALUES | FALSE_VALUES))
        )
    )


def parse_positive_float(raw, field: str, default: float) -> float:
    if raw is None or str(raw).strip() == "":
        return default
    try:
        value = float(str(raw).strip())
    except ValueError:
        raise ConfigError("{0}: '{1}'은 숫자가 아닙니다.".format(field, raw))
    if not math.isfinite(value) or value <= 0:
        raise ConfigError(
            "{0}: '{1}'은 유한한 0보다 큰 숫자여야 합니다. 마감을 없애려면 큰 값을 쓰세요.".format(
                field, raw
            )
        )
    return value


def parse_positive_int(raw, field: str, default: int) -> int:
    """Whole-number sizes only: '1.5' bytes is a typo, not a size."""
    if raw is None or str(raw).strip() == "":
        return default
    value = str(raw).strip()
    if not value.isdigit() or int(value) <= 0:
        raise ConfigError("{0}: '{1}'은 0보다 큰 정수여야 합니다.".format(field, raw))
    return int(value)


def parse_network_allowlist(raw, field: str) -> Tuple[NetworkAllowlistEntry, ...]:
    """Parse explicit HTTP(S) origins used by the Seatbelt worker."""
    entries = []
    seen = set()
    for value in str(raw or "").split(","):
        value = value.strip()
        if not value:
            continue
        try:
            parsed = urlsplit(value)
            scheme = parsed.scheme.lower()
            hostname = parsed.hostname
            port = parsed.port
        except ValueError:
            raise ConfigError(
                "{0}: '{1}'은 유효한 네트워크 origin이 아닙니다.".format(field, value)
            )
        if scheme not in ("http", "https") or not parsed.netloc or not hostname:
            raise ConfigError(
                "{0}: '{1}'은 http 또는 https origin이어야 합니다.".format(field, value)
            )
        if parsed.username is not None or parsed.password is not None:
            raise ConfigError(
                "{0}: '{1}'에는 사용자명이나 비밀번호를 넣을 수 없습니다.".format(field, value)
            )
        if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
            raise ConfigError(
                "{0}: '{1}'에는 root 경로만 허용되며 query/fragment는 금지됩니다.".format(
                    field, value
                )
            )
        if port is None:
            port = 443 if scheme == "https" else 80
        if not 1 <= port <= 65535:
            raise ConfigError(
                "{0}: '{1}'의 포트는 1부터 65535 사이여야 합니다.".format(field, value)
            )
        entry = (scheme, hostname.lower(), port)
        if entry not in seen:
            entries.append(entry)
            seen.add(entry)
    return tuple(entries)


def load_env_file(path) -> Dict[str, str]:
    """Minimal `.env` reader so the documented file actually takes effect.

    No new dependency: the format we document is `KEY=VALUE` with `#` comments,
    an optional `export ` prefix, and optional surrounding quotes.

    Malformed input is refused rather than half-read. This loader exists because
    misconfiguration used to pass silently, so it must not reintroduce that in
    its own parsing.
    """
    values: Dict[str, str] = {}
    if not path:
        return values
    try:
        # utf-8-sig, not utf-8: an editor-written BOM would otherwise land inside
        # the first key, so DISCORD_BOT_TOKEN would parse as "\ufeffDISCORD_BOT_TOKEN"
        # and the value the operator set would be silently ignored.
        with open(path, "r", encoding="utf-8-sig") as handle:
            lines = handle.readlines()
    except (FileNotFoundError, NotADirectoryError, IsADirectoryError, PermissionError):
        return values

    for lineno, raw_line in enumerate(lines, 1):
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, _, value = line.partition("=")
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        if value[:1] in ("\"", "'"):
            quote = value[0]
            end = value.find(quote, 1)
            if end == -1:
                raise ConfigError(
                    "{0} {1}행: {2} 값의 따옴표가 닫히지 않았습니다. "
                    "값은 한 줄로 적어야 합니다.".format(path, lineno, key)
                )
            value = value[1:end]
        else:
            # An unquoted value ends at a whitespace-separated '#'. Requiring the
            # whitespace keeps a '#' that is part of a secret inside the value.
            value = value.split(" #", 1)[0].split("\t#", 1)[0].strip()
        values[key] = value
    return values


def load_config(env: Optional[Mapping[str, str]] = None, env_file: Optional[str] = ".env") -> BotConfig:
    """Build the effective configuration. Raises ConfigError; never exits."""
    if env is None:
        env = os.environ
    file_values = load_env_file(env_file)

    def get(key: str, default=None):
        # A real environment variable always wins over the file.
        value = env.get(key)
        if value is None or str(value).strip() == "":
            value = file_values.get(key)
        if value is None or str(value).strip() == "":
            return default
        return str(value).strip()

    discord_token = get("DISCORD_BOT_TOKEN")
    if not discord_token:
        raise ConfigError(
            "DISCORD_BOT_TOKEN이 설정되지 않았습니다. 환경 변수로 넣거나 .env 파일에 추가하세요."
        )

    tools_enabled = parse_bool(get("BOT_TOOLS_ENABLED"), "BOT_TOOLS_ENABLED", True)
    allow_remote_llm = parse_bool(get("LLM_ALLOW_REMOTE"), "LLM_ALLOW_REMOTE", False)

    allowed_user_ids = parse_id_set(get("DISCORD_ALLOWED_USER_IDS"), "DISCORD_ALLOWED_USER_IDS")
    admin_user_ids = parse_id_set(get("DISCORD_ADMIN_USER_IDS"), "DISCORD_ADMIN_USER_IDS")
    free_response_channel_ids = parse_id_set(
        get("DISCORD_FREE_RESPONSE_CHANNELS"), "DISCORD_FREE_RESPONSE_CHANNELS"
    )

    if tools_enabled and not allowed_user_ids:
        raise ConfigError(
            "DISCORD_ALLOWED_USER_IDS가 비어 있습니다. 도구가 활성화된 구성에서는 호출자 허용 목록이 "
            "필수입니다. 허용할 Discord 사용자 ID를 넣거나, 도구 없이 운영하려면 "
            "BOT_TOOLS_ENABLED=false로 설정하세요."
        )

    unknown_admins = admin_user_ids - allowed_user_ids
    if unknown_admins:
        raise ConfigError(
            "DISCORD_ADMIN_USER_IDS에 허용 목록 밖의 ID가 있습니다: {0}. "
            "관리자도 DISCORD_ALLOWED_USER_IDS에 포함되어야 합니다.".format(
                ", ".join(str(value) for value in sorted(unknown_admins))
            )
        )

    llm_base_url = get("LLM_BASE_URL", DEFAULT_LLM_BASE_URL)
    config = BotConfig(
        discord_token=discord_token,
        llm_base_url=llm_base_url,
        # Local OpenAI-compatible servers ignore this, but the SDK requires a
        # non-empty string. No credential default lives in source.
        llm_api_key=get("LLM_API_KEY", "-"),
        model_name=get("MODEL_NAME", get("LLM_MODEL_NAME", DEFAULT_MODEL_NAME)),
        free_response_channel_ids=free_response_channel_ids,
        allowed_user_ids=allowed_user_ids,
        admin_user_ids=admin_user_ids,
        workspace_dir=os.path.expanduser(get("WORKSPACE_DIR", DEFAULT_WORKSPACE_DIR)),
        system_log_dir=os.path.expanduser(get("SYSTEM_LOG_DIR", DEFAULT_SYSTEM_LOG_DIR)),
        tools_enabled=tools_enabled,
        allow_remote_llm=allow_remote_llm,
        connect_timeout=parse_positive_float(
            get("LLM_CONNECT_TIMEOUT_SECONDS"), "LLM_CONNECT_TIMEOUT_SECONDS", DEFAULT_CONNECT_TIMEOUT
        ),
        idle_timeout=parse_positive_float(
            get("LLM_IDLE_TIMEOUT_SECONDS"), "LLM_IDLE_TIMEOUT_SECONDS", DEFAULT_IDLE_TIMEOUT
        ),
        model_stage_timeout=parse_positive_float(
            get("MODEL_STAGE_TIMEOUT_SECONDS"), "MODEL_STAGE_TIMEOUT_SECONDS", DEFAULT_MODEL_STAGE_TIMEOUT
        ),
        tool_stage_timeout=parse_positive_float(
            get("TOOL_STAGE_TIMEOUT_SECONDS"), "TOOL_STAGE_TIMEOUT_SECONDS", DEFAULT_TOOL_STAGE_TIMEOUT
        ),
        bash_timeout=parse_positive_float(
            get("BASH_TIMEOUT_SECONDS"), "BASH_TIMEOUT_SECONDS", DEFAULT_BASH_TIMEOUT
        ),
        tool_cpu_seconds=parse_positive_float(
            get("TOOL_CPU_SECONDS"), "TOOL_CPU_SECONDS", DEFAULT_TOOL_CPU_SECONDS
        ),
        tool_memory_bytes=parse_positive_int(
            get("TOOL_MEMORY_BYTES"), "TOOL_MEMORY_BYTES", DEFAULT_TOOL_MEMORY_BYTES
        ),
        tool_process_limit=parse_positive_int(
            get("TOOL_PROCESS_LIMIT"), "TOOL_PROCESS_LIMIT", DEFAULT_TOOL_PROCESS_LIMIT
        ),
        tool_thread_limit=parse_positive_int(
            get("TOOL_THREAD_LIMIT"), "TOOL_THREAD_LIMIT", DEFAULT_TOOL_THREAD_LIMIT
        ),
        tool_open_files=parse_positive_int(
            get("TOOL_OPEN_FILES"), "TOOL_OPEN_FILES", DEFAULT_TOOL_OPEN_FILES
        ),
        tool_file_bytes=parse_positive_int(
            get("TOOL_FILE_BYTES"), "TOOL_FILE_BYTES", DEFAULT_TOOL_FILE_BYTES
        ),
        tool_output_bytes=parse_positive_int(
            get("TOOL_OUTPUT_BYTES"), "TOOL_OUTPUT_BYTES", DEFAULT_TOOL_OUTPUT_BYTES
        ),
        tool_disk_bytes=parse_positive_int(
            get("TOOL_DISK_BYTES"), "TOOL_DISK_BYTES", DEFAULT_TOOL_DISK_BYTES
        ),
        tool_network_allowlist=parse_network_allowlist(
            get("TOOL_NETWORK_ALLOWLIST"), "TOOL_NETWORK_ALLOWLIST"
        ),
        log_max_bytes=parse_positive_int(
            get("LOG_MAX_BYTES"), "LOG_MAX_BYTES", DEFAULT_LOG_MAX_BYTES
        ),
        log_retention_days=parse_positive_float(
            get("LOG_RETENTION_DAYS"), "LOG_RETENTION_DAYS", DEFAULT_LOG_RETENTION_DAYS
        ),
        # Deny by default: turning this on stores raw reasoning, tool arguments,
        # and tool results, so it must be an explicit operator decision.
        log_content_debug=parse_bool(
            get("LOG_CONTENT_DEBUG"), "LOG_CONTENT_DEBUG", False
        ),
        log_content_debug_retention_hours=parse_positive_float(
            get("LOG_CONTENT_DEBUG_RETENTION_HOURS"),
            "LOG_CONTENT_DEBUG_RETENTION_HOURS",
            DEFAULT_LOG_CONTENT_DEBUG_RETENTION_HOURS,
        ),
    )

    if not config.llm_is_local and not allow_remote_llm:
        raise ConfigError(
            "LLM_BASE_URL이 로컬 주소가 아닙니다 (host={0}). 대화 내용과 도구 결과의 수신자가 "
            "바뀌므로 LLM_ALLOW_REMOTE=true로 명시적으로 승인해야 합니다.".format(
                _host_of(llm_base_url)
            )
        )

    return config


def startup_diagnostics(config: BotConfig) -> List[str]:
    """Startup lines that identify the deployment without leaking secrets."""
    return [
        "llm endpoint: {0} ({1})".format(
            config.llm_base_url, "local" if config.llm_is_local else "remote, explicitly allowed"
        ),
        "model: {0}".format(config.model_name),
        "tools: {0}".format("enabled" if config.tools_enabled else "disabled"),
        "allowed users: {0} (admins: {1})".format(
            len(config.allowed_user_ids), len(config.admin_user_ids)
        ),
        "free-response channels: {0}".format(len(config.free_response_channel_ids)),
        "deadlines(s): connect={0} idle={1} model={2} tool={3} bash={4}".format(
            config.connect_timeout,
            config.idle_timeout,
            config.model_stage_timeout,
            config.tool_stage_timeout,
            config.bash_timeout,
        ),
        "tool sandbox: cpu={0}s memory={1} processes={2} threads={3} open_files={4} file_bytes={5} output_bytes={6} disk_bytes={7} network_origins={8}".format(
            config.tool_cpu_seconds,
            config.tool_memory_bytes,
            config.tool_process_limit,
            config.tool_thread_limit,
            config.tool_open_files,
            config.tool_file_bytes,
            config.tool_output_bytes,
            config.tool_disk_bytes,
            len(config.tool_network_allowlist),
        ),
        "workspace: {0}".format(config.workspace_dir),
        "system logs: {0}".format(config.system_log_dir),
        "log policy: max_bytes={0} retention_days={1} content_debug={2} content_hours={3}".format(
            config.log_max_bytes,
            config.log_retention_days,
            "ON (원문 저장)" if config.log_content_debug else "off",
            config.log_content_debug_retention_hours,
        ),
    ]
