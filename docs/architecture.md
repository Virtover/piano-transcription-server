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
* `remote-worker` consumes the `remote` queue when `OUTSOURCE_PROVIDER` is set; with `none` it only idles. It always starts, so `.env` is the one switch. It downloads and converts audio here, then runs the model on RunPod. It runs on the API image and never loads torch or the model.
* `cleanup` removes files whose Redis job state has expired.
* `redis` stores job state and queues work.
* `cloudflared` is optional and starts only with `COMPOSE_PROFILES=cloudflare`. It connects out to a Cloudflare Tunnel and forwards HTTPS requests to the API. See [HTTPS through a Cloudflare Tunnel](cloudflare-tunnel.md).
* The shared `data` volume stores job files. PostgreSQL keeps the billing database in `billing-data/postgres`, or in a Docker volume with `POSTGRES_DATA_LOCATION` (see [Where the database is kept](configuration.md#where-the-database-is-kept)); verified dumps are stored separately in `billing-data/backups`. Only the API mounts `billing-data`: the workers process untrusted media and reach billing only through PostgreSQL.

The main implementation surfaces are `app/main.py`, `app/api/routes/`, `app/worker/`, and `app/transcription/`.

## 🎼 Transcription pipeline

1. Download source audio and metadata with `yt-dlp`, or use the uploaded file.
2. Convert audio to 44.1 kHz mono WAV with `ffmpeg`.
3. Run Transkun in overlapping 16-second segments with an 8-second hop.
4. Write the MIDI file.
5. Serve the result from the API.

The video site is asked once per job. When a link is submitted, the API reads the video's information with `yt-dlp` to check its length, and saves it in the job directory. The worker downloads with that information instead of asking the site again. It asks again only if the information is older than an hour or its audio links no longer work: they are signed and expire. It never asks again after the site refused the server or said the video cannot be read. A check is also reused for 5 minutes for the same URL, so asking the cost of a video and then submitting it is one request.

Every request to a video site, from the API or a worker, goes through `app/video_sites.py`, which keeps its state per site in Redis, shared by the API and every worker. Requests that read a link from a site start `VIDEO_SITE_INTERVAL` apart, and at most `VIDEO_SITE_MAX_DOWNLOADS` downloads from it run at once. A rate limit or bot check, or repeated timeouts, open the site's breaker: its links are refused until a cooldown ends, then read one at a time until one succeeds. See [When a video site refuses the server](api.md#-when-a-video-site-refuses-the-server).

The Transkun checkpoint detects sustain-pedal events separately instead of automatically extending note durations until pedal release. Progress during model execution is an estimate because the Transkun command does not expose exact per-segment progress.

## 🗄️ Job retention and restart behavior

Job files are stored under `/data/jobs/<job_id>/`. Queued and processing jobs do not receive a Redis expiration.

A worker holds a lease on the job it runs: a Redis key it renews every 15 seconds and that expires after 60. A processing job whose lease has expired lost its worker, for example to a crash. The API checks every 30 seconds and at startup: such a job is marked failed, its billing reservation is released, and its partial files are removed. A job whose worker is still running keeps running, so restarting only the API interrupts nothing. On API startup, queued jobs are sent to their queue again and keep their reservation, so a restart does not lose them. A message for a queued job left over from before the restart is harmless, because only one delivery can claim a job.

Completed, failed, and cancelled jobs receive a Redis TTL of `RESULT_RETENTION` (default `1h`): until then their status and MIDI file can be retrieved. Every `CLEANUP_INTERVAL`, the cleanup service removes job directories whose Redis keys no longer exist, so a file can stay on disk, though no longer downloadable, for up to that long after its job expires.

Deleting an account removes the user's Redis jobs and job directories immediately. A worker still processing one of those jobs sees it as cancelled at its next progress check and stops.

### Uploaded files

The API streams an upload into `/data/jobs/<job_id>/upload.input`, stopping as soon as `MAX_UPLOAD_SIZE_MB` is exceeded. During the upload the job has the status `uploading` and a one-hour TTL. That keeps the cleanup service from removing the directory, and lets the cleanup service remove it if the API stops mid-upload. After `ffprobe` accepts the file and the cost is reserved, the job becomes `queued` like any other job.

Uploaded files are untrusted. Only common audio and video container formats are accepted. Playlist and concat formats such as HLS are rejected because they make `ffmpeg` open other files or URLs. `ffprobe` and `ffmpeg` run with `-protocol_whitelist file`, so they cannot open network URLs at all. The worker checks the format again before converting.

Billing backups are thinned out by the API after each backup, and optionally copied off-site; see [Billing database backups](configuration.md#-billing-database-backups).

### ⚡ Crashes and power cuts

* **Billing database.** PostgreSQL commits transactions durably through WAL. Each balance change is one transaction, and PostgreSQL advisory transaction locks serialize reservations, settlements, purchases, and refunds. At startup the API settles or releases reservations whose job ended while it was down, and releases those whose job no longer exists in Redis.
* **Redis.** Job state and the queues are written to an append-only file synced every second, so a power cut loses at most about a second of changes. A job lost that way is refunded at the next API startup, as above.
* **Running jobs** fail and are refunded when their worker stops; see above. They are not resumed.
* **A replaced billing database.** The API refuses to start when the billing database was created after the newest backup: PostgreSQL then started empty, for example in a new `POSTGRES_DATA_LOCATION`, and serving it would show every balance as zero.
* **Billing backups** are synced to disk before they are renamed into place, so a power cut leaves either the complete backup or none.
* **Starting again.** Every service restarts by itself, but only once Docker runs. Docker Desktop starts only when a user signs in, so a machine that should serve again after a power cut needs Docker to start at boot, or automatic sign-in, and to be set to power on when power returns.
* **Logs** are kept to about 100 MB per container, so a failing service cannot fill the disk with them.
* **Completed results.** MIDI is written to a same-directory temporary file, synced, atomically renamed into place, and checked before Redis records `completed`. At startup, completed jobs are checked again; a missing, invalid, or out-of-data-directory result is changed to `failed` and its unfinished billing reservation is released.
* **`/ready`** answers `503` while Redis or the billing database cannot be reached. `/health` only says that the API process answers.
* **Security.** PostgreSQL is not published as a host port and its password is mounted as a Docker secret. Redis is not published either, and refuses clients without a password; see [Redis users](#-redis-users). Off-site backup endpoints must use HTTPS. Direct API HTTP is suitable only on a trusted local network; use the Cloudflare Tunnel or another TLS reverse proxy for public access.

Redis started with the append-only file on, over a volume that has only a snapshot, starts empty. `scripts/server.py` turns it on in the running Redis before it restarts or stops anything, so updating with it keeps queued jobs. When updating from an older version with `docker compose` directly, run this first:

```shell
docker compose exec redis redis-cli config set appendonly yes
```

That works on a Redis from before [Redis users](#-redis-users). On one with users, `redis-cli` signs in as `operator`; `scripts/server.py` does that for you.

None of this protects against losing the disk or the machine. For that, turn on [off-site copies](configuration.md#off-site-copies) of the billing backups. Off-site copy failures and backups that stop advancing are logged as operational errors; the backup state is also visible in the administration status. They do not make `/health` fail, and `/ready` remains focused on Redis and billing database reachability.

With `ERROR_REPORT_URL` set, the API and the workers also send every error they log, and every job that fails without a known cause, to a website, with secrets and signed links removed. See [Error reports](configuration.md#error-reports).

### 🔁 Planned restarts and shutdowns

Restart or stop the stack with `scripts/server.py` on the host instead of running `docker compose` directly. It announces the restart or shutdown to clients first, and needs only Python 3 and the docker CLI:

```shell
python scripts/server.py restart 5m                  # restart in 5 minutes, for example to update
python scripts/server.py restart 10m --reason "Faster transcription" --grace 10m
python scripts/server.py stop 10m --back-in 2h       # shut down in 10 minutes, back in about 2 hours
python scripts/server.py start                       # start again and wait until ready
python scripts/server.py start --legacy-gpu          # worker for older GPUs such as the GTX 1050 Ti
python scripts/server.py status
python scripts/server.py cancel                      # also stops a waiting script
```

**Restart:**

1. **Announce.** The notice is stored in Redis, and clients see it in `GET /api/server-info`, job status, and the `X-Server-Maintenance` headers; see [Planned restarts and shutdowns](api.md#-planned-restarts-and-shutdowns). It expires on its own shortly after the planned time plus the grace period, so a crashed script cannot leave it behind.
2. **Build while serving.** The new images are built during the countdown, and the current version keeps serving. If the build fails, the restart is cancelled and the notice removed. If the build takes longer than the countdown, the restart waits for it.
3. **Refuse jobs that would not finish.** New jobs whose estimated wait and run time ends after the planned time are refused with `503`, without charge; in the last minute, all are.
4. **Drain.** At the planned time, running jobs get up to `--grace` (default `5m`) to finish. Queued jobs wait and are queued again after the restart. Then Redis's append-only file is turned on, if it is not yet, and the script waits until it is written; see [Crashes and power cuts](#-crashes-and-power-cuts).
5. **Restart** with `docker compose up -d --build`, which recreates only services whose image or configuration changed. Profiles given with `--profile` apply to the build and the restart; none are needed for outsourcing. `--command` replaces the restart command; then nothing is built before the countdown ends.
6. **Clear** the notice once the API answers `/ready`. If it does not within 3 minutes, the script stops with an error and the notice expires on its own.

**Shutdown** (`stop`) announces, refuses late jobs, and drains the same way, without building. Then it stops every service with `docker compose --profile "*" stop`, so services from any profile stop too. With `--down`, the containers are removed as well; volumes and the billing data are kept either way. `--back-in` tells clients when the server is expected back. Queued jobs stay in Redis and run after `python scripts/server.py start`. The notice stays in Redis while the server is off, and the API removes it when it starts.

Ctrl+C during a countdown cancels it and removes the notice. The script runs `python -m app.maintenance` in the `api` container, so the stack must be running, with a version that includes it.

### 🔐 Redis users

Redis holds every job, the queues, and the [limits](administration.md#-limits), so anything that can send it commands can change them. It is not published as a host port, and it refuses a client without a user and password. The users and what each may do are in `redis/users.acl`:

| User | Who | May |
| --- | --- | --- |
| `default` | anyone without a password | nothing |
| `app` | the API, the workers, and cleanup | every key and every command they use; not those that empty Redis, read or change its configuration or users, write its files, load code, or copy the data elsewhere |
| `operator` | `scripts/server.py`, through `redis-cli` inside the `redis` container | everything, for example turning on the append-only file |
| `admin-read-only` | the [admin web page](administration.md#-admin-web-page) in read-only mode | read commands only, on the keys the page shows; no key can be written |

Each user's password is a file in `secrets/redis`, which `scripts/server.py start` and `restart` create when it is missing. At every start, `redis/start.sh` gives Redis the users with the SHA-256 hash of each password, so no password is in a file Redis reads, a command line, or `docker inspect`. Only the `redis` service sees the whole folder. The others get their own password at `/run/redis/password` (`REDIS_PASSWORD_FILE`), and those that mount `secrets/` have `secrets/redis` covered by an empty folder, so a worker that processes a malicious file cannot read the `operator` password.

`check --online` confirms that Redis refuses a client without a password and that the `app` user cannot read Redis's configuration. To change a password, replace its file and recreate Redis and the services: `docker compose up -d --force-recreate`.

### 🩺 Job diagnostics

Redis job state disappears `RESULT_RETENTION` after a job ends, so each job also has a record in the `job_diagnostics` table of the billing database. Because it is in that database, the billing backups include it. A record is written when the job is queued, starts, is moved between backends, and ends, and holds:

* job ID, user ID, source (`url` or `upload`), and the source URL (none for uploads; the file name is not stored),
* audio duration and reserved cost,
* status, the full error (the type, message, and a failed command's output; users only see a short message), backend, whether the job was outsourced, and why it was last moved between backends,
* times queued, first started, first finished, and last updated (Unix seconds).

Records older than `BILLING_JOB_DIAGNOSTICS_RETENTION` (default `30d`) are deleted before each backup. Deleting an account deletes its records. Writing a record never fails a job; errors are logged.

To look at recent failures, use `python -m app.admin jobs --failed` and `jobs show <job>`; see [Administration](administration.md#-jobs). A backup copy can also be queried directly:

```sql
SELECT job_id, to_timestamp(created_at), backend, outsourced, error
FROM job_diagnostics WHERE status = 'failed' ORDER BY created_at DESC LIMIT 20;
```

## ☁️ Outsourcing

With `OUTSOURCE_PROVIDER=runpod`, jobs can run on RunPod Serverless when the local GPU is busy.

### Routing

Overload means a long **expected wait**, not low VRAM. The worker's thread count already caps GPU concurrency, so overload shows up as jobs waiting in the queue.

1. The GPU worker publishes a heartbeat to Redis every 15 seconds, with a 60-second TTL: its job capacity (thread count) and a rolling average job duration.
2. Each backend keeps a Redis set of its queued and processing job IDs, so counting is cheap and no job keys are scanned per request. The cleanup service removes stale IDs, for example after a crash.
3. When a job is queued, the API estimates its local wait from the active local jobs, the capacity, and the average duration. Without a heartbeat, local capacity counts as zero.
4. With `OUTSOURCE_ROUTING=local_first`, if the wait is longer than `OUTSOURCE_LOCAL_FIRST_MAX_WAIT` and fewer than `OUTSOURCE_MAX_CONCURRENCY` jobs are remote, the job goes to the `remote` queue. With `remote_first_overflow`, every new job tries RunPod and uses local capacity when RunPod is full. With `remote_first_fallback`, every new job enters the remote-pending queue and waits for a RunPod slot; local work is used only after a genuine remote failure or timeout when fallback is enabled.
5. The job's `backend` field is `local` or `remote`, and the status endpoint returns it.

A local job that does not fit in GPU memory even on its own is also sent remote, once, if outsourcing is enabled and a remote slot is free.

### Remote flow

1. `remote-worker` claims the job like a local worker would, and downloads and converts the audio with `yt-dlp` and `ffmpeg`. Downloads stay on this server because video sites often block datacenter IPs.
2. It compresses the WAV to FLAC, uploads it to the bucket under a random key, and creates a presigned GET URL valid for `OUTSOURCE_TIMEOUT` plus five minutes.
3. It submits `{"audio_url": ..., "job_id": ...}` to RunPod `/run`, then polls `/status` every 2 seconds. RunPod progress (0 to 1) is shown as 0.15 to 0.95, the same range as model progress for local jobs.
4. The handler downloads only from the storage host and transcribes the audio in the same batcher as the local worker: one RunPod worker takes several requests at once and runs them as one batch, up to its GPU's job capacity. It returns the MIDI as base64.
5. The dispatcher writes the MIDI to the job directory and completes the job. The storage object is deleted in every case.

When the user cancels, the dispatcher calls RunPod `/cancel` and stops. A remote-pending job waits for a remote concurrency slot and can be cancelled while waiting. When a remote job fails or exceeds `OUTSOURCE_TIMEOUT`, it is cancelled on RunPod; with `OUTSOURCE_FALLBACK_LOCAL=true` it is put back on the local queue with a flag that stops it from being sent remote again. Otherwise it fails. Short network errors while polling are retried up to five times. Download and conversion errors are not retried locally, because they would fail the same way.

### Billing

Remote and local jobs share one lifecycle (`run_job` in `app/worker/tasks.py`), so they reserve, settle, and release minutes the same way. A job keeps its reservation when it moves between backends, and every billing call is idempotent per job, so a job is never charged twice.

## ⚠️ Limitations

Transcription quality depends on the source recording. Dense arrangements, multiple instruments, noise, reverb, sustain effects, and ambiguous note offsets can produce incorrect or missing notes. Generated MIDI may need manual cleanup. The service does not provide MIDI editing, sheet-music generation, playback controls, performance feedback, or piano-roll visualization.
