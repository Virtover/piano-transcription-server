"""build/apt_install.sh: an apt-get install that hangs is ended and tried again; one that keeps failing fails the
build after three attempts. Runs the script with a fake apt-get; needs a POSIX shell and coreutils' timeout."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "build" / "apt_install.sh"

pytestmark = pytest.mark.skipif(os.name == "nt" or not shutil.which("timeout"), reason="needs a POSIX shell and timeout")


def run(tmp_path, fake_body: str) -> tuple[subprocess.CompletedProcess, int]:
    fake = tmp_path / "bin"
    fake.mkdir()
    calls = tmp_path / "calls"
    (fake / "apt-get").write_text(
        "#!/bin/sh\n"
        f'n=$(cat {calls} 2>/dev/null || echo 0); n=$((n+1)); echo $n > {calls}\n'
        f"{fake_body}\n"
    )
    (fake / "apt-get").chmod(0o755)
    env = {**os.environ, "PATH": f"{fake}:{os.environ['PATH']}", "APT_INSTALL_TIMEOUT": "2"}
    result = subprocess.run(["sh", str(SCRIPT), "python3"], env=env, capture_output=True, text=True, timeout=60)
    return result, int(calls.read_text())


def test_a_hung_install_is_ended_and_tried_again(tmp_path):
    result, calls = run(tmp_path, '[ "$n" -eq 1 ] && exec sleep 300\nexit 0')
    assert result.returncode == 0
    assert calls == 2


def test_an_install_that_keeps_failing_fails_after_three_attempts(tmp_path):
    result, calls = run(tmp_path, "exit 100")
    assert result.returncode == 1
    assert calls == 3
    assert "failed three times" in result.stderr
