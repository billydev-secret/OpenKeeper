# Quote Renderer — spec

**Status: Reference** (matches current behavior).

`src/bot_modules/shared/quote_renderer.py` is a shared, stateless
text-to-image service. It turns a string + a background image into a branded
card: animated WebP for every card a cog posts, PNG for still previews, JPEG for
the legacy Guess card. It is **not** a cog; it has no commands and no DB access;
callers pass everything in and get bytes back. Several cogs depend on it, so
treat changes here as cross-cutting.

## Where it's used

| Caller | Function | Mode | Notes |
|---|---|---|---|
| `quotes/cog.py` **Quote** (message context menu: right-click a message → Apps → Quote) | `render_animated_quote_card` | avatar (`circle`/`square`) | Quotes a message; avatar on the left, quote on the right. Theme/font/border picked in an ephemeral style view. |
| `quotes/cog.py` `/banner` | `render_animated_quote_card` | `none` (banner) | Free-text banner in the drawn **banner box** (§ The banner box), set in Belleza. Always the box: a guild's uploaded border frames its quote cards, never its banners. |
| `games/photo/cog.py` launch | `render_animated_quote_card` | `none` | Photo-challenge prompt card, `theme=golden_meadow`. |
| `games/ffa/cog.py` launch | `render_animated_quote_card` | `none` | FFA round banner; theme chosen per game label. |
| `chat_revive.actions` revive post | `render_animated_quote_card` | `none` | `author_name="Ember"`, `theme=midnight`, guild icon background. Falls back to plain text. |
| `games/guess/cog.py` confession rounds | `render_quote` (legacy) | — | Solid-background JPEG spoiler card, `footer="Guess #N"`. |
| `web_server/routes/config.py` | `analyze_border_opening`, `guild_border_path`, `BorderStyle` | — | Dashboard upload/preview/delete of a guild's custom border. |

Every cog call runs under `asyncio.to_thread(...)`: rendering is CPU-bound
(PIL + numpy) and must stay off the event loop. The animated card takes about
12 s for a still picture (the bokeh; half of that is the WebP encoder) and
about 5 s for an animated one. Every interaction path defers before rendering.

## Two render paths

### `render_quote_card(...)` — pfp-as-background card

The primary path. Returns **PNG bytes** with transparent rounded corners.
Canvas **900** wide by default, **height taken from the frame's own aspect
ratio** (`card_size_for_border`); see **Canvas shape** below. With the bundled
Golden Poppy frame that lands at 900×507; a 3:2 frame renders 900×600.

#### Canvas shape

A frame is composited **full-bleed**, so a canvas of a different ratio stretches
the artwork. The card therefore yields to the frame: `card_size_for_border`
keeps the caller's `width` and derives `height` from the frame's native pixel
ratio, before any layout is measured. Every layout constant is a fraction of
`width`/`height`, so they follow the new canvas unchanged.

The alternatives were both worse for a frame: cropping-to-cover would cut the
frame's own edges off the card, and letterboxing would break the full bleed. If
the frame can't be read, the requested canvas is used as-is.

> This is why cards are not a fixed 900×500. The bundled Midnight Frame is
> 1536×1024 and per-guild uploads are 3:2, which a fixed canvas would squash
> 20% horizontally.

**Pipeline (`_build_background` → foreground → border):**
1. **Background:** the passed `avatar_bytes` is fit-covered to the canvas,
   Gaussian-blurred (radius 18), desaturated, blended with the theme's overlay
   color, and given a radial vignette. In avatar modes the face is pushed left
   (`offset_x`) so it doesn't sit under the text column; in `none` mode it stays
   centered. A gold corner gradient is layered toward the bottom-right.
2. **Text:** the quote is wrapped, emoji-measured, and drawn with a soft
   gaussian drop shadow. Long text is truncated to `QUOTE_MAX_CHARS` (280).
3. **Avatar** (non-`none` modes): the *unblurred* avatar is drawn on the left
   as a circle or rounded-square with a drop shadow and a double ring
   (cream outer + theme-gold inner). `author_name` is drawn as a small
   attribution below the quote (see **Attribution placement**).
4. **Rounded-corner alpha**, then the **border** is composited last.

**`pfp_shape`:**
- `"circle"` (default): circular avatar crop with the double ring.
- `"square"`: rounded-square crop (shows the whole avatar uncropped).
- `"none"`: no avatar box. The prompt is **center-aligned** across the card and
  `author_name` becomes a centered **header** above it. This is the "banner" /
  announcement look every caller other than the **Quote** context menu uses.
  (Avatar modes keep the body
  **left-aligned** in the right-hand column; only banner/announcement bodies are
  centered. Body lines are clamped left of the floral corner so centering never
  runs text under the flowers.)

### Attribution placement

In avatar modes the attribution (`— Name`) **left-aligns to the quote column** and
hangs one `gap` (~0.85 × attribution font size) below the quote's last line. The
quote and its attribution are then **centered as a single group**, so neither the
top nor the bottom of the card carries a dead band.

It is *not* centered under the avatar. That was the earlier behavior, and because
it centered on the disc (`pfp_cx − attr_w / 2`), any name wider than the disc drove
the anchor negative, collapsed it onto the left-margin floor, and ran the first
characters across the avatar's lower-left arc, while leaving the attribution
aligned to nothing. Anchoring to the text column fixes both at once: the column
already begins clear of the avatar's drawn footprint (double ring + drop shadow,
not the bare radius), so **no name length can reach back into the avatar**.

A name too wide for the column is **shrunk** to the largest size that fits (floor
≈ half the base attribution size) and only then **truncated with an ellipsis**, so
it never runs past the column's right edge.

Placement is factored into pure helpers so the geometry is unit-testable without
pixel-peeping a blurred card: `attribution_y()`, `attribution_block_h()`, and
`fit_attribution_text()`. Both layout paths share them: the bundled-border path
and the custom-frame (`mask_fit`) path, where a frame opening's bottom edge also
clamps the line upward. **y resolves before x**, because the column's horizontal
bounds are read at whatever row the line finally lands on; reading them at the
pre-clamp row would pick the wrong bound whenever the clamp moved the line, and in
an opening that narrows toward the bottom that means drawing over the frame.

Attribution height comes from the font's **line box** (`ascent + descent`), never a
string's ink bbox; an ink bbox varies with which letters the name contains ("Bob"
20px, "gg" 21px, a parenthesised name 30px), which would make the reserve and the
centring jitter per user. The line box is also identical whether or not pilmoji is
installed, so geometry doesn't fork on an optional dependency. (Width *does* go
through pilmoji when available, so an emoji contributes its drawn width rather than
the tofu box it replaces.) The custom-frame path reserves this same measured block
instead of a `1.7 ×` font-size estimate; the two are close at the default size
(49 vs 45px), so that change is about having one source of truth, not about size.

### Vertical bounding and the floral corner

**All three** layout paths (avatar, custom-frame, and banner) bound the text to a
vertical band and **cap the line count** to what fits, ellipsizing the last line.
Without the bound, past ~9 lines (~150 chars, well
inside `QUOTE_MAX_CHARS`) an avatar-path quote runs off both card edges, and the attribution,
which follows the quote rather than sitting at a fixed y, goes off the bottom with
it and vanishes from the PNG. The banner path has the same hazard (~130 chars
overflowed a 900×500 card; `QUOTE_MAX_CHARS` put the tail at y=725 on a 500px
canvas), which mattered more because it is the layout every caller other than the
**Quote** context menu uses.

Ellipsizing happens **after the block's final position is known**, and the closing
`…”` is re-fitted to the last line's own row via `ellipsize_line()`. Appending it
straight onto the wrapped line is wrong twice over: those two glyphs were never
counted by the wrapper, and capping shifts the block downward so the last line
lands on a *narrower* row than the one it was wrapped against. Since the capped
line is always the block's bottom line, exactly where the floral corner or a
narrowing frame opening leaves least room; an unfitted ellipsis was drawn over the
artwork the layout exists to avoid (measured at 76px past the bound).

The banner **header** is width-fitted with the same `fit_attribution_text()` used
for the attribution: it was drawn from a bare center offset with no bound, so a
39-char display name measured 1350px on a 900px card and was drawn from x=−225. For
the four callers that render in banner mode this header is the only place the
author's name appears, so an overflowing header loses the attribution outright.

On the slim border, the text column's nominal right edge (738 at 900w) runs into
the flower cluster (pasted from x≈586, y≈253), so a low, long line was drawn under
the petals. The usable right edge is therefore bounded **per row** by
`slim_flower_left_edge()`: the cluster's leftmost solidly-drawn pixel per row,
read from its alpha and cached per (frame, mtime, size). Reading real alpha rather
than the cluster's bounding box matters for typography: the cluster's upper rows
are a few sparse buds, and reserving its full width for them cost a wrapped line at
*every* quote length. `flower_limit()` takes the tightest bound across the rows a
line actually covers, since a line is a band and petals dipping into its lower rows
must bound it too. `slim_flower_rect()` is shared with `_composite_slim_border()`
so the layout's idea of where the flowers sit can't drift from where they're drawn.

The **banner** (`pfp_shape="none"`) layout uses the same per-row edge, because a hand-tuned linear ramp (0.95w tapering to 0.58w) over-reserves by up to 233px on a 900×500 card, so each line wraps shorter than
the last and short words are orphaned onto their own line. Frames *without* a
separable cluster (anything not `slim_frame`) keep that ramp: it's a crude but safe
stand-in for border art this can't measure, and dropping it could put text over
their artwork.

The **default frame is resolved at the top of `render_quote_card()`**, not just
before compositing. Filling it in late meant a caller passing no `border_style` (which is every caller except the **Quote** context menu) laid its text out as if the card were bare
and then had the poppy frame drawn over it, putting the last lines back under the
petals.

**Heights that feed layout always come from a font's line box, never a string's ink
bbox.** This applies to the attribution (above) *and* the banner header: the
header's height sets where the body starts, and the body's usable width narrows
toward the floral corner, so an ink-bbox header made the **quote itself re-wrap
based on which glyphs the author's name contained**: a name with parentheses
pushed the body down a line.

### `render_animated_quote_card(...)` — the posted card

Every card a cog posts moves. Same keyword arguments as `render_quote_card`;
returns **animated WebP bytes** (Discord plays animated WebP inline). Always on:
there is no dial, by an explicit product decision (bug-list row 247).

- **Animated avatar** (the source image has more than one frame: Discord
  serves animated avatars and server icons as GIFs): the avatar plays in its
  box *and* in the blurred background. Long animations are sampled evenly to at
  most `ANIM_MAX_FRAMES` (60); each kept frame carries the durations of the ones
  it skipped, so the loop keeps its speed.
- **Still picture** (the default look): the picture does not make the
  background. `_bokeh_backgrounds` paints soft golden out-of-focus lights
  (`BOKEH_LIGHTS`, 34) on a warm near-black base with a glow toward the right,
  and they blow gently rightward: `BOKEH_FRAMES` (120) × `BOKEH_FRAME_MS`
  (33 ms), 30 fps over a 4 s loop. Each light fades in, drifts and fades out
  on its own phase, so the loop has no seam; nearer lights are bigger, softer
  and faster. Lights are placed at sub-pixel positions: at 30 fps a light moves
  well under a pixel a frame, and whole-pixel steps read as a stutter. The
  arrangement is seeded from the card's text, so a card re-renders identically.
  - **Filter.** A lighter cut of the photo grade (`_bokeh_grade`): color at
    85% and the theme's wash at half its `overlay_alpha`. The full photo grade
    (Midnight: 35% color, a 50% navy wash) all but erased the gold. The grade
    is affine per pixel, so it is applied once to the base and to each light,
    not to every frame.
  - **Inside the frame.** On the slim Golden Poppy frame the lights stop at
    the gold line (`_slim_frame_line`, shared with the line's own drawing): the
    feathered edge sits under the line, and the margin between card edge and
    line is exactly the base in every frame. Full-art and uploaded frames cover
    their own edges, so there the lights run to the card edge.
  - **Quality 100 is load-bearing.** libwebp's lossy animation encoder treats
    pixels within a quality-derived tolerance (about ±3 at quality 80, ±1 even
    at 100) as unchanged and merges the frame. A slow soft background sits under
    that tolerance, so the first cut (an 84 ms drift of the blurred avatar at
    quality 80) kept 32 of 48 frames and stuttered. The bokeh at quality 100
    keeps all 120; a test pins that. Lossless keeps everything too but came out
    at 67 MB; 60 fps loses most frames even at 100. Animated avatars change
    enough per frame to stay at quality 80. Encoder effort `method=4`: 2 is
    ~70% larger, 6 took 108 s for 2% less.

**How the layers separate.** The renderer draws text straight onto the
background, so there is no foreground layer to reuse. `_compose_card` is run
twice with `fill=` black and then white (replacing the background *and* the
avatar); a pixel of opacity *a* and color *c* lands at *a·c* on black and
*a·c + (1−a)·255* on white, so `_matte` recovers *a* and *c* exactly (±1 from
rounding). That layer (text, shadows, gold gradient, avatar rings, frame) is
then composited over each frame's background + avatar, and the card's
rounded-corner alpha is re-applied. `tests/quotes/test_quote_animated.py` pins that the
rebuilt still matches the directly composed card.

Animated backgrounds are graded at quarter scale (`fast=True`) and scaled back
up. The result is indistinguishable under the 18 px blur and several times
cheaper per frame.

**Fallback.** If the animation raises, or encodes larger than `ANIM_MAX_BYTES`
(8 MiB, under the 10 MiB bot upload cap; a bokeh card runs 3.5–4.5 MB), the
still PNG is returned instead. Callers name the upload with
`card_filename(stem, card_bytes)`, which reads the bytes' magic, so a fallback
posts as `quote.png` and an animation as `quote.webp`, never a PNG name on
WebP bytes. The Quote preview embeds the card by that same name
(`attachment://<name>`).

`render_quote_card` stays a still PNG: the dashboard border previews and the
existing layout tests use it, and it is the animated card's fallback.

### The banner box

`/banner` wears its own frame, `BANNER_BOX`, in every guild. Ben's brief: "I want make the body text bigger
and an adjacent but more readable font. Id like to change the box to look like
the large header and footer but a box around it with the bokeh that we're using
in the titles today."

The box is the hub panel art's ingredients (`scripts/make_panel_art.py`) closed
on all four sides: a black field, the gold line shading from yellow at the
middle to orange at the sides, and small gold dots twinkling on their own
phases. It is **drawn, not loaded** (`BorderStyle.box`, with no artwork behind
`path`), and that is what lets the **card's height follow its text**. The old
card was a fixed shape, so a bigger face could only have meant fewer words
before the ellipsis; it cut a 150-character notice short. The geometry and the
pixels live in `shared/quote_box.py`; `_compose_box_banner` loads the faces
and draws the lettering, and returns what `_compose_card` returns, so the still
card, the matte and the animation all work as they do for every other card.

| Setting | Value at 900 wide | Why |
|---|---|---|
| Body face | Belleza | Flared strokes and round bowls, so it sits with Thught; plain letterforms, so it reads at phone size. Chosen over Marcellus (no kinship with the title) and Federo (its f drops below the baseline, and it has no ellipsis glyph) |
| Body size | 62 px, stepping down to 52 | Up from 47. A long notice shrinks before it loses a word; at the floor it is cut at a word with an ellipsis |
| Line height | 1.3 | |
| Side padding | 90 px | About 32 characters a line. 140 px made the same notice a line longer and 80 px taller |
| Titled card | 62 px above the title, 44 px title to body, 70 px below | Ben, on the first posted sample: "move the title up so there's the same amount of space between the text and top gold line". 70 px under the last baseline is 57 px of black under a line with descenders and 70 under one without; 62 above the title's capitals sits between the two. It first shipped at 80 |
| Untitled card | 72 px above, 66 px below | Unequal on purpose: descenders hang into the bottom padding, so equal numbers read top-heavy |
| Height | follows the text, never more than the width | Past square a card fills a phone screen |
| Frame | 6 px line, 46 px corners, at the picture's edge | The panel art's 8 and 62 at 1200 wide; `tests/quotes/test_quote_box.py` holds the two files to the same numbers |
| Dots | 16 per 100 000 px², 4 to 10 of them large | None within 6 px of a letter; out to 30 px they are small and dim, and never one of the large ones. One large dot goes to each corner |
| Loop | 34 frames × 90 ms | The panel art's |

Four things differ from every other card, all on purpose:

- **No quote marks.** The words are the moderator's own; marks made a notice
  read as a quotation of somebody else.
- **The body is centered and its wrap is balanced** (`quote_box.balanced`): the
  measure is narrowed while the line count holds, so a block never ends on an
  orphan.
- **No picture.** The box ignores `avatar_bytes` (the cog passes none and
  fetches none), and the animation is the dots' twinkle whether or not the
  guild's icon is animated. The theme therefore only colors the body text.
- **An uploaded border does not apply.** Before the box, `/banner` wore the
  guild's Quote Tool upload when it had one. With the box that upload would
  have swapped the whole look back to the old fixed card, at the old size,
  with nothing on the dashboard saying so, and the only way to the box would
  have been deleting the border every quote card wears. Ben, at ship: "Box
  always wins." `tests/quotes/test_quote_renderer_box.py` fails if the command
  consults the upload again.
- **Always a banner.** The box's layout has no room for an avatar, so a card
  in it is laid out as a banner whatever `pfp_shape` says, and the box is not
  in `BORDERS`, which is the Quote picker's list.

The title is drawn exactly as every other heading is (§ Fonts): Thught Black
in the petal colors, or flat gold when it carries an emoji.

`QUOTE_MAX_CHARS` still applies first, and still cuts at a character. A notice
inside the limit almost always fits whole; one made of unusually wide words can
still reach the floor and be cut. A single word wider than a line (a long
link) cannot wrap, so the body shrinks toward the floor for it and then cuts
that word with an ellipsis rather than let it run through the frame.
A title is one line: it shrinks to a third of its size for a long one, and past
that is cut at a word with an ellipsis, never drawn past the frame.

### `render_quote(...)` — legacy solid-bg card

Older, simpler path used only by `guess_cog`. Solid dark background, accent bars
top and bottom, centered text, optional footer. Returns **JPEG bytes**. No
themes, avatars, or borders. Kept for the confession-guess spoiler cards.

## Themes

`THEMES: dict[str, QuoteTheme]` controls overlay color/strength, desaturation,
text/attribution colors, and vignette darkness.

| Key | Look |
|---|---|
| `golden_meadow` | Warm amber-gold overlay, cream text. The brand default. |
| `midnight` | Deep blue overlay, near-white text. |
| `rose` | Pink-magenta overlay, blush text. |

The `golden_meadow` storage key and the slash-command theme choices are
global, so they cannot be renamed per guild; the choice label is the neutral
"Golden".

## Fonts

`FONT_STYLES: dict[str, Path]` maps a style key to a bundled TTF in
`assets/fonts/`. Body and header fonts are **independent**:

- **Body:** `font_style` param. The signature default is `"inter"`, which is
  what the prompt cards (chat revive, the FFA rounds, Photo Challenge) are
  set in; the Quote command and the quote trigger pass `DEFAULT_FONT_STYLE`
  (`"thught"`), and `/banner` passes `DEFAULT_BANNER_FONT_STYLE`
  (`"belleza"`).
- **Header** (`none` mode only): `header_font_style` param, default
  `DEFAULT_HEADER_FONT_STYLE` (`"thught_black"`), so every heading matches the
  panel titles whatever body font a mod picks. Thught Black carries its own
  weight and takes no stroke; the fallback is faux-bolded with a light stroke
  (`header_size // 40`) because no bold TTF is bundled for it. It is
  **colored like the panel titles** (Ben: "the same coloring and
  gradient as the panel banners"): petal yellow shading to the petal-rim
  orange, with the dark edge, glow and soft blur, drawn by the same
  `panel_title_service.petal_layer` the banners use and scaled to the header's
  size, so it does not follow the border or the theme. The rim is the
  heavy one (`HEAVY_RIM_PX`, 4 px) for Thught Black and the thin one
  (`THIN_RIM_PX`, 2.5 px) for the fallback face. The scale is against the
  101 px type the look was tuned on, not the banner's current 121 px type, so
  these headings do not change when the banner's does.
  **A header with an emoji in it is the exception** (`header_takes_petal`):
  the petal look is cut from the letters' shapes and cannot carry a color
  emoji, so that header keeps the old flat tint: the border's dominant accent
  (`dominant_border_color`) over a drop shadow, with the emoji drawn in it.

| Key | Face | Notes |
|---|---|---|
| `thught` | Thught Regular | Bought license, Latin only. **Default body font** for quotes. |
| `thught_black` | Thught Black | Bought license, Latin only. **Default header font.** |
| `belleza` | Belleza | OFL, Latin only. **Default body font** for banners. |
| `times` | Liberation Serif | OFL, metric-compatible Times New Roman stand-in. What a Thught body falls back to. |
| `helvetica` | Arimo | OFL, metric-compatible Helvetica/Arial stand-in. What a Thught header falls back to. |
| `inter` | Inter | |
| `playfair` | Playfair Display | High-contrast display serif. |
| `oswald` | Oswald | Condensed sans. |
| `caveat` | Caveat | Handwriting. |
| `bebas` | Bebas Neue | Tall condensed caps. |

> The originals (Helvetica, Times New Roman) are proprietary and cannot be
> bundled; the metric-compatible OFL clones are used and exposed under the
> familiar names. There is no `lora` style (its file was corrupt and crashed
> any card that selected it); a stored `lora` value falls back to the default.

**The default faces fall back; nothing else does** (`resolve_font_style`).
Thught and Belleza carry Latin only, and as the defaults they meet every quote,
banner and display name. A card whose text has a character Thught lacks *and the
fallback has* is drawn whole in the fallback; one face per card, never boxes
in the middle of a line. With an avatar the member's name is the attribution
and counts toward the body; without one it is the header and counts there.
Emoji, CJK and anything else neither face carries do not trigger it, because
switching would not draw them either. A missing Thught file degrades the same
way. That is a **deliberate exception** to "fonts fail loudly" below: a default
face that raises would take every card down with it, and the file is a bought
license that a copy of this repo may legitimately lack.
`tests/quotes/test_quote_renderer_fonts.py` fails if either half is reverted. Belleza
takes the character fallback only: it is open-licensed and committed, so a
missing file still raises (`tests/quotes/test_quote_renderer_box.py`).

The picker shows a key through `font_label` (`thught_black` → "Thught Black").
The license is desktop-only: Thught is drawn into images and never served, so
it is not a dashboard web font (`assets/fonts/README.md`).

## Borders

`BORDERS: dict[str, BorderStyle]` holds the bundled frames; a guild may also
upload its own. A `BorderStyle` carries `path`, `flip` (mirror so a corner
motif lands away from the left avatar), `luma_key` (key out an opaque black
background vs. use a real alpha channel), `mask_fit`, and `slim_frame`.

### Golden Poppy (`golden_poppy`, `slim_frame=True`)

The default bundled floral frame. Its PNG bakes a thick gold frame **and** a
large flower cluster into one raster. To keep the frame full-bleed (so the
hand-tuned avatar/text layout is untouched) while making the decoration subtler,
`slim_frame` triggers `_composite_slim_border`, which:
- draws a **thin gold rounded-rect** frame itself (replacing the thick baked one), and
- crops **only** the flower cluster (a fixed interior box clear of the frame
  lines), shrinks it to **~72%** (`_SLIM_FLOWER_SCALE`), and tucks it into the
  bottom-right corner.

Tuning knobs live at module scope: `_SLIM_FRAME_GOLD`, `_SLIM_FLOWER_SCALE`,
`_SLIM_FLOWER_CROP`.

### Midnight (`midnight_frame`, `mask_fit=True`)

A real-alpha PNG composited as-is (no slim treatment, no luma key), rendered at
its native 3:2 on a 900×600 canvas.

Unlike Golden Poppy, whose decoration sits in one corner, this frame's flowers
climb the whole left side and reach ~45% across. The poppy-tuned layout pins the
avatar at `0.18w` and the text column at `0.34w`, which buried the avatar under
the flowers and ran the attribution through them, so this frame **fits its own
opening** like an uploaded one.

Its opening has **no room for an avatar disc at all** (511px wide at mid-height;
an avatar with its ring plus a readable column needs ~600px; no disc fits at
any row down to r=52px, well under the r=66px floor). `_fit_pfp` returns None,
so Midnight quote cards render in the **banner layout**: `author_name` becomes
the centered header, the quote centers under it, and the avatar survives only as
the blurred background. That is the frame's geometry, not a fallback bug.

### Custom uploaded frames (`mask_fit=True`)

A guild can upload its own frame on the dashboard's Quote Tool page, under
Config → Channels & Messages (its API route is `/config/quote-border`). It's stored beside the DB at
`db_path.parent/quote_borders/<guild_id>/border.png` (`guild_border_path`) and
re-encoded to a real-alpha RGBA PNG, so `flip`/`luma_key` are both False.

Like Midnight and unlike Golden Poppy, a custom frame **drives its own layout**:
rather than a
fixed text column, `analyze_border_opening` reads the frame's transparency and
returns the see-through opening as per-row `[left, right]` spans plus a fitted
avatar disc. `render_quote_card` then flows the avatar + auto-shrunk quote text
inside that opening. Upload **rejects** a frame whose center has no usable
opening (probed at `card_size_for_border(900, 500, frame)`, i.e. the canvas the
card will actually have, so the guard measures the real geometry rather than a
stretched one), and rendering always has a valid hole to fit into.
Openings are cached by `(path, mtime, size)`.

> Because custom-frame layout derives from the alpha opening, the `slim_frame`
> shrink treatment is intentionally **not** applied to them; only Golden Poppy
> opts in. Midnight and custom frames render through the original composite.

## Emoji

Both Unicode emoji (via `pilmoji` + Twemoji, when installed) and Discord custom
emoji (`<:name:id>` / `<a:name:id>`, passed in as `custom_emojis: {id: bytes}`)
are measured and composited inline at their token positions. `pilmoji` is an
optional dependency; without it, text still renders (custom emoji still
composite; Unicode emoji fall back to the font).

The quote body, the attribution line, and the no-pfp header all draw through
pilmoji, so a Unicode emoji renders in color wherever it appears, including
inside a member's display name.

Twemoji is fetched over HTTP the first time each glyph is drawn. Two safeguards
keep that off the critical path:

- **Bounded fetch.** pilmoji sets no timeout, so a stalled CDN would hang the
  render worker thread indefinitely. A custom `TwemojiEmojiSource` subclass
  threads a `_EMOJI_FETCH_TIMEOUT` (5 s) into whichever HTTP backend pilmoji
  chose (`timeout` can't sit in pilmoji's `REQUEST_KWARGS`, since `urllib.Request`
  rejects it and only `urlopen` takes it).
- **Fail-soft everywhere.** A fetch failure never fails the card. The
  attribution/header draw is wrapped and degrades that line to plain PIL text
  (emoji become tofu). The body render snapshots the canvas first, and on any
  failure rolls back to that snapshot and re-renders the lines without pilmoji,
  so a partial draw can't double up, and an outage yields a tofu-emoji card
  rather than a failed one.

## Display names

`author_name` is NFKC-normalized once on entry to `render_quote_card`, before
either the attribution or the header uses it. Discord names commonly use
Mathematical Alphanumeric Symbols (`𝓟𝓻𝓲𝓷𝓬𝓮𝓼𝓼 𝓡𝓪𝓬𝓱𝓮𝓵` → `Princess Rachel`) or
fullwidth forms; **no bundled TTF carries those codepoints**, so without folding
they draw as a row of tofu boxes. NFKC maps them to their compatibility
equivalents, is a no-op for ordinary names, and leaves emoji intact (they have
no decomposition) for pilmoji to draw.

## Key constraints

- **`QUOTE_MAX_CHARS = 280`**: longer text is ellipsized.
- Fonts load eagerly and raise `FileNotFoundError` loudly if a TTF is missing:
  a broken/absent font is a hard failure, not a silent degrade. Thught is the
  one exception (§ Fonts).
- Rendering must run in a worker thread (`asyncio.to_thread`).
- New embeds/cards should take their accent from `safe_resolve_accent`; the
  card themes above are the exception where color is part of the design.

## Related

- Live-test checklist: the commit's own `Testing:` section (see CLAUDE.md),
  posted as a QA Tracker card.
- Tests: `tests/quotes/test_quote_border.py` (opening detection, mask-fit rendering,
  determinism), `tests/quotes/test_quote_animated.py` (animation, layer matte,
  still fallback, filename), `tests/web/test_quote_border_routes.py` (upload/preview/delete),
  `tests/quotes/test_quote_box.py` (the box's layout, wrap, dots and frame, and its
  match with the panel art generator), `tests/quotes/test_quote_renderer_box.py` (the
  boxed banner end to end).
