"""Duel/lobby cards: the Components V2 receipts that replaced every terminal
off-ramp, and now (wave 3b, "Duels: live lobby and challenge cards") the two
*live* cards too.

Renamed from ``test_duels_embeds.py`` when the shared duel layer (not the six
mini-game cogs — that was wave 3b's separate job) converted its terminal
notices to Components V2. ``BaseGame._render_lobby`` and
``BaseDuel._build_challenge_embed`` no longer exist — every mini-game cog
reachable through this shared layer now renders its own ACTIVE-game card as
items too, so the accept / lobby-start edit that turns this message into
that card never has to downgrade a Components V2 message back to an embed
(see ``duels/layouts.py``'s module docstring for the full reasoning). The
pure builders that replaced them, ``build_challenge_layout`` and
``build_lobby_layout``, are covered below alongside the other three.

``QuickdrawDuel.render_result_state`` took the layout arm in wave 3b
(2026-09-22, ``tests/duels/test_quickdraw_layouts.py`` is its own contract
file); the two regression tests below for its nickname-flow copy stayed
here rather than moving, since they exist to guard a cross-hook behavioral
fact (the honest "you'll have to do it yourself" sentence for a guild
owner, game night 2026-08-21) rather than the card's shape.

The new tests cover ``duels/layouts.py``'s five functions, the paradigm-7
receipt shape every terminal off-ramp shares, the decision-card and
live-board shapes of the two former embeds, and the dual-arm
``_edit_message_silent`` that lets some out-of-scope mini-game-cog call
sites keep sending embeds while every duels-core one now sends layouts.
"""
from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from bot_modules.duels.quickdraw.cog import QuickdrawDuel
from bot_modules.core.layout import check_budget
from bot_modules.duels.base_game import BaseGame, _fmt_coins
from bot_modules.duels.db import CHALLENGE_RESPONSE_SECONDS
from bot_modules.duels.filters import resolve_stakes_text
from bot_modules.duels.layouts import (
    build_challenge_layout,
    build_duel_notice_layout,
    build_lobby_layout,
    build_nick_reverted_layout,
    card,
)
from bot_modules.economy.service import EconSettings
from bot_modules.shared.name_resolver import resolve_name_from
from tests.paths import REPO_ROOT

_SETTINGS = EconSettings(
    currency_emoji="💎", currency_name="gem", currency_plural="gems"
)


def test_fmt_coins_bolded_with_separator_and_plural():
    assert _fmt_coins(_SETTINGS, 1500) == "💎 **1,500** gems"


def test_fmt_coins_singular_at_one():
    assert _fmt_coins(_SETTINGS, 1) == "💎 **1** gem"


def _guild() -> MagicMock:
    guild = MagicMock()
    guild.get_member = lambda uid: SimpleNamespace(display_name=f"U{uid}")
    return guild


def _name_fn(guild: MagicMock):
    def fn(uid: int) -> str:
        return resolve_name_from(uid, guild=guild)
    return fn


def _quickdraw_self() -> QuickdrawDuel:
    """Same idea as ``_self``, but a real ``QuickdrawDuel`` instance — its
    ``render_result_state`` calls its own ``_display_name`` staticmethod,
    which a bare ``BaseGame`` holder doesn't have."""
    holder = QuickdrawDuel.__new__(QuickdrawDuel)
    holder.GAME_DISPLAY_NAME = "Quickdraw"
    return holder


def _items_text(items) -> str:
    view = discord.ui.LayoutView(timeout=None)
    for item in items:
        view.add_item(item)
    return "\n".join(
        item.content
        for item in view.walk_children()
        if isinstance(item, discord.ui.TextDisplay)
    )


# ── build_lobby_layout: paradigm 8, the live join board ────────────────────


def test_lobby_layout_names_every_roster_member_and_marks_the_host_once():
    """The host is a roster member, so they are named on their roster line
    (marked " — host") and nowhere else — the old embed footer's
    "Host: …" repeated a name the roster already carried (guide § Mentions
    and names: once is per card, per fact)."""
    text = _items_text(build_lobby_layout(
        "Chicken", None, [1, 2], 1, 2, 8, "Last one standing wins.",
        name_fn=_name_fn(_guild()),
    ))
    assert "• U1 — host" in text
    assert "• U2" in text and "U2 — host" not in text
    assert "Host:" not in text
    assert text.count("U1") == 1
    assert "Players (2/8)" in text
    assert "-# Need 2+ players to start." in text


def test_lobby_layout_names_a_host_who_is_not_in_the_roster_in_the_subtext():
    """No mini-game produces this today (``_base_lobby`` seats the host
    first), but the builder must not drop the host's name entirely if one
    ever does — the subtext is where it goes, and only then."""
    text = _items_text(build_lobby_layout(
        "Chicken", None, [2, 3], 1, 2, 8, "Last one standing wins.",
        name_fn=_name_fn(_guild()),
    ))
    assert "— host" not in text
    assert "-# Host: U1 • Need 2+ players to start." in text


def test_lobby_layout_shows_the_pot_only_when_ante_and_pot_text_are_both_given():
    """``pot_text`` and ``stakes_text`` are pre-formatted and separate by the
    time they reach this builder (``base_game.py``'s ``_lobby_view`` is what
    now guards against printing the ante twice), so the builder's own job is
    just: show the pot section when there's a pot to show."""
    no_wager = _items_text(build_lobby_layout(
        "Chicken", None, [1], 1, 2, 8, "Last one standing wins.",
        name_fn=_name_fn(_guild()),
    ))
    assert "💰 Pot" not in no_wager

    wagered = _items_text(build_lobby_layout(
        "Chicken", None, [1, 2], 1, 2, 8,
        "💰 💎 **10** gems to join — winner takes the pot.",
        ante=10, pot_text="💎 **20** gems", name_fn=_name_fn(_guild()),
    ))
    assert "💰 Pot\n💎 **20** gems so far" in wagered
    assert "Leaving the lobby refunds you" in wagered


def test_lobby_layout_a_wagered_lobby_with_no_pot_text_renders_no_pot_section():
    text = _items_text(build_lobby_layout(
        "Chicken", None, [1], 1, 2, 8, "Last one standing wins.",
        ante=10, pot_text=None, name_fn=_name_fn(_guild()),
    ))
    assert "💰 Pot" not in text


def test_lobby_layout_includes_how_to_play_and_closes_only_when_given():
    with_both = _items_text(build_lobby_layout(
        "Chicken", "Don't blink first.", [1], 1, 2, 8, "Last one standing wins.",
        closes_at=1_700_000_000, name_fn=_name_fn(_guild()),
    ))
    assert "📖 How to Play\nDon't blink first." in with_both
    assert "⏱️ Closes\n<t:1700000000:R>" in with_both

    without_either = _items_text(build_lobby_layout(
        "Chicken", None, [1], 1, 2, 8, "Last one standing wins.",
        name_fn=_name_fn(_guild()),
    ))
    assert "How to Play" not in without_either
    assert "Closes" not in without_either


def test_lobby_layout_fits_the_budget_at_its_fullest():
    """The widest lobby a mini-game can produce: a full ten-player roster,
    a how-to-play blurb, a wagered pot and a closing clock — every optional
    section present at once, plus the four-button control row the real
    ``LobbyView`` appends on top of these items."""
    view = card(build_lobby_layout(
        "Hot Potato Group", "Pass it along before the timer runs out.",
        list(range(1, 11)), 1, 2, 10,
        "💰 💎 **10** gems to join — winner takes the pot.",
        ante=10, pot_text="💎 **100** gems", closes_at=1_700_000_000,
        name_fn=_name_fn(_guild()),
    ))
    view.add_item(discord.ui.ActionRow(*(
        discord.ui.Button(label=label) for label in ("Join", "Leave", "Start", "Cancel")
    )))
    check_budget(view)


# ── build_challenge_layout: paradigm 5, the decision card ──────────────────


def test_challenge_layout_lists_every_stake_in_one_place():
    """The wager used to sit in its own field while the in-game and result
    cards showed only the custom stakes, so a two-stake game read as
    one-stake until settlement. The persisted text now carries all of them,
    already in the guild's currency vocabulary, and the card renders it."""
    stakes = resolve_stakes_text(
        "loser sings", 50, nick_stake=True,
        wager_line="💰 💎 **50** gems each — winner takes 💎 **100** gems.",
    )
    text = _items_text(build_challenge_layout(
        "Quickdraw", 1, 2, stakes, wager=50,
        expires_at=int(time.time()) + CHALLENGE_RESPONSE_SECONDS,
        name_fn=_name_fn(_guild()),
    ))
    assert "loser sings" in text
    assert "💎 **50** gems each" in text
    assert "💎 **100** gems" in text  # winner takes 2×
    assert "nickname" in text.lower()
    assert "Nothing is charged" in text


def test_challenge_layout_counts_down_instead_of_stating_a_number():
    """A static "60 seconds to respond" footer never moved, and a footer
    can't carry a Discord timestamp anyway — so the deadline is its own
    section, and the client ticks it down."""
    expires_at = int(time.time()) + CHALLENGE_RESPONSE_SECONDS
    items = build_challenge_layout(
        "Quickdraw", 1, 2, "Last one standing wins.",
        expires_at=expires_at, name_fn=_name_fn(_guild()),
    )
    assert items[-1].content == f"### ⏱️ Expires\n<t:{expires_at}:R>"
    deadline = expires_at - int(time.time())
    assert deadline == pytest.approx(CHALLENGE_RESPONSE_SECONDS, abs=2)


def test_challenge_layout_escrow_caveat_only_appears_when_wagered():
    unwagered = build_challenge_layout(
        "Quickdraw", 1, 2, "Last one standing wins.",
        expires_at=1_700_000_060, name_fn=_name_fn(_guild()),
    )
    assert "Nothing is charged" not in unwagered[2].content

    wagered = build_challenge_layout(
        "Quickdraw", 1, 2, "💰 💎 **50** gems each.", wager=50,
        expires_at=1_700_000_060, name_fn=_name_fn(_guild()),
    )
    assert "Nothing is charged unless the challenge is accepted" in wagered[2].content


def test_challenge_layout_plain_duel_keeps_the_nickname_fallback():
    stakes = (
        "Last one standing wins; the final loser surrenders their nickname "
        "for 24 hours."
    )
    items = build_challenge_layout(
        "Quickdraw", 1, 2, stakes, expires_at=1_700_000_060,
        name_fn=_name_fn(_guild()),
    )
    assert "nickname" in items[2].content.lower()
    assert "Nothing is charged" not in items[2].content


def test_challenge_layout_pings_the_target_first_then_names_both_parties():
    """The target's ping is the **first** item — a bare ``<@{target_id}>``
    Text Display, since a Components V2 message has no ``content=`` to carry
    one — and the challenger/target are named through ``name_fn`` in the
    body itself, never a second raw mention (an embed-era ``<@id>`` in body
    text only shows digits to a reader whose client hasn't cached that
    member)."""
    guild = MagicMock()
    guild.get_member = lambda uid: SimpleNamespace(
        display_name={1: "Chal_lenger", 2: "Tar*get"}[uid]
    )
    items = build_challenge_layout(
        "Quickdraw", 1, 2, "Last one standing wins.",
        expires_at=1_700_000_060, name_fn=_name_fn(guild),
    )
    assert items[0].content == "<@2>"
    assert "Chal\\_lenger has challenged Tar\\*get!" in items[1].content
    assert "<@" not in items[1].content


def test_challenge_layout_fits_the_budget():
    stakes = resolve_stakes_text(
        "loser sings", 50, nick_stake=True,
        wager_line="💰 💎 **50** gems each — winner takes 💎 **100** gems.",
    )
    view = card(build_challenge_layout(
        "Quickdraw", 1, 2, stakes, wager=50,
        expires_at=1_700_000_060, name_fn=_name_fn(_guild()),
    ))
    check_budget(view)


# ── the owner's sentence is honest about not having been applied ─────────────


def test_result_items_self_apply_does_not_claim_the_rename_happened():
    """Discord blocks renaming the guild owner, so the sentence stands but the
    bot never applied it. The card used to say "is now known as", which is
    simply untrue — the owner had to do it by hand (game night 2026-08-21)."""
    game = SimpleNamespace(
        winner_id=1, loser_id=2, stakes_text=None, nick_stake=True, roster=[1, 2],
        fired_at=None, resolved_at=None, loser_fired_at=None,
    )
    items = QuickdrawDuel.render_result_state(
        _quickdraw_self(), game, _guild(), self_apply_nick="Wet Willy",
        original_name="Billy",
    )
    text = _items_text(items)
    assert "Wet Willy" in text
    assert "themselves" in text
    assert "is now known as" not in text


def test_result_items_normal_rename_still_says_it_was_applied():
    game = SimpleNamespace(
        winner_id=1, loser_id=2, stakes_text=None, nick_stake=True, roster=[1, 2],
        fired_at=None, resolved_at=None, loser_fired_at=None,
    )
    items = QuickdrawDuel.render_result_state(
        _quickdraw_self(), game, _guild(), imposed_nick="Wet Willy",
        original_name="Billy",
    )
    assert "is now known as" in _items_text(items)


# ── duels/layouts.py: the thirteen terminal off-ramps' shared shape ────────


def _text(view: discord.ui.LayoutView) -> str:
    return "\n".join(
        item.content
        for item in view.walk_children()
        if isinstance(item, discord.ui.TextDisplay)
    )


def test_card_wraps_items_in_a_control_free_layoutview():
    items = build_duel_notice_layout("⏱️ Challenge Expired", "No response in time.")
    view = card(items)
    assert isinstance(view, discord.ui.LayoutView)
    assert view.timeout is None
    assert list(view.walk_children()) == items
    # A receipt has no buttons — nothing for a component-interaction sweep
    # to ever find live on one of these.
    assert not any(isinstance(i, discord.ui.Button) for i in view.walk_children())


def test_notice_layout_is_one_text_display_heading_then_body():
    items = build_duel_notice_layout("❌ Challenge Declined", "Tar\\*get declined the challenge.")
    assert len(items) == 1
    assert isinstance(items[0], discord.ui.TextDisplay)
    assert items[0].content == "## ❌ Challenge Declined\nTar\\*get declined the challenge."


@pytest.mark.parametrize(
    ("heading", "body"),
    [
        pytest.param("⏱️ Challenge Expired", "No response in time.", id="challenge-expired"),
        pytest.param("⏱️ Lobby Expired", "Not enough players joined in time.", id="lobby-expired"),
        pytest.param("🏳️ Game Abandoned", "No activity in 5m. Game over.", id="abandoned"),
        pytest.param("⏰ Nickname Not Set", "The winner didn't name the loser in time.", id="naming-expired"),
        pytest.param("🚪 Nickname Not Set", "The winner left the server. No rename applied.", id="winner-left"),
        pytest.param("🚫 Lobby Closed", "The host left the server, so the lobby is closed.", id="host-left"),
        pytest.param("🏳️ Game Called Off", "N1 left the server mid-game. Refunded.", id="void-leaver"),
        pytest.param("🚫 Lobby Cancelled", "N1 closed the lobby.", id="lobby-cancelled"),
        pytest.param("❌ Challenge Called Off", "The challenger can no longer cover the wager.", id="ante-failure"),
        pytest.param("❌ Challenge Declined", "N1 declined the challenge.", id="declined"),
        pytest.param("🔁 Nickname Not Set", "The winner started a new game first.", id="superseded"),
    ],
)
def test_every_terminal_notice_fits_the_budget_and_names_what_happened(heading, body):
    view = card(build_duel_notice_layout(heading, body))
    check_budget(view)  # no ValueError — these are two short lines
    text = _text(view)
    assert text.startswith(f"## {heading}\n")
    assert body in text


def test_nick_reverted_layout_names_the_game_and_escapes_the_restored_nickname():
    """The restored nickname is member-set text (their own prior nickname, or
    an admin's), so it goes through escape_markdown like any other free text
    landing in a Text Display — the embed this replaced didn't, which was a
    latent bug, not a behaviour worth keeping."""
    items = build_nick_reverted_layout("Chicken", "*Big_Cheese*")
    text = _text(card(items))
    assert "Chicken nickname sentence has expired" in text
    assert "**\\*Big\\_Cheese\\***" in text
    assert "*Big_Cheese*" not in text  # the unescaped form never appears raw


def test_nick_reverted_layout_fits_the_budget():
    view = card(build_nick_reverted_layout("Musical Chairs", "A Very Long Previous Nickname"))
    check_budget(view)


# ── _edit_message_silent: one embed-shaped arm, one layout-shaped arm ──────


class _Msg:
    def __init__(self) -> None:
        self.edit = AsyncMock()


async def _edit_target(monkeypatch, msg: _Msg) -> BaseGame:
    holder = BaseGame.__new__(BaseGame)
    channel = MagicMock()
    channel.fetch_message = AsyncMock(return_value=msg)
    bot = MagicMock()
    bot.get_channel = MagicMock(return_value=channel)
    holder.bot = bot
    return holder


async def test_edit_message_silent_embed_arm_is_unchanged_for_out_of_scope_callers(monkeypatch):
    """No mini-game cog calls this positionally with an embed and a view
    any more: Quickdraw's three, Chicken's one, Musical Chairs' four, Hot
    Potato's one and Hot Potato Group's two all moved to the layout arm in
    Components V2 wave 3b (Pressure Cooker never called it), and the
    lobby/challenge cards converted in the same wave. The arm itself is
    unchanged — exactly as before this module existed — because
    ``_post_group_result``'s own ``isinstance(rendered, list)`` seam (and
    its siblings, ``_active_game_edit_kwargs`` / ``_edit_continue_state`` /
    ``_result_send_kwargs``) still reach it for a hypothetical mini-game
    cog whose ``render_game_state`` returns an embed instead of items; that
    dual-armed plumbing is a later cleanup, not this commit's job."""
    msg = _Msg()
    holder = await _edit_target(monkeypatch, msg)
    embed = discord.Embed(title="Still an embed")
    view = discord.ui.View()

    await BaseGame._edit_message_silent(holder, 100, 200, embed, view)

    msg.edit.assert_awaited_once_with(embed=embed, view=view)


async def test_edit_message_silent_layout_arm_nulls_embed_and_content(monkeypatch):
    """A layout edit must explicitly null ``content``/``embed``/``attachments``
    alongside the new view — the upgrade is one-way per message, so a stray
    leftover embed field on the edit call would be a silent no-op at best."""
    msg = _Msg()
    holder = await _edit_target(monkeypatch, msg)
    layout = card(build_duel_notice_layout("⏱️ Challenge Expired", "No response in time."))

    await BaseGame._edit_message_silent(holder, 100, 200, layout=layout)

    msg.edit.assert_awaited_once_with(
        view=layout, content=None, embed=None, attachments=[]
    )


async def test_edit_message_silent_still_swallows_not_found(monkeypatch):
    holder = BaseGame.__new__(BaseGame)
    channel = MagicMock()
    channel.fetch_message = AsyncMock(side_effect=discord.NotFound(MagicMock(status=404), "gone"))
    bot = MagicMock()
    bot.get_channel = MagicMock(return_value=channel)
    holder.bot = bot

    await BaseGame._edit_message_silent(
        holder, 100, 200,
        layout=card(build_duel_notice_layout("⏱️ Challenge Expired", "x")),
    )  # no raise


def test_lobby_layout_budgets_a_markdown_heavy_roster_and_counts_the_rest():
    """``escape_markdown`` doubles a 32-char all-``*`` name to 64 and
    ``max_players`` has no backend ceiling (the dashboard's 50 is a UI hint),
    so a big lobby of such names ran the roster past the card's 4,000
    characters and Discord refused the redraw. Whole lines are budgeted —
    never a slice through an escape pair — and the overflow is counted."""
    guild = MagicMock()
    guild.get_member = lambda uid: SimpleNamespace(display_name="*" * 32)
    items = build_lobby_layout(
        "Chicken", None, list(range(1, 80)), 1, 2, 100, "Last one standing wins.",
        name_fn=_name_fn(guild),
    )
    view = discord.ui.LayoutView(timeout=None)
    for item in items:
        view.add_item(item)
    check_budget(view)
    text = _items_text(items)
    assert "\\*\\*\\*\\*" in text and "more_" in text
    assert "*" * 10 not in text.replace("\\*", "")


@pytest.mark.parametrize("copy", ["nick_applied_copy", "nick_self_apply_copy"])
def test_nick_copy_escapes_the_typed_nick_exactly_once(copy):
    """The helper escapes the nick at render (the raw value is also the
    loser's real Discord nickname), so a caller that escapes it too shows
    members a backslash — the V2 merge of 09-27 briefly did exactly that."""
    text = getattr(_quickdraw_self(), copy)("Lo\\_ser", "b_a*d", 2)
    assert "b\\_a\\*d" in text
    assert "\\\\" not in text  # neither the nick nor `who` escaped twice


def test_no_nick_sentence_caller_pre_escapes_what_the_helper_escapes():

    root = REPO_ROOT / "src" / "bot_modules"
    offenders = []
    for path in root.rglob("*.py"):
        src = path.read_text(encoding="utf-8")
        # A layout that renders the nick itself (musical chairs) escapes it
        # once, correctly; only a file that also hands it to the helper errs.
        if "nick_applied_copy(" not in src and "nick_self_apply_copy(" not in src:
            continue
        if "escape_markdown(self_apply_nick" in src or "escape_markdown(imposed_nick" in src:
            offenders.append(str(path.relative_to(root)))
    assert offenders == []
