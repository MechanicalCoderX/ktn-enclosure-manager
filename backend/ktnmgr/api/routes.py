"""Semantic HTTP API.

Every endpoint is semantic: the browser names an enclosure by logical id and a
slot by integer. There is no parameter anywhere in this surface through which a
path, a command, an argument list, or a shell fragment could reach the system
(spec §30, §43).
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import (
    APIRouter,
    Depends,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
    status,
)
from pydantic import BaseModel, Field

from ktnmgr.enclosure.locate import LocateError
from ktnmgr.enclosure.ses import READ_ONLY_PAGES, SesError
from ktnmgr.enclosure.sysfs import EnclosureNotFoundError, SlotNotFoundError
from ktnmgr.services.auth import SESSION_COOKIE, AuthError, AuthService
from ktnmgr.services.ident import ALLOWED_DURATIONS

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api")

#: Mutating requests must carry this header. Combined with a SameSite=Strict
#: cookie it blocks cross-site form posts, which cannot set custom headers.
CSRF_HEADER = "x-ktn-request"


# --------------------------------------------------------------- dependencies


def get_auth(request: Request) -> AuthService:
    return request.app.state.auth


def get_state(request: Request) -> Any:
    return request.app.state.service


#: Recorded as the actor when authentication is switched off, so an audit
#: entry never implies a named person approved something nobody signed in for.
ANONYMOUS = "anonymous"


def current_user(request: Request) -> str:
    """Resolve the caller, or refuse.

    When ``auth_required`` is off the app is an open read-only dashboard, in
    line with how comparable TrueNAS apps ship. A real session is still
    honoured if one exists, so turning authentication off does not throw away
    the identity of someone who did sign in - it only stops requiring one.
    """
    auth: AuthService = request.app.state.auth
    username = auth.read_session(request.cookies.get(SESSION_COOKIE))
    if username is not None:
        return username
    if not request.app.state.settings.auth_required:
        return ANONYMOUS
    raise HTTPException(status.HTTP_401_UNAUTHORIZED, "authentication required")


def require_csrf(x_ktn_request: Annotated[str | None, Header()] = None) -> None:
    if x_ktn_request is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "missing request header")


CurrentUser = Annotated[str, Depends(current_user)]


# ------------------------------------------------------------------- schemas


class BootstrapBody(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=12, max_length=256)


class LoginBody(BaseModel):
    username: str = Field(max_length=64)
    password: str = Field(max_length=256)


class PasswordBody(BaseModel):
    current_password: str = Field(max_length=256)
    new_password: str = Field(min_length=12, max_length=256)


class IdentifyBody(BaseModel):
    on: bool
    duration_seconds: int | None = Field(
        default=None,
        description="One of 10, 30, 60, 300, or null for 'until cleared'.",
    )


# ---------------------------------------------------------------------- auth


@router.get("/auth/status")
def auth_status(request: Request, auth: Annotated[AuthService, Depends(get_auth)]) -> dict:
    settings = request.app.state.settings
    return {
        # False means the UI goes straight to the dashboard instead of
        # demanding an account that would gate nothing.
        "auth_required": settings.auth_required,
        "anonymous_ident_allowed": settings.allow_anonymous_ident,
        "needs_bootstrap": auth.needs_bootstrap and settings.auth_required,
        "user": auth.read_session(request.cookies.get(SESSION_COOKIE)),
    }


@router.post("/auth/bootstrap", dependencies=[Depends(require_csrf)])
def bootstrap(
    body: BootstrapBody, request: Request, auth: Annotated[AuthService, Depends(get_auth)]
) -> dict:
    if not request.app.state.settings.auth_required:
        # Refused, and this one matters. While the app runs open, this endpoint
        # is reachable by anyone on the network - and the account they create
        # is inert only until the operator turns authentication on, at which
        # point a stranger's password is the administrator credential and the
        # operator is the one locked out. Enable authentication first, then
        # create the account.
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "authentication is disabled on this deployment, so no account can "
            "be created; enable KTN_AUTH_REQUIRED first, then create it",
        )
    try:
        auth.bootstrap(body.username, body.password)
    except AuthError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return {"ok": True}


def _set_session(response: Response, request: Request, token: str, max_age: int) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=max_age,
        httponly=True,
        samesite="strict",
        secure=request.url.scheme == "https",
        path="/",
    )


@router.post("/auth/login", dependencies=[Depends(require_csrf)])
def login(
    body: LoginBody,
    request: Request,
    response: Response,
    auth: Annotated[AuthService, Depends(get_auth)],
) -> dict:
    client = request.client.host if request.client else "unknown"
    # The limiter refusal is 429, not 401, mirroring change_password: the two
    # endpoints share one limiter, and the same refusal answered "wrong
    # password" here and "slow down" there. 429 tells a locked-out legitimate
    # user that waiting - not retyping - is the remedy, and tells nothing
    # about the credential that 401 does not already tell.
    try:
        auth.limiter.check(client)
    except AuthError as exc:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, str(exc)) from exc
    try:
        username = auth.verify(body.username, body.password)
    except AuthError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc)) from exc
    auth.limiter.reset(client)
    _set_session(response, request, auth.issue_session(username), auth.max_age_seconds)
    return {"ok": True, "user": username}


@router.post("/auth/logout", dependencies=[Depends(require_csrf)])
def logout(response: Response) -> dict:
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"ok": True}


@router.post("/auth/password", dependencies=[Depends(require_csrf)])
def change_password(
    body: PasswordBody,
    request: Request,
    user: CurrentUser,
    auth: Annotated[AuthService, Depends(get_auth)],
) -> dict:
    if user == ANONYMOUS:
        # There is no account to change. Refusing beats calling change_password
        # with a username that does not exist and returning its generic error.
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "no account is signed in; authentication is disabled on this deployment",
        )
    # Rate limited like login, because it verifies a password like login. This
    # endpoint is the one a stolen session cookie gets pointed at: the cookie
    # expires, the password does not, and current_password is the only thing
    # standing between the two. Unlimited attempts here meant a cookie thief
    # could brute-force their way from a temporary session to the permanent
    # credential; the login limiter never saw it because no login happens.
    client = request.client.host if request.client else "unknown"
    try:
        auth.limiter.check(client)
    except AuthError as exc:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, str(exc)) from exc
    try:
        auth.change_password(user, body.current_password, body.new_password)
    except AuthError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    auth.limiter.reset(client)
    return {"ok": True}


@router.post("/auth/revoke-sessions", dependencies=[Depends(require_csrf)])
def revoke_sessions(
    user: CurrentUser,
    response: Response,
    auth: Annotated[AuthService, Depends(get_auth)],
) -> dict:
    """Sign this account out everywhere, including here.

    The remedy for a stolen cookie when the password itself is not suspected.
    The epoch mechanism existed since the change-password work, but nothing
    exposed it - the same mistake the change-password endpoint itself once
    made, shipping an API with no way to use it. Every session for the account
    stops being accepted, the caller's own included; the caller signs back in
    and holds the only valid session again.
    """
    if user == ANONYMOUS:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "no account is signed in; authentication is disabled on this deployment",
        )
    try:
        auth.revoke_sessions(user)
    except AuthError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"ok": True}


# ---------------------------------------------------------------- enclosures


@router.get("/enclosures")
def list_enclosures(user: CurrentUser, service: Annotated[Any, Depends(get_state)]) -> list[dict]:
    return [
        {
            **ref.model_dump(),
            "slots_discovered": len(service.slots.value.get(ref.logical_id, [])),
        }
        for ref in service.enclosures.value
    ]


def _resolve(service: Any, enclosure_id: str) -> None:
    try:
        service.enclosure(enclosure_id)
    except EnclosureNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "enclosure not attached") from exc


@router.get("/enclosures/{enclosure_id}/bays")
def list_bays(
    enclosure_id: str, user: CurrentUser, service: Annotated[Any, Depends(get_state)]
) -> dict:
    _resolve(service, enclosure_id)
    return {
        "bays": [b.model_dump(mode="json") for b in service.bays(enclosure_id)],
        "sources": {
            "slots": service.slots.updated_at,
            "truenas": service.zfs.updated_at,
            "truenas_error": service.zfs.last_error,
            "smart": service.smart.updated_at,
        },
    }


@router.get("/enclosures/{enclosure_id}/chassis")
def chassis(
    enclosure_id: str, user: CurrentUser, service: Annotated[Any, Depends(get_state)]
) -> dict:
    _resolve(service, enclosure_id)
    telemetry = service.chassis.value.get(enclosure_id.lower())
    if telemetry is None:
        return {
            "available": False,
            "error": service.chassis.last_error or "chassis telemetry not collected yet",
        }
    return {"available": True, **telemetry.model_dump(mode="json")}


@router.post("/enclosures/{enclosure_id}/slots/{ses_slot}/identify",
             dependencies=[Depends(require_csrf)])
async def identify(
    enclosure_id: str,
    ses_slot: int,
    body: IdentifyBody,
    request: Request,
    user: CurrentUser,
    service: Annotated[Any, Depends(get_state)],
) -> dict:
    """The only write this application exposes (§15, §43).

    Gated separately from the read surface. Opening the dashboard does not
    open this: an anonymous caller is refused unless the operator has also
    set ``allow_anonymous_ident``, because a write that actuates hardware
    should never become reachable as a side effect of another setting.
    """
    settings = request.app.state.settings
    if user == ANONYMOUS and not settings.allow_anonymous_ident:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Identify requires an account. Authentication is disabled on this "
            "deployment, so the LED write is refused; set "
            "KTN_ALLOW_ANONYMOUS_IDENT=true to permit it, or re-enable "
            "authentication.",
        )

    if body.on and body.duration_seconds not in ALLOWED_DURATIONS:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"duration must be one of {[d for d in ALLOWED_DURATIONS if d]} or null",
        )
    _resolve(service, enclosure_id)

    serial = None
    for bay in service.bays(enclosure_id):
        if bay.ses_slot == ses_slot:
            serial = bay.disk.serial
            break

    try:
        record = await service.ident.identify(
            enclosure_id,
            ses_slot,
            on=body.on,
            user=user,
            duration_seconds=body.duration_seconds,
            serial=serial,
        )
    except SlotNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "slot not present") from exc
    except LocateError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    # Refresh the slot cache immediately rather than waiting for the next poll
    # tick, so the caller's next read - and the countdown the UI starts showing
    # straight away - reflect the write that was just verified.
    await service.poll_hardware()

    return {
        "ok": True,
        "locate": body.on,
        "expires_at": record.expires_at if record else None,
        "origin": record.origin if record else None,
    }


# --------------------------------------------------------------- diagnostics


@router.get("/diagnostics")
def diagnostics(user: CurrentUser, service: Annotated[Any, Depends(get_state)]) -> dict:
    return service.diagnostics()


@router.get("/audit")
def audit(
    user: CurrentUser,
    request: Request,
    # Bounded, and bounded to the same ceiling AuditLog.tail() clamps to, so
    # the documented maximum is the real one rather than a number the service
    # silently reduces.
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[dict]:
    return [e.model_dump(mode="json") for e in request.app.state.audit.tail(limit)]


@router.get("/raw/pages")
def raw_pages(user: CurrentUser) -> list[str]:
    return sorted(READ_ONLY_PAGES)


@router.get("/raw/{enclosure_id}/{page}")
def raw_page(
    enclosure_id: str, page: str, user: CurrentUser,
    service: Annotated[Any, Depends(get_state)],
) -> dict:
    """Predefined read-only diagnostic output only (§36).

    ``page`` is looked up in an allow-list; arbitrary sg_ses parameters are not
    expressible through this endpoint.
    """
    if page not in READ_ONLY_PAGES:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown diagnostic page")
    try:
        ref = service.enclosure(enclosure_id)
    except EnclosureNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "enclosure not attached") from exc
    if not ref.sg_device:
        raise HTTPException(status.HTTP_409_CONFLICT, "no sg device for this enclosure")
    try:
        result = service.ses.read_for(ref, page)
    except SesError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    return {"page": page, "output": result.stdout}
