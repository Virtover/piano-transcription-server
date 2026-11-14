"""`nodes`: the devices of a home cluster (app/cluster.py, docs/cluster.md)
and what each may do. Switches apply within seconds, without a restart: a
device's supervisor reads them every few seconds, and a task switched off
finishes its running jobs first.

`register` and `forget` are run by scripts/admin.py's `nodes invite` and
`nodes revoke` on the host, which also write the node's password files."""

import time

from app import cluster
from app.admin.common import (
    AdminError,
    ago,
    audit,
    check_reason,
    ensure_audit_log,
    finish,
    print_json,
    proceed,
    table,
)
from app.worker import tasks


STALE_AFTER = cluster.STATUS_TTL


# --- Data ---------------------------------------------------------------------

def node_names() -> list[str]:
    """The hub and every invited node, and any other with a live status."""
    redis = tasks.redis
    names = set(cluster.known_nodes(redis)) | {cluster.HUB}
    names.update(status["node"] for status in cluster.statuses(redis))
    return sorted(names, key=lambda name: (name != cluster.HUB, name))


def overview() -> dict:
    """Every device: its switches, and what each of its services runs."""
    redis = tasks.redis
    by_node: dict[str, list[dict]] = {}
    for status in cluster.statuses(redis):
        by_node.setdefault(status["node"], []).append(status)
    hub = cluster.hub_info(redis) or {}
    devices = []
    known = set(cluster.known_nodes(redis))
    for name in node_names():
        config = cluster.node_config(redis, name)
        services = sorted(by_node.get(name, []), key=lambda status: status["service"])
        devices.append({
            "node": name,
            "invited": name in known,
            "enabled": config["enabled"],
            "tasks": config["tasks"],
            "address": cluster.egress_address(redis, name),
            "services": [
                {
                    "service": status["service"],
                    "state": status.get("state"),
                    "offered": status.get("offered") or [],
                    "running": status.get("running") or [],
                    "stopping": status.get("stopping") or [],
                    "summary": status.get("summary") or "",
                    "problem": status.get("problem") or "",
                    "protocol": status.get("protocol"),
                    "code": status.get("code"),
                    # Unknown until the hub's API published its own: then not different.
                    "same_code": not hub.get("code") or not status.get("code") or status.get("code") == hub.get("code"),
                    "seen": status["updated_at"],
                }
                for status in services
            ],
        })
    for device in devices:
        device["shares_address_with"] = sharing(device, devices)
    return {"protocol": cluster.PROTOCOL, "hub_code": hub.get("code"), "devices": devices}


def check_node(name: str, invited_only: bool = False) -> str:
    name = (name or "").strip().lower()
    if name == cluster.HUB and not invited_only:
        return name
    try:
        name = cluster.check_node_name(name)
    except ValueError as error:
        raise AdminError(str(error)) from error
    if name not in cluster.known_nodes(tasks.redis) and not invited_only:
        raise AdminError(f"No node named {name}; `nodes` lists them, and `nodes invite` adds one")
    return name


def check_tasks(names: list[str]) -> list[str]:
    try:
        parsed = cluster.parse_tasks(names)
    except ValueError as error:
        raise AdminError(str(error)) from error
    if not parsed:
        raise AdminError(f"Name at least one task: {', '.join(cluster.TASKS)}, or all")
    return parsed


def switch(name: str, enabled: bool, actor: str, reason: str | None = None) -> dict:
    """Let a device take work, or stop it; running jobs finish either way."""
    reason = check_reason(reason)
    name = check_node(name)
    ensure_audit_log()
    before = cluster.node_config(tasks.redis, name)["enabled"]
    cluster.set_enabled(tasks.redis, name, enabled)
    audit(actor, "nodes.on" if enabled else "nodes.off", name, reason=reason)
    return {"node": name, "enabled": enabled, "changed": before != enabled}


def allow(name: str, names: list[str], allowed: bool, actor: str, reason: str | None = None) -> dict:
    """Allow or deny tasks on a device. A device that never had a list
    (the hub) starts from every task."""
    reason = check_reason(reason)
    name = check_node(name)
    changes = check_tasks(names)
    ensure_audit_log()
    current = cluster.node_config(tasks.redis, name)["tasks"]
    current = list(cluster.TASKS) if current is None else current
    updated = sorted(set(current) | set(changes) if allowed else set(current) - set(changes),
                     key=cluster.TASKS.index)
    cluster.set_tasks(tasks.redis, name, updated)
    audit(actor, "nodes.allow" if allowed else "nodes.deny", name, ",".join(changes), reason)
    return {"node": name, "tasks": updated}


def register(name: str, names: list[str], actor: str, reason: str | None = None) -> dict:
    """Make an invited node known; run by scripts/admin.py nodes invite."""
    reason = check_reason(reason)
    name = check_node(name, invited_only=True)
    allowed = check_tasks(names)
    ensure_audit_log()
    cluster.register(tasks.redis, name, allowed)
    audit(actor, "nodes.invite", name, ",".join(allowed), reason)
    return {"node": name, "tasks": allowed}


def forget(name: str, actor: str, reason: str | None = None) -> dict:
    """Remove a revoked node; run by scripts/admin.py nodes revoke. Its jobs
    are queued again elsewhere once their leases expire."""
    reason = check_reason(reason)
    name = check_node(name, invited_only=True)
    ensure_audit_log()
    cluster.forget(tasks.redis, name)
    audit(actor, "nodes.revoke", name, reason=reason)
    return {"node": name}


# --- Command line -------------------------------------------------------------

def task_list(names: list[str] | None) -> str:
    if names is None:
        return "all"
    return ", ".join(names) or "none"


def service_line(service: dict) -> list:
    stale = time.time() - service["seen"] > STALE_AFTER
    state = service["state"] or "-"
    if service["stopping"]:
        state += f", finishing {', '.join(service['stopping'])}"
    if service["protocol"] not in (None, cluster.PROTOCOL):
        state += f", protocol {service['protocol']}"
    elif service["code"] and not service["same_code"]:
        state += ", other code than the hub"
    return [service["service"], state + (" (stale)" if stale else ""), task_list(service["running"]),
            ago(service["seen"])]


def sharing(device: dict, devices: list[dict]) -> list[str]:
    """The other devices behind the same public address: video sites see
    them as one, and limit them together."""
    if not device["address"]:
        return []
    return [other["node"] for other in devices if other["node"] != device["node"] and other["address"] == device["address"]]


def address_text(device: dict, devices: list[dict]) -> str:
    if not device["address"]:
        return "not learned yet"
    others = sharing(device, devices)
    return device["address"] + (f" (shared with {', '.join(others)}: video sites limit them together)" if others else "")


def run_list(args) -> None:
    data = overview()
    if args.json:
        print_json(data)
        return
    for device in data["devices"]:
        header = device["node"]
        if not device["enabled"]:
            header += "  [off]"
        print(f"{header}  allowed: {task_list(device['tasks'])}  public address: {address_text(device, data['devices'])}")
        if not device["services"]:
            print("  not running")
        else:
            rows = [service_line(service) for service in device["services"]]
            for line in table(["service", "state", "running", "seen"], rows).splitlines():
                print(f"  {line}")
            for service in device["services"]:
                if service["problem"]:
                    print(f"  {service['service']}: {service['problem']}")
        print()


def run_switch(args) -> None:
    enabled = args.state == "on"
    name = check_node(args.node)
    would = (f"{name} will take work again, as its allowed tasks say" if enabled
             else f"{name} will finish its running jobs and take no new ones")
    if not proceed(args, {"node": name, "enabled": enabled}, would=would):
        return
    result = switch(name, enabled, args.by, args.reason)
    text = f"{name} takes work" if enabled else f"{name} takes no new work; its running jobs finish"
    finish(args, result, text + ("" if result["changed"] else " (no change)"),
           ["nodes", "off" if enabled else "on", name])


def run_allow(args) -> None:
    allowed = args.action == "allow"
    name = check_node(args.node)
    names = check_tasks(args.tasks)
    would = f"{name} will {'be allowed' if allowed else 'no longer be allowed'} {', '.join(names)}"
    if not proceed(args, {"node": name, "tasks": names, "allowed": allowed}, would=would):
        return
    result = allow(name, names, allowed, args.by, args.reason)
    finish(args, result, f"{name} may run: {task_list(result['tasks'])}",
           ["nodes", "deny" if allowed else "allow", name, *names])


def run_register(args) -> None:
    result = register(args.node, args.tasks, args.by, args.reason)
    finish(args, result, f"{result['node']} is registered, for: {task_list(result['tasks'])}")


def run_forget(args) -> None:
    result = forget(args.node, args.by, args.reason)
    finish(args, result, f"{result['node']} is no longer part of the cluster")
