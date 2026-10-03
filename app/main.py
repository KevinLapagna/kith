"""Kith — application entry point.

Responsibilities kept here and nowhere else: building the app (settings, templates,
routers, the login gate), the health endpoint, and the status snapshot that every page
footer reads. Lot-specific behaviour lives in `app/routers/` and `app/repo/`.

The health endpoint is deliberately strict — it returns HTTP 503 unless the database is
at the expected schema version AND a real write succeeds, because the failure mode we
care about (a bind mount the container cannot write to) is silent otherwise.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app import auth, config, db
from app.repo import groups as groups_repo
from app.repo import people as people_repo
from app.routers import auth_routes, pages, train

log = logging.getLogger("kith")

BASE_DIR = Path(__file__).resolve().parent.parent
STARTED_AT = time.monotonic()

# Reachable without a session: the login page, the health endpoint (the container
# healthcheck and Caddy both probe it), and static assets. Everything else is gated
# when KITH_AUTH is on.
PUBLIC_PATHS = {"/login", "/logout", "/api/health", "/favicon.ico"}


def asset_version() -> str:
    """Newest mtime under static/, used as a cache-busting query on asset URLs.

    Learned the hard way in M0: without this, editing the stylesheet leaves the browser
    serving its cached copy, and you end up debugging a fix that is already on disk.
    """
    static = BASE_DIR / "static"
    newest = max((p.stat().st_mtime for p in static.rglob("*") if p.is_file()), default=0.0)
    return str(int(newest))


def breakable_path(value: object) -> str:
    """Insert zero-width break points after path separators.

    A long path in a narrow card otherwise wraps mid-segment ("…/hom e/kith/data"),
    which reads as a rendering bug. Jinja still autoescapes the returned string, so
    this stays safe for user-supplied values too.
    """
    return str(value).replace("/", "/\u200b")


def create_app(settings: config.Settings | None = None) -> FastAPI:
    settings = settings or config.Settings.from_env()
    templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
    templates.env.filters["breakable_path"] = breakable_path
    templates.env.globals["asset_v"] = asset_version

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        logging.basicConfig(
            level=settings.log_level.upper(),
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        )
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        db.check_sqlite_capabilities()

        # Fail loudly rather than boot into a state nobody can use. A half-configured
        # login gate is worse than a container that refuses to start.
        if settings.auth_enabled and not settings.password_hash:
            raise RuntimeError(
                "KITH_AUTH is on but KITH_PASSWORD_HASH is empty, so no password could "
                "ever be accepted. Generate one with: "
                "podman exec -it kith python -m app.cli hash-password"
            )
        if settings.session_secret_generated:
            log.warning(
                "KITH_SESSION_SECRET is not set — a secret was generated for this process, "
                "so existing sessions will be invalidated on every restart."
            )

        with db.connect(settings.db_path) as conn:
            applied = db.migrate(conn)
            version = db.current_version(conn)
        log.info(
            "kith %s ready — db=%s schema=%d sqlite=%s applied=%s",
            config.APP_VERSION,
            settings.db_path,
            version,
            sqlite3.sqlite_version,
            ", ".join(applied) or "none (already up to date)",
        )
        log.info(
            "login gate: %s",
            "ON — a password is required" if settings.auth_enabled else "off — bind to loopback only",
        )
        yield

    app = FastAPI(
        title=config.APP_NAME,
        version=config.APP_VERSION,
        lifespan=lifespan,
        docs_url=None,      # a single-user app does not need a public API browser
        redoc_url=None,
    )
    app.state.settings = settings
    app.state.templates = templates
    app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

    def authenticated(request: Request) -> bool:
        return not settings.auth_enabled or auth.verify_token(
            settings.session_secret, request.cookies.get(auth.COOKIE_NAME)
        )

    def status_snapshot() -> dict:
        """Everything the health endpoint and the dashboard both need."""
        snapshot: dict = {
            "app": config.APP_NAME,
            "version": config.APP_VERSION,
            "sqlite": sqlite3.sqlite_version,
            "db_path": str(settings.db_path),
            "data_dir": str(settings.data_dir),
            "display_tz": settings.display_tz,
            "auth_enabled": settings.auth_enabled,
            "uptime_s": round(time.monotonic() - STARTED_AT, 1),
            "ok": False,
            "db_writable": False,
            "schema": None,
            "expected_schema": config.SCHEMA_VERSION,
            "error": None,
            "pragma": {},
            "people": None,
            "photos": None,
            "groups": None,
            "trainable": None,
            "without_photos": None,
        }
        try:
            with db.connect(settings.db_path) as conn:
                snapshot["schema"] = db.current_version(conn)
                snapshot["db_writable"] = db.probe_write(conn)
                snapshot["pragma"] = db.pragma_report(conn)
                snapshot.update(
                    {k: v for k, v in people_repo.stats(conn).items() if k in snapshot}
                )
        except sqlite3.Error as exc:
            snapshot["error"] = f"{type(exc).__name__}: {exc}"
            log.warning("health check could not read the database", exc_info=True)
            return snapshot

        snapshot["ok"] = bool(
            snapshot["db_writable"]
            and snapshot["schema"] == config.SCHEMA_VERSION
            and snapshot["pragma"].get("integrity") == "ok"
        )
        return snapshot

    app.state.status_snapshot = status_snapshot

    @app.middleware("http")
    async def login_gate(request: Request, call_next):
        """One place that decides whether a request may proceed.

        `/api/*` answers 401 as JSON (a fetch should not be handed a login page), pages
        redirect to `/login` with a `next=` that `_safe_next` will re-validate.
        """
        if not settings.auth_enabled:
            return await call_next(request)
        path = request.url.path
        if path in PUBLIC_PATHS or path.startswith("/static/"):
            return await call_next(request)
        if auth.verify_token(settings.session_secret, request.cookies.get(auth.COOKIE_NAME)):
            return await call_next(request)
        if path.startswith("/api/"):
            return JSONResponse(
                {"ok": False, "error": "authentication required"}, status_code=401
            )
        target = quote(request.url.path, safe="/")
        return RedirectResponse(f"/login?next={target}", status_code=303)

    @app.get("/api/health", response_class=JSONResponse)
    def health(request: Request) -> JSONResponse:
        snapshot = status_snapshot()
        if not authenticated(request):
            # Public on purpose: the container healthcheck and Caddy both probe it. But a
            # public endpoint must not describe the filesystem or the person count, so an
            # anonymous caller gets the verdict and nothing else.
            snapshot = {
                "app": snapshot["app"],
                "version": snapshot["version"],
                "ok": snapshot["ok"],
                "schema": snapshot["schema"],
                "expected_schema": snapshot["expected_schema"],
                "db_writable": snapshot["db_writable"],
                "uptime_s": snapshot["uptime_s"],
                "error": None if snapshot["ok"] else (snapshot["error"] or "unhealthy"),
            }
        return JSONResponse(snapshot, status_code=200 if snapshot["ok"] else 503)

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request) -> HTMLResponse:
        with db.connect(settings.db_path) as conn:
            stats = people_repo.stats(conn)
            groups = groups_repo.listing(conn)
        return templates.TemplateResponse(
            request=request,
            name="dashboard.html",
            context={
                "s": status_snapshot(),
                "nav": "status",
                "stats": stats,
                "groups": groups,
            },
        )

    app.include_router(auth_routes.build_router())
    app.include_router(pages.build_router())
    app.include_router(train.build_router())
    return app


app = create_app()
