"""Economy guide — the how-it-works topics, the card that shows them, and
the buttons that reach it.

The guide is four **topics** — 🔔 Notifications, 💰 Earning, 🛍️ Spending,
🔥 Streaks & perks — each a one-line gist and a full body, built from the
guild's live settings by :func:`guide_topics`. Billy, 2026-09-28: the old
guide was "too much at once", so the member now sees the gists and opens
one topic at a time. Two routes reach it:

* the economy panel's **How it works** block — the gists and a
  **Learn more about…** dropdown on the channel panel itself
  (``bank_panel_layout``), whose pick opens the card at that topic;
* the panel's grey **Help**, which opens the card at its overview: the
  gists and the same dropdown, no topic open.

Either way the card is ephemeral and redraws in place on every pick, so it
never shows the whole guide at once. Until 2026-08-18 the guide was a
channel panel of its own; until 2026-09-28 it was one embed carrying all
four topics. It is a Components V2 card now, like the panel around it.

The panel's ids outlived the old guide panel: ``guide_channel_id`` /
``guide_message_id`` are still the surviving pair, because the message they
name is the one the merged panel kept.

**Help**, the topic dropdown and the 🔔 Notifications toggle for the opt-in
role (``game_role_id``) answer static ``custom_id``s with no per-message
state; the economy panel's layout view binds them by id, and the cog
re-registers that view with ``bot.add_view`` at load, so clicks on the
existing panel still route after a restart. The role is a DM preference
only: it gates no channel and no payout, so opting out costs a member
nothing but their DMs.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast
from functools import partial

import discord

from bot_modules.core.layout import ExpiringLayoutView
from bot_modules.economy.leaderboard import _pad
from bot_modules.economy.logic import resolve_notify_toggle
from bot_modules.core.role_provision import ensure_config_role
from bot_modules.core.role_safety import grant_block_reason
from bot_modules.shared.feature_roles import ECONOMY_NOTIFY
from bot_modules.economy.service import load_econ_settings
from bot_modules.core.utils import safe_ephemeral as _core_safe_ephemeral

if TYPE_CHECKING:
    from bot_modules.core.app_context import Bot
    from bot_modules.economy.service import EconSettings

log = logging.getLogger("dungeonkeeper.economy")

NOTIFY_CUSTOM_ID = "econ_guide_notify"
HOW_IT_WORKS_CUSTOM_ID = "econ_guide_howto"
#: The panel's **Learn more about…** dropdown. Persistent, like the buttons.
GUIDE_TOPIC_CUSTOM_ID = "econ_guide_topic"

#: The panel block's heading, and the dropdown's resting text.
HOW_IT_WORKS_HEADING = "📖 How it works"
TOPIC_PLACEHOLDER = "Learn more about…"

#: How long the ephemeral card's dropdown keeps answering.
GUIDE_CARD_TIMEOUT_SECONDS = 600

PICK_FAILED_MSG = "❌ I couldn't open that part of the guide just now — try again in a moment."
TOPIC_GONE_MSG = "❌ That part of the guide isn't there any more — press Help to see what is."

NOTIFY_ON_MSG = (
    "🔔 Notifications **on** — your daily streak digest and raffle wins will "
    "come to your DMs. Click again to turn them off."
)
NOTIFY_OFF_MSG = (
    "🔕 Notifications **off** — you'll still earn and spend exactly as before, "
    "you just won't get the DMs. Click again to turn them back on."
)
NOTIFY_UNCONFIGURED_MSG = (
    "❌ Notifications aren't set up in this server yet — ask a mod to pick the "
    "economy notification role on the dashboard."
)
NOTIFY_BLOCKED_MSG = (
    "❌ Notifications can't be turned on right now — ask a mod to check the "
    "economy notification role on the dashboard."
)
NOTIFY_FAILED_MSG = (
    "❌ I couldn't update your roles just now — my role may sit below the "
    "notification role. Ask a mod to check, then try again."
)


@dataclass(frozen=True)
class GuideTopic:
    """One part of the guide.

    ``gist`` is the one line the panel and the card's overview show;
    ``description`` is the dropdown option's hint (≤100 characters, saying
    what opens, never a value); ``body`` is what the card shows once the
    topic is picked.
    """

    key: str
    emoji: str
    label: str
    description: str
    gist: str
    body: str

    @property
    def headline(self) -> str:
        """The gist as a line of the panel's block: ``🔔 **Notifications** — …``."""
        return f"{self.emoji} **{self.label}** — {self.gist}"


def _notifications_topic() -> GuideTopic:
    # The opt-in role is a DM preference, nothing more — it gates no channel
    # and no payout — so this topic promises notifications, not access. (It
    # used to point at <id:customize>, back when that role doubled as the
    # onboarding gate hiding these channels.)
    return GuideTopic(
        key="notifications",
        emoji="🔔",
        label="Notifications",
        description="Your streak digest and raffle wins, by DM",
        gist="your streak digest and raffle wins by DM, if you want them.",
        body=(
            "The **🔔 Notifications** button on the panel has your daily "
            "streak digest and raffle wins DMed to you. Toggle it off any "
            "time — it only changes your DMs, never what you can see or earn."
        ),
    )


def _earning_topic(settings: EconSettings) -> GuideTopic:
    emoji = settings.currency_emoji
    plural = settings.currency_plural
    # What pays what, one aligned row each (same table treatment as the
    # leaderboard panel: fixed-width code cells, payment outside them).
    earn_rows = [
        ("First message of the day", f"{emoji} {settings.login_text_base}"),
        ("…after 5 min in voice", f"{emoji} {settings.login_voice_base}"),
        ("Play a server game", f"{emoji} {settings.reward_game_participation}"),
        ("Win it", f"{emoji} {settings.reward_game_win}"),
        ("Answer the QOTD", f"{emoji} {settings.reward_qotd}"),
        ("Post in the Photo Challenge", f"{emoji} {settings.reward_photo_post}"),
        ("Tick an intake-card step", f"{emoji} {settings.reward_intake_step}"),
    ]
    width = max(len(label) for label, _ in earn_rows)
    lines = [f"`{_pad(label, width)}` {value}" for label, value in earn_rows]
    # The XP→coin conversion line only holds when the faucet is on (rate > 0);
    # it ships off, so promise it only when a guild has re-enabled it.
    if settings.xp_per_coin > 0:
        # A ceiling is worth naming: without it a member has no way to know
        # why a heavy day stopped paying partway through.
        ceiling = (
            f" (up to {settings.conversion_daily_cap:,} a day)"
            if settings.conversion_daily_cap > 0
            else ""
        )
        lines.append(
            f"Chatting earns XP all day — each night it converts into {plural} "
            f"automatically{ceiling}. `/bank quests` adds daily and weekly "
            "goals on top."
        )
        gist = (
            f"being around, games, the QOTD and quests pay {plural}, and "
            "your chat XP converts every night."
        )
    else:
        lines.append(
            "`/bank quests` adds daily and weekly goals — the surest way to earn."
        )
        gist = f"being around, games, the QOTD and quests pay {plural}."
    return GuideTopic(
        key="earning",
        emoji="💰",
        label="Earning",
        description="What pays, and how much",
        gist=gist,
        body="\n".join(lines),
    )


def _spending_topic(settings: EconSettings) -> GuideTopic:
    plural = settings.currency_plural
    rows = [
        ("/bank shop", "rent perks for your personal role — color, name, "
                       "gradient, holographic, icon (prices in the shop)"),
        ("/bank gift", "treat a friend to any perk, on your tab"),
    ]
    gist = "rent perks for your role, or gift them to a friend"
    if settings.transfers_enabled:
        rows.append(("/bank pay", f"send {plural} straight to a member"))
        gist += f" — or pay {plural} to a member"
    width = max(len(cmd) for cmd, _ in rows)
    return GuideTopic(
        key="spending",
        emoji="🛍️",
        label="Spending",
        description="Role perks, gifts and payments" if settings.transfers_enabled
        else "Role perks and gifts",
        gist=gist + ".",
        body="\n".join(f"`{_pad(cmd, width)}` {text}" for cmd, text in rows),
    )


def _streaks_topic(settings: EconSettings) -> GuideTopic:
    """The fine print the old embed's footer carried, a line each.

    Streaks and weekly rentals are unconditional, so this topic exists on a
    guild with every optional dial off; the shield, the booster bonus and
    the hoard tax each appear only while their dial is on.
    """
    plural = settings.currency_plural
    lines = [
        f"🔥 Streaks add +1/day (up to +{settings.streak_bonus_cap}), with "
        "bonuses at day 7, 30 and 100."
    ]
    extras: list[str] = []
    if settings.shop_streak_shield_enabled:
        lines.append(
            "🛡️ One missed day is forgiven each week; a shield from the shop "
            "covers one more."
        )
    if settings.booster_multiplier > 1:
        lines.append(
            f"🚀 Boosters earn ×{settings.booster_multiplier:g} on everything."
        )
        extras.append(f"boosters earn ×{settings.booster_multiplier:g}")
    if settings.demurrage_rate_pct > 0:
        lines.append(
            f"🐉 A weekly hoard tax collects {settings.demurrage_rate_pct}% of "
            f"anything above {settings.demurrage_threshold:,} {plural} — "
            "spending keeps a balance safe."
        )
        extras.append("big balances pay a weekly hoard tax")
    lines.append(
        "🔁 Rentals renew weekly — a short grace period covers a missed renewal."
    )
    gist = "come back daily for a growing streak bonus"
    if extras:
        gist += "; " + ", and ".join(extras)
    return GuideTopic(
        key="streaks",
        emoji="🔥",
        label="Streaks & perks",
        description="Streak bonuses and the fine print",
        gist=gist + ".",
        body="\n".join(lines),
    )


def guide_topics(settings: EconSettings) -> list[GuideTopic]:
    """The guide's topics, in order, from the guild's live settings.

    Built on every render, never held in a module constant: what a topic
    promises depends on the guild's dials, and a guide built once would
    quietly quote last week's payouts.
    """
    return [
        _notifications_topic(),
        _earning_topic(settings),
        _spending_topic(settings),
        _streaks_topic(settings),
    ]


def find_topic(settings: EconSettings, key: str) -> GuideTopic | None:
    """The topic named ``key`` under ``settings``, or None."""
    return next((t for t in guide_topics(settings) if t.key == key), None)


def topic_select(
    topics: list[GuideTopic], *, custom_id: str | None = None
) -> discord.ui.Select[Any]:
    """The **Learn more about…** dropdown, one option per topic.

    ``custom_id`` is the panel's persistent id; the ephemeral card passes
    none (discord.py mints one). No option is pre-selected: a pick opens a
    topic, and the card redraws on every pick, which puts the placeholder
    back so the same topic can be picked again.
    """
    kwargs: dict[str, Any] = {"custom_id": custom_id} if custom_id else {}
    return discord.ui.Select(
        placeholder=TOPIC_PLACEHOLDER,
        options=[
            discord.SelectOption(
                label=t.label, value=t.key, emoji=t.emoji, description=t.description,
            )
            for t in topics
        ],
        **kwargs,
    )


def guide_title(settings: EconSettings) -> str:
    return f"## {settings.currency_emoji} {settings.currency_plural} — How It Works"


def build_guide_layout(
    settings: EconSettings, topic: str | None = None
) -> list[discord.ui.Item[Any]]:
    """The ephemeral guide card's text, above its dropdown.

    Bare (no box, no banner — an ephemeral card is one member's, style
    guide § Media). The guild's currency icon, when set, sits beside the
    title as the old embed's thumbnail did — the wallet card's treatment.
    ``topic=None`` is the overview: every topic's gist.
    A topic key shows that one topic's body instead. An unknown key reads
    as the overview, so a stale pick never renders a blank card.
    """
    topics = guide_topics(settings)
    picked = next((t for t in topics if t.key == topic), None) if topic else None
    head: discord.ui.Item[Any] = discord.ui.TextDisplay(
        f"{guide_title(settings)}\n"
        f"Being active here earns you **{settings.currency_plural}**, which "
        "you can spend on role perks and gifts. Check your balance any time "
        "with `/bank wallet` (only you see it)."
    )
    if settings.currency_icon_url:
        head = discord.ui.Section(
            head, accessory=discord.ui.Thumbnail(settings.currency_icon_url)
        )
    items: list[discord.ui.Item[Any]] = [
        head,
        discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.large),
    ]
    if picked is None:
        items.append(discord.ui.TextDisplay(
            "\n".join(t.headline for t in topics)
        ))
    else:
        items.append(discord.ui.TextDisplay(
            f"### {picked.emoji} {picked.label}\n{picked.body}"
        ))
    return items


_safe_ephemeral = partial(_core_safe_ephemeral, log_label="econ guide")


async def _read_settings(bot: Bot, guild_id: int) -> EconSettings:
    """The guild's settings, read fresh — every render calls this."""
    def _read() -> EconSettings:
        with bot.ctx.open_db() as conn:
            return load_econ_settings(conn, guild_id)

    return await asyncio.to_thread(_read)


class GuideCardView(ExpiringLayoutView):
    """The ephemeral guide card: the overview or one topic, and the dropdown.

    Redrawn in place on every pick — the same instance, so its expiry timer
    is the live one (``ExpiringLayoutView``'s contract) — and the redraw
    re-reads the settings, so the third pick in one card quotes the same
    live rates as the first. The redraw is also what resets the dropdown:
    Discord sends nothing for re-picking the option still showing.
    """

    def __init__(
        self, bot: Bot, guild_id: int, settings: EconSettings, topic: str | None = None
    ) -> None:
        super().__init__(timeout=GUIDE_CARD_TIMEOUT_SECONDS)
        self.bot = bot
        self.guild_id = guild_id
        self.topic = topic
        self._render(settings)

    def _render(self, settings: EconSettings) -> None:
        self.clear_items()
        for item in build_guide_layout(settings, self.topic):
            self.add_item(item)
        select = topic_select(guide_topics(settings))
        select.callback = self._on_pick  # type: ignore[method-assign]
        self.add_item(discord.ui.ActionRow(select))

    async def _on_pick(self, interaction: discord.Interaction) -> None:
        """Redraw at the picked topic; on any failure, say so privately.

        A failed settings read or a refused edit would otherwise surface as
        Discord's bare "This interaction failed". The card keeps showing
        what it showed, and ``topic`` goes back to match it.
        """
        values = (interaction.data or {}).get("values") or []  # type: ignore[union-attr]
        previous = self.topic
        self.topic = str(values[0]) if values else None
        try:
            settings = await _read_settings(self.bot, self.guild_id)
            self._render(settings)
            await interaction.response.edit_message(view=self)
        except Exception:  # noqa: BLE001 — any failure gets the private note instead of Discord's bare "interaction failed"
            log.warning("econ guide: redraw failed for guild %s",
                        self.guild_id, exc_info=True)
            self.topic = previous
            await _safe_ephemeral(interaction, PICK_FAILED_MSG)


async def _send_card(interaction: discord.Interaction, topic: str | None) -> bool:
    """Send the guide card at ``topic`` ephemerally. False when it wasn't sent."""
    guild = interaction.guild
    if guild is None:
        await _safe_ephemeral(interaction, "This only works in a server.")
        return False
    bot = cast("Bot", interaction.client)
    settings = await _read_settings(bot, guild.id)
    if topic is not None and find_topic(settings, topic) is None:
        await _safe_ephemeral(interaction, TOPIC_GONE_MSG)
        return False
    view = GuideCardView(bot, guild.id, settings, topic)
    try:
        await interaction.response.send_message(view=view, ephemeral=True)
    except discord.HTTPException:
        log.debug("econ guide: failed to send the guide card", exc_info=True)
        return False
    try:
        view.message = await interaction.original_response()
    except discord.HTTPException:
        log.debug("econ guide: no handle on the guide card", exc_info=True)
    return True


async def show_guide(interaction: discord.Interaction) -> None:
    """The economy panel's **Help** — the guide card at its overview.

    The guide stopped being a posted panel on 2026-08-18; since 2026-09-28
    Help opens the topic picker (the four gists and the dropdown) rather
    than the whole guide at once. The button was "❓ How it Works" under the
    embed panel and is the grey **Help** beside the title since the hub-box
    rework (2026-09-26), on the same ``HOW_IT_WORKS_CUSTOM_ID`` so a panel
    posted before either change still answers.

    Reads settings fresh on every click for the same reason the panel does —
    the rates are live config, and a cached guide would quietly quote last
    week's payouts.
    """
    await _send_card(interaction, None)


async def show_guide_topic(interaction: discord.Interaction) -> bool:
    """The panel's **Learn more about…** pick — the guide card at that topic.

    Returns whether a card went out; the caller (the panel view) repaints
    the panel either way, since the member's pick is showing on it.
    """
    values = (interaction.data or {}).get("values") or []  # type: ignore[union-attr]
    return await _send_card(interaction, str(values[0]) if values else "")


async def toggle_notify(interaction: discord.Interaction) -> None:
    """The economy panel's 🔔 toggle for the economy's opt-in DM role.

    One button serves everyone, so its label can't reflect per-member state;
    the click answers ephemerally with whichever way it just flipped.
    """
    guild = interaction.guild
    member = interaction.user
    if guild is None or not isinstance(member, discord.Member):
        await _safe_ephemeral(interaction, "This only works in a server.")
        return

    bot = cast("Bot", interaction.client)
    # The first press makes the role and then grants it: two REST calls
    # before any reply, against Discord's 3 s window. Every reply below
    # goes through _safe_ephemeral, which follows up once deferred.
    await interaction.response.defer(ephemeral=True, thinking=True)

    # A member pressing 🔔 on a guild that never set an opt-in role used to
    # hit a dead end telling them to find an admin. Make the role instead —
    # this is the one place in the feature where "first use" is a member
    # actually asking for it.
    #
    # An admin who stored "(none)" gets None back, and the dead end with it.
    # That is a 2026-09-03 change: `none_means_off` was False here until
    # then, so the role was made regardless of the dial, while Economy
    # Settings told the admin "(none)" turned notifications off. A
    # preference the code doesn't enforce is the one thing CLAUDE.md rules
    # out flatly, and the ambiguity it was covering for — panels writing 0
    # on every save — is now answered on the panel, by the state line under
    # the picker.
    role = await ensure_config_role(
        bot.ctx, guild, ECONOMY_NOTIFY.key, ECONOMY_NOTIFY.spec,
        feature=ECONOMY_NOTIFY.feature,
        allow_legacy_fallback=ECONOMY_NOTIFY.legacy_fallback,
        respect_opt_out=ECONOMY_NOTIFY.none_means_off,
        # The one ping the bot actually hands out, so a same-named role
        # above its own top role must not be adopted: it could never be
        # granted, and every 🔔 press after would fail at add_roles.
        assigns=ECONOMY_NOTIFY.assigns,
    )
    if role is None:
        # Opted out, or the role couldn't be made (no Manage Roles). Reads
        # the same to the member either way.
        await _safe_ephemeral(interaction, NOTIFY_UNCONFIGURED_MSG)
        return

    action = resolve_notify_toggle(
        role_id=role.id,
        member_role_ids={r.id for r in member.roles},
    )

    if action == "grant" and grant_block_reason(
        role, guild, feature="Economy notifications"
    ) is not None:
        await _safe_ephemeral(interaction, NOTIFY_BLOCKED_MSG)
        return

    try:
        if action == "grant":
            await member.add_roles(role, reason="Economy notifications opt-in")
        else:
            await member.remove_roles(role, reason="Economy notifications opt-out")
    except discord.HTTPException:
        log.debug("econ guide: notify toggle failed", exc_info=True)
        await _safe_ephemeral(interaction, NOTIFY_FAILED_MSG)
        return

    await _safe_ephemeral(
        interaction, NOTIFY_ON_MSG if action == "grant" else NOTIFY_OFF_MSG
    )
