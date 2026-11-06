#!/usr/bin/env python3
"""Spock's own regression suite, fetched fresh at the cluster's branch.

This is upstream's `tests/regress`, forty-odd cases, run the way upstream
runs them: pg_regress starts a scratch instance with the *installed* Spock
library preloaded, creates the roles and nodes the cases expect under the
trust authentication of that instance, and tears it down again. Every run
clones the branch anew, so the cases are whatever upstream has today for the
branch this cluster was built from — v5_STABLE for spock50, main for spock60,
or the exact tag of a source build.

What the verdict is about, and is not: the Spock library the nodes run,
exercised in isolation on one host. Not the mesh, not the subscriptions, not
the cross-wiring — the in-tree `spock` suite is for those. The two are
complementary, which is why asking for `spock` runs both.

What is never run: upstream's `check` target. It is `check: install
regresscheck`, and on a cluster host the `install` half would put the
checkout's spock.so over the one every node is running. Only `regresscheck`
is invoked, which builds nothing and installs nothing.
"""

import shlex

from aspects.health_check.runner import PgRegressSuite


class SpockUpstreamSuite(PgRegressSuite):
    name = "spock-upstream"
    repo = "https://github.com/pgEdge/spock.git"
    scope = ("a scratch instance on the host, with the Spock library the "
             "nodes run")

    def install_target(self):
        # regresscheck, never check: see the module docstring.
        return "regresscheck"

    def missing_requirements(self, executor, plan, node):
        missing = super().missing_requirements(executor, plan, node)
        _, pkglibdir = executor.try_run(
            f"{shlex.quote(node.bin_dir)}/pg_config --pkglibdir 2>/dev/null",
            node=node.name)
        module = f"{pkglibdir.strip() or '/unknown'}/spock.so"
        found, _ = executor.try_run(f"test -f {shlex.quote(module)}",
                                    node=node.name)
        if not found:
            missing.append(f"the installed Spock library at {module}")
        return missing

    def artefacts(self, executor, node):
        """Where upstream's regresscheck leaves what differed."""
        regress = f"{self.source_dir()}/tests/regress"
        found = []
        for candidate in ("regression_output/regression.diffs",
                          "regression.diffs",
                          "regression_output/log/postmaster.log"):
            path = f"{regress}/{candidate}"
            exists, _ = executor.try_run(f"test -s {shlex.quote(path)}",
                                         node=node.name)
            if exists:
                found.append(path)
        return found
