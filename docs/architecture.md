# Architecture and operations

## Services

```text
Client -> FastAPI API -> Redis job state -> Dramatiq worker -> shared data volume
                                             ^
                                             |
                                      cleanup service
```

* `api` accepts requests and serves status and MIDI files.
* `worker` downloads audio and runs the transcription pipeline.
* `cleanup` removes files whose Redis job state has expired.
* `redis` stores job state and queues work.
* The shared `data` volume stores job files and the billing SQLite database.

The main implementation surfaces are `app/main.py`, `app/api/routes/`, `app/worker/`, and `app/transcription/`.

## Transcription pipeline

1. Download source audio and metadata with `yt-dlp`.
2. Convert audio to WAV.
3. Run Transkun in overlapping 20-second segments with a 10-second hop.
4. Write the MIDI file.
5. Serve the result from the API.

The Transkun checkpoint detects sustain-pedal events separately instead of automatically extending note durations until pedal release. Progress during model execution is an estimate because the Transkun command does not expose exact per-segment progress.

## Job retention and restart behavior

Job files are stored under `/data/jobs/<job_id>/`. Queued and processing jobs do not receive a Redis expiration. On API startup, jobs left in those states by a previous session are marked failed, billing reservations are released, and partial files are removed.

Completed and failed jobs receive a one-hour Redis TTL. The cleanup service periodically removes job directories whose Redis keys no longer exist, so files can remain briefly after the TTL expires.

## Limitations

Transcription quality depends on the source recording. Dense arrangements, multiple instruments, noise, reverb, sustain effects, and ambiguous note offsets can produce incorrect or missing notes. Generated MIDI may need manual cleanup. The service does not provide MIDI editing, sheet-music generation, playback controls, performance feedback, or piano-roll visualization.
