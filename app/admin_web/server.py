"""The admin web page: the same data functions as `python -m app.admin`,
behind Cloudflare Access, for the administrators in ADMIN_WEB_EMAILS.

Its mode is fixed when it starts (ADMIN_WEB_MODE). It only reads, except for
two kinds of rows its database role may add: audit entries, including one
for each account viewed, and lowerings of its own access (app.admin.web).
Lowering asks twice: the page first shows what will happen and gets a
signed token that lasts two minutes, then the administrator types the mode.
Nothing here raises the access again; only `web restore` on the command line
does. While the page is disabled, it shows nothing but how to restore it.
"""

import hashlib
import hmac
import logging
import secrets
import time
import uuid
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.admin import audit, jobs, report, status, users, web
from app.admin import limits as limit_controls
from app.admin.common import AdminError, check_reason, parse_duration
from app.admin.common import audit as log_action
from app.admin_web import SERVICE_HOST
from app.admin_web.access import HEADER, AccessVerifier, Denied
from app.config import settings
from app.worker import tasks

logger = logging.getLogger(__name__)

STATIC = Path(__file__).parent / "static"
FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/admin.js": ("admin.js", "text/javascript; charset=utf-8"),
    "/admin.css": ("admin.css", "text/css; charset=utf-8"),
}
TOKEN_SECONDS = 120
MAX_LIMIT = 200
# Everything the page loads is its own; nothing may frame it, and nothing it
# shows may be cached or sent elsewhere.
SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self'; "
        "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=()",
}
SIGN_IN = "Sign in through Cloudflare Access with an address in ADMIN_WEB_EMAILS"


class LowerRequest(BaseModel):
    mode: str
    reason: str | None = None


class LowerConfirmation(LowerRequest):
    token: str
    expires: int
    confirm: str


def create_app(config=None, verifier: AccessVerifier | None = None, clock=time.time) -> FastAPI:
    config = config or settings
    admin = config.admin_web
    # Fixed for the life of the service: changing ADMIN_WEB_MODE needs a restart.
    started_mode = admin.mode
    if started_mode == "disabled":
        raise RuntimeError("ADMIN_WEB_MODE=disabled")
    verifier = verifier or AccessVerifier(admin.access_team_domain, admin.access_aud, admin.emails)
    origin = f"https://{admin.hostname}"
    # Signs confirmation tokens; a restart makes unused ones invalid.
    token_key = secrets.token_bytes(32)

    app = FastAPI(title="Admin", docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=[admin.hostname, SERVICE_HOST])

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.update(SECURITY_HEADERS)
        return response

    @app.exception_handler(AdminError)
    async def admin_error(request: Request, error: AdminError):
        return JSONResponse({"detail": str(error)}, status_code=400)

    @app.exception_handler(Exception)
    async def unexpected(request: Request, error: Exception):
        # The full error goes to the log, never to the browser.
        reference = uuid.uuid4().hex[:12]
        logger.error("admin web request failed (%s)", reference, exc_info=error)
        return JSONResponse(
            {"detail": f"Internal error, reference {reference}; see the admin service's log"},
            status_code=500,
            headers=SECURITY_HEADERS,
        )

    def administrator(request: Request) -> str:
        try:
            return verifier.verify(request.headers.get(HEADER))
        except Denied as error:
            logger.warning("refused %s %s: %s", request.method, request.url.path, error)
            raise HTTPException(403, SIGN_IN) from None

    def current() -> dict:
        """The access in effect now; disabled if it cannot be read."""
        try:
            return web.state(started_mode)
        except Exception:
            logger.exception("cannot read the admin web page's lowerings; acting as disabled")
            return {"configured": started_mode, "effective": "disabled", "lowerings": None}

    def readable(email: str = Depends(administrator)) -> str:
        if current()["effective"] == "disabled":
            raise HTTPException(503, "The admin web page is disabled. Restore it on the server: python scripts/admin.py web restore")
        return email

    def same_origin(request: Request) -> None:
        """A change comes from this page's own script: JSON, from its origin."""
        if request.headers.get("origin") != origin:
            raise HTTPException(403, "Changes are accepted only from this page")
        if request.headers.get("sec-fetch-site") not in (None, "same-origin"):
            raise HTTPException(403, "Changes are accepted only from this page")
        if request.headers.get("content-type", "").split(";")[0].strip() != "application/json":
            raise HTTPException(415, "Send JSON")

    def sign(email: str, current_mode: str, mode: str, reason: str, expires: int) -> str:
        message = "\n".join([email, current_mode, mode, hashlib.sha256(reason.encode()).hexdigest(), str(expires)])
        return hmac.new(token_key, message.encode(), hashlib.sha256).hexdigest()

    # --- The page --------------------------------------------------------------

    for path, (name, media_type) in FILES.items():
        def serve(email: str = Depends(administrator), name=name, media_type=media_type):
            return FileResponse(STATIC / name, media_type=media_type)

        app.get(path, include_in_schema=False)(serve)

    # --- Reading ---------------------------------------------------------------

    @app.get("/api/me")
    def me(email: str = Depends(administrator)):
        state = current()
        return {"email": email, **state, "lower_to": web.lower_modes(state["effective"])}

    @app.get("/api/status")
    def get_status(email: str = Depends(readable)):
        return status.collect()

    @app.get("/api/jobs")
    def get_jobs(
        email: str = Depends(readable),
        job_status: str | None = Query(None, alias="status"),
        backend: str | None = None,
        user: str | None = None,
        since: str | None = None,
        limit: int = Query(30, ge=1, le=MAX_LIMIT),
    ):
        if job_status is not None and job_status not in jobs.STATUSES:
            raise AdminError(f"status must be one of {', '.join(jobs.STATUSES)}")
        if backend not in (None, "local", "remote"):
            raise AdminError("backend must be local or remote")
        return jobs.list_jobs(job_status, backend, user, parse_duration(since, "since"), limit)

    @app.get("/api/jobs/{job}")
    def get_job(job: str, email: str = Depends(readable)):
        return jobs.job_details(job)

    @app.get("/api/users")
    def find_users(text: str, email: str = Depends(readable)):
        found = users.find(text)
        # The search text is not kept: it may be part of someone's address.
        log_action(f"web:{email}", "web.user_search", detail=f"{len(found)} found")
        return found

    @app.get("/api/users/{who}")
    def get_user(who: str, email: str = Depends(readable)):
        account = users.account(who)
        # Who looked at whose account is kept; nothing is shown unless it is.
        log_action(f"web:{email}", "web.user_view", account["user_id"])
        return account

    @app.get("/api/billing")
    def get_billing(email: str = Depends(readable), per: str = "day", since: str = "30d"):
        if per not in report.PERIOD_FORMATS:
            raise AdminError(f"per must be one of {', '.join(sorted(report.PERIOD_FORMATS))}")
        return report.billing_report(parse_duration(since, "since"), per)

    @app.get("/api/limits")
    def get_limits(email: str = Depends(readable)):
        return limit_controls.current()

    @app.get("/api/audit")
    def get_audit(email: str = Depends(readable), limit: int = Query(30, ge=1, le=MAX_LIMIT)):
        return audit.entries(limit)

    # --- Lowering its own access -------------------------------------------------

    @app.post("/api/access/plan", dependencies=[Depends(same_origin)])
    def plan_lowering(body: LowerRequest, email: str = Depends(administrator)):
        current_mode = current()["effective"]
        if body.mode not in web.lower_modes(current_mode):
            raise AdminError(f"The page is {current_mode}; it can only be lowered, to {' or '.join(web.lower_modes(current_mode)) or 'nothing'}")
        reason = check_reason(body.reason, required=True)
        expires = int(clock()) + TOKEN_SECONDS
        return {
            "current": current_mode,
            "mode": body.mode,
            "applies_to": "every administrator",
            "undo": "only on the server: python scripts/admin.py web restore",
            "type_to_confirm": body.mode,
            "token": sign(email, current_mode, body.mode, reason, expires),
            "expires": expires,
        }

    @app.post("/api/access/lower", dependencies=[Depends(same_origin)])
    def lower_access(body: LowerConfirmation, email: str = Depends(administrator)):
        current_mode = current()["effective"]
        reason = check_reason(body.reason, required=True)
        expected = sign(email, current_mode, body.mode, reason, body.expires)
        if not hmac.compare_digest(expected, body.token) or body.expires < clock():
            raise HTTPException(409, "The confirmation has expired or the access changed meanwhile; start again")
        if body.confirm.strip() != body.mode:
            raise AdminError(f"Type {body.mode} to confirm")
        web.lower(body.mode, current_mode, f"web:{email}", reason)
        state = current()
        return {"email": email, **state, "lower_to": web.lower_modes(state["effective"])}

    return app
