"""``games/utils/host_panel.py``: the one table of games that keep their
host-only controls on an ephemeral panel, and Start Here's hand-off.

``/games host``'s own dispatch is covered in
``tests/games/test_games_config_cog.py``; what is pinned here is the lookup
both doors share, and that a game started from ``/games help`` hands the
presser its host panel (ship review, 2026-09-29: a host who started Truth
or Dare from Help had no Ask Question or End Game in reach).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

import bot_modules.games.help.views as help_views
from bot_modules.games.utils.host_panel import HOST_PANEL_COGS, host_panel_opener
from bot_modules.games.help.views import hand_over_host_panel


def _bot(cogs: dict, games_db=None):
    return SimpleNamespace(get_cog=lambda name: cogs.get(name), games_db=games_db)


def test_the_games_with_a_host_panel():
    """Truth or Dare first (2026-09-29), then each party game as it converts
    to Components V2 and its host-only controls leave the board."""
    assert HOST_PANEL_COGS == {
        "traditional": "TraditionalCog",
        "rushmore": "RushmoreCog",
        "wyr": "WYRCog",
        "nhie": "NHIECog",
        "mlt": "MLTCog",
        "legitlibs": "LegitLibsCog",
        "ama": "AMACog",
        "price": "PriceCog",
        "hottakes": "HotTakesCog",
        "fantasies": "FantasiesCog",
        "ttl": "TTLCog",
        "ffa": "FFACog",
    }


def test_opener_is_the_loaded_cogs_open_host_panel():
    opener = AsyncMock()
    bot = _bot({"TraditionalCog": SimpleNamespace(open_host_panel=opener)})
    assert host_panel_opener(bot, "traditional") is opener


@pytest.mark.parametrize(
    "bot, game_type",
    [
        pytest.param(_bot({"ClapbackCog": SimpleNamespace()}), "clapback", id="game-without-a-panel"),
        pytest.param(_bot({}), "traditional", id="cog-not-loaded"),
        pytest.param(_bot({"TraditionalCog": SimpleNamespace()}), "traditional", id="cog-without-the-method"),
        pytest.param(SimpleNamespace(), "traditional", id="client-without-get-cog"),
    ],
)
def test_no_opener_without_a_panel_to_open(bot, game_type):
    assert host_panel_opener(bot, game_type) is None


def _interaction(bot):
    return SimpleNamespace(client=bot, channel_id=100)


async def test_start_here_hands_the_host_the_panel(monkeypatch):
    opener = AsyncMock()
    bot = _bot({"TraditionalCog": SimpleNamespace(open_host_panel=opener)}, games_db=object())
    monkeypatch.setattr(help_views, "get_active_game", AsyncMock(return_value={"game_id": "g9"}))
    interaction = _interaction(bot)

    await hand_over_host_panel(interaction, "traditional")  # type: ignore[arg-type]

    opener.assert_awaited_once_with(interaction, "g9")


async def test_start_here_sends_nothing_for_a_game_without_a_panel(monkeypatch):
    lookup = AsyncMock()
    monkeypatch.setattr(help_views, "get_active_game", lookup)

    await hand_over_host_panel(_interaction(_bot({}, games_db=object())), "clapback")  # type: ignore[arg-type]

    lookup.assert_not_awaited()


async def test_a_failed_hand_off_never_raises_into_the_started_game(monkeypatch):
    """The game is already running; /games host still works."""
    opener = AsyncMock(side_effect=discord.HTTPException(SimpleNamespace(status=500, reason="x"), "boom"))
    bot = _bot({"TraditionalCog": SimpleNamespace(open_host_panel=opener)}, games_db=object())
    monkeypatch.setattr(help_views, "get_active_game", AsyncMock(return_value={"game_id": "g9"}))

    await hand_over_host_panel(_interaction(bot), "traditional")  # type: ignore[arg-type]


@pytest.mark.parametrize("pressed_after", [0.0, 600.0, 830.0])
async def test_a_press_never_pushes_the_panel_past_its_original_window(monkeypatch, pressed_after):
    """discord.py restarts a view's timeout on every press, but the panel's
    grey-out rides the sending interaction's 15-minute token. A press must
    shrink the timeout to what is left of the original window, never renew
    it (ship review, 2026-10-01)."""
    import types

    from bot_modules.games.utils import host_panel as hp

    clock = types.SimpleNamespace(now=1000.0)
    monkeypatch.setattr(hp.time, "monotonic", lambda: clock.now)
    panel = hp.HostPanelView()
    assert panel.timeout == hp.HOST_PANEL_TIMEOUT

    clock.now += pressed_after
    assert await panel.interaction_check(types.SimpleNamespace(user=None)) is True
    remaining = hp.HOST_PANEL_TIMEOUT - pressed_after
    assert panel.timeout == max(1.0, remaining)
    # The expiry discord.py will set on this press is now + timeout: never
    # later than the original send + HOST_PANEL_TIMEOUT.
    assert clock.now + panel.timeout <= 1000.0 + hp.HOST_PANEL_TIMEOUT + 1.0


# ── /games host: every game in the channel is offered the press ──────────────

def _press():
    """An interaction whose response flips to done once something is sent."""
    from types import SimpleNamespace

    state = {"done": False}
    sent: list = []

    async def send_message(content=None, **kw):
        state["done"] = True
        sent.append(("reply", content, kw))

    async def followup_send(content=None, **kw):
        sent.append(("followup", content, kw))

    response = SimpleNamespace(send_message=send_message, is_done=lambda: state["done"])
    return SimpleNamespace(response=response, followup=SimpleNamespace(send=followup_send)), sent


def _opener(kind, text=""):
    """A host-panel opener that refuses via the real ``reply`` or opens a
    panel by answering the interaction, as the cogs do."""
    from bot_modules.games.utils import host_panel as hp

    async def opener(interaction, *_):
        if kind == "refuse":
            await hp.reply(interaction, text)
            return True
        if kind == "open":
            if interaction.response.is_done():
                await interaction.followup.send(view=text)
            else:
                await interaction.response.send_message(view=text)
            return True
        return False  # not this cog's channel

    return opener


@pytest.mark.parametrize(
    "shared, mahjong, risky, expect",
    [
        pytest.param("refuse", "none", "open", [("reply", None, "risky")], id="tod-stranger-is-risky-opener"),
        pytest.param("none", "refuse", "open", [("reply", None, "risky")], id="mahjong-stranger-is-risky-opener"),
        pytest.param("open", "none", "open", [("reply", None, "shared"), ("followup", None, "risky")], id="host-of-both-gets-both"),
        pytest.param("refuse", "refuse", "none", [("reply", "shared-no", None)], id="nothing-opens-shared-refusal-first"),
        pytest.param("none", "refuse", "none", [("reply", "mahjong-no", None)], id="only-a-channel-refusal"),
    ],
)
async def test_games_host_opens_every_panel_the_presser_may_open(shared, mahjong, risky, expect):
    """A channel holding a shared-row game and a Risky round never reached
    the round, so Close Round — on the panel and nowhere else — was
    unreachable (ship review, 2026-10-01)."""
    from types import SimpleNamespace

    from bot_modules.games.utils import host_panel as hp

    cogs = {
        "TraditionalCog": SimpleNamespace(open_host_panel=_opener(shared, "shared" if shared == "open" else "shared-no")),
        "MahjongCog": SimpleNamespace(open_channel_host_panel=_opener(mahjong, "mahjong-no")),
        "RiskyRollCog": SimpleNamespace(open_channel_host_panel=_opener(risky, "risky")),
    }
    bot = SimpleNamespace(get_cog=cogs.get)
    row = None if shared == "none" else {"game_type": "traditional", "game_id": "g1"}
    interaction, sent = _press()

    await hp.open_host_panels(bot, interaction, row, no_game="no-game", no_panel="no-panel")

    assert [(where, content, kw.get("view")) for where, content, kw in sent] == expect


async def test_games_host_with_nothing_in_the_channel_says_so():
    from types import SimpleNamespace

    from bot_modules.games.utils import host_panel as hp

    interaction, sent = _press()
    await hp.open_host_panels(SimpleNamespace(get_cog=lambda _n: None), interaction, None, no_game="no-game", no_panel="no-panel")
    assert [(w, c) for w, c, _ in sent] == [("reply", "no-game")]


async def test_a_held_refusal_outside_games_host_still_sends():
    """Only /games host holds refusals; Start Here and every panel press
    still answer at once."""
    from bot_modules.games.utils import host_panel as hp

    interaction, sent = _press()
    await hp.reply(interaction, "plain")
    assert [(w, c) for w, c, _ in sent] == [("reply", "plain")]


async def test_a_panel_sent_late_counts_from_when_the_press_was_made(monkeypatch):
    """A follow-up panel after a slow launch edits through a token whose 15
    minutes began at the press, not at the panel's build."""
    import datetime as dt
    import types

    from bot_modules.games.utils import host_panel as hp

    clock = types.SimpleNamespace(mono=5000.0, wall=1_800_000_000.0)
    monkeypatch.setattr(hp.time, "monotonic", lambda: clock.mono)
    monkeypatch.setattr(hp.time, "time", lambda: clock.wall)
    panel = hp.HostPanelView()
    pressed = dt.datetime.fromtimestamp(clock.wall - 90, tz=dt.timezone.utc)  # 90 s ago
    panel.start_clock_at(pressed)
    assert panel.timeout == pytest.approx(hp.HOST_PANEL_TIMEOUT - 90)
    panel.start_clock_at(None)  # no timestamp: unchanged
    assert panel.timeout == pytest.approx(hp.HOST_PANEL_TIMEOUT - 90)
