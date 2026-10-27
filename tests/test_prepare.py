import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import config, prepare

ROOT = 0
HOST_GROUP = 1000


class FakeFS:
    """Owners, groups, and modes simulated in a dict, so the tests run on any
    system as any user. Everything starts as root's, as Docker creates it."""

    def __init__(self, group=ROOT):
        self.entries = {}
        self.group = group

    def lstat(self, path):
        path = Path(path)
        is_folder = path.is_dir()
        uid, gid, mode = self.entries.get(path, (ROOT, self.group, 0o755 if is_folder else 0o644))
        return SimpleNamespace(st_uid=uid, st_gid=gid, st_mode=(stat.S_IFDIR if is_folder else stat.S_IFREG) | mode)

    def chown(self, path, uid, gid, follow_symlinks=True):
        assert not follow_symlinks
        info = self.lstat(path)
        self.entries[Path(path)] = (uid, gid, stat.S_IMODE(info.st_mode))

    def chmod(self, path, mode):
        info = self.lstat(path)
        self.entries[Path(path)] = (info.st_uid, info.st_gid, mode)

    def owner(self, path):
        return self.entries.get(Path(path), (ROOT, self.group, None))


def make(root: Path, *paths: str) -> None:
    for path in paths:
        target = root / path
        if path.endswith("/"):
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("x", encoding="utf-8")


def test_uid_matches_the_services():
    assert prepare.CONTAINER_UID == config.CONTAINER_UID


def test_root_owned_folders_are_given_to_the_services(tmp_path):
    fs = FakeFS()
    make(tmp_path, "backups/billing-1.dump", "jobs/a/audio.wav", "billing.sqlite3")

    assert prepare.give(tmp_path, gid=prepare.CONTAINER_UID, fs=fs) == 7
    assert set(fs.entries) == {tmp_path, *tmp_path.rglob("*")}
    assert {fs.owner(path)[:2] for path in fs.entries} == {(prepare.CONTAINER_UID, prepare.CONTAINER_UID)}


def test_what_the_services_own_already_is_left_alone(tmp_path):
    fs = FakeFS()
    make(tmp_path, "backups/")
    for path in (tmp_path, tmp_path / "backups"):
        fs.chown(path, prepare.CONTAINER_UID, ROOT, follow_symlinks=False)

    assert prepare.give(tmp_path, fs=fs, shared=True) == 0


def test_postgresql_clusters_are_never_touched(tmp_path):
    fs = FakeFS()
    # The default cluster before PostgreSQL first ran, and one elsewhere.
    make(tmp_path, "postgres/", "other/PG_VERSION", "other/base/1", "backups/")

    prepare.give(tmp_path, fs=fs, skip_names=frozenset(prepare.CLUSTER_NAMES))

    assert set(fs.entries) == {tmp_path, tmp_path / "backups"}


def test_billing_data_folders_are_shared_with_the_host_group(tmp_path):
    # Created by scripts/server.py as the host user, so the group is theirs.
    fs = FakeFS(group=HOST_GROUP)
    make(tmp_path, "backups/billing-1.dump")

    prepare.give(tmp_path, fs=fs, shared=True)

    for folder in (tmp_path, tmp_path / "backups"):
        assert fs.owner(folder) == (prepare.CONTAINER_UID, HOST_GROUP, 0o755 | prepare.SHARED_FOLDER_BITS)
    # Files stay as they are: deleting one needs only its folder.
    assert fs.owner(tmp_path / "backups" / "billing-1.dump") == (prepare.CONTAINER_UID, HOST_GROUP, 0o644)


def test_a_given_group_replaces_root_and_old_groups(tmp_path):
    fs = FakeFS(group=prepare.CONTAINER_UID)
    make(tmp_path, "backups/")

    prepare.give(tmp_path, gid=HOST_GROUP, fs=fs, shared=True)

    assert fs.owner(tmp_path / "backups")[1:] == (HOST_GROUP, 0o755 | prepare.SHARED_FOLDER_BITS)


def test_root_group_is_never_given_write_access(tmp_path):
    fs = FakeFS()
    make(tmp_path, "backups/")

    prepare.give(tmp_path, fs=fs, shared=True)

    assert fs.owner(tmp_path / "backups") == (prepare.CONTAINER_UID, ROOT, 0o755)


@pytest.mark.parametrize(("value", "group"), [("1000", 1000), ("", None), ("0", None), ("staff", None)])
def test_host_group(monkeypatch, value, group):
    monkeypatch.setenv("BILLING_DATA_GROUP", value)

    assert prepare.host_group() == group


def test_unsupported_ownership_does_not_stop_the_stack(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(prepare, "DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(prepare, "BILLING_DIR", str(tmp_path / "billing"))
    (tmp_path / "data").mkdir()

    def refused(*arguments, **options):
        raise PermissionError("Operation not permitted")

    monkeypatch.setattr(prepare.os, "chown", refused, raising=False)
    monkeypatch.setattr(prepare.os, "lstat", lambda path: os.stat_result((stat.S_IFDIR | 0o755,) + (0,) * 9))

    assert prepare.main() == 0
    assert (tmp_path / "billing" / "backups").is_dir()
    assert "could not change owners" in capsys.readouterr().err
