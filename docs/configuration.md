# ⚙️ Configuration and environment

Settings are read from `.env`. Start from [.env.example](../.env.example). Docker Compose overrides the Redis URL and shared data directory for its services.

| Variable                           | Default                    | Description                                                                |
| ---------------------------------- | -------------------------- | -------------------------------------------------------------------------- |
| `REDIS_URL`                        | `redis://localhost:6379/0` | Redis connection URL. Compose uses `redis://redis:6379/0`.                 |
| `DATA_DIR`                         | `/data`                    | Shared directory for job output files.                                     |
| `BILLING_DATABASE_PATH`            | `/billing/billing.sqlite3` | SQLite database for balances, purchases, and reservations.                 |
| `WORKER_THREADS`                   | `auto`                     | CPU thread limit; auto uses physical CPU cores minus two, capped at capacity. |
| `WORKER_MODEL_MEMORY_GIB`          | `3.0`                      | VRAM reserved for the transcription model on each GPU.                      |
| `WORKER_MEMORY_PER_JOB_GIB`        | `0.65`                     | Estimated VRAM budget for each concurrent GPU transcription job.            |
| `WORKER_BATCH_SIZE`                | `6`                        | Maximum requests combined into one model batch; also CPU worker thread count. |
| `WORKER_BATCH_TIMEOUT_SECONDS`     | `2`                        | Maximum CPU or GPU batch wait before running a partial batch.               |
| `WORKER_MAX_CONCURRENCY`           | `auto`                     | Optional cap for automatically calculated GPU job concurrency.               |
| `CLEANUP_INTERVAL_SECONDS`         | `600`                      | Cleanup scan interval.                                                     |
| `SOURCE_URL_ALLOWED_HOSTS`         | see below                  | JSON list of hosts accepted as transcription sources. Subdomains match too.   |
| `BILLING_PROVIDER`                 | `none`                     | `none` or `google_play`.                                                   |
| `BILLING_PRODUCTS`                 | `{}`                       | JSON mapping of Play product IDs to transcription minutes.                 |
| `FREE_MINUTES_PERIOD`              | unset                      | Free-credit interval using `s`, `m`, `h`, `d`, or `w`.                     |
| `FREE_MINUTES`                     | unset                      | Free minutes granted per interval; omit to disable.                        |
| `MAX_VIDEO_LENGTH_MINUTES`         | unset                      | Maximum accepted video length; unset means no limit.                       |
| `GOOGLE_OAUTH_CLIENT_IDS`          | `[]`                       | Public OAuth client IDs required for billing.                              |
| `GOOGLE_PLAY_PACKAGE_NAME`         | unset                      | Android package name used for Play verification.                           |
| `GOOGLE_PLAY_SERVICE_ACCOUNT_FILE` | unset                      | Service-account JSON filename in the mounted `secrets` directory.          |

Default for `SOURCE_URL_ALLOWED_HOSTS`:

```env
SOURCE_URL_ALLOWED_HOSTS=["youtube.com","youtu.be","youtube-nocookie.com","vimeo.com","soundcloud.com","bandcamp.com","dailymotion.com","instagram.com","instagr.am","facebook.com","fb.watch","tiktok.com","x.com","twitter.com","reddit.com","redd.it","twitch.tv"]
```

Write it on a single line. Subdomains match too, so `youtube.com` also covers `music.youtube.com`.

The API, worker, and cleanup service must use the same `DATA_DIR`. Billing is stored separately in the host directory `./billing-data`, mounted at `/billing` only in the API and worker. The cleanup service cannot access the billing database. Compose mounts `./secrets` read-only at `/run/secrets` for the API and worker.

`WORKER_BATCH_SIZE` and `WORKER_MEMORY_PER_JOB_GIB` control different limits. The batch size controls how many queued requests are combined for one model inference call. The per-job VRAM setting estimates how many GPU jobs fit alongside the loaded model. A larger batch size does not increase the calculated GPU capacity.

For billing configuration, use [.env.google-play.example](../.env.google-play.example) and follow the [Google Play billing guide](google-play-billing.md).

To manually add transcription minutes to an existing billed user, run the command in the API container:

```powershell
docker compose exec api python -m app.admin_credit --user user@example.com --minutes 30
```

The email must belong to a user who has authenticated with the API at least once.


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