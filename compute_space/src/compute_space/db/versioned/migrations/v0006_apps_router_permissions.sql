-- v6: add per-app router-permission columns.
--
-- Apps may request privileged grants on the router itself via a new
-- [permissions] section in openhost.toml (separate from the per-service
-- [services] / [[permissions_v2]] grants which mediate cross-app
-- access).  The owner approves each grant explicitly at install time;
-- approval state lives directly on the apps row so it's part of the
-- same transaction as the install.
--
-- Boolean columns rather than a child table because the cardinality is
-- small (one approval bit per permission per app) and fixed at install
-- time.  Adding a permission is two changes: a new column here + a
-- migration; old apps end up with a 0 default which means "not granted".
--
-- The runtime token issuance + API gating that *enforce* these grants
-- ship in a follow-up PR; this migration just lays the persistence so
-- the manifest contract can be merged independently.

ALTER TABLE apps ADD COLUMN deploy_apps_permission INTEGER NOT NULL DEFAULT 0;
