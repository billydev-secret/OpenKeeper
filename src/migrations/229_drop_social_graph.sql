-- Migration 229: drop the member-to-member edge stores (remove-social-graph,
-- docs/plans/remove-social-graph.md stage 3). Billy's decisions, 2026-09-13:
-- "remove all of the social graph functions", then "drop them" for voice follow
-- and "We can drop it and those features" for reaction_log. 227 is
-- drop-rules-watch's (merged), 228 remove-message-storage's.
--
-- 1. `user_interactions` / `user_interactions_log` — who replied to or
--    @mentioned whom, as pair weights and a timestamped edge log. Their
--    readers (Connection Graph, Interactions, One-Sided Attention, the Social
--    Graph Health tile, Home Social Butterflies, Moderator Engagement's reach
--    figures, Greeting Watch's acknowledgment lookup, the drop-off profiles)
--    were removed or re-sourced in stages 1-2, and ingest no longer writes
--    them. Rules Watch's scorer also read `user_interactions`; the
--    drop-rules-watch branch deletes it and MUST merge before this one.
--
-- 2. `voice_follow` / `voice_follow_log` — who followed whom into a voice
--    channel. One-Sided Attention was the only reader; capture has stopped.
--
-- 3. `reaction_log` — who reacted to whose message. Its figures were cut:
--    Contributors' reaction inputs, the Health Weighted Gini, Moderator
--    Engagement's reactions/engagement rate, Ping Response's reaction turnout,
--    mod presence-by-hour's reactions, and the Pools `reactions`/`reactors`
--    metrics. `message_reactions` (per-message emoji counts, no member id)
--    is untouched.
--
-- Not reversible: the rows are gone and no rollup exists. Row counts read
-- from prod (read-only) on 2026-09-13: user_interactions_log 312,316;
-- user_interactions 15,169; reaction_log 239,457; voice_follow 610;
-- voice_follow_log 1,473. `docs/data_register.md` records the outcome.
-- `000_init.sql` and later migrations still create some of these on a fresh
-- database; this removes them again, which is cheaper than editing history.

DROP TABLE IF EXISTS user_interactions_log;

DROP TABLE IF EXISTS user_interactions;

DROP TABLE IF EXISTS voice_follow_log;

DROP TABLE IF EXISTS voice_follow;

DROP TABLE IF EXISTS reaction_log;
