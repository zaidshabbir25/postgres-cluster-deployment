#!/usr/bin/env python3
"""Operator-facing Spock operations: nodes, replication sets, subscriptions.

This is the day-two surface — the pgEdge CLI's `spock` module, reimplemented
against native-package clusters. It is kept separate from spock_management.py
on purpose:

    spock_management.py   deployment-time: extensions, zodan, cross-wiring,
                          post-failover repair, replication verification
    spock_operations.py   operations on a running cluster: repsets, subs,
                          sequences, DDL replication, lag

Every function takes an executor plus the node to act on, returns structured
data (never prints), and never raises for an expected condition — a missing
subscription is a result, not an exception. The CLI layer decides how to render.
"""

import time

from aspects import pg_server_management as pg

# spock's own catalog names, kept in one place because they differ between
# spock50 and spock60 in ways worth localising.
NODE_TABLE = "spock.node"
SUB_TABLE = "spock.subscription"
REPSET_TABLE = "spock.replication_set"


def _lit(value):
    """Quote a Python value as a SQL string literal."""
    return "'" + str(value).replace("'", "''") + "'"


def _bool(value):
    return "true" if value else "false"


def _call(executor, plan, node, sql, check=False, timeout=300):
    """Run one statement on a node. Returns (ok, output)."""
    code, output = pg.psql(
        executor, node.bin_dir, node.pg_port, plan.db_user, sql,
        dbname=plan.db_name, node=node.name, check=check, timeout=timeout,
    )
    return code == 0, output.strip()


# ---------------------------------------------------------------------------
# Nodes
# ---------------------------------------------------------------------------


def node_list(executor, plan, node):
    """Rows of spock.node as this node sees them."""
    return pg.dict_rows(
        executor, node.bin_dir, node.pg_port, plan.db_user,
        f"SELECT node_id, node_name, location, country "
        f"FROM {NODE_TABLE} ORDER BY node_name;",
        ["node_id", "node_name", "location", "country"],
        dbname=plan.db_name, node=node.name,
    )


def node_create(executor, plan, node, node_name, dsn):
    """Register a Spock node identity."""
    return _call(
        executor, plan, node,
        f"SELECT spock.node_create(node_name := {_lit(node_name)}, "
        f"dsn := {_lit(dsn)});",
    )


def node_drop(executor, plan, node, node_name):
    """Remove a Spock node identity.

    spock.node_drop takes only node_name, so dropping a node that is not
    registered raises. Callers that may be re-running treat a "does not exist"
    error as success rather than passing a tolerance flag spock does not have.
    """
    return _call(
        executor, plan, node,
        f"SELECT spock.node_drop(node_name := {_lit(node_name)});",
    )


def node_add_interface(executor, plan, node, node_name, interface_name, dsn):
    """Add an alternate connection interface for a node.

    This is how a node is reached at a new address without changing its
    identity — the mechanism behind post-failover retargeting.
    """
    return _call(
        executor, plan, node,
        f"SELECT spock.node_add_interface({_lit(node_name)}, "
        f"{_lit(interface_name)}, {_lit(dsn)});",
    )


def node_drop_interface(executor, plan, node, node_name, interface_name):
    return _call(
        executor, plan, node,
        f"SELECT spock.node_drop_interface({_lit(node_name)}, "
        f"{_lit(interface_name)});",
    )


# ---------------------------------------------------------------------------
# Replication sets
# ---------------------------------------------------------------------------


def repset_list(executor, plan, node):
    return pg.dict_rows(
        executor, node.bin_dir, node.pg_port, plan.db_user,
        f"SELECT set_name, replicate_insert::text, replicate_update::text, "
        f"replicate_delete::text, replicate_truncate::text "
        f"FROM {REPSET_TABLE} ORDER BY set_name;",
        ["set_name", "insert", "update", "delete", "truncate"],
        dbname=plan.db_name, node=node.name,
    )


def repset_create(executor, plan, node, set_name, insert=True, update=True,
                  delete=True, truncate=True):
    return _call(
        executor, plan, node,
        f"SELECT spock.repset_create(set_name := {_lit(set_name)}, "
        f"replicate_insert := {_bool(insert)}, "
        f"replicate_update := {_bool(update)}, "
        f"replicate_delete := {_bool(delete)}, "
        f"replicate_truncate := {_bool(truncate)});",
    )


def repset_alter(executor, plan, node, set_name, insert=None, update=None,
                 delete=None, truncate=None):
    """Change which DML types a replication set carries.

    Only the flags actually given are altered, so a caller can turn off deletes
    without restating the rest.
    """
    parts = [f"set_name := {_lit(set_name)}"]
    for name, value in (("replicate_insert", insert), ("replicate_update", update),
                        ("replicate_delete", delete), ("replicate_truncate", truncate)):
        if value is not None:
            parts.append(f"{name} := {_bool(value)}")
    if len(parts) == 1:
        return False, "nothing to alter: pass at least one replicate_* flag"
    return _call(executor, plan, node,
                 f"SELECT spock.repset_alter({', '.join(parts)});")


def repset_drop(executor, plan, node, set_name):
    return _call(executor, plan, node,
                 f"SELECT spock.repset_drop({_lit(set_name)});")


def repset_add_table(executor, plan, node, set_name, table, sync_data=True,
                     columns=None, row_filter=None):
    """Add a table (or a pattern such as 'public.*') to a replication set."""
    parts = [f"set_name := {_lit(set_name)}", f"relation := {_lit(table)}",
             f"synchronize_data := {_bool(sync_data)}"]
    if columns:
        rendered = ", ".join(_lit(c) for c in columns)
        parts.append(f"columns := ARRAY[{rendered}]")
    if row_filter:
        parts.append(f"row_filter := {_lit(row_filter)}")
    return _call(executor, plan, node,
                 f"SELECT spock.repset_add_table({', '.join(parts)});",
                 timeout=1800)


def repset_remove_table(executor, plan, node, set_name, table):
    return _call(
        executor, plan, node,
        f"SELECT spock.repset_remove_table({_lit(set_name)}, {_lit(table)});",
    )


def repset_add_all_tables(executor, plan, node, set_name="default",
                          schemas=("public",), sync_data=True):
    """Add every table in the given schemas to a replication set.

    This is `cluster replication-begin`. Spock replicates UPDATE and DELETE only
    for tables with a primary key, so tables without one are reported back
    rather than silently half-replicating.
    """
    rendered = ", ".join(_lit(s) for s in schemas)
    ok, output = _call(
        executor, plan, node,
        f"SELECT spock.repset_add_all_tables({_lit(set_name)}, "
        f"ARRAY[{rendered}], {_bool(sync_data)});",
        timeout=3600,
    )
    return ok, output, tables_without_primary_key(executor, plan, node, schemas)


def repset_list_tables(executor, plan, node, schema="public"):
    """Which tables are in which replication set."""
    return pg.dict_rows(
        executor, node.bin_dir, node.pg_port, plan.db_user,
        f"SELECT set_name, nspname, relname "
        f"FROM spock.tables WHERE nspname LIKE {_lit(schema)} "
        f"ORDER BY set_name, nspname, relname;",
        ["set_name", "schema", "table"],
        dbname=plan.db_name, node=node.name,
    )


def repset_add_partition(executor, plan, node, parent_table, partition=None,
                         row_filter=None):
    parts = [f"parent := {_lit(parent_table)}::regclass"]
    if partition:
        parts.append(f"partition := {_lit(partition)}::regclass")
    if row_filter:
        parts.append(f"row_filter := {_lit(row_filter)}")
    return _call(executor, plan, node,
                 f"SELECT spock.repset_add_partition({', '.join(parts)});")


def repset_remove_partition(executor, plan, node, parent_table, partition=None):
    parts = [f"parent := {_lit(parent_table)}::regclass"]
    if partition:
        parts.append(f"partition := {_lit(partition)}::regclass")
    return _call(executor, plan, node,
                 f"SELECT spock.repset_remove_partition({', '.join(parts)});")


def tables_without_primary_key(executor, plan, node, schemas=("public",)):
    """Tables Spock can only replicate INSERTs for.

    A table with no primary key (and no REPLICA IDENTITY FULL) gives Spock no
    way to identify the row an UPDATE or DELETE refers to, so those statements
    are dropped. This is the single most common cause of a cluster that looks
    healthy but silently diverges.
    """
    rendered = ", ".join(_lit(s) for s in schemas)
    result = pg.dict_rows(
        executor, node.bin_dir, node.pg_port, plan.db_user,
        f"SELECT n.nspname, c.relname, c.relreplident "
        f"FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        f"WHERE c.relkind IN ('r','p') AND n.nspname IN ({rendered}) "
        f"AND NOT EXISTS (SELECT 1 FROM pg_index i "
        f"                WHERE i.indrelid = c.oid AND i.indisprimary) "
        f"ORDER BY 1, 2;",
        ["schema", "table", "replica_identity"],
        dbname=plan.db_name, node=node.name,
    )
    if result is None:
        return []
    # REPLICA IDENTITY FULL ('f') is an acceptable substitute for a PK.
    return [r for r in result if r["replica_identity"] != "f"]


# ---------------------------------------------------------------------------
# Subscriptions
# ---------------------------------------------------------------------------


def sub_list(executor, plan, node):
    return pg.dict_rows(
        executor, node.bin_dir, node.pg_port, plan.db_user,
        f"SELECT sub_name, sub_enabled::text, sub_slot_name "
        f"FROM {SUB_TABLE} ORDER BY sub_name;",
        ["sub_name", "enabled", "slot_name"],
        dbname=plan.db_name, node=node.name,
    )


def sub_show_status(executor, plan, node, subscription="*"):
    """Authoritative subscription state, from spock.sub_show_status().

    spock.subscription records intent; only sub_show_status() reports whether
    the apply worker is actually connected and replicating.
    """
    arg = "" if subscription in (None, "*", "") else _lit(subscription)
    return pg.dict_rows(
        executor, node.bin_dir, node.pg_port, plan.db_user,
        f"SELECT subscription_name, status, provider_node, "
        f"coalesce(replication_sets::text, ''), coalesce(slot_name, '') "
        f"FROM spock.sub_show_status({arg}) ORDER BY subscription_name;",
        ["subscription_name", "status", "provider_node", "replication_sets",
         "slot_name"],
        dbname=plan.db_name, node=node.name,
    )


def sub_create(executor, plan, node, sub_name, provider_dsn,
               replication_sets=("default", "default_insert_only", "ddl_sql"),
               sync_structure=False, sync_data=False, forward_origins=(),
               apply_delay=None, enabled=True):
    parts = [
        f"subscription_name := {_lit(sub_name)}",
        f"provider_dsn := {_lit(provider_dsn)}",
        f"replication_sets := ARRAY[{', '.join(_lit(s) for s in replication_sets)}]",
        f"synchronize_structure := {_bool(sync_structure)}",
        f"synchronize_data := {_bool(sync_data)}",
        f"enabled := {_bool(enabled)}",
    ]
    if forward_origins:
        parts.append(
            f"forward_origins := ARRAY[{', '.join(_lit(o) for o in forward_origins)}]"
        )
    if apply_delay:
        parts.append(f"apply_delay := {_lit(apply_delay)}::interval")
    return _call(executor, plan, node,
                 f"SELECT spock.sub_create({', '.join(parts)});", timeout=1800)


def sub_drop(executor, plan, node, sub_name):
    return _call(executor, plan, node,
                 f"SELECT spock.sub_drop({_lit(sub_name)});")


def sub_enable(executor, plan, node, sub_name, immediate=True):
    """Enable a subscription.

    immediate=True restarts the apply worker now; without it the change waits
    for the next worker cycle and the subscription reports 'down' meanwhile,
    which reads as a failure when it is only a delay.
    """
    return _call(executor, plan, node,
                 f"SELECT spock.sub_enable({_lit(sub_name)}, {_bool(immediate)});")


def sub_disable(executor, plan, node, sub_name, immediate=True):
    return _call(executor, plan, node,
                 f"SELECT spock.sub_disable({_lit(sub_name)}, {_bool(immediate)});")


def sub_alter_interface(executor, plan, node, sub_name, interface_name):
    return _call(
        executor, plan, node,
        f"SELECT spock.sub_alter_interface({_lit(sub_name)}, {_lit(interface_name)});",
    )


def sub_add_repset(executor, plan, node, sub_name, replication_set):
    return _call(
        executor, plan, node,
        f"SELECT spock.sub_add_repset({_lit(sub_name)}, {_lit(replication_set)});",
    )


def sub_remove_repset(executor, plan, node, sub_name, replication_set):
    return _call(
        executor, plan, node,
        f"SELECT spock.sub_remove_repset({_lit(sub_name)}, {_lit(replication_set)});",
    )


def sub_show_table(executor, plan, node, sub_name, relation):
    return pg.dict_rows(
        executor, node.bin_dir, node.pg_port, plan.db_user,
        f"SELECT nspname, relname, status "
        f"FROM spock.sub_show_table({_lit(sub_name)}, {_lit(relation)}::regclass);",
        ["schema", "table", "status"],
        dbname=plan.db_name, node=node.name,
    )


def sub_resync_table(executor, plan, node, sub_name, relation, truncate=False):
    """Re-copy one table from the provider.

    This truncates and refills the local copy when truncate=True — the standard
    way to recover a single diverged table without rebuilding the node.
    """
    return _call(
        executor, plan, node,
        f"SELECT spock.sub_resync_table({_lit(sub_name)}, "
        f"{_lit(relation)}::regclass, {_bool(truncate)});",
        timeout=3600,
    )


def sub_wait_for_sync(executor, plan, node, sub_name, timeout=1800):
    """Block until a subscription has finished its initial sync."""
    return _call(executor, plan, node,
                 f"SELECT spock.sub_wait_for_sync({_lit(sub_name)});",
                 timeout=timeout)


def table_wait_for_sync(executor, plan, node, sub_name, relation, timeout=3600):
    return _call(
        executor, plan, node,
        f"SELECT spock.table_wait_for_sync({_lit(sub_name)}, "
        f"{_lit(relation)}::regclass);",
        timeout=timeout,
    )


def wait_for_status(executor, plan, node, sub_name, wanted="replicating",
                    timeout=180, interval=5):
    """Poll sub_show_status() until a subscription reaches a state.

    Returns (ok, last_status). Used after any operation that restarts an apply
    worker, so the caller can report the real outcome instead of assuming one.
    """
    attempts = max(1, timeout // interval)
    last = None
    for _ in range(attempts):
        statuses = sub_show_status(executor, plan, node, sub_name) or []
        for entry in statuses:
            if entry["subscription_name"] != sub_name:
                continue
            last = entry["status"]
            if (last or "").lower() == wanted.lower():
                return True, last
        time.sleep(interval)
    return False, last


# ---------------------------------------------------------------------------
# DDL and sequences
# ---------------------------------------------------------------------------


def replicate_ddl(executor, plan, node, sql_command,
                  replication_sets=("ddl_sql",)):
    """Push a DDL statement through replication sets explicitly.

    Needed when automatic DDL replication is off, or for statements that cannot
    run inside the implicit transaction auto-DDL uses.
    """
    rendered = ", ".join(_lit(s) for s in replication_sets)
    return _call(
        executor, plan, node,
        f"SELECT spock.replicate_ddl({_lit(sql_command)}, ARRAY[{rendered}]);",
        timeout=1800,
    )


def sequence_convert(executor, plan, node, sequence="public.*"):
    """Convert PostgreSQL sequences to Snowflake sequences.

    Plain sequences hand out the same values on every node, so a multi-master
    cluster generates colliding keys. Snowflake sequences embed a node id and
    cannot collide. Requires the snowflake extension.
    """
    ok, _ = _call(
        executor, plan, node,
        "CREATE EXTENSION IF NOT EXISTS snowflake;",
    )
    if not ok:
        return False, (
            "the snowflake extension is not available — install "
            f"pgedge-snowflake_{plan.pg_major} (RHEL) or "
            f"pgedge-postgresql-{plan.pg_major}-snowflake (Debian) first"
        )
    return _call(
        executor, plan, node,
        f"SELECT snowflake.convert_sequence_to_snowflake({_lit(sequence)});",
        timeout=600,
    )


def list_sequences(executor, plan, node, schema="public"):
    """Sequences and whether each is Snowflake-backed."""
    return pg.dict_rows(
        executor, node.bin_dir, node.pg_port, plan.db_user,
        f"SELECT n.nspname, c.relname, "
        f"  (EXISTS (SELECT 1 FROM pg_extension WHERE extname='snowflake'))::text "
        f"FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        f"WHERE c.relkind = 'S' AND n.nspname = {_lit(schema)} "
        f"ORDER BY 1, 2;",
        ["schema", "sequence", "snowflake_available"],
        dbname=plan.db_name, node=node.name,
    )


# ---------------------------------------------------------------------------
# Lag
# ---------------------------------------------------------------------------


def replication_lag(executor, plan, node):
    """Per-slot replication lag, in bytes and pretty form."""
    return pg.dict_rows(
        executor, node.bin_dir, node.pg_port, plan.db_user,
        "SELECT slot_name, slot_type, active::text, "
        "  coalesce(pg_wal_lsn_diff(pg_current_wal_lsn(), "
        "           restart_lsn)::text, '0'), "
        "  coalesce(pg_size_pretty(pg_wal_lsn_diff(pg_current_wal_lsn(), "
        "           restart_lsn)), '0 bytes') "
        "FROM pg_replication_slots ORDER BY slot_name;",
        ["slot_name", "slot_type", "active", "lag_bytes", "lag_pretty"],
        dbname=plan.db_name, node=node.name,
    )


def wait_for_lag_below(executor, plan, node, max_bytes=1024, timeout=600,
                       interval=5):
    """Block until every active slot on a node is caught up.

    Used before operations that must not race replication — removing a node,
    or comparing tables across the cluster.
    """
    attempts = max(1, timeout // interval)
    worst = None
    for _ in range(attempts):
        slots = replication_lag(executor, plan, node) or []
        active = [s for s in slots if s["active"] == "t"]
        if not active:
            return True, 0
        try:
            worst = max(int(s["lag_bytes"] or 0) for s in active)
        except ValueError:
            worst = None
        if worst is not None and worst <= max_bytes:
            return True, worst
        time.sleep(interval)
    return False, worst
