# Agent guide

Piano Transcription Server: a FastAPI API, Dramatiq workers, and Redis, run with Docker Compose. It transcribes piano audio from a video URL or an upload to MIDI with Transkun, on a local GPU or, when that is busy, on RunPod Serverless. Billing (Google Play) and sign-in (Google OAuth) are optional. Human documentation is in `docs/`; start from `README.md`.

## Commands

```powershell
.\.venv\Scripts\python -m pytest -q                              # all tests; no Docker, GPU, or credentials needed (.venv/bin/python on macOS/Linux)
docker compose run --rm api python -m app.config check           # validate .env and secrets, offline
docker compose run --rm api python -m app.config check --online  # also test storage, RunPod, Google Play for real
docker compose run --rm api python -m app.config requirements    # what external services must provide; --json for agents
docker compose up -d --build                                     # the stack; OUTSOURCE_PROVIDER in .env switches RunPod on
python scripts/server.py restart 5m                              # announced restart; also: stop 10m, start (also first start), status, cancel
python scripts/server.py restore 2                               # billing database from a backup: latest, a name, or Nth newest; --from local, --fallback
docker compose exec api python -m app.admin status              # live picture; also jobs, user, billing, limits, audit
```

`check --online` is the way to confirm a setup step worked. Code changes reach the containers only after a rebuild, for example `docker compose build api`.

## Layout

| Path | What |
| --- | --- |
| `app/config.py` | All settings (typed, grouped by prefix), validation, and the `check` and `requirements` commands |
| `app/requirements.py` | What external services must provide, derived from the configuration |
| `app/online_check.py` | The `check --online` service checks |
| `app/api/routes/` | HTTP endpoints |
| `app/worker/tasks.py` | Job lifecycle, billing settlement, local and remote actors |
| `app/outsource/` | Routing between local and RunPod, storage, the RunPod client |
| `app/transcription/` | Audio, batching, and the model |
| `app/billing.py` | SQLite billing database, also job diagnostics |
| `app/backups.py` | `python -m app.backups`: billing backups, which are kept, and the encrypted off-site copies |
| `runpod/handler.py` | The RunPod Serverless handler, built with `Dockerfile.runpod` |
| `app/admin/`, `app/controls.py` | `python -m app.admin`: status, jobs, users, the billing report, and temporary limits the API reads from Redis |
| `app/maintenance.py`, `scripts/server.py` | Planned restarts and shutdowns: the notice clients see, and the host script that drives them |

## Conventions

- **Secrets are files** in `secrets/` (mounted at `/run/secrets`), never `.env` values. `.env` holds only choices; container paths are in `docker-compose.yml`.
- **Every setting is documented** in `docs/configuration.md`; a test fails otherwise. Durations use units (`30s`, `2m`, `1d`).
- **Docs and skills describe only this project.** What a provider must offer lives in `app/requirements.py`, stated as an intent with its reason ("signed links must work from any IP address"), with values calculated from settings and code. Never add provider menu paths or setting names to docs or skills: they go stale. Where a provider's API can read a setting, verify it in `app/online_check.py`.
- **Docs serve every OS.** A command that runs the same everywhere (`docker`, `python`, `curl`) goes in one ```` ```shell ```` block. One that differs gets a `**Windows (PowerShell)**` block followed by a `**macOS and Linux**` block (bash, also valid in zsh). Give the user the commands for their OS.
- Match the surrounding code's style and comment density. Add or update tests with every change.

## Setup tasks

For guided setup, use the skills in `.claude/skills/`: `setup-runpod` for outsourcing, `setup-google` for sign-in and billing. The Cloudflare Tunnel has no skill; follow `docs/cloudflare-tunnel.md`. These rules apply to any agent:

- Take the requirements from `requirements --json`, and look up the providers' current documentation for where each is set.
- Never ask for a secret in the chat or read secret files; give the user the command to write it, then check the file exists.
- Ask before outward-facing actions: pushing images, changing a cloud account, restarting a stack that serves users.

## Pitfalls

- `Set-Content -NoNewline <file> <value>` swaps its arguments in Windows PowerShell 5.1 and creates a file named after the secret. Always use `Set-Content -Path <file> -Value <value> -NoNewline`.
- Only `secrets/` is git-ignored for secrets: check `git status` for stray files after writing one.
- The services run as user ID 10001. The `prepare` Compose service (`app/prepare.py`) gives them `/data` and `billing-data` before every start; it must never touch the PostgreSQL cluster.
- Starting without `secrets/postgres_password` makes Docker create an empty folder in its place. `scripts/server.py start` replaces it with a generated password.
- The RunPod image installs PyTorch for a fixed CUDA version, `RUNPOD_CUDA_VERSION` in `app/requirements.py`; a test keeps `Dockerfile.runpod` in sync with it. pip's default PyTorch targets a newer CUDA and fails on hosts whose driver does not support it.
- Every image build is about 8 GB. When Docker Desktop's disk fills up, builds fail with `error reading from server: EOF` and Docker stops starting. See "Freeing disk space" in `docs/setup.md`.
