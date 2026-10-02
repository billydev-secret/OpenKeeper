"""Deletion scope modes — every mode clears only Discord messages and keeps
all server-side data (XP/activity/profile and the local message archive)."""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from bot_modules.privacy import cog as privacy_cog
from bot_modules.privacy import deletion_service
from bot_modules.privacy.logic import (
    EVERYTHING_CONFIRM_LABEL,
    EVERYTHING_IN_PROGRESS,
    render_everything_confirm,
    render_everything_summary,
    FINAL_CONFIRM_LABEL,
    MEMBER_ERASABLE_STORES,
    MODE_ALL,
    MODE_MEDIA,
    MODE_TEXT,
    confirm_button_label,
    message_has_media,
    message_matches_mode,
    render_confirm_prompt,
    render_deletion_summary,
    render_empty_summary,
    render_final_confirm,
    render_panel,
)


def _msg(*, attachments=(), embeds=(), stickers=()) -> SimpleNamespace:
    return SimpleNamespace(
        attachments=list(attachments),
        embeds=[SimpleNamespace(type=t) for t in embeds],
        stickers=list(stickers),
    )


# ── classifier ──────────────────────────────────────────────────────────


def test_plain_text_is_not_media() -> None:
    assert not message_has_media(_msg())


def test_attachment_is_media() -> None:
    assert message_has_media(_msg(attachments=["photo.png"]))


def test_sticker_is_media() -> None:
    assert message_has_media(_msg(stickers=["blob"]))


def test_image_and_video_embeds_are_media() -> None:
    for kind in ("image", "video", "gifv"):
        assert message_has_media(_msg(embeds=[kind])), kind


def test_link_preview_is_not_media() -> None:
    """Discord auto-embeds any link; a article/rich preview is still text.

    This is the case that would otherwise sweep ordinary chatter into a
    "clear my images" run.
    """
    for kind in ("link", "article", "rich"):
        assert not message_has_media(_msg(embeds=[kind])), kind


def test_text_message_with_a_link_survives_a_media_scrub() -> None:
    linky = _msg(embeds=["article"])
    assert not message_matches_mode(linky, MODE_MEDIA)
    assert message_matches_mode(linky, MODE_TEXT)


# ── the discord.py seam ─────────────────────────────────────────────────
#
# The tests above use hand-made fakes, which prove the logic but not that
# discord.py actually emits these strings. If the real embed types ever drift,
# the classifier silently stops seeing media — and in `text` mode that means
# deleting the very images the member asked to keep. So pin against the real
# library rather than our own fakes.


def test_media_embed_types_exist_in_discord_py() -> None:
    import typing

    from discord.types.embed import EmbedType

    from bot_modules.privacy.logic import _MEDIA_EMBED_TYPES

    known = set(typing.get_args(EmbedType))
    assert _MEDIA_EMBED_TYPES <= known, (
        f"embed types drifted; discord.py knows {sorted(known)}"
    )


def test_classifier_against_real_discord_embeds() -> None:
    import discord

    for kind in ("image", "video", "gifv"):
        embed = discord.Embed.from_dict({"type": kind})
        assert message_has_media(_real_embed_msg(embed)), kind

    for kind in ("rich", "link", "article"):
        embed = discord.Embed.from_dict({"type": kind})
        assert not message_has_media(_real_embed_msg(embed)), kind


def _real_embed_msg(embed) -> SimpleNamespace:
    return SimpleNamespace(attachments=[], embeds=[embed], stickers=[])


def test_a_posted_image_url_is_media_not_text() -> None:
    """No attachment, but Discord embeds it as an image — the member posted a
    picture, so a text scrub must not take it."""
    import discord

    msg = _real_embed_msg(discord.Embed.from_dict({"type": "image"}))
    assert message_matches_mode(msg, MODE_MEDIA)
    assert not message_matches_mode(msg, MODE_TEXT)


# ── mode selection ──────────────────────────────────────────────────────


def test_modes_partition_messages() -> None:
    photo = _msg(attachments=["a.png"])
    chat = _msg()
    # Every message lands in exactly one of media/text, and both land in all.
    for m in (photo, chat):
        assert message_matches_mode(m, MODE_ALL)
        assert message_matches_mode(m, MODE_MEDIA) != message_matches_mode(m, MODE_TEXT)
    assert message_matches_mode(photo, MODE_MEDIA)
    assert message_matches_mode(chat, MODE_TEXT)


# ── copy ────────────────────────────────────────────────────────────────
#
# Two load-bearing properties of this copy:
#
# 1. It must never tell anyone their server-side data (XP/activity/profile)
#    was deleted, because it never is. Asserted positively below.
# 2. It must not claim the server keeps a copy of their *messages*. The bot
#    has held no message text since migration 228; all that survives is a row
#    saying a post happened, and every phrasing of that in a six-word aside
#    before an irreversible button read as content retention. Removed
#    2026-09-18 on the owner's call. Retention is disclosed properly in the
#    privacy notice instead. Asserted negatively below, because a stranger
#    reading this flow will see a missing disclosure and want to put it back.


def test_summary_states_data_is_retained_for_moderation() -> None:
    for mode in (MODE_ALL, MODE_MEDIA, MODE_TEXT):
        out = render_deletion_summary(deleted=3, failed=0, replaced=0, mode=mode)
        assert "kept for moderation" in out
        assert "cleared" not in out.lower()
        assert "erased" not in out.lower()


def test_empty_summary_states_data_is_untouched() -> None:
    for mode in (MODE_ALL, MODE_MEDIA, MODE_TEXT):
        out = render_empty_summary(mode=mode)
        assert "Nothing else was touched" in out
        assert "cleared" not in out.lower()


def test_full_mode_summary_exact_copy() -> None:
    out = render_deletion_summary(deleted=5, failed=1, replaced=2, mode=MODE_ALL)
    assert out == (
        "All done. Here's what was removed:\n"
        "Messages deleted from Discord: **5**\n"
        "XP, activity, and profile: **kept for moderation**.\n"
        "Forum posts replaced with tombstone: **2**\n"
        "Messages that couldn't be deleted (no access): **1**"
    )


def test_self_prompt_discloses_what_survives() -> None:
    """Say what is kept before the click, not after."""
    out = render_confirm_prompt(mode=MODE_ALL)
    assert "XP, activity, and profile stay exactly as they are" in out


def test_prompt_never_claims_the_data_is_erased() -> None:
    """Only Discord messages go; the server keeps XP/activity/profile. The
    prompt must not imply otherwise — this is the moment consent is given."""
    out = render_confirm_prompt(mode=MODE_ALL)
    assert "stay exactly as they are" in out
    assert "erased" not in out.lower()


def test_prompt_makes_no_claim_about_a_server_side_message_copy() -> None:
    """**Deliberate omission — do not "fix" this by restoring the sentence.**

    Until 2026-09-18 the prompt read "the server keeps its own copy of your
    messages for moderation — as metadata (who, where, when — not the text)".
    Every part of that was true: migration 228 erased the stored text on
    2026-09-14 and what remains in ``messages`` is a metadata row. It still
    misled, because a member reads "copy of your messages" and stops. The
    owner's call was to drop it from the consent flow outright rather than
    reword it a third time; retention is disclosed in the privacy notice
    (``manual.html`` §Your Data & Privacy), where there is room to say it
    properly — and will be in the ``privacy-tools`` doc too once its
    rewritten body is saved and re-posted. See ``docs/specs/privacy_spec.md``.

    This reads as a missing disclosure to anyone who meets the flow cold, so
    it is pinned rather than left to a comment.
    """
    for mode in (MODE_ALL, MODE_MEDIA, MODE_TEXT):
        out = render_confirm_prompt(mode=mode).lower()
        assert "own copy" not in out
        assert "own record" not in out
        assert "metadata" not in out
        assert "for moderation" not in out
        # The prompt speaks only of Discord, never of a second place.
        assert "those records" not in out


def test_panel_makes_no_claim_about_a_server_side_message_copy() -> None:
    """The ``/mydata`` panel body is the first thing a member reads, before
    they even pick a mode — the same "copy of your messages" phrasing that
    was pulled from the confirm prompt (see
    ``test_prompt_makes_no_claim_about_a_server_side_message_copy``) applies
    here too, and even more so."""
    out = render_panel().lower()
    assert "own copy" not in out
    assert "own record" not in out
    assert "metadata" not in out
    assert "for moderation" not in out


def test_partial_prompt_names_only_its_slice() -> None:
    out = render_confirm_prompt(mode=MODE_MEDIA)
    assert "images & files" in out
    assert "stay exactly as they are" in out
    # No mode describes itself as deleting everything.
    assert "everything" not in out.lower()


def test_button_label_names_the_real_scope() -> None:
    assert confirm_button_label(MODE_ALL) == "Yes, delete my messages"
    assert confirm_button_label(MODE_MEDIA) == "Yes, delete my images & files"
    assert confirm_button_label(MODE_TEXT) == "Yes, delete my text messages"
    # "everything" overstates the scope and must never be a label.
    for mode in (MODE_ALL, MODE_MEDIA, MODE_TEXT):
        assert "everything" not in confirm_button_label(mode).lower()


def test_button_labels_fit_discord_limit() -> None:
    for mode in (MODE_ALL, MODE_MEDIA, MODE_TEXT):
        assert len(confirm_button_label(mode)) <= 80


def test_confirm_view_actually_relabels_the_rendered_button() -> None:
    """The label is set on the decorated button after construction.

    Worth pinning: if that attribute stopped resolving to the bound Button the
    assignment would no-op silently and the button would keep its placeholder
    label instead of the real scope.
    """
    view = privacy_cog._ConfirmDeleteView(
        actor_id=1, confirm_label="Yes, delete my images & files"
    )
    labels = [getattr(c, "label", None) for c in view.children]
    assert "Yes, delete my images & files" in labels
    assert "Cancel" in labels
    assert "Yes, delete my messages" not in labels


def test_confirm_view_default_label_is_messages_scope() -> None:
    view = privacy_cog._ConfirmDeleteView(actor_id=1)
    labels = [getattr(c, "label", None) for c in view.children]
    assert "Yes, delete my messages" in labels
    assert not any("everything" in (label or "").lower() for label in labels)


def test_final_confirm_label_differs_from_every_first_step_label() -> None:
    """A double-click must not land on an identical button one step later."""
    first_steps = {confirm_button_label(m) for m in (MODE_ALL, MODE_MEDIA, MODE_TEXT)}
    first_steps |= {store.confirm_label for store in MEMBER_ERASABLE_STORES}
    first_steps.add(EVERYTHING_CONFIRM_LABEL)
    assert FINAL_CONFIRM_LABEL not in first_steps
    assert len(FINAL_CONFIRM_LABEL) <= 80


def test_final_confirm_names_the_subject_and_its_button() -> None:
    out = render_final_confirm("Your whisper history")
    assert "Your whisper history will be gone for good" in out
    assert FINAL_CONFIRM_LABEL in out
    assert "no undo" in out


def _press() -> MagicMock:
    interaction = MagicMock()
    interaction.response.edit_message = AsyncMock()
    return interaction


@pytest.mark.parametrize(
    ("presses", "expected", "steps"),
    [
        pytest.param([True, True], True, 2, id="both-confirmed"),
        pytest.param([True, False], False, 2, id="cancelled-at-the-last-check"),
        pytest.param([False], False, 1, id="cancelled-at-the-first-step"),
    ],
)
async def test_every_panel_deletion_takes_two_confirms(
    monkeypatch, presses, expected, steps
) -> None:
    """Billy, 2026-09-27: every My Data deletion button confirms twice.

    ``_confirm`` is the one path both halves of the panel take, so pinning it
    covers the message modes and every store button at once.
    """
    shown: list = []

    class _Step:
        def __init__(self, actor_id, *, confirm_label, final=True):
            self.label, self.final = confirm_label, final
            self.confirmed: bool | None = None
            self.interaction = None
            shown.append(self)

        async def wait(self):
            self.confirmed = presses[len(shown) - 1]
            if self.confirmed:
                self.interaction = _press()

    monkeypatch.setattr(privacy_cog, "_ConfirmDeleteView", _Step)
    panel = privacy_cog._MyDataPanelView(
        MagicMock(), actor_id=7, guild=MagicMock(), accent=None
    )
    opener = _press()
    ok = await panel._confirm(
        opener, prompt="first", confirm_label="Yes, delete my whispers",
        subject="Your whisper history",
    )
    assert ok is expected
    assert len(shown) == steps
    assert shown[0].final is False, "the first step must leave the press for step two"
    assert shown[0].label == "Yes, delete my whispers"
    if steps == 2:
        assert shown[1].final is True
        assert shown[1].label == FINAL_CONFIRM_LABEL
        content = shown[0].interaction.response.edit_message.await_args.kwargs["content"]
        assert content == render_final_confirm("Your whisper history")


# ── the safety property, end to end ─────────────────────────────────────


@pytest.fixture
def _wired(monkeypatch):
    """Drive _run_deletion with the network and DB stubbed out.

    The safety property worth guarding: no mode may ever call the DB-purge path.
    ``purge_user_data`` is no longer even imported by the cog, so we assert the
    open_db seam is never entered — if _run_deletion regrew a purge it would have
    to open the DB, and this fixture records every open.

    The scan and delete seams live in ``privacy.deletion_service`` since the
    engine was made headless for the dashboard, so they are patched there; the
    cog keeps only the card.
    """
    calls: dict[str, list] = {"db_opened": [], "scanned_with": []}

    async def _fake_find(guild, user_id, *, on_progress=None, predicate=None):
        calls["scanned_with"].append(predicate)
        return [(11, 22)]

    async def _fake_delete(guild, user_id, msg_rows, on_progress=None):
        return (len(msg_rows), 0, 0)

    class _FakeReporter:
        def __init__(self, interaction, db_path=None, guild=None):
            pass

        async def update(self, embed):
            return None

    monkeypatch.setattr(deletion_service, "find_user_messages", _fake_find)
    monkeypatch.setattr(deletion_service, "delete_discord_messages", _fake_delete)
    monkeypatch.setattr(privacy_cog, "_ProgressReporter", _FakeReporter)
    assert not hasattr(privacy_cog, "purge_user_data"), (
        "the cog must not import the hard-erasure purge at all"
    )

    @contextmanager
    def _open_db():
        calls["db_opened"].append(True)
        yield MagicMock()

    ctx = MagicMock()
    ctx.open_db = _open_db
    return calls, ctx


async def _run(ctx, mode: str) -> None:
    guild = MagicMock()
    guild.id = 1
    await privacy_cog._run_deletion(ctx, guild, 42, MagicMock(), mode=mode)


async def test_no_mode_touches_the_database(_wired) -> None:
    calls, ctx = _wired
    for mode in (MODE_ALL, MODE_MEDIA, MODE_TEXT):
        await _run(ctx, mode)
    assert calls["db_opened"] == [], "deletion must not touch server-side data in any mode"


def test_progress_cards_take_an_accent_and_fall_back_without_one() -> None:
    """The scan/delete cards were a module-level blurple constant with no accent
    path at all — the one privacy surface ignoring the guild's branding (#76).
    The final summary stays green: there the color is semantic ("complete").
    """
    import discord

    accent = discord.Color(0x5A32A8)
    assert privacy_cog._scan_embed(1, 4, 2, accent).color == accent
    assert privacy_cog._delete_embed(1, 4, accent).color == accent
    assert privacy_cog._scan_embed(1, 4, 2).color == privacy_cog._PROGRESS_FALLBACK
    assert privacy_cog._delete_embed(1, 4).color == privacy_cog._PROGRESS_FALLBACK
    assert privacy_cog._summary_embed("done").color == privacy_cog._DONE_COLOR


async def test_the_accent_reaches_the_rendered_progress_card(monkeypatch, _wired) -> None:
    """Wiring: the accent handed to _run_deletion lands on the card the member
    actually sees. It is passed in rather than resolved here because reading the
    branding config would open the DB, which test_no_mode_touches_the_database
    forbids for a deletion run.
    """
    import discord

    calls, ctx = _wired
    accent = discord.Color(0x5A32A8)
    seen: list[discord.Embed] = []

    async def _find_with_progress(guild, user_id, *, on_progress=None, predicate=None):
        calls["scanned_with"].append(predicate)
        if on_progress:
            await on_progress(1, 1, 1)  # done == total, so the throttle lets it through
        return [(11, 22)]

    class _CapturingReporter:
        def __init__(self, interaction, db_path=None, guild=None):
            pass

        async def update(self, embed):
            seen.append(embed)

    monkeypatch.setattr(deletion_service, "find_user_messages", _find_with_progress)
    monkeypatch.setattr(privacy_cog, "_ProgressReporter", _CapturingReporter)

    guild = MagicMock()
    guild.id = 1
    await privacy_cog._run_deletion(
        ctx, guild, 42, MagicMock(), mode=MODE_ALL, accent=accent
    )

    progress = [e for e in seen if e.title == privacy_cog._PROGRESS_TITLE]
    assert progress, "no progress card was rendered"
    assert all(c.color == accent for c in progress)
    assert calls["db_opened"] == [], "rendering the accent must not open the DB"


def _dm_reporter(tmp_path, dm_message):
    """A reporter already past its interaction token, holding ``dm_message``."""
    guild = MagicMock(spec=discord.Guild)
    guild.name = "Test Guild"
    guild.icon = None
    reporter = privacy_cog._ProgressReporter(
        MagicMock(), tmp_path / "progress.db", guild
    )
    reporter._fell_back = True
    reporter._dm_message = dm_message
    return reporter


@pytest.mark.parametrize(
    "card,color",
    [
        pytest.param(
            privacy_cog._scan_embed(1, 4, 2, discord.Color(0x123456)),
            discord.Color(0x123456),
            id="scan-keeps-the-run-accent",
        ),
        pytest.param(
            privacy_cog._delete_embed(2, 4, discord.Color(0x123456)),
            discord.Color(0x123456),
            id="delete-keeps-the-run-accent",
        ),
        pytest.param(
            privacy_cog._summary_embed("done"),
            privacy_cog._DONE_COLOR,
            id="summary-keeps-its-semantic-green",
        ),
    ],
)
async def test_every_progress_edit_carries_the_server_attribution(
    tmp_path, card, color
) -> None:
    """The card keeps saying which server after the first tick replaces it.

    ``send_branded_dm`` stamps one send. Every update after it is an *edit*
    carrying a fresh embed, so an unbranded edit erased the attribution about
    as fast as it appeared — and left it gone for the rest of the run,
    including the final summary. On the erasure surface, "which server is
    deleting my data" is the whole question.

    The color assertion is the other half, and the summary row is why it is
    here: attribution is all this path wants, so it brands with
    ``keep_color=True``. Drop that argument and the brander falls back to
    ``DM_PRIMARY``, repainting the run's accent on every tick and — on the
    one card a member actually waits for — the semantic green of "Deletion
    Complete". Nothing else in the suite would notice.
    """
    dm_message = MagicMock(spec=discord.Message)
    dm_message.edit = AsyncMock()
    reporter = _dm_reporter(tmp_path, dm_message)

    await reporter.update(card)

    edited = dm_message.edit.await_args.kwargs["embed"]
    assert edited.footer.text == "Test Guild"
    assert edited.color == color


async def test_a_failed_progress_edit_does_not_double_the_attribution(
    tmp_path,
) -> None:
    """The branded copy must not escape into the send that replaces it.

    ``brand_dm_embed`` mutates what it is given and appends to any footer
    already there, so branding the caller's embed in place would send
    "Test Guild • Test Guild" the moment an edit fails and the fresh send
    re-brands the same object. The copy is what prevents it, and this is the
    test that notices if someone removes it.
    """
    dm_message = MagicMock(spec=discord.Message)
    dm_message.edit = AsyncMock(
        side_effect=discord.NotFound(MagicMock(status=404), {"code": 10008})
    )
    reporter = _dm_reporter(tmp_path, dm_message)
    sent = MagicMock(spec=discord.Message)
    user = MagicMock()
    user.send = AsyncMock(return_value=sent)
    reporter._interaction.user = user

    await reporter.update(privacy_cog._scan_embed(1, 4, 2))

    resent = user.send.await_args.kwargs["embed"]
    assert resent.footer.text == "Test Guild", resent.footer.text
    assert reporter._dm_message is sent


async def test_scrub_passes_a_predicate_and_full_mode_does_not(_wired) -> None:
    """The filter has to reach the scan: without it every message is collected."""
    calls, ctx = _wired
    await _run(ctx, MODE_MEDIA)
    predicate = calls["scanned_with"][0]
    assert predicate is not None
    assert predicate(_msg(attachments=["a.png"]))
    assert not predicate(_msg())

    calls["scanned_with"].clear()
    await _run(ctx, MODE_ALL)
    assert calls["scanned_with"] == [None]


# ── Delete everything ───────────────────────────────────────────────────────


def test_everything_confirm_says_anything_in_progress_is_lost() -> None:
    """Billy, 2026-09-27: "just erase" — no live-state check runs, so this
    sentence is the member's only warning that a bet, a game seat, a voice
    room or a Pen Pals pairing goes with everything else. Removing it makes
    the button silently destroy things mid-flight."""
    out = render_everything_confirm()
    assert EVERYTHING_IN_PROGRESS in out
    for thing in ("bet", "game", "voice room", "Pen Pals"):
        assert thing in EVERYTHING_IN_PROGRESS
    assert "lost" in EVERYTHING_IN_PROGRESS


def test_everything_confirm_is_honest_about_what_stays() -> None:
    out = render_everything_confirm()
    # Anonymised, not deleted: the confirm must not promise deletion of XP.
    assert "cut loose from your account" in out
    assert "moderation record" in out and "no-contact" in out
    assert "nothing else is touched" in out


def test_panel_names_the_everything_button_and_what_always_stays() -> None:
    out = render_panel()
    assert "**Delete everything**" in out
    assert "moderation record" in out


@pytest.mark.parametrize(
    ("status", "kwargs", "expected"),
    [
        pytest.param("done", {"deleted": 4, "all_guilds": True}, "everywhere this bot knows you", id="done-all"),
        pytest.param("done", {"deleted": 4}, "any other server you share", id="done-guild"),
        pytest.param("held", {"detail": "jailed"}, "you're in jail", id="held-jail"),
        pytest.param("held", {"detail": "ticket"}, "open ticket", id="held-ticket"),
        pytest.param(
            "held",
            {"detail": "jailed", "deleted": 5},
            "5 message(s) were already deleted",
            id="held-after-messages-gone",
        ),
        pytest.param("messages_left", {"deleted": 3, "failed": 1}, "1 wouldn't go", id="messages-left"),
        pytest.param("break_stuck", {}, "break couldn't be ended", id="break"),
        pytest.param("error", {}, "nothing else was changed", id="error"),
    ],
)
def test_everything_summary_per_outcome(status, kwargs, expected) -> None:
    out = render_everything_summary(status, **kwargs)
    assert expected in out
    # Only a finished run may read as one.
    assert out.startswith("✅") is (status == "done")
