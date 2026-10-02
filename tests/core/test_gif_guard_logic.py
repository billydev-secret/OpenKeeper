"""GIF Guard — what counts as a GIF, which channels are guarded, and the gate.

Every rule in gif_guard_logic makes something deletable, so the "kept" rows
below matter as much as the "deleted" ones: a still WebP, a suppressed link,
the word "tenor" in a sentence, and an empty channel list must all survive.
"""
from __future__ import annotations

import logging

import discord
import pytest

from bot_modules.core.post_monitoring import GIF_GUARD_NOTICE, enforce_gif_guard
from bot_modules.core.gif_guard_logic import (
    attachment_is_gif,
    content_has_gif_link,
    embed_is_gif,
    is_guarded_channel,
    message_has_gif,
)

log = logging.getLogger("test")

GUARDED = 100
OTHER = 200
BYPASS_ROLE = 900


# ── Pure predicates ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "content, expected",
    [
        # What Discord's GIF picker actually posts.
        ("https://tenor.com/view/cat-dance-gif-123456", True),
        ("lol this is me https://tenor.com/bXyZ.gif", True),
        ("https://media.tenor.com/abc/AAAAC/cat.gif", True),
        ("https://giphy.com/gifs/cat-l0HlBO7eyXzSZkJri", True),
        ("https://media1.giphy.com/media/xyz/giphy.gif", True),
        ("https://gph.is/g/abcd", True),
        ("https://i.imgur.com/abcd.gifv", True),
        ("https://example.com/path/funny.GIF?size=large", True),
        ("||https://tenor.com/view/spoilered-gif-1||", True),
        ("HTTP://TENOR.COM/view/shouting-gif-1", True),
        # Kept: no preview, not a GIF host, or no link at all.
        pytest.param("<https://tenor.com/view/cat-gif-1>", False, id="suppressed"),
        pytest.param("I love tenor.com gifs", False, id="bare-word"),
        pytest.param("https://nottenor.com/view/x", False, id="lookalike-host"),
        pytest.param("https://tenor.com.evil.example/x", False, id="host-suffix-trick"),
        pytest.param("https://example.com/gif-guide", False, id="gif-in-path-word"),
        pytest.param("https://i.imgur.com/abcd.png", False, id="still-image"),
        pytest.param("", False, id="empty"),
        pytest.param(None, False, id="none"),
    ],
)
def test_content_gif_links(content, expected):
    assert content_has_gif_link(content) is expected


@pytest.mark.parametrize(
    "filename, content_type, animated, expected",
    [
        ("dance.gif", "image/gif", False, True),
        ("DANCE.GIF", None, False, True),
        ("noext", "image/gif", False, True),
        # Animated WebP is caught by Discord's own flag, not the extension…
        ("wave.webp", "image/webp", True, True),
        # …so a still WebP is left alone.
        pytest.param("photo.webp", "image/webp", False, False, id="still-webp"),
        pytest.param("photo.png", "image/png", False, False, id="png"),
        pytest.param("notes.txt", "text/plain", False, False, id="text"),
    ],
)
def test_attachment_gifs(filename, content_type, animated, expected):
    assert attachment_is_gif(filename, content_type, animated) is expected


@pytest.mark.parametrize(
    "embed_type, url, expected",
    [
        ("gifv", "https://some-unknown-host.example/v/1", True),
        ("image", "https://cdn.example/x.gif", True),
        pytest.param("image", "https://cdn.example/x.png", False, id="still-image"),
        pytest.param("article", "https://cdn.example/x.gif", False, id="article"),
        pytest.param("rich", None, False, id="no-url"),
    ],
)
def test_embed_gifs(embed_type, url, expected):
    assert embed_is_gif(embed_type, url) is expected


def test_message_has_gif_checks_every_part():
    assert message_has_gif("hi", [("a.png", "image/png", False)], [("gifv", None)])
    assert message_has_gif("hi", [("a.gif", None, False)], [])
    assert not message_has_gif("hi", [("a.png", "image/png", False)], [])


@pytest.mark.parametrize(
    "channel_id, parent_id, guarded, expected",
    [
        (GUARDED, None, {GUARDED}, True),
        # Threads follow their parent channel.
        (555, GUARDED, {GUARDED}, True),
        (OTHER, None, {GUARDED}, False),
        (555, OTHER, {GUARDED}, False),
        # The off state: nothing listed guards nothing.
        pytest.param(GUARDED, None, frozenset(), False, id="empty-list-is-off"),
    ],
)
def test_guarded_channels(channel_id, parent_id, guarded, expected):
    assert is_guarded_channel(channel_id, parent_id, guarded) is expected


# ── The gate ───────────────────────────────────────────────────────────


class FakeRole:
    def __init__(self, role_id: int) -> None:
        self.id = role_id


class FakeMember:
    def __init__(self, roles: list[FakeRole] | None = None, bot: bool = False) -> None:
        self.id = 42
        self.name = "member"
        self.roles = roles or []
        self.bot = bot


class FakeChannel:
    def __init__(self, channel_id: int = GUARDED, parent_id: int | None = None) -> None:
        self.id = channel_id
        self.name = "chan"
        if parent_id is not None:
            self.parent_id = parent_id
        self.sent: list[tuple[str, dict]] = []

    async def send(self, content: str, **kwargs) -> None:
        self.sent.append((content, kwargs))


class FakeFlags:
    def __init__(self, animated: bool = False) -> None:
        self.animated = animated


class FakeAttachment:
    def __init__(self, filename: str, content_type: str | None = None, animated: bool = False):
        self.filename = filename
        self.content_type = content_type
        self.flags = FakeFlags(animated)


class FakeEmbed:
    def __init__(self, type_: str, url: str | None = None) -> None:
        self.type = type_
        self.url = url


class FakeMessage:
    def __init__(
        self,
        content: str = "",
        *,
        attachments=(),
        embeds=(),
        channel: FakeChannel | None = None,
        author: object | None = None,
        delete_error: Exception | None = None,
    ) -> None:
        self.content = content
        self.attachments = list(attachments)
        self.embeds = list(embeds)
        self.channel = channel or FakeChannel()
        self.author = author if author is not None else FakeMember()
        self.deleted = False
        self._delete_error = delete_error

    async def delete(self) -> None:
        if self._delete_error is not None:
            raise self._delete_error
        self.deleted = True


@pytest.fixture(autouse=True)
def _member_isinstance(monkeypatch):
    monkeypatch.setattr("bot_modules.core.post_monitoring.discord.Member", FakeMember)


async def run(message, guarded=frozenset({GUARDED}), bypass=frozenset()):
    return await enforce_gif_guard(
        message, guarded_channel_ids=guarded, bypass_role_ids=bypass, log=log
    )


@pytest.mark.parametrize(
    "message",
    [
        pytest.param(FakeMessage("https://tenor.com/view/x-gif-1"), id="picker-link"),
        pytest.param(FakeMessage(attachments=[FakeAttachment("a.gif")]), id="upload"),
        pytest.param(
            FakeMessage(attachments=[FakeAttachment("a.webp", "image/webp", animated=True)]),
            id="animated-webp",
        ),
        pytest.param(FakeMessage("look", embeds=[FakeEmbed("gifv")]), id="gifv-preview"),
        pytest.param(
            FakeMessage("https://tenor.com/x", channel=FakeChannel(555, parent_id=GUARDED)),
            id="thread-of-guarded",
        ),
    ],
)
async def test_a_gif_in_a_guarded_channel_is_deleted_with_a_note(message):
    assert await run(message) is True
    assert message.deleted is True
    assert message.channel.sent == [(GIF_GUARD_NOTICE, {"delete_after": 5})]


@pytest.mark.parametrize(
    "message, guarded, bypass",
    [
        # Ships dark: an empty list touches nothing.
        pytest.param(FakeMessage("https://tenor.com/x"), frozenset(), frozenset(), id="off"),
        pytest.param(
            FakeMessage("https://tenor.com/x", channel=FakeChannel(OTHER)),
            frozenset({GUARDED}), frozenset(), id="unguarded-channel",
        ),
        pytest.param(
            FakeMessage("https://tenor.com/x", author=FakeMember([FakeRole(BYPASS_ROLE)])),
            frozenset({GUARDED}), frozenset({BYPASS_ROLE}), id="bypass-role",
        ),
        pytest.param(
            # on_message never reaches this gate for a bot author — but
            # on_message_edit calls straight in, so the gate has to skip
            # bots itself rather than lean on that upstream filter.
            FakeMessage("https://tenor.com/x", author=FakeMember(bot=True)),
            frozenset({GUARDED}), frozenset(), id="bot-author",
        ),
        pytest.param(
            FakeMessage("https://tenor.com/x", author=object()),
            frozenset({GUARDED}), frozenset(), id="non-member-author",
        ),
        pytest.param(
            FakeMessage("no gif here", attachments=[FakeAttachment("a.png", "image/png")]),
            frozenset({GUARDED}), frozenset(), id="not-a-gif",
        ),
    ],
)
async def test_everything_else_is_kept(message, guarded, bypass):
    assert await run(message, guarded, bypass) is False
    assert message.deleted is False
    assert message.channel.sent == []


def _http_error(cls):
    response = type("R", (), {"status": 403, "reason": "x"})()
    return cls(response, "nope")


@pytest.mark.parametrize("error_cls", [discord.Forbidden, discord.NotFound, discord.HTTPException])
async def test_a_failed_delete_is_logged_not_raised(error_cls):
    message = FakeMessage("https://tenor.com/x", delete_error=_http_error(error_cls))

    # Still the gate's message — the pipeline must not persist or reward it —
    # but no note claims a deletion that didn't happen.
    assert await run(message) is True
    assert message.channel.sent == []
