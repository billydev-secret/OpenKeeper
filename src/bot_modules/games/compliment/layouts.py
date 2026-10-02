"""Components V2 card builders for Spin the Compliment.

Replaces ``games_compliment/embeds.py`` (deleted with this conversion —
``docs/plans/components_v2_migration.md`` wave 3b, "games"). These functions
accept plain dicts/primitives and return a list of ``discord.ui.Item``. They
never call out to Discord — testable with no network and no mocks of the
Bot/Guild API.

Three card shapes:

* :func:`build_lobby_layout` — **paradigm 8, live board.** The
  join-the-pool card shown alongside the host/player buttons. The cog
  rebuilds and re-sends the whole card on every Add-Me/Leave/host action
  (only the participant list and countdown line change) rather than
  patching parts of it in place, so it needs no stable ids of its own — same
  shape as ``economy/auction_layouts.py``'s sticky auction card.
* :func:`build_pairings_layout` — **paradigm 7, receipt.** The post-close
  card listing each ``giver → receiver`` mapping with the closing "deliver
  your compliment" call-to-action; reposted at the wrap's halfway nudge with
  a ✅ on every giver seen delivering. Members are *named* through a
  ``name_fn`` (``services/name_resolver``), never ``<@id>``-mentioned: a
  mention in a Text Display is resolved by the reading client from its own
  cache and shows as a bare number to anyone who hasn't seen that member,
  and once the ping is gone this card is the only record of who compliments
  whom. The ping itself lives in message ``content``, where a mention
  belongs.
* :func:`build_wrap_recap_layout` — **paradigm 7, receipt.** The closing
  card: how many of the compliments were seen delivered, plus the payout
  line the cog builds from ``economy.game_rewards.payout_footer_line``. The
  cog appends its own **🔁 Spin Again** ``ActionRow`` after this — a
  receipt with one appended action button stays paradigm 7, same precedent
  as ``casino/layouts.py``'s dice-result "Play Again" row.

The line-formatter (:func:`format_pairing_line`) is split out so the
pairings text can be assembled deterministically in tests without
constructing a ``LayoutView``.

``host_name`` and every pre-resolved participant/receiver name arriving as a
plain string is member-written or member-set display text, so it is
``escape_markdown``'d here — a Text Display renders markdown where an embed
field never did (guide § Words).
"""

from __future__ import annotations

from typing import Any

import discord

from bot_modules.core.layout import lobby_art
from bot_modules.games.constants import GAME_ICONS, GAME_NAMES
from bot_modules.games.compliment.logic import delivered_line
from bot_modules.shared.name_resolver import NameFn, mention


def _text(content: str) -> discord.ui.TextDisplay[Any]:
    return discord.ui.TextDisplay(content)


#: The name a titled lobby header wears (``read_lobby_art``).
LOBBY_TITLE = GAME_NAMES["compliment"]


def build_lobby_layout(
    host_name: str,
    participants: list[str],
    *,
    start_at: int | None = None,
    controls: discord.ui.Item[Any] | None = None,
    header_url: str | None = None,
    footer_url: str | None = None,
    title_in_header: bool = False,
) -> list[discord.ui.Item[Any]]:
    """Build the lobby card shown while players are joining. **Paradigm 8 —
    live board.**

    ``participants`` is a list of pre-resolved display names (the cog runs
    the resolution against the guild before calling); each is escaped here.
    Empty list renders as ``"—"`` so the line always has a value.

    ``start_at`` is an optional UTC epoch shown as a live Discord relative
    timestamp — the host's advertised start time. The host still presses the
    button; the countdown is advertising, not automation.

    ``controls`` is the view's button row, placed here so the closing art
    sits below it. ``header_url``/``footer_url``/``title_in_header`` are the
    guild's lobby art (``core.layout.lobby_art``): nothing draws without a
    header, and a titled header already says :data:`LOBBY_TITLE`, so the
    ``##`` heading goes.
    """
    titled = bool(header_url and title_in_header)
    top, bottom = lobby_art(header_url, footer_url, title=LOBBY_TITLE if titled else None)
    safe_host = discord.utils.escape_markdown(host_name)
    lines = [] if titled else [f"## {GAME_ICONS['compliment']} {LOBBY_TITLE}"]
    lines.append(f"**Host:** {safe_host}")
    if start_at:
        lines.append(f"⏰ Starting <t:{start_at}:R>")
    pool_str = (
        ", ".join(discord.utils.escape_markdown(name) for name in participants)
        if participants
        else "—"
    )
    lines.append(f"**Pool ({len(participants)})**\n{pool_str}")
    items: list[discord.ui.Item[Any]] = [*top, _text("\n".join(lines))]
    if controls is not None:
        items.append(controls)
    items.extend(bottom)
    return items


def format_pairing_line(giver_name: str, receiver_name: str, *, delivered: bool = False) -> str:
    """Format a single ``giver → receiver`` line for the pairings card.

    Centralised so the arrow symbol/spacing can be tweaked in one place
    and so tests can assert on the line shape without rendering a card.
    ``delivered`` prefixes a ✅ on the wrap's repost.
    """
    line = f"{giver_name} → {receiver_name}"
    return f"✅ {line}" if delivered else line


def build_pairings_layout(
    pairings: dict[int, int],
    *,
    name_fn: NameFn = mention,
    delivered: "set[int] | None" = None,
    ends_at: int | None = None,
) -> list[discord.ui.Item[Any]]:
    """Build the post-close card announcing the pairings. **Paradigm 7 —
    receipt.**

    ``pairings`` is the ``{giver_id: receiver_id}`` map from
    ``generate_pairings``; every id is rendered through ``name_fn`` (the
    resolver from ``build_name_fn``, which already escapes what it
    returns). The default keeps an un-wired caller rendering a mention
    rather than crashing — the cog always passes one.

    ``delivered`` marks the givers already seen complimenting (the wrap's
    halfway repost); ``ends_at`` is the wrap's closing epoch, shown as a
    live countdown so the room knows when the recap lands. The trailing
    call-to-action is appended unconditionally so even a 2-player game has
    the prompt — and it says *how* to deliver (reply or @mention), because
    that is what the wrap counts.
    """
    done = delivered or set()
    body = "\n".join(
        format_pairing_line(name_fn(giver), name_fn(receiver), delivered=giver in done)
        for giver, receiver in pairings.items()
    )
    cta = "💛 Reply to (or @mention) your partner with your compliment!"
    if ends_at:
        cta += f"\n🏁 Wrap-up <t:{int(ends_at)}:R>."
    heading = f"## {GAME_ICONS['compliment']} Compliment Pairings"
    return [_text(f"{heading}\n{body}\n\n{cta}")]


def build_wrap_recap_layout(
    delivered_count: int,
    total: int,
    *,
    payout_line: str | None = None,
) -> list[discord.ui.Item[Any]]:
    """The closing card after the wrap: how the round went, in one line.
    **Paradigm 7 — receipt.**

    ``payout_line`` is the pre-formatted text from
    ``economy.game_rewards.payout_footer_line`` (``None`` when the economy
    is disabled or pays nothing this round) — the cog resolves it before
    calling so this builder stays pure. The payout itself fires at this
    point, not when the pairings post (social-prompt-39).
    """
    heading = f"## {GAME_ICONS['compliment']} Spin the Compliment — Wrap-Up"
    body = delivered_line(delivered_count, total)
    content = f"{heading}\n{body}"
    if payout_line:
        content += f"\n-# {payout_line}"
    return [_text(content)]
