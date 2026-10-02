# Survivor — NFL Pick'em Survival Cog for Dungeon Keeper

**Spec v2.2 (final)** · Target: live before NFL Week 1 (Wednesday Sept 9, 2026, 5:20pm PT)
**Research basis:** report #15 in the bank: *NFL Survivor Pool Rulesets & Play Modes (Aug 2026)*

**The game in one breath:** pick one NFL team to win each week, straight up. No team twice. Your team loses, you're out. Last one standing takes the coin pot.

**The product in one breath:** picks are private acts; results are communal theater. Everything funnels toward one Tuesday-morning post, the Reckoning, where the meadow gathers to find out who lived.

---

## 1. Ruleset

### 1.1 Entry
- Enrollment opens when an admin creates a season and **never closes**: the door is always open; the road is real.
- **One entry per person. Season one is free entry** (`buyin_coins: 0`) with a **house-seeded pot**: the largest audience is coin-poor newcomers, and the entry fee shouldn't gate them. Buy-in remains a config for future seasons.
- **Late entry: the Gauntlet.** Join any week; the bot retroactively plays every eligible missed week, assigning each week's chalk (highest win-probability team by closing odds, no reuse) and grading it against real results. You inherit that line's full fate: those teams are burned from your satchel, one chalk loss burns your strike, two and you arrive dead, landing directly in Ghost Streak, picking from day one like everyone else. *(**The replay ends at death**: replaying chalk past the fatal week would either hand the new ghost an unearned streak of auto-picks or burn teams for a corpse. The fatal line's teams are burned; the weeks after belong to the ghost you now are, live. Unsettled stragglers are never chalk: a pending game can't be inherited without fate changing when it settles.)* The gauntlet's toll is structural: chalk torches the elite teams, so survivors arrive alive but poor.
- **The gauntlet fee (anti-free-option):** the receipt shows your inherited fate *before* you pay, which would make waiting free information. So late entry costs `gauntlet_fee_per_week × weeks elapsed` (default 50 coins/week), **independent of the base buy-in**: this is what closes the exploit even in a free-entry season. *("weeks elapsed" = fully-kicked-off weeks. The partial week a mid-week joiner picks live is free and joining before Week 1 kickoff costs 0. The fee charges **every** elapsed week even when the replay line dies early: waiting is what's priced, not surviving.)* Alive arrivals' fees feed the main pot; **dead-on-arrival fees route to the Ghost Streak side-pot**: you pay into the game you're actually playing.
- **Mid-week joins:** weeks where any games haven't kicked off yet are picked live from the remaining slate; only fully-kicked-off weeks are gauntlet-replayed.

### 1.2 Weekly pick
- One team per week to **win, straight up**. Each team usable once per season.
- **Per-game locking:** your pick locks at *that team's* kickoff. Until then you can change freely. After your team kicks off you're locked, even if other games haven't started.
- **Total darkness, loose lips:** the *bot* never reveals a pick to anyone (admins and §3.4 hosts excepted: dashboard and mod-log only) until the Tuesday Reckoning: no reactions, no "X has picked" notices. Players, however, may say anything. **Table talk is legal; lying is encouraged.** The bot keeps the secrets; the bluffing is yours.
- **Missed pick → auto-assign:** at the week's final kickoff, anyone pickless is assigned the highest win-probability available team still unplayed (ESPN odds; fallback best record), marked `📎 assigned by the groundskeeper` on reveal. Dignified, mildly embarrassing, fully survivable. *("still unplayed" means the game has not kicked off: assigning from a game in progress or final would be a pick made with the result known. At the final kickoff that pool is the final game(s) of the week. If every legal team there is already burned for that player, the week is voided for them (survive, mirroring §6.10) and it does **not** count against the auto-assign cap.)*
- **Auto-assign cap (anti-AFK):** three auto-assigns per season (config), counting one per week even in double-pick weeks and even when only one slot was missing. The fourth time the groundskeeper is needed, he declines: elimination, with its own fixed, factual line: *"**X** — out of auto-assigns, no pick made. Eliminated."*

### 1.3 Results
- Win → survive. **Loss → strike/elimination. Tie → loss.** ("Your team must win" is one sentence, no asterisks.)
- Postponed/cancelled game → pick voided, player survives, team returns to their pool. *(A postponement announced before the week's final kickoff frees the slot: the player may re-pick from the remaining not-yet-kicked-off slate rather than sitting the week out.)*

### 1.4 Lives
- **One strike** (config: 0–2). First wrong week burns it 💛→🖤; second is the end. Research is unambiguous that sudden death at 20–50 players routinely ends leagues by Halloween; the strike is the longevity buffer.

### 1.5 Escalation
- **Double-pick weeks from Week 14** (config: start week, min-alive trigger, default >4 alive): two teams, both must win, both burn from your pool. One fate, two slots: **each slot locks independently at its own team's kickoff**, so you can still change your Sunday pick after your Thursday pick has locked.

### 1.6 Endgame
- **Sole survivor** wins the pot + `🏈 Sole Survivor` role (held until next season).
- **Wipeout (all remaining eliminated same week):**
  - Through `wipeout_annul_through_week` (default 13) → **week annulled**: nobody dies, teams used stay burned.
  - After it → **equal split** among that week's players: everyone who was still standing when the week began.
  - A week is a wipeout only if it (a) killed at least one player *of their picks*, (b) left nobody alive, and (c) had no death after it. (a) keeps an attrition week (the last two players running out of auto-assigns, or leaving the server) from resurrecting people who are no longer there; (c) keeps it a question about *that week*, so re-grading Week 1 after the field has since died out in Week 9 does not annul a week that wiped out nobody.
  - **Both outcomes are recorded decisions, not derivations.** The annul goes into the season's `annulled_weeks` and stays; the split sets `status = 'complete'`, which is also what stops a retried sweep paying twice. It claims that status *before* crediting anyone, as a compare-and-set, so the poll sweep and a dashboard correction racing on separate connections can't both pass the check and both pay. Deriving either on every sweep would let a later correction dissolve a Reckoning the channel has already read, burying a roster the bot had just announced survived. The sweep only asks about weeks it actually graded that pass, so a no-news sweep never re-opens the question.
  - `0` = never annul: the first wipeout ends the season whenever it lands. There is no "off": a season with nobody alive has to resolve somehow.
- **The Accord (legalized collusion):** once ≤6 remain (config), any living player may invoke `/survivor accord` in the Tuesday-to-Thursday-kickoff window. The bot posts a public vote; if **every** living player accepts within 24h, the season ends immediately in an equal split, with its own ceremony: *"the meadow chose peace."* Any decline (or silence) dissolves it; one invocation per player per season, so it can't become a weekly nag. Final tables deal openly here; nobody has to sneak a wipeout. *(Unbuilt: no `/survivor accord` command exists yet, and its dial is not stored; see §5.)*
- **Multiple survivors after the final week** → equal split. No margin-of-victory tiebreakers in v1.
- **Any season end settles both pots**: sole survivor, final-week split, wipeout split, or Accord: the Ghost Streak side-pot pays out the same day, on that day's standings.

### 1.7 The dead
- Elimination = `👻 Ghost` role, channel access unchanged, one warm condolence DM. Heckling from the graveyard is a feature. *(Death **swaps** the roles: `🏈 Survivor` comes off, `👻 Ghost` goes on, so the member list shows the state at a glance. Both weekly posts ping `@🏈 Survivor` **and** `@👻 Ghost` (§2.3), so the dead keep hearing about the game they're still playing.)*
- **Ghost Streak (v1 core, promoted):** ghosts keep picking via the identical flow; longest post-death correct streak takes the side-pot (funded by `ghost_pot_pct` of the seed (**20%**) plus DOA gauntlet fees; streak-length ties split it). **Your satchel follows you into death**: no-reuse continues, so early ghosts are rich in teams and late ghosts scrape. **Ghosts always pick one team**, even in double-pick weeks; the escalation exists to end the main game, and the streak game stays simple for casuals. **The groundskeeper never touches the dead**: no auto-assign for ghosts, no cap: a missed week simply **breaks the streak** (resets to 0; best-ever streak stays on record), and a voided game leaves it untouched, neither extending nor breaking. The side-pot rewards showing up; auto-assigned streaks would accrue to people who stopped playing. Load-bearing by design: gauntlet joiners who arrive dead land here, so anyone can join any week and always have a live game to play.

---

## 2. Player experience

### 2.1 Journey
```
Season announced → Join (coins) → [Wed slate → pick → sweat → Tue Reckoning] → death → Ghost life → endgame ceremony
```

### 2.2 Season announcement
Embed in #survivor *(not pinned: the panel is sticky and keeps itself at the channel bottom, and a pin would last only until the next chat message replaced the pinned copy)*: hero copy (~3 lines), buy-in, live entrant counter, five one-line rules, link to full rules thread, **[🌾 Join the Season]** button → ephemeral confirm (coin balance, debit, one-sentence rules) → role grant. At Week 1 kickoff the button stays live but the copy flips to gauntlet mode: *"the season is underway. the door is open; the road is real. 🌾 N souls walking."* Joins from here run the gauntlet receipt flow (§4.2) before charging the buy-in.

### 2.3 Weekly cadence
- **Wed ~9am, Slate post** (`slate_hour`, guild-local; pings `@🏈 Survivor` + `@👻 Ghost`): the week's games with `<t:...:f>` timestamps (local time for free), plus the **[🏈 Make your pick]** button. Footer: picks close at each kickoff · X of N alive have picked. *(**The channel has exactly ONE updating panel.** Season pitch, current week's slate, standings line (alive/eliminated/pots), the rules, the "New Here?" door, and both buttons live in a single message (sticky, never pinned). The bot edits it in place on joins and settles, and **reposts it to the channel bottom every Wednesday with the week-open ping**: that repost IS the slate moment, so the twice-weekly ping budget holds. Entry-closed seasons drop the door. The §2.2 announcement post and this panel are the same message; there is no board auto-post (standings live on the panel, and `/survivor board` is on demand). The Tuesday Reckoning stays a real post: it is the payoff, not furniture.)* *(Survivor-172/-183: the week-open ping names the first kickoff relatively: "Week 1 is open — first kickoff <t:…:R>. Pick a team to win.", so a short week is obvious, and **the slate and last call only fire once the pick week is imminent**: the guild-local Tue–Mon frame its first non-postponed kickoff falls in has opened (`week_frame_opens`; a day-count window would post a week early, because the frame logic treats every day after Wednesday as a missed slate to catch up on). `pick_week` returns Week 1 from the moment the schedule ingests, so without the guard a season created in August would post Week 1's slate and DM its last call on the first Wednesday and Saturday it saw, weeks before any game, and the once-per-week keys would then block the real ones. The guard is state, not clock; it holds under the dashboard's forced run too, and `idle_reason` says "Week 1's first kickoff is N days out".)*
- **Sat 6pm, Last call:** DM only to the pickless (a member on a Take a Break is skipped; closed DMs fall back to a channel mention): *"you haven't picked. Make your pick below — or I'll pick for you, and I have terrible taste. 🌙"* The DM, and the closed-DM channel fallback, carries the panel's own persistent 🏈 Make your pick button, so the nudge is the door itself. *(Survivor-180: the early-games line takes the next three games **still to kick**: the future filter runs in SQL before the LIMIT, so a kicked-but-unsettled game can't steal the slot from the Sunday-morning game the line exists to name.)*
  A pickless player on a Take a Break gets neither the DM nor the channel mention; the hosts' pick status still lists them ([take_a_break_spec.md](take_a_break_spec.md)).
- **Sun–Mon:** the bot posts nothing. The channel does the sweating.
- **Tue 9am, THE RECKONING** (pings both roles; see 2.5).
- **Host DMs** *(§3.4)*: only while the guild has hosts,
  and never pings a role: Saturday's last call also DMs each host the week's
  pick status (*N of M alive have picked*, then the pickless by name); **Mon
  6pm** (fixed `HOST_PREVIEW_HOUR`, not a dial) each host gets a heads-up
  with the Reckoning preview card, its due time and a dashboard link. Monday
  is outside the Tue–Mon catch-up frame the other tasks use, so the heads-up
  has **no catch-up**: a bot down all Monday evening skips it, and the
  Reckoning itself supersedes it. **If no host can be DM'd it goes to the
  mod-log channel** (admins only, never the Survivor channel, since the card
  shows live picks); with no mod-log either, the run report lists it as
  blocked with its own reason. It is marked for the week either way, so
  closed DMs don't retry every tick.

The roles get pinged exactly twice a week. Restraint is the brand.

### 2.4 Pick flow
- **Primary, `/survivor pick`:** autocomplete filtered to unburned ∩ playing ∩ not-yet-kicked-off, options like `SF (vs SEA · Sun 1:25 PM server time)`; the zone is named in every menu label and once in the panel's content, since select labels can't carry Discord timestamps. Confirmation (ephemeral): team + opponent, lock time `<t:...:R>`, satchel count with wealth signal (`🟢 24 teams left` → `🟡` under 12; ambient scarcity awareness, never advice), strike status, **[Change pick]** / **[My season]** buttons.
- **Double-pick weeks:** the same command collects two teams in one flow; the confirmation shows both slots with their independent lock times. Changing one slot never unlocks the other.
- **Secondary, slate button:** ephemeral **AFC/NFC dual select** (Discord's 25-option cap vs up to 32 legal teams early season). Keeps casuals off slash-command syntax entirely.
- `/survivor status` (ephemeral): pick state, lock countdown, satchel, strike, ghost stats if dead.

### 2.5 The Reckoning — one post, three acts

**Hosts take delivery** *(the host replaces the bot)*:
while the guild has at least one §3.4 host, the Reckoning is **DM'd to the
hosts instead of posted**: an intro, the card itself, and the same card as
plain copyable text (chunked under Discord's 2000-char cap). The host writes
the channel post. Everything else is unchanged and still the bot's: the week
is marked, weekly-win coins are paid, Ghost roles swap and condolence DMs go
out in the same pass, so condolences can reach the dead before the host's
post does (accepted). **If no host can be DM'd** (all
closed or gone), the bot posts it to the channel itself, as with no hosts.
A host counts as reached once **the card** lands: if a later copy-text chunk
fails, the host can still post from the card, so the bot does not also post
publicly (ship review).
The switch is automatic on whether a host exists; there is no dial.
0a. **The panel is sticky:** the
   channel panel rides `core.sticky.StickyPanel`: any message beneath it
   (member or bot; the Reckoning is its main burier) reposts it to the
   channel bottom after the house debounce. The Wednesday `repost_panel`
   stays separate: it carries the week-open ping, which sticky placements
   don't (it is not pinned, survivor-184: the sticky replace would un-pin it
   on the next chat message anyway); the
   machinery's at-bottom check keeps the two from
   chasing each other. Panel ids live where they always did
   (`announcement_*` in the season config, via
   `survivor.service.panel_ids/set_panel_ids`); no live season → no restick.
0. **The channel panel roster:** the channel panel lists who is
   alive and who is eliminated **by name**, not just counts: at most
   `ROSTER_DISPLAY_CAP` (30) names per list, dot-separated, with an honest
   "…and N more" tail. Length binds before the count when display names are
   long, so the field can never exceed Discord's 1024-char cap. The graveyard
   field is omitted until someone is in it.
1. **The toll** (*numbers only, no flavor line*)**:** week number, survivors before → after, pot. Plus **the gate**, when anyone joined since last Tuesday: arrivals announced with their gauntlet fate: *"two souls walked the gauntlet this week. one arrived breathing."*
2. **The ledger:** the only place picks ever appear: every living player → team → `✅ / 💀 / 💛→🖤 / 📎 auto`, deaths sorted first. Ghost Streak standings get a compact strip beneath (current streaks, record holder) and one count line, `👻 Ghosts: N picked, N correct` (survivor-182; a ghost's picks stay unrevealed, only the tally shows; correct = every graded pick a win, the weekly-prize rule; pending picks are ignored in both counts).
3. **Eliminations** (*factual*)**:** one line per elimination stating team, result and week. Ghost roles applied on post. Zero deaths: no special line: the toll's unchanged survivor count says it.

**Post first, then commit (survivor-173).** The Reckoning's
writes (leaver eliminations (§6.14), the weekly prize, `last_reckoned_week`)
run once inside a rolled-back transaction to build the post, and again
for real only after Discord accepted the send. A `Forbidden` or exhausted
send therefore leaves the week unreckoned and unpaid: `post_reckoning`
returns False, the run report says *blocked*, and the next pass simply
retries. Committing the marks and the prize before the send would let one
failed send lose the week's post, condolence DMs and panel refresh forever,
and resetting the mark to retry would double-pay.

There is no flavor corpus (migration 172 dropped the table; no CRUD, dashboard
card or rotation).

> **Copy register.** All hardcoded copy (
> embeds, refusals, buttons, DMs) is **standard sports register**, "just the
> facts". The Reckoning's copy is hardcoded and factual: Act 1 is the
> numbers alone (the survivors delta says whether the week took anyone), and
> each elimination states its cause: `FATAL_LINE`
> ("**{name}** — {team} lost. Eliminated in Week {week}.") for football
> deaths, with plain fixed lines for the auto-assign cap, the missed pick,
> and the leaver. This closes the per-guild voice route the corpus carried;
> per-guild personality is the branding kit (accent + bot identity) alone.
> The meadow-voiced sample lines below are color, not shipped
> copy. The groundskeeper survives as a mechanic; his line is factual now.

### 2.6 Boards
- `/survivor board` (public, on demand; the channel panel carries the standings line): alive roster with weeks survived + strike hearts, graveyard with week-of-death, pot, most-burned teams meta-stat.
- `/survivor history [@player]`: revealed picks only. Nothing current ever leaks. *(The channel panel carries a **[📜 My History]** button: ephemeral and personal, so the clicker's own unrevealed picks show too, tagged as hidden from others. One shared builder renders both faces so the secrecy rule can't drift.)* *(Each line names both teams and the outcome: "Week 3: **PHI** vs DAL · ✅ won", a loss naming the winner, via `logic.history_rows`, which joins the pick to its game.)*

### 2.7 Endgame ceremony
Champion gets a dedicated post (never folded into a Reckoning): season stats (weeks survived, closest call, teams ridden), pot receipt, role, optional Bios-cog trophy line. Splits framed as shared victory; annulments as their own mini-event (*"everyone died. therefore nobody did. the week is stricken — but the teams you burned stay burned."*)

### 2.7b Join echo (removed)
There is no join echo: per-user join messages in main chat were unwanted, so
the panel's in-place player-count update is a join's only public trace. The
design that was tried and removed, so it isn't rebuilt by accident: an
**Event Echo** line per join, silent, with per-member-per-season dedupe and
the shared cooldowns, as a mini advertisement (*"🏈 Loaf joined the Survivor
pool — 14 players in · pot 8,000 · one team a week, last one standing takes
it"*) linking to the channel panel.

### 2.8 Notifications & consent
**Not built.** There is no `/survivor notifications` command and no per-category DM toggle. Last-call and condolence DMs are always sent, except that a member on a Take a Break gets no last call ([take_a_break_spec.md](take_a_break_spec.md)); closed DMs fall back to a channel mention.

---

## 3. Admin

> **The strict split.** Configuration and state edits go to the dashboard, per
> CLAUDE.md (*"configuration lives on the web dashboard, not Discord"*), and no
> admin action is a command. **Every rule in §1 is a setting, not code** (see
> §5), and the setting lives on a dashboard panel.

### 3.1 Dashboard — `src/web_server/`, admin- and host-gated, nav heading **Games**

Route id `survivor` (bare feature name, per CLAUDE.md's frozen-id convention).

- **Season lifecycle**: create a season (name, year, config), and end it
  (archives; history stays queryable). One active season per guild (§6.12).
- **Config**: every dial in §5, laid out as one page rather than paginated.
- **Roster table**: one row per player with per-row **eliminate** / **revive**
  buttons. A table beats a command signature you have to remember, and it shows
  you the state you're editing before you edit it.
- **Role pickers**: Survivor / Ghost / Sole Survivor (see §3.3).
- **Weekly clock**: read-only rows on the Season card
  for the slate, the last call and the Reckoning: which week each last fired
  for, whether it is due on the next tick, and otherwise the next guild-local
  moment its gate opens (`GET /survivor/clock`; the clock itself,
  `weekly_clock`, `rearm_weekly_task`, the task→day table, lives in
  `survivor/tasks.py` beside the due-functions the loop runs, and the route
  is glue). A slate or last call already fired for the current pick
  week gets a confirm-gated **Reset this week** (`POST
  /survivor/tasks/{slate|lastcall}/reset`, audited + mod-log) that re-arms it;
  the Reckoning is never resettable; it pays weekly-win coins in the
  transaction that marks the week. The force-run button stays on the
  Simulator card.
- **Hosts card** *(admins only)*: see §3.4.
- **`/games help` pointer**: while a season is live with
  a wired channel (and, for `late_entry: closed`, before Week 1 kickoff), the
  games help embed's Other Commands block ends with one line pointing at the
  Survivor channel. Folded into that block rather than given a field: the
  registry already sits at Discord's 25-field ceiling.

### 3.2 Discord — no admin surface at all

> **Why no `settle` or `preview-reckoning` commands.** A stuck game's real
> deadline is *the Tuesday Reckoning*, not minutes-after-final: the Reckoning
> fires regardless, flags stragglers, and late settles post an addendum, so
> the escape hatch is a Monday-evening tool, and the dashboard (phone-usable)
> serves it better than a remembered command signature.

Both live on the dashboard panel:

- **This Week's Games card**: the week's slate with live status; a stuck game
  offers per-team settle buttons (+ void), feeding the same idempotent settle
  pipeline as the poller, with the usual audit row and mod-log mirror.
- **Preview Reckoning button**: renders Tuesday's post in the panel, any time.

Survivor therefore registers **zero admin commands** in Discord: the member
surface (`/survivor pick|status|board|history` + the Join button) is the entire command
footprint. All admin actions → DK mod-log, as ever.

### 3.3 Roles

**The bot creates them on first season** (`🏈 Survivor`, `👻 Ghost`,
`🏈 Sole Survivor`) if they don't already exist, matching by name, and stores the
resulting ids in config. Dashboard role pickers let you repoint any of the three
at a role you made yourself, and a role deleted out from under the bot is
recreated rather than crashing the week. Creation requires Manage Roles; if the
bot lacks it, the panel says so and the game runs without role grants rather
than failing. This runs on the shared provisioner
(`core/role_provision.py`), so the created roles carry **no permissions**: they
are name badges and ping targets.

### 3.4 Hosts

A host is **a member an admin names**, not a role. The list lives in `survivor_hosts` (guild-scoped,
not per season, so a host can create the next season while none is live) and
is managed on the panel's **Hosts** card, which only admins see and only
admins can call (`GET/POST /survivor/hosts`, `DELETE /survivor/hosts/{id}`;
audited `survivor_host_add|remove` + mod-log).

- **Dashboard:** every other Survivor route takes `require_survivor_host`
  (admin **or** a host for the active guild): the whole panel, destructive
  actions included. `/api/me` carries `survivor_host`,
  and the nav shows a host the Survivor item even without the Games section
  or the admin bit. Hosting one guild grants nothing in another.
- **Hosts may play.** *Deliberate exception* (hosts are trusted): a playing host can see every live pick on the dashboard. §1.2's
  darkness rule otherwise admits only admins.
- **DMs:** §2.3 (pick status, Monday heads-up) and §2.5 (the Reckoning).
  Host DMs name pickless players, including no-contact partners of the host.
  They are the admin view delivered by DM, not a member-to-member contact
  surface, so no no-contact filter applies.
- **Inert until used:** with no rows, nothing changes: no DMs, no clock
  row, the bot posts the Reckoning.
- **Privacy:** erasure deletes the member's own host entry and zeroes
  `added_by` on entries they added (data register).

---

## 4. Data layer

### 4.1 Source
**ESPN unofficial scoreboard API** (free, keyless):
`site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard?week=N&seasontype=2`
It serves the schedule + kickoff times (lock enforcement), live status, finals, and odds (auto-assign) in one endpoint. Unofficial and unversioned: parse defensively, and every settle path must also work through the panel's manual settle (§3.2). *(Field notes, pinned by the fixture tests: the season-year selector is `dates=YYYY` (`year=` is silently ignored and serves the current season); kickoffs come as minute-precision Zulu; and **completed games carry no odds at all**, so the frozen-at-last-poll favorite is genuinely unrecoverable after the fact.)*

### 4.2 Bootstrap & polling
- `create-season` ingests the full season schedule into `nfl_games`; daily refresh (flex scheduling moves kickoffs: validate locks against *current* times).
- **Poll every 10 min during game windows** derived from the ingested schedule (not hardcoded days, so it catches international 9:30am ET games and late-season Saturdays); one daily off-window refresh.
- Settle each game as it goes final (idempotent: result already set → no-op). Week settles when all its games are final; the Reckoning fires Tuesday 9am regardless, flagging stragglers. *(When a flagged straggler settles after the Reckoning has posted, a short addendum goes out and any resulting death applies then: flagged results are neither silently resolved nor held for the next Reckoning.)*
- **Gauntlet replay is deterministic:** it reads only stored `favorite` values (frozen at last pre-kickoff poll) and stored winners: two joiners entering the same week always inherit identical lines, and replays never re-fetch odds. A join posts a private "gauntlet receipt" embed (week-by-week: team, result, strike events) before the confirm button, so nobody buys in blind.

### 4.3 Schema (SQLite, DK conventions)
```sql
CREATE TABLE survivor_seasons (
  id INTEGER PRIMARY KEY,
  guild_id INTEGER NOT NULL,
  name TEXT NOT NULL,
  season_year INTEGER NOT NULL,
  status TEXT NOT NULL DEFAULT 'enrolling',   -- enrolling|active|complete
  config TEXT NOT NULL DEFAULT '{}'           -- json, see §5
);

CREATE TABLE survivor_players (
  season_id INTEGER NOT NULL REFERENCES survivor_seasons(id),
  guild_id INTEGER NOT NULL,                  -- denormalized (stage-4 review): the access
                                              -- export guild-scopes via this column
  user_id INTEGER NOT NULL,
  status TEXT NOT NULL DEFAULT 'alive',       -- alive|ghost
  strikes_used INTEGER NOT NULL DEFAULT 0,
  eliminated_week INTEGER,
  elimination_source TEXT,                    -- added at stage 4: picks|cap|admin|left.
                                              -- 'picks' deaths are DERIVED and a corrected
                                              -- result resurrects; the rest are decisions
                                              -- and survive recomputation
  joined_at TEXT NOT NULL,
  PRIMARY KEY (season_id, user_id)
);

CREATE TABLE survivor_picks (
  season_id INTEGER NOT NULL,
  guild_id INTEGER NOT NULL,                  -- denormalized; see survivor_players
  user_id INTEGER NOT NULL,
  week INTEGER NOT NULL,
  slot INTEGER NOT NULL DEFAULT 1,            -- 2 in double-pick weeks
  team TEXT NOT NULL,                         -- ESPN abbr, e.g. 'SF'
  game_id TEXT NOT NULL,
  auto_assigned INTEGER NOT NULL DEFAULT 0,
  locked_at TEXT,
  result TEXT,                                -- null|win|loss|tie|void
  PRIMARY KEY (season_id, user_id, week, slot)
);

CREATE TABLE nfl_games (
  season_year INTEGER NOT NULL,
  week INTEGER NOT NULL,
  game_id TEXT NOT NULL,
  home TEXT NOT NULL, away TEXT NOT NULL,
  kickoff_utc TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'scheduled',
  favorite TEXT,                              -- abbr, captured at last poll before kickoff
  favorite_prob REAL,                         -- its win probability (gauntlet determinism
                                              -- + auto-assign ranking need the number)
  winner TEXT,                                -- abbr | 'TIE' | null
  PRIMARY KEY (season_year, game_id)
);

-- survivor_flavor: dropped in migration 172 (no flavor corpus)
```

---

## 5. Config reference (season `config` JSON)

| Key | Default | Notes |
|---|---|---|
| `buyin_coins` | 0 | Season one: free entry |
| `pot_seed` | **10000** | House-seeded pot. See §5.1 |
| `ghost_pot_pct` | **20** | Ghost Streak side-pot's share of the seed |
| `gauntlet_fee_per_week` | 50 | × weeks elapsed; alive → main pot, DOA → ghost pot |
| `weekly_win_coins` | **25** | Paid at the Reckoning to every player whose picks all won that week (ghosts included; a double-pick split week earns nothing). 0 = off. **A faucet**: ~15–20 winners × 25 ≈ 400/week ≈ 60/day, own ledger kind `survivor_weekly_win`, paid in the same transaction that marks the week reckoned so it can never double-pay and the preview can never pay |
| `strikes` | 1 | 0 = sudden death |
| `tie_rule` | `loss` | `loss` \| `survive` |
| `late_entry` | `gauntlet` | `gauntlet` \| `closed` \| `ghost_only` |
| `missed_pick` | `auto_assign` | `auto_assign` \| `eliminate` |
| `max_auto_assigns` | 3 | Per season; 4th = elimination |
| `double_pick_start_week` | 14 | 0 = never. **Stored but unread**: the gauntlet replay was its only reader and graded late joiners on a rule live members couldn't play; hidden on the panel until stage 6c wires the live flow and the replay together |
| `double_pick_min_alive` | 5 | Only escalates if ≥ this many alive. **Not a stored setting yet**; see below |
| `wipeout_annul_through_week` | 13 | Annul a wipeout up to and including this week; after it the season ends in an equal split. 0 = never annul. **Stored and enforced** (§1.6) |
| `accord_max_alive` | 6 | `/survivor accord` available at ≤ this many living. **Not a stored setting yet**; see below |
| `ghost_streak` | on | Side-pot % of main pot + DOA fees |
| `slate_hour`, `lastcall_hour`, `reckoning_hour` | Wed 9 / Sat 18 / Tue 9 | Guild-local, via `tz_offset_hours`. §2.3 schedules three tasks, one hour each |
| `last_host_preview_week` | 0 | **State, not a dial**: the week §2.3's Monday host heads-up last went out for, beside the other `last_*_week` keys. Shown as a Weekly clock row only while the guild has hosts; not resettable |
| `annulled_weeks` | `[]` | **State, not a dial**: the weeks §1.6 struck from the record, written by the settle sweep and read by `recompute_player`. Not rendered on the panel, so no admin save ever carries it; `validate_config` still normalizes it (whole weeks 1–30, deduped and sorted) so the stored order can never carry meaning a rewrite would lose |
| `channel_id`, `role_survivor_id`, `role_ghost_id`, `role_sole_survivor_id` | 0 (unset) | Wiring, not rules: the #survivor channel and the three managed roles (§3.3). 0 degrades that step to skipped-and-logged, never a crash |

**Tier 2 dials are not stored yet**:
`double_pick_min_alive` and `accord_max_alive` are **not** in `DEFAULT_CONFIG`,
are not on the panel, and `validate_config` rejects them outright, so an admin
cannot set a season rule the bot never plays by. The values above are the
intended defaults; each dial comes back, key and dial together, in the same
change that ships the rule that enforces it (§1.5 double-pick escalation, the
Accord), as `wipeout_annul_through_week` did with §1.6.

### 5.1 The seed is a faucet — say so out loud

With `buyin_coins: 0` there is **no player money in the main pot**. Every coin the
champion receives is newly minted, so the seed is a faucet and gets counted as
one. The seed was sized against the 2026-07-30 economy health review
(`git show 242bddeb:docs/reviews/2026-07-30-economy-health.md`), a snapshot of the guild's float and balances, not live figures:

| | |
|---|---|
| Seed | **10,000**: main pot 8,000, Ghost Streak side-pot 2,000 |
| Share of the ~74,600 guild float | 13.4% |
| Against net supply growth (+5,221/day) | ~1.9 days, minted once around week 18 |
| Against p90 balance (1,304) / median (186) | 7.7× / 54× |
| Against the largest casino win ever (3,000) | 3.3× |

**Every movement rides `economy.service`**: `apply_credit` / `apply_debit` with
its own ledger kinds, never a bare wallet UPDATE, so the whole feature is visible
in the economy metrics rather than appearing as unexplained mint:

| Ledger kind | Direction | When |
|---|---|---|
| `survivor_buyin` | debit player | join, when `buyin_coins > 0` |
| `survivor_gauntlet_fee` | debit player | late entry, `fee_per_week × weeks` |
| `survivor_payout` | credit player | champion, equal split, or annul-era split |
| `survivor_ghost_payout` | credit player | Ghost Streak side-pot, ties split |
| `survivor_weekly_win` | credit player | weekly all-wins prize, paid at the Reckoning |

The seed itself is **booked, not minted, at create-season**: the pot displays
truthfully all season and the 10,000 enters supply exactly once, at payout.
Gauntlet fees and buy-ins are recycled player coin: they raise the pot without
raising the float, so a pot that grows past the seed is not extra faucet.

---

## 6. Edge cases
1. **Thursday trap:** unpicked at Thursday kickoff = nothing missed; only Thursday teams leave the menu. Locks are per-game.
2. **Flex scheduling:** validate locks against current ingested kickoff, refreshed daily.
3. **International 9:30am ET games:** last-call DM names any early-window games explicitly.
4. **Byes:** filtered in autocomplete *and* validated server-side.
5. **Opposing picks, same game:** two players on opposite sides is fine: settle both.
6. **Timezones:** store UTC, render `<t:...>` everywhere.
7. **Idempotent settling:** no double eliminations, no duplicate Reckonings.
8. **Double-pick weeks:** two slots, one fate: either loss burns the strike/kills; both teams burn regardless.
9. **Gauntlet joins:** replay uses closing favorites only; a week with a voided/postponed chalk game replays as void (survive, team returns). Joins during a double-pick era replay both slots as top-two chalk: *deferred to 6c: until the live flow has two slots, every elapsed week replays one.*
10. **Joiner with no legal team in their entry week** (e.g., joins Sunday night and every unplayed team is gauntlet-burned): that week is voided for them: survive, pick next week. Rare, but must not crash or auto-kill.
11. **Accord discipline:** invocations outside the Tue–Thu window are rejected with the window shown; a player who leaves the server during a vote counts as a decline; accord during a locked pick voids nothing retroactively; the split is computed on invocation-day standings.
12. **One active season per guild;** archives queryable.
13. **Auto-assign dead end**: at the final kickoff every legal not-yet-kicked-off team is burned for the player → week voided (survive), no auto-assign charged. Mirrors #10; must not crash or auto-kill.
14. **Member leaves the server mid-season**: eliminated at the next Reckoning with its own fixed line (*"left the server mid-season. Eliminated."*); ghost streak frozen at its best; no further picks accepted. *(Survivor-174: absence from the member cache is a **suspicion**, not a verdict: the Reckoning eliminates a leaver only when the bot is ready, the guild is fully chunked, and `guild.fetch_member` answers NotFound; any other answer keeps the player. A partial cache in the seconds after a re-IDENTIFY could otherwise bury the whole roster as `left`, which settle never undoes.)* Rejoining the server reactivates nothing for the living game (dead is dead) but a rejoined ghost may resume Ghost Streak picking. *(Todo #203: a departed member is **dropped** from the board, the channel panel and the Reckoning rather than shown, because a bare id no guild can resolve (`soul 4917…`) is the exact failure the house name rule exists to prevent, and there is nobody in the post to eulogize. The drop is decided by the member cache when it can be trusted (`guild.chunked`), because that is the fresher answer and it makes a rejoined member reappear on its own; when it cannot, it falls back to the recorded `left` elimination, stale by up to a week but never wrong in bulk. Filtering happens where the data is assembled, so a list and its own header count cannot disagree.)* Inside an Accord vote, leaving still counts as a decline (#11).

---

## 7. Build order
1. Schema + season/config admin commands
2. Schedule ingest + ESPN parser (fixture-based tests on saved JSON)
3. Join / pick / status / board: lock + validation logic
4. Polling loop + idempotent settle engine + manual settle
5. Reckoning, slate, last-call tasks
5b. Gauntlet replay engine + receipt flow
6. Ghost roles + Ghost Streak (v1 core), wipeout/annul logic, Accord vote flow, endgame + coin payouts (main + ghost pots)
7. **v2 backlog:** buyback window (escalating coin cost, natural sink), dead pool, loser-pool & underdog off-season variants, playoff survivor capstone
8. **Go live Week 1** and let the meadow learn by dying. No mod
   test league: the Gauntlet means a member who shows up in Week 4 still gets a
   live game, so the usual reason to soft-open a pool (miss the start, miss the
   season) doesn't apply here. Enrollment opens as soon as join/pick/slate is
   testable, ~Sept 3, with real stakes from Sept 9.
9. **Testing rig:** seasons with `season_year >= 2090` are
   **synthetic**: the poller never calls ESPN for them (a nonsense year would
   be served the *current* season and pollute the table), and the dashboard
   grows a Simulator card: generate a compressed schedule (weeks measured in
   minutes), settle kicked games (chalk/random/upset, through the same
   manual-settle pipeline, so grading stays derived), and ▶ Run Weekly Tasks
   forces the Reckoning/panel-repost past their clock gates while the
   once-per-week state keys still prevent double-posts. A whole season runs in
   an evening in a test channel; the rig hard-refuses real season years.
   ▶ Run Weekly Tasks lives on the Simulator card, not the Season card: the weekly
   cadence is automatic in production (the poll loop fires each task at or
   after its guild-local hour and catches up if the bot slept), so a manual
   force is a rig affordance, not an operator control. A real season therefore
   has no force button at all.
9b. **Role reconcile:** every weekly-task
   decision pass also repairs life-state roles: alive players hold 🏈
   Survivor, ghosts hold 👻 Ghost, enforced through the idempotent
   `swap_member_roles`, which checks the gateway role cache first, so a
   no-drift pass costs zero Discord calls. Exists for drift: a join that
   crashed after charging but before its grant, a hand-removed role, a
   rejoin. Best-effort per member, never blocks the pass. *(Survivor-185: the pass runs **once an hour per season**, plus whenever
   a decision fired or an admin forced the run, and a member whose swap
   failed (a role above the bot, Manage Roles lost) is skipped for an hour
   and warned about once per hour, instead of a traceback and an API call
   every 60-second tick. The memory is in-process; a restart retries once.)*
   *(The role being put **on** is checked first with
   `core.role_safety.grant_block_reason`: a Survivor or Ghost dial pointed at
   a mod-level, managed or above-the-bot role skips the whole swap, removal
   included, since stripping the old life state with nothing to replace it
   would leave the member in neither. The join grant refuses the same way.)*
10. **A forced run reports what it did:** every gate routes
   through the week lookup, so a season whose year has no ingested schedule is
   indistinguishable from a quiet week: all three gates return `None` and the
   forced run posts nothing. `run_weekly_tasks` therefore returns one record
   per season (`fired`, `blocked`, `reason`) and the dashboard shows it: what
   posted, or why nothing was due ("no schedule ingested for 2035…", "already
   run for this week", "…was due but couldn't post"). The `post_*` helpers
   return a bool so the report can't claim a task fired when its channel was
   unreachable. The route also **scopes the run to the caller's guild**: a
   forced run is an admin acting on their own server, and the unscoped call
   dragged every other guild's live season past its clock gates too.

**Cancel and refund (survivor-181).** The dashboard's Cancel & Refund button (beside End Season, behind a confirm) ends the live season with no winner and credits every `survivor_buyin` / `survivor_gauntlet_fee` debit of that season back as `survivor_refund` (`meta.refund_of` = the debit's ledger id, exact amount, no booster). Refused (422) for a season that is already complete or has any `survivor_payout` / `survivor_ghost_payout` ledger row, so a second press, or a press after the pots paid, refunds nothing; a debit that already has a refund is also skipped. The route runs under `open_db_immediate`, so a double press queues behind the first. Prizes already paid are not clawed back; neither pot is paid. Audited as `survivor_season_cancel` and mirrored to the mod log (`service.cancel_season`).
