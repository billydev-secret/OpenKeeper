# Common modules — the shared seams (Reference)

A map, not a rulebook. `docs/design_guide.md` holds the order of the decisions
and the coding standards; this is the answer to the question that comes up
*while* you build: **does something already own this?**

Most modules below carry a module docstring explaining what they own and what
they deliberately leave to their callers (`app_context` does not yet). **Where one exists it is authoritative; this page only
tells you which module to open.** If a line here disagrees with the module, the
module wins and the line is a bug worth fixing.

Two things this page is not:

- **Not the consolidation backlog.** `git show 678a16fa:docs/plans/common-lib-round-2.md` is the
  2026-08-22 repo-wide review: the ranked queue of duplication still worth
  collapsing, *and* a verified LEAVE list of twins that are deliberate. Read
  that before proposing a de-duplication: half the obvious candidates have
  already been examined and kept apart on purpose.
- **Not exhaustive.** It lists the seams a new feature reaches for by mistake
  when it doesn't know they exist. Small helpers stay findable by grep.

---

## Safety and identity

| Seam | What it owns |
|---|---|
| `no_contact/service`: `is_no_contact_conn`, `no_contact_partners_conn` | The no-contact list. Every member-to-member surface reads it through these, never by querying `no_contact_pairs`. → `no_contact_spec.md` |
| `games/utils/question_source.channel_allows_nsfw` | The single NSFW verdict, gated on Discord's own age flag (`shared/age_gate.is_age_gated_channel`: channel or category) with thread-parent inheritance and fail-safe-to-SFW. ~25 call sites; `advisor/context.can_view` delegates to the same decision |
| `shared/name_resolver.build_name_fn` | Turning a member id into a name a *reading* client can render. Builders take the `name_fn`; a repo test guards that every render site passes one. → `embed_style_guide.md` § Naming members in embeds |
| `privacy/guild_purge_service.table_names` | The one schema walk. Every sweep that reflects table names off `sqlite_master` (the guild purge, the anonymization, the access export) goes through it, and each name comes back past `core/db_utils.sql_identifier` because a table name is formatted into SQL, never bound. A refused name is skipped rather than raised, and surfaces on the anonymize report's `skipped` and the export's `notes`. → `privacy_spec.md` |
| `privacy/service`: `SUBJECT_ID_COLUMNS`, `purge_user_data` | Which column names mean "this row is about a member". A table whose subject column isn't listed is invisible to the subject-access export. → `privacy_spec.md`, `data_register.md` |
| `privacy/deletion_service`: `run_deletion`, `delete_discord_messages`, and `shared/discord_scan.find_user_messages` | Deleting a member's Discord messages. Headless on purpose: the My Data panel and the dashboard's Member Data panel drive the same engine through a progress sink, so neither surface owns the loop. → `privacy_spec.md` |
| `privacy/logic.MEMBER_ERASABLE_STORES`, with `whisper/repo.forget_member` as the reference entry | Erasing one feature's records for one member. The registry is the only list of what the My Data panel offers, and each store carries the eraser itself, so the member's button and `purge_user_data` run the same function. A feature with its own forget-me command is what this replaced. Erasers take a `conn` so the purge can run them in its own transaction. → `privacy_spec.md` § Member-erasable stores |
| `privacy/service.anonymize_user_data` | The third privacy path beside purge and export: rewriting a member's id to a surrogate so the server's statistics stay comparable. Shares its operation list with `plan_anonymization`, so a dry run cannot disagree with the apply. → `privacy_spec.md`, `data_register.md` |
| `shared/anon_audit_service.record_event` | The deanonymizing audit trail behind the anonymous features, swept at 90 days. One of four unrelated `record_event`s the LEAVE list keeps apart |
| `AppContext.member_is_mod` | The mod check, for a `Member` already in hand: Manage Server or Administrator passes outright, then the configured mod/admin roles. `GuildConfig.member_is_mod` is the roles-only half; it is not the mod check. The uncached-member-reads-as-not-a-mod behavior belongs to its sibling `is_mod(interaction)`, where `get_interaction_member` returning `None` is what decides it. It is chosen because an unearned tick is harder to undo than a missed one |
| `core/utils.is_host_or_mod` | Whether the presser may use a game's host controls: its host, or a mod. ~15 callers |
| `web_server/deps`: `require_perms` and the `require_game_host` / `require_economy_manager` / `require_survivor_host` tiers | Every dashboard route's auth guard; `require_perms(set())` is "any signed-in member". All of them refuse through the one private builder, which logs a warning under `dungeonkeeper.web.auth` naming the route, the caller, what they hold and what they needed. The body stays a bare `Not authenticated` / `Insufficient permissions`. The wellness routes kept a private pair that refused in silence until bug #254; `routes/wellness_deps` now only names two tiers of this one. `tests/web/test_authz_logging.py` pins the line and the body |

## Embeds, DMs and panels

| Seam | What it owns |
|---|---|
| `core/branding.safe_resolve_accent` | The accent color, from a bot, an AppContext or a db_path. Never call `resolve_accent_color` directly: it raises, and a repo-wide test fails the suite if you do |
| `shared/dm_branding`: `send_branded_dm`, `brand_dm_embed`, `brand_dm_layout` | Branded DM delivery, for an embed or a Components V2 card (`send_branded_dm(layout=...)` brands the card with the guild's icon and a `-# From <server>` line; leave it three components of headroom). Four near-identical `_try_dm` helpers existed before it did |
| `economy/service.notify_member` | Member notification with the DM → bank-channel fallback, and `require_game_role=True` for recurring economy DMs. Returns a bare `bool`; it is `deliver_econ_dm` underneath that returns the `DmDelivery` naming the surface actually used, so reach for that one when the surface matters |
| `dm/batch`: `store.enqueue`, `service.register_handler` | A member DM that can wait: queue it, and one DM per recipient and subject goes out after ten minutes. Re-checks no-contact and membership at send, retracts on message delete. Kinds so far: Needle replies, confession replies, AMA answers, Whisper guesses, economy notes (`notify_member` delivery); `docs/specs/dm_batching_spec.md` |
| `core/utils.jump_url` | Message permalinks. Never hand-roll the URL |
| `core/utils.safe_ephemeral` | An ephemeral reply whether or not the interaction was already answered: the done-or-followup dance. ~16 callers; an error handler re-deriving it is round-2 bug 6 |
| `core/utils.allow_pings`, `core/utils.template_pings` | The allow-list a send that must ping passes, now that the Bot's default is `AllowedMentions.none()`: `allow_pings(users=, roles=)` pins all four fields from the ids the text names; `template_pings(template, users=, roles=)` derives it from an admin-authored template's own `@here`/`<@&id>` text plus the placeholders' targets. `games/start_ping_service.role_only_mentions` / `host_only_mentions` are its two older special cases and stay; don't add a third. `tests/contracts/test_mention_default_audit.py` sweeps every send |
| `core/layout`: `ExpiringLayoutView`, `layout_budget`, `check_budget`, `disable_all`, `sample_prefix_ids` | The Components V2 counterpart of `ExpiringView` and the 40-component / 4000-character budget (`docs/components_v2_style_guide.md` § The budget). `disable_all` walks nested items and typed selects, which `core/utils.disable_all_items` does not; there is deliberately no `send_layout` wrapper |
| `core/utils.ExpiringView` | A `discord.ui.View` base for finite-timeout views: tracks `self.message`, disables every item and edits it on `on_timeout`, guarded for a message the member already dismissed. A press that swaps in a new view instance on the same message must `stop()` the old one first, or its timer later edits the dead view back over the live one. ~55 pre-existing timed views don't use it yet (`tests/contracts/test_view_timeout_contract.py` tracks the remainder) |
| `core/sticky.StickyPanel` | A panel kept as the last message in its channel: reposted beneath new activity, debounced, deleted with retry. About two dozen features ride it, eleven of them through `LiveBoards` |
| `core/panel_refresh` + `panels/refresh_service` | Redraw a posted panel when the code or the art changes. A sticky opts in with `StickyPanel(refresh_on_update=True)`; a posted-once panel gives its `PanelSpec` a `render` method and the shared Post route records it in `posted_panels`. `panels/cog` runs every entry once per boot and on each `panel_art_change`, so don't add a per-cog art listener. A posted-once panel whose content follows settings saved elsewhere redraws its own copies with `refresh_panel_key(bot, db_path, guild, key)` (the Mod Settings panel, on `staff_pings_config_change`) rather than re-walking `posted_panels` itself |
| `core/background`: `run_forever`, `spawn`, `FailureStreak` | The bot's background work. `run_forever` is a loop that ticks until the bot closes and survives a tick that throws; `spawn` is a one-shot task nobody awaits, held and its crash logged (`test_core_background.py` fails on a discarded `create_task`). `FailureStreak` stops a loop that posts into a channel on a schedule from retrying a dead one forever: consecutive failures per key, a warning each, one error at the threshold, then paused until a success, or with `retry_after` one probe per interval. Sticky panels, coin drops and chat revive use it; a fourth scheduled poster should too, rather than re-counting inline. It overlaps `send_retry` only in name: that retries one send through a transient 5xx, this gives up on a destination that keeps refusing. Every streak registers itself (weakly), and `stalled_deliveries(guild_id)` plus `gave_up()` (the loops `Bot._resilient_task` stopped restarting) are what the Configuration Problems card shows |
| `core/reports.chunk_text` | Splitting long text into Discord-sized messages: `boundary`/`min_fill` for prose with no newlines, a `prefix` paid for out of the first chunk, `max_parts` with an `overflow_note`. Intake's line-accumulator is deliberately separate (LEAVE list) |
| `docs/help_card`: `build_doc_help_layout`, `load_doc_body` | A hub panel's **Help** answer: a dashboard doc read at click time and laid out as one ephemeral Components V2 card: heading lifted into the glyph-led title, `---` as drawn lines, images as galleries, a cut at a block boundary (never inside a code fence) with a `-#` note. It reuses `docs/render`'s section and block splitters rather than re-deriving them, so a doc reads the same in a Help card as in its posted channel |
| `economy/view_helpers`: `EphemeralCard`, `review_surface` | Repainting a review card that may live in a channel *or* behind the todo board's ephemeral detail message. An ephemeral message can't be edited through the channel-message endpoint; this is the one wrapper that hides the difference instead of branching at every repaint |
| `web_server/routes/panel_posting`: `sticky_conflict`, `own_channel_id` | The guards every "post this panel into a channel" route needs. A panel already in the target channel is **warned, never blocked**: refusing doesn't undo a collision, it just locks the admin out of maintaining a panel that is sitting there |
| `web_server/helpers`: `require_in_range` | Refusing an out-of-range number with a 422 in Pydantic's own shape (`type`/`msg`/`loc`), so the dashboard's error handling needs no second branch. **Reach for it only when the bound lives in a rule table** (`_DUEL_GAMES`, which owns the duel dials' defaults and column order too); everywhere else declare `Query(ge=, le=)` or `Field(ge=, le=)` where the value enters and let FastAPI raise it. The house rule is refuse, never clamp: a clamp reports success for a value it did not store |
| `web_server/helpers`: `member_name`, `member_name_fn` | Naming a member inside a dashboard mod-log mirror, which is an embed. `member_name_fn` is the `name_of(uid)` resolver over a list of ids (one bulk read); `member_name` is one name for an f-string. Both wrap `shared/name_resolver.build_name_fn`; two route modules grew private copies of them independently. `resolve_names` in the same module is the third shape: a list of row dicts filled in place |

## Games

| Seam | What it owns |
|---|---|
| `games/utils/question_source` | Every question-drawing game's draw: the shared `games_question_bank` table, heat-tag filtering, NSFW gating, least-recently-served selection and mark-served. Bank-only with one exception: Free-For-All falls back to its own static list (`games/ffa/prompts.pick_prompt`) when the bank is empty. No game has an AI fallback |
| `games/utils/launch_guard`: `refuse_launch`, `launch_refusal` | Every party-game launch's preflight in one place: the channel is allowlisted, the dashboard's enabled dial is on, no game is already live there, and a bank-only game has a question to draw. 16 game cogs call `refuse_launch` (the interaction-shaped wrapper); `game_manager` and the `games_help` views call the `launch_refusal` it wraps. The dial check is what makes an admin's Disable stick |
| `games/utils/round_pacing`: `resolve_pacing`, `launch_pacing`, `RoundPacing`, `voter_may_advance` | Round timing for the vote-a-round games (WYR, NHIE, MLT and the Hot Takes / Fantasies vote pair): how long a round runs and who may move it on |
| `games/board_sticky_service.LiveBoards` | A running game's live board as a sticky, repainted as the state changes and gone when the game ends. Eleven game cogs share it. Since Components V2 wave 3b (games core) it carries both an embed arm (`LiveBoardView`/`_build_embed()`) and a layout arm (`LiveBoardLayoutView`/`_rebuild()`), branching on `isinstance(view, discord.ui.LayoutView)`. **Deliberate deviation:** the embed arm retires when the last games group lands (Components V2 wave 3b) |
| `games/bottom_bar_service`: `make_bar_panel`, `bar_location`, `set_bar_location`, `BAR_TARGETS` | One live game's **bottom bar**: a line of text plus a row of buttons kept at the foot of its own channel. Keyed per *game*, not per guild (two rooms in one server each follow their own conversation), and deliberately **not** on the Live Game Boards dial: a board that stops following is still readable further up, but a bar that stops following is a row of buttons nobody can reach. A game supplies exactly three things: the payload key its message id is stored under, the panel's log name, and how the content is built. AMA's bar was the first and Truth or Dare's opt-in strip the second; the second is what moved the shared two thirds here rather than copying it. Also carries both an embed and a `PanelContent(layout=...)` arm since wave 3b, same retirement point as the board service above |
| `games/utils/send_retry` | Retrying a Discord call that failed transiently, and the `is_transient` classification that decides whether it was worth retrying at all. discord.py retries `{500, 502, 504, 524}` unconditionally but **not 503**, so a bare `channel.send` dies of one. Filed under `games/` because that is who calls it today; nothing in it is game-shaped, so reach for it before writing a fourth inline 5xx loop (`core/sticky._retry_delete` and `events_cog._fetch_reaction_message` are the other two, each with a genuinely different contract) |
| `games/utils/derangement.random_derangement` | Secret-partner assignment: everyone gives to exactly one other, receives from exactly one other, nobody gets themselves (Sattolo cycle, O(n)) |
| `duels/base_game.py` | The template-method base the six mini-games override. `render_*_state` and `handle_interaction` are abstract hooks every game must implement, and `on_game_start` an optional no-op hook, so their look-alike bodies are overrides, not duplication (see the LEAVE list in `git show 678a16fa:docs/plans/common-lib-round-2.md`) |
| `games/utils/game_store` | The shared store under the six mini-games' `db.py` files (`94341d5e`); what remains in each `db.py` is thin wrappers and per-game shape, both on the LEAVE list |

## Economy

| Seam | What it owns |
|---|---|
| `economy/submission_store` | The ledger mechanics under every paid, mod-approved submission: `charge_and_insert`, `move_state`, `refund_once`, `expire_stale_pending`, `set_card`. Only the mechanism: receipt wording, card shape, caps and what approval *produces* stay with the product. `move_state` returns `None` on a lost race rather than raising, because the apology sentence is copy. **Bounties are deliberately not this shape** (many-payer pot with a rake, no pending state) |
| `economy/approvals_service` | The merge that makes the paid products one job for a moderator: pending rows read oldest-first across themed day and pin (sponsored QOTD was a third until it was retired, 2026-09-30). Adding a product is one row in `QUEUES`. Emoji sponsorship is deliberately excluded because its approval is an upload, not a yes/no |

## Config, state and AI

| Seam | What it owns |
|---|---|
| `core/settings_registry` | The declarative inventory of config keys the AI assistant may reason about. **Hand-authored on purpose**: an auto-derived dump would be noise and would expose keys nobody vetted. `writable` is opt-in per setting; permission-boundary keys (`admin_role_ids`, `mod_role_ids`) are never writable at any confirmation level. A feature configured in its own table carries a `TableSignal` instead of settings, so the setup-gap scan can see it. A registry entry that disagrees with its owning panel is the repo's most common config bug |
| `advisor/service` | The grounded-Claude brain behind both the dashboard Help ask box and `/ask`. Answers are grounded in `manual.html`, so the advisor cannot invent a command; the corpus is prompt-cached. Only when the off-by-default `advisor_server_context` dial is on does it also see shared-channel names, staff docs and recent announcements; `data_register.md` § Processors lists exactly what. Model is a per-guild dial (`resolve_advisor_model`, staff and members resolve separately), never a constant in a caller |
| `games/utils/ai_client.generate_text` | The thin Anthropic call for short generated copy. Returns `None` on any API error rather than raising, so callers degrade, they don't crash |
| `games/utils/ai_client.generate_json` | The same call constrained to a JSON schema (structured outputs, `output_config.format`). Returns the parsed object, or `None` on an API error or a truncated reply. Never hand-parse JSON out of prose |
| `core/role_safety`: `role_block_reason`, `grant_block_reason` | Whether the bot may hand a role out at all: exists, not @everyone, not managed, below the bot, no elevated permissions. **Every `add_roles` whose role comes from config calls one of them first**: `grant_block_reason` for a config dial (it logs the refusal and has no elevated override), `role_block_reason` where a human is told why. `tests/contracts/test_role_grant_safety_contract.py` scans `src/` and fails on an unguarded call; the few that restore a member's own saved roles or apply a lock-down role are allowlisted there with the reason. A dashboard route saving such a dial calls `web_server.helpers.refuse_ungrantable_role` so the admin hears the refusal at Save rather than in a log |
| `core/role_provision`: `ensure_feature_role`, `ensure_config_role` | Creating the role a feature or a config dial needs, once, and recording that the bot made it. Replaced the private copies in Jail, Inactive, DM modes and Survivor; Economy Notifications uses it too. The perk shop's per-member roles are deliberately not on it, since adopting a role by a member-chosen name would be privilege escalation → `role_provisioning_spec.md` |
| `feature_rotation/store`: `claim_flip` / `claim_announce`, `release_flip` / `release_announce` | Per-guild per-day idempotency for once-a-day work. The claim is taken *before* the Discord call so nothing can act twice; `release_*` hands the day back when the work didn't happen, so a failure doesn't burn the day |
| `games/external/logic`: `claim_payout` / `claim_payout_sync`, `release_payout` / `release_payout_sync` | Pay-once-per-message for anything minted off a message: external game bots, Mention Awards, and every backfill script replaying them. Claim first, in the same transaction as the credit; a False claim means someone already paid. **One claim per message across all kinds**: the key is `message_id` alone. A script riding a `sqlite3` connection uses the `_sync` twin rather than inlining the `INSERT OR IGNORE` |

## Dashboard

The rules for these live in `design_guide.md` § Dashboard-side code and in
CLAUDE.md; this is only which file to open. All paths are under
`src/web_server/static/js/`. The count is how many of the 147 panel modules in
`panels/` import it (2026-09-13). A helper most panels use is not optional.

| Module | What it owns | Panels |
|---|---|---|
| `api.js`: `api`, `apiPost`/`apiPut`/`apiDelete`, `esc`, `fmtTs`, `fmtAge` | Every fetch, and the one HTML escaper. A panel with its own `esc` is a second copy to keep correct | 104 |
| `config-helpers.js`: `mountAsync`, `guardForm`, `guardedSubmit`, `loadConfig`, `loadChannels`/`loadRoles`/`loadMembers`, `memberSearch`, `resetMetaCaches` | Config panels: the guarded first load with a retry, per-form unsaved-edit tracking, one submit at a time per form (never hand-roll a `busy` flag; `guardedSubmit` is on ~96 handlers and a sweep fails a panel that wires its own submit listener), and the guild-scoped pickers and their caches; a new cache is cleared in `resetMetaCaches()` or a test fails | 87 |
| `ui.js`: `toast`, `confirmDialog`, `promptDialog`, `bindRowActivation` | Feedback and confirmation. Never `window.confirm` or `window.prompt` | 52 |
| `states.js`: `renderLoading`, `renderEmpty`, `renderError` | The three placeholder states, so no panel hand-writes an error div | 50 |
| `report-helpers.js`: `mountReloadable`, `rangePicker`, `withLoading`, `syncHash`, `mountBotToggle` | Report panels: every refresh guarded (not just the first), the date range, URL state | 33 |
| `charts.js`: `makeLineChart`, `makeBarChart`, `makeStackedBarChart`, the `CHART_*` colors | Chart construction and the palette | 23 |
| `table.js`: `renderSortableTable` | Sortable tables that **escape every cell**; a column opts into markup with `html: true` and then owns its escaping. A hand-built `<table>` gives that up | 16 |
| `audit-helpers.js`: `auditPanel`, `badge`, `jumpLink`, `tsColumn` | Audit-log panels | 13 |
| `tabs.js`: `mountTabs` | Tabbed pages | 9 |
