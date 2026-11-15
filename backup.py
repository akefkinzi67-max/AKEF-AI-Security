"""Create a consistent backup or copy a verified backup to a new database path."""
import argparse
from contextlib import closing
from pathlib import Path
import sqlite3


def copy_database(source, target):
    with closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)) as original:
        if original.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("Source failed SQLite integrity check.")
        if original.execute("PRAGMA user_version").fetchone()[0] != 1:
            raise RuntimeError("Source schema version is unsupported.")
        target.touch(exist_ok=False)
        try:
            with closing(sqlite3.connect(target)) as destination:
                original.backup(destination)
                if destination.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise RuntimeError("Copy failed SQLite integrity check.")
        except Exception:
            target.unlink()
            raise


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("target", type=Path)
    arguments = parser.parse_args()
    copy_database(arguments.source, arguments.target)
