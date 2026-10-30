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
    "api": "base/api.yaml",
    "worker": "base/worker.yaml",
    "remote-worker": "base/remote-worker.yaml",
    "cleanup": "base/cleanup.yaml",
    "cloudflared": "base/cloudflared.yaml",
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


def kustomization(folder: str = "") -> dict:
    return yaml.safe_load((KUBERNETES / folder / "kustomization.yaml").read_text(encoding="utf-8"))


def test_every_file_is_listed_in_the_kustomization():
    base = {path.name for path in (KUBERNETES / "base").glob("*.yaml")} - {"kustomization.yaml"}

    assert set(kustomization("base")["resources"]) == base
    # The real cluster and the test cluster: each its namespace, the base, settings, images.
    assert kustomization()["resources"] == ["namespace.yaml", "base"]
    assert kustomization("local")["resources"] == ["namespace.yaml", "../base", "services.yaml"]
    for folder in ("", "local"):
        assert {image["name"] for image in kustomization(folder)["images"]} == set(IMAGES.values())
        assert kustomization(folder)["configMapGenerator"] == [{"name": "settings", "envs": ["settings.env"]}]


def test_the_test_cluster_drops_only_what_a_pc_lacks():
    local = kustomization("local")
    deleted = {
        (patch_document["kind"], patch_document["metadata"]["name"])
        for patch in local["patches"]
        if "$patch: delete" in patch["patch"]
        for patch_document in [yaml.safe_load(patch["patch"])]
    }

    # KEDA's scaling stays: it scales the test cluster's workers too.
    assert deleted == {
        ("Deployment", "cloudflared"), ("PodDisruptionBudget", "cloudflared"), ("HorizontalPodAutoscaler", "api"),
    }
    assert local["namespace"] != kustomization()["namespace"]
    wiring = next(yaml.safe_load(patch["patch"]) for patch in local["patches"] if "name: wiring" in patch["patch"])
    assert wiring["data"] == {"DATABASE_BACKUPS": "server", "CLOUDFLARE_TUNNEL_DEPLOYED": "false"}


def test_the_test_clusters_services_are_the_names_the_settings_use():
    import importlib.util

    services = [document for document in yaml.safe_load_all(
        (KUBERNETES / "local" / "services.yaml").read_text(encoding="utf-8")) if document]
    names = {document["metadata"]["name"] for document in services if document["kind"] == "Service"}
    spec = importlib.util.spec_from_file_location("kube_script", ROOT / "scripts" / "kube.py")
    kube = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(kube)

    assert names == {"redis", "postgres", "seaweed"}
    assert "@redis:6379" in kube.LOCAL_SETTINGS["REDIS_URL"]
    assert kube.LOCAL_SETTINGS["STORAGE_ENDPOINT"].endswith(":8333")


def test_the_namespace_is_the_one_keda_scrapes():
    namespace = kustomization()["namespace"]
    [scaled] = [document for document in documents("base/worker.yaml") if document["kind"] == "ScaledObject"]

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


def test_no_pod_gets_a_kubernetes_api_token():
    pods = [deployment(service)["spec"]["template"]["spec"] for service in DEPLOYED]
    pods += [document["spec"]["template"]["spec"] for document in documents("local/services.yaml")
             if document["kind"] in {"Deployment", "StatefulSet"}]

    assert len(pods) > len(DEPLOYED)
    assert all(pod["automountServiceAccountToken"] is False for pod in pods)


def test_the_wiring_is_composes_with_the_choices_several_machines_need():
    [wiring] = documents("base/wiring.yaml")

    assert wiring["data"]["REDIS_PASSWORD_FILE"] == COMPOSE["x-wiring"]["REDIS_PASSWORD_FILE"]
    assert wiring["data"]["DATA_DIR"] == COMPOSE["x-wiring"]["DATA_DIR"]
    assert wiring["data"]["JOB_STORAGE"] == "s3"
    assert wiring["data"]["DATABASE_BACKUPS"] == "provider"
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
    [scaled] = [document for document in documents("base/worker.yaml") if document["kind"] == "ScaledObject"]

    for trigger in scaled["spec"]["triggers"]:
        location = trigger["metadata"]["valueLocation"]
        name, labels = re.fullmatch(r"(\w+)\{(.*)\}", location).groups()
        assert name in METRIC_NAMES, name
        assert dict(item.split("=") for item in labels.split(",")) == {"queue": '"default"', "backend": '"local"'}
        assert trigger["authenticationRef"]["name"] == "metrics"


def test_the_metrics_token_is_the_secret_the_api_reads():
    [authentication] = [document for document in documents("base/worker.yaml") if document["kind"] == "TriggerAuthentication"]
    [reference] = authentication["spec"]["secretTargetRef"]

    assert reference == {"parameter": "token", "name": "piano-secrets", "key": "metrics_token"}
    assert "metrics_token" in config.SecretSettings.model_fields


def test_the_gpu_worker_asks_for_a_gpu():
    assert container("worker")["resources"]["limits"]["nvidia.com/gpu"] == 1
    assert COMPOSE["services"]["worker"]["gpus"] == "all"


def test_the_kubernetes_settings_are_never_committed():
    assert "/deploy/kubernetes/settings.env" in (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
