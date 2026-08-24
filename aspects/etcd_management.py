#!/usr/bin/env python3
"""etcd — the DCS Patroni uses to elect leaders.

Two shapes are supported and the choice is made by host count, not by the user:

  * three or more hosts -> a 3-member etcd cluster, which is the smallest
    configuration that survives losing a machine
  * fewer hosts -> a single member on the first host, which is a single point
    of failure and is reported as such

The packaged etcd reads a different file in a different dialect on each family
— YAML at /etc/etcd/etcd.yml on RHEL, systemd EnvironmentFile key=value at
/etc/etcd/etcd.conf on Debian — so the config is rendered to match whichever
unit will actually read it. A source-mode deployment installs its own unit and
picks the dialect explicitly.
"""

import re
import shlex

from aspects import service_management

CLIENT_PORT = 2379
PEER_PORT = 2380
DATA_ROOT = "/var/lib/etcd"
UNIT = "etcd"


def member_name(host_name):
    """etcd member names must be simple tokens; host names may not be."""
    return re.sub(r"[^A-Za-z0-9_-]", "-", host_name).strip("-") or "etcd"


def select_members(hosts):
    """Pick etcd members from the available hosts.

    Returns (members, note) where note explains the choice for the report.
    """
    if len(hosts) >= 3:
        members = hosts[:3]
        return members, "3-member etcd cluster (tolerates one host failure)"
    members = hosts[:1]
    return members, (
        f"single-member etcd on {members[0].name} — a single point of failure; "
        f"add a third host to get a fault-tolerant DCS"
    )


def endpoints(members):
    """Client URLs every Patroni instance will be pointed at."""
    return [f"http://{host.address}:{CLIENT_PORT}" for host in members]


def initial_cluster(members):
    return ",".join(
        f"{member_name(host.name)}=http://{host.address}:{PEER_PORT}"
        for host in members
    )


def _yaml_config(host, members, token):
    name = member_name(host.name)
    return (
        f"# Managed by pg-cluster-deployment\n"
        f"name: \"{name}\"\n"
        f"data-dir: \"{DATA_ROOT}/{name}.etcd\"\n"
        f"listen-peer-urls: \"http://0.0.0.0:{PEER_PORT}\"\n"
        f"listen-client-urls: \"http://0.0.0.0:{CLIENT_PORT}\"\n"
        f"initial-advertise-peer-urls: \"http://{host.address}:{PEER_PORT}\"\n"
        f"advertise-client-urls: \"http://{host.address}:{CLIENT_PORT}\"\n"
        f"initial-cluster: \"{initial_cluster(members)}\"\n"
        f"initial-cluster-token: \"{token}\"\n"
        f"initial-cluster-state: \"new\"\n"
        f"enable-v2: false\n"
    )


def _env_config(host, members, token):
    name = member_name(host.name)
    return (
        f"# Managed by pg-cluster-deployment\n"
        f"ETCD_NAME=\"{name}\"\n"
        f"ETCD_DATA_DIR=\"{DATA_ROOT}/{name}.etcd\"\n"
        f"ETCD_LISTEN_PEER_URLS=\"http://0.0.0.0:{PEER_PORT}\"\n"
        f"ETCD_LISTEN_CLIENT_URLS=\"http://0.0.0.0:{CLIENT_PORT}\"\n"
        f"ETCD_INITIAL_ADVERTISE_PEER_URLS=\"http://{host.address}:{PEER_PORT}\"\n"
        f"ETCD_ADVERTISE_CLIENT_URLS=\"http://{host.address}:{CLIENT_PORT}\"\n"
        f"ETCD_INITIAL_CLUSTER=\"{initial_cluster(members)}\"\n"
        f"ETCD_INITIAL_CLUSTER_TOKEN=\"{token}\"\n"
        f"ETCD_INITIAL_CLUSTER_STATE=\"new\"\n"
        f"ETCD_ENABLE_V2=\"false\"\n"
    )


def config_format_for(family):
    """Which config dialect the packaged etcd unit on this family expects."""
    return "yaml" if family == "rhel" else "env"


def config_path_for(config_format):
    return "/etc/etcd/etcd.yml" if config_format == "yaml" else "/etc/etcd/etcd.conf"


def configure(executor, host, members, cluster_name, node=None, config_format=None):
    """Write etcd's config on one member host.

    `config_format` follows the host family by default, because each family's
    packaged unit reads a different file in a different dialect. A source-mode
    deployment installs its own unit and passes "yaml" explicitly, so both
    families end up consistent with the unit that will actually read the file.
    """
    config_format = config_format or config_format_for(host.family)
    token = f"{cluster_name}-etcd"
    name = member_name(host.name)
    content = (
        _yaml_config(host, members, token) if config_format == "yaml"
        else _env_config(host, members, token)
    )
    path = config_path_for(config_format)

    executor.run(f"mkdir -p {DATA_ROOT}/{name}.etcd", node=node)
    executor.run(f"chmod 700 {DATA_ROOT}", node=node)
    # The packaged unit runs etcd as the etcd user where that account exists.
    executor.try_run(f"chown -R etcd:etcd {DATA_ROOT} 2>/dev/null || true", node=node)
    executor.write_file(path, content, owner="root", mode="644", node=node)
    return path


def start(executor, host, members, node=None, timeout=90):
    """Start etcd and wait until it answers a health check."""
    name = member_name(host.name)

    if service_management.unit_exists(executor, UNIT, node=node):
        ok, output = service_management.start(executor, UNIT, node=node)
        if not ok:
            detail = service_management.journal(executor, UNIT, node=node)
            raise RuntimeError(
                f"{host.name}: systemctl could not start etcd\n{output.strip()}\n{detail}"
            )
    else:
        _start_without_systemd(executor, host, members, name, node=node)

    ok, detail = wait_healthy(executor, host, timeout=timeout, node=node)
    if not ok:
        logs = service_management.journal(executor, UNIT, node=node) or \
            executor.fetch_text("/tmp/etcd.log")
        raise RuntimeError(f"{host.name}: etcd never became healthy — {detail}\n{logs}")
    return detail


def _start_without_systemd(executor, host, members, name, node=None):
    """Launch etcd directly for hosts with no usable systemd."""
    binary = executor.which("etcd") or "/usr/bin/etcd"
    executor.try_run("pkill -x etcd 2>/dev/null || true", node=node)
    command = (
        f"nohup {binary} "
        f"--name {shlex.quote(name)} "
        f"--data-dir {DATA_ROOT}/{name}.etcd "
        f"--listen-peer-urls http://0.0.0.0:{PEER_PORT} "
        f"--listen-client-urls http://0.0.0.0:{CLIENT_PORT} "
        f"--initial-advertise-peer-urls http://{host.address}:{PEER_PORT} "
        f"--advertise-client-urls http://{host.address}:{CLIENT_PORT} "
        f"--initial-cluster {shlex.quote(initial_cluster(members))} "
        f"--initial-cluster-state new "
        f"> /tmp/etcd.log 2>&1 </dev/null &"
    )
    executor.try_run(command, node=node)


def wait_healthy(executor, host, timeout=90, interval=3, node=None):
    """Poll etcdctl until the local endpoint reports healthy."""
    endpoint = f"http://{host.address}:{CLIENT_PORT}"
    attempts = max(1, timeout // interval)
    last = ""
    for _ in range(attempts):
        ok, output = executor.try_run(
            f"ETCDCTL_API=3 etcdctl endpoint health "
            f"--endpoints={endpoint} 2>&1",
            node=node,
        )
        if ok and "healthy" in output.lower():
            return True, output.strip()
        last = output.strip()
        executor.try_run(f"sleep {interval}", node=node)
    return False, last or "no response from etcdctl"


def cluster_status(executor, members, node=None):
    """Member list and health, for the dashboard and reports."""
    endpoint_list = ",".join(endpoints(members))
    _, member_output = executor.try_run(
        f"ETCDCTL_API=3 etcdctl member list --endpoints={endpoint_list} "
        f"-w simple 2>&1 || true",
        node=node,
    )
    _, health_output = executor.try_run(
        f"ETCDCTL_API=3 etcdctl endpoint health --endpoints={endpoint_list} "
        f"--cluster 2>&1 || true",
        node=node,
    )

    healthy = [
        line.strip() for line in health_output.splitlines()
        if "is healthy" in line.lower()
    ]
    return {
        "endpoints": endpoints(members),
        "members": member_output.strip(),
        "health": health_output.strip(),
        "healthy_count": len(healthy),
        "member_count": len(members),
        "ok": len(healthy) >= (len(members) // 2 + 1),
    }


def stop(executor, node=None):
    """Stop etcd, whether it is under systemd or not."""
    service_management.stop(executor, UNIT, node=node)
    executor.try_run("pkill -x etcd 2>/dev/null || true", node=node)


def purge(executor, node=None):
    """Remove etcd state so a redeploy starts from an empty DCS.

    Without this, a rerun finds stale Patroni keys from the previous cluster and
    members refuse to bootstrap.
    """
    stop(executor, node=node)
    executor.try_run(f"rm -rf {DATA_ROOT}/*.etcd", node=node)
