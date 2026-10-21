# 🛠️ Local setup

The basic stack runs with Docker alone. The other parts are optional and each needs accounts with outside services:

| Part | Needed for | Guide | Time |
| --- | --- | --- | --- |
| Basic stack | Transcribing on this machine | [Run the stack](#-run-the-stack) | 15 min |
| Google sign-in | Accounts; required for billing | [Google sign-in](google-sign-in.md) | about 30 min |
| Google Play billing | Paid minutes, free minutes | [Google Play billing](google-play-billing.md) | about 1 hour, plus up to a day for Google permissions |
| RunPod outsourcing | Extra GPUs when this machine is busy | [Outsourcing to RunPod](runpod.md) | about 1 hour |

For the optional parts, `python -m app.config requirements` prints what to create at each provider, calculated from your configuration, and `python -m app.config check --online` verifies it. With an AI coding agent, `/setup-google` and `/setup-runpod` guide you through both.

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

The `check` command validates `.env` and the secrets and prints the resolved configuration with secrets masked. With `--online`, it also tests the configured services with the real credentials; see [Checking the configuration](configuration.md#-checking-the-configuration). Container paths and service addresses are set in `docker-compose.yml`, so `.env` only holds your choices. See [Configuration](configuration.md).

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
docker run --rm --gpus all nvidia/cuda:12.8.1-base-ubuntu22.04 nvidia-smi
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

Optional. When the local GPU is busy, jobs can run on RunPod Serverless GPUs instead, billed per second. Only the model runs there; this server still downloads and converts the audio. It needs accounts for S3-compatible storage, a container registry, and RunPod.

See [Outsourcing to RunPod](runpod.md). It also includes a free local test that needs none of these accounts.

## 🧹 Freeing disk space

Each build of the worker or RunPod image is 7 to 8 GB, and Docker keeps every old image and its build cache. On Windows, all of it lives in one virtual disk file, `%LOCALAPPDATA%\Docker\wsl\disk\docker_data.vhdx`, which grows but never shrinks on its own. When drive C: fills up, builds stop with `error reading from server: EOF` and Docker Desktop reports **Docker Desktop is unable to start**.

**See what uses the space:**

```powershell
docker system df
docker image ls
```

**Remove old images and the build cache.** Docker Desktop must be running:

```powershell
# One image, for example an old RunPod tag that is already on Docker Hub:
docker image rm <dockerhub-username>/piano-transcription-runpod:<old-tag>

# All images no container uses, including old tags and old base images:
docker image prune -a

# The build cache. The next build downloads PyTorch again.
docker builder prune -a
```

Never add `--volumes` to these commands: the `data` volume holds job files. Billing data is in `billing-data\` on the host and is not affected either way.

**Give the space back to Windows.** After pruning, Docker reuses the freed space, but the `.vhdx` file keeps its size. To shrink it, quit Docker Desktop, then in an **administrator** PowerShell:

```powershell
wsl --shutdown
diskpart
```

and in `diskpart`:

```text
select vdisk file="C:\Users\<you>\AppData\Local\Docker\wsl\disk\docker_data.vhdx"
attach vdisk readonly
compact vdisk
detach vdisk
exit
```

**If Docker Desktop no longer starts** because the disk is full, free a few GB first, for example by emptying `%TEMP%`, then restart it:

```powershell
Remove-Item "$env:TEMP\*" -Recurse -Force -ErrorAction SilentlyContinue
Get-Process "Docker Desktop", "com.docker.*" -ErrorAction SilentlyContinue | Stop-Process -Force
wsl --shutdown
Start-Process "C:\Program Files\Docker\Docker\Docker Desktop.exe"
```

`wsl --shutdown` also stops any other WSL distributions that are running.
