#!/usr/bin/env python3
"""ACE's integration tests.

These are Go tests, and unlike the other three they do **not** examine the
cluster this tool deployed. `tests/integration/main_test.go` brings up its own
three-node PostgreSQL cluster with testcontainers and the docker-compose file
beside it, on ports 6432-6434, and exercises ACE against that. Running them
proves the ACE binary and its test fixtures are sound on this host; it proves
nothing about the deployed cluster.

That distinction is carried through to the report rather than smoothed over:
`targets_cluster` is False, and a green result says ACE's own suite passed, not
that the cluster is consistent. For the latter, run ACE itself against the
cluster — `ace table-diff` — which is a different operation.

The suite needs Go to build it and a working Docker daemon to run it; a host
with neither is reported as skipped, with which one was missing.
"""

import re
import shlex

from regression.runner import Suite


class AceSuite(Suite):
    name = "ace"
    repo = "https://github.com/pgEdge/ace.git"
    targets_cluster = False
    requires = {
        "git": "to fetch the suite",
        "go": "the tests are Go; 1.26 or newer",
        "docker": "the tests start their own PostgreSQL containers",
    }

    def missing_requirements(self, executor, plan, node):
        missing = super().missing_requirements(executor, plan, node)
        if any(item.startswith("docker") for item in missing):
            return missing
        # Installed but not running is the common case, and it fails deep
        # inside testcontainers with an error that does not mention the daemon.
        running, _ = executor.try_run("docker info >/dev/null 2>&1",
                                      node=node.name)
        if not running:
            missing.append("a running Docker daemon (docker info failed)")
        return missing

    def execute(self, executor, plan, node, run_logger=None):
        log = f"{self.output_dir()}/go-test.log"
        if run_logger:
            run_logger.info(
                "    ace: starting its own containers — this does not read "
                f"cluster '{plan.cluster_name}'"
            )
        # -count=1 defeats Go's test cache: a cached pass from a previous run
        # would report success without starting anything.
        ok, output = executor.try_run(
            f"cd {shlex.quote(self.source_dir())} && "
            f"PATH=/usr/local/go/bin:$PATH go test -count=1 -v -timeout 45m "
            f"./tests/integration/... 2>&1 | tee {shlex.quote(log)}",
            node=node.name, timeout=3600,
        )
        return ok, output

    def parse(self, output):
        """`go test -v` marks every case, and prints ok/FAIL per package."""
        passed = len(re.findall(r"^--- PASS:", output or "", re.MULTILINE))
        failed = len(re.findall(r"^--- FAIL:", output or "", re.MULTILINE))
        skipped = len(re.findall(r"^--- SKIP:", output or "", re.MULTILINE))
        if not (passed or failed or skipped):
            return {}
        return {"total": passed + failed + skipped, "passed": passed,
                "failed": failed, "skipped": skipped}

    def artefacts(self, executor, node):
        return [f"{self.output_dir()}/go-test.log"]
