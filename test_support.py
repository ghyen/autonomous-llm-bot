"""Shared test bootstrap and Discord doubles.

Importing `bot` runs the configuration loader, which is deny-by-default, so the
required policy values have to exist before that import. Every test module goes
through here so there is exactly one place that knows the bootstrap.

All identifiers below are synthetic. No production ids or tokens.
"""

import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from run_workspace import RunCatalog

TEST_USER_ID = 111111111111111111
TEST_ADMIN_ID = 222222222222222222
TEST_OUTSIDER_ID = 999999999999999999

os.environ.setdefault("DISCORD_BOT_TOKEN", "dummy-invalid-token")
os.environ.setdefault(
    "DISCORD_ALLOWED_USER_IDS", "{0},{1}".format(TEST_USER_ID, TEST_ADMIN_ID)
)
os.environ.setdefault("DISCORD_ADMIN_USER_IDS", str(TEST_ADMIN_ID))
os.environ.setdefault("LLM_BASE_URL", "http://127.0.0.1:18080/v1")


def run_catalog_patch(bot_module, root):
    """Patch the bot with a real isolated catalog rooted in a test temp dir."""
    root = Path(root)
    catalog = RunCatalog(root / "workspace", root / "logs")
    return patch.object(bot_module, "RUN_CATALOG", catalog)


class FakeSentMessage:
    def __init__(self):
        self.edits = []
        self.deleted = False

    async def edit(self, **kwargs):
        self.edits.append(kwargs.get("content"))

    async def delete(self):
        self.deleted = True


class FakeChannel:
    def __init__(self, channel_id, manage_messages=False, purge_error=None):
        self.id = channel_id
        self.sent = []
        self.sent_messages = []
        self.purge_calls = 0
        self.manage_messages = manage_messages
        self.purge_error = purge_error

    async def typing(self):
        pass

    async def send(self, content):
        self.sent.append(content)
        handle = FakeSentMessage()
        # Retained so tests can read what was later edited into a status message,
        # such as the error text written when the final reply cannot be delivered.
        self.sent_messages.append(handle)
        return handle

    def permissions_for(self, user):
        return SimpleNamespace(manage_messages=self.manage_messages)

    async def purge(self, limit=0):
        self.purge_calls += 1
        if self.purge_error is not None:
            raise self.purge_error
        return [object()] * min(limit, 5)


class FakeAuthor:
    bot = False

    def __init__(self, user_id=TEST_USER_ID, name="tester"):
        self.id = user_id
        self.name = name

    def __str__(self):
        return self.name


class FakeMessage:
    def __init__(self, content, channel_id, author=None, manage_messages=False, purge_error=None):
        self.content = content
        self.channel = FakeChannel(
            channel_id, manage_messages=manage_messages, purge_error=purge_error
        )
        self.author = author or FakeAuthor()
        self.mentions = []
        self.replies = []
        self.reply_handles = []
        self.reactions = []

    async def reply(self, content):
        self.replies.append(content)
        handle = FakeSentMessage()
        # The first reply is the status message the run later edits, so tests need
        # the handle and not just the text that was originally sent.
        self.reply_handles.append(handle)
        return handle

    async def add_reaction(self, emoji):
        self.reactions.append(emoji)



class FakeInteractionResponse:
    def __init__(self):
        self.messages = []
        self.deferred = False

    async def send_message(self, content, ephemeral=False):
        self.messages.append(content)

    async def defer(self, ephemeral=False):
        self.deferred = True


class FakeFollowup:
    def __init__(self):
        self.messages = []

    async def send(self, content, ephemeral=False):
        self.messages.append(content)


class FakeInteraction:
    """Double for the slash command path.

    Slash commands never looked at `interaction.user`, so they need coverage that
    is independent of the text command path. `permissions` is the caller's own
    channel permission, which is what a purge has to be judged on - not the
    bot's.
    """

    def __init__(
        self,
        channel_id,
        user_id=TEST_USER_ID,
        manage_messages=False,
        purge_error=None,
    ):
        self.channel_id = channel_id
        self.user = FakeAuthor(user_id)
        self.channel = FakeChannel(
            channel_id, manage_messages=manage_messages, purge_error=purge_error
        )
        self.permissions = SimpleNamespace(manage_messages=manage_messages)
        self.response = FakeInteractionResponse()
        self.followup = FakeFollowup()

    @property
    def replies(self):
        """Everything the caller was told, whichever channel it arrived on."""
        return self.response.messages + self.followup.messages
