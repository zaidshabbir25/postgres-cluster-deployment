#!/usr/bin/env python3
"""lolor's regression suite.

A plain PGXS extension with `REGRESS = lolor` and `TAP_TESTS = 1`, so
`installcheck` runs sql/lolor.sql against a live server and compares it with
expected/. The TAP tests need a temporary instance of their own and are not run
here: they would say nothing about this cluster.

lolor is only meaningful once the extension exists in a database, and this tool
deliberately creates it after cross-wiring — so a cluster where that step was
skipped reports "not installed" rather than a wall of failures.
"""

from regression.runner import PgRegressSuite, extension_present


class LolorSuite(PgRegressSuite):
    name = "lolor"
    repo = "https://github.com/pgEdge/lolor.git"

    def missing_requirements(self, executor, plan, node):
        missing = super().missing_requirements(executor, plan, node)
        if not extension_present(executor, plan, node, "lolor"):
            missing.append(f"the lolor extension in {plan.db_name} "
                           f"on {node.name}")
        return missing
