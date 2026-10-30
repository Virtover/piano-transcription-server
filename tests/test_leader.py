import fakeredis
import pytest

from app import leader as leader_module
from app import main
from app.leader import Leadership, current_leader
from app.outsource import routing
from app.worker import tasks


class StopLoop(Exception):
    pass


@pytest.fixture
def redis():
    return fakeredis.FakeRedis(decode_responses=True)


# --- The lead -------------------------------------------------------------------

def test_one_replica_leads_at_a_time(redis):
    first, second = Leadership(redis), Leadership(redis)

    assert first.renew() and first.leading
    assert not second.renew() and not second.leading
    # Renewing keeps the lead.
    assert first.renew()
    assert current_leader(redis) == first.token


def test_a_follower_takes_over_after_a_clean_shutdown(redis):
    first, second = Leadership(redis), Leadership(redis)
    first.renew()

    first.stop()

    assert not first.leading
    assert second.renew()
    assert current_leader(redis) == second.token


def test_a_follower_takes_over_when_the_lead_expires(redis):
    first, second = Leadership(redis), Leadership(redis)
    first.renew()

    # The leader crashed: nothing renewed its key until it expired.
    redis.delete(leader_module.LEADER_KEY)

    assert second.renew()
    assert not first.renew() and not first.leading


def test_the_lead_expires_unless_renewed(redis):
    Leadership(redis, ttl=30).renew()

    assert 0 < redis.ttl(leader_module.LEADER_KEY) <= 30


def test_stopping_does_not_remove_another_replicas_lead(redis):
    first, second = Leadership(redis), Leadership(redis)
    first.renew()

    second.stop()

    assert current_leader(redis) == first.token


def test_without_redis_no_replica_leads(redis, monkeypatch):
    first = Leadership(redis)
    first.renew()

    def unavailable(*args, **kwargs):
        raise ConnectionError("Redis is down")

    monkeypatch.setattr(redis, "set", unavailable)

    assert not first.renew()
    assert not first.leading


# --- Periodic tasks -------------------------------------------------------------

class FakeLeader:
    def __init__(self, events):
        self.events = events

    def wait(self):
        self.events.append("wait")


def test_the_abandoned_job_watcher_waits_for_the_lead(monkeypatch):
    events = []

    def sleep(seconds):
        if events:
            raise StopLoop
        events.append("sleep")

    monkeypatch.setattr(main.time, "sleep", sleep)
    monkeypatch.setattr(main, "fail_abandoned_jobs", lambda: events.append("check"))
    monkeypatch.setattr(main, "reconcile_billing", lambda min_age: events.append(("settle", min_age)))

    with pytest.raises(StopLoop):
        main.watch_abandoned_jobs(FakeLeader(events))

    assert events == ["sleep", "wait", "check", ("settle", tasks.RECONCILE_AFTER)]


def test_the_watcher_settles_billing_when_the_check_fails(monkeypatch):
    events = []

    def sleep(seconds):
        if events:
            raise StopLoop
        events.append("sleep")

    def broken():
        raise ConnectionError("Redis is down")

    monkeypatch.setattr(main.time, "sleep", sleep)
    monkeypatch.setattr(main, "fail_abandoned_jobs", broken)
    monkeypatch.setattr(main, "reconcile_billing", lambda min_age: events.append("settle"))

    with pytest.raises(StopLoop):
        main.watch_abandoned_jobs(FakeLeader(events))

    assert events == ["sleep", "wait", "settle"]


# --- Taking over ----------------------------------------------------------------

def test_a_replica_that_gains_the_lead_recovers_once(redis):
    first, second = Leadership(redis), Leadership(redis)
    first.renew()
    recovered = []

    second.keep(lambda: recovered.append("recovered"))
    assert recovered == []

    # The leader crashed.
    redis.delete(leader_module.LEADER_KEY)
    second.keep(lambda: recovered.append("recovered"))
    second.keep(lambda: recovered.append("recovered"))

    assert recovered == ["recovered"]
    assert second.leading


def test_the_startup_leader_does_not_recover_twice(redis):
    leader = Leadership(redis)
    leader.renew()
    recovered = []
    leader._led = True

    leader.keep(lambda: recovered.append("recovered"))

    assert recovered == []


def test_a_failed_recovery_keeps_the_lead(redis):
    leader = Leadership(redis)

    def broken():
        raise RuntimeError("billing is down")

    leader.keep(broken)

    assert leader.leading


def test_taking_over_recovers_jobs_and_billing(monkeypatch):
    events = []
    monkeypatch.setattr(main, "recover_active_jobs", lambda reason: events.append(("jobs", reason)))
    monkeypatch.setattr(main, "reconcile_billing", lambda min_age: events.append(("billing", min_age)))

    main.recover_after_takeover()

    assert events == [("jobs", tasks.WORKER_STOPPED), ("billing", tasks.RECONCILE_AFTER)]


def test_backups_wait_for_the_lead(monkeypatch):
    events = []
    monkeypatch.setattr(main, "run_billing_backup", lambda: events.append("backup"))

    def stop(seconds):
        raise StopLoop

    monkeypatch.setattr(main.time, "sleep", stop)

    with pytest.raises(StopLoop):
        main.backup_billing_database(FakeLeader(events))

    assert events == ["wait", "backup"]


# --- Startup --------------------------------------------------------------------

@pytest.fixture
def startup(api_context, monkeypatch):
    """Records what API startup recovers, the way a second replica would
    start beside the first, which api_context started."""
    recovered = []
    monkeypatch.setattr(main, "ensure_valid", lambda role: None)
    monkeypatch.setattr(main, "recover_active_jobs", lambda reason: recovered.append("jobs"))
    monkeypatch.setattr(main, "reconcile_billing", lambda **options: recovered.append("billing"))
    monkeypatch.setattr(main, "monitor_backup_health", lambda leader=None: None)
    return recovered


def test_the_first_replica_recovers_jobs(startup):
    tasks.redis.delete(leader_module.LEADER_KEY)

    main.sync_billing_catalog()

    assert startup == ["jobs", "billing"]
    assert current_leader(tasks.redis) == main.leadership.token


def test_a_replica_joining_running_ones_recovers_nothing(startup):
    running = Leadership(tasks.redis)
    tasks.redis.delete(leader_module.LEADER_KEY)
    running.renew()

    main.sync_billing_catalog()

    assert startup == []
    assert not main.leadership.leading
    assert current_leader(tasks.redis) == running.token


def test_shutdown_hands_over_the_lead(startup):
    tasks.redis.delete(leader_module.LEADER_KEY)
    main.sync_billing_catalog()

    main.hand_over_lead()

    assert current_leader(tasks.redis) is None


def test_recovery_keeps_jobs_another_replica_is_starting(api_context):
    # Another replica is reading an upload; its job is not queued yet.
    tasks.redis.hset(tasks.job_key("uploading"), "status", "uploading")
    tasks.redis.sadd(routing.ACTIVE_KEYS["local"], "uploading", "gone")

    tasks.recover_active_jobs("restarted")

    assert tasks.redis.smembers(routing.ACTIVE_KEYS["local"]) == {"uploading"}
