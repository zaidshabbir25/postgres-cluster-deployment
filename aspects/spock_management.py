#!/usr/bin/env python3
"""Spock multi-master setup, cross-wiring and replication inspection.

Cross-wiring goes through zodan's `spock.add_node` procedure rather than raw
node_create/sub_create calls. add_node is cluster-aware: it discovers every
node already registered with the source and wires the newcomer to all of them
in both directions, with the sync-event handshakes that make the join safe on a
live cluster. Adding node N is therefore always one call against node 1:

    n1                             (first node — nothing to wire)
    add_node(n1 -> n2)             n1 <-> n2
    add_node(n1 -> n3)             n1 <-> n3, n2 <-> n3
    add_node(n1 -> n4)             ... and so on

The call runs *on the joining node*, because that is where zodan's procedures
are loaded and where the final sync is awaited.
"""

import re
import shlex
from pathlib import Path

from aspects import pg_server_management

REPO_ROOT = Path(__file__).resolve().parent.parent
ZODAN_DIR = REPO_ROOT / "configuration" / "spock"

# Which zodan revision matches which Spock major. spock60 renamed catalog
# columns (remote_lsn -> remote_commit_lsn), so a spock50 script fails on it.
ZODAN_BY_SPOCK_MAJOR = {
    "50": "zodan-511.sql",
    "60": "zodan-600.sql",
}

REMOTE_ZODAN_DIR = "/tmp/pgcluster"

# zodan lives in the Spock repository, and its procedures track the Spock API
# they call — so the copy that matches a node is the one on the branch that
# node's Spock was built from: v5_STABLE for spock50, main for spock60.
ZODAN_REPO_PATH = "samples/Z0DAN/zodan.sql"
ZODAN_RAW_URL = ("https://raw.githubusercontent.com/pgEdge/spock/"
                 "{branch}/" + ZODAN_REPO_PATH)

# Extensions every Spock node needs. dblink is not optional: zodan's procedures
# reach across nodes through it.
REQUIRED_EXTENSIONS = ("spock", "dblink")

DDL_REPLICATION_GUCS = (
    "spock.enable_ddl_replication",
    "spock.include_ddl_repset",
    "spock.allow_ddl_from_functions",
)


def zodan_script(spock_major, override=None):
    """Resolve the local zodan SQL file for a Spock major version."""
    name = override or ZODAN_BY_SPOCK_MAJOR.get(str(spock_major))
    if not name:
        raise ValueError(
            f"No zodan script known for spock{spock_major}. "
            f"Known majors: {', '.join(sorted(ZODAN_BY_SPOCK_MAJOR))}"
        )
    path = ZODAN_DIR / name
    if not path.exists():
        available = ", ".join(sorted(p.name for p in ZODAN_DIR.glob("zodan-*.sql")))
        raise FileNotFoundError(
            f"zodan script {name} not found in {ZODAN_DIR}. Available: {available}"
        )
    return path


def sub_name(provider, subscriber):
    """zodan's subscription naming convention (spock.gen_sub_name)."""
    return f"sub_{provider}_{subscriber}"


# ---------------------------------------------------------------------------
# Node preparation
# ---------------------------------------------------------------------------


def create_extensions(executor, plan, node, run_logger=None):
    """Create the extensions Spock cross-wiring depends on."""
    created = []
    for extension in REQUIRED_EXTENSIONS:
        pg_server_management.psql(
            executor, node.bin_dir, node.pg_port, plan.db_user,
            f"CREATE EXTENSION IF NOT EXISTS {extension};",
            dbname=plan.db_name, node=node.name,
        )
        created.append(extension)
    if run_logger:
        run_logger.node(node.name, f"extensions ready: {', '.join(created)}")
    return created


def spock_version(executor, plan, node):
    """Installed Spock extension version, or None."""
    return pg_server_management.scalar(
        executor, node.bin_dir, node.pg_port, plan.db_user,
        "SELECT extversion FROM pg_extension WHERE extname='spock';",
        dbname=plan.db_name, node=node.name,
    )


def _safe_name(text):
    """A branch name usable as a filename."""
    return re.sub(r"[^A-Za-z0-9._-]", "-", str(text)).strip("-") or "branch"


def stage_zodan(executor, plan, node, branch, run_logger=None):
    """Put the zodan script matching `branch` on the host.

    Three sources, in order of how closely they match the Spock actually
    installed:

      1. the Spock checkout a source build already made, when it sits on this
         branch — byte-for-byte what the extension was built from;
      2. the branch on GitHub, for a package-mode host with no checkout;
      3. the copy bundled in configuration/spock, so an air-gapped host still
         works. It tracks a Spock major rather than a branch, which is why it
         is last.

    An explicit plan.zodan_sql skips straight to the bundled directory: naming
    a script is an instruction, not a preference. Returns (remote path, origin).
    """
    from aspects import source_build  # local: source_build imports nothing here

    executor.run(f"mkdir -p {REMOTE_ZODAN_DIR}", node=node.name)
    executor.run(f"chmod 755 {REMOTE_ZODAN_DIR}", node=node.name)
    spock_major = getattr(node, "spock_major", "") or plan.spock_major

    def adopt(remote):
        executor.try_run(f"chown {plan.db_user}: {shlex.quote(remote)}",
                         node=node.name)
        executor.try_run(f"chmod 644 {shlex.quote(remote)}", node=node.name)

    if not plan.zodan_sql:
        remote = f"{REMOTE_ZODAN_DIR}/zodan-{_safe_name(branch)}.sql"
        checkout = f"{source_build.BUILD_ROOT}/spock"
        on_branch, current = executor.try_run(
            f"git -C {shlex.quote(checkout)} rev-parse --abbrev-ref HEAD "
            f"2>/dev/null", node=node.name,
        )
        if on_branch and current.strip() == str(branch):
            copied, _ = executor.try_run(
                f"cp {shlex.quote(checkout)}/{ZODAN_REPO_PATH} "
                f"{shlex.quote(remote)}", node=node.name,
            )
            if copied:
                adopt(remote)
                return remote, f"the {branch} checkout at {checkout}"

        url = ZODAN_RAW_URL.format(branch=branch)
        fetched, _ = executor.try_run(
            f"curl -fsSL --max-time 60 -o {shlex.quote(remote)} "
            f"{shlex.quote(url)} && test -s {shlex.quote(remote)}",
            node=node.name,
        )
        if fetched:
            adopt(remote)
            return remote, url

        if run_logger:
            run_logger.warn(
                f"{node.name}: could not read {ZODAN_REPO_PATH} from branch "
                f"{branch}; falling back to the copy bundled for spock"
                f"{spock_major}"
            )

    local = zodan_script(spock_major, plan.zodan_sql or None)
    remote = f"{REMOTE_ZODAN_DIR}/{local.name}"
    executor.put_file(local, remote, owner=plan.db_user, mode="644",
                      node=node.name)
    return remote, f"the bundled {local.name}"


def load_zodan(executor, plan, node, run_logger=None, branch=None):
    """Install zodan's procedures on a node.

    The script follows the Spock running on *this* node: the procedures call
    Spock's own API, which changed between 50 and 60, so a node added with a
    different Spock major needs the script from that major's branch rather
    than the cluster's.
    """
    from aspects import source_build

    spock_major = getattr(node, "spock_major", "") or plan.spock_major
    branch = branch or source_build.default_spock_branch(spock_major)
    remote, origin = stage_zodan(executor, plan, node, branch,
                                 run_logger=run_logger)

    _, output = executor.try_run(f"wc -l {shlex.quote(remote)}", node=node.name)
    pg_server_management.psql_file(
        executor, node.bin_dir, node.pg_port, plan.db_user, remote,
        dbname=plan.db_name, node=node.name,
    )
    if run_logger:
        run_logger.node(node.name,
                        f"zodan for spock{spock_major} loaded from {origin} "
                        f"({output.strip()})")
    return remote.rsplit("/", 1)[-1]


def enable_ddl_replication(executor, plan, node, run_logger=None):
    """Turn on automatic DDL replication.

    Each ALTER SYSTEM is its own psql call — the server rejects ALTER SYSTEM
    inside a multi-statement batch sent as one simple query.
    """
    for guc in DDL_REPLICATION_GUCS:
        pg_server_management.psql(
            executor, node.bin_dir, node.pg_port, plan.db_user,
            f"ALTER SYSTEM SET {guc} = on;",
            dbname=plan.db_name, node=node.name,
        )
    pg_server_management.psql(
        executor, node.bin_dir, node.pg_port, plan.db_user,
        "SELECT pg_reload_conf();", dbname=plan.db_name, node=node.name,
    )
    if run_logger:
        run_logger.node(node.name, "DDL replication enabled")
    return list(DDL_REPLICATION_GUCS)


# ---------------------------------------------------------------------------
# Cross-wiring
# ---------------------------------------------------------------------------


def add_node(executor, plan, source_node, new_node, run_logger=None,
             timeout=1800):
    """Cross-wire `new_node` into the cluster that `source_node` belongs to.

    Runs on new_node. Returns (ok, output) — zodan reports its own verdict as a
    'Success rate: %100' line, which is checked here because the procedure exits
    zero even when individual phases report failures.
    """
    src_dsn = source_node.dsn(plan.db_name, plan.db_user, plan.db_password)
    new_dsn = new_node.dsn(plan.db_name, plan.db_user, plan.db_password)

    call = (
        f"CALL spock.add_node("
        f"{_lit(source_node.name)}, {_lit(src_dsn)}, "
        f"{_lit(new_node.name)}, {_lit(new_dsn)}, true);"
    )

    if run_logger:
        run_logger.info(
            f"    cross-wiring {new_node.name} into the cluster via "
            f"{source_node.name}"
        )

    code, output = pg_server_management.psql(
        executor, new_node.bin_dir, new_node.pg_port, plan.db_user, call,
        dbname=plan.db_name, node=new_node.name, check=False, timeout=timeout,
    )

    if code != 0:
        return False, output

    if "Success rate: %100" not in output:
        # zodan prints a per-phase tally; anything short of 100% means at least
        # one subscription or slot did not come up.
        return False, (
            "zodan add_node did not report a 100% success rate — the node is "
            "partially wired:\n" + output
        )
    return True, output


def health_check(executor, plan, source_node, new_node=None, phase="post",
                 timeout=600):
    """Run zodan's own cluster health check (pre or post add_node)."""
    src_dsn = source_node.dsn(plan.db_name, plan.db_user, plan.db_password)
    if new_node is not None:
        new_dsn = new_node.dsn(plan.db_name, plan.db_user, plan.db_password)
        args = f"{_lit(new_node.name)}, {_lit(new_dsn)}"
    else:
        args = "NULL, NULL"

    call = (
        f"CALL spock.health_check({_lit(source_node.name)}, {_lit(src_dsn)}, "
        f"{args}, {_lit(phase)}, true);"
    )
    target = new_node or source_node
    return pg_server_management.psql(
        executor, target.bin_dir, target.pg_port, plan.db_user, call,
        dbname=plan.db_name, node=target.name, check=False, timeout=timeout,
    )


# ---------------------------------------------------------------------------
# Replication inspection
# ---------------------------------------------------------------------------


def node_table(executor, plan, node):
    """Rows of spock.node as seen from this node."""
    value = pg_server_management.scalar(
        executor, node.bin_dir, node.pg_port, plan.db_user,
        "SELECT string_agg(node_name, ',' ORDER BY node_name) FROM spock.node;",
        dbname=plan.db_name, node=node.name, default="",
    )
    return [n for n in (value or "").split(",") if n]


def subscription_status(executor, plan, node):
    """Subscriptions on this node with their replication state.

    spock.sub_show_status() is the authoritative view — spock.subscription only
    records intent, not whether the apply worker is actually connected.
    """
    code, output = pg_server_management.psql(
        executor, node.bin_dir, node.pg_port, plan.db_user,
        "SELECT subscription_name || '|' || status || '|' || "
        "coalesce(provider_node, '?') FROM spock.sub_show_status();",
        dbname=plan.db_name, node=node.name, tuples_only=True, check=False,
    )
    if code != 0:
        return []

    subscriptions = []
    for line in output.strip().splitlines():
        parts = line.strip().split("|")
        if len(parts) >= 3:
            subscriptions.append(
                {"name": parts[0], "status": parts[1], "provider": parts[2]}
            )
    return subscriptions


def replication_slots(executor, plan, node):
    """Logical and physical slots, with the WAL each is holding back."""
    code, output = pg_server_management.psql(
        executor, node.bin_dir, node.pg_port, plan.db_user,
        "SELECT slot_name || '|' || slot_type || '|' || active::text || '|' || "
        "coalesce(pg_size_pretty(pg_wal_lsn_diff("
        "pg_current_wal_lsn(), restart_lsn)), '0') "
        "FROM pg_replication_slots ORDER BY slot_name;",
        dbname=plan.db_name, node=node.name, tuples_only=True, check=False,
    )
    if code != 0:
        return []

    slots = []
    for line in output.strip().splitlines():
        parts = line.strip().split("|")
        if len(parts) >= 4:
            slots.append(
                {
                    "name": parts[0],
                    "type": parts[1],
                    "active": parts[2] == "t",
                    "retained_wal": parts[3],
                }
            )
    return slots


def streaming_peers(executor, plan, node):
    """pg_stat_replication — who is streaming from this node, and how far behind."""
    code, output = pg_server_management.psql(
        executor, node.bin_dir, node.pg_port, plan.db_user,
        "SELECT coalesce(application_name, client_addr::text, '?') || '|' || "
        "state || '|' || coalesce(sync_state, '-') || '|' || "
        "coalesce(pg_size_pretty(pg_wal_lsn_diff("
        "pg_current_wal_lsn(), replay_lsn)), '0') "
        "FROM pg_stat_replication;",
        dbname=plan.db_name, node=node.name, tuples_only=True, check=False,
    )
    if code != 0:
        return []

    peers = []
    for line in output.strip().splitlines():
        parts = line.strip().split("|")
        if len(parts) >= 4:
            peers.append(
                {
                    "peer": parts[0],
                    "state": parts[1],
                    "sync_state": parts[2],
                    "lag": parts[3],
                }
            )
    return peers


def is_in_recovery(executor, plan, node):
    value = pg_server_management.scalar(
        executor, node.bin_dir, node.pg_port, plan.db_user,
        "SELECT pg_is_in_recovery();", dbname=plan.db_name, node=node.name,
    )
    return value == "t"


def verify_replication(executor_for, plan, run_logger=None, timeout=180):
    """Confirm every Spock node sees every peer and all subscriptions replicate.

    `executor_for` is a callable taking a node and returning its executor, so
    this works whether nodes share a host or not.

    Returns (ok, findings) where findings is a per-node list of dicts.
    """
    expected = {node.name for node in plan.spock_nodes}
    findings, problems = [], []

    for node in plan.spock_nodes:
        executor = executor_for(node)

        seen = set(node_table(executor, plan, node))
        missing = sorted(expected - seen)

        subscriptions = subscription_status(executor, plan, node)
        # Each Spock node subscribes to every peer, so a node in an N-node
        # cluster should carry N-1 replicating subscriptions.
        expected_subs = len(expected) - 1
        replicating = [
            s for s in subscriptions
            if (s["status"] or "").lower() in ("replicating", "running")
        ]

        entry = {
            "node": node.name,
            "nodes_visible": sorted(seen),
            "missing_nodes": missing,
            "subscriptions": subscriptions,
            "replicating": len(replicating),
            "expected_subscriptions": expected_subs,
            "slots": replication_slots(executor, plan, node),
            "streaming": streaming_peers(executor, plan, node),
            "in_recovery": is_in_recovery(executor, plan, node),
        }
        findings.append(entry)

        if missing:
            problems.append(
                f"{node.name} cannot see node(s) {', '.join(missing)} in spock.node"
            )
        if len(replicating) < expected_subs:
            stale = [
                f"{s['name']}={s['status']}" for s in subscriptions
                if s not in replicating
            ]
            problems.append(
                f"{node.name} has {len(replicating)}/{expected_subs} subscriptions "
                f"replicating"
                + (f" ({', '.join(stale)})" if stale else "")
            )
        if entry["in_recovery"]:
            problems.append(f"{node.name} is in recovery — it cannot accept writes")

        if run_logger:
            run_logger.info(
                f"    {node.name}: sees {len(seen)}/{len(expected)} nodes, "
                f"{len(replicating)}/{expected_subs} subscriptions replicating"
            )

    return (not problems), {"nodes": findings, "problems": problems}


# ---------------------------------------------------------------------------
# Post-failover repair
# ---------------------------------------------------------------------------


def retarget_after_failover(executor_for, plan, spock_node, new_address,
                            new_port, run_logger=None):
    """Point the cluster's subscriptions at a promoted standby.

    When Patroni promotes a standby, the Spock node identity stays the same but
    it now answers on a different address and port. Every *peer* holds a DSN for
    the old location, so each peer must be moved onto a new interface for that
    node. Spock has no VIP concept — this is the manual step that stands in for
    one.
    """
    interface = f"{spock_node.name}_promoted_{new_port}"
    new_dsn = (
        f"host={new_address} port={new_port} dbname={plan.db_name} "
        f"user={plan.db_user} password={plan.db_password}"
    )
    results = []

    for peer in plan.spock_nodes:
        if peer.name == spock_node.name:
            continue
        executor = executor_for(peer)
        subscription = sub_name(spock_node.name, peer.name)

        steps = [
            (f"SELECT spock.sub_disable({_lit(subscription)}, true);",
             "disable subscription"),
            (f"SELECT spock.node_add_interface({_lit(spock_node.name)}, "
             f"{_lit(interface)}, {_lit(new_dsn)});", "add interface"),
            (f"SELECT spock.sub_alter_interface({_lit(subscription)}, "
             f"{_lit(interface)});", "switch interface"),
            # immediate := true restarts the apply worker now; without it the
            # subscription stays 'down' until the next worker cycle.
            (f"SELECT spock.sub_enable({_lit(subscription)}, true);",
             "re-enable subscription"),
        ]

        for sql, description in steps:
            code, output = pg_server_management.psql(
                executor, peer.bin_dir, peer.pg_port, plan.db_user, sql,
                dbname=plan.db_name, node=peer.name, check=False,
            )
            results.append(
                {
                    "peer": peer.name,
                    "step": description,
                    "ok": code == 0,
                    "output": output.strip(),
                }
            )
            if code != 0 and run_logger:
                run_logger.warn(f"{peer.name}: {description} failed — {output.strip()}")

    return results


def _lit(value):
    """Quote a Python value as a SQL string literal."""
    return "'" + str(value).replace("'", "''") + "'"


def parse_success_rate(output):
    """Pull zodan's success percentage out of its verbose output."""
    match = re.search(r"Success rate:\s*%?(\d+)", output or "")
    return int(match.group(1)) if match else None
