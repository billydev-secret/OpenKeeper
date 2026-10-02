# XP — Feature Spec

A leveling system for Discord activity. Five independent sources push positive XP into a single per-member ledger: text messages (with a reply bonus), voice-channel participation, reactions received on image posts, reactions *given* to other members' messages, and economy quest rewards (a quest's `reward_xp` lands as ledger source `quest`; flat, no multipliers, driven by the economy module). A handful of anti-grind multipliers attenuate text XP before it lands. Level is a quadratic function of total XP. XP only goes up; there is no decay.

## Commands

| Command | Type | Permission | Purpose |
|---|---|---|---|
| `/xp_leaderboards timescale:[hour\|day\|week\|month\|year\|alltime]` | Slash | Everyone (server only, ephemeral) | Per-source top-5 for the chosen window plus the caller's own rank. Defaults to `alltime` |
| `/xp_give member:<member>` | Slash | Mod, or a user in the per-guild grant-allowlist | Award a flat manual grant (default 20 XP) to a member; public confirmation |
| Message activity | Listener | n/a | Text messages earn XP; replies to a human earn a bonus. See [[events-spec]] |
| Image-reaction received | Listener | n/a | When a member's qualifying image post gets a reaction, the author earns a flat reaction-XP stipend. See [[events-spec]] |
| Reaction given | Listener | n/a | When a member reacts to *another* member's message, the reactor earns a flat stipend, once per message ever. See [[events-spec]] |
| Voice tick | Background loop | n/a | Members in a qualifying voice channel earn XP per completed interval |

The dashboard exposes the leaderboard, a time-to-level-5 histogram (days-to-reach-level-5 distribution: mean/median/std dev/mode, fixed to the role-grant level rather than a chosen one), and an admin config panel for the XP coefficients and the role/channel ids. See [[reporting-spec]] for the dashboard wrapper.

## Behavior

### Sources

- **Text message XP**: every qualified word in a non-bot, non-system message earns a small per-word amount. Replying to another human adds a flat reply bonus. Qualified words exclude URLs, custom Discord emoji, `:shortcode:` emoji, tokens shorter than 3 characters, and tokens with no alphanumerics. URL-only or emoji-only messages award nothing.
- **Voice XP**: every completed voice interval (default 60 seconds) in a qualifying channel pays a flat amount. A channel qualifies when it has at least 2 non-bot humans and is not the guild's AFK channel. The qualification clock resets to zero when the human count drops below the threshold.
- **Image-reaction XP**: when a non-bot reactor adds any reaction to a message whose author posted an image attachment, the **author** receives a flat stipend. Spoiler status isn't checked, so a spoilered image pays the same as an unspoilered one. The reactor gets nothing. Self-reactions and bot reactions don't pay out.
- **Reaction-given XP**: when a non-bot member reacts to *another* member's message, the **reactor** receives a flat stipend (default 0.34, coeff `xp_coeff_reaction_given_xp`, double the image-react rate). Awarded at most **once per (message, reactor) ever** via a dedup table, so react/unreact can't farm. No self-reactions, no bots. This is the same event that feeds the economy's XP→currency conversion (see [[economy-spec]] §3.2).

### Anti-grind multipliers (text only)

Three multipliers stack on the base text+reply award before it lands:

1. **Cooldown**: three banded thresholds (default <10s → 0.35×, <30s → 0.6×, <60s → 0.85×; ≥60s → no penalty). The first message in a session is full credit.
2. **Duplicate-message**: if the normalized content matches the member's previous message, the award is multiplied by 0.2.
3. **Pair-streak**: when two members alternate in a channel for 4+ consecutive turns, subsequent messages from either are multiplied by 0.5. Resets when a third person speaks.

Voice and image-reaction XP are flat, with no multipliers.

### Level model

XP required for level N is `factor × (N − 1)²`. Default factor 15.6 puts level 2 at ~16 XP, level 5 at ~250 XP, level 10 at ~1260 XP. The stored level is recomputed from total XP on every read, so changing the curve factor takes effect on the next read, not retroactively for past announcements.

Crossing level 5 (the configured role-grant level) grants the level-5 role and posts an announcement to the level-5 log channel. This post doubles as a promotion-review signal for staff, so it also shows a "Spicy access" field (✅/❌, or 🌙 while the role is saved in a Take a Break snapshot) when the guild has an NSFW grant role configured, and it's held back (not skipped) if the member joined fewer than 2 days ago: `handle_level_progress` parks it in `pending_promotion_posts` instead of sending, and a background sweep (`promotion_review_recheck_loop`, ~30 min cadence) posts it once tenure clears or drops the row if the member has since left. Intermediate level-ups post to the level-up log channel (one embed per crossed level) and are not tenure-gated. The member is tagged in the message content (the embed keeps their resolved name) and only **once per burst, on its last card**: a backlog of owed levels is one notification, it jumps to the highest level, and a send that fails part-way pings nobody, since the unsent levels are retried on the next award and that burst's last card carries the ping. The level-5 card never pings the member (it pings the mods who review it), so when both channels match and level 5 ends a burst, the last *general* card is the one that pings. Bulk backfill awards grant the role but skip the announcement embed.

**Announcing owed level-ups.** What has actually been *announced* is tracked separately from `level`, via an `announced_level` column that advances only when a level-up is genuinely announced (never by an award). This closes a gap where XP credited with no Discord handle in scope (quest payouts are the live case) moved a member up a level with nobody to announce it, and the next ordinary award then computed its start level from the already-credited total, saw `old == new`, and lost the level-up for good. Now a level won on a silent path stays *owed* and is delivered on the member's next ordinary award. Paths that credit XP but deliberately announce nothing (bulk backfill, the XP recompute script, and migration 075's one-time seed) catch `announced_level` up to `level` for the members they touch, so a deploy or replay never floods the channel with a member's whole level history.

### Leaderboards

`/xp_leaderboards` shows four separate top-5 boards (Text, Replies, Voice, Image Reacts) for the selected timescale, each with median / std dev and the caller's own standing. Manual grants are recorded for audit but **excluded** from leaderboards. If a guild has totals but no event ledger (data predating the ledger), the embed surfaces a note and continues.

The member's own `/info` card (and the Help & Settings hub's ℹ️ Your Info row) shows a single overall rank, `xp_system.get_member_xp_rank`: by all-time `member_xp.total_xp`, the number the level is computed from. That total **includes** manual grants, so it can differ from the per-source standings above, which exclude them, on purpose: it answers "where does my level stand", and a level includes its grants. Members on equal XP share a rank; members with no XP are neither ranked nor counted. See `docs/specs/member_info_spec.md`.

## Permissions

- `/xp_leaderboards`: open to everyone; rejects DMs.
- `/xp_give`: caller must be a mod **or** their id is in the guild's grant-allowlist. Rejects DMs, bots, and self-grants.
- Dashboard XP routes require admin.
- The level-5 role grant requires the bot to have **Manage Roles**. Failures log a warning and skip the role assignment but don't reverse the XP.
- The level-5 reward role is refused (logged, skipped, XP kept) when it carries elevated permissions, is managed, or sits at or above the bot's top role (`core.role_safety.grant_block_reason`). The dial is config, and every member who crosses level 5 would otherwise receive it.
- The reward role is also skipped for a member **under a hold** (on a break, jailed or inactive, `role_grant_audit_service.member_is_held`). Chatting in the hold's one channel can cross the bar, and granting it would hand back a role the hold stripped on purpose; the saved roles return it on release, and the next award re-checks. See `docs/specs/take_a_break_spec.md`.

## User-visible errors

| When | The user sees |
|---|---|
| `/xp_leaderboards` used in DMs | "This command only works in a server." |
| `/xp_leaderboards` caller not resolvable as a member | "Could not resolve your member record in this server." |
| `/xp_leaderboards` with no XP recorded yet | Empty-state embed: "No tracked … XP yet" for each source |
| `/xp_leaderboards` when only legacy totals exist | "Existing XP totals predate the event ledger. New text and voice XP will appear here going forward." |
| `/xp_give` by non-authorized user | "You don't have permission to use this command." |
| `/xp_give` against a bot, self, or in DM | Per-case ephemeral guard message |

## Non-goals

- **No XP decay.** No time-based, inactivity-based, or moderation-based subtraction.
- **No XP loss on message delete or edit.** The award persists in the ledger.
- **No reaction-emoji weighting.** Every reaction on a qualifying image pays the same flat amount.
- **No leaderboard surface for manual grants.** Grants are recorded for audit only.
- **No DM XP.** Both sources require a guild context.
- **No bot XP, no self-reaction XP.** Bot authors and bot reactors never earn.
- **No retroactive level-5 announcement when a backfill crosses the threshold.** The role lands; the embed doesn't.
- **No per-channel rate caps beyond the channel-exclusion list.** A channel is either fully eligible or fully muted.

## Configuration

Per-guild settings, editable from the dashboard XP & Leveling panel except
where noted:

- **Algorithm coefficients**: per-word XP, reply bonus, image-reaction stipend, reaction-given stipend, the three cooldown thresholds and their multipliers, duplicate-message multiplier, pair-streak threshold and multiplier, voice-award amount, voice-interval seconds, voice-minimum-humans, manual-grant amount, level-curve factor. (Quest-reward XP is not tuned here: each quest's `reward_xp` is set on the economy dashboard and credited flat, no multipliers.)
- **Level-5 role**: the role granted on reaching level 5.
- **Level-up log channel**: where per-level-up embeds post.
- **Level-5 log channel**: where the level-5 milestone embed posts (can match the level-up channel; the role-grant level then de-duplicates). Set on **Moderation → Role Management → Promotion Reviews**, labeled *Promotion Reviews Channel*; the same channel carries the promotion-review cards.
- **Grant-allowlist**: extra user ids (beyond mods) allowed to invoke `/xp_give`.
- **Channel exclusion list**: channels (and threads whose parent is in the list) where text and image-reaction XP are suppressed. An excluded **voice** channel likewise never qualifies for a tick, so it pays neither voice XP nor the daily voice login. The AFK voice channel is implicitly excluded.
- **Promotion Review Grant Role / Ping Role**: the role the level-5 card's Grant button hands out, and the role pinged when a promotion-review card posts. Both live on **Moderation → Role Management → Promotion Reviews**, not on the XP panel. See `promotion_review_spec.md`.

One internal knob (the voice-tick poll period) is pinned in code and not exposed. The level-5 promotion post's 2-day tenure minimum is likewise pinned. The "Spicy access" field's grant role is the Promotion Review Grant Role named above when set, falling back to the grant whose internal key is literally `nsfw` (see `role_grant_spec.md`) when it isn't.

**The role-grant level is pinned at 5 by decision, not by omission.** `XpSettings.role_grant_level` exists and every enforcement site reads it (the grant, the milestone embed, the promotion-review card, the time-to-level report), so the field sits one line away from being a coefficient, and it is deliberately left out of the two name lists a dial would have to join: `load_xp_settings`' `_FLOAT_COEFFS`/`_INT_COEFFS` (the read path) and `_XP_COEFF_READERS` in `web_server/routes/config.py` (the dashboard's coefficient table, which the write path iterates too). The reason is wording a dial cannot follow: the number is baked into the frozen `time-to-level5` route id and its whole panel (title, subtitle, empty state, chart heading), into the XP panel's *Level 5 Role* label and the *Level-Up Log Channel* hint that tells admins where level 5 is announced instead, and into the Promotion Reviews copy and `manual.html`. A guild that set it to 7 would be granted at 7 while every surface it reads still said *Level 5*, a worse lie than a fixed number. Revisit only together with renaming those surfaces to "the milestone level". `tests/xp/test_xp_system.py::test_role_grant_level_is_pinned_and_not_loadable_from_config` fails if the key is ever wired up without that pass. (2026-08-29 config audit finding #89; closed as won't-do 2026-09-10.)

## Stored data

Per-guild and per-member: a totals row (total XP, cached level, the highest level actually announced, last-message timestamp and fingerprint for the cooldown / duplicate multipliers), an append-only event ledger tagged by source (text, reply, voice, image-react, reaction-given, grant, quest) with optional channel id, a per-(message, reactor) dedup table backing the once-ever reaction-given award, live voice-session state (current channel, qualifying-since timestamp, intervals already paid), a last-activity row for inactivity reports, a processed-messages ledger for backfill idempotency, an append-only role-event audit (every grant and removal the bot sees, not just XP rewards), and a `pending_promotion_posts` row per member whose level-5 post is waiting out the tenure minimum (cleared by the recheck sweep). The event ledger is kept in full: every leaderboard, graph and report reads `xp_events` directly. Two tables from a retired feature remain: `xp_daily`, a per-(member, source, channel, day) rollup, and `xp_rollup_state`, its single-row watermark (migrations 186/187). They backed an opt-in Event Retention setting that would have pruned raw events older than 90 days; it was never switched on in any guild and was removed on 2026-09-30 with the prune, the nightly rollup and the readers' union. Nothing writes or reads either table now; they hold their last contents until a migration drops them, and `purge_user_data` and the guild purge still clear them. The XP hour-of-day / day-of-week graphs and the XP overlay keep their 90-day window as a display window. The pair-streak state lives only in memory and resets on bot restart. No DM data is ever stored.
