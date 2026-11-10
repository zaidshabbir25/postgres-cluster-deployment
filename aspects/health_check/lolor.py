#!/usr/bin/env python3
"""lolor's health check, written for a cluster that is already running.

lolor's own suite cannot say anything about a deployed cluster, and its
Makefile is explicit about why: `REGRESS_OPTS = --temp-instance=./tmp_check
--temp-config=regress.conf`, because the drop guard is an object_access_hook
that needs the library preloaded, and the suite creates cluster-global roles it
does not want on a shared server. PGXS appends REGRESS_OPTS to `installcheck`
as well as `check`, so even the target that sounds like it tests the server at
hand starts a scratch instance instead. The result is real — the installed
library works — but it tests one node's library in isolation, not large
objects travelling between the nodes that were deployed, which is the only
reason to have lolor in a multi-master cluster at all.

So the cases here are our own and ask that question directly: is lolor
preloaded and identified on every node, are its storage tables in a
replication set everywhere, and does a large object created on this node
arrive on every peer, update there, and disappear there when unlinked. They
follow the spock suite's rules — peers through `spock.node_interface`, read via
dblink, fixed `name|true` output — and touch nothing but a `lolor_regress`
schema and the one large object they create and remove.

lolor is only meaningful once the extension exists in a database, and this tool
deliberately creates it after cross-wiring — so a cluster where that step was
skipped reports "not installed" rather than a wall of failures.
"""

from pathlib import Path

from aspects.health_check.runner import InTreeSuite, extension_present

SUITE_DIR = Path(__file__).resolve().parent / "suites" / "lolor"
SQL_DIR = SUITE_DIR / "sql"
EXPECTED_DIR = SUITE_DIR / "expected"

#: The schema the cases work in. Created by 03 and dropped by 04.
SCHEMA = "lolor_regress"


class LolorSuite(InTreeSuite):
    name = "lolor"
    suite_dir = SUITE_DIR
    settings_prefix = "lolor_regress"

    def missing_requirements(self, executor, plan, node):
        missing = super().missing_requirements(executor, plan, node)
        if not extension_present(executor, plan, node, "lolor"):
            missing.append(f"the lolor extension in {plan.db_name} "
                           f"on {node.name}")
        return missing
