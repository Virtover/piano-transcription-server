import importlib.util
from pathlib import Path
from types import SimpleNamespace

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "admin.py"
spec = importlib.util.spec_from_file_location("admin_script", SCRIPT)
script = importlib.util.module_from_spec(spec)
spec.loader.exec_module(script)


def compose(run):
    return script.STACKS.ComposeStack([], run=run, root=script.ROOT)


def test_runs_the_command_in_the_api_container(monkeypatch):
    calls = []
    monkeypatch.setattr(script.getpass, "getuser", lambda: "chris")
    monkeypatch.setattr(script.sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(script.sys.stdout, "isatty", lambda: True, raising=False)

    def run(arguments, cwd, **options):
        calls.append((arguments, cwd, options))
        return SimpleNamespace(returncode=3)

    assert script.main(["user", "a@example.com", "credit", "5"], compose(run)) == 3

    [(arguments, cwd, options)] = calls
    assert cwd == script.ROOT
    assert options == {}
    assert arguments == [
        "docker", "compose", "exec",
        "-e", "ADMIN_ACTOR=chris",
        "-e", "ADMIN_COMMAND=python scripts/admin.py",
        "api", "python", "-m", "app.admin", "user", "a@example.com", "credit", "5",
    ]


def test_without_a_terminal_the_container_gets_none(monkeypatch):
    calls = []
    monkeypatch.setattr(script.sys.stdin, "isatty", lambda: False, raising=False)

    assert script.main(["status"], compose(lambda arguments, **options: calls.append(arguments)
                                           or SimpleNamespace(returncode=0))) == 0
    assert calls[0][:4] == ["docker", "compose", "exec", "-T"]


def test_another_entry_point_names_itself(monkeypatch):
    calls = []
    monkeypatch.setattr(script.sys.stdin, "isatty", lambda: False, raising=False)

    script.main(["status"], compose(lambda arguments, **options: calls.append(arguments)
                                    or SimpleNamespace(returncode=0)), command="python scripts/kube.py admin")
    assert "ADMIN_COMMAND=python scripts/kube.py admin" in calls[0]


def test_the_actor_is_a_plain_user_name(monkeypatch):
    for name, expected in [
        ("chris", "chris"),
        ("DOMAIN\\Chris Smith", "DOMAIN-Chris-Smith"),
        ("sam@example.com", "sam-example.com"),
        ("", "admin"),
    ]:
        monkeypatch.setattr(script.getpass, "getuser", lambda: name)
        assert script.actor() == expected

    def unknown():
        raise OSError("no user name")

    monkeypatch.setattr(script.getpass, "getuser", unknown)
    assert script.actor() == "admin"


def test_without_docker(capsys):
    def run(arguments, **options):
        raise FileNotFoundError("docker")

    assert script.main(["status"], compose(run)) == 1
    assert "docker was not found" in capsys.readouterr().err
