"""
End-to-end harness: the real bot code (commands, buttons, queue, SQLite,
alerts, webhook HTTP client) against fake Discord interactions and the fake
webhook server in fakes.py.

Nothing is mocked below the Discord boundary: every click goes through
SignupButton.callback -> DBQueue -> SQLite, every alert through
run_alerts_once -> utils.webhook.send_message -> a real socket.
"""

import itertools
from dataclasses import dataclass, field

import discord

import bot as botmod
from utils import database as db
from utils.roster import build_roster

_ids = itertools.count(10_000_000_000_000_000)

GUILD_ID = 123456789012345678
CHANNEL_ID = 223456789012345678
ROLE_ID = "323456789012345678"


@dataclass
class Reply:
    kind: str                  # send | edit | followup
    content: str | None
    embed: discord.Embed | None
    view: object
    ephemeral: bool
    allowed_mentions: discord.AllowedMentions | None


@dataclass
class FakeUser:
    id: int
    display_name: str

    @property
    def mention(self):
        return f"<@{self.id}>"


@dataclass
class FakeMessage:
    id: int


class _Perms:
    def __init__(self, manage_messages=False):
        self.manage_messages = manage_messages


class FakeChannel:
    def __init__(self, moderators=()):
        self.moderators = set(moderators)

    def permissions_for(self, user):
        return _Perms(manage_messages=user.id in self.moderators)


@dataclass
class Sent:
    content: str
    embed: discord.Embed | None
    view: object
    allowed_mentions: discord.AllowedMentions | None
    message_id: int


class FakeTextChannel:
    """What bot.get_channel() returns: records posts and edits of the bot's own messages."""

    def __init__(self, channel_id=CHANNEL_ID, fail_with: Exception | None = None):
        self.id = channel_id
        self.sent: list[Sent] = []
        self.edits: dict[int, list] = {}
        self.fail_with = fail_with

    async def send(self, content=None, *, embed=None, view=None, allowed_mentions=None):
        if self.fail_with is not None:
            raise self.fail_with
        message = FakeMessage(next(_ids))
        self.sent.append(Sent(content, embed, view, allowed_mentions, message.id))
        return message

    def get_partial_message(self, message_id):
        channel = self

        class _Partial:
            async def edit(self, *, embed=None, view=None):
                channel.edits.setdefault(message_id, []).append((embed, view))
        return _Partial()


class FakeResponse:
    def __init__(self, interaction):
        self._it = interaction
        self._done = False

    def is_done(self):
        return self._done

    def _record(self, kind, content, embed, view, ephemeral, allowed_mentions):
        # Discord rejects a second response to one interaction; a click that
        # is answered twice (or never) is a bug the harness must catch.
        assert not self._done, "interaction answered twice"
        self._done = True
        self._it.replies.append(Reply(kind, content, embed, view, ephemeral, allowed_mentions))

    async def send_message(self, content=None, *, embed=None, view=None, ephemeral=False, allowed_mentions=None):
        self._record("send", content, embed, view, ephemeral, allowed_mentions)

    async def edit_message(self, *, content=None, embed=None, view=None):
        self._record("edit", content, embed, view, False, None)


class FakeFollowup:
    def __init__(self, interaction):
        self._it = interaction

    async def send(self, content=None, *, embed=None, ephemeral=False, allowed_mentions=None):
        assert self._it.response.is_done(), "followup before the interaction was answered"
        self._it.replies.append(Reply("followup", content, embed, None, ephemeral, allowed_mentions))


@dataclass
class FakeInteraction:
    client: object
    user: FakeUser
    channel: FakeChannel
    message: FakeMessage | None = None
    guild_id: int = GUILD_ID
    channel_id: int = CHANNEL_ID
    replies: list = field(default_factory=list)

    def __post_init__(self):
        self.response = FakeResponse(self)
        self.followup = FakeFollowup(self)

    async def original_response(self):
        return FakeMessage(next(_ids))


class World:
    """One guild, one channel, one bot. Drives commands and clicks, checks invariants."""

    def __init__(self, cfg, q, moderators=(), text_channel: FakeTextChannel | None = None):
        self.bot = botmod.TenManBot(cfg, q)
        self.text_channel = text_channel or FakeTextChannel()
        self.bot.get_channel = lambda channel_id: self.text_channel if int(channel_id) == CHANNEL_ID else None
        self.cmds = next(c for c in self.bot.tree.get_commands() if c.name == "tenman")
        self.channel = FakeChannel(moderators)
        self.q = q
        self.interactions: list[FakeInteraction] = []
        self.session_message: dict[int, int] = {}

    def _it(self, user, message=None):
        it = FakeInteraction(self.bot, user, self.channel, message)
        self.interactions.append(it)
        return it

    # ----------------------------------------------------------------- commands

    async def start(self, user, when, **options) -> FakeInteraction:
        it = self._it(user)
        await self.cmds.get_command("start").callback(self.cmds, it, when, **options)
        return it

    async def cancel(self, user, number=None) -> FakeInteraction:
        it = self._it(user)
        await self.cmds.get_command("cancel").callback(self.cmds, it, number)
        return it

    async def confirm_cancel(self, ask: FakeInteraction, user) -> FakeInteraction:
        view = ask.replies[-1].view
        it = self._it(user)
        await view.confirm.callback(it)
        return it

    async def schedule(self, user, when, **options) -> FakeInteraction:
        it = self._it(user)
        options = {"repeat": None, "weeks": None, "post_days": None, "title": None, "slots": None, **options}
        if isinstance(options["repeat"], str):
            options["repeat"] = discord.app_commands.Choice(name=options["repeat"], value=options["repeat"])
        await self.cmds.get_command("schedule").callback(self.cmds, it, when, **options)
        return it

    async def list(self, user) -> FakeInteraction:
        it = self._it(user)
        await self.cmds.get_command("list").callback(self.cmds, it)
        return it

    async def lobby(self, user, url, number=None) -> FakeInteraction:
        it = self._it(user)
        await self.cmds.get_command("lobby").callback(self.cmds, it, url, number)
        return it

    async def click(self, user, action, session_id, message_id=None) -> FakeInteraction:
        message_id = message_id or self.message_id(session_id)
        it = self._it(user, FakeMessage(message_id))
        button = botmod.SignupButton(action, session_id)
        await button.callback(it)
        return it

    def message_id(self, session_id) -> int:
        return int(self.q.read(db.get_session, session_id)["message_id"])

    def roster(self, session_id):
        session = self.q.read(db.get_session, session_id)
        return build_roster(self.q.read(db.list_signups, session_id), session["slots"])

    # ----------------------------------------------------------------- invariants

    def check(self):
        """Every interaction answered exactly once, every reply within Discord's limits, no stray pings."""
        for it in self.interactions:
            assert it.response.is_done(), f"interaction by {it.user.display_name} was never answered"
            for reply in it.replies:
                check_reply(reply)
        for session in self.q.read(lambda c: [dict(r) for r in c.execute("SELECT * FROM sessions")]):
            signups = self.q.read(db.list_signups, session["id"])
            roster = build_roster(signups, session["slots"])
            assert roster.count <= session["slots"]
            keys = [(s["user_id"], s["guest_no"]) for s in signups]
            assert len(keys) == len(set(keys)), "duplicate roster row"
            ins = [s for s in sorted(signups, key=lambda s: (s["joined_at"], s["id"])) if s["status"] == db.IN]
            assert roster.confirmed == ins[: session["slots"]], "roster is not first-come-first-served"
            for guest in (s for s in signups if s["guest_no"] > 0):
                sponsor = next(s for s in signups if s["user_id"] == guest["user_id"] and s["guest_no"] == 0)
                assert sponsor["status"] == db.IN, "guest of a player who isn't In"


def check_reply(reply: Reply):
    if reply.content is not None:
        assert len(reply.content) <= 2000
        assert "@everyone" not in reply.content and "@here" not in reply.content
    am = reply.allowed_mentions
    if am is not None:
        assert am.everyone is False
    if reply.embed is not None:
        check_embed(reply.embed.to_dict())


def check_embed(e: dict):
    """Discord's documented embed limits, plus: no user text may render as a mention."""
    total = 0
    assert len(e.get("title", "")) <= 256
    assert len(e.get("description", "")) <= 4096
    assert len(e.get("fields", [])) <= 25
    for f in e.get("fields", []):
        assert 0 < len(f["name"]) <= 256 and 0 < len(f["value"]) <= 1024
        total += len(f["name"]) + len(f["value"])
    total += len(e.get("title", "")) + len(e.get("description", "")) + len(e.get("footer", {}).get("text", ""))
    total += len(e.get("author", {}).get("name", ""))
    assert total <= 6000
    text = " ".join([e.get("title", ""), e.get("description", "")] + [f["value"] for f in e.get("fields", [])])
    assert "@everyone" not in text and "@here" not in text

