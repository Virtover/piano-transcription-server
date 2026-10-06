import time
from typing import Any

import requests
from cachetools import TTLCache
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.config import settings


bearer_scheme = HTTPBearer(auto_error=False)

TOKENINFO_URL = "https://oauth2.googleapis.com/tokeninfo"
USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"

# Avoids a call to Google on every API request. Worst-case delay before a
# revoked token is noticed is the TTL.
_token_cache: TTLCache = TTLCache(maxsize=10_000, ttl=300)


def google_allowed_client_ids() -> set[str]:
    return {client_id.strip() for client_id in settings.google_oauth_client_ids if client_id.strip()}


def google_oauth_client_ids() -> list[str]:
    return list(settings.google_oauth_client_ids)


def _unavailable(error: Exception) -> HTTPException:
    return HTTPException(
        status_code=503,
        detail="Google authentication service is unavailable",
    )


def verify_google_access_token(token: str) -> dict[str, Any]:
    allowed_client_ids = google_allowed_client_ids()
    if not allowed_client_ids:
        raise HTTPException(
            status_code=503,
            detail="Google authentication is not configured",
        )
    
    info = _token_cache.get(token)
    if info is None:
        info = _fetch_token_info(token, allowed_client_ids)
        _token_cache[token] = info
    
    now = time.time()
    expires_at = int(info.get("exp", 0))
    if expires_at <= now:
        _token_cache.pop(token, None)
        raise HTTPException(
            status_code=401,
            detail="Google authentication token has expired",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Test-only: reject tokens older than N seconds (age estimated from exp).
    # age = now - (expires_at - 3600)
    # print(f"Token age: {age:.1f} seconds")
    # if age > 60:
    #     raise HTTPException(
    #         status_code=401,
    #         detail="Google authentication token is too old",
    #         headers={"WWW-Authenticate": "Bearer"},
    #     )
    #
    return info


def _fetch_token_info(token: str, allowed_client_ids: set[str]) -> dict[str, Any]:
    try:
        response = requests.get(
            TOKENINFO_URL,
            params={"access_token": token},
            timeout=5,
        )
    except requests.RequestException as error:
        raise _unavailable(error) from error
    
    if response.status_code in (400, 401):
        raise HTTPException(
            status_code=401,
            detail="Invalid Google authentication token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if not response.ok:
        raise _unavailable(RuntimeError(f"tokeninfo returned {response.status_code}"))

    info: dict[str, Any] = response.json()

    # SECURITY: the token must have been issued to one of OUR clients.
    if info.get("aud") not in allowed_client_ids:
        raise HTTPException(
            status_code=401,
            detail="Google authentication token was not issued for this app",
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        info["exp"] = int(info["exp"])
    except (KeyError, TypeError, ValueError) as error:
        print(f"Token info missing or invalid 'exp' field: {info}") #
        raise HTTPException(status_code=401, detail="Invalid Google authentication token") from error

    # tokeninfo includes `sub` when the openid scope was granted. Fall back to userinfo if not.
    if not info.get("sub"):
        try:
            userinfo = requests.get(
                USERINFO_URL,
                headers={"Authorization": f"Bearer {token}"},
                timeout=5,
            )
        except requests.RequestException as error:
            raise _unavailable(error) from error
        if userinfo.status_code in (400, 401):
            print(f"Userinfo returned {userinfo.status_code}: {userinfo.text}") #
            raise HTTPException(status_code=401, detail="Invalid Google authentication token")
        if not userinfo.ok:
            raise _unavailable(RuntimeError(f"userinfo returned {userinfo.status_code}"))
        info["sub"] = userinfo.json().get("sub")

    if not info.get("sub"):
        raise HTTPException(
            status_code=401,
            detail="Google authentication token has no subject",
        )

    return info


def current_user_id(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
) -> str | None:
    if settings.billing_provider == "none":
        return None

    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=401,
            detail="Bearer Google authentication is required when billing is enabled",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return str(verify_google_access_token(credentials.credentials)["sub"])