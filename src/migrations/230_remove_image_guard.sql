-- Migration 230: remove Image Guard's image classifier and everything it
-- recorded (docs/plans/remove-image-guard.md). Billy, 2026-09-15: "can we
-- remove the image inspection feature and go back to making sure posted images
-- are spoilered in the nsfw areas and just call it good. Stuff keeps getting
-- rejected in the sfw areas". The spoiler gate stays, reading only a file's
-- name and its spoiler flag; nothing about a picture is read, scored or kept.
-- It now covers exactly the channels Discord marks NSFW (Billy: "It should
-- only be based on discord setting"), so the admin channel list goes too.
--
-- 1. `nsfw_classifications` — one row per classified image: the model's
--    explicitness score, verdict and top body-part label.
-- 2. `nsfw_detections` — every NudeNet detection on those images: label,
--    confidence and bounding box.
-- 3. `nsfw_blocks` — every image a gate deleted or would have: poster,
--    channel, filename, score.
-- 4. The six classifier dials (thresholds, bare-chest floor, SFW prevention
--    mode and log channel, observe switch) and the SFW exempt channel bucket.
--    Enumerated, never `LIKE 'nsfw_%'`: legacy grant keys share the prefix.
-- 5. The `spoiler_required_channels` bucket — the admin-picked list the gate
--    used to read. Discord's NSFW flag replaced it; kept, it would be a setting
--    nothing enforces.
--
-- Not reversible: no rollup exists. Row counts read from prod (read-only) on
-- 2026-09-15: nsfw_classifications 3,334; nsfw_detections 7,127; nsfw_blocks
-- 106 (40 SFW removals, 66 spoiler-channel removals). Take a backup before the
-- restart that applies this if the rows might be wanted.
--
-- secure_delete overwrites the pages this frees, as in 228. It cannot reach
-- space that was already free before it ran; VACUUM does, by hand, afterwards.
PRAGMA secure_delete = ON;

DROP TABLE IF EXISTS nsfw_detections;
DROP TABLE IF EXISTS nsfw_classifications;
DROP TABLE IF EXISTS nsfw_blocks;

DELETE FROM config WHERE key IN (
    'nsfw_classifier_threshold',
    'nsfw_classifier_sfw_threshold',
    'nsfw_chest_label_floor',
    'nsfw_sfw_prevention_mode',
    'nsfw_sfw_prevention_log_channel_id',
    'nsfw_observe_age_gated'
);

DELETE FROM config_ids WHERE bucket IN (
    'nsfw_prevention_exempt_channels',
    'spoiler_required_channels'
);
