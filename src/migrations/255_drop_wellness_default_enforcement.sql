-- Migration 255: the wellness "Default Enforcement" setting is removed.
--
-- Owner decision, 2026-10-01. The value was saved per server and applied to no
-- one: opt-in always used the module's own DEFAULT_ENFORCEMENT, and members pick
-- their level themselves. The dashboard control, the routes and the service
-- field go in the same commit. No index references the column, so DROP COLUMN
-- is legal. The service's in-code CREATE TABLE still names it, as it does for
-- crisis_resource_url (migration 189), so a database first built by the web
-- server still has the column for this ALTER to drop.
--
-- Live effect on the next restart: none. No member's setting is touched.

ALTER TABLE wellness_config DROP COLUMN default_enforcement;
