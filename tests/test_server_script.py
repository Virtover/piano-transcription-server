import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "server.py"
spec = importlib.util.spec_from_file_location("server_script", SCRIPT)
server = importlib.util.module_from_spec(spec)
spec.loader.exec_module(server)


class FakeClock:
    def __init__(self):
        self.time = 0.0

    def now(self):
        return self.time

    def sleep(self, seconds):
        self.time += seconds


class FakeBuild:
    def __init__(self, clock, done_at, returncode=0):
        self.clock = clock
        self.done_at = done_at
        self.final_code = returncode
        self.returncode = None
        self.terminated = False

    def poll(self):
        if self.clock.now() >= self.done_at:
            self.returncode = self.final_code
        return self.returncode

    def terminate(self):
        self.terminated = True


class FakeStack:
    def __init__(self, clock, build_seconds=30, build_code=0, active_jobs=(), up_code=0, stop_code=0,
                 ready_after=0, aof_seconds=0):
        self.clock = clock
        self.calls = []
        self.notice = None
        self.build_seconds = build_seconds
        self.build_code = build_code
        self.active_jobs = list(active_jobs)
        self.up_code = up_code
        self.stop_code = stop_code
        self.ready_after = ready_after
        self.build = None
        self.up_call = None
        self.stop_call = None
        self.redis_calls = []
        # Seconds the append-only file takes to write after it is turned on.
        self.aof_seconds = aof_seconds
        self.aof_done_at = None

    def redis(self, *arguments):
        self.redis_calls.append((self.clock.now(), arguments))
        if arguments == ("config", "set", "appendonly", "yes"):
            self.aof_done_at = self.clock.now() + self.aof_seconds
            return "OK"
        if arguments == ("info", "persistence"):
            writing = int(self.clock.now() < self.aof_done_at)
            return f"aof_enabled:1\naof_rewrite_in_progress:{writing}\naof_rewrite_scheduled:0"
        raise AssertionError(arguments)

    def maintenance(self, *arguments):
        self.calls.append(arguments)
        command = arguments[0]
        if command == "schedule":
            action, delay = arguments[1], float(arguments[2].removesuffix("s"))
            self.notice = {"action": action, "at": 1000 + delay, "seconds_remaining": int(delay),
                           "message": f"{action} in {int(delay)} s"}
            return json.dumps(self.notice)
        if command == "status":
            return json.dumps(self.notice)
        if command == "cancel":
            had = self.notice is not None
            self.notice = None
            return "cancelled" if had else "nothing was scheduled"
        if command == "active-jobs":
            return str(self.active_jobs.pop(0) if self.active_jobs else 0)
        raise AssertionError(arguments)

    def start_build(self):
        self.build = FakeBuild(self.clock, self.clock.now() + self.build_seconds, self.build_code)
        return self.build

    def up(self, command=None, build=True):
        self.up_call = (self.clock.now(), command, build)
        return self.up_code

    def stop(self, down):
        self.stop_call = (self.clock.now(), down)
        return self.stop_code

    def ready(self):
        return self.up_call is not None and self.clock.now() >= self.up_call[0] + self.ready_after

    def commands(self):
        return [arguments[0] for arguments in self.calls]


def run(arguments, **stack_options):
    clock = FakeClock()
    stack = FakeStack(clock, **stack_options)
    code = server.main(arguments, stack, clock)
    return code, stack, clock


# --- Arguments --------------------------------------------------------------

@pytest.mark.parametrize("text, seconds", [("30s", 30), ("5m", 300), ("1.5h", 5400), ("1d", 86400), ("0s", 0)])
def test_durations(text, seconds):
    assert server.parse_duration(text) == seconds


@pytest.mark.parametrize("text", ["5", "five minutes", "-1m", ""])
def test_invalid_durations(text):
    with pytest.raises(Exception):
        server.parse_duration(text)


def test_outsource_option_is_no_longer_needed(capsys):
    # .env decides; the option is still accepted so older commands keep working.
    assert server.parse_arguments(["restart", "5m", "--outsource"]).profile == []

    code, stack, _ = run(["start", "--outsource"])

    assert code == 0
    assert server.OUTSOURCE_NOTE in capsys.readouterr().out
    assert stack.up_call is not None


@pytest.mark.parametrize("arguments", [[], ["restart"], ["stop"], ["stop", "5m", "--outsource"], ["start", "5m"]])
def test_invalid_command_lines(arguments):
    with pytest.raises(SystemExit):
        server.parse_arguments(arguments)


def test_profiles_reach_restart_commands():
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    stack = server.Stack(["outsource"], run=fake_run, popen=lambda command: calls.append(command))
    stack.maintenance("status")
    stack.start_build()
    stack.up()
    stack.up(build=False)

    assert all(command[:4] == ["docker", "compose", "--profile", "outsource"] for command in calls)
    assert calls[0][4:] == ["exec", "-T", "api", "python", "-m", "app.maintenance", "status"]
    assert calls[1][4:] == ["build"]
    assert calls[2][4:] == ["up", "-d", "--build"]
    assert calls[3][4:] == ["up", "-d"]


def test_stop_covers_every_profile():
    calls = []
    stack = server.Stack([], run=lambda command, **kwargs: calls.append(command) or SimpleNamespace(returncode=0, stdout="api\n"))

    stack.stop(down=False)
    stack.stop(down=True)

    assert calls[0] == ["docker", "compose", "--profile", "*", "stop"]
    assert calls[1] == ["docker", "compose", "--profile", "*", "down"]


def test_custom_command_is_run_instead_of_compose_up():
    calls = []
    stack = server.Stack([], run=lambda command, **kwargs: calls.append(command) or SimpleNamespace(returncode=0))

    stack.up("docker compose up -d --build api")

    assert calls == [["docker", "compose", "up", "-d", "--build", "api"]]


def test_maintenance_errors_name_the_problem():
    stack = server.Stack([], run=lambda command, **kwargs: SimpleNamespace(returncode=1, stdout="", stderr='service "api" is not running'))

    with pytest.raises(RuntimeError, match="not running"):
        stack.maintenance("status")


# --- Restart ----------------------------------------------------------------

def test_full_restart(capsys):
    code, stack, _ = run(["restart", "5m"], build_seconds=60, aof_seconds=2)

    assert code == 0
    assert stack.calls[0] == ("schedule", "restart", "300s", "--grace", "300s")
    # Redis persistence is on and written before the restart, at the
    # announced time, with the default command; then the notice is cleared.
    assert stack.redis_calls[0] == (300, ("config", "set", "appendonly", "yes"))
    assert stack.up_call == (300 + stack.aof_seconds, None, True)
    assert stack.commands()[-1] == "cancel"
    assert stack.notice is None
    assert "done" in capsys.readouterr().out


def test_reason_and_grace_are_announced():
    _, stack, _ = run(["restart", "30s", "--reason", "Update", "--grace", "0s"])

    assert stack.calls[0] == ("schedule", "restart", "30s", "--grace", "0s", "--reason", "Update")


def test_slow_build_delays_the_restart_until_it_finishes():
    _, stack, _ = run(["restart", "30s"], build_seconds=90)

    assert stack.up_call[0] >= 90


def test_failed_build_cancels_and_keeps_the_current_version(capsys):
    code, stack, _ = run(["restart", "5m"], build_seconds=40, build_code=1)

    assert code == 1
    assert stack.up_call is None
    assert stack.notice is None
    assert "build failed" in capsys.readouterr().out


def test_cancel_from_elsewhere_stops_the_waiting_script():
    clock = FakeClock()
    stack = FakeStack(clock, build_seconds=600)
    original = stack.maintenance

    def maintenance(*arguments):
        if arguments[0] == "status" and clock.now() >= 60:
            stack.notice = None
        return original(*arguments)

    stack.maintenance = maintenance

    assert server.main(["restart", "5m"], stack, clock) == 0
    assert stack.up_call is None
    assert stack.build.terminated


def test_running_jobs_get_the_grace_period():
    _, stack, _ = run(["restart", "10s", "--grace", "1m"], build_seconds=0, active_jobs=[2, 1, 0])

    assert stack.commands().count("active-jobs") == 3
    assert stack.up_call[0] == 10 + 2 * server.DRAIN_INTERVAL


def test_grace_period_is_not_exceeded(capsys):
    _, stack, _ = run(["restart", "10s", "--grace", "20s"], build_seconds=0, active_jobs=[1] * 100)

    assert stack.up_call[0] <= 10 + 20 + server.DRAIN_INTERVAL
    assert "will be interrupted and refunded" in capsys.readouterr().out


def test_no_build_skips_the_build():
    _, stack, _ = run(["restart", "10s", "--no-build"])

    assert stack.build is None
    assert stack.up_call[0] == 10


def test_failed_restart_command_removes_the_notice():
    code, stack, _ = run(["restart", "10s"], build_seconds=0, up_code=3)

    assert code == 3
    assert stack.notice is None


def test_api_not_ready_leaves_the_notice_to_expire(capsys):
    code, stack, _ = run(["restart", "10s"], build_seconds=0, ready_after=10_000)

    assert code == 1
    assert stack.notice is not None
    assert "not ready" in capsys.readouterr().out


def test_interrupt_cancels_the_restart():
    clock = FakeClock()
    stack = FakeStack(clock, build_seconds=600)

    def interrupt(seconds):
        raise KeyboardInterrupt

    clock.sleep = interrupt

    assert server.main(["restart", "5m"], stack, clock) == 130
    assert stack.notice is None
    assert stack.build.terminated


# --- Stop and start ---------------------------------------------------------

def test_full_shutdown(capsys):
    code, stack, _ = run(["stop", "10m", "--back-in", "2h", "--reason", "Hardware"], active_jobs=[1, 0],
                          aof_seconds=2)

    assert code == 0
    assert stack.calls[0] == ("schedule", "shutdown", "600s", "--grace", "300s", "--reason", "Hardware",
                              "--back-in", "7200s")
    # Nothing is built for a shutdown; it stops after the running job finished.
    assert stack.build is None
    assert stack.up_call is None
    assert stack.redis_calls[0][1] == ("config", "set", "appendonly", "yes")
    assert stack.stop_call == (600 + server.DRAIN_INTERVAL + stack.aof_seconds, False)
    # The notice stays until the API starts again and removes it.
    assert stack.notice is not None
    assert "python scripts/server.py start" in capsys.readouterr().out


def test_shutdown_can_remove_the_containers():
    _, stack, _ = run(["stop", "0s", "--down"])

    assert stack.stop_call == (0, True)


def test_shutdown_tells_how_to_start_again(capsys):
    run(["stop", "0s"])

    output = capsys.readouterr().out
    assert "Start again with: python scripts/server.py start\n" in output
    assert "--outsource" not in output


def test_cancelled_shutdown_does_not_stop():
    clock = FakeClock()
    stack = FakeStack(clock)
    original = stack.maintenance

    def maintenance(*arguments):
        if arguments[0] == "status":
            stack.notice = None
        return original(*arguments)

    stack.maintenance = maintenance

    assert server.main(["stop", "5m"], stack, clock) == 0
    assert stack.stop_call is None


def test_failed_stop_reports_the_error(capsys):
    code, _, _ = run(["stop", "0s"], stop_code=2)

    assert code == 2
    assert "failed" in capsys.readouterr().out


def test_start_waits_until_ready(capsys):
    code, stack, _ = run(["start"], ready_after=6)

    assert code == 0
    assert stack.up_call == (0, None, False)
    assert "ready" in capsys.readouterr().out


def test_start_reports_an_api_that_does_not_come_up(capsys):
    code, _, _ = run(["start"], ready_after=10_000)

    assert code == 1
    assert "not ready" in capsys.readouterr().out


# --- Status and cancel ------------------------------------------------------

def test_status_and_cancel(capsys):
    code, _, _ = run(["status"])
    assert code == 0
    assert "nothing is scheduled" in capsys.readouterr().out

    code, stack, _ = run(["cancel"])
    assert code == 0
    assert stack.commands() == ["cancel"]


def test_redis_that_cannot_be_reached_does_not_stop_the_restart(capsys):
    clock = FakeClock()
    stack = FakeStack(clock)

    def unreachable(*arguments):
        raise RuntimeError('service "redis" is not running')

    stack.redis = unreachable

    assert server.main(["restart", "1m"], stack, clock) == 0
    assert stack.up_call is not None
    assert 'could not turn on Redis persistence (service "redis" is not running)' in capsys.readouterr().out


def test_slow_append_only_file_is_waited_for_up_to_a_limit(capsys):
    clock = FakeClock()
    stack = FakeStack(clock)
    stack.aof_seconds = 10 * server.AOF_TIMEOUT

    assert server.main(["restart", "1m"], stack, clock) == 0
    assert stack.up_call[0] == 60 + server.AOF_TIMEOUT
    assert "queued jobs may be lost" in capsys.readouterr().out
