#!/usr/bin/env python3
"""Spock's regression SQL, run against the deployed cluster.

`tests/regress/sql/*.sql` with `tests/regress/expected/*.out` beside them is the
whole suite. Upstream drives it through `make installcheck`, which needs the
server's development files; this runs the same files directly with psql, which
needs nothing a cluster does not already have.

**These tests own the databases they run in.** `infofuncs.sql` ends with
`DROP EXTENSION spock`, `init.sql` creates roles and replicates DDL, and
`init.sql` onwards builds its own Spock nodes and subscriptions. Pointing them
at the cluster's own database would dismantle the cluster. So scratch databases
are created on the real nodes — same servers, same ports, same binaries, same
Spock — and the tests own those. The cluster's database is never opened.

Aligning the SQL is a matter of four GUCs rather than editing any file. Every
test begins

    SELECT * FROM spock_regress_variables()
    \\gset
    \\c :provider_dsn

and `preseed.sql` defines that function as a read of four settings, which
upstream's regress-postgresql.conf fills with `dbname=regression` and friends —
two databases in one temporary instance. Setting them to conninfo strings for
the scratch databases on *different nodes* maps the same tests onto a real
multi-node cluster, which is a better test than the original.
"""

import re
import shlex

from regression.runner import Suite

# The order from Spock's own Makefile. It matters: preseed seeds data that
# preseed_check verifies after init has cloned it, and everything from init
# onwards assumes the nodes it created.
REGRESS_ORDER = (
    "preseed", "infofuncs", "init_fail", "init", "preseed_check", "basic",
    "conflict_secondary_unique", "excluded_schema", "conflict_stat",
    "toasted", "replication_set", "exception_row_capture",
    "wire_tuple_sanity", "matview", "bidirectional", "primary_key",
    "interfaces", "foreign_key", "copy", "sequence", "triggers", "parallel",
    "functions", "row_filter", "row_filter_sampling", "att_list",
    "column_filter", "apply_delay", "alter_options", "extended",
    "node_origin_cascade", "multiple_upstreams", "tuple_origin", "autoddl",
    "sync_event", "sync_table", "generated_columns", "spill_transaction",
    "read_only", "resolutions_retention", "drop",
)

# Upstream disables these in its own Makefile; running them would report
# failures their authors already know about.
DISABLED = {"add_table"}

# PG18 added what conflict_stat reads.
PG18_ONLY = {"conflict_stat"}

# The four databases the suite expects, as scratch copies on real nodes. The
# names say what they are for, and nothing else in the cluster uses them.
SCRATCH = {
    "provider_dsn": "spockregress_provider",
    "subscriber_dsn": "spockregress_subscriber",
    "provider1_dsn": "spockregress_provider1",
    "orig_provider_dsn": "spockregress_source",
}


class SpockSuite(Suite):
    name = "spock"
    repo = "https://github.com/pgEdge/spock.git"
    requires = {"git": "to fetch the suite"}

    # --- what runs where ---------------------------------------------
    def tests_for(self, plan, node):
        major = _major(getattr(node, "pg_version", "") or plan.pg_version
                       or plan.pg_major)
        chosen = self.selected or [t for t in REGRESS_ORDER if t not in DISABLED]
        if major and major < 18:
            chosen = [t for t in chosen if t not in PG18_ONLY]
        return chosen

    def placement(self, plan):
        """Which node hosts each scratch database.

        Spread across the Spock nodes so provider and subscriber are genuinely
        different servers. A one-node cluster puts them all on that node, which
        is what upstream does anyway — separate databases, one instance.
        """
        peers = plan.spock_nodes or plan.nodes
        order = ("provider_dsn", "subscriber_dsn", "provider1_dsn",
                 "orig_provider_dsn")
        return {key: peers[index % len(peers)]
                for index, key in enumerate(order)}

    def dsn_for(self, plan, node, dbname):
        return (f"host={node.address} port={node.pg_port} "
                f"dbname={dbname} user={plan.db_user}")

    # --- preparing ----------------------------------------------------
    def missing_requirements(self, executor, plan, node):
        missing = super().missing_requirements(executor, plan, node)
        found, _ = executor.try_run(
            f"test -x {shlex.quote(node.bin_dir)}/psql", node=node.name
        )
        if not found:
            missing.append(f"psql at {node.bin_dir}")
        return missing

    def prepare(self, executor, plan, node, ref, run_logger=None):
        source = super().prepare(executor, plan, node, ref,
                                 run_logger=run_logger)
        self._create_scratch_databases(executor, plan, node,
                                       run_logger=run_logger)
        return source

    def _create_scratch_databases(self, executor, plan, node, run_logger=None):
        """Fresh databases for the suite to own, and the DSNs pointing at them.

        Dropped and recreated every run: these tests leave roles, tables and
        Spock nodes behind by design, and a second run against the leftovers of
        the first fails in ways that say nothing about the cluster.
        """
        from aspects import pg_server_management

        placement = self.placement(plan)
        dsns = {key: self.dsn_for(plan, placement[key], SCRATCH[key])
                for key in SCRATCH}

        for key, dbname in SCRATCH.items():
            target = placement[key]
            for statement in (
                f'DROP DATABASE IF EXISTS "{dbname}" WITH (FORCE);',
                f'CREATE DATABASE "{dbname}";',
            ):
                code, output = pg_server_management.psql(
                    executor, target.bin_dir, target.pg_port, plan.db_user,
                    statement, dbname=plan.db_name, node=target.name,
                    check=False,
                )
                if code != 0 and "CREATE DATABASE" in statement:
                    raise RuntimeError(
                        f"could not create the scratch database {dbname} on "
                        f"{target.name}: {output.strip()[-300:]}"
                    )
            # The GUCs have to survive psql's \\c, so they live on the database
            # rather than the session.
            for guc, value in dsns.items():
                pg_server_management.psql(
                    executor, target.bin_dir, target.pg_port, plan.db_user,
                    f'ALTER DATABASE "{dbname}" SET spock.{guc} = '
                    f"{_lit(value)};",
                    dbname=plan.db_name, node=target.name, check=False,
                )

        if run_logger:
            run_logger.info(
                f"    spock: provider={SCRATCH['provider_dsn']}@"
                f"{placement['provider_dsn'].name}, "
                f"subscriber={SCRATCH['subscriber_dsn']}@"
                f"{placement['subscriber_dsn'].name} "
                f"— the cluster's own database is not opened"
            )
        self._warn_about_settings(executor, plan, node, run_logger)
        return dsns

    def _warn_about_settings(self, executor, plan, node, run_logger):
        """Settings upstream's regress config turns on that a cluster may not.

        These cannot be fixed per database — they need a restart — so the run
        says what will look wrong rather than silently producing diffs.
        """
        from aspects import pg_server_management

        for setting, wanted in (("track_commit_timestamp", "on"),):
            value = pg_server_management.scalar(
                executor, node.bin_dir, node.pg_port, plan.db_user,
                f"SHOW {setting};", dbname=plan.db_name, node=node.name,
                default="",
            )
            if (value or "").strip() != wanted and run_logger:
                run_logger.warn(
                    f"    spock: {setting} is {value or 'unknown'}; upstream's "
                    f"regression config sets it {wanted}, so some tests will "
                    f"differ from their expected output"
                )

    # --- running ------------------------------------------------------
    #: Per test, not for the whole suite. Some of these set up replication and
    #: wait for it; a case that never finishes is a result worth having, and
    #: 30 minutes of silence is not.
    test_timeout = 300

    def execute(self, executor, plan, node, run_logger=None):
        source = self.source_dir()
        results = f"{self.output_dir()}/results"
        executor.run(f"mkdir -p {shlex.quote(results)}", node=node.name)

        entry = self.placement(plan)["provider_dsn"]
        psql = f"{entry.bin_dir}/psql"
        chosen = self.tests_for(plan, node)
        lines = []
        passed = failed = missing = 0

        if run_logger:
            run_logger.info(
                f"    spock: {len(chosen)} case(s) against "
                f"{SCRATCH['provider_dsn']}, {self.test_timeout}s each"
            )

        for index, test in enumerate(chosen, start=1):
            sql = f"{source}/tests/regress/sql/{test}.sql"
            expected = f"{source}/tests/regress/expected/{test}.out"
            actual = f"{results}/{test}.out"

            exists, _ = executor.try_run(f"test -f {shlex.quote(sql)}",
                                         node=node.name)
            if not exists:
                verdict = "absent from this ref"
                missing += 1
            else:
                verdict = self._run_one(executor, node, entry, plan, psql, sql,
                                        expected, actual, results, test)
                if verdict == "ok":
                    passed += 1
                else:
                    failed += 1

            lines.append(f"test {test:<28} ... {verdict}")
            # Said as it happens. Forty cases with nothing printed until the
            # end is indistinguishable from a hang, which is how this looked
            # the first time it ran for real.
            if run_logger:
                run_logger.info(f"      [{index}/{len(chosen)}] {test:<28} "
                                f"{verdict}")

        total = passed + failed
        if failed:
            lines.append(f"{failed} of {total} tests failed.")
        elif total:
            lines.append(f"All {total} tests passed.")
        if missing:
            lines.append(f"({missing} not present at this ref)")
        return failed == 0, "\n".join(lines)

    def _run_one(self, executor, node, entry, plan, psql, sql, expected,
                 actual, results, test):
        """One case: run it, diff it, and survive it hanging.

        A case that times out is this case's failure, not the suite's: the ones
        already run have results worth keeping and the ones after it have not
        been tried yet.
        """
        try:
            # pg_regress invokes psql exactly like this, which is why the
            # expected files hold the statements as well as their output.
            executor.try_run(
                f"cd {shlex.quote(self.source_dir())}/tests/regress && "
                f"{shlex.quote(psql)} -X -a -q "
                f"-h {shlex.quote(entry.address)} -p {entry.pg_port} "
                f"-U {shlex.quote(plan.db_user)} "
                f"-d {shlex.quote(SCRATCH['provider_dsn'])} "
                f"-f {shlex.quote(sql)} > {shlex.quote(actual)} 2>&1",
                node=node.name, timeout=self.test_timeout,
            )
        except Exception as exc:
            name = type(exc).__name__
            if "Timeout" in name or "timed out" in str(exc).lower():
                return f"TIMED OUT after {self.test_timeout}s"
            return f"FAILED ({name})"

        same, _ = executor.try_run(
            f"diff -u {shlex.quote(expected)} {shlex.quote(actual)} "
            f"> {shlex.quote(results)}/{test}.diff 2>&1",
            node=node.name,
        )
        return "ok" if same else "FAILED"

    def parse(self, output):
        passed = len(re.findall(r"\.\.\. ok$", output or "", re.MULTILINE))
        failed = len(re.findall(r"\.\.\. (FAILED|TIMED OUT)", output or "",
                                re.MULTILINE))
        if not (passed or failed):
            return {}
        return {"total": passed + failed, "passed": passed, "failed": failed}

    def artefacts(self, executor, node):
        """The diffs say what differed; keep them and the actual output."""
        return [f"{self.output_dir()}/results"]


def _lit(value):
    return "'" + str(value).replace("'", "''") + "'"


def _major(version):
    match = re.match(r"^(\d+)", str(version or ""))
    return int(match.group(1)) if match else 0
