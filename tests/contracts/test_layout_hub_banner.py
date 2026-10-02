"""The hub banner and footer go on channel-resident hub panels only, the
lobby header (with the footer under it) on public game lobbies only, and the
thin card strips on the other public cards named in ``CARD_ART_BUILDERS``.

Billy, 2026-09-26, on the poppy banner: "let's not use it on the ephemeral
messages, just the main panels that live in the channel". A hub is the poster
a channel keeps at its foot, so the art brands the place; an ephemeral card is
one member's working surface and stays plain — the style guide's one
exception to § Media's "never decoration".

``core.layout.hub_banner`` and ``hub_footer`` are the only way the art
becomes a component, so the rule is enforced on their **callers**: every call
must sit inside a function named in ``HUB_BANNER_BUILDERS``, and each row
there is a channel panel. A new
V2 hub adds its builder here in the commit that converts it; an ephemeral
card that wants the art is a design question for Billy, not a row.

Static and syntactic, like ``test_layout_no_container``: it matches
``hub_banner(...)`` and ``<module>.hub_banner(...)`` by name, so an alias
(``from ... import hub_banner as hb``) would slip past — not a shape this repo
writes, and the review at each stage is the backstop.
"""

from __future__ import annotations

import ast

import discord
import pytest

from bot_modules.core.branding_service import PanelArt
from bot_modules.core.layout import (
    CARD_FOOTER_ALT,
    CARD_HEADER_ALT,
    HUB_BANNER_ALT,
    HUB_FOOTER_ALT,
    LOBBY_HEADER_ALT,
    card_strips,
    hub_banner,
    hub_footer,
    lobby_art,
)
from tests.paths import REPO_ROOT

SRC = REPO_ROOT / "src"

#: module (relative to src/) → the hub-panel builders in it that may draw the
#: banner or footer. Every one of these renders a sticky channel panel.
HUB_BANNER_BUILDERS: dict[str, set[str]] = {
    # Both break cards live in a channel: the public one wherever an admin
    # posts it, the "I'm back" one in the break channel itself.
    "bot_modules/breaks/layouts.py": {
        "build_break_panel_layout",
        "build_return_panel_layout",
    },
    "bot_modules/casino/cog/hub_layout.py": {"build_casino_hub_layout"},
    "bot_modules/dm/perms/layouts.py": {"build_panel_layout"},
    "bot_modules/economy/bank_panel_layout.py": {"build_economy_panel_layout"},
    "bot_modules/economy/shop_layouts.py": {"build_shop_panel_layout"},
    # The Pen Pals signup panel (Ben, 2026-09-30: "use the big headers on
    # the panel").
    "bot_modules/pen_pals/layouts.py": {"build_panel_layout"},
    "bot_modules/server_features/layouts.py": {"build_hub_layout"},
}

#: Builders allowed to draw the hub banner/footer that are **not** hub
#: panels — each is a narrow, documented exception to "channel-resident hub
#: panels only", not a second door next to ``HUB_BANNER_BUILDERS``. Excluded
#: from :func:`test_every_hub_can_carry_its_title_in_the_banner` below: that
#: check is about a genuine hub's own name, and these builders' banners carry
#: something else.
HUB_BANNER_EXCEPTIONS: dict[str, set[str]] = {
    # Flash Themes' live announcement, which is also the message that gets
    # pinned — a public post that lives in the theme channel for its whole
    # window, not an ephemeral card (Ben, 2026-09-28, "option B"). It opens
    # on the tall hub banner with "Today's Theme" drawn in, unconditionally
    # (not the guild's title_in_banner toggle — that governs a hub swapping
    # its own name into the banner, and this caption is a fixed decoration
    # sitting above the card's own `## 🎨 {title}` heading, never a
    # substitute for it), and closes on the hub footer.
    "bot_modules/economy/theme_layouts.py": {"build_theme_live_layout"},
}


def _merged(*dicts: dict[str, set[str]]) -> dict[str, set[str]]:
    merged: dict[str, set[str]] = {}
    for d in dicts:
        for rel, fns in d.items():
            merged[rel] = merged.get(rel, set()) | fns
    return merged

#: module → the game-lobby builders that may draw the lobby header and footer
#: (``lobby_art``; Billy, 2026-09-27). A lobby is a public card in a games
#: channel, so it takes the hubs' exception — with its own small strip, not
#: the hub banner. A game adds its lobby here when its lobby turns V2.
LOBBY_ART_BUILDERS: dict[str, set[str]] = {
    "bot_modules/games/clapback/layouts.py": {"build_lobby_layout"},
    "bot_modules/duels/layouts.py": {"build_lobby_layout", "card_art"},
    "bot_modules/games/compliment/layouts.py": {"build_lobby_layout"},
    "bot_modules/games/mfk/layouts.py": {"build_lobby_layout"},
    "bot_modules/games/rushmore/layouts.py": {"build_lobby_layout"},
    "bot_modules/games/mlt/layouts.py": {"build_lobby_layout"},
    "bot_modules/games/legitlibs/layouts.py": {"build_lobby_layout"},
    "bot_modules/games/price/layouts.py": {"build_lobby_layout"},
    "bot_modules/games/hottakes/layouts.py": {"build_lobby_layout"},
    "bot_modules/games/fantasies/layouts.py": {"build_lobby_layout"},
    "bot_modules/games/ttl/layouts.py": {"build_lobby_layout"},
    "bot_modules/games/story/layouts.py": {"build_lobby_layout"},
    # Truth or Dare's room board is its lobby: one card from launch to End
    # Game. It wore the tall hub banner for a day, then took the small strip
    # like every other lobby (Ben, 2026-09-29: "switch truth or dare to the
    # small header").
    "bot_modules/games/traditional/layouts.py": {"build_room_layout"},
    # Guess Who's channel prompt had drawn no art before its conversion
    # (2026-09-30), so it takes the small strip under the same general rule
    # as the Bounty Board below.
    "bot_modules/games/guess/layouts.py": {
        "build_prompt_layout",
        # The public cards wear it too (Ben, 2026-09-30: "we're missing
        # headers on a bunch of messages"): the round, its reveal and the
        # leaderboard. The ephemeral cards stay plain.
        "build_round_layout", "build_solved_layout", "build_leaderboard_layout",
    },
    # Risky has no lobby: its round board is the public card players join.
    "bot_modules/games/risky_roll/layouts.py": {"build_round_layout"},
    # Not a game: the intake card in greeter chat wears the strip with the
    # member's name drawn in (Ben, 2026-09-28). A staff noticeboard card,
    # channel-resident like a lobby, never ephemeral.
    "bot_modules/onboarding/intake/views.py": {"build_intake_layout"},
    # The Bounty Board hub had drawn no art of its own at its Components V2
    # conversion (2026-09-28) — Ben's general rule: a channel hub with no
    # tall banner yet wears the small lobby strip instead, not a second door
    # next to HUB_BANNER_BUILDERS. Exempt from
    # test_every_hub_can_carry_its_title_in_the_banner below for the same
    # reason a lobby is: that check is about the tall banner's own toggle.
    "bot_modules/economy/bounty_layouts.py": {"build_bounty_hub_layout"},
    # Pen Pals' question cards in a pair's room (Ben, 2026-09-30: "do the
    # small banners for new questions in the channel") — public posts in a
    # channel, not an ephemeral card.
    "bot_modules/pen_pals/layouts.py": {"build_question_layout"},
    # The Whisper launcher, converted 2026-09-30: the same rule as the Bounty
    # Board — no art of its own before, so the small strip with "Whisper"
    # drawn between the leaves.
    "bot_modules/whisper/layouts.py": {"build_launcher_layout"},
}

#: module → the public cards that may draw the thin, untitled card strips
#: (``card_strips``; Ben, 2026-09-30: "a third thinner header with no title for
#: use in the embeds"). Each is a card posted in a channel that is neither a
#: hub nor a lobby — the economy and casino posts, the staff-channel review
#: and audit cards. A builder that is also sent
#: privately is listed only if its private path passes ``art=None``.
CARD_ART_BUILDERS: dict[str, set[str]] = {
    # Economy posts.
    "bot_modules/economy/auction_layouts.py": {"build_auction_card_layout"},
    "bot_modules/economy/bounty_layouts.py": {"build_bounty_card_layout"},
    "bot_modules/economy/drops_layouts.py": {
        "build_drop_layout",
        "build_claimed_layout",
        "build_expired_layout",
    },
    "bot_modules/economy/layouts.py": {"build_grant_receipt_layout"},
    "bot_modules/economy/pin_layouts.py": {"build_pin_live_layout"},
    # Also the ephemeral receipt: the builder ignores art unless public.
    "bot_modules/economy/transfers.py": {"build_payment_receipt_layout"},
    # Casino posts. The Pools panel takes its bet row as ``controls`` so the
    # footer lands below it.
    "bot_modules/casino/cog/layouts.py": {
        "build_big_win_broadcast_layout",
        "build_jackpot_celebration_layout",
        "build_pools_panel_layout",
        "build_pools_result_layout",
        "build_pools_void_layout",
    },
    # Staff-channel cards. The review assembler also draws the todo board's
    # ephemeral detail and the quest sign-off picker, which pass no art
    # (``economy.view_helpers.surface_art``).
    "bot_modules/economy/approval_layouts.py": {"build_review_card_layout"},
    "bot_modules/dm/perms/layouts.py": {"build_audit_notice_layout"},
}

_DEFINED_IN = "bot_modules/core/layout.py"
_HUB_HELPERS = frozenset({"hub_banner", "hub_footer"})
_LOBBY_HELPERS = frozenset({"lobby_art"})
_CARD_HELPERS = frozenset({"card_strips"})


def scan_source(source: str, helpers: frozenset[str] = _HUB_HELPERS) -> set[tuple[str, int]]:
    """``(enclosing function, line)`` for every call to one of ``helpers``."""
    tree = ast.parse(source)
    hits: set[tuple[str, int]] = set()

    def visit(node: ast.AST, fn: str) -> None:
        for child in ast.iter_child_nodes(node):
            inner = fn
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                inner = child.name
            if isinstance(child, ast.Call):
                f = child.func
                name = f.id if isinstance(f, ast.Name) else getattr(f, "attr", None)
                if name in helpers:
                    hits.add((fn, child.lineno))
            visit(child, inner)

    visit(tree, "<module>")
    return hits


def _scan_tree(helpers: frozenset[str] = _HUB_HELPERS) -> dict[str, set[tuple[str, int]]]:
    found: dict[str, set[tuple[str, int]]] = {}
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC).as_posix()
        source = path.read_text(encoding="utf-8")
        if rel == _DEFINED_IN or not any(h in source for h in helpers):
            continue
        hits = scan_source(source, helpers)
        if hits:
            found[rel] = hits
    return found


# ── the helper ──────────────────────────────────────────────────────────


HELPERS = [
    pytest.param(hub_banner, HUB_BANNER_ALT, id="banner"),
    pytest.param(hub_footer, HUB_FOOTER_ALT, id="footer"),
]


@pytest.mark.parametrize("helper, alt", HELPERS)
@pytest.mark.parametrize("url", [None, ""])
def test_no_art_adds_nothing(helper, alt, url):
    """Ships dark: an unconfigured guild's panel is exactly what it was."""
    assert helper(url) == []


@pytest.mark.parametrize("helper, alt", HELPERS)
def test_art_is_one_gallery_with_alt_text(helper, alt):
    url = "https://dash.example/static/panel-banners/" + "d" * 32 + ".webp"
    (gallery,) = helper(url)
    assert isinstance(gallery, discord.ui.MediaGallery)
    (item,) = gallery.items
    assert item.media.url == url
    assert item.description == alt


HEADER = "https://dash.example/static/panel-banners/" + "e" * 32 + ".webp"
FOOTER = "https://dash.example/static/panel-banners/" + "f" * 32 + ".webp"


@pytest.mark.parametrize(
    "header, footer",
    [
        pytest.param(None, None, id="none"),
        # The hub footer alone must not reach the lobbies: prod has one.
        pytest.param(None, FOOTER, id="footer-alone"),
        pytest.param("", FOOTER, id="blank-header"),
    ],
)
def test_lobby_art_needs_a_header(header, footer):
    assert lobby_art(header, footer) == ([], [])


def _art(items) -> list[tuple[str, str | None]]:
    return [
        (g.items[0].media.url, g.items[0].description)
        for g in items
        if isinstance(g, discord.ui.MediaGallery)
    ]


def test_lobby_art_header_tops_and_footer_closes():
    top, bottom = lobby_art(HEADER, FOOTER)
    assert _art(top) == [(HEADER, LOBBY_HEADER_ALT)] and len(top) == 1
    assert _art(bottom) == [(FOOTER, HUB_FOOTER_ALT)] and len(bottom) == 1


def test_a_titled_lobby_header_carries_the_title_as_alt_text():
    top, _ = lobby_art(HEADER, FOOTER, title="Clapback")
    assert _art(top) == [(HEADER, "Clapback")]


def test_lobby_header_without_a_footer():
    top, bottom = lobby_art(HEADER, None)
    assert len(top) == 1 and bottom == []


CARD_HEADER = "https://dash.example/static/panel-banners/" + "c" * 32 + ".webp"
CARD_FOOTER = "https://dash.example/static/panel-banners/" + "d" * 32 + ".webp"


@pytest.mark.parametrize(
    "art",
    [
        pytest.param(None, id="no-art-read"),
        pytest.param(PanelArt(None, None), id="none"),
        # The card footer alone must not reach the cards, as the lobbies'.
        pytest.param(PanelArt(None, None, card_footer=CARD_FOOTER), id="footer-alone"),
        # The hub and lobby art are not the card strips.
        pytest.param(PanelArt(HEADER, FOOTER, header=HEADER), id="other-slots-only"),
    ],
)
def test_card_strips_need_a_card_header(art):
    assert card_strips(art) == ([], [])


def test_card_strips_header_tops_and_footer_closes():
    top, bottom = card_strips(
        PanelArt(None, None, card_header=CARD_HEADER, card_footer=CARD_FOOTER)
    )
    assert _art(top) == [(CARD_HEADER, CARD_HEADER_ALT)] and len(top) == 1
    assert _art(bottom) == [(CARD_FOOTER, CARD_FOOTER_ALT)] and len(bottom) == 1


def test_card_header_without_a_footer():
    top, bottom = card_strips(PanelArt(None, None, card_header=CARD_HEADER))
    assert len(top) == 1 and bottom == []


# ── the sweep can see a violation ───────────────────────────────────────


@pytest.mark.parametrize(
    "source, expected",
    [
        pytest.param(
            "def build_x():\n    return [*hub_banner(u)]\n",
            {("build_x", 2)},
            id="bare-name",
        ),
        pytest.param(
            "def build_y():\n    return layout.hub_banner(u)\n",
            {("build_y", 2)},
            id="module-attr",
        ),
        pytest.param(
            "class V:\n    def __init__(self):\n        hub_banner(u)\n",
            {("__init__", 3)},
            id="method",
        ),
        pytest.param(
            "def build_z():\n    return [*hub_footer(u)]\n",
            {("build_z", 2)},
            id="footer",
        ),
        pytest.param("def f():\n    return banner(u)\n", set(), id="other-name"),
        pytest.param("def f():\n    return lobby_art(h, f)\n", set(), id="lobby-not-hub"),
    ],
)
def test_scanner(source, expected):
    assert scan_source(source) == expected


def test_scanner_sees_the_lobby_helper():
    src = "def build_lobby():\n    top, bottom = lobby_art(h, f)\n"
    assert scan_source(src, _LOBBY_HELPERS) == {("build_lobby", 2)}


def test_scanner_sees_the_card_helper():
    src = "def build_card(art=None):\n    top, bottom = layout.card_strips(art)\n"
    assert scan_source(src, _CARD_HELPERS) == {("build_card", 2)}


# ── the tree ────────────────────────────────────────────────────────────


def _stray(helpers: frozenset[str], allowed: dict[str, set[str]]) -> list[str]:
    return sorted(
        f"src/{rel}:{line} in {fn}()"
        for rel, hits in _scan_tree(helpers).items()
        for fn, line in hits
        if fn not in allowed.get(rel, set())
    )


def _stale(helpers: frozenset[str], allowed: dict[str, set[str]]) -> list[str]:
    found = _scan_tree(helpers)
    return sorted(
        f"{rel}::{fn}"
        for rel, fns in allowed.items()
        for fn in fns
        if fn not in {f for f, _ in found.get(rel, set())}
    )


def test_only_channel_hub_builders_draw_the_banner():
    stray = _stray(_HUB_HELPERS, _merged(HUB_BANNER_BUILDERS, HUB_BANNER_EXCEPTIONS))
    assert not stray, (
        "hub_banner()/hub_footer() are for channel-resident hub panels only (Billy, "
        "2026-09-26: 'not on the ephemeral messages'), plus the narrow, documented "
        "exceptions in HUB_BANNER_EXCEPTIONS. A new V2 hub adds its builder to "
        "HUB_BANNER_BUILDERS; an ephemeral card never calls it.\n  "
        + "\n  ".join(stray)
    )


def test_only_game_lobbies_draw_the_lobby_art():
    stray = _stray(_LOBBY_HELPERS, LOBBY_ART_BUILDERS)
    assert not stray, (
        "lobby_art() is for public game-lobby cards only (Billy, 2026-09-27). "
        "A game adds its V2 lobby builder to LOBBY_ART_BUILDERS.\n  "
        + "\n  ".join(stray)
    )


def test_only_listed_public_cards_draw_the_card_strips():
    stray = _stray(_CARD_HELPERS, CARD_ART_BUILDERS)
    assert not stray, (
        "card_strips() is for public cards posted in a channel (Ben, 2026-09-30), "
        "never an ephemeral one. A public card adds its builder to "
        "CARD_ART_BUILDERS.\n  " + "\n  ".join(stray)
    )


@pytest.mark.parametrize(
    "helpers, allowed",
    [
        pytest.param(
            _HUB_HELPERS,
            _merged(HUB_BANNER_BUILDERS, HUB_BANNER_EXCEPTIONS),
            id="hubs",
        ),
        pytest.param(_LOBBY_HELPERS, LOBBY_ART_BUILDERS, id="lobbies"),
        pytest.param(_CARD_HELPERS, CARD_ART_BUILDERS, id="cards"),
    ],
)
def test_builder_list_is_not_stale(helpers, allowed):
    stale = _stale(helpers, allowed)
    assert not stale, (
        "builder rows that no longer draw the art — delete them:\n  "
        + "\n  ".join(stale)
    )


# ── every hub can carry its title in the banner ─────────────────────────


def title_faults(source: str, fn_name: str) -> list[str]:
    """What stops ``fn_name`` honouring the Branding **Title in the Banner**
    switch: no ``title_in_banner`` parameter, or no ``hub_banner(...,
    title=...)`` call to hand the drawn title its alt text."""
    fn = next(
        n
        for n in ast.walk(ast.parse(source))
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == fn_name
    )
    params = {a.arg for a in (*fn.args.args, *fn.args.kwonlyargs)}
    faults = []
    if "title_in_banner" not in params:
        faults.append("takes no title_in_banner")
    titled = any(
        isinstance(c, ast.Call)
        and (
            c.func.id if isinstance(c.func, ast.Name) else getattr(c.func, "attr", None)
        )
        == "hub_banner"
        and any(k.arg == "title" for k in c.keywords)
        for c in ast.walk(fn)
    )
    if not titled:
        faults.append("never calls hub_banner(..., title=...)")
    return faults


@pytest.mark.parametrize(
    "source, faults",
    [
        pytest.param(
            "def b(*, banner_url=None, title_in_banner=False):\n"
            "    return hub_banner(banner_url, title='T') if title_in_banner else hub_banner(banner_url)\n",
            [],
            id="honours-it",
        ),
        pytest.param(
            "def b(*, banner_url=None):\n    return hub_banner(banner_url)\n",
            ["takes no title_in_banner", "never calls hub_banner(..., title=...)"],
            id="ignores-it",
        ),
        pytest.param(
            "def b(*, banner_url=None, title_in_banner=False):\n    return hub_banner(banner_url)\n",
            ["never calls hub_banner(..., title=...)"],
            id="flag-but-no-alt",
        ),
    ],
)
def test_title_check_can_see_a_violation(source, faults):
    assert title_faults(source, "b") == faults


def test_every_hub_can_carry_its_title_in_the_banner():
    """One upload serves every hub, and the switch is per guild, not per hub:
    once an admin ticks it, ``with_title`` swaps the titled banner into every
    hub that reads the art. A hub that doesn't take ``title_in_banner`` keeps
    its ``##`` line under a banner already wearing the name — the title twice
    (style guide § The paradigms, the hub checklist)."""
    faults = sorted(
        f"src/{rel}::{fn}: {fault}"
        for rel, fns in HUB_BANNER_BUILDERS.items()
        for fn in fns
        for fault in title_faults((SRC / rel).read_text(encoding="utf-8"), fn)
    )
    assert not faults, "\n  ".join(
        ["hub builders that can't carry the title:", *faults]
    )
