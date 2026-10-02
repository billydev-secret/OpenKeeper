# Casino — Feature Spec

House gambling games staking the guild currency in one admin-configured
**casino channel**. Sunny-meadow
theming over an unmistakably Vegas core. The plans that built it
(`docs/plans/casino-derby.md`, `casino-ephemeral-ux.md`,
`casino-classics-and-prediction-market.md`) are
gitignored; recover one with `git show 678a16fa:docs/plans/<name>.md`.

Every card this feature sends is a **Components V2 layout**, not an embed:
the hub, every game card (coinflip, slots,
blackjack, roulette, the derby, baccarat, dice, keno, war, mines, the daily
comp, My Stats, the jackpot celebration, the public big-win broadcast and
the Pools market).
The builders live in `bot_modules/casino/cog/layouts.py` (its hub-support
functions `TABLES`, `casino_title` and `hub_sections` are imported by
`hub_layout.py`) and are pure: primitives and a `name_fn` in, a
`list[Item]` (or a `CardResult`/`BigWinBroadcast` pairing that list with a
recap/ping flag) out. A card is bare, with no accent color and no `Container`,
so every settled result instead leads its heading with one outcome glyph
(✅ a win landed, ❌ the house kept it, ➖ a push or a refund); a field
becomes a bold label line inside the one flowing text block a card is,
rather than a `Section` per field.

**The casino's name is per-guild branding**: it comes from
`branding_config.casino_name` (`branding_service.resolve_casino_name*`,
default `DEFAULT_CASINO_NAME = "Golden Meadow"`, the home server's name). The cog reads it alongside its other
settings and passes it to the pure builders; the hub title is
`🎲 The {name} Casino` and the help card `How the {name} pays`
(`casino/cog/layouts.casino_title`). The Branding **Title in the Banner** switch
draws `The {name} Casino` with no emoji, which a banner font can't draw
(`hub_layout.casino_panel_title`). Edited on **Config → Server → Branding**, which
dispatches `casino_config_change` so a rename repaints the hub panel. Flavor
copy stays name-agnostic (no "meadow"/"honeypot"
imagery that would survive a rename): result text says "the house" or
"the jackpot" rather than assuming any particular theme, and the slots/
jackpot card titles interpolate the configured name directly.

**Zero slash commands.** The bot maintains a persistent **hub panel** in the
casino channel; every flow starts there and ends in the amount ladder
(below). The panel is a **Components V2 hub**
(`bot_modules/casino/cog/hub_layout.build_casino_hub_layout`, bound by
`views.CasinoPanelView`), the economy panel's shape, style guide paradigm
8: bare (no accent bar; style guide
§ No container), the guild's panel banner first and footer
last (Config → Server → Branding → Panel Art), the title (or the title drawn into the
banner) with a grey **Help** beside it opening How It Works (`casino:help`),
one intro line, then the live board between lines (💰 Progressive Jackpot,
📡 On the Floor, 📊 Today at the Tables, House Rules, from `layouts.hub_sections`,
each left out when it has nothing to say except House Rules), and at the
foot:

- a **Pick a table…** dropdown (`casino:table`, a *menu dropdown*, style
  guide paradigm 11): one option per **open** table in `svc.GAMES` order,
  each with its emoji, name and a one-line description from
  `layouts.TABLES`. Ten tables as buttons plus the board, the art and the footer
  don't fit a V2 card's 40 components, and the dropdown carries the
  descriptions buttons couldn't. A pick opens the table (`views.open_table`), then schedules the debounced hub
  repaint (`_schedule_hub_repaint`, ~8 s): a persistent select keeps showing
  the member's pick until the message is edited, and Discord sends nothing
  for re-picking the shown option, so the repaint is what puts the
  placeholder back. With every table closed the dropdown is replaced by
  *Every table is closed right now.*
  The dropdown's last option is **📊 My Stats** (value `stats`, not a game
  key). It runs the stats card, then the same repaint.
- a grey row holding **🎁 Daily Comp**, only while `comp_on`. With the comp
  off the foot is the dropdown alone. With every table closed there is no
  dropdown, so a **📊 My Stats** button sits there instead. The `cog_load`
  registration carries the `casino:stats` button too, so a post from before
  the move routes until its repaint (`tests/casino/test_casino_hub_layout.py`).

The per-table buttons (`casino:<game>`) stay
registered through `views.CasinoHubView` (never posted), so a panel posted
as an older embed hub keeps working until the panel refresh repaints it,
which it does ~60 s after the next boot, since the hub is a
`refresh_on_update` sticky.

**Private play, public moments** (migration 158, todos #94/#95): a public
message per result would scroll the channel non-stop under heavy slots play,
yanking active UI around. **Every game renders
ephemerally**: each player's private machine edits itself in place
(`_respond_private`: a press on your own ephemeral message edits it via
`response.edit_message`; any other press opens a fresh ephemeral). Animation
frames ride `edit_original_response` (the interaction webhook, not the
channel edit bucket). The channel itself carries only shared surfaces:

- the **hub panel**, whose **📡 On the floor ticker** (`casino_ticker`
  table, `record_ticker`/`recent_ticker`, written inside `record_play`'s
  settlement transaction for every `TICKER_GAMES` game) lists the **latest
  play of each of the last six distinct players** (a window function over
  the log, one row per `user_id`, newest first; casino-135: a raw tail would be
  one grinder's last six spins, with everyone else pushed off within a
  minute). `TICKER_KEEP` is 200 rows per guild for the same reason: the
  log must outlast one member's burst. Each row leads with its table's
  emoji, taken from the same `layouts.TABLES` table the hub's dropdown
  renders from, so a game can't join the ticker without its glyph
  (casino-140).
  A debounced per-guild repaint (`_schedule_hub_repaint`, 8s) coalesces a
  burst of plays into one in-place panel edit; an edit never moves the
  panel. The panel also carries a **📊 Today at the tables** line naming the
  day's biggest net winner (`casino_daily_net` table, `daily_standings`,
  folded into the same `record_play` transaction for *every* game). Net =
  returned − wagered over the guild-local day (the same YYYY-MM-DD boundary
  the wager cap uses); the winner shows only while up (net > 0).
  `daily_standings` still computes the biggest net loser (net < 0) but the
  hub **does not print it** (casino-137, D6): the panel is public, and
  naming a member at −17,495 there is a pillory, not the harm-reduction
  control the plans imagined; a member reads their own net on 📊 My Stats.
  Refunds/voids never reach `record_play`, so a handed-back bet never sways
  the board. The line refreshes on the instant-game repaint; a
  roulette/derby-only settle updates the table but the panel catches up on
  the next repaint;
- **broadcast moments**: the jackpot celebration (always), and any win on
  any table paying ≥ `broadcast_min_payout` (0 = off) **and** ≥
  `broadcast_min_mult` × its stake (default 3; 1 = amount only); see "The
  bar needs a multiple" below. The broadcast is a
  **separate card** built from the player's result card's own recap text
  (`CardResult.recap`), never the card's items themselves (copied so
  editing one card can never rewrite the other),
  `build_big_win_broadcast_layout` in `layouts.py`, routed through the cog's
  one `_send_big_win` seam (`_after_instant` for the instant games,
  `_broadcast_window_win` for the private-round family, `_auto_resolve_hand`
  for the idle sweep; skipped when the jackpot celebration already announced
  the spin). The seam takes the guild's whole `CasinoSettings`, not a
  threshold int, so no call site can carry the bar without the multiple. It carries **no button** (see "Public recaps carry no buttons"
  below) and titles itself for the event rather than the game, on a ladder
  that escalates with how far the payout clears the bar
  (`casino_logic.big_win_tier`, tested in `tests/casino/test_casino_logic.py`):

  | rung | fires at | pings |
  |---|---|---|
  | 💰 Big Win | ≥ 1× `broadcast_min_payout` | — |
  | 🔥 Huge Win | ≥ 3× | — |
  | 💎 Legendary Win | top 3% of the guild's recent **announced** wins, floored at 3× | `@here`, unless `broadcast_ping_enabled` is off |

  Steps are **multiples of the dial**, never coin amounts: the two live
  guilds run economies ~8× apart, and a hardcoded ladder would be wrong in
  at least one of them. The top rung adds a lead line above the result copy
  and is the only one that pings; `AllowedMentions(everyone=True)` is set
  only for it, every other broadcast staying on `.none()`.

  **The ping is a per-guild dial** (`broadcast_ping_enabled`, default on,
  the behavior every guild already had). Unchecked, a Legendary win keeps
  its header, its lead line and its public card and simply drops the `@here`;
  the ladder itself does not move. `big_win_tier` takes it as a `ping_enabled`
  kwarg rather than reading config, so the decision stays pure, and the cog's
  `_send_big_win`, the single chokepoint every broadcast path funnels through,
  is the only place that reads it. That read happens **only** when the
  percentile lookup already returned a rankable mark, so an ordinary
  broadcast never pays for it, and a failed read returns False: a hiccup can
  cost the ping but must never manufacture one in a guild that switched it
  off.

  **Rungs are sized against what the economy actually pays.** Against prod
  data (a snapshot: 4,350 winning bets on an average stake of 36 coins, and a
  largest single win of 3,000 against a 500 bar, 6×), a 10× "Monster Win" rung
  could never render, and neither could a ping sitting above it. The ladder is
  deliberately short, and the *top* of it is the percentile, which resizes
  itself.

  **Legendary supersedes the rung it lands on** rather than being a fourth
  step. The ping fires at the larger of the percentile and 3× the bar, so
  when a guild's percentile sits at or under that floor the two conditions
  coincide and 🔥 Huge Win is subsumed. That is accepted, not overlooked:
  reserving a sliver of range for Huge Win would buy a rung nobody would see
  fire. `tests/casino/test_casino_logic.py` pins both the supersession and, via
  `test_every_ladder_rung_is_reachable`, that no rung is dead.

  **Why the floor exists at all.** A guild whose announced wins all cluster
  near its bar would have a percentile barely over that bar, and without the
  floor every routine broadcast would ping the channel. A percentile can only
  escalate a broadcast, never create one: a guild with the dial at 0 stays
  silent however rare the win.

  **A push is not a win, and the gate knows it.** `big_win_tier` takes the
  `stake` and refuses `payout <= stake`. Blackjack pushes return the stake,
  baccarat Player/Banker bets push on a tie, and a war retreat hands back
  half, all of which clear a 500 bar easily on payout alone. Gating on the
  payout by itself would announce a 2,000-coin blackjack push as "🔥 Huge Win", a
  headline asserting a win that did not happen. It is the same rule
  `record_play` uses to count a win; the two must not disagree, or the
  broadcast advertises what the stats refuse to count.

  **The bar needs a multiple** (decision D6, casino-130). The
  amount bar alone was sized against an average stake of 36; with top players
  at 1,000-coin stakes (495 of them in 30 days), a routine 2× blackjack win
  (2,000) clears the 500 bar four times over and headlines as 🔥 Huge Win, and
  a Mines cash-out at 1.06× on 1,000 (1,060) is a 💰 Big Win, for ~100 public
  cards a day. `big_win_tier` takes the
  guild's `min_mult` (the `broadcast_min_mult` dial, Economy → Wagering → Casino,
  default `casino_logic.BROADCAST_MIN_MULT_DEFAULT` = 3) and refuses
  `payout < min_mult × stake`: a 1,000-coin even-money win does not post,
  a 1,000 → 3,000 does, and a 6× slots hit on 500 still does. The multiple
  gates the **whole card**, Legendary included: the percentile decides how
  loud a qualifying card is, never whether it qualifies, so the Legendary
  tier itself is unchanged. The dial's floor is 1 (amount only) rather than 0, because the bar is already the off switch.
  Pinned in `tests/casino/test_casino_logic.py` (the finder's three cases) and
  `tests/casino/test_casino_big_win_broadcast.py` (the dial read from a
  guild's stored settings changes the outcome).

  **A doubled hand is judged on its total stake** (casino-131). Using
  `base_stake` (right for the Play Again button, which re-offers the base the
  player chose) as the broadcast stake would let a doubled push, which
  returns 2× base, pass the `payout > stake` gate and announce (and bank) a
  hand that won nothing. `_blackjack_step` passes `step.stake` (the
  total) to `_after_instant` and keeps `base_stake` for the view; the
  wiring test in `test_casino_big_win_broadcast.py` drives the button path
  and asserts both.

  The percentile comes from `casino/service.win_percentile` over
  `casino_win_history` (migration 162): a rolling `WIN_HISTORY_KEEP`-row
  window per guild, written by `record_win` from the cog's `_send_big_win`
  seam, **one row per public announcement, after that announcement's
  percentile has been read**. Both halves are load-bearing, and both were
  wrong when this banked from `record_play` instead:

  - *Per announcement, not per settled bet.* A roulette round where the
    player spread five bets that each cleared the bar is one card in the
    channel; banking it five times over-weighted multi-bet rounds. It also
    banked jackpot spins, whose big-win card is suppressed in favor of the
    jackpot celebration, so the largest payouts in the distribution pulled up
    a mark nobody was ranked against.
  - *After the read.* `record_play` runs inside the settle transaction, which
    commits before the broadcast reads, so each win entered the population it
    was about to be ranked against: a payout tying the guild's recent maximum
    always cleared its own mark, and a guild whose announced wins cluster
    tightly above the floor would ping on *every* broadcast.

  The population is announced wins only. Ranking *every* win put the mark
  below the broadcast bar, because the overwhelming majority of casino wins
  are small pair payouts (prod's average win returns 71 coins against a 500
  bar), which left the floor deciding everything and the percentile
  contributing nothing.

  The top band is sized in **rows counted back from the end** (`max(1, total
  × 3 ÷ 100)`), not as an offset forward. `total × 97 ÷ 100` is exact at
  multiples of 100 but rounds loose below them: at the 40-row sample floor it
  left two rows above the mark, the top 5%, handing the smallest guilds the
  loosest ping bar.

  Under `PING_MIN_SAMPLE` (40) banked wins `win_percentile` returns **None**,
  a refusal callers must read as "don't ping" and never as "everything
  qualifies"; this is what stops a fresh guild `@here`-ing its first win. The
  floor is sized against the *announced*-win rate, not the total win rate:
  prod broadcasts a few dozen times a year, so a 100-row floor
  would take years to arm. The read is skipped
  for any payout under 3× the bar, and a failed read degrades to None, so a
  hiccup costs the ping and never the announcement. A guild that has armed
  the percentile and then turns `broadcast_ping_enabled` off reaches the same
  silence from the other direction: the card posts, the `@here` does not.

  The table **deliberately stores no `user_id`**: it answers only "how big is
  an announced win around here lately", which never needs to know who won, so
  it stays outside personal data (see the "deliberately not personal data"
  section of `data_register.md`) and there is nothing for `purge_user_data` to clear.

The panel is **bottom-sticky** (the economy sticky-panel pattern): channel
traffic debounces a restick (delete + repost, since it is the casino's only
entry point) after 60s. There is no hold on the restick during a round, since
a private round lives in its own ephemeral message and no amount of channel
traffic can disturb it. Both the
casino config PUT **and the economy config PUT** dispatch
`casino_config_change`, so enabling/disabling/moving anything updates or
tears down the panel without a restart.

## Money

All movement goes through `casino/service.py`:

- `take_stake`: the only debit path. Guard order: economy enabled → casino
  channel set → table enabled → min/max bet → **daily wager cap** → funds.
  Kind `casino_stake`, meta `{"game": ...}`. A blackjack double-down skips
  the min/max re-check (`enforce_bet_limits=False`) but never the cap or
  balance.
- `take_stake` fires the **`casino_play` quest trigger** on success only, so a
  bet refused by any guard earns nothing. The occurrence key is the stake's own
  `econ_ledger` row id, which makes every charged bet one countable event and a
  blackjack double-down its own (it is a second wager). Fired through
  `fire_trigger_inline`, whose never-raises contract keeps a quest failure from
  dirtying the stake transaction. (Prod carries a `Take a Seat`
  daily quest against this kind, so the trigger must exist or the quest can
  never be cleared while it occupies a daily board slot.)
- `pay_out` / `refund`: credits (`casino_payout` / `casino_refund`),
  **always `booster=False`**: a house payout must never mint through the
  booster multiplier.
- Daily cap accounting: `casino_daily (guild_id, user_id, local_day,
  wagered)` upsert **in the same transaction as the debit**, guild-local day
  via `tz_offset_hours`. Cap 0 = uncapped (and keeps no books). **Refunds
  hand the headroom back** (current-day row, clamped at 0): a
  house-initiated refund must not leave the cap consumed by a bet that
  never resolved.
- `take_stake` also takes the interaction's `channel_id` (from the cog
  entry points) and refuses play outside the configured casino channel, so
  an orphaned hub panel a failed delete left behind can't run games
  elsewhere.
- A departing member's live stakes ride the wager-escrow rule:
  `refund_member_live_stakes` (called from the cog's `on_member_remove`)
  refunds an open blackjack hand and deletes+refunds their bets on open
  roulette rounds and derby races, so nothing settles into a ghost wallet.
- House edge is **fixed paytables in `casino/logic.py`, not
  settings**, enforced by exact-EV tests (see Testing). RTPs: coinflip
  92.5%, slots ≈91.3%, roulette ≈97.3%, blackjack rules-derived, derby
  0.90–0.97 per runner (weights × multipliers, pinned per runner so no
  pick strictly dominates). Coinflip and slots carry the extra ~2 points of
  edge because they carry ~70% of
  the handle; both cuts sit where a single play can't show them (a
  1.85× flip still reads as near-double; a pair pays 145 on a 100 bet
  instead of 150). Blackjack was left at 3:2 deliberately: 6:5 naturals
  is the one edge change players recognize on sight, and it buys under
  half a point of blended edge. The **advertised** returns in the How It
  Works card come from `COINFLIP_RTP_PCT` / `SLOTS_RTP_PCT`, which the
  RTP tests pin against the enumeration, so the copy cannot drift from
  the paytable it describes.
- The register feed **skips** `casino_stake`/`casino_payout` (results are
  already public in the casino channel, and bet-per-play volume would
  outrun the feed's drain budget and starve other kinds); `casino_refund`
  still posts (↩️, memo names the table), and all three keep their
  `/bank wallet` display entries. Casino kinds are deliberately **absent
  from `FAUCET_GROUPS`** (the `wager_payout` precedent; gross winnings
  aren't faucet income) and `casino_stake` is in `BURN_EXCLUDED_KINDS`
  (gross turnover isn't "spending" on the biggest-spenders board).
- Casino games deliberately do **not** call `pay_game_rewards`: gambling
  pays no participation/win faucet.
- **The daily comp is not a wager** (`claim_daily_comp`, casino-134). With
  `daily_comp` > 0 the hub carries a 🎁 Daily Comp button; a press books the
  claim on `casino_daily` (`comp_claimed` 0 → 1 in one upsert, so a second
  press on the same guild-local day, or two at once, flips nothing and
  gets the reset time back, `<t:…:R>`) and then spins the slots at the dial's
  amount with **no debit**: a win is a plain `casino_payout` (meta
  `{"game": "slots", "reels": …, "comp": amount}`), a blank spin moves no
  money and feeds nothing. Neither path touches the cap (the row's `wagered`
  stays 0), `record_play`, `casino_weekly`, `casino_daily_net` or the ticker,
  so the stats, the week's highlights and the day's standings stay a record of
  money members actually put down. A triple-7️⃣ comp pays the flat 120×,
  never the progressive pot, which real lost stakes built. It is never
  broadcast. Guard order mirrors `take_stake` (economy → casino open → right
  channel → slots open → dial), and a refused claim is not a claim. The result
  renders privately (`build_comp_layout`: ✅ on a win, ➖ on a blank; a bare
  card has no color to carry that distinction, so the heading glyph does)
  with no Play Again; 📊 My Stats shows whether today's is
  still unclaimed, since the hub is one shared panel and cannot be
  per-member. Rationale: the casino's volume is ~78% four players with weekly
  actives flat since launch, and the comp is the one cheap return hook for
  the rest; it ships dark so the amount is a deliberate admin choice.

## Settings (`casino_*` keys in the config KV table)

| Field | Default | Notes |
|---|---|---|
| `channel_id` | 0 | **Master switch**: 0 = casino closed (ships dark) |
| `min_bet` / `max_bet` | 5 / 100 | max 0 = no ceiling |
| `daily_wager_cap` | 500 | per member per guild-local day; 0 = uncapped |
| `max_payout` | 0 | casino-133: per-play payout ceiling applied inside `pay_out` for every table (the total credited, stake included); 0 = none (ships dark). `pay_out` returns the amount credited, the instant games and round tables record that, and the ledger meta keeps `uncapped` when it bit. The progressive jackpot is exempt (escrowed skim, not a mint). Hand games (blackjack, war, mines) settle through `_settle_hand`, which returns the credited amount (None = already settled), so their result cards show what was actually paid |
| `{game}_enabled` ×10 | true | closed tables refuse bets + drop off the panel: the hub's **Pick a table…** option (`hub_layout.open_tables`; the registered `CasinoPanelView` carries every option for stale panels) and How It Works field alike. A stale panel's button still opens nothing: `open_bet_picker` / `open_mines_bet_picker` re-read the dial and answer `❌ That table is closed right now.` ephemerally before any ladder is shown (the same copy `take_stake` gives a bet that slips past) |
| `jackpot_enabled` | true | the progressive pot (armed only while the casino is) |
| `jackpot_cut_pct` | 5 | % of each fully-lost stake skimmed into the pot; every skimmed coin is escrowed rather than burned, so this trades sink strength for pot drama (sized in the 2026-07-25 sources/sinks review, `git show 242bddeb:docs/reviews/2026-07-25-economy-casino-sources-sinks.md`) |
| `jackpot_seed` | 100 | what the pot resets to after a win (minted on claim) |
| `round_idle_seconds` | 600 | **abandonment** TTL for a private round, not a betting window; the player paces their own round and presses the resolve button when ready. The 60s maintenance sweep resolves anything past it, so a stake can never sit forever (bounds 60–840, capped for the same webhook-token reason as `blackjack_idle_seconds`) |
| `blackjack_idle_seconds` | 180 | idle hand auto-stands, idle War standoffs auto-resolve **and idle Mines grids auto-cash**: one table-idle knob, not three (bounds 30–**840**: an ephemeral hand is editable only through its interaction webhook, whose token dies at 15 min, so a longer window would stand hands nobody can repaint; a larger stored value still loads and settles fine, its message just goes stale) |
| `pools_enabled` | **false** | the daily prediction market; ships off, unlike the ten tables |
| `pools_channel_id` | 0 | where the market panel lives; 0 = fall back to `channel_id`. Its own channel because the round is a day long |
| `pools_close_hour` | 18 | guild-local hour betting shuts on the day being measured (bounds 0–23). Settlement is at the day roll, not here |
| `pools_takeout_pct` | 5 | % of the whole pool taken at settle and **burned** (bounds 0–50). Distinct from `jackpot_cut_pct`, which is skimmed per lost stake and fed to a pot that re-mints it |
| `pools_metrics` | `""` | comma-separated `pools_metrics` keys the daily draw may pick from. Empty = the whole roster, which is also what an untouched guild runs |
| `broadcast_min_payout` | 0 | wins on any table paying at least this get a public broadcast, if they also clear the multiple below; 0 = never (jackpot celebrations always post) |
| `broadcast_min_mult` | 3 | a broadcast also needs `payout ≥ this × stake` (bounds 1–1000; 1 = amount only). D6: the amount bar alone would let 1,000-coin even-money wins flood the channel |
| `broadcast_ping_enabled` | on | whether a 💎 Legendary Win carries an `@here`. Off, that rung still broadcasts, just silently; no other rung ever pinged |
| `daily_comp` | **0** | the **daily comp** (casino-134): a house-funded slots spin of this many coins, once per member per guild-local day, from a 🎁 **Daily Comp** button on the hub. **Ships off**: 0 hides the button and refuses the claim; bounds 0–100,000. The button (and the House Rules line) only appear while `comp_on`: dial > 0 **and** the slots open, since the comp is a spin on the slots. Enforced at the claim (`claim_daily_comp` re-reads the dial, so a stale panel's button hands out nothing) and pinned by `tests/web/test_game_dials_are_enforced.py` |
| `panel_message_id` / `panel_channel_id` | 0 | bot bookkeeping, not dashboard-editable |

Dashboard: **Economy → Wagering → Casino** (`config-casino.js`, admin-only;
`PUT /api/config/casino`, ids as strings). The panel titles itself with the
guild's casino name, which is edited on **Config → Server → Branding**
(`PUT /api/config/branding`, `casino_name`; blank = the built-in default). Saves dispatch
`casino_config_change` so the cog re-ensures the panel without a restart
(post/edit/move/tear down; a channel move deletes the old panel).

The five `pools_*` keys are **not** on that page. They have
their own admin-only **Economy → Wagering → Pools** page (`config-pools.js`): a day-long
parimutuel round whose takeout is burned had nothing in common with the
ordinary house tables, and `pools_takeout_pct` sat one card from
`jackpot_cut_pct`, which re-mints what it skims. The keys keep their
`casino_pools_*` names in the `config` table, both pages write through the same
`PUT /api/config/casino` (its body model is every-field-optional, so the Pools
page sends five fields and leaves the rest untouched), and both therefore
dispatch `casino_config_change`. There is no `/api/config/pools` route.

## Games

- **Coinflip**: heads/tails picker → amount ladder. Win pays total
  `stake*37//20` (1.85×).
- **Slots**: one weighted 26-symbol reel × 3 pulls
  (🌻6 🍀5 🐝5 🌾4 🦋3 🍯2 7️⃣1). Precedence triple > two-sevens (5×) >
  non-seven pair (1.45× floored); triples 6/8/9/12/18/40/**120×** (a jackpot
  win renames the card's title to "The Jackpot Spills" instead of coloring
  it, since a bare card has no accent to escalate). The pair carries the table:
  it lands on 41.4% of
  spins and contributes ~0.60 of the ~0.91 RTP, which is why the edge is
  taken there rather than off the triples players actually remember.
- **Blackjack**: fresh shuffled deck per hand, dealer stands all 17,
  naturals 3:2 (resolved at deal, either side), double on first two cards
  only (second debit through `take_stake`), no split/insurance. One live
  hand per member (partial unique index backstops the pre-check). Buttons
  are DynamicItems (`casino_bj:{action}:{hand_id}`) so they survive
  restarts; only the owner may press. Idle hands auto-stand via the 60s
  maintenance sweep; the hand's ephemeral message is repainted through a
  cog-held `(followup webhook, message_id)` handle (`_bj_followups`,
  in-memory, swept after the 15-minute token TTL); the settle itself never
  depends on that edit. **Boot refunds every live hand** (honest reset; the
  old message is unreachable post-restart, so the register feed's
  `casino_refund` entry is the player-facing notice, and stale buttons
  answer "already finished").
- **Roulette**: European single zero. One open round **per player**
  (migration 158: partial unique index on `(guild_id, user_id)`, replacing the
  channel-scoped one), rendered in that player's own ephemeral message. Bets
  (red/black 2×, dozens 3×, straight 0–36 36×) debit at placement via buttons
  (`casino_rl:{kind}:{round_id}`) + amount ladder. **🎯 Number** inserts a
  number step first, two selects splitting the wheel 0–18 / 19–36 because 37
  values overflow a select's 25-option cap. The board repaints
  immediately (no debounce: a private round has no burst of several
  bettors to coalesce). The player presses **🎡 Spin**
  (`casino_go:roulette:{round_id}`) when ready; there is no betting deadline.
  The board says so (casino-132): every private-round card opens with
  `_solo_board_lead`: "Your own wheel. Stack bets, then press 🎡 Spin. Left
  alone it spins itself <t:R>." (race/Race, shoe/Deal, table/Roll, draw/Draw
  for the other four), titled "Your Wheel"/"Your Race"/…, never "Bets
  Open!", and an empty board says "No bets yet." rather than "be first".
  The old communal-countdown copy left a third of prod rounds to the
  abandonment sweep; `round_idle_seconds` stays 600 because `closes_at` is
  stamped at open and a bet never extends it, so a shorter TTL would refuse
  late bets rather than merely resolve idle boards sooner.
  The settle takes the `status='open'` claim → exactly-once, the show plays
  into the same ephemeral message, and the result posts publicly only if it
  clears `broadcast_min_payout`.
- **Roulette, the derby, baccarat, dice and keno are ONE machine**: the
  private-round family (`RoundTables` descriptor in `casino/service.py`:
  open/place/settle/void with the exactly-once claims, and the `_WindowUI`
  descriptor driving the cog's open/repaint/resolve flow) is
  implemented once and parameterized per game, so a money-safety fix can
  never land in one game and miss the others. Per-game code is the
  paytable, the bet columns/validation, the layouts and the show frames.
- **Derby** (plan: `docs/plans/casino-derby.md`): the
  shared windowed machinery re-raced: six fixed runners
  (`casino_logic.DERBY_FIELD`, weights /100 × total-return ratios:
  🐇 38·2.5×, 🦔 19·5×, 🐝 13·7×, 🦋 12·8×, 🐢 10·9.5×, 🐌 8·12×), win
  bets only. One open race per player; runner buttons
  (`casino_dy:{runner}:{round_id}`) + amount ladder debit at placement; **🏇
  Race** draws the winner (weighted), settles exactly-once, then plays
  `derby_frames` into the player's own ephemeral message (money **before**
  the first frame). Same boot refund, idle backstop and void rules as
  roulette.
- **Baccarat** (plan: `docs/plans/casino-classics-and-prediction-market.md` Stage 1a): punto banco
  on the shared windowed machinery: side buttons
  (`casino_bc:{player|banker|tie}:{round_id}`) + amount ladder, fixed
  third-card tableau (`casino_logic._banker_draws`), cards drawn from an
  **infinite shoe** (rank uniform /13) so the RTP is exact enumeration,
  not sampling. Paytable is **EZ-Baccarat commission-free**: Player/Banker
  1:1, ties push the side bets, a Banker win on a **three-card 7 pushes
  Banker bets** (the Dragon-7 bar standing in for the 5% commission), Tie
  pays 8:1 (9× total return). Pinned RTPs (exact-EV test): Player 98.77%,
  Banker 98.98%, Tie 85.88%. Player/Banker are deliberately the best odds
  in the house; Tie is the labeled long shot. The dealt coup persists as
  JSON in the round's `result` column (the outcome is the cards, not a
  number). One deal frame, then the result card, where pushes list under
  "Pushed" and only a genuine win (payout > stake) leads the heading with
  ✅ rather than ❌. Recap carries 🎴 Next Hand.
- **Dice / Sic Bo** (plan: `docs/plans/casino-classics-and-prediction-market.md` Stage 1b): three dice,
  one roll on the shared machinery: call buttons
  (`casino_dc:{big|small|odd|even}:{round_id}`) + amount ladder. v1 keeps
  the classic even-money quartet (Big 11–17, Small 4–10, Odd, Even), each
  2× total return, **all losing to any triple** (that exclusion is the
  house edge). Every bet enumerates to exactly 105/216 wins → 97.22% RTP,
  pinned by the exact-EV test; exact-total/triple bets are deliberately
  deferred (their casino pays are sucker-bet territory). The roll persists
  as JSON `[d1, d2, d3]` in the round's `result` column; one tumble frame,
  then die-face verdict (a triple names the sweep). Recap carries
  🎲 Next Roll.
- **Keno** (plan: `docs/plans/casino-classics-and-prediction-market.md` Stage 1d): 20 of 80
  drawn once per private round on the shared machinery. Tier buttons
  (`casino_kn:{4|6|8|10}:{round_id}`) + amount ladder; the ticket's numbers
  are **quick-picked by the house** at placement (`keno_quick_pick`,
  echoed back in the confirmation and on the bets board; a manual-numbers
  modal is a possible later iteration). Paytables are **bespoke**
  (`KENO_PAYTABLE`), built on the exact hypergeometric to land every tier
  at ~94.7–95.5% RTP (real casino keno's 65–75% has no place here), with
  frequent low-catch money-back moments and splashy tops (Pick-8 solid
  pays 5000×). Pinned per-tier by the EV test. The draw persists as JSON
  in the round's `result` column; one hopper frame, then the two-row
  number board. Recap carries 🔢 Next Draw. Keno's rare huge multipliers
  are why `payout` mints stay within the same `pay_out` path, with no special
  casing.
  Keno is the one windowed game that **itemizes its losing tickets**
  ("No payout", above the house-keeps line) instead of collapsing them
  into a total: a ticket that caught 3 of 10 is otherwise
  indistinguishable from one the house failed to pay, which is exactly
  the complaint that produced this. Each settled line comes from
  `describe_keno_result`: picks with the hits **bolded**, `caught N`,
  and on an unpaid ticket the threshold it needed, read off
  `KENO_PAYTABLE` via `keno_pay_threshold` so it can never drift from
  `keno_payout`. Tiers whose floor returns 1× (Pick-6 at 2, Pick-8 at 3,
  Pick-10 at 4) read "N returns your stake", never "N pays"; the
  break-even tier must not be advertised as a win. The draw reaches that
  line through `_WindowUI.annotate_bet`, an optional result-time hook the
  other four games leave `None`; `describe_bet` can't do it, since the
  bets board renders before anything is drawn.
- **War** (plan: `docs/plans/casino-classics-and-prediction-market.md` Stage 1c): one card
  each from the infinite shoe, aces high, high card pays even money. 12 of
  13 plays settle instantly (ephemeral result + 🔁 Battle again, on the
  floor ticker via `TICKER_GAMES`) and never persist a row; the ~1/13 tie
  opens a **live decision** row (`casino_war_hands`, the blackjack shape:
  one live decision per member via partial unique index, exactly-once
  `settled_at IS NULL` settlement, in-transaction claims with ownership,
  the raise derived from the row never the caller). **Go to War** debits a
  matching raise (bet limits waived like the double-down; daily cap and
  funds still apply) and one more card each decides: win **or second tie**
  pays 3× the original (the raise pays even, the original pushes); lose
  and both stakes fall. **Retreat** surrenders half (floored). No Tie side
  bet, since its ~19% edge has no place here. Pinned exact RTPs: always-war
  **177/182 ≈ 97.25%**, always-retreat 25/26 ≈ 96.15%. War is strictly
  better, so the idle sweep (same `blackjack_idle_seconds` clock)
  **defaults to war, falling back to retreat** when the raise can't be
  debited; boot sweeps refund pending standoffs, and a leaver's pending
  standoff refunds via `refund_member_live_stakes`. The retreat's partial
  return means the jackpot feeds on the **lost portion** of the stake
  (`stake − payout`), not only total losses.

- **Mines** (plan: `docs/plans/casino-mines.md`): a **20-tile grid** (5 wide × 4 tall) hiding a player-chosen
  1/3/5/10 bombs. Each safe reveal steps a multiplier; **Cash Out** banks it;
  a bomb takes the stake. The grid is 20 rather than 25 because Discord allows
  25 components per message, and a 5×5 board leaves nowhere to put the stop
  button, the voluntary stop being the entire game.
  **The ladder is generated, not typed** (`MINES_LADDERS`): the paid rung is
  `round(0.95 × C(n,k)/C(n−m,k) × 100)` hundredths, so `P(reach k) × pay(k) =
  0.95` identically and **every cash-out point on every ladder returns 95%**
  before rounding; 43 rungs pinned in [0.9480, 0.9540] by exact enumeration
  written independently of the generator. The ladder **stops at the last rung
  paying ≤ 25×** (uncapped, a 10-bomb clear pays 175,518×), which lands all
  four bomb counts at an 18.6–21.9× ceiling reachable ~5% of the time: the risk
  dial changes the road (19 nervous presses vs 4 brutal ones), not the
  destination. Clearing the top rung **auto-cashes**: the ceiling ends the
  round, so there is no infinite climb to press into.
  **Mines rounds half up where every other table floors** (`mines_payout`),
  and the deviation is arithmetic, not taste: it is the only game paying a
  ladder of tiny multipliers, and flooring a 1.19× rung on a 5-coin stake pays
  5 against a 5.95 expectation, which collapses that cash-out point to **80% RTP**
  for a paytable that is exactly 95% on paper. A second test pins the integer
  drift at `min_bet` so nobody "fixes" it back.
  **The third live-hand game** (`casino_mines_hands`, migration 164): one live
  grid per member via partial unique index, exactly-once `settled_at IS NULL`
  settlement, in-transaction claims carrying ownership (a rejected press must
  not bump `last_action_at`, or a stranger could block the auto-cash and
  strand the owner's stake). Bomb positions are drawn **once at deal** and
  never re-rolled (no adaptive difficulty), and a live `MinesStep` never
  carries them, so nothing downstream can leak a board still being bet into.
  The idle sweep (same `blackjack_idle_seconds` clock) **auto-cashes at the
  rung reached**, exactly what the player's own press would have paid, so
  walking away costs nothing that staying would not have, and **refunds in
  full** an untouched grid, which has no rung to settle. Boot sweeps refund
  live grids; a leaver's grid refunds via `refund_member_live_stakes`.
  **Two UI rules come from the responsible-design notes, not from taste:** a
  cash-out does **not** reveal the tiles you walked away from (that
  manufactures a near miss out of a correct decision), while a bomb reveals
  the whole board (that is what makes the loss legible); and a rung that only
  returns the stake (the one-bomb ladder's 1.00× first step) settles as
  `pushed` and reads "broke even", never as a win.

Every terminal path settles or refunds, exactly-once via
`settled_at IS NULL` / `status='open'` claims, so a stake can never evaporate
or double-pay, including replayed timers and double-clicks. Because the
pre-checks run in autocommit (legacy DEFERRED isolation), every money-moving
path **re-claims its row inside the write transaction** with a guarded
no-op UPDATE before the debit: `place_roulette_bet`, `place_race_bet`,
`place_baccarat_bet`, `place_dice_bet` and `place_keno_ticket`
(a buzzer-beater bet racing the resolution misses the claim instead of
stranding a stake), `double_blackjack_stake`, `reveal_mines_tile` / `cash_out_mines_hand`, and `resolve_blackjack_action` (which also bumps
`last_action_at`, resetting the idle clock per press, and reports
"already finished" instead of rendering an outcome the settle didn't pay).

Recovery is layered: a round whose ephemeral send fails is voided
immediately (the player could neither bet nor resolve it); the 60s
maintenance sweep auto-stands idle blackjack hands **and resolves any open
round past `closes_at`**, which for a private round is not a backstop but
the *primary* auto-resolve (the abandonment TTL, since nothing else would
ever finish a round its player walked away from; the sweep skips the
cosmetic frames so a pile-up can't stall it); and boot **refunds** live
hands and rounds alike, because a restart kills the webhook token their
ephemeral messages are editable through, so no result could ever be
shown. Blackjack game rules (double only on two
cards, hit/stand/dealer flow) live in `resolve_blackjack_action` /
`stand_idle_blackjack_hand` in the service (tested, not cog glue), and
the double's second stake is derived from the hand row, never
caller-supplied.

## Pools — the parimutuel prediction market (migrations 140, 148)

Not a table: **player-versus-player with the house as bookkeeper only.** One
round per guild-local day, opened and settled by the bot with no admin
authoring and no admin resolution.

- **The question** is whether some measure of the day lands over or under a
  line. The line is that metric's trailing 7-day median **plus 0.5**, so an
  exact hit is unreachable and there is no push rule to write.
- **The metric rotates** (migration 148). Nine metrics
  live in `casino/pools_metrics.py` and one is drawn uniformly at random
  each day, never the same one two days running. The round row records
  which metric it bet, because settlement recomputes the outcome and the draw has
  moved on by then. Admins pick the roster on the dashboard; the draw
  itself is not configurable. Full reasoning, the per-metric caps and the
  backtest: `docs/plans/pools-metric-rotation.md`.
- **Every count metric is capped per member**, and the cap is stated on the
  panel where members bet. The economy metric is safe structurally (pools'
  own rows are excluded from it); a metric counting what members do has no
  such defense, so the cap is what makes it bettable at all. Caps are code
  constants, and changing one retroactively changes what past days measured.
- **A metric sits out** until it has 7 completed days and, for count metrics
  only, whenever its trailing window contains a zero day. A zero means the
  feature behind it was dormant, and a line across dormancy prices whether
  the bot ran rather than how members behaved.
- **Settlement is arithmetic, not judgment.** Every metric is recomputable
  from stored history at any later time. A missed close, a restart or hours
  of downtime all settle to the same answer, which no other game in the
  casino can say. A round naming a metric the code no longer defines is
  **voided and refunded** rather than guessed at.
- **Session-day attribution.** Both halves of a casino session are booked to
  the day its round or hand *opened*, not to each ledger row's timestamp.
  Without this, a hand dealt at 23:59 and stood at 00:00:30 shifts the metric
  by its whole stake for an expected cost of nothing. `take_stake` records
  `round_id` for this reason.
- **Pools' own rows are excluded** from the metric, or a bigger pool would
  drag the number it is betting on. The same exclusion is made by the
  `handle` metric (Petals staked across the casino) for the same reason.
- **The takeout is burned**, not fed to the jackpot: the pot re-mints what it
  holds, so routing the takeout there would return it to the metric weeks
  later.
- **A one-sided pool voids and refunds in full.** No counterparty means
  nothing to pay winners out of. At 13–18 bettors a day these are routine
  (a quarter of prod markets, most with 2–7 bettors all on one side), so the
  panel says so before it happens (casino-136): the How It Settles field
  always states the both-sides rule, a market with stakes on exactly one
  side grows a **⚠️ One-Sided So Far** field naming the empty side
  (`pools_logic.unbacked_side`), and the panel repaints itself once inside
  the last hour of betting with a "⏰ Last hour" line
  (`pools_logic.closing_soon` / `reminder_due`, checked on the 60s tick
  next to the hourly refresh; any repaint inside the window satisfies it).
  The house never seeds a side: a settled market must be members against
  members.
- **The day-roll sweep answers the idle case from two indexed lookups.** It
  runs on the cog's 60-second maintenance tick, and on all but one tick a
  day there is nothing to do; only a tick with real work computes the day
  series, which is a full ledger scan.
- **The leaver sweep stops at the betting close** for Pools
  (`RoundTables.leavers_until_close`): the round stays `status='open'` for
  hours after betting shuts, and pulling a departing member's stake from a
  closed pool would silently change every remaining bettor's pro-rata payout.

Design and the full record of what was deliberately left out:
`docs/plans/casino-classics-and-prediction-market.md`
Stage 2.

## The fancy layer

PIL-rendered result graphics were considered and parked.

- **Card strips:** the casino's public posts wear the
  guild's thin, untitled **Card Header** and **Card Footer** from Config →
  Server → Branding → Panel Art (`core.layout.card_strips`): the big-win broadcast
  (a Legendary win's `@here` line sits above the header, outside the
  frame), the jackpot celebration, and the Pools panel, result and void
  cards. The panel takes its bet row as `controls` so the footer closes
  the card below the buttons, and a retired panel keeps its frame without
  them. The player's own result cards are ephemeral and stay plain, as the
  hub keeps its tall banner. Nothing shows until a card header is
  uploaded; the Pools panel picks it up on its next repaint.

- **Progressive jackpot**: `feed_jackpot` skims `jackpot_cut_pct`% of every
  fully-lost stake (any game; refunds/voids never feed) into
  `casino_jackpot`; the hub panel shows the pot and the maintenance loop
  repaints on drift. Slots triple-7️⃣ claims `max(pot, 120×stake)`
  exactly-once inside the spin's transaction (`claim_jackpot`: read+reset
  with the flat multiplier as a floor), reseeds to `jackpot_seed`, and
  posts a standalone gold celebration beside the result. The pot is
  bookkeeping over coins the ledger already burned; paying it re-mints
  that recorded slice (`casino_payout`, `meta.jackpot`, never boosted).
  **The cut is therefore an anti-sink**: it is the share of the house's
  take that comes back rather than staying destroyed, which is why it
  ships at 5%, not a quarter.
- **Play stats**: `record_play` (same transaction as every settlement;
  never refunds) maintains `casino_member_stats` (lifetime wagered /
  returned / plays / wins / biggest win / signed streak) and the bounded
  per-ISO-week `casino_weekly` rollup. Surfaces: an **At the Tables**
  section on `/bank wallet`, and **Night at the Tables** on the live
  leaderboard (week's biggest win + best multiplier, named; public play
  is opting in, the raffle rule).
- **Celebrations**: `is_big_win` (≥10×) prefixes the paid winner's line in
  a result card with 💥 (a bare card has no accent to escalate to gold);
  streak callouts (🔥/🧊 at |streak| ≥ 3) ride instant-game and blackjack
  results.
- **Tiered animations**: `is_big_bet` (≥70% of `max_bet`, or ≥100 coins
  uncapped; fixed constants) gates the staged reveals: slots reels stop
  one at a time (`SLOTS_REEL_STOP_SECONDS`, 1.4s per stop; the reel row
  renders bare on every slots frame, spinning reels shown as 🌀; there is no
  text-art cabinet box, because emoji widths vary by client so no box can be
  made to fit around the reels outside a code span, and a code span would
  strip the reel emoji of their color), coinflip hangs in the air, blackjack pauses on the
  hole-card flip. Roulette's once-per-round resolution always gets a
  two-frame ball bounce. **Money settles before the first frame**: a
  crash mid-show leaves a stale message, never a wrong balance.

## UX layer

- **Loop-closers:** every instant/blackjack result carries a persistent
  🔁 button (`casino_again:{game}:{side}:{amount}`) that replays the same
  stake **for whoever clicks** (their coins; every guard re-applies), and
  respins the same ephemeral message in place. Every private round's recap
  carries 🔁 Play Again, which opens a fresh one. Stale buttons stay safe:
  stakes re-validate at click.
- **Public recaps carry no buttons.** The big-win broadcast does not repost
  the player's own view (Play Again on the instant games, Next Round on the
  private-round family) as a "me too" invitation: a bystander seeing a big
  win could otherwise play on the spot. The buttons live only on the player's
  own card, on every path that posts a public recap (including the
  `_auto_resolve_hand` blackjack/war idle sweep). Pinned by `tests/casino/test_casino_big_win_broadcast.py`,
  which asserts the send reaches `channel.send` with no view.
- **Bet steps expire out loud** (casino-141). The ladder, the
  coinflip side picker and the roulette number picker are `StepView`s
  (`views.py`): `_show_step` (and `_back_to`, and the hub's Coinflip pick)
  **binds** the view to the interaction that showed it, and on timeout the
  view disables its items and edits the message through
  `edit_original_response` to `STEP_EXPIRED_MSG` — "This step expired —
  pick the table on the panel again." — instead of leaving buttons
  that answer with Discord's generic "This interaction failed". The
  interaction token is the only handle on an ephemeral message and lives
  ~15 min; every step's timeout (120s / 600s) sits inside it. A step that
  carries an `on_expiry` (the private-round tables, whose board repaint
  replaces the message) keeps that behavior and does not also write the
  expiry copy, since the two would race. A failed edit is swallowed: the member
  dismissing the message is the ordinary end of an abandoned step.
  **The clock stops when the step does** (games-deep-review K1):
  discord.py keeps a replaced view's timeout running (an
  edit with a new view cancels nothing), so a coinflip picker whose message
  has become the result card would wake 120s later and write the expiry copy
  over the card and its Play Again button (and the ladder's 600s clock over a
  blackjack hand still in play). Every press on a step notes the step on
  `interaction.extras` (`StepView.interaction_check`; the Custom… modal
  carries it as `step` because its submit is a fresh interaction), and each
  path that writes something else into that message (`_show_step`,
  `_respond_private`, `_back_to`, the private-round Back and
  `_finish_window_bet`) calls `consume_step`, which stops the view; an
  expiry that still arrives after that is a no-op. A press that only opens
  a modal or an apology consumes nothing: that step is still standing and
  still expires out loud.
- **The amount ladder** (todo #96 / audit M2): choosing a stake
  is buttons, not typing. `logic.bet_amount_options` builds at most four
  rungs (**Last · Half · Double · Max** off the remembered last stake, or
  **Min · a round middle · Max** on a first bet) plus **Custom…**, which
  opens the old modal unchanged. Every rung is placeable: the ceiling is the
  table maximum, the balance and the daily-cap headroom, whichever binds
  first, so a tap can never come back as an error. An empty ladder (broke, or
  capped out) falls back to the modal, whose service call gives the real
  reason. A press from inside a private surface **replaces it in place**
  (`_show_step`), so a wager still costs no extra message; a press on the
  public hub opens one. On the five private-round tables the step covers the
  board, so it carries **Back**, and a timeout restores the board too, but
  only if that step still owns the board (`_window_steps`): discord.py starts
  a fresh timeout per view and cancels none of the ones it replaces, so an
  abandoned step would otherwise wake minutes later and repaint over whatever
  the player is in the middle of (a step whose own press replaced it is also
  stopped outright; see *The clock stops when the step does* above; the
  claim still covers a board repainted by something other than a press, such
  as the resolve sweep). A **refused** bet repaints too, since the
  step is standing where the board was.
  Coinflip and Mines choose a side/risk first, so their ladder carries Back
  as well, re-rendering that picker. The number step spends rows 0 and 1 on
  its two selects, so its Back sits on row 2 (a select fills a whole row,
  so a Back on row 1 makes the view refuse to build).
- **Informed bets:** the label on the Custom… modal carries live limits and cap
  headroom ("Your bet (5–100 · 340 left today)") and pre-fills the
  member's last stake per game (in-memory), the same numbers that shape the
  ladder. The cap error names its reset
  time; the hub's 📊 My Stats option shows the personal tally + today's
  cap usage ephemerally.
- **Players are named, never mentioned** (todo #90): every card
  that shows a player renders a plain display name through an injected
  `name_fn`, built by `shared/name_resolver.build_name_fn` (live member
  cache → `known_users.display_name` → `username` → `<@id>`, markdown-escaped).
  A `<@id>` inside a card is resolved by the **reading** client from its own
  cache (Discord's servers do nothing to it), so it degrades to a bare numeric
  id for any viewer who hasn't seen that player. That is the normal case here,
  not an edge case: the hub ticker and 📊 standings name *past* betters, and a
  result card is read by the whole channel, not just the player on it. The
  builders require an explicit `name_fn`, with no default, so a caller cannot
  forget to wire the resolver; `tests/casino/test_casino_layouts.py` holds both
  halves of the contract (no raw `<@id>` in any TextDisplay, and every
  member-naming card exercised with a resolver). Whisper follows the same rule.
- **Jackpot feedback:** losing results append "the loss feeds the jackpot
  — now N" (from the settle's own transaction), so the jackpot's funding is
  visible instead of silent. Blackjack hides Double
  Down when the clicker can't afford the second stake.

## Storage (migrations 113 + 114 + 127 + 128 + 206)

`casino_daily`, `casino_blackjack_hands` (state_json = deck/player/dealer,
`settled_at` guard, partial unique live index), `casino_roulette_rounds`
(open|settled|void, partial unique open-per-player since migration 158),
`casino_roulette_bets`;
114 adds `casino_jackpot` (one row per guild), `casino_member_stats` and
`casino_weekly` (bounded upserts, no per-play log); 127 adds
`casino_race_rounds` + `casino_race_bets` (the roulette pair's shape, with
`winner`/`runner` in place of `result`/bet type); 128 adds `casino_ticker`
(the hub floor ticker's bounded per-play log, every `TICKER_GAMES` game; trimmed to `TICKER_KEEP` rows per
guild on insert, read back one row per player); 206 adds
`casino_daily.comp_claimed` (the daily comp's once-a-day book, on the cap's
own day-keyed row, so a comp row can exist with `wagered` 0 even in an
uncapped guild).

## Files

`casino/service.py` (money + settings + persistence) ·
`casino/logic.py` (paytables, RNG at module level) ·
`casino/cog/` (cog + views + layouts glue) · `web_server/routes/config.py`
(`_casino_section`, `update_casino`) · `static/js/panels/config-casino.js`.

Pools adds `casino/pools_logic.py` (pool split, line, candle assembly),
`casino/pools_metrics.py` (the metric roster, the per-member caps and the
daily draw), `casino/pools_service.py` (the economy metric and the
day-roll plan), `casino/pools_charts.py` (matplotlib renderers),
`casino/cog/pools_panel.py` (a mixin on `CasinoCog`, kept out of the
120k `cog.py`) and `static/js/panels/config-pools.js` (its own dashboard page,
with no route of its own; it partial-saves through `update_casino`).

## Testing

`tests/casino/test_casino_logic.py`: exact-EV enumeration pins each paytable's
RTP band (slots 0.90–0.96, coinflip 0.95, roulette single-zero, derby
per-runner 0.90–0.97 with weights summing to 100), blackjack settle
matrix, wheel/dozen/straight payouts, derby race-frame invariants (winner
finishes first and alone, positions only advance).
`tests/casino/test_casino_service.py`: the full `take_stake` guard cascade, cap
accounting across local days, no-boost payouts, blackjack lifecycle
(exactly-once settle, boot sweep, idle sweep, double), and private rounds
(one-per-**player** as a parametrized row per game, the partial index
refusing a second round when the pre-check is bypassed, betting into an
abandoned round, an idle auto-resolve racing a manual resolve, the boot
refund paying exactly once and replaying free, two players' rounds
settling independently, exactly-once settle/void, conservation, jackpot
feeding, the buzzer-beater claim, leaver refunds). Migration 158 has its
own file (`tests/migrations/test_migration_158_casino_private_rounds.py`) proving the index swap in both directions with real INSERTs.
The ticker rides `tests/casino/test_casino_service.py` (rows land via every
settle path including the five private-round games, refunds and pools
stay off it, per-guild trim to `TICKER_KEEP`, one row per player and six
players surviving a 100-spin grind) and `tests/casino/test_casino_layouts.py` (hub "On the
floor" section renders newest-first, omitted when empty, push/partial
lines, every ticker game wearing its own emoji; the standings field crowns
the winner and never prints the loser; the five private boards name their
button and never say "be first"; the pools panel names an empty side and
its last hour; the timing rules themselves are rows in
`tests/casino/test_pools_logic.py`). The hub panel's frame is
`tests/casino/test_casino_hub_layout.py` (closed tables drop off the dropdown in
menu order, every option has a description within Discord's 100
characters, all-closed says so, the comp button only while `comp_on`, Help
beside the title or the intro, banner first and footer last, the fullest
panel within budget); routing a pick (the table opens, then the repaint
that resets the dropdown is scheduled) and the registered views answering
every stale id are in `tests/casino/test_casino_views.py`. Name resolution is a two-part contract in
`tests/casino/test_casino_layouts.py`: a parametrized table renders every
player-naming builder and fails on any surviving `<@id>` (a new builder adds
one row), and an AST guard walks `cog.py`/`pools_panel.py` requiring every
render site to pass a `name_fn`, as defense-in-depth for a required
keyword-only argument that already raises `TypeError` if a call site omits
it (`tests/contracts/test_embed_name_render_sites.py`, the repo-wide sweep, skips
casino because this is that guard).
`tests/web/test_casino_routes.py`: section shape (string ids), PUT
persistence + guards, `broadcast_min_payout` roundtrip/bounds,
`broadcast_ping_enabled` roundtrip (default on), the 840s idle cap;
authz/snowflake/browser sweeps cover the panel automatically.
The big-win broadcast is a three-layer contract: the tier ladder and the
percentile/floor interaction in `tests/casino/test_casino_logic.py` (including that
an unknown percentile withholds the ping rather than passing it, that a
percentile can never create a broadcast the dial switched off, that
`ping_enabled=False` mutes the `@here` while leaving the header and lead line
untouched, the documented supersession, and `test_every_ladder_rung_is_reachable` guarding the dead-rung
defect), the rolling window in `tests/casino/test_casino_service.py` (sample floor,
per-guild scoping, trim keeping the newest, a schema assertion that the table
holds no `user_id`, the band never rounding loose at the sample floor, and
`record_play` no longer banking at all), the
card in `tests/casino/test_casino_layouts.py` (header replaces the game title, the
recap text survives, and the player's own card is never mutated, since it is
copied, not re-rendered), and the cog
seam in `tests/casino/test_casino_big_win_broadcast.py` (no view, `@here` with
`everyone=True` only on the top rung, a thin history or a failed percentile
read still broadcasting, the ping dial muting the `@here` while the Legendary
card still posts, a failed dial read withholding the ping, a push posting and banking nothing, one banked row
per announcement, and the current win not being ranked against itself).
