"""The worker services' supervisor, with Docker Compose on the hub and on
the nodes of a home cluster (app/cluster.py, docs/cluster.md).

It runs Dramatiq as child processes, one per kind of work, with the queues
the cluster's switches allow this device's service now:

* `local`: the default and arrangement queues (transcribe, arrange)
* `checks`: link checks for the hub (check), on nodes only
* `remote`: the remote queue, which sends jobs to RunPod (dispatch)

Every STATUS_INTERVAL it reads the switches again and publishes what it
runs. A child whose work is switched off gets SIGTERM: Dramatiq takes no new
job and finishes those it runs, so nothing is interrupted. A child that
exits on its own is started again, waiting longer each time.

On a node it first takes the hub's settings and waits, doing nothing,
while the hub speaks another protocol version. When the hub's settings
change, it lets its children finish their jobs and starts again with them.

`python -m app.worker.supervisor drain` (scripts/server.py stop on a node)
lets the children finish and keeps them stopped until the container stops.
"""

import os
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from redis import Redis

from app import cluster
from app.config import ConfigError, ensure_valid, settings
from app.error_report import SERVICE_ENVIRONMENT


# This service's name in the cluster's status, by role.
SERVICE_NAMES = {"local": "worker", "remote": "remote-worker", "node": "worker"}
# What each role's service can do, before the switches.
OFFERED = {"local": ("transcribe", "arrange"), "remote": ("dispatch",), "node": cluster.TASKS}
# Created by `drain`, removed when the supervisor starts: in the container's
# own file system, so a restarted container takes work again.
DRAIN_FILE = Path("/tmp/worker-drain")
CHECK_THREADS = 2
RESTART_BACKOFF = (5, 15, 30, 60)
HUB_WAIT_REPORT = 60


@dataclass
class Child:
    group: str
    queues: tuple[str, ...]
    process: subprocess.Popen
    stopping: bool = False
    started: float = field(default_factory=time.monotonic)


def say(message: str) -> None:
    print(f"[supervisor] {message}", flush=True)


def redis_client() -> Redis:
    return Redis.from_url(settings.service.redis_url, decode_responses=True, **settings.service.redis_options())


# --- On a node: the hub's settings and protocol ------------------------------------

def wait_for_hub(redis: Redis, sleep=time.sleep) -> dict:
    """What the hub published, waiting while it cannot be reached or has not
    published yet, as while the hub starts or its network is down."""
    reported = 0.0
    while True:
        try:
            info = cluster.hub_info(redis)
            if info is not None:
                return info
            problem = "the hub has not published its settings yet; is its API running?"
        except Exception as error:
            problem = f"cannot reach the hub's Redis ({type(error).__name__})"
        if time.monotonic() - reported >= HUB_WAIT_REPORT:
            say(f"waiting: {problem}")
            reported = time.monotonic()
        sleep(5)


def take_hub_settings(redis: Redis, arguments: list[str], execve=os.execve) -> dict:
    """On a node: run with the hub's settings. The first time, and whenever
    they changed, the process starts again with them in its environment;
    otherwise this returns what the hub published."""
    info = wait_for_hub(redis)
    if os.environ.get(cluster.SYNCED_HASH_VARIABLE) != cluster.settings_hash(info["settings"]):
        environment = cluster.apply_hub_settings(dict(os.environ), info["settings"])
        say("starting with the hub's settings")
        execve(sys.executable, [sys.executable, "-m", "app.worker.launcher", *arguments], environment)
    return info


# --- The children -----------------------------------------------------------------

def dramatiq_command(queues: tuple[str, ...], threads: int) -> list[str]:
    dramatiq = shutil.which("dramatiq")
    if dramatiq is None:
        raise RuntimeError("The dramatiq executable is not installed")
    return [dramatiq, "app.worker.tasks", "--processes", "1", "--threads", str(threads), "--queues", *queues]


class Supervisor:
    def __init__(self, role: str, redis: Redis, popen=subprocess.Popen, clock=time.monotonic):
        self.role = role
        self.redis = redis
        self.popen = popen
        self.clock = clock
        self.node = cluster.node_name()
        self.service = SERVICE_NAMES[role]
        self.children: dict[str, Child] = {}
        self.failures: dict[str, int] = {}
        self.retry_at: dict[str, float] = {}
        self.stopping = False
        self.allowed: list[str] = []
        self.state = "starting"
        self.problem = ""
        self.restart_for_settings = False
        self.local_plan = None
        self.remote_plan = None
        self.offered = self.capabilities()

    # What this service can do here.
    def capabilities(self) -> list[str]:
        from app.worker import launcher

        offered = list(OFFERED[self.role])
        if any(task in offered for task in ("transcribe", "arrange")):
            try:
                ensure_valid("worker")
            except ConfigError as error:
                raise SystemExit(str(error)) from None
            self.local_plan = launcher.plan_worker(launcher.cuda_job_capacities())
            for warning in self.local_plan.warnings:
                say(f"Warning: {warning}")
            if not self.local_plan.arrangement:
                offered = [task for task in offered if task != "arrange"]
        if "check" in offered and not cluster.is_node():
            # The hub checks links itself, from its own address.
            offered.remove("check")
        if "dispatch" in offered:
            if settings.outsource.provider == "none":
                offered.remove("dispatch")
                self.problem = "outsourcing is off (OUTSOURCE_PROVIDER=none)"
            else:
                try:
                    ensure_valid("remote")
                    self.remote_plan = launcher.remote_plan()
                except ConfigError as error:
                    offered.remove("dispatch")
                    self.problem = f"cannot send jobs to RunPod: {error}"
                    say(self.problem)
        return offered

    def wanted(self) -> dict[str, tuple[tuple[str, ...], int, dict[str, str]]]:
        """Group -> (queues, threads, extra environment) for what is allowed."""
        from app.worker import launcher

        wanted = {}
        allowed = set(self.allowed)
        local_queues = tuple(
            queue for task, queue in (("transcribe", launcher.LOCAL_QUEUE), ("arrange", launcher.ARRANGEMENT_QUEUE))
            if task in allowed
        )
        if local_queues and self.local_plan is not None:
            environment = {SERVICE_ENVIRONMENT: "worker"}
            if self.local_plan.cpu_threads:
                environment.update(OMP_NUM_THREADS=str(self.local_plan.cpu_threads),
                                   MKL_NUM_THREADS=str(self.local_plan.cpu_threads))
            wanted["local"] = (local_queues, self.local_plan.threads, environment)
        if "check" in allowed:
            wanted["checks"] = ((launcher.CHECK_QUEUE,), CHECK_THREADS, {SERVICE_ENVIRONMENT: "worker"})
        if "dispatch" in allowed and self.remote_plan is not None:
            wanted["remote"] = ((launcher.REMOTE_QUEUE,), self.remote_plan.threads, {SERVICE_ENVIRONMENT: "remote-worker"})
        return wanted

    def start_child(self, group: str, queues: tuple[str, ...], threads: int, environment: dict[str, str]) -> None:
        if self.clock() < self.retry_at.get(group, 0):
            return
        command = dramatiq_command(queues, threads)
        say(f"starting {group} on queue(s) {', '.join(queues)} with {threads} thread(s)")
        process = self.popen(command, env={**os.environ, **environment})
        self.children[group] = Child(group, queues, process)

    def stop_child(self, child: Child) -> None:
        if not child.stopping:
            say(f"stopping {child.group}: its running jobs finish first")
            child.stopping = True
            try:
                child.process.send_signal(signal.SIGTERM)
            except (OSError, ProcessLookupError):
                pass

    def reap(self) -> None:
        for group, child in list(self.children.items()):
            code = child.process.poll()
            if code is None:
                continue
            del self.children[group]
            if child.stopping:
                say(f"{group} stopped")
                self.failures.pop(group, None)
                continue
            # Ended on its own: start it again, waiting longer each time.
            failures = self.failures.get(group, 0)
            if self.clock() - child.started > 10 * 60:
                failures = 0
            delay = RESTART_BACKOFF[min(failures, len(RESTART_BACKOFF) - 1)]
            self.failures[group] = failures + 1
            self.retry_at[group] = self.clock() + delay
            say(f"{group} exited with code {code}; starting it again in {delay} s")

    def read_switches(self) -> None:
        try:
            self.allowed = cluster.allowed_tasks(self.redis, self.node, self.offered)
            self.state = "running" if self.allowed else "off"
        except Exception as error:
            # Keep doing what was allowed while Redis is away; Dramatiq
            # reconnects by itself.
            self.state = f"cannot reach Redis ({type(error).__name__})"
        if DRAIN_FILE.exists():
            self.allowed = []
            self.state = "drained"

    def check_hub(self) -> None:
        """On a node: stop taking work while the hub speaks another protocol,
        and start again when its settings change."""
        if not cluster.is_node():
            return
        try:
            info = cluster.hub_info(self.redis)
        except Exception:
            return
        if info is None:
            return
        if info["protocol"] != cluster.PROTOCOL:
            self.allowed = []
            self.state = (f"waiting for an update: the hub speaks protocol {info['protocol']}, this node "
                          f"{cluster.PROTOCOL}. Update this device with: python scripts/server.py update")
            return
        if os.environ.get(cluster.SYNCED_HASH_VARIABLE) != cluster.settings_hash(info["settings"]):
            if not self.restart_for_settings:
                say("the hub's settings changed: finishing the running jobs, then starting again with them")
            self.restart_for_settings = True

    def step(self) -> None:
        self.reap()
        self.read_switches()
        self.check_hub()
        wanted = {} if (self.stopping or self.restart_for_settings) else self.wanted()
        for group, child in list(self.children.items()):
            if group not in wanted or wanted[group][0] != child.queues:
                self.stop_child(child)
        for group, (queues, threads, environment) in wanted.items():
            if group not in self.children:
                self.start_child(group, queues, threads, environment)
        self.publish()

    def running(self) -> list[str]:
        from app.worker import launcher

        tasks = set()
        by_queue = {launcher.LOCAL_QUEUE: "transcribe", launcher.ARRANGEMENT_QUEUE: "arrange",
                    launcher.CHECK_QUEUE: "check", launcher.REMOTE_QUEUE: "dispatch"}
        for child in self.children.values():
            if not child.stopping:
                tasks.update(by_queue[queue] for queue in child.queues if queue in by_queue)
        return [task for task in cluster.TASKS if task in tasks]

    def publish(self) -> None:
        try:
            summary = self.local_plan.summary if self.local_plan else (self.remote_plan.summary if self.remote_plan else "")
            cluster.publish_status(self.redis, self.node, self.service, {
                "state": self.state,
                "offered": self.offered,
                "allowed": self.allowed,
                "running": self.running(),
                "stopping": [child.group for child in self.children.values() if child.stopping],
                "summary": summary,
                "problem": self.problem,
                "egress": cluster.egress(self.redis) or "",
                "address": cluster.egress_address(self.redis, self.node) or "",
                "protocol": cluster.PROTOCOL,
                "code": cluster.code_version(),
                "deployment": settings.service.deployment,
            })
        except Exception as error:
            if "Redis" not in self.state:
                say(f"could not publish this service's status ({type(error).__name__})")

    def finished(self) -> bool:
        return not self.children

    def run(self, arguments: list[str], sleep=time.sleep) -> None:
        signal.signal(signal.SIGTERM, self.on_signal)
        signal.signal(signal.SIGINT, self.on_signal)
        say(f"{self.node}/{self.service} offers {', '.join(self.offered) or 'nothing'}"
            + (f" ({self.problem})" if self.problem else ""))
        while True:
            self.step()
            if (self.stopping or self.restart_for_settings) and self.finished():
                break
            sleep(1 if (self.stopping or self.restart_for_settings) else cluster.STATUS_INTERVAL)
        if self.stopping:
            try:
                cluster.clear_status(self.redis, self.node, self.service)
            except Exception:
                pass
            say("stopped")
            sys.exit(0)
        # New settings from the hub: start again with them.
        os.execv(sys.executable, [sys.executable, "-m", "app.worker.launcher", *arguments])

    def on_signal(self, signum, frame) -> None:
        if not self.stopping:
            say("stopping: the running jobs finish first")
        self.stopping = True
        for child in self.children.values():
            self.stop_child(child)


def drain(timeout: float, sleep=time.sleep) -> int:
    """Let this container's workers finish their jobs and take no more; used
    before a node is stopped. Returns 0 once they have, 1 if timeout passed."""
    DRAIN_FILE.touch()
    redis = redis_client()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            states = [status for status in cluster.statuses(redis)
                      if status["node"] == cluster.node_name() and status["service"] == "worker"]
        except Exception:
            states = []
        if states and all(status.get("state") == "drained" and not status.get("running") and not status.get("stopping")
                          for status in states):
            print("drained: no job is running here", flush=True)
            return 0
        sleep(2)
    print("the running jobs did not finish in time; they are queued again elsewhere when this device stops",
          flush=True)
    return 1


def main(role: str, arguments: list[str]) -> None:
    DRAIN_FILE.unlink(missing_ok=True)
    redis = redis_client()
    if cluster.is_node():
        take_hub_settings(redis, arguments)
    Supervisor(role, redis).run(arguments)


if __name__ == "__main__":
    if sys.argv[1:2] == ["drain"]:
        from app.config import duration_seconds

        sys.exit(drain(duration_seconds(sys.argv[2] if len(sys.argv) > 2 else "15m", "timeout")))
    raise SystemExit("usage: python -m app.worker.supervisor drain [TIMEOUT]")
