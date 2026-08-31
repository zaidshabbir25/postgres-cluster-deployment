#!/usr/bin/env python3
"""Consistency checking across a multi-master cluster.

The pgEdge CLI's `ace` module in the parts that matter operationally: find out
whether the nodes actually agree, and locate the disagreement precisely enough
to fix it.

Four comparisons, cheapest first:

    spock_diff    replication metadata — nodes, subscriptions, repsets
    repset_diff   which tables each node has in which replication set
    schema_diff   table, column, index and constraint definitions
    table_diff    the data itself

table_diff uses bucketed checksums rather than streaming whole tables to the
control machine. Each node hashes its own rows into N buckets keyed off the
primary key, and only the bucket digests cross the network — so comparing a
100M-row table costs N integers per node, not 100M rows. When buckets disagree,
only those buckets are drilled into, which is what makes locating a handful of
diverged rows in a large table practical.

A full-table hash would also work and be simpler, but it can only ever answer
"the same or not" — it cannot say which rows, which is the answer you need.
"""

from aspects import pg_server_management as pg
from aspects import spock_operations

DEFAULT_BUCKETS = 64

# Chopping the trailing 's' gives "indexe", so name the singulars explicitly.
SINGULAR = {"columns": "column", "indexes": "index",
            "constraints": "constraint"}
# Cap the rows pulled back when drilling into a mismatching bucket, so a table
# that has diverged wholesale reports a bounded sample rather than hanging.
MAX_ROWS_PER_BUCKET = 5000


def _lit(value):
    return "'" + str(value).replace("'", "''") + "'"


def _split_table(table):
    """Split 'schema.table' into its parts, defaulting the schema to public."""
    if "." in table:
        schema, _, name = table.partition(".")
        return schema, name
    return "public", table


# A portable 32-bit bucket from any text: take the first 8 hex digits of the
# md5, read them as a bit(32), and fold to a non-negative int. Avoids hashtext(),
# which is an internal function whose result is not guaranteed across versions.
def _bucket_expr(key_expr, buckets):
    return (
        f"abs(('x' || substr(md5({key_expr}), 1, 8))::bit(32)::int) % {buckets}"
    )


# ---------------------------------------------------------------------------
# spock metadata
# ---------------------------------------------------------------------------


def spock_diff(executor_for, plan):
    """Compare Spock replication metadata across every Spock node.

    Returns (ok, report). Divergence here explains most "replication looks up
    but data is not arriving" situations: a node missing from one peer's
    spock.node, or a subscription that exists on one side only.
    """
    per_node, problems = {}, []

    for node in plan.spock_nodes:
        executor = executor_for(node)
        nodes = spock_operations.node_list(executor, plan, node)
        subs = spock_operations.sub_show_status(executor, plan, node)
        repsets = spock_operations.repset_list(executor, plan, node)

        if nodes is None:
            problems.append(f"{node.name}: cannot read {spock_operations.NODE_TABLE}")
            per_node[node.name] = {"reachable": False}
            continue

        per_node[node.name] = {
            "reachable": True,
            "nodes": sorted(r["node_name"] for r in nodes),
            "subscriptions": sorted(
                (s["subscription_name"], s["status"], s["provider_node"])
                for s in (subs or [])
            ),
            "repsets": sorted(r["set_name"] for r in (repsets or [])),
        }

    reachable = {n: d for n, d in per_node.items() if d.get("reachable")}
    expected_nodes = {n.name for n in plan.spock_nodes}

    # Every node should see every node, including itself.
    for name, data in reachable.items():
        missing = sorted(expected_nodes - set(data["nodes"]))
        extra = sorted(set(data["nodes"]) - expected_nodes)
        if missing:
            problems.append(f"{name}: spock.node is missing {', '.join(missing)}")
        if extra:
            problems.append(
                f"{name}: spock.node has unexpected node(s) {', '.join(extra)} "
                f"— left over from a removed node?"
            )

    # Each node should subscribe to every peer, and every subscription should
    # be replicating.
    for name, data in reachable.items():
        providers = {provider for _, _, provider in data["subscriptions"]}
        expected_providers = expected_nodes - {name}
        missing = sorted(expected_providers - providers)
        if missing:
            problems.append(
                f"{name}: no subscription to {', '.join(missing)}"
            )
        for sub_name, status, provider in data["subscriptions"]:
            if (status or "").lower() not in ("replicating", "running"):
                problems.append(
                    f"{name}: subscription {sub_name} (from {provider}) is "
                    f"{status!r}, not replicating"
                )

    # Replication sets should be identical everywhere.
    repset_sets = {name: set(d["repsets"]) for name, d in reachable.items()}
    if len(set(map(frozenset, repset_sets.values()))) > 1:
        for name, sets in repset_sets.items():
            others = set().union(*(s for n, s in repset_sets.items() if n != name))
            diff = sorted(others - sets)
            if diff:
                problems.append(
                    f"{name}: missing replication set(s) {', '.join(diff)} "
                    f"that other nodes have"
                )

    return (not problems), {"nodes": per_node, "problems": problems}


# ---------------------------------------------------------------------------
# replication set membership
# ---------------------------------------------------------------------------


def repset_diff(executor_for, plan, repset="default", schema="public"):
    """Compare which tables each node has in a replication set.

    A table present in the repset on one node but not another replicates in one
    direction only — writes flow one way and the nodes drift apart.
    """
    membership, problems = {}, []

    for node in plan.spock_nodes:
        entries = spock_operations.repset_list_tables(
            executor_for(node), plan, node, schema=schema
        )
        if entries is None:
            problems.append(f"{node.name}: cannot read replication set membership")
            continue
        membership[node.name] = {
            f"{e['schema']}.{e['table']}" for e in entries
            if repset in ("*", None, "") or e["set_name"] == repset
        }

    if len(membership) > 1:
        union = set().union(*membership.values())
        for name, tables in membership.items():
            missing = sorted(union - tables)
            if missing:
                problems.append(
                    f"{name}: repset {repset!r} is missing "
                    f"{', '.join(missing[:10])}"
                    + (f" (+{len(missing) - 10} more)" if len(missing) > 10 else "")
                )

    return (not problems), {
        "repset": repset,
        "membership": {n: sorted(t) for n, t in membership.items()},
        "problems": problems,
    }


# ---------------------------------------------------------------------------
# schema
# ---------------------------------------------------------------------------


def _schema_fingerprint(executor, plan, node, schema):
    """One row per object, hashed — a comparable digest of a node's schema."""
    columns = pg.dict_rows(
        executor, node.bin_dir, node.pg_port, plan.db_user,
        f"SELECT c.table_name || '.' || c.column_name, "
        f"  c.data_type || ':' || coalesce(c.character_maximum_length::text, '-') "
        f"    || ':' || coalesce(c.numeric_precision::text, '-') "
        f"    || ':' || c.is_nullable "
        f"    || ':' || coalesce(c.column_default, '-') "
        f"FROM information_schema.columns c "
        f"WHERE c.table_schema = {_lit(schema)} "
        f"ORDER BY 1;",
        ["object", "definition"], dbname=plan.db_name, node=node.name,
    )
    indexes = pg.dict_rows(
        executor, node.bin_dir, node.pg_port, plan.db_user,
        f"SELECT indexname, indexdef FROM pg_indexes "
        f"WHERE schemaname = {_lit(schema)} ORDER BY 1;",
        ["object", "definition"], dbname=plan.db_name, node=node.name,
    )
    constraints = pg.dict_rows(
        executor, node.bin_dir, node.pg_port, plan.db_user,
        f"SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint "
        f"WHERE connamespace = {_lit(schema)}::regnamespace ORDER BY 1;",
        ["object", "definition"], dbname=plan.db_name, node=node.name,
    )
    return {
        "columns": {r["object"]: r["definition"] for r in (columns or [])},
        "indexes": {r["object"]: r["definition"] for r in (indexes or [])},
        "constraints": {r["object"]: r["definition"] for r in (constraints or [])},
    }


def schema_diff(executor_for, plan, schema="public"):
    """Compare column, index and constraint definitions across nodes.

    Compared against the first Spock node as the reference, because in a
    multi-master cluster there is no authoritative copy — the report says
    "differs from n1", not "is wrong".
    """
    fingerprints, problems = {}, []

    for node in plan.spock_nodes:
        fingerprints[node.name] = _schema_fingerprint(
            executor_for(node), plan, node, schema
        )

    if len(fingerprints) < 2:
        return True, {"schema": schema, "problems": [],
                      "note": "only one Spock node — nothing to compare"}

    reference_name = plan.spock_nodes[0].name
    reference = fingerprints[reference_name]
    differences = {}

    for name, fingerprint in fingerprints.items():
        if name == reference_name:
            continue
        node_diff = {}
        for category in ("columns", "indexes", "constraints"):
            ref, other = reference[category], fingerprint[category]
            missing = sorted(set(ref) - set(other))
            extra = sorted(set(other) - set(ref))
            changed = sorted(
                key for key in set(ref) & set(other) if ref[key] != other[key]
            )
            if missing or extra or changed:
                node_diff[category] = {
                    "missing": missing, "extra": extra, "changed": changed,
                }
                noun = SINGULAR[category]
                for key in missing:
                    problems.append(f"{name}: {noun} {key} is missing "
                                    f"(present on {reference_name})")
                for key in extra:
                    problems.append(f"{name}: {noun} {key} is present but "
                                    f"absent on {reference_name}")
                for key in changed:
                    problems.append(
                        f"{name}: {noun} {key} differs from "
                        f"{reference_name} ({ref[key]!r} vs {other[key]!r})"
                    )
        if node_diff:
            differences[name] = node_diff

    return (not problems), {
        "schema": schema,
        "reference": reference_name,
        "object_counts": {
            n: {c: len(f[c]) for c in ("columns", "indexes", "constraints")}
            for n, f in fingerprints.items()
        },
        "differences": differences,
        "problems": problems,
    }


# ---------------------------------------------------------------------------
# table data
# ---------------------------------------------------------------------------


def primary_key_columns(executor, plan, node, table):
    """Ordered primary key columns for a table, or [] when it has none."""
    schema, name = _split_table(table)
    result = pg.rows(
        executor, node.bin_dir, node.pg_port, plan.db_user,
        f"SELECT a.attname FROM pg_index i "
        f"JOIN pg_class c ON c.oid = i.indrelid "
        f"JOIN pg_namespace n ON n.oid = c.relnamespace "
        f"JOIN unnest(i.indkey) WITH ORDINALITY AS k(attnum, ord) ON true "
        f"JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = k.attnum "
        f"WHERE i.indisprimary AND n.nspname = {_lit(schema)} "
        f"AND c.relname = {_lit(name)} ORDER BY k.ord;",
        dbname=plan.db_name, node=node.name,
    )
    return [r[0] for r in (result or [])]


def _bucket_checksums(executor, plan, node, table, pk_columns, buckets,
                      where=None):
    """Per-bucket row count and digest for one node."""
    key = pk_columns[0] if len(pk_columns) == 1 else \
        "(" + " || '|' || ".join(f"t.{c}::text" for c in pk_columns) + ")"
    key_expr = f"t.{key}::text" if len(pk_columns) == 1 else key
    bucket = _bucket_expr(key_expr, buckets)
    order = ", ".join(f"t.{c}" for c in pk_columns)
    filter_clause = f"WHERE {where} " if where else ""

    return pg.dict_rows(
        executor, node.bin_dir, node.pg_port, plan.db_user,
        f"SELECT {bucket} AS bucket, count(*)::text, "
        f"  md5(string_agg(md5(t::text), '' ORDER BY {order})) "
        f"FROM {table} t {filter_clause}"
        f"GROUP BY 1 ORDER BY 1;",
        ["bucket", "rows", "digest"],
        dbname=plan.db_name, node=node.name,
    )


def _bucket_rows(executor, plan, node, table, pk_columns, buckets, bucket,
                 where=None):
    """Per-row digests inside one bucket, for locating exact divergence."""
    key = pk_columns[0] if len(pk_columns) == 1 else \
        "(" + " || '|' || ".join(f"t.{c}::text" for c in pk_columns) + ")"
    key_expr = f"t.{key}::text" if len(pk_columns) == 1 else key
    bucket_expr = _bucket_expr(key_expr, buckets)
    pk_render = " || '|' || ".join(f"t.{c}::text" for c in pk_columns)
    order = ", ".join(f"t.{c}" for c in pk_columns)
    filter_clause = f"AND {where} " if where else ""

    result = pg.dict_rows(
        executor, node.bin_dir, node.pg_port, plan.db_user,
        f"SELECT {pk_render}, md5(t::text) FROM {table} t "
        f"WHERE {bucket_expr} = {bucket} {filter_clause}"
        f"ORDER BY {order} LIMIT {MAX_ROWS_PER_BUCKET};",
        ["pk", "digest"], dbname=plan.db_name, node=node.name,
    )
    return {r["pk"]: r["digest"] for r in (result or [])}


def table_diff(executor_for, plan, table, buckets=DEFAULT_BUCKETS, where=None,
               drill_down=True, run_logger=None):
    """Compare a table's data across every Spock node.

    Returns (ok, report). The report names the buckets that disagree and, when
    drill_down is on, the individual primary keys that differ — enough to hand
    to table_repair or to fix by hand.
    """
    nodes = plan.spock_nodes
    if len(nodes) < 2:
        return True, {"table": table, "problems": [],
                      "note": "only one Spock node — nothing to compare"}

    reference = nodes[0]
    pk_columns = primary_key_columns(executor_for(reference), plan, reference, table)
    if not pk_columns:
        return False, {
            "table": table,
            "problems": [
                f"{table} has no primary key. Spock replicates only INSERTs for "
                f"such tables, and there is no stable key to compare rows by — "
                f"add a primary key or REPLICA IDENTITY FULL."
            ],
        }

    per_node, problems = {}, []
    for node in nodes:
        checksums = _bucket_checksums(
            executor_for(node), plan, node, table, pk_columns, buckets, where
        )
        if checksums is None:
            problems.append(f"{node.name}: cannot read {table}")
            continue
        per_node[node.name] = {b["bucket"]: (b["rows"], b["digest"])
                               for b in checksums}
        if run_logger:
            total = sum(int(r) for r, _ in per_node[node.name].values())
            run_logger.info(f"    {node.name}: {total} rows across "
                            f"{len(per_node[node.name])} buckets")

    if len(per_node) < 2:
        return False, {"table": table, "problems": problems}

    reference_name = reference.name
    reference_buckets = per_node.get(reference_name, {})
    mismatches = {}

    for name, node_buckets in per_node.items():
        if name == reference_name:
            continue
        differing = sorted(
            set(reference_buckets) | set(node_buckets),
            key=lambda b: int(b),
        )
        differing = [
            b for b in differing
            if reference_buckets.get(b) != node_buckets.get(b)
        ]
        if not differing:
            continue

        ref_rows = sum(int(r) for r, _ in reference_buckets.values())
        node_rows = sum(int(r) for r, _ in node_buckets.values())
        mismatches[name] = {
            "buckets": differing,
            "row_count_reference": ref_rows,
            "row_count_node": node_rows,
        }
        problems.append(
            f"{name}: {len(differing)} of {buckets} buckets differ from "
            f"{reference_name} ({node_rows} rows vs {ref_rows})"
        )

        if drill_down:
            differing_keys = {"only_on_reference": [], "only_on_node": [],
                              "different_values": []}
            for bucket in differing:
                ref_map = _bucket_rows(
                    executor_for(reference), plan, reference, table,
                    pk_columns, buckets, bucket, where,
                )
                node_map = _bucket_rows(
                    executor_for(plan.node(name)), plan, plan.node(name), table,
                    pk_columns, buckets, bucket, where,
                )
                differing_keys["only_on_reference"] += sorted(
                    set(ref_map) - set(node_map)
                )
                differing_keys["only_on_node"] += sorted(
                    set(node_map) - set(ref_map)
                )
                differing_keys["different_values"] += sorted(
                    k for k in set(ref_map) & set(node_map)
                    if ref_map[k] != node_map[k]
                )
            mismatches[name]["keys"] = differing_keys
            counts = {k: len(v) for k, v in differing_keys.items()}
            problems.append(
                f"{name}: rows only on {reference_name}: "
                f"{counts['only_on_reference']}, only on {name}: "
                f"{counts['only_on_node']}, same key but different values: "
                f"{counts['different_values']}"
            )

    return (not mismatches and not problems), {
        "table": table,
        "primary_key": pk_columns,
        "buckets": buckets,
        "reference": reference_name,
        "where": where,
        "row_counts": {
            n: sum(int(r) for r, _ in b.values()) for n, b in per_node.items()
        },
        "mismatches": mismatches,
        "problems": problems,
    }


def table_repair(executor_for, plan, table, source_node_name, target_node_names=None,
                 truncate=True, run_logger=None):
    """Re-sync a diverged table on the target nodes from a chosen source.

    Uses Spock's own sub_resync_table, so the copy goes through the existing
    subscription rather than a side channel. `truncate` empties the target's
    copy first, which is what makes the result an exact copy of the source
    rather than a merge.

    This overwrites data on the target nodes. The caller is responsible for
    confirming that with whoever owns the data.
    """
    try:
        source = plan.node(source_node_name)
    except KeyError:
        return False, {"problems": [
            f"{source_node_name!r} is not a node in this cluster; Spock nodes "
            f"are {', '.join(n.name for n in plan.spock_nodes)}"
        ]}
    if not source.is_spock:
        return False, {"problems": [
            f"{source_node_name} is a standby, not a Spock node — it cannot be "
            f"a repair source"
        ]}

    targets = [
        n for n in plan.spock_nodes
        if n.name != source.name
        and (target_node_names is None or n.name in target_node_names)
    ]
    if not targets:
        return False, {"problems": ["no target nodes selected"]}

    results, problems = [], []
    for target in targets:
        executor = executor_for(target)
        # zodan names subscriptions sub_<provider>_<subscriber>.
        subscription = f"sub_{source.name}_{target.name}"

        if run_logger:
            run_logger.info(
                f"    resyncing {table} on {target.name} from {source.name} "
                f"via {subscription}"
            )
        ok, output = spock_operations.sub_resync_table(
            executor, plan, target, subscription, table, truncate=truncate
        )
        results.append({"node": target.name, "subscription": subscription,
                        "ok": ok, "output": output[:1000]})
        if not ok:
            problems.append(f"{target.name}: resync failed — {output[:300]}")
            continue

        synced, message = spock_operations.table_wait_for_sync(
            executor, plan, target, subscription, table
        )
        if not synced:
            problems.append(
                f"{target.name}: resync started but the table did not finish "
                f"synchronising — {message[:300]}"
            )

    return (not problems), {
        "table": table, "source": source.name,
        "targets": [t.name for t in targets],
        "results": results, "problems": problems,
    }


# ---------------------------------------------------------------------------
# Combined
# ---------------------------------------------------------------------------


def full_check(executor_for, plan, schema="public", tables=None,
               buckets=DEFAULT_BUCKETS, run_logger=None):
    """Run every comparison and return one combined report."""
    report = {"cluster": plan.cluster_name, "schema": schema, "checks": {}}

    for name, runner in (
        ("spock", lambda: spock_diff(executor_for, plan)),
        ("repset", lambda: repset_diff(executor_for, plan, schema=schema)),
        ("schema", lambda: schema_diff(executor_for, plan, schema=schema)),
    ):
        if run_logger:
            run_logger.info(f"  checking {name}")
        ok, result = runner()
        report["checks"][name] = {"ok": ok, **result}

    if tables is None:
        node = plan.spock_nodes[0]
        from aspects import db_operations
        listed = db_operations.list_tables(
            executor_for(node), plan, node, schema=schema
        ) or []
        tables = [f"{t['schema']}.{t['table']}" for t in listed]

    report["checks"]["tables"] = {}
    for table in tables:
        if run_logger:
            run_logger.info(f"  checking table {table}")
        ok, result = table_diff(
            executor_for, plan, table, buckets=buckets, run_logger=run_logger
        )
        report["checks"]["tables"][table] = {"ok": ok, **result}

    problems = []
    for name, check in report["checks"].items():
        if name == "tables":
            for table, result in check.items():
                problems += [f"{table}: {p}" for p in result.get("problems", [])]
        else:
            problems += [f"{name}: {p}" for p in check.get("problems", [])]

    report["problems"] = problems
    report["ok"] = not problems
    return report["ok"], report
