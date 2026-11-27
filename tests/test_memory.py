"""RAM for local jobs (app/memory.py): the container's real room, job estimates, admission under a lock, a job
alone always starting; the worker's hook (app.worker.tasks.waits_for_ram); and the worker's memory limit that the host
scripts set (scripts/stacks.py)."""

import importlib.util
from pathlib import Path

import pytest

from app import memory
from app.config import settings
from app.worker import tasks

GIB = memory.GIB


def test_cgroup_v2_counts_its_limit_without_the_file_cache(tmp_path):
    (tmp_path / "memory.max").write_text("8589934592\n")
    (tmp_path / "memory.current").write_text(str(5 * GIB))
    (tmp_path / "memory.stat").write_text(f"anon {3 * GIB}\ninactive_file {GIB // 2}\nactive_file {GIB // 2}\n")
    assert memory.cgroup_available(tmp_path) == 4 * GIB
    (tmp_path / "memory.max").write_text("max\n")
    assert memory.cgroup_available(tmp_path) is None


def test_cgroup_v1(tmp_path):
    folder = tmp_path / "memory"
    folder.mkdir()
    (folder / "memory.limit_in_bytes").write_text(str(4 * GIB))
    (folder / "memory.usage_in_bytes").write_text(str(3 * GIB))
    (folder / "memory.stat").write_text(f"total_inactive_file {GIB // 2}\ntotal_active_file {GIB // 2}\n")
    assert memory.cgroup_available(tmp_path) == 2 * GIB
    (folder / "memory.limit_in_bytes").write_text(str(1 << 62))
    assert memory.cgroup_available(tmp_path) is None


def test_the_smaller_of_the_machine_and_the_container_counts(monkeypatch):
    import sys
    import types

    fake = types.SimpleNamespace(virtual_memory=lambda: types.SimpleNamespace(available=10 * GIB))
    monkeypatch.setitem(sys.modules, "psutil", fake)
    monkeypatch.setattr(memory, "cgroup_available", lambda root=memory.CGROUP: 3 * GIB)
    assert memory.available_bytes() == 3 * GIB
    monkeypatch.setattr(memory, "cgroup_available", lambda root=memory.CGROUP: None)
    assert memory.available_bytes() == 10 * GIB


def test_estimates_follow_mode_and_length():
    piano = memory.job_ram_bytes({"duration": "3600"})
    ensemble = memory.job_ram_bytes({"duration": "3600", "mode": "arrangement"})
    assert 1.5 * GIB < piano < 2.2 * GIB
    assert 0.55 * GIB < ensemble < 1.0 * GIB
    ten_hours = memory.job_ram_bytes({"duration": str(10 * 3600), "mode": "arrangement"})
    assert ten_hours < 2.5 * GIB                      # past an hour it grows little
    assert memory.job_ram_bytes({"duration": "bad"}) == memory.PIANO_BASE + memory.LEVELS_BASE


@pytest.fixture
def ledger(monkeypatch):
    monkeypatch.setattr(settings.worker, "min_free_ram_gib", 1.0)
    room = {"free": 8 * GIB}
    return memory.Ledger(available=lambda: room["free"]), room


def test_jobs_start_while_their_need_leaves_the_minimum_free(ledger):
    led, room = ledger
    job = {"duration": "240", "mode": "arrangement"}
    first = led.need(job)
    assert first > memory.job_ram_bytes(job)          # the libraries' first load counted once
    assert led.reserve("a", job)[0]
    assert led.need(job) == memory.job_ram_bytes(job)
    room["free"] = 2 * GIB
    assert not led.reserve("b", {"duration": "3600"})[0]
    led.release("a")
    assert led.reserve("b", {"duration": "3600"})[0]      # alone: always starts


def test_a_job_larger_than_the_machine_runs_alone(ledger):
    led, room = ledger
    room["free"] = 0
    assert led.reserve("huge", {"duration": str(20 * 3600)})[0]
    assert not led.reserve("next", {"duration": "60"})[0]


def test_reserving_is_idempotent_and_release_twice_is_harmless(ledger):
    led, _ = ledger
    assert led.reserve("a", {})[0] and led.reserve("a", {})[0]
    led.release("a")
    led.release("a")
    assert led.reserved == {}


def test_a_job_without_ram_waits_and_is_offered_again(api_context, monkeypatch):
    sent = []
    monkeypatch.setattr(tasks, "send_job", lambda job_id, backend, *args, **kw: sent.append((backend, kw)))
    monkeypatch.setattr(memory, "ledger", memory.Ledger(available=lambda: 0))
    memory.ledger.reserve("running", {})
    tasks.redis.hset(tasks.job_key("ram-job"), mapping={"status": "queued", "duration": "600"})
    assert tasks.waits_for_ram("ram-job", "local")
    assert sent == [("local", {"delay": int(tasks.RAM_WAIT_DELAY * 1000)})]
    assert not tasks.waits_for_ram("ram-job", "remote")       # the dispatcher runs no model
    memory.ledger.release("running")
    assert not tasks.waits_for_ram("ram-job", "local")
    assert "ram-job" in memory.ledger.reserved


def test_a_broken_ram_check_never_loses_a_job(api_context, monkeypatch):
    monkeypatch.setattr(memory.Ledger, "reserve", lambda self, job_id, job: 1 / 0)
    tasks.redis.hset(tasks.job_key("ram-job-2"), mapping={"status": "queued"})
    assert not tasks.waits_for_ram("ram-job-2", "local")


_spec = importlib.util.spec_from_file_location("stacks_for_memory", Path(__file__).resolve().parent.parent / "scripts" / "stacks.py")
stacks = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(stacks)


def test_the_host_scripts_give_the_worker_a_memory_limit_once(tmp_path):
    env = tmp_path / ".env"
    env.write_text("COMPOSE_PROFILES=\n")
    messages = stacks.prepare_memory_limit(tmp_path, ".env", 1.5, 0.15, total=16 * GIB)
    assert messages and "WORKER_MEMORY_LIMIT=13926m" in env.read_text()      # 16 GiB less 15% (2.4 GiB)
    assert stacks.prepare_memory_limit(tmp_path, ".env", 1.5, 0.15, total=32 * GIB) == []     # set: kept
    small = tmp_path / "small"
    small.mkdir()
    stacks.prepare_memory_limit(small, ".env", 1.5, 0.15, total=2 * GIB)
    assert "WORKER_MEMORY_LIMIT=1024m" in (small / ".env").read_text()       # never under 1 GiB
    assert stacks.prepare_memory_limit(tmp_path / "none", ".env", 1.5, 0.15, total=0) == []


def test_docker_memory_is_never_read_in_the_tests():
    assert stacks.docker_memory_bytes() is None


def test_memory_jobs_already_use_is_not_counted_twice(monkeypatch):
    """A running job's reservation is held back only for the part it does not use yet: what the process grew by is
    already missing from the free RAM."""
    monkeypatch.setattr(settings.worker, "min_free_ram_gib", 1.0)
    state = {"free": 4 * GIB, "process": 2 * GIB}
    led = memory.Ledger(available=lambda: state["free"], process=lambda: state["process"])
    led.used_kinds.update({"piano", "ensemble"})
    hour = {"duration": "3600"}                       # piano, about 1.9 GiB
    assert led.reserve("a", hour)[0]
    # The first job now uses its memory: the free RAM fell by it, and its reservation is no longer held back.
    state["free"] -= led.reserved["a"]
    state["process"] += led.reserved["a"]
    assert led.unused_reserved() == 0
    assert led.reserve("b", {"duration": "600"})[0]   # 0.45 GiB fits beside the 1 GiB kept free


def test_a_gpus_places_not_ram_limit_short_jobs_on_a_typical_machine(monkeypatch):
    """Throughput: on a machine with 16 GiB available, the 9 places of a 12 GB GPU all fill with 4-minute jobs of
    either kind; RAM holds none of them back."""
    monkeypatch.setattr(settings.worker, "min_free_ram_gib", 1.0)
    led = memory.Ledger(available=lambda: 16 * GIB, process=lambda: None)
    started = [led.reserve(f"job{i}", {"duration": "240", "mode": "arrangement" if i % 2 else ""})[0] for i in range(9)]
    assert all(started)
