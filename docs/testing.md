# 🧪 Testing

The test suite uses an in-memory Redis substitute and temporary SQLite databases. It does not require Docker Compose, a GPU, or Google Play credentials.

## 🐍 Test environment

Create a virtual environment and install the test dependencies:

**Windows (PowerShell)**

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.test.txt
```

**macOS and Linux**

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.test.txt
```

The commands below expect the environment to be active.

## ✅ Run tests

```shell
python -m pytest -q
```

Run one file or one test while working on a feature:

```shell
python -m pytest -q tests/test_account_api.py
python -m pytest -q -k cancellation
```

## ⚡ Benchmarking CPU versus GPU

The repository includes [scripts/benchmark_transcription.py](../scripts/benchmark_transcription.py).
It benchmarks the same `transcribe_piano` inference path used by the local worker and
prints JSON timings. Run it with a representative piano recording; a synthetic tone
only verifies that the command works.

Mount the recording directly from its existing location; do not copy it into the
repository or commit it. The first run loads Transkun and is reported separately as
`warmup_seconds`; compare the `seconds.median` values for steady-state inference.

**Windows (PowerShell)**

```powershell
$audio = "C:\path\to\representative-piano-recording.mp3"
$scriptsMount = "${PWD}\scripts`:/app/scripts:ro"
$audioMount = "${audio}`:/tmp/benchmark-input.mp3:ro"

docker compose run --rm --no-deps -v $scriptsMount -v $audioMount `
  worker python3 scripts/benchmark_transcription.py /tmp/benchmark-input.mp3 --device cpu --warmup --runs 3

docker compose run --rm --no-deps -v $scriptsMount -v $audioMount `
  worker python3 scripts/benchmark_transcription.py /tmp/benchmark-input.mp3 --device gpu --warmup --runs 3
```

**macOS and Linux**

```bash
audio="$HOME/path/to/representative-piano-recording.mp3"

docker compose run --rm --no-deps \
  -v "$PWD/scripts:/app/scripts:ro" \
  -v "$audio:/tmp/benchmark-input.mp3:ro" \
  worker python3 scripts/benchmark_transcription.py /tmp/benchmark-input.mp3 --device cpu --warmup --runs 3

docker compose run --rm --no-deps \
  -v "$PWD/scripts:/app/scripts:ro" \
  -v "$audio:/tmp/benchmark-input.mp3:ro" \
  worker python3 scripts/benchmark_transcription.py /tmp/benchmark-input.mp3 --device gpu --warmup --runs 3
```

The GPU command requires normal Compose `gpus: all` support. Results depend on GPU
model, CPU, PyTorch, audio duration, and worker settings, so keep the complete JSON
output with those details when comparing machines. The benchmark does not contact
video sites or submit an API job.

### Reference result

On 2026-10-27, the supplied Chopin recording
`gregorquendel-chopin-fantaisie-impromptu-op-66-227470.mp3` was benchmarked in
fresh worker containers with `--warmup --runs 3` on an
Intel Core i7-10700K CPU and an NVIDIA GeForce RTX 3060 Ti GPU:

| Mode | Warm-up/model load | Timed range | Median |
| --- | ---: | ---: | ---: |
| CPU | 128.53 s | 116.46–120.56 s | 117.54 s |
| GPU (`cuda:0`) | 21.92 s | 17.40–17.73 s | 17.44 s |

On that machine and recording, GPU steady-state inference was approximately
6.7x faster than CPU. These are reference measurements, not guaranteed
performance: the result changes with the CPU, GPU, PyTorch version, audio
duration, and worker settings.

## 🤖 Continuous integration

GitHub Actions runs the tests on every push to `main` and every pull request ([.github/workflows/tests.yml](../.github/workflows/tests.yml)), on Linux with Python 3.12 and ffmpeg, from a clean checkout: no `.env`, no secrets. The model tests, which need the worker requirements and PyTorch, are skipped there. Run them locally after installing `requirements.worker.txt`.

A test that needs an optional dependency must skip without it: import the dependency with `pytest.importorskip` before anything that uses it, not with a plain `import` at the top.

CI runs as an ordinary user, not root, so code must not write to the container paths (`/billing`, `/data`) when it is imported. Create files on first use instead, as `Billing` does. To reproduce CI locally, run the tests as a non-root user in a `python:3.12-slim` container.

## 🗂️ What is covered

| File | Covers |
| --- | --- |
| `test_transcriptions_api.py` | Job creation, status, cancellation, MIDI download, path checks, job ownership, reservations, and startup and worker errors. |
| `test_billing_api.py` | Server info, cost, balance, Google Play purchase verification, and free-minute grants. |
| `test_account_api.py` | Account deletion: removed data, kept anonymous purchases, running jobs, free-minute abuse protection, and deletion with `app.admin user ... delete`. |
| `test_billing.py` | Cost formulas, reservations and settlement, reserving a failed job again, free minutes, changes by hand, and the minute ledger. |
| `test_admin.py` | `python -m app.admin`: status, listing and showing jobs, cancelling and queueing them again, user accounts, credits and deletion, the billing report, limits, the audit log, secret-free output, `--json`, and the data functions without the command line; the safety rules: no questions without a terminal, required reasons without email addresses, typed confirmation for deletion, 8-character job IDs for changes, ambiguous users, read-only reports, changes saved with their audit entry, dry runs, and undo hints. |
| `test_controls.py` | Temporary controls in the API: pausing submissions, turning links off, blocking a user, the per-user limit, expiry, and running jobs left alone. |
| `test_backups.py` | Backup creation, retention pruning, interval and retention settings, and duration parsing. |
| `test_online_check.py` | `check --online`: each service check's success, failure hints, and secret-free output, and RunPod endpoint settings against the requirements, with fake services. |
| `test_maintenance.py` | Planned restarts and shutdowns: the notice, its expiry, messages, and return time, refusing jobs that would not finish, the `maintenance` field, the `X-Server-Maintenance` headers, `Retry-After`, and clearing past notices at startup. |
| `test_admin_script.py` | `scripts/admin.py`: the command it runs in the API container, no terminal for the container without one, and the user name the audit log gets. |
| `test_admin_web.py` | The admin web page: Cloudflare Access tokens (signature, algorithm, issuer, audience, lifetime, allowed addresses, key rotation), every route refusing strangers, the security headers, read-only routes, recorded account views, lowering its own access and every way to misuse it, restoring it from the command line, its settings, its Compose service, and its online check. Its credentials were also checked against a real Redis and PostgreSQL. |
| `test_redis_users.py` | Redis users: the rules in `redis/users.acl`, which services see which password, that every Redis client signs in, and the `check --online` probe. The rules themselves were checked against a real Redis, because fakeredis does not enforce them. |
| `test_server_script.py` | `scripts/server.py` with a fake stack and clock: announce, build during the countdown, cancel on build failure or Ctrl+C, drain within the grace period, restart, stop every profile, start, and clear. |
| `test_cloudflare_tunnel.py` | The optional Cloudflare Tunnel: `COMPOSE_PROFILES`, the token check, warnings for an open server, the `check --online` results and hints, the requirements, and the `cloudflared` service in `docker-compose.yml`. |
| `test_requirements.py` | `requirements`: values calculated from the configuration, the RunPod CUDA version matching `Dockerfile.runpod`, and the text and JSON output. |
| `test_job_diagnostics.py` | Job diagnostics records at each job state, survival after the Redis job expires, account deletion, pruning, and inclusion in backups. |
| `test_worker_tasks.py` | Worker job lifecycle, billing reconciliation, restart handling, and file cleanup. |
| `test_auth.py` | Google token checks: audience, expiry, verified email, userinfo fallback, caching, and outages. |
| `test_url_safety.py` | Source URL validation: allowed hosts, schemes, ports, credentials, and private addresses. |
| `test_server_api.py` | Health and readiness, server info offers, `yt-dlp` duration handling, reused video checks, and the video information saved for the worker. |
| `test_upload_api.py` | File uploads: storage, size and length limits, title handling, billing, cleanup on errors, and cost by duration. |
| `test_media.py` | `ffprobe` checks: accepted and rejected formats, files without audio, and playlist files. Real-file tests need `ffmpeg`. |
| `test_pipeline_upload.py` | Converting uploaded audio and video to the WAV format the model needs. Needs the worker image. |
| `test_config.py` | Settings parsing, durations, deprecated names, unknown-name warnings, secrets, startup validation, the `check` command, and drift guards against `.env.example` and these docs. |
| `test_launcher.py` | Worker resources resolved at startup: threads, CPU threads, and the values passed to Dramatiq. |
| `test_batching.py` | Batch size rules (`auto` per GPU, clamping, CPU default) and splitting a batch in half on GPU out-of-memory. |
| `test_gpu_batcher.py` | The real batcher and batch loop with a fake model that runs out of memory. Needs the worker image. |
| `test_outsource_routing.py` | Routing: wait estimate, threshold, remote concurrency cap, missing heartbeat, provider `none`, and the API's backend choice. |
| `test_remote_jobs.py` | Remote jobs with a fake RunPod and storage: success, progress mapping, cancellation, fallback to local, timeouts, failures, single billing, and storage cleanup on every path. |
| `test_outsource_capacity.py` | `OUTSOURCE_MAX_CONCURRENCY`: the default, fixed values, and `auto` from RunPod's max workers and the reported requests per worker. |
| `test_outsource_clients.py` | The RunPod client, S3 storage, and the handler's input checks and download limits. |
| `test_runpod_handler.py` | The RunPod handler with the real model: concurrent requests share one batch, and bad requests fail alone. Needs the RunPod image (see below). |
| `test_video_info.py` | One request to the video site per job: the saved video information, its age limit, the retry when its links fail, and real `yt-dlp` downloading from saved information. |
| `test_audio_download.py` | The yt-dlp and ffmpeg commands: no generic extractor, conversion only by the restricted ffmpeg, and audio cut at the charged duration. |
| `test_errors.py` | Job errors as users see them: no command lines or paths, and yt-dlp's reason when it gives one. |
| `test_job_limits.py` | `MAX_ACTIVE_JOBS` and `MAX_ACTIVE_JOBS_PER_USER`: places taken during uploads and video checks, given back when jobs end or fail. |
| `test_voided_purchases.py` | Refunds and chargebacks: minutes taken back once, negative balances, free minutes, and reading every page from Google Play. |
| `test_deployment.py` | The images: pinned requirements and deno, services not running as root, and the startup check for root-owned volumes. |
| `test_device_capacity.py` | GPU capacity calculation, including the `nvidia-smi` free memory check used under WSL. |
| `test_device_selection.py` | GPU and CPU device selection for transcription jobs. Needs the worker dependencies, so it is skipped in the API test environment. |

Some tests need PyTorch, Transkun, or `ffmpeg`, which are only in the worker image. They are skipped in the API test environment. Run them in the worker image:

**Windows (PowerShell)**

```powershell
docker compose run --rm --no-deps -v "${PWD}\tests:/app/tests:ro" worker sh -c "pip install -q --user pytest && python3 -m pytest -q --noconftest -p no:cacheprovider tests/test_media.py tests/test_pipeline_upload.py tests/test_gpu_batcher.py tests/test_device_selection.py tests/test_device_capacity.py"
```

**macOS and Linux**

```bash
docker compose run --rm --no-deps -v "$PWD/tests:/app/tests:ro" worker sh -c "pip install -q --user pytest && python3 -m pytest -q --noconftest -p no:cacheprovider tests/test_media.py tests/test_pipeline_upload.py tests/test_gpu_batcher.py tests/test_device_selection.py tests/test_device_capacity.py"
```

Run the handler tests in the RunPod image, on the CPU (`CUDA_VISIBLE_DEVICES=`) or with `--gpus all`:

**Windows (PowerShell)**

```powershell
docker build -f Dockerfile.runpod -t piano-transcription-runpod:local .
docker run --rm -e CUDA_VISIBLE_DEVICES= -v "${PWD}\tests:/app/tests:ro" -v "${PWD}\runpod:/app/runpod:ro" piano-transcription-runpod:local sh -c "pip install -q pytest && cd /app && PYTHONPATH=/app python3 -m pytest -q --noconftest -p no:cacheprovider tests/test_runpod_handler.py"
```

**macOS and Linux**

```bash
docker build -f Dockerfile.runpod -t piano-transcription-runpod:local .
docker run --rm -e CUDA_VISIBLE_DEVICES= -v "$PWD/tests:/app/tests:ro" -v "$PWD/runpod:/app/runpod:ro" piano-transcription-runpod:local sh -c "pip install -q pytest && cd /app && PYTHONPATH=/app python3 -m pytest -q --noconftest -p no:cacheprovider tests/test_runpod_handler.py"
```

The tests replace Google, Google Play, `yt-dlp`, DNS, and the transcription pipeline with fakes, so they never use the network or a model.

## 🔎 Syntax check

Run the application syntax check:

```shell
python -m compileall -q app
```

For API-only development install `requirements.api.txt`; for transcription-worker development install `requirements.worker.txt`.

Running the complete stack with Docker remains the recommended way to exercise the worker because it depends on `ffmpeg`, Redis, Transkun, PyTorch, and optional CUDA support.
