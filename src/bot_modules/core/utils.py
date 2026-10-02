"""Utility functions for the Discord bot."""

from __future__ import annotations

import logging
import re
from typing import Any, Literal, TypeAlias

import discord
from discord import app_commands

log = logging.getLogger(__name__)

GuildTextLike: TypeAlias = discord.TextChannel | discord.Thread


def disable_all_items(view: discord.ui.View) -> None:
    """Disable every button and select on ``view`` — used to retire a
    message's controls when the interaction it drove is finished."""
    for item in view.children:
        if isinstance(item, (discord.ui.Button, discord.ui.Select)):
            item.disabled = True


class ExpiringView(discord.ui.View):
    """A ``discord.ui.View`` base for views with a finite timeout.

    ``discord.py``'s default ``on_timeout`` is a no-op: past the timeout the
    view stops listening, but its buttons keep rendering as live and a press
    fails with Discord's generic "This interaction failed" — the member gets
    no signal anything expired. Subclassing this instead disables every item
    in place and edits the stored message to show that, so the panel visibly
    goes dead rather than silently failing on the next click.

    The caller is responsible for setting ``self.message`` right after
    sending or editing the view onto a message — usually
    ``view.message = await interaction.original_response()``. That handle
    stays valid and editable for the view's whole timeout window even for an
    ephemeral response (every current caller in this repo has a timeout of
    300s or less), which is why ``original_response()`` is the right handle
    to store rather than trying to track a channel message id. Until
    ``self.message`` is set, ``on_timeout`` disables the in-memory view (so a
    caller who inspects it afterward sees disabled items) but has nothing to
    edit, so it returns without making a request.

    A press that swaps a *new* view instance onto the same message (a page
    turn that rebuilds the view, a picker replaced by its confirm gate) must
    ``stop()`` the view it replaces before the edit. discord.py keeps the
    replaced view's timeout timer running, and once it fires this
    ``on_timeout`` would edit the greyed-out old view back over the live one.
    Re-rendering the same instance in place needs nothing: its timer is the
    live view's own.

    This is the house pattern already used ad hoc by
    ``jail_commands._ConfirmView``, ``games_clapback_cog.ClapbackRecapView``
    and ``pen_pals_cog._EndConfirmView`` — new finite-timeout views should
    inherit this rather than hand-roll it again. It is deliberately narrow:
    the one-member gate is :class:`MemberScoped`, a mixin that composes with
    this, and an ``on_error`` -> ``handle_view_error`` extraction is a
    separate, unbuilt concern (see docs/plans/common-lib-round-2.md).
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.message: discord.Message | None = None

    async def on_timeout(self) -> None:
        disable_all_items(self)
        if self.message is None:
            return
        try:
            await self.message.edit(view=self)
        except discord.HTTPException:
            pass


class MemberScoped:
    """Mixin: a view that answers to one member and refuses everyone else.

    Set ``scope_user_id`` in ``__init__``; a press from anyone else is refused
    before its callback runs, with ``SCOPE_DENIAL`` sent to them privately.
    The copy stays per view on purpose — "This inbox isn't yours." and "Only
    the channel owner can answer." say different things — so override the
    class attribute rather than the check. ``None`` refuses silently, for a
    view that only its owner can ever see.

    List it **before** the view class so its ``interaction_check`` wins the
    MRO — ``class _PickView(MemberScoped, discord.ui.View)``, and the same
    with :class:`ExpiringView` or ``ExpiringLayoutView``.

    Checks the id only. A view where a mod may also press (a game's host
    controls) wants :func:`is_host_or_mod` instead, and a view anyone may
    press but only once is not this either.
    """

    SCOPE_DENIAL: str | None = "❌ This isn't for you."
    scope_user_id: int

    async def interaction_check(self, interaction: discord.Interaction, /) -> bool:
        if interaction.user.id == self.scope_user_id:
            return True
        if self.SCOPE_DENIAL is not None:
            await safe_ephemeral(interaction, self.SCOPE_DENIAL, log_label=type(self).__name__)
        return False

async def safe_ephemeral(
    interaction: discord.Interaction, text: str, *, log_label: str = "ephemeral"
) -> None:
    """Send ``text`` back to the clicker privately, whatever state we're in.

    Answers the two ways a view can find itself needing to say something: if
    the interaction was already responded to (deferred, or a modal handled
    first) the reply has to go through ``followup``, otherwise through
    ``response``. Getting that wrong raises, and a raise inside a button
    callback surfaces to the member as "This interaction failed" — so the
    send is best-effort and an HTTP failure is swallowed.

    ``log_label`` names the caller in the debug line; the traceback would
    otherwise point at this module rather than the view that failed.
    """
    try:
        if interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=True)
        else:
            await interaction.response.send_message(text, ephemeral=True)
    except discord.HTTPException:
        log.debug("%s: failed to send ephemeral", log_label, exc_info=True)


def is_host_or_mod(interaction: discord.Interaction, host_id: int) -> bool:
    """Is the clicker the game's host, or a server mod who can override?

    The gate on every game view's host-only control — close the round, skip,
    end early. One definition for all of them: the host always passes; anyone
    else needs ``administrator`` or ``manage_guild`` *in a guild*, so a DM or
    a non-member user never qualifies.

    Deliberately **not** ``AppContext.is_mod``. That one reads
    ``interaction.permissions`` and additionally honours the guild's
    configured mod roles, so it answers a wider question ("is this person
    staff?"). Routing game overrides through it would hand a configured
    mod-role holder without ``manage_guild`` control over other people's
    games — a permission widening, not a refactor. ``is_mod_or_admin`` below
    is a third rule again (it also accepts ``manage_channels``). Three gates,
    kept apart on purpose.
    """
    if interaction.user.id == host_id:
        return True
    if interaction.guild and isinstance(interaction.user, discord.Member):
        perms = interaction.user.guild_permissions
        return perms.administrator or perms.manage_guild
    return False


def has_mod_or_admin_permissions(perms: Any) -> bool:
    """Return True if perms grant admin, manage_guild, or manage_channels.

    Matches the cog's ``is_mod_or_admin`` rule: any one of the three
    elevated perms qualifies a user to run mod-tier game commands.
    """
    if not perms:
        return False
    return bool(
        getattr(perms, "administrator", False)
        or getattr(perms, "manage_guild", False)
        or getattr(perms, "manage_channels", False)
    )


def is_mod_or_admin():
    """``app_commands.check`` gating the /games admin commands.

    A third rule again, wider than ``is_host_or_mod`` above: it accepts
    ``manage_channels`` as well, because the commands it guards are about
    which channels games may run in — the perm that says "you arrange this
    server's rooms" is the one that ought to say where games live. Kept
    separate from the host gate on purpose; widening that to match this
    would hand any channel manager control of other people's live games.
    """
    async def predicate(interaction: discord.Interaction) -> bool:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return False
        return has_mod_or_admin_permissions(interaction.user.guild_permissions)

    return app_commands.check(predicate)


def get_interaction_member(interaction: discord.Interaction) -> discord.Member | None:
    """Get the member from an interaction, resolving from guild if needed."""
    user = interaction.user
    if isinstance(user, discord.Member):
        return user
    guild = interaction.guild
    if guild is None:
        return None
    return guild.get_member(user.id)


def get_bot_member(guild: discord.Guild) -> discord.Member | None:
    """Get the bot's member object for a guild."""
    return guild.me


def format_user_for_log(
    user: discord.abc.User | discord.Member | None = None,
    user_id: int | None = None,
) -> str:
    """Format a user for logging with display name, username, and ID."""
    if user is not None:
        resolved_id = getattr(user, "id", user_id)
        display_name = getattr(user, "display_name", None)
        username = getattr(user, "name", None)
        if display_name and username and display_name != username:
            return f"{display_name} [{username}] ({resolved_id})"
        label = display_name or username or str(user)
        return f"{label} ({resolved_id})" if resolved_id is not None else label

    if user_id is None:
        return "unknown user"

    return f"user {user_id}"


def resolve_user_for_log(guild: discord.Guild | None, user_id: int) -> str:
    """Resolve and format a user ID for logging."""
    member = guild.get_member(user_id) if guild is not None else None
    return format_user_for_log(member, user_id)


def format_guild_for_log(
    guild: discord.Guild | None = None,
    guild_id: int | None = None,
) -> str:
    """Format a guild for logging with name and ID."""
    if guild is not None:
        resolved_id = getattr(guild, "id", guild_id)
        name = getattr(guild, "name", None)
        if name:
            return f"{name} ({resolved_id})" if resolved_id is not None else name
        return f"guild {resolved_id}" if resolved_id is not None else "unknown guild"

    if guild_id is None:
        return "unknown guild"
    return f"guild {guild_id}"


def resolve_guild_for_log(bot: discord.Client | None, guild_id: int) -> str:
    """Resolve and format a guild ID for logging."""
    guild = bot.get_guild(guild_id) if bot is not None else None
    return format_guild_for_log(guild, guild_id)


def get_guild_channel_or_thread(
    guild: discord.Guild,
    channel_id: int,
) -> GuildTextLike | None:
    """Get a text channel or thread from a guild by ID."""
    resolver = getattr(guild, "get_channel_or_thread", None)
    if callable(resolver):
        channel = resolver(channel_id)
        if isinstance(channel, (discord.TextChannel, discord.Thread)):
            return channel
        return None

    channel = guild.get_channel(channel_id)
    if isinstance(channel, discord.TextChannel):
        return channel

    thread = guild.get_thread(channel_id)
    if isinstance(thread, discord.Thread):
        return thread

    return None


async def resolve_bot_channel(bot, channel_id: int):
    """A channel by id from the cache, falling back to one API fetch.

    Every background loop that posts somewhere configured needs this — the
    cache misses after a restart, or for a channel the bot has never seen —
    and it had been copy-pasted into four of them (game_start_ping,
    scheduled_games, announcements, event_echo) before landing here. Returns
    None rather than raising: a loop must not die because one destination
    went away.
    """
    channel = bot.get_channel(channel_id)
    if channel is not None:
        return channel
    try:
        return await bot.fetch_channel(channel_id)
    except Exception:  # noqa: BLE001 — shared by background loops; a fetch failure of any kind must not kill the loop (see docstring)
        return None


#: Channel types the bot can post a panel or a doc into.
POSTABLE_CHANNELS = (discord.TextChannel, discord.Thread, discord.VoiceChannel)


async def resolve_postable_channel_in_guild(
    bot, channel_id: int, guild: discord.Guild | None = None
):
    """A postable channel by id, refusing anything outside ``guild``.

    This is a permission check, not a convenience. ``channel_id`` arrives from
    a dashboard route, and both ``get_channel`` and ``fetch_channel`` search
    every guild the bot is in — so without the ownership test a moderator of
    one guild could post, edit and pin into a co-tenant guild's channels. The
    docs sync and the role-menu sync each carried this verbatim; a rule about
    who may write where should have exactly one statement.

    ``guild`` is None only on paths acting on an already-stored placement,
    where the channel was vetted when it was first chosen.
    """
    channel = await resolve_bot_channel(bot, channel_id)
    if not isinstance(channel, POSTABLE_CHANNELS):
        return None
    owner = getattr(channel, "guild", None)
    if guild is not None and owner is not None and owner.id != guild.id:
        return None
    return channel


def jump_url(guild_id: int | Literal["@me"], channel_id: int, message_id: int) -> str:
    """A permalink to one message.

    Prefer ``message.jump_url`` when you have the message object; this is for
    callers working from stored ids, which would otherwise have to fetch a
    message purely to read the property off it. A channel-only link loses the
    message and lands the reader at the bottom of the channel instead — see
    the style guide's "Pointing at things" for when that is nonetheless right.

    ``guild_id`` takes the literal ``"@me"`` for a message in a DM, which is
    the form Discord itself uses. That one string is the whole widening — ids
    still arrive as ``int``, so a stringified snowflake from the web layer is
    a type error rather than a link that happens to work.
    """
    return f"https://discord.com/channels/{guild_id}/{channel_id}/{message_id}"


def channel_url(guild_id: int | Literal["@me"], channel_id: int) -> str:
    """A deep link to a channel, for when the channel *is* the subject.

    Its sibling ``jump_url`` is the default — see the style guide's "Pointing
    at things". Reach for this one only where there is no message to land on
    (a voice room to join, a channel to go start something in), or as the
    degraded form when a stored message id is missing.
    """
    return f"https://discord.com/channels/{guild_id}/{channel_id}"


def _snowflakes(values) -> list[discord.Object]:
    """``discord.Object`` per distinct positive id in ``values``.

    Takes whatever the call site happens to hold — ``discord.Member`` /
    ``discord.Role`` objects, raw ints, id strings, and ``None`` where a
    lookup missed — because that is what the ping sites actually have.
    Anything unusable is dropped rather than raising: a member the guild
    cache could not resolve has no mention in the text either, so there is
    nothing for the allow-list to name.
    """
    out: list[discord.Object] = []
    seen: set[int] = set()
    for value in values or ():
        if value is None:
            continue
        raw = getattr(value, "id", value)
        try:
            ident = int(raw)
        except (TypeError, ValueError):
            continue
        if ident > 0 and ident not in seen:
            seen.add(ident)
            out.append(discord.Object(id=ident))
    # Discord rejects a payload naming more than 100 of either kind with a
    # 400, which would cost the message entirely. Truncating costs a ping.
    return out[:100]


def allow_pings(*, users=(), roles=()) -> discord.AllowedMentions:
    """An allow-list naming exactly who this send may notify.

    The bot is constructed with ``allowed_mentions=AllowedMentions.none()``
    (``core.app_context.Bot``), so a mention in ``content`` renders but
    notifies nobody unless the send says otherwise. This is how a send says
    otherwise — a turn ping, a game-start announcement, a mod-role call-out.

    Every field is pinned rather than left unset, so the result means the
    same thing wherever it is sent. A hand-rolled
    ``AllowedMentions(users=[...])`` leaves the other three at *default*,
    which is not a value but a deferral: discord.py merges it onto the
    sending client's own default, so what it finally allows is decided
    elsewhere. Through this bot that merge lands on ``none()`` and behaves —
    but through a client with no default of its own (a REST-only script), or
    through this one if that default is ever revisited, those unset fields
    serialise as *allow* and the send goes out with
    ``parse: ['everyone', 'roles']``. Naming all four keeps the allow-list
    from meaning more than it says.
    ``role_ping_kwargs`` is the role-only twin and builds the ``content``
    too; reach for this one when the content already exists and only the
    allow-list is missing.

    An empty result suppresses everything, which is the right answer for a
    send whose member lookups all missed.
    """
    return discord.AllowedMentions(
        everyone=False,
        users=_snowflakes(users),
        roles=_snowflakes(roles),
        replied_user=False,
    )


#: Written mentions in a template, as an admin can type them. Discord
#: accepts both ``<@id>`` and the legacy nickname form ``<@!id>`` for a
#: user; ``<@&id>`` is a role and cannot match the user pattern, because
#: ``&`` is neither ``!`` nor a digit.
_TEMPLATE_USER_RE = re.compile(r"<@!?(\d+)>")
_TEMPLATE_ROLE_RE = re.compile(r"<@&(\d+)>")
_TEMPLATE_EVERYONE_RE = re.compile(r"@everyone|@here")


def template_pings(template: str, *, users=(), roles=()) -> discord.AllowedMentions:
    """An allow-list for a send whose text came from an **admin-authored
    template** — a greeter arrival line, a ``/grant`` announcement.

    The allow-list is derived from the *raw* template (what an admin typed
    into the dashboard) plus the placeholder targets the caller names, and
    from nothing else:

    * ``everyone`` only if the literal ``@everyone`` or ``@here`` is in the
      template. The greeter line ships as ``@here - {member} has arrived``,
      so that ``@here`` has to survive the bot's suppressive default or
      three guilds stop hearing about newcomers.
    * ``roles`` = every ``<@&id>`` written in the template, plus ``roles``.
    * ``users`` = every ``<@id>``/``<@!id>`` written in the template, plus
      ``users`` — the member a ``{member}`` placeholder resolves to, the
      actor behind an ``{actor}``.

    **Never the rendered result.** A template also splices in text the
    member controls — ``{member_name}`` is a display name — and a newcomer
    who names themselves ``@everyone`` would otherwise widen the allow-list
    by renaming themselves, riding the Mention Everyone permission the
    admin's own ``@here`` needs. Deriving from the template makes that
    impossible by construction rather than by remembering to escape (the
    renderers escape it too, belt and braces). By the same token the
    caller passes *ids*, not rendered text, so a name can never arrive here
    at all.

    Shares ``allow_pings``' normalisation: ids, ``discord.Object``,
    ``Member``/``Role``/``User`` all work, duplicates and non-positive ids
    are dropped, and all four fields are pinned (see ``allow_pings`` for why
    an unset field is a deferral, not a ``False``). A blank template with
    nothing passed suppresses everything, which is the right answer for a
    guild that cleared the box.
    """
    text = template or ""
    return discord.AllowedMentions(
        everyone=bool(_TEMPLATE_EVERYONE_RE.search(text)),
        users=_snowflakes([*_TEMPLATE_USER_RE.findall(text), *users]),
        roles=_snowflakes([*_TEMPLATE_ROLE_RE.findall(text), *roles]),
        replied_user=False,
    )


def role_ping_kwargs(role_ids) -> dict:
    """``content``/``allowed_mentions`` for a post that should ping some roles.

    Two things this gets right that a hand-rolled version keeps getting wrong,
    which is why it is shared (see docs/embed_style_guide.md):

    * The mention has to live in ``content``. A role mention rendered inside
      an **embed** displays but never notifies anyone, so a card that pings
      from its embed body is silently doing nothing.
    * ``AllowedMentions``' unset fields default to *allow*. The bare
      ``AllowedMentions(roles=[...])`` form still serializes
      ``parse: ['everyone', 'users']``, so it allow-lists far more than the
      caller asked for. Every field is therefore pinned explicitly.

    Duplicate and non-positive ids are dropped, and an empty result gives a
    silent post with mentions fully suppressed — the "no ping role is
    configured" state, which is a real and wanted configuration, not a bug.
    """
    seen: list[int] = []
    for raw in role_ids or ():
        try:
            rid = int(raw)
        except (TypeError, ValueError):
            continue
        if rid > 0 and rid not in seen:
            seen.append(rid)
    if not seen:
        return {"allowed_mentions": discord.AllowedMentions.none()}
    return {
        "content": " ".join(f"<@&{rid}>" for rid in seen),
        "allowed_mentions": discord.AllowedMentions(
            everyone=False,
            users=False,
            roles=[discord.Object(id=rid) for rid in seen],
            replied_user=False,
        ),
    }


async def resolve_reply_target(message: discord.Message) -> discord.Message | None:
    """Resolve the target message of a reply, fetching if necessary."""
    if not message.reference:
        return None

    if isinstance(message.reference.resolved, discord.Message):
        return message.reference.resolved

    if not message.reference.message_id:
        return None

    ref_channel: GuildTextLike | None = None
    if message.guild is not None and message.reference.channel_id is not None:
        candidate_channel = message.guild.get_channel(message.reference.channel_id)
        if isinstance(candidate_channel, discord.TextChannel):
            ref_channel = candidate_channel
    if ref_channel is None and isinstance(
        message.channel,
        (discord.TextChannel, discord.Thread),
    ):
        ref_channel = message.channel

    if not isinstance(ref_channel, (discord.TextChannel, discord.Thread)):
        return None

    try:
        return await ref_channel.fetch_message(message.reference.message_id)
    except (discord.NotFound, discord.Forbidden):
        return None
