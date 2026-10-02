"""``is_age_gated_channel`` — Discord's age gate at channel and category level."""
from __future__ import annotations

import pytest

from bot_modules.shared.age_gate import is_age_gated_channel


class _FakeNsfwChannel:
    def __init__(self, nsfw: bool, category: object = None) -> None:
        self._nsfw = nsfw
        self.category = category

    def is_nsfw(self) -> bool:
        return self._nsfw


class _RaisingChannel:
    def is_nsfw(self) -> bool:
        raise RuntimeError("partial channel object")


class _ChannelWithRaisingCategory:
    """A thread whose parent isn't cached — ``Thread.category`` raises."""

    def is_nsfw(self) -> bool:
        return False

    @property
    def category(self):
        raise RuntimeError("Parent channel not found")


@pytest.mark.parametrize(
    ("channel", "expected"),
    [
        pytest.param(_FakeNsfwChannel(True), True, id="gated"),
        pytest.param(_FakeNsfwChannel(False), False, id="ungated"),
        pytest.param(object(), False, id="no-is_nsfw-attribute"),
        pytest.param(_RaisingChannel(), False, id="partial-object-raises"),
        pytest.param(
            _FakeNsfwChannel(False, category=_FakeNsfwChannel(True)),
            True,
            id="ungated-channel-in-gated-category",
        ),
        pytest.param(
            _FakeNsfwChannel(False, category=_FakeNsfwChannel(False)),
            False,
            id="ungated-channel-in-ungated-category",
        ),
        pytest.param(
            _FakeNsfwChannel(True, category=_FakeNsfwChannel(False)),
            True,
            id="gated-channel-in-ungated-category",
        ),
        pytest.param(
            _FakeNsfwChannel(False, category=object()),
            False,
            id="category-without-is_nsfw",
        ),
        pytest.param(
            _ChannelWithRaisingCategory(), False, id="thread-parent-not-cached"
        ),
    ],
)
def test_is_age_gated_channel(channel, expected):
    # Defaults to False on anything unexpected: no tip offered when the age
    # gate can't be established.
    assert is_age_gated_channel(channel) is expected


@pytest.mark.parametrize(
    "channel",
    [
        pytest.param(object(), id="no-is_nsfw-attribute"),
        pytest.param(_RaisingChannel(), id="partial-object-raises"),
        pytest.param(_ChannelWithRaisingCategory(), id="thread-parent-not-cached"),
    ],
)
def test_unresolvable_gate_honours_the_callers_default(channel):
    # A caller excluding NSFW channels from something (advisor_context.can_view
    # keeping them out of /ask) needs the opposite fallback from a gate that
    # permits content in them.
    assert is_age_gated_channel(channel, default=True) is True
    assert is_age_gated_channel(channel) is False
