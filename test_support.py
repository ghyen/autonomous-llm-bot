"""Shared test bootstrap and Discord doubles.

Importing `bot` runs the configuration loader, which is deny-by-default, so the
required policy values have to exist before that import. Every test module goes
through here so there is exactly one place that knows the bootstrap.

All identifiers below are synthetic. No production ids or tokens.
"""

import os
from types import SimpleNamespace

TEST_USER_ID = 111111111111111111
TEST_ADMIN_ID = 222222222222222222
TEST_OUTSIDER_ID = 999999999999999999

os.environ.setdefault("DISCORD_BOT_TOKEN", "dummy-invalid-token")
os.environ.setdefault(
    "DISCORD_ALLOWED_USER_IDS", "{0},{1}".format(TEST_USER_ID, TEST_ADMIN_ID)
)
os.environ.setdefault("DISCORD_ADMIN_USER_IDS", str(TEST_ADMIN_ID))
os.environ.setdefault("LLM_BASE_URL", "http://127.0.0.1:18080/v1")


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
        self.purge_calls = 0
        self.manage_messages = manage_messages
        self.purge_error = purge_error

    async def typing(self):
        pass

    async def send(self, content):
        self.sent.append(content)
        return FakeSentMessage()

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
        self.reactions = []

    async def reply(self, content):
        self.replies.append(content)
        return FakeSentMessage()

    async def add_reaction(self, emoji):
        self.reactions.append(emoji)
