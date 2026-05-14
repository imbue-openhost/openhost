-- v8: drop the v1 ``service_providers`` and ``permissions`` tables.
--
-- The v1 service interface and v1 permissions code were removed in
-- favour of v2 (service_providers_v2, permissions_v2). Both v1 tables
-- are now dead weight on existing zones.

DROP TABLE IF EXISTS service_providers;
DROP TABLE IF EXISTS permissions;
