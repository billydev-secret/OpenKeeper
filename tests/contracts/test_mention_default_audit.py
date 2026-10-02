"""Every send that pings says so — a static sweep of ``src/``.

``core.app_context.Bot`` is constructed with
``allowed_mentions=discord.AllowedMentions.none()``, so a mention in a
message's ``content`` renders but notifies nobody unless that send passes
its own allow-list. That default exists for Components V2: an embed
mention displayed without notifying, but a Text Display's behaves like
content and *does* notify (confirmed by the shop pilot, 2026-09-20), so
converting a builder under discord.py's permissive default would have
turned quiet cards into pings. Suppressing by construction designs the
regression class out; this sweep is the other half, and keeps the default
from silently eating a ping that mattered.

It walks every module's AST and reports a send whose ``content``

* is a literal (or f-string) holding ``<@``, ``<@&``, ``@everyone`` or
  ``@here``, reads a ``.mention`` attribute, calls the
  ``name_resolver.mention(uid)`` builder (which *is* ``f"<@{uid}>"``, and
  reads like any other helper — Marry/F./Kill's reveal built its whole
  ping line from one), or is a local bound from any of those — through as
  many hops as the function takes, since the bump reminder builds
  ``role_mention`` and then folds it into ``content`` a line later, and a
  one-hop reader called that file clean. A local can be bound by
  ``.append``/``.extend`` as well as by ``=``: the policy-vote call-out
  collected ``role.mention`` into a list in a loop and ``' '.join``-ed it,
  which no assignment-only pass could see — **and**
* passes no ``allowed_mentions=`` and no ``**…ping_kwargs(...)`` unpack,
  **or** passes one that a branch can leave as ``None``. Under the
  suppressive default a bare ``None`` is not "whatever the old default
  did", it *is* the default: AMA's hot-seat announcement built its
  allow-list only when a ping role was dialled, so in a guild without one
  every 🔔 Notify Me subscriber would have gone quiet.

Sends counted: ``send``, ``send_message``, ``reply``, ``edit``,
``edit_message``, ``edit_original_response``, and the repo's own one-level
wrappers (``_reply``, ``_send``, ``_try_dm``, ``_announce_to_channel``,
``send_branded_dm``). ``_announce_to_channel`` is ``duels.base_game``'s
shared poster: one unguarded ``channel.send`` in a base class silenced a
host's lobby warning, a winner's naming reminder and the outranked-role
notice across four game cogs at once, and every call site looked like an
ordinary pinging send. A
wrapper's body is read from *every* positional argument and from
``content=``/``layout=``: the ``_reply`` helpers take their text
positionally, while everything after ``user`` on ``send_branded_dm`` is
keyword-only, so checking one shape and not the other would read a whole
entry point as clean. An ``ephemeral=True`` send is skipped outright — it is delivered only to the
person who invoked it and cannot notify anyone else — as are the three
ephemeral-only helpers (``safe_ephemeral`` and friends), each of which was
read to confirm it sends no other way.

Which sends *should* ping, decided site by site on 2026-09-20 over the 49
the sweep first found: **a send keeps its ping when the mention is
addressed to the person and tells them to act** — your turn, your
question, your game is starting, your round resumed. Those 43 got
``core.utils.allow_pings(users=…, roles=…)`` naming exactly who the text
names, which is what the old permissive default had been doing implicitly.
Nothing a member sees changed. Review found three more the first pass had
no way to see — the bump reminder's role ping, AMA's 🔔 Notify Me
subscribers and TTL's Final Results winners — so 46 sends now carry an
allow-list, and two of the three blind spots that hid them are closed
above. A second pass found six more, in four shapes: a role ping collected
with ``list.append``, a mention built by the ``mention(uid)`` helper, three
sends through ``duels.base_game``'s shared ``_announce_to_channel``, and
``/games join``'s roster announcement. The scanner reads the first three
shapes now; the fourth is a ``CROSS_FUNCTION_PINGS`` row, because no static
rule reaches a message built by whichever cog a runtime dict lookup
happened to return.

``SILENT_BY_DESIGN`` holds the remaining 6, and it is not a debt ledger:
each row is a site where no ping was ever possible, so an allow-list would
have been decoration. The stale check makes a row that stops applying
delete itself. A row is a claim about the send, not about the interaction
around it: ``/grant``'s announcement was excused as "ephemeral" because
the *defer* before it is ephemeral, and a ``followup.send`` without its own
``ephemeral=True`` is a plain public message
(``Webhook.send``'s parameter defaults to ``False``). It is allow-listed
now.

**What it cannot see** — so don't read green as proof: a mention built in
one function and sent from another, a mention assembled by ``str.join`` on
a list built elsewhere, and a wrapper two levels deep. The worst shape is
all three at once and dynamically dispatched: ``/games join`` posts a
message built by whichever cog registered the game's
``bot.game_joiners`` handler, fetched out of a dict at runtime, so the
mention and the send are not in the same file, class or call graph.
``CROSS_FUNCTION_PINGS`` is the manual half: a site found by reading, named
here so the allow-list it was given cannot quietly be taken away again.

A **template loaded from the database** — a greeter arrival line, a grant
announcement, a birthday wish — was listed there too, and it was the worst
of them: the mention is text an admin typed into a dashboard box, so no
amount of reading ``src/`` can tell that the send pings. It is closed now,
by hand and by name. ``TEMPLATE_RENDERERS`` lists the functions whose
return value *is* a rendered template, and every send fed from one of them
must pass an ``allowed_mentions=``; ``core.utils.template_pings`` is the
one to pass, because it derives the allow-list from the raw template text
rather than from the rendered result. That closes the gap only for the
names in the ledger: **a new template renderer is invisible to this file
until someone adds its name to TEMPLATE_RENDERERS**, and nothing but this
sentence will say so.

One more, new with Components V2: a card reaches a *plain* send as
``view=``, and only ``send_branded_dm``'s ``layout=`` is read here. A
``channel.send(view=card)`` whose card was built in another function is
invisible twice over. Cards are built by ``*_card``/``build_*`` helpers,
not inline at the send, so the cross-function blind spot above is the one
that covers this; the allow-list still belongs on the send.
"""

from __future__ import annotations

import ast
import functools
import re

import pytest
from tests.paths import REPO_ROOT

SRC = REPO_ROOT / "src"

_SEND_ATTRS = {
    "send",
    "send_message",
    "reply",
    "edit",
    "edit_message",
    "edit_original_response",
}
#: Repo-local helpers that forward to one of the above; followed one level,
#: the way the denial sweep in test_embed_style_contract.py does.
#: ``_announce_to_channel`` is ``duels.base_game``'s shared poster, inherited
#: by every duels-style game cog. It is here because a base-class send helper
#: is worse than an ordinary cross-function ping: one unguarded
#: ``channel.send`` silenced a host's lobby warning, a winner's naming
#: reminder and the outranked-role notice at once, in four cogs, and each
#: call site reads as a perfectly ordinary pinging send.
_WRAPPERS = {"_reply", "_send", "_try_dm", "_announce_to_channel", "send_branded_dm"}
#: Helpers that only ever send ``ephemeral=True`` (each body read to check).
_EPHEMERAL_WRAPPERS = {"_ephemeral", "safe_ephemeral", "_safe_ephemeral"}

_MENTION_RE = re.compile(r"<@|@everyone|@here")

#: Bare calls that *are* a mention, though nothing about the expression says
#: so. ``services.name_resolver.mention(uid)`` returns ``f"<@{uid}>"``; read
#: as an ordinary call it looks as innocent as ``name_fn(uid)`` next to it,
#: and Marry/F./Kill's reveal built its whole ping line out of one and was
#: read clean. Only a bare ``mention(...)`` matches — an attribute call like
#: ``member.mention()`` is somebody else's method.
_MENTION_BUILDERS = {"mention"}

#: ``path::qualname`` -> why this send cannot notify anyone.
SILENT_BY_DESIGN: dict[str, str] = {
    # ── channel links, which AllowedMentions does not govern at all ──
    "bot_modules/bios/cog.py::BiosCog._prompt_resume_restart.on_resume":
        "'<#id>' for the wizard channel — a channel link is not a ping",
    # ── ephemeral, so only the person who acted ever sees it ─────────
    "bot_modules/economy/economy_cog.py::EconomyCog.finalize_gift":
        "edits an ephemeral response (both paths defer ephemerally)",
    # ── edits of an already-posted message ───────────────────────────
    "bot_modules/voice/master/voice_master_commands.py::_KnockResponseView.accept":
        "rewrites the knock card in place; Discord notifies on create, not edit",
    "bot_modules/voice/master/voice_master_commands.py::_KnockResponseView.deny":
        "rewrites the knock card in place; Discord notifies on create, not edit",
}

#: ``path::qualname`` -> (the send's ``content`` — or, for a Components V2
#: card, ``view`` — expression, what it pings). Sends the sweep is
#: structurally blind to because the mention text is built in *another*
#: function, found by reading rather than by scanning. Each row asserts only
#: what a scan cannot: that the site still passes a concrete allow-list. A
#: row whose expression no longer appears fails as stale, the same way
#: ``SILENT_BY_DESIGN`` does.
CROSS_FUNCTION_PINGS: dict[str, tuple[str, str]] = {
    "bot_modules/games/ttl/cog.py::TTLCog._run_guessing": (
        "recap",
        "the Final Results winner ping — the recap card names each winner "
        "still in the guild as a raw mention (games/ttl/layouts.py, "
        "build_recap_layout's `pingable`), since a V2 message has no "
        "content= line to carry it",
    ),
    "bot_modules/games/config/cog.py::GamesConfigCog._membership_command": (
        "message",
        "/games join's roster announcement — the text is built by whichever "
        "cog registered the game's bot.game_joiners handler (Clapback tells "
        "the bumped bye player they are back in, with a raw <@id>) and comes "
        "back through a runtime dict lookup, so no reading of this file can "
        "tell that the message pings. The handler hands the ids over as the "
        "third element of its return (app_context.GameRosterHandler)",
    ),
}


#: Function name -> what admin-authored template it renders.
#: The sweep reads the *text* of a send's ``content``; these functions
#: return text that lives in the database, so their mentions are invisible
#: to it — the greeter line's default is literally ``@here - {member} has
#: arrived`` and the sweep read it as clean while three guilds pinged
#: ``@here`` with it. Sends fed from one of these are swept separately
#: below and must carry an allow-list, which for a template means
#: ``core.utils.template_pings(the_raw_template, users=…, roles=…)``.
#: Only a bare call (``render_arrival_message(...)``) is matched, not
#: ``self._render(...)``: these are module-level renderers, and matching
#: attribute calls would make the generic name ``_render`` collide with
#: every unrelated method that happens to share it.
TEMPLATE_RENDERERS: dict[str, str] = {
    "render_arrival_message": "the greeter-chat arrival line (welcome_service)",
    "_resolve_grant_message": "/grant's announcement (role_grant_commands)",
    "_render": "the birthday wish (birthday_cog)",
}


def _mentiony(node: ast.AST, tainted: set[str]) -> bool:
    for sub in ast.walk(node):
        if (
            isinstance(sub, ast.Constant)
            and isinstance(sub.value, str)
            and _MENTION_RE.search(sub.value)
        ):
            return True
        if isinstance(sub, ast.Attribute) and sub.attr == "mention":
            return True
        if (
            isinstance(sub, ast.Call)
            and isinstance(sub.func, ast.Name)
            and sub.func.id in _MENTION_BUILDERS
        ):
            return True
        if isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Load):
            if sub.id in tainted:
                return True
    return False


def _tainted_locals(scope: ast.AST) -> set[str]:
    """Locals in ``scope`` that hold a mention, however many hops away.

    Re-walked until nothing new is tainted, because a mention rarely
    reaches the send in one assignment: the bump reminder binds
    ``role_mention`` from an f-string and ``content`` from *that*, and a
    single-hop pass (which is what this was) read the send as clean and
    let an unguarded role ping ship.
    """
    names: set[str] = set()
    while True:
        grown = False
        for node in ast.walk(scope):
            if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)) and node.value:
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                if not _mentiony(node.value, names):
                    continue
                for target in targets:
                    if isinstance(target, ast.Name) and target.id not in names:
                        names.add(target.id)
                        grown = True
            elif _appends_a_mention(node, names):
                assert isinstance(node, ast.Call)
                name = node.func.value.id  # type: ignore[union-attr]
                if name not in names:
                    names.add(name)
                    grown = True
        if not grown:
            return names


def _appends_a_mention(node: ast.AST, tainted: set[str]) -> bool:
    """``lst.append(<a mention>)`` / ``lst.extend(...)`` on a plain local.

    A list filled in a loop is never *assigned* a mention, so the pass above
    saw nothing: the policy-vote call-out collected ``role.mention`` with
    ``.append`` and ``' '.join``-ed the result, and the sweep read a send that
    pinged every mod and admin role as clean.
    """
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in ("append", "extend")
        and isinstance(node.func.value, ast.Name)
        and any(_mentiony(arg, tainted) for arg in node.args)
    )


def _templated(node: ast.AST, tainted: set[str]) -> bool:
    """Whether this expression is (or was built from) a rendered template."""
    for sub in ast.walk(node):
        if (
            isinstance(sub, ast.Call)
            and isinstance(sub.func, ast.Name)
            and sub.func.id in TEMPLATE_RENDERERS
        ):
            return True
        if isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Load):
            if sub.id in tainted:
                return True
    return False


def _template_locals(scope: ast.AST) -> set[str]:
    """Locals in ``scope`` holding a rendered template, however many hops away.

    The same fixed point ``_tainted_locals`` runs, and needed for the same
    reason: neither greeter-chat send passes the renderer's call to
    ``send`` directly — both bind ``arrival_line`` first, check it for
    emptiness, and send the local.
    """
    names: set[str] = set()
    while True:
        grown = False
        for node in ast.walk(scope):
            if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)) and node.value:
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                if not _templated(node.value, names):
                    continue
                for target in targets:
                    if isinstance(target, ast.Name) and target.id not in names:
                        names.add(target.id)
                        grown = True
        if not grown:
            return names


def _none_bound(scope: ast.AST) -> set[str]:
    """Locals in ``scope`` that some path assigns a literal ``None``.

    An ``allowed_mentions=`` naming one of these is not a guard: with the
    client default at ``none()`` the ``None`` branch sends the suppressive
    default, so the send is as silent as one passing nothing at all.
    """
    names: set[str] = set()
    for node in ast.walk(scope):
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        else:
            continue
        if isinstance(node.value, ast.Constant) and node.value.value is None:
            names.update(t.id for t in targets if isinstance(t, ast.Name))
    return names


def _is_none(value: ast.AST, none_bound: set[str]) -> bool:
    """Whether this ``allowed_mentions=`` argument can arrive as ``None``."""
    if isinstance(value, ast.Constant) and value.value is None:
        return True
    return isinstance(value, ast.Name) and value.id in none_bound


def _scopes(tree: ast.Module):
    """(qualified name, node) for every function, plus the module itself."""
    yield "<module>", tree

    def visit(node: ast.AST, prefix: str):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                qual = f"{prefix}{child.name}"
                yield qual, child
                yield from visit(child, f"{qual}.")
            elif isinstance(child, ast.ClassDef):
                yield from visit(child, f"{prefix}{child.name}.")
            else:
                yield from visit(child, prefix)

    yield from visit(tree, "")


def _innermost_scope(tree: ast.Module) -> dict[int, str]:
    owner: dict[int, str] = {}
    for qual, scope in _scopes(tree):
        for node in ast.walk(scope):
            owner[id(node)] = qual  # deeper scopes overwrite outer ones
    return owner


def _callee(node: ast.Call) -> str | None:
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return None


def _scan(source: str, is_tainted, locals_of) -> list[tuple[str, int, str]]:
    """Every send in one module whose content is tainted and unguarded.

    Shared by both sweeps, because "which sends count, which arguments are
    the content, and what counts as an allow-list" is one answer and the
    two differ only in what taints a value: a mention in the text
    (``_mentiony``) or a rendered admin template (``_templated``).
    """
    tree = ast.parse(source)
    owner = _innermost_scope(tree)
    hits: set[tuple[str, int, str]] = set()

    for qual, scope in _scopes(tree):
        module = isinstance(scope, ast.Module)
        tainted = set() if module else locals_of(scope)
        none_bound = set() if module else _none_bound(scope)
        for node in ast.walk(scope):
            if owner.get(id(node)) != qual or not isinstance(node, ast.Call):
                continue
            name = _callee(node)
            if name is None or name in _EPHEMERAL_WRAPPERS:
                continue
            wrapper = name in _WRAPPERS
            if not wrapper and name not in _SEND_ATTRS:
                continue
            kw = {k.arg: k.value for k in node.keywords if k.arg}
            if "allowed_mentions" in kw and not _is_none(kw["allowed_mentions"], none_bound):
                continue
            ephemeral = kw.get("ephemeral")
            if isinstance(ephemeral, ast.Constant) and ephemeral.value is True:
                continue
            unpacks = [k.value for k in node.keywords if k.arg is None]
            if any("ping_kwargs" in ast.unparse(u) for u in unpacks):
                continue
            # A real send takes the first positional arg or ``content=``.
            # A wrapper can take its body either way: ``_reply(i, text)``
            # is positional, while ``send_branded_dm``'s parameters after
            # ``user`` are keyword-only, so ``content=``/``layout=`` is the
            # only shape its calls can have.
            if wrapper:
                candidates = list(node.args)
            else:
                candidates = [*node.args[:1]]
            for key in ("content", "layout") if wrapper else ("content",):
                if key in kw:
                    candidates.append(kw[key])
            for candidate in candidates:
                if is_tainted(candidate, tainted):
                    hits.add((qual, node.lineno, ast.unparse(candidate)[:100]))
                    break
    return sorted(hits, key=lambda h: (h[1], h[0]))


def scan_source(source: str) -> list[tuple[str, int, str]]:
    """Every unguarded pinging send in one module, as (function, line, text)."""
    return _scan(source, _mentiony, _tainted_locals)


def scan_template_sends(source: str) -> list[tuple[str, int, str]]:
    """Every unguarded send in one module fed by a ``TEMPLATE_RENDERERS`` call."""
    return _scan(source, _templated, _template_locals)


def send_guard(source: str, qual: str, content_expr: str) -> bool | None:
    """Whether the send in ``qual`` whose ``content`` is ``content_expr``
    passes an allow-list that cannot be ``None``.

    ``None`` when no such send exists — a stale ledger row, not a pass.
    """
    tree = ast.parse(source)
    owner = _innermost_scope(tree)
    scope = next((node for name, node in _scopes(tree) if name == qual), None)
    if scope is None:
        return None
    none_bound = _none_bound(scope)
    for node in ast.walk(scope):
        if owner.get(id(node)) != qual or not isinstance(node, ast.Call):
            continue
        name = _callee(node)
        if name not in _SEND_ATTRS and name not in _WRAPPERS:
            continue
        kw = {k.arg: k.value for k in node.keywords if k.arg}
        candidates = [*node.args[:1]]
        if "content" in kw:
            candidates.append(kw["content"])
        # A Components V2 card carries its ping in the card body, so the
        # expression a row names may be the send's ``view=``.
        if "view" in kw:
            candidates.append(kw["view"])
        if not any(ast.unparse(c) == content_expr for c in candidates):
            continue
        unpacks = [k.value for k in node.keywords if k.arg is None]
        if any("ping_kwargs" in ast.unparse(u) for u in unpacks):
            return True
        allowed = kw.get("allowed_mentions")
        return allowed is not None and not _is_none(allowed, none_bound)
    return None


@functools.cache
def _scan_tree() -> dict[str, list[tuple[int, str]]]:
    found: dict[str, list[tuple[int, str]]] = {}
    for path in sorted(SRC.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        if not _MENTION_RE.search(source) and ".mention" not in source:
            continue
        rel = path.relative_to(SRC).as_posix()
        for qual, lineno, text in scan_source(source):
            found.setdefault(f"{rel}::{qual}", []).append((lineno, text))
    return found


# ── the default itself ──────────────────────────────────────────────────


def test_the_bot_is_constructed_with_no_mentions_allowed():
    """The sweep below is only meaningful while the default is suppressive."""
    source = (SRC / "bot_modules" / "core" / "app_context.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(source)
    found = [
        ast.unparse(kw.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "__init__"
        for kw in node.keywords
        if kw.arg == "allowed_mentions"
    ]
    assert found == ["discord.AllowedMentions.none()"], (
        "core.app_context.Bot must pass allowed_mentions="
        "discord.AllowedMentions.none() to super().__init__ — every ping in "
        f"the bot is allow-listed on that assumption. Got: {found}"
    )


def test_allow_pings_pins_every_field():
    """The allow-list names every field, so no client default decides one."""
    from bot_modules.core.utils import allow_pings

    am = allow_pings(users=[7])
    assert am.everyone is False
    assert am.roles == []
    assert am.replied_user is False
    assert [o.id for o in am.users] == [7]


# ── the sweep can actually see a violation ──────────────────────────────


@pytest.mark.parametrize(
    "source",
    [
        pytest.param(
            "async def f(ch, m):\n    await ch.send(f'{m.mention} your turn')\n",
            id="attr-mention",
        ),
        pytest.param(
            "async def f(ch, uid):\n    await ch.send(content=f'<@{uid}> your turn')\n",
            id="raw-id",
        ),
        pytest.param(
            "async def f(ch):\n    await ch.send('@everyone look')\n",
            id="everyone",
        ),
        pytest.param(
            "async def f(ch, r):\n    await ch.send(f'<@&{r}> heads up')\n",
            id="role-id",
        ),
        pytest.param(
            "async def f(ch, m):\n    text = f'{m.mention} hi'\n    await ch.send(text)\n",
            id="one-hop-local",
        ),
        pytest.param(
            "async def f(ch, r):\n"
            "    rm = f'<@&{r}>'\n"
            "    text = f'{rm} ready'\n"
            "    await ch.send(text)\n",
            id="two-hop-local",
        ),
        pytest.param(
            "async def f(ch, m, r):\n"
            "    am = None\n"
            "    if r:\n"
            "        am = AllowedMentions(roles=[r])\n"
            "    await ch.send(f'{m.mention} hi', allowed_mentions=am)\n",
            id="allow-list-that-can-be-none",
        ),
        pytest.param(
            "async def f(ch, m):\n"
            "    await ch.send(f'{m.mention} hi', allowed_mentions=None)\n",
            id="allow-list-is-literally-none",
        ),
        pytest.param(
            "async def f(i, m):\n    await i.response.send_message(f'{m.mention} hi')\n",
            id="interaction-response",
        ),
        pytest.param(
            "async def f(i, m):\n    await self._reply(i, f'{m.mention} hi')\n",
            id="wrapper-positional",
        ),
        pytest.param(
            "async def f(u, m, p, g):\n"
            "    await send_branded_dm(u, db_path=p, guild=g, "
            "content=f'<@{u.id}> your turn')\n",
            id="wrapper-keyword-content",
        ),
        pytest.param(
            "async def f(u, m, p, g):\n"
            "    await send_branded_dm(u, db_path=p, guild=g, "
            "layout=Card(f'{m.mention} your turn'))\n",
            id="wrapper-keyword-layout",
        ),
        pytest.param(
            "async def f(ch, uid):\n    await ch.send(mention(uid))\n",
            id="mention-builder",
        ),
        pytest.param(
            "async def f(ch, uids):\n"
            "    parts = [mention(u) for u in uids]\n"
            "    await ch.send(content=' '.join(parts))\n",
            id="mention-builder-via-comprehension",
        ),
        pytest.param(
            "async def f(ch, roles):\n"
            "    ms = []\n"
            "    for r in roles:\n"
            "        ms.append(r.mention)\n"
            "    await ch.send(' '.join(ms))\n",
            id="appended-into-a-list",
        ),
        pytest.param(
            "async def f(ch, roles):\n"
            "    ms = []\n"
            "    ms.extend(f'<@&{r}>' for r in roles)\n"
            "    await ch.send(' '.join(ms))\n",
            id="extended-into-a-list",
        ),
        pytest.param(
            "async def f(self, gid, uid):\n"
            "    await self._announce_to_channel(gid, f'<@{uid}> your lobby closes')\n",
            id="base-game-announce-helper",
        ),
    ],
)
def test_scanner_flags(source):
    assert scan_source(source), "scanner missed an unguarded ping"


@pytest.mark.parametrize(
    "source",
    [
        pytest.param(
            "async def f(ch, m):\n"
            "    await ch.send(f'{m.mention} hi', allowed_mentions=allow_pings(users=[m]))\n",
            id="allow-listed",
        ),
        pytest.param(
            "async def f(ch, r):\n    await ch.send(**role_ping_kwargs([r]))\n",
            id="ping-kwargs-unpack",
        ),
        pytest.param(
            "async def f(ch, m, r):\n"
            "    am = allow_pings(roles=[r]) if r else allow_pings()\n"
            "    await ch.send(f'{m.mention} hi', allowed_mentions=am)\n",
            id="allow-list-concrete-on-every-branch",
        ),
        pytest.param(
            "async def f(i, m):\n"
            "    await i.response.send_message(f'{m.mention} hi', ephemeral=True)\n",
            id="ephemeral",
        ),
        pytest.param(
            "async def f(i, m):\n    await safe_ephemeral(i, f'{m.mention} hi')\n",
            id="ephemeral-helper",
        ),
        pytest.param(
            "async def f(ch, name):\n    await ch.send(f'{name} your turn')\n",
            id="resolved-name",
        ),
        pytest.param(
            "async def f(ch, m):\n    await ch.send(embed=Embed(description=m.mention))\n",
            id="embed-only",
        ),
        pytest.param(
            "async def f(ch, names):\n"
            "    parts = []\n"
            "    for n in names:\n"
            "        parts.append(n)\n"
            "    await ch.send(', '.join(parts))\n",
            id="a-list-of-resolved-names-is-not-tainted",
        ),
        pytest.param(
            "async def f(self, gid, uid):\n"
            "    await self._announce_to_channel(gid, f'<@{uid}> hi', "
            "allowed_mentions=allow_pings(users=[uid]))\n",
            id="announce-helper-allow-listed",
        ),
    ],
)
def test_scanner_passes(source):
    assert not scan_source(source)


# ── the tree ────────────────────────────────────────────────────────────


def test_no_send_pings_without_saying_so():
    found = _scan_tree()
    new = {k: v for k, v in found.items() if k not in SILENT_BY_DESIGN}
    assert not new, (
        "This send's content names somebody but passes no allowed_mentions= "
        "(or one a branch can leave as None, which resolves to the same "
        "suppressive client default), so nobody is notified. If it should "
        "notify, "
        "pass allowed_mentions=core.utils.allow_pings(users=[...], "
        "roles=[...]) naming exactly who the text names. If it cannot notify "
        "anyone at all, add a SILENT_BY_DESIGN row saying why.\n  "
        + "\n  ".join(
            f"{k}:{ln} {text}" for k, hits in sorted(new.items()) for ln, text in hits
        )
    )


def test_silent_by_design_ledger_is_not_stale():
    found = _scan_tree()
    stale = sorted(set(SILENT_BY_DESIGN) - set(found))
    assert not stale, (
        "SILENT_BY_DESIGN rows whose site no longer sends an unguarded "
        "mention — delete them so the ledger stays honest:\n  "
        + "\n  ".join(stale)
    )


@pytest.mark.parametrize("site", sorted(CROSS_FUNCTION_PINGS))
def test_cross_function_pings_keep_their_allow_list(site):
    """The sweep cannot reach these; this row can."""
    rel, qual = site.split("::")
    content_expr, what = CROSS_FUNCTION_PINGS[site]
    guarded = send_guard((SRC / rel).read_text(encoding="utf-8"), qual, content_expr)
    assert guarded is not None, (
        f"CROSS_FUNCTION_PINGS row {site} names no send with content "
        f"`{content_expr}` any more. If the ping moved, move the row; if it "
        "is gone, delete it — a row that matches nothing guards nothing."
    )
    assert guarded, (
        f"{site} sends {what} with no allow-list that survives every branch, "
        "so the bot's suppressive default silences it. The mention text is "
        "built in another function, so the tree sweep will never say so — "
        "pass allowed_mentions=core.utils.allow_pings(...) naming who it "
        "pings."
    )


# ── admin-authored templates: the blind spot, closed by name ────────────


@functools.cache
def _scan_template_tree() -> dict[str, list[tuple[int, str]]]:
    found: dict[str, list[tuple[int, str]]] = {}
    for path in sorted(SRC.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        if not any(name in source for name in TEMPLATE_RENDERERS):
            continue
        rel = path.relative_to(SRC).as_posix()
        for qual, lineno, text in scan_template_sends(source):
            found.setdefault(f"{rel}::{qual}", []).append((lineno, text))
    return found


@functools.cache
def _function_names_defined_in_src() -> set[str]:
    names: set[str] = set()
    for path in SRC.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                names.add(node.name)
    return names


@pytest.mark.parametrize(
    "source",
    [
        pytest.param(
            "async def f(ch, cfg, m):\n"
            "    await ch.send(render_arrival_message(cfg.arrival, m))\n",
            id="renderer-inline",
        ),
        pytest.param(
            "async def f(ch, cfg, m):\n"
            "    line = render_arrival_message(cfg.arrival, m)\n"
            "    if line:\n"
            "        await ch.send(line)\n",
            id="renderer-via-local",
        ),
        pytest.param(
            "async def f(ch, cfg, m):\n"
            "    line = render_arrival_message(cfg.arrival, m)\n"
            "    body = f'{line}!'\n"
            "    await ch.send(content=body)\n",
            id="renderer-two-hops-keyword",
        ),
        pytest.param(
            "async def f(ch, t, m, r, a, i):\n"
            "    await ch.send(_resolve_grant_message(t, m, r, a, i))\n",
            id="grant-announcement",
        ),
        pytest.param(
            "async def f(ch, cfg, m):\n"
            "    line = render_arrival_message(cfg.arrival, m)\n"
            "    await ch.send(line, allowed_mentions=None)\n",
            id="allow-list-is-literally-none",
        ),
    ],
)
def test_template_scanner_flags(source):
    assert scan_template_sends(source), "template sweep missed an unguarded send"


@pytest.mark.parametrize(
    "source",
    [
        pytest.param(
            "async def f(ch, cfg, m):\n"
            "    line = render_arrival_message(cfg.arrival, m)\n"
            "    await ch.send(line, "
            "allowed_mentions=template_pings(cfg.arrival, users=[m]))\n",
            id="template-pings",
        ),
        pytest.param(
            "async def f(ch, cfg, m):\n"
            "    line = render_arrival_message(cfg.arrival, m)\n"
            "    await ch.send(line, ephemeral=True)\n",
            id="ephemeral",
        ),
        pytest.param(
            "async def f(ch, cfg, m):\n"
            "    line = render_arrival_message(cfg.arrival, m)\n"
            "    await ch.send(embed=Embed(description=line))\n",
            id="embed-only",
        ),
        pytest.param(
            "async def f(ch, cfg, m):\n    await ch.send(cfg.arrival)\n",
            id="template-that-never-reached-a-renderer",
        ),
    ],
)
def test_template_scanner_passes(source):
    assert not scan_template_sends(source)


def test_no_templated_send_pings_without_saying_so():
    found = _scan_template_tree()
    assert not found, (
        "This send's content is a rendered admin-authored template, so its "
        "mentions are whatever an admin typed into the dashboard — and with "
        "the client default at none() they notify nobody unless the send "
        "says so. Pass allowed_mentions=core.utils.template_pings("
        "<the raw template>, users=[...], roles=[...]): the allow-list has "
        "to come from the template text, never from the rendered result, "
        "because a member whose display name is '@everyone' is spliced into "
        "the latter.\n  "
        + "\n  ".join(
            f"{k}:{ln} {text}" for k, hits in sorted(found.items()) for ln, text in hits
        )
    )


@pytest.mark.parametrize("name", sorted(TEMPLATE_RENDERERS))
def test_template_renderer_ledger_is_not_stale(name):
    """A renamed renderer must not leave a row guarding nothing."""
    assert name in _function_names_defined_in_src(), (
        f"TEMPLATE_RENDERERS names {name} ({TEMPLATE_RENDERERS[name]}) but "
        "src/ defines no such function. If it was renamed, rename the row; "
        "if the template is gone, delete it — a row matching nothing lets "
        "the next templated send ship unguarded."
    )
