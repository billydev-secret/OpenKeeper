"""Every role Dungeon Keeper makes for itself, in one place.

Round 1 (2026-08-26) registered five ping dials here and left nine more roles
scattered across their call sites, each building its own ``RoleSpec`` inline.
That is where the often-quoted "only 5 of 44 dials are safe to auto-create"
came from, and it was true about *this file* and misleading about *the bot*.
The honest sentence, and the one the dashboard now says:

    Dungeon Keeper makes up to **twenty-four** named roles for itself, across
    **twenty-two** dials and three mechanisms, plus one more for every member who
    buys a personal role in the perk shop.

Fourteen roles were the count before round 2; ``guess_role_id`` and
Voice Control's spectate gate brought it to sixteen (see *create-on-offer*
below), the game lobby ping (2026-09-04) to seventeen, and Take a Break's
hold role (2026-09-26) to eighteen, and the staff job pings
(2026-09-28, ``docs/specs/staff_pings_spec.md``) — bump, tickets, chores, Chat Revive, QOTD — to
twenty-three, Staff · Greetings (2026-09-30) to twenty-four, and Staff ·
Spicy QOTD (2026-09-30) to twenty-five; retiring Staff · Bump (2026-09-30,
it duplicated the Bump Tracker's own reminder) took it back to twenty-four. The perk
shop's per-member roles are the unbounded class and are deliberately **not**
here: their name comes from a member, and adopt-by-name
plus a member-chosen name is privilege escalation
(``core/role_provision`` module docstring).

:data:`MANAGED_ROLES` is the whole set. Nothing else may enumerate the bot's
roles — the roster page (``bot-roles``) is the only surface that can show them all
together, and a role that is not in this tuple is invisible on it,
which is exactly the state round 2 exists to end.

**Membership rules.** A dial belongs here only if the role exists *because the
feature exists* and holding it grants nothing by itself. Three families stay
out, permanently:

* **Ownership** — the role is the guild's, not the bot's. ``opt_in_role_id`` is
  the server's main membership role; ``role_menus`` options, ``grant_roles``,
  ``intake_cards.auto_role_id`` (a *watcher*: a bot-made role is a step that
  can never tick). Creating one makes a twin and the feature silently stops
  matching the real role.
* **Authority** — the dial names who may *act*. ``mod_role_ids``,
  ``admin_role_ids``, ``greeter_role_id``, ``economy_manager_role_id`` and
  their kin. An empty ``@Moderator`` reads as configured and grants nobody
  anything, which is the worst failure available.
* **Storage that cannot say "never configured"** — ``bump_tracker_config``'s
  ``role_id`` is ``NOT NULL DEFAULT 0`` and Chat Revive's is nullable with a
  "(none)" that saves NULL, so a decision and a blank are the same bytes. This
  one is *removable* rather than permanent: ``bot_managed_roles``
  (migration 203) answers "did the bot ever provision this" without the dial's
  own column having to. Reopening either dial is still its own piece of work.

**Create-on-offer.** Two dials are here but must never be provisioned lazily on
first use: ``guess_role_id`` and ``voice_master_spectator_gate_role_id``. An
empty gate role is worse than no gate role — it flips the feature from honestly
off to configured-and-refusing (Guess) or hands a spectate room to nobody
(Voice). They are safe only because creation now happens in the *same action*
that offers the role to members, through Discord onboarding. Anything that
provisions a ``create_on_offer`` entry outside that action is a bug; see
``routes/bot_roles.py``, which refuses it, and the test that pins the refusal.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from bot_modules.core.role_provision import RoleSpec

#: Where a role's id is stored. Only ``config`` can tell "never set" from
#: "(none)", which is why every dial the bot provisions lazily lives there.
SOURCE_CONFIG = "config"
SOURCE_DM_MODE = "dm_mode"
SOURCE_SURVIVOR = "survivor"
SOURCE_WELLNESS = "wellness"


@dataclass(frozen=True)
class FeatureRole:
    """One role the bot may provision, and everything a surface needs about it."""

    #: Stable identity. For a ``config``-KV dial this **is** the config key
    #: (``welcome_ping_role_id``); for the others it is a synthetic key that
    #: never changes, because ``bot_managed_roles`` rows and the roster page's
    #: buttons are addressed by it.
    key: str
    #: Human name for the feature, used in the mod-log line on a recreate.
    feature: str
    #: What the role should look like when it has to be made.
    spec: RoleSpec
    #: One line describing the role to a member choosing it in Discord's
    #: onboarding. Kept to Discord's 100-character option-description cap.
    blurb: str = ""
    #: Suggested emoji for the onboarding option, and the roster card's icon.
    emoji: str = ""
    #: Whether a stored ``0`` means "an admin turned this off" and must be left
    #: alone. False only where there is no coherent "off": a jail with no role
    #: is not a jail.
    none_means_off: bool = True
    #: Whether a missing row falls back to the legacy ``guild_id=0`` row, which
    #: has to match how the feature itself reads the key or we'd provision a
    #: role for a guild that is really inheriting an answer. Economy settings
    #: are guild-scoped only.
    legacy_fallback: bool = True
    #: Where the id lives — one of the ``SOURCE_*`` constants above.
    source: str = SOURCE_CONFIG
    #: The bot adds and removes this role from members, so it must sit **below**
    #: the bot's own top role and a same-named role above it must not be
    #: adopted. False for a role the bot only mentions or only names in a
    #: channel overwrite: hierarchy is irrelevant there.
    assigns: bool = False
    #: Members pick this one up themselves — so it can be offered in Discord's
    #: onboarding, and the onboarding panel lists it.
    opt_in: bool = False
    #: Never provision this except in the same action that offers it to
    #: members. See the module docstring.
    create_on_offer: bool = False
    #: A staff job ping: moderators opt in on the Mod Settings panel
    #: (``mod_settings``), never members. Kept out of :data:`CONFIG_ROLES`, so
    #: neither Discord onboarding nor the member Roles & Pings card offers it.
    staff: bool = False
    #: One sentence: when the role gets made, in the admin's terms.
    made_when: str = ""
    #: Frozen dashboard route id of the page owning the dial, and its label.
    panel: str = ""
    panel_label: str = ""
    #: The field's own label on that page, for the "Set on X → Y" line.
    dial_label: str = ""

    @property
    def honours_none(self) -> bool:
        """Whether a stored ``0`` on this dial is a decision to make no role.

        ``none_means_off`` alone is not the whole answer, and every surface that
        asks "did the admin switch this off" has to use this instead — the
        roster page, the per-dial state line and Discord Onboarding all read the
        same dial and must not disagree about it.

        A ``create_on_offer`` dial is never "off": offering the role in
        onboarding *is* the decision that makes it exist, and its own panel
        writes ``"0"`` here on every unrelated whole-form save. Reading that 0
        as a preference would tell an admin '"(none)" — so I won't make one'
        directly underneath the hint telling them offering it in onboarding
        will, and onboarding would then make it anyway.
        """
        return self.none_means_off and not self.create_on_offer


def _ping(
    key: str,
    feature: str,
    name: str,
    *,
    blurb: str = "",
    emoji: str = "",
    legacy_fallback: bool = True,
    none_means_off: bool = True,
    assigns: bool = False,
    staff: bool = False,
    made_when: str = "",
    panel: str = "",
    panel_label: str = "",
    dial_label: str = "",
) -> FeatureRole:
    """A ping-only role: no permissions, and not mentionable.

    Not mentionable is deliberate — the bot pings these through
    ``AllowedMentions(roles=[role])``, which does not need the bit, and leaving
    it off stops any member from mass-pinging the subscriber list by hand.
    """
    return FeatureRole(
        key=key,
        feature=feature,
        blurb=blurb,
        emoji=emoji,
        legacy_fallback=legacy_fallback,
        none_means_off=none_means_off,
        assigns=assigns,
        opt_in=True,
        staff=staff,
        made_when=made_when,
        panel=panel,
        panel_label=panel_label,
        dial_label=dial_label,
        spec=RoleSpec(
            name=name,
            reason=f"Dungeon Keeper {feature} setup",
            mentionable=False,
        ),
    )


# ── the five ping dials (round 1) ────────────────────────────────────

WELCOME_PING = _ping(
    "welcome_ping_role_id", "welcome messages", "Welcome Ping",
    blurb="Get a nudge when somebody new arrives, so you can say hello.",
    emoji="👋",
    made_when="the first time somebody new joins and the welcome post goes up",
    panel="config-welcome", panel_label="Welcome & Leave",
    dial_label="Welcome Ping Role",
)
QOTD_PING = _ping(
    "econ_qotd_ping_role_id", "the question of the day", "QOTD",
    blurb="Be told when the question of the day goes up.",
    emoji="💬",
    legacy_fallback=False,
    # Nothing provisions this lazily since `/qotd post` went (2026-09): a
    # question is opened by tagging the role, so the role has to exist first.
    made_when="when you press Make it now",
    panel="economy-qotd-submissions", panel_label="QOTD",
    dial_label="Ping Role",
)
RISKY_PING = _ping(
    "risky_ping_role_id", "the Risky Rolls round ping", "Risky Rolls",
    blurb="Get pinged when a Risky Rolls round opens.",
    emoji="🎲",
    made_when="the next time a Risky Rolls round opens with its ping switched on",
    panel="config-risky-rolls", panel_label="Risky Rolls",
    dial_label="Ping Role",
)
# 2026-09-04 (discovery-2 / clapback-11, decision D4): the one ping every
# lobby game shares. The start-ping sweep (``game_start_ping_service``) posts
# it once per lobby the moment the lobby's board exists — member launches and
# scheduled ones alike — so no game cog has to know it exists.
GAME_LOBBY_PING = _ping(
    "game_lobby_ping_role_id", "the game lobby ping", "Game Lobbies",
    blurb="Get pinged when a party-game lobby opens.",
    emoji="🎮",
    made_when="the next time a party-game lobby opens (Clapback, Most Likely To, …)",
    panel="games-config", panel_label="Games Global Config",
    dial_label="Game Lobby Ping Role",
)
PROMOTION_REVIEW_PING = _ping(
    "promotion_review_ping_role_id", "promotion reviews", "Promotion Reviewers",
    blurb="Be told when someone comes up for a promotion review.",
    emoji="📋",
    # Mods switch it on themselves on the Mod Settings panel, which hands it out.
    assigns=True,
    staff=True,
    made_when="the next time somebody comes up for a promotion review",
    panel="promotion-reviews", panel_label="Promotion Reviews",
    dial_label="Ping Role",
)


def _staff_ping(key: str, feature: str, name: str, *, blurb: str, emoji: str) -> FeatureRole:
    """A staff job ping (``docs/specs/staff_pings_spec.md``): a moderator turns it
    on from the Mod Settings panel, which makes the role and hands it out, and the
    bot mentions it in the staff ping channel when the job comes up."""
    return _ping(
        key, feature, name,
        blurb=blurb,
        emoji=emoji,
        assigns=True,
        staff=True,
        made_when="the first time a moderator turns it on from the Mod Settings panel",
        panel="config-moderation", panel_label="Moderation & Privacy",
        dial_label=f"Staff Pings → {name}",
    )


STAFF_TICKET_PING = _staff_ping(
    "staff_ping_tickets_role_id", "the staff ticket ping", "Staff · Tickets",
    blurb="Be pinged when a member opens a ticket.",
    emoji="📩",
)
STAFF_CHORES_PING = _staff_ping(
    "staff_ping_chores_role_id", "the staff chores ping", "Staff · Chores",
    blurb="Be pinged when a recurring chore lands on the todo board.",
    emoji="🧹",
)
STAFF_REVIVE_PING = _staff_ping(
    "staff_ping_revive_role_id", "the staff Chat Revive ping", "Staff · Chat Revive",
    blurb="Be pinged when Chat Revive finds a lull it can't fill, or any lull when "
    "set to ping mods.",
    emoji="💬",
)
STAFF_QOTD_PING = _staff_ping(
    "staff_ping_qotd_role_id", "the staff QOTD ping", "Staff · QOTD",
    blurb="Be pinged when no question of the day is up by the deadline.",
    emoji="❓",
)
STAFF_SPICY_QOTD_PING = _staff_ping(
    "staff_ping_spicy_qotd_role_id", "the staff spicy QOTD ping", "Staff · Spicy QOTD",
    blurb="Be pinged when no spicy question of the day is up by the deadline.",
    emoji="🌶️",
)
STAFF_GREETING_PING = _staff_ping(
    "staff_ping_greetings_role_id", "the staff greeting ping", "Staff · Greetings",
    blurb="Be pinged when a hello in chat goes unanswered.",
    emoji="👋",
)
ECONOMY_NOTIFY = _ping(
    "econ_game_role_id", "economy notifications", "Economy Notifications",
    blurb="Get your streak digest and event alerts in your DMs.",
    emoji="🔔",
    legacy_fallback=False,
    assigns=True,
    made_when="the first time a member presses 🔔 on the how-it-works panel",
    panel="economy-config", panel_label="Economy Settings",
    dial_label="Notifications Role",
    # 2026-09-03, Billy: **"(none)" is honoured.** This reverses the
    # 2026-08-22 call that the 🔔 button should always work. Until now this
    # dial was the one entry with ``none_means_off=False``: the panel told the
    # admin "(none)" turned economy notifications off while ``economy/guide.py``
    # provisioned the role anyway — a preference the code did not enforce,
    # which CLAUDE.md forbids outright. With "(none)" stored, 🔔 now tells the
    # member notifications aren't set up in this server.
    #
    # The hazard the old exception was guarding against is real and has not
    # gone away: ``economy-config.js`` saves the whole form, so an untouched
    # picker writes ``"0"`` here on every save. The fix for *that* is on the
    # panel — the dial now says which of the three "(none)" cases it is in,
    # reading ``bot_managed_roles`` — not a code path that overrides the
    # admin.
    none_means_off=True,
)

# ── the two dials round 2 reopened, create-on-offer ───────────────────

GUESS_ROLE = FeatureRole(
    key="guess_role_id",
    feature="the Guess Who game",
    spec=RoleSpec(name="Guess Who", reason="Dungeon Keeper Guess Who setup"),
    blurb="Play Guess Who: submit your own photos and guess at everyone else's.",
    emoji="🖼️",
    assigns=True,
    opt_in=True,
    create_on_offer=True,
    made_when="when you offer it to members in Discord's Channels & Roles screen",
    panel="config-guess", panel_label="Guess Who",
    dial_label="Required Role",
)
VOICE_SPECTATE_GATE = FeatureRole(
    key="voice_master_spectator_gate_role_id",
    feature="voice spectator rooms",
    spec=RoleSpec(
        name="Voice Spectator",
        reason="Dungeon Keeper voice spectator gate setup",
    ),
    blurb="Get into spectator voice rooms to listen in on a hosted session.",
    emoji="🎧",
    # The bot never hands this one out; it names the role in the spectate
    # room's channel overwrites. Members pick it up themselves, which is why it
    # is opt-in without being assigned.
    assigns=False,
    opt_in=True,
    create_on_offer=True,
    made_when="when you offer it to members in Discord's Channels & Roles screen",
    panel="config-voice-master", panel_label="Voice Control",
    dial_label="Spectator Gate Role",
)

# ── the nine roles features made on their own ─────────────────────────
#
# Until round 2 each of these built its RoleSpec inline at its call site, so
# nothing could list them and a missing one was invisible until it failed.
# The call sites now read their spec from here.

JAILED_ROLE = FeatureRole(
    key="jailed_role_id",
    feature="the jail",
    spec=RoleSpec(name="Jailed", reason="Dungeon Keeper jail system setup"),
    emoji="🔒",
    assigns=True,
    # A jail with no role is not a jail, so a stored 0 cannot mean "off" here —
    # it means "not set up yet" and the next jail sets it up.
    none_means_off=False,
    made_when="the first time you jail somebody",
    panel="config-moderation", panel_label="Moderation & Privacy",
    dial_label="Jailed Role",
)
INACTIVE_ROLE = FeatureRole(
    key="inactive_role_id",
    feature="the inactive sweep",
    spec=RoleSpec(name="Inactive", reason="Dungeon Keeper inactive-channel setup"),
    emoji="💤",
    assigns=True,
    none_means_off=False,
    made_when="the first time somebody is marked inactive",
    panel="config-inactive", panel_label="Inactive Kick Sweep",
    dial_label="",
)
BREAK_ROLE = FeatureRole(
    key="break_role_id",
    feature="Take a Break",
    spec=RoleSpec(name="On a Break", reason="Dungeon Keeper break-channel setup"),
    emoji="💤",
    assigns=True,
    # Take a Break reads its keys guild-scoped only (breaks.store).
    legacy_fallback=False,
    # A stored 0 (Stop managing on the roster) is honoured: with no break
    # role, breaks are off (``BreakConfig.enabled``) and setup refuses.
    made_when="when you press Set up break channel",
    panel="take-a-break", panel_label="Take a Break",
    dial_label="",
)


def _dm_mode(mode: str, name: str, emoji: str) -> FeatureRole:
    """One of the three DM-mode roles.

    ``ensure_dm_roles`` is reached from a member's button click and holds
    neither an AppContext nor a db_path, so it stores nothing and records no
    provenance — the adopt-by-name step finds the role again next pass. The
    roster falls back to inference for these three, and says so.
    """
    return FeatureRole(
        key=f"dm_mode_{mode}_role_id",
        feature="DM permissions",
        spec=RoleSpec(name=name, reason="DM permission system"),
        emoji=emoji,
        assigns=True,
        made_when="the first time a member picks that DM mode",
        panel="config-dms", panel_label="DM Permissions",
        source=SOURCE_DM_MODE,
    )


DM_OPEN_ROLE = _dm_mode("open", "DMs: Open", "📬")
DM_ASK_ROLE = _dm_mode("ask", "DMs: Ask", "📪")
DM_CLOSED_ROLE = _dm_mode("closed", "DMs: Closed", "🚫")


def _survivor(key: str, name: str, emoji: str) -> FeatureRole:
    return FeatureRole(
        key=key,
        feature="Survivor",
        spec=RoleSpec(name=name, reason="Survivor season setup"),
        emoji=emoji,
        assigns=True,
        made_when="when you create a Survivor season",
        panel="survivor", panel_label="Survivor",
        source=SOURCE_SURVIVOR,
    )


SURVIVOR_ROLE = _survivor("role_survivor_id", "🏈 Survivor", "🏈")
SURVIVOR_GHOST_ROLE = _survivor("role_ghost_id", "👻 Ghost", "👻")
SURVIVOR_SOLE_ROLE = _survivor("role_sole_survivor_id", "🏈 Sole Survivor", "🏆")

WELLNESS_ROLE = FeatureRole(
    key="wellness_role_id",
    feature="Wellness",
    spec=RoleSpec(
        name="Wellness Guardian",
        reason="Wellness activation from the dashboard",
    ),
    emoji="🌱",
    assigns=True,
    made_when='when you press Activate Wellness and pick "create one for me"',
    panel="config-wellness", panel_label="Wellness",
    source=SOURCE_WELLNESS,
)


#: Every role the bot can make for itself. Twenty-four across twenty-two dials.
MANAGED_ROLES: tuple[FeatureRole, ...] = (
    WELCOME_PING,
    QOTD_PING,
    RISKY_PING,
    GAME_LOBBY_PING,
    PROMOTION_REVIEW_PING,
    STAFF_TICKET_PING,
    STAFF_CHORES_PING,
    STAFF_REVIVE_PING,
    STAFF_QOTD_PING,
    STAFF_SPICY_QOTD_PING,
    STAFF_GREETING_PING,
    ECONOMY_NOTIFY,
    GUESS_ROLE,
    VOICE_SPECTATE_GATE,
    JAILED_ROLE,
    INACTIVE_ROLE,
    BREAK_ROLE,
    DM_OPEN_ROLE,
    DM_ASK_ROLE,
    DM_CLOSED_ROLE,
    SURVIVOR_ROLE,
    SURVIVOR_GHOST_ROLE,
    SURVIVOR_SOLE_ROLE,
    WELLNESS_ROLE,
)

#: The dials a member can opt into, and therefore the ones Discord's onboarding
#: can offer. All live in the ``config`` KV — not a coincidence: it is the only
#: store where a never-set dial is distinguishable from one an admin set to
#: "(none)". Staff pings are opt-in too, but for moderators only: offering one
#: in onboarding would hand every newcomer a mod ping.
CONFIG_ROLES: tuple[FeatureRole, ...] = tuple(
    entry for entry in MANAGED_ROLES if entry.opt_in and not entry.staff
)

#: The staff job pings, in Mod Settings panel display order.
STAFF_ROLES: tuple[FeatureRole, ...] = tuple(
    entry for entry in MANAGED_ROLES if entry.staff
)

#: Keyed lookup for both, since every surface wants one.
BY_KEY: dict[str, FeatureRole] = {entry.key: entry for entry in MANAGED_ROLES}


def dm_mode_role(mode: str) -> FeatureRole:
    """The registry entry for a DM mode ("open"/"ask"/"closed")."""
    return BY_KEY[f"dm_mode_{mode}_role_id"]


def spec_for(key: str, *, name: str | None = None) -> RoleSpec:
    """The registry's ``RoleSpec`` for ``key``, optionally renamed.

    Call sites read their spec from here rather than building one inline, so
    the roster page and the provisioner can never disagree about what a role is
    called. ``name`` is for Survivor, whose season config may carry a role name
    the guild has since changed.
    """
    entry = BY_KEY[key]
    return entry.spec if name is None else replace(entry.spec, name=name)
