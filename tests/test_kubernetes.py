"""deploy/kubernetes stays in step with docker-compose.yml and the code: the
same commands, wiring, secret paths, user, and hardening, and scaling rules
on metrics the API reports."""

import re
from pathlib import Path

import pytest
import yaml

from app import config


ROOT = Path(__file__).resolve().parent.parent
KUBERNETES = ROOT / "deploy" / "kubernetes"
COMPOSE = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
# Compose services and their Deployments; redis, postgres, and prepare are not
# deployed: managed services replace the first two, and pods have no host
# folders to prepare.
DEPLOYED = {
    "api": "api.yaml",
    "worker": "worker.yaml",
    "remote-worker": "remote-worker.yaml",
    "cleanup": "cleanup.yaml",
    "cloudflared": "cloudflared.yaml",
    "admin": "components/admin-web/admin.yaml",
}
IMAGES = {"Dockerfile.api": "piano-api", "Dockerfile.worker": "piano-worker"}
# The metric names app/metrics.py reports.
METRIC_NAMES = set(re.findall(r'\("(piano_\w+)"', (ROOT / "app" / "metrics.py").read_text(encoding="utf-8")))


def documents(name: str) -> list[dict]:
    return [document for document in yaml.safe_load_all((KUBERNETES / name).read_text(encoding="utf-8")) if document]


def deployment(service: str) -> dict:
    [found] = [document for document in documents(DEPLOYED[service]) if document["kind"] == "Deployment"]
    assert found["metadata"]["name"] == service
    return found


def container(service: str) -> dict:
    [found] = deployment(service)["spec"]["template"]["spec"]["containers"]
    return found


def mounts(service: str) -> dict[str, dict]:
    return {mount["mountPath"]: mount for mount in container(service).get("volumeMounts", [])}


def test_every_file_is_listed_in_the_kustomization():
    kustomization = yaml.safe_load((KUBERNETES / "kustomization.yaml").read_text(encoding="utf-8"))
    files = {path.name for path in KUBERNETES.glob("*.yaml")} - {"kustomization.yaml"}

    assert set(kustomization["resources"]) == files
    assert {image["name"] for image in kustomization["images"]} == set(IMAGES.values())


def test_the_namespace_is_the_one_keda_scrapes():
    kustomization = yaml.safe_load((KUBERNETES / "kustomization.yaml").read_text(encoding="utf-8"))
    namespace = kustomization["namespace"]
    [scaled] = [document for document in documents("worker.yaml") if document["kind"] == "ScaledObject"]

    for trigger in scaled["spec"]["triggers"]:
        assert trigger["metadata"]["url"] == f"http://api.{namespace}.svc.cluster.local:8000/metrics"


@pytest.mark.parametrize("service", DEPLOYED)
def test_the_image_and_command_are_composes(service):
    compose = COMPOSE["services"][service]
    found = container(service)

    if "image" in compose:
        assert found["image"] == compose["image"]
        assert found["args"] == compose["command"]
    else:
        assert found["image"] == IMAGES[compose["build"]["dockerfile"]]
        assert found.get("command") == compose.get("command")


@pytest.mark.parametrize("service", DEPLOYED)
def test_every_pod_is_unprivileged(service):
    pod = deployment(service)["spec"]["template"]["spec"]
    security = container(service)["securityContext"]

    assert pod["securityContext"]["runAsNonRoot"] is True
    assert pod["securityContext"]["seccompProfile"] == {"type": "RuntimeDefault"}
    if service != "cloudflared":
        assert pod["securityContext"]["runAsUser"] == config.CONTAINER_UID
    assert security["allowPrivilegeEscalation"] is False
    assert security["capabilities"] == {"drop": ["ALL"]}
    # Read-only where Compose makes it read-only.
    if COMPOSE["services"][service].get("read_only"):
        assert security["readOnlyRootFilesystem"] is True
        assert "/tmp" in mounts(service)


def test_the_wiring_is_composes_with_the_choices_several_machines_need():
    [wiring] = documents("wiring.yaml")

    assert wiring["data"]["REDIS_PASSWORD_FILE"] == COMPOSE["x-wiring"]["REDIS_PASSWORD_FILE"]
    assert wiring["data"]["DATA_DIR"] == COMPOSE["x-wiring"]["DATA_DIR"]
    assert wiring["data"]["JOB_STORAGE"] == "s3"
    assert wiring["data"]["BILLING_BACKUPS"] == "provider"
    # Every value is a setting the server knows.
    assert set(wiring["data"]) <= set(config.known_variables())


@pytest.mark.parametrize("service", [name for name in DEPLOYED if name != "cloudflared"])
def test_the_wiring_wins_over_the_settings(service):
    sources = [source["configMapRef"]["name"] for source in container(service)["envFrom"]]

    assert sources == ["settings", "wiring"]


@pytest.mark.parametrize("service", ["api", "worker", "remote-worker", "cleanup"])
def test_redis_password_where_compose_puts_it(service):
    mount = mounts(service)[COMPOSE["x-wiring"]["REDIS_PASSWORD_FILE"]]

    assert mount["subPath"] == "app" and mount["readOnly"] is True


@pytest.mark.parametrize("service", ["api", "worker", "remote-worker"])
def test_secrets_where_compose_puts_them(service):
    mount = mounts(service)[config.SECRETS_DIR]

    assert mount["readOnly"] is True
    volume = {item["name"]: item for item in deployment(service)["spec"]["template"]["spec"]["volumes"]}[mount["name"]]
    assert volume["secret"]["secretName"] == "piano-secrets"


def test_only_cloudflared_gets_the_tunnel_token():
    for service in DEPLOYED:
        volumes = deployment(service)["spec"]["template"]["spec"].get("volumes", [])
        names = {volume.get("secret", {}).get("secretName") for volume in volumes}
        assert ("piano-cloudflare" in names) is (service == "cloudflared")
    token = COMPOSE["services"]["cloudflared"]["command"][-1]
    assert token.startswith(next(iter(mounts("cloudflared"))))


def test_cleanup_and_admin_get_no_shared_secrets():
    for service in ("cleanup", "admin"):
        volumes = deployment(service)["spec"]["template"]["spec"]["volumes"]
        assert "piano-secrets" not in {volume.get("secret", {}).get("secretName") for volume in volumes}


def test_services_keep_composes_names_and_ports():
    services = {
        document["metadata"]["name"]: document["spec"]["ports"][0]["port"]
        for name in DEPLOYED.values()
        for document in documents(name)
        if document["kind"] == "Service"
    }

    assert services == {"api": 8000, "admin": 8100}
    assert COMPOSE["services"]["api"]["ports"][0].endswith(":8000:8000")


def test_the_workers_scale_on_metrics_the_api_reports():
    [scaled] = [document for document in documents("worker.yaml") if document["kind"] == "ScaledObject"]

    for trigger in scaled["spec"]["triggers"]:
        location = trigger["metadata"]["valueLocation"]
        name, labels = re.fullmatch(r"(\w+)\{(.*)\}", location).groups()
        assert name in METRIC_NAMES, name
        assert dict(item.split("=") for item in labels.split(",")) == {"queue": '"default"', "backend": '"local"'}
        assert trigger["authenticationRef"]["name"] == "metrics"


def test_the_metrics_token_is_the_secret_the_api_reads():
    [authentication] = [document for document in documents("worker.yaml") if document["kind"] == "TriggerAuthentication"]
    [reference] = authentication["spec"]["secretTargetRef"]

    assert reference == {"parameter": "token", "name": "piano-secrets", "key": "metrics_token"}
    assert "metrics_token" in config.SecretSettings.model_fields


def test_the_gpu_worker_asks_for_a_gpu():
    assert container("worker")["resources"]["limits"]["nvidia.com/gpu"] == 1
    assert COMPOSE["services"]["worker"]["gpus"] == "all"


def test_the_kubernetes_settings_are_never_committed():
    assert "/deploy/kubernetes/settings.env" in (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
