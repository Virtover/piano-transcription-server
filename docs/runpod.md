# ☁️ Outsourcing to RunPod

Optional. When the local GPU is busy, jobs run on [RunPod Serverless](https://docs.runpod.io/serverless/overview) instead, billed per second of GPU time. Only the model runs there: this server still downloads and converts the audio, then hands it to RunPod through temporary S3-compatible storage, such as Cloudflare R2. See [how it works](architecture.md#-outsourcing).

> [!IMPORTANT]
> With outsourcing enabled, user audio is sent to RunPod and to the storage provider. Name both as processors in your privacy policy, accept their data processing agreements, and choose EU or EEA locations if your users are there. The server deletes the audio from storage when the job ends.

## 🧭 Setting it up

This page describes only this server. What you must create at each provider, with the values and the reasons, comes from the server itself, so it always matches your configuration:

```shell
docker compose run --rm api python -m app.config requirements
```

It lists the storage bucket, the image registry, the GPU endpoint, and the API key, with values calculated from your settings: for example, the endpoint's minimum execution timeout follows `OUTSOURCE_TIMEOUT`, and the requests per worker follow the GPU memory settings. Add `--json` for scripts and agents.

> [!TIP]
> With an AI coding agent such as Claude Code, run `/setup-runpod`. It turns each requirement into the providers' current settings, guides you through them, and verifies the result.

1. **Create what `requirements` lists**, using each provider's documentation: [Cloudflare R2](https://developers.cloudflare.com/r2/), [Docker Hub](https://docs.docker.com/docker-hub/), [RunPod Serverless](https://docs.runpod.io/serverless/overview).
2. **Save the secrets and configure `.env`**, as in [Configuration](#-configuration).
3. **Build and push the image**, as in [Building the image](#-building-the-image).
4. **Verify everything** against the real services:

   ```shell
   docker compose run --rm api python -m app.config check --online
   ```

   It uploads a small object to the bucket, downloads it through a signed link the way the GPU endpoint does, and deletes it. It checks the endpoint's health, and compares the endpoint's settings with the requirements. Every warning and failure names the likely fix. Repeat until nothing fails.
5. **Restart the stack** so the remote worker reads the new `.env`. It is always part of the stack and idles while `OUTSOURCE_PROVIDER=none`; `.env` is the only switch:

   ```shell
   docker compose up -d --build
   ```

6. **Send one job to RunPod.** Stop the local worker, so local capacity counts as zero, submit a short video (see the [API guide](api.md)), then start the worker again. The job status shows `"backend": "remote"`, and the bucket is empty again afterwards.

   ```shell
   docker compose stop worker
   # submit a job and poll its status
   docker compose start worker
   ```

## ⚙️ Configuration

Save the three secrets as files in `secrets`:

**Windows (PowerShell)**

```powershell
Set-Content -Path secrets\outsource_storage_access_key -Value '<access key ID>' -NoNewline
Set-Content -Path secrets\outsource_storage_secret_key -Value '<secret access key>' -NoNewline
Set-Content -Path secrets\runpod_api_key -Value '<RunPod API key>' -NoNewline
```

**macOS and Linux**

```bash
printf '%s' '<access key ID>' > secrets/outsource_storage_access_key
printf '%s' '<secret access key>' > secrets/outsource_storage_secret_key
printf '%s' '<RunPod API key>' > secrets/runpod_api_key
```

Optionally, a fourth: a RunPod key with read-only access to the account. The key above is restricted to the endpoint, and RunPod does not let restricted keys read endpoint settings. With the read key, `check --online` verifies the endpoint's settings, and `OUTSOURCE_MAX_CONCURRENCY=auto` follows the endpoint's maximum workers; without it, use a number. The read key cannot change anything.

**Windows (PowerShell)**

```powershell
Set-Content -Path secrets\runpod_read_api_key -Value '<RunPod read-only API key>' -NoNewline
```

**macOS and Linux**

```bash
printf '%s' '<RunPod read-only API key>' > secrets/runpod_read_api_key
```

> [!WARNING]
> In PowerShell, always name the parameters as above. The short form `Set-Content -NoNewline <file> <value>` swaps them in Windows PowerShell 5.1 and creates a file **named after your secret** in the current folder. If that happens, delete the file; if it was ever committed, replace the key.

Add to `.env`:

```env
OUTSOURCE_PROVIDER=runpod
RUNPOD_ENDPOINT_ID=<endpoint ID>
OUTSOURCE_STORAGE_ENDPOINT=<the bucket's S3 endpoint URL>
OUTSOURCE_STORAGE_BUCKET=<bucket>
OUTSOURCE_STORAGE_REGION=auto
OUTSOURCE_MAX_CONCURRENCY=4
```

| Setting | Meaning |
| --- | --- |
| `OUTSOURCE_STORAGE_ENDPOINT` | The storage's S3 endpoint URL. The GPU endpoint needs the same value as an environment variable, because the handler downloads only from this host. |
| `OUTSOURCE_LOCAL_FIRST_MAX_WAIT` | Default `30s`. In `local_first` mode, send a job to RunPod when its estimated local wait is longer than this. Remote-first modes do not use a local wait threshold. `OUTSOURCE_MAX_WAIT` remains accepted as a deprecated alias. |
| `OUTSOURCE_MAX_CONCURRENCY` | At most this many jobs on RunPod at once, which caps the cost. `auto` follows the endpoint's maximum workers; it needs the optional read key, and allows 4 jobs without it. |

All settings are in [Configuration](configuration.md#outsourcing).

## 📦 Building the image

```shell
docker login -u <registry-user>
docker build -f Dockerfile.runpod -t <registry-user>/piano-transcription-runpod:<tag> .
docker push <registry-user>/piano-transcription-runpod:<tag>
```

* Use a **new tag for every build** and point the endpoint at it.
* The model is part of the image, so no volume is needed. The image is about 8 GB.
* The image installs PyTorch built for a fixed CUDA version; `requirements` shows which, and the build fails if pip installs a different one.
* Rebuild after changing code the handler uses: `runpod/`, `app/transcription/`, `app/outsource/`. Remove old local images afterwards; see [Freeing disk space](setup.md#-freeing-disk-space).

## 🩺 Troubleshooting

Run `check --online` first: it identifies most configuration problems directly, including endpoint settings that do not match the requirements.

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| Jobs with `"backend": "remote"` stay queued | `remote-worker` is not running, or still idles from before `OUTSOURCE_PROVIDER` was set | Run `docker compose up -d`, then check `docker compose logs remote-worker`. |
| Remote jobs fail and run locally instead | A remote failure with `OUTSOURCE_FALLBACK_LOCAL=true` | `docker compose logs remote-worker` shows the RunPod error. |
| A remote job fails with an error about the audio URL or host | `OUTSOURCE_STORAGE_ENDPOINT` differs between `.env` and the endpoint | `check --online` reports it; use the exact same value in both. |

Logs: `docker compose logs remote-worker` on this server, and the worker logs in the RunPod console. A healthy RunPod worker logs a line like `cuda:0: up to 12 requests at once` at startup; the number depends on the GPU.

## 🧮 Batching on RunPod

A worker takes several requests at once and transcribes them together, in the same batcher as the local worker. How many is its GPU's job capacity, calculated from free VRAM with `WORKER_MEMORY_PER_JOB_GIB` like on the server, capped by `WORKER_MAX_CONCURRENCY` if set on the endpoint. Requests that arrive within `WORKER_BATCH_TIMEOUT` (2 s) of each other share a batch. If a batch runs out of GPU memory, it is split and retried, and the worker takes fewer requests from then on.

Batching makes busy periods cheaper: four 5-minute songs take about 10 s of GPU time each in one batch instead of 14 s each, and they share one cold start and idle period instead of paying for four. At low traffic, requests rarely overlap and each runs alone.

Optional environment variables on the endpoint:

| Key | Default | Set it when |
| --- | --- | --- |
| `WORKER_MAX_CONCURRENCY` | from VRAM | A worker should take fewer requests than fit. Scale the endpoint by the same number of requests per worker. |
| `WORKER_MEMORY_PER_JOB_GIB` | `1.1` | Workers run out of GPU memory. |
| `WORKER_BATCH_TIMEOUT` | `2s` | Requests should wait longer or shorter for a batch. |
| `OUTSOURCE_MAX_AUDIO_MB` | `512` | Long recordings are rejected. |

## ⏱️ Start-up time

Measured on an RTX 3060 Ti; 16 GB data-center GPUs are similar.

| Situation | Extra wait for the first job |
| --- | --- |
| A paused worker resumes | under 1 to 2 s |
| A worker starts, with the image already on that machine | about 10 to 15 s: 6 to 9 s to load PyTorch and the model, 3 to 4 s of GPU warm-up |
| A machine must first download the image | about 1 to 3 minutes, estimated |

## 🧪 Free local end-to-end test

Test outsourcing without RunPod or R2 accounts. [SeaweedFS](https://github.com/seaweedfs/seaweedfs) stands in for the storage, because MinIO no longer publishes community container images. The handler runs with the RunPod SDK's local test server, on the CPU so it does not take VRAM from the GPU worker. On the CPU, a 4-minute recording takes about 1.5 minutes, so test with short recordings.

1. Start the storage with a test key, and create the bucket:

**Windows (PowerShell)**

```powershell
docker network create piano-test
Set-Content -Path s3.json -Value '{"identities":[{"name":"test","credentials":[{"accessKey":"test-access","secretKey":"test-secret"}],"actions":["Admin","Read","Write","List"]}]}' -NoNewline
docker run -d --name seaweed --network piano-test -v "${PWD}\s3.json:/etc/s3.json:ro" `
  chrislusf/seaweedfs server -dir=/data -s3 -s3.port=8333 -s3.config=/etc/s3.json
docker run --rm --network piano-test yt-piano-to-midi-server-api python -c "import boto3; boto3.client('s3', endpoint_url='http://seaweed:8333', region_name='us-east-1', aws_access_key_id='test-access', aws_secret_access_key='test-secret').create_bucket(Bucket='piano-audio')"
```

**macOS and Linux**

```bash
docker network create piano-test
printf '%s' '{"identities":[{"name":"test","credentials":[{"accessKey":"test-access","secretKey":"test-secret"}],"actions":["Admin","Read","Write","List"]}]}' > s3.json
docker run -d --name seaweed --network piano-test -v "$PWD/s3.json:/etc/s3.json:ro" \
  chrislusf/seaweedfs server -dir=/data -s3 -s3.port=8333 -s3.config=/etc/s3.json
docker run --rm --network piano-test yt-piano-to-midi-server-api python -c "import boto3; boto3.client('s3', endpoint_url='http://seaweed:8333', region_name='us-east-1', aws_access_key_id='test-access', aws_secret_access_key='test-secret').create_bucket(Bucket='piano-audio')"
```

2. Build and start the handler with the local test server:

**Windows (PowerShell)**

```powershell
docker build -f Dockerfile.runpod -t piano-transcription-runpod:local .
docker run -d --name runpod-local --network piano-test `
  -e CUDA_VISIBLE_DEVICES= -e OUTSOURCE_STORAGE_ENDPOINT=http://seaweed:8333 `
  piano-transcription-runpod:local `
  python3 -u handler.py --rp_serve_api --rp_api_host 0.0.0.0 --rp_api_port 8010
```

**macOS and Linux**

```bash
docker build -f Dockerfile.runpod -t piano-transcription-runpod:local .
docker run -d --name runpod-local --network piano-test \
  -e CUDA_VISIBLE_DEVICES= -e OUTSOURCE_STORAGE_ENDPOINT=http://seaweed:8333 \
  piano-transcription-runpod:local \
  python3 -u handler.py --rp_serve_api --rp_api_host 0.0.0.0 --rp_api_port 8010
```

3. Put `test` in `secrets/runpod_api_key`, `test-access` in `secrets/outsource_storage_access_key`, and `test-secret` in `secrets/outsource_storage_secret_key`, and add to `.env`:

```env
OUTSOURCE_PROVIDER=runpod
RUNPOD_ENDPOINT_ID=local
RUNPOD_API_BASE=http://runpod-local:8010
OUTSOURCE_STORAGE_ENDPOINT=http://seaweed:8333
OUTSOURCE_STORAGE_BUCKET=piano-audio
OUTSOURCE_STORAGE_REGION=us-east-1
OUTSOURCE_LOCAL_FIRST_MAX_WAIT=0s
OUTSOURCE_MAX_CONCURRENCY=1
```

4. Start the stack with the remote worker, and connect it to the test network:

```shell
docker compose up -d --build
docker network connect piano-test yt-piano-to-midi-server-remote-worker-1
```

With `OUTSOURCE_LOCAL_FIRST_MAX_WAIT=0s`, a job is outsourced as soon as every local slot is busy: submit one job more than the worker's capacity, which the worker log shows at startup. To send every job remote, use `OUTSOURCE_ROUTING=remote_first_fallback`; this does not depend on the local worker heartbeat. The job status shows `"backend": "remote"`, and the bucket is empty again after each job.

The local test server differs from RunPod in two ways:

* It runs one job at a time and blocks all other requests meanwhile, so keep `OUTSOURCE_MAX_CONCURRENCY=1`. With more, the next submission times out and that job falls back to the local queue. Batching across requests only happens on RunPod; `tests/test_runpod_handler.py` checks it locally.
* It runs the handler while the job's status is first requested. Progress therefore jumps from 10% to 96%, and a cancellation takes effect only when the handler returns.

Afterwards, remove the test setup and the outsourcing lines from `.env`:

```shell
docker rm -f seaweed runpod-local
docker network rm piano-test
```
