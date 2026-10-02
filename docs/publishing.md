# Publishing to the public mirror

This checkout is the repository: full history, every branch, the session
worktrees, the private tooling. The public GitHub copy is a **release
mirror**: it holds one commit per release, each a snapshot of `main` at the
moment of the release, and nothing else. Development never happens there.

`scripts/publish_release.py` is the whole flow. This document is the why,
the setup, and what each press does.

## The model

- **The private history stays private.** A release commit on the mirror is
  built from the release's *tree object*, not from its commits: the script
  reads `main`'s tree into a throwaway index, drops the paths in
  `.publishignore`, writes the tree, and makes a commit whose only parent is
  the previous release. Pushing that branch sends the files in the snapshot
  and nothing reachable from the private history — no commit messages, no
  authors, no branch names, no deleted file ever comes along. The test that
  pushes into a bare repo and checks the private commit and a withheld blob
  are absent is the contract (`tests/tooling/test_publish_release.py`).
- **One commit per release, chained.** Not a force-pushed single commit:
  the mirror's history is a flat line `v1.0.0 → v1.1.0 → v2.0.0`, so
  `git diff v1.0.0 v1.1.0` works there, forks keep fast-forwarding, and a
  GitHub Release can sit on each tag.
- **The snapshot is `main`'s tree minus `.publishignore`.** Anything
  `.gitignore`d is already not in a commit (`private/`, the commercial fonts,
  the server map, `docs/plans/`). `.publishignore` is the list of *tracked*
  files that stay here: `assets/` whole — the server's own fonts, borders,
  logo, panel art and tile set; the public copy ships no media at all, and
  `deployment.md` § Assets you supply says what an operator puts there; the
  maintainers' Claude Code session tooling (`.claude/`, the session
  launcher, its systemd units, `dev_sessions.md` and their tests —
  `CLAUDE.md` itself stays, being the rulebook `CONTRIBUTING.md` points at);
  the DK MCP server (`src/dk_mcp/`, its tests, doc, unit, deploy script and
  lock), which is the maintainers' own claude.ai tooling over their tunnel
  and host; and the two Claude GitHub workflows, because they run with the
  maintainer's OAuth token on events any passer-by can raise on a public
  repo. The file itself is published, so the mirror can see what it lacks.
  A test that ships must pass without the withheld files: `test_gate_scope`
  and the `.publishignore` self-check skip rows whose files are absent,
  which is how a test tells the mirror from the repo. A rule is a path from the repo root, a directory with a trailing
  slash, or a glob over the full path (`*` crosses `/`); it is **not**
  gitignore syntax — a leading `/`, a `**` or a `!` is refused rather than
  silently withholding the wrong thing.
- **The CHANGELOG is the public voice.** `CHANGELOG.md` § Unreleased is
  written as features land; `prepare` turns it into the version's section,
  and that section becomes the public commit's message and the tag's notes.
  An empty section blocks the next release — nobody publishes without
  saying what changed.
- **Tags mean the same thing in both repos.** `vX.Y.Z` is an annotated tag on
  the *public* commit and is pushed verbatim; `release/vX.Y.Z` marks the
  private `main` commit it was cut from, and the public commit body carries
  that sha as `Source-Commit:` so a bug report against a tag resolves to the
  exact private commit in one step.
- **The push is its own press.** `publish` builds the commit and tags
  locally and prints the push; `--push` is the irreversible step and is
  never implied. Nothing in the flow force-pushes.

## Setup (once)

1. **Pick a licence** and commit `LICENSE` at the root. `publish` refuses
   without one: a public repository with no licence is all-rights-reserved,
   which is not open source.
2. **Create the mirror empty** — no README, no first commit, or the first
   push is not a fast-forward:

       gh repo create billydev-secret/OpenKeeper --public
       git remote add public git@github.com:billydev-secret/OpenKeeper.git

   On the new repo: Settings → Code security → enable private vulnerability
   reporting (`SECURITY.md` points there), and consider disabling direct
   pushes to `main` for everyone but the publishing account. Nobody commits
   on the mirror; a commit there makes the next release push a non-fast-
   forward, which the script does not force past.
3. **Set the public author.** `publish` never borrows the checkout's own
   `user.name` / `user.email` (the prod checkout commits as
   `gate <gate@example.com>`, and a personal address must not land on the
   mirror either): it refuses until `publish.author` or `--author` names the
   identity, and refuses a placeholder one.

       git config publish.author "Your Name <id+handle@users.noreply.github.com>"

   (`--author "Name <email>"` on a single run overrides it.)

## Cutting a release

From the prod checkout, on a clean `main` that has had its regression pass
(`/dk-regress`):

1. **Write the notes.** `CHANGELOG.md` § Unreleased, for someone who reads the
   mirror: what a server admin or a developer gets, not which branch merged.
2. **Prepare.**

       python scripts/publish_release.py prepare v1.0.0

   Bumps `version` in `pyproject.toml`, retitles the section
   `## v1.0.0 (date)` leaving a fresh empty `## Unreleased` above it, and
   commits `Release: v1.0.0`. `pyproject.toml` is a full-run file for the
   pre-commit gate, so this commit runs the whole suite — that is the
   release's last regression check, and the reason to cut from a tree that
   already passed `/dk-regress`. `--no-verify` skips it when the box cannot
   gate; use it only after that pass.
3. **Publish.**

       python scripts/publish_release.py publish v1.0.0

   Runs the checks, builds the public commit on the local `public` branch,
   tags `v1.0.0` (public commit) and `release/v1.0.0` (source commit), and
   prints the push. `--dry-run` stops after the checks. If tagging or the
   push fails, every ref the run wrote is undone, so a retry starts from
   "nothing built" rather than "already published". The checks:

   | Check | Why |
   |---|---|
   | HEAD is a clean `main` | releases are cut from the integration branch, never a session |
   | `pyproject.toml` version equals the tag | the package says what the tag says |
   | `CHANGELOG.md` has a non-empty `## vX.Y.Z` section | it is the public commit message |
   | a `LICENSE` exists | no licence, no open source |
   | `vX.Y.Z` is untagged; `release/vX.Y.Z`, if present, is HEAD | a version is published once |
   | no tracked file matches `.gitignore` | a `-f`-added `.env` or database never ships |
   | no secret-shaped string in the tree | bot token, GitHub/API/AWS/Slack keys, private-key blocks |
   | `tests/tooling/test_no_real_identifiers.py` passes | the snowflake guard over the working tree |
   | `tests/tooling/test_public_tree_leaks.py` passes | the leak scan over the **built public tree** — see below |
   | a public author is configured and is not a placeholder | the checkout's own identity never signs a public commit |

   The identifier guard reads the working tree, not the commit, so a stray
   untracked file holding a real-looking id can refuse a release whose
   commit is clean; delete the file. The secret scan reads the commit.

4. **Push.** The line the script printed, or `publish v1.0.0 --push`:

       git push public refs/heads/public:refs/heads/main refs/tags/v1.0.0:refs/tags/v1.0.0

   Pressing it publishes the snapshot as the mirror's `main` and its tag.
   Then, optionally, a GitHub Release from the tag's notes:

       gh release create v1.0.0 --repo billydev-secret/OpenKeeper --verify-tag --notes-from-tag

**Undo before the push:** `git tag -d v1.0.0 release/v1.0.0`, then
`git update-ref refs/heads/public <previous public commit>` (or
`git branch -D public` for a first release), then `git reset --hard HEAD~1`
on `main` if the prepare commit should go too. **After the push there is no
undo** — clones exist the moment it lands; fix forward with a patch release.

## The leak scan

`tests/tooling/test_public_tree_leaks.py` builds the exact tree `publish`
would send and reads it back, so it checks the bytes that ship rather than
the working tree. It fails a release on any of: a file under a path that
must never ship (`assets/`, `private/`, the server map, `.env`, the Claude
workflows — pinned in the test, so a shortened `.publishignore` is caught);
any image, font, audio or database file anywhere; a real-looking Discord id
(the `test_no_real_identifiers` rule over the shipped blobs); an email off
the placeholder domains; or any string on the **private denylist**.

The denylist is the part only the prod checkout can run, because it lives in
`private/` (gitignored, so it can never ship): `private/publish-denylist.txt`
(one string per line — real names, handles, hostnames, a LAN address), plus
the left-hand side of every `old==>new` line in `private/replace-text-*.txt`
(the filter-repo lists from the history scrubs) and every non-placeholder
address in `private/mailmap-*.txt`. Matching is literal and
case-insensitive. A failure reports the file and the entry's number and
length, never the string, because that output lands in logs. Add to the
denylist whenever a scrub replaces something; the test then stops it coming
back. In CI or a session worktree, which have no `private/`, the denylist
half skips; under `publish` it **fails** instead (the script sets
`DK_PUBLISH_GUARD=1`), so a release is never cleared by a scan that could
not run — cut releases from the prod checkout, where `private/` lives. An
entry shorter than four characters is refused rather than matched inside
words. The dashboard's own OFL web fonts under `static/fonts/` are the one
font files allowed to ship.

## Versions

Semantic versions, `vX.Y.Z`. "Major release" is the maintainer's call; the
script enforces the shape and that each version is published once, nothing
about ordering. The first public release starts the mirror's history; pick
its number knowing it is the oldest the mirror will ever show.

## Contributions against the mirror

A pull request on the mirror cannot be merged there — its base is a snapshot
with no path back to `main`. The maintainer applies it here instead:

    gh pr diff <n> --repo billydev-secret/OpenKeeper | git apply -3
    git commit --author="Contributor Name <their-noreply@users.noreply.github.com>"

(or `gh pr checkout` in a scratch clone and `git format-patch` / `git am` to
keep their commits), credits them in `CHANGELOG.md` § Unreleased, and closes
the PR with the commit it became. Their authorship survives into the private
history; the mirror shows it in the CHANGELOG and the next release's notes.
`CONTRIBUTING.md` § The public mirror says this from the contributor's side.

## What the mirror runs

`.github/workflows/test.yml` and `nightly.yml` ship in the snapshot and run on
the mirror — Actions is free for public repositories, so the mirror gets the
CI the private repo's billing block denies it. Both need no secrets. The two
Claude workflows are withheld (`.publishignore`); nothing on the mirror holds
a token.
