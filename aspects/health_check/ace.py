#!/usr/bin/env python3
"""ACE, run against the cluster it was installed to check.

ACE's own test suite (`ace-integration` here) starts a three-node cluster of
its own in containers and proves nothing about the one this tool deployed.
What *does* say something is ACE itself: `spock-diff`, `schema-diff` and
`repset-diff` compare the deployed nodes with each other, which is the job the
binary was put on the host for. So the ace health check is those three
commands, pointed at this cluster, read as pass or fail.

Two things ACE needs that the host does not have until this runs:

  * a `pg_service.conf` in ACE's shape — a base section named after the
    cluster and one `[cluster.node]` section per node, each with host, port,
    dbname, user and password. The one this tool writes for psql has only
    `[node]` sections, so a separate file is written into the suite's own
    workspace and named through ACE_PGSERVICEFILE, leaving the operator's
    untouched. Only Spock nodes are listed: a standby holds the same data as
    its leader, and comparing the two is comparing a node with itself.
  * an `ace.yaml`, which ACE insists on even when every value is a default.
    `ace config init` writes one; `default_cluster` is then set so each
    command needs no cluster argument.

How a run is judged: ACE's exit code, plus its own words. schema-diff exits
with a ranked code (16..64) for a difference; table comparisons print
`TABLES DO NOT MATCH` and `Found N differences`. Either makes that check a
failure, and the JSON reports ACE writes are kept as artefacts.
"""

import re
import shlex

from aspects.health_check.runner import Suite

#: Where the binary may be: a package puts it on PATH, a source build in
#: /usr/local/bin, which a non-login shell over SSH may not search.
CANDIDATES = ("ace", "/usr/local/bin/ace", "/usr/bin/ace")

#: Patterns in ACE's output that mean a difference was found even when the
#: process exits zero.
MISMATCH = re.compile(r"DO NOT MATCH|Found \d+ differences|MISMATCH", re.I)


class AceSuite(Suite):
    name = "ace"
    repo = ""          # nothing to clone: the binary is already installed
    requires = {}      # the binary is probed by path, below
    targets_cluster = True
    test_timeout = 1800

    #: What is run, in order. (label, arguments after the binary).
    CHECKS = (
        ("spock-diff", "spock-diff"),
        ("schema-diff", "schema-diff {cluster} public"),
        ("repset-diff", "repset-diff {cluster} default"),
    )

    # --- can it run here ----------------------------------------------
    def binary(self, executor, node):
        for candidate in CANDIDATES:
            if "/" in candidate:
                found, _ = executor.try_run(f"test -x {candidate}",
                                            node=node.name)
            else:
                found, _ = executor.try_run(
                    f"command -v {candidate} >/dev/null 2>&1", node=node.name)
            if found:
                return candidate
        return ""

    def missing_requirements(self, executor, plan, node):
        missing = super().missing_requirements(executor, plan, node)
        if not self.binary(executor, node):
            missing.append("the ace binary (pgedge-ace, or "
                           "--pg-extensions pgedge-ace:source)")
        return missing

    # --- put what it needs on the host --------------------------------
    def service_file(self):
        return f"{self.workspace()}/pg_service.conf"

    def config_file(self):
        return f"{self.workspace()}/ace.yaml"

    def service_content(self, plan):
        """pg_service.conf in ACE's shape: [cluster] then [cluster.node]."""
        def section(title, node):
            return [f"[{title}]",
                    f"host={node.address}",
                    f"port={node.pg_port}",
                    f"dbname={plan.db_name}",
                    f"user={plan.db_user}",
                    f"password={plan.db_password}",
                    ""]

        nodes = plan.spock_nodes
        lines = ["# Managed by pg-cluster-deployment: the ace health check", ""]
        lines += section(plan.cluster_name, nodes[0])
        for node in nodes:
            lines += section(f"{plan.cluster_name}.{node.name}", node)
        return "\n".join(lines)

    def prepare(self, executor, plan, node, ref, run_logger=None):
        workspace = self.workspace()
        executor.run(f"mkdir -p {shlex.quote(self.output_dir())}",
                     node=node.name)
        executor.write_file(self.service_file(), self.service_content(plan),
                            owner=plan.db_user, mode="600", node=node.name)

        binary = self.binary(executor, node)
        config = self.config_file()
        # ACE's own generator first, so the file carries every default ACE
        # expects; a minimal file if this ACE predates `config init`.
        executor.try_run(
            f"rm -f {shlex.quote(config)} && cd {shlex.quote(workspace)} && "
            f"{binary} config init --path {shlex.quote(config)} >/dev/null 2>&1 "
            f"|| printf 'default_cluster: \"\"\\n' > {shlex.quote(config)}",
            node=node.name)
        executor.try_run(
            f"sed -i 's/^default_cluster:.*/default_cluster: "
            f"\"{plan.cluster_name}\"/' {shlex.quote(config)}",
            node=node.name)
        executor.try_run(
            f"chown {plan.db_user} {shlex.quote(config)} "
            f"{shlex.quote(self.service_file())}", node=node.name)
        if run_logger:
            run_logger.info(
                f"    ace: {binary} against {len(plan.spock_nodes)} node(s) "
                f"of '{plan.cluster_name}', config in {workspace}")
        return workspace

    # --- run it ---------------------------------------------------------
    def execute(self, executor, plan, node, run_logger=None):
        binary = self.binary(executor, node)
        env = (f"ACE_PGSERVICEFILE={shlex.quote(self.service_file())} "
               f"HOME={shlex.quote(self.workspace())} "
               f"PATH=/usr/local/bin:$PATH")
        lines = []
        passed = failed = 0

        for label, arguments in self.CHECKS:
            command = arguments.format(cluster=plan.cluster_name)
            log = f"{self.output_dir()}/{label}.log"
            # Run from the output dir so ACE's JSON reports land there; the
            # config lives beside the service file, named explicitly. Output
            # goes to the log and is then read back: a pipe would hide ACE's
            # exit status, and PIPESTATUS is bash's alone.
            ok, output = executor.try_run(
                f"cd {shlex.quote(self.output_dir())} && {env} "
                f"{binary} {command} --dbname {shlex.quote(plan.db_name)} "
                f"--quiet > {shlex.quote(log)} 2>&1; rc=$?; "
                f"cat {shlex.quote(log)}; exit $rc",
                node=node.name, timeout=self.test_timeout,
            )
            text = (output or "").strip()
            mismatch = MISMATCH.search(text)
            if ok and not mismatch:
                verdict = "ok"
                passed += 1
            else:
                reason = (mismatch.group(0) if mismatch
                          else _last_line(text) or "non-zero exit")
                verdict = f"FAILED — {reason[:160]}"
                failed += 1
            lines.append(f"test {label:<28} ... {verdict}")
            if run_logger:
                run_logger.info(f"      {label:<28} {verdict}")

        total = passed + failed
        if failed:
            lines.append(f"{failed} of {total} tests failed.")
        else:
            lines.append(f"All {total} tests passed.")
        return failed == 0, "\n".join(lines)

    def parse(self, output):
        from aspects.health_check.runner import parse_pg_regress
        return parse_pg_regress(output)

    def artefacts(self, executor, node):
        _, listing = executor.try_run(
            f"ls -1 {shlex.quote(self.output_dir())}/*.json "
            f"{shlex.quote(self.output_dir())}/*.log 2>/dev/null",
            node=node.name)
        return [line.strip() for line in (listing or "").splitlines()
                if line.strip()]


def _last_line(text):
    lines = [line for line in text.splitlines() if line.strip()]
    return lines[-1].strip() if lines else ""
