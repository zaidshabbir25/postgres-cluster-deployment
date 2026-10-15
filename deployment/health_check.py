#!/usr/bin/env python3
"""Run each component's upstream regression suite against a deployed cluster.

This is `--health_check spock,lolor,snowflake,ace`. It loads the saved cluster,
picks one host to run from, and gives each component its own workspace, its own
clone and its own report. One suite failing neither stops the next nor
contaminates its artefacts — which is the point of keeping them separate.

The ref each suite is fetched at follows what the cluster is running, not what
is newest: a cluster built from spock v5.0.5 is tested by v5.0.5's tests,
because a newer suite tests behaviour the installed extension does not have and
the failures it produces are about version skew rather than the cluster.
"""

import time

from aspects import pg_extensions, source_build, state
from aspects.logging_setup import RunLogger, new_run_id
from aspects.ssh_executor import build_executor
from regression import runner as regression_runner


class HealthCheckError(RuntimeError):
    pass


def _executor_for(plan, host_name, cache, run_logger):
    if host_name not in cache:
        host = plan.host(host_name)
        executor = build_executor(
            {
                "name": host.name,
                "host": host.address,
                "username": host.username,
                "key_file": host.key_file,
                "port": host.port,
                "local": host.local,
            },
            run_logger=run_logger,
        )
        executor.connect()
        cache[host_name] = executor
    return cache[host_name]


def ref_for(plan, component):
    """The branch or tag whose tests match what this cluster is running.

    Spock's cases ship with this tool rather than being fetched, so there is no
    ref to choose; the version it reports is the extension's own.
    """
    if component == "spock":
        return "in-tree"

    for item in pg_extensions.selections_of(plan):
        if item["name"] == component and item.get("ref"):
            return item["ref"]
    spec = pg_extensions.CATALOG.get(component)
    return spec.default_ref if spec else "main"


def run(cluster_name, components, db_password=None, node_name=None,
        run_logger=None, keep_going=True, install_requirements=False,
        tests=None, test_timeout=None):
    """Run the selected suites. Returns a result dict."""
    plan, metadata = state.load(cluster_name, db_password=db_password)
    plan.db_password = state.resolve_password(plan, explicit=db_password)

    log = run_logger or RunLogger(new_run_id(f"{cluster_name}-health-check"))
    executors = {}
    started = time.time()
    results = []

    try:
        if not plan.spock_nodes:
            raise HealthCheckError(
                f"cluster {cluster_name!r} has no Spock node to test against"
            )
        node = plan.node(node_name) if node_name else plan.spock_nodes[0]
        if not node.is_spock:
            raise HealthCheckError(
                f"{node.name} is a standby; the suites need a node that "
                f"accepts writes. Pick one of: "
                f"{', '.join(n.name for n in plan.spock_nodes)}"
            )

        log.banner(f"Regression suites for '{cluster_name}'")
        log.info(f"    running on  : {node.name} ({node.address}:{node.pg_port})")
        log.info(f"    components  : {', '.join(components)}")
        log.info(f"    workspaces  : {regression_runner.REGRESSION_ROOT}/<component>")
        log.info("")

        executor = _executor_for(plan, node.host, executors, log)

        for component in components:
            suite = regression_runner.suite_for(
                component, tests=tests, test_timeout=test_timeout)
            ref = ref_for(plan, component)
            log.step_start(
                f"{component} regression",
                f"{suite.repo.rsplit('/', 1)[-1]}@{ref} in {suite.workspace()}"
                + ("" if suite.targets_cluster
                   else " — starts its own cluster, does not read this one"),
            )
            result = suite.run(executor, plan, node, ref, run_logger=log,
                               install_requirements=install_requirements)
            results.append(result)

            status = {"passed": "passed", "failed": "failed",
                      "skipped": "skipped"}[result.status]
            log.step_end(status, result.summary())
            if result.output:
                log.node(f"{component}-regression", result.output)
            if result.status == "failed" and not keep_going:
                log.warn("stopping after the first failure (--stop-on-failure)")
                break

        duration = round(time.time() - started, 1)
        failed = [r for r in results if r.status == "failed"]
        ran = [r for r in results if r.status != "skipped"]
        # Nothing failing is not the same as something passing. A run where
        # every suite skipped tested nothing, and a banner saying "succeeded"
        # would be read as evidence it did.
        outcome = ("failed" if failed else
                   "succeeded" if ran else "nothing ran")

        log.banner(f"Regression suites {outcome} in {duration}s")
        for line in summary_lines(results, log_dir=str(log.root)):
            log.info(line)

        return {
            "outcome": outcome,
            "cluster": cluster_name,
            "node": node.name,
            "components": list(components),
            "results": [vars(r) for r in results],
            "duration": duration,
            "steps": log.steps,
            "log_dir": str(log.root),
            "plan": plan,
        }

    except Exception as exc:
        log.error(f"Regression run failed — {exc}")
        return {
            "outcome": "failed",
            "cluster": cluster_name,
            "failure": str(exc),
            "results": [vars(r) for r in results],
            "steps": log.steps,
            "log_dir": str(log.root),
        }
    finally:
        for executor in executors.values():
            try:
                executor.close()
            except Exception:
                pass


def summary_lines(results, log_dir=""):
    """The table printed when the run finishes."""
    if not results:
        return []

    rows = [(r.component, r.ref, r.status.upper(), r.summary()) for r in results]
    headers = ("COMPONENT", "REF", "RESULT", "DETAIL")
    widths = [max(len(str(row[i])) for row in (headers, *rows))
              for i in range(len(headers))]

    def line(cells):
        return "  ".join(f"{str(c):<{widths[i]}}" for i, c in
                         enumerate(cells)).rstrip()

    out = ["", line(headers), "  ".join("-" * w for w in widths)]
    out += [line(row) for row in rows]

    if all(r.status == "skipped" for r in results):
        out.append("")
        out.append("  Every suite was skipped: nothing was tested.")

    detached = [r.component for r in results if not r.targets_cluster
                and r.status != "skipped"]
    if detached:
        out.append("")
        out.append(
            f"  {', '.join(detached)} ran against containers it started itself, "
            f"not against this cluster."
        )

    failed = [r for r in results if r.status == "failed"]
    if failed:
        out.append("")
        for result in failed:
            out.append(f"  {result.component}: {result.detail}")
            for path in result.artefacts:
                out.append(f"    kept: {path}")
        if log_dir:
            out.append(f"  Full output per suite: {log_dir}/<component>-regression.log")
    return out
