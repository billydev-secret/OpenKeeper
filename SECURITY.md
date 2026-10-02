# Security Policy

Dungeon Keeper is a single-operator hobby project, not a vendor product.
Security handling here is best-effort, not backed by an SLA.

## Reporting a vulnerability

Please use GitHub's private vulnerability reporting rather than a public
issue, a pull request, or a Discord DM: open
<https://github.com/billydev-secret/OpenKeeper/security> and click
**Report a vulnerability**.

> **This requires a one-time repo setting that isn't on by default** —
> Settings → Code security and analysis → Private vulnerability reporting →
> Enable. Only the repo owner can flip it; this file alone does not turn it
> on, and the button above will not appear until it's enabled.

There is no dedicated security email — please don't invent one to report to.
GitHub's private advisory flow above is the only intended route.

## What to expect

This is a single operator working best-effort, not a vendor with a support
contract: expect acknowledgement within a few days, not hours, and no
guaranteed fix timeline. There is no bug bounty; a credit on the fix (in the
advisory and/or the commit that resolves it) is offered instead.

## Supported versions

There is no version matrix to check a report against. The project has no
semver releases — `pyproject.toml`'s version has sat at `0.1.0` — and no
CHANGELOG. Deployment is continuous off `main`, tracked by the `deployed` git
tag that the running bot force-moves to its own commit on every boot (see
`docs/deployment.md`). Report against current `main`; that is what's running.

## Scope

**In scope:**

- This repository's bot code (`src/`).
- The dashboard (`src/web_server/`), wherever a deployment publishes it.
- The DK MCP server, the maintainers' read-only docs/source server for
  claude.ai. It is not part of the public repository; reports about it are
  still welcome through the same channel.

**Out of scope:**

- Discord itself — report platform issues to Discord directly.
- Third-party services the bot calls but doesn't own: Spotify, ESPN, and
  whatever LLM backend is configured as the AI provider.
- The Truth-or-Dare bot — it shares the maintainers' host and tunnel but
  lives in a separate codebase, not this repository.
- Other services sharing the same host and Cloudflare tunnel — not this
  codebase.
- Social engineering and physical access to the host.

A report touching member data specifically may also be a GDPR matter — see
`docs/gdpr_runbook.md` § 3 (Personal-data breach, Art 33/34). The two tracks
aren't exclusive; a security report can open both.
