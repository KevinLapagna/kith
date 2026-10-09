"""The login gate: two pages and two cookie writes. Nothing else lives here.

The app is single-user and has no user table, so "login" is exactly this: compare the
submitted password against the configured scrypt hash, and on success hand back a signed
expiry token. There is deliberately no password reset, no "remember me" choice, and no
account page.

Timing note: `verify_password` runs even when no hash is configured, so a
misconfigured deployment cannot be distinguished from a wrong password by response time.
"""

from __future__ import annotations

import logging
from urllib.parse import quote

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from app import auth, i18n

log = logging.getLogger("kith.auth")

# Computed once at import (~50 ms): a real hash to verify against when none is
# configured, so the failure path costs the same as a genuine check.
_DECOY_HASH = auth.hash_password("kith-decoy-not-a-real-credential")


def _safe_next(raw: str | None) -> str:
    """Only ever redirect to a path on this site — never to a URL from the query string."""
    candidate = (raw or "/").strip()
    if not candidate.startswith("/") or candidate.startswith("//"):
        return "/"
    return candidate


def build_router() -> APIRouter:
    router = APIRouter()

    def render(request: Request, name: str, *, status_code: int = 200, **context) -> HTMLResponse:
        return request.app.state.templates.TemplateResponse(
            request=request, name=name, context=context, status_code=status_code
        )

    @router.get("/login", response_class=HTMLResponse)
    def login_form(request: Request, next: str = "/") -> Response:
        settings = request.app.state.settings
        if not settings.auth_enabled:
            return RedirectResponse("/", status_code=303)
        if auth.verify_token(settings.session_secret, request.cookies.get(auth.COOKIE_NAME)):
            return RedirectResponse(_safe_next(next), status_code=303)
        return render(request, "login.html", next=_safe_next(next), error=None)

    @router.post("/login")
    def login(request: Request, password: str = Form(""), next: str = Form("/")) -> Response:
        settings = request.app.state.settings
        if not settings.auth_enabled:
            return RedirectResponse("/", status_code=303)

        target = _safe_next(next)
        stored = settings.password_hash or ""
        if stored:
            matches = auth.verify_password(stored, password)
        else:
            # No hash configured: burn the same scrypt work anyway, so "unset hash" is not
            # a fast path distinguishable from "wrong password".
            auth.verify_password(_DECOY_HASH, password)
            matches = False

        if not matches:
            log.warning("failed login attempt from %s", request.client.host if request.client else "?")
            return render(
                request,
                "login.html",
                status_code=401,
                next=target,
                error=i18n.t("login.error"),
            )

        token = auth.issue_token(settings.session_secret, days=settings.session_days)
        response = RedirectResponse(target, status_code=303)
        response.headers["Set-Cookie"] = auth.cookie_header(
            token, days=settings.session_days, secure=settings.secure_cookies
        )
        log.info("login succeeded")
        return response

    @router.post("/logout")
    def logout() -> Response:
        response = RedirectResponse("/login", status_code=303)
        response.headers["Set-Cookie"] = auth.clear_cookie_header()
        return response

    @router.get("/logout")
    def logout_get() -> Response:
        """A GET logout is not RESTful, but this is a single-user app and typing the URL
        is the shortest way out of a shared browser. It changes no server state."""
        return logout()

    return router
