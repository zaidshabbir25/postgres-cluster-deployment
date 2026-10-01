#!/usr/bin/env python3
"""Remove a node from a running cluster, without taking the cluster down.

Two different operations share one entry point, because from the outside they
are the same request — "take this node out" — and the plan already knows which
kind of node it is.

**A Spock node** is un-wired with zodremove's `spock.remove_node`, the inverse
of the `spock.add_node` that wired it in. It runs on the node being removed
(zodremove insists: it compares `spock.node_info()` with the name it is given)
and reaches the peers over dblink, unwinding things in the order that leaves
nothing retaining WAL: subscriptions — which take their slots with them — then
replication sets, then the node registration. Doing only one side of that
leaves orphaned slots that pin WAL on a healthy server forever, which is the
failure this ordering exists to prevent.

**A standby** never touches Spock at all; it is a physical replica inside one
scope. What matters there is the order with respect to synchronous
replication: a scope told to wait for one synchronous standby, whose only
standby is then stopped, blocks every write. So the sync requirement is
lowered *before* the standby goes away, not after.

Throughout, the surviving nodes are reloaded, never restarted, and no step
touches a peer's PostgreSQL. Removing the last Spock node is refused: that is a
cluster teardown, and `remove` (cleanup.py) does it properly.
"""

import time

from aspects import (
    auth_setup,
    health,
    patroni_management,
    source_build,
    spock_management,
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


def spock_branch_for(plan, node):
    """The branch whose zodremove matches the Spock this node is running."""
    spock_major = getattr(node, "spock_major", "") or plan.spock_major
    configured = (plan.source_build or {}).get("spock_branch")
    if configured and str(spock_major) == str(plan.spock_major):
        return configured
    return source_build.default_spock_branch(spock_major)


def remove(cluster_name, node_name, db_password=None, wipe_data=False,
           drain_timeout=300, force=False, run_logger=None):
    """Remove one node — Spock or standby — and return a result dict."""
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

        if node.is_spock:
            removed_names = _remove_spock_node(
                plan, node, executors, log, warnings,
                wipe_data=wipe_data, drain_timeout=drain_timeout, force=force,
            )
        else:
            removed_names = _remove_standby(
                plan, node, executors, log, warnings,
                wipe_data=wipe_data, force=force,
            )

        # --- narrow the survivors -------------------------------------
        plan.nodes = [n for n in plan.nodes if n.name not in removed_names]
        for survivor in plan.nodes:
            survivor.standbys = [s for s in survivor.standbys
                                 if s not in removed_names]
        _narrow_survivors(plan, executors, log, warnings)

        # A host that no longer carries any node but is still an etcd member
        # keeps its etcd running: the DCS membership is deliberately
        # independent of where the database nodes live.
        orphan_hosts = [h.name for h in plan.hosts if not plan.nodes_on(h.name)]
        if orphan_hosts:
            etcd_members = {h.name for h in plan.etcd_hosts}
            still_needed = [h for h in orphan_hosts if h in etcd_members]
            if still_needed:
                log.info(
                    f"    {', '.join(still_needed)} no longer carries a node "
                    f"but remains an etcd member — leaving etcd running"
                )

        snapshot = health.snapshot(plan, run_logger=log)
        kind = "Spock node" if node.is_spock else "standby"
        state.save(plan, extra={
            **metadata,
            "last_change": f"removed {kind} {node_name}",
            "run_id": log.run_id,
        })

        duration = round(time.time() - started, 1)
        log.banner(f"{kind.capitalize()} {node_name} removed in {duration}s")
        log.info(f"  Cluster is now {len(plan.spock_nodes)} Spock node(s): "
                 f"{', '.join(n.name for n in plan.spock_nodes)}")
        if not wipe_data:
            log.info(f"  {node_name}'s data may still be at {node.data_dir} on "
                     f"{node.host}; delete it by hand or re-run with "
                     f"--wipe-data")

        return {
            "outcome": "succeeded",
            "cluster": cluster_name,
            "node": node_name,
            "kind": kind,
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


# ---------------------------------------------------------------------------
# Spock nodes
# ---------------------------------------------------------------------------


def _remove_spock_node(plan, node, executors, log, warnings, wipe_data,
                       drain_timeout, force, verify_timeout=180):
    """Un-wire a Spock node from the mesh. Returns the names removed."""
    if len(plan.spock_nodes) <= 1:
        raise RemoveNodeError(
            f"{node.name} is the only Spock node left. Removing it would "
            f"leave no cluster — use `remove --cluster {plan.cluster_name}` "
            f"to tear the whole thing down instead."
        )

    peers = [n for n in plan.spock_nodes if n.name != node.name]
    standbys = [plan.node(s) for s in node.standbys
                if s in {x.name for x in plan.nodes}]

    log.banner(f"Removing Spock node {node.name} from '{plan.cluster_name}'")
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
    node_executor = None
    try:
        node_executor = _executor_for(plan, node.host, executors, log)
    except Exception as exc:
        node_reachable = False
        if not force:
            raise RemoveNodeError(
                f"cannot reach {node.host} to detach {node.name} cleanly "
                f"({exc}). zodremove runs on the node being removed, so an "
                f"unreachable node can only be detached from the survivors' "
                f"side. Re-run with --force to do that, leaving the node "
                f"itself untouched."
            )
        warnings.append(
            f"{node.host} unreachable — {node.name} was removed from the "
            f"survivors but its own Spock state and data were left behind"
        )
        log.warn(warnings[-1])

    # --- 1. drain -----------------------------------------------------
    log.step_start("Drain outbound replication",
                   f"wait for {node.name}'s slots to catch up")
    if node_reachable:
        drained, worst = spock_operations.wait_for_lag_below(
            node_executor, plan, node, max_bytes=1024, timeout=drain_timeout
        )
        if drained:
            log.step_end("passed", "all active slots caught up")
        else:
            message = f"slots still {worst} bytes behind after {drain_timeout}s"
            if force:
                log.step_end("failed", message + " — continuing (--force)")
                warnings.append(
                    f"{node.name} was removed with {worst} bytes of "
                    f"un-replicated WAL; writes made on it may be lost"
                )
            else:
                log.step_end("failed", message)
                raise RemoveNodeError(
                    f"{node.name} has not finished replicating ({message}). "
                    f"Writes made on it would be lost. Wait, or re-run with "
                    f"--force to accept the loss."
                )
    else:
        log.step_end("skipped", "node unreachable")

    # --- 2. un-wire with zodremove ------------------------------------
    unwired = False
    if node_reachable:
        branch = spock_branch_for(plan, node)
        log.step_start("Un-wire the node",
                       f"spock.remove_node on {node.name}, from branch {branch}")
        try:
            spock_management.load_zodremove(node_executor, plan, node,
                                            run_logger=log, branch=branch)
            ok, output = spock_management.remove_node(
                node_executor, plan, node, run_logger=log
            )
        except Exception as exc:
            ok, output = False, str(exc)

        summary = spock_management.parse_removal_summary(output)
        if summary["processed"] is not None:
            log.info(f"    zodremove processed {summary['processed']} "
                     f"component(s), removed {summary['removed']}, "
                     f"errors {summary['errors']}")

        if ok is None:
            # zodremove finished but its tally is unreadable. That is not a
            # verdict either way; the peers are. Fall through to the check
            # below, which is the question that actually matters.
            log.step_end("passed", "zodremove finished; verifying on the peers")
            unwired = True
        elif ok:
            log.step_end("passed", "subscriptions, slots and node registration "
                                   "removed across the mesh")
            unwired = True
        else:
            log.step_end("failed", (output or "").strip()[-400:])
            if not force:
                raise RemoveNodeError(
                    f"spock.remove_node could not un-wire {node.name}. The "
                    f"cluster is unchanged and still serving; nothing was left "
                    f"half-detached. Re-run with --force to fall back to "
                    f"dropping the subscriptions one by one from each peer."
                )
            warnings.append(
                f"zodremove failed on {node.name}; fell back to a manual "
                f"detach"
            )
            log.warn(warnings[-1])

    if not unwired:
        _manual_detach(plan, node, peers, executors, log, warnings,
                       node_executor if node_reachable else None, force)

    # --- 3. confirm the peers let go ----------------------------------
    log.step_start("Confirm the mesh is clean",
                   f"no peer still references {node.name}")
    cleared, detail = spock_management.wait_for_removal(
        lambda target: _executor_for(plan, target.host, executors, log),
        plan, peers, node.name, timeout=verify_timeout, run_logger=log,
    )
    if cleared:
        log.step_end("passed", detail)
    else:
        log.step_end("failed", detail)
        # Leftovers here are the expensive kind: a subscription still listed on
        # a peer means a replication slot still pinning that peer's WAL.
        swept = _sweep_residue(plan, peers, node, executors, log)
        cleared, detail = spock_management.wait_for_removal(
            lambda target: _executor_for(plan, target.host, executors, log),
            plan, peers, node.name, timeout=min(60, verify_timeout),
            run_logger=log,
        )
        if cleared:
            log.info(f"    swept {swept} leftover object(s) by hand")
        elif force:
            warnings.append(f"leftovers on the peers after removal: {detail}")
            log.warn(warnings[-1])
        else:
            raise RemoveNodeError(
                f"{node.name} was detached but the peers still reference it: "
                f"{detail}. Each leftover subscription keeps a replication "
                f"slot that retains WAL on a healthy node. Fix those, or "
                f"re-run with --force to accept them."
            )

    # --- 4. stop the node and its standbys ----------------------------
    log.step_start("Stop the node",
                   f"Patroni on {node.name}"
                   + (f" and {', '.join(s.name for s in standbys)}"
                      if standbys else ""))
    if node_reachable:
        for member in standbys + [node]:
            member_executor = _executor_for(plan, member.host, executors, log)
            patroni_management.stop(member_executor, member.name)
            log.info(f"    stopped patroni-{member.name}")
        # Clear the scope from the DCS so a future node can reuse the name.
        patroni_management.remove_scope(node_executor, node, node_name=node.name)
        log.step_end("passed", f"scope {node.scope} cleared from the DCS")
    else:
        log.step_end("skipped", "node unreachable")

    # --- 5. optionally wipe data --------------------------------------
    if wipe_data and node_reachable:
        log.step_start("Delete the node's data", node.data_dir)
        for member in standbys + [node]:
            member_executor = _executor_for(plan, member.host, executors, log)
            patroni_management.remove_instance(
                member_executor, member.name, data_root=plan.data_root,
                node=member.name,
            )
            member_executor.try_run(f"rm -f {member.pgpass_file}",
                                    node=member.name)
        log.step_end("passed", "data directories, configs and units removed")

    return {node.name} | {s.name for s in standbys}


def _manual_detach(plan, node, peers, executors, log, warnings, node_executor,
                   force):
    """Drop the subscriptions one peer at a time.

    The fallback for when zodremove cannot run — an unreachable node, or a
    Spock old enough that the procedure errors. Both directions have to be
    dropped: leaving one side behind is what leaves a slot retaining WAL.
    """
    log.step_start("Detach peers from the node",
                   f"drop sub_{node.name}_<peer> on {len(peers)} peer(s)")
    problems = []
    for peer in peers:
        peer_executor = _executor_for(plan, peer.host, executors, log)
        sub = spock_management.sub_name(node.name, peer.name)
        ok, output = spock_operations.sub_drop(peer_executor, plan, peer, sub)
        if ok:
            log.info(f"    {peer.name}: dropped {sub}")
        elif "does not exist" in output.lower():
            log.info(f"    {peer.name}: {sub} already absent")
        else:
            problems.append(f"{peer.name}: {output[:200]}")
    if problems:
        log.step_end("failed", "; ".join(problems))
        if not force:
            raise RemoveNodeError(
                "could not drop every peer subscription; the cluster would "
                "keep orphaned replication slots retaining WAL:\n  "
                + "\n  ".join(problems)
            )
        warnings.extend(problems)
    else:
        log.step_end("passed", f"{len(peers)} peer subscription(s) dropped")

    log.step_start("Detach the node from its peers",
                   f"drop sub_<peer>_{node.name} on {node.name}")
    if node_executor is not None:
        own_problems = []
        for peer in peers:
            sub = spock_management.sub_name(peer.name, node.name)
            ok, output = spock_operations.sub_drop(node_executor, plan, node, sub)
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

    log.step_start("Deregister the node",
                   f"spock.node_drop({node.name}) on each peer")
    drop_problems = []
    for peer in peers:
        peer_executor = _executor_for(plan, peer.host, executors, log)
        ok, output = spock_operations.node_drop(peer_executor, plan, peer,
                                                node.name)
        if ok:
            log.info(f"    {peer.name}: dropped node {node.name}")
        elif "does not exist" not in output.lower():
            drop_problems.append(f"{peer.name}: {output[:200]}")
    if drop_problems:
        log.step_end("failed", "; ".join(drop_problems))
        warnings.extend(drop_problems)
    else:
        log.step_end("passed",
                     f"removed from spock.node on {len(peers)} peer(s)")


def _sweep_residue(plan, peers, node, executors, log):
    """Drop whatever still names the removed node on each peer."""
    swept = 0
    for peer in peers:
        peer_executor = _executor_for(plan, peer.host, executors, log)
        for subscription in spock_management.subscription_status(
                peer_executor, plan, peer):
            if (subscription.get("provider") == node.name
                    or node.name in subscription["name"].split("_")):
                ok, _ = spock_operations.sub_drop(peer_executor, plan, peer,
                                                  subscription["name"])
                swept += 1 if ok else 0
        if node.name in spock_management.node_table(peer_executor, plan, peer):
            ok, _ = spock_operations.node_drop(peer_executor, plan, peer,
                                               node.name)
            swept += 1 if ok else 0
    return swept


# ---------------------------------------------------------------------------
# Standbys
# ---------------------------------------------------------------------------


def _remove_standby(plan, node, executors, log, warnings, wipe_data, force):
    """Take a physical replica out of its scope. Returns the names removed."""
    leader = plan.node(node.leader) if node.leader else None
    if leader is None:
        raise RemoveNodeError(
            f"{node.name} is a standby but its leader is not recorded in the "
            f"cluster state, so its scope cannot be reconfigured safely. "
            f"Remove it by hand, or re-deploy."
        )

    remaining = [s for s in leader.standbys if s != node.name]

    log.banner(f"Removing standby {node.name} from '{plan.cluster_name}'")
    log.info(f"    host        : {node.host} ({node.address}:{node.pg_port})")
    log.info(f"    scope       : {node.scope} (follows {leader.name})")
    log.info(f"    standbys left: {', '.join(remaining) or 'none'}")
    log.info(f"    data dir    : "
             f"{'wiped' if wipe_data else 'left in place'}")
    log.info("")

    leader_executor = _executor_for(plan, leader.host, executors, log)

    # --- 1. make sure it is not the one serving writes ----------------
    log.step_start("Check the scope's roles",
                   f"confirm {node.name} is not currently leading {node.scope}")
    status = patroni_management.cluster_status(leader_executor, leader,
                                               node_name=leader.name)
    roles = {m["name"]: m["role"] for m in status.get("members", [])}
    if (roles.get(node.name) or "").lower() in ("leader", "primary"):
        log.step_end("failed", f"{node.name} is the current leader of "
                               f"{node.scope}")
        if not force:
            raise RemoveNodeError(
                f"{node.name} is a standby in the cluster state but Patroni "
                f"has promoted it: it is the node currently taking writes for "
                f"scope {node.scope}. Stopping it now is an outage. Switch "
                f"over first — `cluster service switchover --node "
                f"{leader.name} --candidate {leader.name}` — or re-run with "
                f"--force to accept the failover."
            )
        warnings.append(f"{node.name} was the leader of {node.scope}; removing "
                        f"it forces a failover")
        log.warn(warnings[-1])
    else:
        log.step_end("passed",
                     f"{node.name} is {roles.get(node.name, 'not visible')}, "
                     f"{leader.name} leads {node.scope}")

    # --- 2. lower the synchronous requirement BEFORE it goes ----------
    # This is the whole reason the order matters. A scope configured to wait
    # for one synchronous standby, whose only standby is then stopped, blocks
    # every write until someone notices. Relax the requirement first and the
    # scope never has a moment where it is waiting for a node that is gone.
    log.step_start("Relax synchronous replication",
                   patroni_management.describe_sync(plan, leader))
    before = patroni_management.sync_settings(plan, leader)
    if not before:
        log.step_end("skipped", f"{node.scope} replicates asynchronously — "
                                f"nothing waits on {node.name}")
    else:
        wanted = int(before.get("synchronous_node_count") or 1)
        if len(remaining) >= wanted:
            log.step_end("skipped",
                         f"{len(remaining)} standby(s) remain, enough for "
                         f"synchronous_node_count={wanted}")
        else:
            # Narrow the plan, then push it: sync_settings() reads
            # leader.standbys, and with none left it returns {} — which
            # apply_sync_settings writes as synchronous_mode=off.
            leader.standbys = remaining
            if remaining:
                leader.synchronous_node_count = len(remaining)
            applied, output = patroni_management.apply_sync_settings(
                leader_executor, plan, leader, node_name=leader.name
            )
            described = patroni_management.describe_sync(plan, leader)
            if applied:
                log.step_end("passed", described)
                log.info("    waiting for Patroni to apply it before stopping "
                         "the standby")
                time.sleep(10)
            else:
                log.step_end("failed", output[:200])
                if not force:
                    # Restore what we narrowed: nothing has been changed yet.
                    leader.standbys = list(leader.standbys) + [node.name]
                    raise RemoveNodeError(
                        f"could not lower {node.scope}'s synchronous "
                        f"requirement ({output[:200]}). Stopping {node.name} "
                        f"now would block writes on {leader.name}. Nothing was "
                        f"changed. Re-run with --force to stop it anyway."
                    )
                warnings.append(
                    f"{node.scope} still requires {wanted} synchronous "
                    f"standby(s); writes on {leader.name} may block until that "
                    f"is corrected"
                )
                log.warn(warnings[-1])

    # --- 3. stop it ---------------------------------------------------
    log.step_start("Stop the standby", f"Patroni on {node.name}")
    try:
        node_executor = _executor_for(plan, node.host, executors, log)
        patroni_management.stop(node_executor, node.name)
        log.step_end("passed", f"patroni-{node.name} stopped")
    except Exception as exc:
        node_executor = None
        if not force:
            raise RemoveNodeError(
                f"cannot reach {node.host} to stop {node.name} ({exc}). "
                f"Re-run with --force to drop it from the scope anyway — but "
                f"if the host comes back, that Patroni will rejoin."
            )
        log.step_end("failed", str(exc))
        warnings.append(f"{node.name} could not be stopped; if {node.host} "
                        f"comes back it will rejoin {node.scope}")
        log.warn(warnings[-1])

    # --- 4. drop its permanent slot from the DCS ----------------------
    # add-standby declares `slots.<name>.type=physical` in the live config so
    # the leader retains WAL while the standby is offline. Left behind, that is
    # exactly what it keeps doing — forever, for a node that is never coming
    # back.
    log.step_start("Drop the replication slot",
                   f"remove slots.{node.name} from scope {node.scope}")
    binary = patroni_management.patronictl_binary(leader_executor,
                                                  node=leader.name)
    ok, output = leader_executor.try_run(
        f"{binary} -c {leader.config_file} edit-config {leader.scope} "
        f"--force -s slots.{node.name}=null",
        node=leader.name,
    )
    if ok:
        log.step_end("passed", f"slot {node.name} removed from the DCS")
    else:
        log.step_end("failed", output.strip()[:200])
        warnings.append(
            f"the permanent slot for {node.name} is still declared in "
            f"{node.scope}; it will retain WAL on {leader.name} until it is "
            f"removed with `patronictl edit-config {leader.scope} -s "
            f"slots.{node.name}=null`"
        )

    # --- 5. optionally wipe data --------------------------------------
    if wipe_data and node_executor is not None:
        log.step_start("Delete the standby's data", node.data_dir)
        patroni_management.remove_instance(
            node_executor, node.name, data_root=plan.data_root, node=node.name
        )
        node_executor.try_run(f"rm -f {node.pgpass_file}", node=node.name)
        log.step_end("passed", "data directory, config and unit removed")

    return {node.name}


# ---------------------------------------------------------------------------
# Shared
# ---------------------------------------------------------------------------


def _narrow_survivors(plan, executors, log, warnings):
    """Regenerate pg_hba, .pgpass and Patroni config without the removed node.

    Reload, never restart: the survivors are serving traffic throughout.
    """
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
    problems = []
    for survivor in plan.nodes:
        survivor_executor = _executor_for(plan, survivor.host, executors, log)
        patroni_management.write_config(survivor_executor, plan, survivor,
                                        run_logger=log)
        binary = patroni_management.patronictl_binary(survivor_executor,
                                                      node=survivor.name)
        ok, output = survivor_executor.try_run(
            f"{binary} -c {survivor.config_file} reload {survivor.scope} "
            f"{survivor.name} --force",
            node=survivor.name,
        )
        if not ok:
            problems.append(f"{survivor.name}: {output.strip()[:200]}")
    if problems:
        log.step_end("failed", "; ".join(problems))
        warnings.extend(problems)
    else:
        log.step_end("passed",
                     f"{len(plan.nodes)} surviving node(s) reconfigured")
