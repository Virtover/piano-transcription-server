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

`pytest.ini` keeps the output short: short tracebacks, and no warnings for known deprecations. Run one file or one test while working on a feature:

```shell
python -m pytest -q tests/test_account_api.py
python -m pytest -q -k cancellation
```

## 🐳 Tests that need the worker's packages

Tests that import `torch` or `transkun` (`tests/test_ensemble.py`, `tests/test_ensemble_models.py`, `tests/test_budget.py`, and the batching and device tests) are skipped in the test environment above, which does not install them. Run them in a worker image, which has the packages and dettato's weights, which `tests/test_ensemble_models.py` needs. The image has no pytest and no tests, so mount the tests and install pytest in the container:

```shell
docker compose run --rm --no-deps -v ./tests:/app/tests:ro -v ./pytest.ini:/app/pytest.ini:ro worker sh -c "python3 -m pip install -q pytest && python3 -m pytest -q tests/test_budget.py tests/test_ensemble.py tests/test_ensemble_models.py"
```

## 🧱 Tests on a real Redis

fakeredis does not enforce Redis's users, and does not restart. `tests/test_node_redis_rules.py` starts `redis:7-alpine` with `redis/start.sh`, as the stack does, and checks that a [cluster](cluster.md) node's rules allow everything a worker does, Dramatiq's scripts included, and nothing else, and that transactions survive a restart of Redis. It needs Docker, and is skipped unless asked for:

**Windows (PowerShell)**

```powershell
$env:PIANO_DOCKER_TESTS = "1"; .\.venv\Scripts\python -m pytest -q tests/test_node_redis_rules.py
```

**macOS and Linux**

```bash
PIANO_DOCKER_TESTS=1 .venv/bin/python -m pytest -q tests/test_node_redis_rules.py
```

Run it after changing `redis/users.acl`, `redis/node.acl`, `redis/start.sh`, or a Redis key a worker uses.

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

## 🧪 End-to-end checks on a running server

The tests above use fakes. To check a running server end to end, with its real workers, GPUs, nodes, and RunPod, run real jobs and check their MIDI:

```shell
python scripts/admin.py e2e                       # every case, placed as the server places jobs
python scripts/admin.py e2e --backend local       # only this server's workers, and its nodes
python scripts/admin.py e2e --backend remote      # only RunPod; needs outsourcing on
python scripts/admin.py e2e --case clip --mode ensemble
python scripts/kube.py admin e2e                  # the same on Kubernetes
```

Each case uploads a generated 40-second test file and queues it as an unbilled job: no user, nothing charged. The file has four 10-second sections of plucked notes, C4, E4, G4, then A4, so a part cut in the wrong place shows in the notes. The cases are the whole file, `clip` (10 to 20 seconds), and `clip-to-end` (from 20 seconds), each as a piano job and, when ensemble transcriptions are on, as an ensemble job (`--mode piano` or `ensemble` picks one). A case passes when the job completes, is charged for the part's length, and its MIDI has notes only within that length, of the part's sections, and none of the others. Both modes are checked by the same note check; drum notes are left out. Each line says which backend and device ran the job. The command exits with code 1 when a check fails. The jobs and their files are removed afterwards (`--keep` keeps them), and the job list keeps them, titled "e2e check".

* **One device of a home cluster:** stop the others taking work, for example `python scripts/admin.py nodes deny hub transcribe arrange`, run `e2e --backend local`, then `nodes allow hub transcribe arrange`.
* **A crash or power cut:** run `e2e --backend local --case clip --mode piano --timeout 1800` in one terminal. While it runs, make the device's worker exit without warning. On a node, `docker kill -s KILL piano-node-worker-1` and then `docker start piano-node-worker-1`. With Compose, `docker compose kill -s KILL worker` and then `docker compose up -d worker`. The check must still pass: the job runs again, the same part, charged once.
* The first notes of a part, at 0 seconds, may be missing: the model can miss notes at the very start of any audio. The checks allow for it.

## 🤖 Continuous integration

GitHub Actions runs the tests on every push to `main` and every pull request ([.github/workflows/tests.yml](../.github/workflows/tests.yml)), on Linux with Python 3.12 and ffmpeg, from a clean checkout: no `.env` or `server.env`, no secrets. The model tests, which need the worker requirements and PyTorch, are skipped there. Run them locally after installing `requirements.worker.txt`. If a push did not start a run, start one on the repository's Actions tab: **Tests**, then **Run workflow**.

A test that needs an optional dependency must skip without it: import the dependency with `pytest.importorskip` before anything that uses it, not with a plain `import` at the top.

CI runs as an ordinary user, not root, so code must not write to the container paths (`/billing`, `/data`) when it is imported. Create files on first use instead, as `Billing` does. To reproduce CI locally, run the tests as a non-root user in a `python:3.12-slim` container.

## 🗂️ What is covered

| File | Covers |
| --- | --- |
| `test_ensemble.py`, `test_ensemble_midi.py`, `test_ensemble_cpu.py`, `test_ensemble_models.py` | Ensemble transcriptions: the batcher and the memory holds of an ensemble job (needs the worker's packages), the MIDI of tracks, names, and channels, the dettato.cpp process and its limits, and the models themselves (only in a worker image with torch and the weights). |
| `test_ensemble_api.py`, `test_ensemble_tasks.py` | Ensemble transcriptions through the API and the job lifecycle: the `mode` request field and its old names, the ignored `style`, routing, the status and file name, the server info flags, which actor takes a job, restarts, retries on another backend, and the worker's heartbeat. |
| `test_grouper_decode.py` | The grouper's clustering against dettato's own algorithm: the same merges, ties included, and the path for recordings of several hours. Needs torch. |
| `test_source_sample_rate.py` | The source's sample rate read before the conversion, and handed to the ensemble drums. |
| `test_disk.py` | Running out of disk space: jobs wait for room and fail clearly after half an hour, a job whose disk filled up is tried once more, submissions get `503 storage_full`, and the watcher warns. |
| `test_apt_install.py` | `build/apt_install.sh`: a hung `apt-get install` is ended and tried again. Needs a POSIX shell; skipped on Windows. |
| `test_e2e.py` | The end-to-end checks of `admin e2e`: reading MIDI, what passes and fails, and a run against fake workers. |
| `test_clips.py` | Transcribing a part of a video or file: the checks of `start_seconds` and `end_seconds` and hostile values, the job's stored part, billing only its length, the cost, the cut ffmpeg makes, the RunPod dispatcher, a job run again after its worker stopped, and an administrator's requeue. |
| `test_budget.py` | The shared per-device memory budget. It needs the worker's packages. |
| `test_cluster.py` | A [home cluster](cluster.md): attempt tokens, so a device that lost its job writes nothing; jobs queued again after an interruption, and once only refunded; billing the hub settles from the job's state; jobs handed to another device; rate limits per public address; link checks by a node; the settings nodes take from the hub; switches and status; the supervisor; the shape of a node's Redis rules. |
| `test_load_placement.py` | Placing jobs by load: a busy GPU, or one without VRAM, leaves a job to a less busy device with room; GPU and CPU sampling. |
| `test_cluster_scripts.py` | The cluster's host scripts: `nodes hub`, `invite`, `revoke`, and `join`, what an invite holds and never holds, node mode of `scripts/server.py`, and the registry's images. |
| `test_node_redis_rules.py` | A node's Redis rules, and transactions across a Redis restart, on a real Redis (Docker; see [Tests on a real Redis](#-tests-on-a-real-redis)). |
| `test_transcriptions_api.py` | Job creation, status, cancellation, MIDI download, path checks, job ownership, reservations, and startup and worker errors. |
| `test_billing_api.py` | Server info, cost, balance, Google Play purchase verification, and free-minute grants. |
| `test_developer.py` | Developer accounts: never charged, not held to the per-user limit, job details and the server's state only for them, and the column added to old databases. |
| `test_account_api.py` | Account deletion: removed data, kept anonymous purchases, running jobs, free-minute abuse protection, and deletion with `app.admin user ... delete`. |
| `test_billing.py` | Cost formulas, reservations and settlement, reserving a failed job again, monthly minutes (spent first, renewal, expiry, jobs running across a renewal, upgrading an older database), changes by hand, and the minute ledger. |
| `test_admin.py` | `python -m app.admin`: status, listing and showing jobs, cancelling and queueing them again, user accounts, credits and deletion, the billing report, limits, the audit log, secret-free output, `--json`, and the data functions without the command line; the safety rules: no questions without a terminal, required reasons without email addresses, typed confirmation for deletion, 8-character job IDs for changes, ambiguous users, read-only reports, changes saved with their audit entry, dry runs, and undo hints. |
| `test_controls.py` | Temporary controls in the API: pausing submissions, turning links off, blocking a user, the per-user limit, expiry, and running jobs left alone. |
| `test_backups.py` | Backup creation, retention pruning, interval and retention settings, and duration parsing. |
| `test_online_check.py` | `check --online`: each service check's success, failure hints, and secret-free output, and RunPod endpoint settings against the requirements, with fake services. |
| `test_announcements.py` | Announcements: what is accepted (https links only, in ASCII, no control or invisible characters, English required, length and count limits), the formatting (blocks, plain text, links in a body checked, unpaired marks, and the admin page's parser giving the same result, run with Node.js when it is installed), the cache never holding up the API and not asking a database that is down on every request, links whose host could mislead, bodies parsed once per read, nothing about administrators in the public answer, no announcement without its audit entry after a failed write, recovery after a restart while the database is down, a clock set back, which app sees which by platform, version, and language, order and time window, the endpoint and its parameters, answering from the last list while the database is down and `503` before any was read, rows changed by hand into something invalid, and `announce` with confirmation, audit entries, `--replace`, ending, and dry runs. The admin web page's part is in `test_admin_web.py`. |
| `test_maintenance.py` | Planned restarts and shutdowns: the notice, its expiry, messages, and return time, refusing jobs that would not finish, the `maintenance` field, the `X-Server-Maintenance` headers, `Retry-After`, and clearing past notices at startup. |
| `test_admin_script.py` | `scripts/admin.py`: the command it runs in the API container, no terminal for the container without one, the user name the audit log gets, and how another entry point names itself. |
| `test_admin_web.py` | The admin web page: Cloudflare Access tokens (signature, algorithm, issuer, audience, lifetime, allowed addresses, key rotation), every route refusing strangers, the security headers, read-only routes, recorded account views, lowering its own access and every way to misuse it, read-write changes (plan, single-use confirmation kept on the server, the same administrator, refused when anything changed, rate limit, credit cap, nothing irreversible, announcements published, replaced meanwhile, and refused), the page's code (no HTML parsing, nothing loaded from elsewhere, no accidental lowering), restoring it from the command line, its settings, its Compose service, and its online check. Its credentials were also checked against a real Redis and PostgreSQL. |
| `test_redis_users.py` | Redis users: the rules in `redis/users.acl`, which services see which password, that every Redis client signs in, and the `check --online` probe. The rules themselves were checked against a real Redis, because fakeredis does not enforce them. |
| `test_server_script.py` | `scripts/server.py` with a fake stack and clock: announce, build during the countdown, cancel on build failure or Ctrl+C, drain within the grace period, restart, stop every profile, start, and clear; and moving each line of `.env` and `server.env` to its file. `scripts/stacks.py`: the docker commands, every request built on `exec` and `one_off`, and no other script building a `docker compose` command. |
| `test_cloudflare_tunnel.py` | The optional Cloudflare Tunnel: `COMPOSE_PROFILES`, the token check, warnings for an open server, the `check --online` results and hints, the requirements, and the `cloudflared` service in `docker-compose.yml`. |
| `test_requirements.py` | `requirements`: values calculated from the configuration, the RunPod CUDA version matching `Dockerfile.runpod`, the text and JSON output, and `--section` and `--feature`. |
| `test_job_diagnostics.py` | Job diagnostics records at each job state, survival after the Redis job expires, account deletion, pruning, and inclusion in backups. |
| `test_worker_tasks.py` | Worker job lifecycle, billing reconciliation (leaving jobs that just ended to whoever ended them), restart handling, and file cleanup. |
| `test_auth.py` | Google token checks: audience, expiry, verified email, userinfo fallback, caching, remembered rejections, the limit on calls to Google at once, and outages. |
| `test_url_safety.py` | Source URL validation: allowed hosts, schemes, ports, credentials, and private addresses. |
| `test_failed_links.py` | Link checks refused before `yt-dlp`: past the threads of an API replica, and for a client with `MAX_FAILED_VIDEO_CHECKS` different failed links; the same link counting once, the window, what never counts, and the counts expiring. |
| `test_server_api.py` | Health and readiness, server info offers, `yt-dlp` duration handling, reused video checks, and the video information saved for the worker. |
| `test_upload_api.py` | File uploads: storage, size and length limits, title handling, billing, cleanup on errors, and cost by duration. |
| `test_media.py` | `ffprobe` checks: accepted and rejected formats, files without audio, and playlist files. Real-file tests need `ffmpeg`. |
| `test_pipeline_upload.py` | Converting uploaded audio and video to the WAV format the model needs. Needs the worker image. |
| `test_config.py` | Settings parsing, durations, deprecated names, unknown-name warnings, secrets, startup validation, the `check` and `describe` commands, and drift guards against `.env.example` and these docs. |
| `test_launcher.py` | Worker resources resolved at startup: places, threads, CPU threads, and the values passed to Dramatiq. |
| `test_places.py` | Places on a device's models: in order of arrival, given back when the model step ends or the job fails; on a GPU, room for each job's memory in the VRAM other programs leave, waiting while they hold it, one winner of a race for the last room; a job without a place stays queued, goes to a device with a free one, or is queued again when the worker stops; the heartbeat reports the places that fit now. |
| `test_job_storage.py` | `JOB_STORAGE=s3` with an in-memory bucket: submissions kept only in the bucket, workers' scratch folders, results and signed download links, cleanup, account deletion, settings, requirements, and the online check. |
| `test_managed_services.py` | Redis and PostgreSQL outside the stack: TLS options, warnings about unencrypted connections, the online check without `CONFIG`, backups turned off, and their requirements. |
| `test_metrics.py` | `GET /metrics`: its token, waiting and running jobs per queue, the oldest wait, worker capacity, the text format, and the requests in flight. |
| `test_slots.py` | Places limited across processes through Redis: the limit, unlimited places, a crashed holder's place freeing itself, and waiting for a place. |
| `test_kubernetes.py` | `deploy/kubernetes` against `docker-compose.yml`: images, commands, wiring, secret mounts, the tunnel token's isolation, the non-root user and hardening, and scaling on metrics the API reports. |
| `test_copy_to.py` | `python -m app.backups copy-to`: the newest backup restored in one transaction, the password kept out of the command line, and refusals. |
| `test_features.py` | The optional features' table: references, needs and exclusions, required settings and secrets per role, unused settings, `features --json` and `--feature`, and `.env.example` and the docs in step with it. |
| `test_setup_script.py` | `scripts/setup.py`: editing `server.env` and `.env` in place, descriptions from the docs, setting features up with what they need, secrets never shown, what is missing named by its file or setting, the status columns explained with what differs on Kubernetes, colors only in a terminal, and turning features off; values the server would refuse asked again, a failed online test asking again for what its hint names or undoing the setup, the tunnel tested after a restart, `check`, `set` with its undo and Kubernetes choices, `fix` (a byte order mark, broken and repeated lines, typos in names, UTF-16 and misnamed secret files, folders Docker made), and the one container that answers, with its fallback. |
| `test_diagnosis.py` | `check --json`, `validate`, and the configuration session of `app/diagnosis.py`: the settings and secret files a problem names, renames for typos, deprecated names, and old secret file names, problems by feature, online results by feature with what they need, a session reading the files as they change, `check` naming a value it cannot read instead of failing to start, and a secret file that is no UTF-8. |
| `test_kube_script.py` | `scripts/kube.py` with a fake kubectl and docker: one question before any change, the order of the steps, Secrets from files and never shown, the test cluster's generated passwords and Redis users, KEDA installed for its scaling, the context checks, `down`'s confirmation, and `status`; and `scripts/server.py`'s and `scripts/admin.py`'s commands through `KubeStack`: one question before a restart, a rolling restart of the server's Deployments only, KEDA paused by `stop`, the build in the background, and `restore` refused. |
| `test_leader.py` | Which API replica leads: one at a time, handover on shutdown and after a crash, periodic tasks waiting for the lead, startup recovery only when no other API runs, and recovery by a replica that takes over. |
| `test_tempo_map.py`, `test_beat_decoder.py` | The tempo map: notes keeping their time, lead beats, pickups and meter changes, beat repairs, rejected beats and bars, and writing the file; decoding beats at one level and bars from Beat This!'s probabilities. |
| `test_batching.py` | Batch size rules (`auto` per GPU, clamping, CPU default) and splitting a batch in half on GPU out-of-memory. |
| `test_gpu_batcher.py` | The real batcher and batch loop with a fake model that runs out of memory. Needs the worker image. |
| `test_outsource_routing.py` | Routing: wait estimate, threshold, remote concurrency cap, missing heartbeat, several local workers, provider `none`, and the API's backend choice. |
| `test_remote_jobs.py` | Remote jobs with a fake RunPod and storage: success, progress mapping, cancellation, fallback to local, timeouts, failures, single billing, and storage cleanup on every path. |
| `test_outsource_capacity.py` | `OUTSOURCE_MAX_CONCURRENCY`: the default, fixed values, and `auto` from RunPod's max workers and the reported requests per worker. |
| `test_outsource_clients.py` | The RunPod client, S3 storage, and the handler's input checks and download limits. |
| `test_runpod_handler.py` | The RunPod handler with the real model: concurrent requests share one batch, and bad requests fail alone. Needs the RunPod image (see below). |
| `test_video_info.py` | One request to the video site per job: the saved video information, its age limit, the retry when its links fail, and real `yt-dlp` downloading from saved information. |
| `test_audio_download.py` | The yt-dlp and ffmpeg commands: no generic extractor, conversion only by the restricted ffmpeg, and audio cut at the charged duration. |
| `test_errors.py` | Job errors as users see them: no command lines or paths, and yt-dlp's reason when it gives one. |
| `test_job_limits.py` | `MAX_ACTIVE_JOBS` and `MAX_ACTIVE_JOBS_PER_USER`: places taken during uploads and video checks, given back when jobs end or fail. |
| `test_billing_races.py` | Changes of plan with the replaced and the replacing token reported in either order or at once, and races: threads released together reserving, settling, releasing, crediting a purchase, recording one subscription order, claiming one subscription from many accounts, refunding, renewing, granting to everyone and undoing it, and the API's verifications and submissions at once. With `PIANO_TEST_POSTGRES_URL`, also on PostgreSQL. |
| `test_subscriptions.py` | Subscriptions (plans' settings and levels, periods replacing free minutes, renewals, grace, hold, upgrades, one account per subscription, the refresh from Google Play, acknowledgement), unlimited plans and their fair-use limit, refunded orders, crashes mid-transaction, welcome minutes, and minutes for every account. With `PIANO_TEST_POSTGRES_URL`, also on PostgreSQL, with the admin web page's role. |
| `test_voided_purchases.py` | Refunds and chargebacks: minutes taken back once, negative balances, free minutes, and reading every page from Google Play. |
| `test_deployment.py` | The images: pinned requirements and deno, services not running as root, and the startup check for root-owned volumes. |
| `test_build_sources.py` | Download sources of the image builds: apt's mirror lists, `build/fetch.py` keeping only a file with its checksum, `build/pip_install.py` moving to the next index, every Dockerfile using them, and the images taken from a copy of Docker Hub's when it fails. |
| `test_device_capacity.py` | GPU capacity calculation from total VRAM, the live room for jobs (`GpuRoom`: the worker's own memory, caching, failed readings), and the `nvidia-smi` free memory check used under WSL. |
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
