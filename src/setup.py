#!/usr/bin/env python3
"""Create local-finance-tracker's local data directories and initialize or upgrade its SQLite DB."""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_FILE = Path(__file__).with_name("schema.sql")
MIGRATIONS_DIR = Path(__file__).with_name("migrations")
LATEST_MIGRATION = 9


class SetupError(RuntimeError):
    pass


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    if not _table_exists(conn, table):
        return set()
    return {row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')}


def _category_names(conn: sqlite3.Connection) -> set[str]:
    if not _table_exists(conn, "categories"):
        return set()
    return {row[0] for row in conn.execute("SELECT name FROM categories")}


def _infer_applied_migrations(conn: sqlite3.Connection) -> list[int]:
    """Infer historical migrations from schema/data markers for pre-ledger DBs.

    Historical versions 3 and 7 are data migrations, so their effect is inferred
    from canonical category names. Ambiguous or non-local-finance-tracker databases are rejected.
    """
    if not _table_exists(conn, "transactions") or not _table_exists(conn, "categories"):
        raise SetupError("Existing database has no migration history and does not look like a local-finance-tracker database.")
    tx_cols = _columns(conn, "transactions")
    training_cols = _columns(conn, "training_examples")
    names = _category_names(conn)

    # Migration 1 creates training_examples/imports and adds training_lookup.
    if not {"training_examples", "training_imports"} <= {
        row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }:
        raise SetupError(
            "This database predates migration 001. Automatic upgrade is unsafe because migration 001 rebuilds transactions; "
            "back up the database and migrate it with the matching historical local-finance-tracker version first."
        )
    # 002 added the source transaction link.
    highest = 1
    if "source_transaction_id" in training_cols:
        highest = 2

    # 003 merged Eating out into Going out. If a later structural migration
    # exists, sequential history proves 003 ran even if a user recreated that
    # category afterwards.
    later_marker = (
        {"label_origin", "original_category_id", "reviewed_at"} <= training_cols
        or {"classification_confidence", "classification_model"} <= tx_cols
        or _table_exists(conn, "error_review_decisions")
        or ("Education" in names and "Shopping" not in names)
    )
    if "Eating out" not in names or later_marker:
        highest = max(highest, 3)

    # 004 adds review/audit columns as a group.
    if {"label_origin", "original_category_id", "reviewed_at"} <= training_cols:
        highest = max(highest, 4)

    # 005 adds both classifier metadata columns.
    if {"classification_confidence", "classification_model"} <= tx_cols:
        highest = max(highest, 5)

    # 006 adds review decision history.
    if _table_exists(conn, "error_review_decisions"):
        highest = max(highest, 6)

    # 007 replaced Shopping with Education.
    if "Education" in names and "Shopping" not in names:
        highest = max(highest, 7)

    # Migration files are sequential, so a later marker establishes earlier
    # versions even when a data-only migration cannot be inferred from names.
    return list(range(1, highest + 1))


def _ensure_ledger(conn: sqlite3.Connection) -> int:
    if _table_exists(conn, "schema_migrations"):
        rows = conn.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall()
        versions = [int(row[0]) for row in rows]
        if not versions:
            applied = _infer_applied_migrations(conn)
            conn.executemany("INSERT INTO schema_migrations(version) VALUES (?)", [(v,) for v in applied])
            return max(applied, default=0)
        if versions != list(range(1, max(versions, default=0) + 1)):
            raise SetupError("schema_migrations contains gaps; refusing to upgrade this database.")
        if versions and versions[-1] > LATEST_MIGRATION:
            raise SetupError(f"Database schema version {versions[-1]} is newer than this local-finance-tracker code (latest {LATEST_MIGRATION}).")
        return versions[-1] if versions else 0

    applied = _infer_applied_migrations(conn)
    conn.execute("CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)")
    conn.executemany("INSERT INTO schema_migrations(version) VALUES (?)", [(v,) for v in applied])
    return max(applied, default=0)


def _record_migration(conn: sqlite3.Connection, version: int) -> None:
    conn.execute("INSERT INTO schema_migrations(version) VALUES (?)", (version,))


def _restrict_mode(path: Path, mode: int) -> None:
    if os.name == "posix":
        try:
            os.chmod(path, mode)
        except OSError:
            pass


def initialize_database(db_path: Path) -> tuple[bool, list[int]]:
    """Initialize a missing DB or apply pending migrations; never replaces data."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    _restrict_mode(db_path.parent, 0o700)
    is_new = not db_path.exists()
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        if is_new:
            schema = SCHEMA_FILE.read_text(encoding="utf-8")
            conn.executescript("BEGIN IMMEDIATE;\n" + schema + "\nCREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);\nINSERT INTO schema_migrations(version) VALUES (1),(2),(3),(4),(5),(6),(7),(8),(9);\nCOMMIT;")
            _restrict_mode(db_path, 0o600)
            return True, []

        if not _table_exists(conn, "schema_migrations"):
            # Do not create a ledger until inference and migrations are validated.
            conn.execute("BEGIN IMMEDIATE")
            current = _ensure_ledger(conn)
            conn.commit()
        else:
            current = _ensure_ledger(conn)

        applied_now: list[int] = []
        for version in range(current + 1, LATEST_MIGRATION + 1):
            matches = list(MIGRATIONS_DIR.glob(f"{version:03d}_*.sql"))
            if len(matches) != 1:
                raise SetupError(f"Expected one SQL file for migration {version:03d}; found {len(matches)}.")
            sql = matches[0].read_text(encoding="utf-8")
            # The first and third/final migrations manage their own transaction.
            # Wrap other scripts so a failure cannot leave a half-applied version.
            if version in (1, 3, 7):
                conn.executescript(sql)
                conn.execute("BEGIN IMMEDIATE")
                _record_migration(conn, version)
                conn.commit()
            else:
                conn.executescript("BEGIN IMMEDIATE;\n" + sql + f"\nINSERT INTO schema_migrations(version) VALUES ({version});\nCOMMIT;")
            applied_now.append(version)
        return False, applied_now
    except (sqlite3.Error, OSError, SetupError) as exc:
        try:
            conn.rollback()
        except sqlite3.Error:
            pass
        if isinstance(exc, SetupError):
            raise
        raise SetupError(f"Could not initialize or upgrade database {db_path}: {exc}") from exc
    finally:
        conn.close()
        if db_path.exists():
            _restrict_mode(db_path, 0o600)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Create local-finance-tracker's local folders and initialize or safely upgrade its SQLite database."
    )
    parser.add_argument(
        "--project-dir", type=Path, default=PROJECT_ROOT,
        help=f"local-finance-tracker project directory (default: {PROJECT_ROOT})",
    )
    parser.add_argument(
        "--db", type=Path,
        help="database path (default: <project-dir>/data/local-finance-tracker.sqlite)",
    )
    args = parser.parse_args(argv)
    root = args.project_dir.expanduser().resolve()
    db_path = args.db.expanduser().resolve() if args.db else root / "data" / "local-finance-tracker.sqlite"
    try:
        for dirname in ("data", "inbox", "archived", "rejected"):
            folder = root / dirname
            folder.mkdir(parents=True, exist_ok=True)
            _restrict_mode(folder, 0o700)
        created, migrations = initialize_database(db_path)
    except (OSError, SetupError) as exc:
        print(f"Setup failed: {exc}", file=sys.stderr)
        return 1
    if created:
        print(f"Initialized new database: {db_path} (schema version {LATEST_MIGRATION})")
    elif migrations:
        print(f"Upgraded database: {db_path} (applied migrations {', '.join(f'{v:03d}' for v in migrations)})")
    else:
        print(f"Database is ready: {db_path} (no migration needed)")
    print(f"Data directories are ready under: {root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
