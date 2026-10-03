"""Your data, out and back in again.

The whole cabinet — people, photos, every review — is one SQLite file, so a backup is not a
feature bolted onto the side of the app: it *is* the app, copied correctly. The snapshot comes
from `VACUUM INTO` and never from copying the file behind SQLite's back, which is the
difference between a backup and a file that merely looks like one.
"""

from __future__ import annotations

import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, RedirectResponse, Response
from starlette.background import BackgroundTask
from starlette.datastructures import UploadFile

from app import config, db

# What a Kith database must contain before we will let it replace a working one.
REQUIRED_TABLES = {"person", "photo", "card", "session", "session_item", "review"}
CONFIRM_WORD = "REPLACE"


def _stamp() -> str:
    """A filename that sorts, in UTC like every other timestamp Kith stores."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d-%H%M")


# An uploaded database is streamed to disk, so the ceiling has to be enforced while it
# arrives rather than after: a 4 GB file must not be written before being refused.
MAX_IMPORT = 1 << 30


def _remove(path: Path) -> None:
    path.unlink(missing_ok=True)


def _reject(path: Path) -> str | None:
    """Why this file must not replace the cabinet, or None if it may.

    The answer is a fixed code and nothing else — no text from the file itself ever travels
    back, because it lands in a query string and is rendered as one of a known few sentences.
    """
    probe: sqlite3.Connection | None = None
    try:
        probe = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        integrity = probe.execute("PRAGMA integrity_check").fetchone()
        if not integrity or integrity[0] != "ok":
            return "corrupt"
        version = int(probe.execute("PRAGMA user_version").fetchone()[0])
        if not 1 <= version <= config.SCHEMA_VERSION:
            return "version"
        tables = {
            row[0]
            for row in probe.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        if not REQUIRED_TABLES <= tables:
            return "tables"
    except sqlite3.Error:
        return "notsqlite"
    finally:
        if probe is not None:
            probe.close()
    return None


def build_router() -> APIRouter:
    router = APIRouter()

    # ── out ───────────────────────────────────────────────────────────────────
    @router.get("/api/backup")
    def backup(request: Request) -> Response:
        """A consistent snapshot of the whole cabinet, streamed as a download.

        `VACUUM INTO` reads through the same locking the app itself uses, so what lands is
        complete and self-consistent even mid-session — which a filesystem copy of a live
        WAL database is not.
        """
        settings = request.app.state.settings
        target = Path(settings.data_dir) / f"backup-{uuid.uuid4().hex}.sqlite"
        with db.connect(settings.db_path) as conn:
            conn.execute("VACUUM INTO ?", (str(target),))
        return FileResponse(
            target,
            media_type="application/vnd.sqlite3",
            filename=f"kith-{_stamp()}.sqlite",
            background=BackgroundTask(_remove, target),
        )

    # ── back in ───────────────────────────────────────────────────────────────
    @router.post("/api/import")
    async def import_db(request: Request) -> Response:
        """Replace the cabinet with an uploaded database — but never by accident.

        Three things must be true first: the file has to *be* a Kith database (integrity,
        schema version, tables), the word has to have been typed, and the database being
        replaced gets a snapshot of its own. The swap is atomic, so there is never a moment
        when that path holds half of each file.
        """
        settings = request.app.state.settings
        form = await request.form()
        upload = form.get("database")
        typed = str(form.get("confirm", "")).strip()
        if typed.upper() != CONFIRM_WORD:
            return RedirectResponse("/stats?data=confirm", status_code=303)
        if not isinstance(upload, UploadFile):
            return RedirectResponse("/stats?data=nofile", status_code=303)

        incoming = Path(settings.data_dir) / f"import-{uuid.uuid4().hex}.sqlite"
        size = 0
        with open(incoming, "wb") as handle:
            while chunk := await upload.read(1 << 20):
                size += len(chunk)
                if size > MAX_IMPORT:
                    incoming.unlink(missing_ok=True)
                    return RedirectResponse("/stats?data=huge", status_code=303)
                handle.write(chunk)

        reason = _reject(incoming)
        if reason:
            incoming.unlink(missing_ok=True)
            return RedirectResponse(f"/stats?data={reason}", status_code=303)

        keep = Path(settings.data_dir) / f"replaced-{_stamp()}.sqlite"
        with db.connect(settings.db_path) as conn:
            conn.execute("VACUUM INTO ?", (str(keep),))
        # The old write-ahead log belongs to the old database. Left in place, SQLite would
        # replay it into the new one as though it were the new one's unfinished history.
        for suffix in ("-wal", "-shm"):
            Path(str(settings.db_path) + suffix).unlink(missing_ok=True)
        os.replace(incoming, settings.db_path)

        # An older file is migrated forward here exactly as it would be at startup, so an
        # import can never leave behind a database the running code cannot read.
        with db.connect(settings.db_path) as conn:
            db.migrate(conn)
        return RedirectResponse("/stats?data=imported", status_code=303)

    return router
