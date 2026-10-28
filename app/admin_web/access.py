"""Who is asking: the Cloudflare Access token on every request.

Cloudflare Access signs in an administrator before a request reaches the
admin service, and adds a token, signed by the team's key, in the
Cf-Access-Jwt-Assertion header. The service checks that token itself, so it
stays closed even if Access is missing from the hostname, and a process on
the Compose network cannot pretend to be Cloudflare. A request is accepted
only when the token is signed with RS256 by one of the team's current keys,
its issuer is the team domain, its audience includes the Access
application's AUD tag, it is within its lifetime, and its email address is
in ADMIN_WEB_EMAILS.
"""

import base64
import json
import logging
import threading
import time

import requests
from google.auth import exceptions as auth_exceptions
from google.auth import jwt

logger = logging.getLogger(__name__)

HEADER = "Cf-Access-Jwt-Assertion"
ALGORITHM = "RS256"
# Access rotates its keys every few weeks and accepts the old one for a week.
KEYS_LIFETIME = 60 * 60
# A token signed by a key not in the cache fetches the keys again, at most this often.
UNKNOWN_KEY_INTERVAL = 60
CLOCK_SKEW_SECONDS = 30
REQUEST_TIMEOUT = 10


class Denied(Exception):
    """The request does not come from an allowed administrator. The message
    is for the log; the client is only told to sign in."""


def keys_url(team_domain: str) -> str:
    return f"https://{team_domain}/cdn-cgi/access/certs"


def _segment(token: str, index: int) -> dict:
    """A part of the token, decoded but not verified."""
    try:
        segment = token.split(".")[index]
        data = json.loads(base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4)))
    except (IndexError, ValueError) as error:
        raise Denied("the token is not a JWT") from error
    if not isinstance(data, dict):
        raise Denied("the token is not a JWT")
    return data


def _header(token: str) -> dict:
    return _segment(token, 0)


def _claims(token: str) -> dict:
    return _segment(token, 1)


class AccessVerifier:
    def __init__(self, team_domain: str, audience: str, emails: list[str], http=requests, clock=time.time):
        self.issuer = f"https://{team_domain}"
        self.url = keys_url(team_domain)
        self.audience = audience
        self.emails = frozenset(email.strip().lower() for email in emails)
        self._http = http
        self._clock = clock
        self._keys: dict[str, str] = {}
        self._fetched_at = 0.0
        self._lock = threading.Lock()

    def _fetch(self) -> None:
        response = self._http.get(self.url, timeout=REQUEST_TIMEOUT, allow_redirects=False)
        response.raise_for_status()
        # Matched by key ID; public_cert alone would miss the previous key.
        keys = {entry["kid"]: entry["cert"] for entry in response.json().get("public_certs", [])}
        if not keys:
            raise ValueError("no keys in the answer")
        self._keys = keys
        self._fetched_at = self._clock()

    def key(self, key_id: str) -> str:
        with self._lock:
            age = self._clock() - self._fetched_at
            stale = age > KEYS_LIFETIME or (key_id not in self._keys and age > UNKNOWN_KEY_INTERVAL)
            if stale:
                try:
                    self._fetch()
                except Exception as error:
                    # The keys already known keep working until Access is reachable again.
                    logger.warning("cannot fetch the Cloudflare Access keys from %s: %s", self.url, error)
            if key_id not in self._keys:
                raise Denied(f"no Access key {key_id!r}")
            return self._keys[key_id]

    def verify(self, token: str | None) -> str:
        """The administrator's email address, or Denied."""
        if not token:
            raise Denied(f"no {HEADER} header: the request did not come through Cloudflare Access")
        header = _header(token)
        if header.get("alg") != ALGORITHM:
            raise Denied(f"the token is signed with {header.get('alg')!r}, not {ALGORITHM}")
        key_id = header.get("kid")
        if not isinstance(key_id, str):
            raise Denied("the token names no key")
        # Read before the signature is checked, only to refuse: a token from
        # another team would otherwise fail as an unknown key, which hides
        # a wrong CLOUDFLARE_ACCESS_TEAM_DOMAIN.
        issuer = _claims(token).get("iss")
        if issuer != self.issuer:
            raise Denied(
                f"the token was issued by {issuer!r}, but CLOUDFLARE_ACCESS_TEAM_DOMAIN gives {self.issuer}"
            )
        try:
            claims = jwt.decode(token, certs={key_id: self.key(key_id)}, clock_skew_in_seconds=CLOCK_SKEW_SECONDS)
        except (ValueError, auth_exceptions.GoogleAuthError) as error:
            raise Denied(f"the token is not valid: {error}") from error
        if claims.get("iss") != self.issuer:
            raise Denied(f"the token was issued by {claims.get('iss')!r}, not {self.issuer}")
        audience = claims.get("aud")
        audiences = audience if isinstance(audience, list) else [audience]
        if self.audience not in audiences:
            raise Denied("the token is for another Access application")
        not_before = claims.get("nbf")
        if isinstance(not_before, (int, float)) and not_before > self._clock() + CLOCK_SKEW_SECONDS:
            raise Denied("the token is not valid yet")
        email = claims.get("email")
        if not isinstance(email, str) or email.strip().lower() not in self.emails:
            raise Denied(f"{email!r} is not in ADMIN_WEB_EMAILS")
        return email.strip().lower()
