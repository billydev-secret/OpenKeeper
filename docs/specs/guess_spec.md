# Guess — Feature Spec

A guess-the-member image game. A consenting submitter posts an image (**SFW or NSFW**); the bot detects an "interesting" region, offers a crop editor to frame it (faces filtered out where possible), and posts the crop to a dedicated channel. Anyone can guess the member from a picker restricted to opted-in members. All-time posting/solving totals are tracked per guild.

> **SFW support:** Submissions may be SFW or NSFW, and **the bot enforces nothing about where they are posted**. This is a deliberate call by the server owner: moderators police placement, and the bot does not. The web config route accepts any channel (an age-restricted-flag check would be bypassable anyway: save an age-gated channel, then clear the flag), and the runtime has no `is_nsfw()` recheck. There is no per-submission explicitness verdict stored; NudeNet's detections drive crop selection only.

> **Naming:** the feature's tables, commands, cog and web panels are all `guess_*`; there is no Veil variant.

## Commands

| Command | Type | Permission | Purpose |
|---|---|---|---|
| `/guess submit <image>` | Slash | Guess role (required; errors if the role isn't configured, or if you don't hold it) | Submit an image (SFW or NSFW); runs the detection pipeline and opens the crop editor / post flow |
| `/guess optin` | Slash | Everyone (errors if the Guess role isn't configured) | Opens a consent card disclosing what joining stores (cached originals, confession text + your id, stats); the role is granted only on **Join the Pool** |
| `/guess optout` | Slash | Everyone | Self-service leave: removes the Guess role; open rounds where you're the answer flip `answer_optout`, past rounds and stats stay |
| `/guess leaderboard` | Slash | Everyone | Posts the top 5 submitters (rounds posted/solved) and top 5 guessers (rounds solved); fixed, no arguments or categories |
| `/guess round <round_id>` | Slash | Mod (`manage_guild`; hidden from non-mods in the Discord UI via `default_permissions`) | Inspect a specific round (status, submitter, answer, crop, guess/unique-guesser counts, re-roll count) |
| `/guess delete <round_id>` | Slash | Submitter or Mod (`manage_guild`; checked in code only; the command itself isn't permission-restricted client-side) | Soft-delete a round (message best-effort deleted, stats preserved) |
| `/guess confess text:<...>` | Slash | Guess role (requires both the role and the channel to be configured) | Renders an anonymous text confession as an image card and previews it for you to post or cancel |
| Guess Who Submit Prompt | Web (dashboard) | Admin | Config → Guess Who → Post Submit Prompt immediately (re)posts the sticky Submit/Help prompt at the bottom of the configured guess channel. There is no `/guess prompt` command |
| `Guess` button (on round post) | Persistent | Everyone except the round's submitter | Opens an ephemeral member picker (see Guessing below) |
| `Submit` / `Help` buttons (on the sticky prompt) | Persistent | Everyone (Guess role enforced on submit) | Submit opens a form taking a **photo upload or an image link** (one is required; an upload wins if both are given) that runs the same checks and feeds the same detection pipeline as `/guess submit`; Help shows the guild's `guess` doc from the dashboard's Docs panel, or a built-in how-to when there is none |
| Web config panel | Web (dashboard) | Admin | Per-guild role, channel, cooldown, difficulty, image limits. Any channel may be chosen, age-gated or not. The API only checks the channel exists, and only when the bot can resolve the guild |
| Web audit log | Web (dashboard) | Mod | Recent submit / delete / solve / guess-cap / failed-post events |

There is no `/guess stats` command; it doesn't exist in code.

Bot perms required: **Send Messages**, **Attach Files**, **Read Message History** in the guess channel (no **Embed Links**, because no Guess card is an embed); **Manage Roles** to grant the consent role on `/guess optin`. The bot never removes the role itself; see "Consent and opt-in" below.

## Behavior

### Consent and opt-in

There's a configured **consent role** (`guess_role_id`). It gates submitting (`/guess submit` and the sticky prompt's Submit form), posting confessions (`/guess confess`), and being a pickable answer. Unlike some other guild features, this role has no "Everyone if unset" fallback: if it isn't configured, `/guess submit` and `/guess confess` both refuse with a message telling the user to ask an admin to configure it in the web dashboard. The bot will make that role for you, but **only while you are offering it to members** on Config → Discord Onboarding (create-on-offer): creating `@Guess Who` on its own would turn an honestly-unconfigured game into a configured one refusing every member, since nobody would hold the new role. Its live state is on Config → Bot-Managed Roles (`docs/specs/role_provisioning_spec.md`).

- `/guess optin` opens an ephemeral **consent card** stating what joining stores (original submissions cached on disk until solve, with unsolved rounds cleared after 7 days per `ORIGINAL_MAX_AGE_DAYS`; confession text stored with the author's id and admin-visible; rounds/stats kept) and grants the role only on the **Join the Pool** button; **Cancel** closes the card without a reply. Already-holding members just get told so. The card's heading is layout only; `GUESS_DISCLOSURE_VERSION` tracks the wording.
- **The consent is evidenced, not just taken** (migration 154). Clicking Join writes a `guess_consents` row: `consented_at` and the `disclosure_version` of the wording that was on screen. GDPR Art 7(1) puts the burden on the controller to *demonstrate* consent, and the Discord role demonstrates only that someone holds it, not what they were shown or when. Bump `GUESS_DISCLOSURE_VERSION` (`games/guess/repo.py`) whenever the disclosure changes materially: rows carrying an older version recorded agreement to wording the member never saw, and that cannot be reconstructed afterwards. Version 2 is the 7-day age-out of unsolved originals.
  - Join refuses — "The Guess role can't be handed out right now" when the configured role carries elevated permissions, is managed, or sits above the bot (`core.role_safety.grant_block_reason`); no role, no consent row.
  - The write happens **after** the role grant succeeds, because a consent row for a member who never received the role would misrepresent what happened; a failure to write it is logged rather than raised, so a DB hiccup can't cost a member their opt-in.
  - **Withdrawal stamps, it does not delete** (`withdrawn_at`). Art 7(3) makes withdrawal as easy as giving consent, but the record that consent *was* held, and for how long, is what makes the processing done under it defensible. Rejoining writes a second row rather than overwriting the first.
  - It is stamped in the **role-removal listener**, not in `/guess optout`: that listener is the one choke point both the command and a mod's manual role removal pass through.
  - `guess_consents` joins `purge_user_data`: a full erasure clears it, an optout does not.
  - **Legacy role holders.** Members who held the role before the record shipped were grandfathered and have no consent row. `disclosure_version` **0** (`guess_repo.LEGACY_DISCLOSURE_VERSION`) means "legacy role holder, disclosure not shown": a row that records the gap and when the member's data begins (`consented_at` is their earliest round or guess), **not** evidence of consent, and nothing may count it as consent. The disclosure cannot be shown retroactively, and inventing a consent that was never given would misstate the record, so the row is deliberately an admission, not a cure; a legacy holder who presses **Join the Pool** (or rejoins) writes a real row beside it. `scripts/backfill_guess_legacy_consents.py` writes them: dry run by default, `--apply` copies the database with the SQLite backup API first and is idempotent, `--holders-file` restricts it to current role holders (the database cannot say who holds the role). The owner runs it.
- **A full erasure takes the rounds too.** A round they submitted, or whose `answer_id` is them, is deleted **whole**, with the `guess_guesses` and `guess_audit_log` rows hanging off it: the round *is* the submission, and other players' guesses on it go with it (the Risky Rolls ground). Their guesses on surviving rounds, guesses naming them, and their audit-log actor rows go too. Their `solver_id` on somebody else's round is scrubbed to NULL instead, because that round is the other member's record, and `top_solvers` already filters `solver_id IS NOT NULL`. The cached original is unlinked by the dashboard route **before** the rows go, because an unlink cannot be rolled back inside the purge's transaction and a deleted round is invisible to `list_cached_originals_for_user`; `sweep_orphan_originals` is the backstop if that order is ever broken.
- **The member can do it themselves:** **My Data → Guess Who data** on the Help & Settings panel runs the same deletes, extracted into `guess_repo.forget_member(conn, guild_id=, user_id=)` so `purge_user_data` and the button share one function (the bug #222 rule, `docs/specs/privacy_spec.md` § erasable stores). It leaves `guess_consents` alone: the opt-in is the consent evidence and `/guess optout` owns it.
- **Every erasure takes the post down** (deleting the message should clear everything from Guess). Before any row goes, `guess_cleanup_service.clear_member_media` deletes the bot's crop post for each of the member's rounds (`list_member_round_posts`; NotFound/Forbidden tolerated) and then unlinks the cached originals. The My Data button, the dashboard purge route and the departure auto-anonymise job all call it, so a departed or erased member's photo does not sit in the Guess channel after the record is gone. `list_cached_originals_for_user` matches `answer_id` as well as `submitter_id`: the deletes take rounds whose answer is the member, and a lookup by submitter alone would leave those photos to the orphan sweep.
- **Anonymisation deletes Guess records too:** the departure job and My Data → **Delete everything** both run `forget_member` inside `departure_erasure_service.anonymise_member`, in every guild the scope reaches (`member_guild_ids`), instead of rewriting the rounds to the surrogate. With the photo and post already gone, a kept round would be a hole. The operator Anonymise tab on the Member Data panel is unchanged and still rewrites them.
- **`/guess optout`** removes the role self-service; a mod removing the role in Discord works identically. Either way the `on_member_update` listener flags any of that member's **open** rounds (where they're the answer) as `answer_optout` in the DB. The round itself is not deleted or hidden: its Guess button still works, but clicking it now replies "This round can't be solved any more." (It says nothing about the answer opting out: in Guess the answer is the submitter, so that would tell the channel who had left the pool.) This flag is permanent for that round; re-adding the role later does not clear it.
  - **Leaving the server** does the same without the role loss: the cog's `on_member_remove` calls `cleanup_service.withdraw_departed_member`, which flags the member's open rounds `answer_optout`, stamps their consent withdrawn and deletes their cached originals at once, so a leaver doesn't leave rounds nobody can win or a photo on disk until the 7-day age-out. The crop already posted stays. It does not wait for, or depend on, the departure auto-anonymise (`PrivacyCog`'s own `on_member_remove`, which queues a job that later takes the post and the rows and finds no original left to unlink); it runs for every leaver whether or not that switch is on, and for a jailed or ticketed leaver too, since the photo has no consent holder either way. A leaver who rejoins keeps the flag, as with a role loss.
  - **A hold is not an opt-out.** A break, jail or inactive hold strips every role, the Guess role included, and that loss must not withdraw consent or kill the member's open rounds. The listener waits `HOLD_SETTLE_SECONDS` (jail and inactive write their row *after* the strip), re-reads the member, and ignores the loss when `member_is_held` (`roles/grant/audit_service.py`) says they're under a hold, by row or by a hold role worn. The saved roles bring the Guess role back on release. See `docs/specs/take_a_break_spec.md`.

### `/guess submit` — the crop pipeline

The submitter uploads an image (via `/guess submit <image>`, or via the sticky prompt's **Submit** form, which takes an upload or an image link). Both doors run the same role, rate-limit, type and size checks through one function, `games/guess/submit_logic.submit_refusal`, so they cannot drift. Per-user submissions are rate-limited in-memory to `guess_submit_max_per_window` per rolling `guess_submit_window_seconds` (defaults: 5 per hour); past that the bot replies "You've hit the submission limit (N per window). Please wait a bit before submitting again." The message templates the configured values (resets on bot restart). The bot then:

1. Validates MIME, dimensions (≥ configured min), and file size (≤ configured cap).
2. Saves the original to an on-disk cache, keyed by round id, retained **only** until first correct solve.
3. Runs **candidate detection** — combines NudeNet detections with a separate pose-based detector, merges adjacent different-type genital detections into a single "sex act" candidate, and re-weights scores so more "interesting" regions (genitals/breasts/buttocks) outrank incidental ones (belly/armpits). Candidates overlapping a detected face are filtered out, with a fallback to the single highest-scoring detection (even if it overlaps a face) if that would otherwise eliminate everything. **If no detector returns anything at all, the submission is not rejected**: the editor still opens with a default centered crop box and the note "Nothing was detected — frame your crop by hand, then ✓ Post." (Auto is disabled, since there are no candidates to cycle.) This is the normal path for an SFW submission with no person in it (a pet, a desk, a tattoo close-up). Pose landmarking works on clothed bodies, so an SFW photo *of a person* still produces torso/hip/thigh candidates and crops automatically.
4. Applies difficulty-tuned padding around the top candidate: **easy** = looser (more context), **medium** = moderate, **hard** = tight crop. Output is clamped to image bounds and expanded if smaller than the minimum.
5. Opens an ephemeral **crop editor**: a D-pad view (move up/down/left/right, zoom in/out, an **Auto** button that cycles through the ranked detected candidates, **✓ Post**, and a grey **Cancel**, which deletes the editor without a reply). The editor greys itself out after five idle minutes. There is no fixed re-roll cap: the submitter can nudge/zoom/cycle as many times as they like before posting. Cancelling before posting discards the submission entirely (nothing is written to the DB until Post).
6. On Post, the crop posts publicly to the guess channel as the **round card** (`🎭 Round #N`, the question, the spoilered crop, a line reading `Guesses: N • Submitted by ▒▒▒▒▒▒▒` and a **Guess** button), and the bot best-effort reposts the sticky channel prompt underneath it. The submitter's editor becomes a "✅ Round Posted" notice.
7. **If that post fails, the whole submission is rolled back** (bug #227). The round row and the cached original are both already durable by the time `guess_channel.send` runs, so a raised send would otherwise leave an open round that never posted plus a full-size original on disk. The handler deletes the original, clears its path, soft-deletes the round, writes a `post_failed` audit event and tells the submitter "❌ Couldn't Post That Round — Nothing was saved. Try again in a moment." Soft-delete rather than hard so the failure stays visible afterwards. The confession Post path does the same (no file, so the orphaned row is what it undoes). The **quest credit for submitting a round fires only after the post lands**: a rolled-back round is not a submitted round.

### Guessing

Clicking **Guess** first checks that the round hasn't been flagged `answer_optout` and that the clicker isn't the submitter, then opens an ephemeral **picker card**: the round's heading, a status line, and a string-select dropdown of opted-in members, paginated 25 per page (◀/▶ buttons appear when there's more than one page, disabled at the ends). The card says it closes after 60 seconds without a pick, and greys out when it does. Every answer to a pick (wrong, cooldown, out of guesses, correct) redraws that one card's status line rather than posting another reply; after a wrong guess the card stays open through the cooldown (capped at 14 minutes, the longest an ephemeral card can still be edited). A **🔍 Filter** button opens a modal for a text query; matches are scored (exact name match, then prefix, then substring, then subsequence) and the same select is rebuilt with the filtered/reordered results, with a **✕ Clear** button to reset. The picker's candidate list is built once, when it opens, but the guesser's no-contact partners are re-read on every pick. So a picker opened before a no-contact pair existed still lists her, and the pick is **re-scored**: on a round the guesser's **no-contact partner** is part of (she is its answer, or she submitted it), every pick is a **forced miss**, even the right one: scored wrong, written, counted toward the cap and cooldown, and answered "❌ Not it. Keep trying!" like any other miss (`no_contact.logic.guess_scores_correct`, 2026-09-30). The reachable case today is that stale-picker race (she is both answer and submitter). The submitter-differs-from-answer half of the rule is **latent**: every submit path currently makes the submitter the answer, so it guards for the day those ids diverge. Submitters cannot guess on their own rounds. The guesser is also left out of their own picker (a self-pick would burn a guess and start the cooldown), alongside their no-contact partners (`submit_logic.picker_candidates`).

**Round lifetime.** With `guess_round_lifetime_days` set above 0, the daily sweep (`_age_out_loop`) closes every open round older than that. `expiry_service.claim_expired_rounds` stamps `guess_rounds.expired_at` (migration 256) first. The sweep then **deletes the round's card from the channel** and its cached original from disk, and an `expire` event goes in the audit log. Nothing is revealed: no name, no photo, no reveal card, because members consented to a reveal only when a round is **solved**. A round is cleared once `expiry_cleared_at` is stamped, which happens when the card is deleted, is already gone (`NotFound`) or its channel no longer exists. Any other Discord error, or a restart between the claim and the delete, leaves it uncleared and the next sweep retries it (even if the dial has since gone back to 0); the original is deleted on the first attempt. Until cleared, the round keeps its persistent Guess button registered at restart (`get_unsolved_round_ids`) so a press still answers "round has ended". It is **not a solve**: `solved_at` and `solver_id` stay NULL, because the top-posters query counts any round with `solved_at` set as a solve, so nobody is credited and the leaderboard does not move. A correct pick racing the sweep loses (`mark_round_solved` requires `expired_at IS NULL`) and a stale picker or Guess button gets the "round has ended" line. Rounds flagged `answer_optout`, soft-deleted, or never posted are skipped. At the default 0 nothing expires. At 7, a round posted today closes within about a day after its seventh day. Granularity is the 24-hour sweep, not the minute. There is no submitter "Reveal now" button.

Guesses are capped per (user, round) at `guess_max_guesses_per_round` total (default 5); past that the bot replies "You're out of guesses on this round (cap: N)." with the configured cap. Below that cap, there's also a per-(user, round) cooldown, configurable (default 60 s; 0 disables it). On cooldown, the guesser sees "⏳ On cooldown — you can guess again <t:...:R>." (a Discord relative timestamp). A wrong guess gets "❌ Not it. Keep trying!".

On a **correct first solve**, the bot edits the round's card into the reveal: the original image (the full submission, not the crop) replaces the crop behind a spoiler (click-to-reveal blur), under "✅ Round #N — Solved!", then "It was **{member}**." (every submit path makes the submitter the answer, so the card names them once; a round whose two ids differ shows **Answer:** and **Submitted by:** on two lines), "Solved by **{user}**", and a subtext line "N guesses • M guessers". A round with no cached original (a confession, or a photo whose original already aged out) keeps the image it had. All three are **resolved display names**, not `<@id>` mentions: a mention on a card is resolved by the reading client from its own cache, so it shows as a bare number to anyone who hasn't seen that member (`shared/name_resolver.py`; see `components_v2_style_guide.md` § Mentions and names). The **one exception** is a submitter/answer pair on the no-contact list: there both degrade to a plain "User <id>", because naming the two together in the bot's own voice is the association the list exists to prevent. The solver is always named: a no-contact partner of either side can't have solved the round, since every pick they make on it is a forced miss. The one tell this leaves arises only in the stale-picker race: if someone else solves her round, the reveal names the member he was told "Not it" about. The on-disk original is deleted at this point. If a mod deleted the round's card while a member's picker was open, a correct pick still finishes the solve (the original is deleted, the solve is audited and the solver is told) and only the reveal edit is skipped, since there is no card left to edit. **The solved card carries no button**: the solve edit has always removed it, and an earlier revision of this spec described a "Guess late" button that the code never showed. A member whose picker was already open when someone else solved the round can still pick: a correct pick gets "⌛ Right name — but someone already solved this one." (it does not name the first solver), a wrong one "⌛ Someone solved this round while you were picking." Both close the picker, and both guesses are still recorded like any other, so the answer depends only on the round and never on who is picking (the no-contact rule).

### Leaderboard

`/guess leaderboard` takes no arguments and always posts both lists together, non-ephemeral, naming every member by resolved display name rather than `<@id>` (the lists rank *past* posters, exactly the people a reader's client is least likely to have cached): **Top Posters** (top 5 by rounds posted, tie-broken by rounds solved, each shown as "posted, solved (pct%)") and **Top Guessers** (top 5 by rounds solved as first correct guesser). Both exclude soft-deleted rounds. There is no per-user `/guess stats` command, no accuracy/streak/hardest-crop tracking, and no leaderboard category argument — none of that is implemented.

### Mod tools

`/guess round` shows a specific round to a mod: status (open / solved / deleted), submitter, answer (each as `Name (id)`, keeping a copyable identifier next to the resolved name, as the whisper mod-log cards do), difficulty, guess and unique-guesser counts, re-roll count, and the crop image, **spoilered**. For dispute resolution. `/guess delete` soft-deletes a round (message best-effort deleted, stats survive; already-deleted rounds are rejected with "Round #N is already deleted."). The web audit panel lists recent submit, delete, solve, and cap events for the guild.

Config → Guess Who → Post Submit Prompt lets an admin force an immediate repost of the sticky Submit/Help prompt message in the configured channel (normally it reposts itself automatically ~2s after the last message in the channel, debounced). Useful if the sticky prompt gets buried or its message is deleted. Re-running it when the prompt is already in that channel **edits it in place** rather than hopping it to the bottom, so a re-brand refresh doesn't move it.

The prompt runs on the shared `core.sticky.StickyPanel`, which posts the new prompt *before* deleting the old one (a failed send must not leave the channel with no prompt) and shields placements from cancellation (a cancel landing mid-send would orphan a prompt whose id was never recorded). The placer takes a per-panel `target_types` because the Guess channel may be a thread or a voice channel's text view. Placement is debounced (~2s).

### Cards (Components V2)

Every Guess surface is a Components V2 card built by a pure builder in `games/guess/layouts.py` (`docs/components_v2_style_guide.md`); nothing in the feature is an embed and nothing reads the Branding accent.

| Card | Paradigm | Notes |
|---|---|---|
| Channel prompt | 1 — hub panel | Title with a grey **Help** beside it, one sentence, one **Submit** row. Wears the guild's small lobby strip with "Guess Who" drawn in (`core.layout.lobby_art`) when a Lobby Header is uploaded; nothing draws without one. Help's doc key is **`guess`**; renaming the doc disconnects it and Help falls back to the built-in how-to. |
| Round card → reveal | 9 → 7 | The two disabled "chip" buttons the embed used as labels are one `-#` line. The reveal has no controls. Both wear the lobby strip (below). |
| Guess picker | 9, ephemeral | One card, redrawn in place. |
| Crop editor | ephemeral | The preview over the D-pad grid; **✓ Post** green, everything else (**Cancel** included) grey. |
| Confession preview, opt-in consent | 5 (decision card), ephemeral | Cancel is grey and silent on every Guess card: it closes something that was never saved. |
| Leaderboard, mod inspector, Help, one-line outcomes | 7 (receipt / notice) | The leaderboard wears the lobby strip; with the name drawn in, its heading shortens to "Leaderboard". Each leaderboard row is the name, then a `-#` line of its figures, ranked 🥇 🥈 🥉 4️⃣ 5️⃣. |

**The public cards wear the lobby strip:** the round card, its reveal and the leaderboard open on the guild's small header with "Guess Who" drawn between its leaves and close on the footer, the way a game lobby does (`core.layout.lobby_art`; the read is `games/utils/lobby_art.read_lobby_art`, unspoilered in an age-gated channel). The round card keeps its `## 🎭 Round #N` heading: the strip names the game, the heading names the round. Nothing draws until a Lobby Header is uploaded on Branding → Panel Art, and a failed art read draws nothing rather than holding up a post or a reveal. The ephemeral cards stay plain.

**The channel prompt** is a short line beside **Help** ("Guess whose photo it is.") over a full-width subtext line, because beside a button a phone keeps only ~30 columns, and the first cut's 69-character intro dropped Help under it. The picker's status line says "Pick a name from the list."; the question itself is the dropdown's placeholder, asked once.

**Spoilers are unconditional, and that is deliberate.** The crop, the revealed original, the confession card and the inspector's copy of the crop are spoilered wherever they land. The rest of the bot spoilers member media by `is_age_gated_channel()`; Guess does not ask, because the bot enforces nothing about where a round is posted (see the SFW note at the top) and a round may be NSFW in a channel that isn't flagged. The submitter's own previews (crop editor, confession preview) are ephemeral and unspoilered. `tests/games/test_guess_layouts.py::test_member_submitted_media_is_always_spoilered` fails if a card drops it.

**Rounds posted before the conversion convert on their next edit.** Prod had 32 open rounds posted as embeds. Their Guess buttons keep working: the button's custom id (`guess_guess:<round>`) did not change, and the first wrong guess (the counter bump) or the solve edits the message into the new card, keeping the crop it already carries by referencing the existing upload. If Discord refuses that reference, the bot downloads the crop and uploads it again under the same name (`_edit_round_card`). A round nobody touches stays an embed.

The round's stored `crop_url` is read from the posted card's gallery, not `message.attachments`: an upload a V2 component references is hidden from that array.

## Permissions

- `/guess submit`, `/guess confess`: require the consent role to be configured **and** held by the caller; there is no "Everyone if unset" fallback; an unconfigured role blocks both.
- `/guess optin`: anyone can run it, but it errors if the consent role isn't configured. No opt-out equivalent exists as a command.
- `/guess leaderboard`: Everyone, no arguments.
- `/guess delete` on your own round: submitter; on someone else's round: Mod (`manage_guild`). This check is enforced in code only; the command isn't hidden from non-mods client-side.
- `/guess round`: Mod (`manage_guild`), and also hidden from non-mods in the Discord UI via `default_permissions`.
- Submitter cannot guess on their own round.

## User-visible errors

| When | The user sees |
|---|---|
| `/guess submit` / `/guess confess` with the Guess role unconfigured | "Guess role is not configured. Ask an admin to set it in the web dashboard." (confess: "Guess is not fully configured...", since it also requires the channel) |
| `/guess submit` without the consent role | "You need the Guess role to submit." |
| Submission rate limit hit (per user, in-memory, config-backed) | "You've hit the submission limit (N per window). Please wait a bit before submitting again." Templates `guess_submit_max_per_window` / `guess_submit_window_seconds` |
| Image not an image / too small / too large | "Please submit an image file.", "Image too small. Minimum dimension is Npx.", or "Image too large. Maximum is N MB." |
| No detections found in the image | **Not an error**: the crop editor opens with a manual default box: "Nothing was detected — frame your crop by hand, then ✓ Post." |
| Submit form sent with neither a photo nor a link | "Add a photo or paste an image link." |
| Submitter clicks Guess on their own round | "You can't guess on your own round." |
| Guess on a round the answer opted out of | "This round can't be solved any more." |
| Guess on a round that has expired | "⌛ This round has ended." |
| Guess cap hit (one user exhausts `guess_max_guesses_per_round` on one round) | "You're out of guesses on this round (cap: N)." Templates the configured cap |
| Guess on cooldown | "⏳ On cooldown — you can guess again <t:...:R>." |
| Wrong guess | "❌ Not it. Keep trying!" |
| Late correct guess (already solved) | "⌛ Right name — but someone already solved this one." (does not name the first solver) |
| Late wrong guess (solved while the picker was open) | "⌛ Someone solved this round while you were picking." |
| `/guess delete` by someone other than submitter or mod | "Only the submitter or a mod can delete this round." |
| `/guess delete` on an already-deleted round | "Round #N is already deleted." |
| `/guess round` by a non-mod | "Only mods (manage_guild permission) can inspect rounds." |
| `/guess confess` with disallowed content | "That confession contains disallowed content. Please rephrase." |

## Non-goals

- **Submitting on behalf of another member.** Submitter is always the answer.
- **Per-round difficulty override.** Uses the guild default only.
- **Cross-guild stats / global leaderboards.**
- **Image moderation beyond the built-in detector.** No age verification: Discord ToS and the consent role are the gate.
- **Alt-account collusion detection.**
- **Round reuse / throwback rounds** do not exist (migration 184 dropped the four reuse columns on `guess_rounds`). `idx_guess_rounds_reuse` keeps its old name but indexes `(guild_id, submitter_id, image_hash)`; it is the duplicate-image guard.
- **Web crop override UI.**

## Configuration

| Key | Default | Purpose |
|---|---|---|
| `guess_role_id` | unset | Consent / eligibility role. Unlike some other guild features, unset does **not** open submit/confess to Everyone; it blocks both until an admin configures it |
| `guess_channel_id` | unset | Where crops post and modals launch from |
| `guess_guess_cooldown_seconds` | `60` | Per-user, per-round cooldown between guesses (`0` disables it). 0-3600; outside that the save is refused, not rounded |
| `guess_max_guesses_per_round` | `5` | Per-user, per-round total guess cap |
| `guess_round_lifetime_days` | `0` | Days an open round stays guessable before it expires (see Round lifetime). `0` = never; ships dark |
| `guess_submit_max_per_window` | `5` | Per-user submission rate limit: max submissions per rolling window (in-memory, resets on restart) |
| `guess_submit_window_seconds` | `3600` | Length of the submission rate-limit window |
| `guess_crop_difficulty` | `medium` | `easy` / `medium` / `hard`; controls crop editor padding, not a per-round choice |
| `guess_min_image_dimension_px` | `400` | Reject submissions smaller than this on either axis |
| `guess_max_image_size_mb` | `10` | Hard cap on upload size |
| `guess_prompt_message_id` | unset | Persistent prompt message at the bottom of the channel |
| `guess_prompt_channel_id` | unset | Where that message actually **is**. The placer deletes the old prompt through this channel, so pairing a stale message id with a repointed `guess_channel_id` would aim the delete at the wrong channel and strand the old prompt with its buttons live. Repointing the Guess channel therefore leaves the prompt where it is until the next round (or an explicit repost) moves it. **Legacy rows fall back to `guess_channel_id`**: a row with a message id and no channel id predates this key, when the prompt was always posted into `guess_channel_id` |

## Inactivity nudge (removed)

There is no recurring ping chasing members back to an open round: no loop,
service or dial. A game people find on their own does not need one, and a
nudge would have to pick the right round (an abandoned one must not draw a
"quiet for 2704 hours" ping).

The dead config keys (`guess_inactivity_ping_hours`,
`guess_last_nudged_round_id`) were deleted from existing guilds by migration
216, and both sit in `settings_registry.DEAD_KEYS` so the Config Advisor cannot
offer the dial for a reader that no longer exists. Migration 217 removed the
loop's `guess_last_nudge_at` clock and the long-dead `veil_role_id`.

With the nudge gone **nothing pings the Guess role at all.** `guess_role_id`
is a gate, not an audience: it decides who may submit and who is pickable as an
answer (`_has_guess_role`), and new rounds post without a `content=` mention.
The role is self-serve — members add and drop it themselves — so the only thing
that should ever change because of it is eligibility.

## Stored data

Rounds (one row per submission with crop / answer / solver / counts), guesses (one row per guess attempt), and an audit log (submit / delete / solve / guess-cap / `post_failed` events) per guild.

**There is no opt-in table.** Eligibility (who may submit, and who is pickable as an answer) is derived live from Discord role membership (`guess_role.members`), and that is the single source of truth. (An unused `guess_optins` table was dropped in migration 136.) Deriving eligibility from the role is what makes a mod removing the role take effect immediately, and it's why the `answer_optout` round flag (see "Consent and opt-in") is the only persisted consent state.

Filesystem cache: original submissions live in a per-round file on disk **only until first correct solve**, at which point the file is deleted and the path cleared. Crops live on disk for the round's lifetime and are deleted on round deletion. The Discord CDN URL of the posted crop is the canonical reference if the local cache is missing.

Three things delete a cached original, all in `games/guess/cleanup_service.py`: the solve (above), the **age-out** (`ORIGINAL_MAX_AGE_DAYS = 7`, open rounds only) and the **orphan sweep**. The first two find their work by reading `guess_rounds`, so neither can see a file no row points at (a row deleted outside the bot, or a file written in the instant before its path was recorded). The sweep reads the directory instead and deletes any file in `guess_cache/orig/` that no round records a path to, skipping anything written in the last `ORPHAN_GRACE_SECONDS` (1 h) so it cannot race a submission that is posting right now. A file counts as referenced by resolved path **or** bare filename: recorded paths are relative and resolve against the process cwd, and the name match is the guard against a cwd difference turning the sweep loose on live rounds' photos. Both run in the cog's 24-hour `_age_out_loop`.

The original image is never reused for a future round and never published unspoilered. This retention policy is disclosed in the `/guess optin` consent view, before the role is granted.
