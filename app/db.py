"""SQLite access: connections, pragmas, and the migration runner.

Design notes that matter later:
  * Every connection is opened per call. SQLite connections are cheap, WAL mode is a
    property of the file, and this removes any question about which thread owns a
    connection (the app is single-user, but uvicorn still has a thread pool).
  * `PRAGMA foreign_keys` is per-connection and therefore reapplied every time.
  * Migrations are numbered `NNNN_name.sql`, applied once each, recorded in
    `PRAGMA user_version`. They are never edited after being applied — a change is a
    new migration. The test suite enforces the numbering.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("kith.db")

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"

# STRICT tables and the RETURNING-free subset we rely on both need >= 3.37.
MIN_SQLITE_VERSION = (3, 37, 0)

_PRAGMAS = (
    "PRAGMA journal_mode=WAL",
    "PRAGMA foreign_keys=ON",
    "PRAGMA busy_timeout=5000",
    "PRAGMA synchronous=NORMAL",
)


def utcnow() -> str:
    """Timestamps are stored as UTC ISO-8601 strings, always."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def check_sqlite_capabilities() -> None:
    if sqlite3.sqlite_version_info < MIN_SQLITE_VERSION:
        raise RuntimeError(
            f"SQLite {sqlite3.sqlite_version} is too old; Kith needs "
            f"{'.'.join(map(str, MIN_SQLITE_VERSION))}+ for STRICT tables."
        )


@contextmanager
def connect(db_path: Path) -> Iterator[sqlite3.Connection]:
    """Open a connection with Kith's pragmas applied."""
    conn = sqlite3.connect(str(db_path), timeout=5.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        for pragma in _PRAGMAS:
            conn.execute(pragma)
        yield conn
    finally:
        conn.close()


def current_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def discover_migrations(directory: Path | None = None) -> list[tuple[int, Path]]:
    """Return [(version, path)] sorted, failing loudly on a malformed filename."""
    directory = directory or MIGRATIONS_DIR
    found: list[tuple[int, Path]] = []
    for path in sorted(directory.glob("*.sql")):
        prefix = path.name.split("_", 1)[0]
        if not prefix.isdigit():
            raise RuntimeError(f"migration {path.name!r} must start with a number, e.g. 0002_photos.sql")
        found.append((int(prefix), path))
    versions = [v for v, _ in found]
    if len(set(versions)) != len(versions):
        raise RuntimeError(f"duplicate migration numbers: {versions}")
    return found


def _statements(sql: str) -> Iterator[str]:
    """Split a migration script into complete SQL statements.

    `executescript()` is deliberately NOT used, even though it would be shorter: it
    issues an implicit COMMIT before running, which silently destroys the atomicity
    guarantee `migrate()` promises. `sqlite3.complete_statement` does the parsing
    properly (it understands strings, comments and trigger bodies).
    """
    buffer = ""
    for line in sql.splitlines(keepends=True):
        buffer += line
        if sqlite3.complete_statement(buffer):
            statement = buffer.strip()
            if statement:
                yield statement
            buffer = ""
    remainder = buffer.strip()
    if remainder:  # unterminated final statement: let SQLite produce the error
        yield remainder


def migrate(conn: sqlite3.Connection, directory: Path | None = None) -> list[str]:
    """Apply every pending migration. Returns the names applied (empty = up to date).

    Each migration is applied inside its own transaction together with its version
    bump, so a failure halfway through leaves the database at the last good version.
    (SQLite DDL is transactional, which is what makes this work.) Migrations must
    therefore not contain statements that cannot run inside a transaction — no
    `VACUUM`, no `PRAGMA journal_mode`.
    """
    applied: list[str] = []
    for version, path in discover_migrations(directory):
        if version <= current_version(conn):
            continue
        log.info("applying migration %04d (%s)", version, path.name)
        sql = path.read_text(encoding="utf-8")
        try:
            conn.execute("BEGIN")
            for statement in _statements(sql):
                conn.execute(statement)
            # PRAGMA values cannot be bound; `version` is validated as an int above.
            conn.execute(f"PRAGMA user_version={version:d}")
            conn.execute("COMMIT")
        except sqlite3.Error:
            if conn.in_transaction:  # SQLite may already have aborted it
                conn.execute("ROLLBACK")
            log.exception(
                "migration %s failed; database left at version %d",
                path.name,
                current_version(conn),
            )
            raise
        applied.append(path.name)
    return applied


def probe_write(conn: sqlite3.Connection) -> bool:
    """Actually try to write, then undo it.

    A writable-looking directory is not the same as a writable database: this catches
    read-only mounts, a read-only WAL, and `:Z`/ownership mistakes at health-check
    time instead of at the first saved answer.
    """
    try:
        conn.execute("SAVEPOINT kith_health")
        conn.execute(
            "INSERT OR REPLACE INTO app_meta (key, value) VALUES ('health_probe', ?)",
            (utcnow(),),
        )
        conn.execute("ROLLBACK TO kith_health")
        conn.execute("RELEASE kith_health")
        return True
    except sqlite3.Error:
        log.warning("write probe failed", exc_info=True)
        return False


def count(conn: sqlite3.Connection, table: str) -> int:
    """Row count for a table name from our own schema (not user input)."""
    return int(conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0])


def pragma_report(conn: sqlite3.Connection) -> dict[str, str]:
    """Small diagnostics blob for the dashboard."""
    return {
        "journal_mode": str(conn.execute("PRAGMA journal_mode").fetchone()[0]),
        "foreign_keys": str(conn.execute("PRAGMA foreign_keys").fetchone()[0]),
        "integrity": str(conn.execute("PRAGMA quick_check").fetchone()[0]),
    }
