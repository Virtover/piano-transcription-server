"""Benchmark one local Transkun transcription on CPU or CUDA."""

import argparse
import json
import statistics
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("audio", type=Path, help="A representative WAV, FLAC, or other supported audio file.")
    parser.add_argument("--device", choices=("cpu", "gpu"), required=True)
    parser.add_argument("--runs", type=int, default=3, help="Timed runs after the warm-up (default: 3).")
    parser.add_argument("--warmup", action="store_true", help="Run one untimed warm-up transcription first.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.runs < 1:
        raise SystemExit("--runs must be at least 1")
    if not args.audio.is_file():
        raise SystemExit(f"Audio file does not exist: {args.audio}")

    # Set this before importing the transcription module so the normal worker
    # configuration and device-selection path are used.
    from app.config import settings

    settings.worker.enforce_cpu = args.device == "cpu"

    from app.transcription.piano_transcription import select_device, transcribe_piano

    selected_device = select_device()
    if args.device == "gpu" and not selected_device.startswith("cuda:"):
        raise SystemExit("GPU benchmark requested, but no usable CUDA device was detected")
    if args.device == "cpu" and selected_device != "cpu":
        raise SystemExit(f"CPU benchmark requested, but selected device is {selected_device}")

    with tempfile.TemporaryDirectory(prefix="transcription-benchmark-") as directory:
        output_dir = Path(directory)

        def run_once(run_number: int) -> float:
            output_path = output_dir / f"run-{run_number}.mid"
            started = time.perf_counter()
            transcribe_piano(args.audio, output_path)
            elapsed = time.perf_counter() - started
            output_path.unlink()
            return elapsed

        warmup_seconds = run_once(0) if args.warmup else None
        samples = [run_once(run_number) for run_number in range(1, args.runs + 1)]

    result = {
        "device": args.device,
        "selected_device": selected_device,
        "audio": str(args.audio),
        "runs": len(samples),
        "warmup_seconds": warmup_seconds,
        "seconds": {
            "min": min(samples),
            "median": statistics.median(samples),
            "mean": statistics.mean(samples),
            "max": max(samples),
        },
    }
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
