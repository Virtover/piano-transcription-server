import json
from pathlib import Path
from typing import Any

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from google.auth.transport.requests import Request
from google.oauth2 import id_token

from app.config import settings


bearer_scheme = HTTPBearer(auto_error=False)


def google_oauth_client_id() -> str | None:
    configured_file = settings.google_oauth_client_file
    if configured_file:
        configured_path = Path(configured_file)
        path = (
            configured_path
            if configured_path.is_absolute()
            else Path("/run/secrets") / configured_path.name
        )
        try:
            with path.open(encoding="utf-8") as file:
                configuration = json.load(file)
            client_configuration = (
                configuration.get("web")
                or configuration.get("installed")
                or {}
            )
            client_id = client_configuration.get("client_id")
        except (OSError, TypeError, ValueError) as error:
            raise HTTPException(
                status_code=503,
                detail="Google authentication credentials cannot be read",
            ) from error
        if not isinstance(client_id, str) or not client_id:
            raise HTTPException(
                status_code=503,
                detail="Google authentication client ID is missing",
            )
        return client_id

    return settings.google_oauth_client_id


def verify_google_id_token(token: str) -> dict[str, Any]:
    client_id = google_oauth_client_id()
    if not client_id:
        raise HTTPException(
            status_code=503,
            detail="Google authentication is not configured",
        )

    try:
        payload = id_token.verify_oauth2_token(
            token,
            Request(),
            audience=client_id,
        )
    except ValueError as error:
        raise HTTPException(
            status_code=401,
            detail="Invalid Google authentication token",
        ) from error
    except Exception as error:
        raise HTTPException(
            status_code=503,
            detail="Google authentication service is unavailable",
        ) from error

    if not payload.get("sub"):
        raise HTTPException(
            status_code=401,
            detail="Google authentication token has no subject",
        )

    return payload


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

    return str(verify_google_id_token(credentials.credentials)["sub"])
