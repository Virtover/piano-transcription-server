"""Attacks an outsider could try against the API, each refused. The
component tests check each defense; these check them from the outside, the
way an attacker would combine them."""
import base64
import socket

import pytest
from pydantic import SecretStr

from app import url_safety
from app.api.routes import transcriptions as routes
from app.worker.tasks import job_key

PASSWORD = "correct-horse-battery"


def bearer(user):
    return {"Authorization": f"Bearer {user}"}


@pytest.fixture
def billing_on(api_context):
    client, settings, billing = api_context
    settings.billing.provider = "google_play"
    settings.billing.free_minutes = 100
    return client, settings, billing


# --- 1. Server password bypass ------------------------------------------------

@pytest.mark.parametrize("path", [
    "/health/", "//health", "/HEALTH", "/health/../api/server-info", "/./api/server-info",
    "/api/server-info/", "/health%2F..%2Fapi%2Fserver-info", "/api//server-info",
    "/docs/", "/openapi.json?x=/health", "/api/transcriptions/x",
])
def test_password_bypass_paths(api_context, path):
    client, settings, _ = api_context
    settings.secrets.server_password = SecretStr(PASSWORD)
    response = client.get(path, follow_redirects=False)
    assert response.status_code in (401, 404, 307), (path, response.status_code)
    assert response.status_code != 200, path


@pytest.mark.parametrize("headers", [
    {"X-Server-Password": PASSWORD + " "},
    {"X-Server-Password": PASSWORD[:-1]},
    {"X-Server-Password": ""},
    {"Authorization": "Basic " + base64.b64encode(f"{PASSWORD}:".encode()).decode()},  # password as username
    {"Authorization": "Basic " + base64.b64encode(b"x").decode()},
    {"Authorization": "Basic !!!"},
    {"Authorization": "Bearer " + PASSWORD},
])
def test_password_wrong_forms(api_context, headers):
    client, settings, _ = api_context
    settings.secrets.server_password = SecretStr(PASSWORD)
    assert client.get("/api/server-info", headers=headers).status_code == 401


# --- 2. Cross-user access (IDOR) ----------------------------------------------

def test_other_user_cannot_touch_a_job(billing_on, tmp_path):
    client, settings, _ = billing_on
    created = client.post("/api/transcriptions", json={"source_url": "https://youtube.com/watch?v=1"}, headers=bearer("alice"))
    assert created.status_code == 202, created.text
    job_id = created.json()["job_id"]
    routes.redis.hset(job_key(job_id), mapping={"status": "completed", "result": str(tmp_path / "x.mid")})
    for method, path in [("get", ""), ("get", "/midi"), ("delete", "")]:
        response = getattr(client, method)(f"/api/transcriptions/{job_id}{path}", headers=bearer("mallory"))
        assert response.status_code == 404, (method, path, response.status_code)
    # And with no token at all.
    assert client.get(f"/api/transcriptions/{job_id}").status_code == 401


def test_same_idempotency_key_does_not_return_another_users_job(billing_on):
    client, _, _ = billing_on
    key = {"Idempotency-Key": "shared-key"}
    alice = client.post("/api/transcriptions", json={"source_url": "https://youtube.com/watch?v=1"}, headers={**bearer("alice"), **key})
    mallory = client.post("/api/transcriptions", json={"source_url": "https://youtube.com/watch?v=1"}, headers={**bearer("mallory"), **key})
    assert alice.json()["job_id"] != mallory.json()["job_id"]


# --- 3. Path traversal ----------------------------------------------------------

@pytest.mark.parametrize("job_id", ["..%2F..%2Fetc%2Fpasswd", "%2e%2e", "..", "*", "a\\..\\b"])
def test_job_id_traversal(api_context, job_id):
    client, _, _ = api_context
    for path in (f"/api/transcriptions/{job_id}", f"/api/transcriptions/{job_id}/midi"):
        assert client.get(path).status_code in (404, 405), path


def test_result_path_outside_data_is_never_served(api_context, tmp_path):
    client, _, _ = api_context
    secret = tmp_path / "secret.txt"
    secret.write_text("secret")
    routes.redis.hset(job_key("j1"), mapping={"status": "completed", "result": str(secret), "user_id": "anonymous"})
    response = client.get("/api/transcriptions/j1/midi")
    assert response.status_code == 500 and "secret" not in response.text
    routes.redis.hset(job_key("j2"), mapping={"status": "completed", "result": str(tmp_path / "data" / ".." / "secret.txt"), "user_id": "anonymous"})
    assert client.get("/api/transcriptions/j2/midi").status_code == 500


# --- 4. Upload abuse --------------------------------------------------------------

def test_upload_filename_cannot_choose_where_it_is_written(api_context, tmp_path, monkeypatch):
    client, settings, _ = api_context
    monkeypatch.setattr(routes, "probe_audio", lambda path, measure, max_seconds: 10.0)
    response = client.post("/api/transcriptions/upload", params={"filename": "../../../../tmp/evil.sh"}, content=b"RIFF")
    assert response.status_code == 202, response.text
    job_id = response.json()["job_id"]
    written = list((tmp_path / "data").rglob("*"))
    assert all(path.is_relative_to(tmp_path / "data" / "jobs" / job_id) or path == tmp_path / "data" / "jobs" for path in written if path.is_file()), written
    assert routes.redis.hget(job_key(job_id), "title") == "evil"


def test_chunked_upload_without_length_is_cut_at_the_limit(api_context, monkeypatch):
    client, settings, _ = api_context
    settings.limits.max_upload_size_mb = 1
    probed = []
    monkeypatch.setattr(routes, "probe_audio", lambda *a: probed.append(a) or 10.0)

    def body():
        for _ in range(3):
            yield b"x" * (1024 * 1024)

    response = client.post("/api/transcriptions/upload", params={"filename": "a.mp3"}, content=body())
    assert response.status_code == 413
    assert probed == []


def test_lying_content_length_does_not_help(api_context, monkeypatch):
    client, settings, _ = api_context
    settings.limits.max_upload_size_mb = 1
    monkeypatch.setattr(routes, "probe_audio", lambda *a: 10.0)
    response = client.post(
        "/api/transcriptions/upload", params={"filename": "a.mp3"},
        content=b"x" * (2 * 1024 * 1024), headers={"Content-Length": "10"},
    )
    assert response.status_code in (400, 413, 422), response.status_code


@pytest.mark.parametrize("key", ["a\nb", "x" * 201, "\x00", "é‮"])
def test_bad_idempotency_keys(api_context, key):
    client, _, _ = api_context
    try:
        response = client.post("/api/transcriptions", json={"source_url": "https://youtube.com/watch?v=1"}, headers={"Idempotency-Key": key})
    except Exception:  # httpx refuses some header values itself
        return
    assert response.status_code == 422, (key, response.status_code)


# --- 5. SSRF through the source URL ---------------------------------------------

@pytest.fixture
def resolver(api_context, monkeypatch):
    addresses = {"youtube.com": "142.250.74.14", "www.youtube.com": "142.250.74.14", "evil.youtube.com": "127.0.0.1"}

    def fake(host, port, type=None):
        if host not in addresses:
            raise socket.gaierror("unknown")
        family = socket.AF_INET6 if ":" in addresses[host] else socket.AF_INET
        return [(family, socket.SOCK_STREAM, 6, "", (addresses[host], port))]

    monkeypatch.setattr(url_safety.socket, "getaddrinfo", fake)
    return addresses


@pytest.mark.parametrize("url", [
    "http://127.0.0.1/", "http://2130706433/", "http://0x7f.1/", "http://[::1]/", "http://0/",
    "http://youtube.com@127.0.0.1/", "http://127.0.0.1#@youtube.com/", "http://127.0.0.1\\@youtube.com/",
    "http://evil.youtube.com/", "http://youtube.com:6379/",
    "http://youtube.com%2F@127.0.0.1/", "http://xn--youtube-.com/",
    "http://youtube.com.127.0.0.1.nip.io/", "http://[::ffff:169.254.169.254]/",
    "gopher://youtube.com/", "http://youtube.com\t.evil/", "http://youtube.com\r\nX: y/",
    "http://redis:6379/", "http://api:8000/", "http://metadata.google.internal/",
])
def test_ssrf_urls_are_refused_by_the_api(api_context, resolver, monkeypatch, url):
    client, _, _ = api_context
    from app.api.routes import billing as billing_routes

    ran = []
    monkeypatch.setattr(billing_routes, "read_video_info", lambda safe_url: ran.append(safe_url) or {"duration": 10})
    response = client.get("/api/billing/cost", params={"source_url": url})
    assert ran == [], (url, ran)
    assert response.status_code in (400, 422), (url, response.status_code, response.text)


@pytest.mark.parametrize(("url", "checked"), [
    # A browser reads a backslash as a slash; so does the URL type the API
    # parses with, before any check. The host stays youtube.com.
    ("http://youtube.com\\@127.0.0.1/", "http://youtube.com/@127.0.0.1/"),
    # Full-width letters become the host they look like.
    ("http://ｙｏｕｔｕｂｅ.com/", "http://youtube.com/"),
])
def test_disguised_urls_reach_only_the_host_they_show(api_context, resolver, monkeypatch, url, checked):
    client, _, _ = api_context
    from app.api.routes import billing as billing_routes

    ran = []
    monkeypatch.setattr(billing_routes, "read_video_info", lambda safe_url: ran.append(safe_url) or {"duration": 10})
    client.get("/api/billing/cost", params={"source_url": url})
    assert ran == [checked]


def test_resolver_rebinding_answers_with_any_private_address(api_context, resolver):
    resolver["youtube.com"] = "10.0.0.1"
    with pytest.raises(url_safety.UnsafeSourceUrl):
        url_safety.ensure_safe_source_url("https://youtube.com/watch?v=1")


# --- 6. Response hardening --------------------------------------------------------

@pytest.mark.parametrize("protected", [False, True])
def test_api_responses_carry_security_headers(api_context, protected):
    client, settings, _ = api_context
    if protected:
        # The password refusal too.
        settings.secrets.server_password = SecretStr(PASSWORD)
    response = client.get("/api/server-info")

    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["Referrer-Policy"] == "no-referrer"
    assert response.headers["Cache-Control"] == "no-store"


def test_a_users_midi_is_not_cached(api_context, tmp_path):
    client, _, _ = api_context
    result = tmp_path / "data" / "jobs" / "j1" / "transcription.mid"
    result.parent.mkdir(parents=True)
    result.write_bytes(b"MThd")
    routes.redis.hset(job_key("j1"), mapping={"status": "completed", "result": str(result), "user_id": "anonymous"})

    response = client.get("/api/transcriptions/j1/midi")

    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
