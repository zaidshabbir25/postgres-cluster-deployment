#!/usr/bin/env python3
"""Test workloads for exercising a cluster.

The pgEdge CLI's `cluster app-install` / `app-remove`. Two workloads:

    pgbench   ships with PostgreSQL, so nothing extra is downloaded
    sample    a small multi-master-shaped schema written here

Both are shaped for Spock rather than for raw throughput. pgbench's own schema
gives pgbench_history no primary key, which means Spock replicates INSERTs into
it but silently drops UPDATEs and DELETEs — so a primary key is added after
initialisation. Without that, a pgbench run against a multi-master cluster
diverges and the cause is very hard to see.
"""

from aspects import pg_server_management as pg
from aspects import spock_operations

APPS = ("pgbench", "sample")

# pgbench_history has no primary key of its own; Spock needs one.
PGBENCH_FIXUPS = (
    "ALTER TABLE pgbench_history ADD COLUMN IF NOT EXISTS hid bigserial;",
    "ALTER TABLE pgbench_history DROP CONSTRAINT IF EXISTS pgbench_history_pkey;",
    "ALTER TABLE pgbench_history ADD CONSTRAINT pgbench_history_pkey "
    "PRIMARY KEY (hid);",
)

SAMPLE_SCHEMA = """
-- Managed by pg-cluster-deployment: a small workload shaped for multi-master.
CREATE TABLE IF NOT EXISTS customers (
    id          bigint PRIMARY KEY,
    name        text NOT NULL,
    email       text,
    region      text,
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS orders (
    id          bigint PRIMARY KEY,
    customer_id bigint NOT NULL REFERENCES customers(id),
    total_cents bigint NOT NULL DEFAULT 0,
    status      text NOT NULL DEFAULT 'new',
    placed_at   timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS order_items (
    id          bigint PRIMARY KEY,
    order_id    bigint NOT NULL REFERENCES orders(id),
    sku         text NOT NULL,
    quantity    int NOT NULL DEFAULT 1,
    price_cents bigint NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS orders_customer_idx ON orders (customer_id);
CREATE INDEX IF NOT EXISTS order_items_order_idx ON order_items (order_id);
"""

# Every id is derived from a per-node offset, so two nodes writing at once
# cannot collide on a primary key. This is the same problem Snowflake sequences
# solve; doing it explicitly keeps the sample workload dependency-free.
SAMPLE_DATA = """
INSERT INTO customers (id, name, email, region)
SELECT {offset} + g,
       'customer-' || ({offset} + g),
       'c' || ({offset} + g) || '@example.test',
       (ARRAY['emea','apac','amer'])[1 + (g %% 3)]
FROM generate_series(1, {rows}) g
ON CONFLICT (id) DO NOTHING;

INSERT INTO orders (id, customer_id, total_cents, status)
SELECT {offset} + g, {offset} + g, (g * 997) %% 50000,
       (ARRAY['new','paid','shipped'])[1 + (g %% 3)]
FROM generate_series(1, {rows}) g
ON CONFLICT (id) DO NOTHING;

INSERT INTO order_items (id, order_id, sku, quantity, price_cents)
SELECT {offset} + g, {offset} + g, 'SKU-' || (g %% 250), 1 + (g %% 4),
       (g * 331) %% 20000
FROM generate_series(1, {rows}) g
ON CONFLICT (id) DO NOTHING;
"""

SAMPLE_TABLES = ("order_items", "orders", "customers")  # drop order


def node_offset(plan, node, span=1_000_000_000):
    """A disjoint primary-key range per node.

    Derived from the node's position in the plan, so it is stable across runs
    and identical on every host that computes it.
    """
    names = [n.name for n in plan.spock_nodes]
    index = names.index(node.name) if node.name in names else 0
    return index * span


# ---------------------------------------------------------------------------
# Install
# ---------------------------------------------------------------------------


def install(executor_for, plan, app="pgbench", scale=1, rows=1000,
            add_to_repset=True, run_logger=None):
    """Install a workload on every Spock node.

    Only the first node is populated with data; the rest receive it through
    replication, which is both faster and a genuine end-to-end test that
    replication works.
    """
    if app not in APPS:
        return False, {"problems": [f"unknown app {app!r}; expected one of {APPS}"]}

    nodes = plan.spock_nodes
    if not nodes:
        return False, {"problems": ["cluster has no Spock nodes"]}

    first = nodes[0]
    results, problems = [], []

    if app == "pgbench":
        ok, detail = _install_pgbench(executor_for(first), plan, first, scale,
                                      run_logger)
        results.append({"node": first.name, "step": "pgbench -i", "ok": ok,
                        "detail": detail})
        if not ok:
            problems.append(f"{first.name}: pgbench initialisation failed — {detail}")
    else:
        ok, detail = _install_sample(executor_for(first), plan, first, rows,
                                     run_logger)
        results.append({"node": first.name, "step": "sample schema", "ok": ok,
                        "detail": detail})
        if not ok:
            problems.append(f"{first.name}: sample install failed — {detail}")

    if problems:
        return False, {"app": app, "results": results, "problems": problems}

    warnings = []
    if add_to_repset:
        if run_logger:
            run_logger.info("    adding tables to the default replication set")
        ok, output, no_pk = spock_operations.repset_add_all_tables(
            executor_for(first), plan, first, "default", ("public",),
            sync_data=True,
        )
        results.append({"node": first.name, "step": "repset-add-all-tables",
                        "ok": ok, "detail": output[:500]})
        if not ok:
            problems.append(f"{first.name}: repset-add-all-tables failed")

        # Tables with no primary key are reported as warnings, not failures:
        # repset_add_all_tables reports every such table in the schema, and a
        # pre-existing unrelated one must not make installing a workload fail.
        for entry in no_pk:
            warnings.append(
                f"{entry['schema']}.{entry['table']} has no primary key — "
                f"Spock will replicate INSERTs into it but silently drop "
                f"UPDATEs and DELETEs"
            )
            if run_logger:
                run_logger.warn(warnings[-1])

    return (not problems), {"app": app, "seeded_on": first.name,
                            "results": results, "problems": problems,
                            "warnings": warnings}


def _install_pgbench(executor, plan, node, scale, run_logger=None):
    if run_logger:
        run_logger.info(f"    pgbench -i -s {scale} on {node.name}")
    ok, output = executor.try_run(
        f"{node.bin_dir}/pgbench -i -s {scale} -q "
        f"-h 127.0.0.1 -p {node.pg_port} -U {plan.db_user} {plan.db_name}",
        user=plan.db_user, node=node.name, timeout=3600,
    )
    if not ok:
        return False, output.strip()[:1000]

    for statement in PGBENCH_FIXUPS:
        code, fix_output = pg.psql(
            executor, node.bin_dir, node.pg_port, plan.db_user, statement,
            dbname=plan.db_name, node=node.name, check=False,
        )
        if code != 0 and "already exists" not in fix_output:
            return False, f"pgbench_history primary key fixup failed: {fix_output[:400]}"

    return True, f"pgbench schema at scale {scale}, pgbench_history given a PK"


def _install_sample(executor, plan, node, rows, run_logger=None):
    if run_logger:
        run_logger.info(f"    sample schema with {rows} rows on {node.name}")
    code, output = pg.psql(
        executor, node.bin_dir, node.pg_port, plan.db_user, SAMPLE_SCHEMA,
        dbname=plan.db_name, node=node.name, check=False, timeout=600,
    )
    if code != 0:
        return False, output.strip()[:1000]

    data = SAMPLE_DATA.format(offset=node_offset(plan, node), rows=int(rows))
    code, output = pg.psql(
        executor, node.bin_dir, node.pg_port, plan.db_user, data,
        dbname=plan.db_name, node=node.name, check=False, timeout=1800,
    )
    if code != 0:
        return False, output.strip()[:1000]
    return True, f"3 tables, {rows} rows each"


# ---------------------------------------------------------------------------
# Remove
# ---------------------------------------------------------------------------


def remove(executor_for, plan, app="pgbench", run_logger=None):
    """Drop a workload's tables from every Spock node.

    Issued on every node rather than relying on replicated DDL, because auto-DDL
    may be off and a half-dropped schema is worse than either state.
    """
    if app not in APPS:
        return False, {"problems": [f"unknown app {app!r}"]}

    tables = (
        ("pgbench_history", "pgbench_accounts", "pgbench_tellers", "pgbench_branches")
        if app == "pgbench" else SAMPLE_TABLES
    )
    results, problems = [], []

    for node in plan.spock_nodes:
        executor = executor_for(node)
        for table in tables:
            code, output = pg.psql(
                executor, node.bin_dir, node.pg_port, plan.db_user,
                f"DROP TABLE IF EXISTS {table} CASCADE;",
                dbname=plan.db_name, node=node.name, check=False,
            )
            results.append({"node": node.name, "table": table, "ok": code == 0})
            if code != 0:
                problems.append(f"{node.name}: dropping {table} failed — "
                                f"{output.strip()[:200]}")
        if run_logger:
            run_logger.info(f"    {node.name}: {app} tables dropped")

    return (not problems), {"app": app, "results": results, "problems": problems}


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------


def run_pgbench(executor, plan, node, clients=4, jobs=2, duration=30,
                read_only=False, run_logger=None):
    """Drive load at one node, to watch it replicate to the others."""
    mode = "-S " if read_only else ""
    if run_logger:
        run_logger.info(
            f"    pgbench {clients} clients for {duration}s against {node.name}"
        )
    ok, output = executor.try_run(
        f"{node.bin_dir}/pgbench {mode}-c {clients} -j {jobs} -T {duration} "
        f"-h 127.0.0.1 -p {node.pg_port} -U {plan.db_user} {plan.db_name}",
        user=plan.db_user, node=node.name, timeout=duration + 300,
    )
    return ok, output.strip()


def row_counts(executor_for, plan, app="pgbench"):
    """Row counts per table per node — the quickest replication smoke test."""
    tables = (
        ("pgbench_accounts", "pgbench_history")
        if app == "pgbench" else SAMPLE_TABLES
    )
    counts = {}
    for node in plan.spock_nodes:
        executor = executor_for(node)
        counts[node.name] = {}
        for table in tables:
            value = pg.scalar(
                executor, node.bin_dir, node.pg_port, plan.db_user,
                f"SELECT count(*) FROM {table};",
                dbname=plan.db_name, node=node.name, default="?",
            )
            counts[node.name][table] = value
    return counts


def concurrent_index(executor_for, plan, table, column, index_name=None,
                     run_logger=None):
    """Build an index concurrently on every node.

    CREATE INDEX CONCURRENTLY cannot run inside a transaction, so it cannot be
    replicated by Spock's DDL machinery — it has to be issued on each node
    directly. That is exactly why the CLI has a dedicated command for it.
    """
    index_name = index_name or f"{table.replace('.', '_')}_{column}_idx"
    results, problems = [], []

    for node in plan.spock_nodes:
        executor = executor_for(node)
        if run_logger:
            run_logger.info(f"    building {index_name} on {node.name}")
        code, output = pg.psql(
            executor, node.bin_dir, node.pg_port, plan.db_user,
            f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {index_name} "
            f"ON {table} ({column});",
            dbname=plan.db_name, node=node.name, check=False, timeout=7200,
        )
        results.append({"node": node.name, "ok": code == 0,
                        "detail": output.strip()[:300]})
        if code != 0:
            problems.append(f"{node.name}: {output.strip()[:200]}")

    return (not problems), {"index": index_name, "table": table,
                            "results": results, "problems": problems}
