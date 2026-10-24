# 💳 Google Play billing

This guide covers the additional setup required when `BILLING_PROVIDER=google_play`. Set up [Google sign-in](google-sign-in.md) first: billing needs it. Uncomment the billing section of [.env.example](../.env.example) in your `.env`, and see [Configuration](configuration.md#billing) for every setting.

## 🧭 Setting it up

The server lists products and verifies and consumes purchases through the Google Play Developer API, as a service account. What you must create, with the reasons, comes from the server itself:

```shell
docker compose run --rm api python -m app.config requirements
```

See the **Google Play** section of its output: the API, the service account and its key, its access in Play Console, and the products from `BILLING_PRODUCTS`.

> [!TIP]
> With an AI coding agent such as Claude Code, run `/setup-google`. It turns each requirement into Google's current settings, guides you through them, and verifies the result.

1. **Create what `requirements` lists**, using Google's documentation: [Play Developer API access](https://developers.google.com/android-publisher/getting_started), [one-time products](https://support.google.com/googleplay/android-developer/answer/1153481), and [testing billing](https://developer.android.com/google/play/billing/test).
2. **Save the service account's JSON key** under the exact name the server expects:

   **Windows (PowerShell)**

   ```powershell
   Move-Item "$env:USERPROFILE\Downloads\<downloaded-key>.json" secrets\google_play_service_account.json
   ```

   **macOS and Linux**

   ```bash
   mv ~/Downloads/<downloaded-key>.json secrets/google_play_service_account.json
   ```

   The key gives access to your app's orders: never commit, email, or paste it. Compose mounts `./secrets` at `/run/secrets`, so the containers read `/run/secrets/google_play_service_account.json`. Older setups that use `GOOGLE_PLAY_SERVICE_ACCOUNT_FILE` keep working, with a deprecation warning.

3. **Configure `.env`.** Map every product ID to the transcription minutes it buys:

   ```env
   GOOGLE_PLAY_PACKAGE_NAME=com.example.pianoweave
   BILLING_PRODUCTS={"transcription_25":25,"transcription_60":60,"transcription_300":300}
   FREE_MINUTES=10
   FREE_MINUTES_PERIOD=30d
   ```

   Only products that exist in Play Console **and** are listed in `BILLING_PRODUCTS` are offered through `/api/server-info`. The API reads the products at startup, so restart it after changes; there is no on-demand synchronization endpoint:

   ```shell
   docker compose restart api
   ```

4. **Verify.** `check --online` lists the products with the service account and compares them with `BILLING_PRODUCTS`. A failure names the likely cause: a rejected key, missing permissions, or a wrong package name. Products in only one of the two places are reported as warnings.

   ```shell
   docker compose run --rm api python -m app.config check --online
   ```

   A 401 or 403 right after giving the service account access is usually Google's delay in applying new permissions, which can take up to a day.

## 🔄 Purchase flow

The Android client sends the completed purchase's product ID and purchase token to `POST /api/billing/google-play/verify` with the user's Google ID token:

**Windows (PowerShell)**

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

**macOS and Linux**

```bash
curl -X POST http://localhost:8000/api/billing/google-play/verify \
  -H "Authorization: Bearer $GOOGLE_ID_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"product_id":"transcription_60","purchase_token":"<token-returned-by-google-play>"}'
```

The server verifies the purchase with Google Play, credits the configured minutes once per purchase token, and consumes the purchase. Repeating the same token returns `credited_minutes: 0`.

## ⏱️ Transcription charges

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

## 🗑️ Account deletion

Google Play requires apps that let users create an account to also let them delete it. The client can call `DELETE /api/account` with the user's Google ID token. See **Account deletion** in the [API guide](api.md) for what is deleted and what is kept.

Google Play also requires a web link where users can request deletion without the app. Handle those requests with the `app.admin_delete` command described in [Configuration](configuration.md).
