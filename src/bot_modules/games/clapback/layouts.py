"""Components V2 card builders for the Clapback cog.

Replaces ``games_clapback/embeds.py`` (deleted with this conversion —
``docs/plans/components_v2_migration.md`` wave 3b, "games"). These
functions accept plain dicts/primitives and return a list of
``discord.ui.Item``. They never call out to Discord — testable with no
network and no mocks of the Bot/Guild API.

Six builders, one per card the game posts:

* :func:`build_lobby_layout` — the join lobby. **Paradigm 8 — live board**:
  rebuilt and re-edited in place on every Join/Leave, and again with a
  ``notice`` when the lobby times out or is cancelled.
* :func:`build_submit_layout` — the round's prompt card. **Paradigm 9 —
  game turn card**, and a live board: the submit loop redraws it as the
  countdown and the answer count move, and with Live Game Boards on a
  sticky repost rebuilds it from the view.
* :func:`build_vote_layout` — one head-to-head matchup. **Paradigm 9.**
  The two vote buttons carry the answers themselves
  (``logic.vote_button_label``).
* :func:`build_reveal_layout` — what the matchup card edits into once the
  vote closes. **Paradigm 7 — receipt**: the buttons are gone.
* :func:`build_scoreboard_layout` — between rounds, and once more after the
  last. **Paradigm 7**, with an optional ▶️ Next Round row for the host.
* :func:`build_recap_layout` — the final results, with the Play Again row.
  **Paradigm 7.**

Buttons carry their frozen ``ql_*`` custom_ids and no callbacks — the cog's
views bind them after adding the items (guide § Code conventions).

Names come in through ``name_fn`` already rendered (and markdown-escaped by
the resolver the cog builds), so no builder escapes a name itself. A
player's *answer* is untrusted text, so every builder escapes it — the old
recap embed quoted the best answer raw, and a ``*`` in it broke the italics.

There is no colour. The old embeds painted a decided matchup green on
purpose; a bare card has no bar to paint, so the 🏆 and 💀 lines carry the
outcome, as they always had to for a phone with the bar off-screen.
"""

from __future__ import annotations

from typing import Any

import discord

from bot_modules.core.layout import lobby_art
from bot_modules.games.constants import GAME_ICONS
from bot_modules.games.clapback.logic import (
    MIN_PLAYERS,
    THREE_PLAYER_NOTE,
    board_scores,
    find_best_answer_record,
    find_closest_matchup_record,
    vote_button_label,
)
from bot_modules.shared.name_resolver import NameFn, mention

ICON = GAME_ICONS["clapback"]

#: How many joiners the lobby names before it summarises the rest.
LOBBY_NAMES_SHOWN = 10

#: The medals the scoreboards hand the top three.
MEDALS = ("🥇", "🥈", "🥉")

#: The game's name, as the lobby heading prints it and as the bot draws it
#: into a lobby header.
LOBBY_TITLE = "Clapback"

# Frozen custom_ids: posted cards and restart recovery route by these.
JOIN_ID = "ql_join"
LEAVE_ID = "ql_leave"
START_ID = "ql_start"
HELP_ID = "ql_htp"
CANCEL_ID = "ql_cancel"
SUBMIT_ID = "ql_submit"
JOIN_NOW_ID = "ql_join_midgame"
CLOSE_ANSWERS_ID = "ql_close_answers"
VOTE_A_ID = "ql_vote_a"
VOTE_B_ID = "ql_vote_b"
NEXT_ROUND_ID = "ql_next"
REPLAY_ID = "ql_replay"
REPLAY_SHUFFLED_ID = "ql_replay_shuffle"


def _text(content: str) -> discord.ui.TextDisplay[Any]:
    return discord.ui.TextDisplay(content)


def _button(
    label: str, custom_id: str, style: discord.ButtonStyle = discord.ButtonStyle.secondary,
) -> discord.ui.Button[Any]:
    return discord.ui.Button(label=label, style=style, custom_id=custom_id)


def _quote(answer: str) -> str:
    """A player's answer, escaped and italicised in quotes."""
    return f'*"{discord.utils.escape_markdown(answer)}"*'


def _byes(bye_players: Any) -> list[Any]:
    """A round normally benches at most one player, but a missing submitter
    can leave an odd number of answers and force a second, late bye — so the
    scoreboard takes a list. A bare id still works for older records."""
    if not bye_players:
        return []
    if isinstance(bye_players, (list, tuple)):
        return list(bye_players)
    return [bye_players]


def build_lobby_layout(
    host_name: str,
    config: dict[str, Any],
    players: list[int],
    name_fn: NameFn = mention,
    start_at: int | None = None,
    notice: str | None = None,
    *,
    header_url: str | None = None,
    footer_url: str | None = None,
    title_in_header: bool = False,
) -> list[discord.ui.Item[Any]]:
    """Build the ``/games play clapback`` lobby. **Paradigm 8 — live board.**

    Names the first :data:`LOBBY_NAMES_SHOWN` joiners with a ``(+N more)``
    tail past that. ``start_at`` is the countdown the start-ping sweep keeps
    (clapback-8): it starts the game itself when it runs out, so the line
    promises that rather than advertising a time. At exactly three players
    the three-player note says what that floor means while a fourth can
    still be found (clapback-7).

    ``notice`` is the line a retired lobby carries — timed out or cancelled
    — above the (by then disabled) buttons.

    ``header_url``/``footer_url`` are the guild's lobby header and panel
    footer (``resolve_panel_art_conn``): the strip tops the card and the
    footer closes it below the buttons, both only when a header is set
    (``core.layout.lobby_art``). ``title_in_header`` says the header already
    wears :data:`LOBBY_TITLE` between its leaves, so the ``##`` heading goes
    — it would say the name twice — and the header's alt text carries it.
    """
    titled = bool(header_url and title_in_header)
    top, bottom = lobby_art(
        header_url, footer_url, title=LOBBY_TITLE if titled else None
    )
    head = [] if titled else [f"## {ICON} {LOBBY_TITLE}"]
    head += [
        "Join the battle of wits! Write the funniest answer to each prompt, "
        "then vote head-to-head.",
        f"Host: **{host_name}** · Rounds: **{config['rounds']}**",
    ]
    if start_at:
        head.append(
            f"⏰ Starting <t:{start_at}:R> — on its own, once {MIN_PLAYERS} have joined"
        )
    items: list[discord.ui.Item[Any]] = [*top, _text("\n".join(head))]

    if not players:
        roster = "(nobody yet)"
    else:
        roster = ", ".join(name_fn(int(uid)) for uid in players[:LOBBY_NAMES_SHOWN])
        if len(players) > LOBBY_NAMES_SHOWN:
            roster += f" (+{len(players) - LOBBY_NAMES_SHOWN} more)"
    players_text = f"### Players ({len(players)})\n{roster}"
    if len(players) == 3:
        players_text += f"\n{THREE_PLAYER_NOTE}"
    items.append(discord.ui.Separator(spacing=discord.SeparatorSpacing.small))
    items.append(_text(players_text))

    if notice:
        items.append(_text(notice))
    items.append(
        discord.ui.ActionRow(
            _button("Join", JOIN_ID, discord.ButtonStyle.success),
            _button("Leave", LEAVE_ID),
            _button("Start", START_ID, discord.ButtonStyle.primary),
            _button("❓ Help", HELP_ID),
            _button("Cancel", CANCEL_ID),
        )
    )
    items.extend(bottom)
    return items


def build_submit_layout(
    prompt: str,
    round_num: int,
    total_rounds: int,
    deadline_str: str,
    answers_in: int,
    total_players: int,
    bye_player: Any = None,
    name_fn: NameFn = mention,
) -> list[discord.ui.Item[Any]]:
    """Build the per-round prompt card. **Paradigm 9 — game turn card.**

    ``deadline_str`` is pre-formatted by the cog (the opening
    ``format_deadline`` timestamp, then the loop's ``⏰ 1:30`` ticks, then
    ``⏱️ Closed``), so the builder stays free of timer imports.

    ``bye_player`` is whoever sits this round out, decided before the prompt
    goes out and named here up front — half of the fix for players
    discovering they had sat out only once the round was over.
    ``total_players`` already excludes them.
    """
    items: list[discord.ui.Item[Any]] = [
        _text(
            f"## {ICON} Clapback — Round {round_num}/{total_rounds}\n"
            f'**"{discord.utils.escape_markdown(prompt)}"**'
        ),
        _text(f"{deadline_str} · Answers in: **{answers_in}/{total_players}**"),
    ]
    if bye_player is not None:
        items.append(
            _text(
                f"🪑 **{name_fn(int(bye_player))}** is sitting out — odd number "
                f"of players, so no answer needed this round. You're paid the "
                f"round's average and you can still vote."
            )
        )
    items.append(
        discord.ui.ActionRow(
            _button("✏️ Submit", SUBMIT_ID, discord.ButtonStyle.primary),
            _button("🙋 Join now", JOIN_NOW_ID),
            _button("🔒 Close answers", CLOSE_ANSWERS_ID),
        )
    )
    return items


def build_vote_layout(
    answer_a: str,
    answer_b: str,
    round_num: int,
    matchup_index: int,
    total_matchups: int,
    deadline_str: str,
    vote_count: int = 0,
    prompt: str | None = None,
) -> list[discord.ui.Item[Any]]:
    """Build the head-to-head card for one matchup. **Paradigm 9.**

    The round's prompt leads when supplied, so a spectator sees what was
    being answered. The buttons say the answers themselves — voters were
    mapping "the left one" back to an answer and getting it wrong.
    """
    head = f"## {ICON} Head to Head — Round {round_num}, Matchup {matchup_index + 1}/{total_matchups}"
    if prompt:
        head += f'\n💬 **"{discord.utils.escape_markdown(prompt)}"**'
    items: list[discord.ui.Item[Any]] = [
        _text(head),
        _text(f"🅰️ {_quote(answer_a)}\n\n⚔️ **VS** ⚔️\n\n🅱️ {_quote(answer_b)}"),
        _text(f"{deadline_str} · Votes: **{vote_count}**"),
        discord.ui.ActionRow(
            _button(vote_button_label("🅰️", answer_a), VOTE_A_ID, discord.ButtonStyle.primary),
            _button(vote_button_label("🅱️", answer_b), VOTE_B_ID, discord.ButtonStyle.primary),
        ),
    ]
    return items


def build_reveal_layout(
    result: dict[str, Any],
    answers: dict[str, str],
    player_a: int,
    player_b: int,
    anonymous: bool,
    name_fn: NameFn,
    prompt: str | None = None,
) -> list[discord.ui.Item[Any]]:
    """Build what a matchup card becomes once voting closes. **Paradigm 7.**

    Three outcomes:

    - **clapback** (unanimous, 2+ votes): "Clapback!" heading, the winner and
      the defeated answer, and the celebration line.
    - **tie** (no winner): both answers at 50%.
    - **regular win**: winner and loser with their vote percentages.

    ``anonymous`` hides both authors as ``???`` and skips ``name_fn``.
    """
    vc = result["vote_counts"]
    total_v = vc[player_a] + vc[player_b]

    def _name(uid: int) -> str:
        return "???" if anonymous else name_fn(uid)

    def _answer(uid: int) -> str:
        return _quote(answers.get(str(uid), "???"))

    prompt_line = (
        f'\n💬 *"{discord.utils.escape_markdown(prompt)}"*' if prompt else ""
    )

    if result["winner"] is None:
        body = (
            f"🤝 {_answer(player_a)} — {vc[player_a]} votes (50%)\n"
            f"by **{_name(player_a)}** (+50 pts)\n\n"
            f"🤝 {_answer(player_b)} — {vc[player_b]} votes (50%)\n"
            f"by **{_name(player_b)}** (+50 pts)"
        )
        return [
            _text(f"## {ICON} Matchup Result — Tie!{prompt_line}"),
            _text(body),
        ]

    winner_id = result["winner"]
    loser_id = player_b if winner_id == player_a else player_a
    w_pts = result["scores"][winner_id]
    l_pts = result["scores"][loser_id]

    if result["clapback"]:
        items: list[discord.ui.Item[Any]] = [
            _text(f"## {ICON} Clapback!{prompt_line}"),
            _text(
                f"### 🏆 Winner\n{_answer(winner_id)} — "
                f"{vc[winner_id]}/{total_v} votes (100%)\n"
                f"by **{_name(winner_id)}** (+{w_pts} pts!)"
            ),
            _text(
                f"### 💀 Defeated\n{_answer(loser_id)} — 0 votes (0%)\n"
                f"by **{_name(loser_id)}** (+{l_pts} pts)"
            ),
            _text(f"**{_name(winner_id)}** got a CLAPBACK! 🎉"),
        ]
        return items

    w_pct = round((vc[winner_id] / total_v) * 100) if total_v else 0
    l_pct = 100 - w_pct
    return [
        _text(f"## {ICON} Matchup Result{prompt_line}"),
        _text(
            f"### 🏆 Winner\n{_answer(winner_id)} — {vc[winner_id]} votes ({w_pct}%)\n"
            f"by **{_name(winner_id)}** (+{w_pts} pts)"
        ),
        _text(
            f"### 💀 Runner-up\n{_answer(loser_id)} — {vc[loser_id]} votes ({l_pct}%)\n"
            f"by **{_name(loser_id)}** (+{l_pts} pts)"
        ),
    ]


def _standings(
    payload: dict[str, Any], name_fn: NameFn, *, clapbacks: dict[str, int] | None = None,
    withdrawn_tail: str,
) -> str:
    """The ranked scoreboard lines, then any leaver's withdrawn score.

    A leaver's score is withdrawn from the standings (clapback-17): shown,
    unranked and struck through, so the board never crowns someone who
    walked out.
    """
    sorted_scores, withdrawn = board_scores(payload)
    lines = []
    for i, (pid, pts) in enumerate(sorted_scores):
        prefix = MEDALS[i] if i < len(MEDALS) else f"{i + 1}."
        tally = ""
        if clapbacks is not None:
            count = clapbacks.get(pid, 0)
            if count:
                tally = f" ({count} CLAPBACK{'S' if count != 1 else ''})"
        lines.append(f"{prefix} **{name_fn(int(pid))}** — **{pts}** pts{tally}")
    for pid, pts in withdrawn:
        lines.append(f"🚪 ~~{name_fn(int(pid))}~~ — {pts} pts ({withdrawn_tail})")
    return "\n".join(lines)


def build_scoreboard_layout(
    payload: dict[str, Any],
    round_num: int,
    total_rounds: int,
    bye_players: Any = None,
    bye_award: int | None = None,
    name_fn: NameFn = mention,
    with_next: bool = False,
) -> list[discord.ui.Item[Any]]:
    """Build the between-round (or final) scoreboard. **Paradigm 7.**

    Players are ranked highest-first with medals for the top three.
    ``bye_award`` is what a bye player was actually paid this round (the
    field's average); it falls back to 50 for round records written before
    the award became dynamic. ``with_next`` adds the host's ▶️ Next Round
    row — the round summary between rounds carries it, the final board
    before the recap doesn't.
    """
    items: list[discord.ui.Item[Any]] = [
        _text(
            f"## {ICON} Round {round_num} Complete\n### 📊 Scoreboard\n"
            + (_standings(payload, name_fn, withdrawn_tail="left the game") or "No scores yet")
        )
    ]
    byes = _byes(bye_players)
    if byes:
        who = ", ".join(f"**{name_fn(int(b))}**" for b in byes)
        pts = 50 if bye_award is None else bye_award
        each = " each" if len(byes) > 1 else ""
        heading = "Bye" if len(byes) == 1 else "Byes"
        items.append(
            _text(
                f"### 🪑 {heading}\n{who} sat this round out — **+{pts}** pts{each} "
                f"(the round's average)"
            )
        )
    remaining = total_rounds - round_num
    items.append(
        _text(
            f"-# {remaining} round(s) remaining!" if remaining > 0 else "-# Final round complete!"
        )
    )
    if with_next:
        items.append(
            discord.ui.ActionRow(
                _button("▶️ Next Round", NEXT_ROUND_ID, discord.ButtonStyle.primary)
            )
        )
    return items


def build_recap_layout(
    payload: dict[str, Any],
    config: dict[str, Any],
    name_fn: NameFn,
    payout_line: str | None = None,
) -> list[discord.ui.Item[Any]]:
    """Build the final results card, with the Play Again row. **Paradigm 7.**

    - Winner: the highest scorer still in (a leaver's score is withdrawn,
      clapback-17), or "Nobody" with no scores.
    - Final scoreboard with medals and CLAPBACK counts.
    - Best Single Answer (highest vote share) — skipped when no matchup hit
      the 3-vote floor; ``anonymous`` hides its author as ``???``.
    - Closest Matchup — skipped when no matchup drew a vote.
    - The game's total clapbacks, only when there were any.
    - ``payout_line`` (``economy.game_rewards.payout_footer_line``) as the
      card's ``-#`` subtext.
    """
    clapbacks = payload.get("clapbacks", {})
    round_history = payload.get("round_history", [])
    players = payload.get("players", [])
    anonymous = config.get("anonymous", False)

    sorted_scores, _withdrawn = board_scores(payload)
    if sorted_scores:
        winner = f"🏆 Winner: **{name_fn(int(sorted_scores[0][0]))}** — **{sorted_scores[0][1]}** pts"
    else:
        winner = "🏆 Winner: **Nobody**"

    items: list[discord.ui.Item[Any]] = [
        _text(
            f"## {ICON} Clapback — Final Results\n"
            f"Rounds: **{len(round_history)}** · Players: **{len(players)}**\n"
            f"{winner}"
        ),
        discord.ui.Separator(spacing=discord.SeparatorSpacing.small),
        _text(
            "### 📊 Final Scoreboard\n"
            + (
                _standings(
                    payload, name_fn, clapbacks=clapbacks,
                    withdrawn_tail="left mid-game; score withdrawn",
                )
                or "—"
            )
        ),
    ]

    best = find_best_answer_record(round_history)
    if best is not None:
        author = "???" if anonymous else name_fn(int(best["author"]))
        items.append(
            _text(
                f"### ⚡ Best Single Answer\n{_quote(best['text'])} by **{author}**\n"
                f"Round {best['round']} — {round(best['pct'] * 100)}% of votes"
            )
        )

    closest = find_closest_matchup_record(round_history)
    if closest is not None:
        m = closest["matchup"]
        items.append(
            _text(
                f"### 🤣 Closest Matchup\n{_quote(m['answer_a'])} vs {_quote(m['answer_b'])}\n"
                f"{m['votes_a']}–{m['votes_b']} in Round {closest['round']}"
            )
        )

    total_clapbacks = sum(clapbacks.values())
    if total_clapbacks:
        items.append(_text(f"⚡ Total Clapbacks: **{total_clapbacks}**"))
    if payout_line:
        items.append(_text(f"-# {payout_line}"))

    items.append(
        discord.ui.ActionRow(
            _button("🔁 Play Again", REPLAY_ID, discord.ButtonStyle.primary),
            _button("🔀 Play Again (Shuffled)", REPLAY_SHUFFLED_ID),
        )
    )
    return items
