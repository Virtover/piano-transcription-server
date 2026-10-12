# 🛠️ Local setup

## 📋 Requirements

* Docker Desktop with Docker Compose and Linux containers enabled.
* NVIDIA drivers and NVIDIA Container Toolkit for GPU transcription.
* A public video URL containing an audio or piano performance.

The worker image includes `ffmpeg`, Transkun, and the Python dependencies. Redis and shared job storage are provided by Compose.

## 🚀 Run the stack

```powershell
Copy-Item .env.example .env
docker compose up --build
```

The API is available at `http://localhost:8000` and its interactive OpenAPI documentation is at `http://localhost:8000/docs`.

Check the service state from PowerShell:

```powershell
Invoke-RestMethod http://localhost:8000/health
Invoke-RestMethod http://localhost:8000/ready
```

Do not commit `.env` or any machine-specific values.

## 🎮 GPU and CPU execution

Verify GPU access before starting the stack:

```powershell
docker run --rm --gpus all nvidia/cuda:12.6.0-cudnn-runtime-ubuntu22.04 nvidia-smi
```

The worker automatically uses CUDA when PyTorch detects a usable NVIDIA GPU. Otherwise it falls back to CPU. CPU execution is significantly slower; on machines without GPU support, remove `gpus: all` from the worker service in `docker-compose.yml`.

By default, one worker process shares one model and batches requests. GPU capacity is estimated from currently free VRAM using `WORKER_MODEL_MEMORY_GIB` (`3.0` GiB reserved for the model) and `WORKER_MEMORY_PER_JOB_GIB` (`0.65` GiB estimated for each job). See [Configuration](configuration.md) for tuning details.

## 📈 Worker scaling

Configure the shared worker with explicit CPU compute threads and request batching:

```env
WORKER_THREADS=4
WORKER_BATCH_SIZE=6
WORKER_BATCH_TIMEOUT_SECONDS=2
```

Increase the batch size only after checking RAM or VRAM usage. The API accepts jobs immediately and Redis queues work beyond available worker capacity. `WORKER_BATCH_SIZE` controls model batching; it does not replace the per-job VRAM estimate used for GPU capacity.


## 💾 Billing database backups

The API creates a SQLite backup in `/billing/backups` every hour. On the host these files are in `./billing-data/backups`. They are made with SQLite's online backup, so they are consistent while the API is running.

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

To inspect a backup in a viewer such as DB Browser for SQLite, work on a copy. Some viewers, especially online ones, report that WAL-mode files are not databases. If that happens, run `PRAGMA journal_mode=DELETE` on the copy (never on the live file). Do not upload billing data to online tools.