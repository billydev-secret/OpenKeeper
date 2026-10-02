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
from ..classic_logic import (
    add_player as cl_add_player,
    add_volunteer as cl_add_volunteer,
    build_initial_payload as cl_build_initial_payload,
    claim_start as cl_claim_start,
    clamp_tier as cl_clamp_tier,
    existing_fill_values as cl_existing_fill_values,
    filter_rescuers as cl_filter_rescuers,
    freeze_rescue as cl_freeze_rescue,
    init_rescue as cl_init_rescue,
    my_blank_ids as cl_my_blank_ids,
    remove_player as cl_remove_player,
    rescuers_done_count as cl_rescuers_done_count,
    set_rescue_fill_state as cl_set_rescue_fill_state,
    store_round1_fills as cl_store_round1_fills,
    store_rescue_fills as cl_store_rescue_fills,
)
from ..data import (
    pick_template, mark_template_used, get_prompts, get_channel_max_tier,
    HEAT_LABELS,
)
from ..distribution import (
    assign_blanks_round_robin, compute_unfilled, assign_rescue,
    players_done_count, unique_contributors,
)
from ..layouts import (
    CLASSIC_HELP_ID, CLASSIC_SUBMIT_ID, JOIN_ID, LEAVE_ID, LOBBY_HELP_ID,
    RESCUE_SUBMIT_ID, TITLE, VOLUNTEER_ID,
    build_cancelled_layout, build_classic_fill_layout, build_classic_reveal_layout,
    build_lobby_layout, build_rescue_claim_layout, build_rescue_fill_layout,
)
from ..rendering import render_filled_body_attributed
from ..modals import make_fill_modal
from ..validation import lobby_is_full, player_range
from ..views import LibsCard, send_help

log = logging.getLogger(__name__)

# Round 1 fill window. Was five minutes; the loop already ends early once
# everyone has submitted, and a shorter clock keeps the room's attention for
# the reveal and the next story (trivia-tail-90).
FILL_TIMEOUT = 120   # seconds — round 1
CLAIM_TIMEOUT = 45   # seconds — rescue claim window
RESCUE_TIMEOUT = 120 # seconds — round 2 fill
POLL_INTERVAL = 15   # seconds — counter refresh cadence


async def run_classic(cog, *, channel, guild, host_id: int, host_name: str,
                      tier: int, template_id: str | None, tag: str | None) -> str | None:
    """Entry point for a Classic-mode LegitLibs round (interaction-free)."""
    db = cog.db

    # Enforce per-channel tier cap
    max_tier = await get_channel_max_tier(db, channel.id)
    tier, clamped = cl_clamp_tier(tier, max_tier)
    if clamped:
        log.info(
            "legitlibs classic: tier clamped to %s (%s) in channel %s",
            max_tier, HEAT_LABELS[max_tier], channel.id,
        )

    # Pick a template
    prompts = await get_prompts(db)
    template = await pick_template(db, guild.id, tier, tag=tag, template_id=template_id)
    if not template:
        log.info(
            "legitlibs classic: no published templates for tier %s tag %r in channel %s",
            tier, tag, channel.id,
        )
        return None

    blanks = template["blanks"]
    # Classic keeps the template's stored range — round-robin hands out the
    # blanks, so it is a real capacity (see validation.player_range).
    lobby_min, lobby_max = player_range(template, "classic")

    # Create game record
    game_id = await create_game(
        db, channel.id, host_id, "legitlibs",
        state="joining",
        payload=cl_build_initial_payload(host_id, tier, template),
    )
    cog._game_canceled.discard(game_id)

    # Fill-phase state shared with submit/poll
    fill_msg: discord.Message | None = None
    fill_deadline: int | None = None
    fill_view_ref: LibsCard | None = None

    # Rescue-phase state
    rescue_claim_msg: discord.Message | None = None
    rescue_fill_msg: discord.Message | None = None

    # ── Join phase ──────────────────────────────────────────────────────────
    # The guild's lobby art, read once here and reused on every redraw.
    art = await read_lobby_art(cog.bot, channel, TITLE)

    def _lobby_items(player_count: int, *, closed: bool = False):
        return build_lobby_layout(
            host_name, template["title"], tier, "classic", player_count, lobby_min,
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

    async def _open_lobby(action_interaction: discord.Interaction) -> dict | None:
        payload = await get_game_payload(db, game_id)
        if payload.get("state") != "joining":
            await action_interaction.response.send_message(
                "The round has already started.", ephemeral=True)
            return None
        return payload

    async def handle_join(action_interaction: discord.Interaction) -> None:
        log.info("%s pressed 'Join' in #%s", action_interaction.user.display_name, getattr(action_interaction.channel, "name", "?"))
        payload = await _open_lobby(action_interaction)
        if payload is None:
            return
        uid = action_interaction.user.id
        if uid in payload["players"]:
            await action_interaction.response.send_message(
                "You're already in!", ephemeral=True)
            return
        if lobby_is_full(payload["players"], lobby_max):
            await action_interaction.response.send_message(
                f"This round is full — **{lobby_max}** players "
                "are already in. This template doesn't have enough blanks "
                "to give anyone else a turn.",
                ephemeral=True,
            )
            return
        def _add(p):
            cl_add_player(p, uid)
        payload = await modify_payload(db, game_id, _add)
        await action_interaction.response.send_message("✅ You joined!", ephemeral=True)
        await _redraw_lobby(action_interaction, len(payload["players"]))

    async def handle_leave(action_interaction: discord.Interaction) -> None:
        log.info("%s pressed 'Leave' in #%s", action_interaction.user.display_name, getattr(action_interaction.channel, "name", "?"))
        payload = await _open_lobby(action_interaction)
        if payload is None:
            return
        uid = action_interaction.user.id
        if uid == host_id:
            await action_interaction.response.send_message(
                "You're the host! If you leave, the game will be cancelled. "
                "Use **Cancel Round** on your host panel instead.", ephemeral=True,
            )
            return
        if uid not in payload["players"]:
            await action_interaction.response.send_message(
                "You're not in this round.", ephemeral=True)
            return
        def _remove(p):
            cl_remove_player(p, uid)
        payload = await modify_payload(db, game_id, _remove)
        await action_interaction.response.send_message("You've left.", ephemeral=True)
        await _redraw_lobby(action_interaction, len(payload["players"]))

    async def handle_start(action_interaction: discord.Interaction) -> None:
        payload = await _open_lobby(action_interaction)
        if payload is None:
            return
        if len(payload["players"]) < lobby_min:
            await action_interaction.response.send_message(
                f"Need at least {lobby_min} players to start.",
                ephemeral=True,
            )
            return
        claimed = False
        def _claim_start(p):
            nonlocal claimed
            assignments = assign_blanks_round_robin(blanks, p.get("players", []))
            claimed = cl_claim_start(p, assignments)
        payload = await modify_payload(db, game_id, _claim_start)
        if not claimed:
            await action_interaction.response.send_message(
                "Round already started.", ephemeral=True)
            return
        await action_interaction.response.defer()
        join_view.close_phase()
        join_view.render(_lobby_items(len(payload["players"]), closed=True))
        try:
            await msg.edit(view=join_view)
        except discord.HTTPException:
            pass
        await _run_fill_phase(payload)

    async def handle_join_cancel(action_interaction: discord.Interaction) -> None:
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
        actions={"start": handle_start, "cancel": handle_join_cancel},
        cancel_prompt="Cancel this round? The lobby will close.",
    )
    try:
        msg = await retry_transient(
            lambda: channel.send(view=join_view),
            label="legitlibs classic launch",
        )
    except (discord.Forbidden, discord.DiscordServerError):
        await end_game(db, game_id)
        cog.bot.active_views.pop(game_id, None)
        log.warning("legitlibs launch lacked send perms in channel %s", channel.id)
        return None
    await update_game_message(db, game_id, msg.id)
    cog.bot.active_views[game_id] = join_view

    # ── Round 1 fill phase ──────────────────────────────────────────────────
    async def _run_fill_phase(payload: dict):
        nonlocal fill_msg, fill_deadline
        await update_game_state(db, game_id, "filling")

        player_ids = payload["players"]
        assignments = payload["assignments"]
        fill_deadline = int(datetime.now(timezone.utc).timestamp()) + FILL_TIMEOUT
        deadline = fill_deadline

        def _fill_items(done: int, *, closed: bool = False):
            return build_classic_fill_layout(
                template["title"], tier, len(player_ids), done, deadline, closed=closed,
            )

        fill_view = LibsCard(
            game_id, host_id,
            _fill_items(players_done_count(assignments, payload.get("fills", {}), player_ids)),
            {CLASSIC_SUBMIT_ID: _handle_round1_submit, CLASSIC_HELP_ID: send_help},
            actions={"cancel": _handle_fill_cancel},
            cancel_prompt="Cancel this round? Players' in-progress fills will be lost.",
        )
        cog.bot.active_views[game_id] = fill_view
        nonlocal fill_view_ref
        fill_view_ref = fill_view

        # Local alias: pyright can't narrow the nonlocal `fill_msg` binding.
        fill_msg = sent_msg = await retry_transient(
            lambda: channel.send(view=fill_view),
            label="legitlibs classic round 1 fill card",
        )
        await update_game_message(db, game_id, sent_msg.id)

        await update_session(db, channel.id, game_id, player_ids)

        elapsed = 0
        while elapsed < FILL_TIMEOUT:
            await asyncio.sleep(POLL_INTERVAL)
            elapsed += POLL_INTERVAL
            if game_id in cog._game_canceled:
                return

            cur_payload = await get_game_payload(db, game_id)
            if cur_payload.get("state") != "filling":
                return

            cur_fills = cur_payload.get("fills", {})
            done = players_done_count(assignments, cur_fills, player_ids)
            fill_view.render(_fill_items(done))
            try:
                await sent_msg.edit(view=fill_view)
            except discord.HTTPException:
                pass

            if done >= len(player_ids):
                break

        if game_id in cog._game_canceled:
            return

        # Close the fill card: the closed frame carries no controls.
        fill_view.close_phase()
        cur_payload = await get_game_payload(db, game_id)
        fill_view.render(_fill_items(
            players_done_count(assignments, cur_payload.get("fills", {}), player_ids), closed=True,
        ))
        try:
            await sent_msg.edit(view=fill_view)
        except discord.HTTPException:
            pass

        unfilled = compute_unfilled(blanks, cur_payload.get("fills", {}))
        if unfilled:
            await _run_rescue_claim_phase()
        else:
            await _run_reveal_phase()

    async def _handle_round1_submit(submit_interaction: discord.Interaction):
        log.info("%s pressed 'Submit Fills' in #%s", submit_interaction.user.display_name, getattr(submit_interaction.channel, "name", "?"))
        cur_payload = await get_game_payload(db, game_id)
        if cur_payload.get("state") != "filling":
            await submit_interaction.response.send_message(
                "The fill phase is over.", ephemeral=True)
            return

        uid = submit_interaction.user.id
        if uid not in cur_payload["players"]:
            await submit_interaction.response.send_message(
                "You're not in this round.", ephemeral=True)
            return

        assignments = cur_payload.get("assignments", {})
        blank_ids = cl_my_blank_ids(assignments, uid)
        if not blank_ids:
            await submit_interaction.response.send_message(
                "You weren't assigned any blanks this round. "
                "You'll be eligible to volunteer if any go unfilled.",
                ephemeral=True,
            )
            return

        my_blanks = [b for b in blanks if b["id"] in blank_ids]

        cur_fills = cur_payload.get("fills", {})
        prior = cl_existing_fill_values(blanks, cur_fills, blank_ids)

        async def _save_fills(sub_interaction: discord.Interaction,
                              fills: dict, partial: bool):
            if partial:
                # Mid-flow page save — FillModalPage handles this internally
                # by writing to payload["submissions"]. Classic ignores that
                # path; we only write real data on the final (partial=False)
                # submit.
                return

            saved = False
            def _store(p):
                nonlocal saved
                saved = cl_store_round1_fills(p, fills, uid)
            updated_payload = await modify_payload(db, game_id, _store)
            if not saved:
                await sub_interaction.response.send_message(
                    "The fill phase has ended — your fills were not saved.",
                    ephemeral=True,
                )
                return

            had_all = all(bid in cur_fills for bid in blank_ids)
            msg_text = "✅ Fills updated!" if had_all else "✅ Fills saved!"
            await sub_interaction.response.send_message(msg_text, ephemeral=True)

            if fill_msg is not None and fill_deadline is not None \
                    and updated_payload.get("state") == "filling":
                cur_player_ids = updated_payload.get("players", [])
                done = players_done_count(
                    updated_payload.get("assignments", {}),
                    updated_payload.get("fills", {}),
                    cur_player_ids,
                )
                if fill_view_ref is not None:
                    fill_view_ref.render(build_classic_fill_layout(
                        template["title"], tier, len(cur_player_ids), done, fill_deadline,
                    ))
                    try:
                        await fill_msg.edit(view=fill_view_ref)
                    except discord.HTTPException:
                        pass

        modal = make_fill_modal(
            game_id, db, prompts, my_blanks, tier, _save_fills,
            existing_fills=prior,
        )
        await submit_interaction.response.send_modal(modal)

    async def _handle_fill_cancel(cancel_interaction: discord.Interaction):
        cog._game_canceled.add(game_id)
        cur_payload = await get_game_payload(db, game_id)
        await end_game(db, game_id, player_count=len(cur_payload.get("players", [])))
        cog._game_canceled.discard(game_id)
        view = cog.bot.active_views.pop(game_id, None)
        if view:
            view.stop()
        # Whichever phase card is current becomes the cancelled notice.
        target = rescue_fill_msg or rescue_claim_msg or fill_msg
        if target is not None:
            try:
                await target.edit(view=card(build_cancelled_layout()))
            except discord.HTTPException:
                pass

    # ── Rescue claim phase ──────────────────────────────────────────────────
    async def _run_rescue_claim_phase():
        nonlocal rescue_claim_msg
        await update_game_state(db, game_id, "rescuing_claim")
        claim_deadline_ts = int(datetime.now(timezone.utc).timestamp()) + CLAIM_TIMEOUT
        def _init_rescue(p):
            cl_init_rescue(p, claim_deadline_ts)
        payload = await modify_payload(db, game_id, _init_rescue)

        unfilled = compute_unfilled(blanks, payload.get("fills", {}))
        deadline = payload["rescue"]["claim_deadline"]

        def _claim_items(volunteer_names: list[str], *, closed: bool = False):
            return build_rescue_claim_layout(
                template["title"], tier, len(unfilled), volunteer_names, deadline, closed=closed,
            )

        rescue_view = LibsCard(
            game_id, host_id, _claim_items([]),
            {VOLUNTEER_ID: _handle_volunteer},
            actions={"cancel": _handle_rescue_cancel},
        )
        cog.bot.active_views[game_id] = rescue_view
        # Local alias: pyright can't narrow the nonlocal `rescue_claim_msg` binding.
        rescue_claim_msg = claim_msg = await retry_transient(
            lambda: channel.send(view=rescue_view),
            label="legitlibs classic rescue claim card",
        )
        await update_game_message(db, game_id, claim_msg.id)

        elapsed = 0
        while elapsed < CLAIM_TIMEOUT:
            await asyncio.sleep(min(POLL_INTERVAL, CLAIM_TIMEOUT - elapsed))
            elapsed += POLL_INTERVAL
            if game_id in cog._game_canceled:
                return
            cur_payload = await get_game_payload(db, game_id)
            if cur_payload.get("state") != "rescuing_claim":
                return

            vols = cur_payload.get("rescue", {}).get("volunteers", [])
            rescue_view.render(_claim_items(await _volunteer_names(vols)))
            try:
                await claim_msg.edit(view=rescue_view)
            except discord.HTTPException:
                pass

        if game_id in cog._game_canceled:
            return

        cur_payload = await get_game_payload(db, game_id)
        vols = cur_payload.get("rescue", {}).get("volunteers", [])
        # Close the claim card: the final roster, and no controls.
        rescue_view.close_phase()
        rescue_view.render(_claim_items(await _volunteer_names(vols), closed=True))
        try:
            await claim_msg.edit(view=rescue_view)
        except discord.HTTPException:
            pass

        if not vols:
            await _run_reveal_phase()
            return

        unfilled_now = compute_unfilled(blanks, cur_payload.get("fills", {}))
        rescue_assign = assign_rescue(unfilled_now, vols)
        fill_deadline_rescue = int(datetime.now(timezone.utc).timestamp()) + RESCUE_TIMEOUT
        def _freeze(p):
            cl_freeze_rescue(p, rescue_assign, fill_deadline_rescue)
        await modify_payload(db, game_id, _freeze)
        await _run_rescue_fill_phase()

    async def _volunteer_names(vols: list[int]) -> list[str]:
        # ``plain_name`` returns raw, unescaped display names; escape here
        # since the card that renders them fits by whole escaped names.
        names = await build_names(cog.bot, guild, vols)
        return [discord.utils.escape_markdown(plain_name(names, uid)) for uid in vols]

    async def _handle_volunteer(vol_interaction: discord.Interaction):
        log.info("%s pressed 'Volunteer' in #%s", vol_interaction.user.display_name, getattr(vol_interaction.channel, "name", "?"))
        uid = vol_interaction.user.id
        outcome = {"value": "closed"}
        def _add_vol(p):
            outcome["value"] = cl_add_volunteer(p, uid)
        await modify_payload(db, game_id, _add_vol)
        if outcome["value"] == "closed":
            await vol_interaction.response.send_message(
                "The claim window is closed.", ephemeral=True)
            return
        if outcome["value"] == "not_player":
            await vol_interaction.response.send_message(
                "❌ Only players in this round can volunteer.", ephemeral=True)
            return
        if outcome["value"] == "already":
            await vol_interaction.response.send_message(
                "You're already signed up. Stay tuned for your blanks.",
                ephemeral=True,
            )
            return
        await vol_interaction.response.send_message(
            "🙋 You're in the rescue squad! You'll get your blanks when the timer runs out.",
            ephemeral=True,
        )

    async def _handle_rescue_cancel(cancel_interaction: discord.Interaction):
        await _handle_fill_cancel(cancel_interaction)

    # ── Rescue fill phase ───────────────────────────────────────────────────
    async def _run_rescue_fill_phase():
        nonlocal rescue_fill_msg
        await update_game_state(db, game_id, "rescuing_fill")
        def _set_rescue_fill(p):
            cl_set_rescue_fill_state(p)
        payload = await modify_payload(db, game_id, _set_rescue_fill)

        rescue_assignments = payload["rescue"]["assignments"]
        vols = payload["rescue"]["volunteers"]
        deadline = payload["rescue"]["fill_deadline"]

        rescuers = cl_filter_rescuers(rescue_assignments, vols)

        def _rescue_fill_items(done: int, *, closed: bool = False):
            return build_rescue_fill_layout(
                template["title"], tier, done, len(rescuers), deadline, closed=closed,
            )

        rescue_fill_view = LibsCard(
            game_id, host_id,
            _rescue_fill_items(
                cl_rescuers_done_count(rescue_assignments, payload.get("fills", {}), rescuers),
            ),
            {RESCUE_SUBMIT_ID: _handle_rescue_submit},
            actions={"cancel": _handle_rescue_cancel},
        )
        cog.bot.active_views[game_id] = rescue_fill_view
        # Local alias: pyright can't narrow the nonlocal `rescue_fill_msg` binding.
        rescue_fill_msg = rfill_msg = await retry_transient(
            lambda: channel.send(view=rescue_fill_view),
            label="legitlibs classic rescue fill card",
        )
        await update_game_message(db, game_id, rfill_msg.id)

        elapsed = 0
        while elapsed < RESCUE_TIMEOUT:
            await asyncio.sleep(POLL_INTERVAL)
            elapsed += POLL_INTERVAL
            if game_id in cog._game_canceled:
                return
            cur_payload = await get_game_payload(db, game_id)
            if cur_payload.get("state") != "rescuing_fill":
                return

            done = cl_rescuers_done_count(
                rescue_assignments, cur_payload.get("fills", {}), rescuers,
            )
            rescue_fill_view.render(_rescue_fill_items(done))
            try:
                await rfill_msg.edit(view=rescue_fill_view)
            except discord.HTTPException:
                pass
            if done >= len(rescuers):
                break

        if game_id in cog._game_canceled:
            return

        # Close the rescue card: the closed frame carries no controls.
        rescue_fill_view.close_phase()
        cur_payload = await get_game_payload(db, game_id)
        rescue_fill_view.render(_rescue_fill_items(
            cl_rescuers_done_count(rescue_assignments, cur_payload.get("fills", {}), rescuers),
            closed=True,
        ))
        try:
            await rfill_msg.edit(view=rescue_fill_view)
        except discord.HTTPException:
            pass

        await _run_reveal_phase()

    async def _handle_rescue_submit(submit_interaction: discord.Interaction):
        log.info("%s pressed 'Submit Fills' (rescue) in #%s", submit_interaction.user.display_name, getattr(submit_interaction.channel, "name", "?"))
        cur_payload = await get_game_payload(db, game_id)
        if cur_payload.get("state") != "rescuing_fill":
            await submit_interaction.response.send_message(
                "The rescue fill phase is over.", ephemeral=True)
            return

        uid = submit_interaction.user.id
        rescue_assignments = cur_payload.get("rescue", {}).get("assignments", {})
        rescue_blank_ids = cl_my_blank_ids(rescue_assignments, uid)
        if not rescue_blank_ids:
            await submit_interaction.response.send_message(
                "Rescue is for volunteers assigned blanks. Maybe next round!",
                ephemeral=True,
            )
            return

        my_blanks = [b for b in blanks if b["id"] in rescue_blank_ids]
        cur_fills = cur_payload.get("fills", {})
        prior = cl_existing_fill_values(blanks, cur_fills, rescue_blank_ids)

        async def _save_rescue_fills(sub_interaction: discord.Interaction,
                                      fills: dict, partial: bool):
            if partial:
                return
            saved = False
            def _store(p):
                nonlocal saved
                saved = cl_store_rescue_fills(p, fills, uid)
            await modify_payload(db, game_id, _store)
            if saved:
                await sub_interaction.response.send_message(
                    "🙌 Rescue fills saved!", ephemeral=True)
            else:
                await sub_interaction.response.send_message(
                    "The rescue phase has ended — your fills were not saved.",
                    ephemeral=True,
                )

        modal = make_fill_modal(
            game_id, db, prompts, my_blanks, tier, _save_rescue_fills,
            existing_fills=prior,
        )
        await submit_interaction.response.send_modal(modal)

    # ── Reveal phase ────────────────────────────────────────────────────────
    async def _run_reveal_phase():
        cur_payload = await get_game_payload(db, game_id)
        player_ids = cur_payload.get("players", [])
        fills = cur_payload.get("fills", {})
        # The reveal pays the people who put a word in, not everyone who
        # pressed Join (trivia-tail-95); the history row still counts the
        # joined roster.
        contrib_ids = unique_contributors(fills)
        reveal_msg: discord.Message | None = None
        try:
            await update_game_state(db, game_id, "revealing")

            names = await build_names(cog.bot, guild, contrib_ids)
            filled_body = render_filled_body_attributed(
                template["body"], blanks, fills, names,
            )
            contrib_names = [plain_name(names, uid) for uid in contrib_ids]

            reveal_items = build_classic_reveal_layout(
                template["title"], tier, filled_body, contrib_names,
            )
            reveal_msg = await channel.send(view=card(reveal_items))

            await mark_template_used(db, guild.id, template["template_id"])
        finally:
            await end_game(db, game_id,
                           player_count=len(player_ids),
                           round_count=1,
                           bot=cog.bot, player_ids=contrib_ids)
            cog.bot.active_views.pop(game_id, None)
            cog._game_canceled.discard(game_id)
        # Only once the round is over and the channel is free: the button
        # goes through the launch guard, which would refuse a busy channel.
        if reveal_msg is not None:
            await cog.offer_another(
                reveal_msg, reveal_items, host_id, {"mode": "classic", "tier": tier, "tag": tag},
            )

    # Lobby is live; the round advances via button presses (defined above).
    return game_id
