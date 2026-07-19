-- v12: storage guard settings table.
--
-- The storage guard (stops running apps when free disk drops below a
-- threshold) used to be configurable only via the ``storage_min_free_mb``
-- key in the router config (env/TOML), read once at startup. This made it
-- effectively unreachable for instance owners: no UI, and a restart was
-- required to change it.
--
-- This table makes the guard runtime-configurable from the System page:
-- an owner can enable/disable it and set the minimum-free-MB threshold
-- without restarting the router. It is a single-row table (one guard
-- config per zone).
--
-- Seeding: the row is created disabled with min_free_mb = 0. On first
-- boot the application seeds it from the legacy ``storage_min_free_mb``
-- config value if that was set (> 0), so existing operators who relied on
-- the config keep their behavior.

CREATE TABLE IF NOT EXISTS storage_settings (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    enabled INTEGER NOT NULL DEFAULT 0 CHECK (enabled IN (0, 1)),
    min_free_mb INTEGER NOT NULL DEFAULT 0 CHECK (min_free_mb >= 0)
);

INSERT OR IGNORE INTO storage_settings (id, enabled, min_free_mb) VALUES (1, 0, 0);
