"""``core.card_art.read_card_art``: the one read of the card strips for a
public card (Ben, 2026-09-30) — no guild, no art; otherwise the guild's own
card header and footer, from ``branding_config``."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from bot_modules.core import branding_service as bs
from bot_modules.core.card_art import NO_ART, read_card_art
from bot_modules.core.db_utils import open_db
from tests.db_template import migrated_db

GUILD, OTHER = 5151, 6262
HEADER = "https://dash.example/static/panel-banners/" + "c" * 32 + ".webp"
FOOTER = "https://dash.example/static/panel-banners/" + "d" * 32 + ".webp"


@pytest.fixture
def ctx(tmp_path):
    path = tmp_path / "card_art.db"
    migrated_db(path)
    cfg = bs.get_branding(path, GUILD)
    cfg.panel_card_header_url, cfg.panel_card_footer_url = HEADER, FOOTER
    bs.upsert_branding(path, cfg)
    return SimpleNamespace(open_db=lambda: open_db(path))


def _read(ctx, guild):
    return asyncio.run(read_card_art(ctx, guild))


def test_no_guild_reads_no_art(ctx):
    assert _read(ctx, None) is NO_ART


def test_reads_the_guilds_card_strips(ctx):
    art = _read(ctx, SimpleNamespace(id=GUILD))
    assert (art.card_header, art.card_footer) == (HEADER, FOOTER)


def test_another_guild_reads_none(ctx):
    art = _read(ctx, SimpleNamespace(id=OTHER))
    assert (art.card_header, art.card_footer) == (None, None)


def test_a_failed_read_falls_back_to_the_plain_card():
    """The strips are decoration: a broken read must never cost the post."""

    def _boom():
        raise RuntimeError("db gone")

    assert _read(SimpleNamespace(open_db=_boom), SimpleNamespace(id=GUILD)) is NO_ART


def test_no_context_reads_no_art():
    """A caller holding no app context (the theme restamp's bare client)
    gets the plain card without a read being attempted."""
    assert _read(None, SimpleNamespace(id=GUILD)) is NO_ART
