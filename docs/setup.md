# 🛠️ Local setup

The basic stack runs with Docker alone. The other parts are optional and each needs accounts with outside services:

| Part | Needed for | Guide | Time |
| --- | --- | --- | --- |
| Basic stack | Transcribing on this machine | [Run the stack](#-run-the-stack) | 15 min |
| Server password | A private server reachable from the internet | [Password protection](#-password-protection) | 5 min |
| Cloudflare Tunnel | An HTTPS address without opening ports | [HTTPS through a Cloudflare Tunnel](cloudflare-tunnel.md) | about 20 min, with a domain on Cloudflare |
| Google sign-in | Accounts; required for billing | [Google sign-in](google-sign-in.md) | about 30 min |
| Google Play billing | Paid minutes, free minutes | [Google Play billing](google-play-billing.md) | about 1 hour, plus up to a day for Google permissions |
| RunPod outsourcing | Extra GPUs when this machine is busy | [Outsourcing to RunPod](runpod.md) | about 1 hour |

> [!NOTE]
> Where commands differ by system, the docs show a **Windows (PowerShell)** block and a **macOS and Linux** block (bash or zsh): run the one for yours. All other commands run the same in any terminal. On macOS and Linux, type `python3` where the docs say `python` if `python` is not found.

For the optional parts, `python -m app.config requirements` prints what to create at each provider, calculated from your configuration, and `python -m app.config check --online` verifies it. With an AI coding agent, `/setup-google` and `/setup-runpod` guide you through both.

## 📋 Requirements

* Docker Desktop with Docker Compose and Linux containers enabled.
* NVIDIA drivers and NVIDIA Container Toolkit for GPU transcription, on Windows or Linux. Docker on macOS cannot use the GPU, so the worker runs on the CPU there.
* A public video URL containing an audio or piano performance.

The worker image includes `ffmpeg`, Transkun, and the Python dependencies. Redis and shared job storage are provided by Compose.

## 🚀 Run the stack

```shell
cp .env.example .env
docker compose run --rm api python -m app.config check
docker compose up --build
```

The `check` command validates `.env` and the secrets and prints the resolved configuration with secrets masked. With `--online`, it also tests the configured services with the real credentials; see [Checking the configuration](configuration.md#-checking-the-configuration). Container paths and service addresses are set in `docker-compose.yml`, so `.env` only holds your choices. See [Configuration](configuration.md).

The API is available at `http://localhost:8000` and its interactive OpenAPI documentation is at `http://localhost:8000/docs`.

Check the service state:

```shell
curl http://localhost:8000/health
curl http://localhost:8000/ready
```

In Windows PowerShell 5.1, type `curl.exe`: plain `curl` there is a different command.

Do not commit `.env`, the `secrets` folder, or any machine-specific values.

To update a running server, use `python scripts/server.py restart 5m`: it announces the restart to clients, builds while the current version keeps serving, and lets running jobs finish. `python scripts/server.py stop 10m` shuts the server down the same way. See [Planned restarts and shutdowns](architecture.md#-planned-restarts-and-shutdowns).

## 🔒 Password protection

Optional. Without it, anyone who can reach port 8000 can use the server. For a server that only you, or people you share a password with, use from the internet, create one password and restart the API:

**Windows (PowerShell)**

```powershell
New-Item -ItemType Directory -Force secrets | Out-Null
$bytes = New-Object byte[] 18
[Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
Set-Content -Path secrets\server_password -Value ([Convert]::ToBase64String($bytes)) -NoNewline
Get-Content secrets\server_password
```

**macOS and Linux**

```bash
mkdir -p secrets
openssl rand -base64 18 | tr -d '\n' > secrets/server_password
cat secrets/server_password; echo
```

Then, on any system:

```shell
docker compose run --rm api python -m app.config check
docker compose restart api
```

This writes a random 24-character password and prints it once so you can save it in a password manager. You can also write your own; it must be at least 12 characters, because the server does not slow down repeated guesses. The API reads it when it starts, so restart the API after changing it (`python scripts/server.py restart 1m` while others are using the server). To turn protection off, delete the file and restart.

Every request then needs the password, except `/health` and `/ready`:

* **Browser:** opening `/docs` or any other address asks for a user name and password. The user name can be anything.
* **Apps and scripts:** send the header `X-Server-Password: <password>`. It works next to the `Authorization` header of [Google sign-in](google-sign-in.md).
* **curl:** `curl -u any:<password> ...`, or `-H "X-Server-Password: <password>"`.

Requests without the correct password get `401` with `{"detail": "Server password required"}`.

**Use HTTPS when the server is reachable from the internet.** Over plain `http://`, the password and the transcriptions are readable by anyone on the network path. Put the API behind a reverse proxy or tunnel that terminates HTTPS and forwards to port 8000, and expose only that. The stack includes an optional one: see [HTTPS through a Cloudflare Tunnel](cloudflare-tunnel.md). If the proxy runs on the same machine, set `API_BIND_ADDRESS=127.0.0.1` in `.env` so port 8000 cannot be reached around it.

## 👤 Running as a non-root user

The API, workers, and cleanup service run as user ID `10001`, not root, so a bug in a video download tool or in ffmpeg cannot reach the rest of the container as root.

New installations need nothing extra. An installation from before this change has a `data` volume and a `billing-data` folder that belong to root. The services then refuse to start, and their log names the directory and this command. Run it once, on any system:

```shell
docker compose run --rm --no-deps -u root api chown -R 10001:10001 /data /billing
```

On Linux, this also changes the owner of `billing-data` on the host: its files stay readable, but changing them there, for example to restore a backup, needs `sudo`. Admin commands such as `docker compose exec api python -m app.admin_credit` run as the same user, so files they create stay usable by the services.

## 🎮 GPU and CPU execution

Verify GPU access before starting the stack:

```shell
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

```shell
docker system df
docker image ls
```

**Remove old images and the build cache.** Docker Desktop must be running:

```shell
# One image, for example an old RunPod tag that is already on Docker Hub:
docker image rm <dockerhub-username>/piano-transcription-runpod:<old-tag>

# All images no container uses, including old tags and old base images:
docker image prune -a

# The build cache. The next build downloads PyTorch again.
docker builder prune -a
```

Never add `--volumes` to these commands: the `data` volume holds job files. Billing data is in `billing-data\` on the host and is not affected either way.

On Linux, pruning gives the space back to the disk right away. The rest of this section is for Windows.

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
