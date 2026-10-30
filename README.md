# 🎹 Piano Transcription Server

[![Tests](https://github.com/Virtover/piano-transcription-server/actions/workflows/tests.yml/badge.svg)](https://github.com/Virtover/piano-transcription-server/actions/workflows/tests.yml)
[![Support on Ko-fi](https://img.shields.io/badge/Support%20on%20Ko--fi-29ABE0?style=flat-square&logo=ko-fi&logoColor=white)](https://ko-fi.com/krzysztofolszak)

> Download a piano performance, transcribe it to MIDI, and retrieve the result through a simple asynchronous API.

Piano Transcription Server accepts a public video URL or an uploaded audio or video file, runs [Transkun](https://github.com/Yujia-Yan/Transkun), and exposes the resulting MIDI file through FastAPI. Redis stores job state and Docker Compose runs the API, worker, cleanup service, and Redis together. The worker uses NVIDIA CUDA when available and falls back to CPU.

## 🚀 Start here

```shell
python scripts/server.py start
```

The first start creates `.env` from `.env.example` and a random PostgreSQL password in `secrets/postgres_password`, builds the images, and waits until the API is ready. Other docs show commands for **Windows (PowerShell)** and for **macOS and Linux** where they differ.

The API is available at `http://localhost:8000`; interactive documentation is at `http://localhost:8000/docs`.

See [Local setup](docs/setup.md) for requirements, GPU checks, health checks, and worker scaling.

Optional features, such as HTTPS, Google Play billing, or outsourcing to RunPod, are set up step by step with `python scripts/setup.py`; see [Setting up optional features](docs/setup.md#-setting-up-optional-features).

> [!NOTE]
> Production billing uses PostgreSQL in `billing-data/postgres` and is backed up every 15 minutes with verified `pg_dump` files in `billing-data/backups`, optionally copied and encrypted off-site. Existing SQLite installations must be migrated explicitly; startup never performs a data migration. See [Billing database backups](docs/configuration.md#-billing-database-backups).

## 📚 Documentation

### 🛠️ Getting started

* [Local setup](docs/setup.md) - run the Docker stack, check services, protect a private server with a password, tune CPU/GPU workers, and free disk space.
* [HTTPS through a Cloudflare Tunnel](docs/cloudflare-tunnel.md) - an HTTPS address for the server without opening ports, turned on from `.env`.
* [Outsourcing to RunPod](docs/runpod.md) - step by step: Cloudflare R2 storage, Docker Hub, the RunPod endpoint, and a free local test.
* [Scaling beyond one machine](docs/scaling.md) - the two ways to run the server, what makes several machines work, and moving a running server.
* [Kubernetes](docs/kubernetes.md) - several machines: API replicas, GPU workers added as the queue grows, managed Redis and PostgreSQL; `python scripts/kube.py`, also for a test cluster on this PC. Tested only on that test cluster so far, not yet on real infrastructure.
* [Administration](docs/administration.md) - server status, jobs, user accounts and credits, the billing report, and temporary limits, with `python scripts/admin.py`.
* [Configuration and environment](docs/configuration.md) - environment variables, GPU memory and batching, secrets, error reports, and database backups.
* [Testing](docs/testing.md) - install development dependencies and run checks.
* Using an AI coding agent? [AGENTS.md](AGENTS.md) has the commands and pitfalls, and Claude Code users can run `/setup-runpod` and `/setup-google` for guided setup. `python -m app.config requirements` prints what each external service must provide, and `python -m app.config check --online` verifies it.

### 🎵 Using the service

* [API guide](docs/api.md) - submit, monitor, cancel, and download transcriptions, plus billing and account deletion endpoints.
* [Google sign-in](docs/google-sign-in.md) - step by step: Google Cloud project, consent screen, and OAuth clients with SHA-1 fingerprints.
* [Google Play billing](docs/google-play-billing.md) - step by step: the Play service account, products, and test purchases.

### ⚙️ Operating and contributing

* [Architecture and operations](docs/architecture.md) - service boundaries, transcription behavior, retention, cleanup, and limitations.

## <img src="docs/images/pianoweave-icon.png" height="24" alt="" /> Piano Weave

The server works with any client that can make HTTP requests and download MIDI files. **[Piano Weave](https://github.com/Virtover/pianoweave)** is an Android app built on it, for learning piano from the songs you love: send a video link, then practice the piano part on a falling-notes piano roll with looping, speed control, and a wait mode that listens to your piano.

[![Piano Weave on GitHub](https://img.shields.io/badge/Piano%20Weave-on%20GitHub-181717?style=flat-square&logo=github&logoColor=white)](https://github.com/Virtover/pianoweave)

## 💬 Community & Support

Found a bug or have a question? [Open an issue](https://github.com/Virtover/piano-transcription-server/issues).

If you find this project useful, support is always appreciated:

<p align="center">
  <a href="https://ko-fi.com/krzysztofolszak">
    <img src="https://ko-fi.com/img/githubbutton_sm.svg" alt="Support me on Ko-fi" />
  </a>
</p>

## 📄 License

Piano Transcription Server is an independent open-source project. See [LICENSE](LICENSE).
