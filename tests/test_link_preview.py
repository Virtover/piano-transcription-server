"""The link preview of GET /api/billing/cost: the title, channel, thumbnail, and page of a pasted link, from the same
cached check that reads its length (video_info.link_preview), with only links that are safe to show."""

import json
import subprocess

import pytest

from app import url_safety, video_check, video_info
from app.api.routes import billing as billing_routes

LINK = "https://youtube.com/watch?v=1"
MINUTES = billing_routes.transcription_cost(90)


@pytest.fixture
def yt_dlp(api_context, monkeypatch):
    """yt-dlp answering with info, which the test sets; each call recorded."""
    output = {"info": {"duration": 90}, "commands": []}

    def fake_run(command, **kwargs):
        output["commands"].append(command)
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps(output["info"]))

    monkeypatch.setattr(billing_routes, "ensure_safe_source_url", lambda url: url)
    monkeypatch.setattr(video_check.subprocess, "run", fake_run)
    return output


def cost(client, **params):
    response = client.get("/api/billing/cost", params={"source_url": LINK, **params})
    assert response.status_code == 200, response.text
    return response.json()


def test_a_link_gets_its_preview(api_context, yt_dlp):
    client, _, _ = api_context
    yt_dlp["info"] = {"duration": 90, "title": "  Nocturne Op. 9  ", "channel": "Pianist", "uploader": "Someone",
                      "thumbnail": "https://i.ytimg.com/vi/1/hq.jpg", "webpage_url": "https://www.youtube.com/watch?v=1"}

    assert cost(client) == {
        "duration_seconds": 90.0, "cost_minutes": MINUTES, "title": "Nocturne Op. 9", "channel": "Pianist",
        "thumbnail": "https://i.ytimg.com/vi/1/hq.jpg", "webpage_url": "https://www.youtube.com/watch?v=1",
    }


def test_a_cached_check_gives_the_preview_without_another_call(api_context, yt_dlp):
    client, _, _ = api_context
    yt_dlp["info"] = {"duration": 90, "title": "Song", "uploader": "Uploader"}

    first = cost(client)
    second = cost(client, start_seconds=10)

    assert len(yt_dlp["commands"]) == 1
    assert first["title"] == second["title"] == "Song"
    # without a channel, the uploader
    assert second["channel"] == "Uploader" and second["clip_seconds"] == 80.0


def test_missing_fields_are_left_out(api_context, yt_dlp):
    client, _, _ = api_context
    yt_dlp["info"] = {"duration": 90, "title": "", "channel": None, "thumbnail": 7}

    assert cost(client) == {"duration_seconds": 90.0, "cost_minutes": MINUTES}


@pytest.mark.parametrize("thumbnail", [
    "http://i.ytimg.com/vi/1/hq.jpg",                 # not https
    "https://127.0.0.1/hq.jpg",                       # a non-public address
    "https://[::1]/hq.jpg",
    "https://10.0.0.5/hq.jpg",
    "https://localhost/hq.jpg",
    "https://router.lan/hq.jpg",
    "https://intranet/hq.jpg",                        # no public name
    "https://user:secret@i.ytimg.com/hq.jpg",         # credentials
    "https://i.ytimg.com:8443/hq.jpg",                # another port
    "https://i.ytimg.com/hq.jpg\n<script>",           # control characters
    "javascript:alert(1)",
    "https://i.ytimg.com/" + "a" * 3000,              # too long
])
def test_an_unsafe_thumbnail_is_left_out(api_context, yt_dlp, thumbnail):
    client, _, _ = api_context
    yt_dlp["info"] = {"duration": 90, "title": "Song", "thumbnail": thumbnail, "webpage_url": thumbnail}

    body = cost(client)

    assert "thumbnail" not in body and "webpage_url" not in body and body["title"] == "Song"


def test_long_texts_are_capped(api_context, yt_dlp):
    client, _, _ = api_context
    yt_dlp["info"] = {"duration": 90, "title": "t" * 5000, "channel": "c" * 5000}

    body = cost(client)

    assert body["title"] == "t" * video_info.MAX_TEXT_LENGTH
    assert body["channel"] == "c" * video_info.MAX_TEXT_LENGTH
    # the job's metadata keeps the same cap
    assert video_info.video_metadata(yt_dlp["info"], LINK)["title"] == "t" * video_info.MAX_TEXT_LENGTH


def test_a_files_cost_has_no_preview(api_context, yt_dlp):
    client, _, _ = api_context
    response = client.get("/api/billing/cost", params={"duration_seconds": 90})
    assert response.json() == {"duration_seconds": 90.0, "cost_minutes": MINUTES}
    assert yt_dlp["commands"] == []


def test_safe_links_pass_as_they_are():
    for url in ("https://i.ytimg.com/vi/1/hq.jpg", "https://example.com:443/a?b=c", "https://93.184.216.34/x.jpg"):
        assert url_safety.client_link(url) == url
