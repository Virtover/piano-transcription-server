"""Places on a device's models (app/worker/places.py, app.worker.tasks):
a job takes one before it is claimed and gives it back once its model step
ends, so its last steps run beside the next job's model step; a job without
a place stays queued and goes to a device that has one."""

import sys
import threading
import time
import types

import pytest

from app import cluster
from app.worker import load, places, tasks


# --- The places themselves ------------------------------------------------------------------

def never() -> bool:
    return False


def test_places_are_handed_out_up_to_the_capacity():
    pool = places.Places(2)

    first, second = pool.take(never), pool.take(never)

    assert pool.in_use == 2
    assert pool.take(lambda: True, interval=0.01) is None
    first.release()
    assert pool.in_use == 1
    second.release()
    assert pool.in_use == 0


def test_a_place_given_back_twice_is_given_back_once():
    pool = places.Places(2)
    place = pool.take(never)
    pool.take(never)

    place.release()
    place.release()

    assert pool.in_use == 1


def test_a_waiting_job_gets_the_place_given_back():
    pool = places.Places(1)
    held = pool.take(never)
    got = []
    waiter = threading.Thread(target=lambda: got.append(pool.take(never, interval=0.01)))
    waiter.start()
    time.sleep(0.05)
    assert not got

    held.release()
    waiter.join(timeout=2)

    assert got and got[0] is not None and pool.in_use == 1


def test_places_go_in_order_of_arrival():
    pool = places.Places(1)
    held = pool.take(never)
    order = []

    def wait(name):
        place = pool.take(never, interval=0.01)
        order.append(name)
        place.release()

    first = threading.Thread(target=wait, args=("first",))
    first.start()
    while pool.waiting < 1:
        time.sleep(0.005)
    second = threading.Thread(target=wait, args=("second",))
    second.start()
    while pool.waiting < 2:
        time.sleep(0.005)

    held.release()
    first.join(timeout=2)
    second.join(timeout=2)

    assert order == ["first", "second"]


def test_a_job_that_gives_up_leaves_its_turn_to_the_next():
    pool = places.Places(1)
    held = pool.take(never)
    stop = threading.Event()
    gave_up = []
    first = threading.Thread(target=lambda: gave_up.append(pool.take(stop.is_set, interval=0.01)))
    first.start()
    while pool.waiting < 1:
        time.sleep(0.005)

    stop.set()
    first.join(timeout=2)
    held.release()

    assert gave_up == [None]
    assert pool.waiting == 0
    assert pool.take(never, interval=0.01) is not None


def test_every_change_is_announced():
    changes = []
    pool = places.Places(1, on_change=lambda: changes.append(pool.in_use))

    pool.take(never).release()

    assert changes == [1, 0]


@pytest.mark.parametrize("value, expected", [("3", 3), ("", 6), ("0", 6), ("many", 6)])
def test_the_launchers_places_are_read_from_the_environment(monkeypatch, value, expected):
    monkeypatch.setenv(places.PLACES_VARIABLE, value)

    assert places.configured(6) == expected


# --- Jobs and places --------------------------------------------------------------------------

@pytest.fixture
def worker(api_context, monkeypatch):
    _, settings, billing = api_context
    settings.billing.provider = "google_play"
    settings.billing.products = {"starter": 10}
    billing.credit_purchase("user", "starter", "token", "order")
    monkeypatch.setattr(tasks, "PLACE_CHECK_INTERVAL", 0.01)
    monkeypatch.setattr(tasks, "worker_stopping", threading.Event())
    sent = []
    monkeypatch.setattr(tasks, "send_job",
                        lambda job_id, backend, *arguments, **options: sent.append((job_id, options)))
    cluster.forget_egress_cache()
    yield settings, billing, sent
    cluster.forget_egress_cache()


def queue_job(billing, job_id="job"):
    billing.reserve("user", job_id, 2)
    tasks.redis.hset(tasks.job_key(job_id), mapping={
        "status": "queued", "progress": "0.0", "user_id": "user", "full_cost": "2", "source": "upload",
    })


def job(job_id="job"):
    return tasks.redis.hgetall(tasks.job_key(job_id))


def install_pipeline(monkeypatch, transcribe_source):
    module = types.ModuleType("app.transcription.pipeline")
    module.transcribe_source = transcribe_source
    monkeypatch.setitem(sys.modules, "app.transcription.pipeline", module)


def finished_model(seen):
    """A pipeline that ends its model step, then records the places in use
    while it writes the MIDI file."""

    def transcribe_source(output_dir, model_done_callback, **kwargs):
        seen.append(tasks.model_places.in_use)
        model_done_callback()
        seen.append(tasks.model_places.in_use)
        midi = output_dir / "transcription.mid"
        midi.write_bytes(b"MThd\x00\x00\x00\x06\x00\x00\x00\x01\x01\xe0MTrk\x00\x00\x00\x04\x00\xff\x2f\x00")
        return midi

    return transcribe_source


def test_a_job_gives_its_place_back_when_its_model_step_ends(worker, monkeypatch):
    _, billing, _ = worker
    monkeypatch.setattr(tasks, "model_places", places.Places(1))
    monkeypatch.setattr(tasks, "check_midi", lambda path: None)
    queue_job(billing)
    seen = []
    install_pipeline(monkeypatch, finished_model(seen))

    tasks.transcribe_job.fn("job")

    # One place while on the model; none while the MIDI file is written.
    assert seen == [1, 0]
    assert job()["status"] == "completed"
    assert tasks.model_places.in_use == 0


def test_a_failed_job_gives_its_place_back(worker, monkeypatch):
    _, billing, _ = worker
    monkeypatch.setattr(tasks, "model_places", places.Places(1))
    queue_job(billing)

    def broken(**kwargs):
        raise RuntimeError("the model broke")

    install_pipeline(monkeypatch, broken)
    tasks.transcribe_job.fn("job")

    assert job()["status"] == "failed"
    assert tasks.model_places.in_use == 0


def test_a_job_without_a_place_is_not_claimed_and_runs_once_one_is_free(worker, monkeypatch):
    _, billing, sent = worker
    pool = places.Places(1)
    monkeypatch.setattr(tasks, "model_places", pool)
    monkeypatch.setattr(tasks, "check_midi", lambda path: None)
    held = pool.take(never)
    queue_job(billing)
    seen = []
    install_pipeline(monkeypatch, finished_model(seen))
    runner = threading.Thread(target=tasks.transcribe_job.fn, args=("job",))
    runner.start()
    while pool.waiting < 1:
        time.sleep(0.005)

    # Waiting for the place: still queued, so another device may take it.
    assert job()["status"] == "queued"
    held.release()
    runner.join(timeout=5)

    assert job()["status"] == "completed"
    assert sent == []


def test_a_job_without_a_place_goes_to_a_device_with_a_free_one(worker, monkeypatch):
    _, billing, sent = worker
    pool = places.Places(1)
    monkeypatch.setattr(tasks, "model_places", pool)
    monkeypatch.setattr(tasks, "load_monitor", types.SimpleNamespace(current=lambda: load.Load(40.0, 8000.0, True)))
    cluster.register(tasks.redis, "desktop", cluster.TASKS)
    tasks.routing.publish_heartbeat(tasks.redis, 2, 30.0, worker="desktop:1", node="desktop", running=0,
                                    utilization=10.0, free_mib=10000.0, gpu=True)
    held = pool.take(never)
    queue_job(billing)
    install_pipeline(monkeypatch, lambda **kwargs: pytest.fail("ran here"))

    tasks.transcribe_job.fn("job")

    assert sent == [("job", {"delay": tasks.PASS_DELAY_MS})]
    # Never claimed here, and it took no place.
    assert job()["status"] == "queued" and job()["passes"] == "1"
    assert pool.in_use == 1


def test_a_job_waits_while_every_other_device_is_full(worker, monkeypatch):
    _, billing, sent = worker
    pool = places.Places(1)
    monkeypatch.setattr(tasks, "model_places", pool)
    monkeypatch.setattr(tasks, "load_monitor", types.SimpleNamespace(current=lambda: load.Load(40.0, 8000.0, True)))
    cluster.register(tasks.redis, "desktop", cluster.TASKS)
    tasks.routing.publish_heartbeat(tasks.redis, 2, 30.0, worker="desktop:1", node="desktop", running=2,
                                    utilization=10.0, free_mib=10000.0, gpu=True)
    pool.take(never)
    queue_job(billing)

    got = []
    waiter = threading.Thread(target=lambda: got.append(tasks.wait_for_place("job")))
    waiter.start()
    time.sleep(0.1)
    assert not got and sent == []
    tasks.redis.hset(tasks.job_key("job"), "status", "cancelled")
    waiter.join(timeout=2)

    # Cancelled while it waited: nothing is sent, nothing claimed.
    assert got == [(None, False)] and sent == []


def test_a_stopping_worker_queues_its_waiting_jobs_again(worker, monkeypatch):
    _, billing, sent = worker
    pool = places.Places(1)
    monkeypatch.setattr(tasks, "model_places", pool)
    pool.take(never)
    queue_job(billing)
    tasks.worker_stopping.set()

    assert tasks.wait_for_place("job") == (None, True)
    # At once, and not counted as a pass.
    assert sent == [("job", {})]
    assert "passes" not in job()


def test_the_heartbeat_reports_places_not_threads(api_context, monkeypatch):
    import dramatiq

    monkeypatch.setenv(places.PLACES_VARIABLE, "3")
    monkeypatch.setattr(tasks, "model_places", None)
    monkeypatch.setattr(tasks, "load_monitor", None)
    # Set by the shutdown below; the module's own stays clear.
    monkeypatch.setattr(tasks, "worker_stopping", threading.Event())
    monkeypatch.setattr(load, "sample_gpus", lambda: None)
    middleware = tasks.LocalWorkerHeartbeat()
    middleware.after_worker_boot(tasks.broker, dramatiq.Worker(tasks.broker, queues=[tasks.LOCAL_QUEUE],
                                                               worker_threads=6))
    try:
        place = tasks.model_places.take(never)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            workers = tasks.routing.local_workers(tasks.redis)
            if workers and workers[0]["running"] == 1:
                break
            time.sleep(0.01)
        assert workers[0]["capacity"] == 3 and workers[0]["running"] == 1
        place.release()
    finally:
        middleware.before_worker_shutdown(tasks.broker, None)


def test_the_time_limit_starts_when_the_job_has_its_place(worker, monkeypatch):
    from dramatiq.middleware import TimeLimit

    _, billing, _ = worker
    monkeypatch.setattr(tasks, "model_places", places.Places(1))
    queue_job(billing)
    limit = next(middleware for middleware in tasks.broker.middleware if isinstance(middleware, TimeLimit))
    started = []
    monkeypatch.setattr(limit.manager, "add_timeout", lambda thread, ttl: started.append((thread, ttl)))

    place, _ = tasks.wait_for_place("job")

    assert place is not None
    assert started == [(threading.get_ident(), limit.time_limit)]


# --- Places as memory, on a GPU that other programs share --------------------------------

class Room:
    """The room for jobs on a GPU, in GiB, as GpuRoom reads it: what other
    programs leave; tests change it as another program takes or frees VRAM."""

    def __init__(self, gib):
        self.gib = gib

    def __call__(self):
        return self.gib


def test_a_place_needs_room_for_its_memory_beside_the_held_ones():
    pool = places.Places(5, room=Room(2.5))

    pool.take(never, memory_gib=1.1)
    pool.take(never, memory_gib=1.1)

    # 2.2 GiB held: one more job of 1.1 does not fit in 2.5, though places are free.
    assert pool.take(lambda: True, interval=0.01, memory_gib=1.1) is None
    assert pool.in_use == 2 and pool.memory_gib == pytest.approx(2.2)
    # A smaller job, as an arrangement, fits.
    assert pool.take(never, memory_gib=0.3) is not None


def test_a_job_waits_while_another_program_holds_the_vram_and_starts_once_it_frees_it():
    room = Room(0.5)
    pool = places.Places(5, room=room)
    got = []
    waiter = threading.Thread(target=lambda: got.append(pool.take(never, interval=0.01, memory_gib=1.1)))
    waiter.start()
    time.sleep(0.05)
    assert not got and pool.waiting == 1

    # The other program closes: no restart, the job takes its place.
    room.gib = 4.0
    waiter.join(timeout=2)

    assert got and got[0] is not None and pool.in_use == 1


def test_running_jobs_keep_their_places_when_another_program_takes_vram():
    room = Room(4.0)
    pool = places.Places(5, room=room)
    held = [pool.take(never, memory_gib=1.1) for _ in range(3)]

    room.gib = 1.0
    # No new job, but the three keep running.
    assert pool.take(lambda: True, interval=0.01, memory_gib=1.1) is None
    assert pool.in_use == 3
    assert pool.usable(1.1) == 3
    for place in held:
        place.release()
    assert pool.memory_gib == 0.0


def test_a_job_past_its_model_step_leaves_its_memory_to_the_next():
    # Pipelining: a job gives its place back when its model step ends, and
    # the next takes it while the first writes its MIDI file and uploads.
    pool = places.Places(2, room=Room(2.2))
    first = pool.take(never, memory_gib=1.1)
    pool.take(never, memory_gib=1.1)
    got = []
    waiter = threading.Thread(target=lambda: got.append(pool.take(never, interval=5, memory_gib=1.1)))
    waiter.start()
    while pool.waiting < 1:
        time.sleep(0.005)

    started = time.monotonic()
    first.release()
    waiter.join(timeout=2)

    # At once, not after a check interval.
    assert got and got[0] is not None
    assert time.monotonic() - started < 1


def test_only_one_of_several_jobs_racing_for_the_last_room_takes_it():
    pool = places.Places(10, room=Room(1.5))
    start = threading.Barrier(8)
    got = []

    def take():
        start.wait()
        got.append(pool.take(lambda: True, interval=0.01, memory_gib=1.1))

    threads = [threading.Thread(target=take) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert sum(place is not None for place in got) == 1
    assert pool.memory_gib == pytest.approx(1.1)


def test_usable_places_follow_the_room_up_to_the_capacity():
    room = Room(10.0)
    pool = places.Places(4, room=room)

    assert pool.usable(1.1) == 4
    room.gib = 3.0
    assert pool.usable(1.1) == 2
    pool.take(never, memory_gib=1.1)
    assert pool.usable(1.1) == 2
    room.gib = 0.0
    assert pool.usable(1.1) == 1


def test_a_room_that_cannot_be_read_keeps_jobs_waiting_rather_than_failing():
    readings = iter([0.5])

    def room():
        try:
            return next(readings)
        except StopIteration:
            raise RuntimeError("nvidia-smi broke")

    pool = places.Places(3, room=room)

    # The last reading, 0.5 GiB, stands: the job waits, and nothing raises.
    assert pool.take(lambda: True, interval=0.01, memory_gib=1.1) is None


def test_places_without_a_room_are_only_a_count():
    pool = places.Places(2)

    pool.take(never, memory_gib=50.0)
    pool.take(never, memory_gib=50.0)

    assert pool.usable(1.1) == 2


def test_a_job_on_a_gpu_takes_the_memory_of_its_kind(worker, monkeypatch):
    _, billing, _ = worker
    pool = places.Places(3, room=Room(10.0))
    monkeypatch.setattr(tasks, "model_places", pool)
    queue_job(billing)
    tasks.redis.hset(tasks.job_key("job"), "mode", tasks.ARRANGEMENT)

    place, _ = tasks.wait_for_place("job")

    assert place.memory_gib == tasks.settings.worker.arrangement_memory_gib
    place.release()


def test_a_job_on_the_cpu_takes_no_memory(worker, monkeypatch):
    _, billing, _ = worker
    monkeypatch.setattr(tasks, "model_places", places.Places(3))
    queue_job(billing)

    place, _ = tasks.wait_for_place("job")

    assert place.memory_gib == 0.0
    place.release()


def test_a_device_without_room_passes_a_job_to_one_with_room(worker, monkeypatch):
    # The owner of this device uses its GPU: places are free, VRAM is not.
    _, billing, sent = worker
    monkeypatch.setattr(tasks, "model_places", places.Places(4, room=Room(0.2)))
    monkeypatch.setattr(tasks, "load_monitor", types.SimpleNamespace(current=lambda: load.Load(10.0, 500.0, True)))
    cluster.register(tasks.redis, "desktop", cluster.TASKS)
    tasks.routing.publish_heartbeat(tasks.redis, 2, 30.0, worker="desktop:1", node="desktop", running=0,
                                    utilization=10.0, free_mib=200.0, gpu=True)
    queue_job(billing)
    install_pipeline(monkeypatch, lambda **kwargs: pytest.fail("ran here"))

    tasks.transcribe_job.fn("job")

    assert sent == [("job", {"delay": tasks.PASS_DELAY_MS})]
    assert job()["status"] == "queued"


def test_a_device_whose_heartbeat_shows_no_room_gets_no_job(worker, monkeypatch):
    _, billing, sent = worker
    monkeypatch.setattr(tasks, "model_places", places.Places(4, room=Room(0.2)))
    monkeypatch.setattr(tasks, "load_monitor", types.SimpleNamespace(current=lambda: load.Load(10.0, 500.0, True)))
    cluster.register(tasks.redis, "desktop", cluster.TASKS)
    # Its owner uses its GPU: the places that fit now are those it runs.
    tasks.routing.publish_heartbeat(tasks.redis, 1, 30.0, worker="desktop:1", node="desktop", running=1,
                                    utilization=10.0, free_mib=9000.0, gpu=True)
    queue_job(billing)

    assert tasks.roomier_device(job(), "transcribe") is None


def test_the_heartbeat_reports_the_places_that_fit_now(api_context, monkeypatch):
    import dramatiq

    room = Room(10.0)
    monkeypatch.setenv(places.PLACES_VARIABLE, "4")
    monkeypatch.setattr(tasks, "model_places", None)
    monkeypatch.setattr(tasks, "load_monitor", None)
    monkeypatch.setattr(tasks, "worker_stopping", threading.Event())
    monkeypatch.setattr(load, "sample_gpus", lambda: None)
    monkeypatch.setattr(tasks, "gpu_room", lambda: types.SimpleNamespace(budget_gib=room))
    middleware = tasks.LocalWorkerHeartbeat()
    middleware.room_check_interval = 0.02
    middleware.after_worker_boot(tasks.broker, dramatiq.Worker(tasks.broker, queues=[tasks.LOCAL_QUEUE],
                                                               worker_threads=8))

    def capacity():
        workers = tasks.routing.local_workers(tasks.redis)
        return workers[0]["capacity"] if workers else None

    def wait_for(expected):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and capacity() != expected:
            time.sleep(0.01)
        return capacity()

    try:
        assert wait_for(4) == 4
        # Another program takes most of the VRAM: the API sees one place.
        room.gib = 1.5
        assert wait_for(1) == 1
        room.gib = 0.0
        assert wait_for(0) == 0
        # The API then expects an endless wait here, and outsources.
        assert tasks.routing.estimated_local_wait(tasks.redis) == float("inf")
    finally:
        middleware.before_worker_shutdown(tasks.broker, None)
