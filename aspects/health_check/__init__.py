"""Upstream regression suites for the components a cluster runs.

`--health_check spock,lolor,snowflake,ace` runs each component's own test suite
against a cluster this tool deployed. One module per component, because the
four suites have almost nothing in common: two are pg_regress, one is a Python
harness built for a different directory layout, and one starts its own
containers.

What each suite actually tests is worth knowing before reading its result:

  * spock and lolor run `make USE_PGXS=1 installcheck`, which points pg_regress
    at a *live server*. These exercise the cluster this tool deployed.
  * snowflake runs its own `test/runner.py`, which is driven by EDGE_*
    environment variables but hardcodes a pgEdge-CLI directory layout. A shim
    directory bridges that; it still runs against the deployed cluster.
  * ACE's integration tests start their own three-node PostgreSQL cluster with
    testcontainers and docker-compose. They validate ACE itself and ignore the
    deployed cluster entirely. The result says so rather than implying
    otherwise.

Each component gets its own workspace under REGRESSION_ROOT and its own
report, so one failing suite neither blocks nor contaminates the others.
"""

from regression.runner import (
    REGRESSION_ROOT,
    SUITES,
    SuiteResult,
    known_components,
    parse_components,
    suite_for,
)

__all__ = [
    "REGRESSION_ROOT",
    "SUITES",
    "SuiteResult",
    "known_components",
    "parse_components",
    "suite_for",
]
