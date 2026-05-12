import os


def _schema_path() -> str:
    return os.path.join(os.path.dirname(__file__), "schema.sql")
