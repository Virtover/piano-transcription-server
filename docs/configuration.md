# ⚙️ Configuration and environment

## 🧭 How configuration works

* **Choices go in `.env`.** Start from [.env.example](../.env.example), which lists the values operators usually change. Every setting is described below.
* **Wiring goes in `docker-compose.yml`.** Container paths and service addresses (`REDIS_URL`, `DATA_DIR`, `BILLING_DATABASE_PATH`, `BILLING_BACKUP_DIR`) are fixed in the Compose `environment:` sections. The code defaults suit local development outside Docker.
* **Secrets are files in `./secrets`.** Compose mounts the folder read-only at `/run/secrets`. The file name is the setting name. Secrets are never read from `.env` or the environment, and never appear in logs or error messages. See [Secrets](#-secrets).
* **The process environment overrides `.env`.** Empty values count as unset.

All settings are validated when a service starts. An invalid value stops the service with a message that names the variable. A variable that starts with `WORKER_`, `BILLING_`, `OUTSOURCE_`, `RUNPOD_`, or `GOOGLE_PLAY_` but is not a known setting is reported as a warning with the closest known name, for example `Unknown setting WORKER_BATCHSIZE; did you mean WORKER_BATCH_SIZE?`.

### ✅ Checking the configuration

```powershell
docker compose run --rm api python -m app.config check
```

The command validates everything, prints the resolved configuration with secrets masked, and exits with code `1` on errors. Outside Docker, run `python -m app.config check --env-file .env --secrets-dir secrets`.

### ⏱️ Value formats

* **Durations** use a number and a unit: `s`, `m`, `h`, `d`, or `w`, for example `2s`, `10m`, `1.5h`, `30d`. A plain number is rejected, so `600` cannot be mistaken for minutes.
* **`auto` or a number:** `WORKER_THREADS`, `WORKER_BATCH_SIZE`, and `WORKER_MAX_CONCURRENCY` accept `auto` or a positive integer.
* **Lists and mappings** are JSON on one line, for example `GOOGLE_OAUTH_CLIENT_IDS=["id-1","id-2"]`.
* Comments go on their own lines in `.env`.

## 📋 Settings

### Limits

| Variable | Default | Description |
| --- | --- | --- |
| `MAX_VIDEO_LENGTH_MINUTES` | unset | Maximum video or uploaded file length; unset means no limit. |
| `MAX_UPLOAD_SIZE_MB` | `200` | Maximum size of an uploaded file. |
| `SOURCE_URL_ALLOWED_HOSTS` | see below | JSON list of hosts accepted as transcription sources. Subdomains match too. |
| `CLEANUP_INTERVAL` | `10m` | How often the cleanup service deletes expired job files. |

### Worker

| Variable | Default | Description |
| --- | --- | --- |
| `WORKER_THREADS` | `auto` | CPU threads for PyTorch and ffmpeg. `auto` uses physical cores minus two; a number is capped at the physical core count. |
| `WORKER_MODEL_MEMORY_GIB` | `1.0` | VRAM reserved on each GPU for the model and GPU libraries. See [GPU memory and batching](#-gpu-memory-and-batching). |
| `WORKER_MEMORY_PER_JOB_GIB` | `1.1` | VRAM budget for each concurrent GPU job. |
| `WORKER_BATCH_SIZE` | `6` | Maximum jobs combined into one model batch. In CPU mode, also the number of worker threads. `auto` means `6`. |
| `WORKER_BATCH_TIMEOUT` | `2s` | Maximum wait for more jobs before running a partial batch. |
| `WORKER_MAX_CONCURRENCY` | `auto` | Optional cap on concurrent GPU jobs. It can only lower the capacity calculated from VRAM. |

### Billing

| Variable | Default | Description |
| --- | --- | --- |
| `BILLING_PROVIDER` | `none` | `none` or `google_play`. |
| `BILLING_PRODUCTS` | `{}` | JSON mapping of Google Play product IDs to transcription minutes. |
| `FREE_MINUTES` | unset | Free minutes granted per period; omit to disable. |
| `FREE_MINUTES_PERIOD` | `30d` | How often free minutes are topped up. |
| `BILLING_BACKUP_INTERVAL` | `1h` | How often the billing database is backed up. |
| `BILLING_BACKUP_RETENTION` | `30d` | Age after which automatic backups are deleted. |
| `GOOGLE_OAUTH_CLIENT_IDS` | `[]` | Public OAuth client IDs whose Google ID tokens are accepted. Required for billing. |
| `GOOGLE_PLAY_PACKAGE_NAME` | unset | Android package name used for Google Play verification. Required for billing. |

`BILLING_PROVIDER=google_play` also requires the secret `google_play_service_account.json`. The API refuses to start if the package name, the OAuth client IDs, or the secret is missing. See the [Google Play billing guide](google-play-billing.md).

### Container wiring

Set by `docker-compose.yml`. Change them only when running the services outside Docker.

| Variable | Default | Compose value | Description |
| --- | --- | --- | --- |
| `REDIS_URL` | `redis://localhost:6379/0` | `redis://redis:6379/0` | Redis connection URL. |
| `DATA_DIR` | `/data` | `/data` | Shared directory for job files. |
| `BILLING_DATABASE_PATH` | `/billing/billing.sqlite3` | same | SQLite database for balances, purchases, and reservations. |
| `BILLING_BACKUP_DIR` | `/billing/backups` | same | Directory for billing database backups. |

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

Secrets are only read from these files: a variable with the same name in `.env` or the environment is ignored. Never commit the `secrets` folder.

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
3. Remove `REDIS_URL`, `DATA_DIR`, and `BILLING_DATABASE_PATH` from `.env`. Compose sets them now; values left in `.env` are overridden by Compose.
4. Remove any inline comments after values.
5. Run `docker compose run --rm api python -m app.config check`, then `docker compose up -d --build`.

`MAX_UPLOAD_SIZE_MB` limits disk use per upload. Uploads are written to the shared `DATA_DIR` and removed after conversion. Compressed files are small: an hour of MP3 at 192 kbit/s is about 85 MB. Uncompressed WAV is large: an hour of 16-bit 44.1 kHz stereo is about 635 MB. Raise the limit only if users need to upload long lossless recordings and the disk has room for several at once.

`WORKER_BATCH_SIZE` and `WORKER_MEMORY_PER_JOB_GIB` control different limits. The batch size controls how many queued requests are combined for one model inference call. The per-job VRAM setting estimates how many GPU jobs fit alongside the loaded model. A larger batch size does not increase the calculated GPU capacity. See [GPU memory and batching](#-gpu-memory-and-batching).

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

**`WORKER_BATCH_SIZE`** (default `6`)

* On GPU, the batch can never hold more jobs than the worker has threads, so values above the calculated capacity have no effect. Values below it leave capacity unused: jobs that arrive while a batch runs wait until the whole batch finishes.
* Larger batches give more total throughput, but with diminishing returns. In the table above, going from 4 to 6 jobs saves only about 5% time per job. Each job also progresses more slowly, because every step processes one segment for every job in the batch.
* A batch starts early only when it is full, so with a large value each batch waits the full `WORKER_BATCH_TIMEOUT`.
* On CPU, the batch size is also the number of worker threads, so a large value means many simultaneous downloads and much more RAM. Keep it small, around 2 to 6.

**`WORKER_BATCH_TIMEOUT`** (default `2s`)

* Longer: more jobs can join a batch when requests arrive close together, which helps throughput, but every batch starts later.
* Shorter: lower latency for single jobs, but jobs arriving a moment later wait for the next batch.

**`WORKER_MAX_CONCURRENCY`** (default `auto`)

* Set a number to leave GPU memory for other programs or to limit load on a shared machine. It can only lower the calculated capacity, never raise it.

For billing configuration, uncomment the billing section of [.env.example](../.env.example) and follow the [Google Play billing guide](google-play-billing.md).

To manually add transcription minutes to an existing billed user, run the command in the API container:

```powershell
docker compose exec api python -m app.admin_credit --user user@example.com --minutes 30
```

The email must belong to a user who has authenticated with the API at least once.

To delete a user's account and data, for example after a deletion request by email or through a web form, run:

```powershell
docker compose exec api python -m app.admin_delete --user user@example.com
```

The command asks for confirmation; add `--yes` to skip it. It deletes the same data as `DELETE /api/account`, including running jobs and remaining minutes, and cannot be undone. See **Account deletion** in the [API guide](api.md) for what is kept.


## 💾 Billing database backups

The API creates a SQLite backup in `/billing/backups` every `BILLING_BACKUP_INTERVAL` (default `1h`), starting when the API starts. On the host these files are in `./billing-data/backups`. They are made with SQLite's online backup, so they are consistent while the API is running.

After each backup, the API deletes automatic backups older than `BILLING_BACKUP_RETENTION` (default `30d`). The age is read from the timestamp in the file name, `billing-<unix-time>.sqlite3`. Other files in the directory, such as manually renamed copies, are never deleted. The API refuses to start if either value is invalid.

```env
BILLING_BACKUP_INTERVAL=6h
BILLING_BACKUP_RETENTION=14d
```

Data from a deleted account stays in older backups until they expire, so the retention period is also the longest time deleted data is kept. Copies you make elsewhere are not pruned; apply the same retention to them.

These backups are on the same disk as the live database. They protect against a bad migration, a wrong manual edit, or file corruption, but not against losing the machine or the disk. Copy `./billing-data/backups` to another location regularly, for example with `rclone` or `rsync` from a host cron job, and encrypt the copy first (for example with `age`). The files contain user emails, Google account IDs, and purchase tokens.

The database is in WAL mode, so `billing.sqlite3-wal` and `billing.sqlite3-shm` may appear next to it while the stack runs. They belong to the live database. Never copy `billing.sqlite3` alone with a filesystem command while the API is running.

To restore a backup, stop the stack so nothing writes to the database, replace the live file, and remove the leftover WAL files:

```powershell
docker compose down
Remove-Item billing-data\billing.sqlite3-wal, billing-data\billing.sqlite3-shm -ErrorAction SilentlyContinue
Copy-Item billing-data\backups\<backup-file>.sqlite3 billing-data\billing.sqlite3 -Force
docker compose up -d
```

Check a backup before relying on it:

```powershell
docker compose exec api python -c "import sqlite3; d=sqlite3.connect('file:/billing/backups/<backup-file>.sqlite3?mode=ro', uri=True); print(d.execute('PRAGMA integrity_check').fetchone())"
```

It should print `('ok',)`. Test a restore on a separate copy from time to time.

### Opening a database or backup in a viewer

Use a local desktop tool. Billing data contains user emails, Google account IDs, and purchase tokens, so do not upload it to online tools.

- [DB Browser for SQLite](https://sqlitebrowser.org): free and the simplest option. Use **File → Open Database Read Only**, then browse tables under **Browse Data** or run queries under **Execute SQL**.
- DBeaver, DataGrip, and TablePlus also open WAL databases.
- The **SQLite Viewer** extension for VS Code opens a file directly in the editor.

Rules when opening files:

- Prefer a backup file or a snapshot (below) over the live database. A viewer that writes or holds a lock can interfere with the running API.
- Open files read-only whenever you only want to look.
- If you open the live `billing.sqlite3`, keep `billing.sqlite3-wal` and `billing.sqlite3-shm` in the same folder. Recent writes are in the WAL file, so opening the main file alone shows outdated data.
- Work on a copy and delete it when you are done.

Some viewers, especially online and older ones, report that a WAL-mode file is "not a database". The file is valid. To make a copy that any viewer can open, without stopping the API:

```powershell
docker compose exec -T api python -c "
import os, sqlite3
src = sqlite3.connect(os.environ.get('BILLING_DATABASE_PATH', '/billing/billing.sqlite3'), timeout=30)
dst = sqlite3.connect('/tmp/view.sqlite3')
src.backup(dst)
print(dst.execute('PRAGMA journal_mode=DELETE').fetchone())
dst.close(); src.close()
"
docker compose cp api:/tmp/view.sqlite3 .\view.sqlite3
docker compose exec api rm /tmp/view.sqlite3
```

It should print `('delete',)`. Open `view.sqlite3` and delete it afterwards. The snapshot includes data that is still in the WAL file, and the live database stays in WAL mode.

To convert a copy of an existing backup file instead:

```powershell
Copy-Item billing-data\backups\<backup-file>.sqlite3 view.sqlite3
python -c "import sqlite3; d=sqlite3.connect('view.sqlite3'); print(d.execute('PRAGMA journal_mode=DELETE').fetchone()); d.close()"
```

Never run `PRAGMA journal_mode=DELETE` on the live database or on a backup you plan to keep as is. Do it on a copy.