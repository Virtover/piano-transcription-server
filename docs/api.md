# 📡 API guide

The API base URL is `http://localhost:8000`. FastAPI's generated reference is available at `/docs`.

If the server has a [password](setup.md#-password-protection), add it to every request except `/health` and `/ready`, for example `-Headers @{ 'X-Server-Password' = $password }` in PowerShell or `-H "X-Server-Password: $PASSWORD"` with curl. HTTP Basic auth with any user name works too. Without it, requests get `401` with `{"detail": "Server password required"}` and a `WWW-Authenticate: Basic` header. When billing is enabled, send both this header and the Google `Authorization` header.

## 🎹 Transcriptions

Create a job with an HTTP(S) video URL:

**Windows (PowerShell)**

```powershell
$job = Invoke-RestMethod `
  -Method Post `
  -Uri http://localhost:8000/api/transcriptions `
  -ContentType 'application/json' `
  -Body '{"source_url":"https://example.com/video"}'
```

**macOS and Linux**

```bash
curl -X POST http://localhost:8000/api/transcriptions \
  -H 'Content-Type: application/json' \
  -d '{"source_url":"https://example.com/video"}'
```

The endpoint returns `202 Accepted`, a `job_id`, and `queued` status. The job's title and metadata are available from the start, read when the link is checked. PowerShell keeps the response in `$job`, which the examples below use. With curl, keep the ID for them: `JOB_ID=<job_id from the response>`.

### 📁 Uploading a file

Send an audio or video file as the raw request body, and its original name as `filename`:

**Windows (PowerShell)**

```powershell
$job = Invoke-RestMethod `
  -Method Post `
  -Uri "http://localhost:8000/api/transcriptions/upload?filename=Nocturne.mp3" `
  -ContentType 'application/octet-stream' `
  -InFile .\Nocturne.mp3
```

**macOS and Linux**

```bash
curl -X POST "http://localhost:8000/api/transcriptions/upload?filename=Nocturne.mp3" \
  -H "Content-Type: application/octet-stream" \
  --data-binary @Nocturne.mp3
```

The body is the file itself, not a multipart form. This lets the server stop reading as soon as the size limit is exceeded. When billing is enabled, add the `Authorization` header as for other requests.

* Accepted formats: MP3, WAV, FLAC, Ogg/Opus, AAC, M4A, AIFF, WMA, MP4, MOV, MKV, and WebM. Video files are fine; only their audio is used. The server checks the content, not the file name.
* `filename` is only used as the title (without the extension). It never decides where the file is stored.
* The size limit is `MAX_UPLOAD_SIZE_MB` (default 200 MB), and the length limit is `MAX_VIDEO_LENGTH_MINUTES` (default 120 minutes), the same as for links. Either setting can be `unlimited`. `GET /api/server-info` returns both; an unlimited limit is returned as `null`.
* The length is measured by decoding the audio, not read from the file's header, which can be wrong. The job is charged for the measured length, and only that much is transcribed.
* The response is the same as for a link, and the job is polled, cancelled, and downloaded the same way. Its metadata contains only the title and duration.
* The uploaded file is deleted when the job completes, fails, or is cancelled. It is kept until then so the job can be retried on another backend.

Upload errors:

| Status | Meaning |
| --- | --- |
| `413` | The file exceeds the size limit or the maximum length. |
| `422` | The file is empty, is not a supported audio or video file, has no audio, or `filename` is missing. |
| `402` | Not enough transcription minutes (billing enabled). A balance below one minute is refused before the file is read. |
| `429` | Too many transcriptions in progress; see [Limits](#-limits). |
| `403`, `503` | Submissions are stopped by an administrator; see [Limits](#-limits). |

To show the cost before uploading, read the duration on the device and call `GET /api/billing/cost?duration_seconds=...`.

### 🔂 Retrying a submission safely

When the connection drops after a submission was sent, the client cannot tell whether the server queued it. A plain retry would queue, and charge, a second job, and the first one could not be found: there is no endpoint that lists a user's jobs.

To avoid that, send an `Idempotency-Key` header with both submission endpoints: a random ID, such as a UUID, created once per submission and sent again unchanged with every retry of it. Store it until a response arrives, so a retry after the app restarts uses it too. A new submission, even of the same video, needs a new key.

| A request with a key the server has seen | Answer |
| --- | --- |
| The first request queued the job | `202` with the same `job_id` and the job's current `status`. Nothing is queued or charged again, even while new submissions are paused. |
| The first request is still checking the link or receiving the file | `409` with `Retry-After: 5`. Retry with the same key. |
| The first request failed before queuing, for example with `402` or `503` | Handled as a new submission. |
| The job finished and has expired (`RESULT_RETENTION`) | `409`: the submission was already processed. |

A retried upload is answered before its body is read, so the file is not stored twice. Keys are per user, 1 to 200 printable characters; others are refused with `422`. Without the header, every request is a new job.

### 🔄 Following a job

Poll the job:

**Windows (PowerShell)**

```powershell
Invoke-RestMethod "http://localhost:8000/api/transcriptions/$($job.job_id)"
```

**macOS and Linux**

```bash
curl "http://localhost:8000/api/transcriptions/$JOB_ID"
```

Possible statuses are `queued`, `processing`, `completed`, `failed`, and `cancelled`. The response includes progress from `0` to `1`, source metadata when available, an error on failure, and the current `minutes` balance when billing is enabled. The `error` is written for users, for example `The video could not be downloaded: Video unavailable. This video is private`; the full technical error is only in the server's logs and [job diagnostics](architecture.md#-job-diagnostics). `backend` is `local` or `remote` and says where the model runs; it can change once if a remote job falls back to the local queue. Progress never goes backwards when that happens. `maintenance` announces a planned restart or shutdown; see [Planned restarts and shutdowns](#-planned-restarts-and-shutdowns). A failed job also has an `error_code`, for the app to act on; see [Error codes](#-error-codes).

Cancel a queued or processing job:

**Windows (PowerShell)**

```powershell
Invoke-RestMethod -Method Delete -Uri "http://localhost:8000/api/transcriptions/$($job.job_id)"
```

**macOS and Linux**

```bash
curl -X DELETE "http://localhost:8000/api/transcriptions/$JOB_ID"
```

After the status is `completed`, download the MIDI:

**Windows (PowerShell)**

```powershell
Invoke-WebRequest -Uri "http://localhost:8000/api/transcriptions/$($job.job_id)/midi" -OutFile transcription.mid
```

**macOS and Linux**

```bash
curl -L -o transcription.mid "http://localhost:8000/api/transcriptions/$JOB_ID/midi"
```

The MIDI can contain piano notes, velocities, onset and offset information, sustain-pedal events (`CC64`), and other detected MIDI control events. The download returns `409` while processing, `404` for an unknown or missing result, and `500` for invalid stored result metadata.

With `JOB_STORAGE=s3` (see [Job storage](architecture.md#job-storage)), it answers `307 Temporary Redirect` instead, to a signed link to the file in object storage, valid for 5 minutes. Clients must follow the redirect without the `Authorization` header and without the server password: the link carries its own signature, and storage refuses a request with a second one. Most HTTP clients drop those headers when a redirect leads to another host; `curl -L` does. Ask the API again for a new link rather than keeping one.

## 🚦 Limits

The server limits how many transcriptions are in progress at once: uploading, being checked, queued, or running. When a limit is reached, new submissions get `429 Too Many Requests` with `Retry-After: 60` and a `detail` message for the user:

* `MAX_ACTIVE_JOBS` (default 50): all users together.
* `MAX_ACTIVE_JOBS_PER_USER` (default 5): one signed-in user, when billing is enabled.

A transcription stops counting when it completes, fails, or is cancelled.

An administrator can also stop submissions for a while, without a restart (see [Administration](administration.md#-limits)). Show the `detail` to the user:

* `503` for every new transcription while submissions are paused, and for links and their cost while links are turned off; uploads are still accepted then. `Retry-After` is sent when the end is known.
* `403` for a blocked account, on new transcriptions and link costs. Its jobs, balance, and account deletion still work.

Checking a link's length runs a video download tool. Only a few checks run at once, and checks of links to the same site start at least `VIDEO_SITE_INTERVAL` (default 3 seconds) apart. When no check can start within 10 seconds, `POST /api/transcriptions` and `GET /api/billing/cost?source_url=...` answer `503` with `Retry-After` and the error code `source_busy`. A check is reused for 5 minutes, so submitting a link right after asking its cost does not run it again. Requests for a link that is being checked wait for that check. A video the site says cannot be read is answered from memory for a minute, and a link whose site timed out for 30 seconds.

### 🛑 When a video site refuses the server

A video site that receives too many requests from one address rate-limits it, asks it to prove it is not a bot, or blocks it. Asking again makes this worse, so the server stops asking:

* The first such answer, to the API or a worker, turns links to that site off for every user for `VIDEO_SITE_COOLDOWN` (default 30 minutes). Each further incident in a row doubles the time, up to `VIDEO_SITE_MAX_COOLDOWN` (default 1 day). Links to other sites are not affected. YouTube's links, including `youtu.be`, count as one site.
* A site that times out 3 times within 5 minutes is not asked for `VIDEO_SITE_TIMEOUT_COOLDOWN` (default 2 minutes), doubling while it keeps timing out, up to `VIDEO_SITE_COOLDOWN`. One timeout answers only that request, with `Retry-After: 30`.
* Meanwhile, links to the site and their cost are refused with `503`, `Retry-After` with the time left, and the error code `source_temporarily_unavailable`. Link jobs already queued fail with the same code without contacting the site, and their minutes are given back. Uploads work as usual.
* When the time is up, links are read one at a time until one succeeds, so the users who waited do not reach the site all at once.
* At most `VIDEO_SITE_MAX_DOWNLOADS` (default 2) downloads from one site run at once; further jobs wait in the queue for a place.
* Failed jobs are never retried on their own. An administrator sees the state in `status`, and can end it early with `limits sites reset`; see [Administration](administration.md#-limits).

In the app, offer an upload when you get `source_temporarily_unavailable`, and do not retry the link before `Retry-After`.

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

## 🏷️ Error codes

A failed job has an `error_code` next to its `error`. Refusals of a link, and `402`, carry one in `detail`, as `{"message": "...", "error_code": "..."}`. Act on the code rather than the message, which can change. The status alone does not tell these apart: `402` and `429` are also this server's own limits, while a video site's rate limit is `source_temporarily_unavailable`.

| `error_code` | Answer | Meaning | In the app |
| --- | --- | --- | --- |
| `source_temporarily_unavailable` | `503` with `Retry-After`, or a failed job | A video site refused this server, or did not answer in time; see [When a video site refuses the server](#-when-a-video-site-refuses-the-server). | Offer an upload. Do not retry the link before `Retry-After`. |
| `source_busy` | `503` with `Retry-After`, or rarely a failed job | Too many links are being checked or downloaded. | Retry once after `Retry-After`. |
| `source_video_unavailable` | `422`, or a failed job | The video is private, removed, restricted, not available in the server's country, or not out yet. | Ask the user to check the link, or upload the file. |
| `source_unsupported` | `422` | The link is not one the server can read: a site it does not accept, or an address it must not open. | Ask for an upload. |
| `source_failed` | `422`, or a failed job | The video could not be read for another reason. | Show the message; offer an upload. |
| `insufficient_minutes` | `402` | Not enough minutes; `required_minutes` says how many the job needs. | Offer to buy minutes. |
| `transcription_failed` | a failed job | Converting or transcribing the audio failed. | Show the message. |
| `transcription_interrupted` | a failed job | A restart or a stopped worker interrupted the job. | Submit it again. |

A failed job's minutes are always given back. A request the API cannot parse gets FastAPI's own `422`, whose `detail` is a list, without a code.

## 💳 Billing endpoints

* `GET /api/server-info` returns billing provider, available offers, how long finished jobs and their MIDI files can be retrieved (`result_retention_seconds`), the cleanup interval, free-minute policy, maximum video length, the upload size limit (`max_upload_size_mb`), and a pending restart or shutdown (`maintenance`, see [Planned restarts and shutdowns](#-planned-restarts-and-shutdowns)).
* `GET /api/billing/cost?source_url=...` reads video duration and returns the transcription cost before submission. For a file, use `?duration_seconds=...` instead. Pass exactly one of the two. When billing is enabled, it needs the `Authorization` header like other requests.
* `GET /api/billing/balance` returns the authenticated user's balance and free-minute timing. `minutes` is negative when a refunded purchase's minutes were already used; see [Refunds and chargebacks](google-play-billing.md#-refunds-and-chargebacks). Show the user that they owe minutes, and offer to buy more.
* `POST /api/billing/google-play/verify` verifies and credits a completed Google Play purchase.
* `DELETE /api/account` deletes the authenticated user's account and data (see below).

## 🗑️ Account deletion

When billing is enabled, a signed-in user can delete their account and data:

**Windows (PowerShell)**

```powershell
Invoke-RestMethod `
  -Method Delete `
  -Uri http://localhost:8000/api/account `
  -Headers @{ Authorization = "Bearer $googleIdToken" }
```

**macOS and Linux**

```bash
curl -X DELETE http://localhost:8000/api/account \
  -H "Authorization: Bearer $GOOGLE_ID_TOKEN"
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

Billing backups, including off-site copies, still contain the deleted data until they are deleted: after 8 weeks with the default retention steps, or `BILLING_BACKUP_RETENTION` if it is set. See [Configuration](configuration.md#-billing-database-backups).

The endpoint returns `401` without a valid token and `409` when billing is disabled, because there are no accounts then.

Administrators can delete an account by email with `python -m app.admin user <email> delete`; see [Administration](administration.md#-users).

When billing is enabled, send a valid Google OAuth 2.0 access token as `Authorization: Bearer <token>`. The server checks the token with Google's `tokeninfo` endpoint, requires its audience to match one of `GOOGLE_OAUTH_CLIENT_IDS`, and uses the verified Google `sub` claim as the billing identity. The token must include the `openid` and `email` scopes, and the account's email must be verified. Results are cached for up to 5 minutes, so a revoked token can keep working for that long. `X-User-Id` cannot select another user. See [Google sign-in](google-sign-in.md) for the setup.
