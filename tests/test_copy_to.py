from pathlib import Path

import pytest

from app import backups, billing


@pytest.fixture
def tools(monkeypatch):
    calls = []

    def run(command, environment):
        calls.append((command, environment.get("PGPASSWORD")))

    monkeypatch.setattr(billing, "run_postgres_tool", run)
    return calls


def url_file(tmp_path, url):
    path = tmp_path / "managed_database_url"
    path.write_text(url + "\n", encoding="utf-8")
    return str(path)


def dumps(tmp_path, *stamps):
    folder = tmp_path / "backups"
    folder.mkdir()
    for stamp in stamps:
        (folder / f"billing-{stamp}.dump").write_bytes(b"PGDMP")
    return str(folder)


def test_the_newest_backup_is_restored_in_one_transaction(tmp_path, tools, capsys):
    url = "postgresql://billing:p%40ss@db.example.com:5432/billing?sslmode=verify-full"

    assert backups.copy_to(url_file(tmp_path, url), dumps(tmp_path, 1000, 3000, 2000)) == 0

    [(command, password)] = tools
    assert command[:4] == ["pg_restore", "--no-owner", "--exit-on-error", "--single-transaction"]
    # The password only in the environment, never in the command line.
    assert command[5] == "postgresql://billing@db.example.com:5432/billing?sslmode=verify-full"
    assert password == "p@ss"
    assert Path(command[-1]).name == "billing-3000.dump"
    assert "into the database at db.example.com" in capsys.readouterr().out


def test_nothing_to_copy(tmp_path, tools, capsys):
    assert backups.copy_to(url_file(tmp_path, "postgresql://db.example.com/billing"), dumps(tmp_path)) == 1

    assert tools == []
    assert "python -m app.backups now" in capsys.readouterr().err


def test_a_file_without_a_postgres_url_is_refused(tmp_path, tools):
    assert backups.copy_to(url_file(tmp_path, "rediss://x"), dumps(tmp_path, 1000)) == 1
    assert tools == []


def test_a_failed_restore_says_the_target_is_unchanged(tmp_path, monkeypatch, capsys):
    def fail(command, environment):
        raise RuntimeError("pg_restore failed with exit status 1: relation users already exists")

    monkeypatch.setattr(billing, "run_postgres_tool", fail)

    assert backups.copy_to(url_file(tmp_path, "postgresql://db.example.com/billing"), dumps(tmp_path, 1000)) == 1
    assert "was not changed" in capsys.readouterr().err
