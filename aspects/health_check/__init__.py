"""Health checks for the components a cluster runs.

`--health_check spock,lolor,snowflake,ace` asks each component, on the cluster
this tool deployed, whether it works there. One module per component, because
the checks have almost nothing in common: two are cases written for a running
cluster, one is an upstream Python harness built for a different directory
layout, and one is the ACE binary pointed at the nodes.

What each actually tests is worth knowing before reading its result:

  * spock and lolor run cases that ship with this tool, through psql and
    dblink, against the deployed mesh. They create nothing but a schema of
    their own and drop it again.
  * snowflake runs its own `test/runner.py`, driven by EDGE_* environment
    variables but hardcoding a pgEdge-CLI directory layout. A shim directory
    bridges that; it runs against two of the deployed nodes.
  * ace runs the installed ACE binary's spock-diff, schema-diff and
    repset-diff against the deployed nodes — ACE doing its job.
  * spock-upstream and lolor-upstream are the components' own pg_regress
    suites, cloned fresh at the ref the cluster was built from on every run.
    Both start a scratch instance on the host with the installed library,
    so they test the build the nodes run, not the mesh. A component's name
    runs both halves: `spock` is `spock` and `spock-upstream`.
  * ace-integration is ACE's own Go suite. It starts a three-node cluster in
    containers and ignores the deployed one entirely, so it runs only when
    asked for by name, never as part of `all`.

Each component gets its own workspace under REGRESSION_ROOT and its own
report, so one failing suite neither blocks nor contaminates the others.
"""

from aspects.health_check.runner import (
    REGRESSION_ROOT,
    SUITES,
    SuiteResult,
    default_components,
    known_components,
    upstream_components,
    parse_components,
    suite_for,
)

__all__ = [
    "REGRESSION_ROOT",
    "SUITES",
    "SuiteResult",
    "default_components",
    "known_components",
    "upstream_components",
    "parse_components",
    "suite_for",
]
