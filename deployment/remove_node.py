#!/usr/bin/env python3
"""Remove a Spock node from a running cluster.

This is `cluster remove-node`. The order is what makes it safe:

  1. drain — wait for the node's outbound replication to catch up, so writes
     that originated on it are not lost when its slots go away
  2. detach both directions — every peer drops its subscription *from* the
     node, and the node drops its subscriptions *to* every peer. Dropping only
     one side leaves orphaned slots that quietly retain WAL forever.
  3. deregister — peers drop the node from spock.node
  4. stop — the node's Patroni instance and any standbys of it
  5. narrow — regenerate pg_hba and .pgpass on the survivors so the removed
     node loses access

Removing the last Spock node is refused: that is a cluster teardown, and
`remove` (cleanup.py) does it properly.
"""

import time

from aspects import (
    auth_setup,
    etcd_management,
    health,
    patroni_management,
    spock_operations,
    state,
)
from aspects.logging_setup import RunLogger, new_run_id
from aspects.ssh_executor import build_executor


class RemoveNodeError(RuntimeError):
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


def remove(cluster_name, node_name, db_password=None, wipe_data=False,
           drain_timeout=300, force=False, run_logger=None):
    """Remove one Spock node and its standbys. Returns a result dict."""
    plan, metadata = state.load(cluster_name, db_password=db_password)
    plan.db_password = state.resolve_password(plan, explicit=db_password)

    log = run_logger or RunLogger(new_run_id(f"{cluster_name}-remove-node"))
    executors = {}
    started = time.time()
    warnings = []

    try:
        try:
            node = plan.node(node_name)
        except KeyError:
            raise RemoveNodeError(
                f"{node_name!r} is not a node in cluster {cluster_name!r}. "
                f"Nodes: {', '.join(n.name for n in plan.nodes)}"
            )

        if not node.is_spock:
            raise RemoveNodeError(
                f"{node_name} is a standby, not a Spock node. Remove it with "
                f"remove-standby, or remove its leader "
                f"({node.leader}) to take the whole scope out."
            )

        if len(plan.spock_nodes) <= 1:
            raise RemoveNodeError(
                f"{node_name} is the only Spock node left. Removing it would "
                f"leave no cluster — use `remove --cluster {cluster_name}` to "
                f"tear the whole thing down instead."
            )

        peers = [n for n in plan.spock_nodes if n.name != node.name]
        standbys = [plan.node(s) for s in node.standbys if s in
                    {x.name for x in plan.nodes}]

        log.banner(f"Removing Spock node {node_name} from '{cluster_name}'")
        log.info(f"    host        : {node.host} ({node.address}:{node.pg_port})")
        log.info(f"    scope       : {node.scope}")
        log.info(f"    peers kept  : {', '.join(p.name for p in peers)}")
        if standbys:
            log.info(f"    standbys    : {', '.join(s.name for s in standbys)} "
                     f"(removed with it)")
        log.info(f"    data dir    : "
                 f"{'wiped' if wipe_data else 'left in place'}")
        log.info("")

        node_reachable = True
        try:
            node_executor = _executor_for(plan, node.host, executors, log)
        except Exception as exc:
            node_reachable = False
            node_executor = None
            if not force:
                raise RemoveNodeError(
                    f"cannot reach {node.host} to detach {node_name} cleanly "
                    f"({exc}). Re-run with --force to remove it from the "
                    f"survivors anyway, leaving the node itself untouched."
                )
            warnings.append(
                f"{node.host} unreachable — {node_name} was removed from the "
                f"survivors but its own Spock state and data were left behind"
            )
            log.warn(warnings[-1])

        # --- 1. drain -------------------------------------------------
        log.step_start("Drain outbound replication",
                       f"wait for {node_name}'s slots to catch up")
        if node_reachable:
            drained, worst = spock_operations.wait_for_lag_below(
                node_executor, plan, node, max_bytes=1024, timeout=drain_timeout
            )
            if drained:
                log.step_end("passed", "all active slots caught up")
            else:
                message = (
                    f"slots still {worst} bytes behind after {drain_timeout}s"
                )
                if force:
                    log.step_end("failed", message + " — continuing (--force)")
                    warnings.append(
                        f"{node_name} was removed with {worst} bytes of "
                        f"un-replicated WAL; writes made on it may be lost"
                    )
                else:
                    log.step_end("failed", message)
                    raise RemoveNodeError(
                        f"{node_name} has not finished replicating ({message}). "
                        f"Writes made on it would be lost. Wait, or re-run with "
                        f"--force to accept the loss."
                    )
        else:
            log.step_end("skipped", "node unreachable")

        # --- 2. detach: peers drop their subscriptions from the node ---
        log.step_start(
            "Detach peers from the node",
            f"drop sub_{node_name}_<peer> on {len(peers)} peer(s)",
        )
        detach_problems = []
        for peer in peers:
            peer_executor = _executor_for(plan, peer.host, executors, log)
            # zodan names subscriptions sub_<provider>_<subscriber>.
            sub = f"sub_{node.name}_{peer.name}"
            ok, output = spock_operations.sub_drop(
                peer_executor, plan, peer, sub
            )
            if ok:
                log.info(f"    {peer.name}: dropped {sub}")
            else:
                # A subscription that is already gone is not a problem.
                if "does not exist" in output.lower():
                    log.info(f"    {peer.name}: {sub} already absent")
                else:
                    detach_problems.append(f"{peer.name}: {output[:200]}")
        if detach_problems:
            log.step_end("failed", "; ".join(detach_problems))
            if not force:
                raise RemoveNodeError(
                    "could not drop every peer subscription; the cluster would "
                    "keep orphaned replication slots retaining WAL:\n  "
                    + "\n  ".join(detach_problems)
                )
            warnings.extend(detach_problems)
        else:
            log.step_end("passed", f"{len(peers)} peer subscription(s) dropped")

        # --- 3. the node drops its own subscriptions ------------------
        log.step_start("Detach the node from its peers",
                       f"drop sub_<peer>_{node_name} on {node_name}")
        if node_reachable:
            own_problems = []
            for peer in peers:
                sub = f"sub_{peer.name}_{node.name}"
                ok, output = spock_operations.sub_drop(
                    node_executor, plan, node, sub
                )
                if ok:
                    log.info(f"    dropped {sub}")
                elif "does not exist" not in output.lower():
                    own_problems.append(output[:200])
            if own_problems:
                log.step_end("failed", "; ".join(own_problems))
                warnings.extend(own_problems)
            else:
                log.step_end("passed", f"{len(peers)} subscription(s) dropped")
        else:
            log.step_end("skipped", "node unreachable")

        # --- 4. deregister from spock.node on the survivors -----------
        log.step_start("Deregister the node",
                       f"spock.node_drop({node_name}) on each peer")
        drop_problems = []
        for peer in peers:
            peer_executor = _executor_for(plan, peer.host, executors, log)
            ok, output = spock_operations.node_drop(
                peer_executor, plan, peer, node.name
            )
            if ok:
                log.info(f"    {peer.name}: dropped node {node.name}")
            elif "does not exist" not in output.lower():
                drop_problems.append(f"{peer.name}: {output[:200]}")
        if drop_problems:
            log.step_end("failed", "; ".join(drop_problems))
            warnings.extend(drop_problems)
        else:
            log.step_end("passed", f"removed from spock.node on {len(peers)} peer(s)")

        # --- 5. stop Patroni on the node and its standbys -------------
        log.step_start("Stop the node",
                       f"Patroni on {node_name}"
                       + (f" and {', '.join(s.name for s in standbys)}"
                          if standbys else ""))
        if node_reachable:
            for member in standbys + [node]:
                member_executor = _executor_for(plan, member.host, executors, log)
                patroni_management.stop(member_executor, member.name)
                log.info(f"    stopped patroni-{member.name}")
            # Clear the scope from the DCS so a future node can reuse the name.
            patroni_management.remove_scope(node_executor, node,
                                            node_name=node.name)
            log.step_end("passed", f"scope {node.scope} cleared from the DCS")
        else:
            log.step_end("skipped", "node unreachable")

        # --- 6. optionally wipe data ----------------------------------
        if wipe_data and node_reachable:
            log.step_start("Delete the node's data", node.data_dir)
            for member in standbys + [node]:
                member_executor = _executor_for(plan, member.host, executors, log)
                member_executor.try_run(f"rm -rf {member.data_dir}",
                                        node=member.name)
                member_executor.try_run(
                    f"rm -f {member.config_file} {member.pgpass_file} "
                    f"/etc/systemd/system/"
                    f"{patroni_management.unit_name(member.name)}.service",
                    node=member.name,
                )
                member_executor.try_run(
                    "systemctl daemon-reload 2>/dev/null || true",
                    node=member.name,
                )
            log.step_end("passed", "data directories, configs and units removed")

        # --- 7. narrow the survivors ----------------------------------
        removed_names = {node.name} | {s.name for s in standbys}
        plan.nodes = [n for n in plan.nodes if n.name not in removed_names]

        log.step_start("Narrow access on the survivors",
                       "regenerate pg_hba and .pgpass without the removed node")
        for host in plan.hosts:
            if not plan.nodes_on(host.name):
                continue
            host_executor = _executor_for(plan, host.name, executors, log)
            auth_setup.configure_host(
                host_executor, host.family, plan.nodes, plan.db_user,
                plan.db_password, plan.db_name, host.bin_dir, node=host.name,
            )
        reload_problems = []
        for survivor in plan.nodes:
            survivor_executor = _executor_for(plan, survivor.host, executors, log)
            patroni_management.write_config(survivor_executor, plan, survivor,
                                            run_logger=log)
            binary = patroni_management.patronictl_binary(
                survivor_executor, node=survivor.name
            )
            ok, output = survivor_executor.try_run(
                f"{binary} -c {survivor.config_file} reload {survivor.scope} "
                f"{survivor.name} --force",
                node=survivor.name,
            )
            if not ok:
                reload_problems.append(f"{survivor.name}: {output.strip()[:200]}")
        if reload_problems:
            log.step_end("failed", "; ".join(reload_problems))
            warnings.extend(reload_problems)
        else:
            log.step_end("passed",
                         f"{len(plan.nodes)} surviving node(s) reconfigured")

        # A host that no longer carries any node but is still an etcd member
        # keeps its etcd running: the DCS membership is deliberately independent
        # of where the database nodes live.
        orphan_hosts = [h.name for h in plan.hosts if not plan.nodes_on(h.name)]
        if orphan_hosts:
            etcd_members = {h.name for h in plan.etcd_hosts}
            still_needed = [h for h in orphan_hosts if h in etcd_members]
            if still_needed:
                log.info(
                    f"    {', '.join(still_needed)} no longer carries a node but "
                    f"remains an etcd member — leaving etcd running"
                )

        snapshot = health.snapshot(plan, run_logger=log)
        state.save(plan, extra={
            **metadata,
            "last_change": f"removed Spock node {node_name}",
            "run_id": log.run_id,
        })

        duration = round(time.time() - started, 1)
        log.banner(f"Spock node {node_name} removed in {duration}s")
        log.info(f"  Cluster is now {len(plan.spock_nodes)} Spock node(s): "
                 f"{', '.join(n.name for n in plan.spock_nodes)}")
        if not wipe_data and node_reachable:
            log.info(f"  {node_name}'s data is still at {node.data_dir} on "
                     f"{node.host}; delete it by hand or re-run with --wipe-data")

        return {
            "outcome": "succeeded",
            "cluster": cluster_name,
            "node": node_name,
            "removed": sorted(removed_names),
            "duration": duration,
            "steps": log.steps,
            "warnings": warnings,
            "health": snapshot,
            "plan": plan,
            "log_dir": str(log.root),
        }

    except Exception as exc:
        log.error(f"Removing a node failed — {exc}")
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
