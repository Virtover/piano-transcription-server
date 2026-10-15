# 🏗️ Architecture and operations

## 🧩 Services

```text
Client -> FastAPI API -> Redis job state -> Dramatiq worker -> shared data volume
                                             ^
                                             |
                                      cleanup service
```

* `api` accepts requests and uploaded files, and serves status and MIDI files. It checks uploads with `ffprobe`.
* `worker` downloads audio, or reads the uploaded file, and runs the transcription pipeline.
* `cleanup` removes files whose Redis job state has expired.
* `redis` stores job state and queues work.
* The shared `data` volume stores job files. Billing SQLite data and its periodic backups are stored in the host-mounted `billing-data` directory, available only to API and worker.

The main implementation surfaces are `app/main.py`, `app/api/routes/`, `app/worker/`, and `app/transcription/`.

## 🎼 Transcription pipeline

1. Download source audio and metadata with `yt-dlp`, or use the uploaded file.
2. Convert audio to 44.1 kHz mono WAV with `ffmpeg`.
3. Run Transkun in overlapping 16-second segments with an 8-second hop.
4. Write the MIDI file.
5. Serve the result from the API.

The Transkun checkpoint detects sustain-pedal events separately instead of automatically extending note durations until pedal release. Progress during model execution is an estimate because the Transkun command does not expose exact per-segment progress.

## 🗄️ Job retention and restart behavior

Job files are stored under `/data/jobs/<job_id>/`. Queued and processing jobs do not receive a Redis expiration. On API startup, jobs left in those states by a previous session are marked failed, billing reservations are released, and partial files are removed.

Completed and failed jobs receive a one-hour Redis TTL. The cleanup service periodically removes job directories whose Redis keys no longer exist, so files can remain briefly after the TTL expires.

Deleting an account removes the user's Redis jobs and job directories immediately. A worker still processing one of those jobs sees it as cancelled at its next progress check and stops.

### Uploaded files

The API streams an upload into `/data/jobs/<job_id>/upload.input`, stopping as soon as `MAX_UPLOAD_SIZE_MB` is exceeded. During the upload the job has the status `uploading` and a one-hour TTL. That keeps the cleanup service from removing the directory, and lets the cleanup service remove it if the API stops mid-upload. After `ffprobe` accepts the file and the cost is reserved, the job becomes `queued` like any other job.

Uploaded files are untrusted. Only common audio and video container formats are accepted. Playlist and concat formats such as HLS are rejected because they make `ffmpeg` open other files or URLs. `ffprobe` and `ffmpeg` run with `-protocol_whitelist file`, so they cannot open network URLs at all. The worker checks the format again before converting.

Billing backups are pruned by the API after each backup, using `BILLING_BACKUP_RETENTION`.

## ⚠️ Limitations

Transcription quality depends on the source recording. Dense arrangements, multiple instruments, noise, reverb, sustain effects, and ambiguous note offsets can produce incorrect or missing notes. Generated MIDI may need manual cleanup. The service does not provide MIDI editing, sheet-music generation, playback controls, performance feedback, or piano-roll visualization.
