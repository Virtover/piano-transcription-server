# Agent guide

Piano Transcription Server: a FastAPI API, Dramatiq workers, and Redis, run with Docker Compose. It transcribes piano audio from a video URL or an upload to MIDI with Transkun, on a local GPU or, when that is busy, on RunPod Serverless. Billing (Google Play) and sign-in (Google OAuth) are optional. Human documentation is in `docs/`; start from `README.md`.

## Commands

```powershell
.\.venv\Scripts\python -m pytest -q                              # all tests; no Docker, GPU, or credentials needed (.venv/bin/python on macOS/Linux)
docker compose run --rm api python -m app.config check           # validate server.env, .env, and secrets, offline
docker compose run --rm api python -m app.config check --online  # also test storage, RunPod, Google Play for real
docker compose run --rm api python -m app.config requirements    # what external services must provide; --json for agents
docker compose run --rm api python -m app.config features --json # the optional features: on or off, what each needs, what is missing
python scripts/setup.py status                                  # the same for people; python scripts/setup.py <feature> sets one up
docker compose up -d --build                                     # the stack; OUTSOURCE_PROVIDER in server.env switches RunPod on
python scripts/server.py restart 5m                              # announced restart; also: stop 10m, start (also first start), status, cancel
python scripts/server.py restore 2                               # billing database from a backup: latest, a name, or Nth newest; --from local, --fallback
python scripts/admin.py status                                  # live picture; also jobs, user, users find, billing, limits, audit
python scripts/kube.py local up                                 # Kubernetes: a test cluster on this PC; up, update, status, down for a real one
python scripts/kube.py restart 5m                               # server.py's and admin.py's commands on a cluster: restart, stop, start, cancel, admin
```

`check --online` is the way to confirm a setup step worked. Code changes reach the containers only after a rebuild, for example `docker compose build api`.

## Layout

| Path | What |
| --- | --- |
| `app/config.py` | All settings (typed, grouped by prefix), validation, and the `check`, `features`, and `requirements` commands |
| `app/features.py` | The optional features in one table: what turns each on, what it needs and excludes, its settings and secrets; validation reads it |
| `app/requirements.py` | What external services must provide, derived from the configuration |
| `app/online_check.py` | The `check --online` service checks |
| `app/api/routes/` | HTTP endpoints |
| `app/worker/tasks.py` | Job lifecycle, billing settlement, local and remote actors |
| `app/outsource/` | Routing between local and RunPod, storage, the RunPod client |
| `app/transcription/` | Audio, batching, and the model |
| `app/billing.py` | Billing database (PostgreSQL; SQLite only for legacy installs), also job diagnostics |
| `app/backups.py` | `python -m app.backups`: billing backups, which are kept, and the encrypted off-site copies |
| `app/error_report.py` | Optional error reports of the API and workers to `ERROR_REPORT_URL`, with secrets redacted |
| `redis/` | The Redis users and their rules (`users.acl`), and the script that gives them to Redis at start |
| `runpod/handler.py` | The RunPod Serverless handler, built with `Dockerfile.runpod` |
| `app/admin_web/` | The optional admin web page behind Cloudflare Access: token checks, read-only routes, lowering its own access |
| `app/admin/`, `app/controls.py`, `scripts/admin.py` | `python -m app.admin`: status, jobs, users, the billing report, and temporary limits the API reads from Redis; the host script runs it in the API container |
| `deploy/kubernetes/` | The Kubernetes deployment: `base/`, the real cluster's overlay, and `local/`, a test cluster on this PC; `tests/test_kubernetes.py` keeps it in step with `docker-compose.yml` |
| `scripts/kube.py` | Kubernetes from the host: up, update, status, down, for a real cluster and, with `local`, the test cluster |
| `scripts/setup.py` | Guided setup of the optional features on the host, from `features --json` and `requirements --json` |
| `app/maintenance.py`, `scripts/server.py` | Planned restarts and shutdowns: the notice clients see, and the host script that drives them |
| `scripts/stacks.py` | Where the server runs, for the host scripts: requests built on a few primitives, `ComposeStack` gives them with the docker CLI and `KubeStack` (in `scripts/kube.py`) with kubectl. A change to server.py or admin.py reaches both |

## Conventions

- **Secrets are files** in `secrets/` (mounted at `/run/secrets`), never env file values. **Choices are in two files:** `server.env`, the server's settings, the same on every deployment, and `.env`, only what Docker Compose itself reads (`COMPOSE_ONLY_VARIABLES` in `app/config.py`). Container paths are wiring, in `docker-compose.yml` and `deploy/kubernetes/base/wiring.yaml`. A check that applies to one deployment is gated on `DEPLOYMENT` (`uses_compose_profiles`, `tunnel_enabled`).
- **Every setting is documented** in `docs/configuration.md`; a test fails otherwise. Durations use units (`30s`, `2m`, `1d`).
- **An optional feature is a row of `FEATURES`** in `app/features.py`: its switch, needs, exclusions, settings, and secrets. Add a rule there, not in `validate_settings`, and give the feature a block in `.env.example` and a row in "Optional features" in `docs/configuration.md`; tests keep the three in step.
- **Docs and skills describe only this project.** What a provider must offer lives in `app/requirements.py`, stated as an intent with its reason ("signed links must work from any IP address"), with values calculated from settings and code. Never add provider menu paths or setting names to docs or skills: they go stale. Where a provider's API can read a setting, verify it in `app/online_check.py`.
- **Docs serve every OS.** A command that runs the same everywhere (`docker`, `python`, `curl`) goes in one ```` ```shell ```` block. One that differs gets a `**Windows (PowerShell)**` block followed by a `**macOS and Linux**` block (bash, also valid in zsh). Give the user the commands for their OS.
- Match the surrounding code's style and comment density. Add or update tests with every change.

## Setup tasks

People can run `python scripts/setup.py`. For guided setup by an agent, use the skills in `.claude/skills/`: `setup-runpod` for outsourcing, `setup-google` for sign-in and billing. The Cloudflare Tunnel has no skill; follow `docs/cloudflare-tunnel.md`. These rules apply to any agent:

- Take the requirements from `requirements --json`, and look up the providers' current documentation for where each is set.
- Never ask for a secret in the chat or read secret files; give the user the command to write it, then check the file exists.
- Ask before outward-facing actions: pushing images, changing a cloud account, restarting a stack that serves users.

## Pitfalls

- `Set-Content -NoNewline <file> <value>` swaps its arguments in Windows PowerShell 5.1 and creates a file named after the secret. Always use `Set-Content -Path <file> -Value <value> -NoNewline`.
- Only `secrets/` is git-ignored for secrets: check `git status` for stray files after writing one.
- The services run as user ID 10001. The `prepare` Compose service (`app/prepare.py`) gives them `/data` and `billing-data` before every start; it must never touch the PostgreSQL cluster.
- Starting without `secrets/postgres_password` or a file in `secrets/redis` makes Docker create an empty folder in its place. `scripts/server.py start` and `restart` replace it with a generated password.
- The workers cover `secrets/cloudflare` (the tunnel token) with an empty folder, which needs the folder to exist: without it they fail with `make mountpoint "/run/secrets/cloudflare": read-only file system`. `scripts/server.py start` creates it.
- Every service drops all Linux capabilities and runs with `no-new-privileges`; `prepare` keeps only `CHOWN`, `DAC_OVERRIDE`, `FOWNER`, and `FSETID` (without `FSETID` the setgid bit on `billing-data` folders is silently lost). A new service gets the same, through `<<: *unprivileged`.
- Redis refuses clients without a password. Every service signs in as `app` with `REDIS_PASSWORD_FILE`; only the `redis` service sees all of `secrets/redis`, which an empty mount hides from services that mount `secrets/`. A new Redis client must pass `**settings.service.redis_options()`; Dramatiq gets a client, because it ignores a password beside a URL. fakeredis does not enforce Redis's user rules.
- The RunPod image installs PyTorch for a fixed CUDA version, `RUNPOD_CUDA_VERSION` in `app/requirements.py`; a test keeps `Dockerfile.runpod` in sync with it. pip's default PyTorch targets a newer CUDA and fails on hosts whose driver does not support it.
- Settings and secrets are read once, at startup: after writing a secret file, `docker compose restart <service>`. `docker compose up -d` restarts only services whose configuration changed.
- With `JOB_STORAGE=s3`, cleanup deletes the job files its own Redis does not know: a bucket must never be shared by two servers, such as a development copy and production.
- Every image build is about 8 GB. When Docker Desktop's disk fills up, builds fail with `error reading from server: EOF` and Docker stops starting. See "Freeing disk space" in `docs/setup.md`.
