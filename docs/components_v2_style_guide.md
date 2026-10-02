# Components V2 card guide (Design spec)

How a bot-generated card is built once it is a **Components V2 `LayoutView`**
rather than a `discord.Embed`. Written 2026-09-20 from a live pilot in
`#🤖│bot-command-spam` (the perk shop three ways plus two probe cards, by a
throwaway script since deleted), the discord.py 2.7.1 source, Discord's
component reference, and the writeups listed at the end. The decision it
records: **new cards are built this way, and the existing embeds convert over
time**. The conversion is one-way per message and touches ~386 `Embed(` sites
in 108 files, so it is a staged migration with its own plan, not a sweep.

This guide owns only what changes. Everything about *copy* (casing, the ❌
rule, voice, currency, ledger rows, timestamps, empty states, slash commands,
modals, persistent and expiring views, DM branding, reach, pointing at things,
dashboard copy) is unchanged and stays in `embed_style_guide.md`; § *What
carries over* below lists those sections by name so nothing is restated. Where
the two disagree, this one wins for a V2 card and the old one for an embed.

## What V2 is, in one paragraph

A message with the `IS_COMPONENTS_V2` flag has **no `content` and no
`embeds`**; its whole body is a tree of components: `TextDisplay` (markdown),
`Section` (1–3 text displays with a Button or Thumbnail beside them),
`Container` (a bordered group with an optional accent bar), `Separator`,
`MediaGallery` (1–10 images), `Thumbnail`, `File`, and the familiar
`ActionRow`. discord.py sets the flag for you whenever the view is a
`LayoutView` (`http.py`, `handle_message_parameters`). The flag is per-message
and **cannot be removed** once set; an embed message *can* be edited into a V2
one. Attachments are not shown unless a component references them; polls and
stickers are unavailable. Limits: **40 components per message, counting nested
ones**, and **4000 characters across every Text Display** (embeds allowed
6000). Both are enforced by Discord, not the library.

## What the pilot established (2026-09-20)

Facts, not opinions: each one changes a rule below.

- **A `<@id>` in a Text Display behaves like message `content`, not like an
  embed.** With the default `allowed_mentions`, the guild owner named inside a
  Text Display **was notified**, and the fetched message's `mentions` array
  named them. With `AllowedMentions.none()` the same text produced no
  notification and an **empty** `mentions` array, which is also what makes the
  name render from the *reader's* cache, exactly as in an embed. So V2 gives
  us a way to ping from inside a card, and a brand-new way to ping by accident.
- **`Container(accent_color=…)` takes what `safe_resolve_accent` returns**
  (`discord.Color | None`), a literal drop-in, had we wanted it. The
  comparison was posted both ways, and the bare rendering won (§ No container).
- **The 40-component cap binds before the 4000-character cap.** The full shop
  listing (seven perk rows, each a `Section` with a button) came to **37/40
  components at 1,133/4000 characters**. A Section costs three (itself, its
  text, its accessory).
- **Monospace tables and `▰▱` bars render the same**: the code-span rules
  from the embed guide hold, verified on a phone.
- **Animated media works** in a `Thumbnail` and a `MediaGallery` (a 256 px,
  80-frame GIF, 2.1 MB). Both landed as real CDN attachments, yet
  **`message.attachments` reads empty on a V2 message**: component-referenced
  files are hidden from that array.
- **Row buttons read well.** Billy's verdict on the phone comparison was for
  the V2 cards, the bare one over the boxed one, and the button-beside-each-row
  layout specifically. Discord
  will not lay two buttons side by side inside a Section on purpose (a staff
  answer in the inline-fields discussion: it would push developers into
  too-short labels on mobile).
- `interaction.response.send_message(view=LayoutView, ephemeral=True)` works:
  the ephemeral flag is set first and the V2 flag OR-ed in. Followups,
  `edit_message`, and modals (`TextDisplay` + `Label`, already in the TTL
  modal) take V2 too.

## Mentions and names (the rule that changed)

- **The bot pings nobody unless a send says so.** The client is constructed
  with `allowed_mentions=AllowedMentions.none()` (Billy's call, 2026-09-20),
  so a card that names someone is silent by construction and a send that
  should notify passes an explicit allow-list, as the game-start, role
  ping and weekly-flip sends already did. An embed never pinged; converting
  its builder and sending the result under discord.py's old default would
  have made a formerly silent card notify everyone it names, and that is the
  regression class the global default designs out rather than reviews for.
  The flip lands with an AST audit of every send whose text carries a mention
  and no `allowed_mentions=`: each one that should ping gets its allow-list in
  the same commit, and a test keeps that list at zero afterwards.
- **An admin-authored template allow-lists from the template, not the
  render.** A greeter arrival line or a `/grant` announcement is text an
  admin typed, so `core.utils.template_pings(template, users=…, roles=…)`
  reads the raw template for `@everyone`/`@here`, `<@&id>` and `<@id>` and
  adds the placeholder targets; deriving from the rendered string instead
  would let a member named `@everyone` widen it by renaming themselves.
- **A send in one file, a mention built in another, is the shape that keeps
  getting missed.** The AST sweep reads the text at the send, so it is blind
  to a mention assembled by a helper (`name_resolver.mention(uid)`), collected
  into a list with `append`, passed through a shared base-class poster
  (`duels.base_game._announce_to_channel`), or handed over by a callback
  fetched out of a dict at runtime (`bot.game_joiners`). It now reads the
  first three; for the fourth the *ids* travel with the text and the site gets
  a `CROSS_FUNCTION_PINGS` row. **Never derive an allow-list by re-reading the
  rendered message**: a display name is member-written, so scraping `<@id>`
  back out of it lets a member choose who gets notified, the same trap
  `template_pings` exists to close. A shared send helper defaults to
  suppressing: pass the allow-list in, don't let it guess.
- **Naming a member is still `name_fn(id)`, never `<@id>`.** With pings
  suppressed a component mention renders from the reader's cache, so the
  bare-number failure the embed rule exists for is unchanged. `build_name_fn`,
  the `name_fn` parameter with no default, and the render-site guards all
  carry over verbatim (→ `embed_style_guide.md` § Naming members in embeds).
  The no-contact degradation to `User <id>` carries over too.
- **A deliberate ping is `<@id>` plus an allow-list of exactly that user or
  role**. Because a V2 message has no `content=` line to carry it, the
  card body is now the *only* place a ping can live. Name a person once: a
  ping *or* a resolved name, never both. The weekly-flip pattern
  (`AllowedMentions(roles=[Object(id=…)])`) is the model. **A turn ping is the
  exception to "names are resolved, never mentioned":** the AMA question card's
  `<@seat>` and a Rushmore pick ping notify the one member whose move it is, so
  they stay a raw mention allow-listed to that single id; don't "fix" them into
  resolved names.
- `tests/contracts/test_embed_mention_guard.py` sinks are embed slots. Before the first
  conversion it grows two sinks: a string or f-string argument to
  `TextDisplay(` and a string child of `Section(`. `KNOWN_EMBED_MENTIONS`
  rows for deliberate pings say so beside the row.

## No container, no accent bar

**A card is bare.** Its Text Displays, Section rows, Separators and galleries
sit directly on the message: no `Container`, no box outline, no colored bar
of any kind. Billy's call on 2026-09-20 after seeing the shop three ways:
"drop the big bars … the modern simple look". It is the whole point of
converting: the embed's block was the visual noise. Consequences:

- **A layout builder takes no accent and calls no resolver.** The
  `safe_resolve_accent` rule (→ `embed_style_guide.md` § Color) is an embed
  rule; a `LayoutView` never sees a color, so the accent-contract table
  (`tests/contracts/test_embed_accent_contract.py`) gets no V2 rows. What replaces it is
  a repo-wide guard the other way round: **no `ui.Container(` is constructed
  anywhere in `src/`**; the sweep hard-fails on one outside its allow-list.
  A container someone wants badly enough to add to that list is a design
  question for Billy, not a local choice.
- **The hub panels are bare too: no box.** The five channel hubs (DM
  Requests, Help & Settings, the Perk Shop, the economy panel, the casino)
  once sat in one `Container`, but inside a box the text and the buttons
  overhung the banner's gold frame, and Ben wanted them "in the width of the
  yellow line". Discord can't fix that inside a box: there is no way to pad
  a Text Display, none to hide a Container's fill or outline, and a leading
  space is trimmed. So the panel art is drawn with its frame's sides at the
  picture's edge (§ Media), which is the edge the text starts on and the
  buttons end on. The allow-list is empty. The **hub archetype** is the art,
  the title and the grey **Help**; see the checklist under § Controls.
- **Setting `accent_colour` can't make a bar vanish**, should a box ever
  come back: every Discord theme paints the card a different grey, so any
  color chosen to match one theme shows as a stripe on the others.
- **Semantic color goes too.** There is no red bar on a denial or a green
  one on a receipt; the ❌ rule and the copy carry the meaning, as they
  already had to for anyone reading on a phone with the bar off-screen. The
  semantic set and the identity palettes in § Color apply only to embeds that
  haven't converted yet.
- **Weight comes from structure, not chrome:** a `##` heading, a thumbnail
  beside the subject, a button beside a row, a `-#` subtext line. If a bare
  card reads as unfinished, the fix is a heading or a separator, never a box.
- **The Branding accent dial retires with the last embed.** A preference the
  bot no longer reads is a dead control (CLAUDE.md: never ship a toggle that
  isn't enforced), so the migration's final stage removes the dial, the
  resolver and the contract table together, and not before, because every
  unconverted embed still reads it.
- `spoiler=True` on a `Container` is therefore unavailable; spoilers go on the
  media item (§ Media).

## Anatomy: which component does which job

The embed had slots; a layout has a tree. This is the mapping, in the order
the parts appear on the card.

- **Heading = a `## Title` line in the first `TextDisplay`**, Title Case, led
  by the feature glyph when sibling cards are (→ § Titles, labels & casing;
  § Card anatomy's glyph rule). The subtitle sentence follows on the next
  line. A masked link is fine anywhere now; the title/field-name restriction
  was an embed rendering quirk. **Every card**, including an ephemeral
  confirm, the one-liner that answers it and a mod-feed notice: there is no
  lighter class of card that drops to `###` or goes glyph-less. The pilot's
  remove flow shipped four of those plus its audit notice and they read as
  half-styled beside the receipts they sit next to. `dm_perms`'
  `test_every_card_leads_with_a_glyph_led_title` is the sweep that catches
  the next one, and a converting feature is cheapest to give the same one.
  **One exception: a hub whose title is drawn into its banner** (§ Media,
  the title in the banner). The drawn title *is* the heading, and a `##`
  line under it would say the name twice, so the builder drops it and the
  banner's alt text carries the title. That variant stays out of the
  heading sweep's card table and is pinned by its own layout test instead.
- **The person the card is about = a `Section` whose accessory is a
  `Thumbnail` of their avatar**, with the name (via `name_fn`) in the
  Section's text. This replaces `set_author` + `set_thumbnail`. Thumbnail
  semantics are unchanged: currency icon on money cards, member avatar on
  person cards, server icon on server-level panels.
- **A row with its own action = a `Section` with a `Button` accessory.** This
  is the layout an embed could not do and the one the pilot was liked for. One
  button per row, label **one or two words** (the row text already names the
  thing, so `🎨 Color`, `Claim`, `Rent`; never the price) **and at most 10
  columns wide, an emoji counting two** (`core.layout.MAX_ACCESSORY_LABEL`,
  measured by `core.layout.text_columns`). Keep the row text to one line:
  on a phone the button sits beside it. It is the *label* that moves a
  button, not the text: on Billy's phone (2026-09-24, the shop gallery)
  "Browse Colors" at 13 columns dropped its button under the row while "Set
  Color" at 9 and "✨ Rent" stayed beside theirs, and the row whose text
  wrapped kept its button. A wide label is the defect to test for;
  `shop_layouts`'s tests walk every accessory the shop can build.
  **A checklist is not a list of things.** A row with an
  accessory is taller than a text line, so a checklist mixing auto-step
  lines with manual-step Sections draws tight lines and then a padded row
  with a lone Done floating at its right. The intake card keeps every step
  in one text block and puts its buttons in one row
  at the foot, each naming its step ("Done: SFW questions asked", "Give
  role: Main chat") with Dismiss beside them (paradigm 5's shape). The
  rule of thumb: a row earns its own accessory when it is an *item* a
  reader picks from (a perk, a bounty, a connection); the steps of one
  procedure are lines of one list, and their buttons read as one set.
- **Buttons are grey (`secondary`) by default.** Color on a button is a
  meaning, not an emphasis: **red** (`danger`) for a destructive press or
  the no on a decision card, **green** (`success`) for the yes on a decision
  card or an owned / active state. Blurple (`primary`) marks nothing but
  "this one matters more", and beside a grey sibling it reads as a ranking
  of two things that aren't ranked (Billy on the DM Requests panel,
  2026-09-24: "can both buttons be the same light grey … make that the
  default"). A hub's entry points are all grey. `dm_perms` enforces it
  (`test_buttons_are_grey_unless_the_colour_means_something`); cards that
  converted before the rule (the shop's Open Shop, Rent, Sponsor, Pin and
  Themed Day buttons) still carry blurple until they are revisited.
- **A catalog row is the one row that runs to three lines.** A row that
  *sells* something (the Perk Shop's perk rows and its Server Store rows)
  is `**Name**`, a plain one-line description, and a `-#` line carrying the
  price and the status word (`🪙 50/wk · ✅ owned`, `3 left`), in that
  order, one Text Display per `Section`. The one-line rule above is about
  *wrapping*, not line count: each of the three lines is held under the
  ~30-column working number for text beside a button (§ Screen use) on its
  own, so none wraps. **Every row has all three lines**, or the rows read as
  unfinished beside each other (Billy, 2026-09-24, on the first gallery:
  "make sure all the text elements are there for every item and are
  visually regular"): a store item with no admin blurb shows a line saying
  what kind of thing it is (`shop_layouts.STORE_FALLBACK_BLURBS`), and the
  refund row's third line says what comes back. A list of things the member
  already knows (the refund picker, a roster) is two lines (the name, then
  its `-#` line), since a one-line join does not hold at 30 either.
  `shop_layouts` carries the tests that
  hold every line and every label to it.
- **Card-level actions stay in an `ActionRow` at the bottom** (page
  arrows, Cancel). Don't put the same action on a row *and* in the bottom row.
  Page arrows are `◀` / `▶` secondary buttons (the shape every paged view in
  the bot already uses), present only when there is a second page and
  **disabled at the ends rather than hidden**, so the row keeps its shape from
  one page to the next; the subtext says `Page N of M`. A paged *book* whose
  pages are different things (the shop's Specials, spread over several
  pages, captioned by their item span) also carries a **greyed secondary `Button` between the arrows
  naming the page you are on**. Discord has no label component, and a
  disabled chip is how that shop already says "you are here". It never has
  a callback, its label is cut to Discord's 80, and the `Page N of M` line
  stays under it.
- **Between sections, a `Separator(spacing=large)`**; within a section, when
  a line needs air, `Separator(spacing=small, visible=False)`; between the
  headed groups of a hub, `Separator(visible=True, spacing=large)`: the
  drawn line says a new kind of thing starts, and the large spacing keeps it
  from crowding the headings either side (§ Controls). This replaces
  the zero-width-space `SECTION_SPACER` and `apply_section_spacing` entirely;
  neither applies to a layout, and the spacing sweep in
  `test_embed_style_contract.py` must not demand it of one.
- **Section headings inside the card are `### Heading` lines**, one Text
  Display per section (heading + its body), so a section stays one component
  and the budget stays legible.
- **Footer jobs move to a `-# subtext` line** at the bottom of the card, under the
  same one-job rule (→ § Footers: next step, attribution, freshness, game
  signature, pagination, privacy notice). Subtext is markdown, so the footer's
  plain-text caveat is gone: custom emoji and `**bold**` render there. The
  `footer_emoji()` workaround is unnecessary on a layout.
- **Record cards keep a timestamp** as a `<t:…:f>` in the subtext line;
  `embed.timestamp` has no equivalent (→ § Timestamps for which cards).
- **A clock time is a `<t:…:t>`, never "14:00"**: server time reads wrong in
  every other zone (→ embed_style_guide.md § Timestamps).
- **A DM says which server it is from** with the guild icon as the
  `Thumbnail` beside its first section and the server name in the `-#`
  subtext, the two attribution jobs `brand_dm_embed` did with the author
  slot and footer (→ § DM branding). `send_branded_dm` grows a `layout=`
  path that stamps both; the accent it also applied is gone with the rest.

## Inline facts (there are no inline fields)

Components V2 has no side-by-side columns and Discord has said it won't add
them for buttons (`discord-api-docs` discussion #7516, open, no plan). The
three-across `inline=True` triple (Host / Hot Seat / Mode; Yes / No / Abstain)
becomes **one line of ` · `-joined facts**: `Host: {name} · Hot Seat:
{name} · Mode: Classic`. Single-spaced middot, bold values, the label first.
This also settles the embed guide's open "separator strays in body text"
question for layouts: **` · ` in a facts line, ` — ` in a heading, ` • ` in
subtext**, mirroring the title/footer rule. Anything list-like stays a list.

## Controls: which one, and when

Components V2 changed how a card *reads*, not what a member can *do*. There
are five verbs (press a button, pick from a dropdown, fill a form (modal),
run a command, react) and four replies: a public message, an ephemeral one,
an edit of the card in place, a DM. The modal is the part that grew: in
discord.py 2.7 it holds text inputs, any of the five dropdown kinds, a
**radio group** (2–10 options), a **checkbox group** (≤10), a single checkbox
and a **file upload** (1–10 files), up to five of them, submitted at once. So
the rules (Billy, 2026-09-20):

- **A dropdown for a mutually exclusive choice.** Three or more options that
  can't be combined (a DM mode, a voice-room access mode, a game variant)
  are one `Select`: emoji + label + a one-line description per option, the
  current option pre-selected, and the card text stating the current value
  at rest ("You're on **🟡 Ask** — people must request first") because the
  descriptions are hidden until it opens. It costs 2 components where three
  button rows cost 9. Today's button dials (DM mode, Voice Control's access
  dial) become dropdowns as their features convert; the "collapse toggles
  into one dial" rule in `embed_style_guide.md` § Buttons, modals & selects
  now means a select.
- **A button for an action** (Accept, Remove, Buy, Open Form, Done) and for a
  plain on/off. Never for one of several exclusive values.
- **A row with a button beside it for a list where each item has its own
  action**: Remove beside each connection, Buy beside each perk (§ Anatomy).
  One button per row; the row text carries the meaning, the label is one or
  two words.
- **A hub's rows are grouped by the kind of thing they do, and the groups are
  headed and divided.** When some rows reach out to *another member* and
  others configure *yourself*, each group gets its own `### heading` and a
  visible `Separator` sits between the groups; rows within one group share a
  verb. Billy's call on 2026-09-20, reading the DM Requests panel's first
  draft: "can you put some sort of separation between requesting and setting
  your preference". A `Section` carries no weight of its own, so two of them
  stacked read as two items of one list however different the things they do
  are: the heading is what says a new kind of thing starts, and the divider
  is what says the last one ended. The divider takes `spacing=large`: the
  first draft used `small` to keep the card on one phone screen, and Billy,
  reading the posted gallery, asked for "a little more vertical separation"
  (2026-09-20): a line with small padding reads as crowded. A second line
  of the same kind sits **under the intro sentence, above the first group**
  (Billy, 2026-09-24: "put a horizontal line above ask someone"), so the
  title block reads as the card's own and not as the first group's. **The
  groups are even:** each is heading, row, then one `-#` blurb under the
  row, saying what that group leads to or holds (Billy, 2026-09-24, adding
  one under *Your Preference*: "so they're even"). A group with a blurb
  beside one without reads as unfinished. A blurb says what the member can
  *do* there; it never becomes a legend of what other people see (the
  "your status shows on your profile" line was dropped for that on
  2026-09-20). A hub with only one kind of row needs neither heading nor
  divider.
- **A hub's long explanation is a Help button beside its title**, never
  prose on the card (Billy, 2026-09-24: "a help button that gives the long
  explanation from the FAQ"). The title line is a `Section` with a grey
  **Help** accessory, and the press answers ephemerally with the feature's
  FAQ doc **read from the `docs` table at click time**, so it can't drift
  from the one admins edit and post in the dashboard's Docs panel. Copying
  the doc's body into a builder would guarantee that drift. The doc's own
  leading `#` heading becomes the card's glyph-led `##` title, because a
  bigger heading under the card's title reads as a second card. The card
  sends with `AllowedMentions.none()`, since an admin-written doc may hold
  a role mention. The doc is found by its key, so the spec says which key
  and that renaming it disconnects the button. **Build it with
  `docs/help_card.build_doc_help_layout`** and read the body with its
  `load_doc_body`, which reuses the docs renderer's splitters, so `---`
  breaks, images, the cut (at a block, never inside a code fence, with a
  `-#` note) and the no-doc line all behave as they do everywhere else.
  The feature passes only its glyph, a fallback title and its no-doc line.
  DM Permissions is the first caller; the Perk Shop (doc key `shop`) the
  second. **A hub that already had a how-to keeps it as the no-doc line**:
  Guess Who (doc key `guess`) passes its built-in guide as `missing_text`,
  so Help on a server with no doc yet still explains the game instead of
  saying there is nothing to read (Ben, 2026-09-30).
- **A dropdown may be a menu.** A select whose options *open* things ("Settings
  ▾ → DM mode / Connections / Notifications", each pick answering with its own
  ephemeral panel) is fine when a hub has more than three entry points; the
  option description says what opens, and it never carries a value. On a
  shared sticky panel the member's pick stays showing until the message is
  edited, and re-picking the shown option sends nothing, so the pick
  schedules a repaint that puts the placeholder back (the casino's
  **Pick a table…**, via its debounced hub repaint).
- **Live panel for dials, form for profiles.** A choice that applies on its
  own and instantly (a mode) is a dropdown on an ephemeral panel that
  redraws in place, its status sentence now reading "Changed to …" (§ Words:
  the sentence is the answer, not a second `-#` line). Several fields set
  together (a voice profile's name + limit + flags; a perk's color + icon)
  are one modal opened from the panel's Edit button: radio for the exclusive
  part, checkboxes for flags, text for text, one Save. Never both for the
  same field.
- **A flow whose steps don't depend on each other is one form.** The DM
  request was pick-a-person → pick-a-type → reason modal → submit; it is now
  one modal (user picker, radio DM / Friend with a description each, optional
  reason, Submit). Validation moves to submit and answers with the ❌ line the
  stepped flow gave earlier, and the no-contact rule holds: the refusal is
  **whichever ordinary refusal that surface already gives**, chosen so the
  blocked party cannot tell it apart from one. DM Permissions picked the
  closed-DM refusal for it (`_precheck_dm_request` presents the pair as
  though the target's DMs were closed, and the requester never reaches a
  "sent" receipt at all) because a sent receipt for a request that was never
  delivered would have to be followed by a silence the requester could read.
  Pick the one that fits the surface, and say in the spec which it is. Keep a
  stepped card only where step 2 genuinely depends on step 1's answer.
- **Confirm destructive with a card, never a form:** a red button answers with
  an ephemeral "Yes, …" / Cancel; nothing destructive is one tap.
- **Admin config is a link button** to the dashboard section (CLAUDE.md:
  configuration lives on the web).

### The paradigms

Eleven compositions cover every card in the bot. A conversion names which
one it is building; a card that fits none is a design question, not a new
shape.

| # | Paradigm | Shape | Examples |
|---|---|---|---|
| 1 | **Hub panel** (sticky, public) | A `##` title with a grey **Help** beside it (the feature's FAQ doc, read live); one sentence on what this is; a line; then the entry points **grouped by the kind of thing they do**: a `###` heading per group, a row per entry point with its button beside it, one `-#` blurb under each group's row so the groups are even, and a visible `Separator` between groups. All buttons grey. No how-to on the card; the form explains itself and Help holds the long version. **DM Requests and the Perk Shop:** the hub archetype: the hub's art, title and grey **Help**, bare since 2026-09-29 (§ No container). The shop's entry points are its sections, and each row describes its section: a `###` caption over the section's `-#` blurb (the chooser's own line), with a grey **Browse** beside it. Stock names and prices live one tap away, because they change and prices differ by reader. Then a line and **🧾 Your Rentals → Manage**. A dashboard save that changes which sections are stocked repaints the panel. Its Help opens the guild's `shop` doc. | DM Requests panel, shop panel, whisper |
| 2 | **Settings panel** (ephemeral) | Current state in text; a dropdown per dial; per-item rows with an action; Done at the bottom; redraws in place, the state sentence answering the pick. | DM Settings, voice profile, perks |
| 3 | **Settings form** (modal) | Radio + checkboxes + text, one Save; opened from a panel's Edit. | voice profile edit, perk customization |
| 4 | **Request form** (modal) | Every independent step of a flow as one field; validation at submit. | DM request, bank pay, whisper |
| 5 | **Decision card** (DM or channel) | Facts, then 2–3 decision buttons in the bottom row; edits into a receipt with the buttons removed. The intake card is the long form: facts, a checklist block, up to five rows of step buttons, closes into the same receipt, framed by the lobby strips, with its done lines in subtext so the receipt is visibly one (2026-09-30). | consent DM, mod approvals, promotion review, intake card |
| 6 | **List with per-row actions** | Section + button rows, paged at ten, arrows in the bottom row. Rows are *items*; a checklist's steps are lines of one list with its buttons at the foot (paradigm 5: the intake card, since 2026-09-30). | shop, connections, bounties, auctions |
| 7 | **Receipt / notice** | Bare text: heading, facts line, `-#` subtext. No controls. | roughly half of all embeds today |
| 8 | **Live board** (sticky, event-edited) | Text + optional gallery; stable ids on the parts that change. **The economy panel** is a live board in the hub's frame: the hub's art, title and grey **Help** of paradigm 1, then its `###` sections between lines, then a **📖 How it works** block: the guide's four one-line gists over a **Learn more about…** menu dropdown (paradigm 11) that opens one topic on an ephemeral card, then the member's buttons in **one row at the foot**: the board is what the channel is for, so the entry points follow it rather than leading. The How it works block is the one how-to a hub card carries, against paradigm 1's "no how-to on the card": Billy's call on 2026-09-28 ("it's too much at once. Let's put just the headlines in the panel"); its Help opens the same guide at its overview rather than a docs-table FAQ, because the guide quotes the guild's live rates. **The casino hub** is the same shape with its ten tables in a **Pick a table…** menu dropdown (paradigm 11) whose last option is 📊 My Stats, above a grey 🎁 Daily Comp button: ten table buttons would outgrow the card, and each option carries its table's one-line description. | game boards, now-playing, AMA bar, mod stats, economy panel, casino hub, the todo and bug boards (**bare**: staff worklists wear no strip, footer or Help, per Ben, 2026-09-30; `docs/specs/todo_spec.md` § The cards) |
| 9 | **Game turn card** | Prompt, then answer buttons (≤5), a dropdown (≤25) or a modal (free text); edits into the reveal. | every game cog |
| 10 | **Confirm destructive** | Red button → ephemeral "Yes, …" / Cancel. | resets, revokes, end game |
| 11 | **Menu dropdown** | A select whose options open ephemeral panels. | a hub with more than three entry points; the casino's tables |

**Converting a hub panel (paradigm 1): the checklist.** The hub's rules
are spread across four sections; a conversion does all of these in one
commit, and each has a test to copy:

1. **No box**: the builder returns its items bare, like any other card
   (§ No container; `test_layout_no_container`'s allow-list is empty).
2. **The art**: splice `hub_banner(banner_url)` first and
   `hub_footer(footer_url)` last, and add the builder to
   `HUB_BANNER_BUILDERS` in `tests/contracts/test_layout_hub_banner.py` (§ Media).
   Nothing on an ephemeral card the hub opens.
3. **The title in the banner**: a `<FEATURE>_PANEL_TITLE` constant (a
   function of the guild's settings when the title is one, as the economy panel's
   names the currency: `economy_panel_title(settings)`, with no emoji in it,
   since a banner font can't draw one), a `title_in_banner` keyword, and `hub_banner(banner_url, title=...)` with
   the `##` line dropped when it is set *and* a banner is present.
   `test_every_hub_can_carry_its_title_in_the_banner` fails a builder on the
   list without it: a hub that ignored the flag would show its title twice
   once an admin ticked the box.
4. **Read the art off the event loop**: the cog reads
   `resolve_panel_art_conn` inside its DB block, then passes the result
   through `panels/title_service.with_title(art, TITLE)` in the same worker
   thread: the first build of a new title draws every frame of the banner.
5. **Join the panel refresher**: a sticky passes
   `StickyPanel(refresh_on_update=True)`; a posted-once hub gives its
   `PanelSpec` a `render` method. Either way `panels/cog.py`
   redraws it in place on every `panel_art_change` and once per restart, so
   an upload, a switch on the dashboard or a new layout shows at once. No cog
   keeps its own art listener (docs/plans/panel-refresh.md, gitignored; recover
   it with `git show 678a16fa:docs/plans/panel-refresh.md`).

Things a layout cannot do, so nobody spends an afternoon trying: two buttons
beside one row; a dropdown beside a row (a Section's accessory is a button or
a thumbnail); a button with a description (label ≤80 characters; the row
text carries the meaning); a modal opening another modal; inline fields.

## Words

The pilot's cards went through a frontend-design pass on 2026-09-20 (Billy:
"could we use the frontend design skill on these cards?"), and what it found
was copy, not layout: the same panel under three names, the same connection
kind under two, a card that told Ada "**Ada** ↔ **Bo**", a `-#` line
restating the sentence above it. Each finding is a rule now; Verifier D in
the migration plan reads every wave against them, and
`tests/dm/test_dm_perms_layouts.py` sweeps the pilot's cards for the first two.

- **One name per thing.** A panel has one name, and it is the name on the
  button that opens it, in its own heading, and in every card that points
  back to it: "DM Settings" on the hub's button, `## 📬 DM Settings` on the
  panel, "you can undo this later in DM Settings" on the receipt. The hub is
  "the DM Requests panel". A connection's kind is "DM" / "Friend" on every
  card; the long form ("Direct Message", "Friend Request") lives in the
  request form's radio descriptions, where it is explaining a choice, and
  on the mod feed's audit line, which a third party reads.
  The requester's words are a "reason"; the decliner's are a "note".
- **A card addressed to one member names the other.** "You and **Bo** can
  now DM each other", never "**Ada** ↔ **Bo**" read by Ada. A builder that
  renders for either party takes a `viewer_id` and is called once per
  recipient; a card both parties read (the edited source message in a
  channel) takes `viewer_id=None` and names both.
- **A removal is symmetric.** The card says the two are no longer connected
  and never who pressed Remove: the other party can work it out, and a card
  that says it reads as an accusation.
- **One glyph per outcome.** ✅ Connected · ❌ Request Declined, Nothing to
  Remove, Server Unavailable · 🚫 Connection Removed · ⌛ Request Expired,
  No Longer Active · ⚠️ a confirm · 📨 a request in flight · 📬 the
  feature's own panels · 📜 the audit notice. A heading with any other glyph
  is inventing a new kind of outcome. ℹ️ is not in the set on purpose.
  Billy proposed it for "Nothing to Remove" and "Server Unavailable", and
  the ❌ ruling in `embed_style_guide.md` (2026-07-21) already covers every
  "that didn't happen" line, with `test_embed_style_contract.py` enforcing
  it; a second glyph for a softer no would be the overlap that ruling ended.
- **The status sentence is the answer.** A settings panel that redraws after
  a pick says "Changed to 🟢 **Open** — …" in the sentence it already
  carries; it does not add a `-#` line saying the same thing. Subtext under a
  settings panel is for what the sentence cannot say: which page this is,
  that a removal happened.
- **Quote a member's words.** A reason or a note is a `>` block straight
  under the sentence it answers, every line prefixed, escaped as the
  untrusted text it is. A bold "**Their reason**" label over it is one more
  thing to read. A label is needed only where the words could be mistaken
  for the card's own voice: "**Note from Bo**" under "Bo declined your
  request", because unlabelled they read as *why*.
- **Count down, don't state.** "Expires <t:…:R>" reads "in 23 hours"
  whenever it is read; "expires in 24 hours" was true once. The label form
  survives only on the expired card, where the window is history.
- **A failure card names what happened and the next step.** "This request
  was already answered, expired, or cancelled — there's nothing left to do
  with it." "The server this request came from is no longer reachable, so
  it can't be answered." Not "no longer active" on its own.
- **Cancel is silent.** A confirm's Cancel deletes the ephemeral confirm
  (`defer()`, then `delete_original_response()`), leaving the panel behind
  it exactly as it was. The pilot's first cut answered with a "↩️ Nothing
  Changed" card, a receipt for a non-event.
- **The audit line rides on the receipts that add a log row** (Request
  Sent, Connected, Connection Removed), not on a settings panel that would
  repeat it on every press, and not on a card that logs nothing.
- **Row copy is friendly, not terse.** A hub row is a sentence in the
  member's own terms, not an instruction: "Ask to DM someone or add them as
  a friend." and "Choose who can message you directly." replaced "Pick who,
  and say why." and "Choose who may reach you." (Billy, 2026-09-24:
  "pretty dry"). Friendly is not slang: the 2026-09-28 copy pass retired
  "slide into your DMs" for plain, complete sentences. It still fits the one-line row budget (§ Screen use on a
  phone), because warmth that wraps the row pushes its button down. The
  button label stays two plain words; the sentence beside it is where the
  voice goes.
- **A button says what it does or opens, in two words where two will do.**
  "DM Settings", "Open Form", "Yes, Remove", "Decline + Note" (the `+` is
  the modal it opens; "Decline With Reply" was a sentence). Renaming a label
  is free: a persistent view keys off `custom_id`, and the boot-time
  `place_or_refresh` repaints a sticky panel, so a label is never kept for
  fear of orphaning a posted panel.

## Tables, bars, and graphics

- **Monospace tables and progress bars are unchanged**: `pad_cell` cells in
  backticks, emoji and `<t:…>` outside the span, `▰▱` from `core/meters`
  always inside a code span (→ § Tables & column alignment, § Progress bars).
  Verified on a phone in the pilot; Discord's font stack is what those rules
  answer to, and V2 does not change it.
- **A bar may become a graphic, and the preferred form is application
  emoji**, not an image: an eleven-glyph meter set (0–10 filled) uploaded once
  through the `register_tile_emoji.py` pipeline, rendered as
  `{meter_emoji(current, target)} {current:,}/{target:,}`. No attachment, no
  re-upload on edit, monospaced by construction, works in every guild, may be
  animated. A rendered image bar is the fallback for a one-off card that
  already carries an image.
- **Real content renders go in a `MediaGallery`** (quote cards, Guess puzzles,
  the mod-stats chart), the `set_image` equivalent, with a `description`
  (alt text, ≤1024 chars) saying what the picture shows. **Never
  decoration**: the embed guide's rule stands. A `Thumbnail` is a Section
  accessory only.
- **One exception: the hub banner.** A channel-resident hub panel (the
  sticky poster a channel keeps at its foot) may open on the guild's own
  banner art, set per guild on the dashboard's Branding page and absent until
  an admin uploads one. Billy, 2026-09-26, on the poppy banner: "let's not
  use it on the ephemeral messages, just the main panels that live in the
  channel". A hub brands the place; an ephemeral card is one member's
  working surface and stays plain. It is built only by
  `core.layout.hub_banner(url)` (a `MediaGallery` of one, or nothing), and
  `tests/contracts/test_layout_hub_banner.py` pins the builders that may call it; a
  newly converted hub adds its builder there. The banner is a **hosted
  URL**, never an `attachment://`: a sticky reposts on channel activity and
  an attachment would re-upload on every send and edit (below). Discord
  shows a single gallery image whole and scaled to the card's width, never
  cropped, so the art is authored wide (about 3:1).
  **Transparency: clear only what lies outside the art's frame.** The card
  sits on the member's theme, dark or light, so a transparent pixel shows
  either. Keying the black *inside* dark-painted art hollows it out on the
  light theme: the poppy banner's centers went white and its leaves
  ghostly, because its shadows were painted as black, while clearing just
  the corners outside its gold frame held up on both (Billy's pick,
  2026-09-26). A banner meant to float on the theme is generated on a
  transparent background, or on a flat color the art never uses, not
  keyed out of black afterwards. Check any transparent banner over both
  themes before uploading it; transparency also costs size (the poppy
  twinkle went from 0.5 MB to 1.4 MB), inside the 2 MB budget below.
  Check the cleared corners are *fully* clear, too: keying from black leaves
  near-black noise a few percent opaque, invisible on dark and a faint box on
  light (the first poppy upload carried one, alpha 9–18 of 255; zeroing
  everything under ~10% fixed it).
  **Square the inner corners.** The banner's bottom corners and the
  footer's top corners are square, so the pair frames the text between
  them like a mount (Billy, 2026-09-26: "remove the radius on the bottom of
  the header and the top of the footer"). Discord's own rounding only clips
  a gallery image's *outer* corners, which on transparent-cornered art
  touch nothing; a rounded inner corner is painted into the art's frame,
  so it is an art fix (extend the frame's straight edges into the corner),
  never a code one. The outer corners keep their curve.
  **The footer is the banner's partner** (Billy, 2026-09-26: the art does
  the framing, not a colored bar): a thin strip,
  about 7.5:1 (1200 × 160, the lobby header's height, below), that closes
  the card below its last line. It is built only by
  `core.layout.hub_footer(url)`, uploaded in the same **Panel Art** card,
  held to the same callers and the same transparency rule. A hub posted
  into a Discord-NSFW channel shows its banner and footer unspoilered:
  they are the server's own decoration, outside the spoiler rule below.
  Keep it quieter
  than the banner: Billy turned down a centered poppy as "a little too much"
  and took leaves and bokeh dots in the banner's frame.
  **Game lobbies get a lobby header** (Billy, 2026-09-27: "give the game
  lobby the header and footer treatment from panels … a small header … same
  height as the footer with a simpler flower accent"). A lobby is a public
  card in a games channel, not a member's working surface, so it takes this
  exception too, but not the hub banner, which would push the Join row off
  a phone screen. It has its own **Lobby Header** slot in Panel Art: a strip
  1200 × 160, **the footer's height** (Ben: "make the small
  header banner the same height as the footer"), with the name and its leaves
  scaled by 160/108 so the word keeps its share of the strip. One
  constant, `make_panel_art.py`'s `STRIP_H`, sets both. The capitals sit
  midway between the frame's inside and the faded bottom edge. Its *top*
  corners round and its bottom open (see the fade below). The lobby closes with the hubs' footer.
  **Art shorter than the geometry draws a smaller name** (bug #274):
  every title size is a fraction of the art's *width*, so a 116 px name
  drawn onto a 108 px strip would run its capitals into the frame's top line.
  `panel_title_service._height_scale`
  shrinks the type, its floor, the rim, the leaves and their gap by the
  share of the geometry's height the upload has (a 108 strip gets a
  78 px name) and leaves taller art alone. The hub banner
  opts out (its geometry records no height): a banner is any wide picture
  with a clear bottom third, and a 3:1 upload keeps its full 121 px title.
  **The game's name goes between two leaves** (Billy, 2026-09-27: "Can we
  put the title between two leaves?"): the bot draws each game's name
  across the strip's clear center (116 px type, held to 616 px) and a leaf sprig
  either side of it, **a fixed 47 px from the words** so a short name and a
  long one are framed alike (Ben, 2026-09-29); the upload itself carries
  no leaves, and the sprig is `assets/panel_art/lobby_sprig.png`, cut from
  the poppy art by `make_panel_art.py sprig` and faded with the strip's
  open bottom. The name is in the hub title's petal colors but the lighter
  **Thught Regular** (Ben, 2026-09-28: "use the regular variant for the
  small headers"; its thinner strokes take a 3.7 px rim, not 4), always,
  with no switch, since
  one upload serves every game (`panel_title_service.with_lobby_title`,
  geometry `LOBBY`: 116 px type, its capitals midway between the frame's
  inside and the faded edge, fitted to the clear center and cut below
  58 px). With the name drawn the lobby drops its `##`
  heading and the header's alt text carries the name; a failed drawing
  keeps the heading. Built only by
  `core.layout.lobby_art(header, footer)`, which draws **nothing unless a
  header is set**: the footer alone never reaches a lobby, so a guild with
  hub art sees no change until it uploads a header. It is pinned to its
  builders by `LOBBY_ART_BUILDERS` in `tests/contracts/test_layout_hub_banner.py`.
  Lobbies wearing it: Clapback, Compliment, MFK, Story and, as the
  party games convert, Mt. Rushmore Draft,
  Most Likely To, LegitLibs, Name Your Price, Hot Takes, Fantasies &
  Dealbreakers (whose one tracked card is its panel) and Two Truths and a
  Lie, plus
  the duels layer's shared group lobby (`duels/layouts.build_lobby_layout`,
  titled with the mini-game's `GAME_DISPLAY_NAME`). A builder whose view
  owns the button row takes it as `controls`, so the footer still lands
  below the buttons.
  **A PvP game wears it on every card, not just the lobby** (Billy,
  2026-09-27: "make the pvp games always carry the headers … the whole
  thing lives in the card"). The six duel/lobby games are one message from
  challenge or lobby to result: challenge, lobby, live board, every
  terminal notice and the result all carry the header and footer, drawn by
  `duels/layouts.card_art` through `duels/views.FramedCard`'s one shared
  `render()` (header, items, buttons, footer). The result **replaces the
  board in place** rather than posting under a greyed-out copy of it, and
  being an edit it notifies nobody, so it names the players (🏆 / 💀) and
  pings neither. A one-on-one challenge keeps its target's ping as the
  first line under the header. Spec: `dk_pvp_games_suite_spec.md`
  § One card.
  **The intake card wears it with the member's name** (Ben, 2026-09-28:
  "use the small game headers, make the title the person's name"). Not a
  game, but a channel-resident noticeboard card, never ephemeral. The one
  difference is where the drawing lives: a game's titled strip is cached in
  the public art directory, but a member's name must not be (that
  directory outlives an erasure), so `panels/title_service
  .titled_strip_bytes` draws in memory and the card **attaches** it
  (`attachment://intake-header.webp`), re-attaching on every edit.
  `drawable_title` strips what Inter can't draw (emoji, CJK); a name with
  nothing left keeps the `##` heading and no art. It takes **both strips**
  (Ben, 2026-09-30: "use the small header and footer"), the footer under
  the buttons like a lobby's: in a channel of a dozen open cards the frame
  is what makes each one a discrete object (with the header alone the cards run into each other). Under the drawn
  name the event is a `###` line ("🚪 Just joined"), swapped for the
  outcome once the card closes; a `##` under the strip would say the name
  twice, and a `**bold**` kicker read as one more weight in a small block.
  Its done and skipped lines drop to `-#` subtext (the pending ones keep
  the body size); it is the one card whose lines change size, because it is a
  checklist worked in place, not a list read once. Spec: `intake_spec.md`
  § Cards.
  The one read is `games/utils/lobby_art.read_lobby_art(bot, channel, title)`,
  which returns the same art in an **age-gated channel**, unspoilered
  (Ben, 2026-09-28: "we can use design elements without need spoilers.
  Spoilers are for user submitted nudity"), so Risky Rolls (played only in an
  age-gated channel) wears it too.
  **The title can live in the banner** (Billy, 2026-09-26: "make the banner
  a little taller … a stylized H1", then "an open source Helvetica"). One
  upload serves every hub, so the bot draws each hub's own title into it,
  `panels/title_service`, behind the Branding page's **Title in the
  Banner** switch, off by default. The face is **Thught Black**
  (Syafbe Studio, a bought license; Ben, 2026-09-28, replacing Inter
  Display Bold; `assets/fonts/ThughtBlack.ttf`), 121 px on a 1200-wide banner, each letter
  colored like a poppy petal (yellow inside shading to the petal-rim
  orange over its outer 4 px, both at 95%) with a dark edge and glow,
  the whole title then blurred 0.7 px so its edges sit soft like the bokeh, centered with its cap height at 79% of
  the banner's height. Art meant for it is taller (1200 × 520 works) and
  **leaves its bottom third empty** for the words: at 121 px the letters
  start 70% of the way down ("fifth" was written for a smaller title and
  was short even then).
  **A title too wide is fitted, then cut.** A hub title may run to 90% of
  the banner's width, a lobby title to the 616 px between its leaves. A
  wider one is drawn at the largest size that fits, to the pixel ("Coins —
  The Bank" at 118, the casino's at 77), so a wider title is never drawn
  larger than a narrower one. One that would have to drop below the floor
  (72 px on the banner, 58 on the strip) is drawn at the floor and cut with
  an ellipsis (Ben, 2026-09-29: "put a text limit in and truncate"); the alt
  text still carries it whole. No game's name on the register is cut.
  Every frame of an
  animation is drawn (the quote cards' headings wear the same colors, through
  the same `petal_layer`; see `docs/specs/quote_renderer_spec.md` § Fonts), and each
  (banner, title, style) is drawn once and
  cached under `panel-banners/titled/`; bump `TITLE_STYLE_VERSION` when the
  drawing changes. With the title drawn the builder **drops its `##`
  heading** (it would say the name twice) and the banner's alt text becomes
  the title, so a screen reader still hears it; anything that stops the
  drawing (no banner, an unreadable file) keeps the text heading, never a
  panel with no name.
  **A second, narrower exception: Flash Themes' live announcement.** It is not a hub panel (`EconomyCog` keeps
  no sticky panel in the theme channel) but it is a public post that pins
  itself there for the theme's whole window, reading as a hub of one, so it
  opens on the guild's hub banner and closes on the hub footer the same way
  a hub does. Unlike the title-in-banner switch above, the caption "Today's
  Theme" is drawn **unconditionally**, not behind a per-guild toggle: it is
  a fixed decoration, not a hub swapping its own name in, and the
  member-supplied theme title stays text-only, in the card's own
  `## 🎨 {title}` heading below the banner, never drawn into the image,
  so it never needs escaping through Pillow. With no banner configured the
  card is a plain `## 🎨 {title}` card. The review
  card, the DM receipts and every ephemeral reply stay plain; only the
  public post carries it. `tests/contracts/test_layout_hub_banner.py`'s
  `HUB_BANNER_EXCEPTIONS` pins the one builder allowed to call
  `hub_banner`/`hub_footer` from outside a hub panel, kept separate from
  `HUB_BANNER_BUILDERS` so it is exempt from the title-in-banner check
  above, which does not apply to it.
  **A channel hub with no tall banner of its own wears the small lobby
  strip instead** (a general rule): the Bounty Board's hub, which has no
  tall banner, reuses the game-lobby machinery above rather than opening a
  second door beside `hub_banner`/`hub_footer`:
  `core.layout.lobby_art(header, footer, title=...)` and
  `panels/title_service.with_lobby_title`, drawing the hub's title between
  the leaves in place of its `## ` heading. It is pinned in
  `LOBBY_ART_BUILDERS`, not `HUB_BANNER_BUILDERS`, so it is exempt from
  the title-in-banner switch above (the lobby strip has no such switch;
  the title draws whenever art is configured, same as a game lobby) and
  from the title-in-banner test. A hub already on the tall banner keeps
  it; this rule applies to a hub that has no tall banner. Ships dark: no header configured, no gallery, no drawn
  title; the plain `##` heading stays. Guess Who's
  channel prompt is the second hub on it
  (`games/guess/layouts.build_prompt_layout`), and the Whisper launcher the
  third (`whisper/layouts.build_launcher_layout`).
  **Pen Pals is the exception**: its signup panel
  wears the **tall hub banner** with its name drawn
  in (Ben's call, `pen_pals/layouts.build_panel_layout`, in
  `HUB_BANNER_BUILDERS`). Its **question cards** (posted into a
  pair's private room, neither a lobby nor a hub) wear the **small
  strip** (`build_question_layout`, in `LOBBY_ART_BUILDERS`). The room's
  intro and the DMs stay plain.
  **Every other public card wears the thin card strips.** A card the
  bot posts into a channel that is neither a hub nor a lobby (the economy
  and casino posts, the staff-channel review and audit cards) opens on a
  **Card Header** and closes on a **Card Footer**,
  two more Panel Art slots: 1200 × 80, half the lobby strip, gold frame and
  bokeh only. **Nothing is ever drawn onto them**, so they need no
  `panels/title_service` pass and the card keeps its own `##` heading. Built
  only by `core.layout.card_strips(art)`, which draws **nothing unless a card
  header is set** (the footer alone never shows, as with the lobbies), and
  pinned to its builders by `CARD_ART_BUILDERS` in
  `tests/contracts/test_layout_hub_banner.py`. The read is
  `core/card_art.read_card_art(ctx, guild)`, or `resolve_panel_art_conn` on
  a connection the sender already holds; the art is the same, unspoilered,
  in an age-gated channel. A builder also sent privately (a review card's
  todo-board picker, a /pay receipt) takes `art=None` on that path, so an
  ephemeral card stays plain. High-volume lines (the ledger register, its
  sign-off notices) stay bare on purpose: a strip on every line would bury
  the channel. Ships dark: no card header, no gallery.

## Media: generated images and animation

Animated media is now cheap to *show*, so the rules are about cost to *make*
and *send*.

- **Favor WebP.** Generated media, static or animated, is written as WebP
  (Pillow: `save(..., save_all=True)` for animation). Real alpha, no
  256-color palette, smaller than GIF at the same size; Discord's reference
  lists WEBP beside GIF as a supported animated format. Keep GIF only for an
  asset members are expected to paste elsewhere. The committed
  `assets/logoanimated.gif` (512 px, 240 frames, 8.4 MB) is a source, not a
  deliverable; a card-sized cut is ≤ 256 px, ≤ 80 frames, ≤ 2 MB.
- **Every send *and every edit* re-uploads the file.** A sticky panel that
  redraws hourly uploads hourly. Hold bytes, not a `discord.File`, and build a
  fresh `File` per request; `PanelImage` already does exactly this and is
  the model. Budget a panel's media accordingly; an animated header on a
  panel that edits every few minutes is the wrong place for one.
- **Pre-render or cache; never draw on the interaction path.** The quote
  renderer runs in a thread for this reason. Per-member-per-interaction
  imagery (an animated wallet) is rendered from a cached base with the
  variable part composited on, or not animated.
- **Fonts are open-licensed or bought, and the license travels with them.** Anything
  the bot draws text with (the quote renderer, the banner title) uses a face
  under the SIL Open Font License or an equally permissive one, committed to
  `assets/fonts/` and listed in its `README.md` with its license. A font
  that is free only for personal use (the Stockholm script tried in the
  title mockups was one) is never committed, not even as a fallback. The
  one exception is a face with a **bought** license (Thught Black and
  Regular: the panel titles, quote cards and the Poppy word cloud; MyFonts 2026-09-28,
  desktop license): its README row names where the license
  was bought, and the file stays in the private repo; only images drawn
  with it are published, never the font itself.
  Instancing a variable font (fontTools) keeps its OFL name records; say in
  the README which instance it is.
- **The panel art is generated, not hand-drawn:** `scripts/make_panel_art.py`
  builds the Branding → Panel Art files (the hub banner, the footer,
  the lobby header and the two card strips (`card`, `CARD_H`)) with the bokeh softness and loop length as dials at
  its top (Ben, 2026-09-28: σ 3 px dots, 34 frames at 90 ms, a 3 s loop).
  The footer, lobby header and card strips are drawn from scratch; the banner is Billy's
  painting, so the script re-twinkles an existing banner file rather than
  redrawing it. Keep a crisp original: softening a soft banner blurs it
  twice. The uploads themselves live in the git-ignored art directory.
  **Each strip is open on the side that meets the card's text** (Ben,
  2026-09-28: "fade into the message content instead of looking like a
  cheeseburger"): the banner, lobby header and card header have no gold
  line along the bottom, the footers none along the top, and on that side the black *and*
  the side strokes fade to transparent over the last 10% of the height
  (`FADE`). The banner is Billy's art with its bottom line baked in, so the
  script lifts it by repeating the last clean row down to the edge, so the
  banner keeps its 520 px, and the drawn title with it.
  **The frame's sides are the picture's sides** (Ben, 2026-09-29: "the
  text should move in the width of the yellow line"). Discord draws a
  gallery image exactly as wide as the text column under it, starts the
  text on the picture's left edge and ends a Section's button on its
  right, and offers no way to pad either. A frame drawn 10 px inside a
  1200 px picture therefore sits inside the text on both sides, and the
  card reads as copy spilling out of its frame. So the gold side strokes
  are drawn at x = 0 and x = 1199 (`SIDE_INSET = 0`; the closed side
  keeps `INSET`), and `test_the_frames_sides_are_the_pictures_sides`
  fails if they move back in. The banner's frame is baked into Billy's
  painting, so `to_edge` crops the transparent margin off each side and
  scales the picture back to 1200 wide, under 2% of stretch.
  `python scripts/make_panel_art.py edge <banner.webp>` does only that,
  to a banner that is already finished, keeping the file's own frame
  times, and refuses a picture with no clear margin to crop rather than
  write the same file back; `banner` does it as its last step. New art from anyone's hand follows the same rule: no margin
  outside the frame's sides.
- **Bot upload cap is 10 MB per message**, up to 10 files, and a
  `MediaGallery` holds up to 10 items.
- **`message.attachments` is empty on a V2 message.** Anything that inspects a
  bot message's attachments (the bump detector already walks component trees
  for text, `bump_tracker/detector_logic.component_texts`) must walk
  components for media too. That helper is the shared seam; promote it to
  `core/` when a second caller appears rather than writing a second walker.
- **Spoilers are per component**: `spoiler=True` on a `Thumbnail`, a
  `MediaGalleryItem` or a `File`. The NSFW rule is unchanged:
  member-submitted media into any Discord-NSFW channel is spoilered (a Guess
  reveal, a starboard repost), and the flag now goes on the media item, not
  on a whole-message attachment. **The bot's own design elements are not
  member media** and go unspoilered anywhere: hub banners and footers, lobby
  headers (Ben, 2026-09-28: "Spoilers are for user submitted nudity").

## The budget

- **40 components, nested ones counted; 4000 characters of Text Display.** A
  `Section` with an accessory is 3; an `ActionRow` is 1 plus its buttons; a
  `Separator`, `TextDisplay` or `MediaGallery` is 1.
- **A row-per-button list pages at ten rows.** Ten Sections (30) plus a
  heading, separators and a bottom row fill the message. The shop's seven
  perks reached 37. The store's `STORE_PAGE_SIZE = 10` is already right.
- Measure, don't estimate: `view.content_length()` for characters, and a
  `layout_budget(view)` helper (§ Code conventions) for the count, since
  discord.py keeps `_total_children` private. A builder test asserts both
  ceilings on the fullest card the feature can produce, the equivalent of
  today's `len(embed) <= 6000` checks.

## Screen use on a phone

The case for V2 is phone readability, and the pilot's phone comparison
favored it, so a layout is designed for a 400 px column first.

- One idea per Text Display; a paragraph is a line or two, not a wall.
- A `Section` row is **one line of text**; if it needs two, it is not a row
  with a button, it is a paragraph; the catalog row in § Controls is the
  one exception, and it holds each of its lines to the same number. The
  working number **beside a button accessory is ~30 columns** (an emoji
  counts two, `core.layout.text_columns`): measured on Billy's phone on
  2026-09-24, 32 characters of "A custom color for your personal role" fit
  beside "Set Color" before the wrap, and a label at the 10-column budget
  takes a little more. A line with **no accessory** (a bare Text Display,
  or a Section whose accessory is a thumbnail) keeps the older **~45**
  (the same screenshot ran 35 characters across the full width with room
  to spare). Both are counted the way the reader sees them (markdown
  syntax is not drawn, but a `<t:…:D>` is, as the written-out date it
  expands to), and the worst case to design for is the 32-character display
  name Discord allows. Three facts rarely fit. `shop_layouts.MAX_ROW_CHARS`
  carries the 30; `dm.perms.layouts.MAX_ROW_CHARS` still carries 45 for
  rows that *do* sit beside a button and is due the same measurement; its
  longest row is 42 characters.
- **Restraint is the feature**: the DEV writeup's whole argument is that
  embeds "look like automated marketing" because they fill space. A one-line
  answer is a bare Text Display, not a card.
- Test on a phone. Desktop and mobile lay a Section out differently.

## Code conventions

- **Builders stay pure and return children, not behavior.** A per-feature
  `embeds.py` builder becomes `build_*_layout(...) -> list[ui.Item]`:
  primitives and a `name_fn` in, a list of `TextDisplay` / `Section` /
  `Separator` / `MediaGallery` items out, with its buttons carrying a stable
  `custom_id` (persistent) or numeric `id` (transient) and **no callbacks**.
  The `LayoutView` subclass in the cog adds them in order and attaches
  behavior by looking items up (`find_item(id)`). This keeps the purity contract
  (`test_embed_builder_purity_contract.py`) meaningful and lets a builder
  test render the fullest card offline.
- **`core/layout.py`** is the one place layout mechanics live, and it is
  two exports: `layout_budget(view) -> (components, chars)` and
  `ExpiringLayoutView`, the `LayoutView` sibling of
  `core/utils.ExpiringView`, disabling through `walk_children()` because
  items nest. There is deliberately **no** `send_layout` / `edit_layout`
  wrapper (stage 1 landed it that way; the module docstring carries the
  same reasoning): the client is built with `allowed_mentions=none()`, so a
  wrapper has no mention default left to set, and the surfaces a card lands
  on (`channel.send`, `send_message`, `followup.send`, `edit_message`,
  `edit_original_response`, `message.edit`, `user.send`) differ too much
  for one to be honest. An edit converting an embed message still passes
  `content=None, embed=None, attachments=[]` itself; for a sticky panel
  `core.sticky.PanelContent` does that, and anywhere else it is the
  caller's line to write.
  `tests/contracts/test_view_timeout_contract.py` sweeps `discord.ui.View` subclasses;
  it must sweep `LayoutView` subclasses too before the first one ships.
- **`PanelContent` grows a `layout:` field** (a `LayoutView`), exclusive with
  `embed` and `content`, whose `send_kwargs` pass only `view` (+ files) and
  whose `edit_kwargs` null out `content`, `embed` and `attachments`
  explicitly, the same "every field explicit" reasoning its docstring already
  gives. Every sticky panel is gated behind this; nothing else about the
  sticky machinery changes.
- **Stable component `id`s on anything a later edit reads back.** Jail tickets
  fetch a live message, take `msg.embeds[0]`, change a field and edit it
  back. The V2 idiom is `LayoutView.from_message(msg)`
  then `view.find_item(id)`; a card built without ids cannot be edited that
  way; or, where the view is still in memory, rebuild it from state.
- **`message.content` is empty on a V2 message.** Anything that matches a bot
  message's text (`games/external/cog.py` re-serializes other bots' embeds; the
  bump detector reads component text) walks the tree. Other bots already post
  V2.
- Ephemeral, followup and modal usage is unchanged from `View`; a
  `LayoutView` on an ephemeral message with `timeout=None` is a leak like any
  other persistent-looking view (discord.py warns on it).

## What carries over unchanged (by pointer)

From `embed_style_guide.md`, applying to a layout exactly as written:
§ Titles, labels & casing · § Currency vocabulary · § Ledger rows · § Footers (the job list; the
slot is now subtext) · § Timestamps · § Tables & column alignment ·
§ Progress bars · § Empty states & pagination · § Errors, denials &
confirmations · § Voice & terminology · § Slash commands · § Buttons, modals &
selects · § Persistent views · § Expiring views (via `ExpiringLayoutView`) ·
§ Sticky panels · § Reach & privacy · § DM branding (`brand_dm_embed` needs a
layout twin before DMs convert) · § Mentions, pings & user-supplied text
(escaping) · § Naming members in embeds · § Pointing at things · § Dashboard
(JS) specifics.

**Superseded for a layout:** § Color in its entirety (a layout has no color),
§ Card anatomy's slot rules (title vs author,
masked-link placement, footer plain-text caveat), § Fields & layout (inline
triples), § Section spacing (the spacer and `apply_section_spacing`), and the
1024-character field cap.

## Verifying a conversion

Tests and a diff review can't see a rendering, so every converted feature is
checked four ways before it merges (Billy, 2026-09-20):

- **Offline**: the builder tests render the fullest card the feature can
  produce and assert `layout_budget()` under 40/4000; the mention sweep, the
  no-`Container` sweep and the timeout sweep are green; the old embed
  builders and their tests are gone, not left beside the new ones.
- **Against this guide**: a reviewer names the paradigm each card is, and
  fails the card that fits none, keeps a Container, puts a value on buttons,
  names a member with `<@id>`, or wraps a Section row.
- **A sample gallery in `#🤖│bot-command-spam`**: the converted cards rendered
  with placeholder data and posted as the bot (a REST-only login with the
  prod token, the way `scripts/layout_samples.py` does it), one message
  per card state, each led by a `-# Sample — <feature>: <state>` line. That
  channel and no other, ever. Buttons and selects on a sample carry
  `sample:`-prefixed custom ids so the *running* bot, which still holds the
  old code, ignores a press instead of acting on it. **This matters most for
  a revision of a live card:** the running bot already answers that card's
  real ids, so an un-prefixed preview of the DM Requests panel once had a working Open Form
  that sent real requests, beside a Help that failed because the ids were
  new. `core.layout.sample_prefix_ids(view)` does the prefixing. The
  gallery is what Billy checks on his phone; nothing else stands in for
  that. A tweak he asks for from the gallery ("both buttons the same light
  grey") is re-posted as a fresh sample of just that card, not the whole
  gallery again.
- **A design pass**: a reviewer reads each card with the `frontend-design`
  skill's lens against § Words: one name per thing across the feature's
  cards, the other party named, one glyph per outcome, no `-#` restating
  the sentence above it, member text quoted rather than labelled,
  countdown timestamps, failure copy that names the next step, a silent
  Cancel, and reports each miss against the card and the rule. Every wave's
  overnight batch includes it.

## Adoption order (the shape of the plan, not the plan)

The migration's plan is `docs/plans/components_v2_migration.md` (gitignored;
recover it with `git show 678a16fa:docs/plans/components_v2_migration.md`). Its stages,
in dependency order:

1. **Mechanics first**: `core/layout.py`, the `PanelContent.layout` shape, and
   the test extensions above (mention-guard sinks, the no-`Container` sweep,
   timeout sweep over `LayoutView`, spacing sweep exempting layouts). Nothing
   member-visible changes.
2. **DM Permissions, the pilot**: a hub panel, a settings panel, a request
   form, a decision card, four receipts and a notice in one feature, so the
   mechanics meet every paradigm once. Ships alone; Billy looks at it live.
3. **Waves by area**, each its own branch and QA card: economy (shop, wallet,
   bank, perks, casino, auctions), games, mod & server tools, notices & DMs.
   Read-back-and-mutate sites (jail tickets, AMA, TTL, Hot Takes, starboard)
   convert inside their wave, each with its edit idiom rewritten around
   `find_item`.
4. **Retire the accent**: the Branding dial, `resolve_accent_color` and the
   accent-contract table go in one commit once no `Embed(` remains.

Each stage converts a *feature*, never a single card, so a feature's siblings
never mix shapes on one screen.

## Sources

- Discord, [Component Reference](https://docs.discord.com/developers/components/reference):
  limits, formats, the flag's restrictions, Text Display mention behavior.
- Discord, [Components overview](https://docs.discord.com/developers/components/overview):
  legacy embeds are not deprecated; the flag is per message.
- `discord-api-docs` [discussion #7516](https://github.com/discord/discord-api-docs/discussions/7516):
  no inline fields in V2; staff rationale on side-by-side buttons.
- discord.py 2.7.1 source (`ui/view.py`, `ui/container.py`, `ui/section.py`,
  `http.py`, `interactions.py`): flag auto-set, `accent_colour` type, the
  4000-char helper, the ephemeral flag merge, the edit-to-layout note.
- [Why we ditched Discord Embeds for Components V2](https://dev.to/yuaxx/why-we-ditched-discord-embeds-for-components-v2-and-built-a-minimalist-bot-fb6)
  (DEV, one developer's view): the aesthetic case: sidebar noise, screen
  use, "automated marketing"; restraint as the rule.
- [Components V2 — Umbra's Rantings](https://about.abstractumbra.dev/discord.py/2025/08/17/components-v2.html):
  discord.py patterns: `find_item` with numeric ids, attachments via
  `attachment://`, the mention warning.
- [Components V2 — discord.py Masterclass](https://fallendeity.github.io/discord.py-masterclass/components-v2/):
  `walk_children()` for disabling nested items, the edit-after-interaction
  pattern.
- [Discord Components V2 replace embeds with structured agent UI](https://joelclaw.com/cool/discord-components-v2-replace-embeds-with-structured-agent-ui):
  the 40-component ceiling as a chunking problem; containers for status.
- The pilot itself, posted 2026-09-20 (its script is deleted; git history keeps it).
