#!/usr/bin/env python3
"""Put a different Spock build onto a cluster that is already running.

This exists for one job: validating a change to Spock — a pull request, a
commit, a branch — against a real deployed topology rather than a throwaway
two-node sandbox. A sandbox proves the change compiles and the regression
suite passes; it cannot show what the change does under Patroni failover,
across a three-peer mesh, or beside lolor and snowflake. The cluster this
tool deploys can.

What a rebuild does, in order:

  1. builds Spock from the ref on every host, against the PostgreSQL that host
     actually runs (its own pg_config — packaged or source-built), and
     installs it over the one there;
  2. restarts PostgreSQL on every node one at a time through patronictl, so
     each loads the new module while the others keep serving;
  3. runs ALTER EXTENSION spock UPDATE on each Spock node whose SQL version
     moved — a standby replicates the catalog, so it gets this for free;
  4. reads back, on every node, what is loaded and what is created, so the
     report says what the cluster now runs rather than what was asked for.

Why the shared-module caution matters: spock.so is per PostgreSQL prefix, so
every node on a host that shares one is changed by a single `make install`.
A restart of only some of them would leave the cluster running two Spocks
from one file. Hence every node is restarted, always.

The previous build is recorded in the cluster state so a validation can be
undone with the same command and the earlier ref. A packaged cluster is
restored differently — by reinstalling the package — and the report says so.
"""

import time
from datetime import datetime, timezone

from aspects import (cluster_services, package_management, pg_server_management,
                     platform_detect, source_build, spock_management, state)
from aspects.logging_setup import RunLogger, new_run_id
from aspects.ssh_executor import build_executor

#: How long a node may take to accept connections again after its restart.
RESTART_TIMEOUT = 180


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


def _close(cache):
    for executor in cache.values():
        try:
            executor.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# What is running
# ---------------------------------------------------------------------------


def node_versions(executor, plan, node):
    """What one node loads and has created. Every field may be None."""
    def ask(sql, default=None):
        return pg_server_management.scalar(
            executor, node.bin_dir, node.pg_port, plan.db_user, sql,
            dbname=plan.db_name, node=node.name, default=default,
        )

    _, pkglibdir = executor.try_run(
        f"{node.bin_dir}/pg_config --pkglibdir 2>/dev/null", node=node.name)
    module = f"{pkglibdir.strip() or '/unknown'}/spock.so"
    _, checksum = executor.try_run(
        f"md5sum {module} 2>/dev/null | cut -d' ' -f1", node=node.name)

    return {
        "node": node.name,
        "role": node.role,
        "host": node.host,
        # What the loaded library reports about itself: the build.
        "loaded": ask("SELECT spock.spock_version();"),
        # What the catalog says is created: the SQL side.
        "extension": ask(
            "SELECT extversion FROM pg_extension WHERE extname = 'spock';"),
        # What the control file on disk offers: a gap to `extension` means
        # ALTER EXTENSION UPDATE is outstanding.
        "available": ask(
            "SELECT default_version FROM pg_available_extensions "
            "WHERE name = 'spock';"),
        "module": module,
        "checksum": checksum.strip()[:12],
    }


def versions(cluster_name, db_password=None):
    """Per-node Spock versions for a cluster. Returns a list of dicts."""
    plan, _ = state.load(cluster_name, db_password=db_password)
    plan.db_password = state.resolve_password(plan, explicit=db_password)
    cache = {}
    try:
        return [node_versions(_executor_for(plan, node.host, cache, None),
                              plan, node)
                for node in plan.nodes]
    finally:
        _close(cache)


# ---------------------------------------------------------------------------
# The rebuild
# ---------------------------------------------------------------------------


def _wait_until_up(executor, plan, node, timeout=RESTART_TIMEOUT):
    """Block until the node answers a query again, or give up."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        answer = pg_server_management.scalar(
            executor, node.bin_dir, node.pg_port, plan.db_user, "SELECT 1;",
            dbname=plan.db_name, node=node.name, default="",
        )
        if (answer or "").strip() == "1":
            return True
        time.sleep(3)
    return False


def _devel_packages(plan, host, node):
    """Headers and pg_config, which a packaged server does not ship with."""
    major = pg_server_management.major_of(
        str(getattr(node, "pg_version", "") or plan.pg_major)) or plan.pg_major
    return platform_detect.devel_packages(host.family or "rhel", major)


def rebuild(cluster_name, ref, db_password=None, restart=True,
            update_extension=True, jobs=None):
    """Build Spock from `ref` onto every node of a running cluster.

    Returns a result dict: outcome, per-node versions before and after, the
    steps taken, and where the log is. Nothing here raises for a build or a
    restart that fails on one host — that is a result to report, with the
    other hosts' outcomes beside it.
    """
    started = time.time()
    log = RunLogger(new_run_id(f"{cluster_name}-spock-rebuild"))
    cache = {}
    steps = []
    warnings = []

    def executor_for(target):
        return _executor_for(plan, getattr(target, "host", target), cache, log)

    try:
        plan, metadata = state.load(cluster_name, db_password=db_password)
        plan.db_password = state.resolve_password(plan, explicit=db_password)

        log.banner(f"Rebuilding Spock from {ref} on cluster {plan.cluster_name}")

        # --- what is there now ------------------------------------------
        log.step_start("Record what is running", "so the change is measurable")
        before = {}
        for node in plan.nodes:
            before[node.name] = node_versions(executor_for(node), plan, node)
            log.info(f"    {node.name}: loaded {before[node.name]['loaded']}, "
                     f"extension {before[node.name]['extension']}")
        log.step_end("passed", f"{len(before)} node(s)")

        # --- build once per host ----------------------------------------
        # spock.so lives in the server's lib dir, so hosts build, not nodes:
        # every node on a host that shares a prefix picks up one install.
        log.step_start("Build on every host", f"Spock @ {ref}")
        builds = {}
        failed_hosts = []
        for host in plan.hosts:
            node = next((n for n in plan.nodes if n.host == host.name), None)
            if node is None:
                continue
            executor = executor_for(host.name)
            try:
                _, message = source_build.install_build_dependencies(
                    executor, host.family or "rhel", node=host.name)
                log.info(f"    {host.name}: {message}")
                if plan.deploy_mode != "source":
                    package_management.install(
                        executor, host.family or "rhel",
                        _devel_packages(plan, host, node),
                        node=host.name, allow_missing=True)
                spock_dir, revision, describe = source_build.fetch_spock_ref(
                    executor, ref, node=host.name)
                module, checksum = source_build.build_spock_against(
                    executor, spock_dir, node.bin_dir, node=host.name, jobs=jobs)
                builds[host.name] = {
                    "revision": revision, "describe": describe,
                    "module": module, "checksum": checksum[:12],
                }
                log.info(f"    {host.name}: built {describe or revision} "
                         f"({revision}) -> {module}")
            except Exception as exc:              # noqa: BLE001 - reported
                failed_hosts.append(host.name)
                log.error(f"    {host.name}: build failed — {exc}")
        if failed_hosts:
            log.step_end("failed", f"{', '.join(failed_hosts)} did not build")
            # A half-built cluster must not be restarted: the hosts that
            # failed would come back on the old module, the rest on the new.
            raise RuntimeError(
                f"build failed on {', '.join(failed_hosts)}; nothing was "
                f"restarted, every node still runs what it ran before")
        log.step_end("passed", f"{len(builds)} host(s)")

        # --- rolling restart --------------------------------------------
        if restart:
            log.step_start("Restart every node, one at a time",
                           "patronictl restart, so the scope stays served")
            not_back = []
            for node in plan.nodes:
                executor = executor_for(node)
                ok, message = cluster_services.restart(
                    executor, plan, node, component="postgres")
                if not ok:
                    log.warn(f"    {node.name}: restart reported: {message}")
                if _wait_until_up(executor, plan, node):
                    log.info(f"    {node.name}: back")
                else:
                    not_back.append(node.name)
                    log.error(f"    {node.name}: not answering after "
                              f"{RESTART_TIMEOUT}s")
            log.step_end("passed" if not not_back else "failed",
                         f"{len(plan.nodes) - len(not_back)}/{len(plan.nodes)} "
                         f"back")
            if not_back:
                warnings.append(
                    f"{', '.join(not_back)} did not come back after the "
                    f"restart; check `service status` before trusting the "
                    f"versions below")
        else:
            warnings.append("not restarted: every node still runs the old "
                            "module until it restarts")

        # --- extension SQL ----------------------------------------------
        if update_extension and restart:
            log.step_start("Update the extension where its SQL moved",
                           "ALTER EXTENSION spock UPDATE")
            updated = []
            for node in plan.spock_nodes:
                executor = executor_for(node)
                current = node_versions(executor, plan, node)
                if (current["available"] and current["extension"]
                        and current["available"] != current["extension"]):
                    code, output = pg_server_management.psql(
                        executor, node.bin_dir, node.pg_port, plan.db_user,
                        "ALTER EXTENSION spock UPDATE;",
                        dbname=plan.db_name, node=node.name, check=False)
                    if code == 0:
                        updated.append(node.name)
                        log.info(f"    {node.name}: {current['extension']} -> "
                                 f"{current['available']}")
                    else:
                        warnings.append(
                            f"{node.name}: ALTER EXTENSION spock UPDATE "
                            f"failed — {output.strip()[:200]}")
                        log.warn(f"    {node.name}: update failed — "
                                 f"{output.strip()[:200]}")
            log.step_end("passed",
                         f"{len(updated)} updated" if updated else "none needed")

        # --- what is there now ------------------------------------------
        log.step_start("Record what is running now", "")
        after = {}
        for node in plan.nodes:
            after[node.name] = node_versions(executor_for(node), plan, node)
            log.info(f"    {node.name}: loaded {after[node.name]['loaded']}, "
                     f"extension {after[node.name]['extension']}, "
                     f"module {after[node.name]['checksum']}")
        log.step_end("passed", f"{len(after)} node(s)")

        checksums = {b["checksum"] for b in builds.values()}
        if len(checksums) > 1:
            warnings.append(
                "hosts built different spock.so files from the same ref "
                "(different compilers or headers); results may differ per host")

        # --- remember ---------------------------------------------------
        previous = (metadata or {}).get("spock_rebuild") or {}
        record = {
            "ref": ref,
            "revisions": {h: b["revision"] for h, b in builds.items()},
            "at": datetime.now(timezone.utc).isoformat(),
            "previous_ref": previous.get("ref")
                            or (plan.source_build or {}).get("spock_branch")
                            or ("package" if plan.deploy_mode != "source"
                                else ""),
            "deploy_mode": plan.deploy_mode,
        }
        if plan.deploy_mode == "source":
            plan.source_build = dict(plan.source_build or {},
                                     spock_branch=ref)
        new_metadata = dict(metadata or {}, spock_rebuild=record)
        state.save(plan, extra=new_metadata)

        return {
            "outcome": "succeeded" if not warnings else "succeeded_with_warnings",
            "cluster": plan.cluster_name,
            "ref": ref,
            "builds": builds,
            "before": before,
            "after": after,
            "previous_ref": record["previous_ref"],
            "deploy_mode": plan.deploy_mode,
            "warnings": warnings,
            "steps": log.steps if hasattr(log, "steps") else steps,
            "duration": time.time() - started,
            "log_dir": str(log.root),
        }
    except Exception as exc:                      # noqa: BLE001 - reported
        log.error(str(exc))
        return {
            "outcome": "failed",
            "cluster": cluster_name,
            "ref": ref,
            "error": str(exc),
            "warnings": warnings,
            "steps": log.steps if hasattr(log, "steps") else steps,
            "duration": time.time() - started,
            "log_dir": str(log.root),
        }
    finally:
        _close(cache)
