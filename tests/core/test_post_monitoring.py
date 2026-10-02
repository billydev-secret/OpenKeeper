"""Spoiler enforcement — every unspoilered image in an NSFW channel goes.

The gate reads only the filename and Discord's spoiler flag. It used to ask an
image classifier first and delete only what scored as explicit; that was
removed (docs/specs/post_monitoring_spec.md), and these tests pin the plain rule
and the guards around it. The scope is Discord's NSFW flag alone, with no
bot-side channel list.
"""
from __future__ import annotations

import logging

import pytest

from bot_modules.core.post_monitoring import (
    IMAGE_EXTENSIONS,
    SPOILER_IMAGE_EXTENSIONS,
    attachment_is_image,
    enforce_spoiler_requirement,
)

log = logging.getLogger("test")

BYPASS_ROLE = 900


class FakeRole:
    def __init__(self, role_id: int) -> None:
        self.id = role_id


class FakeAttachment:
    def __init__(
        self,
        filename: str = "pic.png",
        spoiler: bool = False,
        content_type: str | None = "auto",
    ) -> None:
        self.filename = filename
        # Mirror Discord: the content type follows the file, so a .txt upload
        # doesn't claim to be an image.
        if content_type == "auto":
            content_type = (
                "image/png"
                if filename.lower().endswith(SPOILER_IMAGE_EXTENSIONS)
                else "text/plain"
            )
        self.content_type = content_type
        self._spoiler = spoiler

    def is_spoiler(self) -> bool:
        return self._spoiler


class FakeCategory:
    def __init__(self, nsfw: bool) -> None:
        self._nsfw = nsfw

    def is_nsfw(self) -> bool:
        return self._nsfw


class FakeChannel:
    def __init__(self, nsfw: bool = True, category: FakeCategory | None = None) -> None:
        self.id = 100
        self.name = "chan"
        self.category = category
        self._nsfw = nsfw
        self.sent: list[str] = []

    def is_nsfw(self) -> bool:
        return self._nsfw

    async def send(self, content: str, **_kwargs) -> None:
        self.sent.append(content)


class FakeMember:
    """A discord.Member for isinstance purposes — see the patch below."""

    def __init__(self, roles: list[FakeRole] | None = None) -> None:
        self.id = 42
        self.roles = roles or []

    def __str__(self) -> str:
        return "member#0001"


class FakeMessage:
    def __init__(
        self,
        *,
        attachments: list[FakeAttachment],
        channel: object | None = None,
        author: object | None = None,
    ) -> None:
        self.attachments = attachments
        self.channel = channel if channel is not None else FakeChannel()
        self.author = author if author is not None else FakeMember()
        self.content = "look at this"
        self.deleted = False

    async def delete(self) -> None:
        self.deleted = True


@pytest.fixture(autouse=True)
def _member_isinstance(monkeypatch):
    """Let FakeMember satisfy the isinstance(discord.Member) guard."""
    monkeypatch.setattr(
        "bot_modules.core.post_monitoring.discord.Member", FakeMember
    )


async def run(message, bypass=frozenset()):
    return await enforce_spoiler_requirement(
        message,
        bypass_role_ids=bypass,
        log=log,
    )


@pytest.mark.asyncio
async def test_unspoilered_image_is_deleted_and_the_member_told_why():
    message = FakeMessage(attachments=[FakeAttachment()])

    assert await run(message) is True
    assert message.deleted is True
    assert message.channel.sent


@pytest.mark.asyncio
async def test_spoilered_image_is_kept():
    message = FakeMessage(attachments=[FakeAttachment(spoiler=True)])

    assert await run(message) is False
    assert message.deleted is False
    assert message.channel.sent == []


@pytest.mark.asyncio
async def test_one_spoilered_attachment_does_not_clear_an_unspoilered_one():
    message = FakeMessage(
        attachments=[FakeAttachment("a.png", spoiler=True), FakeAttachment("b.png")]
    )

    assert await run(message) is True
    assert message.deleted is True


@pytest.mark.asyncio
async def test_a_channel_in_an_nsfw_category_counts():
    # Discord doesn't cascade the category flag onto channels; the gate reads both.
    channel = FakeChannel(nsfw=False, category=FakeCategory(nsfw=True))
    message = FakeMessage(attachments=[FakeAttachment()], channel=channel)

    assert await run(message) is True
    assert message.deleted is True


class _UnreadableChannel(FakeChannel):
    def is_nsfw(self) -> bool:
        raise RuntimeError("partial channel")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "channel",
    [
        # The complaint this rule answers: nothing is removed from SFW channels.
        pytest.param(FakeChannel(nsfw=False), id="sfw-channel"),
        pytest.param(
            FakeChannel(nsfw=False, category=FakeCategory(nsfw=False)), id="sfw-category"
        ),
        # A flag that can't be read must not delete a member's post.
        pytest.param(_UnreadableChannel(), id="unreadable-flag"),
    ],
)
async def test_channels_not_marked_nsfw_are_untouched(channel):
    message = FakeMessage(attachments=[FakeAttachment()], channel=channel)

    assert await run(message) is False
    assert message.deleted is False
    assert channel.sent == []


@pytest.mark.asyncio
async def test_bypass_role_still_bypasses():
    message = FakeMessage(
        attachments=[FakeAttachment()],
        author=FakeMember(roles=[FakeRole(BYPASS_ROLE)]),
    )

    assert await run(message, bypass={BYPASS_ROLE}) is False
    assert message.deleted is False


@pytest.mark.asyncio
async def test_non_member_author_is_ignored():
    message = FakeMessage(attachments=[FakeAttachment()], author=object())

    assert await run(message) is False
    assert message.deleted is False


@pytest.mark.asyncio
async def test_message_without_attachments_is_ignored():
    message = FakeMessage(attachments=[])

    assert await run(message) is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "filename",
    [
        pytest.param("notes.txt", id="not-an-image"),
        # Images, but outside the narrow deletable set.
        pytest.param("scan.bmp", id="bmp"),
        pytest.param("scan.tiff", id="tiff"),
    ],
)
async def test_attachments_outside_the_deletable_set_are_ignored(filename):
    message = FakeMessage(attachments=[FakeAttachment(filename)])

    assert await run(message) is False
    assert message.deleted is False


def test_spoiler_extensions_are_a_strict_subset():
    # Spoiler enforcement deletes what it matches, so its list is narrower
    # than the general image predicate on purpose. Adding a format to
    # IMAGE_EXTENSIONS must not silently make it deletable, and collapsing the
    # two must not happen as a "tidy-up".
    assert set(SPOILER_IMAGE_EXTENSIONS) < set(IMAGE_EXTENSIONS)
    assert ".bmp" not in SPOILER_IMAGE_EXTENSIONS
    assert ".tiff" not in SPOILER_IMAGE_EXTENSIONS


@pytest.mark.parametrize(
    ("filename", "content_type", "expected"),
    [
        pytest.param("a.png", "image/png", True, id="content-type"),
        pytest.param("a.PNG", None, True, id="extension-without-content-type"),
        pytest.param("a.tiff", None, True, id="tiff-counts-for-xp"),
        pytest.param("blob", "image/heic", True, id="content-type-without-extension"),
        pytest.param("a.txt", "text/plain", False, id="not-an-image"),
    ],
)
def test_attachment_is_image(filename, content_type, expected):
    assert attachment_is_image(FakeAttachment(filename, content_type=content_type)) is expected
