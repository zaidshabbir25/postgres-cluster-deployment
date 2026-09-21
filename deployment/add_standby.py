#!/usr/bin/env python3
"""Add a Patroni standby to a Spock node in an already-deployed cluster.

This is the "add a standby node with n1 or n2 as per the input" operation run
after the fact, against saved cluster state rather than a fresh plan.

Two details make it more than just starting another Patroni instance:

  * the new member's permanent replication slot has to be added to the *live*
    DCS configuration, not to the leader's bootstrap block — bootstrap ran once
    and will never be read again, so a slot declared there is never created
  * .pgpass and pg_service.conf on every host must learn the new node, or the
    operator's `psql service=n1s1` fails while everything else looks fine
"""

import time

from aspects import (
    auth_setup,
    health,
    patroni_management,
    platform_detect,
    state,
)
from aspects.cluster_model import Node, ROLE_STANDBY
from aspects.logging_setup import RunLogger, new_run_id
from aspects.ssh_executor import build_executor
from deployment import topology


class StandbyError(RuntimeError):
    pass


def _executor_for(plan, host_name, cache, run_logger):
    if host_name not in cache:
        host = plan.host(host_name)
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
        cache[host_name] = executor
    return cache[host_name]


def choose_host(plan, leader):
    """Prefer a host that is not the leader's, and is least loaded."""
    candidates = [h for h in plan.hosts if h.name != leader.host]
    if candidates:
        return min(candidates, key=lambda h: len(plan.nodes_on(h.name))), None
    return plan.host(leader.host), (
        f"only one host is available, so {leader.name}'s standby shares its "
        f"machine — this protects against a PostgreSQL failure, not a host failure"
    )


def next_ports(plan, host_name):
    """First free postgres/REST port pair on a host."""
    used_pg = {n.pg_port for n in plan.nodes_on(host_name)}
    used_api = {n.restapi_port for n in plan.nodes_on(host_name)}
    pg_port = plan.base_pg_port
    while pg_port in used_pg:
        pg_port += 1
    api_port = plan.base_restapi_port
    while api_port in used_api:
        api_port += 1
    return pg_port, api_port


def next_standby_name(plan, leader):
    """n1s1, n1s2, ... — first name not already taken."""
    ordinal = 1
    existing = {node.name for node in plan.nodes}
    while topology.standby_name(leader.name, ordinal) in existing:
        ordinal += 1
    return topology.standby_name(leader.name, ordinal)


def add(cluster_name, leader_name, host_name=None, db_password=None,
        run_logger=None, synchronous_mode=None, synchronous_node_count=None,
        synchronous_mode_strict=None):
    """Add one standby. Returns a result dict.

    `synchronous_mode` ("async"/"sync"/"quorum") applies to the leader's scope
    only, and is applied to the running DCS once the standby is streaming —
    setting it earlier would make a strict scope refuse writes while the clone
    is still being taken.
    """
    plan, metadata = state.load(cluster_name, db_password=db_password)
    plan.db_password = state.resolve_password(plan, explicit=db_password)

    log = run_logger or RunLogger(new_run_id(f"{cluster_name}-add-standby"))
    executors = {}
    started = time.time()
    warnings = []

    try:
        try:
            leader = plan.node(leader_name)
        except KeyError:
            raise StandbyError(
                f"{leader_name!r} is not a node in cluster {cluster_name!r}. "
                f"Spock nodes are: "
                f"{', '.join(n.name for n in plan.spock_nodes)}"
            )

        if not leader.is_spock:
            raise StandbyError(
                f"{leader_name} is a standby, not a Spock node — a standby "
                f"cannot itself be followed. Pick one of: "
                f"{', '.join(n.name for n in plan.spock_nodes)}"
            )

        if host_name:
            target_host = plan.host(host_name)
            placement_note = None
        else:
            target_host, placement_note = choose_host(plan, leader)

        name = next_standby_name(plan, leader)
        pg_port, api_port = next_ports(plan, target_host.name)

        node = Node(
            name=name,
            role=ROLE_STANDBY,
            host=target_host.name,
            address=target_host.address,
            pg_port=pg_port,
            restapi_port=api_port,
            data_dir=f"{plan.data_root}/{name}",
            scope=leader.scope,
            config_file=f"/etc/patroni/{name}.yml",
            pgpass_file=f"{plan.data_root}/pgpass_{name}",
            leader=leader.name,
        )

        log.banner(f"Adding standby {name} for {leader.name} on {target_host.name}")
        log.info(f"    scope     : {node.scope}")
        log.info(f"    postgres  : {node.address}:{node.pg_port}")
        log.info(f"    patroni   : {node.address}:{node.restapi_port}")
        log.info(f"    data dir  : {node.data_dir}")
        if placement_note:
            log.warn(placement_note)

        # Register the node before writing any config: pg_hba and .pgpass are
        # generated from plan.nodes, and the new node must appear in both.
        leader.standbys.append(name)
        plan.nodes.append(node)

        # --- host preparation -----------------------------------------
        log.step_start("Prepare host", f"platform probe and paths on {target_host.name}")
        executor = _executor_for(plan, target_host.name, executors, log)
        info = platform_detect.detect(executor)
        topology.apply_platform(
            plan, target_host, info,
            advertise_address=platform_detect.advertise_for(
                executor, target_host.address
            ),
        )
        node.family = target_host.family
        node.bin_dir = target_host.bin_dir
        node.pg_version = leader.pg_version
        log.step_end("passed", f"{info['pretty']} — binaries at {node.bin_dir}")

        # --- refresh auth on every host -------------------------------
        log.step_start("Refresh passwordless psql",
                       "add the new node to .pgpass and pg_service.conf everywhere")
        for host in plan.hosts:
            host_executor = _executor_for(plan, host.name, executors, log)
            auth_setup.configure_host(
                host_executor, host.family, plan.nodes, plan.db_user,
                plan.db_password, plan.db_name, host.bin_dir, node=host.name,
            )
        log.step_end("passed", f"{len(plan.hosts)} host(s) updated")

        # --- declare the slot in the live DCS -------------------------
        log.step_start(
            "Register replication slot",
            f"add a permanent physical slot for {name} to scope {node.scope}",
        )
        leader_executor = _executor_for(plan, leader.host, executors, log)
        binary = patroni_management.patronictl_binary(leader_executor,
                                                      node=leader.name)
        ok, output = leader_executor.try_run(
            f"{binary} -c {leader.config_file} edit-config {leader.scope} "
            f"--force -s slots.{name}.type=physical",
            node=leader.name,
        )
        if ok:
            log.step_end("passed", f"slot {name} declared in the DCS")
        else:
            # Not fatal: with use_slots enabled Patroni still creates a
            # transient member slot. The standby works, but the leader may
            # recycle WAL while the standby is down.
            log.step_end(
                "failed",
                "could not declare a permanent slot — the standby will still "
                "stream, but WAL is not protected while it is offline",
            )
            log.warn(output.strip())

        # --- config and unit ------------------------------------------
        log.step_start("Write Patroni configuration", node.config_file)
        patroni_management.write_config(executor, plan, node, run_logger=log)
        unit = patroni_management.write_service_unit(executor, plan, node)
        log.step_end("passed", f"{node.config_file}, {unit}")

        # --- start and wait -------------------------------------------
        log.step_start("Start standby",
                       f"clone {leader.name} with pg_basebackup and join {node.scope}")
        message = patroni_management.start(executor, plan, node, run_logger=log)
        log.info(f"    {message}")
        ok, role, detail = patroni_management.wait_for_role(
            executor, node, ["Replica", "Sync Standby"], timeout=900,
            run_logger=log,
        )
        if not ok:
            logs = patroni_management.logs(executor, node.name)
            log.step_end("failed", f"never became a replica ({detail})")
            raise StandbyError(
                f"{name} did not join scope {node.scope} as a replica: {detail}\n{logs}"
            )
        log.step_end("passed", f"{role} of {node.scope}")

        # --- verify ---------------------------------------------------
        log.step_start("Validate scope", f"members of {node.scope}")
        status = patroni_management.cluster_status(leader_executor, leader,
                                                   node_name=leader.name)
        roles = ", ".join(
            f"{m['name']}={m['role']}/{m['state']}" for m in status["members"]
        )
        log.info(f"    {roles}")
        if name not in {m["name"] for m in status["members"]}:
            log.step_end("failed", f"{name} is not visible in patronictl output")
            raise StandbyError(f"{name} joined but does not appear in the DCS")
        log.step_end("passed", roles)

        # --- replication mode -----------------------------------------
        # Only now: bootstrap.dcs was read when the scope was created, so a
        # running cluster takes this through patronictl, and a strict scope
        # must not be told to wait for a standby that is not streaming yet.
        if synchronous_mode is not None:
            leader.synchronous_mode = patroni_management.normalise_sync_mode(
                synchronous_mode)
            if synchronous_node_count:
                leader.synchronous_node_count = int(synchronous_node_count)
        if synchronous_mode_strict is not None:
            plan.synchronous_mode_strict = bool(synchronous_mode_strict)

        log.step_start("Set the replication mode",
                       patroni_management.describe_sync(plan, leader))
        applied, output = patroni_management.apply_sync_settings(
            leader_executor, plan, leader, node_name=leader.name
        )
        if applied:
            log.step_end("passed",
                         patroni_management.describe_sync(plan, leader))
        else:
            # The standby is up and streaming either way; only the durability
            # guarantee is missing, so this is reported, not fatal.
            log.step_end("failed", output[:200])
            warnings.append(
                f"could not set the replication mode on {leader.scope}: "
                f"{output[:200]}. The standby is streaming asynchronously; "
                f"apply it by hand with `patronictl -c {leader.config_file} "
                f"edit-config {leader.scope}`"
            )

        snapshot = health.snapshot(plan, run_logger=log)
        state.save(plan, extra={
            **metadata,
            "last_change": f"added standby {name} for {leader.name}",
            "run_id": log.run_id,
        })

        duration = round(time.time() - started, 1)
        log.banner(f"Standby {name} added in {duration}s")
        log.info(f"  psql -h {node.address} -p {node.pg_port} -U {plan.db_user} "
                 f"-d {plan.db_name}   # read-only until promoted")
        log.info("")
        log.info(
            "Note: promoting this standby moves the Spock node to a new address "
            "and port. Its peers keep the old DSN, so run "
            f"`./pg_cluster_status.sh --cluster {cluster_name} --repair-failover "
            f"{leader.name}` after a promotion."
        )

        return {
            "outcome": "succeeded",
            "cluster": cluster_name,
            "standby": name,
            "leader": leader.name,
            "host": target_host.name,
            "duration": duration,
            "steps": log.steps,
            "warnings": warnings,
            "health": snapshot,
            "plan": plan,
            "log_dir": str(log.root),
        }

    except Exception as exc:
        log.error(f"Adding a standby failed — {exc}")
        return {
            "outcome": "failed",
            "cluster": cluster_name,
            "failure": str(exc),
            "steps": log.steps,
            "log_dir": str(log.root),
        }
    finally:
        for executor in executors.values():
            try:
                executor.close()
            except Exception:
                pass
