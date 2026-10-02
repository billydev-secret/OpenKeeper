"""Repo-wide style rules for embeds and member-facing replies.

``docs/embed_style_guide.md`` is the standard. Two of its rules already have
teeth — accent colour (``test_embed_accent_contract.py``) and member names
(the per-feature ``name_fn`` sweeps) — and everything else drifted quietly for
want of a gate. A 2026-09 review found 77 denial replies missing the ``❌``
the 2026-07-21 ruling requires, nine footers separating with ``·`` instead of
``•``, and 101 multi-field builders never calling ``apply_section_spacing``.
None of that was catchable by review alone at 364 call sites.

Each sweep below owns one rule, names the guide section it comes from, and
carries a "guards the guard" meta-test — the check is only worth having if a
reintroduced violation is actually seen.

Adding a builder? None of this asks anything of you that the guide doesn't
already: prefix a denial with ``❌``, separate footer clauses with ``•``, say
"Pick…" on a select, and call ``apply_section_spacing(embed)`` once after the
fields on any card with more than one (the helper skips inline fields itself).

Building a Components V2 card instead? The same rules reach it, checked where
its text is written rather than at the send, because a V2 message has no
``content`` to look at: a ``TextDisplay``'s string and a bare string child of
a ``Section`` are the card's copy, a ``##``/``###`` line inside one is a
title, and a ``-#`` line inside one is the footer
(``components_v2_style_guide.md`` § What carries over unchanged). Those arms
were born green on 2026-09-20, when the tree held one ``TextDisplay`` — the
Two Truths modal's prompt. The two surviving ``-#`` lines that separate with
``·`` (``advisor_cog``, ``survivor/tasks``) are embed field text and message
text, not footers, so they stay out of scope; rewording them would change
what a member reads, which is not a test's call to make.
"""

from __future__ import annotations

import ast
import re

import pytest
from tests.paths import REPO_ROOT

SRC = REPO_ROOT / "src"

_SEND_METHODS = {"send_message", "send", "edit_message", "respond"}

#: Local helpers that forward their text straight to one of the above. A sweep
#: that only looked at ``send``-shaped calls missed every denial routed through
#: one of these — six in ``roles/menus/views.py`` alone — so they count as sends.
#: Add a new wrapper here when you write one.
_SEND_WRAPPERS = {
    "_reply",
    "_ephemeral",
    "safe_ephemeral",
    "_safe_ephemeral",
}

#: Replies that open by refusing. The guide's own worked example is
#: "❌ Only the host or a mod can start."
_REFUSAL = re.compile(
    r"^(only\b|you can'?t\b|you cannot\b|i can'?t\b|i cannot\b"
    r"|i don'?t have permission\b|you don'?t have permission\b"
    r"|you need\b|i need\b)",
    re.I,
)

#: Openers that read as a refusal but are really an *empty state* or narration
#: ("No one is in the hot seat.", "Nobody submitted a price this round."). The
#: guide wants those as a plain sentence with no emoji, so they must NOT be
#: swept into the ❌ rule. → guide § Empty states & pagination
_NOT_A_REFUSAL = re.compile(
    r"^(no one\b|nobody\b|there'?s no\b|there is no\b|you are not watching\b)",
    re.I,
)

#: Prefixes that already mark a reply's register.
_MARKERS = ("❌", "✅", "⚠️", "🔒", "⏳", "🎉", "💡", "🚫")


def _py_files():
    for path in sorted(SRC.rglob("*.py")):
        yield path, path.relative_to(SRC).as_posix()


def _leading_text(node: ast.expr) -> str | None:
    """The literal text a string or f-string starts with, or None."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                return value.value
            return ""
    return None


def _content_arg(call: ast.Call) -> ast.expr | None:
    """The message body of a send-shaped call: first positional or content=."""
    if call.args:
        return call.args[0]
    for keyword in call.keywords:
        if keyword.arg == "content":
            return keyword.value
    return None


def _is_interaction_reply(call: ast.Call) -> bool:
    """A reply to an interaction, rather than a broadcast into a channel."""
    receiver = ast.unparse(call.func)
    return any(
        marker in receiver
        for marker in ("interaction.", ".response", ".followup", "itx.")
    )


# ── Components V2 text sinks ──────────────────────────────────────────

#: The two components whose arguments Discord renders as card copy.
_V2_TEXT_SINKS = ("TextDisplay", "Section")


def _v2_text_aliases(tree: ast.AST) -> dict[str, str]:
    """Local names this module bound to ``discord.ui``'s text components.

    A bare ``Section(`` only counts when the module imported it from
    ``discord.ui``: ``src/dk_mcp`` has an unrelated ``Section`` of its own,
    and sweeping its arguments for card copy would be nonsense.
    """
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "discord.ui":
            for alias in node.names:
                if alias.name in _V2_TEXT_SINKS:
                    aliases[alias.asname or alias.name] = alias.name
    return aliases


def _v2_text_strings(call: ast.Call, aliases: dict[str, str]) -> list[ast.expr]:
    """The arguments of ``call`` that Discord renders as text, if any.

    ``TextDisplay`` takes its markdown first-positional or as ``content=``.
    A ``Section``'s positional arguments are its children — an ``Item``, or a
    bare string Discord turns into a Text Display — while its ``accessory``
    is keyword-only and holds a button or a thumbnail, never text.
    """
    receiver = ast.unparse(call.func)
    kind = next((k for k in _V2_TEXT_SINKS if receiver.endswith(f"ui.{k}")), None)
    if kind is None and isinstance(call.func, ast.Name):
        kind = aliases.get(call.func.id)
    if kind == "TextDisplay":
        return [
            *call.args[:1],
            *(k.value for k in call.keywords if k.arg == "content"),
        ]
    if kind == "Section":
        return list(call.args)
    return []


def _literal_skeleton(node: ast.expr) -> str:
    """Everything literal in a string or f-string, spliced values dropped."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(
            value.value
            for value in node.values
            if isinstance(value, ast.Constant) and isinstance(value.value, str)
        )
    return ""


def _card_body_lines(node: ast.expr) -> list[str]:
    """A card's prose lines — ``#`` headings and ``-#`` subtext dropped.

    A heading answers to the Title Case rule and a ``-#`` line to the footer
    rule; the sentences between them are the card's body.
    """
    return [
        line
        for raw in _literal_skeleton(node).splitlines()
        if (line := raw.strip())
        and not line.startswith("#")
        and not line.startswith("-#")
    ]


# ── ❌ on denials (ruling 2026-07-21) ─────────────────────────────────


def _unmarked_refusal(text: str) -> bool:
    """``text`` opens by refusing and carries none of the register markers."""
    stripped = text.strip()
    if not stripped or stripped.startswith(_MARKERS):
        return False
    if _NOT_A_REFUSAL.match(stripped):
        return False
    return bool(_REFUSAL.match(stripped))


def unprefixed_denials(tree: ast.AST) -> list[int]:
    """Lines where a reply or a V2 card refuses without the ``❌`` prefix."""
    aliases = _v2_text_aliases(tree)
    hits: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        # A V2 card has no ``content=`` to inspect at the send, so the check
        # moves to where its text display is built. Only the body lines are
        # in scope — see ``_card_body_lines``.
        v2_texts = _v2_text_strings(node, aliases)
        if v2_texts:
            for arg in v2_texts:
                if any(_unmarked_refusal(line) for line in _card_body_lines(arg)):
                    hits.append(arg.lineno)
            continue
        if isinstance(node.func, ast.Attribute) and node.func.attr in _SEND_METHODS:
            wrapped = False
        elif isinstance(node.func, ast.Name) and node.func.id in _SEND_WRAPPERS:
            wrapped = True
        elif isinstance(node.func, ast.Attribute) and node.func.attr in _SEND_WRAPPERS:
            wrapped = True
        else:
            continue
        # A wrapper takes the interaction first, so scan every literal argument
        # rather than assuming the body is arg 0.
        args = list(node.args) if wrapped else [_content_arg(node)]
        for arg in args:
            if arg is None:
                continue
            text = _leading_text(arg)
            if not text:
                continue
            if _unmarked_refusal(text) and (wrapped or _is_interaction_reply(node)):
                hits.append(arg.lineno)
    return sorted(set(hits))


def test_every_denial_reply_opens_with_the_cross():
    """A denial that doesn't look like one reads as an ordinary reply.

    → ``embed_style_guide.md`` § Errors, denials & confirmations
    """
    offenders: list[str] = []
    for path, rel in _py_files():
        source = path.read_text(encoding="utf-8")
        if "send" not in source and "TextDisplay" not in source:  # cheap skip
            continue
        try:
            tree = ast.parse(source)
        except SyntaxError:
            continue
        offenders += [f"{rel}:{line}" for line in unprefixed_denials(tree)]

    assert not offenders, (
        "These refuse a member without the ❌ prefix the 2026-07-21 ruling "
        "requires. Say how to fix it too, and name the role where there is "
        "one ('❌ Only the host or a mod can start.'):\n  "
        + "\n  ".join(offenders)
    )


def test_the_denial_sweep_can_actually_see_a_violation():
    """Guards the guard, in both directions."""
    bare = ast.parse(
        'await interaction.response.send_message("Only the host can start.")'
    )
    prefixed = ast.parse(
        'await interaction.response.send_message("❌ Only the host can start.")'
    )
    empty_state = ast.parse(
        'await interaction.response.send_message("No one is in the hot seat.")'
    )
    broadcast = ast.parse('await channel.send("Only one price submitted.")')

    assert unprefixed_denials(bare) == [1]
    assert unprefixed_denials(prefixed) == []
    # An empty state is a plain sentence by design, not a denial.
    assert unprefixed_denials(empty_state) == []
    # A narration into the channel is not refusing anybody.
    assert unprefixed_denials(broadcast) == []


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        pytest.param(
            'discord.ui.TextDisplay("You can\'t start someone else\'s game.")',
            [1],
            id="bare-text-display",
        ),
        pytest.param(
            'discord.ui.TextDisplay("❌ You can\'t start someone else\'s game.")',
            [],
            id="marked-text-display",
        ),
        pytest.param(
            'discord.ui.TextDisplay(content="Only the host can start.")',
            [1],
            id="content-kwarg",
        ),
        pytest.param(
            'discord.ui.Section("Only the host can start.", accessory=btn)',
            [1],
            id="string-child-of-section",
        ),
        pytest.param(
            'discord.ui.Section(accessory=discord.ui.Button(label="Only Me"))',
            [],
            id="accessory-is-never-text",
        ),
        pytest.param(
            'discord.ui.TextDisplay("## Locked\\nYou need the Denizen role.")',
            [1],
            id="refusal-under-a-heading",
        ),
        pytest.param(
            'discord.ui.TextDisplay("## Locked\\n❌ You need the Denizen role.")',
            [],
            id="marked-refusal-under-a-heading",
        ),
        pytest.param(
            'discord.ui.TextDisplay("No one is in the hot seat.")',
            [],
            id="empty-state-card",
        ),
        pytest.param(
            'from dk_mcp.models import Section\nSection("You can\'t do that")',
            [],
            id="some-other-section",
        ),
    ],
)
def test_the_denial_sweep_reads_a_components_v2_card(source, expected):
    """A V2 message has no ``content``; the refusal is written in the card.

    → ``components_v2_style_guide.md`` § What carries over unchanged
    """
    assert unprefixed_denials(ast.parse(source)) == expected


def test_the_denial_sweep_follows_a_local_send_wrapper():
    """The first version of this sweep only looked at ``send``-shaped calls,
    so six denials in ``roles/menus/views.py`` that go through a local
    ``_reply(interaction, text)`` helper sailed past it."""
    wrapped = ast.parse('await _reply(interaction, "Only the host can start.")')
    wrapped_ok = ast.parse('await _reply(interaction, "❌ Only the host can start.")')

    assert unprefixed_denials(wrapped) == [1]
    assert unprefixed_denials(wrapped_ok) == []


# ── footers separate with • ───────────────────────────────────────────


def middot_footers(tree: ast.AST) -> list[int]:
    """Lines where footer text separates clauses with ``·``.

    Covers both shapes: a literal handed straight to ``set_footer``, and a
    string-layer builder that assembles the text and hands it over later
    (``todo/board_logic.render_board_footer`` joined on ``" · "`` and was
    invisible to a ``set_footer``-only sweep).

    A Components V2 card has no footer slot — the job moves to a ``-#``
    subtext line inside a text display, so a third shape counts: a ``-#``
    line, anywhere in a ``TextDisplay`` or a string child of a ``Section``,
    that separates with ``·``. Only ``-#`` lines; a ``·`` in a card's body
    text or a monospace table is not a footer and never was.
    """
    aliases = _v2_text_aliases(tree)
    hits: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            for arg in _v2_text_strings(node, aliases):
                for raw in _literal_skeleton(arg).splitlines():
                    if raw.strip().startswith("-#") and "·" in raw:
                        hits.append(arg.lineno)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr == "set_footer":
                for candidate in list(node.args) + [k.value for k in node.keywords]:
                    for piece in ast.walk(candidate):
                        if (
                            isinstance(piece, ast.Constant)
                            and isinstance(piece.value, str)
                            and "·" in piece.value
                        ):
                            hits.append(node.lineno)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if "footer" not in node.name.lower():
                continue
            docstring = node.body[0].value if _has_docstring(node) else None
            for piece in ast.walk(node):
                if piece is docstring:
                    continue  # prose about the footer, not the footer itself
                if (
                    isinstance(piece, ast.Constant)
                    and isinstance(piece.value, str)
                    and "·" in piece.value
                ):
                    hits.append(piece.lineno)
    return sorted(set(hits))


def _has_docstring(node: ast.AST) -> bool:
    body = getattr(node, "body", None)
    return bool(
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    )


def test_footers_separate_with_a_bullet():
    """→ ``embed_style_guide.md`` § Card anatomy (Separators)."""
    offenders: list[str] = []
    for path, rel in _py_files():
        source = path.read_text(encoding="utf-8")
        if (
            "set_footer" not in source
            and "TextDisplay" not in source
            and "Section" not in source
        ):
            continue
        try:
            tree = ast.parse(source)
        except SyntaxError:
            continue
        offenders += [f"{rel}:{line}" for line in middot_footers(tree)]

    assert not offenders, (
        "Footers separate clauses with a single-spaced ' • ', not '·':\n  "
        + "\n  ".join(offenders)
    )


def test_the_footer_sweep_can_actually_see_a_violation():
    assert middot_footers(ast.parse('e.set_footer(text="Pin of the Day · 24h")')) == [1]
    assert middot_footers(ast.parse('e.set_footer(text="Pin of the Day • 24h")')) == []
    # An f-string carries its literal pieces in the same place.
    assert middot_footers(ast.parse('e.set_footer(text=f"Page {n} · {ctx}")')) == [1]
    # A string-layer builder never touches set_footer, and was the shape the
    # first version of this sweep could not see.
    built = ast.parse('def render_board_footer(p):\n    return " · ".join(p)\n')
    built_ok = ast.parse('def render_board_footer(p):\n    return " • ".join(p)\n')
    assert middot_footers(built) == [2]
    assert middot_footers(built_ok) == []


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        pytest.param(
            'discord.ui.TextDisplay("-# Page 1 of 3 · refreshed hourly")',
            [1],
            id="subtext-line",
        ),
        pytest.param(
            'discord.ui.TextDisplay("-# Page 1 of 3 • refreshed hourly")',
            [],
            id="subtext-line-fixed",
        ),
        pytest.param(
            'discord.ui.TextDisplay(f"## Shop\\n-# {n} left · <t:{ts}:R>")',
            [1],
            id="interpolated-subtext",
        ),
        pytest.param(
            'discord.ui.TextDisplay("Two 7s **2×** · any pair **1.5×**")',
            [],
            id="body-text-is-not-a-footer",
        ),
        pytest.param(
            'discord.ui.Section("-# Sent · 2 minutes ago", accessory=btn)',
            [1],
            id="string-child-of-section",
        ),
        pytest.param(
            'from dk_mcp.models import Section\nSection("-# a · b")',
            [],
            id="some-other-section",
        ),
    ],
)
def test_the_footer_sweep_reads_a_components_v2_subtext_line(source, expected):
    """→ ``components_v2_style_guide.md`` § Anatomy (footer jobs move to ``-#``)."""
    assert middot_footers(ast.parse(source)) == expected


# ── selects say "Pick", not "Select" ──────────────────────────────────


def _is_select_call(call: ast.Call) -> bool:
    receiver = ast.unparse(call.func)
    return any(
        marker in receiver
        for marker in (
            "ui.select",
            "ui.Select",
            "UserSelect",
            "RoleSelect",
            "ChannelSelect",
            "MentionableSelect",
        )
    )


def select_says_select(tree: ast.AST) -> list[int]:
    hits: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not _is_select_call(node):
            continue
        for keyword in node.keywords:
            if keyword.arg != "placeholder":
                continue
            value = keyword.value
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                if "select" in value.value.lower():
                    hits.append(value.lineno)
    return hits


def test_select_placeholders_say_pick():
    """→ ``embed_style_guide.md`` § Buttons, modals & selects."""
    offenders: list[str] = []
    for path, rel in _py_files():
        source = path.read_text(encoding="utf-8")
        if "placeholder" not in source:
            continue
        try:
            tree = ast.parse(source)
        except SyntaxError:
            continue
        offenders += [f"{rel}:{line}" for line in select_says_select(tree)]

    assert not offenders, (
        "Select placeholders are imperative and say 'Pick…', not 'Select…':\n  "
        + "\n  ".join(offenders)
    )


def test_the_select_sweep_can_actually_see_a_violation():
    bad = ast.parse('discord.ui.Select(placeholder="Select a user")')
    good = ast.parse('discord.ui.Select(placeholder="Pick a user…")')
    assert select_says_select(bad) == [1]
    assert select_says_select(good) == []
    # A layout puts the same select classes inside an ``ActionRow``, so the
    # rule reaches a V2 card without extending: the nested call is still a
    # call. ``UserSelect`` and friends do not subclass ``Select``, which is
    # why the receiver check names each one.
    row = ast.parse(
        'discord.ui.ActionRow(discord.ui.UserSelect(placeholder="Select a member"))'
    )
    row_ok = ast.parse(
        'discord.ui.ActionRow(discord.ui.UserSelect(placeholder="Pick a member…"))'
    )
    assert select_says_select(row) == [1]
    assert select_says_select(row_ok) == []


# ── the colour= kwarg ─────────────────────────────────────────────────


def test_embeds_spell_the_colour_kwarg_color():
    """551 ``color=`` against one ``colour=`` when the guide was written; that
    one has since gone. Keep it that way — a split kwarg makes every
    colour-related sweep read both spellings forever."""
    offenders = [
        f"{rel}:{i}"
        for path, rel in _py_files()
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if "colour=" in line
    ]
    assert not offenders, "Use color=, not colour=:\n  " + "\n  ".join(offenders)


# ── section spacing on stacked cards ──────────────────────────────────


def _add_field_inline_flags(fn: ast.AST, var: str) -> list[object]:
    flags: list[object] = []
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call):
            continue
        if not isinstance(node.func, ast.Attribute) or node.func.attr != "add_field":
            continue
        if not isinstance(node.func.value, ast.Name) or node.func.value.id != var:
            continue
        inline: object = None
        for keyword in node.keywords:
            if keyword.arg == "inline":
                inline = getattr(keyword.value, "value", "expr")
        flags.append(inline)
    return flags


def unspaced_stacked_builders(tree: ast.AST) -> list[int]:
    """Builders that add 2+ fields and never call ``apply_section_spacing``.

    This used to be scoped to *pure stacked* cards, because the helper spaced
    inline fields too and that only made a three-across row taller. The helper
    skips ``inline=True`` now (ruling 2026-09-03), so the rule applies to every
    builder and the exemption is gone.

    It must not demand spacing of a Components V2 builder — a layout separates
    with ``Separator`` and has neither the spacer nor the helper
    (``components_v2_style_guide.md`` § Anatomy). No branch below says so
    because none is needed: the sweep keys off an ``Embed(`` assignment the
    function *returns*, which a layout builder has not got.
    ``test_the_spacing_sweep_does_not_reach_a_layout_builder`` pins that,
    rather than leaving a skip that would fire wrongly on the one shape which
    genuinely mixes the two — a flag-switched builder still returning an embed
    on its legacy path, which still wants its spacing.
    """
    hits: list[int] = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if any(
            isinstance(n, ast.Call)
            and isinstance(n.func, ast.Name)
            and n.func.id == "apply_section_spacing"
            for n in ast.walk(fn)
        ):
            continue
        built = {
            target.id
            for node in ast.walk(fn)
            if isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Attribute)
            and node.value.func.attr == "Embed"
            for target in node.targets
            if isinstance(target, ast.Name)
        }
        returned = {
            node.value.id
            for node in ast.walk(fn)
            if isinstance(node, ast.Return) and isinstance(node.value, ast.Name)
        }
        for var in built & returned:
            if len(_add_field_inline_flags(fn, var)) < 2:
                continue
            hits.append(fn.lineno)
    return sorted(set(hits))


def test_stacked_cards_get_their_section_spacing():
    """→ ``embed_style_guide.md`` § Section spacing (breathing room)."""
    offenders: list[str] = []
    for path, rel in _py_files():
        source = path.read_text(encoding="utf-8")
        if "add_field" not in source or "Embed(" not in source:
            continue
        try:
            tree = ast.parse(source)
        except SyntaxError:
            continue
        offenders += [f"{rel}:{line}" for line in unspaced_stacked_builders(tree)]

    assert not offenders, (
        "These stack sections without breathing room. Call "
        "apply_section_spacing(embed) once after the fields "
        "(bot_modules.core.branding — it is idempotent and a no-op under two "
        "fields):\n  " + "\n  ".join(offenders)
    )


def test_the_spacing_sweep_can_actually_see_a_violation():
    stacked = ast.parse(
        "def build():\n"
        "    embed = discord.Embed(title='x')\n"
        "    embed.add_field(name='a', value='1', inline=False)\n"
        "    embed.add_field(name='b', value='2', inline=False)\n"
        "    return embed\n"
    )
    spaced = ast.parse(
        "def build():\n"
        "    embed = discord.Embed(title='x')\n"
        "    embed.add_field(name='a', value='1', inline=False)\n"
        "    embed.add_field(name='b', value='2', inline=False)\n"
        "    apply_section_spacing(embed)\n"
        "    return embed\n"
    )
    triples = ast.parse(
        "def build():\n"
        "    embed = discord.Embed(title='x')\n"
        "    embed.add_field(name='a', value='1', inline=True)\n"
        "    embed.add_field(name='b', value='2', inline=False)\n"
        "    return embed\n"
    )
    single = ast.parse(
        "def build():\n"
        "    embed = discord.Embed(title='x')\n"
        "    embed.add_field(name='a', value='1', inline=False)\n"
        "    return embed\n"
    )

    assert unspaced_stacked_builders(stacked) == [1]
    assert unspaced_stacked_builders(spaced) == []
    # A card mixing inline triples is in scope now — the helper skips the
    # inline fields itself, so the builder still has to call it.
    assert unspaced_stacked_builders(triples) == [1]
    # One field has nothing to space against.
    assert unspaced_stacked_builders(single) == []


def test_the_spacing_sweep_does_not_reach_a_layout_builder():
    """A layout gets its air from ``Separator``, never ``apply_section_spacing``.

    → ``components_v2_style_guide.md`` § Anatomy: "this replaces the
    zero-width-space ``SECTION_SPACER`` and ``apply_section_spacing``
    entirely — neither applies to a layout, and the spacing sweep in
    ``test_embed_style_contract.py`` must not demand it of one."
    """
    layout = ast.parse(
        "def build_shop_layout(name_fn):\n"
        "    view = discord.ui.LayoutView()\n"
        "    view.add_item(discord.ui.TextDisplay('## Shop'))\n"
        "    view.add_item(discord.ui.Separator())\n"
        "    view.add_item(discord.ui.TextDisplay('### Perks'))\n"
        "    return view\n"
    )
    items = ast.parse(
        "def build_shop_items():\n"
        "    return [\n"
        "        discord.ui.TextDisplay('## Shop'),\n"
        "        discord.ui.Separator(),\n"
        "        discord.ui.TextDisplay('### Perks'),\n"
        "    ]\n"
    )
    assert unspaced_stacked_builders(layout) == []
    assert unspaced_stacked_builders(items) == []


@pytest.mark.parametrize(
    "name",
    ["apply_section_spacing", "SECTION_SPACER"],
)
def test_the_spacing_helper_still_lives_where_the_sweep_expects(name):
    """Guards the sweep's advice: if the helper moved, every failure message
    above would send the next person to a file that no longer has it."""
    from bot_modules.core import branding

    assert hasattr(branding, name)


# ── Title Case on titles and labels (ruling 2026-07-21) ───────────────

#: Words that stay lowercase inside a title — unless they open it, close it, or
#: open a clause after a separator.
_TITLE_STOPWORDS = {
    "a", "an", "and", "as", "at", "but", "by", "for", "from", "in", "into",
    "nor", "of", "on", "or", "per", "the", "to", "up", "vs", "via", "with",
    "over", "off", "out",
}
_WORD = re.compile(r"[A-Za-z][A-Za-z'’]*")
_LEAD_IN = re.compile(r"^[\W\d_]+", re.UNICODE)
#: A disabled option stub IS the empty-state message, not a label, and the
#: guide wants those as a plain sentence. → § Empty states & pagination
_EMPTY_STATE = re.compile(r"^(no |nobody|none|there'?s no )", re.I)


def _title_slot(call: ast.Call) -> str | None:
    """Which Title-Case slot this call writes, if any.

    Deliberately excludes ``TextInput(label=)`` and every ``placeholder``: the
    guide gives modal field labels their own rule (a terse noun phrase with a
    parenthetical hint — "Reason (optional)") and select placeholders another
    ("Pick …"). Applying Title Case to either is a false positive.
    """
    name = ast.unparse(call.func)
    if name.endswith("Embed"):
        return "title"
    if name.endswith("add_field"):
        return "name"
    if "TextInput" in name:
        return None
    if "Button" in name or "SelectOption" in name:
        return "label"
    if "Modal" in name:
        return "title"
    return None


def _reads_as_a_sentence(text: str) -> bool:
    """``text`` has a word past the first that Title Case would capitalise."""
    words = _WORD.findall(_LEAD_IN.sub("", text).strip())
    if len(words) < 2:
        return False
    for word in words[1:]:
        if word.lower() in _TITLE_STOPWORDS:
            continue
        if word[0].islower() and not word.isupper():
            return True
    return False


def sentence_case_labels(tree: ast.AST) -> list[int]:
    """Lines where a title or label reads as a sentence rather than a label."""
    aliases = _v2_text_aliases(tree)
    hits: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        # A V2 card's title is a ``##`` line and its section headings are
        # ``###`` lines, inside a text display rather than a ``title=``.
        for arg in _v2_text_strings(node, aliases):
            # Plain literals only. An interpolated heading is no more ours to
            # judge than an interpolated embed title, and an f-string's
            # literal pieces read as a sentence with the spliced words gone.
            if not (isinstance(arg, ast.Constant) and isinstance(arg.value, str)):
                continue
            for raw in arg.value.splitlines():
                line = raw.strip()
                if line.startswith("#") and _reads_as_a_sentence(line):
                    hits.append(arg.lineno)
                    break
        slot = _title_slot(node)
        if slot is None:
            continue
        for keyword in node.keywords:
            if keyword.arg != slot:
                continue
            value = keyword.value
            if not (isinstance(value, ast.Constant) and isinstance(value.value, str)):
                continue
            text = value.value
            if "{" in text:  # interpolated — the casing isn't ours to judge
                continue
            if slot == "label" and _EMPTY_STATE.match(text):
                continue
            if _reads_as_a_sentence(text):
                hits.append(value.lineno)
    return sorted(set(hits))


def test_titles_and_labels_are_title_case():
    """→ ``embed_style_guide.md`` § Titles, labels & casing."""
    offenders: list[str] = []
    for path, rel in _py_files():
        source = path.read_text(encoding="utf-8")
        if (
            "add_field" not in source
            and "Embed(" not in source
            and "label=" not in source
            and "TextDisplay" not in source
            and "Section" not in source
        ):
            continue
        try:
            tree = ast.parse(source)
        except SyntaxError:
            continue
        offenders += [f"{rel}:{line}" for line in sentence_case_labels(tree)]

    assert not offenders, (
        "Embed titles, field names, button labels and modal titles use Title "
        "Case (ruling 2026-07-21). Prose — descriptions, errors, DMs — stays "
        "sentence-style:\n  " + "\n  ".join(offenders)
    )


def test_the_title_case_sweep_can_actually_see_a_violation():
    def one(src):
        return sentence_case_labels(ast.parse(src))

    assert one('discord.Embed(title="Daily streak")') == [1]
    assert one('discord.Embed(title="Daily Streak")') == []
    # A leading emoji doesn't excuse the words after it. The example is
    # deliberately synthetic: an earlier sweep over the test suite "fixed" a
    # realistic one and broke this guard, which is how it earned the comment.
    assert one('e.add_field(name="🧪 Fixture label", value="x")') == [1]
    # Stopwords stay lowercase inside a title.
    assert one('discord.Embed(title="Nerves of Steel")') == []
    # Acronyms keep their shape.
    assert one('e.add_field(name="NSFW Tags", value="x")') == []
    # A modal field label is a noun phrase with a hint, NOT Title Case.
    assert one('discord.ui.TextInput(label="Reason (optional)")') == []
    # A disabled option stub is an empty state, not a label.
    assert one('discord.SelectOption(label="No results", value="x")') == []
    # An interpolated title is not ours to judge.
    assert one('discord.Embed(title=f"Pools — {label}")') == []


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        pytest.param(
            'discord.ui.TextDisplay("## Daily streak")',
            [1],
            id="card-title",
        ),
        pytest.param(
            'discord.ui.TextDisplay("## Daily Streak")',
            [],
            id="card-title-fixed",
        ),
        pytest.param(
            'discord.ui.TextDisplay("## 📬 DM Requests\\n### Your connections")',
            [1],
            id="section-heading",
        ),
        pytest.param(
            'discord.ui.TextDisplay("## 📬 DM Requests\\n### Your Connections")',
            [],
            id="section-heading-fixed",
        ),
        pytest.param(
            'discord.ui.TextDisplay("## Nerves of Steel\\nYou held on longest.")',
            [],
            id="body-prose-stays-prose",
        ),
        pytest.param(
            'discord.ui.Section("### Send a request", accessory=btn)',
            [1],
            id="string-child-of-section",
        ),
        pytest.param(
            'discord.ui.TextDisplay(f"## Pools — {label} today")',
            [],
            id="interpolated-heading",
        ),
        pytest.param(
            'from dk_mcp.models import Section\nSection("## some other heading")',
            [],
            id="some-other-section",
        ),
    ],
)
def test_the_title_case_sweep_reads_a_components_v2_heading(source, expected):
    """A layout has no ``title=``; its title is a ``##`` line in a card.

    → ``components_v2_style_guide.md`` § Anatomy
    """
    assert sentence_case_labels(ast.parse(source)) == expected
