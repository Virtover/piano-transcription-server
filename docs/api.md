# 📡 API guide

The API base URL is `http://localhost:8000`. FastAPI's generated reference is available at `/docs`.

If the server has a [password](setup.md#-password-protection), add it to every request except `/health` and `/ready`, for example `-Headers @{ 'X-Server-Password' = $password }` in PowerShell. HTTP Basic auth with any user name works too. Without it, requests get `401` with `{"detail": "Server password required"}` and a `WWW-Authenticate: Basic` header. When billing is enabled, send both this header and the Google `Authorization` header.

## 🎹 Transcriptions

Create a job with an HTTP(S) video URL:

```powershell
$job = Invoke-RestMethod `
  -Method Post `
  -Uri http://localhost:8000/api/transcriptions `
  -ContentType 'application/json' `
  -Body '{"source_url":"https://example.com/video"}'
```

The endpoint returns `202 Accepted`, a `job_id`, and `queued` status.

### 📁 Uploading a file

Send an audio or video file as the raw request body, and its original name as `filename`:

```powershell
$job = Invoke-RestMethod `
  -Method Post `
  -Uri "http://localhost:8000/api/transcriptions/upload?filename=Nocturne.mp3" `
  -ContentType 'application/octet-stream' `
  -InFile .\Nocturne.mp3
```

```bash
curl -X POST "http://localhost:8000/api/transcriptions/upload?filename=Nocturne.mp3" \
  -H "Content-Type: application/octet-stream" \
  --data-binary @Nocturne.mp3
```

The body is the file itself, not a multipart form. This lets the server stop reading as soon as the size limit is exceeded. When billing is enabled, add the `Authorization` header as for other requests.

* Accepted formats: MP3, WAV, FLAC, Ogg/Opus, AAC, M4A, AIFF, WMA, MP4, MOV, MKV, and WebM. Video files are fine; only their audio is used. The server checks the content, not the file name.
* `filename` is only used as the title (without the extension). It never decides where the file is stored.
* The size limit is `MAX_UPLOAD_SIZE_MB` (default 200 MB), and the length limit is `MAX_VIDEO_LENGTH_MINUTES`, the same as for links. `GET /api/server-info` returns both.
* The response is the same as for a link, and the job is polled, cancelled, and downloaded the same way. Its metadata contains only the title and duration.
* The uploaded file is deleted when the job completes, fails, or is cancelled. It is kept until then so the job can be retried on another backend.

Upload errors:

| Status | Meaning |
| --- | --- |
| `413` | The file exceeds the size limit or the maximum length. |
| `422` | The file is empty, is not a supported audio or video file, has no audio, or `filename` is missing. |
| `402` | Not enough transcription minutes (billing enabled). |

To show the cost before uploading, read the duration on the device and call `GET /api/billing/cost?duration_seconds=...`.

### 🔄 Following a job

Poll the job:

```powershell
$status = Invoke-RestMethod `
  "http://localhost:8000/api/transcriptions/$($job.job_id)"
$status
```

Possible statuses are `queued`, `processing`, `completed`, `failed`, and `cancelled`. The response includes progress from `0` to `1`, source metadata when available, an error on failure, and the current `minutes` balance when billing is enabled. `backend` is `local` or `remote` and says where the model runs; it can change once if a remote job falls back to the local queue. Progress never goes backwards when that happens. `maintenance` announces a planned restart or shutdown; see [Planned restarts and shutdowns](#-planned-restarts-and-shutdowns).

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

## 🔁 Planned restarts and shutdowns

Before a planned restart or shutdown, the server announces it. The notice appears in three places, so a client sees it whatever it is doing:

* `GET /api/server-info` and every job status response have a `maintenance` field: `null`, or

  ```json
  {"action": "restart", "at": 1792668429, "seconds_remaining": 287,
   "message": "Server restart in 5 m", "reason": "Update", "back_at": null}
  ```

* Every response has the headers `X-Server-Maintenance: restart` (or `shutdown`) and `X-Server-Maintenance-At: <unix time>` while one is pending.

| Field | Meaning |
| --- | --- |
| `action` | `restart`: the server is back within a few minutes. `shutdown`: it goes offline. |
| `at` | When it happens, as a Unix timestamp in seconds. |
| `seconds_remaining` | Seconds until `at`, when the response was made. |
| `message` | Ready-made English text, such as `Server restart in 5 m` or `Server shutting down in 30 s`: minutes rounded, or seconds under a minute. |
| `reason` | Optional text from the operator, or `null`. |
| `back_at` | Shutdowns only: when the server is expected back, as a Unix timestamp, or `null` if unknown. |

While a restart or shutdown is pending, a new transcription is refused with `503` if it would not finish before it, and always in the last minute. Nothing is reserved or charged. The body looks like:

```json
{"detail": {"message": "Server restart in 1 m; new transcriptions are paused until it is back",
            "maintenance": {"action": "restart", "at": 1792668429, "seconds_remaining": 58}}}
```

The `maintenance` object in the body is complete; it is shortened here. The response has a `Retry-After` header with the seconds until the server should accept jobs again: after a restart, or at `back_at` for a shutdown. A shutdown without `back_at` has no `Retry-After`.

During a restart, requests fail to connect, and right after it the API answers `503` with `Retry-After: 10` until it has finished starting.

Jobs survive both: running jobs get time to finish first, and queued jobs stay queued, keeping their `job_id`. After a restart they continue within minutes; after a shutdown, when the server starts again. Only a job still running when the grace period ends fails, with its minutes refunded.

**Recommended client behavior:**

* Show a banner while `maintenance` is not `null`, counting down from `at` locally rather than from `seconds_remaining`, so the countdown stays exact between polls. Build the text in the user's language from `action`, `at`, and `back_at` instead of showing `message`.
* Before submitting a long recording, warn the user if a restart or shutdown is close.
* After a restart: on `503` or a connection error, wait for `Retry-After` (or a few seconds) and retry instead of showing an error, and keep polling jobs: their `job_id` stays valid.
* After a shutdown: show the server as offline, with `back_at` if known, and check again from time to time instead of retrying quickly.

## 💳 Billing endpoints

* `GET /api/server-info` returns billing provider, available offers, cleanup interval, free-minute policy, maximum video length, the upload size limit (`max_upload_size_mb`), and a pending restart or shutdown (`maintenance`, see [Planned restarts and shutdowns](#-planned-restarts-and-shutdowns)).
* `GET /api/billing/cost?source_url=...` reads video duration and returns the transcription cost before submission. For a file, use `?duration_seconds=...` instead. Pass exactly one of the two.
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
* job diagnostics records (source URLs, errors, and timings of past jobs),
* all of the user's jobs in Redis, including source URLs and metadata, and their MIDI and audio files. Running jobs stop without being charged.

Remaining minutes are lost, and deletion cannot be undone. Signing in again creates a new, empty account.

Some records are kept without personal data:

* Purchases keep the product, order ID, purchase token, and minutes, but no user. This keeps refunds and accounting possible and stops the same purchase from being credited twice.
* When free minutes are enabled, a SHA-256 hash of the Google account ID and the time of the last free grant are kept until the free-minute period ends. A deleted and recreated account therefore cannot get free minutes early. The record is removed when the period ends or when the user signs in again.

Billing backups still contain the deleted data until they expire after `BILLING_BACKUP_RETENTION`. See [Configuration](configuration.md#-billing-database-backups).

The endpoint returns `401` without a valid token and `409` when billing is disabled, because there are no accounts then.

Administrators can delete an account by email with `python -m app.admin_delete`; see [Configuration](configuration.md).

When billing is enabled, send a valid Google OAuth 2.0 access token as `Authorization: Bearer <token>`. The server checks the token with Google's `tokeninfo` endpoint, requires its audience to match one of `GOOGLE_OAUTH_CLIENT_IDS`, and uses the verified Google `sub` claim as the billing identity. The token must include the `openid` and `email` scopes, and the account's email must be verified. Results are cached for up to 5 minutes, so a revoked token can keep working for that long. `X-User-Id` cannot select another user. See [Google sign-in](google-sign-in.md) for the setup.
