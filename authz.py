"""Deny-by-default authorization for every Discord entry point.

The bot drives an agent that can run shell commands and read and write files on
the host, but nothing in the code checked *who* was asking. The channel
allowlist only ever decided where the bot would speak, and DMs and mentions
bypassed it entirely; text control commands ran before that check even happened.

DMs and mentions are routing signals. They say where a message came from, not
who sent it. Identity is the allowlist, and one gate answers for all of it:
text commands, slash commands, mid-flight steering, and normal requests.

Rules, in one place so they can be read as a whole:

* Not on the allowlist means no. Nothing else is evaluated.
* Run control (stop, reset, steering) is for the run's owner or a configured
  admin. With no run in flight, any allowed caller may reset their channel.
* Purging channel messages needs the caller's own Discord "Manage Messages"
  permission - the bot's permission is not the caller's. When an admin list is
  configured, the caller must also be on it.
"""

from dataclasses import dataclass
from typing import AbstractSet, Optional

ACCESS = "access"
CONTROL = "control"
PURGE = "purge"

ACTIONS = (ACCESS, CONTROL, PURGE)


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str = ""

    def __bool__(self) -> bool:
        return self.allowed


DENY_ACCESS_MESSAGE = (
    "⛔ **권한이 없습니다.** 이 봇은 호스트에서 명령을 실행하므로 허용된 사용자만 사용할 수 있습니다. "
    "운영자에게 `DISCORD_ALLOWED_USER_IDS` 등록을 요청하세요."
)


def authorize(
    action: str,
    user_id,
    allowed_user_ids: AbstractSet,
    admin_user_ids: AbstractSet,
    tools_enabled: bool = True,
    run_owner_id: Optional[int] = None,
    caller_can_manage_messages: bool = False,
) -> Decision:
    """Single decision point. Returns a Decision; never raises on bad input."""
    if action not in ACTIONS:
        return Decision(False, "알 수 없는 동작 '{0}'".format(action))

    allowed_user_ids = allowed_user_ids or frozenset()
    admin_user_ids = admin_user_ids or frozenset()

    if user_id is None:
        return Decision(False, "호출자 신원을 확인할 수 없습니다.")

    if not allowed_user_ids:
        if tools_enabled:
            # config.load_config() rejects this combination at startup. If it is
            # ever reached anyway, refuse rather than fail open.
            return Decision(
                False,
                "허용 목록이 비어 있는데 도구가 활성화되어 있습니다. 시작 설정을 확인하세요.",
            )
        # Documented tool-free mode: no host access, so no allowlist required.
        open_access = True
    else:
        open_access = False
        if user_id not in allowed_user_ids:
            return Decision(False, "허용 목록에 없는 호출자입니다.")

    is_admin = user_id in admin_user_ids

    if action == ACCESS:
        return Decision(True)

    if action == CONTROL:
        if is_admin or run_owner_id is None or user_id == run_owner_id:
            return Decision(True)
        return Decision(
            False,
            "실행 중인 조사는 시작한 사용자 또는 관리자만 제어할 수 있습니다.",
        )

    # PURGE
    if not caller_can_manage_messages:
        return Decision(
            False,
            "메시지 삭제에는 호출자의 '메시지 관리' 권한이 필요합니다.",
        )
    if admin_user_ids and not is_admin:
        return Decision(
            False,
            "메시지 삭제는 DISCORD_ADMIN_USER_IDS에 등록된 관리자만 가능합니다.",
        )
    if open_access:
        return Decision(
            False,
            "관리자 목록이 설정되지 않아 메시지 삭제를 허용하지 않습니다.",
        )
    return Decision(True)
