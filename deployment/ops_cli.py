#!/usr/bin/env python3
"""Day-two operations command groups, registered into the main CLI.

Kept in its own module so deployment/cli.py stays about cluster lifecycle
(deploy, plan, status, remove) while this covers everything you do to a cluster
that already exists. Both present one command tree to the user:

    node     list / add / remove / add-standby / command / ssh / psql
    spock    nodes, replication sets, subscriptions, DDL, sequences, lag
    db       databases, GUCs, read-only mode, IO test, tables
    service  status / start / stop / restart / reload / enable / disable / logs
             / switchover / reinit
    package  list / install / remove / upgrade
    app      install / remove / run / counts / concurrent-index
    diff     spock / repset / schema / table / all / repair

Every handler returns a process exit code: 0 success, 1 failure, 2 bad usage.
"""

import json
import sys

from aspects import (
    app_management,
    cluster_services,
    consistency,
    db_operations,
    spock_operations,
    state,
)
from aspects.logging_setup import RunLogger, new_run_id
from deployment import add_node, add_standby, node_access, remove_node

EXIT_OK, EXIT_FAIL, EXIT_USAGE = 0, 1, 2


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _resolve_cluster(args):
    cluster = getattr(args, "cluster", None) or state.latest_cluster()
    if not cluster:
        print("No deployed clusters found. Run ./pg_deploy_cluster.sh first.",
              file=sys.stderr)
        return None
    return cluster


def _load(args):
    """Load a cluster plan with its password resolved. Returns (plan, meta)."""
    cluster = _resolve_cluster(args)
    if cluster is None:
        raise SystemExit(EXIT_FAIL)
    plan, meta = state.load(cluster, db_password=args.db_password)
    plan.db_password = state.resolve_password(plan, explicit=args.db_password)
    return plan, meta


def _node(plan, name=None, spock_only=True):
    """Resolve a node by name, defaulting to the first Spock node."""
    if not name:
        candidates = plan.spock_nodes if spock_only else plan.nodes
        if not candidates:
            raise ValueError("this cluster has no usable node")
        return candidates[0]
    try:
        node = plan.node(name)
    except KeyError:
        raise ValueError(
            f"{name!r} is not a node in this cluster. Nodes: "
            f"{', '.join(n.name for n in plan.nodes)}"
        )
    if spock_only and not node.is_spock:
        raise ValueError(
            f"{name} is a standby; this operation needs a Spock node "
            f"({', '.join(n.name for n in plan.spock_nodes)})"
        )
    return node


def _emit(payload, as_json, renderer=None):
    """Print a result either as JSON or through a renderer."""
    if as_json:
        print(json.dumps(payload, indent=2, default=str))
    elif renderer:
        print(renderer(payload))
    return payload


def _table(rows, columns, empty="(none)"):
    """Render a list of dicts as an aligned table."""
    if not rows:
        return f"  {empty}"
    widths = {c: max(len(c), *(len(str(r.get(c, "") or "")) for r in rows))
              for c in columns}
    header = "  ".join(f"{c.upper():<{widths[c]}}" for c in columns)
    lines = [header, "-" * len(header)]
    for row in rows:
        lines.append("  ".join(
            f"{str(row.get(c, '') or ''):<{widths[c]}}" for c in columns
        ))
    return "\n".join(lines)


def _report(ok, payload, as_json, title, problems_key="problems"):
    """Standard rendering for the diff-style results."""
    if as_json:
        print(json.dumps(payload, indent=2, default=str))
        return EXIT_OK if ok else EXIT_FAIL

    print(f"{title}: {'OK' if ok else 'DIFFERENCES FOUND'}")
    if payload.get("note"):
        print(f"  {payload['note']}")
    problems = payload.get(problems_key) or []
    if problems:
        print(f"\n{len(problems)} finding(s):")
        for problem in problems:
            print(f"  - {problem}")
    return EXIT_OK if ok else EXIT_FAIL


def _with_pool(plan, handler):
    """Run a handler with a shared executor pool, always closing it."""
    executor_for, cache = node_access.executor_pool(plan)
    try:
        return handler(executor_for)
    finally:
        node_access.close(cache)


# ===========================================================================
# node
# ===========================================================================


def cmd_node_list(args):
    result = node_access.list_nodes(
        _resolve_cluster(args) or "", db_password=args.db_password,
        probe=not args.no_probe,
    )
    if args.json:
        payload = dict(result)
        payload.pop("plan", None)
        print(json.dumps(payload, indent=2, default=str))
    else:
        print(node_access.format_nodes(result))
    return EXIT_OK


def cmd_node_add(args):
    cluster = _resolve_cluster(args)
    if cluster is None:
        return EXIT_FAIL

    role = (getattr(args, "role", None) or "leader").lower()
    if role not in ("leader", "standby"):
        print(f"--role must be 'leader' or 'standby', not {role!r}",
              file=sys.stderr)
        return EXIT_USAGE

    if role == "standby":
        if not args.leader:
            print("--role standby needs --leader NODE: the Spock node the "
                  "standby follows", file=sys.stderr)
            return EXIT_USAGE
        # A physical replica is a byte-for-byte copy, so its version is the
        # leader's — there is nothing to choose.
        if args.pg_version or args.spock_major or args.spock_branch:
            print("--pg-version, --spock-major and --spock-branch do not apply "
                  "to a standby: a physical replica is a byte-for-byte copy of "
                  "its leader and runs exactly what the leader runs",
                  file=sys.stderr)
            return EXIT_USAGE
        result = add_standby.add(
            cluster_name=cluster,
            leader_name=args.leader,
            host_name=args.host,
            db_password=args.db_password,
            synchronous_mode=args.sync_mode,
            synchronous_node_count=args.sync_count,
            synchronous_mode_strict=args.sync_strict or None,
        )
        kind = "add-standby"
    else:
        if args.leader:
            print("--leader only applies with --role standby; a Spock node "
                  "leads its own scope. Use --source to choose the node the "
                  "join runs through.", file=sys.stderr)
            return EXIT_USAGE
        if args.sync_mode or args.sync_count or args.sync_strict:
            print("--sync-mode, --sync-count and --sync-strict describe how a "
                  "leader replicates to its standbys, so they apply with "
                  "--role standby. A new Spock node starts its own scope with "
                  "no standby to replicate to.", file=sys.stderr)
            return EXIT_USAGE
        result = add_node.add(
            cluster_name=cluster,
            host_name=args.host,
            node_name=args.name,
            source_node=args.source,
            inventory_path=args.inventory,
            db_password=args.db_password,
            skip_verify=args.skip_verify,
            pg_version=args.pg_version,
            spock_major=args.spock_major,
            spock_branch=args.spock_branch,
        )
        kind = "add-node"

    if result["outcome"] == "succeeded":
        _write_report(result, kind)
        return EXIT_OK
    print(f"\nFailed: {result.get('failure')}", file=sys.stderr)
    print(f"Logs  : {result.get('log_dir')}", file=sys.stderr)
    return EXIT_FAIL


def cmd_node_remove(args):
    cluster = _resolve_cluster(args)
    if not args.yes:
        print(
            f"This removes Spock node '{args.name}' from cluster '{cluster}': "
            f"its subscriptions are dropped in both directions and it is "
            f"deregistered from every peer."
            + ("  Its data directory will be DELETED." if args.wipe_data else
               "  Its data directory will be left in place.")
        )
        answer = input(f"Type '{args.name}' to confirm: ").strip()
        if answer != args.name:
            print("Aborted — nothing was changed.")
            return EXIT_OK

    result = remove_node.remove(
        cluster_name=cluster, node_name=args.name,
        db_password=args.db_password, wipe_data=args.wipe_data,
        drain_timeout=args.drain_timeout, force=args.force,
    )
    if result["outcome"] == "succeeded":
        _write_report(result, "remove-node")
        return EXIT_OK
    print(f"\nFailed: {result.get('failure')}", file=sys.stderr)
    print(f"Logs  : {result.get('log_dir')}", file=sys.stderr)
    return EXIT_FAIL


def cmd_node_command(args):
    try:
        result = node_access.run_command(
            _resolve_cluster(args), args.on, args.exec_command,
            kind="sql" if args.sql else "shell",
            db_password=args.db_password, user=args.user,
        )
    except node_access.NodeAccessError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_USAGE

    if args.json:
        print(json.dumps(result, indent=2, default=str))
    else:
        print(node_access.format_command(result, compare=args.compare))
    return EXIT_FAIL if result["failed"] else EXIT_OK


def cmd_node_ssh(args):
    try:
        node_access.open_shell(_resolve_cluster(args), args.name,
                               db_password=args.db_password)
    except node_access.NodeAccessError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_USAGE
    return EXIT_OK


def cmd_node_psql(args):
    try:
        node_access.open_psql(_resolve_cluster(args), args.name,
                              db_password=args.db_password)
    except node_access.NodeAccessError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_USAGE
    return EXIT_OK


def _write_report(result, kind):
    """Generate an HTML report for an operation that changed the cluster."""
    from pathlib import Path
    from reports import report_generator

    steps = result.get("steps", [])
    directory = report_generator.generate(
        {
            "run_id": Path(result["log_dir"]).name,
            "cluster": result["cluster"],
            "outcome": result["outcome"],
            "duration": result.get("duration", 0),
            "counts": {
                "passed": sum(1 for s in steps if s["status"] == "passed"),
                "failed": sum(1 for s in steps if s["status"] == "failed"),
            },
            "steps": steps,
            "warnings": result.get("warnings", []),
            "plan": result.get("plan"),
            "results": {"health": result.get("health")},
            "health": result.get("health"),
            "log_dir": result["log_dir"],
        },
        kind=kind,
    )
    print(f"\nReport: {directory / 'report.html'}")


# ===========================================================================
# spock
# ===========================================================================


def cmd_spock(args):
    plan, _ = _load(args)
    try:
        node = _node(plan, args.node)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_USAGE

    action = args.spock_action

    def run(executor_for):
        executor = executor_for(node)
        sp = spock_operations

        # --- read-only listings -----------------------------------
        if action == "node-list":
            rows = sp.node_list(executor, plan, node) or []
            _emit(rows, args.json, lambda r: _table(
                r, ["node_id", "node_name", "location", "country"]))
            return EXIT_OK

        if action == "repset-list":
            rows = sp.repset_list(executor, plan, node) or []
            _emit(rows, args.json, lambda r: _table(
                r, ["set_name", "insert", "update", "delete", "truncate"]))
            return EXIT_OK

        if action == "repset-list-tables":
            rows = sp.repset_list_tables(executor, plan, node,
                                         schema=args.schema) or []
            _emit(rows, args.json,
                  lambda r: _table(r, ["set_name", "schema", "table"]))
            return EXIT_OK

        if action == "sub-list":
            rows = sp.sub_list(executor, plan, node) or []
            _emit(rows, args.json,
                  lambda r: _table(r, ["sub_name", "enabled", "slot_name"]))
            return EXIT_OK

        if action == "sub-show-status":
            # Across every Spock node, because a subscription's health is only
            # visible from the subscriber side.
            all_rows = []
            for target in (plan.spock_nodes if args.node is None else [node]):
                rows = sp.sub_show_status(executor_for(target), plan, target,
                                          args.subscription) or []
                for row in rows:
                    row["on_node"] = target.name
                all_rows += rows
            _emit(all_rows, args.json, lambda r: _table(
                r, ["on_node", "subscription_name", "status", "provider_node",
                    "replication_sets"]))
            bad = [r for r in all_rows
                   if (r.get("status") or "").lower() not in
                   ("replicating", "running")]
            return EXIT_FAIL if bad else EXIT_OK

        if action == "sub-show-table":
            rows = sp.sub_show_table(executor, plan, node, args.subscription,
                                     args.table) or []
            _emit(rows, args.json,
                  lambda r: _table(r, ["schema", "table", "status"]))
            return EXIT_OK

        if action == "lag":
            all_rows = []
            for target in plan.nodes:
                rows = sp.replication_lag(executor_for(target), plan, target) or []
                for row in rows:
                    row["on_node"] = target.name
                all_rows += rows
            _emit(all_rows, args.json, lambda r: _table(
                r, ["on_node", "slot_name", "slot_type", "active",
                    "lag_pretty"]))
            return EXIT_OK

        if action == "sequences":
            rows = sp.list_sequences(executor, plan, node,
                                     schema=args.schema) or []
            _emit(rows, args.json, lambda r: _table(
                r, ["schema", "sequence", "snowflake_available"]))
            return EXIT_OK

        if action == "no-primary-key":
            rows = sp.tables_without_primary_key(
                executor, plan, node, (args.schema,)
            )
            if args.json:
                print(json.dumps(rows, indent=2))
            elif rows:
                print("Tables with no primary key — Spock replicates INSERTs "
                      "only, so UPDATEs and DELETEs are silently dropped:\n")
                print(_table(rows, ["schema", "table", "replica_identity"]))
                print("\nFix with a primary key, or "
                      "ALTER TABLE <t> REPLICA IDENTITY FULL;")
            else:
                print("Every table has a primary key or REPLICA IDENTITY FULL.")
            return EXIT_FAIL if rows else EXIT_OK

        # --- mutations --------------------------------------------
        handlers = {
            "repset-create": lambda: sp.repset_create(
                executor, plan, node, args.repset),
            "repset-drop": lambda: sp.repset_drop(
                executor, plan, node, args.repset),
            "repset-add-table": lambda: sp.repset_add_table(
                executor, plan, node, args.repset, args.table,
                sync_data=not args.no_sync),
            "repset-remove-table": lambda: sp.repset_remove_table(
                executor, plan, node, args.repset, args.table),
            "sub-drop": lambda: sp.sub_drop(
                executor, plan, node, args.subscription),
            "sub-enable": lambda: sp.sub_enable(
                executor, plan, node, args.subscription),
            "sub-disable": lambda: sp.sub_disable(
                executor, plan, node, args.subscription),
            "sub-add-repset": lambda: sp.sub_add_repset(
                executor, plan, node, args.subscription, args.repset),
            "sub-remove-repset": lambda: sp.sub_remove_repset(
                executor, plan, node, args.subscription, args.repset),
            "sub-resync-table": lambda: sp.sub_resync_table(
                executor, plan, node, args.subscription, args.table,
                truncate=args.truncate),
            "sub-wait-for-sync": lambda: sp.sub_wait_for_sync(
                executor, plan, node, args.subscription),
            "replicate-ddl": lambda: sp.replicate_ddl(
                executor, plan, node, args.sql_command),
            "sequence-convert": lambda: sp.sequence_convert(
                executor, plan, node, args.sequence),
        }

        if action == "replication-begin":
            ok, output, no_pk = sp.repset_add_all_tables(
                executor, plan, node, args.repset, (args.schema,),
                sync_data=not args.no_sync,
            )
            print(f"{'OK' if ok else 'FAILED'} on {node.name}: added all tables "
                  f"in {args.schema} to repset {args.repset}")
            if output:
                print(f"  {output[:500]}")
            if no_pk:
                print(f"\n{len(no_pk)} table(s) have no primary key — Spock "
                      f"replicates INSERTs only for these:")
                for entry in no_pk:
                    print(f"  - {entry['schema']}.{entry['table']}")
            return EXIT_OK if ok else EXIT_FAIL

        handler = handlers.get(action)
        if handler is None:
            print(f"unknown spock action {action!r}", file=sys.stderr)
            return EXIT_USAGE

        ok, output = handler()
        print(f"{'OK' if ok else 'FAILED'} on {node.name}: {action}")
        if output:
            print(f"  {output[:1500]}")
        return EXIT_OK if ok else EXIT_FAIL

    return _with_pool(plan, run)


# ===========================================================================
# db
# ===========================================================================


def cmd_db(args):
    plan, _ = _load(args)
    try:
        node = _node(plan, args.node)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_USAGE

    action = args.db_action

    def run(executor_for):
        executor = executor_for(node)
        db = db_operations

        if action == "list":
            rows = db.list_databases(executor, plan, node) or []
            _emit(rows, args.json, lambda r: _table(
                r, ["database", "owner", "encoding", "size"]))
            return EXIT_OK

        if action == "tables":
            rows = db.list_tables(executor, plan, node, schema=args.schema) or []
            _emit(rows, args.json, lambda r: _table(
                r, ["schema", "table", "row_estimate", "size",
                    "has_primary_key"]))
            return EXIT_OK

        if action == "stats":
            stats = db.database_stats(executor, plan, node, args.name)
            _emit(stats, args.json, lambda s: "\n".join(
                f"  {k:<12} {v}" for k, v in s.items()))
            return EXIT_OK

        if action == "guc-show":
            rows = db.guc_show(executor, plan, node, args.pattern) or []
            _emit(rows, args.json, lambda r: _table(
                r, ["name", "setting", "unit", "source", "context",
                    "pending_restart"]))
            return EXIT_OK

        if action == "guc-set":
            ok, message, needs_restart = db.guc_set(
                executor, plan, node, args.name, args.value,
                through_patroni=not args.local_only,
            )
            print(f"{'OK' if ok else 'FAILED'}: {message}")
            return EXIT_OK if ok else EXIT_FAIL

        if action == "guc-reset":
            ok, output = db.guc_reset(executor, plan, node, args.name)
            print(f"{'OK' if ok else 'FAILED'}: {args.name} reset. {output[:400]}")
            return EXIT_OK if ok else EXIT_FAIL

        if action == "set-readonly":
            readonly = args.mode == "on"
            targets = plan.spock_nodes if args.all_nodes else [node]
            failures = []
            for target in targets:
                ok, message = db.set_readonly(
                    executor_for(target), plan, target, readonly=readonly,
                    through_patroni=not args.local_only,
                )
                print(f"  {'OK' if ok else 'FAILED'} {target.name}: {message}")
                if not ok:
                    failures.append(target.name)
            if readonly:
                print("\nRead-only nodes still apply incoming replication — "
                      "the apply worker is not subject to this GUC.")
            return EXIT_FAIL if failures else EXIT_OK

        if action == "create":
            ok, results = db.create_database(
                executor, plan, node, args.name, owner=args.owner,
                password=args.owner_password,
            )
            for step, step_ok, detail in results:
                print(f"  {'OK' if step_ok else 'FAILED'} {step}"
                      + (f": {detail[:200]}" if detail and not step_ok else ""))
            if ok:
                print(f"\nDatabase {args.name} created on {node.name}. It is NOT "
                      f"replicated: create it on every Spock node and cross-wire "
                      f"it, or use the same name at deploy time.")
            return EXIT_OK if ok else EXIT_FAIL

        if action == "drop":
            if not args.yes:
                answer = input(f"Type '{args.name}' to drop that database on "
                               f"{node.name}: ").strip()
                if answer != args.name:
                    print("Aborted.")
                    return EXIT_OK
            ok, output = db.drop_database(executor, plan, node, args.name,
                                          force=args.force)
            print(f"{'OK' if ok else 'FAILED'}: {output[:400]}")
            return EXIT_OK if ok else EXIT_FAIL

        if action == "test-io":
            print(f"Running fio against {node.data_dir} on {node.name} "
                  f"({args.runtime}s)...")
            ok, result = db.test_io(executor, plan, node, size=args.size,
                                   runtime=args.runtime,
                                   block_size=args.block_size)
            if args.json:
                print(json.dumps(result, indent=2))
            elif ok:
                for key, value in result.items():
                    print(f"  {key:<22} {value}")
            else:
                print(f"  FAILED: {result.get('error')}", file=sys.stderr)
            return EXIT_OK if ok else EXIT_FAIL

        print(f"unknown db action {action!r}", file=sys.stderr)
        return EXIT_USAGE

    return _with_pool(plan, run)


# ===========================================================================
# service
# ===========================================================================


def cmd_service(args):
    plan, _ = _load(args)
    action = args.service_action

    def run(executor_for):
        svc = cluster_services

        if action == "status":
            entries = svc.cluster_status(executor_for, plan)
            if args.json:
                print(json.dumps(entries, indent=2, default=str))
            else:
                rows = []
                for entry in entries:
                    rows.append({
                        "node": entry["node"],
                        "host": entry["host"],
                        "patroni": "active" if entry["patroni"]["active"] else "DOWN",
                        "enabled": entry["patroni"]["enabled"],
                        "pg_role": entry["postgres"]["role"] or "-",
                        "pg_state": entry["postgres"]["state"] or "-",
                        "etcd": ("active" if entry.get("etcd", {}).get("active")
                                 else ("DOWN" if "etcd" in entry else "-")),
                    })
                print(_table(rows, ["node", "host", "patroni", "enabled",
                                    "pg_role", "pg_state", "etcd"]))
            down = [e["node"] for e in entries if not e["patroni"]["active"]]
            return EXIT_FAIL if down else EXIT_OK

        try:
            node = _node(plan, args.node, spock_only=False)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return EXIT_USAGE
        executor = executor_for(node)

        if action == "logs":
            print(svc.logs(executor, plan, node, component=args.component,
                           lines=args.lines))
            return EXIT_OK

        if action == "switchover":
            ok, message = svc.switchover(executor, plan, node, args.candidate)
            print(f"{'OK' if ok else 'FAILED'}: {message}")
            return EXIT_OK if ok else EXIT_FAIL

        if action == "reinit":
            if not args.yes:
                answer = input(f"Rebuild {args.member} from its leader, "
                               f"discarding its data? Type yes: ").strip()
                if answer != "yes":
                    print("Aborted.")
                    return EXIT_OK
            ok, message = svc.reinitialise(executor, plan, node, args.member)
            print(f"{'OK' if ok else 'FAILED'}: {message[:800]}")
            return EXIT_OK if ok else EXIT_FAIL

        dispatch = {
            "start": svc.start, "stop": svc.stop, "restart": svc.restart,
            "reload": svc.reload, "enable": svc.enable, "disable": svc.disable,
        }
        handler = dispatch.get(action)
        if handler is None:
            print(f"unknown service action {action!r}", file=sys.stderr)
            return EXIT_USAGE

        ok, message = handler(executor, plan, node, component=args.component)
        print(f"{'OK' if ok else 'FAILED'} {node.name}/{args.component}: {message}")
        return EXIT_OK if ok else EXIT_FAIL

    return _with_pool(plan, run)


# ===========================================================================
# package
# ===========================================================================


def cmd_package(args):
    cluster = _resolve_cluster(args)
    if cluster is None:
        return EXIT_FAIL
    action = args.package_action

    if action == "list":
        result = node_access.package_list(cluster, selector=args.on,
                                          db_password=args.db_password)
        if args.json:
            print(json.dumps(result, indent=2, default=str))
        else:
            print(node_access.format_packages(result))
        return EXIT_FAIL if result["drift"] else EXIT_OK

    log = RunLogger(new_run_id(f"{cluster}-package-{action}"))
    if action == "install":
        result = node_access.package_install(cluster, args.packages,
                                            selector=args.on,
                                            db_password=args.db_password,
                                            run_logger=log)
    elif action == "remove":
        result = node_access.package_remove(cluster, args.packages,
                                           selector=args.on,
                                           db_password=args.db_password,
                                           run_logger=log)
    elif action == "upgrade":
        result = node_access.package_upgrade(cluster, args.packages,
                                            selector=args.on,
                                            db_password=args.db_password,
                                            run_logger=log)
    else:
        print(f"unknown package action {action!r}", file=sys.stderr)
        return EXIT_USAGE

    if args.json:
        print(json.dumps(result, indent=2, default=str))
        return EXIT_OK if result["ok"] else EXIT_FAIL

    for entry in result["results"]:
        print(f"  {entry['host']}: " + ", ".join(
            f"{k}={v}" for k, v in entry.items() if k != "host" and k != "output"
        ))
    for problem in result["problems"]:
        print(f"  PROBLEM {problem}", file=sys.stderr)
    for step in result.get("next_steps", []):
        print(f"  {step}")
    return EXIT_OK if result["ok"] else EXIT_FAIL


# ===========================================================================
# app
# ===========================================================================


def cmd_app(args):
    plan, _ = _load(args)
    action = args.app_action
    log = RunLogger(new_run_id(f"{plan.cluster_name}-app-{action}"))

    def run(executor_for):
        if action == "install":
            ok, result = app_management.install(
                executor_for, plan, app=args.app, scale=args.scale,
                rows=args.rows, add_to_repset=not args.no_repset,
                run_logger=log,
            )
        elif action == "remove":
            ok, result = app_management.remove(executor_for, plan, app=args.app,
                                               run_logger=log)
        elif action == "counts":
            counts = app_management.row_counts(executor_for, plan, app=args.app)
            if args.json:
                print(json.dumps(counts, indent=2))
            else:
                tables = sorted({t for c in counts.values() for t in c})
                rows = [{"node": n, **{t: c.get(t, "?") for t in tables}}
                        for n, c in counts.items()]
                print(_table(rows, ["node"] + tables))
                distinct = {tuple(c.get(t) for t in tables)
                            for c in counts.values()}
                print("\n" + ("All nodes agree." if len(distinct) == 1 else
                              "Nodes DISAGREE — replication may still be "
                              "catching up, or the cluster has diverged. "
                              "Check with: diff table"))
            return EXIT_OK
        elif action == "run":
            try:
                node = _node(plan, args.node)
            except ValueError as exc:
                print(str(exc), file=sys.stderr)
                return EXIT_USAGE
            ok, output = app_management.run_pgbench(
                executor_for(node), plan, node, clients=args.clients,
                jobs=args.jobs, duration=args.duration,
                read_only=args.read_only, run_logger=log,
            )
            print(output)
            return EXIT_OK if ok else EXIT_FAIL
        elif action == "concurrent-index":
            ok, result = app_management.concurrent_index(
                executor_for, plan, args.table, args.column,
                index_name=args.index_name, run_logger=log,
            )
        else:
            print(f"unknown app action {action!r}", file=sys.stderr)
            return EXIT_USAGE

        if args.json:
            print(json.dumps(result, indent=2, default=str))
        else:
            for warning in result.get("warnings", []):
                print(f"  WARN    {warning}")
            for problem in result.get("problems", []):
                print(f"  PROBLEM {problem}")
            if ok:
                print(f"\nOK: {action} completed")
        return EXIT_OK if ok else EXIT_FAIL

    return _with_pool(plan, run)


# ===========================================================================
# diff
# ===========================================================================


def cmd_diff(args):
    plan, _ = _load(args)
    action = args.diff_action
    log = RunLogger(new_run_id(f"{plan.cluster_name}-diff"))

    def run(executor_for):
        if action == "spock":
            ok, payload = consistency.spock_diff(executor_for, plan)
            return _report(ok, payload, args.json, "Spock metadata")

        if action == "repset":
            ok, payload = consistency.repset_diff(executor_for, plan,
                                                 repset=args.repset,
                                                 schema=args.schema)
            return _report(ok, payload, args.json,
                           f"Replication set {args.repset!r}")

        if action == "schema":
            ok, payload = consistency.schema_diff(executor_for, plan,
                                                 schema=args.schema)
            if not args.json:
                counts = payload.get("object_counts", {})
                if counts:
                    print("Object counts per node:")
                    rows = [{"node": n, **c} for n, c in counts.items()]
                    print(_table(rows, ["node", "columns", "indexes",
                                        "constraints"]))
                    print()
            return _report(ok, payload, args.json, f"Schema {args.schema!r}")

        if action == "table":
            ok, payload = consistency.table_diff(
                executor_for, plan, args.table, buckets=args.buckets,
                where=args.where, drill_down=not args.no_drill_down,
                run_logger=log if not args.json else None,
            )
            if not args.json and payload.get("row_counts"):
                print("\nRow counts:")
                for name, count in payload["row_counts"].items():
                    print(f"  {name:<12} {count}")
                for name, mismatch in (payload.get("mismatches") or {}).items():
                    keys = mismatch.get("keys") or {}
                    for label, values in keys.items():
                        if values:
                            sample = ", ".join(values[:10])
                            more = (f" (+{len(values) - 10} more)"
                                    if len(values) > 10 else "")
                            print(f"\n  {name} {label}: {sample}{more}")
                print()
            return _report(ok, payload, args.json, f"Table {args.table}")

        if action == "all":
            ok, payload = consistency.full_check(
                executor_for, plan, schema=args.schema,
                tables=[t.strip() for t in args.tables.split(",")]
                if args.tables else None,
                buckets=args.buckets, run_logger=log if not args.json else None,
            )
            if args.json:
                print(json.dumps(payload, indent=2, default=str))
                return EXIT_OK if ok else EXIT_FAIL
            print(f"\nCluster {payload['cluster']}: "
                  f"{'CONSISTENT' if ok else 'DIFFERENCES FOUND'}")
            for name, check in payload["checks"].items():
                if name == "tables":
                    for table, result in check.items():
                        marker = "OK" if result["ok"] else "DIFF"
                        print(f"  [{marker:>4}] table {table}")
                else:
                    marker = "OK" if check["ok"] else "DIFF"
                    print(f"  [{marker:>4}] {name}")
            if payload["problems"]:
                print(f"\n{len(payload['problems'])} finding(s):")
                for problem in payload["problems"]:
                    print(f"  - {problem}")
            return EXIT_OK if ok else EXIT_FAIL

        if action == "repair":
            if not args.yes:
                print(
                    f"This OVERWRITES {args.table} on the target nodes with "
                    f"{args.source}'s copy. Rows that exist only on a target "
                    f"are lost."
                )
                answer = input("Type 'repair' to confirm: ").strip()
                if answer != "repair":
                    print("Aborted — nothing was changed.")
                    return EXIT_OK
            targets = ([t.strip() for t in args.targets.split(",")]
                       if args.targets else None)
            ok, payload = consistency.table_repair(
                executor_for, plan, args.table, args.source,
                target_node_names=targets, truncate=not args.no_truncate,
                run_logger=log,
            )
            if args.json:
                print(json.dumps(payload, indent=2, default=str))
            else:
                for entry in payload.get("results", []):
                    print(f"  {'OK' if entry['ok'] else 'FAILED'} "
                          f"{entry['node']} via {entry['subscription']}")
                for problem in payload.get("problems", []):
                    print(f"  PROBLEM {problem}")
                if ok:
                    print(f"\nOK: {args.table} resynced from {args.source}. "
                          f"Verify with: diff table {args.table}")
            return EXIT_OK if ok else EXIT_FAIL

        print(f"unknown diff action {action!r}", file=sys.stderr)
        return EXIT_USAGE

    return _with_pool(plan, run)


# ===========================================================================
# Parser registration
# ===========================================================================


def register(subparsers, add_common):
    """Add every operations group to the main CLI's subparsers."""
    _register_node(subparsers, add_common)
    _register_spock(subparsers, add_common)
    _register_db(subparsers, add_common)
    _register_service(subparsers, add_common)
    _register_package(subparsers, add_common)
    _register_app(subparsers, add_common)
    _register_diff(subparsers, add_common)


def _register_node(subparsers, add_common):
    node = subparsers.add_parser(
        "node", help="list, add, remove and reach into cluster nodes"
    )
    actions = node.add_subparsers(dest="node_action", required=True)

    listing = actions.add_parser("list", help="list every node in the cluster")
    add_common(listing)
    listing.add_argument("--json", action="store_true")
    listing.add_argument("--no-probe", action="store_true",
                         help="skip the live role check (faster, offline-safe)")
    listing.set_defaults(func=cmd_node_list)

    adding = actions.add_parser(
        "add", help="add a new Spock node (multi-master peer) to the cluster"
    )
    add_common(adding)
    adding.add_argument("--host", help="host to place it on; default: a host "
                                      "with no Spock node yet")
    adding.add_argument("--name", help="node name; default: the next free nN")
    adding.add_argument("--source", help="node to join through; default: n1")
    adding.add_argument("--role", default="leader", choices=("leader", "standby"),
                        help="leader: a Spock node leading its own scope; "
                             "standby: a Patroni replica of an existing node "
                             "[leader]")
    adding.add_argument("--leader", help="with --role standby, the Spock node "
                                        "the standby follows")
    adding.add_argument("--pg-version", default="",
                        help="PostgreSQL version for the new node; must be the "
                             "cluster's version or newer [the cluster's]")
    adding.add_argument("--spock-major", default="", choices=("", "50", "60"),
                        help="Spock major for the new node [the cluster's]")
    adding.add_argument("--spock-branch", default="",
                        help="Spock git branch to build, for a source-built "
                             "cluster [the cluster's]")
    adding.add_argument("--sync-mode", default=None,
                        choices=("off", "on", "quorum", "async", "sync"),
                        help="with --role standby: how the leader replicates "
                             "to it — async/off, sync/on, or quorum "
                             "[the cluster's]")
    adding.add_argument("--sync-count", type=int, default=None,
                        help="with --role standby: synchronous standbys in "
                             "that scope [1]")
    adding.add_argument("--sync-strict", action="store_true",
                        help="with --role standby and a synchronous mode: "
                             "block writes when no standby is available")
    adding.add_argument("--skip-verify", action="store_true")
    adding.set_defaults(func=cmd_node_add)

    removing = actions.add_parser("remove", help="remove a Spock node")
    add_common(removing)
    removing.add_argument("name", help="node to remove")
    removing.add_argument("--wipe-data", action="store_true",
                          help="also delete its data directory")
    removing.add_argument("--drain-timeout", type=int, default=300,
                          help="seconds to wait for its WAL to drain [300]")
    removing.add_argument("--force", action="store_true",
                          help="proceed despite un-drained WAL or an "
                               "unreachable host")
    removing.add_argument("--yes", action="store_true", help="skip confirmation")
    removing.set_defaults(func=cmd_node_remove)

    running = actions.add_parser(
        "command", help="run a shell command or SQL statement on nodes"
    )
    add_common(running)
    running.add_argument("exec_command", metavar="COMMAND")
    running.add_argument("--on", default="all",
                         help="all | spock | standby | node,node | host [all]")
    running.add_argument("--sql", action="store_true",
                         help="run it as SQL through psql instead of a shell")
    running.add_argument("--user", default="root",
                         help="OS user for shell commands [root]")
    running.add_argument("--compare", action="store_true",
                         help="group nodes by identical output")
    running.add_argument("--json", action="store_true")
    running.set_defaults(func=cmd_node_command)

    sshing = actions.add_parser("ssh", help="open an interactive shell on a node's host")
    add_common(sshing)
    sshing.add_argument("name", help="node or host name")
    sshing.set_defaults(func=cmd_node_ssh)

    psqling = actions.add_parser("psql", help="open an interactive psql on a node")
    add_common(psqling)
    psqling.add_argument("name", help="node name")
    psqling.set_defaults(func=cmd_node_psql)


def _register_spock(subparsers, add_common):
    spock = subparsers.add_parser(
        "spock", help="Spock nodes, replication sets, subscriptions, DDL"
    )
    actions = spock.add_subparsers(dest="spock_action", required=True)

    def base(name, help_text, node_default=True):
        parser = actions.add_parser(name, help=help_text)
        add_common(parser)
        parser.add_argument("--node", help="node to act on [first Spock node]")
        parser.add_argument("--json", action="store_true")
        parser.set_defaults(func=cmd_spock)
        return parser

    base("node-list", "list spock.node")
    base("repset-list", "list replication sets")
    base("sub-list", "list subscriptions")

    p = base("repset-list-tables", "list tables per replication set")
    p.add_argument("--schema", default="public")

    p = base("sub-show-status", "subscription state on every node")
    p.add_argument("--subscription", default="*")

    p = base("sub-show-table", "sync state of one table in a subscription")
    p.add_argument("--subscription", required=True)
    p.add_argument("--table", required=True)

    base("lag", "replication slot lag on every node")

    p = base("sequences", "list sequences and Snowflake availability")
    p.add_argument("--schema", default="public")

    p = base("no-primary-key", "tables Spock can only replicate INSERTs for")
    p.add_argument("--schema", default="public")

    p = base("replication-begin", "add all tables to a replication set")
    p.add_argument("--repset", default="default")
    p.add_argument("--schema", default="public")
    p.add_argument("--no-sync", action="store_true",
                   help="do not copy existing rows to subscribers")

    p = base("repset-create", "create a replication set")
    p.add_argument("repset")

    p = base("repset-drop", "drop a replication set")
    p.add_argument("repset")

    p = base("repset-add-table", "add a table to a replication set")
    p.add_argument("repset")
    p.add_argument("table", help="schema.table, or 'public.*'")
    p.add_argument("--no-sync", action="store_true")

    p = base("repset-remove-table", "remove a table from a replication set")
    p.add_argument("repset")
    p.add_argument("table")

    for name, help_text in (
        ("sub-drop", "drop a subscription"),
        ("sub-enable", "enable a subscription"),
        ("sub-disable", "disable a subscription"),
        ("sub-wait-for-sync", "block until a subscription has synced"),
    ):
        p = base(name, help_text)
        p.add_argument("subscription")

    for name, help_text in (("sub-add-repset", "add a repset to a subscription"),
                            ("sub-remove-repset",
                             "remove a repset from a subscription")):
        p = base(name, help_text)
        p.add_argument("subscription")
        p.add_argument("repset")

    p = base("sub-resync-table", "re-copy one table from its provider")
    p.add_argument("subscription")
    p.add_argument("table")
    p.add_argument("--truncate", action="store_true",
                   help="empty the local copy first (exact copy, not a merge)")

    p = base("replicate-ddl", "push a DDL statement through replication sets")
    p.add_argument("sql_command", metavar="SQL")

    p = base("sequence-convert", "convert sequences to Snowflake sequences")
    p.add_argument("--sequence", default="public.*")


def _register_db(subparsers, add_common):
    db = subparsers.add_parser("db", help="databases, GUCs, read-only mode, IO")
    actions = db.add_subparsers(dest="db_action", required=True)

    def base(name, help_text):
        parser = actions.add_parser(name, help=help_text)
        add_common(parser)
        parser.add_argument("--node", help="node to act on [first Spock node]")
        parser.add_argument("--json", action="store_true")
        parser.set_defaults(func=cmd_db)
        return parser

    base("list", "list databases")

    p = base("tables", "list tables with sizes and PK status")
    p.add_argument("--schema", default="public")

    p = base("stats", "size, connections and table count")
    p.add_argument("--name", help="database [the cluster's database]")

    p = base("guc-show", "show settings matching a pattern")
    p.add_argument("--pattern", default="%", help="SQL LIKE pattern [%%]")

    p = base("guc-set", "set a GUC through the Patroni DCS")
    p.add_argument("name")
    p.add_argument("value")
    p.add_argument("--local-only", action="store_true",
                   help="ALTER SYSTEM on this node only; Patroni may revert it")

    p = base("guc-reset", "remove a GUC override from the DCS")
    p.add_argument("name")

    p = base("set-readonly", "turn read-only mode on or off")
    p.add_argument("mode", choices=("on", "off"))
    p.add_argument("--all-nodes", action="store_true",
                   help="apply to every Spock node")
    p.add_argument("--local-only", action="store_true")

    p = base("create", "create a database with the Spock extensions")
    p.add_argument("name")
    p.add_argument("--owner")
    p.add_argument("--owner-password")

    p = base("drop", "drop a database")
    p.add_argument("name")
    p.add_argument("--force", action="store_true", help="disconnect users first")
    p.add_argument("--yes", action="store_true")

    p = base("test-io", "run fio against the node's data directory")
    p.add_argument("--size", default="1G")
    p.add_argument("--runtime", type=int, default=10)
    p.add_argument("--block-size", default="8k")


def _register_service(subparsers, add_common):
    service = subparsers.add_parser(
        "service", help="control Patroni, PostgreSQL and etcd per node"
    )
    actions = service.add_subparsers(dest="service_action", required=True)

    def base(name, help_text, with_component=True):
        parser = actions.add_parser(name, help=help_text)
        add_common(parser)
        parser.add_argument("--node", help="node to act on")
        parser.add_argument("--json", action="store_true")
        if with_component:
            parser.add_argument("--component", default="patroni",
                                choices=cluster_services.COMPONENTS,
                                help="patroni | postgres | etcd [patroni]")
        parser.set_defaults(func=cmd_service)
        return parser

    base("status", "service status for every node", with_component=False)
    base("start", "start a component")
    base("stop", "stop a component")
    base("restart", "restart a component")
    base("reload", "reload configuration without a restart")
    base("enable", "start on boot")
    base("disable", "do not start on boot")

    p = base("logs", "recent log output")
    p.add_argument("--lines", type=int, default=50)

    p = base("switchover", "hand a scope's leadership to a standby",
             with_component=False)
    p.add_argument("--candidate", help="standby to promote [the first one]")

    p = base("reinit", "rebuild a member from its leader", with_component=False)
    p.add_argument("member", help="member to rebuild")
    p.add_argument("--yes", action="store_true")


def _register_package(subparsers, add_common):
    package = subparsers.add_parser(
        "package", help="native package inventory and updates (the CLI's um)"
    )
    actions = package.add_subparsers(dest="package_action", required=True)

    def base(name, help_text):
        parser = actions.add_parser(name, help=help_text)
        add_common(parser)
        parser.add_argument("--on", default="all",
                            help="all | spock | standby | node,node | host")
        parser.add_argument("--json", action="store_true")
        parser.set_defaults(func=cmd_package)
        return parser

    base("list", "installed pgEdge packages per host, with drift flagged")

    p = base("install", "install packages")
    p.add_argument("packages", help="comma-separated package names")

    p = base("remove", "remove packages")
    p.add_argument("packages")

    p = base("upgrade", "upgrade packages (all pgEdge ones by default)")
    p.add_argument("--packages", help="comma-separated; default: every pgEdge "
                                      "package installed")


def _register_app(subparsers, add_common):
    app = subparsers.add_parser("app", help="test workloads for exercising a cluster")
    actions = app.add_subparsers(dest="app_action", required=True)

    def base(name, help_text):
        parser = actions.add_parser(name, help=help_text)
        add_common(parser)
        parser.add_argument("--app", default="pgbench",
                            choices=app_management.APPS)
        parser.add_argument("--json", action="store_true")
        parser.set_defaults(func=cmd_app)
        return parser

    p = base("install", "install a workload and add its tables to a repset")
    p.add_argument("--scale", type=int, default=1, help="pgbench scale [1]")
    p.add_argument("--rows", type=int, default=1000, help="sample rows [1000]")
    p.add_argument("--no-repset", action="store_true",
                   help="do not add the tables to the default replication set")

    base("remove", "drop a workload's tables from every node")
    base("counts", "row counts per node — the quickest replication check")

    p = base("run", "drive pgbench load at one node")
    p.add_argument("--node")
    p.add_argument("--clients", type=int, default=4)
    p.add_argument("--jobs", type=int, default=2)
    p.add_argument("--duration", type=int, default=30)
    p.add_argument("--read-only", action="store_true")

    p = base("concurrent-index", "build an index concurrently on every node")
    p.add_argument("table")
    p.add_argument("column")
    p.add_argument("--index-name")


def _register_diff(subparsers, add_common):
    diff = subparsers.add_parser(
        "diff", help="check whether the nodes actually agree (the CLI's ace)"
    )
    actions = diff.add_subparsers(dest="diff_action", required=True)

    def base(name, help_text):
        parser = actions.add_parser(name, help=help_text)
        add_common(parser)
        parser.add_argument("--json", action="store_true")
        parser.set_defaults(func=cmd_diff)
        return parser

    base("spock", "compare Spock metadata across nodes")

    p = base("repset", "compare replication set membership")
    p.add_argument("--repset", default="default")
    p.add_argument("--schema", default="public")

    p = base("schema", "compare columns, indexes and constraints")
    p.add_argument("--schema", default="public")

    p = base("table", "compare a table's data using bucketed checksums")
    p.add_argument("table", help="schema.table")
    p.add_argument("--buckets", type=int, default=consistency.DEFAULT_BUCKETS)
    p.add_argument("--where", help="compare only rows matching this predicate")
    p.add_argument("--no-drill-down", action="store_true",
                   help="report differing buckets but not individual keys")

    p = base("all", "run every check, over every table")
    p.add_argument("--schema", default="public")
    p.add_argument("--tables", help="comma-separated; default: every table")
    p.add_argument("--buckets", type=int, default=consistency.DEFAULT_BUCKETS)

    p = base("repair", "resync a diverged table from a chosen source node")
    p.add_argument("table")
    p.add_argument("--source", required=True, help="node to copy from")
    p.add_argument("--targets", help="comma-separated; default: every other node")
    p.add_argument("--no-truncate", action="store_true",
                   help="merge instead of replacing the target's copy")
    p.add_argument("--yes", action="store_true")
