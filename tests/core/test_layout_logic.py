"""``core.layout`` — the Components V2 mechanics.

The budget is the part that bites: Discord enforces both ceilings, not the
library, so a card that grows past one fails at send time in production on
whichever guild happens to have the most rows. These pin the count against
hand-built views whose component totals are worked out by hand, so a
discord.py change that alters what ``walk_children`` yields shows up here
rather than as a 400 on a live card.
"""

from __future__ import annotations

import discord
import pytest

from bot_modules.core.layout import (
    MAX_ACCESSORY_LABEL,
    MAX_CHARS,
    MAX_COMPONENTS,
    ExpiringLayoutView,
    check_budget,
    disable_all,
    layout_budget,
    sample_prefix_ids,
    text_columns,
)


def _button(label: str = "Go", custom_id: str | None = "b") -> discord.ui.Button:
    return discord.ui.Button(label=label, custom_id=custom_id)


def _view(*items: discord.ui.Item) -> discord.ui.LayoutView:
    view = discord.ui.LayoutView(timeout=None)
    for item in items:
        view.add_item(item)
    return view


# ── layout_budget ───────────────────────────────────────────────────────


def test_budget_counts_a_flat_card():
    view = _view(
        discord.ui.TextDisplay("## Shop"),
        discord.ui.Separator(),
        discord.ui.TextDisplay("Nothing for sale."),
    )
    assert layout_budget(view) == (3, len("## Shop") + len("Nothing for sale."))


def test_budget_counts_a_sections_accessory():
    """A Section is itself + its children + its accessory: three here."""
    section = discord.ui.Section(
        discord.ui.TextDisplay("Palette"), accessory=_button()
    )
    assert layout_budget(_view(section)) == (3, len("Palette"))


def test_budget_counts_buttons_inside_an_action_row():
    row = discord.ui.ActionRow()
    row.add_item(_button("A", "a"))
    row.add_item(_button("B", "b"))
    assert layout_budget(_view(row)) == (3, 0)


def test_budget_counts_a_separator_as_one():
    assert layout_budget(_view(discord.ui.Separator())) == (1, 0)


def test_budget_agrees_with_discord_pys_own_total():
    """The public walk must match the private counter discord.py checks."""
    view = _view(
        discord.ui.TextDisplay("## Shop"),
        discord.ui.Section(discord.ui.TextDisplay("Row"), accessory=_button()),
        discord.ui.Separator(),
    )
    assert layout_budget(view)[0] == view._total_children


# ── check_budget ────────────────────────────────────────────────────────


def test_check_budget_passes_a_card_inside_both_ceilings():
    check_budget(_view(discord.ui.TextDisplay("x" * MAX_CHARS)))


class _OversizeCard:
    """A card past the component ceiling, which discord.py will not build.

    ``LayoutView.add_item`` counts nested children and refuses the 41st, so
    the component arm of ``check_budget`` cannot be reached through a real
    view — it is the belt to that braces, and stays because the count is
    what a builder test wants reported next to the character count. Duck
    typing is what ``check_budget`` actually needs: a ``walk_children`` and
    a ``content_length``.
    """

    def __init__(self, components: int, chars: int) -> None:
        self._components = components
        self._chars = chars

    def walk_children(self):
        return iter(range(self._components))

    def content_length(self) -> int:
        return self._chars


def test_check_budget_rejects_too_many_components():
    card = _OversizeCard(MAX_COMPONENTS + 1, 0)
    with pytest.raises(ValueError) as excinfo:
        check_budget(card)  # type: ignore[arg-type]
    assert "41 components (max 40)" in str(excinfo.value)


def test_discord_py_refuses_the_41st_component_itself():
    """Why the case above needs a stub — and a canary if that ever changes."""
    view = _view(*(discord.ui.Separator() for _ in range(MAX_COMPONENTS)))
    with pytest.raises(ValueError, match="maximum number of children"):
        view.add_item(discord.ui.Separator())


def test_check_budget_rejects_too_much_text():
    view = _view(discord.ui.TextDisplay("x" * (MAX_CHARS + 1)))
    with pytest.raises(ValueError) as excinfo:
        check_budget(view)
    assert "4001 characters" in str(excinfo.value)


def test_check_budget_names_both_ceilings_when_both_give_way():
    card = _OversizeCard(MAX_COMPONENTS + 1, MAX_CHARS + 1)
    message = str(pytest.raises(ValueError, check_budget, card).value)
    assert "components" in message and "characters" in message


def test_check_budget_names_the_view_class_so_the_error_says_which_card():
    class ShopCard(discord.ui.LayoutView):
        pass

    view = ShopCard(timeout=None)
    view.add_item(discord.ui.TextDisplay("x" * (MAX_CHARS + 1)))
    assert "ShopCard" in str(pytest.raises(ValueError, check_budget, view).value)


# ── disable_all ─────────────────────────────────────────────────────────


def test_disable_all_reaches_a_nested_accessory_and_action_row():
    accessory = _button("Buy", "buy")
    row = discord.ui.ActionRow()
    row.add_item(_button("Next", "next"))
    view = _view(
        discord.ui.Section(discord.ui.TextDisplay("Palette"), accessory=accessory),
        row,
    )
    disable_all(view)
    assert accessory.disabled and row.children[0].disabled  # type: ignore[attr-defined]


def test_disable_all_reaches_a_typed_select():
    """UserSelect does not subclass Select — the old isinstance test missed it."""
    picker = discord.ui.UserSelect(custom_id="who")
    row = discord.ui.ActionRow()
    row.add_item(picker)
    disable_all(_view(row))
    assert picker.disabled


def test_disable_all_leaves_the_layout_items_alone():
    text = discord.ui.TextDisplay("## Shop")
    disable_all(_view(text, discord.ui.Separator()))
    assert not hasattr(text, "disabled")


# ── ExpiringLayoutView ──────────────────────────────────────────────────


class _FakeMessage:
    def __init__(self) -> None:
        self.edits: list[dict] = []

    async def edit(self, **kwargs):
        self.edits.append(kwargs)


class _RefusingMessage(_FakeMessage):
    async def edit(self, **kwargs):
        raise discord.HTTPException(_Response(), "gone")


class _Response:
    status = 404
    reason = "Not Found"


def _expiring() -> tuple[ExpiringLayoutView, discord.ui.Button]:
    view = ExpiringLayoutView(timeout=1)
    accessory = _button("Buy", "buy")
    view.add_item(
        discord.ui.Section(discord.ui.TextDisplay("Palette"), accessory=accessory)
    )
    return view, accessory


@pytest.mark.asyncio
async def test_on_timeout_disables_nested_items_and_edits_the_message():
    view, accessory = _expiring()
    view.message = _FakeMessage()  # type: ignore[assignment]
    await view.on_timeout()
    assert accessory.disabled
    assert view.message.edits == [{"view": view}]  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_on_timeout_edits_only_the_view():
    """A V2 message has no content or embeds to null out, and no re-upload."""
    view, _ = _expiring()
    view.message = _FakeMessage()  # type: ignore[assignment]
    await view.on_timeout()
    assert list(view.message.edits[0]) == ["view"]  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_on_timeout_without_a_message_still_disables_in_memory():
    view, accessory = _expiring()
    await view.on_timeout()
    assert accessory.disabled


@pytest.mark.asyncio
async def test_on_timeout_swallows_a_deleted_message():
    view, accessory = _expiring()
    view.message = _RefusingMessage()  # type: ignore[assignment]
    await view.on_timeout()
    assert accessory.disabled


def test_expiring_layout_view_starts_with_no_message():
    assert ExpiringLayoutView(timeout=1).message is None


# ── sample_prefix_ids ───────────────────────────────────────────────────


def test_sample_prefix_ids_prefixes_every_nested_custom_id():
    accessory = _button("Buy", "shop:buy:palette")
    row = discord.ui.ActionRow()
    row.add_item(_button("Next", "shop:page:2"))
    sample_prefix_ids(_view(discord.ui.Section("Palette", accessory=accessory), row))
    assert accessory.custom_id == "sample:shop:buy:palette"
    assert row.children[0].custom_id == "sample:shop:page:2"  # type: ignore[attr-defined]


def test_sample_prefix_ids_is_idempotent():
    accessory = _button("Buy", "shop:buy")
    view = _view(discord.ui.Section("Palette", accessory=accessory))
    sample_prefix_ids(view)
    sample_prefix_ids(view)
    assert accessory.custom_id == "sample:shop:buy"


def test_sample_prefix_ids_leaves_a_link_button_alone():
    link = discord.ui.Button(label="Docs", url="https://example.invalid")
    row = discord.ui.ActionRow()
    row.add_item(link)
    sample_prefix_ids(_view(row))
    assert link.custom_id is None


def test_sample_prefix_ids_handles_a_templated_dynamic_item():
    """A DynamicItem's own ``custom_id`` setter only accepts a value matching
    its template (auctions' Bid button: ``econ_auction:bid:<id>``) — a plain
    ``sample:`` prefix through that setter raises. The write has to land on
    the wrapped item underneath, which discord.py itself reads from and
    which enforces nothing."""
    import re

    class _Dyn(
        discord.ui.DynamicItem[discord.ui.Button],
        template=re.compile(r"econ_auction:bid:(?P<aid>\d+)"),
    ):
        def __init__(self) -> None:
            super().__init__(
                discord.ui.Button(
                    label="Bid", custom_id="econ_auction:bid:1",
                )
            )

        async def callback(self, interaction: discord.Interaction) -> None:
            pass

    dyn = _Dyn()
    row = discord.ui.ActionRow()
    row.add_item(dyn)
    sample_prefix_ids(_view(row))
    assert dyn.custom_id == "sample:econ_auction:bid:1"


# ── text_columns ─────────────────────────────────────────────────────────


def test_text_columns_plain_ascii_is_one_column_a_character():
    assert text_columns("Set Color") == 9


def test_text_columns_an_emoji_counts_two_columns():
    # "🖌️" is U+1F58C (outside the BMP, 2 columns) + U+FE0F (a variation
    # selector, 0 columns) — this is the exact label that turned "Browse
    # Colors" (13) into the accessory that dropped under its row on Billy's
    # phone screenshot (2026-09-24) while "Set Color" (9) stayed beside it.
    assert text_columns("🖌️ Browse") == 9
    assert text_columns("Browse Colors") == 13


def test_text_columns_a_variation_selector_draws_no_width_of_its_own():
    assert text_columns("🖌️") == 2
    assert text_columns("🖌") == 2


def test_text_columns_a_zero_width_joiner_draws_nothing():
    # Not a claim that a ZWJ sequence renders as one glyph — text_columns
    # doesn't fold sequences, it only skips the joiner itself, so a real
    # ZWJ family emoji is over-counted rather than measured exactly (the
    # function's own docstring says so).
    family = "\U0001f468‍\U0001f469‍\U0001f467"  # man-ZWJ-woman-ZWJ-girl
    assert text_columns(family) == 6  # three 2-column glyphs, joiners free


def test_text_columns_symbol_category_glyph_also_counts_two():
    # unicodedata.category == "So" catches an emoji-ish symbol that's
    # inside the BMP and so wouldn't be caught by the "outside the BMP"
    # check alone (e.g. ❤ U+2764, ✅ U+2705, ↩ U+21A9 is Symbol/other too).
    assert text_columns("✅") == 2
    assert text_columns("↩️") == 2


def test_max_accessory_label_is_the_budget_the_screenshot_set():
    assert MAX_ACCESSORY_LABEL == 10
    assert text_columns("🖌️ Browse") <= MAX_ACCESSORY_LABEL
    assert text_columns("Browse Colors") > MAX_ACCESSORY_LABEL
