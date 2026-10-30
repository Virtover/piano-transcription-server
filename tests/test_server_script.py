import importlib.util
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import config

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

    def prepare_files(self):
        self.prepared_at = self.clock.now()
        return []

    def reload_redis_users(self):
        self.reloaded_at = self.clock.now()
        return getattr(self, "reload_problem", "")

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

    stack = server.ComposeStack(["outsource"], run=fake_run, popen=lambda command: calls.append(command))
    stack.maintenance("status")
    stack.start_build()
    stack.up()
    stack.up(build=False)

    assert all(command[:4] == ["docker", "compose", "--profile", "outsource"] for command in calls)
    assert calls[0][4:] == ["exec", "-T", "api", "python", "-m", "app.maintenance", "status"]
    assert calls[1][4:] == ["build"]
    assert calls[2][4:] == ["up", "-d", "--build"]
    assert calls[3][4:] == ["up", "-d"]


def test_requests_are_built_on_the_primitives_alone(tmp_path):
    """What the scripts ask of a stack goes through exec and one_off, so
    another place to run the server needs only those."""
    calls = []

    class Recording(server.Stack):
        def exec(self, service, *command, **options):
            calls.append(("exec", service, command[:3]))
            return SimpleNamespace(returncode=0, stdout="OK\n", stderr="")

        def one_off(self, service, *command, **options):
            calls.append(("one_off", service, command[:3]))
            return SimpleNamespace(returncode=0, stdout="", stderr="")

    stack = Recording()
    dump = tmp_path / "billing-1.dump"
    dump.write_bytes(b"PGDMP")
    stack.maintenance("status")
    stack.ready()
    stack.reload_redis_users()
    stack.redis("ping")
    stack.fetch_backup("latest", KEY)
    stack.backup_now()
    stack.restore_dump(dump)

    assert [call[:2] for call in calls] == [
        ("exec", "api"), ("exec", "api"), ("exec", "redis"), ("exec", "redis"),
        ("one_off", "api"), ("one_off", "api"), ("exec", "postgres"),
    ]


def test_compose_stack_prepares_the_files_of_its_checkout(tmp_path):
    roots = []
    stack = server.ComposeStack([], root=tmp_path, prepare=lambda root: roots.append(root) or ["created .env"])

    assert stack.prepare_files() == ["created .env"]
    assert roots == [tmp_path]
    assert server.ComposeStack([], root=tmp_path).prepare_files() == []


def test_stop_covers_every_profile():
    calls = []
    stack = server.ComposeStack([], run=lambda command, **kwargs: calls.append(command) or SimpleNamespace(returncode=0, stdout="api\n"))

    stack.stop(down=False)
    stack.stop(down=True)

    assert calls[0] == ["docker", "compose", "--profile", "*", "stop"]
    assert calls[1] == ["docker", "compose", "--profile", "*", "down"]


def test_custom_command_is_run_instead_of_compose_up():
    calls = []
    stack = server.ComposeStack([], run=lambda command, **kwargs: calls.append(command) or SimpleNamespace(returncode=0))

    stack.up("docker compose up -d --build api")

    assert calls == [["docker", "compose", "up", "-d", "--build", "api"]]


def test_maintenance_errors_name_the_problem():
    stack = server.ComposeStack([], run=lambda command, **kwargs: SimpleNamespace(returncode=1, stdout="", stderr='service "api" is not running'))

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
    # Built, so a start after an update runs the new code.
    assert stack.up_call == (0, None, True)
    assert "ready" in capsys.readouterr().out


def test_start_prepares_files_before_starting():
    _, stack, _ = run(["start"])

    assert stack.prepared_at == 0


def test_start_stops_when_files_cannot_be_prepared(capsys):
    clock = FakeClock()
    stack = FakeStack(clock)

    def blocked():
        raise RuntimeError("secrets/postgres_password is a folder, not a file: move it away and start again")

    stack.prepare_files = blocked

    assert server.main(["start"], stack, clock) == 1
    assert stack.up_call is None
    assert "is a folder, not a file" in capsys.readouterr().out


@pytest.fixture
def checkout(tmp_path):
    (tmp_path / ".env.example").write_text("# Compose\n# COMPOSE_PROFILES=cloudflare\nAPI_BIND_ADDRESS=127.0.0.1\n",
                                           encoding="utf-8")
    (tmp_path / "server.env.example").write_text("# Limits\nMAX_ACTIVE_JOBS=50\n# OUTSOURCE_PROVIDER=runpod\n",
                                                 encoding="utf-8")
    return tmp_path


def test_new_installation_gets_env_and_password(checkout):
    created = server.prepare_files(checkout)

    assert len(created) == 6
    assert (checkout / ".env").read_text(encoding="utf-8") == (checkout / ".env.example").read_text(encoding="utf-8")
    assert (checkout / "server.env").read_text(encoding="utf-8") == (
        (checkout / "server.env.example").read_text(encoding="utf-8")
    )
    assert created[:2] == [
        "created .env from .env.example, for Docker Compose",
        "created server.env from server.env.example: the server's settings",
    ]
    passwords = [
        (checkout / "secrets" / name).read_text(encoding="utf-8")
        for name in ("postgres_password", "redis/operator", "redis/app", "redis/admin-read-only", "redis/admin-read-write", "admin-web/postgres-read-only", "admin-web/postgres-read-write")
    ]
    assert len(set(passwords)) == 7
    for password in passwords:
        assert len(password) == 48 and password.isalnum()
        # Nothing prints a password.
        assert not any(password in message for message in created)
    assert created[3] == "created random Redis passwords in secrets/redis: operator, app, admin-read-only, admin-read-write"
    assert created[4] == "created random admin web page passwords in secrets/admin-web: postgres-read-only, postgres-read-write"
    assert created[5] == "created the empty folder secrets/cloudflare, for a Cloudflare Tunnel's token"


def test_an_update_gets_the_missing_redis_passwords(checkout):
    (checkout / ".env").write_text("API_BIND_ADDRESS=127.0.0.1\n", encoding="utf-8")
    (checkout / "server.env").write_text("MAX_ACTIVE_JOBS=5\n", encoding="utf-8")
    (checkout / "secrets" / "redis").mkdir(parents=True)
    (checkout / "secrets" / "postgres_password").write_text("chosen", encoding="utf-8")
    (checkout / "secrets" / "redis" / "operator").write_text("kept", encoding="utf-8")
    # Docker made a folder where the missing file is mounted.
    (checkout / "secrets" / "redis" / "app").mkdir()

    assert server.prepare_files(checkout) == [
        "created random Redis passwords in secrets/redis: app, admin-read-only, admin-read-write",
        "created random admin web page passwords in secrets/admin-web: postgres-read-only, postgres-read-write",
        "created the empty folder secrets/cloudflare, for a Cloudflare Tunnel's token",
    ]
    assert (checkout / "secrets" / "redis" / "operator").read_text(encoding="utf-8") == "kept"
    assert (checkout / "secrets" / "redis" / "app").is_file()


def test_tunnel_token_folder_exists_for_the_workers_to_hide(checkout):
    # The workers mount an empty folder over it, which fails where none is.
    server.prepare_files(checkout)

    assert (checkout / "secrets" / "cloudflare").is_dir()
    assert list((checkout / "secrets" / "cloudflare").iterdir()) == []


def test_tunnel_token_folder_is_the_configured_one():
    assert server.TUNNEL_TOKEN_FOLDER == "secrets/" + Path(config.TUNNEL_TOKEN_FILE).parent.as_posix()


def test_tunnel_token_is_kept(checkout):
    (checkout / "secrets" / "cloudflare").mkdir(parents=True)
    (checkout / "secrets" / "cloudflare" / "tunnel_token").write_text("chosen", encoding="utf-8")

    created = server.prepare_files(checkout)

    assert not any("cloudflare" in message for message in created)
    assert (checkout / "secrets" / "cloudflare" / "tunnel_token").read_text(encoding="utf-8") == "chosen"


def test_billing_data_is_created_by_the_user_not_by_docker(checkout):
    server.prepare_files(checkout)

    assert (checkout / "billing-data" / "backups").is_dir()


@pytest.mark.parametrize(("gid", "expected"), [(1000, "1000"), (0, None)])
def test_billing_data_is_shared_with_the_users_group(monkeypatch, gid, expected):
    monkeypatch.setattr(server.os, "getgid", lambda: gid, raising=False)
    environment = {}

    server.share_billing_data(environment)

    assert environment.get("BILLING_DATA_GROUP") == expected


@pytest.mark.parametrize("action", [["start"], ["restart", "5m"], ["restore", "--from", "local"]])
def test_legacy_gpu_selects_the_older_worker_build(action):
    environment = {}

    server.select_gpu_build(server.parse_arguments(action + ["--legacy-gpu"]), environment)

    assert environment == {"WORKER_GPU_BUILD": "legacy"}


def test_without_legacy_gpu_the_env_file_decides():
    # Nothing set here, so WORKER_GPU_BUILD in .env, if any, still applies.
    environment = {}

    server.select_gpu_build(server.parse_arguments(["start"]), environment)

    assert environment == {}


def test_existing_files_are_never_changed(checkout):
    (checkout / ".env").write_text("API_BIND_ADDRESS=127.0.0.1\n", encoding="utf-8")
    (checkout / "server.env").write_text("MAX_ACTIVE_JOBS=5\n", encoding="utf-8")
    (checkout / "secrets" / "redis").mkdir(parents=True)
    (checkout / "secrets" / "postgres_password").write_text("chosen", encoding="utf-8")
    (checkout / "secrets" / "admin-web").mkdir()
    (checkout / "secrets" / "cloudflare").mkdir()
    for name in server.REDIS_USERS:
        (checkout / "secrets" / "redis" / name).write_text("chosen", encoding="utf-8")
    for name in server.ADMIN_WEB_DATABASE_PASSWORDS:
        (checkout / "secrets" / "admin-web" / name).write_text("chosen", encoding="utf-8")

    assert server.prepare_files(checkout) == []
    assert (checkout / "server.env").read_text(encoding="utf-8") == "MAX_ACTIVE_JOBS=5\n"
    assert (checkout / "secrets" / "postgres_password").read_text(encoding="utf-8") == "chosen"


# --- .env and server.env ------------------------------------------------------------------

def test_an_env_from_before_server_env_is_split_with_its_values(checkout):
    old = "# my notes\nMAX_ACTIVE_JOBS=5\nCOMPOSE_PROFILES=cloudflare\nOUTSOURCE_PROVIDER=runpod\nAPI_BIND_ADDRESS=0.0.0.0\n"
    (checkout / ".env").write_text(old, encoding="utf-8")

    messages = server.separate_env_files(checkout)

    assert "split .env into .env, for Docker Compose, and server.env" in messages[0]
    assert (checkout / ".env.before-split").read_text(encoding="utf-8") == old
    # Each file is its template, with the values in their places.
    assert (checkout / ".env").read_text(encoding="utf-8") == "# Compose\nCOMPOSE_PROFILES=cloudflare\nAPI_BIND_ADDRESS=0.0.0.0\n"
    assert (checkout / "server.env").read_text(encoding="utf-8") == "# Limits\nMAX_ACTIVE_JOBS=5\nOUTSOURCE_PROVIDER=runpod\n"


def test_a_split_changes_no_value(checkout):
    # The old file sets neither of the templates' active lines.
    (checkout / ".env").write_text("OUTSOURCE_PROVIDER=runpod\n", encoding="utf-8")

    server.separate_env_files(checkout)

    assert (checkout / ".env").read_text(encoding="utf-8") == (
        "# Compose\n# COMPOSE_PROFILES=cloudflare\n# API_BIND_ADDRESS=127.0.0.1\n"
    )
    assert (checkout / "server.env").read_text(encoding="utf-8") == (
        "# Limits\n# MAX_ACTIVE_JOBS=50\nOUTSOURCE_PROVIDER=runpod\n"
    )


def test_a_line_in_the_wrong_file_is_moved(checkout):
    (checkout / ".env").write_text("API_BIND_ADDRESS=127.0.0.1\nRESULT_RETENTION='2h'\n", encoding="utf-8")
    (checkout / "server.env").write_text("MAX_ACTIVE_JOBS=5\nCOMPOSE_PROFILES=cloudflare,admin\n", encoding="utf-8")

    messages = server.separate_env_files(checkout)

    assert messages == [
        "moved RESULT_RETENTION from .env to server.env: it is one of the server's settings",
        "moved COMPOSE_PROFILES from server.env to .env: Docker Compose reads it",
    ]
    assert (checkout / ".env").read_text(encoding="utf-8") == (
        "API_BIND_ADDRESS=127.0.0.1\n\n" + server.ADDED_HEADER + "\nCOMPOSE_PROFILES=cloudflare,admin\n"
    )
    # The line as it was, quotes and all.
    assert "RESULT_RETENTION='2h'" in (checkout / "server.env").read_text(encoding="utf-8")
    assert not (checkout / ".env.before-split").exists()


def test_a_value_in_both_files_keeps_the_one_that_applies(checkout):
    (checkout / ".env").write_text("MAX_ACTIVE_JOBS=5\n", encoding="utf-8")
    (checkout / "server.env").write_text("MAX_ACTIVE_JOBS=9\n", encoding="utf-8")

    server.separate_env_files(checkout)

    assert (checkout / "server.env").read_text(encoding="utf-8") == "MAX_ACTIVE_JOBS=9\n"
    assert (checkout / ".env").read_text(encoding="utf-8") == "# MAX_ACTIVE_JOBS=5  (moved: server.env sets it)\n"


def test_files_in_their_places_are_left_alone(checkout):
    (checkout / ".env").write_text("# mine\nAPI_BIND_ADDRESS=127.0.0.1\n", encoding="utf-8")
    (checkout / "server.env").write_text("# mine\nMAX_ACTIVE_JOBS=5\n", encoding="utf-8")

    assert server.separate_env_files(checkout) == []
    assert (checkout / ".env").read_text(encoding="utf-8") == "# mine\nAPI_BIND_ADDRESS=127.0.0.1\n"


@pytest.mark.parametrize("line", [
    "\ufeffMAX_ACTIVE_JOBS=5",          # the BOM of a file saved by Notepad
    "export MAX_ACTIVE_JOBS=5",
    "MAX_ACTIVE_JOBS = 5",
    "  MAX_ACTIVE_JOBS=5",
])
def test_every_way_of_writing_a_setting_is_moved(checkout, line):
    (checkout / ".env").write_text(f"{line}\nAPI_BIND_ADDRESS=0.0.0.0\n", encoding="utf-8")

    server.separate_env_files(checkout)

    assert server.EnvFile(checkout / "server.env").get("MAX_ACTIVE_JOBS") == "5"
    assert server.EnvFile(checkout / ".env").get("API_BIND_ADDRESS") == "0.0.0.0"
    assert "MAX_ACTIVE_JOBS" not in server.EnvFile(checkout / ".env").assignments()


def test_the_last_of_a_setting_written_twice_is_the_one_kept(checkout):
    (checkout / ".env").write_text("MAX_ACTIVE_JOBS=5\nMAX_ACTIVE_JOBS=9\n", encoding="utf-8")

    server.separate_env_files(checkout)

    assert server.EnvFile(checkout / "server.env").get("MAX_ACTIVE_JOBS") == "9"


def test_a_value_over_several_lines_is_not_moved(checkout):
    old = 'API_BIND_ADDRESS=127.0.0.1\nBILLING_PRODUCTS="{\n  \\"a\\": 5}"\n'
    (checkout / ".env").write_text(old, encoding="utf-8")

    [message] = server.separate_env_files(checkout)

    assert "BILLING_PRODUCTS over several lines" in message and "Nothing was changed" in message
    assert (checkout / ".env").read_text(encoding="utf-8") == old
    assert not (checkout / "server.env").exists() and not (checkout / ".env.before-split").exists()


def test_messages_name_settings_but_never_show_values(checkout):
    (checkout / ".env").write_text("API_BIND_ADDRESS=127.0.0.1\n", encoding="utf-8")
    (checkout / "server.env").write_text("ERROR_REPORT_URL=https://private.example/report\n", encoding="utf-8")
    (checkout / ".env").write_text("API_BIND_ADDRESS=127.0.0.1\nRESULT_RETENTION=7h\n", encoding="utf-8")

    messages = server.separate_env_files(checkout)

    assert messages and not any("7h" in message or "private.example" in message for message in messages)


def test_a_failed_write_leaves_the_files_as_they_were(checkout, monkeypatch):
    old = "MAX_ACTIVE_JOBS=5\nAPI_BIND_ADDRESS=0.0.0.0\n"
    (checkout / ".env").write_text(old, encoding="utf-8")

    def full_disk(*args, **kwargs):
        raise OSError("No space left on device")

    monkeypatch.setattr(server.os, "replace", full_disk)

    with pytest.raises(OSError):
        server.separate_env_files(checkout)
    assert (checkout / ".env").read_text(encoding="utf-8") == old
    assert not (checkout / "server.env").exists()


def test_the_files_keep_their_permissions(checkout):
    (checkout / ".env").write_text("MAX_ACTIVE_JOBS=5\n", encoding="utf-8")
    (checkout / ".env").chmod(0o600)

    server.separate_env_files(checkout)

    for name in (".env", "server.env"):
        assert (checkout / name).stat().st_mode & 0o777 == (checkout / ".env.before-split").stat().st_mode & 0o777


def test_admin_web_mode_goes_where_compose_reads_it(checkout):
    (checkout / ".env").write_text("ADMIN_WEB_MODE=read-only\nADMIN_WEB_HOSTNAME=admin.example.com\n", encoding="utf-8")

    server.separate_env_files(checkout)

    assert server.EnvFile(checkout / ".env").get("ADMIN_WEB_MODE") == "read-only"
    assert server.EnvFile(checkout / "server.env").get("ADMIN_WEB_HOSTNAME") == "admin.example.com"


def test_the_script_knows_what_compose_reads_as_the_server_does():
    assert server.COMPOSE_ONLY_VARIABLES == config.COMPOSE_ONLY_VARIABLES
    assert (server.COMPOSE_ENV, server.SERVER_ENV) == (config.COMPOSE_ENV_FILE, config.SERVER_ENV_FILE)


def test_empty_folder_left_by_docker_becomes_the_password(checkout):
    # Starting without the file makes Docker create a folder in its place.
    (checkout / "secrets" / "postgres_password").mkdir(parents=True)

    server.prepare_files(checkout)

    assert (checkout / "secrets" / "postgres_password").is_file()


@pytest.mark.skipif(os.name != "posix", reason="file modes")
def test_password_is_readable_by_the_containers_whatever_the_umask(checkout):
    previous = os.umask(0o077)
    try:
        server.prepare_files(checkout)
    finally:
        os.umask(previous)

    assert (checkout / "secrets").stat().st_mode & 0o777 == 0o755
    assert (checkout / "secrets" / "redis").stat().st_mode & 0o777 == 0o755
    assert (checkout / "secrets" / "postgres_password").stat().st_mode & 0o777 == 0o644
    assert (checkout / "secrets" / "redis" / "app").stat().st_mode & 0o777 == 0o644


@pytest.mark.parametrize("action", [["restart", "1m"], ["start"]])
def test_redis_reads_its_users_again_after_the_stack_is_up(action):
    code, stack, _ = run(action)

    assert code == 0
    # An update may add a Redis user without recreating Redis.
    assert stack.reloaded_at >= stack.up_call[0]


def test_a_failed_reload_says_how_to_fix_it(capsys):
    clock = FakeClock()
    stack = FakeStack(clock)
    stack.reload_problem = "NOPERM"

    assert server.main(["start"], stack, clock) == 0
    assert "docker compose restart redis" in capsys.readouterr().out


def test_reloading_redis_users_runs_the_start_script():
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout="OK\n", stderr="")

    assert server.ComposeStack([], run=fake_run).reload_redis_users() == ""
    assert calls == [["docker", "compose", "exec", "-T", "redis", "sh", "/redis/start.sh", "reload"]]
    refused = server.ComposeStack([], run=lambda command, **kwargs: SimpleNamespace(returncode=0, stdout="ERR Error in user declaration", stderr=""))
    assert refused.reload_redis_users() == "ERR Error in user declaration"


def test_restart_creates_missing_files_before_announcing(monkeypatch):
    def fails():
        raise RuntimeError("secrets/redis/app is a folder, not a file")

    clock = FakeClock()
    stack = FakeStack(clock)
    monkeypatch.setattr(stack, "prepare_files", fails)

    assert server.main(["restart", "5m"], stack, clock) == 1
    assert stack.calls == [] and stack.up_call is None


def test_redis_cli_signs_in_as_operator_without_showing_the_password():
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout="OK\n", stderr="")

    assert server.ComposeStack([], run=fake_run).redis("config", "set", "appendonly", "yes") == "OK"
    [command] = calls
    assert command[:6] == ["docker", "compose", "exec", "-T", "redis", "sh"]
    assert command[6:8] == ["-c", server.STACKS.REDIS_CLI]
    assert command[8:] == ["redis-cli", "config", "set", "appendonly", "yes"]
    assert "REDISCLI_AUTH" in server.STACKS.REDIS_CLI and "--pass" not in server.STACKS.REDIS_CLI


@pytest.mark.parametrize("answer", ["NOPERM User app has no permissions", "AUTH failed: WRONGPASS invalid", "ERR unknown command"])
def test_a_refused_redis_command_is_an_error(answer):
    stack = server.ComposeStack([], run=lambda command, **kwargs: SimpleNamespace(returncode=0, stdout=answer, stderr=""))

    with pytest.raises(RuntimeError, match="Redis refused"):
        stack.redis("config", "set", "appendonly", "yes")


def test_root_owned_folder_explains_the_fix(checkout, monkeypatch):
    (checkout / "secrets" / "postgres_password").mkdir(parents=True)

    def denied(self):
        raise PermissionError("Permission denied")

    monkeypatch.setattr(Path, "rmdir", denied)

    with pytest.raises(RuntimeError, match=r"sudo rm -r secrets/postgres_password"):
        server.prepare_files(checkout)


def test_folder_with_contents_is_not_removed(checkout):
    (checkout / "secrets" / "postgres_password").mkdir(parents=True)
    (checkout / "secrets" / "postgres_password" / "kept").write_text("x", encoding="utf-8")

    with pytest.raises(RuntimeError, match="is a folder, not a file"):
        server.prepare_files(checkout)
    assert (checkout / "secrets" / "postgres_password" / "kept").exists()


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


# --- Restore ------------------------------------------------------------------

KEY = "AGE-SECRET-KEY-1EXAMPLE"


class RestoreStack(FakeStack):
    def __init__(self, clock, fetch_code=0, backup_code=0, restore_code=0, **options):
        super().__init__(clock, **options)
        self.steps = []
        self.fetch_code = fetch_code
        self.backup_code = backup_code
        self.restore_code = restore_code
        self.root = Path("/srv/piano")

    def fetch_backup(self, which, key):
        self.steps.append(("fetch", which, key))
        if self.fetch_code:
            return self.fetch_code, "No off-site backup named billing-9."
        return 0, "Backup from 2026-10-27 16:47 UTC saved and checked: /billing/restore/billing-1793116030.dump"

    def host_path(self, container_path):
        return server.ComposeStack.host_path(self, container_path)

    def start_postgres(self):
        self.steps.append(("postgres",))
        return 0

    def stop_services(self, services):
        self.steps.append(("stop", tuple(services)))
        return 0

    def backup_now(self):
        self.steps.append(("backup",))
        return self.backup_code

    def restore_dump(self, dump):
        self.steps.append(("restore", dump))
        return self.restore_code

    def up(self, command=None, build=True):
        self.steps.append(("up",))
        return super().up(command, build)


@pytest.fixture
def terminal(monkeypatch):
    """A person at a terminal who types the key and answers the question."""
    answers = {"key": KEY, "confirm": True, "tty": True}
    monkeypatch.setattr(server, "read_key", lambda: answers["key"])
    monkeypatch.setattr(server, "confirm", lambda question: answers["confirm"])
    monkeypatch.setattr(server.sys.stdin, "isatty", lambda: answers["tty"], raising=False)
    return answers


def restore_run(arguments, **options):
    clock = FakeClock()
    stack = RestoreStack(clock, **options)
    return server.main(arguments, stack, clock), stack


def test_restore_fetches_first_then_backs_up_and_restores(terminal, capsys):
    code, stack = restore_run(["restore"])

    assert code == 0
    dump = Path("/srv/piano/billing-data/restore/billing-1793116030.dump")
    assert stack.steps == [
        ("fetch", "latest", KEY), ("postgres",), ("stop", tuple(server.BILLING_SERVICES)), ("backup",),
        ("restore", dump), ("up",),
    ]
    assert "the API is ready" in capsys.readouterr().out


@pytest.mark.parametrize("which", ["3", "billing-1793116030.dump.age"])
def test_restore_passes_the_chosen_backup(terminal, which):
    _, stack = restore_run(["restore", which])

    assert stack.steps[0] == ("fetch", which, KEY)


def test_declined_restore_changes_nothing(terminal, capsys):
    terminal["confirm"] = False

    code, stack = restore_run(["restore"])

    assert code == 0
    assert [step[0] for step in stack.steps] == ["fetch"]
    assert "nothing was changed" in capsys.readouterr().out


def test_failed_fetch_changes_nothing(terminal):
    code, stack = restore_run(["restore", "9"], fetch_code=1)

    assert code == 1
    assert [step[0] for step in stack.steps] == ["fetch"]


def test_missing_key_changes_nothing(terminal):
    terminal["key"] = "\n"

    code, stack = restore_run(["restore"])

    assert code == 1
    assert stack.steps == []


def test_piped_key_needs_yes(terminal):
    terminal["tty"] = False

    code, stack = restore_run(["restore"])
    assert code == 1
    assert [step[0] for step in stack.steps] == ["fetch"]

    code, stack = restore_run(["restore", "--yes"])
    assert code == 0
    assert stack.steps[-1] == ("up",)


def test_failed_safety_backup_restores_nothing(terminal, capsys):
    code, stack = restore_run(["restore"], backup_code=1)

    assert code == 1
    assert "restore" not in [step[0] for step in stack.steps]
    assert "nothing was restored" in capsys.readouterr().out


def test_failed_restore_still_starts_the_services(terminal, capsys):
    code, stack = restore_run(["restore"], restore_code=1)

    assert code == 1
    assert stack.steps[-1] == ("up",)
    assert "the database is unchanged" in capsys.readouterr().out


def test_key_is_sent_on_standard_input_only(tmp_path):
    calls = []

    def fake_run(command, **options):
        calls.append((command, options))
        return SimpleNamespace(returncode=0, stdout="saved and checked: /billing/restore/billing-1.dump\n", stderr="")

    stack = server.ComposeStack([], run=fake_run, root=tmp_path)
    stack.fetch_backup("2", KEY)

    command, options = calls[0]
    assert KEY not in " ".join(command)
    assert options["input"] == KEY
    assert command[-2:] == ["fetch", "2"]
    assert "env" not in options


def test_dump_is_streamed_into_pg_restore(tmp_path):
    dump = tmp_path / "billing-1.dump"
    dump.write_bytes(b"PGDMP")
    received = []

    def fake_run(command, **options):
        received.append((command, options["stdin"].read()))
        return SimpleNamespace(returncode=0)

    server.ComposeStack([], run=fake_run, root=tmp_path).restore_dump(dump)

    command, data = received[0]
    assert command[-len(server.STACKS.PG_RESTORE):] == server.STACKS.PG_RESTORE
    assert "--single-transaction" in command and "--clean" in command
    assert data == b"PGDMP"


def test_container_paths_map_to_billing_data(tmp_path):
    stack = server.ComposeStack([], root=tmp_path)

    assert stack.host_path("/billing/restore/billing-1.dump") == tmp_path / "billing-data" / "restore" / "billing-1.dump"


@pytest.fixture
def local_backups(tmp_path):
    folder = tmp_path / "billing-data" / "backups"
    folder.mkdir(parents=True)
    for name in ("billing-1000.sqlite3", "billing-2000.dump", "billing-3000.dump", "billing-3000.dump.tmp"):
        (folder / name).write_bytes(b"PGDMP")
    return folder


def local_run(arguments, root, **options):
    clock = FakeClock()
    stack = RestoreStack(clock, **options)
    stack.root = root
    return server.main(arguments, stack, clock), stack


def test_local_pattern_matches_the_backups():
    from app import backups

    assert server.LOCAL_BACKUP.pattern == backups.BACKUP_FILE_PATTERN.pattern


@pytest.mark.parametrize(("which", "name"), [("latest", "billing-3000.dump"), ("2", "billing-2000.dump"),
                                              ("billing-2000.dump", "billing-2000.dump")])
def test_local_restore_needs_no_key(terminal, local_backups, which, name):
    terminal["key"] = ""

    code, stack = local_run(["restore", which, "--from", "local"], local_backups.parent.parent)

    assert code == 0
    assert "fetch" not in [step[0] for step in stack.steps]
    assert ("restore", local_backups / name) in stack.steps


@pytest.mark.parametrize(("which", "problem"), [("3", "is a legacy SQLite backup"), ("4", "choose 1 (the newest) to 3"),
                                                 ("billing-9", "no local backup named billing-9")])
def test_unusable_local_backup_changes_nothing(terminal, local_backups, capsys, which, problem):
    code, stack = local_run(["restore", which, "--from", "local"], local_backups.parent.parent)

    assert code == 1
    assert stack.steps == []
    assert problem in capsys.readouterr().out


def test_failed_fetch_falls_back_to_local(terminal, local_backups, capsys):
    code, stack = local_run(["restore", "--fallback"], local_backups.parent.parent, fetch_code=1)

    assert code == 0
    assert stack.steps[0][0] == "fetch"
    assert ("restore", local_backups / "billing-3000.dump") in stack.steps
    assert "trying the local backups" in capsys.readouterr().out


def test_missing_key_falls_back_to_local(terminal, local_backups):
    terminal["key"] = ""

    code, stack = local_run(["restore", "--fallback"], local_backups.parent.parent)

    assert code == 0
    assert ("restore", local_backups / "billing-3000.dump") in stack.steps


def test_missing_local_backup_falls_back_to_remote(terminal, tmp_path, capsys):
    code, stack = local_run(["restore", "--from", "local", "--fallback"], tmp_path)

    assert code == 0
    assert stack.steps[0] == ("fetch", "latest", KEY)
    assert "trying the remote backups" in capsys.readouterr().out


def test_both_places_failing_changes_nothing(terminal, tmp_path, capsys):
    code, stack = local_run(["restore", "--fallback"], tmp_path, fetch_code=1)

    assert code == 1
    assert [step[0] for step in stack.steps] == ["fetch"]
    assert "there are no local backups in billing-data/backups: nothing was changed" in capsys.readouterr().out
