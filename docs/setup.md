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
| Error reports | Server errors sent to a website that collects them | [Error reports](configuration.md#error-reports) | 5 min, with a website that accepts them |

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
python scripts/server.py start
docker compose run --rm api python -m app.config check
```

On a new installation, `start` creates `.env` from `.env.example`, a random PostgreSQL password in `secrets/postgres_password`, and random [Redis passwords](architecture.md#-redis-users) in `secrets/redis`; it never prints a password. It does not change these files once they exist, and `start` and `restart` create one that an update adds. It then builds the images and waits until the API is ready. For an older NVIDIA GPU, such as a GTX 1050 Ti or another GTX 10 series card, add `--legacy-gpu`, or set `WORKER_GPU_BUILD=legacy` in `.env`; see [`WORKER_GPU_BUILD`](configuration.md). After changing `.env`, run `docker compose up -d` to apply it.

The `check` command validates `.env`, `server.env`, and the secrets and prints the resolved configuration with secrets masked. With `--online`, it also tests the configured services with the real credentials; see [Checking the configuration](configuration.md#-checking-the-configuration). Container paths and service addresses are set in `docker-compose.yml`, so `.env` only holds your choices. See [Configuration](configuration.md).

The API is available at `http://localhost:8000` and its interactive OpenAPI documentation is at `http://localhost:8000/docs`.

Check the service state:

```shell
curl http://localhost:8000/health
curl http://localhost:8000/ready
```

In Windows PowerShell 5.1, type `curl.exe`: plain `curl` there is a different command.

`/health` answers while the API process runs. `/ready` also checks that Redis and the billing database can be reached, and answers `503` with the one that cannot.

Do not commit `.env`, `server.env`, the `secrets` folder, or any machine-specific values.

To update a running server, use `python scripts/server.py restart 5m`: it announces the restart to clients, builds while the current version keeps serving, and lets running jobs finish. `python scripts/server.py stop 10m` shuts the server down the same way. See [Planned restarts and shutdowns](architecture.md#-planned-restarts-and-shutdowns).

## 🧩 Setting up optional features

The stack runs as above with every optional feature off: no sign-in, no billing, job files on this machine. Features such as HTTPS through a Cloudflare Tunnel, Google Play billing, or outsourcing to RunPod are turned on in `server.env` and `secrets`, and the tunnel and admin page also in `.env`; [Optional features](configuration.md#-optional-features) lists them, with what each needs. A guided setup does it step by step:

```shell
python scripts/setup.py                   # what is on, then a menu
python scripts/setup.py status            # what is on and what is missing
python scripts/setup.py outsourcing       # set one feature up, test it, and fix what fails
python scripts/setup.py check [feature]   # test what is on with the real services, and fix what fails
python scripts/setup.py fix               # repair the env files, the secret files, and the settings
python scripts/setup.py set [NAME]        # change any one setting or secret file
python scripts/setup.py off outsourcing   # turn one off
```

None of it needs a file edited by hand.

For a feature, it:

1. **sets up what it needs first**, such as object storage for outsourcing, and offers to turn off what it cannot be on with;
2. **lists what external services must provide**, from `python -m app.config requirements`, as what is needed and why. Where each is set at a provider changes, so look it up in the provider's current documentation, or give the list to an AI agent;
3. **asks for the settings** with their descriptions from [Configuration](configuration.md), checks each value as the server will read it, asking again for one it would refuse, and writes them into `server.env` or `.env`, where the example files have them, keeping the comments;
4. **asks for the secrets** without showing them, or generates a random one where any value works, and writes them to `secrets`, without the byte order mark or line ends some editors and shells add; a downloaded JSON key must be valid JSON;
5. **checks the result** with the server's own validation, and asks again for what each problem names;
6. **tests the external services** with the real settings and secrets, as `check --online` does, such as uploading to the bucket and downloading through a signed link. When a test fails, it shows the likely fix and asks again for what that names, such as `secrets/storage_access_key`; or tests again after a change at the provider; or keeps the setup as it is; or undoes it. The tunnel and the admin page are tested once the server runs with them, after an announced restart it offers;
7. **offers to apply it** with an announced restart, when the server runs.

A setup that does not finish, because something is still missing or because you stop it with Ctrl+C, puts `server.env` and `.env` back as they were: a feature that was off is off again, and one you were changing keeps its previous settings. The secret files you gave stay for the next try, except one whose presence alone turns the feature on, such as `server_password`. What it set up first because the feature needs it, such as object storage for outsourcing, stays set up.

`check` tests a feature that is already on, or every one, the same way, and asks again for what fails. `set` changes any one setting or secret file, found by part of its name or a word of what it does, checked the same way, and undoes the change if the server would not start with it; with Kubernetes, it also changes the cluster's own choices in `deploy/kubernetes/cluster.env`, checked as `scripts/kube.py` checks them. The passwords that `scripts/server.py start` creates, and the hub bucket's keys, are not offered: scripts write them.

`fix` repairs what keeps the configuration from being read or the server from starting:

* in `server.env` and `.env`: a byte order mark before the first setting, lines that are no setting, settings written twice (the earlier lines are commented out, so what applies does not change), a setting name with a typo, a deprecated name, which it renames with its value converted, and a setting in the wrong one of the two files;
* in `secrets`: a folder where a file belongs, as Docker creates for a missing file; a file in UTF-16, as Windows PowerShell's `>` writes it, or with a byte order mark, Windows line ends, or spaces around the value; a file the services cannot read; one named almost as one the server reads, such as `storage_access_key.txt`; one under an old name; and a secret file git keeps;
* every other error, by asking again for the settings and secret files it names.

Then it offers to test every feature that is on. Secret values are never shown. `status` says when the configuration cannot be read at all, and the menu offers `fix` then.

Apply the changes with `python scripts/server.py restart 1m`, or `start` when the stack is stopped: settings and secrets are read when the services start. The questions go to one container of the API's image, which reads `.env`, `server.env`, and `secrets` from this folder as they change, so each answer takes a fraction of a second; with an image from before it, each question starts a container of its own. The script needs only Python 3 and the docker CLI. It asks the server itself what the features are, after bringing the API image up to the code, which takes seconds when nothing changed, so it changes with the code. `status` also says, for each feature, whether Kubernetes needs it, can use it, or cannot run it; see [Kubernetes](kubernetes.md). It names what each feature that is on still misses: a secret file by its place in `secrets`, a setting by its name in `server.env`, or another problem. In a terminal it shows them in color; `NO_COLOR=1` turns colors off.

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

The API, workers, and cleanup service run as user ID `10001`, not root, so a bug in a video download tool or in ffmpeg cannot reach the rest of the container as root. They also have no Linux capabilities and cannot gain privileges through a setuid program (`no-new-privileges`); the cleanup service's files are read-only except `/data`. Redis and PostgreSQL start as root only to switch to their own user, and cannot gain privileges after that. The workers, which download and decode untrusted media, cannot see the Cloudflare tunnel token: their copy of `secrets/` has `secrets/cloudflare` covered by an empty folder.

Nothing needs to be done by hand. Docker creates a missing `billing-data` folder as root, and volumes from older versions belong to root, so before the other services start, the `prepare` service gives `/data` and `billing-data` to user `10001` and exits. It runs as root without a network or secrets, keeps only root's powers to change owners and modes, and never touches the PostgreSQL cluster in `billing-data/postgres`. If a service still reports a directory it cannot write, run `docker compose run --rm prepare` and read its output.

On Linux, the folders in `billing-data` keep the group of the user who ran `python scripts/server.py start` and are writable by it, so you can copy, move, or delete backups without `sudo`. The PostgreSQL cluster stays readable only by PostgreSQL.

Admin commands such as `python scripts/admin.py` run as the same user, so files they create stay usable by the services.

## 🎮 GPU and CPU execution

Verify GPU access before starting the stack:

```shell
docker run --rm --gpus all nvidia/cuda:12.8.1-base-ubuntu22.04 nvidia-smi
```

The worker automatically uses CUDA when PyTorch detects a usable NVIDIA GPU. Otherwise it falls back to CPU. CPU execution is significantly slower; on machines without GPU support, remove `gpus: all` from the worker service in `docker-compose.yml`.

By default, one worker process shares one model and batches requests. GPU capacity is estimated from total VRAM, and each job starts only while the VRAM other programs leave holds it, using `WORKER_GPU_RESERVE_GIB` (`auto`: `1.0` GiB reserved for the models and GPU libraries, `1.25` with [ensemble transcriptions](architecture.md#-ensemble-transcriptions)) and `WORKER_MEMORY_PER_JOB_GIB` (`1.1` GiB for each piano job). See [GPU memory and batching](configuration.md#-gpu-memory-and-batching) for measurements and tuning. To measure this machine's actual CPU/GPU performance, see [Benchmarking CPU versus GPU](testing.md#-benchmarking-cpu-versus-gpu).

## 📈 Worker scaling

Configure the shared worker with explicit CPU compute threads and request batching:

```env
WORKER_THREADS=4
WORKER_BATCH_SIZE=auto
WORKER_BATCH_TIMEOUT=2s
```

With `WORKER_BATCH_SIZE=auto`, each GPU batches as many jobs as fit in its VRAM, and the CPU batch size is estimated from physical CPU threads and available RAM. The API accepts jobs immediately and Redis queues work beyond available worker capacity. If a batch runs out of GPU memory, the worker splits it and retries. See [WORKER_BATCH_SIZE](configuration.md#choosing-other-values).


## ☁️ Outsourcing to RunPod

Optional. When the local GPU is busy, jobs can run on RunPod Serverless GPUs instead, billed per second. Only the model runs there; this server still downloads and converts the audio. It needs accounts for S3-compatible storage, a container registry, and RunPod.

See [Outsourcing to RunPod](runpod.md). It also includes a free local test that needs none of these accounts.

## 🌍 Download sources of a build

A build downloads from several services, and each has copies elsewhere that the build turns to when it fails. A copy is used only after the original failed, and the build log says which.

| What | First | Then |
| --- | --- | --- |
| Ubuntu and Debian packages | The distribution's own servers | Mirrors in the US, Germany, Japan, and Australia (`build/apt/`). apt checks every package against the distribution's signature |
| deno and model weights | GitHub, and the weights' authors | Deno's own server, and copies of the weights on Hugging Face and the Internet Archive. Only a file with a checksum in the Dockerfile is kept (`build/fetch.py`) |
| Python packages | PyPI, and PyTorch's index for torch | University and company mirrors in South Korea and China (`build/pip_install.py`) |
| Base and service images | Docker Hub | Google's copy of Docker Hub, once Docker is set up for it (below) |

**Images.** Every image the server uses is on Docker Hub. Docker itself can take them from Google's copy of Docker Hub, `mirror.gcr.io`, and from Docker Hub when the copy lacks one or fails: `registry-mirrors` in Docker's own settings, once per machine. Builds, `docker pull`, and Compose all use it.

**Docker Desktop (Windows, macOS):** in Docker Desktop's settings for the Docker Engine, add the line to the JSON, keeping what is there, and apply:

```json
"registry-mirrors": ["https://mirror.gcr.io"]
```

**Linux:** add it to `/etc/docker/daemon.json`. The file may already hold settings, such as NVIDIA's runtime for the GPU, which must stay, so add to it rather than replace it:

```shell
sudo cp -a /etc/docker/daemon.json /etc/docker/daemon.json.bak 2>/dev/null
sudo python3 -c "
import json, os
path = '/etc/docker/daemon.json'
config = json.load(open(path)) if os.path.exists(path) and os.path.getsize(path) else {}
mirrors = config.setdefault('registry-mirrors', [])
if 'https://mirror.gcr.io' not in mirrors:
    mirrors.append('https://mirror.gcr.io')
json.dump(config, open(path, 'w'), indent=2)
"
```

Docker reads the setting when it starts, and restarting Docker stops every container. Stop the server first (`python scripts/server.py stop 1m`, or `python scripts/server.py stop` on a node), restart Docker (`sudo systemctl restart docker` on Linux; Docker Desktop restarts itself), check, and start the server again:

```shell
docker info --format "{{json .RegistryConfig.Mirrors}}"
python scripts/server.py start
```

Python packages are not signed: a mirror is trusted as much as PyPI. Only mirrors run by universities and large companies are listed.

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
