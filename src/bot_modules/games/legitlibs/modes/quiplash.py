from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

import discord

from bot_modules.core.layout import card
from bot_modules.games.utils.game_manager import (
    create_game, update_game_message, update_game_state,
    modify_payload, get_game_payload, end_game, update_session,
)
from bot_modules.games.utils.lobby_art import read_lobby_art
from bot_modules.games.utils.names import build_names, plain_name
from bot_modules.games.utils.send_retry import retry_transient
from ..data import (
    pick_template, mark_template_used, get_prompts, get_channel_max_tier,
    HEAT_LABELS,
)
from ..layouts import (
    JOIN_ID, LEAVE_ID, LOBBY_HELP_ID, SUBMIT_ID, FILL_HELP_ID, TITLE,
    build_cancelled_layout, build_cast_layout, build_lobby_layout,
    build_no_submissions_layout, build_quiplash_fill_layout, build_reveal_layout,
)
from ..quiplash_logic import (
    add_player as ql_add_player,
    build_initial_payload as ql_build_initial_payload,
    claim_start as ql_claim_start,
    clamp_tier as ql_clamp_tier,
    collect_complete_submissions as ql_collect_complete_submissions,
    get_prior_submission as ql_get_prior_submission,
    payout_roster as ql_payout_roster,
    remove_player as ql_remove_player,
    shuffle_reveal_order as ql_shuffle_reveal_order,
    store_submission as ql_store_submission,
    submitted_count as ql_submitted_count,
)
from ..rendering import render_filled_body, render_redacted_body
from ..modals import make_fill_modal
from ..validation import lobby_is_full, player_range
from ..views import LibsCard, send_help

log = logging.getLogger(__name__)

# Was five minutes; the loop already ends early once everyone has submitted,
# and a shorter clock keeps the room's attention for the reveal and the next
# story (trivia-tail-90).
FILL_TIMEOUT = 120  # seconds players have to submit


def _build_cast_lines(names, uid_list: list[str]) -> list[str]:
    """Render the "Who Wrote What" cast-reveal lines for a completed round.

    ``plain_name`` is unescaped, and this list lands in a
    Text Display, so a markdown nickname must be escaped here just like
    ``classic.py``'s parallel contributor list is.
    """
    return [
        f"**#{i}** — {discord.utils.escape_markdown(plain_name(names, int(uid)))}"
        for i, uid in enumerate(uid_list, 1)
    ]


async def run_quiplash(cog, *, channel, guild, host_id: int, host_name: str,
                       tier: int, template_id: str | None, tag: str | None) -> str | None:
    """Entry point for a Quiplash-mode LegitLibs round (interaction-free)."""
    db = cog.db

    # Enforce per-channel tier cap
    max_tier = await get_channel_max_tier(db, channel.id)
    tier, clamped = ql_clamp_tier(tier, max_tier)
    if clamped:
        log.info(
            "legitlibs quiplash: tier clamped to %s (%s) in channel %s",
            max_tier, HEAT_LABELS[max_tier], channel.id,
        )

    # Pick a template
    prompts = await get_prompts(db)
    template = await pick_template(db, guild.id, tier, tag=tag, template_id=template_id)
    if not template:
        log.info(
            "legitlibs quiplash: no published templates for tier %s tag %r in channel %s",
            tier, tag, channel.id,
        )
        return None

    blanks = template["blanks"]
    # Everyone fills every blank, so the blank-derived cap is a Classic fact
    # that made every short template a one-player lobby here (trivia-tail-83).
    lobby_min, lobby_max = player_range(template, "quiplash")

    # Create game record
    game_id = await create_game(
        db, channel.id, host_id, "legitlibs",
        state="joining",
        payload=ql_build_initial_payload(host_id, tier, template),
    )
    cog._game_canceled.discard(game_id)

    # The fill card, once posted — the cancel path edits it into the notice.
    fill_msg: discord.Message | None = None

    # ── Join phase ──────────────────────────────────────────────────────────
    # The guild's lobby art, read once here and reused on every redraw.
    art = await read_lobby_art(cog.bot, channel, TITLE)

    def _lobby_items(player_count: int, *, closed: bool = False):
        return build_lobby_layout(
            host_name, template["title"], tier, "quiplash", player_count, lobby_min,
            closed=closed, header_url=art.header, footer_url=art.footer,
            title_in_header=art.title_in_header,
        )

    async def _redraw_lobby(interaction: discord.Interaction, player_count: int) -> None:
        join_view.render(_lobby_items(player_count))
        assert interaction.message is not None
        try:
            await interaction.message.edit(view=join_view)
        except discord.HTTPException:
            pass

    async def handle_join(action_interaction: discord.Interaction) -> None:
        log.info("%s pressed 'Join' in #%s", action_interaction.user.display_name, getattr(action_interaction.channel, "name", "?"))
        payload = await get_game_payload(db, game_id)
        if payload.get("state") != "joining":
            await action_interaction.response.send_message("The round has already started.", ephemeral=True)
            return
        uid = action_interaction.user.id
        if uid in payload["players"]:
            await action_interaction.response.send_message("You're already in!", ephemeral=True)
            return
        if lobby_is_full(payload["players"], lobby_max):
            await action_interaction.response.send_message(
                f"This round is full — **{lobby_max}** players are already in.",
                ephemeral=True,
            )
            return
        def _add(p):
            ql_add_player(p, uid)
        payload = await modify_payload(db, game_id, _add)
        await action_interaction.response.send_message("✅ You joined!", ephemeral=True)
        await _redraw_lobby(action_interaction, len(payload["players"]))

    async def handle_leave(action_interaction: discord.Interaction) -> None:
        """The embed era's Leave went nowhere in this mode — the handler had
        no branch for it, so the press failed silently."""
        log.info("%s pressed 'Leave' in #%s", action_interaction.user.display_name, getattr(action_interaction.channel, "name", "?"))
        payload = await get_game_payload(db, game_id)
        if payload.get("state") != "joining":
            await action_interaction.response.send_message("The round has already started.", ephemeral=True)
            return
        uid = action_interaction.user.id
        if uid == host_id:
            await action_interaction.response.send_message(
                "You're the host! If you leave, the round is cancelled — use "
                "**Cancel Round** on your host panel instead.", ephemeral=True,
            )
            return
        if uid not in payload["players"]:
            await action_interaction.response.send_message("You're not in this round.", ephemeral=True)
            return
        def _remove(p):
            ql_remove_player(p, uid)
        payload = await modify_payload(db, game_id, _remove)
        await action_interaction.response.send_message("You've left.", ephemeral=True)
        await _redraw_lobby(action_interaction, len(payload["players"]))

    async def handle_start(action_interaction: discord.Interaction) -> None:
        payload = await get_game_payload(db, game_id)
        if payload.get("state") != "joining":
            await action_interaction.response.send_message("The round has already started.", ephemeral=True)
            return
        if len(payload["players"]) < lobby_min:
            await action_interaction.response.send_message(
                f"Need at least {lobby_min} players to start.", ephemeral=True
            )
            return
        claimed = False
        def _claim_start(p):
            nonlocal claimed
            claimed = ql_claim_start(p)
        payload = await modify_payload(db, game_id, _claim_start)
        if not claimed:
            await action_interaction.response.send_message("Round already started.", ephemeral=True)
            return
        await action_interaction.response.defer()
        join_view.close_phase()
        join_view.render(_lobby_items(len(payload["players"]), closed=True))
        try:
            await msg.edit(view=join_view)
        except discord.HTTPException:
            pass
        await _run_fill_phase(payload)

    async def handle_cancel(action_interaction: discord.Interaction) -> None:
        cog._game_canceled.add(game_id)
        await end_game(db, game_id)
        cog._game_canceled.discard(game_id)
        join_view.close_phase()
        cog.bot.active_views.pop(game_id, None)
        try:
            await msg.edit(view=card(build_cancelled_layout()))
        except discord.HTTPException:
            pass

    join_view = LibsCard(
        game_id, host_id, _lobby_items(1),
        {JOIN_ID: handle_join, LEAVE_ID: handle_leave, LOBBY_HELP_ID: send_help},
        actions={"start": handle_start, "cancel": handle_cancel},
        cancel_prompt="Cancel this round? The lobby will close.",
    )
    try:
        msg = await retry_transient(
            lambda: channel.send(view=join_view),
            label="legitlibs quiplash launch",
        )
    except (discord.Forbidden, discord.DiscordServerError):
        await end_game(db, game_id)
        cog.bot.active_views.pop(game_id, None)
        log.warning("legitlibs launch lacked send perms in channel %s", channel.id)
        return None
    await update_game_message(db, game_id, msg.id)
    cog.bot.active_views[game_id] = join_view

    # ── Fill phase ──────────────────────────────────────────────────────────
    async def _run_fill_phase(payload: dict):
        nonlocal fill_msg
        await update_game_state(db, game_id, "filling")
        # NOTE: claim_start already set state="filling" in payload; this is
        # belt-and-braces in case some other code path lands here without it.
        def _set_filling(p):
            p["state"] = "filling"
        await modify_payload(db, game_id, _set_filling)

        player_ids = payload["players"]
        deadline = int((datetime.now(timezone.utc).timestamp()) + FILL_TIMEOUT)
        redacted = render_redacted_body(template["body"], blanks)

        def _fill_items(submitted: int, *, closed: bool = False):
            return build_quiplash_fill_layout(
                template["title"], tier, len(player_ids), submitted, deadline, redacted,
                closed=closed,
            )

        fill_view = LibsCard(
            game_id, host_id, _fill_items(0),
            {SUBMIT_ID: _handle_submit_press, FILL_HELP_ID: send_help},
            actions={"cancel": _handle_fill_cancel},
            cancel_prompt="Cancel this round? Players' in-progress fills will be lost.",
        )
        cog.bot.active_views[game_id] = fill_view

        # Local alias: pyright can't narrow the nonlocal ``fill_msg`` binding.
        fill_msg = sent_msg = await retry_transient(
            lambda: channel.send(view=fill_view),
            label="legitlibs quiplash fill card",
        )
        await update_game_message(db, game_id, sent_msg.id)

        await update_session(db, channel.id, game_id, player_ids)

        # Wait for timer, updating the counter periodically
        elapsed = 0
        while elapsed < FILL_TIMEOUT:
            await asyncio.sleep(15)
            elapsed += 15
            if game_id in cog._game_canceled:
                return

            cur_payload = await get_game_payload(db, game_id)
            if cur_payload.get("state") != "filling":
                return

            submitted = ql_submitted_count(cur_payload, player_ids)
            fill_view.render(_fill_items(submitted))
            try:
                await sent_msg.edit(view=fill_view)
            except discord.HTTPException:
                pass

            # Early exit if everyone submitted
            if submitted >= len(player_ids):
                break

        if game_id in cog._game_canceled:
            return

        # Close the fill card: the closed frame carries no controls.
        fill_view.close_phase()
        cur_payload = await get_game_payload(db, game_id)
        fill_view.render(_fill_items(ql_submitted_count(cur_payload, player_ids), closed=True))
        try:
            await sent_msg.edit(view=fill_view)
        except discord.HTTPException:
            pass

        await _run_reveal_phase()

    async def _handle_submit_press(submit_interaction: discord.Interaction):
        log.info("%s pressed 'Submit Fills' in #%s", submit_interaction.user.display_name, getattr(submit_interaction.channel, "name", "?"))
        cur_payload = await get_game_payload(db, game_id)
        if cur_payload.get("state") != "filling":
            await submit_interaction.response.send_message("The fill phase is over.", ephemeral=True)
            return

        uid = submit_interaction.user.id
        if uid not in cur_payload["players"]:
            await submit_interaction.response.send_message("You're not in this round.", ephemeral=True)
            return

        prior_fills, had_complete = ql_get_prior_submission(cur_payload, uid)

        async def _save_fills(sub_interaction: discord.Interaction, fills: dict, partial: bool):
            saved = False
            def _store(p):
                nonlocal saved
                saved = ql_store_submission(p, uid, fills, partial)
            await modify_payload(db, game_id, _store)
            if saved:
                msg = "✅ Fills updated!" if had_complete else "✅ Fills saved! You can resubmit to tweak them."
                await sub_interaction.response.send_message(msg, ephemeral=True)
            else:
                await sub_interaction.response.send_message("The fill phase has ended — your fills were not saved.", ephemeral=True)

        modal = make_fill_modal(game_id, db, prompts, blanks, tier, _save_fills, existing_fills=prior_fills)
        await submit_interaction.response.send_modal(modal)

    async def _handle_fill_cancel(cancel_interaction: discord.Interaction):
        cog._game_canceled.add(game_id)
        cur_payload = await get_game_payload(db, game_id)
        await end_game(db, game_id, player_count=len(cur_payload.get("players", [])))
        cog._game_canceled.discard(game_id)
        view = cog.bot.active_views.pop(game_id, None)
        if view:
            view.stop()
        # The fill card becomes the cancelled notice.
        if fill_msg is not None:
            try:
                await fill_msg.edit(view=card(build_cancelled_layout()))
            except discord.HTTPException:
                pass

    # ── Reveal phase ────────────────────────────────────────────────────────
    async def _run_reveal_phase():
        cur_payload = await get_game_payload(db, game_id)
        player_ids = cur_payload.get("players")
        if not player_ids:
            return
        submissions = cur_payload.get("submissions", {})

        # Collect complete (non-partial) submissions
        complete = ql_collect_complete_submissions(submissions)
        # The reveal pays whoever put a story in, not everyone who pressed
        # Join (trivia-tail-95); the history row still counts the roster.
        paid_ids = ql_payout_roster(complete)
        last_msg: discord.Message | None = None
        last_items = None

        try:
            if not complete:
                last_items = build_no_submissions_layout(template["title"])
                last_msg = await channel.send(view=card(last_items))
            else:
                await update_game_state(db, game_id, "revealing")

                # Shuffle order for anonymous reveal
                uid_list = ql_shuffle_reveal_order(list(complete.keys()))

                total = len(uid_list)

                if total == 1:
                    # Single submission — reveal attributed immediately
                    uid = uid_list[0]
                    fills = complete[uid]["fills"]
                    filled = render_filled_body(template["body"], blanks, fills)
                    last_items = build_reveal_layout(
                        template["title"], tier, filled, 1, 1, by=plain_name(await build_names(cog.bot, guild, [int(uid)]), int(uid)),
                    )
                    last_msg = await channel.send(view=card(last_items))
                else:
                    await channel.send(f"**Revealing {total} submissions…**")
                    for i, uid in enumerate(uid_list, 1):
                        fills = complete[uid]["fills"]
                        filled = render_filled_body(template["body"], blanks, fills)
                        await channel.send(view=card(build_reveal_layout(
                            template["title"], tier, filled, i, total,
                        )))
                        await asyncio.sleep(3)

                    # Cast reveal — show who wrote which submission.
                    last_items = build_cast_layout(_build_cast_lines(
                        await build_names(cog.bot, guild, [int(u) for u in uid_list]), uid_list,
                    ))
                    last_msg = await channel.send(view=card(last_items))

                await mark_template_used(db, guild.id, template["template_id"])
        finally:
            await end_game(db, game_id, player_count=len(player_ids), round_count=1,
                           bot=cog.bot, player_ids=paid_ids)
            cog.bot.active_views.pop(game_id, None)
            cog._game_canceled.discard(game_id)
        # Only once the round is over and the channel is free: the button
        # goes through the launch guard, which would refuse a busy channel.
        if last_msg is not None and last_items is not None:
            await cog.offer_another(
                last_msg, last_items, host_id, {"mode": "quiplash", "tier": tier, "tag": tag},
            )

    # Lobby is live; the round advances via button presses (defined above).
    return game_id
