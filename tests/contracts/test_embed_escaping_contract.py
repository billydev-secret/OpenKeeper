"""Contract: an embed builder never reads a raw member name.

CLAUDE.md § Conventions routes every member named inside an embed through
``services/name_resolver.build_name_fn``. That rule is usually explained by
what it fixes — a ``<@id>`` renders as a bare number to anyone whose client
has not cached that user — but it does a second job that nothing stated:
``resolve_name_from`` escapes what it returns, so every name arriving through
a ``name_fn`` is already safe to drop into markdown.

A builder that reaches past the resolver for ``member.display_name`` loses
that silently. Discord renders markdown in an embed's title, description and
field values, so a member calling themselves ``**Moderator**`` renders bold in
every card that names them, ``||x||`` hides the surrounding text behind a
spoiler, and an unclosed ``` reflows the rest of the field. Nothing warns: the
card is well-formed, it just says something its author did not write.

**Born green.** 31 ``embeds.py`` files, 174 builders, and the five raw reads
below are each excused for a reason recorded here — no builder escapes by
hand, because none needs to. That is the state this locks in, so the cost of
the rule is only ever paid by the next builder that breaks it.

The cog layer is a separate surface with separate sites: a cog composes
``content=`` strings and ephemeral replies that this sweep does not see.
``discord.utils.escape_markdown`` is the tool there, and the builders are
what this file keeps clean.
"""

from __future__ import annotations

import ast
from pathlib import Path
from tests.paths import REPO_ROOT

SRC = REPO_ROOT / "src"

#: Attributes that hold a member-chosen name. ``.name`` is deliberately absent:
#: channels, roles, emoji, guilds and files all carry one, so guarding it would
#: flag a dozen reads that no member can influence and teach the next person to
#: silence the check rather than read it.
NAME_ATTRS = frozenset({"display_name", "nick", "global_name"})

#: ``(module path under src/, the expression as written)`` → why it is safe.
#: A row is a claim about that expression, so moving the line does not stale
#: it, and a second read of the same expression in the same file is covered by
#: the same reasoning. Anything not listed is a finding.
ALLOWED: dict[tuple[str, str], str] = {
    (
        "bot_modules/bios/embeds.py",
        "payload.display_name",
    ): (
        "embed.set_author(name=...) — Discord renders no markdown in an "
        "author line, so escaping it would change what is displayed without "
        "removing any risk"
    ),
    (
        "bot_modules/starboard/embeds.py",
        "message.author.display_name",
    ): (
        "embed.set_author(name=...) — as above. The channel name beside it is "
        "staff-controlled"
    ),
    (
        "bot_modules/games/mahjong/layouts.py",
        "card.display_name",
    ): (
        "a Meadow Card, not a member: card_logic.load_card_file reads it from "
        "a card file in the repo, so its name is authored here and cannot "
        "carry member markdown"
    ),
}


def _embeds_modules() -> list[Path]:
    # layouts.py is the Components V2 successor of embeds.py, and each one the
    # migration writes deletes an embeds.py; a Text Display renders markdown
    # just as a field value does, so the rule follows the builders across.
    return sorted([*SRC.rglob("embeds.py"), *SRC.rglob("layouts.py")])


def test_sweep_sees_the_embed_modules() -> None:
    """A moved tree would make the check below pass vacuously. Embeds and V2
    layouts are counted together because the migration trades one for the
    other: 22 + 18 on 2026-09-27."""
    assert len(_embeds_modules()) >= 25


def test_every_allowed_row_still_exists() -> None:
    """An excuse outlives the code it excused, and then it is just a hole.

    When a builder stops reading a raw name — because it moved to a
    ``name_fn``, or the builder went away — its row here has to go too, or the
    next raw read of the same expression inherits an exemption nobody granted
    it.
    """
    present = {
        (path.relative_to(SRC).as_posix(), ast.unparse(node))
        for path in _embeds_modules()
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.Attribute) and node.attr in NAME_ATTRS
    }
    stale = sorted(f"{path} :: {expr}" for path, expr in ALLOWED if (path, expr) not in present)
    assert not stale, (
        "ALLOWED excuses a raw name read that is no longer there. Delete the "
        "row:\n  " + "\n  ".join(stale)
    )


def test_embed_builders_take_names_through_name_fn() -> None:
    offenders = []
    for path in _embeds_modules():
        rel = path.relative_to(SRC).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Attribute) or node.attr not in NAME_ATTRS:
                continue
            expr = ast.unparse(node)
            if (rel, expr) in ALLOWED:
                continue
            offenders.append(f"src/{rel}:{node.lineno} {expr}")

    assert not offenders, (
        "an embed builder is reading a member name directly instead of taking "
        "it through a name_fn:\n  "
        + "\n  ".join(offenders)
        + "\n\nresolve_name_from escapes what it returns, so a name_fn name is "
        "safe in a title, description or field value; a raw display_name is "
        "not — '**Moderator**' renders bold and '||x||' spoilers whatever "
        "follows it. Take a `name_fn: NameFn` parameter and resolve in the "
        "cog or service. If the value genuinely cannot carry member markdown "
        "(it is staff-authored, or it lands somewhere markdown does not "
        "render, such as an author line), add a row to ALLOWED saying which."
    )
