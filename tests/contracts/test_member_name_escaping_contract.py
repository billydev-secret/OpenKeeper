"""Contract: outside an embed builder, a member name in an f-string is escaped.

``tests/contracts/test_embed_escaping_contract.py`` keeps the *builder* layer clean by
routing every name through a ``name_fn`` — and says in its own docstring that
the cog layer is a separate surface it cannot see: a cog composes ``content=``
strings, ephemeral replies, DM bodies and audit lines that no builder sweep
reaches. ``discord.utils.escape_markdown`` is the tool there. This file is what
keeps *that* half clean.

The defect is display integrity, not code execution. Discord renders markdown
in a message's content, in an embed's title, description and field values, and
in a Components V2 Text Display. A member named ``**Moderator**`` renders bold
wherever the bot names them; ``||x||`` hides the rest of the line behind a
spoiler; an unclosed ``` reflows everything after it. Nothing warns — the
message is well-formed, it just says something the bot did not write.

**Born green.** The 2026-09-20 sweep (``docs/plans/markdown-escaping-sweep.md``)
took this measure from 39 unguarded reads across 20 files to the eight below,
every one of which is excused here for a reason. That is the state this locks
in, so the cost of the rule is only ever paid by the next site that breaks it.

What this can and cannot see
----------------------------
It sees a **member-name attribute read inside an f-string slot** that no
``escape_markdown``/``escape_mentions`` call guards. That is the half of the
problem with a reliable AST signature.

It is blind to the other half, and saying so is the point — an enforcement
test that overstates its reach is worse than one that doesn't exist:

* **Member free text.** ``description=blurb`` where ``blurb`` is a bio answer,
  a whisper body or a ticket description is exactly as dangerous and has no
  distinguishing shape. The sweep fixed 68 such sites by reading; nothing here
  stops the 69th.
* **Non-f-string slots.** ``embed.add_field(name=p["title"])`` — a live bug
  this sweep fixed in ``moderation/jail/embeds.py`` — is neither an f-string nor a name
  attribute, so neither this file nor the builder contract would have caught
  it.
* **``embeds.py`` modules**, which the builder contract owns outright. They are
  skipped here so one site never answers to two files with two allowlists.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from tests.paths import REPO_ROOT

SRC = REPO_ROOT / "src"

#: Attributes that hold a member-chosen name. ``.name`` is deliberately absent
#: for the same reason the builder contract omits it: channels, roles, emoji,
#: guilds and files all carry one, so guarding it would flag a dozen reads no
#: member can influence and teach the next person to silence the check.
NAME_ATTRS = frozenset({"display_name", "nick", "global_name"})

#: Calls that make a name safe to interpolate. ``core.branding.escape_to_fit``
#: belongs here because it escapes *through* ``escape_markdown`` and only then
#: fits a cap: a slot that goes through it is guarded exactly as one that calls
#: ``escape_markdown`` directly. Leaving it out forced a caller to choose
#: between escaping twice and claiming an ALLOWED row it did not deserve.
SAFE_CALLS = frozenset({"escape_markdown", "escape_mentions", "escape_to_fit"})

#: ``(module path under src/, the expression as written)`` → why it is safe.
#: Keyed on the expression rather than the line so moving code does not stale a
#: row; as in the builder contract, that means a second read of the *same*
#: expression in the same file inherits the same excuse. Anything not listed is
#: a finding.
ALLOWED: dict[tuple[str, str], str] = {
    (
        "bot_modules/advisor/cog.py",
        "asker.display_name",
    ): (
        "embed.set_footer(text=...) — Discord renders no markdown in a footer, "
        "so escaping would show the asker a backslash and remove no risk"
    ),
    (
        "bot_modules/moderation/jail/cog.py",
        "user.display_name",
    ): (
        "a matplotlib chart title passed to render_activity_chart — the name "
        "is drawn into a PNG, where markdown is not a thing; escaping would "
        "paint a literal backslash into the image"
    ),
    (
        "bot_modules/duels/base_game.py",
        "interaction.user.display_name",
    ): (
        "the reason= on loser.edit() — a Discord audit-log entry, not a "
        "rendered surface, and a value written back to Discord besides"
    ),
    (
        "bot_modules/moderation/ai_service.py",
        "message.author.display_name",
    ): (
        "built into the user_content of an ollama_client.chat prompt, not a "
        "Discord surface. Prompt injection is a real concern on this line and "
        "a different one — escaping markdown would not address it"
    ),
    (
        "bot_modules/onboarding/welcome/service.py",
        "member.display_name",
    ): (
        "embed.set_author(name=...) in both the welcome and the leave card — "
        "an author line renders no markdown"
    ),
    (
        "web_server/routes/games_external.py",
        "member.display_name",
    ): (
        "a JSON field in a dashboard API response, not a Discord message. The "
        "browser layer escapes at render — table.js escapes every cell — so "
        "escaping here would double-encode into the page"
    ),
}


def _modules() -> list[Path]:
    """Every module this contract owns: all of ``src/`` bar the builders."""
    return sorted(p for p in SRC.rglob("*.py") if p.name != "embeds.py")


def _unguarded(path: Path) -> list[tuple[int, str]]:
    try:
        return _unguarded_source(path.read_text(encoding="utf-8"))
    except SyntaxError:  # pragma: no cover - a syntax error fails its own tests
        return []


def _unguarded_source(source: str) -> list[tuple[int, str]]:
    """Member-name reads in an f-string slot that no SAFE_CALLS call guards.

    The guard is looked for within the slot, not the whole f-string, so
    ``f"{escape_markdown(a.display_name)} vs {b.display_name}"`` still reports
    ``b`` — one escaped slot must not vouch for its neighbour.
    """
    tree = ast.parse(source)

    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.JoinedStr):
            continue
        for slot in node.values:
            if not isinstance(slot, ast.FormattedValue):
                continue
            if any(
                isinstance(sub, ast.Call) and _callee(sub) in SAFE_CALLS
                for sub in ast.walk(slot)
            ):
                continue
            for sub in ast.walk(slot):
                if isinstance(sub, ast.Attribute) and sub.attr in NAME_ATTRS:
                    found.append((sub.lineno, ast.unparse(sub)))
    return found


def _callee(call: ast.Call) -> str:
    fn = call.func
    return fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")


def test_sweep_sees_the_source_tree() -> None:
    """A moved tree would make the check below pass vacuously."""
    assert len(_modules()) >= 300


@pytest.mark.parametrize(
    ("source", "caught"),
    [
        pytest.param('f"hi {member.display_name}"', True, id="bare-read"),
        pytest.param('f"hi {member.nick}"', True, id="nick"),
        pytest.param('f"hi {u.global_name}"', True, id="global-name"),
        pytest.param(
            'f"{escape_markdown(member.display_name)}"', False, id="guarded"
        ),
        pytest.param(
            'f"{discord.utils.escape_markdown(member.display_name)}"',
            False,
            id="guarded-qualified",
        ),
        pytest.param(
            'f"{escape_to_fit(member.display_name, NAME_LIMIT)}"',
            False,
            id="guarded-by-escape-to-fit",
        ),
        pytest.param(
            'f"{escape_markdown(a.display_name)} beat {b.display_name}"',
            True,
            id="one-escaped-slot-does-not-vouch-for-its-neighbour",
        ),
        pytest.param('f"{member.name}"', False, id="dot-name-is-not-a-member-name"),
        pytest.param('x = member.display_name', False, id="not-in-an-f-string"),
    ],
)
def test_the_detector_actually_bites(source: str, caught: bool) -> None:
    """A born-green guard that never demonstrates a catch proves nothing.

    Every row above is the check's own behaviour, not the tree's: if a future
    edit makes the AST walk silently match nothing, these fail while the
    sweep over ``src/`` would still pass green and empty.
    """
    assert bool(_unguarded_source(source)) is caught


def test_every_allowed_row_still_exists() -> None:
    """An excuse outlives the code it excused, and then it is just a hole.

    When a site stops reading a raw name — it moved to a ``name_fn``, gained
    an ``escape_markdown``, or went away — its row here has to go too, or the
    next raw read of the same expression in that file inherits an exemption
    nobody granted it.
    """
    present = {
        (path.relative_to(SRC).as_posix(), expr)
        for path in _modules()
        for _, expr in _unguarded(path)
    }
    stale = sorted(
        f"{path} :: {expr}" for path, expr in ALLOWED if (path, expr) not in present
    )
    assert not stale, (
        "ALLOWED excuses an unguarded name read that is no longer there. "
        "Delete the row:\n  " + "\n  ".join(stale)
    )


def test_member_names_are_escaped_outside_embed_builders() -> None:
    offenders = []
    for path in _modules():
        rel = path.relative_to(SRC).as_posix()
        for lineno, expr in _unguarded(path):
            if (rel, expr) in ALLOWED:
                continue
            offenders.append(f"src/{rel}:{lineno} {expr}")

    assert not offenders, (
        "a member name is being interpolated into an f-string without "
        "escaping:\n  "
        + "\n  ".join(offenders)
        + "\n\nDiscord renders markdown in message content, in an embed's "
        "title/description/field values, and in a Components V2 Text Display, "
        "so '**Moderator**' renders bold and '||x||' spoilers whatever "
        "follows. Wrap it: discord.utils.escape_markdown(member.display_name) "
        "— or, if you hold an id rather than a Member, take the name through "
        "services/name_resolver, which escapes what it returns.\n\n"
        "If the value genuinely cannot be forged or does not land on a "
        "rendering surface — a footer, an author line, a button or "
        "select-option label, an audit-log reason, a value written back to "
        "Discord, or text inside a `code span` where markdown does not render "
        "at all — add a row to ALLOWED saying which. Do not escape inside a "
        "code span: it prints a literal backslash, and the risk there is a "
        "backtick closing the span early."
    )
