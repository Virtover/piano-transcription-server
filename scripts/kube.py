"""The server on Kubernetes, from this machine.

    python scripts/kube.py local up       # a test cluster on this PC, everything inside it
    python scripts/kube.py up             # a real cluster: managed Redis, PostgreSQL, bucket
    python scripts/kube.py status         # pods, KEDA, queue, leader, problems
    python scripts/kube.py update         # new images, rolling update
    python scripts/kube.py restart 5m     # as scripts/server.py: announce, build, drain, restart
    python scripts/kube.py admin jobs     # as scripts/admin.py, in an API pod
    python scripts/kube.py down

`local` before a command acts on the test cluster (deploy/kubernetes/local)
instead of the real one (deploy/kubernetes). Also: check (check --online in
the cluster), logs <deployment>, and build <tag> (the images alone).

restart, stop, start, restore, and cancel are scripts/server.py's commands,
and admin runs scripts/admin.py's, both through KubeStack: what they do is
written once, there. stop pauses KEDA and scales the server's deployments to
none, start applies the manifests again, and a restart is a rolling one. The
cluster's Redis is managed, or the test cluster's, which keeps nothing, so a
restart neither saves nor reloads it. restore refuses: the database's
provider keeps its backups.

Needs Python 3, kubectl signed in to the cluster, and docker for the images;
the test cluster is Docker Desktop's Kubernetes, which uses the images built
here, and gets KEDA installed for its scaling. See docs/kubernetes.md.

up and update copy the settings and Secrets themselves, and ask once,
naming the kubectl context, before they change the cluster or push an image.
Secrets are never printed: each is made from files by kubectl --dry-run and
applied from memory. Files only a cluster uses are in secrets/kubernetes,
and the test cluster's generated ones in secrets/kubernetes-local, so the
Compose stack on this machine never reads them.
"""

import argparse
import hashlib
import importlib.util
import json
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parent.parent
MANIFESTS = ROOT / "deploy" / "kubernetes"
# Inside the project's secrets folder: what only a real cluster uses, and the
# test cluster's generated passwords.
CLUSTER_SECRETS = Path("kubernetes")
LOCAL_SECRETS = Path("kubernetes-local")
# KEDA for the test cluster, from its release; a real cluster brings its own.
KEDA_VERSION = "2.16.1"
KEDA_MANIFEST = f"https://github.com/kedacore/keda/releases/download/v{KEDA_VERSION}/keda-{KEDA_VERSION}.yaml"
IMAGES = {"piano-api": "Dockerfile.api", "piano-worker": "Dockerfile.worker"}
LOCAL_CONTEXTS = ("docker-desktop",)
# The test cluster's own services in deploy/kubernetes/local/services.yaml,
# which stop and restart leave running: its Redis keeps the queue only while
# it runs. A test keeps the list in step.
INFRASTRUCTURE = ("redis", "postgres", "seaweed")
# KEDA keeps a ScaledObject's target at this many replicas while it is set.
KEDA_PAUSED = "autoscaling.keda.sh/paused-replicas"

# The test cluster's settings over server.env's: its own services, no
# outsourcing, no billing, and no reports to the real error monitor.
LOCAL_SETTINGS = {
    "REDIS_URL": "redis://app@redis:6379/0",
    "STORAGE_ENDPOINT": "http://host.docker.internal:8333",
    "STORAGE_BUCKET": "piano-local",
    "STORAGE_REGION": "us-east-1",
    "OUTSOURCE_PROVIDER": "none",
    "BILLING_PROVIDER": "none",
}
LOCAL_UNSET = ("ERROR_REPORT_URL", "OFFSITE_BACKUP_BUCKET")


def load_script(name: str):
    spec = importlib.util.spec_from_file_location(f"{name}_script", Path(__file__).with_name(f"{name}.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


SETUP_SCRIPT = load_script("setup")
Console, Style, EnvFile = SETUP_SCRIPT.Console, SETUP_SCRIPT.Style, SETUP_SCRIPT.EnvFile
# The scripts whose commands kube.py runs on a cluster.
SERVER_SCRIPT = SETUP_SCRIPT.SERVER_SCRIPT
ADMIN_SCRIPT = load_script("admin")


@dataclass(frozen=True)
class SecretFile:
    """A file a Secret holds: its key there, its place in the secrets folder,
    and whether the deployment needs it."""

    key: str
    path: Path
    needed: bool = False
    description: str = ""
    # Any random value works.
    generated: bool = False


# The real cluster's Secrets. piano-secrets is mounted at /run/secrets in the
# API and workers, as ./secrets is with Compose: SecretSettings in
# app/config.py, which a test compares. Off-site backups' keys are left out:
# Kubernetes leaves backups to the database's provider.
CLUSTER_SECRET_FILES: dict[str, list[SecretFile]] = {
    "piano-secrets": [
        SecretFile("database_url", CLUSTER_SECRETS / "database_url", needed=True,
                   description="the managed PostgreSQL's URL, with its password and sslmode=verify-full"),
        SecretFile("storage_access_key", Path("storage_access_key"), needed=True,
                   description="the bucket's access key ID"),
        SecretFile("storage_secret_key", Path("storage_secret_key"), needed=True,
                   description="the bucket's secret access key"),
        SecretFile("metrics_token", Path("metrics_token"), needed=True,
                   description="the token KEDA reads /metrics with", generated=True),
        SecretFile("google_play_service_account.json", Path("google_play_service_account.json")),
        SecretFile("runpod_api_key", Path("runpod_api_key")),
        SecretFile("runpod_read_api_key", Path("runpod_read_api_key")),
        SecretFile("server_password", Path("server_password")),
        SecretFile("error_report_token", Path("error_report_token")),
        SecretFile("redis_ca.crt", CLUSTER_SECRETS / "redis_ca.crt"),
        SecretFile("postgres_ca.crt", CLUSTER_SECRETS / "postgres_ca.crt"),
    ],
    "piano-redis": [
        SecretFile("app", CLUSTER_SECRETS / "redis_app_password", needed=True,
                   description="the password of the managed Redis's user app"),
    ],
    "piano-cloudflare": [
        SecretFile("tunnel_token", Path("cloudflare") / "tunnel_token", needed=True,
                   description="the Cloudflare Tunnel's token"),
    ],
}
# Only with the admin web page's component.
ADMIN_SECRET_FILES = [
    SecretFile("redis_url", CLUSTER_SECRETS / "admin_redis_url", needed=True,
               description="rediss://admin-read-only@<host>:<port>/0, or admin-read-write"),
    SecretFile("redis_password", CLUSTER_SECRETS / "admin_redis_password", needed=True,
               description="the password of that Redis user"),
    SecretFile("database_url", CLUSTER_SECRETS / "admin_database_url", needed=True,
               description="the URL of its PostgreSQL role, with password and TLS options"),
]
# The test cluster's, all generated: services/*.yaml in deploy/kubernetes/local read them.
LOCAL_SECRET_FILES: dict[str, list[SecretFile]] = {
    "piano-secrets": [
        SecretFile("database_url", LOCAL_SECRETS / "database_url", needed=True),
        SecretFile("storage_access_key", LOCAL_SECRETS / "storage_access_key", needed=True),
        SecretFile("storage_secret_key", LOCAL_SECRETS / "storage_secret_key", needed=True),
        SecretFile("metrics_token", LOCAL_SECRETS / "metrics_token", needed=True),
    ],
    "piano-redis": [
        SecretFile("app", LOCAL_SECRETS / "redis_app_password", needed=True),
        SecretFile("users.acl", LOCAL_SECRETS / "users.acl", needed=True),
    ],
    "piano-postgres": [SecretFile("password", LOCAL_SECRETS / "postgres_password", needed=True)],
    "piano-seaweed": [SecretFile("s3.json", LOCAL_SECRETS / "s3.json", needed=True)],
}


# --- The two clusters ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Target:
    """The real cluster or the test cluster on this PC: their manifests."""

    local: bool
    manifests: Path

    @property
    def name(self) -> str:
        return "the test cluster on this PC" if self.local else "the cluster"

    @property
    def kustomization(self) -> Path:
        return self.manifests / "kustomization.yaml"

    @property
    def settings(self) -> Path:
        return self.manifests / "settings.env"

    def text(self) -> str:
        return self.kustomization.read_text(encoding="utf-8")

    def namespace(self) -> str:
        match = re.search(r"^namespace:\s*(\S+)", self.text(), re.M)
        return match.group(1) if match else "default"

    def admin_component(self) -> bool:
        return not self.local and bool(re.search(r"^components:\s*\n\s+- components/admin-web", self.text(), re.M))

    def secret_files(self) -> dict[str, list[SecretFile]]:
        if self.local:
            return LOCAL_SECRET_FILES
        files = dict(CLUSTER_SECRET_FILES)
        if self.admin_component():
            files["piano-admin-web"] = ADMIN_SECRET_FILES
        return files

    def images(self) -> dict[str, tuple[str, str]]:
        """Each image's name and tag, as the kustomization sets them."""
        found = {}
        for name in IMAGES:
            match = re.search(rf"- name: {name}\n\s+newName: (\S+)\n\s+newTag: \"?([^\"\n]+)\"?", self.text())
            if match:
                found[name] = (match.group(1), match.group(2))
        return found

    def registry(self) -> str | None:
        """Where the real cluster's images are pushed; None for the test
        cluster, whose images stay on this machine."""
        if self.local:
            return None
        repository = self.images().get("piano-api", ("",))[0]
        return repository.rsplit("/", 1)[0] if "/" in repository else None


CLUSTER = Target(local=False, manifests=MANIFESTS)
LOCAL = Target(local=True, manifests=MANIFESTS / "local")


def set_images(text: str, registry: str | None, tag: str) -> str:
    """A kustomization with the images at tag, in registry when one is given."""
    for name in IMAGES:
        pattern = rf"(- name: {name}\n\s+newName: )(\S+)(\n\s+newTag: )[^\n]*"

        def replace(match):
            repository = match.group(2).rsplit("/", 1)[-1]
            image = f"{registry.rstrip('/')}/{repository}" if registry else repository
            return f'{match.group(1)}{image}{match.group(3)}"{tag}"'

        text, count = re.subn(pattern, replace, text)
        if count != 1:
            raise SystemExit(f"The kustomization has no image {name} to change")
    return text


def redis_users(password: str, users_acl: Path) -> str:
    """The Redis users of redis/users.acl that the services need, with the
    password as its SHA-256 hash, as redis/start.sh gives them to Compose."""
    digest = hashlib.sha256(password.encode()).hexdigest()
    lines = [line for line in users_acl.read_text(encoding="utf-8").splitlines()
             if line.startswith(("user default ", "user app "))]
    return "\n".join(line.replace("#{app}", f"#{digest}") for line in lines) + "\n"


def seaweed_config(access_key: str, secret_key: str) -> str:
    """SeaweedFS's S3 users: one, with the storage keys."""
    return json.dumps({"identities": [{
        "name": "server",
        "credentials": [{"accessKey": access_key, "secretKey": secret_key}],
        "actions": ["Admin", "Read", "Write", "List"],
    }]})


# --- kubectl and docker --------------------------------------------------------------------------

def run(command: list[str], input: str | None = None, capture: bool = True, stdin=None) -> subprocess.CompletedProcess:
    if stdin is not None:
        return subprocess.run(command, cwd=ROOT, stdin=stdin, capture_output=capture)
    return subprocess.run(command, cwd=ROOT, input=input, capture_output=capture, text=True)


@dataclass
class Kube:
    target: Target = CLUSTER
    console: Console = field(default_factory=Console)
    run: Callable[..., subprocess.CompletedProcess] = run
    root: Path = ROOT
    clock: Callable[[], str] = lambda: time.strftime("%Y%m%d-%H%M%S")

    @property
    def style(self) -> Style:
        return self.console.style

    def path(self, file: SecretFile) -> Path:
        return self.root / "secrets" / file.path

    def kubectl(self, *arguments: str, input: str | None = None, capture: bool = True, stdin=None
                ) -> subprocess.CompletedProcess:
        options = {"stdin": stdin} if stdin is not None else {}
        return self.run(["kubectl", "-n", self.target.namespace(), *arguments], input=input, capture=capture, **options)

    @property
    def stack(self) -> "KubeStack":
        """This target as server.py's and admin.py's commands see it."""
        return KubeStack(self)

    @property
    def script(self) -> str:
        return f"python scripts/kube.py{' local' if self.target.local else ''}"

    def context(self) -> str | None:
        result = self.run(["kubectl", "config", "current-context"])
        return result.stdout.strip() if result.returncode == 0 and result.stdout.strip() else None

    def get_json(self, *arguments: str) -> dict | None:
        result = self.kubectl("get", *arguments, "-o", "json")
        if result.returncode != 0 or not result.stdout.strip():
            return None
        return json.loads(result.stdout)

    def fail(self, message: str) -> int:
        self.console.say(self.style(message, "red"))
        return 1

    def right_context(self) -> str | None:
        """The kubectl context, if it fits the target: Docker Desktop's for
        the test cluster, anything else for a real one."""
        context = self.context()
        if context is None:
            self.fail("kubectl has no context: sign in to the cluster first.")
            return None
        if self.target.local and context not in LOCAL_CONTEXTS:
            self.fail(f"The test cluster runs on Docker Desktop's Kubernetes, but kubectl uses {context}. "
                      "Turn Kubernetes on in Docker Desktop, then: kubectl config use-context docker-desktop")
            return None
        if not self.target.local and context in LOCAL_CONTEXTS:
            self.fail(f"kubectl uses {context}, this PC's Kubernetes: for a test there, run "
                      "python scripts/kube.py local up; for a real cluster, choose its context first.")
            return None
        return context

    # --- Settings and secret files on this machine -------------------------------------------------

    def write_settings(self) -> bool:
        """settings.env: the server's settings, from server.env; with the test
        cluster's services, or the managed Redis's address."""
        settings_path = self.target.settings
        if not settings_path.exists():
            source = self.root / "server.env"
            shutil.copyfile(source if source.exists() else self.root / "server.env.example", settings_path)
        settings = EnvFile(settings_path)
        if self.target.local:
            for name, value in LOCAL_SETTINGS.items():
                settings.set(name, value)
            for name in LOCAL_UNSET:
                settings.unset(name)
        else:
            url = settings.get("REDIS_URL")
            while not url or not url.startswith(("redis://", "rediss://")) or "@redis:" in url:
                url = self.console.ask("The managed Redis's URL, with the services' user "
                                       "(rediss://app@<host>:<port>/0): ").strip()
            settings.set("REDIS_URL", url)
            if self.target.admin_component() and not settings.get("ADMIN_WEB_MODE"):
                mode = ""
                while mode not in ("read-only", "read-write"):
                    mode = self.console.ask("The admin web page's mode, read-only or read-write: ").strip()
                settings.set("ADMIN_WEB_MODE", mode)
        settings.save()
        return True

    def write_local_secret_files(self) -> None:
        """The test cluster's passwords, random, made once and kept: its
        database keeps its password from one up to the next."""
        local = self.root / "secrets" / LOCAL_SECRETS

        def made(name: str) -> str:
            path = local / name
            if not path.exists():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(secrets.token_hex(24), encoding="utf-8")
            return path.read_text(encoding="utf-8").strip()

        postgres, redis = made("postgres_password"), made("redis_app_password")
        access, secret = made("storage_access_key"), made("storage_secret_key")
        made("metrics_token")
        (local / "database_url").write_text(f"postgresql://billing:{postgres}@postgres:5432/billing", encoding="utf-8")
        (local / "users.acl").write_text(redis_users(redis, self.root / "redis" / "users.acl"), encoding="utf-8")
        (local / "s3.json").write_text(seaweed_config(access, secret), encoding="utf-8")

    def ensure_secret_files(self) -> bool:
        """Every file a Secret needs: generated for the test cluster; for a
        real one, asked for when missing, hidden, or generated where any
        value works."""
        if self.target.local:
            self.write_local_secret_files()
            return True
        style, missing = self.style, []
        for files in self.target.secret_files().values():
            for file in files:
                path = self.path(file)
                if not file.needed or (path.is_file() and path.stat().st_size):
                    continue
                relative = path.relative_to(self.root).as_posix()
                self.console.say(f"{style(relative, 'bold')}  {style(file.description, 'dim')}")
                if file.generated and self.console.yes("  Missing. Generate a random one?"):
                    value = secrets.token_urlsafe(32)
                else:
                    value = self.console.ask_hidden("  Missing. Paste it (hidden; Enter to stop): ").strip()
                if not value:
                    missing.append(relative)
                    continue
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(value, encoding="utf-8")
        if missing:
            self.fail(f"Still missing: {', '.join(missing)}.")
            self.console.wrap("python scripts/setup.py object_storage writes the storage keys, and tunnel the "
                              "tunnel's token.", "", "dim")
            return False
        return True

    # --- The cluster -----------------------------------------------------------------------------------

    def apply_secrets(self) -> bool:
        """Create or update every Secret from its files. Its YAML holds the
        values, so it goes from memory to kubectl and is never shown."""
        for name, files in self.target.secret_files().items():
            present = [file for file in files if self.path(file).is_file()]
            if not present:
                continue
            made = self.kubectl("create", "secret", "generic", name,
                                *[f"--from-file={file.key}={self.path(file)}" for file in present],
                                "--dry-run=client", "-o", "yaml")
            applied = self.kubectl("apply", "-f", "-", input=made.stdout) if made.returncode == 0 else made
            if applied.returncode != 0:
                return not self.fail(f"The Secret {name} could not be applied: {applied.stderr.strip()}")
            self.console.say(f"  {self.style('Secret', 'dim')} {name}: {', '.join(file.key for file in present)}")
        return True

    def image_registry(self) -> str | None:
        """Where the images go: None for the test cluster, which uses this
        machine's; asked for when the kustomization names none."""
        registry = self.target.registry()
        if not self.target.local and (registry is None or "example.com" in registry):
            registry = self.console.ask("Registry to push the images to, such as docker.io/<user>: ").strip().rstrip("/")
            if not registry:
                raise RuntimeError("No registry: the cluster cannot pull the images.")
        return registry

    def build_images(self, tag: str, registry: str | None = None) -> bool:
        if registry is None:
            try:
                registry = self.image_registry()
            except RuntimeError as error:
                return not self.fail(str(error))
        names = {name: f"{registry + '/' if registry else ''}{repository.rsplit('/', 1)[-1]}:{tag}"
                 for name, (repository, _) in self.target.images().items()}
        for name, image in names.items():
            self.console.say(f"  Building {self.style(image, 'bold')}")
            if self.run(["docker", "build", "-f", IMAGES[name], "-t", image, "."], capture=False).returncode != 0:
                return not self.fail(f"The build of {image} failed.")
        if registry:
            for image in names.values():
                if self.run(["docker", "push", image], capture=False).returncode != 0:
                    return not self.fail(f"Pushing {image} failed: is docker signed in to the registry?")
        self.target.kustomization.write_text(set_images(self.target.text(), registry, tag), encoding="utf-8")
        return True

    def roll_out(self, restart: bool = False) -> bool:
        """Apply the manifests, which also brings back what stop scaled
        down, and wait for every deployment; with restart, replace the
        server's pods even where nothing changed."""
        if self.run(["kubectl", "apply", "-k", str(self.target.manifests)], capture=False).returncode != 0:
            return not self.fail("kubectl apply failed.")
        # Fails harmlessly where KEDA or the annotation is missing.
        self.kubectl("annotate", "scaledobject", "--all", f"{KEDA_PAUSED}-")
        if restart:
            server = [f"deploy/{name}" for name in self.server_deployments()]
            if server and self.kubectl("rollout", "restart", *server, capture=False).returncode != 0:
                return not self.fail("kubectl rollout restart failed.")
        deployments = self.get_json("deployments") or {"items": []}
        for item in deployments["items"]:
            name = item["metadata"]["name"]
            if self.kubectl("rollout", "status", f"deploy/{name}", "--timeout=15m", capture=False).returncode != 0:
                return not self.fail(f"{name} did not become ready: python scripts/kube.py "
                                     f"{'local ' if self.target.local else ''}logs {name}")
        return True

    def server_deployments(self) -> list[str]:
        """The server's deployments, without the test cluster's own services."""
        items = (self.get_json("deployments") or {"items": []})["items"]
        return [item["metadata"]["name"] for item in items if item["metadata"]["name"] not in INFRASTRUCTURE]

    def scale_down(self) -> int:
        """Pause KEDA at none, then scale the server's deployments to none.
        roll_out brings both back."""
        self.kubectl("annotate", "scaledobject", "--all", f"{KEDA_PAUSED}=0", "--overwrite")
        server = [f"deploy/{name}" for name in self.server_deployments()]
        if not server:
            return 0
        return self.kubectl("scale", *server, "--replicas=0", capture=False).returncode

    def keda_installed(self) -> bool:
        return self.run(["kubectl", "get", "crd", "scaledobjects.keda.sh"]).returncode == 0

    def install_keda(self) -> bool:
        """KEDA for the test cluster's scaling, from its release."""
        self.console.say(f"  Installing KEDA {KEDA_VERSION}")
        if self.run(["kubectl", "apply", "--server-side", "-f", KEDA_MANIFEST], capture=False).returncode != 0:
            return not self.fail("KEDA could not be installed.")
        waited = self.run(["kubectl", "-n", "keda", "rollout", "status", "deploy/keda-operator", "--timeout=5m"],
                          capture=False)
        return waited.returncode == 0 or not self.fail("KEDA did not start.")

    def create_local_bucket(self) -> None:
        code = ("from app.config import settings; from app.outsource.storage import s3_client; "
                "c = s3_client(settings); b = settings.storage.bucket; "
                "b in [x['Name'] for x in c.list_buckets().get('Buckets', [])] or c.create_bucket(Bucket=b)")
        self.stack.exec("api", "python", "-c", code)

    def deploy(self, first: bool) -> int:
        """up and update: settings, secret files, images, Secrets, rollout,
        and the check, with one question before the cluster changes."""
        context = self.right_context()
        if context is None:
            return 1
        style = self.style
        action = "Deploy the server to" if first else "Update the server in"
        self.console.heading(f"{action} {self.target.name}")
        self.console.say(f"  kubectl context {style(context, 'bold')}, namespace {style(self.target.namespace(), 'bold')}")
        if not self.target.local:
            self.console.wrap("Before this: the managed Redis, PostgreSQL, and bucket that python -m app.config "
                              "requirements lists, the GPU nodes, and KEDA; see docs/kubernetes.md.", "  ", "dim")
        if not self.write_settings() or not self.ensure_secret_files():
            return 1
        pushes = "" if self.target.local else ", push them"
        keda = self.target.local and not self.keda_installed()
        also = f", install KEDA {KEDA_VERSION} for the scaling" if keda else ""
        if not self.console.yes(f"Build the images{pushes}{also}, copy the settings and Secrets, and apply, "
                                f"in context {context}?", default=False):
            return 1
        if keda and not self.install_keda():
            return 1
        if not self.build_images(self.clock()):
            return 1
        self.run(["kubectl", "create", "namespace", self.target.namespace()])
        if not self.apply_secrets() or not self.roll_out():
            return 1
        if self.target.local:
            self.create_local_bucket()
        result = self.check()
        if self.target.local:
            self.console.say(style("The API answers on http://localhost:8080.", "green", "bold"))
        return result

    def down(self) -> int:
        context = self.right_context()
        if context is None:
            return 1
        namespace = self.target.namespace()
        self.console.wrap(f"This deletes the namespace {namespace} in context {context}, with every pod and "
                          "Secret in it" + (", and the test cluster's database." if self.target.local else
                                            ". The managed Redis, PostgreSQL, and bucket are not touched."), "")
        if self.target.local:
            if not self.console.yes("Delete it?", default=False):
                return 1
        elif self.console.ask(f"Type {namespace} to delete it: ").strip() != namespace:
            return 1
        return self.run(["kubectl", "delete", "namespace", namespace, "--wait=true"], capture=False).returncode

    # --- Status ------------------------------------------------------------------------------------------

    def status(self) -> int:
        context = self.right_context()
        if context is None:
            return 1
        say, style = self.console.say, self.style
        say(f"{style(self.target.name.capitalize(), 'bold')}: context {style(context, 'cyan')}, "
            f"namespace {style(self.target.namespace(), 'cyan')}")
        deployments = (self.get_json("deployments") or {"items": []})["items"]
        say("")
        say(style("Deployments", "bold"))
        if not deployments:
            say(style(f"  none: python scripts/kube.py {'local ' if self.target.local else ''}up deploys them", "yellow"))
            return 0
        for item in deployments:
            wanted = item["spec"].get("replicas", 0)
            ready = item.get("status", {}).get("readyReplicas", 0)
            image = item["spec"]["template"]["spec"]["containers"][0]["image"]
            state = style(f"{ready}/{wanted} ready", "green" if ready == wanted else "yellow")
            say(f"  {item['metadata']['name']:15} {state}  {style(image, 'dim')}")

        scaled = self.get_json("hpa", "keda-hpa-worker")
        say("")
        if scaled is None:
            say(f"{style('Scaling', 'bold')}  {style('no KEDA scaler for the workers: is KEDA installed?', 'yellow')}")
        else:
            current = scaled.get("status", {}).get("currentReplicas", 0)
            desired = scaled.get("status", {}).get("desiredReplicas", 0)
            say(f"{style('Scaling', 'bold')}  KEDA: {current} workers, wants {desired}, "
                f"{scaled['spec'].get('minReplicas', 1)} to {scaled['spec'].get('maxReplicas')}")

        server = self.stack.exec("api", "python", "-m", "app.admin", "status", "--json")
        if server.returncode == 0:
            state = json.loads(server.stdout)
            say("")
            say(f"{style('Leader', 'bold')}   {state.get('api_leader') or style('none', 'yellow')}")
            worker = state.get("local_worker")
            say(f"{style('Workers', 'bold')}  " + (f"{worker['count']}, {worker['capacity']} jobs at once"
                                                   if worker else style("no heartbeat", "yellow")))
            waits = []
            for backend, queue in state.get("waiting", {}).items():
                text = f"{backend} {queue['waiting']} waiting"
                if queue["waiting"]:
                    text += f", the oldest {queue['oldest_wait_seconds']:.0f} s"
                waits.append(text)
            say(f"{style('Queue', 'bold')}    {'; '.join(waits)}")
            notice = self.stack.exec("api", "python", "-m", "app.maintenance", "status")
            planned = json.loads(notice.stdout or "null") if notice.returncode == 0 else None
            if planned:
                say(f"{style('Notice', 'bold')}   {planned['message']} (at {planned['at']})")

        self.secrets_status()
        self.problems()
        return 0

    def secrets_status(self) -> None:
        say, style = self.console.say, self.style
        present = {item["metadata"]["name"]: set(item.get("data", {}))
                   for item in (self.get_json("secrets") or {"items": []})["items"]}
        say("")
        say(style("Secrets", "bold") + style("  (names and keys only)", "dim"))
        for name, files in self.target.secret_files().items():
            needed = {file.key for file in files if file.needed}
            lacking = sorted(needed - present.get(name, set()))
            if name not in present:
                say(f"  {style('missing', 'yellow', 'bold')}  {name}")
            elif lacking:
                say(f"  {style('missing keys', 'yellow')}  {name}: {', '.join(lacking)}")
            else:
                say(f"  {style('present', 'green')}  {name}")

    def problems(self) -> None:
        """What the API in the cluster says is wrong, and the features
        Kubernetes needs or cannot run."""
        say, style = self.console.say, self.style
        result = self.stack.config("features", "--json")
        say("")
        say(style("Problems", "bold"))
        if result.returncode != 0:
            say(style("  the API does not answer", "yellow"))
            return
        found = [problem for feature in json.loads(result.stdout) for problem in feature["problems"]]
        check = self.stack.config("check")
        found += [line.strip()[2:] for line in check.stderr.splitlines() if line.strip().startswith("- ")]
        for problem in dict.fromkeys(found):
            self.console.wrap(f"! {problem}", "  ", "red")
        if not found:
            say(style("  none", "green"))

    # --- In the cluster --------------------------------------------------------------------------------

    def check(self) -> int:
        return self.stack.config("check", "--online", capture=False).returncode

    def logs(self, deployment: str, follow: bool) -> int:
        return self.kubectl("logs", f"deploy/{deployment}", "--tail=200", *(["-f"] if follow else []),
                            capture=False).returncode


class KubeStack(SERVER_SCRIPT.Stack):
    """A cluster's namespace as server.py's and admin.py's commands see it,
    through kubectl. A cluster has no one-off containers: those commands run
    in a running pod."""

    cli = "kubectl"
    cannot_restore = ("the cluster's database belongs to its provider, which keeps its backups: restore "
                      "with the provider's own restore; see docs/kubernetes.md")

    def __init__(self, kube: Kube, popen=subprocess.Popen):
        self.kube = kube
        self.root = kube.root
        self.script = kube.script
        self._popen = popen
        self.registry: str | None = None

    def exec(self, service, *command, input=None, stdin=None, capture=True, terminal=False, env=None):
        # Standard input only where it is used, as docker compose exec -T.
        interactive = ["-i"] if terminal or input is not None or stdin is not None or not capture else []
        variables = ["env", *(f"{name}={value}" for name, value in env.items())] if env else []
        return self.kube.kubectl("exec", *interactive, *(["-t"] if terminal else []), f"deploy/{service}", "--",
                                 *variables, *command, input=input, stdin=stdin, capture=capture)

    def one_off(self, service, *command, input=None, capture=True, terminal=False):
        return self.exec(service, *command, input=input, capture=capture, terminal=terminal)

    def prepare_files(self) -> list[str]:
        """The settings and secret files a rollout copies, asked for when
        missing, and where the images go, before anything is announced."""
        if not self.kube.right_context():
            raise RuntimeError("nothing was changed")
        if not self.kube.write_settings() or not self.kube.ensure_secret_files():
            raise RuntimeError("nothing was changed")
        self.registry = self.kube.image_registry()
        return []

    def start_build(self):
        """The images, built and pushed by `kube.py build` while the
        countdown runs; the rollout then uses them."""
        target = ["local"] if self.kube.target.local else []
        registry = ["--registry", self.registry] if self.registry else []
        return self._popen([sys.executable, str(Path(__file__).resolve()), *target, "build", self.kube.clock(),
                            *registry], cwd=self.root)

    def up(self, command: str | None = None, build: bool = True) -> int:
        if command:
            return self.kube.run(shlex.split(command, posix=(sys.platform != "win32")), capture=False).returncode
        return 0 if self.kube.apply_secrets() and self.kube.roll_out(restart=True) else 1

    def stop(self, down: bool) -> int:
        return self.kube.scale_down()

    def logs_command(self, service: str) -> str:
        return f"{self.script} logs {service}"


COMMANDS = ("up", "update", "down", "status", "check", "logs", "build", "admin")
# scripts/server.py's commands, run on the cluster; kube.py's status also
# shows the notice that server.py's status prints.
SERVER_COMMANDS = tuple(action for action in SERVER_SCRIPT.actions() if action not in COMMANDS)
# What changes the cluster, so it asks first, naming the kubectl context.
ASKS_FIRST = ("restart", "stop", "start")


def main(arguments: list[str], kube: Kube | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python scripts/kube.py", description=__doc__.split("\n\n")[0])
    parser.add_argument("words", nargs="+", metavar="[local] command",
                        help=f"one of {', '.join(COMMANDS + SERVER_COMMANDS)}; local first for the test cluster "
                             "on this PC")
    parser.add_argument("-f", "--follow", action="store_true", help="with logs: keep following")
    parser.add_argument("--registry", help="with build: where to push the images")
    # admin's and server.py's options are theirs: everything after the
    # command goes to them unparsed.
    local = arguments[:1] == ["local"]
    words = arguments[1:] if local else arguments
    if words and words[0] in SERVER_COMMANDS + ("admin",):
        command, extra = words[0], words[1:]
    else:
        args, rest = parser.parse_known_args(words)
        words = args.words + rest
        if not words or words[0] not in COMMANDS:
            parser.error(f"choose one of {', '.join(COMMANDS + SERVER_COMMANDS)}")
        command, extra = words[0], words[1:]

    if kube is None:
        if shutil.which("kubectl") is None:
            print("kubectl is needed, signed in to the cluster.", file=sys.stderr)
            return 1
        kube = Kube(console=Console(style=Style.for_terminal()))
    kube.target = LOCAL if local else CLUSTER
    if command == "admin":
        return ADMIN_SCRIPT.main(extra, kube.stack, command=f"{kube.script} admin")
    if command in SERVER_COMMANDS:
        if command in ASKS_FIRST and "-h" not in extra and "--help" not in extra:
            context = kube.right_context()
            if context is None or not kube.console.yes(
                    f"{command.capitalize()} the server in {kube.target.name}, context {context}?", default=False):
                return 1
        return SERVER_SCRIPT.main([command, *extra], kube.stack)
    if command == "build":
        if not extra:
            parser.error("build needs a tag for the images")
        return 0 if kube.build_images(extra[0], args.registry) else 1
    if command in ("up", "update"):
        return kube.deploy(first=command == "up")
    if command == "down":
        return kube.down()
    if command == "status":
        return kube.status()
    if command == "check":
        return kube.check()
    if not extra:
        parser.error("logs needs a deployment: api, worker, remote-worker, cleanup, cloudflared, or admin")
    return kube.logs(extra[0], args.follow)


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except KeyboardInterrupt:
        print("\nStopped.")
        sys.exit(130)
