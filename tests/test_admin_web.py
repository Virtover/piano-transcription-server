"""The admin web page: Cloudflare Access tokens checked by the page itself,
read-only routes, the headers on every answer, and lowering its own access,
which can only go down and is hard to do by accident."""

import base64
import datetime
import json
import re
import time
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from fastapi.testclient import TestClient
from google.auth import crypt, jwt

from app import controls
from app.admin import web
from app.admin.__main__ import main as admin_main
from app.admin_web import access, server
from app.admin_web.__main__ import least_privilege_problems
from app.admin_web.__main__ import main as service_main
from app.worker import tasks

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "app" / "admin_web" / "static"
TEAM = "example.cloudflareaccess.com"
AUD = "aud-tag-of-the-admin-application"
HOST = "admin.example.com"
ORIGIN = f"https://{HOST}"


def key_pair():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "access")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1)).not_valid_after(now + datetime.timedelta(days=30))
        .sign(key, hashes.SHA256())
    )
    private = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    return private.decode(), cert.public_bytes(serialization.Encoding.PEM).decode()


SIGNING_KEY, CERT = key_pair()
OTHER_KEY, OTHER_CERT = key_pair()


def token(email="chris@example.com", key=SIGNING_KEY, kid="key-1", **claims):
    now = int(time.time())
    payload = {"iss": f"https://{TEAM}", "aud": [AUD], "email": email, "iat": now, "nbf": now, "exp": now + 3600, **claims}
    return jwt.encode(crypt.RSASigner.from_string(key, kid), payload).decode()


class Keys:
    """Cloudflare's keys endpoint."""

    def __init__(self, certs=None, fail=False):
        self.certs = certs if certs is not None else {"key-1": CERT}
        self.fail = fail
        self.calls = 0

    def get(self, url, **kwargs):
        self.calls += 1
        assert url == f"https://{TEAM}/cdn-cgi/access/certs"
        assert kwargs["allow_redirects"] is False
        if self.fail:
            raise ConnectionError("unreachable")
        certs = [{"kid": kid, "cert": cert} for kid, cert in self.certs.items()]
        return type("Response", (), {"raise_for_status": lambda self: None, "json": lambda self: {"public_certs": certs}})()


def verifier(keys=None, clock=time.time):
    return access.AccessVerifier(TEAM, AUD, ["Chris@Example.com"], http=keys or Keys(), clock=clock)


# --- Access tokens -------------------------------------------------------------

def test_a_valid_token_names_the_administrator():
    assert verifier().verify(token()) == "chris@example.com"


@pytest.mark.parametrize(
    "make, reason",
    [
        (lambda: None, "no Cf-Access-Jwt-Assertion"),
        (lambda: "not-a-token", "not a JWT"),
        (lambda: token(email="someone@example.com"), "not in ADMIN_WEB_EMAILS"),
        (lambda: token(email=None), "not in ADMIN_WEB_EMAILS"),
        (lambda: token(iss="https://other.cloudflareaccess.com"), "issued by"),
        (lambda: token(aud=["another-application"]), "another Access application"),
        (lambda: token(exp=int(time.time()) - 600), "not valid"),
        (lambda: token(nbf=int(time.time()) + 600), "not valid yet"),
        (lambda: token(key=OTHER_KEY), "not valid"),
        (lambda: token(kid="key-2"), "no Access key"),
    ],
)
def test_anything_else_is_refused(make, reason):
    with pytest.raises(access.Denied, match=reason):
        verifier().verify(make())


def test_only_rs256_is_accepted():
    def segment(data):
        return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()

    unsigned = f"{segment({'alg': 'none', 'kid': 'key-1'})}.{segment({'email': 'chris@example.com'})}."
    with pytest.raises(access.Denied, match="not RS256"):
        verifier().verify(unsigned)


def test_a_wrong_team_domain_is_named():
    keys = Keys()
    other_team = token(key=OTHER_KEY, kid="their-key", iss="https://pianoweave.cloudflareaccess.com")

    with pytest.raises(access.Denied, match="issued by 'https://pianoweave.cloudflareaccess.com', but CLOUDFLARE_ACCESS_TEAM_DOMAIN gives"):
        verifier(keys).verify(other_team)
    # Refused before anything is fetched or trusted.
    assert keys.calls == 0


def test_a_single_audience_is_accepted():
    assert verifier().verify(token(aud=AUD)) == "chris@example.com"


def test_keys_are_cached_and_fetched_again_when_they_rotate():
    keys = Keys()
    now = [time.time()]
    check = verifier(keys, clock=lambda: now[0])
    signed = token()
    check.verify(signed)
    check.verify(signed)
    assert keys.calls == 1

    # Access signs with a new key: it is fetched, at most once a minute.
    keys.certs = {"key-1": CERT, "key-2": OTHER_CERT}
    rotated = token(key=OTHER_KEY, kid="key-2")
    with pytest.raises(access.Denied):
        check.verify(rotated)
    now[0] += access.UNKNOWN_KEY_INTERVAL + 1
    assert check.verify(rotated) == "chris@example.com"
    assert keys.calls == 2


def test_known_keys_keep_working_while_access_cannot_be_reached():
    keys = Keys()
    now = [time.time()]
    check = verifier(keys, clock=lambda: now[0])
    signed = token()
    check.verify(signed)
    keys.fail = True
    now[0] += access.KEYS_LIFETIME + 1

    assert check.verify(signed) == "chris@example.com"


# --- The service ---------------------------------------------------------------

@pytest.fixture
def page(api_context, default_settings):
    _, settings, billing = api_context
    settings.billing.provider = "google_play"
    settings.billing.products = {"starter": 30}
    billing.credit_purchase("user", "starter", "secret-token", "GPA.1", "user@example.com")
    admin = settings.admin_web
    admin.mode = "read-only"
    admin.hostname = HOST
    admin.emails = ["chris@example.com"]
    admin.access_team_domain = TEAM
    admin.access_aud = AUD
    app = server.create_app(settings, verifier(), clock=time.time)
    client = TestClient(app, base_url=ORIGIN, raise_server_exceptions=False)
    return client, settings, billing


def signed_in(**headers):
    return {access.HEADER: token(), **headers}


def changing():
    return signed_in(origin=ORIGIN)


def audit_rows(billing):
    return [dict(row) for row in billing.read("SELECT * FROM admin_actions ORDER BY id")]


def routes(app):
    return [(method, route.path) for route in app.routes for method in getattr(route, "methods", ()) if method != "HEAD"]


def test_every_route_needs_an_access_token(page):
    client, _, _ = page
    for method, path in routes(client.app):
        path = path.replace("{job}", "abcd1234").replace("{who}", "user")
        response = client.request(method, path, headers={access.HEADER: token(email="someone@example.com"), "origin": ORIGIN}, json={})
        assert response.status_code == 403, (method, path)
        assert "Cloudflare Access" in response.json()["detail"]


def test_the_only_changes_lower_its_own_access(page):
    client, _, _ = page

    changes = {path for method, path in routes(client.app) if method != "GET"}
    assert changes == {"/api/access/plan", "/api/access/lower"}
    assert not any("docs" in path or "openapi" in path for _, path in routes(client.app))


def test_other_hosts_are_refused(page):
    client, _, _ = page

    response = client.get("/api/me", headers={**signed_in(), "host": "evil.example.com"})

    assert response.status_code == 400


@pytest.mark.parametrize("path, status", [("/", 200), ("/api/me", 200), ("/api/me", 403), ("/missing", 404)])
def test_every_answer_has_the_security_headers(page, path, status):
    client, _, _ = page
    headers = signed_in() if status != 403 else {}

    response = client.get(path, headers=headers)

    assert response.status_code == status
    assert response.headers["content-security-policy"].startswith("default-src 'none'; script-src 'self'")
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["x-frame-options"] == "DENY"


def test_an_unexpected_error_shows_only_a_reference(page, monkeypatch):
    client, _, _ = page

    def broken():
        raise RuntimeError("password=hunter2 at /run/secrets")

    monkeypatch.setattr(server.status, "collect", broken)
    response = client.get("/api/status", headers=signed_in())

    assert response.status_code == 500
    assert "hunter2" not in response.text and "reference" in response.json()["detail"]
    assert response.headers["cache-control"] == "no-store"


def test_the_page_and_its_files(page):
    client, _, _ = page

    for path, media_type in [("/", "text/html"), ("/admin.js", "text/javascript"), ("/admin.css", "text/css")]:
        response = client.get(path, headers=signed_in())
        assert response.status_code == 200 and response.headers["content-type"].startswith(media_type)


def test_reading(page, monkeypatch):
    client, _, billing = page
    controls.block(tasks.redis, "user", "spam")

    assert client.get("/api/status", headers=signed_in()).json()["admin_web"]["effective"] == "read-only"
    assert client.get("/api/limits", headers=signed_in()).json()["blocked"][0]["reason"] == "spam"
    assert client.get("/api/jobs?status=failed&since=1d", headers=signed_in()).json() == []
    assert client.get("/api/billing?per=month", headers=signed_in()).status_code == 200
    assert client.get("/api/audit", headers=signed_in()).status_code == 200
    response = client.get("/api/jobs?status=everything", headers=signed_in())
    assert response.status_code == 400 and "status must be one of" in response.json()["detail"]
    assert client.get("/api/audit?limit=100000", headers=signed_in()).status_code == 422


def test_viewing_an_account_is_recorded_first(page, monkeypatch):
    client, _, billing = page

    account = client.get("/api/users/user@example.com", headers=signed_in()).json()
    found = client.get("/api/users?text=example", headers=signed_in()).json()

    assert account["minutes"] == 30
    assert "secret-token" not in json.dumps(account)
    assert [row["email"] for row in found] == ["user@example.com"]
    rows = audit_rows(billing)
    assert [(row["actor"], row["action"], row["target"]) for row in rows] == [
        ("web:chris@example.com", "web.user_view", "user"),
        ("web:chris@example.com", "web.user_search", None),
    ]
    # The search text is not kept.
    assert (rows[1]["detail"], rows[1]["reason"]) == ("1 found", None)

    # Nothing is shown when the view cannot be recorded.
    monkeypatch.setattr(billing, "log_admin_action", lambda *args: (_ for _ in ()).throw(RuntimeError("disk full")))
    response = client.get("/api/users/user", headers=signed_in())
    assert response.status_code == 400 and "minutes" not in response.text


# --- Lowering its own access ---------------------------------------------------

def plan(client, mode="disabled", reason="leaving for a week", headers=None):
    return client.post("/api/access/plan", headers=headers or changing(), json={"mode": mode, "reason": reason})


def lower(client, planned, confirm=None, reason="leaving for a week", headers=None, **changes):
    body = {"mode": planned["mode"], "reason": reason, "token": planned["token"], "expires": planned["expires"],
            "confirm": planned["mode"] if confirm is None else confirm, **changes}
    return client.post("/api/access/lower", headers=headers or changing(), json=body)


def test_lowering_takes_two_steps_and_the_typed_mode(page):
    client, _, billing = page

    planned = plan(client).json()
    assert planned["current"] == "read-only" and planned["type_to_confirm"] == "disabled"
    assert "web restore" in planned["undo"]
    assert web.state("read-only")["effective"] == "read-only"

    response = lower(client, planned, confirm="disable")
    assert response.status_code == 400 and "Type disabled" in response.json()["detail"]
    assert web.state("read-only")["effective"] == "read-only"

    response = lower(client, planned)
    assert response.status_code == 200 and response.json()["effective"] == "disabled"
    [row] = billing.admin_web_caps()
    assert (row["mode"], row["actor"], row["reason"]) == ("disabled", "web:chris@example.com", "leaving for a week")
    assert audit_rows(billing)[-1]["action"] == "web.lower"


def test_a_disabled_page_shows_nothing_but_how_to_restore_it(page):
    client, _, _ = page
    lower(client, plan(client).json())

    response = client.get("/api/status", headers=signed_in())
    assert response.status_code == 503 and "web restore" in response.json()["detail"]
    for path in ("/api/jobs", "/api/users/user", "/api/limits", "/api/audit", "/api/billing"):
        assert client.get(path, headers=signed_in()).status_code == 503, path
    me = client.get("/api/me", headers=signed_in()).json()
    assert me["effective"] == "disabled" and me["lower_to"] == []
    # Nothing on the page raises it again.
    assert plan(client, mode="read-only").status_code == 400


def test_access_cannot_be_raised_from_the_page(page):
    client, _, billing = page

    response = plan(client, mode="read-write")
    assert response.status_code == 400 and "can only be lowered" in response.json()["detail"]
    assert plan(client, mode="read-only").status_code == 400
    assert plan(client, mode="enabled").status_code == 400
    assert billing.admin_web_caps() == []


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"origin": "https://evil.example.com"},
        {"origin": ORIGIN, "sec-fetch-site": "cross-site"},
    ],
)
def test_changes_come_only_from_the_page_itself(page, headers):
    client, _, billing = page

    response = plan(client, headers=signed_in(**headers))

    assert response.status_code == 403
    assert billing.admin_web_caps() == []


def test_changes_must_be_json(page):
    client, _, _ = page

    response = client.post("/api/access/plan", headers=changing(), data={"mode": "disabled", "reason": "x"})

    assert response.status_code == 415


def test_a_reason_is_needed_and_must_not_name_anyone(page):
    client, _, _ = page

    assert "Give a --reason" in plan(client, reason="").json()["detail"]
    assert "email addresses" in plan(client, reason="asked by sam@example.org").json()["detail"]


@pytest.mark.parametrize(
    "tamper",
    [
        {"token": "0" * 64},
        {"expires": 0},
        {"mode": "read-only"},
    ],
)
def test_a_confirmation_cannot_be_forged_or_changed(page, tamper):
    client, _, billing = page
    planned = plan(client).json()

    response = lower(client, planned, **tamper)

    assert response.status_code in (400, 409)
    assert billing.admin_web_caps() == []


def test_a_confirmation_is_for_one_person_reason_and_moment(page):
    _, settings, billing = page
    now = [time.time()]
    two = access.AccessVerifier(TEAM, AUD, ["chris@example.com", "sam@example.com"], http=Keys())
    client = TestClient(server.create_app(settings, two, clock=lambda: now[0]), base_url=ORIGIN)
    planned = plan(client).json()

    sam = {access.HEADER: token(email="sam@example.com"), "origin": ORIGIN}
    assert lower(client, planned, headers=sam).status_code == 409
    assert lower(client, planned, reason="something else").status_code == 409
    now[0] += server.TOKEN_SECONDS + 1
    assert lower(client, planned).status_code == 409
    assert billing.admin_web_caps() == []


def test_the_mode_is_read_once_at_start(page):
    client, settings, _ = page

    settings.admin_web.mode = "read-write"

    assert client.get("/api/me", headers=signed_in()).json()["configured"] == "read-only"


def test_an_unreadable_database_counts_as_disabled(page, monkeypatch):
    client, _, billing = page

    def broken():
        raise RuntimeError("connection refused")

    monkeypatch.setattr(billing, "admin_web_caps", broken)

    assert client.get("/api/me", headers=signed_in()).json()["effective"] == "disabled"
    assert client.get("/api/status", headers=signed_in()).status_code == 503


# --- The page's code -------------------------------------------------------------

def test_the_page_never_inserts_html_or_loads_anything_else():
    script = (STATIC / "admin.js").read_text(encoding="utf-8")
    html = (STATIC / "index.html").read_text(encoding="utf-8")

    for unsafe in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"):
        assert unsafe not in script, unsafe
    assert not re.search(r"https?://", script + (STATIC / "admin.css").read_text(encoding="utf-8"))
    # No inline script or style: the policy allows only the page's own files.
    assert re.findall(r"<script[^>]*>", html) == ['<script src="/admin.js" defer>']
    assert "<style" not in html and " style=" not in html
    assert not re.search(r"\son[a-z]+\s*=", html)


def test_lowering_is_never_one_keystroke_away():
    script = (STATIC / "admin.js").read_text(encoding="utf-8")

    assert '"Enter"' in script and "preventDefault" in script
    # Only a dialog's Close button ever gets focus by itself.
    assert "autofocus" not in script
    assert re.findall(r"(\w+)\.focus\(", script) == ["close"]
    assert "checked: true" not in script
    assert "<form" not in (STATIC / "index.html").read_text(encoding="utf-8")


# --- Starting ------------------------------------------------------------------

def test_disabled_does_not_start(default_settings, monkeypatch):
    monkeypatch.setattr(server, "create_app", lambda *args, **kwargs: pytest.fail("started"))

    assert service_main() == 0


def test_incomplete_settings_do_not_start(default_settings, caplog):
    default_settings.admin_web.mode = "read-only"

    assert service_main() == 1
    assert "ADMIN_WEB_HOSTNAME is required" in caplog.text


def test_credentials_that_may_write_are_refused(api_context):
    # fakeredis and SQLite accept everything, as a wrongly set up Redis or
    # PostgreSQL would.
    problems = least_privilege_problems("read-only")

    assert len(problems) == 4
    assert all("admin-read-only" in problem for problem in problems)
    assert len(least_privilege_problems("read-write")) == 3
    assert not tasks.redis.exists("controls:admin_web_probe")


# --- Restoring from the command line ---------------------------------------------

def test_only_the_command_line_restores_access(page, capsys, monkeypatch):
    client, settings, billing = page
    lower(client, plan(client).json())

    assert admin_main(["web"]) == 0
    assert "Admin web page:  disabled (ADMIN_WEB_MODE is read-only)" in capsys.readouterr().out
    assert admin_main(["web", "restore", "--yes"]) == 1
    assert "Give a --reason" in capsys.readouterr().err
    assert admin_main(["web", "restore", "--yes", "--reason", "back from holiday", "--by", "chris"]) == 0

    assert billing.admin_web_caps() == []
    assert audit_rows(billing)[-1]["action"] == "web.restore"
    assert client.get("/api/status", headers=signed_in()).status_code == 200
    assert admin_main(["web", "restore", "--yes", "--reason", "again"]) == 1
    assert "was not lowered" in capsys.readouterr().err


def test_status_shows_the_page_s_access(page, capsys):
    client, _, _ = page
    lower(client, plan(client).json())

    admin_main(["status"])

    assert "Admin web     disabled, lowered from read-only (see: web)" in capsys.readouterr().out


# --- Settings, Compose, and the requirements ----------------------------------------

def complete(settings):
    admin = settings.admin_web
    admin.mode = "read-only"
    admin.hostname = HOST
    admin.emails = ["chris@example.com"]
    admin.access_team_domain = TEAM
    admin.access_aud = AUD
    settings.tunnel.compose_profiles = "cloudflare,admin"
    settings.tunnel.hostname = "piano.example.com"


def test_complete_settings_pass(default_settings):
    from app import config

    complete(default_settings)

    assert config.admin_web_problems(default_settings) == []


@pytest.mark.parametrize(
    "change, problem",
    [
        (lambda s: setattr(s.admin_web, "hostname", None), "ADMIN_WEB_HOSTNAME is required"),
        (lambda s: setattr(s.admin_web, "emails", []), "ADMIN_WEB_EMAILS is required"),
        (lambda s: setattr(s.admin_web, "access_aud", None), "CLOUDFLARE_ACCESS_AUD is required"),
        (lambda s: setattr(s.admin_web, "access_team_domain", "example.com"), "your team domain"),
        (lambda s: setattr(s.admin_web, "hostname", "piano.example.com"), "must differ from CLOUDFLARE_TUNNEL_HOSTNAME"),
        (lambda s: setattr(s.tunnel, "compose_profiles", "cloudflare"), "needs admin in COMPOSE_PROFILES"),
        (lambda s: setattr(s.tunnel, "compose_profiles", "admin"), "needs the Cloudflare Tunnel"),
        (lambda s: setattr(s.billing, "database_url", ""), "needs the PostgreSQL billing database"),
        (lambda s: setattr(s.admin_web, "mode", "disabled"), "remove it from COMPOSE_PROFILES"),
    ],
)
def test_incomplete_or_unsafe_settings_are_refused(default_settings, change, problem):
    from app import config

    complete(default_settings)
    change(default_settings)

    assert any(problem in text for text in config.admin_web_problems(default_settings)), config.admin_web_problems(default_settings)


def test_the_service_in_compose_has_only_what_its_mode_needs():
    import yaml

    service = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))["services"]["admin"]

    assert service["profiles"] == ["admin"]
    assert "ports" not in service
    assert service["read_only"] is True and service["cap_drop"] == ["ALL"]
    assert "no-new-privileges:true" in service["security_opt"]
    assert service["command"] == ["python", "-m", "app.admin_web"]
    # Credentials follow the mode: in read-only mode there are no others.
    assert service["environment"]["REDIS_URL"] == "redis://admin-${ADMIN_WEB_MODE:-disabled}@redis:6379/0"
    assert service["environment"]["BILLING_DATABASE_URL"] == "postgresql://admin-${ADMIN_WEB_MODE:-disabled}@postgres:5432/billing"
    assert sorted(service["volumes"]) == sorted([
        "data:/data:ro",
        "./billing-data/backups:/billing/backups:ro",
        "./secrets/redis/admin-${ADMIN_WEB_MODE:-disabled}:/run/redis/password:ro",
        "./secrets/admin-web/postgres-${ADMIN_WEB_MODE:-disabled}:/run/secrets/postgres_password:ro",
    ])


def test_the_roles_match_the_modes_compose_names():
    from app.billing import ADMIN_WEB_ROLES

    assert ADMIN_WEB_ROLES == {"read-only": "admin-read-only", "read-write": "admin-read-write"}


def test_the_redis_user_of_read_only_cannot_write():
    rules = next(
        line.split()[3:] for line in (ROOT / "redis" / "users.acl").read_text(encoding="utf-8").splitlines()
        if line.startswith("user admin-read-only ")
    )

    assert rules[0] == "#{admin-read-only}"
    assert "-@all" in rules and "+@read" in rules and "-@dangerous" in rules
    granted = [rule for rule in rules if rule.startswith("+") and rule != "+@read"]
    assert granted == ["+ping", "+multi", "+exec", "+discard", "+client|setinfo"]
    assert "~*" not in rules and "&*" not in rules


def test_the_requirements_state_intents_with_reasons(default_settings):
    from app.requirements import admin_web_sections

    complete(default_settings)
    [section] = admin_web_sections(default_settings)

    assert section.enabled
    values = " ".join(requirement.value for requirement in section.requirements)
    assert f"{HOST} on the same tunnel, sent to the service http://admin:8100" in values
    assert "chris@example.com" in values and "second factor" in values
    assert all(requirement.reason for requirement in section.requirements)


class Http:
    def __init__(self, answers):
        self.answers = answers

    def get(self, url, **kwargs):
        assert kwargs["allow_redirects"] is False
        answer = self.answers[url]
        if isinstance(answer, Exception):
            raise answer
        return answer


def answer(status, headers=None, payload=None):
    return type("Response", (), {
        "status_code": status,
        "headers": headers or {},
        "json": lambda self: payload or {},
        "raise_for_status": lambda self: None,
    })()


def online(default_settings, public, keys=None, internal=None):
    from app import online_check

    complete(default_settings)
    return online_check.check_admin_web(default_settings, Http({
        f"https://{HOST}/api/me": public,
        f"https://{TEAM}/cdn-cgi/access/certs": keys or answer(200, payload={"public_certs": [{"kid": "k", "cert": "c"}]}),
        "http://admin:8100/api/me": internal or answer(403),
    }))


def test_online_check_passes_behind_access(default_settings):
    results = online(default_settings, answer(302, {"location": f"https://{TEAM}/cdn-cgi/access/login"}))

    assert [result.status for result in results] == ["ok", "ok", "ok"]


def test_online_check_fails_when_the_page_answers_without_a_sign_in(default_settings):
    result = online(default_settings, answer(200))[0]

    assert result.status == "fail" and "without a sign-in" in result.detail


def test_online_check_fails_when_the_service_answers_without_a_token(default_settings):
    results = online(default_settings, answer(302, {"location": f"https://{TEAM}/"}), internal=answer(200))

    assert results[2].status == "fail"


# --- Read-write mode -------------------------------------------------------------

@pytest.fixture
def writable(page):
    _, settings, billing = page
    settings.admin_web.mode = "read-write"
    two = access.AccessVerifier(TEAM, AUD, ["chris@example.com", "sam@example.com"], http=Keys())
    client = TestClient(server.create_app(settings, two, clock=time.time), base_url=ORIGIN, raise_server_exceptions=False)
    return client, settings, billing


def act(client, action, args=None, reason="checked", headers=None):
    return client.post("/api/actions/plan", headers=headers or changing(), json={"action": action, "args": args or {}, "reason": reason})


def apply(client, planned, headers=None):
    return client.post("/api/actions/apply", headers=headers or changing(), json={"token": planned["token"]})


def test_read_only_has_no_actions(page):
    client, _, _ = page

    assert client.get("/api/me", headers=signed_in()).json()["actions"] == []
    assert act(client, "user.credit", {"user": "user", "minutes": 5}).status_code in (404, 405)


def test_read_write_lists_its_actions_and_nothing_irreversible(writable):
    client, _, _ = writable

    actions = client.get("/api/me", headers=signed_in()).json()["actions"]

    assert "user.credit" in actions and "job.cancel" in actions and "limits.pause" in actions
    assert not any("delete" in action or "requeue" in action for action in actions)


def test_a_change_is_planned_then_applied_once(writable):
    client, _, billing = writable

    planned = act(client, "user.credit", {"user": "user@example.com", "minutes": 20}, "refund for a failed job").json()
    assert planned["plan"]["balance_after"] == 50 and planned["reason"] == "refund for a failed job"
    assert billing.get_balance("user") == 30

    response = apply(client, planned)
    assert response.status_code == 200 and response.json()["result"]["balance_after"] == 50
    assert billing.get_balance("user") == 50
    row = audit_rows(billing)[-1]
    assert (row["actor"], row["action"], row["reason"]) == ("web:chris@example.com", "user.credit", "refund for a failed job")

    # The same confirmation never works twice.
    assert apply(client, planned).status_code == 409
    assert billing.get_balance("user") == 50


def test_the_browser_cannot_change_what_was_confirmed(writable):
    client, _, billing = writable
    planned = act(client, "user.credit", {"user": "user", "minutes": 5}).json()

    response = client.post("/api/actions/apply", headers=changing(), json={
        "token": planned["token"], "action": "user.credit", "args": {"user": "user", "minutes": 500},
    })

    assert response.status_code == 200
    assert billing.get_balance("user") == 35


def test_a_confirmation_belongs_to_one_administrator(writable):
    client, _, billing = writable
    planned = act(client, "user.credit", {"user": "user", "minutes": 5}).json()

    response = apply(client, planned, headers={access.HEADER: token(email="sam@example.com"), "origin": ORIGIN})

    assert response.status_code == 403
    assert billing.get_balance("user") == 30


def test_a_change_meanwhile_needs_a_new_review(writable):
    client, _, billing = writable
    planned = act(client, "user.credit", {"user": "user", "minutes": 5}).json()
    billing.adjust_minutes("user", 1, "elsewhere")

    response = apply(client, planned)

    assert response.status_code == 409 and "review it again" in response.json()["detail"]
    assert billing.get_balance("user") == 31


def test_a_confirmation_expires(writable):
    client, _, billing = writable
    planned = act(client, "user.credit", {"user": "user", "minutes": 5}).json()
    tasks.redis.delete(*tasks.redis.keys(server.CONFIRM_PREFIX + "*"))

    assert apply(client, planned).status_code == 409
    assert billing.get_balance("user") == 30


def test_the_token_is_not_stored_as_it_is(writable):
    client, _, _ = writable
    planned = act(client, "user.credit", {"user": "user", "minutes": 5}).json()

    [key] = tasks.redis.keys(server.CONFIRM_PREFIX + "*")
    assert planned["token"] not in key
    assert 0 < tasks.redis.ttl(key) <= server.TOKEN_SECONDS


@pytest.mark.parametrize(
    "action, args, reason, problem",
    [
        ("user.credit", {"user": "user", "minutes": 601}, "big", "at most 600 minutes"),
        ("user.credit", {"user": "user", "minutes": "5"}, "text", "whole number"),
        ("user.credit", {"user": "user", "minutes": 5}, "", "Give a --reason"),
        ("user.credit", {"user": "user", "minutes": 5}, "asked by sam@example.org", "email addresses"),
        ("job.cancel", {"job": "abcd"}, "stuck", "at least 8 characters"),
        ("limits.block", {"user": "user"}, None, "Give a --reason"),
        ("limits.pause", {"for": "soon"}, None, "for must use a format"),
        ("limits.per_user", {"limit": 0}, None, "1 or more"),
        ("user.delete", {"user": "user"}, "request", "cannot"),
        ("job.requeue", {"job": "abcd1234"}, "retry", "cannot"),
    ],
)
def test_invalid_changes_are_refused_before_anything_happens(writable, action, args, reason, problem):
    client, _, billing = writable

    response = act(client, action, args, reason)

    assert response.status_code == 400 and problem in response.json()["detail"], response.json()
    assert tasks.redis.keys(server.CONFIRM_PREFIX + "*") == []
    assert billing.get_balance("user") == 30


def test_changes_come_only_from_the_page(writable):
    client, _, _ = writable

    assert act(client, "limits.pause", headers=signed_in()).status_code == 403
    assert act(client, "limits.pause", headers=signed_in(origin="https://evil.example.com")).status_code == 403
    outsider = {access.HEADER: token(email="someone@example.com"), "origin": ORIGIN}
    assert act(client, "limits.pause", headers=outsider).status_code == 403


def test_changes_are_rate_limited(writable, monkeypatch):
    _, settings, billing = writable
    monkeypatch.setattr(server, "CHANGES_PER_HOUR", 2)
    client = TestClient(server.create_app(settings, verifier(), clock=time.time), base_url=ORIGIN, raise_server_exceptions=False)

    for _ in range(2):
        assert apply(client, act(client, "user.credit", {"user": "user", "minutes": 1}).json()).status_code == 200
    response = apply(client, act(client, "user.credit", {"user": "user", "minutes": 1}).json())

    assert response.status_code == 429
    assert billing.get_balance("user") == 32


def test_limits_from_the_page_apply_to_users(writable, api_context):
    client, _, _ = writable
    api = api_context[0]

    apply(client, act(client, "limits.pause", {"message": "Back at six", "for": "1h"}).json())
    response = api.post("/api/transcriptions", headers={"Authorization": "Bearer user"}, json={"source_url": "https://example.com/video"})
    assert response.status_code == 503 and response.json()["detail"] == "Back at six"

    apply(client, act(client, "limits.resume").json())
    assert controls.paused(tasks.redis) is None


def test_cancelling_from_the_page_charges_like_the_user(writable, api_context):
    client, _, billing = writable
    api = api_context[0]
    job_id = api.post("/api/transcriptions", headers={"Authorization": "Bearer user"}, json={"source_url": "https://example.com/video"}).json()["job_id"]
    tasks.redis.hset(tasks.job_key(job_id), mapping={"status": "processing", "progress": "0.5"})

    planned = act(client, "job.cancel", {"job": job_id}, "stuck").json()
    assert planned["serious"] is True and planned["plan"]["charge"] == 1
    apply(client, planned)

    assert tasks.redis.hget(tasks.job_key(job_id), "status") == "cancelled"
    assert billing.get_balance("user") == 29


def test_a_lowered_page_makes_no_more_changes(writable):
    client, _, billing = writable
    planned = act(client, "user.credit", {"user": "user", "minutes": 5}).json()

    lowered = client.post("/api/access/plan", headers=changing(), json={"mode": "read-only", "reason": "travelling"}).json()
    assert lower(client, lowered, reason="travelling").json()["actions"] == []

    assert apply(client, planned).status_code == 403
    assert act(client, "user.credit", {"user": "user", "minutes": 5}).status_code == 403
    assert billing.get_balance("user") == 30


def test_lowering_needs_the_new_access_name_typed_exactly(writable):
    client, _, billing = writable
    planned = client.post("/api/access/plan", headers=changing(), json={"mode": "read-only", "reason": "travelling"}).json()
    assert planned["type_to_confirm"] == "read-only"

    for typed in ("", "readonly", "Read-only", "disabled", "read-write", "yes"):
        assert lower(client, planned, confirm=typed, reason="travelling").status_code == 400, typed
    assert billing.admin_web_caps() == []
    assert lower(client, planned, confirm="read-only", reason="travelling").status_code == 200
