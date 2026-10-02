"""Pure Components V2 card builders for the casino.

The V2 replacement for this package's ``embeds.py`` (deleted with this
conversion — ``docs/plans/components_v2_migration.md`` wave 3a) for every
**game** card: daily comp, help, coinflip/slots/blackjack/roulette/derby/
baccarat/dice/war/keno rounds and results, mines, your-stats, the jackpot
celebration, the big-win broadcast, and the Pools panel/result/void cards.

The hub panel is deliberately **not** here. Main's own Components V2 hub
(``cogs.casino.hub_layout`` — a live board in the hub's frame, tables in a
"Pick a table…" dropdown) shipped ahead of this port and supersedes the
approved design's button-grid hub; this module keeps the hub's *supporting*
text (``TABLES``, ``casino_title``, ``ticker_line``, ``hub_sections``)
because ``hub_layout.py`` imports them, relocated here unchanged from
``embeds.py`` rather than converted.

Every game-card function here is **pure**: primitives and a ``name_fn`` in,
a list of ``discord.ui.Item`` out. No IO, no accent lookup, no callbacks —
the cog's ``LayoutView`` subclasses (and ``views.py``'s
``build_*_action_rows``) add the items in order and bind behaviour by
``custom_id`` or by a ``DynamicItem`` template. Each builder's docstring
names the paradigm it builds (``docs/components_v2_style_guide.md`` §
The paradigms).

Conventions the guide fixes and this module follows throughout:

* **A card is bare** — no ``Container``, no accent colour. A bare card has
  nowhere to put win/loss as colour, so every settled result instead leads
  with one outcome glyph on its heading — ✅ a win landed, ❌ the house
  kept it, ➖ a push or a refund — and a live/neutral card (a board still
  taking bets, a standoff) carries none. The same three glyphs mark an
  individual line inside a shared hand (blackjack, war, Mines) where more
  than one outcome can appear on one card.
* **Every card is one flowing Text Display** rather than a Section per old
  embed field: Discord's per-field 1024-char cap is gone for a layout (guide
  § What carries over unchanged) and these cards are receipts (paradigm 7)
  or live boards (paradigm 8) — "bare text" is the guide's own model for
  both, and one block costs one component against the 40-component budget
  instead of one per field. A field becomes a bold label line inside that
  block. This is a documented deviation from mirroring dm_perms's
  Section-per-row shape; see the ship commit's ``deviations``.
* **A member is named through ``name_fn``**, never as ``<@id>``: a mention in
  a Text Display renders from the *reading* client's cache, so it shows as a
  bare number to anyone who has not seen that member. Every game-card
  builder's parameter has no default so a caller cannot forget it;
  ``ticker_line``/``hub_sections`` keep the ``= mention`` default they had in
  ``embeds.py``, unchanged, since they are relocated hub support, not part
  of this conversion.
* Flavor copy stays name-agnostic: every guild can rename its casino from
  the Branding panel (``casino_name``/``DEFAULT_CASINO_NAME``), so nothing
  here hardcodes "meadow"/"honeypot"/other Golden-Meadow-specific imagery.
"""

from __future__ import annotations

import sqlite3

from typing import Any, NamedTuple

import discord

from bot_modules.core.branding import EMBED_TITLE_LIMIT, escape_to_fit
from bot_modules.core.layout import card_strips
from bot_modules.core.meters import mono
from bot_modules.economy.leaderboard import bar_fill
from bot_modules.casino import logic
from bot_modules.casino import pools_charts, pools_logic
from bot_modules.casino.service import CasinoSettings, MinesStep, comp_on
from bot_modules.economy.service import EconSettings
from bot_modules.core.branding_service import DEFAULT_CASINO_NAME, PanelArt
from bot_modules.shared.name_resolver import NameFn, mention

Item = discord.ui.Item[Any]


def _text(content: str) -> discord.ui.TextDisplay[Any]:
    return discord.ui.TextDisplay(content)


def _casino_title(prefix: str, casino_name: str, suffix: str = "") -> str:
    """Compose a heading around an admin-set, free-text ``casino_name``.

    ``casino_name`` comes from the dashboard Branding panel (admin-gated, but
    free text otherwise) so it is escaped like any other member/admin text
    reaching a rendered heading — then fitted to what the literal
    prefix/suffix leave of ``EMBED_TITLE_LIMIT``, since escaping can double
    its length. Nothing in Components V2 imposes a per-heading limit the way
    an embed title's 256 characters did, but that budget is still a
    conservative, already-battle-tested bound for a one-line heading, and
    reusing it is cheaper than deriving a new one (carried over from
    ``embeds.py``'s ``_casino_title``, the escape sweep's fix — casino-*
    completeness re-audit).
    """
    budget = EMBED_TITLE_LIMIT - len(prefix) - len(suffix)
    return prefix + escape_to_fit(casino_name, budget) + suffix


def casino_title(casino_name: str = DEFAULT_CASINO_NAME) -> str:
    """Hub-panel heading text for a guild's casino ("🎲 The Golden Meadow Casino")."""
    return _casino_title("🎲 The ", casino_name, " Casino")


# Kept for callers with no guild handy; the per-guild name is the norm.
CASINO_TITLE = casino_title()

#: Every table the hub's **Pick a table…** dropdown can offer, in menu order:
#: game → (emoji, label, one-line blurb). The blurb is the option's
#: description, so it stays inside Discord's 100 characters; the emoji is
#: also the floor ticker's (``_TICKER_EMOJI``). Relocated unchanged from
#: ``embeds.py`` — ``cogs.casino.hub_layout`` is this dict's only consumer.
TABLES: dict[str, tuple[str, str, str]] = {
    "coinflip": (
        "🪙", "Coinflip",
        "Call it in the air; a win pays "
        f"{logic.mult_text(logic.COINFLIP_MULT_NUM, logic.COINFLIP_MULT_DEN)}× "
        "your bet",
    ),
    "slots": ("🎰", "Slots", "Three spinning reels: pairs pay back, and sevens pay big"),
    "blackjack": ("🃏", "Blackjack", "Beat the dealer to 21; naturals pay 3:2"),
    "roulette": (
        "🎡", "Roulette",
        "Red, black, a dozen, or a number; stack your bets, then spin",
    ),
    "derby": ("🏇", "Derby", "Six critters, one finish line; back your favorite"),
    "baccarat": ("🎴", "Baccarat", "Player, Banker, or Tie; nearest to nine wins"),
    "dice": ("🎲", "Dice", "Three dice, one roll; call Big, Small, Odd, or Even"),
    "war": ("⚔️", "War", "One card each, high card wins; on a tie, go to war"),
    "keno": (
        "🔢", "Keno",
        "Pick your numbers; 20 are drawn, and the more you match, the more you win",
    ),
    "mines": ("💣", "Mines", "Open tiles, climb the multiplier, stop before the bang"),
}


def _coins(econ: EconSettings, n: int) -> str:
    unit = econ.currency_name if n == 1 else econ.currency_plural
    return f"{econ.currency_emoji} **{n:,}** {unit}"


def _streak_line(econ: EconSettings, streak: int) -> str | None:
    """The 🔥/🧊 callout once a run reaches the threshold, else None."""
    if streak >= logic.STREAK_CALLOUT_AT:
        return f"🔥 **{streak} wins in a row!**"
    if streak <= -logic.STREAK_CALLOUT_AT:
        return f"🧊 {abs(streak)} losses in a row — the house is merciless."
    return None


def _with_streak(desc: str, econ: EconSettings, streak: int) -> str:
    line = _streak_line(econ, streak)
    return f"{desc}\n{line}" if line else desc


def _pot_line(pot_after: int) -> str:
    """Every loss is a tiny ad for the jackpot."""
    return f"💰 The loss feeds the jackpot — now **{pot_after:,}**."


class CardResult(NamedTuple):
    """One built card, plus its own recap text for the big-win broadcast to
    copy rather than re-render (``build_big_win_broadcast_layout``)."""

    items: list[Item]
    recap: str


def _result_block(
    econ: EconSettings,
    paid: list[tuple[int, str, int, int]],
    losers_total: int,
    pot_after: int,
    *,
    paid_name: str = "Winners",
    keep_name: str = "The house keeps",
    name_fn: NameFn,
) -> str:
    """Every result card's shared tail: the paid board (💥 big-win prefix)
    and the house-keeps line with its pot ad. One implementation so a fix
    can never land in one game's recap and miss another's."""
    parts = []
    if paid:
        lines = [
            f"{'💥 ' if logic.is_big_win(amount, payout) else ''}"
            f"{name_fn(uid)} — {d} · {_coins(econ, amount)} → {_coins(econ, payout)}"
            for uid, d, amount, payout in paid
        ]
        parts.append(
            f"**{paid_name}**\n" + "\n".join(logic.cap_lines(lines, limit=1500))
        )
    if losers_total:
        kept = _coins(econ, losers_total)
        if pot_after > 0:
            kept += f"\n{_pot_line(pot_after)}"
        parts.append(f"**{keep_name}**\n{kept}")
    return "\n\n".join(parts)


# Each ticker row leads with its table's emoji — the same glyph the table
# dropdown uses, taken from that table rather than kept as a second copy: a
# hand-maintained map stopped at the original four instant games and left
# Mines, roulette, derby, baccarat and keno all rendering as the Dice 🎲
# (casino-140). Relocated unchanged from ``embeds.py``.
_TICKER_EMOJI = {game: emoji for game, (emoji, _label, _blurb) in TABLES.items()}


def ticker_line(
    user_id: int, game: str, stake: int, payout: int, *, name_fn: NameFn = mention
) -> str:
    """One compact floor-ticker entry, naming the player as plain text.

    Relocated unchanged from ``embeds.py`` — hub support, not part of this
    conversion, so it keeps the ``= mention`` default the game-card builders
    below deliberately don't.
    """
    if payout > stake:
        result = f"**{payout:,}**"
    elif payout == stake and payout > 0:
        result = "push"
    elif payout > 0:
        result = f"{payout:,} back"
    else:
        result = "the house"
    return f"{_TICKER_EMOJI.get(game, '🎲')} {name_fn(user_id)} · {stake:,} → {result}"


def hub_sections(
    econ: EconSettings,
    settings: CasinoSettings,
    *,
    jackpot: int | None = None,
    ticker: list[tuple[int, str, int, int]] | None = None,
    standings: tuple[tuple[int, int] | None, tuple[int, int] | None]
    | None = None,
    name_fn: NameFn = mention,
) -> list[tuple[str, str]]:
    """The hub panel's live board as (heading, text) pairs, in order.

    Relocated unchanged from ``embeds.py``: pure text, and
    ``cogs.casino.hub_layout`` — main's own Components V2 hub, out of this
    conversion's scope — is its only caller and does the actual laying out.
    A section with nothing to say is left out rather than shown empty — no
    jackpot while slots or the pot is off, no floor before anyone has
    played. House Rules is always there.
    """
    sections: list[tuple[str, str]] = []
    if jackpot is not None:
        sections.append((
            "💰 Progressive Jackpot",
            f"The pot stands at {_coins(econ, jackpot)}. Every lost bet adds "
            "to it, and triple 7️⃣ on the slots wins all of it.",
        ))
    if ticker:
        sections.append((
            "📡 On the Floor",
            "\n".join(
                ticker_line(uid, game, stake, payout, name_fn=name_fn)
                for uid, game, stake, payout in ticker
            ),
        ))
    # Only the day's biggest winner is crowned. ``standings`` still carries
    # the biggest loser (daily_standings computes both), but the hub is a
    # public panel and naming someone at −17,495 there is a pillory, not
    # the harm-reduction control the plans imagined (casino-137, D6). A
    # member's own losses are theirs to read on 📊 My Stats.
    if standings is not None and standings[0] is not None:
        earner = standings[0]
        sections.append((
            "📊 Today at the Tables",
            f"📈 Furthest ahead: {name_fn(earner[0])} · "
            f"**+{earner[1]:,}** {econ.currency_plural}",
        ))
    limits = [f"Bets: **{settings.min_bet:,}**–**{settings.max_bet:,}**"
              if settings.max_bet else f"Bets: **{settings.min_bet:,}**+"]
    if settings.daily_wager_cap:
        limits.append(
            f"Daily table limit: **{settings.daily_wager_cap:,}** "
            f"{econ.currency_plural} staked per player"
        )
    if comp_on(settings):
        limits.append(
            f"🎁 Daily comp: a free **{settings.daily_comp:,}**-"
            f"{econ.currency_name} slots spin, once a day"
        )
    sections.append(("House Rules", " · ".join(limits)))
    return sections


def build_comp_layout(
    econ: EconSettings,
    user_id: int,
    reels: tuple[str, str, str],
    amount: int,
    payout: int,
    label: str | None,
    *,
    casino_name: str = DEFAULT_CASINO_NAME,
    name_fn: NameFn,
) -> list[Item]:
    """The 🎁 Daily Comp result. **Paradigm 7 — receipt.** A win is a win
    (✅); a blank spin cost nothing, so it gets the push glyph rather than
    the loss one."""
    reel_line = _reel_row(reels)
    title = _casino_title("🎁 ", casino_name, " Daily Comp")
    if payout > 0:
        body = (
            f"## ✅ {title}\n{reel_line}\n\n{label} The house spotted "
            f"{name_fn(user_id)} a free {_coins(econ, amount)} spin and it "
            f"paid {_coins(econ, payout)}."
        )
    else:
        body = (
            f"## ➖ {title}\n{reel_line}\n\nThe house spotted {name_fn(user_id)} "
            f"a free {_coins(econ, amount)} spin — no luck this time, and it "
            "cost nothing. There's another tomorrow."
        )
    return [_text(body)]


def build_help_layout(
    econ: EconSettings,
    settings: CasinoSettings,
    *,
    casino_name: str = DEFAULT_CASINO_NAME,
) -> list[Item]:
    """One block per OPEN table — a closed table's odds are not on offer.
    **Paradigm 7 — receipt** (an ephemeral, control-free explainer)."""
    parts = [
        f"## {_casino_title('❓ How the ', casino_name, ' pays')}\n"
        "Payouts below are **total return** on your bet — a 2× win on a "
        "10-bet hands back 20. The house keeps a small edge on every "
        "table; that's what makes it a casino."
    ]
    if settings.coinflip_enabled:
        parts.append(
            "**🪙 Coinflip**\nCall heads or tails. Win: "
            f"**{logic.mult_text(logic.COINFLIP_MULT_NUM, logic.COINFLIP_MULT_DEN)}×** "
            f"({logic.COINFLIP_RTP_PCT:g}% return)."
        )
    if settings.slots_enabled:
        triples = " · ".join(
            f"{sym}{sym}{sym} **{mult}×**"
            for sym, mult in logic.SLOT_TRIPLE_PAYOUT.items()
        )
        parts.append(
            f"**🎰 Slots**\n{triples}\n"
            f"Two 7️⃣ **{logic.SLOT_TWO_SEVENS_MULT}×** · any pair "
            f"**{logic.mult_text(logic.SLOT_PAIR_NUM, logic.SLOT_PAIR_DEN)}×** "
            f"(~{logic.SLOTS_RTP_PCT:g}% return)"
        )
    if settings.blackjack_enabled:
        parts.append(
            "**🃏 Blackjack**\nDealer stands on all 17s. Blackjack pays "
            "**3:2**, wins pay **2×**, pushes return your bet. Double down "
            "on your first two cards. Idle hands stand automatically."
        )
    if settings.roulette_enabled:
        parts.append(
            "**🎡 Roulette**\nEuropean wheel, one zero. Red/black **2×** · "
            "dozens **3×** · straight numbers **36×** (~97% return). Your "
            "own private wheel: stack as many bets as you like, then press "
            "**Spin**."
        )
    if settings.derby_enabled:
        odds = " · ".join(
            f"{r.emoji} **{logic.derby_odds_label(i)}**"
            for i, r in enumerate(logic.DERBY_FIELD)
        )
        parts.append(
            f"**🏇 Derby**\n{odds}\n"
            "Back a critter to win — the favorite pays least, the snail "
            "pays big (91–96% return by runner). Back as many runners as "
            "you like in your own private race, then press **Race**."
        )
    if settings.baccarat_enabled:
        parts.append(
            "**🎴 Baccarat**\nBack the **Player** or **Banker** — both pay "
            "**2×** (~99% return, the best odds in the house; a Banker win "
            "on a three-card 7 pushes instead). **Tie** pays **9×** — the "
            "long shot (~86% return). Ties push the side bets. Your own "
            "private coup — take your positions, then press **Deal**."
        )
    if settings.dice_enabled:
        parts.append(
            "**🎲 Dice**\nThree dice, one roll. **Big** (11–17), **Small** "
            "(4–10), **Odd**, **Even** — all pay **2×**, and any triple "
            "sweeps the table (~97% return). Your own private roll — make "
            "your calls, then press **Roll**."
        )
    if settings.war_enabled:
        parts.append(
            "**⚔️ War**\nOne card each, aces high — the higher card pays "
            "**2×** on the spot. A tie is a standoff: **go to war** (match "
            "your bet; win *or tie* the next card for **3×** your original) "
            "or **retreat** (half your bet back). ~97% return going to war "
            "— always the braver *and* the better play."
        )
    if settings.keno_enabled:
        pays = " · ".join(
            f"Pick-{tier} up to **{max(table.values())}×**"
            for tier, table in logic.KENO_PAYTABLE.items()
        )
        parts.append(
            f"**🔢 Keno**\nGrab a quick-pick ticket of 4, 6, 8, or 10 "
            f"numbers; 20 of 80 drop in your own private draw. {pays} — "
            "pays scale with how many of yours hit (~95% return, tuned far "
            "kinder than real keno). Buy your tickets, then press **Draw**."
        )
    if settings.mines_enabled:
        ladders = "\n".join(
            f"· **{bombs} bomb{'' if bombs == 1 else 's'}** — "
            f"{logic.mines_top_rung(bombs)} tiles to "
            f"{logic.mines_mult_label(logic.mines_ladder(bombs)[-1])}"
            for bombs in logic.MINES_BOMB_CHOICES
        )
        parts.append(
            f"**💣 Mines**\n{logic.MINES_TILES} tiles, and you pick the "
            f"danger:\n{ladders}\n"
            "Every safe tile lifts your multiplier and **Cash Out** banks "
            "it — the button shows exactly what you would walk away with. "
            "Every ladder tops out around the same place, so the choice is "
            "how long the road is. Every stopping point returns the same "
            f"~{logic.MINES_RTP_PCT:.0f}%, so there is no wrong moment to "
            "stop, and if you wander off the house cashes you out where "
            "you stood."
        )
    if settings.daily_wager_cap:
        parts.append(
            "**Daily Limit**\n"
            f"You can stake up to **{settings.daily_wager_cap:,}** "
            f"{econ.currency_plural} per day across all tables."
        )
    return [_text("\n\n".join(parts))]


def build_coinflip_layout(
    econ: EconSettings,
    user_id: int,
    call: str,
    landed: str,
    stake: int,
    payout: int,
    *,
    streak: int = 0,
    pot_after: int = 0,
    name_fn: NameFn,
) -> CardResult:
    """The coinflip result. **Paradigm 9 — game turn card**, settled."""
    won = payout > 0
    face = "🌞" if landed == "heads" else "🌙"
    glyph = "✅" if won else "❌"
    desc = (
        f"{name_fn(user_id)} called **{call}** for {_coins(econ, stake)}.\n"
        + (
            f"The coin agrees — they collect {_coins(econ, payout)}."
            if won else "The coin does not care. The house keeps the bet."
        )
    )
    if not won and pot_after > 0:
        desc += f"\n{_pot_line(pot_after)}"
    body = f"## {glyph} Coinflip — {landed}! {face}\n" + _with_streak(
        desc, econ, streak
    )
    return CardResult([_text(body)], body)


def _reel_row(cells: tuple[str, ...]) -> str:
    """The slots reel row, deliberately unframed — see the removed-box
    rationale in the original embeds.py history (2026-08-16): the reel
    emoji's client-dependent width never let a text-art frame stay aligned,
    and a code span strips their colour."""
    return f"▶ {cells[0]} │ {cells[1]} │ {cells[2]} ◀"


def build_slots_layout(
    econ: EconSettings,
    user_id: int,
    reels: tuple[str, str, str],
    stake: int,
    payout: int,
    label: str | None,
    *,
    jackpot_won: int = 0,
    streak: int = 0,
    pot_after: int = 0,
    casino_name: str = DEFAULT_CASINO_NAME,
    name_fn: NameFn,
) -> CardResult:
    """The slots result. **Paradigm 9 — game turn card**, settled."""
    reel_line = _reel_row(reels)
    title = _casino_title("", casino_name, " Slots")
    if payout > 0:
        desc = (
            f"{reel_line}\n\n{label} {name_fn(user_id)} bet "
            f"{_coins(econ, stake)} and collects {_coins(econ, payout)}."
        )
        if jackpot_won:
            title = "The Jackpot Spills"
            desc += "\nThe whole progressive pot — gone in one spin."
        heading = f"## ✅ 🎰 {title}"
    else:
        desc = (
            f"{reel_line}\n\n{name_fn(user_id)}'s {_coins(econ, stake)} "
            "scatters into the house's take."
        )
        if pot_after > 0:
            desc += f"\n{_pot_line(pot_after)}"
        heading = f"## ❌ 🎰 {title}"
    body = f"{heading}\n" + _with_streak(desc, econ, streak)
    return CardResult([_text(body)], body)


def build_jackpot_celebration_layout(
    econ: EconSettings,
    user_id: int,
    amount: int,
    *,
    casino_name: str = DEFAULT_CASINO_NAME,
    name_fn: NameFn,
    art: PanelArt | None = None,
) -> list[Item]:
    """The standalone fanfare posted beside a jackpot result. **Paradigm 7 —
    receipt.** ``art`` frames it in the guild's thin card strips
    (``core.layout.card_strips``); the player's own result card stays plain."""
    top, bottom = card_strips(art)
    return [
        *top,
        _text(
            f"## {_casino_title('🏆 Jackpot at the ', casino_name, ' 🏆')}\n"
            f"💰 7️⃣ 7️⃣ 7️⃣ 💰\n\n{name_fn(user_id)} just hit the progressive "
            f"jackpot for {_coins(econ, amount)}!\n"
            "The pot reseeds — every lost bet grows the next one."
        ),
        *bottom,
    ]


class BigWinBroadcast(NamedTuple):
    items: list[Item]
    ping: bool  # send with @here, allowed_mentions=everyone


def build_big_win_broadcast_layout(
    recap: str,
    *,
    payout: int,
    threshold: int,
    stake: int,
    min_mult: int,
    game_label: str,
    top_pct_payout: int | None = None,
    ping_enabled: bool = True,
    winner_name: str | None = None,
    winner_icon: str | None = None,
    art: PanelArt | None = None,
) -> BigWinBroadcast | None:
    """The public big-win card, or None when this win stays private.

    ``recap`` is the player's own result card's stitched text
    (``CardResult.recap``) — copied rather than re-rendered, same rationale
    the embed version had: the player's card and this one must not be the
    same mutable object, or editing one would rewrite the other. ``winner``
    takes the ``Section`` + ``Thumbnail`` slot the embed's ``set_author``
    held; a card with no winner (a communal round with no single presser)
    stays plain text.

    ``art`` frames the card in the guild's thin card strips
    (``core.layout.card_strips``). A pinging card's ``@here`` line is put in
    front by the caller, so it sits above the strip, not inside the frame.
    """
    tier = logic.big_win_tier(
        payout, threshold, stake=stake, min_mult=min_mult,
        top_pct_payout=top_pct_payout, ping_enabled=ping_enabled,
    )
    if tier is None:
        return None
    body = recap
    if tier.lead:
        body = f"{tier.lead}\n\n{body}" if body else tier.lead
    text = f"## {tier.header} — {game_label}\n{body}" if body else (
        f"## {tier.header} — {game_label}"
    )
    # winner_name and winner_icon are always both-or-neither at the only
    # call site (both derive from the same `winner` presence check), so
    # there is no name-without-icon shape to render.
    if winner_name and winner_icon:
        items: list[Item] = [
            discord.ui.Section(
                _text(text), accessory=discord.ui.Thumbnail(winner_icon)
            )
        ]
    else:
        items = [_text(text)]
    top, bottom = card_strips(art)
    return BigWinBroadcast([*top, *items, *bottom], tier.ping)


# ── animation frames (big bets get the show; money is already settled) ─


def build_coinflip_spin_layout(
    econ: EconSettings, user_id: int, call: str, stake: int, *, name_fn: NameFn
) -> list[Item]:
    return [
        _text(
            "## 🪙 Coinflip — It's in the Air!\n"
            f"{name_fn(user_id)} calls **{call}** for {_coins(econ, stake)}…\n"
            "The coin spins high in the air. 🪙"
        )
    ]


def build_slots_spin_layout(
    econ: EconSettings,
    user_id: int,
    stake: int,
    revealed: tuple[str | None, str | None, str | None],
    *,
    casino_name: str = DEFAULT_CASINO_NAME,
    name_fn: NameFn,
) -> list[Item]:
    cells = tuple(sym if sym is not None else "🌀" for sym in revealed)
    return [
        _text(
            f"## {_casino_title('🎰 ', casino_name, ' Slots')}\n"
            f"{_reel_row(cells)}\n\n{name_fn(user_id)} bet "
            f"{_coins(econ, stake)} — the reels are spinning…"
        )
    ]


def build_blackjack_reveal_layout(
    econ: EconSettings,
    user_id: int,
    player: list[str],
    dealer_first_two: list[str],
    stake: int,
    *,
    doubled: bool = False,
    name_fn: NameFn,
) -> list[Item]:
    stake_note = f" (doubled to {stake:,})" if doubled else ""
    body = (
        f"## 🃏 Blackjack\n"
        f"{name_fn(user_id)} is in for {_coins(econ, stake)}{stake_note}\n\n"
        f"**Their Hand**\n{_hand_line(player)}\n\n"
        f"**Dealer**\n{_hand_line(dealer_first_two)}\n"
        "*The dealer turns the hole card…*"
    )
    return [_text(body)]


def build_roulette_bounce_layout(bounce: tuple[int, int]) -> list[Item]:
    frames = " … ".join(
        f"{_COLOR_DOTS[logic.wheel_color(n)]} {n}" for n in bounce
    )
    return [
        _text(f"## 🎡 Roulette — No More Bets!\nThe ball dances across the wheel… {frames} …")
    ]


_OUTCOME_LINES = {
    "blackjack": "**Blackjack!** Paid 3:2 —",
    "win": "**They beat the dealer** —",
    "push": "**Push.** The bet comes home —",
    "lose": "The dealer takes it.",
    "bust": "**Bust.** The dealer takes it.",
    "refunded": "The table was reset — the bet came home.",
}

_OUTCOME_GLYPH = {
    "blackjack": "✅", "win": "✅",
    "push": "➖", "refunded": "➖",
    "lose": "❌", "bust": "❌",
}


def _hand_line(cards: list[str], *, hide_hole: bool = False) -> str:
    if hide_hole and len(cards) >= 2:
        shown = [cards[0]] + ["🂠"] * (len(cards) - 1)
        return f"`{'  '.join(shown)}`"
    return f"`{'  '.join(cards)}`  ({logic.hand_value(cards)})"


def build_blackjack_layout(
    econ: EconSettings,
    user_id: int,
    player: list[str],
    dealer: list[str],
    stake: int,
    *,
    doubled: bool = False,
    outcome: str | None = None,
    payout: int = 0,
    streak: int = 0,
    pot_after: int = 0,
    name_fn: NameFn,
) -> CardResult:
    """One blackjack hand, live or settled. **Paradigm 9 — game turn
    card.**"""
    live = outcome is None
    stake_note = f" (doubled to {stake:,})" if doubled else ""
    parts = [
        f"## 🃏 Blackjack\n"
        f"{name_fn(user_id)} is in for {_coins(econ, stake)}{stake_note}",
        f"**Their Hand**\n{_hand_line(player)}",
        f"**Dealer**\n{_hand_line(dealer, hide_hole=live)}",
    ]
    if not live:
        glyph = _OUTCOME_GLYPH.get(outcome or "", "❌")
        line = f"{glyph} {_OUTCOME_LINES.get(outcome or '', '')}"
        if payout > 0:
            line = f"{line} {_coins(econ, payout)}."
        if payout == 0 and pot_after > 0:
            line = f"{line}\n{_pot_line(pot_after)}"
        if outcome != "refunded":
            line = _with_streak(line, econ, streak)
        parts.append(f"**Result**\n{line}")
    body = "\n\n".join(parts)
    return CardResult([_text(body)], body)


def _bets_block(
    econ: EconSettings, bets: list[tuple[int, str, int]], *, name_fn: NameFn
) -> str:
    """The live bets board, newest first."""
    if not bets:
        # A private board has one player, so there is nobody to be first
        # ahead of (casino-132).
        return "**Bets**\n*No bets yet.*"
    lines = [
        f"{name_fn(uid)} — {desc} · {_coins(econ, amount)}"
        for uid, desc, amount in reversed(bets)
    ]
    return f"**Bets ({len(bets)})**\n" + "\n".join(
        logic.cap_lines(lines, limit=1500, more_label="earlier bet(s)")
    )


def build_roulette_round_layout(
    econ: EconSettings,
    closes_at: float,
    bets: list[tuple[int, str, int]],
    *,
    name_fn: NameFn,
) -> list[Item]:
    """``bets`` = (user_id, bet description, amount), placement order.
    **Paradigm 8 — live board.**"""
    body = (
        "## 🎡 Roulette — Your Wheel\n"
        + _solo_board_lead("wheel", "🎡 Spin", "spins itself", closes_at)
        + " Pick a color, a dozen, or go all-in on a single number.\n\n"
        + _bets_block(econ, bets, name_fn=name_fn)
    )
    return [_text(body)]


def _solo_board_lead(thing: str, button: str, auto: str, closes_at: float) -> str:
    """The opening line every private-round board shares (casino-132)."""
    return (
        f"Your own {thing}. Stack bets, then press {button}. "
        f"Left alone it {auto} <t:{int(closes_at)}:R>."
    )


_COLOR_DOTS = {"red": "🔴", "black": "⚫", "green": "🟢"}


def build_roulette_result_layout(
    econ: EconSettings,
    result: int,
    bets: list[tuple[int, str, int, int]],
    *,
    pot_after: int = 0,
    name_fn: NameFn,
) -> CardResult:
    """``bets`` = (user_id, bet description, amount, payout). **Paradigm 7 —
    receipt.**"""
    color_name = logic.wheel_color(result)
    dot = _COLOR_DOTS[color_name]
    winners = [b for b in bets if b[3] > 0]
    losers_total = sum(b[2] for b in bets if b[3] == 0)
    glyph = "✅" if winners else "❌"
    if bets:
        lead = f"The ball lands on {dot} **{result}**."
    else:
        lead = (
            f"The ball lands on {dot} **{result}** — but nobody bet. "
            "The wheel spins for no one."
        )
    parts = [f"## {glyph} Roulette — No More Bets!\n{lead}"]
    block = _result_block(
        econ, winners, losers_total, pot_after, name_fn=name_fn
    )
    if block:
        parts.append(block)
    body = "\n\n".join(parts)
    return CardResult([_text(body)], body)


# ── derby ────────────────────────────────────────────────────────────────


def _odds_board() -> str:
    return "\n".join(
        f"{logic.describe_runner(i)} — pays **{logic.derby_odds_label(i)}**"
        for i in range(len(logic.DERBY_FIELD))
    )


def build_derby_round_layout(
    econ: EconSettings,
    closes_at: float,
    bets: list[tuple[int, str, int]],
    *,
    name_fn: NameFn,
) -> list[Item]:
    """``bets`` = (user_id, runner description, amount). **Paradigm 8 —
    live board.**"""
    body = (
        "## 🏇 Meadow Derby — Your Race\n"
        + _solo_board_lead("race", "🏇 Race", "runs itself", closes_at)
        + " Back a critter — payouts are total return on your bet.\n\n"
        + f"**The Field**\n{_odds_board()}\n\n"
        + _bets_block(econ, bets, name_fn=name_fn)
    )
    return [_text(body)]


def _track_lines(positions: list[int]) -> str:
    """One line per runner, racing right-to-left toward the flag."""
    return "\n".join(
        f"🏁{'┄' * (logic.DERBY_TRACK_LEN - pos)}{runner.emoji}"
        for runner, pos in zip(logic.DERBY_FIELD, positions)
    )


def build_derby_race_layout(positions: list[int]) -> list[Item]:
    return [_text(f"## 🏇 Meadow Derby — And They're Off!\n{_track_lines(positions)}")]


def build_derby_result_layout(
    econ: EconSettings,
    winner: int,
    final_positions: list[int],
    bets: list[tuple[int, str, int, int]],
    *,
    pot_after: int = 0,
    name_fn: NameFn,
) -> CardResult:
    """``bets`` = (user_id, runner description, amount, payout). **Paradigm
    7 — receipt.**"""
    winners = [b for b in bets if b[3] > 0]
    losers_total = sum(b[2] for b in bets if b[3] == 0)
    glyph = "✅" if winners else "❌"
    lead = (
        f"{_track_lines(final_positions)}\n\n"
        f"**{logic.describe_runner(winner)}** takes it!"
    )
    if not bets:
        lead += " Nobody bet — the critters race for the glory alone."
    parts = [f"## {glyph} Meadow Derby — Photo Finish!\n{lead}"]
    block = _result_block(
        econ, winners, losers_total, pot_after,
        keep_name="The meadow keeps", name_fn=name_fn,
    )
    if block:
        parts.append(block)
    body = "\n\n".join(parts)
    return CardResult([_text(body)], body)


# ── baccarat ────────────────────────────────────────────────────────────


def _baccarat_hand_line(cards: list[str], *, reveal: int | None = None) -> str:
    if reveal is not None and reveal < len(cards):
        shown = cards[:reveal] + ["🂠"] * (len(cards) - reveal)
        return f"`{'  '.join(shown)}`"
    return f"`{'  '.join(cards)}`  ({logic.baccarat_total(cards)})"


def build_baccarat_round_layout(
    econ: EconSettings,
    closes_at: float,
    bets: list[tuple[int, str, int]],
    *,
    name_fn: NameFn,
) -> list[Item]:
    """**Paradigm 8 — live board.**"""
    body = (
        "## 🎴 Baccarat — Your Coup\n"
        + _solo_board_lead("shoe", "🎴 Deal", "deals itself", closes_at)
        + " Back the Player, the Banker, or the long-shot Tie — nearest to "
        "nine wins.\n\n"
        + _bets_block(econ, bets, name_fn=name_fn)
    )
    return [_text(body)]


def build_baccarat_deal_layout(player: list[str], banker: list[str]) -> list[Item]:
    """The dealing frame: both starting hands down, draws still to come."""
    return [
        _text(
            "## 🎴 Baccarat — No More Bets!\n"
            f"🔵 Player  {_baccarat_hand_line(player, reveal=2)}\n"
            f"🔴 Banker  {_baccarat_hand_line(banker, reveal=2)}\n"
            "The cards hit the felt…"
        )
    ]


_BACCARAT_VERDICTS = {
    "player": "🔵 **Player wins.**",
    "banker": "🔴 **Banker wins.**",
    "tie": "🟡 **A tie!** Side bets come home; Tie pays 9×.",
}


def build_baccarat_result_layout(
    econ: EconSettings,
    player: list[str],
    banker: list[str],
    bets: list[tuple[int, str, int, int]],
    *,
    pot_after: int = 0,
    name_fn: NameFn,
) -> CardResult:
    """**Paradigm 7 — receipt.**"""
    winner = logic.baccarat_winner(player, banker)
    verdict = _BACCARAT_VERDICTS[winner]
    if (
        winner == "banker" and len(banker) == 3
        and logic.baccarat_total(banker) == 7
    ):
        verdict += " A three-card seven — Banker bets push."
    hands = (
        f"🔵 Player  {_baccarat_hand_line(player)}\n"
        f"🔴 Banker  {_baccarat_hand_line(banker)}"
    )
    lead = f"{hands}\n{verdict}"
    if not bets:
        lead += " Nobody bet — the cards fall for no one."
    won = [b for b in bets if b[3] > b[2]]
    paid = [b for b in bets if b[3] > 0]
    losers_total = sum(b[2] for b in bets if b[3] == 0)
    # A three-card-seven Banker win pushes every Banker bet (paid back in
    # full, nothing won) rather than losing it — ➖ matches War's retreat/
    # refund glyph for the same "nobody lost, nobody won" shape; ❌ is
    # reserved for a bet that actually lost (nothing paid at all).
    glyph = "✅" if won else "➖" if paid else "❌"
    parts = [f"## {glyph} Baccarat — Cards Down!\n{lead}"]
    block = _result_block(
        econ, paid, losers_total, pot_after,
        paid_name="Winners" if won else "Pushed", name_fn=name_fn,
    )
    if block:
        parts.append(block)
    body = "\n\n".join(parts)
    return CardResult([_text(body)], body)


# ── dice / sic bo ───────────────────────────────────────────────────────


def build_dice_round_layout(
    econ: EconSettings,
    closes_at: float,
    bets: list[tuple[int, str, int]],
    *,
    name_fn: NameFn,
) -> list[Item]:
    """**Paradigm 8 — live board.**"""
    body = (
        "## 🎲 Dice — Your Roll\n"
        + _solo_board_lead("table", "🎲 Roll", "rolls itself", closes_at)
        + " Call Big, Small, Odd, or Even — but any triple sweeps the "
        "table.\n\n"
        + _bets_block(econ, bets, name_fn=name_fn)
    )
    return [_text(body)]


def build_dice_tumble_layout() -> list[Item]:
    """The rolling frame — dice still in the air."""
    return [
        _text("## 🎲 Dice — No More Bets!\nThe dice tumble across the felt… 🎲 🎲 🎲 …")
    ]


def build_dice_result_layout(
    econ: EconSettings,
    dice: tuple[int, int, int],
    bets: list[tuple[int, str, int, int]],
    *,
    pot_after: int = 0,
    name_fn: NameFn,
) -> CardResult:
    """**Paradigm 7 — receipt.**"""
    total = sum(dice)
    is_triple = dice[0] == dice[1] == dice[2]
    call = "Big" if total >= 11 else "Small"
    parity = "odd" if total % 2 else "even"
    verdict = f"{logic.dice_faces(dice)} — **{total}**, {call} and {parity}."
    if is_triple:
        verdict = (
            f"{logic.dice_faces(dice)} — **a triple {dice[0]}!** The house "
            "sweeps every bet."
        )
    lead = verdict if bets else f"{verdict} Nobody bet — the dice roll for no one."
    winners = [b for b in bets if b[3] > 0]
    losers_total = sum(b[2] for b in bets if b[3] == 0)
    glyph = "✅" if winners else "❌"
    parts = [f"## {glyph} Dice — No More Bets!\n{lead}"]
    block = _result_block(econ, winners, losers_total, pot_after, name_fn=name_fn)
    if block:
        parts.append(block)
    body = "\n\n".join(parts)
    return CardResult([_text(body)], body)


# ── keno ─────────────────────────────────────────────────────────────────


def build_keno_round_layout(
    econ: EconSettings,
    closes_at: float,
    bets: list[tuple[int, str, int]],
    *,
    name_fn: NameFn,
) -> list[Item]:
    """**Paradigm 8 — live board.**"""
    body = (
        "## 🔢 Keno — Your Draw\n"
        + _solo_board_lead("draw", "🔢 Draw", "draws itself", closes_at)
        + " Pick a tier — the house quick-picks your numbers, fate does "
        "the rest.\n\n"
        + _bets_block(econ, bets, name_fn=name_fn)
    )
    return [_text(body)]


def build_keno_tumble_layout() -> list[Item]:
    """The drawing frame — balls still in the hopper."""
    return [
        _text(
            "## 🔢 Keno — No More Tickets!\nThe hopper churns… numbers "
            "rattling into the chute…"
        )
    ]


def _keno_board(drawn: list[int]) -> str:
    """The 20 drawn numbers as two monospace rows of ten."""
    cells = [f"{n:>2}" for n in drawn]
    return f"`{'  '.join(cells[:10])}`\n`{'  '.join(cells[10:])}`"


def build_keno_result_layout(
    econ: EconSettings,
    drawn: list[int],
    bets: list[tuple[int, str, int, int]],
    *,
    pot_after: int = 0,
    name_fn: NameFn,
) -> CardResult:
    """**Paradigm 7 — receipt.**"""
    if bets:
        lead = _keno_board(drawn)
    else:
        lead = (
            f"{_keno_board(drawn)}\nNobody held a ticket — twenty numbers "
            "fall for no one."
        )
    winners = [b for b in bets if b[3] > 0]
    losers = [b for b in bets if b[3] == 0]
    losers_total = sum(b[2] for b in losers)
    glyph = "✅" if winners else "❌"
    parts = [f"## {glyph} Keno — The Draw Is In!\n{lead}"]
    block = _result_block(econ, winners, losers_total, pot_after, name_fn=name_fn)
    if block:
        parts.append(block)
    # Keno itemises its losers — a loss on every other table is
    # self-evident, but a ticket that caught 3 of 10 looks identical to one
    # the house forgot to pay, so the unpaid lines carry their catch counts.
    if losers:
        lines = [
            f"{name_fn(uid)} — {d} · {_coins(econ, amount)}"
            for uid, d, amount, _ in losers
        ]
        parts.append(
            "**No payout**\n" + "\n".join(logic.cap_lines(lines, limit=1500))
        )
    body = "\n\n".join(parts)
    return CardResult([_text(body)], body)


# ── war ──────────────────────────────────────────────────────────────────

_WAR_OUTCOME_LINES = {
    "win": "**High card!**",
    "lose": "The dealer's card stands taller.",
    "war_win": "**Victory on the battlefield!**",
    "war_lose": "The war is lost — both stakes fall.",
    "retreat": "A tactical retreat — half the bet comes home:",
    "refunded": "The table was reset — the bet came home.",
}

_WAR_GLYPH = {
    "win": "✅", "war_win": "✅",
    "retreat": "➖", "refunded": "➖",
    "lose": "❌", "war_lose": "❌",
}


def build_war_layout(
    econ: EconSettings,
    user_id: int,
    player: str,
    dealer: str,
    stake: int,
    *,
    war_player: str | None = None,
    war_dealer: str | None = None,
    outcome: str | None = None,
    payout: int = 0,
    streak: int = 0,
    pot_after: int = 0,
    name_fn: NameFn,
) -> CardResult:
    """One war play — instant verdict, the tie standoff, or its resolution.
    **Paradigm 9 — game turn card.**"""
    live = outcome is None
    lines = [
        f"{name_fn(user_id)} is in for {_coins(econ, stake)}",
        f"Their card `{player}` · Dealer `{dealer}`",
    ]
    if war_player is not None and war_dealer is not None:
        lines.append(f"⚔️ War cards — theirs `{war_player}` · dealer `{war_dealer}`")
    heading = "## ⚔️ Casino War" if live else (
        f"## {_WAR_GLYPH.get(outcome or '', '❌')} Casino War"
    )
    parts = [heading + "\n" + "\n".join(lines)]
    if live:
        parts.append(
            "**A Standoff!**\nMatched cards. **Go to War** doubles your "
            "stake — win *or tie* the next card and take 3× your original "
            "bet — or **Retreat** with half. Fortune favors the bold (and "
            "so do the odds)."
        )
    else:
        line = _WAR_OUTCOME_LINES.get(outcome or "", "")
        if payout > 0:
            line = f"{line} {_coins(econ, payout)}."
        if payout == 0 and pot_after > 0:
            line = f"{line}\n{_pot_line(pot_after)}"
        if outcome != "refunded":
            line = _with_streak(line, econ, streak)
        parts.append(f"**Result**\n{line}")
    body = "\n\n".join(parts)
    return CardResult([_text(body)], body)


def build_my_stats_layout(
    econ: EconSettings,
    stats: sqlite3.Row | None,
    used: int,
    cap: int,
    reset_ts: float,
    *,
    comp_amount: int = 0,
    comp_claimed: bool = False,
) -> list[Item]:
    """The hub's 📊 My Stats ephemeral. **Paradigm 7 — receipt.**"""
    parts = ["## 📊 Your Night at the Tables"]
    if stats is not None and int(stats["plays"]) > 0:
        wagered = int(stats["wagered"])
        returned = int(stats["returned"])
        net = returned - wagered
        streak = int(stats["streak"])
        lines = [
            f"Wagered {_coins(econ, wagered)} · returned {_coins(econ, returned)}",
            f"Net: **{'+' if net >= 0 else '−'}{abs(net):,}** over "
            f"{int(stats['plays']):,} plays",
        ]
        if int(stats["biggest_win"]) > 0:
            lines.append(
                f"Biggest win: {_coins(econ, int(stats['biggest_win']))} "
                f"({stats['biggest_win_game']})"
            )
        streak_note = _streak_line(econ, streak)
        if streak_note:
            lines.append(streak_note)
        parts.append("\n".join(lines))
    else:
        parts.append("You haven't played yet — the tables are patient.")
    if cap > 0:
        parts.append(
            "**Today**\n"
            f"**{used:,}** of **{cap:,}** {econ.currency_plural} wagered "
            f"· resets <t:{int(reset_ts)}:R>"
        )
    if comp_amount > 0:
        parts.append(
            "**🎁 Daily Comp**\n"
            + (
                f"Claimed · the next one is yours <t:{int(reset_ts)}:R>"
                if comp_claimed else
                f"Waiting for you — a free {_coins(econ, comp_amount)} "
                "slots spin, on the house. Press 🎁 Daily Comp on the panel."
            )
        )
    return [_text("\n\n".join(parts))]


# ── pools (casino-classics Stage 2 — the parimutuel market) ─────────────


def _pool_bar(prob: float | None, width: int = 20) -> str:
    """Implied odds as a bar. None = nobody has staked yet."""
    if prob is None:
        return f"{mono(bar_fill(0, 1, width))}  no bets yet"
    bar = mono(bar_fill(round(prob * 1000), 1000, width))
    return f"{bar}  **{prob * 100:.0f}%** over"


def _pools_question(spec, econ: EconSettings, line: float, day: str) -> str:
    return spec.question.format(
        line=pools_logic.format_line(line), day=day, currency=econ.currency_plural,
    )


def build_pools_panel_layout(
    econ: EconSettings,
    line: float,
    split: pools_logic.PoolSplit,
    closes_at: float,
    day: str,
    *,
    spec,
    closed: bool = False,
    closing_soon: bool = False,
    controls: Item | None = None,
    art: PanelArt | None = None,
) -> list[Item]:
    """The standing market panel, repainted as stakes land. **Paradigm 8 —
    live board** (sticky, event-edited, one stable chart image).

    ``controls`` is the bet row (``views.pools_bet_row``), handed in so the
    footer of ``art`` — the guild's thin card strips
    (``core.layout.card_strips``) — lands under the buttons, not above them;
    the retired panel passes none.
    """
    prob = pools_logic.implied_probability(split)
    if closed:
        when = "Betting is **closed** — settles when the day rolls over."
    elif closing_soon:
        when = f"⏰ **Last hour** — betting closes <t:{int(closes_at)}:R>."
    else:
        when = f"Betting closes <t:{int(closes_at)}:R>."
    parts = [
        f"## 📈 Pools — today's market · {spec.label}\n"
        f"{_pools_question(spec, econ, line, day)}\n{when}",
        f"**Implied Odds**\n{_pool_bar(prob)}",
        f"Over: {_coins(econ, split.over)} · "
        f"Under: {_coins(econ, split.under)} · "
        f"Pool: {_coins(econ, split.total)}",
    ]
    empty = pools_logic.unbacked_side(split)
    if empty is not None and not closed:
        parts.append(
            "**⚠️ One-Sided So Far**\n"
            f"Nobody has backed **{pools_logic.describe_side(empty)}** yet. "
            "If it's still empty when betting closes there's no one to "
            "play against — the market is called off and every stake is "
            "refunded."
        )
    parts.append(
        "**How It Settles**\n"
        "Winners split the whole pool pro-rata — you're betting against "
        "the other side, not the house. Both sides need a stake: if "
        "everyone backs the same side, the market is called off and "
        "refunded in full. The bot counts it up when the day rolls over "
        "and compares it to the line; there is nothing to dispute."
        + (f"\n{spec.cap_note}" if spec.cap_note else "")
    )
    parts.append(
        "**Tomorrow**\n"
        "A different metric is drawn each day — never the same one two "
        "days running."
    )
    items: list[Item] = [_text("\n\n".join(parts))]
    items.append(
        discord.ui.MediaGallery(
            discord.MediaGalleryItem(
                media=f"attachment://{pools_charts.LIVE_FILENAME}"
            )
        )
    )
    if controls is not None:
        items.append(controls)
    top, bottom = card_strips(art)
    return [*top, *items, *bottom]


def build_pools_result_layout(
    econ: EconSettings,
    day: str,
    result: int,
    line: float,
    winning_side: str,
    payouts: list[tuple[int, int, int]],
    takeout: int,
    *,
    spec,
    chart: bool = True,
    name_fn: NameFn,
    art: PanelArt | None = None,
) -> list[Item]:
    """``payouts`` = (user_id, stake, payout), biggest return first.
    **Paradigm 7 — receipt.** ``art`` frames it in the guild's thin card
    strips (``core.layout.card_strips``), the chart inside the frame."""
    unit = spec.unit.format(currency=econ.currency_plural)
    measured = f"{result:+,}" if spec.signed else f"{result:,}"
    parts = [
        f"## 📈 Pools — the day is in · {spec.label}\n"
        f"**{day}** closed at **{measured}**{f' {unit}' if unit else ''} "
        f"against a line of **{pools_logic.format_line(line)}** — "
        f"**{pools_logic.describe_side(winning_side)}** takes it."
    ]
    if payouts:
        rows = [
            f"{name_fn(user_id)} +{payout - stake:,} (staked {stake:,})"
            if payout else f"{name_fn(user_id)} −{stake:,}"
            for user_id, stake, payout in payouts
        ]
        parts.append(
            "**Positions**\n"
            + "\n".join(
                logic.cap_lines(rows, limit=1500, more_label="more positions")
            )
        )
    if takeout:
        parts.append(
            "**Takeout**\n"
            f"{_coins(econ, takeout)} burned — taken out of circulation, "
            "not paid to the house."
        )
    items: list[Item] = [_text("\n\n".join(parts))]
    if chart:
        items.append(
            discord.ui.MediaGallery(
                discord.MediaGalleryItem(
                    media=f"attachment://{pools_charts.INSTRUMENT_FILENAME}"
                )
            )
        )
    top, bottom = card_strips(art)
    return [*top, *items, *bottom]


def build_pools_void_layout(
    day: str,
    refunded: int,
    *,
    unmeasurable: bool = False,
    art: PanelArt | None = None,
) -> list[Item]:
    """One-sided pools have no counterparty, so everyone gets their coins
    back. **Paradigm 7 — receipt.** ``art`` frames it in the guild's thin
    card strips (``core.layout.card_strips``)."""
    top, bottom = card_strips(art)
    reason = (
        "the bot can no longer measure what it was counting, so there is no "
        "honest way to settle it"
        if unmeasurable
        else "everyone who staked backed the same side, so there was nothing "
        "to play against"
    )
    return [
        *top,
        _text(
            "## ➖ Pools — No Market Today\n"
            f"The market for **{day}** has been called off: {reason}. All "
            f"**{refunded:,}** staked has been refunded in full."
        ),
        *bottom,
    ]


# ── mines ────────────────────────────────────────────────────────────────

_MINES_HIDDEN = "⬛"
_MINES_SAFE = "💎"
_MINES_BOMB = "💣"

_MINES_OUTCOME_LINES = {
    "cashed": "**Banked it.**",
    "pushed": "**Broke even** — the stake comes home.",
    "bombed": "**Boom.** The grid takes it.",
    "refunded": "The table was reset — the bet came home.",
}

_MINES_GLYPH = {
    "cashed": "✅", "pushed": "➖", "refunded": "➖", "bombed": "❌",
}


def _mines_board(
    revealed: tuple[int, ...] | list[int],
    bomb_tiles: tuple[int, ...] | list[int] | None,
) -> str:
    """The grid as it stands. ``bomb_tiles`` only ever passed on a loss —
    revealing the safe tiles a player walked away from would manufacture a
    near miss out of a decision that was correct by construction."""
    opened, bombs = set(revealed), set(bomb_tiles or ())
    rows = []
    for start in range(0, logic.MINES_TILES, logic.MINES_GRID_WIDTH):
        row = []
        for tile in range(start, start + logic.MINES_GRID_WIDTH):
            if tile in bombs:
                row.append(_MINES_BOMB)
            elif tile in opened:
                row.append(_MINES_SAFE)
            else:
                row.append(_MINES_HIDDEN)
        rows.append("".join(row))
    return "\n".join(rows)


def build_mines_layout(
    econ: EconSettings,
    user_id: int,
    step: MinesStep,
    *,
    name_fn: NameFn,
    footer: str | None = None,
) -> CardResult:
    """One grid, live or finished. **Paradigm 9 — game turn card.**
    ``footer`` renders as a trailing ``-#`` subtext line (the idle sweep's
    "cashed out automatically" notice — the embed footer's replacement).
    """
    live = step.outcome is None
    bomb_word = "bomb" if step.bombs == 1 else "bombs"
    heading = "## 💣 Mines" if live else (
        f"## {_MINES_GLYPH.get(step.outcome or '', '❌')} Mines"
    )
    show_bombs = step.outcome == "bombed"
    parts = [
        f"{heading}\n{name_fn(user_id)} is in for {_coins(econ, step.stake)} "
        f"· {step.bombs} {bomb_word}",
        "**The Grid**\n"
        + _mines_board(step.revealed, step.bomb_tiles if show_bombs else None),
    ]
    if live:
        standing = (
            f"**{logic.mines_mult_label(step.mult)}** · "
            f"{_coins(econ, logic.mines_payout(step.bombs, len(step.revealed), step.stake))}"
            if step.mult else "*Open a tile to start the ladder.*"
        )
        lines = [f"Banked if you stop now: {standing}"]
        if step.next_mult:
            lines.append(
                f"One more safe tile: {logic.mines_mult_label(step.next_mult)}"
            )
        parts.append("**Standing**\n" + "\n".join(lines))
    else:
        line = _MINES_OUTCOME_LINES.get(step.outcome or "", "")
        if step.topped:
            line = f"**Cleared the board!** {line}"
        if step.payout > 0:
            line = (
                f"{line} {logic.mines_mult_label(step.mult)} — "
                f"{_coins(econ, step.payout)}."
            )
        if step.payout == 0 and step.pot_after > 0:
            line = f"{line}\n{_pot_line(step.pot_after)}"
        if step.outcome != "refunded":
            line = _with_streak(line, econ, step.streak)
        parts.append(f"**Result**\n{line}")
    body = "\n\n".join(parts)
    items: list[Item] = [_text(body)]
    if footer:
        items.append(_text(f"-# {footer}"))
    return CardResult(items, body)


# ── shared control fragments ───────────────────────────────────────────


def next_round_row(game: str, emoji: str) -> discord.ui.ActionRow[Any]:
    """The persistent "Play Again" row a windowed game's result card ends
    on, and what a minimal ``cog_load``-registered catcher view also carries
    — folding the five standalone ``*NextView`` classes the embed pilot had
    into one shared builder (``custom_id`` unchanged: ``casino:{game}_next``).
    """
    return discord.ui.ActionRow(
        discord.ui.Button(
            label="Play Again", emoji=emoji,
            style=discord.ButtonStyle.secondary,
            custom_id=f"casino:{game}_next",
        )
    )
