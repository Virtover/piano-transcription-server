# Google Play billing

This guide covers the additional setup required when `BILLING_PROVIDER=google_play`. For the environment variables, start from [.env.google-play.example](../.env.google-play.example) and see [Configuration](configuration.md).

## Google Cloud and OAuth

1. In Google Cloud Console, create an OAuth 2.0 client ID for the Android application.
2. Put the public client ID in `.env`:

```env
GOOGLE_OAUTH_CLIENT_IDS=["123456789-example.apps.googleusercontent.com"]
```

3. Configure the Android client to request Google ID tokens whose audience is one of these IDs.

The client ID is public configuration. Never commit OAuth or service-account credentials.

## Google Play service account

1. Create or select a Google Cloud service account.
2. Enable the Android Publisher API for its project.
3. Grant the service account access to the Android application in Google Play Console with permission to view and manage orders and subscriptions, as required by the Publisher API.
4. Download its JSON key and place it in the local `secrets` directory. Do not commit it.
5. Set the package name and filename in `.env`:

```env
GOOGLE_PLAY_PACKAGE_NAME=com.example.pianoweave
GOOGLE_PLAY_SERVICE_ACCOUNT_FILE=google-play-service-account.json
```

Compose mounts `./secrets` at `/run/secrets`, so the service account is read from `/run/secrets/google-play-service-account.json` inside the containers.

## Products and free minutes

Create the one-time products in Google Play Console, then map their exact product IDs to transcription minutes:

```env
BILLING_PRODUCTS={"transcription_25":25,"transcription_60":60,"transcription_300":300}
FREE_MINUTES=10
FREE_MINUTES_PERIOD=30d
```

Only products confirmed by the startup synchronization are advertised through `/api/server-info`. Restart the API after adding or changing products:

```powershell
docker compose restart api
```

There is currently no on-demand synchronization endpoint.

## Purchase flow

The Android client sends the completed purchase's product ID and purchase token to `POST /api/billing/google-play/verify` with the user's Google ID token:

```powershell
$purchase = @{
    product_id = 'transcription_60'
    purchase_token = '<token-returned-by-google-play>'
} | ConvertTo-Json

Invoke-RestMethod `
  -Method Post `
  -Uri http://localhost:8000/api/billing/google-play/verify `
  -Headers @{ Authorization = "Bearer $googleIdToken" } `
  -ContentType 'application/json' `
  -Body $purchase
```

The server verifies the purchase with Google Play, credits the configured minutes once per purchase token, and consumes the purchase. Repeating the same token returns `credited_minutes: 0`.

## Transcription charges

For a video lasting $d$ seconds, the full cost in minutes is:

$$
m =
\begin{cases}
1, & d / 60 < 2 \\
\lfloor d / 60 \rfloor, & d / 60 \ge 2
\end{cases}
$$

The full amount is reserved before a job is queued. Completed jobs charge the reservation; failed jobs release it. Cancelled jobs charge based on progress $p$:

$$
c =
\begin{cases}
0, & p < 0.05 \\
\lceil 0.6 \times m \times p \rceil, & p \ge 0.05
\end{cases}
$$

The cancellation charge cannot exceed the reserved cost. Billing records are stored in the SQLite database configured by `BILLING_DATABASE_PATH`.
