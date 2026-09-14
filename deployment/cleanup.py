#!/usr/bin/env python3
"""Tear a deployed cluster down.

Two depths, because they answer different questions:

  * `remove`  — stop everything and delete cluster state (data directories,
                Patroni units and configs, etcd keys). Packages stay, so a
                redeploy is fast. This is what you want between test runs.
  * `purge`   — also uninstall the pgEdge packages and the repository, leaving
                the hosts as they were found.

Teardown is deliberately forgiving: a host that has already been half cleaned,
or is missing entirely, must not stop the rest from being cleaned up.
"""

import shlex
import time

from aspects import (
    etcd_management,
    package_management,
    patroni_management,
    platform_detect,
    state,
)
from aspects.logging_setup import RunLogger, new_run_id
from aspects.ssh_executor import build_executor
from deployment import topology

CONFIG_PATHS = (
    "/etc/patroni",
    "/etc/profile.d/pgcluster.sh",
    "/etc/profile.d/pgedge-source.sh",
    "/tmp/pgcluster",
)


def _executors(plan, run_logger):
    """Connect to every host, recording the ones that are unreachable."""
    executors, unreachable = {}, {}
    for host in plan.hosts:
        try:
            executor = build_executor(
                {
                    "name": host.name,
                    "host": host.address,
                    "username": host.username,
                    "key_file": host.key_file,
                    "port": host.port,
                    "local": host.local,
                },
                run_logger=run_logger,
            )
            executor.connect()
            executors[host.name] = executor
        except Exception as exc:
            unreachable[host.name] = str(exc)
            run_logger.warn(f"{host.name} unreachable, skipping: {exc}")
    return executors, unreachable


def wipe_hosts(hosts, data_root=None, db_user="postgres", purge_packages=False,
               run_logger=None):
    """Scrub a deployment off the given hosts without consulting saved state.

    `remove` works from the cluster state file, which is the right thing when
    there is one. A deployment that failed halfway, or one whose state file was
    deleted, leaves Patroni units, data directories and etcd keys that nothing
    then knows about — this finds them on the machine itself and removes them.
    """
    data_root = data_root or topology.DEFAULT_DATA_ROOT
    log = run_logger or RunLogger(new_run_id("host-cleanup"))
    started = time.time()
    cleaned, unreachable = [], {}

    log.banner("Cleaning up hosts")
    log.info(f"    {len(hosts)} host(s); data root {data_root}")

    for host in hosts:
        try:
            executor = build_executor(
                {
                    "name": host.name,
                    "host": host.address,
                    "username": host.username,
                    "key_file": host.key_file,
                    "port": host.port,
                    "local": host.local,
                },
                run_logger=log,
            )
            executor.connect()
        except Exception as exc:
            unreachable[host.name] = str(exc)
            log.warn(f"{host.name} unreachable, skipping: {exc}")
            continue

        log.step_start(f"Clean {host.name}", host.address)

        # --- Patroni units, whatever they are called ------------------
        _, listed = executor.try_run(
            "systemctl list-unit-files 'patroni-*.service' --no-legend "
            "2>/dev/null | awk '{print $1}'",
            node=host.name,
        )
        units = [line.strip() for line in listed.splitlines() if line.strip()]
        for unit in units:
            executor.try_run(f"systemctl disable --now {unit} 2>/dev/null || true",
                             node=host.name)
            executor.try_run(f"rm -f /etc/systemd/system/{unit}", node=host.name)
        executor.try_run("pkill -f '/etc/patroni/' 2>/dev/null || true", node=host.name)

        # --- PostgreSQL left behind by a half-bootstrapped node -------
        executor.try_run(
            f"pkill -f 'postgres -D {data_root}' 2>/dev/null || true", node=host.name
        )

        # --- etcd -----------------------------------------------------
        etcd_management.purge(executor, node=host.name)
        executor.try_run("rm -f /etc/etcd/etcd.yml /etc/etcd/etcd.conf",
                         node=host.name)

        # --- files ----------------------------------------------------
        executor.try_run(f"rm -rf {shlex.quote(data_root)}", node=host.name)
        for path in CONFIG_PATHS:
            executor.try_run(f"rm -rf {path}", node=host.name)
        try:
            family = host.family or platform_detect.detect(executor)["family"]
        except Exception:
            family = "rhel"
        home = platform_detect.pg_home(family)
        executor.try_run(
            f"rm -f {home}/.pgpass {home}/.pg_service.conf "
            f"/root/.pgpass /root/.pg_service.conf",
            node=host.name,
        )
        executor.try_run("systemctl daemon-reload 2>/dev/null || true", node=host.name)

        if purge_packages:
            installed = package_management.list_pgedge_packages(
                executor, family, node=host.name
            )
            names = [entry["package"] for entry in installed]
            if names:
                package_management.remove(executor, family, names, node=host.name)
                log.info(f"    {host.name}: removed {len(names)} package(s)")
            if family == "rhel":
                executor.try_run("rm -f /etc/yum.repos.d/pgedge.repo", node=host.name)
            else:
                executor.try_run(
                    "rm -f /etc/apt/sources.list.d/pgedge.sources && apt-get update",
                    node=host.name,
                )

        log.step_end("passed", f"{len(units)} patroni unit(s), data and configs removed")
        cleaned.append(host.name)
        try:
            executor.close()
        except Exception:
            pass

    # Saved state describing only these hosts is now a description of nothing.
    wiped_addresses = {host.address for host in hosts if host.name in cleaned}
    dropped = []
    for name in state.list_clusters():
        try:
            plan, _ = state.load(name)
        except Exception:
            continue
        if {h.address for h in plan.hosts} <= wiped_addresses:
            state.delete(name)
            dropped.append(name)
    if dropped:
        log.info(f"    removed saved state for: {', '.join(dropped)}")

    duration = time.time() - started
    log.info(f"\n{len(cleaned)} host(s) cleaned in {duration:.1f}s")
    return {
        "outcome": "succeeded" if cleaned else "failed",
        "cleaned": cleaned,
        "unreachable": unreachable,
        "clusters_forgotten": dropped,
        "duration": duration,
    }


def remove(cluster_name, purge_packages=False, keep_state=False,
           db_password=None, run_logger=None):
    """Stop and delete a cluster. Returns a result dict."""
    plan, metadata = state.load(cluster_name, db_password=db_password)
    plan.db_password = state.resolve_password(plan, explicit=db_password)

    log = run_logger or RunLogger(new_run_id(f"{cluster_name}-teardown"))
    started = time.time()
    actions = []

    log.banner(f"Removing cluster '{cluster_name}'")
    log.info(f"    {len(plan.nodes)} node(s) across {len(plan.hosts)} host(s)")
    if purge_packages:
        log.info("    packages and the pgEdge repository will also be removed")

    executors, unreachable = _executors(plan, log)

    # --- stop Patroni before touching the DCS -------------------------
    # Stopping members first means no surviving member tries to fail over into
    # a data directory that is being deleted.
    log.step_start("Stop Patroni", f"{len(plan.nodes)} node(s)")
    for node in plan.nodes:
        executor = executors.get(node.host)
        if executor is None:
            continue
        patroni_management.stop(executor, node.name)
        actions.append(f"stopped patroni-{node.name}")
    log.step_end("passed", f"{len(actions)} instance(s) stopped")

    # --- remove scopes from the DCS -----------------------------------
    log.step_start("Remove Patroni scopes", f"{len(plan.scopes())} scope(s)")
    removed_scopes = []
    for scope, members in plan.scopes().items():
        leader = next((m for m in members if m.is_spock), members[0])
        executor = executors.get(leader.host)
        if executor is None:
            continue
        patroni_management.remove_scope(executor, leader, node_name=leader.name)
        removed_scopes.append(scope)
    log.step_end("passed", f"{len(removed_scopes)} scope(s) cleared")

    # --- stop etcd and drop its state ---------------------------------
    log.step_start("Stop etcd", f"{len(plan.etcd_hosts)} member(s)")
    for host in plan.etcd_hosts:
        executor = executors.get(host.name)
        if executor is None:
            continue
        etcd_management.purge(executor, node=host.name)
    log.step_end("passed", "etcd stopped and data removed")

    # --- delete data directories and config ---------------------------
    log.step_start("Delete cluster data", f"under {plan.data_root}")
    for host in plan.hosts:
        executor = executors.get(host.name)
        if executor is None:
            continue
        for node in plan.nodes_on(host.name):
            executor.try_run(f"rm -rf {node.data_dir}", node=node.name)
            executor.try_run(f"rm -f {node.config_file} {node.pgpass_file}",
                             node=node.name)
            executor.try_run(
                f"rm -f /etc/systemd/system/{patroni_management.unit_name(node.name)}"
                f".service",
                node=node.name,
            )
            executor.try_run(f"rm -f /tmp/patroni_{node.name}.log", node=node.name)
        executor.try_run("systemctl daemon-reload 2>/dev/null || true", node=host.name)
        for path in CONFIG_PATHS:
            executor.try_run(f"rm -rf {path}", node=host.name)
        # .pgpass carries the cluster password; it should not outlive the cluster.
        home = platform_detect.pg_home(host.family or "rhel")
        executor.try_run(
            f"rm -f {home}/.pgpass {home}/.pg_service.conf "
            f"/root/.pgpass /root/.pg_service.conf",
            node=host.name,
        )
    log.step_end("passed", "data directories, configs and units removed")

    # --- optional package purge ---------------------------------------
    if purge_packages:
        log.step_start("Uninstall packages", "pgEdge packages and repository")
        for host in plan.hosts:
            executor = executors.get(host.name)
            if executor is None:
                continue
            family = host.family or platform_detect.detect(executor)["family"]
            installed = package_management.list_pgedge_packages(
                executor, family, node=host.name
            )
            names = [entry["package"] for entry in installed]
            if names:
                package_management.remove(executor, family, names, node=host.name)
                log.info(f"    {host.name}: removed {len(names)} package(s)")
            if family == "rhel":
                executor.try_run("rm -f /etc/yum.repos.d/pgedge.repo", node=host.name)
            else:
                executor.try_run(
                    "rm -f /etc/apt/sources.list.d/pgedge.sources && apt-get update",
                    node=host.name,
                )
        log.step_end("passed", "packages and repository removed")

    for executor in executors.values():
        try:
            executor.close()
        except Exception:
            pass

    if not keep_state:
        state.delete(cluster_name)
        log.info(f"    cluster state file for {cluster_name} deleted")

    duration = round(time.time() - started, 1)
    log.banner(f"Cluster '{cluster_name}' removed in {duration}s")
    if unreachable:
        log.warn(
            f"{len(unreachable)} host(s) could not be reached and were left "
            f"untouched: {', '.join(unreachable)}"
        )

    return {
        "outcome": "succeeded",
        "cluster": cluster_name,
        "duration": duration,
        "purged_packages": purge_packages,
        "unreachable": unreachable,
        "steps": log.steps,
        "log_dir": str(log.root),
    }
