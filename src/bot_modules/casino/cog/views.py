"""Views and modals for the casino — Discord glue only.

Two persistence styles, per the house rules: the hub panel and the pools
panel are static custom_id ``LayoutView``s (a state-free dropdown/buttons,
one instance registered at cog_load); blackjack hands and roulette rounds
use DynamicItems whose custom_ids carry the hand/round id, so clicks route
after a restart with no re-registration per message. Every handler lives on
the cog; views just parse and dispatch.

``core.layout.bind`` (imported below as ``bind_layout``) is this module's
wiring helper (the pattern ``dm_perms_cog.py`` established first): it attaches
a callback to the persistent item carrying a given custom_id after a
``LayoutView`` subclass has added its content. ``core.layout.card`` — which
wraps a builder's finished items in a control-free ``LayoutView`` for a
one-off send — is ``cog.py``'s and ``pools_panel.py``'s to import; nothing in
this module sends a bare card. ``_row`` is casino-specific: every per-game
bet/resolve button here is a ``DynamicItem``, which discord.py types as
``Item[Union[View, LayoutView]]`` rather than narrowing it to
``LayoutView`` — so pyright rejects it as an ``ActionRow`` child even though
it works fine at runtime; taking the children in as ``Item[Any]`` sidesteps
that gap without a blanket ignore.
"""

from __future__ import annotations

import re

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any
from functools import partial

import discord

from bot_modules.casino import logic
from bot_modules.casino import service as svc
from bot_modules.casino import pools_logic
from bot_modules.casino.cog.layouts import TABLES
from bot_modules.casino.cog.hub_layout import (
    COMP_CUSTOM_ID,
    HELP_CUSTOM_ID,
    STATS_CUSTOM_ID,
    STATS_OPTION,
    TABLE_SELECT_CUSTOM_ID,
    build_casino_hub_layout,
)
from bot_modules.core.layout import bind as bind_layout
from bot_modules.core.utils import safe_ephemeral as _core_safe_ephemeral
from bot_modules.economy.service import EconSettings

if TYPE_CHECKING:
    from bot_modules.casino.cog.cog import CasinoCog

#: Builds the typed modal for a bet whose target is already chosen. The
#: ladder uses it twice over: Custom… opens it, and a rung borrows its
#: ``_place`` so the tap and the typed path route identically.
ModalFactory = Callable[[], "_AmountBetModal"]
#: Puts a private round's board back when a step that replaced it ends.
CancelFn = Callable[[discord.Interaction], Awaitable[None]]
ExpiryFn = Callable[[], Awaitable[None]]


def _cog(interaction: discord.Interaction) -> CasinoCog | None:
    cog = interaction.client.get_cog("CasinoCog")  # type: ignore[attr-defined]
    return cog


safe_ephemeral = partial(_core_safe_ephemeral, log_label="casino")


async def _dispatch_or_apologize(interaction: discord.Interaction) -> CasinoCog | None:
    cog = _cog(interaction)
    if cog is None:
        await safe_ephemeral(interaction, "❌ The casino isn't running right now.")
    return cog


def parse_amount(raw: str) -> int | None:
    """A bet amount from modal text — a positive whole number or None."""
    try:
        value = int(raw.strip().replace(",", ""))
    except ValueError:
        return None
    return value if value > 0 else None


def _row(*items: discord.ui.Item[Any]) -> discord.ui.ActionRow[Any]:
    """Build an ActionRow from persistent bet/resolve buttons.

    Every per-game bet/resolve button here is a ``DynamicItem``, which
    discord.py types as ``Item[Union[View, LayoutView]]`` rather than
    narrowing it to ``LayoutView`` — so pyright rejects it as an
    ``ActionRow`` child even though it works fine at runtime. That's
    discord.py's own typing gap, not a real issue; taking the children in
    as ``Item[Any]`` sidesteps it without a blanket ignore.
    """
    return discord.ui.ActionRow(*items)


# ── bet modals ─────────────────────────────────────────────────────────


class _AmountBetModal(discord.ui.Modal):
    """One amount box + the shared parse/apologize flow.

    The label carries the live limits ("Your bet (5–100 · 340 left today)")
    and the box pre-fills the member's last bet on this game — nobody
    should learn about a limit from the error after submitting. Subclasses
    set the title, stash their routing fields, and implement ``_place``.
    """

    def __init__(
        self,
        *,
        title: str,
        limits_label: str = "Your bet",
        default_amount: int | None = None,
    ) -> None:
        super().__init__(title=title[:45])
        self.amount: discord.ui.TextInput = discord.ui.TextInput(
            label=limits_label[:45],
            placeholder="A whole number of coins",
            default=str(default_amount) if default_amount else None,
            max_length=10,
        )
        self.add_item(self.amount)
        #: The step this box was opened over (Custom…), if any. The step
        #: stays live under the modal — the member may cancel it — so it is
        #: the submit that hands off, and the submit has to know the step.
        self.step: StepView | None = None

    async def on_submit(self, interaction: discord.Interaction) -> None:
        cog = await _dispatch_or_apologize(interaction)
        if cog is None:
            return
        amount = parse_amount(str(self.amount.value))
        if amount is None:
            await safe_ephemeral(interaction, "❌ Bets are whole positive numbers.")
            return
        if self.step is not None:
            interaction.extras[STEP_EXTRA] = self.step
        await self._place(cog, interaction, amount)

    async def _place(
        self, cog: CasinoCog, interaction: discord.Interaction, amount: int
    ) -> None:
        raise NotImplementedError


class BetModal(_AmountBetModal):
    """``game`` (+ coinflip's ``side``) decides the instant table."""

    def __init__(
        self,
        *,
        title: str,
        game: str,
        side: str | None = None,
        limits_label: str = "Your bet",
        default_amount: int | None = None,
    ) -> None:
        super().__init__(
            title=title, limits_label=limits_label, default_amount=default_amount
        )
        self.game = game
        self.side = side

    async def _place(
        self, cog: CasinoCog, interaction: discord.Interaction, amount: int
    ) -> None:
        if self.game == "coinflip" and self.side is not None:
            await cog.play_coinflip(interaction, self.side, amount)
        elif self.game == "slots":
            await cog.play_slots(interaction, amount)
        elif self.game == "blackjack":
            await cog.deal_blackjack(interaction, amount)
        elif self.game == "war":
            await cog.play_war(interaction, amount)


ROULETTE_KINDS = {
    "red": ("red", 0),
    "black": ("black", 0),
    "d1": ("dozen", 1),
    "d2": ("dozen", 2),
    "d3": ("dozen", 3),
}


class RouletteBetModal(_AmountBetModal):
    """One amount box; the wager was resolved before this opened.

    A straight-up bet used to carry a second box asking for a number 0–36,
    typed and then validated. The number comes off a pair of selects now
    (37 values overflow one select's 25-option cap), so by the time this
    modal exists the bet is fully decided and only the stake is open.
    """

    def __init__(
        self,
        round_id: int,
        bet_type: str,
        selection: int,
        *,
        limits_label: str = "Your bet",
        default_amount: int | None = None,
    ) -> None:
        super().__init__(
            title=f"Back {logic.describe_bet(bet_type, selection)}",
            limits_label=limits_label, default_amount=default_amount,
        )
        self.round_id = round_id
        self.bet_type = bet_type
        self.selection = selection

    async def _place(
        self, cog: CasinoCog, interaction: discord.Interaction, amount: int
    ) -> None:
        await cog.place_roulette_bet(
            interaction, self.round_id, self.bet_type, self.selection, amount
        )


class DerbyBetModal(_AmountBetModal):
    """One amount box; the runner was chosen by the button that opened it."""

    def __init__(
        self,
        round_id: int,
        runner: int,
        runner_label: str,
        **kwargs,
    ) -> None:
        super().__init__(title=f"Back {runner_label}", **kwargs)
        self.round_id = round_id
        self.runner = runner

    async def _place(
        self, cog: CasinoCog, interaction: discord.Interaction, amount: int
    ) -> None:
        await cog.place_derby_bet(interaction, self.round_id, self.runner, amount)


class BaccaratBetModal(_AmountBetModal):
    """One amount box; the side was chosen by the button that opened it."""

    def __init__(
        self,
        round_id: int,
        side: str,
        side_label: str,
        **kwargs,
    ) -> None:
        super().__init__(title=f"Back {side_label}", **kwargs)
        self.round_id = round_id
        self.side = side

    async def _place(
        self, cog: CasinoCog, interaction: discord.Interaction, amount: int
    ) -> None:
        await cog.place_baccarat_bet(
            interaction, self.round_id, self.side, amount
        )


class DiceBetModal(_AmountBetModal):
    """One amount box; the call was chosen by the button that opened it."""

    def __init__(
        self,
        round_id: int,
        bet_type: str,
        call_label: str,
        **kwargs,
    ) -> None:
        super().__init__(title=f"Call {call_label}", **kwargs)
        self.round_id = round_id
        self.bet_type = bet_type

    async def _place(
        self, cog: CasinoCog, interaction: discord.Interaction, amount: int
    ) -> None:
        await cog.place_dice_bet(
            interaction, self.round_id, self.bet_type, amount
        )


# ── bet steps with a shelf life ────────────────────────────────────────

STEP_EXPIRED_MSG = "This step expired — pick the table on the panel again."
#: ``Interaction.extras`` key under which a press notes the step it came
#: from, so whatever the press replaces the step's message with can stop it.
STEP_EXTRA = "casino_step"


class StepView(discord.ui.View):
    """A private bet step that knows how to expire out loud.

    A step (the amount ladder, the coinflip side picker, the roulette
    number picker) lives in an ephemeral message with a timer. Past it,
    Discord answers every press with "This interaction failed" — no hint
    that the fix is to pick the table on the hub again. So a step
    is **bound** to the interaction that showed it, and on timeout it
    disables its own buttons and rewrites the message to say what to do.

    The interaction token is the only handle on an ephemeral message once
    the response is sent, and it lives ~15 minutes — every step's timeout
    is comfortably inside that. A failed edit (the member dismissed the
    message) is the ordinary end of an abandoned step, not an error.

    The clock has to stop when the step does. discord.py keeps a replaced
    view's timeout running — an edit with a new view cancels nothing — so
    a coinflip picker whose message had become the result card would wake
    120s later and write the expiry copy over the card and its Play Again
    button. Every press on a step notes the step on the interaction
    (``interaction_check``), and the cog **consumes** it (``consume_step``)
    once that press has replaced the message; an expiry that still arrives
    after that is a no-op.

    A step is a plain ``discord.ui.View`` still, not a ``LayoutView`` — it
    carries no card body, just controls over whatever message (a board, a
    result) it was opened on, so it stays the ordinary control-only shape
    Components V2 leaves unchanged.
    """

    def __init__(self, *, timeout: float) -> None:
        super().__init__(timeout=timeout)
        self._bound: discord.Interaction | None = None
        self._consumed = False

    def bind(self, interaction: discord.Interaction) -> None:
        """Remember the interaction whose response this step is."""
        self._bound = interaction

    async def interaction_check(self, interaction: discord.Interaction, /) -> bool:
        interaction.extras[STEP_EXTRA] = self
        return True

    def consume(self) -> None:
        """The message now shows something else — stop the clock for good.

        ``is_finished()`` can't be the guard: discord.py marks the view
        finished *before* it schedules ``on_timeout``, so a genuine expiry
        would read as consumed too.
        """
        self._consumed = True
        self.stop()

    async def expire(self) -> None:
        if self._consumed:
            return
        for item in self.children:
            if isinstance(item, (discord.ui.Button, discord.ui.Select)):
                item.disabled = True
        if self._bound is None:
            return
        try:
            await self._bound.edit_original_response(
                content=STEP_EXPIRED_MSG, embed=None, view=self
            )
        except discord.HTTPException:
            pass

    async def on_timeout(self) -> None:
        await self.expire()


def consume_step(interaction: discord.Interaction) -> None:
    """Stop the step a press came from, once the press has replaced its
    message. A press from anywhere else (the hub, a board) noted no step
    and this is a no-op."""
    step = interaction.extras.pop(STEP_EXTRA, None)
    if isinstance(step, StepView):
        step.consume()


# ── the amount ladder (one tap for the usual stake) ────────────────────


class _LadderButton(discord.ui.Button):
    """One rung. Routes through the same ``_place`` the modal would."""

    def __init__(self, option: logic.BetOption, make_modal: ModalFactory) -> None:
        super().__init__(
            label=option.label, style=discord.ButtonStyle.primary, row=0
        )
        self.amount = option.amount
        self._make_modal = make_modal

    async def callback(self, interaction: discord.Interaction) -> None:
        cog = await _dispatch_or_apologize(interaction)
        if cog is not None:
            await self._make_modal()._place(cog, interaction, self.amount)


class _CustomAmountButton(discord.ui.Button):
    """The typed path, kept for every stake the ladder doesn't carry."""

    def __init__(self, make_modal: ModalFactory) -> None:
        super().__init__(
            label="Custom…", style=discord.ButtonStyle.secondary, row=1
        )
        self._make_modal = make_modal

    async def callback(self, interaction: discord.Interaction) -> None:
        modal = self._make_modal()
        if isinstance(self.view, StepView):
            modal.step = self.view
        await interaction.response.send_modal(modal)


class _BackButton(discord.ui.Button):
    """Puts the surface a step replaced back on screen.

    ``row`` is a parameter rather than a constant because the two steps have
    different shapes: the ladder's rungs leave row 1 free, while the number
    step spends rows 0 and 1 on selects — a select occupies a whole row, so
    a hardcoded row 1 made the view refuse to build at all.
    """

    def __init__(self, on_cancel: CancelFn, *, row: int = 1) -> None:
        super().__init__(
            label="Back", emoji="↩️", style=discord.ButtonStyle.secondary, row=row
        )
        self._on_cancel = on_cancel

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._on_cancel(interaction)


class AmountPickerView(StepView):
    """The stake, as buttons.

    Every wager used to cost a modal round-trip to type a number, on tables
    where prod says one player often bets several times in a round. The
    ladder comes from ``logic.bet_amount_options`` and is built so no rung
    can be refused; Custom… still opens the box for anything else.

    ``on_cancel`` / ``on_expiry`` exist for the private-round tables, where
    this step replaces the round's own board and something has to put the
    board back — on the player's press, and on the view timing out under
    them. A hub game's ladder has no board to restore, so its timeout says
    the step expired instead (``StepView``).
    """

    def __init__(
        self,
        make_modal: ModalFactory,
        options: list[logic.BetOption],
        *,
        on_cancel: CancelFn | None = None,
        on_expiry: ExpiryFn | None = None,
    ) -> None:
        # Comfortably inside the ~15 minutes an ephemeral stays editable,
        # so the board can still be restored when this expires.
        super().__init__(timeout=600)
        self._on_expiry = on_expiry
        for option in options:
            self.add_item(_LadderButton(option, make_modal))
        self.add_item(_CustomAmountButton(make_modal))
        if on_cancel is not None:
            self.add_item(_BackButton(on_cancel))

    async def on_timeout(self) -> None:
        # The board repaint replaces this message; writing "expired" over it
        # first would race the repaint, so the restorer keeps precedence.
        if self._on_expiry is not None:
            await self._on_expiry()
        else:
            await self.expire()


_WHEEL_EMOJI = {"red": "🔴", "black": "⚫", "green": "🟢"}


class _RouletteNumberSelect(discord.ui.Select):
    """Half the wheel — 37 numbers overflow one select's 25-option cap."""

    def __init__(self, round_id: int, low: int, high: int, row: int) -> None:
        super().__init__(
            placeholder=f"{low}–{high}",
            row=row,
            options=[
                discord.SelectOption(
                    label=str(n),
                    value=str(n),
                    emoji=_WHEEL_EMOJI[logic.wheel_color(n)],
                )
                for n in range(low, high + 1)
            ],
        )
        self.round_id = round_id

    async def callback(self, interaction: discord.Interaction) -> None:
        cog = await _dispatch_or_apologize(interaction)
        if cog is not None:
            await cog.open_roulette_amount_picker(
                interaction, self.round_id, "number", int(self.values[0])
            )


class RouletteNumberView(StepView):
    """The straight-up picker: two selects, then the amount ladder."""

    def __init__(
        self,
        round_id: int,
        *,
        on_cancel: CancelFn | None = None,
        on_expiry: ExpiryFn | None = None,
    ) -> None:
        super().__init__(timeout=600)
        self._on_expiry = on_expiry
        self.add_item(_RouletteNumberSelect(round_id, 0, 18, row=0))
        self.add_item(_RouletteNumberSelect(round_id, 19, 36, row=1))
        if on_cancel is not None:
            self.add_item(_BackButton(on_cancel, row=2))

    async def on_timeout(self) -> None:
        if self._on_expiry is not None:
            await self._on_expiry()
        else:
            await self.expire()


# ── the hub panel ──────────────────────────────────────────────────────


class CoinflipSideView(StepView):
    """Ephemeral heads-or-tails picker; each side opens the amount ladder."""

    def __init__(self) -> None:
        super().__init__(timeout=120)

    @discord.ui.button(label="Heads", emoji="🌞", style=discord.ButtonStyle.primary)
    async def heads(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        cog = await _dispatch_or_apologize(interaction)
        if cog is not None:
            await cog.open_bet_picker(interaction, "coinflip", side="heads")

    @discord.ui.button(label="Tails", emoji="🌙", style=discord.ButtonStyle.primary)
    async def tails(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        cog = await _dispatch_or_apologize(interaction)
        if cog is not None:
            await cog.open_bet_picker(interaction, "coinflip", side="tails")


async def open_table(interaction: discord.Interaction, game: str) -> None:
    """Open ``game``'s first step for the clicker — the one route every
    entry to a table shares, the panel's dropdown and a legacy panel's
    buttons alike."""
    if game == "coinflip":
        view = CoinflipSideView()
        await interaction.response.send_message(
            "Heads or tails?", view=view, ephemeral=True
        )
        view.bind(interaction)
        return
    cog = await _dispatch_or_apologize(interaction)
    if cog is None:
        return
    if game in ("slots", "blackjack", "war"):
        await cog.open_bet_picker(interaction, game)
    else:
        opener = getattr(cog, f"open_{game}", None)
        if opener is None:
            await safe_ephemeral(interaction, "❌ That table isn't open.")
            return
        await opener(interaction)


async def _claim_comp(interaction: discord.Interaction) -> None:
    cog = await _dispatch_or_apologize(interaction)
    if cog is not None:
        await cog.claim_daily_comp(interaction)


async def _my_stats(interaction: discord.Interaction) -> None:
    cog = await _dispatch_or_apologize(interaction)
    if cog is not None:
        await cog.send_my_stats(interaction)


async def _help(interaction: discord.Interaction) -> None:
    cog = await _dispatch_or_apologize(interaction)
    if cog is not None:
        await cog.send_help(interaction)


async def _pick_table(interaction: discord.Interaction) -> None:
    """The panel's **Pick a table…** dropdown.

    Opens the table, then schedules the debounced hub repaint: a persistent
    select keeps showing the member's pick until the message is edited, and
    Discord sends no second event for picking the same option again. The
    repaint (an edit, ~8 s later) puts the placeholder back.

    Its last option, My Stats, is not a table: it runs the stats button's
    own callback, and then the same repaint.
    """
    values = (interaction.data or {}).get("values") or []  # type: ignore[union-attr]
    game = str(values[0]) if values else ""
    if game == STATS_OPTION:
        await _my_stats(interaction)
    elif game in svc.GAMES:
        await open_table(interaction, game)
    else:
        await safe_ephemeral(interaction, "❌ That table isn't open.")
        return
    cog = _cog(interaction)
    if cog is not None and interaction.guild_id is not None:
        cog._schedule_hub_repaint(interaction.guild_id)


class CasinoPanelView(discord.ui.LayoutView):
    """The casino hub panel: the live board in the hub's frame, **Help** beside
    the title, the table dropdown and the member's buttons at the foot.

    Carries no per-message state — every id is static — so it is
    re-registered in ``cog_load`` rather than being a DynamicItem.
    ``items=None`` is that registration instance: every table in the
    dropdown and the comp button present, so whatever a posted panel shows
    routes after a restart.
    """

    def __init__(self, items: list[discord.ui.Item] | None = None) -> None:
        super().__init__(timeout=None)
        if items is None:
            items = build_casino_hub_layout(
                EconSettings(), svc.CasinoSettings(),
                games=list(svc.GAMES), comp=True, stats_button=True,
            )
        for item in items:
            self.add_item(item)
        # A panel with every table closed has no dropdown to bind.
        if any(
            getattr(item, "custom_id", None) == TABLE_SELECT_CUSTOM_ID
            for item in self.walk_children()
        ):
            bind_layout(self, TABLE_SELECT_CUSTOM_ID, _pick_table)
        if any(
            getattr(item, "custom_id", None) == COMP_CUSTOM_ID
            for item in self.walk_children()
        ):
            bind_layout(self, COMP_CUSTOM_ID, _claim_comp)
        if any(
            getattr(item, "custom_id", None) == STATS_CUSTOM_ID
            for item in self.walk_children()
        ):
            bind_layout(self, STATS_CUSTOM_ID, _my_stats)
        bind_layout(self, HELP_CUSTOM_ID, _help)


def _legacy_button(game: str, label: str, emoji: str) -> discord.ui.Button:
    button: discord.ui.Button = discord.ui.Button(
        label=label, emoji=emoji, custom_id=f"casino:{game}",
    )

    async def _open(interaction: discord.Interaction) -> None:
        await open_table(interaction, game)

    button.callback = _open  # type: ignore[method-assign]
    return button


class CasinoHubView(discord.ui.View):
    """The embed-era hub panel's buttons — never posted, registered in
    ``cog_load`` only, so a panel posted before the Components V2 hub keeps
    answering until its sticky repaints it. One button per table: its
    Daily Comp, My Stats and How It Works carried the ids the new panel's
    buttons keep, and the registered ``CasinoPanelView`` routes those.
    """

    def __init__(self) -> None:
        super().__init__(timeout=None)
        for game in svc.GAMES:
            emoji, label, _blurb = TABLES[game]
            self.add_item(_legacy_button(game, label, emoji))


# ── blackjack table buttons ────────────────────────────────────────────

_BJ_STYLES = {
    "hit": ("Hit", discord.ButtonStyle.primary),
    "stand": ("Stand", discord.ButtonStyle.secondary),
    "double": ("Double Down", discord.ButtonStyle.success),
}


class BlackjackActionButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=re.compile(r"casino_bj:(?P<action>hit|stand|double):(?P<hid>\d+)"),
):
    def __init__(self, action: str, hand_id: int) -> None:
        label, style = _BJ_STYLES[action]
        super().__init__(
            discord.ui.Button(
                label=label, style=style,
                custom_id=f"casino_bj:{action}:{hand_id}",
            )
        )
        self.action = action
        self.hand_id = hand_id

    @classmethod
    async def from_custom_id(  # type: ignore[override]
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Button,
        match: re.Match[str],
    ) -> BlackjackActionButton:
        return cls(match["action"], int(match["hid"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        cog = await _dispatch_or_apologize(interaction)
        if cog is not None:
            await cog.blackjack_action(interaction, self.hand_id, self.action)


def build_blackjack_action_rows(
    hand_id: int, *, can_double: bool
) -> list[discord.ui.ActionRow[Any]]:
    buttons: list[discord.ui.Item[Any]] = [
        BlackjackActionButton("hit", hand_id),
        BlackjackActionButton("stand", hand_id),
    ]
    if can_double:
        buttons.append(BlackjackActionButton("double", hand_id))
    return [discord.ui.ActionRow(*buttons)]


# ── roulette round buttons ─────────────────────────────────────────────

_RL_SPECS: dict[str, tuple[str, str | None, discord.ButtonStyle, int]] = {
    "red": ("Red", "🔴", discord.ButtonStyle.danger, 0),
    "black": ("Black", "⚫", discord.ButtonStyle.secondary, 0),
    "num": ("Number", "🎯", discord.ButtonStyle.primary, 0),
    "d1": ("1–12", None, discord.ButtonStyle.secondary, 1),
    "d2": ("13–24", None, discord.ButtonStyle.secondary, 1),
    "d3": ("25–36", None, discord.ButtonStyle.secondary, 1),
}


class RouletteBetButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=re.compile(r"casino_rl:(?P<kind>red|black|num|d1|d2|d3):(?P<rid>\d+)"),
):
    def __init__(self, kind: str, round_id: int) -> None:
        label, emoji, style, row = _RL_SPECS[kind]
        super().__init__(
            discord.ui.Button(
                label=label, emoji=emoji, style=style, row=row,
                custom_id=f"casino_rl:{kind}:{round_id}",
            )
        )
        self.kind = kind
        self.round_id = round_id

    @classmethod
    async def from_custom_id(  # type: ignore[override]
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Button,
        match: re.Match[str],
    ) -> RouletteBetButton:
        return cls(match["kind"], int(match["rid"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        cog = await _dispatch_or_apologize(interaction)
        if cog is not None:
            await cog.open_roulette_bet_step(
                interaction, self.round_id, self.kind
            )


# The verb, emoji and row for each game's resolve button. Anchored in the
# template below as an enumerated alternation (the roulette-button rule), so
# a stale custom_id naming a game that no longer exists fails the match
# rather than KeyError-ing inside dispatch.
_RESOLVE_SPECS = {
    "roulette": ("Spin", "🎡"),
    "derby": ("Race", "🏇"),
    "baccarat": ("Deal", "🎴"),
    "dice": ("Roll", "🎲"),
    "keno": ("Draw", "🔢"),
}


class RoundResolveButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=re.compile(
        r"casino_go:(?P<game>roulette|derby|baccarat|dice|keno):(?P<rid>\d+)"
    ),
):
    """"Go" on a private round — the player's replacement for the clock.

    A communal round closed on a timer because other people needed time to
    join. Nobody else can join a private one, so the only sensible moment to
    resolve is the one its owner picks.
    """

    def __init__(self, game: str, round_id: int) -> None:
        label, emoji = _RESOLVE_SPECS[game]
        super().__init__(
            discord.ui.Button(
                label=label, emoji=emoji,
                style=discord.ButtonStyle.success, row=4,
                custom_id=f"casino_go:{game}:{round_id}",
            )
        )
        self.game = game
        self.round_id = round_id

    @classmethod
    async def from_custom_id(  # type: ignore[override]
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Button,
        match: re.Match[str],
    ) -> RoundResolveButton:
        return cls(match["game"], int(match["rid"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        cog = await _dispatch_or_apologize(interaction)
        if cog is not None:
            await cog.resolve_round(interaction, self.game, self.round_id)


def build_roulette_action_rows(round_id: int) -> list[discord.ui.ActionRow[Any]]:
    row0 = _row(
        RouletteBetButton("red", round_id),
        RouletteBetButton("black", round_id),
        RouletteBetButton("num", round_id),
    )
    row1 = _row(
        RouletteBetButton("d1", round_id),
        RouletteBetButton("d2", round_id),
        RouletteBetButton("d3", round_id),
    )
    row4 = _row(RoundResolveButton("roulette", round_id))
    return [row0, row1, row4]


# ── derby race buttons ─────────────────────────────────────────────────


class DerbyBetButton(
    discord.ui.DynamicItem[discord.ui.Button],
    # The runner range is anchored in the template itself (the roulette
    # buttons' enumerated-alternation rule): a stale button minted for a
    # runner the field no longer has must fail the match, not IndexError
    # inside dispatch.
    template=re.compile(
        rf"casino_dy:(?P<runner>[0-{len(logic.DERBY_FIELD) - 1}]):(?P<rid>\d+)"
    ),
):
    def __init__(self, runner: int, round_id: int) -> None:
        r = logic.DERBY_FIELD[runner]
        super().__init__(
            discord.ui.Button(
                label=f"{r.name.split()[0]} {logic.derby_odds_label(runner)}",
                emoji=r.emoji,
                style=discord.ButtonStyle.secondary,
                row=runner // 3,
                custom_id=f"casino_dy:{runner}:{round_id}",
            )
        )
        self.runner = runner
        self.round_id = round_id

    @classmethod
    async def from_custom_id(  # type: ignore[override]
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Button,
        match: re.Match[str],
    ) -> DerbyBetButton:
        return cls(int(match["runner"]), int(match["rid"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        cog = await _dispatch_or_apologize(interaction)
        if cog is not None:
            await cog.open_derby_bet_picker(
                interaction, self.round_id, self.runner
            )


def build_derby_action_rows(round_id: int) -> list[discord.ui.ActionRow[Any]]:
    rows: list[discord.ui.ActionRow[Any]] = []
    field = [DerbyBetButton(runner, round_id) for runner in range(len(logic.DERBY_FIELD))]
    for start in range(0, len(field), 3):
        rows.append(_row(*field[start:start + 3]))
    rows.append(_row(RoundResolveButton("derby", round_id)))
    return rows


# ── baccarat coup buttons ──────────────────────────────────────────────

_BC_SPECS: dict[str, tuple[str, str, discord.ButtonStyle]] = {
    "player": ("Player 2×", "🔵", discord.ButtonStyle.primary),
    "banker": ("Banker 2×", "🔴", discord.ButtonStyle.danger),
    "tie": ("Tie 9×", "🟡", discord.ButtonStyle.secondary),
}


class BaccaratBetButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=re.compile(r"casino_bc:(?P<side>player|banker|tie):(?P<rid>\d+)"),
):
    def __init__(self, side: str, round_id: int) -> None:
        label, emoji, style = _BC_SPECS[side]
        super().__init__(
            discord.ui.Button(
                label=label, emoji=emoji, style=style,
                custom_id=f"casino_bc:{side}:{round_id}",
            )
        )
        self.side = side
        self.round_id = round_id

    @classmethod
    async def from_custom_id(  # type: ignore[override]
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Button,
        match: re.Match[str],
    ) -> BaccaratBetButton:
        return cls(match["side"], int(match["rid"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        cog = await _dispatch_or_apologize(interaction)
        if cog is not None:
            await cog.open_baccarat_bet_picker(
                interaction, self.round_id, self.side
            )


def build_baccarat_action_rows(round_id: int) -> list[discord.ui.ActionRow[Any]]:
    row0 = _row(
        *(BaccaratBetButton(side, round_id) for side in logic.BACCARAT_SIDES)
    )
    row4 = _row(RoundResolveButton("baccarat", round_id))
    return [row0, row4]


# ── dice roll buttons ──────────────────────────────────────────────────

_DC_SPECS: dict[str, tuple[str, str | None, discord.ButtonStyle, int]] = {
    "big": ("Big 11–17", "⬆️", discord.ButtonStyle.primary, 0),
    "small": ("Small 4–10", "⬇️", discord.ButtonStyle.primary, 0),
    "odd": ("Odd", None, discord.ButtonStyle.secondary, 1),
    "even": ("Even", None, discord.ButtonStyle.secondary, 1),
}


class DiceBetButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=re.compile(r"casino_dc:(?P<kind>big|small|odd|even):(?P<rid>\d+)"),
):
    def __init__(self, kind: str, round_id: int) -> None:
        label, emoji, style, row = _DC_SPECS[kind]
        super().__init__(
            discord.ui.Button(
                label=label, emoji=emoji, style=style, row=row,
                custom_id=f"casino_dc:{kind}:{round_id}",
            )
        )
        self.kind = kind
        self.round_id = round_id

    @classmethod
    async def from_custom_id(  # type: ignore[override]
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Button,
        match: re.Match[str],
    ) -> DiceBetButton:
        return cls(match["kind"], int(match["rid"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        cog = await _dispatch_or_apologize(interaction)
        if cog is not None:
            await cog.open_dice_bet_picker(
                interaction, self.round_id, self.kind
            )


def build_dice_action_rows(round_id: int) -> list[discord.ui.ActionRow[Any]]:
    row0 = _row(
        DiceBetButton("big", round_id), DiceBetButton("small", round_id)
    )
    row1 = _row(
        DiceBetButton("odd", round_id), DiceBetButton("even", round_id)
    )
    row4 = _row(RoundResolveButton("dice", round_id))
    return [row0, row1, row4]


# ── keno ticket buttons ────────────────────────────────────────────────


class KenoTicketModal(_AmountBetModal):
    """One amount box; the tier was chosen by the button that opened it."""

    def __init__(self, round_id: int, spots: int, **kwargs) -> None:
        super().__init__(title=f"Keno — Pick {spots}", **kwargs)
        self.round_id = round_id
        self.spots = spots

    async def _place(
        self, cog: CasinoCog, interaction: discord.Interaction, amount: int
    ) -> None:
        await cog.place_keno_ticket(
            interaction, self.round_id, self.spots, amount
        )


class KenoTierButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=re.compile(r"casino_kn:(?P<spots>4|6|8|10):(?P<rid>\d+)"),
):
    def __init__(self, spots: int, round_id: int) -> None:
        top = max(logic.KENO_PAYTABLE[spots].values())
        super().__init__(
            discord.ui.Button(
                label=f"Pick {spots} · to {top}×",
                emoji="🎟️",
                style=discord.ButtonStyle.primary,
                custom_id=f"casino_kn:{spots}:{round_id}",
            )
        )
        self.spots = spots
        self.round_id = round_id

    @classmethod
    async def from_custom_id(  # type: ignore[override]
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Button,
        match: re.Match[str],
    ) -> KenoTierButton:
        return cls(int(match["spots"]), int(match["rid"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        cog = await _dispatch_or_apologize(interaction)
        if cog is not None:
            await cog.open_keno_ticket_picker(
                interaction, self.round_id, self.spots
            )


def build_keno_action_rows(round_id: int) -> list[discord.ui.ActionRow[Any]]:
    row0 = _row(
        *(KenoTierButton(spots, round_id) for spots in logic.KENO_TIERS)
    )
    row4 = _row(RoundResolveButton("keno", round_id))
    return [row0, row4]


# ── war standoff buttons ───────────────────────────────────────────────

_WAR_STYLES = {
    "war": ("Go to War", "⚔️", discord.ButtonStyle.danger),
    "retreat": ("Retreat", "🏳️", discord.ButtonStyle.secondary),
}


class WarActionButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=re.compile(r"casino_wr:(?P<action>war|retreat):(?P<hid>\d+)"),
):
    def __init__(self, action: str, hand_id: int) -> None:
        label, emoji, style = _WAR_STYLES[action]
        super().__init__(
            discord.ui.Button(
                label=label, emoji=emoji, style=style,
                custom_id=f"casino_wr:{action}:{hand_id}",
            )
        )
        self.action = action
        self.hand_id = hand_id

    @classmethod
    async def from_custom_id(  # type: ignore[override]
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Button,
        match: re.Match[str],
    ) -> WarActionButton:
        return cls(match["action"], int(match["hid"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        cog = await _dispatch_or_apologize(interaction)
        if cog is not None:
            await cog.war_action(interaction, self.hand_id, self.action)


def build_war_action_rows(hand_id: int) -> list[discord.ui.ActionRow[Any]]:
    return [
        _row(
            WarActionButton("war", hand_id), WarActionButton("retreat", hand_id)
        )
    ]


# ── mines grid buttons ─────────────────────────────────────────────────

# 20 tiles (5 wide × 4 tall) in 4 ActionRows + one Cash Out row = 26
# components, well under the 40-component budget. That is what chose the
# grid size: a 5×5 board leaves nowhere to put Cash Out, and the voluntary
# stop IS the game.

_MINES_HIDDEN = "⬛"   # ⬛ an unopened tile
_MINES_SAFE = "\U0001f48e"  # 💎 a safe reveal
_MINES_BOMB = "\U0001f4a3"  # 💣 the one that ended it


_MINES_RISK_CUSTOM_ID = "casino_mn:risk"


class MinesRiskSelect(
    discord.ui.DynamicItem[discord.ui.Select],
    template=re.compile(r"casino_mn:risk"),
):
    """The bomb-count dial — one dropdown, a description per rung.

    Four mutually exclusive risk levels used to be four buttons; the style
    guide's "a mutually exclusive choice is a Select" rule applies to this
    picker exactly as it does to a persistent one, so it holds regardless of
    the surrounding view's shape. "5 bombs · 8 tiles to 18.60×" — what the
    choice costs and what it tops out at — no longer fits a two-word button
    label, so it moves to the option description, read before the pick
    rather than after.
    """

    def __init__(self) -> None:
        super().__init__(
            discord.ui.Select(
                custom_id=_MINES_RISK_CUSTOM_ID,
                placeholder="How dangerous do you want it?",
                min_values=1,
                max_values=1,
                options=[
                    discord.SelectOption(
                        label=f"{bombs} Bomb{'' if bombs == 1 else 's'}",
                        value=str(bombs),
                        description=logic.mines_risk_label(bombs),
                        emoji="💣",
                    )
                    for bombs in logic.MINES_BOMB_CHOICES
                ],
            )
        )

    @classmethod
    async def from_custom_id(  # type: ignore[override]
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Select,
        match: re.Match[str],
    ) -> MinesRiskSelect:
        return cls()

    async def callback(self, interaction: discord.Interaction) -> None:
        cog = await _dispatch_or_apologize(interaction)
        if cog is not None:
            values = (interaction.data or {}).get("values") or []
            bombs = int(values[0]) if values else logic.MINES_BOMB_CHOICES[0]
            await cog.open_mines_bet_picker(interaction, bombs)


class MinesBetModal(_AmountBetModal):
    """One amount box; the risk was chosen by the button that opened it."""

    def __init__(self, bombs: int, **kwargs) -> None:
        super().__init__(title=f"Mines — {bombs} bomb{'' if bombs == 1 else 's'}",
                         **kwargs)
        self.bombs = bombs

    async def _place(
        self, cog: CasinoCog, interaction: discord.Interaction, amount: int
    ) -> None:
        await cog.deal_mines(interaction, self.bombs, amount)


def build_mines_risk_view() -> discord.ui.View:
    """The risk picker never held an embed — a one-line ``content=`` prompt
    over a single control — so unlike every other builder here it stays an
    ordinary View rather than a LayoutView's ActionRows; there is no card
    for it to join."""
    view = discord.ui.View(timeout=None)
    view.add_item(MinesRiskSelect())
    return view


class MinesTileButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=re.compile(r"casino_mn:tile:(?P<hid>\d+):(?P<tile>\d+)"),
):
    """One tile. Opened tiles stay in place as disabled buttons so the grid
    never reflows under the player's finger mid-press."""

    def __init__(
        self, hand_id: int, tile: int, *, opened: bool = False,
        emoji: str = _MINES_HIDDEN,
    ) -> None:
        super().__init__(
            discord.ui.Button(
                emoji=emoji,
                style=(
                    discord.ButtonStyle.success if opened
                    else discord.ButtonStyle.secondary
                ),
                disabled=opened,
                custom_id=f"casino_mn:tile:{hand_id}:{tile}",
                row=tile // logic.MINES_GRID_WIDTH,
            )
        )
        self.hand_id = hand_id
        self.tile = tile

    @classmethod
    async def from_custom_id(  # type: ignore[override]
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Button,
        match: re.Match[str],
    ) -> MinesTileButton:
        return cls(int(match["hid"]), int(match["tile"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        cog = await _dispatch_or_apologize(interaction)
        if cog is not None:
            await cog.mines_reveal(interaction, self.hand_id, self.tile)


class MinesCashOutButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=re.compile(r"casino_mn:cash:(?P<hid>\d+)"),
):
    """The stop button, carrying what it actually banks right now.

    Dead until the first tile is open: there is no rung at zero reveals,
    and a stop button that pays 0.95× for doing nothing is a trap.
    """

    def __init__(self, hand_id: int, *, payout: int = 0, unit: str = "coins") -> None:
        super().__init__(
            discord.ui.Button(
                label=(
                    f"Cash Out — {payout:,} {unit}" if payout
                    else "Cash Out"
                ),
                emoji="💰",
                style=discord.ButtonStyle.success,
                disabled=not payout,
                custom_id=f"casino_mn:cash:{hand_id}",
                row=4,
            )
        )
        self.hand_id = hand_id

    @classmethod
    async def from_custom_id(  # type: ignore[override]
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Button,
        match: re.Match[str],
    ) -> MinesCashOutButton:
        return cls(int(match["hid"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        cog = await _dispatch_or_apologize(interaction)
        if cog is not None:
            await cog.mines_cash_out(interaction, self.hand_id)


def build_mines_action_rows(
    hand_id: int,
    revealed: tuple[int, ...] | list[int],
    *,
    payout: int = 0,
    unit: str = "coins",
) -> list[discord.ui.ActionRow[Any]]:
    """The live grid: 20 tiles + the stop button, four rows of five plus
    the cash-out row."""
    opened = set(revealed)
    tiles = [
        MinesTileButton(
            hand_id, tile,
            opened=tile in opened,
            emoji=_MINES_SAFE if tile in opened else _MINES_HIDDEN,
        )
        for tile in range(logic.MINES_TILES)
    ]
    rows: list[discord.ui.ActionRow[Any]] = []
    for start in range(0, len(tiles), logic.MINES_GRID_WIDTH):
        rows.append(_row(*tiles[start:start + logic.MINES_GRID_WIDTH]))
    rows.append(
        _row(MinesCashOutButton(hand_id, payout=payout, unit=unit))
    )
    return rows


# ── the loop-closer: Play Again ─────────────────────────────────────────

_AGAIN_LABELS = {
    "coinflip": "Flip again",
    "slots": "Spin again",
    "blackjack": "Deal again",
    "war": "Battle again",
    "mines": "Play again",
}


class PlayAgainButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=re.compile(
        r"casino_again:(?P<game>coinflip|slots|blackjack|war|mines)"
        r":(?P<side>heads|tails|x|\d+):(?P<amt>\d+)"
    ),
):
    """On every instant/blackjack result: replay the same bet — for
    WHOEVER clicks (their own coins, every guard re-applies). On your own
    ephemeral machine it spins the same message in place; on a public
    big-win broadcast it opens the clicker's own machine — the "me too"
    invitation surviving the ephemeral move."""

    def __init__(self, game: str, side: str, amount: int) -> None:
        side_note = (
            f" · {side}" if game == "coinflip"
            else f" · {side} 💣" if game == "mines"
            else ""
        )
        super().__init__(
            discord.ui.Button(
                label=f"{_AGAIN_LABELS[game]} ({amount:,}{side_note})",
                emoji="🔁",
                style=discord.ButtonStyle.secondary,
                custom_id=f"casino_again:{game}:{side}:{amount}",
            )
        )
        self.game = game
        self.side = side
        self.amount = amount

    @classmethod
    async def from_custom_id(  # type: ignore[override]
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Button,
        match: re.Match[str],
    ) -> PlayAgainButton:
        return cls(match["game"], match["side"], int(match["amt"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        cog = await _dispatch_or_apologize(interaction)
        if cog is None:
            return
        if self.game == "coinflip":
            await cog.play_coinflip(interaction, self.side, self.amount)
        elif self.game == "slots":
            await cog.play_slots(interaction, self.amount)
        elif self.game == "war":
            await cog.play_war(interaction, self.amount)
        elif self.game == "mines":
            await cog.deal_mines(interaction, int(self.side), self.amount)
        else:
            await cog.deal_blackjack(interaction, self.amount)


def play_again_row(
    game: str, amount: int, side: str = "x"
) -> discord.ui.ActionRow[Any]:
    return _row(PlayAgainButton(game, side, amount))


# ── pools (casino-classics Stage 2) ────────────────────────────────────


class PoolsBetModal(_AmountBetModal):
    """One amount box; the side was chosen by the button that opened it."""

    def __init__(self, side: str, **kwargs) -> None:
        super().__init__(
            title=f"Bet {pools_logic.describe_side(side)}", **kwargs
        )
        self.side = side

    async def _place(
        self, cog: CasinoCog, interaction: discord.Interaction, amount: int
    ) -> None:
        await cog.place_pools_bet(interaction, self.side, amount)


def pools_bet_row() -> discord.ui.ActionRow[Any]:
    """The Pools panel's two standing bet buttons."""
    return discord.ui.ActionRow(
        discord.ui.Button(
            label="Bet Over", emoji="📈",
            style=discord.ButtonStyle.success,
            custom_id="casino:pools_over",
        ),
        discord.ui.Button(
            label="Bet Under", emoji="📉",
            style=discord.ButtonStyle.danger,
            custom_id="casino:pools_under",
        ),
    )


class PoolsPanelView(discord.ui.LayoutView):
    """The daily pools market panel: live content (line, odds, chart) plus
    the two standing bet buttons, all in the one LayoutView a V2 message
    carries. No round id in the custom ids: there is exactly one open
    market per guild at a time, so the handler resolves it at click — which
    also means the panel keeps working across a restart and across the day
    roll without re-registering anything, and a ``content=None`` instance
    (cog_load's routing copy) still carries both ids to bind against.

    ``content`` is the whole card, bet row included
    (``layouts.build_pools_panel_layout(..., controls=pools_bet_row())``), so
    the card-strip footer can close it below the buttons; with no content the
    view is just the row. A content list without the row fails here, at
    ``bind_layout``, not on a member's press.
    """

    def __init__(self, content: list[discord.ui.Item[Any]] | None = None) -> None:
        super().__init__(timeout=None)
        for item in content if content is not None else [pools_bet_row()]:
            self.add_item(item)
        bind_layout(self, "casino:pools_over", self._bet_over)
        bind_layout(self, "casino:pools_under", self._bet_under)

    async def _bet_over(self, interaction: discord.Interaction) -> None:
        cog = await _dispatch_or_apologize(interaction)
        if cog is not None:
            await cog.open_pools_bet(interaction, pools_logic.OVER)

    async def _bet_under(self, interaction: discord.Interaction) -> None:
        cog = await _dispatch_or_apologize(interaction)
        if cog is not None:
            await cog.open_pools_bet(interaction, pools_logic.UNDER)
