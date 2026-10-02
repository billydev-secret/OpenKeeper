# Batched DMs (`dm_batch`)

**Reference**: matches the code.

A member notification that can wait is queued instead of sent, and every
notification sharing a **batch key** (the same recipient, feature (`kind`) and subject
(`group_key`, e.g. a thread)) goes out together as one DM once the oldest has
waited **ten minutes** (`dm.batch.logic.WINDOW_SECONDS`). All non-urgent DMs are batched; the rollout order is in
`docs/plans/dm-batching.md`.

The window opens on the first notification and does not slide: a burst gets
one DM ten minutes after it started, however long it runs, and anything later
opens the next window.

## Who uses it

| Kind | Feature | `group_key` | Since |
|---|---|---|---|
| `needle_reply` | Auto-Thread reply DMs (`needle_spec.md` § Reply DMs) | thread id | 2026-09-28 |
| `confession_reply` | Confession reply DMs, named and anonymous (`confessions_spec.md`). Anonymous rows carry `actor_id` 0 | root confession message id | 2026-09-28 |
| `ama_answer` | Anonymous AMA "your question got a reply" (`games_system_spec.md` § AMA). Actor is the answerer | AMA channel id | 2026-09-28 |
| `whisper_guess` | Whisper's "they guessed X" notes to the sender (`whisper_spec.md`). `actor_id` is the member the guess **named**, registered with `recheck_actor=False` | whisper id | 2026-09-28 |
| `econ_notice` | Economy notes (`economy_spec.md` § 5 Transfers and the bounty no-contact paragraph): "X sent you N" (`finalize_pay`, memo included), "X gifted you P" (`finalize_gift`), and a cancelled bounty's refund note. One line per row, the actor's name prepended at send unless the row is `named` 0 (the refund note, whose actor is the poster, kept only for the no-contact re-check). Delivered through `notify_member`, keeping mute prefs and the bank-channel fallback; capped at 12 lines | 0 (one batch per recipient) | 2026-09-28 |

## Joining

1. Where the feature sends the DM, call `dm.batch.store.enqueue(conn, kind=…,
   guild_id=…, user_id=<recipient>, group_key=…, actor_id=<member whose action
   it reports, or 0>, ref_id=<message it is about, or 0>, payload={…})` after
   its own gates pass. The payload is small JSON: ids, never message content.
2. Register one handler for the kind in `cog_load` (`service.register_handler`)
   and unregister it in `cog_unload`. The handler receives
   `(bot, guild, recipient_member, rows)`, oldest row first, re-checks the
   feature's own switches (they may have changed during the window), and sends
   one DM through `send_branded_dm`.

## What every kind gets

- **No-contact, again at send time.** `service.take_due` drops every row whose
  actor is now on the recipient's no-contact list; a batch left empty is not
  sent. Nothing tells either side (`no_contact_spec.md`). A kind whose actor
  is a member the DM *names* rather than one who acted registers with
  `recheck_actor=False` and applies its own rule in its handler (Whisper
  degrades the name to "someone").
- **A recipient who left the server gets nothing.**
- **Retraction.** `dm_batch_cog` deletes any waiting row whose `ref_id` message
  is deleted (single and bulk deletes), so a deleted message is never
  announced, the same principle as a deleted ping retracting itself.
- **Take a Break and closed DMs**, through `send_branded_dm`.

## Delivery

`dm_batch_cog` runs `service.flush_due` every 30 seconds. A batch is
**claimed** (its rows deleted) before it is handed to the handler, so it is
sent at most once: a crash between claim and send loses that DM rather than
repeating it. Rows survive a restart; overdue batches go out on the first pass
after it. A batch whose kind has no registered handler is dropped with a log
line. One handler raising does not stop the rest.

## Stored data

`dm_batch_pending` (migration 243): `kind`, `guild_id`, `user_id` (recipient),
`group_key`, `actor_id`, `ref_id`, `payload`, `created_at`. Per-user data, row
in `docs/data_register.md`: kept until the batch is sent, deleted with the
`ref_id` message or (Needle) the thread, and purged by `purge_user_data` with
the member on either side.

## Not batched

A DM whose meaning depends on arriving now (an interactive challenge, a
receipt for the member's own action, a moderation notice, a timed reminder)
is never queued. Which existing DMs fall on which side is stage 2 of the plan.
