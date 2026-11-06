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
* `worker` consumes the `default` queue, and the `arrangement` queue when it can make [piano arrangements](#-piano-arrangements): it downloads audio, or reads the uploaded file, and runs the model on this machine's GPU or CPU.
* `remote-worker` consumes the `remote` queue when `OUTSOURCE_PROVIDER` is set; with `none` it only idles. It always starts, so `server.env` is the one switch. It downloads and converts audio here, then runs the model on RunPod. It runs on the API image and never loads torch or the model.
* `cleanup` removes files whose Redis job state has expired.
* `redis` stores job state and queues work.
* `cloudflared` is optional and starts only with `COMPOSE_PROFILES=cloudflare`. It connects out to a Cloudflare Tunnel and forwards HTTPS requests to the API. See [HTTPS through a Cloudflare Tunnel](cloudflare-tunnel.md).
* The shared `data` volume stores job files. PostgreSQL keeps the billing database in `billing-data/postgres`, or in a Docker volume with `POSTGRES_DATA_LOCATION` (see [Where the database is kept](configuration.md#where-the-database-is-kept)); verified dumps are stored separately in `billing-data/backups`. Only the API mounts `billing-data`: the workers process untrusted media and reach billing only through PostgreSQL.

The main implementation surfaces are `app/main.py`, `app/api/routes/`, `app/worker/`, and `app/transcription/`.

## 🎼 Transcription pipeline

1. Download source audio and metadata with `yt-dlp`, or use the uploaded file.
2. Convert audio to 44.1 kHz mono WAV with `ffmpeg`.
3. Run Transkun in overlapping 16-second segments with an 8-second hop.
4. Find the beats and downbeats with Beat This!, and write the MIDI file on them: the [tempo map](#-tempo-map).
5. Serve the result from the API.

The video site is asked once per job. When a link is submitted, the API reads the video's information with `yt-dlp` to check its length, and saves it in the job directory. The worker downloads with that information instead of asking the site again. It asks again only if the information is older than an hour or its audio links no longer work: they are signed and expire. It never asks again after the site refused the server or said the video cannot be read. A check is also reused for 5 minutes for the same URL, so asking the cost of a video and then submitting it is one request.

Every request to a video site, from the API or a worker, goes through `app/video_sites.py`, which keeps its state per site in Redis, shared by the API and every worker. Requests that read a link from a site start `VIDEO_SITE_INTERVAL` apart, and at most `VIDEO_SITE_MAX_DOWNLOADS` downloads from it run at once. A rate limit or bot check, or repeated timeouts, open the site's breaker: its links are refused until a cooldown ends, then read one at a time until one succeeds. See [When a video site refuses the server](api.md#-when-a-video-site-refuses-the-server).

The Transkun checkpoint detects sustain-pedal events separately instead of automatically extending note durations until pedal release. Progress during model execution is an estimate because the Transkun command does not expose exact per-segment progress.

### 🥁 Tempo map

Transkun gives every note's time in seconds; by itself the MIDI file would be at a fixed 120 BPM, so in a notation program or DAW the bar lines would not follow the music. `app/transcription/tempo.py` gives it the beats and bars of the recording (`WORKER_TEMPO_MAP`, on by default):

* [Beat This!](https://github.com/CPJKU/beat_this) finds the beats and downbeats in the audio. Its weights are part of the worker and RunPod images. It runs on the job's GPU, one job at a time, in 0.25 GiB that GPU job capacity reserves for it (see [GPU memory and batching](configuration.md#-gpu-memory-and-batching)); if another program has taken that memory, on CPU. Jobs on CPU track beats on CPU.
* Every beat becomes one quarter note, with a tempo change at every beat, so the grid follows rubato and tempo changes. Time signatures come from the downbeats, and notes before the first downbeat form a pickup bar.
* No note is moved and nothing is quantized: every event keeps its time in seconds to within a millisecond, so the MIDI plays back exactly as before. Syncopation, swing, and grace notes keep their timing and sit off the beat where they belong.
* Repairs: a beat missed between two others is filled in, and a spurious beat between two beats is dropped. A single bar of another length, such as one 2/4 bar among 4/4 bars, is followed, and a missed downbeat is filled with bars of the main meter.
* Beats that cannot be trusted leave the MIDI at 120 BPM, as Transkun wrote it, rather than give it a wrong grid: fewer than 8 beats, a tempo outside 20 to 400 BPM, more than 15% of beats off the local tempo by over 30%, fewer than two downbeats, or fewer than three quarters of the bars in one meter. So does any error in beat tracking: it never fails a job. The worker log says which.
* Memory does not grow with the audio's length: the spectrogram is made on CPU two minutes at a time, and only the weights and one 30 s chunk are on the GPU. Measured on an RTX 3060 Ti: 228 MiB of VRAM at most, the same for 2.4, 20, and 60 minutes of audio.
* Measured on 142 s of audio: 0.23 s on an RTX 3060 Ti, 4.2 s on 4 CPU threads; 60 minutes take 7 s on that GPU.
* Safeguards: the weights are checked against their SHA-256 when the image is built and never downloaded at run time. Before the file is replaced, every event of the new MIDI is checked to play at its original time. The new file is written beside the old one and moved over it, so a failed write, such as on a full disk, leaves the MIDI as Transkun wrote it; the whole result is then synced to disk before it replaces the job's MIDI, as without a tempo map.
* Limits: the beat is the level Beat This! hears, usually the quarter note but sometimes the half or eighth note. Compound meters such as 6/8 become 2/4 with triplets. Free passages without a pulse, such as cadenzas, can make the beats too irregular for a map.

## 🎹 Piano arrangements

Unless `WORKER_ARRANGEMENT=false`, a job can have `mode` `arrangement`: instead of transcribing a piano recording, the worker makes a piano cover of a song, whatever the song's instruments. The model is [AMT-APC](https://github.com/310hz/amt-apc) ([paper](https://arxiv.org/abs/2409.14086)), which hears the audio and writes the notes of a piano arrangement. Its code is MIT-licensed and vendored in `app/transcription/amt_apc`; its weights, `apc.pth`, are part of the worker and RunPod images, checked against their SHA-256 when the image is built and loaded as plain tensors (`weights_only`), never downloaded at run time. For how to ask for one, see [Piano arrangements](api.md#-piano-arrangements) in the API guide.

* **Job path.** The job is created, billed, downloaded, and converted like a transcription, and bills the same minutes for the same length. Only the step that makes the notes differs: AMT-APC instead of Transkun. The audio is resampled with torchaudio's `Resample`, as in AMT-APC's training; with soxr about 30% of the notes differed. The MIDI is named `arrangement.mid`.
* **Queue.** Arrangements use their own Dramatiq queue, `arrangement`. A worker consumes it only if it can arrange: `WORKER_ARRANGEMENT` is not `false` and, on a GPU, one arrangement step fits the memory budget; a CPU worker always can. The worker log says `Starting Dramatiq on queue(s) default, arrangement`, and a GPU that takes arrangements shows `arrangements,` in its summary. Heartbeats carry `arrangement` as `1` or `0`.
* **Routing.** The API refuses an arrangement with `409` when `WORKER_ARRANGEMENT` is off. Without outsourcing, it refuses with `503` when live workers exist and none has room for arrangements; with no live worker the job is queued, as a transcription is. With outsourcing, an arrangement goes to RunPod when no live local worker arranges, or when the local wait is long, as for [any job](#routing). A remote arrangement that fails goes back to the local queue only if a local worker arranges. After a restart with `WORKER_ARRANGEMENT` turned off, queued arrangements fail with `Piano arrangements were turned off on this server` and their minutes are given back. On RunPod the endpoint reads the same default, so it arranges unless `WORKER_ARRANGEMENT=false` is set on it; set it on both or neither.
* **Memory budget.** Every model step on a device runs in one shared budget (`app/transcription/budget.py`): a Transkun step takes its jobs times `WORKER_MEMORY_PER_JOB_GIB`, an arrangement step `WORKER_ARRANGEMENT_MEMORY_GIB`. A step waits for memory instead of running out of it, steps are served in the order they arrived so none starves, and a step larger than the whole budget runs alone. This is how transcriptions and arrangements share one GPU; see [GPU memory and batching](configuration.md#-gpu-memory-and-batching) for the numbers. On CPU, steps of both models run one at a time: both at once took 296 s on an i7-10700K, against 84 s and 175 s one after the other.
* **Audio windows.** AMT-APC reads 512 frames, about 8.2 s of audio, at a time, with no overlap. A step is one window, whatever the song's length, so memory does not grow with it, and the worker takes each job's next window in turn so every arrangement progresses. The original code needed about 3.1 GiB for a window; the vendored model computes the same results in chunks of 64 frames and 22 notes, which was checked to give the same notes, one for one.
* **Styles.** `level1` is sparse and `level3` dense; `level2` is the default. On five pop songs they gave about 5.7, 7.0, and 7.4 notes per second. The style does not change speed or memory.
* **Tempo map.** The result gets the same [tempo map](#-tempo-map) as a transcription, from Beat This!, with `WORKER_TEMPO_MAP`.
* **Speed.** On an RTX 3060 Ti, about 9.5 s for a 4-minute song alone, 27 times real time. With a transcription running at the same time in the same process, the transcription takes about 50% longer. On CPU (an i7-10700K, 8 threads) about 173 s for 4 minutes, 1.4 times real time; CPU times are approximate.
* **Quality.** A note held across a window boundary is sometimes cut and struck again: about one boundary in seven in measurements. This was measured and accepted rather than overlapping the windows. An arrangement is a new piece in the style of the song, not a transcription: its notes are the model's choice and need not match the recording.
* **Licence.** AMT-APC's code is MIT-licensed. Its weights come with no licence file of their own and descend from hFT-Transformer, whose checkpoints Sony distributes under CC BY-NC-SA 4.0, and which was trained on MAESTRO, also CC BY-NC-SA 4.0. Whether those terms bind weights fine-tuned from them is legally unsettled. Check it before using arrangements commercially; this guide is not legal advice.

## 🗄️ Job retention and restart behavior

Job files are stored under `/data/jobs/<job_id>/`, or in object storage with `JOB_STORAGE=s3`; see [Job storage](#job-storage). Queued and processing jobs do not receive a Redis expiration.

A worker holds a lease on the job it runs: a Redis key it renews every 15 seconds and that expires after 60. A processing job whose lease has expired lost its worker, for example to a crash. The API checks every 30 seconds and at startup: such a job is marked failed, its billing reservation is released, and its partial files are removed. A job whose worker is still running keeps running, so restarting only the API interrupts nothing. On API startup, queued jobs are sent to their queue again and keep their reservation, so a restart does not lose them. A message for a queued job left over from before the restart is harmless, because only one delivery can claim a job.

### Job storage

A job's lasting files are the uploaded file, the video information the API saved when it checked the link's length, and the MIDI result. `JOB_STORAGE` decides where they are kept (`app/job_storage.py`):

* **`local`** (default): in `/data/jobs/<job_id>/`, on the `data` volume that the API, the workers, and cleanup share. This needs every service on one machine.
* **`s3`**: in the bucket of `STORAGE_BUCKET`, which outsourcing also uses, under `jobs/<job_id>/`, so API replicas and workers on several machines share them. The API stores a submission's file before queueing it and keeps no copy. Each attempt at a job downloads the files into a scratch folder of its own, `/data/work/<job_id>.<attempt>`, and removes the folder when it ends. A job that moves to another backend on the same machine therefore cannot lose the files of its next attempt. The result is uploaded before the job is marked completed, and the upload and video information are deleted when the job ends. The MIDI download answers with a signed link to the bucket, so results do not pass through the API.

A completed job's `result` field names where its file is, a path or `s3:<key>`, and the download reads that rather than `JOB_STORAGE`. Results kept before `JOB_STORAGE` changed stay downloadable, if the bucket stays configured.

Expired jobs' files are deleted every `CLEANUP_INTERVAL`: the cleanup service removes folders on the `data` volume, including scratch folders left by a worker that crashed, and with `s3` the leading API deletes them from the bucket. The cleanup service gets no storage keys. A bucket with `JOB_STORAGE=s3` must serve one server only: cleanup deletes the job files its own Redis does not know, so a development copy needs a bucket of its own. The bucket's own automatic deletion catches what both miss; `python -m app.config requirements` gives the shortest safe lifetime, which grows with `RESULT_RETENTION`.

### Several API replicas

The API can run as several replicas that share Redis and PostgreSQL. One of them, the leader, runs the periodic tasks: billing backups and their health checks, the abandoned job check, and the voided-purchase sync. The lead is a Redis key that the leader renews every 10 seconds and that expires after 30. A replica that stops cleanly hands the lead over at once; one that crashes loses it when the key expires, and another replica takes over. `python scripts/admin.py status` shows which replica leads, by host name.

The startup recovery above runs in a replica that takes a lead no one held, which means no other API is running: the server started or restarted. A replica that joins running ones recovers nothing, because their jobs are not interrupted. A replica that gains the lead later, because the leader crashed or because this API restarted before its own lead expired, runs the same recovery then. Recovery corrects the routing counts without clearing them, so jobs another replica is starting meanwhile keep their place. With one API, as in the Compose setup, that API always leads and behaves as before.

Replicas share everything that decides an answer through Redis and PostgreSQL: jobs and their places, limits and blocks, maintenance notices, video sites' cooldowns and pacing, finished video checks, and the places for checks running now (`MAX_VIDEO_CHECKS`, counted across replicas). A replica keeps only what it can rebuild by itself: the Google sign-in tokens it verified in the last 5 minutes, the product list it read from Google Play at startup, and whether its own startup has finished. A load balancer can therefore send each request to any replica. With `JOB_STORAGE=s3`, a replica writes only scratch files to `DATA_DIR`, which may be a temporary folder of its own; with `local`, every replica needs the same `data` volume, so they must run on one machine.

Completed, failed, and cancelled jobs receive a Redis TTL of `RESULT_RETENTION` (default `1h`): until then their status and MIDI file can be retrieved. Every `CLEANUP_INTERVAL`, the cleanup service removes job directories whose Redis keys no longer exist, so a file can stay on disk, though no longer downloadable, for up to that long after its job expires.

Deleting an account removes the user's Redis jobs and job directories immediately. A worker still processing one of those jobs sees it as cancelled at its next progress check and stops.

### Uploaded files

The API streams an upload into `/data/jobs/<job_id>/upload.input`, stopping as soon as `MAX_UPLOAD_SIZE_MB` is exceeded. During the upload the job has the status `uploading` and a one-hour TTL. That keeps the cleanup service from removing the directory, and lets the cleanup service remove it if the API stops mid-upload. After `ffprobe` accepts the file and the cost is reserved, the job becomes `queued` like any other job.

Uploaded files are untrusted. Only common audio and video container formats are accepted. Playlist and concat formats such as HLS are rejected because they make `ffmpeg` open other files or URLs. `ffprobe` and `ffmpeg` run with `-protocol_whitelist file`, so they cannot open network URLs at all. The worker checks the format again before converting.

Billing backups are thinned out by the API after each backup, and optionally copied off-site; see [Billing database backups](configuration.md#-billing-database-backups).

### ⚡ Crashes and power cuts

* **Billing database.** PostgreSQL commits transactions durably through WAL. Each balance change is one transaction, and PostgreSQL advisory transaction locks serialize reservations, settlements, purchases, and refunds. At startup the API settles or releases reservations whose job ended while it was down, and releases those whose job no longer exists in Redis. The leading API also does so every 30 seconds for jobs that ended more than 5 minutes ago, so a worker or API that crashed between ending a job and settling it leaves no minutes reserved, even if no restart follows.
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
| `admin-read-write` | the admin web page in read-write mode | writes only jobs, limits, video sites' state, and its own confirmations; reads the rest the page shows; no scripts or dangerous commands |

Each user's password is a file in `secrets/redis`, which `scripts/server.py start` and `restart` create when it is missing. At every start, `redis/start.sh` gives Redis the users with the SHA-256 hash of each password, so no password is in a file Redis reads, a command line, or `docker inspect`. Only the `redis` service sees the whole folder. The others get their own password at `/run/redis/password` (`REDIS_PASSWORD_FILE`), and those that mount `secrets/` have `secrets/redis` covered by an empty folder, so a worker that processes a malicious file cannot read the `operator` password.

`check --online` confirms that Redis refuses a client without a password and that the `app` user cannot read Redis's configuration. Redis builds its users when it starts, and `scripts/server.py start` and `restart` make it read them again afterwards (`redis/start.sh reload`), so a user or password added by an update takes effect without restarting Redis. A reload that fails keeps the users Redis has. After changing a password file by hand, run `docker compose exec redis sh /redis/start.sh reload`, then restart the services that use it.

### Managed Redis and PostgreSQL

The Compose stack runs Redis and PostgreSQL itself. Several machines need them as services they all reach, usually managed ones; the server then connects by URL alone, and `python -m app.config requirements` lists what each service must provide when its URL leads outside the stack.

* **Redis:** a `rediss://` `REDIS_URL` encrypts the connection. A provider whose certificates its own CA signs needs that CA's file in `REDIS_CA_FILE`. `redis/start.sh` does not run there: create the users of `redis/users.acl` at the provider, or at least the services' user, which must not change the configuration. `check --online` confirms that Redis refuses strangers, and accepts a provider that removed `CONFIG` altogether. Redis must never evict keys, because queued jobs exist only there.
* **PostgreSQL:** the URL in `secrets/database_url` carries the TLS options, for example `postgresql://billing:<password>@db.example.com:5432/billing?sslmode=verify-full&sslrootcert=/run/secrets/postgres_ca.crt`, with the provider's CA file next to the other secrets. The server opens a connection per request and per job, so the database must allow one per request thread; a pooler in transaction mode works, because every lock the server takes lasts one transaction.
* **Backups:** the API's pg_dump reads servers of its own major version and older. With a provider that keeps point-in-time backups, `DATABASE_BACKUPS=provider` leaves backups to it and the server makes none; `check` warns as a reminder. Off-site copies need the server's backups, so they cannot be on then.

`check` warns when either URL leads outside the stack without TLS. A Redis failover with asynchronous replicas can lose the last second of writes: a job queued in it stays queued until the next full restart sends it again.

### 🩺 Job diagnostics

Redis job state disappears `RESULT_RETENTION` after a job ends, so each job also has a record in the `job_diagnostics` table of the billing database. Because it is in that database, the billing backups include it. A record is written when the job is queued, starts, is moved between backends, and ends, and holds:

* job ID, user ID, source (`url` or `upload`), and the source URL (none for uploads; the file name is not stored),
* audio duration and reserved cost,
* status, the full error (the type, message, and a failed command's output; users only see a short message), backend, whether the job was outsourced, and why it was last moved between backends,
* times queued, first started, first finished, and last updated (Unix seconds).

Records older than `JOB_DIAGNOSTICS_RETENTION` (default `30d`) are deleted before each backup. Deleting an account deletes its records. Writing a record never fails a job; errors are logged.

To look at recent failures, use `python -m app.admin jobs --failed` and `jobs show <job>`; see [Administration](administration.md#-jobs). A backup copy can also be queried directly:

```sql
SELECT job_id, to_timestamp(created_at), backend, outsourced, error
FROM job_diagnostics WHERE status = 'failed' ORDER BY created_at DESC LIMIT 20;
```

## ☁️ Outsourcing

With `OUTSOURCE_PROVIDER=runpod`, jobs can run on RunPod Serverless when the local GPU is busy.

### Routing

Overload means a long **expected wait**, not low VRAM. The worker's thread count already caps GPU concurrency, so overload shows up as jobs waiting in the queue.

1. Each GPU worker publishes a heartbeat of its own to Redis every 15 seconds, with a 60-second TTL: its job capacity (thread count) and a rolling average job duration. With several workers, on one machine or more, the API adds up their capacities and averages their job durations, weighing each worker by its capacity. A worker that stops cleanly removes its heartbeat; one that crashes stops counting within 60 seconds.
2. Each backend keeps a Redis set of its queued and processing job IDs, so counting is cheap and no job keys are scanned per request. The cleanup service removes stale IDs, for example after a crash.
3. When a job is queued, the API estimates its local wait from the active local jobs, the capacity, and the average duration. Without a heartbeat, local capacity counts as zero.
4. With `OUTSOURCE_ROUTING=local_first`, if the wait is longer than `OUTSOURCE_LOCAL_FIRST_MAX_WAIT` and fewer than `OUTSOURCE_MAX_CONCURRENCY` jobs are remote, the job goes to the `remote` queue. With `remote_first_overflow`, every new job tries RunPod and uses local capacity when RunPod is full. With `remote_first_fallback`, every new job enters the remote-pending queue and waits for a RunPod slot; local work is used only after a genuine remote failure or timeout when fallback is enabled.
5. The job's `backend` field is `local` or `remote`, and the status endpoint returns it.

A local job that does not fit in GPU memory even on its own is also sent remote, once, if outsourcing is enabled and a remote slot is free.

[Piano arrangements](#-piano-arrangements) use the same routing, but only workers that arrange count for their wait, and the heartbeat says whether a worker does.

### Remote flow

1. `remote-worker` claims the job like a local worker would, and downloads and converts the audio with `yt-dlp` and `ffmpeg`. Downloads stay on this server because video sites often block datacenter IPs.
2. It compresses the WAV to FLAC, uploads it to the bucket under a random key, and creates a presigned GET URL valid for `OUTSOURCE_TIMEOUT` plus five minutes.
3. It submits `{"audio_url": ..., "job_id": ...}` to RunPod `/run`, then polls `/status` every 2 seconds. RunPod progress (0 to 1) is shown as 0.15 to 0.95, the same range as model progress for local jobs.
4. The handler downloads only from the storage host and transcribes (or, for an arrangement, arranges) the audio in the same batcher as the local worker: one RunPod worker takes several requests at once and runs them as one batch, up to its GPU's job capacity. It adds the [tempo map](#-tempo-map) and returns the MIDI as base64.
5. The dispatcher writes the MIDI to the job directory and completes the job. The storage object is deleted in every case.

When the user cancels, the dispatcher calls RunPod `/cancel` and stops. A remote-pending job waits for a remote concurrency slot and can be cancelled while waiting. When a remote job fails or exceeds `OUTSOURCE_TIMEOUT`, it is cancelled on RunPod; with `OUTSOURCE_FALLBACK_LOCAL=true` it is put back on the local queue with a flag that stops it from being sent remote again. Otherwise it fails. Short network errors while polling are retried up to five times. Download and conversion errors are not retried locally, because they would fail the same way.

### Billing

Remote and local jobs share one lifecycle (`run_job` in `app/worker/tasks.py`), so they reserve, settle, and release minutes the same way. A job keeps its reservation when it moves between backends, and every billing call is idempotent per job, so a job is never charged twice.

## 📈 Metrics for scaling

`GET /metrics` answers in Prometheus's text format when the secret `metrics_token` exists, and only to a request with `Authorization: Bearer <token>`; without the secret it answers `404`. The server password does not apply to it, because the token protects it. Every API replica reads the same values from Redis, so a scraper can ask any of them; only `piano_api_requests_in_flight` is the replica's own.

| Metric | Labels | Meaning |
| --- | --- | --- |
| `piano_queue_jobs` | `queue`, `backend` | Jobs waiting to start: `default` (local workers, arrangements included) or `remote` (RunPod). |
| `piano_queue_oldest_job_age_seconds` | `queue`, `backend` | How long the oldest waiting job has waited, counted from when it entered that queue; 0 when none waits. |
| `piano_jobs_running` | `queue`, `backend` | Jobs being transcribed. |
| `piano_queue_active_jobs` | `queue`, `backend` | Both together: what workers are needed for. |
| `piano_local_workers`, `piano_local_worker_capacity` | | Local workers with a live heartbeat, and the jobs they run at once together. |
| `piano_local_workers_needed` | `queue`, `backend` | Local workers the active jobs need, at the jobs the live workers run at once on average; 1 for any job while no worker has a heartbeat. |
| `piano_remote_job_limit` | | Remote jobs allowed at once, with outsourcing on. |
| `piano_video_checks`, `piano_video_checks_limit` | | Video links being checked by every replica, and `MAX_VIDEO_CHECKS`. |
| `piano_api_requests_in_flight` | | Requests this replica is answering. |

Scale on the queue, not on CPU. A GPU worker that waits for its next job uses little CPU while users wait, and one that transcribes uses all of it however long the queue is.

* **Local workers:** run as many as `piano_local_workers_needed{queue="default"}`, which is `piano_queue_active_jobs` divided by the jobs a live worker runs at once on average, so workers on larger GPUs count for more and no setting has to say how many jobs one takes. Run one more while `piano_queue_oldest_job_age_seconds{queue="default"}` stays above what users should wait, for example 60 seconds. Scaling on waiting jobs alone would remove workers that are still transcribing once the queue is empty. Remove workers slowly, a few minutes apart: a new GPU worker loads the model before it takes jobs, which takes about a minute, and a stopping one finishes its jobs first.
* **The remote worker** only waits for RunPod, so one is enough; RunPod scales the GPUs.
* **API replicas:** on `piano_api_requests_in_flight` and CPU. Uploads and video checks hold a request for seconds, so in-flight requests rise before CPU does.

`python scripts/admin.py status` shows the same waits in its `Waiting` line.

## ⚠️ Limitations

Transcription quality depends on the source recording. Dense arrangements, multiple instruments, noise, reverb, sustain effects, and ambiguous note offsets can produce incorrect or missing notes. Generated MIDI may need manual cleanup. The service does not provide MIDI editing, sheet-music generation, playback controls, performance feedback, or piano-roll visualization.
