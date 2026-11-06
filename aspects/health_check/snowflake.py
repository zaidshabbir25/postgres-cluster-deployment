#!/usr/bin/env python3
"""Snowflake's test harness, pointed at the deployed cluster.

`test/runner.py` reads its whole configuration from EDGE_* environment
variables, which is what makes it usable here at all. One thing it does not
take from the environment is where psql lives: it builds that path itself, as

    $EDGE_CLUSTER_DIR/n1/pgedge/$EDGE_COMPONENT/bin/psql

which is the pgEdge CLI's layout, not ours. Rather than patch the harness — a
copy that drifts from upstream is worse than no copy — a small directory of
symlinks is built in the component's own workspace so that path resolves to
this cluster's binaries. Everything else is handed over as environment.

The harness drives two nodes (n1 and n2 = EDGE_START_PORT and +1), so a
single-node cluster cannot run it.
"""

import shlex

from regression.runner import Suite, extension_present


class SnowflakeSuite(Suite):
    name = "snowflake"
    repo = "https://github.com/pgEdge/snowflake.git"
    requires = {"git": "to fetch the suite", "python3": "to run the harness"}

    def shim_dir(self):
        """The pgEdge-shaped tree the harness expects to find psql under."""
        return f"{self.workspace()}/cluster"

    def missing_requirements(self, executor, plan, node):
        missing = super().missing_requirements(executor, plan, node)
        if not extension_present(executor, plan, node, "snowflake"):
            missing.append(f"the snowflake extension in {plan.db_name} "
                           f"on {node.name}")
        if len(plan.spock_nodes) < 2:
            missing.append("two Spock nodes (the harness drives n1 and n2)")
        return missing

    def prepare(self, executor, plan, node, ref, run_logger=None):
        source = super().prepare(executor, plan, node, ref,
                                 run_logger=run_logger)

        # $EDGE_CLUSTER_DIR/n1/pgedge/$EDGE_COMPONENT/bin -> this node's bin.
        component = f"pg{plan.pg_major}"
        bin_link = f"{self.shim_dir()}/n1/pgedge/{component}"
        executor.run(f"mkdir -p {shlex.quote(bin_link)}", node=node.name)
        executor.run(
            f"ln -sfn {shlex.quote(node.bin_dir)} {shlex.quote(bin_link)}/bin",
            node=node.name, message="link psql into the layout the harness expects",
        )
        if run_logger:
            run_logger.info(
                f"    snowflake: {bin_link}/bin -> {node.bin_dir}"
            )
        return source

    def environment(self, plan, node):
        peers = plan.spock_nodes
        return {
            "EDGE_CLUSTER_DIR": self.shim_dir(),
            "EDGE_COMPONENT": f"pg{plan.pg_major}",
            # The harness takes n2's port as n1's + 1. Where the real second
            # node sits elsewhere, that assumption is wrong and the run says so
            # rather than silently testing one node twice.
            "EDGE_START_PORT": str(peers[0].pg_port),
            "EDGE_DB": plan.db_name,
            "EDGE_USERNAME": plan.db_user,
            "EDGE_PASSWORD": plan.db_password or "",
            "EDGE_HOST": peers[0].address,
            "EDGE_ACTUAL_OUT_DIR": f"{self.output_dir()}/actual",
        }

    def ports_are_contiguous(self, plan):
        peers = plan.spock_nodes
        return len(peers) >= 2 and peers[1].pg_port == peers[0].pg_port + 1

    def execute(self, executor, plan, node, run_logger=None):
        env = " ".join(f"{key}={shlex.quote(value)}"
                       for key, value in self.environment(plan, node).items())
        executor.run(f"mkdir -p {shlex.quote(self.output_dir())}/actual",
                     node=node.name)

        notes = []
        if not self.ports_are_contiguous(plan):
            peers = plan.spock_nodes
            notes.append(
                f"the harness assumes n2 is on n1's port + 1; this cluster has "
                f"{peers[0].name} on {peers[0].pg_port} and {peers[1].name} on "
                f"{peers[1].pg_port}"
            )
            if run_logger:
                run_logger.warn(f"    snowflake: {notes[-1]}")

        log = f"{self.output_dir()}/runner.log"
        ok, output = executor.try_run(
            f"cd {shlex.quote(self.source_dir())}/test && "
            f"{env} python3 runner.py 2>&1 | tee {shlex.quote(log)}",
            node=node.name, timeout=3600,
        )
        if notes:
            output = "\n".join(notes) + "\n" + output
        return ok, output

    def parse(self, output):
        """The harness compares produced output with expected, file by file."""
        import re

        passed = len(re.findall(r"\b(PASS|PASSED|OK)\b", output or ""))
        failed = len(re.findall(r"\b(FAIL|FAILED|ERROR)\b", output or ""))
        if not passed and not failed:
            return {}
        return {"total": passed + failed, "passed": passed, "failed": failed}

    def artefacts(self, executor, node):
        return [f"{self.output_dir()}/actual", f"{self.output_dir()}/runner.log"]
