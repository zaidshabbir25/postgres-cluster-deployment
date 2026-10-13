#!/usr/bin/env python3
"""Spock's own regression suite, run against the deployed cluster.

`tests/regress` is pg_regress: sql/ in, expected/ compared. The Makefile's
`check` target builds a *temporary* instance to run them in, which tests the
code but says nothing about this cluster. `installcheck` points pg_regress at a
live server instead, which is what upstream's own
tests/run-single-pg18-installcheck.sh does and what is wanted here.

The suite is fetched at the branch or tag the node's Spock was built from. A
newer suite tests behaviour an older extension does not have, and the failures
that produces are about the version skew rather than the cluster.
"""

from regression.runner import PgRegressSuite


class SpockSuite(PgRegressSuite):
    name = "spock"
    repo = "https://github.com/pgEdge/spock.git"

    def environment(self, plan, node):
        env = super().environment(plan, node)
        # The suite creates and drops its own database, so it must not be
        # pointed at the cluster's. Spock's REGRESS_OPTS already says
        # --dbname=regression; this keeps the connection that creates it
        # pointed somewhere that exists.
        env["PGDATABASE"] = plan.db_name
        return env
