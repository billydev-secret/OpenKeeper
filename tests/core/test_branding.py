"""Tests for the shared embed helpers in ``bot_modules.core.branding``.

``apply_section_spacing`` is the Embed-layer equivalent of the trailing
zero-width spacer the login digest and weekly leaderboard apply at their
string layer: it evens out a stacked-field embed's vertical rhythm so a
section heading doesn't hug the section above it.
"""

from __future__ import annotations

import ast
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

from bot_modules.core import branding
from bot_modules.core.branding import (
    BACKTICK_STAND_IN,
    SECTION_SPACER,
    apply_section_spacing,
    code_safe,
    escape_to_fit,
    trim_escaped,
)
from tests.paths import REPO_ROOT


def _embed(*values: str) -> discord.Embed:
    embed = discord.Embed(title="t")
    for i, v in enumerate(values):
        embed.add_field(name=f"F{i}", value=v, inline=False)
    return embed


def test_spacer_is_a_zero_width_line():
    # A newline plus U+200B — a printable char Discord won't strip as
    # trailing whitespace, so it renders as one extra empty line.
    assert SECTION_SPACER == "\n​"


def test_appends_spacer_to_every_field_but_the_last():
    embed = apply_section_spacing(_embed("a", "b", "c"))
    assert [f.value.endswith(SECTION_SPACER) for f in embed.fields] == [
        True,
        True,
        False,
    ]


def test_preserves_field_name_and_inline_flag():
    embed = discord.Embed(title="t")
    embed.add_field(name="First", value="a", inline=False)
    embed.add_field(name="Second", value="b", inline=False)
    apply_section_spacing(embed)
    assert embed.fields[0].name == "First"
    assert embed.fields[0].inline is False
    assert embed.fields[0].value == "a" + SECTION_SPACER


def test_inline_fields_are_skipped():
    """An inline field has no heading below it — it sits beside its neighbours.

    Spacing one only makes its box taller, so a three-across row would carry
    dead height on every card. Skipping them is what lets the helper be applied
    to every builder rather than only the all-stacked ones (ruling 2026-09-03).
    """
    embed = discord.Embed(title="t")
    embed.add_field(name="Host", value="a", inline=True)
    embed.add_field(name="Hot Seat", value="b", inline=True)
    embed.add_field(name="Mode", value="c", inline=True)
    embed.add_field(name="Rules", value="d", inline=False)
    embed.add_field(name="Players", value="e", inline=False)

    apply_section_spacing(embed)

    assert [f.value for f in embed.fields[:3]] == ["a", "b", "c"]
    # The stacked section before the last one still gets its breathing room.
    assert embed.fields[3].value == "d" + SECTION_SPACER
    assert embed.fields[4].value == "e"


def test_a_card_of_only_inline_fields_is_untouched():
    embed = discord.Embed(title="t")
    for name, value in (("Yes", "3"), ("No", "1"), ("Abstain", "0")):
        embed.add_field(name=name, value=value, inline=True)

    apply_section_spacing(embed)

    assert [f.value for f in embed.fields] == ["3", "1", "0"]


def test_a_field_at_the_value_cap_is_left_alone():
    """``fit_lines`` and a raw ``value[:1024]`` slice both fill a field right up
    to Discord's cap. Two more characters there make Discord reject the whole
    embed — losing the card entirely to buy two pixels of air."""
    embed = discord.Embed(title="t")
    embed.add_field(name="Recent Activity", value="x" * 1024, inline=False)
    embed.add_field(name="Wallet", value="y", inline=False)

    apply_section_spacing(embed)

    assert embed.fields[0].value == "x" * 1024
    # One char of headroom is still not enough for the two-char spacer.
    embed.set_field_at(0, name="Recent Activity", value="x" * 1023, inline=False)
    apply_section_spacing(embed)
    assert embed.fields[0].value == "x" * 1023
    # With room for it, the spacer lands as usual.
    embed.set_field_at(0, name="Recent Activity", value="x" * 1022, inline=False)
    apply_section_spacing(embed)
    assert embed.fields[0].value == "x" * 1022 + SECTION_SPACER


def test_single_field_is_untouched():
    embed = apply_section_spacing(_embed("only"))
    assert embed.fields[0].value == "only"


def test_empty_embed_is_a_no_op():
    embed = apply_section_spacing(discord.Embed(title="t"))
    assert len(embed.fields) == 0


def test_is_idempotent():
    embed = _embed("a", "b", "c")
    apply_section_spacing(embed)
    apply_section_spacing(embed)
    # Re-applying must not stack a second spacer on already-spaced fields.
    assert embed.fields[0].value == "a" + SECTION_SPACER
    assert embed.fields[1].value == "b" + SECTION_SPACER
    assert embed.fields[2].value == "c"


def test_returns_the_same_embed_for_chaining():
    embed = _embed("a", "b")
    assert apply_section_spacing(embed) is embed


# ── escape_to_fit / trim_escaped ──────────────────────────────────────
#
# These exist because the 2026-09-20 escaping sweep introduced a bug of its
# own: escaping a value without re-fitting its cap. ``escape_markdown``
# roughly doubles a markdown-heavy string, so a title capped at 200 when it
# was written — safely under a 256-char surface — escapes to 400 and Discord
# rejects the whole embed. The fix had to *reach* the input the escape existed
# to defend against, which is exactly the input that overflows.


def test_short_text_is_escaped_but_not_trimmed():
    assert escape_to_fit("a*b", 256) == "a\\*b"


def test_text_with_no_markdown_is_returned_unchanged():
    assert escape_to_fit("plain text", 256) == "plain text"


@pytest.mark.parametrize("limit", [16, 100, 256, 1024, 2000, 4096])
def test_an_all_markdown_string_never_exceeds_its_limit(limit):
    """The doubling case, at every cap the repo actually uses.

    A raw string of ``limit`` markdown characters escapes to ``2 * limit``.
    This is the shape that 400s a send, so it is asserted at each surface's
    real ceiling rather than one representative number.
    """
    assert len(escape_to_fit("*" * limit, limit)) <= limit


def test_a_trim_never_leaves_a_dangling_backslash():
    """A naive slice can cut between a backslash and what it escapes.

    The orphan would then escape the ellipsis, so the same value trimmed one
    character earlier would render differently. Every cut position is checked
    because which ones split a pair depends on where the markdown falls.
    """
    escaped = discord.utils.escape_markdown("*" * 60)
    for limit in range(2, len(escaped)):
        out = trim_escaped(escaped, limit)
        assert len(out) <= limit
        body = out.removesuffix("…")
        assert (len(body) - len(body.rstrip("\\"))) % 2 == 0, (
            f"limit={limit} left an odd run of backslashes: {out!r}"
        )


def test_trimming_marks_that_it_trimmed():
    assert escape_to_fit("abcdef", 4).endswith("…")


def test_an_exactly_fitting_value_is_not_marked():
    """Off-by-one here silently eats a character from every honest answer."""
    assert escape_to_fit("abcd", 4) == "abcd"


def test_a_caller_can_pick_its_own_ellipsis():
    # jail's policy list appends its own marker; the helper must not assume.
    assert escape_to_fit("abcdef", 5, ellipsis="...") == "ab..."


@pytest.mark.parametrize("limit", [-5, 0, 1, 2])
def test_a_budget_too_small_for_the_marker_still_honours_the_budget(limit):
    """The degenerate case a prefix-aware caller can actually reach.

    Callers fit a title by subtracting the bot's own prefix from the surface
    cap — ``escape_to_fit(title, EMBED_TITLE_LIMIT - len(prefix))`` — so a
    long enough prefix drives the limit to zero or below. Returning
    ``cut + "…"`` there would hand back a value *longer* than the limit it
    was asked to respect, turning the helper that prevents 400s into one that
    causes them.
    """
    out = escape_to_fit("*" * 50, limit, ellipsis="...")
    assert len(out) <= max(0, limit)
    assert not out.endswith("\\")


def test_trim_escaped_does_not_escape_again():
    """The already-escaped half of the contract.

    A caller that escaped upstream — because it had to escape the member's
    half of a mixed string — must be able to fit the result without a second
    pass turning ``\\*`` into ``\\\\\\*`` and showing members a backslash.
    """
    assert trim_escaped("a\\*b", 256) == "a\\*b"


# ── code_safe ───────────────────────────────────────────
#
# The sweep's own blind spot. Every site above asks "does markdown render
# here?" and escapes if it does; a code span answers no, so the sweep walked
# past it — but a span is breakable in a way an ordinary string is not, and
# the breakout lands the rest of the member's text back outside the span
# where markdown *does* render.


def test_a_backtick_cannot_close_an_inline_span():
    assert "`" not in code_safe("ev`il")


def test_the_stand_in_is_not_markup():
    """The substitution has to be inert, or it just moves the problem.

    U+02BC is a modifier *letter*. Discord's parser has no rule for it, so a
    name full of them is a name full of letters — which is the whole reason
    it beats deleting the backtick (two different names would render alike)
    or backslash-escaping it (a span prints the backslash).
    """
    assert code_safe("`") == BACKTICK_STAND_IN
    assert BACKTICK_STAND_IN not in "*_~|`\\"


def test_ordinary_text_is_untouched():
    assert code_safe("a *b* c") == "a *b* c"


def test_fences_only_leaves_a_lone_backtick_alone():
    """The deliberate asymmetry, and the reason it is a keyword argument.

    Inside a ``` block a single backtick is inert, so rewriting it would edit
    a member's prose to no purpose — a fenced quote of member prose wants
    exactly that. An inline span has no such slack.
    """
    assert code_safe("a`b", fences_only=True) == "a`b"
    assert code_safe("a```b", fences_only=True) == f"a{BACKTICK_STAND_IN * 3}b"


# ── safe_resolve_accent ───────────────────────────────────────────────
#
# Twelve near-copies of this wrapper lived across the game cogs. They agreed on
# the shape — resolve the guild accent, and never let a branding hiccup crash a
# live game — but disagreed on the fallback, so ``default`` stayed per-caller.


class _Bot:
    def __init__(self, db_path="db.sqlite"):
        self.ctx = SimpleNamespace(db_path=db_path) if db_path else None


@pytest.mark.asyncio
async def test_resolves_through_to_the_real_accent(monkeypatch):
    guild = SimpleNamespace(id=7)
    monkeypatch.setattr(
        branding, "resolve_accent_color", AsyncMock(return_value=discord.Color(0x123456))
    )
    got = await branding.safe_resolve_accent(_Bot(), guild)  # type: ignore[arg-type]
    assert got == discord.Color(0x123456)
    branding.resolve_accent_color.assert_awaited_once_with("db.sqlite", guild)


@pytest.mark.asyncio
async def test_no_guild_returns_the_default_without_touching_the_db(monkeypatch):
    """A DM, or a channel whose .guild is None — nothing to brand."""
    monkeypatch.setattr(branding, "resolve_accent_color", AsyncMock())
    assert await branding.safe_resolve_accent(_Bot(), None) is None
    branding.resolve_accent_color.assert_not_awaited()


@pytest.mark.asyncio
async def test_bot_without_ctx_returns_the_default(monkeypatch):
    """Early startup, or a test double — no db_path to read branding from."""
    monkeypatch.setattr(branding, "resolve_accent_color", AsyncMock())
    assert await branding.safe_resolve_accent(_Bot(db_path=None), SimpleNamespace(id=7)) is None
    assert await branding.safe_resolve_accent(object(), SimpleNamespace(id=7)) is None
    branding.resolve_accent_color.assert_not_awaited()


@pytest.mark.asyncio
async def test_resolution_failure_falls_back_rather_than_raising(monkeypatch):
    """The whole reason this wrapper exists: an embed with the wrong color
    still beats a game that crashes building it."""
    monkeypatch.setattr(
        branding, "resolve_accent_color", AsyncMock(side_effect=RuntimeError("db gone"))
    )
    assert await branding.safe_resolve_accent(_Bot(), SimpleNamespace(id=7)) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    [
        pytest.param("no_guild", id="no-guild"),
        pytest.param("no_ctx", id="no-ctx"),
        pytest.param("raises", id="resolve-raises"),
    ],
)
async def test_every_failure_path_honours_a_custom_default(monkeypatch, case):
    """chicken, musical chairs and pressure cooker fall back to their own
    yellow rather than letting discord.py pick."""
    YELLOW = 0xF1C40F
    monkeypatch.setattr(
        branding,
        "resolve_accent_color",
        AsyncMock(side_effect=RuntimeError("db gone") if case == "raises" else None),
    )
    bot = _Bot(db_path=None) if case == "no_ctx" else _Bot()
    guild = None if case == "no_guild" else SimpleNamespace(id=7)
    assert await branding.safe_resolve_accent(bot, guild, default=YELLOW) == YELLOW


@pytest.mark.asyncio
async def test_failure_is_logged_at_warning_under_the_callers_label(monkeypatch, caplog):
    """WARNING, not debug: the root logger runs at INFO, so a debug line here
    would be invisible in production and a branding table that has started
    raising would strip every embed's accent with no evidence anywhere."""
    monkeypatch.setattr(
        branding, "resolve_accent_color", AsyncMock(side_effect=RuntimeError("db gone"))
    )
    with caplog.at_level(logging.DEBUG, logger="bot_modules.core.branding"):
        await branding.safe_resolve_accent(
            _Bot(), SimpleNamespace(id=7), log_label="pressure"
        )
    (record,) = [r for r in caplog.records if r.name == "bot_modules.core.branding"]
    assert record.levelno == logging.WARNING
    assert "pressure" in record.getMessage()
    assert record.exc_info is not None  # the traceback rides along


@pytest.mark.asyncio
async def test_no_guild_stays_silent(monkeypatch, caplog):
    """A DM is ordinary — there is nothing to brand and nothing to report."""
    monkeypatch.setattr(branding, "resolve_accent_color", AsyncMock())
    with caplog.at_level(logging.DEBUG, logger="bot_modules.core.branding"):
        await branding.safe_resolve_accent(_Bot(), None)
    assert [r for r in caplog.records if r.name == "bot_modules.core.branding"] == []


@pytest.mark.asyncio
async def test_a_source_with_no_db_path_is_reported(monkeypatch, caplog):
    """Almost always the wrong object, and it used to raise.

    Before this helper existed, ``safe_resolve_accent(self, ...)`` from a cog
    that keeps its context on ``self.bot`` was an AttributeError you couldn't
    miss. Returning the default silently would turn that typo into an embed
    that is permanently unbranded and never complains, which neither ruff nor
    pyright can see (``source`` is deliberately untyped).
    """
    monkeypatch.setattr(branding, "resolve_accent_color", AsyncMock())
    with caplog.at_level(logging.DEBUG, logger="bot_modules.core.branding"):
        got = await branding.safe_resolve_accent(_Bot(db_path=None), SimpleNamespace(id=7))
    assert got is None  # still degrades rather than raising
    (record,) = [r for r in caplog.records if r.name == "bot_modules.core.branding"]
    assert record.levelno == logging.WARNING


@pytest.mark.asyncio
async def test_an_explicit_none_source_stays_silent(monkeypatch, caplog):
    """Passing None says "I know I have no context" — that isn't a mistake."""
    monkeypatch.setattr(branding, "resolve_accent_color", AsyncMock())
    with caplog.at_level(logging.DEBUG, logger="bot_modules.core.branding"):
        await branding.safe_resolve_accent(None, SimpleNamespace(id=7))
    assert [r for r in caplog.records if r.name == "bot_modules.core.branding"] == []


# ── adoption: nothing calls the raw resolver ──────────────────────────

# ``resolve_accent_color`` raises. Every caller that isn't prepared to handle
# that wants ``safe_resolve_accent``, and as of the sweep every caller went
# through it — 121 call sites across cogs, views, commands, duels, services,
# background loops and the dashboard routes. This keeps it that way.
#
# core/branding.py is the one legitimate caller: it *is* the wrapper.
RESOLVER_HOME = "src/bot_modules/core/branding.py"


def _repo_root() -> Path:
    return REPO_ROOT


def _direct_calls(tree: ast.AST) -> list[int]:
    """Line numbers of real ``resolve_accent_color(...)`` calls.

    Parsed, not grepped: prose in a docstring naming the function is not a
    call, and ``branding.resolve_accent_color(...)`` is one even though it
    doesn't start with the bare name.
    """
    lines = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = (
            func.id if isinstance(func, ast.Name)
            else func.attr if isinstance(func, ast.Attribute)
            else None
        )
        if name == "resolve_accent_color":
            lines.append(node.lineno)
    return lines


def test_nothing_outside_branding_calls_the_raw_resolver():
    """A raise here reaches a live game, a background loop, or an HTTP handler.

    If you're adding a new embed: call ``safe_resolve_accent(source, guild)``.
    ``source`` is whatever you have — a bot, an AppContext, or a db_path. Pass
    ``default=DEFAULT_ACCENT_COLOR`` when you need a non-optional Color.
    """
    root = _repo_root()
    offenders: list[str] = []
    for path in sorted(root.joinpath("src").rglob("*.py")):
        rel = path.relative_to(root).as_posix()
        if rel == RESOLVER_HOME:
            continue
        source = path.read_text(encoding="utf-8")
        if "resolve_accent_color" not in source:  # cheap skip
            continue
        for lineno in _direct_calls(ast.parse(source)):
            offenders.append(f"{rel}:{lineno}")

    assert not offenders, (
        "These call resolve_accent_color directly, so a branding failure "
        "raises into them. Use safe_resolve_accent instead:\n  "
        + "\n  ".join(offenders)
    )


def test_the_sweep_can_actually_see_a_violation():
    """Guards the guard. The check is only worth having if a reintroduced
    direct call — bare *or* module-qualified — is actually detected."""
    bare = ast.parse("x = await resolve_accent_color(db_path, guild)")
    qualified = ast.parse("x = await branding.resolve_accent_color(db_path, guild)")
    prose = ast.parse('"""Use resolve_accent_color(db_path, guild) for this."""')
    wrapped = ast.parse("x = await safe_resolve_accent(ctx, guild)")

    assert _direct_calls(bare) == [1]
    assert _direct_calls(qualified) == [1]
    assert _direct_calls(prose) == []  # a docstring is not a call
    assert _direct_calls(wrapped) == []


def test_the_wrapper_still_lives_where_the_sweep_left_it():
    """Guards the exemption above: if branding.py moves, the sweep test would
    silently start exempting nothing (or the wrong file)."""
    assert _repo_root().joinpath(RESOLVER_HOME).is_file()


# ── prime_accent_cache ────────────────────────────────────────────────
#
# Both hot-potato cogs resolve a game's accent once and reuse it for every
# subsequent embed edit. What's worth pinning is the miss policy: a failure
# must leave the key unset, not cache a fallback.


@pytest.mark.asyncio
async def test_a_resolved_accent_is_cached(monkeypatch):
    monkeypatch.setattr(
        branding, "resolve_accent_color", AsyncMock(return_value=discord.Color(0x00FF00))
    )
    cache: dict = {}
    await branding.prime_accent_cache(
        cache, 7, _Bot(), SimpleNamespace(id=1), log_label="hot potato"
    )
    assert cache == {7: discord.Color(0x00FF00)}


@pytest.mark.asyncio
async def test_an_already_primed_key_is_not_resolved_again(monkeypatch):
    monkeypatch.setattr(branding, "resolve_accent_color", AsyncMock())
    cache = {7: discord.Color(0x123456)}
    await branding.prime_accent_cache(
        cache, 7, _Bot(), SimpleNamespace(id=1), log_label="x"
    )
    branding.resolve_accent_color.assert_not_awaited()
    assert cache == {7: discord.Color(0x123456)}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    [
        pytest.param("raises", id="resolve-fails"),
        pytest.param("no_guild", id="no-guild"),
        pytest.param("no_ctx", id="no-ctx"),
    ],
)
async def test_a_failure_leaves_the_key_unset(monkeypatch, case):
    """Not cached-as-fallback: the render's own default applies now, and a
    later prime can still succeed. Caching the fallback would pin a wrong
    colour for the life of the game on one transient hiccup."""
    monkeypatch.setattr(
        branding,
        "resolve_accent_color",
        AsyncMock(side_effect=RuntimeError("boom") if case == "raises" else None),
    )
    cache: dict = {}
    await branding.prime_accent_cache(
        cache,
        7,
        _Bot(db_path=None) if case == "no_ctx" else _Bot(),
        None if case == "no_guild" else SimpleNamespace(id=1),
        log_label="hot potato",
    )
    assert cache == {}
