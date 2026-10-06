# Local setup

## Requirements

* Docker Desktop with Docker Compose and Linux containers enabled.
* NVIDIA drivers and NVIDIA Container Toolkit for GPU transcription.
* A public video URL containing an audio or piano performance.

The worker image includes `ffmpeg`, Transkun, and the Python dependencies. Redis and shared job storage are provided by Compose.

## Run the stack

```powershell
Copy-Item .env.example .env
docker compose up --build
```

The API is available at `http://localhost:8000` and its interactive OpenAPI documentation is at `http://localhost:8000/docs`.

Check the service state from PowerShell:

```powershell
Invoke-RestMethod http://localhost:8000/health
Invoke-RestMethod http://localhost:8000/ready
```

Do not commit `.env` or any machine-specific values.

## GPU and CPU execution

Verify GPU access before starting the stack:

```powershell
docker run --rm --gpus all nvidia/cuda:12.6.0-cudnn-runtime-ubuntu22.04 nvidia-smi
```

The worker automatically uses CUDA when PyTorch detects a usable NVIDIA GPU. Otherwise it falls back to CPU. CPU execution is significantly slower; on machines without GPU support, remove `gpus: all` from the worker service in `docker-compose.yml`.

By default, CPU mode runs one transcription at a time. GPU capacity is estimated from currently free VRAM using `WORKER_MEMORY_PER_JOB_GIB` (3 GiB by default). See [Configuration](configuration.md) for tuning details.

## Worker scaling

Run multiple worker containers:

```powershell
docker compose up --build --scale worker=2
```

Or configure one worker container with explicit process and thread counts:

```env
WORKER_PROCESSES=1
WORKER_THREADS=4
```

Increase CPU concurrency only after checking RAM usage. The API accepts jobs immediately and Redis queues work beyond available worker capacity.
