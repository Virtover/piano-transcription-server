# 🔐 Google sign-in

Users sign in with their Google account. Sign-in is required when `BILLING_PROVIDER=google_play`, because the Google account is the billing identity. Set it up before [Google Play billing](google-play-billing.md).

## ⚙️ How it works

The app signs the user in with Google and gets an access token with the `openid` and `email` scopes. It sends the token with every request as `Authorization: Bearer <token>`. The server asks Google's `tokeninfo` endpoint about the token, and accepts it only if:

* it was issued to one of the client IDs in `GOOGLE_OAUTH_CLIENT_IDS`,
* it has not expired,
* it has a Google account ID (`sub`, from the `openid` scope) and a verified email.

The Google account ID becomes the user's billing identity. Client IDs are public, so they go in `.env`; no OAuth client secret is needed. Results are cached for up to 5 minutes.

## 🧭 Setting it up

What you must create in Google Cloud, with the reasons, comes from the server itself:

```shell
docker compose run --rm api python -m app.config requirements
```

See the **Google sign-in** section of its output: the consent screen, one Android OAuth client per certificate that signs the app, and a web client, for the package name in `GOOGLE_PLAY_PACKAGE_NAME`.

> [!TIP]
> With an AI coding agent such as Claude Code, run `/setup-google`. It turns each requirement into Google's current settings, guides you through them, and verifies the result.

1. **Create what `requirements` lists**, using Google's documentation: [Google Auth Platform](https://support.google.com/cloud/answer/15544987) and [Android sign-in](https://developer.android.com/identity/sign-in/credential-manager-siwg).
2. **Put every client ID in `.env`**, as one JSON list on one line, and restart the API:

   ```env
   GOOGLE_OAUTH_CLIENT_IDS=["111-debug.apps.googleusercontent.com","111-play.apps.googleusercontent.com","111-web.apps.googleusercontent.com"]
   ```

3. **Verify.** `check --online` checks the client IDs' format; Google can only confirm a client ID with a signed-in user's token, so it catches typos and pasted client secrets, not a missing client:

   ```shell
   docker compose run --rm api python -m app.config check --online
   ```

   Then sign in from the app and call the balance endpoint with its token. It returns your Google account ID and minutes:

   **Windows (PowerShell)**

   ```powershell
   Invoke-RestMethod http://localhost:8000/api/billing/balance -Headers @{ Authorization = "Bearer $token" }
   ```

   **macOS and Linux**

   ```bash
   curl http://localhost:8000/api/billing/balance -H "Authorization: Bearer $TOKEN"
   ```

### SHA-1 fingerprints

Each Android client needs the SHA-1 fingerprint of one signing certificate:

| Certificate | SHA-1 from |
| --- | --- |
| Debug | Windows: `keytool -list -v -keystore "$env:USERPROFILE\.android\debug.keystore" -alias androiddebugkey -storepass android`, or `.\gradlew signingReport`. macOS and Linux: `keytool -list -v -keystore ~/.android/debug.keystore -alias androiddebugkey -storepass android`, or `./gradlew signingReport` |
| Upload key | `keytool -list -v -keystore <upload-keystore>.jks -alias <alias>` |
| Play app signing | Play Console, which holds this key: the app signing key certificate, not the upload key |

## 🩺 Troubleshooting

| Error | Cause | Fix |
| --- | --- | --- |
| `401 Google authentication token was not issued for this app` | The token's client ID is not in `GOOGLE_OAUTH_CLIENT_IDS` | Add the missing client ID and restart the API. |
| `401 Google authentication token has no subject` | The app did not request the `openid` scope | Request `openid` and `email` in the app. |
| `401 Google authentication email is not verified` | The Google account's email is unverified | Use another account, or verify the email at Google. |
| `401 Google authentication token has expired` | Access tokens last about an hour | The app must request a fresh token. |
| `503 Google authentication is not configured` | `GOOGLE_OAUTH_CLIENT_IDS` is empty | Set it in `.env`. |
| Error 10 or `DEVELOPER_ERROR` in the app | No Android client matches the package name and certificate | Create the client for that certificate. Sign-in that works from Android Studio but not from Google Play is missing the Play app signing client. |
