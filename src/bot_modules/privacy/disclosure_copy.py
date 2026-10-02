"""Member-facing copy for the GDPR disclosure report.

The data register (``docs/data_register.md``) knows *what* every table holds,
how long it is kept and whether erasure clears it. It does not speak to a
member: its cells read ``user_id + amounts + timestamps; meta sampled clean``.

This module supplies the other half — the ~15 categories a member can actually
read, and one authored paragraph each. The report joins the two: register facts,
copy language.

Adding a table to the register therefore needs no change here *unless* it brings
a new ``Feature`` value with it. ``tests/privacy/test_disclosure_service.py`` hard-fails
when a register Feature maps to no category, so a new feature cannot slip into a
member's report as an uncategorised table name.
"""

from __future__ import annotations

from dataclasses import dataclass


#: What any erasure of a member's Guess Who data takes, and what it leaves.
#: Shared with the My Data → Guess Who button's confirm
#: (``privacy.logic.GUESS_STORE``), so the notice and the irreversible click
#: can never describe two different deletions. Both sentences are lower-case
#: led to follow a colon here; the confirm capitalises the first.
GUESS_ROUNDS_ERASED = (
    "a round you submitted, or one whose answer was you, is deleted whole — "
    "the photo, its post in the Guess channel, the text of a confession round, "
    "and every guess made on it."
)
GUESS_SOLVER_SURVIVES = (
    "If you solved somebody else's round, that round stays but stops naming "
    "you as the solver."
)


@dataclass(frozen=True)
class Category:
    """One section of the member-facing report."""

    key: str
    title: str
    # What this is, in the member's terms. Second person, no table names.
    blurb: str
    # Why the server holds it at all — the Art 13(1)(c) purpose, plainly put.
    purpose: str
    # What survives an erasure request here, and on what ground. Authored
    # rather than lifted from the register: the register's Purge? cells carry
    # commit hashes, table names, live row counts and notes-to-self, none of
    # which belongs in a document sent to a member.
    preserved: str = ""


# Order is the order they appear in the report. Deliberately front-loaded with
# the categories a member is most likely to have come to ask about.
CATEGORIES: tuple[Category, ...] = (
    Category(
        "messages",
        "Your messages",
        "A record that each message happened: which channel you posted in and "
        "when, who you were replying to, who you @-mentioned, the reactions it "
        "received, an automatic sentiment score, and whether it carried an "
        "image or video. The bot does not keep the text of your messages, or "
        "the files and links in them — it stopped storing them in September "
        "2026 and erased what it had. A message you delete in Discord has its "
        "record marked deleted, with the time, rather than removed.",
        "This record powers the server's activity and XP features, the "
        "moderation record, and features like quotes and the starboard.",
        "Nothing — the whole record goes. A moderator handling a narrower "
        "request (one about your profile and activity only) can choose to "
        "leave the bare activity log in place; that is the most that could "
        "remain, and it is never any text."
    ),
    Category(
        "activity",
        "Your activity and levels",
        "A running record of the XP you have earned — from messages, from "
        "reactions, and from time spent in voice channels — plus the daily "
        "totals it rolls up into, and your current level.",
        "This is what the levelling system is: without it your level could not "
        "be calculated or shown on a leaderboard.",
        "Nothing. Your XP history and level are erased."
    ),
    Category(
        "economy",
        "Your coins and purchases",
        "Your wallet balance and every transaction behind it: coins earned, "
        "spent, gifted and won, daily login streaks, quests you completed, "
        "items you bought, and any custom shop items or themes you own.",
        "A currency needs a ledger. Both sides of every transfer are kept so "
        "balances stay correct and disputes can be settled.",
        "The transaction ledger. A transfer has two sides, and deleting only yours would corrupt the balance of the person you traded with. It is kept as a financial record, not as a profile of you."
    ),
    Category(
        "games",
        "Games you have played",
        "Records from the games you have taken part in — rounds played, hands "
        "dealt, results, scores and rankings across the casino, duels, Mahjong, "
        "Guess, Risky Rolls, Survivor and the other game features.",
        "Game history keeps scores, leaderboards and statistics working, and "
        "lets a game resume rather than restart.",
        "Records of completed games where a result involved other players, so that their own game history and scores stay intact. "
        f"Guess Who is the exception: {GUESS_ROUNDS_ERASED} {GUESS_SOLVER_SURVIVES} "
        "You can clear your Guess Who data yourself at any time from the Guess Who button under My Data on the Help & Settings panel."
    ),
    Category(
        "social",
        "Who you interact with",
        "Moments where you and another member were publicly linked: @-mentions "
        "that earned an award, messages you starred or that were starred, "
        "quote cards saved from something someone said, and role pings you "
        "sent. The bot no longer keeps a general map of who talks to, replies "
        "to or reacts to whom, or who follows whom between voice channels — "
        "those records were removed in September 2026.",
        "These drive features such as Mention Awards, the starboard and quote "
        "cards, and the report on how a role ping landed.",
        "If you set up a Mention Award rule yourself, the record that you created it stays — it is the account of who opened a coin faucet, kept alongside the ledger entries it produced. The award chips naming you are removed."
    ),
    Category(
        "voice",
        "Voice channels",
        "Time spent in voice, the rooms you own or are trusted in, and your "
        "voice room preferences. Where a channel has transcription switched "
        "on, a voice note you post there is transcribed and the text posted "
        "into that channel as an ordinary message — the bot keeps no copy of "
        "the transcript of its own.",
        "Voice time earns XP, and room ownership and trust lists are what let "
        "you manage your own voice channel.",
        "Where another member has blocked you from their own voice room, that entry stays. Your own block and trust lists are removed — those are your preferences about your room — but a block somebody else placed is their protection, and erasing it at the request of the person it excludes would defeat it."
    ),
    Category(
        "profile",
        "Things you told us about yourself",
        "Information you entered yourself: your bio and its answers, your "
        "birthday if you set one, your music preferences, and settings you have "
        "chosen for individual features.",
        "You provided these so the server could show or use them. Nothing here "
        "was collected without you typing it in.",
        "Nothing. Everything you entered about yourself is erased."
    ),
    Category(
        "anonymous",
        "Anonymous posts you have made",
        "Whispers, confessions and other anonymous posts. These appear to "
        "others without your name — but the server does keep the link between "
        "you and what you posted, so that abuse can be dealt with.",
        "The link exists solely so an anonymous feature cannot be used to "
        "harass people with no accountability. The audit link is deleted after "
        "90 days by default, and the record that routes replies to a confession "
        "after 14; whispers are kept until they are cleared.",
        "Almost nothing. An erasure deletes your whispers, and you can clear them yourself at any time from the Whisper history button under My Data on the Help & Settings panel. The audit link between you and your other anonymous posts is erased with them. Two things stay: a report you filed about somebody else's whisper, and a row recording that somebody guessed you on a third party's whisper. Both are about another member's message rather than yours."
    ),
    Category(
        "penpals",
        "Pen Pals",
        "Whether you opted in or out, who you were matched with, and any "
        "blocks you set.",
        "Matching people needs a record of who is in the pool and who should "
        "not be paired.",
        "A block someone else placed against you, so that it continues to protect them. Your own blocks and your opt-out are erased, so re-block or opt out again if you rejoin the pool and do not want to be matched."
    ),
    Category(
        "wellness",
        "Wellbeing tools",
        "Your use of the wellness features: whether you opted in, usage limits, "
        "streaks, quiet periods, weekly summaries, and a break you are taking "
        "(the roles held back to give you on your return).",
        "These features are opt-in and their limits and streaks only work if "
        "your own usage is remembered.",
        "Nothing. Your wellness data is erased."
    ),
    Category(
        "moderation",
        "Your moderation record",
        "Warnings, mutes, jails, and tickets you opened or were named in.",
        "A moderation record is the evidence behind decisions taken about your "
        "account, and the basis on which one could be challenged or reversed.",
        "Warnings, mutes, jails and tickets. These are the evidence behind decisions taken about your account: if a decision were ever challenged, this is the record it would be judged on. It is kept for that reason and no other."
    ),
    Category(
        "roles",
        "Roles and permissions",
        "Roles you have been granted, role menus you have used, permissions "
        "given to you over particular features, and your direct-message "
        "preferences.",
        "The server needs to know what you have access to in order to grant or "
        "refuse it.",
        "The record of permissions granted to you over server features, kept as the counterpart to whatever those permissions were then used to do; and records of direct-message consent between you and other members, which are often the other person's evidence."
    ),
    Category(
        "joining",
        "Joining and onboarding",
        "When you joined, how your onboarding progressed, greeting checks, and "
        "records relating to periods of inactivity.",
        "Onboarding has to remember where you got to, and inactivity handling "
        "needs to know when you were last seen.",
        "The fact that a join or an inactivity sweep happened, with your identity removed from it. Your own welcome card is deleted along with it. If you greeted somebody else, your name stays on their card as the person who did."
    ),
    Category(
        "usage",
        "How you use the bot",
        "Which commands and dashboard panels you have used, and when.",
        "Usage figures show which features are worth keeping and which are not "
        "working. They are about the features, not about judging you.",
        "Nothing. Your usage history is erased."
    ),
    Category(
        "admin",
        "Server administration",
        "Records created because of things you did as a moderator or admin — "
        "settings you changed, items you configured, tasks you signed off — "
        "along with small internal records that do not fit the categories above.",
        "These are the record of who configured a shared server surface, and "
        "the counterpart to whatever that surface then did.",
        "Records of settings you changed as a moderator or admin. These are about the server's configuration rather than about you, and they are the counterpart to whatever that setting then did."
    ),
)

CATEGORY_BY_KEY = {c.key: c for c in CATEGORIES}

# Register ``Feature`` cell -> category key. Every distinct Feature value in
# docs/data_register.md must appear here; the coverage test enforces it.
FEATURE_TO_CATEGORY: dict[str, str] = {
    "Message archive": "messages",
    "XP/activity": "activity",
    "XP rollup (migration 186)": "activity",
    "Economy": "economy",
    "Economy — Custom shop items": "economy",
    "Economy — Flash Themes": "economy",
    "Economy — emoji sponsorship": "economy",
    "Economy — live login digest": "economy",
    "Economy — quests & QOTD": "economy",
    "Casino": "games",
    "Games": "games",
    "Games — duels, group Hot Potato": "games",
    "External games": "games",
    "Meadow Mahjong": "games",
    "Guess": "games",
    "Risky Rolls": "games",
    "Survivor": "games",
    "Mention Awards": "social",
    "Starboard": "social",
    "Quote cards": "social",
    "Ping Response report": "social",
    "Voice Master": "voice",
    "Voice transcription": "voice",
    "Bios": "profile",
    "Birthday": "profile",
    "Music Playlist": "profile",
    "Whisper": "anonymous",
    "Confessions": "anonymous",
    "Anon features": "anonymous",
    "Pen Pals": "penpals",
    "Wellness": "wellness",
    "Take a Break": "wellness",
    "Mod actions": "moderation",
    "Mod safety": "moderation",
    "Mod watch": "moderation",
    "Mod/audit misc": "moderation",
    "Policy Tickets": "moderation",
    "Role Grants": "roles",
    "Role menus": "roles",
    "DM perms": "roles",
    # The "Replies to Your Threads" opt-in is a direct-message preference.
    "Needle (Auto-Thread)": "roles",
    # A DM waiting ten minutes to go out with others like it; same heading as
    # the reply-DM preference it first served.
    "Batched DMs": "roles",
    "Intake": "joining",
    "Greeting watch": "joining",
    "Inactivity": "joining",
    "Inactivity sweep": "joining",
    "Promotion": "joining",
    # A queued departure anonymisation is the last event in a membership: the
    # row holds the real member id only until the worker runs, and is deleted
    # in the same transaction as the anonymisation. It reports under joining
    # for the same reason an inactivity sweep does — it is the lifecycle of
    # this member's time in the guild, not an administrative act of theirs.
    "Departure auto-anonymise": "joining",
    "Telemetry": "usage",
    "Admin-authored configuration": "admin",
    "QA tracker": "admin",
    "Todo / Mod Chores": "admin",
    # The todos table holds both lists. A filed bug is a report about the bot
    # rather than about the filer, and it sits alongside the chore rows it
    # shares a table with, so it reports under the same heading; the register
    # row is where the bug text's own retention decision is written down.
    "Todo / Mod Chores / Bug list": "admin",
    "Small per-member stores": "admin",
    "LegitLibs": "admin",
    "Orphaned": "admin",
    # departed_guilds names no member, so it can only ever contribute zero rows
    # to a member's report; the mapping exists so the register gate stays honest.
    "Guild removal erasure": "admin",
}

# Values the member supplied or chose themselves, safe to print back to them.
# (table, column, label). Deliberately narrow: nothing here can name a second
# member, so no Art 15(4) judgement is ever needed before sending the report.
SELF_AUTHORED_VALUES: tuple[tuple[str, str, str], ...] = (
    ("econ_wallets", "balance", "Wallet balance"),
    ("member_xp", "total_xp", "Total XP"),
    ("member_xp", "level", "Level"),
    ("econ_streaks", "current_streak", "Current login streak"),
    ("econ_streaks", "longest_streak", "Longest login streak"),
    ("bio_answers", "answer", "Bio answer"),
    ("bio_field_values", "value", "Bio field"),
    ("mahjong_prefs", "mode", "Mahjong play mode"),
    ("wellness_users", "timezone", "Wellness: your timezone"),
    ("pen_pals_pool", "joined_at", "Pen Pals: joined the pool"),
    ("voice_master_profiles", "saved_name", "Your saved voice room name"),
    ("member_birthdays", "preference", "Birthday: how you asked to be greeted"),
)

# A few values are spread across columns and need composing before they read as
# anything. ``bios`` is absent from this module entirely on purpose: the bio
# text lives in a Discord message, not in the table, so there is nothing here
# to print back.
_MONTHS = (
    "January", "February", "March", "April", "May", "June", "July",
    "August", "September", "October", "November", "December",
)


def composed_values(table: str, row: dict) -> list[tuple[str, str]]:
    """Values that only make sense assembled from more than one column."""
    if table == "member_birthdays":
        month, day = row.get("birth_month"), row.get("birth_day")
        if month and day and 1 <= int(month) <= 12:
            return [("Birthday you set", f"{int(day)} {_MONTHS[int(month) - 1]}")]
    return []
