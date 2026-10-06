# Testing

The test suite uses an in-memory Redis substitute and temporary SQLite databases. It does not require Docker Compose, a GPU, or Google Play credentials.

Create a virtual environment and install the test dependencies:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.test.txt
```

Run the tests:

```powershell
python -m pytest -q
```

Run the application syntax check:

```powershell
python -m compileall -q app
```

For API-only development install `requirements.api.txt`; for transcription-worker development install `requirements.worker.txt`. Running the complete stack with Docker remains the recommended way to exercise the worker because it depends on `ffmpeg`, Redis, Transkun, PyTorch, and optional CUDA support.
