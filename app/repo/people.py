"""People: CRUD, search, and the counts the dashboard shows."""

from __future__ import annotations

import re
import sqlite3

from app import i18n
from app.db import utcnow

# The columns the cabinet form is allowed to write. Explicit on purpose: a stray form
# field must never be able to reach `id`, `created_at`, or (accidentally) `status`.
EDITABLE = (
    "display_name",
    "given_name",
    "family_name",
    "nickname",
    "pronunciation",
    "mnemonic",
    "feature_note",
    "where_met",
    "met_on",
    "notes",
    "status",
)

ALLOWED_STATUS = ("active", "archived", "paused")

_COLUMNS = (
    "id, display_name, given_name, family_name, nickname, pronunciation, mnemonic, "
    "feature_note, where_met, met_on, notes, status, paused_until, created_at, updated_at"
)

# What you are asked to produce when this face comes up. An explicit "ask for" wins; otherwise
# the given name is used, so typing a first name is enough and nothing has to be repeated.
# Written as SQL, not copied at write time: correct a first name and what you are asked changes
# with it, with no second copy to fall out of step — and the lists, the trainer, the search
# index and the review log all share this one definition.
_RECALL_NAME = "COALESCE(NULLIF(TRIM({alias}.display_name), ''), {alias}.given_name, '')"


def recall_name(alias: str = "p") -> str:
    """That expression for a table aliased `alias` — the trainer joins person twice."""
    return _RECALL_NAME.format(alias=alias)


RECALL_NAME = recall_name()

_SELECT = ", ".join(
    [f"{RECALL_NAME} AS display_name"]
    + [
        f"p.{column.strip()}"
        for column in _COLUMNS.split(",")
        if column.strip() != "display_name"
    ]
)

# The picture a list row should show, chosen in SQL so 200 people cost one query.
_PHOTO_ID = (
    "(SELECT f.id FROM photo f WHERE f.person_id = p.id "
    " ORDER BY f.is_primary DESC, f.id LIMIT 1) AS photo_id"
)

_LIST_SELECT = f"{_SELECT}, {_PHOTO_ID}"

# The same subquery, for other modules that want the thumbnail a list row would show.
PHOTO_ID_SQL = _PHOTO_ID

_WORD = re.compile(r"[^\w]+", re.UNICODE)


class InvalidPerson(ValueError):
    """Validation failed. The message is shown to the user as-is."""


def fts_query(raw: str) -> str | None:
    """Turn free text into a safe FTS5 prefix query.

    Every token is quoted and star-suffixed, which is what makes typing half a name
    work ("ku" finds "Kumar"), and what stops FTS5 operator characters in user input
    from being parsed as syntax.
    """
    tokens = [_WORD.sub("", token) for token in raw.split()]
    tokens = [token for token in tokens if token]
    if not tokens:
        return None
    return " ".join(f'"{token}"*' for token in tokens)


def get(conn: sqlite3.Connection, person_id: int) -> dict | None:
    row = conn.execute(
        f"SELECT {_SELECT}, {_PHOTO_ID} FROM person p WHERE p.id = ?", (person_id,)
    ).fetchone()
    return dict(row) if row else None


def listing(
    conn: sqlite3.Connection,
    *,
    q: str | None = None,
    group_id: int | None = None,
    tag: str | None = None,
    status: str | None = None,
    limit: int = 500,
    offset: int = 0,
) -> list[dict]:
    """Filtered people list, alphabetical.

    Ordering is deliberately alphabetical rather than FTS relevance-ranked: with a few
    hundred names, a stable order you can scan beats a clever one that reshuffles.
    """
    filters: list[str] = []
    filter_params: list[object] = []

    if status:
        filters.append("p.status = ?")
        filter_params.append(status)
    if group_id:
        filters.append(
            "EXISTS (SELECT 1 FROM grp_member m WHERE m.person_id = p.id AND m.grp_id = ?)"
        )
        filter_params.append(group_id)
    if tag:
        filters.append(
            "EXISTS (SELECT 1 FROM person_tag pt JOIN tag t ON t.id = pt.tag_id "
            "WHERE pt.person_id = p.id AND t.name = ?)"
        )
        filter_params.append(tag)

    fts_where = "p.id IN (SELECT rowid FROM person_fts WHERE person_fts MATCH ?)"
    query = fts_query(q or "")
    # FTS matches word prefixes, so a mid-word fragment ("ber" for "Weber") finds nothing.
    # That is what the LIKE fallback below is for — and why both paths go through one
    # builder: the fallback has to carry the group/status/tag filters too, or searching
    # inside a group quietly lists the whole cabinet.
    like_where = (
        f"{RECALL_NAME} LIKE ? COLLATE NOCASE "
        "OR IFNULL(p.nickname, '') LIKE ? COLLATE NOCASE "
        "OR IFNULL(p.family_name, '') LIKE ? COLLATE NOCASE"
    )

    def _run(text_clause: str | None, text_params: tuple[object, ...]) -> list[dict]:
        clauses = list(filters)
        if text_clause:
            # Parenthesised: the LIKE fallback is a chain of ORs, and without brackets it
            # would escape past the ANDed filters standing in front of it.
            clauses.append(f"({text_clause})")
        sql = f"SELECT {_LIST_SELECT} FROM person p"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += f" ORDER BY {RECALL_NAME} COLLATE NOCASE, p.id LIMIT ? OFFSET ?"
        return [
            dict(row)
            for row in conn.execute(sql, (*filter_params, *text_params, limit, offset))
        ]

    if not query:
        return _run(None, ())
    rows = _run(fts_where, (query,))
    if not rows:
        like = f"%{(q or '').strip()}%"
        rows = _run(like_where, (like, like, like))
    return rows


def create(conn: sqlite3.Connection, **fields: object) -> int:
    values = _validated(fields)
    now = utcnow()
    columns = ", ".join(values)
    placeholders = ", ".join("?" for _ in values)
    cursor = conn.execute(
        f"INSERT INTO person ({columns}, created_at, updated_at) VALUES ({placeholders}, ?, ?)",
        (*values.values(), now, now),
    )
    return int(cursor.lastrowid or 0)


def update(conn: sqlite3.Connection, person_id: int, **fields: object) -> bool:
    values = _validated(fields, partial=True)
    if not values:
        return False
    assignments = ", ".join(f"{name} = ?" for name in values)
    cursor = conn.execute(
        f"UPDATE person SET {assignments}, updated_at = ? WHERE id = ?",
        (*values.values(), utcnow(), person_id),
    )
    return cursor.rowcount > 0


def delete(conn: sqlite3.Connection, person_id: int) -> bool:
    """Photos, group memberships, tags and (later) review cards cascade away."""
    return conn.execute("DELETE FROM person WHERE id = ?", (person_id,)).rowcount > 0


def _validated(fields: dict[str, object], *, partial: bool = False) -> dict[str, object]:
    unknown = set(fields) - set(EDITABLE)
    if unknown:
        raise InvalidPerson(i18n.t("person.error.unknown_field", fields=", ".join(sorted(unknown))))

    values: dict[str, object] = {}
    for name, raw in fields.items():
        if raw is None:
            values[name] = None
            continue
        text = str(raw).strip()
        if name == "display_name":
            # "Ask for": an optional override for what you are asked to produce. Empty is the
            # normal case and means "just the given name" — see RECALL_NAME.
            if len(text) > 120:
                raise InvalidPerson(i18n.t("person.error.name_too_long"))
            values[name] = text
        elif name == "status":
            if not text:
                # A form that does not carry a status means "leave it alone" on an edit,
                # and "active" on a new person — not "an invalid status".
                if partial:
                    continue
                values[name] = "active"
                continue
            if text not in ALLOWED_STATUS:
                raise InvalidPerson(i18n.t("person.error.bad_status", statuses=", ".join(ALLOWED_STATUS)))
            values[name] = text
        else:
            values[name] = text or None

    if not partial:
        # A new person needs something to ask for: an explicit override, or a first name.
        if not (values.get("display_name") or values.get("given_name")):
            raise InvalidPerson(i18n.t("person.error.first_name_required"))
        # The column is NOT NULL and doubles as the override slot, so an override that was
        # not supplied is stored empty; the given name is what gets asked for.
        if values.get("display_name") is None:
            values["display_name"] = ""
    return values


def sheet_rows(conn: sqlite3.Connection, *, group_id: int | None = None) -> list[dict]:
    """Everyone who belongs on a printed sheet, with a photo, in one query.

    The name this prints is the one the trainer *asks* with — what you want to say out loud,
    which is not always the legal name. A person with no photo still gets a row: a missing
    face is precisely what you would want to notice in the hour before the party, not
    something the query should quietly hide.
    """
    sql = f"""
        SELECT p.id,
               {recall_name()} AS asked_name,
               p.family_name,
               p.mnemonic,
               (SELECT ph.id FROM photo ph WHERE ph.person_id = p.id
                 ORDER BY ph.is_primary DESC, ph.id LIMIT 1) AS photo_id
          FROM person p
         WHERE p.status = 'active'
    """
    params: list[object] = []
    if group_id:
        sql += (
            " AND EXISTS (SELECT 1 FROM grp_member m"
            " WHERE m.person_id = p.id AND m.grp_id = ?)"
        )
        params.append(group_id)
    sql += " ORDER BY asked_name COLLATE NOCASE"
    return [dict(row) for row in conn.execute(sql, params)]


def stats(conn: sqlite3.Connection) -> dict:
    """Counts for the dashboard. One query per number, which is honest at this size."""
    counts = dict(
        conn.execute(
            """
            SELECT
              (SELECT count(*) FROM person)                           AS people,
              (SELECT count(*) FROM person WHERE status = 'active')   AS active,
              (SELECT count(*) FROM person WHERE status = 'paused')   AS paused,
              (SELECT count(*) FROM person WHERE status = 'archived') AS archived,
              (SELECT count(*) FROM photo)                            AS photos,
              (SELECT IFNULL(sum(bytes), 0) FROM photo)               AS photo_bytes,
              (SELECT count(*) FROM grp)                              AS groups,
              (SELECT count(*) FROM tag)                              AS tags
            """
        ).fetchone()
    )
    # People you cannot train on yet: no photo means no prompt.
    counts["without_photos"] = int(
        conn.execute(
            "SELECT count(*) FROM person p "
            "WHERE NOT EXISTS (SELECT 1 FROM photo f WHERE f.person_id = p.id)"
        ).fetchone()[0]
    )
    counts["trainable"] = counts["active"] - counts["without_photos"]
    return counts
