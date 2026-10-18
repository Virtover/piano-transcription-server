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
WORKER_BATCH_SIZE=6
WORKER_BATCH_TIMEOUT=2s
```

Increase the batch size only after checking RAM or VRAM usage. The API accepts jobs immediately and Redis queues work beyond available worker capacity. `WORKER_BATCH_SIZE` controls model batching; it does not replace the per-job VRAM estimate used for GPU capacity.
