#!/usr/bin/env python3
"""Spock's regression suite, written for a cluster that is already running.

Upstream's `tests/regress` cannot be used here, and the reason is worth
recording. Those cases build the thing they test: `init.sql` creates the nodes
and the subscription, after creating passwordless roles `super` and `nonsuper`
that only work under the `trust` pg_hba upstream ships for its throwaway
instance. Against a deployed cluster every Spock connection as `super` fails
authentication, `init` fails, and all thirty-eight cases after it fail with it.
Making them pass would mean lowering a running cluster's authentication and
leaving a superuser behind.

So the cases here are our own, and they ask the opposite question: given a
cluster that is already cross-wired and already serving, does replication work?
Nothing is created, initialised or torn down — no instance, no node, no
subscription, no role, no database, and no change to pg_hba. The only objects
that appear are a `spock_regress` schema and one table in it, dropped again by
the last case.

Three things make that possible:

  * peers are found through `spock.node_interface`, which is where the
    deployment registered them and what Spock's own procedures read. A wrong
    DSN there is a real fault, and case 02 is the first to trip over it.
  * peers are read through dblink in one session rather than by reconnecting,
    so nothing depends on psql's handling of a conninfo in a variable, and
    nothing needs a password the suite does not already have.
  * output is tuples-only and unaligned, and every case prints fixed `name|true`
    lines. Detail that names a node is printed only when that node is wrong, so
    a healthy cluster matches a fixed expected file and a broken one produces a
    diff that says which node and why.
"""

import re
import shlex
from pathlib import Path

from regression.runner import Suite

SUITE_DIR = Path(__file__).resolve().parent / "suites" / "spock"
SQL_DIR = SUITE_DIR / "sql"
EXPECTED_DIR = SUITE_DIR / "expected"

#: The schema the cases work in. Created by 05 and dropped by 08.
SCHEMA = "spock_regress"


def available_tests():
    """Case names, in the order their filenames give them.

    Numbered because they depend on each other: 05 creates the table 06 writes
    to, and 08 removes it again.
    """
    return [path.stem for path in sorted(SQL_DIR.glob("*.sql"))]


class SpockSuite(Suite):
    name = "spock"
    repo = ""          # nothing to clone: the cases live in this repository
    requires = {}      # and psql is the only thing they need

    #: Each case waits for replication rather than assuming it, so the limit is
    #: about a cluster that is not replicating at all, not about a slow one.
    test_timeout = 180

    # --- it is already here -------------------------------------------
    def prepare(self, executor, plan, node, ref, run_logger=None):
        """Copy the cases onto the host. There is nothing to fetch."""
        remote_sql = f"{self.workspace()}/sql"
        remote_expected = f"{self.workspace()}/expected"
        executor.run(f"mkdir -p {shlex.quote(remote_sql)} "
                     f"{shlex.quote(remote_expected)} "
                     f"{shlex.quote(self.output_dir())}", node=node.name)

        for local, remote in ((SQL_DIR, remote_sql),
                              (EXPECTED_DIR, remote_expected)):
            for path in sorted(local.iterdir()):
                if path.is_file():
                    executor.put_file(path, f"{remote}/{path.name}",
                                      owner=plan.db_user, mode="644",
                                      node=node.name)
        if run_logger:
            run_logger.info(
                f"    spock: {len(available_tests())} case(s) from this "
                f"repository, against the running cluster — nothing is created"
            )
        return remote_sql

    def missing_requirements(self, executor, plan, node):
        missing = super().missing_requirements(executor, plan, node)
        found, _ = executor.try_run(
            f"test -x {shlex.quote(node.bin_dir)}/psql", node=node.name)
        if not found:
            missing.append(f"psql at {node.bin_dir}")
        return missing

    def tests_for(self, plan, node):
        chosen = self.selected or available_tests()
        known = set(available_tests())
        return [name for name in chosen if name in known] or list(chosen)

    # --- running ------------------------------------------------------
    def execute(self, executor, plan, node, run_logger=None):
        results = f"{self.output_dir()}/results"
        executor.run(f"mkdir -p {shlex.quote(results)}", node=node.name)

        chosen = self.tests_for(plan, node)
        lines = []
        passed = failed = 0

        if run_logger:
            run_logger.info(
                f"    spock: {len(chosen)} case(s) on {node.name} as "
                f"{plan.db_user}, {self.test_timeout}s each"
            )

        for index, name in enumerate(chosen, start=1):
            verdict = self._run_one(executor, plan, node, name, results)
            lines.append(f"test {name:<28} ... {verdict}")
            if verdict == "ok":
                passed += 1
            else:
                failed += 1
            if run_logger:
                run_logger.info(
                    f"      [{index}/{len(chosen)}] {name:<28} {verdict}")

        total = passed + failed
        if failed:
            lines.append(f"{failed} of {total} tests failed.")
        elif total:
            lines.append(f"All {total} tests passed.")
        return failed == 0, "\n".join(lines)

    def _run_one(self, executor, plan, node, name, results):
        sql = f"{self.workspace()}/sql/{name}.sql"
        expected = f"{self.workspace()}/expected/{name}.out"
        actual = f"{results}/{name}.out"

        # -A -t: values only, so the expected files hold what the case asserts
        # rather than an echo of how it asked. -w: never prompt, so a missing
        # password fails at once instead of consuming the whole timeout.
        # Settings rather than psql variables: psql does not interpolate
        # :name inside a dollar-quoted body, and the cases need the same value
        # in both places. Separate -c arguments, so each is its own statement
        # in the session the -f then runs in.
        set_node = f"SET spock_regress.node = {_lit(node.name)}"
        set_count = (f"SET spock_regress.node_count = "
                     f"{len(plan.spock_nodes)}")
        settings = (f"-c {shlex.quote(set_node)} "
                    f"-c {shlex.quote(set_count)} ")
        try:
            executor.try_run(
                f"{shlex.quote(node.bin_dir)}/psql -X -q -w -A -t "
                f"-v ON_ERROR_STOP=0 "
                f"-h {shlex.quote(node.address)} -p {node.pg_port} "
                f"-U {shlex.quote(plan.db_user)} "
                f"-d {shlex.quote(plan.db_name)} "
                f"{settings}-f {shlex.quote(sql)} "
                f"> {shlex.quote(actual)} 2>&1",
                node=node.name, timeout=self.test_timeout,
            )
        except Exception as exc:
            if "Timeout" in type(exc).__name__:
                return f"TIMED OUT after {self.test_timeout}s"
            return f"FAILED ({type(exc).__name__})"

        same, _ = executor.try_run(
            f"diff -u {shlex.quote(expected)} {shlex.quote(actual)} "
            f"> {shlex.quote(results)}/{name}.diff 2>&1", node=node.name)
        if same:
            return "ok"
        return f"FAILED{self._first_problem(executor, node, actual)}"

    def _first_problem(self, executor, node, actual):
        """The first thing that looks like an explanation.

        An ERROR if there is one; otherwise the first assertion that came back
        false, or the first line naming a node as disagreeing. Forty rows
        saying FAILED say only that something is wrong.
        """
        ok, line = executor.try_run(
            f"grep -m1 -E '^(psql:.*)?(ERROR|FATAL):|\\|false$|^(DOWN|DISAGREES)\\|' "
            f"{shlex.quote(actual)} 2>/dev/null", node=node.name)
        text = (line or "").strip()
        if not ok or not text:
            return ""
        text = re.sub(r"^psql:[^:]*:\d+:\s*", "", text)
        return f" — {text[:120]}"

    def parse(self, output):
        passed = len(re.findall(r"\.\.\. ok$", output or "", re.MULTILINE))
        failed = len(re.findall(r"\.\.\. (FAILED|TIMED OUT)", output or "",
                                re.MULTILINE))
        if not (passed or failed):
            return {}
        return {"total": passed + failed, "passed": passed, "failed": failed}

    def artefacts(self, executor, node):
        return [f"{self.output_dir()}/results"]


def _lit(value):
    return "'" + str(value).replace("'", "''") + "'"
