from __future__ import annotations

import os


def schema_path() -> str:
    return os.path.join(os.path.dirname(__file__), "schema.sql")
