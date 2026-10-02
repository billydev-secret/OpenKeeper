"""The My Data panel contract: one member-facing erasure surface.

Two halves, and the bug they come from is the reason for both. Whisper shipped
its own ``/whisper forget-me``, whose deletes were wired to that command and to
nothing else — so an *operator* erasure reached no whisper at all, while the
member could clear the lot themselves with one slash command (bug #222,
item 3). A per-feature erasure command is how that happens: the feature
carries the knowledge of what to delete, and nothing else can reach it.

So:

1. **No cog defines an erase-my-data command.** Member-facing erasure is the
   My Data panel (the Server Features hub's 📜 row, or ``/info``), and a
   feature adds an
   :class:`~bot_modules.privacy.logic.ErasableStore` to get a button. Enforced
   repo-wide rather than per-directory, for the same reason the cog ``ctx``
   guards are: a convention with an unpinned exception is how the next one
   gets written.

2. **A register row marked member-erasable has a button.** The register is
   where a store's erasure decision is recorded, so a row promising the member
   can clear it themselves, with no button on the panel, is a promise nothing
   keeps. This half watches a *doc*, and docs map to no test by filename —
   ``scripts/gate.py`` selects this file by path for both halves.

**Deliberate non-members of the panel**, asserted below so a later cleanup
pass doesn't "fix" them: ``/birthday remove`` and ``/voice profile reset``
each delete a stored row, but they are also how the member turns that feature
off. Folding them into the My Data panel would take the off switch with them. The
rule is about a surface whose *only* job is erasure.
"""

from __future__ import annotations

import ast
import dataclasses
import re

import pytest

from bot_modules.privacy.logic import GUESS_STORE, MEMBER_ERASABLE_STORES, WHISPER_STORE
from tests.paths import REPO_ROOT

ROOT = REPO_ROOT
SRC = ROOT / "src"
REGISTER = ROOT / "docs" / "data_register.md"

#: A command the walk must always see. It used to be ``/mydata`` itself, the
#: one exemption; since 2026-09-26 the panel opens from a button, so no module
#: is exempt and the anchor is any command that is staying.
ANCHOR = ("src/bot_modules/advisor/cog.py", "ask")

#: A command *name* that says "erase my data" whatever its description claims.
_ERASE_NAME = re.compile(r"forget|erase|wipe|delete[-_]?me|my[-_]?data|purge[-_]?me")

#: A description that promises erasure of the caller's own records. Deliberately
#: phrase-based: "Remove your birthday so the bot stops announcing it" is a
#: feature off switch and must not match, while "Delete all your whisper data
#: from this server" must.
_ERASE_DESCRIPTION = re.compile(
    r"(delete|erase|remove|wipe)\s+(all\s+)?(your|my)\s+"
    r"(\w+\s+){0,2}(data|records|history|messages)",
    re.IGNORECASE,
)


def _command_decorators(tree: ast.AST):
    """Yield ``(name, description)`` for every slash command in *tree*.

    Covers both shapes in this repo: ``@app_commands.command(...)`` and a
    group's ``@<group>.command(...)``.
    """
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        for dec in node.decorator_list:
            if not isinstance(dec, ast.Call):
                continue
            target = ast.unparse(dec.func)
            if not target.endswith(".command"):
                continue
            kwargs = {
                kw.arg: kw.value
                for kw in dec.keywords
                if kw.arg in {"name", "description"}
            }
            name = kwargs.get("name")
            description = kwargs.get("description")
            yield (
                name.value if isinstance(name, ast.Constant) else node.name,
                description.value if isinstance(description, ast.Constant) else "",
            )


def _all_commands() -> list[tuple[str, str, str]]:
    """``(module, command name, description)`` for every slash command in src/."""
    found: list[tuple[str, str, str]] = []
    for path in sorted(SRC.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover - not this test's problem
            continue
        rel = path.relative_to(ROOT).as_posix()
        for name, description in _command_decorators(tree):
            found.append((rel, str(name), str(description)))
    return found


def test_the_walk_finds_the_panel_command():
    """Guard the guard: a walk that finds nothing would pass halves 1 forever."""
    names = {(mod, name) for mod, name, _ in _all_commands()}
    assert ANCHOR in names, (
        "the command walk can no longer see /ask, so it can no longer see "
        "an offender either — fix the walk before trusting this file"
    )


def test_no_cog_ships_its_own_erase_my_data_command():
    offenders = [
        f"{mod}::/{name} — {description!r}"
        for mod, name, description in _all_commands()
        if (_ERASE_NAME.search(name) or _ERASE_DESCRIPTION.search(description))
    ]
    assert not offenders, (
        "member-facing data erasure belongs on the My Data panel, not in a "
        "per-feature command — add an ErasableStore in "
        "bot_modules/privacy/logic.py and the panel grows a button, with the "
        "deletes in a service both it and purge_user_data can call:\n  "
        + "\n  ".join(offenders)
    )


@pytest.mark.parametrize(
    ("command", "description"),
    [
        pytest.param("remove", "Remove your birthday so the bot stops announcing it.", id="birthday-remove"),
        pytest.param("reset", "Reset your saved profile (or one specific field).", id="voice-profile-reset"),
    ],
)
def test_a_feature_off_switch_is_not_an_erasure_command(command, description):
    """The two standing exceptions, pinned so the pattern can't widen onto them.

    Both delete a stored row; both are also the member's off switch for that
    feature. If a future tightening of the pattern starts flagging them, that
    is the pattern being wrong, not these commands.
    """
    assert not _ERASE_NAME.search(command)
    assert not _ERASE_DESCRIPTION.search(description)


def _register_member_erasable_tables() -> set[str]:
    """Tables whose register row is marked ``**Member-erasable**``.

    The marker is documented at the top of ``docs/data_register.md``. The
    table names are the row's first cell, comma-separated and backticked or
    not — the register writes both ways.
    """
    tables: set[str] = set()
    for line in REGISTER.read_text(encoding="utf-8").splitlines():
        if not line.startswith("|") or "**Member-erasable**" not in line:
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        for name in cells[0].split(","):
            cleaned = name.strip().strip("`").strip()
            if cleaned:
                tables.add(cleaned)
    return tables


def test_every_member_erasable_register_row_has_a_panel_button():
    from_register = _register_member_erasable_tables()
    from_panel = {t for store in MEMBER_ERASABLE_STORES for t in store.register_tables}

    assert from_register, (
        "no register row is marked **Member-erasable** — either the marker was "
        "renamed (update this test and the register's legend together) or the "
        "whispers row lost it"
    )
    missing = from_register - from_panel
    assert not missing, (
        "the register says a member can clear these themselves, but no "
        "My Data button does it — the promise is unkept:\n  "
        + "\n  ".join(sorted(missing))
    )
    extra = from_panel - from_register
    assert not extra, (
        "the panel clears these tables but their register row isn't marked "
        "**Member-erasable**, so the record of processing disagrees with the "
        "surface:\n  " + "\n  ".join(sorted(extra))
    )


def test_every_store_has_an_eraser_wired():
    """The one wiring assertion: a button with no eraser is a dead button.

    Deliberately identity, not behaviour — that the panel runs *the same*
    function an operator erasure runs is the whole point of the refactor, and
    the deletes themselves are tested in ``test_whisper_forget_member.py``.

    The eraser rides on the store itself, so this can no longer catch a
    *missing* wiring (the dataclass field is required); what it still pins is
    that the wiring points at the shared engine rather than at some panel-only
    copy of the deletes.
    """
    from bot_modules.games.guess import repo as guess_repo
    from bot_modules.whisper import repo as whisper_repo

    assert all(callable(store.eraser) for store in MEMBER_ERASABLE_STORES)
    assert WHISPER_STORE.eraser is whisper_repo.forget_member
    assert GUESS_STORE.eraser is guess_repo.forget_member


def test_the_guess_confirm_reuses_the_notice_wording():
    """The privacy notice and the irreversible click describe one deletion.

    Both sentences come from ``disclosure_copy``; a confirm that paraphrased
    them would drift the first time either was edited."""
    from bot_modules.privacy.disclosure_copy import (
        GUESS_ROUNDS_ERASED,
        GUESS_SOLVER_SURVIVES,
    )

    assert GUESS_ROUNDS_ERASED[1:] in GUESS_STORE.confirm
    assert GUESS_SOLVER_SURVIVES in GUESS_STORE.confirm
    assert GUESS_STORE.clear_media is not None, "the photo and post must go too"


async def test_a_store_whose_media_will_not_clear_deletes_no_rows(monkeypatch):
    """The one cog wiring assertion for ``clear_media``: it runs first, and a
    failure there stops the eraser and tells the member nothing was deleted
    — never the store's "has been deleted" line."""
    from unittest.mock import AsyncMock, MagicMock

    from bot_modules.privacy import cog as privacy_cog

    erased: list = []
    monkeypatch.setattr(privacy_cog, "_erase_store", lambda *a, **k: erased.append(a))

    async def _refuse(*args, **kwargs):
        raise RuntimeError("post would not delete")

    store = dataclasses.replace(GUESS_STORE, clear_media=_refuse)
    panel = privacy_cog._MyDataPanelView(
        MagicMock(), actor_id=7, guild=MagicMock(id=1), accent=None
    )
    monkeypatch.setattr(panel, "_confirm", AsyncMock(return_value=True))
    interaction = MagicMock(user=MagicMock(id=7))
    interaction.edit_original_response = AsyncMock()

    await panel._store_callback(store)(interaction)

    assert erased == []
    content = interaction.edit_original_response.await_args.kwargs["content"]
    assert content == privacy_cog._STORE_FAILED
    assert GUESS_STORE.done not in content


async def test_a_store_whose_eraser_fails_after_media_cleared_admits_it(monkeypatch):
    """The media step (the post, the photo) can succeed and then the row
    delete can still raise — a locked database, say. That is not "nothing was
    deleted": the post and photo are already gone for good, so the member must
    not be told the plain ``_STORE_FAILED`` line."""
    from unittest.mock import AsyncMock, MagicMock

    from bot_modules.privacy import cog as privacy_cog

    cleared: list = []

    async def _clear(*args, **kwargs):
        cleared.append(True)

    def _raise(*a, **k):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(privacy_cog, "_erase_store", _raise)
    store = dataclasses.replace(GUESS_STORE, clear_media=_clear)
    panel = privacy_cog._MyDataPanelView(
        MagicMock(), actor_id=7, guild=MagicMock(id=1), accent=None
    )
    monkeypatch.setattr(panel, "_confirm", AsyncMock(return_value=True))
    interaction = MagicMock(user=MagicMock(id=7))
    interaction.edit_original_response = AsyncMock()

    await panel._store_callback(store)(interaction)

    assert cleared == [True]
    content = interaction.edit_original_response.await_args.kwargs["content"]
    assert content == privacy_cog._STORE_ROWS_FAILED
    assert content != privacy_cog._STORE_FAILED
    assert GUESS_STORE.done not in content


@pytest.mark.parametrize("confirmed", [True, False], ids=["confirmed", "cancelled"])
async def test_delete_everything_runs_only_after_both_confirms_and_holds_the_claim(
    monkeypatch, confirmed
):
    """The Delete everything button's glue: it goes through the same two-step
    ``_confirm`` as every other button, runs the cog's erasure only when both
    are pressed, and holds ``_active_deletions`` for the whole run (the message
    sweep inside it, and the departure worker, both read that claim)."""
    from unittest.mock import AsyncMock, MagicMock

    from bot_modules.privacy import cog as privacy_cog
    from bot_modules.privacy.logic import EVERYTHING_LABEL, render_everything_confirm

    cog = MagicMock()
    cog._active_deletions = set()
    claimed: list[bool] = []

    async def _erase(interaction, guild, *, accent):
        claimed.append(7 in cog._active_deletions)

    cog.erase_everything = _erase
    panel = privacy_cog._MyDataPanelView(cog, actor_id=7, guild=MagicMock(id=1), accent=None)
    assert EVERYTHING_LABEL in [getattr(c, "label", None) for c in panel.children]
    confirm = AsyncMock(return_value=confirmed)
    monkeypatch.setattr(panel, "_confirm", confirm)

    await panel._everything_callback()(MagicMock(user=MagicMock(id=7)))

    assert confirm.await_args.kwargs["prompt"] == render_everything_confirm()
    assert claimed == ([True] if confirmed else [])
    assert cog._active_deletions == set()
