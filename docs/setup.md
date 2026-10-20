# 🛠️ Local setup

## 📋 Requirements

* Docker Desktop with Docker Compose and Linux containers enabled.
* NVIDIA drivers and NVIDIA Container Toolkit for GPU transcription.
* A public video URL containing an audio or piano performance.

The worker image includes `ffmpeg`, Transkun, and the Python dependencies. Redis and shared job storage are provided by Compose.

## 🚀 Run the stack

```powershell
Copy-Item .env.example .env
docker compose run --rm api python -m app.config check
docker compose up --build
```

The `check` command validates `.env` and the secrets and prints the resolved configuration with secrets masked. Container paths and service addresses are set in `docker-compose.yml`, so `.env` only holds your choices. See [Configuration](configuration.md).

The API is available at `http://localhost:8000` and its interactive OpenAPI documentation is at `http://localhost:8000/docs`.

Check the service state from PowerShell:

```powershell
Invoke-RestMethod http://localhost:8000/health
Invoke-RestMethod http://localhost:8000/ready
```

Do not commit `.env`, the `secrets` folder, or any machine-specific values.

## 🎮 GPU and CPU execution

Verify GPU access before starting the stack:

```powershell
docker run --rm --gpus all nvidia/cuda:12.6.0-cudnn-runtime-ubuntu22.04 nvidia-smi
```

The worker automatically uses CUDA when PyTorch detects a usable NVIDIA GPU. Otherwise it falls back to CPU. CPU execution is significantly slower; on machines without GPU support, remove `gpus: all` from the worker service in `docker-compose.yml`.

By default, one worker process shares one model and batches requests. GPU capacity is estimated from VRAM free at startup using `WORKER_MODEL_MEMORY_GIB` (`1.0` GiB reserved for the model and GPU libraries) and `WORKER_MEMORY_PER_JOB_GIB` (`1.1` GiB for each job). See [GPU memory and batching](configuration.md#-gpu-memory-and-batching) for measurements and tuning.

## 📈 Worker scaling

Configure the shared worker with explicit CPU compute threads and request batching:

```env
WORKER_THREADS=4
WORKER_BATCH_SIZE=auto
WORKER_BATCH_TIMEOUT=2s
```

With `WORKER_BATCH_SIZE=auto`, each GPU batches as many jobs as fit in its VRAM, and the CPU batches 6. The API accepts jobs immediately and Redis queues work beyond available worker capacity. If a batch runs out of GPU memory, the worker splits it and retries. See [WORKER_BATCH_SIZE](configuration.md#choosing-other-values).


## ☁️ Outsourcing to RunPod

Optional. When local GPUs are overloaded, jobs run on [RunPod Serverless](https://docs.runpod.io/serverless/overview), and RunPod bills per second of GPU time. Only the model runs remotely: this server still downloads and converts audio, then hands it to RunPod through S3-compatible storage. See [how it works](architecture.md#-outsourcing).

> [!IMPORTANT]
> With outsourcing enabled, user audio is sent to RunPod and to the storage provider. Mention both as processors in your privacy policy, and choose EU locations if your users are in the EU. Audio is deleted from storage when the job ends, and the lifecycle rule below deletes anything left behind within a day. RunPod keeps job results (the MIDI) for 30 minutes.

### 1. Cloudflare R2 storage

1. In the Cloudflare dashboard, open **R2** and create a bucket, for example `piano-transcription-audio`. Under **Location**, choose **Specify jurisdiction → European Union**.
2. In the bucket settings, add an **Object lifecycle rule** that deletes objects 1 day after upload. The server deletes each object itself; this catches leftovers after a crash.
3. Under **R2 → Manage API tokens**, create a token with **Object Read & Write** permission, applied to this bucket only.
4. Save the token's credentials as files and configure `.env`:

```powershell
Set-Content -Path secrets\outsource_storage_access_key -Value '<Access Key ID>' -NoNewline
Set-Content -Path secrets\outsource_storage_secret_key -Value '<Secret Access Key>' -NoNewline
```

```env
OUTSOURCE_STORAGE_ENDPOINT=https://<account-id>.eu.r2.cloudflarestorage.com
OUTSOURCE_STORAGE_BUCKET=piano-transcription-audio
OUTSOURCE_STORAGE_REGION=auto
```

EU-jurisdiction buckets use the `.eu.` endpoint shown above. The account ID is on the R2 overview page.

### 2. RunPod endpoint

Build and push the handler image. The model weights are part of the image, so no network volume is needed:

```powershell
docker build -f Dockerfile.runpod -t <registry>/piano-transcription-runpod:1 .
docker push <registry>/piano-transcription-runpod:1
```

In the RunPod console, create a **Serverless endpoint** from that image:

* **GPUs:** 16 GB first, with 24 GB as fallback. One job needs about 1.1 GiB of VRAM, so a 16 GB GPU transcribes about 12 jobs at once.
* **Active workers:** 0, so you pay only while jobs run.
* **Max workers:** enough for `OUTSOURCE_MAX_CONCURRENCY` jobs at about 12 per worker, so 1 for up to 12 jobs and 2 for up to 24. One more gives headroom when a worker is starting.
* **Scaling:** by request count, with about 12 requests per worker, so RunPod adds a worker only when the running ones are full instead of one per request.
* **Idle timeout:** short, for example 5 seconds.
* **FlashBoot:** on, to shorten cold starts.
* **Data centers:** EU locations only, if your users are in the EU.
* **Environment variables:** `OUTSOURCE_STORAGE_ENDPOINT`, set to the same value as on the server. The handler only downloads from that host. Optionally `OUTSOURCE_MAX_AUDIO_MB`, and `WORKER_MAX_CONCURRENCY` to take fewer requests per worker than fit in VRAM.

#### Batching on RunPod

A worker takes several requests at once and transcribes them together, in the same batcher as the local worker. How many is its GPU's job capacity, calculated from free VRAM with `WORKER_MEMORY_PER_JOB_GIB` like on the server, capped by `WORKER_MAX_CONCURRENCY` if set there; the worker log shows it at startup, for example `cuda:0: up to 12 requests at once`. Requests that arrive within `WORKER_BATCH_TIMEOUT` (2 s) of each other share a batch. If a batch runs out of GPU memory, it is split and retried, and the worker takes fewer requests from then on.

Batching makes busy periods cheaper: four 5-minute songs take about 10 s of GPU time each in one batch instead of 14 s each, and they share one cold start and idle timeout instead of paying for four. At low traffic, requests rarely overlap and each runs alone.

Then create an API key under **Settings → API Keys** with **Restricted** permissions, giving access only to this endpoint, and configure the server. With `OUTSOURCE_MAX_CONCURRENCY=auto`, the key also needs read access to the endpoint's settings, so the server can read its max workers:

```powershell
Set-Content -Path secrets\runpod_api_key -Value '<API key>' -NoNewline
```

```env
OUTSOURCE_PROVIDER=runpod
RUNPOD_ENDPOINT_ID=<endpoint ID>
OUTSOURCE_MAX_WAIT=2m
OUTSOURCE_MAX_CONCURRENCY=4
```

### 3. Start the remote worker

```powershell
docker compose run --rm api python -m app.config check
docker compose --profile outsource up -d --build
```

The `remote-worker` service starts only with the `outsource` profile. Without it, set `OUTSOURCE_PROVIDER=none`, or remote jobs wait in the queue.

### RunPod limits

* A `/run` request body may be at most 10 MB, and a `/runsync` body at most 20 MB ([operation reference](https://docs.runpod.io/serverless/endpoints/operation-reference)). That is why audio goes through storage instead of the request.
* Job results are kept for 30 minutes after completion. The handler returns the MIDI as base64 in the result, and refuses results above 8 MB. A transcribed hour of piano is usually well under 1 MB.

### 🧪 Free local end-to-end test

Test outsourcing without RunPod or R2 accounts. [SeaweedFS](https://github.com/seaweedfs/seaweedfs) stands in for the storage, because MinIO no longer publishes community container images. The handler runs with the RunPod SDK's local test server, on the CPU so it does not take VRAM from the GPU worker. On the CPU, a 4-minute recording takes about 1.5 minutes, so test with short recordings.

1. Start the storage with a test key, and create the bucket:

```powershell
docker network create piano-test
Set-Content -Path s3.json -Value '{"identities":[{"name":"test","credentials":[{"accessKey":"test-access","secretKey":"test-secret"}],"actions":["Admin","Read","Write","List"]}]}' -NoNewline
docker run -d --name seaweed --network piano-test -v "${PWD}\s3.json:/etc/s3.json:ro" `
  chrislusf/seaweedfs server -dir=/data -s3 -s3.port=8333 -s3.config=/etc/s3.json
docker run --rm --network piano-test yt-piano-to-midi-server-api python -c "import boto3; boto3.client('s3', endpoint_url='http://seaweed:8333', region_name='us-east-1', aws_access_key_id='test-access', aws_secret_access_key='test-secret').create_bucket(Bucket='piano-audio')"
```

2. Build and start the handler with the local test server:

```powershell
docker build -f Dockerfile.runpod -t piano-transcription-runpod:local .
docker run -d --name runpod-local --network piano-test `
  -e CUDA_VISIBLE_DEVICES= -e OUTSOURCE_STORAGE_ENDPOINT=http://seaweed:8333 `
  piano-transcription-runpod:local `
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
OUTSOURCE_MAX_WAIT=0s
OUTSOURCE_MAX_CONCURRENCY=1
```

4. Start the stack with the remote worker, and connect it to the test network:

```powershell
docker compose --profile outsource up -d --build
docker network connect piano-test yt-piano-to-midi-server-remote-worker-1
```

With `OUTSOURCE_MAX_WAIT=0s`, a job is outsourced as soon as every local slot is busy: submit one job more than the worker's capacity, which the worker log shows at startup. To send every job remote, stop the GPU worker with `docker compose stop worker`. Without its heartbeat, local capacity counts as zero. The job status shows `"backend": "remote"`, and the bucket is empty again after each job.

The local test server differs from RunPod in two ways:

* It runs one job at a time and blocks all other requests meanwhile, so keep `OUTSOURCE_MAX_CONCURRENCY=1`. With more, the next submission times out and that job falls back to the local queue. Batching across requests only happens on RunPod; `tests/test_runpod_handler.py` checks it locally.
* It runs the handler while the job's status is first requested. Progress therefore jumps from 10% to 96%, and a cancellation takes effect only when the handler returns.

Afterwards, remove the test setup and the outsourcing lines from `.env`:

```powershell
docker rm -f seaweed runpod-local
docker network rm piano-test
```
