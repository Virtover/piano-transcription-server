# 🏗️ Architecture and operations

## 🧩 Services

```text
                                   ┌─ queue "default" ─> worker (GPU) ──────────────┐
Client -> API -> Redis job state ──┤                                                 ├─> data volume
                                   └─ queue "remote" ──> remote-worker (no GPU) ─────┘
                                                            │   ▲
                                              FLAC upload   │   │ MIDI (base64)
                                                            ▼   │
                                          S3 storage (R2) ◄── RunPod Serverless handler
                                                         download

cleanup service: deletes expired job files and stale routing entries
```

* `api` accepts requests and uploaded files, serves status and MIDI files, and decides whether each job runs locally or remotely. It checks uploads with `ffprobe`.
* `worker` consumes the `default` queue: it downloads audio, or reads the uploaded file, and runs the model on this machine's GPU or CPU.
* `remote-worker` (optional, Compose profile `outsource`) consumes the `remote` queue. It downloads and converts audio here, then runs the model on RunPod. It runs on the API image and never loads torch or the model.
* `cleanup` removes files whose Redis job state has expired.
* `redis` stores job state and queues work.
* The shared `data` volume stores job files. Billing SQLite data and its periodic backups are stored in the host-mounted `billing-data` directory, available only to the API and the workers.

The main implementation surfaces are `app/main.py`, `app/api/routes/`, `app/worker/`, and `app/transcription/`.

## 🎼 Transcription pipeline

1. Download source audio and metadata with `yt-dlp`, or use the uploaded file.
2. Convert audio to 44.1 kHz mono WAV with `ffmpeg`.
3. Run Transkun in overlapping 16-second segments with an 8-second hop.
4. Write the MIDI file.
5. Serve the result from the API.

The Transkun checkpoint detects sustain-pedal events separately instead of automatically extending note durations until pedal release. Progress during model execution is an estimate because the Transkun command does not expose exact per-segment progress.

## 🗄️ Job retention and restart behavior

Job files are stored under `/data/jobs/<job_id>/`. Queued and processing jobs do not receive a Redis expiration. On API startup, jobs left processing by a previous session are marked failed, their billing reservations are released, and their partial files are removed: their worker stopped mid-job. Queued jobs are sent to their queue again and keep their reservation, so a restart does not lose them. A message for a queued job left over from before the restart is harmless, because only one delivery can claim a job.

Completed and failed jobs receive a one-hour Redis TTL. The cleanup service periodically removes job directories whose Redis keys no longer exist, so files can remain briefly after the TTL expires.

Deleting an account removes the user's Redis jobs and job directories immediately. A worker still processing one of those jobs sees it as cancelled at its next progress check and stops.

### Uploaded files

The API streams an upload into `/data/jobs/<job_id>/upload.input`, stopping as soon as `MAX_UPLOAD_SIZE_MB` is exceeded. During the upload the job has the status `uploading` and a one-hour TTL. That keeps the cleanup service from removing the directory, and lets the cleanup service remove it if the API stops mid-upload. After `ffprobe` accepts the file and the cost is reserved, the job becomes `queued` like any other job.

Uploaded files are untrusted. Only common audio and video container formats are accepted. Playlist and concat formats such as HLS are rejected because they make `ffmpeg` open other files or URLs. `ffprobe` and `ffmpeg` run with `-protocol_whitelist file`, so they cannot open network URLs at all. The worker checks the format again before converting.

Billing backups are pruned by the API after each backup, using `BILLING_BACKUP_RETENTION`.

### 🔁 Planned restarts and shutdowns

Restart or stop the stack with `scripts/server.py` on the host instead of running `docker compose` directly. It announces the restart or shutdown to clients first, and needs only Python 3 and the docker CLI:

```shell
python scripts/server.py restart 5m                  # restart in 5 minutes, for example to update
python scripts/server.py restart 5m --outsource      # the same, with the outsource profile
python scripts/server.py restart 10m --reason "Faster transcription" --grace 10m
python scripts/server.py stop 10m --back-in 2h       # shut down in 10 minutes, back in about 2 hours
python scripts/server.py start --outsource           # start again and wait until ready
python scripts/server.py status
python scripts/server.py cancel                      # also stops a waiting script
```

**Restart:**

1. **Announce.** The notice is stored in Redis, and clients see it in `GET /api/server-info`, job status, and the `X-Server-Maintenance` headers; see [Planned restarts and shutdowns](api.md#-planned-restarts-and-shutdowns). It expires on its own shortly after the planned time plus the grace period, so a crashed script cannot leave it behind.
2. **Build while serving.** The new images are built during the countdown, and the current version keeps serving. If the build fails, the restart is cancelled and the notice removed. If the build takes longer than the countdown, the restart waits for it.
3. **Refuse jobs that would not finish.** New jobs whose estimated wait and run time ends after the planned time are refused with `503`, without charge; in the last minute, all are.
4. **Drain.** At the planned time, running jobs get up to `--grace` (default `5m`) to finish. Queued jobs wait and are queued again after the restart.
5. **Restart** with `docker compose up -d --build`, which recreates only services whose image or configuration changed. Profiles given with `--profile` or `--outsource` apply to the build and the restart. `--command` replaces the restart command; then nothing is built before the countdown ends.
6. **Clear** the notice once the API answers `/ready`. If it does not within 3 minutes, the script stops with an error and the notice expires on its own.

**Shutdown** (`stop`) announces, refuses late jobs, and drains the same way, without building. Then it stops every service with `docker compose --profile "*" stop`, so services from any profile stop too. With `--down`, the containers are removed as well; volumes and the billing data are kept either way. `--back-in` tells clients when the server is expected back. Queued jobs stay in Redis and run after `start`, whose command line, with the profiles that were running, the script prints. The notice stays in Redis while the server is off, and the API removes it when it starts.

Ctrl+C during a countdown cancels it and removes the notice. The script runs `python -m app.maintenance` in the `api` container, so the stack must be running, with a version that includes it.

### 🩺 Job diagnostics

Redis job state disappears an hour after a job ends, so each job also has a record in the `job_diagnostics` table of the billing database. Because it is in that database, the hourly billing backups include it. A record is written when the job is queued, starts, is moved between backends, and ends, and holds:

* job ID, user ID, source (`url` or `upload`), and the source URL (none for uploads; the file name is not stored),
* audio duration and reserved cost,
* status, error message, backend, whether the job was outsourced, and why it was last moved between backends,
* times queued, first started, first finished, and last updated (Unix seconds).

Records older than `BILLING_JOB_DIAGNOSTICS_RETENTION` (default `30d`) are deleted before each backup. Deleting an account deletes its records. Writing a record never fails a job; errors are logged.

To look at recent failures, query a backup copy or the live database:

```sql
SELECT job_id, datetime(created_at, 'unixepoch'), backend, outsourced, error
FROM job_diagnostics WHERE status = 'failed' ORDER BY created_at DESC LIMIT 20;
```

## ☁️ Outsourcing

With `OUTSOURCE_PROVIDER=runpod`, jobs can run on RunPod Serverless when the local GPU is busy.

### Routing

Overload means a long **expected wait**, not low VRAM. The worker's thread count already caps GPU concurrency, so overload shows up as jobs waiting in the queue.

1. The GPU worker publishes a heartbeat to Redis every 15 seconds, with a 60-second TTL: its job capacity (thread count) and a rolling average job duration.
2. Each backend keeps a Redis set of its queued and processing job IDs, so counting is cheap and no job keys are scanned per request. The cleanup service removes stale IDs, for example after a crash.
3. When a job is queued, the API estimates its local wait from the active local jobs, the capacity, and the average duration. Without a heartbeat, local capacity counts as zero.
4. If the wait is longer than `OUTSOURCE_MAX_WAIT` and fewer than `OUTSOURCE_MAX_CONCURRENCY` jobs are remote, the job goes to the `remote` queue. Otherwise it stays local.
5. The job's `backend` field is `local` or `remote`, and the status endpoint returns it.

A local job that does not fit in GPU memory even on its own is also sent remote, once, if outsourcing is enabled and a remote slot is free.

### Remote flow

1. `remote-worker` claims the job like a local worker would, and downloads and converts the audio with `yt-dlp` and `ffmpeg`. Downloads stay on this server because video sites often block datacenter IPs.
2. It compresses the WAV to FLAC, uploads it to the bucket under a random key, and creates a presigned GET URL valid for `OUTSOURCE_TIMEOUT` plus five minutes.
3. It submits `{"audio_url": ..., "job_id": ...}` to RunPod `/run`, then polls `/status` every 2 seconds. RunPod progress (0 to 1) is shown as 0.15 to 0.95, the same range as model progress for local jobs.
4. The handler downloads only from the storage host and transcribes the audio in the same batcher as the local worker: one RunPod worker takes several requests at once and runs them as one batch, up to its GPU's job capacity. It returns the MIDI as base64.
5. The dispatcher writes the MIDI to the job directory and completes the job. The storage object is deleted in every case.

When the user cancels, the dispatcher calls RunPod `/cancel` and stops. When a remote job fails or exceeds `OUTSOURCE_TIMEOUT`, it is cancelled on RunPod; with `OUTSOURCE_FALLBACK_LOCAL=true` it is put back on the local queue with a flag that stops it from being sent remote again. Otherwise it fails. Short network errors while polling are retried up to five times. Download and conversion errors are not retried locally, because they would fail the same way.

### Billing

Remote and local jobs share one lifecycle (`run_job` in `app/worker/tasks.py`), so they reserve, settle, and release minutes the same way. A job keeps its reservation when it moves between backends, and every billing call is idempotent per job, so a job is never charged twice.

## ⚠️ Limitations

Transcription quality depends on the source recording. Dense arrangements, multiple instruments, noise, reverb, sustain effects, and ambiguous note offsets can produce incorrect or missing notes. Generated MIDI may need manual cleanup. The service does not provide MIDI editing, sheet-music generation, playback controls, performance feedback, or piano-roll visualization.
