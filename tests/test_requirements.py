import json
from pathlib import Path

from app import config as config_module
from app.requirements import RUNPOD_CUDA_VERSION, build_requirements, format_requirements, requirements_json


ROOT = Path(__file__).resolve().parent.parent


def requirement(config, section_name, item):
    [section] = [section for section in build_requirements(config) if section.name == section_name]
    [found] = [entry for entry in section.requirements if entry.item == item]
    return found


def test_execution_timeout_follows_outsource_timeout(default_settings):
    assert requirement(default_settings, "GPU endpoint", "Execution timeout").value == "at least 20 min"

    default_settings.outsource.timeout = "30m"

    assert requirement(default_settings, "GPU endpoint", "Execution timeout").value == "at least 35 min"


def test_jobs_per_worker_follow_gpu_memory_settings(default_settings):
    assert "about 12 per worker (19 on 24 GB)" in requirement(default_settings, "GPU endpoint", "Scaling").value

    default_settings.worker.memory_per_job_gib = 2.0

    assert "about 7 per worker (11 on 24 GB)" in requirement(default_settings, "GPU endpoint", "Scaling").value


def test_max_workers_follow_max_concurrency(default_settings):
    default_settings.outsource.max_concurrency = 30
    assert requirement(default_settings, "GPU endpoint", "Maximum workers").value == "at least 4"

    default_settings.outsource.max_concurrency = None
    assert "auto" in requirement(default_settings, "GPU endpoint", "Maximum workers").reason


def test_configured_values_replace_placeholders(default_settings):
    assert requirement(default_settings, "Storage", "Bucket").value == "<set STORAGE_BUCKET>"

    default_settings.storage.bucket = "audio"
    default_settings.storage.endpoint = "https://storage.example"
    default_settings.billing.products = {"minutes_60": 60}

    assert requirement(default_settings, "Storage", "Bucket").value == "audio"
    assert requirement(default_settings, "GPU endpoint", "Environment variable").value == (
        "STORAGE_ENDPOINT=https://storage.example"
    )
    assert "minutes_60 (60 min)" in requirement(default_settings, "Google Play", "Products").value


def test_sections_say_whether_they_are_enabled(default_settings):
    sections = {section.name: section for section in build_requirements(default_settings)}
    assert not sections["GPU endpoint"].enabled
    assert "OUTSOURCE_PROVIDER=runpod" in sections["GPU endpoint"].note

    default_settings.outsource.provider = "runpod"
    sections = {section.name: section for section in build_requirements(default_settings)}
    assert sections["GPU endpoint"].enabled


def test_runpod_cuda_version_matches_the_dockerfile():
    dockerfile = (ROOT / "Dockerfile.runpod").read_text()
    wheel_tag = "cu" + RUNPOD_CUDA_VERSION.replace(".", "")

    assert f"download.pytorch.org/whl/{wheel_tag}" in dockerfile
    assert f"FROM nvidia/cuda:{RUNPOD_CUDA_VERSION}." in dockerfile
    assert f"torch.version.cuda.startswith('{RUNPOD_CUDA_VERSION}')" in dockerfile


def test_text_output_marks_checked_requirements(default_settings):
    lines = format_requirements(build_requirements(default_settings))

    assert any(line.endswith("[checked]") for line in lines)
    assert lines[-1].startswith("[checked] = verified by")


def test_cli_prints_requirements_as_json(tmp_path, capsys):
    env_file = tmp_path / ".env"
    env_file.write_text("OUTSOURCE_TIMEOUT=10m\n")

    assert config_module.main(["requirements", "--json", "--env-file", str(env_file), "--secrets-dir", str(tmp_path)]) == 0

    sections = json.loads(capsys.readouterr().out)
    gpu = next(section for section in sections if section["name"] == "GPU endpoint")
    timeout = next(entry for entry in gpu["requirements"] if entry["item"] == "Execution timeout")
    assert timeout == {
        "item": "Execution timeout",
        "value": "at least 15 min",
        "reason": timeout["reason"],
        "checked": True,
    }


def test_cli_reports_invalid_configuration(tmp_path, capsys):
    env_file = tmp_path / ".env"
    env_file.write_text("OUTSOURCE_TIMEOUT=forever\n")

    assert config_module.main(["requirements", "--env-file", str(env_file), "--secrets-dir", str(tmp_path)]) == 1
    assert "OUTSOURCE_TIMEOUT" in capsys.readouterr().err


def test_json_matches_the_sections(default_settings):
    sections = build_requirements(default_settings)

    assert [section["name"] for section in requirements_json(sections)] == [section.name for section in sections]
