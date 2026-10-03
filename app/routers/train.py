"""The training routes: the builder, the session screen, and the JSON the screen talks to.

One rule shapes all of it: **the browser is a renderer, not a participant**. It posts a
choice, not a verdict, and never sees the answer before it has answered. Grading happens in
`app.trainer`, against the card the server already knows the item is about.
"""

from __future__ import annotations

import json
import random

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from app import db, modes, trainer
from app.repo import cards as cards_repo
from app.repo import groups as groups_repo
from app.repo import sessions as sessions_repo
from app.repo import settings as settings_repo


def build_router() -> APIRouter:
    router = APIRouter()

    def session(request: Request):
        return db.connect(request.app.state.settings.db_path)

    def render(request: Request, name: str, *, status_code: int = 200, **context) -> HTMLResponse:
        context.setdefault("s", request.app.state.status_snapshot())
        return request.app.state.templates.TemplateResponse(
            request=request, name=name, context=context, status_code=status_code
        )

    def json_error(message: str, status_code: int = 400) -> JSONResponse:
        return JSONResponse({"ok": False, "error": message}, status_code=status_code)

    def _int(value: str | None, default: int = 0) -> int:
        try:
            return int(str(value).strip())
        except (TypeError, ValueError):
            return default

    def _truthy(value: object) -> bool:
        """An unticked checkbox posts nothing, a browser may post "0", a link posts "1"."""
        return str(value or "").strip().lower() not in ("", "0", "false", "off", "no", "none")

    # ── what you chose last time ──────────────────────────────────────────────
    # The builder's selections survive a reload, and they have to: "nothing is due" bounces you
    # straight back to this form, and a form that quietly re-defaults itself there turns a choice
    # you made two clicks ago into a surprise. A cookie rather than the database — it is one
    # browser's convenience, not part of the training record, and it costs no migration.
    BUILDER_COOKIE = "kith_builder"
    BUILDER_NUMBERS = {"new_budget": (0, 3, 5, 10), "minutes": (5, 10, 20, 30)}
    BUILDER_FALLBACK_MODES = ("mc4", "recall_self")

    def remembered(request: Request) -> dict:
        """The last submission, parsed as what it is: user input wearing a cookie."""
        raw = request.cookies.get(BUILDER_COOKIE)
        if not raw:
            return {}
        try:
            state = json.loads(raw)
        except (TypeError, ValueError):
            return {}
        if not isinstance(state, dict):
            return {}
        clean: dict = {}
        if isinstance(state.get("modes"), list):
            clean["modes"] = [key for key in state["modes"] if isinstance(key, str)]
        for key, allowed in BUILDER_NUMBERS.items():
            if state.get(key) in allowed:
                clean[key] = int(state[key])
        if isinstance(state.get("grp"), int):
            clean["grp"] = state["grp"]
        if isinstance(state.get("force"), bool):
            clean["force"] = state["force"]
        return clean

    def remember(
        response,
        request: Request,
        *,
        wanted: list[str],
        grp: int,
        new_budget: int,
        minutes: int,
        force: bool,
    ):
        """Record the submission on whatever leaves this route — refusals included."""
        response.set_cookie(
            BUILDER_COOKIE,
            json.dumps(
                {
                    "modes": wanted,
                    "grp": grp,
                    "new_budget": new_budget,
                    "minutes": minutes,
                    "force": force,
                }
            ),
            max_age=180 * 24 * 3600,
            httponly=True,
            samesite="lax",
            secure=bool(request.app.state.settings.secure_cookies),
        )
        return response

    # ── the builder ───────────────────────────────────────────────────────────
    @router.get("/train", response_class=HTMLResponse)
    def builder(request: Request, error: str = "", force: str = "") -> HTMLResponse:
        now = db.utcnow()
        with session(request) as conn:
            pool = trainer.training_pool(conn)
            groups = groups_repo.listing(conn)
            card_stats = cards_repo.counts(conn, now=now)
            resume = sessions_repo.open_session(conn)
            resume_counts = sessions_repo.counts(conn, int(resume["id"])) if resume else None
        usable = modes.available(len(pool))
        usable_keys = [mode.key for mode in usable]
        # The recommended default is the recognition → production pair and nothing else. `study`
        # is deliberately not in it: a never-seen face is introduced with a study card whatever
        # you tick, so ticking it means "also show me faces I have already met" — a different wish.
        default_keys = [key for key in BUILDER_FALLBACK_MODES if key in usable_keys]
        last = remembered(request)
        chosen_modes = [key for key in last.get("modes", []) if key in usable_keys] or default_keys
        group_ids = {int(group["id"]) for group in groups}
        return render(
            request,
            "train.html",
            nav="train",
            pool_size=len(pool),
            card_stats=card_stats,
            groups=groups,
            all_modes=modes.all_modes(),
            usable_keys=usable_keys,
            chosen_modes=chosen_modes,
            chosen_grp=last.get("grp") if last.get("grp") in group_ids else 0,
            chosen_new_budget=last.get("new_budget", 3),
            chosen_minutes=last.get("minutes", 10),
            # The refusal's own hint wins for that render: it is the one moment when ticking
            # "practise anyway" is the obvious next move.
            show_force=_truthy(force) or bool(last.get("force")),
            resume=resume,
            resume_counts=resume_counts,
            error=error or None,
        )

    @router.post("/train")
    async def start(request: Request) -> HTMLResponse:
        form = await request.form()
        wanted = [str(value) for value in form.getlist("modes")]
        grp = _int(form.get("grp"), 0)
        new_budget = _int(form.get("new_budget"), 3)
        minutes = _int(form.get("minutes"), 10)
        people = [
            int(part)
            for part in str(form.get("people") or "").replace(" ", "").split(",")
            if part.strip().isdigit()
        ]
        force = _truthy(form.get("force"))
        keep = dict(wanted=wanted, grp=grp, new_budget=new_budget, minutes=minutes, force=force)
        if not wanted:
            return remember(
                RedirectResponse("/train?error=Pick+at+least+one+exercise", status_code=303),
                request,
                **keep,
            )
        now = db.utcnow()
        try:
            with session(request) as conn:
                session_id = trainer.build_session(
                    conn,
                    mode_keys=wanted,
                    grp_ids=[grp] if grp else None,
                    person_ids=people or None,
                    new_budget=max(0, min(new_budget, 50)),
                    minutes=max(1, min(minutes, 60)),
                    include_all=force,
                    rng=random.Random(),
                    now=now,
                )
        except trainer.NoPeople as exc:
            # Hand the switch back ticked: "nothing is due" is exactly when you want to be able to
            # say "run me through them anyway" without hunting for the control. And remember the
            # rest of the form — that reload is precisely where mode choices used to evaporate.
            return remember(
                RedirectResponse(
                    f"/train?error={str(exc).replace(' ', '+')}&force=1", status_code=303
                ),
                request,
                **keep,
            )
        except trainer.NoModes as exc:
            return remember(
                RedirectResponse(f"/train?error={str(exc).replace(' ', '+')}", status_code=303),
                request,
                **keep,
            )
        return remember(
            RedirectResponse(f"/train/{session_id}", status_code=303), request, **keep
        )

    # ── the session screen ────────────────────────────────────────────────────
    @router.get("/train/{session_id}", response_class=HTMLResponse)
    def run(request: Request, session_id: int) -> HTMLResponse:
        with session(request) as conn:
            sheet = sessions_repo.get(conn, session_id)
            if sheet is None:
                return render(request, "train_missing.html", nav="train", status_code=404)
            if sheet.get("ended_at"):
                return RedirectResponse(f"/train/{session_id}/summary", status_code=303)
            envelope = trainer.envelope(conn, sheet)
            # Name the modes this run may use. The ladder moves a face between the ticked ones as
            # it settles, so without this a learner who ticked "multiple choice" and was then asked
            # to free-recall a solid face has no way to tell the app did what it was told.
            # (`sessions_repo` hands the column back decoded; be tolerant of both shapes.)
            ticked = sheet.get("modes") or []
            if not isinstance(ticked, list):
                ticked = json.loads(ticked)
            mode_labels = [
                mode.label for key in ticked if (mode := modes.get(str(key))) is not None
            ]
        if envelope["done"]:
            with session(request) as conn:
                sessions_repo.end(conn, session_id, now=db.utcnow())
            return RedirectResponse(f"/train/{session_id}/summary", status_code=303)
        return render(
            request,
            "run.html",
            nav="train",
            session_id=session_id,
            bootstrap=json.dumps(envelope),
            mode_labels=mode_labels,
        )

    @router.get("/train/{session_id}/summary", response_class=HTMLResponse)
    def summary(request: Request, session_id: int) -> HTMLResponse:
        with session(request) as conn:
            sheet = sessions_repo.get(conn, session_id)
            if sheet is None:
                return render(request, "train_missing.html", nav="train", status_code=404)
            data = trainer.summary(conn, session_id)
        return render(request, "summary.html", nav="train", session_id=session_id, **data)

    # ── the JSON the screen talks to ──────────────────────────────────────────
    @router.get("/api/sessions/{session_id}")
    def state(request: Request, session_id: int) -> JSONResponse:
        with session(request) as conn:
            sheet = sessions_repo.get(conn, session_id)
            if sheet is None:
                return json_error("no such session", 404)
            return JSONResponse({"ok": True, **trainer.envelope(conn, sheet)})

    @router.post("/api/sessions/{session_id}/answer")
    async def answer(request: Request, session_id: int) -> JSONResponse:
        try:
            payload = await request.json()
        except Exception:  # noqa: BLE001 - any malformed body is a 400, not a 500
            return json_error("expected a JSON body")
        if not isinstance(payload, dict):
            return json_error("expected a JSON object")

        with session(request) as conn:
            sheet = sessions_repo.get(conn, session_id)
            if sheet is None:
                return json_error("no such session", 404)
            item = sessions_repo.item(conn, _int(payload.get("item_id"), -1))
            if item is None or int(item["session_id"]) != session_id:
                return json_error("that item is not part of this session", 409)
            if str(item["state"]) not in sessions_repo.OPEN_STATES:
                return json_error("that item has already been answered", 409)
            try:
                envelope = trainer.submit(conn, sheet, item, payload)
            except trainer.NoPeople as exc:
                return json_error(str(exc), 409)
            except ValueError as exc:
                # A mode refused the answer (nothing to grade it with). Say so rather than
                # guessing: an invented verdict would be written into the review log.
                return json_error(str(exc), 409)
            if envelope["done"]:
                sessions_repo.end(conn, session_id, now=db.utcnow())
                # Keep the verdict: the *last* answer of a session is exactly when the
                # feedback matters most, and recomputing the envelope must not drop it.
                envelope = trainer.envelope(
                    conn,
                    sessions_repo.get(conn, session_id) or sheet,
                    verdict=envelope["verdict"],
                )
        return JSONResponse({"ok": True, **envelope})

    @router.post("/api/sessions/{session_id}/hint")
    async def hint(request: Request, session_id: int) -> JSONResponse:
        try:
            payload = await request.json()
        except Exception:  # noqa: BLE001
            return json_error("expected a JSON body")
        level = _int(payload.get("level"), 1) if isinstance(payload, dict) else 1
        with session(request) as conn:
            sheet = sessions_repo.get(conn, session_id)
            if sheet is None:
                return json_error("no such session", 404)
            item = sessions_repo.item(conn, _int(payload.get("item_id"), -1))
            if item is None or int(item["session_id"]) != session_id:
                return json_error("that item is not part of this session", 409)
            text = trainer.hint_for(conn, sheet, item, max(1, min(level, 5)))
        if text is None:
            return json_error("no hint for that item", 409)
        return JSONResponse({"ok": True, "level": level, "text": text})

    @router.post("/api/sessions/{session_id}/reveal")
    async def reveal(request: Request, session_id: int) -> JSONResponse:
        """Hand back what the card says — only once Reveal has been pressed."""
        try:
            payload = await request.json()
        except Exception:  # noqa: BLE001
            return json_error("expected a JSON body")
        with session(request) as conn:
            sheet = sessions_repo.get(conn, session_id)
            if sheet is None:
                return json_error("no such session", 404)
            item = sessions_repo.item(conn, _int(payload.get("item_id"), -1))
            if item is None or int(item["session_id"]) != session_id:
                return json_error("that item is not part of this session", 409)
            data = trainer.reveal_for(conn, sheet, item)
        if data is None:
            return json_error("that item no longer exists", 409)
        return JSONResponse({"ok": True, "reveal": data})

    @router.post("/api/sessions/{session_id}/end")
    async def end(request: Request, session_id: int) -> JSONResponse:
        with session(request) as conn:
            sheet = sessions_repo.get(conn, session_id)
            if sheet is None:
                return json_error("no such session", 404)
            sessions_repo.end(conn, session_id, now=db.utcnow())
        return JSONResponse({"ok": True, "summary_url": f"/train/{session_id}/summary"})

    @router.get("/api/train/settings")
    def tuning(request: Request) -> JSONResponse:
        """What the builder shows as current defaults — read-only, for transparency."""
        with session(request) as conn:
            values = settings_repo.all_values(conn)
        return JSONResponse({"ok": True, "settings": values})

    return router
