"""Post one sample message per card state of a converted feature.

The Components V2 migration's verification step (``docs/plans/
components_v2_migration.md`` § 5.5 and § 6): a card's budget, its mention
array and the way it actually looks on a phone can only be checked on a real
Discord message, and the running bot still holds the *old* code while a
conversion sits on a branch. So this logs in REST-only with the prod token,
renders every state of the feature's cards from placeholder data, and posts
them to the staff scratch channel — no gateway, no cog, no database writes.

Generalises the 2026-09-20 Components V2 pilot script (since deleted), which
did the same thing for one hand-built card. A feature registers a name and a list of
``(state, items)`` pairs in ``SAMPLES`` below; everything after that — the
login, the ``sample:`` id rewrite, the budget print and the read-back — is
shared::

    .venv/bin/python scripts/layout_samples.py dm_perms           # dry run
    .venv/bin/python scripts/layout_samples.py dm_perms --post    # posts
    .venv/bin/python scripts/layout_samples.py dm_perms --only hub  # one card

Dry by default: it renders, prints each card's component tree and budget, and
posts nothing.

``--only`` matches the state name case-insensitively on a substring and
narrows everything after it — the budget table, the printed tree and, with
``--post``, what is sent. Review of a gallery arrives one card at a time
("can you put some sort of separation between requesting and setting your
preference"), and re-posting nineteen cards to show one fix buries the fix
in eighteen messages Billy has already looked at.

Two safety rules are deliberately not flags:

* **The destination is hard-coded** to the channel whose name contains
  ``bot-command-spam``. A sample card is a real message in a real guild; a
  typo in a ``--channel`` value is how a half-built panel ends up in a
  member-facing channel.
* **Every custom_id is prefixed** ``sample:`` (``core.layout.
  sample_prefix_ids``). Unprefixed, the persistent ids on these cards are the
  *live* ones: a press on a sample would reach the running bot's real handler
  and act on real data. Prefixed, nothing matches and the press fails
  harmlessly — which the lead line says.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

import discord
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

#: The prod checkout owns the token; a worktree has no ``.env`` of its own.
PROD_ENV = PROJECT_ROOT / ".env"


def dashboard_base() -> str:
    """The dashboard's public origin for sample art URLs — ``DASHBOARD_BASE_URL``
    once the env file is loaded, else a placeholder that renders a broken image
    rather than someone else's host."""
    import os

    return os.environ.get("DASHBOARD_BASE_URL", "https://dk.example.com").rstrip("/")

#: Hard-coded on purpose — see the module docstring.
SCRATCH_CHANNEL_FRAGMENT = "bot-command-spam"

INERT = (
    "-# The controls are inert (every id is prefixed `sample:`) and none of "
    "the members or dates below are real."
)

#: Placeholder ids. Real-looking snowflakes so a card that forgot ``name_fn``
#: renders a visible ``<@…>`` rather than a suspiciously short number.
REQUESTER_ID = 111111111111111111
TARGET_ID = 222222222222222222
OTHER_IDS = (333333333333333333, 444444444444444444, 555555555555555555)


async def _sample_noop_rematch_hook(interaction: discord.Interaction, game_id: int) -> None:
    """The ``on_set_nick``/``on_rematch`` hook a sample result card passes to
    ``ResultLayoutView`` — never actually called, since ``sample_prefix_ids``
    has already rewritten the buttons' ``custom_id``s to the inert
    ``sample:`` form and this script never registers the view with a bot, so
    no interaction can ever reach it."""
    del interaction, game_id


def _players_line(winner_id: int, loser_id: int) -> discord.ui.TextDisplay:
    """The names line ``BaseGame._result_layout`` opens a result card with,
    for the placeholder members."""
    from bot_modules.duels.layouts import build_players_line

    name_fn = _placeholder_names()
    return build_players_line(name_fn(winner_id), name_fn(loser_id))


def _placeholder_names() -> Callable[[int], str]:
    """A ``name_fn`` for members who do not exist.

    The real one resolves through ``services.name_resolver``; these ids have
    no member behind them, and the point of the sample is to see the *shape*
    a resolved name makes on the card.
    """
    names = {
        REQUESTER_ID: "Ada Sample",
        TARGET_ID: "Bo Placeholder",
        OTHER_IDS[0]: "Cy Example",
        OTHER_IDS[1]: "Dee Stand-in",
        OTHER_IDS[2]: "Eli Mock",
    }
    return lambda uid: names.get(uid, f"Member {uid}")


# ── features ──────────────────────────────────────────────────────────────


def dm_perms_samples(guild: discord.Guild, avatar_url: str):
    """Every card ``bot_modules/dm/perms/layouts.py`` can produce."""
    from bot_modules.dm.perms import layouts as dm
    from bot_modules.dm.perms.logic import audit_line_asked

    name_fn = _placeholder_names()
    now = int(time.time())
    connections = [
        {"user_id": TARGET_ID, "type": "dm", "reason": "chess", "created_at": now},
        {
            "user_id": OTHER_IDS[0],
            "type": "friend",
            "reason": "",
            "created_at": now - 86400 * 12,
        },
        {
            "user_id": OTHER_IDS[1],
            "type": "dm",
            "reason": "",
            "created_at": now - 86400 * 90,
        },
    ]
    many = [
        {
            "user_id": OTHER_IDS[i % len(OTHER_IDS)],
            "type": "dm" if i % 2 else "friend",
            "reason": "",
            "created_at": now - 86400 * i,
        }
        for i in range(12)
    ]
    expires_at = now + 86400
    return [
        # Named for its paradigm as well as its surface, so ``--only hub``
        # reaches it: a reviewer naming the paradigm is what guide
        # § Verifying a conversion asks of the gallery.
        ("Request panel (hub)", dm.build_panel_layout()),
        (
            "DM Settings — no connections",
            dm.build_settings_layout(mode="ask", connections=[], name_fn=name_fn),
        ),
        (
            "DM Settings — three connections, just changed",
            dm.build_settings_layout(
                mode="open", connections=connections, name_fn=name_fn, changed=True
            ),
        ),
        (
            "DM Settings — after a removal",
            dm.build_settings_layout(
                mode="open",
                connections=connections[1:],
                name_fn=name_fn,
                note=f"Removed your connection with {name_fn(TARGET_ID)}.",
            ),
        ),
        (
            "DM Settings — page 1 of 2 (twelve connections)",
            dm.build_settings_layout(mode="closed", connections=many, name_fn=name_fn),
        ),
        (
            "DM Settings — page 2 of 2",
            dm.build_settings_layout(
                mode="closed", connections=many, name_fn=name_fn, page=1
            ),
        ),
        (
            "Remove connection — confirm",
            dm.build_remove_confirm_layout(
                user_id=TARGET_ID, request_type="dm", name_fn=name_fn
            ),
        ),
        (
            "Remove connection — confirmed",
            dm.build_remove_done_layout(user_id=TARGET_ID, name_fn=name_fn),
        ),
        (
            "Remove connection — already gone",
            dm.build_remove_missing_layout(user_id=TARGET_ID, name_fn=name_fn),
        ),
        (
            "Consent DM — the decision card",
            dm.build_request_dm_layout(
                requester_id=REQUESTER_ID,
                requester_avatar_url=avatar_url,
                guild_name=guild.name,
                request_type="dm",
                reason="We're both in the chess thread and I'd rather not clog it up.",
                expires_at=expires_at,
                name_fn=name_fn,
            ),
        ),
        (
            "Consent DM — friend request, no reason given",
            dm.build_request_dm_layout(
                requester_id=REQUESTER_ID,
                requester_avatar_url=avatar_url,
                guild_name=guild.name,
                request_type="friend",
                reason="",
                expires_at=now + 6 * 3600,
                name_fn=name_fn,
            ),
        ),
        (
            "Connected",
            dm.build_acceptance_layout(
                viewer_id=TARGET_ID,
                requester_id=REQUESTER_ID,
                target_id=TARGET_ID,
                request_type="dm",
                name_fn=name_fn,
            ),
        ),
        (
            "Declined — the decliner's own card",
            dm.build_denial_layout_for_view(
                requester_id=REQUESTER_ID,
                request_type="dm",
                reply="Not right now, sorry!",
                name_fn=name_fn,
            ),
        ),
        (
            "Declined — as the requester hears it",
            dm.build_denial_layout_for_requester(
                target_id=TARGET_ID,
                guild_name=guild.name,
                request_type="dm",
                reply="Not right now, sorry!",
                name_fn=name_fn,
            ),
        ),
        (
            "Request sent — receipt",
            dm.build_request_sent_layout(
                target_id=TARGET_ID,
                guild_name=guild.name,
                request_type="dm",
                reason="We're both in the chess thread and I'd rather not clog it up.",
                expires_at=expires_at,
                name_fn=name_fn,
            ),
        ),
        (
            "Request expired",
            dm.build_expired_layout(
                target_id=TARGET_ID,
                guild_name=guild.name,
                request_type="dm",
                expiry_label="24 hours",
                name_fn=name_fn,
            ),
        ),
        (
            "Connection removed — as the other party hears it",
            dm.build_revoked_layout(
                viewer_id=TARGET_ID,
                actor_id=REQUESTER_ID,
                other_id=TARGET_ID,
                name_fn=name_fn,
            ),
        ),
        (
            "Connection removed — the channel card both can read",
            dm.build_revoked_layout(
                viewer_id=None,
                actor_id=REQUESTER_ID,
                other_id=TARGET_ID,
                name_fn=name_fn,
            ),
        ),
        ("Request already resolved", dm.build_stale_request_layout()),
        ("Server unavailable", dm.build_guild_unavailable_layout()),
        (
            "Audit feed notice",
            dm.build_audit_notice_layout(
                message=audit_line_asked(
                    name_fn(REQUESTER_ID), name_fn(TARGET_ID), "Direct Message"
                ),
                timestamp=now,
            ),
        ),
    ]


def shop_samples(guild: discord.Guild, avatar_url: str):
    """Every card ``bot_modules/economy/shop_layouts.py`` can produce: the
    channel hub panel, a Server Store page (a blurbed weekly-role item, a
    blurb-less one that falls back to a kind-derived line, a sold-out one
    and an "N left" one, on a middle page of a multi-page nav so the chip
    and both arrows render enabled), one perk-section page (cosmetics, with
    an owned, a gated and a rentable perk on it, plus the always-visible
    palette row), a two-row refund picker (a rental and the shield) +
    confirm gate, and a post-rent receipt both with and without a Customise
    accessory."""
    from bot_modules.economy.shop import (
        SECTION_COSMETICS,
        SECTION_GAMES,
        SECTION_SERVER,
        SECTION_SPECIALS,
    )
    from bot_modules.economy.shop_items import BILLING_WEEKLY, KIND_ROLE, ItemView
    from bot_modules.economy.shop_layouts import (
        build_post_rent_layout,
        build_refund_confirm_layout,
        build_refund_picker_layout,
        build_shop_panel_layout,
        build_shop_section_layout,
        build_store_layout,
        shop_section_rows,
    )
    from bot_modules.economy.service import EconSettings

    settings = EconSettings(currency_emoji="🪙")
    cosmetics_nav = ["🎁 Specials", "🎨 Role cosmetics", "🏠 Server features", "🎲 Game features"]
    #: The store's own book runs two Specials pages deep here on purpose — a
    #: middle page of a bigger nav, so this sample's ◀ and ▶ both render
    #: enabled and the chip carries the spread caption (``page_captions``'
    #: own shape for a Specials section with more than one page).
    store_nav = [
        "🎁 Specials · 1–14", "🎁 Specials · 15–24",
        "🎨 Role cosmetics", "🏠 Server features", "🎲 Game features",
    ]

    store_items = [
        ItemView(
            item_id=1, name="Founder role", price=500,
            blurb="A one-of-a-kind holo role",
            billing=BILLING_WEEKLY, kind=KIND_ROLE,
        ),
        ItemView(item_id=2, name="Shoutout", price=75),
        ItemView(
            item_id=3, name="Server Sticker Pack", price=150,
            blurb="Custom stickers, mod-delivered", stock=3, sold=3,
        ),
        ItemView(item_id=4, name="Custom Emoji Slot", price=200, stock=5, sold=2),
    ]

    cosmetics_rows = shop_section_rows(
        settings, SECTION_COSMETICS,
        gated={"role_gradient"}, owned={"role_color"},
        icon_catalog=None, color_catalog=None, has_palette=True,
        shields_held=0, refundable=True,
    )

    rentals = [
        {
            "perk": "role_color", "state": "active",
            "price": 40, "next_bill_at": time.time() + 3 * 86400,
        },
    ]

    return [
        ("Channel hub panel", build_shop_panel_layout(
            [SECTION_SPECIALS, SECTION_COSMETICS, SECTION_SERVER, SECTION_GAMES]
        )),
        ("Server Store — a middle page of items", build_store_layout(
            settings, store_items, owned_item_ids=frozenset(), balance=1_250,
            page=1, nav=store_nav,
        )),
        ("Section page — cosmetics, one owned, one gated, one rentable",
         build_shop_section_layout(
            settings, SECTION_COSMETICS, cosmetics_rows,
            balance=1_250, page=1, nav=cosmetics_nav,
        )),
        ("Refund picker — two rows", build_refund_picker_layout(
            settings, rentals, shield_price=30
        )),
        ("Refund confirm gate", build_refund_confirm_layout(
            settings, "Custom Role Color", "🪙 28 back — ends now."
        )),
        ("Post-rent receipt — role perk, with Customise", build_post_rent_layout(
            "role_color", comped=False, customise=True
        )),
        ("Post-rent receipt — comped, no customisation needed", build_post_rent_layout(
            "role_holographic", comped=True, customise=False
        )),
    ]


def server_features_samples(guild: discord.Guild, avatar_url: str):
    """Every card ``bot_modules/server_features/layouts.py`` can produce."""
    from bot_modules.server_features import layouts as sf
    from bot_modules.server_features.logic import RoleCard, build_card
    from bot_modules.shared import feature_roles as fr
    from bot_modules.no_contact.logic import (
        ADDED_TEXT,
        REMOVAL_DENIED_MUTUAL,
        OwnEntry,
    )
    from bot_modules.onboarding.welcome.onboarding_service import OptionView, PromptView

    name_fn = _placeholder_names()
    pings = [(entry, 900 + i) for i, entry in enumerate(
        (fr.WELCOME_PING, fr.QOTD_PING, fr.RISKY_PING, fr.GAME_LOBBY_PING, fr.ECONOMY_NOTIFY)
    )]
    prompts = [
        PromptView(id=1, title="Pronouns", single_select=True, options=(
            OptionView(title="she/her", role_ids=(11,), emoji="🌸"),
            OptionView(title="he/him", role_ids=(12,), emoji="🌊"),
            OptionView(title="they/them", role_ids=(13,), emoji="🌈"),
        )),
        PromptView(id=2, title="What are you into?", options=tuple(
            OptionView(title=t, role_ids=(20 + i,)) for i, t in enumerate(
                ("Gaming", "Art", "Music", "Movies", "Books")
            )
        )),
    ]
    grantable = {900, 901, 902, 903, 904, 11, 12, 13, 20, 21, 22, 23, 24}
    full = build_card(pings, prompts, grantable=grantable, member_role_ids={901, 13, 21})
    entries = [
        OwnEntry(other_id=TARGET_ID, removable=True, set_by_moderator=False),
        OwnEntry(other_id=OTHER_IDS[0], removable=False, set_by_moderator=True),
    ]
    many = [
        OwnEntry(other_id=OTHER_IDS[i % 3], removable=True, set_by_moderator=False)
        for i in range(12)
    ]
    return [
        ("Server Features (hub)", sf.build_hub_layout()),
        ("Roles & Pings — five pings, two Customize questions",
         sf.build_roles_layout(full, note="Saved.")),
        ("Roles & Pings — nothing set up", sf.build_roles_layout(RoleCard((), ()))),
        ("Roles & Pings — on a break", sf.build_roles_layout(RoleCard((), ()), held=True)),
        ("No Contact — empty", sf.build_no_contact_layout([], name_fn=name_fn)),
        ("No Contact — one own, one moderator's",
         sf.build_no_contact_layout(entries, name_fn=name_fn)),
        ("No Contact — twelve entries, page 1",
         sf.build_no_contact_layout(many, name_fn=name_fn)),
        ("No Contact — remove confirm",
         sf.build_remove_confirm_layout(user_id=TARGET_ID, name_fn=name_fn)),
        ("No Contact — added receipt", sf.build_no_contact_answer_layout(
            ADDED_TEXT.format(name=name_fn(TARGET_ID))
        )),
        ("No Contact — refused remove",
         sf.build_no_contact_answer_layout(REMOVAL_DENIED_MUTUAL)),
        ("Take a Break (from the hub)", sf.build_break_card_layout()),
    ]


def games_core_samples(guild: discord.Guild, avatar_url: str):
    """Every card the "Games core" group (wave 3b) can produce: the
    ``/games config game-status`` inspection card (active and idle), the
    ``/games end force:true`` notice, the ``/games help`` overview and one
    game's detail card, the ``/support`` card, ``/recap``'s session recap,
    and the audit-channel mirror an anonymous submission posts."""
    from bot_modules.games.utils.audit import build_audit_notice_layout
    from bot_modules.games.config.layouts import (
        build_force_end_layout,
        build_game_status_layout,
    )
    from bot_modules.games.help.layouts import (
        build_game_detail_layout,
        build_help_layout,
        build_support_layout,
    )
    from bot_modules.games.session.layouts import build_session_recap_layout

    name_fn = _placeholder_names()
    active_row = {
        "game_type": "wyr", "state": "playing",
        "host_id": REQUESTER_ID, "game_id": "sample-game-1",
    }
    audit_user = SimpleNamespace(id=TARGET_ID, display_name="Bo Placeholder")

    return [
        ("Game status — active", build_game_status_layout(active_row, name_fn=name_fn)),
        ("Game status — idle", build_game_status_layout(None, name_fn=name_fn)),
        ("Force-end notice", build_force_end_layout("wyr")),
        ("Games help — overview", build_help_layout(
            extra_lines=["🏈 **Survivor** — open in <#123456789012345678>"]
        )),
        ("Games help — one game's detail", build_game_detail_layout("wyr")),
        ("/support card", build_support_layout()),
        ("Session recap", build_session_recap_layout(
            game_count=4,
            player_ids=[REQUESTER_ID, TARGET_ID, *OTHER_IDS],
            duration_str="28m",
            highlights=[
                "**🤔 Would You Rather**: 'fly | be invisible' split the room 3-2",
                "**🧊 Never Have I Ever**: Bo Placeholder survived with 1 life left",
            ],
            name_fn=name_fn,
        )),
        ("Audit-channel mirror", build_audit_notice_layout(
            game_type="ffa", user=audit_user,
            content="This is a sample anonymous submission.",
        )),
    ]


def clapback_samples(guild: discord.Guild, avatar_url: str):
    """Every card a Clapback game posts: the lobby (empty, three seated with a
    countdown, and retired by a timeout), the round's prompt card (with a bye,
    and closed), a head-to-head matchup, the three reveals, a scoreboard with a
    bye and the host's Next Round row, and the recap with its payout line."""
    from bot_modules.games.clapback.layouts import (
        build_lobby_layout,
        build_recap_layout,
        build_reveal_layout,
        build_scoreboard_layout,
        build_submit_layout,
        build_vote_layout,
    )

    name_fn = _placeholder_names()
    a, b, c, d = REQUESTER_ID, TARGET_ID, *OTHER_IDS[:2]
    cfg = {"rounds": 3, "anonymous": False}
    start_at = int(time.time()) + 600
    answers = {str(a): "A GPS that only says 'you know where you went wrong'",
               str(b): "Mild peril"}
    prompt = "The worst thing to hear from a tour guide"

    def _reveal(result):
        return build_reveal_layout(
            result=result, answers=answers, player_a=a, player_b=b,
            anonymous=False, name_fn=name_fn, prompt=prompt,
        )

    history = [{"round": 1, "matchups": [
        {"player_a": a, "answer_a": answers[str(a)], "votes_a": 3,
         "player_b": b, "answer_b": answers[str(b)], "votes_b": 0, "clapback": True},
        {"player_a": c, "answer_a": "We're walking. Forever.", "votes_a": 2,
         "player_b": d, "answer_b": "Please don't touch the lava", "votes_b": 2,
         "clapback": False},
    ]}] * 3
    final = {
        "scores": {str(a): 325, str(b): 140, str(c): 210, str(d): 90},
        "clapbacks": {str(a): 2}, "round_history": history,
        "players": [a, b, c, d],
    }
    return [
        ("Lobby — empty", build_lobby_layout("Ada Placeholder", cfg, [], name_fn)),
        ("Lobby — three seated, countdown", build_lobby_layout(
            "Ada Placeholder", cfg, [a, b, c], name_fn, start_at=start_at,
        )),
        ("Lobby — timed out", build_lobby_layout(
            "Ada Placeholder", cfg, [a], name_fn,
            notice="⌛ **Lobby timed out** — the game wasn't started in time. "
                   "Run `/games play clapback` to open a new one.",
        )),
        ("Prompt card — with a bye", build_submit_layout(
            prompt, 1, 3, f"<t:{start_at}:R>", 2, 4, bye_player=d, name_fn=name_fn,
        )),
        ("Prompt card — closed", build_submit_layout(
            prompt, 1, 3, "⏱️ Closed", 4, 4, name_fn=name_fn,
        )),
        ("Matchup — voting", build_vote_layout(
            answers[str(a)], answers[str(b)], 1, 0, 2, f"<t:{start_at}:R>",
            vote_count=2, prompt=prompt,
        )),
        ("Reveal — clapback", _reveal({"winner": a, "scores": {a: 125, b: 0},
                                       "clapback": True, "vote_counts": {a: 3, b: 0}})),
        ("Reveal — regular win", _reveal({"winner": a, "scores": {a: 67, b: 33},
                                          "clapback": False, "vote_counts": {a: 2, b: 1}})),
        ("Reveal — tie", _reveal({"winner": None, "scores": {a: 50, b: 50},
                                  "clapback": False, "vote_counts": {a: 1, b: 1}})),
        ("Scoreboard — bye and Next Round", build_scoreboard_layout(
            {"scores": {str(a): 125, str(b): 0, str(c): 50}}, 1, 3,
            bye_players=[d], bye_award=58, name_fn=name_fn, with_next=True,
        )),
        ("Recap", build_recap_layout(
            final, cfg, name_fn,
            payout_line="🪙 +100 to winners • +40 to everyone who played",
        )),
    ]


def traditional_samples(guild: discord.Guild, avatar_url: str):
    """Every card ``bot_modules/games/traditional/layouts.py`` can produce:
    the join screen (plain and single-choice), the working board (empty,
    populated, single-choice), the host's ephemeral controls, the question card (a cached ping, a resolved
    name, an unknown-category fallback) and the recap (plain, with a note,
    with the question-bank count, with a payout line) — the room board with
    and without the guild's lobby strip."""
    del avatar_url
    from bot_modules.games.traditional.layouts import (
        build_host_panel_layout,
        build_question_layout,
        build_recap_layout,
        build_room_layout,
    )

    name_fn = _placeholder_names()
    a, b, c = REQUESTER_ID, TARGET_ID, OTHER_IDS[0]
    names = {str(a): name_fn(a), str(b): name_fn(b), str(c): name_fn(c)}
    # The live guild's small lobby strip and its footer — the frame every
    # lobby wears (``lobby_art.read_lobby_art``). Untitled: prod draws the
    # "Truth or Dare" strip on the first game after the deploy, so until
    # then the sample keeps the card's own heading.
    banners = f"{dashboard_base()}/static/panel-banners"
    art = {
        "header_url": f"{banners}/fd7f86580127410a95a47ee1f716e29f.webp",
        "footer_url": f"{banners}/0a7c324b50c14c4ea05d982cdcc283e5.webp",
    }
    working_payload = {
        "participants": [a, b, c],
        "prefs": {str(a): ["sfw_truth", "sfw_dare"], str(b): ["nsfw_dare"], str(c): ["sfw_truth"]},
        "asked": {f"{a}:sfw_truth": "q"},
    }
    recap_payload = {
        "participants": [a, b, c],
        "asked": {
            f"{a}:sfw_truth": "q", f"{a}:sfw_dare": "q",
            f"{b}:nsfw_dare": "q", f"{c}:sfw_truth": "q",
        },
        "bank_asked": 2,
    }
    return [
        ("Join screen", build_room_layout("Ada Sample", {}, lobby=True)),
        ("Join screen — one category each", build_room_layout(
            "Ada Sample", {}, lobby=True, single_choice=True,
        )),
        ("Join screen — with the lobby strip", build_room_layout(
            "Ada Sample", {}, lobby=True, **art,
        )),
        ("Working board — nobody in any category yet", build_room_layout(
            "Ada Sample", {"participants": [a]}, lobby=False,
        )),
        ("Working board — populated, one category each", build_room_layout(
            "Ada Sample", working_payload, lobby=False,
            names=names, single_choice=True, **art,
        )),
        ("Host controls — ephemeral, from /games host", build_host_panel_layout()),
        ("Question card — cached member (pings)", build_question_layout(
            "sfw_truth", "What's the pettiest thing you've ever held a grudge over?",
            target_id=b,
        )),
        ("Question card — uncached member (resolved name)", build_question_layout(
            "nsfw_dare", "Send your last text out of context, no explanation.",
            target_name=name_fn(c),
        )),
        ("Question card — unknown category fallback", build_question_layout(
            "legacy_cat", "A stale payload's category key falls back gracefully.",
            target_name=name_fn(a),
        )),
        ("Recap — plain", build_recap_layout(
            {"participants": [a, b], "asked": {f"{a}:sfw_truth": "q"}},
        )),
        ("Recap — idle close, question bank, payout", build_recap_layout(
            recap_payload,
            note="⌛ Closed after 60 quiet minutes.",
            payout_line="🪙 +20 to winners • +5 to everyone who played",
        )),
    ]


#: The live guild's small lobby strip and its footer — the frame every lobby
#: wears (``lobby_art.read_lobby_art``). Untitled: prod draws a game's name
#: into the strip on its first lobby after the deploy, so until then a
#: sample keeps the card's own heading.
_PANEL_BANNERS = f"{dashboard_base()}/static/panel-banners"
LOBBY_ART = {
    "header_url": f"{_PANEL_BANNERS}/fd7f86580127410a95a47ee1f716e29f.webp",
    "footer_url": f"{_PANEL_BANNERS}/0a7c324b50c14c4ea05d982cdcc283e5.webp",
}


def rushmore_samples(guild: discord.Guild, avatar_url: str):
    """Every card ``bot_modules/games/rushmore/layouts.py`` can produce: the
    lobby (empty, populated in both modes, with the lobby strip, closed), the
    host's ephemeral Start Draft panel, the draft board (snake mid-turn,
    blitz mid-round, complete), the final boards, the vote, the winner (one
    and tied), the recap (no winner; full stats with a payout) and the
    restart notice."""
    del guild, avatar_url
    from bot_modules.games.rushmore.layouts import (
        build_draft_layout,
        build_final_boards_pages,
        build_host_panel_layout,
        build_interrupted_layout,
        build_lobby_layout,
        build_recap_layout,
        build_vote_layout,
        build_winner_layout,
    )
    from bot_modules.games.rushmore.logic import SKIPPED_MARKER

    name_fn = _placeholder_names()
    ids = [REQUESTER_ID, TARGET_ID, *OTHER_IDS[:2]]
    players = [(uid, name_fn(uid)) for uid in ids]
    names = [name for _, name in players]
    picks = [
        ["Nachos", "Popcorn", "Pretzels", "Trail Mix"],
        ["Gummy Bears", "Dark Chocolate", SKIPPED_MARKER, "Licorice"],
        ["Hot Cheetos", "Pickles", "Beef Jerky", "Seaweed"],
        ["Apple Slices", "Rice Cakes", "Hummus", "Celery"],
    ]
    done = {str(uid): board for uid, board in zip(ids, picks)}
    mid = {str(uid): [board[0], None, None, None] for uid, board in zip(ids, picks)}
    mid[str(ids[0])][1] = picks[0][1]
    soon = int(time.time()) + 45
    stats = {
        "first_pick": {"pick": "Nachos", "player": names[0]},
        "skipped_count": 1, "skipped_names": [names[1]],
        "fastest": {"pick": "Pickles", "player": names[2], "time": 2.4},
        "slowest": {"pick": "Licorice", "player": names[1], "time": 41.0},
        "vote_split": 2,
    }
    return [
        ("Lobby — empty, blitz", build_lobby_layout("Ada Sample", [], mode="blitz")),
        ("Lobby — populated, snake, topic set, countdown", build_lobby_layout(
            "Ada Sample", names, "Snacks", mode="snake", start_at=soon + 600,
        )),
        ("Lobby — with the lobby strip", build_lobby_layout(
            "Ada Sample", names[:2], "Snacks", mode="blitz", **LOBBY_ART,
        )),
        ("Lobby — closed (the draft has started)", build_lobby_layout(
            "Ada Sample", names, "Snacks", mode="blitz", closed=True,
        )),
        ("Host controls — ephemeral, from /games host", build_host_panel_layout()),
        ("Draft board — snake, mid-turn", build_draft_layout(
            "Snacks", players, mid, ids[1], names[1], 2, soon,
        )),
        ("Draft board — blitz, mid-round", build_draft_layout(
            "Snacks", players, mid, None, None, 2, soon,
        )),
        ("Draft board — complete", build_draft_layout(
            "Snacks", players, done, None, None, 4, soon, closed=True,
        )),
        *[
            (f"Final boards — page {number}", page)
            for number, page in enumerate(
                build_final_boards_pages("Snacks", players, done), start=1,
            )
        ],
        ("Vote", build_vote_layout(
            "Snacks", soon, [(name, str(uid)) for uid, name in players],
        )),
        ("Winner", build_winner_layout(
            "Snacks", [names[0]], 3, [picks[0]],
            [(names[0], 3), (names[2], 1), (names[1], 0), (names[3], 0)],
        )),
        ("Winner — tied", build_winner_layout(
            "Snacks", [names[0], names[2]], 2, [picks[0], picks[2]],
            [(names[0], 2), (names[2], 2), (names[1], 0), (names[3], 0)],
        )),
        ("Recap — nobody won", build_recap_layout("Snacks", 4, 95.0, [], 0, [], {})),
        ("Recap — stats and payout", build_recap_layout(
            "Snacks", 4, 212.0, [names[0]], 3, [picks[0]], stats,
            payout_line="🪙 +20 to winners • +5 to everyone who played",
        )),
        ("Restart notice", build_interrupted_layout()),
    ]


def wyr_samples(guild: discord.Guild, avatar_url: str):
    """Every card ``bot_modules/games/wyr/layouts.py`` can produce: the round
    card (fresh, mid-vote with shown voters and a countdown, scheduled with
    Next on it, waiting for a question, closed), the host's ephemeral panel,
    and the recap (at the round cap with a payout; no votes cast)."""
    del guild, avatar_url
    from bot_modules.games.wyr.layouts import (
        build_host_panel_layout,
        build_recap_layout,
        build_round_layout,
    )

    name_fn = _placeholder_names()
    a, b, c = REQUESTER_ID, TARGET_ID, OTHER_IDS[0]
    fly, swim = "be able to fly", "breathe underwater"
    soon = int(time.time()) + 45
    rounds = {
        "1": {"a": [a, b], "b": [c], "q": f"{fly} OR {swim}"},
        "2": {"a": [a], "b": [b], "q": "never eat cheese again OR never eat bread again"},
    }
    return [
        ("Round — just opened", build_round_layout(fly, swim, [], [], True, 1)),
        ("Round — votes in, two shown, timed, two queued", build_round_layout(
            fly, swim, [a, b, *OTHER_IDS], [c], True, 3,
            name_fn=name_fn, shown=[a, c], advance_at=soon, queued=2,
        )),
        ("Round — scheduled game (Next on the card)", build_round_layout(
            fly, swim, [a], [b], True, 2, scheduled=True,
        )),
        ("Round — waiting for a question", build_round_layout(
            "", "", [], [], True, 4, waiting=True,
        )),
        ("Round — over", build_round_layout(
            fly, swim, [a, b], [c], True, 3, closed=True, name_fn=name_fn, shown=[a],
        )),
        ("Host controls — ephemeral, from /games host", build_host_panel_layout()),
        ("Recap — round cap, payout", build_recap_layout(
            rounds, reason="round_cap",
            payout_line="🪙 +5 to everyone who played",
        )),
        ("Recap — no votes cast", build_recap_layout(
            {"1": {"a": [], "b": [], "q": f"{fly} OR {swim}"}}, reason="ended",
        )),
    ]


def nhie_samples(guild: discord.Guild, avatar_url: str):
    """Every card ``bot_modules/games/nhie/layouts.py`` can produce: the round
    card (lives off; lives on with someone out and a countdown; scheduled;
    waiting; closed), the host's ephemeral panel, and the recap (a winner
    with a payout; the guilt board at the round cap; a wipe-out; no scores)."""
    del guild, avatar_url
    from bot_modules.games.nhie.layouts import (
        build_host_panel_layout,
        build_recap_layout,
        build_round_layout,
    )

    name_fn = _placeholder_names()
    a, b, c, d = REQUESTER_ID, TARGET_ID, *OTHER_IDS[:2]
    soon = int(time.time()) + 45
    lives = {a: 3, b: 1, c: 2, d: 0}
    scores = {str(a): 1, str(b): 4, str(c): 2, str(d): 3}
    return [
        ("Round — lives off", build_round_layout("gone skydiving", [a], [b, c], 1, name_fn=name_fn)),
        ("Round — lives on, one out, timed, one queued", build_round_layout(
            "lied about my age", [a, b], [c], 4, lives=lives, eliminated={d},
            name_fn=name_fn, advance_at=soon, queued=1,
        )),
        ("Round — scheduled game (Next on the card)", build_round_layout(
            "eaten a whole cake alone", [], [], 2, name_fn=name_fn, scheduled=True,
        )),
        ("Round — waiting for a statement", build_round_layout(
            "", [], [], 5, waiting=True, name_fn=name_fn,
        )),
        ("Round — over", build_round_layout(
            "lied about my age", [a, b], [c], 4, closed=True, lives=lives, eliminated={d},
            name_fn=name_fn,
        )),
        ("Host controls — ephemeral, from /games host", build_host_panel_layout()),
        ("Recap — last one standing, payout", build_recap_layout(
            a, scores, name_fn=name_fn, payout_line="🪙 +20 to winners • +5 to everyone who played",
        )),
        ("Recap — guilt board at the round cap", build_recap_layout(
            None, scores, name_fn=name_fn, ended=True, reason="round_cap",
        )),
        ("Recap — everyone eliminated", build_recap_layout(None, scores, name_fn=name_fn)),
        ("Recap — no scores", build_recap_layout(None, {}, name_fn=name_fn, ended=True)),
    ]


def mlt_samples(guild: discord.Guild, avatar_url: str):
    """Every card ``bot_modules/games/mlt/layouts.py`` can produce: the lobby
    (empty, populated with a countdown, with the strip, closed), the round
    card (open, scheduled, waiting, closed), the host's ephemeral panel, the
    round results (a winner, a tie, no votes) and the final standings (with
    a payout at the round cap; no crowns)."""
    del guild, avatar_url
    from bot_modules.games.mlt.layouts import (
        build_final_standings_layout,
        build_host_panel_layout,
        build_lobby_layout,
        build_results_layout,
        build_round_layout,
    )

    name_fn = _placeholder_names()
    ids = [REQUESTER_ID, TARGET_ID, *OTHER_IDS[:2]]
    names = [name_fn(uid) for uid in ids]
    options = [(name, str(uid)) for uid, name in zip(ids, names)]
    soon = int(time.time()) + 45
    prompt = "win a staring contest"
    return [
        ("Lobby — empty", build_lobby_layout("Ada Sample", [])),
        ("Lobby — populated, countdown", build_lobby_layout("Ada Sample", names, start_at=soon + 600)),
        ("Lobby — with the lobby strip", build_lobby_layout("Ada Sample", names[:2], **LOBBY_ART)),
        ("Lobby — closed (the game has started)", build_lobby_layout("Ada Sample", names, closed=True)),
        ("Round — open, timed, one queued", build_round_layout(prompt, 2, 0, options, advance_at=soon, queued=1)),
        ("Round — scheduled game (Next on the card)", build_round_layout(prompt, 2, 0, options, scheduled=True)),
        ("Round — waiting for a prompt", build_round_layout("", 3, 0, options, waiting=True)),
        ("Round — over", build_round_layout(prompt, 2, 3, options, closed=True)),
        ("Host controls — ephemeral, from /games host", build_host_panel_layout()),
        ("Round results — a winner", build_results_layout(
            prompt, 2, {ids[0]: 2, ids[1]: 1, ids[2]: 0, ids[3]: 0}, name_fn=name_fn, winners=[ids[0]],
        )),
        ("Round results — a tie at the top", build_results_layout(
            prompt, 3, {ids[0]: 2, ids[1]: 2, ids[2]: 0}, name_fn=name_fn, winners=[ids[0], ids[1]],
        )),
        ("Round results — no votes", build_results_layout(prompt, 4, {}, name_fn=name_fn)),
        ("Final standings — round cap, payout", build_final_standings_layout(
            {str(ids[0]): 3, str(ids[1]): 3, str(ids[2]): 1}, name_fn=name_fn, reason="round_cap",
            payout_line="🪙 +20 to winners • +5 to everyone who played",
        )),
        ("Final standings — no crowns", build_final_standings_layout({}, name_fn=name_fn)),
    ]


def legitlibs_samples(guild: discord.Guild, avatar_url: str):
    """Every card ``games/legitlibs/layouts.py`` can produce: the lobby
    (short of players, ready, with the strip, closed), the host's ephemeral
    panel, the four fill cards (one closed), the Quiplash reveal (anonymous,
    attributed), Who Wrote What with Another One, no submissions, Classic's
    reveal with Another One, and the cancelled notice."""
    del guild, avatar_url
    from bot_modules.games.legitlibs.layouts import (
        build_cancelled_layout,
        build_cast_layout,
        build_classic_fill_layout,
        build_classic_reveal_layout,
        build_host_panel_layout,
        build_lobby_layout,
        build_no_submissions_layout,
        build_quiplash_fill_layout,
        build_rescue_claim_layout,
        build_rescue_fill_layout,
        build_reveal_layout,
    )
    from bot_modules.games.legitlibs.rendering import (
        render_filled_body,
        render_redacted_body,
    )

    name_fn = _placeholder_names()
    names = [name_fn(uid) for uid in (REQUESTER_ID, TARGET_ID, *OTHER_IDS[:2])]
    title = "A Night to Remember"
    body = "Last night I {b1} with a {b2} until the {b3} showed up."
    blanks = [{"id": "b1", "position": 1}, {"id": "b2", "position": 2}, {"id": "b3", "position": 3}]
    fills = {"b1": "danced", "b2": "flamingo", "b3": "landlord"}
    filled = render_filled_body(body, blanks, fills)
    attributed = (
        f"Last night I **danced** (-||{names[0]}||) with a **flamingo** (-||{names[1]}||) "
        f"until the **landlord** (-||{names[2]}||) showed up."
    )
    soon = int(time.time()) + 100
    return [
        ("Lobby — short of players", build_lobby_layout("Ada Sample", title, 2, "classic", 1, 3)),
        ("Lobby — ready, Quiplash", build_lobby_layout("Ada Sample", title, 3, "quiplash", 4, 2)),
        ("Lobby — with the lobby strip", build_lobby_layout("Ada Sample", title, 2, "classic", 2, 3, **LOBBY_ART)),
        ("Lobby — closed (the round has started)", build_lobby_layout("Ada Sample", title, 2, "classic", 3, 3, closed=True)),
        ("Host controls — ephemeral, from /games host", build_host_panel_layout()),
        ("Quiplash — fill phase", build_quiplash_fill_layout(
            title, 3, 4, 1, soon, render_redacted_body(body, blanks),
        )),
        ("Classic — fill phase", build_classic_fill_layout(title, 2, 3, 1, soon)),
        ("Classic — rescue round", build_rescue_claim_layout(title, 2, 2, names[:2], soon)),
        ("Classic — rescue fill, closed", build_rescue_fill_layout(title, 2, 2, 2, soon, closed=True)),
        ("Quiplash reveal — anonymous", build_reveal_layout(title, 3, filled, 2, 4)),
        ("Quiplash reveal — the only submission", build_reveal_layout(title, 3, filled, 1, 1, by=names[0])),
        ("Who Wrote What — with Another One", build_cast_layout(
            [f"**#{i}** — {name}" for i, name in enumerate(names, 1)], another=True,
        )),
        ("No submissions — with Another One", build_no_submissions_layout(title, another=True)),
        ("Classic reveal — with Another One", build_classic_reveal_layout(
            title, 2, attributed, names[:3], another=True,
        )),
        ("Cancelled", build_cancelled_layout()),
    ]


def ama_samples(guild: discord.Guild, avatar_url: str):
    """Every card ``bot_modules/games/ama/layouts.py`` can produce: the board
    (lobby, hot seat with a queue and progress, open panel empty and full,
    closed), the host's ephemeral panel (both formats), the question card
    (pinging, named, passed, ended), the answer, the screened DM and its
    receipt, the asker's DM (one, batched) and the recap."""
    del guild, avatar_url
    from bot_modules.games.ama.layouts import (
        STATE_ENDED,
        STATE_PASSED,
        build_answered_layout,
        build_asker_dm_layout,
        build_board_layout,
        build_host_panel_layout,
        build_question_layout,
        build_recap_layout,
        build_screened_dm_layout,
    )
    from bot_modules.games.ama.logic import AMA_FORMAT_HOT_SEAT, AMA_FORMAT_PANEL

    name_fn = _placeholder_names()
    a, b, c = REQUESTER_ID, TARGET_ID, OTHER_IDS[0]
    payload = {
        "questions": [{"status": "answered"}] * 5 + [{"status": "passed"}] + [{"status": "approved"}] * 2,
        "total_answered": 5, "total_passed": 1,
    }
    question = "What's the pettiest thing you've ever held a grudge over?"
    answer = "A borrowed pen in 2014. I know exactly who has it."
    return [
        ("Board — lobby, no hot seat", build_board_layout("Ada Sample", "unfiltered", game_format=AMA_FORMAT_HOT_SEAT)),
        ("Board — hot seat, two queued, progress", build_board_layout(
            "Ada Sample", "screened", game_format=AMA_FORMAT_HOT_SEAT, hot_seat_name=name_fn(b),
            questions_this_turn=1, queue=[a, c], name_fn=name_fn, payload=payload,
        )),
        ("Board — open panel, empty", build_board_layout("Ada Sample", "unfiltered", game_format=AMA_FORMAT_PANEL)),
        ("Board — open panel, three answering", build_board_layout(
            "Ada Sample", "unfiltered", game_format=AMA_FORMAT_PANEL, panel=[a, b, c], name_fn=name_fn, payload=payload,
        )),
        ("Board — closed (after the recap)", build_board_layout(
            "Ada Sample", "unfiltered", game_format=AMA_FORMAT_HOT_SEAT, hot_seat_name=name_fn(b), payload=payload, closed=True,
        )),
        ("Host controls — hot seat format", build_host_panel_layout(AMA_FORMAT_HOT_SEAT)),
        ("Host controls — open panel format", build_host_panel_layout(AMA_FORMAT_PANEL)),
        ("Question — cached seat (pings)", build_question_layout(question, target_id=b)),
        ("Question — uncached seat (named)", build_question_layout(question, target_name=name_fn(c))),
        ("Question — passed", build_question_layout(question, target_id=b, state=STATE_PASSED)),
        ("Question — AMA ended", build_question_layout(question, target_id=b, state=STATE_ENDED)),
        ("Question + Answer", build_answered_layout(question, answer, name_fn(b))),
        ("Screened DM — awaiting the host", build_screened_dm_layout(question, name_fn(b), "<#100000000000000001>")),
        ("Screened DM — approved", build_screened_dm_layout(question, name_fn(b), "<#100000000000000001>", receipt="✅ Question approved.")),
        ("Asker's DM — one answer", build_asker_dm_layout("<#100000000000000001>", answer_url="https://discord.com/channels/1/2/3")),
        ("Asker's DM — three answers", build_asker_dm_layout("<#100000000000000001>", answer_url="https://discord.com/channels/1/2/3", answer_count=3)),
        ("Recap", build_recap_layout(
            "unfiltered", {"total_q": 8, "total_answered": 5, "total_passed": 1, "rotations": 3, "unique_askers": 4},
            payout_line="🪙 +5 to everyone who played",
        )),
    ]


def price_samples(guild: discord.Guild, avatar_url: str):
    """Every card ``bot_modules/games/price/layouts.py`` can produce: the
    lobby (empty, populated with a countdown, with the strip, closed), the
    host's ephemeral panel, the starting frame, the scenario wait (host,
    players), the submission board (open, closed), the reveal, the vote,
    the round results (a tie), the recap (with a payout; bare) and the two
    notices."""
    del guild, avatar_url
    from bot_modules.games.price.layouts import (
        CANCELLED_TEXT,
        PICKING_UP_TEXT,
        build_host_panel_layout,
        build_lobby_layout,
        build_notice_layout,
        build_recap_layout,
        build_reveal_layout,
        build_round_results_layout,
        build_scenario_layout,
        build_scenario_wait_layout,
        build_start_layout,
        build_vote_layout,
    )

    name_fn = _placeholder_names()
    ids = [REQUESTER_ID, TARGET_ID, *OTHER_IDS[:2]]
    names = [name_fn(uid) for uid in ids]
    scenario = "eat a live cricket on camera"
    ladder = list(zip(names, (0, 250, 5000, 2_000_000)))
    soon = int(time.time()) + 30
    awards = {
        "cheapest": ("💸 Cheapest Date", [names[0]], "averaged $125"),
        "priciest": ("💰 Highest Roller", [names[3]], "averaged $1,000,000"),
        "reasonable": ("🎯 Most Reasonable", [names[1], names[2]], "2 wins each"),
    }
    return [
        ("Lobby — empty", build_lobby_layout("Ada Sample", [], 5, "bank")),
        ("Lobby — populated, countdown, host writes", build_lobby_layout(
            "Ada Sample", names, 5, "host", start_at=soon + 600, min_players=2,
        )),
        ("Lobby — with the lobby strip", build_lobby_layout("Ada Sample", names[:2], 5, "players", **LOBBY_ART)),
        ("Lobby — closed (the game has started)", build_lobby_layout("Ada Sample", names, 5, "bank", closed=True)),
        ("Host controls — ephemeral, from /games host", build_host_panel_layout()),
        ("Starting up", build_start_layout("Ada Sample", 1, 5)),
        ("Scenario wait — the host writes (from the panel)", build_scenario_wait_layout(2, 5, "host")),
        ("Scenario wait — anyone writes (on the card)", build_scenario_wait_layout(2, 5, "players")),
        ("Submission board — open", build_scenario_layout(scenario, 2, 5, soon, 3, 4, roster_submitted=2)),
        ("Submission board — closed", build_scenario_layout(scenario, 2, 5, soon, 4, 4, roster_submitted=4, closed=True)),
        ("Reveal", build_reveal_layout(scenario, 2, 5, ladder)),
        ("Vote", build_vote_layout(scenario, 2, 5, soon, [(f"{n} — ${a:,}", str(u)) for (n, a), u in zip(ladder, ids)])),
        ("Round results — a tie for Unhinged", build_round_results_layout(
            2, 5, [names[1]], 250, 3, [names[2], names[3]], 2_000_000, 2,
        )),
        ("Recap — awards, highlight, payout", build_recap_layout(
            5, 4, awards, "Round 2 had the widest spread — $0 to $2,000,000",
            payout_line="🪙 +20 to winners • +5 to everyone who played",
        )),
        ("Recap — bare", build_recap_layout(1, 2, {}, None)),
        ("Notice — cancelled lobby", build_notice_layout(CANCELLED_TEXT)),
        ("Notice — restart pick-up", build_notice_layout(PICKING_UP_TEXT)),
    ]


def hottakes_samples(guild: discord.Guild, avatar_url: str):
    """Every card ``bot_modules/games/hottakes/layouts.py`` can produce: the
    lobby (empty, with takes and a countdown, with the strip, closed once
    voting opens), the host's ephemeral panel, the take card (timed and
    open, host-paced with votes in, closed), the recap (three takes with a
    payout; one take) and the cancelled notice."""
    del guild, avatar_url  # every Hot Takes card names nobody
    from bot_modules.games.hottakes.layouts import (
        CANCELLED_TEXT,
        build_host_panel_layout,
        build_lobby_layout,
        build_notice_layout,
        build_recap_layout,
        build_vote_layout,
    )

    soon = int(time.time()) + 45
    results = [
        {"text": "Pineapple belongs on pizza", "avg": 4.6, "std": 0.5, "voters": [1, 2, 3, 4]},
        {"text": "Cereal is a soup", "avg": 1.8, "std": 0.4, "voters": [1, 2, 3]},
        {"text": "Mornings are the best part of the day", "avg": 3.0, "std": 2.2, "voters": [2, 3, 4]},
    ]
    return [
        ("Lobby — empty", build_lobby_layout("Ada Sample")),
        ("Lobby — takes in, countdown", build_lobby_layout("Ada Sample", 4, start_at=soon + 600)),
        ("Lobby — with the lobby strip", build_lobby_layout("Ada Sample", 2, **LOBBY_ART)),
        ("Lobby — closed (voting has started)", build_lobby_layout("Ada Sample", 5, closed=True)),
        ("Host controls — ephemeral, from /games host", build_host_panel_layout()),
        ("Take card — timed, open", build_vote_layout("Pineapple belongs on pizza", 1, 5, {}, advance_at=soon)),
        ("Take card — host-paced, votes in", build_vote_layout(
            "Cereal is a soup", 2, 5, {1: 0, 2: 0, 3: 1, 4: 4},
        )),
        ("Take card — closed", build_vote_layout("Cereal is a soup", 2, 5, {1: 0, 2: 0, 3: 1, 4: 4}, closed=True)),
        ("Recap — three takes, payout", build_recap_layout(
            results, payout_line="🪙 +20 to the hottest take • +5 to everyone who played",
        ) or []),
        ("Recap — one take", build_recap_layout(results[:1]) or []),
        ("Notice — cancelled lobby", build_notice_layout(CANCELLED_TEXT)),
    ]


def fantasies_samples(guild: discord.Guild, avatar_url: str):
    """Every card ``bot_modules/games/fantasies/layouts.py`` can produce: the
    panel (fresh with a countdown, rounds in, with the strip, ended), the
    host's ephemeral panel, the submissions card (open, closed), the entry
    card (timed and open, host-paced with votes in, closed) and the recap
    (with a payout)."""
    del guild, avatar_url  # every Fantasies card names nobody
    from bot_modules.games.fantasies.layouts import (
        build_host_panel_layout,
        build_lobby_layout,
        build_recap_layout,
        build_submit_layout,
        build_vote_layout,
    )

    soon = int(time.time()) + 45
    results = [
        {"text": "A weekend with no phones", "category": "Fantasy", "same_pct": 0.9, "voters": [1, 2, 3, 4]},
        {"text": "Talking through the whole film", "category": "Dealbreaker", "same_pct": 0.5, "voters": [1, 2, 3, 4]},
        {"text": "Pineapple on everything", "category": "Fantasy", "same_pct": 0.1, "voters": [2, 3, 4]},
    ]
    return [
        ("Panel — fresh, countdown", build_lobby_layout("Ada Sample", start_at=soon + 600)),
        ("Panel — two rounds in", build_lobby_layout("Ada Sample", 2)),
        ("Panel — with the lobby strip", build_lobby_layout("Ada Sample", 1, **LOBBY_ART)),
        ("Panel — ended", build_lobby_layout("Ada Sample", 3, ended=True)),
        ("Host controls — ephemeral, from /games host", build_host_panel_layout()),
        ("Submissions — open", build_submit_layout(2)),
        ("Submissions — closed", build_submit_layout(2, closed=True)),
        ("Entry card — timed, open", build_vote_layout(
            entry_text="A weekend with no phones", entry_num=1, category="Fantasy",
            same_votes=[], nope_votes=[], total_entries=3, advance_at=soon,
        )),
        ("Entry card — host-paced, votes in", build_vote_layout(
            entry_text="Talking through the whole film", entry_num=2, category="Dealbreaker",
            same_votes=[1, 2], nope_votes=[3], total_entries=3,
        )),
        ("Entry card — closed", build_vote_layout(
            entry_text="Talking through the whole film", entry_num=2, category="Dealbreaker",
            same_votes=[1, 2], nope_votes=[3, 4], total_entries=3, closed=True,
        )),
        ("Recap — payout", build_recap_layout(
            results, payout_line="🪙 +5 to everyone who played",
        ) or []),
    ]


def ttl_samples(guild: discord.Guild, avatar_url: str):
    """Every card ``bot_modules/games/ttl/layouts.py`` can produce: the
    lobby (empty with a prompt, with players, with the strip, closed once
    guessing starts), the host's ephemeral panel, the guess card (host-paced
    with votes in, timed, closed), the reveal and the recap (winners pinged
    from the card, one departed member named)."""
    del guild, avatar_url
    from bot_modules.games.ttl.layouts import (
        build_guess_layout,
        build_host_panel_layout,
        build_lobby_layout,
        build_recap_layout,
        build_reveal_layout,
    )

    name_fn = _placeholder_names()
    names = [name_fn(uid) for uid in (REQUESTER_ID, TARGET_ID, *OTHER_IDS[:2])]
    statements = ["I've climbed a volcano", "I once met a Beatle", "I can't ride a bike"]
    soon = int(time.time()) + 60
    stats = {
        "best_liar": [str(REQUESTER_ID)], "most_fooled_count": 3,
        "most_honest": [str(TARGET_ID)], "least_fooled_count": 0,
        "best_guesser": [str(OTHER_IDS[0]), "999999999999999999"], "max_correct": 2,
    }
    return [
        ("Lobby — empty, with a prompt", build_lobby_layout("your worst first date", [])),
        ("Lobby — players in", build_lobby_layout(None, names)),
        ("Lobby — with the lobby strip", build_lobby_layout("your worst first date", names[:2], **LOBBY_ART)),
        ("Lobby — closed (guessing has started)", build_lobby_layout(None, names, closed=True)),
        ("Host controls — ephemeral, from /games host", build_host_panel_layout()),
        ("Guess card — host-paced, votes in", build_guess_layout(
            names[0], statements, {1: 0, 2: 2, 3: 2}, prompt="your worst first date",
        )),
        ("Guess card — timed, fresh", build_guess_layout(names[1], statements, {}, advance_at=soon)),
        ("Guess card — closed", build_guess_layout(names[0], statements, {1: 0, 2: 2, 3: 2}, closed=True)),
        ("Reveal", build_reveal_layout(names[0], statements, 1, names[1:3], names[3:])),
        ("Recap — winners pinged from the card, one departed", build_recap_layout(
            stats, name_fn=name_fn, pingable=[REQUESTER_ID, TARGET_ID, OTHER_IDS[0]],
            payout_line="🪙 +20 to the Best Liar and Best Guesser • +5 to everyone who played",
        )),
    ]


def ffa_samples(guild: discord.Guild, avatar_url: str):
    """Every card ``bot_modules/games/ffa/layouts.py`` can produce: the
    prompt card (a fresh truth, a dare with replies, closed), the host's
    ephemeral panel and the recap."""
    del guild, avatar_url  # every FFA card names nobody
    from bot_modules.games.ffa.layouts import (
        build_host_panel_layout,
        build_prompt_layout,
        build_recap_layout,
    )

    summary = {
        "per_prompt": [
            ("TRUTH", "What's a small lie you tell people all the time?", 4),
            ("DARE", "Send the last photo in your camera roll.", 1),
        ],
        "total_replies": 5,
        "busiest": ("TRUTH", "What's a small lie you tell people all the time?", 4),
        "repliers": [1, 2, 3],
        "prompt_count": 2,
    }
    return [
        ("Prompt card — fresh truth", build_prompt_layout("What's a small lie you tell people all the time?", "TRUTH")),
        ("Prompt card — dare, replies in", build_prompt_layout("Send the last photo in your camera roll.", "DARE", reply_count=3)),
        ("Prompt card — closed", build_prompt_layout("Send the last photo in your camera roll.", "DARE", reply_count=3, closed=True)),
        ("Host controls — ephemeral, from /games host", build_host_panel_layout()),
        ("Recap — payout", build_recap_layout(summary, payout_line="🪙 +5 to everyone who replied")),
    ]


def mahjong_samples(guild: discord.Guild, avatar_url: str):
    """Every card ``bot_modules/games/mahjong/layouts.py`` can produce, driven
    through the real engine: the member panel, the table card in its phases
    (open lobby, Charleston, claim window, settled, closed), the rack panel
    with a coach readout, the reveal, the settlement, a joker redemption,
    How to Play, the card viewer's first page and the stats card."""
    import random

    from bot_modules.games.mahjong import game_logic as G
    from bot_modules.games.mahjong import layouts as mj
    from bot_modules.games.mahjong.card_logic import load_first_light
    from bot_modules.games.mahjong.game_logic import Phase, TableConfig
    from bot_modules.games.mahjong.mahjong_service import MahjongSettings, escrow_amount
    from bot_modules.games.mahjong.tiles import Tile, shuffled_wall

    del guild, avatar_url
    season = load_first_light()
    name_fn = _placeholder_names()
    names = {REQUESTER_ID: name_fn(REQUESTER_ID), TARGET_ID: name_fn(TARGET_ID)}
    rng = random.Random(5)
    state = G.create_table(TableConfig(seat_count=2), REQUESTER_ID)
    lobby_open = state
    state, _ = G.join_table(state, TARGET_ID)
    state, _ = G.deal(state, shuffled_wall(rng))
    charleston = state
    for _ in range(3):
        for seat in range(2):
            picks = [t for t in state.seats[seat].rack if t is not Tile.JOKER][:3]
            state, _ = G.charleston_pick(state, seat, picks, 0, rng)
    state, _ = G.vote_second_charleston(state, 0, False)
    state, _ = G.courtesy_propose(state, 0, 0)
    state, _ = G.courtesy_propose(state, 1, 0)
    await_discard = state
    state, _ = G.discard(state, 0, state.seats[0].rack[0], season)
    claim_window = state
    out = G.Outcome(kind="mahjong", winner=1, line_id="gh-1", line_name="Golden Hour",
                    value=25, won_by="discard", jokerless_double=True, discarder=0,
                    point_deltas={0: -100, 1: 100})
    settled = G.GameState(config=TableConfig(seat_count=2), host=REQUESTER_ID,
                          seats=list(await_discard.seats), phase=Phase.SETTLE, hand_no=1, outcome=out)
    closed, _ = G.close_table(settled, "closed")
    assist = G.assist_readout(await_discard, 0, season, "coach")
    escrow = escrow_amount(season, 2, 1)
    return [
        ("Member panel — /mahjong", mj.build_member_panel(
            season, (1, 2, 5), 250, escrow_for=lambda seats, stake: escrow_amount(season, seats, stake),
        )),
        ("Table card — open lobby", mj.build_table_panel(lobby_open, names, 1, escrow, time.time() + 900)),
        ("Table card — Charleston", mj.build_table_panel(charleston, names, 1, escrow, time.time() + 60)),
        ("Table card — claim window", mj.build_table_panel(claim_window, names, 1, escrow, time.time() + 8)),
        ("Table card — settled", mj.build_table_panel(settled, names, 1, escrow, time.time() + 600)),
        ("Table card — closed", mj.build_table_panel(closed, names, 1, 0, closed_reason="closed")),
        ("Table controls — ephemeral, from /games host", mj.build_host_panel_layout()),
        ("Rack panel — your turn, coach readout", mj.build_rack_panel(
            await_discard, 0, time.time() + 120, assist=assist,
        )),
        ("Mahjong reveal", mj.build_mahjong_reveal(out, names[TARGET_ID], "1D 1D 1D  ·  3D 3D 3D")),
        ("Settlement", mj.build_settlement(out, {0: names[REQUESTER_ID], 1: names[TARGET_ID]}, 2)),
        ("Joker redeemed", mj.build_joker_redeemed(names[REQUESTER_ID], names[TARGET_ID], Tile("5b"))),
        ("How to Play", mj.build_how_to_play(season, MahjongSettings(), dashboard_base())),
        ("Card viewer — page 1", mj.build_card_viewer(season)[0]),
        ("Your record", mj.build_my_stats([
            {"mode": 2, "hands_played": 4, "wins": 2, "jokerless_wins": 1,
             "coins_won": 300, "coins_lost": 100, "biggest_win": 150},
        ])),
    ]


def duels_samples(guild: discord.Guild, avatar_url: str):
    """Every card ``bot_modules/duels/layouts.py`` can produce: the two
    *live* cards (the pending challenge, the join lobby) and the thirteen
    terminal off-ramps of the shared duel/lobby layer, plus the one card
    shape the render-hooks seam (``base_game.py``/``base_duel.py``) adds —
    the composed result card a layout-shaped mini-game's items land inside,
    in both stake modes. Every mini-game cog reachable through this shared
    layer is on the layout arm now (wave 3b), which is what let the two live
    cards convert alongside the terminal notices — see ``duels/layouts.py``'s
    module docstring. A converted mini-game's own in-progress and result
    cards get their own sample entry instead (see
    ``hot_potato_group_samples``) — this function only covers the shared
    shell every mini-game reuses.

    The two live cards reuse the real ``ChallengeView`` / ``LobbyView`` for
    their control rows (Accept/Decline; Join/Leave/Start/Cancel), and the two
    result cards reuse ``ResultLayoutView`` itself (not a hand-copy) for the
    names line, the fake mini-game item and the control row — exactly as
    ``auctions_samples`` reuses ``AuctionBidButton``. The buttons are plain,
    not ``DynamicItem``, so ``sample_prefix_ids`` rewrites their
    ``custom_id``s the same as every other card's."""
    from bot_modules.duels.base_game import BaseGame
    from bot_modules.duels.db import NAMING_WINDOW_SECONDS
    from bot_modules.duels.layouts import (
        build_challenge_layout,
        build_duel_notice_layout,
        build_lobby_layout,
        build_nick_reverted_layout,
    )
    from bot_modules.duels.lobby import LobbyView
    from bot_modules.duels.views import ChallengeView, ResultLayoutView

    del guild, avatar_url  # no member is pictured on a bare receipt
    name_fn = _placeholder_names()
    challenge = ChallengeView(
        999999999999999999, TARGET_ID,
        _sample_noop_rematch_hook, _sample_noop_rematch_hook,
    )
    challenge.render(build_challenge_layout(
        "Quickdraw", REQUESTER_ID, TARGET_ID,
        "💰 💎 **50** gems each — winner takes 💎 **100** gems.",
        wager=50, expires_at=int(time.time()) + 60, name_fn=name_fn,
    ))
    lobby = LobbyView(
        999999999999999999, REQUESTER_ID,
        _sample_noop_rematch_hook, _sample_noop_rematch_hook,
        _sample_noop_rematch_hook, _sample_noop_rematch_hook,
    )
    lobby.render(build_lobby_layout(
        "Hot Potato Group",
        "Pass it along before the timer runs out. Last one holding loses.",
        [REQUESTER_ID, TARGET_ID, OTHER_IDS[0]], REQUESTER_ID,
        2, 10, "💰 💎 **10** gems to join — winner takes the pot.",
        ante=10, pot_text="💎 **30** gems",
        closes_at=time.time() + 300, name_fn=name_fn,
    ))
    return [
        ("Challenge — pending (wagered)", list(challenge.children)),
        ("Lobby — join board (wagered, mid-fill)", list(lobby.children)),
        (
            "Challenge expired",
            build_duel_notice_layout("⏱️ Challenge Expired", "No response in time."),
        ),
        (
            "Challenge declined",
            build_duel_notice_layout(
                "❌ Challenge Declined",
                f"{name_fn(TARGET_ID)} declined the challenge.",
            ),
        ),
        (
            "Challenge called off — ante failure",
            build_duel_notice_layout(
                "❌ Challenge Called Off",
                "The challenger can no longer cover the wager — challenge called off.",
            ),
        ),
        (
            "Lobby expired",
            build_duel_notice_layout(
                "⏱️ Lobby Expired", "Not enough players joined in time."
            ),
        ),
        (
            "Lobby cancelled",
            build_duel_notice_layout(
                "🚫 Lobby Cancelled", f"{name_fn(REQUESTER_ID)} closed the lobby."
            ),
        ),
        (
            "Lobby closed — host left",
            build_duel_notice_layout(
                "🚫 Lobby Closed",
                "The host left the server, so the lobby is closed.",
            ),
        ),
        (
            "Game abandoned",
            build_duel_notice_layout(
                "🏳️ Game Abandoned",
                "No activity in 5m. Game over — no nickname consequences.",
            ),
        ),
        (
            "Game called off — a player left",
            build_duel_notice_layout(
                "🏳️ Game Called Off",
                f"{name_fn(TARGET_ID)} left the server mid-game. No result, "
                "no nickname — any stakes are refunded.",
            ),
        ),
        (
            "Nickname not set — naming window lapsed",
            build_duel_notice_layout(
                "⏰ Nickname Not Set",
                f"The winner didn't name the loser within "
                f"{BaseGame._span(NAMING_WINDOW_SECONDS)}. No rename applied.",
            ),
        ),
        (
            "Nickname not set — winner left",
            build_duel_notice_layout(
                "🚪 Nickname Not Set",
                "The winner left the server. No rename applied.",
            ),
        ),
        (
            "Nickname not set — loser left mid-round",
            build_duel_notice_layout(
                "🚪 Nickname Not Set",
                "The loser left the server, so there's nobody to rename. The win stands.",
            ),
        ),
        (
            "Nickname not set — superseded by a rematch",
            build_duel_notice_layout(
                "🔁 Nickname Not Set",
                "The winner started a new game against the loser before naming "
                "them. No rename applied.",
            ),
        ),
        (
            "Nickname restored — DM",
            build_nick_reverted_layout("Quickdraw", "Old_Nickname"),
        ),
        (
            "Result card — nickname mode (both buttons)",
            list(
                ResultLayoutView(
                    999999999999999999,
                    REQUESTER_ID,
                    TARGET_ID,
                    _sample_noop_rematch_hook,
                    on_rematch=_sample_noop_rematch_hook,
                    rematch_deadline=time.time() + 600,
                    content=[
                        _players_line(REQUESTER_ID, TARGET_ID),
                        # A mini-game's own item, per render_result_state's contract
                        # (see its docstring): describe the outcome, don't re-name
                        # the winner/loser the names line above already named.
                        discord.ui.TextDisplay("🥊 Knockout in round two. Match over."),
                    ],
                ).children
            ),
        ),
        (
            "Result card — custom stakes (Run It Back only)",
            list(
                ResultLayoutView(
                    999999999999999999,
                    REQUESTER_ID,
                    TARGET_ID,
                    None,
                    on_rematch=_sample_noop_rematch_hook,
                    rematch_deadline=time.time() + 600,
                    content=[
                        _players_line(REQUESTER_ID, TARGET_ID),
                        discord.ui.TextDisplay("🥊 Knockout in round two. Match over."),
                    ],
                ).children
            ),
        ),
    ]


def mfk_samples(guild: discord.Guild, avatar_url: str):
    """Every card ``bot_modules/games/mfk/layouts.py`` can produce: the
    lobby (empty, and mid-fill with a countdown) and the post-close
    assignments reveal, with a payout line attached."""
    del guild, avatar_url  # no member is pictured on a bare receipt
    from bot_modules.games.mfk.layouts import build_assignments_layout, build_lobby_layout

    name_fn = _placeholder_names()
    return [
        ("Lobby — empty", build_lobby_layout("Ada Sample", [])),
        (
            "Lobby — filling, with a countdown",
            build_lobby_layout(
                "Ada Sample",
                ["Bo Placeholder", "Cy Example", "Dee Stand-in"],
                start_at=1_700_000_000,
            ),
        ),
        (
            "Assignments reveal",
            build_assignments_layout(
                {
                    REQUESTER_ID: [TARGET_ID, OTHER_IDS[0], OTHER_IDS[1]],
                    TARGET_ID: [REQUESTER_ID, OTHER_IDS[1], OTHER_IDS[2]],
                },
                name_fn=name_fn,
                payout_line="🪙 +20 to winners • +5 to everyone who played",
            ),
        ),
    ]


def compliment_samples(guild: discord.Guild, avatar_url: str):
    """Every card ``bot_modules/games/compliment/layouts.py`` can produce
    (Components V2 wave 3b): the lobby before and after players join, the
    pairings card fresh and at the wrap's halfway repost, and the closing
    Wrap-Up card."""
    from bot_modules.games.compliment.layouts import (
        build_lobby_layout,
        build_pairings_layout,
        build_wrap_recap_layout,
    )

    name_fn = _placeholder_names()
    pairings = {REQUESTER_ID: TARGET_ID, TARGET_ID: OTHER_IDS[0], OTHER_IDS[0]: REQUESTER_ID}

    return [
        ("Lobby — empty pool", build_lobby_layout("Ada Sample", [])),
        ("Lobby — players joined, with countdown", build_lobby_layout(
            "Ada Sample", ["Bo Placeholder", "Cy Example"],
            start_at=int(time.time()) + 300,
        )),
        ("Pairings — just generated", build_pairings_layout(
            pairings, name_fn=name_fn, ends_at=int(time.time()) + 600,
        )),
        ("Pairings — halfway repost", build_pairings_layout(
            pairings, name_fn=name_fn, delivered={REQUESTER_ID},
            ends_at=int(time.time()) + 300,
        )),
        ("Wrap-Up recap", build_wrap_recap_layout(
            2, 3, payout_line="🪙 +20 to winners • +5 to everyone who played",
        )),
    ]


def story_samples(guild: discord.Guild, avatar_url: str):
    """Every card ``bot_modules/games/story/layouts.py`` can produce: the
    join lobby (empty, a few joined, and mid-countdown), the turn panel, and
    the reveal (a short story, and the fullest real one — 30 sentences at
    the modal's 500-char max — trimmed the way the cog trims it)."""
    from bot_modules.games.story.layouts import (
        build_lobby_layout,
        build_story_board_layout,
        build_story_recap_layout,
        build_turn_prompt_layout,
    )
    from bot_modules.games.story.logic import (
        ATTRIBUTION_CARD_BUDGET,
        STORY_TEXT_CARD_BUDGET,
        assemble_story_text,
        build_attribution_lines,
        trim_attribution_lines,
    )

    name_fn = _placeholder_names()
    turn_order = [REQUESTER_ID, TARGET_ID, *OTHER_IDS]

    long_sentences = [
        {
            "author_id": turn_order[i % len(turn_order)] if i else None,
            "text": "It was a dark and stormy night in the old lighthouse. " * 9,
        }
        for i in range(30)
    ]
    long_story_text = assemble_story_text(long_sentences, max_len=STORY_TEXT_CARD_BUDGET)
    long_lines = build_attribution_lines(long_sentences, name_fn)
    long_attribution, long_omitted = trim_attribution_lines(long_lines, ATTRIBUTION_CARD_BUDGET)

    short_sentences = [
        {"author_id": None, "text": "It was a dark and stormy night."},
        {"author_id": REQUESTER_ID, "text": "Suddenly, the lighthouse keeper heard a knock."},
        {"author_id": TARGET_ID, "text": "It was only the cat, demanding dinner."},
    ]
    short_story_text = assemble_story_text(short_sentences, max_len=STORY_TEXT_CARD_BUDGET)
    short_attribution = build_attribution_lines(short_sentences, name_fn)

    return [
        ("Join lobby — empty", build_lobby_layout(
            host_name=name_fn(REQUESTER_ID), visibility="blind", max_sentences=10,
            player_ids=[], name_fn=name_fn,
        )),
        ("Join lobby — a few joined", build_lobby_layout(
            host_name=name_fn(REQUESTER_ID), visibility="full", max_sentences=10,
            player_ids=[REQUESTER_ID, TARGET_ID, OTHER_IDS[0]], name_fn=name_fn,
        )),
        ("Join lobby — advertised start", build_lobby_layout(
            host_name=name_fn(REQUESTER_ID), visibility="blind", max_sentences=15,
            player_ids=[REQUESTER_ID, TARGET_ID], name_fn=name_fn,
            start_at=int(time.time()) + 300,
        )),
        ("Turn panel — board half", build_story_board_layout(
            sentence_count=3, max_sentences=10, current_player_id=TARGET_ID,
            turn_order=turn_order, name_fn=name_fn,
        )),
        ("Turn panel — prompt half", build_turn_prompt_layout(
            current_player_id=TARGET_ID, name_fn=name_fn,
        )),
        ("Reveal — short story", build_story_recap_layout(
            story_text=short_story_text, player_count=2, sentence_count=len(short_sentences),
            attribution_lines=short_attribution,
            footer_line="🪙 +20 to winners • +5 to everyone who played",
        )),
        ("Reveal — fullest real story (30 sentences, trimmed)", build_story_recap_layout(
            story_text=long_story_text, player_count=len(turn_order),
            sentence_count=len(long_sentences),
            attribution_lines=long_attribution, omitted=long_omitted,
            footer_line="🪙 +20 to winners • +5 to everyone who played",
        )),
    ]


def quickdraw_samples(guild: discord.Guild, avatar_url: str):
    """Every card ``bot_modules/duels/quickdraw/layouts.py`` can produce
    (Components V2 wave 3b): the turn card across its states — WAITING, the
    live draw signal (DRAW and WINNER_FIRED render identically on
    purpose — the opponent must stay blind to an already-fired winner) —
    the "Nobody Drew" void receipt, and the composed
    result card's own items in every state ``render_result_state`` can
    return: false start, a clean draw with both reaction times, a clean
    draw where the loser never fired (the ``_fire_void`` winner-only
    path), and each of the three nickname-flow sections attached.
    """
    del avatar_url  # no member is pictured on a bare receipt
    from bot_modules.duels.quickdraw.layouts import (
        build_result_items_layout,
        build_turn_card_layout,
        build_void_layout,
    )

    del guild
    name_fn = _placeholder_names()
    p1_name, p2_name = name_fn(REQUESTER_ID), name_fn(TARGET_ID)
    stakes = "Loser surrenders their nickname for 24 hours."
    return [
        ("Turn card — waiting", build_turn_card_layout("WAITING", p1_name, p2_name, stakes)),
        ("Turn card — draw signal live", build_turn_card_layout("DRAW", p1_name, p2_name, stakes)),
        ("Nobody drew — void receipt", build_void_layout(p1_name, p2_name)),
        (
            "Result — false start",
            build_result_items_layout(clean_draw=False, stakes_text=stakes),
        ),
        (
            "Result — clean draw, both reaction times",
            build_result_items_layout(
                clean_draw=True, stakes_text=stakes,
                winner_reaction=0.187, loser_reaction=0.239,
            ),
        ),
        (
            "Result — clean draw, opponent never fired",
            build_result_items_layout(
                clean_draw=True, stakes_text=stakes, winner_reaction=0.187,
            ),
        ),
        (
            "Result — nickname over to you (winner is the server owner)",
            build_result_items_layout(
                clean_draw=True, stakes_text=stakes,
                winner_reaction=0.187, loser_reaction=0.239,
                nick_heading="🏷️ Nickname — Over To You",
                nick_body=(
                    f"Discord won't let me rename the server owner, so "
                    f"**{p1_name}** has to set **Sir Slowpoke** themselves. "
                    "It stands for 24 hours."
                ),
            ),
        ),
        (
            "Result — nickname applied",
            build_result_items_layout(
                clean_draw=True, stakes_text=stakes,
                winner_reaction=0.187, loser_reaction=0.239,
                nick_heading="🏷️ Nickname Applied",
                nick_body=f"**{p2_name}** is now known as **Sir Slowpoke** for 24 hours.",
            ),
        ),
        (
            "Result — awaiting nickname",
            build_result_items_layout(
                clean_draw=True, stakes_text=stakes,
                winner_reaction=0.187, loser_reaction=0.239,
                nick_heading="⏳ Awaiting Nickname",
                nick_body=f"**{p1_name}**, press **Name the Loser** within 5m. The nickname lasts 24 hours.",
            ),
        ),
    ]


def chicken_samples(guild: discord.Guild, avatar_url: str):
    """Every card ``bot_modules/duels/chicken/layouts.py`` can produce: the
    live climb board inside its own ``ChickenView`` (Bail button attached,
    same as a real message), the crash-with-loser result composed inside
    ``ResultLayoutView`` the way ``BaseGame._result_layout`` actually builds
    it (names line first, nickname sub-section variants), and the two
    cosmetic outcomes (everyone blinked, total wipeout) edited in through
    ``duels.layouts.card`` outside the base's result seam."""
    del guild, avatar_url  # no member is pictured on a bare receipt or board
    from bot_modules.duels.chicken.layouts import (
        awaiting_nick_section,
        build_crash_result_layout,
        build_everyone_blinked_layout,
        build_round_layout,
        build_total_wipeout_layout,
        nick_applied_section,
        nick_self_apply_section,
    )
    from bot_modules.duels.chicken.views import ChickenView
    from bot_modules.duels.layouts import card as duel_card
    from bot_modules.duels.views import ResultLayoutView

    name_fn = _placeholder_names()
    game = SimpleNamespace(
        alive=[REQUESTER_ID],
        bail_log=[{"player_id": TARGET_ID, "meter_pct": 30.0}, {"player_id": OTHER_IDS[0], "meter_pct": 55.0}],
        stakes_text=None,
    )

    def _round_view(g, pct):
        view = ChickenView(999999999999999999, _sample_noop_rematch_hook)
        view.render(build_round_layout(g, pct=pct, name_fn=name_fn))
        return list(view.children)

    def _crash_result(nick_section):
        return list(
            ResultLayoutView(
                999999999999999999,
                REQUESTER_ID,
                TARGET_ID,
                _sample_noop_rematch_hook,
                on_rematch=_sample_noop_rematch_hook,
                rematch_deadline=time.time() + 600,
                content=[
                    _players_line(REQUESTER_ID, TARGET_ID),
                    *build_crash_result_layout(
                        blew_at_pct=62.0,
                        n_alive=2,
                        bravest_pct=75.0,
                        stakes="Loser surrenders their nickname for 24 hours.",
                        nick_section=nick_section,
                    ),
                ],
            ).children
        )

    return [
        ("Live climb board", _round_view(game, 45.0)),
        (
            "Crash result — awaiting nickname",
            _crash_result(awaiting_nick_section(time.time() + 3600, "24 hours")),
        ),
        (
            "Crash result — nickname applied",
            _crash_result(nick_applied_section("Chicken Little", "24 hours")),
        ),
        (
            "Crash result — server owner sets it themselves",
            _crash_result(nick_self_apply_section("The Boss", "24 hours")),
        ),
        (
            "Everyone blinked — ranked",
            duel_card(
                build_everyone_blinked_layout(
                    winner_ref=name_fn(REQUESTER_ID),
                    ranking=[(name_fn(TARGET_ID), 55.0), (name_fn(OTHER_IDS[0]), 30.0)],
                )
            ).children,
        ),
        (
            "Everyone blinked — winner has left",
            duel_card(
                build_everyone_blinked_layout(winner_ref=name_fn(REQUESTER_ID), ranking=[])
            ).children,
        ),
        (
            "Total wipeout",
            duel_card(build_total_wipeout_layout(blew_at_pct=88.0)).children,
        ),
    ]


def musical_chairs_samples(guild: discord.Guild, avatar_url: str):
    """Every card ``bot_modules/duels/musical_chairs/layouts.py`` can produce
    (Components V2 wave 3b): the round-active turn card in both phases, and
    the result receipt in each of its four nickname states. The called-off
    ("nobody sat twice") notice reuses ``duels.layouts.build_duel_notice_layout``
    unchanged and is already in the gallery under ``duels``."""
    del guild, avatar_url  # no member is pictured on a bare receipt
    from bot_modules.duels.musical_chairs.layouts import (
        build_result_layout,
        build_round_layout,
    )

    name_fn = _placeholder_names()
    alive = [REQUESTER_ID, TARGET_ID, OTHER_IDS[0], OTHER_IDS[1]]
    return [
        ("Round — music playing", build_round_layout(
            phase="MUSIC", round_no=2, chairs=3, alive=alive,
            stakes="🏷️ Loser surrenders their nickname for 24 hours.",
            name_fn=name_fn,
        )),
        ("Round — Sit! (scramble)", build_round_layout(
            phase="SCRAMBLE", round_no=2, chairs=3, alive=alive,
            stakes="🏷️ Loser surrenders their nickname for 24 hours.",
            name_fn=name_fn,
        )),
        ("Result — imposed nickname", build_result_layout(
            players=4, stakes="🏷️ Loser surrenders their nickname for 24 hours.",
            nick_stake=True, imposed_nick="Chair Thief", self_apply_nick=None,
            nick_span="24 hours", naming_deadline=int(time.time()) + 1800,
        )),
        ("Result — self-apply (server owner)", build_result_layout(
            players=4, stakes="🏷️ Loser surrenders their nickname for 24 hours.",
            nick_stake=True, imposed_nick=None, self_apply_nick="Chair Thief",
            nick_span="24 hours", naming_deadline=int(time.time()) + 1800,
        )),
        ("Result — awaiting nickname", build_result_layout(
            players=4, stakes="🏷️ Loser surrenders their nickname for 24 hours.",
            nick_stake=True, imposed_nick=None, self_apply_nick=None,
            nick_span="24 hours", naming_deadline=int(time.time()) + 1800,
        )),
        ("Result — custom stakes only (no nickname)", build_result_layout(
            players=4, stakes="💰 🪙 **50** coins each — winner takes 🪙 **200** coins.",
            nick_stake=False, imposed_nick=None, self_apply_nick=None,
            nick_span="24 hours", naming_deadline=int(time.time()) + 1800,
        )),
    ]


def hot_potato_samples(guild: discord.Guild, avatar_url: str):
    """Every card ``bot_modules/duels/hot_potato/layouts.py`` can produce
    (Components V2 wave 3b, the first of §9's six duel/lobby games to
    convert): the live turn card, and the result card in its minimal,
    style-points, and nickname-applied shapes — reusing ``PassLayoutView``
    and ``ResultLayoutView`` themselves, not hand-copies, the same way
    ``duels_samples`` does for the shared result card."""
    del avatar_url
    from bot_modules.duels.hot_potato.game import HotPotatoGame
    from bot_modules.duels.hot_potato.layouts import (
        build_hot_potato_card,
        build_hot_potato_result,
    )
    from bot_modules.duels.hot_potato.views import PassLayoutView
    from bot_modules.duels.views import ResultLayoutView

    name_fn = _placeholder_names()

    async def _noop_pass(interaction: discord.Interaction, game_id: int) -> None:
        return None

    def _turn_card(game: HotPotatoGame) -> list:
        view = PassLayoutView(game.id, _noop_pass)
        view.render(build_hot_potato_card(game, name_fn))
        return list(view.children)

    holding_game = HotPotatoGame(
        id=1, guild_id=guild.id, channel_id=1, challenger_id=REQUESTER_ID,
        target_id=TARGET_ID, state="ACTIVE", holder_id=REQUESTER_ID,
        pass_log=[{"holder_id": REQUESTER_ID, "received_at": 0.0, "passed_at": None}],
    )
    boom_game = HotPotatoGame(
        id=2, guild_id=guild.id, channel_id=1, challenger_id=REQUESTER_ID,
        target_id=TARGET_ID, state="RESOLVED", winner_id=REQUESTER_ID, loser_id=TARGET_ID,
        pass_log=[
            {"holder_id": REQUESTER_ID, "received_at": 0.0, "passed_at": 4.0},
            {"holder_id": TARGET_ID, "received_at": 4.0, "passed_at": None},
        ],
    )

    def _result_card(content: list) -> list:
        return list(
            ResultLayoutView(
                boom_game.id, REQUESTER_ID, TARGET_ID, None,
                on_rematch=_sample_noop_rematch_hook,
                rematch_deadline=time.time() + 600,
                content=[_players_line(REQUESTER_ID, TARGET_ID), *content],
            ).children
        )

    return [
        ("Turn card — holding", _turn_card(holding_game)),
        (
            "Result card — minimal",
            _result_card(
                build_hot_potato_result(4, "Loser surrenders their nickname for 24 hours.")
            ),
        ),
        (
            "Result card — with style points",
            _result_card(
                build_hot_potato_result(
                    6, "Loser surrenders their nickname for 24 hours.",
                    style_lines=[
                        "**🏆 Winner**: +30 pts — now has 174 style points",
                        "**💀 Loser**: +10 pts — now has 40 style points",
                    ],
                )
            ),
        ),
        (
            "Result card — nickname applied",
            _result_card(
                build_hot_potato_result(
                    4, "🏷️ Loser surrenders their nickname for 24 hours.",
                    nick_heading="🏷️ Nickname Applied",
                    nick_body="**The Loser** is now known as **Spudhead** for 24 hours.",
                )
            ),
        ),
    ]


def hot_potato_group_samples(guild: discord.Guild, avatar_url: str):
    """Every card ``bot_modules/duels/hot_potato_group/layouts.py`` can
    produce (Components V2 wave 3b): the live holding card at low and high
    dread (the escalating fuse emoji, and the "Out" line once someone's
    eliminated), and the composed result card — reusing ``ResultLayoutView``
    itself, same as ``duels_samples``'s two result-card entries, since this
    builder's own output is only ever posted *inside* that composition,
    never alone."""
    del avatar_url
    from bot_modules.duels.hot_potato_group.layouts import build_holding_layout, build_result_layout
    from bot_modules.duels.views import ResultLayoutView

    name_fn = _placeholder_names()
    return [
        (
            "Holding card — early round",
            build_holding_layout(
                [REQUESTER_ID, TARGET_ID, *OTHER_IDS], [], REQUESTER_ID, "🥔",
                None, name_fn=name_fn,
            ),
        ),
        (
            "Holding card — high dread, one eliminated",
            build_holding_layout(
                [TARGET_ID, *OTHER_IDS], [REQUESTER_ID], OTHER_IDS[0], "🥔💣💥💥",
                "💰 **50** gems to the winner", name_fn=name_fn,
            ),
        ),
        (
            "Result card — composed",
            list(
                ResultLayoutView(
                    999999999999999999,
                    REQUESTER_ID,
                    TARGET_ID,
                    _sample_noop_rematch_hook,
                    on_rematch=_sample_noop_rematch_hook,
                    rematch_deadline=time.time() + 600,
                    content=[
                        _players_line(REQUESTER_ID, TARGET_ID),
                        *build_result_layout(
                            5,
                            "Loser surrenders their nickname for 24 hours.",
                            bravest_id=OTHER_IDS[0],
                            bravest_seconds=87.0,
                            nick_line="🏷️ **The loser** is now known as "
                            "**Spud** for 24 hours.",
                            name_fn=name_fn,
                        ),
                    ],
                ).children
            ),
        ),
    ]


def pressure_cooker_samples(guild: discord.Guild, avatar_url: str):
    """Every card ``bot_modules/duels/pressure_cooker/layouts.py`` can
    produce (Components V2 wave 3b, "games"): the live turn card at a few
    gauge levels, and the result card in each of its four mutually exclusive
    nickname states, plus the fullest one for a budget check.

    The result cards reuse ``ResultLayoutView`` itself for the names line
    and the button row, exactly as ``duels_samples`` does for the shared
    duel layer's own result cards, so a sample shows the shape members
    actually see.
    """
    del guild, avatar_url  # no member is pictured on either card
    from bot_modules.duels.pressure_cooker.game import PumpEntry
    from bot_modules.duels.pressure_cooker.layouts import build_boom_layout, build_pump_layout
    from bot_modules.duels.views import ResultLayoutView

    name_fn = _placeholder_names()

    def _pumps(count: int, gauge_start: int = 10) -> list[PumpEntry]:
        gauge = gauge_start
        entries: list[PumpEntry] = []
        for i in range(count):
            player = REQUESTER_ID if i % 2 == 0 else TARGET_ID
            entries.append(
                PumpEntry(player_id=player, roll=7, gauge_before=gauge, ts=1_750_000_000.0 + i)
            )
            gauge += 7
        return entries

    def _result(items: list, self_apply: bool = False) -> list:
        return list(
            ResultLayoutView(
                999999999999999999,
                REQUESTER_ID if not self_apply else TARGET_ID,
                TARGET_ID if not self_apply else REQUESTER_ID,
                None if self_apply else _sample_noop_rematch_hook,
                on_rematch=_sample_noop_rematch_hook,
                rematch_deadline=time.time() + 600,
                content=[
                    _players_line(
                        REQUESTER_ID if not self_apply else TARGET_ID,
                        TARGET_ID if not self_apply else REQUESTER_ID,
                    ),
                    *items,
                ],
            ).children
        )

    return [
        (
            "Turn card — fresh, no history",
            build_pump_layout(REQUESTER_ID, TARGET_ID, 10, [], TARGET_ID, "ACTIVE", name_fn=name_fn),
        ),
        (
            "Turn card — mid-game, with recent-pumps history",
            build_pump_layout(
                REQUESTER_ID, TARGET_ID, 61, _pumps(5), REQUESTER_ID, "ACTIVE", name_fn=name_fn,
            ),
        ),
        (
            "Turn card — near bust",
            build_pump_layout(
                REQUESTER_ID, TARGET_ID, 96, _pumps(3, gauge_start=75), TARGET_ID, "ACTIVE",
                name_fn=name_fn,
            ),
        ),
        (
            "Result card — forfeit (no stakes staked, no nickname)",
            _result(build_boom_layout(100, 7, None, "The stake is forfeited.")),
        ),
        (
            "Result card — custom stakes",
            _result(
                build_boom_layout(
                    100, 4, "loser buys the next round", "The stake is forfeited.",
                )
            ),
        ),
        (
            "Result card — nickname applied",
            _result(
                build_boom_layout(
                    100, 9, None, "The stake is forfeited.",
                    imposed_copy="Now going by **Doofus**, for 24 hours.",
                )
            ),
        ),
        (
            "Result card — nickname, self-apply (winner is the server owner)",
            _result(
                build_boom_layout(
                    100, 2, None, "The stake is forfeited.",
                    self_apply_copy=(
                        "Discord won't let me rename the server owner, so the "
                        "nickname has to be set by hand: **Doofus**, for 24 hours."
                    ),
                ),
                self_apply=True,
            ),
        ),
        (
            "Result card — awaiting nickname",
            _result(
                build_boom_layout(
                    100, 1, None, "The stake is forfeited.",
                    awaiting_copy=(
                        "Press **Name the Loser** within 30 minutes. The nickname "
                        "lasts 24 hours."
                    ),
                )
            ),
        ),
        (
            "Result card — fullest (long stakes + self-apply copy, budget check)",
            _result(
                build_boom_layout(
                    100, 40,
                    "loser buys the next round for the whole voice channel",
                    "The stake is forfeited.",
                    self_apply_copy=(
                        "Discord won't let me rename the server owner, so the "
                        "nickname has to be set by hand: **A Very Long Nickname "
                        "Indeed**, for 48 hours."
                    ),
                ),
                self_apply=True,
            ),
        ),
    ]


def wallet_samples(guild: discord.Guild, avatar_url: str):
    """Every shape ``bot_modules/economy/wallet.py`` can produce for
    ``/bank wallet`` (and the economy panel's Wallet button): a new member's
    empty wallet, and the fullest one — activity, a gifted and a lapsing
    rental, a casino record on a streak, a shield and the currency icon.
    """
    del guild, avatar_url  # the card pictures a coin, not a member
    import dataclasses
    import json
    import sqlite3

    from bot_modules.economy.wallet import build_wallet_layout
    from bot_modules.economy.service import DEFAULT_ECON_SETTINGS

    name_fn = _placeholder_names()
    now = int(time.time())
    iconed = dataclasses.replace(
        DEFAULT_ECON_SETTINGS,
        currency_icon_url="https://cdn.discordapp.com/embed/avatars/2.png",
    )
    ledger_rows = [
        {"amount": 50, "kind": "quest", "created_at": now - 900, "meta": "{}"},
        {
            "amount": -75, "kind": "rental", "created_at": now - 1800,
            "meta": json.dumps({"perk": "role_color"}),
        },
        {
            "amount": 200, "kind": "transfer_in", "created_at": now - 3600,
            "meta": json.dumps({"from": OTHER_IDS[1], "memo": "birthday gift"}),
        },
    ]
    rentals = [
        {
            "perk": "role_color", "price": 75, "next_bill_at": now + 86400 * 3,
            "user_id": TARGET_ID, "beneficiary_id": TARGET_ID, "state": "active",
        },
        {
            "perk": "role_name", "price": 40, "next_bill_at": now + 86400,
            "user_id": TARGET_ID, "beneficiary_id": OTHER_IDS[0], "state": "grace",
        },
    ]
    # build_wallet_layout reads the casino record by column name, so it wants
    # a real sqlite3.Row; a throwaway in-memory query hands it one.
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    casino = conn.execute(
        "SELECT 42 AS plays, 1200 AS wagered, 950 AS returned, 4 AS streak, "
        "300 AS biggest_win, 'Slots' AS biggest_win_game"
    ).fetchone()
    common = {"viewer_id": TARGET_ID, "name_fn": name_fn}
    return [
        (
            "Wallet — empty",
            build_wallet_layout(
                DEFAULT_ECON_SETTINGS, balance=0, ledger=[], rentals=[],
                shields=0, casino=None, **common,
            ),
        ),
        (
            "Wallet — activity, rentals, casino, shield, icon",
            build_wallet_layout(
                iconed, balance=530, ledger=ledger_rows, rentals=rentals,
                shields=1, casino=casino, **common,
            ),
        ),
    ]


def risky_samples(guild: discord.Guild, avatar_url: str):
    """Every card ``bot_modules/games/risky_roll/layouts.py`` can
    produce: the round board open (empty, then mid-round) and closed under
    each result rule, the How to Play card, and the winner prompt in each
    kind, then spent.

    The prompt pings on purpose, so it prints ``<@id>`` rather than a name;
    these ids have no member behind them and render as unknown users here.
    """
    del guild, avatar_url  # no member is pictured
    from bot_modules.games.risky_roll.formatters import (
        build_pending_question_summary,
    )
    from bot_modules.games.risky_roll.layouts import (
        build_host_panel_layout,
        build_how_to_play_layout,
        build_prompt_layout,
        build_round_layout,
    )
    from bot_modules.games.risky_roll.logic import PayoffDials
    from bot_modules.games.risky_roll.models import (
        PendingQuestionState,
        PromptKind,
        RiskyRollState,
    )

    name_fn = _placeholder_names()
    a, b, (c, d, e) = REQUESTER_ID, TARGET_ID, OTHER_IDS
    # The live guild's lobby header with "Risky Rolls" drawn in, and its
    # footer — the art a round in the age-gated channel wears.
    banners = f"{dashboard_base()}/static/panel-banners"
    art = {
        "header_url": f"{banners}/titled/3c021dc7990d401b8ebd4293fe7bacbd-6314e649cd01.webp",
        "footer_url": f"{banners}/99db7050d4b54a229481cbcbe7700b4f.webp",
        "title_in_header": True,
    }

    def _round(rolls: dict[int, int], *, is_open: bool = False, **fields):
        state = RiskyRollState(
            channel_id=1, guild_id=1, opener_id=a,
            auto_close_players=8, auto_close_minutes=10,
        )
        state.rolls = dict(rolls)
        state.is_open = is_open
        for key, value in fields.items():
            setattr(state, key, value)
        return build_round_layout(state, name_fn, **art)

    def _prompt(kind: PromptKind, *, asked=None, **fields):
        state = PendingQuestionState(
            channel_id=1, guild_id=1, winner_id=a, game_id="sample",
            participant_user_ids={b}, prompt_kind=kind,
        )
        for key, value in fields.items():
            setattr(state, key, value)
        closed = None
        if asked is not None:
            question, asker_id, from_bank = asked
            closed = build_pending_question_summary(
                state, question, asker_id, from_bank=from_bank
            )
        return build_prompt_layout(state, closed_text=closed)

    five = {a: 87, b: 12, c: 55, d: 40, e: 73}
    return [
        ("Board — open, nobody yet", _round({}, is_open=True)),
        ("Board — open, five rolls", _round(five, is_open=True)),
        ("Board — closed, winner and loser", _round(five, highest_user=a, lowest_user=b)),
        (
            "Board — closed, 100 rule (two answer)",
            _round(
                {**five, a: 100}, highest_user=a, lowest_user=b,
                second_lowest_user=d,
            ),
        ),
        (
            "Board — closed, 1 rule (two ask) and a roll-off",
            _round(
                {**five, b: 1, c: 73}, highest_user=e, lowest_user=b,
                second_highest_user=c, highest_tie_user_ids={c, e},
            ),
        ),
        (
            "Board — closed, 69 asks the room",
            _round({**five, c: 69}, highest_user=c),
        ),
        (
            "Host controls — ephemeral, from /games host",
            build_host_panel_layout(
                [RiskyRollState(channel_id=1, guild_id=1, opener_id=a, message_id=2)],
                name_fn,
            ),
        ),
        (
            "How to Play — reminders and the deck on",
            build_how_to_play_layout(PayoffDials(chase_hours=12, fallback_hours=24)),
        ),
        ("Prompt — winner asks the loser", _prompt(PromptKind.DIRECT)),
        (
            "Prompt — a 1 was rolled, two askers",
            _prompt(PromptKind.TWO_QUESTIONERS, extra_questioner_id=e),
        ),
        ("Prompt — 69, asks the room", _prompt(PromptKind.ROOM, participant_user_ids=set())),
        (
            "Prompt — asked (button gone)",
            _prompt(PromptKind.DIRECT, asked=("What's the pettiest thing you've ever done?", a, False)),
        ),
        (
            "Prompt — the deck asked for them",
            _prompt(PromptKind.DIRECT, asked=("Worst date you've been on?", None, True)),
        ),
    ]


def economy_samples(guild: discord.Guild, avatar_url: str):
    """Every card the bank/register/leaderboard/guide half of the economy cog
    can produce — ``economy/{layouts,register,transfers,guide,
    bank_panel_layout}.py``. The wallet (``economy/wallet.py``) has its own
    ``wallet`` entry above, and the Perk Shop (``economy/shop.py``) is its
    own conversion wave — neither has cards here."""
    from bot_modules.economy.bank_panel_layout import build_economy_panel_layout
    from bot_modules.economy.guide import build_guide_layout
    from bot_modules.economy.layouts import (
        build_grant_receipt_layout,
        build_mute_toggle_layout,
        build_pay_confirm_layout,
    )
    from bot_modules.economy.leaderboard import (
        CommunityGoal,
        FeedLine,
        LeaderboardData,
        QuestLine,
    )
    from bot_modules.economy.register import (
        RegisterEntry,
        build_register_entry_layout,
        build_signoff_notice_layout,
    )
    from bot_modules.economy.transfers import build_payment_receipt_layout
    from bot_modules.economy.service import DEFAULT_ECON_SETTINGS

    name_fn = _placeholder_names()
    now = time.time()
    iconed = dataclasses.replace(
        DEFAULT_ECON_SETTINGS,
        currency_icon_url="https://example.invalid/coin.png",
    )

    grant_entry = RegisterEntry(
        ledger_id=1,
        user_id=TARGET_ID,
        amount=30,
        kind="grant",
        actor_id=REQUESTER_ID,
        meta={"reason": "helping out in mod support"},
        created_at=now - 300,
        balance_after=530,
    )
    quest_entry = RegisterEntry(
        ledger_id=2,
        user_id=TARGET_ID,
        amount=50,
        kind="quest",
        actor_id=None,
        meta={},
        created_at=now - 900,
        balance_after=580,
        quest_title="Send a message in #general",
        quest_target=1,
    )
    rental_entry = RegisterEntry(
        ledger_id=3,
        user_id=OTHER_IDS[0],
        amount=-75,
        kind="rental",
        actor_id=None,
        meta={"perk": "role_color"},
        created_at=now - 1800,
        balance_after=125,
    )
    transfer_entry = RegisterEntry(
        ledger_id=4,
        user_id=REQUESTER_ID,
        amount=200,
        kind="transfer_out",
        actor_id=None,
        meta={"to": TARGET_ID, "memo": "thanks for the art!"},
        created_at=now - 60,
        balance_after=300,
    )

    leaderboard_data = LeaderboardData(
        top_earners=[
            (TARGET_ID, 1250),
            (OTHER_IDS[0], 980),
            (OTHER_IDS[1], 640),
            (OTHER_IDS[2], 300),
        ],
        community=[
            CommunityGoal(
                title="Fill the Event Calendar",
                current=340,
                target=500,
                completed=False,
                settled=False,
                auto=True,
                tiers=2,
                contributors=18,
                today_delta=40,
                on_track=True,
                ends_ts=now + 86400 * 4,
                kind_flavor="calendar",
                channel_id=None,
            ),
            CommunityGoal(
                title="Server Anniversary Fund",
                current=1000,
                target=1000,
                completed=True,
                settled=True,
                auto=False,
                tiers=1,
                contributors=32,
                today_delta=None,
                on_track=True,
                ends_ts=None,
                kind_flavor="",
                channel_id=None,
            ),
        ],
        quests=[
            QuestLine(
                qtype="trigger_word",
                title="Say hello in #welcome",
                reward=20,
                reward_xp=5,
                spotlight=True,
            ),
            QuestLine(
                qtype="voice_time",
                title="Spend 30 minutes in voice",
                reward=40,
                reward_xp=10,
            ),
        ],
        spotlight_kind="trigger_word",
        spotlight_label="Say hello in #welcome",
        today_by_user={TARGET_ID: 120, OTHER_IDS[0]: 90},
        feed=(
            FeedLine(title="Quest completions", count=14, last_ts=now - 120),
            FeedLine(title="Perk rentals", count=3, last_ts=now - 1800),
        ),
        set_bonuses_today=2,
        day_roll_ts=now - 3600 * 5,
        week_roll_ts=now - 86400 * 2,
        raffle_on=True,
        raffle_tickets=27,
        raffle_entrants=11,
        last_winner_id=OTHER_IDS[1],
        last_winner_week="2026-W37",
        casino_biggest=(TARGET_ID, 300),
        casino_luckiest=(OTHER_IDS[0], 4),
    )
    leaderboard_data_quiet = LeaderboardData(
        top_earners=[],
        community=[],
        quests=[],
    )

    return [
        # ── economy/layouts.py — grant, mute, pay confirm, QOTD fallback ──
        (
            "Grant receipt",
            build_grant_receipt_layout(
                settings=DEFAULT_ECON_SETTINGS,
                credited=45,
                amount=30,
                booster=True,
                member_name=name_fn(TARGET_ID),
                reason="helping out in mod support",
                actor_name=name_fn(REQUESTER_ID),
            ),
        ),
        (
            "Grant receipt — icon, no reason",
            build_grant_receipt_layout(
                settings=iconed,
                credited=30,
                amount=30,
                booster=False,
                member_name=name_fn(TARGET_ID),
                reason="",
                actor_name=name_fn(REQUESTER_ID),
            ),
        ),
        ("Mute toggle — muted", build_mute_toggle_layout(muted=True)),
        ("Mute toggle — unmuted", build_mute_toggle_layout(muted=False)),
        (
            "Pay confirm — private, with memo",
            build_pay_confirm_layout(
                settings=DEFAULT_ECON_SETTINGS,
                amount=750,
                recipient_name=name_fn(TARGET_ID),
                memo="rent money",
                public=False,
            ),
        ),
        (
            "Pay confirm — will post publicly",
            build_pay_confirm_layout(
                settings=DEFAULT_ECON_SETTINGS,
                amount=750,
                recipient_name=name_fn(TARGET_ID),
                memo=None,
                public=True,
            ),
        ),
        # ── economy/register.py — the public transaction feed ────────────
        (
            "Register — staff grant",
            build_register_entry_layout(
                grant_entry, DEFAULT_ECON_SETTINGS, name_fn, avatar_url=avatar_url
            ),
        ),
        (
            "Register — quest payout",
            build_register_entry_layout(quest_entry, DEFAULT_ECON_SETTINGS, name_fn),
        ),
        (
            "Register — perk rental",
            build_register_entry_layout(rental_entry, DEFAULT_ECON_SETTINGS, name_fn),
        ),
        (
            "Register — transfer, consolidated",
            build_register_entry_layout(transfer_entry, DEFAULT_ECON_SETTINGS, name_fn),
        ),
        (
            "Register — quest sign-off denied",
            build_signoff_notice_layout(
                "denied",
                member_name=name_fn(TARGET_ID),
                quest_title="Send a Whisper",
                deny_reason="already claimed today",
            ),
        ),
        (
            "Register — quest claim expired",
            build_signoff_notice_layout(
                "expired",
                member_name=name_fn(TARGET_ID),
                quest_title="Voice Time",
            ),
        ),
        # ── economy/transfers.py — /bank pay receipts ─────────────────────
        (
            "Payment receipt — private, with memo and balance",
            build_payment_receipt_layout(
                DEFAULT_ECON_SETTINGS,
                name_fn(REQUESTER_ID),
                name_fn(TARGET_ID),
                200,
                memo="rent money",
                balance=300,
                public=False,
            ),
        ),
        (
            "Payment receipt — public (no balance, even if passed)",
            build_payment_receipt_layout(
                DEFAULT_ECON_SETTINGS,
                name_fn(REQUESTER_ID),
                name_fn(TARGET_ID),
                750,
                memo="rent money",
                balance=300,
                public=True,
            ),
        ),
        # ── economy/bank_panel_layout.py — the live economy panel ─────────
        (
            "Economy panel — live board",
            build_economy_panel_layout(
                DEFAULT_ECON_SETTINGS, leaderboard_data, name_fn, now_ts=now
            ),
        ),
        (
            "Economy panel — quiet guild, nothing running",
            build_economy_panel_layout(
                iconed, leaderboard_data_quiet, name_fn, now_ts=now
            ),
        ),
        # ── economy/guide.py — How It Works ───────────────────────────────
        ("Guide — defaults", build_guide_layout(DEFAULT_ECON_SETTINGS)),
        (
            "Guide — custom currency, icon, XP conversion on",
            build_guide_layout(
                dataclasses.replace(
                    iconed,
                    currency_plural="Gems",
                    currency_emoji="💎",
                    xp_per_coin=50,
                    conversion_daily_cap=100,
                )
            ),
        ),
    ]


def _sample_signoff_buttons(claim_id: int) -> "discord.ui.ActionRow[Any]":
    """A visual stand-in for ``quest_views._signoff_buttons_row``.

    The real row is built from ``DynamicItem`` buttons, whose ``custom_id``
    is validated against a fixed regex template — ``sample_prefix_ids`` can't
    rewrite it to the inert ``sample:`` form the way it does every other
    button, so posting the real row here would leave a live Approve/Deny
    pair reachable from a throwaway sample message. These two are plain
    buttons with the same label, emoji and style, already carrying the
    inert id, so they render identically and press to nothing.
    """
    row: discord.ui.ActionRow[Any] = discord.ui.ActionRow()
    row.add_item(
        discord.ui.Button(
            label="Approve", emoji="✅", style=discord.ButtonStyle.success,
            custom_id=f"sample:econ_claim:approve:{claim_id}",
        )
    )
    row.add_item(
        discord.ui.Button(
            label="Deny", emoji="✖️", style=discord.ButtonStyle.danger,
            custom_id=f"sample:econ_claim:deny:{claim_id}",
        )
    )
    return row


def quests_samples(guild: discord.Guild, avatar_url: str):
    """Every card ``bot_modules/economy/quest_layouts.py`` (+
    ``approval_layouts.py`` via it) and the sign-off buttons row can produce."""
    from bot_modules.economy.quest_layouts import (
        build_quest_board_layout,
        build_quest_claimed_layout,
        build_quest_detail_layout,
        build_signoff_card_layout,
    )
    from bot_modules.economy.service import EconSettings

    name_fn = _placeholder_names()
    settings = EconSettings(currency_emoji="💎")
    iconed_settings = EconSettings(currency_emoji="💎", currency_icon_url=avatar_url)
    board_quests = [
        {
            "id": 1, "title": "Say hi in general", "qtype": "daily", "reward": 10,
            "state": "claimable",
        },
        {
            "id": 2, "title": "Chat it up", "qtype": "daily", "reward": 15,
            "state": "active", "progress_current": 3, "progress_target": 6,
        },
        {
            "id": 3, "title": "Weekly grind", "qtype": "weekly", "reward": 40,
            "state": "done",
        },
        {
            "id": 4, "title": "Get a sign-off", "qtype": "weekly", "reward": 50,
            "state": "pending",
        },
        {
            "id": 5, "title": "Team goal", "qtype": "community", "reward": 20,
            "state": "community", "current": 40, "target": 100,
        },
        {
            "id": 6, "title": "Monthly Marathon", "qtype": "monthly", "reward": 60,
            "state": "community", "current": 200, "target": 500,
        },
    ]
    spotlight_quests = [
        {
            "id": 7, "title": "Spotlight quest", "qtype": "daily", "reward": 30,
            "state": "claimable", "spotlight": True,
        },
    ]
    detail_quest = {
        "id": 2, "title": "Chat it up", "qtype": "daily", "reward": 15,
        "reward_xp": 5, "description": "Post a few messages in any channel today.",
        "state": "active", "progress_current": 3, "progress_target": 6,
    }
    community_quest = {
        "id": 5, "title": "Team goal", "qtype": "community", "reward": 20,
        "description": "Everyone pitches in — 100 messages this week.",
        "state": "community", "current": 40, "target": 100,
    }
    done_quest = {
        "id": 3, "title": "Weekly grind", "qtype": "weekly", "reward": 40,
        "description": "Show up five days this week.", "state": "done",
    }
    return [
        ("Quest board — nothing active", build_quest_board_layout(settings, [])),
        ("Quest board — a full mix", build_quest_board_layout(settings, board_quests)),
        (
            "Quest board — spotlight active",
            build_quest_board_layout(settings, spotlight_quests),
        ),
        (
            "Sign-off card — pending",
            build_signoff_card_layout(
                settings, claimant_id=TARGET_ID, name_fn=name_fn,
                quest_title="Get a sign-off", reward=50,
                criteria="Post a screenshot of the finished thing.", deny_count=0,
                state="pending", buttons=_sample_signoff_buttons(4),
            ),
        ),
        (
            "Sign-off card — pending, prior denial",
            build_signoff_card_layout(
                settings, claimant_id=TARGET_ID, name_fn=name_fn,
                quest_title="Get a sign-off", reward=50,
                criteria="Post a screenshot of the finished thing.", deny_count=1,
                state="pending", buttons=_sample_signoff_buttons(4),
            ),
        ),
        (
            "Sign-off card — approved",
            build_signoff_card_layout(
                settings, claimant_id=TARGET_ID, name_fn=name_fn,
                quest_title="Get a sign-off", reward=50,
                criteria="Post a screenshot of the finished thing.", deny_count=0,
                state="paid", resolver_id=REQUESTER_ID,
            ),
        ),
        (
            "Sign-off card — denied, with reason",
            build_signoff_card_layout(
                settings, claimant_id=TARGET_ID, name_fn=name_fn,
                quest_title="Get a sign-off", reward=50,
                criteria="Post a screenshot of the finished thing.", deny_count=1,
                state="denied", resolver_id=REQUESTER_ID,
                deny_reason="Screenshot didn't show the finished result.",
            ),
        ),
        (
            "Quest detail — in progress, channel-scoped",
            build_quest_detail_layout(
                settings, detail_quest, scope_line="📍 Only counts in #general"
            ),
        ),
        (
            "Quest detail — community goal",
            build_quest_detail_layout(settings, community_quest),
        ),
        ("Quest detail — done this period", build_quest_detail_layout(settings, done_quest)),
        (
            "Quest complete — no currency icon",
            build_quest_claimed_layout(settings, title="Say hi in general", paid=10),
        ),
        (
            "Quest complete — with currency icon",
            build_quest_claimed_layout(iconed_settings, title="Say hi in general", paid=10),
        ),
    ]


def drops_samples(guild: discord.Guild, avatar_url: str):
    """Every card ``bot_modules/economy/drops_layouts.py`` can
    produce. The Claim button is a plain stand-in with the same custom_id
    shape the real ``DropClaimButton`` uses — ``sample_prefix_ids`` kills it
    either way, so the sample needs none of that class's persistence."""
    from bot_modules.economy.drops_layouts import (
        build_claimed_layout,
        build_drop_layout,
        build_expired_layout,
    )
    from bot_modules.economy.service import DEFAULT_ECON_SETTINGS

    name_fn = _placeholder_names()
    now = int(time.time())

    def claim_button() -> discord.ui.Button:
        return discord.ui.Button(
            label="Claim",
            emoji="🪙",
            style=discord.ButtonStyle.success,
            custom_id="econ_drop:claim:1",
        )

    return [
        (
            "Pouch landed",
            build_drop_layout(
                settings=DEFAULT_ECON_SETTINGS,
                amount=250,
                expires_at=now + 600,
                claim_button=claim_button(),
            ),
        ),
        (
            "Claimed",
            build_claimed_layout(
                settings=DEFAULT_ECON_SETTINGS,
                credited=250,
                claimant_id=TARGET_ID,
                name_fn=name_fn,
            ),
        ),
        ("Vanished — nobody claimed it", build_expired_layout()),
    ]


def auctions_samples(guild: discord.Guild, avatar_url: str):
    """Every state ``bot_modules/economy/auction_layouts.py`` can produce."""
    from bot_modules.economy.auction_layouts import build_auction_card_layout
    from bot_modules.economy.auction_views import AuctionBidButton
    from bot_modules.economy.service import EconSettings

    name_fn = _placeholder_names()
    now = int(time.time())
    settings = EconSettings(
        enabled=True, currency_emoji="🪙", currency_name="Coin", currency_plural="Coins",
        auction_min_bid=10, auction_min_increment=5, auction_soft_close_seconds=300,
        auction_max_duration_hours=168,
    )

    def _row(**over):
        row = {
            "id": 1,
            "title": "Founder role",
            "description": "One-of-a-kind holographic role for a fortnight.",
            "created_by": REQUESTER_ID,
            "state": "open",
            "high_bid": None,
            "high_bidder_id": None,
            "winner_id": None,
            "winning_bid": None,
            "ends_at": now + 3600,
        }
        row.update(over)
        return row

    open_no_bid = _row()
    open_high_bid = _row(high_bid=40, high_bidder_id=TARGET_ID)
    closed_sold = _row(
        state="closed", high_bid=40, high_bidder_id=TARGET_ID,
        winner_id=TARGET_ID, winning_bid=40,
    )
    closed_no_bids = _row(state="closed")
    cancelled = _row(state="cancelled", high_bid=40, high_bidder_id=TARGET_ID)

    def _build(row, *, bids, open_state):
        bid_button = AuctionBidButton(int(row["id"])) if open_state else None
        return build_auction_card_layout(
            settings=settings, auction=row, bids=bids, name_fn=name_fn,
            bid_button=bid_button,
        )

    return [
        (
            "Open — no bids yet",
            _build(open_no_bid, bids=0, open_state=True),
        ),
        (
            "Open — with a high bid",
            _build(open_high_bid, bids=3, open_state=True),
        ),
        ("Closed — sold", _build(closed_sold, bids=3, open_state=False)),
        ("Closed — no bids", _build(closed_no_bids, bids=0, open_state=False)),
        ("Cancelled", _build(cancelled, bids=3, open_state=False)),
    ]


def _sample_pin_review_buttons(submission_id: int) -> discord.ui.ActionRow[Any]:
    """A visual stand-in for ``pin_views._review_buttons_row``.

    The real Approve/Decline pair is built from ``DynamicItem`` buttons whose ``custom_id`` is
    validated against a fixed regex template, so ``sample_prefix_ids`` can't
    rewrite it the way it does every other button. These two carry the same
    label, emoji and style with the inert id baked in from the start.
    """
    row: discord.ui.ActionRow[Any] = discord.ui.ActionRow()
    row.add_item(
        discord.ui.Button(
            label="Approve", emoji="✅", style=discord.ButtonStyle.success,
            custom_id=f"sample:econ_pin_sub:approve:{submission_id}",
        )
    )
    row.add_item(
        discord.ui.Button(
            label="Decline", emoji="🚫", style=discord.ButtonStyle.danger,
            custom_id=f"sample:econ_pin_sub:deny:{submission_id}",
        )
    )
    return row


def pins_samples(guild: discord.Guild, avatar_url: str):
    """Every card ``bot_modules/economy/pin_layouts.py`` can produce: the
    mod-review card across pin's five states (collapsed onto three card
    shapes, but four headings — a lapsed-after-going-live pin reads "Ended",
    not "Declined"), and the public "Pinned by a Member" card."""
    from bot_modules.economy.pin_layouts import (
        build_pin_live_layout,
        build_pin_review_layout,
    )
    from bot_modules.economy.service import EconSettings

    name_fn = _placeholder_names()
    settings = EconSettings(currency_emoji="💎")
    now = int(time.time())
    expires_at = now + 86400
    message = "Movie night starts at 8 — bring snacks!"
    return [
        (
            "Review card — pending",
            build_pin_review_layout(
                settings, sponsor_id=REQUESTER_ID, name_fn=name_fn,
                message=message, price=200, state="pending",
                buttons=_sample_pin_review_buttons(9),
            ),
        ),
        (
            "Review card — approved",
            build_pin_review_layout(
                settings, sponsor_id=REQUESTER_ID, name_fn=name_fn,
                message=message, price=200, state="live",
                resolver_id=TARGET_ID, expires_at=expires_at,
            ),
        ),
        (
            "Review card — declined, with reason",
            build_pin_review_layout(
                settings, sponsor_id=REQUESTER_ID, name_fn=name_fn,
                message=message, price=200, state="denied",
                resolver_id=TARGET_ID, deny_reason="Off-topic for this server.",
                refunded=True,
            ),
        ),
        (
            "Review card — expired, never reviewed",
            build_pin_review_layout(
                settings, sponsor_id=REQUESTER_ID, name_fn=name_fn,
                message=message, price=200, state="expired",
                refunded=True,
            ),
        ),
        (
            "Review card — expired, ran its day",
            build_pin_review_layout(
                settings, sponsor_id=REQUESTER_ID, name_fn=name_fn,
                message=message, price=200, state="expired",
                # resolver_id still holds the mod who originally APPROVED
                # it — economy_loop.expire_live_pins never touches the
                # column — so this proves the card doesn't credit them with
                # a decline they didn't make, and doesn't show a refund
                # that didn't happen.
                resolver_id=TARGET_ID, refunded=False,
            ),
        ),
        (
            "Review card — superseded by a newer pin",
            build_pin_review_layout(
                settings, sponsor_id=REQUESTER_ID, name_fn=name_fn,
                message=message, price=200, state="superseded",
                resolver_id=TARGET_ID, refunded=False,
            ),
        ),
        (
            "Pinned by a Member",
            build_pin_live_layout(
                sponsor_id=REQUESTER_ID, name_fn=name_fn,
                message=message, expires_at=expires_at,
            ),
        ),
        (
            "Pinned by a Member — sponsor since erased",
            build_pin_live_layout(
                sponsor_id=0, name_fn=name_fn,
                message=message, expires_at=expires_at,
            ),
        ),
    ]


def _sample_theme_buttons(submission_id: int) -> discord.ui.ActionRow[Any]:
    """A visual stand-in for ``theme_views._review_buttons_row``.

    Same reason as ``_sample_pin_review_buttons``: the real pair is
    ``DynamicItem`` buttons whose ``custom_id`` is validated against a fixed
    regex template, so ``sample_prefix_ids`` can't rewrite it — these two are
    plain buttons with the same label, emoji and style, already carrying the
    inert id.
    """
    row: discord.ui.ActionRow[Any] = discord.ui.ActionRow()
    row.add_item(
        discord.ui.Button(
            label="Approve", emoji="✅", style=discord.ButtonStyle.success,
            custom_id=f"sample:econ_theme_sub:approve:{submission_id}",
        )
    )
    row.add_item(
        discord.ui.Button(
            label="Decline", emoji="🚫", style=discord.ButtonStyle.danger,
            custom_id=f"sample:econ_theme_sub:deny:{submission_id}",
        )
    )
    return row


def themes_samples(guild: discord.Guild, avatar_url: str):
    """Every card ``bot_modules/economy/theme_layouts.py`` can produce: the
    mod-review card across its states, and the public live announcement with
    and without the guild's hub banner/footer art (Ben, 2026-09-28, "option
    B") — the announcement's one documented exception to "channel-resident
    hub panels only" (``tests/contracts/test_layout_hub_banner.py``'s
    ``HUB_BANNER_EXCEPTIONS``)."""
    del guild, avatar_url
    from bot_modules.economy.theme_layouts import (
        build_theme_live_layout,
        build_theme_review_layout,
    )
    from bot_modules.economy.service import EconSettings

    name_fn = _placeholder_names()
    settings = EconSettings(currency_emoji="💎", theme_hours=24)
    title = "Cursed Cooking"
    blurb = "Post the worst thing you have ever eaten."
    price = 300
    # A theme that went live 5 hours ago in a 24-hour window — the review
    # card's countdown reflects the row's own clock, not the configured
    # duration, which is what makes the sample worth looking at.
    expires_at = time.time() + 19 * 3600
    # The live guild's hub banner with "Today's Theme" drawn in, and its
    # footer — the art a themed-day announcement wears once a guild has
    # uploaded hub art (``branding_service.resolve_panel_art_conn``).
    banners = f"{dashboard_base()}/static/panel-banners"
    banner_url = f"{banners}/titled/3c021dc7990d401b8ebd4293fe7bacbd-6314e649cd01.webp"
    footer_url = f"{banners}/99db7050d4b54a229481cbcbe7700b4f.webp"

    def _review(state: str, **over):
        kwargs: dict = dict(
            sponsor_id=REQUESTER_ID, name_fn=name_fn, title=title,
            blurb=blurb, price=price, state=state,
        )
        kwargs.update(over)
        return build_theme_review_layout(settings, **kwargs)

    return [
        (
            "Review card — pending",
            _review("pending", buttons=_sample_theme_buttons(9)),
        ),
        (
            "Review card — approved, queued",
            _review("approved", resolver_id=TARGET_ID),
        ),
        (
            "Review card — live",
            _review("live", resolver_id=TARGET_ID, expires_at=expires_at),
        ),
        (
            "Review card — ended early, no refund",
            _review("expired", resolver_id=TARGET_ID, refunded=False),
        ),
        (
            "Review card — expired unreviewed, refunded",
            _review("expired", refunded=True),
        ),
        (
            "Review card — declined, with reason and refund",
            _review(
                "denied", resolver_id=TARGET_ID,
                deny_reason="Too close to last week's theme.", refunded=True,
            ),
        ),
        (
            "Live announcement — no hub art configured (ships dark)",
            build_theme_live_layout(
                settings, sponsor_id=REQUESTER_ID, name_fn=name_fn,
                title=title, blurb=blurb, expires_at=expires_at,
            ),
        ),
        (
            "Live announcement — anonymous sponsor, no blurb",
            build_theme_live_layout(
                settings, sponsor_id=0, name_fn=name_fn, title=title, blurb="",
                expires_at=expires_at,
            ),
        ),
        (
            "Live announcement — hub banner + footer configured",
            build_theme_live_layout(
                settings, sponsor_id=REQUESTER_ID, name_fn=name_fn,
                title=title, blurb=blurb, expires_at=expires_at,
                banner_url=banner_url, footer_url=footer_url,
            ),
        ),
    ]


def bounties_samples(guild: discord.Guild, avatar_url: str):
    """Every state ``bot_modules/economy/bounty_layouts.py`` can produce: the
    board card across its four states, and the hub panel empty, with open
    bounties, with its list capped, and wearing the guild's lobby strip
    (Ben, 2026-09-28: a channel hub with no tall banner of its own wears the
    small header/footer strip a game lobby wears, title drawn in place of
    the ``## `` heading; ships dark until an admin uploads one)."""
    del guild, avatar_url
    from bot_modules.economy.bounty_layouts import (
        build_bounty_card_layout,
        build_bounty_hub_layout,
    )
    from bot_modules.economy.bounty_service import BountyBoardEntry
    from bot_modules.economy.service import EconSettings

    name_fn = _placeholder_names()
    now = int(time.time())
    settings = EconSettings(
        currency_emoji="🪙", currency_plural="coins",
        bounty_rake_pct=10, bounty_expire_days=14,
    )

    def _row(state="open", **over):
        row = {
            "id": 1,
            "state": state,
            "title": "Draw the mascot",
            "description": "Any medium, any style — just make it fun.",
            "poster_id": REQUESTER_ID,
            "winner_id": 0,
            "payout": 0,
            "rake_amount": 0,
            "resolved_at": now,
        }
        row.update(over)
        return row

    def _card(state, **over):
        return build_bounty_card_layout(
            settings, _row(state, **over), pot=1000, contributors=4, name_fn=name_fn,
        )

    def _entry(**over):
        base = dict(
            bounty_id=1, title="Draw the mascot", pot=340, contributors=4,
            card_channel_id=1, card_message_id=1,
        )
        base.update(over)
        return BountyBoardEntry(**base)

    def _hub(entries, *, open_total=None, **kw):
        return build_bounty_hub_layout(
            settings, 700, entries,
            open_total=open_total if open_total is not None else len(entries), **kw,
        )

    # The live guild's lobby header art with the board's name drawn in, and
    # its footer — the same strip a titled game lobby wears
    # (``branding_service.resolve_panel_art_conn``'s ``header``/``footer``).
    banners = f"{dashboard_base()}/static/panel-banners"
    header_url = f"{banners}/titled/3c021dc7990d401b8ebd4293fe7bacbd-6314e649cd01.webp"
    footer_url = f"{banners}/99db7050d4b54a229481cbcbe7700b4f.webp"

    return [
        ("Board card — open", _card("open")),
        (
            "Board card — awarded",
            _card("awarded", winner_id=TARGET_ID, payout=900, rake_amount=100),
        ),
        ("Board card — cancelled", _card("cancelled")),
        ("Board card — expired", _card("expired")),
        ("Hub panel — empty board", _hub([])),
        (
            "Hub panel — open bounties",
            _hub([
                _entry(),
                _entry(
                    bounty_id=2, title="Compose a victory jingle",
                    pot=120, contributors=1,
                ),
            ]),
        ),
        (
            "Hub panel — list capped",
            _hub(
                [
                    _entry(bounty_id=i, title=f"Bounty #{i}", pot=100 * i, contributors=i)
                    for i in range(1, 9)
                ],
                open_total=15,
            ),
        ),
        (
            "Hub panel — lobby strip + footer configured, title drawn in",
            _hub(
                [_entry()], header_url=header_url, footer_url=footer_url,
                title_in_header=True,
            ),
        ),
    ]


def todo_samples(guild: discord.Guild, avatar_url: str):
    """Every card ``bot_modules/todo/layouts.py`` can produce: the todo board
    (empty, tasks only, every section, the all-markdown board that gets
    trimmed), the bug board (empty, populated, overflowing), both Complete
    pickers with both outcomes each, and the three change cards — arrivals,
    the noon chore digest, and a new-bug card with and without screenshots.

    Both boards are bare on purpose (no banner, strip or footer art), and no
    bug card or bug row names who filed it."""
    from bot_modules.todo.board_logic import (
        board_sections,
        render_board_footer,
        render_footer,
        render_rows,
    )
    from bot_modules.todo.change_logic import (
        image_filename,
        plan_bug_cards,
        plan_cards,
    )
    from bot_modules.todo.layouts import (
        build_bug_board_layout,
        build_change_card_layout,
        build_complete_picker_layout,
        build_completed_layout,
        build_todo_board_layout,
    )

    del guild
    name_fn = _placeholder_names()
    a, b, c = REQUESTER_ID, TARGET_ID, OTHER_IDS[0]
    now = time.time()

    tasks = [
        {"id": 212, "task": "Archive the summer event channels", "added_by": a},
        {"id": 215, "task": "Write up the new voice-room rules for the FAQ", "added_by": a},
        {"id": 219, "task": "Custom role icon", "purchase_id": 41, "buyer_id": b,
         "buyer_name": name_fn(b), "added_by": b},
        {"id": 223, "task": "Ask about a second movie night slot", "added_by": c},
    ]
    chores = [
        {"recurring_id": 1, "todo_id": 301, "task": "Post the question of the day",
         "completed_at": now - 3 * 3600, "completed_by_name": name_fn(a), "streak": 12},
        {"recurring_id": 2, "todo_id": 302, "task": "Bump the server"},
        {"recurring_id": 3, "todo_id": 303, "task": "Clear the report queue",
         "missed_previous": 1},
    ]
    signoffs = [
        {"id": 14, "user_id": b, "claimant_name": name_fn(b),
         "quest_title": "Post a photo of your desk", "reward": 500},
    ]
    approvals = [
        {"id": 3, "kind": "theme", "user_id": c, "requester_name": name_fn(c),
         "summary": "Cursed Cooking", "price": 300},
        {"id": 8, "kind": "pin", "user_id": a, "requester_name": name_fn(a),
         "summary": "the screenshot of the raid wipe", "price": 150},
    ]
    confessions = [
        {"id": 5, "content": "I still haven't read the rules channel.",
         "created_at": now - 26 * 3600},
    ]

    def todo_board(chore_rows, task_rows, *, total=None, **queues):
        totals = {
            "signoff_total": len(queues.get("signoff_rows", ())),
            "approval_total": len(queues.get("approval_rows", ())),
            "confession_total": len(queues.get("confession_rows", ())),
        }
        total = len(task_rows) if total is None else total
        return build_todo_board_layout(
            board_sections(
                chore_rows, task_rows, currency_emoji="🪙", task_total=total,
                **queues, **totals,
            ),
            render_board_footer(
                chore_rows, total, totals["signoff_total"],
                totals["approval_total"], totals["confession_total"],
            ),
        )

    loud = [
        {"id": 400 + n, "task": "_*" * 250, "quest_title": "_*" * 250,
         "summary": "_*" * 250, "content": "_*" * 250, "kind": "sponsor",
         "reward": 10**9, "price": 10**9, "created_at": now, "recurring_id": n,
         "todo_id": 400 + n, "claimant_name": "N" * 32, "requester_name": "N" * 32,
         "completed_at": now, "completed_by_name": "N" * 32, "streak": 999,
         "buyer_name": "N" * 32, "purchase_id": 1}
        for n in range(16)
    ]

    bugs = [
        {"id": 254, "task": "Quote cards cut off the last line on mobile", "added_by": a},
        {"id": 257, "task": "/wallet shows yesterday's balance after a transfer",
         "added_by": b, "description": "Sent 50, the card still showed the old total"},
        {"id": 261, "task": "Truth or Dare Join button says interaction failed",
         "added_by": c},
    ]
    many_bugs = [
        {"id": 300 + n, "task": f"Sample bug number {n + 1} with a fairly ordinary title"}
        for n in range(16)
    ]

    def bug_board(rows, total=None):
        total = len(rows) if total is None else total
        return build_bug_board_layout(
            render_rows(rows, total=total), render_footer(total, noun="bug")
        )

    [(arrival, _), (digest, _)] = plan_cards(
        data={
            "now": now, "cursor_at": now - 60, "digest_at": 0.0,
            "tz_offset_hours": 0.0, "tasks": tasks[:1] + tasks[2:3],
            "signoffs": signoffs, "approvals": approvals[:1], "confessions": 2,
            "spawned": 3, "missed": 1,
        },
        name_fn=name_fn,
    )
    [(one_bug, _)] = plan_bug_cards(data={"now": now, "cursor_at": now - 60, "bugs": bugs[:1]})
    [(burst, _)] = plan_bug_cards(data={"now": now, "cursor_at": now - 60, "bugs": many_bugs + bugs})

    def with_shots(plan, shots):
        """A bug card with screenshots. The real card references its own
        uploads (``attachment://``); a sample has none to upload, so the
        bot's avatar stands in for each picture."""
        items = build_change_card_layout(
            plan.title, plan.body,
            images=[(image_filename(bug_id, n, "png"), bug_id) for bug_id, n in shots],
        )
        gallery = items[-1]
        gallery.items = [
            discord.MediaGalleryItem(avatar_url, description=item.description)
            for item in gallery.items
        ]
        return items

    picker_rows = [
        {"id": r["todo_id"], "task": r["task"], "description": ""} for r in chores[1:]
    ] + tasks

    return [
        ("Todo board — nothing on it", todo_board([], [])),
        ("Todo board — tasks only", todo_board([], tasks)),
        ("Todo board — every section", todo_board(
            chores, tasks, total=19, signoff_rows=signoffs,
            approval_rows=approvals, confession_rows=confessions,
        )),
        ("Todo board — all markdown, trimmed to fit", todo_board(
            loud[:9], loud, total=999, signoff_rows=loud[:6],
            approval_rows=loud[:6], confession_rows=loud[:6],
        )),
        ("Bug board — no open bugs", bug_board([])),
        ("Bug board — populated", bug_board(bugs)),
        ("Bug board — more than a screenful", bug_board(many_bugs, total=23)),
        ("Complete picker — todo (ephemeral)", build_complete_picker_layout(picker_rows)),
        ("Complete picker — todo, completed", build_completed_layout(2)),
        ("Complete picker — todo, someone got there first", build_completed_layout(0)),
        ("Complete picker — bugs (ephemeral)", build_complete_picker_layout(bugs, bugs=True)),
        ("Complete picker — bugs, fixed", build_completed_layout(1, bugs=True)),
        ("Complete picker — bugs, someone got there first", build_completed_layout(0, bugs=True)),
        ("Change card — arrivals", build_change_card_layout(arrival.title, arrival.body)),
        ("Change card — noon chore digest", build_change_card_layout(digest.title, digest.body)),
        ("Bug card — one new bug", build_change_card_layout(one_bug.title, one_bug.body)),
        ("Bug card — with screenshots", with_shots(one_bug, [(254, 1), (254, 2)])),
        ("Bug card — a burst, more than it prints", build_change_card_layout(burst.title, burst.body)),
    ]


def casino_samples(guild: discord.Guild, avatar_url: str):
    """A representative card from every paradigm
    ``bot_modules/casino/cog/layouts.py`` produces — receipts, live boards
    and game-turn cards, win and loss, plus the pools market and the public
    big-win broadcast. The hub panel itself is
    ``hub_layout.build_casino_hub_layout``'s sample, not this one — the
    table dropdown it draws replaced the button-per-table hub these cards
    once shared a gallery entry with."""
    from bot_modules.casino.cog import layouts as casino
    from bot_modules.casino import logic as game_logic
    from bot_modules.casino import pools_logic, pools_metrics
    from bot_modules.casino.service import CasinoSettings, MinesStep
    from bot_modules.economy.service import EconSettings

    name_fn = _placeholder_names()
    now = int(time.time())
    econ = EconSettings(
        currency_emoji="🪙", currency_name="coin", currency_plural="coins"
    )
    settings = CasinoSettings()
    # Roulette, the derby, baccarat, dice and keno are each their own
    # private round — ONE player stacks bets in it, never two — and each
    # game describes its own bets differently, so every table below gets
    # its own fixture in that game's real bet-description format rather
    # than one roulette-flavoured tuple reused everywhere (casino_spec.md
    # "Roulette, the derby, baccarat, dice and keno are ONE machine").
    roulette_round_bets = [
        (TARGET_ID, game_logic.describe_bet("red", 0), 20),
        (TARGET_ID, game_logic.describe_bet("straight", 17), 5),
    ]
    roulette_result_bets = [
        (TARGET_ID, game_logic.describe_bet("red", 0), 20, 40),
        (TARGET_ID, game_logic.describe_bet("straight", 17), 5, 0),
    ]
    derby_result_bets = [
        (TARGET_ID, game_logic.describe_runner(0), 20, 50),
        (TARGET_ID, game_logic.describe_runner(2), 10, 0),
    ]
    baccarat_result_bets = [
        (TARGET_ID, game_logic.describe_baccarat_side("banker"), 20, 40),
        (TARGET_ID, game_logic.describe_baccarat_side("tie"), 5, 0),
    ]
    # The sample roll below totals 6 (Small, Even) — Small wins, Odd loses.
    dice_result_bets = [
        (TARGET_ID, game_logic.describe_sicbo_bet("small"), 20, 40),
        (TARGET_ID, game_logic.describe_sicbo_bet("odd"), 10, 0),
    ]
    # A pick-6 ticket that caught 4 (pays 8×) and a pick-4 that caught 1
    # (below the pay line) — one player holding two tickets, the way Keno's
    # own "itemise the losers" branch expects to see a real miss.
    _keno_win_ticket = [4, 12, 21, 33, 47, 62]
    _keno_lose_ticket = [7, 19, 28, 55]
    keno_drawn = sorted(
        {4, 7, 12, 21, 33, 2, 9, 15, 18, 24, 29, 36, 40, 44, 50, 58, 60, 65, 70, 75}
    )
    keno_result_bets = [
        (TARGET_ID, game_logic.describe_keno_ticket(_keno_win_ticket), 20, 160),
        (TARGET_ID, game_logic.describe_keno_ticket(_keno_lose_ticket), 10, 0),
    ]
    spec = pools_metrics.SPECS[pools_metrics.ANCHOR]
    # The broadcast's recap is copied from the player's own result card
    # (CardResult.recap — see build_big_win_broadcast_layout's docstring),
    # never hand-typed, so build the real jackpot slots card first and
    # reuse its recap for the sample below as well as for the "Slots —
    # jackpot" entry further down.
    jackpot_result = casino.build_slots_layout(
        econ, TARGET_ID, ("7", "7", "7"), 10, 5_000, "Jackpot!",
        jackpot_won=5_000, name_fn=name_fn,
    )
    big_win = casino.build_big_win_broadcast_layout(
        jackpot_result.recap,
        payout=5_000, threshold=1_000, stake=10, min_mult=5,
        game_label="Slots", winner_name=name_fn(TARGET_ID),
        winner_icon=avatar_url,
    )
    assert big_win is not None  # payout well above threshold — always built
    return [
        ("How the house pays", casino.build_help_layout(econ, settings)),
        (
            "Daily comp — win",
            casino.build_comp_layout(
                econ, TARGET_ID, ("7", "7", "🍒"), 10, 50, "Two 7s!", name_fn=name_fn
            ),
        ),
        (
            "Coinflip — win",
            casino.build_coinflip_layout(
                econ, TARGET_ID, "heads", "heads", 10, 20, name_fn=name_fn
            ).items,
        ),
        ("Slots — jackpot", jackpot_result.items),
        (
            "Blackjack — live hand",
            casino.build_blackjack_layout(
                econ, TARGET_ID, ["A♠", "K♥"], ["9♦", "8♣"], 10, name_fn=name_fn
            ).items,
        ),
        (
            "Roulette — your wheel (live board)",
            casino.build_roulette_round_layout(
                econ, now + 60, roulette_round_bets, name_fn=name_fn
            ),
        ),
        (
            "Roulette — result",
            casino.build_roulette_result_layout(
                econ, 7, roulette_result_bets, name_fn=name_fn
            ).items,
        ),
        (
            "Meadow Derby — result",
            casino.build_derby_result_layout(
                econ, 0, [3, 5, 7, 9, 11, 2], derby_result_bets, name_fn=name_fn
            ).items,
        ),
        (
            "Baccarat — result",
            casino.build_baccarat_result_layout(
                econ, ["A♠", "9♥"], ["K♦", "2♣"], baccarat_result_bets, name_fn=name_fn
            ).items,
        ),
        (
            "Dice — result",
            casino.build_dice_result_layout(
                econ, (1, 2, 3), dice_result_bets, name_fn=name_fn
            ).items,
        ),
        (
            "Keno — result",
            casino.build_keno_result_layout(
                econ, keno_drawn, keno_result_bets, name_fn=name_fn
            ).items,
        ),
        (
            "Casino War — standoff win",
            casino.build_war_layout(
                econ, TARGET_ID, "K♠", "K♦", 10,
                war_player="Q♣", war_dealer="9♥",
                outcome="war_win", payout=30, name_fn=name_fn,
            ).items,
        ),
        (
            "Mines — live grid",
            casino.build_mines_layout(
                econ, TARGET_ID,
                MinesStep(
                    hand_id=1, stake=10, bombs=3, revealed=(0, 1),
                    mult=150, next_mult=200,
                ),
                name_fn=name_fn,
            ).items,
        ),
        (
            "Mines — bombed out",
            casino.build_mines_layout(
                econ, TARGET_ID,
                MinesStep(
                    hand_id=1, stake=10, bombs=3, revealed=(0,),
                    outcome="bombed", payout=0, bomb_tiles=(1, 5, 9),
                ),
                name_fn=name_fn,
            ).items,
        ),
        (
            "Your Night at the Tables (My Stats)",
            casino.build_my_stats_layout(
                econ, None, 100, 500, now + 3600, comp_amount=200
            ),
        ),
        (
            "Jackpot celebration",
            casino.build_jackpot_celebration_layout(econ, TARGET_ID, 42_000, name_fn=name_fn),
        ),
        (
            "Pools — today's market",
            casino.build_pools_panel_layout(
                econ, 1_200.0, pools_logic.PoolSplit(400, 250), now + 3600,
                "2026-08-03", spec=spec,
            ),
        ),
        (
            "Pools — the day is in",
            casino.build_pools_result_layout(
                econ, "2026-08-03", 1_250, 1_200.0, "over",
                [(TARGET_ID, 100, 190), (OTHER_IDS[0], 50, 0)], 30,
                spec=spec, name_fn=name_fn,
            ),
        ),
        (
            "Pools — no market today",
            casino.build_pools_void_layout("2026-08-03", 250),
        ),
        ("Big win — public broadcast", big_win.items),
    ]


def guess_samples(guild: discord.Guild, avatar_url: str):
    """Every card ``bot_modules/games/guess/layouts.py`` can produce: the
    channel prompt (plain and with the lobby strip), Help (built-in and from
    a doc), the round card (photo, confession, with the strip), the solved
    reveal (named, the no-contact degrade, with the strip), the guess picker in each of its states, the crop
    editor (with and without detections), the confession preview, the opt-in
    consent, the leaderboard (empty, full, with the strip), the mod inspector (open,
    solved, deleted) and the one-line outcomes.

    The live cards show an uploaded file by ``attachment://`` name; a sample
    is sent with no upload, so every such reference is swapped for the bot's
    own avatar — spoilered exactly where the real card spoilers it."""
    del guild
    from bot_modules.games.guess import layouts
    from bot_modules.games.guess.models import GuessRound

    name_fn = _placeholder_names()
    a, b, c = REQUESTER_ID, TARGET_ID, OTHER_IDS[0]

    def hosted(items):
        """Swap each ``attachment://`` gallery for the placeholder image."""
        out = []
        for item in items:
            if isinstance(item, discord.ui.MediaGallery):
                item = discord.ui.MediaGallery(*[
                    discord.MediaGalleryItem(
                        avatar_url, description=m.description, spoiler=m.spoiler
                    )
                    for m in item.items
                ])
            out.append(item)
        return out

    banners = f"{dashboard_base()}/static/panel-banners"
    art = {
        "header_url": f"{banners}/fd7f86580127410a95a47ee1f716e29f.webp",
        "footer_url": f"{banners}/0a7c324b50c14c4ea05d982cdcc283e5.webp",
    }

    def round_row(**kw):
        base = dict(
            id=446, guild_id=1, submitter_id=a, answer_id=a, channel_id=1,
            message_id=1, crop_path="", crop_url=avatar_url, original_path="",
            difficulty="medium", candidate_count=4, reroll_count=0,
            created_at=1_790_000_000.0, solved_at=None, solver_id=None,
            guesses_to_solve=None, unique_guessers_to_solve=None,
            answer_optout=False, deleted_at=None,
        )
        base.update(kw)
        return GuessRound(**base)

    members = [(f"Member {i:02d}", str(i)) for i in range(1, 26)]
    crop = layouts.attachment_ref(layouts.CROP_FILENAME)
    full = layouts.attachment_ref(layouts.FULL_FILENAME)
    named = layouts.reveal_names(a, a, no_contact=False, name_fn=name_fn)
    blocked = layouts.reveal_names(a, b, no_contact=True, name_fn=name_fn)

    def picker(status, **kw):
        kw.setdefault("placeholder", layouts.PHOTO_QUESTION)
        return layouts.build_picker_layout(446, members, status=status, **kw)

    return [
        ("Channel prompt", layouts.build_prompt_layout()),
        ("Channel prompt — with the lobby strip", layouts.build_prompt_layout(**art)),
        ("Help — built-in (no guess doc)", layouts.build_help_layout(None)),
        ("Help — from the guild's guess doc", layouts.build_help_layout(
            "# Guess Who House Rules\nFaces and tattoos stay out of the crop."
            "\n\n---\n\nOne round a day each, please.",
        )),
        ("Round — photo, no guesses yet", hosted(
            layouts.build_round_layout(446, 0, media=crop))),
        ("Round — photo, guesses in", hosted(
            layouts.build_round_layout(446, 12, media=crop))),
        ("Round — with the lobby strip", hosted(
            layouts.build_round_layout(446, 12, media=crop, **art))),
        ("Round — confession", hosted(layouts.build_round_layout(
            447, 3, media=layouts.attachment_ref(layouts.CONFESSION_FILENAME),
            confession=True,
        ))),
        ("Solved — full image revealed", hosted(layouts.build_solved_layout(
            446, *named, c, 12, 5, name_fn=name_fn, media=full,
            self_submitted=True,
        ))),
        ("Solved — first guess, no-contact pair degrades to ids", hosted(
            layouts.build_solved_layout(
                446, *blocked, c, 1, 1, name_fn=name_fn, media=full,
            ))),
        ("Solved — confession keeps its card", hosted(layouts.build_solved_layout(
            447, *named, c, 3, 2, name_fn=name_fn,
            media=layouts.attachment_ref(layouts.CONFESSION_FILENAME),
            confession=True, self_submitted=True,
        ))),
        ("Solved — with the lobby strip", hosted(layouts.build_solved_layout(
            446, *named, c, 12, 5, name_fn=name_fn, media=full,
            self_submitted=True, **art,
        ))),
        ("Picker — asking", picker(layouts.picker_prompt(60))),
        ("Picker — paged, filtered", picker(
            layouts.picker_prompt(60),
            placeholder=layouts.picker_placeholder(
                layouts.PHOTO_QUESTION, filter_query="mem", match_count=40,
                page=0, page_count=2,
            ),
            page=0, page_count=2, filter_query="mem",
        )),
        ("Picker — filter matched nobody", layouts.build_picker_layout(
            446, [], status=layouts.picker_prompt(60), placeholder="",
            filter_query="zzz",
        )),
        ("Picker — wrong guess", picker("❌ Not it. Keep trying!")),
        ("Picker — on cooldown", picker(
            "⏳ On cooldown — you can guess again <t:1790000060:R>.")),
        ("Picker — out of guesses", picker(
            "❌ You're out of guesses on this round (cap: 5).")),
        ("Picker — solved it", picker("✅ **Correct!** You solved Round #446!")),
        ("Picker — correct, but late", picker(
            "⌛ Right name — but someone already solved this one.")),
        ("Picker — wrong, and solved meanwhile", picker(
            "⌛ Someone solved this round while you were picking.")),
        ("Crop editor — detections found", hosted(
            layouts.build_crop_editor_layout(detections=True))),
        ("Crop editor — nothing detected", hosted(
            layouts.build_crop_editor_layout(detections=False))),
        ("Confession preview", hosted(layouts.build_confession_preview_layout())),
        ("Opt-in consent", layouts.build_consent_layout(7)),
        ("Leaderboard — empty", layouts.build_leaderboard_layout(
            [], [], name_fn=name_fn)),
        ("Leaderboard — full", layouts.build_leaderboard_layout(
            [(a, 41, 38), (b, 30, 12), (c, 9, 9), (OTHER_IDS[1], 4, 0),
             (OTHER_IDS[2], 1, 1)],
            [(c, 52), (a, 17), (OTHER_IDS[1], 8), (b, 2), (OTHER_IDS[2], 1)],
            name_fn=name_fn,
        )),
        ("Leaderboard — with the lobby strip", layouts.build_leaderboard_layout(
            [(a, 41, 38), (b, 30, 12), (c, 9, 9)], [(c, 52), (a, 17)],
            name_fn=name_fn, **art,
        )),
        ("Inspector — open", layouts.build_inspector_layout(
            round_row(), 12, 5, name_fn=name_fn)),
        ("Inspector — solved", layouts.build_inspector_layout(
            round_row(solved_at=1_790_000_900.0, solver_id=c), 12, 5,
            name_fn=name_fn)),
        ("Inspector — deleted, no crop", layouts.build_inspector_layout(
            round_row(deleted_at=1_790_000_900.0, crop_url=""), 0, 0,
            name_fn=name_fn)),
        ("Notice — round posted", layouts.build_notice_layout(
            "✅ Round Posted", "It's up in <#1515999999999999968>.")),
        ("Notice — post failed, rolled back", layouts.build_notice_layout(
            "❌ Couldn't Post That Round",
            "Nothing was saved. Try again in a moment.")),
        ("Notice — joined the pool", layouts.build_notice_layout(
            "✅ Welcome to the Guess Pool",
            "You can now submit images and be guessed at. "
            "Leave anytime with `/guess optout`.")),
    ]


def pen_pals_samples(guild: discord.Guild, avatar_url: str):
    """Every card ``bot_modules/pen_pals/layouts.py`` can produce: the signup
    panel (instant and scheduled, empty and populated, each room-visibility
    line, with and without the hub banner), Help with and without a doc,
    the pinned room intro (plain, and with an opening message and no swaps),
    the closing DM's three outcomes and its unnamed-partner wording, the two
    ended-early DMs, status in all four states, the blocklist empty and
    populated, and the end confirm and what it becomes.

    The DMs are shown as the builders return them; a real one also gets the
    server icon beside its first line and a ``-# From {server}`` line from
    ``shared.dm_branding``."""
    del guild, avatar_url
    from bot_modules.pen_pals import layouts as pp

    name_fn = _placeholder_names()
    a, b = REQUESTER_ID, TARGET_ID
    now = int(time.time())
    banners = f"{dashboard_base()}/static/panel-banners"
    # The live guild's hub banner with "Pen Pals" drawn in — the file
    # ``with_title`` writes on the panel's first build — so the sample shows
    # the panel as prod will: the drawn name, no ``##`` heading.
    banner = f"{banners}/titled/962b492aeafc4c8693dab56a24317ee2-870571e6762b.webp"
    footer = f"{banners}/447650e9e2ff4c0bbafd369c6a18fc13.webp"
    # The lobby strip with "Pen Pals" between the leaves, as ``with_lobby_title``
    # draws it for the room's question cards.
    strip = f"{banners}/titled/541e6bea993d454d801661e607a5ba18-15ef3037f613.webp"
    gid = 999999999999999999
    return [
        ("Signup panel — instant, nobody waiting", pp.build_panel_layout(0)),
        ("Signup panel — instant, three waiting, with the hub banner", pp.build_panel_layout(
            3, banner_url=banner, footer_url=footer, title_in_banner=True,
        )),
        ("Signup panel — scheduled weekly round, mods can see chats", pp.build_panel_layout(
            1, scheduled=True, schedule_label="Thursdays at 5:00 PM server time",
            session_label="3 days", visibility="mods",
        )),
        ("Signup panel — chats readable by everyone", pp.build_panel_layout(
            12, visibility="everyone",
        )),
        ("Help — the guild has no penpals doc", pp.build_help_layout(None)),
        ("Help — the guild's doc", pp.build_help_layout(
            "# How Pen Pals Works\nJoin the pool and you're paired with another "
            "member for a day.\n\n---\n\nLeave whenever you like — it sticks "
            "until you rejoin.",
        )),
        ("Room intro — plain", pp.build_intro_layout(
            name_fn(a), name_fn(b), now + 86400,
        )),
        ("Room intro — opening message, no swaps, mods can see", pp.build_intro_layout(
            name_fn(a), name_fn(b), now + 86400,
            intro_message="Be kind, be curious, and say hi within the hour!",
            max_swaps=0, visibility="mods",
        )),
        ("Closing DM — back in the pool, with a cooldown", pp.build_closed_layout(
            gid, partner_name=name_fn(b), outcome=pp.OUTCOME_REQUEUED,
            eligible_at=now + 30 * 86400,
        )),
        ("Closing DM — back in the pool, no cooldown", pp.build_closed_layout(
            gid, partner_name=name_fn(b), outcome=pp.OUTCOME_REQUEUED,
        )),
        ("Closing DM — left out for staying quiet", pp.build_closed_layout(
            gid, partner_name=name_fn(b), outcome=pp.OUTCOME_SKIPPED,
        )),
        ("Closing DM — nothing changed", pp.build_closed_layout(
            gid, partner_name=name_fn(b),
        )),
        ("Closing DM — partner not named (left, blocked or no-contact)", pp.build_closed_layout(
            gid, partner_name=None, outcome=pp.OUTCOME_REQUEUED, eligible_at=now + 86400,
        )),
        ("Ended early DM — partner unavailable, re-pooled", pp.build_partner_gone_layout(
            requeued=True,
        )),
        ("Ended early DM — partner unavailable, member had left the pool", pp.build_partner_gone_layout(
            requeued=False,
        )),
        ("Ended early DM — partner ended it", pp.build_ended_by_partner_layout()),
        ("Status — matched", pp.build_status_matched_layout(
            name_fn(b), channel_id=None, expiry_at=now + 3600 * 7, swaps_left=2,
        )),
        ("Status — matched, after leaving the pool", pp.build_status_matched_layout(
            name_fn(b), channel_id=None, expiry_at=now + 3600 * 7, swaps_left=0,
            opted_out=True,
        )),
        ("Status — waiting, instant", pp.build_status_pool_layout(2)),
        ("Status — waiting, scheduled", pp.build_status_pool_layout(
            4, scheduled=True, schedule_label="12:00 PM server time",
        )),
        ("Status — left the pool", pp.build_status_left_layout(now - 3 * 86400)),
        ("Status — not in the pool", pp.build_status_idle_layout()),
        ("Blocklist — empty", pp.build_block_layout([])),
        ("Blocklist — three blocked, one gone from the server", pp.build_block_layout([
            (b, name_fn(b)), (OTHER_IDS[0], name_fn(OTHER_IDS[0])),
            (OTHER_IDS[1], f"User {OTHER_IDS[1]}"),
        ])),
        ("Question — first, with the lobby strip", pp.build_question_layout(
            pp.QUESTION_FIRST, "What's a small thing that made your week better?",
            a, b, header_url=strip, footer_url=footer, title_in_header=True,
        )),
        ("Question — daily top-up, with the lobby strip", pp.build_question_layout(
            pp.QUESTION_AUTO, "If you could live anywhere for a year, where?",
            a, b, header_url=strip, footer_url=footer, title_in_header=True,
        )),
        ("Question — swapped, with the lobby strip", pp.build_question_layout(
            pp.QUESTION_SWAP, "What's a hobby you'd pick up with unlimited time?",
            a, b, swaps_left=2, header_url=strip, footer_url=footer, title_in_header=True,
        )),
        ("Question — no art configured", pp.build_question_layout(
            pp.QUESTION_FIRST, "What's a small thing that made your week better?", a, b,
        )),
        ("End confirm", pp.build_end_confirm_layout()),
        ("End confirm — pressed", pp.build_end_closing_layout()),
    ]


def whisper_samples(guild: discord.Guild, avatar_url: str):
    """Every card ``bot_modules/whisper/layouts.py`` can produce: the launcher
    plain and wearing the guild's lobby strip, the three feed notices, the
    whisper DM in each state (fresh, out of guesses, shared, deleted), the
    reply DM, the sender's digest, the inbox list (empty, a page, paged with a
    filter, a filter with no matches) and the opened whisper (received and
    sent), the delete confirm and its receipt, both guess pickers and the
    three results, the opt-in card and receipts, the pool refusal with a
    draft, the three mod-log notices and Help with no doc."""
    del guild, avatar_url
    from bot_modules.whisper.layouts import (
        build_delete_confirm_layout,
        build_deleted_layout,
        build_guess_digest_layout,
        build_guess_list_picker_layout,
        build_guess_native_picker_layout,
        build_guess_result_layout,
        build_help_layout,
        build_inbox_detail_layout,
        build_inbox_list_layout,
        build_launcher_layout,
        build_optin_done_layout,
        build_optin_layout,
        build_optout_done_layout,
        build_pool_refusal_layout,
        build_reply_audit_layout,
        build_reply_dm_layout,
        build_reply_report_audit_layout,
        build_report_audit_layout,
        build_send_feed_layout,
        build_share_feed_layout,
        build_solved_feed_layout,
        build_whisper_dm_layout,
    )
    from bot_modules.whisper.models import Whisper, WhisperReply
    from bot_modules.whisper.service import ERROR_TARGET_NEEDS_ROLE, GuessOutcome

    name_fn = _placeholder_names()
    now = time.time()
    sender, target = REQUESTER_ID, TARGET_ID
    # The live guild's small lobby strip and its footer (``lobby_art``),
    # untitled: prod draws the "Whisper" strip on the launcher's first
    # repost after the deploy.
    banners = f"{dashboard_base()}/static/panel-banners"
    art = {
        "header_url": f"{banners}/fd7f86580127410a95a47ee1f716e29f.webp",
        "footer_url": f"{banners}/0a7c324b50c14c4ea05d982cdcc283e5.webp",
    }

    def _w(i=12, **over):
        row = dict(
            id=i, guild_id=1, sender_id=sender, target_id=target,
            message="You have the kindest laugh in the whole voice chat.",
            created_at=now - 3 * 3600, state="pending", solved=False,
            guesses_left=3, channel_msg_id=1, dm_msg_id=2,
        )
        row.update(over)
        return Whisper(**row)

    def _list(rows, **over):
        args = dict(
            mode="received", total=len(rows), shown=len(rows), page=0,
            page_count=1, filter_query="", name_fn=name_fn,
        )
        args.update(over)
        return build_inbox_list_layout(rows, **args)

    page = [
        _w(20 + i, message=m, created_at=now - i * 5400, guesses_left=g, state=st)
        for i, (m, g, st) in enumerate([
            ("You have the kindest laugh in the whole voice chat.", 3, "pending"),
            ("Your playlist last night was perfect.", 2, "pending"),
            ("I think about your photo thread more than I should.", 0, "pending"),
            ("Thanks for sticking up for me in general.", 3, "shared"),
            ("You're better at Chicken than you think.", 1, "pending"),
            ("Would you ever host a game night?", 3, "pending"),
            ("Your bio made me laugh out loud.", 3, "pending"),
            ("Still thinking about that story you told.", 2, "pending"),
            ("You give the best welcome messages.", 3, "pending"),
            ("Someone here has a crush. Guess who.", 3, "pending"),
        ])
    ]
    wrong, exhausted, correct = (
        GuessOutcome(False, 2, False), GuessOutcome(False, 0, True), GuessOutcome(True, 2, False),
    )
    return [
        ("Launcher — plain", build_launcher_layout()),
        ("Launcher — with the lobby strip", build_launcher_layout(**art)),
        ("Feed — someone sent a whisper", build_send_feed_layout(target, name_fn)),
        ("Feed — a whisper was shared", build_share_feed_layout(_w(), name_fn)),
        ("Feed — solved", build_solved_feed_layout(target, name_fn)),
        ("DM — fresh whisper, every button", build_whisper_dm_layout(_w())),
        ("DM — out of guesses", build_whisper_dm_layout(_w(guesses_left=0))),
        ("DM — shared", build_whisper_dm_layout(_w(state="shared"))),
        ("DM — deleted (no buttons)", build_whisper_dm_layout(_w(deleted_at=now))),
        ("DM — anonymous reply", build_reply_dm_layout(
            whisper_id=12, whisper_message=_w().message,
            reply_content="Ha — I know exactly who you are.", reply_id=7,
        )),
        ("DM — the sender's guess digest", build_guess_digest_layout(
            whisper_id=12,
            guesses=[(name_fn(OTHER_IDS[0]), wrong), ("someone", wrong), ("you", correct)],
        )),
        ("Inbox — empty", _list([])),
        ("Inbox — one page", _list(page[:3])),
        ("Inbox — full page, paged, filter on", _list(
            page, total=23, shown=14, page=1, page_count=2, filter_query="you",
        )),
        ("Inbox — filter with no matches", _list([], total=23, shown=0, filter_query="zebra")),
        ("Sent — one page", _list(page[:3], mode="sent")),
        ("Inbox — opened whisper", build_inbox_detail_layout(_w(), mode="received", name_fn=name_fn)),
        ("Sent — opened whisper", build_inbox_detail_layout(_w(), mode="sent", name_fn=name_fn)),
        ("Delete — confirm", build_delete_confirm_layout(12)),
        ("Delete — receipt", build_deleted_layout(12)),
        ("Guess — native picker (in the server)", build_guess_native_picker_layout(_w())),
        ("Guess — list picker (from the DM), paged", build_guess_list_picker_layout(
            _w(),
            [discord.SelectOption(label=name_fn(uid), value=str(uid)) for uid in OTHER_IDS],
            placeholder="Pick the sender… (1/2)", page=0, page_count=2, filter_query="",
        )),
        ("Guess — wrong", build_guess_result_layout(wrong)),
        ("Guess — out of guesses", build_guess_result_layout(exhausted)),
        ("Guess — solved", build_guess_result_layout(correct)),
        ("Opt-in — consent card", build_optin_layout()),
        ("Opt-in — receipt", build_optin_done_layout()),
        ("Opt-out — receipt", build_optout_done_layout()),
        ("Send — pick hasn't opted in, draft handed back", build_pool_refusal_layout(
            ERROR_TARGET_NEEDS_ROLE, "You have the kindest laugh in the whole voice chat.",
        )),
        ("Mod log — reply", build_reply_audit_layout(
            whisper_id=12, from_user_id=target, to_user_id=sender,
            content="Ha — I know exactly who you are.", name_fn=name_fn,
        )),
        ("Mod log — whisper reported", build_report_audit_layout(
            whisper=_w(), reason="Creepy, and not the first time.", name_fn=name_fn,
        )),
        ("Mod log — reply reported", build_reply_report_audit_layout(
            reply=WhisperReply(7, 12, target, sender, "Ha — I know exactly who you are.", now),
            reporter_id=sender, reason="(no reason provided)", name_fn=name_fn,
        )),
        ("Help — no doc written yet", build_help_layout(None)),
    ]


def card_strips_samples(guild: discord.Guild, avatar_url: str):
    """The thin, untitled card strips at the heights on offer (Ben,
    2026-09-30), on three real cards: a public economy post, a game's
    follow-on card and a staff-channel review card. The strips come from
    ``make_panel_art.py card --height H --out <CARD_STRIP_SAMPLES>/hH`` and
    ride on the sample message as attachments, since they are not uploaded
    anywhere yet."""
    from bot_modules.core.branding_service import PanelArt
    from bot_modules.core.layout import card_strips

    cards = {
        "bounty card": dict(bounties_samples(guild, avatar_url))["Board card — open"],
        "Clapback scoreboard": dict(clapback_samples(guild, avatar_url))[
            "Scoreboard — bye and Next Round"
        ],
        "Pin of the Day review": dict(pins_samples(guild, avatar_url))[
            "Review card — pending"
        ],
    }
    out = []
    for h in CARD_STRIP_HEIGHTS:
        header, footer = f"card_header_{h}.webp", f"card_footer_{h}.webp"
        ATTACHMENTS[header] = CARD_STRIP_SAMPLES / f"h{h}" / "card_header.webp"
        ATTACHMENTS[footer] = CARD_STRIP_SAMPLES / f"h{h}" / "card_footer.webp"
        art = PanelArt(
            None, None,
            card_header=f"attachment://{header}", card_footer=f"attachment://{footer}",
        )
        for name, items in cards.items():
            top, bottom = card_strips(art)
            out.append((f"{h} px strips — {name}", [*top, *items, *bottom]))
    return out


#: Where ``card_strips_samples`` finds each height's pair of strips.
CARD_STRIP_SAMPLES = Path.home() / "dk-panel-art-2026-09-30-card"
CARD_STRIP_HEIGHTS = (80, 100)

#: ``attachment://`` name → local file, filled in by a feature whose art isn't
#: hosted anywhere yet; each posted card carries the files it references.
ATTACHMENTS: dict[str, Path] = {}


#: feature name → (title for the lead line, sample builder).
SAMPLES: dict[str, tuple[str, Callable[..., list]]] = {
    "dm_perms": ("DM Permissions", dm_perms_samples),
    "shop": ("Perk Shop", shop_samples),
    "server_features": ("Server Features", server_features_samples),
    "games_core": ("Games Core", games_core_samples),
    "clapback": ("Clapback", clapback_samples),
    "traditional": ("Truth or Dare", traditional_samples),
    "rushmore": ("Mt. Rushmore Draft", rushmore_samples),
    "wyr": ("Would You Rather", wyr_samples),
    "nhie": ("Never Have I Ever", nhie_samples),
    "mlt": ("Most Likely To", mlt_samples),
    "legitlibs": ("LegitLibs", legitlibs_samples),
    "ama": ("Anonymous AMA", ama_samples),
    "price": ("Name Your Price", price_samples),
    "hottakes": ("Hot Takes", hottakes_samples),
    "fantasies": ("Fantasies & Dealbreakers", fantasies_samples),
    "ttl": ("Two Truths and a Lie", ttl_samples),
    "ffa": ("Anonymous Truth or Dare", ffa_samples),
    "mahjong": ("Meadow Mahjong", mahjong_samples),
    "guess": ("Guess Who", guess_samples),
    "pen_pals": ("Pen Pals", pen_pals_samples),
    "duels": ("Duels", duels_samples),
    "mfk": ("Marry, Fornicate, Kiss", mfk_samples),
    "compliment": ("Spin the Compliment", compliment_samples),
    "story": ("Story Builder", story_samples),
    "quickdraw": ("Quickdraw", quickdraw_samples),
    "chicken": ("Chicken", chicken_samples),
    "musical_chairs": ("Musical Chairs", musical_chairs_samples),
    "hot_potato": ("Hot Potato (duel)", hot_potato_samples),
    "hot_potato_group": ("Hot Potato (Group)", hot_potato_group_samples),
    "pressure_cooker": ("Pressure Cooker", pressure_cooker_samples),
    "risky": ("Risky Rolls", risky_samples),
    "wallet": ("Wallet", wallet_samples),
    "economy": ("Economy", economy_samples),
    "quests": ("Quests", quests_samples),
    "drops": ("Coin Drops", drops_samples),
    "auctions": ("Auctions", auctions_samples),
    "pins": ("Pin of the Day", pins_samples),
    "themes": ("Flash Themes", themes_samples),
    "bounties": ("Bounty Board", bounties_samples),
    "casino": ("Casino", casino_samples),
    "whisper": ("Whisper", whisper_samples),
    "todo": ("Todo & Bug List", todo_samples),
    "card_strips": ("Card strips", card_strips_samples),
}


# ── plumbing ──────────────────────────────────────────────────────────────


def _load_config(env_file: Path):
    """Load the prod ``.env`` *before* ``core.config`` runs its own
    ``load_dotenv`` (which searches upward from cwd and finds nothing in a
    worktree), then build the config."""
    load_dotenv(env_file, override=True)
    from bot_modules.core.config import load_config  # noqa: PLC0415

    return load_config()


def build_card(state: str, title: str, items) -> discord.ui.LayoutView:
    """One sample message: the lead line and inert note, then the card.

    ``sample_prefix_ids`` runs last so it also catches ids inside the card's
    nested rows and sections.
    """
    view = discord.ui.LayoutView(timeout=None)
    # The lead line and the inert note ride inside the card's own title block
    # rather than as a Text Display of their own: the fullest card this
    # feature builds is the 40-component ceiling exactly (a full page of
    # connections with both arrows), and a wrapper that spent one component
    # would put the sample of it over the ceiling the sample exists to check.
    # Every card leads with a glyph-led ``## Title`` Text Display, which a
    # builder test guarantees, so the fold is always onto that block.
    items = list(items)
    lead = f"-# Sample — {title}: {state}\n{INERT}"
    if items and isinstance(items[0], discord.ui.TextDisplay):
        items[0] = discord.ui.TextDisplay(f"{lead}\n{items[0].content}", id=items[0].id)
    else:
        items.insert(0, discord.ui.TextDisplay(lead))
    for item in items:
        view.add_item(item)

    from bot_modules.core.layout import sample_prefix_ids  # noqa: PLC0415

    sample_prefix_ids(view)
    return view


def attached(view: discord.ui.LayoutView) -> list[str]:
    """The ``attachment://`` names ``view``'s galleries reference."""
    names = []
    for item in view.walk_children():
        if isinstance(item, discord.ui.MediaGallery):
            for media in item.items:
                url = media.media.url
                if url.startswith("attachment://"):
                    names.append(url.removeprefix("attachment://"))
    return names


def describe(view: discord.ui.LayoutView) -> str:
    def walk(items, depth=0):
        for item in items:
            name = type(item).__name__
            extra = ""
            if isinstance(item, discord.ui.TextDisplay):
                extra = f" ({len(item.content)} chars)"
            elif isinstance(item, discord.ui.Button):
                extra = f" [{item.label}]"
            elif isinstance(item, discord.ui.Select):
                extra = f" [{len(item.options)} options]"
            yield "  " * depth + name + extra
            kids = getattr(item, "children", None)
            if kids:
                yield from walk(kids, depth + 1)
            accessory = getattr(item, "accessory", None)
            if accessory is not None:
                yield from walk([accessory], depth + 1)

    return "\n".join(walk(view.children))


def _plural(count: int, noun: str) -> str:
    """``--only`` routinely selects exactly one card; "1 card states" reads
    like a bug in the script rather than a count."""
    return f"{count} {noun}" + ("" if count == 1 else "s")


async def find_scratch_channel(guild: discord.Guild) -> discord.TextChannel | None:
    for channel in await guild.fetch_channels():
        if (
            isinstance(channel, discord.TextChannel)
            and SCRATCH_CHANNEL_FRAGMENT in channel.name
        ):
            return channel
    return None


async def run(
    feature: str, *, post: bool, env_file: Path, only: str | None = None
) -> int:
    from bot_modules.core.layout import MAX_CHARS, MAX_COMPONENTS, layout_budget

    title, builder = SAMPLES[feature]
    config = _load_config(env_file)
    # DB_PATH_* is relative to the checkout that owns the .env, not to a worktree.
    db_path = (env_file.parent / config.db_path).resolve()
    print(f"env={config.env} guild={config.guild_id} db={db_path}")

    client = discord.Client(intents=discord.Intents.none())
    await client.login(config.token)
    try:
        guild = await client.fetch_guild(config.guild_id)
        me = await guild.fetch_member(client.user.id)  # type: ignore[union-attr]
        cards = [
            (state, build_card(state, title, items))
            for state, items in builder(guild, me.display_avatar.url)
        ]

        if only:
            wanted = only.casefold()
            cards = [
                (state, view) for state, view in cards if wanted in state.casefold()
            ]
            if not cards:
                print(
                    f"\nNo card state of {title} matches {only!r}. Available:\n  "
                    + "\n  ".join(
                        state for state, _items in builder(guild, me.display_avatar.url)
                    )
                )
                return 1

        print(f"\n{_plural(len(cards), 'card state')} for {title}:")
        over = 0
        for state, view in cards:
            components, chars = layout_budget(view)
            flag = ""
            if components > MAX_COMPONENTS or chars > MAX_CHARS:
                flag = "  ← OVER BUDGET"
                over += 1
            print(
                f"  {state:<48} {components:>2}/{MAX_COMPONENTS} components, "
                f"{chars:>4}/{MAX_CHARS} chars{flag}"
            )
        if over:
            print(f"\n{over} card(s) over budget — fix before posting.")
            return 1

        missing = sorted(
            {n for _s, view in cards for n in attached(view) if not ATTACHMENTS[n].exists()}
        )
        if missing:
            print("\nMissing attachment files:\n  " + "\n  ".join(
                str(ATTACHMENTS[n]) for n in missing
            ))
            return 1

        fullest = max(cards, key=lambda c: layout_budget(c[1])[0])
        label = "the selected card" if len(cards) == 1 else "the fullest card"
        print(f"\nComponent tree of {label}:\n{describe(fullest[1])}")

        channel = await find_scratch_channel(guild)
        if channel is None:
            print(
                f"\nNo channel matching {SCRATCH_CHANNEL_FRAGMENT!r} in "
                f"{guild.name} — refusing to post anywhere else."
            )
            return 1
        print(f"\nDestination: #{channel.name} ({channel.id})")

        if not post:
            print(
                "\nDry run — nothing posted. Re-run with --post to send "
                f"{_plural(len(cards), 'message')}."
            )
            return 0

        posted: list[tuple[str, discord.Message]] = []
        for state, view in cards:
            files = [discord.File(ATTACHMENTS[n], filename=n) for n in attached(view)]
            message = await channel.send(
                view=view, files=files, allowed_mentions=discord.AllowedMentions.none()
            )
            posted.append((state, message))

        # Read each one back: the budget above is what we *built*, and the
        # mention array is the only proof the global none() default held.
        print(f"\nPosted {len(posted)} messages. Reading them back:\n")
        for state, message in posted:
            fetched = await channel.fetch_message(message.id)
            view = discord.ui.LayoutView.from_message(fetched, timeout=None)
            components, chars = layout_budget(view)
            mentions = [u.id for u in fetched.mentions]
            print(f"{state}")
            print(
                f"  id={fetched.id}  components_v2={fetched.flags.components_v2}  "
                f"mentions={mentions or 'none'}  "
                f"budget={components}/{MAX_COMPONENTS}, {chars}/{MAX_CHARS}"
            )
            print(
                f"  https://discord.com/channels/{guild.id}/{channel.id}/{fetched.id}"
            )
        return 0
    finally:
        await client.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "feature", choices=sorted(SAMPLES), help="which feature's cards to render"
    )
    parser.add_argument(
        "--post",
        action="store_true",
        help=f"actually send the cards to #{SCRATCH_CHANNEL_FRAGMENT}",
    )
    parser.add_argument(
        "--only",
        metavar="STATE",
        help=(
            "render just the card states whose name contains STATE "
            "(case-insensitive), so one fixed card can be re-posted without "
            "the whole gallery"
        ),
    )
    parser.add_argument("--env-file", type=Path, default=PROD_ENV)
    args = parser.parse_args()
    if not args.env_file.exists():
        print(f"no env file at {args.env_file}")
        return 1
    return asyncio.run(
        run(args.feature, post=args.post, env_file=args.env_file, only=args.only)
    )


if __name__ == "__main__":
    raise SystemExit(main())
