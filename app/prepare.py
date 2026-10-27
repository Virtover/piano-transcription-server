"""Give the folders the services write to to their non-root user.

Run as root by the Compose `prepare` service before the other services
start. Docker creates a missing bind-mounted folder such as billing-data as
root, and volumes from older versions belong to root; the services, which
run as user ID 10001, could not write to either. The PostgreSQL cluster is
never touched: PostgreSQL owns its files.

billing-data is also used from the host, to copy, move, or delete backups.
Its folders keep their group, or take BILLING_DATA_GROUP, which
scripts/server.py sets to the group of the user running it, and are made
writable by that group, with new files inheriting it.
"""
import os
import stat
import sys
from pathlib import Path

# app.config.CONTAINER_UID; not imported, so a mistake in .env cannot stop
# this from running. A test keeps them equal.
CONTAINER_UID = 10001
DATA_DIR = "/data"
BILLING_DIR = "/billing"
# The default place of the live cluster inside billing-data. It may still be
# empty before PostgreSQL first starts, so it is skipped by name too.
CLUSTER_NAMES = {"postgres"}
# Group read, write, and search, and setgid so new files inherit the group.
SHARED_FOLDER_BITS = stat.S_ISGID | stat.S_IRWXG


def is_cluster(directory: Path) -> bool:
    return (directory / "PG_VERSION").exists()


def give(root: Path, uid: int = CONTAINER_UID, gid: int | None = None, shared: bool = False,
         skip_names: frozenset[str] = frozenset(), fs=os) -> int:
    """Change the owner of root and everything below it that uid does not
    own yet, except PostgreSQL clusters and top-level folders named in
    skip_names. Without gid, the group is kept. With shared, folders whose
    group is not root's become writable by it. Returns the number of
    entries changed."""
    changed = 0

    def take(path: Path) -> None:
        nonlocal changed
        info = fs.lstat(path)
        group = info.st_gid if gid is None else gid
        if info.st_uid != uid or info.st_gid != group:
            fs.chown(path, uid, group, follow_symlinks=False)
            changed += 1
        bits = SHARED_FOLDER_BITS if shared and group != 0 and stat.S_ISDIR(info.st_mode) else 0
        if stat.S_IMODE(info.st_mode) & bits != bits:
            fs.chmod(path, stat.S_IMODE(info.st_mode) | bits)
            changed += 1

    take(root)
    for directory, folders, files in os.walk(root):
        here = Path(directory)
        folders[:] = [
            name for name in folders
            if not (here == root and name in skip_names) and not is_cluster(here / name)
        ]
        for name in folders + files:
            take(here / name)
    return changed


def host_group() -> int | None:
    """BILLING_DATA_GROUP, unless unset or root's."""
    value = os.environ.get("BILLING_DATA_GROUP", "").strip()
    return int(value) if value.isdigit() and int(value) != 0 else None


def main() -> int:
    backups = Path(BILLING_DIR) / "backups"
    try:
        backups.mkdir(parents=True, exist_ok=True)
        changed = give(Path(DATA_DIR), gid=CONTAINER_UID) + give(
            Path(BILLING_DIR), gid=host_group(), shared=True, skip_names=frozenset(CLUSTER_NAMES)
        )
    except OSError as error:
        # Some hosts' shared folders do not support ownership; the services
        # check what they can write and explain it if they cannot.
        print(f"prepare: could not change owners ({error})", file=sys.stderr)
        return 0
    print(f"prepare: {changed} change(s) for user ID {CONTAINER_UID}" if changed else "prepare: nothing to change")
    return 0


if __name__ == "__main__":
    sys.exit(main())
