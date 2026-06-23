-- v11: rename apps.cpu_millicores -> apps.cpu_cores and switch the unit
-- from millicores (integer, 1000 = 1 core) to fractional CPU cores (real).
--
-- Existing integer millicore values are divided by 1000 so the effective
-- allocation is preserved (e.g. 100 -> 0.1, 500 -> 0.5, 1000 -> 1.0).
-- SQLite can't change a column's type in place, so we add the new REAL
-- column, copy the converted values across, then drop the old one.

ALTER TABLE apps ADD COLUMN cpu_cores REAL NOT NULL DEFAULT 0.1;
UPDATE apps SET cpu_cores = cpu_millicores / 1000.0;
ALTER TABLE apps DROP COLUMN cpu_millicores;
