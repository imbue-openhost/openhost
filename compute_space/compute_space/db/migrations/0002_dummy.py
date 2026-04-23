"""
No-op migration.

Exists solely to give the snapshot harness a real migration pair
(0001 -> 0002) to exercise.  When a genuine schema change comes along,
promote the next integer (0003_...) for it and leave this one alone so
existing snapshots continue to chain through history unchanged.
"""

from yoyo import step

steps: list[step] = []
