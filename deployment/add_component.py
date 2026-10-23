#!/usr/bin/env python3
"""Add lolor, snowflake or ACE to a cluster that is already running.

`--pg-extensions` chooses these when a cluster is deployed. This adds them
afterwards, which is a different job: the nodes exist, they are cross-wired,
and they are serving.

The order is forced by what each component needs, and lolor is the one that
sets the shape:

  1. install on every host — a package, or a build from a ref
  2. extend shared_preload_libraries where something requires it, through the
     DCS, once per scope
  3. restart the nodes that need it, one at a time. shared_preload_libraries
     is read only at startup, so until this happens `CREATE EXTENSION lolor`
     fails with "must be loaded via shared_preload_libraries" — which is why
     step 4 refuses to run before it
  4. create the extension on every Spock node and give each its identity
  5. put lolor's own tables into a replication set, or large objects stay on
     the node that made them

ACE skips 2 through 5 entirely: it is a binary beside the cluster, with nothing
to create and no node identity.

Nothing here is destructive, and a component already present is reported as
such rather than reinstalled — so a half-finished run can simply be repeated.
"""

import time

from aspects import patroni_management, pg_extensions, state
from aspects.logging_setup import RunLogger, new_run_id
from aspects.ssh_executor import build_executor


class ComponentError(RuntimeError):
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


def merge(existing, additions):
    """The cluster's selections with the new ones folded in.

    An entry already there keeps what it was deployed with: re-stating a
    component should not quietly change the ref it was built from.
    """
    merged = list(existing or [])
    known = {item["name"] for item in merged}
    for item in additions:
        if item["name"] not in known:
            merged.append(item)
            known.add(item["name"])
    return merged


def add(cluster_name, components, db_password=None, restart=False,
        run_logger=None, inventory_path=None):
    """Add the named components. Returns a result dict."""
    log = run_logger or RunLogger(new_run_id(f"{cluster_name}-add-component"))
    executors = {}
    started = time.time()
    warnings = []
    created = {}

    try:
        # Inside the try: a cluster with no saved state is an ordinary refusal
        # with a message, not a traceback out of a function that promises to
        # return a result dict.
        plan, metadata = state.load(cluster_name, db_password=db_password,
                                    inventory_path=inventory_path)
        plan.db_password = state.resolve_password(plan, explicit=db_password)

        additions = pg_extensions.parse(components,
                                        default_mode=plan.deploy_mode)
        if not additions:
            raise ComponentError(
                "name at least one component: "
                + ", ".join(pg_extensions.known_names())
            )
        pg_extensions.validate(additions, plan.pg_version or plan.pg_major,
                               node_count=len(plan.spock_nodes))

        already = {item["name"] for item in plan.pg_extensions}
        fresh = [item for item in additions if item["name"] not in already]
        for item in additions:
            if item["name"] in already:
                log.info(f"    {item['name']} is already part of this cluster "
                         f"— checking it is complete rather than redoing it")

        log.banner(f"Adding {', '.join(i['name'] for i in additions)} "
                   f"to '{cluster_name}'")
        log.info(f"    hosts : {len(plan.hosts)}")
        log.info(f"    nodes : {', '.join(n.name for n in plan.spock_nodes)}")
        log.info("")

        # --- 1. on every host -----------------------------------------
        log.step_start("Install on every host",
                       ", ".join(pg_extensions.unparse([i]) for i in additions))
        for host in plan.hosts:
            executor = _executor_for(plan, host.name, executors, log)
            pg_extensions.install_packages(
                executor, host.family, additions, plan.pg_major,
                node=host.name, run_logger=log,
            )
            pg_extensions.build_on_host(executor, plan, host, additions,
                                        run_logger=log)
        log.step_end("passed", f"{len(plan.hosts)} host(s)")

        # --- 2. shared_preload_libraries ------------------------------
        preload = pg_extensions.preload_libraries(additions)
        pending = []
        if preload:
            log.step_start("Add to shared_preload_libraries",
                           f"{', '.join(preload)}, once per scope")
            for scope, members in plan.scopes().items():
                leader = next((m for m in members if m.is_spock), members[0])
                executor = _executor_for(plan, leader.host, executors, log)
                ok, detail = patroni_management.apply_preload(
                    executor, plan, leader, preload
                )
                if ok:
                    log.info(f"    {scope}: {detail}")
                else:
                    warnings.append(f"{scope}: {detail}")
                    log.warn(warnings[-1])
            log.step_end("passed" if not warnings else "failed",
                         f"{len(plan.scopes())} scope(s)")

            # --- 3. the restart it costs ------------------------------
            # Every node, not every scope: a standby has to load the library
            # too, or it cannot replay what its leader writes.
            report = patroni_management.preload_report(
                lambda node: _executor_for(plan, node.host, executors, log),
                plan, preload,
            )
            for row in report:
                if row["missing_from_dcs"]:
                    warnings.append(
                        f"{row['node']}: the DCS does not list "
                        f"{', '.join(row['missing_from_dcs'])} — its scope was "
                        f"not configured"
                    )
                    log.warn(warnings[-1])
                if row["not_loaded"]:
                    pending.append(row["node"])

            if pending and restart:
                log.step_start("Restart the nodes that need it",
                               "one at a time, so the cluster keeps serving")
                for name in list(pending):
                    node = plan.node(name)
                    executor = _executor_for(plan, node.host, executors, log)
                    ok, output = patroni_management.restart_member(executor, node)
                    if ok:
                        pending.remove(name)
                        log.info(f"    {name}: restarted")
                    else:
                        warnings.append(f"{name}: restart failed — {output[:200]}")
                        log.warn(warnings[-1])
                log.step_end("passed" if not pending else "failed",
                             f"{len(plan.nodes) - len(pending)}/"
                             f"{len(plan.nodes)} loaded")

                # Ask again rather than assume the restart did it: a node that
                # comes back without the library is the failure this whole
                # step exists to prevent, and it is silent otherwise.
                still = [row for row in patroni_management.preload_report(
                    lambda node: _executor_for(plan, node.host, executors, log),
                    plan, preload) if row["not_loaded"]]
                pending = [row["node"] for row in still]
                for row in still:
                    warnings.append(
                        f"{row['node']} restarted but still does not load "
                        f"{', '.join(row['not_loaded'])}"
                    )
                    log.warn(warnings[-1])

        # --- 4. create, but only where it can succeed -----------------
        in_database = [item for item in additions
                       if not pg_extensions.CATALOG[item["name"]].is_tool]
        if in_database and pending:
            log.warn(f"    not creating "
                     f"{', '.join(i['name'] for i in in_database)}: "
                     f"{', '.join(pending)} have not loaded "
                     f"{', '.join(preload)} yet")
            warnings.append(
                f"{', '.join(pending)} still need a restart before "
                f"{', '.join(i['name'] for i in in_database)} can be created"
            )
        elif in_database:
            log.step_start("Create on every node",
                           "node identity GUCs, then CREATE EXTENSION")
            for node in plan.spock_nodes:
                executor = _executor_for(plan, node.host, executors, log)
                created[node.name] = pg_extensions.create_on_node(
                    executor, plan, node, selections=in_database,
                    run_logger=log,
                )
            log.step_end("passed",
                         f"{len(plan.spock_nodes)} node(s)")

            # --- 5. lolor's tables have to replicate ------------------
            if any(item["name"] in pg_extensions.REPLICATED_TABLES
                   for item in in_database):
                log.step_start("Replicate the extension's tables",
                               "into the default replication set")
                added, problems = [], []
                for node in plan.spock_nodes:
                    executor = _executor_for(plan, node.host, executors, log)
                    node_added, node_problems = pg_extensions.replicate_tables(
                        executor, plan, node, selections=in_database,
                        run_logger=log,
                    )
                    added += node_added
                    problems += node_problems
                if problems:
                    warnings += problems
                log.step_end("passed", f"{len(added)} table(s)")

        # --- the cluster now owns them --------------------------------
        plan.pg_extensions = merge(plan.pg_extensions, additions)
        state.save(plan, extra={
            **metadata,
            "last_change": f"added {', '.join(i['name'] for i in additions)}",
            "run_id": log.run_id,
        })

        duration = round(time.time() - started, 1)
        outcome = "incomplete" if pending else "succeeded"
        log.banner(f"Components {outcome} in {duration}s")

        if preload:
            log.info("")
            log.info(f"  {'NODE':<12} {'IN THE DCS':<22} LOADED NOW")
            log.info(f"  {'-' * 12} {'-' * 22} {'-' * 22}")
            for row in patroni_management.preload_report(
                    lambda node: _executor_for(plan, node.host, executors, log),
                    plan, preload):
                log.info(f"  {row['node']:<12} "
                         f"{','.join(row['configured']) or '(none)':<22} "
                         f"{','.join(row['loaded']) or '(none)'}")
            log.info("")
        if pending:
            log.info("  Restart these, then run this command again to create "
                     "the extension:")
            for name in pending:
                node = plan.node(name)
                log.info(f"    patronictl -c {node.config_file} restart "
                         f"{node.scope} {name} --force")
            log.info("  or re-run with --restart to do it one at a time.")
        log.info(f"  The cluster now records: "
                 f"{pg_extensions.unparse(plan.pg_extensions)}")

        return {
            "outcome": "succeeded" if not pending else "incomplete",
            "cluster": cluster_name,
            "added": [item["name"] for item in fresh],
            "already_present": sorted(already & {i["name"] for i in additions}),
            "pending_restart": pending,
            "created": created,
            "duration": duration,
            "steps": log.steps,
            "warnings": warnings,
            "log_dir": str(log.root),
        }

    except Exception as exc:
        log.error(f"Adding a component failed — {exc}")
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
