# 📡 API guide

The API base URL is `http://localhost:8000`. FastAPI's generated reference is available at `/docs`.

## 🎹 Transcriptions

Create a job with an HTTP(S) video URL:

```powershell
$job = Invoke-RestMethod `
  -Method Post `
  -Uri http://localhost:8000/api/transcriptions `
  -ContentType 'application/json' `
  -Body '{"source_url":"https://example.com/video"}'
```

The endpoint returns `202 Accepted`, a `job_id`, and `queued` status. Poll the job:

```powershell
$status = Invoke-RestMethod `
  "http://localhost:8000/api/transcriptions/$($job.job_id)"
$status
```

Possible statuses are `queued`, `processing`, `completed`, `failed`, and `cancelled`. The response includes progress from `0` to `1`, source metadata when available, an error on failure, and the current `minutes` balance when billing is enabled.

Cancel a queued or processing job:

```powershell
Invoke-RestMethod `
  -Method Delete `
  -Uri "http://localhost:8000/api/transcriptions/$($job.job_id)"
```

After the status is `completed`, download the MIDI:

```powershell
Invoke-WebRequest `
  -Uri "http://localhost:8000/api/transcriptions/$($job.job_id)/midi" `
  -OutFile transcription.mid
```

The MIDI can contain piano notes, velocities, onset and offset information, sustain-pedal events (`CC64`), and other detected MIDI control events. The download returns `409` while processing, `404` for an unknown or missing result, and `500` for invalid stored result metadata.

## 💳 Billing endpoints

* `GET /api/server-info` returns billing provider, available offers, cleanup interval, free-minute policy, and maximum video length.
* `GET /api/billing/cost?source_url=...` reads video duration and returns the transcription cost before submission.
* `GET /api/billing/balance` returns the authenticated user's balance and free-minute timing.
* `POST /api/billing/google-play/verify` verifies and credits a completed Google Play purchase.
* `DELETE /api/account` deletes the authenticated user's account and data (see below).

## 🗑️ Account deletion

When billing is enabled, a signed-in user can delete their account and data:

```powershell
Invoke-RestMethod `
  -Method Delete `
  -Uri http://localhost:8000/api/account `
  -Headers @{ Authorization = "Bearer $googleIdToken" }
```

The endpoint returns `204 No Content`. It removes:

* the user's balance, email, and Google account ID,
* transcription reservations,
* all of the user's jobs in Redis, including source URLs and metadata, and their MIDI and audio files. Running jobs stop without being charged.

Remaining minutes are lost, and deletion cannot be undone. Signing in again creates a new, empty account.

Some records are kept without personal data:

* Purchases keep the product, order ID, purchase token, and minutes, but no user. This keeps refunds and accounting possible and stops the same purchase from being credited twice.
* When free minutes are enabled, a SHA-256 hash of the Google account ID and the time of the last free grant are kept until the free-minute period ends. A deleted and recreated account therefore cannot get free minutes early. The record is removed when the period ends or when the user signs in again.

Billing backups still contain the deleted data until they expire after `BILLING_BACKUP_RETENTION`. See [Configuration](configuration.md#-billing-database-backups).

The endpoint returns `401` without a valid token and `409` when billing is disabled, because there are no accounts then.

Administrators can delete an account by email with `python -m app.admin_delete`; see [Configuration](configuration.md).

When billing is enabled, send a valid Google OAuth 2.0 access token as `Authorization: Bearer <token>`. The server checks the token with Google's `tokeninfo` endpoint, requires its audience to match one of `GOOGLE_OAUTH_CLIENT_IDS`, and uses the verified Google `sub` claim as the billing identity. The token must include the `openid` and `email` scopes, and the account's email must be verified. Results are cached for up to 5 minutes, so a revoked token can keep working for that long. `X-User-Id` cannot select another user. See [Google Play billing](google-play-billing.md) for the complete setup.
