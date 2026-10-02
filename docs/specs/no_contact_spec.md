# No-Contact List

**Status: Reference.** Describes the code as shipped.

Pairs of members the bot will never put in contact, in either direction,
through any feature that can carry contact between two people.

## Why this exists

Blocking someone in Discord stops them messaging you directly. It does not
stop them reaching you *through a bot*: an anonymous whisper, an AMA question
aimed at you, a reply to your confession are all the bot delivering something
on their behalf, and Discord's block never sees them. The motivating incident
was exactly that: a member blocked someone, and he could still reach her
through the anonymous features.

This is a safety feature. Two rules follow from that and are worth stating
before any implementation detail:

1. **Enforcement is not a preference.** Every surface that can relay contact
   is gated. A guarantee that holds in three features and quietly fails in the
   other three is not a guarantee.
2. **The blocked party must never learn an entry exists.** Telling him he has
   been blocked confirms she acted against him, which is the escalation the
   feature exists to prevent. That constraint shapes almost every design
   decision below.

## Storage

`no_contact_pairs` (migration 146), one row per pair:

| Column | Meaning |
|---|---|
| `guild_id`, `user_low`, `user_high` | The pair, order-independent (`user_low = min(a, b)`) |
| `protected_user_id` | Who the entry protects, and the only member who may lift it. `NULL` = mutual separation, liftable only by staff |
| `created_by` | The member, or the mod who acted |
| `reason` | Moderator-visible only; the other party never sees it |
| `created_at` | — |

Also `no_contact_settings` (alert channel + ping role, per guild) and
`no_contact_events` (blocked attempts and mention/reply alerts, staff-visible).

### Why not `dm_consent_pairs`

That table has the same `(user_low, user_high)` shape and was the obvious
candidate. Three reasons it cannot be used:

1. `dm_perms_service.normalize_request_type()` coerces anything that isn't
   `'friend'` to `'dm'`, so a `rel_type='no_contact'` insert would silently
   store `'dm'`.
2. `load_consent_pairs()` ignores `rel_type` entirely and adds every row to
   the mutual-consent set in both orderings. A no-contact row there would not
   merely fail to block; it would **grant the blocked party a DM consent
   pair.** Exactly inverted.
3. Its primary key allows one row per pair, so it cannot represent "these two
   had a consent pair, and now they have a no-contact"; the insert would
   overwrite the consent row and lose it.

The *convention* is reused; the table is not.

### Why not an in-memory cache

`dm_perms` caches consent pairs at boot. That is safe there because a stale
cache fails toward "not connected". A stale no-contact cache fails the other
way: it lets a blocked member through until the next restart. The gates are
low-frequency, so every check is a direct indexed read.

## Gated surfaces

| Surface | What is gated | Where |
|---|---|---|
| Whisper | Send, and replies to whispers sent before the pair existed. Also the guess feedback to the sender: a wrong guess that lands on a blocked partner is reported as "someone" rather than by name, the same line any wrong guess reads as. The line is batched, and the pair is checked when the batch is sent; the row is deliberately **not** dropped by `dm_batch`'s actor re-check (`recheck_actor=False`), because a missing line is itself a tell | `whisper_cog._send_impl`, `WhisperReplyModal`, `_deliver_guess_batch` |
| AMA | Directed questions (both modes), the answer-DM back to the asker (checked when queued and again when its batch is sent), and the panelist picker | `games_ama_cog`, `dm.batch.service.take_due` |
| Confessions | Anonymous replies (`parent_author_id` is the member replied to). Also the reply DM for a *named* reply (a message typed in a confession's thread, or Discord's Reply on it): only the DM to the author is dropped, the reply stays up. The named-reply DM is checked again when its batch is sent (`dm_batch`); an anonymous reply's queued DM carries no actor and is **not** re-checked, since dropping it would identify the replier to the confessor; the reply-time refusal is its gate | `confessions_cog.ReplyModal`, `confessions_cog._on_message_typed_reply`, `dm.batch.service.take_due` |
| Needle (Auto-Thread) | The reply DM to the member who started the thread: only the DM is dropped, the reply stays up. Checked when the reply is queued and again when the batched DM is sent, so a pair added inside the ten-minute window drops that replier from the DM (and a DM left with nobody in it is not sent) | `needle_cog._notify_starter`; `dm.batch.service.take_due` |
| Guess Who | Guesses on the other's rounds (every pick on a round she is the answer to or submitted is a forced miss, scored wrong even when right), the candidate picker, submissions where `answer_id` differs from `submitter_id`, and the paired reveal | `guess_cog` |
| Pen Pals | Matching, via `_is_blocked_pair`. Also the reply reminder and the closing DM, re-checked when they fire since a pair can land mid-session: the reminder is skipped, and the closing DM says "your pen pal" instead of the partner's name, the same words it uses when the partner has left | `pen_pals_cog`, `_reply_reminder_due`, `_notify_expiry` |
| Voice Master | Room permissions, via `effective_blocked`. **`/voice knock`**: the knocker gets the ordinary "Knock sent" line and the owner receives nothing, which is indistinguishable from a knock left unanswered. **`/voice invite`** (and the panel's invite picker): the owner gets the ordinary receipt and a `remember` still saves to their own trust list, but the invitee gets no permission and no DM, and no `vm_invite` audit row is written. Both **record an attempt** (surfaces `voice_knock`, `voice_invite`). A knock delivered before the pair existed, accepted afterwards, opens nothing and answers with the ordinary "The requester is no longer in this server." line | `voice_master_service`; `voice_master_commands.post_knock_request`, `_apply_invite`, `_KnockResponseView._resolve`; the permission syncs in `voice_master_commands` and `voice_master_cog._handle_hub_join` |
| DM requests | Consent suppressed; new requests refused. The settings panel's **Your Connections** list leaves a no-contact partner out (the row is suppressed, not deleted), so the member sees the same list they would if that connection had never been made | `dm_perms_cog`, `dm_perms_service.load_member_connections` |
| Risky Rolls | The dice: a draw that would seat a pair as asker/answerer is redrawn, and a round that cannot be made safe refuses to close. Plus the 69 room ping, and the payoff chaser: the bank fallback and the answerer re-ping are re-checked against the list when they fire, since it can change in the hours after the draw: a pair it now forbids is skipped silently (`fallback_blocked`, `posted_chase_blocked`) | `risky_roll/logic.py`, `risky_roll/views.py` |
| Economy transfers | `/bank pay`'s public receipt *and* the recipient's notification; `/bank gift`'s notification. The money and the perk still move. The notification is a batched `econ_notice` line (`dm_batching_spec.md`), so a rule added inside the ten-minute window also drops it at send | `economy/transfers.py`, `economy_cog.finalize_pay` / `finalize_gift` |
| Community Bounty | Three deliveries, all keyed on the **poster** and all leaving the money alone. (1) The winner's "you were awarded" DM, which names the bounty and links its card; the card says who posted it. (2) The cancellation DM to each refunded contributor, same payload, same reason: the refund lands, the note doesn't, and the resolver's "refunded N contributor(s)" still counts everyone. The note is a batched `econ_notice` line that names nobody; the poster rides as its actor, so a rule added before the batch is sent drops it too. (3) An **awarded card degrades both names to a bare `User <id>`**: a public card printing "Posted by X / Winner Y" in the bot's own voice is the association the list exists to prevent, the same call `games/guess/layouts.reveal_names` makes. Keyed on the pair rather than on who pressed the button, since a mod's award leaks the same link | `economy_bounty_service.should_notify_winner` / `notifiable_contributors` / `award_pair_no_contact`, `bounty_views._handle_award` / `_handle_cancel` / `_card_name_fn` |
| Spin the Compliment | The pairing: a blocked pair is a forbidden edge of the derangement in both directions. A pool with no valid pairing left is refused with the ordinary "Need at least 2 players in the pool!" | `games_compliment/logic.generate_pairings`, `games/utils/derangement.py`, `games_compliment_cog` |
| Marry-Fornicate-Kiss | The assignments: blocked members are dropped from each other's three-name sample; a pool that then can't give everyone three names draws nothing and gets the ordinary "need at least 4 players" refusal (a shorter list would name the pair, since every ordinary pool of four gives everyone three) | `games_mfk/logic.assign_targets`, `games_mfk_cog` |
| Clapback | The bracket: a blocked pair is never a head-to-head matchup. Both bracket functions take the round's pairs; when the only pairing left would seat the pair, one of the two is that round's bye, paid the round average and announced like any bye. A member kept apart from every other player doesn't count toward `MIN_PLAYERS`, so Start refuses with the ordinary "Need at least 3 players" line | `games_clapback/logic.pick_round_bye` / `create_matchups` / `playable_players`, `games_clapback_cog._forbidden_pairs` |
| Most Likely To | The vote: a blocked voter→target pick is dropped, not stored. The voter gets the ordinary "✅ Voted for …" ack, the results card shows counts only, and no event row is written | `games_mlt/logic.record_vote`, `games_mlt_cog` |
| Truth or Dare (traditional) | **Ask Question**: the presser's blocked partners are excluded from the target pool before the least-asked weighting (keyed on the presser, not the host, since a mod can ask too). When only they remain, the press gets the ordinary "All player/category combinations have been asked!" | `games_traditional/logic.select_next_question_target`, `games_traditional_cog` |
| Duel challenge | `/games <duel> challenge @user`: refused with the existing "You two already have a game in progress." line, before the rate limit so it costs no strike. **Records an attempt** (surface `duel_challenge`) | `duels/base_duel.py`, `base_game.BaseGame._blocked_pair` |
| Group-game lobby join | A joiner who holds a pair with anyone already seated (host included) gets the lobby's own "❌ You're on cooldown for this game — try again later." (the timeless form of the cooldown line; a real cooldown names the time left). The **Rematch** press pings the old roster to the new lobby; the presser's blocked partners are left off that ping, which reads like the omission of anyone who has left the server | `duels/base_game.py`, `BaseGame._blocked_with_any`, `BaseGame._handle_rematch` |
| Name the Loser | The winner's press and the modal submit are both refused with the "already serving a nickname sentence" line; the win stands and the game concludes at `NO_NICK_SET` with no rename | `duels/base_game.py`, `BaseGame._refuse_rename_across_pair` |
| Meadow Mahjong | Taking a seat: a joiner who holds a pair with anyone already seated gets the table's ordinary stale-tap failure, "That didn't go through — the table moved on before your tap landed.", the same line a genuine race produces, so the refusal is indistinguishable by construction. House-bot seats (negative ids) match nothing | `games/mahjong/mahjong_service.py`, `MahjongService._tx_join`, `STALE_TABLE` |

Features that hold their own connection (Pen Pals' matching pass, Voice
Master's permission build) consult the list through
`is_no_contact_conn` / `no_contact_partners_conn` rather than querying
`no_contact_pairs` directly, so the table's shape stays owned by one module:
adding an expiry or soft-delete column is a change in one place, not a grep.

Only the first four surfaces, the duel challenge, and Voice Master's knock and
invite record an **attempt** event. The rest are gated without one. For Pen
Pals, Voice Master's permission syncs and DM requests that is because the gate extends an existing predicate running inside
matching loops and permission syncs; there is no single moment there that
means "he tried", and recording per iteration would bury the real attempts.
Risky Rolls is the stronger case: nobody ever submits anything aimed at a
blocked person, because the pairing never forms. He pressed a dice button aimed
at nobody, and a log line saying so would be a record of the bot's own
arithmetic, not of an attempt. Spin the Compliment, MFK, a lobby join and Name
the Loser are the same shape (the game's own arithmetic put two people
together, nobody aimed anything), and an MLT vote or a Traditional Ask press
is one click in a game whose whole point is picking someone, so logging it
would fill the mod feed with ordinary play. The duel challenge is the
exception because it is the one game surface where a member types the other's
name: `A picks B` on purpose, so it is logged with surface `duel_challenge`.
Economy transfers are the weakest case of all for a log line: paying someone
is not an attempt to contact them, and `econ_ledger` already records the
transfer under the mods' eyes. They are enforced just as strictly; they
simply produce no log lines. The named-reply DMs (Confessions' typed replies,
Needle's thread replies) are the same: replying in a public thread is ordinary
chat, not a message aimed at the other member, so the gate uses the
non-recording `is_no_contact` and logs nothing.

`dm_consent_pairs` rows are **suppressed, not deleted**: `_is_mutual` returns
False for a no-contact pair while the row and its provenance survive for a mod
reviewing the case. The consequence: if a no-contact entry is later
lifted, the old consent becomes live again without either party re-consenting.

## The disclosure rules

**Gate the narrowest thing that makes contact.** Prefer suppressing the
delivery to refusing the action: `/bank pay` moves the money and drops the
recipient's DM; Risky Rolls gates the die, not the pairing. A blanket block
(the pair can't share a game, a lobby, a channel) is usually too wide. Before
proposing one, say what it would kill for everyone else in the room. The
sections below are that rule applied one surface at a time.

The blocked party sees a refusal that is indistinguishable from either an
ordinary success or an ordinary failure, chosen per surface by whether
anything would have been *publicly visible*:

- **Fake success** where nothing visible was going to happen: whisper send and
  reply ("Whisper delivered." / "Reply delivered anonymously."), screened AMA
  questions (they would have gone to the host's DMs), confession replies (he
  does not know whose confession it is, so a reply that fails to appear cannot
  be attributed to anyone), and the AMA answer-DM.
- **An ordinary failure** where fake success would leave a hole he could see:
  an *unfiltered* AMA question would have been posted to the channel, and he
  chose the panelist, so "posted!" followed by nothing appearing points
  straight at her. The existing stale-target error is returned instead, a
  believable race that explains the absence without involving her.
- **The ordinary wrong-guess line** for Guess Who, which is not even a lie:
  his partner is filtered out of his candidate picker, so whatever he selected
  really was not the answer.
- **A genuine success** for `/bank pay` and `/bank gift`: the transfer runs and
  the sender's ephemeral receipt is the real one, balance and all. Only the
  recipient's DM (the part carrying his name and his memo) is withheld. See
  "Economy transfers" below for why the money is not held back with it.
- **A refusal the surface already makes** for the party games and duels: a
  compliment pool that cannot be paired safely gets the "need at least 2
  players" line, a duel challenge gets "already have a game in progress", a
  lobby join gets "on cooldown", Name the Loser gets "already serving a
  sentence", Traditional's Ask gets "all combinations asked". Each is an
  outcome the presser has met before and cannot pin on anyone; none is a new
  string. See "Party games and duels" below for the choice at each surface.
- **Fake success with nothing to fake** for MLT: the blocked vote is dropped
  and the voter gets the same "✅ Voted" ack as everyone else, and since the
  results card shows counts only there is no hole to notice. MFK is the same
  in the other direction: the blocked member is simply absent from the
  sample, and a pool that can't then give everyone three names is refused
  with the ordinary too-few-players line rather than shown shorter lists
  (every ordinary pool of four gives everyone three, so two short rows
  would name the pair).

Three further leaks are closed away from the send paths:

- **The No Contact card's list** (the Help & Settings hub, and `/info`) hides entries that protect the *other*
  party (`is_visible_to` via `no_contact_logic.own_entries`).
- **A remove that finds nothing** (`no_contact_service.member_remove`) answers "there's no entry between you two" rather
  than "you can't remove this", because a permission refusal confirms
  something exists to remove.
- **Voice Master** splits `list_blocked` (what the owner *sees*) from
  `effective_blocked` (what is *enforced*). Folding no-contact partners into
  the visible list would show him her name in a blocklist he never set.

A duplicate `add_pair` never rewrites `reason` or `created_by`, and callers
must not distinguish it from a fresh insert: "that already exists" would tell
him she had added one first.

It does, however, **escalate `protected_user_id` to NULL when the second party
adds the same pair.** Leaving the row untouched was unsafe: if he adds the pair
first, the row records him as protected, and her later add would be a silent
no-op, leaving him holding the only key to her protection, free to lift it,
while she could neither remove it nor even see that it existed. When both
parties have asked for the separation, neither gets to undo it alone.
Escalation is one-way, so a pair can never be walked back down to
single-party control.

Each of these response strings is a **module-level constant shared by the
ordinary path and the gated one** (`whisper_service.SENT_CONFIRMATION`,
`games_ama_cog.STALE_PANEL_TEXT`, `guess_cog.WRONG_GUESS_TEXT`, …). They were
briefly duplicated literals, which is the wrong shape for this: copy edits are
the most common change in this repo, and rewording one branch and not the other
would silently turn the refusal into a tell with no test failing.

### Guess Who: the guess is written, not discarded

A blocked member's guess on the other's round runs its ordinary course (row
written, counter bumped, quest fired), and only the **candidate filter** stops
him reaching her. That is deliberate, and it replaced an earlier design that
discarded the guess.

Discarding was worse than useless. The per-round cap and the cooldown both
count rows in `guess_guesses`, so a guess that was never written left him
uncapped and never on cooldown: he could hold the button down on her round and
get "Not it" forever, while every other round in the server put him on cooldown
after one guess. That is a tell anyone bored enough finds in a minute, and it
wrote a log row per click. Since she is absent from his picker he cannot name
her, so the guess was always going to be wrong; it costs her nothing, and
being distinguishable would have cost her everything.

The filter is applied when the picker opens, but his partners are re-read on
every pick, so a picker opened before the pair existed still lists her. That
is the reachable hole: he picks her, solves the round, and the reveal names him
as the one who found her. So every pick on a round she is part of — the answer
or the submitter — is a **forced miss** (`no_contact.logic.guess_scores_correct`,
2026-09-30): scored wrong, but written, capped, cooled down and answered "Not
it" exactly like any other miss. The submitter-differs-from-answer half is
**latent**: every submit path makes the submitter the answer today, so it
guards for the day those ids diverge (he could then name a round's answer, with
her beside them as submitter, which the filter alone never covered).

The accepted tell arises only in the stale-picker race: if someone else later
solves her round, the reveal names the member he was told "Not it" about. A
fresh picker never offers her, so he cannot make that deduction. The pair still
never meets, since he can never solve a round she is part of.

One attempt event is still recorded per round, on his first guess only.

The same reasoning applies to whisper replies: a blocked reply **writes its
row** (and is never delivered or mod-logged), because `_do_count_replies` is
what enforces the one-reply-per-whisper cap. Skipping the write let a second
press succeed where a genuine one returns "already replied".

### Economy transfers: the money moves, the note does not

Refusing the payment was the obvious design and is the wrong one. Every other
gate here hides behind something the blocked party cannot observe; a refused
transfer leaves his balance untouched, and `/bank` will tell him so. Paying one
member and having the coins vanish while paying anyone else works is a
one-command probe.

So the transfer runs its ordinary course (the same reasoning as Guess Who's
written-not-discarded guess), and only `notify_member` is skipped. The
recipient gets coins with nothing authored by him attached: no name, no memo,
no bank-channel fallback post. The cost, accepted knowingly, is that she
receives money she cannot account for; that is quieter than any refusal shape
available, and mods can still read `econ_ledger`.

`/bank gift` is the same call and the same answer, minus the public receipt it
never had. `pay_disclosure` returns both decisions together so the receipt half
and the notification half cannot drift apart, as they once did (the public receipt was gated and the DM was not).

### Risky Rolls: the dice are nudged, not the outcome

Every other gate here hides behind something private. Guess Who filters a
picker only the guesser sees; AMA screens a question before it is posted;
Whisper fakes a delivery nobody could observe. Risky Rolls has no private
moment at all: `/risky start` opens a round in a public channel, everyone
presses Roll, and the highest unique roll asks a question of the lowest with
the whole roster of names and numbers in the embed. The room watched the dice
decide. There is nowhere in that sequence to refuse something quietly.

So the gate moved earlier than the contact: **to the draw itself.** When a
value would seat a no-contact pair as asker and answerer, it is redrawn before
it exists. Nothing is refused, so there is no refusal to make
indistinguishable from a success: the core rule is satisfied by there being
nothing to distinguish. Nobody can audit a number that was never generated,
where a re-*targeted* pairing would have been checkable against the roster
anyone can read.

Four seats, not two. A 100 makes the second-lowest a recipient of the winner's
question and a 1 makes the second-highest an asker aimed at the loser, so the
safe-value test covers every directed asker→answerer edge the round could
produce, not just (winner, loser).

**Two layers.** `choose_roll` is avoidance; the check in `close_round` (the
host panel's Close Round; an older board's `close_button`
only points there) is the guarantee. The nudge cannot see the future: a round can be doomed the moment
the second member of a pair rolls (they are the only two players so far, and
every value makes one of them highest and the other lowest) and then *rescued*
by a third player rolling above both. The close-time check is what actually
holds, and the nudge is what stops it having to fire.

**69 is excluded from the redraw pool**, and this is the subtle part. A room
question has no directed edge, so when the pair are the only players in the
round 69 is the *only* safe value. Picking uniformly from the safe set would
therefore make the second of them to roll come up 69 nearly every time, which
is a far louder tell than the pairing it hides. The draw is honest first and
redrawn only on a collision, which leaves 69 at its true odds and lets a
doomed round fall through to the close check instead.

**The special-roll odds dials don't change any of this.** An admin can make 69,
100 and 1 rarer or commoner than 1 in 100 (`risky_roll_spec.md` §Special-roll
odds). The redraw draws with the *same* face odds as the natural roll,
restricted to the safe faces, so it is never a differently shaped pool. 69 in
particular stays exact for a pair: a natural 69 is always safe and always kept,
and a redraw only ever follows a non-69, so a member of a pair rolls 69 at
precisely the dialled rate whatever it is set to. What *does* scale with the
dials is the existing, smaller bias on 100 and 1: a pair member whose natural
100 or 1 would seat the pair gets redrawn, so they land those numbers a little
less often than everyone else. At 1 in 100 that is already true and invisible;
dialled up to 1 in 10 it is larger, though still only visible across many
rounds containing that pair.

**When no value is safe**, the round takes the ordinary "at least 2 players
must roll" refusal (the same constant, `views.NOT_ENOUGH_TEXT`, that a
genuinely short round gets), and the auto-close path uses the matching
`AUTO_CLOSE_NOT_ENOUGH_TEXT`. The cost is real and worth stating: a round with
eight people in it can die because two of them ended up in those seats. The
nudge makes that rare; it does not make it impossible.

**The 69 room question is not directed contact** and posts intact. She is
dropped from its `@`-mention list, and the thread stays public, so she can read
it exactly as she can read anything else he says in the channel, which
no-contact has never tried to prevent.

The dice are genuinely biased for that pair, which is unauditable and
therefore safe, but it is a real change to a game of chance and belongs in
writing rather than in folklore.

### Party games and duels: ordinary refusals borrowed, not invented

Compliment, MFK, MLT, Traditional's Ask Question, and every game on
`BaseGame` (the three duels and the three group games) carry the gate (a sweep had found rule 1 quietly failing in all of them). Each
uses a refusal the surface *already* produces; the rule of thumb is "pick the
ordinary outcome nobody else in the room can check", because a lie the card
contradicts is a tell:

- **Compliment / MFK move the gate to the draw**, like Risky Rolls: a blocked
  pair is a forbidden edge of the derangement (both directions) and is dropped
  from each other's MFK sample. `random_derangement` solves the constrained
  case constructively (randomised backtracking with a forward check) rather
  than re-rolling Sattolo draws, because a bounded retry cannot tell "unlucky"
  from "impossible" and Sattolo's single-cycle shape can never produce some
  valid arrangements. When no valid pairing exists the helper returns `{}`,
  the same answer as a one-player pool, so the cog's "Need at least 2 players
  in the pool!" covers both and nobody learns which fired.
- **Clapback puts the gate in the bracket** (the vote and reveal cards would
  otherwise place a pair's answers side by
  side under both names; with three players the round-robin *guarantees* it).
  `pick_round_bye` and `create_matchups` both take the round's no-contact
  pairs (`no_contact_pairs_among`, read fresh at each use because the service
  keeps no cache on purpose) and never seat one. Everything the gate does is a
  **bye**, and Clapback already pays and announces byes: a player blocked from
  every other submitter is the bye; three players who include a pair bench one
  of the two (fewest-byes-first between them, so it alternates) instead of the
  round-robin; an odd field's rotation bye is chosen so the rest can still be
  paired; and the pairing itself is a constrained draw that keeps the
  duplicate-answer avoidance. A round the gate leaves with nothing safe to vote
  on is skipped with the ordinary "Not enough answers this round" line. At
  Start, `playable_players` drops anyone with no allowed opponent before the
  `MIN_PLAYERS` check, and the refusal is the ordinary short-lobby line with
  the *roster* count: a lobby of three where one member holds pairs with both
  others reads "Need at least 3 players … Currently: 3", which is contradictory
  but identical to what the host would see if the count were right, and the
  only alternative was a lobby where one player sits out every round. Known
  soft tell, accepted: a pair in a three-player game sees a bye in a game size
  that otherwise never has one.
- **MLT drops the vote**: `record_vote` skips a blocked pick and hands back the
  same `changed` flag an ordinary vote would, so the ephemeral ack is
  byte-identical. The results card shows counts, so a missing vote is
  invisible; a lone voter's dropped pick simply reads as "No votes cast."
- **Traditional excludes before it weights**: the presser's partners leave the
  target pool *before* the least-asked filter, so a blocked player's low count
  can never pull them back in. Exhaustion reuses the existing "All
  player/category combinations have been asked!" reply.
- **Duel challenge** borrows the pair-already-playing line, placed after the
  guild-wide enabled/allowed checks (so "game in progress" is believable) and
  before the rate limit (so a refusal costs no strike).
- **Lobby join** borrows the cooldown line rather than "full" or "no longer
  open": a cooldown is a private condition nobody else can check, where the
  lobby card visibly contradicts the other two. It runs before the nickname
  preflight so a refused joiner never reaches it.
- **Name the Loser** borrows the sentence-in-progress line, the one place the
  spec accepts a small lie the loser could in theory disprove, because the
  alternative was letting the winner compose a name for someone they are kept
  apart from. The win stands (`NO_NICK_SET`), and the check runs both at the
  press and again under the lock on modal submit, so a modal opened before the
  pair existed still applies no rename. `_sentence_in_progress_copy` is shared
  with the genuine sentence path so the two strings can never drift apart.

## Alerts

Fires when one member of a pair `@mention`s **or replies to** the other. The
reply trigger is not redundant: a Discord reply with the ping switched off
still lands in front of the person replied to and never appears in
`message_mentions`. Read off the live message, with a `messages.reply_to_id`
fallback when Discord did not resolve the reference.

Alerts go to a dashboard-configured channel and ping a dashboard-configured
role. They carry a jump link, channel, and timestamp, and **no** message text:
the bot keeps none (`messages.content` was dropped by migration 228), and
storing it to serve this feature would cost every other member's privacy. Enforcement does not depend on alerts being configured.

**The protected member is never notified**, whether of alerts or of blocked
attempts. She already receives a real `@mention` by definition, and a
notification on every attempt would hand him an indirect way to distress her.
Attempts are recorded for staff, who can act on a pattern.

## Management

**Member self-service:** the **No Contact** card on the
[Help & Settings hub](server_features_spec.md) (also behind `/info`'s No contact
button): the member's own list, **Add Someone** (user select + optional reason)
and **Remove** beside each entry they may lift, all ephemeral. There is no
`/nocontact` command; the copy and
the indistinguishability rules live in `no_contact_logic` /
`no_contact_service` (`member_add`, `member_remove`, `member_entries`) so they
are tested below the Discord layer.

This is a deliberate exception to the project's config-belongs-on-the-web
rule, and the reasoning should survive the next command audit: that rule
governs *admin/server settings*, and the same paragraph of CLAUDE.md reserves
Discord for *member self-service and mod actions*. A member protecting
themselves is self-service. Routing it through a dashboard, or a moderator,
puts the highest barrier exactly where someone is least willing to explain
themselves. The hub's No Contact row is one tap from the channel, a picker away from
done.

**Moderator dashboard:** Moderation → Queues & Workflows → No-Contact List. Adds entries on a
member's behalf (third-party reports, or someone who will not file it
themselves), shows the whole list and the event log, and sets the alert
channel and role. Moderators can remove any entry.

Removal is restricted to the protected member plus staff, deliberately
asymmetric with adding. Adding is the safe direction; removal is the one a
harasser benefits from, and the one he might pressure her into.

## Tests

- `tests/no_contact/test_no_contact_logic.py`: pure decisions: removal authorisation,
  disclosure, alert triggers, the fake-success contract.
- `tests/no_contact/test_no_contact_service.py`: storage, the gate helper, and the
  cross-feature enforcement points (Pen Pals, Voice Master, DM consent),
  including that the *visible* voice blocklist stays clean.
- `tests/games/test_guess_no_contact.py`: wiring: the gate is actually called,
  and a blocked guess is neither recorded nor distinguishable.
- `tests/games/test_risky_roll_no_contact.py`: the four contact moments, the redraw,
  and two property tests that carry the design: that the predicted edge set
  never under-reports what a real resolution produces (the predicate restates
  `resolve`'s seat rules, so it can drift), and that a later roll can clear a
  collision but never create one (which is why the draw only has to look at the
  round in front of it).
- `tests/games/test_risky_roll_cog_no_contact.py`: wiring: the draw consults the list,
  and an unsafe round refuses to close *before* `resolve` runs.
- `tests/games/test_derangement.py`: a forbidden pair never appears in either
  direction, impossible constraints return `{}`, and the constrained result is
  still random.
- `tests/games/test_games_compliment_logic.py`, `tests/games/test_games_mfk_logic.py`,
  `tests/games/test_games_mlt_logic.py`, `tests/games/test_games_traditional_logic.py`:
  each party game's gate at the logic layer plus one wiring assertion: the
  blocked outcome is neither recorded nor distinguishable from the ordinary
  one.
- `tests/games/test_games_clapback_logic.py` (the no-contact gate section): a pair
  is never a matchup for three, four, odd and contrived rosters, a property
  check over random rosters, and that the draw is unchanged with nothing
  forbidden; `tests/games/test_games_clapback_cog.py`: Start counts only
  playable members and refuses with the ordinary line, and one round of a
  three-player game benches one of the pair and seats the other against the
  third.
- `tests/duels/test_duels_no_contact.py`: the challenge line and that it costs no
  rate-limit strike, the lobby cooldown line, and Name the Loser refused at
  both the press and the submit.
- `tests/no_contact/test_no_contact_surface_registry.py`: holds this spec's **Gated
  surfaces** table to the code: every function that consults the list belongs
  to a surface named there, every surface's listed entry points still reach
  the list (directly or through a helper), and each row names the modules
  its gates live in. A new gate without a row, or a refactor that drops one,
  fails. It can't see a new contact surface that never consults the list.
- `tests/games/test_mahjong_service.py::test_no_contact_blocks_the_join_indistinguishably`:
  a blocked join raises the same `STALE_TABLE` refusal a race does, and
  seats nobody.
