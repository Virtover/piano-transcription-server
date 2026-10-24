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

## 🤖 Continuous integration

GitHub Actions runs the tests on every push to `main` and every pull request ([.github/workflows/tests.yml](../.github/workflows/tests.yml)), on Linux with Python 3.12 and ffmpeg, from a clean checkout: no `.env`, no secrets. The model tests, which need the worker requirements and PyTorch, are skipped there. Run them locally after installing `requirements.worker.txt`.

A test that needs an optional dependency must skip without it: import the dependency with `pytest.importorskip` before anything that uses it, not with a plain `import` at the top.

CI runs as an ordinary user, not root, so code must not write to the container paths (`/billing`, `/data`) when it is imported. Create files on first use instead, as `Billing` does. To reproduce CI locally, run the tests as a non-root user in a `python:3.12-slim` container.

## 🗂️ What is covered

| File | Covers |
| --- | --- |
| `test_transcriptions_api.py` | Job creation, status, cancellation, MIDI download, path checks, job ownership, reservations, and startup and worker errors. |
| `test_billing_api.py` | Server info, cost, balance, Google Play purchase verification, and free-minute grants. |
| `test_account_api.py` | Account deletion: removed data, kept anonymous purchases, running jobs, free-minute abuse protection, and the `admin_delete` command. |
| `test_billing.py` | Cost formulas, reservations and settlement, free minutes, and the `admin_credit` command. |
| `test_backups.py` | Backup creation, retention pruning, interval and retention settings, and duration parsing. |
| `test_online_check.py` | `check --online`: each service check's success, failure hints, and secret-free output, and RunPod endpoint settings against the requirements, with fake services. |
| `test_maintenance.py` | Planned restarts and shutdowns: the notice, its expiry, messages, and return time, refusing jobs that would not finish, the `maintenance` field, the `X-Server-Maintenance` headers, `Retry-After`, and clearing past notices at startup. |
| `test_server_script.py` | `scripts/server.py` with a fake stack and clock: announce, build during the countdown, cancel on build failure or Ctrl+C, drain within the grace period, restart, stop every profile, start, and clear. |
| `test_requirements.py` | `requirements`: values calculated from the configuration, the RunPod CUDA version matching `Dockerfile.runpod`, and the text and JSON output. |
| `test_job_diagnostics.py` | Job diagnostics records at each job state, survival after the Redis job expires, account deletion, pruning, and inclusion in backups. |
| `test_worker_tasks.py` | Worker job lifecycle, billing reconciliation, restart handling, and file cleanup. |
| `test_auth.py` | Google token checks: audience, expiry, verified email, userinfo fallback, caching, and outages. |
| `test_url_safety.py` | Source URL validation: allowed hosts, schemes, ports, credentials, and private addresses. |
| `test_server_api.py` | Health and readiness, server info offers, and `yt-dlp` duration handling. |
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
