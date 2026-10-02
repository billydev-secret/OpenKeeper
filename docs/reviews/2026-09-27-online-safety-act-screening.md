# Online Safety Act 2023 — screening note (2026-09-27)

Standards audit row 3.36. This is a **screening note, not an assessment**. It
records the questions the UK Online Safety Act (OSA) raises for this bot so that
they can go to someone qualified to answer them. **It needs a legal opinion
before anyone relies on it, and it is not legal advice.** It is an engineer
reading the Act's structure against the code, in the same spirit as the GDPR
assessment ([2026-08-06-gdpr-compliance-assessment.md](2026-08-06-gdpr-compliance-assessment.md)),
which left its own threshold question open for the same reason.

Nothing here changes the bot's behaviour. The Ofcom dates below are recalled,
not checked against Ofcom's current guidance, so a reader should verify them
first.

## 1. The threshold questions (for counsel)

Everything below depends on these, and none of them is answered.

1. **Is Dungeon Keeper a regulated service in its own right, or only a
   feature of Discord's?** Discord is plainly a regulated user-to-user service,
   and the duties sit with Discord as its provider. Several features here,
   though, take one member's content and put it in front of other members
   through the bot's own flows — sometimes with the author's identity removed
   (§2). Whether a third-party bot doing that is a "user-to-user service" under
   s.3, and whether its operator is a "provider" under s.226, is the central
   question. None of the Schedule 1 exemptions obviously fits: the bot is not
   email or SMS, not limited-functionality comments on provider content, and
   not an internal business tool.
2. **Does it have "links with the United Kingdom" (s.4(5)–(6))?** The operator
   appears to be US-based (the GDPR assessment's reading). The test is about
   users, not the operator: a significant number of UK users, the UK as a
   target market, or a material risk of significant harm to UK users. The bot
   stores no member location, and this note deliberately does not go looking.
   Whether any member is in the UK is a question for the operator's own
   knowledge of the community.
3. **Does the dashboard change the answer?** Members log in to a web
   dashboard hosted by the operator, not by Discord. Its member-facing surfaces
   (the Help panel's AI ask box, the My Data panel) show a member their own
   content and the bot's answers. They do not show one member's content to
   another. Counsel should confirm that this keeps the dashboard itself out of
   scope.

## 2. What would be in scope if the answers are "yes"

These are the surfaces where content from one member reaches another through
the bot. Each has its own spec.

| Surface | What passes between members | Anonymity | Spec |
|---|---|---|---|
| Confessions | Free text reposted into a channel; anonymous replies | Anonymous to members; the real author is kept in an admin-only audit log | [confessions_spec.md](../specs/confessions_spec.md) |
| Whisper | Anonymous member-to-member messages, with a report inbox | Anonymous to the recipient | [whisper_spec.md](../specs/whisper_spec.md) |
| Anonymous AMA | Anonymous questions posted to whoever is in the hot seat | Anonymous; screened mode routes each question through the host first | `docs/features.md` § AMA |
| Photo Challenge | Member photos posted to prompts in a channel the bot runs | Named | [photo_challenge_spec.md](../specs/photo_challenge_spec.md) |
| Games with free-text answers | Clapback, Hot Takes, Two Truths, Fantasies, Story Builder and others | Varies by game | `docs/features.md` |
| Bios | A member's profile, posted to the bios channel | Named | `bios` module |
| Pen Pals | The bot pairs members and opens a shared room | Named | `pen_pals` module |

Several things apply across all of these:

- Everything happens inside a Discord server, whose members are already
  subject to Discord's terms, age requirements and reporting tools.
- **NSFW content** is gated on Discord's own age-restricted flag, set on the
  channel or its category (`is_age_gated_channel`). The bot adds no age check
  of its own. Whether relying on the platform's age assurance satisfies the
  children's-safety duties, if the bot were in scope, is a question for
  counsel.
- The **no-contact list** already suppresses deliveries between members who
  have blocked each other ([no_contact_spec.md](../specs/no_contact_spec.md)).
- Moderator tools already exist: jail and tickets, the mods' todo board, the
  confession approval queue, and Whisper's report inbox.

## 3. The duties that would attach, mapped to what exists

The headings follow the Act's Part 3 duties for user-to-user services. The
right-hand column describes what the code has today. It does not say whether
that is enough.

| Duty area | Exists today | Gap if in scope |
|---|---|---|
| Illegal content risk assessment | Nothing written | A written assessment per surface in §2 |
| Illegal content safety duties (take-down) | Mods can delete in Discord; confession approval; Whisper reports | No written process tying a report to a take-down |
| Children's access assessment | NSFW relies on Discord's age flag | A written assessment, and possibly a children's risk assessment |
| Content reporting | Whisper report inbox; `/bug`; tickets; Discord's own reporting | No single route for reporting content across every surface |
| Complaints | Tickets | No written complaints procedure |
| Terms of service provisions | The privacy notice in `manual.html` | No terms that state how these duties are met |
| Record-keeping and review | Specs and reviews in `docs/` | No record in the form Ofcom's codes expect |

## 4. Timeline (to verify)

As best recalled from Ofcom's published roadmap:

- illegal harms duties in force from **17 March 2025**;
- children's access assessments due **16 April 2025**;
- the protection-of-children duties in force from **25 July 2025**.

If the bot is in scope, all three are already past, and the first step would be
the written risk assessments. None of the code changes that would follow can be
scoped until then.

## 5. Recommendation

Put §1's three questions to counsel together with the GDPR assessment's §0
threshold question. They turn on the same facts (where the members are, and
whether the bot is a service in its own right), so one opinion can answer both.
Until then, build nothing for the OSA. The mitigations in §2 exist for their
own reasons and do not depend on the answer.
