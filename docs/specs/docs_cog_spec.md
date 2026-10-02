# Channel Docs — Feature Spec

This spec describes the **channel-docs feature**, not the repo's `docs/` folder. A "doc" here is a guild-scoped, single-source markdown document (rules, mod FAQ, staff info, and so on) authored on the dashboard (Config → Channels & Messages → Docs) and stored in the database. Placing a doc into channels and re-syncing it happens entirely on the dashboard: editing the doc's markdown re-renders every channel it's posted in, in place. Code lives in the `src/bot_modules/docs/` package (`render` / `db` / `sync`) and the dashboard route `src/web_server/routes/docs.py`.

## Surface

Everything is on the dashboard's Docs panel (`static/js/panels/docs.js`), moderator-level:

| Action | Endpoint | Purpose |
|---|---|---|
| List docs + placements | `GET /api/docs` | The guild's docs and which channels each is posted in |
| Post into a channel | `POST /api/docs/{doc_key}/placements` | Place a doc in a channel and keep it synced |
| Re-render everywhere | `POST /api/docs/{doc_key}/sync` | Re-render a doc in every channel it's posted in |
| Remove from a channel | `DELETE /api/docs/{doc_key}/placements/{channel_id}` | Delete the doc's messages and drop the placement |
| Pin / unpin a placement | `PUT /api/docs/{doc_key}/placements/{channel_id}/pin` | Toggle the placement's pinned state |

There are no `/docs post|sync|unpost|list` slash commands: each would be a 1:1 duplicate of the endpoint above it, and CLAUDE.md keeps configuration on the dashboard. There is no docs cog; the routes call `docs_db` / `docs_sync` directly, with no cog dependency.

## Behavior

### Rendering (markdown → embeds)

`render.render_doc(title, body_md)` is a pure function (no `discord` import; unit-testable). Discord renders most markdown natively inside an embed *description*, so rendering is mostly pass-through, with structure imposed only where Discord can't:

- A line that is just `---` / `***` / `___` is a **message break**: each section becomes its own message.
- Headings stay inline as `#`/`##`/`###` markdown in the description (Discord renders those larger than the fixed-size embed `title` field). If the first section doesn't open with a heading, the doc's title is prepended as a `#` header. The embed `title` field is never used.
- Image markdown `![alt](url)` is stripped from the text and the section's first image becomes the embed's image. Masked links `[text](url)` pass through.
- A section longer than 4096 chars splits on paragraph boundaries into continuation embeds, never inside a fenced code block. Oversized single blocks hard-split on line breaks.
- One embed per message (sidesteps the 6000-char aggregate limit). An empty doc renders one placeholder embed (`*(This document is empty.)*`).

Embed color: the doc's `accent` hex if set, otherwise the guild's branding accent (`safe_resolve_accent`).

### Posting and sync reconciliation

`post` upserts a **placement** (doc × channel) and renders into it. Postable channel types: text channels, threads, and voice-channel chats (`core.utils.POSTABLE_CHANNELS`). The dashboard's channel picker can place a doc into any of the three.

`sync` reconciles the rendered embed list against the placement's tracked message ids position-by-position: edit where a tracked message exists, send where it doesn't, delete the surplus (doc shrank). If a tracked message was manually deleted mid-list, the mapping is "torn": since `channel.send` only appends, everything from the tear down is re-sent fresh and the stale tail deleted, keeping the channel visually in doc order. On `Forbidden`/HTTP errors the sync bails with a per-channel status (`ok` / `missing_channel` / `forbidden` / `error`) but never loses track of still-live message ids. Resulting ids are persisted after every sync (except when the channel itself is gone).

`unpost` best-effort deletes each tracked message, then drops the placement row.

### Pinning (dashboard-only)

A placement can be flagged `pinned`; sync then pins the doc's messages and unpins them when cleared. The pin pass is **delta-only** (never re-pins an already-pinned message, so steady-state syncs emit no "pinned a message" notices) and pins bottom-up so the pins list reads in doc order. Pin failures (missing Manage Messages, 50-pin limit) are reported separately (`pin_detail`) and never mark the placement broken. There is no `/docs` subcommand for this; the toggle lives on the dashboard.

### Dashboard surface

The dashboard (moderator-gated) owns authoring: create/edit/delete docs, live embed preview, image upload (raster only: PNG/JPG/GIF/WEBP, 8 MB cap, uuid filenames served from `/static/doc-images/`), placement add/remove, pin toggle, and manual sync. An upload wider than 3:4 portrait is padded on arrival (see *Image width on desktop* below). A dashboard save (`PUT /docs/{key}`) immediately re-renders every placement; deleting a doc first pulls its messages down from every channel.

### Image width on desktop

Nothing in the bot sets an embed's width; Discord does. The **desktop** client sizes an embed to its image, so a square or landscape image renders the whole embed visibly narrower than a portrait one, and the text wraps to match. The phone apps stretch every embed to the screen and show no difference. Measured 2026-09-12 in the staff bios channel: one square (1:1) image among portrait bios rendered narrow whatever text sat above it; the same image on a transparent 2:3 or 3:4 canvas rendered full width. Text length and heading size made no difference.

So the upload endpoint (`POST /docs/images`) runs `image_logic.pad_to_portrait` on every upload: an image wider than 3:4 (height/width below 4/3, with a 0.01 tolerance so a photo a pixel off 3:4 is left alone) is centered on a transparent 3:4 canvas (never cropped) after its EXIF orientation is applied. Transparency needs alpha, so JPG is stored as PNG; WEBP stays WEBP. The canvas is capped at 1536 px wide (Discord never displays an embed image near that). Left exactly as uploaded: portrait images, GIFs and animated WEBPs (re-encoding would flatten an animation), anything Pillow can't decode, and a padded result that would exceed the 8 MB limit. The response carries `padded: true|false` and the editor says when it padded. Tall bars on a wide banner image are the accepted cost. Images uploaded before this existed are not rewritten; re-upload one to pad it.

### Limits

Doc keys are slugs (`[a-z0-9-]`, max 49 chars; user input is slugified). Title ≤ 200 chars, body ≤ 40,000 chars.

### Docs panes (Design, unbuilt — the pager is built)

A **docs pane** will be a posted channel panel that opens Channel Docs
privately from dropdowns, instead of (or as well as) posting them. Stage 1,
the **pager**, is built and dark: nothing calls it yet. Stages 2 (schema,
service, dashboard) and 3 (the pane in Discord) are unbuilt. The build plan is
local, not committed (docs/plans/ is ignored); the decisions below are the
record (Ben, 2026-09-28/29):

| Question | Decision |
|---|---|
| Which docs go in the dropdowns? | **Grouped by category**, one select per category; a doc in no category is not in the pane. |
| Does a pane replace channel posts? | **An option, per doc**: posted, in a pane, both, or neither. No guild-wide mode dial. |
| Dashboard | A **Docs Panes** card at the top of Config → Docs (panes, their categories and order, Post); each doc's editor gains a **Where it shows** block. |
| Long docs | **Auto-fill to budget**: sections packed into as few pages as fit; `---` inside a page is a drawn separator; short docs never page. |
| Where a pane lives | **Its own channel panel**, and a guild can have **several** (e.g. Start Here, Staff). |
| A doc in several panes | **Many pane slots per doc**, each a *(pane, category)*. |
| NSFW slot guard | **None**: admins place docs where they belong (2026-09-29). |
| Hub Help cards | **Not paged**: they keep one card with a cut line (2026-09-29). |
| Header art | The guild's Branding banner with **the pane's own name** drawn in (2026-09-29). |

**The pager** (`docs/pages.py`, `plan_doc_pages` / `build_doc_page_items`):
the doc's leading heading becomes a `##` title repeated on every page, with a
`-# Page N of M` caption; sections fill a page until the text budget
(`help_card.HELP_TEXT_BUDGET`) or the components left after the controls run
out, and only the budget turns a page. An image rides its section's first
piece (only the first image of a section is kept, as everywhere else in
docs). A block too long for any page splits at a line break; a fenced block
split that way is **re-fenced on every piece** — unless its opening line is so
long that re-fencing would leave under `MIN_FENCE_ROOM` (200) characters a
piece, when it splits as plain text instead (an admin-written opener near a
page long once made the split loop forever — ship review, 2026-10-01). A
single line with no break is cut at the budget, which can land inside a
markdown span; blank lines at a cut are dropped.

## Configuration

No global settings. Per-doc: optional `accent` hex (falls back to the guild branding accent). Access is gated at the dashboard's moderator level; the bot needs send/edit/delete permission in target channels, and Manage Messages where pinning is enabled.

## Stored data

Three tables (migrations `059_docs.sql`, `061_doc_placement_pinned.sql`):

- **`docs`**: canonical source: `guild_id`, `doc_key` (unique per guild), `title`, `body_md`, `accent`, timestamps, `updated_by`.
- **`doc_placements`**: one row per doc × channel: `doc_id`, `channel_id` (unique pair), `pinned`, timestamps.
- **`doc_placement_messages`**: the ordered Discord message ids rendering a placement (`placement_id`, `message_id`, `position`); replaced wholesale on each sync.

Uploaded doc images live on disk under `src/web_server/static/doc-images/` and are referenced by URL from the markdown.
