#!/usr/bin/env python3
"""Database-level operations: databases, GUCs, read-only mode, IO testing.

The pgEdge CLI's `db` module. One difference worth stating: on a Patroni-managed
cluster, postgresql.conf is rendered by Patroni from the DCS, so a GUC written
straight into the file is silently reverted on the next reload. Anything that
should persist has to go through `patronictl edit-config`, which is what
guc_set does by default.
"""

import json
import shlex

from aspects import pg_server_management as pg
from aspects import patroni_management

# GUCs that cannot be changed without a restart, so the caller can be told
# rather than left wondering why nothing happened.
RESTART_ONLY = {
    "shared_preload_libraries", "max_connections", "max_worker_processes",
    "max_replication_slots", "max_wal_senders", "wal_level",
    "track_commit_timestamp", "shared_buffers", "output_plugin_libraries",
    "wal_log_hints", "port",
}


def _lit(value):
    return "'" + str(value).replace("'", "''") + "'"


# ---------------------------------------------------------------------------
# Databases
# ---------------------------------------------------------------------------


def list_databases(executor, plan, node):
    return pg.dict_rows(
        executor, node.bin_dir, node.pg_port, plan.db_user,
        "SELECT d.datname, pg_get_userbyid(d.datdba), "
        "  pg_encoding_to_char(d.encoding), "
        "  pg_size_pretty(pg_database_size(d.datname)) "
        "FROM pg_database d WHERE NOT d.datistemplate ORDER BY d.datname;",
        ["database", "owner", "encoding", "size"],
        dbname=plan.db_name, node=node.name,
    )


def create_database(executor, plan, node, db_name, owner=None, password=None,
                    extensions=("spock", "dblink")):
    """Create a database and the extensions a Spock member needs.

    CREATE DATABASE cannot run inside a transaction block, and CREATE ROLE is
    cluster-wide, so both are issued as separate statements against the
    maintenance database.
    """
    owner = owner or plan.db_user
    results = []

    if owner != plan.db_user:
        exists = pg.scalar(
            executor, node.bin_dir, node.pg_port, plan.db_user,
            f"SELECT 1 FROM pg_roles WHERE rolname = {_lit(owner)};",
            dbname=plan.db_name, node=node.name,
        )
        if not exists:
            clause = f"LOGIN PASSWORD {_lit(password)}" if password else "LOGIN"
            code, output = pg.psql(
                executor, node.bin_dir, node.pg_port, plan.db_user,
                f"CREATE ROLE {owner} {clause};",
                dbname=plan.db_name, node=node.name, check=False,
            )
            results.append(("create role", code == 0, output.strip()))

    code, output = pg.psql(
        executor, node.bin_dir, node.pg_port, plan.db_user,
        f'CREATE DATABASE "{db_name}" OWNER {owner};',
        dbname=plan.db_name, node=node.name, check=False,
    )
    results.append(("create database", code == 0, output.strip()))
    if code != 0 and "already exists" not in output:
        return False, results

    for extension in extensions:
        code, output = pg.psql(
            executor, node.bin_dir, node.pg_port, plan.db_user,
            f"CREATE EXTENSION IF NOT EXISTS {extension};",
            dbname=db_name, node=node.name, check=False,
        )
        results.append((f"extension {extension}", code == 0, output.strip()))

    return all(ok for _, ok, _ in results), results


def drop_database(executor, plan, node, db_name, force=False):
    """Drop a database, optionally evicting existing connections."""
    clause = " WITH (FORCE)" if force else ""
    code, output = pg.psql(
        executor, node.bin_dir, node.pg_port, plan.db_user,
        f'DROP DATABASE IF EXISTS "{db_name}"{clause};',
        dbname=plan.db_name, node=node.name, check=False,
    )
    return code == 0, output.strip()


def database_stats(executor, plan, node, db_name=None):
    """Size, connection count and table count for one database."""
    target = db_name or plan.db_name
    size = pg.scalar(
        executor, node.bin_dir, node.pg_port, plan.db_user,
        f"SELECT pg_size_pretty(pg_database_size({_lit(target)}));",
        dbname=plan.db_name, node=node.name,
    )
    connections = pg.scalar(
        executor, node.bin_dir, node.pg_port, plan.db_user,
        f"SELECT count(*) FROM pg_stat_activity WHERE datname = {_lit(target)};",
        dbname=plan.db_name, node=node.name,
    )
    tables = pg.scalar(
        executor, node.bin_dir, node.pg_port, plan.db_user,
        "SELECT count(*) FROM pg_class c JOIN pg_namespace n "
        "ON n.oid=c.relnamespace WHERE c.relkind IN ('r','p') "
        "AND n.nspname NOT IN ('pg_catalog','information_schema');",
        dbname=target, node=node.name,
    )
    return {"database": target, "size": size, "connections": connections,
            "tables": tables}


# ---------------------------------------------------------------------------
# GUCs
# ---------------------------------------------------------------------------


def guc_show(executor, plan, node, pattern="%"):
    """Show GUCs matching a pattern, with their source and restart status."""
    return pg.dict_rows(
        executor, node.bin_dir, node.pg_port, plan.db_user,
        f"SELECT name, setting, coalesce(unit,''), source, context, "
        f"  pending_restart::text "
        f"FROM pg_settings WHERE name LIKE {_lit(pattern)} ORDER BY name;",
        ["name", "setting", "unit", "source", "context", "pending_restart"],
        dbname=plan.db_name, node=node.name,
    )


def server_log(executor, plan, node, lines=100, follow_file=None):
    """Read a node's PostgreSQL log from under its data directory.

    Separate from `service logs`, which shows the unit's journal: the journal
    carries Patroni's view of events, while this is the server's own account —
    checkpoints, lock waits, recovery, the statements that failed.

    Returns (path, text). An empty path means the node is not logging to a
    file, which is worth saying plainly rather than showing nothing.
    """
    directory = pg.log_directory(node)
    ok, listing = executor.try_run(
        f"ls -1t {shlex.quote(directory)}/*.log 2>/dev/null | head -n 20",
        node=node.name,
    )
    files = [line.strip() for line in (listing or "").splitlines() if line.strip()]
    if not ok or not files:
        return "", (
            f"no log files under {directory}. The server logs to its stderr "
            f"unless logging_collector is on — turn it on with "
            f"`db logging-on --node {node.name}`."
        )

    path = follow_file or files[0]
    _, text = executor.try_run(
        f"tail -n {int(lines)} {shlex.quote(path)}", node=node.name
    )
    return path, text


def enable_server_log(executor, plan, node):
    """Make a running scope keep its server log under <data_dir>/log."""
    return patroni_management.apply_logging_settings(
        executor, plan, node, node_name=node.name
    )


def guc_set(executor, plan, node, guc_name, guc_value, through_patroni=True):
    """Set a GUC.

    Through Patroni by default: it owns postgresql.conf for the clusters it
    bootstraps, so an ALTER SYSTEM setting is overwritten at the next reload.
    Going through the DCS also applies the value to every member of the scope,
    which is almost always what was intended.

    Returns (ok, message, needs_restart).
    """
    needs_restart = guc_name.lower() in RESTART_ONLY

    if through_patroni:
        binary = patroni_management.patronictl_binary(executor, node=node.name)
        ok, output = executor.try_run(
            f"{binary} -c {node.config_file} edit-config {node.scope} --force "
            f"-s postgresql.parameters.{shlex.quote(guc_name)}="
            f"{shlex.quote(str(guc_value))}",
            node=node.name,
        )
        if ok:
            message = (
                f"{guc_name}={guc_value} written to the DCS for scope "
                f"{node.scope}; every member of that scope picks it up"
            )
            if needs_restart:
                message += ". A restart is required: patronictl restart " \
                           f"{node.scope}"
            return True, message, needs_restart
        return False, f"patronictl edit-config failed: {output.strip()}", needs_restart

    code, output = pg.psql(
        executor, node.bin_dir, node.pg_port, plan.db_user,
        f"ALTER SYSTEM SET {guc_name} = {_lit(guc_value)};",
        dbname=plan.db_name, node=node.name, check=False,
    )
    if code != 0:
        return False, output.strip(), needs_restart
    pg.psql(
        executor, node.bin_dir, node.pg_port, plan.db_user,
        "SELECT pg_reload_conf();", dbname=plan.db_name, node=node.name,
        check=False,
    )
    return True, (
        f"{guc_name}={guc_value} set via ALTER SYSTEM on {node.name} only. "
        f"Patroni may revert this at its next reload — prefer the default "
        f"(--through-patroni) for anything that must persist."
    ), needs_restart


def guc_reset(executor, plan, node, guc_name):
    """Remove a GUC override from the DCS, restoring the inherited value."""
    binary = patroni_management.patronictl_binary(executor, node=node.name)
    ok, output = executor.try_run(
        f"{binary} -c {node.config_file} edit-config {node.scope} --force "
        f"-s postgresql.parameters.{shlex.quote(guc_name)}=null",
        node=node.name,
    )
    return ok, output.strip()


# ---------------------------------------------------------------------------
# Read-only mode
# ---------------------------------------------------------------------------


def set_readonly(executor, plan, node, readonly=True, through_patroni=True):
    """Turn default_transaction_read_only on or off.

    Read-only mode is how a node is drained before maintenance without
    detaching it from replication: it keeps applying incoming changes (the apply
    worker is not subject to this GUC) while refusing new local writes.
    """
    value = "on" if readonly else "off"
    ok, message, _ = guc_set(
        executor, plan, node, "default_transaction_read_only", value,
        through_patroni=through_patroni,
    )
    return ok, message


def is_readonly(executor, plan, node):
    value = pg.scalar(
        executor, node.bin_dir, node.pg_port, plan.db_user,
        "SHOW default_transaction_read_only;", dbname=plan.db_name,
        node=node.name,
    )
    return value == "on"


# ---------------------------------------------------------------------------
# IO test
# ---------------------------------------------------------------------------


def test_io(executor, plan, node, size="1G", runtime=10, block_size="8k"):
    """Run fio against the node's data directory.

    Tests the disk the WAL and heap actually live on, which is the number that
    matters for commit latency — testing /tmp can measure a different device
    entirely.
    """
    from aspects import platform_detect, package_management

    if not executor.which("fio"):
        family = node.family or "rhel"
        installed, failures = package_management.install(
            executor, family, ["fio"], node=node.name, allow_missing=True
        )
        if failures or not executor.which("fio"):
            hint = (
                "dnf --enablerepo=devel install fio"
                if family == "rhel" else "apt-get install fio"
            )
            return False, {"error": f"fio is not installed. Install it with: {hint}"}

    target = f"{node.data_dir}/fio_test"
    executor.run(f"mkdir -p {shlex.quote(target)}", node=node.name)
    executor.run(f"chown {plan.db_user}:{plan.db_user} {shlex.quote(target)}",
                 node=node.name)

    command = (
        f"fio --name=pgcluster_io --directory={shlex.quote(target)} "
        f"--rw=write --bs={block_size} --fsync=1 --size={size} "
        f"--runtime={runtime}s --time_based --output-format=json"
    )
    ok, output = executor.try_run(command, user=plan.db_user, node=node.name,
                                  timeout=runtime + 300)
    executor.try_run(f"rm -rf {shlex.quote(target)}", node=node.name)

    if not ok:
        return False, {"error": output.strip()[:2000]}

    try:
        # fio can emit warnings before the JSON document.
        start = output.index("{")
        parsed = json.loads(output[start:])
    except (ValueError, json.JSONDecodeError) as exc:
        return False, {"error": f"could not parse fio output: {exc}"}

    jobs = parsed.get("jobs") or []
    if not jobs:
        return False, {"error": "fio reported no jobs"}

    write = jobs[0].get("write", {})
    return True, {
        "data_dir": node.data_dir,
        "block_size": block_size,
        "iops": round(write.get("iops", 0), 1),
        "bandwidth_kb": round(write.get("bw", 0), 1),
        "latency_us_mean": round(write.get("lat_ns", {}).get("mean", 0) / 1000, 1),
        "latency_us_p99": round(
            write.get("clat_ns", {}).get("percentile", {}).get("99.000000", 0) / 1000, 1
        ),
        "fsync_latency_us_mean": round(
            jobs[0].get("sync", {}).get("lat_ns", {}).get("mean", 0) / 1000, 1
        ),
    }


# ---------------------------------------------------------------------------
# Table inventory
# ---------------------------------------------------------------------------


def list_tables(executor, plan, node, schema="public", dbname=None):
    """Tables with row estimates and sizes."""
    return pg.dict_rows(
        executor, node.bin_dir, node.pg_port, plan.db_user,
        f"SELECT n.nspname, c.relname, "
        f"  c.reltuples::bigint::text, "
        f"  pg_size_pretty(pg_total_relation_size(c.oid)), "
        f"  (EXISTS (SELECT 1 FROM pg_index i WHERE i.indrelid=c.oid "
        f"           AND i.indisprimary))::text "
        f"FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
        f"WHERE c.relkind IN ('r','p') AND n.nspname = {_lit(schema)} "
        f"ORDER BY 1, 2;",
        ["schema", "table", "row_estimate", "size", "has_primary_key"],
        dbname=dbname or plan.db_name, node=node.name,
    )
