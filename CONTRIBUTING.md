# Contributing to Dungeon Keeper

Welcome. This page is the human entry point: what to read, how a change
lands, and where the rules live. It restates as little as possible — every
rule below points at the document that owns it.

## Read in this order

1. **[`docs/dev_setup.md`](docs/dev_setup.md)** — get the bot and dashboard
   running on your machine against your own test server. An afternoon.
2. **[`docs/codebase_tour.md`](docs/codebase_tour.md)** — where things are and
   how a request flows. Half an hour.
3. **[`docs/design_guide.md`](docs/design_guide.md)** — *before building a
   feature.* The order of the design decisions, the coding standards, what
   tests ship with it, and the **Part 4 checklist** you run before you
   commit. It owns the spine; each rule points at the document that owns
   the detail.
4. **[`docs/INDEX.md`](docs/INDEX.md)** — every spec, classified Reference /
   Design / Aspirational. Not all of them describe things that exist. When
   a spec and the code disagree, **the code wins**.

`CLAUDE.md` at the repo root is the same rulebook compressed for AI coding
sessions; it is loaded into every one of them automatically. You can read it
as a one-page summary, but the design guide is the version written for a
person, and several of its Workflow rules (worktree sessions, "this checkout
is production") describe the maintainer's own host, not yours.

## How a change lands

The repository is developed by a small group of maintainers; the live bot
runs from one maintainer's checkout, and only they deploy.

1. **Branch** from `main`. One branch per change.
2. **Build it with its tests and its docs in the same commit.** A feature or
   a bug fix lands with tests at the logic layer; a behaviour change updates
   the matching spec in `docs/`; a UI change updates the dashboard manual;
   a new per-user table gets a data-register row. The full list, in the
   order the hook and the reviewer hit it: `design_guide.md` Part 4.
3. **Commit.** The pre-commit hook runs `python scripts/gate.py --scoped`
   (ruff, then the tests mapped to your diff). Subject `Scope: descriptive
   summary`, a prose body saying why, and — if a member or admin can see the
   change — a `Testing:` section of `- [ ]` lines written for a volunteer
   tester (`design_guide.md` Part 4 § The commit itself). No
   `Co-Authored-By` or similar trailers; the commit-msg hook refuses them.
4. **Before pushing**, `python scripts/gate.py --quick` (pyright and the
   browser panel checks — the two whole-repo checks the hook skips) and, if
   you touched dashboard JS or CSS, `npx eslint` / `npx stylelint`.
5. **Open a pull request** against `main`. CI runs ruff, pyright, the full
   suite and the browser sweep on every PR; all four must be green. A
   maintainer reviews and merges.
6. **Merging is not launching.** The live bot picks up `main` only when the
   deploying maintainer pulls and restarts, and anything with a live blast
   radius must land inert — a dial defaulting off, a NULL meaning "not
   configured" (`design_guide.md` Part 5, *Ship it dark*). Say in the PR
   what the change does the instant it is live, and which switch, if any,
   someone has to flip afterwards.

Never force-push `main`. Never commit `.env`, a database, or anything under
`assets/fonts/` (licensed) or `private/`.

## The rules that bite

- **No real people or server ids in the tree.** Synthetic ids, placeholders
  and invented names only — a test enforces the ids, a reviewer the rest
  (`docs/design_guide.md` § The docs contract).

The gate, ruff, pyright, eslint and a few hundred contract tests enforce
most conventions mechanically — when one fails, the message names the rule.
The handful that no tool can express, and that reviews turn on:

- **Configuration lives on the web dashboard, not in Discord.** No slash
  commands, modals or buttons for admin settings. Discord is for member
  self-service and moderator actions.
- **Collapse controls.** One dial with a few states beats several
  overlapping toggles; reorganise a jumbled page rather than appending.
- **Safety and privacy defaults.** Age gating keys off Discord's own channel
  flag; store minimal data; never ship a toggle nothing reads; any surface
  that puts two members in contact consults the no-contact list and refuses
  invisibly.
- **Behaviour goes in the logic layer; cogs, views and routes are glue.**
  That is what makes a feature testable without Discord.
- **Same commit, not a follow-up** — tests, spec, manual, register.

Each is a pointer into `design_guide.md` (Part 1 for the decisions, Part 2
for the standards), and `docs/common_modules.md` answers the question that
comes up while you build: *does something already own this?*

## The public mirror

The GitHub repository is a **release mirror**: one commit per release, each
a snapshot of the maintainers' `main`, published by
`scripts/publish_release.py` (`docs/publishing.md`). Development history
lives in the maintainers' repository and is not pushed. Two consequences
for a contributor:

- **A pull request here is applied, not merged.** A maintainer applies your
  change to their `main` with your authorship, credits you in
  `CHANGELOG.md`, and closes the PR naming the release it ships in. Your
  change appears on the mirror with that release.
- **`main` here moves only at a release.** Branch from the latest tag; the
  next release is a fast-forward of it.

A change a server admin or developer would notice gets a line in
`CHANGELOG.md` § Unreleased in the same PR — that section is the release's
public notes.

## Where to ask

Open a GitHub issue or discussion for anything about the code. Security
problems go through the private channel in [`SECURITY.md`](SECURITY.md),
never a public issue.
