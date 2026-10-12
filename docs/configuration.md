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

The API creates a SQLite backup in `/billing/backups` every hour. Before switching an existing deployment from the old shared volume, stop the stack and copy the database with SQLite's online backup command, then start the stack again:

```powershell
docker compose down
New-Item -ItemType Directory -Force billing-data | Out-Null
docker run --rm -v yt-piano-to-midi-server_data:/data -v "${PWD}/billing-data:/billing" python:3.12-slim python -c "import sqlite3; src=sqlite3.connect('/data/billing.sqlite3'); dst=sqlite3.connect('/billing/billing.sqlite3'); src.backup(dst); dst.close(); src.close()"
docker compose up -d
```

Use the actual Compose project volume name if it differs from `yt-piano-to-midi-server_data`. Do not copy a live SQLite file with a filesystem copy command.

`WORKER_BATCH_SIZE` and `WORKER_MEMORY_PER_JOB_GIB` control different limits. The batch size controls how many queued requests are combined for one model inference call. The per-job VRAM setting estimates how many GPU jobs fit alongside the loaded model. A larger batch size does not increase the calculated GPU capacity.

For billing configuration, use [.env.google-play.example](../.env.google-play.example) and follow the [Google Play billing guide](google-play-billing.md).

To manually add transcription minutes to an existing billed user, run the command in the API container:

```powershell
docker compose exec api python -m app.admin_credit --user user@example.com --minutes 30
```

The email must belong to a user who has authenticated with the API at least once.
