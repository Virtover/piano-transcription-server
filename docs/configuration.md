# ⚙️ Configuration and environment

Settings are read from `.env`. Start from [.env.example](../.env.example). Docker Compose overrides the Redis URL and shared data directory for its services.

| Variable                           | Default                    | Description                                                                |
| ---------------------------------- | -------------------------- | -------------------------------------------------------------------------- |
| `REDIS_URL`                        | `redis://localhost:6379/0` | Redis connection URL. Compose uses `redis://redis:6379/0`.                 |
| `DATA_DIR`                         | `/data`                    | Shared directory for job output files.                                     |
| `BILLING_DATABASE_PATH`            | `/data/billing.sqlite3`    | SQLite database for balances, purchases, and reservations.                 |
| `WORKER_THREADS`                   | `auto`                     | CPU thread limit; auto uses physical CPU cores minus two, capped at capacity. |
| `WORKER_MODEL_MEMORY_GIB`          | `3.0`                      | VRAM reserved for the transcription model on each GPU.                      |
| `WORKER_MEMORY_PER_JOB_GIB`        | `1.5`                      | VRAM reserved for each concurrent transcription job.                       |
| `WORKER_BATCH_SIZE`                | `6`                        | Maximum batch size and CPU request-thread count.                             |
| `WORKER_BATCH_TIMEOUT_SECONDS`     | `2`                        | Maximum CPU or GPU batch wait before running a partial batch.               |
| `WORKER_MAX_CONCURRENCY`           | `auto`                     | Optional cap for automatic concurrency.                                    |
| `CLEANUP_INTERVAL_SECONDS`         | `600`                      | Cleanup scan interval.                                                     |
| `BILLING_PROVIDER`                 | `none`                     | `none` or `google_play`.                                                   |
| `BILLING_PRODUCTS`                 | `{}`                       | JSON mapping of Play product IDs to transcription minutes.                 |
| `FREE_MINUTES_PERIOD`              | unset                      | Free-credit interval using `s`, `m`, `h`, `d`, or `w`.                     |
| `FREE_MINUTES`                     | unset                      | Free minutes granted per interval; omit to disable.                        |
| `MAX_VIDEO_LENGTH_MINUTES`         | unset                      | Maximum accepted video length; unset means no limit.                       |
| `GOOGLE_OAUTH_CLIENT_IDS`          | `[]`                       | Public OAuth client IDs required for billing.                              |
| `GOOGLE_PLAY_PACKAGE_NAME`         | unset                      | Android package name used for Play verification.                           |
| `GOOGLE_PLAY_SERVICE_ACCOUNT_FILE` | unset                      | Service-account JSON filename in the mounted `secrets` directory.          |

The API, worker, and cleanup service must use the same `DATA_DIR`. Compose mounts `./secrets` read-only at `/run/secrets` for the API and worker.

For billing configuration, use [.env.google-play.example](../.env.google-play.example) and follow the [Google Play billing guide](google-play-billing.md).
