"""Contract for the shared branded-DM helper.

``brand_dm_embed`` is pure, so most of this is direct assertion. The send
path is exercised against a stub messageable rather than Discord mocks —
what matters is that a closed DM returns None instead of raising, since
several callers roll back database state on that signal.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from bot_modules.shared.dm_branding import (
    ATTRIBUTION_AUTHOR,
    ATTRIBUTION_FOOTER,
    ATTRIBUTION_NONE,
    brand_dm_embed,
    brand_dm_layout,
    guild_display_name,
    guild_icon_url,
    resolve_dm_accent,
    send_branded_dm,
)
from bot_modules.shared.embeds import DM_PRIMARY

ACCENT = discord.Color(0x5A32A8)


def _guild(name="Test Guild", icon_url="https://cdn.example/icon.png"):
    guild = MagicMock(spec=discord.Guild)
    guild.name = name
    if icon_url is None:
        guild.icon = None
    else:
        guild.icon = MagicMock()
        guild.icon.url = icon_url
    return guild


class _Sink:
    """Stub messageable recording the kwargs it was sent."""

    def __init__(self, raises=None):
        self.raises = raises
        self.sent = None

    async def send(self, **kwargs):
        if self.raises is not None:
            raise self.raises
        self.sent = kwargs
        return MagicMock(spec=discord.Message)


# ── brand_dm_embed: accent ───────────────────────────────────────────────


def test_accent_passthrough():
    embed = brand_dm_embed(discord.Embed(title="x"), guild_name="G", color=ACCENT)
    assert embed.color == ACCENT


def test_accent_falls_back_to_dm_primary():
    """An unbranded guild keeps today's DM look, not Discord-default grey."""
    embed = brand_dm_embed(discord.Embed(title="x"), guild_name="G")
    assert embed.color == discord.Color(DM_PRIMARY)


# ── brand_dm_embed: attribution placement ────────────────────────────────


def test_footer_attribution_is_the_default():
    embed = brand_dm_embed(
        discord.Embed(title="x"), guild_name="Meadow", guild_icon_url="u"
    )
    assert embed.footer.text == "Meadow"
    assert embed.footer.icon_url == "u"


def test_footer_attribution_preserves_an_existing_footer():
    """Feature-specific footer copy outranks ours, so the guild trails it."""
    base = discord.Embed(title="x")
    base.set_footer(text="DM relationships are logged for audit transparency.")
    embed = brand_dm_embed(base, guild_name="Meadow")
    assert embed.footer.text == (
        "DM relationships are logged for audit transparency. • Meadow"
    )


def test_author_placement_sets_the_author_slot():
    embed = brand_dm_embed(
        discord.Embed(title="x"),
        guild_name="Meadow",
        guild_icon_url="u",
        placement=ATTRIBUTION_AUTHOR,
    )
    assert embed.author.name == "Meadow"
    assert embed.author.icon_url == "u"
    assert embed.footer.text is None


def test_author_placement_does_not_clobber_an_existing_author():
    """dm_perms puts the requesting member here; footer placement protects it."""
    base = discord.Embed(title="x")
    base.set_author(name="RequestingMember", icon_url="avatar")
    embed = brand_dm_embed(base, guild_name="Meadow", placement=ATTRIBUTION_FOOTER)
    assert embed.author.name == "RequestingMember"
    assert embed.footer.text == "Meadow"


def test_keep_color_preserves_a_semantic_color():
    """CLR_SUCCESS on a release notice is meaning, not branding."""
    base = discord.Embed(title="Released", color=discord.Color.green())
    embed = brand_dm_embed(
        base, guild_name="Meadow", color=ACCENT, keep_color=True
    )
    assert embed.color == discord.Color.green()
    assert embed.footer.text == "Meadow"


def test_keep_color_still_accents_an_uncolored_embed():
    """Nothing semantic to protect, so the accent applies as normal."""
    embed = brand_dm_embed(
        discord.Embed(title="x"), guild_name="Meadow", color=ACCENT, keep_color=True
    )
    assert embed.color == ACCENT


def test_attribution_none_applies_accent_only():
    embed = brand_dm_embed(
        discord.Embed(title="x"),
        guild_name="Meadow",
        color=ACCENT,
        placement=ATTRIBUTION_NONE,
    )
    assert embed.color == ACCENT
    assert embed.footer.text is None
    assert embed.author.name is None


# ── brand_dm_embed: missing guild ────────────────────────────────────────


@pytest.mark.parametrize("placement", [ATTRIBUTION_FOOTER, ATTRIBUTION_AUTHOR])
def test_no_guild_name_still_accents_but_skips_attribution(placement):
    """Bot kicked mid-flight: a DM with no server name beats no DM."""
    embed = brand_dm_embed(
        discord.Embed(title="x"), guild_name=None, color=ACCENT, placement=placement
    )
    assert embed.color == ACCENT
    assert embed.footer.text is None
    assert embed.author.name is None


def test_guild_name_and_icon_tolerate_incomplete_guild_objects():
    """Attribution is a nicety; a missing attribute must not break the send."""
    bare = SimpleNamespace()
    assert guild_display_name(bare) is None
    assert guild_icon_url(bare) is None
    assert guild_display_name(None) is None
    assert guild_display_name(_guild(name="Meadow")) == "Meadow"


def test_guild_icon_url_handles_no_icon_and_no_guild():
    assert guild_icon_url(None) is None
    assert guild_icon_url(_guild(icon_url=None)) is None
    assert guild_icon_url(_guild()) == "https://cdn.example/icon.png"


# ── resolve_dm_accent ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_resolve_accent_defers_to_shared_resolver():
    guild = _guild()
    with patch(
        "bot_modules.core.branding.resolve_accent_color",
        AsyncMock(return_value=ACCENT),
    ) as resolver:
        assert await resolve_dm_accent(Path("db"), guild) == ACCENT
    resolver.assert_awaited_once()


@pytest.mark.asyncio
async def test_resolve_accent_survives_a_failing_lookup():
    """A branding failure must not cost the member the message."""
    with patch(
        "bot_modules.core.branding.resolve_accent_color",
        AsyncMock(side_effect=RuntimeError("avatar fetch exploded")),
    ):
        assert await resolve_dm_accent(Path("db"), _guild()) == discord.Color(
            DM_PRIMARY
        )


@pytest.mark.asyncio
async def test_resolve_accent_propagates_cancellation():
    """Swallowing CancelledError would break task shutdown."""
    with patch(
        "bot_modules.core.branding.resolve_accent_color",
        AsyncMock(side_effect=asyncio.CancelledError()),
    ):
        with pytest.raises(asyncio.CancelledError):
            await resolve_dm_accent(Path("db"), _guild())


@pytest.mark.asyncio
async def test_resolve_accent_without_a_db_path_skips_the_lookup():
    with patch(
        "bot_modules.core.branding.resolve_accent_color", AsyncMock()
    ) as resolver:
        assert await resolve_dm_accent(None, _guild()) == discord.Color(DM_PRIMARY)
    resolver.assert_not_awaited()


@pytest.mark.asyncio
async def test_resolve_accent_without_a_guild_skips_the_db():
    with patch(
        "bot_modules.core.branding.resolve_accent_color", AsyncMock()
    ) as resolver:
        assert await resolve_dm_accent(Path("db"), None) == discord.Color(DM_PRIMARY)
    resolver.assert_not_awaited()


# ── send_branded_dm ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_send_brands_the_embed_and_passes_extras_through():
    sink, view = _Sink(), MagicMock()
    with patch(
        "bot_modules.core.branding.resolve_accent_color",
        AsyncMock(return_value=ACCENT),
    ):
        msg = await send_branded_dm(
            sink,
            db_path=Path("db"),
            guild=_guild(name="Meadow"),
            embed=discord.Embed(title="hi"),
            view=view,
        )
    assert msg is not None
    assert sink.sent["embed"].color == ACCENT
    assert sink.sent["embed"].footer.text == "Meadow"
    assert sink.sent["view"] is view


@pytest.mark.asyncio
async def test_send_leaves_content_only_dms_unbranded():
    """Branding applies to embeds; a text DM has nowhere to put an accent."""
    sink = _Sink()
    with patch(
        "bot_modules.core.branding.resolve_accent_color", AsyncMock()
    ) as resolver:
        await send_branded_dm(
            sink, db_path=Path("db"), guild=_guild(), content="plain text"
        )
    assert sink.sent == {"content": "plain text"}
    resolver.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "exc",
    [
        discord.Forbidden(MagicMock(status=403), "closed DMs"),
        discord.HTTPException(MagicMock(status=500), "boom"),
    ],
    ids=["forbidden", "http-error"],
)
async def test_send_returns_none_when_the_dm_bounces(exc):
    """Callers roll back DB state on None, so this must not raise."""
    with patch(
        "bot_modules.core.branding.resolve_accent_color",
        AsyncMock(return_value=ACCENT),
    ):
        result = await send_branded_dm(
            _Sink(raises=exc),
            db_path=Path("db"),
            guild=_guild(),
            embed=discord.Embed(title="hi"),
        )
    assert result is None


# ── brand_dm_layout: the V2 half ─────────────────────────────────────────
#
# A Components V2 card has no embed and no footer, so the two halves of the
# attribution move onto components: the icon becomes a Section accessory and
# the server name a ``-#`` subtext line where the footer used to read.


def _card(*items: discord.ui.Item) -> discord.ui.LayoutView:
    view = discord.ui.LayoutView(timeout=None)
    for item in items:
        view.add_item(item)
    return view


def _texts(view: discord.ui.LayoutView) -> list[str]:
    return [
        item.content
        for item in view.walk_children()
        if isinstance(item, discord.ui.TextDisplay)
    ]


def test_layout_attribution_wraps_a_bare_opening_text_display():
    body = discord.ui.TextDisplay("## Your perks")
    view = brand_dm_layout(
        _card(body, discord.ui.Separator()),
        guild_name="Test Guild",
        guild_icon_url="https://cdn.example/icon.png",
    )

    first = view.children[0]
    assert isinstance(first, discord.ui.Section)
    assert first.children == [body]
    assert isinstance(first.accessory, discord.ui.Thumbnail)
    assert first.accessory.media.url == "https://cdn.example/icon.png"


def test_layout_attribution_keeps_the_rest_of_the_card_in_order():
    view = brand_dm_layout(
        _card(
            discord.ui.TextDisplay("## Your perks"),
            discord.ui.TextDisplay("Palette"),
            discord.ui.TextDisplay("Voice lease"),
        ),
        guild_name="Test Guild",
        guild_icon_url="https://cdn.example/icon.png",
    )
    assert _texts(view) == [
        "## Your perks",
        "Palette",
        "Voice lease",
        "-# From Test Guild",
    ]


def test_layout_attribution_adds_the_name_as_a_subtext_line():
    view = brand_dm_layout(_card(discord.ui.Separator()), guild_name="Test Guild")
    assert _texts(view) == ["-# From Test Guild"]


def test_layout_attribution_leaves_an_existing_section_accessory_alone():
    """A Section's accessory is mandatory, so the builder already chose it —
    claiming it here would silently delete that button."""
    button = discord.ui.Button(label="Manage", custom_id="manage")
    section = discord.ui.Section(
        discord.ui.TextDisplay("## Your perks"), accessory=button
    )
    view = brand_dm_layout(
        _card(section),
        guild_name="Test Guild",
        guild_icon_url="https://cdn.example/icon.png",
    )
    assert view.children[0] is section
    assert section.accessory is button
    assert _texts(view)[-1] == "-# From Test Guild"


def test_layout_attribution_without_a_guild_changes_nothing():
    body = discord.ui.TextDisplay("## Your perks")
    view = brand_dm_layout(_card(body))
    assert view.children == [body]


def test_layout_attribution_costs_at_most_three_components():
    """The wrap is a Section plus its Thumbnail; the name line is one more."""
    before = _card(discord.ui.TextDisplay("## Your perks"))
    grown = brand_dm_layout(
        before, guild_name="Test Guild", guild_icon_url="https://cdn.example/icon.png"
    )
    assert sum(1 for _ in grown.walk_children()) == 1 + 3


# ── send_branded_dm(layout=...) ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_send_brands_a_layout_and_passes_it_as_the_view():
    sink = _Sink()
    layout = _card(discord.ui.TextDisplay("## Your perks"))

    result = await send_branded_dm(
        sink, db_path=None, guild=_guild(), layout=layout
    )

    assert result is not None
    assert sink.sent == {"view": layout}
    assert _texts(layout)[-1] == "-# From Test Guild"


@pytest.mark.asyncio
async def test_send_with_a_layout_never_resolves_an_accent():
    """A V2 card has no colour at all, so the db read and the avatar fetch
    behind it are pure cost on a DM that matters."""
    sink = _Sink()
    with patch(
        "bot_modules.shared.dm_branding.resolve_dm_accent", new=AsyncMock()
    ) as resolver:
        await send_branded_dm(
            sink,
            db_path=Path("/nope.db"),
            guild=_guild(),
            layout=_card(discord.ui.TextDisplay("hi")),
        )
    resolver.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "extra",
    [
        pytest.param({"embed": discord.Embed(title="x")}, id="embed"),
        pytest.param({"content": "hi"}, id="content"),
        pytest.param({"view": discord.ui.View()}, id="view"),
    ],
)
async def test_send_refuses_a_layout_beside_an_embed_content_or_view(extra):
    with pytest.raises(ValueError):
        await send_branded_dm(
            _Sink(),
            db_path=None,
            guild=_guild(),
            layout=_card(discord.ui.TextDisplay("hi")),
            **extra,
        )


@pytest.mark.asyncio
async def test_send_returns_none_when_a_layout_dm_bounces():
    sink = _Sink(raises=discord.Forbidden(MagicMock(status=403), "closed"))
    result = await send_branded_dm(
        sink,
        db_path=None,
        guild=_guild(),
        layout=_card(discord.ui.TextDisplay("hi")),
    )
    assert result is None
