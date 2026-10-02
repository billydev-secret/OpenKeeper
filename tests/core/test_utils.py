from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from bot_modules.core.utils import (
    ExpiringView,
    MemberScoped,
    format_guild_for_log,
    format_user_for_log,
    is_host_or_mod,
    is_mod_or_admin,
    resolve_postable_channel_in_guild,
    role_ping_kwargs,
    template_pings,
    safe_ephemeral,
    resolve_guild_for_log,
    resolve_user_for_log,
)
from tests.paths import REPO_ROOT


# ── format_user_for_log ───────────────────────────────────────────────

def test_no_args_returns_unknown():
    assert format_user_for_log() == "unknown user"


def test_user_id_only():
    assert format_user_for_log(user_id=42) == "user 42"


def test_display_name_matches_username():
    user = SimpleNamespace(id=1, display_name="Alice", name="Alice")
    assert format_user_for_log(user) == "Alice (1)"  # type: ignore[arg-type]


def test_display_name_differs_from_username():
    user = SimpleNamespace(id=1, display_name="Wonderland Alice", name="alice99")
    assert format_user_for_log(user) == "Wonderland Alice [alice99] (1)"  # type: ignore[arg-type]


def test_display_name_none_falls_back_to_username():
    user = SimpleNamespace(id=5, display_name=None, name="bob")
    assert format_user_for_log(user) == "bob (5)"  # type: ignore[arg-type]


def test_user_overrides_user_id():
    user = SimpleNamespace(id=10, display_name="Carol", name="Carol")
    assert format_user_for_log(user, user_id=99) == "Carol (10)"  # type: ignore[arg-type]


def test_user_with_no_id_uses_fallback_id():
    user = SimpleNamespace(display_name="Dave", name="dave")
    assert format_user_for_log(user, user_id=7) == "Dave [dave] (7)"  # type: ignore[arg-type]


# ── resolve_user_for_log ──────────────────────────────────────────────

def test_known_member_uses_format():
    member = SimpleNamespace(id=10, display_name="Eve", name="Eve")
    guild = SimpleNamespace(get_member=lambda uid: member if uid == 10 else None)
    assert resolve_user_for_log(guild, 10) == "Eve (10)"  # type: ignore[arg-type]


def test_unknown_member_falls_back_to_id():
    guild = SimpleNamespace(get_member=lambda uid: None)
    assert resolve_user_for_log(guild, 99) == "user 99"  # type: ignore[arg-type]


def test_none_guild_falls_back_to_id():
    assert resolve_user_for_log(None, 42) == "user 42"


# ── format_guild_for_log ──────────────────────────────────────────────

def test_format_guild_no_args_returns_unknown():
    assert format_guild_for_log() == "unknown guild"


def test_format_guild_id_only():
    assert format_guild_for_log(guild_id=42) == "guild 42"


def test_format_guild_with_name():
    guild = SimpleNamespace(id=7, name="My Server")
    assert format_guild_for_log(guild) == "My Server (7)"  # type: ignore[arg-type]


def test_format_guild_without_name_uses_id():
    guild = SimpleNamespace(id=7, name=None)
    assert format_guild_for_log(guild) == "guild 7"  # type: ignore[arg-type]


# ── resolve_guild_for_log ─────────────────────────────────────────────

def test_resolve_known_guild_uses_format():
    guild = SimpleNamespace(id=10, name="Zone")
    bot = SimpleNamespace(get_guild=lambda gid: guild if gid == 10 else None)
    assert resolve_guild_for_log(bot, 10) == "Zone (10)"  # type: ignore[arg-type]


def test_resolve_unknown_guild_falls_back_to_id():
    bot = SimpleNamespace(get_guild=lambda gid: None)
    assert resolve_guild_for_log(bot, 99) == "guild 99"  # type: ignore[arg-type]


def test_resolve_none_bot_falls_back_to_id():
    assert resolve_guild_for_log(None, 42) == "guild 42"


# ── is_host_or_mod ────────────────────────────────────────────────────
#
# The gate on every game view's host-only control. One definition now backs
# all 29 former copies, so every branch is exercised here rather than in a
# per-game cog test. Deliberately narrower than AppContext.is_mod (which also
# honours guild-configured mod roles) — see the docstring on the helper.

HOST_ID = 777


def _member(user_id: int, *, administrator=False, manage_guild=False) -> discord.Member:
    member = MagicMock(spec=discord.Member)
    member.id = user_id
    member.guild_permissions = SimpleNamespace(
        administrator=administrator, manage_guild=manage_guild
    )
    return member


def _interaction(user, *, in_guild: bool = True) -> discord.Interaction:
    return SimpleNamespace(  # type: ignore[return-value]
        user=user, guild=SimpleNamespace(id=1) if in_guild else None
    )


def test_host_passes():
    assert is_host_or_mod(_interaction(_member(HOST_ID)), HOST_ID) is True


def test_host_passes_even_in_a_dm():
    """The host check runs before the guild check, so it holds with no guild."""
    user = MagicMock(spec=discord.User)
    user.id = HOST_ID
    assert is_host_or_mod(_interaction(user, in_guild=False), HOST_ID) is True


@pytest.mark.parametrize(
    "perms",
    [
        pytest.param({"administrator": True}, id="administrator"),
        pytest.param({"manage_guild": True}, id="manage_guild"),
        pytest.param({"administrator": True, "manage_guild": True}, id="both"),
    ],
)
def test_mod_overrides_the_host(perms):
    assert is_host_or_mod(_interaction(_member(1, **perms)), HOST_ID) is True


def test_plain_member_is_refused():
    assert is_host_or_mod(_interaction(_member(1)), HOST_ID) is False


@pytest.mark.parametrize(
    "perms",
    [
        pytest.param({"manage_channels": True}, id="manage_channels"),
        pytest.param({"manage_messages": True}, id="manage_messages"),
        pytest.param({"moderate_members": True}, id="moderate_members"),
    ],
)
def test_other_elevated_perms_do_not_qualify(perms):
    """Only administrator/manage_guild count.

    is_mod_or_admin — the /games admin-command gate — *does* accept
    manage_channels. Keeping that perm out here is the whole reason the two
    rules stayed separate; if this test starts failing, a gate was widened.
    """
    member = _member(1)
    member.guild_permissions = SimpleNamespace(
        administrator=False, manage_guild=False, **perms
    )
    assert is_host_or_mod(_interaction(member), HOST_ID) is False


def test_non_member_user_in_a_guild_is_refused():
    """A raw User (uncached member) has no guild_permissions to trust."""
    user = MagicMock(spec=discord.User)
    user.id = 1
    assert is_host_or_mod(_interaction(user), HOST_ID) is False


def test_mod_in_a_dm_is_refused():
    """No guild on the interaction ⇒ no guild permissions apply."""
    assert (
        is_host_or_mod(_interaction(_member(1, administrator=True), in_guild=False), HOST_ID)
        is False
    )


# ── safe_ephemeral ────────────────────────────────────────────────────
#
# Nine copies of this send-or-followup dance lived across economy/, casino/,
# services/ and confessions_cog. The branch that matters is which of the two
# send paths gets used: picking the wrong one raises inside a button callback,
# which the member sees as "This interaction failed".


def _send_interaction(*, responded: bool, raises: Exception | None = None):
    interaction = MagicMock(spec=discord.Interaction)
    interaction.response = MagicMock()
    interaction.response.is_done.return_value = responded
    interaction.response.send_message = AsyncMock(side_effect=raises)
    interaction.followup = MagicMock()
    interaction.followup.send = AsyncMock(side_effect=raises)
    return interaction


@pytest.mark.asyncio
async def test_fresh_interaction_replies_through_response():
    interaction = _send_interaction(responded=False)
    await safe_ephemeral(interaction, "hi")
    interaction.response.send_message.assert_awaited_once_with("hi", ephemeral=True)
    interaction.followup.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_already_responded_interaction_replies_through_followup():
    """Deferred, or a modal answered first — ``response`` is spent."""
    interaction = _send_interaction(responded=True)
    await safe_ephemeral(interaction, "hi")
    interaction.followup.send.assert_awaited_once_with("hi", ephemeral=True)
    interaction.response.send_message.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("responded", [False, True], ids=["response", "followup"])
async def test_http_failure_is_swallowed(responded):
    """Best-effort: a raise here would surface as an interaction failure."""
    boom = discord.HTTPException(MagicMock(status=500), "nope")
    await safe_ephemeral(_send_interaction(responded=responded, raises=boom), "hi")


@pytest.mark.asyncio
async def test_non_http_errors_still_propagate():
    """Only HTTPException is swallowed — a bug in the caller shouldn't vanish."""
    interaction = _send_interaction(responded=False, raises=ValueError("bug"))
    with pytest.raises(ValueError):
        await safe_ephemeral(interaction, "hi")


@pytest.mark.asyncio
async def test_log_label_names_the_caller(caplog):
    """Each module binds its own label; the traceback alone points here."""
    boom = discord.HTTPException(MagicMock(status=500), "nope")
    with caplog.at_level(logging.DEBUG, logger="bot_modules.core.utils"):
        await safe_ephemeral(
            _send_interaction(responded=False, raises=boom), "hi", log_label="econ pin"
        )
    assert "econ pin" in caplog.text


@pytest.mark.asyncio
async def test_module_partials_carry_their_label():
    """The nine former copies are now one function plus a bound label."""
    from bot_modules.economy.pin_views import _safe_ephemeral

    interaction = _send_interaction(responded=False)
    await _safe_ephemeral(interaction, "hi")
    interaction.response.send_message.assert_awaited_once_with("hi", ephemeral=True)


# ── is_mod_or_admin ───────────────────────────────────────────────────
#
# The /games admin-command gate, formerly duplicated in games_config_cog and
# games_external_cog. Wider than is_host_or_mod: manage_channels qualifies
# here and deliberately does not there.


def _predicate():
    """Pull the check out of the decorator so it can be called directly."""

    @is_mod_or_admin()
    async def command(interaction):  # pragma: no cover - never invoked
        ...

    (check,) = command.__discord_app_commands_checks__
    return check


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "perms",
    [
        pytest.param({"administrator": True}, id="administrator"),
        pytest.param({"manage_guild": True}, id="manage_guild"),
        pytest.param({"manage_channels": True}, id="manage_channels"),
    ],
)
async def test_each_elevated_perm_qualifies(perms):
    member = MagicMock(spec=discord.Member)
    member.guild_permissions = SimpleNamespace(
        **{"administrator": False, "manage_guild": False, "manage_channels": False, **perms}
    )
    assert await _predicate()(_interaction(member)) is True


@pytest.mark.asyncio
async def test_manage_channels_qualifies_here_but_not_for_a_game_host():
    """The one perm that separates this gate from is_host_or_mod.

    Both rules have one definition now; this pins the difference between
    them so a later tidy-up can't quietly merge them.
    """
    member = MagicMock(spec=discord.Member)
    member.guild_permissions = SimpleNamespace(
        administrator=False, manage_guild=False, manage_channels=True
    )
    interaction = _interaction(member)
    assert await _predicate()(interaction) is True
    assert is_host_or_mod(interaction, HOST_ID) is False


@pytest.mark.asyncio
async def test_plain_member_is_refused_by_the_admin_gate():
    member = MagicMock(spec=discord.Member)
    member.guild_permissions = SimpleNamespace(
        administrator=False, manage_guild=False, manage_channels=False
    )
    assert await _predicate()(_interaction(member)) is False


@pytest.mark.asyncio
async def test_refused_outside_a_guild_and_for_a_non_member():
    member = MagicMock(spec=discord.Member)
    member.guild_permissions = SimpleNamespace(administrator=True, manage_guild=True)
    assert await _predicate()(_interaction(member, in_guild=False)) is False

    user = MagicMock(spec=discord.User)
    assert await _predicate()(_interaction(user)) is False


# ── resolve_postable_channel_in_guild ─────────────────────────────────
#
# A permission check, not a convenience: channel_id arrives from a dashboard
# route and get_channel/fetch_channel search every guild the bot is in, so the
# ownership test is the only thing stopping a moderator of one guild writing
# into a co-tenant's channels. docs/sync and role_menus/sync each had it.


def _guild(gid: int):
    g = MagicMock(spec=discord.Guild)
    g.id = gid
    return g


def _channel(kind, guild=None):
    ch = MagicMock(spec=kind)
    ch.guild = guild
    return ch


def _bot(cached=None, fetched=None, fetch_error=None):
    bot = MagicMock()
    bot.get_channel = MagicMock(return_value=cached)
    bot.fetch_channel = AsyncMock(
        side_effect=fetch_error, return_value=fetched
    )
    return bot


@pytest.mark.asyncio
async def test_a_channel_in_the_right_guild_resolves():
    home = _guild(1)
    ch = _channel(discord.TextChannel, home)
    assert await resolve_postable_channel_in_guild(_bot(cached=ch), 55, home) is ch


@pytest.mark.asyncio
async def test_a_channel_in_another_guild_is_refused():
    """The whole point: a co-tenant guild's channel must not be writable."""
    ch = _channel(discord.TextChannel, _guild(999))
    assert await resolve_postable_channel_in_guild(_bot(cached=ch), 55, _guild(1)) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind",
    [
        pytest.param(discord.TextChannel, id="text"),
        pytest.param(discord.Thread, id="thread"),
        pytest.param(discord.VoiceChannel, id="voice"),
    ],
)
async def test_every_postable_kind_is_allowed(kind):
    home = _guild(1)
    ch = _channel(kind, home)
    assert await resolve_postable_channel_in_guild(_bot(cached=ch), 55, home) is ch


@pytest.mark.asyncio
async def test_an_unpostable_channel_is_refused():
    """A category or a stage channel is not somewhere a panel can go."""
    ch = _channel(discord.CategoryChannel, _guild(1))
    assert await resolve_postable_channel_in_guild(_bot(cached=ch), 55, _guild(1)) is None


@pytest.mark.asyncio
async def test_a_cache_miss_falls_back_to_one_fetch():
    home = _guild(1)
    ch = _channel(discord.TextChannel, home)
    bot = _bot(cached=None, fetched=ch)
    assert await resolve_postable_channel_in_guild(bot, 55, home) is ch
    bot.fetch_channel.assert_awaited_once_with(55)


@pytest.mark.asyncio
async def test_an_unfetchable_channel_is_refused_not_raised():
    """A deleted channel must not take down the route that asked for it."""
    bot = _bot(cached=None, fetch_error=discord.NotFound(MagicMock(status=404), "gone"))
    assert await resolve_postable_channel_in_guild(bot, 55, _guild(1)) is None


@pytest.mark.asyncio
async def test_no_guild_skips_the_ownership_check():
    """Paths acting on an already-stored placement vetted it when it was set."""
    ch = _channel(discord.TextChannel, _guild(999))
    assert await resolve_postable_channel_in_guild(_bot(cached=ch), 55, None) is ch


# ── role_ping_kwargs ──────────────────────────────────────────────────
#
# Two mistakes this is shared to stop repeating: a role mention inside an
# *embed* renders but never notifies (so it has to go in content), and
# AllowedMentions' unset fields default to allow (so the bare
# AllowedMentions(roles=[...]) form still permits @everyone and @user).


def test_a_single_role_is_mentioned_in_content():
    kwargs = role_ping_kwargs([42])
    assert kwargs["content"] == "<@&42>"


def test_only_the_named_roles_are_allow_listed():
    kwargs = role_ping_kwargs([42])
    allowed = kwargs["allowed_mentions"]
    assert [r.id for r in allowed.roles] == [42]
    # The whole point: these default to *allow* when left unset.
    assert allowed.everyone is False
    assert allowed.users is False
    assert allowed.replied_user is False


def test_several_roles_are_all_mentioned_and_all_allow_listed():
    kwargs = role_ping_kwargs([42, 43])
    assert kwargs["content"] == "<@&42> <@&43>"
    assert [r.id for r in kwargs["allowed_mentions"].roles] == [42, 43]


@pytest.mark.parametrize(
    "role_ids",
    [
        pytest.param([], id="empty"),
        pytest.param(None, id="none"),
        pytest.param([0], id="zero"),
        pytest.param(["nope"], id="not-a-number"),
        pytest.param([-1], id="negative"),
    ],
)
def test_nothing_to_ping_posts_silently(role_ids):
    """A guild with no ping role configured is a real, wanted state."""
    kwargs = role_ping_kwargs(role_ids)
    assert "content" not in kwargs
    assert kwargs["allowed_mentions"].roles is False


def test_duplicates_are_pinged_once():
    kwargs = role_ping_kwargs([42, 42, "42"])
    assert kwargs["content"] == "<@&42>"
    assert [r.id for r in kwargs["allowed_mentions"].roles] == [42]


def test_unusable_ids_are_dropped_without_losing_the_usable_ones():
    kwargs = role_ping_kwargs([0, "nope", 42, None])
    assert kwargs["content"] == "<@&42>"


# ── template_pings ────────────────────────────────────────────────────
#
# The allow-list for an admin-authored template (a greeter arrival line, a
# /grant announcement). It is derived from the *raw template* — what the
# admin typed — plus the ids the caller names, and never from the rendered
# result, because the render splices in a display name the member controls.


@pytest.mark.parametrize(
    ("template", "users", "roles", "everyone", "user_ids", "role_ids"),
    [
        # The greeter line's shipped default: the @here is the admin's and
        # has to survive, the {member} placeholder is the newcomer.
        pytest.param("@here - {member} has arrived", [500], (), True, [500], [],
                     id="here-plus-placeholder"),
        pytest.param("@everyone look", (), (), True, [], [], id="everyone"),
        pytest.param("{member} is here", [500], (), False, [500], [],
                     id="no-everyone-in-the-template"),
        # A role the admin typed into the box pings; nothing else does.
        pytest.param("<@&123> a newcomer", (), (), False, [], [123], id="written-role"),
        pytest.param("<@77> said hi", (), (), False, [77], [], id="written-user"),
        pytest.param("<@!77> said hi", (), (), False, [77], [], id="written-user-legacy"),
        # Written and passed ids merge; a role typed twice pings once.
        pytest.param("<@&123> <@&123> ping", (), [456], False, [], [123, 456],
                     id="duplicate-role-written-twice"),
        pytest.param("{member} welcome", [500, 500], (), False, [500], [],
                     id="duplicate-user-passed-twice"),
        pytest.param("{member}", [0, -1, None, "nope", 500], (), False, [500], [],
                     id="unusable-ids-dropped"),
        # A cleared box: the caller sends nothing, but the value still means
        # "suppress everything" rather than deferring to a client default.
        pytest.param("", [500], (), False, [500], [], id="blank-template"),
        pytest.param(None, (), (), False, [], [], id="none-template"),
    ],
)
def test_template_pings_derives_the_allow_list_from_the_template(
    template, users, roles, everyone, user_ids, role_ids
):
    allowed = template_pings(template, users=users, roles=roles)
    assert allowed.everyone is everyone
    assert [o.id for o in allowed.users] == user_ids
    assert [o.id for o in allowed.roles] == role_ids
    # Every field pinned: an unset one is a deferral to the sending client,
    # not a False (see allow_pings' docstring).
    assert allowed.replied_user is False
    assert None not in (allowed.everyone, allowed.users, allowed.roles,
                        allowed.replied_user)


def test_a_hostile_display_name_cannot_widen_the_allow_list():
    """The helper never sees the rendered line — only the admin's template.

    A newcomer named ``@everyone`` lands in the *rendered* text via
    ``{member_name}``. Deriving from the template is what makes renaming
    yourself unable to buy a server-wide ping; the renderers escape the
    name as well, but this is the guard that holds without remembering to.
    """
    template = "Welcome {member_name}, say hi to {member}!"
    rendered = template.replace("{member_name}", "@everyone").replace(
        "{member}", "<@500>"
    )
    assert "@everyone" in rendered  # it really is in what gets sent

    allowed = template_pings(template, users=[500])

    assert allowed.everyone is False
    assert [o.id for o in allowed.users] == [500]


# ── ExpiringView ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_expiring_view_on_timeout_disables_items_and_edits_stored_message():
    view = ExpiringView(timeout=1)
    button: discord.ui.Button = discord.ui.Button(label="Go")  # type: ignore[assignment]
    view.add_item(button)
    view.message = AsyncMock(spec=discord.Message)

    await view.on_timeout()

    assert button.disabled is True
    view.message.edit.assert_awaited_once_with(view=view)


@pytest.mark.asyncio
async def test_expiring_view_on_timeout_noop_without_a_stored_message():
    view = ExpiringView(timeout=1)
    button: discord.ui.Button = discord.ui.Button(label="Go")  # type: ignore[assignment]
    view.add_item(button)

    await view.on_timeout()  # must not raise with self.message still None

    assert button.disabled is True


@pytest.mark.asyncio
async def test_expiring_view_on_timeout_swallows_a_deleted_message():
    view = ExpiringView(timeout=1)
    message = AsyncMock(spec=discord.Message)
    message.edit.side_effect = discord.HTTPException(MagicMock(), "deleted")
    view.message = message

    await view.on_timeout()  # a member dismissing the ephemeral first is routine


# ── MemberScoped ──────────────────────────────────────────────────────
#
# Fifteen views hand-rolled the same "is this the member it was opened for"
# interaction_check, each with its own attribute name; the copy is the only
# part that genuinely differs, so it is the only part left per view.

_OWNER = 500


class _Scoped(MemberScoped, discord.ui.View):
    SCOPE_DENIAL = "❌ Not yours."

    def __init__(self) -> None:
        super().__init__(timeout=None)
        self.scope_user_id = _OWNER


class _SilentScoped(_Scoped):
    SCOPE_DENIAL = None


class _ScopedLayout(MemberScoped, discord.ui.LayoutView):
    def __init__(self) -> None:
        super().__init__(timeout=None)
        self.scope_user_id = _OWNER


def _press(user_id: int):
    interaction = _send_interaction(responded=False)
    interaction.user = SimpleNamespace(id=user_id)
    return interaction


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "view_cls, presser, allowed, said",
    [
        pytest.param(_Scoped, _OWNER, True, None, id="owner-passes-quietly"),
        pytest.param(_Scoped, 99, False, "❌ Not yours.", id="stranger-gets-the-views-copy"),
        pytest.param(_SilentScoped, 99, False, None, id="none-refuses-silently"),
        pytest.param(_ScopedLayout, 99, False, "❌ This isn't for you.", id="layout-view-default-copy"),
        pytest.param(_ScopedLayout, _OWNER, True, None, id="layout-view-owner"),
    ],
)
async def test_member_scoped_gate(view_cls, presser, allowed, said):
    interaction = _press(presser)
    assert await view_cls().interaction_check(interaction) is allowed
    if said is None:
        interaction.response.send_message.assert_not_awaited()
    else:
        interaction.response.send_message.assert_awaited_once_with(said, ephemeral=True)


def test_member_scoped_wins_the_mro():
    """Listed first, the mixin's check is the one discord.py calls."""
    assert _Scoped.interaction_check is MemberScoped.interaction_check
    assert _ScopedLayout.interaction_check is MemberScoped.interaction_check


def _hand_rolled_owner_checks(sources: dict[str, str]) -> list[str]:
    """``interaction_check`` bodies comparing ``interaction.user.id`` to ``self.X``."""
    import ast

    found = []
    for path, text in sources.items():
        for node in ast.walk(ast.parse(text)):
            if not (isinstance(node, ast.ClassDef)):
                continue
            for fn in node.body:
                if not (isinstance(fn, ast.AsyncFunctionDef) and fn.name == "interaction_check"):
                    continue
                for cmp in ast.walk(fn):
                    if isinstance(cmp, ast.Compare) and any(
                        ast.unparse(side) == "interaction.user.id" for side in (cmp.left, *cmp.comparators)
                    ) and any(
                        ast.unparse(side).startswith("self.") for side in (cmp.left, *cmp.comparators)
                    ):
                        found.append(f"{path}::{node.name}")
                        break
    return found


def test_no_view_hand_rolls_the_owner_check():
    """A new one-member view inherits MemberScoped instead of re-typing it."""

    src = REPO_ROOT / "src"
    sources = {
        p.relative_to(src).as_posix(): p.read_text(encoding="utf-8") for p in src.rglob("*.py")
    }
    offenders = [
        o for o in _hand_rolled_owner_checks(sources)
        if o != "bot_modules/core/utils.py::MemberScoped"  # the one real copy
    ]
    assert not offenders, (
        "These views compare interaction.user.id to an attribute in their own "
        "interaction_check. Inherit bot_modules.core.utils.MemberScoped, set "
        "scope_user_id, and put the refusal copy in SCOPE_DENIAL:\n  "
        + "\n  ".join(offenders)
    )


@pytest.mark.parametrize(
    "body, flagged",
    [
        pytest.param("return interaction.user.id == self._owner_id", True, id="bare-equality"),
        pytest.param(
            "if interaction.user.id != self.user_id:\n            return False\n        return True",
            True,
            id="negated-guard",
        ),
        pytest.param("return is_host_or_mod(interaction, self.host_id)", False, id="host-or-mod-is-not-this"),
        pytest.param("return True", False, id="no-comparison"),
    ],
)
def test_the_owner_check_sweep(body, flagged):
    source = f"""
class V:
    async def interaction_check(self, interaction):
        {body}
"""
    assert bool(_hand_rolled_owner_checks({"v.py": source})) is flagged
