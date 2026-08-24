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
