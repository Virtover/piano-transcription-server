# 🎹 Piano Transcription Server

> Download a piano performance, transcribe it to MIDI, and retrieve the result through a simple asynchronous API.

Piano Transcription Server accepts a public video URL, downloads its audio, runs [Transkun](https://github.com/Yujia-Yan/Transkun), and exposes the resulting MIDI file through FastAPI. Redis stores job state and Docker Compose runs the API, worker, cleanup service, and Redis together. The worker uses NVIDIA CUDA when available and falls back to CPU.

## 🚀 Start here

```powershell
Copy-Item .env.example .env

docker compose up --build
```

The API is available at `http://localhost:8000`; interactive documentation is at `http://localhost:8000/docs`.

See [Local setup](docs/setup.md) for requirements, GPU checks, health checks, and worker scaling.

## 📚 Documentation

### 🛠️ Getting started

* [Local setup](docs/setup.md) - run the Docker stack, check services, and tune CPU/GPU workers.
* [Configuration and environment](docs/configuration.md) - environment variables, shared volumes, and secrets.
* [Testing](docs/testing.md) - install development dependencies and run checks.

### 🎵 Using the service

* [API guide](docs/api.md) - submit, monitor, cancel, and download transcriptions, plus billing endpoints.
* [Google Play billing](docs/google-play-billing.md) - configure Google Cloud, Play Console, OAuth, service accounts, and product synchronization.

### ⚙️ Operating and contributing

* [Architecture and operations](docs/architecture.md) - service boundaries, transcription behavior, retention, cleanup, and limitations.

## 🎼 Piano Weave

The server is independent of any client that can make HTTP requests and download MIDI files. One client is [Piano Weave](https://github.com/Virtover/pianoweave), an Android application for learning piano songs from online videos.

## 📄 License

Piano Transcription Server is an independent open-source project. See [LICENSE](LICENSE).
