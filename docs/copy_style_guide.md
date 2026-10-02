# Copy style guide (Reference)

How the prose people read should sound: dashboard field hints and panel text,
the dashboard Help guide (`manual.html`), the member docs published from the
`docs` table, Discord channel topics, and the specs in `docs/`.

`embed_style_guide.md` owns the *shape* of bot cards and dashboard UI: casing,
colors, buttons, toasts, errors. This guide owns the *prose*. Where the two
overlap (voice, terminology) that guide's § Voice & terminology still applies,
and this one adds the rules below on top.

The cleanup pass that wrote this down started 2026-09-30 with the field hints.
Sections for the other surfaces are added as each one is cleaned.

## House style (Ben, 2026-08-26)

These are the habits that make copy read as machine-written. Leave them out of
everything a person reads.

- **No em dashes in sentences.** Use a full stop, a comma, a colon or
  parentheses. An en dash stays in numeric ranges ("0–99"). HTML counts too:
  `&mdash;` is an em dash. Bot card and embed titles keep the " — " separator
  `embed_style_guide.md` § Card anatomy sets ("Grant Audit — {label}"); the ban
  is for prose (Ben, 2026-10-01).
- **No dash-as-definition bullets.** Write `**Bank Channel:** where…` or a
  plain sentence, not `**Bank Channel** — where…`.
- **No one-word punch sentences.** "Simple." "That's it." "Done." add rhythm
  and nothing else.
- **No "not X, but Y" framing.** Say what the thing is. Mention what it isn't
  only when readers genuinely confuse the two, and then as a plain sentence.
- **No filler openers or sign-offs:** "It's worth noting", "Simply", "Whether
  you're X or Y", "seamless", "dive in", a closing line that restates the
  paragraph.
- **No triplets for rhythm.** A list of three is fine when there are three
  things.

## Names

Use the name the member sees today.

| Say | Not | Why |
|---|---|---|
| Member (the membership role) | Denizen | "Denizen" is only an internal grant label |
| DMs: Open / DMs: Ask / DMs: Closed | older boundary role names | the live role names |
| `🎟️│open-help-ticket` | "the ticket channel", older names | the live channel |
| `👋│about-us` is member bios | "about the server" | that is what the channel holds |
| Pricing (a dashboard page) | "the Sinks page" for a price | every price and rate lives on Pricing; Shop & Perks decides what is on sale |
| the dashboard page's nav label | its route id | admins navigate by label |

## Field hints (dashboard)

The grey line under a control. It exists so an admin can set the control
correctly without leaving the page.

- **At most 120 characters of rendered text**, and at most two short
  sentences. Tags are stripped and entities decoded before counting.
- **Say what the control does and what its empty or zero value means.** Use
  the picker's own words for the empty state: "(none)", "(disabled)".
- **Lead with a warning that changes the pick.** "Staff only." on a channel
  whose cards quote unreviewed text; a permission the bot needs in that
  channel.
- **Don't restate the label**, and don't explain what a checkbox is ("When
  checked, …").
- **Everything else goes to `manual.html`**: mechanics, edge cases, examples,
  history, how two settings interact. Put it in the section the panel's "?"
  link opens, so the detail is one click away and the Help assistant (which
  answers from the manual) still knows it.

`tests/web/test_field_hint_copy_style.py` enforces the em dash ban and the
length cap on every hint it can read statically: literal `field-hint` markup,
and `hint:` properties a shared helper renders. A hint built in code or passed
as a positional argument is out of its reach, so follow the rule there by
hand. Every hint met the rule by 2026-10-01, so the test's per-file allowance is
empty and any new over-length hint or em dash fails it.

## Help guide (`manual.html`)

- **Name a dashboard page by its nav path as it stands today**, such as
  Config &rarr; New Members &rarr; Welcome &amp; Leave, and link it with
  `/#/<panel-id>`. Several old paths ("Moved to Reports", "Config &rarr;
  Members &rarr; Welcome") outlived the pages they named.
- **Quote the bot exactly.** A quoted bot message or UI label keeps its own
  punctuation, em dashes included, so a reader can match it on screen. Fix the
  label in the code and the quote follows.
- **State a default as a default.** "10 minutes" or "two weeks" that is really
  a dial's starting value says so and names the dial.
- The house style was first applied 2026-09-30, section by section, skipping
  sections another open branch was editing at the time. Em dashes left in the
  manual's prose are backlog, not precedent.
