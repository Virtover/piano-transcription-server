# ⚙️ Configuration and environment

## 🧭 How configuration works

* **Choices go in `.env`.** Start from [.env.example](../.env.example), which lists the values operators usually change. Every setting is described below.
* **Wiring goes in `docker-compose.yml`.** Container paths and service addresses (`REDIS_URL`, `DATA_DIR`, `BILLING_DATABASE_PATH`, `BILLING_BACKUP_DIR`) are fixed in the Compose `environment:` sections. The code defaults suit local development outside Docker.
* **Secrets are files in `./secrets`.** Compose mounts the folder read-only at `/run/secrets`. The file name is the setting name. Secrets are never read from `.env` or the environment, and never appear in logs or error messages. See [Secrets](#-secrets).
* **The process environment overrides `.env`.** Empty values count as unset.

All settings are validated when a service starts. An invalid value stops the service with a message that names the variable. A variable that starts with `WORKER_`, `BILLING_`, `OUTSOURCE_`, `RUNPOD_`, `GOOGLE_PLAY_`, `CLOUDFLARE_`, `OFFSITE_`, or `ERROR_REPORT_` but is not a known setting is reported as a warning with the closest known name, for example `Unknown setting WORKER_BATCHSIZE; did you mean WORKER_BATCH_SIZE?`.

### ✅ Checking the configuration

```shell
docker compose run --rm api python -m app.config check
```

The command validates everything, prints the resolved configuration with secrets masked, and exits with code `1` on errors. Outside Docker, run `python -m app.config check --env-file .env --secrets-dir secrets`.

It works offline, so it cannot tell whether a key is *correct*. Add `--online` to test each configured service with the real credentials:

```shell
docker compose run --rm api python -m app.config check --online
```

| Service | Tested when | What is tested |
| --- | --- | --- |
| Storage | `OUTSOURCE_PROVIDER=runpod` | Uploads a small object, downloads it through a presigned URL like the RunPod handler, and deletes it. |
| RunPod | `OUTSOURCE_PROVIDER=runpod` | The endpoint's health, and its settings against the requirements below: image, `OUTSOURCE_STORAGE_ENDPOINT`, allowed CUDA versions, execution timeout, workers, scaling, and idle timeout. |
| Google sign-in | `BILLING_PROVIDER=google_play` | The format of `GOOGLE_OAUTH_CLIENT_IDS`. Google can only confirm a client ID with a signed-in user's token. |
| Google Play | `BILLING_PROVIDER=google_play` | Lists the products with the service account and compares them with `BILLING_PRODUCTS`. |
| Error reports | `ERROR_REPORT_URL` is set | Sends a test report, which the website shows as an info entry. |

Each result is `OK`, `WARN`, `FAIL`, or `SKIP`, and every warning or failure names the likely fix. The command exits with code `1` if any check fails. It never prints secrets or presigned URLs.

### 📋 External service requirements

```shell
docker compose run --rm api python -m app.config requirements
```

Prints what the external services must provide for outsourcing, sign-in, and billing: each requirement, its value, and the reason. Values are calculated from the configuration and the code, so they cannot go out of date. For example, the GPU endpoint's minimum execution timeout follows `OUTSOURCE_TIMEOUT`, and its requests per worker follow `WORKER_MODEL_MEMORY_GIB` and `WORKER_MEMORY_PER_JOB_GIB`. Requirements are stated as what they must achieve, not as a provider's setting names, so they stay valid when a provider changes its console. Those marked `[checked]` are verified by `check --online`. Add `--json` for scripts and AI agents.

### ⏱️ Value formats

* **Durations** use a number and a unit: `s`, `m`, `h`, `d`, or `w`, for example `2s`, `10m`, `1.5h`, `30d`. A plain number is rejected, so `600` cannot be mistaken for minutes.
* **`auto` or a number:** `WORKER_THREADS`, `WORKER_BATCH_SIZE`, and `WORKER_MAX_CONCURRENCY` accept `auto` or a positive integer.
* **Lists and mappings** are JSON on one line, for example `GOOGLE_OAUTH_CLIENT_IDS=["id-1","id-2"]`.
* Comments go on their own lines in `.env`.

## 📋 Settings

### Limits

| Variable | Default | Description |
| --- | --- | --- |
| `MAX_VIDEO_LENGTH_MINUTES` | `120` | Maximum video or uploaded file length in minutes. Set to `unlimited` to remove this limit. |
| `MAX_UPLOAD_SIZE_MB` | `200` | Maximum size of an uploaded file in MB. Set to `unlimited` to remove this limit; this can use substantial disk space. |
| `MAX_ACTIVE_JOBS` | `50` | Transcriptions being uploaded, checked, queued, or run at once, from all users. Further requests get `429`. Set to `unlimited` to remove this limit; this also removes the total upload/job-count guard. |
| `MAX_ACTIVE_JOBS_PER_USER` | `5` | The same for one signed-in user. Set to `unlimited` to remove this per-user limit. Only applies with billing; without it, all clients are one anonymous user and only `MAX_ACTIVE_JOBS` applies. |
| `SOURCE_URL_ALLOWED_HOSTS` | see below | JSON list of hosts accepted as transcription sources. Subdomains match too. |
| `VIDEO_SITE_COOLDOWN` | `30m` | How long links to a video site are refused after it rate-limits this server or asks it to prove it is not a bot. Each further incident in a row doubles it. Other sites and uploads are not affected. See [When a video site refuses the server](api.md#-when-a-video-site-refuses-the-server). |
| `VIDEO_SITE_MAX_COOLDOWN` | `1d` | The longest that doubling goes. Incidents stop counting as in a row once this long has passed without one. |
| `VIDEO_SITE_TIMEOUT_COOLDOWN` | `2m` | How long links to a video site are refused after it timed out 3 times within 5 minutes. Each further time in a row doubles it, up to `VIDEO_SITE_COOLDOWN`. A single timeout refuses nothing. |
| `VIDEO_SITE_INTERVAL` | `3s` | Least time between the starts of two requests that read a link from the same video site, from the API and the workers together. `0s` turns spacing off. Longer intervals make users wait for link checks when many arrive at once. |
| `VIDEO_SITE_MAX_DOWNLOADS` | `2` | Downloads from one video site at once, from all workers together, including downloads with the information the link check saved. Further jobs wait for a place. Set to `unlimited` to remove this concurrency limit. |
| `RESULT_RETENTION` | `1h` | How long a finished job can be retrieved: its status and, when completed, its MIDI file. Counted from when the job completed, failed, or was cancelled. Then the job is gone (`404`) and its files are deleted at the next cleanup. `GET /api/server-info` returns it as `result_retention_seconds`. Longer periods keep more files on disk. |
| `CLEANUP_INTERVAL` | `10m` | How often the cleanup service deletes the files of expired jobs. A file can stay on disk up to this long after its job expires, but cannot be downloaded. |

### Worker

| Variable | Default | Description |
| --- | --- | --- |
| `WORKER_THREADS` | `auto` | CPU threads for PyTorch and ffmpeg. `auto` uses physical cores minus two; a number is capped at the physical core count. |
| `WORKER_ENFORCE_CPU` | `false` | Ignore available CUDA GPUs and run the local worker on CPU. Useful for testing or when the GPU should be reserved for another workload. |
| `WORKER_MODEL_MEMORY_GIB` | `1.0` | VRAM reserved on each GPU for the model and GPU libraries. See [GPU memory and batching](#-gpu-memory-and-batching). |
| `WORKER_MEMORY_PER_JOB_GIB` | `1.1` | VRAM budget for each concurrent GPU job. |
| `WORKER_CPU_MODEL_MEMORY_GIB` | `2.0` | RAM reserved for the loaded model when CPU `WORKER_BATCH_SIZE=auto` estimates how many jobs fit. This is a planning reserve, not a hard memory limit. It does not affect GPU batching. |
| `WORKER_CPU_MEMORY_PER_JOB_GIB` | `2.0` | RAM estimated for each additional concurrent CPU transcription when CPU `WORKER_BATCH_SIZE=auto` estimates capacity. Increase it if CPU jobs cause memory pressure; decrease it only when measurements show more jobs fit. It does not affect GPU batching. |
| `WORKER_BATCH_SIZE` | `auto` | Maximum jobs combined into one model batch. `auto` uses each GPU's job capacity, or the lower of CPU thread capacity and the available-RAM estimate on CPU. A number is lowered to a GPU's capacity. In CPU mode, it is also the number of worker threads. See [below](#choosing-other-values). |
| `WORKER_BATCH_TIMEOUT` | `2s` | Maximum wait for more jobs before running a partial batch. |
| `WORKER_MAX_CONCURRENCY` | `auto` | Optional cap on concurrent GPU jobs. It can only lower the capacity calculated from VRAM. |

### Billing

| Variable | Default | Description |
| --- | --- | --- |
| `BILLING_PROVIDER` | `none` | `none` or `google_play`. |
| `BILLING_PRODUCTS` | `{}` | JSON mapping of Google Play product IDs to transcription minutes. |
| `FREE_MINUTES` | unset | Free minutes granted per period; omit to disable. |
| `FREE_MINUTES_PERIOD` | `30d` | How often free minutes are topped up. |
| `BILLING_BACKUP_INTERVAL` | `15m` | How often the billing database is backed up. |
| `BILLING_BACKUP_KEEP_ALL` | `1d` | Every backup younger than this is kept. See [Which backups are kept](#which-backups-are-kept). |
| `BILLING_BACKUP_KEEP_HOURLY` | `2d` | Then the first backup of each hour, up to this age. `0s` turns a step off. |
| `BILLING_BACKUP_KEEP_DAILY` | `14d` | Then the first of each day. |
| `BILLING_BACKUP_KEEP_WEEKLY` | `8w` | Then the first of each week. |
| `BILLING_BACKUP_KEEP_MONTHLY` | `0s` | Then the first of each month; off by default. |
| `BILLING_BACKUP_RETENTION` | unset | Optional upper limit: older backups are deleted whatever the steps keep. The newest backup is always kept. |
| `BILLING_JOB_DIAGNOSTICS_RETENTION` | `30d` | Age after which job diagnostics records are deleted. See [Job diagnostics](architecture.md#-job-diagnostics). |
| `GOOGLE_OAUTH_CLIENT_IDS` | `[]` | Public OAuth client IDs whose Google ID tokens are accepted. Required for billing. |
| `GOOGLE_PLAY_PACKAGE_NAME` | unset | Android package name used for Google Play verification. Required for billing. |
| `GOOGLE_PLAY_VOIDED_PURCHASES_INTERVAL` | `1h` | How often refunded and charged-back purchases are read from Google Play, and their minutes taken back. See [Refunds](google-play-billing.md#-refunds-and-chargebacks). |

`BILLING_PROVIDER=google_play` also requires the secret `google_play_service_account.json`. The API refuses to start if the package name, the OAuth client IDs, or the secret is missing. See the [Google Play billing guide](google-play-billing.md). The optional complete URL secret file is named `billing_database_url` (not an environment variable).

Production billing is PostgreSQL. The Compose `postgres` service is
reachable only on the internal Compose network, and keeps its live database
where `POSTGRES_DATA_LOCATION` says; see [Where the database is
kept](#where-the-database-is-kept). `python scripts/server.py start` creates a
random `secrets/postgres_password` when it is missing. PostgreSQL uses it only
when it creates the database, so changing the file later does not change the
password of an existing database.
Backups are verified custom-format `pg_dump` files. To move an existing
SQLite database, stop the old stack, start only PostgreSQL, and run the
one-time migration command:

**Windows (PowerShell)**

```powershell
docker compose down
docker compose up -d --wait postgres
docker compose run --build --rm --no-deps api python -m app.migrate_billing /billing/billing.sqlite3 "postgresql://billing@postgres:5432/billing"
```

**macOS and Linux**

```bash
docker compose down
docker compose up -d --wait postgres
docker compose run --build --rm --no-deps api python -m app.migrate_billing /billing/billing.sqlite3 "postgresql://billing@postgres:5432/billing"
```

The migration reads the PostgreSQL password from
`secrets/postgres_password`; it is not part of the command. It validates the
SQLite source, copies all billing tables, and compares row counts. If the
command reports `Billing migration completed and validated.`, start the full
stack with `docker compose up -d`.

The command validates SQLite integrity, copies all billing tables, and compares
row counts. It is never run automatically at startup. PostgreSQL schema setup is
serialized with an advisory lock so concurrent API/worker starts cannot
partially initialize it. `python -m app.backups
now` creates and verifies a `billing-<timestamp>.dump`; `fetch` validates
off-site dumps with `pg_restore --list` before exposing them for restore.

### Outsourcing

When local GPUs are overloaded, jobs can run on RunPod Serverless. Only the model runs remotely; downloading and converting stay on this server. See [Outsourcing to RunPod](runpod.md) and [how routing works](architecture.md#-outsourcing).

| Variable | Default | Description |
| --- | --- | --- |
| `OUTSOURCE_PROVIDER` | `none` | `none` or `runpod`. With `none`, nothing is outsourced and the `remote-worker` service is not needed. |
| `OUTSOURCE_ROUTING` | `local_first` | `local_first` uses the local wait threshold; `remote_first_overflow` tries RunPod for every new job and uses local capacity when RunPod is full; `remote_first_fallback` queues every new job remotely and leaves local capacity idle unless a remote job fails and fallback is enabled. |
| `OUTSOURCE_LOCAL_FIRST_MAX_WAIT` | `30s` | Only in `local_first`: outsource a job when its estimated local wait is longer than this. The estimate is a whole number of average local job durations, so a value below that average outsources as soon as every local slot is busy. `0s` always does. Remote-first modes do not use a local wait threshold. `OUTSOURCE_MAX_WAIT` remains accepted as a deprecated alias. |
| `OUTSOURCE_MAX_CONCURRENCY` | `4` | In `local_first` and `remote_first_overflow`, the maximum jobs assigned to the remote backend at once. In `remote_first_fallback`, it limits simultaneously running remote jobs while additional jobs wait in the remote-pending queue. `auto`: the RunPod endpoint's max workers times the requests one worker takes. See [below](#outsource_max_concurrencyauto). |
| `OUTSOURCE_TIMEOUT` | `15m` | How long a remote job may take, including RunPod's queue and cold start. It is then cancelled. |
| `OUTSOURCE_FALLBACK_LOCAL` | `true` | When a remote job fails or times out, put it back on the local queue instead of failing it. |
| `OUTSOURCE_STORAGE_ENDPOINT` | unset | S3-compatible endpoint used to hand audio to RunPod, for example `https://<account-id>.eu.r2.cloudflarestorage.com`. |
| `OUTSOURCE_STORAGE_BUCKET` | unset | Bucket for the audio files. |
| `OUTSOURCE_STORAGE_REGION` | `auto` | Storage region. `auto` for Cloudflare R2. |
| `OUTSOURCE_MAX_AUDIO_MB` | `512` | Read by the RunPod handler: largest audio file it downloads. |
| `RUNPOD_ENDPOINT_ID` | unset | ID of the RunPod Serverless endpoint. |
| `RUNPOD_API_BASE` | `https://api.runpod.ai/v2` | RunPod API URL. Change it only for local testing with the SDK's test server; the endpoint ID is then not added to the path. |

`OUTSOURCE_PROVIDER=runpod` requires the endpoint ID, both storage settings, and the three secret files below. The API and `remote-worker` refuse to start without them.

#### `OUTSOURCE_MAX_CONCURRENCY=auto`

`auto` sets the limit from RunPod: **max workers × requests per worker**.

* **Max workers** is the endpoint's setting in the RunPod console. The `remote-worker` reads it from RunPod's REST API when it starts and every 10 minutes, so console changes apply without a restart. The API key needs read access to the endpoint's settings.
* **Requests per worker** is how many jobs one RunPod worker transcribes at once: its GPU's job capacity, about 12 on 16 GB. Each worker reports it with every result. Until the first result, 12 is assumed.
* If max workers cannot be read, the limit is 4, and the `remote-worker` logs a warning. The last known value is kept when a later read fails.
* The `remote-worker` starts with enough threads for larger GPUs (24 per worker, at most 64). After raising max workers a lot, restart it so it has threads for the new limit.

A number is easier to reason about, because it limits what your server does: every remote job downloads and converts audio here, from your IP. `auto` suits deployments where the RunPod console should be the one place that sets capacity. Either way, RunPod's max workers is the ceiling on the bill.

### Cloudflare Tunnel

| Variable | Default | Description |
| --- | --- | --- |
| `COMPOSE_PROFILES` | unset | Read by Docker Compose: `cloudflare` starts the `cloudflared` service, which serves the API over HTTPS through a Cloudflare Tunnel. Separate several profiles with commas. |
| `CLOUDFLARE_TUNNEL_HOSTNAME` | unset | The tunnel's public hostname, such as `piano.example.com`, without `https://`. Only `check --online` and `requirements` use it. |

`COMPOSE_PROFILES=cloudflare` also requires the secret file `cloudflare/tunnel_token`; `check` fails without it. See [HTTPS through a Cloudflare Tunnel](cloudflare-tunnel.md).

### Admin web page

Optional. A page for the [admin commands](administration.md#-admin-web-page), on a hostname of its own, behind Cloudflare Access.

| Variable | Default | Description |
| --- | --- | --- |
| `ADMIN_WEB_MODE` | `disabled` | `disabled`, `read-only`, or `read-write`. Read when the service starts. The page can lower it for everyone; only `python scripts/admin.py web restore` raises it again. Also add `admin` to `COMPOSE_PROFILES`, which starts the service. |
| `ADMIN_WEB_HOSTNAME` | unset | The page's hostname, such as `admin.example.com`, on the same tunnel as the API but never the API's own. |
| `ADMIN_WEB_EMAILS` | `[]` | JSON list of the addresses that may use the page, for example `["you@example.com"]`. The page refuses every other address, whatever Access allows. |
| `CLOUDFLARE_ACCESS_TEAM_DOMAIN` | unset | Your Cloudflare Access team domain, such as `example.cloudflareaccess.com`. The page reads the keys that sign Access's tokens from it. |
| `CLOUDFLARE_ACCESS_AUD` | unset | The AUD tag of the Access application that protects `ADMIN_WEB_HOSTNAME`. The page accepts only tokens signed for it. |

With a mode other than `disabled`, every one of these is required, as are the Cloudflare Tunnel and the PostgreSQL billing database; the service does not start otherwise.

### Off-site backups

Optional, and on when `OFFSITE_BACKUP_BUCKET` is set. Then the other values and the secret files `offsite_backup_access_key` and `offsite_backup_secret_key` are required. See [Off-site copies](#off-site-copies).

| Variable | Default | Description |
| --- | --- | --- |
| `OFFSITE_BACKUP_ENDPOINT` | unset | HTTPS S3-compatible endpoint of the backup storage. Plain HTTP is rejected because it would expose backup credentials and data. |
| `OFFSITE_BACKUP_BUCKET` | unset | Bucket for the encrypted billing backups. Setting it turns off-site copies on. |
| `OFFSITE_BACKUP_REGION` | `auto` | Storage region. `auto` for Cloudflare R2. |
| `OFFSITE_BACKUP_RECIPIENT` | unset | The age public key (`age1...`) backups are encrypted to. Not a secret; the private key stays off this server. |

### Error reports

| Variable | Default | Description |
| --- | --- | --- |
| `ERROR_REPORT_URL` | unset | Where the API and the workers send their errors, such as the Piano Weave website's `https://pianoweave.com/api/server-errors`. Needs the secret file `error_report_token`. Must use `https://`, so the token is never sent in the clear; plain `http://` is accepted only for `localhost`. |

A report is sent for every error the API or a worker logs, and for every job that fails without a known cause: a failure such as a private video or an unsupported file is the user's, not the server's. Each report holds the error's type, message, traceback, and where it happened, never the user's file. Before a report is sent, every secret file's value, passwords in connection URLs, and the query of every link (where signed storage links keep their signature) are replaced. Redirects are not followed, so the token only goes to `ERROR_REPORT_URL`. The same error is sent at most once a minute, and at most 30 reports a minute in total. The cleanup service has no secrets, so it sends none.

The reports are sent from the server to the website, so they also work when the server is reachable only through the tunnel. They cannot tell the website that the server is down: a stopped server sends nothing. The website's status monitor of `/ready` covers that.

### Container wiring

Set by `docker-compose.yml`. Change them only when running the services outside Docker.

| Variable | Default | Compose value | Description |
| --- | --- | --- | --- |
| `REDIS_URL` | `redis://localhost:6379/0` | `redis://app@redis:6379/0` | Redis connection URL, with the Redis user the services sign in as. |
| `REDIS_PASSWORD_FILE` | empty | `/run/redis/password` | The file with that user's password, from `secrets/redis/app`. Empty for a Redis without users, such as one for local development. See [Redis users](architecture.md#-redis-users). |
| `DATA_DIR` | `/data` | `/data` | Shared directory for job files. |
| `BILLING_DATABASE_URL` | `postgresql://billing@postgres:5432/billing` | same | Production PostgreSQL URL for balances, purchases, and reservations. Keep passwords in `secrets/billing_database_url` (or use Compose's PostgreSQL password file). |
| `BILLING_DATABASE_PATH` | `/billing/billing.sqlite3` | tests/tools | Explicit SQLite path retained for legacy tests and the one-time migration source. |
| `BILLING_BACKUP_DIR` | `/billing/backups` | same | Directory for billing database backups. |

`POSTGRES_DATA_LOCATION` in `.env` is read by Docker Compose, not by the server: where PostgreSQL keeps the live billing database. See [Where the database is kept](#where-the-database-is-kept).

`BILLING_DATA_GROUP` is read by Docker Compose: the numeric host group that may manage the files in `billing-data`. `scripts/server.py` sets it to the group of the user running it, so it normally stays unset in `.env`. See [Running as a non-root user](setup.md#-running-as-a-non-root-user).

`API_BIND_ADDRESS` in `.env` is read by Docker Compose, not by the server: the network address the API's port `8000` is published on. The secure default is `127.0.0.1`, which keeps direct HTTP local-only. Set `0.0.0.0` only when clients on the network require direct access, and use HTTPS through a tunnel or reverse proxy for public access. See [Password protection](setup.md#-password-protection).

`WORKER_GPU_BUILD` in `.env` is read by Docker Compose when it builds the worker image: `current` (default) or `legacy`. The default installs PyTorch's CUDA 13 build, which no longer supports older NVIDIA GPUs such as the GTX 10 series (Pascal, for example the GTX 1050 Ti); on them the worker fails with "no kernel image is available". `legacy` installs the same PyTorch version built for CUDA 12.6, which still supports them, on a CUDA 12.6 base image, so it also runs with older drivers. The build fails if that PyTorch lacks Pascal support. `python scripts/server.py start --legacy-gpu` (also `restart` and `restore`) builds `legacy` for that run only; set it in `.env` to keep it. Rebuild after changing it: `docker compose up -d --build worker`.

Default for `SOURCE_URL_ALLOWED_HOSTS`:

```env
SOURCE_URL_ALLOWED_HOSTS=["youtube.com","youtu.be","youtube-nocookie.com","vimeo.com","soundcloud.com","bandcamp.com","dailymotion.com","instagram.com","instagr.am","facebook.com","fb.watch","tiktok.com","x.com","twitter.com","reddit.com","redd.it","twitch.tv"]
```

Write it on a single line. Subdomains match too, so `youtube.com` also covers `music.youtube.com`.

The API, worker, and cleanup service share the `data` volume at `/data`. Billing is stored separately in the host directory `./billing-data`, mounted at `/billing` only in the API and worker. The cleanup service has no access to the billing database. It runs on the API image, because it only deletes files and needs neither CUDA nor the model.

## 🔐 Secrets

Put each secret in `./secrets` as a file named after the setting. Compose mounts the folder read-only at `/run/secrets` for the API and worker.

| File | Used for |
| --- | --- |
| `google_play_service_account.json` | Google Play service-account JSON key, used to verify purchases and read the product catalog. |
| `runpod_api_key` | RunPod API key, restricted to the transcription endpoint. It runs, follows, and cancels jobs. |
| `runpod_read_api_key` | Optional. A RunPod key with read-only access to the account. Restricted keys cannot read endpoint settings, so `OUTSOURCE_MAX_CONCURRENCY=auto` and the endpoint settings check of `check --online` use this one. Without it, `auto` allows 4 remote jobs. |
| `outsource_storage_access_key` | Access key ID of the storage token. |
| `outsource_storage_secret_key` | Secret access key of the storage token. |
| `offsite_backup_access_key` | Optional. Access key ID for the off-site backup bucket. See [Off-site copies](#off-site-copies). |
| `offsite_backup_secret_key` | Optional. Secret access key for the off-site backup bucket. |
| `server_password` | Optional. When present, every request except `/health` and `/ready` needs this password, as the `X-Server-Password` header or HTTP Basic auth with any user name. At least 12 characters; the API refuses to start otherwise. See [Password protection](setup.md#-password-protection). |
| `postgres_password` | PostgreSQL password used by the API and workers. Mount it as `secrets/postgres_password`; never put it in `.env`. |
| `redis/operator`, `redis/app`, `redis/admin-read-only`, `redis/admin-read-write` | Redis passwords, one per [Redis user](architecture.md#-redis-users). `scripts/server.py` creates them; each service gets only its own. |
| `admin-web/postgres-read-only`, `admin-web/postgres-read-write` | The passwords of the [admin web page's](administration.md#-admin-web-page) PostgreSQL roles, one per mode. `scripts/server.py` creates it, and the API gives the role its rights. |
| `error_report_token` | Optional. The token sent with every error report to `ERROR_REPORT_URL`. The website must have the same token. Generate one with `python -c "import secrets; print(secrets.token_urlsafe(32))"`. |

The Cloudflare tunnel token is in a folder of its own, `secrets/cloudflare/tunnel_token`, so that it can be hidden: anyone with it can receive the API's traffic. Besides `cloudflared`, only the API sees it, for `check`; the workers, which process untrusted media, have the folder covered by an empty one. `scripts/server.py` creates the empty folder when it is missing, because Docker cannot cover a folder that does not exist. The Redis passwords are in `secrets/redis` for the same reason: only the `redis` service sees the folder. See [HTTPS through a Cloudflare Tunnel](cloudflare-tunnel.md).

Secrets are only read from these files: a variable with the same name in `.env` or the environment is ignored. Never commit the `secrets` folder.

The services run as user ID `10001`, not root. On Linux, the secret files must be readable by that user: files created with the commands in these docs are, unless your `umask` is strict. If `check` reports a secret as missing although the file exists, run `sudo chown 10001 secrets/*`.

## 🕰️ Deprecated names

Old names keep working. Each logs one deprecation warning when a service starts. Setting both the old and the new name is an error.

| Old name | Replacement | Notes |
| --- | --- | --- |
| `CLEANUP_INTERVAL_SECONDS` | `CLEANUP_INTERVAL` | A plain number is read as seconds. |
| `WORKER_BATCH_TIMEOUT_SECONDS` | `WORKER_BATCH_TIMEOUT` | A plain number is read as seconds. |
| `GOOGLE_PLAY_SERVICE_ACCOUNT_FILE` | the secret file `google_play_service_account.json` | Still read if set: a file name in `secrets`, or an absolute path. It takes priority over the secret file. |

### Migrating an existing deployment

An existing `.env` keeps working without changes; you only see deprecation warnings. To move to the new format:

1. Rename `CLEANUP_INTERVAL_SECONDS=86400` to `CLEANUP_INTERVAL=1d`, and `WORKER_BATCH_TIMEOUT_SECONDS=2` to `WORKER_BATCH_TIMEOUT=2s`.
2. Rename the service-account key in `secrets` to `google_play_service_account.json` and remove `GOOGLE_PLAY_SERVICE_ACCOUNT_FILE`.
3. Remove `REDIS_URL`, `DATA_DIR`, `BILLING_DATABASE_URL`, and `BILLING_DATABASE_PATH` from `.env`. Compose sets them now; values left in `.env` are overridden by Compose.
4. Remove any inline comments after values.
5. Run `docker compose run --rm api python -m app.config check`, then `docker compose up -d --build`.

`MAX_UPLOAD_SIZE_MB` limits disk use per upload. Uploads are written to the shared `DATA_DIR` and removed after conversion. Compressed files are small: an hour of MP3 at 192 kbit/s is about 85 MB. Uncompressed WAV is large: an hour of 16-bit 44.1 kHz stereo is about 635 MB. Raise the limit only if users need to upload long lossless recordings and the disk has room for several at once.

`WORKER_BATCH_SIZE` and `WORKER_MEMORY_PER_JOB_GIB` control different limits. The batch size controls how many queued requests are combined for one model inference call. The per-job VRAM setting estimates how many GPU jobs fit alongside the loaded model. A larger batch size never increases the calculated GPU capacity: on a GPU, the batch size is at most that GPU's capacity. See [GPU memory and batching](#-gpu-memory-and-batching).

## 🎛️ GPU memory and batching

### How capacity is calculated

When the worker starts, it reads the free VRAM of each GPU and calculates:

```text
jobs per GPU = floor((free VRAM - WORKER_MODEL_MEMORY_GIB) / WORKER_MEMORY_PER_JOB_GIB)
```

The total, optionally capped by `WORKER_MAX_CONCURRENCY`, becomes the number of worker threads, which is the number of jobs transcribed at the same time. If no GPU has room for one job, the worker runs on the CPU. The worker log prints the result, for example `up to 4 job(s) by VRAM (GPU 0: 4 job(s), 5.68 GiB free)`.

Free VRAM is the lower of two readings: CUDA's (`cudaMemGetInfo`) and `nvidia-smi`'s. They normally agree. Under Windows with Docker Desktop or WSL, CUDA does not count memory used by Windows programs, such as the desktop, browsers, or games, and reported 1.3 GiB too much free memory on an 8 GB card. `nvidia-smi` sees that usage. If `nvidia-smi` is unavailable, CUDA's reading is used.

Free VRAM is read once at startup, so memory used by other programs at that moment is taken into account. Programs that start using the GPU later are not. On a machine with a desktop, leave headroom with `WORKER_MODEL_MEMORY_GIB` or `WORKER_MAX_CONCURRENCY`.

The CUDA context, about 0.13 GiB, is created before free memory is read, so it is already excluded. `WORKER_MODEL_MEMORY_GIB` covers only what the worker loads after that.

### Measured usage

Measured on an RTX 3060 Ti (8 GB) with the worker image, Transkun 2.0, and 44.1 kHz audio. Allocated memory is what PyTorch tensors use; reserved memory is what PyTorch holds from the driver, which is what counts toward running out of memory.

| What | VRAM |
| --- | --- |
| Transkun 2.0 model: 13.6 M parameters, fp32 | 0.06 GiB |
| CUDA context (already excluded from free VRAM) | ~0.13 GiB |
| Model, GPU kernels, and library handles after the context | ~0.14 GiB |
| One job, allocated | ~0.75 GiB |
| One job, reserved | ~1.0 GiB |

The model itself is small. Most of the memory is the per-segment work for each job, mostly the interval scores computed for every pitch.

GPU kernels are loaded on first use (CUDA lazy loading, the default since CUDA 12.3). With `CUDA_MODULE_LOADING=EAGER`, all kernels are loaded at startup. The context then grows to ~0.74 GiB and the memory after the context to ~0.63 GiB. The default `WORKER_MODEL_MEMORY_GIB=1.0` covers both modes.

Per-job usage does not depend on video length or content. A job keeps its audio in system RAM and moves only the current 16-second segment to the GPU. Dense piano chords and white noise use the same memory.

| Video length | Jobs in batch | Reserved VRAM (incl. model) | Per job | Time per job |
| --- | --- | --- | --- | --- |
| 5 min | 1 | 1.08 GiB | 1.03 GiB | 14 s |
| 5 min | 2 | 2.04 GiB | 1.00 GiB | 11.5 s |
| 5 min | 4 | 3.96 GiB | 0.98 GiB | 10.3 s |
| 5 min | 6 | 5.88 GiB | 0.97 GiB | 9.8 s |
| 60 min | 1 | 1.08 GiB | 1.03 GiB | 147 s |

Times were measured without download and conversion, which usually take longer than transcription. Other GPUs give different times, but memory use should be similar.

The worker also uses system RAM for audio: about 20 MiB per minute of video for each running job, so about 1.2 GiB for a 60-minute video.

Example capacities with the default values:

| GPU memory free at startup | Jobs at the same time |
| --- | --- |
| 4 GiB | 2 |
| 6 GiB | 4 |
| 8 GiB | 6 |
| 12 GiB | 10 |
| 24 GiB | 20 |

### Choosing other values

**`WORKER_MEMORY_PER_JOB_GIB`** (default `1.1`, measured ~1.0)

* Lower, for example `0.8`: more jobs run at once. The capacity is then larger than the GPU can hold. When the GPU runs out of memory, every job in the batch fails, not just one. On Windows and WSL the driver may instead move memory to system RAM, which avoids the error but makes transcription much slower.
* Higher, for example `1.5`: more headroom for other programs on the GPU and for different driver or PyTorch versions, but fewer jobs at once, so more jobs wait in the queue. Free VRAM is wasted.

**`WORKER_MODEL_MEMORY_GIB`** (default `1.0`, measured ~0.14 with lazy loading, ~0.63 with eager loading)

* Lower, down to about `0.3`: room for one more job on small GPUs. Only safe with lazy loading. Eager loading, or a different CUDA, cuDNN, or driver version that loads more, can then cause out-of-memory errors.
* Higher: headroom for programs that start using the GPU after the worker, such as a browser or game on a desktop machine. Each extra 1.1 GiB removes one job.

**`WORKER_BATCH_SIZE`** (default `auto`)

* `auto` on GPU: each GPU's batch size is its job capacity from the startup calculation, capped by `WORKER_MAX_CONCURRENCY` if set, and at least 1. A GPU that fits 4 jobs batches up to 4; a GPU that fits 20 batches up to 20.
* A number on GPU is used as given, but never above that GPU's capacity. When it is lowered, the worker logs a warning at startup, for example `GPU 0: WORKER_BATCH_SIZE=6 is more than this GPU's capacity of 4 job(s); using 4`.
* On CPU, `auto` is the lower of the resolved CPU thread count and the RAM capacity estimate, and the batch size is also the number of worker threads. The estimate uses `WORKER_CPU_MODEL_MEMORY_GIB` as the model reserve and `WORKER_CPU_MEMORY_PER_JOB_GIB` for each additional request, using currently available system memory. It is recalculated when the worker starts. A large value means many simultaneous downloads and much more RAM.
* `WORKER_CPU_MODEL_MEMORY_GIB` and `WORKER_CPU_MEMORY_PER_JOB_GIB` control only that CPU RAM estimate. They are separate from `WORKER_MODEL_MEMORY_GIB` and `WORKER_MEMORY_PER_JOB_GIB`, which describe GPU VRAM. For example, with 14 GiB available RAM, a 2 GiB model reserve, and 2 GiB per job, the RAM estimate is 6 jobs; the CPU thread limit may lower it further.
* The worker log shows the result at startup, for example `GPU 0: 4 job(s), batch 4, 5.84 GiB free`. `python -m app.config check` shows `auto` because the capacity is only known on the worker.
* Values below the capacity leave capacity unused: jobs that arrive while a batch runs wait until the whole batch finishes.
* Larger batches give more total throughput, with diminishing returns. In the table above, going from 4 to 6 jobs saves only about 5% time per job. Each job also progresses more slowly, because every step processes one segment for every job in the batch.
* A batch starts early only when it is full, so with a large value each batch waits the full `WORKER_BATCH_TIMEOUT`.
* `WORKER_BATCH_SIZE=6` gives the behavior of earlier versions.

**Running out of GPU memory.** Free VRAM is measured once at startup, so another program can take memory later, which is common under WSL. When a batch runs out of GPU memory, the worker frees memory, splits the unfinished jobs in half, and retries each half from the start. Progress shown to users does not go backwards during a retry. The GPU's batch size is then lowered to the size that was retried, and the worker logs `cuda:0 ran out of memory; batch size lowered from 4 to 2`. A single job that still does not fit fails with `The GPU ran out of memory for this transcription`. Restart the worker to measure the capacity again.

**`WORKER_BATCH_TIMEOUT`** (default `2s`)

* Longer: more jobs can join a batch when requests arrive close together, which helps throughput, but every batch starts later.
* Shorter: lower latency for single jobs, but jobs arriving a moment later wait for the next batch.

**`WORKER_MAX_CONCURRENCY`** (default `auto`)

* Set a number to leave GPU memory for other programs or to limit load on a shared machine. It can only lower the calculated capacity, never raise it.

For billing configuration, uncomment the billing section of [.env.example](../.env.example) and follow the [Google Play billing guide](google-play-billing.md).

To add minutes to a user by hand, take them away, or delete an account, use the admin commands, for example:

```shell
python scripts/admin.py user user@example.com credit 30 --reason "refund for a failed job"
```

See [Administration](administration.md#-users).


## 💾 Billing database backups

The API creates a PostgreSQL custom-format dump in `/billing/backups` every `BILLING_BACKUP_INTERVAL` (default `15m`), starting when the API starts. On the host these files are in `./billing-data/backups`. Each dump is written under a temporary name, checked with `pg_restore --list`, and only then atomically renamed, so every `billing-<unix-time>.dump` file is complete. A backup that fails the check is deleted and the error logged.

To back up now, for example before an update, and to see what exists:

```shell
docker compose exec api python -m app.backups now
docker compose exec api python -m app.backups list
```

### Which backups are kept

After each backup, the API thins out older ones, so recent history is dense and older history sparse. With the defaults:

| Age | Kept | Setting |
| --- | --- | --- |
| up to 1 day | every backup (96 at `15m`) | `BILLING_BACKUP_KEEP_ALL=1d` |
| up to 2 days | the first of each hour | `BILLING_BACKUP_KEEP_HOURLY=2d` |
| up to 14 days | the first of each day | `BILLING_BACKUP_KEEP_DAILY=14d` |
| up to 8 weeks | the first of each week (Monday) | `BILLING_BACKUP_KEEP_WEEKLY=8w` |
| older | none; with a monthly step, the first of each month | `BILLING_BACKUP_KEEP_MONTHLY=0s` |

That is about 140 files. Hours, days, weeks, and months are in UTC. Each step keeps the *first* backup of its period, which is also the one the next step keeps, so thinning never removes a backup a longer step still needs. `0s` turns a step off. The newest backup is always kept, so a server that was off for months still has its last one. The age is read from the timestamp in the file name; other files in the directory, such as manually renamed copies, are never deleted.

`BILLING_BACKUP_RETENTION`, unset by default, is an upper limit: older backups are deleted whatever the steps keep. Data from a deleted account stays in backups until they are deleted, so the longest step, or this limit, is also the longest time deleted data is kept: 8 weeks with the defaults. Job diagnostics records are in the same database, so a record can survive in backups for up to `BILLING_JOB_DIAGNOSTICS_RETENTION` plus that. For example, for at most 30 days of history:

```env
BILLING_BACKUP_RETENTION=30d
```

### Off-site copies

The backups above are on the same disk as the live database. They protect against a bad migration, a wrong manual edit, or file corruption, but not against losing the disk or the machine. For that, the API can copy each backup to S3-compatible storage elsewhere. It is optional, and off until `OFFSITE_BACKUP_BUCKET` is set.

Copies are encrypted on this machine with [age](https://age-encryption.org) before upload, to a public key you give the server. The matching private key never needs to be on the server, so the storage provider, or anyone with a leaked storage key, sees only encrypted files. **Without the private key the copies cannot be restored**: store it somewhere that survives losing this machine, such as a password manager.

The off-site history follows the same steps as the local one, decided from the copies that exist there. After a disk is replaced, the old copies stay and new backups are added to them.

1. List what the bucket must provide:

   ```shell
   docker compose run --rm api python -m app.config requirements
   ```

   Use a bucket and keys of their own, not the outsourcing ones: the outsourcing keys are used for every outsourced job, and must not be able to delete the backups.

2. Create a key pair:

   ```shell
   docker compose run --rm --no-deps api python -m app.backups keygen
   ```

   It prints an `OFFSITE_BACKUP_RECIPIENT=age1...` line for `.env`, and the private key, `AGE-SECRET-KEY-...`. Save the private key in your password manager now. Do not put it in `.env` or `secrets`. A key pair from `age-keygen` works too.

3. Save the storage keys as files in `secrets`:

   **Windows (PowerShell)**

   ```powershell
   Set-Content -Path secrets\offsite_backup_access_key -Value '<access key ID>' -NoNewline
   Set-Content -Path secrets\offsite_backup_secret_key -Value '<secret access key>' -NoNewline
   ```

   **macOS and Linux**

   ```bash
   printf '%s' '<access key ID>' > secrets/offsite_backup_access_key
   printf '%s' '<secret access key>' > secrets/offsite_backup_secret_key
   ```

4. Add to `.env`:

   ```env
   OFFSITE_BACKUP_ENDPOINT=https://<storage endpoint>
   OFFSITE_BACKUP_BUCKET=<bucket>
   OFFSITE_BACKUP_RECIPIENT=age1...
   ```

5. Check, apply, and make the first copy. The existing local backups are uploaded too:

   ```shell
   docker compose run --rm api python -m app.config check --online
   docker compose up -d api
   docker compose exec api python -m app.backups now
   ```

`python -m app.admin status` shows when the last copy succeeded, or why it failed. A failed copy never stops the local backups, and the next backup retries it.

### Restoring a backup

To replace the live billing database with a backup, run on any system:

```shell
python scripts/server.py restore
```

By default it takes an off-site backup, and asks for the private key without showing it, so the key is not stored anywhere and does not appear in the shell history. Then it:

1. Downloads, decrypts, and checks the backup with `pg_restore --list`, into `./billing-data/restore`. If this fails, nothing has changed.
2. Asks before going on, then stops the API and the workers. Running jobs are interrupted.
3. Takes a safety backup of the current database into `./billing-data/backups`. If this fails, nothing is restored.
4. Restores with `pg_restore --clean --if-exists --no-owner --single-transaction --exit-on-error`, so a failed restore leaves the database as it was.
5. Starts the stack again and waits until `/ready` answers.

Without an argument it restores the newest backup. Give a name, or a number counted from the newest, as `python -m app.backups list` shows them: `restore 2` is the one before the newest. The safety backup is also kept, and copied off-site when off-site copies are on, so after a restore it is number 1 in both lists.

| Option | Backups used |
| --- | --- |
| none, or `--from remote` | Off-site |
| `--from local` | `./billing-data/backups`; no key is needed |
| `--fallback` | Also the other place, when the first has no such backup, or it cannot be fetched, for example because the storage cannot be reached or no key was given |

With `--fallback`, `latest` or a number picks from the other place's own list, so `restore 2 --fallback` may restore a different point in time than the off-site number 2 would have. A name is the same backup in both places.

For automation, pipe the key in and add `--yes`, which skips the question:

**Windows (PowerShell)**

```powershell
Get-Content key.txt | python scripts/server.py restore 2 --yes
Remove-Item key.txt
```

**macOS and Linux**

```bash
python scripts/server.py restore 2 --yes < key.txt
rm key.txt
```

On a new machine, copy `.env` and the `secrets/offsite_backup_*` files there first. Without this server's code, any S3 client and `age --decrypt -i key.txt` download and decrypt a backup too.

To only fetch a backup without restoring it, pipe the key into `docker compose run --rm --no-deps -T api python -m app.backups fetch latest`.

The live PostgreSQL data is where `POSTGRES_DATA_LOCATION` says, mounted
inside the PostgreSQL container at `/var/lib/postgresql/data`. Do not edit,
prune, or copy its files while PostgreSQL is running. PostgreSQL WAL and crash
recovery protect committed transactions; these files are the live database,
not a portable backup.

The host-mounted `billing-data\backups` directory contains verified PostgreSQL
custom-format files named `billing-<timestamp>.dump`. Older `.sqlite3` files
are legacy backups from before the migration and are not the live database.
Use `docker compose exec api python -m app.backups list` to see both local and
off-site backups.

Every dump is written to a temporary file, validated with `pg_restore --list`,
and atomically renamed only after validation. To look into a local backup
without touching the live database, restore it into a temporary database:

**Windows (PowerShell)**

```powershell
docker compose stop api worker remote-worker cleanup
docker compose exec -T postgres createdb -U billing billing_restore
docker cp .\billing-data\backups\<backup-file>.dump $(docker compose ps -q postgres):/tmp/billing-restore.dump
docker compose exec -T postgres pg_restore -U billing -d billing_restore --no-owner --exit-on-error /tmp/billing-restore.dump
docker compose exec -T postgres rm -f /tmp/billing-restore.dump
docker compose exec -T postgres psql -U billing -d billing_restore -c "SELECT count(*) FROM users"
```

Remove it afterwards with `docker compose exec -T postgres dropdb -U billing
billing_restore`, and start the services again with `python scripts/server.py
start`. Never overwrite the live database files with filesystem commands.

### Where the database is kept

`POSTGRES_DATA_LOCATION` in `.env` chooses where PostgreSQL keeps the live
billing database:

| Value | Where | Notes |
| --- | --- | --- |
| `./billing-data/postgres` (default) | A folder next to `docker-compose.yml` | Easy to see and to copy with the machine's own backup tools while the stack is stopped. |
| `postgres_data` | A Docker volume | Recommended with Docker Desktop on Windows and macOS. |

On Linux both are plain directories on the same disk: they are equally fast
and equally safe. Docker Desktop on Windows and macOS runs the containers in a
virtual machine and shares host folders into it. Every write to a folder then
crosses that boundary, which makes small, synced writes several times slower,
and Docker does not document that a sync to disk is passed through to the
host's disk. PostgreSQL relies on that sync to keep committed transactions
after a power cut. A Docker volume is stored inside the virtual machine's own
disk, where syncs are reliable. Billing writes are few, so the speed rarely
matters; the reliability does.

Another path on the host also works, and so does any volume declared in
`docker-compose.yml`.

### Moving the billing database

Changing `POSTGRES_DATA_LOCATION` does not move the data: PostgreSQL starts
with an empty database in the new place. The API notices a billing database
created after the newest backup and refuses to start, so balances are never
served from an empty database by mistake. To move the database, back it up,
switch, and restore the backup into the new place:

**Windows (PowerShell)**

```powershell
docker compose stop api worker remote-worker cleanup
docker compose run --rm --no-deps api python -m app.backups now
$backup = Get-ChildItem .\billing-data\backups\billing-*.dump | Sort-Object Name | Select-Object -Last 1
docker compose stop postgres
# Now set POSTGRES_DATA_LOCATION in .env.
docker compose up -d --wait postgres
docker cp $backup.FullName "$(docker compose ps -q postgres):/tmp/billing.dump"
docker compose exec -T postgres pg_restore -U billing -d billing --no-owner --exit-on-error /tmp/billing.dump
docker compose exec -T postgres rm -f /tmp/billing.dump
docker compose up -d
```

**macOS and Linux**

```bash
docker compose stop api worker remote-worker cleanup
docker compose run --rm --no-deps api python -m app.backups now
backup=$(ls billing-data/backups/billing-*.dump | sort | tail -n 1)
docker compose stop postgres
# Now set POSTGRES_DATA_LOCATION in .env.
docker compose up -d --wait postgres
docker cp "$backup" "$(docker compose ps -q postgres):/tmp/billing.dump"
docker compose exec -T postgres pg_restore -U billing -d billing --no-owner --exit-on-error /tmp/billing.dump
docker compose exec -T postgres rm -f /tmp/billing.dump
docker compose up -d
```

Then check balances, for example with `docker compose exec api python -m
app.admin status`. The old database is left where it was. Remove it once the
new one works, so that switching back later cannot start an outdated copy:
delete the `billing-data/postgres` folder, or the volume with `docker volume
rm` (`docker volume ls` shows its full name).

### Opening a database or backup in a viewer

Use a local PostgreSQL client such as `psql`, DBeaver, DataGrip, or TablePlus.
Billing data contains user emails, Google account IDs, and purchase tokens, so
do not upload it to online tools. Inspect a dump by restoring it into a
temporary database; never point a viewer at the live PostgreSQL volume or grant
the viewer write access to production.
