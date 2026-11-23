"""Every download of an image build, and every image, has a source to fall
back on: build/ for what the Dockerfiles download, scripts/stacks.py for the
images themselves."""

import hashlib
import importlib.util
import json
import re
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
DOCKERFILES = sorted(ROOT.glob("Dockerfile*"))


def load(path: Path):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


fetch = load(ROOT / "build" / "fetch.py")
pip_install = load(ROOT / "build" / "pip_install.py")
stacks = load(ROOT / "scripts" / "stacks.py")


# --- fetch.py ---------------------------------------------------------------

def test_fetch_keeps_the_first_file_with_the_checksum(tmp_path):
    good = b"weights"
    tried = []

    def download(url, out):
        tried.append(url)
        if url == "https://down.example":
            raise OSError("connection refused")
        data = b"tampered" if url == "https://wrong.example" else good
        Path(out).write_bytes(data)
        return hashlib.sha256(data).hexdigest()

    out = tmp_path / "file"
    urls = ["https://down.example", "https://wrong.example", "https://good.example", "https://unused.example"]

    assert fetch.fetch(urls, hashlib.sha256(good).hexdigest(), str(out), download) == "https://good.example"
    assert out.read_bytes() == good
    assert tried == urls[:3]
    assert list(tmp_path.iterdir()) == [out]


def test_fetch_takes_any_of_the_published_checksums(tmp_path):
    builds = {"https://github.example": b"build one", "https://own.example": b"build two"}

    def download(url, out):
        Path(out).write_bytes(builds[url])
        return hashlib.sha256(builds[url]).hexdigest()

    sums = [hashlib.sha256(data).hexdigest().upper() for data in builds.values()]
    assert fetch.fetch(["https://own.example"], sums, str(tmp_path / "file"), download) == "https://own.example"
    assert fetch.main(["--sha256", "0" * 64, "--sha256", "1" * 64, "--out", str(tmp_path / "x"), "https://a.invalid/"]) == 1


def test_fetch_keeps_nothing_when_no_source_matches(tmp_path):
    def download(url, out):
        Path(out).write_bytes(b"other")
        return hashlib.sha256(b"other").hexdigest()

    assert fetch.fetch(["https://a.example", "https://b.example"], "0" * 64, str(tmp_path / "file"), download) is None
    assert list(tmp_path.iterdir()) == []


# --- pip_install.py ---------------------------------------------------------

def fake_pip(results):
    calls = []

    def run(arguments):
        calls.append(arguments)
        return results[len(calls) - 1]
    return calls, run


def test_pip_moves_to_the_next_index_when_one_fails():
    calls, run = fake_pip([(1, "ERROR: Could not install: ConnectionError"), (1, "Read timed out"), (0, "")])

    assert pip_install.install(["-r", "requirements.txt"], pip_install.PYPI, run) == 0
    assert [call[1] for call in calls] == [urls[0] for urls in pip_install.PYPI[:3]]
    assert all(call[-2:] == ["-r", "requirements.txt"] for call in calls)


@pytest.mark.parametrize("output", ["ERROR: ResolutionImpossible: ...", "ERROR: Failed to build installable wheels for ncls",
                                    "OSError: [Errno 28] No space left on device"])
def test_pip_stays_when_every_index_would_fail_the_same(output):
    calls, run = fake_pip([(1, output)])

    assert pip_install.install(["x==1"], pip_install.PYPI, run) == 1
    assert len(calls) == 1


def test_pip_fails_when_every_index_failed():
    calls, run = fake_pip([(1, "No matching distribution found")] * len(pip_install.PYPI))

    assert pip_install.install(["x==1"], pip_install.PYPI, run) == 1
    assert len(calls) == len(pip_install.PYPI)


def test_torch_comes_from_pytorch_or_its_mirrors():
    indexes = pip_install.torch_indexes("cu128")

    assert indexes[0] == ["https://download.pytorch.org/whl/cu128"]
    assert len(indexes) >= 3
    assert all(urls[0].endswith("/cu128") for urls in indexes)
    # A PyPI mirror only beside a PyTorch mirror, never in its place.
    assert pip_install.index_arguments(indexes[2])[:2] == ["--index-url", indexes[2][0]]


def test_pip_install_chooses_the_indexes_itself():
    assert pip_install.main(["--index-url", "https://elsewhere.example/simple", "x"]) == 2


def test_pypi_and_its_mirrors_are_on_different_networks():
    hosts = [re.match(r"https://([^/]+)/", urls[0]).group(1) for urls in pip_install.PYPI]

    assert hosts[0] == "pypi.org"
    assert len(set(hosts)) == len(hosts) >= 3


# --- apt mirror lists --------------------------------------------------------

def mirror_list(name):
    entries = []
    for line in (ROOT / "build" / "apt" / name).read_text(encoding="utf-8").splitlines():
        if line and not line.startswith("#"):
            uri, metadata = line.split("\t")
            entries.append((uri, int(metadata.removeprefix("priority:"))))
    return entries


@pytest.mark.parametrize("name, default", [
    ("ubuntu.list", "http://archive.ubuntu.com/ubuntu/"),
    ("ubuntu-security.list", "http://security.ubuntu.com/ubuntu/"),
    ("debian.list", "http://deb.debian.org/debian/"),
    ("debian-security.list", "http://deb.debian.org/debian-security/"),
])
def test_apt_tries_the_distribution_first_then_its_mirrors_in_order(name, default):
    entries = mirror_list(name)

    assert entries[0][0] == default
    assert [priority for _, priority in entries] == list(range(1, len(entries) + 1))
    assert len({uri.split("/")[2] for uri, _ in entries}) == len(entries) >= 3
    assert all(uri.endswith("/") for uri, _ in entries)


def test_apt_mirror_lists_keep_unix_line_endings():
    # sh and apt in the build read a carriage return as part of the line.
    attributes = (ROOT / ".gitattributes").read_text(encoding="utf-8").splitlines()

    assert "build/* text eol=lf" in attributes and "build/apt/* text eol=lf" in attributes
    sources = [path for pattern in ("*.sh", "*.py", "*.list") for path in (ROOT / "build").rglob(pattern)]
    assert len(sources) >= 7
    assert [path.name for path in sources if b"\r" in path.read_bytes()] == []


# --- The Dockerfiles use them ------------------------------------------------

@pytest.mark.parametrize("dockerfile", DOCKERFILES, ids=lambda path: path.name)
def test_every_download_of_a_build_has_fallbacks(dockerfile):
    text = dockerfile.read_text(encoding="utf-8")
    runs = [line.strip() for line in text.splitlines() if not line.lstrip().startswith("#")]

    assert "COPY build /build" in text
    # apt: the mirror lists, before its first update.
    assert text.index("sh /build/apt-mirrors.sh") < text.index("apt-get update") < text.index("COPY requirements")
    # pip: only through pip_install.py.
    assert not [line for line in runs if re.search(r"(^|&& |RUN )(python3? -m )?pip install", line)]
    assert "pip_install.py --no-cache-dir -r requirements." in text
    # Files: only through fetch.py, with a checksum.
    assert not [line for line in runs if re.search(r"wget |curl |urlretrieve", line)]
    assert "fetch.py" in text
    for name in re.findall(r"--sha256 \"\$(\w+)\"", text):
        assert re.search(rf"ARG {name}=[0-9a-f]{{64}}", text)


def test_the_build_tools_reach_the_build():
    ignored = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()

    assert not any(line.strip("/") in ("build", "build/*") for line in ignored)


# --- Images: Docker Hub, or an identical copy ---------------------------------

def test_image_sources():
    assert stacks.image_sources("python:3.12-slim") == [
        "python:3.12-slim", "public.ecr.aws/docker/library/python:3.12-slim", "mirror.gcr.io/library/python:3.12-slim"]
    assert stacks.image_sources("nvidia/cuda:12.8.1-base-ubuntu22.04") == [
        "nvidia/cuda:12.8.1-base-ubuntu22.04", "nvcr.io/nvidia/cuda:12.8.1-base-ubuntu22.04",
        "mirror.gcr.io/nvidia/cuda:12.8.1-base-ubuntu22.04"]
    assert stacks.image_sources("cloudflare/cloudflared:2026.10.0") == [
        "cloudflare/cloudflared:2026.10.0", "mirror.gcr.io/cloudflare/cloudflared:2026.10.0"]
    # Another registry's image has only its own.
    for image in ("hub.example.ts.net/piano-worker:current", "localhost:5000/x", "nvcr.io/nvidia/cuda:12.8.1"):
        assert stacks.image_sources(image) == [image]


def test_every_image_of_the_project_has_a_copy_elsewhere():
    images = stacks.base_images(DOCKERFILES)
    for compose in ROOT.glob("docker-compose*.yml"):
        images += re.findall(r"^\s*image:\s*([^\s$]+)\s*$", compose.read_text(encoding="utf-8"), re.MULTILINE)

    assert "python:3.12-slim" in images and "nvidia/cuda:12.6.3-base-ubuntu22.04" in images
    assert [image for image in images if len(stacks.image_sources(image)) < 2] == []


def test_base_images_leave_out_stages_and_build_arguments():
    assert stacks.base_images([ROOT / "Dockerfile.worker"]) == [
        "nvidia/cuda:12.8.1-base-ubuntu22.04", "nvidia/cuda:12.6.3-base-ubuntu22.04"]


INSPECT = ["docker", "image", "inspect", "--format", "{{.Id}}"]


def fake_docker(failing=(), local=(), stalling=()):
    calls = []

    def docker(command):
        calls.append(command)
        if command[:3] == ["docker", "image", "inspect"]:
            return SimpleNamespace(returncode=0 if command[-1] in local else 1, stdout="", stderr="")
        if command[:2] == ["docker", "pull"] and command[-1] in stalling:
            raise subprocess.TimeoutExpired(command, stacks.PULL_TIMEOUT)
        failed = command[:2] == ["docker", "pull"] and command[-1] in failing
        return SimpleNamespace(returncode=1 if failed else 0, stdout="", stderr="")
    return calls, docker


def test_an_image_comes_from_a_copy_under_its_own_name_when_docker_hub_fails():
    calls, docker = fake_docker(failing={"redis:7-alpine"})
    said = []

    assert stacks.ensure_image(docker, "redis:7-alpine", say=said.append)
    assert calls == [
        INSPECT + ["redis:7-alpine"],
        ["docker", "pull", "redis:7-alpine"],
        ["docker", "pull", "public.ecr.aws/docker/library/redis:7-alpine"],
        ["docker", "tag", "public.ecr.aws/docker/library/redis:7-alpine", "redis:7-alpine"],
        ["docker", "image", "rm", "public.ecr.aws/docker/library/redis:7-alpine"],
    ]
    assert "Docker Hub failed" in said[-1] and "public.ecr.aws" in said[-1]


def test_a_stalled_registry_gives_way_to_the_next():
    image = "nvidia/cuda:12.8.1-base-ubuntu22.04"
    calls, docker = fake_docker(stalling={image})

    assert stacks.ensure_image(docker, image, say=lambda text: None)
    assert calls[2] == ["docker", "pull", "nvcr.io/" + image]


def test_no_registry_giving_an_image_is_said_and_left_to_the_build():
    image = "postgres:17-alpine"
    calls, docker = fake_docker(failing=set(stacks.image_sources(image)))
    said = []

    assert not stacks.ensure_image(docker, image, say=said.append)
    assert "will fail" in said[-1]


def test_an_image_on_this_machine_needs_no_network():
    # As builds and Compose: a start after a power cut, offline, rebuilds
    # nothing because Docker Hub has a newer image.
    calls, docker = fake_docker(local={"python:3.12-slim"})

    assert stacks.ensure_image(docker, "python:3.12-slim")
    assert calls == [INSPECT + ["python:3.12-slim"]]


IMAGE_LS = ["docker", "image", "ls", "--format", "{{.Repository}}:{{.Tag}}"]


def compose_run(config, calls, local=()):
    def run(command, **options):
        calls.append((command, options))
        stdout = ""
        if command[2:4] == ["config", "--format"]:
            stdout = json.dumps(config)
        elif command == IMAGE_LS:
            stdout = "".join(image + chr(10) for image in local)
        return SimpleNamespace(returncode=0, stdout=stdout, stderr="")
    return run


def pulls(calls):
    return [command[-1] for command, _ in calls if command[:2] == ["docker", "pull"]]


def test_compose_stack_gets_missing_images_before_a_start_once(tmp_path):
    (tmp_path / "Dockerfile.api").write_text("FROM python:3.12-slim", encoding="utf-8")
    config = {"services": {
        "api": {"build": {"context": str(tmp_path), "dockerfile": "Dockerfile.api"}, "image": "project-api"},
        "redis": {"image": "redis:7-alpine"},
        "postgres": {"image": "postgres:17-alpine"},
    }}
    calls = []
    stack = stacks.ComposeStack([], run=compose_run(config, calls, local={"postgres:17-alpine"}), root=tmp_path)
    stack.up()
    stack.up()

    # One listing for every image, not a call each.
    assert [command for command, _ in calls] == [
        ["docker", "compose", "config", "--format", "json"],
        IMAGE_LS,
        ["docker", "pull", "python:3.12-slim"],
        ["docker", "pull", "redis:7-alpine"],
        ["docker", "compose", "up", "-d", "--build"],
        ["docker", "compose", "up", "-d", "--build"],
    ]
    # A pull shows docker's progress, as Compose's would, within a time limit.
    for command, options in calls:
        if command[:2] == ["docker", "pull"]:
            assert options == {"cwd": tmp_path, "timeout": stacks.PULL_TIMEOUT}
        else:
            assert "timeout" not in options


def test_a_restart_checks_the_service_images_after_its_build(tmp_path):
    config = {"services": {"api": {"build": {"context": str(tmp_path)}}, "redis": {"image": "redis:7-alpine"}}}
    calls = []
    stack = stacks.ComposeStack([], run=compose_run(config, calls), popen=lambda *a, **k: None, root=tmp_path)
    stack.start_build()
    assert pulls(calls) == []

    stack.up()
    assert pulls(calls) == ["redis:7-alpine"]


def test_building_one_service_gets_only_its_images(tmp_path):
    (tmp_path / "Dockerfile.api").write_text("FROM python:3.12-slim", encoding="utf-8")
    (tmp_path / "Dockerfile.worker").write_text("FROM nvidia/cuda:12.8.1-base-ubuntu22.04", encoding="utf-8")
    config = {"services": {
        "api": {"build": {"context": str(tmp_path), "dockerfile": "Dockerfile.api"}},
        "worker": {"build": {"context": str(tmp_path), "dockerfile": "Dockerfile.worker"}},
        "redis": {"image": "redis:7-alpine"},
    }}
    calls = []
    stacks.ComposeStack([], run=compose_run(config, calls), root=tmp_path).build("api")

    assert pulls(calls) == ["python:3.12-slim"]


def test_a_start_with_every_image_here_is_one_listing(tmp_path):
    (tmp_path / "Dockerfile.api").write_text("FROM python:3.12-slim", encoding="utf-8")
    config = {"services": {"api": {"build": {"context": str(tmp_path), "dockerfile": "Dockerfile.api"}},
                           "redis": {"image": "redis:7-alpine"}}}
    calls = []
    stacks.ComposeStack([], run=compose_run(config, calls, local={"python:3.12-slim", "redis:7-alpine"}),
                        root=tmp_path).up()

    assert [command for command, _ in calls][1:-1] == [IMAGE_LS]


def test_a_start_without_a_build_gets_no_base_image(tmp_path):
    (tmp_path / "Dockerfile.worker").write_text("FROM nvidia/cuda:12.8.1-base-ubuntu22.04", encoding="utf-8")
    config = {"services": {"worker": {"build": {"context": str(tmp_path), "dockerfile": "Dockerfile.worker"},
                                      "image": "hub.example/piano-worker:current"}}}
    calls = []
    stacks.NodeStack(gpu=False, run=compose_run(config, calls), root=tmp_path).up(build=False)

    assert [command for command, _ in calls if command[:2] == ["docker", "image"]] == []


def test_a_broken_compose_configuration_is_left_to_compose(tmp_path):
    calls = []

    def run(command, **options):
        calls.append(command)
        return SimpleNamespace(returncode=1 if "config" in command else 0, stdout="not json", stderr="error")

    assert stacks.ComposeStack([], run=run, root=tmp_path).up() == 0
    assert calls[-1] == ["docker", "compose", "up", "-d", "--build"]
