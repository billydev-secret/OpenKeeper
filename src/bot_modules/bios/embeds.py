"""Embed builders for the bios cog.

Pure: takes a fully-snapshotted `BioRenderPayload` and returns a
`discord.Embed`. No template lookups, no Discord state — what the user
entered at submit time is exactly what renders, even if the template
or question pool changed afterward.

The wizard's *prompt* embeds live here for the same reason: they are pure
functions of (what we're asking, what's already saved), which keeps the
overwrite warning — the thing members actually get bitten by — assertable
without standing up a wizard session.
"""

from __future__ import annotations

from datetime import datetime, timezone

import discord

from bot_modules.bios.logic import (
    BioField,
    BioQuestion,
    BioRenderPayload,
    FieldSnapshot,
    QuestionSnapshot,
    cap_field_values_for_embed,
    cap_question_answers_for_embed,
    question_field_name,
    shrink_to_embed_total,
)
from bot_modules.core.branding import (
    EMBED_TITLE_LIMIT,
    FIELD_VALUE_LIMIT,
    apply_section_spacing,
    escape_to_fit,
)

#: `bio_fields.hint` is admin-authored; keep the example from crowding the ask.
_HINT_LIMIT = 256


def _parse_timestamp(iso: str) -> datetime | None:
    if not iso:
        return None
    raw = iso.rstrip("Z")
    for parser in (
        lambda s: datetime.fromisoformat(s),
        # Legacy fallback format carries no offset; it was always UTC.
        lambda s: datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc),
    ):
        try:
            dt = parser(raw)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except (TypeError, ValueError):
            continue
    return None


def build_bio_embed(payload: BioRenderPayload) -> discord.Embed:
    """Build the styled member-bio embed (spec §6).

    Layout:
        author = display name + avatar icon
        title  = headline value
        thumbnail = avatar
        color  = guild's configured embed color (same across all bios)
        fields = profile fields in order (short/choice inline, paragraph
                 full-width, skipped omitted) then icebreaker answers
                 (full-width, name = ``› {question}``)
        footer = timestamp only
    """
    # Escape *before* the caps, not after. Member-typed text is escaped so a
    # bio can't reformat the card, but a backslash is a character like any
    # other: escaping a value that has already been trimmed to 1024 can push it
    # past Discord's field cap (and the whole-embed 6000 budget with it), which
    # 400s the entire card. Escaping first lets both safety nets measure what
    # actually gets sent.
    fields = cap_field_values_for_embed(
        [
            FieldSnapshot(
                label=s.label,
                value=discord.utils.escape_markdown(s.value),
                field_type=s.field_type,
                skipped=s.skipped,
            )
            for s in payload.fields
        ]
    )
    questions = cap_question_answers_for_embed(
        [
            QuestionSnapshot(
                question_text=s.question_text,
                answer=discord.utils.escape_markdown(s.answer),
                skipped=s.skipped,
            )
            for s in payload.questions
        ]
    )
    fields, questions = shrink_to_embed_total(fields, questions)

    embed = discord.Embed(
        # Escape the chosen value, not before choosing: the em-dash fallback
        # carries no markdown, and escaping first would hand a null straight
        # to escape_markdown instead of letting `or` catch it.
        #
        # Unlike every other value in this function, headline_value never
        # passes through cap_field_values_for_embed or any other truncation —
        # it's whichever field is flagged is_headline, and that field's raw
        # max_len is admin-configurable up to 4096 (default 1024). An
        # ordinary headline answer that's comfortably inside that 1024
        # default already doubles under escape_markdown and clears the
        # title's 256-char cap by roughly 8x once it has any markdown chars
        # in it — this isn't an edge case, it's the common case. A title
        # over the cap 400s the whole embed, so escape_to_fit escapes first
        # and then trims to the length Discord actually counts.
        title=escape_to_fit(payload.headline_value or "—", EMBED_TITLE_LIMIT),
        color=payload.embed_color,
    )
    embed.set_author(name=payload.display_name, icon_url=payload.avatar_url or None)
    if payload.avatar_url:
        embed.set_thumbnail(url=payload.avatar_url)

    for snap in fields:
        if snap.skipped or not snap.value:
            continue
        inline = snap.field_type in ("short", "choice")
        embed.add_field(name=snap.label, value=snap.value, inline=inline)

    for snap in questions:
        if snap.skipped or not snap.answer:
            continue
        embed.add_field(
            name=question_field_name(snap.question_text),
            value=snap.answer,
            inline=False,
        )

    ts = _parse_timestamp(payload.created_at_iso)
    if ts is not None:
        embed.timestamp = ts
    apply_section_spacing(embed)
    return embed


def build_field_prompt_embed(
    field: BioField,
    *,
    prior: str,
    editing: bool,
    step_index: int,
    total_steps: int,
    embed_color: int,
) -> discord.Embed:
    """The wizard's ask for one profile field.

    When the member is editing and the field already has a value, the prompt
    leads with the overwrite warning: the wizard captures a plain channel
    message, so there is no modal to pre-fill and nothing else on screen says
    that typing *replaces* rather than appends. **Keep** is the non-destructive
    exit, so the warning names it.
    """
    replacing = editing and bool(prior)
    e = discord.Embed(title=field.label, color=embed_color)
    if replacing:
        # Escape before the cap, same reasoning as build_bio_embed — but a
        # bare `escape_markdown(prior)[:FIELD_VALUE_LIMIT]` slice only does
        # the first half of that. escape_markdown can double `prior`, and a
        # plain slice on the escaped result can land inside an escape pair,
        # dropping the character the cut backslash was protecting and
        # leaving a lone trailing backslash to escape whatever renders next.
        # escape_to_fit measures the same string it escapes, and trims on a
        # whole-pair boundary.
        e.add_field(
            name="Current Answer",
            value=escape_to_fit(prior, FIELD_VALUE_LIMIT),
            inline=False,
        )
    # Warm, type-aware guidance instead of a bare "reply with..." line.
    if field.field_type == "paragraph":
        guidance = "Take a few sentences — no need to be polished, just be you."
    elif field.field_type == "choice":
        guidance = "Pick whichever fits best below — you can change it later."
    else:
        guidance = "A word or a short phrase is perfect."
    if replacing:
        verb = "Picking a new option" if field.field_type == "choice" else "Whatever you send next"
        e.description = (
            f"✏️ **{verb} replaces the answer above** — it isn't added to it. "
            "Tap **Keep** to leave it exactly as it is.\n\n" + guidance
        )
    else:
        e.description = guidance
    # Admin-authored example, if one is set — the biggest unblocker for
    # members who aren't sure what to write.
    if field.hint:
        e.add_field(name="💡 For Example", value=field.hint[:_HINT_LIMIT], inline=False)
    if not field.required:
        e.add_field(
            name="​",
            value="*Totally optional — tap **Skip** if you'd rather not.*",
            inline=False,
        )
    e.set_footer(text=f"Step {step_index + 1} / {total_steps}")
    apply_section_spacing(e)
    return e


def build_question_prompt_embed(
    question: BioQuestion,
    *,
    existing: str,
    answered_count: int,
    target_questions: int,
    embed_color: int,
) -> discord.Embed:
    """The wizard's ask for one icebreaker question.

    Re-picking an answered question (the ✏️ rows in the browse list) lands
    here, and this step has no **Keep** button — **Back** is what drops the
    pending question without saving. So when there's an existing answer, show
    it and say plainly that a reply overwrites it.
    """
    e = discord.Embed(title=question_field_name(question.prompt), color=embed_color)
    if existing:
        # Escape before the cap, same fix as build_field_prompt_embed's
        # "Current Answer" field: escape_to_fit escapes `existing` and
        # trims the escaped result without splitting an escape pair, which
        # a bare `escape_markdown(...)[:FIELD_VALUE_LIMIT]` slice could do.
        e.add_field(
            name="Current Answer",
            value=escape_to_fit(existing, FIELD_VALUE_LIMIT),
            inline=False,
        )
        e.description = (
            "✏️ **Whatever you send next replaces the answer above** — it isn't "
            "added to it. Tap **Back** to leave it unchanged and pick a "
            "different question."
        )
    else:
        e.description = (
            "Reply with your answer, or use **Back** to pick a different question."
        )
    e.set_footer(text=f"Icebreaker {answered_count + 1} of up to {target_questions}")
    return e


__all__ = [
    "build_bio_embed",
    "build_field_prompt_embed",
    "build_question_prompt_embed",
    "FieldSnapshot",
    "QuestionSnapshot",
]
