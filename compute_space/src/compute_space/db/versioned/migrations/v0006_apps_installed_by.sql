-- v6: add ``apps.installed_by`` for the installer v2 service.
--
-- When an app is installed via the ``installer`` v2 service rather
-- than owner-initiated UI/CLI, the requesting consumer app's name is
-- recorded here so the installer service can scope status/logs queries
-- to the installs each caller initiated.  NULL for owner-initiated
-- installs (the existing /api/add_app path); set to the consumer app
-- name for installer-service-initiated installs.

ALTER TABLE apps ADD COLUMN installed_by TEXT;
