def test_sign_in_and_refresh_rotate_tokens(api_context):
    client, _, _ = api_context

    signed_in = client.post("/api/auth/sign-in", json={"google_id_token": "google-sub"})
    assert signed_in.status_code == 200
    original = signed_in.json()

    refreshed = client.post("/api/auth/refresh", json={"refresh_token": original["refresh_token"]})
    assert refreshed.status_code == 200
    rotated = refreshed.json()
    assert rotated["refresh_token"] != original["refresh_token"]
    assert rotated["access_token"]

    reused = client.post("/api/auth/refresh", json={"refresh_token": original["refresh_token"]})
    assert reused.status_code == 401
    assert reused.json()["detail"] == "Refresh token reuse detected"

    family_revoked = client.post("/api/auth/refresh", json={"refresh_token": rotated["refresh_token"]})
    assert family_revoked.status_code == 401
    assert family_revoked.json()["detail"] == "Refresh token reuse detected"


def test_google_token_cannot_be_used_as_api_access_token(api_context):
    client, settings, _ = api_context
    settings.billing_provider = "google_play"

    response = client.get(
        "/api/billing/balance",
        headers={"Authorization": "Bearer google-sub"},
    )

    assert response.status_code == 401