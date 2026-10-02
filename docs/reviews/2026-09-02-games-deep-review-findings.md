# Games deep review: findings register (2026-09-02)

Condensed 2026-10-01; the full original is at `git show 66a4d886:docs/reviews/2026-09-02-games-deep-review-findings.md`.

Companion to `2026-09-02-games-deep-review.md`. The original register held all 211 findings from the fourteen
review agents with verifier verdicts, repro narrative and fix text. Packages P0 to P9 were built and merged
(commits `7a8bb441` P1, `d47b4b90` P2, `7eaae35d` P3, `2d0b54bf` P4, `5d4a2390` P5, `b0d4d50c` P6,
`38c1ff6e` P7, `4f440aee` P8, `ccd37cd7` P9, plus the ship-review fix commits), and the P0 prod-state script
was applied and then deleted as a spent one-shot (`b85623b6`). This version keeps only what is still open.
Severity (high = money, safety, data lost or unplayable; medium = a player or host will hit it; low = polish)
and size (S under an hour, M half a day, L multi-day) are the verifiers' values. File paths are as of
2026-10-01, after the package layout move; line numbers are dropped because they no longer hold.

## Resolved tally

**204 of 211 resolved, 7 declined or left to an owner press on 2026-10-01; none open.** Of the 196: 4 refuted by the verifiers
(discovery-11, safety-sweep-9, clapback-16, rotation-rooms-168), the rest fixed in code or settled by a
recorded decision (D1, D2, D4 and D6 taken as recommended; Fantasies stays without an age gate by the owner's
07-27 call). All seven highs (safety-sweep-1 and 2, social-prompt-32, anon-tail-67, trivia-tail-81,
duels-party-112, rotation-rooms-155) are fixed.

Resolved IDs by prefix:

- platform: 18-31
- discovery: 1-13
- safety-sweep: 1-11
- clapback: 1-14, 16, 17
- social-prompt: 32-48
- vote-games: 49-62
- anon-tail: 63-80
- trivia-tail: 81-98
- photo-external: 99-105, 107-111
- duels-party: 112-119, 121-123, 125-128
- casino: 130-132, 134-137, 139-141
- mahjong: 142-149, 151, 152, 154
- survivor: 172-180, 183-187
- rotation-rooms: 155-157, 159-163, 165-169

Decisions D3 (retirements) and D5 (XP for play) from the main document were never taken, so nothing was
retired and no game grants XP; the findings below that depend on them say so.

## Declined 2026-10-01

The owner closed these without building them:

- **duels-party-120** (fold Hot Potato Group into the duel), **duels-party-124** (a Pressure Cooker Vent
  choice), **mahjong-150** (lighter escrow) and **mahjong-153** (XP and a quest hook for Mahjong, decision D5).
- **clapback-15** and **photo-external-106** (thin prompt banks): content work for the question studio, not code.
- **casino-138** (dead tables) needs no code: closing derby, baccarat and dice is an owner press on
  Economy, Casino.

The last eight were fixed on 2026-10-01 (merges 843da910, 674e870e, 6a85cc64): rotation-rooms-158
(a round lifetime dial whose expiry deletes the card), 164 (a leaver's rounds close and originals go),
170 (the guesser is left out of their own picker) and 171 (a version-0 legacy consent kind and an
owner-run backfill script); survivor-181 (Cancel & Refund) and 182 (the ghosts tally); casino-133 (a
per-play payout ceiling dial); duels-party-129 (one shared duel config panel).

## Still open

None. The review is closed.
