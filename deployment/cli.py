#!/usr/bin/env python3
"""Command-line interface behind the shell wrappers.

The shell scripts handle interactive prompting and the virtualenv; everything
below the prompt lives here, so every action is equally available as a flag for
scripted and CI use:

    python3 -m deployment.cli deploy --mode packages --nodes 3 --pg-major 17 \
        --standby n1 --cluster demo
    python3 -m deployment.cli status --cluster demo
    python3 -m deployment.cli node add --cluster demo
    python3 -m deployment.cli diff all --cluster demo

Lifecycle commands (deploy, plan, status, add-standby, remove, list) live here;
the day-two groups (node, spock, db, service, package, app, diff) are defined in
deployment/ops_cli.py and registered into the same parser, so the user sees one
command tree.

Exit codes: 0 success, 1 failure, 2 bad usage.
"""

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from aspects import health, inventory, patroni_management, spock_management, state
from aspects.logging_setup import RunLogger, new_run_id
from aspects.ssh_executor import build_executor
from deployment import add_standby, cleanup, deploy_cluster, ops_cli, topology
from reports import report_generator

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_USAGE = 2


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _split_list(value):
    if not value:
        return []
    if isinstance(value, list):
        items = []
        for entry in value:
            items.extend(part.strip() for part in str(entry).split(","))
        return [item for item in items if item]
    return [part.strip() for part in str(value).split(",") if part.strip()]


def _executor_pool(plan, run_logger=None):
    cache = {}

    def executor_for(node):
        host_name = node.host if hasattr(node, "host") else node
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

    return executor_for, cache


def _close(cache):
    for executor in cache.values():
        try:
            executor.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# deploy
# ---------------------------------------------------------------------------


def cmd_deploy(args):
    options = {
        "inventory": args.inventory,
        "cluster_name": args.cluster,
        "node_count": args.nodes,
        "standby_of": _split_list(args.standby),
        "deploy_mode": args.mode,
        "pg_major": args.pg_major,
        "pg_version": args.pg_version,
        "spock_major": args.spock_major,
        "repo_channel": args.channel,
        "db_name": args.db_name,
        "db_user": args.db_user,
        "db_password": args.db_password,
        "base_port": args.base_port,
        "base_restapi_port": args.base_restapi_port,
        "data_root": args.data_root,
        "hba_cidrs": _split_list(args.hba_cidr),
        "clean": args.clean,
        "skip_verify": args.skip_verify,
        "zodan_sql": args.zodan_sql,
    }

    if args.mode == "source":
        options["source_build"] = {
            "pg_version": args.pg_version,
            "spock_branch": args.spock_branch,
            "etcd_version": args.etcd_version,
            "jobs": args.jobs,
        }
        if not args.pg_version:
            print(
                "A source build needs an exact PostgreSQL version to download "
                "(e.g. --pg-version 17.11); --pg-major alone is not enough.",
                file=sys.stderr,
            )
            return EXIT_USAGE

    if args.dry_run:
        return _cmd_plan(args)

    result = deploy_cluster.deploy(options)

    directory = report_generator.generate(result, kind="deployment")
    print(f"\nReport: {directory / 'report.html'}")
    print(f"Index : {REPO_ROOT / 'reports' / 'index.html'}")

    if result["outcome"] == "succeeded":
        print(
            f"\nWatch it live:  ./pg_dashboard.sh --cluster {result['cluster']}"
            f"\nCheck it now :  ./pg_cluster_status.sh --cluster {result['cluster']}"
        )
    return EXIT_OK if result["outcome"] == "succeeded" else EXIT_FAIL


def _cmd_plan(args):
    """Show the layout a deployment would produce, without touching anything."""
    hosts, defaults = inventory.load(args.inventory)
    plan, warnings = topology.plan_cluster(
        hosts=hosts,
        cluster_name=args.cluster or defaults.get("cluster_name", "pgedge"),
        node_count=int(args.nodes or defaults.get("node_count", 2)),
        standby_of=_split_list(args.standby),
        db_name=args.db_name or defaults.get("db_name", "postgres"),
        db_user=args.db_user or defaults.get("db_user", "postgres"),
        db_password=args.db_password or defaults.get("db_password", "postgres"),
        pg_major=args.pg_major or defaults.get("pg_major", "17"),
        pg_version=args.pg_version or "",
        spock_major=args.spock_major or defaults.get("spock_major", "50"),
        repo_channel=args.channel or defaults.get("repo_channel", "release"),
        deploy_mode=args.mode,
        base_pg_port=int(args.base_port or defaults.get("base_port", 5432)),
        base_restapi_port=int(args.base_restapi_port or defaults.get("base_restapi_port", 8008)),
        data_root=args.data_root or defaults.get("data_root", topology.DEFAULT_DATA_ROOT),
        extra_hba_cidrs=_split_list(args.hba_cidr),
    )

    if getattr(args, "json", False):
        print(json.dumps(plan.to_dict(), indent=2))
        return EXIT_OK

    print("Planned cluster (nothing has been changed):\n")
    for line in plan.summary_lines():
        print(line)

    standby_notes = topology.describe_standby_choice(plan)
    if standby_notes:
        print("\nStandby placement:")
        for note in standby_notes:
            print(f"  {note}")

    if warnings:
        print(f"\nWarnings ({len(warnings)}):")
        for warning in warnings:
            print(f"  - {warning}")
    return EXIT_OK


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


def cmd_status(args):
    cluster = args.cluster or state.latest_cluster()
    if not cluster:
        print(
            "No deployed clusters found. Run ./pg_deploy_cluster.sh first.",
            file=sys.stderr,
        )
        return EXIT_FAIL

    try:
        plan, metadata = state.load(cluster, db_password=args.db_password)
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_FAIL

    try:
        _, defaults = inventory.load(args.inventory)
    except Exception:
        defaults = {}
    plan.db_password = state.resolve_password(
        plan, explicit=args.db_password, inventory_defaults=defaults
    )

    if args.repair_failover:
        return _repair_failover(plan, args.repair_failover)

    snapshot = health.snapshot(plan)

    if args.json:
        print(json.dumps(snapshot, indent=2, default=str))
    else:
        print(health.text_report(snapshot))
        if metadata.get("last_change"):
            print(f"\nLast change: {metadata['last_change']}")

    if args.report:
        directory = report_generator.generate(
            {
                "run_id": new_run_id(f"{cluster}-status"),
                "cluster": cluster,
                "outcome": "succeeded" if snapshot["status"] == "ok" else "failed",
                "duration": 0,
                "counts": {},
                "steps": [],
                "warnings": snapshot["problems"],
                "plan": plan,
                "results": {"health": snapshot},
                "health": snapshot,
                "log_dir": metadata.get("log_dir", ""),
            },
            kind="health",
        )
        print(f"\nReport: {directory / 'report.html'}")

    return EXIT_OK if snapshot["status"] == "ok" else EXIT_FAIL


def _repair_failover(plan, node_name):
    """Point peers at a Spock node's promoted standby.

    Patroni moves the leader inside a scope; Spock's peers keep the DSN they
    were given at cross-wire time. Until they are retargeted, they replicate
    from an address that is now a read-only replica.
    """
    log = RunLogger(new_run_id(f"{plan.cluster_name}-repair"))
    executor_for, cache = _executor_pool(plan, run_logger=log)

    try:
        try:
            node = plan.node(node_name)
        except KeyError:
            print(
                f"{node_name!r} is not a node in this cluster. Spock nodes: "
                f"{', '.join(n.name for n in plan.spock_nodes)}",
                file=sys.stderr,
            )
            return EXIT_USAGE

        if not node.is_spock:
            print(
                f"{node_name} is a standby. Pass the Spock node whose peers need "
                f"retargeting — one of "
                f"{', '.join(n.name for n in plan.spock_nodes)}.",
                file=sys.stderr,
            )
            return EXIT_USAGE

        log.banner(f"Checking scope {node.scope} for a moved leader")
        status = patroni_management.cluster_status(
            executor_for(node), node, node_name=node.name
        )
        leader_name = status.get("leader")

        if not leader_name:
            log.error(f"scope {node.scope} has no leader — nothing to retarget onto")
            return EXIT_FAIL

        if leader_name == node.name:
            log.info(
                f"{node.name} is still the leader of {node.scope}; peers already "
                f"point at the right address. Nothing to do."
            )
            return EXIT_OK

        try:
            promoted = plan.node(leader_name)
        except KeyError:
            log.error(
                f"scope {node.scope} reports leader {leader_name!r}, which is not "
                f"in the saved plan — cannot determine its address"
            )
            return EXIT_FAIL

        log.info(
            f"{node.scope} has failed over: {leader_name} now leads, so "
            f"{node.name}'s peers must replicate from "
            f"{promoted.address}:{promoted.pg_port}"
        )

        results = spock_management.retarget_after_failover(
            executor_for, plan, node, promoted.address, promoted.pg_port,
            run_logger=log,
        )

        failed = [r for r in results if not r["ok"]]
        for entry in results:
            marker = "ok" if entry["ok"] else "FAILED"
            log.info(f"    [{marker}] {entry['peer']}: {entry['step']}")

        if failed:
            log.error(f"{len(failed)} retarget step(s) failed — see {log.root}")
            return EXIT_FAIL

        log.info("")
        log.info(
            "Peers retargeted. Confirm with: "
            f"./pg_cluster_status.sh --cluster {plan.cluster_name}"
        )
        return EXIT_OK
    finally:
        _close(cache)


# ---------------------------------------------------------------------------
# add-standby / remove / list
# ---------------------------------------------------------------------------


def cmd_add_standby(args):
    cluster = args.cluster or state.latest_cluster()
    if not cluster:
        print("No deployed clusters found.", file=sys.stderr)
        return EXIT_FAIL

    result = add_standby.add(
        cluster_name=cluster,
        leader_name=args.leader,
        host_name=args.host,
        db_password=args.db_password,
    )

    if result["outcome"] == "succeeded":
        directory = report_generator.generate(
            {
                "run_id": Path(result["log_dir"]).name,
                "cluster": cluster,
                "outcome": "succeeded",
                "duration": result["duration"],
                "counts": {
                    "passed": sum(1 for s in result["steps"] if s["status"] == "passed"),
                    "failed": sum(1 for s in result["steps"] if s["status"] == "failed"),
                },
                "steps": result["steps"],
                "warnings": [],
                "plan": result["plan"],
                "results": {"health": result["health"]},
                "health": result["health"],
                "log_dir": result["log_dir"],
            },
            kind="add-standby",
        )
        print(f"\nReport: {directory / 'report.html'}")
        return EXIT_OK

    print(f"\nFailed: {result.get('failure')}", file=sys.stderr)
    print(f"Logs  : {result.get('log_dir')}", file=sys.stderr)
    return EXIT_FAIL


def cmd_remove(args):
    cluster = args.cluster or state.latest_cluster()
    if not cluster:
        print("No deployed clusters found.", file=sys.stderr)
        return EXIT_FAIL

    if not args.yes:
        print(
            f"This deletes cluster '{cluster}': every node's data directory, its "
            f"Patroni configuration and its etcd state."
            + (" Packages and the pgEdge repository will also be removed."
               if args.purge else "")
        )
        answer = input("Type the cluster name to confirm: ").strip()
        if answer != cluster:
            print("Aborted — nothing was changed.")
            return EXIT_OK

    result = cleanup.remove(
        cluster_name=cluster,
        purge_packages=args.purge,
        keep_state=args.keep_state,
        db_password=args.db_password,
    )
    return EXIT_OK if result["outcome"] == "succeeded" else EXIT_FAIL


def cmd_list(args):
    clusters = state.list_clusters()
    if not clusters:
        print("No deployed clusters.")
        return EXIT_OK

    print(f"{'CLUSTER':<20} {'NODES':<7} {'MODE':<10} {'PG':<8} {'OUTCOME':<10} SAVED")
    for name in clusters:
        try:
            plan, metadata = state.load(name)
        except Exception as exc:
            print(f"{name:<20} (unreadable state: {exc})")
            continue
        print(
            f"{name:<20} {len(plan.nodes):<7} {plan.deploy_mode:<10} "
            f"{(plan.pg_version or plan.pg_major):<8} "
            f"{metadata.get('outcome', '?'):<10} {metadata.get('run_id', '')}"
        )
    return EXIT_OK


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------


def build_parser():
    parser = argparse.ArgumentParser(
        prog="deployment.cli",
        description="Deploy and operate n-node Patroni + Spock PostgreSQL clusters",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_common(sub):
        sub.add_argument("--inventory", help="path to the inventory JSON")
        sub.add_argument("--cluster", help="cluster name")
        sub.add_argument("--db-password", help="database superuser password "
                                              "(default: PG_CLUSTER_DB_PASSWORD)")

    # --- deploy -------------------------------------------------------
    deploy = subparsers.add_parser("deploy", help="deploy a new cluster")
    add_common(deploy)
    deploy.add_argument("--mode", choices=("packages", "source"), default="packages",
                        help="native pgEdge packages, or build from source")
    deploy.add_argument("--nodes", type=int, default=2,
                        help="number of Spock (multi-master) nodes")
    deploy.add_argument("--standby", action="append",
                        help="Spock node to give a Patroni standby; repeatable "
                             "or comma-separated (e.g. --standby n1,n2)")
    deploy.add_argument("--pg-major", default="17", help="PostgreSQL major version")
    deploy.add_argument("--pg-version", default="",
                        help="exact PostgreSQL version (required for --mode source)")
    deploy.add_argument("--spock-major", default="50", choices=("50", "60"),
                        help="Spock major version")
    deploy.add_argument("--channel", default="release",
                        choices=("release", "staging", "daily"),
                        help="pgEdge repository channel")
    deploy.add_argument("--db-name", default="postgres")
    deploy.add_argument("--db-user", default="postgres")
    deploy.add_argument("--base-port", type=int, default=5432)
    deploy.add_argument("--base-restapi-port", type=int, default=8008)
    deploy.add_argument("--data-root", default=topology.DEFAULT_DATA_ROOT)
    deploy.add_argument("--hba-cidr", action="append",
                        help="extra CIDR to allow in pg_hba (repeatable)")
    deploy.add_argument("--zodan-sql", default="",
                        help="override the zodan script (default: chosen by "
                             "--spock-major)")
    deploy.add_argument("--spock-branch", default="main",
                        help="Spock git branch for --mode source")
    deploy.add_argument("--etcd-version", default="3.5.17",
                        help="etcd release for --mode source")
    deploy.add_argument("--jobs", type=int, help="make -j for --mode source")
    deploy.add_argument("--clean", action="store_true",
                        help="wipe any previous deployment on these hosts first")
    deploy.add_argument("--skip-verify", action="store_true",
                        help="skip the replication verification step")
    deploy.add_argument("--dry-run", action="store_true",
                        help="print the planned topology and exit")
    deploy.add_argument("--json", action="store_true",
                        help="with --dry-run, emit the plan as JSON")
    deploy.set_defaults(func=cmd_deploy)

    # --- plan ---------------------------------------------------------
    plan_parser = subparsers.add_parser(
        "plan", help="show the topology a deployment would create"
    )
    add_common(plan_parser)
    plan_parser.add_argument("--mode", choices=("packages", "source"), default="packages")
    plan_parser.add_argument("--nodes", type=int, default=2)
    plan_parser.add_argument("--standby", action="append")
    plan_parser.add_argument("--pg-major", default="17")
    plan_parser.add_argument("--pg-version", default="")
    plan_parser.add_argument("--spock-major", default="50", choices=("50", "60"))
    plan_parser.add_argument("--channel", default="release",
                             choices=("release", "staging", "daily"))
    plan_parser.add_argument("--db-name", default="postgres")
    plan_parser.add_argument("--db-user", default="postgres")
    plan_parser.add_argument("--base-port", type=int, default=5432)
    plan_parser.add_argument("--base-restapi-port", type=int, default=8008)
    plan_parser.add_argument("--data-root", default=topology.DEFAULT_DATA_ROOT)
    plan_parser.add_argument("--hba-cidr", action="append")
    plan_parser.add_argument("--json", action="store_true")
    plan_parser.set_defaults(func=_cmd_plan)

    # --- status -------------------------------------------------------
    status = subparsers.add_parser("status", help="health of a deployed cluster")
    add_common(status)
    status.add_argument("--json", action="store_true", help="emit the raw snapshot")
    status.add_argument("--report", action="store_true",
                        help="also write an HTML health report")
    status.add_argument("--repair-failover", metavar="NODE",
                        help="retarget peers after Patroni promoted NODE's standby")
    status.set_defaults(func=cmd_status)

    # --- add-standby --------------------------------------------------
    standby = subparsers.add_parser(
        "add-standby", help="add a Patroni standby to an existing Spock node"
    )
    add_common(standby)
    standby.add_argument("--leader", required=True,
                         help="Spock node the standby follows (e.g. n1)")
    standby.add_argument("--host", help="host to place it on (default: a host "
                                        "other than the leader's)")
    standby.set_defaults(func=cmd_add_standby)

    # --- remove -------------------------------------------------------
    remove = subparsers.add_parser("remove", help="tear a cluster down")
    add_common(remove)
    remove.add_argument("--purge", action="store_true",
                        help="also uninstall packages and the pgEdge repository")
    remove.add_argument("--keep-state", action="store_true",
                        help="keep the saved cluster state file")
    remove.add_argument("--yes", action="store_true", help="skip the confirmation")
    remove.set_defaults(func=cmd_remove)

    # --- list ---------------------------------------------------------
    listing = subparsers.add_parser("list", help="list deployed clusters")
    listing.set_defaults(func=cmd_list)

    # --- day-two operations groups ------------------------------------
    # node / spock / db / service / package / app / diff
    ops_cli.register(subparsers, add_common)

    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except inventory.InventoryError as exc:
        print(f"Inventory problem:\n{exc}", file=sys.stderr)
        return EXIT_USAGE
    except topology.TopologyError as exc:
        print(f"Cannot build that topology: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_FAIL
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return EXIT_FAIL


if __name__ == "__main__":
    sys.exit(main())
