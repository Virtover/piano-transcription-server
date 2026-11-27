"""Every download of an image build has a source to fall back on: build/
for what the Dockerfiles download. The images themselves fall back through
Docker's own registry-mirrors ("Download sources of a build" in docs/setup.md)."""

import hashlib
import importlib.util
import re
from pathlib import Path

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

    # the shared tools only; one step's own files are copied just before it, so a change to them rebuilds no earlier layer
    assert "COPY build/apt-mirrors.sh build/apt_install.sh build/fetch.py build/pip_install.py /build/" in text
    assert "COPY build /build" not in text
    if "fetch_dettato.sh /opt/dettato" in text:
        assert text.index("COPY build/dettato-weights.txt") > text.index("pip_install.py --no-cache-dir -r requirements.")
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
