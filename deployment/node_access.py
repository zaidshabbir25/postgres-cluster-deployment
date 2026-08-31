#!/usr/bin/env python3
"""Reach into a cluster: run commands, open a shell, list what is there.

The pgEdge CLI's `cluster command`, `cluster ssh` and `cluster list-nodes`, plus
the package-inventory half of its `um` module.

`command` deliberately offers two scopes, because they answer different
questions. A shell command tells you about the machine; a SQL statement tells
you about the database. Running the same SQL on every node and lining the
answers up side by side is the single most useful thing this file does — it is
how you see that one node disagrees.
"""

import os
import shlex
import subprocess
import sys

from aspects import package_management, pg_server_management as pg
from aspects import patroni_management, platform_detect, state
from aspects.ssh_executor import build_executor, REPO_ROOT


class NodeAccessError(RuntimeError):
    pass


def _host_config(host):
    return {
        "name": host.name,
        "host": host.address,
        "username": host.username,
        "key_file": host.key_file,
        "port": host.port,
        "local": host.local,
    }


def executor_pool(plan, run_logger=None):
    """Returns (executor_for(node_or_host_name), cache) sharing connections."""
    cache = {}

    def executor_for(target):
        host_name = getattr(target, "host", target)
        if host_name not in cache:
            executor = build_executor(_host_config(plan.host(host_name)),
                                      run_logger=run_logger)
            executor.connect()
            cache[host_name] = executor
        return cache[host_name]

    return executor_for, cache


def close(cache):
    for executor in cache.values():
        try:
            executor.close()
        except Exception:
            pass


def resolve_nodes(plan, selector):
    """Turn a selector into a list of nodes.

    Accepts 'all', 'spock', 'standby', a comma-separated list of node names, or
    a host name (meaning every node on that host).
    """
    if not selector or selector == "all":
        return list(plan.nodes)
    if selector == "spock":
        return list(plan.spock_nodes)
    if selector == "standby":
        return list(plan.standby_nodes)

    wanted = [part.strip() for part in selector.split(",") if part.strip()]
    known_nodes = {n.name: n for n in plan.nodes}
    known_hosts = {h.name for h in plan.hosts}
    selected, unknown = [], []

    for name in wanted:
        if name in known_nodes:
            selected.append(known_nodes[name])
        elif name in known_hosts:
            selected.extend(plan.nodes_on(name))
        else:
            unknown.append(name)

    if unknown:
        raise NodeAccessError(
            f"unknown node or host: {', '.join(unknown)}. Nodes: "
            f"{', '.join(known_nodes)}. Hosts: {', '.join(sorted(known_hosts))}."
        )
    # Preserve plan order and drop duplicates from overlapping selectors.
    seen = set()
    ordered = []
    for node in plan.nodes:
        if node.name in {n.name for n in selected} and node.name not in seen:
            seen.add(node.name)
            ordered.append(node)
    return ordered


# ---------------------------------------------------------------------------
# list-nodes
# ---------------------------------------------------------------------------


def list_nodes(cluster_name, db_password=None, probe=True):
    """Every node in a cluster, optionally with live role and reachability."""
    plan, metadata = state.load(cluster_name, db_password=db_password)
    plan.db_password = state.resolve_password(plan, explicit=db_password)

    entries = []
    executor_for, cache = executor_pool(plan)
    try:
        for node in plan.nodes:
            entry = {
                "node": node.name,
                "role": node.role,
                "host": node.host,
                "address": node.address,
                "pg_port": node.pg_port,
                "restapi_port": node.restapi_port,
                "scope": node.scope,
                "follows": node.leader,
                "standbys": node.standbys,
                "data_dir": node.data_dir,
                "pg_version": node.pg_version,
            }
            if probe:
                try:
                    executor = executor_for(node)
                    rest = patroni_management.rest_health(
                        executor, node, node_name=node.name
                    )
                    entry["reachable"] = True
                    entry["patroni_role"] = (rest or {}).get("role")
                    entry["patroni_state"] = (rest or {}).get("state")
                    entry["accepting_writes"] = (
                        None if rest is None
                        else (rest.get("role") or "").lower() in
                        ("leader", "master", "primary")
                    )
                except Exception as exc:
                    entry["reachable"] = False
                    entry["error"] = str(exc)
            entries.append(entry)
    finally:
        close(cache)

    return {"cluster": cluster_name, "nodes": entries, "metadata": metadata,
            "plan": plan}


def format_nodes(result):
    """Render list_nodes() as an aligned table."""
    lines = [
        f"Cluster {result['cluster']} — {len(result['nodes'])} node(s)",
        "",
        f"{'NODE':<10} {'ROLE':<9} {'HOST':<14} {'ADDRESS:PORT':<22} "
        f"{'PATRONI':<9} {'WRITES':<7} {'SCOPE':<20} FOLLOWS",
    ]
    for node in result["nodes"]:
        writes = {True: "yes", False: "no", None: "?"}.get(
            node.get("accepting_writes"), "?"
        )
        if node.get("reachable") is False:
            writes = "-"
        lines.append(
            f"{node['node']:<10} {node['role']:<9} {node['host']:<14} "
            f"{node['address'] + ':' + str(node['pg_port']):<22} "
            f"{str(node.get('patroni_role') or '-'):<9} {writes:<7} "
            f"{node['scope']:<20} {node.get('follows') or '-'}"
        )
    unreachable = [n["node"] for n in result["nodes"]
                   if n.get("reachable") is False]
    if unreachable:
        lines += ["", f"Unreachable: {', '.join(unreachable)}"]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# command
# ---------------------------------------------------------------------------


def run_command(cluster_name, selector, command, kind="shell", db_password=None,
                user="root", run_logger=None):
    """Run a shell command or SQL statement on the selected nodes.

    Returns a result dict with one entry per node. Never raises for a failing
    remote command — a non-zero exit is data, and the caller wants to see which
    node produced it.
    """
    plan, _ = state.load(cluster_name, db_password=db_password)
    plan.db_password = state.resolve_password(plan, explicit=db_password)

    nodes = resolve_nodes(plan, selector)
    if not nodes:
        raise NodeAccessError(f"selector {selector!r} matched no nodes")

    executor_for, cache = executor_pool(plan, run_logger=run_logger)
    results = []

    try:
        for node in nodes:
            try:
                executor = executor_for(node)
            except Exception as exc:
                results.append({"node": node.name, "host": node.host,
                                "ok": False, "exit_code": None,
                                "output": f"unreachable: {exc}"})
                continue

            if kind == "sql":
                code, output = pg.psql(
                    executor, node.bin_dir, node.pg_port, plan.db_user,
                    command, dbname=plan.db_name, node=node.name, check=False,
                )
            else:
                code, output = executor.exec_run(command, user=user,
                                                 node=node.name)
            results.append({"node": node.name, "host": node.host,
                            "ok": code == 0, "exit_code": code,
                            "output": output.rstrip()})
    finally:
        close(cache)

    return {"cluster": cluster_name, "kind": kind, "command": command,
            "selector": selector, "results": results,
            "failed": [r["node"] for r in results if not r["ok"]]}


def format_command(result, compare=False):
    """Render run_command() output.

    With compare=True, nodes whose output is identical are grouped — which turns
    "run this on 6 nodes" into a one-line answer when they agree and an obvious
    split when they do not.
    """
    lines = [f"$ {result['command']}"
             + (f"   [{result['kind']}]" if result["kind"] != "shell" else ""), ""]

    if compare:
        groups = {}
        for entry in result["results"]:
            groups.setdefault(entry["output"], []).append(entry["node"])
        if len(groups) == 1:
            output, nodes = next(iter(groups.items()))
            lines.append(f"All {len(nodes)} node(s) agree ({', '.join(nodes)}):")
            lines += [f"  {line}" for line in (output or "(no output)").splitlines()]
        else:
            lines.append(f"{len(groups)} distinct answers across "
                         f"{len(result['results'])} node(s):")
            for output, nodes in groups.items():
                lines.append("")
                lines.append(f"  {', '.join(nodes)}:")
                lines += [f"    {line}"
                          for line in (output or "(no output)").splitlines()]
    else:
        for entry in result["results"]:
            marker = "" if entry["ok"] else f"  [exit {entry['exit_code']}]"
            lines.append(f"--- {entry['node']} ({entry['host']}){marker}")
            body = entry["output"] or "(no output)"
            lines += [f"  {line}" for line in body.splitlines()]
            lines.append("")

    if result["failed"]:
        lines.append(f"Failed on: {', '.join(result['failed'])}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# ssh
# ---------------------------------------------------------------------------


def ssh_command(cluster_name, node_name, db_password=None):
    """Build the ssh command line that opens a shell on a node's host.

    Returns (argv, description). The caller execs it, so the user gets a real
    interactive terminal — paramiko would give a pseudo-shell with no job
    control, which is worse than useless for troubleshooting.
    """
    plan, _ = state.load(cluster_name, db_password=db_password)

    node = None
    host = None
    for candidate in plan.nodes:
        if candidate.name == node_name:
            node = candidate
            host = plan.host(candidate.host)
            break
    if host is None:
        for candidate in plan.hosts:
            if candidate.name == node_name:
                host = candidate
                break
    if host is None:
        raise NodeAccessError(
            f"{node_name!r} is not a node or host in cluster {cluster_name!r}. "
            f"Nodes: {', '.join(n.name for n in plan.nodes)}. "
            f"Hosts: {', '.join(h.name for h in plan.hosts)}."
        )

    argv = ["ssh", "-o", "StrictHostKeyChecking=accept-new"]
    if host.port and int(host.port) != 22:
        argv += ["-p", str(host.port)]
    if host.key_file:
        key_path = host.key_file
        if not os.path.isabs(key_path):
            key_path = str(REPO_ROOT / key_path)
        argv += ["-i", key_path]
    argv.append(f"{host.username}@{host.address}")

    description = f"{host.username}@{host.address}"
    if node is not None:
        description += (
            f"  (node {node.name}: PostgreSQL on :{node.pg_port}, "
            f"data in {node.data_dir}, config {node.config_file})"
        )
    return argv, description


def open_shell(cluster_name, node_name, db_password=None):
    """Exec an interactive ssh session. Does not return on success."""
    argv, description = ssh_command(cluster_name, node_name,
                                    db_password=db_password)
    print(f"Connecting to {description}")
    print(f"  {' '.join(shlex.quote(a) for a in argv)}\n")
    try:
        os.execvp(argv[0], argv)
    except FileNotFoundError:
        raise NodeAccessError(
            "the ssh client is not installed on this machine; the command line "
            f"to run by hand is:\n  {' '.join(shlex.quote(a) for a in argv)}"
        )


def psql_command(cluster_name, node_name, db_password=None):
    """Build a psql command line for a node, for an interactive session."""
    plan, _ = state.load(cluster_name, db_password=db_password)
    try:
        node = plan.node(node_name)
    except KeyError:
        raise NodeAccessError(
            f"{node_name!r} is not a node in cluster {cluster_name!r}"
        )
    argv, _ = ssh_command(cluster_name, node_name, db_password=db_password)
    remote = (
        f"{node.bin_dir}/psql -h 127.0.0.1 -p {node.pg_port} "
        f"-U {plan.db_user} -d {plan.db_name}"
    )
    # -t forces a pty so psql runs as a real interactive client.
    return argv[:1] + ["-t"] + argv[1:] + [
        f"sudo -u {plan.db_user} {remote}"
    ], f"psql on {node.name} ({node.address}:{node.pg_port})"


def open_psql(cluster_name, node_name, db_password=None):
    """Exec an interactive psql session on a node."""
    argv, description = psql_command(cluster_name, node_name,
                                     db_password=db_password)
    print(f"Connecting: {description}\n")
    try:
        os.execvp(argv[0], argv)
    except FileNotFoundError:
        raise NodeAccessError(
            f"ssh not found; run this by hand:\n  "
            f"{' '.join(shlex.quote(a) for a in argv)}"
        )


# ---------------------------------------------------------------------------
# packages (the CLI's `um` module, over native package managers)
# ---------------------------------------------------------------------------


def package_list(cluster_name, selector="all", db_password=None):
    """Installed pgEdge packages per host, with agreement flagged.

    Version drift between nodes is the thing worth catching here: a cluster
    where one node runs a different Spock build is a cluster that can fail in
    ways no single-node test reproduces.
    """
    plan, _ = state.load(cluster_name, db_password=db_password)
    plan.db_password = state.resolve_password(plan, explicit=db_password)

    nodes = resolve_nodes(plan, selector)
    host_names = []
    for node in nodes:
        if node.host not in host_names:
            host_names.append(node.host)

    executor_for, cache = executor_pool(plan)
    per_host, problems = {}, []
    try:
        for host_name in host_names:
            host = plan.host(host_name)
            try:
                executor = executor_for(host_name)
            except Exception as exc:
                problems.append(f"{host_name}: unreachable ({exc})")
                continue
            family = host.family or platform_detect.detect(executor)["family"]
            entries = package_management.list_pgedge_packages(
                executor, family, node=host_name
            )
            per_host[host_name] = {e["package"]: e["version"] for e in entries}
    finally:
        close(cache)

    # Compare versions across hosts.
    all_packages = sorted({p for versions in per_host.values() for p in versions})
    drift = {}
    for package in all_packages:
        versions = {h: v.get(package) for h, v in per_host.items()}
        distinct = {v for v in versions.values() if v}
        if len(distinct) > 1:
            drift[package] = versions
            problems.append(
                f"{package} differs across hosts: "
                + ", ".join(f"{h}={v or 'absent'}" for h, v in versions.items())
            )
        missing = [h for h, v in versions.items() if not v]
        if missing and len(missing) < len(versions):
            problems.append(
                f"{package} is missing on {', '.join(missing)}"
            )

    return {"cluster": cluster_name, "hosts": per_host, "packages": all_packages,
            "drift": drift, "problems": problems}


def format_packages(result):
    lines = [f"Installed pgEdge packages — cluster {result['cluster']}", ""]
    hosts = sorted(result["hosts"])
    if not hosts:
        lines.append("  (no reachable hosts)")
    else:
        width = max([len(p) for p in result["packages"]] + [7])
        header = f"{'PACKAGE':<{width}}  " + "  ".join(f"{h:<20}" for h in hosts)
        lines += [header, "-" * len(header)]
        for package in result["packages"]:
            row = f"{package:<{width}}  "
            row += "  ".join(
                f"{(result['hosts'][h].get(package) or '-'):<20}" for h in hosts
            )
            lines.append(row)
    if result["problems"]:
        lines += ["", f"Problems ({len(result['problems'])}):"]
        lines += [f"  - {p}" for p in result["problems"]]
    else:
        lines += ["", "All hosts carry the same package versions."]
    return "\n".join(lines)


def package_install(cluster_name, packages, selector="all", db_password=None,
                    run_logger=None):
    """Install packages on the selected nodes' hosts."""
    return _package_action(cluster_name, packages, selector, "install",
                           db_password, run_logger)


def package_remove(cluster_name, packages, selector="all", db_password=None,
                   run_logger=None):
    return _package_action(cluster_name, packages, selector, "remove",
                           db_password, run_logger)


def package_upgrade(cluster_name, packages=None, selector="all",
                    db_password=None, run_logger=None):
    """Upgrade pgEdge packages.

    Upgrading a running cluster's server or Spock package is not a no-op: the
    new shared library only takes effect after a restart, and the restart has
    to be coordinated through Patroni. This installs the packages and then says
    what still needs doing, rather than restarting anything behind your back.
    """
    result = _package_action(cluster_name, packages, selector, "upgrade",
                             db_password, run_logger)
    result["next_steps"] = [
        "New shared libraries are not loaded until PostgreSQL restarts.",
        "Restart one scope at a time, standby first, to keep the cluster up:",
        "  pg_cluster_ctl.sh service restart --node <standby> --component postgres",
        "  pg_cluster_ctl.sh service switchover --node <leader>",
        "Then verify: pg_cluster_ctl.sh package list",
    ]
    return result


def _package_action(cluster_name, packages, selector, action, db_password,
                    run_logger):
    plan, _ = state.load(cluster_name, db_password=db_password)
    plan.db_password = state.resolve_password(plan, explicit=db_password)

    nodes = resolve_nodes(plan, selector)
    host_names = []
    for node in nodes:
        if node.host not in host_names:
            host_names.append(node.host)

    if isinstance(packages, str):
        packages = [p.strip() for p in packages.split(",") if p.strip()]

    executor_for, cache = executor_pool(plan, run_logger=run_logger)
    results, problems = [], []
    try:
        for host_name in host_names:
            host = plan.host(host_name)
            try:
                executor = executor_for(host_name)
            except Exception as exc:
                problems.append(f"{host_name}: unreachable ({exc})")
                continue
            family = host.family or platform_detect.detect(executor)["family"]

            if action == "remove":
                removed = package_management.remove(
                    executor, family, packages, node=host_name
                )
                results.append({"host": host_name, "removed": removed})
                if run_logger:
                    run_logger.info(f"    {host_name}: removed "
                                    f"{', '.join(removed) or 'nothing'}")
                continue

            targets = packages
            if action == "upgrade" and not targets:
                installed = package_management.list_pgedge_packages(
                    executor, family, node=host_name
                )
                targets = [e["package"] for e in installed]

            if not targets:
                problems.append(f"{host_name}: no packages to {action}")
                continue

            if action == "upgrade":
                pm = platform_detect.package_manager(family)
                verb = ("dnf upgrade -y" if family == "rhel"
                        else "DEBIAN_FRONTEND=noninteractive apt-get install "
                             "-y --only-upgrade")
                ok, output = executor.try_run(
                    f"{verb} {' '.join(targets)}", node=host_name, timeout=3600
                )
                results.append({"host": host_name, "packages": targets,
                                "ok": ok, "output": output.strip()[-2000:]})
                if not ok:
                    problems.append(f"{host_name}: upgrade failed")
            else:
                installed, failures = package_management.install(
                    executor, family, targets, node=host_name, allow_missing=True
                )
                results.append({"host": host_name, "installed": installed,
                                "failures": [f[0] for f in failures]})
                for name, detail in failures:
                    problems.append(f"{host_name}: {name} — {detail[:200]}")
            if run_logger:
                run_logger.info(f"    {host_name}: {action} completed")
    finally:
        close(cache)

    return {"cluster": cluster_name, "action": action, "results": results,
            "problems": problems, "ok": not problems}
