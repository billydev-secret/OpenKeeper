"""Contract: an embed sent to a member's DMs is branded through ``dm_branding``.

A DM carries no guild of its own, so the embed is the only place the server
can be named (``shared/dm_branding.py``). ``send_branded_dm`` is the common
path; a caller that owns its delivery policy brands with ``brand_dm_embed``
and sends itself — either call in the sending function counts.

Static, so heuristic: a ``.send(...)`` passing ``embed=``/``embeds=`` or
``**kwargs``, on a receiver named like a person (``user``, ``member``,
``owner``, ``target``, …). Content-only DMs are exempt — branding applies only
to embeds.

``.edit(embed=...)`` counts too, on a receiver whose name carries ``dm`` as a
word (``self._dm_message``). A branded first send followed by unbranded edits
is a live shape, not a hypothetical: the erasure progress card in
``privacy_cog`` sent one branded DM and then edited a fresh, unstamped embed
onto it every couple of seconds, so the attribution existed for about as long
as it took the first tick to land. The narrow receiver rule is deliberate —
``message.edit(embed=...)`` in a guild channel is ordinary and must stay
invisible here.

An edit is held to a **stricter** rule than a send, and the asymmetry is the
whole reason the edit check is worth having. For a send, "this function calls
a brander" is enough, because the shared brander mutates in place and the
idiomatic site discards its return value. An edit cannot use that idiom: it
stamps a *fresh* embed onto a message that is already stamped, so branding the
caller's object would double the footer the moment the edit fails and the send
path re-brands the same object. The branded value therefore has to be a
distinct, bound object — which is exactly what makes it visible here. So the
argument to ``.edit(embed=...)`` must itself be a brander call, or a local
name assigned from one. Function-wide "a brander is mentioned somewhere" would
have passed on the bug above verbatim, since ``_update_dm`` brands its first
send in the same function it edits from.

**What this cannot see**, stated so nobody reads a green run as more than it
is. On the send side it is blind to an embed passed positionally
(``user.send(e)``); to a send through a callable parameter
(``send_fn(embed=...)``) or a bound method held as a value
(``retry_transient(user.send, ...)``); to ``**`` forwarding of anything but a
bare name; to a receiver the parser cannot name, such as a subscript or a
call result (``members[uid].send(...)``); and to any DM whose send and whose
branding live in *different* functions, since the check is "does this
function mention a brander at all". That last one is also too coarse in the
other direction: a function that brands one embed and sends a second,
unrelated one reads as compliant.

The edit side has three blind spots of its own, and one of them is occupied.

* **The receiver has to be named like a DM.** ``_DM_HANDLE`` is a naming
  convention, not an analysis, so a DM handle spelled anything else is
  invisible. ``dm/perms/cog.py`` holds the live instance:
  ``DmDenyReplyModal`` is handed the branded consent DM as a constructor
  argument, stores it as ``self._message``, and on submit replaces it with an
  unbranded denial card. Widening the regex would not reach it either — the
  binding and the edit are in different functions of different classes, which
  is the cross-function gap above wearing a different hat.
* **An edit through an interaction is a different shape entirely.**
  ``interaction.response.edit_message(embed=...)`` on a component attached to
  a DM card edits that DM, but the receiver is ``interaction.response`` and
  nothing in the expression mentions a DM at all. The same file does this in
  four more places.
* **The brander has to be bound by a plain assignment.** A walrus
  (``edit(embed=(branded := brand_dm_embed(...)))``), a conditional
  expression, or a tuple unpack all defeat ``_brander_bound_names`` and would
  be reported as offenders. None of those shapes exists in ``src`` today, and
  the walrus is the one a future author is most likely to reach for.

Born green against a baseline keyed by (file, function), shrink-only. The
baseline is empty and stays empty — but read that as "nothing is *excused*",
not "nothing is left": the ``dm_perms_cog`` sites above are real and
uncaught, recorded in the standards status doc rather than here, because a
baseline entry would claim this check had seen them.
"""

from __future__ import annotations

import ast
import re
from tests.paths import REPO_ROOT

SRC = REPO_ROOT / "src"

HELPER = "bot_modules/shared/dm_branding.py"
BRANDERS = {"brand_dm_embed", "send_branded_dm"}

#: A receiver holding a message we sent *because it is a DM*. Narrow on
#: purpose — see the module docstring.
_DM_HANDLE = re.compile(r"(^|_)dm(_|$)", re.IGNORECASE)

_PERSON = re.compile(
    r"(^|_)(user|member|target|owner|author|recipient|winner|loser|player|host|"
    r"requester|invoker|opener|asker|moderator|dm|dm_channel)$",
    re.IGNORECASE,
)

#: Unbranded embed DMs that exist today. Empty, and kept empty: the two
#: entries it was born with — the moderation-hold notice and the voice-room
#: knock — were pinned only because their files were mid-edit on the
#: embed-mention-sweep session (2026-09-14), and both were branded on
#: 2026-09-21. Shrink only, so a new exemption has to be argued for in a
#: review rather than appearing in a diff.
BASELINE: set[tuple[str, str]] = set()


def _receiver_name(node: ast.expr) -> str:
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    return ""


def _is_embed_dm(call: ast.Call) -> bool:
    if not (isinstance(call.func, ast.Attribute) and call.func.attr == "send"):
        return False
    if not _PERSON.search(_receiver_name(call.func.value)):
        return False
    return any(
        kw.arg in ("embed", "embeds") or (kw.arg is None and isinstance(kw.value, ast.Name))
        for kw in call.keywords
    )


def _is_brander_call(node: ast.expr) -> bool:
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
    return name in BRANDERS


def _brander_bound_names(fn: ast.AST) -> set[str]:
    """Local names assigned the result of a brander call in ``fn``."""
    bound: set[str] = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Assign) and _is_brander_call(node.value):
            bound.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif (
            isinstance(node, ast.AnnAssign)
            and node.value is not None
            and _is_brander_call(node.value)
            and isinstance(node.target, ast.Name)
        ):
            bound.add(node.target.id)
    return bound


def _unbranded_dm_edits(fn: ast.AST) -> list[int]:
    """Lines in ``fn`` editing an unbranded embed onto a DM we are holding.

    No ``**kwargs`` clause: an edit forwarding an opaque bag is not a shape
    this codebase has, and guessing would cost false positives on every
    guild-channel message edit.
    """
    bound = _brander_bound_names(fn)
    lines: list[int] = []
    for node in ast.walk(fn):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        if node.func.attr != "edit":
            continue
        if not _DM_HANDLE.search(_receiver_name(node.func.value)):
            continue
        for kw in node.keywords:
            if kw.arg not in ("embed", "embeds"):
                continue
            if _is_brander_call(kw.value):
                continue
            if isinstance(kw.value, ast.Name) and kw.value.id in bound:
                continue
            lines.append(node.lineno)
    return lines


def _calls(fn: ast.AST) -> set[str]:
    names = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Call):
            func = node.func
            names.add(func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", ""))
    return names


def _unbranded() -> dict[tuple[str, str], tuple[int, str]]:
    """Offending (file, function) -> (line, which rule fired).

    The rule name reaches the assertion message: "brand before your own send"
    is the wrong instruction to print at someone whose problem is an ``.edit``.
    """
    found: dict[tuple[str, str], tuple[int, str]] = {}
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC).as_posix()
        if rel == HELPER:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            direct = [
                n for n in ast.walk(fn)
                if isinstance(n, ast.Call) and _is_embed_dm(n)
            ]
            if direct and not (_calls(fn) & BRANDERS):
                found.setdefault((rel, fn.name), (direct[0].lineno, "send"))
            for line in _unbranded_dm_edits(fn):
                found.setdefault((rel, fn.name), (line, "edit"))
    return found


#: What to do about each rule, printed with the offender it belongs to.
_REMEDY = {
    "send": "send_branded_dm(), or brand_dm_embed() before your own send",
    "edit": (
        "pass .edit(embed=…) a branded embed — brand_dm_embed(embed.copy(), …), "
        "a copy because the brander mutates and appends to an existing footer"
    ),
}


def test_embed_dms_are_branded():
    offenders = sorted(
        f"src/{rel}:{line} in {name}() [{rule}] — {_REMEDY[rule]}"
        for (rel, name), (line, rule) in _unbranded().items()
        if (rel, name) not in BASELINE
    )
    assert not offenders, (
        "DM embeds go through services.dm_branding. Offenders:\n  "
        + "\n  ".join(offenders)
    )


def test_baseline_only_shrinks():
    stale = sorted(f"src/{rel} {name}()" for rel, name in BASELINE - _unbranded().keys())
    assert not stale, f"Now branded or gone — remove from BASELINE: {stale}"
