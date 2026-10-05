import hashlib
import secrets
import sqlite3
import time
import uuid
from typing import Any

import jwt
from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from google.auth.transport.requests import Request
from google.oauth2 import id_token
from pydantic import BaseModel, Field

from app.config import settings


bearer_scheme = HTTPBearer(auto_error=False)
router = APIRouter(prefix="/auth", tags=["auth"])


class SignInRequest(BaseModel):
    google_id_token: str = Field(min_length=1)


class RefreshRequest(BaseModel):
    refresh_token: str = Field(min_length=1)


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int


def google_oauth_client_id() -> str | None:
    return settings.google_oauth_client_id


def _connect() -> sqlite3.Connection:
    connection = sqlite3.connect(settings.billing_database_path, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute(
        """CREATE TABLE IF NOT EXISTS auth_refresh_tokens (
            token_hash TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            family_id TEXT NOT NULL,
            expires_at INTEGER NOT NULL,
            used_at INTEGER,
            revoked_at INTEGER
        )"""
    )
    return connection


def verify_google_id_token(token: str) -> dict[str, Any]:
    client_id = google_oauth_client_id()
    if not client_id:
        raise HTTPException(status_code=503, detail="Google authentication is not configured")
    try:
        payload = id_token.verify_oauth2_token(token, Request(), audience=client_id)
    except ValueError as error:
        raise HTTPException(status_code=401, detail="Invalid Google authentication token") from error
    except Exception as error:
        raise HTTPException(status_code=503, detail="Google authentication service is unavailable") from error
    if not payload.get("sub"):
        raise HTTPException(status_code=401, detail="Google authentication token has no subject")
    return payload


def _hash_refresh_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _access_token(user_id: str) -> tuple[str, int]:
    if not settings.auth_jwt_secret:
        raise HTTPException(status_code=503, detail="Authentication signing is not configured")
    now = int(time.time())
    expires_at = now + settings.auth_access_token_minutes * 60
    token = jwt.encode(
        {"sub": user_id, "iat": now, "exp": expires_at},
        settings.auth_jwt_secret,
        algorithm="HS256",
    )
    return token, expires_at - now


def _issue_token_pair(user_id: str, family_id: str | None = None) -> TokenResponse:
    refresh_token = secrets.token_urlsafe(48)
    now = int(time.time())
    with _connect() as connection:
        connection.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,))
        connection.execute(
            "INSERT INTO auth_refresh_tokens VALUES (?, ?, ?, ?, NULL, NULL)",
            (
                _hash_refresh_token(refresh_token),
                user_id,
                family_id or str(uuid.uuid4()),
                now + settings.auth_refresh_token_days * 24 * 60 * 60,
            ),
        )
    access_token, expires_in = _access_token(user_id)
    return TokenResponse(access_token=access_token, refresh_token=refresh_token, expires_in=expires_in)


@router.post("/sign-in", response_model=TokenResponse)
def sign_in(request: SignInRequest) -> TokenResponse:
    payload = verify_google_id_token(request.google_id_token)
    return _issue_token_pair(str(payload["sub"]))


@router.post("/refresh", response_model=TokenResponse)
def refresh(request: RefreshRequest) -> TokenResponse:
    token_hash = _hash_refresh_token(request.refresh_token)
    now = int(time.time())
    reuse_detected = False
    with _connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        stored = connection.execute(
            "SELECT * FROM auth_refresh_tokens WHERE token_hash = ?", (token_hash,)
        ).fetchone()
        if not stored:
            raise HTTPException(status_code=401, detail="Invalid refresh token")
        if stored["used_at"] is not None or stored["revoked_at"] is not None:
            connection.execute(
                "UPDATE auth_refresh_tokens SET revoked_at = ? WHERE family_id = ?",
                (now, stored["family_id"]),
            )
            reuse_detected = True
            user_id = ""
            family_id = ""
        elif stored["expires_at"] <= now:
            raise HTTPException(status_code=401, detail="Invalid refresh token")
        else:
            connection.execute("UPDATE auth_refresh_tokens SET used_at = ? WHERE token_hash = ?", (now, token_hash))
            user_id = str(stored["user_id"])
            family_id = str(stored["family_id"])
    if reuse_detected:
        raise HTTPException(status_code=401, detail="Refresh token reuse detected")
    return _issue_token_pair(user_id, family_id)


def current_user_id(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
) -> str | None:
    if settings.billing_provider == "none":
        return None
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(status_code=401, detail="Bearer access token is required", headers={"WWW-Authenticate": "Bearer"})
    if not settings.auth_jwt_secret:
        raise HTTPException(status_code=503, detail="Authentication signing is not configured")
    try:
        payload = jwt.decode(credentials.credentials, settings.auth_jwt_secret, algorithms=["HS256"])
    except jwt.InvalidTokenError as error:
        raise HTTPException(status_code=401, detail="Invalid access token", headers={"WWW-Authenticate": "Bearer"}) from error
    user_id = payload.get("sub")
    if not isinstance(user_id, str) or not user_id:
        raise HTTPException(status_code=401, detail="Invalid access token")
    return user_id
