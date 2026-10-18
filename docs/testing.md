# 🧪 Testing

The test suite uses an in-memory Redis substitute and temporary SQLite databases. It does not require Docker Compose, a GPU, or Google Play credentials.

## 🐍 Test environment

Create a virtual environment and install the test dependencies:

```powershell id="vznqyw"
py -3.12 -m venv .venv

.\.venv\Scripts\Activate.ps1

python -m pip install -r requirements.test.txt
```

## ✅ Run tests

```powershell id="nbxv8g"
python -m pytest -q
```

Run one file or one test while working on a feature:

```powershell
python -m pytest -q tests/test_account_api.py
python -m pytest -q -k cancellation
```

## 🗂️ What is covered

| File | Covers |
| --- | --- |
| `test_transcriptions_api.py` | Job creation, status, cancellation, MIDI download, path checks, job ownership, reservations, and startup and worker errors. |
| `test_billing_api.py` | Server info, cost, balance, Google Play purchase verification, and free-minute grants. |
| `test_account_api.py` | Account deletion: removed data, kept anonymous purchases, running jobs, free-minute abuse protection, and the `admin_delete` command. |
| `test_billing.py` | Cost formulas, reservations and settlement, free minutes, and the `admin_credit` command. |
| `test_backups.py` | Backup creation, retention pruning, interval and retention settings, and duration parsing. |
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
| `test_device_capacity.py` | GPU capacity calculation, including the `nvidia-smi` free memory check used under WSL. |
| `test_device_selection.py` | GPU and CPU device selection for transcription jobs. Needs the worker dependencies, so it is skipped in the API test environment. |

Some tests need PyTorch, Transkun, or `ffmpeg`, which are only in the worker image. They are skipped in the API test environment. Run them in the worker image:

```powershell
docker compose run --rm --no-deps -v "${PWD}\tests:/app/tests:ro" worker sh -c "pip install -q pytest && python3 -m pytest -q --noconftest -p no:cacheprovider tests/test_media.py tests/test_pipeline_upload.py tests/test_gpu_batcher.py tests/test_device_selection.py tests/test_device_capacity.py"
```

The tests replace Google, Google Play, `yt-dlp`, DNS, and the transcription pipeline with fakes, so they never use the network or a model.

## 🔎 Syntax check

Run the application syntax check:

```powershell id="y6e1gz"
python -m compileall -q app
```

For API-only development install `requirements.api.txt`; for transcription-worker development install `requirements.worker.txt`.

Running the complete stack with Docker remains the recommended way to exercise the worker because it depends on `ffmpeg`, Redis, Transkun, PyTorch, and optional CUDA support.
