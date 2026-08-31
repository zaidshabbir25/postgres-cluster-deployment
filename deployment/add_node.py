#!/usr/bin/env python3
"""Add a new Spock node to a running cluster.

This is `cluster add-node` — a new multi-master peer, not a physical standby
(that is add_standby.py). The new node ends up cross-wired to every existing
node in both directions and carrying a copy of their data.

How the data gets there differs from the pgEdge CLI, deliberately. The CLI
restored the new node from a pgBackRest physical backup of a source node and
then promoted it. Here, zodan's Phase 5 creates the source-to-new subscription
with synchronize_structure and synchronize_data both true, so Spock copies the
schema and rows logically. That needs no backup infrastructure and no
pgBackRest stanza, at the cost of being slower than a physical restore on a
large dataset — a reasonable trade for a tool whose whole premise is native
packages and no side channels.

Two steps here are easy to overlook and both break the join if skipped:

  * every existing node's pg_hba must learn the new node's address, or the
    new node's replication connections are refused
  * every existing node's .pgpass must learn it too, or zodan's dblink calls
    from the new node back to its peers cannot authenticate
"""

import time

from aspects import (
    auth_setup,
    configure_repository,
    health,
    inventory,
    package_management,
    patroni_management,
    pg_server_management,
    platform_detect,
    prereq_setup,
    spock_management,
    state,
)
from aspects.cluster_model import Host, Node, ROLE_SPOCK
from aspects.logging_setup import RunLogger, new_run_id
from aspects.ssh_executor import build_executor
from deployment import topology


class AddNodeError(RuntimeError):
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


def next_node_name(plan):
    """The next nN not already taken."""
    used = {n.name for n in plan.nodes}
    index = 1
    while f"n{index}" in used:
        index += 1
    return f"n{index}"


def resolve_host(plan, host_name, inventory_path=None):
    """Pick the host for the new node, pulling it from the inventory if needed.

    Returns (host, added_to_plan, note).
    """
    if host_name:
        for host in plan.hosts:
            if host.name == host_name:
                return host, False, None

        # Not in the cluster yet — look it up in the inventory so a cluster can
        # grow onto a machine that was not part of the original deployment.
        hosts, _ = inventory.load(inventory_path)
        for candidate in hosts:
            if candidate.name == host_name:
                return candidate, True, (
                    f"{host_name} is not part of this cluster yet; it will be "
                    f"prepared from scratch"
                )
        raise AddNodeError(
            f"host {host_name!r} is neither in the cluster nor in the "
            f"inventory. Cluster hosts: "
            f"{', '.join(h.name for h in plan.hosts)}"
        )

    # No host given: prefer one with no Spock node, else the least loaded.
    without_spock = [
        h for h in plan.hosts
        if not any(n.is_spock for n in plan.nodes_on(h.name))
    ]
    if without_spock:
        return without_spock[0], False, None

    target = min(plan.hosts, key=lambda h: len(plan.nodes_on(h.name)))
    return target, False, (
        f"every host already carries a Spock node, so {target.name} takes a "
        f"second one on a separate port — it shares a failure domain with the "
        f"node already there"
    )


def next_ports(plan, host_name):
    used_pg = {n.pg_port for n in plan.nodes_on(host_name)}
    used_api = {n.restapi_port for n in plan.nodes_on(host_name)}
    pg_port = plan.base_pg_port
    while pg_port in used_pg:
        pg_port += 1
    api_port = plan.base_restapi_port
    while api_port in used_api:
        api_port += 1
    return pg_port, api_port


def add(cluster_name, host_name=None, node_name=None, source_node=None,
        inventory_path=None, db_password=None, run_logger=None,
        skip_verify=False):
    """Add one Spock node. Returns a result dict; never raises."""
    plan, metadata = state.load(cluster_name, db_password=db_password)
    plan.db_password = state.resolve_password(plan, explicit=db_password)

    log = run_logger or RunLogger(new_run_id(f"{cluster_name}-add-node"))
    executors = {}
    started = time.time()
    warnings = []

    try:
        if not plan.spock_nodes:
            raise AddNodeError(
                f"cluster {cluster_name!r} has no Spock nodes to join"
            )

        source = plan.node(source_node) if source_node else plan.spock_nodes[0]
        if not source.is_spock:
            raise AddNodeError(
                f"{source.name} is a standby and cannot be a join source; pick "
                f"one of {', '.join(n.name for n in plan.spock_nodes)}"
            )

        target_host, host_is_new, note = resolve_host(plan, host_name,
                                                      inventory_path)
        if note:
            warnings.append(note)
            log.warn(note)

        name = node_name or next_node_name(plan)
        if name in {n.name for n in plan.nodes}:
            raise AddNodeError(f"node {name!r} already exists in this cluster")

        if host_is_new:
            plan.hosts.append(target_host)

        pg_port, api_port = next_ports(plan, target_host.name)

        node = Node(
            name=name,
            role=ROLE_SPOCK,
            host=target_host.name,
            address=target_host.address,
            pg_port=pg_port,
            restapi_port=api_port,
            data_dir=f"{plan.data_root}/{name}",
            # A new Spock node is the leader of its own new scope.
            scope=f"{plan.cluster_name}-{name}",
            config_file=f"/etc/patroni/{name}.yml",
            pgpass_file=f"{plan.data_root}/pgpass_{name}",
        )

        log.banner(f"Adding Spock node {name} to cluster '{cluster_name}'")
        log.info(f"    host      : {target_host.name} ({target_host.address})")
        log.info(f"    postgres  : {node.address}:{node.pg_port}")
        log.info(f"    patroni   : {node.address}:{node.restapi_port}")
        log.info(f"    scope     : {node.scope}")
        log.info(f"    joining   : via {source.name} "
                 f"({len(plan.spock_nodes)} existing node(s))")
        log.info("")

        existing_nodes = list(plan.nodes)
        # Register before generating any config: pg_hba and .pgpass are built
        # from plan.nodes, and every node must know about the newcomer.
        plan.nodes.append(node)

        # --- 1. prepare the host ---------------------------------------
        log.step_start("Prepare host",
                       f"platform, packages and paths on {target_host.name}")
        executor = _executor_for(plan, target_host.name, executors, log)
        info = platform_detect.detect(executor)
        topology.apply_platform(plan, target_host, info)
        node.family = target_host.family
        node.bin_dir = target_host.bin_dir

        installed_version = pg_server_management.server_version(
            executor, node.bin_dir, node=node.name
        )
        if installed_version:
            node.pg_version = installed_version
            log.step_end("passed",
                         f"{info['pretty']} — PostgreSQL {installed_version} "
                         f"already installed")
        else:
            if plan.deploy_mode == "source":
                log.step_end("failed", "source-mode host is not prepared")
                raise AddNodeError(
                    f"{target_host.name} has no PostgreSQL at {node.bin_dir}. "
                    f"This cluster was built from source; build the new host "
                    f"first, or add the node on a host that is already prepared."
                )
            prereq_setup.install_prerequisites(executor, node=node.name)
            configure_repository.configure(
                executor, target_host.family, plan.repo_channel, node=node.name
            )
            packages = platform_detect.server_packages(
                target_host.family, plan.pg_major, plan.spock_major
            )
            packages += platform_detect.patroni_packages(target_host.family)
            log.info(f"    installing {', '.join(packages)}")
            package_management.install(
                executor, target_host.family, packages, node=node.name
            )
            node.pg_version = pg_server_management.server_version(
                executor, node.bin_dir, node=node.name
            )
            if not node.pg_version:
                log.step_end("failed", "PostgreSQL not runnable after install")
                raise AddNodeError(
                    f"{target_host.name}: no PostgreSQL server at {node.bin_dir} "
                    f"after installing {', '.join(packages)}"
                )
            log.step_end("passed",
                         f"{info['pretty']} — PostgreSQL {node.pg_version} installed")

        # --- 2. auth everywhere ---------------------------------------
        log.step_start("Refresh passwordless psql",
                       "teach every host about the new node")
        for host in plan.hosts:
            host_executor = _executor_for(plan, host.name, executors, log)
            if not host.bin_dir:
                topology.apply_platform(
                    plan, host, platform_detect.detect(host_executor)
                )
            auth_setup.configure_host(
                host_executor, host.family, plan.nodes, plan.db_user,
                plan.db_password, plan.db_name, host.bin_dir, node=host.name,
            )
        log.step_end("passed", f"{len(plan.hosts)} host(s) updated")

        # --- 3. widen pg_hba on the existing nodes --------------------
        log.step_start(
            "Authorise the new node on its peers",
            "rewrite pg_hba on every existing node and reload Patroni",
        )
        reloaded, hba_problems = [], []
        for existing in existing_nodes:
            existing_executor = _executor_for(plan, existing.host, executors, log)
            # Regenerating from the plan (which now includes the new node)
            # widens pg_hba; Patroni rewrites pg_hba.conf on reload.
            patroni_management.write_config(existing_executor, plan, existing,
                                            run_logger=log)
            binary = patroni_management.patronictl_binary(
                existing_executor, node=existing.name
            )
            ok, output = existing_executor.try_run(
                f"{binary} -c {existing.config_file} reload {existing.scope} "
                f"{existing.name} --force",
                node=existing.name,
            )
            if ok:
                reloaded.append(existing.name)
            else:
                hba_problems.append(f"{existing.name}: {output.strip()[:200]}")
        if hba_problems:
            log.step_end("failed", "; ".join(hba_problems))
            raise AddNodeError(
                "could not reload Patroni on every existing node, so the new "
                "node's replication connections would be refused:\n  "
                + "\n  ".join(hba_problems)
            )
        log.step_end("passed", f"reloaded {', '.join(reloaded)}")

        # --- 4. bring the new node up ---------------------------------
        log.step_start("Bootstrap the new node with Patroni",
                       f"scope {node.scope}")
        patroni_management.write_config(executor, plan, node, run_logger=log)
        unit = patroni_management.write_service_unit(executor, plan, node)
        log.info(f"    {node.config_file}, {unit}")
        message = patroni_management.start(executor, plan, node, run_logger=log)
        log.info(f"    {message}")
        ok, role, detail = patroni_management.wait_for_role(
            executor, node, ["Leader", "Master", "Primary"], timeout=420,
            run_logger=log,
        )
        if not ok:
            logs = patroni_management.logs(executor, node.name)
            log.step_end("failed", f"never became leader ({detail})")
            raise AddNodeError(
                f"{name} did not bootstrap as leader of {node.scope}: {detail}\n{logs}"
            )
        log.step_end("passed", f"{role} of {node.scope}")

        # --- 5. spock on the new node ---------------------------------
        log.step_start("Prepare Spock on the new node",
                       "extensions and the zodan procedures")
        pg_server_management.wait_for_ready(
            executor, node.bin_dir, node.pg_port, plan.db_user, timeout=120,
            node=node.name,
        )
        spock_management.create_extensions(executor, plan, node, run_logger=log)
        script = spock_management.load_zodan(executor, plan, node, run_logger=log)
        version = spock_management.spock_version(executor, plan, node)
        log.step_end("passed", f"spock {version or 'unknown'}, {script} loaded")

        # --- 6. cross-wire --------------------------------------------
        log.step_start(
            "Cross-wire into the cluster",
            f"spock.add_node via {source.name}; the source-to-new subscription "
            f"copies schema and data",
        )
        joined, output = spock_management.add_node(
            executor, plan, source, node, run_logger=log
        )
        log.node(node.name, output)
        if not joined:
            rate = spock_management.parse_success_rate(output)
            log.step_end("failed", f"zodan success rate {rate}%"
                         if rate is not None else "zodan reported failure")
            raise AddNodeError(
                f"cross-wiring {name} via {source.name} failed"
                + (f" (zodan success rate {rate}%)" if rate is not None else "")
                + f". {name} is registered but only partially wired — full "
                  f"zodan output is in {log.node_log_path(name)}"
            )
        peers = ", ".join(n.name for n in plan.spock_nodes)
        log.step_end("passed", f"cluster is now a full mesh of {peers}")

        # --- 7. DDL replication ---------------------------------------
        log.step_start("Enable DDL replication", "on the new node")
        spock_management.enable_ddl_replication(executor, plan, node,
                                                run_logger=log)
        log.step_end("passed", "schema changes will propagate automatically")

        # --- 8. verify ------------------------------------------------
        if not skip_verify:
            log.step_start("Verify replication",
                           "every node sees every peer, all subscriptions live")

            def executor_for(target):
                return _executor_for(plan, target.host, executors, log)

            ok, findings = spock_management.verify_replication(
                executor_for, plan, run_logger=log
            )
            if ok:
                log.step_end("passed",
                             f"all {len(plan.spock_nodes)} nodes replicating")
            else:
                log.step_end("failed", "; ".join(findings["problems"]))
                warnings.extend(findings["problems"])
                log.warn(
                    "the node joined but replication is not fully converged; "
                    "it may still be completing its initial data sync"
                )

        snapshot = health.snapshot(plan, run_logger=log)
        state.save(plan, extra={
            **metadata,
            "last_change": f"added Spock node {name} via {source.name}",
            "run_id": log.run_id,
        })

        duration = round(time.time() - started, 1)
        log.banner(f"Spock node {name} added in {duration}s")
        log.info(f"  psql -h {node.address} -p {node.pg_port} -U {plan.db_user} "
                 f"-d {plan.db_name}    # or: psql service={name}")
        log.info("")
        log.info(f"  Cluster is now {len(plan.spock_nodes)} Spock node(s): "
                 f"{', '.join(n.name for n in plan.spock_nodes)}")
        if not plan.node(name).standbys:
            log.info(f"  {name} has no standby. Add one with: "
                     f"add-standby --leader {name}")

        return {
            "outcome": "succeeded",
            "cluster": cluster_name,
            "node": name,
            "host": target_host.name,
            "source": source.name,
            "duration": duration,
            "steps": log.steps,
            "warnings": warnings,
            "health": snapshot,
            "plan": plan,
            "log_dir": str(log.root),
        }

    except Exception as exc:
        log.error(f"Adding a node failed — {exc}")
        # Persist the partial state: a half-joined node still has to be
        # inspectable and removable.
        try:
            state.save(plan, extra={
                **metadata,
                "last_change": f"FAILED add-node: {exc}",
                "run_id": log.run_id,
            })
        except Exception:
            pass
        return {
            "outcome": "failed",
            "cluster": cluster_name,
            "failure": str(exc),
            "steps": log.steps,
            "warnings": warnings,
            "log_dir": str(log.root),
        }
    finally:
        for executor in executors.values():
            try:
                executor.close()
            except Exception:
                pass
