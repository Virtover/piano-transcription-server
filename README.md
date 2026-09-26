# Piano Transcription Server

A small FastAPI service that downloads a piano performance, transcribes it to MIDI asynchronously, and returns the resulting MIDI file.

The transcription uses [Transkun](https://github.com/Yujia-Yan/Transkun), a neural audio-to-MIDI transcription model with GPU acceleration through PyTorch/CUDA.

The Transkun checkpoint used by the service detects sustain-pedal events separately rather than extending note durations according to the pedal. This is useful for preserving the distinction between actual key holds and notes sounding under the sustain pedal.

Piano Transcription Server is an independent open-source project.

## Android application

This server can be used independently by any client capable of making HTTP requests and downloading MIDI files.

One client using the server is **PianoWeave**, an Android application for learning piano songs from online videos.

The application uses the server for the computationally intensive transcription process and provides the user-facing learning experience, including:

* submitting online performances for transcription
* monitoring transcription progress
* storing transcribed MIDI files locally
* interactive piano-roll visualization
* MIDI playback with a piano sound
* playback speed control and seeking
* loop sections for practice
* transposition
* wait mode
* MIDI keyboard support

The Android application is maintained as a separate project:

**[PianoWeave](https://github.com/Virtover/pianoweave)**

The server itself does not depend on the Android application and can be integrated with other clients or applications.

## Requirements

* Docker Desktop with Docker Compose and Linux containers enabled
* NVIDIA drivers and NVIDIA Container Toolkit, if using the GPU configuration
* A public video URL containing an audio or piano performance

The transcription worker needs `ffmpeg`, which is included in the worker image.

Redis stores job state, while generated files are stored in the shared `data` volume.

The worker is configured for GPU execution through `gpus: all` in `docker-compose.yml`.

Multiple transcription jobs can run at the same time. By default, the worker automatically chooses concurrency from the available CPU count and CUDA VRAM. On CPU-only systems it can use one job per detected CPU, with no built-in four-job cap. On GPU systems it uses approximately one job per `WORKER_MEMORY_PER_JOB_GIB` GiB of VRAM, also bounded by the available CPU count. Configure `WORKER_PROCESSES` and `WORKER_THREADS` in `.env`; each can be `auto` or an explicit positive integer.

Verify that Docker can access the GPU before starting the stack:

```powershell
docker run --rm --gpus all nvidia/cuda:12.6.0-cudnn-runtime-ubuntu22.04 nvidia-smi
```

The worker automatically selects CUDA when PyTorch detects an available NVIDIA GPU and otherwise falls back to CPU.

CPU execution is significantly slower and may require removing `gpus: all` from the worker service if GPU support is not available.

## Run locally with Docker

```powershell
Copy-Item .env.example .env

docker compose up --build
```

The included `.env.example` is already configured for Docker Compose.

Keep:

```env
REDIS_URL=redis://redis:6379/0

DATA_DIR=/data

WORKER_PROCESSES=auto

WORKER_THREADS=auto

WORKER_MAX_CONCURRENCY=auto
```

unless the Compose configuration is changed to use different service or volume settings.

For more parallel jobs, run multiple worker containers. For example, this runs two independently auto-sized workers:

```powershell
docker compose up --build --scale worker=2
```

To force four threads in one process instead:

```env
WORKER_PROCESSES=1

WORKER_THREADS=4
```

You can also tune the automatic GPU estimate with `WORKER_MEMORY_PER_JOB_GIB`; its default is `8`. Capacity is calculated independently for each visible GPU, so GPUs with different VRAM sizes are handled correctly. Each transcription chooses the visible GPU with the most free memory when it starts. Set `WORKER_MAX_CONCURRENCY` to an explicit value when you want an operational safety limit; leave it as `auto` to use all detected capacity. The API accepts jobs immediately and Redis queues any jobs beyond the available worker capacity.

Do not commit local secrets or machine-specific values from `.env`.

The API is available at:

`http://localhost:8000`

Interactive API documentation:

`http://localhost:8000/docs`

Check service health:

```powershell
Invoke-RestMethod http://localhost:8000/health

Invoke-RestMethod http://localhost:8000/ready
```

## API

### Create a transcription

```powershell
$job = Invoke-RestMethod `
  -Method Post `
  -Uri http://localhost:8000/api/transcriptions `
  -ContentType 'application/json' `
  -Body '{"source_url":"https://example.com/video"}'

$job
```

The endpoint returns `202 Accepted` with a `job_id` and an initial `queued` status.

The URL must be an HTTP(S) URL.

The worker downloads the audio, retrieves the source video title, and runs Transkun. Processing time depends on the track length and available hardware.

### Poll status

```powershell
Invoke-RestMethod `
  "http://localhost:8000/api/transcriptions/$($job.job_id)"
```

The status response contains:

* `job_id`
* `status`
* `progress` from `0` to `1`
* `title` — the source video title, once retrieved
* `metadata` with the available author, channel, upload date, duration, thumbnail, URL, view count, and like count
* `error` when processing fails

Example:

```json
{
  "job_id": "c203c265-fac5-467e-bab9-53321a1f4488",
  "status": "processing",
  "progress": 0.42,
  "title": "River Flows in You - Yiruma",
  "metadata": {
    "title": "River Flows in You - Yiruma",
    "author": "Piano Channel",
    "channel": "Piano Channel",
    "channel_id": "UC...",
    "channel_url": "https://example.com/channel/UC...",
    "upload_date": "2026-01-25",
    "duration": 245.0,
    "thumbnail": "https://example.com/thumbnail.jpg",
    "webpage_url": "https://example.com/video",
    "view_count": 12345,
    "like_count": 321
  },
  "error": null
}
```

Metadata is retrieved during the audio download and may therefore become available while the transcription is still processing. Fields unavailable in the source metadata are returned as `null`.

Possible statuses are:

* `queued`
* `processing`
* `completed`
* `failed`
* `cancelled`

### Cancel a transcription

Cancel a queued or processing job with:

```powershell
Invoke-RestMethod `
  -Method Delete `
  -Uri "http://localhost:8000/api/transcriptions/$($job.job_id)"
```

The endpoint returns the `cancelled` status. The worker stops active audio download, conversion, and transcription work as soon as the current subprocess or model segment can be interrupted, and removes partial job files. Cancelling a completed or failed job returns `409`.

Poll until the status is `completed`, `failed`, or `cancelled`:

```powershell
do {
    $status = Invoke-RestMethod `
        "http://localhost:8000/api/transcriptions/$($job.job_id)"

    $status

    if ($status.status -in @('completed', 'failed', 'cancelled')) {
        break
    }

    Start-Sleep -Seconds 2
} while ($true)
```

A completed response looks like:

```json
{
  "job_id": "c203c265-fac5-467e-bab9-53321a1f4488",
  "status": "completed",
  "progress": 1.0,
  "title": "River Flows in You - Yiruma",
  "error": null
}
```

Progress is reported by the transcription pipeline. The Transkun command itself does not currently expose exact per-segment progress, so the progress reported while Transkun is running is an estimate rather than an exact measure of completed model computation.

### Download MIDI

After the status is `completed`:

```powershell
Invoke-WebRequest `
  -Uri "http://localhost:8000/api/transcriptions/$($job.job_id)/midi" `
  -OutFile transcription.mid
```

The download endpoint returns:

* `409` while transcription is still processing
* `404` for an unknown job or missing result
* `500` if the stored result metadata is invalid

The generated MIDI contains both detected note events and detected MIDI control-change events, including sustain-pedal events (`CC64`) when detected by the model.

Completed and failed jobs are retained for a limited time and are then automatically removed.

## Configuration

Settings are read from environment variables or `.env`:

| Variable    | Default                    | Description                    |
| ----------- | -------------------------- | ------------------------------ |
| `REDIS_URL` | `redis://localhost:6379/0` | Redis connection URL           |
| `DATA_DIR`  | `/data`                    | Directory for job output files |

Docker Compose overrides these values to use the Redis service and the shared `/data` volume.

The API, worker, and cleanup service must use the same `DATA_DIR`.

## Transcription

The transcription pipeline consists of:

1. Downloading the source audio using `yt-dlp`
2. Extracting the source video title
3. Converting the audio to WAV
4. Running Transkun
5. Writing the resulting MIDI file
6. Returning the MIDI through the API

The worker uses CUDA when available:

```text
NVIDIA GPU
    │
    ▼
PyTorch / CUDA
    │
    ▼
Transkun
    │
    ▼
MIDI
```

When CUDA is unavailable, the transcription falls back to CPU.

Transkun processes the audio in overlapping segments. The current configuration uses a 20-second segment size with a 10-second hop.

The resulting MIDI can contain:

* piano note events
* note velocities
* note onset and offset information
* sustain-pedal events (`CC64`)
* other detected MIDI control events

The Transkun checkpoint used by the service is intended to keep sustain-pedal events separate from note durations. Therefore, a note sounding while the sustain pedal is held should not automatically become a long MIDI note whose duration extends until pedal release.

## Job cleanup

Job state is stored in Redis and generated files are stored under:

```text
/data/jobs/<job_id>/
```

Jobs are not given a Redis expiration while they are queued or being processed. This prevents a long-running transcription from disappearing simply because no progress update occurred for some time.

When a transcription finishes successfully or fails, its Redis key receives a one-hour TTL.

A separate cleanup service periodically scans the jobs directory. If the corresponding Redis key no longer exists, the cleanup service removes the job directory and its generated files.

The cleanup process runs independently from the transcription worker.

The cleanup interval is currently 10 minutes, so files may remain for a short period after their one-hour retention period expires.

## Development checks

Run the syntax check from the repository root:

```powershell
python -m compileall -q app
```

Install `requirements.api.txt` for API-only development or `requirements.worker.txt` for transcription-worker development.

Running the complete stack with Docker is recommended because the worker requires:

* `ffmpeg`
* Redis
* Transkun
* PyTorch
* CUDA support when using the GPU configuration

## Architecture

```text
                    ┌──────────────┐
                    │    Client    │
                    └──────┬───────┘
                           │ HTTP
                           ▼
                    ┌──────────────┐
                    │   FastAPI    │
                    │     API      │
                    └──────┬───────┘
                           │
                           │ enqueue
                           ▼
                    ┌──────────────┐
                    │    Redis     │
                    │   job state  │
                    └──────┬───────┘
                           │
                           │ Dramatiq
                           ▼
                    ┌──────────────┐
                    │   Worker     │
                    │ Transkun/CUDA│
                    └──────┬───────┘
                           │
                           ▼
                    ┌──────────────┐
                    │ Shared data  │
                    │    volume    │
                    └──────────────┘
                           ▲
                           │
                    ┌──────┴───────┐
                    │   Cleanup    │
                    │   service    │
                    └──────────────┘
```

### Components

* `app/main.py` — FastAPI application and health endpoints
* `app/api/routes/transcriptions.py` — job creation, status polling, and MIDI download
* `app/worker/tasks.py` — Dramatiq transcription task and Redis job state
* `app/worker/cleanup.py` — periodic cleanup process
* `app/transcription/pipeline.py` — source audio download, metadata retrieval, and transcription pipeline
* `app/transcription/piano_transcription.py` — Transkun integration
* `docker-compose.yml` — API, worker, cleanup, Redis, and shared storage
* `Dockerfile.api` — API container image
* `Dockerfile.worker` — CUDA-enabled transcription worker image
* `app/worker/launcher.py` — resource-aware Dramatiq process and thread startup

## Limitations

This is an asynchronous transcription service, not a sheet-music editor.

Transcription quality depends heavily on the source recording. Dense arrangements, multiple instruments, background noise, sustain-pedal effects, reverberation, and ambiguous note offsets can result in incorrect or missing notes.

In particular, note onset detection and note offset detection are not equally reliable. Some notes may be detected with durations that are shorter or longer than the performed notes.

The generated MIDI may therefore require manual cleanup before being used as a final piano arrangement.

The current API accepts a source video URL and returns a MIDI file. It also exposes the source video title through the transcription status endpoint.

It does not yet provide:

* interactive MIDI editing
* sheet-music generation
* playback controls
* MIDI performance feedback
* automatic correction of transcription errors
* piano-roll visualization
