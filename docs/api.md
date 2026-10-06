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

When billing is enabled, send a valid Google OpenID Connect ID token as `Authorization: Bearer <token>`. The server validates its audience against `GOOGLE_OAUTH_CLIENT_IDS` and uses the verified Google `sub` claim as the billing identity. `X-User-Id` cannot select another user. See [Google Play billing](google-play-billing.md) for the complete setup.
