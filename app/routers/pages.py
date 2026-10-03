"""The cabinet: people, photos, groups — server-rendered HTML, no JavaScript framework.

Conventions in this file:
  * A connection is opened per request (`with session(request)`) and used entirely
    inside one thread, which is why routes are plain `def` and only the multipart ones
    are `async def` (they must await the upload body).
  * Validation failures re-render the form with HTTP 400 and the submitted values still
    in it. Failures that happen *after* a redirect (uploads) travel back as short codes
    in the query string and are mapped to fixed sentences — nothing a user typed is ever
    reflected back into a page.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
# NB: the class the multipart parser actually instantiates. FastAPI's `UploadFile`
# *subclasses* this one, so `isinstance(parsed, fastapi.UploadFile)` is False for every
# upload — checking against Starlette's class is what makes uploads arrive at all.
from starlette.datastructures import UploadFile

from app import db, images
from app.repo import groups as groups_repo
from app.repo import people as people_repo
from app.repo import photos as photos_repo
from app.repo import stats as stats_repo

UPLOAD_ERRORS = {
    "not_an_image": "That file is not an image Kith can read.",
    "too_large": "That image is too big — 25 MB is the limit.",
    "empty": "The upload arrived empty. Try again.",
    "duplicate": "That exact photo is already in the cabinet.",
    "generic": "The upload did not work. Try again.",
}

# Redirects carry short codes, never prose, so an attacker cannot make the app print
# arbitrary text by crafting a URL.
OK_MESSAGES = {
    "saved": "Changes saved.",
    "deleted": "Removed from the cabinet.",
    "primary": "Primary photo updated.",
    "photo_deleted": "Photo deleted.",
    "photos_added": "Photo added.",
}


def _optional_int(value: str | None) -> int | None:
    """An empty filter field means "no filter", not a malformed request.

    The toolbar form submits *every* field on one go, so an unselected group arrives as
    `group=`. Declared as `int | None`, FastAPI answered 422 to every search made from the
    UI while hand-written URLs without `group=` worked — see the regression test that
    submits the whole form. Unparseable values are treated as "no filter" too: these are
    optional refinements of a list, not commands.
    """
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def ok_message(code: str | None) -> str | None:
    if not code:
        return None
    if code.startswith("photos_added_"):
        count = code.removeprefix("photos_added_")
        return "Photo added." if count == "1" else f"{count} photos added."
    return OK_MESSAGES.get(code)


def build_router() -> APIRouter:
    router = APIRouter()

    # ── helpers ───────────────────────────────────────────────────────────────
    def session(request: Request):
        """A connection scoped to this request, closed before the response is sent."""
        return db.connect(request.app.state.settings.db_path)

    def render(request: Request, name: str, *, status_code: int = 200, **context) -> HTMLResponse:
        context.setdefault("s", request.app.state.status_snapshot())
        return request.app.state.templates.TemplateResponse(
            request=request, name=name, context=context, status_code=status_code
        )

    def photo_response(request: Request, photo_id: int, *, thumb: bool) -> Response:
        """Stream one WebP. The ETag is the content hash, so the browser can hold it
        forever — a photo keyed by its own sha256 can never go stale."""
        with session(request) as conn:
            meta = photos_repo.get_meta(conn, photo_id)
            if meta is None:
                return Response("No such photo.", status_code=404, media_type="text/plain")
            etag = f'"{meta["sha256"]}-{photo_id}-{"t" if thumb else "f"}"'
            if request.headers.get("if-none-match") == etag:
                return Response(status_code=304, headers={"ETag": etag})
            blob = photos_repo.get_blob(conn, photo_id, thumb=thumb)
        if blob is None:
            return Response("No such photo.", status_code=404, media_type="text/plain")
        data, mime = blob
        return Response(
            data,
            media_type=mime,
            headers={
                "ETag": etag,
                "Cache-Control": "private, max-age=31536000, immutable",
                "Content-Length": str(len(data)),
            },
        )

    # ── cabinet: people ───────────────────────────────────────────────────────
    @router.get("/cabinet", response_class=HTMLResponse)
    def cabinet(
        request: Request,
        q: str = "",
        group: str = "",
        status: str = "active",
        ok: str | None = None,
    ) -> HTMLResponse:
        group_id = _optional_int(group)
        with session(request) as conn:
            people = people_repo.listing(
                conn, q=q or None, group_id=group_id, status=status or None
            )
            groups = groups_repo.listing(conn)
            stats = people_repo.stats(conn)
        return render(
            request,
            "cabinet.html",
            nav="cabinet",
            people=people,
            groups=groups,
            stats=stats,
            q=q,
            group=group_id,
            status=status,
            notice_ok=ok_message(ok),
            notice_err=None,
        )

    @router.get("/people/new", response_class=HTMLResponse)
    def person_new(request: Request) -> HTMLResponse:
        with session(request) as conn:
            groups = groups_repo.listing(conn)
        return render(
            request,
            "person_form.html",
            nav="cabinet",
            person=None,
            groups=groups,
            member_of=[],
            values={"status": "active"},
            error=None,
        )

    @router.post("/people")
    async def person_create(request: Request) -> Response:
        form = await request.form()
        fields = {name: form.get(name, "") for name in people_repo.EDITABLE}
        with session(request) as conn:
            try:
                person_id = people_repo.create(conn, **fields)
            except people_repo.InvalidPerson as exc:
                return render(
                    request,
                    "person_form.html",
                    status_code=400,
                    nav="cabinet",
                    person=None,
                    groups=groups_repo.listing(conn),
                    member_of=[],
                    values=fields,
                    error=str(exc),
                )
            groups_repo.set_groups_for_person(
                conn, person_id, [int(g) for g in form.getlist("groups")]
            )
        return RedirectResponse(f"/people/{person_id}", status_code=303)

    @router.get("/people/{person_id}", response_class=HTMLResponse)
    def person_detail(request: Request, person_id: int, ok: str | None = None, err: str | None = None) -> Response:
        with session(request) as conn:
            person = people_repo.get(conn, person_id)
            if person is None:
                return render(request, "missing.html", status_code=404, nav="cabinet", what="person")
            context = {
                "person": person,
                "photos": photos_repo.listing(conn, person_id),
                # Named `groups` because that is what the shared form partial reads; a
                # second spelling would silently empty the checklist.
                "groups": groups_repo.listing(conn),
                "member_of": groups_repo.member_ids(conn, person_id),
                "values": person,
                "notice_ok": ok_message(ok),
                "notice_err": UPLOAD_ERRORS.get(err or ""),
                "error": None,
            }
        return render(request, "person_detail.html", nav="cabinet", **context)

    @router.post("/people/{person_id}")
    async def person_update(request: Request, person_id: int) -> Response:
        form = await request.form()
        fields = {name: form.get(name, "") for name in people_repo.EDITABLE}
        with session(request) as conn:
            person = people_repo.get(conn, person_id)
            if person is None:
                return render(request, "missing.html", status_code=404, nav="cabinet", what="person")
            try:
                people_repo.update(conn, person_id, **fields)
            except people_repo.InvalidPerson as exc:
                return render(
                    request,
                    "person_form.html",
                    status_code=400,
                    nav="cabinet",
                    person=person,
                    groups=groups_repo.listing(conn),
                    member_of=groups_repo.member_ids(conn, person_id),
                    values=fields,
                    error=str(exc),
                )
            groups_repo.set_groups_for_person(
                conn, person_id, [int(g) for g in form.getlist("groups")]
            )
        return RedirectResponse(f"/people/{person_id}?ok=saved", status_code=303)

    @router.post("/people/{person_id}/delete")
    def person_delete(request: Request, person_id: int) -> Response:
        with session(request) as conn:
            people_repo.delete(conn, person_id)
        return RedirectResponse("/cabinet?ok=deleted", status_code=303)

    # ── cabinet: photos ───────────────────────────────────────────────────────
    @router.post("/people/{person_id}/photos")
    async def photo_upload(request: Request, person_id: int) -> Response:
        settings = request.app.state.settings
        form = await request.form()
        uploads = [value for key, value in form.multi_items() if key == "photos"]

        added = 0
        error: str | None = None
        with session(request) as conn:
            if people_repo.get(conn, person_id) is None:
                return render(request, "missing.html", status_code=404, nav="cabinet", what="person")
            for upload in uploads:
                if not isinstance(upload, UploadFile) or not upload.filename:
                    continue
                # Starlette reports the size the parser saw; trust it before reading.
                if upload.size is not None and upload.size > settings.max_upload_bytes:
                    error = error or "too_large"
                    continue
                raw = await upload.read()
                if not raw:
                    error = error or "empty"
                    continue
                if len(raw) > settings.max_upload_bytes:
                    error = error or "too_large"
                    continue
                try:
                    prepared = images.import_image(raw)
                except images.ImageRejected:
                    error = error or "not_an_image"
                    continue
                if photos_repo.find_by_sha256(conn, prepared.sha256) is not None:
                    error = error or "duplicate"
                    continue
                try:
                    photos_repo.add(conn, person_id, prepared, source=upload.filename[:200])
                except Exception:  # noqa: BLE001 - surfaced to the user as a code, not a traceback
                    error = error or "generic"
                    continue
                added += 1

        params = []
        if added:
            params.append(f"ok=photos_added_{added}")
        if error:
            params.append(f"err={error}")
        suffix = f"?{'&'.join(params)}" if params else ""
        return RedirectResponse(f"/people/{person_id}{suffix}", status_code=303)

    @router.post("/people/{person_id}/photos/{photo_id}/primary")
    def photo_primary(request: Request, person_id: int, photo_id: int) -> Response:
        with session(request) as conn:
            photos_repo.set_primary(conn, person_id, photo_id)
        return RedirectResponse(f"/people/{person_id}?ok=primary", status_code=303)

    @router.post("/people/{person_id}/photos/{photo_id}/delete")
    def photo_delete(request: Request, person_id: int, photo_id: int) -> Response:
        with session(request) as conn:
            photos_repo.delete(conn, photo_id)
        return RedirectResponse(f"/people/{person_id}?ok=photo_deleted", status_code=303)

    @router.get("/photos/{photo_id}.webp")
    def photo_full(request: Request, photo_id: int) -> Response:
        return photo_response(request, photo_id, thumb=False)

    @router.get("/photos/{photo_id}/thumb.webp")
    def photo_thumb(request: Request, photo_id: int) -> Response:
        return photo_response(request, photo_id, thumb=True)

    # ── cabinet: groups ───────────────────────────────────────────────────────
    @router.get("/groups", response_class=HTMLResponse)
    def groups(request: Request, error: str | None = None) -> HTMLResponse:
        with session(request) as conn:
            listing = groups_repo.listing(conn)
            stats = people_repo.stats(conn)
        return render(
            request,
            "groups.html",
            nav="groups",
            groups=listing,
            stats=stats,
            error=error,
            ok=None,
        )

    @router.post("/groups")
    async def group_create(request: Request) -> Response:
        form = await request.form()
        with session(request) as conn:
            try:
                group_id = groups_repo.create(conn, str(form.get("name", "")), str(form.get("note", "")))
            except groups_repo.InvalidGroup:
                listing = groups_repo.listing(conn)
                stats = people_repo.stats(conn)
                return render(
                    request,
                    "groups.html",
                    status_code=400,
                    nav="groups",
                    groups=listing,
                    stats=stats,
                    error="A group needs a name, and it must be unique.",
                    ok=None,
                )
        return RedirectResponse(f"/groups/{group_id}", status_code=303)

    @router.get("/groups/{group_id}", response_class=HTMLResponse)
    def group_detail(request: Request, group_id: int) -> Response:
        with session(request) as conn:
            group = groups_repo.get(conn, group_id)
            if group is None:
                return render(request, "missing.html", status_code=404, nav="groups", what="group")
            context = {
                "group": group,
                "members": groups_repo.members(conn, group_id),
                "member_ids": groups_repo.member_ids(conn, group_id),
                "candidates": people_repo.listing(conn, limit=1000),
            }
        return render(request, "group_detail.html", nav="groups", error=None, ok=None, **context)

    @router.post("/groups/{group_id}")
    async def group_update(request: Request, group_id: int) -> Response:
        form = await request.form()
        with session(request) as conn:
            group = groups_repo.get(conn, group_id)
            if group is None:
                return render(request, "missing.html", status_code=404, nav="groups", what="group")
            try:
                groups_repo.update(
                    conn, group_id, name=str(form.get("name", "")), note=str(form.get("note", ""))
                )
            except groups_repo.InvalidGroup as exc:
                return render(
                    request,
                    "group_detail.html",
                    status_code=400,
                    nav="groups",
                    group=group,
                    members=groups_repo.members(conn, group_id),
                    member_ids=groups_repo.member_ids(conn, group_id),
                    candidates=people_repo.listing(conn, limit=1000),
                    error=str(exc),
                    ok=None,
                )
            groups_repo.set_members(conn, group_id, [int(p) for p in form.getlist("members")])
        return RedirectResponse(f"/groups/{group_id}", status_code=303)

    @router.post("/groups/{group_id}/delete")
    def group_delete(request: Request, group_id: int) -> Response:
        with session(request) as conn:
            groups_repo.delete(conn, group_id)
        return RedirectResponse("/groups", status_code=303)

    # ── progress: what the review log knows ───────────────────────────────────
    @router.get("/stats", response_class=HTMLResponse)
    def progress(request: Request) -> HTMLResponse:
        """Everything the log can tell you, recomputed on every visit.

        Deliberately quiet, and deliberately about *capability* rather than activity: how
        many people you can now name, which faces you keep swapping, and where your own
        sense of certainty disagrees with your answers. No streaks, no points, no guilt.
        """
        with session(request) as conn:
            context = stats_repo.overview(conn, db_path=request.app.state.settings.db_path)
        return render(request, "stats.html", nav="stats", **context)

    # ── the sheet you take with you ───────────────────────────────────────────
    @router.get("/sheet", response_class=HTMLResponse)
    def sheet(request: Request) -> HTMLResponse:
        """One printable page: the faces who will be there, and what to call them.

        Made for the hour before a party, which is why it is a page of its own rather than a
        view of the cabinet: nothing on it but faces and names, no navigation, no controls,
        and it loads its own stylesheet instead of the app's so that printing is the point.
        """
        group_id = _optional_int(request.query_params.get("group"))
        with session(request) as conn:
            rows = people_repo.sheet_rows(conn, group_id=group_id)
            group_name = None
            if group_id:
                found = conn.execute("SELECT name FROM grp WHERE id = ?", (group_id,)).fetchone()
                group_name = found["name"] if found else None
        return render(request, "sheet.html", rows=rows, group_name=group_name)

    return router
